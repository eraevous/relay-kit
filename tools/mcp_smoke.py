#!/usr/bin/env python3
"""relay-kit mcp-smoke: drive a running relay-kit/mcp@2 HTTP endpoint and report. Standard library only.

    relay-kit mcp-smoke --url http://127.0.0.1:8765/mcp --token FILE
    relay-kit mcp-smoke --url https://<tunnel-host>/mcp --token FILE      (what ChatGPT will see)

Checks, in order: a request without the token is refused (401) and the body holds nothing from
the records; GET is 405; initialize negotiates a supported revision; tools/list names the read
tools; a tools/call (status) answers with the envelope (schema, evidence_class, provenance,
content_level); a batch is answered as a batch. Prints one line per check and exits 1 if any
failed. Nothing it prints is record text: the status result is summarised by its counts only.

It is a client of the server, not a test of ChatGPT: whether ChatGPT accepts a bearer-only server
is learned by adding the URL there (docs/mcp.md, runbook).
"""
import argparse, json, pathlib, sys, urllib.error, urllib.request

READ_TOOLS = ["status", "list_decisions", "get_decision", "list_challenges", "list_handoffs", "get_handoff",
              "search", "export", "threads"]


def call(url, body, token=None, method="POST", headers=None, timeout=30):
    data = body.encode("utf-8") if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", errors="replace"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace"), dict(e.headers)


def rpc(mid, method, params=None):
    m = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        m["params"] = params
    return json.dumps(m)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="relay-kit mcp-smoke", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="the endpoint, ending in /mcp")
    ap.add_argument("--token", required=True, type=pathlib.Path, help="the token file the server was started with")
    ap.add_argument("--version", default="2025-11-25", help="protocol revision to ask for")
    ap.add_argument("--timeout", type=float, default=30)
    a = ap.parse_args(argv)
    token = a.token.read_text(encoding="utf-8").strip()
    results = []

    def check(name, ok, detail=""):
        results.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not ok else ""))

    s, b, _ = call(a.url, rpc(1, "ping"), token=None, timeout=a.timeout)
    check("no token -> 401, body without record fields", s == 401 and "provenance" not in b and "handoff" not in b.lower(), f"{s} {b[:120]}")
    s, b, _ = call(a.url, rpc(1, "ping"), token="not-the-token", timeout=a.timeout)
    check("wrong token -> 401", s == 401, f"{s}")
    s, b, h = call(a.url, None, token=token, method="GET", timeout=a.timeout)
    check("GET -> 405 with Allow: POST", s == 405 and "POST" in (h.get("Allow") or ""), f"{s} {h.get('Allow')}")
    s, b, _ = call(a.url, rpc(1, "initialize", {"protocolVersion": a.version, "capabilities": {},
                                                "clientInfo": {"name": "relay-kit mcp-smoke", "version": "0"}}), token=token, timeout=a.timeout)
    try:
        init = json.loads(b)["result"]
    except (ValueError, KeyError):
        init = {}
    check(f"initialize {a.version} -> that revision, read-only instructions", s == 200 and init.get("protocolVersion") == a.version
          and "not instructions" in init.get("instructions", ""), f"{s} {b[:200]}")
    s, b, _ = call(a.url, json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}), token=token, timeout=a.timeout)
    check("notification -> 202, no body", s == 202 and b == "", f"{s} {b[:80]}")
    s, b, _ = call(a.url, rpc(2, "tools/list"), token=token, headers={"MCP-Protocol-Version": a.version}, timeout=a.timeout)
    try:
        tools = json.loads(b)["result"]["tools"]
        names = [t["name"] for t in tools]
    except (ValueError, KeyError, TypeError):
        tools, names = [], []
    check("tools/list -> the read tools, all readOnlyHint", s == 200 and names == READ_TOOLS
          and all(t.get("annotations", {}).get("readOnlyHint") for t in tools), f"{s} {names}")
    s, b, _ = call(a.url, rpc(3, "tools/call", {"name": "status", "arguments": {}}), token=token, timeout=a.timeout)
    try:
        sc = json.loads(b)["result"]["structuredContent"]
    except (ValueError, KeyError, TypeError):
        sc = {}
    check("tools/call status -> envelope with schema, evidence_class, provenance, content_level",
          s == 200 and sc.get("schema", "").startswith("relay-kit/mcp@") and sc.get("evidence_class") == "derived"
          and str(sc.get("provenance", "")).startswith("read from") and sc.get("content_level") in ("full", "metadata"),
          f"{s} {sorted(sc)[:8]}")
    if sc:
        counts = (sc.get("summary") or {}).get("counts") or {}
        print(f"      content_level={sc.get('content_level')}  counts={json.dumps(counts, sort_keys=True)}")
    s, b, _ = call(a.url, json.dumps([{"jsonrpc": "2.0", "id": 4, "method": "ping"},
                                      {"jsonrpc": "2.0", "method": "notifications/x"}]), token=token, timeout=a.timeout)
    try:
        batch = json.loads(b)
    except ValueError:
        batch = None
    check("batch -> a batch, notifications left out", s == 200 and batch == [{"id": 4, "jsonrpc": "2.0", "result": {}}], f"{s} {b[:120]}")
    print(f"\n{sum(results)} passed, {len(results) - sum(results)} failed  ({a.url})")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
