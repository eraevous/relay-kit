# Export: `relay-kit/export@1`

A read-only export of what a records store holds about a lane, so a later evaluation can put
conversations, relays, handoffs, decisions and outputs side by side. It exports only what was
recorded: an arm of a lane that nobody captured shows up as a **coverage gap**, never as an
empty conversation.

```
relay-kit export --root path/to/private-records --out path/to/private-export \
  [--lane AEC] [--actor claude:aec[#SESSION] ...] [--since ISO] [--until ISO] \
  [--kinds conversations,handoffs,outputs,decisions,challenges] \
  [--granularity inventory|message] [--format jsonl|json] [--content full|metadata|none] \
  [--sanitize-manifest] [--allow-public-dest]
```

From a checkout: `python3 tools/export.py ...` or `python3 -m tools export ...`.

## What it reads

| Source | Kind | Notes |
| --- | --- | --- |
| `*.json` anywhere under `--root` whose `schema` is `relay-kit/conversation@1` | `conversation`, `message` | Also files with no `schema` that have `meta` and `turns` (captures made before the schema). Other conversation versions are refused and listed as unreadable. Symlinks and `.git` are not followed. |
| `handoffs/HO-*.md`, `handoffs/.ledger.json` | `handoff` | Parsed with the handoff-ledger script's own parser (`relay-kit/handoff@1` and `@2`). Lane codes come from `.ledger.json`. |
| `handoffs/outputs/**/*.md` | `output` | Linked to a handoff by file name. |
| `governance/decisions.jsonl`, `governance/challenges.jsonl` | `decision`, `challenge` | One record per line, hashed per line. |
| `threads.md` | (gaps only) | The chat links and actors it names are checked against the captures. |

It never writes inside `--root` and changes no source file.

## Selection

- `--lane` takes a lane name or code. `AEC` matches lane `aec` and every lane with code `AEC`
  (for example `aec-release`). A record matches if any lane it involves matches.
- `--actor` (repeat or comma-separate): `claude:aec` matches every session of that actor,
  `claude:aec#01N4` the sessions starting `01N4`.
- `--since` / `--until` are inclusive; a bare date covers its whole day. A record with no
  timestamp can't be placed in a window, so under a time filter it is left out and counted in
  `exclusions.no_timestamp_under_time_filter`, not kept silently.
- `--granularity message` (default) writes a `conversation` row and one `message` row per
  message; `inventory` writes only the `conversation` rows. Handoffs, outputs and ledger lines
  are one row each either way.

## Output

Two files in `--out` (a new folder, or one holding only a previous export):

**`records.jsonl`** (or `records.json`, an array, with `--format json`). Every record has:

