"""Tests for tools/prs.py (relay-kit/prs@4) and the Pull requests, Status and decision-support parts of
tools/cockpit.py (relay-kit/cockpit@7). Standard library only; synthetic data only; no network: the
GitHub reader is given a fake `get`.

Run alone with `python3 -m unittest tests.test_prs`, or as part of tests/run_tests.py."""
import contextlib, hashlib, importlib.util, io, json, pathlib, shutil, tempfile, unittest

KIT = pathlib.Path(__file__).resolve().parent.parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


prs = load("relaykit_prs_under_test", KIT / "tools" / "prs.py")
tc = load("relaykit_test_cockpit_fixtures", KIT / "tests" / "test_cockpit.py")
ck = tc.ck

HOSTILE = tc.HOSTILE
BODY = f"""Author: claude:research#SESSA1

## What
Adds the synthetic report. {HOSTILE}

## Why
<!-- template hint only -->

## How
```
## What
not a heading: inside a fence
```
Built and checked.
"""


def gh_pr(n, state="open", merged=False, body=BODY, updated="2026-11-03T10:00:00Z", sha="a" * 40, title="Synthetic PR"):
    return {"number": n, "html_url": f"https://github.com/example/synthetic/pull/{n}", "title": title,
            "state": state, "draft": False, "user": {"login": "sam"}, "body": body,
            "created_at": "2026-11-02T10:00:00Z", "updated_at": updated,
            "merged_at": "2026-11-03T09:00:00Z" if merged else None, "closed_at": "2026-11-03T09:00:00Z" if merged else None,
            "base": {"ref": "main"}, "head": {"ref": f"b{n}", "sha": sha}}


class FakeGitHub:
    def __init__(self, prs_by_repo, reviews=None, runs=None, statuses=None, broken=(), issue_comments=None, inline=None, broken_paths=()):
        self.prs, self.reviews, self.runs, self.statuses, self.broken = prs_by_repo, reviews or {}, runs or {}, statuses or {}, set(broken)
        self.issue_comments, self.inline, self.broken_paths = issue_comments or {}, inline or {}, set(broken_paths)
        self.calls = []

    def __call__(self, path):
        self.calls.append(path)
        repo = "/".join(path.split("/")[1:3])
        if repo in self.broken:
            raise prs.Refused(f"GET {path}: HTTP 404")
        if path.count("/") == 2:
            return {"default_branch": "main", "pushed_at": "2026-11-03T09:30:00Z"}
        if "/pulls?" in path:
            return self.prs.get(repo, [])
        if any(path.startswith(b) for b in self.broken_paths):
            raise prs.Refused(f"GET {path}: HTTP 500")
        if "/issues/" in path and "/comments" in path:
            return self.issue_comments.get(int(path.split("/issues/")[1].split("/")[0]), [])
        if "/pulls/" in path and "/comments" in path:
            return self.inline.get(int(path.split("/pulls/")[1].split("/")[0]), [])
        if "/reviews" in path:
            return self.reviews.get(int(path.split("/pulls/")[1].split("/")[0]), [])
        if path.endswith("/status"):
            return {"statuses": self.statuses.get(path.split("/commits/")[1].split("/")[0], [])}
        if "/check-runs" in path:
            return {"check_runs": self.runs.get(path.split("/commits/")[1].split("/")[0], [])}
        raise AssertionError(path)


def lines(root):
    return [json.loads(x) for x in (root / prs.OUT).read_text(encoding="utf-8").splitlines() if x.strip()]


