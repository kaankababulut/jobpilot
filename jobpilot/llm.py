"""Claude calls for outreach: prices, cost accounting and the wrapper for untrusted text.
This file is the pure part (no DB, no network, no anthropic import); the budget ledger and the API call
come in later steps. Every cost is a Decimal, because floats can't hold 0.1 exactly and a spend cap
that drifts by rounding errors is not a cap.

Page text is data, never instructions. Anything fetched from a company website goes through untrusted(),
which wraps it in <untrusted_page> and neutralises < and >, so the page can't close the tag and pose as
our own prompt. The system prompt tells the model that whatever is inside the tag is only material to read."""
import re
from collections.abc import Mapping
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal


class UnknownModel(ValueError):
    """A model with no known price. Fail closed: we can't check the budget for a call we can't price."""


class BudgetExceeded(Exception):
    """The call would go over the monthly cap or max_call_usd, so it was refused before reaching the API."""


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
    caching, and if it is ever switched on this must use CACHE_WRITE_MULTIPLIER instead."""
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
