"""relay-kit/dec-shadow@1 (Docket item B, DEC-251): the read-only DEC compatibility shadow.
Synthetic data only; scratch git repositories; no network.

Run alone with `python3 -m unittest tests.test_dec_shadow`, or as part of tests/run_tests.py."""
import importlib.util, json, os, pathlib, subprocess, sys, tempfile, unittest

KIT = pathlib.Path(__file__).resolve().parent.parent
FIX = json.loads((KIT / "tests" / "fixtures" / "dec_shadow_cases.json").read_text(encoding="utf-8"))["cases"]
spec = importlib.util.spec_from_file_location("relaykit_dec_shadow_under_test", KIT / "tools" / "dec_shadow.py")
DS = importlib.util.module_from_spec(spec)
spec.loader.exec_module(DS)
MAIL = "t" + "@" + "example.invalid"   # split so the prepublish scan doesn't read it as a real address
ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL=MAIL, GIT_COMMITTER_NAME="t",
           GIT_COMMITTER_EMAIL=MAIL, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], env=ENV, capture_output=True, text=True)


def row(i, text, status="adopted"):
    return json.dumps({"id": f"DEC-{i:03d}", "date": "2026-11-01", "status": status, "decision": text, "scope": "s"})


class Models(unittest.TestCase):
    def test_cases(self):
        """dec-shadow: both interpretations give the fixture's standing, merge conflicts and problems on every lifecycle case"""
        for c in FIX:
            for model, exp in c["expect"].items():
                with self.subTest(case=c["name"], model=model):
                    got = DS.run_case(c, model)
                    if "standing" in exp:
                        self.assertEqual({k: got["standing"].get(k) for k in exp["standing"]}, exp["standing"])
                    if "merge_conflicts" in exp:
                        self.assertEqual(got["merge_conflicts"], exp["merge_conflicts"])
                    self.assertEqual(sorted({p["kind"] for p in got["problems"]}), sorted(exp["problem_kinds"]))
                    if "latest_file_reader" in exp:
                        self.assertEqual({k: got["latest_file_reader"].get(k) for k in exp["latest_file_reader"]},
                                         exp["latest_file_reader"])

    def test_no_accidental_revival(self):
        """dec-shadow: a reverted withdrawal never reads as adopted; the naive latest-file reader does, and the shadow says so"""
        c = next(x for x in FIX if x["name"] == "revert_of_withdrawal")
        for model in ("event", "version"):
            got = DS.run_case(c, model)
            self.assertEqual(got["standing"]["K-AAAA"], "withdrawn")
            self.assertEqual(got["latest_file_reader"]["K-AAAA"], "adopted")
            self.assertIn("inconsistent", {p["kind"] for p in got["problems"]})

    def test_merge3_matches_git(self):
        """dec-shadow: the path-level merge model agrees with git on add/add (different bytes conflict, identical bytes merge clean)"""
        for same in (True, False):
            with tempfile.TemporaryDirectory() as d:
                git(d, "init", "-q", "-b", "main")
                pathlib.Path(d, "base").write_text("x\n"); git(d, "add", "."); git(d, "commit", "-qm", "base")
                for br, body in (("a", "one\n"), ("b", "one\n" if same else "two\n")):
                    git(d, "checkout", "-q", "-b", br, "main")
                    pathlib.Path(d, "f.json").write_text(body); git(d, "add", "."); git(d, "commit", "-qm", br)
                git(d, "checkout", "-q", "a")
                real = git(d, "merge", "-q", "--no-edit", "b").returncode != 0
                _, model = DS.merge3({}, {"f.json": b"one\n"}, {"f.json": b"one\n" if same else b"two\n"})
                self.assertEqual(real, bool(model), f"same={same}")


