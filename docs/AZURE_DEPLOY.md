# Deploy to Azure

Runbook for the cloud half of JobPilot: what exists, how to deploy and roll back, where secrets live, cost guardrails, the kill switch and the tracker setup. Back to the [README](../README.md).

## What exists

All in resource group `rg-jobpilot`, region **Sweden Central**, on a personal pay-as-you-go subscription.

| Resource | Name | Size | Cost |
|----------|------|------|------|
| PostgreSQL Flexible Server | `psql-jobpilot-kk` | PostgreSQL 17, Burstable B1ms, 32 GiB storage, 7-day backups; no HA, geo-backup, storage autogrow or Defender | Free for 12 months (750 h B1ms + 32 GB per month) |
| Container Apps environment | `cae-jobpilot` | Consumption plan, no Log Analytics | Free within the monthly Consumption grant |
| Container App | `ca-jobpilot-api` | 0.25 vCPU / 0.5 GiB, min 0 / max 1 replica, HTTPS only, target port 8000 | Free within the grant; scales to zero when idle |
| Image | `ghcr.io/kaankababulut/jobpilot-api` | Public GitHub package, tags `sha-<commit>` and `main` | Free |
| Budget | on the subscription | $5/month; email alerts at 50% and 100% actual, 100% forecast | Free |

The API is served at the Container App's default HTTPS address, `https://ca-jobpilot-api.<environment>.swedencentral.azurecontainerapps.io` (`/health` needs no key; `/docs` is off). The examples below use `<api-host>` for it.

The database lives in Sweden Central because Germany West Central refused new PostgreSQL servers for new subscriptions. Both the local database and CI also run PostgreSQL 17.

## Deploy a new version

1. Merge the pull request into `main`. CI runs the tests, then builds the image and pushes it as `sha-<full commit hash>` (and `main`). Nothing untested becomes an image.
2. Find the tag: GitHub → the repo → Packages → `jobpilot-api`, or `git rev-parse origin/main` after a `git pull`.
3. Azure portal → Container App `ca-jobpilot-api` → **Containers** → **Edit and deploy** → select the container → change the image tag to the new `sha-...` → **Save** → **Create**. A new revision starts and takes over the traffic.
   (CLI equivalent: `az containerapp update -n ca-jobpilot-api -g rg-jobpilot --image ghcr.io/kaankababulut/jobpilot-api:sha-<hash>`.)
