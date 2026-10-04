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
| 2026-10-02 | Azure Database for PostgreSQL | Flexible Server (PG 17, B1ms, free tier) in Sweden Central; the daily run loads into it as a second, fail-safe target | `job_searcher.py`, `jobpilot/db.py`, commits 435c7d5, bbda844 | No |
| 2026-10-02 | Database security (least privilege) | Migration 002: SELECT-only role `jobpilot_api`, read-only default, connection limit 5, no access to `schema_migrations` | `db/migrations/002_api_reader_role.sql`, commit e665eaf | No |
| 2026-10-02 | Performance: network round trips | Pipelined `executemany` + one `unnest` insert; Azure backfill went from 4 min (339 jobs) to 19 s (389 jobs) | `jobpilot/db.py`, commit 519cb2d | No |
| 2026-10-03 | Resilience: retries | Whole-round fetch retry (3 rounds, 120 s) only when every source had a network error, after a lost day | `job_searcher.py`, commit 8769725 (PR #6) | No |
| 2026-10-03 | Azure identity (Entra ID, RBAC) | Service principal with a custom role (firewall rules read/write only) on one server; the run moves the firewall rule to today's IP | `jobpilot/azure_firewall.py`, commit 388095f (PR #7) | No |
| 2026-10-03 | Production-ready API | DB-free `/health`, docs off by default, 504 on query timeouts, OpenAPI contract snapshot, pinned dependencies | `jobpilot/api.py`, `docs/openapi.json`, `requirements-api.txt`, commits 90e4ce6, 41b33c6 | No |
| 2026-10-03 | Docker images + CI/CD | Whitelist `.dockerignore`, non-root image, CI builds and pushes `sha-<commit>` tags to ghcr.io after tests pass on `main` | `Dockerfile`, `.dockerignore`, `.github/workflows/ci.yml`, commit c4e50b6 | No |
| 2026-10-04 | Azure Container Apps | Deployed the API (Consumption, 0.25 vCPU, min 0 / max 1 replica, HTTPS) behind a $5 budget; live checks passed | README "Deploy to Azure", PR #8 (287bafe) | No |
| 2026-10-04 | Testing with pytest | Suite now 308 default + 63 opt-in database tests | `tests/` | No |

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

## 2026-10-04: Azure deployment (roadmap step 4)

**What I built.** The API now runs on Azure Container Apps over HTTPS, in front of an Azure PostgreSQL database. The 12:00 run on my PC loads Excel, the local database and the Azure database; each target fails safe on its own. Before the cloud load, the run moves the Azure firewall rule to my current home IP. Everything runs on free tiers under a $5/month budget with alerts. Live checks on 2026-10-04: `/health` 200, `/docs` 404, `/jobs` 401 without the key and 200 in about 0.5 s with it (top open matches 91, 88, 87), `/skills` put Python in 60% of 428 jobs and SQL in 54%, and http redirected to https.

**Key concepts in plain words**
- **Managed database.** Azure runs PostgreSQL for me: patches, backups (7 days) and TLS. I only manage the data, users and firewall.
- **Region restrictions.** Not every region offers every service to every subscription. Germany West Central refused a new Postgres server for my new subscription, so everything is in Sweden Central.
- **Free-tier traps.** "Free" has limits and add-ons that aren't free: high availability, geo-backup, storage autogrow, Defender, and a Log Analytics workspace the portal created by default. I turned them off or set logging to "don't save logs", and set a budget alert from day one. The database's free tier ends after 12 months (around Sep 2027).
- **Scale to zero and cold starts.** With min 0 replicas the app stops when nobody calls it, so it costs nothing. The first request after a pause has to start the container, so it is slower. Max 1 replica caps cost if someone floods it.
- **Container images and immutable tags.** An image is the app plus everything it needs, frozen. CI tags each build with its commit hash and never reuses a tag, so I know exactly what's running, and rollback means picking an older tag.
- **Whitelist `.dockerignore`.** Instead of listing what to leave out (and forgetting something), it leaves out everything and lists what goes in. A new secret file can't end up in the image by accident.
- **Fail closed.** When a setting is missing or wrong, choose the safe outcome. The image turns the API docs off unless someone turns them on; the API refuses data requests when no key is set.
- **Least-privilege roles.** Give each user exactly what it needs. The API's database role can only `SELECT` four tables. A session "read-only" flag can be switched off by the client; a missing privilege can't.
- **Service principal + custom RBAC role.** A service principal is an identity for a program, not a person. Azure RBAC decides what it may do, and where. Mine has a custom role with two actions (read and write firewall rules), on one server only. A leaked secret can move one firewall rule, nothing else.
- **Dynamic IP and firewall automation.** My home IP changes almost daily and Azure drops unknown IPs without an error, so the load just times out. The run updates the rule to today's IP, then probes the database until it answers.
- **Shared outbound IPs trade-off.** Container Apps on the cheap plan sends traffic from a shared pool of about 170 IPs, more than the firewall's ~128-rule limit. A fixed IP or a private network costs more than my budget, so I allowed Azure services and rely on strong passwords, TLS and the SELECT-only role. In production: VNet integration and a private endpoint.
- **Network round trips and pipelining.** Each request to a remote database waits for the answer before the next one. Pipelining sends many statements, then waits once. That turned a 4-minute load into 19 seconds.
- **Dev/prod parity.** Local Docker, CI and Azure all run PostgreSQL 17, so tests mean something for production.
- **Retry only what can recover.** A network error after waking the PC can fix itself, so the run waits and retries. A 401 from a server won't, so it isn't retried.

**Lessons from mistakes**
- Pasting a password into psql's hidden prompt through `docker exec` silently changed it, twice. Now a script sets passwords (`ALTER ROLE ... PASSWORD` over the admin connection) and copies them to the clipboard.
- A screenshot showed a secret value. I rotated it.
- Wi-Fi said "connected" before traffic actually worked, and a day of data was lost. That's why the retry exists.

**Interview questions**
- *How is your project deployed?* The API is a Docker image built by GitHub Actions after the tests pass, stored in ghcr.io with a commit-hash tag, and run on Azure Container Apps over HTTPS. Data sits in Azure Database for PostgreSQL Flexible Server. A daily job on my PC loads it.
- *How do you keep cloud costs under control?* Free tiers, a $5 budget with alerts at 50% and 100% actual and 100% forecast, max 1 replica, scale to zero, and no paid add-ons like HA, geo-backup or Log Analytics. I know the free database tier ends after 12 months.
- *What's the trade-off of scaling to zero?* It costs nothing when idle, but the first request after a pause is slower (a cold start). For one user that's fine; for a user-facing product I'd keep one replica warm.
- *Your database allows Azure services. Isn't that insecure?* It's a trade-off I chose on purpose. The tight version (home IP plus the app's IPs) doesn't work because the app's outbound pool is about 170 shared IPs, over the rule limit, and fixing that costs more than my budget. I compensate with long random passwords, TLS and a SELECT-only role. In production I'd use VNet integration and a private endpoint, so the database has no public address.
- *How does the API avoid writing to the database?* Three layers: the code only runs parameterised `SELECT`s, the session is read-only, and the role only has `SELECT` on four tables. The last one is enforced by Postgres no matter what the client does.
- *How did you handle the changing home IP?* A service principal with a custom role that can only read and write firewall rules on that one server. Before the cloud load, the run points the rule at today's IP and probes the database, within a 3-minute budget. If it fails, it logs one line and the rest of the run carries on.
- *How did you make the cloud load faster?* Measured first: 4 minutes for 339 jobs, because each job needed about 4 round trips to a remote server. Pipelined `executemany` and one `unnest` insert brought it to about 16 round trips per batch: 19 s for 389 jobs, with the same per-row logic and tests.
- *How do you deploy and roll back?* Merge to `main`, CI builds `sha-<commit>`, and I create a new revision with that tag in the portal. Rollback is a new revision with the previous tag. I left out automatic deploys (OIDC) because I deploy rarely and it's another identity to secure.
- *What would you change for a real production system?* Private networking, a separate writer role for the loader, Key Vault, automated deploys, stored logs with alerts, and rate limiting or API Management.
