# Daily Job Searcher

Every day at **09:00**, Windows Task Scheduler ("Daily LinkedIn Job Search") runs `job_searcher.py`:

1. Searches, for each title in `config.json` (Internship + Entry level, posted in the past 24h):
   - LinkedIn through Apify: **remote** jobs Worldwide and in the European Union, plus all jobs in Türkiye.
   - Himalayas.app (free API): remote jobs that explicitly accept applicants living in Türkiye (last 72h, only new ones).
2. Drops senior, non-tech and non-remote postings, internship mills (unpaid, pay-to-join, certificate-as-pay) and job aggregators. Marks each job **Open To You? Yes/No**: on LinkedIn, a remote job listed under a country usually means you must live there. Then it detects the skills each posting asks for and compares them with `cv_skills`.
3. Writes `output/daily/jobs_YYYY-MM-DD.xlsx` and updates `output/job_market_master.xlsx` (last 30 days, for the Copilot agent).

| File | Purpose |
|------|---------|
| `config.json` | Search titles, filters, your CV skills, output folder |
| `run_now.cmd` | Double-click to run immediately |
| `logs/run.log` | Check here if a morning's file is missing |
| `COPILOT_AGENT_SETUP.md` | Agent instructions and setup steps |
| `learning_log.md` | Track new skills; upload it to the agent |

- **Cost:** about $0.40–0.45 of Apify credit per run (~$12–14/month); Himalayas is free. The Apify free plan's $5/month runs out after ~11 days, then runs fail until next month (no surprise charges on the free plan). To stay within $5, lower `limit_per_search` in `regions`, e.g. Worldwide 10, EU 10, Türkiye 15.
- **Token:** read from the `APIFY_TOKEN` user environment variable.
- **PC off at 09:00?** The task runs at the next login.
- **Stop it:** Task Scheduler → "Daily LinkedIn Job Search" → Disable.
