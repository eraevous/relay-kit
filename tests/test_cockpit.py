"""Tests for tools/cockpit.py (relay-kit/cockpit@7). Standard library only; synthetic data only.

Run alone with `python3 -m unittest discover -s tests`, or as part of tests/run_tests.py, which
reports each test by the first line of its docstring ("cockpit: ...")."""
import base64, hashlib, importlib.util, json, os, pathlib, re, shlex, shutil, subprocess, sys, tempfile, unittest

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
        self.assertEqual(json.loads(js)["schema"], "relay-kit/cockpit@7")
        for sid in ("overview", "intake", "questions", "choices", "topology", "budget", "lanes", "run", "review", "prs", "status", "actions", "coordination"):
            self.assertIn(f'id="{sid}"', page)
        for bad in ("<link", "src=\"http", "src='http", "@import", "url(http"):
            self.assertNotIn(bad, page)
        self.assertEqual(page.count("<script"), 1)
        self.assertIn("newest record 2026-11-03T10:05Z", page)   # the latest record, not the clock
        self.assertIn("not wired to records yet".upper(), page)
        self.assertIn("https://chatgpt.com/c/00000000-0000-0000-0000-0000000000ff", page)   # intake link from threads.md
        self.assertEqual(data["run"]["pr_links"], ["https://github.com/example/synthetic/pull/7"])

    def test_review_burden_line_names_the_sample(self):
        """cockpit: the review_burden line says how many readable reviewed PRs the distribution covers, never "among those" (Kate, #46 finding 1)"""
        data, _, _ = self.build()
        rb = {"metric": "review_burden", "label": "measured", "version": "review_burden@2", "definition": "d",
              "merged": 3, "reviewed": 2, "unreviewed": 1, "review_state_unknown": 0,
              "submissions": {"n": 1, "median": 0, "p90": 0, "max": 0},
              "excluded": {"reviews retained from an earlier read, not sampled": 1}, "unknown": None}
        data["telemetry"] = {"metrics": [rb], "notes": []}
        page = ck.render(data)
        self.assertIn("2 of 3 merged PRs in the observed pr-state cohort have a recorded GitHub review", page)
        self.assertIn("among the 1 readable reviewed PRs", page)
        self.assertIn("1 reviewed PR(s) with unread reviews not sampled", page)
        self.assertNotIn("among those", page)
        self.assertNotIn("in the read window", page)

    def test_csp_allows_only_rendered_scripts(self):
        """cockpit: CSP hashes match exact plain/themed script bytes and forbid arbitrary inline scripts"""
        theme = self.tmp / "theme.json"
        theme.write_text(json.dumps({"name": "Synthetic", "dark": {}, "light": {}, "default": "auto"}), encoding="utf-8")
        for themed in (False, True):
            with self.subTest(themed=themed):
                data = ck.collect(self.root)
                if themed:
                    data["theme"] = ck.load_theme(theme)
                page = ck.render(data)
                policy = re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]+)">', page).group(1)
                directives = {parts[0]: parts[1:] for clause in policy.split(";") if (parts := clause.split())}
                scripts = re.findall(r"<script>([\s\S]*?)</script>", page)
                self.assertEqual(len(scripts), 2 if themed else 1)
                expected = {"'sha256-" + base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii") + "'"
                            for script in scripts}
                self.assertEqual(set(directives["script-src"]), expected)
                self.assertNotIn("'unsafe-inline'", directives["script-src"])
                self.assertEqual(directives["base-uri"], ["'none'"])
                self.assertEqual(directives["form-action"], ["'none'"])
                self.assertEqual(directives["default-src"], ["'none'"])
                injected = "'sha256-" + base64.b64encode(hashlib.sha256(b"alert(1)").digest()).decode("ascii") + "'"
                self.assertNotIn(injected, directives["script-src"])
                self.assertLess(page.index('http-equiv="Content-Security-Policy"'), page.index("<script>"))

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

    def test_topology_layout(self):
        """cockpit: Topology carries each handoff count beside its arrowhead with a sender dot, a visible self-loop, a per-actor sent/received table, and tags lanes without a thread"""
        # a self-handoff and a lane with no thread line, on top of the fixture's two flows
        (self.root / "handoffs" / "HO-RS-20261104-SELF.md").write_text(
            handoff("HO-RS-20261104-SELF", "sent", "claude:research", "claude:research", "Note to self", True,
                    [("2026-11-04T09:00Z", "sent", "claude:research#SESSA1", "self")]), encoding="utf-8")
        (self.root / "handoffs" / "HO-RS-20261104-SOLO.md").write_text(
            handoff("HO-RS-20261104-SOLO", "sent", "claude:research", "claude:solo", "To a lane with no thread", True,
                    [("2026-11-04T09:00Z", "sent", "claude:research#SESSA1", "solo")]), encoding="utf-8")
        (self.root / "handoffs" / ".ledger.json").write_text(json.dumps({"owner": "sam", "lanes": {"research": "RS", "review": "RV", "solo": "SO"}}), encoding="utf-8")
        data, page, _ = self.build()
        info = data["topology"]["information"]
        rows = {r["actor"]: r for r in info["actors"]}
        self.assertEqual((rows["claude:research"]["sends"], rows["claude:research"]["receives"], rows["claude:research"]["self"]), (4, 2, 1))
        self.assertEqual(list(rows["claude:research"]["to"].items()), [("chatgpt:review", 2), ("claude:research", 1), ("claude:solo", 1)])
        self.assertEqual([r["actor"] for r in info["actors"]][0], "claude:research")           # busiest sender first
        self.assertEqual(info["lanes_without_thread"], ["solo"])
        self.assertEqual({n["label"]: n["has_thread"] for n in info["nodes"] if n["type"] == "lane"}, {"research": True, "review": True, "solo": False})
        svg = page[page.index('aria-label="Information topology"'):page.index("</svg>", page.index('aria-label="Information topology"'))]
        self.assertIn('class="band-tag"', svg)
        self.assertIn(">no thread</text>", svg)
        self.assertEqual(svg.count('class="e-tail"'), 4)                                     # one sender dot per handoff edge
        self.assertEqual(svg.count('class="e-count-bg"'), 4)                                  # one count pill per handoff edge
        self.assertIn('markerWidth="15"', svg)                                                # the handoff arrowhead is the large one
        loop = next(g for g in svg.split("<g>") if "claude:research → claude:research" in g)
        self.assertIn(" C ", loop)                                                            # the self-loop is a cubic lobe
        self.assertIn('class="e-count" x=', loop)
        # the count sits by the arrowhead end, not mid-span: for the research → review flow, nearer the receiver
        import re as _re
        g = next(g for g in svg.split("<g>") if "claude:research → chatgpt:review" in g)
        m = _re.search(r'd="M ([\d.]+) ([\d.]+) Q [\d.]+ [\d.]+ ([\d.]+) ([\d.]+)"', g)
        lx, ly = map(float, _re.search(r'class="e-count" x="([\d.]+)" y="([\d.-]+)"', g).groups())
        sx, sy, ex, ey = map(float, m.groups())
        self.assertLess((lx - ex) ** 2 + (ly - ey) ** 2, (lx - sx) ** 2 + (ly - sy) ** 2)
        self.assertIn("HANDOFFS PER ACTOR", page)
        self.assertIn('<td data-h="sent" class="n">4 (1 to itself)</td>', page)
        self.assertIn('<span class="tag warn">no thread</span>', page)
        self.assertIn("Every sender → receiver count · 2 senders × 3 receivers", page)   # the folded matrix
        self.assertIn('<tr><th>claude:research</th><td class="n">1</td><td class="n">2</td><td class="n">1</td><td class="n tot">4</td></tr>', page)

    def test_actions_surface(self):
        """cockpit: Actions renders a markdown checklist as written: open items with since/age, size and due when stated, done items folded; --actions FILE or actions/*.md; a landing target"""
        text = ("# To do\n\nOnly you can do these. <b>x</b>\n\n## Open\n"
                "- [ ] **Enable** the thing (`cfg`) — DEC-001. Since 2026-11-01. ~15 min.\n"
                "- [ ] Decide go / no-go. Due 2026-11-10.\n"
                "- [ ] Rename <script>alert(1)</script> one word each\n"
                "## Done\n- [x] Old item. Since 2026-10-20. 2 min.\n")
        (self.root / "actions").mkdir()
        (self.root / "actions" / "owner.md").write_text(text, encoding="utf-8")
        (self.root / "actions" / "notes.txt").write_text("ignored", encoding="utf-8")
        data, page, _ = self.build(landing="actions")
        ac = data["actions"]
        self.assertEqual((ac["present"], ac["source"], ac["open"], ac["done"], ac["contract"]), (True, "actions/", 3, 1, "relay-kit/actions@1"))
        self.assertEqual([f["file"] for f in ac["files"]], ["actions/owner.md"])
        items = ac["files"][0]["items"]
        self.assertEqual([(i["done"], i["since"], i["size"], i["due"], i["section"]) for i in items],
                         [(False, "2026-11-01", "~15 min", None, "Open"), (False, None, None, "2026-11-10", "Open"),
                          (False, None, "one word each", None, "Open"), (True, "2026-10-20", "2 min", None, "Done")])
        self.assertEqual(items[0]["age_days"], 2)                      # to the newest record (2026-11-03), not today
        self.assertEqual(items[1]["days_to_due"], 7)
        self.assertEqual(data["counts"]["actions_open"], 3)
        self.assertIn('data-landing="actions"', page)
        self.assertIn("<b>Enable</b> the thing (<code>cfg</code>)", page)
        self.assertIn("Rename &lt;script&gt;alert(1)&lt;/script&gt; one word each", page)
        self.assertIn("Only you can do these. &lt;b&gt;x&lt;/b&gt;", page)
        self.assertIn("since 2026-11-01 · <b>2 d</b> · size ~15 min", page)
        self.assertIn("counted to the newest record, 2026-11-03, not today · 2 open without a stated since-when · 1 without a stated size", page)
        self.assertIn("due 2026-11-10 · <b class=\"warn\">in 7 d</b> · size not stated", page)
        self.assertIn("since not stated · size one word each", page)
        self.assertIn("<summary>Done · 1</summary>", page)
        self.assertEqual(page.count("NEEDS YOU</span>"), 3)             # done items carry no needs-you tag
        self.assertIn("Actions ↗</a> <b class=\"warn\">3 open</b> for you", page)
        # --actions FILE wins over the folder and is named by its place in the records
        (self.root / "list.md").write_text("- [ ] only this\n", encoding="utf-8")
        data, page, _ = self.build("out2", actions=self.root / "list.md")
        self.assertEqual((data["actions"]["source"], data["actions"]["open"], data["actions"]["files"][0]["file"]), ("--actions", 1, "list.md"))
        self.assertNotIn(str(self.root), page)
        # metadata mode keeps states and dates, not words
        data, page, _ = self.build("out3", content="metadata")
        self.assertIsNone(data["actions"]["files"][0]["items"][0]["text"])
        self.assertNotIn("Enable</b> the thing", page)
        self.assertEqual(data["actions"]["open"], 3)

    def test_actions_absent(self):
        """cockpit: without an action list the Actions surface says how to keep one"""
        data, page, _ = self.build()
        self.assertFalse(data["actions"]["present"])
        self.assertIn("No action list. Pass <code>--actions FILE</code>", page)

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

    def test_coordination_unknown_without_captures(self):
        """cockpit: with no chat-telemetry output the Coordination capture half says unknown with a reason, never zero turns"""
        data, page, js = self.build()
        co = json.loads(js)["coordination"]
        self.assertEqual(co["capture"]["state"], "unknown")
        self.assertIn("no chat-telemetry output", co["capture"]["reason"])
        self.assertNotIn("owner_turns", co["capture"])
        self.assertIn("UNKNOWN: NO CAPTURES", page)
        self.assertIn("Wall-clock gaps are not effort", page)
        self.assertEqual([m["metric"] for m in co["ledger"]["metrics"]],
                         ["coordination_loss", "idea_to_completed_handoff_latency", "measurement_coverage"])

    def test_coordination_from_chat_telemetry(self):
        """cockpit: Coordination reads chat-telemetry output: owner turns as a lower bound, gaps ending in owner turns only, no previews"""
        def turn(n, start, relay, gap, band, hour=9):
            return {"n": n, "start": start, "local_hour": hour, "local_weekday": 0, "relay": relay, "user_words": 10,
                    "user_preview": "PREVIEWSECRET words", "assistant_words": 50, "assistant_messages": 1, "tool_steps": 1,
                    "interruptions": 0, "reply_latency_s": 4.0, "turn_duration_s": 9.0, "longest_silence_s": 130.0 if n == 2 else 3.0,
                    "hangs": [{"at": start, "seconds": 130.0}] if n == 2 else [], "end": start, "answered": True,
                    "return_gap_s": gap, "return_band": band}
        out = {"generated": "2026-11-01T00:00:00+00:00", "timezone": "America/Chicago", "hang_seconds": 120, "engaged_seconds": 900,
               "bands": ["live", "short break", "stepped away", "came back later"],
               "threads": [{"title": "Synthetic thread", "platform": "chatgpt", "source": "raw/one.raw.json",
                            "first": "2026-11-01T10:00:00+00:00", "last": "2026-11-01T10:30:00+00:00", "checks": ["ok"],
                            "summary": {}, "turns": [turn(1, "2026-11-01T10:00:00+00:00", False, 60.0, "live"),
                                                     turn(2, "2026-11-01T10:02:00+00:00", True, 300.0, "short break"),
                                                     turn(3, "2026-11-01T10:10:00+00:00", False, None, None)]}]}
        (self.root / "telemetry" / "chat").mkdir(parents=True)
        (self.root / "telemetry" / "chat" / "t.json").write_text(json.dumps(out), encoding="utf-8")
        (self.root / "raw").mkdir()
        (self.root / "raw" / "one.raw.json").write_text("{}", encoding="utf-8")
        (self.root / "raw" / "two.raw.json").write_text("{}", encoding="utf-8")
        data, page, js = self.build()
        co = json.loads(js)["coordination"]
        c = co["capture"]
        self.assertEqual((c["state"], c["owner_turns"], c["relayed_turns"], c["owner_turns_bound"]), ("measured", 2, 1, "lower bound"))
        # turn 1's gap ends in a relayed paste, so only turn 2's gap (ending in owner turn 3) is the owner's
        self.assertEqual(c["return_gap_bands"]["short break"], 1)
        self.assertEqual(c["return_gap_bands"]["live"], 0)
        self.assertEqual(c["gaps_ending_in_relayed_turn"], 1)
        self.assertEqual([s["relayed_turns"] for s in c["stretches"]], [1, 0])
        self.assertEqual(c["hangs"], 1)
        self.assertEqual(co["raw_captures_not_analysed"], ["raw/two.raw.json"])
        self.assertIn("decisions", c["ledger_owner_lines"])
        self.assertNotIn("PREVIEWSECRET", page)
        self.assertNotIn("PREVIEWSECRET", js)
        self.assertIn("≥ 2", page)
        _, page2, js2 = self.build("out2")
        self.assertEqual(js, js2)

    def test_coordination_threads_by_source_and_metadata(self):
        """cockpit: Coordination keeps two captures with one title apart, and --content metadata leaves their titles out"""
        def turn(n, start, relay, gap):
            return {"n": n, "start": start, "local_hour": 9, "relay": relay, "user_words": 1, "assistant_words": 1,
                    "tool_steps": 0, "reply_latency_s": 1.0, "longest_silence_s": 1.0, "hangs": [], "return_gap_s": gap,
                    "return_band": "live" if gap is not None else None}
        def thread(src, turns):
            return {"title": "SECRETTITLE", "platform": "chatgpt", "source": src, "first": turns[0]["start"],
                    "last": turns[-1]["start"], "checks": [], "summary": {}, "turns": turns}
        out = {"engaged_seconds": 900, "bands": [], "threads": [
            thread("raw/a.raw.json", [turn(1, "2026-11-01T10:00:00+00:00", False, None)]),
            thread("raw/b.raw.json", [turn(1, "2026-11-01T10:01:00+00:00", True, 30.0),
                                      turn(2, "2026-11-01T10:02:00+00:00", True, None)])]}
        (self.root / "telemetry" / "chat").mkdir(parents=True)
        (self.root / "telemetry" / "chat" / "t.json").write_text(json.dumps(out), encoding="utf-8")
        _, page, js = self.build()
        c = json.loads(js)["coordination"]
        self.assertEqual(len({t["thread"] for t in c["threads"]}), 2)
        # the relayed-only capture opens its own stretch; it never joins the owner's stretch in the other capture
        self.assertEqual(sorted((s["thread"].endswith("[2]"), s["relayed_turns"]) for s in c["capture"]["stretches"]),
                         [(False, 0), (True, 2)])
        _, page_m, js_m = self.build("meta", content="metadata")
        self.assertNotIn("SECRETTITLE", page_m)
        self.assertNotIn("SECRETTITLE", js_m)
        self.assertIn("capture 1 (chatgpt)", page_m)

    def test_coordination_recorded_order(self):
        """cockpit: Coordination follows recorded turn order when timestamps run backwards (stretches, owner reply gaps)"""
        def turn(n, start, relay, gap, band):
            return {"n": n, "start": start, "local_hour": 9, "relay": relay, "user_words": 1, "assistant_words": 1,
                    "tool_steps": 0, "reply_latency_s": 1.0, "longest_silence_s": 1.0, "hangs": [], "return_gap_s": gap,
                    "return_band": band}
        turns = [turn(1, "2026-11-01T10:00:00+00:00", False, 30.0, "live"),
                 turn(2, "2026-11-01T09:59:00+00:00", True, 400.0, "short break"),   # clock ran backwards
                 turn(3, "2026-11-01T10:05:00+00:00", False, None, None)]
        out = {"engaged_seconds": 900, "bands": [], "threads": [{"title": "T", "platform": "chatgpt", "source": "raw/a.raw.json",
               "first": turns[0]["start"], "last": turns[-1]["start"], "checks": [], "summary": {}, "turns": turns}]}
        (self.root / "telemetry" / "chat").mkdir(parents=True)
        (self.root / "telemetry" / "chat" / "t.json").write_text(json.dumps(out), encoding="utf-8")
        _, page, js = self.build()
        c = json.loads(js)["coordination"]["capture"]
        self.assertEqual([s["relayed_turns"] for s in c["stretches"]], [1, 0])
        self.assertEqual((c["return_gap_bands"]["short break"], c["return_gap_bands"]["live"]), (1, 0))

    def test_coordination_malformed_duplicates_and_coverage(self):
        """cockpit: Coordination lists a malformed report as unreadable, counts a capture in two reports once, and matches raw captures by path"""
        def turn(n, start, relay):
            return {"n": n, "start": start, "local_hour": 9, "relay": relay, "user_words": 1, "assistant_words": 1,
                    "tool_steps": 0, "reply_latency_s": 1.0, "longest_silence_s": 1.0, "hangs": [], "return_gap_s": None,
                    "return_band": None}
        def report(gen, src):
            return {"generated": gen, "engaged_seconds": 900, "bands": [], "threads": [{"title": "T", "platform": "chatgpt",
                    "source": src, "first": "2026-11-01T10:00:00+00:00", "last": "2026-11-01T10:00:00+00:00", "checks": [],
                    "summary": {}, "turns": [turn(1, "2026-11-01T10:00:00+00:00", False)]}]}
        d = self.root / "telemetry" / "chat"
        d.mkdir(parents=True)
        (d / "a.json").write_text(json.dumps(report("2026-11-01T00:00:00+00:00", "raw/a/thread.raw.json")), encoding="utf-8")
        (d / "b.json").write_text(json.dumps(report("2026-11-02T00:00:00+00:00", "./raw/a/thread.raw.json")), encoding="utf-8")
        bad = report("2026-11-03T00:00:00+00:00", "raw/c.raw.json")
        bad["threads"][0]["turns"] = [None]
        (d / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
        nots = report("2026-11-03T00:00:00+00:00", "raw/d.raw.json")
        nots["threads"][0]["turns"][0]["start"] = None
        (d / "nostart.json").write_text(json.dumps(nots), encoding="utf-8")
        for r in ("raw/a", "raw/b"):
            (self.root / r).mkdir(parents=True, exist_ok=True)
            (self.root / r / "thread.raw.json").write_text("{}", encoding="utf-8")
        data, page, js = self.build()
        co = json.loads(js)["coordination"]
        self.assertEqual(co["unreadable"], ["telemetry/chat/bad.json", "telemetry/chat/nostart.json"])
        self.assertEqual(co["capture"]["owner_turns"], 1)
        self.assertEqual(co["duplicate_sources"], [{"source": "raw/a/thread.raw.json", "skipped": "telemetry/chat/a.json",
                                                    "kept": "telemetry/chat/b.json"}])
        self.assertEqual(co["raw_captures_not_analysed"], ["raw/b/thread.raw.json"])
        self.assertIn('id="choices"', page)

    def test_coordination_bad_numbers_and_absolute_paths(self):
        """cockpit: Coordination refuses a report with a non-numeric field and counts a capture once when reports spell its path differently"""
        def report(gen, src, words=1):
            return {"generated": gen, "engaged_seconds": 900, "bands": [], "threads": [{"title": "T", "platform": "chatgpt",
                    "source": src, "checks": [], "summary": {}, "turns": [{"n": 1, "start": "2026-11-01T10:00:00+00:00",
                    "relay": False, "user_words": 1, "assistant_words": words, "hangs": []}]}]}
        (self.root / "raw").mkdir()
        (self.root / "raw" / "one.raw.json").write_text("{}", encoding="utf-8")
        d = self.root / "telemetry" / "chat"
        d.mkdir(parents=True)
        (d / "rel.json").write_text(json.dumps(report("2026-11-01T00:00:00+00:00", "raw/one.raw.json")), encoding="utf-8")
        (d / "abs.json").write_text(json.dumps(report("2026-11-02T00:00:00+00:00", str(self.root / "raw" / "one.raw.json"))), encoding="utf-8")
        (d / "bad.json").write_text(json.dumps(report("2026-11-03T00:00:00+00:00", "raw/two.raw.json", words="invalid")), encoding="utf-8")
        data, page, js = self.build()
        co = json.loads(js)["coordination"]
        self.assertEqual(co["unreadable"], ["telemetry/chat/bad.json"])
        self.assertEqual(co["capture"]["owner_turns"], 1)
        self.assertEqual([x["source"] for x in co["duplicate_sources"]], ["raw/one.raw.json"])
        self.assertEqual(co["raw_captures_not_analysed"], [])

    def test_coordination_utc_order_table_and_unknown_claims(self):
        """cockpit: Coordination orders and labels turns in UTC whatever their offsets, lists chart values as a table, and shows unknown claim counts as unknown"""
        turns = [{"n": 1, "start": "2026-11-01T05:30:00-06:00", "relay": False, "user_words": 7, "assistant_words": 70, "hangs": []},
                 {"n": 1, "start": "2026-11-01T10:00:00+00:00", "relay": False, "user_words": 8, "assistant_words": 80, "hangs": []}]
        out = {"engaged_seconds": 900, "bands": [], "threads": [
            {"title": "A", "platform": "chatgpt", "source": "raw/a.raw.json", "checks": [], "summary": {}, "turns": [turns[0]]},
            {"title": "B", "platform": "chatgpt", "source": "raw/b.raw.json", "checks": [], "summary": {}, "turns": [turns[1]]}]}
        (self.root / "telemetry" / "chat").mkdir(parents=True)
        (self.root / "telemetry" / "chat" / "t.json").write_text(json.dumps(out), encoding="utf-8")
        _, page, js = self.build()
        c = json.loads(js)["coordination"]["capture"]
        self.assertEqual((c["first"], c["last"]), ("2026-11-01T10:00:00+00:00", "2026-11-01T05:30:00-06:00"))
        self.assertIn("2026-11-01 11:30", page)
        self.assertIn("The same values as a table", page)
        _, page_m, _ = self.build("meta", content="metadata")
        self.assertNotIn("None overlapping", page_m)


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
