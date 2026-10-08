"""Read-only HTTP API over the job database (FastAPI): API-key auth, one read-only connection per
request, /health and the job endpoints. SQL lives in jobpilot.queries; this layer validates input.
Run locally: uvicorn jobpilot.api:app --host 127.0.0.1 --port 8000
(127.0.0.1 locally, so nothing outside this PC can reach it; docs at http://127.0.0.1:8000/docs unless JOBPILOT_DOCS=0).
Deployed: the Dockerfile runs it on 0.0.0.0 in Azure Container Apps, behind HTTPS ingress, with docs off.
Endpoints are plain `def`, not async: psycopg calls block, and FastAPI runs sync endpoints in a
threadpool so one slow query doesn't stall the others. No connection pool yet; one user doesn't need it.
The one exception to read-only is POST /telegram/webhook (jobpilot.telegram_webhook), with its own
secret, owner check and write-only-to-feedback database role."""
import datetime as dt
import json
import logging
import os
import secrets
import sys
from enum import Enum
from typing import Annotated, Iterator

import psycopg
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Query, Request, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from jobpilot import queries, telegram_webhook
from jobpilot.db import connect, redact

try:
    from dotenv import load_dotenv
except ImportError:  # same fallback as job_searcher: env vars set by hand still work
    def load_dotenv(*args, **kwargs): return False

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))  # doesn't override variables already set

log = logging.getLogger("jobpilot.api")

# routes hang on a router, and create_app() builds the app around it, so tests can build an app
# with other settings (docs off) without reloading this module
router = APIRouter()

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


def db_unavailable(request: Request, exc: psycopg.OperationalError) -> JSONResponse:
    # the driver's message can contain host, user or password, so it's redacted for the log
    # and never sent to the client
    log.warning("database unavailable: %s", redact(exc, os.environ.get("DATABASE_URL", "").strip()))
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


def query_timed_out(request: Request, exc: psycopg.errors.QueryCanceled) -> JSONResponse:
    # QueryCanceled (statement_timeout hit) is a subclass of OperationalError; Starlette picks the
    # handler by walking the exception's class hierarchy, most specific first, so this one wins.
    # 504, not 503: the database is up, this one query was just too slow
    log.warning("query timed out: %s", redact(exc, os.environ.get("DATABASE_URL", "").strip()))
    return JSONResponse(status_code=504, content={"detail": "query timed out"})


@router.get("/health", operation_id="health", summary="Check that the API process is up",
            description="Needs no API key, returns no job data and doesn't touch the database, so it's a cheap "
                        "public liveness check. To check the database too, call recent_runs (needs the key).")
def health() -> dict:
    # no database call: an unauthenticated endpoint that opens connections would let anyone
    # load the database, and a liveness probe must not fail just because Postgres is slow
    return {"status": "ok"}


# ---------- jobs ----------
# Enums, so FastAPI rejects a typo with 422 instead of quietly returning no rows,
# and the allowed values appear in OpenAPI for tool builders. Same values as the schema's CHECKs.
class WorkType(str, Enum):
    remote = "Remote"
    remote_unclear = "Remote?"  # the posting hints at remote but doesn't say so clearly
    hybrid = "Hybrid"
    on_site = "On-site"


class Source(str, Enum):
    linkedin = "linkedin"
    himalayas = "himalayas"
    jooble = "jooble"


# the response models are flat (no nesting beyond the skills list) and use plain types, because
# Power Platform connectors import OpenAPI 2.0, which handles simple schemas best
class JobSummary(BaseModel):
    id: int = Field(description="Job id; pass it to get_job for the full posting.")
    source: str = Field(description="Where the job was found: linkedin, himalayas or jooble.")
    title: str
    company: str | None
    location: str | None
    work_type: str | None = Field(description="Remote, Remote? (unclear), Hybrid or On-site.")
    open_to_you: bool = Field(description="True if an applicant based in Türkiye can apply.")
    match_score: int | None = Field(description="0-100 fit to the owner's CV; null if not scored.")
    date_posted: dt.date | None
    first_seen: dt.date = Field(description="First day JobPilot saw the job.")
    last_seen: dt.date = Field(description="Last day JobPilot saw the job.")
    apply_url: str | None
    skills: list[str] = Field(description="Skills the posting asks for, sorted by name.")


class JobList(BaseModel):
    items: list[JobSummary]
    limit: int
    offset: int
    has_more: bool = Field(description="True if another page exists; fetch it with offset + limit.")


class SkillOnJob(BaseModel):
    name: str
    category: str
    on_cv: bool = Field(description="True if the owner already has this skill.")


