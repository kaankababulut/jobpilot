-- Migration 004: my feedback on jobs (thumbs up/down) and the applications I've sent, plus a writer role.
-- Never edit a migration that has been applied: databases that recorded it won't run it again.
--
-- Why: the match score is a guess. Labels and applications are the ground truth it gets judged against
-- (the eval set for steps 7-9), and the tracker replaces the spreadsheet I kept by hand.
-- Why a second role: jobpilot_api stays SELECT-only. Whatever records feedback (the Telegram bot) gets its
-- own role that can write these three tables and nothing else, and can't DELETE, so a bug can't wipe history.
-- No sequence grants: GENERATED ALWAYS AS IDENTITY draws ids from an internal sequence that Postgres
-- doesn't permission-check on INSERT (unlike SERIAL), so INSERT on the table is enough (tested).
-- The password is never in a migration. The owner sets it once, in psql:
--   ALTER ROLE jobpilot_feedback LOGIN;  then  \password jobpilot_feedback

-- one label per job, latest wins (PK = idempotency key); score_at_label freezes what the scorer said,
-- so later re-scoring (steps 7-8) can still be compared with what I saw
CREATE TABLE feedback (
    job_id          BIGINT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    label           TEXT NOT NULL CHECK (label IN ('up', 'down')),
    score_at_label  SMALLINT,
    source          TEXT NOT NULL CHECK (source IN ('telegram', 'import')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE applications (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id      BIGINT REFERENCES jobs(id) ON DELETE SET NULL,  -- NULL: applied outside JobPilot
    company     TEXT NOT NULL,
    title       TEXT NOT NULL,
    url         TEXT,
    status      TEXT NOT NULL DEFAULT 'applied'
                CHECK (status IN ('applied', 'assessment', 'interview', 'offer', 'rejected', 'withdrawn')),
    applied_on  DATE NOT NULL DEFAULT current_date,
    notes       TEXT,
    source      TEXT NOT NULL CHECK (source IN ('telegram', 'import', 'manual')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (company, title, applied_on)     -- makes the one-off import safe to re-run
);
-- partial: many applications have no job_id, but a JobPilot job can be applied to only once
CREATE UNIQUE INDEX applications_job_uq ON applications (job_id) WHERE job_id IS NOT NULL;

-- status history, so "how long from applied to interview?" can be answered later
CREATE TABLE application_events (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    application_id  BIGINT NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    status          TEXT NOT NULL,
    note            TEXT,
    at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX application_events_app_idx ON application_events (application_id);

-- the eval set: an explicit label wins; having applied counts as 'up'
CREATE VIEW labelled_jobs AS
SELECT j.id AS job_id, j.source, j.source_id, j.title, j.company, j.description, j.match_score,
       f.score_at_label,
       COALESCE(f.label, CASE WHEN a.id IS NOT NULL THEN 'up' END) AS label,
       COALESCE(f.updated_at, a.created_at) AS labelled_at
FROM jobs j
LEFT JOIN feedback f ON f.job_id = j.id
LEFT JOIN applications a ON a.job_id = j.id
WHERE f.job_id IS NOT NULL OR a.id IS NOT NULL;

-- Roles are cluster-wide, so create only if missing; NOLOGIN until the owner sets a password.
-- CONNECTION LIMIT 3: one bot needs one connection; a leak can't use up the server's slots.
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'jobpilot_feedback') THEN
    CREATE ROLE jobpilot_feedback NOLOGIN CONNECTION LIMIT 3;
  END IF;
  -- current_schema() rather than "public", so the test suite's throwaway schemas get it too
  EXECUTE format('GRANT USAGE ON SCHEMA %I TO jobpilot_feedback', current_schema());
END $$;

GRANT SELECT, INSERT, UPDATE ON feedback, applications, application_events TO jobpilot_feedback;
GRANT SELECT ON jobs TO jobpilot_feedback;  -- to look up the job being labelled

-- the API reads the new tables too; the view runs with its owner's rights on the underlying tables
GRANT SELECT ON feedback, applications, application_events, labelled_jobs TO jobpilot_api;
