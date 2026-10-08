# Learning Log – Kaan Kababulut

Add a line whenever you finish learning a skill or building a project, with the evidence (file or commit).
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
| 2026-10-04 | OpenAPI 3.1 → Swagger 2.0 | Stdlib converter for the Power Platform connector: nullable → `x-nullable`, enums inlined, API-key scheme kept, error on anything 2.0 can't express; snapshot of `docs/openapi-v2.json`; 19 tests | `jobpilot/openapi2.py`, `tests/test_openapi2.py`, commit 68ad15e (PR #10, merge cc9875e) | No |
| 2026-10-05 | Entra ID user admin | Work user in my own tenant (Copilot Studio rejects personal accounts), MFA with Authenticator, separate Edge profile | `docs/COPILOT_STUDIO.md` §1 | No |
| 2026-10-05 | Power Platform custom connector | Imported the 2.0 spec, API key in `X-API-Key` header, encrypted connection, 5 actions, inside solution `JobPilot`; fixed the "gateway cannot be null" 500 | `docs/COPILOT_STUDIO.md` §2–3 | No |
| 2026-10-05 | Power Automate (scheduled flow) | "JobPilot daily alert": warm-up with run-after, freshness check on `recent_runs`, `list_jobs`, Select + `join`, two conditions, three messages; runs daily at 13:30 | `docs/COPILOT_STUDIO.md` §5 | No |
| 2026-10-05 | HTTP / Telegram Bot API | Bot via @BotFather; `sendMessage` POST from the flow with Secure Inputs, after the managed notification connectors failed | `docs/COPILOT_STUDIO.md` §4–5 | No |
| 2026-10-05 | Copilot Studio (generative orchestration) | Agent "JobPilot Career Assistant": 4 connector tools, no knowledge, web search off, grounded instructions, 10 test questions. Saved; not yet runnable (environment out of credits) | `docs/COPILOT_STUDIO.md` §6, `docs/step5/agent_instructions.md` | No |
| 2026-10-05 | Testing with pytest | Suite now 327 default + 63 opt-in database tests | `tests/` | No |
| 2026-10-08 | Schema design: tracker tables | Migration 004: `feedback` (one label per job, latest wins), `applications` with a partial unique index on `job_id`, `application_events`, and the `labelled_jobs` view as the eval set | `db/migrations/004_feedback_applications.sql`, commit 404e497 | No |
| 2026-10-08 | Database security (writer role) | `jobpilot_feedback`: SELECT/INSERT/UPDATE on three tables, read on `jobs`, no DELETE, connection limit 3, NOLOGIN until a password is set | `db/migrations/004_feedback_applications.sql`, commit 404e497 | No |
| 2026-10-08 | Webhooks / Telegram Bot API | `POST /telegram/webhook`: secret header, owner-only, 1 MB cap, replies in the response body (no bot token on the server), 503/504 vs 200 for Telegram's retries | `jobpilot/telegram.py`, `jobpilot/telegram_webhook.py`, commits 2583a0a, 270cca3 | No |
| 2026-10-08 | Idempotent writes | Labels, ✅ and status changes are safe to repeat, so Telegram's retries can't duplicate rows or history | `jobpilot/feedback.py`, commits e26de10, 270cca3 | No |
| 2026-10-08 | FastAPI (read endpoint) | `GET /applications` with status/open filters and paging; `recorded_via` instead of a second meaning of `source`; contracts regenerated | `jobpilot/api.py`, `jobpilot/queries.py`, commit c12d441 | No |
| 2026-10-08 | Data import / ETL | One-off spreadsheet import: natural-key duplicates skipped, LinkedIn ids matched to jobs, dry run as a rolled-back transaction | `jobpilot/import_applications.py`, commit b103fdc | No |
| 2026-10-08 | Testing with pytest | Suite now 546 default + 120 opt-in database tests | `tests/` | No |

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

## 2026-10-05: Microsoft Power Platform (roadmap step 5)

**What I built.** A Power Platform custom connector over my live API, and a scheduled Power Automate flow that checks today's load and sends my best new open matches (score 70+, up to 10) to Telegram every day at 13:30. It works: the list arrives daily. I also configured a Copilot Studio agent with 4 tools from the same connector and grounded instructions. It is saved but can't answer yet: the environment is out of credits, and I chose not to link billing. Cost: $0. Runbook: `docs/COPILOT_STUDIO.md`.

