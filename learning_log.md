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
