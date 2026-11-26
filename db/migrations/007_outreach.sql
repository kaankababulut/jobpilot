-- Migration 007: outreach (companies, their profiles, email drafts, my decisions, sends and replies).
-- Never edit a migration that has been applied: databases that recorded it won't run it again.
--
-- Why insert-only decisions: approving an email is the one step a human must take. The Telegram bot
-- (jobpilot_feedback) can INSERT a decision and never UPDATE or DELETE one, and draft_id is the primary key,
-- so a double tap, a replayed webhook or a bug can't flip a reject into an approve. Status isn't a column
-- anyone writes: the outreach_status view computes it from which rows exist.
-- Why one send per company: a cold email is welcome once and spam twice. UNIQUE(company_id) on
-- outreach_sends makes a second email to the same company impossible, even from a new draft, and the
-- composite FKs stop a send that mixes up drafts, companies or addresses, or that was never approved.
-- A send row is claimed before SMTP; sent_at NULL means "sending" and is never retried automatically,
-- because a crash after SMTP accepted the mail would otherwise send it twice.
-- KVKK: we only store role addresses (info@, ik@, kariyer@, ...) that the company publishes on its own site,
-- and contact_email_url keeps the page we found it on as evidence. A person's address is personal data we
-- have no basis to collect, so the role_email domain rejects anything outside the allowlist.
-- Data minimisation for replies: outreach_replies keeps only the kind of reply and when it came, never the
-- body, sender name or address. That is all the tracker needs, and what we don't store can't leak.
-- Sends and replies don't cascade: the record that I emailed a company must survive, so a company or draft
-- that was mailed can't be deleted. Opting out is the opted_out flag, not a delete.

-- one definition of "an address we may store or mail", used by every email column below.
-- A domain, not a copied CHECK, so the allowlist can't drift between tables.
-- The host part is lowercase letters, digits and hyphens in at least two dot-separated labels (punycode
-- xn-- hosts pass). That also shuts out spaces and CR/LF, so an address can't smuggle extra mail headers.
CREATE DOMAIN role_email AS TEXT CHECK (
    VALUE ~ '^(info|ik|kariyer|careers|career|hr|jobs|insankaynaklari)@[a-z0-9-]+(\.[a-z0-9-]+)+$'
);

-- name_norm is the dedupe key (case and legal suffixes stripped by the code); domain is a bare lowercase
-- host, never a URL, so "acme.com.tr" and "https://Acme.com.tr/" can't become two companies
CREATE TABLE companies (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name           TEXT NOT NULL,
    name_norm      TEXT NOT NULL UNIQUE,
    domain         TEXT UNIQUE CHECK (domain <> '' AND domain = lower(domain) AND domain !~ '[/:@[:space:]]'),
    -- where the domain came from; the LLM never guesses it
    domain_source  TEXT CHECK (domain_source IN ('apply_url', 'owner', 'manual')),
    opted_out      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((domain IS NULL) = (domain_source IS NULL))  -- every domain says where it came from
);

CREATE TABLE company_jobs (
    company_id  BIGINT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    job_id      BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    PRIMARY KEY (company_id, job_id)
);
CREATE INDEX company_jobs_job_idx ON company_jobs (job_id);

-- what the enrich step learned from the company's own site; a new row per fetch, so old profiles stay
-- comparable. llm_call_id ties it to the ledger (no cascade: the ledger is never deleted)
CREATE TABLE company_profiles (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id         BIGINT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    profile            JSONB NOT NULL,
    schema_version     SMALLINT NOT NULL,
    model              TEXT,
    llm_call_id        BIGINT REFERENCES llm_calls(id),
    source_urls        TEXT[] NOT NULL DEFAULT '{}',
    contact_email      role_email,
    contact_email_url  TEXT,  -- the page the address was published on (KVKK evidence)
    fetched_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (contact_email IS NULL OR contact_email_url IS NOT NULL)
);
CREATE INDEX company_profiles_company_idx ON company_profiles (company_id);

-- honesty_ok defaults to FALSE, so a draft whose honesty check never ran shows as blocked, not pending.
-- UNIQUE (id, company_id, to_email) exists only as the target of outreach_sends' composite FK.
CREATE TABLE outreach_drafts (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id      BIGINT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    profile_id      BIGINT REFERENCES company_profiles(id),
    to_email        role_email NOT NULL,
    language        TEXT NOT NULL DEFAULT 'tr' CHECK (language IN ('tr', 'en')),
    subject         TEXT NOT NULL,
    body            TEXT NOT NULL,
    projects        TEXT[] NOT NULL DEFAULT '{}',
    honesty_ok      BOOLEAN NOT NULL DEFAULT FALSE,
    honesty_issues  JSONB NOT NULL DEFAULT '[]',
    model           TEXT,
    llm_call_id     BIGINT REFERENCES llm_calls(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id, company_id, to_email)
);
CREATE INDEX outreach_drafts_company_idx ON outreach_drafts (company_id);

