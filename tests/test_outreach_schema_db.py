"""Integration tests for migration 007 (outreach tables, outreach_status view, role_email domain, grants).
Needs the local Postgres (pytest -m db). The pg fixture has already applied every migration to its
throwaway schema. Test data uses fictional companies on example.com / .test domains only."""
import pytest
from psycopg import errors

from test_db import one
from test_feedback_schema_db import FEEDBACK_ROLE, add_job, as_role
from test_llm_schema_db import add_call
from test_migrate_db import recorded, table_exists

pytestmark = pytest.mark.db

ALLOWED = ("info", "ik", "kariyer", "careers", "career", "hr", "jobs", "insankaynaklari")
TABLES = ("companies", "company_jobs", "company_profiles", "outreach_drafts", "outreach_decisions",
          "outreach_sends", "outreach_replies")
OBJECTS = TABLES + ("outreach_status",)


def add_company(conn, name: str = "Acme", domain: str | None = "acme.example.com", opted_out: bool = False) -> int:
    return one(conn, "INSERT INTO companies (name, name_norm, domain, domain_source, opted_out) "
                     "VALUES (%s, lower(%s), %s, CASE WHEN %s::text IS NULL THEN NULL ELSE 'owner' END, %s) RETURNING id",
               (name, name, domain, domain, opted_out))[0]


def add_profile(conn, company: int, email: str | None = "info@acme.example.com",
                url: str | None = "https://acme.example.com/iletisim") -> int:
    return one(conn, "INSERT INTO company_profiles (company_id, profile, schema_version, contact_email, contact_email_url) "
                     "VALUES (%s, '{}', 1, %s, %s) RETURNING id", (company, email, url))[0]


def add_draft(conn, company: int, honesty_ok: bool = True, to_email: str = "info@acme.example.com") -> int:
    return one(conn, "INSERT INTO outreach_drafts (company_id, to_email, subject, body, honesty_ok) "
                     "VALUES (%s, %s, 'Staj başvurusu', 'Merhaba', %s) RETURNING id", (company, to_email, honesty_ok))[0]


def decide(conn, draft: int, decision: str = "approve", subject: str | None = "S", body: str | None = "B") -> None:
    conn.execute("INSERT INTO outreach_decisions (draft_id, decision, final_subject, final_body, via) "
                 "VALUES (%s, %s, %s, %s, 'telegram')", (draft, decision, subject, body))


def add_send(conn, draft: int, company: int, sent: bool = True, message_id: str | None = None,
             approve: bool = True, to_email: str = "info@acme.example.com") -> int:
    if approve:  # a send needs an approve row; keeps one that a test already made
        conn.execute("INSERT INTO outreach_decisions (draft_id, decision, final_subject, final_body, via) "
                     "VALUES (%s, 'approve', 'S', 'B', 'cli') ON CONFLICT DO NOTHING", (draft,))
    return one(conn, "INSERT INTO outreach_sends (draft_id, company_id, to_email, message_id, sent_at) "
                     "VALUES (%s, %s, %s, %s, CASE WHEN %s THEN now() END) RETURNING id",
               (draft, company, to_email, message_id, sent))[0]


def add_reply(conn, send: int, kind: str, message_id: str, received_at: str) -> None:
    conn.execute("INSERT INTO outreach_replies (send_id, message_id, kind, received_at) VALUES (%s, %s, %s, %s)",
                 (send, message_id, kind, received_at))


def status(conn, draft: int) -> str:
    return one(conn, "SELECT status FROM outreach_status WHERE draft_id = %s", (draft,))[0]


def count(conn, table: str) -> int:
    return one(conn, f"SELECT count(*) FROM {table}")[0]


def test_007_creates_every_object(pg):
    for name in OBJECTS:
        assert table_exists(pg, name), name
    assert ("007", "outreach") in recorded(pg)


# --- the role_email rule (KVKK) ---

@pytest.mark.parametrize("local", ALLOWED)
def test_every_allowlisted_role_address_is_accepted(pg, local):
    company = add_company(pg)
    add_profile(pg, company, email=f"{local}@acme.example.com")
    add_draft(pg, company, to_email=f"{local}@acme.test")


