import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import AfterValidator, BaseModel, Field, Strict, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # does not override variables that are already set in the environment

logger = logging.getLogger("attendance")

# tz_aware=True makes PyMongo return UTC-aware datetimes instead of naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
GRACE_PERIOD = timedelta(minutes=10)
MIN_OVERTIME_MINUTES = 30
HALF_DAY_BELOW_HOURS = Decimal("4.50")
MAX_SHIFT_LENGTH = timedelta(hours=24)
MAX_TREND_DAYS = 92
PRESENCE_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]

Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

# Fields that a correction can change, in the order they appear in history entries.
TRACKED_FIELDS = ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]


# --------------------------------------------------------------------------- #
# Validation types
# --------------------------------------------------------------------------- #
def _check_date(value: str) -> str:
    # date.fromisoformat alone would also accept "20260706", so check the shape first.
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        raise ValueError("must be a date in YYYY-MM-DD format")
    date.fromisoformat(value)  # rejects impossible dates such as 2026-02-30
    return value


DateStr = Annotated[str, AfterValidator(_check_date)]
MonthStr = Annotated[str, Query(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")]
ShiftTime = Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
# Strict: floats, strings and booleans are rejected instead of being coerced to int.
EpochMs = Annotated[int, Strict(), Field(ge=100_000_000_000, le=4_102_444_800_000)]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]


def validation_error(loc: list[str], msg: str):
    """Raise a 422 with FastAPI's default validation body, for rules Pydantic can't express."""
    raise RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": None}])


# --------------------------------------------------------------------------- #
# Time and business-rule helpers (R1 - R5)
# --------------------------------------------------------------------------- #
def from_ms(ms: int) -> datetime:
    # R1: truncate to whole seconds before anything else.
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def to_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    return (value - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse_hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def is_overnight(employee: dict) -> bool:
    return employee["shift_end"] <= employee["shift_start"]


def attendance_date(punch_in: datetime, employee: dict) -> str:
    """R1: the IST date of the punch-in, or the previous day for the early part of an overnight shift."""
    local = punch_in.astimezone(IST)
    if is_overnight(employee) and local.time() < parse_hhmm(employee["shift_end"]):
        return (local.date() - timedelta(days=1)).isoformat()
    return local.date().isoformat()


def shift_window(day: str, employee: dict) -> tuple[datetime, datetime]:
    """Start and end instants of the shift that starts on `day` (IST)."""
    start_day = date.fromisoformat(day)
    start = datetime.combine(start_day, parse_hhmm(employee["shift_start"]), IST)
    end = datetime.combine(start_day, parse_hhmm(employee["shift_end"]), IST)
    if is_overnight(employee):
        end += timedelta(days=1)
    return start, end


def round_half_up(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def compute_late_minutes(punch_in: datetime, shift_start: datetime) -> int:
    """R2: late only when strictly more than 10:00 after shift start; counted from shift start."""
    elapsed = punch_in - shift_start
    return elapsed // timedelta(minutes=1) if elapsed > GRACE_PERIOD else 0


def compute_overtime(punch_out: datetime, shift_end: datetime) -> int:
    """R3: whole minutes after shift end, only when at least 30."""
    minutes = (punch_out - shift_end) // timedelta(minutes=1)
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> Decimal:
    """R4: exact seconds / 3600, rounded half-up (Decimal avoids float and banker's rounding)."""
    seconds = (punch_out - punch_in) // timedelta(seconds=1)
    return round_half_up(Decimal(seconds) / 3600, 2)


def derived_fields(
    status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], day: str, employee: dict
) -> dict:
    """The four stored derived values for a record in its final state (DATA_MODEL.md section 3)."""
    if status not in PRESENCE_STATUSES:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    shift_start, shift_end = shift_window(day, employee)
    fields = {
        "work_hours": None,
        "late_minutes": compute_late_minutes(punch_in, shift_start),
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        hours = compute_work_hours(punch_in, punch_out)
        fields["work_hours"] = float(hours)
        fields["overtime_minutes"] = compute_overtime(punch_out, shift_end)
        fields["half_day"] = hours < HALF_DAY_BELOW_HOURS  # R5 uses the rounded value
    return fields


def check_punch_out(punch_in: datetime, punch_out: datetime, loc: list[str]) -> None:
    if punch_out <= punch_in:
        validation_error(loc, "punch_out must be after punch_in")
    if punch_out - punch_in > MAX_SHIFT_LENGTH:
        validation_error(loc, "punch_out must be within 24 hours of punch_in")


def month_bounds(month: str) -> tuple[str, str]:
    first = date.fromisoformat(f"{month}-01")
    next_month = (first + timedelta(days=32)).replace(day=1)
    return first.isoformat(), (next_month - timedelta(days=1)).isoformat()


def count_working_days(first: str, last: str) -> int:
    """R7: Mon-Fri between two dates, inclusive. Pure calendar arithmetic, no data involved."""
    day, end = date.fromisoformat(first), date.fromisoformat(last)
    count = 0
    while day <= end:
        count += day.weekday() < 5
        day += timedelta(days=1)
    return count


# --------------------------------------------------------------------------- #
# Serialization (BSON -> API)
# --------------------------------------------------------------------------- #
def _api_value(value: Any) -> Any:
    return to_ms(value) if isinstance(value, datetime) else value


def record_state(doc: dict) -> dict:
    """The tracked fields of a stored record, with the defaults for fields that legacy records omit."""
    return {
        "status": doc["status"],
        "punch_in": doc.get("punch_in"),
        "punch_out": doc.get("punch_out"),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
    }


def serialize_record(doc: dict) -> dict:
    state = record_state(doc)
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        **{field: _api_value(value) for field, value in state.items()},
        "history": [
            {
                "at": to_ms(entry["at"]),
                "by": entry["by"],
                "reason": entry["reason"],
                "changes": {
                    field: {"from": _api_value(change["from"]), "to": _api_value(change["to"])}
                    for field, change in entry["changes"].items()
                },
            }
            for entry in doc.get("history") or []
        ],
    }


def serialize_employee(doc: dict) -> dict:
    fields = ["emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on"]
    return {**{field: doc[field] for field in fields}, "created_at": to_ms(doc["created_at"])}


# --------------------------------------------------------------------------- #
# Indexes and app lifecycle
# --------------------------------------------------------------------------- #
_indexes_ready = False


def ensure_indexes() -> None:
    global _indexes_ready
    # Uniqueness is enforced here, by the database, not by find-then-insert in the endpoints.
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    # GET /employees?department= sorted by emp_code; department lookups in the summary and trend.
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
    # Department summary without a department filter: headcount by joined_on.
    db.employees.create_index([("joined_on", ASCENDING)])
    # One record per employee and day; also serves per-employee lookups and date ranges
    # (monthly report, punch-out, regularize, summary $lookup, trend).
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    # GET /attendance without emp_code (sort date desc, emp_code asc) and month-wide scans (leaderboard).
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError:
        # Keep starting so /health can report 503; indexes are retried on the next successful health check.
        logger.exception("could not create indexes at startup")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


def get_employee_or_404(emp_code: str) -> dict:
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    return employee


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: Annotated[str, Field(pattern=r"^EMP[0-9]{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: ShiftTime = "09:30"
    shift_end: ShiftTime = "18:30"
    joined_on: DateStr

    @model_validator(mode="after")
    def check_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    # Derived fields are not declared, so a client that sends them is ignored (Pydantic drops extra keys).
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]


# --------------------------------------------------------------------------- #
# System and employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "MongoDB is not reachable") from None
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = {**body.model_dump(), "created_at": now_utc()}
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists") from None
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    cursor = db.employees.find(query).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_employee(doc) for doc in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = get_employee_or_404(body.emp_code)
    punched_at = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(punched_at, employee)
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        **derived_fields(body.status, punched_at, None, day, employee),
        "history": [],
    }
    try:
        # The unique (emp_code, date) index makes this the race-safe check: concurrent duplicates fail here.
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date") from None
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = get_employee_or_404(body.emp_code)
    punched_at = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    # The latest punch-in at or before the punch-out, whatever its date, so overnight shifts work.
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}}, sort=[("punch_in", DESCENDING)]
    )
    if record is None or record.get("punch_out") is not None:
        # A punch-out earlier than the open punch-in is a bad timestamp (422), not a missing or closed record.
        if db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$gt": punched_at}, "punch_out": None}
        ):
            validation_error(["body", "punched_at"], "punch_out must be after punch_in")
        if record is None:
            raise HTTPException(404, "no punch-in found")
        raise HTTPException(409, "already punched out")
    check_punch_out(record["punch_in"], punched_at, ["body", "punched_at"])

    fields = derived_fields(record["status"], record["punch_in"], punched_at, record["date"], employee)
    del fields["late_minutes"]  # set at punch-in; punch-out only writes the R3-R5 values
    # Conditional update: only one of several concurrent punch-outs can still see punch_out == null.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": punched_at, **fields}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return serialize_record(updated)


