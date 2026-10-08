"""Integration tests for the spreadsheet import against the local Postgres (pytest -m db).
Writes run as jobpilot_feedback, so passing tests also prove the import needs no more than 004 grants."""
import datetime as dt

import pytest

from jobpilot import feedback as fb
from jobpilot import import_applications as imp
from test_db import one
from test_feedback_db import events, seed
from test_feedback_schema_db import FEEDBACK_ROLE, as_role

pytestmark = pytest.mark.db

TODAY = dt.date(2026, 10, 8)
LINKEDIN_ID = "4012345678"  # the job id in the default apply link of test_records.make_row
HEADER = ("Date", "Company", "Job title", "Link", "Score", "Status", "Notes")
SHEET = [
    HEADER,
    ("2026-09-01", "Globex", "Data Intern", "https://globex.example/jobs/1", 80, "Applied", "via a friend"),
    ("02.09.2026", "Initech", "QA Intern", None, None, "coding test", None),
    ("2026-09-03", "Umbrella", "Backend Intern", None, None, "ghosted", None),  # an error row
]


def run(conn, sheet=SHEET, dry_run: bool = False) -> dict[str, int]:
    rows, _ = imp.parse_rows(sheet, TODAY)
    with as_role(conn, FEEDBACK_ROLE):
        return imp.import_rows(conn, rows, dry_run, log=lambda msg: None)


def test_import_inserts_with_the_sheet_date_and_source(pg):
    assert run(pg) == {"inserted": 2, "present": 0, "matched": 0}
    assert pg.execute("SELECT company, title, url, status, applied_on, notes, source, job_id "
                      "FROM applications ORDER BY id").fetchall() == [
        ("Globex", "Data Intern", "https://globex.example/jobs/1", "applied", dt.date(2026, 9, 1), "via a friend",
         "import", None),
        ("Initech", "QA Intern", None, "assessment", dt.date(2026, 9, 2), None, "import", None)]


def test_rerun_adds_nothing(pg):
    run(pg)
    assert run(pg) == {"inserted": 0, "present": 2, "matched": 0}
    assert one(pg, "SELECT count(*) FROM applications")[0] == 2
    assert one(pg, "SELECT count(*) FROM application_events")[0] == 3  # 2 applied + 1 assessment, once


def test_status_other_than_applied_logs_an_event_and_applied_is_dated(pg):
    run(pg)
    app = one(pg, "SELECT id FROM applications WHERE company = 'Initech'")[0]
    assert events(pg, app) == [("applied", None), ("assessment", None)]
    applied_at = one(pg, "SELECT at::date FROM application_events WHERE application_id = %s AND status = 'applied'",
                     (app,))[0]
    assert applied_at == dt.date(2026, 9, 2)  # the day in the sheet, not the day of the import


def test_rerun_never_overwrites_a_status_the_bot_moved_on(pg):
    run(pg)
    app = one(pg, "SELECT id FROM applications WHERE company = 'Initech'")[0]
    fb.set_status(pg, app, "interview")
    run(pg)
    assert one(pg, "SELECT status FROM applications WHERE id = %s", (app,))[0] == "interview"


def test_linkedin_link_matches_the_job_and_counts_as_up(pg):
    job = seed(pg, LINKEDIN_ID)
    sheet = [HEADER, ("2026-09-05", "Acme", "Junior Python Developer",
                      "https://www.linkedin.com/jobs/search/?currentJobId=4012345678&keywords=python")]
    assert run(pg, sheet) == {"inserted": 1, "present": 0, "matched": 1}
    assert one(pg, "SELECT job_id FROM applications")[0] == job
    assert one(pg, "SELECT label FROM labelled_jobs WHERE job_id = %s", (job,))[0] == "up"


def test_exact_url_match_for_other_sources(pg):
    job = seed(pg, "https://himalayas.app/jobs/globex/data-intern",
               **{"Direct Application Link": "https://globex.example/apply/7"})
    sheet = [HEADER, (None, "Globex", "Data Intern", "https://globex.example/apply/7")]
    assert run(pg, sheet)["matched"] == 1
    assert one(pg, "SELECT job_id FROM applications")[0] == job


def test_unknown_link_leaves_job_id_null(pg):
    seed(pg, LINKEDIN_ID)
    sheet = [HEADER, (None, "Acme", "Junior Python Developer", "https://www.linkedin.com/jobs/view/4099999999/")]
    assert run(pg, sheet)["matched"] == 0
    assert one(pg, "SELECT job_id FROM applications")[0] is None


def test_job_already_applied_via_the_bot_is_present_not_duplicated(pg):
    job = seed(pg, LINKEDIN_ID)
    with as_role(pg, FEEDBACK_ROLE):
        app, _ = fb.mark_applied(pg, job)
    sheet = [HEADER, ("2026-09-05", "Acme", "Junior Python Developer", "https://www.linkedin.com/jobs/view/4012345678/")]
    assert run(pg, sheet) == {"inserted": 0, "present": 1, "matched": 1}
    assert one(pg, "SELECT count(*), min(id) FROM applications") == (1, app)


def test_rerun_with_a_link_added_links_the_existing_row(pg):
    job = seed(pg, LINKEDIN_ID)
    run(pg, [HEADER, ("2026-09-05", "Acme", "Junior Python Developer")])
    run(pg, [HEADER, ("2026-09-05", "Acme", "Junior Python Developer", "https://www.linkedin.com/jobs/view/4012345678/")])
    assert one(pg, "SELECT count(*), min(job_id) FROM applications") == (1, job)


def test_dry_run_leaves_nothing(pg):
    assert run(pg, dry_run=True) == {"inserted": 2, "present": 0, "matched": 0}
    assert one(pg, "SELECT count(*) FROM applications")[0] == 0
    assert one(pg, "SELECT count(*) FROM application_events")[0] == 0


def test_a_failing_row_rolls_back_the_whole_import(pg, monkeypatch):
    real = fb.import_application

    def fail_on_initech(conn, company, *args):
        if company == "Initech":
            raise RuntimeError("boom")
        return real(conn, company, *args)
    monkeypatch.setattr(imp.feedback, "import_application", fail_on_initech)
    with pytest.raises(RuntimeError):
        run(pg)
    assert one(pg, "SELECT count(*) FROM applications")[0] == 0  # Globex, written first, is gone too
