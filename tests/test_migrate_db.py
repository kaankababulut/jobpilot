"""Integration tests for jobpilot.migrate against the local Postgres (pytest -m db).
The pg fixture's schema is built by the real migrations; `legacy` drops its schema_migrations table
to mimic a database the old db/init script built (the baseline case), and `empty` switches the
same connection to a second, empty throwaway schema for the apply tests."""
import uuid

import pytest

from jobpilot import migrate
from jobpilot.migrate import MigrationFailed
from test_db import one

pytestmark = pytest.mark.db


@pytest.fixture
def empty(pg):
    schema = f"test_{uuid.uuid4().hex[:12]}"
    pg.execute(f"CREATE SCHEMA {schema}")
    try:
        pg.execute(f"SET search_path TO {schema}, public")  # public holds the vector extension
        yield pg
    finally:
        try:
            if not pg.autocommit:  # a test switched it off; the DROP must not sit in an open transaction
                pg.rollback()
                pg.autocommit = True
        finally:
            pg.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


@pytest.fixture
def legacy(pg):
    # the 001 tables without schema_migrations: what the real database looks like before --baseline
    pg.execute("DROP TABLE schema_migrations")
    return pg


def write(directory, **files) -> str:
    for name, sql in files.items():
        (directory / f"{name}.sql").write_text(sql, encoding="utf-8")
    return str(directory)


def table_exists(conn, name: str) -> bool:
    # current schema only, so tables of the same name in public don't count
    return one(conn, "SELECT to_regclass(quote_ident(current_schema()) || '.' || %s)", (name,))[0] is not None


def recorded(conn) -> list[tuple[str, str]]:
    return conn.execute("SELECT version, name FROM schema_migrations ORDER BY version").fetchall()


def test_apply_runs_all_in_order_and_records_them(empty, tmp_path):
    d = write(tmp_path, **{"001_a": "CREATE TABLE a (id INT)",
                           "002_b": "CREATE TABLE b (a_id INT); INSERT INTO a VALUES (1)"})  # 002 needs 001's table
    assert [m.version for m in migrate.apply(empty, d)] == ["001", "002"]
    assert recorded(empty) == [("001", "a"), ("002", "b")]
    assert table_exists(empty, "b") and one(empty, "SELECT count(*) FROM a")[0] == 1


def test_second_apply_applies_nothing(empty, tmp_path):
    d = write(tmp_path, **{"001_a": "CREATE TABLE a (id INT)"})
    migrate.apply(empty, d)
    assert migrate.apply(empty, d) == []  # would fail with DuplicateTable if 001 ran again
    assert recorded(empty) == [("001", "a")]


def test_failing_migration_rolls_back_completely_and_stops(empty, tmp_path):
    d = write(tmp_path, **{"001_a": "CREATE TABLE a (id INT)",
                           "002_bad": "CREATE TABLE b (id INT); SELEKT oops",  # valid statement, then invalid
                           "003_c": "CREATE TABLE c (id INT)"})
    with pytest.raises(MigrationFailed, match="002_bad.sql") as exc:
        migrate.apply(empty, d)
    assert exc.value.migration.version == "002" and not exc.value.fresh
    assert recorded(empty) == [("001", "a")]  # 001 stays committed
    assert table_exists(empty, "a") and not table_exists(empty, "b") and not table_exists(empty, "c")


def test_failure_rolls_back_alone_on_non_autocommit_connection(empty, tmp_path):
    # backfill-style callers use non-autocommit connections; earlier files must still be committed
    d = write(tmp_path, **{"001_a": "CREATE TABLE a (id INT)", "002_bad": "CREATE TABLE b (id INT); SELEKT"})
    empty.autocommit = False
    with pytest.raises(MigrationFailed):
        migrate.apply(empty, d)
    empty.rollback()  # throws away anything that wasn't committed
    assert recorded(empty) == [("001", "a")] and not table_exists(empty, "b")


def test_duplicate_table_on_fresh_db_is_marked_fresh(legacy, tmp_path):
    # tables but no schema_migrations: what the CLI hint is for
    d = write(tmp_path, **{"001_initial": "CREATE TABLE jobs (id INT)"})
    with pytest.raises(MigrationFailed) as exc:
        migrate.apply(legacy, d)
    assert exc.value.fresh and exc.value.cause.sqlstate == migrate.DUPLICATE_TABLE


def test_real_migrations_build_an_empty_schema(empty):
    # the real db/migrations directory, so a broken or misnamed migration fails here and in CI
    assert [m.version for m in migrate.apply(empty, migrate.DEFAULT_DIR)][0] == "001"
    for t in ("jobs", "skills", "job_skills", "runs"):
        assert table_exists(empty, t)
    assert recorded(empty)[0] == ("001", "initial")


def test_baseline_records_001_without_running_it(legacy, tmp_path):
    d = write(tmp_path, **{"001_initial": "SELEKT this would fail if executed",
                           "002_later": "CREATE TABLE later (id INT)"})
    assert [m.version for m in migrate.baseline(legacy, d)] == ["001"]
    assert recorded(legacy) == [("001", "initial")]  # 002 not marked: it hasn't run
    assert migrate.baseline(legacy, d) == []  # twice is a no-op
    assert recorded(legacy) == [("001", "initial")]
    assert [m.version for m in migrate.apply(legacy, d)] == ["002"]  # apply after baseline runs only later ones
    assert table_exists(legacy, "later")


def test_baseline_refuses_without_jobs_table(empty, tmp_path):
    # the real public schema has a jobs table behind this one in search_path; it mustn't count
    d = write(tmp_path, **{"001_initial": "CREATE TABLE jobs (id INT)"})
    with pytest.raises(ValueError, match="no jobs table"):
        migrate.baseline(empty, d)
    assert not table_exists(empty, "schema_migrations")  # refused inside the transaction: no trace left


def test_refuses_inside_open_transaction(empty, tmp_path):
    # the per-file blocks would only be savepoints here, so "committed per file" wouldn't hold
    d = write(tmp_path, **{"001_a": "CREATE TABLE a (id INT)"})
    empty.autocommit = False
    empty.execute("SELECT 1")  # psycopg opens a transaction implicitly
    for func in (migrate.apply, migrate.baseline):
        with pytest.raises(ValueError, match="open transaction"):
            func(empty, d)
    empty.rollback()
    assert not table_exists(empty, "schema_migrations") and not table_exists(empty, "a")
