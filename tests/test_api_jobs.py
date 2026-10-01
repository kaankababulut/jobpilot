"""Tests for GET /jobs and GET /jobs/{id} in jobpilot.api. The default tests fake the queries
and the connection; the db-marked ones run the real SQL in a throwaway schema (the pg fixture)."""
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from jobpilot import api, queries

KEY = "test-key-123"
AUTH = {"X-API-Key": KEY}

SUMMARY = {"id": 7, "source": "linkedin", "title": "Junior Python Developer", "company": "Acme",
           "location": "Istanbul", "work_type": "Remote", "open_to_you": True, "match_score": 90,
           "date_posted": dt.date(2026, 9, 28), "first_seen": dt.date(2026, 9, 29),
           "last_seen": dt.date(2026, 9, 30), "apply_url": "https://example.com/7", "skills": ["Python", "SQL"]}
DETAIL = {**{k: v for k, v in SUMMARY.items() if k != "skills"},
          "source_id": "7", "region": "Türkiye", "restrictions": [], "red_flags": ["unpaid"],
          "employment_type": "Internship", "seniority": "Entry level", "salary": None, "years_required": 0,
          "description": "Python and SQL.", "updated_at": dt.datetime(2026, 9, 30, 12, 0, tzinfo=dt.timezone.utc),
          "skills": [{"name": "Python", "category": "Languages", "on_cv": True},
                     {"name": "Docker", "category": "DevOps", "on_cv": False}]}


@pytest.fixture(autouse=True)
def env(monkeypatch, request):
    # importing jobpilot.api loads the real .env; tests use their own key and never the real URL
    # db tests keep it: the pg fixture needs the URL, and in CI there is no .env to reload it from
    if request.node.get_closest_marker("db") is None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("JOBPILOT_API_KEY", KEY)


@pytest.fixture
def fake_conn():
    """Overrides get_conn with a sentinel object, so no test here opens a real connection."""
    conn = object()
    api.app.dependency_overrides[api.get_conn] = lambda: conn
    yield conn
    api.app.dependency_overrides.clear()


@pytest.fixture
def client():
    return TestClient(api.app)


@pytest.fixture
def calls(monkeypatch, fake_conn):
    """Fakes queries.list_jobs/get_job and records their arguments."""
    seen = []

    def list_jobs(conn, **kw):
        assert conn is fake_conn
        seen.append(kw)
        return [SUMMARY] * min(kw["limit"], 3)  # 3 matching jobs in the "database"

    def get_job(conn, job_id):
        seen.append(job_id)
        return DETAIL if job_id == 7 else None
    monkeypatch.setattr(queries, "list_jobs", list_jobs)
    monkeypatch.setattr(queries, "get_job", get_job)
    return seen


@pytest.mark.parametrize("path", ["/jobs", "/jobs/7"])
@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}])
def test_auth_required(client, calls, path, headers):
    assert client.get(path, headers=headers).status_code == 401
    assert calls == []


@pytest.mark.parametrize("path", ["/jobs", "/jobs/7"])
def test_no_key_never_opens_a_connection(client, monkeypatch, path):
    # require_key runs before get_conn: a refused request must not reach the database
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@127.0.0.1:1/x")
    monkeypatch.setattr(api, "connect", lambda *a, **k: pytest.fail("connect should not be called"))
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1", "offset=10001", "min_score=101",
                                   "min_score=-1", "work_type=remote", "work_type=Onsite", "source=indeed",
                                   "since=garbage", "since=2026-13-01", "open_to_you=maybe",
                                   "skill=" + "x" * 61, "q=" + "x" * 101, "q="])
def test_list_validation_is_422(client, calls, query):
    assert client.get(f"/jobs?{query}", headers=AUTH).status_code == 422
    assert calls == []  # rejected before any query runs


@pytest.mark.parametrize("job_id", ["0", "-1", "abc", str(2**63)])
def test_get_validation_is_422(client, calls, job_id):
    assert client.get(f"/jobs/{job_id}", headers=AUTH).status_code == 422
    assert calls == []


def test_defaults_passed_through(client, calls):
    client.get("/jobs", headers=AUTH)
    assert calls == [{"limit": 21, "offset": 0, "open_to_you": None, "min_score": None, "skill": None,
                      "work_type": None, "source": None, "since": None, "q": None}]


