"""DEC-272: decision ids move from DEC-NNN to DEC-YYYYMMDD-XXXX. Standard library only.

    python3 -I -B -m unittest discover -s tests -p 'test_dec_hash_ids.py' -v
"""
import importlib.util, json, pathlib, re, subprocess, sys, tempfile, unittest

KIT = pathlib.Path(__file__).resolve().parent.parent
CHK = KIT / "tools" / "check_ledgers.py"
NEW_ID = KIT / "tools" / "new_id.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


CL = _load("relaykit_check_ledgers", CHK)


def dec(i, date="2026-11-23", status="adopted", **kw):
    rid = i if isinstance(i, str) else f"DEC-{i:03d}"
    r = {"id": rid, "date": date, "status": status, "decision": f"decision {rid}", "scope": "s",
         "decider": "sam", "basis": "\"yes\"", "locator": "thread"}
    r.update(kw)
    return r


def rows(*rs):
    return [(n, r) for n, r in enumerate(rs, 1)]


def legacy(k=3, date="2026-11-23"):
    return [dec(i, date) for i in range(1, k + 1)]


class DecisionIds(unittest.TestCase):
    def ids(self, *rs, policy=None):
        return CL.check_decision_ids(rows(*rs), policy)

    def test_legacy_order_still_enforced(self):
        """dec ids: sequential DEC-NNN lines are still checked strictly in order"""
        self.assertEqual(self.ids(*legacy(3)), [])
        out = self.ids(dec(1), dec(3))
        self.assertTrue(any("expected DEC-002" in x for x in out), out)

    def test_hash_ids_accepted_after_legacy(self):
        """dec ids: hash ids are accepted after the sequential lines, and dates may repeat or grow"""
        out = self.ids(*legacy(3), dec("DEC-20261123-K7QF"), dec("DEC-20261123-0A9Z"), dec("DEC-20261201-VWXY"))
        self.assertEqual(out, [])

    def test_duplicate_hash_rejected(self):
        """dec ids: a hash id used twice is a problem"""
        out = self.ids(*legacy(2), dec("DEC-20261123-K7QF"), dec("DEC-20261123-K7QF"))
        self.assertTrue(any("used by an earlier line" in x for x in out), out)

    def test_bad_alphabet_rejected(self):
        """dec ids: a suffix with I, L, O or U (or lower case) is a problem"""
        for bad in ("DEC-20261123-K7QI", "DEC-20261123-LOUX", "DEC-20261123-k7qf", "DEC-20261123-K7Q"):
            out = self.ids(*legacy(2), dec(bad))
            self.assertTrue(any("suffix must be 4 characters" in x for x in out), (bad, out))

    def test_bad_date_rejected(self):
        """dec ids: a hash id whose date part isn't a real date is a problem"""
        out = self.ids(*legacy(2), dec("DEC-20261131-K7QF"))
        self.assertTrue(any("isn't a real date" in x for x in out), out)

    def test_date_before_cutover_rejected(self):
        """dec ids: a hash id dated before the last sequential line is a problem"""
        out = self.ids(*legacy(2, "2026-11-23"), dec("DEC-20261122-K7QF", "2026-11-22"))
        self.assertTrue(any("before the last sequential decision (2026-11-23)" in x for x in out), out)

    def test_date_before_previous_hash_rejected(self):
        """dec ids: hash ids stay in append order: one dated before the previous hash id is a problem"""
        out = self.ids(*legacy(2), dec("DEC-20261125-K7QF"), dec("DEC-20261124-0A9Z"))
        self.assertTrue(any("before the previous hash id (2026-11-25)" in x for x in out), out)

    def test_sequential_after_hash_rejected(self):
        """dec ids: a new DEC-NNN after the first hash id is a problem, even in sequence"""
        out = self.ids(*legacy(3), dec("DEC-20261123-K7QF"), dec(4))
        self.assertTrue(any("sequential ids ended when hash ids began" in x for x in out), out)

    def test_sequential_past_policy_limit_rejected(self):
        """dec ids: with policy.json last_sequential_id, the next DEC-NNN is a problem before any hash id"""
        pol = {"last_sequential_id": "DEC-003"}
        self.assertEqual(self.ids(*legacy(3), policy=pol), [])
        out = self.ids(*legacy(4), policy=pol)
        self.assertTrue(any("sequential ids end at DEC-003" in x for x in out), out)

    def test_ledger_of_only_hash_ids(self):
        """dec ids: a new ledger may start with hash ids (no date floor without sequential lines)"""
        self.assertEqual(self.ids(dec("DEC-20200101-0000", "2020-01-01")), [])


