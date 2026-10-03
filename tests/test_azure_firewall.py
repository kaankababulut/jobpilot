"""Tests for jobpilot.azure_firewall.update_home_rule (fake http, probe, clock and sleep: no network, no waiting)."""
import json
import urllib.error

import pytest

import job_searcher
from jobpilot import azure_firewall as fw

SECRET, TOKEN = "s3cret-Value~x", "eyJ0b2tlbi1zZWNyZXQ"
ENV = {"AZURE_TENANT_ID": "tenant-1", "AZURE_CLIENT_ID": "client-1", "AZURE_CLIENT_SECRET": SECRET,
       "AZURE_SUBSCRIPTION_ID": "sub-1",
       "AZURE_DATABASE_URL": "postgresql://admin:pw@psql-jobpilot-kk.postgres.database.azure.com/jobs?sslmode=require"}
CFG = {"azure_firewall": {"resource_group": "rg-jobpilot", "server": "psql-jobpilot-kk", "rule": "home"}}
TOKEN_URL = "https://login.microsoftonline.com/tenant-1/oauth2/v2.0/token"
RULE_URL = ("https://management.azure.com/subscriptions/sub-1/resourceGroups/rg-jobpilot/providers/"
            f"Microsoft.DBforPostgreSQL/flexibleServers/psql-jobpilot-kk/firewallRules/home?api-version={fw.API_VERSION}")
IPIFY, AMAZON = fw.IP_SERVICES
IP = "81.214.5.9"  # not a TEST-NET address: ipaddress counts those as non-global, so they'd be rejected


def rule(ip):
    return 200, json.dumps({"properties": {"startIpAddress": ip, "endIpAddress": ip}}).encode()


class World:
    """Fake http/probe/clock/sleep. routes: (method, url) -> list of responses, each (status, bytes) or an
    exception; the last response repeats. Records every call and the remaining budget at that moment."""
    def __init__(self, routes, probes=(True,), probe_cost=0.0):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.probes, self.probe_cost = list(probes), probe_cost
        self.now, self.start = 1000.0, 1000.0
        self.calls, self.probe_calls, self.sleeps, self.timeouts = [], [], [], []

    def remaining(self):
        return self.start + fw.BUDGET_S - self.now

    def http(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, body))
        self.timeouts.append((timeout, self.remaining()))
        queue = self.routes.get((method, url))
        if queue is None:
            pytest.fail(f"unexpected request {method} {url}")
        r = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(r, BaseException):
            raise r
        return r[0], {}, r[1]

    def probe(self, host, port, timeout):
        self.probe_calls.append((host, port))
        self.timeouts.append((timeout, self.remaining()))
        self.now += self.probe_cost
        return self.probes.pop(0) if len(self.probes) > 1 else self.probes[0]

    def clock(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s

    def run(self, cfg=CFG):
        lines = []
        ok = fw.update_home_rule(cfg, lines.append, http=self.http, probe=self.probe, clock=self.clock, sleep=self.sleep)
        return ok, lines

    def methods(self):
        return [(m, u) for m, u, _, _ in self.calls]


def base_routes(over=None):
    routes = {("GET", IPIFY): [(200, f"{IP}\n".encode())],
              ("POST", TOKEN_URL): [(200, json.dumps({"access_token": TOKEN, "expires_in": 3599}).encode())],
              ("GET", RULE_URL): [rule("1.2.3.4")],
              ("PUT", RULE_URL): [(200, b"{}")]}
    routes.update(over or {})
    return routes


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)


def assert_clean(lines):
    for line in lines:
        assert SECRET not in line and TOKEN not in line


@pytest.mark.parametrize("name", fw.ENV_VARS)
def test_missing_env_var_skips_without_http(monkeypatch, name):
    monkeypatch.setenv(name, "  ")
    w = World({})
    ok, lines = w.run()
    assert ok is False and w.calls == []
    assert lines == [f"Azure firewall update skipped: {name} not set"]


def test_missing_config_block_skips_without_http():
    w = World({})
    ok, lines = w.run(cfg={})
    assert ok is False and w.calls == [] and "skipped" in lines[0]


def test_unchanged_ip_reads_rule_and_does_not_put():
    w = World(base_routes({("GET", RULE_URL): [rule(IP)]}))
    ok, lines = w.run()
    assert ok is True
    assert w.methods() == [("GET", IPIFY), ("POST", TOKEN_URL), ("GET", RULE_URL)]
    assert lines == [f"Azure firewall: rule home already allows {IP}"] and w.probe_calls == []