class Legacy(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        r = self.repo = pathlib.Path(self.d.name)
        git(r, "init", "-q", "-b", "main")
        (r / "governance").mkdir()
        self.led = r / "governance" / "decisions.jsonl"
        self.write([row(1, "first"), row(2, "second draft")])
        git(r, "add", "."); git(r, "commit", "-qm", "two")
        self.write([row(1, "first"), row(2, "second, final")])           # an in-place text revision
        git(r, "commit", "-qam", "revise 2")
        # collision era: two branches both take DEC-003; the second renumbers to DEC-004 after the first merges
        git(r, "checkout", "-q", "-b", "x")
        self.write([row(1, "first"), row(2, "second, final"), row(3, "from x")]); git(r, "commit", "-qam", "x")
        git(r, "checkout", "-q", "-b", "y", "main")
        self.write([row(1, "first"), row(2, "second, final"), row(3, "from y")]); git(r, "commit", "-qam", "y")
        git(r, "checkout", "-q", "main"); git(r, "merge", "-q", "--no-ff", "--no-edit", "x")
        git(r, "checkout", "-q", "y"); git(r, "merge", "-q", "-s", "ours", "--no-edit", "main")   # y catches up, then renumbers
        self.write([row(1, "first"), row(2, "second, final"), row(3, "from x"), row(4, "from y")]); git(r, "commit", "-qam", "renumber")
        git(r, "checkout", "-q", "main"); git(r, "merge", "-q", "--no-ff", "--no-edit", "y")
        self.pin = DS.pin(r)

    def tearDown(self):
        self.d.cleanup()

    def write(self, lines):
        self.led.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_bare_reference_in_collision_era_is_ambiguous(self):
        """dec-shadow: a bare reference to an id two decisions carried fails as ambiguous, listing both, never redirected"""
        r = DS.resolve_ref(self.pin, "DEC-003")
        self.assertEqual(r["result"], "ambiguous")
        self.assertEqual(len(r["candidates"]), 2)
        self.assertIn("DEC-004", r["why"])
        four = DS.resolve_ref(self.pin, "DEC-004")   # only ever y's decision: resolves, and says where else it was
        self.assertEqual((four["result"], four["also_carried_by"]), ("resolved", ["DEC-003"]))

    def test_digest_or_commit_picks_one(self):
        """dec-shadow: DEC-N#digest and DEC-N@commit resolve one line's bytes, even in the collision era"""
        cands = DS.resolve_ref(self.pin, "DEC-003")["candidates"]
        for c in cands:
            by_d = DS.resolve_ref(self.pin, f"DEC-003#{c['digest'][:12]}")
            by_c = DS.resolve_ref(self.pin, f"DEC-003@{c['commit'][:10]}")
            self.assertEqual((by_d["result"], by_d["digest"]), ("resolved", c["digest"]))
            self.assertEqual((by_c["result"], by_c["digest"]), ("resolved", c["digest"]))
        head = git(self.repo, "rev-parse", "HEAD").stdout.strip()   # bytes first seen earlier, cited at a later commit
        self.assertEqual(DS.resolve_ref(self.pin, f"DEC-001@{head[:10]}")["result"], "unresolved")
        self.assertEqual(DS.resolve_ref(self.pin, f"DEC-001@{head[:10]}", self.repo)["digest"], self.pin["current"]["DEC-001"])

    def test_text_revision_is_not_a_collision(self):
        """dec-shadow: text changed in place under one id resolves to the current line, marked as revised"""
        r = DS.resolve_ref(self.pin, "DEC-002")
        self.assertEqual((r["result"], r["revisions"], r["text_revised"]), ("resolved", 2, True))
        self.assertEqual(r["digest"], self.pin["current"]["DEC-002"])
        self.assertEqual(DS.resolve_ref(self.pin, "DEC-009")["result"], "unknown")
        self.assertEqual(set(DS.collisions(self.pin)), {"DEC-003"})

    def test_two_lines_one_id(self):
        """dec-shadow: two lines carrying one id in the same file make a bare reference ambiguous"""
        self.write([row(1, "first"), row(1, "a correction")]); git(self.repo, "commit", "-qam", "dup")
        self.assertEqual(DS.resolve_ref(DS.pin(self.repo), "DEC-001")["result"], "ambiguous")

    def test_prefix_and_view(self):
        """dec-shadow: the generated view keeps the legacy lines byte for byte; a sorted-key rewrite of the same content does not"""
        old = self.led.read_bytes()
        new = DS.view(old, [{"key": "DEC-20261117-AB12", "status": "adopted", "decision": "n"}])
        self.assertTrue(DS.prefix_check(old, new, 4)["identical"])
        resorted = b"".join(json.dumps(json.loads(x), sort_keys=True).encode() + b"\n" for x in old.splitlines())
        bad = DS.prefix_check(old, resorted, 4)
        self.assertFalse(bad["identical"])
        self.assertEqual(bad["first_difference"], 1)

    def test_standing_is_separate_from_row_status(self):
        """dec-shadow: standing at snapshot S reports a proposed row that a later line settled, apart from the row's own status"""
        self.write([row(1, "first", "proposed"), json.dumps({"id": "DEC-002", "date": "2026-11-02", "status": "adopted",
                    "decision": "yes", "scope": "s", "decider": "o", "basis": "b", "locator": "l", "supersedes": "DEC-001"})])
        git(self.repo, "commit", "-qam", "settle")
        s = DS.standing(self.repo, "HEAD")
        self.assertEqual(s["row_status_differs_from_standing"]["DEC-001"]["row_status"], "proposed")
        self.assertEqual(s["row_status_differs_from_standing"]["DEC-001"]["standing"], "settled")

    def test_reading_gaps(self):
        """dec-shadow: a new-format adopted record without reading evidence is reported, since the numeric threshold can't see it"""
        recs = [{"key": "DEC-20261117-AB12", "status": "adopted"},
                {"key": "DEC-20261117-CD34", "status": "adopted", "reading": {"said": "s", "read_as": "r", "confirmed": "explicit"}},
                {"id": "DEC-300", "status": "adopted"}]
        self.assertEqual(DS.reading_gaps(recs), ["DEC-20261117-AB12"])


if __name__ == "__main__":
    unittest.main()
