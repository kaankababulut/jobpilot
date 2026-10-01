# Learning Log – Kaan Kababulut

Add a line whenever you finish learning a skill or building a project. Upload this file to your Copilot agent's Knowledge next to the CV, so it knows what you've learned since the CV was written.
When a skill here is solid enough to go on your CV, also add it to `cv_skills` in config.json, so the daily Match Scores reflect it.

| Date | Skill / Project | What I built or did | Evidence (GitHub link, certificate) | On CV yet? |
|------|-----------------|---------------------|--------------------------------------|------------|
| 2026-10-01 | PostgreSQL schema design | Reworked the schema: `jobs` with natural key `UNIQUE(source, source_id)` plus surrogate `BIGINT` id, `skills`, `job_skills`, `runs` | `db/init/001_schema.sql`, commit 539c6d8 | No |
| 2026-10-01 | Idempotent upserts (SQL) | `INSERT ... ON CONFLICT DO UPDATE ... WHERE last_seen <=` so loads can be repeated in any order | `jobpilot/db.py`, commit b2ccf7f | No |
| 2026-10-01 | Python + psycopg 3, transactions | `safe_load`: one transaction per load, rollback on error, timeouts, password redacted from logs | `jobpilot/db.py`, commits 819825a, b6cb97f | No |
| 2026-10-01 | Data backfill / ETL | `python -m jobpilot.backfill`: 5 Excel files, 540 rows → 270 distinct jobs; re-runs leave the count at 270 | `jobpilot/backfill.py`, commit 00389fe | No |
| 2026-10-01 | Testing with pytest | 140 default tests + 13 opt-in database tests (`-m db`) in a throwaway schema | `tests/`, `pytest.ini`, commit 5a52f7b | No |
| 2026-10-01 | Docker Compose | Local Postgres 17 + pgvector, bound to 127.0.0.1, secrets in `.env` | `docker-compose.yml`, `.env.example` | No |
| 2026-10-01 | CI with GitHub Actions | Every PR: default tests, then migrations on a fresh pgvector service container, then DB tests with skip turned into fail | `.github/workflows/ci.yml`, commit 48a8283 | No |
| 2026-10-01 | Database migrations | Homegrown runner: numbered SQL files, one transaction per file, `schema_migrations` table, `--baseline` for my existing database (270 jobs kept) | `jobpilot/migrate.py`, `db/migrations/`, commits 8989b1c, 5bf2f9f, aa8d9f9 | No |
| 2026-10-01 | Safe read-only SQL | Parameterised queries with filters, LIKE escaping, `has_more` paging, skill demand with share of jobs; read-only connection option | `jobpilot/queries.py`, `jobpilot/db.py`, commits 24417bb, bd9e6c1, 883d228 | No |
| 2026-10-01 | FastAPI + API security | 5 endpoints (`/health`, `/jobs`, `/jobs/{job_id}`, `/skills`, `/runs`), API key failing closed, constant-time compare, OpenAPI written for LLM tools | `jobpilot/api.py`, commits 709f930, d0b6b09, 00752ea | No |
| 2026-10-01 | Testing with pytest | Suite now 242 default + 56 opt-in database tests | `tests/` | No |

## 2026-10-01: Postgres loader (roadmap step 2)

**What I built.** The daily run now loads its 30-day job window into Postgres after writing the Excel files. A backfill command loads old Excel files. Excel still works exactly as before. I also started tracking Microsoft and low-code skills, and fixed a crash when a day had no new jobs.

**Key concepts in plain words**
- **Idempotent upsert.** "Insert, or update if it already exists." Running the same load twice gives the same result, so re-runs are safe.
- **Natural vs surrogate key.** The natural key is what identifies a job in the real world (`source` + `source_id`). The surrogate key is a number the database makes up (`id`). The natural key stops duplicates; the surrogate key keeps foreign keys small and stable.
- **Transactions and rollback.** All writes of one load either all succeed (commit) or none do (rollback). A crash halfway never leaves half a batch behind.
- **Failing safe vs failing loud.** The daily run runs unattended, so a database problem only logs a warning and Excel still gets written. The backfill is run by hand, so it stops with an error and exit code 1, so I notice.
- **Redacting secrets.** Error messages can contain the connection URL. Before logging, the URL and password are replaced with placeholders.
- **Opt-in integration tests.** Tests that need a real database are marked `db` and skipped by default. The fast suite runs after every edit without Docker; the database tests run when I ask for them.

**Interview questions**
- *How do you stop the same job being inserted twice?* A `UNIQUE (source, source_id)` constraint plus `INSERT ... ON CONFLICT DO UPDATE`. I ran the backfill more than once; the count stayed at 270.
- *What if you load an old file after a newer one?* The update has `WHERE jobs.last_seen <= EXCLUDED.last_seen`, so older data can't overwrite newer data. `first_seen` uses `LEAST`, so it can only move earlier.
- *Why a surrogate id if you already have a natural key?* Foreign keys in `job_skills` stay a small integer, and the id doesn't change if the natural key format changes.
- *What happens if the database is down at 12:00?* `safe_load` logs one line and returns `False`; the Excel files are still written. The next day loads the full 30-day window, so the gap fills itself.
- *Why is the `try` outside the `with` block?* If the error were caught inside, the `with` block would exit normally and commit a partial batch. Outside, the error leaves the block and psycopg rolls back.
- *How do you keep the password out of logs?* A `redact` function replaces the URL and the password (also its URL-decoded form) before logging.
- *Why not delete and re-insert every day?* It throws away `first_seen` history and breaks foreign keys, and a crash between delete and insert loses data.
- *What did you leave out?* A migrations tool, which I need before the next schema change. Also "still listed" tracking and cross-source de-duplication.