class JobDetail(BaseModel):
    id: int = Field(description="Job id.")
    source: str = Field(description="Where the job was found: linkedin, himalayas or jooble.")
    source_id: str = Field(description="The job's id at its source (LinkedIn id, Himalayas URL or jooble:<id>).")
    region: str | None
    title: str
    company: str | None
    location: str | None
    work_type: str | None = Field(description="Remote, Remote? (unclear), Hybrid or On-site.")
    open_to_you: bool = Field(description="True if an applicant based in Türkiye can apply.")
    restrictions: list[str] = Field(description="Location or eligibility limits found in the posting.")
    red_flags: list[str] = Field(description="Warning signs found in the posting, e.g. unpaid or senior-only.")
    date_posted: dt.date | None
    employment_type: str | None
    seniority: str | None
    salary: str | None = Field(description="Salary text as posted; null if not disclosed.")
    apply_url: str | None
    match_score: int | None = Field(description="0-100 fit to the owner's CV; null if not scored.")
    years_required: int | None = Field(description="Years of experience the posting asks for, if stated.")
    description: str | None = Field(description="The full posting text.")
    first_seen: dt.date = Field(description="First day JobPilot saw the job.")
    last_seen: dt.date = Field(description="Last day JobPilot saw the job.")
    updated_at: dt.datetime
    skills: list[SkillOnJob] = Field(description="Skills the posting asks for, sorted by name.")


# require_key sits in `dependencies`, which FastAPI resolves before get_conn,
# so a request without a valid key never opens a database connection
@router.get("/jobs", response_model=JobList, operation_id="list_jobs", dependencies=[Depends(require_key)],
            summary="List jobs, best CV match first, with optional filters",
            description="Returns one page of job summaries (no description text), sorted by match_score "
                        "(highest first), then most recently seen. All filters are optional and combine with AND. "
                        "Use get_job for the full posting.")
def list_jobs(
    conn=Depends(get_conn),
    open_to_you: Annotated[bool | None, Query(
        description="true: only jobs an applicant in Türkiye can apply to; false: only those they can't.")] = None,
    min_score: Annotated[int | None, Query(
        ge=0, le=100, description="Only jobs with a match_score of at least this (0-100); unscored jobs drop out.")] = None,
    skill: Annotated[str | None, Query(
        min_length=1, max_length=60, description="Only jobs that ask for this skill, e.g. Python (case-insensitive).")] = None,
    work_type: Annotated[WorkType | None, Query(description="Only jobs with this work type.")] = None,
    source: Annotated[Source | None, Query(description="Only jobs from this source.")] = None,
    since: Annotated[dt.date | None, Query(
        description="Only jobs first seen on or after this date (YYYY-MM-DD).")] = None,
    q: Annotated[str | None, Query(
        min_length=1, max_length=100, description="Text to find in the title or company (case-insensitive).")] = None,
    limit: Annotated[int, Query(ge=1, le=100, description="Page size (1-100).")] = 20,
    # capped so a huge offset can't make Postgres walk the whole table; filters narrow results better
    offset: Annotated[int, Query(ge=0, le=10000, description="How many jobs to skip, for paging (0-10000).")] = 0,
) -> JobList:
    # one extra row tells us whether there's a next page, without a separate count(*) query
    rows = queries.list_jobs(conn, limit=limit + 1, offset=offset, open_to_you=open_to_you, min_score=min_score,
                             skill=skill, work_type=work_type.value if work_type else None,
                             source=source.value if source else None, since=since, q=q)
    return JobList(items=rows[:limit], limit=limit, offset=offset, has_more=len(rows) > limit)


@router.get("/jobs/{job_id}", response_model=JobDetail, operation_id="get_job", dependencies=[Depends(require_key)],
            responses={404: {"description": "No job with this id"}},
            summary="Get one job with its full description and skills",
            description="Returns every stored field of a job, including the full description, restrictions, "
                        "red flags and each skill with whether the owner already has it (on_cv).")
