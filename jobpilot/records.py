"""Turns one Excel-shaped job row into a dict keyed by the jobs table columns.
Pure: no database, no job_searcher import, so it can be tested on its own.
Content fields forgive bad values (they become None) so a DB CHECK never rejects a batch;
identity fields (Run Date, Job ID, Job Title) raise ValueError so the loader can skip just that row."""
import datetime as dt

HIMALAYAS_PREFIX = "https://himalayas.app/"
# mirrors the CHECK on jobs.work_type; a value outside it would fail the whole insert
WORK_TYPES = ("Remote", "Remote?", "Hybrid", "On-site")


def source_of(source_id: str) -> str:
    # Himalayas ids are guid URLs; LinkedIn ids are numeric
    return "himalayas" if source_id.startswith(HIMALAYAS_PREFIX) else "linkedin"


def _split(value) -> list[str]:
    # the Excel sheet stores lists as ", "-joined strings
    return [s.strip() for s in str(value).split(", ") if s.strip()] if value else []


def _date(value) -> dt.date | None:
    if isinstance(value, dt.datetime):  # check first: datetime is a subclass of date
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        # [:10] also accepts timestamps like 2026-09-29T08:00:00Z
        return dt.date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _int(value, lo: int, hi: int) -> int | None:
    try:
        n = int(float(value))  # Excel can hand back 85.0 for 85
    except (TypeError, ValueError):  # None, "", "-", " ", "abc"
        return None
    return n if lo <= n <= hi else None


def _source_id(value) -> str:
    # an id Excel turned into a number (e.g. 3912345678.0) must still match the text id
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    sid = "" if value is None else str(value).strip()
    if not sid:  # str(None) would otherwise become the real key "None"
        raise ValueError("row has no Job ID")
    return sid


def row_to_record(row: dict) -> dict:
    run_date = _date(row.get("Run Date"))
    if run_date is None:
        # every row job_searcher writes has one, so a missing date means a real bug upstream
        raise ValueError(f"row has no valid Run Date: {row.get('Run Date')!r} (Job ID {row.get('Job ID')!r})")
    source_id = _source_id(row.get("Job ID"))
    title = str(row.get("Job Title") or "").strip()
    if not title:  # jobs.title is NOT NULL
        raise ValueError(f"row has no Job Title (Job ID {source_id!r})")
    restr = row.get("Location Restrictions")
    salary = row.get("Salary")
    wtype = row.get("Work Type")
    skills = _split(row.get("Skills You Have")) + _split(row.get("Skills To Learn"))
    return {
        "source": source_of(source_id), "source_id": source_id, "region": row.get("Region"),
        "title": title, "company": row.get("Company Name"), "location": row.get("Location"),
        "work_type": wtype if wtype in WORK_TYPES else None,
        "open_to_you": row.get("Open To You?") == "Yes",
        "restrictions": [] if restr == "None found" else _split(restr),
        "red_flags": _split(row.get("Red Flags")),
        "date_posted": _date(row.get("Date Posted")),  # a bad posted date only loses that field
        "employment_type": row.get("Employment Type"), "seniority": row.get("Seniority Level"),
        "salary": None if salary in (None, "", "Not disclosed") else salary,
        "apply_url": row.get("Direct Application Link"),
        "match_score": _int(row.get("Match Score (/100)"), 0, 100),
        "years_required": _int(row.get("Years Required"), 0, 50),
        "description": row.get("Job Description"),
        "run_date": run_date,
        "skills": list(dict.fromkeys(skills)),  # dedupe, keep first-seen order
    }
