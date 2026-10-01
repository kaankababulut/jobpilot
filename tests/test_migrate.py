import pytest

from jobpilot import migrate
from jobpilot.migrate import Migration, MigrationFailed, discover

URL = "postgresql://jobs:s3cretPass@127.0.0.1:5432/jobs"


def _touch(directory, *names):
    for n in names:
        (directory / n).write_text("SELECT 1;")


def test_sorted_by_version_regardless_of_creation_order(tmp_path):
    _touch(tmp_path, "010_later.sql", "002_add_index.sql", "001_initial.sql")
    assert [m.version for m in discover(str(tmp_path))] == ["001", "002", "010"]


def test_version_name_and_path_parsed(tmp_path):
    _touch(tmp_path, "001_initial_schema.sql")
    assert discover(str(tmp_path)) == [Migration("001", "initial_schema", str(tmp_path / "001_initial_schema.sql"))]


def test_non_sql_files_ignored(tmp_path):
    _touch(tmp_path, "001_initial.sql", "README.md", ".gitkeep", "001_initial.sql.bak")
    assert [m.name for m in discover(str(tmp_path))] == ["initial"]


def test_directory_named_like_sql_ignored(tmp_path):
    (tmp_path / "old.sql").mkdir()
    assert discover(str(tmp_path)) == []


@pytest.mark.parametrize("bad", ["1_initial.sql", "001-initial.sql", "001_Initial.sql", "initial.sql", "0001_x.sql"])
def test_badly_named_sql_raises_naming_the_file(tmp_path, bad):
    _touch(tmp_path, "001_ok.sql", bad)
    with pytest.raises(ValueError, match=bad):
        discover(str(tmp_path))


def test_duplicate_version_raises(tmp_path):
    _touch(tmp_path, "002_a.sql", "002_b.sql")
    with pytest.raises(ValueError, match="duplicate migration version 002"):
        discover(str(tmp_path))


def test_missing_directory_raises(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        discover(str(tmp_path / "nope"))


def test_empty_directory_returns_empty_list(tmp_path):
    assert discover(str(tmp_path)) == []


# --- CLI (no database: connect is faked) ---

def test_cli_missing_url_returns_1_without_connecting(monkeypatch, capsys):
    monkeypatch.setattr(migrate, "load_dotenv", lambda *a, **k: False)  # the real .env would set it
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(migrate, "connect", lambda url: pytest.fail("connect should not be called"))
    assert migrate.main([]) == 1
    assert "DATABASE_URL not set" in capsys.readouterr().err


def test_cli_error_is_one_redacted_line(monkeypatch, capsys):
    monkeypatch.setattr(migrate, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setenv("DATABASE_URL", URL)

    def boom(url):
        raise RuntimeError(f"could not connect to {url} (password s3cretPass)\nsecond line")
    monkeypatch.setattr(migrate, "connect", boom)
    assert migrate.main(["--baseline"]) == 1
    err = capsys.readouterr().err
    assert "s3cretPass" not in err and URL not in err
    assert err.count("\n") == 1 and err.startswith("ERROR: migrate failed: RuntimeError")


class _FakeConn:
    def __init__(self):
        self.autocommit, self.sql = False, []
    def execute(self, sql): self.sql.append(sql)
    def __enter__(self): return self
    def __exit__(self, *exc): return False


class _DuplicateTable(Exception):
    sqlstate = "42P07"


@pytest.mark.parametrize("fresh, hinted", [(True, True), (False, False)])
def test_cli_hints_baseline_only_for_existing_tables_on_fresh_db(monkeypatch, capsys, fresh, hinted):
    monkeypatch.setattr(migrate, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setattr(migrate, "connect", lambda url: _FakeConn())

    def fail(conn, directory, log):
        raise MigrationFailed(Migration("001", "initial", "001_initial.sql"), _DuplicateTable('relation "jobs" already exists'), fresh)
    monkeypatch.setattr(migrate, "apply", fail)
    assert migrate.main([]) == 1
    err = capsys.readouterr().err
    assert "001_initial.sql" in err
    assert ("--baseline" in err) == hinted


@pytest.mark.parametrize("argv, func, out", [([], "apply", "nothing to apply\n"),
                                             (["--baseline"], "baseline", "001 already recorded\n")])
def test_cli_up_to_date_message_and_session_settings(monkeypatch, capsys, argv, func, out):
    monkeypatch.setattr(migrate, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setenv("DATABASE_URL", URL)
    conn = _FakeConn()
    monkeypatch.setattr(migrate, "connect", lambda url: conn)
    monkeypatch.setattr(migrate, func, lambda conn, directory, log: [])
    assert migrate.main(argv) == 0
    assert capsys.readouterr().out == out
    assert conn.autocommit  # per-file commits need it
    assert conn.sql == ["SET statement_timeout = 0", "SET lock_timeout = '10s'"]


# --- --url-env: the name of the variable, never the URL itself ---

def test_url_env_reads_the_named_variable_and_ignores_database_url(monkeypatch, capsys):
    monkeypatch.setattr(migrate, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://local/wrong")
    monkeypatch.setenv("AZURE_DATABASE_URL", URL)
    seen = []
    monkeypatch.setattr(migrate, "connect", lambda url: seen.append(url) or _FakeConn())
    monkeypatch.setattr(migrate, "apply", lambda conn, directory, log: [])
    assert migrate.main(["--url-env", "AZURE_DATABASE_URL"]) == 0
    assert seen == [URL]
    assert migrate.os.environ["DATABASE_URL"] == "postgresql://local/wrong"  # untouched


def test_url_env_missing_names_the_variable_without_connecting(monkeypatch, capsys):
    monkeypatch.setattr(migrate, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setenv("DATABASE_URL", URL)  # set, but not the one asked for: must not be used as a fallback
    monkeypatch.delenv("AZURE_DATABASE_URL", raising=False)
    monkeypatch.setattr(migrate, "connect", lambda url: pytest.fail("connect should not be called"))
    assert migrate.main(["--url-env", "AZURE_DATABASE_URL"]) == 1
    err = capsys.readouterr().err
    assert err == "ERROR: AZURE_DATABASE_URL not set\n" and URL not in err


def test_baseline_works_with_url_env(monkeypatch, capsys):
    monkeypatch.setattr(migrate, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setenv("AZURE_DATABASE_URL", URL)
    monkeypatch.setattr(migrate, "connect", lambda url: _FakeConn())
    monkeypatch.setattr(migrate, "apply", lambda *a: pytest.fail("apply should not be called"))
    monkeypatch.setattr(migrate, "baseline", lambda conn, directory, log: [])
    assert migrate.main(["--baseline", "--url-env", "AZURE_DATABASE_URL"]) == 0
    assert capsys.readouterr().out == "001 already recorded\n"
