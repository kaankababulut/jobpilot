"""Tests for GET /skills and GET /runs in jobpilot.api, plus checks over the whole OpenAPI spec.
The default tests fake the queries and the connection; the db-marked ones use the pg fixture."""
import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient

from jobpilot import api, queries

KEY = "test-key-123"
AUTH = {"X-API-Key": KEY}
SKILL = {"name": "Python", "category": "Languages", "on_cv": True, "jobs": 12, "share": 0.4}
RUN = {"id": 3, "kind": "daily", "run_date": dt.date(2026, 10, 1),
       "loaded_at": dt.datetime(2026, 10, 1, 10, 5, tzinfo=dt.timezone.utc), "rows_offered": 40, "rows_written": 38}


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
    """Overrides get_conn with a sentinel and fakes top_skills/recent_runs, recording their arguments."""
    conn, seen = object(), []

    def top_skills(c, **kw):
        assert c is conn
        seen.append(("top_skills", kw))
        return [SKILL]

    def recent_runs(c, **kw):
        assert c is conn
        seen.append(("recent_runs", kw))
        return [RUN]
    monkeypatch.setattr(queries, "top_skills", top_skills)
    monkeypatch.setattr(queries, "recent_runs", recent_runs)
    api.app.dependency_overrides[api.get_conn] = lambda: conn
    yield seen
    api.app.dependency_overrides.clear()


@pytest.mark.parametrize("path", ["/skills", "/runs"])
@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}])
def test_auth_required(client, calls, path, headers):
    assert client.get(path, headers=headers).status_code == 401
    assert calls == []


@pytest.mark.parametrize("query", ["/skills?days=0", "/skills?days=366", "/skills?days=x", "/skills?limit=0",
                                   "/skills?limit=101", "/skills?category=", "/skills?category=" + "x" * 41,
                                   "/runs?limit=0", "/runs?limit=51"])
def test_validation_is_422(client, calls, query):
    assert client.get(query, headers=AUTH).status_code == 422
    assert calls == []


def test_skills_defaults_and_params_passed_through(client, calls):
    client.get("/skills", headers=AUTH)
    client.get("/skills", headers=AUTH, params={"days": 7, "category": "Microsoft & Low-code", "limit": 5})
    assert calls == [("top_skills", {"days": 30, "category": None, "limit": 20}),
                     ("top_skills", {"days": 7, "category": "Microsoft & Low-code", "limit": 5})]


def test_skills_shape(client, calls):
    body = client.get("/skills?days=7", headers=AUTH).json()
    assert body == {"items": [SKILL], "days": 7}  # days echoed, so a caller can say "in the last 7 days"


def test_runs_params_and_shape(client, calls):
    assert client.get("/runs", headers=AUTH).status_code == 200
    body = client.get("/runs?limit=3", headers=AUTH).json()
    assert calls == [("recent_runs", {"limit": 10}), ("recent_runs", {"limit": 3})]
    assert body == {"items": [{**RUN, "run_date": "2026-10-01", "loaded_at": "2026-10-01T10:05:00Z"}]}


def test_openapi_covers_every_endpoint():
    spec = api.app.openapi()
    assert set(spec["paths"]) == {"/health", "/jobs", "/jobs/{job_id}", "/skills", "/runs", "/applications"}
    ops = {path: item["get"] for path, item in spec["paths"].items()}
    assert all(set(item) == {"get"} for item in spec["paths"].values())  # read-only: no other methods
    names = [op["operationId"] for op in ops.values()]
    assert sorted(names) == ["get_job", "health", "list_applications", "list_jobs", "recent_runs", "top_skills"]
    for path, op in ops.items():
        assert op["summary"] and op["description"], path
        assert all(p.get("description") for p in op.get("parameters", [])), path
        if path == "/health":
            assert "security" not in op
        else:
            assert op["security"] == [{"APIKeyHeader": []}], path
    share = spec["components"]["schemas"]["SkillDemand"]["properties"]["share"]
    assert "0-1" in share["description"]


def test_openapi_snapshot_is_current():
    # compared as parsed JSON, so a CRLF checkout on Windows doesn't fail it
    with open(api.SPEC_FILE, encoding="utf-8") as f:
        saved = json.load(f)
    assert saved == json.loads(api.spec_json()), (
        "the API contract changed: if that's intended, run `python -m jobpilot.api --write` and commit docs/openapi.json")


def test_openapi_snapshot_ignores_docs_switch(monkeypatch):
    monkeypatch.setenv("JOBPILOT_DOCS", "0")  # the deployed setting must not empty the snapshot
    assert set(json.loads(api.spec_json())["paths"]) == {"/health", "/jobs", "/jobs/{job_id}", "/skills", "/runs", "/applications"}


def test_write_spec_is_utf8_with_trailing_newline(tmp_path):
    path = tmp_path / "openapi.json"
    api.write_spec(str(path))
    raw = path.read_bytes()
    assert raw.endswith(b"}\n") and b"\r\n" not in raw  # not UTF-16, not CRLF
    assert raw.decode("utf-8") == api.spec_json()


# ---------- against the real SQL (pytest -m db) ----------
@pytest.fixture
def db_client(pg):
    api.app.dependency_overrides[api.get_conn] = lambda: pg  # the throwaway schema, never public
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()


@pytest.mark.db
def test_db_skills(db_client, pg):
    from test_db import CATEGORIES, load, rec
    today = pg.execute("SELECT current_date").fetchone()[0]  # the server's date, as top_skills uses
    load(pg, [rec(**{"Job ID": sid, "Run Date": (today - dt.timedelta(days=ago)).isoformat(),
                     "Skills You Have": have, "Skills To Learn": learn})
              for sid, ago, have, learn in [("a", 0, "Python", "Docker"), ("b", 3, "Python", ""), ("c", 40, "SQL", "")]])
    body = db_client.get("/skills?days=30", headers=AUTH).json()
    assert body["days"] == 30
    assert body["items"] == [  # c is outside the window, so SQL doesn't appear
        {"name": "Python", "category": CATEGORIES["Python"], "on_cv": True, "jobs": 2, "share": 1.0},
        {"name": "Docker", "category": CATEGORIES["Docker"], "on_cv": False, "jobs": 1, "share": 0.5}]


@pytest.mark.db
def test_db_runs(db_client, pg):
    pg.execute("INSERT INTO runs (kind, run_date, loaded_at, rows_offered, rows_written) VALUES "
               "('backfill', '2026-09-20', '2026-09-20 12:00+00', 5, 5), "
               "('daily', '2026-09-29', '2026-09-29 12:00+00', 10, 7)")
    items = db_client.get("/runs?limit=1", headers=AUTH).json()["items"]
    assert len(items) == 1 and items[0]["kind"] == "daily" and items[0]["run_date"] == "2026-09-29"
    assert set(items[0]) == {"id", "kind", "run_date", "loaded_at", "rows_offered", "rows_written"}