4. Check it: `/health` answers 200, and `/jobs` with the key answers 200 (see [Call the live API](#call-the-live-api)). The first request after an idle period is slower, because the app starts from zero.

**Roll back:** repeat step 3 with the previous `sha-...` tag. Tags never move, so an old tag is exactly the old build. Don't deploy the `main` tag: you couldn't tell which build is running.

**Schema change:** apply the new migration to Azure before deploying code that needs it: `python -m jobpilot.migrate --url-env AZURE_DATABASE_URL`. New tables also need a `GRANT SELECT` to `jobpilot_api` in that migration.

## Secrets

| Secret | Lives in | Used by |
|--------|----------|---------|
| Admin URL of the Azure database | `.env` → `AZURE_DATABASE_URL` | the daily run, `migrate`, `backfill` |
| `database-url` (connects as the read-only `jobpilot_api` role) | Container App → **Secrets** → env var `DATABASE_URL` | the deployed API |
| `api-key` (the cloud API key, different from the local one) | Container App → **Secrets** → env var `JOBPILOT_API_KEY`; my copy in `.env` → `JOBPILOT_CLOUD_API_KEY` | the deployed API; my scripts |
| Firewall updater client secret | `.env` → `AZURE_CLIENT_SECRET` (expires after 12 months) | `azure_firewall.py` |
| `telegram-webhook-secret` | Container App → **Secrets** → env var `TELEGRAM_WEBHOOK_SECRET`; my copy in `.env` → `TELEGRAM_WEBHOOK_SECRET` | the webhook; `setWebhook` |
| `feedback-database-url` (connects as the writer role `jobpilot_feedback`) | Container App → **Secrets** → env var `FEEDBACK_DATABASE_URL` | the webhook only |

No secret is in the image, in git or in CI. CI pushes images with the short-lived `GITHUB_TOKEN`.

Never type or paste a password into psql's hidden prompt (`\password`) through `docker exec`: it silently changed the password twice. Never screenshot the Secrets page with values shown. If a secret was ever visible, rotate it.

**Rotate the cloud API key.** A secret change doesn't restart the running app, so finish with a restart.

1. Create a key straight onto the clipboard, without printing it:
   `python -c "import secrets, subprocess; subprocess.run('clip', input=secrets.token_urlsafe(32), text=True, check=True)"`
2. Container App → **Secrets** → `api-key` → Edit → paste → Save.
3. Paste the same value into `.env` as `JOBPILOT_CLOUD_API_KEY` (and edit the connection `JobPilot Cloud` in Power Platform).
4. Container App → **Revisions and replicas** → active revision → **Restart**. The old key now gets 401.

**Rotate the `jobpilot_api` password.** The script sets a random password through the admin connection and puts the new API database URL on the clipboard. From home, so the firewall lets you in (run `python -m jobpilot.azure_firewall` first if your IP changed). The old password stops working at once, so do all steps in one go.

```powershell
# PowerShell, from the project folder; prints no secret
@'
import os, secrets, subprocess
from urllib.parse import urlsplit
import psycopg
from psycopg import sql
from dotenv import load_dotenv
load_dotenv(".env")
admin = urlsplit(os.environ["AZURE_DATABASE_URL"])
pw = secrets.token_urlsafe(32)  # URL-safe characters: no encoding needed in the URL
with psycopg.connect(admin.geturl(), connect_timeout=10) as conn:
    conn.execute(sql.SQL("ALTER ROLE jobpilot_api LOGIN PASSWORD {}").format(sql.Literal(pw)))
url = f"postgresql://jobpilot_api:{pw}@{admin.hostname}:5432{admin.path}?sslmode=require"
subprocess.run("clip", input=url, text=True, check=True)
print("jobpilot_api password changed; the new DATABASE_URL is on the clipboard")
'@ | python -
```

Then: Container App → **Secrets** → `database-url` → Edit → paste → Save → restart the active revision → check `/jobs` answers 200. Clear the clipboard afterwards (copy any other text).

**Firewall updater secret:** see [Azure firewall auto-update](#azure-firewall-auto-update).

## Call the live API

```powershell
# PowerShell, from the project folder: reads the cloud key from .env, prints nothing
$env:JOBPILOT_CLOUD_API_KEY = (Select-String -Path .env -Pattern '^JOBPILOT_CLOUD_API_KEY=(.*)$').Matches[0].Groups[1].Value.Trim()
$base = "https://<api-host>"

Invoke-RestMethod "$base/health"
Invoke-RestMethod -Headers @{"X-API-Key"=$env:JOBPILOT_CLOUD_API_KEY} "$base/jobs?open_to_you=true&limit=3"
Invoke-RestMethod -Headers @{"X-API-Key"=$env:JOBPILOT_CLOUD_API_KEY} "$base/runs?limit=3"
```

```bash
# Git Bash
export JOBPILOT_CLOUD_API_KEY="$(grep '^JOBPILOT_CLOUD_API_KEY=' .env | cut -d= -f2-)"
BASE=https://<api-host>
curl -s -H "X-API-Key: $JOBPILOT_CLOUD_API_KEY" "$BASE/skills?days=30&limit=5"
```

Checked on 2026-10-04: `/health` 200; `/docs` and `/openapi.json` 404; `/jobs` 401 without the key and 200 in about 0.5 s with it; `/runs` shows the daily load; plain `http://` redirects to `https://`.

## Cost guardrails

- **Budget:** $5/month on the subscription, with email alerts at 50% and 100% of actual spend and at 100% of forecast. An alert doesn't stop anything; it tells me to act.
- **Max 1 replica:** a flood of requests can't scale the app (and the bill) up.
- **Scale to zero:** with no traffic the app runs no replicas and costs nothing. The price is a slower first request (a cold start).
- **No paid extras:** no high availability, geo-backup, storage autogrow, Defender or Log Analytics. The portal forced a Log Analytics workspace when the environment was created; logging is set to "Don't save logs" so nothing is ingested and billed.
- **Free tier ends around Sep 2027.** After 12 months the B1ms server becomes a paid resource. Decide before then: delete it, stop it, or move the database somewhere cheaper. Put a calendar reminder next to the client-secret one.

## Kill switch

- **Take the API offline, keep everything:** Container App → **Ingress** → untick **Enabled** → Save. Turn it on again the same way.
- **Remove everything and all costs:** delete the resource group `rg-jobpilot`. This deletes the database too; the local database and Excel files still hold the data, and `python -m jobpilot.backfill --url-env AZURE_DATABASE_URL` can refill a new one.

## Azure firewall auto-update

The home IP changes almost daily, and Azure's firewall silently drops connections from any IP it doesn't list, so the cloud load would just time out. Before the Azure load, the 12:00 run points the firewall rule `home` (set in `config.json` → `azure_firewall`) at today's public IP, then waits up to about 3 minutes for the database to answer (a TCP probe). If anything fails, it logs one line and the run carries on. A real log line: `rule home <old> -> <new>; database reachable after 3s`, then `Azure Postgres: wrote 428 of 428 jobs`.

- **Identity:** the Entra app `jobpilot-firewall-updater` signs in with a client secret. Its only permission is the custom role `JobPilot Firewall Rule Updater` (read and write `flexibleServers/firewallRules`, nothing else), assigned on `psql-jobpilot-kk` only. A leaked secret can move that one rule and nothing more.
- **.env:** `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_SUBSCRIPTION_ID` (plus `AZURE_DATABASE_URL`, for the host). If any is blank, the update is skipped.
- **Test it (from home):** `python -m jobpilot.azure_firewall`. It prints one line and exits 0 when the database is reachable.
- **Secret expired** (the log says "client secret expired"): Entra ID → App registrations → `jobpilot-firewall-updater` → Certificates & secrets → New client secret → copy the **Value** into `.env` as `AZURE_CLIENT_SECRET` → delete the old secret → set a calendar reminder for the new expiry date.

## Tracker setup

One-time setup for the application tracker: Telegram buttons and commands → `POST /telegram/webhook` → Azure PostgreSQL as `jobpilot_feedback`. Do the steps in this order, from home (so the firewall lets you in; run `python -m jobpilot.azure_firewall` first if your IP changed). The design is explained in [DESIGN_DECISIONS.md](DESIGN_DECISIONS.md#application-tracker-roadmap-step-5-follow-up).

Placeholders: `<api-host>` is the Container App's address (see [What exists](#what-exists)), `<TELEGRAM_OWNER_ID>` is my Telegram user id. In a private chat the user id is the same number as the chat id, so it's the `<chat_id>` from [COPILOT_STUDIO.md §4](COPILOT_STUDIO.md#4-telegram-bot).

**a) Merge and migrate.** Merge the pull request into `main` and wait for CI to push the image. Then apply migration 004 to both databases, before deploying the new code:

```powershell
python -m jobpilot.migrate                                # local
python -m jobpilot.migrate --url-env AZURE_DATABASE_URL   # Azure
```

**b) Give `jobpilot_feedback` a password.** The migration creates the role without login. This is the [`jobpilot_api` rotation script](#secrets), adapted: it sets a random password and puts the webhook's database URL on the clipboard. Nothing is printed.

```powershell
# PowerShell, from the project folder; prints no secret
@'
import os, secrets, subprocess
from urllib.parse import urlsplit
import psycopg
from psycopg import sql
from dotenv import load_dotenv
load_dotenv(".env")
admin = urlsplit(os.environ["AZURE_DATABASE_URL"])
pw = secrets.token_urlsafe(32)  # URL-safe characters: no encoding needed in the URL
with psycopg.connect(admin.geturl(), connect_timeout=10) as conn:
    conn.execute(sql.SQL("ALTER ROLE jobpilot_feedback LOGIN PASSWORD {}").format(sql.Literal(pw)))
url = f"postgresql://jobpilot_feedback:{pw}@{admin.hostname}:5432{admin.path}?sslmode=require"
subprocess.run("clip", input=url, text=True, check=True)
print("jobpilot_feedback password set; the new FEEDBACK_DATABASE_URL is on the clipboard")
'@ | python -
```

Keep the clipboard for step c. This URL goes only into the Container App. `FEEDBACK_DATABASE_URL` in `.env` is for a local API run; leave it blank there (the local webhook then answers 503), so a local test can't write to Azure. The same script rotates the password later: run it again, update the secret, then restart the active revision.

**c) Container App secrets, variables and a new revision.**

1. Container App → **Secrets** → **Add**: name `feedback-database-url`, value: paste the clipboard → **Add**.
2. Create the webhook secret on the clipboard:
   `python -c "import secrets, subprocess; subprocess.run('clip', input=secrets.token_urlsafe(32), text=True, check=True)"`
   Telegram allows only `A-Z a-z 0-9 _ -` in it, which is exactly what `token_urlsafe` produces. Paste it into `.env` as `TELEGRAM_WEBHOOK_SECRET` (step d needs it), then **Secrets** → **Add**: name `telegram-webhook-secret`, paste the same value. Clear the clipboard afterwards.
3. **Containers** → **Edit and deploy** → select the container:
   - Image tag: the latest `sha-...` from `main` (see [Deploy a new version](#deploy-a-new-version)).
   - **Environment variables** → add:

     | Name | Source | Value |
     |------|--------|-------|
     | `TELEGRAM_WEBHOOK_SECRET` | Reference a secret | `telegram-webhook-secret` |
     | `FEEDBACK_DATABASE_URL` | Reference a secret | `feedback-database-url` |
     | `TELEGRAM_OWNER_ID` | Manual entry | `<TELEGRAM_OWNER_ID>` |

   - **Save** → **Create**. This revision also brings `source=jooble` in `/jobs` and `GET /applications`.
4. Check: `/health` answers 200; `/applications?open=true` with the key answers 200 (see [Call the live API](#call-the-live-api)); a POST to the webhook without the secret answers 401:
   `Invoke-WebRequest -Method Post -Uri "https://<api-host>/telegram/webhook" -Body '{}' -ContentType application/json` → 401 (PowerShell shows it as an error, which is expected).

**d) Point Telegram at the webhook.** The bot token is part of the `setWebhook` URL. Don't type it into a terminal (shell history keeps it), don't let it show in a screenshot, and never paste the request URL anywhere. This script reads the token and the secret from `.env` and prints only Telegram's answer:

```powershell
# PowerShell, from the project folder; prints no secret
@'
import json, os, urllib.request
from dotenv import load_dotenv
load_dotenv(".env")
HOST = "<api-host>"  # edit: the Container App's address, without https://
token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
def call(method, payload=None):
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                 data=json.dumps(payload or {}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except Exception as e:  # the type and status only: the request URL contains the token
        return {"ok": False, "error": type(e).__name__, "status": getattr(e, "code", None)}
r = call("setWebhook", {"url": f"https://{HOST}/telegram/webhook",
                        "secret_token": os.environ["TELEGRAM_WEBHOOK_SECRET"].strip(),
                        "allowed_updates": ["message", "callback_query"],
                        "drop_pending_updates": True})
print("setWebhook:", r.get("ok"), r.get("description") or r.get("error"))
info = call("getWebhookInfo").get("result", {})
print({k: info.get(k) for k in ("url", "pending_update_count", "allowed_updates",
                                "last_error_date", "last_error_message")})
'@ | python -
```

Expected: `setWebhook: True Webhook was set`, then `getWebhookInfo` with your URL, `allowed_updates` `['message', 'callback_query']` and no `last_error_message`. `getWebhookInfo` never returns the secret. `drop_pending_updates` throws away anything sent to the bot before the webhook existed. From now on `getUpdates` (the chat-id script in COPILOT_STUDIO §4) stops working: Telegram delivers updates by webhook or by polling, not both.

**e) Update the connector.** Power Platform → solution `JobPilot` → custom connector `JobPilot` → **Edit** → **Update from OpenAPI file** → `docs/openapi-v2.json` → **Update connector**. Check that **Definition** now shows 6 actions (new: `list_applications`) and that **Security** still says API Key, `X-API-Key`, Header. The webhook is hidden from the spec, so it never appears as an action.

**f) Import the old spreadsheet.** It reads `~/Desktop/applications.xlsx` by default (header row 1: Date, Company, Job title, Link, Status, Notes). Dry run first; it writes nothing:

```powershell
python -m jobpilot.import_applications --dry-run                               # local
python -m jobpilot.import_applications --dry-run --url-env AZURE_DATABASE_URL  # Azure
```

Read the summary line: rows read, inserted, already present, **matched to a job**, errors. A low "matched" count usually means the Link cells are hyperlinks whose display text ("link", "LinkedIn") differs from the address: the import reads the cell's text, not the link behind it. Put the full URL in the cell as its text, save, and dry-run again. Fix any rows listed as errors (shown by Excel row number). Then run it for real, local and Azure:

```powershell
python -m jobpilot.import_applications
python -m jobpilot.import_applications --url-env AZURE_DATABASE_URL
```

Re-running is safe: rows already imported are skipped, and a status the bot has changed is never overwritten.

**g) Smoke test.** In the private chat with the bot (the first answer can take a few seconds while the app starts from zero; Telegram retries if it times out):

1. `/start` → the command list.
2. `/apps` → the open applications from the import.
3. Run the flow once (**Test** → **Manually**, after the [keyboard edit](COPILOT_STUDIO.md#7-tracker-buttons-in-the-daily-alert)) and tap 👍 under a job → the toast "Saved 👍". Tap 👎 on the same job to see the label change; the latest label wins.

If nothing answers, run the step d script again and read `last_error_message`: 401 means the secret in `.env` (used for `setWebhook`) and in the Container App differ; 503 means a variable is missing in the revision.
