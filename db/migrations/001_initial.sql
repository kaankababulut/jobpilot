-- Migration 001: the initial schema, applied by `python -m jobpilot.migrate`.
-- Never edit a migration that has been applied: databases that recorded it won't run it again.
-- New schema changes go in a new numbered file (002_name.sql, ...).

CREATE EXTENSION IF NOT EXISTS vector;

-- One row per job posting. A surrogate id keeps foreign keys small and stable;
-- (source, source_id) is the natural key that makes re-loading idempotent.
CREATE TABLE jobs (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- CHECK because a typo'd source would silently create a second key for the same job.
    source           TEXT NOT NULL CHECK (source IN ('linkedin', 'himalayas')),
    source_id        TEXT NOT NULL,            -- LinkedIn numeric id / Himalayas guid URL
    region           TEXT,
    title            TEXT NOT NULL,
    company          TEXT,
    location         TEXT,
    work_type        TEXT CHECK (work_type IN ('Remote', 'Remote?', 'Hybrid', 'On-site')),
    open_to_you      BOOLEAN NOT NULL DEFAULT FALSE,
    restrictions     TEXT[] NOT NULL DEFAULT '{}',
    red_flags        TEXT[] NOT NULL DEFAULT '{}',
    date_posted      DATE,
    employment_type  TEXT,
    seniority        TEXT,
    salary           TEXT,                     -- NULL when 'Not disclosed'
    apply_url        TEXT,
    match_score      SMALLINT CHECK (match_score BETWEEN 0 AND 100),
    years_required   SMALLINT,
    description      TEXT,
    first_seen       DATE NOT NULL,            -- earliest Run Date seen; only ever moves earlier
    last_seen        DATE NOT NULL,            -- latest Run Date that wrote this row
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, source_id)                 -- the idempotency key used by ON CONFLICT
);

-- Skills are a separate table so "which skills are rising?" is a simple GROUP BY.
CREATE TABLE skills (
    skill_id  INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name      TEXT UNIQUE NOT NULL,
    category  TEXT NOT NULL,                   -- 'Unknown' if a name isn't in SKILLS any more
    on_cv     BOOLEAN NOT NULL DEFAULT FALSE   -- refreshed from config.json on every load
);

CREATE TABLE job_skills (
    job_id    BIGINT REFERENCES jobs(id) ON DELETE CASCADE,
    skill_id  INT    REFERENCES skills(skill_id),
    PRIMARY KEY (job_id, skill_id)
);

-- One row per load, so we can see what each daily run or backfill actually changed.
CREATE TABLE runs (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN ('daily', 'backfill')),
    run_date      DATE NOT NULL,
    loaded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    rows_offered  INT NOT NULL,
    rows_written  INT NOT NULL                 -- inserted or updated (older data is ignored)
);

CREATE INDEX jobs_first_seen_idx ON jobs (first_seen);
CREATE INDEX jobs_open_idx ON jobs (open_to_you) WHERE open_to_you;
-- The PK covers lookups by job_id; this one serves "jobs with skill X" queries.
CREATE INDEX job_skills_skill_idx ON job_skills (skill_id);
