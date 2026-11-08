"""Deterministic fixtures for merge-preflight Git and static-edge evidence."""
import importlib.util
import copy
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "tools" / "merge_preflight.py"
spec = importlib.util.spec_from_file_location("merge_preflight", TOOL)
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


class GitMergeFixtures(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture")
        self.write("shared.py", "value = 1\n")
        self.write("a.py", "one = 1\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, text=True,
                              capture_output=True, check=True)

    def write(self, name, content):
        (self.repo / name).write_text(content, encoding="utf-8")

    def commit_branch(self, name, changes):
        self.git("checkout", "-qb", name, "main")
        for path, contents in changes.items():
            self.write(path, contents)
        self.git("add", ".")
        self.git("commit", "-qm", name)

    def call(self, *args):
        return subprocess.run([sys.executable, str(TOOL), "--repo", str(self.repo),
                               "--base", "main", *args, "--json"],
                              text=True, capture_output=True)

    def test_clean_independent_changes(self):
        self.commit_branch("candidate", {"a.py": "one = 2\n"})
        self.commit_branch("peer", {"b.py": "two = 2\n"})
        result = self.call("--head", "candidate", "--peer", "peer=peer")
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        pairs = json.loads(result.stdout)["pairs"]
        self.assertEqual(["clean", "clean"], [x["merge"] for x in pairs])
        self.assertEqual(0, pairs[1]["shared_paths_count"])
        self.assertEqual("not_available", pairs[1]["edges"]["coverage"])

    def test_clean_overlapping_file(self):
        self.git("checkout", "-q", "main")
        self.write("shared.py", "value = 1\nkeep1\nkeep2\nkeep3\nkeep4\nother = 1\n")
        self.git("add", ".")
        self.git("commit", "-qm", "two-sections")
        self.commit_branch("candidate", {"shared.py": "value = 2\nkeep1\nkeep2\nkeep3\nkeep4\nother = 1\n"})
        self.commit_branch("peer", {"shared.py": "value = 1\nkeep1\nkeep2\nkeep3\nkeep4\nother = 2\n"})
        result = self.call("--head", "candidate", "--peer", "peer=peer")
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        peer = json.loads(result.stdout)["pairs"][1]
        self.assertEqual("clean", peer["merge"])
        self.assertEqual(1, peer["shared_paths_count"])

    def test_detects_conflicting_peer_and_preserves_worktree(self):
        self.commit_branch("candidate", {"shared.py": "value = 2\n"})
        self.commit_branch("peer", {"shared.py": "value = 3\n"})
        self.write("scratch.txt", "untouched\n")
        before = self.git("status", "--porcelain").stdout
        result = self.call("--head", "candidate", "--peer", "peer=peer")
        self.assertEqual(2, result.returncode, result.stderr + result.stdout)
        pairs = json.loads(result.stdout)["pairs"]
        self.assertEqual("clean", pairs[0]["merge"])
        self.assertEqual("conflict", pairs[1]["merge"])
        self.assertEqual(["shared.py"], pairs[1]["shared_paths"])
        self.assertEqual(before, self.git("status", "--porcelain").stdout)
        self.assertEqual("peer", self.git("branch", "--show-current").stdout.strip())

    def test_target_only_and_dirty_tree(self):
        self.commit_branch("candidate", {"shared.py": "value = 2\n"})
        self.write("scratch.txt", "do not touch\n")
        before = self.git("status", "--porcelain").stdout
        result = self.call("--head", "candidate")
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertEqual(before, self.git("status", "--porcelain").stdout)

    def test_unknown_peer_fails_closed(self):
        result = self.call("--peer", "missing=not_a_real_ref")
        self.assertEqual(1, result.returncode)
        self.assertIn("unavailable", result.stderr)

    def test_target_overlap_after_target_advances(self):
        self.commit_branch("candidate", {"shared.py": "value = 2\n"})
        self.git("checkout", "-q", "main")
        self.write("shared.py", "value = 3\n")
        self.git("add", ".")
        self.git("commit", "-qm", "advance target")
        result = self.call("--head", "candidate")
        self.assertEqual(2, result.returncode, result.stderr + result.stdout)
        target = json.loads(result.stdout)["pairs"][0]
        self.assertEqual(["shared.py"], target["shared_paths"])
        self.assertEqual(1, target["shared_paths_count"])

    def test_external_merge_driver_is_refused_without_execution(self):
        self.write(".gitattributes", "shared.py merge=fixture\n")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture attribute")
        self.commit_branch("candidate", {"shared.py": "value = 2\n"})
        self.commit_branch("peer", {"shared.py": "value = 3\n"})
        marker = self.repo / "driver-canary"
        command = "printf executed > " + shlex.quote(str(marker))
        self.git("config", "merge.fixture.driver", command)
        result = self.call("--head", "candidate", "--peer", "peer=peer")
        self.assertFalse(marker.exists(), "configured merge driver executed")
        self.assertEqual(1, result.returncode, result.stderr + result.stdout)
        self.assertIn("external merge driver", result.stderr + result.stdout)

    def test_checkout_and_pythonpath_modules_are_not_executed(self):
        for source in ("checkout", "pythonpath"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                marker = self.repo / (source + "-canary")
                location = self.repo if source == "checkout" else Path(directory)
                module = location / "aec.py"
                module.write_text("from pathlib import Path\nPath(" + repr(str(marker))
                                  + ").touch()\nprint('{}')\n", encoding="utf-8")
                env = dict(os.environ, PYTHONPATH=str(location))
                result = subprocess.run([sys.executable, "-I", "-B", str(TOOL),
                                         "--repo", str(self.repo), "--base", "main",
                                         "--peer", "peer=main", "--run-edges", "--json"],
                                        env=env, text=True, capture_output=True)
                module.unlink()
                self.assertFalse(marker.exists(), source + " aec module executed")
                self.assertNotIn("Traceback", result.stderr)

    def test_checkout_package_and_sitecustomize_are_not_executed(self):
        package = self.repo / "aec"
        package.mkdir()
        marker = self.repo / "package-canary"
        payload = "from pathlib import Path\nPath(" + repr(str(marker)) + ").touch()\n"
        (package / "__init__.py").write_text(payload, encoding="utf-8")
        (package / "__main__.py").write_text("print('{}')\n", encoding="utf-8")
        (self.repo / "sitecustomize.py").write_text(payload, encoding="utf-8")
        result = self.call("--run-edges", "--peer", "peer=main")
        self.assertFalse(marker.exists(), "checkout package or startup hook executed")
        self.assertNotIn("Traceback", result.stderr)

    def test_included_merge_driver_configuration_is_refused(self):
        include = self.repo / "driver-config"
        include.write_text("[merge \"fixture\"]\n\tdriver = false\n", encoding="utf-8")
        self.git("config", "include.path", str(include))
        result = self.call()
        self.assertEqual(1, result.returncode, result.stderr + result.stdout)
        self.assertIn("external merge driver", result.stdout + result.stderr)

    def test_renormalization_filters_are_refused_without_execution(self):
        self.write(".gitattributes", "shared.py filter=fixture\n")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture attribute")
        self.commit_branch("candidate", {"shared.py": "value = 2\n"})
        self.commit_branch("peer", {"shared.py": "value = 3\n"})
        marker = self.repo / "filter-canary"
        self.git("config", "filter.fixture.clean", "printf executed > " + shlex.quote(str(marker)) + "; cat")
        self.git("config", "merge.renormalize", "true")
        result = self.call("--head", "candidate", "--peer", "peer=peer")
        self.assertFalse(marker.exists(), "normalization filter executed")
        self.assertEqual(1, result.returncode, result.stderr + result.stdout)
        self.assertIn("merge.renormalize", result.stdout + result.stderr)

    def test_simulation_does_not_inherit_config_changed_after_check(self):
        self.write(".gitattributes", "shared.py merge=fixture\n")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture attribute")
        self.commit_branch("candidate", {"shared.py": "value = 2\n"})
        self.commit_branch("peer", {"shared.py": "value = 3\n"})
        marker = self.repo / "late-driver-canary"
        actual_git = preflight.git
        def change_after_check(repo, *args, **kwargs):
            result = actual_git(repo, *args, **kwargs)
            if args == ("config", "--get-regexp", r"^merge\..*\.driver$"):
                self.git("config", "merge.fixture.driver", "printf executed > " + shlex.quote(str(marker)))
            return result
        with mock.patch.object(preflight, "git", side_effect=change_after_check):
            left = self.git("rev-parse", "candidate").stdout.strip()
            right = self.git("rev-parse", "peer").stdout.strip()
            state, detail = preflight.merge_sim(self.repo, left, right)
        self.assertFalse(marker.exists())
        self.assertEqual("conflict", state, detail)

    def test_git_environment_cannot_redirect_the_inspected_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, GIT_DIR=str(Path(directory) / "missing"))
            with mock.patch.dict(os.environ, env, clear=True):
                rows = preflight.inspect(self.repo, "main", "HEAD", {}, False)
            self.assertEqual("clean", rows[0]["merge"])

    def test_simulation_leaves_source_objects_and_config_unchanged(self):
        self.commit_branch("candidate", {"shared.py": "value = 2\n"})
        self.commit_branch("peer", {"a.py": "one = 2\n"})
        def contents():
            return {str(path.relative_to(self.repo)): path.read_bytes()
                    for path in (self.repo / ".git").rglob("*") if path.is_file()}
        before = contents()
        result = self.call("--head", "candidate", "--peer", "peer=peer")
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertEqual(before, contents())

    def attribute_fixture(self, strategy, candidate_strategy=None, competing=False):
        self.write(".gitattributes", "shared.py merge=" + strategy + "\n")
        base = "first = 1\nkeep1\nkeep2\nkeep3\nkeep4\nlast = 1\n"
        self.write("shared.py", base)
        self.git("add", ".")
        self.git("commit", "-qm", "committed attributes")
        candidate = {"shared.py": base.replace("first = 1", "first = 2")}
        if candidate_strategy:
            candidate[".gitattributes"] = "shared.py merge=" + candidate_strategy + "\n"
        self.commit_branch("candidate", candidate)
        peer = base.replace("first = 1", "first = 3") if competing else base.replace("last = 1", "last = 3")
        self.commit_branch("peer", {"shared.py": peer})
        left = self.git("rev-parse", "peer").stdout.strip()
        right = self.git("rev-parse", "candidate").stdout.strip()
        direct = subprocess.run(["git", "merge-tree", "--write-tree", "--name-only", left, right],
                                cwd=self.repo, text=True, capture_output=True)
        state, detail = preflight.merge_sim(self.repo, left, right)
        return direct, state, detail

    def test_binary_attributes_are_preserved_in_isolated_simulation(self):
        direct, state, detail = self.attribute_fixture("binary")
        self.assertEqual(1, direct.returncode, direct.stderr)
        self.assertEqual("conflict", state, detail)

    def test_union_attributes_are_preserved_in_isolated_simulation(self):
        direct, state, detail = self.attribute_fixture("union", competing=True)
        self.assertEqual(0, direct.returncode, direct.stderr)
        self.assertEqual("clean", state, detail)

    def test_differing_attributes_use_the_target_or_peer_commit(self):
        direct, state, detail = self.attribute_fixture("binary", candidate_strategy="union")
        self.assertEqual(1, direct.returncode, direct.stderr)
        self.assertEqual("conflict", state, detail)
        result = self.call("--head", "candidate", "--peer", "peer=peer")
        self.assertEqual(2, result.returncode, result.stderr + result.stdout)
        self.assertEqual("conflict", json.loads(result.stdout)["pairs"][1]["merge"])

    def test_git_without_explicit_attribute_source_fails_closed(self):
        actual_git = preflight.git
        def unsupported_attribute_source(repo, *args, **kwargs):
            if any(arg.startswith("--attr-source=") for arg in args):
                return subprocess.CompletedProcess([], 129, "", "unknown option: --attr-source")
            return actual_git(repo, *args, **kwargs)
        with mock.patch.object(preflight, "git", side_effect=unsupported_attribute_source):
            head = self.git("rev-parse", "HEAD").stdout.strip()
            state, detail = preflight.merge_sim(self.repo, head, head)
        self.assertEqual("error", state, detail)
        self.assertIn("--attr-source", detail)

    def test_plain_text_escapes_terminal_controls_without_changing_json(self):
        filename = "fixture\x1b[31m\nforged\trow.py"
        self.write(filename, "value = 1\n")
        self.git("add", ".")
        self.git("commit", "-qm", "control-character filename")
        self.commit_branch("candidate", {filename: "value = 2\n"})
        self.commit_branch("peer", {filename: "value = 3\n"})
        name = "peer\x1b[31m\u202eforged"
        args = [sys.executable, "-I", "-B", str(TOOL), "--repo", str(self.repo),
                "--base", "main", "--head", "candidate", "--peer", name + "=peer"]
        text = subprocess.run(args, text=True, capture_output=True)
        self.assertEqual(2, text.returncode, repr(text.stderr))
        self.assertNotIn("\x1b", text.stdout + text.stderr)
        self.assertNotIn("\u202e", text.stdout + text.stderr)
        self.assertIn(r"\x1b", text.stdout)
        self.assertIn(r"\nforged\trow.py", text.stdout)
        machine = subprocess.run([*args, "--json"], text=True, capture_output=True)
        pair = json.loads(machine.stdout)["pairs"][1]
        self.assertEqual(name, pair["pair"])
        self.assertEqual([filename], pair["shared_paths"])

    def test_legacy_grafts_cannot_hide_shared_paths(self):
        self.commit_branch("candidate", {"shared.py": "value = 2\n"})
        self.commit_branch("peer", {"shared.py": "value = 3\n"})
        candidate = self.git("rev-parse", "candidate").stdout.strip()
        peer = self.git("rev-parse", "peer").stdout.strip()
        (self.repo / ".git" / "info" / "grafts").write_text(candidate + " " + peer + "\n", encoding="utf-8")
        result = self.call("--head", "candidate", "--peer", "peer=peer")
        self.assertEqual(2, result.returncode, result.stderr + result.stdout)
        pair = json.loads(result.stdout)["pairs"][1]
        self.assertEqual("conflict", pair["merge"])
        self.assertEqual(["shared.py"], pair["shared_paths"])

    def test_emits_fathom_observation_csv(self):
        self.commit_branch("candidate", {"shared.py": "value = 2\n"})
        self.commit_branch("peer", {"shared.py": "value = 3\n"})
        output = self.repo / "risk.csv"
        result = self.call("--head", "candidate", "--peer", "peer=peer", "--csv", str(output))
        self.assertEqual(2, result.returncode, result.stderr + result.stdout)
        table = output.read_text()
        self.assertIn("shared_cross_module_nodes", table)
        self.assertIn("not_available", table)


class EdgeEvidence(unittest.TestCase):
    def fixture(self, pairs, base="a" * 40, method="1" * 64):
        return {"base_revision": base, "head_method_sha": method,
                "base_method_sha": method, "method_comparison": False,
                "requested_base_revision": "a" * 40, "head_revision": "b" * 40,
                "cross_module_changes": {
                    "added": [{"src": a, "dst": b} for a, b in pairs], "removed": []}}

    def test_shared_nodes_requires_matching_methods_and_bases(self):
        a = self.fixture([("pkg.a", "pkg.b"), ("pkg.a", "pkg.c")])
        b = self.fixture([("pkg.a", "pkg.z")])
        result = preflight.edge_signal(a, b)
        self.assertEqual("comparable_static", result["coverage"])
        self.assertEqual(1, result["shared_cross_module_nodes"])
        self.assertEqual("incompatible_bases",
                         preflight.edge_signal(a, self.fixture([], base="d" * 40))["coverage"])
        self.assertEqual("incompatible_extraction_methods",
                         preflight.edge_signal(a, self.fixture([], method="2" * 64))["coverage"])

    def test_malformed_reports_never_become_measured_zero(self):
        valid = self.fixture([])
        malformed = [[], "unexpected", 42, {}, {"base_revision": "abc"}]
        for key in ("base_revision", "requested_base_revision", "head_revision",
                    "head_method_sha", "base_method_sha", "method_comparison", "cross_module_changes"):
            missing = copy.deepcopy(valid)
            del missing[key]
            malformed.append(missing)
        for changes in ({}, {"added": [], "removed": None},
                        {"added": "bad", "removed": []},
                        {"added": [None], "removed": []},
                        {"added": [{"src": "pkg.a"}], "removed": []},
                        {"added": [{"src": [], "dst": "pkg.b"}], "removed": []}):
            malformed.append(dict(valid, cross_module_changes=changes))
        malformed.extend([dict(valid, head_method_sha=None),
                          dict(valid, method_comparison="false"),
                          dict(valid, base_revision=""),
                          dict(valid, head_revision="not-a-commit"),
                          dict(valid, head_method_sha="unknown")])
        for report in malformed:
            with self.subTest(report=report):
                result = preflight.edge_signal(report, report)
                self.assertEqual("invalid_report", result["coverage"])
                self.assertIsNone(result["shared_cross_module_nodes"])
                self.assertEqual([], result["examples"])

    def test_report_is_bound_to_requested_revisions(self):
        report = self.fixture([])
        for key in ("base_revision", "requested_base_revision", "head_revision"):
            with self.subTest(key=key):
                wrong = dict(report, **{key: "e" * 40})
                completed = subprocess.CompletedProcess([], 0, json.dumps(wrong), "")
                with mock.patch.object(preflight, "run", return_value=completed), \
                     mock.patch.object(preflight, "merge_base", return_value="a" * 40):
                    with self.assertRaisesRegex(ValueError, "revisions"):
                        preflight.edge_diff(Path("."), "a" * 40, "b" * 40)

    def test_json_primitives_are_rejected_by_edge_reader(self):
        for value in ([], "unexpected", 42, None, {}):
            with self.subTest(value=value):
                completed = subprocess.CompletedProcess([], 0, json.dumps(value), "")
                with mock.patch.object(preflight, "run", return_value=completed):
                    with self.assertRaises(ValueError):
                        preflight.edge_diff(Path("."), "a" * 40, "b" * 40)

    def test_malformed_json_is_rejected_without_a_traceback(self):
        report = self.fixture([])
        duplicate = json.dumps(report)[:-1] + ', "cross_module_changes": {"added": [], "removed": []}}'
        nonfinite = json.dumps(dict(report, unexpected="NONFINITE")).replace('"NONFINITE"', 'NaN')
        for text in ("[" * 2000 + "]" * 2000, duplicate, nonfinite, "{bad json"):
            with self.subTest(text=text[:80]):
                completed = subprocess.CompletedProcess([], 0, text, "")
                with mock.patch.object(preflight, "run", return_value=completed):
                    with self.assertRaises(ValueError):
                        preflight.edge_diff(Path("."), "a" * 40, "b" * 40)

    def test_peer_failure_does_not_contaminate_later_peer(self):
        report = self.fixture([])
        with mock.patch.object(preflight, "resolve", return_value="b" * 40), \
             mock.patch.object(preflight, "merge_base", return_value="a" * 40), \
             mock.patch.object(preflight, "merge_sim", return_value=("clean", "")), \
             mock.patch.object(preflight, "files_changed", return_value=set()), \
             mock.patch.object(preflight, "edge_diff", side_effect=[report, ValueError("invalid peer"), report]):
            rows = preflight.inspect(Path("."), "main", "candidate", {"bad": "bad", "good": "good"}, True)
        self.assertEqual("invalid peer", rows[1]["edge_error"])
        self.assertIsNone(rows[1]["edges"]["shared_cross_module_nodes"])
        self.assertIsNone(rows[2]["edge_error"])
        self.assertEqual(0, rows[2]["edges"]["shared_cross_module_nodes"])

    def test_valid_empty_report_is_measured_zero(self):
        report = self.fixture([])
        result = preflight.edge_signal(report, report)
        self.assertEqual("comparable_static", result["coverage"])
        self.assertEqual(0, result["shared_cross_module_nodes"])

    def test_within_report_method_mismatch_is_not_comparable(self):
        report = self.fixture([])
        report["base_method_sha"] = "2" * 64
        self.assertEqual("incompatible_extraction_methods", preflight.edge_signal(report, report)["coverage"])

    def test_missing_is_not_zero(self):
        self.assertIsNone(
            preflight.edge_signal(None, self.fixture([]))["shared_cross_module_nodes"])


if __name__ == "__main__":
    unittest.main()
