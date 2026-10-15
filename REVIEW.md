# REVIEW.md

Defects in the starter `app/main.py` (line numbers refer to the starter as supplied). Rows 1-7 were reproduced by
calling the starter's own functions with `TZ=UTC` (except row 3's example, computed with Python's `round`); the rest
are read from the code, and each fix is covered by a test in `tests/`.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes`, L36-37 | Grace check is done on truncated whole minutes (`minutes > 10`), so 10:01 after start is treated as inside the grace period. | Shift 09:30, punch 09:40:01 IST returns 0; R2 says 10. | Compare the `timedelta` with `> 10 min` first, then floor to minutes (`elapsed // timedelta(minutes=1)`). |
| 2 | `compute_late_minutes`, L35 | Shift start is built with `punch_in.replace(hour=..)` on whatever the punch's date/zone is: wrong for naive UTC/local datetimes and for overnight shifts (01:00 for a 22:00 shift is "before" start). | Overnight shift 22:00-06:00, punch 01:00 IST returns 0; expected 180. | `shift_window(date, employee)` builds the start in IST on the record's attendance date. |
| 3 | `compute_work_hours`, L41 | Float `round()` is binary/banker's rounding, not half-up (R4); sub-second parts are not truncated. | 4h00m18s = 4.005 h -> `round` gives 4.0; R4 needs 4.01. | Integer seconds -> `Decimal` / 3600 -> `quantize(ROUND_HALF_UP)`. |
| 4 | `compute_overtime`, L47 | No 30-minute minimum (R3). | Sample 1 (out 18:35, shift end 18:30) gives 5; expected 0. | Return minutes only when `>= 30`. |
| 5 | `compute_overtime`, L46 | Shift end is put on the attendance date even for overnight shifts, where it is the next day. | Sample 7 (22:00-06:00, out 06:40 next day) gives 1480; expected 40. | `shift_window` adds a day to the end when `shift_end <= shift_start`. |
| 6 | `punch_in`, L102-103 | `datetime.fromtimestamp` gives a naive datetime in the *server's* zone, and `date` is taken from it, not from IST; the overnight rule (R1) is missing; milliseconds are not truncated. | On a UTC server, 00:30 IST on 7 July gets date 2026-07-06. 09:40:00.900 is stored with .900. | `from_ms` truncates to seconds and returns aware UTC; `attendance_date` converts to IST and applies the overnight rule. |
| 7 | `PunchInIn`, L60-63 | `punched_at` is a lax `int` (accepts `1783312500000.0`, `"1783312500000"`, seconds, negatives) and `status` is any string (accepts `ABSENT`, typos). | `{"punched_at": "1783312500000"}` is accepted instead of 422. | `EpochMs` = strict int, 1e11..4102444800000; `status: Literal["PRESENT","WFH","ON_DUTY"]`. |
| 8 | `punch_in`, L101, L113 | No 404 for an unknown employee: `emp` is `None`, and `emp["shift_start"]` raises -> 500. | Punch-in for `EMP9999` returns 500. | `get_employee_or_404`. |
| 9 | `punch_in`, L104-118 | Check-then-insert with no unique index: two concurrent punch-ins both pass `find_one` and both insert. | Two parallel identical punch-ins can both get 201 and leave two records for one day (no constraint stops the second insert). | Unique index on `(emp_code, date)`; insert and map `DuplicateKeyError` to 409. |
| 10 | `create_employee`, L79-83 | Same check-then-insert race on `emp_code`; there is no unique index anywhere. | Parallel creates of one `emp_code` can all succeed, leaving duplicate employees. | Unique index on `emp_code`; `DuplicateKeyError` -> 409. |
| 11 | `create_employee` / `EmployeeIn`, L50-58 | No input validation: shift times are free text, `shift_start == shift_end` is allowed, `emp_code`/email/`joined_on` formats unchecked. | `shift_start: "25:99"` or equal start/end is stored; later arithmetic raises 500. | Pydantic patterns and lengths from the contract, real date check, model validator for equal shifts. |
| 12 | `create_employee`, L82, L85 | `created_at = datetime.now()` is naive local time (stored in MongoDB as if it were UTC, so off by the server offset) and is returned as an ISO string, not epoch ms. | Response has `"created_at": "2026-07-06T09:30:00.123"`. | Aware UTC `now`, serialized with `to_ms`. |
| 13 | `list_employees`, L93 | `skip = page * page_size` skips the first page entirely. | `page=1` returns employees 21-40. | `skip = (page - 1) * page_size`. |
| 14 | `list_employees`, L94 | `total` counts the whole collection, ignoring the department filter (R10). | `?department=Sales` reports total of all employees. | `count_documents(query)`. |
| 15 | `list_employees`, L95 | No sort, so order (and therefore pagination) is not by `emp_code` and not stable. | Items come back in natural (insertion) order; the contract says sorted by `emp_code`. | `.sort("emp_code")`, backed by the `emp_code` / `(department, emp_code)` indexes. |
| 16 | `list_employees`, `list_attendance` signatures | `page`/`page_size` are unbounded: `page=0` is silently accepted, a negative page gives a negative skip, `page_size=100000` is allowed. | `/employees?page=-1` -> PyMongo `ValueError` -> 500. | `Query(ge=1)` and `Query(ge=1, le=100)` -> 422. |
| 17 | `list_attendance`, L144-147 | Loads every matching document into Python, then sorts and slices in memory. | With 100k records each call reads the whole collection. | `find().sort().skip().limit()` on indexes, `count_documents` for `total`. |
| 18 | `list_attendance`, L145 | Sort is only by `date` desc; contract requires `date` desc then `emp_code` asc. | Rows for the same date come back in insertion order. | Sort `[("date", -1), ("emp_code", 1)]`, matched by index `(date -1, emp_code 1)`. |
| 19 | `list_attendance`, L127-142 | No validation: malformed dates are compared as strings, `date_from > date_to` is not rejected, `status` is not checked against the enum. | `?date_from=2026-13-45` returns 200. | `DateStr` validator, range check -> 422, `Status` literal. |
| 20 | `punch_in` L119-121, `list_attendance` L148-150 | Responses expose MongoDB `_id` as `id`, return datetimes as ISO strings instead of epoch ms, and pass stored documents through unchanged (legacy records lack `half_day`/`history`). | Response contains `"id": "66..."` and `"punch_in": "2026-07-06T09:40:00"`. | One `serialize_record` that emits exactly the contract fields with defaults for missing ones. |
| 21 | Module level, L22 | `MongoClient` without `tz_aware=True` returns naive datetimes, so any arithmetic against aware IST values raises or silently uses the wrong zone. | Comparing a read-back `punch_in` with an IST datetime raises `TypeError`. | `MongoClient(..., tz_aware=True)`; all datetimes are aware UTC internally. |
| 22 | Module level | No indexes are created at startup; the grader's database has only `_id`. | Explain on any query shows `COLLSCAN`. | `ensure_indexes()` in the lifespan hook (idempotent). |
| 23 | `health`, L68-70 | Always returns 200 without contacting MongoDB. | Stop MongoDB: `/health` still says ok. | `ping` the server; `PyMongoError` -> 503. |

Not defects (looked at and kept):

- `load_dotenv()` at import: it does not override variables already in the environment, so real env vars win as required.
- `IST = timezone(timedelta(hours=5, minutes=30))`: correct; India has no DST, so a fixed offset is right.
- The starter's helper split (late / work hours / overtime) is the right shape and is kept, with fixed logic and with
  datetimes as inputs instead of strings.
- `history: []` and `punch_out: None` on punch-in are correct; punch-in must not write history.
- The missing endpoints (TODO block) are unfinished work rather than defects; they are implemented.
