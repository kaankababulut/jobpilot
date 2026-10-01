"""Tests for jobpilot.backfill. The db-marked ones load into the pg fixture's throwaway schema;
the rest fake the connection, so they run in the default (no Docker) suite."""
import datetime as dt
import os

import pytest

import job_searcher as js
from jobpilot import backfill, db
from test_db import CATEGORIES, CV, one
from test_records import make_row

URL = "postgresql://jobs:s3cretPass@127.0.0.1:5432/jobs"


def write_xlsx(path, rows, cfg) -> str:
    # job_searcher's own writer, so the test file has exactly the layout the real files have
    js.build_workbook(str(path), rows, cfg, "test")
    return str(path)


@pytest.mark.db
def test_same_file_twice_keeps_job_count(pg, tmp_path, cfg):
    path = write_xlsx(tmp_path / "jobs_2026-09-29.xlsx", [make_row(**{"Job ID": "1"}), make_row(**{"Job ID": "2"})], cfg)
    assert backfill.load_file(pg, path, CATEGORIES, CV, print) == (2, 2)
    assert backfill.load_file(pg, path, CATEGORIES, CV, print) == (2, 2)  # same-day rewrite still counts
    assert one(pg, "SELECT count(*) FROM jobs")[0] == 2
    assert one(pg, "SELECT count(*) FROM job_skills")[0] == 6
    runs = pg.execute("SELECT kind, run_date, rows_offered, rows_written FROM runs").fetchall()
    assert runs == [("backfill", dt.date(2026, 9, 29), 2, 2)] * 2


@pytest.mark.db
def test_older_file_after_newer_writes_nothing(pg, tmp_path, cfg):
    new = write_xlsx(tmp_path / "new.xlsx", [make_row(**{"Run Date": "2026-09-30"})], cfg)
    old = write_xlsx(tmp_path / "old.xlsx", [make_row(**{"Run Date": "2026-09-28"})], cfg)
    backfill.load_file(pg, new, CATEGORIES, CV, print)
    assert backfill.load_file(pg, old, CATEGORIES, CV, print) == (1, 0)
    assert one(pg, "SELECT first_seen, last_seen FROM jobs") == (dt.date(2026, 9, 28), dt.date(2026, 9, 30))


@pytest.mark.db
def test_run_date_is_latest_in_file(pg, tmp_path, cfg):
    rows = [make_row(**{"Job ID": "1", "Run Date": "2026-09-20"}), make_row(**{"Job ID": "2", "Run Date": "2026-09-27"})]
    backfill.load_file(pg, write_xlsx(tmp_path / "master.xlsx", rows, cfg), CATEGORIES, CV, print)
    assert one(pg, "SELECT run_date FROM runs")[0] == dt.date(2026, 9, 27)


@pytest.mark.db
def test_empty_file_uses_date_from_filename(pg, tmp_path):
    from openpyxl import Workbook
    # header-only Jobs sheet by hand: build_workbook can't write zero rows (its conditional-format range breaks)
    wb = Workbook(); wb.active.title = "Jobs"; wb.active.append([n for n, _ in js.JOB_COLS])
    path = str(tmp_path / "jobs_2026-09-15.xlsx"); wb.save(path)
    assert backfill.load_file(pg, path, CATEGORIES, CV, print) == (0, 0)
    assert one(pg, "SELECT run_date, rows_offered FROM runs") == (dt.date(2026, 9, 15), 0)


@pytest.mark.db
def test_failed_file_rolls_back_alone(pg, tmp_path, cfg, monkeypatch):
    good = write_xlsx(tmp_path / "good.xlsx", [make_row(**{"Job ID": "1"})], cfg)
    bad = write_xlsx(tmp_path / "bad.xlsx", [make_row(**{"Job ID": "2"})], cfg)
    with pg.transaction():  # stands in for main's per-file `with connect(url) as conn:`
        backfill.load_file(pg, good, CATEGORIES, CV, print)

    def fail(*a):
        raise RuntimeError("disk full")
    monkeypatch.setattr(backfill, "record_run", fail)  # fails after the bad file's jobs were upserted
    with pytest.raises(RuntimeError), pg.transaction():
        backfill.load_file(pg, bad, CATEGORIES, CV, print)
    assert [r for (r,) in pg.execute("SELECT source_id FROM jobs")] == ["1"]
    assert one(pg, "SELECT count(*) FROM runs")[0] == 1


def test_default_paths_sorts_dailies_and_puts_master_last(tmp_path):
    os.makedirs(tmp_path / "daily")
    for name in ("jobs_2026-09-30.xlsx", "jobs_2026-09-28.xlsx", "jobs_2026-09-29.xlsx"):  # created out of order
        (tmp_path / "daily" / name).write_bytes(b"")
    (tmp_path / "job_market_master_1230.xlsx").write_bytes(b"")  # side file: not a default
    names = lambda: [os.path.basename(p) for p in backfill.default_paths(str(tmp_path))]
    assert names() == ["jobs_2026-09-28.xlsx", "jobs_2026-09-29.xlsx", "jobs_2026-09-30.xlsx"]
    (tmp_path / "job_market_master.xlsx").write_bytes(b"")
    assert names()[-1] == "job_market_master.xlsx" and len(names()) == 4


def test_default_paths_empty_dir(tmp_path):
    assert backfill.default_paths(str(tmp_path)) == []


@pytest.mark.parametrize("name, expected", [
    ("jobs_2026-09-15.xlsx", dt.date(2026, 9, 15)),
    ("job_market_master.xlsx", dt.date.today()),
    ("jobs_2026-13-45.xlsx", dt.date.today()),
])
def test_file_date(name, expected):
    assert backfill.file_date(os.path.join("out", "daily", name)) == expected


def test_no_files_returns_1(monkeypatch, capsys):
    monkeypatch.setattr(backfill, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setattr(backfill, "default_paths", lambda out: [])
    monkeypatch.setattr(backfill, "connect", lambda url: pytest.fail("connect should not be called"))
    assert backfill.main([]) == 1
    assert "no Excel files found in" in capsys.readouterr().err


def test_redact_empty_url_leaves_message_alone():
    assert db.redact(RuntimeError("boom\nmore"), "") == "RuntimeError: boom"


def test_missing_file_raises(tmp_path):
    # load_master_rows returns [] for a missing path; the backfill must not report that as success
    with pytest.raises(FileNotFoundError):
        backfill.load_file(None, str(tmp_path / "nope.xlsx"), {}, set(), print)


def test_missing_url_returns_1_without_connecting(monkeypatch, capsys):
    monkeypatch.setattr(backfill, "load_dotenv", lambda *a, **k: False)  # the real .env would set it
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(backfill, "connect", lambda url: pytest.fail("connect should not be called"))
    assert backfill.main([]) == 1
    assert "DATABASE_URL not set" in capsys.readouterr().err


def test_error_returns_1_and_hides_password(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(backfill, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setenv("DATABASE_URL", URL)

    def boom(url):
        raise RuntimeError(f"could not connect to {url} (password s3cretPass)\nsecond line")
    monkeypatch.setattr(backfill, "connect", boom)
    assert backfill.main([str(tmp_path / "x.xlsx")]) == 1
    err = capsys.readouterr().err
    assert "s3cretPass" not in err and "second line" not in err
    assert err.startswith("ERROR: backfill failed on x.xlsx: RuntimeError:")
