from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from conftest import ist_dt, ist_ms
from test_employees_attendance import make_employee, punch_in, punch_out

URL = "/attendance/EMP0001/2026-07-07"


def patch(api, url=URL, **body):
    return api.patch(url, json={"reason": "biometric glitch", "regularized_by": "hr.admin", **body})


@pytest.fixture
def late_record(api):
    """The DATA_MODEL sample 8 before correction: in 10:05, out 18:30 IST."""
    make_employee(api)
    punch_in(api, ist_ms(2026, 7, 7, 10, 5))
    return punch_out(api, ist_ms(2026, 7, 7, 18, 30)).json()


def test_correct_punch_in_recomputes_and_records_history(api, db, late_record):
    assert (late_record["late_minutes"], late_record["work_hours"]) == (35, 8.42)
    response = patch(api, punch_in=ist_ms(2026, 7, 7, 9, 28))
    assert response.status_code == 200
    rec = response.json()
    assert (rec["late_minutes"], rec["work_hours"], rec["punch_in"]) == (0, 9.03, ist_ms(2026, 7, 7, 9, 28))
    [entry] = rec["history"]
    assert entry["by"] == "hr.admin" and entry["reason"] == "biometric glitch" and isinstance(entry["at"], int)
    assert entry["changes"] == {
        "punch_in": {"from": ist_ms(2026, 7, 7, 10, 5), "to": ist_ms(2026, 7, 7, 9, 28)},
        "work_hours": {"from": 8.42, "to": 9.03},
        "late_minutes": {"from": 35, "to": 0},
    }
    # Nested instants are BSON dates in MongoDB, epoch ms in the API.
    stored = db.attendance_logs.find_one()["history"][0]
    assert stored["at"].tzinfo is not None
    assert stored["changes"]["punch_in"]["to"] == ist_dt(2026, 7, 7, 9, 28)


def test_change_punch_out_and_status_together(api, late_record):
    rec = patch(api, status="ON_DUTY", punch_out=ist_ms(2026, 7, 7, 19, 15)).json()
    assert (rec["status"], rec["overtime_minutes"], rec["work_hours"]) == ("ON_DUTY", 45, 9.17)
    assert set(rec["history"][0]["changes"]) == {"status", "punch_out", "overtime_minutes", "work_hours"}


def test_leave_clears_punches_and_derived(api, late_record):
    rec = patch(api, status="LEAVE").json()
    assert (rec["punch_in"], rec["punch_out"], rec["work_hours"], rec["late_minutes"]) == (None, None, None, 0)
    assert rec["history"][0]["changes"]["punch_out"] == {"from": ist_ms(2026, 7, 7, 18, 30), "to": None}
    assert set(rec["history"][0]["changes"]) == {"status", "punch_in", "punch_out", "work_hours", "late_minutes"}
    # Back to PRESENT needs a punch_in.
    assert patch(api, status="PRESENT").status_code == 422
    rec = patch(api, status="PRESENT", punch_in=ist_ms(2026, 7, 7, 9, 45)).json()
    assert (rec["late_minutes"], rec["punch_out"], rec["work_hours"], len(rec["history"])) == (15, None, None, 2)
    assert rec["history"][0]["changes"]["status"] == {"from": "PRESENT", "to": "LEAVE"}  # first entry untouched


def test_half_day_change_is_recorded(api, late_record):
    rec = patch(api, punch_out=ist_ms(2026, 7, 7, 13, 0)).json()
    assert (rec["work_hours"], rec["half_day"]) == (2.92, True)
    assert rec["history"][0]["changes"]["half_day"] == {"from": False, "to": True}


