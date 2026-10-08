"""Integration tests for jobpilot.queries against the local Postgres (pytest -m db).
Each test runs in its own throwaway schema (see the pg fixture), never in the real tables."""
import datetime as dt

import pytest

from jobpilot import queries
from jobpilot.queries import DETAIL_COLS, SUMMARY_COLS
from test_db import CATEGORIES, load, rec
from test_queries import EVIL

pytestmark = pytest.mark.db
HIMALAYAS_ID = "https://himalayas.app/companies/fifty/jobs/backend-engineer"

# (Job ID, score, Run Date, open, work type, title, company, skills); "-" loads as a NULL score
SEED = [
    ("1", 90, "2026-09-29", "Yes", "Remote", "Junior Python Developer", "Acme", "Python, SQL"),
    ("2", 90, "2026-09-30", "No", "Hybrid", "Data Analyst", "Globex", "SQL"),
    ("3", "-", "2026-09-29", "Yes", "Remote?", "Save 50% Intern", "Initech", ""),
    (HIMALAYAS_ID, 40, "2026-09-20", "No", "On-site", "Backend Engineer", "50 Apples", "Kubernetes"),
    ("5", 90, "2026-09-30", "Yes", "Hybrid", "QA Tester", "acme labs", "Python"),
    ("6", 30, "2026-09-29", "No", "Remote", "Intern C:\\dev Tools", "Umbrella", ""),
]
# score DESC NULLS LAST, then last_seen DESC, then id: 2 and 5 tie on both, so insert order decides
ORDER = ["2", "5", "1", HIMALAYAS_ID, "6", "3"]


@pytest.fixture
def ids(pg) -> dict[str, int]:
    # one batch, so ids are generated in SEED order
    load(pg, [rec(**{"Job ID": sid, "Match Score (/100)": score, "Run Date": run, "Open To You?": open_,
                     "Work Type": wt, "Job Title": title, "Company Name": company,
                     "Skills You Have": skills, "Skills To Learn": ""})
              for sid, score, run, open_, wt, title, company, skills in SEED])
    return dict(pg.execute("SELECT source_id, id FROM jobs").fetchall())


def listed(pg, ids, **kw) -> list[str]:
    # results as Job IDs, so assertions read like the SEED table
    back = {v: k for k, v in ids.items()}
    return [back[r["id"]] for r in queries.list_jobs(pg, **kw)]


def test_default_sort_puts_null_score_last_and_breaks_ties(pg, ids):
    assert listed(pg, ids) == ORDER


def test_paging_walks_the_same_order(pg, ids):
    pages = [listed(pg, ids, limit=2, offset=o) for o in (0, 2, 4, 6)]
    assert pages == [ORDER[0:2], ORDER[2:4], ORDER[4:], []]


@pytest.mark.parametrize("kw, expected", [
    ({"open_to_you": True}, ["5", "1", "3"]),
    ({"open_to_you": False}, ["2", HIMALAYAS_ID, "6"]),
    ({"min_score": 90}, ["2", "5", "1"]),
    ({"min_score": 0}, ["2", "5", "1", HIMALAYAS_ID, "6"]),  # NULL score is not "at least 0"
    ({"work_type": "Hybrid"}, ["2", "5"]),
    ({"source": "himalayas"}, [HIMALAYAS_ID]),
    ({"since": dt.date(2026, 9, 29)}, ["2", "5", "1", "6", "3"]),
    ({"skill": "python"}, ["5", "1"]),
    ({"skill": "SQL"}, ["2", "1"]),
    ({"skill": "Rust"}, []),
    ({"q": "ACME"}, ["5", "1"]),                        # company, case-insensitive
    ({"q": "analyst"}, ["2"]),                          # title
    ({"q": "50%"}, ["3"]),                              # % is literal: "50 Apples" doesn't match
    ({"q": "_"}, []),                                   # _ is literal, not "any one character"
    ({"q": "C:\\dev"}, ["6"]),                          # unescaped, \d would mean a plain "d" and miss
    ({"q": EVIL}, []),                                  # injection strings are just data
    ({"skill": EVIL}, []),
    ({"open_to_you": True, "skill": "Python"}, ["5", "1"]),
])
def test_filters(pg, ids, kw, expected):
    assert listed(pg, ids, **kw) == expected


def test_list_rows_are_summaries_with_skill_names(pg, ids):
    rows = {r["id"]: r for r in queries.list_jobs(pg)}
    assert all(set(r) == set(SUMMARY_COLS) | {"skills"} for r in rows.values())  # no description
    assert rows[ids["1"]]["skills"] == ["Python", "SQL"]
    assert rows[ids["3"]]["skills"] == []
    assert rows[ids["1"]]["first_seen"] == dt.date(2026, 9, 29)


def test_get_job_returns_full_record_with_skills(pg, ids):
    job = queries.get_job(pg, ids["1"])
    assert set(job) == set(DETAIL_COLS) | {"skills"}
    assert job["title"] == "Junior Python Developer" and job["description"] == "Python and SQL."
    assert job["restrictions"] == [] and job["red_flags"] == []
    assert job["skills"] == [{"name": "Python", "category": CATEGORIES["Python"], "on_cv": True},
                             {"name": "SQL", "category": CATEGORIES["SQL"], "on_cv": True}]
    assert queries.get_job(pg, ids["3"])["skills"] == []


def test_get_job_unknown_id_is_none(pg, ids):
    assert queries.get_job(pg, max(ids.values()) + 1) is None