def check_date_range(first: Optional[str], last: Optional[str], loc: list[str]) -> None:
    if first is not None and last is not None and first > last:
        validation_error(loc, "start date must not be after end date")


def attendance_list_query(
    emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str], status: Optional[str]
) -> dict:
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    return query


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    check_date_range(date_from, date_to, ["query", "date_from"])
    query = attendance_list_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    cursor = db.attendance_logs.find(query).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_record(doc) for doc in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: DateStr, body: RegularizeIn):
    employee = get_employee_or_404(emp_code)
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "no attendance record for this date")

    current = record_state(record)
    status = body.status or current["status"]
    if status in PRESENCE_STATUSES:
        new_in = from_ms(body.punch_in) if body.punch_in is not None else current["punch_in"]
        new_out = from_ms(body.punch_out) if body.punch_out is not None else current["punch_out"]
        if new_in is None:
            validation_error(["body", "punch_in"], f"status {status} requires a punch_in")
        if attendance_date(new_in, employee) != date:
            validation_error(["body", "punch_in"], "punch_in must stay on the record's attendance date")
        if new_out is not None:
            check_punch_out(new_in, new_out, ["body", "punch_out"])
    else:
        if body.punch_in is not None or body.punch_out is not None:
            validation_error(["body"], f"status {status} cannot have punch times")
        new_in = new_out = None

    target = {
        "status": status,
        "punch_in": new_in,
        "punch_out": new_out,
        **derived_fields(status, new_in, new_out, date, employee),
    }
    if all(target[field] == current[field] for field in ("status", "punch_in", "punch_out")):
        validation_error(["body"], "the request does not change anything")

    entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": {
            field: {"from": current[field], "to": target[field]}
            for field in TRACKED_FIELDS
            if target[field] != current[field]
        },
    }
    # Optimistic concurrency: the update only applies if the inputs we computed from and the history
    # length are still what we read. A concurrent correction makes this match nothing, so the loser
    # gets 409 instead of overwriting the winner's values or history entry.
    guard = {
        "_id": record["_id"],
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(record.get("history") or [])]},
    }
    updated = db.attendance_logs.find_one_and_update(
        guard, {"$set": target, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "the record was changed by another request; retry")
    return serialize_record(updated)


# --------------------------------------------------------------------------- #
# Aggregation building blocks
# --------------------------------------------------------------------------- #
LATE = {"$ifNull": ["$late_minutes", 0]}
IS_LATE = {"$cond": [{"$gt": [LATE, 0]}, 1, 0]}
IS_PRESENT = {"$in": ["$status", PRESENCE_STATUSES]}
# R5/R6: a present record is worth 1 day, or 0.5 when it is a half day.
PRESENT_VALUE = {"$cond": [IS_PRESENT, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}
# R7: present_days only counts Mon-Fri. $isoDayOfWeek: 1 = Monday ... 7 = Sunday.
IS_WEEKDAY = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]}
PRESENT_WEEKDAY_VALUE = {"$cond": [IS_WEEKDAY, PRESENT_VALUE, 0]}


