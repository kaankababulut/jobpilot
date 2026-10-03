"""Tests for the whole-round fetch retry in job_searcher.fetch_all (fake sources, no network, no sleeping)."""
import io
import ssl
import urllib.error

import pytest

import job_searcher as js
from conftest import make_job

SSL_EOF = urllib.error.URLError(ssl.SSLError("[SSL: UNEXPECTED_EOF_WHILE_READING]"))


@pytest.fixture
def run(monkeypatch):
    """Silences the log (it writes logs/run.log) and records log lines and sleeps instead."""
    lines, sleeps = [], []
    monkeypatch.setattr(js, "log", lines.append)
    monkeypatch.setattr(js.time, "sleep", sleeps.append)
    monkeypatch.setattr(js, "FETCH_RETRY_WAIT", 0)
    return lines, sleeps


def source(name, *results):
    """A fake source that returns (or raises) the next result on each call, and counts its calls."""
    results, calls = list(results), []

    def fetch():
        calls.append(1)
        r = results.pop(0) if len(results) > 1 else results[0]
        if isinstance(r, BaseException):
            raise r
        return r
    return (name, False, fetch), calls


def warnings(lines):
    return [l for l in lines if l.startswith("WARNING: every source failed")]


def test_all_network_fail_then_success_retries_once(run, cfg):
    lines, sleeps = run
    a, a_calls = source("A", SSL_EOF, [make_job(id="1")])
    b, _ = source("B", ConnectionResetError("reset"), [])
    jobs, n_raw = js.fetch_all([a, b], set(), cfg)
    assert [j["id"] for j in jobs] == ["1"] and n_raw == 1
    assert warnings(lines) == ["WARNING: every source failed with a network error; retrying in 0s (attempt 2 of 3)"]
    assert sleeps == [0] and len(a_calls) == 2


def test_always_failing_tries_three_times_then_exits(run, cfg):
    lines, sleeps = run
    a, a_calls = source("A", SSL_EOF)
    b, _ = source("B", TimeoutError("timed out"))
    with pytest.raises(SystemExit) as e:
        js.fetch_all([a, b], set(), cfg)
    assert e.value.code == 1
    assert len(a_calls) == js.FETCH_ATTEMPTS == 3
    assert len(warnings(lines)) == 2 and sleeps == [0, 0]
    assert lines[-1] == "ERROR: no postings returned from any source; keeping previous files."


def test_partial_failure_does_not_retry(run, cfg):
    # the successful source may be a paid Apify search, which must never be re-run
    lines, sleeps = run
    a, a_calls = source("A", [make_job(id="1")])
    b, _ = source("B", SSL_EOF)
    jobs, n_raw = js.fetch_all([a, b], set(), cfg)
    assert n_raw == 1 and len(a_calls) == 1
    assert not warnings(lines) and sleeps == []


def test_http_error_on_all_sources_does_not_retry(run, cfg):
    # HTTPError subclasses URLError, but a 401 means the server answered: a bad token, not a down network
    lines, sleeps = run
    err = urllib.error.HTTPError("https://api.apify.com", 401, "Unauthorized", {}, io.BytesIO())
    a, a_calls = source("A", err)
    with pytest.raises(SystemExit):
        js.fetch_all([a], set(), cfg)
    assert len(a_calls) == 1 and not warnings(lines) and sleeps == []


def test_zero_postings_does_not_retry(run, cfg):
    lines, sleeps = run
    a, a_calls = source("A", [])
    b, _ = source("B", [])
    with pytest.raises(SystemExit):
        js.fetch_all([a, b], set(), cfg)
    assert len(a_calls) == 1 and not warnings(lines) and sleeps == []


def test_network_and_http_mix_does_not_retry(run, cfg):
    lines, sleeps = run
    a, _ = source("A", SSL_EOF)
    b, _ = source("B", urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO()))
    with pytest.raises(SystemExit):
        js.fetch_all([a, b], set(), cfg)
    assert not warnings(lines) and sleeps == []


@pytest.mark.parametrize("exc, expected", [
    (SSL_EOF, True),
    (ssl.SSLError("eof"), True),
    (ConnectionRefusedError(), True),
    (TimeoutError(), True),
    (urllib.error.HTTPError("u", 500, "Server Error", {}, io.BytesIO()), False),
    (ValueError("bad JSON"), False),
    (PermissionError("run.log is locked"), False),  # an OSError, but not a network one
])
def test_is_network_error(exc, expected):
    assert js.is_network_error(exc) is expected
