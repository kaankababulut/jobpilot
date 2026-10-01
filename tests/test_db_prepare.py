"""Tests for the pure parts of jobpilot.db (no database needed)."""
import datetime as dt
import sys
import types

from jobpilot import db
from test_records import make_row


def test_prepare_skips_bad_row_and_logs_its_id():
    lines = []
    recs = db.prepare([make_row(**{"Job ID": "111"}), make_row(**{"Job ID": "222", "Run Date": "nope"})],
                      lines.append)
    assert [r["source_id"] for r in recs] == ["111"]
    assert any("222" in line for line in lines)
    assert any("1 row" in line for line in lines)


def test_prepare_dedupes_keeping_latest_run_date():
    rows = [make_row(**{"Job ID": "111", "Run Date": "2026-09-29", "Match Score (/100)": 90}),
            make_row(**{"Job ID": "111", "Run Date": "2026-09-27", "Match Score (/100)": 40})]
    recs = db.prepare(rows, lambda msg: None)
    assert len(recs) == 1
    assert recs[0]["run_date"] == dt.date(2026, 9, 29) and recs[0]["match_score"] == 90


def test_prepare_merges_float_and_text_id():
    rows = [make_row(**{"Job ID": 4012345678.0}), make_row(**{"Job ID": "4012345678"})]
    recs = db.prepare(rows, lambda msg: None)
    assert [r["source_id"] for r in recs] == ["4012345678"]


def test_connect_sets_timeouts(monkeypatch):
    seen = {}
    fake = types.SimpleNamespace(connect=lambda url, **kw: seen.update(url=url, **kw))
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    db.connect("postgresql://example")
    assert seen["connect_timeout"] == 5
    assert "statement_timeout=30000" in seen["options"]
    assert "read_only" not in seen["options"]  # the loader must stay able to write


def test_connect_read_only_adds_server_setting(monkeypatch):
    seen = {}
    fake = types.SimpleNamespace(connect=lambda url, **kw: seen.update(url=url, **kw))
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    db.connect("postgresql://example", read_only=True)
    assert "default_transaction_read_only=on" in seen["options"]
    assert "statement_timeout=30000" in seen["options"]  # timeouts still apply
