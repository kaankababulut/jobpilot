-- Runs automatically the first time the database container starts (empty volume).
-- To re-run after editing: docker compose down -v && docker compose up -d  (this wipes the data)

CREATE EXTENSION IF NOT EXISTS vector;

-- One row per job posting, keyed by the source's own id so re-loading is idempotent.
CREATE TABLE jobs (
    job_id           TEXT PRIMARY KEY,
    source           TEXT NOT NULL,            -- 'linkedin' | 'himalayas'
    region           TEXT,                     -- search region, e.g. 'Worldwide', 'Türkiye'
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
    salary           TEXT,
    apply_url        TEXT,
    match_score      SMALLINT CHECK (match_score BETWEEN 0 AND 100),
    years_required   SMALLINT,
    description      TEXT,
    first_seen       DATE NOT NULL DEFAULT CURRENT_DATE,
    last_seen        DATE NOT NULL DEFAULT CURRENT_DATE
);

-- Skills are a separate table so "which skills are rising?" is a simple GROUP BY.
CREATE TABLE skills (
    skill_id  SERIAL PRIMARY KEY,
    name      TEXT UNIQUE NOT NULL,
    category  TEXT NOT NULL,
    on_cv     BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE job_skills (
    job_id    TEXT REFERENCES jobs(job_id) ON DELETE CASCADE,
    skill_id  INT  REFERENCES skills(skill_id),
    PRIMARY KEY (job_id, skill_id)
);

CREATE INDEX jobs_first_seen_idx ON jobs (first_seen);
CREATE INDEX jobs_open_idx ON jobs (open_to_you) WHERE open_to_you;
