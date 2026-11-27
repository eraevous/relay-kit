# Cold catch-up test

Can an assistant that has never seen the work pick up a handoff from the file alone?
This tests the handoff layer without any browser relay.

## The packet

Send a fresh chat **two files and one message**, nothing else:

1. `skills/handoff-ledger/SKILL.md`, the procedure.
2. The handoff file, e.g. `HO-LANE-YYYYMMDD-XXXX.md`.

> Pick up handoff HO-LANE-YYYYMMDD-XXXX. Everything you need is in the two attached files.
> Follow the handoff-ledger procedure and give me the output in the format the handoff
> asks for.

Don't attach the whole kit: a zip hands the chat every file, including your pass
criteria. For the coldest run, use a chat with no memory, project or custom
instructions (in ChatGPT, a Temporary chat with Personalization off).

Two paths to test:
- **prepared:** send the file as drafted. The recipient should do the work, leave the
  status alone, and say delivery needs recording.
- **submitted:** record `submitted` right before pasting, so the copy it reads says `submitted`. The
  recipient should acknowledge, do the work and give `acknowledged`/`completed` lines.

Note for each run: app and mode, model, whether it had memory or a project, and when you
pasted.

## Afterwards

Save the reply as `outputs/<id>.md`, then record the recipient's steps on its behalf,
as yourself, with the times it gave:

```
python3 skills/handoff-ledger/scripts/handoff.py --dir handoffs set <id> acknowledged --by <owner> --at <time> --note "for <recipient>, from its reply"
python3 skills/handoff-ledger/scripts/handoff.py --dir handoffs set <id> completed --by <owner> --at <time> --note "for <recipient>: outputs/<id>.md"
python3 skills/handoff-ledger/scripts/handoff.py --dir handoffs check
```

## What counts as a pass

- It handles the status the way the procedure says.
- It restates the task and says which authority it acted on (the quote under
  Authorized, or the owner's message in the chat).
- It names the proposals as not adopted and acts on none of them.
- It stays inside the limits (word cap, no links, nobody contacted).
- Its review, if asked for one, is specific enough to act on.

A fail is just as useful: whatever it misread is what the handoff format needs to say
more plainly.

A sample prepared handoff is in `tests/fixtures/`.
