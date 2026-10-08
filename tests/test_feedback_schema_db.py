"""Integration tests for migration 004 (feedback, applications, labelled_jobs, jobpilot_feedback role).
Needs the local Postgres (pytest -m db). The pg fixture has already applied every migration to its
throwaway schema. Role checks use SET ROLE on the fixture's owner connection; `as_role` always resets
it, so a failing assertion can't leave the fixture's DROP SCHEMA running as a restricted role."""
from contextlib import contextmanager

import pytest
from psycopg import errors

from jobpilot import migrate
from test_db import one
from test_migrate_db import empty, recorded, table_exists  # noqa: F401 (empty is a fixture)

pytestmark = pytest.mark.db

FEEDBACK_ROLE = "jobpilot_feedback"
NEW_TABLES = ("feedback", "applications", "application_events")


@contextmanager
def as_role(conn, role: str):
    conn.execute(f"SET ROLE {role}")
    try:
        yield conn
    finally:
        conn.execute("RESET ROLE")


def add_job(conn, source_id: str, title: str = "Junior Python Developer") -> int:
    return one(conn, "INSERT INTO jobs (source, source_id, title, company, first_seen, last_seen, match_score) "
                     "VALUES ('linkedin', %s, %s, 'Acme', current_date, current_date, 70) RETURNING id",
               (source_id, title))[0]


def add_application(conn, company: str = "Acme", title: str = "Intern", job_id: int | None = None,
                    source: str = "manual") -> int:
    return one(conn, "INSERT INTO applications (job_id, company, title, source) VALUES (%s, %s, %s, %s) RETURNING id",
               (job_id, company, title, source))[0]


def upsert_feedback(conn, job_id: int, label: str, score: int | None = None) -> None:
    # the shape the Telegram bot will use: one row per job, the latest label wins
    conn.execute("INSERT INTO feedback (job_id, label, score_at_label, source) VALUES (%s, %s, %s, 'telegram') "
                 "ON CONFLICT (job_id) DO UPDATE SET label = EXCLUDED.label, "
                 "score_at_label = EXCLUDED.score_at_label, updated_at = now()", (job_id, label, score))


def labels(conn) -> dict[int, str]:
    return dict(conn.execute("SELECT job_id, label FROM labelled_jobs").fetchall())


def test_all_four_objects_exist(pg):
    for name in NEW_TABLES + ("labelled_jobs",):
        assert table_exists(pg, name), name
    assert ("004", "feedback_applications") in recorded(pg)


def test_feedback_latest_label_wins(pg):
    job = add_job(pg, "1")
    with as_role(pg, FEEDBACK_ROLE):
        upsert_feedback(pg, job, "up", 70)
        upsert_feedback(pg, job, "down", 65)
    assert pg.execute("SELECT label, score_at_label FROM feedback").fetchall() == [("down", 65)]


def test_feedback_rejects_unknown_label(pg):
    job = add_job(pg, "1")
    with pytest.raises(errors.CheckViolation):
        upsert_feedback(pg, job, "meh")


def test_applications_unique_on_company_title_date(pg):
    add_application(pg)
    with pytest.raises(errors.UniqueViolation):
        add_application(pg)  # same company, title and today's date: the import re-run case
    add_application(pg, title="Data Intern")  # a different title is a different application


def test_one_application_per_job_but_many_without_one(pg):
    job = add_job(pg, "1")
    add_application(pg, title="A", job_id=job)
    with pytest.raises(errors.UniqueViolation):
        add_application(pg, title="B", job_id=job)
    add_application(pg, title="C")
    add_application(pg, title="D")  # NULL job_id twice is fine: the index is partial
    assert one(pg, "SELECT count(*) FROM applications")[0] == 3


def test_application_status_check(pg):
    app = add_application(pg)
    assert one(pg, "SELECT status FROM applications WHERE id = %s", (app,))[0] == "applied"  # the default
    pg.execute("UPDATE applications SET status = 'assessment' WHERE id = %s", (app,))
    with pytest.raises(errors.CheckViolation):
        pg.execute("UPDATE applications SET status = 'ghosted' WHERE id = %s", (app,))