@pytest.mark.parametrize("email", ["ahmet.yilmaz@acme.example.com", "Info@acme.example.com", "info",
                                   "info@", "info@a@acme.test", "ikk@acme.test", "noreply@acme.test",
                                   "info@acme.test\r\nBcc: victim@other.test", "info@acme com", "info@.",
                                   "info@acme", "info@Acme.test", "info@acme.test "])
@pytest.mark.parametrize("where", ["profile", "draft", "send"])
def test_personal_or_malformed_addresses_are_rejected(pg, email, where):
    company = add_company(pg)
    with pytest.raises(errors.CheckViolation):
        if where == "profile":
            add_profile(pg, company, email=email)
        elif where == "draft":
            add_draft(pg, company, to_email=email)
        else:
            draft = add_draft(pg, company)
            pg.execute("INSERT INTO outreach_sends (draft_id, company_id, to_email) VALUES (%s, %s, %s)",
                       (draft, company, email))


def test_punycode_host_is_accepted(pg):
    add_profile(pg, add_company(pg), email="info@xn--kariyer-q9a.test")


def test_contact_email_needs_its_source_url(pg):
    company = add_company(pg)
    with pytest.raises(errors.CheckViolation):
        add_profile(pg, company, url=None)
    add_profile(pg, company, email=None, url=None)  # no address found is fine


@pytest.mark.parametrize("domain", ["Acme.example.com", "https://acme.example.com", "acme.example.com/careers",
                                    "info@acme.example.com", "acme example.com", ""])
def test_company_domain_is_a_bare_lowercase_host(pg, domain):
    with pytest.raises(errors.CheckViolation):
        add_company(pg, domain=domain)


def test_company_domain_and_name_norm_are_unique(pg):
    add_company(pg, "Acme")
    with pytest.raises(errors.UniqueViolation):
        add_company(pg, "ACME", domain="other.example.com")  # same name_norm
    with pytest.raises(errors.UniqueViolation):
        add_company(pg, "Beta")  # same domain
    add_company(pg, "Gamma", domain=None)
    add_company(pg, "Delta", domain=None)  # many companies can have no domain yet


@pytest.mark.parametrize("domain,source", [("acme.example.com", "llm"),  # the LLM never supplies a domain
                                           ("acme.example.com", None), (None, "owner")])
def test_domain_and_its_source_come_together(pg, domain, source):
    with pytest.raises(errors.CheckViolation):
        pg.execute("INSERT INTO companies (name, name_norm, domain, domain_source) VALUES ('Acme', 'acme', %s, %s)",
                   (domain, source))


# --- decisions ---

@pytest.mark.parametrize("subject,body", [(None, None), ("S", None), (None, "B"), ("  ", "B"), ("S", "")])
def test_approve_needs_both_final_texts(pg, subject, body):
    draft = add_draft(pg, add_company(pg))
    with pytest.raises(errors.CheckViolation):
        decide(pg, draft, "approve", subject, body)


@pytest.mark.parametrize("subject,body", [("S", "B"), ("S", None), (None, "B")])
def test_reject_carries_no_final_text(pg, subject, body):
    draft = add_draft(pg, add_company(pg))
    with pytest.raises(errors.CheckViolation):
        decide(pg, draft, "reject", subject, body)


def test_second_decision_for_a_draft_is_rejected(pg):
    draft = add_draft(pg, add_company(pg))
    decide(pg, draft, "reject", None, None)
    with pytest.raises(errors.UniqueViolation):
        decide(pg, draft, "approve")  # a reject can't be flipped by a second tap


def test_decision_via_check(pg):
    draft = add_draft(pg, add_company(pg))
    with pytest.raises(errors.CheckViolation):
        pg.execute("INSERT INTO outreach_decisions (draft_id, decision, via) VALUES (%s, 'reject', 'email')", (draft,))


# --- sends and replies ---

def test_second_send_to_the_same_company_is_rejected_even_for_another_draft(pg):
    company = add_company(pg)
    first, second = add_draft(pg, company), add_draft(pg, company)
    add_send(pg, first, company)
    with pytest.raises(errors.UniqueViolation):
        add_send(pg, second, company)


