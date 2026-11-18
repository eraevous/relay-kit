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


def row(i, text, status="adopted", date="2026-11-01", locator=None):
    r = {"id": f"DEC-{i:03d}", "date": date, "status": status, "decision": text, "scope": "s"}
    if locator:
        r["locator"] = locator
    return json.dumps(r)


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

    def test_removed_decision_needs_a_qualifier(self):
        """dec-shadow: a revised decision later removed from the ledger doesn't resolve bare; its versions come back in history order"""
        for i in range(6):   # Codex #67, round 3: never pick an old revision by hash order
            self.write([row(1, "first"), row(2, "second, final"), row(3, "from x"), row(4, "from y"), row(5, f"draft {i}")])
            git(self.repo, "commit", "-qam", f"5 v{i}")
        self.write([row(1, "first"), row(2, "second, final"), row(3, "from x"), row(4, "from y")]); git(self.repo, "commit", "-qam", "drop 5")
        r = DS.resolve_ref(json.loads(json.dumps(DS.pin(self.repo), sort_keys=True)), "DEC-005")   # as the CLI reads a pin file
        self.assertEqual(r["result"], "unresolved")
        self.assertEqual([c["line"] for c in r["candidates"]], [5] * 6)
        commits = git(self.repo, "log", "--reverse", "--format=%H", "--", "governance/decisions.jsonl").stdout.split()
        self.assertEqual([c["commit"] for c in r["candidates"]], [c for c in commits if c in {x["commit"] for x in r["candidates"]}])

    def test_two_lines_one_id(self):
        """dec-shadow: two lines carrying one id in one file make a bare reference, and DEC-N@that commit, ambiguous"""
        self.write([row(1, "first"), row(1, "a correction")]); git(self.repo, "commit", "-qam", "dup")
        p, head = DS.pin(self.repo), git(self.repo, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(DS.resolve_ref(p, "DEC-001")["result"], "ambiguous")
        self.assertEqual(DS.resolve_ref(p, f"DEC-001@{head[:10]}", self.repo)["result"], "ambiguous")   # Codex #67
        self.assertNotEqual(DS.resolve_ref(p, f"DEC-001@{head[:10]}")["result"], "resolved")

    def test_merge_resolution_keeps_both_allocations(self):
        """dec-shadow: two branches take one id and a merge keeps the second's row; a bare reference stays ambiguous"""
        r = self.repo   # Codex #67, round 2: the kept row came from the other parent, so it isn't a revision
        base = [row(1, "first"), row(2, "second, final"), row(3, "from x"), row(4, "from y")]
        git(r, "checkout", "-q", "-b", "p", "main"); self.write(base + [row(5, "from p")]); git(r, "commit", "-qam", "p")
        git(r, "checkout", "-q", "-b", "q", "main"); self.write(base + [row(5, "from q")]); git(r, "commit", "-qam", "q")
        git(r, "checkout", "-q", "main"); git(r, "merge", "-q", "--no-ff", "--no-edit", "p")
        git(r, "merge", "-q", "--no-edit", "q")   # conflicts; resolved by keeping q's row
        self.write(base + [row(5, "from q")]); git(r, "commit", "-qam", "merge q, keep q")
        p = DS.pin(r)
        self.assertEqual(DS.resolve_ref(p, "DEC-005")["result"], "ambiguous")
        self.assertIn("DEC-005", DS.collisions(p))

    def test_reuse_in_place_is_not_a_revision(self):
        """dec-shadow: a different decision written over an id (new date and locator) leaves a bare reference ambiguous"""
        base = [row(1, "first"), row(2, "second, final"), row(3, "from x"), row(4, "from y")]   # Kate, YDFR: continuity must be shown
        self.write(base + [row(5, "one decision", locator="chat A 09:00")]); git(self.repo, "commit", "-qam", "5")
        self.write(base + [row(5, "an unrelated decision", date="2026-11-02", locator="chat B 14:00")]); git(self.repo, "commit", "-qam", "reuse 5")
        self.assertEqual(DS.resolve_ref(DS.pin(self.repo), "DEC-005")["result"], "ambiguous")

    def test_renumber_with_edit_stays_ambiguous(self):
        """dec-shadow: a renumber that also edits the moved text still leaves the contested id ambiguous"""
        r, base = self.repo, [row(1, "first"), row(2, "second, final"), row(3, "from x"), row(4, "from y")]
        git(r, "checkout", "-q", "-b", "p", "main"); self.write(base + [row(5, "from p", locator="chat P")]); git(r, "commit", "-qam", "p")
        git(r, "checkout", "-q", "-b", "q", "main"); self.write(base + [row(5, "from q", locator="chat Q")]); git(r, "commit", "-qam", "q")
        git(r, "checkout", "-q", "main"); git(r, "merge", "-q", "--no-ff", "--no-edit", "p")
        git(r, "checkout", "-q", "q"); git(r, "merge", "-q", "-s", "ours", "--no-edit", "main")
        self.write(base + [row(5, "from p", locator="chat P"), row(6, "from q, reworded", locator="chat Q")])
        git(r, "commit", "-qam", "renumber and edit"); git(r, "checkout", "-q", "main"); git(r, "merge", "-q", "--no-ff", "--no-edit", "q")
        p = DS.pin(r)
        self.assertEqual(DS.resolve_ref(p, "DEC-005")["result"], "ambiguous")
        self.assertEqual(DS.resolve_ref(p, "DEC-006")["result"], "resolved")

    def test_restored_text_breaks_a_revision_link(self):
        """dec-shadow: a text moved away, the new id revised, then the text restored under its old id: the new id is ambiguous"""
        r, base = self.repo, [row(1, "first"), row(2, "second, final")]   # Copilot #67: every snapshot occurrence counts
        for rows, msg in (([row(5, "A")], "add 5"), ([row(6, "A")], "move to 6"), ([row(6, "B")], "revise 6"),
                          ([row(5, "A"), row(6, "B")], "restore 5")):
            self.write(base + rows); git(r, "commit", "-qam", msg)
        self.assertEqual(DS.resolve_ref(DS.pin(r), "DEC-006")["result"], "ambiguous")

    def test_commit_outside_the_pin_counts_every_row(self):
        """dec-shadow: DEC-N@commit off the pinned history counts unindexed rows too: two rows ambiguous, one alone outside the pin"""
        r = self.repo   # Copilot #67
        p = DS.pin(r)
        git(r, "checkout", "-q", "-b", "z", "main")
        self.write([row(1, "first"), row(1, "a rival first"), row(2, "second, final")]); git(r, "commit", "-qam", "z")
        z = git(r, "rev-parse", "HEAD").stdout.strip()[:10]
        self.write([row(1, "only a rival"), row(2, "second, final")]); git(r, "commit", "-qam", "z2")
        z2 = git(r, "rev-parse", "HEAD").stdout.strip()[:10]
        self.assertEqual(DS.resolve_ref(p, f"DEC-001@{z}", r)["result"], "ambiguous")
        self.assertEqual(DS.resolve_ref(p, f"DEC-001@{z2}", r)["result"], "unresolved")

    def test_revision_after_renumber(self):
        """dec-shadow: correcting the text of a renumbered decision keeps its new id resolvable; the old id stays ambiguous"""
        self.write([row(1, "first"), row(2, "second, final"), row(3, "from x"), row(4, "from y, corrected")])
        git(self.repo, "commit", "-qam", "fix 4")   # Codex #67: the destination id's own revision is not a collision
        p = DS.pin(self.repo)
        four = DS.resolve_ref(p, "DEC-004")
        self.assertEqual((four["result"], four["text_revised"]), ("resolved", True))
        self.assertEqual(DS.resolve_ref(p, "DEC-003")["result"], "ambiguous")

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
        weak = [{"key": "K1", "status": "adopted", "reading": {}},   # Copilot #67: the fields check_ledgers requires
                {"key": "K2", "status": "adopted", "reading": {"said": "s", "read_as": "r", "confirmed": "maybe"}},
                {"key": "K3", "status": "adopted", "reading": {"said": "s", "read_as": "r", "confirmed": {"words": "yes", "locator": "x"}}}]
        self.assertEqual(DS.reading_gaps(weak), ["K1", "K2"])


if __name__ == "__main__":
    unittest.main()
