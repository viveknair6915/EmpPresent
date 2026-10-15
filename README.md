# Employee Attendance & Analytics API

FastAPI + MongoDB service for punch-in/punch-out with business rules R1-R10, manual corrections with an audit trail,
and analytics built on MongoDB aggregation pipelines. All application code is in `app/main.py`; the contract is
`openapi.yaml` and the stored data is described in `DATA_MODEL.md`.

| Area | Endpoints |
|---|---|
| System | `GET /health` (503 when MongoDB does not answer) |
| Employees | `POST /employees`, `GET /employees` |
| Attendance | `POST /attendance/punch-in`, `POST /attendance/punch-out`, `GET /attendance`, `PATCH /attendance/{emp_code}/{date}` |
| Analytics | `GET /analytics/employees/{emp_code}/monthly`, `GET /analytics/departments/summary`, `GET /analytics/leaderboard/late`, `GET /analytics/departments/{department}/trend` |
| Admin | `GET /admin/explain/{endpoint}` (`executionStats` explain of the endpoint's real query) |

## Requirements

Python 3.11+, MongoDB 6.0+ (developed and tested against 7.0; the trend endpoint uses `$densify`, the leaderboard
`$setWindowFields`).

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # or export MONGO_URI / MONGO_DB; real environment variables take priority
python sample_seed.py         # optional: loads the nine sample documents (replaces both collections)
uvicorn app.main:app --port 8000
```

| Variable | Default | |
|---|---|---|
| `MONGO_URI` | `mongodb://localhost:27017` | Any MongoDB URI, including `mongodb+srv://` for Atlas |
| `MONGO_DB` | `attendance_db` | Database name |

Interactive docs: http://localhost:8000/docs

Indexes are created idempotently at startup (and retried from `/health` if MongoDB was down at startup), so no
manual setup is needed against a pre-seeded database. See `DECISIONS.md` for which indexes and why.

## Tests

The tests start the real app with uvicorn against throwaway databases (`attendance_test_*`, `attendance_perf_*`)
on a running MongoDB and drop them afterwards. They never touch `MONGO_DB`.

```bash
pip install -r requirements-dev.txt
pytest                        # TEST_MONGO_URI overrides mongodb://localhost:27017
```

- `tests/test_employees_attendance.py`: validation, pagination, R1-R5 boundaries (grace period, overtime threshold,
  half-up work hours, IST vs UTC dates, overnight shifts), concurrent creates, punch-ins and punch-outs.
- `tests/test_regularize.py`: corrections, recomputed derived fields, history contents, concurrent corrections.
- `tests/test_analytics.py`: a hand-checked dataset with ties, a mid-month joiner, a future joiner, an employee with no
  logs, weekend records, legacy records and orphan logs.
- `tests/test_performance.py`: seeds 100,000 records and checks that every explain target uses `IXSCAN` with no
  `COLLSCAN`, and that endpoints answer quickly.

## Notes and interpretations

- Concurrent regularizations use optimistic concurrency: the loser gets 409 rather than a silent overwrite (the
  contract allows this). The client can retry.
- A punch-out timestamp earlier than the employee's open punch-in returns 422, not 404.
- `moving_avg_7d` averages the already rounded daily `attendance_rate` values shown in the response.
- Trend ranges may cover at most 92 days inclusive (a full quarter is accepted).
- Explicit `null` for an optional timestamp or status is treated the same as omitting it.
