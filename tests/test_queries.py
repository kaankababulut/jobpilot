"""Tests for jobpilot.queries.jobs_filter: the WHERE builder (pure, no database)."""
import datetime as dt

import pytest

from jobpilot.queries import jobs_filter

EVIL = "Robert'); DROP TABLE jobs;--"


def test_no_filters_is_empty():
    assert jobs_filter() == ("", [])


@pytest.mark.parametrize("kw, column, param", [
    ({"open_to_you": False}, "j.open_to_you = %s", False),
    ({"min_score": 0}, "j.match_score >= %s", 0),
    ({"work_type": "Remote?"}, "j.work_type = %s", "Remote?"),
    ({"source": "himalayas"}, "j.source = %s", "himalayas"),
    ({"since": dt.date(2026, 9, 1)}, "j.first_seen >= %s", dt.date(2026, 9, 1)),
])
def test_each_filter_adds_one_placeholder(kw, column, param):
    # falsy values (False, 0) still count as a filter; only None means "not set"
    sql, params = jobs_filter(**kw)
    assert sql == f"WHERE {column}"
    assert params == [param]


def test_skill_filter_is_case_insensitive_exists():
    sql, params = jobs_filter(skill="PYTHON")
    assert sql.startswith("WHERE EXISTS (")
    assert "lower(s.name) = lower(%s)" in sql
    assert sql.count("%s") == 1 and params == ["PYTHON"]


def test_q_searches_title_and_company():
    sql, params = jobs_filter(q="acme")
    assert "j.title ILIKE %s ESCAPE '\\'" in sql and "j.company ILIKE %s ESCAPE '\\'" in sql
    assert params == ["%acme%", "%acme%"]


@pytest.mark.parametrize("q, pattern", [
    ("50%_off", "%50\\%\\_off%"),
    ("C:\\dev", "%C:\\\\dev%"),         # a backslash is escaped too, so it can't escape what follows
    ("100\\%", "%100\\\\\\%%"),         # backslash escaped before %, not after
])
def test_q_escapes_like_wildcards(q, pattern):
    assert jobs_filter(q=q)[1] == [pattern, pattern]


@pytest.mark.parametrize("field", ["skill", "work_type", "source", "q"])
def test_values_never_reach_the_sql_text(field):
    sql, params = jobs_filter(**{field: EVIL})
    assert "Robert'); DROP" not in sql and "DROP" not in sql
    assert sql.count("%s") == len(params)


def test_combined_filters_join_with_and_in_order():
    sql, params = jobs_filter(open_to_you=True, min_score=60, skill="sql", work_type="Remote",
                              source="linkedin", since=dt.date(2026, 9, 1), q="dev")
    assert sql.startswith("WHERE j.open_to_you = %s AND j.match_score >= %s AND EXISTS (")
    assert sql.count(" AND ") == 7  # 6 joins between filters + 1 inside the EXISTS
    assert sql.count("%s") == len(params) == 8
    assert params == [True, 60, "sql", "Remote", "linkedin", dt.date(2026, 9, 1), "%dev%", "%dev%"]
