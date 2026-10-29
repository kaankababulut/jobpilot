"""Tests for jobpilot.embed.safe_embed, the daily run's hook, and its call in job_searcher.load_databases
(no database and no model needed: connect, the model loader and the sync are faked)."""
import pytest

import job_searcher
from jobpilot import embed
from tests.fakes import FakeEmbedder

URL = "postgresql://jobs:s3cretPass@127.0.0.1:5432/jobs"
AZURE_URL = "postgresql://jobsadmin:Azur3Secret@jobpilot.postgres.database.azure.com:5432/jobs?sslmode=require"
AZURE = {"url_var": "AZURE_DATABASE_URL", "label": "Azure Postgres"}


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def on(tmp_path):
    """An enabled config whose model dir and CV profile live in tmp_path (the CV file exists)."""
    (tmp_path / "cv.txt").write_text("Junior Python developer", encoding="utf-8")
    return {"embeddings": {"enabled": True, "model_dir": str(tmp_path / "model"), "cv_profile": str(tmp_path / "cv.txt")}}


@pytest.fixture
def fakes(monkeypatch):
    """A working model and database: counts model loads and records each sync's (url, memo, cv)."""
    calls = {"loads": 0, "syncs": []}

    def load(model_dir):
        calls["loads"] += 1
        return FakeEmbedder(), ""
    monkeypatch.setattr(embed, "load_embedder", load)
    monkeypatch.setattr(embed, "connect", lambda url: calls["syncs"].append([url]) or FakeConn())

    def run_sync(conn, embedder, cv, memo):
        calls["syncs"][-1] += [memo, cv]
        return 2, True
    monkeypatch.setattr(embed, "run_sync", run_sync)
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("AZURE_DATABASE_URL", AZURE_URL)
    return calls


@pytest.mark.parametrize("cfg", [{}, {"embeddings": {"enabled": False}}, {"embeddings": None}])
def test_disabled_or_missing_block_skips_without_loading(monkeypatch, cfg):
    monkeypatch.setattr(embed, "load_embedder", lambda d: pytest.fail("the model should not be loaded"))
    lines = []
    assert embed.safe_embed(cfg, {}, lines.append) is False
    assert lines == ["Embeddings (Postgres) skipped: disabled"]


def test_unset_env_var_skips_without_loading(monkeypatch, on):
    monkeypatch.delenv("AZURE_DATABASE_URL", raising=False)
    monkeypatch.setattr(embed, "load_embedder", lambda d: pytest.fail("the model should not be loaded"))
    lines = []
    assert embed.safe_embed(on, {}, lines.append, **AZURE) is False
    assert lines == ["Embeddings (Azure Postgres) skipped: AZURE_DATABASE_URL not set"]


def test_missing_model_logs_a_skip_line(monkeypatch, on):
    # the real load_embedder on an empty dir: a reason, never an exception
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setattr(embed, "connect", lambda url: pytest.fail("connect should not be called"))
    lines = []
    assert embed.safe_embed(on, {}, lines.append) is False
    assert len(lines) == 1 and lines[0].startswith("Embeddings (Postgres) skipped: model files missing")
    assert "--download" in lines[0]


def test_happy_path_logs_one_line(fakes, on):
    lines = []
    assert embed.safe_embed(on, {}, lines.append) is True
    assert len(lines) == 1 and lines[0].startswith("Embeddings (Postgres): 2 new vectors, CV updated in ")
    assert fakes["syncs"][0][0] == URL and fakes["syncs"][0][2] == "Junior Python developer"


def test_missing_cv_profile_is_noted_not_an_error(fakes, on, tmp_path, monkeypatch):
    (tmp_path / "cv.txt").unlink()
    monkeypatch.setattr(embed, "run_sync", lambda conn, e, cv, memo: fakes["syncs"][-1].append(cv) or (3, False))
    lines = []
    assert embed.safe_embed(on, {}, lines.append) is True
    assert fakes["syncs"][0][1] is None  # no CV text, so sync writes no CV row
    assert lines[0].startswith("Embeddings (Postgres): 3 new vectors, no CV (cv.txt missing) in ")


def test_model_and_memo_are_shared_by_both_databases(fakes, on):
    state, lines = {}, []
    assert embed.safe_embed(on, state, lines.append) is True
    assert embed.safe_embed(on, state, lines.append, **AZURE) is True
    assert fakes["loads"] == 1
    assert [s[0] for s in fakes["syncs"]] == [URL, AZURE_URL]
    assert fakes["syncs"][0][1] is fakes["syncs"][1][1]  # the same memo dict, so Azure reuses local vectors
    assert lines[1].startswith("Embeddings (Azure Postgres): 2 new vectors")


