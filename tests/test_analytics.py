"""Analytics against a small hand-built dataset; every expected number is worked out in the comments.

July 2026 starts on a Wednesday and has 23 working days.
"""
from datetime import datetime, timezone

import pytest


def employee(code, department, joined_on, name=None):
    return {"emp_code": code, "name": name or code, "email": f"{code.lower()}@example.com", "department": department,
            "shift_start": "09:30", "shift_end": "18:30", "joined_on": joined_on,
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}


def log(code, day, status="PRESENT", wh=None, late=0, ot=0, half=False, legacy=False):
    doc = {"emp_code": code, "date": day, "status": status, "work_hours": wh, "late_minutes": late,
           "overtime_minutes": ot, "half_day": half, "history": [],
           "punch_in": None if status in ("ABSENT", "LEAVE") else datetime(2026, 7, 1, tzinfo=timezone.utc),
           "punch_out": None}
    if legacy:  # older records may omit these entirely
        del doc["half_day"], doc["history"]
    return doc


@pytest.fixture
def dataset(db):
    db.employees.insert_many([
        employee("EMP1001", "Engineering", "2026-01-01"),
        employee("EMP1002", "Engineering", "2026-07-10"),  # mid-month joiner: 16 working days in July
        employee("EMP1003", "Engineering", "2026-01-01"),  # no logs at all
        employee("EMP1004", "Engineering", "2026-08-03"),  # joins after July
        employee("EMP2001", "Sales", "2026-01-01"),
        employee("EMP2002", "Sales", "2026-01-01"),
        employee("EMP2003", "Sales", "2026-01-01"),
        employee("EMP3001", "Ops", "2026-08-01"),          # department with July headcount 0
    ])
    db.attendance_logs.insert_many([
        log("EMP1001", "2026-06-30", wh=9.0, late=100),                 # previous month
        log("EMP1001", "2026-07-01", wh=9.0),
        log("EMP1001", "2026-07-02", wh=4.0, late=20, half=True),       # half day = 0.5
        log("EMP1001", "2026-07-03", "WFH", wh=8.5, late=15, ot=45),
        log("EMP1001", "2026-07-04", wh=5.0, late=30, ot=60),           # Saturday
        log("EMP1001", "2026-07-06", "LEAVE"),
        log("EMP1001", "2026-07-07", "ABSENT"),
        log("EMP1001", "2026-07-08", "ON_DUTY", wh=8.0, legacy=True),
        log("EMP1001", "2026-07-09", late=12),                          # open record, no work_hours
        log("EMP1002", "2026-07-15", "WFH", wh=3.0, late=77, half=True),
        log("EMP1002", "2026-07-18", wh=9.0),                           # Saturday
        log("EMP2001", "2026-07-01", wh=8.0, late=40),
        log("EMP2001", "2026-07-02", wh=8.0),
        log("EMP2002", "2026-07-01", wh=9.0, late=40),
        log("EMP2003", "2026-07-03", wh=8.02, late=25),
        log("EMP9999", "2026-07-01", wh=8.0, late=500),                 # employee does not exist
    ])


# --------------------------------------------------------------------------- employee monthly
def test_monthly_full_month(api, dataset):
    assert api.get("/analytics/employees/EMP1001/monthly", params={"month": "2026-07"}).json() == {
        "emp_code": "EMP1001", "month": "2026-07",
        "working_days": 23,
        "present_days": 4.5,          # 1 + 0.5 + 1 + 1 (on duty) + 1 (open); Saturday excluded
        "leave_days": 1,
        "late_count": 4,              # 20, 15, 30 (Saturday), 12
        "total_late_minutes": 77,
        "total_overtime_minutes": 105,
        "attendance_pct": 19.57,      # 4.5 / 23 = 19.5652...
    }


