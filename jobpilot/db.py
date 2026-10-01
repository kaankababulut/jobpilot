"""Writes job records into Postgres with idempotent upserts.
Doesn't import job_searcher; the caller passes SKILLS categories and cv_skills in.
Transactions are the caller's job: `with connect(url) as conn:` commits when the block
succeeds and rolls back on any error, so a failed load never leaves half a batch behind."""
import datetime as dt
import os
from typing import Callable
from urllib.parse import unquote, urlsplit

from jobpilot.records import row_to_record

# every jobs column the loader writes, except first_seen/last_seen (both come from run_date)
JOB_COLS = ("source", "source_id", "region", "title", "company", "location", "work_type", "open_to_you",
            "restrictions", "red_flags", "date_posted", "employment_type", "seniority", "salary", "apply_url",
            "match_score", "years_required", "description")
KEY_COLS = ("source", "source_id")

# ON CONFLICT ... WHERE: only data at least as new as what's stored may overwrite it,
# so a backfill of old files after daily loads can't roll values back. Same-day re-runs still update.
UPSERT_SQL = (
    f"INSERT INTO jobs ({', '.join(JOB_COLS)}, first_seen, last_seen) "
    f"VALUES ({', '.join(f'%({c})s' for c in JOB_COLS)}, %(run_date)s, %(run_date)s) "
    f"ON CONFLICT (source, source_id) DO UPDATE SET "
    + ", ".join(f"{c} = EXCLUDED.{c}" for c in JOB_COLS if c not in KEY_COLS)
    + ", last_seen = EXCLUDED.last_seen, updated_at = now() "
    "WHERE jobs.last_seen <= EXCLUDED.last_seen RETURNING id"
)
# an older file can still prove we saw the job earlier than we thought
FIRST_SEEN_SQL = "UPDATE jobs SET first_seen = LEAST(first_seen, %s) WHERE source = %s AND source_id = %s"


def connect(url: str):
    import psycopg  # lazy, so a missing driver can't break `import job_searcher`
    # timeouts so a stopped container or a stuck lock fails fast instead of hanging the 12:00 run
    return psycopg.connect(url, connect_timeout=5, options="-c statement_timeout=30000")


def prepare(rows: list[dict], log: Callable[[str], None]) -> list[dict]:
    """Excel rows -> records, skipping bad rows and keeping one record per (source, source_id)."""
    latest: dict[tuple[str, str], dict] = {}
    skipped = 0
    for row in rows:
        try:
            rec = row_to_record(row)
        except ValueError:
            skipped += 1
            log(f"  Skipped row with bad identity fields (Job ID {row.get('Job ID')!r})")
            continue
        key = (rec["source"], rec["source_id"])
        # keep only the newest copy, so each job is written once and `written` counts jobs, not rows
        if key not in latest or rec["run_date"] >= latest[key]["run_date"]:
            latest[key] = rec
    if skipped:
        log(f"  Skipped {skipped} row(s) in total")
    return list(latest.values())


def upsert_jobs(conn, records: list[dict], categories: dict[str, str], cv_skills: set[str]) -> int:
    """Upserts skills, jobs and job_skills; returns how many jobs were inserted or updated."""
    names = sorted({s for r in records for s in r["skills"]})
    skill_ids: dict[str, int] = {}
    with conn.cursor() as cur:
        if names:
            # category and on_cv are refreshed every load, so config.json edits show up in the DB
            cur.executemany(
                "INSERT INTO skills (name, category, on_cv) VALUES (%s, %s, %s) "
                "ON CONFLICT (name) DO UPDATE SET category = EXCLUDED.category, on_cv = EXCLUDED.on_cv",
                [(n, categories.get(n, "Unknown"), n in cv_skills) for n in names])
            cur.execute("SELECT name, skill_id FROM skills WHERE name = ANY(%s)", (names,))
            skill_ids = dict(cur.fetchall())
        written = 0
        for rec in records:
            cur.execute(UPSERT_SQL, rec)
            row = cur.fetchone()
            if row is None:
                # update skipped (stored data is newer); first_seen may still need to move earlier.
                # Not needed on the write path: there first_seen <= last_seen <= run_date already.
                cur.execute(FIRST_SEEN_SQL, (rec["run_date"], rec["source"], rec["source_id"]))
                continue
            job_id = row[0]
            # replace rather than merge, so a skill dropped from the posting disappears
            cur.execute("DELETE FROM job_skills WHERE job_id = %s", (job_id,))
            if rec["skills"]:
                cur.executemany("INSERT INTO job_skills (job_id, skill_id) VALUES (%s, %s)",
                                [(job_id, skill_ids[s]) for s in rec["skills"]])
            written += 1
    return written


def record_run(conn, kind: str, run_date, offered: int, written: int) -> None:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO runs (kind, run_date, rows_offered, rows_written) VALUES (%s, %s, %s, %s)",
                    (kind, run_date, offered, written))


def redact(e: Exception, url: str) -> str:
    """One-line `Type: message` for an error, with the URL and its password hidden."""
    msg = (str(e).splitlines() or [""])[0]
    if not url:  # "".replace inserts the marker between every character
        return f"{type(e).__name__}: {msg}"
    msg = msg.replace(url, "<DATABASE_URL>")
    try:
        password = urlsplit(url).password
    except ValueError:  # malformed URL; the full-URL replace above still applies
        password = None
    if password:  # driver messages can echo connection details; never let the password reach the log
        # the driver may show it %-decoded (p%40ss -> p@ss), so hide both forms
        for form in {password, unquote(password)}:
            msg = msg.replace(form, "***")
    return f"{type(e).__name__}: {msg}"


def safe_load(rows: list[dict], run_date, categories: dict[str, str], cv_skills: set[str],
              log: Callable[[str], None]) -> bool:
    """Loads rows into Postgres in one transaction; logs and returns False instead of raising,
    so a database problem can never break the Excel run."""
    def say(msg: str) -> None:
        # the log writes to the console and logs/run.log, which can fail (locked file, non-UTF-8
        # console); a failed log line must not break the Excel run either
        try:
            log(msg)
        except Exception:
            pass

    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        say("Postgres load skipped: DATABASE_URL not set")
        return False
    # try sits outside the with: if the except were inside, the with-block would exit cleanly
    # and psycopg would commit a partial batch instead of rolling it back
    try:
        if isinstance(run_date, str):
            run_date = dt.date.fromisoformat(run_date)  # runs.run_date is a DATE column
        with connect(url) as conn:
            written = upsert_jobs(conn, prepare(rows, log), categories, cv_skills)
            record_run(conn, "daily", run_date, len(rows), written)
    except Exception as e:  # not BaseException, so Ctrl+C still stops the run
        say(f"WARNING: Postgres load skipped: {redact(e, url)}")
        return False
    say(f"Postgres: wrote {written} of {len(rows)} jobs")
    return True
