"""The real model, opt-in: python -m pytest -q -m model (needs python -m jobpilot.embed --download; never in CI)."""
import math

import pytest

from jobpilot import embed

pytestmark = pytest.mark.model


@pytest.fixture(scope="module")
def embedder():
    e, reason = embed.load_embedder(embed.DEFAULT_MODEL_DIR)
    if e is None:
        pytest.skip(reason)
    return e


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))  # = cosine, since the vectors are unit length


INTERN = embed.embed_text({"title": "Data Analyst Intern", "company": "Acme",
                           "skills": ["SQL", "Excel", "Power BI"],
                           "description": "Join our analytics team as an intern: clean data, write SQL and build dashboards."})
TRUCK = embed.embed_text({"title": "Truck Driver", "company": "Logistics Co",
                          "description": "Long-haul truck driver with a valid CE licence for international routes."})


def test_vectors_are_unit_length_and_384_dims(embedder):
    for v in [embedder.embed_query("python"), *embedder.embed_passages([INTERN, TRUCK])]:
        assert len(v) == embedder.dims == 384 and math.isclose(math.sqrt(dot(v, v)), 1.0, abs_tol=1e-5)


def test_turkish_query_finds_the_english_intern_job(embedder):
    q = embedder.embed_query("veri analisti stajı")  # "data analyst internship"
    intern, truck = embedder.embed_passages([INTERN, TRUCK])
    assert dot(q, intern) > dot(q, truck)


def test_batch_matches_single_and_prefix_is_not_doubled(embedder):
    batch = embedder.embed_passages([INTERN, TRUCK, INTERN[len("passage: "):]])  # third: the same text without its prefix
    assert all(math.isclose(a, b, abs_tol=1e-4) for a, b in zip(batch[0], embedder.embed_passages([INTERN])[0]))
    assert all(math.isclose(a, b, abs_tol=1e-4) for a, b in zip(batch[1], embedder.embed_passages([TRUCK])[0]))
    assert all(math.isclose(a, b, abs_tol=1e-4) for a, b in zip(batch[0], batch[2]))  # prefix added once, only if missing
    assert embedder.embed_query("query: python") == embedder.embed_query("python")


def test_padded_batches_stay_close(embedder):
    # batch_size > 1 pads; the int8 model then scales activations per batch, so vectors move slightly
    padded = embed.OnnxEmbedder(embed.DEFAULT_MODEL_DIR, batch_size=4).embed_passages([INTERN, TRUCK])
    assert dot(padded[0], embedder.embed_passages([INTERN])[0]) > 0.99
