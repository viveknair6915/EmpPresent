from concurrent.futures import ThreadPoolExecutor
from decimal import ROUND_HALF_UP, Decimal

import httpx
import pytest

from conftest import ist_dt, ist_ms


def make_employee(api, emp_code="EMP0001", **overrides):
    body = {
        "emp_code": emp_code,
        "name": "Test Person",
        "email": f"{emp_code.lower()}@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01",
        **overrides,
    }
    response = api.post("/employees", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def punch_in(api, ms, emp_code="EMP0001", **extra):
    return api.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": ms, **extra})


def punch_out(api, ms, emp_code="EMP0001"):
    return api.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": ms})


def parallel(n, fn):
    with ThreadPoolExecutor(max_workers=n) as pool:
        return [f.result() for f in [pool.submit(fn) for _ in range(n)]]


# --------------------------------------------------------------------------- employees
def test_health(api):
    assert api.get("/health").json() == {"status": "ok"}


def test_indexes_created_at_startup(api, db):
    assert {tuple(i["key"].items()): i.get("unique", False) for i in db.employees.list_indexes()} == {
        (("_id", 1),): False,
        (("emp_code", 1),): True,
        (("department", 1), ("emp_code", 1)): False,
        (("joined_on", 1),): False,
    }
    assert {tuple(i["key"].items()): i.get("unique", False) for i in db.attendance_logs.list_indexes()} == {
        (("_id", 1),): False,
        (("emp_code", 1), ("date", 1)): True,
        (("date", -1), ("emp_code", 1)): False,
    }


def test_create_employee(api, db):
    created = make_employee(api, shift_start="22:00", shift_end="06:00")
    assert set(created) == {"emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on", "created_at"}
    assert isinstance(created["created_at"], int) and created["created_at"] % 1000 == 0
    stored = db.employees.find_one({"emp_code": "EMP0001"})
    assert stored["created_at"].tzinfo is not None  # stored as a BSON date


def test_create_employee_defaults_shift(api):
    created = make_employee(api)
    assert (created["shift_start"], created["shift_end"]) == ("09:30", "18:30")


def test_duplicate_employee_is_409(api):
    make_employee(api)
    body = {"emp_code": "EMP0001", "name": "X", "email": "x@example.com", "department": "D", "joined_on": "2026-01-01"}
    assert api.post("/employees", json=body).status_code == 409


@pytest.mark.parametrize("override", [
    {"shift_start": "24:00"},
    {"shift_end": "9:30"},
    {"shift_start": "10:00", "shift_end": "10:00"},
    {"emp_code": "E1"},
    {"joined_on": "2026-02-30"},
    {"joined_on": "20260101"},
    {"email": "not-an-email"},
    {"name": ""},
])
def test_invalid_employee_is_422(api, override):
    body = {"emp_code": "EMP0001", "name": "X", "email": "x@example.com", "department": "D", "joined_on": "2026-01-01"}
    assert api.post("/employees", json={**body, **override}).status_code == 422


def test_concurrent_duplicate_create(server, db):
    body = {"emp_code": "EMP0042", "name": "X", "email": "x@example.com", "department": "D", "joined_on": "2026-01-01"}
    codes = parallel(12, lambda: httpx.post(f"{server.url}/employees", json=body).status_code)
    assert sorted(codes) == [201] + [409] * 11
    assert db.employees.count_documents({"emp_code": "EMP0042"}) == 1


def test_list_employees_filter_and_pagination(api):
    for i in range(25):
        make_employee(api, f"EMP{1000 + i}", department="Sales" if i % 5 == 0 else "Engineering")
    page = api.get("/employees", params={"department": "Engineering", "page": 2, "page_size": 15}).json()
    assert page["total"] == 20 and page["page"] == 2 and page["page_size"] == 15
    assert [e["emp_code"] for e in page["items"]] == [f"EMP{1000 + i}" for i in range(25) if i % 5][15:]
    first = api.get("/employees").json()
    assert first["total"] == 25 and len(first["items"]) == 20 and first["items"][0]["emp_code"] == "EMP1000"
    assert api.get("/employees", params={"department": "engineering"}).json()["total"] == 0  # case-sensitive


@pytest.mark.parametrize("params", [{"page": 0}, {"page_size": 0}, {"page_size": 101}, {"page": "x"}])
def test_bad_pagination_is_422(api, params):
    assert api.get("/employees", params=params).status_code == 422
    assert api.get("/attendance", params=params).status_code == 422


# --------------------------------------------------------------------------- punch-in (R1, R2)
@pytest.mark.parametrize("wall, expected_late", [
    ((9, 28, 0), 0),
    ((9, 40, 0), 0),     # exactly 10:00 after start is still inside the grace period
    ((9, 40, 1), 10),    # strictly more than 10:00 -> minutes since shift start
    ((10, 15, 59), 45),
])
def test_late_minutes(api, wall, expected_late):
    make_employee(api)
    record = punch_in(api, ist_ms(2026, 7, 6, *wall)).json()
    assert record["late_minutes"] == expected_late
    assert record["date"] == "2026-07-06"


def test_punch_in_truncates_to_seconds(api, db):
    make_employee(api)
    record = punch_in(api, ist_ms(2026, 7, 6, 9, 40, 0, ms=900)).json()
    assert record["punch_in"] == ist_ms(2026, 7, 6, 9, 40, 0)
    assert record["late_minutes"] == 0  # 09:40:00.900 counts as 09:40:00
    assert db.attendance_logs.find_one()["punch_in"] == ist_dt(2026, 7, 6, 9, 40, 0)


def test_punch_in_response_shape(api):
    make_employee(api)
    response = punch_in(api, ist_ms(2026, 7, 6, 9, 0), status="WFH")
    assert response.status_code == 201
    assert response.json() == {
        "emp_code": "EMP0001", "date": "2026-07-06", "status": "WFH",
        "punch_in": ist_ms(2026, 7, 6, 9, 0), "punch_out": None, "work_hours": None,
        "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": [],
    }


def test_date_is_ist_not_utc(api):
    make_employee(api)
    # 00:30 IST on the 7th is 19:00 UTC on the 6th.
    assert punch_in(api, ist_ms(2026, 7, 7, 0, 30)).json()["date"] == "2026-07-07"
    # 05:00 IST on the 8th is 23:30 UTC on the 7th.
    make_employee(api, "EMP0002")
    assert punch_in(api, ist_ms(2026, 7, 8, 5, 0), "EMP0002").json()["date"] == "2026-07-08"


def test_overnight_shift(api):
    make_employee(api, shift_start="22:00", shift_end="06:00")
    # 21:55 on the 6th: shift of the 6th, on time.
    rec = punch_in(api, ist_ms(2026, 7, 6, 21, 55)).json()
    assert (rec["date"], rec["late_minutes"]) == ("2026-07-06", 0)
    out = punch_out(api, ist_ms(2026, 7, 7, 6, 40)).json()
    assert (out["date"], out["overtime_minutes"], out["work_hours"], out["half_day"]) == ("2026-07-06", 40, 8.75, False)
    # 01:00 on the 8th is before shift_end, so it belongs to the shift that started on the 7th; 3 hours late.
    rec = punch_in(api, ist_ms(2026, 7, 8, 1, 0)).json()
    assert (rec["date"], rec["late_minutes"]) == ("2026-07-07", 180)
    # 06:00 exactly is not earlier than shift_end, so it starts the 8th's record.
    out = punch_out(api, ist_ms(2026, 7, 8, 5, 0)).json()
    assert out["date"] == "2026-07-07"
    assert punch_in(api, ist_ms(2026, 7, 8, 6, 0)).json()["date"] == "2026-07-08"


@pytest.mark.parametrize("value", [1783312500000.0, 1783312500000.5, "1783312500000", 1783312500, True, 4102444800001, -1])
def test_invalid_timestamps_are_422(api, value):
    make_employee(api)
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": value}).status_code == 422
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": value}).status_code == 422


@pytest.mark.parametrize("status", ["ABSENT", "LEAVE", "present", "HOLIDAY"])
def test_punch_in_rejects_non_presence_status(api, status):
    make_employee(api)
    assert punch_in(api, ist_ms(2026, 7, 6, 9, 0), status=status).status_code == 422


def test_punch_in_unknown_employee_404(api):
    assert punch_in(api, ist_ms(2026, 7, 6, 9, 0), "EMP9999").status_code == 404


def test_punch_in_twice_same_day_409(api):
    make_employee(api)
    assert punch_in(api, ist_ms(2026, 7, 6, 9, 0)).status_code == 201
    assert punch_in(api, ist_ms(2026, 7, 6, 14, 0)).status_code == 409


def test_concurrent_punch_ins(server, api, db):
    make_employee(api)
    body = {"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 6, 9, 0)}
    codes = parallel(12, lambda: httpx.post(f"{server.url}/attendance/punch-in", json=body).status_code)
    assert sorted(codes) == [201] + [409] * 11
    assert db.attendance_logs.count_documents({}) == 1


# --------------------------------------------------------------------------- punch-out (R3 - R5)
@pytest.mark.parametrize("out, expected_overtime", [((18, 59, 59), 0), ((19, 0, 0), 30), ((19, 10, 30), 40), ((17, 0, 0), 0)])
def test_overtime_threshold(api, out, expected_overtime):
    make_employee(api)
    punch_in(api, ist_ms(2026, 7, 6, 9, 30))
    assert punch_out(api, ist_ms(2026, 7, 6, *out)).json()["overtime_minutes"] == expected_overtime


def test_work_hours_half_up_and_half_day_boundary(api):
    make_employee(api)
    # 4h29m42s = 4.495 h: half-up gives 4.50 (float rounding gives 4.49), and 4.50 is not a half day.
    punch_in(api, ist_ms(2026, 7, 6, 9, 0))
    rec = punch_out(api, ist_ms(2026, 7, 6, 13, 29, 42)).json()
    assert (rec["work_hours"], rec["half_day"]) == (4.5, False)
    # 4h29m41s = 4.49472 h -> 4.49, a half day.
    punch_in(api, ist_ms(2026, 7, 7, 9, 0))
    rec = punch_out(api, ist_ms(2026, 7, 7, 13, 29, 41)).json()
    assert (rec["work_hours"], rec["half_day"]) == (4.49, True)
    assert rec["history"] == []
    # 4h00m18s = 4.005 h: Python's round(4.005, 2) gives 4.0; half-up gives 4.01.
    punch_in(api, ist_ms(2026, 7, 8, 9, 0))
    assert punch_out(api, ist_ms(2026, 7, 8, 13, 0, 18)).json()["work_hours"] == 4.01


def test_punch_out_errors(api):
    make_employee(api)
    assert punch_out(api, ist_ms(2026, 7, 6, 18, 0)).status_code == 404  # no punch-in yet
    assert punch_out(api, ist_ms(2026, 7, 6, 18, 0), "EMP9999").status_code == 404
    punch_in(api, ist_ms(2026, 7, 6, 9, 0))
    assert punch_out(api, ist_ms(2026, 7, 6, 9, 0)).status_code == 422  # equal to punch_in
    assert punch_out(api, ist_ms(2026, 7, 6, 8, 0)).status_code == 422  # before punch_in
    assert punch_out(api, ist_ms(2026, 7, 7, 9, 0, 1)).status_code == 422  # more than 24 h
    assert punch_out(api, ist_ms(2026, 7, 7, 9, 0)).status_code == 200  # exactly 24 h
    assert punch_out(api, ist_ms(2026, 7, 7, 10, 0)).status_code == 409  # already closed


def test_punch_out_picks_latest_open_record(api):
    make_employee(api)
    punch_in(api, ist_ms(2026, 7, 6, 9, 0))
    punch_out(api, ist_ms(2026, 7, 6, 18, 0))
    punch_in(api, ist_ms(2026, 7, 7, 9, 0))
    rec = punch_out(api, ist_ms(2026, 7, 7, 18, 0)).json()
    assert rec["date"] == "2026-07-07" and rec["work_hours"] == 9.0
    # A punch-out time before today's open punch-in is a bad timestamp, not "yesterday is closed".
    punch_in(api, ist_ms(2026, 7, 8, 9, 0))
    assert punch_out(api, ist_ms(2026, 7, 8, 8, 0)).status_code == 422


def test_concurrent_punch_outs(server, api, db):
    make_employee(api)
    punch_in(api, ist_ms(2026, 7, 6, 9, 0))
    outs = [ist_ms(2026, 7, 6, 18, i) for i in range(12)]
    with ThreadPoolExecutor(max_workers=12) as pool:
        codes = list(pool.map(lambda ms: httpx.post(
            f"{server.url}/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms}).status_code, outs))
    assert sorted(codes) == [200] + [409] * 11
    stored = db.attendance_logs.find_one()
    # Whichever request won, the derived values belong to its punch_out.
    seconds = (stored["punch_out"] - ist_dt(2026, 7, 6, 9, 0)).seconds
    expected = (Decimal(seconds) / 3600).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    assert stored["work_hours"] == float(expected)


# --------------------------------------------------------------------------- list attendance
def test_list_attendance(api, db):
    make_employee(api)
    make_employee(api, "EMP0002")
    for day in (6, 7, 8):
        punch_in(api, ist_ms(2026, 7, day, 9, 0))
        punch_in(api, ist_ms(2026, 7, day, 9, 0), "EMP0002", status="WFH")
    db.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-09", "status": "ABSENT",
                                   "punch_in": None, "punch_out": None})  # legacy shape: missing fields
    page = api.get("/attendance", params={"page_size": 3}).json()
    assert page["total"] == 7
    assert [(r["date"], r["emp_code"]) for r in page["items"]] == [
        ("2026-07-09", "EMP0001"), ("2026-07-08", "EMP0001"), ("2026-07-08", "EMP0002")]
    absent = page["items"][0]
    assert (absent["late_minutes"], absent["half_day"], absent["history"], absent["work_hours"]) == (0, False, [], None)
    page2 = api.get("/attendance", params={"page": 2, "page_size": 3}).json()
    assert [(r["date"], r["emp_code"]) for r in page2["items"]] == [
        ("2026-07-07", "EMP0001"), ("2026-07-07", "EMP0002"), ("2026-07-06", "EMP0001")]
    filtered = api.get("/attendance", params={"emp_code": "EMP0002", "date_from": "2026-07-07",
                                              "date_to": "2026-07-08", "status": "WFH"}).json()
    assert filtered["total"] == 2 and {r["date"] for r in filtered["items"]} == {"2026-07-07", "2026-07-08"}
    assert api.get("/attendance", params={"status": "ABSENT"}).json()["total"] == 1


@pytest.mark.parametrize("params", [
    {"date_from": "2026-07-09", "date_to": "2026-07-08"},
    {"date_from": "2026-13-01"},
    {"date_to": "07/08/2026"},
    {"status": "HOLIDAY"},
])
def test_list_attendance_validation(api, params):
    assert api.get("/attendance", params=params).status_code == 422
