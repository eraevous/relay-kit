# Merge collision preflight — experimental

**Status:** Proposed read-only probe, not an adopted merge gate. The immediate purpose is to reduce time lost to competing pull requests, while testing whether Telosmith edges add anything beyond Git.

The standard-library script tools/merge_preflight.py produces two explicitly different signals:

1. **Actual merge conflicts:** Git's merge-tree --write-tree simulates merging a candidate branch into the locally fetched target, and against each supplied peer PR branch. A written merge tree with Git exit 1 = conflict; exit 0 = clean simulation. Each simulation uses a temporary bare repository with the source's local objects available read-only. It does not check out, advance refs, or add objects to the inspected repository. Configured external merge drivers and merge.renormalize are refused, including when --run-edges is absent. Shared paths are computed from both branches' unique common ancestor, including changes made on the target after the candidate branched.
2. **Structural review overlap (optional):** --run-edges invokes the installed Telosmith with python -I -B -m aec diff --base SHA --head SHA --json for both branches. With the same merge base and compatible extractor methods, it counts common endpoints in changed cross-module calls/imports. This is advisory, not evidence of a Git conflict, behavioral incompatibility or complete runtime reachability.

## Use

Fetch the target and candidate/peer PR heads before testing. This tool deliberately does not query GitHub or fetch refs; relay-kit prs remains the one GitHub PR reader.

    git fetch origin main
    git fetch origin pull/123/head:refs/remotes/origin/pr-123
    python3 tools/merge_preflight.py --base origin/main --head HEAD \
        --peer PR-123=refs/remotes/origin/pr-123 --csv /tmp/merge-risk.csv --json

For Python code with a compatible Telosmith installation:

    python3 tools/merge_preflight.py --base origin/main --head HEAD \
        --peer PR-123=refs/remotes/origin/pr-123 --run-edges

Exit codes: **0** clean simulations, **2** at least one true simulated conflict, **1** missing refs or failed simulation. A shared-file warning does not imply an actual conflict; two branches can edit different lines of one file and merge cleanly. An absent or malformed edge reading is unknown, not zero. Edge reports must include valid revision and method identities, an explicit comparison flag, both change lists, and nonempty string endpoints. The requested revisions and computed merge base must match the report. A valid empty change set is the only way to measure zero overlap.

## Execution boundary and limits

Run this tool from a trusted relay-kit installation or checkout, not by executing a PR's copy of the script. --repo identifies data to inspect, not Python code to load. Use an up-to-date, trusted Git executable and Python environment. --run-edges requires Telosmith installed in that interpreter's trusted system or virtual-environment site-packages; isolated Python deliberately ignores the inspected checkout, PYTHONPATH and the user site. Installed packages and their system-level startup hooks remain trusted executable code. If no compatible installation is available, the edge signal stays unavailable.

Git inherits neither repository-routing environment overrides nor permission to lazily fetch missing objects or prompt for credentials. Hooks, fsmonitor, external diff, text conversion, replacement refs and legacy grafts are disabled for these invocations, so graph and content reads use the same commit identities. Merge simulation uses isolated Git configuration and built-in drivers, with the target/peer commit as the left (ours) side and its committed .gitattributes selected explicitly via --attr-source. Git versions that cannot select that attribute source fail closed. It does not reproduce checkout/global preferences such as info/attributes, global attributes, custom rename settings or custom merge drivers. Driver configuration (including included files) or normalization that could execute a filter is a simulation error, not a clean result. Source configuration is not loaded by the temporary merge repository, even if it changes after inspection. Unknown, missing or multiple common ancestors are errors. Shallow/partial repositories may need objects fetched separately by the operator; this tool never fetches them.

Plain-text reports escape terminal control and formatting characters in untrusted fields, and bound each displayed line. JSON and CSV retain the underlying data for machine consumers, which must handle that data safely.

The peer checks are pairwise. They do not simulate every possible sequence of three or more PR merges. A clean Git merge never proves that the software will work.

## Fathom

--csv PATH emits one aggregate row per pair: commit identities, git_merge, shared_paths, edge_coverage, and shared_cross_module_nodes. Fathom can treat this as ordinary tabular data for profiling, missingness analysis and later trend comparisons. It does not receive private source text or infer a causal merge-risk score. A single run cannot support predictive claims. Any history collector should pin source revisions, extraction methods, timestamps, and sampling coverage.

## Follow-up experiments (not implemented)

- Consume the existing relay-kit/prs@2 record store and fetch only required peer heads through a separately reviewed network wrapper; no redundant GitHub polling.
- Run a read-only report when PRs are opened, when the base moves, and just before merging.
- Compare three approaches across actual historical collisions: file overlap alone; Git merge simulation; Git plus Telosmith edges, measuring true conflicts found, false alerts, review time saved and semantic regressions missed.
- Evaluate whether Fathom finds actionable patterns in the accumulated observation table, and whether using those analyses improves merge planning.
- Keep structural analysis optional for non-Python repos; any mandatory enforcement requires an explicit owner decision and additional review.

## Tests

    python3 -I -B -m unittest discover -s tests -p test_merge_preflight.py -v

Tests cover clean changes, clean same-file edits, real conflicts, target-branch divergence, preservation of dirty working trees, objects, configuration and refs, unavailable refs, CSV output, edge-report validation, revision/method/base mismatches, checkout/PYTHONPATH import canaries, and merge-driver/filter execution canaries, and committed binary/union attributes (including attributes that differ between branches). They use only synthetic repositories and fixture data, and are included in tests/run_tests.py. The code introduces no versioned relay-kit contract, new authority, automatic merge or new persistent record schema. As a check-related change, it needs independent outside review before merging, per CONTRIBUTING.md.
