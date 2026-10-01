# JobPilot (job-searcher)

A personal job-market platform. It collects internship and entry-level tech jobs every morning, scores how well each one fits the owner's CV, and tracks which skills are in demand. It's being grown into a portfolio project: PostgreSQL, Docker, FastAPI, tests, CI, an LLM agent and cloud deployment.

The owner directs the work and reviews it; AI agents write most of the code. Explain design decisions in plain words, because the owner must be able to defend them in interviews.

## Current state
- `job_searcher.py`: the entry point. It fetches from LinkedIn (via Apify) and Himalayas, then filters, scores, writes Excel files to `output/` and loads the 30-day window into Postgres via `jobpilot.db.safe_load` (never breaks the run).
- `jobpilot/`: `records.py` (Excel row → DB record, pure), `db.py` (idempotent upserts, `safe_load`), `backfill.py` (`python -m jobpilot.backfill` loads existing Excel files).
- `config.json`: search titles, regions, filters and the CV skill list (`cv_skills` = skills the owner already has).
- `tests/`: pytest suite (analysis, record mapping, loader). DB tests are opt-in: `python -m pytest -q -m db` (needs the container; uses a throwaway schema).
- `docker-compose.yml` + `db/init/`: local PostgreSQL (with pgvector). Tables: jobs, skills, job_skills, runs. Init scripts only run on an empty volume, so the next schema change needs a migration.

## Hard rules
- **Never run `job_searcher.py` or `run_now.cmd`.** Each run costs about $0.45 of Apify credit. Ask the owner first.
- **Don't rename or move `job_searcher.py` or `config.json`.** Windows Task Scheduler runs `job_searcher.py` from this folder at 12:00. Refactor by extracting modules that `job_searcher.py` imports, and keep it as the entry point.
- Excel output must keep working until the owner says otherwise. New storage (Postgres) is added alongside it, not instead of it.
- Secrets live only in environment variables or `.env` (git-ignored): `APIFY_TOKEN`, `POSTGRES_*`, `ANTHROPIC_API_KEY`. Never print them or put them in code.
- `output/` and `logs/` hold scraped data and stay out of git.
- Tests never call the network or Apify.
- Don't commit or push unless the owner asks.

## Commands
- Tests: `python -m pytest -q` (a hook runs this automatically after every .py edit)
- Database: `docker compose up -d` (needs Docker Desktop running), `docker compose exec db psql -U jobs -d jobs`
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
1. Git, agents, tests, Docker Postgres
2. Load jobs into Postgres (idempotent upserts), Excel kept as an export ← current
3. FastAPI endpoints + GitHub Actions CI
4. Embeddings with pgvector, semantic job search
5. LLM matching agent (tool use) + MCP server over the job database
6. React/Next.js frontend
7. Agent evals (quality, hallucinated skills, cost per query)
8. Cloud deployment
