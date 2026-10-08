"""Tests for jobpilot.records: Excel row -> jobs table record (no database, no network)."""
import datetime as dt

import pytest

import job_searcher as js
from conftest import make_job  # cfg fixture and import path also come from conftest.py
from jobpilot.records import row_to_record, source_of

DB_KEYS = {"source", "source_id", "region", "title", "company", "location", "work_type", "open_to_you",
           "restrictions", "red_flags", "date_posted", "employment_type", "seniority", "salary", "apply_url",
           "match_score", "years_required", "description", "run_date", "skills"}
HIMALAYAS_ID = "https://himalayas.app/companies/acme/jobs/junior-python-developer"


def make_row(**kw):
    # a full row in the current JOB_COLS layout; override single columns per test
    row = {"Run Date": "2026-09-29", "Region": "Türkiye", "Job Title": "Junior Python Developer",
           "Company Name": "Acme", "Location": "Istanbul, Türkiye", "Work Type": "On-site",
           "Open To You?": "Yes", "Location Restrictions": "None found", "Red Flags": "",
           "Date Posted": "2026-09-28", "Employment Type": "Full-time", "Seniority Level": "Entry level",
           "Salary": "Not disclosed", "Direct Application Link": "https://www.linkedin.com/jobs/view/4012345678/",
           "Match Score (/100)": 72, "Years Required": "-", "Skills You Have": "Python, SQL",
           "Skills To Learn": "Docker", "Job ID": "4012345678", "Job Description": "Python and SQL."}
    row.update(kw)
    return row


# ---------- source_of ----------
def test_source_of_linkedin_id():
    assert source_of("4012345678") == "linkedin"


def test_source_of_himalayas_url():
    assert source_of(HIMALAYAS_ID) == "himalayas"


@pytest.mark.parametrize("sid", ["jooble:123", "jooble:-4567890123456789"])  # Jooble ids can be negative
def test_source_of_jooble_id(sid):
    assert source_of(sid) == "jooble"


def test_jooble_row_keeps_the_prefixed_id():
    rec = row_to_record(make_row(**{"Job ID": "jooble:123"}))
    assert (rec["source"], rec["source_id"]) == ("jooble", "jooble:123")


# ---------- row_to_record ----------
def test_record_has_exactly_the_db_keys():
    assert set(row_to_record(make_row())) == DB_KEYS


def test_himalayas_row_gets_himalayas_source():
    rec = row_to_record(make_row(**{"Job ID": HIMALAYAS_ID}))
    assert (rec["source"], rec["source_id"]) == ("himalayas", HIMALAYAS_ID)


@pytest.mark.parametrize("job_id, expected", [
    ("4012345678", "4012345678"), (" 4012345678 ", "4012345678"),
    (4012345678, "4012345678"), (4012345678.0, "4012345678"),  # Excel may turn the id into a number
])
def test_job_id_becomes_clean_text(job_id, expected):
    rec = row_to_record(make_row(**{"Job ID": job_id}))
    assert rec["source_id"] == expected and rec["source"] == "linkedin"


@pytest.mark.parametrize("job_id", [None, "", "   "])
def test_missing_job_id_raises(job_id):
    # str(None) would otherwise become the real key "None"
    with pytest.raises(ValueError):
        row_to_record(make_row(**{"Job ID": job_id}))


@pytest.mark.parametrize("title", [None, "", "  "])
def test_missing_title_raises(title):
    with pytest.raises(ValueError):
        row_to_record(make_row(**{"Job Title": title}))


@pytest.mark.parametrize("value, expected", [("-", None), (None, None), ("", None), (3, 3), ("2", 2), (4.0, 4),
                                             ("abc", None), (99, None)])
def test_years_required(value, expected):
    assert row_to_record(make_row(**{"Years Required": value}))["years_required"] == expected


@pytest.mark.parametrize("value, expected", [
    ("None found", []), (None, []), ("", []),
    ("Remote within Germany (likely), No visa sponsorship", ["Remote within Germany (likely)", "No visa sponsorship"]),
])
def test_restrictions(value, expected):
    assert row_to_record(make_row(**{"Location Restrictions": value}))["restrictions"] == expected


@pytest.mark.parametrize("value, expected", [
    (None, []), ("", []), ("Unpaid, Asks you to pay", ["Unpaid", "Asks you to pay"]),
])
def test_red_flags(value, expected):
    assert row_to_record(make_row(**{"Red Flags": value}))["red_flags"] == expected


@pytest.mark.parametrize("value, expected", [("", None), (None, None), ("Remote?", "Remote?"), ("Hybrid", "Hybrid"),
                                             ("remote", None), ("Onsite", None)])  # outside the DB CHECK
def test_work_type(value, expected):
    assert row_to_record(make_row(**{"Work Type": value}))["work_type"] == expected


