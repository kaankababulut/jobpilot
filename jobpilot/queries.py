"""Read-only SQL for the API: list and fetch jobs. Takes a psycopg connection the caller opens
(ideally `connect(url, read_only=True)`). SQL only: no FastAPI import, no input validation;
ranges and enums are checked in the API layer. Values only ever reach Postgres as %s parameters,
never pasted into the SQL text, so user input can't change the query.
Even a SELECT opens a transaction in psycopg: use autocommit or a short `with connect(...)` block
per request, so a connection is never left idle in transaction (holding locks and a snapshot)."""
import datetime as dt

from psycopg.rows import dict_row

# the list view leaves out description, restrictions and red_flags to keep each row small
SUMMARY_COLS = ("id", "source", "title", "company", "location", "work_type", "open_to_you", "match_score",
                "date_posted", "first_seen", "last_seen", "apply_url")
# listed, not SELECT *, so a future column (e.g. a pgvector embedding) is exposed on purpose, not by accident
DETAIL_COLS = ("id", "source", "source_id", "region", "title", "company", "location", "work_type",
               "open_to_you", "restrictions", "red_flags", "date_posted", "employment_type", "seniority",
               "salary", "apply_url", "match_score", "years_required", "description", "first_seen",
               "last_seen", "updated_at")
# a correlated subquery rather than JOIN + GROUP BY, so a job without skills still appears once;
# COALESCE because array_agg over no rows gives NULL, and callers expect []
SKILLS_SQL = ("COALESCE((SELECT array_agg(s.name ORDER BY s.name) FROM job_skills js "
              "JOIN skills s USING (skill_id) WHERE js.job_id = j.id), '{}') AS skills")
# best fit first; last_seen then id break ties, so paging with OFFSET never repeats or skips a row
ORDER_SQL = "ORDER BY j.match_score DESC NULLS LAST, j.last_seen DESC, j.id"


def _like_pattern(q: str) -> str:
    # escape the backslash first, or it would double the escapes added for % and _
    q = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{q}%"


def jobs_filter(open_to_you: bool | None = None, min_score: int | None = None, skill: str | None = None,
                work_type: str | None = None, source: str | None = None, since: dt.date | None = None,
                q: str | None = None) -> tuple[str, list]:
    """Builds "WHERE ..." (or "" with no filters) over jobs aliased as j, plus its parameters.
    Column names are fixed here; only values come from the caller, and only as parameters."""
    parts: list[str] = []
    params: list = []
    if open_to_you is not None:
        parts.append("j.open_to_you = %s")
        params.append(open_to_you)
    if min_score is not None:  # NULL scores fail >= and drop out, which is what "at least N" means
        parts.append("j.match_score >= %s")
        params.append(min_score)
    if skill is not None:
        # EXISTS, not a JOIN, so a job never comes back twice; lower() on both sides for case-insensitivity
        parts.append("EXISTS (SELECT 1 FROM job_skills js JOIN skills s USING (skill_id) "
                     "WHERE js.job_id = j.id AND lower(s.name) = lower(%s))")
        params.append(skill)
    if work_type is not None:
        parts.append("j.work_type = %s")
        params.append(work_type)
    if source is not None:
        parts.append("j.source = %s")
        params.append(source)
    if since is not None:
        parts.append("j.first_seen >= %s")
        params.append(since)
    if q is not None:
        # % and _ are wildcards in LIKE; escaped, so a search for "50%" means the literal text
        parts.append("(j.title ILIKE %s ESCAPE '\\' OR j.company ILIKE %s ESCAPE '\\')")
        params += [_like_pattern(q)] * 2
    return ("WHERE " + " AND ".join(parts), params) if parts else ("", [])


def list_jobs(conn, limit: int = 20, offset: int = 0, **filters) -> list[dict]:
    """One page of job summaries (no description), each with a sorted `skills` list of names."""
    where, params = jobs_filter(**filters)
    sql = (f"SELECT {', '.join('j.' + c for c in SUMMARY_COLS)}, {SKILLS_SQL} FROM jobs j "
           f"{where} {ORDER_SQL} LIMIT %s OFFSET %s")
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params + [limit, offset]).fetchall()


def get_job(conn, job_id: int) -> dict | None:
    """The full jobs row plus `skills` as [{name, category, on_cv}] sorted by name; None if no such id."""
    with conn.cursor(row_factory=dict_row) as cur:
        job = cur.execute(f"SELECT {', '.join(DETAIL_COLS)} FROM jobs WHERE id = %s", (job_id,)).fetchone()
        if job is None:
            return None
        job["skills"] = cur.execute(
            "SELECT s.name, s.category, s.on_cv FROM job_skills js JOIN skills s USING (skill_id) "
            "WHERE js.job_id = %s ORDER BY s.name", (job_id,)).fetchall()
    return job
