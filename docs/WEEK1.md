# Week 1: Git, agents, tests, Docker

## What's set up and why

| File | What it is | Concept to understand |
|------|------------|------------------------|
| `CLAUDE.md` | Project memory. Claude reads it at the start of every session. | Agents only know what you tell them. Rules like "never run the scraper" live here, so you don't have to repeat them. |
| `.claude/agents/*.md` | 5 subagents, each with a role, a tool list and instructions. | **Separation of duties.** The architect and reviewer have no Edit tool, so they can't "fix" things silently. Each agent also starts with a clean context, so it stays focused. |
| `.claude/settings.json` + `.claude/hooks/run_tests.py` | Runs pytest after every Python edit. If tests fail, Claude gets the error and must fix it. | **Guardrails.** Don't trust an agent's "done"; verify it automatically. Exit code 2 = "blocked, here's why". |
| `tests/test_analysis.py` | 27 tests for the scoring/filter logic. | **Safety net before refactoring.** Week 2 moves code around; these tests prove the behaviour didn't change. |
| `docker-compose.yml` | Postgres 17 + pgvector in a container. | **Reproducible environments.** Anyone can run `docker compose up` and get the same database. |
| `db/init/001_schema.sql` (now `db/migrations/001_initial.sql`) | Tables `jobs`, `skills`, `job_skills`. | **Normalization.** Skills get their own table, so skill-trend questions become simple SQL. |
| `.env` / `.env.example` | Local DB password (git-ignored) / template (committed). | **Secrets never go in git.** |

## Checklist

- [ ] Start Docker Desktop, then run `docker compose up -d` and `docker compose ps`. `db` should show "healthy".
- [ ] Open a SQL shell: `docker compose exec db psql -U jobs -d jobs`, type `\dt` (you should see 3 tables), then `\q`.
- [ ] Run `python -m pytest -q`. You should see 27 passed.
- [ ] Create an empty **private** GitHub repo called `jobpilot`. Before making it public, remove personal details from `COPILOT_AGENT_SETUP.md`.
- [ ] First commit (ask Claude: "use the reviewer agent, then commit").

## Your first agent-driven feature: load jobs into Postgres

Paste this into Claude Code, one message at a time. Read each answer before sending the next.

1. `Use the architect agent to plan: load each run's jobs into Postgres in addition to the Excel files. Loading must be idempotent (re-running the same day creates no duplicates), must not break the run if the database is down, and should backfill the existing output/job_market_master.xlsx.`
2. Read the plan. Ask about anything you don't understand, e.g. "why an upsert instead of delete + insert?" Then: `Approved. Use the implementer agent for step 1.` Repeat for each step.
3. `Use the test-writer agent to add edge-case tests for the loader.`
4. `Use the reviewer agent on the current changes.`
5. `Use the docs-writer agent to update the README and learning log.`

**Done when:** `SELECT count(*) FROM jobs;` matches the master Excel, running the loader twice gives the same count, and you can explain "idempotent" and "upsert" in one sentence each.

## Interview questions you should be able to answer after this week
- Why did you put Postgres in Docker instead of installing it?
- What does your test hook do, and why not just trust the AI?
- How do you stop the pipeline from inserting the same job twice?
- How did you split the work between agents, and why can't the reviewer edit files?
