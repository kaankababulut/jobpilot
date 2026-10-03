"""Points the Azure Postgres firewall rule at this PC's current public IP: python -m jobpilot.azure_firewall
Why: the home IP changes (often daily), and Azure's firewall silently drops packets from any IP that
isn't listed, so the 12:00 cloud load would just time out. The daily run calls update_home_rule right
before the Azure load; it logs one line and returns False instead of raising, like db.safe_load.

Least privilege: it signs in as an Entra app (`jobpilot-firewall-updater`) whose only permission is a
custom role on the one server allowing Microsoft.DBforPostgreSQL/flexibleServers/firewallRules read and
write. A leaked secret can move that one rule, nothing else. Stdlib only, so nothing new to install."""
import ipaddress
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable
from urllib.parse import quote, urlsplit

API_VERSION = "2022-12-01"  # GA for flexibleServers/firewallRules; verify on first run (2024-08-01 is also GA)
IP_SERVICES = ("https://api.ipify.org", "https://checkip.amazonaws.com")  # second is the fallback
# the whole update, probe included, delays the 12:00 run by about this much at most. Every request and
# probe gets a timeout no longer than what's left; DNS lookups and slow reads can stretch it slightly (no threads)
BUDGET_S = 180
PROBE_EVERY_S = 10
ENV_VARS = ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_SUBSCRIPTION_ID", "AZURE_DATABASE_URL")


