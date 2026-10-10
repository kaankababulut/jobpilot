import math

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