def count_if(condition: dict) -> dict:
    return {"$cond": [condition, 1, 0]}


def round_half_up_expr(expr: Any, places: int) -> dict:
    """R8 in MongoDB. $round is banker's rounding, so use floor(x * 10^n + 0.5) / 10^n in Decimal128."""
    factor = 10**places
    scaled = {"$add": [{"$multiply": [{"$toDecimal": expr}, factor]}, Decimal128("0.5")]}
    return {"$toDouble": {"$divide": [{"$floor": scaled}, factor]}}  # null stays null


def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": PRESENT_WEEKDAY_VALUE},
                "leave_days": {"$sum": count_if({"$eq": ["$status", "LEAVE"]})},
                "late_count": {"$sum": IS_LATE},
                "total_late_minutes": {"$sum": LATE},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def department_summary_pipeline(month: str, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    # Start from employees, not logs: everyone who has joined by month end is in the headcount (R9),
    # including people with no logs at all.
    match: dict = {"joined_on": {"$lte": last}}
    if department is not None:
        match["department"] = department
    per_record = {
        "_id": 0,
        "present": PRESENT_WEEKDAY_VALUE,
        "late": LATE,
        "is_late": IS_LATE,
        "is_leave": count_if({"$eq": ["$status", "LEAVE"]}),
        "is_on_duty": count_if({"$eq": ["$status", "ON_DUTY"]}),
        # Only present records with a work_hours value take part in the average; others leave it missing.
        "work_hours": {
            "$cond": [
                {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]},
                {"$round": [{"$toDecimal": "$work_hours"}, 2]},
                "$$REMOVE",
            ]
        },
    }
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [{"$match": {"date": {"$gte": first, "$lte": last}}}, {"$project": per_record}],
                "as": "logs",
            }
        },
        # Keep employees with no logs as one row with no "logs" field, so they still reach the headcount.
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$emp_code"},
                "present_days": {"$sum": "$logs.present"},
                # $avg over individual records, skipping missing values: not an average of averages.
                "avg_work_hours": {"$avg": "$logs.work_hours"},
                "late_count": {"$sum": "$logs.is_late"},
                "total_late_minutes": {"$sum": "$logs.late"},
                "leave_count": {"$sum": "$logs.is_leave"},
                "on_duty_count": {"$sum": "$logs.is_on_duty"},
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$employees"},
                "present_days": {"$toDouble": "$present_days"},
                "avg_work_hours": round_half_up_expr("$avg_work_hours", 2),
                "late_count": "$late_count",
                "total_late_minutes": "$total_late_minutes",
                "leave_count": "$leave_count",
                "on_duty_count": "$on_duty_count",
            }
        },
    ]


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "pipeline": [{"$project": {"_id": 0, "name": 1, "department": 1}}],
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},  # drops logs whose employee does not exist
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})  # rank inside the department
    pipeline += [
        # $rank is standard competition ranking: ties share a rank and the next one is skipped (1, 2, 2, 4).
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        # limit applies after ranking, so everyone tied at a rank <= limit is kept.
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": "$rank",
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": "$total_late_minutes",
                "late_count": "$late_count",
            }
        },
    ]
    return pipeline


