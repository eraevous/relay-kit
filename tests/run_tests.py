#!/usr/bin/env python3
"""Regression tests for the relay kit. Standard library only; no network, no accounts.

Run from the kit root:  python3 -I -B tests/run_tests.py   (and: node tests/browser_tests.cjs)
Each case is adapted from the 2026-10-28 ChatGPT review's probes, rewritten to assert the
behavior the kit should have. Exit code 1 if any case fails."""
import contextlib, csv, datetime, importlib.util, io, json, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
LEDGER = KIT / "skills" / "handoff-ledger" / "scripts" / "handoff.py"
FIXTURE = KIT / "tests" / "fixtures" / "HO-20261028-001.md"   # frozen prepared copy
HID = FIXTURE.stem
TMP = Path(tempfile.mkdtemp(prefix="relaykit-tests-"))
X = TMP / "extracted"   # generated from the skills for this run; nothing generated is kept in git
RESULTS = []


def case(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not ok else ""))


def run(script, *args, env=None):
    e = dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    e.update(env or {})
    p = subprocess.run([sys.executable, "-I", "-B", str(script), *map(str, args)], capture_output=True,
                       encoding="utf-8", env=e, timeout=30)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ---------------- handoff ledger ----------------
def ledger_dir(name, text=None):
    d = TMP / "ledger" / name
    (d / "outputs").mkdir(parents=True)
    (d / FIXTURE.name).write_text(text if text is not None else FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    (d / ".ledger.json").write_text('{"owner": "sam"}', encoding="utf-8")   # the fixture's owner
    return d


def setstate(d, state, by, note=None, hid=HID):
    args = ["--dir", d, "set", hid, state, "--by", by] + (["--note", note] if note is not None else [])
    return run(LEDGER, *args)


def ledger_tests():
    if (KIT / "handoffs").is_dir():   # present in the private working copy, not in the public repo
        rc, out, _ = run(LEDGER, "--dir", KIT / "handoffs", "check")
        case("ledger: shipped handoff folder passes check", rc == 0, out)

    d = TMP / "ledger" / "no-config"
    rc, hid, _ = run(LEDGER, "--dir", d, "new", "--from", "claude:a", "--by", "claude:a#t1", "--to", "chatgpt:b", "--title", "T")
    r1 = run(LEDGER, "--dir", d, "set", hid, "cancelled", "--by", "sam", "--note", "x")
    r2 = run(LEDGER, "--dir", d, "set", hid, "cancelled", "--by", "owner", "--note", "x")
    case("ledger: with no .ledger.json the owner is 'owner'", r1[0] != 0 and r2[0] == 0, r1[2] + r2[2])
    d = TMP / "ledger" / "named-owner"
    run(LEDGER, "--dir", d, "new", "--from", "claude:a", "--by", "claude:a#t1", "--to", "chatgpt:b", "--title", "T")
    (d / ".ledger.json").write_text('{"owner": "Sam"}', encoding="utf-8")
    hid = next(d.glob("HO-*.md")).stem
    r3 = run(LEDGER, "--dir", d, "set", hid, "cancelled", "--by", "sam", "--note", "x")
    case("ledger: .ledger.json names the owner (case-insensitive)", r3[0] == 0, r3[2])
    d = TMP / "ledger" / "bad-config"
    run(LEDGER, "--dir", d, "new", "--from", "claude:a", "--by", "claude:a#t1", "--to", "chatgpt:b", "--title", "T")
    (d / ".ledger.json").write_text('{"owner": "two words"}', encoding="utf-8")
    case("ledger: a malformed owner setting is refused", run(LEDGER, "--dir", d, "list")[0] != 0)

    d = ledger_dir("lifecycle")
    steps = [setstate(d, "sent", "claude:relay-kit", "local test delivery"),
             setstate(d, "acknowledged", "chatgpt:ada", "read")]
    (d / "outputs" / FIXTURE.name).write_text("HO-20261028-001 · chatgpt:ada · test\n", encoding="utf-8")
    steps.append(setstate(d, "completed", "chatgpt:ada", "outputs/HO-20261028-001.md"))
    steps.append(run(LEDGER, "--dir", d, "check"))
    case("ledger: prepared -> sent -> acknowledged -> completed, then check", all(s[0] == 0 for s in steps),
         str([s[2] for s in steps if s[0]]))
    case("ledger: completed can't reopen", setstate(d, "in_progress", "chatgpt:ada")[0] != 0)

    for name, state, by, note in [("can't skip delivery", "acknowledged", "chatgpt:ada", "x"),
                                  ("wrong sender refused", "sent", "chatgpt:ada", "x"),
                                  ("sent needs a note", "sent", "claude:relay-kit", None),
                                  ("only the owner cancels", "cancelled", "claude:relay-kit", "x")]:
        case(f"ledger: {name}", setstate(ledger_dir(name.replace(' ', '-').replace("'", '')), state, by, note)[0] != 0)

    d = ledger_dir("output-required")
    setstate(d, "sent", "claude:relay-kit", "x"); setstate(d, "acknowledged", "chatgpt:ada", "x")
    case("ledger: completed needs the output file", setstate(d, "completed", "chatgpt:ada", "x")[0] != 0)
    case("ledger: duplicate acknowledgement refused", setstate(d, "acknowledged", "chatgpt:ada", "x")[0] != 0)

    d = ledger_dir("blocked-return")
    a = setstate(d, "blocked", "claude:relay-kit", "waiting on Sam")
    b = setstate(d, "sent", "claude:relay-kit", "skips the return")
    c = setstate(d, "prepared", "claude:relay-kit", "unblocked")
    case("ledger: blocked returns only to the state it left", a[0] == 0 and b[0] != 0 and c[0] == 0, str([a, b, c]))

    d = ledger_dir("sent-no-note")
    p = d / FIXTURE.name
    p.write_text(p.read_text(encoding="utf-8").replace("status: prepared", "status: sent")
                 + "- 2026-10-28T14:06Z | sent | claude:relay-kit |\n", encoding="utf-8")
    case("ledger: check flags a sent line with no note", run(LEDGER, "--dir", d, "check")[0] != 0)

    d = ledger_dir("status-log-disagree")
    p = d / FIXTURE.name
    p.write_text(p.read_text(encoding="utf-8").replace("status: prepared", "status: cancelled"), encoding="utf-8")
    before = p.read_text(encoding="utf-8")
    r = setstate(d, "sent", "claude:relay-kit", "x")
    case("ledger: set refuses an already-inconsistent file, unchanged", r[0] != 0 and p.read_text(encoding="utf-8") == before, r[2])

    d = TMP / "ledger" / "unfinished"
    rc, hid, _ = run(LEDGER, "--dir", d, "new", "--from", "claude:test", "--by", "claude:test#t1", "--to", "chatgpt:test", "--title", "Draft")
    r = setstate(d, "sent", "claude:test", "x", hid=hid)
    case("ledger: a TODO-filled draft can't be sent", rc == 0 and r[0] != 0, r[2])

    d = ledger_dir("log-not-last")
    p = d / FIXTURE.name
    p.write_text(p.read_text(encoding="utf-8") + "\n## Appendix\nMore.\n", encoding="utf-8")
    before = p.read_text(encoding="utf-8")
    r = setstate(d, "sent", "claude:relay-kit", "x")
    case("ledger: refuses a file whose Log isn't the last section", r[0] != 0 and p.read_text(encoding="utf-8") == before)

    d = ledger_dir("multiline-note")
    r = setstate(d, "sent", "claude:relay-kit", "ok\n- 2026-10-28T14:07Z | acknowledged | chatgpt:ada | injected")
    case("ledger: multi-line note rejected", r[0] != 0 and "acknowledged" not in (d / FIXTURE.name).read_text(encoding="utf-8").split("## Log")[1])

    d = ledger_dir("traversal")
    outside = d.parent / "outside-sentinel.md"
    shutil.copy2(FIXTURE, outside)
    r = run(LEDGER, "--dir", d, "set", "../outside-sentinel", "sent", "--by", "claude:relay-kit", "--note", "x")
    case("ledger: id can't escape the folder", r[0] != 0 and "status: prepared" in outside.read_text(encoding="utf-8"), r[2])

    d = ledger_dir("locked")
    (d / ".handoff.lock").write_text("other\n")
    r = setstate(d, "sent", "claude:relay-kit", "x")
    case("ledger: a held lock refuses the update", r[0] != 0 and "lock" in r[2])
    case("ledger: check reports a leftover lock", run(LEDGER, "--dir", d, "check")[0] != 0)

    d = ledger_dir("at-flag")
    a = run(LEDGER, "--dir", d, "set", HID, "sent", "--by", "sam", "--at", "2026-10-28T17:50Z", "--note", "approx.")
    last = (d / FIXTURE.name).read_text(encoding="utf-8").rstrip().splitlines()[-1]
    case("ledger: --at logs the event time and keeps the recording time", a[0] == 0 and last.startswith("- 2026-10-28T17:50Z | sent | sam |") and "(recorded " in last, last)
    b = run(LEDGER, "--dir", d, "set", HID, "acknowledged", "--by", "sam", "--at", "2026-10-28T17:40Z")
    case("ledger: --at can't go earlier than the previous line", b[0] != 0, b[2])
    c = run(LEDGER, "--dir", d, "set", HID, "acknowledged", "--by", "sam", "--at", "2099-01-01T00:00Z")
    case("ledger: --at can't be in the future", c[0] != 0, c[2])

    d = ledger_dir("out-of-order")
    p = d / FIXTURE.name
    p.write_text(p.read_text(encoding="utf-8").replace("status: prepared", "status: sent")
                 + "- 2026-10-28T09:00Z | sent | claude:relay-kit | before it was drafted\n", encoding="utf-8")
    case("ledger: check flags log lines out of time order", run(LEDGER, "--dir", d, "check")[0] != 0)

    # The 2026-10-29 collision: two sessions, two clones, the same new id.
    origin = TMP / "ids-origin.git"
    g = lambda cwd, *a: subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], capture_output=True)
    A, B = TMP / "ids-A", TMP / "ids-B"
    for c in (A, B):
        subprocess.run(["git", "clone", "-q", str(origin), str(c)], capture_output=True)
        g(c, "config", "user.name", "t"); g(c, "config", "user.email", "t@t")
    (B / "handoffs").mkdir()
    (B / "handoffs" / ".keep").write_text("")
    g(B, "add", "."); g(B, "commit", "-qm", "init"); g(B, "push", "-q", "origin", "main")
    g(A, "pull", "-q", "origin", "main")
    rc, hid_a, _ = run(LEDGER, "--dir", A / "handoffs", "new", "--from", "claude:aec", "--by", "claude:aec#t1", "--to", "codex:aec", "--title", "AEC work")
    g(A, "add", "."); g(A, "commit", "-qm", "aec handoff"); g(A, "push", "-q", "origin", "main")
    rc2, hid_b, err_b = run(LEDGER, "--dir", B / "handoffs", "new", "--from", "claude:rk", "--by", "claude:rk#t1", "--to", "claude:builder", "--title", "Pilot")
    case("ledger: new gives two clones different ids, each carrying its lane",
         rc == 0 and rc2 == 0 and hid_a != hid_b and hid_a.startswith("HO-AEC-") and hid_b.startswith("HO-RK-"),
         f"{hid_a} / {hid_b} {err_b}")
    tpl = (A / "handoffs" / f"{hid_a}.md").read_text(encoding="utf-8")
    case("ledger: an unfilled Authorized section says not to start", "NOT AUTHORIZED YET" in tpl)

    d = ledger_dir("renumbered-output")
    p = d / FIXTURE.name
    p.write_text(p.read_text(encoding="utf-8").replace("outputs/HO-20261028-001.md", "outputs/HO-20261028-009.md"), encoding="utf-8")
    rc, so, _ = run(LEDGER, "--dir", d, "check")
    r = setstate(d, "sent", "claude:relay-kit", "x")
    case("ledger: check warns (doesn't block) on an Expected output naming another handoff's file",
         rc == 0 and "WARNING" in so and "HO-20261028-009" in so and r[0] == 0, so + r[2])

    d = TMP / "ledger" / "block-draft"
    rc, hid, _ = run(LEDGER, "--dir", d, "new", "--from", "claude:a", "--by", "claude:a#t1", "--to", "chatgpt:b", "--title", "T")
    r = run(LEDGER, "--dir", d, "set", hid, "blocked", "--by", "claude:a#t1", "--note", "reported run without authorization")
    case("ledger: an unfilled draft can be marked blocked", r[0] == 0 and run(LEDGER, "--dir", d, "check")[0] == 0, r[2])

    d = ledger_dir("log-inside")
    setstate(d, "sent", "claude:relay-kit", "x")
    txt = (d / FIXTURE.name).read_text(encoding="utf-8")
    case("ledger: new log line lands inside Log, file ends cleanly", txt.rstrip().endswith("| sent | claude:relay-kit | x")
         and not list(d.glob(".*.tmp")))


# ---------------- capture -> seed / SSD ----------------
def chatgpt_fixture(times=True, leaf=True):
    msgs = [("u1", "user", "Please preserve the exact reply pointers."),
            ("a1", "assistant", "First assistant statement belongs only to a1."),
            ("a2", "assistant", "Second assistant statement belongs only to a2."),
            ("u2", "user", "I approve only the bounded review."),
            ("a3", "assistant", "Acknowledged bounded review.")]
    stamps = [1793196000, 1793196005, 1793196010, 1793197810, 1793197815]
    mp = {}
    for i, (mid, role, text) in enumerate(msgs):
        m = dict(id=mid, author=dict(role=role), content=dict(content_type="text", parts=[text]), recipient="all", metadata={})
        if times: m["create_time"] = stamps[i]
        mp[mid] = dict(id=mid, parent=msgs[i - 1][0] if i else None,
                       children=[msgs[i + 1][0]] if i + 1 < len(msgs) else [], message=m)
    d = dict(title="Fixture", conversation_id="fixture00", mapping=mp)
    if leaf: d["current_node"] = "a3"
    return d


def wrap(d):
    return dict(_capture=dict(platform="chatgpt", kind="conversation", id="fixture00"), data=d)


def render(name, data, extra_env=None):
    raw = TMP / f"{name}.raw.json"
    raw.write_text(json.dumps(data), encoding="utf-8")
    out = TMP / f"{name}-rendered"
    rc, so, se = run(X / "chat-capture" / "03_render.py", raw, "-o", out, env=extra_env)
    return rc, so, se, out


