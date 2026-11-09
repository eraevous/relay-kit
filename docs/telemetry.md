# Telemetry: `relay-kit/telemetry@2`

A **derived view** of how the work is going, rebuilt from the records on every run and never entered
by hand (DEC-123: budget.jsonl and pr-state.jsonl are the records; this isn't). It is built by
[tools/telemetry.py](../tools/telemetry.py) and shown on the Cockpit's Review surface.

```
relay-kit telemetry --root RECORDS [--lane LANE] [--content full|metadata] [--as-of TIME] [--out FILE]
```

FILE must be outside the records; without `--out` it prints.

**Lane and content, as the Cockpit applies them.** `--lane` keeps only that lane's handoffs, the
sessions of that lane named in them, and that lane's budget lines; pull requests are per repository
and are all shown. `--content metadata` derives nothing from free text and emits none: the
overlapping-claims figure, remediation and the capture-latency figure (which need handoff or
pull-request text) are `unknown`, with that reason. The shared handoff reader still opens every
handoff file to take its front matter and log; only the body stays unused.

**Budget lines.** A line is read only if every reader accepts it (budget@2's `row_problems`, the same
check the Cockpit uses); any other line is counted under `invalid_lines` with its reasons, and never
stops the view or the Cockpit build.

**Inputs.** It reads `governance/budget.jsonl` ([budget@2](budget.md)), `governance/pr-state.jsonl`
([prs@2](prs.md)), the handoffs and the ledgers ([lifecycle@1](lifecycle.md)). It never calls GitHub
and writes nothing into the records.

**As of.** `--as-of` if given (the Cockpit passes its own, so the page and the panel agree), else
the newest timestamp it read, never the clock. A pr-state fetch time is when the reader ran, so it
doesn't count; a record time more than a day after the last fetch is left out of as of and listed in
`notes`. Weeks are ISO calendar weeks in UTC.

**What each figure carries.** Every metric carries its frozen **definition**, a **version**, and a
**label** taken from its inputs (`measured`, `proxy`, `estimate`, `unlabelled`, or `unknown`). Where
the records can't say, it gives `unknown` with a reason. A figure counted from the records'
structure rather than measured (coverage, coordination loss) is labelled `derived`. **Missing is never zero.** Distributions
come as median, p90 and n, never a mean.

**No score, no targets.** DEC-100's "2-3 points" is an evaluation objective with no baseline yet, so
it appears nowhere. A merged pull request is not a production deployment, so the remediation figure
is not DORA's Change Failure Rate.

## Metrics

| Metric | Frozen definition | Label |
| --- | --- | --- |
| `compute_consumption` | Input and output tokens per uniquely identified execution interval (budget@2 session lines, with corrections and included child runs applied), by how each was measured; cache reads apart, from measured lines' `tokens_detail` only. Lines with zero tokens are skipped; actors with both a measured and an estimated line in one window are listed as a possible double count. Unattributed use stays unattributed | the methods of its lines |
| `delivery_throughput` | Merged pull requests per ISO calendar week (UTC), by repository; abandoned (closed, not merged) counted apart | proxy (for delivered value) |
| `lead_time` | Wall-clock hours from pull request creation to merge. Never human work time; pull requests missing either time are counted under `excluded` | measured |
| `review_burden` (@2) | Change-request review **submissions** per merged pull request that has a recorded GitHub review submission (GitHub's verdict, or a verdict stated at the start of the submission's body). Submissions, not revision rounds. Merged PRs are split into `reviewed` / `unreviewed` (reviews read, none) / `review_state_unknown` (reviews unread); unknown is never counted as unreviewed or zero, and `coverage` is null when any are unknown (`coverage_lower_bound` is always given). Source: `/pulls/{n}/reviews` only; ordinary PR comments, inline comments and off-GitHub reviews aren't captured. Denominator: the pr-state read window (`read_window`), not repository history. @1 counted unreviewed PRs as zero. | measured |
| `post_merge_remediation` | Share of merged pull requests with an explicitly linked later revert or fix (below) within 14 days (DEC-123) and 30 days (the other-family review's recommendation), counted only over cohorts at least that old | proxy |
| `surviving_changes` | Merged pull requests not reverted or fixed by an explicitly linked change within 14 days (DEC-123's unit of output; ">50% rewritten" can't be told from pr-state and isn't applied) | proxy |
| `human_intervention` | Owner minutes recorded on budget lines (`human_minutes`, reported as `minutes_recorded`), labelled by those lines' methods; unknown otherwise, never inferred from session spans | the methods of its lines, or unknown |
| `measurement_coverage` | The agent sessions named in the handoffs (counted by session id, not by how the actor is spelled) that have a measured or any budget line, budget lines by method, repositories read and whether more pull requests were available than read, and pull-request lines with unread parts | derived |
| `idea_to_completed_handoff_latency` | Hours from a recorded idea-capture time to the first completed handoff carrying it: the `Idea captured: <ISO time>` line in the handoff's **Task or Context** section (a time written by hand from the intake entry and **not verified against it**) to the handoff's first `completed` step, which exists once an output file does (DEC-162). Completion is not merged, accepted, deployed or used: this is a **progress proxy** for DEC-162's idea-to-realization latency, not that latency. The unit is a handoff carrying a marker, so one idea carried by two handoffs is two cases (no idea identity or de-duplication yet). A marker that doesn't parse (including a time without a zone), lies after `as of`, or follows the completion is excluded under that name with the handoff id; a handoff without a marker isn't a case; a captured idea whose handoff is still open is listed under `in_flight` with its age. Not used in `--content metadata` | measured when both times exist, else unknown |
| `coordination_loss` | Stale handoffs (open, no step in the 24 h before as of), cancelled handoffs, handoffs in progress claiming the same path, and ledger transitions in conflict. Reconciliation time isn't recorded: unknown | derived |

**An explicitly linked fix.** A later merged pull request in the same repository whose title or body
has a fix word (`fix`, `fixes`, `fixed`, `fix regression from`, `hotfix for`, `revert`, `reverts`, ...)
immediately before the earlier one's reference (`#N`, `owner/repo#N` or its pull-request URL), or a
line starting `Reverts owner/repo#N`. A bare mention ("Stacked on #2"), another repository's `#N`,
or a fix word elsewhere in the sentence does not link. It is a heuristic over wording, hence `proxy`.

**Carrying an idea's capture time.** A handoff that carries a captured idea says so with one line in its Task or
Context section, `Idea captured: 2026-11-06T23:05Z (kairos/intake.md, crawl 23:05 CT)`: an ISO-8601 time with a
zone, the intake entry's own time rather than the handoff's, followed by where the capture lives. It is a
recorded time, written by the handoff's author; the metric doesn't check it against the source. Only that
line in those two sections is read (the same words in Expected output, Log or elsewhere don't count); the
handoff contract (handoff@2) is unchanged, and a handoff without the line is simply not a case.

## Not yet

- **Dollar figures.** No price table is recorded, so dollars per surviving change and per active day
  are left out rather than guessed.
- **Attributing sessions to pull requests by branch.** Budget lines don't carry branches yet.
- **ChatGPT and Codex spend.** No metering is exposed; estimates from conversation captures are not recorded yet.
- **Authorship-based comparisons.** They wait on CH-017 identities.
