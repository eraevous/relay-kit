"""Tests for tools/github.py (the kit's one GitHub client) and relay-kit/prs@4's coverage reporting.
Standard library only; no network: transports are fakes.

Run alone with `python3 -m unittest tests.test_github`, or as part of tests/run_tests.py."""
import importlib.util, json, pathlib, shutil, subprocess, tempfile, unittest

KIT = pathlib.Path(__file__).resolve().parent.parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


GH = load("relaykit_github_under_test", KIT / "tools" / "github.py")
PRS = load("relaykit_prs_for_github", KIT / "tools" / "prs.py")
TC = load("relaykit_test_cockpit_for_github", KIT / "tests" / "test_cockpit.py")


class Script:
    """A transport that answers from a list of (status, headers, body) per path, in order."""
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, path, headers):
        self.calls.append((path, dict(headers)))
        queue = self.answers.get(path) or self.answers.get("*")
        if not queue:
            raise AssertionError(f"unexpected {path}")
        a = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(a, Exception):
            raise a
        status, h, body = a
        return status, h, body if isinstance(body, str) else json.dumps(body)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="relaykit-gh-"))
        self.slept = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def client(self, t, **kw):
        return GH.Client(t, sleep=self.slept.append, clock=lambda: 1000, **kw)

    def test_pages(self):
        """github: pages walks pages until the limit or a short page, and says whether more were available"""
        t = Script({"x?per_page=2&page=1": [(200, {}, [1, 2])], "x?per_page=2&page=2": [(200, {}, [3])]})
        self.assertEqual(self.client(t).pages("x", limit=5, per_page=2), ([1, 2, 3], False))
        pages = {"x?a=1&per_page=2&page=1": [(200, {}, [1, 2])], "x?a=1&per_page=2&page=2": [(200, {}, [3, 4])],
                 "x?a=1&per_page=2&page=3": [(200, {}, [5])]}
        self.assertEqual(self.client(Script(pages)).pages("x?a=1", limit=4, per_page=2), ([1, 2, 3, 4], True))
        pages["x?a=1&per_page=2&page=3"] = [(200, {}, [])]       # exactly the limit: nothing more
        self.assertEqual(self.client(Script(pages)).pages("x?a=1", limit=4, per_page=2), ([1, 2, 3, 4], False))
        t = Script({"y?per_page=4&page=1": [(200, {}, [1, 2, 3])]})   # one past the limit is asked for
        self.assertEqual(self.client(t).pages("y", limit=3), ([1, 2, 3], False))
        t = Script({"z?per_page=2&page=1": [(200, {}, [1, 2])]})
        self.assertEqual(self.client(t).pages("z", limit=9, per_page=2, max_pages=1), ([1, 2], None))   # pages ran out: unknown

    def test_retries(self):
        """github: network errors and 5xx are retried with backoff; other errors and exhausted retries are refused with the status"""
        t = Script({"p": [OSError("reset"), (502, {}, "bad gateway"), (200, {}, {"ok": 1})]})
        c = self.client(t)
        self.assertEqual(c.get("p"), {"ok": 1})
        self.assertEqual((c.stats["retries"], self.slept), (2, [1, 2]))
        with self.assertRaisesRegex(GH.Refused, "HTTP 404: Not Found"):
            self.client(Script({"q": [(404, {}, {"message": "Not Found"})]})).get("q")
        with self.assertRaisesRegex(GH.Refused, "HTTP 503"):
            self.client(Script({"r": [(503, {}, "")]}), retries=2).get("r")

    def test_rate_limit(self):
        """github: a rate limit waits for the reset when it is short, and is refused with the reset time when it isn't"""
        t = Script({"p": [(403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1030"}, {"message": "rate"}),
                          (200, {"x-ratelimit-remaining": "4999"}, [])]})
        c = self.client(t)
        self.assertEqual(c.get("p"), [])
        self.assertEqual((self.slept, c.stats["rate_limit_waits"], c.stats["rate_limit_remaining"]), ([30], 1, 4999))
        t = Script({"p": [(429, {"x-ratelimit-reset": "99999"}, "")]})
        with self.assertRaisesRegex(GH.Refused, "rate limited \\(until 1970-01-02T03:46:39Z\\)"):
            self.client(t, max_wait=60).get("p")
        self.slept.clear()     # GitHub's secondary limit: a 403 with retry-after, requests still left
        t = Script({"q": [(403, {"retry-after": "5", "x-ratelimit-remaining": "4000"}, {"message": "secondary"}), (200, {}, [])]})
        self.assertEqual(self.client(t).get("q"), [])
        self.assertEqual(self.slept, [5])

    def test_cache(self):
        """github: a path is fetched once per run; with a cache folder an unchanged resource is revalidated by its ETag"""
        t = Script({"p": [(200, {"etag": '"e1"'}, {"v": 1})]})
        c = self.client(t, cache_dir=self.tmp / "cache")
        self.assertEqual((c.get("p"), c.get("p")), ({"v": 1}, {"v": 1}))
        self.assertEqual((len(t.calls), c.stats["memo_hits"]), (1, 1))
        t2 = Script({"p": [(304, {}, "")]})
        c2 = self.client(t2, cache_dir=self.tmp / "cache")
        self.assertEqual(c2.get("p"), {"v": 1})
        self.assertEqual((t2.calls[0][1], c2.stats["not_modified"]), ({"If-None-Match": '"e1"'}, 1))
        f = list((self.tmp / "cache").iterdir())[0]
        self.assertEqual((f.suffix, f.stat().st_mode & 0o777, (self.tmp / "cache").stat().st_mode & 0o777), (".json", 0o600, 0o700))
        c3 = self.client(Script({"p": [(200, {}, {"v": 2})]}))            # no cache folder: nothing is kept
        self.assertEqual(c3.get("p"), {"v": 2})

    def test_read_only(self):
        """github: the gh transport only ever sends GET, and parses status and headers from its output"""
        seen = []
        real = subprocess.run
        def fake(cmd, **kw):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "HTTP/2.0 200 OK\r\nEtag: \"x\"\r\nX-Ratelimit-Remaining: 7\r\n\r\n[1]", "")
        subprocess.run = fake
        try:
            st, h, body = GH.gh_transport()("repos/o/r/pulls", {"If-None-Match": '"x"'})
        finally:
            subprocess.run = real
        self.assertEqual(seen[0][:5], ["gh", "api", "-i", "--method", "GET"])
        self.assertEqual((st, h["etag"], h["x-ratelimit-remaining"], body), (200, '"x"', "7", "[1]"))
        src = (KIT / "tools" / "github.py").read_text(encoding="utf-8")
        for verb in ("POST", "PATCH", "PUT", "DELETE"):
            self.assertNotIn(f'"{verb}"', src)

    def test_redirect_auth_stays_on_secure_api_origin(self):
        """github: bearer auth never crosses a scheme, port, userinfo or host boundary."""
        handler = GH._NoAuthElsewhere()
        for target, keep in [
            ("https://api.github.com/repos/o/renamed", True),
            ("https://api.github.com:443/repos/o/renamed", True),
            ("http://api.github.com/repos/o/r", False),
            ("https://api.github.com:444/repos/o/r", False),
            ("https://api.github.com.evil.invalid/repos/o/r", False),
            ("https://other.invalid/repos/o/r", False),
            ("https://user" + "@" + "api.github.com/repos/o/r", False),
            ("https://api.github.com:invalid/repos/o/r", False),
        ]:
            with self.subTest(target=target):
                req = GH.urllib.request.Request("https://api.github.com/repos/o/r",
                    headers={"Authorization": "Bearer synthetic-test-token", "Accept": "application/json"})
                redirected = handler.redirect_request(req, None, 302, "Found", {}, target)
                self.assertEqual(redirected.get_header("Authorization"),
                                 "Bearer synthetic-test-token" if keep else None)
                self.assertEqual(redirected.get_header("Accept"), "application/json")

    def test_utf8(self):
        """github: gh's output is read as UTF-8 whatever the system's code page (Windows cp1252 broke on review text), and no output at all is a refusal, not a crash"""
        real, kws = subprocess.run, []
        def fake(cmd, **kw):
            kws.append(kw)
            return subprocess.CompletedProcess(cmd, 0, "HTTP/2.0 200 OK\r\n\r\n[\"\u201cok\u201d\"]", "")
        subprocess.run = fake
        try:
            self.assertEqual(GH.gh_transport()("p", {})[2], '["\u201cok\u201d"]')
            subprocess.run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, None, "gh: not signed in")
            with self.assertRaises(GH.Refused):
                GH.gh_transport()("p", {})
        finally:
            subprocess.run = real
        self.assertEqual(kws[0].get("encoding"), "utf-8")
        import re
        for f in list((KIT / "tools").glob("*.py")) + list((KIT / "skills").rglob("*.py")):     # every reader of a subprocess
            src = f.read_text(encoding="utf-8")
            self.assertEqual(re.findall(r"text=True(?!, encoding=)", src), [], f"{f.name}: text=True without encoding")

    def test_coverage(self):
        """prs: what was asked for and what came back is recorded per repository; unreadable reviews or CI are unread, not none"""
        root = TC.build_root(self.tmp / "r")
        pr = {"number": 7, "html_url": "https://github.com/o/r/pull/7", "title": "t", "state": "open", "user": {"login": "sam"},
              "body": "## What\nx", "created_at": "2026-11-02T00:00:00Z", "updated_at": "2026-11-03T00:00:00Z",
              "base": {"ref": "main"}, "head": {"ref": "b", "sha": "a" * 40}}
        t = Script({"repos/o/r": [(200, {}, {"default_branch": "main", "pushed_at": "2026-11-03T01:00:00Z"})],
                    "repos/o/r/pulls?state=all&sort=updated&direction=desc&per_page=2&page=1": [(200, {}, [pr])],
                    "repos/o/r/pulls/7/reviews?per_page=100&page=1": [(403, {}, {"message": "Resource not accessible"})],
                    "repos/o/r/issues/7/comments?per_page=100&page=1": [(200, {}, [])],
                    f"repos/o/r/commits/{'a' * 40}/check-runs?per_page=100": [(200, {}, {"check_runs": []})],
                    f"repos/o/r/commits/{'a' * 40}/status": [(200, {}, {"statuses": []})]})
        c = PRS.GH.Client(t, sleep=self.slept.append, clock=lambda: 1000)   # the reader's own client module
        f = PRS.run(root, c, "test", ["o/r"], limit=1, ts="2026-11-03T12:00:00Z")
        cov = f["coverage"]["o/r"]
        self.assertEqual((cov["listed"], cov["more_available"], cov["prs_read"]), (1, False, 1))
        self.assertEqual([(x["number"], x["what"]) for x in cov["partial"]], [(7, "reviews")])
        self.assertEqual(f["revisions"]["o/r"], {"default_branch": "main", "pushed_at": "2026-11-03T01:00:00Z"})
        self.assertEqual(f["client"]["requests"], 6)   # @3: one more read per PR, its issue comments
        line = [json.loads(x) for x in (root / PRS.OUT).read_text().splitlines()][0]
        self.assertEqual((line["schema"], line["unread"], line["reviews"]), ("relay-kit/prs@4", ["reviews"], []))
        data = TC.ck.collect(root)
        item = data["pull_requests"]["items"][0]
        self.assertEqual(item["unread"], ["reviews"])
        out = self.tmp / "o"
        TC.ck.ex.repo_visibility = lambda repo: "unknown"
        TC.ck.build(root, out)
        page = (out / "index.html").read_text(encoding="utf-8")
        self.assertIn("not read (the reader couldn't get them)", page)
        self.assertIn("1 partial read(s)", page)


    def test_partial_and_churn(self):
        """prs: a PR that can't be read again is a partial read, not a failed repository; an unread part keeps its last value, recorded once as unread"""
        root = TC.build_root(self.tmp / "r2")
        def pr(n, state="open"):
            return {"number": n, "html_url": f"https://github.com/o/r/pull/{n}", "title": "t", "state": state, "user": {"login": "sam"},
                    "body": "## What\nx", "created_at": "2026-11-02T00:00:00Z", "updated_at": "2026-11-03T00:00:00Z",
                    "base": {"ref": "main"}, "head": {"ref": "b", "sha": "a" * 40}}
        ok = {"repos/o/r": [(200, {}, {"default_branch": "main"})],
              "repos/o/r/pulls?state=all&sort=updated&direction=desc&per_page=31&page=1": [(200, {}, [pr(7), pr(8)])],
              "repos/o/r/pulls/7/reviews?per_page=100&page=1": [(200, {}, [{"id": 1, "user": {"login": "x"}, "state": "APPROVED", "body": ""}])],
              "repos/o/r/pulls/7/comments?per_page=100&page=1": [(200, {}, [])],
              "repos/o/r/pulls/8/reviews?per_page=100&page=1": [(200, {}, [])],
              "repos/o/r/issues/7/comments?per_page=100&page=1": [(200, {}, [])],
              "repos/o/r/issues/8/comments?per_page=100&page=1": [(200, {}, [])],
              f"repos/o/r/commits/{'a' * 40}/check-runs?per_page=100": [(200, {}, {"check_runs": []})],
              f"repos/o/r/commits/{'a' * 40}/status": [(200, {}, {"statuses": []})]}
        mk = lambda answers: PRS.GH.Client(Script(answers), sleep=self.slept.append, clock=lambda: 1000)
        PRS.run(root, mk(ok), "test", ["o/r"], ts="2026-11-03T12:00:00Z")
        flaky = dict(ok)
        flaky["repos/o/r/pulls/7/reviews?per_page=100&page=1"] = [(502, {}, "bad")]
        f = PRS.run(root, mk(flaky), "test", ["o/r"], ts="2026-11-03T13:00:00Z")
        self.assertEqual(f["appended"], 1)                            # reviews unread: the last value stands, marked unread once (Kate 1 on #33)
        self.assertEqual([x["what"] for x in f["coverage"]["o/r"]["partial"]], ["reviews"])
        f = PRS.run(root, mk(flaky), "test", ["o/r"], ts="2026-11-03T13:30:00Z")
        self.assertEqual(f["appended"], 0)                            # the same failure again: no new line
        gone = dict(ok)
        gone["repos/o/r/pulls?state=all&sort=updated&direction=desc&per_page=31&page=1"] = [(200, {}, [pr(8)])]
        gone["repos/o/r/pulls/7"] = [(404, {}, {"message": "Not Found"})]
        f = PRS.run(root, mk(gone), "test", ["o/r"], ts="2026-11-03T14:00:00Z")
        self.assertEqual((f["ok"], f["failed"]), (["o/r"], {}))
        self.assertEqual([(x["number"], x["what"]) for x in f["coverage"]["o/r"]["partial"]], [(7, "pull request")])
        # a client loaded from another copy of github.py: its Refused still marks the repository failed
        other = GH.Client(Script({"*": [(404, {}, {"message": "Not Found"})]}), sleep=self.slept.append, clock=lambda: 1000)
        f = PRS.run(root, other, "test", ["o/r"], ts="2026-11-03T15:00:00Z")
        self.assertIn("o/r", f["failed"])


if __name__ == "__main__":
    unittest.main()
