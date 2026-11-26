"""Claude calls for outreach: prices, cost accounting and the wrapper for untrusted text.
Prices, estimates and untrusted() are pure; the budget ledger (month_spend, reserve, settle) writes to the
llm_calls table; the API call comes in a later step. Every cost is a Decimal, because floats can't hold 0.1 exactly and a spend cap
that drifts by rounding errors is not a cap.

Page text is data, never instructions. Anything fetched from a company website goes through untrusted(),
which wraps it in <untrusted_page> and neutralises < and >, so the page can't close the tag and pose as
our own prompt. The system prompt tells the model that whatever is inside the tag is only material to read."""
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal


class UnknownModel(ValueError):
    """A model with no known price. Fail closed: we can't check the budget for a call we can't price."""


class BudgetExceeded(Exception):
    """The call would go over the monthly cap or max_call_usd, so it was refused before reaching the API."""


class LedgerBusy(RuntimeError):
    """The connection was already inside a transaction. reserve/settle must commit on their own, so they
    refuse rather than commit (or nest inside) someone else's unfinished work."""


class LLMError(Exception):
    """The API call failed (status, connection or timeout error); the ledger row records the error type."""


# USD per 1M tokens, by exact model id. Taken from Anthropic's pricing page on 2026-11-26.
# Re-check the page whenever you change or add a price: a wrong price here makes the budget cap wrong.
PRICES: dict[str, dict[str, Decimal]] = {
    "claude-opus-5-5": {"input": Decimal("4"), "output": Decimal("20")},
    "claude-sonnet-5-5": {"input": Decimal("2"), "output": Decimal("10")},
    "claude-haiku-5-5": {"input": Decimal("0.10"), "output": Decimal("0.50")},
}
# Haiku 5.5's price above holds for prompts up to 100K tokens (it rises beyond); estimate_cost refuses
# bigger prompts rather than price them too low. Outreach prompts are a few thousand tokens.
MAX_INPUT_TOKENS: dict[str, int] = {"claude-haiku-5-5": 100_000}
CACHE_WRITE_MULTIPLIER = Decimal("1.25")  # writing a prompt to the cache costs 1.25x the input price
CACHE_READ_MULTIPLIER = Decimal("0.1")    # reading it back costs 0.1x
PER_TOKENS = Decimal(1_000_000)
USD_PLACES = Decimal("0.000001")  # matches NUMERIC(10,6) in llm_calls
CHARS_PER_TOKEN = 2  # deliberately low (real text is ~3-4 chars per token), so estimates come out high

# Estimates round UP (ROUND_CEILING): they guard the cap, and rounding one down could let a call through
# that the cap should have refused. Real costs round half-up, the normal accounting rule, so they are fair.


def _prices(model: str) -> dict[str, Decimal]:
    if model not in PRICES:
        raise UnknownModel(f"no price for model {model!r}; add it to jobpilot.llm.PRICES")
    return PRICES[model]


def _tokens(usage: object, name: str) -> int:
    # the SDK's Usage object and a plain dict (tests, stored rows) both work; a missing or None field is 0
    value = usage.get(name) if isinstance(usage, Mapping) else getattr(usage, name, None)
    return int(value or 0)


def cost_of(usage: object, model: str) -> Decimal:
    """The real cost of one call from its usage (input_tokens, output_tokens, cache_creation_input_tokens,
    cache_read_input_tokens). Opus thinking tokens are billed inside output_tokens, so they are included."""
    p = _prices(model)
    total = (_tokens(usage, "input_tokens") * p["input"]
             + _tokens(usage, "output_tokens") * p["output"]
             + _tokens(usage, "cache_creation_input_tokens") * p["input"] * CACHE_WRITE_MULTIPLIER
             + _tokens(usage, "cache_read_input_tokens") * p["input"] * CACHE_READ_MULTIPLIER)
    return (total / PER_TOKENS).quantize(USD_PLACES, rounding=ROUND_HALF_UP)