def capture_tests():
    rc, so, se, out = render("fixture", wrap(chatgpt_fixture()))
    case("capture: renders the fixture", rc == 0, se)
    cap_json, cap_md = next(out.glob("*.json")), next(out.glob("*.md"))
    seed = load("seed_index", X / "thread-seed" / "01_seed_index.py")
    want = ["T1u", "T1a", "T1a2", "T2u", "T2a"]
    case("seed: JSON index keeps every message id", [seed.mid(m) for m in seed.load(str(cap_json))] == want)
    case("seed: Markdown index keeps every message id", [seed.mid(m) for m in seed.load(str(cap_md))] == want)

    rc2, _, _, out2 = render("no-time", wrap(chatgpt_fixture(times=False)))
    md2 = next(out2.glob("*.md"))
    case("seed: timestamp-free Markdown keeps T1a2", [seed.mid(m) for m in seed.load(str(md2))] == want,
         str([seed.mid(m) for m in seed.load(str(md2))]))

    mixed_msgs = [dict(n=1, k=1, role="user", label="You", text="Compare this.\nFROM: Ada\nanswer\nEND FROM", ts="")]
    case("seed: a FROM block inside your message is flagged as a paste, speaker stays you",
         "contains paste" in seed.index(mixed_msgs) and "(relayed)" not in seed.index(mixed_msgs))
    q = TMP / "quote.md"
    q.write_text('"First assistant statement belongs only to a1." [T1a2]\n', encoding="utf-8")
    case("seed: quote cited to the wrong message fails", run(X / "thread-seed" / "01_seed_index.py", cap_json, "--verify", q)[0] == 1)
    q.write_text('"First assistant statement belongs only to a1." [T1a]\n', encoding="utf-8")
    case("seed: quote cited to the right message passes", run(X / "thread-seed" / "01_seed_index.py", cap_json, "--verify", q)[0] == 0)
    sparse = json.loads(cap_json.read_text(encoding="utf-8"))
    for t in sparse["turns"]:
        if t["n"] == 2: t["n"] = 3
    sp = TMP / "sparse.json"; sp.write_text(json.dumps(sparse), encoding="utf-8")
    q.write_text("Missing exchange [T2].\n", encoding="utf-8")
    case("seed: a ref to a missing exchange fails", run(X / "thread-seed" / "01_seed_index.py", sp, "--verify", q)[0] == 1)

    rc3, _, se3, _ = render("no-leaf", chatgpt_fixture(leaf=False) | {"mapping": {**chatgpt_fixture()["mapping"],
        "a3b": dict(id="a3b", parent="u2", children=[], message=dict(id="a3b", author=dict(role="assistant"),
        content=dict(content_type="text", parts=["Alternate branch."]), recipient="all", metadata={}))}})
    case("capture: ambiguous active branch is an error, not an empty 'ok'", rc3 != 0, se3)
    rc4, so4, _, _ = render("no-leaf-single", chatgpt_fixture(leaf=False))
    case("capture: missing current_node with one leaf still renders", rc4 == 0 and '"visible_messages": 5' in so4)

    ssd = X / "selective-self-distillation" / "01_extract_turns.py"
    o = TMP / "ssd-json.json"
    rc, so, se = run(ssd, cap_json, "-o", o)
    recs = json.loads(o.read_text(encoding="utf-8"))
    case("ssd: reads chat-capture normalized JSON", rc == 0 and len(recs) == 2, so + se)
    o = TMP / "ssd-md.json"
    run(ssd, cap_md, "-o", o)
    recs = json.loads(o.read_text(encoding="utf-8"))
    case("ssd: Markdown own-text has no capture comments or separators",
         len(recs) == 2 and all("<!--" not in r["own"] and not r["own"].rstrip().endswith("---") for r in recs),
         str([r["own"] for r in recs]))
    bad = TMP / "unsupported.json"; bad.write_text(json.dumps({"hello": 1}), encoding="utf-8")
    rc, so, se = run(ssd, bad, "-o", TMP / "ssd-bad.json")
    case("ssd: unsupported JSON fails loudly", rc != 0 and "SKIPPED" in se, se)
    relay_md = TMP / "relay.md"
    relay_md.write_text("### You said:\nFROM: Claude\nPlease implement this proposed change.\n\n### ChatGPT said:\nOK\n", encoding="utf-8")
    o = TMP / "ssd-relay.json"; run(ssd, relay_md, "-o", o)
    case("ssd: short relay without a paste delimiter is flagged", "relayed_from_model" in json.loads(o.read_text(encoding="utf-8"))[0]["flags"])
    mixed = TMP / "mixed.md"
    mixed.write_text("### You said:\nCompare this with our plan.\nFROM: Ada (ChatGPT)\nKate's long answer here.\nEND FROM\nThen tell me which wins.\n", encoding="utf-8")
    o = TMP / "ssd-mixed.json"; run(ssd, mixed, "-o", o)
    r = json.loads(o.read_text(encoding="utf-8"))[0]
    case("ssd: a FROM ... END FROM block after your words is stripped from own text",
         "Ada's long answer" not in r["own"] and "Compare this" in r["own"] and "which wins" in r["own"]
         and "relayed_from_model" not in r["flags"], r["own"])
    named = TMP / "named.md"
    named.write_text("### Sam said:\nMy own idea.\n\n### ChatGPT said:\nA reply.\n\n### Sam said:\nAda here, relaying.\n", encoding="utf-8")
    o = TMP / "ssd-named.json"; run(ssd, named, "-o", o, "--user-names", "Sam", "--assistant-names", "Ada")
    recs = json.loads(o.read_text(encoding="utf-8"))
    case("ssd: --user-names and --assistant-names configure who is who",
         [r["own"] for r in recs] == ["My own idea.", "Ada here, relaying."] and "relayed_from_model" in recs[1]["flags"], str(recs))
    smart = TMP / "smart.md"
    smart.write_text("### You said:\nMy note.\n>“””\npasted from phone\n“””\n", encoding="utf-8")
    o = TMP / "ssd-smart.json"; run(ssd, smart, "-o", o)
    case("ssd: smart-quoted >\"\"\" paste delimiter still works", "pasted from phone" not in json.loads(o.read_text(encoding="utf-8"))[0]["own"])
    nonascii = TMP / "na.md"; nonascii.write_text("### You said:\nCafé — naïve “quotes”\n", encoding="utf-8")
    o = TMP / "ssd-na.json"; run(ssd, nonascii, "-o", o, env={"PYTHONUTF8": "0"})
    try:
        ok = "Café" in json.loads(o.read_bytes().decode("utf-8"))[0]["own"]
    except Exception:
        ok = False
    src = ssd.read_text(encoding="utf-8")
    # This host's locale is UTF-8, so the run alone can't show a CP1252 failure; also check the source.
    case("ssd: output written as UTF-8 (run + source check)", ok and "open(a.o, 'w', encoding='utf-8')" in src)


def live_fixture_tests():
    """A real ChatGPT capture from the 2026-10-28 live check (redacted)."""
    f = KIT / "tests" / "fixtures" / "live" / "chatgpt-live-2026-10-28.raw.json"
    out = TMP / "live"
    rc, so, se = run(X / "chat-capture" / "03_render.py", f, "-o", out)
    j = json.loads(next(out.glob("*.json")).read_text(encoding="utf-8")) if rc == 0 else {}
    ok = (rc == 0 and [t["role"] for t in j["turns"]] == ["user", "assistant"]
          and j["turns"][1]["messages"][0]["text"] == "RELAY-OK 1"
          and j["meta"]["models_seen"] == ["gpt-6-thinking"] and j["hidden_counts"] == {"assistant:reasoning_recap": 1})
    case("live capture: renders the real ChatGPT conversation", ok, se or so)


def schema_tests():
    """One conversation format, relay-kit/conversation@1, with one message id per message."""
    rc, so, se, out = render("schema", wrap(chatgpt_fixture()))
    cap = next(out.glob("*.json")); md = next(out.glob("*.md"))
    j = json.loads(cap.read_text(encoding="utf-8"))
    mids = [m["mid"] for tt in j["turns"] for m in tt["messages"]]
    case("schema: renderer tags the version and gives every message a unique mid",
         j.get("schema") == "relay-kit/conversation@1" and mids == ["T1u", "T1a", "T1a2", "T2u", "T2a"], str(mids))

    d = chatgpt_fixture()   # a conversation that opens with the assistant
    mp = d["mapping"]
    greet = dict(id="g0", parent=None, children=["u1"], message=dict(id="g0", author=dict(role="assistant"),
                 content=dict(content_type="text", parts=["Hi, how can I help?"]), recipient="all", metadata={}, create_time=1793195990))
    mp["u1"]["parent"] = "g0"; mp["g0"] = greet
    rc, so, se, out2 = render("lead-assistant", wrap(d))
    j2 = json.loads(next(out2.glob("*.json")).read_text(encoding="utf-8"))
    m2 = [m["mid"] for tt in j2["turns"] for m in tt["messages"]]
    case("schema: a reply before the first user turn is T0a, ids stay unique",
         m2[0] == "T0a" and len(set(m2)) == len(m2), str(m2))
    md2 = next(out2.glob("*.md"))
    got = [(seed_mod.mid(m), m["role"]) for m in seed_mod.load(str(md2))] if (seed_mod := load("seed_lead", X / "thread-seed" / "01_seed_index.py")) else []
    case("schema: Markdown of an assistant-first conversation keeps ids and roles",
         got == [("T0a", "assistant"), ("T1u", "user"), ("T1a", "assistant"), ("T1a2", "assistant"), ("T2u", "user"), ("T2a", "assistant")], str(got))

    seed = load("seed_index_schema", X / "thread-seed" / "01_seed_index.py")
    case("schema: thread-seed's ids match the capture's, from JSON and Markdown",
         [seed.mid(m) for m in seed.load(str(cap))] == mids == [seed.mid(m) for m in seed.load(str(md))])

    old = TMP / "old-markers.md"
    old.write_text("# Old\n\n### You said:\n<!-- T1 · 2026-10-27 09:00:00 CDT -->\n\nHi\n\n---\n\n"
                   "### ChatGPT said:\n<!-- T1 · 2026-10-27 09:00:05 CDT · 2 messages -->\n\nOne\n\n"
                   "<!-- 2026-10-27 09:00:09 CDT -->\n\nTwo\n\n---\n", encoding="utf-8")
    case("schema: captures written before the schema still parse",
         [seed.mid(m) for m in seed.load(str(old))] == ["T1u", "T1a", "T1a2"])

    ssd = X / "selective-self-distillation" / "01_extract_turns.py"
    o = TMP / "ssd-schema.json"; run(ssd, cap, "-o", o)
    case("schema: selective-self-distillation locates turns by mid",
         [r["loc"] for r in json.loads(o.read_text(encoding="utf-8"))] == ["T1u", "T2u"])

    tel = X / "chat-telemetry" / "01_chat_telemetry.py"
    rc, so, se = run(tel, cap, "-o", TMP / "tel-schema")
    th = json.loads((TMP / "tel-schema" / "telemetry.json").read_text(encoding="utf-8"))["threads"][0] if rc == 0 else {}
    case("schema: chat-telemetry reads a normalized capture and says what it can't measure",
         rc == 0 and th["summary"]["turns"] == 2 and any("normalized" in c for c in th["checks"]), se)

    bad = dict(j, schema="relay-kit/conversation@2")
    bp = TMP / "future.json"; bp.write_text(json.dumps(bad), encoding="utf-8")
    r_seed = run(X / "thread-seed" / "01_seed_index.py", bp)
    r_ssd = run(ssd, bp, "-o", TMP / "ssd-future.json")
    r_tel = run(tel, bp, "-o", TMP / "tel-future")
    case("schema: an unknown version is refused by seed, SSD and telemetry",
         r_seed[0] != 0 and r_ssd[0] != 0 and r_tel[0] != 0, str([r_seed[2][-80:], r_ssd[2][-80:], r_tel[2][-80:]]))


