"""A tiny migration runner: numbered plain-SQL files in db/migrations/ (NNN_name.sql),
applied in order, each in its own transaction, and recorded in a schema_migrations table.
Chosen over Alembic because there's no ORM and one developer; this module is just SQL files and a loop.
Usage: python -m jobpilot.migrate [--baseline]
Migration files must not contain BEGIN/COMMIT or CREATE INDEX CONCURRENTLY: the runner wraps each
file in its own transaction, and those statements either break that or can't run inside one."""
import argparse
import os
import re
import sys
from typing import Callable, NamedTuple

from jobpilot.db import connect, redact

try:
    from dotenv import load_dotenv
except ImportError:  # same fallback as job_searcher: env vars set by hand still work
    def load_dotenv(*args, **kwargs): return False

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DIR = os.path.join(ROOT, "db", "migrations")
DUPLICATE_TABLE = "42P07"  # Postgres error code, so the hint check needn't import psycopg

# lowercase only, so 001_Init.sql and 001_init.sql can't both exist on a case-sensitive CI runner
NAME_RE = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")


class Migration(NamedTuple):
    version: str  # "001", kept as text so the leading zeros match schema_migrations.version
    name: str
    path: str


class MigrationFailed(Exception):
    """A migration file failed and was rolled back; the message names the file."""
    def __init__(self, migration: Migration, cause: Exception, fresh: bool):
        msg = (str(cause).splitlines() or [""])[0]
        super().__init__(f"{os.path.basename(migration.path)}: {type(cause).__name__}: {msg}")
        self.migration, self.cause = migration, cause
        self.fresh = fresh  # nothing was recorded before it: maybe a database the old db/init built


def discover(directory: str) -> list[Migration]:
    """Every NNN_name.sql in directory, sorted by version."""
    # raise rather than return []: a wrong path would otherwise look like "nothing to apply"
    if not os.path.isdir(directory):
        raise ValueError(f"migrations directory not found: {directory}")
    found: dict[str, Migration] = {}
    for fname in os.listdir(directory):
        path = os.path.join(directory, fname)
        if not fname.endswith(".sql") or not os.path.isfile(path):
            continue  # README.md, .gitkeep and the like
        m = NAME_RE.match(fname)
        if not m:
            # a typo'd name skipped silently would never be applied, so fail loudly
            raise ValueError(f"bad migration file name: {fname} (expected NNN_name.sql, lowercase)")
        version, name = m.groups()
        if version in found:
            raise ValueError(f"duplicate migration version {version}: {os.path.basename(found[version].path)} and {fname}")
        found[version] = Migration(version, name, path)
    return [found[v] for v in sorted(found)]


# Everything below runs inside `with conn.transaction():`, which is BEGIN ... COMMIT on an autocommit
# connection and also commits at the end on an idle non-autocommit one. Table names are unqualified
# on purpose: search_path picks the schema, so tests can run in throwaway schemas.

def ensure_table(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                 "version TEXT PRIMARY KEY, name TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")


def applied(conn) -> set[str]:
    return {v for (v,) in conn.execute("SELECT version FROM schema_migrations").fetchall()}


def _require_idle(conn) -> None:
    from psycopg.pq import TransactionStatus  # lazy, like db.connect
    # inside a caller's open transaction our per-file blocks would only be savepoints, so nothing
    # would really commit per file and a later rollback could undo migrations we reported as applied
    if not conn.autocommit and conn.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError("migrations need an autocommit or idle connection, not one inside an open transaction")


def _record(conn, m: Migration) -> None:
    conn.execute("INSERT INTO schema_migrations (version, name) VALUES (%s, %s)", (m.version, m.name))


def apply(conn, directory: str, log: Callable[[str], None] = lambda msg: None) -> list[Migration]:
    """Runs every unrecorded migration in order, one transaction per file; returns those applied.
    A failing file rolls back completely (its SQL and its schema_migrations row) and stops the run,
    so later files never run on a half-migrated schema. Files mustn't contain their own BEGIN/COMMIT."""
    migrations = discover(directory)  # before touching the DB, so a wrong path changes nothing
    _require_idle(conn)
    with conn.transaction():
        ensure_table(conn)
        done = applied(conn)
    ran: list[Migration] = []
    for m in migrations:
        if m.version in done:
            continue
        with open(m.path, encoding="utf-8") as f:
            sql = f.read()
        try:
            # two runs at once fail cleanly: the second waits on the first's locks, then errors
            # (duplicate table or schema_migrations key) and rolls back its own file
            with conn.transaction():
                conn.execute(sql)  # no parameters, so psycopg sends it as one multi-statement query
                _record(conn, m)
        except Exception as e:
            raise MigrationFailed(m, e, fresh=not done and not ran) from e
        ran.append(m)
        log(f"applied {os.path.basename(m.path)}")  # as we go, so a later failure still shows these
    return ran


def baseline(conn, directory: str, log: Callable[[str], None] = lambda msg: None) -> list[Migration]:
    """Records 001 as applied WITHOUT running it, for a database the old db/init script already built.
    Only 001, because that's all db/init created; marking later files too would hide that they never ran."""
    first = [m for m in discover(directory) if m.version == "001"]
    if not first:
        raise ValueError(f"no 001 migration in {directory}")
    _require_idle(conn)
    with conn.transaction():
        ensure_table(conn)
        if "001" in applied(conn):
            return []  # already baselined or applied
        # current_schema() rather than the whole search_path: a jobs table in a later schema (public
        # behind a test schema) doesn't mean this one has the tables. Raising here also rolls back
        # the schema_migrations table we just created, so a refused baseline leaves no trace.
        if conn.execute("SELECT to_regclass(quote_ident(current_schema()) || '.jobs')").fetchone()[0] is None:
            raise ValueError("no jobs table in this schema, so there is nothing to baseline; "
                             "run `python -m jobpilot.migrate` without --baseline")
        _record(conn, first[0])
    log(f"baselined {os.path.basename(first[0].path)} (recorded, not executed)")
    return first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jobpilot.migrate", description="Apply db/migrations/*.sql")
    parser.add_argument("--baseline", action="store_true",
                        help="record 001 as applied without running it (database built by the old db/init script)")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    load_dotenv(os.path.join(ROOT, ".env"))
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        return 1
    try:
        with connect(url) as conn:
            conn.autocommit = True  # each file commits on its own, so a later failure keeps earlier ones
            # db.connect's 30s limit suits the daily load, but index builds and backfills can take
            # longer; a lock wait (e.g. on a table the API is reading) should fail, not hang
            conn.execute("SET statement_timeout = 0")
            conn.execute("SET lock_timeout = '10s'")
            done = (baseline if args.baseline else apply)(conn, DEFAULT_DIR, print)
            if not done:
                print("001 already recorded" if args.baseline else "nothing to apply")
    except Exception as e:
        hint = ""
        if isinstance(e, MigrationFailed) and e.fresh and getattr(e.cause, "sqlstate", None) == DUPLICATE_TABLE:
            hint = "; database already has tables: run `python -m jobpilot.migrate --baseline` once"
        print(f"ERROR: migrate failed: {redact(e, url)}{hint}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
