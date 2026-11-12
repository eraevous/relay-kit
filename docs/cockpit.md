# Cockpit (`relay-kit/cockpit@8`)

One static page over your records: what waits on you, how the work is wired, what it costs, and
what has drifted. It is generated, not hand-kept: every number and line on it comes from the
records store, read with the same readers as [export](export.md).

    relay-kit cockpit --root path/to/records --out ~/cockpit [--lane L] [--content full|metadata]
                      [--weekly-tokens N] [--drift-days N] [--coordinator LANE] [--brand NAME] [--pr-state]
                      [--theme FILE] [--landing SURFACE] [--actions FILE]

It writes two files into `--out`:

- `index.html`: one self-contained page (inline CSS and JS; no fonts, scripts or images from
  anywhere else; a Content-Security-Policy that forbids loading any). It works offline, from a
  file, at phone width.
- `cockpit.json`: the data the page was built from (`"schema": "relay-kit/cockpit@8"`).

**@2** (HO-RK-20261105-VZH4) adds, on every choice: `ask`, `refs`, `waiting_on_it`, `cited_later_by`,
`prs`, `age_days`, `possibly_overtaken`, and `compose` entries `amend`, `overtaken`, `clarify` and
`inquire`; at the top level `questions`, `pr_state`, and `counts.questions_open` and
`counts.possibly_overtaken`. Nothing from @1 was removed or changed in meaning.

**@3** (HO-RK-20261105-SWK5) adds the **Pull requests** surface (below), `--theme` and `--landing`;
in `cockpit.json`, top-level `pull_requests` and `theme`, `query.landing` and `query.theme`, and
`counts.prs_open`. PR states for the "possibly overtaken" check now come from
`governance/pr-state.jsonl` when it holds the pull request; `--pr-state` only looks up the ones it
doesn't, and each state says where it came from. With `--pr-state`, gh is asked first (it is newer)
and the file answers the rest. Nothing from @2 was removed or changed in meaning. Without `--theme`
the page keeps its dark palette and layout; moving about a hundred literal colours onto thirty-odd
variables merged near-identical shades, so some text and borders differ slightly in tone.

**@4** (HO-RK-20261105-8DTW, CH-035 findings 1, 5 and 6) reads the ledgers through
[`relay-kit/lifecycle@1`](lifecycle.md), as status, export and the MCP server do: a proposal leaves
Choices only when a settled line closes it (a proposed replacement is shown on its card instead), a
challenge only when a resolved or accepted-risk line resolves it, and transitions that take no effect
or conflict are listed on Review. Every line it composes for a decision or challenge carries
`--expect ID=STATE@HASH`, so applying it after the records have moved on is refused with what changed
(below). The header shows the records' **revision, its commit time and when the clone last fetched**
(aged in your browser) apart from the **newest record**; outside a git repository it says the
revision is unknown; `cockpit.json` gains `snapshot`, `transitions` and, on a choice,
`proposed_replacements`. Nothing from @3 was removed.

**@5** (HO-RK-20261106-NXPR; DEC-139, DEC-140) adds three things the owner asked to read from the
page rather than from GitHub or the ledgers. **Reviews, folded under each pull request** (below):
author, family (self-identified, inferred from the login, or unsigned), kind, verdict, time, inline-comment
count and the body as written. **A Status surface** (11, a `--landing` target) rendering the newest
[status edition](status.md) verbatim. **Decision support on each choice** ([spec](decision-support.md)):
authored notes, one block per author, with the ledger mechanics moved behind "how the ledger records
this". Decision and challenge text is now rendered with its structure (enumerations and line breaks
become lists and paragraphs). `cockpit.json` gains `status`, `decision_support`, `choices[].support`,
on each review `kind`, `body`, `body_recorded`, `truncated`, `comments`, `family_source`, `findings`,
and `counts.status_editions`, `support_notes`, `choices_without_support`. Nothing from @4 was removed.