def test_monthly_mid_month_joiner_and_half_up(api, dataset):
    body = api.get("/analytics/employees/EMP1002/monthly", params={"month": "2026-07"}).json()
    # 0.5 / 16 * 100 = 3.125 -> 3.13 half-up (float or banker's rounding would give 3.12)
    assert (body["working_days"], body["present_days"], body["attendance_pct"]) == (16, 0.5, 3.13)


def test_monthly_no_logs_and_not_joined(api, dataset):
    no_logs = api.get("/analytics/employees/EMP1003/monthly", params={"month": "2026-07"}).json()
    assert no_logs == {"emp_code": "EMP1003", "month": "2026-07", "working_days": 23, "present_days": 0.0,
                       "leave_days": 0, "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0,
                       "attendance_pct": 0.0}
    future = api.get("/analytics/employees/EMP1004/monthly", params={"month": "2026-07"}).json()
    assert (future["working_days"], future["attendance_pct"]) == (0, None)


@pytest.mark.parametrize("month", ["2026-13", "2026-7", "26-07", "2026-07-01"])
def test_monthly_validation(api, dataset, month):
    assert api.get("/analytics/employees/EMP1001/monthly", params={"month": month}).status_code == 422


def test_monthly_unknown_employee(api, dataset):
    assert api.get("/analytics/employees/EMP0404/monthly", params={"month": "2026-07"}).status_code == 404
    assert api.get("/analytics/employees/EMP1001/monthly").status_code == 422


# --------------------------------------------------------------------------- department summary
def test_department_summary(api, dataset):
    body = api.get("/analytics/departments/summary", params={"month": "2026-07"}).json()
    assert body == {"month": "2026-07", "items": [
        {
            "department": "Engineering",
            "headcount": 3,           # EMP1001, EMP1002 (mid-month), EMP1003 (no logs); EMP1004 not yet joined
            "present_days": 5.0,      # 4.5 + 0.5
            # 46.5 h over 7 present records with hours (incl. the Saturdays); an average of
            # per-employee averages would give 6.45 instead.
            "avg_work_hours": 6.64,
            "late_count": 5,
            "total_late_minutes": 154,
            "leave_count": 1,
            "on_duty_count": 1,
        },
        {
            "department": "Sales",
            "headcount": 3,
            "present_days": 4.0,
            "avg_work_hours": 8.26,   # 33.02 / 4 = 8.255 -> half-up
            "late_count": 3,
            "total_late_minutes": 105,
            "leave_count": 0,
            "on_duty_count": 0,
        },
    ]}  # Ops has nobody who joined by 31 July, so it is omitted


def test_department_summary_filter_and_empty_periods(api, dataset):
    sales = api.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Sales"}).json()
    assert [i["department"] for i in sales["items"]] == ["Sales"]
    assert api.get("/analytics/departments/summary", params={"month": "2025-12"}).json()["items"] == []
    august = api.get("/analytics/departments/summary", params={"month": "2026-08"}).json()["items"]
    assert [(i["department"], i["headcount"], i["present_days"], i["avg_work_hours"]) for i in august] == [
        ("Engineering", 4, 0.0, None), ("Ops", 1, 0.0, None), ("Sales", 3, 0.0, None)]
    assert api.get("/analytics/departments/summary").status_code == 422


# --------------------------------------------------------------------------- leaderboard
def ranks(api, **params):
    items = api.get("/analytics/leaderboard/late", params={"month": "2026-07", **params}).json()["items"]
    return [(i["rank"], i["emp_code"], i["total_late_minutes"]) for i in items]


def test_leaderboard_competition_ranking(api, dataset):
    # Totals: EMP1001 77, EMP1002 77, EMP2001 40, EMP2002 40, EMP2003 25. EMP9999 has no employee: ignored.
    assert ranks(api) == [(1, "EMP1001", 77), (1, "EMP1002", 77), (3, "EMP2001", 40), (3, "EMP2002", 40),
                          (5, "EMP2003", 25)]


