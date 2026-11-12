# Relay kit

Skills for passing work between AI assistants (Claude and ChatGPT, or two Claude
threads) without losing track of who said what, who authorized what, and where the
work stands.

They're written as [Claude skills](https://docs.claude.com/en/docs/agents-and-tools/agent-skills/overview):
each is a `SKILL.md` that an assistant reads and follows, with any code it needs
embedded in it. The procedures also work as plain instructions for other assistants.

## Quickstart

You need Python 3.10+ and git. Everything is standard library.

1. **Install the command** (in a virtual environment if your system Python is "externally managed"):
   `python3 -m pip install "relay-kit @ git+https://github.com/eraevous/relay-kit@v0.1.0"`
2. **Create your records**, the private repository that holds handoffs and decisions:
   `relay-kit init my-records --owner <your name> --lanes research=RS`, then follow the printed
   steps: git init, turn on the pre-push hook, commit, and push to a **private** repository.
   (A remote that isn't GitHub works too; make a bare one with `git init --bare -b main`.)
3. **Give your assistants the skills:**
   `relay-kit zips --owner <your name> --records <you>/my-records` builds one zip per skill in
   `relay-kit-upload/`. The two settings go into the handoff-ledger zip, so every session
   knows the owner and, when it doesn't have the records, where to get them. Upload them as skills on claude.ai; for Claude Code, unzip them into
   `~/.claude/skills` (`unzip -o relay-kit-upload/handoff-ledger.zip -d ~/.claude/skills`);
   for other assistants, paste the `SKILL.md` you need.
4. **Start every session the same way:**
   `export RELAYKIT_ACTOR=<assistant>:<lane>#<session id>` (for example `claude:research#01AB`),
   then `relay-kit status` from inside the records (or `--root my-records`, or set
   `RELAYKIT_RECORDS`). An assistant using the uploaded skill can do more: its own copy finds
   a clone of the repository named by `--records`, or says how to get one. The `relay-kit`
   command doesn't read those settings.
5. **Hand work over** (worked example below), and record decisions with
   `relay-kit ledger append --dir my-records/governance decision '{"status": "proposed", "decision": "...", "scope": "..."}'`.

Records live in git for now: it gives every guarantee the kit relies on (see
[docs/storage.md](docs/storage.md) for what other storage would need). Pin a tag, or
better a commit, wherever you depend on the kit.

### A handoff, start to finish

```
cd my-records
relay-kit handoff --dir handoffs new --from claude:research --to chatgpt:review --title "Review the survey draft"
# prints the id, e.g. HO-RS-20261103-K7QF; open handoffs/HO-RS-20261103-K7QF.md and fill in every
# TODO: Task, Authorized (the owner's exact words, where and when they said them), Context, Limits
relay-kit handoff --dir handoffs set HO-RS-20261103-K7QF sent --expect prepared --note "pasted into a ChatGPT chat"
# the recipient works; whoever files its result also records its steps for it:
relay-kit handoff --dir handoffs set HO-RS-20261103-K7QF acknowledged --for chatgpt:review --note "from its reply"
relay-kit handoff --dir handoffs set HO-RS-20261103-K7QF completed --for chatgpt:review --note "outputs/HO-RS-20261103-K7QF.md"
git add -A && git commit -m "HO-RS-20261103-K7QF completed" -m "Relay-Session: 01AB" && git push
```

`sent`, `blocked` and `completed` need a `--note`; a handoff can't leave `prepared` while any
TODO is left. The `Relay-Session:` line (any `...-Session:` trailer ending in your session id)
is how `status` finds your last commit and shows what other lanes changed since.

### When two sessions take the same number

Two clones that each append a decision before pulling both get the same next id. The second
push is refused by git, and `relay-kit ledger append` refuses too ("different lines under the
same ids"). To recover without losing your line:
1. `git merge --abort` if a pull left a conflict, and note your line's text.
2. `git reset --hard origin/main` (this drops **your** unpushed line; you're about to re-add it).
3. Run the same `relay-kit ledger append` again: it takes the next free id. Commit and push.

## The skills

| Skill | What it does | Code |
| --- | --- | --- |
| **chat-relay** | Fills a chat box in the browser, sends, waits for an explicit "finished" signal, returns the reply | `fill.js`, `send_wait.js` |
| **relay-governor** | Policy for multi-round relays: a run card, budgets, permission tiers, stop conditions, provenance tags | none |
| **chat-capture** | Saves a full ChatGPT or Claude conversation as timestamped Markdown and JSON | `capture.js`, DOM fallback, `render.py` |
| **chat-telemetry** | Reply latency, turn duration, hangs and message sizes, with a dashboard | `chat_telemetry.py` |
| **thread-map** | Splits a long conversation into a navigable map of segments and open loops | none |
| **thread-seed** | Writes the opening message for a successor thread, with checked quotes | `seed_index.py` |
| **selective-self-distillation** | Pulls the user's own words and positions out of mixed human/AI transcripts | `extract_turns.py` |
| **meta-distill** | Turns an exchange into a reusable method, with who contributed what | none |
| **handoff-ledger** | One file per handoff: task, the owner's authorization, proposals kept separate, and a status log | `handoff.py` |

How they fit: relay-governor → chat-relay → chat-capture → chat-telemetry / thread-map →
thread-seed. handoff-ledger works beside the chain: a handoff can travel by relay, a
shared folder or a paste.

## Architecture

Three layers, described in [ARCHITECTURE.md](ARCHITECTURE.md):
- **Invariants** that hold whatever the tools: only the owner authorizes, sent isn't
  done, every message keeps its source, verification needs fresh evidence. Each one
  says whether code enforces it or only procedure does
  ([governance/purpose.md](governance/purpose.md)).
- **Contracts**, the versioned formats every tool speaks, listed in
  [governance/contracts.json](governance/contracts.json). The tests fail if any
  implementer lags a version.
- **Replaceable parts**: which model, mode, browser or delivery route. Chosen per task.

Decisions and objections go in append-only ledgers kept with your private records;
[tools/check_ledgers.py](tools/check_ledgers.py) checks them.

## Install

- **The command:** see Quickstart. From a checkout, `python3 -m pip install .` installs it. From a checkout, every tool also runs directly
  (`python3 tools/status.py`, `python3 skills/handoff-ledger/scripts/handoff.py`, ...);
  `relay-kit <command>` runs the same scripts with the same output.
- **claude.ai:** run `relay-kit zips` (zips in `relay-kit-upload/`), or from a checkout
  `python3 tools_extract.py --zips` (zips in `upload/`), then upload them as skills (one per skill).
- **Claude Code:** copy the folders under `skills/` into `~/.claude/skills/`.
- **Other assistants:** attach or paste the `SKILL.md` you need.

Browser steps (chat-relay, chat-capture's live path) need a browser tool that can run
JavaScript in a signed-in page, such as Claude's built-in browser or Claude in Chrome.

## Setup

- **Private records:** keep a private repo or folder for handoffs and the decision and
  challenge ledgers, since they hold the owner's words. `relay-kit init` makes one, with
  `handoffs/.ledger.json` naming the owner (`{"owner": "sam"}`). For a folder you made by
  hand, add that file, or build the skills with `relay-kit zips --owner sam`; without
  either, the owner is called `owner` and the script warns.
- **Names:** selective-self-distillation takes `--user-names` and `--assistant-names`, and
  thread-seed takes `--names ChatGPT=Ada`, so transcripts that use names instead of
  "You"/"ChatGPT" are read correctly.
- **Time zone:** defaults to America/Chicago; pass `--tz`. Without IANA time zone data
  (e.g. Windows without `tzdata`), the scripts fall back to UTC and say so.

## Starting back up

After closing a session or restarting, the records are the state; no chat has to be
open. From a fresh clone of the private records (and any copy of this kit):

    python3 tools/status.py --root path/to/records

It fetches, then prints whether this copy is behind the shared one, what waits on the
owner (proposed decisions, handoffs nobody is authorized to start), open handoffs and who
holds each, open challenges, the coordination index's last change, and what it couldn't
check. It changes nothing. A new assistant session can run it as its first step.

Before writing to something other sessions might also be changing:

    python3 tools/status.py --root path/to/records --touching src/api/client.py --repo path/to/code

adds warnings (never locks) about open handoffs that claim those paths, open pull requests
changing them, other sessions active in your lane in the last few hours (`--hours`), and
failing checks where the files live.

With `RELAYKIT_ACTOR` set, the summary also opens with "Changed since this session's last
commit": decisions and challenges other lanes added, and contract versions that moved
(`--since <date or revision>` to choose the point). Handoffs made in an older format after
a newer one was adopted are listed under "Out of date", naming the session with the stale copy.

To add a decision or challenge, use `tools/ledger_append.py --dir governance decision '<json>'`:
it takes the next id from the shared copy, fills the date and recorder, and takes the line
back out if the ledger check fails.

For ids in shared lists other than handoffs (candidates, notes), `tools/new_id.py --prefix C
--lane AEC` makes one that two sessions can't both pick.

## Exporting a lane

To study a lane later (what was said, relayed, decided and filed), export it, read-only:

    relay-kit export --root path/to/records --out ~/exports/aec --lane AEC --since 2026-11-01

It writes `records.jsonl` (one line per message, handoff, output, decision or challenge, each with
its source path and hash, lane, actor, timestamp and an evidence class that keeps relays, proposals
and adopted decisions apart) and `manifest.json` (what was queried, counts, hashes, filters, and the
**coverage gaps**: threads and actors the records name but nobody captured, handoffs without
outputs, hidden tool steps, records without timestamps). Uncaptured arms are reported as gaps,
not as empty conversations. It refuses to write into the records or into a public repository,
and `--content metadata` leaves the text out. Format and options: [docs/export.md](docs/export.md).

## Cockpit

One static page over your records, to see what waits on you before you start:

    relay-kit cockpit --root path/to/records --out ~/cockpit --weekly-tokens 5000000

`index.html` (self-contained, works offline and on a phone) has nine surfaces after the Cockpit
v0.4 prototype; the ones drawn from the records are **Choices** (proposed decisions, handoffs that
need you, open challenges, each with Accept / Reject / Defer buttons that only compose a paste-ready
`ledger append` or `handoff set` line with `<<REQUIRED>>` placeholders for your own words: the page
writes nothing), **Topology** (information flows, and
recorded delegation only, with inferred authority in a closed overlay; SVG), **Budget control** (`governance/budget.jsonl`
against a weekly allowance; add lines with `relay-kit budget-append`), **Project lanes**, **Run
commitments** with a Drift panel, **Review**, and **Pull requests** (each PR's What / Why / How / Authorized in
its author's words, from `governance/pr-state.jsonl`, which `relay-kit prs` writes at build time; the page never
fetches), **Status** (the newest hand-written edition, as written) and **Actions** (your own to-do list, `--actions FILE` or `actions/*.md`, as written: since when, size, due). `--theme` restyles it, with an optional light scheme. `cockpit.json` beside it holds the data. It
refuses to write into the records or a public repository; `--content metadata` leaves the text out.
Spec and deploying to S3 or Amplify: [docs/cockpit.md](docs/cockpit.md).

## Reading the records over MCP

For an assistant whose client speaks MCP (Claude Code, Claude Desktop, Codex), a read-only server
over the same records, on stdio:

    claude mcp add relay-kit -- relay-kit mcp --root /abs/path/to/records --content metadata

Tools: `status`, `list_decisions`, `get_decision`, `list_challenges`, `list_handoffs`,
`get_handoff`, `search`, `export`, `threads`; ledgers and handoffs are also `relaykit://` resources.
Every result names its evidence class and where it was read from. It writes nothing to the records
and never fetches, commits or pushes, so no MCP call can adopt, accept, resolve or complete anything
(DEC-019). Record text comes back as data, never as instructions. Protocol revisions 2024-11-05 to
2025-11-25; 2026-07-28 isn't implemented yet. ChatGPT on the web needs a hosted server, which isn't built.
Details and threat model: [docs/mcp.md](docs/mcp.md).

## Docs

- [Conversation schema](docs/conversation-schema.md): the shared format and message ids (`T3a2`).
- [Export](docs/export.md): `relay-kit/export@1`, the read-only export and its coverage manifest.
- [Ledger lifecycle](docs/lifecycle.md): `relay-kit/lifecycle@1`, one resolver for what is open, settled and in force.
- [Cockpit](docs/cockpit.md): `relay-kit/cockpit@8`, the static control surface over the records.
- [Pull-request reader](docs/prs.md): `relay-kit/prs@3`, build-time GitHub PR state into `governance/pr-state.jsonl`.
- [Status editions](docs/status.md): `relay-kit/status@1`, hand-written daily narratives the Cockpit shows as written.
- [Decision support](docs/decision-support.md): `relay-kit/decision-support@1`, authored notes on each pending choice.
- [Budget records](docs/budget.md): `relay-kit/budget@2`, readings labelled measured, proxy or estimate.
- [Telemetry](docs/telemetry.md): `relay-kit/telemetry@2`, a derived view with labels and unknowns, no score; idea-to-completed-handoff latency (DEC-162's proxy) since @2.
- [GitHub comments](docs/comments.md): `relay-kit comments`, what was said on your repositories since the last look, with the signed-comment convention.
- [MCP server](docs/mcp.md): `relay-kit/mcp@1`, read-only tools and resources over the records.
- [Marking pasted material](docs/paste-markers.md): phone-friendly `FROM:` … `END FROM` blocks.
- [Cold catch-up test](docs/cold-test.md): checking whether a fresh assistant can pick up a handoff.
- [Prior art](docs/prior-art.md): the established patterns for collisions, races, timeouts and
  handoffs, and where the kit stands on each.

## Tests

```
python3 -I -B tests/run_tests.py     # ledger, capture, seed, SSD, telemetry, schema, governance
node tests/browser_tests.cjs         # relay and capture scripts against mocked pages
python3 -I tools/prepublish_scan.py  # before publishing: emails, chat links, tokens, private names
```

Standard library only; no network or accounts. CI runs all three on every push. The browser tests check the scripts'
logic, not the current markup of chatgpt.com or claude.ai.

The `SKILL.md` files are the source of truth. To get their code as standalone files, run
`python3 tools_extract.py` (writes `extracted/`, which git ignores); the tests do this
themselves. `python3 tools_extract.py --zips` builds the upload zips in `upload/`.

## Status and limits

- **Live-checked on chatgpt.com (signed in), 2026-10-28:** fill, send, the completion
  signal and the conversation endpoint. Not yet checked live: claude.ai, a long reply
  with tool pauses, and the DOM fallback.
- **Expect breakage:** site markup changes, and the capture endpoints are undocumented.
  The selector table in chat-relay records what was verified when.
- **The ledger is a prototype:** actor names aren't authenticated, the log is append-only
  by convention rather than tamper-evident, and the lock is local to one machine. Keep the
  handoff folder in git if you need history you can trust.
- **Cold catch-up tests:** three runs across ChatGPT's Work mode, a Temporary chat and a
  regular chat all caught up from two files and stayed in scope. Their findings shaped the
  current Authorized and logging rules.

## License

Licensed under the [MIT License](LICENSE). Contributions are welcome under the
[Contributor License Agreement](CLA.md), which keeps them open under the same license
while letting the maintainer also offer commercial versions. See
[CONTRIBUTING.md](CONTRIBUTING.md).
