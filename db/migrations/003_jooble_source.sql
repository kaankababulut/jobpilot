-- Migration 003: allow 'jooble' as a jobs.source (the Jooble Türkiye source in job_searcher.py).
-- Never edit a migration that has been applied: databases that recorded it won't run it again.
--
-- 001 declared the CHECK inline, so Postgres named it itself (normally jobs_source_check). Rather than
-- trust that name, drop whichever CHECK constraints are on the source column, then add one with a fixed name.
-- No new table, so nothing new to grant to jobpilot_api.

DO $$
DECLARE c TEXT;
BEGIN
  FOR c IN
    SELECT con.conname FROM pg_constraint con
    JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = ANY (con.conkey)
    -- 'jobs'::regclass follows search_path, so the test suite's throwaway schemas get it too
    WHERE con.conrelid = 'jobs'::regclass AND con.contype = 'c' AND att.attname = 'source'
  LOOP
    EXECUTE format('ALTER TABLE jobs DROP CONSTRAINT %I', c);
  END LOOP;
END $$;

ALTER TABLE jobs ADD CONSTRAINT jobs_source_check CHECK (source IN ('linkedin', 'himalayas', 'jooble'));
