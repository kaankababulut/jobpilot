"""Tests for jobpilot.db.safe_load (no database needed: connect and the loaders are faked)."""
import datetime as dt
import importlib
import sys

import pytest

from jobpilot import db

URL = "postgresql://jobs:s3cretPass@127.0.0.1:5432/jobs"


class FakeConn:
    """Stands in for a psycopg connection; records what its with-block exit saw."""
    def __init__(self):
        self.exit_args = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.exit_args = (exc_type, exc)
        return False  # like psycopg: never swallow the error


def test_missing_url_skips_without_connecting(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(db, "connect", lambda url: pytest.fail("connect should not be called"))
    lines = []
    assert db.safe_load([], "2026-10-01", {}, set(), lines.append) is False
    assert lines == ["Postgres load skipped: DATABASE_URL not set"]


def test_blank_url_counts_as_missing(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "   ")
    monkeypatch.setattr(db, "connect", lambda url: pytest.fail("connect should not be called"))
    lines = []
    assert db.safe_load([], "2026-10-01", {}, set(), lines.append) is False
    assert "not set" in lines[0]


def test_connect_error_is_logged_without_url_or_password(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)

    def boom(url):
        raise RuntimeError(f"could not connect to {url} (password s3cretPass rejected)\nsecond line")
    monkeypatch.setattr(db, "connect", boom)
    lines = []
    assert db.safe_load([], "2026-10-01", {}, set(), lines.append) is False
    assert len(lines) == 1 and lines[0].startswith("WARNING: Postgres load skipped: RuntimeError:")
    assert URL not in lines[0] and "s3cretPass" not in lines[0]
    assert "second line" not in lines[0]


def test_happy_path_writes_and_records_run(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)
    conn, runs = FakeConn(), []
    monkeypatch.setattr(db, "connect", lambda url: conn)
    monkeypatch.setattr(db, "prepare", lambda rows, log: ["r1", "r2"])
    monkeypatch.setattr(db, "upsert_jobs", lambda c, recs, cats, cv: len(recs))
    monkeypatch.setattr(db, "record_run", lambda c, *args: runs.append((c, *args)))
    lines = []
    assert db.safe_load([{}, {}, {}], "2026-10-01", {}, set(), lines.append) is True
    assert runs == [(conn, "daily", dt.date(2026, 10, 1), 3, 2)]
    assert lines == ["Postgres: wrote 2 of 3 jobs"]
    assert conn.exit_args == (None, None)  # clean exit, so psycopg would commit


def test_error_inside_load_reaches_the_with_block(monkeypatch):
    # guards the try-outside-with rule: the connection must see the error so it rolls back
    monkeypatch.setenv("DATABASE_URL", URL)
    conn = FakeConn()
    monkeypatch.setattr(db, "connect", lambda url: conn)
    monkeypatch.setattr(db, "prepare", lambda rows, log: ["r1"])

    def fail(*args):
        raise ValueError("bad row")
    monkeypatch.setattr(db, "upsert_jobs", fail)
    monkeypatch.setattr(db, "record_run", lambda *args: pytest.fail("record_run should not be called"))
    lines = []
    assert db.safe_load([{}], "2026-10-01", {}, set(), lines.append) is False
    assert conn.exit_args[0] is ValueError
    assert lines == ["WARNING: Postgres load skipped: ValueError: bad row"]


def test_import_job_searcher_without_psycopg(monkeypatch):
    # monkeypatch restores sys.modules entries (and jobpilot.db) afterwards, so other tests keep the real modules
    import jobpilot
    monkeypatch.setattr(jobpilot, "db", db)
    for name in ("job_searcher", "jobpilot.db"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "psycopg", None)  # None makes `import psycopg` raise ImportError
    importlib.import_module("job_searcher")
    importlib.import_module("jobpilot.db")


def test_import_job_searcher_without_dotenv(monkeypatch):
    # the scheduler's Python may lack python-dotenv; the Excel run must still start
    monkeypatch.delitem(sys.modules, "job_searcher", raising=False)
    monkeypatch.setitem(sys.modules, "dotenv", None)
    js = importlib.import_module("job_searcher")
    assert js.load_dotenv("unused.env") is False  # the no-op fallback


def raising_log(msg):
    raise OSError("run.log is locked")


def test_raising_log_never_escapes_when_url_missing(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert db.safe_load([], "2026-10-01", {}, set(), raising_log) is False


def test_raising_log_never_escapes_on_connect_error(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)

    def boom(url):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(db, "connect", boom)
    assert db.safe_load([], "2026-10-01", {}, set(), raising_log) is False


def test_raising_log_never_escapes_on_success(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setattr(db, "connect", lambda url: FakeConn())
    monkeypatch.setattr(db, "prepare", lambda rows, log: [])
    monkeypatch.setattr(db, "upsert_jobs", lambda *args: 0)
    monkeypatch.setattr(db, "record_run", lambda *args: None)
    assert db.safe_load([], "2026-10-01", {}, set(), raising_log) is True


def test_encoded_password_is_hidden_in_both_forms(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://jobs:p%40ss@127.0.0.1:5432/jobs")

    def boom(url):
        raise RuntimeError("auth failed for password p@ss (raw p%40ss)")
    monkeypatch.setattr(db, "connect", boom)
    lines = []
    assert db.safe_load([], "2026-10-01", {}, set(), lines.append) is False
    assert len(lines) == 1 and "p@ss" not in lines[0] and "p%40ss" not in lines[0]


AZURE_URL = "postgresql://jobsadmin:Azur3Secret@jobpilot.postgres.database.azure.com:5432/jobs?sslmode=require"
AZURE = {"url_var": "AZURE_DATABASE_URL", "label": "Azure Postgres"}


def test_named_target_missing_url_skips_even_if_default_is_set(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)  # the local URL must not be used as a fallback
    monkeypatch.delenv("AZURE_DATABASE_URL", raising=False)
    monkeypatch.setattr(db, "connect", lambda url: pytest.fail("connect should not be called"))
    lines = []
    assert db.safe_load([], "2026-10-01", {}, set(), lines.append, **AZURE) is False
    assert lines == ["Azure Postgres load skipped: AZURE_DATABASE_URL not set"]


def test_named_target_reads_its_own_url_and_redacts_it(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("AZURE_DATABASE_URL", AZURE_URL)
    seen = []

    def boom(url):
        seen.append(url)
        raise RuntimeError(f"could not connect to {url} (password Azur3Secret rejected)")
    monkeypatch.setattr(db, "connect", boom)
    lines = []
    assert db.safe_load([], "2026-10-01", {}, set(), lines.append, **AZURE) is False
    assert seen == [AZURE_URL]
    assert len(lines) == 1 and lines[0].startswith("WARNING: Azure Postgres load skipped: RuntimeError:")
    assert AZURE_URL not in lines[0] and "Azur3Secret" not in lines[0]
    assert "firewall" not in lines[0]  # not a pg_hba-style rejection


def test_firewall_rejection_adds_hint(monkeypatch):
    monkeypatch.setenv("AZURE_DATABASE_URL", AZURE_URL)

    def boom(url):
        raise RuntimeError('connection failed: FATAL:  no pg_hba.conf entry for host "203.0.113.7", '
                           'user "jobsadmin", database "jobs", SSL encryption\nsecond line')
    monkeypatch.setattr(db, "connect", boom)
    lines = []
    assert db.safe_load([], "2026-10-01", {}, set(), lines.append, **AZURE) is False
    assert len(lines) == 1 and "\n" not in lines[0]
    assert lines[0].startswith("WARNING: Azure Postgres load skipped: RuntimeError:")
    assert lines[0].endswith("; if your IP changed, update the database firewall rule")
    assert "Azur3Secret" not in lines[0] and "second line" not in lines[0]


def test_not_allowed_rejection_adds_hint(monkeypatch):
    monkeypatch.setenv("AZURE_DATABASE_URL", AZURE_URL)

    def boom(url):
        raise RuntimeError("Client with IP address '203.0.113.7' is not allowed to connect to this server")
    monkeypatch.setattr(db, "connect", boom)
    lines = []
    assert db.safe_load([], "2026-10-01", {}, set(), lines.append, **AZURE) is False
    assert lines[0].endswith("update the database firewall rule")


def test_named_target_happy_path(monkeypatch):
    monkeypatch.setenv("AZURE_DATABASE_URL", AZURE_URL)
    seen = []
    monkeypatch.setattr(db, "connect", lambda url: seen.append(url) or FakeConn())
    monkeypatch.setattr(db, "prepare", lambda rows, log: ["r1"])
    monkeypatch.setattr(db, "upsert_jobs", lambda c, recs, cats, cv: len(recs))
    monkeypatch.setattr(db, "record_run", lambda *args: None)
    lines = []
    assert db.safe_load([{}, {}], "2026-10-01", {}, set(), lines.append, **AZURE) is True
    assert seen == [AZURE_URL]
    assert lines == ["Azure Postgres: wrote 1 of 2 jobs"]


def test_named_target_never_raises_with_raising_log(monkeypatch):
    monkeypatch.setenv("AZURE_DATABASE_URL", AZURE_URL)

    def boom(url):
        raise RuntimeError("no pg_hba.conf entry for host")
    monkeypatch.setattr(db, "connect", boom)
    assert db.safe_load([], "2026-10-01", {}, set(), raising_log, **AZURE) is False
    monkeypatch.delenv("AZURE_DATABASE_URL")
    assert db.safe_load([], "2026-10-01", {}, set(), raising_log, **AZURE) is False


def test_failed_local_load_does_not_block_azure_load(monkeypatch):
    import job_searcher
    calls = []

    def fake_safe_load(rows, today, cats, cv, log, **kw):
        calls.append((rows, today, cats, cv, kw))
        return False  # the local load fails; the Azure one must still be attempted
    monkeypatch.setattr(job_searcher.jobdb, "safe_load", fake_safe_load)
    job_searcher.load_databases([{"Job ID": "1"}], "2026-10-01", {"cv_skills": ["Python"]})
    assert [c[4] for c in calls] == [{}, {"url_var": "AZURE_DATABASE_URL", "label": "Azure Postgres"}]
    assert calls[0][:4] == calls[1][:4] and calls[0][3] == {"Python"}
