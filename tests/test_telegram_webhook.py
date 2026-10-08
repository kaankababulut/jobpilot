"""Tests for POST /telegram/webhook (jobpilot.telegram_webhook). No database or network: the feedback
functions and the write connection are faked, and TestClient runs the app in-process.
The DB test at the end sends one real button tap through to a throwaway schema (pytest -m db)."""
import logging
from contextlib import contextmanager

import psycopg
import pytest
from fastapi.testclient import TestClient

from jobpilot import api
from jobpilot import feedback as fb
from jobpilot import telegram as tg
from jobpilot import telegram_webhook as wh

SECRET = "hook-s3cret-XYZ"
OWNER = 987654321
URL = "postgresql://jobpilot_feedback:fbPassw0rd@db.example:5432/jobs"
PATH = "/telegram/webhook"
HEADERS = {wh.SECRET_HEADER: SECRET}


@pytest.fixture(autouse=True)
def env(monkeypatch, request):
    # importing jobpilot.api loads the real .env; replace its values with the test ones
    for name in ("TELEGRAM_WEBHOOK_SECRET", "TELEGRAM_OWNER_ID", "FEEDBACK_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    # db tests keep it: the pg fixture needs the URL, and in CI there is no .env to reload it from
    if request.node.get_closest_marker("db") is None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("TELEGRAM_OWNER_ID", str(OWNER))


@pytest.fixture
def client():
    return TestClient(api.create_app())


@pytest.fixture
def no_connect(monkeypatch):
    # the URL is set, so a request that got as far as connecting would really try: the guard proves it doesn't
    monkeypatch.setenv("FEEDBACK_DATABASE_URL", URL)

    def fail(*args, **kwargs):
        pytest.fail("the webhook must not connect here")
    monkeypatch.setattr(wh, "connect", fail)


class FakeConn:
    pass


@pytest.fixture
def conns(monkeypatch):
    """Replaces the write connection; the list records each connection handed out."""
    opened: list[FakeConn] = []

    @contextmanager
    def fake():
        opened.append(FakeConn())
        yield opened[-1]
    monkeypatch.setattr(wh, "get_write_conn", fake)
    return opened


def fake(monkeypatch, name, result):
    """Replaces one feedback function; returns the list of argument tuples it was called with."""
    calls = []

    def f(conn, *args, **kwargs):
        assert isinstance(conn, FakeConn)
        calls.append((*args, *kwargs.values()))
        return result
    monkeypatch.setattr(fb, name, f)
    return calls


def tap(data, sender=OWNER) -> dict:
    return {"update_id": 1, "callback_query": {"id": "cb7", "from": {"id": sender}, "data": data}}


def msg(text, sender=OWNER) -> dict:
    return {"update_id": 1, "message": {"message_id": 3, "from": {"id": sender}, "chat": {"id": sender}, "text": text}}


def post(client, update, headers=HEADERS):
    return client.post(PATH, json=update, headers=headers)


# --- the checks before any database work ---

@pytest.mark.parametrize("value", [None, "", "   "])
def test_secret_unset_is_503(client, monkeypatch, no_connect, value):
    if value is None:
        monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET")
    else:
        monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", value)
    r = post(client, tap("u:1"))
    assert (r.status_code, r.json()) == (503, {"detail": "webhook not configured"})


@pytest.mark.parametrize("headers", [{}, {wh.SECRET_HEADER: ""}, {wh.SECRET_HEADER: "wrong"},
                                     {wh.SECRET_HEADER: SECRET + "x"}, {"X-API-Key": SECRET}])
def test_missing_or_wrong_secret_is_401(client, no_connect, headers):
    r = post(client, tap("u:1"), headers=headers)
    assert r.status_code == 401 and r.content == b""


@pytest.mark.parametrize("value", [None, "", "abc", "-5", "0", "1_000", "١٢٣", "12.5"])
def test_owner_id_unset_or_invalid_is_503(client, monkeypatch, no_connect, value):
    if value is None:
        monkeypatch.delenv("TELEGRAM_OWNER_ID")
    else:
        monkeypatch.setenv("TELEGRAM_OWNER_ID", value)
    r = post(client, tap("u:1"))
    assert (r.status_code, r.json()) == (503, {"detail": "webhook not configured"})


# ids, because pytest puts the test id in an environment variable, and 100000 brackets overflow it on Windows
@pytest.mark.parametrize("body", [b"{not json", b"\xff\xfe", b"", b"[" * 100000],
                         ids=["broken", "bad-utf8", "empty", "deep-nesting"])
def test_invalid_json_is_400(client, no_connect, body):
    r = client.post(PATH, content=body, headers={**HEADERS, "Content-Type": "application/json"})
    assert (r.status_code, r.json()) == (400, {"detail": "invalid JSON"})


@pytest.mark.parametrize("update", [tap("u:1", sender=OWNER + 1), msg("/apps", sender=OWNER + 1),
                                    msg("hello"), {"update_id": 1, "edited_message": {}}, [], 42])
def test_ignored_updates_are_200_empty_without_connecting(client, no_connect, update):
    r = post(client, update)
    assert (r.status_code, r.json()) == (200, {})


# --- actions ---

def test_label_up(client, monkeypatch, conns):
    calls = fake(monkeypatch, "set_label", (True, 72))
    r = post(client, tap("u:42"))
    assert r.json() == tg.answer_callback("cb7", tg.SAVED_UP)
    assert calls == [(42, "up")] and len(conns) == 1


def test_label_down(client, monkeypatch, conns):
    calls = fake(monkeypatch, "set_label", (True, None))
    assert post(client, tap("d:42")).json() == tg.answer_callback("cb7", tg.SAVED_DOWN)
    assert calls == [(42, "down")]


def test_label_unknown_job(client, monkeypatch, conns):
    fake(monkeypatch, "set_label", (False, None))
    assert post(client, tap("u:42")).json() == tg.answer_callback("cb7", tg.UNKNOWN_JOB)


@pytest.mark.parametrize("result, text", [((5, True), tg.APPLIED), ((5, False), tg.ALREADY_APPLIED),
                                          (None, tg.UNKNOWN_JOB)])
def test_applied(client, monkeypatch, conns, result, text):
    calls = fake(monkeypatch, "mark_applied", result)
    assert post(client, tap("a:42")).json() == tg.answer_callback("cb7", text)
    assert calls == [(42,)]


def test_list_apps(client, monkeypatch, conns):
    rows = [{"id": 3, "company": "Acme", "title": "Intern", "status": "applied", "applied_on": "2026-10-01"}]
    fake(monkeypatch, "open_applications", rows)
    assert post(client, msg("/apps")).json() == tg.send_text(OWNER, tg.format_apps(rows))


@pytest.mark.parametrize("result, text", [(True, "Updated #3 → interview"), (False, "Unknown application #3")])
def test_set_status(client, monkeypatch, conns, result, text):
    calls = fake(monkeypatch, "set_status", result)
    assert post(client, msg("/s 3 interview Tuesday")).json() == tg.send_text(OWNER, text)
    assert calls == [(3, "interview", "Tuesday")]


@pytest.mark.parametrize("result, text", [((8, True), "Added #8"), ((8, False), "Already tracked #8")])
def test_add_application(client, monkeypatch, conns, result, text):
    calls = fake(monkeypatch, "add_application", result)
    assert post(client, msg("/add Acme | Intern | https://acme.example/1")).json() == tg.send_text(OWNER, text)
    assert calls == [("Acme", "Intern", "https://acme.example/1", "manual")]


def test_help_needs_no_database(client, no_connect):
    assert post(client, msg("/help")).json() == tg.send_text(OWNER, tg.HELP)


# --- the write connection and errors ---

def test_feedback_url_unset_is_503(client, monkeypatch, no_connect):
    monkeypatch.delenv("FEEDBACK_DATABASE_URL")
    r = post(client, tap("u:42"))
    assert (r.status_code, r.json()) == (503, {"detail": "feedback database not configured"})


def test_write_conn_is_not_read_only(monkeypatch):
    seen = []

    @contextmanager
    def fake_connect(url, read_only=False):
        seen.append((url, read_only))
        yield FakeConn()
    monkeypatch.setattr(wh, "connect", fake_connect)
    monkeypatch.setenv("FEEDBACK_DATABASE_URL", URL)
    with wh.get_write_conn() as conn:
        assert isinstance(conn, FakeConn)
    assert seen == [(URL, False)]


@pytest.mark.parametrize("error, status", [
    (psycopg.OperationalError, 503), (psycopg.errors.QueryCanceled, 504)])
def test_database_errors_retry_without_leaking(client, monkeypatch, caplog, error, status):
    def boom(url, read_only=False):
        raise error(f"connection to {URL} failed: password fbPassw0rd rejected")
    monkeypatch.setattr(wh, "connect", boom)
    monkeypatch.setenv("FEEDBACK_DATABASE_URL", URL)
    with caplog.at_level(logging.INFO, logger="jobpilot.telegram"):
        r = post(client, tap("u:42"))
    assert r.status_code == status  # non-2xx, so Telegram tries again later
    assert "<DATABASE_URL>" in caplog.text  # logged, but redacted
    for text in (r.text, caplog.text):
        assert "fbPassw0rd" not in text and "db.example" not in text


def test_unexpected_error_is_200_with_type_only_log(client, monkeypatch, conns, caplog):
    def broken(conn, *args):
        raise RuntimeError(f"bad value from chat {OWNER}: {SECRET}")
    monkeypatch.setattr(fb, "set_label", broken)
    with caplog.at_level(logging.INFO, logger="jobpilot.telegram"):
        r = post(client, tap("u:42"))
    # 200, so a poison update isn't retried forever; the button gets an answer instead of a spinner
    assert (r.status_code, r.json()) == (200, tg.answer_callback("cb7", tg.ERROR))
    assert "RuntimeError" in caplog.text and "telegram: label" in caplog.text
    assert SECRET not in caplog.text and str(OWNER) not in caplog.text  # the type only, not the message
    assert len([rec for rec in caplog.records if rec.levelno == logging.WARNING]) == 1


def test_unexpected_error_on_a_message_is_200_empty(client, monkeypatch, conns):
    monkeypatch.setattr(fb, "open_applications", lambda conn, *a: 1 / 0)
    r = post(client, msg("/apps"))
    assert (r.status_code, r.json()) == (200, {})  # no callback to answer


# --- body size ---

def test_body_over_the_cap_by_content_length_is_413(client, no_connect):
    r = client.post(PATH, content=b"x" * (wh.MAX_BODY + 1), headers=HEADERS)
    assert (r.status_code, r.json()) == (413, {"detail": "body too large"})


def test_chunked_body_over_the_cap_is_413(client, no_connect):
    # a generator body is sent chunked, without Content-Length, so only the stream count can catch it
    chunks = (b"x" * 400_000 for _ in range(3))
    r = client.post(PATH, content=chunks, headers=HEADERS)
    assert (r.status_code, r.json()) == (413, {"detail": "body too large"})


def test_body_at_the_cap_is_read(client, no_connect):
    r = client.post(PATH, content=b" " * (wh.MAX_BODY - 2) + b"{}", headers=HEADERS)
    assert (r.status_code, r.json()) == (200, {})  # valid JSON, an ignored update


def asgi_post(headers: dict, chunks: list[bytes]) -> tuple[int, int]:
    """Calls the app straight through ASGI with a body we control; returns (status, chunks read).
    TestClient always sends an honest Content-Length, so a lying one needs this."""
    import asyncio
    app, read, sent = api.create_app(), [], []

    async def receive():
        i = len(read)
        read.append(i)
        return {"type": "http.request", "body": chunks[i] if i < len(chunks) else b"",
                "more_body": i < len(chunks) - 1}

    async def send(message):
        sent.append(message)
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
             "scheme": "http", "path": PATH, "raw_path": PATH.encode(), "query_string": b"", "root_path": "",
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
             "client": ("127.0.0.1", 1), "server": ("testserver", 80)}
    asyncio.run(app(scope, receive, send))
    return next(m["status"] for m in sent if m["type"] == "http.response.start"), len(read)


