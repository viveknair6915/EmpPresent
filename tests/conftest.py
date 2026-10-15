"""Test fixtures: each server fixture runs the real app under uvicorn against a throwaway database."""
import os
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from pymongo import MongoClient

ROOT = Path(__file__).resolve().parent.parent
MONGO_URI = os.getenv("TEST_MONGO_URI", "mongodb://localhost:27017")
IST = timezone(timedelta(hours=5, minutes=30))


def ist_ms(y, mo, d, h=0, mi=0, s=0, ms=0) -> int:
    """Epoch milliseconds of an IST wall-clock time."""
    dt = datetime(y, mo, d, h, mi, s, ms * 1000, tzinfo=IST)
    return int(dt.timestamp()) * 1000 + ms


def ist_dt(y, mo, d, h=0, mi=0, s=0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=IST).astimezone(timezone.utc)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Server:
    def __init__(self, db_name: str):
        self.db_name = db_name
        self.port = _free_port()
        env = {**os.environ, "MONGO_URI": MONGO_URI, "MONGO_DB": db_name}
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(self.port), "--workers", "1"],
            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        self.url = f"http://127.0.0.1:{self.port}"
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                if httpx.get(f"{self.url}/health").status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        self.stop()
        raise RuntimeError("server did not become healthy within 20 seconds")

    def stop(self):
        self.proc.terminate()
        self.proc.wait(timeout=10)


@pytest.fixture(scope="session")
def mongo():
    client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=3000)
    client.admin.command("ping")
    return client


@pytest.fixture(scope="session")
def server(mongo):
    db_name = f"attendance_test_{uuid.uuid4().hex[:8]}"
    srv = Server(db_name)
    yield srv
    srv.stop()
    mongo.drop_database(db_name)


@pytest.fixture
def db(server, mongo):
    """Empty collections (indexes kept) before every test."""
    database = mongo[server.db_name]
    database.employees.delete_many({})
    database.attendance_logs.delete_many({})
    return database


@pytest.fixture
def api(server, db):
    with httpx.Client(base_url=server.url, timeout=30) as client:
        yield client
