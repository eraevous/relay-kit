# Architecture

This kit grew out of real use: relays between ChatGPT and Claude, captures, handoffs to
fresh chats. This page fixes what that use showed to be load-bearing, and deliberately
leaves the rest open. Models, modes and chat interfaces change month to month, so the
architecture fixes **rules and interfaces**, not a topology of agents.

It has three layers.

## 1. Invariants (what never changes with the tools)

Full text, enforcement and tests: [governance/purpose.md](governance/purpose.md).

| | Invariant | Enforced by |
| --- | --- | --- |
| P-01 | Only the owner authorizes | software in the ledger (names, not identities) + procedure |
| P-02 | Proposals and reported decisions are not permission | procedure |
| P-03 | Sent is not done; completion needs a filed output | software |
| P-04 | Every message keeps its source; pastes and relays are marked | software |
| P-05 | Verification means fresh evidence, not agreement | software + procedure |
| P-06 | Say what wasn't checked | software where data can be missing + procedure |
| P-07 | The owner's words stay private | procedure (public kit / private records split) |

"Procedure" means an assistant follows it because a skill says so. Native chat tools
can't enforce permissions finely, and this kit prefers native tools over building an
orchestration layer, so each invariant says plainly which kind of guarantee it has.

## 2. Contracts (the interfaces every tool and assistant speaks)

Registry: [governance/contracts.json](governance/contracts.json).

| Contract | What it fixes | Spec |
| --- | --- | --- |
| `relay-kit/conversation@1` | conversation JSON and message ids (`T3a2`) | [docs/conversation-schema.md](docs/conversation-schema.md) |
| `relay-kit/handoff@2` | the handoff file, its states and log, the owner and lane settings (reads @1) | [handoff-ledger](skills/handoff-ledger/SKILL.md) |
| `relay-kit/paste-markers@1` | `FROM:` … `END FROM` and `>"""` | [docs/paste-markers.md](docs/paste-markers.md) |
| `relay-kit/relay-result@1` | relay statuses: done / unconfirmed / pending | [chat-relay](skills/chat-relay/SKILL.md) |
| `relay-kit/export@1` | read-only export: records file and coverage manifest | [docs/export.md](docs/export.md) |
| `relay-kit/lifecycle@1` | what the ledgers say is open, settled, in force or contested, shared by every reader | [docs/lifecycle.md](docs/lifecycle.md) |
| `relay-kit/cockpit@8` | static control surface: index.html and the cockpit.json it was built from | [docs/cockpit.md](docs/cockpit.md) |
| `relay-kit/prs@3` | governance/pr-state.jsonl: pull-request state read from GitHub at build time | [docs/prs.md](docs/prs.md) |
| `relay-kit/status@1` | status/<date>-<edition>.md: a hand-written narrative edition, rendered as written | [docs/status.md](docs/status.md) |
| `relay-kit/decision-support@1` | governance/decision-support.jsonl: authored notes per pending item | [docs/decision-support.md](docs/decision-support.md) |
| `relay-kit/budget@2` | governance/budget.jsonl: readings of compute and time, labelled by method, with corrections that replace | [docs/budget.md](docs/budget.md) |
| `relay-kit/telemetry@2` | a derived view of compute, delivery, review, remediation, idea-to-completed-handoff latency, coverage and coordination loss, never stored | [docs/telemetry.md](docs/telemetry.md) |
| `relay-kit/mcp@1` | read-only MCP server over the records (stdio) | [docs/mcp.md](docs/mcp.md) |

**How a contract changes:** only by a new version. The spec, every implementer and the
tests change in the same commit, and the owner records the change in the decision ledger.
The tests fail if any implementer still names the old version, so a half-done change
can't pass.

## 3. Replaceable parts (chosen per task, free to change)

| Part | Current choices | Known from use |
| --- | --- | --- |
| Model and mode | Claude; ChatGPT Chat, Work or Temporary | Cold handoffs succeeded in all three ChatGPT modes |
| Delivery | browser relay (chat-relay), paste, shared folder | Paste with two files is the coldest and most portable route |
| Browser | Claude's built-in browser, Claude in Chrome | Site markup changes; the selector table records what was verified when |
| Contexts per task | one context, or inquiry + builder (+ fresh check) | Not yet compared; the first pilot does that |
| Record store | a private git repository, made with `relay-kit init` | Other stores need the five operations in [docs/storage.md](docs/storage.md); none built yet |
| Delivery of the kit | `relay-kit` package (pin a tag or commit), skill zips | Telosmith pins a tagged release (DEC-045) |
| Starting back up | `tools/status.py` over the record store | Read-only; it reads the records, so it's only as current as they are |
| Reading the records from an MCP client | `relay-kit mcp` (stdio) | Local clients only; ChatGPT web would need a hosted server |

