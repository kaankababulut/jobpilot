"""Read-only HTTP API over the job database (FastAPI). This module is the skeleton: app, auth,
per-request connection and /health; the data endpoints are added on top of it.
Run locally: uvicorn jobpilot.api:app --host 127.0.0.1 --port 8000
(127.0.0.1 only, so nothing outside this PC can reach it; docs at http://127.0.0.1:8000/docs).
Endpoints are plain `def`, not async: psycopg calls block, and FastAPI runs sync endpoints in a
threadpool so one slow query doesn't stall the others. No connection pool yet; one user doesn't need it."""
import logging
import os
import secrets
from typing import Iterator

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader

from jobpilot.db import connect, redact

try:
    from dotenv import load_dotenv
except ImportError:  # same fallback as job_searcher: env vars set by hand still work
    def load_dotenv(*args, **kwargs): return False

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))  # doesn't override variables already set

log = logging.getLogger("jobpilot.api")

# LLM orchestrators (Copilot Studio, Power Automate, agents) pick tools by reading these texts,
# so they say what the API is for, not just what it is
app = FastAPI(
    title="JobPilot API",
    version="0.1.0",
    description="Read-only access to JobPilot's job-market data: internship and entry-level tech jobs "
                "collected daily, each scored against the owner's CV, plus which skills are in demand. "
                "Every data endpoint needs an X-API-Key header; /health doesn't.",
)

# auto_error=False: a missing header reaches require_key as None, so we choose the status code,
# and the scheme still shows up in OpenAPI (the docs' Authorize button, and for tool builders)
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False,
                              description="The value of JOBPILOT_API_KEY on the server.")


def require_key(key: str | None = Security(api_key_header)) -> None:
    """Rejects the request unless X-API-Key matches JOBPILOT_API_KEY. Never logs or echoes either."""
    expected = os.environ.get("JOBPILOT_API_KEY", "").strip()  # read per request, so tests can set it
    if not expected:  # fail closed: a forgotten key must not mean an open API
        raise HTTPException(503, "API key not configured")
    # compare_digest takes the same time however many leading characters match, so timing can't leak the key
    if key is None or not secrets.compare_digest(key.encode(), expected.encode()):
        raise HTTPException(401, "invalid or missing API key")


def get_conn() -> Iterator[psycopg.Connection]:
    """One short-lived read-only connection per request, closed when the request ends."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise HTTPException(503, "database not configured")
    with connect(url, read_only=True) as conn:
        # autocommit: each SELECT is its own transaction and ends at once, so the connection is
        # never left "idle in transaction"; the read-only setting still applies to every statement
        conn.autocommit = True
        yield conn  # the with-block closes the connection after the response, even on an error


@app.exception_handler(psycopg.OperationalError)
def db_unavailable(request: Request, exc: psycopg.OperationalError) -> JSONResponse:
    # the driver's message can contain host, user or password, so it's redacted for the log
    # and never sent to the client
    log.warning("database unavailable: %s", redact(exc, os.environ.get("DATABASE_URL", "").strip()))
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


@app.get("/health", summary="Check that the API is up and whether it can reach the database",
         description="Needs no API key and returns no job data. Always 200 while the API process runs, "
                     "so it works as a liveness check; `db` is \"ok\", \"down\" or \"not configured\".")
def health() -> dict:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return {"status": "ok", "db": "not configured"}
    try:
        with connect(url, read_only=True) as conn:
            conn.execute("SELECT 1")
    except Exception as e:  # health must answer, whatever the database does
        log.warning("health check: database down: %s", redact(e, url))
        return {"status": "ok", "db": "down"}
    return {"status": "ok", "db": "ok"}
