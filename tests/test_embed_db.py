"""Integration tests for jobpilot.embed.sync_embeddings against the local Postgres (pytest -m db),
with FakeEmbedder in place of the real model. Each test runs in the pg fixture's throwaway schema."""
import pytest

from jobpilot import embed
from jobpilot.embed import embed_text, sync_embeddings, text_hash
from test_db import one
from tests.fakes import FakeEmbedder

pytestmark = pytest.mark.db


class CountingEmbedder(FakeEmbedder):
    """FakeEmbedder that remembers every text it embedded, to prove what was (not) recomputed."""
    def __init__(self, model: str = "fake@test"):
        self.model = model
        self.passages: list[str] = []
        self.queries: list[str] = []

    def embed_passages(self, texts):
        self.passages.extend(texts)
        return super().embed_passages(texts)

    def embed_query(self, text):
        self.queries.append(text)
        return super().embed_query(text)


def add_job(conn, source_id: str, title: str = "Junior Python Developer", description: str | None = "Python and SQL.",
            skills: tuple[str, ...] = ()) -> int:
    job = one(conn, "INSERT INTO jobs (source, source_id, title, company, description, first_seen, last_seen) "
                    "VALUES ('linkedin', %s, %s, 'Acme', %s, current_date, current_date) RETURNING id",
              (source_id, title, description))[0]
    for name in skills:
        sid = one(conn, "INSERT INTO skills (name, category) VALUES (%s, 'Language') "
                        "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING skill_id", (name,))[0]
        conn.execute("INSERT INTO job_skills (job_id, skill_id) VALUES (%s, %s)", (job, sid))
    return job


def stored(conn, model: str = "fake@test") -> dict[int, str]:
    return dict(conn.execute("SELECT job_id, text_hash FROM job_embeddings WHERE model = %s", (model,)).fetchall())


def test_first_run_writes_every_job_and_a_rerun_writes_none(pg):
    jobs = [add_job(pg, str(i), title=f"Job {i}", skills=("Python", "SQL")) for i in range(3)]
    e = CountingEmbedder()
    assert sync_embeddings(pg, e) == 3
    assert set(stored(pg)) == set(jobs)
    assert one(pg, "SELECT DISTINCT dims, vector_dims(embedding) FROM job_embeddings") == (384, 384)
    assert sync_embeddings(pg, e) == 0
    assert len(e.passages) == 3  # nothing re-embedded


def test_hash_matches_embed_text_with_sorted_skills(pg):
    job = add_job(pg, "1", skills=("SQL", "Python", "Docker"))
    sync_embeddings(pg, FakeEmbedder())
    want = embed_text({"title": "Junior Python Developer", "company": "Acme", "skills": ["Docker", "Python", "SQL"],
                       "description": "Python and SQL."})
    assert stored(pg) == {job: text_hash(want)}


def test_changed_description_re_embeds_only_that_job(pg):
    same, changed = add_job(pg, "1"), add_job(pg, "2")
    sync_embeddings(pg, FakeEmbedder())
    before = stored(pg)
    pg.execute("UPDATE jobs SET description = 'Now with Kubernetes.' WHERE id = %s", (changed,))
    e = CountingEmbedder()
    assert sync_embeddings(pg, e) == 1
    assert len(e.passages) == 1 and "Kubernetes" in e.passages[0]
    after = stored(pg)
    assert after[same] == before[same] and after[changed] != before[changed]


def test_whitespace_only_change_does_not_re_embed(pg):
    job = add_job(pg, "1")
    sync_embeddings(pg, FakeEmbedder())
    pg.execute("UPDATE jobs SET description = 'Python   and\nSQL.' WHERE id = %s", (job,))
    assert sync_embeddings(pg, FakeEmbedder()) == 0


def test_changed_skills_re_embed(pg):
    job = add_job(pg, "1", skills=("Python",))
    sync_embeddings(pg, FakeEmbedder())
    add_job(pg, "2", skills=("Go",))  # creates the Go skill
    pg.execute("INSERT INTO job_skills (job_id, skill_id) SELECT %s, skill_id FROM skills WHERE name = 'Go'", (job,))
    assert sync_embeddings(pg, FakeEmbedder()) == 2  # job 2 is new, job 1 gained a skill


def test_each_model_gets_its_own_rows(pg):
    job = add_job(pg, "1")
    sync_embeddings(pg, CountingEmbedder("model-a"))
    assert sync_embeddings(pg, CountingEmbedder("model-b")) == 1  # a's vector doesn't count for b
    assert one(pg, "SELECT count(*) FROM job_embeddings WHERE job_id = %s", (job,))[0] == 2
    assert sync_embeddings(pg, CountingEmbedder("model-a")) == 0