Nothing in this layer is mandated. Add a context, a role or a tool only when removing it
would make a result the owner cares about worse.

## Records

Kept proportionate; the owner shouldn't become a second-shift bookkeeper.

- **Handoff files**, in the owner's private folder: task, authorization, proposals, state.
- **Decision ledger and challenge ledger** (`decisions.jsonl`, `challenges.jsonl`), also
  private, append-only, checked by [tools/check_ledgers.py](tools/check_ledgers.py).
  Decisions record what the owner decided, in their words, with where they said it.
  Challenges record objections and known risks.
- Run notes go in the handoff's output. There is no separate run database.

**Exporting** ([tools/export.py](tools/export.py), `relay-kit/export@1`) reads these records and any
conversation@1 captures among them, and writes a records file plus a manifest to a private folder
outside them. It is a reader like `status`: it changes nothing and derives nothing the records
don't hold. Each exported item says whether it is a verbatim source, a relay, a derived summary, a
proposal or an adopted decision, using the ledger's status rather than inferring one, and the
manifest lists what the records refer to but don't contain (uncaptured threads and sessions,
missing outputs, hidden steps) so that missing is never read as empty.

**The cockpit** ([tools/cockpit.py](tools/cockpit.py), `relay-kit/cockpit@8`) is a third reader on
the same export readers: it renders the records into one static page (Choices, Topology, Budget,
Run/Drift and the other surfaces of the v0.4 design prototype) plus the `cockpit.json` it was built from.
It keeps DEC-019 by construction: its buttons only compose lines for the owner to paste, so the
ledger still changes only through a human-run `ledger_append` or `handoff set`. Its one new record
is `governance/budget.jsonl`, append-only, written by `relay-kit budget-append`, never by the page.
**The pull-request reader** ([tools/prs.py](tools/prs.py), `relay-kit/prs@3`) is the one part of the kit that
reads pull requests from GitHub, and only when someone runs it: it appends what changed to
`governance/pr-state.jsonl`. The cockpit renders that file; any other reader (a telemetry collector, say)
should read the file rather than GitHub again. It copies PR body sections verbatim and summarises nothing.
**The comments reader** ([tools/comments.py](tools/comments.py), `relay-kit comments`, no contract) is the
small sibling for a lane's poll: the conversation, line and review comments posted since a stored time, each with
the actor id it is signed with or `unsigned` ([docs/comments.md](docs/comments.md)). It prints and writes
nothing but its cursor; what it reads is evidence for a person or lane to record, never a record itself.
**The MCP server** ([tools/mcp_server.py](tools/mcp_server.py), `relay-kit/mcp@1`) is the same kind
of reader, reached over the Model Context Protocol on stdio instead of the command line. It reuses
export@1's readers, so every result carries the same evidence classes, plus a provenance line with
the hash of what it read. It encodes DEC-019 (information crosses freely, authority never crosses
implicitly): reads are open, and it writes nothing to the records, so no MCP call can adopt,
accept, resolve or complete anything. Proposals over MCP are deferred until they have dedup, rate
limits and explicit review. Record text is returned as data; no tool acts on it. It is a delivery route (layer 3), not an orchestration layer.

Claims in records keep one of four labels where it matters: **stated preference**,
**belief to test**, **proposal**, **result** (with the artifact that shows it). A result
needs something inspectable: a test, a file, a captured conversation.

## How the architecture itself changes

1. A change starts as a **proposal** in the decision ledger (or a handoff).
2. If it's an empirical claim (say, "a separate verifier catches more"), it's tested on
   one real task against the simpler option, time-boxed, with the check written first.
3. The owner adopts, rejects or defers it; the ledger records that.

The first such test is set up: one context alone versus a handoff plus a fresh check, on
one Data-Guide task. It waits for the owner's authorization.

## What this deliberately doesn't do

No general orchestration layer, standing multi-agent team, fixed ChatGPT-versus-Claude
role map, or automatic sharing of the owner's history across models. Each would need a
measured reason first.

## Lineage

Draws on the owner's architecture proposal of 2026-10-28 (kept with the private records):
its four claim labels, "verify outcomes, not agreement", the A-versus-B selection policy,
and the rule that a tool-making role must show a second real use. Its 19 recommendations,
8 pilots and full templates were left out, to keep record-keeping proportionate.
