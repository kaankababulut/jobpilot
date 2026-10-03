"""Tests for the API skeleton in jobpilot.api: /health, the API key check, the per-request
connection, the database-error handlers and the docs switch. No database or network: TestClient runs
the app in-process."""
import logging

import psycopg
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from jobpilot import api

URL = "postgresql://jobs:s3cretPass@127.0.0.1:5432/jobs"
KEY = "test-key-123"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    # importing jobpilot.api loads the real .env; start every test without its values
    for name in ("DATABASE_URL", "JOBPILOT_API_KEY", "JOBPILOT_DOCS"):
        monkeypatch.delenv(name, raising=False)


class FakeConn:
    """Stands in for a psycopg connection; records whether its with-block was exited."""
    def __init__(self):
        self.autocommit = False
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False


def boom(url, read_only=False):
    raise psycopg.OperationalError(f"connection to {url} failed: password s3cretPass rejected")


def no_connect(*args, **kwargs):
    pytest.fail("connect should not be called")


@pytest.fixture
def client():
    return TestClient(api.app)


@pytest.fixture
def test_app():
    """A fresh real app (same routes and error handlers) plus test-only routes using the real
    dependencies, so no throwaway endpoint ships."""
    app = api.create_app()

    @app.get("/protected", dependencies=[Depends(api.require_key)])
    def protected():
        return {"ok": True}

    @app.get("/conn")
    def uses_conn(conn=Depends(api.get_conn)):
        return {"autocommit": conn.autocommit}

    @app.get("/fails")
    def fails():
        boom(URL)

    @app.get("/times-out")
    def times_out():
        raise psycopg.errors.QueryCanceled(
            f"canceling statement due to statement timeout on {URL} (password s3cretPass)")

    return app


@pytest.mark.parametrize("db_url", [None, URL])
def test_health_never_touches_the_database(client, monkeypatch, db_url):
    # a public endpoint that opened connections would let anyone without a key load the database
    if db_url:
        monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setattr(api, "connect", no_connect)
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


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


def test_query_timeout_is_504_not_503_without_leaking(test_app, monkeypatch, caplog):
    # QueryCanceled is a subclass of OperationalError: the more specific handler must win
    monkeypatch.setenv("DATABASE_URL", URL)
    with caplog.at_level(logging.WARNING, logger="jobpilot.api"):
        r = TestClient(test_app).get("/times-out")
    assert r.status_code == 504 and r.json() == {"detail": "query timed out"}
    assert "statement timeout" not in r.text  # no driver text reaches the client
    assert "<DATABASE_URL>" in caplog.text  # logged, but redacted
    for text in (r.text, caplog.text):
        assert URL not in text and "s3cretPass" not in text


# ---------- docs switch (create_app reads JOBPILOT_DOCS, so no module reload is needed) ----------
@pytest.mark.parametrize("value", [None, "1", "", "yes"])
def test_docs_on_unless_switched_off(monkeypatch, value):
    if value is not None:
        monkeypatch.setenv("JOBPILOT_DOCS", value)
    client = TestClient(api.create_app())
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 200, path


@pytest.mark.parametrize("value", ["0", " 0 "])
def test_docs_off_hides_docs_but_spec_still_builds(monkeypatch, value):
    monkeypatch.setenv("JOBPILOT_DOCS", value)
    app = api.create_app()
    client = TestClient(app)
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert client.get(path).status_code == 404, path
    assert client.get("/health").json() == {"status": "ok"}  # the API itself still serves
    spec = app.openapi()  # built in code, for the snapshot, even with the URL closed
    assert set(spec["paths"]) == {"/health", "/jobs", "/jobs/{job_id}", "/skills", "/runs"}
