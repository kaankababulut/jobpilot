"""Integration tests for migration 005 (job_embeddings, cv_embeddings). Needs the local Postgres (pytest -m db).
The pg fixture has already applied every migration to its throwaway schema; its search_path ends in public,
where 001 created the vector extension, so the `vector` type and vector_dims() resolve there."""
import pytest
from psycopg import errors

from jobpilot.embed import to_pgvector
from test_db import one
from test_feedback_schema_db import FEEDBACK_ROLE, add_job, as_role
from test_migrate_db import recorded, table_exists

pytestmark = pytest.mark.db

NEW_TABLES = ("job_embeddings", "cv_embeddings")


def add_vector(conn, job_id: int, model: str = "m1", vec=(1.0, 0.0, 0.0), dims: int = 3) -> None:
    conn.execute("INSERT INTO job_embeddings (job_id, model, embedding, dims, text_hash) "
                 "VALUES (%s, %s, %s::vector, %s, 'h')", (job_id, model, to_pgvector(vec), dims))


def test_005_creates_both_tables(pg):
    for t in NEW_TABLES:
        assert table_exists(pg, t), t
    assert ("005", "embeddings") in recorded(pg)


def test_vector_round_trips(pg):
    job = add_job(pg, "1")
    add_vector(pg, job, vec=(0.5, -0.25, 1.0))
    assert one(pg, "SELECT embedding::text, vector_dims(embedding) FROM job_embeddings") == ("[0.5,-0.25,1]", 3)


def test_dims_check_rejects_a_mismatched_vector(pg):
    job = add_job(pg, "1")
    with pytest.raises(errors.CheckViolation):
        add_vector(pg, job, vec=(1.0, 0.0), dims=3)  # a model that returned 2 numbers but claims 3


def test_two_models_coexist_for_one_job(pg):
    job = add_job(pg, "1")
    add_vector(pg, job, "m1")
    add_vector(pg, job, "m2", vec=(1.0, 0.0), dims=2)  # another model, another size: same table
    with pytest.raises(errors.UniqueViolation):
        add_vector(pg, job, "m1")  # but only one vector per (job, model)
    assert one(pg, "SELECT count(*) FROM job_embeddings WHERE job_id = %s", (job,))[0] == 2


def test_deleting_a_job_deletes_its_vectors(pg):
    keep, gone = add_job(pg, "1"), add_job(pg, "2")
    add_vector(pg, keep)
    add_vector(pg, gone)
    pg.execute("DELETE FROM jobs WHERE id = %s", (gone,))
    assert pg.execute("SELECT job_id FROM job_embeddings").fetchall() == [(keep,)]


def test_api_role_reads_vectors_but_cannot_write(pg):
    job = add_job(pg, "1")
    add_vector(pg, job)
    pg.execute("INSERT INTO cv_embeddings (model, embedding, text_hash) VALUES ('m1', '[1,0,0]', 'h')")
    with as_role(pg, "jobpilot_api"):
        assert one(pg, "SELECT count(*) FROM job_embeddings")[0] == 1
        assert one(pg, "SELECT count(*) FROM cv_embeddings")[0] == 1
        with pytest.raises(errors.InsufficientPrivilege):
            pg.execute("INSERT INTO cv_embeddings (model, embedding, text_hash) VALUES ('m2', '[1]', 'h')")


@pytest.mark.parametrize("sql", [
    "INSERT INTO job_embeddings (job_id, model, embedding, dims, text_hash) VALUES (1, 'm', '[1]', 1, 'h')",
    "UPDATE job_embeddings SET text_hash = 'x'",
    "DELETE FROM job_embeddings",
])
def test_api_role_cannot_change_job_embeddings(pg, sql):
    with as_role(pg, "jobpilot_api"), pytest.raises(errors.InsufficientPrivilege):
        pg.execute(sql)


@pytest.mark.parametrize("table", NEW_TABLES)
def test_feedback_role_has_no_access(pg, table):
    with as_role(pg, FEEDBACK_ROLE), pytest.raises(errors.InsufficientPrivilege):
        pg.execute(f"SELECT * FROM {table}")