@pytest.mark.parametrize("limit, expected", [
    (1, ["EMP1001", "EMP1002"]),                         # tie at the cutoff: both rank 1
    (2, ["EMP1001", "EMP1002"]),                         # rank 3 is beyond 2
    (3, ["EMP1001", "EMP1002", "EMP2001", "EMP2002"]),   # more rows than limit
    (5, ["EMP1001", "EMP1002", "EMP2001", "EMP2002", "EMP2003"]),
])
def test_leaderboard_limit_after_ranking(api, dataset, limit, expected):
    assert [code for _, code, _ in ranks(api, limit=limit)] == expected


def test_leaderboard_department_and_shape(api, dataset):
    assert ranks(api, department="Sales", limit=1) == [(1, "EMP2001", 40), (1, "EMP2002", 40)]
    item = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 1}).json()["items"][0]
    assert item == {"rank": 1, "emp_code": "EMP1001", "name": "EMP1001", "department": "Engineering",
                    "total_late_minutes": 77, "late_count": 4}
    for bad in ({"limit": 0}, {"limit": 51}):
        assert api.get("/analytics/leaderboard/late", params={"month": "2026-07", **bad}).status_code == 422


# --------------------------------------------------------------------------- trend
def test_department_trend(api, dataset):
    body = api.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-08", "to": "2026-07-18"}).json()
    assert body["department"] == "Engineering"
    rows = [(r["date"], r["is_working_day"], r["headcount"], r["present_count"], r["late_count"],
             r["attendance_rate"], r["moving_avg_7d"]) for r in body["items"]]
    assert rows == [
        ("2026-07-08", True, 2, 1.0, 0, 0.5, 0.5),
        ("2026-07-09", True, 2, 1.0, 1, 0.5, 0.5),
        ("2026-07-10", True, 3, 0.0, 0, 0.0, 0.3333),      # EMP1002 joins; no records -> 0.0
        ("2026-07-11", False, 3, 0.0, 0, None, 0.3333),    # weekend: no rate, window unchanged
        ("2026-07-12", False, 3, 0.0, 0, None, 0.3333),
        ("2026-07-13", True, 3, 0.0, 0, 0.0, 0.25),
        ("2026-07-14", True, 3, 0.0, 0, 0.0, 0.2),         # full 7-row window: 1.0 / 5 rates
        ("2026-07-15", True, 3, 0.5, 1, 0.1667, 0.1333),   # half day
        ("2026-07-16", True, 3, 0.0, 0, 0.0, 0.0333),
        ("2026-07-17", True, 3, 0.0, 0, 0.0, 0.0333),
        ("2026-07-18", False, 3, 1.0, 0, None, 0.0333),    # weekend record counts as present; 0.1667 / 5
    ]


def test_trend_single_day_and_92_days(api, dataset):
    one = api.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-11", "to": "2026-07-11"}).json()
    assert one["items"] == [{"date": "2026-07-11", "is_working_day": False, "headcount": 3, "present_count": 0.0,
                             "late_count": 0, "attendance_rate": None, "moving_avg_7d": None}]
    quarter = api.get("/analytics/departments/Sales/trend", params={"from": "2026-10-01", "to": "2026-12-31"}).json()
    assert len(quarter["items"]) == 92 and quarter["items"][-1]["date"] == "2026-12-31"


@pytest.mark.parametrize("params, status", [
    ({"from": "2026-07-10", "to": "2026-07-09"}, 422),
    ({"from": "2026-10-01", "to": "2027-01-01"}, 422),  # 93 days
    ({"from": "2026-07-10"}, 422),
    ({"from": "2026-07-10", "to": "2026-07-32"}, 422),
])
def test_trend_validation(api, dataset, params, status):
    assert api.get("/analytics/departments/Engineering/trend", params=params).status_code == status


def test_trend_unknown_department(api, dataset):
    params = {"from": "2026-07-01", "to": "2026-07-02"}
    assert api.get("/analytics/departments/Marketing/trend", params=params).status_code == 404
    assert api.get("/analytics/departments/sales/trend", params=params).status_code == 404  # case-sensitive
