"""Tests for tools/export.py (relay-kit/export@1). Standard library only; synthetic data only.

Run alone with `python3 -m unittest discover -s tests`, or as part of tests/run_tests.py, which
reports each test by the first line of its docstring ("export: ...")."""
import hashlib, importlib.util, json, os, pathlib, shutil, subprocess, sys, tempfile, unittest

KIT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("relaykit_export_under_test", KIT / "tools" / "export.py")
ex = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ex)

URL_A = "https://chatgpt.com/c/00000000-0000-0000-0000-00000000000a"
URL_B = "https://chatgpt.com/c/00000000-0000-0000-0000-00000000000b"
URL_UNCAPTURED = "https://chatgpt.com/c/00000000-0000-0000-0000-00000000000c"


def msg(mid, mid_id, ts, text, model=None):
    return {"mid": mid, "id": mid_id, "ts": ts, "model": model, "dictated": False, "text": text, "sources": []}


def capture(cid, actor, turns, hidden=None):
    return {"schema": "relay-kit/conversation@1",
            "meta": {"title": f"Synthetic {cid[-1]}", "platform": "chatgpt", "conversation_id": cid,
                     "source_url": f"https://chatgpt.com/c/{cid}", "checks": ["ok"], "actor": actor, "lane": "research",
                     "raw_sha256": "0" * 64},
            "hidden_counts": hidden or {}, "turns": turns}


CONV_A = capture("00000000-0000-0000-0000-00000000000a", "claude:research#SESSA1", [
    {"n": 1, "role": "user", "label": "You", "messages": [msg("T1u", "u-1", "2026-11-01T10:00:00Z", "Plan the survey.\n  Keep  spacing\texactly.")]},
    {"n": 1, "role": "assistant", "label": "ChatGPT", "messages": [
        msg("T1a", "a-1", "2026-11-01T10:00:30Z", "Here is a plan.", "gpt-x"),
        msg("T1a2", "a-2", "2026-11-01T10:00:40Z", "And a second part — with unicode é.", "gpt-x")]},
    {"n": 2, "role": "user", "label": "You", "messages": [msg("T2u", "u-2", "2026-11-01T11:00:00Z",
        "FROM: Ada (ChatGPT) T1a\nA relayed reply from another thread.\nEND FROM")]},
    {"n": 2, "role": "assistant", "label": "ChatGPT", "messages": [msg("T2a", "a-3", None, "Reply with no timestamp.", "gpt-x")]},
], hidden={"assistant:tool_use": 2})
CONV_B = capture("00000000-0000-0000-0000-00000000000b", "claude:research#SESSB2", [
    {"n": 1, "role": "user", "label": "You", "messages": [msg("T1u", "u-1", "2026-11-03T09:00:00Z", "Second session opener.")]},
    {"n": 1, "role": "assistant", "label": "ChatGPT", "messages": [msg("T1a", "a-1", "2026-11-03T09:01:00Z", "Second session reply.", "gpt-y")]},
])

HANDOFF_DONE = """---
id: HO-RS-20261101-AB12
title: Review the synthetic survey draft
from: claude:research
to: chatgpt:review
created: 2026-11-01T12:00Z
status: completed
contract: relay-kit/handoff@2
source: none
last_processed: none
reply_to:
---

## Task
Review the draft.

## Authorized
- Quote: "go ahead" (sam, synthetic, 2026-11-01)

## Proposals
- none

## Context
- none

## Expected output
Write the result to outputs/HO-RS-20261101-AB12.md.

## Limits
- none

## Log
- 2026-11-01T12:00Z | prepared | claude:research#SESSA1 | drafted
- 2026-11-01T12:05Z | sent | claude:research#SESSA1 | pasted
- 2026-11-01T13:00Z | completed | claude:research#SESSA1 for chatgpt:review | outputs/HO-RS-20261101-AB12.md
"""
HANDOFF_OPEN = HANDOFF_DONE.replace("HO-RS-20261101-AB12", "HO-RS-20261102-CD34").replace(
    "status: completed", "status: sent").replace("from: claude:research", "from: claude:review").split(
    "- 2026-11-01T13:00Z")[0].replace("claude:research#SESSA1", "claude:review#SESSR9")

