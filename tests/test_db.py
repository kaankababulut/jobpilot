"""Integration tests for jobpilot.db against the local Postgres (pytest -m db).
Each test runs in its own throwaway schema (see the pg fixture), never in the real tables."""
import datetime as dt

import pytest

import job_searcher as js
from jobpilot import db
from jobpilot.records import row_to_record
from test_records import make_row

pytestmark = pytest.mark.db
CATEGORIES = {name: cat for name, (cat, *_) in js.SKILLS.items()}
CV = {"Python", "SQL"}


def rec(**kw) -> dict:
    return row_to_record(make_row(**kw))


def load(conn, records) -> int:
    return db.upsert_jobs(conn, records, CATEGORIES, CV)


def one(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()


def test_same_batch_twice_is_idempotent(pg):
    batch = [rec(**{"Job ID": str(i)}) for i in (1, 2, 3)]
    assert load(pg, batch) == 3
    assert load(pg, batch) == 3  # a same-day re-run still counts as written
    assert one(pg, "SELECT count(*) FROM jobs")[0] == 3
    assert one(pg, "SELECT count(*) FROM job_skills")[0] == 9  # 3 skills each, not doubled


def test_newer_run_updates_but_keeps_first_seen(pg):
    load(pg, [rec(**{"Match Score (/100)": 50})])
    pg.execute("UPDATE jobs SET updated_at = now() - interval '1 day'")
    before = one(pg, "SELECT updated_at FROM jobs")[0]
    assert load(pg, [rec(**{"Run Date": "2026-09-30", "Match Score (/100)": 80})]) == 1
    score, first, last, updated = one(pg, "SELECT match_score, first_seen, last_seen, updated_at FROM jobs")
    assert (score, first, last) == (80, dt.date(2026, 9, 29), dt.date(2026, 9, 30))
    assert updated > before


def test_older_run_is_ignored_but_moves_first_seen(pg):
    load(pg, [rec(**{"Match Score (/100)": 80})])
    assert load(pg, [rec(**{"Run Date": "2026-09-20", "Match Score (/100)": 10, "Skills To Learn": ""})]) == 0
    score, first, last = one(pg, "SELECT match_score, first_seen, last_seen FROM jobs")
    assert (score, first, last) == (80, dt.date(2026, 9, 20), dt.date(2026, 9, 29))
    assert one(pg, "SELECT count(*) FROM job_skills")[0] == 3  # old skill list didn't replace the new one


def test_changed_skills_replace_job_skills(pg):
    load(pg, [rec()])
    load(pg, [rec(**{"Skills You Have": "Python", "Skills To Learn": "Kubernetes"})])
    names = pg.execute("SELECT s.name FROM job_skills js JOIN skills s USING (skill_id) ORDER BY 1").fetchall()
    assert [n for (n,) in names] == ["Kubernetes", "Python"]


def test_types_round_trip(pg):
    load(pg, [rec(**{"Region": "Türkiye", "Location": "İstanbul, Türkiye", "Work Type": "",
                     "Location Restrictions": "EU only, US only", "Red Flags": "Unpaid",
                     "Open To You?": "No", "Salary": "₺40.000"})])
    row = one(pg, "SELECT region, location, work_type, restrictions, red_flags, open_to_you, "
                  "date_posted, salary, years_required FROM jobs")
    assert row == ("Türkiye", "İstanbul, Türkiye", None, ["EU only", "US only"], ["Unpaid"], False,
                   dt.date(2026, 9, 28), "₺40.000", None)


def test_skill_category_and_on_cv(pg):
    load(pg, [rec(**{"Skills To Learn": "Docker, Made Up Skill"})])
    rows = dict((n, (c, cv)) for n, c, cv in pg.execute("SELECT name, category, on_cv FROM skills"))
    assert rows["Made Up Skill"] == ("Unknown", False)
    assert rows["Python"] == (CATEGORIES["Python"], True)
    assert rows["Docker"] == (CATEGORIES["Docker"], False)


def test_failed_load_rolls_back_everything(pg):
    import psycopg
    bad = rec(**{"Job ID": "2"})
    bad["source"] = "bogus"  # violates the CHECK on jobs.source
    with pytest.raises(psycopg.errors.CheckViolation):
        with pg.transaction():  # stands in for the caller's `with connect(url) as conn:`
            load(pg, [rec(**{"Job ID": "1"}), bad])
    for table in ("jobs", "job_skills", "skills"):
        assert one(pg, f"SELECT count(*) FROM {table}")[0] == 0, table


def test_record_run(pg):
    db.record_run(pg, "backfill", dt.date(2026, 9, 29), 10, 7)
    assert one(pg, "SELECT kind, run_date, rows_offered, rows_written FROM runs") == \
        ("backfill", dt.date(2026, 9, 29), 10, 7)
