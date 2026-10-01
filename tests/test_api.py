"""Tests for the API skeleton in jobpilot.api: /health, the API key check, the per-request
connection and the database-error handler. No database or network: TestClient runs the app in-process."""
import logging
import os

import psycopg
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from conftest import db_unavailable
from jobpilot import api

# taken before clean_env removes it: CI's own variable, or the one importing api loaded from .env
REAL_DB_URL = os.environ.get("DATABASE_URL", "").strip()
URL = "postgresql://jobs:s3cretPass@127.0.0.1:5432/jobs"
KEY = "test-key-123"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, request):
    # importing jobpilot.api loads the real .env; start every test without its values
    # db tests keep it: the pg fixture needs the URL, and in CI there is no .env to reload it from
    if request.node.get_closest_marker("db") is None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("JOBPILOT_API_KEY", raising=False)


class FakeConn:
    """Stands in for a psycopg connection; records whether its with-block was exited."""
    def __init__(self):
        self.autocommit = False
        self.closed = False
        self.sql = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def execute(self, sql):
        self.sql.append(sql)


def boom(url, read_only=False):
    raise psycopg.OperationalError(f"connection to {url} failed: password s3cretPass rejected")


@pytest.fixture
def client():
    return TestClient(api.app)


@pytest.fixture
def test_app():
    """A fresh app with test-only routes using the real dependencies, so no throwaway endpoint ships."""
    app = FastAPI()
    app.add_exception_handler(psycopg.OperationalError, api.db_unavailable)

    @app.get("/protected", dependencies=[Depends(api.require_key)])
    def protected():
        return {"ok": True}

    @app.get("/conn")
    def uses_conn(conn=Depends(api.get_conn)):
        return {"autocommit": conn.autocommit}

    @app.get("/fails")
    def fails():
        boom(URL)

    return app


def test_health_without_database_url(client, monkeypatch):
    monkeypatch.setattr(api, "connect", lambda *a, **k: pytest.fail("connect should not be called"))
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok", "db": "not configured"}


def test_health_reports_down_without_leaking(client, monkeypatch, caplog):
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setattr(api, "connect", boom)
    with caplog.at_level(logging.WARNING, logger="jobpilot.api"):
        r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok", "db": "down"}
    for text in (r.text, caplog.text):
        assert URL not in text and "s3cretPass" not in text


def test_health_ok_uses_read_only_connection(client, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)
    conn, calls = FakeConn(), []
    monkeypatch.setattr(api, "connect", lambda url, read_only=False: calls.append(read_only) or conn)
    assert client.get("/health").json() == {"status": "ok", "db": "ok"}
    assert calls == [True] and conn.sql == ["SELECT 1"] and conn.closed


def test_key_not_configured_fails_closed(test_app, monkeypatch):
    monkeypatch.setenv("JOBPILOT_API_KEY", "   ")  # blank counts as unset
    r = TestClient(test_app).get("/protected", headers={"X-API-Key": "anything"})
    assert r.status_code == 503 and r.json() == {"detail": "API key not configured"}


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}, {"X-API-Key": ""}])
def test_missing_or_wrong_key_is_401(test_app, monkeypatch, headers):
    monkeypatch.setenv("JOBPILOT_API_KEY", KEY)
    r = TestClient(test_app).get("/protected", headers=headers)
    assert r.status_code == 401 and KEY not in r.text


def test_right_key_passes(test_app, monkeypatch):
    monkeypatch.setenv("JOBPILOT_API_KEY", KEY)
    r = TestClient(test_app).get("/protected", headers={"X-API-Key": KEY})
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_openapi_has_key_scheme_and_health_summary(test_app):
    test_app.include_router(api.app.router)  # /health from the real app, next to the protected route
    spec = TestClient(test_app).get("/openapi.json").json()
    scheme = spec["components"]["securitySchemes"]["APIKeyHeader"]
    assert scheme == {"type": "apiKey", "in": "header", "name": "X-API-Key",
                      "description": "The value of JOBPILOT_API_KEY on the server."}
    assert spec["paths"]["/protected"]["get"]["security"] == [{"APIKeyHeader": []}]
    assert spec["paths"]["/health"]["get"]["summary"]
    assert "security" not in spec["paths"]["/health"]["get"]


def test_real_app_describes_itself():
    spec = api.app.openapi()
    assert spec["info"]["title"] == "JobPilot API" and spec["info"]["description"]


def test_get_conn_without_url_is_503(test_app):
    r = TestClient(test_app).get("/conn")
    assert r.status_code == 503 and r.json() == {"detail": "database not configured"}


def test_get_conn_is_read_only_autocommit_and_closed(test_app, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)
    conn, calls = FakeConn(), []
    monkeypatch.setattr(api, "connect", lambda url, read_only=False: calls.append(read_only) or conn)
    r = TestClient(test_app).get("/conn")
    assert r.json() == {"autocommit": True}
    assert calls == [True] and conn.closed


def test_connect_error_in_dependency_is_503_without_leaking(test_app, monkeypatch, caplog):
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setattr(api, "connect", boom)
    with caplog.at_level(logging.WARNING, logger="jobpilot.api"):
        r = TestClient(test_app).get("/conn")
    assert r.status_code == 503 and r.json() == {"detail": "database unavailable"}
    assert "<DATABASE_URL>" in caplog.text  # logged, but redacted
    for text in (r.text, caplog.text):
        assert URL not in text and "s3cretPass" not in text


def test_operational_error_in_route_is_503_without_leaking(test_app, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)
    r = TestClient(test_app).get("/fails")
    assert r.status_code == 503 and r.json() == {"detail": "database unavailable"}
    assert URL not in r.text and "s3cretPass" not in r.text


@pytest.mark.db
def test_health_with_real_database(client, monkeypatch, real_db_url):
    monkeypatch.setenv("DATABASE_URL", real_db_url)  # read-only SELECT 1; touches no table
    body = client.get("/health").json()
    if body["db"] == "down":
        db_unavailable("database not reachable")
    assert body == {"status": "ok", "db": "ok"}


@pytest.fixture
def real_db_url():
    if not REAL_DB_URL:
        db_unavailable("DATABASE_URL not set")
    return REAL_DB_URL