def test_lying_content_length_is_still_capped(no_connect):
    status, read = asgi_post({**HEADERS, "Content-Length": "10"}, [b"x" * 600_000] * 3)
    assert status == 413 and read == 2  # stopped as soon as the count passed the cap, not at the end


def test_wrong_secret_never_reads_the_body(no_connect):
    status, read = asgi_post({wh.SECRET_HEADER: "wrong", "Content-Length": "2"}, [b"{}"])
    assert (status, read) == (401, 0)


def test_secret_and_owner_never_in_responses_or_logs(client, monkeypatch, conns, caplog):
    fake(monkeypatch, "set_label", (True, 1))
    fake(monkeypatch, "open_applications", [])
    updates = [tap("u:1"), msg("/apps"), msg("hello"), tap("u:1", sender=OWNER + 1)]
    with caplog.at_level(logging.DEBUG):
        texts = [post(client, u).text for u in updates] + [post(client, tap("u:1"), headers={}).text]
    # send_text replies carry chat_id by design (Telegram needs it); the owner id must appear nowhere else
    texts = [t for t in texts if '"chat_id"' not in t]
    for text in texts + [caplog.text]:
        assert SECRET not in text and str(OWNER) not in text
    assert "telegram: label" in caplog.text and "telegram: apps" in caplog.text