def test_labelled_jobs_combines_feedback_and_applications(pg):
    applied, overruled, unlabelled, thumbs = (add_job(pg, str(i)) for i in range(4))
    add_application(pg, title="A", job_id=applied)
    add_application(pg, title="B", job_id=overruled)
    upsert_feedback(pg, overruled, "down")  # an explicit label beats "I applied"
    upsert_feedback(pg, thumbs, "up", 70)
    assert labels(pg) == {applied: "up", overruled: "down", thumbs: "up"}  # unlabelled is absent
    assert unlabelled not in labels(pg)


def test_feedback_role_writes_its_three_tables_without_sequence_grants(pg):
    job = add_job(pg, "1")
    seq = one(pg, "SELECT pg_get_serial_sequence('applications', 'id')")[0]
    # identity ids come from an internal sequence that INSERT doesn't permission-check
    assert not one(pg, "SELECT has_sequence_privilege(%s, %s, 'USAGE')", (FEEDBACK_ROLE, seq))[0]
    with as_role(pg, FEEDBACK_ROLE):
        app = add_application(pg, job_id=job, source="telegram")
        pg.execute("INSERT INTO application_events (application_id, status) VALUES (%s, 'applied')", (app,))
        pg.execute("UPDATE applications SET status = 'interview', updated_at = now() WHERE id = %s", (app,))
        pg.execute("UPDATE application_events SET note = 'call booked' WHERE application_id = %s", (app,))
        upsert_feedback(pg, job, "up")
        pg.execute("UPDATE feedback SET score_at_label = 71 WHERE job_id = %s", (job,))
        assert one(pg, "SELECT title FROM jobs WHERE id = %s", (job,))[0] == "Junior Python Developer"
    assert one(pg, "SELECT status FROM applications WHERE id = %s", (app,))[0] == "interview"


@pytest.mark.parametrize("table", NEW_TABLES)
def test_feedback_role_cannot_delete(pg, table):
    with as_role(pg, FEEDBACK_ROLE), pytest.raises(errors.InsufficientPrivilege):
        pg.execute(f"DELETE FROM {table}")


@pytest.mark.parametrize("sql", [
    "INSERT INTO jobs (source, source_id, title, first_seen, last_seen) VALUES ('linkedin', 'x', 't', current_date, current_date)",
    "UPDATE jobs SET title = 'x'",
    "INSERT INTO skills (name, category) VALUES ('x', 'y')",
    "UPDATE skills SET on_cv = true",
    "INSERT INTO runs (kind, run_date, rows_offered, rows_written) VALUES ('daily', current_date, 0, 0)",
    "UPDATE runs SET rows_written = 0",
])
def test_feedback_role_cannot_write_the_loader_tables(pg, sql):
    with as_role(pg, FEEDBACK_ROLE), pytest.raises(errors.InsufficientPrivilege):
        pg.execute(sql)


def test_feedback_role_has_no_login_and_a_connection_limit(pg):
    assert one(pg, "SELECT rolcanlogin, rolconnlimit, rolsuper FROM pg_roles WHERE rolname = %s",
               (FEEDBACK_ROLE,)) == (False, 3, False)


def test_api_role_reads_the_new_tables_and_view_but_cannot_write(pg):
    job = add_job(pg, "1")
    upsert_feedback(pg, job, "up")
    with as_role(pg, "jobpilot_api"):
        for t in NEW_TABLES:
            pg.execute(f"SELECT * FROM {t}").fetchall()
        # the view reads jobs/feedback/applications with its owner's rights, and the API may read those anyway
        assert one(pg, "SELECT label FROM labelled_jobs WHERE job_id = %s", (job,))[0] == "up"
        with pytest.raises(errors.InsufficientPrivilege):
            pg.execute("INSERT INTO feedback (job_id, label, source) VALUES (%s, 'down', 'import')", (job,))


def test_004_reapplies_cleanly_when_roles_already_exist(empty):
    # both roles are cluster-wide and already exist (the pg fixture created them), as on a second database
    assert one(empty, "SELECT count(*) FROM pg_roles WHERE rolname IN ('jobpilot_api', %s)", (FEEDBACK_ROLE,))[0] == 2
    migrate.apply(empty, migrate.DEFAULT_DIR)
    assert ("004", "feedback_applications") in recorded(empty)
    for t in NEW_TABLES:  # granted in this new schema too
        assert one(empty, "SELECT has_table_privilege(%s, quote_ident(current_schema()) || '.' || %s, 'INSERT')",
                   (FEEDBACK_ROLE, t))[0], t
