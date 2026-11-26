-- Migration 006: a ledger of every Claude API call (outreach, roadmap step 8), and the monthly spend view.
-- Never edit a migration that has been applied: databases that recorded it won't run it again.
--
-- Why a ledger: the owner caps LLM spend at $4.50 a month. The cap is checked against this table before
-- each call, so it has to hold every call, including ones in flight. A call is first written as 'reserved'
-- at its worst-case estimate, then settled to 'ok' (real cost), 'error' (0, or the estimate if we can't
-- tell whether it was billed) or 'refused' (over the cap: never sent, cost 0).
-- The CHECKs tie cost to status: no cost while reserved, a cost (never negative) once settled, 0 when refused,
-- so the spend sum can't silently undercount or double-count.
-- NUMERIC, not float: money is added up here, and floats drift on sums of cents.
-- No prompts or responses are stored (they hold CV and company text): only tokens, cost and ids.
-- Months are UTC (AT TIME ZONE 'UTC'), so the bucket a call lands in doesn't depend on the session's
-- TimeZone setting (Türkiye is UTC+3); otherwise the cap could reset at a different moment per connection.

CREATE TABLE llm_calls (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    purpose       TEXT NOT NULL CHECK (purpose IN ('enrich','draft','honesty','smoke')),
    model         TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'reserved' CHECK (status IN ('reserved','ok','error','refused')),
    est_cost_usd  NUMERIC(10,6) NOT NULL,
    cost_usd      NUMERIC(10,6),  -- NULL exactly while reserved, set once settled
    input_tokens INT, output_tokens INT, cache_creation_tokens INT, cache_read_tokens INT,
    stop_reason TEXT, error_type TEXT, request_id TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    CHECK ((status = 'reserved') = (cost_usd IS NULL)),
    CHECK (est_cost_usd >= 0 AND (cost_usd IS NULL OR cost_usd >= 0)),  -- a negative cost would hide spend
    CHECK (status <> 'refused' OR cost_usd = 0)  -- refused calls were never sent
);
CREATE INDEX llm_calls_created_idx ON llm_calls (created_at);

-- reserved rows count at their estimate (they may be billed), refused rows have cost 0 and aren't calls
CREATE VIEW llm_spend_monthly AS
SELECT date_trunc('month', created_at AT TIME ZONE 'UTC')::date AS month, model,
       count(*) FILTER (WHERE status <> 'refused') AS calls,
       count(*) FILTER (WHERE status = 'error')    AS errors,
       sum(COALESCE(cost_usd, est_cost_usd))        AS spend_usd,
       sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens
FROM llm_calls GROUP BY 1, 2;

-- the API can report spend; the PC's owner connection writes the ledger.
-- jobpilot_feedback gets nothing: the bot never calls Claude.
GRANT SELECT ON llm_calls, llm_spend_monthly TO jobpilot_api;
