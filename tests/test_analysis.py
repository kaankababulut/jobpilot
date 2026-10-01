"""Tests for the pure analysis functions in job_searcher.py (no network, no Excel files)."""
import pytest

import job_searcher as js
from conftest import make_job  # cfg fixture and import path also come from conftest.py


# ---------- config consistency ----------
def test_every_cv_skill_is_a_known_skill(cfg):
    # a typo in cv_skills would silently never match anything
    assert set(cfg["cv_skills"]) <= set(js.SKILLS)


# ---------- Microsoft skill patterns ----------
def found(text: str) -> set[str]:
    return {s for s, rx in js.SKILL_RE.items() if rx.search(text)}


@pytest.mark.parametrize("text, skill", [
    ("Build agents in Copilot Studio", "Copilot Studio"),
    ("Experience with Power Virtual Agents", "Copilot Studio"),
    ("Roll out Microsoft 365 Copilot to users", "Microsoft 365 Copilot"),
    ("M365 Copilot adoption", "Microsoft 365 Copilot"),
    ("Daily use of GitHub Copilot", "GitHub Copilot"),
    ("Canvas apps in PowerApps", "Power Apps"),
    ("Automate flows with Power Automate", "Power Automate"),
    ("Power Platform and Dataverse", "Power Platform"),
    ("Azure OpenAI and Azure AI Foundry", "Azure AI / OpenAI"),
    ("SharePoint Online and Office 365", "SharePoint / M365"),
    ("Dynamics 365 CRM customisation", "Dynamics 365"),
])
def test_microsoft_skills_are_detected(text, skill):
    assert skill in found(text)


@pytest.mark.parametrize("text, absent", [
    ("Daily use of GitHub Copilot", "Microsoft 365 Copilot"),  # different products
    ("Microsoft Teams copilot integration", "Microsoft 365 Copilot"),  # "...ms copilot" inside "teams"
    ("Roll out Microsoft 365 Copilot", "SharePoint / M365"),  # Copilot isn't SharePoint/M365 admin
])
def test_microsoft_skills_dont_overlap(text, absent):
    assert absent not in found(text)


# ---------- years_required ----------
@pytest.mark.parametrize("text, expected", [
    ("Requires 3+ years of experience", 3),
    ("at least 2 years in backend work", 2),
    ("2-4 years of experience", 2),
    ("en az 2 yıl deneyim", 2),
    ("no experience needed", None),
    ("company founded 20 years ago", None),  # >15 is ignored
])
def test_years_required(text, expected):
    assert js.years_required(text) == expected


# ---------- red flags / internship mills ----------
def test_unpaid_is_a_mill():
    flags = js.red_flags("This is an unpaid internship.")
    assert flags == ["Unpaid"]
    assert js.is_mill(flags)


def test_single_soft_flag_is_not_a_mill():
    flags = js.red_flags("You will receive a certificate of completion.")
    assert flags == ["Certificate/LOR as the reward"]
    assert not js.is_mill(flags)


def test_two_soft_flags_are_a_mill():
    flags = js.red_flags("Performance-based stipend and a certificate of completion.")
    assert js.is_mill(flags)


# ---------- work_type ----------
@pytest.mark.parametrize("job, expected", [
    ({"title": "Data Engineer (Remote)", "location": "Istanbul"}, "Remote"),
    ({"title": "Developer", "location": "Istanbul", "descriptionText": "Hybrid work, 2 days in office"}, "Hybrid"),
    ({"title": "Developer", "location": "Istanbul", "descriptionText": "Some remote days possible"}, "Remote?"),
    ({"title": "Developer", "location": "Istanbul", "descriptionText": "Office in Levent"}, "On-site"),
    ({"title": "Developer", "location": "x", "_remote": True}, "Remote"),
])
def test_work_type(job, expected):
    assert js.work_type(job) == expected


# ---------- relevant ----------
@pytest.mark.parametrize("job, expected", [
    (make_job(title="Junior Data Engineer"), True),
    (make_job(title="Senior Software Engineer"), False),
    (make_job(title="Sales Intern"), False),
    (make_job(companyName="Jobgether Partners"), False),
    (make_job(title="Software Engineer", seniorityLevel="Mid-Senior level"), False),
    # generic titles need at least 3 real tech skills in the description
    (make_job(title="Intern", descriptionText="Great team, free coffee."), False),
    (make_job(title="Intern", descriptionText="Python, SQL and Docker."), True),
])
def test_relevant(cfg, job, expected):
    assert js.relevant(job, cfg) is expected


# ---------- analyse + job_row ----------
def test_job_in_turkiye_is_open_to_you(cfg):
    job = make_job()
    row = js.job_row("2026-09-29", job, js.analyse(job, cfg))
    assert row["Open To You?"] == "Yes"
    assert "Python" in row["Skills You Have"]


def test_us_work_authorization_is_not_open(cfg):
    job = make_job(location="Worldwide",
                   descriptionText="Python. Must be authorized to work in the United States.")
    a = js.analyse(job, cfg)
    assert "United States work authorization" in a["restrictions"]
    assert js.job_row("2026-09-29", job, a)["Open To You?"] == "No"


def test_remote_in_other_country_is_not_open(cfg):
    job = make_job(title="Junior Python Developer (Remote)", location="Berlin, Germany")
    a = js.analyse(job, cfg)
    assert "Remote within Germany (likely)" in a["restrictions"]


def test_years_required_lowers_score(cfg):
    base = js.analyse(make_job(descriptionText="Python and SQL."), cfg)
    senior = js.analyse(make_job(descriptionText="Python and SQL. 5 years of experience."), cfg)
    assert senior["score"] < base["score"]


def test_score_is_clamped(cfg):
    job = make_job(location="Nowhere, Mars", descriptionText="10 years. Must be a U.S. citizen.")
    assert 0 <= js.analyse(job, cfg)["score"] <= 100


# ---------- Excel output ----------
def test_workbook_with_no_new_jobs_is_written(cfg, tmp_path):
    # a quiet day: postings came back but none were new, so only the header row exists
    path = str(tmp_path / "jobs_2026-10-02.xlsx")
    assert js.build_workbook(path, [], cfg, "2026-10-02 only")
    assert js.load_master_rows(path) == []
