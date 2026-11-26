"""Integration tests for migration 006 (llm_calls, llm_spend_monthly). Needs the local Postgres (pytest -m db).
The pg fixture has already applied every migration to its throwaway schema."""
from datetime import date
from decimal import Decimal

import pytest
from psycopg import errors

from test_db import one
from test_feedback_schema_db import FEEDBACK_ROLE, as_role
from test_migrate_db import recorded, table_exists

pytestmark = pytest.mark.db


def add_call(conn, status: str = "ok", est: str = "0.10", cost: str | None = "0.05", model: str = "m1",
             purpose: str = "draft", created_at: str | None = None, input_tokens: int | None = None) -> int:
    # created_at given as an ISO string with an offset, so it's an absolute moment whatever the session zone
    return one(conn, "INSERT INTO llm_calls (purpose, model, status, est_cost_usd, cost_usd, input_tokens, created_at) "
                     "VALUES (%s, %s, %s, %s, %s, %s, COALESCE(%s::timestamptz, now())) RETURNING id",
               (purpose, model, status, est, cost, input_tokens, created_at))[0]


def spend(conn) -> list[tuple]:
    return conn.execute("SELECT month, model, calls, errors, spend_usd FROM llm_spend_monthly "
                        "ORDER BY month, model").fetchall()


def test_006_creates_the_table_and_view(pg):
    assert table_exists(pg, "llm_calls") and table_exists(pg, "llm_spend_monthly")
    assert ("006", "llm_calls") in recorded(pg)


def test_new_row_defaults_to_reserved_with_no_cost(pg):
    pg.execute("INSERT INTO llm_calls (purpose, model, est_cost_usd) VALUES ('smoke', 'm1', 0.01)")
    assert one(pg, "SELECT status, cost_usd, created_at IS NOT NULL FROM llm_calls") == ("reserved", None, True)


@pytest.mark.parametrize("status", ["ok", "error", "refused"])
def test_settled_row_needs_a_cost(pg, status):
    with pytest.raises(errors.CheckViolation):
        add_call(pg, status=status, cost=None)


def test_settling_a_reservation_without_a_cost_is_rejected(pg):
    call = add_call(pg, status="reserved", cost=None)
    with pytest.raises(errors.CheckViolation):
        pg.execute("UPDATE llm_calls SET status = 'ok' WHERE id = %s", (call,))


def test_reserved_row_cannot_have_a_cost(pg):
    with pytest.raises(errors.CheckViolation):
        add_call(pg, status="reserved", cost="0.05")


@pytest.mark.parametrize("est,cost", [("-0.01", "0.05"), ("0.10", "-0.01"), ("-0.01", None)])
def test_negative_amounts_are_rejected(pg, est, cost):
    with pytest.raises(errors.CheckViolation):
        add_call(pg, status="reserved" if cost is None else "ok", est=est, cost=cost)


def test_refused_row_must_cost_zero(pg):
    with pytest.raises(errors.CheckViolation):
        add_call(pg, status="refused", cost="0.01")


@pytest.mark.parametrize("column,value", [("status", "sent"), ("purpose", "chat")])
def test_status_and_purpose_checks(pg, column, value):
    with pytest.raises(errors.CheckViolation):
        add_call(pg, **{column: value})


@pytest.mark.parametrize("purpose", ["enrich", "draft", "honesty", "smoke"])
def test_every_purpose_is_accepted(pg, purpose):
    add_call(pg, purpose=purpose)


def test_view_counts_reserved_at_estimate_and_refused_at_zero(pg):
    when = "2026-11-10 12:00+00"
    add_call(pg, "ok", est="0.10", cost="0.04", created_at=when, input_tokens=100)
    add_call(pg, "reserved", est="0.20", cost=None, created_at=when)  # in flight: may be billed
    add_call(pg, "error", est="0.30", cost="0", created_at=when)  # API refused it: not billed
    add_call(pg, "error", est="0.30", cost="0.30", created_at=when)  # timeout: may be billed, keeps the estimate
    add_call(pg, "refused", est="0.90", cost="0", created_at=when)  # never sent
    assert spend(pg) == [(date(2026, 11, 1), "m1", 4, 2, Decimal("0.540000"))]
    assert one(pg, "SELECT input_tokens FROM llm_spend_monthly")[0] == 100


def test_view_groups_by_model(pg):
    add_call(pg, model="a", cost="0.01", created_at="2026-11-10 12:00+00")
    add_call(pg, model="b", cost="0.02", created_at="2026-11-10 12:00+00")
    assert [(r[1], r[4]) for r in spend(pg)] == [("a", Decimal("0.010000")), ("b", Decimal("0.020000"))]


@pytest.mark.parametrize("zone", ["UTC", "Europe/Istanbul", "America/Los_Angeles"])
def test_view_buckets_by_utc_month_whatever_the_session_zone(pg, zone):
    pg.execute(f"SET TIME ZONE '{zone}'")
    try:
        add_call(pg, cost="0.01", created_at="2026-11-30 23:30+00")  # already December in Istanbul
        add_call(pg, cost="0.02", created_at="2026-12-01 00:30+00")  # still November in Los Angeles
        assert [(r[0], r[4]) for r in spend(pg)] == [(date(2026, 11, 1), Decimal("0.010000")),
                                                    (date(2026, 12, 1), Decimal("0.020000"))]
    finally:
        pg.execute("RESET TIME ZONE")


@pytest.mark.parametrize("name", ["llm_calls", "llm_spend_monthly"])
def test_api_role_can_read(pg, name):
    add_call(pg)
    with as_role(pg, "jobpilot_api"):
        assert one(pg, f"SELECT count(*) FROM {name}")[0] == 1


@pytest.mark.parametrize("sql", [
    "INSERT INTO llm_calls (purpose, model, est_cost_usd) VALUES ('smoke', 'm', 0)",
    "UPDATE llm_calls SET cost_usd = 0",
    "DELETE FROM llm_calls",
])
def test_api_role_cannot_write(pg, sql):
    with as_role(pg, "jobpilot_api"), pytest.raises(errors.InsufficientPrivilege):
        pg.execute(sql)


@pytest.mark.parametrize("name", ["llm_calls", "llm_spend_monthly"])
def test_feedback_role_has_no_access(pg, name):
    with as_role(pg, FEEDBACK_ROLE), pytest.raises(errors.InsufficientPrivilege):
        pg.execute(f"SELECT * FROM {name}")