-- one decision per draft, ever (PK). The final text is what gets sent, so an approve must carry it and a
-- reject must not (a rejected text lying around could be sent by mistake)
CREATE TABLE outreach_decisions (
    draft_id       BIGINT PRIMARY KEY REFERENCES outreach_drafts(id) ON DELETE CASCADE,
    decision       TEXT NOT NULL CHECK (decision IN ('approve', 'reject')),
    final_subject  TEXT,
    final_body     TEXT,
    edited         BOOLEAN NOT NULL DEFAULT FALSE,
    via            TEXT NOT NULL CHECK (via IN ('telegram', 'cli')),
    decided_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- COALESCE: a CHECK that evaluates to NULL passes, so a bare btrim(NULL) <> '' would let NULL through
    CHECK (decision = 'reject' OR (btrim(COALESCE(final_subject, '')) <> '' AND btrim(COALESCE(final_body, '')) <> '')),
    CHECK (decision = 'approve' OR (final_subject IS NULL AND final_body IS NULL)),
    UNIQUE (draft_id, decision)  -- only as the target of outreach_sends' "approved" FK
);

-- no ON DELETE CASCADE anywhere here (see the header). Two composite FKs make a bad send impossible
-- rather than merely unlikely: (draft_id, company_id, to_email) must be the draft's own company and address,
-- and (draft_id, decision) must point at an 'approve' row, so an undecided or rejected draft can't be sent.
CREATE TABLE outreach_sends (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    draft_id    BIGINT NOT NULL UNIQUE,
    company_id  BIGINT NOT NULL UNIQUE,  -- one email per company, ever
    to_email    role_email NOT NULL,
    decision    TEXT NOT NULL DEFAULT 'approve' CHECK (decision = 'approve'),
    message_id  TEXT UNIQUE,             -- our Message-ID header, to match replies
    claimed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at     TIMESTAMPTZ,             -- NULL: claimed, SMTP not confirmed ("sending")
    error       TEXT,
    FOREIGN KEY (draft_id, company_id, to_email) REFERENCES outreach_drafts(id, company_id, to_email),
    FOREIGN KEY (draft_id, decision) REFERENCES outreach_decisions(draft_id, decision),
    CHECK (sent_at IS NULL OR sent_at >= claimed_at)
);

-- message_id is the reply's own Message-ID, so reading the inbox twice records it once
CREATE TABLE outreach_replies (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    send_id      BIGINT NOT NULL REFERENCES outreach_sends(id),
    message_id   TEXT NOT NULL UNIQUE,
    kind         TEXT NOT NULL CHECK (kind IN ('replied', 'interview', 'rejected')),
    received_at  TIMESTAMPTZ NOT NULL,
    notified_at  TIMESTAMPTZ
);
CREATE INDEX outreach_replies_send_idx ON outreach_replies (send_id);

-- What happened beats what was planned: replies (the latest wins, so an interview followed by a rejection
-- is a rejection) > sent > sending. Then every reason not to send beats an approve: the company was already
-- mailed via another draft, it opted out, I rejected the draft, or it failed the honesty check. The sender
-- mails ONLY drafts whose status is 'approved', so an approve given before an opt-out can't slip through.
-- The view runs with its owner's rights on purpose (never security_invoker): the bot reads statuses
-- through it without any access to sends or replies.
CREATE VIEW outreach_status AS
SELECT d.id AS draft_id, d.company_id, c.name AS company, d.to_email, d.created_at,
       CASE
         WHEN r.kind = 'rejected'                  THEN 'rejected_by_company'
         WHEN r.kind IS NOT NULL                   THEN r.kind  -- 'interview' or 'replied'
         WHEN s.sent_at IS NOT NULL                THEN 'sent'
         WHEN s.id IS NOT NULL                     THEN 'sending'
         WHEN s.id IS NULL AND cs.id IS NOT NULL   THEN 'already_contacted'
         WHEN c.opted_out                          THEN 'opted_out'
         WHEN dec.decision = 'reject'              THEN 'rejected'
         WHEN NOT d.honesty_ok                     THEN 'blocked'
         WHEN dec.decision = 'approve'             THEN 'approved'
         ELSE 'pending'
       END AS status
FROM outreach_drafts d
JOIN companies c ON c.id = d.company_id
LEFT JOIN outreach_decisions dec ON dec.draft_id = d.id
LEFT JOIN outreach_sends s ON s.draft_id = d.id
LEFT JOIN outreach_sends cs ON cs.company_id = d.company_id  -- at most one row: UNIQUE(company_id)
LEFT JOIN LATERAL (SELECT kind FROM outreach_replies WHERE send_id = s.id
                   ORDER BY received_at DESC, id DESC LIMIT 1) r ON TRUE;

-- the API reads everything
GRANT SELECT ON companies, company_jobs, company_profiles, outreach_drafts, outreach_decisions,
                outreach_sends, outreach_replies, outreach_status TO jobpilot_api;
-- the bot shows drafts and records my decision, and nothing more: no profiles, sends, replies or llm_calls
GRANT SELECT ON companies, outreach_drafts, outreach_decisions, outreach_status TO jobpilot_feedback;
GRANT INSERT ON outreach_decisions TO jobpilot_feedback;
