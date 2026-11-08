# Enforcement audit: governing instructions as executable dependencies

**Status: draft for owner review.** Docs only. Nothing here changes a rule, a check, a hook or CI;
every fix is proposed, not applied. Asked for by the external review CH-035 §6 and DEC-120
(enforcement audit), taken under the DEC-136 idle policy.

Audited on 2026-11-06 against `origin/main` at `1fa18c3`. Sources read: `AGENTS.md`,
`CONTRIBUTING.md`, `governance/purpose.md`, `governance/contracts.json`, `ARCHITECTURE.md`,
`.aec/README.md`, `.github/workflows/tests.yml`, `.github/workflows/telosmith.yml`,
`.claude/settings.json`, `.github/pull_request_template.md`, `tests/run_tests.py` (governance
block, lines 380–430), `tools/prepublish_scan.py`, `tools/templates/records/`.

`governance/purpose.md` already labels each invariant **software** or **procedure** and names its
tests, and `tests/run_tests.py` checks that every invariant states its enforcement and that the
cited tests exist. This audit therefore re-verifies those labels and extends the same treatment
to the rules in AGENTS.md, CONTRIBUTING.md, ARCHITECTURE.md and `.aec/README.md`, which have no
such self-check.

## Labels

| Label | Meaning |
| --- | --- |
| `enforced` | a hook, CI job, test or script fails when the rule is broken; the path is named |
| `procedural` | relies on a person (or an agent) doing it; nothing fails if they don't |
| `advisory` | guidance or an epistemic stance; not the kind of thing a check can fail on |
| `planned` | names a mechanism that does not exist yet |
| `obsolete` | contradicted by the current records or code |

## What was run (read-only)

- `python3 -I -B tests/run_tests.py`: 226 passed, 0 failed.
- `node tests/browser_tests.cjs`: 37 passed, 0 failed.
- `python3 -I tools/prepublish_scan.py`: 75 tracked files, 0 findings; "no .private-names file: names not checked".
- `python3 -m aec edges . -o <scratch>` then `python3 -m aec check .aec/records.jsonl --edges <scratch>` (installed telosmith 0.5.0): 0 errors, 3 anchor-mismatch warnings (`P0@2` README anchor, `RK-R-export@1` two anchors).
- `python3 tools/status.py --root <owner's records> --no-fetch --no-ci`: ran; reported four `handoff@1` files made after `@2` was adopted (an "out of date" finding of the tool itself, not of this repo).
- In the Telosmith checkout: `git cat-file -t c5ed7af92584` (the commit `telosmith.yml` pins) exists and its `__main__.py` has `code-impact --exit-code`; `git tag` lists `v0.4.0`, `v0.4.1` only.

## Rules and their enforcement