class _Fail(Exception):
    """An expected failure carrying the exact line to log."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib follows redirects by default and resends our headers, Bearer token included. Refusing them
    # means the token only ever goes to the URL we built; a 3xx comes back as an HTTPError status instead
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _urllib_request(method: str, url: str, headers: dict, body: bytes | None, timeout: float) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with _opener.open(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:  # the server answered: a status to inspect, not a network error
        try:
            data = e.read()
        except Exception:
            data = b""
        return e.code, dict(e.headers or {}), data


def _tcp_probe(host: str, port: int, timeout: float) -> bool:
    # a bare TCP connect is enough: a firewalled IP gets no answer at all, an allowed one gets a handshake
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _arm_error(status: int, body: bytes) -> str:
    # Azure errors look like {"error": {"code": "AuthorizationFailed", "message": ...}}; only the code is
    # logged, because messages can echo subscription and object IDs
    try:
        code = json.loads(body)["error"]["code"]
        if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.]{1,80}", code):
            return code
    except Exception:
        pass
    return f"HTTP {status}"


def _token_error(status: int, body: bytes) -> str:
    # the token endpoint answers {"error": "invalid_client", "error_codes": [7000222], ...}
    try:
        data = json.loads(body)
        codes = data.get("error_codes") or []
        if codes and isinstance(codes[0], int):
            return f"AADSTS{codes[0]}"
        m = re.search(r"AADSTS\d+", str(data.get("error_description", "")))
        if m:
            return m.group(0)
    except Exception:
        pass
    return f"HTTP {status}"


def update_home_rule(cfg: dict, log: Callable[[str], None], http=_urllib_request, probe=_tcp_probe,
                     clock=time.monotonic, sleep=time.sleep) -> bool:
    """Sets the firewall rule to this PC's public IP and waits until the database answers.
    Returns True when the database is reachable through the rule; logs and returns False otherwise."""
    hidden: list[str] = []  # the secret and, once fetched, the token: scrubbed from every log line

    def say(msg: str) -> None:
        for s in hidden:
            msg = msg.replace(s, "***")
        try:  # same guard as safe_load's say: a failed log line must not break the run
            log(msg)
        except Exception:
            pass

    try:
        env = {k: os.environ.get(k, "").strip() for k in ENV_VARS}
        for k in ENV_VARS:
            if not env[k]:
                say(f"Azure firewall update skipped: {k} not set")
                return False
        hidden.append(env["AZURE_CLIENT_SECRET"])
        fw = cfg.get("azure_firewall") or {}
        for k in ("resource_group", "server", "rule"):
            if not str(fw.get(k, "")).strip():
                say(f"Azure firewall update skipped: azure_firewall.{k} not set in config.json")
                return False
        rule = fw["rule"]
        db = urlsplit(env["AZURE_DATABASE_URL"])
        if not db.hostname:
            raise _Fail("WARNING: Azure firewall update skipped: AZURE_DATABASE_URL has no host")
        host, port = db.hostname, db.port or 5432

        deadline = clock() + BUDGET_S

        def remaining() -> float:
            left = deadline - clock()
            if left <= 0:
                raise _Fail(f"WARNING: Azure firewall update failed: ran out of time ({BUDGET_S}s)")
            return left

        def call(method: str, url: str, headers: dict, body: bytes | None = None) -> tuple[int, bytes]:
            status, _, data = http(method, url, headers, body, min(15, remaining()))
            return status, data

        ip = None
        for service in IP_SERVICES:  # try each until one gives a valid public IPv4 address
            try:
                status, data = call("GET", service, {})
                text = data.decode("ascii", "replace").strip() if status == 200 else ""
                addr = ipaddress.IPv4Address(text)
                if addr.is_global:  # a private/reserved answer means a proxy or captive portal, not our IP
                    ip = str(addr)
                    break
            except _Fail:
                raise
            except Exception:  # bad answer or network error: fall through to the next service
                continue
        if ip is None:
            raise _Fail("WARNING: Azure firewall update skipped: could not determine the public IP")

        token_url = f"https://login.microsoftonline.com/{quote(env['AZURE_TENANT_ID'], safe='')}/oauth2/v2.0/token"
        form = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": env["AZURE_CLIENT_ID"],
                                       "client_secret": env["AZURE_CLIENT_SECRET"],
                                       "scope": "https://management.azure.com/.default"}).encode()
        status, data = call("POST", token_url, {"Content-Type": "application/x-www-form-urlencoded"}, form)
        if status != 200:
            code = _token_error(status, data)
            if code == "AADSTS7000222":
                raise _Fail("WARNING: Azure firewall: client secret expired; create a new one (see README: Azure firewall auto-update)")
            raise _Fail(f"WARNING: Azure firewall: sign-in failed ({code})")
        token = json.loads(data)["access_token"]
        hidden.append(token)

        rule_url = (f"https://management.azure.com/subscriptions/{quote(env['AZURE_SUBSCRIPTION_ID'], safe='')}"
                    f"/resourceGroups/{quote(fw['resource_group'], safe='')}/providers/Microsoft.DBforPostgreSQL"
                    f"/flexibleServers/{quote(fw['server'], safe='')}/firewallRules/{quote(rule, safe='')}"
                    f"?api-version={API_VERSION}")
        auth = {"Authorization": f"Bearer {token}"}
        status, data = call("GET", rule_url, auth)
        if status == 200:
            props = json.loads(data).get("properties", {})
            start, end = props.get("startIpAddress"), props.get("endIpAddress")
            if start == end == ip:
                say(f"Azure firewall: rule {rule} already allows {ip}")
                return True
            old = start if start == end else f"{start}-{end}"
        elif status == 404:  # first run: the rule doesn't exist yet, so the PUT creates it
            old = "none"
        else:
            raise _Fail(f"WARNING: Azure firewall: reading rule {rule} failed ({_arm_error(status, data)})")

        body = json.dumps({"properties": {"startIpAddress": ip, "endIpAddress": ip}}).encode()
        status, data = call("PUT", rule_url, {**auth, "Content-Type": "application/json"}, body)
        if status not in (200, 201, 202):  # 202 = accepted, applied in the background; the probe waits for it
            raise _Fail(f"WARNING: Azure firewall: updating rule {rule} failed ({_arm_error(status, data)})")

        # Azure takes a while to apply a rule; wait until the database actually answers, so the load
        # right after this doesn't time out against a rule that isn't live yet
        started = clock()
        try:
            while True:
                if probe(host, port, min(5, remaining())):
                    say(f"Azure firewall: rule {rule} {old} -> {ip}; database reachable after {round(clock() - started)}s")
                    return True
                sleep(min(PROBE_EVERY_S, remaining()))
        except _Fail:
            raise _Fail(f"WARNING: Azure firewall: rule updated but database not reachable after {BUDGET_S}s")
    except _Fail as e:
        say(str(e))
        return False
    except Exception as e:  # never raise: the Excel run and the load after this must go on
        msg = (str(e).splitlines() or [""])[0]
        say(f"WARNING: Azure firewall update failed: {type(e).__name__}: {msg}")
        return False


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
    except ImportError:  # same fallback as job_searcher: env vars set by hand still work
        def load_dotenv(*args, **kwargs): return False
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(root, ".env"))
    with open(os.path.join(root, "config.json"), encoding="utf-8") as f:
        config = json.load(f)
    sys.exit(0 if update_home_rule(config, print) else 1)
