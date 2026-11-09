#!/usr/bin/env python3
"""PR collision preflight: Git merge simulation plus optional Telosmith static edges.

    python3 tools/merge_preflight.py --base origin/main --head HEAD \
      --peer PR-12=origin/pr-12 --run-edges --csv risks.csv --json

Exit: 0 clean, 2 conflicts found, 1 unable to simulate. Does not merge,
check out, advance refs or modify working-tree files.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def run(repo: Path, *args: str, isolated_config: bool = False) -> subprocess.CompletedProcess[str]:
    # Do not let inherited Git routing/configuration variables redirect this read
    # or cause a missing object to launch a credential helper or remote transport.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1", GIT_ALLOW_PROTOCOL="",
               GIT_GRAFT_FILE=os.devnull,
               GIT_TERMINAL_PROMPT="0")
    if isolated_config:
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_ATTR_NOSYSTEM="1", GIT_CONFIG_COUNT="1",
                   GIT_CONFIG_KEY_0="core.attributesFile", GIT_CONFIG_VALUE_0=os.devnull)
    return subprocess.run(list(args), cwd=repo, capture_output=True, text=True, encoding="utf-8",
                          errors="surrogateescape", env=env, check=False, timeout=90)


def git(repo: Path, *args: str, isolated_config: bool = False) -> subprocess.CompletedProcess[str]:
    return run(repo, "git", "--no-pager", "--no-replace-objects",
               "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false",
               *args, isolated_config=isolated_config)


def resolve(repo: Path, ref: str) -> str:
    p = git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
    if p.returncode:
        raise ValueError(f"ref {ref!r} unavailable: {p.stderr.strip()}")
    return p.stdout.strip()


def merge_base(repo: Path, left: str, right: str) -> str:
    p = git(repo, "merge-base", "--all", left, right)
    bases = p.stdout.split()
    if p.returncode or len(bases) != 1:
        raise ValueError("a unique merge base is required; history is missing or ambiguous")
    return bases[0]


def files_changed(repo: Path, base: str, head: str) -> set[str]:
    p = git(repo, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z",
            base, head, "--")
    if p.returncode:
        raise ValueError(f"could not diff {base[:12]}..{head[:12]}: {p.stderr.strip()}")
    return set(filter(None, p.stdout.split("\0")))


def validate_edge_report(report: object) -> dict:
    """Missing evidence must not become a valid empty change set."""
    if not isinstance(report, dict):
        raise ValueError("Telosmith edge report must be an object")
    for key in ("base_revision", "requested_base_revision", "head_revision",
                "base_method_sha", "head_method_sha"):
        value = report.get(key)
        sizes = (64,) if key.endswith("method_sha") else (40, 64)
        if (not isinstance(value, str) or len(value) not in sizes
                or re.fullmatch(r"[0-9a-f]+", value) is None):
            raise ValueError(f"Telosmith edge report has missing or invalid {key}")
    if type(report.get("method_comparison")) is not bool:
        raise ValueError("Telosmith edge report needs a boolean method_comparison")
    changes = report.get("cross_module_changes")
    if not isinstance(changes, dict):
        raise ValueError("Telosmith edge report needs cross_module_changes")
    for direction in ("added", "removed"):
        edges = changes.get(direction)
        if not isinstance(edges, list):
            raise ValueError(f"Telosmith edge report needs a {direction} edge list")
        for edge in edges:
            if (not isinstance(edge, dict) or any(
                    not isinstance(edge.get(key), str) or not edge[key].strip()
                    for key in ("src", "dst"))):
                raise ValueError("Telosmith edge report has an invalid edge endpoint")
    return report


def report_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Telosmith edge report has duplicate JSON fields")
        result[key] = value
    return result


def reject_constant(value: str) -> None:
    raise ValueError("Telosmith edge report contains non-finite JSON data")


def edge_diff(repo: Path, base: str, head: str) -> dict:
    # Only use the operator's installed Telosmith, never checkout-local modules,
    # PYTHONPATH entries, user-site packages or startup hooks from the checkout.
    p = run(repo, sys.executable, "-I", "-B", "-m", "aec", "diff", "--base", base,
            "--head", head, "--json")
    if p.returncode:
        raise ValueError(f"Telosmith edge diff unavailable: {(p.stderr or p.stdout).strip()[:300]}")
    try:
        report = validate_edge_report(json.loads(p.stdout, object_pairs_hook=report_object,
                                                parse_constant=reject_constant))
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Telosmith edge diff returned invalid or excessively nested JSON") from exc
    if (report["requested_base_revision"] != base or report["head_revision"] != head
            or report["base_revision"] != merge_base(repo, base, head)):
        raise ValueError("Telosmith edge report revisions do not match the requested comparison")
    return report


def cross_nodes(diff: dict) -> set[str]:
    changes = validate_edge_report(diff)["cross_module_changes"]
    return {edge[key] for direction in ("added", "removed")
            for edge in changes[direction] for key in ("src", "dst")}


def edge_signal(left: dict | None, right: dict | None) -> dict:
    def unavailable(reason: str) -> dict:
        return {"coverage": reason, "shared_cross_module_nodes": None, "examples": []}

    if left is None or right is None:
        return unavailable("not_available")
    try:
        validate_edge_report(left)
        validate_edge_report(right)
    except ValueError:
        return unavailable("invalid_report")
    if left["base_revision"] != right["base_revision"]:
        return unavailable("incompatible_bases")
    if (left["head_method_sha"] != right["head_method_sha"]
            or any(report["base_method_sha"] != report["head_method_sha"]
                   or report["method_comparison"] for report in (left, right))):
        return unavailable("incompatible_extraction_methods")
    overlap = sorted(cross_nodes(left) & cross_nodes(right))
    return {"coverage": "comparable_static", "shared_cross_module_nodes": len(overlap),
            "examples": overlap[:10]}


def merge_sim(repo: Path, left: str, right: str) -> tuple[str, str]:
    # Attributes can select executable merge drivers, including without --run-edges.
    # Refuse these configurations instead of substituting a falsely clean result.
    drivers = git(repo, "config", "--get-regexp", r"^merge\..*\.driver$")
    if drivers.returncode not in (0, 1):
        return "error", "could not inspect Git merge-driver configuration"
    if drivers.returncode == 0:
        return "error", "external merge driver configured; simulation refused without execution"
    renormalize = git(repo, "config", "--type=bool", "--get", "merge.renormalize")
    if renormalize.returncode not in (0, 1):
        return "error", "could not inspect Git merge normalization configuration"
    if renormalize.stdout.strip() == "true":
        return "error", "merge.renormalize may execute filters; simulation refused without execution"
    objects = git(repo, "rev-parse", "--path-format=absolute", "--git-path", "objects")
    algorithm = git(repo, "rev-parse", "--show-object-format")
    object_path = objects.stdout.rstrip("\n")
    if (objects.returncode or not Path(object_path).is_absolute()
            or "\n" in object_path or "\r" in object_path
            or algorithm.returncode or algorithm.stdout.strip() not in ("sha1", "sha256")):
        return "error", "could not locate the local Git object database"
    # Isolate execution-capable configuration even if the source config changes
    # after the checks above. Read existing objects only; generated trees go into
    # this temporary bare repository, not the inspected checkout's object store.
    with tempfile.TemporaryDirectory(prefix="merge-preflight-") as directory:
        sandbox = Path(directory)
        init = git(sandbox, "init", "--bare", "--quiet", "--template=",
                   "--object-format=" + algorithm.stdout.strip(), isolated_config=True)
        if init.returncode:
            return "error", "could not initialize an isolated Git simulation"
        (sandbox / "objects" / "info" / "alternates").write_text(object_path + "\n", encoding="utf-8")
        p = git(sandbox, "--attr-source=" + left, "merge-tree", "--write-tree",
                "--name-only", left, right,
                isolated_config=True)
    lines = p.stdout.splitlines()
    tree_written = bool(lines and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", lines[0]))
    if p.returncode == 0 and tree_written:
        return "clean", ""
    detail = (p.stdout + "\n" + p.stderr).strip()[:4000]
    return ("conflict" if p.returncode == 1 and tree_written else "error"), detail


def inspect(repo: Path, base_ref: str, head_ref: str,
            peers: dict[str, str], use_edges: bool) -> list[dict]:
    base, head = resolve(repo, base_ref), resolve(repo, head_ref)
    left_edges = None
    edge_error = None
    if use_edges:
        try:
            left_edges = edge_diff(repo, base, head)
        except (OSError, UnicodeError, ValueError, subprocess.TimeoutExpired) as exc:
            edge_error = str(exc)
    rows: list[dict] = []
    for name, ref in [("target", base_ref), *peers.items()]:
        pair_edge_error = edge_error
        other = base if name == "target" else resolve(repo, ref)
        state, diagnostic = merge_sim(repo, other, head)
        common = merge_base(repo, head, other)
        shared = sorted(files_changed(repo, common, head) & files_changed(repo, common, other))
        if name == "target":
            signal = {"coverage": "not_applicable", "shared_cross_module_nodes": None,
                      "examples": []}
        else:
            other_edges = None
            if use_edges and left_edges is not None:
                try:
                    other_edges = edge_diff(repo, base, other)
                except (OSError, UnicodeError, ValueError, subprocess.TimeoutExpired) as exc:
                    pair_edge_error = (edge_error + "; " if edge_error else "") + str(exc)
            signal = edge_signal(left_edges, other_edges)
        rows.append({"pair": name, "ref": ref, "head_sha": head, "other_sha": other,
                     "merge": state, "shared_paths_count": len(shared),
                     "shared_paths": shared[:30], "shared_paths_truncated": max(0, len(shared) - 30),
                     "edges": signal, "edge_error": pair_edge_error, "diagnostic": diagnostic})
    return rows


def terminal_text(value: str, limit: int = 4000) -> str:
    # Repository paths and diagnostics are data, not terminal control sequences.
    # Escape control/format characters (including bidi controls) and bound output.
    result = "".join(char if char.isprintable() else ascii(char)[1:-1] for char in value[:limit])
    return result + ("... [truncated]" if len(value) > limit else "")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--repo", type=Path, default=Path("."))
    p.add_argument("--base", default="origin/main", help="target branch/ref")
    p.add_argument("--head", default="HEAD", help="candidate branch/ref")
    p.add_argument("--peer", action="append", default=[], metavar="NAME=REF",
                   help="another open PR head")
    p.add_argument("--run-edges", action="store_true",
                   help="run installed Telosmith with isolated Python (-I -B -m aec diff)")
    p.add_argument("--csv", type=Path, help="observation table for Fathom")
    p.add_argument("--json", action="store_true", help="machine-readable stdout")
    a = p.parse_args(argv)
    repo = a.repo.resolve()
    peers = {}
    try:
        for item in a.peer:
            name, sep, ref = item.partition("=")
            if not sep or not name or not ref or name == "target" or name in peers:
                raise ValueError("--peer must be a unique NAME=REF (NAME cannot be target)")
            peers[name] = ref
        if git(repo, "rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
            raise ValueError("--repo must be a Git working tree")
        rows = inspect(repo, a.base, a.head, peers, a.run_edges)
    except (OSError, UnicodeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(terminal_text(f"preflight error: {exc}"), file=sys.stderr)
        return 1
    result = {"schema": "merge-preflight/probe@1", "base": a.base, "head": a.head,
              "pairs": rows, "warning": "Simulation uses isolated Git configuration and built-in merge "
              "drivers, not checkout/global merge preferences. A clean Git merge does not guarantee compatible "
              "behavior. Static edges are incomplete and advisory."}
    if a.csv:
        a.csv.parent.mkdir(parents=True, exist_ok=True)
        with a.csv.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=[
                "pair", "head_sha", "other_sha", "git_merge", "shared_paths",
                "edge_coverage", "shared_cross_module_nodes"])
            w.writeheader()
            for r in rows:
                w.writerow({"pair": r["pair"], "head_sha": r["head_sha"],
                            "other_sha": r["other_sha"], "git_merge": r["merge"],
                            "shared_paths": r["shared_paths_count"],
                            "edge_coverage": r["edges"]["coverage"],
                            "shared_cross_module_nodes":
                                r["edges"]["shared_cross_module_nodes"]})
    if a.json:
        print(json.dumps(result, sort_keys=True, indent=2))
    else:
        for r in rows:
            print(terminal_text(f"{r['pair']}: {r['merge']} | shared files={r['shared_paths_count']} | "
                  f"edge coverage={r['edges']['coverage']} | "
                  f"shared cross-module nodes={r['edges']['shared_cross_module_nodes']}"))
            for path in r["shared_paths"][:5]:
                print(terminal_text(f"    file: {path}"))
            if r["edge_error"]:
                print(terminal_text(f"    edge warning: {r['edge_error']}"))
            if r["diagnostic"]:
                print(terminal_text(f"    Git: {r['diagnostic'][:500]}"))
        print(result["warning"])
    if any(r["merge"] == "error" for r in rows):
        return 1
    return 2 if any(r["merge"] == "conflict" for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