DECISIONS = [
    {"id": "DEC-001", "date": "2026-11-01", "status": "adopted", "decision": "Use the synthetic survey.", "scope": "research",
     "decider": "owner", "basis": "\"yes\"", "locator": "synthetic chat", "recorded_by": "claude:research#SESSA1"},
    {"id": "DEC-002", "date": "2026-11-02", "status": "proposed", "decision": "Add a second survey.", "scope": "research",
     "proposed_by": "chatgpt:review"},
]
CHALLENGES = [{"id": "CH-001", "date": "2026-11-02", "status": "open", "concern": "Sample is small.", "raised_by": "claude:research#SESSB2"}]
THREADS = f"""# Coordination index

## Lanes

### Research
| | Where | Status |
|---|---|---|
| Ada (ChatGPT) | [RS] coord: {URL_A} | **active** |
| Ada (ChatGPT) | [RS] second: {URL_B} | active |
| Ada (ChatGPT) | [RS] archive: {URL_UNCAPTURED} | archive |
| Claude | review session: https://claude.ai/code/session_SESSR9 | active |

## Change log
- 2026-11-01 | claude:research#SESSA1 | created
"""


def build_root(base):
    r = pathlib.Path(base) / "records"
    (r / "handoffs" / "outputs").mkdir(parents=True)
    (r / "governance").mkdir()
    (r / "captures" / "nested").mkdir(parents=True)
    (r / "handoffs" / ".ledger.json").write_text(json.dumps({"owner": "sam", "lanes": {"research": "RS", "review": "RV"}}), encoding="utf-8")
    (r / "handoffs" / "HO-RS-20261101-AB12.md").write_text(HANDOFF_DONE, encoding="utf-8")
    (r / "handoffs" / "HO-RS-20261102-CD34.md").write_text(HANDOFF_OPEN, encoding="utf-8")
    (r / "handoffs" / "outputs" / "HO-RS-20261101-AB12.md").write_text("HO-RS-20261101-AB12 · chatgpt:review · synthetic\n\nLooks fine.\n", encoding="utf-8")
    (r / "governance" / "decisions.jsonl").write_text("".join(json.dumps(d) + "\n" for d in DECISIONS), encoding="utf-8")
    (r / "governance" / "challenges.jsonl").write_text("".join(json.dumps(c) + "\n" for c in CHALLENGES), encoding="utf-8")
    (r / "threads.md").write_text(THREADS, encoding="utf-8")
    (r / "captures" / "a.json").write_text(json.dumps(CONV_A, ensure_ascii=False, indent=1), encoding="utf-8")
    (r / "captures" / "nested" / "b.json").write_text(json.dumps(CONV_B), encoding="utf-8")
    (r / "captures" / "other.json").write_text(json.dumps({"not": "a conversation"}), encoding="utf-8")
    return r


def git_repo(path, config=""):
    """A real (empty) git repository, so git itself resolves its remotes; `config` is appended to .git/config."""
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    with open(path / ".git" / "config", "a", encoding="utf-8") as f:
        f.write(config)
    return path


def tree_hash(root):
    h = hashlib.sha256()
    for p in sorted(pathlib.Path(root).rglob("*")):
        h.update(p.relative_to(root).as_posix().encode())
        if p.is_file():
            h.update(p.read_bytes())
            h.update(str(p.stat().st_mtime_ns).encode())
    return h.hexdigest()