def tree(root, skip=()):
    h = hashlib.sha256()
    for p in sorted(pathlib.Path(root).rglob("*")):
        rel = p.relative_to(root).as_posix()
        if rel in skip:
            continue
        h.update(rel.encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


class PrsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="relaykit-prs-"))
        self.root = tc.build_root(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sections(self):
        """prs: body sections are kept verbatim; missing and comment-only sections are listed as missing"""
        b = prs.parse_body(BODY)
        self.assertEqual(b["sections"]["what"], f"Adds the synthetic report. {HOSTILE}")
        self.assertNotIn("why", b["sections"])                       # only an HTML comment: missing
        self.assertIn("not a heading: inside a fence", b["sections"]["how"])   # a fenced ## isn't a heading
        self.assertEqual(b["missing"], ["why", "authorized"])
        self.assertEqual(b["author"], "claude:research#SESSA1")
        self.assertEqual(prs.parse_body("## What this changes\nX\n")["sections"], {"what": "X"})
        self.assertEqual(prs.parse_body(None)["missing"], list(prs.SECTIONS))
        tpl = prs.parse_body((KIT / "tools" / "templates" / "PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8"))
        self.assertEqual((tpl["author"], tpl["missing"]), (None, list(prs.SECTIONS)))   # the unfilled template
        self.assertEqual(prs.parse_body("Author: \t\n## What\nx")["author"], None)
        fenced = prs.parse_body("## How\n```html\n<!-- kept -->\n```\n<!-- hint -->done")["sections"]["how"]
        self.assertEqual(fenced, "```html\n<!-- kept -->\n```\ndone")
        long = prs.parse_body("## What\n" + "x" * 5000)
        self.assertEqual((len(long["sections"]["what"]), long["truncated"]), (prs.MAX_SECTION, ["what"]))

    def test_append_only_changes(self):
        """prs: an unchanged PR appends no new line; a changed one appends one, and every run appends a fetch line"""
        gh = FakeGitHub({"example/synthetic": [gh_pr(7), gh_pr(8)]})
        prs.run(self.root, gh, "test", ["example/synthetic"], ts="2026-11-03T12:00:00Z")
        first = (self.root / prs.OUT).read_text(encoding="utf-8")
        self.assertEqual([x["kind"] for x in lines(self.root)], ["pr", "pr", "fetch"])
        prs.run(self.root, gh, "test", ["example/synthetic"], ts="2026-11-03T13:00:00Z")
        self.assertEqual([x["kind"] for x in lines(self.root)], ["pr", "pr", "fetch", "fetch"])
        gh.prs["example/synthetic"][0] = gh_pr(7, state="closed", merged=True)
        f = prs.run(self.root, gh, "test", ["example/synthetic"], ts="2026-11-03T14:00:00Z")
        after = (self.root / prs.OUT).read_text(encoding="utf-8")
        self.assertTrue(after.startswith(first))                   # nothing earlier rewritten
        got = lines(self.root)
        self.assertEqual([(x["kind"], x.get("number"), x.get("state")) for x in got[-2:]], [("pr", 7, "merged"), ("fetch", None, None)])
        self.assertEqual((f["prs_seen"], f["appended"]), (2, 1))

    def test_open_reread(self):
        """prs: a PR last recorded open is read again however old, so a closed one never stays open"""
        gh = FakeGitHub({"example/synthetic": [gh_pr(7)]})
        gh.single = {}
        orig = gh.__call__
        def get(path):
            if path == "repos/example/synthetic/pulls/7":
                gh.calls.append(path)
                return gh_pr(7, state="closed", merged=True, updated="2026-10-01T00:00:00Z")
            return orig(path)
        prs.run(self.root, gh, "test", ["example/synthetic"], ts="2026-11-03T12:00:00Z")
        gh.prs["example/synthetic"] = []        # no longer among the recent ones the list returns
        prs.run(self.root, get, "test", ["example/synthetic"], ts="2026-12-30T12:00:00Z")
        last = [x for x in lines(self.root) if x["kind"] == "pr"][-1]
        self.assertEqual((last["number"], last["state"]), (7, "merged"))
        self.assertIn("repos/example/synthetic/pulls/7", gh.calls)

    def test_newline(self):
        """prs: appending to a file without a final newline doesn't join two lines"""
        path = self.root / prs.OUT
        path.write_text('{"schema": "relay-kit/prs@2", "kind": "fetch", "ts": "2026-11-01T00:00:00Z", "repos": [], "ok": [], "failed": {}}',
                        encoding="utf-8")
        prs.run(self.root, FakeGitHub({"example/synthetic": [gh_pr(7)]}), "test", ["example/synthetic"], ts="2026-11-03T12:00:00Z")
        self.assertEqual([x["kind"] for x in lines(self.root)], ["fetch", "pr", "fetch"])
        gov = self.tmp / "elsewhere"
        gov.mkdir()
        other = tc.build_root(self.tmp / "sym")
        shutil.rmtree(other / "governance")
        (other / "governance").symlink_to(gov)
        with self.assertRaises(prs.Refused):
            prs.run(other, FakeGitHub({}), "test", ["example/synthetic"])
        self.assertEqual(list(gov.iterdir()), [])

    def test_failed_repo(self):
        """prs: a failing repository is recorded in the fetch line and the others are still read"""
        gh = FakeGitHub({"example/synthetic": [gh_pr(7)]}, broken={"example/gone"})
        f = prs.run(self.root, gh, "test", ["example/gone", "example/synthetic"], ts="2026-11-03T12:00:00Z")
        self.assertEqual(f["ok"], ["example/synthetic"])
        self.assertIn("HTTP 404", f["failed"]["example/gone"])
        self.assertEqual(sum(1 for x in lines(self.root) if x["kind"] == "pr"), 1)

    def test_writes_only_its_file(self):
        """prs: it writes only governance/pr-state.jsonl and never rewrites a line"""
        before = tree(self.root)
        prs.run(self.root, FakeGitHub({"example/synthetic": [gh_pr(7)]}), "test", ["example/synthetic"], ts="2026-11-03T12:00:00Z")
        self.assertEqual(tree(self.root, skip={prs.OUT}), before)
        (self.root / prs.OUT).write_text((self.root / prs.OUT).read_text() + "not json\n", encoding="utf-8")
        with self.assertRaises(prs.Refused):                       # a damaged file is reported, not repaired
            prs.run(self.root, FakeGitHub({}), "test", ["example/synthetic"])
        out = self.tmp / "dry"
        dry_root = tc.build_root(out)
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            prs.run(dry_root, FakeGitHub({"example/synthetic": [gh_pr(7)]}), "test", ["example/synthetic"], dry_run=True)
        self.assertEqual(len(printed.getvalue().splitlines()), 2)
        self.assertFalse((dry_root / prs.OUT).exists())

    def test_ci_and_reviews(self):
        """prs: CI state from check runs and statuses; reviews keep the verdict stated in their body"""
        gh = FakeGitHub({"example/synthetic": [gh_pr(7, sha="b" * 40), gh_pr(8, sha="c" * 40), gh_pr(9, sha="d" * 40)]},
                        reviews={7: [{"user": {"login": "sam"}, "state": "COMMENTED", "submitted_at": "2026-11-03T11:00:00Z",
                                      "body": "**APPROVED (round 2).**\nReviewer: model-b (other family)"},
                                     {"user": {"login": "sam"}, "state": "COMMENTED", "submitted_at": "2026-11-03T11:30:00Z",
                                      "body": "**NOT APPROVED**: not LGTM yet; REQUEST CHANGES"},
                                     {"user": {"login": "x"}, "state": "PENDING", "body": ""}]},
                        runs={"b" * 40: [{"name": "tests", "status": "completed", "conclusion": "failure"}],
                              "c" * 40: [{"name": "tests", "status": "in_progress", "conclusion": None}]})
        prs.run(self.root, gh, "test", ["example/synthetic"], ts="2026-11-03T12:00:00Z")
        by = {x["number"]: x for x in lines(self.root) if x["kind"] == "pr"}
        self.assertEqual(by[7]["ci"], {"state": "failure", "failing": ["tests"]})
        self.assertEqual(by[8]["ci"]["state"], "pending")
        self.assertEqual(by[9]["ci"]["state"], "none")
        slim = [{k: r[k] for k in ("by", "verdict", "at", "signed", "stated")} for r in by[7]["reviews"]]
        self.assertEqual(slim, [{"by": "sam", "verdict": "COMMENTED", "at": "2026-11-03T11:00:00Z",
                                 "signed": "model-b (other family)", "stated": "APPROVED"},
                                {"by": "sam", "verdict": "COMMENTED", "at": "2026-11-03T11:30:00Z",
                                 "signed": None, "stated": "REQUEST CHANGES"}])   # negated verdicts are skipped

    def test_review_bodies(self):
        """prs: review bodies are recorded verbatim (cut at 4000, flagged), with inline-comment counts; review-style issue comments join as kind comment"""
        long_body = "Execution review (fresh Claude): 2 blocking, 10 should-fix.\n\n" + "x" * 5000
        gh = FakeGitHub({"example/synthetic": [gh_pr(7)]},
                        reviews={7: [{"id": 501, "user": {"login": "rev"}, "state": "CHANGES_REQUESTED", "submitted_at": "2026-11-03T11:00:00Z",
                                      "body": long_body}]},
                        inline={7: [{"pull_request_review_id": 501, "body": "nit"}, {"pull_request_review_id": 501, "body": "nit2"},
                                    {"pull_request_review_id": 999, "body": "other"}]},
                        issue_comments={7: [{"id": 1, "user": {"login": "sam"}, "created_at": "2026-11-03T12:00:00Z",
                                             "body": "**APPROVED (round 3).**\nReviewer: model-b\nFine."},
                                            {"id": 2, "user": {"login": "sam"}, "created_at": "2026-11-03T12:10:00Z", "body": "thanks, merging"},
                                            {"id": 3, "user": {"login": "sam"}, "created_at": "2026-11-03T12:20:00Z", "body": "  \n## Review: nothing blocking\nok"}]})
        prs.run(self.root, gh, "test", ["example/synthetic"], ts="2026-11-03T13:00:00Z")
        pr = [x for x in lines(self.root) if x["kind"] == "pr"][-1]
        self.assertEqual(pr["schema"], "relay-kit/prs@4")
        kinds = [(r["kind"], r["by"], r["stated"], r["signed"]) for r in pr["reviews"]]
        self.assertEqual(kinds, [("review", "rev", None, None), ("comment", "sam", "APPROVED", "model-b"), ("comment", "sam", None, None)])
        rv = pr["reviews"][0]
        self.assertEqual((len(rv["body"]), rv["truncated"], rv["comments"], rv["verdict"]), (prs.MAX_REVIEW, True, 2, "CHANGES_REQUESTED"))
        self.assertTrue(rv["body"].startswith("Execution review (fresh Claude): 2 blocking"))
        self.assertEqual((pr["reviews"][1]["body"], pr["reviews"][1]["truncated"], pr["reviews"][1]["verdict"]),
                         ("**APPROVED (round 3).**\nReviewer: model-b\nFine.", False, None))
        self.assertNotIn("thanks, merging", json.dumps(pr["reviews"]))   # a plain remark isn't a review (@4 keeps its excerpt under comments)
        # unchanged next run: no new pr line, even though comments were re-read
        prs.run(self.root, gh, "test", ["example/synthetic"], ts="2026-11-03T14:00:00Z")
        self.assertEqual([x["kind"] for x in lines(self.root)], ["pr", "fetch", "fetch"])

    def test_comments_unread(self):
        """prs: review-style comments that can't be read are unread, keep their last value, and never read as none"""
        gh = FakeGitHub({"example/synthetic": [gh_pr(7)]},
                        issue_comments={7: [{"id": 1, "user": {"login": "sam"}, "created_at": "2026-11-03T12:00:00Z", "body": "LGTM"}]})
        prs.run(self.root, gh, "test", ["example/synthetic"], ts="2026-11-03T13:00:00Z")
        gh.broken_paths = {"repos/example/synthetic/issues/7/comments"}
        f = prs.run(self.root, gh, "test", ["example/synthetic"], ts="2026-11-03T14:00:00Z")
        got = lines(self.root)
        self.assertEqual([x["kind"] for x in got], ["pr", "fetch", "fetch"])   # carried forward: no new line
        self.assertEqual(f["coverage"]["example/synthetic"]["partial"][0]["what"], "comments")
        # an @2 line (no bodies) followed by an @3 read of the same PR with a review: one new line, with the body
        (self.root / prs.OUT).unlink()
        gh2 = FakeGitHub({"example/synthetic": [gh_pr(8)]},
                         reviews={8: [{"id": 9, "user": {"login": "rev"}, "state": "APPROVED", "submitted_at": "2026-11-03T11:00:00Z", "body": "fine"}]})
        old = {"schema": "relay-kit/prs@2", "kind": "pr", "ts": "2026-11-01T00:00:00Z", "repo": "example/synthetic", "number": 8,
               "reviews": [{"by": "rev", "verdict": "APPROVED", "at": "2026-11-03T11:00:00Z", "signed": None, "stated": None}], "state": "open"}
        (self.root / prs.OUT).write_text(json.dumps(old) + "\n", encoding="utf-8")
        prs.run(self.root, gh2, "test", ["example/synthetic"], ts="2026-11-03T15:00:00Z")
        last = [x for x in lines(self.root) if x["kind"] == "pr"][-1]
        self.assertEqual((last["schema"], last["reviews"][0]["body"]), ("relay-kit/prs@4", "fine"))

    def test_repos(self):
        """prs: repositories come from .ledger.json repos or --repo; anything not OWNER/NAME is refused"""
        with self.assertRaises(prs.Refused):
            prs.repos_of(self.root, None)
        cfg = json.loads((self.root / "handoffs" / ".ledger.json").read_text())
        cfg["repos"] = ["example/synthetic", "example/synthetic", "example/other"]
        (self.root / "handoffs" / ".ledger.json").write_text(json.dumps(cfg))
        self.assertEqual(prs.repos_of(self.root, None), ["example/synthetic", "example/other"])
        self.assertEqual(prs.repos_of(self.root, ["a/b"]), ["a/b"])
        for bad in (["a"], ["a/b/c"], ["../x"], ["a/b c"]):
            with self.assertRaises(prs.Refused):
                prs.repos_of(self.root, bad)


class CockpitPrTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="relaykit-ckpr-"))
        self.root = tc.build_root(self.tmp)
        self._vis = ck.ex.repo_visibility
        ck.ex.repo_visibility = lambda repo: "unknown"
        gh = FakeGitHub({"example/synthetic": [
            gh_pr(7),
            gh_pr(8, body="Author: sam\n\n## What\nOwner's own change.\n## Why\nBecause.\n## How\nBy hand.\n## Authorized\nn/a\n"),
            gh_pr(9, state="closed", merged=True, body="no sections here"),
            gh_pr(10, state="closed", merged=True, body="Author: \n", updated="2026-11-03T08:00:00Z"),
        ]}, reviews={7: [{"id": 1, "user": {"login": "rev-bot"}, "state": "APPROVED", "submitted_at": "2026-11-03T11:00:00Z",
                          "body": "Execution review: 1 blocking / 3 should-fix.\n\n**Blocking:**\n1. First. <script>alert(2)</script>\n2. Second http://example.test/x\n\nParagraph two."},
                         {"id": 2, "user": {"login": "sam"}, "state": "COMMENTED", "submitted_at": "2026-11-03T11:10:00Z",
                          "body": "Reviewer: SECRETSIGNED reviewer\nlooks fine"}]},
           inline={7: [{"pull_request_review_id": 1, "body": "nit"}]},
           issue_comments={7: [{"id": 5, "user": {"login": "zed"}, "created_at": "2026-11-03T12:30:00Z", "body": "**APPROVED (round 2).** Nothing blocking here."}]})
        prs.run(self.root, gh, "test", ["example/synthetic"], ts="2026-11-03T12:00:00Z")

    def tearDown(self):
        ck.ex.repo_visibility = self._vis
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, name="out", **kw):
        out = self.tmp / name
        data = ck.build(self.root, out, **kw)
        return data, (out / "index.html").read_text(encoding="utf-8")

    def test_pr_surface(self):
        """cockpit: Pull requests renders each PR's sections verbatim and escaped, with who wrote it and what is missing"""
        data, page = self.build()
        items = {x["number"]: x for x in data["pull_requests"]["items"]}
        self.assertEqual([x["number"] for x in data["pull_requests"]["items"]], [7, 8, 9, 10])   # open first, then merged
        self.assertEqual(items[7]["sections"]["what"], f"Adds the synthetic report. {HOSTILE}")
        self.assertNotIn("<script>alert(1)", page)
        self.assertIn("&lt;/textarea&gt;&lt;/script&gt;&lt;script&gt;alert(1)", page)
        self.assertIn("sections missing: why, authorized", page)
        self.assertEqual(items[7]["reviews"][0]["family"], None)
        self.assertIn("unsigned", page)
        self.assertEqual(data["counts"]["prs_open"], 2)
        self.assertEqual(page.count('class="card pr '), 4)
        self.assertEqual(items[10]["written_by"], "unstated")             # a blank Author: line builds, as unstated
        self.assertEqual(items[7]["reviewers_counted"], 3)

    def test_written_by(self):
        """cockpit: an agent-written PR is tagged as not written by the owner; one with no Author line says so"""
        data, page = self.build()
        items = {x["number"]: x for x in data["pull_requests"]["items"]}
        self.assertEqual((items[7]["written_by"], items[8]["written_by"], items[9]["written_by"]), ("agent", "owner", "unstated"))
        self.assertIn("written by claude:research#SESSA1, not the owner", page)
        self.assertIn("no Author: line · opened by sam", page)
        self.assertEqual(items[7]["author_family"], "claude")
        (self.root / "governance" / "cockpit.json").write_text(json.dumps({"review_families": {"rev-bot": "other"}}))
        data, _ = self.build("o2")
        self.assertIs({x["number"]: x for x in data["pull_requests"]["items"]}[7]["other_family"], True)

    def test_pr_metadata(self):
        """cockpit: --content metadata shows which PR sections exist but none of their text"""
        data, page = self.build(content="metadata")
        for word in ("Adds the synthetic report", "Owner's own change", "Synthetic PR", "By hand"):
            self.assertNotIn(word, page)
        self.assertIn("written by an agent, not the owner", page)   # actors stay in metadata; the Author line's text doesn't
        js = (self.tmp / "out" / "cockpit.json").read_text(encoding="utf-8")
        for word in ("SECRETSIGNED", "Adds the synthetic report", "Synthetic PR"):
            self.assertNotIn(word, page)
            self.assertNotIn(word, js)
        self.assertIsNone({x["number"]: x for x in data["pull_requests"]["items"]}[7]["author_family"])
        self.assertIn("Sections present: what, how", page)
        self.assertEqual({x["number"]: x["sections"] for x in data["pull_requests"]["items"]}[7], {})

    def test_overtaken_from_file(self):
        """cockpit: PR states for overtaken checks come from pr-state.jsonl without any network lookup"""
        with open(self.root / "governance" / "decisions.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"id": "DEC-010", "date": "2026-11-03", "status": "proposed", "scope": "research",
                                "decision": "Merge https://github.com/example/synthetic/pull/9 and https://github.com/example/synthetic/pull/7.",
                                "recorded_by": "claude:research#SESSA1"}) + "\n")
        saved = ck.gh_pr_state
        def no_net(*a):
            raise AssertionError("network")
        ck.gh_pr_state = no_net
        try:
            data, _ = self.build()                                   # --pr-state off: the file alone, no gh
        finally:
            ck.gh_pr_state = saved
        c = next(x for x in data["choices"] if x["id"] == "DEC-010")
        self.assertIn("example/synthetic#9 is merged", c["possibly_overtaken"])
        self.assertTrue(all("pr-state.jsonl" in x["source"] for x in c["prs"]))
        # with --pr-state, gh is asked first: the file's "open" for #7 is older than what gh says now
        calls = []
        def fresh(o, r, n):
            calls.append(n)
            return "merged"
        data, _ = self.build("live", pr_state=True, pr_lookup=fresh)
        c = next(x for x in data["choices"] if x["id"] == "DEC-010")
        self.assertEqual(sorted(calls), [7, 9])
        self.assertIn("example/synthetic#7 is merged", c["possibly_overtaken"])
        self.assertTrue(all(x["source"].startswith("gh at build time") for x in c["prs"]))

    def test_theme(self):
        """cockpit: a theme can set only known variables to colours or font lists; anything else is refused"""
        good = self.tmp / "t.json"
        good.write_text(json.dumps({"name": "t", "dark": {"bg": "#000000", "mark": "var(--gold)", "heading": "Georgia, serif"}}))
        data, page = self.build(theme=good)
        for fine in ('"Source Sans 3", ui-sans-serif, Arial', "'Fira Code', monospace", "Georgia"):
            self.assertTrue(ck.FONT_VALUE.fullmatch(fine), fine)
        self.assertIn("--bg:#000000", page)
        self.assertEqual(data["query"]["theme"], "t")
        for bad in ({"dark": {"bg": "red;}</style><script>"}}, {"dark": {"nope": "#000"}}, {"dark": {"heading": "x{}"}},
                    {"dark": {"bg": "url(http://x)"}}, {"light": "x"}, {"dark": {}, "extra": 1}, {"default": "light"},
                    {"dark": {"display": 'Georgia"'}}, {"dark": {"display": "'a, b"}}, {"dark": {"display": ","}}):
            p = self.tmp / "bad.json"
            p.write_text(json.dumps(bad))
            with self.assertRaises(ck.Refused, msg=str(bad)):
                self.build("b", theme=p)

    def test_light(self):
        """cockpit: a theme with a light block adds a light scheme and a toggle; without --theme the page is unchanged dark"""
        _, plain = self.build("plain")
        self.assertNotIn("prefers-color-scheme", plain)
        self.assertNotIn('id="theme-toggle"', plain)
        t = self.tmp / "t.json"
        t.write_text(json.dumps({"name": "t", "dark": {}, "light": {"bg": "#ffffff"}, "default": "light"}))
        _, page = self.build("lit", theme=t)
        self.assertIn("@media (prefers-color-scheme: light)", page)
        self.assertIn(":root[data-theme=light]{", page)
        self.assertIn("--bg:#ffffff", page)
        self.assertIn("--text:" + ck.LIGHT_DEFAULTS["text"], page)   # names it doesn't set come from the light defaults
        self.assertIn('<html lang="en" data-theme="light">', page)
        self.assertIn('id="theme-toggle"', page)

    def test_landing(self):
        """cockpit: --landing opens the page on the named surface; an unknown surface is refused"""
        data, page = self.build(landing="prs")
        self.assertIn('data-landing="prs"', page)
        with self.assertRaises(ck.Refused):
            self.build("x", landing="nowhere")
        (self.root / "governance" / "cockpit.json").write_text(json.dumps({"landing": "choices"}))
        data, _ = self.build("y")
        self.assertEqual(data["query"]["landing"], "choices")

    def test_recent(self):
        """cockpit: merged or closed pull requests older than the reader's window are counted, not listed"""
        with open(self.root / prs.OUT, "a", encoding="utf-8") as f:
            old = dict(lines(self.root)[0], number=3, state="merged", merged_at="2026-09-01T00:00:00Z", updated_at="2026-09-01T00:00:00Z")
            f.write(json.dumps(old) + "\n")
        data, page = self.build()
        self.assertFalse({x["number"]: x for x in data["pull_requests"]["items"]}[3]["recent"])
        self.assertIn("1 older merged or closed pull request(s) are in the file but not shown", page)
        self.assertNotIn("example/synthetic#3", page)

    def test_review_fold(self):
        """cockpit: reviews fold under the card with author, family or unsigned, kind, verdict, time and the body as escaped paragraphs; Findings only as stated"""
        data, page = self.build()
        items = {x["number"]: x for x in data["pull_requests"]["items"]}
        rv = items[7]["reviews"][0]
        self.assertEqual((rv["kind"], rv["comments"], rv["findings"]), ("review", 1, {"blocking": 1, "should_fix": 3}))
        self.assertIn("Findings, as the review states them: 1 blocking / 3 should-fix", page)
        self.assertIn('<details class="pr-reviews">', page)
        self.assertIn("&lt;script&gt;alert(2)&lt;/script&gt;", page)
        self.assertNotIn("<script>alert(2)", page)
        self.assertIn("<ol><li>First. &lt;script&gt;alert(2)&lt;/script&gt;</li><li>Second http://example.test/x</li></ol>", page)
        self.assertNotIn('href="http://example.test/x"', page)        # URLs are text, never links
        self.assertIn("1 inline comment (on GitHub)", page)
        # DEC-141 run 1: both verdicts labelled, reviewers counted apart from reviews, unsigned visible
        # (rev-bot's fixture review is APPROVED with no stated verdict; the reader's CHANGES_REQUESTED case is in PrsTests)
        self.assertIn("GitHub: approved · stated: none", page)
        self.assertIn("GitHub: comment, no review state · stated: APPROVED", page)
        self.assertIn("3 reviews from 3 reviewers", page)
        self.assertIn('<span class="tag warn unsigned">unsigned</span>', page)
        c7 = items[7]["reviews"][2]
        self.assertEqual((c7["kind"], c7["verdict"], c7["stated"], c7["who"]), ("comment", None, "APPROVED", "zed"))
        self.assertIn("review-style comment", page)
        self.assertEqual(items[7]["reviewers_counted"], 3)             # a stated verdict in a comment counts as a review
        self.assertNotIn("Findings, as the review states them: 0", page)   # "Nothing blocking" states no count
        # a review from the owner's lane signed with a lane actor: family from the signature, labelled signed
        self.assertEqual(items[7]["reviews"][1]["family_source"], None)   # "SECRETSIGNED reviewer" maps to no family
        # metadata: no bodies anywhere
        _, mpage = self.build("meta", content="metadata")
        for word in ("First.", "looks fine", "Nothing blocking here"):
            self.assertNotIn(word, mpage)
        self.assertIn("body left out (--content metadata)", mpage)

    def test_family_inferred(self):
        """cockpit: a family taken from the login (the records' actors map or review_families) is marked inferred; a Reviewer: signature is marked signed"""
        cfg = json.loads((self.root / "handoffs" / ".ledger.json").read_text())
        cfg["actors"] = {"rev-bot": "copilot", "sam": {"family": "owner-account", "lanes": ["research"]}}
        (self.root / "handoffs" / ".ledger.json").write_text(json.dumps(cfg))
        data, page = self.build()
        items = {x["number"]: x for x in data["pull_requests"]["items"]}
        self.assertEqual((items[7]["reviews"][0]["family"], items[7]["reviews"][0]["family_source"]), ("copilot", "login"))
        self.assertIn("copilot (inferred from login)", page)
        (self.root / "governance" / "cockpit.json").write_text(json.dumps({"review_families": {"secretsigned": "model-b", "rev-bot": "bot"}}))
        data, page = self.build("o2")
        items = {x["number"]: x for x in data["pull_requests"]["items"]}
        self.assertEqual((items[7]["reviews"][1]["family"], items[7]["reviews"][1]["family_source"]), ("model-b", "signed"))
        self.assertEqual(items[7]["reviews"][0]["family"], "bot")       # cockpit.json's explicit mapping wins over actors
        self.assertIn("model-b (signed)", page)

    def test_old_schema_body(self):
        """cockpit: a review recorded by prs@2 (no body) says the body isn't recorded, rather than showing an empty one"""
        old = {"schema": "relay-kit/prs@2", "kind": "pr", "ts": "2026-11-04T00:00:00Z", "repo": "example/synthetic", "number": 11,
               "url": "https://github.com/example/synthetic/pull/11", "title": "Old", "state": "open", "draft": False, "author": "sam",
               "updated_at": "2026-11-04T00:00:00Z", "reviews": [{"by": "rev-bot", "verdict": "APPROVED", "at": "2026-11-03T11:00:00Z", "signed": None, "stated": None}],
               "ci": {"state": "none", "failing": []}, "body_sections": {}, "missing": list(prs.SECTIONS), "truncated": []}
        with open(self.root / prs.OUT, "a", encoding="utf-8") as f:
            f.write(json.dumps(old) + "\n")
        data, page = self.build()
        rv = {x["number"]: x for x in data["pull_requests"]["items"]}[11]["reviews"][0]
        self.assertEqual((rv["body_recorded"], rv["body"]), (False, None))
        self.assertIn("body not recorded: this line was read by relay-kit prs@2 or earlier", page)

    def test_no_file(self):
        """cockpit: without pr-state.jsonl the Pull requests surface says how to fill it and nothing is fetched"""
        (self.root / prs.OUT).unlink()
        data, page = self.build()
        self.assertFalse(data["pull_requests"]["present"])
        self.assertIn("relay-kit prs --root RECORDS", page)


