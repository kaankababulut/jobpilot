"""Read-only HTTP API over the job database (FastAPI): API-key auth, one read-only connection per
request, /health and the job endpoints. SQL lives in jobpilot.queries; this layer validates input.
Run locally: uvicorn jobpilot.api:app --host 127.0.0.1 --port 8000
(127.0.0.1 only, so nothing outside this PC can reach it; docs at http://127.0.0.1:8000/docs).
Endpoints are plain `def`, not async: psycopg calls block, and FastAPI runs sync endpoints in a
threadpool so one slow query doesn't stall the others. No connection pool yet; one user doesn't need it."""
import datetime as dt
import logging
import os
import secrets
from enum import Enum
from typing import Annotated, Iterator

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from jobpilot import queries
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


# the response models are flat (no nesting beyond the skills list) and use plain types, because
# Power Platform connectors import OpenAPI 2.0, which handles simple schemas best
class JobSummary(BaseModel):
    id: int = Field(description="Job id; pass it to get_job for the full posting.")
    source: str = Field(description="Where the job was found: linkedin or himalayas.")
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
    source: str = Field(description="Where the job was found: linkedin or himalayas.")
    source_id: str = Field(description="The job's id at its source (LinkedIn id or Himalayas URL).")
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
@app.get("/jobs", response_model=JobList, operation_id="list_jobs", dependencies=[Depends(require_key)],
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


@app.get("/jobs/{job_id}", response_model=JobDetail, operation_id="get_job", dependencies=[Depends(require_key)],
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