**@6** (HO-CKPT-20261106-D1C5, HO-CKPT-20261107-EBVA; DEC-141 run 1 items 7–11, DEC-151) is a
Topology layout iteration and an **Actions** surface. A fresh reader of the @5 Topology could not
attach the handoff counts to directions (they sat where edges crossed), so each handoff now runs from
a **dot on the sender's rim** to a **large arrowhead on the receiver's rim**, its count in a **pill
beside the arrowhead**; opposite directions between two actors bend to opposite sides; a handoff to
oneself is a **visible loop** above the node with its count; actors step across columns band by band
so edges leave and arrive at distinct angles; a lane with no line in threads.md is tagged **no
thread** on its band; and a **per-actor table** (sent, received, to itself, busiest counterpart each
way) beside or under the graph carries the same counts, so nobody has to add them off the drawing.
The **Actions** surface (12, a `--landing` target) renders the human action list
([`relay-kit/actions@1`](#actions-the-human-action-list)) as written. `cockpit.json` gains
`topology.information.actors`, `topology.information.lanes_without_thread`, `has_thread` on lane
nodes, top-level `actions`, and `counts.actions_open` / `actions_done`. Nothing from @5 was removed.

**@7** (HO-AEC-20261111-2CY6) adds a **Coordination** surface (13, a `--landing` target), built by
`tools/coordination.py`. Its captured-sessions half reads chat-telemetry's own output (the
`telemetry.json` its script writes) saved in the records under `telemetry/chat/*.json`; it re-measures
nothing. From the per-turn rows it draws a turn timeline across threads, the owner's reply gaps
(only gaps that end in a turn the owner typed; bands, and the median of gaps under 15 minutes),
turns by local hour, message length per turn, a per-thread table, hangs and the longest silences,
and governed vs autonomous (owner turns vs relayed turns, and stretches: an owner turn plus the
relayed turns after it in the same thread). Owner turns are a **lower bound** (the records can't
show every session was captured, CH-033), shown beside the ledgers' count of the owner's decision
and challenge lines in the same days. Raw captures in the records that no output covers are listed.
With no output, the half says unknown with the reason, never zero. The ledger half gathers
telemetry@2's `coordination_loss`, `idea_to_completed_handoff_latency` and `measurement_coverage`;
handoff stage times are shown as not derived (a new metric needs the DEC-038 review). Previews of the
owner's words are dropped. `cockpit.json` gains top-level `coordination`. Nothing from @6 was removed.

The page is deterministic: two runs over the same records give byte-identical files. It shows two
times, kept apart: the records' git revision and when it was committed (fixed for a revision; the
browser counts its age, so an old page shows as old), and the newest record's own timestamp. "Last
7 days" and "older than N days" are measured back from the newest record, never from today.
Uncommitted changes in the records are flagged beside the revision.