def governance_tests():
    """The architecture's own rules: contracts, invariants, ledgers."""
    test_src = "".join((KIT / "tests" / f).read_text(encoding="utf-8") for f in ("run_tests.py", "browser_tests.cjs", "test_export.py", "test_cockpit.py", "test_mcp.py", "test_prs.py", "test_lifecycle.py", "test_github.py", "test_budget.py", "test_usage.py", "test_telemetry.py", "test_comments.py"))
    def test_exists(name):
        tail = name.split(": ", 1)[-1]
        return tail.replace('\\"', '"') in test_src.replace('\\"', '"')

    reg = json.loads((KIT / "governance" / "contracts.json").read_text(encoding="utf-8"))
    missing = []
    for c in reg["contracts"]:
        for f in [c["spec"]] + c["implementers"]:
            path = KIT / f
            if not path.exists():
                missing.append(f"{c['id']}: {f} missing")
            elif c["id"] not in path.read_text(encoding="utf-8"):
                missing.append(f"{c['id']}: {f} doesn't name this version")
        missing += [f"{c['id']}: no test '{n}'" for n in c["tests"] if not test_exists(n)]
    case("governance: every contract's spec and implementers carry its version; its tests exist",
         not missing, "; ".join(missing))

    purpose = (KIT / "governance" / "purpose.md").read_text(encoding="utf-8")
    sections = re.split(r"^## ", purpose, flags=re.M)[1:]
    pids = [s.split()[0] for s in sections]
    bad = [s.split()[0] for s in sections if "- Enforced by:" not in s]
    cited = re.findall(r"`([a-z]+(?: \([^)]*\))?: [^`]+)`", purpose)
    gone = [n for n in cited if not test_exists(n)]
    case("governance: every invariant states its enforcement and cites real tests",
         pids and not bad and not gone, f"no enforcement: {bad}; missing tests: {gone}")
    arch = (KIT / "ARCHITECTURE.md").read_text(encoding="utf-8")
    case("governance: ARCHITECTURE.md lists exactly the invariants in purpose.md",
         re.findall(r"^\| (P-\d\d) \|", arch, re.M) == pids, str(pids))

    chk = KIT / "tools" / "check_ledgers.py"
    g = TMP / "gov-ok"; g.mkdir()
    dec = [dict(id="DEC-001", date="2026-10-28", status="adopted", decision="d", scope="s", decider="owner", basis='"yes"', locator="chat"),
           dict(id="DEC-002", date="2026-10-28", status="proposed", decision="d2", scope="s")]
    ch = [dict(id="CH-001", date="2026-10-28", status="open", concern="c", raised_by="x")]
    (g / "decisions.jsonl").write_text("".join(json.dumps(d) + "\n" for d in dec), encoding="utf-8")
    (g / "challenges.jsonl").write_text("".join(json.dumps(c) + "\n" for c in ch), encoding="utf-8")
    case("ledgers: a valid decision and challenge ledger passes", run(chk, "--dir", g)[0] == 0)
    b = TMP / "gov-bad"; b.mkdir()
    bad_dec = [dict(id="DEC-001", date="2026-10-28", status="adopted", decision="d", scope="s"),
               dict(id="DEC-003", date="28 Oct", status="maybe", decision="d", scope="s")]
    (b / "decisions.jsonl").write_text("".join(json.dumps(d) + "\n" for d in bad_dec), encoding="utf-8")
    (b / "challenges.jsonl").write_text(json.dumps(dict(id="CH-001", date="2026-10-28", status="resolved", concern="c", raised_by="x")) + "\n", encoding="utf-8")
    rc, so, _ = run(chk, "--dir", b)
    case("ledgers: missing basis, skipped id, bad date/status and unexplained resolution are all caught",
         rc != 0 and all(k in so for k in ("'basis' is required", "expected DEC-002", "YYYY-MM-DD", "status must be", "'resolution' is required")), so)
    gi = TMP / "gov-git"; gi.mkdir()
    (gi / "decisions.jsonl").write_text(json.dumps(dec[0]) + "\n", encoding="utf-8")
    git = lambda *a: subprocess.run(["git", "-C", str(gi), *a], capture_output=True, text=True)
    git("init", "-q"); git("add", "."); git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x")
    (gi / "decisions.jsonl").write_text(json.dumps(dict(dec[0], decision="rewritten")) + "\n", encoding="utf-8")
    rc, so, _ = run(chk, "--dir", gi, "--git")
    (gi / "decisions.jsonl").write_text(json.dumps(dec[0]) + "\n" + json.dumps(dict(dec[1], recorded_by="sam")) + "\n", encoding="utf-8")
    rc2, so2, _ = run(chk, "--dir", gi, "--git")
    case("ledgers: --git catches an edited earlier line and allows appending", rc != 0 and "append-only" in so and rc2 == 0, so + so2)

    d = ledger_dir("newer-contract")
    p = d / FIXTURE.name
    p.write_text(p.read_text(encoding="utf-8").replace("status: prepared\n", "status: prepared\ncontract: relay-kit/handoff@3\n"), encoding="utf-8")
    case("ledger: a handoff from a newer contract version is refused",
         run(LEDGER, "--dir", d, "check")[0] != 0 and setstate(d, "sent", "claude:relay-kit", "x")[0] != 0)


# ---------------- status (starting back up) ----------------
def status_tests():
    st = KIT / "tools" / "status.py"
    r = TMP / "status-root"
    d = ledger_dir("status")                      # fixture: prepared, Authorized filled
    shutil.copytree(d, r / "handoffs")
    fx = FIXTURE.read_text(encoding="utf-8")
    unauth = re.sub(r"(## Authorized[ \t]*\n).*?(?=^## )", r"\1TODO: NOT AUTHORIZED YET.\n\n", fx.replace(HID, "HO-20261028-002"), flags=re.S | re.M)
    (r / "handoffs" / "HO-20261028-002.md").write_text(unauth, encoding="utf-8")
    (r / "handoffs" / "outputs" / f"{HID}.md").write_text("result\n", encoding="utf-8")
    (r / "governance").mkdir()
    rows = [dict(id="DEC-001", date="2026-10-28", status="proposed", decision="old idea", scope="s"),
            dict(id="DEC-002", date="2026-10-29", status="adopted", decision="replaces old idea", scope="s", supersedes="DEC-001",
                 decider="owner", basis='"yes"', locator="chat"),
            dict(id="DEC-003", date="2026-10-30", status="proposed", decision="pending idea", scope="s")]
    (r / "governance" / "decisions.jsonl").write_text("".join(json.dumps(x) + "\n" for x in rows), encoding="utf-8")
    chs = [dict(id="CH-001", date="2026-10-29", status="open", concern="first worry", raised_by="x"),
           dict(id="CH-002", date="2026-10-30", status="resolved", concern="first worry fixed", raised_by="x", resolution="fixed", resolves="CH-001"),
           dict(id="CH-003", date="2026-10-30", status="open", concern="second worry", raised_by="x")]
    (r / "governance" / "challenges.jsonl").write_text("".join(json.dumps(x) + "\n" for x in chs), encoding="utf-8")
    bad = TMP / "gov-bad-resolves"; bad.mkdir()
    (bad / "challenges.jsonl").write_text(json.dumps(dict(chs[1], id="CH-001", resolves="CH-009")) + "\n", encoding="utf-8")
    chk = KIT / "tools" / "check_ledgers.py"
    case("ledgers: 'resolves' must name an earlier challenge",
         run(chk, "--dir", r / "governance")[0] == 0 and run(chk, "--dir", bad)[0] != 0)
    before = {p: p.read_bytes() for p in r.rglob("*") if p.is_file()}
    rc, out, err = run(st, "--root", r, "--no-fetch")
    waiting = out.split("== Waiting on the owner")[1].split("==")[0] if "== Waiting on the owner" in out else ""
    case("status: lists unsuperseded proposals and unauthorized handoffs as waiting on the owner",
         rc == 0 and "DEC-003" in waiting and "DEC-001" not in waiting and "HO-20261028-002" in waiting and HID not in waiting, out + err)
    case("status: flags a handoff whose output is filed but whose state was never closed",
         re.search(rf"{HID} .*OUTPUT FILED, state not closed", out) is not None, out)
    open_part = out.split("== Open challenges")[1].split("==")[0] if "== Open challenges" in out else ""
    case("status: a challenge closed by a later 'resolves' line isn't listed as open",
         "CH-003" in open_part and "CH-001" not in open_part, open_part)
    case("status: says what it couldn't check (no git, no index) and changes nothing",
         all(k in out for k in ("not a git repository", "threads.md"))
         and before == {p: p.read_bytes() for p in r.rglob("*") if p.is_file()}, out)
    sent = setstate(r / "handoffs", "sent", "claude:relay-kit", "delivered")
    rc1, fresh, _ = run(st, "--root", r, "--no-fetch")
    rc2, late, _ = run(st, "--root", r, "--no-fetch", "--stale-hours", "-1")
    case("status: a sent handoff with no acknowledgement is flagged only after the timeout",
         sent[0] == 0 and "NO ACK" not in fresh and re.search(rf"{HID} +sent .*NO ACK for", late) is not None, sent[2] + late)

    g = lambda cwd, *a: subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                        capture_output=True, text=True)
    remote, c1, c2 = TMP / "st-remote.git", TMP / "st-c1", TMP / "st-c2"
    g(TMP, "init", "-q", "--bare", "-b", "main", str(remote))
    g(TMP, "clone", "-q", str(remote), str(c1))
    g(c1, "checkout", "-q", "-b", "main"); (c1 / "threads.md").write_text("# index\n- 2026-10-31 | x | created\n", encoding="utf-8")
    g(c1, "add", "."); g(c1, "commit", "-qm", "a"); g(c1, "push", "-q", "-u", "origin", "main")
    g(TMP, "clone", "-q", str(remote), str(c2))
    (c1 / "threads.md").write_text("# index\n- 2026-10-31 | x | created\n- 2026-10-31 | y | moved\n", encoding="utf-8")
    g(c1, "commit", "-qam", "b"); g(c1, "push", "-q")
    rc, out, err = run(st, "--root", c2)
    case("status: a copy behind the shared one is told to pull first",
         rc == 0 and "1 behind" in out and "PULL FIRST" in out and "last change: - 2026-10-31 | x | created" in out, out + err)


# ---------------- coordination invariants (HO-20261101-005) ----------------
# The reviewer's replayable cases from dogfood-001: A shared handoff id, B independent work, C shared
# interface across files (item 5, scoped only: no test), D expired lease, E red CI. Each item
# has a violation and a decoy (a legitimate action the guard must not block).
H2 = "HO-RK-20261028-K7QF"


def fixture2(d, hid=H2):
    """The frozen fixture as a handoff@2 file: lane-coded id, contract, sessions on the actor."""
    t = FIXTURE.read_text(encoding="utf-8").replace(HID, hid).replace("status: prepared\n", "status: prepared\ncontract: relay-kit/handoff@2\n")
    t = re.sub(r"(\| prepared \| )claude:relay-kit( \|)", r"\1claude:relay-kit#s0\2", t)
    (d / f"{hid}.md").write_text(t, encoding="utf-8")
    return d / f"{hid}.md"


def git_world(name):
    """A bare 'shared copy' and two clones with a handoff folder, like two sessions' checkouts."""
    origin, c1, c2 = TMP / f"{name}.git", TMP / f"{name}-1", TMP / f"{name}-2"
    g = lambda cwd, *a: subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                       capture_output=True, text=True)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], capture_output=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(c1)], capture_output=True)
    (c1 / "handoffs" / "outputs").mkdir(parents=True)
    (c1 / "handoffs" / ".ledger.json").write_text('{"owner": "sam", "lanes": {"relay-kit": "RK"}}', encoding="utf-8")
    (c1 / "handoffs" / "outputs" / ".keep").write_text("")
    g(c1, "checkout", "-q", "-b", "main"); g(c1, "add", "."); g(c1, "commit", "-qm", "init"); g(c1, "push", "-q", "-u", "origin", "main")
    subprocess.run(["git", "clone", "-q", str(origin), str(c2)], capture_output=True)
    return g, c1, c2


