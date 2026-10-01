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
]
# score DESC NULLS LAST, then last_seen DESC, then id: 2 and 5 tie on both, so insert order decides
ORDER = ["2", "5", "1", HIMALAYAS_ID, "3"]


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
    ({"open_to_you": False}, ["2", HIMALAYAS_ID]),
    ({"min_score": 90}, ["2", "5", "1"]),
    ({"min_score": 0}, ["2", "5", "1", HIMALAYAS_ID]),  # NULL score is not "at least 0"
    ({"work_type": "Hybrid"}, ["2", "5"]),
    ({"source": "himalayas"}, [HIMALAYAS_ID]),
    ({"since": dt.date(2026, 9, 29)}, ["2", "5", "1", "3"]),
    ({"skill": "python"}, ["5", "1"]),
    ({"skill": "SQL"}, ["2", "1"]),
    ({"skill": "Rust"}, []),
    ({"q": "ACME"}, ["5", "1"]),                        # company, case-insensitive
    ({"q": "analyst"}, ["2"]),                          # title
    ({"q": "50%"}, ["3"]),                              # % is literal: "50 Apples" doesn't match
    ({"q": "_"}, []),                                   # _ is literal, not "any one character"
    ({"q": "C:\\dev"}, []),                             # backslash escape works in real Postgres
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