## 2026-10-01: API foundation (roadmap step 3)

**What I built.** A read-only FastAPI service over the job database, protected by an API key. The schema now comes from numbered SQL migrations instead of a Docker init script. GitHub Actions runs every test, including the database tests, on each pull request. A smoke test on my real data worked: `/jobs?open_to_you=true&limit=3` returned matches scored 91, 88 and 81, `/skills?days=30` put Python first (159 jobs, 0.589 share), and a request without the key got 401.

**Key concepts in plain words**
- **Migrations vs init scripts.** A Docker init script only runs when the data volume is empty, so it can create a database but never change one. A migration is a numbered SQL file. The runner applies the ones not yet recorded in `schema_migrations`, each in its own transaction, so a failed file leaves nothing behind. An applied file is never edited; a change is a new file.
- **Baseline.** My database existed before migrations, so 001 had in effect already run. `--baseline` writes "001 is applied" into `schema_migrations` without running it. My 270 jobs stayed.
- **CI service containers.** GitHub Actions starts a real Postgres next to the test job and waits until it is healthy. CI first builds it with the migrations, which proves they work on an empty database.
- **Why skip must become fail in CI.** Locally, database tests skip when Docker is off, which is handy. In CI a skip looks green, so a broken database would go unnoticed. `JOBPILOT_REQUIRE_DB=1` makes the tests fail instead.
- **API key, failing closed.** The caller sends `X-API-Key`; it must match `JOBPILOT_API_KEY`. If I forget to set the key, the API answers 503 instead of letting everyone in. "Fail closed" means a mistake locks the door rather than leaving it open.
- **Constant-time compare.** A normal `==` stops at the first wrong character, so an attacker could measure response times and guess the key one character at a time. `secrets.compare_digest` always takes the same time.
- **Read-only in two layers.** Layer 1: the code only runs `SELECT`. Layer 2: the connection sets `default_transaction_read_only=on`, so Postgres rejects any write, even from a bug.
- **Idle in transaction.** In psycopg even a `SELECT` opens a transaction. If the connection then just sits there, it holds a snapshot and locks, which can block a migration. Each request uses a short autocommit connection, so every statement ends at once.
- **Parameters vs LIKE escaping.** `%s` parameters stop SQL injection: user input is always data, never SQL. But inside a `LIKE` pattern, `%` and `_` are still wildcards, so a search for "50%" would match too much. I escape them (backslash first) so the search means the literal text.
- **operation_ids as tool names.** Each endpoint has a fixed `operation_id` like `list_jobs`. Copilot Studio and LLM agents turn the OpenAPI spec into tools and use these as names, and they read the descriptions to decide which tool fits.
- **`has_more` paging.** I ask for one row more than the page size. If it comes back, there is a next page. That avoids a separate `count(*)` query.

**Interview questions**
- *Why write your own migration runner instead of using Alembic?* Alembic is built around SQLAlchemy, which I don't use; my SQL is plain psycopg. One developer and plain SQL files need only a loop, a table and a transaction per file. It's under 200 lines and tested. In a team with an ORM I would use Alembic.
- *How did you move an existing database onto migrations without losing data?* A `--baseline` flag records 001 as applied without running it. It refuses if there's no `jobs` table, so it can't hide a migration that never ran. The 270 jobs stayed.
- *What does your CI do?* On every pull request: the default tests, then the migrations on a fresh Postgres service container, then the database tests. `JOBPILOT_REQUIRE_DB=1` turns a skipped database test into a failure.
- *How is the API secured?* An API key in the `X-API-Key` header, compared in constant time. If the server has no key set, it returns 503 instead of running open. It listens on 127.0.0.1 only until it moves to Azure with HTTPS.
- *Why an API key and not OAuth?* There is one user. Copilot Studio and Power Platform custom connectors support API keys, and that is the next consumer. OAuth would be the choice for many users.
- *How do you make sure the API can't change data?* The code only runs `SELECT` with parameters, and the connection is read-only at the Postgres level, so even a buggy write is rejected. In Azure I'll add a SELECT-only role.
- *Why no connection pool or async?* One user and a few hundred rows. A short connection per request is simple and never stays idle in a transaction. FastAPI runs sync endpoints in a thread pool, so a slow query doesn't block others. I'd add a pool when traffic needs it.
- *How does your paging work, and why not return a total count?* `limit + 1` rows tell me whether `has_more` is true, without a `count(*)`. The sort ends with the id, so `OFFSET` paging never repeats or skips a job.
- *How did you design the API for LLM agents?* Stable `operation_id`s as tool names, descriptions that say when to use each endpoint, enums for fixed values so typos get a 422, and flat response models because Power Platform imports OpenAPI 2.0.
