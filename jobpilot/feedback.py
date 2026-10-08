"""Write SQL for the Telegram webhook: thumbs up/down on jobs and the application tracker (migration 004).
Kept apart from queries.py, which stays read-only. Meant to run as the jobpilot_feedback role, which can
SELECT/INSERT/UPDATE feedback, applications and application_events and only read jobs.
Callers own the transaction: nothing here commits, so a function that writes two rows (an application
and its first event) is all-or-nothing inside the caller's `with conn.transaction():`.
Values only ever reach Postgres as %s parameters, never pasted into the SQL text."""
import datetime as dt

from psycopg.rows import dict_row

LABELS = ("up", "down")
# the same list as the CHECK in 004; the bot's command parser reuses it
STATUSES = ("applied", "assessment", "interview", "offer", "rejected", "withdrawn")
CLOSED = ("offer", "rejected", "withdrawn")  # finished, so no longer "open"


def set_label(conn, job_id: int, label: str) -> tuple[bool, int | None]:
    """Labels a job (latest label wins) and freezes its current match_score beside the label.
    Returns (found, score): (False, None) if no job has this id (nothing written), else (True, match_score).
    found is separate because match_score itself can be NULL, so None alone couldn't say "unknown job"."""
    if label not in LABELS:
        raise ValueError(f"label must be one of {LABELS}, not {label!r}")
    # INSERT ... SELECT: an unknown job id selects no row, so nothing is written and no FK error is raised
    row = conn.execute(
        "INSERT INTO feedback (job_id, label, score_at_label, source) "
        "SELECT id, %s, match_score, 'telegram' FROM jobs WHERE id = %s "
        "ON CONFLICT (job_id) DO UPDATE SET label = EXCLUDED.label, score_at_label = EXCLUDED.score_at_label, "
        "source = 'telegram', updated_at = now() RETURNING score_at_label", (label, job_id)).fetchone()
    return (True, row[0]) if row else (False, None)


def _add_event(conn, application_id: int, status: str, note: str | None = None) -> None:
    conn.execute("INSERT INTO application_events (application_id, status, note) VALUES (%s, %s, %s)",
                 (application_id, status, note))


def _existing(conn, company: str, title: str) -> int | None:
    # current_date on the server, the same clock the applied_on default and the unique key use
    row = conn.execute("SELECT id FROM applications WHERE company = %s AND title = %s AND applied_on = current_date",
                       (company, title)).fetchone()
    return row[0] if row else None


def mark_applied(conn, job_id: int) -> tuple[int, bool] | None:
    """Records an application for a JobPilot job. Returns (application_id, created); created is False
    when this job, or a manual/imported row with the same company and title today, is already tracked
    (that row is then linked to the job). None if no job has this id."""
    job = conn.execute("SELECT company, title, apply_url FROM jobs WHERE id = %s", (job_id,)).fetchone()
    if job is None:
        return None
    company, title, url = job
    company = company or "Unknown"  # jobs.company may be NULL, applications.company may not
    # no conflict target, so it covers both unique keys: the partial one on job_id and (company, title, applied_on)
    row = conn.execute("INSERT INTO applications (job_id, company, title, url, source) "
                       "VALUES (%s, %s, %s, %s, 'telegram') ON CONFLICT DO NOTHING RETURNING id",
                       (job_id, company, title, url)).fetchone()
    if row:
        _add_event(conn, row[0], "applied")
        return row[0], True
    found = conn.execute("SELECT id FROM applications WHERE job_id = %s", (job_id,)).fetchone()
    if found:
        return found[0], False
    app_id = _existing(conn, company, title)
    # a manual/imported row for the same job: link it, so it counts in labelled_jobs; job_id IS NULL so a
    # row already tied to another job (same company and title) is never moved
    conn.execute("UPDATE applications SET job_id = %s, updated_at = now() WHERE id = %s AND job_id IS NULL",
                 (job_id, app_id))
    return app_id, False


def add_application(conn, company: str, title: str, url: str | None, notes: str | None = None,
                    source: str = "manual") -> tuple[int, bool]:
    """An application made outside JobPilot (/add, or the one-off import). Returns (application_id, created);
    the same company and title on the same day is the same application, so re-running is safe."""
    row = conn.execute("INSERT INTO applications (company, title, url, notes, source) VALUES (%s, %s, %s, %s, %s) "
                       "ON CONFLICT (company, title, applied_on) DO NOTHING RETURNING id",
                       (company, title, url, notes, source)).fetchone()
    if row:
        _add_event(conn, row[0], "applied")
        return row[0], True
    return _existing(conn, company, title), False


def import_application(conn, company: str, title: str, url: str | None, applied_on: dt.date,
                       notes: str | None = None, job_id: int | None = None) -> tuple[int, bool]:
    """One spreadsheet row (jobpilot.import_applications). Like add_application, but with the real applied
    date and an optional matched job. Returns (application_id, created); created is False when the same
    company, title and date, or the same job, is already tracked, so re-running the import is safe."""
    # no conflict target: covers (company, title, applied_on) and the partial unique key on job_id
    row = conn.execute("INSERT INTO applications (job_id, company, title, url, applied_on, notes, source) "
                       "VALUES (%s, %s, %s, %s, %s, %s, 'import') ON CONFLICT DO NOTHING RETURNING id",
                       (job_id, company, title, url, applied_on, notes)).fetchone()
    if row:
        # dated on the day applied, not today, so "days from applied to interview" stays true later
        conn.execute("INSERT INTO application_events (application_id, status, at) VALUES (%s, 'applied', %s)",
                     (row[0], applied_on))
        return row[0], True
    if job_id is not None:
        found = conn.execute("SELECT id FROM applications WHERE job_id = %s", (job_id,)).fetchone()
        if found:
            return found[0], False
    found = conn.execute("SELECT id FROM applications WHERE company = %s AND title = %s AND applied_on = %s",
                         (company, title, applied_on)).fetchone()
    if job_id is not None:  # link a manual row to the job, as mark_applied does; never move a linked one
        conn.execute("UPDATE applications SET job_id = %s, updated_at = now() WHERE id = %s AND job_id IS NULL",
                     (job_id, found[0]))
    return found[0], False


def set_status(conn, application_id: int, status: str, note: str | None = None) -> bool:
    """Moves an application to a new status and logs it in application_events. False for an unknown id.
    The same status again without a note changes nothing, so a repeated /s doesn't log a second event."""
    if status not in STATUSES:  # before any SQL, so a typo never reaches the database
        raise ValueError(f"status must be one of {STATUSES}, not {status!r}")
    # a note is news even when the status is the same, so only a note-less repeat is skipped
    row = conn.execute("UPDATE applications SET status = %s, updated_at = now() "
                       "WHERE id = %s AND (status IS DISTINCT FROM %s OR %s) RETURNING id",
                       (status, application_id, status, note is not None)).fetchone()
    if row is None:  # unknown id, or nothing new
        return conn.execute("SELECT 1 FROM applications WHERE id = %s", (application_id,)).fetchone() is not None
    _add_event(conn, application_id, status, note)
    return True


def open_applications(conn, limit: int = 20) -> list[dict]:
    """Applications still in progress, newest first."""
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute("SELECT id, company, title, status, applied_on FROM applications "
                           "WHERE status <> ALL(%s) ORDER BY applied_on DESC, id DESC LIMIT %s",
                           (list(CLOSED), limit)).fetchall()