def test_a_draft_is_sent_only_once(pg):
    a, b = add_company(pg, "A", "a.example.com"), add_company(pg, "B", "b.example.com")
    draft = add_draft(pg, a)
    add_send(pg, draft, a)
    # the composite FK rejects this before UNIQUE(draft_id) is checked (the draft isn't b's);
    # UNIQUE(draft_id) is the backstop if that FK ever changes
    with pytest.raises((errors.UniqueViolation, errors.ForeignKeyViolation)):
        add_send(pg, draft, b)


def test_send_must_match_the_drafts_company(pg):
    a, b = add_company(pg, "A", "a.example.com"), add_company(pg, "B", "b.example.com")
    draft = add_draft(pg, a)
    with pytest.raises(errors.ForeignKeyViolation):
        add_send(pg, draft, b)


def test_send_must_use_the_drafts_address(pg):
    company = add_company(pg)
    draft = add_draft(pg, company)
    with pytest.raises(errors.ForeignKeyViolation):
        add_send(pg, draft, company, to_email="kariyer@acme.example.com")


def test_undecided_draft_cannot_be_sent(pg):
    company = add_company(pg)
    with pytest.raises(errors.ForeignKeyViolation):
        add_send(pg, add_draft(pg, company), company, approve=False)


def test_rejected_draft_cannot_be_sent(pg):
    company = add_company(pg)
    draft = add_draft(pg, company)
    decide(pg, draft, "reject", None, None)
    with pytest.raises(errors.ForeignKeyViolation):
        add_send(pg, draft, company)
    with pytest.raises(errors.CheckViolation):  # nor by declaring the send itself a reject
        pg.execute("INSERT INTO outreach_sends (draft_id, company_id, to_email, decision) "
                   "VALUES (%s, %s, 'info@acme.example.com', 'reject')", (draft, company))


def test_sent_at_cannot_precede_the_claim(pg):
    company = add_company(pg)
    draft = add_draft(pg, company)
    decide(pg, draft)
    with pytest.raises(errors.CheckViolation):
        pg.execute("INSERT INTO outreach_sends (draft_id, company_id, to_email, claimed_at, sent_at) "
                   "VALUES (%s, %s, 'info@acme.example.com', now(), now() - interval '1 second')", (draft, company))


def test_send_and_reply_message_ids_are_unique(pg):
    a, b = add_company(pg, "A", "a.example.com"), add_company(pg, "B", "b.example.com")
    send = add_send(pg, add_draft(pg, a), a, message_id="<1@jobpilot.test>")
    with pytest.raises(errors.UniqueViolation):
        add_send(pg, add_draft(pg, b), b, message_id="<1@jobpilot.test>")
    add_reply(pg, send, "replied", "<r1@acme.test>", "2026-11-20 10:00+00")
    with pytest.raises(errors.UniqueViolation):  # reading the inbox twice records the reply once
        add_reply(pg, send, "replied", "<r1@acme.test>", "2026-11-20 10:00+00")


def test_reply_kind_check(pg):
    company = add_company(pg)
    send = add_send(pg, add_draft(pg, company), company)
    with pytest.raises(errors.CheckViolation):
        add_reply(pg, send, "offer", "<r1@acme.test>", "2026-11-20 10:00+00")


# --- cascades ---

def test_deleting_an_unsent_company_cascades(pg):
    company = add_company(pg)
    pg.execute("INSERT INTO company_jobs (company_id, job_id) VALUES (%s, %s)", (company, add_job(pg, "1")))
    profile = add_profile(pg, company)
    draft = add_draft(pg, company)
    pg.execute("UPDATE outreach_drafts SET profile_id = %s WHERE id = %s", (profile, draft))
    decide(pg, draft)
    pg.execute("DELETE FROM companies WHERE id = %s", (company,))
    for t in ("company_jobs", "company_profiles", "outreach_drafts", "outreach_decisions"):
        assert count(pg, t) == 0, t
    assert count(pg, "jobs") == 1  # the job itself stays


def test_deleting_a_job_drops_only_the_link(pg):
    company, job = add_company(pg), add_job(pg, "1")
    pg.execute("INSERT INTO company_jobs (company_id, job_id) VALUES (%s, %s)", (company, job))
    pg.execute("DELETE FROM jobs WHERE id = %s", (job,))
    assert count(pg, "company_jobs") == 0 and count(pg, "companies") == 1