# ---------- top_skills ----------
@pytest.fixture
def window(pg) -> dt.date:
    # dates relative to the server's current_date, not Python's, so the test can't straddle midnight
    today = pg.execute("SELECT current_date").fetchone()[0]
    jobs = [("a", 0, "Python, SQL"), ("b", 30, "Python, Docker"), ("c", 31, "Python, Kubernetes"), ("d", 5, "")]
    load(pg, [rec(**{"Job ID": sid, "Run Date": (today - dt.timedelta(days=ago)).isoformat(),
                     "Skills You Have": skills, "Skills To Learn": ""}) for sid, ago, skills in jobs])
    return today


def test_top_skills_counts_share_and_window_boundary(pg, window):
    # a, b (exactly 30 days ago) and d (no skills) are in the window; c (31 days ago) is not
    rows = queries.top_skills(pg, days=30)
    assert [(r["name"], r["jobs"], r["share"]) for r in rows] == \
        [("Python", 2, 0.667), ("Docker", 1, 0.333), ("SQL", 1, 0.333)]  # jobs DESC, then name
    assert {r["name"]: r["on_cv"] for r in rows} == {"Python": True, "Docker": False, "SQL": True}
    assert rows[0]["category"] == CATEGORIES["Python"]


def test_top_skills_narrow_window_and_limit(pg, window):
    assert [(r["name"], r["share"]) for r in queries.top_skills(pg, days=0)] == [("Python", 1.0), ("SQL", 1.0)]
    assert [r["name"] for r in queries.top_skills(pg, days=30, limit=1)] == ["Python"]


def test_top_skills_category_filter_keeps_window_total(pg, window):
    cat = CATEGORIES["Docker"]
    in_window = {"Python": 2, "Docker": 1, "SQL": 1}
    expected = sorted(((n, c) for n, c in in_window.items() if CATEGORIES[n] == cat), key=lambda x: (-x[1], x[0]))
    rows = queries.top_skills(pg, days=30, category=cat)
    assert [(r["name"], r["jobs"]) for r in rows] == expected
    assert all(r["share"] == round(r["jobs"] / 3, 3) for r in rows)  # still out of all 3 jobs in the window
    assert queries.top_skills(pg, category=EVIL) == []


def test_top_skills_empty_window(pg):
    assert queries.top_skills(pg) == []  # no jobs: no rows, and no division by zero


# ---------- recent_runs ----------
def test_recent_runs_newest_first_with_id_tiebreak(pg):
    pg.execute("INSERT INTO runs (kind, run_date, loaded_at, rows_offered, rows_written) VALUES "
               "('backfill', '2026-09-20', '2026-09-20 12:00+00', 5, 5), "
               "('daily', '2026-09-29', '2026-09-29 12:00+00', 10, 7), "
               "('daily', '2026-09-29', '2026-09-29 12:00+00', 10, 0)")  # same loaded_at: higher id first
    rows = queries.recent_runs(pg)
    assert [(r["kind"], r["rows_written"]) for r in rows] == [("daily", 0), ("daily", 7), ("backfill", 5)]
    assert set(rows[0]) == {"id", "kind", "run_date", "loaded_at", "rows_offered", "rows_written"}
    assert [r["id"] for r in queries.recent_runs(pg, limit=2)] == [r["id"] for r in rows[:2]]


# ---------- list_applications ----------
# (company, applied_on, status); 3 and 4 share a day, so the higher id must come first
APPS = [("Acme", "2026-09-20", "applied"), ("Globex", "2026-09-25", "rejected"),
        ("Initech", "2026-09-28", "interview"), ("Umbrella", "2026-09-28", "offer")]


@pytest.fixture
def apps(pg) -> dict[str, int]:
    for company, day, status in APPS:
        pg.execute("INSERT INTO applications (company, title, applied_on, status, source) "
                   "VALUES (%s, 'Intern', %s, %s, 'manual')", (company, day, status))
    return dict(pg.execute("SELECT company, id FROM applications").fetchall())


def companies(pg, **kw) -> list[str]:
    return [r["company"] for r in queries.list_applications(pg, **kw)]


@pytest.mark.parametrize("kw, expected", [
    ({}, ["Umbrella", "Initech", "Globex", "Acme"]),     # newest applied_on first, id breaks the tie
    ({"open_only": True}, ["Initech", "Acme"]),          # offer and rejected are closed
    ({"status": "rejected"}, ["Globex"]),
    ({"status": "offer", "open_only": True}, []),        # the two filters combine with AND
    ({"status": EVIL}, []),                              # injection strings are just data
])
def test_list_applications_filters(pg, apps, kw, expected):
    assert companies(pg, **kw) == expected


def test_list_applications_paging(pg, apps):
    pages = [companies(pg, limit=3, offset=o) for o in (0, 3, 6)]
    assert pages == [["Umbrella", "Initech", "Globex"], ["Acme"], []]


def test_list_applications_columns_and_last_event(pg, apps):
    pg.execute("INSERT INTO application_events (application_id, status, at) VALUES "
               "(%s, 'applied', '2026-09-28 09:00+00'), (%s, 'interview', '2026-10-02 09:00+00')",
               (apps["Initech"], apps["Initech"]))
    rows = {r["company"]: r for r in queries.list_applications(pg)}
    assert set(rows["Acme"]) == {"id", "job_id", "company", "title", "url", "recorded_via", "status", "applied_on",
                                 "created_at", "updated_at", "last_event_at"}  # no notes
    assert rows["Initech"]["last_event_at"] == dt.datetime(2026, 10, 2, 9, 0, tzinfo=dt.timezone.utc)
    assert rows["Acme"]["last_event_at"] is None and rows["Acme"]["applied_on"] == dt.date(2026, 9, 20)


def test_list_applications_runs_as_the_read_only_api_role(pg, apps):
    from test_feedback_schema_db import as_role
    with as_role(pg, "jobpilot_api"):  # the role the deployed API connects as
        assert companies(pg, open_only=True) == ["Initech", "Acme"]