| Field | Meaning |
| --- | --- |
| `kind` | `conversation`, `message`, `handoff`, `output`, `decision`, `challenge` |
| `rid` | Stable id: `conv:<platform>:<conversation_id>`, `msg:<platform>:<conversation_id>:<mid>`, `handoff:<id>`, `output:<path>`, `decision:<DEC-id>`, `challenge:<CH-id>`. Two files capturing the same conversation get a `~<file hash>` suffix. |
| `source_path`, `source_hash` | Path under the root and the sha256 of the source file (of the line, for ledger lines; the file's is `source_file_hash`). |
| `lane`, `lanes`, `actor`, `actors` | Where known. A lane is a name declared in `handoffs/.ledger.json` or as a lane heading in `threads.md` (or a declared lane's unique code); an actor's name part that isn't declared (a persona such as `chatgpt:nova`) gives no lane, and a lane code shared by several lanes adds none. Session ids keep their case and match a capture only as a whole link segment or its conversation id. Conversations also say where they came from: `lane_source` / `actor_source` is `capture` (the capture's meta), `threads.md`, `records session id` or `actor`. |
| `ts`, `ts_missing` | The record's own timestamp (message `ts`, handoff `created`, ledger `date`); `null` with `ts_missing: true` when the source has none. Nothing is guessed. |
| `evidence_class` | See below. |

Message rows copy `mid`, `id`, `ts`, `model`, `dictated`, `text` and `sources` exactly, and add
`seq` (order within the conversation), `turn`, `role`, `label`, `mid_derived` (the capture had no
`mid`, so the schema's rule computed one), `relay` and `contains_paste`.

**Evidence classes** describe a record's relation to its source, not whether its claims are true:

| Class | Used for |
| --- | --- |
| `source-grounded` | Copied from a record as it stands: an original message, a handoff file, an output, a challenge. |
| `relay` | A user-side message that opens with `FROM: <source>` (`relay-kit/paste-markers@1`): material carried from elsewhere, not the user's own words. `relay.origin_label` is the source as marked; `relay.origin_ref` is the first message id, handoff id or link on that line, when there is one. A `FROM:` block *later* in a message leaves the speaker as is and sets `contains_paste`. |
| `derived` | Computed by the export: the `conversation` summary rows (counts, time span). |
| `decision` | A decision-ledger line whose status is `adopted`, `rejected` or `superseded`: the owner decided something. Decision records also carry `in_force` and `applicability`, resolved by [`relay-kit/lifecycle@1`](lifecycle.md): `in_force` is `true` only when the line is `adopted` and no effective later line supersedes or revokes it and its own effective dates (if any) cover the ledger's latest date. It means "adopted, no structured supersession found", not that it is known to still apply. They also carry `closed_by`, `proposed_replacements`, `amended_by`, `conflict` and, for a line whose own transition takes no effect, `transition_problems`. Challenge records carry `open`, `closed_by` and `conflict`. |
| `proposal` | A decision-ledger line whose status is `proposed` (or anything else). |

The export reports a ledger's status; it never upgrades a proposal or an inference. An adopted
line without a `basis` is listed under `coverage_gaps.decisions_without_basis`.

**`manifest.json`** (`schema: relay-kit/export@1`):

- `root`: the records folder's name and `root_hash` (sha256 over every file read, as `path<TAB>sha256` lines), never its local path; `destination` likewise gives the output folder's name and any enclosing repository relative to it.
- `query`: the filters as given; `contracts_read`; `generator` (kit version).
- `sources`: each source location and whether it was read or absent, and every file read with
  its sha256 and size.
- `seen` (before filters) and `counts` (exported, per kind); `time_bounds` of the exported records;
  `outputs`: the records file's sha256 and record count.
- `redaction`: the `--content` level, its policy in words, and the fields it suppresses.
- `destination`: any git repository around the output folder, with each remote's visibility.
- `exclusions`: records left out by lane, actor or time (and those with no lane, actor or time to
  match on), kinds not requested, JSON files that aren't conversations, symlinks not followed.
- `coverage_gaps`, in the scope of the query:
  - `threads_without_capture`: chat links in `threads.md` with no capture (matched by conversation
    id or source URL), with their lane and listed status;
  - `actors_without_capture`: actors named in handoffs, ledgers or `threads.md` with no capture,
    and their uncaptured sessions;
  - `lanes_without_capture`;
  - `handoffs_missing_output`: handoffs with no `outputs/<id>.md`, flagged `inconsistent` when the
    handoff says `completed`;
  - `conversations_with_hidden_steps` (`hidden_counts` > 0: tool calls and reasoning that were
    counted but not captured) and `incomplete_captures` (renderer checks other than `ok`);
  - `raw_captures_not_rendered`: chat-capture raw downloads that were never rendered to conversation@1
    (render them with chat-capture to export them; this list isn't narrowed by lane or actor);
  - `records_missing_timestamps`, `messages_with_derived_ids`, `unreadable` (including handoff files
    without `id`, `status`, `from`, `created` or any section), `unknown_fields` (ledger fields, and
    conversation top-level, `meta`, turn and message fields the renderer doesn't write);
  - `summary`: a count for each.
- `limits`: what this export can't see, in words.
- `nondeterministic.generated_at`: the only field that changes between runs over unchanged input.

Records are ordered by kind, then id, then position (messages keep the capture's order), and
JSON is written with sorted keys, so the records file's hash is the same on every run over the
same input.

## Privacy

Exports can carry the owner's words, so:

- **Destination.** The export refuses an `--out` inside `--root`, and one inside any git
  repository whose remote is a public GitHub repository, or whose visibility it couldn't confirm
  Remotes are read through git itself (`git remote -v`), so `include`/`includeIf`, `insteadOf` and
  global config apply; if git can't resolve them, or (without git) the config has include sections,
  the remotes are unknown. Visibility: an unauthenticated GitHub API answer means public; **private
  counts only when an authenticated call says so** (`GH_TOKEN`/`GITHUB_TOKEN`, else the `gh`
  command). An unauthenticated 404 can also mean renamed, deleted or unreachable, so on its own it is
  unknown. Only `github.com` remotes can be checked; any other remote (GitLab, an ssh host alias such as
  `git@github-work:`, an unresolved alias) and a `.git` whose config can't be read count as
  unconfirmed and are refused, as is a GitHub remote when offline. A repository with no remote is allowed.
  `--allow-public-dest` overrides this, and the manifest records that it was used. A folder
  outside any repository (a scratch folder) needs no flag. A private repository is allowed, with
  a note not to commit the export by accident.
- **Content.** `--content full` (default) keeps exact text. `metadata` leaves out every free-text
  field (message text, sources and turn labels, titles, handoff bodies and log notes, ledger prose)
  and keeps each one's sha256 and length, so text can be matched to its source later. `none` also leaves
  out those hashes and lengths. The policy and the suppressed fields are written to the manifest.
- **Redacted is not anonymised.** `metadata` and `none` remove content, but by themselves the
  records and manifest still hold links (for example `coverage_gaps.threads_without_capture[].ref`),
  source paths and file names, session ids, conversation ids and content hashes that can be
  correlated with the sources. `--sanitize-manifest` (implied by `--content none`) replaces links,
  paths, the root and destination names, repository folders and remotes, session ids and
  conversation ids with `h:` + 16 hex digits of their sha256, everywhere in both files, so
  cross-references still line up. Content hashes stay. The manifest's `sanitize` block records
  whether it was on, the policy, and how many values of each class were replaced.
- The command prints only counts. Tests use synthetic records only.

## Not in this version

Conversation and turn summaries, Markdown bundles, selecting by handoff, thread or session id,
linking conversation segments to handoff state changes, other platforms' adapters, live capture,
cross-lane flows and repository or test events are later increments (issue #14, "Next" and
"Later"). Hidden events can't be reconstructed from a conversation@1 capture at all; only their
counts are reported.

A new export format is a new version (`@2`); the spec, `tools/export.py` and the tests change in
the same commit.

Handoff @3 progress entries are preserved in the exported log as `state: "note"`;
they are events, not transitions. The handoff status and completion time still
come from its state transitions. The export envelope remains export@1.