def department_trend_pipeline(department: str, emp_codes: list[str], first: str, last: str) -> list:
    densify_end = date.fromisoformat(last) + timedelta(days=1)  # $densify's upper bound is exclusive
    bounds = [
        datetime.combine(date.fromisoformat(first), time(), timezone.utc),
        datetime.combine(densify_end, time(), timezone.utc),
    ]
    return [
        {"$match": {"emp_code": {"$in": emp_codes}, "date": {"$gte": first, "$lte": last}}},
        {"$group": {"_id": "$date", "present_count": {"$sum": PRESENT_VALUE}, "late_count": {"$sum": IS_LATE}}},
        # Calendar dates become UTC-midnight datetimes only so that $densify can step through them.
        {
            "$project": {
                "_id": 0,
                "day": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": bounds}}},
        {
            "$set": {
                "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        # R9 per day: department employees who had joined by that day.
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date"},
                "pipeline": [
                    {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}},
                    {"$count": "n"},
                ],
                "as": "joined",
            }
        },
        {"$set": {"headcount": {"$ifNull": [{"$first": "$joined.n"}, 0]}}},
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        round_half_up_expr({"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}, 4),
                        None,
                    ]
                }
            }
        },
        # Rows outside [first, last] never exist, so the first rows naturally get a shorter window.
        # $avg skips nulls and returns null when the window has no rate.
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$date",
                "is_working_day": "$is_working_day",
                "headcount": "$headcount",
                "present_count": {"$toDouble": "$present_count"},
                "late_count": "$late_count",
                "attendance_rate": "$attendance_rate",
                "moving_avg_7d": round_half_up_expr("$moving_avg_7d", 4),
            }
        },
    ]


