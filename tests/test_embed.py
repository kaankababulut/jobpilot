import hashlib
import io
import math
import os

import pytest

from jobpilot import embed
from jobpilot.embed import Embedder, embed_text, text_hash, to_pgvector
from tests.fakes import FakeEmbedder

JOB = {"title": "Junior Data Engineer", "company": "Acme", "skills": ["Python", "SQL"],
       "description": "Build pipelines."}


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b))  # both are unit length


def test_embed_text_field_order_and_prefix():
    assert embed_text(JOB) == "passage: Junior Data Engineer | Acme | Python, SQL | Build pipelines."


def test_embed_text_accepts_skill_dicts_from_job_detail():
    job = {**JOB, "skills": [{"name": "Python", "category": "Language", "on_cv": True}, {"name": "SQL"}]}
    assert embed_text(job) == embed_text(JOB)


def test_embed_text_skips_none_and_empty_parts():
    assert embed_text({"title": "Intern", "company": None, "skills": [], "description": "  "}) == "passage: Intern"
    assert embed_text({"title": "Intern", "skills": ["", None, "Go"]}) == "passage: Intern | Go"
    assert embed_text({}) == "passage: "


def test_embed_text_collapses_whitespace():
    job = {"title": "  Junior\tDev ", "company": "Acme\n", "description": "Line one.\n\n  Line   two."}
    assert embed_text(job) == "passage: Junior Dev | Acme | Line one. Line two."


def test_text_hash_is_stable_and_changes_with_text():
    h = text_hash(embed_text(JOB))
    assert h == text_hash(embed_text(dict(JOB))) and len(h) == 64
    assert h != text_hash(embed_text({**JOB, "description": "Build dashboards."}))


def test_fake_embedder_is_deterministic_unit_length_and_fits_protocol():
    fake: Embedder = FakeEmbedder()  # type check: the fake satisfies the Protocol
    v = fake.embed_query("python sql pipelines")
    assert v == FakeEmbedder().embed_passages(["python sql pipelines"])[0]
    assert len(v) == fake.dims == 384 and math.isclose(math.sqrt(sum(x * x for x in v)), 1.0)
    assert math.isclose(math.sqrt(sum(x * x for x in fake.embed_query(""))), 0.0)  # no words: zeros, no crash


def test_fake_embedder_shared_words_score_higher():
    fake = FakeEmbedder()
    q = fake.embed_query("python data engineer")
    near, far = fake.embed_passages(["junior python data engineer", "graphic designer figma branding"])
    assert cosine(q, near) > cosine(q, far)


def test_to_pgvector_format():
    assert to_pgvector([0.1, -2, 0.0]) == "[0.1,-2.0,0.0]"
    assert to_pgvector([]) == "[]"


# --- load_embedder and the pinned download, with no model and no network ---

def test_load_embedder_missing_dir_returns_reason_not_error(tmp_path):
    embedder, reason = embed.load_embedder(str(tmp_path / "nope"))
    assert embedder is None and "missing" in reason and "--download" in reason


def test_load_embedder_bad_files_returns_reason_not_error(tmp_path):
    for name in embed.FILES:
        (tmp_path / name).write_bytes(b"not a model")
    embedder, reason = embed.load_embedder(str(tmp_path))
    assert embedder is None and reason.startswith("model not loaded")


def fake_hub(monkeypatch, bodies: dict[str, bytes]) -> list[str]:
    """Serve files from memory instead of huggingface.co; returns the list of requested URLs."""
    calls = []

    def urlopen(url, timeout=None):
        calls.append(url)
        return io.BytesIO(bodies[url.rsplit("/", 1)[1]])  # BytesIO is a context manager with .read(n)
    monkeypatch.setattr(embed.urllib.request, "urlopen", urlopen)
    return calls


def pin(monkeypatch, bodies: dict[str, bytes]) -> None:
    files = {name: (f"onnx/{name}", hashlib.sha256(body).hexdigest()) for name, body in bodies.items()}
    monkeypatch.setattr(embed, "FILES", files)


def test_download_verifies_writes_and_skips_verified_files(tmp_path, monkeypatch):
    bodies = {"m.onnx": b"weights" * 1000, "tok.json": b"{}"}
    pin(monkeypatch, bodies)
    calls = fake_hub(monkeypatch, bodies)
    embed.download(str(tmp_path), log=lambda _: None)
    assert (tmp_path / "m.onnx").read_bytes() == bodies["m.onnx"] and (tmp_path / "tok.json").read_bytes() == b"{}"
    assert all(f"/resolve/{embed.HF_REVISION}/onnx/" in u for u in calls)  # always the pinned commit
    embed.download(str(tmp_path), log=lambda _: None)
    assert len(calls) == 2  # second run: both files already verified, nothing fetched


def test_download_sha_mismatch_raises_and_leaves_no_file(tmp_path, monkeypatch):
    pin(monkeypatch, {"m.onnx": b"expected"})
    fake_hub(monkeypatch, {"m.onnx": b"tampered"})
    with pytest.raises(ValueError, match="sha256 mismatch"):
        embed.download(str(tmp_path), log=lambda _: None)
    assert os.listdir(tmp_path) == []  # neither the file nor its .part temp file


def test_download_replaces_a_corrupt_file(tmp_path, monkeypatch):
    pin(monkeypatch, {"m.onnx": b"good"})
    calls = fake_hub(monkeypatch, {"m.onnx": b"good"})
    (tmp_path / "m.onnx").write_bytes(b"half a downl")
    embed.download(str(tmp_path), log=lambda _: None)
    assert (tmp_path / "m.onnx").read_bytes() == b"good" and len(calls) == 1


def test_download_mismatch_keeps_the_previous_file(tmp_path, monkeypatch):
    pin(monkeypatch, {"m.onnx": b"good"})
    fake_hub(monkeypatch, {"m.onnx": b"evil"})
    (tmp_path / "m.onnx").write_bytes(b"old")
    with pytest.raises(ValueError):
        embed.download(str(tmp_path), log=lambda _: None)
    assert os.listdir(tmp_path) == ["m.onnx"] and (tmp_path / "m.onnx").read_bytes() == b"old"


def test_onnx_embedder_model_name_carries_variant_and_revision():
    assert embed.OnnxEmbedder.model == f"multilingual-e5-small@qint8-{embed.HF_REVISION[:7]}"
    assert embed.OnnxEmbedder.dims == 384
