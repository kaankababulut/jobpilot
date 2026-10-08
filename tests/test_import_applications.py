"""Unit tests for jobpilot.import_applications: parsing, status words, LinkedIn ids and headers.
No database: the sheets are small xlsx files built in tmp_path with made-up companies."""
import datetime as dt

import pytest
from openpyxl import Workbook

from jobpilot import import_applications as imp

TODAY = dt.date(2026, 10, 8)
HEADER = ("Date", "Company", "Job title", "Link", "Score", "Status", "Notes")


def xlsx(path, rows) -> str:
    wb = Workbook()
    for row in rows:
        wb.active.append(row)
    wb.save(path)
    return str(path)


def parse(rows):
    return imp.parse_rows([HEADER, *rows], TODAY)


# ---------- dates ----------
@pytest.mark.parametrize("value, expected", [
    (dt.datetime(2026, 9, 1, 14, 30), dt.date(2026, 9, 1)),
    (dt.date(2026, 9, 2), dt.date(2026, 9, 2)),
    ("2026-09-03", dt.date(2026, 9, 3)),
    (" 04.09.2026 ", dt.date(2026, 9, 4)),
    (46000, dt.date(2025, 12, 9)),  # an Excel serial number from a cell without a date format
    (None, TODAY), ("", TODAY), ("  ", TODAY),
])
def test_parse_date(value, expected):
    assert imp.parse_date(value, TODAY) == expected


@pytest.mark.parametrize("value", ["09/04/2026", "yesterday", "2026-13-01", "31.02.2026"])
def test_parse_date_rejects_unreadable(value):
    with pytest.raises(ValueError):
        imp.parse_date(value, TODAY)


# ---------- statuses ----------
@pytest.mark.parametrize("value, expected", [
    (None, "applied"), ("", "applied"), ("Applied", "applied"), ("  APPLIED ", "applied"),
    ("test", "assessment"), ("Coding  Test", "assessment"), ("assessment", "assessment"),
    ("Interviewing", "interview"), ("interview", "interview"),
    ("Rejected", "rejected"), ("red", "rejected"), ("offer", "offer"), ("withdrawn", "withdrawn"),
])
def test_parse_status(value, expected):
    assert imp.parse_status(value) == expected


def test_every_mapped_status_is_one_004_allows():
    assert set(imp.STATUS_WORDS.values()) <= set(imp.feedback.STATUSES)


def test_unknown_status_raises():
    with pytest.raises(ValueError, match="unknown status 'ghosted'"):
        imp.parse_status("Ghosted")


# ---------- LinkedIn ids ----------
@pytest.mark.parametrize("url, expected", [
    ("https://www.linkedin.com/jobs/view/4012345678/", "4012345678"),
    ("https://tr.linkedin.com/jobs/view/4012345678?trk=public_jobs", "4012345678"),
    ("https://www.linkedin.com/jobs/view/junior-python-developer-at-acme-4012345678", "4012345678"),
    ("https://www.linkedin.com/jobs/search/?currentJobId=4012345678&keywords=python", "4012345678"),
    ("https://www.linkedin.com/jobs/collections/recommended/?currentJobId=4012345678", "4012345678"),
    ("https://globex.example/careers/4012345678", None),  # a number, but not LinkedIn
    ("https://www.linkedin.com/company/acme/", None),
    ("", None), (None, None),
])
def test_linkedin_id(url, expected):
    assert imp.linkedin_id(url) == expected


# ---------- rows and headers ----------
def test_parse_rows_reads_every_field():
    rows, errors = parse([("2026-09-01", " Acme ", "Data Intern", " https://acme.example/1 ", 88, "test", " via a friend ")])
    assert errors == []
    assert rows == [imp.Row(2, dt.date(2026, 9, 1), "Acme", "Data Intern", "https://acme.example/1",
                            "assessment", "via a friend")]


def test_blank_cells_become_defaults():
    rows, _ = parse([(None, "Acme", "Data Intern", None, None, None, None)])
    assert rows == [imp.Row(2, TODAY, "Acme", "Data Intern", None, "applied", None)]