def estimate_cost(chars: int, max_tokens: int, model: str) -> Decimal:
    """The worst-case cost before a call: ceil(chars / 2) input tokens (chars = system prompt + messages)
    and the full max_tokens of output. All input is priced at the full input price: we don't use prompt
    caching, and if it is ever switched on this must use CACHE_WRITE_MULTIPLIER instead.
    chars / 2 is a safe margin for ordinary text (Turkish and English), not a hard bound: emoji and some
    other symbols can take more than one token per character, so a real cost can exceed this estimate.
    settle() records the real cost anyway and reports the overrun."""
    p = _prices(model)
    input_tokens = -(-max(chars, 0) // CHARS_PER_TOKEN)  # ceiling division without floats
    if input_tokens > MAX_INPUT_TOKENS.get(model, input_tokens):
        raise UnknownModel(f"no price for a {input_tokens}-token prompt on {model!r}; shorten the input")
    total = input_tokens * p["input"] + max(max_tokens, 0) * p["output"]
    return (total / PER_TOKENS).quantize(USD_PLACES, rounding=ROUND_CEILING)


_ANGLES = str.maketrans({"<": "‹", ">": "›"})
# C0/C1 controls except \t (09) and \n (0a), plus zero-width and bidi-override characters, which can hide
# text from a human reviewer while the model still reads it
_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f​-‏‪-‮⁠-⁩﻿]")
_UNSAFE_LABEL = re.compile(r"[^A-Za-z0-9._:/?=&%#~+-]")  # URL-ish characters; no quotes, spaces or angles
MAX_LABEL = 200


def untrusted(label: str, text: str, max_chars: int | None = None) -> str:
    """Wrap third-party text as <untrusted_page source="label">...</untrusted_page> for a prompt.
    The text keeps its letters (Turkish included) but loses control characters, and its < > become ‹ ›,
    so it can never contain a closing tag. The label is cut down to URL-safe characters. With max_chars,
    the cleaned text is capped and a note says how much was cut, so the model knows it saw only part."""
    safe_label = _UNSAFE_LABEL.sub("_", str(label).translate(_ANGLES))[:MAX_LABEL]
    clean = _CONTROL.sub("", str(text).replace("\r\n", "\n").replace("\r", "\n")).translate(_ANGLES)
    if max_chars is not None and len(clean) > max_chars:
        clean = clean[:max(max_chars, 0)] + f"\n[truncated: {max(max_chars, 0)} of {len(clean)} characters kept]"
    return f'<untrusted_page source="{safe_label}">\n{clean}\n</untrusted_page>'


# --- Budget ledger ---------------------------------------------------------------------------------------
# Reserve, then settle. Before a call, reserve() writes a 'reserved' row at the call's worst-case estimate
# and commits; only then is the API called; afterwards settle() replaces the estimate with the real cost.
# While a row is reserved it counts toward the cap at its estimate. So if the process crashes mid-call and
# never settles, the call still counts: we can't know whether Anthropic billed it, and counting the estimate
# errs on the side of spending less. A crashed call can only make the cap stricter, never looser.

# One fixed advisory-lock key for the whole ledger ("JPLLM" in hex + 1). Every reserve() takes it, so two
# processes can't both read the same spend, both see room, and both go over the cap together.
LEDGER_LOCK = 0x4A504C4C4D000001

# The current UTC month as a range on created_at. A range (not date_trunc on the column) lets Postgres use
# llm_calls_created_idx. AT TIME ZONE 'UTC' makes the bounds the same whatever the session's TimeZone.
MONTH_SPEND_SQL = """
SELECT COALESCE(sum(CASE WHEN status = 'reserved' THEN est_cost_usd ELSE cost_usd END), 0)
FROM llm_calls
WHERE created_at >= date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
  AND created_at < (date_trunc('month', now() AT TIME ZONE 'UTC') + interval '1 month') AT TIME ZONE 'UTC'
"""
INSERT_CALL_SQL = """
INSERT INTO llm_calls (purpose, model, status, est_cost_usd, cost_usd) VALUES (%s, %s, %s, %s, %s) RETURNING id
"""
# cost NULL means "keep the estimate" (a timeout: we can't tell whether it was billed)
SETTLE_SQL = """
UPDATE llm_calls
SET status = %s, cost_usd = COALESCE(%s, est_cost_usd),
    input_tokens = %s, output_tokens = %s, cache_creation_tokens = %s, cache_read_tokens = %s,
    stop_reason = %s, error_type = %s, request_id = %s, finished_at = now()
WHERE id = %s AND status = 'reserved'
RETURNING est_cost_usd, cost_usd
"""