def test_changed_ip_puts_rule_and_waits_for_database():
    w = World(base_routes(), probes=(False, False, True))
    ok, lines = w.run()
    assert ok is True
    put = [c for c in w.calls if c[0] == "PUT"]
    assert len(put) == 1
    _, url, headers, body = put[0]
    assert url == RULE_URL and headers["Authorization"] == f"Bearer {TOKEN}"
    assert json.loads(body) == {"properties": {"startIpAddress": IP, "endIpAddress": IP}}
    token_form = [c for c in w.calls if c[0] == "POST"][0][3].decode()
    assert "grant_type=client_credentials" in token_form and "scope=https%3A%2F%2Fmanagement.azure.com%2F.default" in token_form
    assert w.probe_calls == [("psql-jobpilot-kk.postgres.database.azure.com", 5432)] * 3
    assert w.sleeps == [fw.PROBE_EVERY_S] * 2
    assert lines == [f"Azure firewall: rule home 1.2.3.4 -> {IP}; database reachable after 20s"]
    assert_clean(lines)


def test_missing_rule_is_created():
    w = World(base_routes({("GET", RULE_URL): [(404, b'{"error": {"code": "ResourceNotFound"}}')],
                             ("PUT", RULE_URL): [(202, b"")]}))
    ok, lines = w.run()
    assert ok is True and ("PUT", RULE_URL) in w.methods()
    assert lines == [f"Azure firewall: rule home none -> {IP}; database reachable after 0s"]


def test_ipify_failure_falls_back_to_second_service():
    w = World(base_routes({("GET", IPIFY): [urllib.error.URLError("down")],
                             ("GET", AMAZON): [(200, f"{IP}\n".encode())],
                             ("GET", RULE_URL): [rule(IP)]}))
    ok, _ = w.run()
    assert ok is True and w.methods()[:2] == [("GET", IPIFY), ("GET", AMAZON)]


@pytest.mark.parametrize("first,second", [
    (urllib.error.URLError("down"), urllib.error.URLError("down")),
    ((200, b"10.0.0.1"), (200, b"<html>captive portal</html>")),
    ((500, b"oops"), (200, b"127.0.0.1")),
    ((200, b"2001:db8::1"), (200, b"")),
])
def test_no_valid_public_ip_stops_before_token(first, second):
    w = World(base_routes({("GET", IPIFY): [first], ("GET", AMAZON): [second]}))
    ok, lines = w.run()
    assert ok is False and ("POST", TOKEN_URL) not in w.methods()
    assert len(lines) == 1 and lines[0].startswith("WARNING:") and "public IP" in lines[0]


def test_expired_secret_has_its_own_message():
    body = json.dumps({"error": "invalid_client", "error_codes": [7000222],
                       "error_description": f"AADSTS7000222: The provided client secret keys are expired. {SECRET}"})
    w = World(base_routes({("POST", TOKEN_URL): [(401, body.encode())]}))
    ok, lines = w.run()
    assert ok is False and ("GET", RULE_URL) not in w.methods()
    assert lines == ["WARNING: Azure firewall: client secret expired; create a new one (see README: Azure firewall auto-update)"]


def test_other_sign_in_error_logs_only_the_code():
    body = json.dumps({"error": "invalid_client", "error_description": "AADSTS7000215: Invalid client secret provided."})
    w = World(base_routes({("POST", TOKEN_URL): [(401, body.encode())]}))
    ok, lines = w.run()
    assert ok is False and lines == ["WARNING: Azure firewall: sign-in failed (AADSTS7000215)"]


def test_put_forbidden_logs_error_code_only():
    body = json.dumps({"error": {"code": "AuthorizationFailed", "message": "client 'client-1' with object id ... sub-1"}})
    w = World(base_routes({("PUT", RULE_URL): [(403, body.encode())]}))
    ok, lines = w.run()
    assert ok is False and w.probe_calls == []
    assert lines == ["WARNING: Azure firewall: updating rule home failed (AuthorizationFailed)"]
    assert "sub-1" not in lines[0]


@pytest.mark.parametrize("route", [("POST", TOKEN_URL), ("GET", RULE_URL), ("PUT", RULE_URL)])
def test_network_error_anywhere_is_one_warning(route):
    leak = SECRET if route[0] == "POST" else f"{SECRET} {TOKEN}"  # the token can't leak before it's issued
    w = World(base_routes({route: [urllib.error.URLError(f"timed out {leak}")]}))
    ok, lines = w.run()
    assert ok is False and len(lines) == 1
    assert lines[0].startswith("WARNING: Azure firewall update failed: URLError:")
    assert_clean(lines)


def test_secret_and_token_scrubbed_from_unexpected_errors():
    def http(method, url, headers, body, timeout):
        if url == RULE_URL:
            raise RuntimeError(f"bad header Authorization: Bearer {TOKEN} for {SECRET}\nsecond line")
        return World(base_routes()).http(method, url, headers, body, timeout)
    lines = []
    ok = fw.update_home_rule(CFG, lines.append, http=http, probe=lambda *a: True, clock=lambda: 0.0, sleep=lambda s: None)
    assert ok is False and len(lines) == 1
    assert "***" in lines[0] and "second line" not in lines[0]
    assert_clean(lines)