def test_headers_ignore_case_whitespace_order_and_extra_columns():
    header = ("  STATUS", "job  Title ", "Extra", "company", "notes")
    rows, errors = imp.parse_rows([header, ("Rejected", "Data Intern", "x", "Acme", "n")], TODAY)
    assert errors == []
    assert rows == [imp.Row(2, TODAY, "Acme", "Data Intern", None, "rejected", "n")]


@pytest.mark.parametrize("header, missing", [
    (("Date", "Job title"), "company"), (("Company", "Title"), "job title"), ((), "company, job title"),
])
def test_missing_required_column_fails_clearly(header, missing):
    with pytest.raises(ValueError, match=f"missing column\\(s\\): {missing}"):
        imp.parse_rows([header], TODAY)


def test_blank_rows_are_skipped_and_row_numbers_stay_excel_rows():
    rows, errors = parse([(None,) * 7, ("", " ", None), ("2026-09-01", "Acme", "Data Intern")])
    assert errors == [] and [r.line for r in rows] == [4]


def test_short_rows_are_padded():
    rows, _ = parse([(None, "Acme", "Data Intern")])  # trailing cells missing entirely
    assert rows[0].status == "applied" and rows[0].notes is None


def test_bad_rows_are_errors_not_crashes():
    rows, errors = parse([
        ("2026-09-01", "Acme", "Data Intern", None, None, "ghosted"),
        ("someday", "Globex", "QA Intern"),
        (None, "", "No Company Intern"),
        (None, "Initech", None, None, None, None, "a note but no title"),
        (None, "Umbrella", "Backend Intern"),
    ])
    assert [r.company for r in rows] == ["Umbrella"]
    assert [line for line, _ in errors] == [2, 3, 4, 5]
    assert "unknown status" in errors[0][1] and "unreadable date" in errors[1][1]
    assert errors[2][1] == errors[3][1] == "Company and Job title are required"


def test_numbers_in_text_columns_become_text():
    rows, _ = parse([(None, 3.0, 2026.0)])
    assert (rows[0].company, rows[0].title) == ("3", "2026")


def test_read_sheet_round_trips_an_xlsx(tmp_path):
    path = xlsx(tmp_path / "applications.xlsx", [HEADER, (dt.datetime(2026, 9, 1), "Acme", "Data Intern", None, 70, "Interviewing")])
    rows, errors = imp.parse_rows(imp.read_sheet(path), TODAY)
    assert errors == [] and rows[0].applied_on == dt.date(2026, 9, 1) and rows[0].status == "interview"


# ---------- main, without a database ----------
def test_main_missing_file(tmp_path, capsys):
    assert imp.main([str(tmp_path / "nope.xlsx")]) == 1
    assert "no such file" in capsys.readouterr().err


def test_main_missing_column_fails_before_connecting(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(imp, "connect", lambda url: pytest.fail("must not connect"))
    path = xlsx(tmp_path / "a.xlsx", [("Date", "Company"), ("2026-09-01", "Acme")])
    assert imp.main([path]) == 1
    assert "missing column(s): job title" in capsys.readouterr().err


def test_main_unset_url_names_the_variable_only(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(imp, "load_dotenv", lambda *a, **k: False)
    monkeypatch.delenv("JOBPILOT_TEST_URL", raising=False)
    path = xlsx(tmp_path / "a.xlsx", [HEADER, (None, "Acme", "Data Intern")])
    assert imp.main([path, "--url-env", "JOBPILOT_TEST_URL"]) == 1
    assert "JOBPILOT_TEST_URL not set" in capsys.readouterr().err


def test_main_redacts_the_url_on_a_db_error(tmp_path, capsys, monkeypatch):
    url = "postgresql://jobs:s3cretPass@127.0.0.1:5432/jobs"
    monkeypatch.setenv("JOBPILOT_TEST_URL", url)

    def boom(u):
        raise RuntimeError(f"could not connect to {u}")
    monkeypatch.setattr(imp, "connect", boom)
    path = xlsx(tmp_path / "a.xlsx", [HEADER, (None, "Acme", "Data Intern")])
    assert imp.main([path, "--url-env", "JOBPILOT_TEST_URL"]) == 1
    out = capsys.readouterr()
    assert "nothing written" in out.err and "s3cretPass" not in out.err + out.out and url not in out.err