The layout follows the Cockpit v0.4 prototype (2026-11-04): a left rail of numbered surfaces,
the dark instrument-panel palette, and its vocabulary ("Inquiry is not authority. Capture is not
commitment."; "Settle this choice?"; "Move from Drift to Run?"). `--brand` sets the name in the
header (default `relay-kit`; or `brand` in `governance/cockpit.json`).

## Surfaces

| # | Surface | What it shows | Read from |
| --- | --- | --- | --- |
| 01 | Overview | Four dials (choices waiting, 7-day budget, open handoffs, drift flags), the first items waiting on you, the Drift ≠ Run distinction | everything below |
| 02 | Idea intake | **Not wired to records yet**: a tile saying so and the intake thread link(s), i.e. threads.md lines that mention "intake" | threads.md |
| 03 | Questions | Open questions asked on the record with Clarify: challenges with `"kind": "question"`, with what they're `about`, who they're `addressed_to`, who asked and when. Each leaves Choices, since it waits on whoever must answer, not on you. It stays here until a line `resolves` it | challenges.jsonl |
| 04 | Choices | Proposed decisions nobody has superseded (id, text, scope, proposer, decider, basis, locator, source line); handoffs that need you (prepared, Authorized not filled, addressed to you, or blocked); open challenges nobody has resolved. Accept / Reject / Defer on each | decisions.jsonl, challenges.jsonl, handoffs/ |
| 05 | Topology | Two SVG diagrams. **Information**: one row per lane; actors, threads from threads.md, conversation@1 captures; handoff flows sender (dot) → receiver (arrowhead) with the count beside the arrowhead, self-handoffs as loops, lanes without a thread tagged, and a per-actor sent/received table with the same counts (@6). **Authority**: only *recorded* delegation, i.e. an adopted, in-force decision with a `delegates_to` field (a lane name or code, or a list); if none has one, the surface says "No recorded delegation" and draws nothing. A separate, visually distinct **hypothesised authority (inferred)** overlay, closed by default, holds the assumed owner → coordinator → lane lines and the text-matched delegations (decision text mentions delegating or authorizing; target read from its scope). Nothing executable is derived from either graph | handoffs/, threads.md, captures, decisions.jsonl, .ledger.json |
| 06 | Budget control | Burn over the last 7 days against the weekly allowance, by lane and by actor; an advisory resource policy; a pre-flight estimator ("Should we spend on this question?") that runs in the page and records nothing | governance/budget.jsonl, governance/cockpit.json |
| 07 | Project lanes | One card per lane (from .ledger.json and threads.md): code, handoffs open/total, decisions and proposals, open challenges, threads, last activity | all |
| 08 | Run commitments | **Authorized & in progress** (sent, acknowledged or in_progress, with a filled Authorized section; the only handoffs the gauge counts) apart from **pending authorization** (prepared, or no authorization quote); all handoffs by status; PR links found in threads.md; the Drift panel: proposals older than `--drift-days` (default 3) and challenges that mention drift | handoffs/, threads.md, ledgers |
| 09 | Review | The last 20 decisions, newest first; open challenges | ledgers |
| 10 | Pull requests | Each pull request [`relay-kit prs`](prs.md) recorded: open first, then merged or closed recently. Its `## What` / `## Why` / `## How` / `## Authorized` sections **verbatim**; who wrote it (an `Author:` line naming anyone but the owner is tagged "written by …, not the owner"; no line says so, with the GitHub login that opened it); which sections are missing; **reviews folded under the card** with author, family, kind, verdict, time and body (below); whether a review comes from another family than the author; CI; when it was read. A table of repositories with their last read time, aged in your browser | governance/pr-state.jsonl, governance/cockpit.json, handoffs/.ledger.json |
| 11 | Status | The newest [status edition](status.md), rendered as written from a markdown subset: title, the four sections, lists, bold; links as text. Tagged "written by <author>, not the owner"; flagged stale when its date is more than `--drift-days` before the newest record; older editions in a collapsed index. Overview links to it | status/*.md |
| 12 | Actions | The human action list, as written: open items with since-when (and its age to the newest record), size and due date when the author stated them, done items folded, tagged NEEDS YOU; from `--actions FILE` or `actions/*.md` ([below](#actions-the-human-action-list)) | the action list file(s) |
| 13 | Coordination | Captured sessions (chat-telemetry output): turn timeline, owner reply gaps (wall clock, not effort), turns by local hour, message length per turn, per-thread table, hangs and longest silences, governed vs autonomous with stretches; owner turns as a lower bound beside the ledgers' owner lines; unknown with a reason when nothing is captured. Ledger half: telemetry@2 coordination_loss, idea-to-completed-handoff latency, measurement coverage | telemetry/chat/*.json, raw captures, decisions.jsonl, challenges.jsonl, telemetry@2 |

### Pull requests: words, not summaries

Nothing on the surface is summarised or generated. Section text is what the author wrote under
those headings, with HTML comments (template hints) removed; a missing section is shown as missing,
not filled in. A PR body follows the template in
[tools/templates/PULL_REQUEST_TEMPLATE.md](../tools/templates/PULL_REQUEST_TEMPLATE.md).

**Families.** A reviewer's or author's family comes from `review_families` in
`governance/cockpit.json` (`{"login or signed name": "family"}`, case-insensitive; the first word
of a signed name also matches), else from an actor's prefix (`claude:relay-kit#01AB` is `claude`).
A review can name its reviewer with a `Reviewer: <name>` line in its body; that matters when several
reviewers post from one account. "Other family" is yes when a review that approved, commented or
asked for changes comes from a login whose mapped family differs from the author's; "can't tell" when
a family is unknown, or when only a `Reviewer:` line claims another family. A `Reviewer:` line is the
reviewer's own word, not an authenticated identity (purpose P-01): it is shown as *self-identified*
with the GitHub login beside it, and the card says how many counted reviewers are self-identified
(`reviewers_self_identified`; `other_family_claimed: "self-identified"` when only a signature claims another family). The page states the facts; any rule about how many reviews are needed is yours.
`owner_names` in the same file lists other names that mean the owner in an `Author:` line. A line
counts as the owner's only when it is the owner's name alone, or with a note in brackets
("sam (from a call)"); "sam via claude:x" is an agent's.

**Reviews (@5).** Each review is folded under its card, in order of time: the reviewer (a `Reviewer:`
signature, else the login), the family, whether it is a GitHub review or a **review-style comment**
(an issue comment opening with `Review:` / `Execution review` / `Reviewer:` or stating a verdict;
`relay-kit prs@3` records those), the verdict (GitHub's, or "comment"), a verdict stated in the
body, the time, and how many inline comments it carries (their text stays on GitHub). The body is
shown as written: escaped, paragraphs and lists kept, no markdown executed, URLs as text and never
links. A line "Findings, as the review states them: N blocking / M should-fix" appears **only** when
the body says those words with a number; the page counts nothing itself. A review recorded by an
older `prs` (no body) says so rather than showing an empty one.

**Family, self-identified or inferred.** A family is *self-identified* (`family_source: "signed"`
in the JSON) when it comes from the review's own `Reviewer:` line. Otherwise it is taken from the login, through `review_families` in
`governance/cockpit.json` (first) or the optional `actors` map in `handoffs/.ledger.json`
(`{"login": "family"}` or `{"login": {"family": "…", "lanes": […]}}`), and shown as "inferred from
login". With neither, the review is "unsigned". Several people may post from one account, so an
inferred family is a guess the records made, never the page's.

**Freshness.** The page never fetches. Each card shows when its repository was last read and when
the pull request last changed; the source summary counts the last read's age in your browser (over a
day is marked). Rebuild after `relay-kit prs` for newer state. Merged or closed pull requests older
than the reader's `--since-days` window are counted, not listed.

**In `--content metadata`** the cards keep repository, number, state, which sections exist, review
logins, verdicts and CI. Titles, section text, the Author line, `Reviewer:` signatures, and any family
not taken from `review_families` by login are left out.

## Themes and landing

`--landing SURFACE` (or `"landing"` in `governance/cockpit.json`) opens the page on another surface,
e.g. `prs` or `status`. A `#surface` in the address still wins.

`--theme FILE` restyles the page from a JSON file:

```json
{"name": "my-theme", "default": "auto",
 "dark":  {"bg": "#0b1015", "teal": "#9bd5c8", "mark": "#e5a45d", "heading": "Georgia, serif"},
 "light": {"bg": "#f3f1ea", "text": "#182832"}}
```

Keys are the page's own colour variables (without `--`): `bg sunk panel panel-2 field field-hover
border border-strong border-hover line text text-2 muted soft teal teal-hi on-teal teal-bg teal-deep
gold gold-deep gold-bg purple purple-deep purple-bg coral blue blue-bg ok mark mark-word focus`, and
the font lists `display mono heading`. Colours are `#hex` or `var(--another)`; font lists are
names, commas and quotes. Anything else is refused, so a theme cannot carry markup or other CSS
into the page. `mark` and `mark-word` colour the brand mark and name. Fonts are named, never
loaded: the page fetches nothing, so a font that isn't installed falls back to the next in the list.

A `light` block adds a light scheme over a neutral light palette (a theme names only what it
changes): it follows the system setting, and a **Theme** button in the header cycles auto / light /
dark, remembered in this browser only. `default` (`auto`, `dark` or `light`) is where it starts.
Without `--theme` the page is the dark palette above, with no light scheme and no button.

### Choices compose; you apply (DEC-019)

The buttons write nothing. Accept and Reject ask "Settle this choice?" and then put a line into
the **To apply** box; Defer puts a comment there. **The owner supplies the justification**: each
line carries `<<REQUIRED: ...>>` placeholders for the decider, the basis (your words) and the
locator (where you said them), or for a challenge's resolution or a handoff note. The page fills a
placeholder only from what you type on the card (the decider, which it remembers in this browser; the
locator; your words, which it never defaults), and never sets `recorded_by` (ledger_append takes it from
`RELAYKIT_ACTOR`; handoff.py takes the actor the same way). `check_ledgers.py` refuses any line
still holding `<<REQUIRED`, so a line pasted unedited is taken back out by ledger_append. Replace
the placeholders, copy, and run the lines in your records with `RELAYKIT_ACTOR=<owner>`:

| Item | Accept | Reject | Defer |
| --- | --- | --- | --- |
| proposed decision | `relay-kit ledger append --dir governance decision '{"status":"adopted","supersedes":"DEC-N",...}'` | same with `"status":"rejected"` | `# DEC-N: deferred ...` (nothing to append) |
| open challenge | `... challenge '{"status":"accepted-risk","resolves":"CH-N",...}'` | `... '{"status":"resolved","resolves":"CH-N",...}'` | comment |
| handoff | `relay-kit handoff --dir handoffs set HO-... <next>`, after you fill its Authorized section | `... set HO-... cancelled` | comment |

**Applying from an old page.** Decision and challenge lines carry `--expect DEC-N=open@<hash>`
(the item's state when the page was built, and the first 12 hex digits of the sha256 of its line's
bytes as stored). A proposal that proposes to replace another also carries `--expect` for that one,
so its replacement can't be adopted after the target was settled another way.
`ledger_append` checks it against the records you are appending to and, if the item has since been
settled or its line changed, refuses, appends nothing, and lists the later lines that mention it.
Handoff lines already carry `--expect` for their state.

### Decision support, then mechanics (DEC-140)

Under the question, each choice shows the [decision-support notes](decision-support.md) written for
it: one block per author (independent authors first, then the owner, then the coordinator lane; a
reading order, not a ranking), each a labelled list: What changes / What doesn't / If nothing is done
/ Can it wait / Depends on it / Recommendation, with that author's earlier revisions folded. An item
with none says "no decision-support note yet". What each action does to the ledger, the page's own
description of the composed lines, now sits behind "how the ledger records this".

Decision text, challenge concerns and the notes are rendered with the structure their authors wrote:
`(1) … (2) …` enumerations, lines starting with `1.` or `-`, and line breaks become lists and
paragraphs. Records are not markdown: `**` and backticks stay as typed, and everything is escaped.

### So what is this asking?

Every choice card opens with a block, above the raw record, that answers what you're being asked:

- **The question**, in one sentence ending in "?".
- **What each action does**, as a consequence ("Accept → an adopted line supersedes DEC-N: it is
  in force…"), and what happens if nobody answers. This is always the page's own description of the
  lines its buttons compose. A proposer can't redefine it.
- **What waits on it** (open handoffs, proposed decisions and open challenges that cite it), its
  **age** in days back from the records' as-of date, the ids it cites, and the pull requests it names.
- **Whose words these are.** A proposer can write the question with an `ask` field on the proposal
  (see below). The block then says "question written by <proposer>", and shows their explanation of
  the options, with their default, in the open right under the question and before the authored
  notes, under its own heading ("the proposer's explanation of the options: their words, not what the
  buttons do"). The alternatives and their claimed consequences are substance, so they are never
  behind the mechanics disclosure; only what the buttons compose, and the page's own
  if-nobody-answers, sit there. Otherwise the page derives a question from the record and says so: "derived by this page from
  the record, not written by the proposer". With `--content metadata` the proposer's `ask` is free
  text, so it's left out like the rest, and the question is derived from ids only.

Write `ask` when you propose. `check_ledgers.py` checks it when it's an object: a question ending
in "?", at least one `{label, effect}` option, and `default_if_unanswered`.

    relay-kit ledger append --dir governance decision '{"status": "proposed", "decision": "...", "scope": "...",
      "ask": {"question": "Run the survey weekly instead of monthly?",
              "options": [{"label": "Accept", "effect": "the survey runs every Monday"},
                          {"label": "Reject", "effect": "it stays monthly"}],
              "default_if_unanswered": "it stays monthly"}}'

### Possibly overtaken

A card is flagged **possibly overtaken**, with its reasons, only on strong evidence:
- a later, settled record that cites it says, near its id, that it closed, replaced, amended or carried
  it out ("overtaken", "supersedes", "closes", "amended", "merged", "already", "executed", "carried
  out" and similar). Only that record's text is searched, within the field that names the id: never
  field names, and not when a negation comes just before ("not done", "keep it open"). The flag
  quotes the words it found, so you can judge it. A settled record is a decision that isn't
  proposed, a challenge that isn't open, or a handoff that has finished or been cancelled. "Later"
  means a higher id in the same ledger, otherwise a later day.
- with `--pr-state`, a pull request it names is merged or closed.

Weaker evidence is listed but not flagged: other later citations ("cited later by"), and open records
that cite it ("waiting on it"), including an open Clarify question about it. Records settled since
don't appear in either list. Answering a question about DEC-N doesn't overtake DEC-N.
The flag is a prompt to check, not a verdict.

A flagged card offers **Close as overtaken**. It composes a closing line that adopts nothing: a
`"status": "superseded"` line that supersedes a decision, or a resolved line for a challenge, with
`<<REQUIRED>>` space for what overtook it.

Pull requests a choice names are always listed. Looking up their state needs `--pr-state`: opt-in, at
build time only, and only with full content. It runs `gh api` for each pull request named in a choice's text: a full `https://github.com/<owner>/<repo>/pull/N` URL, or "<repo> PR #N" where the
records spell `<repo>` out as `<owner>/<repo>` somewhere (bare "#N" is ignored as ambiguous). The
owner is the GitHub owner of the records' origin. The page and `cockpit.json` record when the
states were looked up (`pr_state.fetched_at`), the one clock time the output can hold, so a page
built with it isn't byte-identical between runs. Without
the flag nothing touches the network, and the page never does.

### Amend, Clarify, Inquire

Beside Accept / Reject / Defer:

| Action | On the record? | What it composes |
| --- | --- | --- |
| **Amend** (decisions) | yes: your decision | Opens an editor prefilled with the proposal's text. Your edited text goes into an adopted line that supersedes the proposal (`"amends"` names it, for the reader), with the usual decider / basis / locator placeholders. |
| **Clarify** | yes: a question on the record | Opens an editor for your question. Composes an open challenge with `"kind": "question"`, `"about"` the item and `"addressed_to"` its proposer (a handoff's sender, for a handoff). Once you run it, it leaves Choices and shows under Questions until a line resolves it, with the answer as the resolution. **Recording it doesn't deliver it**: nothing notifies the addressee, so tell them, or send a handoff, if you need the answer. A challenge rather than a handoff note because a question about a decision belongs next to it in the ledger, and the answer closes it the way any challenge closes. A handoff remains the tool for work someone must do. |
| **Inquire** | no | Copies a prompt for you to paste to an assistant ("Explain DEC-N from my records: what is being decided, what has changed since it was proposed, and the consequence of each option. Cite record ids…"). Nothing is written and nobody owes anything; the answer is information, not a decision. |

Clarify and Inquire look different on purpose: Clarify is a solid button marked "on record", and
Inquire sits apart under a dashed line, marked "off record". All of them, like Accept, write nothing:
they compose into To apply (Inquire into your clipboard), and you run the line yourself.

The handoff's `<next>` follows handoff.py's rules: prepared → sent, sent → acknowledged, acknowledged
→ in_progress, in_progress → completed (with a reminder to write `handoffs/outputs/HO-....md` first,
since handoff.py refuses completed without it), and blocked → the state it was blocked from, read from
its log.

**Your edits in To apply are kept.** Until you type in the box, each choice rebuilds it from the picks.
Once you have edited it, a new choice is added at the end (or replaces that item's earlier line if you
left that line untouched), and Clear asks before discarding your text.

`ledger_append.py` fills the id, date and recorded_by and runs the ledger check, which refuses an
adopted line without decider, basis and locator, and any line with a `<<REQUIRED` placeholder.
(handoff.py does not check notes for placeholders; the note is yours to write.)

## Actions: the human action list

`relay-kit/actions@1` (DEC-151; pilot form `kairos/actions-zach.md`). A markdown file kept by hand:

- `- [ ] …` is an open item, `- [x] …` (or a line struck through with `~~…~~`) a done one; the
  heading above a line is its section; the file's `#` title and any prose before the first item are
  shown above the list.
- Inside a line, `Since YYYY-MM-DD`, `Due YYYY-MM-DD` and a size (`~15 min`, `2 min`, `1 h`,
  `3 days`, `one word each`) are read out **only when the author wrote them**; otherwise the page says
  "since not stated" / "size not stated". Age is counted from `Since` to the newest record, as every
  other age on the page; a due date within seven days is marked.
- Items are appended and ticked, never deleted. The page ticks nothing, ranks nothing and writes
  nothing; the line itself is rendered through the same inline markdown subset as the narrative
  (bold, italic, code; links as text; everything escaped).
- `--actions FILE` renders one file (named by its path inside the records, or its file name; never an
  absolute path); without it every `actions/*.md` in the records store is rendered, one block each.
  In `--content metadata` the states, dates, sizes and sections stay and the words are left out.

## Budget records: `governance/budget.jsonl`

The format is its own contract, [`relay-kit/budget@2`](budget.md). The cockpit reads it through
`tools/budget.py`: a corrected line and a child line its parent already includes are never counted
again, and the burn is shown split by method (measured, proxy, estimate, or unlabelled for older
lines). The gauge, burn and by-lane / by-actor tables are the sum of the counted `session` lines in
the 7 days ending at the newest record; a `weekly` line is shown separately as a reference, never
added. With no session lines in the window, burn is "no data", not 0. The allowance comes from
`--weekly-tokens N`, else `weekly_tokens` in `governance/cockpit.json`. **The coordinator's
status-file cost lines are not parsed**; only this file counts.

`governance/cockpit.json` (optional, read-only to the cockpit):

```json
{"weekly_tokens": 5000000, "drift_days": 3, "brand": "Kairos",
 "policy": {"default_effort": "economy-first", "spawn_route": "coordinator only",
            "max_delegations_per_question": 2, "claude_build_context_cap": 4}}
```

`policy` is shown as the **advisory** resource policy (keys: `default_effort`, `spawn_route`,
`weekly_tokens_soft`, `max_delegations_per_question`, `chatgpt_standing_thread_cap`,
`claude_build_context_cap`, `codex_repo_context_cap`, `human_interruptions_per_day`). Unset means
unknown, not unlimited. Nothing enforces it.

## What it never does

- Write to the records, the ledgers, the handoffs or any capture. It reads them and writes only
  the two files in `--out`.
- Fetch anything from the page: no GitHub calls, no provider usage, no fonts. At build time,
  `--pr-state` (opt-in) asks gh about pull requests choices name, and the destination guard below
  may ask GitHub whether a repository is public, as export does. Pull-request data otherwise comes
  from `governance/pr-state.jsonl`, which `relay-kit prs` writes.
- Decide: the authority diagram shows only recorded delegation (`delegates_to`); text-matched
  edges and the owner → coordinator → lane lines live in a closed overlay labelled hypothesised.
  Nothing executable (permissions, routing, commands) is derived from either graph.
- Supply a justification: decider, basis and locator are always left for the owner.
- Show idea intake it can't read: that surface says "not wired to records yet" instead of showing
  placeholder content. (Questions is wired since @2: open challenges with `"kind": "question"`.)

## `--content metadata`

Leaves out decision text, scope, basis and locator, challenge concerns, handoff titles and bodies,
capture titles and budget notes, in the page and in `cockpit.json`. Ids, statuses, dates, actors,
lanes and counts remain. Composed lines then say "As proposed in DEC-N" instead of copying the text.
It is redaction of content, not anonymisation and not publication safety. What remains in
metadata mode: thread URLs (intake links, PR links), session ids inside actor names, actor and
owner names, lane names and codes, handoff and decision ids, dates, source file paths
(`governance/decisions.jsonl:12`, `handoffs/HO-...md`) and counts.

## Privacy and deploying

The generated page's Content-Security-Policy allows only the SHA-256 hashes of its own static
scripts, blocks base-URL overrides and form submissions, and denies external resource loads.
Inline styles remain allowed for the existing layout. This is defense in depth, not a claim of
an exploited script-injection bug or a replacement for authentication. The generator and the
stdio MCP transport open no HTTP listener. Open the page locally with `file://`; do not expose
records or your workspace through a general-purpose development server. If you deploy the page,
configure authentication for both output files and anti-framing headers at the server: a
`frame-ancestors` directive in a meta policy would not be enforced by browsers.


**The page holds your records' text by default.** It refuses to write inside the records or inside
a git repository that is public or whose visibility it can't confirm (the export's guard;
`--allow-public-dest` overrides). It sets `noindex` and `no-referrer`, but **`noindex` is not
access control**. Serving the folder from a domain requires authentication on **both**
`index.html` and `cockpit.json` (the JSON holds everything the page shows). `--content metadata`
reduces what is exposed (see above); it does not make the folder safe to publish.

In both recipes access control is on, and checked, **before** any records go up.

AWS S3 (private bucket; serve it through CloudFront with access control, or open it with a
presigned link):

    # all four settings must be true before anything is synced
    aws s3api get-public-access-block --bucket YOUR-PRIVATE-BUCKET
    relay-kit cockpit --root ~/records --out ~/cockpit --brand Kairos
    aws s3 sync ~/cockpit s3://YOUR-PRIVATE-BUCKET/cockpit/ --delete
    aws s3 presign s3://YOUR-PRIVATE-BUCKET/cockpit/index.html --expires-in 3600

AWS Amplify Hosting (password protection is set per branch, so the branch has to exist first;
give it a placeholder that holds no records):

    # 1. a placeholder, no records in it
    mkdir -p ~/cockpit-placeholder && printf '<!doctype html><title>placeholder</title>\n' > ~/cockpit-placeholder/index.html
    (cd ~/cockpit-placeholder && zip -r ../placeholder.zip .)
    # Amplify console > Deploy without Git > drag placeholder.zip
    # 2. App settings > Access control > Manage access: password-protect the branch
    # 3. verify: both paths must answer 401 without the password
    curl -s -o /dev/null -w '%{http_code}\n' https://BRANCH.APP-ID.amplifyapp.com/index.html
    curl -s -o /dev/null -w '%{http_code}\n' https://BRANCH.APP-ID.amplifyapp.com/cockpit.json
    # 4. only then build and upload the records
    relay-kit cockpit --root ~/records --out ~/cockpit && (cd ~/cockpit && zip -r ../cockpit.zip .)
    # Amplify console > the same branch > Deploy updates > drag cockpit.zip

Rebuild and redeploy whenever the records change; the page never updates itself.

## Restoration candidates from v0.4

Parts of the v0.4 prototype that were dropped because the records can't feed them yet. Each needs a
record format (and a decision) before it can come back:

- **Choices lifecycle**: Open / Leaning / Settled / Reconsider / Deferred, with alternatives,
  evidence, objections, uncertainty and consequences per choice (the ledger has proposed /
  adopted / rejected / superseded only).
- **Provenance chain**: Source → Evidence → Interpretation → Assertion → Decision, so a decision
  resolves to what it rests on ("Why did we choose this?").
- **Budget profiles**: soft and hard limits per resource, effort profiles, and attention budgeting
  (human interruptions per day), beyond the advisory caps shown now.
- **Browser-local capture and JSON import/export**: Idea intake and Question stewards as local
  notebooks with export to the records, instead of the "not wired to records yet" tiles.

## Drafts waiting on you (`cockpit@8`, DEC-173)

Every open draft's card on Pull requests carries one line: **draft waiting on you** / **draft waiting on
a lane / review** / **draft parked**, the `relay-kit/prs@4` rule that fired (W1–W4, L1–L3, P0; see
[prs.md](prs.md)) and its one-line why, quoted from the records. The page judges nothing: the rules are
fixed text patterns over `pr-state.jsonl`, and the rule is shown so a wrong call can be argued. `cockpit.json`
gains `pull_requests.items[].waiting`, `pull_requests.drafts_waiting_on_owner` (repo, number, url, title,
rule, why), `pull_requests.draft_rules`, `counts.drafts_open` and `counts.drafts_waiting_on_owner`; the
morning status edition's "Drafts waiting on you: N" is read from that count. In `--content metadata` each wait keeps its bucket and rule and leaves out the why, which quotes record words. This was built as
`cockpit@7` in relay-kit #43 and renumbered `@8` when relay-kit #54 shipped the Coordination surface as `@7`.

**On Actions** (DEC-173 item 2): a "Drafts waiting on you" block sits above the hand-kept list. It lists
`pull_requests.drafts_waiting_on_owner` only: the PR link, its title (full mode), the rule that fired with its why, and
what that rule asks of you ("Needs: …"). Parked drafts and drafts waiting on a lane stay off it; they are on Pull
requests. With no `pr-state.jsonl` the block says it cannot tell which drafts wait on you, rather than "none". When the newest read failed, was partial or was cut short, or an open draft's comments or reviews went unread or were never recorded (a pre-prs@4 line), the count is shown as `N+` with "INCOMPLETE" and the reasons, never as a clean number (P-06). The block
adds no field to `cockpit.json` and no rule of its own; the contract stays `cockpit@8`.

