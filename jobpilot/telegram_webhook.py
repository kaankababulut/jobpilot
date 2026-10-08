"""POST /telegram/webhook: Telegram calls it for every button tap and message sent to the bot.
The one route in the API that writes, so it is locked down in layers, all checked before any database work:
1. TELEGRAM_WEBHOOK_SECRET must be set (fail closed, like JOBPILOT_API_KEY).
2. Telegram sends that secret in X-Telegram-Bot-Api-Secret-Token (set with setWebhook's secret_token),
   so a request from anyone else gets 401.
3. Only updates from TELEGRAM_OWNER_ID in a private chat parse to an action (jobpilot.telegram).
Writes go through FEEDBACK_DATABASE_URL, which logs in as jobpilot_feedback (migration 004): it can
write the feedback tables only, never DELETE, and the API's read-only DATABASE_URL stays read-only.
Hidden from OpenAPI (include_in_schema=False): it's Telegram's endpoint, not a tool for the connector.
Telegram retries any non-2xx answer, so: database down -> 503/504 (retrying later is right);
an update we ignore or can't process -> 200 (retrying a poison update forever is not): {}, or for a
failed button tap an answerCallbackQuery saying so.
Logs name the action kind and the error type only: never the body, chat or owner id, secret or URL."""
import json
import logging
import os
import re
import secrets
from contextlib import contextmanager
from typing import Iterator

import psycopg
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from jobpilot import feedback as fb
from jobpilot import telegram as tg
from jobpilot.db import connect, redact

log = logging.getLogger("jobpilot.telegram")
router = APIRouter()

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
# real updates are a few KB; the cap stops a huge body using up memory once the secret is known
MAX_BODY = 1_000_000
KINDS = {tg.Label: "label", tg.Applied: "applied", tg.ListApps: "apps", tg.SetStatus: "status",
         tg.AddApp: "add", tg.Help: "help"}


def _owner_id() -> int | None:
    raw = os.environ.get("TELEGRAM_OWNER_ID", "").strip()
    # a regex, not int(): int() also accepts "1_000", " -5" and non-ASCII digits
    return int(raw) if re.fullmatch(r"[1-9][0-9]{0,18}", raw) else None


def _feedback_url() -> str:
    return os.environ.get("FEEDBACK_DATABASE_URL", "").strip()


@contextmanager
def get_write_conn() -> Iterator[psycopg.Connection]:
    """One write connection per update; the with-block commits on success and rolls back on any error,
    so an application and its first event are written together or not at all."""
    url = _feedback_url()
    if not url:
        raise HTTPException(503, "feedback database not configured")
    with connect(url) as conn:  # not read_only: this is the one writer
        yield conn


def handle(incoming: tg.Incoming) -> dict:
    """Runs the action in one transaction and returns the Bot API reply."""
    a, chat = incoming.action, incoming.chat_id
    if isinstance(a, tg.Help):  # no database needed
        return tg.send_text(chat, tg.HELP)
    with get_write_conn() as conn:
        if isinstance(a, tg.Label):
            found, _score = fb.set_label(conn, a.job_id, a.label)
            text = (tg.SAVED_UP if a.label == "up" else tg.SAVED_DOWN) if found else tg.UNKNOWN_JOB
            return tg.answer_callback(incoming.callback_id, text)
        if isinstance(a, tg.Applied):
            result = fb.mark_applied(conn, a.job_id)
            text = tg.UNKNOWN_JOB if result is None else (tg.APPLIED if result[1] else tg.ALREADY_APPLIED)
            return tg.answer_callback(incoming.callback_id, text)
        if isinstance(a, tg.ListApps):
            return tg.send_text(chat, tg.format_apps(fb.open_applications(conn)))
        if isinstance(a, tg.SetStatus):
            if not fb.set_status(conn, a.app_id, a.status, a.note):
                return tg.send_text(chat, f"Unknown application #{a.app_id}")
            return tg.send_text(chat, f"Updated #{a.app_id} → {a.status}")
        if isinstance(a, tg.AddApp):
            app_id, created = fb.add_application(conn, a.company, a.title, a.url, source="manual")
            return tg.send_text(chat, f"Added #{app_id}" if created else f"Already tracked #{app_id}")
    raise TypeError(f"unhandled action {type(a).__name__}")


async def _read_capped(request: Request) -> bytes | None:
    """The body, or None if it's over MAX_BODY. Content-Length is checked first, but a client can lie
    about it or leave it out (chunked), so the stream is counted too and reading stops at the cap."""
    try:
        if int(request.headers.get("content-length", "0")) > MAX_BODY:
            return None
    except ValueError:
        pass  # unparsable header: the stream count below still applies
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY:
            return None
    return bytes(body)


# async so the secret check runs before the body is read; the blocking database work goes to a thread
@router.post("/telegram/webhook", include_in_schema=False)
async def telegram_webhook(request: Request):
    expected = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()  # per request, like require_key
    if not expected:  # fail closed: a forgotten secret must not mean anyone can write
        return JSONResponse(status_code=503, content={"detail": "webhook not configured"})
    given = request.headers.get(SECRET_HEADER)
    # compare_digest takes the same time however many leading characters match, so timing can't leak it
    if given is None or not secrets.compare_digest(given.encode(), expected.encode()):
        return Response(status_code=401)
    owner_id = _owner_id()
    if owner_id is None:
        return JSONResponse(status_code=503, content={"detail": "webhook not configured"})
    body = await _read_capped(request)
    if body is None:
        return JSONResponse(status_code=413, content={"detail": "body too large"})
    try:
        update = json.loads(body)
    except (ValueError, RecursionError):  # bad JSON or bad UTF-8 (both ValueErrors), or absurd nesting
        return JSONResponse(status_code=400, content={"detail": "invalid JSON"})
    incoming = tg.parse_update(update, owner_id)
    if incoming is None:
        return {}  # someone else, or something we don't handle: 200, so Telegram doesn't retry it
    kind = KINDS[type(incoming.action)]
    log.info("telegram: %s", kind)
    try:
        return await run_in_threadpool(handle, incoming)
    except HTTPException:
        raise  # 503 feedback database not configured
    # handled here rather than by the API's handlers, which redact DATABASE_URL, not this URL.
    # QueryCanceled first: it's a subclass of OperationalError
    except psycopg.errors.QueryCanceled as e:
        log.warning("telegram: %s timed out: %s", kind, redact(e, _feedback_url()))
        return JSONResponse(status_code=504, content={"detail": "query timed out"})
    except psycopg.OperationalError as e:
        log.warning("telegram: %s database unavailable: %s", kind, redact(e, _feedback_url()))
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})
    except Exception as e:
        # the type only: a message could quote the update's text or a value from the database
        log.warning("telegram: %s failed (%s); answered 200 so Telegram won't retry it", kind, type(e).__name__)
        # a tapped button shows a spinner until answered, so say something rather than leave it hanging
        return tg.answer_callback(incoming.callback_id, tg.ERROR) if incoming.callback_id else {}
