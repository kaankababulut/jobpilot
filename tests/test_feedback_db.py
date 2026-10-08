"""Integration tests for jobpilot.feedback against the local Postgres (pytest -m db).
Every write runs as the jobpilot_feedback role (as_role), so passing tests also prove 004's grants
are enough for the webhook, ON CONFLICT paths included. Seeding and checks run as the fixture's owner."""
import datetime as dt

import pytest

from jobpilot import feedback as fb
from test_db import load, one, rec
from test_feedback_schema_db import FEEDBACK_ROLE, as_role

pytestmark = pytest.mark.db


def seed(conn, job_id: str = "1", **kw) -> int:
    load(conn, [rec(**{"Job ID": job_id, **kw})])
    return one(conn, "SELECT id FROM jobs WHERE source_id = %s", (job_id,))[0]


def events(conn, app_id: int) -> list[tuple]:
    return conn.execute("SELECT status, note FROM application_events WHERE application_id = %s ORDER BY id",
                        (app_id,)).fetchall()


def test_set_label_latest_wins_and_freezes_the_score(pg):
    job = seed(pg, **{"Match Score (/100)": 72})
    with as_role(pg, FEEDBACK_ROLE):
        assert fb.set_label(pg, job, "up") == (True, 72)
    pg.execute("UPDATE jobs SET match_score = 40 WHERE id = %s", (job,))  # a later re-score
    with as_role(pg, FEEDBACK_ROLE):
        assert fb.set_label(pg, job, "down") == (True, 40)  # the update path refreshes the frozen score too
    assert pg.execute("SELECT label, score_at_label, source FROM feedback").fetchall() == [("down", 40, "telegram")]


def test_set_label_unknown_job_writes_nothing(pg):
    with as_role(pg, FEEDBACK_ROLE):
        assert fb.set_label(pg, 999999, "up") == (False, None)
    assert one(pg, "SELECT count(*) FROM feedback")[0] == 0


def test_set_label_null_score_is_still_found(pg):
    job = seed(pg)
    pg.execute("UPDATE jobs SET match_score = NULL WHERE id = %s", (job,))
    with as_role(pg, FEEDBACK_ROLE):
        assert fb.set_label(pg, job, "up") == (True, None)
    assert one(pg, "SELECT label, score_at_label FROM feedback") == ("up", None)


def test_set_label_rejects_unknown_label(pg):
    job = seed(pg)
    with pytest.raises(ValueError):
        fb.set_label(pg, job, "meh")


def test_mark_applied_creates_once(pg):
    job = seed(pg)
    with as_role(pg, FEEDBACK_ROLE):
        app, created = fb.mark_applied(pg, job)
        assert created
        assert fb.mark_applied(pg, job) == (app, False)  # second tap: same application, no new event
    assert one(pg, "SELECT job_id, company, title, url, status, source FROM applications") == (
        job, "Acme", "Junior Python Developer", "https://www.linkedin.com/jobs/view/4012345678/", "applied", "telegram")
    assert events(pg, app) == [("applied", None)]


def test_mark_applied_unknown_job(pg):
    with as_role(pg, FEEDBACK_ROLE):
        assert fb.mark_applied(pg, 999999) is None
    assert one(pg, "SELECT count(*) FROM applications")[0] == 0


def test_mark_applied_finds_a_manual_row_from_today(pg):
    job = seed(pg)
    with as_role(pg, FEEDBACK_ROLE):
        manual, _ = fb.add_application(pg, "Acme", "Junior Python Developer", None)
        assert fb.mark_applied(pg, job) == (manual, False)  # (company, title, applied_on) clash: not an error
        assert fb.mark_applied(pg, job) == (manual, False)  # now found by job_id
    assert one(pg, "SELECT count(*), min(job_id) FROM applications") == (1, job)  # linked to the job
    assert one(pg, "SELECT label FROM labelled_jobs WHERE job_id = %s", (job,))[0] == "up"
    assert len(events(pg, manual)) == 1  # linking isn't a new application


def test_mark_applied_never_moves_a_row_linked_to_another_job(pg):
    first, second = seed(pg, "1"), seed(pg, "2")  # same company and title, two postings
    with as_role(pg, FEEDBACK_ROLE):
        app, _ = fb.mark_applied(pg, first)
        assert fb.mark_applied(pg, second) == (app, False)
    assert one(pg, "SELECT job_id FROM applications WHERE id = %s", (app,))[0] == first


def test_add_application_is_idempotent(pg):
    with as_role(pg, FEEDBACK_ROLE):
        app, created = fb.add_application(pg, "Globex", "Data Intern", "https://globex.example/jobs/1", "via a friend")
        assert created
        assert fb.add_application(pg, "Globex", "Data Intern", None) == (app, False)
    assert one(pg, "SELECT url, notes, source, job_id FROM applications") == (
        "https://globex.example/jobs/1", "via a friend", "manual", None)
    assert events(pg, app) == [("applied", None)]


def test_set_status_logs_an_event(pg):
    with as_role(pg, FEEDBACK_ROLE):
        app, _ = fb.add_application(pg, "Globex", "Data Intern", None)
        assert fb.set_status(pg, app, "interview", "Tuesday 10:00")
    assert one(pg, "SELECT status FROM applications")[0] == "interview"
    assert events(pg, app) == [("applied", None), ("interview", "Tuesday 10:00")]


def test_set_status_bad_status_changes_nothing(pg):
    app, _ = fb.add_application(pg, "Globex", "Data Intern", None)
    with pytest.raises(ValueError):
        fb.set_status(pg, app, "ghosted")
    assert one(pg, "SELECT status FROM applications")[0] == "applied"
    assert len(events(pg, app)) == 1


def test_set_status_unknown_id(pg):
    with as_role(pg, FEEDBACK_ROLE):
        assert fb.set_status(pg, 999999, "offer") is False
    assert one(pg, "SELECT count(*) FROM application_events")[0] == 0


def test_open_applications_filters_closed_and_orders_newest_first(pg):
    with as_role(pg, FEEDBACK_ROLE):
        ids = {name: fb.add_application(pg, name, "Intern", None)[0] for name in ("A", "B", "C", "D")}
        fb.set_status(pg, ids["C"], "rejected")
        fb.set_status(pg, ids["D"], "assessment")
    pg.execute("UPDATE applications SET applied_on = %s WHERE id = %s", (dt.date(2026, 1, 1), ids["A"]))
    with as_role(pg, FEEDBACK_ROLE):
        rows = fb.open_applications(pg)
        assert fb.open_applications(pg, limit=1) == rows[:1]
    assert [r["company"] for r in rows] == ["D", "B", "A"]  # same day: newer id first; C is closed
    assert set(rows[0]) == {"id", "company", "title", "status", "applied_on"}
    assert rows[0]["status"] == "assessment"
