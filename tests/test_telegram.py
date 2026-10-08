"""Tests for jobpilot.telegram: parsing Updates into actions and building replies. Pure: no network, no DB."""
import datetime as dt

import pytest

from jobpilot import telegram as tg
from jobpilot.feedback import STATUSES
from jobpilot.telegram import AddApp, Applied, Help, Incoming, Label, ListApps, SetStatus

OWNER = 123456789


def tap(data, sender=OWNER, callback_id="cb1") -> dict:
    return {"update_id": 1, "callback_query": {"id": callback_id, "from": {"id": sender}, "data": data}}


def msg(text, sender=OWNER, chat=OWNER, kind="message") -> dict:
    return {"update_id": 1, kind: {"message_id": 5, "from": {"id": sender}, "chat": {"id": chat}, "text": text}}


def parse(update):
    return tg.parse_update(update, OWNER)


# --- button taps ---

@pytest.mark.parametrize("data, action", [
    ("u:42", Label(42, "up")), ("d:42", Label(42, "down")), ("a:42", Applied(42)),
    (f"a:{2**63 - 1}", Applied(2**63 - 1)),  # the largest BIGINT is still valid
])
def test_callback_actions(data, action):
    assert parse(tap(data)) == Incoming(action, OWNER, "cb1")


@pytest.mark.parametrize("data", [
    "u:0", "u:-1", "u:007", "u:", "u:abc", "u:4.2", "u: 42", "u:42 ", "x:42", "U:42", "u42", "u:42:1",
    f"u:{2**63}",  # one past BIGINT
    "u:٤٢",  # Arabic-Indic digits: int() accepts them, the regex mustn't
    "u:" + "1" * 70,  # over the 64-byte limit
    "", None, 42,
])
def test_callback_rejects_malformed_data(data):
    assert parse(tap(data)) is None


def test_callback_64_byte_limit_counts_bytes_not_characters():
    assert len("u:" + "1" * 62) == 64
    assert parse(tap("u:" + "1" * 62)) is None  # 64 bytes is allowed by Telegram but over BIGINT anyway
    assert parse(tap("é" * 33)) is None  # 33 characters, 66 bytes


def test_callback_from_someone_else_is_ignored():
    assert parse(tap("u:42", sender=OWNER + 1)) is None


@pytest.mark.parametrize("cq", [
    {"id": "cb1", "data": "u:42"},  # no from
    {"id": "cb1", "from": {}, "data": "u:42"},
    {"id": "cb1", "from": {"id": str(OWNER)}, "data": "u:42"},  # id as text
    {"from": {"id": OWNER}, "data": "u:42"},  # no callback id to answer
])
def test_callback_missing_fields(cq):
    assert parse({"update_id": 1, "callback_query": cq}) is None


def test_owner_check_rejects_bool():
    assert tg.parse_update(tap("u:42", sender=True), owner_id=1) is None  # True == 1 in Python


# --- text commands ---

@pytest.mark.parametrize("text, action", [
    ("/start", Help()), ("/help", Help()), ("/apps", ListApps()), ("/APPS", ListApps()),
    ("/apps@example_bot", ListApps()), ("  /apps  ", ListApps()),
    ("/s 3 interview", SetStatus(3, "interview", None)),
    ("/s 3 Interview  Tuesday 10:00 ", SetStatus(3, "interview", "Tuesday 10:00")),
    ("/s@example_bot 3 offer", SetStatus(3, "offer", None)),
    ("/s 3\nrejected", SetStatus(3, "rejected", None)),
    ("/add Acme | Data Intern", AddApp("Acme", "Data Intern", None)),
    ("/add  Acme |Data Intern| https://acme.example/jobs/1 ", AddApp("Acme", "Data Intern", "https://acme.example/jobs/1")),
    ("/add Acme | Data Intern |  ", AddApp("Acme", "Data Intern", None)),
])
def test_commands(text, action):
    assert parse(msg(text)) == Incoming(action, OWNER)


def test_every_status_is_accepted():
    for status in STATUSES:
        assert parse(msg(f"/s 1 {status}")).action.status == status


@pytest.mark.parametrize("text", [
    "/s", "/s 3", "/s 3 ghosted", "/s x interview", "/s 0 interview", f"/s {2**63} interview",
    "/add", "/add Acme", "/add | Data Intern", "/add Acme |  ", "/add a | b | c | d",
    "/delete 3", "/", "hello", "apps", "", "   ",
])
def test_bad_commands_and_plain_text_are_ignored(text):
    assert parse(msg(text)) is None


def test_message_from_someone_else_is_ignored():
    assert parse(msg("/apps", sender=OWNER + 1, chat=OWNER + 1)) is None


def test_owner_in_a_group_is_ignored():
    assert parse(msg("/apps", chat=-1001234)) is None  # private chat only


def test_edited_message_is_ignored():
    assert parse(msg("/apps", kind="edited_message")) is None


@pytest.mark.parametrize("update", [
    {"update_id": 1, "message": {"chat": {"id": OWNER}, "text": "/apps"}},  # no from (e.g. a channel post)
    {"update_id": 1, "message": {"from": {"id": OWNER}, "text": "/apps"}},  # no chat
    {"update_id": 1, "message": {"from": {"id": OWNER}, "chat": {"id": OWNER}}},  # a photo: no text
    {"update_id": 1, "my_chat_member": {}}, {"update_id": 1}, {}, [], None,
    # wrong types where Telegram sends objects: ignored, never an AttributeError (a 500 in the webhook)
    {"update_id": 1, "message": {"from": "x", "chat": {"id": OWNER}, "text": "/apps"}},
    {"update_id": 1, "message": {"from": {"id": OWNER}, "chat": [OWNER], "text": "/apps"}},
    {"update_id": 1, "message": "/apps"},
    {"update_id": 1, "callback_query": "u:42"},
    {"update_id": 1, "callback_query": None, "message": {"from": {"id": OWNER}, "chat": {"id": OWNER}, "text": "/apps"}},
    {"update_id": 1, "callback_query": {"id": "cb1", "from": 7, "data": "u:42"}},
])
def test_other_updates_are_ignored(update):
    assert parse(update) is None


# --- replies ---

def test_reply_shapes():
    assert tg.answer_callback("cb1", tg.SAVED_UP) == {
        "method": "answerCallbackQuery", "callback_query_id": "cb1", "text": "Saved 👍"}
    assert tg.send_text(OWNER, "hi") == {"method": "sendMessage", "chat_id": OWNER, "text": "hi"}


def test_format_apps():
    rows = [{"id": 7, "company": "Acme", "title": "Data Intern", "status": "interview", "applied_on": dt.date(2026, 10, 1)},
            {"id": 3, "company": "Globex", "title": "QA Intern", "status": "applied", "applied_on": dt.date(2026, 9, 20)}]
    assert tg.format_apps(rows) == ("#7 interview · Data Intern @ Acme (2026-10-01)\n"
                                    "#3 applied · QA Intern @ Globex (2026-09-20)")
    assert tg.format_apps([]) == "No open applications."


def test_help_lists_commands_and_statuses():
    for word in ("/apps", "/s", "/add", "/help", *STATUSES):
        assert word in tg.HELP