# le: jobs.id is a BIGINT, so a bigger number would be a database error (500) rather than a 422
def get_job(job_id: Annotated[int, Path(ge=1, le=2**63 - 1, description="Job id from list_jobs.")],
            conn=Depends(get_conn)) -> JobDetail:
    job = queries.get_job(conn, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return JobDetail(**job)


# ---------- skills and runs ----------
class SkillDemand(BaseModel):
    name: str
    category: str
    on_cv: bool = Field(description="True if the owner already has this skill; "
                                    "false with a high share is a skill gap.")
    jobs: int = Field(description="Number of jobs first seen in the window that ask for this skill.")
    share: float = Field(description="Fraction (0-1) of all jobs first seen in the window that ask for this skill.")


class SkillDemandList(BaseModel):
    items: list[SkillDemand]
    days: int = Field(description="The window: jobs first seen in the last this many days.")


class RunInfo(BaseModel):
    id: int
    kind: str = Field(description="daily (the 12:00 scheduled run) or backfill (old Excel files loaded by hand).")
    run_date: dt.date = Field(description="The date of the data that was loaded.")
    loaded_at: dt.datetime = Field(description="When the load finished writing to the database.")
    rows_offered: int = Field(description="Job rows the load was given.")
    rows_written: int = Field(description="Jobs inserted or updated; older data than what's stored is skipped.")


class RunList(BaseModel):
    items: list[RunInfo]


@router.get("/skills", response_model=SkillDemandList, operation_id="top_skills", dependencies=[Depends(require_key)],
            summary="Most-demanded skills among recently found jobs, and which the owner lacks",
            description="Counts the skills asked for by jobs first seen in the last `days` days, most-demanded first. "
                        "Filter on_cv=false entries with a high share to find skill gaps. Skills are detected when a "
                        "job is scraped, so a newly tracked skill only shows for jobs fetched after it was added.")
def top_skills(
    conn=Depends(get_conn),
    days: Annotated[int, Query(
        ge=1, le=365, description="Window size: jobs first seen in the last this many days (1-365).")] = 30,
    # categories listed by hand (from SKILLS in job_searcher.py) so the API needn't import the pipeline;
    # a stale list only makes the hint incomplete, the filter itself accepts any text
    category: Annotated[str | None, Query(
        min_length=1, max_length=40,
        description="Only skills in this category (exact match). Current categories: AI/ML, Backend, "
                    "Cloud & DevOps, Data, Frontend & Mobile, Languages, Languages (spoken), "
                    "Microsoft & Low-code, Tools & Practices.")] = None,
    limit: Annotated[int, Query(ge=1, le=100, description="How many skills to return (1-100).")] = 20,
) -> SkillDemandList:
    return SkillDemandList(items=queries.top_skills(conn, days=days, category=category, limit=limit), days=days)


@router.get("/runs", response_model=RunList, operation_id="recent_runs", dependencies=[Depends(require_key)],
            summary="Latest database loads, newest first",
            description="One entry per load into the database. Use it to check whether today's daily load "
                        "landed: the newest daily entry should have today's run_date.")
def recent_runs(conn=Depends(get_conn),
                limit: Annotated[int, Query(ge=1, le=50, description="How many loads to return (1-50).")] = 10,
                ) -> RunList:
    return RunList(items=queries.recent_runs(conn, limit=limit))


def create_app(docs: bool | None = None) -> FastAPI:
    """Builds the app. JOBPILOT_DOCS=0 turns off /docs, /redoc and /openapi.json; anything else keeps them.
    `docs` overrides the variable (the spec snapshot always builds with docs on)."""
    # the Dockerfile sets JOBPILOT_DOCS=0, so the deployed image is closed even if nobody remembers
    # to set it in Azure (fail closed); a local run leaves it unset and /docs keeps working.
    # app.openapi() still builds the spec in code, so the OpenAPI snapshot works either way
    if docs is None:
        docs = os.environ.get("JOBPILOT_DOCS", "").strip() != "0"
    # LLM orchestrators (Copilot Studio, Power Automate, agents) pick tools by reading these texts,
    # so they say what the API is for, not just what it is
    app = FastAPI(
        title="JobPilot API",
        version="0.1.0",
        description="Read-only access to JobPilot's job-market data: internship and entry-level tech jobs "
                    "collected daily, each scored against the owner's CV, plus which skills are in demand. "
                    "Every data endpoint needs an X-API-Key header; /health doesn't.",
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.add_exception_handler(psycopg.OperationalError, db_unavailable)
    app.add_exception_handler(psycopg.errors.QueryCanceled, query_timed_out)
    app.include_router(router)
    # the Telegram bot's write route; hidden from OpenAPI, so the connector specs don't change
    app.include_router(telegram_webhook.router)
    return app


app = create_app()


# ---------- OpenAPI snapshot ----------
# docs/openapi.json is the API's contract, kept in git: step 5's Copilot Studio connector imports it,
# and a test fails when the code's spec drifts from it, so a contract change is always deliberate.
# FastAPI emits OpenAPI 3.1; Power Platform connectors want 2.0, so jobpilot.openapi2 converts it
# into docs/openapi-v2.json. Both snapshots come from this code: regenerate them together.
SPEC_FILE = os.path.join(ROOT, "docs", "openapi.json")


def spec_json() -> str:
    """The OpenAPI spec as stable text: sorted keys, so a diff shows only real changes."""
    return json.dumps(create_app(docs=True).openapi(), indent=2, sort_keys=True) + "\n"


def write_spec(path: str = SPEC_FILE) -> None:
    # written here rather than with a shell `>`, because PowerShell's `>` writes UTF-16
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(spec_json())


if __name__ == "__main__":  # python -m jobpilot.api [--write]
    if sys.argv[1:] == ["--write"]:
        write_spec()
        print(f"wrote {SPEC_FILE}")
    elif sys.argv[1:]:
        sys.exit("usage: python -m jobpilot.api [--write]")
    else:
        sys.stdout.write(spec_json())