@dataclass(frozen=True)
class Settlement:
    """What settle() did. Truthy when the row was settled. overrun is how far the real cost went over the
    reserved estimate (0 normally): the cap was checked against the estimate, so the caller should log it."""
    settled: bool
    overrun: Decimal = Decimal(0)

    def __bool__(self) -> bool:
        return self.settled


def _idle(conn) -> bool:
    from psycopg.pq import TransactionStatus  # lazy, like jobpilot.db: importing llm needs no driver
    return conn.info.transaction_status == TransactionStatus.IDLE


def _own_transaction(conn):
    # conn.transaction() commits at the end of the block, in autocommit mode or not. But inside an open
    # transaction it would only make a savepoint, and nothing would commit until the caller does.
    if not _idle(conn):
        raise LedgerBusy("the connection has an open transaction; commit or roll back before using the ledger")
    return conn.transaction()


def month_spend(conn) -> Decimal:
    """This UTC month's spend over all models: settled calls at their cost, reserved ones at their estimate,
    refused ones at 0 (their cost is 0). Called on an idle non-autocommit connection, it ends the read
    transaction it opened, so the next reserve() doesn't find the connection busy."""
    was_idle = _idle(conn)
    spent = Decimal(conn.execute(MONTH_SPEND_SQL).fetchone()[0])
    if was_idle:
        conn.rollback()  # only a read: nothing to keep (a no-op in autocommit mode)
    return spent


def reserve(conn, purpose: str, model: str, est: Decimal, cap: Decimal, max_call: Decimal) -> int:
    """Book a call at its estimate before sending it; returns the llm_calls id to settle() afterwards.
    In its own committed transaction, under the ledger lock, so concurrent reservations are checked one at
    a time. If est is over max_call, or this month's spend plus est is over cap, it records a 'refused' row
    (cost 0), commits it and raises BudgetExceeded: the API must not be called. Exactly at the cap is allowed.
    Amounts must be Decimal (a float would bring rounding errors into the cap); est is rounded up to the
    micro-dollar the table stores, and must be positive (a zero or negative estimate would hide spend)."""
    if not all(isinstance(x, Decimal) for x in (est, cap, max_call)):
        raise TypeError("est, cap and max_call must be Decimal, not float or int")
    est = est.quantize(USD_PLACES, rounding=ROUND_CEILING)
    if est <= 0:
        raise ValueError(f"the estimate must be positive, got {est}")
    refused = None
    with _own_transaction(conn):
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (LEDGER_LOCK,))  # held until this transaction ends
        spent = month_spend(conn)  # read after the lock, so it includes whatever the previous holder booked
        if est > max_call:
            refused = f"this call's estimate ${est} is over the ${max_call} per-call limit"
        elif spent + est > cap:
            refused = f"this call's estimate ${est} would take this month's spend over the ${cap} cap"
        status, cost = ("refused", Decimal(0)) if refused else ("reserved", None)
        call_id = conn.execute(INSERT_CALL_SQL, (purpose, model, status, est, cost)).fetchone()[0]
    # raised outside the block, so the refused row is committed first and stays as a record of the refusal
    if refused:
        raise BudgetExceeded(f"LLM call refused: {refused} "
                             f"(spent ${spent} of ${cap} this UTC month, ${max(cap - spent, Decimal(0))} remaining)")
    return call_id


def settle(conn, call_id: int, status: str, cost: Decimal | None, usage: object = None,
           stop_reason: str | None = None, error_type: str | None = None,
           request_id: str | None = None) -> Settlement:
    """Close a reservation: status 'ok' or 'error', the real cost (None keeps the estimate, for a timeout
    that may have been billed), token counts from usage, and the stop reason, error type and request id.
    Prompts and responses are never stored. Commits. Only a row that is still 'reserved' changes, so a second
    settle of the same call is a no-op and returns a falsy Settlement.
    A real cost above the estimate is still recorded (it is what was billed), and returned as the overrun:
    the estimate is a safe margin, not a hard bound (see estimate_cost)."""
    if status not in ("ok", "error"):
        raise ValueError(f"settle status must be 'ok' or 'error', not {status!r}")
    tokens = [None] * 4 if usage is None else [
        _tokens(usage, name) for name in
        ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")]
    with _own_transaction(conn):
        row = conn.execute(SETTLE_SQL, (status, cost, *tokens, stop_reason, error_type, request_id,
                                        call_id)).fetchone()
    if row is None:
        return Settlement(False)
    return Settlement(True, max(row[1] - row[0], Decimal(0)))