@pytest.mark.parametrize("table", ["companies", "outreach_drafts", "outreach_decisions"])
def test_a_mailed_company_draft_or_approval_cannot_be_deleted(pg, table):
    company = add_company(pg)
    draft = add_draft(pg, company)
    add_send(pg, draft, company)
    with pytest.raises(errors.ForeignKeyViolation):
        pg.execute(f"DELETE FROM {table}")


def test_a_send_with_replies_cannot_be_deleted(pg):
    company = add_company(pg)
    send = add_send(pg, add_draft(pg, company), company)
    add_reply(pg, send, "replied", "<r1@acme.test>", "2026-11-20 10:00+00")
    with pytest.raises(errors.ForeignKeyViolation):
        pg.execute("DELETE FROM outreach_sends")


def test_a_used_llm_call_cannot_be_deleted(pg):
    call = add_call(pg)
    company = add_company(pg)
    pg.execute("INSERT INTO company_profiles (company_id, profile, schema_version, llm_call_id) "
               "VALUES (%s, '{}', 1, %s)", (company, call))
    with pytest.raises(errors.ForeignKeyViolation):
        pg.execute("DELETE FROM llm_calls")


# --- the outreach_status view ---

def test_outreach_status_gives_every_status(pg):
    def draft_for(name: str, **kw) -> tuple[int, int]:
        company = add_company(pg, name, f"{name.lower()}.example.com", opted_out=kw.pop("opted_out", False))
        return add_draft(pg, company, **kw), company

    pending, _ = draft_for("Pending")
    blocked, _ = draft_for("Blocked", honesty_ok=False)
    opted_out, _ = draft_for("Optout", opted_out=True)
    approved, _ = draft_for("Approved")
    decide(pg, approved)
    rejected, _ = draft_for("Rejected")
    decide(pg, rejected, "reject", None, None)
    expected = {pending: "pending", blocked: "blocked", opted_out: "opted_out",
                approved: "approved", rejected: "rejected"}
    contacted, company = draft_for("Contacted")
    add_send(pg, add_draft(pg, company), company)  # another draft for the same company went out
    expected[contacted] = "already_contacted"
    for name, kinds in [("Sending", None), ("Sent", []), ("Replied", ["replied"]),
                        ("Interview", ["replied", "interview"]), ("Turned", ["interview", "rejected"]),
                        ("Back", ["rejected", "interview"])]:
        draft, company = draft_for(name)
        decide(pg, draft)
        send = add_send(pg, draft, company, sent=kinds is not None)
        for day, kind in enumerate(kinds or [], start=1):
            add_reply(pg, send, kind, f"<{name}{day}@acme.test>", f"2026-11-{day:02d} 10:00+00")
        expected[draft] = {"Sending": "sending", "Sent": "sent", "Replied": "replied", "Interview": "interview",
                           "Turned": "rejected_by_company", "Back": "interview"}[name]  # the latest reply wins
    got = dict(pg.execute("SELECT draft_id, status FROM outreach_status").fetchall())
    got = {d: st for d, st in got.items() if d in expected}  # leave out the Contacted company's sent draft
    assert got == expected
    assert set(got.values()) == {"pending", "blocked", "opted_out", "approved", "rejected", "sending", "sent",
                                 "replied", "interview", "rejected_by_company", "already_contacted"}


def test_opt_out_after_approval_wins(pg):
    company = add_company(pg)
    draft = add_draft(pg, company)
    decide(pg, draft)
    pg.execute("UPDATE companies SET opted_out = true WHERE id = %s", (company,))
    assert status(pg, draft) == "opted_out"  # the sender mails only 'approved', so this one is held back


def test_failed_honesty_check_beats_an_approval(pg):
    draft = add_draft(pg, add_company(pg), honesty_ok=False)
    decide(pg, draft)
    assert status(pg, draft) == "blocked"


def test_second_draft_for_a_mailed_company_is_already_contacted(pg):
    company = add_company(pg)
    sent, other = add_draft(pg, company), add_draft(pg, company)
    add_send(pg, sent, company)
    decide(pg, other)  # even approved, it must never go out
    assert (status(pg, sent), status(pg, other)) == ("sent", "already_contacted")