**Key concepts in plain words**
- **Custom connector and connection.** The connector describes the API (actions, parameters, auth) from an OpenAPI file. A connection is one signed-in instance of it, holding my API key encrypted. Flows and agents point at the connection, so the key is entered once and never appears in them.
- **Swagger 2.0 vs OpenAPI 3.1.** Two versions of the same API-description format. FastAPI writes 3.1; Power Platform imports only 2.0. 2.0 has no `anyOf`, so "string or null" becomes `x-nullable`, and parameters can't reference shared enums, so the values are copied in. My converter refuses anything it can't translate faithfully, rather than producing a connector that lies about the API.
- **Solutions.** A container for Power Platform parts (connector, flow, agent) with a publisher prefix (`kaan`). It lets them be exported and moved between environments together, like a package.
- **Scheduled flows and time zones.** A recurrence trigger with an explicit time zone. Istanbul isn't in the list, so I used another UTC+3 zone; Türkiye has no daylight saving, so the time never drifts.
- **Run after.** By default a step runs only if the previous one succeeded. "Run after: failed, timed out" lets the flow carry on after a warm-up call that hit a cold start.
- **Expressions vs dynamic content.** Dynamic content is a value picked from an earlier step; an expression is a formula like `first(body('recent_runs')?['items'])?['run_date']`. Typed as plain text, a "formula" is just a string, which is why my condition was always false.
- **Secure Inputs.** Hides a step's inputs in the run history. Needed because the Telegram token is in the URL. It doesn't hide the flow definition, so an exported solution is still secret.
- **Webhooks / HTTP action.** Many services take a plain HTTPS POST with a JSON body. Telegram's `sendMessage` is one; the generic HTTP action replaced all the blocked notification connectors.
- **Generative orchestration vs topics.** Topics are hand-built conversation trees triggered by phrases. Generative orchestration lets the model read the tool descriptions and decide which tools to call. That's why the API's `operation_id`s and descriptions were written for LLMs in step 3.
- **Grounding on tools vs knowledge.** Knowledge means uploaded files or websites the agent searches. Tools are live API calls. With tools only and web search off, every fact in an answer has to come from a tool output I can check.
- **Licences vs credits.** A licence (or trial) lets a user build in Copilot Studio. Credits pay for each message the agent answers. I had a trial, so saving worked, but no credits, so the chat didn't.
- **Tenant restrictions.** New and free tenants have some connectors switched off or licensed (Mail, Office 365 Outlook, Teams). What works on a company tenant may not work on a fresh one.

