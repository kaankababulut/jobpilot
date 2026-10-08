# Design decisions

One short entry per decision: what I chose, why, and what I rejected. Grouped by roadmap step. The five most important are summarised in the [README](../README.md#design-decisions).

## Loading data (roadmap step 2)

**Natural key plus surrogate id.** A job is identified by `(source, source_id)`, e.g. LinkedIn's numeric id. That pair has a `UNIQUE` constraint, so loading the same file twice cannot create duplicates. Foreign keys use a small `BIGINT id` instead, so `job_skills` doesn't repeat long Himalayas URLs. Rejected: using the URL or title as the key (they change or collide).

**Order-independent upsert.** Each load uses `INSERT ... ON CONFLICT DO UPDATE ... WHERE jobs.last_seen <= EXCLUDED.last_seen`. Only data at least as new as what's stored can overwrite it, so a backfill of old files after daily loads can't roll values back. `first_seen` can only move earlier (`LEAST`). Rejected: delete-then-insert, which loses history and breaks foreign keys.

**Load the whole 30-day window every day.** The daily run loads the full master window, not just today's new jobs. If the database was down yesterday, today's run fills the gap by itself. The cost is a few hundred upserts, which is small.

**Fail safe in the daily run.** `safe_load` does the whole load in one transaction, so an error rolls everything back instead of leaving half a batch. The `try` sits outside the `with` block for exactly that reason. It never raises: it logs one redacted line (password and URL hidden) and returns `False`. Connect and statement timeouts (5 s / 30 s) stop a stopped container or a stuck lock from hanging the run. `psycopg` is imported lazily, so a missing driver can't break the Excel output.

**Skip bad rows, not the batch.** The row mapper turns bad content (an unreadable date or score) into `NULL`. Missing identity fields (Run Date, Job ID, Title) raise an error, and the loader skips just that row.

**Fail loud in the backfill.** The backfill is run by hand, so it prints the error and exits with code 1. Each file is its own transaction: a bad file rolls back alone and earlier files stay loaded.

**Microsoft and low-code skills are tracked, not claimed.** Copilot Studio, Power Platform, Azure AI and similar skills are detected in postings, to measure real demand. They are not in `cv_skills`, so match scores stay honest.

## API foundation (roadmap step 3)

**Plain-SQL migrations with a small homegrown runner.** Every schema change is a new numbered file in `db/migrations/`; an applied file is never edited. Each file runs in its own transaction and is recorded in `schema_migrations`, so re-running applies only what's new and a failing file leaves nothing behind. The runner is one tested file of under 200 lines. Rejected: Alembic (built around SQLAlchemy, which this project doesn't use, and heavy for one developer) and Docker init scripts (they only run on an empty volume, so they can't change an existing database). The init mount is removed from `docker-compose.yml`, so the schema has one source of truth.

**Baseline instead of rebuilding.** My database was created by the old init script and already held 270 jobs. `--baseline` records 001 as applied without running it, so the real data stays. It refuses if the `jobs` table doesn't exist, so it can't hide a migration that never ran. Rejected: wiping the volume and backfilling again.

**CI with a real Postgres.** GitHub Actions starts the same `pgvector/pgvector:pg17` image as a service container, builds it with the migrations, then runs the database tests. Locally, database tests skip when Docker is off, which is convenient. In CI a skip would look like a pass, so `JOBPILOT_REQUIRE_DB=1` turns it into a failure. Rejected: mocking the database (it wouldn't catch real SQL errors).

**API key, failing closed.** Callers send an `X-API-Key` header that must match `JOBPILOT_API_KEY`. If the key isn't set, data endpoints return 503 rather than running open. The check uses `secrets.compare_digest`, which takes the same time however many characters match, so response timing can't leak the key. The key check runs before the database connection opens. Rejected: OAuth, which is a lot of moving parts for one user. Copilot Studio and Power Platform custom connectors support API-key auth, which is where this API is going.

**Read-only in three layers.** The code only runs `SELECT`, with values passed as `%s` parameters. The API's connections are opened with `default_transaction_read_only=on`, so Postgres rejects a write even if a bug slips into the code. In Azure the API also logs in as the SELECT-only role `jobpilot_api` (see [A SELECT-only database role](#azure-deployment-roadmap-step-4)).

**One short connection per request, no pool, no async.** Each request opens a read-only connection in autocommit mode and closes it when the response is sent. Autocommit means even a `SELECT` never leaves the connection "idle in transaction", holding a snapshot and locks. Endpoints are plain `def`: psycopg calls block, and FastAPI runs sync endpoints in a thread pool. Rejected for now: a connection pool and async code. With one user and a few hundred rows they add complexity and save nothing measurable.

**Paging with `has_more`.** `/jobs` asks the database for `limit + 1` rows. If the extra row comes back, there is another page. This avoids a second `count(*)` query. The sort ends with `id`, so paging with `OFFSET` never repeats or skips a job. `offset` is capped at 10,000.

**Written for LLM tools.** Each endpoint has a stable `operation_id` (`list_jobs`, `get_job`, `top_skills`, ...) and a description that says when to use it. Agents and Copilot Studio use these as tool names and read the descriptions to pick a tool. Filters like `work_type` are enums, so a typo gets a 422 instead of silently returning nothing. Response models are flat, because Power Platform imports OpenAPI 2.0 and handles simple schemas best.

## Azure deployment (roadmap step 4)

**Managed services on free tiers.** The database is Azure Database for PostgreSQL Flexible Server (B1ms, free for 12 months) and the API runs on Azure Container Apps (Consumption plan). Azure handles patching, backups and HTTPS certificates. Rejected: a virtual machine (I'd have to patch and secure the OS myself) and Kubernetes (far too much for one container).

**Same PostgreSQL version everywhere.** Local Docker, CI and Azure all run PostgreSQL 17. A query that works in tests works in production; a version gap is a class of bug I don't have to think about.

**A second load target, not a sync.** The daily run loads the same 30-day window into the local database and then into Azure, each through `safe_load`, each failing safe on its own. A missed cloud day fills itself the next day. Rejected: copying the local database to the cloud (another moving part) and loading only into the cloud (the local database is my dev copy and my backup).

**Batched upserts for a remote database.** Row by row, each job cost about 4 network round trips, which is nothing on localhost but slow across the internet: the first backfill into Azure took 4 minutes for 339 jobs. `upsert_jobs` now sends each statement group with psycopg's pipelined `executemany`, and inserts all job-skill links in one `unnest` statement. A batch costs about 16 round trips in total. The next backfill took 19 s for 389 jobs. The per-row logic (which update wins, `first_seen` only moving earlier) is unchanged and still tested.

**A SELECT-only database role.** Migration 002 creates `jobpilot_api`: `SELECT` on the four data tables, nothing on `schema_migrations`, read-only by default, at most 5 connections so the API can't use up the server's slots and block the daily load. A read-only session setting can be switched off by the client; a missing privilege can't. Rejected: letting the API use the admin login. The daily loader still uses the admin login (see "Left out on purpose" below).

**Firewall: "Allow Azure services", with compensating controls.** The plan was to allow only my home IP and the Container App's outbound IPs. But Container Apps on the Consumption plan sends traffic from a shared pool of about 170 IPs, more than the firewall's rule limit of about 128. A fixed outbound IP (NAT gateway, about $30/month) or a private network link (VNet plus private endpoint, about $7/month) would blow the $5 budget. So the server allows Azure services, and relies on long random passwords, TLS (`sslmode=require`) and the least-privilege role. In production with a budget I'd use VNet integration and a private endpoint, so the database has no public address at all.

**Firewall automation with a least-privilege identity.** My home IP changes almost daily. The run updates one firewall rule through the Azure management API, signed in as an Entra app whose only permission is a custom role with two actions (read and write firewall rules), assigned on this one server. Rejected: a built-in role like Contributor (a leaked secret could change or delete anything) and opening the firewall to a wide IP range.

**Retry when the whole network is down.** One day was lost because the PC woke at 12:00 and Wi-Fi said "connected" before traffic actually flowed. The fetch now retries the whole round (up to 3 rounds, 120 s apart), but only if every source failed with a network error. An HTTP error, like a bad token, means the server answered, so it isn't retried, and a search that already succeeded is never paid for twice.

**Immutable image tags, built only after tests pass.** CI builds the image on `main` only, after all tests pass, and tags it with the commit hash. A tag never moves, so I always know which code is running and rollback means picking an older tag. The image is a public GitHub package: Azure pulls it without a registry password. I made it public only after checking the image's file list: it holds only `jobpilot/*.py` and the requirements file. Rejected: Azure Container Registry (costs money) and deploying the moving `main` tag.

**A small, closed image.** `.dockerignore` is a whitelist: it ignores everything, then lets in only `jobpilot/` and `requirements-api.txt`. A secret file added later can't slip in by accident. The app runs as a non-root user, uvicorn doesn't send a `server` header, and dependencies are pinned exactly in `requirements-api.txt`, which `requirements.txt` includes, so CI tests the versions that ship. The image sets `JOBPILOT_DOCS=0`: the docs are off unless someone deliberately turns them on (fail closed). `docs/openapi.json` stays in the repo as the contract, checked by a test.

**Production behaviour in the API.** `/health` never touches the database, so Azure's health checks and a cold database can't take each other down; `/runs` shows whether data is flowing. A query that hits the statement timeout returns 504 (the database is up, the query was slow) instead of a generic 500.

**Secrets in Container Apps secrets.** The database URL and the cloud API key are Container Apps secrets, exposed to the app as `DATABASE_URL` and `JOBPILOT_API_KEY`. The cloud key is different from the local one, so a leak of one doesn't open the other. Rejected for now: Azure Key Vault (another resource to secure for two values).

**Manual deploys.** A deploy is a new revision with a new `sha-` tag, in the portal or with `az containerapp update`. It takes a minute and I deploy rarely. Rejected for now: deploying from GitHub Actions, which needs an Azure identity for CI (OIDC federation) and is more to secure than it saves.

**Left out on purpose (for now):**
- VNet integration and a private endpoint for the database: the production answer, over budget here.
- A separate writer role for the daily loader: it still uses the admin login, from my PC only.
- Automatic deploys from GitHub Actions (OIDC), Azure Key Vault, a custom domain.
- Rate limiting or API Management: max 1 replica and the API key cover one user.
- Log Analytics: it bills per GB ingested, and one user doesn't need stored logs.
- Connection pool, async endpoints and a total count in paging: not needed for one user.
- Write endpoints and multiple users or OAuth. (The tracker later added one write route, owner-only: see below.)
- Tracking whether a job is still listed, and de-duplicating the same job across LinkedIn and Himalayas.
- Full descriptions: they are cut at 8,000 characters, as in Excel. Revisit with embeddings in roadmap step 7.

## Microsoft Power Platform (roadmap step 5)

**One connector over the existing API.** The flow and the agent both use one custom connector over the deployed API, so they see exactly the data and rules every other client sees. Rejected: reading the Excel file from OneDrive (the earlier agent), which needs a school or work OneDrive and has no API key or schema.

**Generate Swagger 2.0, don't hand-write it.** Power Platform imports Swagger 2.0, but FastAPI emits OpenAPI 3.1. `jobpilot/openapi2.py` converts only what the API uses (nullable fields, enums, the API-key scheme) and raises an error naming the JSON path for anything 2.0 can't express. A snapshot test fails if `docs/openapi-v2.json` drifts. Rejected: editing the spec by hand in the connector wizard, which drifts silently when an endpoint changes.

**The key lives in a connection.** The API key is stored once, encrypted, in the connection `JobPilot Cloud`. The agent uses maker-provided credentials, so a chat user never sees or types a key. Rejected: a key in the flow or the instructions.

**Check freshness before alerting.** The flow first asks `recent_runs` whether today's daily load exists. If not, it says so, instead of sending "no new matches" when the real problem is a failed run. A `health` call wakes the API first, and the next step runs even if that call times out.

**Telegram, after several dead ends.** On a free tenant, email, Outlook.com, Teams and mobile push were restricted, retired or needed a licence; Gmail can't share a flow with a custom connector; Discord is blocked in Türkiye. A Telegram bot takes one HTTP POST. The token is in the URL, so Secure Inputs hide it from the run history.

**Tools, not knowledge, for the agent.** The agent has 4 tools and no knowledge sources or web search, and its instructions forbid naming any job or skill a tool didn't return. Every answer can then be checked against tool output. Rejected: uploading the Excel file as knowledge, which goes stale and can't be filtered.

**No paid plan for the agent.** The trial environment has no credits, so the agent can't answer yet. I didn't link pay-as-you-go billing for a portfolio demo. The same API and test questions carry over to the custom Claude agent in roadmap step 8.

## Application tracker (roadmap step 5, follow-up)

The tracker replaces the spreadsheet I kept by hand. I label jobs and update applications from the Telegram chat that already gets the daily alert. Everything goes through one route, `POST /telegram/webhook`. Labels and applications are also the ground truth the match score will be judged against later.

**Replies go in the webhook's response body.** Telegram lets a webhook answer with a Bot API call (`sendMessage`, `answerCallbackQuery`) in its HTTP response, and Telegram runs that call itself. So the server never calls Telegram and never holds the bot token. A leak of the Container App's settings can't be used to send messages as the bot. The price: one reply per update, and the server can't see whether the reply arrived. Rejected: storing the token on the server and calling `sendMessage`, which adds a secret and an outgoing call for no gain.

**Secret header first, then the owner check, then the database.** `setWebhook` registers a `secret_token`, and Telegram sends it back in `X-Telegram-Bot-Api-Secret-Token` on every call. The route checks it (in constant time) before it reads the body; a wrong or missing value gets 401. Then only updates sent by `TELEGRAM_OWNER_ID`, in a private chat with the bot, turn into an action. Anything else is answered 200 and ignored, without opening a database connection. Both checks are needed: the secret proves the call comes from Telegram, and the owner id proves the message comes from me, because anyone can find and message a bot. If the secret or owner id isn't set, the route answers 503 (fail closed, like the API key). Rejected: relying on the webhook URL staying unknown.

**A separate writer role; the API stays read-only.** Migration 004 adds `jobpilot_feedback`. It can `SELECT`, `INSERT` and `UPDATE` the three tracker tables, read `jobs`, and nothing else: no `DELETE`, at most 3 connections, and no login until I set a password. The webhook connects with its own `FEEDBACK_DATABASE_URL`. Every other endpoint still uses `jobpilot_api` with read-only sessions. So a bug or a leaked URL can add or change tracker rows, but it can't wipe history, change jobs or use up the server's connection slots. Rejected: giving `jobpilot_api` write rights (that would break "read-only in three layers" for every endpoint) and using the admin login.

**503/504 so Telegram retries; 200 for poison updates.** Telegram resends an update whenever the answer isn't 2xx. If the database is down or a query times out, the route answers 503 or 504, because trying again later is the right thing. If an update can't be processed for any other reason, retrying won't help, so it answers 200 (with "Something went wrong" for a button tap, so the spinner stops) and logs only the error type. Otherwise one bad update would be resent again and again. Retries are safe because every write is idempotent: a label is "latest wins", ✅ hits a unique key, and repeating a status without a note writes nothing.

**A 1 MB body cap.** Real updates are a few KB. The cap stops one huge request from filling the 0.5 GiB container's memory. It checks `Content-Length`, but also counts the bytes as they arrive, because a client can lie about the length or leave it out. Too large means 413. The cap only matters after the secret check, so only someone who knows the secret could hit it.

**`labelled_jobs`: applied counts as 👍.** The view lists every job I labelled or applied to. An explicit 👍/👎 wins; otherwise having applied counts as 👍, because applying is the strongest "this fits" signal I give. `score_at_label` freezes the score I saw at the time, so a new scorer (steps 7–8) can be compared with the old one on the same labels. Known bias: I only see and label jobs the alert sends me (score 70+, open to me), so the set leans towards high scores and has few 👎. Not applying doesn't mean 👎. The evals in step 9 must account for that, for example by also labelling a sample of low-scoring jobs.

**`recorded_via` in the API, `source` in the table.** In the database, `applications.source` says how a row was recorded (`telegram`, `import`, `manual`), the same as `feedback.source`. But in `/jobs`, `source` means the job board (LinkedIn, Himalayas, Jooble). An LLM tool reading both responses could mix them up, so `GET /applications` renames the column to `recorded_via` in its SQL. The table keeps its name, because migration 004 is applied and is never edited.

**The import: natural keys and a rolled-back dry run.** `python -m jobpilot.import_applications` loads my old spreadsheet once. A row is a duplicate if the same company, title and applied date exist (`UNIQUE (company, title, applied_on)`), or if it links to a job that is already tracked (a partial unique index on `job_id`). `ON CONFLICT DO NOTHING` without a named target covers both keys, so a re-run skips what is already there. A status is set only on newly inserted rows, so a re-run never overwrites a status the bot has changed since. `--dry-run` runs exactly the same SQL in one transaction and rolls it back (`force_rollback`), so its counts, including which rows match a job, are the real ones, and nothing is written. Rejected: a separate "simulate" code path, which could drift from the real one. Bad rows are reported by Excel row number; the good ones are committed, and after a fix a re-run adds only the rest.
