"""Tests for GET /applications in jobpilot.api. The default tests fake the query and the connection;
the db-marked one runs the real SQL in a throwaway schema (the pg fixture)."""
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from jobpilot import api, feedback, openapi2, queries

KEY = "test-key-123"
AUTH = {"X-API-Key": KEY}
APP = {"id": 4, "job_id": 7, "company": "Acme", "title": "Data Intern", "url": "https://example.com/7",
       "recorded_via": "telegram", "status": "interview", "applied_on": dt.date(2026, 10, 1),
       "created_at": dt.datetime(2026, 10, 1, 9, 0, tzinfo=dt.timezone.utc),
       "updated_at": dt.datetime(2026, 10, 3, 9, 0, tzinfo=dt.timezone.utc),
       "last_event_at": dt.datetime(2026, 10, 3, 9, 0, tzinfo=dt.timezone.utc)}


@pytest.fixture(autouse=True)
def env(monkeypatch, request):
    # importing jobpilot.api loads the real .env; tests use their own key and never the real URL
    # db tests keep it: the pg fixture needs the URL, and in CI there is no .env to reload it from
    if request.node.get_closest_marker("db") is None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("JOBPILOT_API_KEY", KEY)


@pytest.fixture
def client():
    return TestClient(api.app)


@pytest.fixture
def calls(monkeypatch):
    """Overrides get_conn with a sentinel and fakes list_applications, recording its arguments."""
    conn, seen = object(), []

    def list_applications(c, **kw):
        assert c is conn
        seen.append(kw)
        return [APP] * min(kw["limit"], 3)  # 3 matching applications in the "database"
    monkeypatch.setattr(queries, "list_applications", list_applications)
    api.app.dependency_overrides[api.get_conn] = lambda: conn
    yield seen
    api.app.dependency_overrides.clear()


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}])
def test_auth_required(client, calls, headers):
    assert client.get("/applications", headers=headers).status_code == 401
    assert calls == []


def test_no_key_never_opens_a_connection(client, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@127.0.0.1:1/x")
    monkeypatch.setattr(api, "connect", lambda *a, **k: pytest.fail("connect should not be called"))
    assert client.get("/applications").status_code == 401


@pytest.mark.parametrize("query", ["status=Applied", "status=open", "status=", "open=maybe", "limit=0",
                                   "limit=101", "offset=-1", "offset=10001"])
def test_validation_is_422(client, calls, query):
    assert client.get(f"/applications?{query}", headers=AUTH).status_code == 422
    assert calls == []  # rejected before any query runs


def test_defaults_passed_through(client, calls):
    client.get("/applications", headers=AUTH)
    assert calls == [{"status": None, "open_only": False, "limit": 21, "offset": 0}]


def test_params_passed_through_as_plain_values(client, calls):
    r = client.get("/applications", headers=AUTH,
                   params={"status": "interview", "open": "true", "limit": 5, "offset": 10})
    assert r.status_code == 200
    assert calls == [{"status": "interview", "open_only": True, "limit": 6, "offset": 10}]
    assert type(calls[0]["status"]) is str  # not the Enum, so psycopg sends exactly the schema's value


def test_shape_and_has_more(client, calls):
    body = client.get("/applications?limit=2", headers=AUTH).json()
    assert (body["limit"], body["offset"], body["has_more"], len(body["items"])) == (2, 0, True, 2)
    assert body["items"][0] == {**APP, "applied_on": "2026-10-01", "created_at": "2026-10-01T09:00:00Z",
                                "updated_at": "2026-10-03T09:00:00Z", "last_event_at": "2026-10-03T09:00:00Z"}
    assert client.get("/applications?limit=3", headers=AUTH).json()["has_more"] is False


def test_status_enum_matches_the_schema_list():
    assert [s.value for s in api.ApplicationStatus] == list(feedback.STATUSES)


def test_openapi_and_connector_spec():
    spec = api.app.openapi()
    op = spec["paths"]["/applications"]["get"]
    assert op["operationId"] == "list_applications" and op["security"] == [{"APIKeyHeader": []}]
    assert spec["components"]["schemas"]["ApplicationStatus"]["enum"] == list(feedback.STATUSES)
    status = spec["components"]["schemas"]["ApplicationInfo"]["properties"]["status"]
    assert status["$ref"] == "#/components/schemas/ApplicationStatus"  # the response publishes the 6 values too
    v2_spec = openapi2.to_swagger2(spec)
    assert v2_spec["definitions"]["ApplicationInfo"]["properties"]["status"] == {"$ref": "#/definitions/ApplicationStatus"}
    assert v2_spec["definitions"]["ApplicationStatus"]["enum"] == list(feedback.STATUSES)
    v2 = {p["name"]: p for p in v2_spec["paths"]["/applications"]["get"]["parameters"]}
    assert v2["status"]["type"] == "string" and v2["status"]["enum"] == list(feedback.STATUSES)
    assert v2["open"]["type"] == "boolean" and v2["open"]["default"] is False  # the alias, not "open_only"


# ---------- against the real SQL (pytest -m db) ----------
@pytest.mark.db
def test_db_list_applications(pg):
    from test_feedback_schema_db import add_application
    api.app.dependency_overrides[api.get_conn] = lambda: pg  # the throwaway schema, never public
    try:
        live = add_application(pg, "Acme", "Intern")
        done = add_application(pg, "Globex", "Analyst")
        pg.execute("UPDATE applications SET status = 'rejected' WHERE id = %s", (done,))
        body = TestClient(api.app).get("/applications?open=true", headers=AUTH).json()
    finally:
        api.app.dependency_overrides.clear()
    assert [a["id"] for a in body["items"]] == [live] and body["has_more"] is False
    assert body["items"][0]["last_event_at"] is None  # inserted directly, so no event was logged
