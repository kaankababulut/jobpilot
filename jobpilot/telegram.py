"""Telegram webhook, the pure half: turns an incoming Update into an action, and builds the replies.
No network and no database here, so every rule (who may talk to the bot, what a valid command is) is
tested without either. Anything unexpected parses to None and the webhook just ignores it.
Replies are Bot API calls returned as the webhook's HTTP response body, which Telegram executes itself,
so the server needs no outgoing call (and no bot token) to answer."""
import re
from dataclasses import dataclass
from typing import NamedTuple

from jobpilot.feedback import STATUSES

MAX_ID = 2**63 - 1  # BIGINT; a larger id would be a database error, not just "unknown job"
MAX_CALLBACK_BYTES = 64  # Telegram's own limit on callback_data, so anything longer isn't ours
# [0-9] rather than \d, which also matches other scripts' digits; no leading zero, so one id has one spelling
_ID = r"[1-9][0-9]{0,18}"
_CALLBACK_RE = re.compile(rf"([uda]):({_ID})")
_ID_RE = re.compile(_ID)

SAVED_UP, SAVED_DOWN = "Saved 👍", "Saved 👎"
APPLIED, ALREADY_APPLIED, UNKNOWN_JOB = "Marked as applied ✅", "Already applied", "Unknown job"
NO_APPS = "No open applications."
ERROR = "Something went wrong"
HELP = ("JobPilot commands:\n"
        "/apps - open applications\n"
        "/s <id> <status> [note] - update an application\n"
        "/add Company | Title | URL - track an application made elsewhere (URL optional)\n"
        "/help - this text\n"
        f"Statuses: {', '.join(STATUSES)}\n"
        "Buttons under each job: 👍 / 👎 to label it, ✅ when you've applied.")


# frozen dataclasses rather than NamedTuples: two empty NamedTuples (ListApps() and Help()) would compare equal
@dataclass(frozen=True)
class Label:
    job_id: int
    label: str  # 'up' or 'down'


@dataclass(frozen=True)
class Applied:
    job_id: int


@dataclass(frozen=True)
class ListApps:
    pass


@dataclass(frozen=True)
class SetStatus:
    app_id: int
    status: str
    note: str | None


@dataclass(frozen=True)
class AddApp:
    company: str
    title: str
    url: str | None


@dataclass(frozen=True)
class Help:
    pass


Action = Label | Applied | ListApps | SetStatus | AddApp | Help


class Incoming(NamedTuple):
    """A parsed update: what to do, where to reply, and (for a button tap) the id to acknowledge."""
    action: Action
    chat_id: int
    callback_id: str | None = None  # set for button taps, which must be answered with answerCallbackQuery


def _is_owner(value, owner_id: int) -> bool:
    # bool is an int in Python, so True == 1 would pass a bare == check
    return isinstance(value, int) and not isinstance(value, bool) and value == owner_id


def _parse_id(text: str) -> int | None:
    if not _ID_RE.fullmatch(text):
        return None
    n = int(text)
    return n if n <= MAX_ID else None


def _id_of(obj: dict, key: str):
    # isinstance, not `or {}`: a string or list where Telegram sends an object must be ignored, not crash
    inner = obj.get(key)
    return inner.get("id") if isinstance(inner, dict) else None


def _parse_callback(cq: dict, owner_id: int) -> Incoming | None:
    sender = _id_of(cq, "from")
    data, callback_id = cq.get("data"), cq.get("id")
    if not _is_owner(sender, owner_id) or not isinstance(data, str) or not isinstance(callback_id, str):
        return None
    if len(data.encode("utf-8")) > MAX_CALLBACK_BYTES:
        return None
    m = _CALLBACK_RE.fullmatch(data)
    job_id = _parse_id(m.group(2)) if m else None
    if job_id is None:
        return None
    kind = m.group(1)
    action = Applied(job_id) if kind == "a" else Label(job_id, "up" if kind == "u" else "down")
    return Incoming(action, sender, callback_id)


def _parse_command(text: str) -> Action | None:
    words = text.split(None, 1)  # any whitespace, so a newline after the command works too
    if not words:
        return None
    head, rest = words[0], words[1] if len(words) == 2 else ""
    command, _, _bot = head.partition("@")  # "/apps@jobpilot_kaan_bot": the suffix Telegram adds in menus
    command, rest = command.lower(), rest.strip()
    if command in ("/start", "/help"):
        return Help()
    if command == "/apps":
        return ListApps()
    if command == "/s":
        parts = rest.split(None, 2)
        if len(parts) < 2:
            return None
        app_id, status = _parse_id(parts[0]), parts[1].lower()
        if app_id is None or status not in STATUSES:
            return None
        note = parts[2].strip() if len(parts) == 3 else ""
        return SetStatus(app_id, status, note or None)
    if command == "/add":
        parts = [p.strip() for p in rest.split("|")]
        if len(parts) not in (2, 3) or not parts[0] or not parts[1]:
            return None
        return AddApp(parts[0], parts[1], (parts[2] if len(parts) == 3 else "") or None)
    return None  # unknown command or plain text


def parse_update(update: dict, owner_id: int) -> Incoming | None:
    """The action an Update asks for, or None to ignore it: not from the owner, not a private chat with
    the owner, malformed, or a kind of update the bot doesn't handle (edited messages, joins, ...)."""
    if not isinstance(update, dict):
        return None
    if "callback_query" in update:
        cq = update["callback_query"]
        return _parse_callback(cq, owner_id) if isinstance(cq, dict) else None
    msg = update.get("message")
    if not isinstance(msg, dict):
        return None  # edited_message and every other update type
    sender, chat = _id_of(msg, "from"), _id_of(msg, "chat")
    text = msg.get("text")
    # both, so the owner talking in a group (chat id != owner) is ignored too: private chat only
    if not _is_owner(sender, owner_id) or not _is_owner(chat, owner_id) or not isinstance(text, str):
        return None
    action = _parse_command(text)
    return Incoming(action, chat) if action else None


def answer_callback(callback_id: str, text: str) -> dict:
    return {"method": "answerCallbackQuery", "callback_query_id": callback_id, "text": text}


def send_text(chat_id: int, text: str) -> dict:
    return {"method": "sendMessage", "chat_id": chat_id, "text": text}


def format_apps(rows: list[dict]) -> str:
    """The /apps reply, from feedback.open_applications rows."""
    if not rows:
        return NO_APPS
    return "\n".join(f"#{r['id']} {r['status']} · {r['title']} @ {r['company']} ({r['applied_on']})" for r in rows)