def test_unchecked_draft_is_blocked_not_pending(pg):
    company = add_company(pg)
    draft = one(pg, "INSERT INTO outreach_drafts (company_id, to_email, subject, body) "
                    "VALUES (%s, 'info@acme.example.com', 'S', 'B') RETURNING id", (company,))[0]
    assert status(pg, draft) == "blocked"


def test_status_view_columns(pg):
    company = add_company(pg, "Acme")
    draft = add_draft(pg, company)
    row = one(pg, "SELECT draft_id, company_id, company, to_email, created_at IS NOT NULL, status "
                  "FROM outreach_status")
    assert row == (draft, company, "Acme", "info@acme.example.com", True, "pending")


# --- grants ---

def test_feedback_role_inserts_a_decision_but_cannot_change_it(pg):
    draft = add_draft(pg, add_company(pg))
    with as_role(pg, FEEDBACK_ROLE):
        assert status(pg, draft) == "pending"
        decide(pg, draft)
        assert status(pg, draft) == "approved"
        for sql in ("UPDATE outreach_decisions SET decision = 'reject', final_subject = NULL, final_body = NULL",
                    "DELETE FROM outreach_decisions"):
            with pytest.raises(errors.InsufficientPrivilege):
                pg.execute(sql)
    assert one(pg, "SELECT decision FROM outreach_decisions")[0] == "approve"


@pytest.mark.parametrize("sql", [
    "INSERT INTO outreach_drafts (company_id, to_email, subject, body) VALUES ({c}, 'info@acme.test', 'S', 'B')",
    "UPDATE outreach_drafts SET body = 'x'",
    "INSERT INTO outreach_sends (draft_id, company_id, to_email) VALUES ({d}, {c}, 'info@acme.test')",
    "UPDATE outreach_sends SET sent_at = now()",
    "SELECT * FROM outreach_sends",
    "SELECT * FROM outreach_replies",
    "INSERT INTO company_profiles (company_id, profile, schema_version) VALUES ({c}, '{{}}', 1)",
    "UPDATE company_profiles SET profile = '{{}}'",
    "SELECT * FROM company_profiles",
    "INSERT INTO companies (name, name_norm) VALUES ('X', 'x')",
    "UPDATE companies SET opted_out = false",
    "INSERT INTO llm_calls (purpose, model, est_cost_usd) VALUES ('smoke', 'm', 0)",
    "UPDATE llm_calls SET cost_usd = 0",
])
def test_feedback_role_cannot_touch_the_rest(pg, sql):
    company = add_company(pg)
    draft = add_draft(pg, company)
    add_send(pg, draft, company)
    with as_role(pg, FEEDBACK_ROLE), pytest.raises(errors.InsufficientPrivilege):
        pg.execute(sql.format(c=company, d=draft))


FEEDBACK_PRIVILEGES = {"companies": {"SELECT"}, "outreach_drafts": {"SELECT"}, "outreach_status": {"SELECT"},
                       "outreach_decisions": {"SELECT", "INSERT"}, "company_profiles": set(), "company_jobs": set(),
                       "outreach_sends": set(), "outreach_replies": set(), "llm_calls": set()}


@pytest.mark.parametrize("name", FEEDBACK_PRIVILEGES)
def test_feedback_role_has_exactly_its_privileges(pg, name):
    held = {p for p in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
            if one(pg, "SELECT has_table_privilege(%s, quote_ident(current_schema()) || '.' || %s, %s)",
                   (FEEDBACK_ROLE, name, p))[0]}
    assert held == FEEDBACK_PRIVILEGES[name]


@pytest.mark.parametrize("name", OBJECTS)
def test_api_role_is_select_only(pg, name):
    privileges = {p: one(pg, "SELECT has_table_privilege('jobpilot_api', quote_ident(current_schema()) || '.' || %s, %s)",
                         (name, p))[0] for p in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")}
    assert privileges == {"SELECT": True, "INSERT": False, "UPDATE": False, "DELETE": False, "TRUNCATE": False}
    with as_role(pg, "jobpilot_api"):
        pg.execute(f"SELECT * FROM {name}").fetchall()


def test_api_role_reads_the_status_view(pg):
    draft = add_draft(pg, add_company(pg))
    with as_role(pg, "jobpilot_api"):
        assert status(pg, draft) == "pending"
        with pytest.raises(errors.InsufficientPrivilege):
            decide(pg, draft)
