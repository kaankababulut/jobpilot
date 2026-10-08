# JobPilot (job-searcher)

A personal job-market platform. It collects internship and entry-level tech jobs every morning, scores how well each one fits the owner's CV, and tracks which skills are in demand. It's being grown into a portfolio project: PostgreSQL, Docker, FastAPI, tests, CI, an LLM agent and cloud deployment.

The owner directs the work and reviews it; AI agents write most of the code. Explain design decisions in plain words, because the owner must be able to defend them in interviews.

## Current state
- `job_searcher.py`: the entry point. It fetches from LinkedIn (via Apify, Türkiye only to fit the free $5/month), Himalayas and Jooble Türkiye (only when `JOOBLE_API_KEY` is set; 500-request lifetime quota, ~3/day, stops at the first 401/403) (retrying the round when every source has a network error), writes Excel to `output/`, loads the 30-day window into local Postgres, moves the Azure firewall rule to today's IP (`jobpilot.azure_firewall`), then loads Azure Postgres. Each load fails safe and never breaks the run.
- `jobpilot/`: `records.py` (Excel row → DB record, pure), `db.py` (idempotent upserts, `safe_load`, `connect(read_only=True)`), `backfill.py` (`python -m jobpilot.backfill`), `migrate.py` (schema migrations), `queries.py` (the API's read-only SQL), `api.py` (FastAPI app, X-API-Key auth), `azure_firewall.py` (points the Azure firewall rule at today's IP via a least-privilege service principal), `feedback.py` (tracker writes), `telegram.py` (pure Telegram update parser and replies), `telegram_webhook.py` (hidden POST /telegram/webhook, writes as `jobpilot_feedback`), `import_applications.py` (one-off applications.xlsx import, `--dry-run`, `--url-env`). `migrate`/`backfill` take `--url-env AZURE_DATABASE_URL` for the cloud DB, `openapi2.py` (OpenAPI 3.1 → Swagger 2.0 for the Power Platform connector, `python -m jobpilot.openapi2 --write`).
- `config.json`: search titles, regions, filters and the CV skill list (`cv_skills` = skills the owner already has).
- `tests/`: pytest suite (analysis, records, loader, migrations, queries, API). DB tests are opt-in: `python -m pytest -q -m db` (needs the container; uses a throwaway schema). CI (`.github/workflows/ci.yml`) runs both on every PR, and on pushes to main builds and pushes the API image to ghcr.io (`sha-<commit>` tags); `JOBPILOT_REQUIRE_DB=1` turns DB-test skips into failures there.
- `docker-compose.yml`: local PostgreSQL (with pgvector). Tables: jobs, skills, job_skills, runs, feedback, applications, application_events; view labelled_jobs.
- `db/migrations/` + `jobpilot/migrate.py`: the schema as numbered SQL files (001 schema, 002 SELECT-only `jobpilot_api` role, 003 adds 'jooble' to the jobs.source CHECK, 004 feedback/applications/application_events + labelled_jobs view + `jobpilot_feedback` writer role), applied by `python -m jobpilot.migrate`. Schema changes go in a new file (005_...); grant new tables to `jobpilot_api` in the same migration; never edit an applied one.
- `Dockerfile` + whitelist `.dockerignore`: the API image (non-root, `JOBPILOT_DOCS=0`). `requirements-api.txt`: pinned API deps (included by `requirements.txt`). `docs/openapi.json`: contract snapshot, regenerate with `python -m jobpilot.api --write`; `docs/openapi-v2.json`: Swagger 2.0 copy for the connector, `python -m jobpilot.openapi2 --write` (both snapshot-tested).
- Azure (Sweden Central, rg-jobpilot): Postgres Flexible `psql-jobpilot-kk` and Container App `ca-jobpilot-api` (live API). Runbook in README "Deploy to Azure".
- Power Platform (developer environment, solution JobPilot): connector JobPilot, flow "JobPilot daily alert" (13:30 → Telegram), agent "JobPilot Career Assistant". Runbook docs/COPILOT_STUDIO.md.

