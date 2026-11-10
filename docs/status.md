# Status editions: `relay-kit/status@1`

A status edition is a short narrative someone writes for the owner: what got done, what went wrong,
where things stand, what comes next. It is **authored, never generated**: the Cockpit's Status
surface shows the newest edition as written and summarises nothing (DEC-139, HO-RK-20261106-NXPR).

    relay-kit edition new --root RECORDS --edition morning [--date YYYY-MM-DD] [--author ACTOR] [--covers TEXT]
    relay-kit edition check --root RECORDS

## The file

`status/<YYYY-MM-DD>-<edition>.md` in the records store. `edition` is a short lower-case name
(`morning`, `midday`, `evening`, or anything `[a-z][a-z0-9-]*`). Front matter, then the body:

```markdown
---
date: 2026-11-06
edition: morning
author: claude:kairos-coord#01GZ…
covers: 2026-11-05 10:00 CT → 2026-11-06 08:30 CT
contract: relay-kit/status@1
---

# Kairos status · Friday 2026-11-06, morning

## Accomplishments
- …
## Road bumps
- …
## Status
- …
## Next steps
1. …
```

- `date` and `edition` must match the file name; `author` is the writer (an actor with a session,
  or the owner); `covers` is the period, in words, on one line (`edition new` refuses line breaks
  and control characters, which could add a key or end the front matter); `contract` is required
  (`relay-kit/status@1`, or the pilot `kairos/status@0`): `check` reports a missing or empty one.
  Other keys are kept and shown.
- A file whose name is not a calendar date (`2026-13-03-morning.md`), or that is a symlink, is listed
  as unreadable, never followed or guessed at; `edition new` refuses to write through a symlink and
  creates the file exclusively.
- The four H2 sections are **Accomplishments / Road bumps / Status / Next steps**, in that order
  by convention (a heading counts when it starts with the section's name, so "Status (as of
  08:30 CT)" is fine). A missing one is flagged on the surface, not filled in.
- **Editions are added, never edited.** `edition new` refuses to overwrite; a correction is a new
  edition (`morning-2`, `midday`). The folder is the coordinator's; lanes don't write to it unless
  the owner says so.
- **Newest**: the latest `date`; on one day `morning < midday < noon < afternoon < evening <
  night`, then other names alphabetically. The Cockpit renders the newest and lists the rest in a
  collapsed index (date, edition, author, covers).
- **Stale**: an edition whose date is more than `--drift-days` (default 3) before the newest
  record is flagged stale. Like everything on the page, that is measured from the records, not
  from today; the browser also ages the dates it shows.

## The markdown subset

Rendered: ATX headings (`#` to `######`; the file's `#` becomes the surface's `h2`), bullet and
numbered lists, paragraphs, `**bold**`, `*italic*` / `_italic_`, `` `code` ``, horizontal rules,
and fenced code (shown as plain text in a `<pre>`). **Links are shown as text**: `[the PR](url)`
becomes "the PR (url)"; nothing on the page is a link into the world and nothing is fetched. HTML
comments are removed (template hints). Everything else, HTML and images included, is plain text:
the page escapes first and applies only the patterns above.

`kairos/status@0`, the pilot form the first edition was written under, has the same fields and is
read as @1.

## In `--content metadata`

The surface keeps date, edition, author, covers, file name, which sections exist and the body's
length. The text is left out.
