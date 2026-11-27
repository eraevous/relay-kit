# Budget records: `relay-kit/budget@2`

`governance/budget.jsonl` in the records: append-only JSON lines, one per reading of compute or time
spent. Read through [tools/budget.py](../tools/budget.py) by every reader (the [Cockpit](cockpit.md)
today, the telemetry view next), and written with `relay-kit budget-append`.

## Fields

| Field | Since | Meaning |
| --- | --- | --- |
| `ts` | @1 | ISO time (UTC) the line was recorded, or covers up to |
| `actor` | @1 | `assistant:lane#session`, or the owner |
| `window` | @1 | `session`: one session's or work block's use; `weekly`: a reading of the whole week so far |
| `tokens_in`, `tokens_out` | @1 | integers >= 0 |
| `agents`, `note`, `human_minutes`, `merges` | @1 | optional |
| `id` | @2 | stable id. A line without one is known by `bud-` + the first 10 hex digits of its sha256 (stable, since the file is append-only) |
| `method` | @2 | `measured` (a provider or tool meter: Claude Code's session logs, GitHub timestamps), `proxy` (a stand-in: a merge window, a harness-reported total), `estimate` (anything else). Required on new lines; an older line without it reads as `unlabelled`, never as measured |
| `interval_start`, `interval_end` | @2 | what the reading covers (ISO). Start is not after end |
| `includes_children` | @2 | `{"included": true or false, "ids": [...]}`: whether sub-agent use is inside these numbers. Listed child lines are then not counted again |
| `correction_of` | @2 | the id of an earlier line this one **replaces**: readers count this line instead, never both. Relabelling an old line (say, as an `estimate`) is a correction carrying the same numbers |
| `metric_version` | @2 | the definition the numbers follow (`budget@2`) |
| `scope` | @2 | what the numbers cover, in words ("Kairos threads only") |
| `source` | @2 | where the numbers came from (a log location, a capture id, a status file) |
| `tokens_detail` | @2 | `{input, output, cache_write, cache_read, by_model}`: the split behind `tokens_in` (input + cache_write) and `tokens_out` (output). Cache reads are kept here, never folded into `tokens_in` |

**Corrections.**
- A correction must name an **earlier** line with the **same window**. Anything else (itself, a later
  line, an unknown id, another window) is reported, and the line counts as a plain reading.
- A chain resolves to its last line.
- Two corrections of the same line are a **conflict**: the later one counts, the earlier one doesn't,
  and both are reported.
- A counted line is counted **at the time of the line it replaces**, so relabelling an old reading
  never moves its use into the current week. `budget-append --correction-of` also defaults `--ts` to
  the target's time, and refuses to correct a line that has already been corrected, naming the
  latest line in its chain.

**Children.** A parent's `includes_children` follows each child through its corrections. A correction
that doesn't say otherwise keeps its target's inclusion. A child is dropped only where its parent is
counted in the same window. A line that includes itself, and two lines that include each other, are
reported and ignored.

**Ids.** `budget-append` never gives two lines the same id; the same reading appended twice gets
`-2`. Two lines with the same id in the file are both read, and the duplicate is reported.

**Old readers.** A reader from before @2 adds a correction to the line it replaces. Append
corrections only once every reader in use understands @2.

**Missing is not zero.** A reader that has no line for an actor or a period says so; it never fills
in a 0.

## Definitions

- **Unit:** tokens (`tokens_in + tokens_out`); `tokens_detail` gives the split. There are no dollars:
  no token price is recorded.
- **Provenance:** each line names its `actor` and, from @2, its `method` and `source`. A line is an
  observation by whoever appended it. Only `measured` lines come from a meter.
- **Aggregation:** sums are over counted `session` lines (corrections and children applied). A
  `weekly` line is a separate reference, never added. Every total says which methods it mixes.

## Writing

```
relay-kit budget-append --window session --tokens-in N --tokens-out N --method measured \
    [--interval-start ISO --interval-end ISO] [--includes-children yes|no [--child-id ID ...]] \
    [--correction-of ID] [--scope TEXT] [--source TEXT] [--agents N] [--note TEXT]
```

The actor comes from `RELAYKIT_ACTOR` and the time is now, unless given. The line's id is computed
from its content, and the line is printed. Commit it with the records.

## Measured usage from Claude Code

```
relay-kit usage-ingest --session <session id> [--session ...] [--projects ~/.claude/projects] [--dir governance]
```

It runs on the machine that ran the session. Claude Code keeps each session's transcript there under
`~/.claude/projects/<folder>/<session>.jsonl`, with its subagents in `<session>/subagents/agent-*.jsonl`.
Each assistant message carries the API's `usage` (input, output, cache creation, cache read) and the
model. For each session it appends one line:

- `method: measured`, `window: session`, with the interval from the first to the last timestamp;
- the token split under `tokens_detail`, per model too;
- `includes_children: {"included": true}`, with the number of subagent logs in `agents`;
- id `cc-<session>`.

**What it counts.** A message split over several log lines is counted once, by its message id. The
logs' `costUSD` isn't used. Only what is on disk is counted: a transcript compacted mid-session keeps
only its later part, though subagent logs may reach back further. The line's `scope` says how many
messages each file gave.

**Incomplete logs.** A main log that was compacted (it holds a `compact_boundary` record), or whose
first entry is more than an hour after the session started (Claude Code's `cost-state` record gives
the start), misses part of the session. A usage line without a response id can't be de-duplicated,
so it isn't counted. In any of these cases the line is `method: estimate` with `complete: false`: its
numbers are a lower bound. `log_coverage` gives the compactions, the tokens the compaction reported
dropping, when the session started and when its logs start. The `cost-state` record's own totals are
not used: whether they span process restarts isn't verified.

**Running it again** for the same session appends a correction of its latest line, so a session is
counted once. It is refused if the logs now show less than that line (compacted or deleted since),
and if the session's line appears more than once in the file. Nothing but the line leaves the
machine, and the line holds no file paths or folder names.

**Estimates it replaces.** Earlier estimate lines covering the same session (an orchestrator's
approximate block totals, say) aren't linked to the measured line automatically. Correct them by
hand (`budget-append --correction-of`), or the telemetry view counts both and flags the actor.

**What it can't meter.** It can't see Codex or ChatGPT sessions, or another machine's sessions. Those
stay `estimate` lines, or absent, and a coverage count says so.

## Migration

Nothing is edited. Lines written before @2 stay as they are and read as `unlabelled`. To label them,
append one correction per line with the same numbers and `--method estimate` (or what they are).
Append those corrections only once the readers in use understand @2: a reader from before @2 would
add a correction to the line it replaces.