@pytest.mark.parametrize("value, expected", [
    ("2026-09-28", dt.date(2026, 9, 28)),
    ("2026-09-28T08:15:00.000Z", dt.date(2026, 9, 28)),
    (dt.date(2026, 9, 28), dt.date(2026, 9, 28)),
    (dt.datetime(2026, 9, 28, 8, 15), dt.date(2026, 9, 28)),
    ("3 days ago", None), ("", None), (None, None),  # one bad row mustn't kill the batch
])
def test_date_posted(value, expected):
    assert row_to_record(make_row(**{"Date Posted": value}))["date_posted"] == expected


@pytest.mark.parametrize("value", ["2026-09-29", dt.date(2026, 9, 29), dt.datetime(2026, 9, 29, 9, 0)])
def test_run_date_accepts_text_and_dates(value):
    assert row_to_record(make_row(**{"Run Date": value}))["run_date"] == dt.date(2026, 9, 29)


@pytest.mark.parametrize("value", [None, "", "yesterday"])
def test_bad_run_date_raises(value):
    with pytest.raises(ValueError):
        row_to_record(make_row(**{"Run Date": value}))


def test_missing_run_date_raises():
    row = make_row()
    del row["Run Date"]
    with pytest.raises(ValueError):
        row_to_record(row)


@pytest.mark.parametrize("value, expected", [
    ("Not disclosed", None), ("", None), (None, None), ("30,000–40,000 USD/year", "30,000–40,000 USD/year"),
])
def test_salary(value, expected):
    assert row_to_record(make_row(**{"Salary": value}))["salary"] == expected


@pytest.mark.parametrize("value, expected", [("Yes", True), ("No", False), (None, False), ("", False)])
def test_open_to_you(value, expected):
    assert row_to_record(make_row(**{"Open To You?": value}))["open_to_you"] is expected


@pytest.mark.parametrize("value, expected", [(85, 85), (85.0, 85), ("85", 85), (None, None), ("", None),
                                             ("abc", None), (" ", None), (150, None), (-1, None), (0, 0), (100, 100)])
def test_match_score(value, expected):
    assert row_to_record(make_row(**{"Match Score (/100)": value}))["match_score"] == expected


def test_skills_are_merged_and_deduped_in_order():
    rec = row_to_record(make_row(**{"Skills You Have": "Python, SQL", "Skills To Learn": "Docker, Python"}))
    assert rec["skills"] == ["Python", "SQL", "Docker"]


def test_empty_skills():
    rec = row_to_record(make_row(**{"Skills You Have": None, "Skills To Learn": ""}))
    assert rec["skills"] == []


def test_old_layout_row_maps_without_error():
    # master files written before Work Type / Open To You? existed lack those columns
    row = make_row()
    for k in ("Work Type", "Open To You?", "Red Flags", "Location Restrictions"):
        del row[k]
    rec = row_to_record(row)
    assert rec["work_type"] is None and rec["open_to_you"] is False
    assert rec["restrictions"] == [] and rec["red_flags"] == []


# ---------- round trip through job_searcher.job_row ----------
def test_round_trip_linkedin_job_row(cfg):
    # catches an Excel column rename drifting away from the DB mapping
    job = make_job(id=4012345678, postedAt="2026-09-28", employmentType="Full-time",
                   descriptionText="Python and SQL. Docker is a plus. 3 years of experience.")
    a = js.analyse(job, cfg)
    rec = row_to_record(js.job_row("2026-09-29", job, a))
    assert set(rec) == DB_KEYS
    assert (rec["source"], rec["source_id"]) == ("linkedin", "4012345678")
    assert rec["run_date"] == dt.date(2026, 9, 29) and rec["date_posted"] == dt.date(2026, 9, 28)
    assert rec["title"] == "Junior Python Developer" and rec["company"] == "Acme"
    assert rec["location"] == "Istanbul, Türkiye" and rec["work_type"] == a["work_type"]
    assert rec["open_to_you"] is True and rec["restrictions"] == [] and rec["red_flags"] == []
    assert rec["salary"] is None and rec["employment_type"] == "Full-time" and rec["seniority"] == "Entry level"
    assert rec["apply_url"] == "https://www.linkedin.com/jobs/view/4012345678/"
    assert rec["match_score"] == a["score"] and rec["years_required"] == 3
    assert rec["skills"] == a["matched"] + a["missing"] and "Python" in rec["skills"]
    assert rec["description"].startswith("Python and SQL.")


def test_round_trip_himalayas_job_row(cfg):
    job = make_job(id=HIMALAYAS_ID, _link="https://acme.example/apply", _remote=True, location="Worldwide",
                   salary="30,000–40,000 USD/year", descriptionText="Unpaid. Python.")
    a = js.analyse(job, cfg)
    rec = row_to_record(js.job_row("2026-09-29", job, a))
    assert (rec["source"], rec["source_id"]) == ("himalayas", HIMALAYAS_ID)
    assert rec["work_type"] == "Remote" and rec["apply_url"] == "https://acme.example/apply"
    assert rec["salary"] == "30,000–40,000 USD/year" and rec["red_flags"] == a["red_flags"] == ["Unpaid"]
    assert rec["region"] is None and rec["date_posted"] is None  # no _region / postedAt on this job
