"""Explain plans and timings on a ~100,000-record dataset (its own database and server)."""
import json
import random
import re
import time
import uuid
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from conftest import Server

DEPARTMENTS = ["Engineering", "Sales", "Support", "Finance", "Operations"]
EMPLOYEES = 400
DAYS = 250  # 400 x 250 = 100,000 records


@pytest.fixture(scope="module")
def big(mongo):
    db_name = f"attendance_perf_{uuid.uuid4().hex[:8]}"
    db = mongo[db_name]
    rng = random.Random(7)
    db.employees.insert_many([
        {"emp_code": f"EMP{i:04d}", "name": f"Person {i}", "email": f"p{i}@example.com",
         "department": DEPARTMENTS[i % 5], "shift_start": "09:30", "shift_end": "18:30",
         "joined_on": (date(2026, 1, 1) + timedelta(days=rng.randint(0, 200))).isoformat(),
         "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}
        for i in range(EMPLOYEES)
    ])
    statuses = ["PRESENT"] * 14 + ["WFH", "ON_DUTY", "ABSENT", "LEAVE"]
    batch = []
    for i in range(EMPLOYEES):
        for d in range(DAYS):
            day = date(2026, 1, 1) + timedelta(days=d)
            status = rng.choice(statuses)
            present = status not in ("ABSENT", "LEAVE")
            start = datetime(day.year, day.month, day.day, 4, 0, tzinfo=timezone.utc)
            wh = round(rng.uniform(3, 10), 2) if present else None
            batch.append({
                "emp_code": f"EMP{i:04d}", "date": day.isoformat(), "status": status,
                "punch_in": start if present else None,
                "punch_out": start + timedelta(hours=wh) if present else None,
                "work_hours": wh, "late_minutes": rng.choice([0] * 6 + [12, 25, 40]) if present else 0,
                "overtime_minutes": 0, "half_day": bool(wh and wh < 4.5), "history": [],
            })
    db.attendance_logs.insert_many(batch)
    server = Server(db_name)  # the app creates its indexes on startup against the pre-seeded data
    yield server
    server.stop()
    mongo.drop_database(db_name)


CASES = [
    ("attendance_list", {}),
    ("attendance_list", {"status": "LEAVE", "page": 3}),
    ("attendance_list", {"emp_code": "EMP0042", "date_from": "2026-03-01", "date_to": "2026-03-31"}),
    ("attendance_list", {"date_from": "2026-05-01"}),
    ("employee_monthly", {"emp_code": "EMP0042", "month": "2026-03"}),
    ("department_summary", {"month": "2026-03"}),
    ("department_summary", {"month": "2026-03", "department": "Sales"}),
    ("late_leaderboard", {"month": "2026-03"}),
    ("late_leaderboard", {"month": "2026-03", "department": "Sales", "limit": 3}),
    ("department_trend", {"department": "Sales", "from": "2026-03-01", "to": "2026-05-31"}),
]


@pytest.mark.parametrize("endpoint, params", CASES)
def test_explain_uses_indexes(big, endpoint, params):
    response = httpx.get(f"{big.url}/admin/explain/{endpoint}", params=params, timeout=60)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["endpoint"] == endpoint and body["collection"] in ("attendance_logs", "employees")
    plan = json.dumps(body["explain"])
    assert "IXSCAN" in plan and "COLLSCAN" not in plan
    # $lookup stages report how their inner queries ran; none of them may scan a whole collection.
    assert all(n == 0 for n in map(int, re.findall(r'"collectionScans": (\d+)', plan)))
    print(endpoint, params, "indexes:", sorted(set(re.findall(r'"indexName": "([^"]+)"', plan))
                                               | {i for m in re.findall(r'"indexesUsed": \[([^\]]*)\]', plan)
                                                  for i in re.findall(r'"([^"]+)"', m)}))


def test_explain_validation(big):
    assert httpx.get(f"{big.url}/admin/explain/unknown").status_code == 422
    assert httpx.get(f"{big.url}/admin/explain/employee_monthly", params={"month": "2026-03"}).status_code == 422
    assert httpx.get(f"{big.url}/admin/explain/department_trend", params={"department": "Sales"}).status_code == 422


ENDPOINTS = [
    ("/attendance", {"page": 50}),
    ("/attendance", {"status": "LEAVE"}),
    ("/attendance", {"emp_code": "EMP0042", "date_from": "2026-03-01"}),
    ("/analytics/employees/EMP0042/monthly", {"month": "2026-03"}),
    ("/analytics/departments/summary", {"month": "2026-03"}),
    ("/analytics/leaderboard/late", {"month": "2026-03"}),
    ("/analytics/departments/Sales/trend", {"from": "2026-03-01", "to": "2026-05-31"}),
]


@pytest.mark.parametrize("path, params", ENDPOINTS)
def test_endpoint_latency(big, path, params):
    started = time.perf_counter()
    response = httpx.get(f"{big.url}{path}", params=params, timeout=60)
    elapsed = time.perf_counter() - started
    print(f"{path} {params}: {elapsed * 1000:.0f} ms")
    assert response.status_code == 200
    assert elapsed < 2.0