def coordination_tests():
    # item 1: identity. A lane isn't an actor; the owner needs no session.
    d = TMP / "c-identity"; (d / "outputs").mkdir(parents=True)
    (d / ".ledger.json").write_text('{"owner": "sam"}', encoding="utf-8")
    fixture2(d)
    r_lane = setstate(d, "sent", "claude:relay-kit", "x", hid=H2)
    r_sess = setstate(d, "sent", "claude:relay-kit#s1", "x", hid=H2)
    r_own = setstate(d, "acknowledged", "sam", "read", hid=H2)
    case("ledger: @2 needs a session on every actor; the owner needs none",
         r_lane[0] != 0 and "session" in r_lane[2] and r_sess[0] == 0 and r_own[0] == 0
         and run(LEDGER, "--dir", d, "check")[0] == 0, r_lane[2] + r_sess[2] + r_own[2])
    st = load("status", KIT / "tools" / "status.py")
    short = st.short_actors(["a claude:aec#01GZK2Kne8fYjfQ x", "b claude:relay-kit#01GZK2KmPhikGJM x", "c claude:aec#01N4bRwz x"])
    case("identity: sessions with near-identical ids stay apart when shortened",
         short == ["a claude:aec#01GZK2Kn x", "b claude:relay-kit#01GZK2Km x", "c claude:aec#01N4 x"], str(short))
    d = TMP / "c-new-by"
    rc, _, err = run(LEDGER, "--dir", d, "new", "--from", "claude:aec", "--by", "claude:aec", "--to", "codex:aec", "--title", "T")
    rc2, hid, _ = run(LEDGER, "--dir", d, "new", "--from", "claude:aec", "--to", "codex:aec", "--title", "T", env={"RELAYKIT_ACTOR": "claude:aec#01N4"})
    case("identity: new refuses a lane as the drafter and takes the session from RELAYKIT_ACTOR",
         rc != 0 and rc2 == 0 and f"| prepared | claude:aec#01N4 |" in (d / f"{hid}.md").read_text(encoding="utf-8"), err)

    # item 6: recording a step for someone else (the skill/checker conflict in CH-012)
    d = TMP / "c-for"; (d / "outputs").mkdir(parents=True)
    (d / ".ledger.json").write_text('{"owner": "sam"}', encoding="utf-8")
    fixture2(d)
    setstate(d, "sent", "claude:relay-kit#s1", "pasted", hid=H2)
    bare = run(LEDGER, "--dir", d, "set", H2, "acknowledged", "--by", "claude:relay-kit#s1", "--note", "from its reply")
    nonote = run(LEDGER, "--dir", d, "set", H2, "acknowledged", "--by", "claude:relay-kit#s1", "--for", "chatgpt:ada")
    wrong = run(LEDGER, "--dir", d, "set", H2, "acknowledged", "--by", "claude:relay-kit#s1", "--for", "chatgpt:bob", "--note", "x")
    ok = run(LEDGER, "--dir", d, "set", H2, "acknowledged", "--by", "claude:relay-kit#s1", "--for", "chatgpt:ada", "--note", "from its reply, chat T4a")
    (d / "outputs" / f"{H2}.md").write_text("result\n", encoding="utf-8")
    done = run(LEDGER, "--dir", d, "set", H2, "completed", "--by", "claude:relay-kit#s1", "--for", "chatgpt:ada", "--note", "outputs/" + H2 + ".md, filed from its reply")
    txt = (d / f"{H2}.md").read_text(encoding="utf-8")
    case("for: a recipient step recorded by someone else needs --for the recipient and a source note",
         bare[0] != 0 and nonote[0] != 0 and wrong[0] != 0 and ok[0] == 0 and done[0] == 0
         and "| acknowledged | claude:relay-kit#s1 for chatgpt:ada |" in txt and run(LEDGER, "--dir", d, "check")[0] == 0,
         " / ".join(x[2] for x in (bare, nonote, wrong, ok, done)))

    # item 3: protected writes
    d = TMP / "c-expect"; (d / "outputs").mkdir(parents=True)
    (d / ".ledger.json").write_text('{"owner": "sam"}', encoding="utf-8")
    fixture2(d)
    stale = run(LEDGER, "--dir", d, "set", H2, "sent", "--by", "claude:relay-kit#s1", "--expect", "sent", "--note", "x")
    fresh = run(LEDGER, "--dir", d, "set", H2, "sent", "--by", "claude:relay-kit#s1", "--expect", "prepared", "--note", "x")
    case("protect: --expect refuses a write against a state that moved, and allows the right one",
         stale[0] != 0 and "expected" in stale[2] and fresh[0] == 0, stale[2] + fresh[2])

    g, c1, c2 = git_world("c-history")
    f = fixture2(c1 / "handoffs")
    g(c1, "add", "."); g(c1, "commit", "-qm", "h"); base = g(c1, "rev-parse", "HEAD").stdout.strip()
    setstate(c1 / "handoffs", "sent", "claude:relay-kit#s1", "appended", hid=H2)
    rc_append, so_append, _ = run(LEDGER, "--dir", c1 / "handoffs", "check", "--base", base)
    good = f.read_text(encoding="utf-8")
    f.write_text(good.replace("to: chatgpt:ada", "to: codex:other").replace("| drafted", "| drafted (edited)"), encoding="utf-8")
    rc_edit, so_edit, _ = run(LEDGER, "--dir", c1 / "handoffs", "check", "--base", base)
    f.write_text(good, encoding="utf-8")
    f.rename(f.with_name("HO-RK-20261028-ZZZZ.md"))
    rc_rm, so_rm, _ = run(LEDGER, "--dir", c1 / "handoffs", "check", "--base", base)
    case("protect: check --base allows appending, refuses a rewritten header or log and a removed handoff",
         rc_append == 0 and rc_edit != 0 and "'to' changed" in so_edit and "earlier log lines" in so_edit
         and rc_rm != 0 and f"{H2}.md: removed" in so_rm, so_append + so_edit + so_rm)

    chk = KIT / "tools" / "check_ledgers.py"
    gl = TMP / "c-ledgers"; gl.mkdir()
    gg = lambda *a: subprocess.run(["git", "-C", str(gl), "-c", "user.name=t", "-c", "user.email=t@t", *a], capture_output=True, text=True)
    row = lambda i, who: json.dumps(dict(id=f"DEC-00{i}", date="2026-11-01", status="proposed", decision="d", scope="s", recorded_by=who)) + "\n"
    (gl / "decisions.jsonl").write_text(row(1, "claude:aec"), encoding="utf-8")
    gg("init", "-q"); gg("add", "."); gg("commit", "-qm", "x")
    (gl / "decisions.jsonl").write_text(row(1, "claude:aec") + row(2, "sam") + row(3, "claude:aec#01N4"), encoding="utf-8")
    rc_ok, so_ok, _ = run(chk, "--dir", gl, "--git")
    (gl / "decisions.jsonl").write_text(row(1, "claude:aec") + row(2, "claude:aec"), encoding="utf-8")
    rc_lane, so_lane, _ = run(chk, "--dir", gl, "--git")
    for who in ("", "claude:aec#"):
        (gl / "decisions.jsonl").write_text(row(1, "claude:aec") + row(2, who), encoding="utf-8")
        rc_x, so_x, _ = run(chk, "--dir", gl, "--git")
        rc_lane, so_lane = (rc_lane if rc_x else 0), so_lane + so_x
    (gl / "decisions.jsonl").write_text(row(1, "claude:aec"), encoding="utf-8")
    (gl / ".gitattributes").write_text("*.jsonl merge=union\n", encoding="utf-8")
    rc_union, so_union, _ = run(chk, "--dir", gl, "--git")
    case("protect: new ledger lines need a recorder with a real session (old lines and the owner are fine); merge=union is refused",
         rc_ok == 0 and rc_lane != 0 and "names a lane" in so_lane and rc_union != 0 and "merge=union" in so_union,
         so_ok + so_lane + so_union)

    # A: shared handoff id. Online, new skips an id another clone pushed; offline, git conflicts.
    g, c1, c2 = git_world("c-A")
    env = {"RELAYKIT_ACTOR": "claude:relay-kit#s1", "RELAYKIT_TEST_SUFFIXES": "AAAA,BBBB"}
    r1 = run(LEDGER, "--dir", c1 / "handoffs", "new", "--from", "claude:relay-kit", "--to", "codex:x", "--title", "one", env=env)
    g(c1, "add", "."); g(c1, "commit", "-qm", "one"); g(c1, "push", "-q")
    r2 = run(LEDGER, "--dir", c2 / "handoffs", "new", "--from", "claude:relay-kit", "--to", "codex:x", "--title", "two",
             env=dict(env, RELAYKIT_ACTOR="claude:relay-kit#s2"))
    online = r1[1].endswith("-AAAA") and r2[1].endswith("-BBBB")
    g3, d1, d2 = git_world("c-A-offline")
    for c, t in ((d1, "one"), (d2, "two")):
        run(LEDGER, "--dir", c / "handoffs", "new", "--from", "claude:relay-kit", "--to", "codex:x", "--title", t,
            env=dict(env, RELAYKIT_TEST_SUFFIXES="AAAA"))
        g3(c, "add", "."); g3(c, "commit", "-qm", t)
    g3(d1, "push", "-q")
    merged = g3(d2, "pull", "-q", "--no-rebase", "origin", "main")
    case("case A (shared id): online, the second clone skips the pushed id; offline, the clash is a git conflict, never a silent merge",
         online and merged.returncode != 0 and "CONFLICT" in (merged.stdout + merged.stderr), f"{r1[1]} {r2[1]} {merged.stdout}{merged.stderr}")

    # B: independent work in one lane: two sessions, two handoffs, no refusals (no false blocking).
    g, c1, c2 = git_world("c-B")
    h1 = run(LEDGER, "--dir", c1 / "handoffs", "new", "--from", "claude:relay-kit", "--to", "codex:x", "--title", "one",
             env={"RELAYKIT_ACTOR": "claude:relay-kit#s1"})[1]
    g(c1, "add", "."); g(c1, "commit", "-qm", "one"); g(c1, "push", "-q")
    h2 = run(LEDGER, "--dir", c2 / "handoffs", "new", "--from", "claude:relay-kit", "--to", "codex:x", "--title", "two",
             env={"RELAYKIT_ACTOR": "claude:relay-kit#s2"})[1]
    g(c2, "add", "."); g(c2, "commit", "-qm", "two"); g(c2, "pull", "-q", "--rebase", "origin", "main"); g(c2, "push", "-q")
    b1 = run(LEDGER, "--dir", c1 / "handoffs", "set", h1, "blocked", "--by", "claude:relay-kit#s1", "--note", "waiting")
    b2 = run(LEDGER, "--dir", c2 / "handoffs", "set", h2, "blocked", "--by", "claude:relay-kit#s2", "--note", "waiting")
    case("case B (independent work): two sessions in one lane, each on its own handoff, are never refused",
         h1 != h2 and b1[0] == 0 and b2[0] == 0, b1[2] + b2[2])

    # D: expired lease. s1 claimed, went quiet; the owner reassigned on the shared copy; s1's stale write is refused.
    g, c1, c2 = git_world("c-D")
    fixture2(c1 / "handoffs")
    for st_, who in (("sent", "claude:relay-kit#s0"), ("acknowledged", "chatgpt:ada#s1")):
        setstate(c1 / "handoffs", st_, who, "x", hid=H2)
    g(c1, "add", "."); g(c1, "commit", "-qm", "claimed"); g(c1, "push", "-q")
    g(c2, "pull", "-q")
    own = run(LEDGER, "--dir", c2 / "handoffs", "set", H2, "blocked", "--by", "sam", "--expect", "acknowledged", "--note", "lease expired; reassigning")
    g(c2, "commit", "-qam", "owner"); g(c2, "push", "-q")
    late = setstate(c1 / "handoffs", "in_progress", "chatgpt:ada#s1", "back", hid=H2)
    g(c1, "pull", "-q")
    after = run(LEDGER, "--dir", c1 / "handoffs", "set", H2, "in_progress", "--by", "chatgpt:ada#s1", "--expect", "acknowledged", "--note", "back")
    case("case D (expired lease): a stale writer is refused by the shared-copy check, and --expect catches it after a pull",
         own[0] == 0 and late[0] != 0 and "shared copy" in late[2] and after[0] != 0 and "expected" in after[2],
         own[2] + late[2] + after[2])

    # E: red CI on the shared branch is shown, and unacknowledged red reaches "Waiting on the owner".
    stub = TMP / "gh-stub.py"
    stub.write_text("import json, os, sys\nd = json.load(open(os.environ['GH_STUB_DATA']))\n"
                    "path = sys.argv[2]\nfor k in sorted(d, key=len, reverse=True):\n    if path.startswith(k):\n        print(json.dumps(d[k])); sys.exit(0)\n"
                    "sys.exit('no stub for ' + path)\n", encoding="utf-8")
    gh = TMP / "gh"
    gh.write_text(f"#!/bin/sh\nexec {sys.executable} {stub} \"$@\"\n", encoding="utf-8"); gh.chmod(0o755)
    g, c1, _ = git_world("c-E")
    g(c1, "remote", "set-url", "origin", "https://github.com/o/r.git")
    run_ = lambda sha, c, wf="records", t="2026-11-01T17:24:00Z": dict(status="completed", conclusion=c, head_sha=sha, created_at=t, name=wf)
    issue = dict(number=7, title="Records check failing on main")
    base = {"repos/o/r/issues/7/comments": [dict(user=dict(login="github-actions[bot]", type="Bot"), created_at="2026-11-01T17:25:00Z")],
            "repos/o/r/issues": [issue], "repos/o/r": dict(default_branch="main")}
    worlds = {
        "red": dict(base, **{"repos/o/r/actions": dict(workflow_runs=[run_("ccc", "failure"), run_("bbb", "failure"), run_("aaa", "success")])}),
        "acked": dict(base, **{"repos/o/r/actions": dict(workflow_runs=[run_("ccc", "failure")]),
                               "repos/o/r/issues/7/comments": [dict(user=dict(login="ada", type="User"), created_at="2026-11-01T18:00:00Z")]}),
        "old-ack": dict(base, **{"repos/o/r/actions": dict(workflow_runs=[run_("ccc", "failure")]),
                                 "repos/o/r/issues/7/comments": [dict(user=dict(login="ada", type="User"), created_at="2026-10-01T09:00:00Z")]}),
        "other-green": dict(base, **{"repos/o/r/actions": dict(workflow_runs=[run_("ccc", "success", "pages"), run_("ccc", "failure", "records")])}),
        "cancelled": {"repos/o/r/actions": dict(workflow_runs=[run_("eee", "cancelled"), run_("ddd", "success")]),
                      "repos/o/r/issues": [], "repos/o/r": dict(default_branch="main")},
        "green": {"repos/o/r/actions": dict(workflow_runs=[run_("ddd", "success")]), "repos/o/r/issues": [],
                  "repos/o/r": dict(default_branch="main")}}
    outs = {}
    for k, w in worlds.items():
        (TMP / f"gh-{k}.json").write_text(json.dumps(w), encoding="utf-8")
        outs[k] = run(KIT / "tools" / "status.py", "--root", c1, "--no-fetch",
                      env={"RELAYKIT_GH": str(gh), "GH_STUB_DATA": str(TMP / f"gh-{k}.json")})[1]
    wait = lambda o: o.split("== Waiting on the owner")[1].split("==")[0] if "== Waiting on the owner" in o else ""
    alarm = lambda k: "nobody has acknowledged" in wait(outs[k])
    case("case E (red CI): status shows the failing streak and the unacknowledged alert at the top; acked or green raises nothing",
         "records: RED, 2 failing run(s) in a row since bbb" in outs["red"] and "NOT ACKNOWLEDGED" in outs["red"] and alarm("red")
         and "acknowledged by ada" in outs["acked"] and not alarm("acked")
         and "all green" in outs["green"] and not alarm("green"), "\n---\n".join(outs.values()))
    case("case E: each workflow is its own check; a cancelled run isn't red; an acknowledgement from an earlier incident doesn't count",
         "records: RED" in outs["other-green"] and "pages: green" in outs["other-green"] and alarm("other-green")
         and "all green" in outs["cancelled"] and not alarm("cancelled")
         and alarm("old-ack") and "acknowledged by" not in outs["old-ack"], "\n---\n".join(outs[k] for k in ("other-green", "cancelled", "old-ack")))

    # Fresh-context review of PR #5: owner powers through --for; @1 files; branch and squash cases for the shared copy.
    d = TMP / "c-for-owner"; (d / "outputs").mkdir(parents=True)
    (d / ".ledger.json").write_text('{"owner": "sam"}', encoding="utf-8")
    fixture2(d)
    as_owner = run(LEDGER, "--dir", d, "set", H2, "sent", "--by", "mallory:x#1", "--for", "sam", "--note", "hi")
    inline = run(LEDGER, "--dir", d, "set", H2, "cancelled", "--by", "mallory:x#1 for sam", "--note", "hi")
    cancel_for = run(LEDGER, "--dir", d, "set", H2, "blocked", "--by", "mallory:x#1", "--for", "claude:relay-kit", "--note", "hi")
    sess_party = run(LEDGER, "--dir", d, "set", H2, "sent", "--by", "zed:carrier#9", "--for", "claude:relay-kit#s0", "--note", "pasted, chat T2u")
    case("for: nobody acts as the owner or records blocked/cancelled for someone; a party may carry its session",
         as_owner[0] != 0 and inline[0] != 0 and cancel_for[0] != 0 and sess_party[0] == 0, as_owner[2] + inline[2] + cancel_for[2] + sess_party[2])
    d = ledger_dir("v1-syntax")
    v1a = setstate(d, "sent", "claude:relay-kit#s1", "x")
    v1b = setstate(d, "sent", "claude:relay-kit", "x")
    case("compat: an @1 file refuses @2 actor syntax, so @1 readers can still read it",
         v1a[0] != 0 and "handoff@1" in v1a[2] and v1b[0] == 0, v1a[2] + v1b[2])

    g, c1, c2 = git_world("c-branches")
    fixture2(c1 / "handoffs")
    g(c1, "add", "."); g(c1, "commit", "-qm", "h"); g(c1, "push", "-q")
    g(c2, "pull", "-q"); g(c2, "checkout", "-q", "-b", "feat"); g(c2, "push", "-q", "-u", "origin", "feat")
    setstate(c1 / "handoffs", "sent", "claude:relay-kit#s0", "x", hid=H2); g(c1, "commit", "-qam", "sent"); g(c1, "push", "-q")
    feat = setstate(c2 / "handoffs", "blocked", "claude:relay-kit#s9", "stale", hid=H2)
    g(c2, "checkout", "-q", "--detach", "HEAD")
    detached = setstate(c2 / "handoffs", "blocked", "claude:relay-kit#s9", "stale", hid=H2)
    g(c2, "checkout", "-q", "main"); g(c2, "pull", "-q")
    shutil.copy2(c2 / "handoffs" / f"{H2}.md", TMP / "squash-copy.md")
    g(c1, "reset", "-q", "--hard", "HEAD~1")   # c1: a different commit holding the same file content (as after a squash)
    shutil.copy2(TMP / "squash-copy.md", c1 / "handoffs" / f"{H2}.md"); g(c1, "commit", "-qam", "same content")
    squash = setstate(c1 / "handoffs", "acknowledged", "chatgpt:ada#c1", "read", hid=H2)
    g(c1, "push", "-q", "--force")
    g(c2, "fetch", "-q"); g(c2, "reset", "-q", "--hard", "origin/main")
    g(c1, "rm", "-qf", f"handoffs/{H2}.md"); g(c1, "commit", "-qm", "removed"); g(c1, "push", "-q")
    gone = setstate(c2 / "handoffs", "acknowledged", "chatgpt:ada#c1", "read", hid=H2)
    case("protect: the shared copy is origin's default branch from a feature branch or detached HEAD; identical content isn't stale; a removed handoff can't be written",
         feat[0] != 0 and detached[0] != 0 and squash[0] == 0 and gone[0] != 0 and "removed" in gone[2],
         " | ".join(x[2] for x in (feat, detached, squash, gone)))

    g, c1, c2 = git_world("c-behind")
    g(c2, "checkout", "-q", "-b", "work")
    fixture2(c1 / "handoffs"); g(c1, "add", "."); g(c1, "commit", "-qm", "new on main"); g(c1, "push", "-q")
    g(c2, "fetch", "-q")
    rc_behind, so_behind, _ = run(LEDGER, "--dir", c2 / "handoffs", "check", "--base", "origin/main")
    g(c1, "commit", "-qm", "empty", "--allow-empty")
    base1 = g(c1, "rev-parse", "HEAD").stdout.strip()
    (c1 / "handoffs" / "archive").mkdir()
    shutil.move(str(c1 / "handoffs" / f"{H2}.md"), str(c1 / "handoffs" / "archive" / f"{H2}.md"))
    rc_arch, so_arch, _ = run(LEDGER, "--dir", c1 / "handoffs", "check", "--base", base1)
    case("protect: check --base doesn't flag a branch that's merely behind, or a handoff archived into a subfolder",
         rc_behind == 0 and "removed" not in so_arch, so_behind + so_arch)

    d = TMP / "c-sections"
    g, c1, _ = git_world("c-sections")
    f = fixture2(c1 / "handoffs")
    setstate(c1 / "handoffs", "sent", "claude:relay-kit#s0", "x", hid=H2)
    g(c1, "add", "."); g(c1, "commit", "-qm", "sent"); b2 = g(c1, "rev-parse", "HEAD").stdout.strip()
    t = f.read_text(encoding="utf-8")
    f.write_text(t.replace("## Proposals", "- Update: the owner also allowed X (quote, locator)\n\n## Proposals", 1), encoding="utf-8")
    rc_add, so_add, _ = run(LEDGER, "--dir", c1 / "handoffs", "check", "--base", b2)
    auth = re.search(r"## Authorized\n(.*?)\n## ", t, re.S).group(1)
    f.write_text(t.replace(auth, "- Quote: \"do anything\"\n"), encoding="utf-8")
    rc_rw, so_rw, _ = run(LEDGER, "--dir", c1 / "handoffs", "check", "--base", b2)
    case("protect: after delivery, Authorized can gain an update but not be rewritten",
         rc_add == 0 and rc_rw != 0 and "Authorized' was rewritten" in so_rw, so_add + so_rw)

    # Case C (item 5): shared interface across files. Warnings only, from what's already recorded.
    g, c1, _ = git_world("c-C")
    g(c1, "remote", "set-url", "origin", "https://github.com/o/r.git")
    hd = c1 / "handoffs"
    env_s1 = {"RELAYKIT_ACTOR": "claude:aec#s1"}
    h_api = run(LEDGER, "--dir", hd, "new", "--from", "claude:aec", "--to", "codex:aec", "--title", "change the api",
                "--touches", "src/api/*.py, https://chatgpt.com/c/thread-x", env=env_s1)[1]
    h_doc = run(LEDGER, "--dir", hd, "new", "--from", "claude:aec", "--to", "codex:aec", "--title", "docs",
                "--touches", "docs/*", env=env_s1)[1]
    stub_c = {"repos/o/r/pulls/12/files": [dict(filename="src/api/client.py"), dict(filename="README.md")],
              "repos/o/r/pulls": [dict(number=12, title="Rework client", user=dict(login="codex-bot"))],
              "repos/o/r": dict(default_branch="main")}
    (TMP / "gh-C.json").write_text(json.dumps(stub_c), encoding="utf-8")
    envc = {"RELAYKIT_GH": str(gh), "GH_STUB_DATA": str(TMP / "gh-C.json")}
    st_ = KIT / "tools" / "status.py"
    over = lambda out: out.split("== Overlaps")[1].split("\n== ")[0] if "== Overlaps" in out else ""
    me2 = run(st_, "--root", c1, "--no-fetch", "--no-ci", "--touching", "src/api/client.py", env=dict(envc, RELAYKIT_ACTOR="claude:aec#s2"))[1]
    me1 = run(st_, "--root", c1, "--no-fetch", "--no-ci", "--touching", "src/api/client.py", env=dict(envc, RELAYKIT_ACTOR="claude:aec#s1"))[1]
    other = run(st_, "--root", c1, "--no-fetch", "--no-ci", "--touching", "src/db/models.py", env=dict(envc, RELAYKIT_ACTOR="claude:mtg#s3"))[1]
    thread = run(st_, "--root", c1, "--no-fetch", "--no-ci", "--touching", "https://chatgpt.com/c/thread-x", env=dict(envc, RELAYKIT_ACTOR="claude:rk#s4"))[1]
    o2, o1, oo, ot = over(me2), over(me1), over(other), over(thread)
    case("case C (shared interface): warns about a handoff whose touches cover the file, an open PR changing it, and another session in the lane",
         "touches src/api/*.py: matches src/api/client.py" in o2
         and "open PR #12 (codex-bot" in o2 and "another session in claude:aec" in o2 and "#s1" in o2 and "docs" not in o2, me2)
    case("case C decoys: your own session isn't a warning; unrelated files and other lanes are silent; a thread URL claim is seen",
         "another session" not in o1 and "touches src/api/*.py" in o1 and "none found" in oo
         and "touches https://chatgpt.com/c/thread-x" in ot and "open PR" not in ot, o1 + "\n--\n" + oo + "\n--\n" + ot)
    # Review of #7: one thing, several spellings; a bad log time doesn't stop status.
    h_db = run(LEDGER, "--dir", hd, "new", "--from", "claude:aec", "--to", "codex:aec", "--title", "db",
               "--touches", "src/db", env=env_s1)[1]
    spell = {"./src/api/client.py": "touches src/api/*.py", str(c1 / "src" / "api" / "client.py"): "touches src/api/*.py",
             "src\\api\\client.py": "touches src/api/*.py", "src/db/models.py": "touches src/db",
             "https://chatgpt.com/c/thread-x/?model=x": "touches https://chatgpt.com/c/thread-x"}
    seen = {t: over(run(st_, "--root", c1, "--repo", c1, "--no-fetch", "--no-ci", "--touching", t,
                        env=dict(envc, RELAYKIT_ACTOR="claude:mtg#s5"))[1]) for t in spell}
    case("case C: ./paths, absolute paths, backslashes, a directory claim and a URL with a query all match",
         all(want in seen[t] for t, want in spell.items()) and "open PR #12" in seen["./src/api/client.py"]
         and "open PR #12" in seen[str(c1 / "src" / "api" / "client.py")], json.dumps(seen, indent=1))
    (c1 / "src" / "api").mkdir(parents=True); (c1 / "src" / "api" / "client.py").write_text("x\n")
    g(c1, "add", "src"); g(c1, "commit", "-qm", "code")
    globs = over(run(st_, "--root", c1, "--repo", c1, "--no-fetch", "--no-ci", "--touching", "src/*/client.py",
                     env=dict(envc, RELAYKIT_ACTOR="claude:mtg#s5"))[1])
    nomatch = over(run(st_, "--root", c1, "--repo", c1, "--no-fetch", "--no-ci", "--touching", "lib/*/client.py",
                       env=dict(envc, RELAYKIT_ACTOR="claude:mtg#s5"))[1])
    case("case C: two globs overlap when a tracked file matches both; globs meeting no common file don't",
         "touches src/api/*.py: matches src/*/client.py" in globs and "none found" in nomatch, globs + "\n--\n" + nomatch)
    f_db = hd / f"{h_db}.md"
    f_db.write_text(f_db.read_text(encoding="utf-8").replace("| prepared |", "| prepared |", 1)
                    + "- 2026-11-02T17:25 | sent | claude:aec#s9 | no time zone\n"
                    + "- 0001-01-01T00:00+01:00 | sent | claude:aec#s9 | before the first UTC moment\n", encoding="utf-8")
    rc_bad, out_bad, err_bad = run(st_, "--root", c1, "--no-fetch", "--no-ci", "--touching", "src/db/x.py",
                                   env=dict(envc, RELAYKIT_ACTOR="claude:aec#s2"))
    case("status: a log time without a zone doesn't crash it", rc_bad == 0 and "== Not checked" in out_bad, err_bad[-300:])