## Hard rules
- **Never run `job_searcher.py` or `run_now.cmd`.** Each run costs about $0.45 of Apify credit. Ask the owner first.
- **Don't rename or move `job_searcher.py` or `config.json`.** Windows Task Scheduler runs `job_searcher.py` from this folder at 12:00. Refactor by extracting modules that `job_searcher.py` imports, and keep it as the entry point.
- Excel output must keep working until the owner says otherwise. New storage (Postgres) is added alongside it, not instead of it.
- Secrets live only in environment variables or `.env` (git-ignored): `APIFY_TOKEN`, `JOOBLE_API_KEY`, `POSTGRES_*`, `DATABASE_URL`, `JOBPILOT_API_KEY`, `JOBPILOT_CLOUD_API_KEY`, `AZURE_DATABASE_URL`, `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_SUBSCRIPTION_ID`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `TELEGRAM_OWNER_ID`, `FEEDBACK_DATABASE_URL`, `ANTHROPIC_API_KEY`. Never print them or put them in code.
- `output/` and `logs/` hold scraped data and stay out of git.
- Tests never call the network or Apify.
- Don't commit or push unless the owner asks.

## Commands
- Tests: `python -m pytest -q` (a hook runs this automatically after every .py edit)
- Database: `docker compose up -d` (needs Docker Desktop running), `docker compose exec db psql -U jobs -d jobs`
- API: `uvicorn jobpilot.api:app --host 127.0.0.1 --port 8000` (docs at http://127.0.0.1:8000/docs; needs `JOBPILOT_API_KEY` in `.env`)
- Schema: `python -m jobpilot.migrate` (fresh database; `--baseline` once for a database built before migrations)
- Deploy: README "Deploy to Azure" (new Container App revision with the `sha-` image tag).
- Never run `python -m jobpilot.azure_firewall` or touch Azure or Power Platform resources without the owner.
- Stop the database: `docker compose down` (add `-v` only to wipe the data)

## Workflow (agents in .claude/agents/)
1. **architect**: writes the plan. The owner approves it before any code is written.
2. **implementer**: carries out one approved step at a time.
3. **test-writer**: adds tests for edge cases, or before a refactor.
4. **reviewer**: reviews the diff before each commit.
5. **docs-writer**: updates the README and learning log, and drafts CV bullets, at the end of each feature.

Keep each change small enough to review in 5 minutes.

## Style
Match `job_searcher.py`: compact, plain Python, few dependencies, short comments that explain *why*. Type hints on new functions. New Python dependencies go in `requirements.txt`.

## Roadmap
One read-only HTTPS API serves every consumer (Copilot Studio, Power Automate, Power BI, the custom agent). That's why the API and Azure come before the agents: Microsoft's cloud can't reach localhost.

1. Git, agents, tests, Docker Postgres ✓
2. Load jobs into Postgres (idempotent upserts), Excel kept as an export ✓
3. API foundation: SQL migrations runner, read-only FastAPI with an API key, GitHub Actions CI ✓
4. Deploy to Azure on free tiers (Container Apps + Postgres Flexible, SELECT-only role, budget alert); the 12:00 run also loads into Azure ✓
5. Power Platform custom connector + Power Automate daily Telegram alert over the deployed API ✓; Copilot Studio agent configured, blocked on credits (runbook docs/COPILOT_STUDIO.md) ← current
6. Power BI dashboard: skill trends, Microsoft-skill demand, match quality
7. Embeddings with pgvector, semantic search; full descriptions; re-tag skills on stored jobs
8. Custom LLM matching agent (Claude tool use) + MCP server over the same API
9. Agent evals (quality, hallucinated skills, cost per query), incl. Copilot Studio vs custom agent
10. Polish; React/Next.js frontend only if targeting frontend roles

Owner context: the GitHub repo is public (portfolio) since 2026-10-05, so never commit secrets, IDs, chat ids, phone numbers or home IPs; run a secrets scan before every push. Azure uses the free account (card) with a $5 budget alert.
