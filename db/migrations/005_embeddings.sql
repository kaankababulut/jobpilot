-- Migration 005: embedding vectors for jobs and the CV (semantic search, roadmap step 7).
-- Never edit a migration that has been applied: databases that recorded it won't run it again.
--
-- Why separate tables, not a column on jobs: a vector belongs to one model. Keying by (job_id, model) lets a
-- new model be filled in next to the old one and switched over without a gap, and vectors from different
-- models are never compared by mistake (their numbers mean different things).
-- `vector` has no fixed size here, so a model with other dimensions fits the same table; the CHECK ties each
-- row to the size its writer declared, so a model returning the wrong length fails loudly instead of being stored.
-- text_hash is the sha256 of the exact text embedded; when it changes, the vector is recomputed.
-- No ANN index: an exact scan over a few thousand rows is fast and has perfect recall. Revisit at ~100k rows.
-- The vector type and vector_dims() come from the extension 001 created in public.

-- ON DELETE CASCADE: a deleted job takes its vectors with it, so search can't return a missing job
CREATE TABLE job_embeddings (
    job_id      BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    model       TEXT NOT NULL,
    embedding   vector NOT NULL,
    dims        SMALLINT NOT NULL,
    text_hash   TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, model),
    CHECK (vector_dims(embedding) = dims)
);

-- one CV vector per model; the CV text itself stays on the PC (cv_profile.txt is git-ignored)
CREATE TABLE cv_embeddings (
    model       TEXT PRIMARY KEY,
    embedding   vector NOT NULL,
    text_hash   TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- the API reads vectors for /similar and cv_similarity; the PC's owner connection writes them.
-- jobpilot_feedback gets nothing: the bot has no use for vectors.
GRANT SELECT ON job_embeddings, cv_embeddings TO jobpilot_api;