class References(unittest.TestCase):
    def test_ref_re_takes_both(self):
        """dec refs: a question's 'about' may name either form"""
        for ok in ("DEC-007", "DEC-20261123-K7QF", "CH-001", "HO-RK-20261123-06B9"):
            self.assertTrue(CL.REF_RE.match(ok), ok)
        for bad in ("DEC-20261123-K7QI", "DEC-20261123-K7Q", "DEC-7"):
            self.assertFalse(CL.REF_RE.match(bad), bad)

    def test_lifecycle_refs_resolve_in_both_forms(self):
        """dec refs: supersedes, amends (text and {id}), revokes and related resolve across the two forms"""
        H1, H2, H3, H4 = "DEC-20261123-K7Q9", "DEC-20261123-0A9Z", "DEC-20261124-VWX1", "DEC-20261124-AB12"
        ds = legacy(3) + [
            dec(H1, supersedes="DEC-003"),                                   # hash closes sequential (and ends in a digit)
            dec(H2, amends={"id": "DEC-002", "clauses": ["1"]}),             # {"id": ...} form
            dec(H3, "2026-11-24", amends=H1, related=["DEC-001", H2]),       # hash -> hash
            dec(H4, "2026-11-24", revokes=[H2]),
        ]
        out, warn = CL.check_transitions(rows(*ds), [])
        self.assertEqual((out, warn), ([], []))
        res = CL.LC.resolve(ds, [])
        self.assertEqual(res["decisions"]["DEC-003"]["applicability"], "superseded")
        self.assertEqual([a["id"] for a in res["decisions"]["DEC-002"]["amended_by"]], [H2])
        self.assertEqual([a["id"] for a in res["decisions"][H1]["amended_by"]], [H3])
        self.assertEqual(res["decisions"][H2]["applicability"], "revoked")

    def test_forward_ref_still_caught(self):
        """dec refs: a sequential line can't point at a later hash id (forward), nor a hash id at itself"""
        H = "DEC-20261123-K7QF"
        ds = legacy(2) + [dec(H, supersedes=H)]
        out, _ = CL.check_transitions(rows(*ds), [])
        self.assertTrue(any("itself" in x for x in out), out)
        ds = [dec(1), dec(2, supersedes=H), dec(H)]
        out, _ = CL.check_transitions(rows(*ds), [])
        self.assertTrue(any("isn't earlier" in x for x in out), out)

    def test_reading_for_and_reading_required(self):
        """dec refs: reading_for names either form, and reading_required_from covers the hash ids after it"""
        H = "DEC-20261123-K7QF"
        ds = rows(*legacy(2), dec(H))
        out, ok = CL.check_reading_corrections(ds, rows({"reading_for": H, "date": "2026-11-23",
                                                         "reading": {"said": "x", "read_as": "y", "confirmed": "explicit"}}))
        self.assertEqual((out, ok), ([], {H}))
        out = CL.check_readings(ds, {"reading_required_from": "DEC-002"})
        self.assertTrue(any("DEC-002" in x for x in out) and any(H in x for x in out), out)
        self.assertFalse(any("DEC-001" in x for x in out), out)
        self.assertEqual(CL.check_readings(ds, {"reading_required_from": "DEC-002"}, {"DEC-002", H}), [])
        out = CL.check_readings(ds, {"reading_required_from": H})
        self.assertEqual([H in x for x in out], [True], out)


class HandoffReaders(unittest.TestCase):
    def test_fingerprints_and_withdrawn_for_hash_ids(self):
        """handoff.py: a hash decision cited under Authorized is read whole, fingerprinted, and flagged once revoked"""
        HO = _load("relaykit_handoff_hash", KIT / "skills" / "handoff-ledger" / "scripts" / "handoff.py")
        H, R = "DEC-20261123-K7Q9", "DEC-20261124-0A9Z"
        body = f"- Zach: yes. Recorded as {H}; also DEC-002. Not DEC-20261123-K7QI.\n"
        self.assertEqual(HO.cited_decisions(body), [H, "DEC-002"])
        with tempfile.TemporaryDirectory() as t:
            root = pathlib.Path(t); (root / "governance").mkdir(); d = root / "handoffs"; d.mkdir()
            led = root / "governance" / "decisions.jsonl"
            led.write_text("".join(json.dumps(r) + "\n" for r in legacy(2) + [dec(H)]), encoding="utf-8")
            text = f"---\nid: HO-RK-20261123-AAAA\nstatus: working\ncreated: 2026-11-23T20:00Z\n---\n## Authorized\n{body}\n## Proposals\n- none\n"
            filled = HO.add_fingerprints(text, d)
            self.assertRegex(filled, rf"- Fingerprint: {H} @ \S+ sha256:[0-9a-f]{{12}}")
            self.assertEqual([f[0] for f in HO.FP_RE.findall(filled)], [H, "DEC-002"])
            meta = {"status": "working", "created": "2026-11-23T20:00Z"}
            self.assertEqual(HO.fingerprint_warnings(filled, meta, d), [])
            with led.open("a", encoding="utf-8") as f:
                f.write(json.dumps(dec(R, "2026-11-24", revokes=H)) + "\n")
            HO._LEDGER_CACHE.clear()
            warn = HO.fingerprint_warnings(filled, meta, d)
            self.assertTrue(any(f"cites {H}, which is revoked by {R}" in w for w in warn), warn)
            self.assertFalse(any("DEC-20261123," in w or "DEC-20261123 " in w for w in warn), warn)

    def test_cockpit_links_hash_ids(self):
        """cockpit: ID_REF links a hash decision id whole, and not a malformed one"""
        CP = _load("relaykit_cockpit_hash", KIT / "tools" / "cockpit.py")
        self.assertEqual(CP.ID_REF.findall("see DEC-20261123-K7QF, DEC-214 and CH-003; not DEC-20261123-K7QI"),
                         ["DEC-20261123-K7QF", "DEC-214", "CH-003"])


