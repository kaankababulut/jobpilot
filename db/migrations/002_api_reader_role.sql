-- Migration 002: a SELECT-only role for the deployed API.
-- Never edit a migration that has been applied: databases that recorded it won't run it again.
--
-- Why a role, when the API already connects read-only: default_transaction_read_only is a session
-- setting any client can turn off again (SET ... = off). Privileges are enforced by Postgres itself,
-- so a client logged in as this role can't write even if it tries.
-- CONNECTION LIMIT 5: a flood of API connections can't use up the server's slots and block the 12:00 load.
-- No ALTER DEFAULT PRIVILEGES: each future table is granted to the API on purpose, in its own migration.
-- schema_migrations is deliberately not granted: the API has no reason to read it.
-- The password is never in a migration. The owner sets it once, in psql:
--   ALTER ROLE jobpilot_api LOGIN;  then  \password jobpilot_api

-- Roles are cluster-wide, so create only if missing; NOLOGIN until the owner sets a password.
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'jobpilot_api') THEN
    CREATE ROLE jobpilot_api NOLOGIN CONNECTION LIMIT 5;
  END IF;
  -- current_schema() rather than "public", so the test suite's throwaway schemas get it too
  EXECUTE format('GRANT USAGE ON SCHEMA %I TO jobpilot_api', current_schema());
END $$;

GRANT SELECT ON jobs, skills, job_skills, runs TO jobpilot_api;

-- third layer for any client that connects as this role, not only our API
ALTER ROLE jobpilot_api SET default_transaction_read_only = on;
