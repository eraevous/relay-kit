"""Tests for tools/cockpit.py (relay-kit/cockpit@5). Standard library only; synthetic data only.

Run alone with `python3 -m unittest discover -s tests`, or as part of tests/run_tests.py, which
reports each test by the first line of its docstring ("cockpit: ...")."""
import hashlib, importlib.util, json, os, pathlib, re, shlex, shutil, subprocess, sys, tempfile, unittest

KIT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("relaykit_cockpit_under_test", KIT / "tools" / "cockpit.py")
ck = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ck)

HOSTILE = "</textarea></script><script>alert(1)</script><img src=x onerror=alert(2)> \"q\" 'a' & é"
BODY_DEC = "BODYDEC Adopt the synthetic plan."
BODY_CH = "BODYCH The sample may drift from the plan."
TITLE_HO = "BODYHO Draft the synthetic report"


def handoff(hid, status, frm, to, title, authorized, log):
    return f"""---
id: {hid}
title: {title}
from: {frm}
to: {to}
created: {log[0][0]}
status: {status}
contract: relay-kit/handoff@2
source: none
last_processed: none
reply_to:
---

## Task
Synthetic task body BODYTASK.

## Authorized
{authorized}

## Proposals
- none

## Context
- none

## Expected output
Write the result to outputs/{hid}.md.

## Limits
- none

## Log
""" + "".join(f"- {at} | {st} | {who} | {note}\n" for at, st, who, note in log)


HO_DONE = handoff("HO-RS-20261101-AB12", "completed", "claude:research", "chatgpt:review", "Review the synthetic draft",
                  '- Quote: "go ahead" (sam, synthetic, 2026-11-01)',
                  [("2026-11-01T12:00Z", "prepared", "claude:research#SESSA1", "drafted"),
                   ("2026-11-01T12:05Z", "sent", "claude:research#SESSA1", "pasted"),
                   ("2026-11-01T13:00Z", "completed", "claude:research#SESSA1 for chatgpt:review", "outputs/HO-RS-20261101-AB12.md")])
HO_WAIT = handoff("HO-RS-20261103-CD34", "prepared", "claude:research", "chatgpt:review", TITLE_HO,
                  "- TODO: the owner's words",
                  [("2026-11-03T09:00Z", "prepared", "claude:research#SESSA1", "drafted")])
HO_SENT = handoff("HO-RV-20261103-EF56", "sent", "claude:review", "claude:research", "Check the figures",
                  '- Quote: "yes" (sam, synthetic, 2026-11-03)',
                  [("2026-11-03T10:00Z", "prepared", "claude:review#SESSR9", "drafted"),
                   ("2026-11-03T10:05Z", "sent", "claude:review#SESSR9", "pasted")])

DECISIONS = [
    {"id": "DEC-001", "date": "2026-11-01", "status": "adopted", "decision": "The owner delegates merging research PRs to the research lane.",
     "scope": "research", "decider": "sam", "basis": "\"yes\"", "locator": "synthetic chat", "recorded_by": "claude:research#SESSA1"},
    {"id": "DEC-002", "date": "2026-11-02", "status": "proposed", "decision": BODY_DEC + " " + HOSTILE, "scope": "research",
     "proposed_by": "claude:research#SESSA1", "recorded_by": "claude:research#SESSA1"},
    {"id": "DEC-003", "date": "2026-11-02", "status": "proposed", "decision": "Settled already.", "scope": "research",
     "recorded_by": "claude:research#SESSA1"},
    {"id": "DEC-004", "date": "2026-11-03", "status": "adopted", "supersedes": "DEC-003", "decision": "Settled already.",
     "scope": "research", "decider": "sam", "basis": "\"ok\"", "locator": "synthetic chat", "recorded_by": "sam"},
]
RECORDED = {"id": "DEC-005", "date": "2026-11-03", "status": "adopted", "decision": "Review may close its own handoffs.",
            "scope": "review", "delegates_to": "review", "decider": "sam", "basis": "\"fine\"", "locator": "synthetic chat",
            "recorded_by": "sam"}
CHALLENGES = [
    {"id": "CH-001", "date": "2026-11-02", "status": "open", "concern": BODY_CH, "raised_by": "claude:review#SESSR9"},
    {"id": "CH-002", "date": "2026-11-02", "status": "open", "concern": "Closed later.", "raised_by": "claude:review#SESSR9"},
    {"id": "CH-003", "date": "2026-11-03", "status": "resolved", "resolves": "CH-002", "concern": "Closed later.",
     "raised_by": "claude:review#SESSR9", "resolution": "fixed", "recorded_by": "sam"},
]
THREADS = """# Coordination index

## Lanes

### Research
| | Where | Status |
|---|---|---|
| Ada (ChatGPT) | [RS] coord: https://chatgpt.com/c/00000000-0000-0000-0000-00000000000a | **active** |

### Review
| | Where | Status |
|---|---|---|
| Claude | review session: https://claude.ai/code/session_SESSR9 | active |

## Intake
- idea intake thread: https://chatgpt.com/c/00000000-0000-0000-0000-0000000000ff

## Change log
- 2026-11-03 | claude:research#SESSA1 | PR https://github.com/example/synthetic/pull/7 opened
"""
BUDGET = [
    {"ts": "2026-11-03T08:00:00Z", "actor": "claude:research#SESSA1", "window": "session", "tokens_in": 1000, "tokens_out": 500, "agents": 2, "note": "n1"},
    {"ts": "2026-10-20T08:00:00Z", "actor": "claude:research#SESSA1", "window": "session", "tokens_in": 9999, "tokens_out": 0, "agents": 1, "note": "old"},
]


