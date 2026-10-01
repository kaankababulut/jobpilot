# CV bullets (drafts)

Only verified numbers. Anything in [brackets] still needs a real figure.

## JobPilot: Postgres loader (2026-10-01)

- Built an idempotent PostgreSQL loader (Python, psycopg 3, Docker) for a daily job-scraping pipeline; backfilled 540 Excel rows into 270 distinct jobs and 2,041 job–skill links, with repeated loads leaving the job count unchanged.
- Designed a fail-safe load path: one transaction per run with rollback, 5 s / 30 s timeouts and password-redacted logs, so a database outage never stops the daily Excel report.
- Grew the pytest suite to 153 tests (140 default plus 13 opt-in tests against a throwaway Postgres schema) covering row mapping, upserts and error handling.

## JobPilot: API foundation (2026-10-01)

- Built a read-only REST API (FastAPI, PostgreSQL) with 5 endpoints over the job database, secured by an API key that fails closed and is compared in constant time, with read-only enforced in both the SQL code and the database session; OpenAPI operation IDs are designed for use as LLM-agent and Copilot Studio tools.
- Set up GitHub Actions CI that, on every pull request, runs 242 unit tests, builds a fresh pgvector database from versioned SQL migrations and runs 56 database integration tests; wrote the migration runner, including a baseline mode that moved the existing 270-job database onto migrations with no data loss.