def test_memo_reuses_vectors_across_databases(pg):
    # the same connection stands in for both databases: wipe the rows, then sync again with the same memo
    for i in range(3):
        add_job(pg, str(i), title=f"Job {i}")
    e, memo = CountingEmbedder(), {}
    assert sync_embeddings(pg, e, cv_text="Python developer", memo=memo) == 3
    pg.execute("DELETE FROM job_embeddings")
    pg.execute("DELETE FROM cv_embeddings")
    assert sync_embeddings(pg, e, cv_text="Python developer", memo=memo) == 3  # still written to the "second" DB
    assert len(e.passages) == 3 and len(e.queries) == 1  # but computed once
    assert one(pg, "SELECT count(*) FROM cv_embeddings")[0] == 1


def test_shared_memo_never_crosses_models(pg):
    for i in range(2):
        add_job(pg, str(i), title=f"Job {i}")
    a, b, memo = CountingEmbedder("model-a"), CountingEmbedder("model-b"), {}  # same dims (384), same texts
    assert sync_embeddings(pg, a, cv_text="Python developer", memo=memo) == 2
    assert sync_embeddings(pg, b, cv_text="Python developer", memo=memo) == 2
    assert (len(a.passages), len(a.queries)) == (2, 1)
    assert (len(b.passages), len(b.queries)) == (2, 1)  # b computed its own vectors, not a's from the memo
    assert set(memo) == {"model-a", "model-b"}


def test_read_only_connection_raises_and_writes_nothing(pg):
    from psycopg import errors
    add_job(pg, "1")
    pg.autocommit = False
    try:
        pg.execute("SET TRANSACTION READ ONLY")  # what connect(read_only=True) gives the API, for this transaction
        with pytest.raises(errors.ReadOnlySqlTransaction):
            sync_embeddings(pg, FakeEmbedder(), cv_text="Python developer")
    finally:
        pg.rollback()
        pg.autocommit = True
    assert one(pg, "SELECT count(*) FROM job_embeddings")[0] == 0
    assert one(pg, "SELECT count(*) FROM cv_embeddings")[0] == 0


def test_identical_texts_are_embedded_once(pg):
    a, b = add_job(pg, "1"), add_job(pg, "2")  # the same posting under two ids (a repost)
    e = CountingEmbedder()
    assert sync_embeddings(pg, e) == 2
    assert len(e.passages) == 1
    assert stored(pg)[a] == stored(pg)[b]


def test_cv_row_written_once_and_updated_only_when_the_text_changes(pg):
    e = CountingEmbedder()
    sync_embeddings(pg, e, cv_text="Python  and SQL\nstudent")
    assert e.queries == ["query: Python and SQL student"]  # embedded as a query, whitespace collapsed
    first = one(pg, "SELECT text_hash, updated_at FROM cv_embeddings WHERE model = 'fake@test'")
    assert first[0] == text_hash("query: Python and SQL student")
    sync_embeddings(pg, e, cv_text="Python and SQL student")  # only formatting differs
    assert len(e.queries) == 1
    assert one(pg, "SELECT text_hash, updated_at FROM cv_embeddings") == first
    sync_embeddings(pg, e, cv_text="Python, SQL and Docker student")
    assert len(e.queries) == 2
    assert one(pg, "SELECT text_hash FROM cv_embeddings")[0] != first[0]


@pytest.mark.parametrize("cv_text", [None, "", "  \n "])
def test_no_cv_text_writes_no_cv_row(pg, cv_text):
    sync_embeddings(pg, FakeEmbedder(), cv_text=cv_text)
    assert one(pg, "SELECT count(*) FROM cv_embeddings")[0] == 0


def test_job_with_no_text_is_skipped_and_its_stale_vector_dropped(pg):
    blank = add_job(pg, "1", title=" ", description=None)  # title is NOT NULL but can still be blank
    full = add_job(pg, "2")
    pg.execute("UPDATE jobs SET company = NULL WHERE id = %s", (blank,))
    e = CountingEmbedder()
    assert sync_embeddings(pg, e) == 1
    assert set(stored(pg)) == {full}
    # a job that had text, then lost it: the old vector would describe text that's gone
    pg.execute("UPDATE jobs SET title = '', company = NULL, description = NULL WHERE id = %s", (full,))
    assert sync_embeddings(pg, e) == 0
    assert stored(pg) == {}


def test_commits_per_chunk_so_a_crash_keeps_earlier_chunks(pg, monkeypatch):
    for i in range(5):
        add_job(pg, str(i), title=f"Job {i}")
    pg.autocommit = False  # like the CLI's connection, so commits really matter
    monkeypatch.setattr(embed, "CHUNK", 2)

    class Crashing(CountingEmbedder):
        def embed_passages(self, texts):
            if len(self.passages) >= 4:
                raise RuntimeError("model crashed")
            return super().embed_passages(texts)

    try:
        with pytest.raises(RuntimeError):
            sync_embeddings(pg, Crashing())
        pg.rollback()
        assert len(stored(pg)) == 4  # two chunks committed before the third failed
        assert sync_embeddings(pg, CountingEmbedder()) == 1  # the next run only does what's left
    finally:
        pg.rollback()
        pg.autocommit = True