def test_raising_log_never_breaks_the_caller():
    def boom(msg):
        raise OSError("log file locked")
    w = World(base_routes())
    assert fw.update_home_rule(CFG, boom, http=w.http, probe=w.probe, clock=w.clock, sleep=w.sleep) is True


def test_budget_ends_probe_loop_and_bounds_every_timeout():
    w = World(base_routes(), probes=(False,), probe_cost=5)
    ok, lines = w.run()
    assert ok is False
    assert lines == [f"WARNING: Azure firewall: rule updated but database not reachable after {fw.BUDGET_S}s"]
    assert w.remaining() <= 0 and w.now - w.start <= fw.BUDGET_S  # never waits past the deadline
    assert len(w.probe_calls) <= fw.BUDGET_S // fw.PROBE_EVERY_S + 1
    assert all(0 < t <= rem for t, rem in w.timeouts)
    assert all(t <= 15 for t, _ in w.timeouts)


def test_slow_azure_never_exceeds_the_budget():
    # each request eats most of the budget; the next one must get a timeout no larger than what's left
    w = World(base_routes())
    real_http = w.http

    def slow_http(*args):
        r = real_http(*args)
        w.now += 70
        return r
    lines = []
    ok = fw.update_home_rule(CFG, lines.append, http=slow_http, probe=w.probe, clock=w.clock, sleep=w.sleep)
    assert ok is False and "ran out of time" in lines[0]
    assert all(0 < t <= rem for t, rem in w.timeouts)


def test_load_databases_updates_firewall_before_azure_load(monkeypatch):
    order = []
    monkeypatch.setattr(job_searcher.jobdb, "safe_load",
                        lambda rows, today, cats, cv, log, **kw: order.append(kw.get("label", "local")) or True)
    monkeypatch.setattr(job_searcher.azure_firewall, "update_home_rule", lambda cfg, log: order.append("firewall") or True)
    job_searcher.load_databases([], "2026-10-01", {"cv_skills": []})
    assert order == ["local", "firewall", "Azure Postgres"]


@pytest.mark.parametrize("outcome", [False, RuntimeError("bug")])
def test_azure_load_still_runs_when_firewall_update_fails(monkeypatch, outcome):
    order, lines = [], []

    def update(cfg, log):
        order.append("firewall")
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome
    monkeypatch.setattr(job_searcher, "log", lines.append)
    monkeypatch.setattr(job_searcher.jobdb, "safe_load",
                        lambda rows, today, cats, cv, log, **kw: order.append(kw.get("label", "local")) or False)
    monkeypatch.setattr(job_searcher.azure_firewall, "update_home_rule", update)
    job_searcher.load_databases([], "2026-10-01", {"cv_skills": []})
    assert order == ["local", "firewall", "Azure Postgres"]
    if isinstance(outcome, BaseException):
        assert lines == ["WARNING: Azure firewall update failed: RuntimeError"]


def test_redirect_is_refused_not_followed(monkeypatch):
    # a 3xx must come back as a status, so the Bearer token is never resent to the redirect target
    import io
    import urllib.response
    seen = []

    class Redirecting(fw.urllib.request.BaseHandler):
        def https_open(self, req):
            seen.append(req.full_url)
            hdrs = fw.urllib.response.addinfourl(io.BytesIO(b""), {"Location": "https://evil.example/steal"},
                                                 req.full_url, 302)
            hdrs.msg = "Found"
            return hdrs
    # a bare OpenerDirector, not build_opener: build_opener would also add the real HTTPS handler,
    # which could win over the fake and send a real request
    ur = fw.urllib.request
    opener = ur.OpenerDirector()
    for h in (Redirecting(), fw._NoRedirect(), ur.HTTPErrorProcessor(), ur.HTTPDefaultErrorHandler()):
        opener.add_handler(h)
    assert not any(isinstance(h, ur.HTTPSHandler) for h in opener.handlers)
    monkeypatch.setattr(fw, "_opener", opener)
    status, _, _ = fw._urllib_request("GET", RULE_URL, {"Authorization": f"Bearer {TOKEN}"}, None, 5)
    assert status == 302 and seen == [RULE_URL]  # never requested evil.example


def test_redirect_status_makes_the_update_fail():
    w = World(base_routes({("GET", RULE_URL): [(302, b"")]}))
    ok, lines = w.run()
    assert ok is False and ("PUT", RULE_URL) not in w.methods()
    assert lines == ["WARNING: Azure firewall: reading rule home failed (HTTP 302)"]