@pytest.mark.parametrize("body", [
    {"status": "ABSENT", "punch_in": 1783396680000},
    {"status": "LEAVE", "punch_out": 1783429200000},
    {"punch_in": ist_ms(2026, 7, 8, 9, 0)},             # moves the record to another date
    {"punch_out": ist_ms(2026, 7, 7, 10, 0)},           # before punch_in
    {"punch_out": ist_ms(2026, 7, 8, 10, 6)},           # more than 24 h
    {"punch_in": ist_ms(2026, 7, 7, 19, 0)},            # after the stored punch_out
    {"status": "PRESENT"},                              # changes nothing
    {"punch_in": ist_ms(2026, 7, 7, 10, 5)},            # same value: changes nothing
    {"late_minutes": 0, "work_hours": 10},              # derived fields are ignored -> nothing to change
    {"status": "HOLIDAY"},
    {"punch_in": 1783396680000.0},
    {"punch_in": "1783396680000"},
])
def test_invalid_corrections_are_422(api, late_record, body):
    assert patch(api, **body).status_code == 422


def test_reason_and_author_validation(api, late_record):
    assert api.patch(URL, json={"status": "WFH", "reason": "abc", "regularized_by": "hr"}).status_code == 422
    assert api.patch(URL, json={"status": "WFH", "reason": "valid reason"}).status_code == 422


def test_not_found(api, late_record):
    assert patch(api, "/attendance/EMP9999/2026-07-07", status="WFH").status_code == 404
    assert patch(api, "/attendance/EMP0001/2026-07-08", status="WFH").status_code == 404
    assert patch(api, "/attendance/EMP0001/2026-7-8", status="WFH").status_code == 422


def test_legacy_record_without_history_or_half_day(api, db):
    make_employee(api, "EMP0002")
    db.attendance_logs.insert_one({
        "emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT",
        "punch_in": ist_dt(2026, 7, 14, 9, 32), "punch_out": ist_dt(2026, 7, 14, 18, 31),
        "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0,
    })
    rec = patch(api, "/attendance/EMP0002/2026-07-14", status="WFH").json()
    assert rec["history"][0]["changes"] == {"status": {"from": "PRESENT", "to": "WFH"}}  # no spurious half_day
    assert rec["half_day"] is False


def test_absent_to_present(api, db):
    make_employee(api)
    db.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-07", "status": "ABSENT",
                                   "punch_in": None, "punch_out": None, "work_hours": None,
                                   "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []})
    rec = patch(api, status="PRESENT", punch_in=ist_ms(2026, 7, 7, 9, 30), punch_out=ist_ms(2026, 7, 7, 13, 30)).json()
    assert (rec["work_hours"], rec["half_day"], rec["late_minutes"]) == (4.0, True, 0)
    assert set(rec["history"][0]["changes"]) == {"status", "punch_in", "punch_out", "work_hours", "half_day"}


def test_overnight_regularize_keeps_shift_date(api):
    make_employee(api, shift_start="22:00", shift_end="06:00")
    punch_in(api, ist_ms(2026, 7, 6, 22, 0))
    # 01:00 on the 7th still belongs to the shift of the 6th.
    rec = patch(api, "/attendance/EMP0001/2026-07-06", punch_in=ist_ms(2026, 7, 7, 1, 0)).json()
    assert rec["late_minutes"] == 180
    assert patch(api, "/attendance/EMP0001/2026-07-06", punch_in=ist_ms(2026, 7, 7, 7, 0)).status_code == 422


def test_concurrent_corrections_never_lose_history(server, api, db, late_record):
    outs = [ist_ms(2026, 7, 7, 19, i) for i in range(10)]

    def correct(ms):
        return httpx.patch(f"{server.url}{URL}", json={
            "punch_out": ms, "reason": "concurrent fix", "regularized_by": f"user{ms}"}).status_code

    with ThreadPoolExecutor(max_workers=10) as pool:
        codes = list(pool.map(correct, outs))
    assert set(codes) <= {200, 409} and 200 in codes
    stored = db.attendance_logs.find_one()
    # Every accepted correction has exactly one entry, and entries chain: each "from" is the previous "to".
    assert len(stored["history"]) == codes.count(200)
    previous = ist_dt(2026, 7, 7, 18, 30)
    for entry in stored["history"]:
        assert entry["changes"]["punch_out"]["from"] == previous
        previous = entry["changes"]["punch_out"]["to"]
    assert stored["punch_out"] == previous