# ---------------- ledger append, stale copies, changed-since, review handoffs ----------------
def learning_tests():
    app = KIT / "tools" / "ledger_append.py"
    st_ = KIT / "tools" / "status.py"
    origin, c1, c2 = TMP / "la.git", TMP / "la-1", TMP / "la-2"
    g = lambda cwd, *a: subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *a], capture_output=True, text=True)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], capture_output=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(c1)], capture_output=True)
    (c1 / "governance").mkdir()
    first = dict(id="DEC-001", date="2026-11-01", status="adopted", decision="d1", scope="s", decider="owner", basis='"yes"', locator="chat", recorded_by="sam")
    (c1 / "governance" / "decisions.jsonl").write_text(json.dumps(first) + "\n", encoding="utf-8")
    g(c1, "checkout", "-q", "-b", "main"); g(c1, "add", "."); g(c1, "commit", "-qm", "init"); g(c1, "push", "-q", "-u", "origin", "main")
    subprocess.run(["git", "clone", "-q", str(origin), str(c2)], capture_output=True)
    prop = json.dumps(dict(status="proposed", decision="d2", scope="s"))
    a1 = run(app, "--dir", c1 / "governance", "decision", prop, env={"RELAYKIT_ACTOR": "claude:rk#s1"})
    g(c1, "commit", "-qam", "d2"); g(c1, "push", "-q")
    a2 = run(app, "--dir", c2 / "governance", "decision", prop, env={"RELAYKIT_ACTOR": "claude:aec#s2"})
    before2 = (c2 / "governance" / "decisions.jsonl").read_text(encoding="utf-8")
    g(c2, "pull", "-q")
    a3 = run(app, "--dir", c2 / "governance", "decision", prop, env={"RELAYKIT_ACTOR": "claude:aec#s2"})
    last = json.loads((c2 / "governance" / "decisions.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    case("ledger_append: takes the next id from the shared copy, refuses a stale copy, fills date and recorder",
         a1[1] == "DEC-002" and a2[0] != 0 and "pull the shared branch first" in a2[2] and before2.count("\n") == 1
         and a3[1] == "DEC-003" and last["recorded_by"] == "claude:aec#s2" and re.match(r"\d{4}-\d{2}-\d{2}$", last["date"]),
         " | ".join(x[1] + x[2] for x in (a1, a2, a3)))
    n_before = (c2 / "governance" / "decisions.jsonl").read_text(encoding="utf-8")
    bad = run(app, "--dir", c2 / "governance", "decision", json.dumps(dict(status="adopted", decision="no basis", scope="s")),
              env={"RELAYKIT_ACTOR": "claude:aec#s2"})
    given = run(app, "--dir", c2 / "governance", "decision", json.dumps(dict(id="DEC-009", status="proposed", decision="x", scope="s")),
                env={"RELAYKIT_ACTOR": "claude:aec#s2"})
    case("ledger_append: a line the check rejects is taken back out with a non-zero exit; a chosen id is refused",
         bad[0] != 0 and "nothing was appended" in bad[2] and (c2 / "governance" / "decisions.jsonl").exists()
         and (c2 / "governance" / "decisions.jsonl").read_text(encoding="utf-8") == n_before
         and given[0] != 0, bad[2] + given[2])

    lone = TMP / "la-lone" / "governance"; lone.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(lone.parent)], capture_output=True)
    off1 = run(app, "--dir", lone, "decision", prop, env={"RELAYKIT_ACTOR": "claude:rk#s1"})
    off2 = run(app, "--dir", lone, "decision", prop, "--offline", env={"RELAYKIT_ACTOR": "claude:rk#s1"})
    (c2 / "governance" / ".ledger_append.lock").write_text("x")
    locked = run(app, "--dir", c2 / "governance", "decision", prop, env={"RELAYKIT_ACTOR": "claude:aec#s2"})
    (c2 / "governance" / ".ledger_append.lock").unlink()
    case("ledger_append: no shared copy means refuse (unless --offline); a held lock refuses a second writer",
         off1[0] != 0 and "can't be read" in off1[2] and off2[0] == 0 and off2[1] == "DEC-001"
         and locked[0] != 0 and "lock" in locked[2], " | ".join(x[1] + x[2] for x in (off1, off2, locked)))

    # second review of #8: diverged copies, a missing folder, an empty ledger
    g(c1, "fetch", "-q"); g(c1, "reset", "-q", "--hard", "origin/main"); g(c2, "fetch", "-q"); g(c2, "reset", "-q", "--hard", "origin/main")
    run(app, "--dir", c1 / "governance", "decision", json.dumps(dict(status="proposed", decision="A's", scope="s")), env={"RELAYKIT_ACTOR": "claude:a#1"})
    run(app, "--dir", c2 / "governance", "decision", json.dumps(dict(status="proposed", decision="B's", scope="s")), env={"RELAYKIT_ACTOR": "claude:b#2"})
    g(c2, "commit", "-qam", "B"); g(c2, "push", "-q")
    div = run(app, "--dir", c1 / "governance", "decision", prop, env={"RELAYKIT_ACTOR": "claude:a#1"})
    g(c1, "checkout", "-q", "--", "."); g(c1, "pull", "-q")
    sub = run(app, "--dir", c1 / "new" / "gov", "decision", prop, env={"RELAYKIT_ACTOR": "claude:a#1"})
    (c1 / "new" / "gov" / "decisions.jsonl").unlink(missing_ok=True)
    (c1 / "governance" / "challenges.jsonl").write_text(""); g(c1, "add", "."); g(c1, "commit", "-qm", "empty ch"); g(c1, "push", "-q")
    emp = run(app, "--dir", c1 / "governance", "challenge", json.dumps(dict(status="resolved", concern="c", raised_by="x")), env={"RELAYKIT_ACTOR": "claude:a#1"})
    case("ledger_append: equal top ids with different lines are refused; a ledger folder not created yet still reads the shared copy; "
         "a failed append to an empty ledger leaves the file in place",
         div[0] != 0 and "different lines under the same ids" in div[2] and sub[0] == 0 and sub[1] == "DEC-001"
         and emp[0] != 0 and (c1 / "governance" / "challenges.jsonl").exists(), " | ".join(x[1] + x[2] for x in (div, sub, emp)))

    sp = TMP / "la-spaced" / "governance"; sp.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(sp.parent)], capture_output=True)
    (sp / "decisions.jsonl").write_text('{"id" : "DEC-001", "date": "2026-11-01", "status": "proposed", "decision": "d", "scope": "s", "evidence": {"id": "DEC-099"}}\n', encoding="utf-8")
    spaced = run(app, "--dir", sp, "decision", prop, "--offline", env={"RELAYKIT_ACTOR": "claude:rk#s1"})
    case("ledger_append: reads ids as JSON (spacing, nested ids)", spaced[1] == "DEC-002", spaced[1] + spaced[2])
    lanerec = run(app, "--dir", sp, "decision", json.dumps(dict(status="proposed", decision="x", scope="s", recorded_by="claude:rk")),
                  "--offline", env={"RELAYKIT_ACTOR": "claude:rk#s1"})
    g(c2, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/gone")   # dangling, as after branch cleanup
    dang = run(app, "--dir", c2 / "governance", "decision", prop, env={"RELAYKIT_ACTOR": "claude:aec#s2"})
    g(c2, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    case("ledger_append: a lane as recorder is refused even with no commits to compare; a dangling origin/HEAD refuses, not 'empty'",
         lanerec[0] != 0 and "lane#session" in lanerec[2] and dang[0] != 0 and "can't be read" in dang[2], lanerec[2] + " | " + dang[2])

    # stale copies: an @1 handoff made after @2 was adopted is named; older @1 files and @2 files aren't
    d = TMP / "stale-fmt"; (d / "outputs").mkdir(parents=True)
    (d / ".ledger.json").write_text('{"owner": "sam", "contract_since": {"relay-kit/handoff@2": "2026-11-02T00:03Z"}}', encoding="utf-8")
    old_ok = FIXTURE.read_text(encoding="utf-8")                                    # @1, created 2026-10-28
    (d / FIXTURE.name).write_text(old_ok, encoding="utf-8")
    late = old_ok.replace(HID, "HO-20261102-003").replace("created: 2026-10-28T14:05Z", "created: 2026-11-02T17:17Z")
    (d / "HO-20261102-003.md").write_text(late, encoding="utf-8")
    fixture2(d)
    rc, so, _ = run(LEDGER, "--dir", d, "check")
    case("stale copy: check names the session that made an old-format handoff after adoption, and only that one",
         rc == 0 and "HO-20261102-003.md: made in relay-kit/handoff@1" in so and so.count("old copy") == 1, so)
    for since in ("2026-11-02T18:03+01:00", "2026-11-02T00:03:30Z", 20261102):   # 17:03Z: later as text, earlier as time
        (d / ".ledger.json").write_text(json.dumps({"owner": "sam", "contract_since": {"relay-kit/handoff@2": since}}), encoding="utf-8")
        rc2, so2, se2 = run(LEDGER, "--dir", d, "check")
        ok = rc2 == 0 and (("old copy" in so2) if not isinstance(since, int) else ("isn't an ISO time" in so2))
        if not ok:
            break
    case("stale copy: contract_since with an offset or seconds compares as a time; a bad value is a warning, not a crash",
         ok, f"{since}: {so2}{se2}")

    # changed since this session's last commit
    g(c1, "pull", "-q")
    (c1 / "notes.txt").write_text("x\n"); g(c1, "add", "."); g(c1, "commit", "-qm", "my work\n\nClaude-Session: https://claude.ai/code/session_ME123"); g(c1, "push", "-q")
    quiet = run(st_, "--root", c1, "--no-fetch", "--no-ci", env={"RELAYKIT_ACTOR": "claude:rk#session_ME123"})[1]
    g(c2, "fetch", "-q"); g(c2, "reset", "-q", "--hard", "origin/main")
    run(app, "--dir", c2 / "governance", "decision", json.dumps(dict(status="proposed", decision="other lane's news", scope="s")),
        env={"RELAYKIT_ACTOR": "claude:aec#s2"})
    with open(c2 / "governance" / "decisions.jsonl", "a", encoding="utf-8") as f:   # valid JSON, indented
        f.write("  " + json.dumps(dict(id="DEC-099", date="2026-11-02", status="proposed", decision="indented line", scope="s")) + "\n")
    g(c2, "commit", "-qam", "news\n\nSee session_ME123's earlier work.\n\nClaude-Session: https://claude.ai/code/session_xsession_ME123")   # a mention, and an id that merely ends in ours
    g(c2, "push", "-q")
    news = run(st_, "--root", c1, "--no-ci", env={"RELAYKIT_ACTOR": "claude:rk#session_ME123"})[1]   # fetched, not pulled
    # (the earlier decision "d2" must not be listed: matched as a whole line, since a commit hash in the header can contain "d2")
    sec = lambda o: o.split("== Changed")[1].split("\n== ")[0] if "== Changed" in o else ""
    g(c1, "pull", "-q")
    pulled = run(st_, "--root", c1, "--no-fetch", "--no-ci", env={"RELAYKIT_ACTOR": "claude:rk#session_ME123"})[1]
    one = TMP / "la-one"; (one / "governance").mkdir(parents=True)
    (one / "governance" / "decisions.jsonl").write_text(json.dumps(first) + "\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(one)], capture_output=True)
    g(one, "add", "."); g(one, "commit", "-qm", "only commit")
    only = run(st_, "--root", one, "--no-fetch", "--no-ci", "--since", "2020-01-01")[1]
    empty = TMP / "la-empty"; subprocess.run(["git", "init", "-q", str(empty)], capture_output=True)
    rc_e, out_e, err_e = run(st_, "--root", empty, "--no-fetch", "--no-ci", env={"RELAYKIT_ACTOR": "claude:rk#s1"})
    case("changed since: lists what other lanes pushed after this session's last commit (fetched, not yet pulled), "
         "not earlier lines, and isn't fooled by a commit that only mentions the session id; an empty repo doesn't crash",
         "nothing new" in sec(quiet) and "other lane's news" in sec(news) and not re.search(r": d2$", sec(news), re.M)
         and "other lane's news" in sec(pulled) and "indented line" in sec(pulled) and "DEC-001" in sec(only) and rc_e == 0 and "== Not checked" in out_e, sec(quiet) + "\n--\n" + sec(news) + err_e[-300:])

    # review handoffs
    d = TMP / "review"
    no = run(LEDGER, "--dir", d, "new", "--from", "claude:rk", "--to", "codex:x", "--title", "R", "--review", env={"RELAYKIT_ACTOR": "claude:rk#s1"})
    rc, hid, _ = run(LEDGER, "--dir", d, "new", "--from", "claude:rk", "--to", "review:chatgpt", "--title", "Review #8", "--review",
                     "--artifact", "PR 8", "--spec", "the handoff", env={"RELAYKIT_ACTOR": "claude:rk#s1"})
    t = (d / f"{hid}.md").read_text(encoding="utf-8") if rc == 0 else ""
    rc2, hid2, _ = run(LEDGER, "--dir", d, "new", "--from", "claude:rk", "--to", "review:copilot", "--title", "R2", "--review",
                       env={"RELAYKIT_ACTOR": "claude:rk#s1"})
    t2 = (d / f"{hid2}.md").read_text(encoding="utf-8") if rc2 == 0 else ""
    t2f = re.sub(r"## Authorized\n.*?\n## ", "## Authorized\n- Quote: \"go\" (owner, chat, 2026-11-02)\n- Approving: x\n- Permits: x\n- Doesn't cover: x\n\n## ", t2, flags=re.S)
    (d / f"{hid2}.md").write_text(t2f, encoding="utf-8")
    sent2 = run(LEDGER, "--dir", d, "set", hid2, "sent", "--by", "claude:rk#s1", "--note", "x")
    case("review handoff: without --artifact and --spec it can't be sent, even once Authorized is filled",
         rc2 == 0 and sent2[0] != 0 and "TODO" in sent2[2], sent2[2])
    case("review handoff: needs a review: recipient; fills the review sections, leaves Authorized for the owner",
         no[0] != 0 and rc == 0 and "Review PR 8 against the handoff" in t and "left out on purpose" in t
         and f"outputs/{hid}.md" in t and "NOT AUTHORIZED YET" in t and run(LEDGER, "--dir", d, "check")[0] == 0, no[2] + t[:400])


# ---------------- deployable: the relay-kit command and init (DEC-044) ----------------
def deploy_tests():
    link = TMP / "pkgpath"; link.mkdir()
    os.symlink(KIT / "tools", link / "relaykit")   # the checkout's tools/ as the installed `relaykit` package
    env = {"PYTHONPATH": str(link)}
    rk = lambda *a, env_=None: subprocess.run([sys.executable, "-m", "relaykit", *map(str, a)], capture_output=True,
                                              encoding="utf-8", env=dict(os.environ, **env, **(env_ or {})), timeout=60)
    v = rk("--version")
    out = TMP / "records-new"
    i = rk("init", out, "--owner", "Sam", "--lanes", "research=rs")
    files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    cfg = json.loads((out / "handoffs" / ".ledger.json").read_text(encoding="utf-8")) if i.returncode == 0 else {}
    wf = (out / ".github" / "workflows" / "records.yml").read_text(encoding="utf-8") if i.returncode == 0 else ""
    leftover = [f for f in files if re.search(r"(?<!\$)\{\{[A-Z_]+\}\}", (out / f).read_text(encoding="utf-8"))]
    case("deploy: init makes a records folder: owner and lanes set, dot-files named, CI pinned to this version, hook executable",
         v.stdout.strip() == "relay-kit 0.1.0" and i.returncode == 0 and cfg.get("owner") == "sam" and cfg.get("lanes") == {"research": "RS"}
         and "relay-kit/handoff@2" in cfg.get("contract_since", {})
         and {".gitignore", ".githooks/pre-push", ".github/workflows/records.yml", "README.md", "threads.md",
              "governance/decisions.jsonl", "governance/challenges.jsonl", "handoffs/.ledger.json"} <= set(files)
         and (out / "handoffs" / "outputs").is_dir() and "@v0.1.0" in wf and "${{ github.token }}" in wf
         and os.access(out / ".githooks" / "pre-push", os.X_OK) and not leftover, i.stdout + i.stderr + str(files) + str(leftover))
    again = rk("init", out, "--owner", "sam")
    os.symlink(TMP / "elsewhere" / "deep", TMP / "r-link")
    bad = [rk("init", TMP / "r-bad1", "--owner", "two words"), rk("init", TMP / "r-bad2", "--owner", "sam", "--lanes", "x=too-long-code!"),
           rk("init", TMP / "r-bad3", "--owner", "sam", "--lanes", "a=RS,b=RS"), rk("init", TMP / "r-bad4", "--owner", "sam", "--lanes", "foo bar=X"),
           rk("init", TMP / "r-bad5", "--owner", "sam", "--kit-ref", "main"), rk("init", TMP / "r-link", "--owner", "sam")]
    case("deploy: init refuses a non-empty folder, bad owner or lane values, repeated codes, a branch as kit ref and a symlink, writing nothing",
         again.returncode != 0 and "isn't an empty folder" in again.stderr and all(b.returncode != 0 for b in bad)
         and not any((TMP / f"r-bad{k}").exists() for k in range(1, 6)) and not (TMP / "elsewhere").exists(),
         again.stderr + "".join(b.stderr for b in bad))

    # each subcommand is the same script: same output and exit code as running it directly
    d = ledger_dir("cli-same")
    pairs = [(("handoff", "--dir", d, "list"), (LEDGER, "--dir", d, "list")),
             (("handoff", "--dir", d, "check"), (LEDGER, "--dir", d, "check")),
             (("ledger", "check", "--dir", out / "governance"), (KIT / "tools" / "check_ledgers.py", "--dir", out / "governance"))]
    same = []
    for via_cli, direct in pairs:
        c, dr = rk(*via_cli), run(*direct)
        same.append((c.returncode, c.stdout.strip()) == (dr[0], dr[1]))
    case("deploy: relay-kit handoff/ledger give the same output and exit code as the scripts", all(same), str(same))

    # the new records folder works end to end, and its pre-push hook blocks a bad ledger line
    g = lambda *a: subprocess.run(["git", "-C", str(out), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "push.negotiate=false", *a],
                                  capture_output=True, text=True, env=dict(os.environ, **env))
    origin = TMP / "records-new.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], capture_output=True)
    g("init", "-q", "-b", "main"); g("config", "core.hooksPath", ".githooks"); g("remote", "add", "origin", str(origin))
    g("add", "."); g("commit", "-qm", "init")
    first = g("push", "-q", "-u", "origin", "main")
    actor = {"RELAYKIT_ACTOR": "claude:research#s1"}
    hid = rk("handoff", "--dir", out / "handoffs", "new", "--from", "claude:research", "--to", "chatgpt:review", "--title", "T", env_=actor).stdout.strip()
    dec = rk("ledger", "append", "--dir", out / "governance", "decision", json.dumps(dict(status="proposed", decision="d", scope="s")), env_=actor).stdout.strip()
    g("add", "."); g("commit", "-qm", "work")
    good = g("push", "-q")
    with open(out / "governance" / "decisions.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(dict(id="DEC-009", date="2026-11-03", status="adopted", decision="no basis", scope="s")) + "\n")
    g("commit", "-qam", "bad line")
    blocked = g("push", "-q")
    g("reset", "-q", "--hard", "origin/main")
    # a bad commit on a branch that isn't checked out, and a bad commit under a clean one, are both caught
    g("checkout", "-q", "-b", "side")
    lines = (out / "governance" / "decisions.jsonl").read_text(encoding="utf-8").replace('"decision": "d"', '"decision": "rewritten"')
    (out / "governance" / "decisions.jsonl").write_text(lines, encoding="utf-8"); g("commit", "-qam", "rewrite an earlier line")
    (out / "notes.txt").write_text("x"); g("add", "notes.txt"); g("commit", "-qm", "clean commit on top")
    g("checkout", "-q", "main")
    side = g("push", "-q", "origin", "side")
    clean = g("push", "-q", "origin", "main")
    sc = TMP / "scan"; sc.mkdir()
    (sc / ".private-names").write_text("ownername\n"); (sc / ".gitignore").write_text(".private-names\n")
    (sc / "pyproject.toml").write_text('[project.urls]\nHomepage = "https://github.com/ownername/relay-kit"\n')
    (sc / "a.md").write_text('pip install "relay-kit @ git+https://github.com/ownername/relay-kit' + '@' + 'v0.1.0"\nold: ownername/-relay-kit\n')
    (sc / "c.md").write_text("see github.com/ownername/other-relay-kit and github.com/ownername/relay-kit-private\n")
    (sc / "b.md").write_text("ownername wrote this; mail " + "ownername" + "@example.com\n")
    subprocess.run(["git", "init", "-q", str(sc)], capture_output=True); subprocess.run(["git", "-C", str(sc), "add", "."], capture_output=True)
    rc_s, so_s, _ = run(KIT / "tools" / "prepublish_scan.py", "--root", sc)
    case("scan: only the kit's own repository URL passes; the name in other repos, text or emails is still caught",
         rc_s != 0 and "a.md" not in so_s and "pyproject" not in so_s and "b.md:1: private name" in so_s
         and "b.md:1: email address" in so_s and so_s.count("c.md:1: private name") == 2, so_s)
    case("deploy: a fresh records repo takes handoffs and ledger lines, and its pre-push hook blocks a bad line",
         first.returncode == 0 and hid.startswith("HO-RS-") and dec == "DEC-001" and good.returncode == 0
         and blocked.returncode != 0 and "checks failed" in blocked.stderr, first.stderr + good.stderr + blocked.stderr)
    case("deploy: the hook checks the commits being pushed (a branch not checked out, a bad commit under a clean one)",
         side.returncode != 0 and "checks failed on refs/heads/side" in side.stderr and clean.returncode == 0, side.stderr + clean.stderr)


# ---------------- telemetry ----------------
def telemetry_tests():
    tel = load("telemetry", X / "chat-telemetry" / "01_chat_telemetry.py")
    title, platform, events = tel.load_chatgpt(chatgpt_fixture())
    th = tel.analyze(title, platform, events, "fixture", datetime.timezone.utc, 120, None)
    case("telemetry: a 30-minute gap is not 'engaged'", th["summary"]["return_gap_engaged_s"].get("n", 0) == 0,
         str(th["summary"]["return_gap_engaged_s"]))
    conv = dict(name="No tools", chat_messages=[
        dict(uuid="u", sender="human", created_at="2026-10-28T14:00:00Z", content=[dict(type="text", text="Hello")]),
        dict(uuid="a", sender="assistant", created_at="2026-10-28T14:00:01Z",
             content=[dict(type="text", text="Hello back", start_timestamp="2026-10-28T14:00:01Z", stop_timestamp="2026-10-28T14:00:02Z")])])
    title, platform, events = tel.load_claude_ai(conv)
    th = tel.analyze(title, platform, events, "fixture", datetime.timezone.utc, 120, None)
    case("telemetry: a text block's end time isn't a tool step", th["summary"]["tool_steps"] == 0, str(th["summary"]["tool_steps"]))
    raw = TMP / "skew.raw.json"
    d = chatgpt_fixture()
    d["mapping"]["a1"]["message"]["create_time"] = 1793197811   # clock skew: after u2
    raw.write_text(json.dumps(wrap(d)), encoding="utf-8")
    out = TMP / "tel-skew"
    rc, so, se = run(X / "chat-telemetry" / "01_chat_telemetry.py", raw, "-o", out)
    t = json.loads((out / "telemetry.json").read_text(encoding="utf-8"))["threads"][0]
    case("telemetry: clock skew doesn't move a reply into the next exchange",
         rc == 0 and [x["assistant_messages"] for x in t["turns"]] == [2, 1] and any("backwards" in c for c in t["checks"]),
         str([x["assistant_messages"] for x in t["turns"]]) + se)
    rc, so, se = run(X / "chat-telemetry" / "01_chat_telemetry.py", TMP / "fixture.raw.json", "-o", TMP / "tel-badtz", "--tz", "Nowhere/Missing")
    t = json.loads((TMP / "tel-badtz" / "telemetry.json").read_text(encoding="utf-8")) if rc == 0 else {}
    case("telemetry: missing timezone data falls back to UTC with a note",
         rc == 0 and any("unavailable" in c for c in t["threads"][0]["checks"]), se)


# ---------------- evidence ----------------
def evidence_tests():
    if not (KIT / "evidence").is_dir():   # private material; absent from the public repo
        return
    html = (KIT / "evidence" / "coordination-telemetry-dashboard.html").read_text(encoding="utf-8")
    dash = json.loads(re.search(r"const DATA=(.*?);\n", html).group(1))
    turns = list(csv.DictReader((KIT / "evidence" / "turns.csv").open(encoding="utf-8")))
    note = (KIT / "evidence" / "README.md").read_text(encoding="utf-8") if (KIT / "evidence" / "README.md").exists() else ""
    rel_csv = sum(r["author"] == "model-relayed" for r in turns)
    rel_dash = sum(t["relay"] for th in dash["threads"] for t in th["turns"])
    case("evidence: relay-count difference between dashboard and CSV is documented",
         rel_csv == rel_dash or (str(rel_csv) in note and str(rel_dash) in note), f"csv {rel_csv}, dashboard {rel_dash}")


def packaging_tests():
    """The skills as they ship: each upload zip holds the skill, and handoff-ledger's script runs
    from the unzipped copy alone, with nothing from this repository on its path."""
    ext = load("tools_extract", KIT / "tools_extract.py")
    zips = ext.build_zips(TMP / "upload")
    names = {z.stem for z in zips}
    want = {p.name for p in (KIT / "skills").iterdir() if (p / "SKILL.md").exists()}
    u = TMP / "unzipped"
    shutil.unpack_archive(str(TMP / "upload" / "handoff-ledger.zip"), str(u))
    script = u / "handoff-ledger" / "scripts" / "handoff.py"
    d = TMP / "pkg-handoffs"
    rc, hid, err = run(script, "--dir", d, "new", "--from", "claude:pkg", "--to", "codex:x", "--title", "T", env={"RELAYKIT_ACTOR": "claude:pkg#1"})
    import zipfile
    mismatch = []
    for z in zips:   # each zip holds exactly its skill's files, byte for byte
        src = KIT / "skills" / z.stem
        files = {f.relative_to(KIT / "skills").as_posix(): f.read_bytes() for f in src.rglob("*") if f.is_file() and "__pycache__" not in f.parts}
        with zipfile.ZipFile(z) as zf:
            members = {n: zf.read(n) for n in zf.namelist()}
        if members != files:
            mismatch.append(z.name)
    case("packaging: one zip per skill holding exactly its files, and the zipped handoff script works standalone",
         names == want and not mismatch and rc == 0 and run(script, "--dir", d, "check")[0] == 0,
         f"{sorted(names ^ want)} {mismatch} {err}")
    kc = TMP / "kit-copy"   # never point a deleting tool at the real checkout, even in a test
    shutil.copytree(KIT / "skills", kc / "skills"); shutil.copytree(KIT / "tests", kc / "tests")
    shutil.copy2(KIT / "tools_extract.py", kc / "tools_extract.py"); shutil.copytree(KIT / "tools", kc / "tools")
    subprocess.run(["git", "init", "-q", str(kc)], capture_output=True)
    subprocess.run(["git", "-C", str(kc), "add", "."], capture_output=True)
    tx = kc / "tools_extract.py"
    (TMP / "userdocs").mkdir(); (TMP / "userdocs" / "thesis.txt").write_text("mine")
    (kc / "notes").mkdir(); (kc / "notes" / "draft.md").write_text("untracked draft")
    (TMP / "userdocs2").mkdir(); (TMP / "userdocs2" / "a.txt").write_text("mine"); os.symlink(TMP / "userdocs2", kc / "linkout")
    refused = [run(tx, "--out", target) for target in (kc / "skills", kc, kc / "tests", TMP / "userdocs", kc / "notes", kc / "linkout")]
    noarg = run(tx, "--out")
    case("packaging: tools_extract replaces only folders it made: it refuses sources, the repo, user files inside or outside it, symlinks to them, and a missing --out value",
         all(r[0] != 0 and "refus" in r[2] for r in refused) and (kc / "skills" / "handoff-ledger" / "SKILL.md").exists()
         and (kc / "tests" / "run_tests.py").exists()
         and (TMP / "userdocs" / "thesis.txt").exists() and (kc / "notes" / "draft.md").exists() and (TMP / "userdocs2" / "a.txt").exists()
         and run(tx, "--out", TMP / "fresh-out")[0] == 0 and run(tx, "--out", TMP / "fresh-out")[0] == 0
         and noarg[0] != 0 and "Traceback" not in noarg[2], " | ".join(r[2] for r in refused) + noarg[2])


def finding_tests():
    """Without --dir, a session finds the records only where it's told to look: ./handoffs, an
    existing $RELAYKIT_RECORDS, or a clone of the repository the skill's relay-kit.json names.
    The owner comes from the folder, else from relay-kit.json, and the two must agree. Each
    reproduction from the PR #12 reviews (a fresh Claude subagent, and a ChatGPT reviewer) is a case here."""
    home = TMP / "find-home"; home.mkdir()
    def at(cwd, script, *args, env=None):
        e = dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", HOME=str(home))
        e.pop("RELAYKIT_RECORDS", None); e.update(env or {})
        p = subprocess.run([sys.executable, "-I", "-B", str(script), *map(str, args)], capture_output=True,
                           encoding="utf-8", env=e, timeout=60, cwd=cwd)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    def records(path, origin=None, owner="sam", git=True):
        (path / "handoffs" / "outputs").mkdir(parents=True); (path / "governance").mkdir()
        (path / "handoffs" / ".ledger.json").write_text(json.dumps({"owner": owner}), encoding="utf-8")
        shutil.copy(FIXTURE, path / "handoffs" / FIXTURE.name)
        if git:
            subprocess.run(["git", "init", "-q", str(path)], check=True)
            if origin:
                subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)
        return path
    skill = TMP / "skill-copy" / "handoff-ledger"
    shutil.copytree(KIT / "skills" / "handoff-ledger", skill, ignore=shutil.ignore_patterns("__pycache__"))
    baked = skill / "scripts" / "handoff.py"
    def bake(**kw):
        (skill / "relay-kit.json").write_text(json.dumps(kw), encoding="utf-8")
    def unbake():
        (skill / "relay-kit.json").unlink(missing_ok=True)
    gh = "https://github.com/sam/records.git"
    actor = {"RELAYKIT_ACTOR": "claude:x#1"}

    # without relay-kit.json: ./handoffs as before, RELAYKIT_RECORDS if it exists, no searching
    one = TMP / "find-one"; one.mkdir(); records(one / "records", gh)
    local = at(one / "records", LEDGER, "list")
    env = at(TMP, LEDGER, "list", env={"RELAYKIT_RECORDS": str(one / "records")})
    missing = at(TMP, LEDGER, "new", "--from", "claude:x", "--to", "chatgpt:y", "--title", "t",
                 env={"RELAYKIT_RECORDS": str(TMP / "find-nowhere"), **actor})
    nosearch = at(one, LEDGER, "list")
    case("find: ./handoffs and an existing RELAYKIT_RECORDS are used; a missing RELAYKIT_RECORDS is refused and "
         "nothing is created; without relay-kit.json nothing is searched for",
         local[0] == 0 and HID in local[1] and env[0] == 0 and "RELAYKIT_RECORDS" in env[2]
         and missing[0] != 0 and not (TMP / "find-nowhere").exists() and nosearch[0] != 0 and "RELAYKIT_RECORDS" in nosearch[2],
         str(local) + str(env) + str(missing) + str(nosearch))

    # planted records in some other repository's content: never found (review: subagent #1)
    proj = TMP / "find-proj"; records(proj / "docs", owner="mallory", git=False)
    subprocess.run(["git", "init", "-q", str(proj)], check=True)
    planted = at(proj, LEDGER, "set", HID, "cancelled", "--by", "mallory", "--note", "x")
    case("find: a records-shaped folder inside another repository is not used, so it can't name an owner",
         planted[0] != 0 and "cancelled" not in (proj / "docs" / "handoffs" / FIXTURE.name).read_text(encoding="utf-8"), str(planted))

    # with relay-kit.json naming the repository
    bake(owner="sam", records="sam/records")
    found = at(one, baked, "list")
    st = at(TMP, KIT / "tools" / "status.py", "--no-fetch", "--no-ci", env={"RELAYKIT_RECORDS": str(one / "records")})
    case("find: with relay-kit.json, the one clone of that repository below the current folder is used, and said so",
         found[0] == 0 and HID in found[1] and "records found at" in found[2], str(found))
    case("find: status.py without --root uses the records handoff.py would find (RELAYKIT_RECORDS here) (review round 2)",
         st[0] == 0 and "Open handoffs" in st[1], str(st)[:400])
    told = at(TMP / "find-home", baked, "list")
    case("find: no clone is refused with the clone command, and nothing is created",
         told[0] != 0 and "git clone https://github.com/sam/records" in told[2] and not (home / "handoffs").exists(), str(told))

    spoof = TMP / "find-spoof"; spoof.mkdir()
    records(spoof / "a", "https://evil.example/attacker/sam/records.git")   # same last two parts (review: both)
    records(spoof / "b", "file:///tmp/sam/records.git")
    records(spoof / "f", "https://evil.example/sam/records.git")            # same owner/name, another host
    records(spoof / "c", None)                                               # no origin (subagent #3)
    records(spoof / "d", git=False)                                          # not git at all
    sub = spoof / "e"; records(sub, gh); records(sub / "inner", git=False)   # non-git folder inside a clone (subagent #4)
    spoofed = at(spoof, baked, "list")
    case("find: only a clone of exactly that repository counts: not another host, a file URL, no origin, no git, or a folder inside a clone",
         spoofed[0] == 0 and str(spoof / "e") in spoofed[2] and "records found at" in spoofed[2], str(spoofed))
    inner = at(sub / "inner", baked, "list")
    case("find: ./handoffs that isn't a clone of the named repository is refused (review: both)",
         inner[0] != 0 and "isn't in a clone of sam/records" in inner[2], str(inner))
    envwrong = at(TMP, baked, "list", env={"RELAYKIT_RECORDS": str(spoof / "a")})
    case("find: RELAYKIT_RECORDS that isn't a clone of the named repository is refused",
         envwrong[0] != 0 and "isn't in a clone" in envwrong[2], str(envwrong))

    sym = TMP / "find-sym"; sym.mkdir(); outside = records(TMP / "find-outside", git=False)   # same owner, so only the symlink rule can refuse it
    (sym / "r").mkdir(); subprocess.run(["git", "init", "-q", str(sym / "r")], check=True)
    subprocess.run(["git", "-C", str(sym / "r"), "remote", "add", "origin", gh], check=True)
    os.symlink(outside / "handoffs", sym / "r" / "handoffs")
    symlinked = at(sym, baked, "list"); symlocal = at(sym / "r", baked, "list")
    case("find: a clone whose handoffs/ is a symlink out of it is not used (review: ChatGPT)",
         symlinked[0] != 0 and symlocal[0] != 0, str(symlinked) + str(symlocal))

    filesym = TMP / "find-filesym"; filesym.mkdir(); fr = records(filesym / "r", gh)
    (TMP / "find-evil.json").write_text('{"owner": "sam"}', encoding="utf-8")
    (fr / "handoffs" / ".ledger.json").unlink(); os.symlink(TMP / "find-evil.json", fr / "handoffs" / ".ledger.json")
    filelink = at(filesym, baked, "list")
    plainfolder = TMP / "find-gitdir"; records(plainfolder / "p", git=False)
    gitdir = at(plainfolder / "p", baked, "list", env={"GIT_DIR": str(one / "records" / ".git")})
    case("find: a file inside handoffs/ that links out of the clone, or GIT_DIR pointing at a real clone, doesn't pass (review round 2)",
         filelink[0] != 0 and gitdir[0] != 0 and "isn't in a clone" in gitdir[2], str(filelink) + str(gitdir))
    badroot = TMP / "find-badroot"; (badroot / "handoffs").mkdir(parents=True)
    (badroot / "handoffs" / ".ledger.json").write_text("nope", encoding="utf-8")
    asgiven = at(TMP, KIT / "tools" / "status.py", "--root", badroot, "--no-fetch", "--no-ci")
    case("find: status --root is used as given, with no new owner check (review round 2)",
         asgiven[0] == 0 and "refused" not in asgiven[2], str(asgiven))

    gl = TMP / "find-govlink"; records(gl / "r", gh); shutil.rmtree(gl / "r" / "governance")
    (TMP / "find-other-gov").mkdir(); os.symlink(TMP / "find-other-gov", gl / "r" / "governance")
    glink = at(gl, KIT / "tools" / "ledger_append.py", "decision", '{"status": "proposed", "decision": "x", "scope": "t"}', "--offline",
               env={"RELAYKIT_RECORDS": str(gl / "r"), **actor})
    glocal = at(gl / "r", KIT / "tools" / "ledger_append.py", "decision", '{"status": "proposed", "decision": "x", "scope": "t"}', "--offline", env=actor)
    case("find: ledger_append without --dir refuses a governance folder that links out of the records (review round 2)",
         glink[0] != 0 and glocal[0] != 0 and not list((TMP / "find-other-gov").iterdir()), str(glink) + str(glocal))

    two = TMP / "find-two"; two.mkdir(); records(two / "x", gh); records(two / "y", "git" + "@github.com:Sam/Records")
    several = at(two, baked, "list")
    case("find: two clones of the named repository are refused", several[0] != 0 and "several clones" in several[2], str(several))

    # owner
    d = ledger_dir("find-owner"); (d / ".ledger.json").unlink(); unbake()
    warned = at(TMP, LEDGER, "--dir", d, "list")
    as_plain = at(TMP, LEDGER, "--dir", d, "set", HID, "cancelled", "--by", "sam", "--note", "x")
    bake(owner="sam")
    as_baked = at(TMP, baked, "--dir", d, "set", HID, "cancelled", "--by", "sam", "--note", "dropped")
    case("find: with no .ledger.json the owner comes from relay-kit.json; without either, a warning says the owner is 'owner'",
         warned[0] == 0 and "owner is called 'owner'" in warned[2] and as_plain[0] != 0
         and as_baked[0] == 0 and "from relay-kit.json" in as_baked[2], str(warned) + str(as_plain) + str(as_baked))
    bake(owner="alex", records="sam/records")
    clash = at(one, baked, "list")
    case("find: a folder owner and a skill owner that differ are refused",
         clash[0] != 0 and "made for 'alex'" in clash[2], str(clash))

    for bad in ('{"owner": ["sam"]}', '{"records": "x/.."}', '{"records": "https://evil.example"}', "not json"):
        (skill / "relay-kit.json").write_text(bad, encoding="utf-8")
        r = at(one / "records", baked, "list")
        if r[0] == 0 or "Traceback" in r[2]:
            break
    (skill / "relay-kit.json").unlink(); (skill / "relay-kit.json").mkdir()
    isdir = at(one / "records", baked, "list"); (skill / "relay-kit.json").rmdir()
    case("find: a malformed relay-kit.json is refused, not ignored or a traceback (subagent #10)",
         r[0] != 0 and "Traceback" not in r[2] and isdir[0] != 0 and "Traceback" not in isdir[2], str(r) + str(isdir))

    # ledger_append: ./governance stays the default; discovery only when there's none (subagent #5)
    unbake()
    here = TMP / "find-gov"; (here / "governance").mkdir(parents=True)
    kept = at(here, KIT / "tools" / "ledger_append.py", "decision", '{"status": "proposed", "decision": "x", "scope": "t"}', "--offline", env=actor)
    viaenv = at(TMP, KIT / "tools" / "ledger_append.py", "decision", '{"status": "proposed", "decision": "x", "scope": "t"}', "--offline",
                env={"RELAYKIT_RECORDS": str(one / "records"), **actor})
    (here / "handoffs").mkdir(); (here / "handoffs" / ".ledger.json").write_text("not json", encoding="utf-8")
    checked_here = at(here, KIT / "tools" / "ledger_append.py", "decision", '{"status": "proposed", "decision": "y", "scope": "t"}', "--offline", env=actor)
    case("find: ledger_append with ./governance still makes the owner check on ./handoffs (review round 2)",
         checked_here[0] != 0 and ".ledger.json" in checked_here[2], str(checked_here))
    case("find: ledger_append keeps ./governance when it exists, else uses the found records' governance folder",
         kept[0] == 0 and (here / "governance" / "decisions.jsonl").exists() and viaenv[0] == 0 and viaenv[1] == "DEC-001"
         and (one / "records" / "governance" / "decisions.jsonl").exists(), str(kept) + str(viaenv))

    import zipfile
    cli = KIT / "tools" / "cli.py"
    z = at(TMP, cli, "zips", "--out", TMP / "zips-baked", "--owner", "Sam", "--records", "sam/records")
    names = {f.name: zipfile.ZipFile(f).namelist() for f in (TMP / "zips-baked").glob("*.zip")} if z[0] == 0 else {}
    inside = json.loads(zipfile.ZipFile(TMP / "zips-baked" / "handoff-ledger.zip").read("handoff-ledger/relay-kit.json")) if z[0] == 0 else {}
    bads = [at(TMP, cli, "zips", "--out", TMP / "zips-bad", "--records", v) for v in ("not a repo", "git" + "@x", "https://evil.example", "x/..")]
    case("find: zips --owner --records puts relay-kit.json in the handoff-ledger zip only, leaves the source alone, refuses bad values",
         z[0] == 0 and inside == {"owner": "sam", "records": "sam/records"}
         and sum("relay-kit.json" in " ".join(v) for v in names.values()) == 1
         and not (KIT / "skills" / "handoff-ledger" / "relay-kit.json").exists() and all(b[0] != 0 for b in bads),
         str(z) + str(names.keys()) + str(bads))

def output_name_tests():
    """check --base compares every copy of a handoff, wherever it's archived, and never takes
    outputs/<id>.md (same file name) for the handoff. Records main went red on
    HO-RK-20261103-5J0W's output, 2026-11-04; the scenarios are the two #15 reviews' probes."""
    text = FIXTURE.read_text(encoding="utf-8")
    edited = text.replace("\nto: ", "\nto: chatgpt:other-than-", 1)
    assert edited != text
    output = f"---\nid: {HID}\nrecipient: chatgpt:review\ndate: 2026-10-28\n---\n\n# Output\n\nDone.\n"
    def scenario(name, at_base, after):
        """at_base and after: {relative path: text}. Returns (exit code, output) of check --base."""
        g, c1, _ = git_world(f"out-{name}")
        d = c1 / "handoffs"
        for rel, body in at_base.items():
            (d / rel).parent.mkdir(parents=True, exist_ok=True); (d / rel).write_text(body, encoding="utf-8")
        g(c1, "add", "-A"); g(c1, "commit", "-qm", "base")
        base = g(c1, "rev-parse", "HEAD").stdout.strip()
        for rel in at_base:
            (d / rel).unlink()
        for rel, body in after.items():   # "-> path" makes a symlink to that path
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            if body.startswith("-> "):
                os.symlink(d / body[3:], d / rel)
            else:
                (d / rel).write_text(body, encoding="utf-8", errors="surrogateescape")
        g(c1, "add", "-A"); g(c1, "commit", "-qm", "after")
        r = run(LEDGER, "--dir", d, "check", "--base", base)
        return r[0], r[1] + r[2]
    top, arc, out = FIXTURE.name, f"archive/{FIXTURE.name}", f"outputs/{FIXTURE.name}"
    ok = {
        "output added": scenario("a", {top: text}, {top: text, out: output}),
        "archived unchanged, output present": scenario("c", {top: text}, {arc: text, out: output}),
        "archived into archive/outputs/": scenario("f", {top: text}, {f"archive/outputs/{FIXTURE.name}": text}),
        "archived under a name with a space": scenario("q", {f"closed history/{FIXTURE.name}": text}, {f"closed history/{FIXTURE.name}": text}),
    }
    caught = {
        "deleted, output kept": scenario("b", {top: text}, {out: output}),
        "deleted, output is a copy of it": scenario("b2", {top: text}, {out: text}),
        "edited, output present": scenario("k", {top: text, out: output}, {top: edited, out: output}),
        "top untouched, archived copy edited": scenario("g", {top: text}, {top: text, arc: edited}),
        "two archived copies, one edited (a/)": scenario("j", {top: text}, {f"a/{FIXTURE.name}": edited, f"z/{FIXTURE.name}": text}),
        "two archived copies, one edited (z/)": scenario("i", {top: text}, {f"a/{FIXTURE.name}": text, f"z/{FIXTURE.name}": edited}),
        "archived copy made unreadable": scenario("m", {top: text}, {arc: "no front matter\n"}),
        "archived at the base, then edited": scenario("n", {arc: text}, {arc: edited}),
        "archived at the base, then deleted": scenario("o", {arc: text}, {}),
        "archived under a name with a space, then edited": scenario("p", {f"closed history/{FIXTURE.name}": text},
                                                                    {f"closed history/{FIXTURE.name}": edited}),
        "archived under a non-ASCII name, then edited": scenario("q2", {f"arch\u00e9/{FIXTURE.name}": text}, {f"arch\u00e9/{FIXTURE.name}": edited}),
        "archived under a non-ASCII name, then deleted": scenario("r", {f"arch\u00e9/{FIXTURE.name}": text}, {}),
        "unreadable at the base, then rewritten": scenario("aa", {top: "no front matter\n"}, {top: "something else\n"}),
        "not UTF-8 any more": scenario("u", {arc: text}, {arc: "\udcff"}),
        "handoff replaced by a symlink to its output": scenario("w", {top: text}, {out: text, top: f"-> {out}"}),
    }
    case("ledger: check --base ignores outputs/<id>.md and accepts archiving: " + ", ".join(ok),
         all(r[0] == 0 for r in ok.values()), str({k: v for k, v in ok.items() if v[0] != 0}))
    case("ledger: check --base catches every rewrite or removal, whichever copy holds it: " + ", ".join(caught),
         all(r[0] != 0 for r in caught.values()), str({k: v for k, v in caught.items() if v[0] == 0}))

def export_tests(module="test_export"):
    """relay-kit/export@1, relay-kit/mcp@1 and relay-kit/prs@1: the unittest cases in tests/<module>.py, each reported by its docstring."""
    import unittest
    mod = load(module, KIT / "tests" / f"{module}.py")
    for t in unittest.defaultTestLoader.loadTestsFromModule(mod):
        for one in t:
            r = unittest.TestResult()
            with contextlib.redirect_stderr(io.StringIO()):
                one.run(r)
            case(one.shortDescription() or one.id(), r.wasSuccessful() and r.testsRun == 1,
                 "; ".join(e[1][-400:] for e in r.failures + r.errors))


def cockpit_tests():
    """relay-kit/cockpit@4: the unittest cases in tests/test_cockpit.py, each reported by its docstring."""
    import unittest
    mod = load("test_cockpit", KIT / "tests" / "test_cockpit.py")
    for t in unittest.defaultTestLoader.loadTestsFromModule(mod):
        for one in t:
            r = unittest.TestResult()
            with contextlib.redirect_stderr(io.StringIO()):
                one.run(r)
            case(one.shortDescription() or one.id(), r.wasSuccessful() and r.testsRun == 1,
                 "; ".join(e[1][-400:] for e in r.failures + r.errors))


if __name__ == "__main__":
    load("tools_extract", KIT / "tools_extract.py").extract(X)
    packaging_tests()
    ledger_tests()
    (TMP / "fixture.raw.json").write_text(json.dumps(wrap(chatgpt_fixture())), encoding="utf-8")
    capture_tests()
    live_fixture_tests()
    schema_tests()
    governance_tests()
    status_tests()
    coordination_tests()
    learning_tests()
    deploy_tests()
    finding_tests()
    output_name_tests()
    telemetry_tests()
    evidence_tests()
    export_tests()
    cockpit_tests()
    export_tests("test_merge_preflight")
    export_tests("test_mcp")
    export_tests("test_prs")
    export_tests("test_lifecycle")
    export_tests("test_github")
    export_tests("test_budget")
    export_tests("test_usage")
    export_tests("test_telemetry")
    export_tests("test_comments")
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed  (scratch: {TMP})")
    sys.exit(1 if failed else 0)
