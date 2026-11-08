"""Tests for tools/telemetry.py (relay-kit/telemetry@2): a derived view rebuilt from the records.
Standard library only; synthetic data only; no network.

Run alone with `python3 -m unittest tests.test_telemetry`, or as part of tests/run_tests.py."""
import importlib.util, json, pathlib, shutil, subprocess, sys, tempfile, unittest

KIT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("relaykit_test_cockpit_for_telemetry", KIT / "tests" / "test_cockpit.py")
TC = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(TC)
CK = TC.ck
TEL = CK.TEL


def pr(n, created, merged=None, state=None, reviews=(), title="t", sections=None, repo="o/r"):
    return {"schema": "relay-kit/prs@2", "kind": "pr", "ts": "2026-12-01T00:00:00Z", "repo": repo, "number": n, "title": title,
            "state": state or ("merged" if merged else "open"), "created_at": created, "merged_at": merged,
            "reviews": list(reviews), "body_sections": sections or {}, "unread": []}


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="relaykit-tel-"))
        self.root = TC.build_root(self.tmp)
        self._vis = CK.ex.repo_visibility
        CK.ex.repo_visibility = lambda repo: "unknown"

    def tearDown(self):
        CK.ex.repo_visibility = self._vis
        shutil.rmtree(self.tmp, ignore_errors=True)

    def m(self, view, name):
        return next(x for x in view["metrics"] if x["metric"] == name)

    def write_prs(self, *rows):
        fetch = {"schema": "relay-kit/prs@2", "kind": "fetch", "ts": "2026-12-01T00:00:00Z", "repos": ["o/r"], "ok": ["o/r"], "failed": {}}
        (self.root / "governance" / "pr-state.jsonl").write_text("".join(json.dumps(r) + "\n" for r in list(rows) + [fetch]), encoding="utf-8")

    def test_unknown(self):
        """telemetry: every metric carries its definition, version and label; with no records each says unknown, never zero"""
        v = TEL.derive(self.root)
        self.assertEqual(v["schema"], "relay-kit/telemetry@2")
        for x in v["metrics"]:
            self.assertTrue(x["definition"] and x["label"] and x["version"].startswith(x["metric"] + "@"), x["metric"])
        self.assertEqual(self.m(v, "delivery_throughput")["unknown"], "no governance/pr-state.jsonl: run relay-kit prs")
        self.assertIsNone(self.m(v, "lead_time")["hours"]["median"])
        self.assertIsNone(self.m(v, "human_intervention")["minutes_recorded"])
        self.assertEqual(self.m(v, "compute_consumption")["label"], "unknown")

    def test_prs(self):
        """telemetry: lead time and review burden are distributions from pr-state; abandoned PRs are counted apart"""
        self.write_prs(pr(1, "2026-11-02T00:00:00Z", "2026-11-02T02:00:00Z", reviews=[{"verdict": "CHANGES_REQUESTED"}, {"verdict": "APPROVED"}]),
                       pr(2, "2026-11-03T00:00:00Z", "2026-11-03T10:00:00Z", reviews=[{"verdict": "COMMENTED", "stated": "REQUEST CHANGES"}]),
                       pr(3, "2026-11-04T00:00:00Z", "2026-11-04T01:00:00Z"),
                       pr(4, "2026-11-04T00:00:00Z", state="closed"))
        v = TEL.derive(self.root)
        self.assertEqual(self.m(v, "lead_time")["hours"], {"n": 3, "median": 2.0, "p90": 10.0, "min": 1.0, "max": 10.0})
        self.assertEqual(self.m(v, "review_burden")["rounds"]["n"], 3)
        self.assertEqual(self.m(v, "review_burden")["rounds"]["max"], 1)
        d = self.m(v, "delivery_throughput")
        self.assertEqual((d["merged"], d["abandoned"], d["merged_per_week"]), (3, 1, {"2026-W45": {"o/r": 3}}))

    def test_remediation(self):
        """telemetry: remediation is counted only over cohorts old enough; only a fix or revert wording right before this repository's reference counts"""
        self.write_prs(pr(1, "2026-11-01T00:00:00Z", "2026-11-01T01:00:00Z"),
                       pr(2, "2026-11-01T00:00:00Z", "2026-11-01T02:00:00Z"),
                       pr(3, "2026-11-05T00:00:00Z", "2026-11-05T01:00:00Z", title="Fix regression from #1"),
                       pr(4, "2026-11-28T00:00:00Z", "2026-11-28T01:00:00Z"),
                       # decoys: a fix word and a reference that aren't linked, another repository's #2, #20 for #2
                       pr(5, "2026-11-05T00:00:00Z", "2026-11-05T02:00:00Z", title="fixes typo", sections={"why": "Stacked on #2"}),
                       pr(6, "2026-11-05T00:00:00Z", "2026-11-05T03:00:00Z", title="other/lib#2 fixed upstream; fixes #20"),
                       pr(7, "2026-11-05T00:00:00Z", "2026-11-05T04:00:00Z", title="Same authority as #2"))
        v = TEL.derive(self.root)          # as of 2026-12-01: PRs 1-3 are 14 days old, PR 4 isn't
        w = self.m(v, "post_merge_remediation")["windows"]
        self.assertEqual((w["14d"]["cohort"], w["14d"]["remediated"]), (6, 1))     # only #1, by #3
        self.assertEqual((w["30d"]["cohort"], w["30d"]["share"]), (0, None))
        self.assertEqual(self.m(v, "surviving_changes")["count"], 5)
        x = {"number": 2, "repo": "o/r"}
        for text, linked in (("Reverts o/r#2", True), ("revert #2", True), ("hotfix for #2", True), ("fixes o/r#2", True),
                             ("fixes other/lib#2", False), ("fix #20", False), ("#2 then a fix", False)):
            self.assertEqual(TEL.remediation_link({"title": text}, x), linked, text)

    def test_compute_and_coverage(self):
        """telemetry: corrections and child runs are applied before compute is summed; a malformed line is counted invalid, as the cockpit counts it, and breaks nothing"""
        rows = [{"id": "kid", "ts": "2026-11-03T09:10:00Z", "actor": "claude:review#SESSR9", "window": "session", "tokens_in": 30, "tokens_out": 0,
                 "method": "measured"},
                {"id": "a", "ts": "2026-11-03T09:00:00Z", "actor": "claude:research#SESSA1", "window": "session", "tokens_in": 100, "tokens_out": 0},
                {"id": "b", "ts": "2026-11-03T09:00:00Z", "actor": "claude:research#SESSA1", "window": "session", "tokens_in": 100, "tokens_out": 0,
                 "method": "estimate", "correction_of": "a"},
                {"id": "c", "ts": "2026-11-03T10:00:00Z", "actor": "claude:review#SESSR9", "window": "session", "tokens_in": 40, "tokens_out": 2,
                 "method": "measured", "tokens_detail": {"input": 4, "output": 2, "cache_write": 36, "cache_read": 900},
                 "includes_children": {"included": True, "ids": ["kid"]}},
                {"id": "bad", "ts": "2026-11-03T10:00:00Z", "actor": "sam", "window": "session", "tokens_in": "12k", "tokens_out": 0}]
        (self.root / "governance" / "budget.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        v = TEL.derive(self.root)
        c = self.m(v, "compute_consumption")
        self.assertEqual((c["tokens_by_method"], c["label"], c["corrections_applied"]), ({"estimate": 100, "measured": 42}, "estimate+measured", 1))
        self.assertEqual(c["categories_from_measured_lines"]["cache_read"], 900)
        self.assertEqual([x["line"] for x in c["invalid_lines"]], [5])                   # the same line the cockpit rejects
        data = CK.collect(self.root)                                                    # and the build doesn't fall over
        self.assertEqual([x["line"] for x in data["budget"]["invalid_lines"]], [5])
        cov = self.m(v, "measurement_coverage")
        self.assertIn("claude:research#SESSA1", cov["unmeasured_sessions"])
        self.assertNotIn("claude:review#SESSR9", cov["unmeasured_sessions"])

    def test_panel(self):
        """telemetry: the cockpit's Review panel shows each metric with its label and definition, no score or target, at the page's as of"""
        self.write_prs(pr(1, "2026-11-02T00:00:00Z", "2026-11-02T02:00:00Z"))
        out = self.tmp / "o"
        data = CK.build(self.root, out)
        page = (out / "index.html").read_text(encoding="utf-8")
        self.assertIn("TELEMETRY (relay-kit/telemetry@2, DERIVED", page)
        for x in data["telemetry"]["metrics"]:
            self.assertIn(f'>{x["metric"]}<', page)
        panel = page.split("TELEMETRY (relay-kit/telemetry@2")[1].split("</section>")[0]
        for word in ("score", "target"):
            self.assertEqual([l for l in panel.lower().split(".") if word in l and "no composite score and no targets" not in l], [])
        self.assertEqual(data["telemetry"]["as_of"].replace(":00Z", "Z")[:16], (data["as_of"] or "")[:16])    # one clock for page and panel

    def test_not_a_record(self):
        """telemetry: the view is derived and is never written into the records"""
        p = subprocess.run([sys.executable, "-I", str(KIT / "tools" / "telemetry.py"), "--root", str(self.root),
                            "--out", str(self.root / "governance" / "telemetry.json")], capture_output=True, text=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertFalse((self.root / "governance" / "telemetry.json").exists())
        p = subprocess.run([sys.executable, "-I", str(KIT / "tools" / "telemetry.py"), "--root", str(self.root),
                            "--out", str(self.tmp / "view.json")], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads((self.tmp / "view.json").read_text())["schema"], "relay-kit/telemetry@2")


    def test_metadata_and_lane(self):
        """telemetry: --content metadata reads no free text (claims and remediation are unknown); --lane keeps other lanes' handoffs and sessions out"""
        self.write_prs(pr(1, "2026-11-02T00:00:00Z", "2026-11-02T02:00:00Z"))
        v = TEL.derive(self.root, content="metadata")
        cl = self.m(v, "coordination_loss")
        self.assertIsNone(cl["overlapping_claims"])
        self.assertEqual(self.m(v, "post_merge_remediation")["windows"]["14d"]["unknown"], "--content metadata: pull-request text isn't read")
        everyone = self.m(TEL.derive(self.root), "measurement_coverage")["unmeasured_sessions"]
        research = self.m(TEL.derive(self.root, lane="research"), "measurement_coverage")["unmeasured_sessions"]
        self.assertTrue(any(s_.startswith("claude:review#") for s_ in everyone))
        self.assertFalse(any(s_.startswith("claude:review#") for s_ in research))

    def test_future(self):
        """telemetry: a time after the last pull-request read doesn't move as_of; it is flagged instead"""
        self.write_prs(pr(1, "2026-11-02T00:00:00Z", "2026-11-02T02:00:00Z"))
        (self.root / "governance" / "budget.jsonl").write_text(json.dumps(
            {"ts": "2027-06-01T00:00:00Z", "actor": "sam", "window": "session", "tokens_in": 1, "tokens_out": 0}) + "\n", encoding="utf-8")
        v = TEL.derive(self.root)
        self.assertTrue(v["as_of"] < "2027")
        self.assertTrue(any("2027-06-01" in n for n in v["notes"]))


    def test_latency(self):
        """telemetry: capture latency is measured only from a marker in a handoff's Task or Context and a completed step; markers elsewhere, invalid, future or reversed are excluded by name, other lanes left out, metadata unknown, never zero"""
        K = "idea_to_completed_handoff_latency"
        v = TEL.derive(self.root)
        lat = self.m(v, K)
        self.assertEqual(lat["label"], "unknown")
        self.assertEqual(lat["hours"]["n"], 0)
        self.assertEqual(lat["excluded"], {"completed without a capture marker": 1})     # HO_DONE completed, carries no marker
        self.assertEqual(lat["unknown"], "no completed handoff carries an 'Idea captured:' time")
        H = self.root / "handoffs"
        ctx = lambda text, line: text.replace("- none\n\n## Expected", f"- {line}\n\n## Expected", 1)      # into Context
        # a marker in Context on a completed handoff is a case; one in Task on an open handoff is in flight
        H.joinpath("HO-RS-20261101-AB12.md").write_text(ctx(TC.HO_DONE, "Idea captured: 2026-11-01T01:00Z (intake, synthetic)"), encoding="utf-8")
        H.joinpath("HO-RS-20261103-CD34.md").write_text(TC.HO_WAIT.replace("Synthetic task body BODYTASK.", "Synthetic task body BODYTASK.\nIdea captured: 2026-11-02T09:00Z", 1), encoding="utf-8")
        v = TEL.derive(self.root, as_of="2026-11-03T09:00Z")
        lat = self.m(v, K)
        self.assertEqual(lat["label"], "measured")
        self.assertEqual(lat["hours"], {"n": 1, "median": 12.0, "p90": 12.0, "min": 12.0, "max": 12.0})
        self.assertEqual(lat["cases"], [{"handoff": "HO-RS-20261101-AB12", "captured_at": "2026-11-01T01:00:00Z",
                                         "completed_at": "2026-11-01T13:00:00Z", "hours": 12.0}])
        self.assertEqual(lat["in_flight"], [{"handoff": "HO-RS-20261103-CD34", "captured_at": "2026-11-02T09:00:00Z", "open_hours": 24.0}])
        self.assertEqual(lat["excluded"], {})
        self.assertIsNone(lat["unknown"])
        # another lane's case is left out under --lane (the fixtures above span both lanes; this one is review's alone)
        only_review = TC.handoff("HO-RV-20261102-GH78", "completed", "claude:review", "sam", "Review-only", '- Quote: "ok" (sam, synthetic)',
                                 [("2026-11-02T00:00Z", "prepared", "claude:review#SESSR9", "drafted"),
                                  ("2026-11-02T06:00Z", "completed", "claude:review#SESSR9", "outputs/HO-RV-20261102-GH78.md")])
        H.joinpath("HO-RV-20261102-GH78.md").write_text(ctx(only_review, "Idea captured: 2026-11-02T00:00Z"), encoding="utf-8")
        H.joinpath("outputs", "HO-RV-20261102-GH78.md").write_text("synthetic\n", encoding="utf-8")
        by_lane = lambda lane: [c["handoff"] for c in self.m(TEL.derive(self.root, lane=lane, as_of="2026-11-03T09:00:00Z"), K)["cases"]]
        self.assertEqual(by_lane("research"), ["HO-RS-20261101-AB12"])
        self.assertEqual(sorted(by_lane("review")), ["HO-RS-20261101-AB12", "HO-RV-20261102-GH78"])   # AB12 is addressed to review
        self.assertEqual(sorted(by_lane(None)), ["HO-RS-20261101-AB12", "HO-RV-20261102-GH78"])
        H.joinpath("HO-RV-20261102-GH78.md").unlink(); H.joinpath("outputs", "HO-RV-20261102-GH78.md").unlink()
        # the same words outside Task/Context don't count: Expected output, Log note
        H.joinpath("HO-RS-20261101-AB12.md").write_text(TC.HO_DONE.replace("Write the result to", "Idea captured: 2026-11-01T01:00Z\nWrite the result to", 1)
                                                        .replace("| drafted\n", "| drafted; Idea captured: 2026-11-01T01:00Z\n", 1), encoding="utf-8")
        lat = self.m(TEL.derive(self.root, as_of="2026-11-03T09:00:00Z"), K)
        self.assertEqual(lat["cases"], [])
        self.assertEqual(lat["excluded"]["completed without a capture marker"], 1)
        # invalid (malformed, zone-less), future and reversed markers are excluded under their own names, with the id
        for line, reason in (("Idea captured: yesterday", "invalid capture time"),
                             ("Idea captured: 2026-11-01T01:00", "invalid capture time"),
                             ("Idea captured: 2026-11-09T00:00Z", "capture time in the future"),
                             ("Idea captured: 2026-11-01T14:00Z", "capture time after completion")):
            H.joinpath("HO-RS-20261101-AB12.md").write_text(ctx(TC.HO_DONE, line), encoding="utf-8")
            lat = self.m(TEL.derive(self.root, as_of="2026-11-03T09:00:00Z"), K)
            self.assertEqual(lat["cases"], [], line)
            self.assertEqual(lat["excluded"], {reason: ["HO-RS-20261101-AB12"]}, line)
            self.assertEqual(lat["label"], "unknown", line)
        # an open handoff with an invalid marker stays visible too, beside a valid case
        H.joinpath("HO-RS-20261101-AB12.md").write_text(ctx(TC.HO_DONE, "Idea captured: 2026-11-01T01:00Z"), encoding="utf-8")
        H.joinpath("HO-RS-20261103-CD34.md").write_text(TC.HO_WAIT.replace("Synthetic task body BODYTASK.", "Synthetic task body BODYTASK.\nIdea captured: 2026-11-02 09:00 CT", 1), encoding="utf-8")
        lat = self.m(TEL.derive(self.root, as_of="2026-11-03T09:00:00Z"), K)
        self.assertEqual(lat["label"], "measured")
        self.assertEqual(lat["excluded"], {"invalid capture time": ["HO-RS-20261103-CD34"]})
        self.assertEqual(lat["in_flight"], [])
        # metadata content uses no handoff text: unknown with that reason, never a zero
        lat = self.m(TEL.derive(self.root, content="metadata"), K)
        self.assertEqual(lat["label"], "unknown")
        self.assertEqual(lat["unknown"], "--content metadata: handoff text isn't used")
        self.assertEqual(lat["cases"], [])

if __name__ == "__main__":
    unittest.main()