class Cli(unittest.TestCase):
    def test_check_ledgers_cli(self):
        """dec ids: the command line reads last_sequential_id from policy.json and passes a mixed ledger"""
        with tempfile.TemporaryDirectory() as t:
            g = pathlib.Path(t)
            (g / "policy.json").write_text(json.dumps({"last_sequential_id": "DEC-002"}), encoding="utf-8")
            led = g / "decisions.jsonl"
            led.write_text("".join(json.dumps(r) + "\n" for r in legacy(2) + [dec("DEC-20261123-K7QF")]), encoding="utf-8")
            p = subprocess.run([sys.executable, "-I", "-B", str(CHK), "--dir", str(g)], capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            with led.open("a", encoding="utf-8") as f:
                f.write(json.dumps(dec(3)) + "\n")
            p = subprocess.run([sys.executable, "-I", "-B", str(CHK), "--dir", str(g)], capture_output=True, text=True)
            self.assertEqual(p.returncode, 1)
            self.assertIn("sequential ids ended when hash ids began", p.stdout)

    def test_ledger_append_mints_hash_ids_after_cutover(self):
        """ledger_append: after policy.json last_sequential_id (or a first hash id) a decision gets DEC-YYYYMMDD-XXXX; before it, the next number"""
        app = KIT / "tools" / "ledger_append.py"
        with tempfile.TemporaryDirectory() as t:
            root = pathlib.Path(t); gv = root / "governance"; gv.mkdir()
            g = lambda *a: subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
                                           "-c", "commit.gpgsign=false", *a], capture_output=True, text=True)
            (gv / "decisions.jsonl").write_text("".join(json.dumps(dict(r, recorded_by="sam")) + "\n" for r in legacy(2)), encoding="utf-8")
            g("init", "-q"); g("add", "-A"); g("commit", "-qm", "init")
            env = dict(__import__("os").environ, RELAYKIT_ACTOR="claude:rk#t1")
            app_run = lambda: subprocess.run([sys.executable, "-I", "-B", str(app), "--offline", "--dir", str(gv), "decision",
                                              json.dumps({"status": "proposed", "decision": "d", "scope": "s"})],
                                             capture_output=True, text=True, env=env)
            p = app_run()
            self.assertEqual((p.returncode, p.stdout.strip()), (0, "DEC-003"), p.stderr)
            (gv / "policy.json").write_text(json.dumps({"last_sequential_id": "DEC-003"}), encoding="utf-8")
            g("add", "-A"); g("commit", "-qm", "d3")
            p = app_run()
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertTrue(CL.DEC_HASH_RE.match(p.stdout.strip()), p.stdout)
            (gv / "policy.json").unlink()   # a hash id already in the ledger is enough on its own
            g("add", "-A"); g("commit", "-qm", "h1")
            q = app_run()
            self.assertEqual(q.returncode, 0, q.stdout + q.stderr)
            self.assertTrue(CL.DEC_HASH_RE.match(q.stdout.strip()) and q.stdout != p.stdout, q.stdout)

    def test_new_id_decision(self):
        """new_id --decision prints DEC-<UTC date>-XXXX that the check accepts, avoiding ids already in the ledger"""
        with tempfile.TemporaryDirectory() as t:
            p = subprocess.run([sys.executable, "-I", "-B", str(NEW_ID), "--decision"], capture_output=True, text=True, cwd=t)
            hid = p.stdout.strip()
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertTrue(CL.DEC_HASH_RE.match(hid), hid)
            self.assertIsNotNone(CL._hash_date(hid[4:12]))
            p = subprocess.run([sys.executable, "-I", "-B", str(NEW_ID), "--decision", "--lane", "RK"], capture_output=True, text=True, cwd=t)
            self.assertNotEqual(p.returncode, 0)


if __name__ == "__main__":
    unittest.main()