def test_route_is_hidden_from_openapi():
    assert PATH not in api.create_app(docs=True).openapi()["paths"]
    assert PATH not in api.spec_json()  # so docs/openapi*.json and their snapshot tests are unchanged


def test_route_is_mounted_even_with_docs_off(monkeypatch, no_connect):
    monkeypatch.setenv("JOBPILOT_DOCS", "0")
    assert TestClient(api.create_app()).post(PATH, json={}, headers=HEADERS).status_code == 200


# --- end to end against the throwaway schema ---

@pytest.mark.db
def test_button_tap_writes_feedback_as_the_feedback_role(pg, client, monkeypatch):
    from test_db import load, one, rec
    from test_feedback_schema_db import FEEDBACK_ROLE, as_role
    load(pg, [rec(**{"Job ID": "1", "Match Score (/100)": 72})])
    job = one(pg, "SELECT id FROM jobs")[0]

    @contextmanager
    def pg_conn():
        with as_role(pg, FEEDBACK_ROLE):  # the same privileges FEEDBACK_DATABASE_URL will log in with
            yield pg
    monkeypatch.setattr(wh, "get_write_conn", pg_conn)
    assert post(client, tap(f"u:{job}")).json() == tg.answer_callback("cb7", tg.SAVED_UP)
    assert one(pg, "SELECT label, score_at_label, source FROM feedback WHERE job_id = %s", (job,)) == ("up", 72, "telegram")