def check_trend_range(first: str, last: str) -> None:
    if last < first:
        validation_error(["query", "to"], "to must not be before from")
    if (date.fromisoformat(last) - date.fromisoformat(first)).days + 1 > MAX_TREND_DAYS:
        validation_error(["query", "to"], f"the range must not exceed {MAX_TREND_DAYS} days")


def department_emp_codes(department: str) -> list[str]:
    return db.employees.distinct("emp_code", {"department": department})


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthStr):
    employee = get_employee_or_404(emp_code)
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month)))
    totals = rows[0] if rows else {}

    first, last = month_bounds(month)
    working_days = count_working_days(max(first, employee["joined_on"]), last)
    present_days = totals.get("present_days", 0)
    attendance_pct = None
    if working_days:
        attendance_pct = float(round_half_up(Decimal(str(present_days)) * 100 / working_days, 2))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(present_days),
        "leave_days": totals.get("leave_days", 0),
        "late_count": totals.get("late_count", 0),
        "total_late_minutes": totals.get("total_late_minutes", 0),
        "total_overtime_minutes": totals.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: MonthStr, department: Optional[str] = None):
    items = list(db.employees.aggregate(department_summary_pipeline(month, department)))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: MonthStr, limit: Annotated[int, Query(ge=1, le=50)] = 10, department: Optional[str] = None
):
    items = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: Annotated[DateStr, Query(alias="from")],
    to: DateStr,
):
    check_trend_range(from_, to)
    emp_codes = department_emp_codes(department)
    if not emp_codes:
        raise HTTPException(404, "department not found")
    items = list(db.attendance_logs.aggregate(department_trend_pipeline(department, emp_codes, from_, to)))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Explain
# --------------------------------------------------------------------------- #
ExplainTarget = Literal[
    "attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"
]


def require(params: dict) -> None:
    for name, value in params.items():
        if value is None:
            validation_error(["query", name], "field required for this endpoint")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Annotated[Optional[str], Query(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[DateStr], Query(alias="from")] = None,
    to: Optional[DateStr] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    """Explains the same query/pipeline the real endpoint runs, built by the same functions."""
    if endpoint == "attendance_list":
        check_date_range(date_from, date_to, ["query", "date_from"])
        collection = "attendance_logs"
        command = {
            "find": collection,
            "filter": attendance_list_query(emp_code, date_from, date_to, status),
            "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            require({"emp_code": emp_code, "month": month})
            collection, pipeline = "attendance_logs", employee_monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            require({"month": month})
            collection, pipeline = "employees", department_summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            require({"month": month})
            collection, pipeline = "attendance_logs", late_leaderboard_pipeline(month, limit, department)
        else:
            require({"department": department, "from": from_, "to": to})
            check_trend_range(from_, to)
            emp_codes = department_emp_codes(department)
            collection = "attendance_logs"
            pipeline = department_trend_pipeline(department, emp_codes, from_, to)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}

    result = db.command("explain", command, verbosity="executionStats")
    # Relaxed Extended JSON turns BSON-only types (Timestamp, ObjectId, ...) into plain JSON.
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(result))}