def build_root(base, budget=False, recorded=False):
    r = pathlib.Path(base) / "records"
    (r / "handoffs" / "outputs").mkdir(parents=True)
    (r / "governance").mkdir()
    (r / "handoffs" / ".ledger.json").write_text(json.dumps({"owner": "sam", "lanes": {"research": "RS", "review": "RV"}}), encoding="utf-8")
    for text, hid in ((HO_DONE, "HO-RS-20261101-AB12"), (HO_WAIT, "HO-RS-20261103-CD34"), (HO_SENT, "HO-RV-20261103-EF56")):
        (r / "handoffs" / f"{hid}.md").write_text(text, encoding="utf-8")
    (r / "handoffs" / "outputs" / "HO-RS-20261101-AB12.md").write_text("HO-RS-20261101-AB12 · synthetic\n\nok\n", encoding="utf-8")
    (r / "governance" / "decisions.jsonl").write_text("".join(json.dumps(d) + "\n" for d in DECISIONS + ([RECORDED] if recorded else [])), encoding="utf-8")
    (r / "governance" / "challenges.jsonl").write_text("".join(json.dumps(c) + "\n" for c in CHALLENGES), encoding="utf-8")
    (r / "threads.md").write_text(THREADS, encoding="utf-8")
    if budget:
        (r / "governance" / "budget.jsonl").write_text("".join(json.dumps(b) + "\n" for b in BUDGET) + "not json\n", encoding="utf-8")
    return r


def tree_hash(root):
    h = hashlib.sha256()
    for p in sorted(pathlib.Path(root).rglob("*")):
        h.update(p.relative_to(root).as_posix().encode())
        if p.is_file():
            h.update(p.read_bytes())
            h.update(str(p.stat().st_mtime_ns).encode())
    return h.hexdigest()


class CockpitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="relaykit-cockpit-"))
        self.root = build_root(self.tmp)
        self._vis = ck.ex.repo_visibility
        ck.ex.repo_visibility = lambda repo: "unknown"   # no network in tests

    def tearDown(self):
        ck.ex.repo_visibility = self._vis
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, name="out", root=None, **kw):
        out = self.tmp / name
        data = ck.build(root or self.root, out, **kw)
        return data, (out / "index.html").read_text(encoding="utf-8"), (out / "cockpit.json").read_text(encoding="utf-8")

    def test_page_builds(self):
        """cockpit: the page builds self-contained, with every surface, and its data matches cockpit.json"""
        data, page, js = self.build()
        self.assertEqual(json.loads(js)["schema"], "relay-kit/cockpit@5")
        for sid in ("overview", "intake", "questions", "choices", "topology", "budget", "lanes", "run", "review", "prs", "status"):
            self.assertIn(f'id="{sid}"', page)
        for bad in ("<link", "src=\"http", "src='http", "@import", "url(http"):
            self.assertNotIn(bad, page)
        self.assertEqual(page.count("<script"), 1)
        self.assertIn("newest record 2026-11-03T10:05Z", page)   # the latest record, not the clock
        self.assertIn("not wired to records yet".upper(), page)
        self.assertIn("https://chatgpt.com/c/00000000-0000-0000-0000-0000000000ff", page)   # intake link from threads.md
        self.assertEqual(data["run"]["pr_links"], ["https://github.com/example/synthetic/pull/7"])

    def test_choices_exact(self):
        """cockpit: Choices lists exactly the proposed decision, the owner-waiting handoff and the open challenge"""
        data, page, _ = self.build()
        got = sorted((c["kind"], c["id"]) for c in data["choices"])
        self.assertEqual(got, [("challenge", "CH-001"), ("decision", "DEC-002"), ("handoff", "HO-RS-20261103-CD34")])
        self.assertEqual(page.count('class="card choice"'), 3)

    def test_composed_line(self):
        """cockpit: a composed ledger line parses as JSON with the referenced id and the chosen status, and passes the ledger check"""
        data, _, _ = self.build()
        dec = next(c for c in data["choices"] if c["id"] == "DEC-002")
        ch = next(c for c in data["choices"] if c["id"] == "CH-001")
        for item, action, key, status in ((dec, "accept", "supersedes", "adopted"), (dec, "reject", "supersedes", "rejected"),
                                          (ch, "accept", "resolves", "accepted-risk"), (ch, "reject", "resolves", "resolved")):
            argv = shlex.split(item["compose"][action]["command"])
            self.assertEqual(argv[:5], ["relay-kit", "ledger", "append", "--dir", "governance"])
            obj = json.loads(argv[-1])
            self.assertEqual((obj[key], obj["status"]), (item["id"], status))
            self.assertEqual(obj, item["compose"][action]["json"])
        self.assertTrue(dec["compose"]["defer"]["command"].startswith("# DEC-002"))
        ho = next(c for c in data["choices"] if c["kind"] == "handoff")
        self.assertIn("set HO-RS-20261103-CD34 sent --note", ho["compose"]["accept"]["command"])
        self.assertIn("set HO-RS-20261103-CD34 cancelled --note", ho["compose"]["reject"]["command"])
        self.assertNotIn("--by", ho["compose"]["accept"]["command"])   # the actor comes from whoever runs it
        acc = json.loads(shlex.split(dec["compose"]["accept"]["command"])[-1])
        self.assertNotIn("recorded_by", acc)
        for f in ("decider", "basis", "locator"):
            self.assertIn("<<REQUIRED", acc[f])
        # once the owner replaces the placeholders, the line (with the id and date ledger_append fills) is valid
        g = self.tmp / "gov"
        g.mkdir()
        filled = dict(acc, id="DEC-005", date="2026-11-04", decider="sam", basis='"adopt it"', locator="synthetic chat", recorded_by="sam")
        (g / "decisions.jsonl").write_text("".join(json.dumps(d) + "\n" for d in DECISIONS + [filled]), encoding="utf-8")
        (g / "challenges.jsonl").write_text("".join(json.dumps(c) + "\n" for c in CHALLENGES), encoding="utf-8")
        p = subprocess.run([sys.executable, "-I", str(KIT / "tools" / "check_ledgers.py"), "--dir", str(g)], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_unedited_paste_refused(self):
        """cockpit: a composed line pasted unedited is refused by ledger_append (its <<REQUIRED placeholders fail the ledger check)"""
        data, _, _ = self.build()
        rec = self.tmp / "rec"
        (rec / "governance").mkdir(parents=True)
        shutil.copy(self.root / "governance" / "decisions.jsonl", rec / "governance")
        shutil.copy(self.root / "governance" / "challenges.jsonl", rec / "governance")
        env = dict(os.environ, RELAYKIT_ACTOR="sam")
        ident = ["-c", "user.name=t", "-c", "user.email=t"]   # a throwaway identity for the synthetic commit
        for cmd in (["git", "init", "-q", "-b", "main"], ["git", "add", "."], ["git"] + ident + ["commit", "-q", "-m", "synthetic"]):
            subprocess.run(cmd, cwd=rec, check=True, capture_output=True, env=env)
        before = (rec / "governance" / "decisions.jsonl").read_bytes(), (rec / "governance" / "challenges.jsonl").read_bytes()
        for item in data["choices"]:
            if item["kind"] == "handoff":
                continue
            for action in ("accept", "reject"):
                argv = shlex.split(item["compose"][action]["command"])
                p = subprocess.run([sys.executable, "-I", str(KIT / "tools" / "ledger_append.py"), "--offline"] + argv[3:],
                                   cwd=rec, capture_output=True, text=True, env=env)
                self.assertNotEqual(p.returncode, 0, f"{item['id']} {action}: {p.stdout}")
                self.assertIn("<<REQUIRED", p.stdout + p.stderr)
        self.assertEqual(before, ((rec / "governance" / "decisions.jsonl").read_bytes(), (rec / "governance" / "challenges.jsonl").read_bytes()))

    def test_run_commitments(self):
        """cockpit: Run commitments count only authorized handoffs in progress; prepared or unauthorized ones are pending"""
        data, page, _ = self.build()
        c, com = data["counts"], data["run"]["commitments"]
        self.assertEqual((c["authorized_in_progress"], c["pending_authorization"]), (1, 1))
        self.assertEqual([x["id"] for x in com["authorized"]], ["HO-RV-20261103-EF56"])
        self.assertEqual([x["id"] for x in com["pending"]], ["HO-RS-20261103-CD34"])
        self.assertIn("run commitments / authorized", page)

    def test_topology_edges(self):
        """cockpit: information topology handoff edges match the planted handoffs; delegations are inferred from decisions"""
        data, page, _ = self.build()
        edges = {(x["from"], x["to"], tuple(x["refs"])) for x in data["topology"]["information"]["edges"] if x["type"] == "handoff"}
        self.assertEqual(edges, {("claude:research", "chatgpt:review", ("HO-RS-20261101-AB12", "HO-RS-20261103-CD34")),
                                 ("claude:review", "claude:research", ("HO-RV-20261103-EF56",))})
        auth = data["topology"]["authority"]
        self.assertEqual(auth["recorded"]["edges"], [])   # no decision carries delegates_to
        self.assertIn("No recorded delegation", page)
        dele = [x for x in auth["hypothesised"]["edges"] if x["kind"] == "delegation"]
        self.assertEqual([(x["decision"], x["to"], x["inferred"]) for x in dele], [("DEC-001", "lane:research", True)])
        self.assertIn('<details class="hyp" id="hyp">', page)   # inferred overlay: closed unless opened
        self.assertNotIn('<details class="hyp" id="hyp" open', page)
        self.assertFalse(auth["executable"])

    def test_recorded_delegation(self):
        """cockpit: only an adopted decision with delegates_to draws a recorded delegation edge"""
        root = build_root(self.tmp / "r", recorded=True)
        data, page, _ = self.build(root=root)
        rec = data["topology"]["authority"]["recorded"]["edges"]
        self.assertEqual([(x["decision"], x["from"], x["to"], x["inferred"]) for x in rec], [("DEC-005", "owner", "lane:review", False)])
        self.assertNotIn("No recorded delegation", page)

    def test_budget_absent(self):
        """cockpit: without budget.jsonl the gauge says there are no budget records yet and shows the schema"""
        data, page, _ = self.build()
        self.assertFalse(data["budget"]["present"])
        self.assertIn("no budget records yet", page)
        self.assertIn("tokens_out", page)

    def test_budget_present(self):
        """cockpit: with budget.jsonl the gauge shows 7-day burn against the weekly allowance, by lane and actor"""
        root = build_root(self.tmp / "b", budget=True)
        data, page, _ = self.build(root=root, weekly_tokens=10000)
        b = data["budget"]
        self.assertEqual((b["burn_7d"], b["fraction"], b["records"]), (1500, 0.15, 2))
        self.assertIsNone(b["weekly_reference"])
        self.assertEqual(b["by_lane"], {"research": 1500})
        self.assertEqual([x["line"] for x in b["invalid_lines"]], [3])
        self.assertFalse(b["status_file_costs_parsed"])
        self.assertIn("15%", page)

    def test_budget_weekly_reference(self):
        """cockpit: a weekly reading is a labelled reference and never replaces the session sum; no session lines means no reading, not zero"""
        root = build_root(self.tmp / "w")
        (root / "governance" / "budget.jsonl").write_text(json.dumps(
            {"ts": "2026-11-03T09:00:00Z", "actor": "sam", "window": "weekly", "tokens_in": 7000, "tokens_out": 0, "agents": None, "note": "plan page"}) + "\n", encoding="utf-8")
        data, page, _ = self.build(root=root, weekly_tokens=10000)
        b = data["budget"]
        self.assertIsNone(b["burn_7d"])
        self.assertIsNone(b["fraction"])
        self.assertEqual(b["weekly_reference"]["tokens"], 7000)
        self.assertIn("no session lines in the window", page)
        self.assertIn("reference only", page)

    def test_budget_append(self):
        """cockpit: budget-append adds one valid line the cockpit reads"""
        g = self.root / "governance"
        p = subprocess.run([sys.executable, "-I", str(KIT / "tools" / "budget_append.py"), "--dir", str(g), "--window", "session",
                            "--tokens-in", "10", "--tokens-out", "5", "--method", "measured", "--actor", "claude:research#SESSA1",
                            "--ts", "2026-11-03T11:00:00Z"], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        data, _, _ = self.build()
        self.assertEqual((data["budget"]["records"], data["budget"]["burn_7d"]), (1, 15))

    def test_metadata_mode(self):
        """cockpit: --content metadata leaves out decision, challenge and handoff bodies (ids and states only)"""
        data, page, js = self.build(content="metadata")
        for text in (page, js):
            for body in ("BODYDEC", "BODYCH", "BODYHO", "BODYTASK", "alert(1)"):
                self.assertNotIn(body, text)
        self.assertIn("DEC-002", page)
        self.assertIn("HO-RS-20261103-CD34", page)

    def test_hostile_strings(self):
        """cockpit: hostile strings in the records are escaped in the page"""
        _, page, _ = self.build()
        self.assertNotIn("<script>alert(1)", page)
        self.assertNotIn("<img src=x", page)
        self.assertNotIn("</textarea></script>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertEqual(page.count("<script"), 1)

    def test_refuse_public(self):
        """cockpit: a destination inside a public GitHub repository or inside the records is refused"""
        repo = self.tmp / "pub"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        with open(repo / ".git" / "config", "a", encoding="utf-8") as f:
            f.write('[remote "origin"]\n\turl = https://github.com/example/public-repo.git\n')
        ck.ex.repo_visibility = lambda r: "public"
        with self.assertRaises(ck.Refused):
            ck.build(self.root, repo / "site")
        self.assertFalse((repo / "site").exists())
        with self.assertRaises(ck.Refused):
            ck.build(self.root, self.root / "site")

    def test_handoff_accept_is_legal(self):
        """cockpit: a handoff's Accept is the next legal state (handoff.py rules): owner-addressed running handoffs move on, a blocked one returns to the state it left"""
        q = '- Quote: "go" (sam, synthetic, 2026-11-04)'
        planted = {   # id: (status, log states after prepared/sent, recipient, expected Accept state)
            "HO-RV-20261104-AA01": ("acknowledged", ["acknowledged"], "sam", "in_progress"),
            "HO-RV-20261104-AA02": ("in_progress", ["acknowledged", "in_progress"], "sam", "completed"),
            "HO-RV-20261104-AA03": ("blocked", ["blocked"], "claude:research", "sent"),
            "HO-RV-20261104-AA04": ("blocked", ["acknowledged", "blocked"], "claude:research", "acknowledged"),
            "HO-RV-20261104-AA05": ("blocked", ["acknowledged", "in_progress", "blocked"], "claude:research", "in_progress"),
        }
        for hid, (st, states, to, _) in planted.items():
            log = [("2026-11-04T09:00Z", "prepared", "claude:review#SESSR9", "drafted"),
                   ("2026-11-04T09:01Z", "sent", "claude:review#SESSR9", "pasted")]
            log += [(f"2026-11-04T09:1{i}Z", s, "sam", "synthetic step") for i, s in enumerate(states)]
            (self.root / "handoffs" / f"{hid}.md").write_text(handoff(hid, st, "claude:review", to, "Synthetic", q, log), encoding="utf-8")
        data, _, _ = self.build()
        led = ck.ex.load_ledger()
        got = {c["id"]: c for c in data["choices"] if c["kind"] == "handoff"}
        for hid, (st, states, _, want) in planted.items():
            acc = got[hid]["compose"]["accept"]
            self.assertEqual(acc["state"], want, hid)
            self.assertIsNone(led.legal(["prepared", "sent"] + states, acc["state"]), hid)
            self.assertIn(f"set {hid} {want} --note", acc["command"])
        # completing needs the output file first: the line says so until it exists
        self.assertIn("handoffs/outputs/HO-RV-20261104-AA02.md", got["HO-RV-20261104-AA02"]["compose"]["accept"]["command"])
        (self.root / "handoffs" / "outputs" / "HO-RV-20261104-AA02.md").write_text("HO-RV-20261104-AA02\n\nok\n", encoding="utf-8")
        data, _, _ = self.build("again")
        acc = next(c for c in data["choices"] if c["id"] == "HO-RV-20261104-AA02")["compose"]["accept"]
        self.assertNotIn("# first write the result", acc["command"])

    def add_lanes_and_capture(self):
        """Plant a lane whose only activity is a closed challenge, and a capture whose last message is the newest record."""
        cfg = self.root / "handoffs" / ".ledger.json"
        cfg.write_text(json.dumps({"owner": "sam", "lanes": {"research": "RS", "review": "RV", "audit": "AU", "notes": "NT"}}), encoding="utf-8")
        with open(self.root / "governance" / "challenges.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"id": "CH-010", "date": "2026-11-02", "status": "open", "concern": "Audit gap.", "raised_by": "claude:audit#SESSAU"}) + "\n")
            f.write(json.dumps({"id": "CH-011", "date": "2026-11-04", "status": "resolved", "resolves": "CH-010", "concern": "Audit gap.",
                                "raised_by": "claude:audit#SESSAU", "resolution": "fixed", "recorded_by": "claude:audit#SESSAU"}) + "\n")
        msgs = lambda role, mid, ts, text: {"n": 1, "role": role, "label": role, "messages": [
            {"mid": mid, "id": mid.lower(), "ts": ts, "model": None, "dictated": False, "text": text, "sources": []}]}
        (self.root / "captures").mkdir()
        (self.root / "captures" / "notes.json").write_text(json.dumps({
            "schema": "relay-kit/conversation@1",
            "meta": {"title": "Synthetic notes", "platform": "chatgpt", "conversation_id": "00000000-0000-0000-0000-0000000000c1",
                     "source_url": "https://chatgpt.com/c/00000000-0000-0000-0000-0000000000c1", "checks": ["ok"],
                     "actor": "claude:notes#SESSNT", "lane": "notes", "raw_sha256": "0" * 64},
            "hidden_counts": {}, "turns": [msgs("user", "T1u", "2026-11-01T08:00:00Z", "Start."),
                                           msgs("assistant", "T1a", "2026-11-05T09:00:00Z", "Much later.")]}), encoding="utf-8")

    def test_as_of_capture_end(self):
        """cockpit: the as-of date includes a capture's last message, not only its first"""
        self.add_lanes_and_capture()
        data, page, _ = self.build()
        self.assertEqual(data["as_of"], "2026-11-05T09:00Z")
        self.assertIn("newest record 2026-11-05T09:00Z", page)

    def test_lane_last_activity(self):
        """cockpit: a lane's last activity includes handoff log updates, closed challenges and capture end times"""
        self.add_lanes_and_capture()
        data, _, _ = self.build()
        last = {r["lane"]: r["last_activity"] for r in data["lanes"]}
        self.assertEqual(last["review"], "2026-11-03T10:05Z")   # HO-RV-...-EF56 was sent at 10:05, created at 10:00
        self.assertEqual(last["audit"], "2026-11-04T00:00Z")    # its challenge was closed on 2026-11-04
        self.assertEqual(last["notes"], "2026-11-05T09:00Z")    # the capture's last message

    def test_lane_filter_keeps_settled(self):
        """cockpit: with --lane, a proposal superseded or a challenge resolved from another lane stays settled (not in Choices or Drift)"""
        with open(self.root / "governance" / "challenges.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"id": "CH-020", "date": "2026-10-20", "status": "open", "concern": "drift in research", "raised_by": "claude:research#SESSA1"}) + "\n")
            f.write(json.dumps({"id": "CH-021", "date": "2026-11-03", "status": "resolved", "resolves": "CH-020", "concern": "drift in research",
                                "raised_by": "claude:review#SESSR9", "resolution": "fixed", "recorded_by": "claude:review#SESSR9"}) + "\n")
        data, _, _ = self.build(lane="research", drift_days=0)
        ids = {c["id"] for c in data["choices"]}
        self.assertNotIn("DEC-003", ids)   # superseded by DEC-004, recorded by the owner (no lane)
        self.assertNotIn("CH-020", ids)    # resolved by CH-021, recorded in the review lane
        self.assertIn("DEC-002", ids)
        drift = data["run"]["drift"]
        self.assertNotIn("DEC-003", [x["id"] for x in drift["stale_proposals"]])
        self.assertEqual([x["id"] for x in drift["challenges_mentioning_drift"] if x["open"]], [])
        self.assertEqual(data["counts"]["drift_flags"], len(drift["stale_proposals"]))

    def test_dialog_accessible_name(self):
        """cockpit: the confirmation dialog is named and described by its dynamic title and body"""
        _, page, _ = self.build()
        m = re.search(r"<dialog [^>]*>", page)
        self.assertIsNotNone(m)
        self.assertIn('aria-labelledby="settle-title"', m.group(0))
        self.assertIn('aria-describedby="settle-body"', m.group(0))
        self.assertIn('id="settle-title"', page)
        self.assertIn('id="settle-body"', page)

    def test_deploy_order(self):
        """cockpit: the deploy steps in docs/cockpit.md turn on and verify access control before any records are uploaded"""
        doc = (KIT / "docs" / "cockpit.md").read_text(encoding="utf-8")
        amp = doc[doc.index("AWS Amplify Hosting"):doc.index("Rebuild and redeploy")]
        upload = amp.index("drag cockpit.zip")
        self.assertLess(amp.index("placeholder.zip"), amp.index("Access control"))
        self.assertLess(amp.index("Access control"), upload)
        self.assertLess(amp.index("401"), upload)   # verified, not just switched on
        s3 = doc[doc.index("AWS S3"):doc.index("AWS Amplify Hosting")]
        self.assertLess(s3.index("get-public-access-block"), s3.index("aws s3 sync"))

    def test_deterministic(self):
        """cockpit: two runs give byte-identical output and leave the records untouched"""
        before = tree_hash(self.root)
        _, p1, j1 = self.build("one")
        _, p2, j2 = self.build("two")
        self.assertEqual((p1, j1), (p2, j2))
        self.assertEqual(before, tree_hash(self.root))


ASKED = {"id": "DEC-006", "date": "2026-11-03", "status": "proposed", "decision": "Use the weekly survey.", "scope": "research",
         "proposed_by": "claude:research#SESSA1", "recorded_by": "claude:research#SESSA1",
         "ask": {"question": "Run the survey weekly instead of monthly?",
                 "options": [{"label": "Accept", "effect": "the survey runs every Monday"},
                             {"label": "Reject", "effect": "it stays monthly"}],
                 "default_if_unanswered": "it stays monthly"}}
CITED = {"id": "DEC-007", "date": "2026-11-03", "status": "proposed", "decision": "Merge https://github.com/example/synthetic/pull/7 first.",
         "scope": "research", "recorded_by": "claude:research#SESSA1"}
CITER = {"id": "DEC-008", "date": "2026-11-04", "status": "adopted", "decision": "The merge order in DEC-007 was carried out.",
         "scope": "research", "decider": "sam", "basis": "\"done\"", "locator": "synthetic chat", "recorded_by": "sam"}


class AskTests(unittest.TestCase):
    """HO-RK-20261105-VZH4: what each choice asks, staleness, and Amend / Clarify / Inquire."""
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="relaykit-cockpit-ask-"))
        self.root = build_root(self.tmp, recorded=True)   # DEC-001..005, so the new lines continue at DEC-006
        with open(self.root / "governance" / "decisions.jsonl", "a", encoding="utf-8") as f:
            for d in (ASKED, CITED, CITER):
                f.write(json.dumps(d) + "\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, name="out", **kw):
        out = self.tmp / name
        data = ck.build(self.root, out, **kw)
        return data, (out / "index.html").read_text(encoding="utf-8")

    def item(self, data, cid):
        return next(c for c in data["choices"] if c["id"] == cid)

    def check(self, decisions=(), challenges=()):
        g = self.tmp / f"gov{len(list(self.tmp.iterdir()))}"
        g.mkdir()
        base_d = [json.loads(x) for x in (self.root / "governance" / "decisions.jsonl").read_text().splitlines()]
        base_c = [json.loads(x) for x in (self.root / "governance" / "challenges.jsonl").read_text().splitlines()]
        (g / "decisions.jsonl").write_text("".join(json.dumps(d) + "\n" for d in base_d + list(decisions)), encoding="utf-8")
        (g / "challenges.jsonl").write_text("".join(json.dumps(c) + "\n" for c in base_c + list(challenges)), encoding="utf-8")
        return subprocess.run([sys.executable, "-I", str(KIT / "tools" / "check_ledgers.py"), "--dir", str(g)], capture_output=True, text=True)

    def test_ask_written_or_derived(self):
        """cockpit: every choice says what it asks; the proposer's ask is shown as theirs, anything else is labelled derived"""
        data, page = self.build()
        a = self.item(data, "DEC-006")["ask"]
        self.assertEqual((a["source"], a["question"], a["by"]), ("proposer", "Run the survey weekly instead of monthly?", "claude:research#SESSA1"))
        self.assertEqual([o["label"] for o in a["proposer_options"]], ["Accept", "Reject"])
        # what the buttons do is the page's own description, whatever the proposer wrote
        self.assertIn("supersedes DEC-006", next(o["effect"] for o in self.item(data, "DEC-006")["effects"] if o["label"] == "Accept"))
        self.assertIn("THE PROPOSER'S EXPLANATION", page)
        d = self.item(data, "DEC-002")["ask"]
        self.assertEqual(d["source"], "derived")
        self.assertTrue(d["question"].endswith("?"))
        self.assertIn("Amend", [o["label"] for o in self.item(data, "DEC-002")["effects"]])
        for c in data["choices"]:
            self.assertTrue(c["ask"]["question"].endswith("?"), c["id"])
        self.assertIn("not written by the proposer", page)
        self.assertIn("written by claude:research#SESSA1", page)
        meta, mpage = self.build("meta", content="metadata")
        self.assertEqual(self.item(meta, "DEC-002")["ask"]["question"], "Should DEC-002 be adopted?")
        mjson = (self.tmp / "meta" / "cockpit.json").read_text(encoding="utf-8")
        for secret in ("Use the weekly survey.", "Run the survey weekly", "the survey runs every Monday", "it stays monthly"):
            self.assertNotIn(secret, mpage)   # the proposer's ask is free text: left out in metadata mode
            self.assertNotIn(secret, mjson)
        self.assertEqual(self.item(meta, "DEC-006")["ask"]["source"], "derived")

    def test_proposer_options_outside_mechanics(self):
        """cockpit: the proposer's options and default are visible in the open, before the authored notes and facts, never inside the ledger-mechanics disclosure; what the buttons do stays inside it (relay-kit #33 review, Kate 2026-11-08)"""
        _, page = self.build()
        # DEC-006 is the fixture with two proposer-authored options and a default (ASKED)
        start = page.index('data-key="decision:DEC-006"')
        card = page[start:page.index("</article>", start)]
        mech_open = card.index('<details class="mech">')
        mech = card[mech_open:card.index("</details>", mech_open)]
        theirs = card.index("THE PROPOSER'S EXPLANATION OF THE OPTIONS")
        # (1) both options, the default and the author label are in the open, outside any disclosure
        self.assertLess(theirs, mech_open)
        self.assertNotIn("<details", card[:theirs])
        for text in ("<b>Accept</b>: the survey runs every Monday", "<b>Reject</b>: it stays monthly", "If unanswered: it stays monthly",
                     "claude:research#SESSA1 · their words, not what the buttons do"):
            self.assertLess(card.index(text), mech_open, text)
        # (2) none of it is nested under the mechanics disclosure
        for text in ("the survey runs every Monday", "PROPOSER", 'class="effects theirs"'):
            self.assertNotIn(text, mech, text)
        # (3) the canonical button effects remain inside the disclosure, under their own label, and the page's
        #     own default-if-unanswered with them; the proposer's list is marked as theirs
        self.assertIn("WHAT EACH ACTION DOES (THIS PAGE)", mech)
        self.assertIn("supersedes DEC-006", mech)
        self.assertIn('<p class="dflt">If nobody answers:', mech)
        self.assertIn('<div class="proposer-options"><p class="mini-label theirs">', card)
        # reading order: question, the proposer's options, the authored notes, the facts, then mechanics
        self.assertLess(card.index('<p class="q">'), theirs)
        self.assertLess(theirs, card.index("DECISION SUPPORT"))
        self.assertLess(card.index("DECISION SUPPORT"), card.index("<dt>age</dt>"))
        self.assertLess(card.index("<dt>age</dt>"), mech_open)
        # (4) the disclosure is a plain <details>: no script touches the proposer's block, so toggling it changes nothing outside
        self.assertNotIn("proposer-options", page[page.index("<script"):])
        # (5) metadata mode still omits the free-text option bodies, and the block with them
        _, mpage = self.build("meta", content="metadata")
        self.assertNotIn("PROPOSER'S EXPLANATION", mpage)
        self.assertNotIn("the survey runs every Monday", mpage)

    def test_overtaken_by_later_citation(self):
        """cockpit: a later record that cites a choice flags it possibly overtaken, with the reason; an open one is listed as waiting on it"""
        data, page = self.build()
        c = self.item(data, "DEC-007")
        self.assertEqual(len(c["possibly_overtaken"]), 1)
        self.assertTrue(c["possibly_overtaken"][0].startswith("decision DEC-008 (adopted): “"), c["possibly_overtaken"])
        self.assertIn("carried out", c["possibly_overtaken"][0])   # the words that triggered it are shown
        self.assertIn("POSSIBLY OVERTAKEN", page)
        self.assertIn("Close as overtaken", page)
        self.assertEqual(self.item(data, "DEC-006")["possibly_overtaken"], [])
        over = json.loads(shlex.split(c["compose"]["overtaken"]["command"])[-1])
        self.assertEqual((over["status"], over["supersedes"]), ("superseded", "DEC-007"))   # closes it; adopts nothing
        self.assertIn("<<REQUIRED", over["decision"])   # the owner says what overtook it
        self.assertEqual(data["counts"]["possibly_overtaken"], 1)

    def test_pr_state_is_opt_in(self):
        """cockpit: pull request states are looked up only with --pr-state, at build time; a merged PR flags the choice and the page says when it looked"""
        calls = []
        def fake(owner, repo, n):
            calls.append((owner, repo, n))
            return "merged"
        data, _ = self.build()
        self.assertEqual((calls, self.item(data, "DEC-007")["prs"], data["pr_state"]),
                         ([], [{"pr": "example/synthetic#7", "state": None, "source": None}], {"looked_up": False}))   # named, not looked up
        self.assertEqual(len(self.item(data, "DEC-007")["possibly_overtaken"]), 1)
        data, _ = self.build("pr", pr_state=True, pr_lookup=fake)
        self.assertEqual(calls, [("example", "synthetic", 7)])
        c = self.item(data, "DEC-007")
        self.assertEqual([(x["pr"], x["state"], x["source"].split(" (")[0]) for x in c["prs"]],
                         [("example/synthetic#7", "merged", "gh at build time")])
        self.assertIn("example/synthetic#7 is merged", c["possibly_overtaken"])
        self.assertIn("Close as overtaken", [o["label"] for o in c["effects"]])
        self.assertTrue(data["pr_state"]["looked_up"] and data["pr_state"]["fetched_at"])
        # a bare "<repo> PR #N" counts only for a repository the records spell out as owner/repo
        self.assertEqual(ck.pr_refs("see library PR #71 and Telosmith PR #5", "sam", {"telosmith"}), [("sam", "Telosmith", 5)])
        # a PR named only in the proposer's ask (or the scope) is found too
        with open(self.root / "governance" / "decisions.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"id": "DEC-009", "date": "2026-11-05", "status": "proposed", "decision": "Ship it.", "scope": "release",
                                "recorded_by": "claude:research#SESSA1",
                                "ask": {"question": "Merge https://github.com/example/synthetic/pull/9?",
                                        "options": [{"label": "Accept", "effect": "merged"}], "default_if_unanswered": "open"}}) + "\n")
        data, _ = self.build("pr-ask", pr_state=True, pr_lookup=fake)
        self.assertEqual([(x["pr"], x["state"]) for x in self.item(data, "DEC-009")["prs"]], [("example/synthetic#9", "merged")])
        self.assertEqual(ck.pr_refs("https://github.com/../../pull/1 and telosmith pr #9", "sam", {"telosmith"}), [])
        before = len(calls)
        meta, _ = self.build("pr-meta", content="metadata", pr_state=True, pr_lookup=fake)
        self.assertEqual((meta["pr_state"], len(calls)), ({"looked_up": False}, before))   # metadata reads no text, looks nothing up

    def test_amend_clarify_lines(self):
        """cockpit: Amend and Clarify compose ledger lines that pass the check once filled in, and are refused while placeholders remain"""
        data, _ = self.build()
        c = self.item(data, "DEC-002")
        amend = dict(c["compose"]["amend"]["json"], decision="Use the survey, but only for the pilot.")
        clar = c["compose"]["clarify"]["json"]
        self.assertEqual((amend["status"], amend["supersedes"], amend["amends"]), ("adopted", "DEC-002", "DEC-002"))
        self.assertEqual((clar["kind"], clar["about"], clar["addressed_to"], clar["status"]),
                         ("question", "DEC-002", "claude:research#SESSA1", "open"))
        unfilled = self.check([dict(amend, id="DEC-009", date="2026-11-05", recorded_by="sam")],
                              [dict(clar, id="CH-004", date="2026-11-05", recorded_by="sam")])
        self.assertNotEqual(unfilled.returncode, 0)
        self.assertIn("REQUIRED", unfilled.stdout)
        filled = self.check([dict(amend, id="DEC-009", date="2026-11-05", decider="sam", basis='"pilot only"',
                                  locator="synthetic chat", recorded_by="sam")],
                            [dict(clar, id="CH-004", date="2026-11-05", concern="Which pilot sites?", recorded_by="sam")])
        self.assertEqual(filled.returncode, 0, filled.stdout + filled.stderr)
        self.assertRegex(c["compose"]["amend"]["command"], r"ledger append --dir governance --expect DEC-\d+=open@[0-9a-f]{12} decision ")

    def test_questions_surface(self):
        """cockpit: a Clarify question leaves Choices and shows under Questions until a line resolves it"""
        q = {"id": "CH-004", "date": "2026-11-05", "status": "open", "kind": "question", "about": "DEC-002",
             "addressed_to": "claude:research#SESSA1", "concern": "Which pilot sites?", "raised_by": "sam", "recorded_by": "sam"}
        with open(self.root / "governance" / "challenges.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(q) + "\n")
        data, page = self.build()
        self.assertNotIn("CH-004", [c["id"] for c in data["choices"]])
        self.assertEqual([(x["id"], x["about"], x["addressed_to"]) for x in data["questions"]], [("CH-004", "DEC-002", "claude:research#SESSA1")])
        self.assertIn("Which pilot sites?", page)
        self.assertNotIn("NOT WIRED TO RECORDS YET</span><h2>Question stewards", page)
        with open(self.root / "governance" / "challenges.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"id": "CH-005", "date": "2026-11-05", "status": "resolved", "resolves": "CH-004", "concern": "Which pilot sites?",
                                "raised_by": "sam", "resolution": "Sites A and B.", "recorded_by": "claude:research#SESSA1"}) + "\n")
        data, _ = self.build("again")
        self.assertEqual(data["questions"], [])
        # the answer names DEC-002, but answering a question doesn't overtake what it was about
        self.assertEqual(self.item(data, "DEC-002")["possibly_overtaken"], [])

    def test_overtaken_needs_unnegated_words_in_text(self):
        """cockpit: closure words count only in record text, not negated, and 'amended' counts; an open question about it is waiting"""
        lines = [
            {"id": "DEC-009", "date": "2026-11-04", "status": "adopted", "decision": "Keep DEC-006 open; it is not done yet.",
             "scope": "research", "decider": "sam", "basis": "\"wait\"", "locator": "synthetic chat", "recorded_by": "sam"},
            {"id": "DEC-010", "date": "2026-11-04", "status": "rejected", "supersedes": "DEC-002", "decision": "Unrelated; see DEC-001 for context.",
             "scope": "research", "decider": "sam", "basis": "\"no\"", "locator": "synthetic chat", "recorded_by": "sam"},
        ]
        with open(self.root / "governance" / "decisions.jsonl", "a", encoding="utf-8") as f:
            for d in lines:
                f.write(json.dumps(d) + "\n")
        q = {"id": "CH-004", "date": "2026-11-05", "status": "open", "kind": "question", "about": "DEC-006",
             "addressed_to": "claude:research#SESSA1", "concern": "Which day?", "raised_by": "sam", "recorded_by": "sam"}
        with open(self.root / "governance" / "challenges.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(q) + "\n")
        data, _ = self.build()
        c = self.item(data, "DEC-006")
        self.assertEqual(c["possibly_overtaken"], [])   # "not done yet" / "keep ... open" are negations
        self.assertEqual([w["id"] for w in c["waiting_on_it"]], ["CH-004"])   # the open question about it
        with open(self.root / "governance" / "decisions.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"id": "DEC-011", "date": "2026-11-05", "status": "adopted", "decision": "DEC-006 amended: fortnightly.",
                                "scope": "research", "decider": "sam", "basis": "\"ok\"", "locator": "synthetic chat",
                                "recorded_by": "sam"}) + "\n")
        data, _ = self.build("again")
        self.assertEqual(len(self.item(data, "DEC-006")["possibly_overtaken"]), 1)
        self.assertIn("amended", self.item(data, "DEC-006")["possibly_overtaken"][0])

    def test_settled_records_dont_wait(self):
        """cockpit: a superseded decision or resolved challenge that cites a choice is neither waiting on it nor evidence about it"""
        later = {"id": "DEC-009", "date": "2026-11-05", "status": "proposed", "decision": "Follow DEC-006 next month.",
                 "scope": "research", "recorded_by": "claude:research#SESSA1"}
        settle = {"id": "DEC-010", "date": "2026-11-05", "status": "rejected", "supersedes": "DEC-009", "decision": "No.",
                  "scope": "research", "decider": "sam", "basis": "\"no\"", "locator": "synthetic chat", "recorded_by": "sam"}
        with open(self.root / "governance" / "decisions.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(later) + "\n")
        data, _ = self.build()
        self.assertEqual([w["id"] for w in self.item(data, "DEC-006")["waiting_on_it"]], ["DEC-009"])
        with open(self.root / "governance" / "decisions.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(settle) + "\n")
        data, _ = self.build("again")
        self.assertEqual(self.item(data, "DEC-006")["waiting_on_it"], [])

    def test_inquire_writes_nothing(self):
        """cockpit: Inquire is off the record: no command, no ledger line, only a prompt naming the item"""
        data, page = self.build()
        for c in data["choices"]:
            inq = c["compose"]["inquire"]
            self.assertIsNone(inq["command"])
            self.assertIsNone(inq["json"])
            self.assertIn(str(c["id"]), inq["prompt"])
        self.assertIn("off record", page)
        self.assertIn("Clarify is on the record", page)

    def test_ledger_check_new_fields(self):
        """ledger check: ask, kind 'question' (with about and addressed_to) and amends are checked when present"""
        bad = [
            ([dict(ASKED, id="DEC-009", ask={"question": "No question mark", "options": []})], [], "ending in '?'"),
            ([dict(ASKED, id="DEC-009", ask={"question": "Ok?", "options": [{"label": "Accept"}]})], [], "label, effect"),
            ([dict(ASKED, id="DEC-009", ask={"question": "Ok?", "surprise": 1})], [], "unknown key"),
            ([dict(ASKED, id="DEC-009", ask={"question": "Ok?", "options": [], "default_if_unanswered": "x"})], [], "non-empty list"),
            ([dict(ASKED, id="DEC-009", ask={"question": "Ok?", "options": [{"label": "A", "effect": "b"}]})], [], "if nobody answers"),
            ([], [{"id": "CH-004", "date": "2026-11-05", "status": "open", "kind": "question", "concern": "x", "raised_by": "sam"}], "needs 'about'"),
            ([], [{"id": "CH-004", "date": "2026-11-05", "status": "open", "kind": "question", "about": "DEC-001",
                   "addressed_to": None, "concern": "x", "raised_by": "sam"}], "needs 'addressed_to'"),
        ]
        for decs, chs, msg in bad:
            r = self.check(decs, chs)
            self.assertNotEqual(r.returncode, 0, msg)
            self.assertIn(msg, r.stdout, msg)
        self.assertEqual(self.check([]).returncode, 0)
        # lines that were valid before stay valid: other kinds, a non-object ask, amends on its own
        ok = self.check([dict(ASKED, id="DEC-009", ask="Should we?"), dict(CITER, id="DEC-010", amends="DEC-006")],
                        [{"id": "CH-004", "date": "2026-11-05", "status": "open", "kind": "risk", "concern": "x", "raised_by": "sam"}])
        self.assertEqual(ok.returncode, 0, ok.stdout)

if __name__ == "__main__":
    unittest.main()
