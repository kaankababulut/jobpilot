"""Loads existing Excel files into Postgres: python -m jobpilot.backfill [paths...]
With no paths it loads output/daily/*.xlsx oldest first, then the master. Safe to re-run:
the upserts are idempotent, and older files can't overwrite newer data (see jobpilot.db).
Unlike the daily run it fails loudly: any error is printed and the exit code is 1."""
import datetime as dt
import glob
import json
import os
import re
import sys
from typing import Callable

import job_searcher as js
from jobpilot.db import connect, prepare, record_run, redact, upsert_jobs

try:
    from dotenv import load_dotenv
except ImportError:  # same fallback as job_searcher: env vars set by hand still work
    def load_dotenv(*args, **kwargs): return False


def default_paths(out: str) -> list[str]:
    # dailies first (names sort by date), master last; the order only affects which run row comes first.
    # Master side files (job_market_master_HHMM.xlsx) are left out: they're temporary copies the next daily run merges back and deletes.
    paths = sorted(glob.glob(os.path.join(out, "daily", "jobs_*.xlsx")))
    master = os.path.join(out, "job_market_master.xlsx")
    return paths + ([master] if os.path.exists(master) else [])


def file_date(path: str) -> dt.date:
    # an empty file still gets a meaningful runs row: the date in jobs_YYYY-MM-DD.xlsx, else today
    m = re.search(r"jobs_(\d{4}-\d{2}-\d{2})", os.path.basename(path))
    try:
        return dt.date.fromisoformat(m.group(1)) if m else dt.date.today()
    except ValueError:  # e.g. jobs_2026-13-45.xlsx
        return dt.date.today()


def load_file(conn, path: str, categories: dict[str, str], cv_skills: set[str],
              log: Callable[[str], None]) -> tuple[int, int]:
    """Loads one Excel file through conn and records a backfill run; returns (offered, written)."""
    if not os.path.exists(path):  # load_master_rows would quietly return [] for a typo'd path
        raise FileNotFoundError(f"no such file: {path}")
    rows = js.load_master_rows(path)  # daily files and the master share the same Jobs sheet
    records = prepare(rows, log)
    written = upsert_jobs(conn, records, categories, cv_skills)
    run_date = max((r["run_date"] for r in records), default=None) or file_date(path)
    record_run(conn, "backfill", run_date, len(rows), written)
    return len(rows), written


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    load_dotenv(os.path.join(js.HERE, ".env"))
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        return 1
    path = "config.json"  # names the failing file in the error message
    try:
        with open(os.path.join(js.HERE, path), encoding="utf-8") as f:
            cfg = json.load(f)
        out = cfg["output_dir"] if os.path.isabs(cfg["output_dir"]) else os.path.join(js.HERE, cfg["output_dir"])
        paths = argv or default_paths(out)
        if not paths:  # loading nothing is almost certainly a wrong output_dir, so don't report success
            print(f"ERROR: no Excel files found in {out}", file=sys.stderr)
            return 1
        categories = {k: v[0] for k, v in js.SKILLS.items()}
        cv_skills = set(cfg["cv_skills"])
        for path in paths:
            # one transaction per file: a bad file rolls back alone, earlier files stay loaded
            with connect(url) as conn:
                offered, written = load_file(conn, path, categories, cv_skills, print)
            print(f"{os.path.basename(path)}: {offered} rows, {written} written")
        path = "jobs table count"
        with connect(url) as conn:
            print(f"jobs table now holds {conn.execute('SELECT count(*) FROM jobs').fetchone()[0]} jobs")
    except Exception as e:
        print(f"ERROR: backfill failed on {os.path.basename(path)}: {redact(e, url)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