EDITION = """---
date: 2026-11-03
edition: morning
author: claude:coord#S1
covers: 2026-11-02 → 2026-11-03
---

# Status · Monday, morning

Written **by hand**. <script>alert(3)</script> and <b>not bold</b>. <!-- a hint, removed -->

## Accomplishments

- **Thing one** landed (`code`), see [the PR](https://github.com/example/synthetic/pull/7).
- Thing two

## Road bumps

1. Ids collided
2. CI broke

## Status

- Waiting on you: 1 item.
- Handoffs in progress: 9 — this and that.
- Open PRs: 14. Reviews on none.

---

## Next steps

**Yours:**
1. Decide.

```
## not a heading
```
"""


class CockpitStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="relaykit-ckst-"))
        self.root = tc.build_root(self.tmp)
        self._vis = ck.ex.repo_visibility
        ck.ex.repo_visibility = lambda repo: "unknown"
        (self.root / "status").mkdir()
        (self.root / "status" / "2026-11-03-morning.md").write_text(EDITION, encoding="utf-8")
        (self.root / "status" / "2026-11-02-evening.md").write_text(EDITION.replace("2026-11-03", "2026-11-02").replace("edition: morning", "edition: evening"), encoding="utf-8")
        (self.root / "status" / "2026-11-02-morning.md").write_text(EDITION.replace("2026-11-03", "2026-11-02").replace("## Status\n", "## Standing\n"), encoding="utf-8")
        (self.root / "status" / "notes.txt").write_text("x", encoding="utf-8")

    def tearDown(self):
        ck.ex.repo_visibility = self._vis
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, name="out", **kw):
        out = self.tmp / name
        data = ck.build(self.root, out, **kw)
        return data, (out / "index.html").read_text(encoding="utf-8")

    def test_status_surface(self):
        """cockpit: Status renders the newest edition verbatim from the markdown subset (escaped; links as text), indexes older ones, and can be the landing"""
        data, page = self.build(landing="status")
        st = data["status"]
        self.assertEqual((st["latest"]["date"], st["latest"]["edition"], st["latest"]["author"]), ("2026-11-03", "morning", "claude:coord#S1"))
        self.assertEqual([(o["date"], o["edition"]) for o in st["older"]], [("2026-11-02", "evening"), ("2026-11-02", "morning")])
        self.assertEqual(st["older"][1]["sections_missing"], ["Status"])
        self.assertEqual(st["unreadable"], [{"file": "notes.txt", "reason": "name isn't <YYYY-MM-DD>-<edition>.md"}])
        self.assertIn('data-landing="status"', page)
        self.assertIn("<h2>Status · Monday, morning</h2>", page)
        self.assertIn("<h3>Accomplishments</h3>", page)
        self.assertIn("<li><b>Thing one</b> landed (<code>code</code>), see the PR <span class=\"url\">(https://github.com/example/synthetic/pull/7)</span>.</li>", page)
        self.assertIn("<ol><li>Ids collided</li><li>CI broke</li></ol>", page)
        self.assertIn("&lt;script&gt;alert(3)&lt;/script&gt; and &lt;b&gt;not bold&lt;/b&gt;", page)
        self.assertNotIn("<script>alert(3)", page)
        self.assertIn("<pre>## not a heading</pre>", page)
        self.assertNotIn("a hint, removed", page)
        self.assertIn("<hr>", page)
        self.assertIn("written by claude:coord#S1, not the owner", page)
        self.assertIn("Written by <b>claude:coord#S1</b> · 2026-11-03 morning · covers", page)
        self.assertEqual(ck.stated_counts(EDITION), {"waiting on you": 1, "handoffs in progress": 9, "open prs": 14})
        self.assertIn('<p class="stat-label">OPEN PRS</p><strong>14</strong><small>as the edition states it</small>', page)
        self.assertIn("Older editions · 2", page)
        narrative = page[page.index('<div class="narrative">'):page.index("Older editions")]
        self.assertNotIn("<a ", narrative)                                # links are text in the narrative
        self.assertEqual(data["counts"]["status_editions"], 3)
        # stale: the newest record in the fixture is 2026-11-03; an edition dated 2026-10-20 is older than drift_days
        shutil.rmtree(self.root / "status")
        (self.root / "status").mkdir()
        (self.root / "status" / "2026-10-20-morning.md").write_text(EDITION.replace("2026-11-03", "2026-10-20"), encoding="utf-8")
        data, page = self.build("stale")
        self.assertTrue(data["status"]["latest"]["stale"])
        self.assertIn("STALE · 14 days before the newest record (limit 3)", page)
        _, mpage = self.build("meta", content="metadata")
        self.assertNotIn("Thing one", mpage)
        self.assertIn("Body left out (--content metadata)", mpage)

    def test_edition_tool(self):
        """status: `relay-kit edition new` scaffolds the front matter and four headings and never overwrites; `check` reports what doesn't fit"""
        se = load("relaykit_status_edition_under_test", KIT / "tools" / "status_edition.py")
        p = se.new(self.root, "midday", "2026-11-03", "claude:coord#S1", "lunch")
        text = p.read_text(encoding="utf-8")
        self.assertEqual(p.name, "2026-11-03-midday.md")
        for line in ("date: 2026-11-03", "edition: midday", "author: claude:coord#S1", "covers: lunch", "contract: relay-kit/status@1",
                     "## Accomplishments", "## Road bumps", "## Status", "## Next steps"):
            self.assertIn(line, text)
        with self.assertRaises(se.Refused):
            se.new(self.root, "midday", "2026-11-03", "claude:coord#S1", None)      # exists: added, never rewritten
        for bad in (("Morning", "2026-11-03", "a#1"), ("morning", "2026-13-03", "a#1"), ("morning", "2026-11-03", "")):
            with self.assertRaises((se.Refused, ValueError)):
                se.new(self.root, *bad, None)
        probs = se.check(self.root)
        self.assertIn(("notes.txt", "name isn't <YYYY-MM-DD>-<edition>.md"), probs)
        self.assertIn(("status/2026-11-02-morning.md", "sections missing: Status"), probs)
        self.assertNotIn("status/2026-11-03-midday.md", [f for f, _ in probs])
        data, page = self.build()
        self.assertEqual(data["status"]["latest"]["edition"], "midday")             # midday is later than morning on one day
        self.assertIn("<h3>Accomplishments</h3>", page)

    def test_status_absent(self):
        """cockpit: without a status folder the Status surface says how to write an edition"""
        shutil.rmtree(self.root / "status")
        data, page = self.build()
        self.assertFalse(data["status"]["present"])
        self.assertIn("No editions in <code>status/</code> yet", page)

    def test_decision_support(self):
        """cockpit: decision-support notes render per item, one block per author, independent authors before the coordinator, latest per author with history; mechanics folded"""
        notes = [{"id": "DS-001", "item": "CH-001", "author": "claude:coord#S1", "at": "2026-11-03T09:00Z", "contract": "relay-kit/decision-support@1",
                  "changes_if_accepted": "Coord says: (1) A changes. (2) B changes.", "unchanged": "Repos.", "counterfactual": "Stays open.",
                  "can_wait": "Yes.", "depends": "HO-X.", "recommendation": "Defer (coord, v1)."},
                 {"id": "DS-002", "item": "CH-001", "author": "chatgpt:kate#K1", "at": "2026-11-03T09:30Z", "contract": "relay-kit/decision-support@1",
                  "changes_if_accepted": "Kate says A.", "recommendation": "Resolve now <script>alert(4)</script>."},
                 {"id": "DS-003", "item": "CH-001", "author": "claude:coord#S2", "at": "2026-11-03T10:00Z", "contract": "relay-kit/decision-support@1",
                  "changes_if_accepted": "Coord says: A changes.", "recommendation": "Defer (coord, v2)."},
                 "not json"]
        (self.root / "governance" / "decision-support.jsonl").write_text("".join((json.dumps(n) if isinstance(n, dict) else n) + "\n" for n in notes), encoding="utf-8")
        cfg = json.loads((self.root / "handoffs" / ".ledger.json").read_text())
        cfg["lanes"]["coord"] = "KO"
        (self.root / "handoffs" / ".ledger.json").write_text(json.dumps(cfg))
        data, page = self.build()
        ch = next(c for c in data["choices"] if c["id"] == "CH-001")
        self.assertEqual([(b["author_base"], b["role"], b["current"]["id"], len(b["history"])) for b in ch["support"]],
                         [("chatgpt:kate", "independent", "DS-002", 0), ("claude:coord", "coordinator", "DS-003", 1)])
        self.assertEqual(data["decision_support"]["unreadable"], 1)
        self.assertEqual(data["counts"]["choices_without_support"], 2)
        self.assertLess(page.index("Kate says A."), page.index("Coord says: A changes."))
        self.assertIn("Resolve now &lt;script&gt;alert(4)&lt;/script&gt;.", page)
        self.assertIn("1 earlier revision(s)", page)
        self.assertIn('<div class="ds-glances">', page)                       # recommendations side by side, Kate's first
        self.assertLess(page.index("chatgpt:kate · RECOMMENDATION"), page.index("claude:coord · RECOMMENDATION"))
        self.assertIn("Defer (coord, v1).", page)
        self.assertIn("No decision-support note yet", page)
        self.assertIn('<details class="mech"><summary>how the ledger records this</summary>', page)
        self.assertLess(page.index("DECISION SUPPORT"), page.index("WHAT EACH ACTION DOES"))
        for label in ("What changes", "What doesn't", "If nothing is done", "Can it wait", "Depends on it", "Recommendation"):
            self.assertIn(f"<dt>{ck.e(label)}</dt>", page)
        _, mpage = self.build("meta", content="metadata")
        self.assertNotIn("Kate says A.", mpage)
        self.assertIn("fields left out (--content metadata)", mpage)

    def test_structured_text(self):
        """cockpit: record text with (1)…(n), numbered or dashed lines, or line breaks renders as lists and paragraphs, escaped"""
        h = ck.structured("Seven findings: (1) CONFIRMED a. (2) b <x>. (3) c. Proposed sequence: resolver first.")
        self.assertEqual(h, "<p>Seven findings:</p><ol><li>CONFIRMED a.</li><li>b &lt;x&gt;.</li><li>c. Proposed sequence: resolver first.</li></ol>")
        self.assertEqual(ck.structured("intro\n- one\n- two\n\nafter"), "<p>intro</p><ul><li>one</li><li>two</li></ul><p>after</p>")
        self.assertEqual(ck.structured("1. a\n2. b\nc"), "<ol><li>a</li><li>b</li></ol><p>c</p>")
        self.assertEqual(ck.structured("see (2) and (1)"), "<p>see (2) and (1)</p>")          # out of order: not an enumeration
        self.assertEqual(ck.structured("**raw** `md`"), "<p>**raw** `md`</p>")                # records aren't markdown
        with open(self.root / "governance" / "challenges.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"id": "CH-035", "date": "2026-11-03", "status": "open", "kind": "review", "raised_by": "ext",
                                "concern": "Seven findings: (1) one. (2) two. (3) three.", "recorded_by": "claude:research#SESSA1"}) + "\n")
        _, page = self.build()
        self.assertIn('<div class="txt rec"><p>Seven findings:</p><ol><li>one.</li><li>two.</li><li>three.</li></ol></div>', page)
        self.assertIn('<span class="tag items">lists 3 items</span>', page)   # the count near the title (DEC-141 run 1)
        self.assertEqual(ck.enumerated_count("1. a\n2. b\n3. c"), 3)
        self.assertEqual(ck.enumerated_count("see (2) then (1)"), 0)