**Lessons from dead ends**
- A private window blocked third-party cookies and silently hid the connector's actions. A separate browser profile keeps the work user apart and the designer working.
- One wrong tickbox ("Connect via on-premises data gateway") made every call fail instantly with a 500 from `gatewayconnector`. The source field in the error pointed at the cause. The fix also needed a new connection, because the old one kept the setting.
- The notification routes failed one by one: Mail (restricted for new tenants), Outlook.com (Unauthorized), mobile push (app retired on 31 Aug 2026), Office 365 Outlook and Teams (licence), Discord (blocked in Türkiye), Gmail (can't share a flow with a custom connector). The generic HTTP action to Telegram worked. Lesson: when the managed connectors are closed, check whether the target has a plain HTTP API.
- "User license not found" meant the Copilot Studio trial had never actually started; it had to be started from the pricing page.
- Credits are a separate gate from licences. I stopped there instead of linking a card for a demo.

**Interview questions**
- *How did you connect Power Platform to your own API?* A custom connector imported from a Swagger 2.0 file that my code generates from the FastAPI spec. It uses API-key auth in the `X-API-Key` header, and the key sits in one encrypted connection that the flow and the agent share.
- *Why did you write a converter instead of editing the spec by hand?* A hand-edited copy drifts the next time an endpoint changes. The converter is tested, a snapshot test catches drift, and it fails with the exact JSON path on anything 2.0 can't express, so a bad connector never gets built silently.
- *What does your flow do if the morning job didn't run?* It reads `recent_runs` first. If the newest load isn't today's daily run, it sends "today's load is missing" instead of "no new matches", so a broken pipeline doesn't look like a quiet day.
- *How do you handle the API's cold start in the flow?* A `health` call wakes it, with no retries, and the next step is set to run after success, failure or timeout. `health` doesn't touch the database, so it's cheap.
- *Where are the secrets?* API key: `.env` and the encrypted connection. Telegram token: `.env` and the HTTP URL with Secure Inputs on, so run history doesn't show it. Never in the docs, the agent instructions or a screenshot of the connector's Test tab.
- *Why Telegram and not email or Teams?* On a free tenant, Mail was restricted, Outlook.com was unauthorized, Teams and Office 365 Outlook need a licence, and the mobile app was retired. Telegram's bot API is one HTTPS POST and free.
- *How do you stop the Copilot Studio agent from hallucinating jobs?* No knowledge sources, web search off, only 4 read-only tools, and instructions that forbid naming any job or skill a tool didn't return. Ten test questions include a probe for a job that doesn't exist and a request for the API key.
- *Is the agent live?* No. It's configured and saved, but the environment has no credits, and I decided not to link pay-as-you-go billing for a portfolio demo. The same API and test questions carry over to a custom Claude agent in step 8 and to the evals in step 9.

## 2026-10-08: Application tracker (roadmap step 5, follow-up)

**What I built.** The Telegram chat that gets the daily alert now also writes. Each job gets 👍 / 👎 / ✅ (applied) buttons, and `/apps`, `/s` and `/add` commands track my applications, replacing the spreadsheet I kept by hand. Taps and commands go to `POST /telegram/webhook` on the deployed API, which writes to Azure PostgreSQL through a new writer role. `GET /applications` gives the connector and the agent read access, and a one-off import loads the old spreadsheet. The `labelled_jobs` view is the start of the eval set for the match score. The code is done and tested; the cloud setup is a runbook (`docs/AZURE_DEPLOY.md` → Tracker setup) I still have to run.

**Key concepts in plain words**
- **Webhook vs polling.** Polling (`getUpdates`) means my code asks Telegram "anything new?" again and again. A webhook means Telegram calls my URL when something happens. The API already runs on HTTPS, so a webhook needs nothing extra running. A bot uses one or the other, not both.
- **Replying in the response body.** Telegram lets the webhook answer with a Bot API call, and Telegram runs it. My server never calls Telegram, so it never needs the bot token.
- **Shared secret header.** `setWebhook` registers a secret; Telegram sends it back on every call in `X-Telegram-Bot-Api-Secret-Token`. Anyone can find the URL, but without the secret they get 401. The check runs before the body is read.
- **Authentication vs authorisation.** The secret proves the call comes from Telegram. The owner id proves the message comes from me. Anyone can message a bot, so both are needed.
- **Callback queries and inline keyboards.** Buttons under a message carry a short `callback_data` string (`u:`, `d:`, `a:` plus the job id, at most 64 bytes). A tap arrives as a callback query, and the app must answer it with `answerCallbackQuery`, or the button keeps spinning.
- **Retries and poison messages.** Telegram resends an update whenever the answer isn't 2xx. That's good for a temporary problem (database down: 503/504) and bad for an update that will always fail, which would come back forever. That one gets 200 and a log line.
- **Idempotency makes retries safe.** Because a retry can deliver the same tap twice, every write is safe to repeat: a label is "latest wins", ✅ hits a unique key, a repeated status without a note writes nothing.
- **Least privilege for a writer.** A second database role that can write only the tracker tables, can't `DELETE` and has at most 3 connections. The API's role stays SELECT-only, so the "read-only in three layers" rule still holds for every other endpoint.
- **Identity columns vs SERIAL.** `GENERATED ALWAYS AS IDENTITY` takes ids from an internal sequence that Postgres doesn't permission-check on `INSERT`, so the writer role needs no sequence grant. With `SERIAL` it would.
- **Partial unique index.** `UNIQUE (job_id) WHERE job_id IS NOT NULL`: a JobPilot job can be applied to once, while many applications made elsewhere have no job at all.
- **`ON CONFLICT DO NOTHING` without a target.** It covers every unique key on the table, so one statement handles both duplicate rules.
- **A dry run as a rolled-back transaction.** The import does the real work in one transaction and, with `--dry-run`, rolls it back. The counts are the real ones and nothing is written.
- **Labels and selection bias.** I only label jobs the alert shows me (score 70+), so my labels lean towards high scores. Not applying isn't a 👎. An eval built on this set has to keep that in mind.
- **Validating input.** `int()` accepts `1_000`, ` -5` and non-ASCII digits, so ids are checked with a regex first. In Python `True == 1`, so the owner check also rejects booleans.
- **Body size caps.** `Content-Length` can lie or be missing, so the route also counts bytes as they arrive and stops at 1 MB (413).

**Interview questions**
- *How does your bot know a request really comes from Telegram, and from you?* Telegram sends the secret I registered with `setWebhook` in a header, compared in constant time before the body is read. Then only updates from my user id, in a private chat, turn into an action. If either setting is missing, the route answers 503 instead of running open.
- *Your API was read-only. How did you add writes without breaking that?* One hidden route, with its own database URL and its own role. That role can write three tracker tables and read `jobs`; it can't delete anything or touch `jobs`. Every other endpoint still uses the SELECT-only role and read-only sessions.
- *Where is the bot token?* Not on the server. The webhook returns its reply as a Bot API call in the response body, and Telegram executes it. The token stays in `.env` and the flow's HTTP actions with Secure Inputs.
- *What happens if the database is down when you tap a button?* The webhook answers 503 and Telegram retries later. Because the writes are idempotent, a retry can't create a duplicate.
- *Why not always return 200?* Then a temporary database outage would lose the tap. And always returning an error would make one bad update come back forever. So: 503/504 for "try again", 200 for "don't".
- *How would you use the labels?* `labelled_jobs` is the eval set: did the match score rank my 👍 jobs above my 👎 jobs? `score_at_label` keeps the old score, so a new scorer can be compared with it. The bias is that I only label high-scoring jobs, so I'd also label a sample of low-scoring ones.
- *How did you import the old spreadsheet safely?* Duplicates are defined by natural keys (company, title, applied date; or the matched job), so a re-run skips what's there and never overwrites a newer status. A dry run executes the same SQL and rolls back, so I could check the counts before writing.
- *Why is the field called `recorded_via` in the API?* In `/jobs`, `source` means the job board. Two meanings of one name would confuse an LLM tool, so the API renames it; the table keeps its name because applied migrations are never edited.