class ExportTests(unittest.TestCase):
    def test_visibility_uses_shared_authenticated_transport(self):
        """export: visibility uses the shared redirect-safe GitHub token transport"""
        from unittest.mock import Mock, patch
        transport = Mock(return_value=(200, {}, '{"private":true}'))
        with patch.dict(os.environ, {"GH_TOKEN": "synthetic-test-token"}), \
             patch.object(ex.GH, "token_transport", return_value=transport) as factory, \
             patch.object(ex.urllib.request, "urlopen", side_effect=AssertionError("unguarded request")):
            self.assertEqual(ex.authenticated_visibility("example/repo"), "private")
        factory.assert_called_once_with("synthetic-test-token")
        transport.assert_called_once_with("repos/example/repo", {})

    def test_reader_preserves_missing_file_errors(self):
        """export: missing capture files retain recoverable I/O errors"""
        with tempfile.TemporaryDirectory() as tmp:
            rd = ex.Reader(pathlib.Path(tmp))
            with self.assertRaises(FileNotFoundError):
                rd.read_bytes(rd.root / "missing.json")

    @unittest.skipUnless(os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"), "requires POSIX no-follow opens")
    def test_reader_descriptor_survives_root_and_ancestor_swaps(self):
        """export: descriptor reads reject symlink swaps and keep the selected root pinned"""
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp).resolve()
            root, outside = base / "records", base / "outside"
            (root / "sub").mkdir(parents=True)
            (outside / "sub").mkdir(parents=True)
            (root / "sub/data.json").write_text("inside", encoding="utf-8")
            (outside / "sub/data.json").write_text("outside", encoding="utf-8")
            rd = ex.Reader(root)
            root.rename(base / "original")
            root.symlink_to(outside, target_is_directory=True)
            self.assertEqual(rd.read_bytes(root / "sub/data.json"), b"inside")
            original = base / "original"
            (original / "sub").rename(original / "saved")
            (original / "sub").symlink_to(outside / "sub", target_is_directory=True)
            with self.assertRaises(ex.Refused):
                rd.read_bytes(root / "sub/data.json")
            (original / "sub").unlink()
            (original / "saved").rename(original / "sub")
            (original / "sub/data.json").unlink()
            (original / "sub/data.json").symlink_to(outside / "sub/data.json")
            with self.assertRaises(ex.Refused):
                rd.read_bytes(root / "sub/data.json")

    def test_symlinked_ancestors_cannot_read_outside_records(self):
        """export: symlinked ledger, config, handoff and output directories cannot escape the records root"""
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            outside = base / "outside"
            outside.mkdir()
            for name in ("decisions.jsonl", ".ledger.json", "HO-RS-20261101-AB12.md"):
                (outside / name).write_text("SYNTHETIC-OUTSIDE-ROOT-CANARY", encoding="utf-8")
            cases = [
                ("governance", lambda rd: rd.ledger("decisions.jsonl", "decision")),
                ("handoffs", lambda rd: rd.config()),
                ("handoffs", lambda rd: ex.handoff_records(rd, ex.load_ledger())),
                ("handoffs/outputs", lambda rd: ex.output_records(rd, [])),
            ]
            for n, (directory, read) in enumerate(cases):
                with self.subTest(directory=directory, reader=n):
                    root = base / str(n)
                    link = root / directory
                    link.parent.mkdir(parents=True)
                    try:
                        link.symlink_to(outside, target_is_directory=True)
                    except (OSError, NotImplementedError):
                        self.skipTest("directory symlinks are unavailable")
                    rd = ex.Reader(root)
                    with patch.object(pathlib.Path, "read_bytes", side_effect=AssertionError("outside file opened")):
                        with self.assertRaisesRegex(ex.Refused, "outside the records root"):
                            read(rd)
                    self.assertEqual(rd.files, {})

    def test_reader_confines_direct_paths_and_allows_regular_records(self):
        """export: the shared reader rejects outside paths before opening and still reads regular records"""
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp).resolve()
            root = base / "records"
            root.mkdir()
            inside = root / "threads.md"
            inside.write_text("synthetic inside record", encoding="utf-8")
            outside = base / "outside.md"
            outside.write_text("synthetic outside record", encoding="utf-8")
            rd = ex.Reader(root)
            self.assertEqual(rd.read(inside, "threads"), b"synthetic inside record")
            with self.assertRaisesRegex(ex.Refused, "outside the records root"):
                rd.read(outside, "threads")
            self.assertEqual(set(rd.files), {"threads.md"})

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="relaykit-export-"))
        self.root = build_root(self.tmp)
        self._vis = ex.repo_visibility
        ex.repo_visibility = lambda repo: "unknown"   # no network in tests; each test sets what it needs

    def tearDown(self):
        ex.repo_visibility = self._vis
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_export(self, name="out", **kw):
        out = self.tmp / name
        m = ex.export(self.root, out, **kw)
        f = out / ("records.json" if kw.get("fmt") == "json" else "records.jsonl")
        text = f.read_text(encoding="utf-8")
        recs = json.loads(text) if kw.get("fmt") == "json" else [json.loads(l) for l in text.splitlines()]
        return m, recs, out

    def test_two_actors(self):
        """export: two actors in one lane export separately and together without id collisions"""
        _, a, _ = self.run_export("a", actors=["claude:research#SESSA1"], kinds=["conversations"])
        _, b, _ = self.run_export("b", actors=["claude:research#SESSB2"], kinds=["conversations"])
        _, both, _ = self.run_export("both", actors=["claude:research"], kinds=["conversations"])
        ma = {r["rid"] for r in a if r["kind"] == "message"}
        mb = {r["rid"] for r in b if r["kind"] == "message"}
        self.assertEqual(len(ma), 5)
        self.assertEqual(len(mb), 2)
        self.assertFalse(ma & mb)   # both have T1u and T1a, yet no rid collides
        rids = [r["rid"] for r in both]
        self.assertEqual(len(rids), len(set(rids)))
        self.assertEqual({r["rid"] for r in both if r["kind"] == "message"}, ma | mb)
        self.assertEqual({r["actor"] for r in a}, {"claude:research#SESSA1"})

    def test_round_trip(self):
        """export: message round trip from a conversation@1 capture preserves text, ids and order"""
        _, recs, _ = self.run_export(kinds=["conversations"], lane="research")
        got = [r for r in recs if r["kind"] == "message" and r["source_path"] == "captures/a.json"]
        want = [(t["role"], m) for t in CONV_A["turns"] for m in t["messages"]]
        self.assertEqual(len(got), len(want))
        for r, (role, m) in zip(got, want):
            for k in ("mid", "id", "ts", "model", "text", "dictated", "sources"):
                self.assertEqual(r[k], m[k], k)
            self.assertEqual(r["role"], role)
        self.assertEqual([r["seq"] for r in got], list(range(1, len(want) + 1)))
        src = (self.root / "captures" / "a.json").read_bytes()
        self.assertEqual(got[0]["source_hash"], hashlib.sha256(src).hexdigest())

    def test_relay_distinguishable(self):
        """export: a relayed message is marked relay with its origin, distinct from original speech"""
        _, recs, _ = self.run_export(kinds=["conversations"])
        by = {r["mid"]: r for r in recs if r["kind"] == "message" and r["source_path"] == "captures/a.json"}
        self.assertEqual(by["T2u"]["evidence_class"], "relay")
        self.assertEqual(by["T2u"]["relay"]["origin_label"], "Ada (ChatGPT) T1a")
        self.assertEqual(by["T2u"]["relay"]["origin_ref"], "T1a")
        self.assertEqual(by["T1u"]["evidence_class"], "source-grounded")
        self.assertIsNone(by["T1u"]["relay"])
        conv = next(r for r in recs if r["kind"] == "conversation" and r["source_path"] == "captures/a.json")
        self.assertEqual(conv["relayed_messages"], 1)
        self.assertEqual(conv["evidence_class"], "derived")

    def test_gaps_reported(self):
        """export: missing captures, hidden steps, absent timestamps and missing outputs are reported in the manifest"""
        m, recs, _ = self.run_export()
        g = m["coverage_gaps"]
        self.assertIn("00000000-0000-0000-0000-00000000000c", [t["conversation_id"] for t in g["threads_without_capture"]])
        self.assertNotIn("00000000-0000-0000-0000-00000000000a", [t["conversation_id"] for t in g["threads_without_capture"]])
        self.assertIn("SESSR9", [t["conversation_id"] for t in g["threads_without_capture"]])
        gap_actors = {a["actor"]: a for a in g["actors_without_capture"]}
        self.assertIn("chatgpt:review", gap_actors)
        self.assertIn("claude:review", gap_actors)
        self.assertNotIn("claude:research", gap_actors)
        self.assertIn("review", g["lanes_without_capture"])
        self.assertEqual([c["hidden_counts"] for c in g["conversations_with_hidden_steps"]], [{"assistant:tool_use": 2}])
        self.assertIn("message", g["records_missing_timestamps"])
        self.assertIn("output", g["records_missing_timestamps"])
        self.assertEqual([h["handoff"] for h in g["handoffs_missing_output"]], ["HO-RS-20261102-CD34"])
        self.assertEqual(m["exclusions"]["non_conversation_json_files"], 1)
        no_ts = next(r for r in recs if r.get("mid") == "T2a")
        self.assertIsNone(no_ts["ts"])
        self.assertTrue(no_ts["ts_missing"])
        self.assertTrue(all(r["ts_missing"] is False for r in recs if r["kind"] == "decision"))
        self.assertEqual(m["counts"]["conversation"], 2)   # captured arms exported; uncaptured ones never appear as empty conversations
        self.assertTrue(m["limits"])

    def test_decisions_vs_proposals(self):
        """export: decisions, proposals and derived views keep distinct evidence classes"""
        _, recs, _ = self.run_export(kinds=["decisions", "challenges"])
        by = {r["rid"]: r for r in recs}
        self.assertEqual(by["decision:DEC-001"]["evidence_class"], "decision")
        self.assertEqual(by["decision:DEC-002"]["evidence_class"], "proposal")
        self.assertEqual(by["decision:DEC-002"]["status"], "proposed")
        self.assertEqual(by["challenge:CH-001"]["evidence_class"], "source-grounded")
        self.assertTrue(all(r["evidence_class"] in ex.EVIDENCE for r in self.run_export("all")[1]))

    def test_deterministic(self):
        """export: repeated export over unchanged inputs gives matching content hashes"""
        kw = dict(since="2026-11-01", until="2026-11-02T23:00Z")
        m1, _, out = self.run_export("same", **kw)
        b1 = (out / "records.jsonl").read_bytes()
        m2, _, _ = self.run_export("same", **kw)
        b2 = (out / "records.jsonl").read_bytes()
        self.assertEqual(b1, b2)
        m1.pop("nondeterministic"); m2.pop("nondeterministic")
        self.assertEqual(m1, m2)
        self.assertEqual(m1["outputs"]["records.jsonl"]["sha256"], hashlib.sha256(b1).hexdigest())
        # the time window is applied: session B (2026-11-03) is out, undated records are counted, not silently kept
        _, recs, _ = self.run_export("win", **kw)
        self.assertFalse([r for r in recs if r["source_path"] == "captures/nested/b.json"])
        self.assertGreater(m1["exclusions"]["no_timestamp_under_time_filter"], 0)

    def test_no_mutation(self):
        """export: no original record is modified, whatever the options"""
        before = tree_hash(self.root)
        for i, kw in enumerate([{}, dict(granularity="inventory", fmt="json"), dict(content="metadata", lane="RS"),
                                dict(content="none", actors=["chatgpt:review"])]):
            self.run_export(f"o{i}", **kw)
        self.assertEqual(before, tree_hash(self.root))

    def test_public_destination_refused(self):
        """export: a destination inside a public or unconfirmed GitHub repository is refused"""
        repo = git_repo(self.tmp / "somerepo", '[remote "origin"]\n\turl = https://github.com/example/public-thing.git\n')
        for vis, refused in (("public", True), ("unknown", True), ("private", False)):
            ex.repo_visibility = lambda r, v=vis: v
            dest = repo / f"exp-{vis}"
            if refused:
                with self.assertRaises(ex.Refused):
                    ex.export(self.root, dest)
                self.assertFalse(dest.exists())
            else:
                ex.export(self.root, dest)
                self.assertTrue((dest / "manifest.json").exists())
        ex.repo_visibility = lambda r: "public"
        m = ex.export(self.root, repo / "exp-allowed", allow_public_dest=True)
        self.assertTrue(m["destination"]["allow_public_dest"])
        with self.assertRaises(ex.Refused):   # never into the records themselves
            ex.export(self.root, self.root / "export")

    def test_metadata_only(self):
        """export: metadata content leaves out free text and records the redaction policy"""
        m, recs, out = self.run_export(content="metadata")
        body = (out / "records.jsonl").read_text(encoding="utf-8")
        for secret in ("Plan the survey", "Here is a plan", "Use the synthetic survey", "Sample is small", "Looks fine", "Review the draft"):
            self.assertNotIn(secret, body)
        self.assertEqual(m["redaction"]["content"], "metadata")
        self.assertTrue(m["redaction"]["suppressed_fields"])
        t1u = next(r for r in recs if r.get("mid") == "T1u" and r["source_path"] == "captures/a.json")
        self.assertEqual(t1u["text_hash"]["sha256"], hashlib.sha256(CONV_A["turns"][0]["messages"][0]["text"].encode()).hexdigest())
        _, recs_none, out_none = self.run_export("none", content="none")
        self.assertNotIn("_hash", (out_none / "records.jsonl").read_text(encoding="utf-8").replace("source_hash", "").replace("source_file_hash", ""))

    def test_cli(self):
        """export: python -m tools export runs read-only from the command line and refuses bad input"""
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        ok = subprocess.run([sys.executable, "-B", "-m", "tools", "export", "--root", str(self.root), "--out", str(self.tmp / "cli"),
                             "--lane", "RS", "--granularity", "inventory"], cwd=KIT, capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertTrue((self.tmp / "cli" / "manifest.json").exists())
        bad = subprocess.run([sys.executable, "-B", "-m", "tools", "export", "--root", str(self.root), "--out", str(self.tmp / "cli2"),
                              "--kinds", "everything"], cwd=KIT, capture_output=True, text=True, env=env, timeout=60)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("refused", bad.stderr)


class ReviewFixTests(ExportTests):
    """Fixes from the fresh-context review of PR #16."""
    test_two_actors = test_round_trip = test_relay_distinguishable = test_gaps_reported = None
    test_decisions_vs_proposals = test_deterministic = test_no_mutation = test_public_destination_refused = None
    test_metadata_only = test_cli = None

    def repo(self, name, config):
        return git_repo(self.tmp / name, config)

    def test_unconfirmed_remotes_refused(self):
        """export: a remote whose visibility can't be confirmed (GitLab, ssh alias, unresolved insteadOf, unreadable .git) is refused; no remote is allowed"""
        calls = []
        ex.repo_visibility = lambda repo: calls.append(repo) or "private"
        remote = '[remote "origin"]\n\turl = {}\n'
        cases = {
            "gitlab": remote.format("https://gitlab.com/example/public-thing.git"),
            "ssh-alias": remote.format("git@github-work:example/public-thing.git"),
            "unresolved-alias": remote.format("gh:example/public-thing"),
        }
        for name, cfg in cases.items():
            with self.assertRaises(ex.Refused, msg=name):
                ex.export(self.root, self.repo(name, cfg) / "exp")
        self.assertEqual(calls, [])   # none of them was mistaken for a checkable GitHub remote
        aliased = self.repo("alias", '[url "https://github.com/"]\n\tinsteadOf = gh:\n' + remote.format("gh:example/public-thing"))
        ex.repo_visibility = lambda repo: calls.append(repo) or "public"
        with self.assertRaises(ex.Refused):
            ex.export(self.root, aliased / "exp")
        self.assertEqual(calls, ["example/public-thing"])   # the insteadOf alias was resolved, then found public
        broken = self.tmp / "broken"
        broken.mkdir()
        (broken / ".git").write_text("gitdir: /nonexistent/place\n", encoding="utf-8")
        with self.assertRaises(ex.Refused):
            ex.export(self.root, broken / "exp")
        m = ex.export(self.root, self.repo("no-remote", "") / "exp")
        self.assertEqual(m["destination"]["inside_git"][0]["visibility"], "no remote")

    def test_sessions_keep_case_and_match_whole_segments(self):
        """export: session ids keep their case and match a whole link segment, never a substring"""
        h = HANDOFF_DONE.replace("claude:research#SESSA1", "claude:research#SessMix9").replace("HO-RS-20261101-AB12", "HO-RS-20261105-EF56")
        h = h.replace("- 2026-11-01T12:05Z", "- 2026-11-01T12:04Z | acknowledged | claude:research#0000 | short\n- 2026-11-01T12:05Z")
        (self.root / "handoffs" / "HO-RS-20261105-EF56.md").write_text(h, encoding="utf-8")
        for name, url in (("c.json", "https://claude.ai/code/session_SessMix9"),
                          ("d.json", "https://chatgpt.com/c/00000000-0000-0000-0000-00000000000d")):
            c = json.loads(json.dumps(CONV_B))
            del c["meta"]["actor"], c["meta"]["lane"]
            c["meta"]["source_url"], c["meta"]["conversation_id"] = url, url.rsplit("/", 1)[-1]
            (self.root / "captures" / name).write_text(json.dumps(c), encoding="utf-8")
        _, recs, _ = self.run_export(kinds=["conversations", "handoffs"], granularity="inventory")
        by = {r["source_path"]: r for r in recs}
        self.assertEqual(by["captures/c.json"]["actor"], "claude:research#SessMix9")
        self.assertEqual(by["captures/c.json"]["actor_source"], "records session id")
        self.assertIsNone(by["captures/d.json"]["actor"])   # '0000' is inside its id, but isn't a segment of it
        self.assertIn("claude:research#SessMix9", by["handoffs/HO-RS-20261105-EF56.md"]["actors"])

    def test_personas_and_shared_codes_are_not_lanes(self):
        """export: only declared lanes count as lanes; a persona name or a shared lane code adds none"""
        cfg = {"owner": "sam", "lanes": {"research": "RS", "review": "RV", "ops": "OP", "ops-rel": "OP"}}
        (self.root / "handoffs" / ".ledger.json").write_text(json.dumps(cfg), encoding="utf-8")
        h = HANDOFF_OPEN.replace("HO-RS-20261102-CD34", "HO-OP-20261102-GH78").replace("from: claude:review", "from: claude:ops") \
            .replace("to: chatgpt:review", "to: chatgpt:nova").replace("claude:review#SESSR9", "claude:ops#OPS1")
        (self.root / "handoffs" / "HO-OP-20261102-GH78.md").write_text(h, encoding="utf-8")
        m, recs, _ = self.run_export(kinds=["handoffs"])
        ho = next(r for r in recs if r["rid"] == "handoff:HO-OP-20261102-GH78")
        self.assertEqual(ho["lanes"], ["ops"])
        self.assertEqual(ho["lane"], "ops")
        g = m["coverage_gaps"]
        self.assertNotIn("nova", g["lanes_without_capture"])
        nova = next(a for a in g["actors_without_capture"] if a["actor"] == "chatgpt:nova")
        self.assertIsNone(nova["lane"])

    def test_in_force(self):
        """export: in_force separates adopted decisions still in force from rejected and superseded ones"""
        extra = [dict(DECISIONS[0], id="DEC-003", status="rejected"),
                 dict(DECISIONS[0], id="DEC-004", decision="Use survey v2.", supersedes="DEC-001")]
        with open(self.root / "governance" / "decisions.jsonl", "a", encoding="utf-8") as f:
            f.write("".join(json.dumps(d) + "\n" for d in extra))
        _, recs, _ = self.run_export(kinds=["decisions"])
        force = {r["ledger_id"]: (r["evidence_class"], r["in_force"]) for r in recs}
        self.assertEqual(force, {"DEC-001": ("decision", False), "DEC-002": ("proposal", False),
                                 "DEC-003": ("decision", False), "DEC-004": ("decision", True)})

    def test_manifest_has_no_local_paths(self):
        """export: the manifest names the root by folder name and hash, not by its local path"""
        for content in ("full", "metadata", "none"):
            m, _, out = self.run_export(f"p-{content}", content=content)
            text = (out / "manifest.json").read_text(encoding="utf-8")
            self.assertNotIn(str(self.tmp), text)
            self.assertEqual(m["root"]["name"], "records" if content != "none" else "h:" + hashlib.sha256(b"records").hexdigest()[:16])
            self.assertEqual(len(m["root"]["root_hash"]), 64)

    def test_metadata_nits(self):
        """export: metadata hides labels and keeps hashes of log notes; bad handoffs and unknown capture fields are reported"""
        (self.root / "handoffs" / "HO-RS-20261106-JK90.md").write_text("---\nid: HO-RS-20261106-JK90\ntitle: t\n---\nno sections\n", encoding="utf-8")
        c = json.loads(json.dumps(CONV_B))
        c["meta"]["mystery"] = 1
        c["turns"][0]["messages"][0]["extra"] = True
        (self.root / "captures" / "e.json").write_text(json.dumps(c).replace("0000000b", "0000000e"), encoding="utf-8")
        m, recs, out = self.run_export(content="metadata")
        body = (out / "records.jsonl").read_text(encoding="utf-8")
        self.assertNotIn('"label"', body)
        ho = next(r for r in recs if r["rid"] == "handoff:HO-RS-20261101-AB12")
        self.assertTrue(all(e["note_suppressed"] and len(e["note_hash"]["sha256"]) == 64 for e in ho["log"]))
        self.assertIn("handoffs/HO-RS-20261106-JK90.md", [u["path"] for u in m["coverage_gaps"]["unreadable"]])
        self.assertNotIn("handoff:HO-RS-20261106-JK90", [r["rid"] for r in recs])
        self.assertEqual(m["coverage_gaps"]["unknown_fields"]["conversation.meta"], ["mystery"])
        self.assertEqual(m["coverage_gaps"]["unknown_fields"]["conversation.message"], ["extra"])

    def test_unauthenticated_404_is_not_private(self):
        """export: an unauthenticated GitHub 404 is unknown, not private, and the destination is refused"""
        import urllib.error
        ex.repo_visibility = self._vis
        saved = (ex.urllib.request.urlopen, ex.authenticated_visibility)
        def not_found(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
        try:
            ex.urllib.request.urlopen, ex.authenticated_visibility = not_found, (lambda repo: None)
            self.assertEqual(ex.repo_visibility("example/maybe-private"), "unknown")
            repo = self.repo("r404", '[remote "origin"]\n\turl = https://github.com/example/maybe-private.git\n')
            with self.assertRaises(ex.Refused):
                ex.export(self.root, repo / "exp")
            ex.authenticated_visibility = lambda repo: "private"   # only an authenticated answer confirms privacy
            self.assertEqual(ex.repo_visibility("example/maybe-private"), "private")
        finally:
            ex.urllib.request.urlopen, ex.authenticated_visibility = saved

    def test_included_remote_detected(self):
        """export: a remote declared in an included git config is found, and without git an include is refused"""
        inc = self.tmp / "extra.gitconfig"
        inc.write_text('[remote "origin"]\n\turl = https://github.com/example/public-thing.git\n', encoding="utf-8")
        repo = self.repo("inc", f"[include]\n\tpath = {inc}\n")
        seen = []
        ex.repo_visibility = lambda r: seen.append(r) or "public"
        with self.assertRaises(ex.Refused):
            ex.export(self.root, repo / "exp")
        self.assertEqual(seen, ["example/public-thing"])
        saved = ex.GIT
        try:
            ex.GIT = str(self.tmp / "no-such-git")
            ex.repo_visibility = lambda r: "private"
            with self.assertRaises(ex.Refused):
                ex.export(self.root, repo / "exp2")
        finally:
            ex.GIT = saved

    def test_sanitize_manifest(self):
        """export: --sanitize-manifest (implied by --content none) replaces links, paths and session ids with stable hashes"""
        for kw in (dict(sanitize=True), dict(content="none")):
            m, recs, out = self.run_export("s-" + "-".join(kw), **kw)
            text = (out / "manifest.json").read_text(encoding="utf-8") + (out / "records.jsonl").read_text(encoding="utf-8")
            for leak in ("https://", "captures/a.json", "handoffs/HO-RS", "SESSA1", "SESSR9", "00000000-0000"):
                self.assertNotIn(leak, text, (kw, leak))
            self.assertTrue(m["sanitize"]["enabled"])
            self.assertTrue(m["sanitize"]["replaced"]["links"])
            a = next(r for r in recs if r["kind"] == "conversation" and r["actor"].startswith("claude:research#"))
            self.assertEqual(a["actor"], "claude:research#" + "h:" + hashlib.sha256(b"SESSA1").hexdigest()[:16])
        m1, _, o1 = self.run_export("s1", sanitize=True)
        m2, _, o2 = self.run_export("s1", sanitize=True)
        self.assertEqual((o1 / "records.jsonl").read_bytes(), (o2 / "records.jsonl").read_bytes())
        m, _, _ = self.run_export("plain")
        self.assertFalse(m["sanitize"]["enabled"])
        self.assertIn("don't anonymise", m["sanitize"]["policy"])


if __name__ == "__main__":
    unittest.main()