if __name__ == "__main__":
    unittest.main()


class DraftWaitTests(unittest.TestCase):
    """relay-kit/prs@4 (HO-CKPT-20261107-AVAA, DEC-173): which drafts wait on the owner, from recorded words only.
    Synthetic PRs modelled on the three known cases of 2026-11-07: an owner question the coordinator answered
    asking for his word; a title that says 'draft for owner review'; a lane saying it is iterating."""
    OWNERS = {"sam", "owner"}

    def draft(self, n, title="Synthetic draft", comments=(), reviews=(), sections=None, author_line="claude:lane#S1", requested=()):
        return {"schema": "relay-kit/prs@4", "kind": "pr", "repo": "example/synthetic", "number": n, "state": "open", "draft": True,
                "url": f"https://github.com/example/synthetic/pull/{n}", "title": title, "author": "sam", "author_line": author_line,
                "body_sections": sections or {"what": "Adds a thing."}, "comments": list(comments), "reviews": list(reviews),
                "review_requested": list(requested), "ci": {"state": "success", "failing": []}, "unread": []}

    def c(self, at, body, signed=None):
        return {"id": at, "by": "sam", "at": at, "signed": signed, "excerpt": body, "truncated": False}

    def test_rules(self):
        """prs: an open draft is classified by fixed rules over its recorded words (W1–W4 waiting on the owner, L1–L3 a lane or review, P0 parked), first rule wins, with the rule named; lines older than @4 say comments weren't recorded"""
        owner_q = self.c("2026-11-07T06:01:07Z", "Why is this still in draft?")
        coord = self.c("2026-11-07T21:33:05Z", "Status, for the owner's question: it is iterating under the study. "
                       "Two options: mark it ready, or leave it in draft. The lane's next wake can do either on your word. — claude:kairos-coord",
                       signed="claude:kairos-coord")
        cases = {
            "unanswered": (self.draft(1, comments=[owner_q]), ("owner", "W1")),
            "answered-asks-word": (self.draft(2, title="distiller v0.2 (draft, iterating)", comments=[owner_q, coord]), ("owner", "W2")),
            "question-back": (self.draft(3, comments=[owner_q, self.c("2026-11-07T22:00:00Z", "Should it go to review now? — claude:lane", "claude:lane")]), ("owner", "W2")),
            "title-marker": (self.draft(4, title="docs: enforcement audit — draft for owner review"), ("owner", "W3")),
            "reviewed-not-blocking": (self.draft(5, reviews=[{"kind": "review", "by": "sam", "signed": "chatgpt:kate", "verdict": "COMMENTED",
                                                               "stated": None, "body": "Reviewer: chatgpt:kate\nOne finding, not blocking."}]), ("owner", "W4")),
            "reviewed-blocking": (self.draft(6, reviews=[{"kind": "review", "by": "sam", "signed": "chatgpt:kate", "verdict": "COMMENTED",
                                                           "stated": None, "body": "Reviewer: chatgpt:kate\nFindings: 2 blocking / 1 should-fix."}]), ("lane", "L3")),
            "same-family-review": (self.draft(7, reviews=[{"kind": "review", "by": "sam", "signed": "claude:other", "verdict": "APPROVED", "stated": None, "body": "fine"}]), ("parked", "P0")),
            "requested": (self.draft(8, requested=["rev-bot"]), ("lane", "L1")),
            "iterating": (self.draft(9, comments=[self.c("2026-11-07T20:00:00Z", "Still iterating on the fixtures; next wake. — claude:lane", "claude:lane")]), ("lane", "L2")),
            "parked": (self.draft(10), ("parked", "P0")),
        }
        for name, (rec, want) in cases.items():
            got = prs.classify_draft(rec, self.OWNERS)
            self.assertEqual((got["on"], got["rule"]), want, name)
            self.assertTrue(got["why"], name)
        self.assertIsNone(prs.classify_draft({**self.draft(11), "draft": False}, self.OWNERS))
        self.assertIsNone(prs.classify_draft({**self.draft(12), "state": "merged"}, self.OWNERS))
        old = {k: v for k, v in self.draft(13).items() if k != "comments"}   # a @2/@3 line: comments were never recorded
        self.assertIn("not recorded before prs@4", prs.classify_draft(old, self.OWNERS)["why"])

    def test_reader_records_comments_and_requests(self):
        """prs: every issue comment's opening words (600 characters, flagged when cut), the actor id it is signed with, and the logins a review is requested from are recorded; --waiting-on reads the file and fetches nothing"""
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="relaykit-dw-"))
        try:
            root = tc.build_root(tmp)
            pr = gh_pr(21, title="draft one")
            pr["draft"], pr["requested_reviewers"] = True, [{"login": "rev-bot"}]
            gh = FakeGitHub({"example/synthetic": [pr]},
                            issue_comments={21: [{"id": 1, "user": {"login": "sam"}, "created_at": "2026-11-03T12:30:00Z", "body": "Why is this still in draft?"},
                                                 {"id": 2, "user": {"login": "sam"}, "created_at": "2026-11-03T12:40:00Z", "body": "x" * 700 + "\n— claude:lane#S1"}]})
            prs.run(root, gh, "test", ["example/synthetic"], ts="2026-11-03T13:00:00Z")
            line = [json.loads(l) for l in (root / prs.OUT).read_text(encoding="utf-8").splitlines() if '"kind": "pr"' in l][0]
            self.assertEqual(line["review_requested"], ["rev-bot"])
            # the signature is read from the whole comment, so a sign-off past the 600-character cut still counts
            self.assertEqual([(c["signed"], c["truncated"], len(c["excerpt"])) for c in line["comments"]], [(None, False, 27), ("claude:lane#S1", True, 600)])
            rows = prs.waiting_on([line], self.OWNERS, "all")
            self.assertEqual([(r["number"], r["on"], r["rule"]) for r in rows], [(21, "lane", "L1")])   # the owner's question was answered; a review is requested
            line["comments"].pop()
            self.assertEqual(prs.classify_draft(line, self.OWNERS)["rule"], "W1")   # unsigned newest comment = the owner's words, unanswered
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                prs.main(["--root", str(root), "--waiting-on", "lane"])
            self.assertIn("example/synthetic#21\tlane\tL1", out.getvalue())
            self.assertEqual(len(gh.calls), 6)   # --waiting-on fetched nothing more
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_cockpit_shows_the_wait(self):
        """cockpit: each open draft's card says whom it waits on and the rule that fired; cockpit.json lists the drafts waiting on the owner and counts them"""
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="relaykit-dwc-"))
        try:
            root = tc.build_root(tmp)
            a, b = gh_pr(31, title="docs: audit — draft for owner review"), gh_pr(32, title="kit: quiet draft")
            a["draft"] = b["draft"] = True
            gh = FakeGitHub({"example/synthetic": [a, b, gh_pr(33)]})
            prs.run(root, gh, "test", ["example/synthetic"], ts="2026-11-03T13:00:00Z")
            vis, ck.ex.repo_visibility = ck.ex.repo_visibility, (lambda repo: "unknown")
            try:
                data = ck.build(root, tmp / "out")
            finally:
                ck.ex.repo_visibility = vis
            html_ = (tmp / "out" / "index.html").read_text(encoding="utf-8")
            waits = {x["number"]: x["waiting"] for x in data["pull_requests"]["items"]}
            self.assertEqual((waits[31]["on"], waits[31]["rule"], waits[32]["on"], waits[33]), ("owner", "W3", "parked", None))
            self.assertEqual((data["counts"]["drafts_open"], data["counts"]["drafts_waiting_on_owner"]), (2, 1))
            self.assertEqual([d["number"] for d in data["pull_requests"]["drafts_waiting_on_owner"]], [31])
            self.assertIn("draft waiting on you</span> <span class=\"rule\">rule W3</span>: title says &quot;owner review&quot;", html_)
            self.assertIn("draft parked</span> <span class=\"rule\">rule P0</span>", html_)
            self.assertIn("W1 owner-unanswered", data["pull_requests"]["draft_rules"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
