"""Integration tests for the LLM budget ledger (llm.month_spend, reserve, settle). Needs the local Postgres
(pytest -m db); every row goes into the pg fixture's throwaway schema."""
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from jobpilot import llm
from jobpilot.db import connect
from test_db import one

pytestmark = pytest.mark.db

CAP, MAX_CALL = Decimal("1.00"), Decimal("0.50")


def add_row(conn, status: str, est: str, cost: str | None, created_at: datetime | None = None) -> int:
    return one(conn, "INSERT INTO llm_calls (purpose, model, status, est_cost_usd, cost_usd, created_at) "
                     "VALUES ('draft', 'm1', %s, %s, %s, COALESCE(%s, now())) RETURNING id",
               (status, est, cost, created_at))[0]


def row(conn, call_id: int) -> tuple:
    return one(conn, "SELECT status, est_cost_usd, cost_usd, input_tokens, output_tokens, cache_creation_tokens, "
                     "cache_read_tokens, stop_reason, error_type, request_id, finished_at IS NOT NULL "
                     "FROM llm_calls WHERE id = %s", (call_id,))


def statuses(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT status FROM llm_calls ORDER BY id")]


def reserve(conn, est: str, cap: Decimal = CAP, max_call: Decimal = MAX_CALL) -> int:
    return llm.reserve(conn, "draft", "m1", Decimal(est), cap, max_call)


def utc_month_start(conn) -> datetime:
    # from the database clock, the same now() month_spend uses, so a skewed PC clock can't move the bound
    return one(conn, "SELECT date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'")[0]


# a bigint advisory lock shows in pg_locks as classid = high 32 bits, objid = low 32 bits, objsubid = 1
WAITING_ON_LEDGER_SQL = """
SELECT count(*) FROM pg_locks
WHERE locktype = 'advisory' AND NOT granted AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
  AND classid::bigint = %s AND objid::bigint = %s AND objsubid = 1
"""


@contextmanager
def second_connection(pg):
    # a real, separate session (not autocommit, like the production connect()) on the same throwaway schema
    conn = connect(os.environ["DATABASE_URL"])
    try:
        conn.execute(f"SET search_path TO {one(pg, 'SHOW search_path')[0]}")
        conn.commit()
        yield conn
    finally:
        conn.close()


def test_under_the_cap_reserves_a_row(pg):
    call = reserve(pg, "0.30")
    assert row(pg, call)[:3] == ("reserved", Decimal("0.300000"), None)
    assert llm.month_spend(pg) == Decimal("0.30")


def test_over_the_cap_commits_a_refused_row_and_raises(pg):
    add_row(pg, "ok", "0.90", "0.90")
    with pytest.raises(llm.BudgetExceeded, match=r"\$0\.100000 remaining"):
        reserve(pg, "0.20")
    with second_connection(pg) as other:  # another session sees it, so it was committed despite the raise
        assert other.execute("SELECT status, est_cost_usd, cost_usd FROM llm_calls WHERE status <> 'ok'").fetchall() \
            == [("refused", Decimal("0.200000"), Decimal("0.000000"))]
    assert statuses(pg) == ["ok", "refused"]  # nothing reserved


def test_over_max_call_is_refused_even_with_budget_left(pg):
    with pytest.raises(llm.BudgetExceeded, match="per-call limit"):
        reserve(pg, "0.51")
    assert statuses(pg) == ["refused"]
    assert llm.month_spend(pg) == 0


def test_exactly_at_the_cap_is_allowed_and_a_micro_dollar_over_is_refused(pg):
    add_row(pg, "ok", "0.60", "0.60")
    reserve(pg, "0.40")  # 0.60 + 0.40 == 1.00
    assert llm.month_spend(pg) == CAP
    with pytest.raises(llm.BudgetExceeded):
        reserve(pg, "0.000001")
    assert statuses(pg) == ["ok", "reserved", "refused"]


def test_a_reserved_row_counts_at_its_estimate(pg):
    reserve(pg, "0.50")
    reserve(pg, "0.50")
    with pytest.raises(llm.BudgetExceeded):
        reserve(pg, "0.01")  # the two in-flight calls already hold the whole cap


def test_settle_ok_writes_cost_and_tokens_and_frees_the_difference(pg):
    call = reserve(pg, "0.50")
    usage = {"input_tokens": 1200, "output_tokens": 300, "cache_creation_input_tokens": None,
             "cache_read_input_tokens": 7}
    assert llm.settle(pg, call, "ok", Decimal("0.10"), usage, stop_reason="end_turn", request_id="req_1")         == llm.Settlement(True, Decimal(0))
    assert row(pg, call) == ("ok", Decimal("0.500000"), Decimal("0.100000"), 1200, 300, 0, 7, "end_turn",
                             None, "req_1", True)
    assert llm.month_spend(pg) == Decimal("0.10")
    reserve(pg, "0.50")  # 0.10 + 0.50 fits again now that the real cost replaced the estimate


def test_settle_error_with_zero_cost(pg):
    call = reserve(pg, "0.40")
    assert llm.settle(pg, call, "error", Decimal(0), error_type="APIStatusError")
    assert row(pg, call)[:4] == ("error", Decimal("0.400000"), Decimal("0.000000"), None)
    assert row(pg, call)[8] == "APIStatusError"
    assert llm.month_spend(pg) == 0


def test_settle_error_keeping_the_estimate(pg):
    call = reserve(pg, "0.40")
    assert llm.settle(pg, call, "error", None, error_type="APITimeoutError")  # None keeps the estimate
    assert row(pg, call)[:3] == ("error", Decimal("0.400000"), Decimal("0.400000"))
    assert llm.month_spend(pg) == Decimal("0.40")


def test_settling_twice_is_a_no_op(pg):
    call = reserve(pg, "0.40")
    assert llm.settle(pg, call, "ok", Decimal("0.05"), {"input_tokens": 10})
    before = row(pg, call)
    again = llm.settle(pg, call, "error", Decimal(0), error_type="X")
    assert not again and again == llm.Settlement(False)
    assert row(pg, call) == before


def test_a_cost_over_the_estimate_is_recorded_and_reported(pg):
    call = reserve(pg, "0.10")
    result = llm.settle(pg, call, "ok", Decimal("0.15"), {"input_tokens": 90_000})  # e.g. emoji-heavy text
    assert result == llm.Settlement(True, Decimal("0.05"))  # the caller can log the overrun
    assert row(pg, call)[:3] == ("ok", Decimal("0.100000"), Decimal("0.150000"))  # what was billed, not capped
    assert llm.month_spend(pg) == Decimal("0.15")


def test_settle_rejects_other_statuses(pg):
    call = reserve(pg, "0.10")
    with pytest.raises(ValueError):
        llm.settle(pg, call, "refused", Decimal(0))
    assert row(pg, call)[0] == "reserved"


def test_last_months_rows_are_ignored(pg):
    start = utc_month_start(pg)
    add_row(pg, "ok", "0.90", "0.90", start - timedelta(microseconds=1))
    add_row(pg, "reserved", "0.90", None, start - timedelta(days=20))
    assert llm.month_spend(pg) == 0
    reserve(pg, "0.50")


@pytest.mark.parametrize("zone", ["UTC", "America/Los_Angeles", "Pacific/Kiritimati"])
def test_month_boundary_is_utc_whatever_the_session_zone(pg, zone):
    # Los Angeles' month starts 8 h after UTC's and Kiritimati's 14 h before, so a local-time bound would
    # drop the first row or pick up the second
    start = utc_month_start(pg)
    add_row(pg, "ok", "0.20", "0.20", start + timedelta(hours=1))
    add_row(pg, "ok", "0.40", "0.40", start - timedelta(hours=1))
    pg.execute(f"SET TIME ZONE '{zone}'")
    try:
        assert llm.month_spend(pg) == Decimal("0.20")
    finally:
        pg.execute("RESET TIME ZONE")


def test_refuses_to_run_inside_an_open_transaction(pg):
    with second_connection(pg) as other:
        other.execute("SELECT 1")  # psycopg opened a transaction that the caller hasn't ended
        with pytest.raises(llm.LedgerBusy):
            reserve(other, "0.10")
        other.rollback()
        reserve(other, "0.10")  # fine once the caller's transaction is closed
        assert other.info.transaction_status.name == "IDLE"  # and reserve left nothing open


def test_month_spend_then_reserve_on_a_non_autocommit_connection(pg):
    with second_connection(pg) as conn:  # like production: connect() leaves autocommit off
        assert llm.month_spend(conn) == 0
        assert conn.info.transaction_status.name == "IDLE"  # month_spend ended its read transaction
        reserve(conn, "0.10")
        assert llm.month_spend(conn) == Decimal("0.10")


def test_inside_an_explicit_transaction_on_autocommit_reserve_and_settle_are_refused(pg):
    call = reserve(pg, "0.10")
    with pytest.raises(llm.LedgerBusy), pg.transaction():
        reserve(pg, "0.10")
    with pytest.raises(llm.LedgerBusy), pg.transaction():
        llm.settle(pg, call, "ok", Decimal("0.05"))
    assert statuses(pg) == ["reserved"]


@pytest.mark.parametrize("est,cap,error", [
    (Decimal("0"), CAP, ValueError),
    (Decimal("-0.01"), CAP, ValueError),
    (0.10, CAP, TypeError),
    (Decimal("0.10"), 1.0, TypeError),
])
def test_bad_amounts_are_rejected_before_anything_is_written(pg, est, cap, error):
    with pytest.raises(error):
        llm.reserve(pg, "draft", "m1", est, cap, MAX_CALL)
    assert statuses(pg) == []


def test_estimate_rounds_up_to_the_stored_micro_dollar(pg):
    call = reserve(pg, "0.0000001")  # would be 0 rounded half-up, which would hide the call
    assert row(pg, call)[1] == Decimal("0.000001")


def test_reservation_waits_for_the_lock_and_sees_the_holders_booking(pg):
    # deterministic: hold the ledger lock in one session, start a reserve in another, check it is really
    # waiting on the lock, book the whole cap and release. The waiter must read the spend after the lock.
    with second_connection(pg) as holder, second_connection(pg) as waiter:
        holder.execute("SELECT pg_advisory_xact_lock(%s)", (llm.LEDGER_LOCK,))
        result = {}

        def run() -> None:
            try:
                result["id"] = reserve(waiter, "0.50")
            except llm.BudgetExceeded as e:
                result["error"] = e

        t = threading.Thread(target=run)
        t.start()
        for _ in range(200):  # up to 10 s for the waiter's lock request to show up as not granted
            if holder.execute(WAITING_ON_LEDGER_SQL, (llm.LEDGER_LOCK >> 32, llm.LEDGER_LOCK & 0xFFFFFFFF)).fetchone()[0]:
                break
            time.sleep(0.05)
        else:
            pytest.fail("the second reserve never waited on the ledger lock")
        assert "id" not in result and "error" not in result
        holder.execute("INSERT INTO llm_calls (purpose, model, est_cost_usd) VALUES ('draft', 'm1', 0.80)")
        holder.commit()  # releases the lock
        t.join(10)
        assert not t.is_alive()
        assert isinstance(result.get("error"), llm.BudgetExceeded)
    assert statuses(pg) == ["reserved", "refused"]


def test_two_simultaneous_reservations_cannot_both_pass(pg):
    with second_connection(pg) as a, second_connection(pg) as b:
        go, results = threading.Barrier(2), []

        def run(conn) -> None:
            go.wait()
            try:
                results.append(reserve(conn, "0.60", max_call=CAP))  # only one 0.60 fits in 1.00
            except llm.BudgetExceeded:
                results.append("refused")

        threads = [threading.Thread(target=run, args=(c,)) for c in (a, b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
            assert not t.is_alive()
    assert results.count("refused") == 1
    assert sorted(statuses(pg)) == ["refused", "reserved"]
