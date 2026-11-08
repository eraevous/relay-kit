#!/usr/bin/env python3
"""relay-kit mcp-smoke: drive a running relay-kit/mcp@2 HTTP endpoint and report. Standard library only.

    relay-kit mcp-smoke --url http://127.0.0.1:8765/mcp --token FILE
    relay-kit mcp-smoke --url https://<tunnel-host>/mcp --token FILE      (what ChatGPT will see)

Checks, in order: requests without the right token get the fixed unauthorized response;
GET is 405; initialize negotiates the requested revision; tools/list names the read tools;
a tools/call (status) answers with the envelope and numeric counts; a batch is answered as
a batch. Prints fixed check names, vetted status values and bounded numeric counts only,
including on failure. Response text, URLs, token paths and exception details are never printed.

HTTPS uses the standard certificate and hostname checks. Plain HTTP requires a literal
loopback IP, not a DNS name (including localhost). Redirects and environment/system proxies
are disabled for this client only. This does not authenticate a local process or protect
against a compromised machine, certificate trust store, or the chosen HTTPS endpoint.

It is a client of the server, not a test of ChatGPT: whether ChatGPT accepts a bearer-only server
is learned by adding the URL there (docs/mcp.md, runbook).
"""
import argparse
import http.client
import ipaddress
import json
import math
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

READ_TOOLS = ["status", "list_decisions", "get_decision", "list_challenges", "list_handoffs", "get_handoff",
              "search", "export", "threads"]
COUNT_NAMES = ("decisions", "challenges", "handoffs", "outputs")
MAX_COUNT = 2 ** 63 - 1
MAX_RESPONSE = 1024 * 1024
MAX_TOKEN = 4096


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Refuse even same-origin redirects: a smoke check tests exactly the supplied endpoint.
        return None


def validate_url(url):
    """Require HTTPS or a literal loopback HTTP address; never resolve a plaintext hostname.

    Reject parser ambiguities before urllib can normalize them. No DNS lookup is used to
    justify plaintext transport, avoiding a check-then-resolve rebinding window.
    """
    if not isinstance(url, str) or len(url) > 8192 or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        raise ValueError("invalid endpoint")
    try:
        p = urllib.parse.urlsplit(url)
        if p.scheme not in ("https", "http") or not p.hostname or p.username is not None or p.password is not None:
            raise ValueError
        if not re.fullmatch(r"(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(?::[0-9]+)?", p.netloc):
            raise ValueError
        if p.fragment or "\\" in url or (p.port is not None and not 1 <= p.port <= 65535):
            raise ValueError
        if p.scheme == "http":
            address = ipaddress.ip_address(p.hostname)
            if not address.is_loopback or (address.version == 6 and address != ipaddress.IPv6Address("::1")):
                raise ValueError
    except ValueError:
        raise ValueError("invalid endpoint") from None
    return url


def call(url, body, token=None, method="POST", headers=None, timeout=30):
    validate_url(url)
    data = body.encode("utf-8") if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    # A private opener leaves other clients unchanged. An empty ProxyHandler also disables
    # platform proxy discovery, so a loopback credential cannot be sent via a remote proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        response = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        response = e
    with response as r:
        raw = r.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise ValueError("response too large")
        return r.status, raw.decode("utf-8", errors="replace"), dict(r.headers)


def rpc(mid, method, params=None):
    m = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        m["params"] = params
    return json.dumps(m)


def json_body(body):
    def unique_object(pairs):
        decoded = {}
        for key, value in pairs:
            if key in decoded:
                # Last-key-wins parsing could conceal record text in an unauthorized body.
                raise ValueError("duplicate JSON key")
            decoded[key] = value
        return decoded

    def reject_constant(value):
        raise ValueError("invalid JSON constant")
    try:
        return json.loads(body, parse_constant=reject_constant, object_pairs_hook=unique_object)
    except (ValueError, TypeError, RecursionError):
        return None


def obj(value):
    return value if isinstance(value, dict) else {}


def result(body):
    return obj(obj(json_body(body)).get("result"))


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse errors normally repeat the supplied argument, which may contain secrets.
        raise ValueError("invalid arguments")


