"""Tests for the `python -m jobpilot.embed --backfill / --query` CLI. Exit-code tests need no database;
the db-marked ones run main() against the pg fixture's throwaway schema with FakeEmbedder injected."""
import pytest

from jobpilot import embed
from tests.fakes import FakeEmbedder

URL = "postgresql://jobs:s3cretPass@127.0.0.1:5432/jobs"


@pytest.fixture
def cli(monkeypatch, tmp_path):
    """No real .env, and a config whose model dir and CV profile live in tmp_path (no CV file yet)."""
    monkeypatch.setattr(embed, "load_dotenv", lambda *a, **k: False)  # the real .env would set DATABASE_URL
    cfg = {"embeddings": {"enabled": True, "model_dir": str(tmp_path / "model"), "cv_profile": str(tmp_path / "cv.txt")}}
    monkeypatch.setattr(embed, "read_config", lambda: cfg)
    return tmp_path


def test_no_action_prints_help_and_returns_1(cli, capsys):
    assert embed.main([]) == 1
    assert "--backfill" in capsys.readouterr().out


def test_unset_env_var_returns_1_without_loading(cli, monkeypatch, capsys):
    monkeypatch.delenv("JOBPILOT_TEST_UNSET", raising=False)
    monkeypatch.setattr(embed, "load_embedder", lambda d: pytest.fail("the model should not be loaded"))
    assert embed.main(["--backfill", "--url-env", "JOBPILOT_TEST_UNSET"]) == 1
    assert capsys.readouterr().err.strip() == "ERROR: JOBPILOT_TEST_UNSET not set"


@pytest.mark.parametrize("action", [["--backfill"], ["--query", "python"]])
def test_missing_model_returns_1_with_download_hint(cli, monkeypatch, capsys, action):
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setattr(embed, "connect", lambda *a, **k: pytest.fail("connect should not be called"))
    assert embed.main(action) == 1  # the real loader on tmp_path/model, which doesn't exist
    err = capsys.readouterr().err
    assert err.startswith("ERROR: model files missing") and "python -m jobpilot.embed --download" in err


def test_model_dir_flag_overrides_config(cli, monkeypatch):
    seen = []
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setattr(embed, "load_embedder", lambda d: seen.append(d) or (None, "missing"))
    embed.main(["--backfill", "--model-dir", "elsewhere"])
    assert seen == ["elsewhere"]


@pytest.mark.parametrize("action", [["--backfill"], ["--query", "python"]])
def test_db_error_returns_1_and_hides_the_url(cli, monkeypatch, capsys, action):
    monkeypatch.setenv("AZURE_DATABASE_URL", URL)

    def boom(url, read_only=False):
        raise RuntimeError(f"could not connect to {url} (password s3cretPass rejected)\nsecond line")
    monkeypatch.setattr(embed, "connect", boom)
    assert embed.main([*action, "--url-env", "AZURE_DATABASE_URL"], embedder=FakeEmbedder()) == 1
    err = capsys.readouterr().err
    assert err.startswith("ERROR: embedding ") and "RuntimeError" in err
    assert URL not in err and "s3cretPass" not in err and "second line" not in err


def test_limit_below_1_is_a_usage_error(cli):
    with pytest.raises(SystemExit) as e:
        embed.main(["--query", "python", "--limit", "0"])
    assert e.value.code == 2


# --- against the database (pytest -m db) ---

class Borrowed:
    """Hands main() the fixture's connection without closing it, recording how it was opened."""
    def __init__(self, conn):
        self.conn, self.calls = conn, []

    def __call__(self, url, read_only=False):
        self.calls.append((url, read_only))
        return self

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        return False


@pytest.fixture
def on_pg(pg, cli, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)  # only checked for presence; connect is the fixture's
    borrowed = Borrowed(pg)
    monkeypatch.setattr(embed, "connect", borrowed)
    return borrowed


@pytest.mark.db
def test_backfill_embeds_jobs_and_cv_then_reruns_as_no_op(on_pg, pg, cli, capsys):
    from test_embed_db import add_job
    add_job(pg, "1", title="Junior Python Developer")
    add_job(pg, "2", title="Truck Driver", description="Long-haul routes.")
    (cli / "cv.txt").write_text("Python developer, SQL", encoding="utf-8")
    assert embed.main(["--backfill"], embedder=FakeEmbedder()) == 0
    assert capsys.readouterr().out.startswith("2 jobs embedded, CV updated, in ")
    assert pg.execute("SELECT count(*) FROM job_embeddings").fetchone()[0] == 2
    assert pg.execute("SELECT model FROM cv_embeddings").fetchall() == [("fake@test",)]
    assert embed.main(["--backfill"], embedder=FakeEmbedder()) == 0
    assert capsys.readouterr().out.startswith("0 jobs embedded, CV not updated, in ")


@pytest.mark.db
def test_backfill_without_cv_profile_embeds_jobs_only(on_pg, pg, capsys):
    from test_embed_db import add_job
    add_job(pg, "1")
    assert embed.main(["--backfill"], embedder=FakeEmbedder()) == 0
    out = capsys.readouterr().out
    assert "no CV profile at" in out and "1 jobs embedded, CV not updated" in out
    assert pg.execute("SELECT count(*) FROM cv_embeddings").fetchone()[0] == 0


@pytest.mark.db
def test_query_prints_nearest_first_and_connects_read_only(on_pg, pg, capsys):
    from test_embed_db import add_job
    python = add_job(pg, "1", title="Junior Python Developer", description="Python and SQL.")
    add_job(pg, "2", title="Truck Driver", description="Long-haul routes.")
    embed.sync_embeddings(pg, FakeEmbedder())
    capsys.readouterr()
    assert embed.main(["--query", "python developer", "--limit", "1"], embedder=FakeEmbedder()) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1 and lines[0].endswith(f"Junior Python Developer | Acme  (id {python})")
    assert 0 < float(lines[0].split()[0]) <= 1
    assert on_pg.calls == [(URL, True)]


@pytest.mark.db
def test_query_without_vectors_hints_at_backfill(on_pg, capsys):
    assert embed.main(["--query", "python"], embedder=FakeEmbedder()) == 0
    assert "run python -m jobpilot.embed --backfill" in capsys.readouterr().out


@pytest.mark.db
def test_safe_embed_writes_vectors_through_the_real_sync(pg, monkeypatch, tmp_path):
    from test_embed_db import add_job
    add_job(pg, "1")
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setattr(embed, "connect", Borrowed(pg))
    cfg = {"embeddings": {"enabled": True, "cv_profile": str(tmp_path / "none.txt")}}
    state, lines = {"embedder": FakeEmbedder(), "reason": ""}, []  # a pre-loaded model, as the second DB sees it
    assert embed.safe_embed(cfg, state, lines.append) is True
    assert lines[0].startswith("Embeddings (Postgres): 1 new vectors, no CV (none.txt missing) in ")
    assert pg.execute("SELECT count(*) FROM job_embeddings").fetchone()[0] == 1
    assert state["memo"]["fake@test"]  # the memo the Azure call would reuse