def test_filters_passed_through_as_plain_values(client, calls):
    r = client.get("/jobs", headers=AUTH, params={
        "open_to_you": "true", "min_score": 70, "skill": "Python", "work_type": "Remote?", "source": "himalayas",
        "since": "2026-09-01", "q": "50% off", "limit": 5, "offset": 10})
    assert r.status_code == 200
    kw = calls[0]
    assert kw == {"limit": 6, "offset": 10, "open_to_you": True, "min_score": 70, "skill": "Python",
                  "work_type": "Remote?", "source": "himalayas", "since": dt.date(2026, 9, 1), "q": "50% off"}
    # plain str, not the Enum, so psycopg sends exactly the schema's value
    assert type(kw["work_type"]) is str and type(kw["source"]) is str


def test_list_shape_and_has_more(client, calls):
    body = client.get("/jobs?limit=2", headers=AUTH).json()
    assert body["limit"] == 2 and body["offset"] == 0 and body["has_more"] is True
    assert len(body["items"]) == 2  # the extra row is only a probe, never returned
    item = body["items"][0]
    assert set(item) == set(queries.SUMMARY_COLS) | {"skills"}
    assert item["skills"] == ["Python", "SQL"] and item["first_seen"] == "2026-09-29"
    last = client.get("/jobs?limit=3", headers=AUTH).json()
    assert len(last["items"]) == 3 and last["has_more"] is False


def test_get_job_shape(client, calls):
    r = client.get("/jobs/7", headers=AUTH)
    assert r.status_code == 200 and calls == [7]
    body = r.json()
    assert set(body) == set(queries.DETAIL_COLS) | {"skills"}
    assert body["skills"][1] == {"name": "Docker", "category": "DevOps", "on_cv": False}
    assert body["red_flags"] == ["unpaid"] and body["salary"] is None


def test_unknown_job_is_404(client, calls):
    r = client.get("/jobs/8", headers=AUTH)
    assert r.status_code == 404 and r.json() == {"detail": "job not found"}


def test_openapi_lists_job_tools():
    spec = api.app.openapi()
    assert spec["components"]["securitySchemes"]["APIKeyHeader"]["name"] == "X-API-Key"
    ops = {path: spec["paths"][path]["get"] for path in ("/jobs", "/jobs/{job_id}")}
    assert ops["/jobs"]["operationId"] == "list_jobs" and ops["/jobs/{job_id}"]["operationId"] == "get_job"
    for op in ops.values():
        assert op["summary"] and op["description"]
        assert op["security"] == [{"APIKeyHeader": []}]
        assert all(p.get("description") for p in op["parameters"])  # orchestrators read these
    schemas = spec["components"]["schemas"]
    assert "description" in schemas["JobSummary"]["properties"]["match_score"]
    assert schemas["WorkType"]["enum"] == ["Remote", "Remote?", "Hybrid", "On-site"]
    assert "security" not in spec["paths"]["/health"]["get"]


# ---------- against the real SQL (pytest -m db) ----------
@pytest.fixture
def db_client(pg):
    api.app.dependency_overrides[api.get_conn] = lambda: pg  # the throwaway schema, never public
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()


@pytest.fixture
def seeded(pg) -> dict[str, int]:
    from test_db import load, rec
    # (Job ID, score, open, skills)
    jobs = [("1", 90, "Yes", "Python, SQL"), ("2", 95, "No", "SQL"), ("3", 60, "Yes", "Python"),
            ("4", 70, "Yes", "Docker")]
    load(pg, [rec(**{"Job ID": sid, "Match Score (/100)": score, "Open To You?": open_,
                     "Skills You Have": skills, "Skills To Learn": ""}) for sid, score, open_, skills in jobs])
    return dict(pg.execute("SELECT source_id, id FROM jobs").fetchall())


@pytest.mark.db
def test_db_list_filters(db_client, seeded):
    body = db_client.get("/jobs?open_to_you=true&min_score=70", headers=AUTH).json()
    assert [j["id"] for j in body["items"]] == [seeded["1"], seeded["4"]]  # 2 isn't open, 3 scores too low
    assert body["has_more"] is False


@pytest.mark.db
def test_db_get_job_with_skills(db_client, seeded):
    from test_db import CATEGORIES
    body = db_client.get(f"/jobs/{seeded['4']}", headers=AUTH).json()
    assert body["skills"] == [{"name": "Docker", "category": CATEGORIES["Docker"], "on_cv": False}]
    assert db_client.get(f"/jobs/{max(seeded.values()) + 1}", headers=AUTH).status_code == 404