def test_failed_model_load_is_tried_once_per_run(monkeypatch, on):
    loads = []
    monkeypatch.setattr(embed, "load_embedder", lambda d: loads.append(d) or (None, "model files missing in x"))
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("AZURE_DATABASE_URL", AZURE_URL)
    state, lines = {}, []
    embed.safe_embed(on, state, lines.append)
    embed.safe_embed(on, state, lines.append, **AZURE)
    assert len(loads) == 1
    assert lines == ["Embeddings (Postgres) skipped: model files missing in x",
                     "Embeddings (Azure Postgres) skipped: model files missing in x"]


def test_error_inside_sync_never_propagates_and_hides_the_url(fakes, on, monkeypatch):
    def boom(conn, embedder, cv, memo):
        raise RuntimeError(f"lost {AZURE_URL} (password Azur3Secret)\nsecond line")
    monkeypatch.setattr(embed, "run_sync", boom)
    lines = []
    assert embed.safe_embed(on, {}, lines.append, **AZURE) is False
    assert len(lines) == 1 and lines[0].startswith("WARNING: Embeddings (Azure Postgres) skipped: RuntimeError:")
    assert AZURE_URL not in lines[0] and "Azur3Secret" not in lines[0] and "second line" not in lines[0]


def test_error_while_loading_the_model_never_propagates(monkeypatch, on):
    monkeypatch.setenv("DATABASE_URL", URL)

    def boom(model_dir):
        raise MemoryError("out of memory")
    monkeypatch.setattr(embed, "load_embedder", boom)
    lines = []
    assert embed.safe_embed(on, {}, lines.append) is False
    assert lines == ["WARNING: Embeddings (Postgres) skipped: MemoryError: out of memory"]


def raising_log(msg):
    raise OSError("run.log is locked")


def test_raising_log_never_escapes(fakes, on, monkeypatch):
    assert embed.safe_embed(on, {}, raising_log) is True
    assert embed.safe_embed({}, {}, raising_log) is False
    monkeypatch.setattr(embed, "run_sync", lambda *a: 1 / 0)
    assert embed.safe_embed(on, {}, raising_log) is False


def run_load_databases(monkeypatch, cfg, loaded: bool) -> tuple[list, list]:
    order, lines = [], []
    monkeypatch.setattr(job_searcher, "log", lines.append)
    monkeypatch.setattr(job_searcher.jobdb, "safe_load",
                        lambda rows, today, cats, cv, log, **kw: order.append(kw.get("label", "local")) or loaded)
    monkeypatch.setattr(job_searcher.azure_firewall, "update_home_rule", lambda cfg, log: order.append("firewall"))
    monkeypatch.setattr(job_searcher.embed, "safe_embed",
                        lambda cfg, state, log, **kw: order.append("embed " + kw.get("label", "local")) or state)
    job_searcher.load_databases([], "2026-10-01", {"cv_skills": [], **cfg})
    return order, lines


def test_load_databases_embeds_after_each_successful_load(monkeypatch, on):
    order, _ = run_load_databases(monkeypatch, on, loaded=True)
    assert order == ["local", "embed local", "firewall", "Azure Postgres", "embed Azure Postgres"]


def test_load_databases_skips_embedding_when_the_load_failed(monkeypatch, on):
    order, _ = run_load_databases(monkeypatch, on, loaded=False)
    assert order == ["local", "firewall", "Azure Postgres"]


def test_load_databases_still_loads_both_when_embedding_blows_up(fakes, monkeypatch, on):
    order, lines = [], []
    monkeypatch.setattr(job_searcher, "log", lines.append)
    monkeypatch.setattr(job_searcher.jobdb, "safe_load",
                        lambda rows, today, cats, cv, log, **kw: order.append(kw.get("label", "local")) or True)
    monkeypatch.setattr(job_searcher.azure_firewall, "update_home_rule", lambda cfg, log: order.append("firewall"))
    monkeypatch.setattr(embed, "run_sync", lambda *a: 1 / 0)
    job_searcher.load_databases([], "2026-10-01", {"cv_skills": [], **on})
    assert order == ["local", "firewall", "Azure Postgres"]
    assert lines == ["WARNING: Embeddings (Postgres) skipped: ZeroDivisionError: division by zero",
                     "WARNING: Embeddings (Azure Postgres) skipped: ZeroDivisionError: division by zero"]
    assert fakes["loads"] == 1