| # | Rule (short quote) | Where stated | Enforcing mechanism | Label | Evidence |
| --- | --- | --- | --- | --- | --- |
| 1 | "A contract changes only by a new version, with the spec, every implementer and the tests in the same change" | AGENTS.md, ARCHITECTURE.md §2, CONTRIBUTING step 2, `contracts.json` `rule` | `tests/run_tests.py` case "governance: every contract's spec and implementers carry its version; its tests exist" (reads `governance/contracts.json`, checks each spec/implementer file contains the `id@version` string and each named test exists in the test sources); CI `tests.yml` | `enforced` for "names the current version" and "tests exist"; `procedural` for "the change is recorded in decisions.jsonl" (the ledger is private; the public repo cannot check it) | read the case; ran the suite |
| 2 | "A change to a contract, a check or guard, or anything that decides who may do what needs the outside review ... at least two reviewers that didn't build it, one from a different model family" | AGENTS.md, CONTRIBUTING step 4 | None in this repo. The cockpit's PR surface (`tools/prs.py`, `tools/cockpit.py`) *displays* reviewer families ("the page states the facts; any rule about how many reviews are needed is yours", `docs/cockpit.md`) but does not gate | `procedural` | read `docs/cockpit.md` §Pull requests; no CI step or CODEOWNERS |
| 3 | "Standard library only" | AGENTS.md; `tests.yml` comment | No import-scanning test (Telosmith has one; this kit does not). CI installs the package into a clean venv with `pip install .` and runs `relay-kit` commands, which would fail on a missing third-party import at import time only for modules those commands load | `procedural` (partly caught by the CI install smoke) | grep `tests/` for `stdlib_module_names`: none; read `tests.yml` install step |
| 4 | "Before you finish: `python3 -I -B tests/run_tests.py`, `node tests/browser_tests.cjs` and `python3 -I tools/prepublish_scan.py`" | AGENTS.md, CONTRIBUTING step 2 | All three run in CI `tests.yml` on push to `main` and on pull requests, each with `if: success() \|\| failure()` so one cannot hide another | `enforced` (outcome); the local run is `procedural` | read `tests.yml`; ran all three |
| 5 | "A record is provisional until Zach commits it; agents may append provisional records and proposed decisions, never accepted ones" | AGENTS.md "Records and ledgers" | `aec check` (CI `telosmith.yml`, hook) fails an accepted decision whose `decided_by` does not govern the subject; it cannot tell who typed `epistemic: committed`. Only `AU-owner@1` governs, so an agent writing `committed` is caught only by review | `enforced` for accepted decisions lacking a governing authority; `procedural` for the epistemic field | read Telosmith `records.py` authority rule; records list |
| 6 | "Append only. Never edit or delete an earlier line in `.aec/records.jsonl`" | AGENTS.md, `.aec/README.md` | `aec check` catches a same-id/version conflict between two present lines; nothing diffs the file against the PR base. (The *ledgers* in a records repo do get this: `relay-kit ledger check --git --base`, in `tools/check_ledgers.py`, run by the `relay-kit init` CI template and pre-push hook. The kit's own `.aec/records.jsonl` does not) | `procedural` for a silent rewrite; `enforced` for conflicting duplicates | read `telosmith.yml`; read `check_ledgers.py` and `tools/templates/records/dot-github/workflows/records.yml` |
| 7 | "Records cite [ledger] ids as text only ... this public repo cannot read them" | AGENTS.md | True by construction (no path to the private repo in the kit); nothing to enforce | `advisory` | — |
| 8 | "The in-review cockpit@1 and mcp@1 get requirement records only once their code is on main" | AGENTS.md | None. Both are on main (`tools/cockpit.py` at `cockpit@4`, `tools/mcp_server.py` at `mcp@1`); `.aec/records.jsonl` holds `RK-R-export@1` only; no `RK-R-cockpit` or `RK-R-mcp` record exists | `obsolete` (the condition has been met; the sentence still describes a pre-merge state and names `cockpit@1`) | listed record ids; read `contracts.json` |
| 9 | "Records ... hold the requirements behind each contract and the code that implements them" | AGENTS.md | `aec check --edges` fails on a dangling `implemented_by code:` reference (CI `telosmith.yml`); nothing requires that each contract *has* a requirement record. One of nine contracts does | `enforced` for references that exist; `procedural` for coverage | records list vs `contracts.json` (9 contracts) |
| 10 | "After editing code, the Claude Code hook runs `aec check` and `aec code-impact` ... without it the hook reports that and does not block" | AGENTS.md, `.claude/settings.json` | Hook exists and does what AGENTS.md says; it exits 1 with a message when `aec` lacks `--exit-code`, 2 on findings. The hook writes `.aec/edges.jsonl` and `.aec/hook-before.jsonl`, both git-ignored | `enforced` locally for Claude Code only (Codex and humans do not run it); CI backstop is `telosmith.yml` | read the hook; `git check-ignore` |
| 11 | "The `telosmith` CI job runs the same check on every pull request" | AGENTS.md | `telosmith.yml` `on: [push, pull_request]`; records check fails on errors; code-impact is report-only (`exit 0`) | `enforced` (records check); `advisory` (code-impact) | read the workflow |
| 12 | "Pinned to the Telosmith commit ... change the ref only to a release >= v0.5.0 or a commit you have reviewed" | `telosmith.yml` comment | Pin is a commit SHA (`c5ed7af92584`), which exists upstream. No `v0.5.0` tag exists in Telosmith (`git tag`: `v0.4.0`, `v0.4.1`), so the "release" half of the rule cannot currently be followed | `enforced` (pin is a SHA); the guidance text is partly `obsolete`/unfollowable | checked upstream tags |
| 13 | "Open a pull request and check the CLA box in the template" / Author line / What-Why-How-Authorized sections | CONTRIBUTING step 3, `.github/pull_request_template.md` | Template only; no CLA bot. `tools/prs.py` reads sections verbatim and the cockpit shows a missing section as missing, which makes an omission *visible* but not failing | `procedural` | read template; `docs/cockpit.md` §Pull requests |
| 14 | "A release is a commit on main tagged `vX.Y.Z` by the owner" / "The version is in `pyproject.toml` and `tools/__init__.py`" | CONTRIBUTING Releases | No test that the two version strings agree (both read `0.1.0` today); CI prints `relay-kit --version` but compares nothing | `procedural` | grep tests for `__version__`: none |
| 15 | "Anything that depends on the kit ... pins a tag, or better a commit, never a branch" | CONTRIBUTING Releases, ARCHITECTURE §3 | Enforced *downstream*: Telosmith's `tests/test_records_location.py::test_relay_kit_is_pinned_to_one_ref_everywhere`; the `relay-kit init` template inserts `{{KIT_REF}}` with a comment preferring a SHA. Nothing here can check other dependants | `enforced` where the dependant has a test (Telosmith); `procedural` elsewhere | read Telosmith test name; read `records.yml` template |
| 16 | "A change to a contract's format is at least a minor version" | CONTRIBUTING Releases | None | `procedural` | — |
| 17 | P-01 "Only the owner authorizes": owner-only cancel, sender/recipient checks, TODO-filled draft can't be sent | `governance/purpose.md`, ARCHITECTURE §1 | Three named tests in `tests/run_tests.py`; the governance case verifies they exist | `enforced` (software part, as labelled); `procedural` (skills) | ran suite; governance case |
| 18 | P-02 "Proposals and reports are not permission" | purpose.md | Labelled procedure; the MCP server (`tools/mcp_server.py`) writes nothing and `docs/mcp.md` says proposals over MCP are deferred, which keeps the invariant by construction on that route | `procedural` (as labelled) | read ARCHITECTURE §Records |
| 19 | P-03 "Sent is not done" | purpose.md | Ledger state machine; three named tests | `enforced` | governance case |
| 20 | P-04 "Every message keeps its source" | purpose.md | Schema ids and paste detection; three named tests | `enforced` | governance case |
| 21 | P-05 "Verification means fresh evidence" | purpose.md | Relay completion and quote verification tests; reviews are procedure | `enforced` + `procedural` (as labelled) | governance case |
| 22 | P-06 "Say what wasn't checked" | purpose.md | Three named tests where data can be missing | `enforced` + `procedural` (as labelled) | governance case |
| 23 | P-07 "The owner's words stay private ... a pre-publish scan" | purpose.md, CONTRIBUTING (by reference), `tests.yml` | `tools/prepublish_scan.py` in CI catches emails, chat links and tokens. Private *names* are checked only when a `.private-names` file exists, and that file is git-ignored, so CI never checks names (the run printed "names not checked") | `enforced` for emails/links/tokens; `procedural` for names | ran the scan; read `tests.yml` |
| 24 | "ARCHITECTURE.md lists exactly the invariants in purpose.md" | implicit (ARCHITECTURE §1 table) | `tests/run_tests.py` case "governance: ARCHITECTURE.md lists exactly the invariants in purpose.md" | `enforced` | read case |
| 25 | "Every invariant states its enforcement and cites real tests" | purpose.md intro | `tests/run_tests.py` case of that name | `enforced` | read case |
| 26 | DEC-019 "the ledger still changes only through a human-run `ledger_append` or `handoff set`"; cockpit buttons only compose lines; MCP writes nothing | ARCHITECTURE §Records, `docs/cockpit.md` "What it never does", `docs/mcp.md` | `tests/test_cockpit.py` and `tests/test_mcp.py` (write-free MCP, compose-only buttons); `tests/browser_tests.cjs` for the page | `enforced` | ran both suites; test names in `contracts.json` |
| 27 | "Keep intermediate files out of the records: `edges.jsonl`, `before.jsonl`, `hook-before.jsonl` are regenerated, never committed" | `.aec/README.md`, `.aec/.gitignore` | `.aec/.gitignore` discourages adding all three locally; CI regenerates edges "never trusted from the branch", but no check rejects tracked intermediate files | `procedural` (assisted by ignore patterns) | read `.aec/.gitignore` and `telosmith.yml` |
| 28 | "Add a context, a role or a tool only when removing it would make a result the owner cares about worse" / no orchestration layer | ARCHITECTURE §3, §"What this deliberately doesn't do" | None | `advisory` | — |

Counts by primary label (28 rules): `enforced` 17 (rows 1, 4, 5, 9, 10, 11, 12, 15, 17, 19–27), `procedural` 8 (rows 2, 3, 6, 13, 14, 16, 18, plus the remainder of several enforced rows), `advisory` 2 (rows 7, 28), `planned` 0, `obsolete` 1 (row 8; row 12's guidance text is partly unfollowable).

## Mismatches found

1. **AGENTS.md describes cockpit and MCP as "in-review" at `cockpit@1`, but `cockpit@4` and `mcp@1` are on main, and the promised requirement records were never appended.** `.aec/records.jsonl` has `RK-R-export@1` only; eight of nine contracts in `governance/contracts.json` have no requirement record, although AGENTS.md says records hold "the requirements behind each contract".
   *Proposed fix:* append `RK-R-cockpit@1` and `RK-R-mcp@1` (provisional, `implemented_by code:` the two modules) and reword the AGENTS.md sentence to present tense; or drop the sentence and say which contracts have records.
2. **Release availability (informational, not a rule mismatch).** `telosmith.yml` says "change the ref only to a release >= v0.5.0 or a commit you have reviewed". The audit found only `v0.4.0` and `v0.4.1` tags; the workflow uses commit `c5ed7af92584`.
   No policy change is required: the release threshold applies to future releases, and the reviewed-commit alternative remains available.
3. **P-07's name scan never runs in CI.** `prepublish_scan.py` reads `.private-names`, which `.gitignore` excludes, so the CI step checks emails, links and tokens only and prints "names not checked". `governance/purpose.md` says "a pre-publish scan" without this limit.
   *Proposed fix:* say in purpose.md P-07 and CONTRIBUTING that the name list is local-only (owner's machine), or store a hashed name list in the repo so CI can check without publishing the names.
4. **Append-only is enforced for the records repos the kit creates, not for the kit's own `.aec/records.jsonl`.** `relay-kit ledger check --git --base` catches a rewritten ledger line in a records repo (template CI and pre-push hook); nothing diffs `.aec/records.jsonl` against the PR base here, and `aec check` catches only conflicting duplicates.
   *Proposed fix:* add a step to `telosmith.yml` that fails if any pre-existing line of `.aec/records.jsonl` changed or vanished versus the base (`git diff --unified=0 base...HEAD -- .aec/records.jsonl`, reject `-` lines); until then label the AGENTS.md rule procedural.
5. **"Standard library only" has no check.** Telosmith enforces this with `tests/test_stdlib_only.py`; this kit relies on review plus the CI install smoke.
   *Proposed fix:* a 20-line stdlib-only case in `tests/run_tests.py` over `tools/` and `skills/*/scripts/`, modelled on Telosmith's.
6. **Version string agreement is unchecked.** CONTRIBUTING names two places for the version (`pyproject.toml`, `tools/__init__.py`); nothing asserts they match.
   *Proposed fix:* one `run_tests.py` case reading both.
7. **Residual cockpit docs tension (CH-035 §6 follow-up).** The "Questions" and PR-fetch statements were reconciled (`docs/cockpit.md` lines 87–90 and 281–284 now say opt-in build-time fetching; line 290 says Questions is wired since @2). One soft contradiction remains: "What it never does" says "decider, basis and locator are always left for the owner" (line 288) while the Choices section says the page fills the decider "which it remembers in this browser" (line 128).
   *Proposed fix:* reword line 288 to "never invents a justification: basis is always yours; decider and locator come only from what you typed".
8. **The outside-review rule (row 2) is the heaviest rule in the repo and is wholly procedural.** The cockpit shows reviewer families but states no rule. That is consistent with the design ("any rule about how many reviews are needed is yours"), so this is a label, not a defect: the PR template could carry a "Review: contract/check/authority change? yes/no; reviewers and families" line so the gap is visible on every PR.

## What this audit did not do

- It did not audit the nine `skills/*/SKILL.md` files rule by rule; their enforcement is "procedure" by the kit's own definition.
- It did not read the owner's private ledgers for DEC ids beyond `status` output; the kit cannot.
- It did not judge whether any rule is right; only what would fail if it were broken.