def main(argv=None):
    ap = SafeArgumentParser(prog="relay-kit mcp-smoke", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="HTTPS endpoint, or HTTP with a literal loopback IP")
    ap.add_argument("--token", required=True, type=pathlib.Path, help="the token file the server was started with")
    ap.add_argument("--version", default="2025-11-25", help="protocol revision to ask for")
    ap.add_argument("--timeout", type=float, default=30)
    try:
        a = ap.parse_args(argv)
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", a.version) or not math.isfinite(a.timeout) or a.timeout <= 0:
            raise ValueError
    except ValueError:
        print("FAIL  arguments")
        return 2
    try:
        validate_url(a.url)
    except ValueError:
        print("FAIL  endpoint requires HTTPS or literal loopback HTTP without userinfo")
        return 2
    try:
        with a.token.open("rb") as f:
            raw = f.read(MAX_TOKEN + 3)
        token = raw.decode("ascii").strip()
        if len(raw) > MAX_TOKEN + 2 or not token or len(token) > MAX_TOKEN or any(ord(c) <= 32 or ord(c) >= 127 for c in token):
            raise ValueError
    except (OSError, ValueError):
        print("FAIL  token file")
        return 2
    results = []

    def check(name, ok, status):
        results.append(bool(ok))
        # Do not interpolate any server-provided name, text, header, URL or exception.
        vetted_status = str(status) if type(status) is int and 100 <= status <= 599 else "unavailable"
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  -- HTTP {vetted_status}" if not ok else ""))

    def request(body, credential=token, method="POST", headers=None):
        try:
            return call(a.url, body, token=credential, method=method, headers=headers, timeout=a.timeout)
        except (OSError, ValueError, http.client.HTTPException, OverflowError):
            # Includes TLS, DNS, timeout, malformed HTTP and header-encoding failures.
            return None, "", {}

    s, b, _ = request(rpc(1, "ping"), credential=None)
    check("no token -> 401, fixed unauthorized body", s == 401 and json_body(b) == {"error": "unauthorized"}, s)
    s, b, _ = request(rpc(1, "ping"), credential="not-the-token")
    check("wrong token -> 401, fixed unauthorized body", s == 401 and json_body(b) == {"error": "unauthorized"}, s)
    s, b, h = request(None, method="GET")
    check("GET -> 405 with Allow: POST", s == 405 and h.get("Allow") == "POST", s)
    s, b, _ = request(rpc(1, "initialize", {"protocolVersion": a.version, "capabilities": {},
                                          "clientInfo": {"name": "relay-kit mcp-smoke", "version": "0"}}))
    init = result(b)
    check("initialize -> requested revision, read-only instructions", s == 200 and init.get("protocolVersion") == a.version
          and isinstance(init.get("instructions"), str) and "not instructions" in init["instructions"], s)
    s, b, _ = request(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
    check("notification -> 202, no body", s == 202 and b == "", s)
    s, b, _ = request(rpc(2, "tools/list"), headers={"MCP-Protocol-Version": a.version})
    tools = result(b).get("tools")
    check("tools/list -> the read tools, all readOnlyHint", s == 200 and isinstance(tools, list)
          and [obj(t).get("name") for t in tools] == READ_TOOLS
          and all(obj(obj(t).get("annotations")).get("readOnlyHint") is True for t in tools), s)
    s, b, _ = request(rpc(3, "tools/call", {"name": "status", "arguments": {}}))
    sc = obj(result(b).get("structuredContent"))
    counts = obj(obj(sc.get("summary")).get("counts"))
    good_counts = all(type(counts.get(k)) is int and 0 <= counts[k] <= MAX_COUNT for k in COUNT_NAMES)
    valid_status = (s == 200 and isinstance(sc.get("schema"), str)
                    and re.fullmatch(r"relay-kit/mcp@[1-9][0-9]{0,5}", sc["schema"]) is not None
                    and sc.get("evidence_class") == "derived" and isinstance(sc.get("provenance"), str)
                    and sc["provenance"].startswith("read from") and sc.get("content_level") in ("full", "metadata")
                    and good_counts)
    check("tools/call status -> envelope with schema, evidence_class, provenance, content_level, counts", valid_status, s)
    if valid_status:
        print("      content_level=" + sc["content_level"] + "  counts: "
              + " ".join(f"{k}={counts[k]}" for k in COUNT_NAMES))
    s, b, _ = request(json.dumps([{"jsonrpc": "2.0", "id": 4, "method": "ping"},
                                 {"jsonrpc": "2.0", "method": "notifications/x"}]))
    check("batch -> a batch, notifications left out", s == 200 and json_body(b) == [{"id": 4, "jsonrpc": "2.0", "result": {}}], s)
    print(f"\n{sum(results)} passed, {len(results) - sum(results)} failed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
