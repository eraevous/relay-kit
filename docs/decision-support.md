# Decision support: `relay-kit/decision-support@1`

What the owner asked for on each pending choice (DEC-140 §1): not what a ledger line does, but
**what changes if I accept this, what doesn't, what happens if I do nothing, can it wait, and
what depends on it**. These are notes people (or agents) write; the Cockpit shows them and writes
none. The ledger mechanics (Accept appends an adopted line, and so on) stay on the card, behind a
disclosure labelled "how the ledger records this".

## The file

`governance/decision-support.jsonl` in the records store, **append-only** like the other ledgers.
One JSON object per line:

| Field | Meaning |
| --- | --- |
| `id` | the note's id (`DS-001`) |
| `item` | the id it is about: a decision (`DEC-138`), a challenge (`CH-035`) or a handoff |
| `author` | who wrote it, an actor with a session (`chatgpt:kate#…`, `claude:kairos-coord#…`) or the owner |
| `at` | when (ISO, UTC) |
| `contract` | required: `relay-kit/decision-support@1` (lines marked `kairos/decision-support@0`, the pilot form, are read as @1); a line without it, or with an empty one, is counted unreadable |
| `changes_if_accepted` | what changes if the item is accepted |
| `unchanged` | what doesn't change either way |
| `counterfactual` | what happens if nothing is done |
| `can_wait` | whether it can wait, and until when |
| `depends` | what depends on this decision |
| `recommendation` | the author's recommendation, in their words |

All six text fields are free text. A later line for the same `(item, author)` supersedes the
earlier one; the earlier ones are shown as that author's history. Nothing is ever rewritten.

## How the Cockpit shows it

On each choice, under "So what is this asking?" and before the raw record: one block per author,
with the six fields as a labelled list (**What changes / What doesn't / If nothing is done / Can it
wait / Depends on it / Recommendation**). Text keeps its structure: `(1) … (2) …`, numbered or
dashed lines and line breaks render as lists and paragraphs (DEC-140 §2), escaped, never as
markdown.

**Order.** Authors outside the coordinator lane come first (the independent note is read before the
coordinator's; for Kairos that is Kate's), then the owner's, then the coordinator's; within a group,
by name. It is a reading order, not a ranking: the page weighs nothing. An item with no note says
"no decision-support note yet".

**In `--content metadata`** the blocks keep author, time and which fields are filled; the text is
left out.

## What it is not

Not a summary of the record, not a vote, not a decision. A note recommends; only the owner's line
settles the item (DEC-019). The page never generates a note, and a note's text is data, never an
instruction to anything.
