# Prior art: coordination problems this kit inherits

Several assistants working on shared records is a distributed system. Its problems
(collisions, races, lost messages, stale reads, work nobody closes) were solved long ago
in databases, job queues, version control and network protocols. This table maps each
problem the kit has met, or will meet, to the established pattern, and says where the
kit stands. The point is to use what exists and not invent a protocol.

Status: **in kit** (code and tests), **procedure** (a skill says so), **proposed**
(decision ledger), **open**.

| Problem | Seen here | Established pattern | Kit status |
| --- | --- | --- | --- |
| Two writers pick the same id | HO-20261029-001, two clones, 2026-10-29; candidates C-11..C-13, 2026-11-01 | **Partition the id space** by writer (namespaced ids, as in UUIDs with a node part, or Snowflake ids); or **claim with compare-and-swap** (a create that fails if the id exists) | In kit (handoff@2): lane-coded ids with a random suffix, so sessions create them offline without colliding; `new` still skips ids seen in git and never replaces a file; an offline clash surfaces as a git conflict. `tools/new_id.py` does the same for candidate lists. Decisions keep one DEC-NNN sequence (DEC-018) |
| Two writers update one file | Concurrent `set` on one handoff; HO-20261101-003 overwritten by another session | **Optimistic concurrency**: read a version, write only if unchanged; retry on conflict. Locally a lock file; across clones `git push` refusing a non-fast-forward is the compare-and-swap | In kit: hash check and lock; `--expect <state>`; `set` refuses when the upstream copy changed the file since the last pull; `check --base` refuses changed headers, rewritten log lines and removed handoffs; no `merge=union` on ledgers |
| Acting on a stale copy | The colliding session hadn't fetched | **Read before write / read-your-writes**: refresh before deciding | `status.py` reports how far behind the copy is and says PULL FIRST: in kit |
| Delivered but never picked up | `sent` handoffs with no acknowledgement | **Acknowledgements with timeouts** (TCP retransmit, message-queue ack deadlines) | `status.py` flags NO ACK after `--stale-hours`: in kit. Who re-sends: open |
| Picked up, then abandoned | A claimed handoff with no progress | **Leases** with **fencing**: a claim expires unless renewed, and a stale holder's late write is rejected (Kleppmann on fencing tokens) | `status.py` flags STALE CLAIM; the shared-copy check and `--expect` reject the stale holder's write after the owner reassigns: in kit. Automatic expiry: open (the owner reassigns) |
| Done but never closed | Three handoffs with outputs filed, still `sent` (CH-010) | **Completion is part of the transaction**: the step that writes the result also records the commit (write-ahead log, two-phase commit's commit record) | Flagged by `status.py`; the skill now makes filing and recording `completed` one step, `--for` a recipient that can't write: in kit |
| Started without permission | Codex ran an unauthorized pilot | **Two-person rule, guarded states**: a transition is refused unless its precondition holds | `sent` refuses an unfilled draft; pickup refuses NOT AUTHORIZED: in kit and procedure |
| Who said what, in what order | Relayed text mistaken for the owner's | **Message ids and causal links** (Message-ID / In-Reply-To in email), **append-only event log** as the source of truth (event sourcing) | conversation@1 ids, `reply_to`, append-only logs and ledgers: in kit |
| Two copies of a rule drift apart | Skills on claude.ai and ChatGPT behind the repo | **Versioned interfaces and cache invalidation**: stamp the version, check it at use | Contract markers checked by tests: in kit. Uploaded skill copies carry no version check: open |
| Too much work in flight | More threads than the owner can follow | **WIP limits** (Kanban; Little's law: more in flight means each item waits longer) and **backpressure** | Thread budget: proposed |
| Checking your own work | A model agreeing with itself | **Independent verification** (IV&V, N-version programming): the checker doesn't share the builder's reasoning | P-05; a fresh-context review per task: proposed |
| Who did it | Two sessions signed as `claude:aec` (dogfood-001) | **Distinct principal ids** per process, not per role | In kit: actors carry a session (`claude:aec#01N4`) on handoff@2 and on new ledger lines; `status.py` shortens to a unique prefix |
| A failure nobody sees | Records CI red for 15 pushes (CH-012) | **Alerting with an owner and acknowledgement** (on-call paging) | In kit and records CI: each check runs independently; red on main opens an issue assigned to the owner and closes when green; `status.py` shows red and unacknowledged at the top |
| Two sessions on the same thing | Overlapping PRs on one file; two sessions writing to one coordination thread minutes apart (2026-11-01) | **Advisory locks and change notifications**: announce intent, warn on conflict, don't block (editor presence, `git` merge previews) | In kit: `status.py --touching` warns from handoffs' optional `touches:`, open PRs, recent sessions in the lane and failing checks. Nothing sees a message being typed into a chat |
| Who coordinates | One coordinator or none? | **Orchestration vs choreography**; **single writer per record** | Choreography over shared files, each state transition owned by sender, recipient or owner: in kit. No general coordinator by design |

## Deliberately not adopted

Consensus protocols (Paxos, Raft), CRDTs and distributed locks solve problems at a scale
this kit doesn't have: a few writers, one owner, and git as the shared store. The
owner arbitrates, and git's history is the log. Revisit if the number of writers or the
rate of writes grows enough that conflicts become routine.

MCP is now adopted for one narrow use: a read-only server over the records,
`relay-kit/mcp@2` ([mcp.md](mcp.md)), on stdio for clients that run local servers (Claude Code, Claude
Desktop, Codex) and on loopback Streamable HTTP behind an owner-run tunnel for ChatGPT. It reads;
it writes nothing and orchestrates nothing. Not adopted: MCP as a
write or coordination channel, a hosted server (DEC-150 decides that), MCP Tasks
as a stand-in for handoff states (handoffs carry authorization; tasks don't), and A2A-style task
exchange. Those protocols remain worth reading for their task-state and handoff vocabularies.
