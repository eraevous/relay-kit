# Pull-request reader: `relay-kit/prs@3`

`relay-kit prs` reads pull-request state from GitHub, at build time and only when you run it, and
appends what changed to `governance/pr-state.jsonl` in the records. The [Cockpit](cockpit.md)
renders that file; it never fetches. Other readers that need pull-request data should read the
file too, so there is one GitHub reader, not one per tool.

    relay-kit prs --root RECORDS [--repo OWNER/NAME ...] [--since-days 14] [--limit 30]
                  [--via gh|token] [--token-env NAME] [--max-wait 60] [--cache DIR] [--dry-run]

**@3** (HO-RK-20261106-NXPR, DEC-139) records what reviewers wrote, not only how they voted: each
review's body verbatim (cut at 4,000 characters, flagged), its inline-comment count, and **issue
comments that are reviews in all but GitHub's type** (the first non-empty line opens with `Review`,
`Execution review` or `Reviewer`, or a verdict is stated in the opening words), as reviews of
`kind: "comment"`. That is one more read per pull request (its issue comments) and one per pull
request with reviews (its inline comments), each walked page by page up to 1,000 comments; past that the
read is listed under `coverage.partial` as incomplete, never presented as everything. On the first @3 run a pull request whose reviews gained
bodies gets one new line; an unchanged one none. Inline comments' text stays on GitHub. Review
submissions are walked page by page too, up to the same cap, and reported as partial past it. A change
in `unread` is a change: the run where a read starts failing appends one line (the retained content,
marked unread), a repeat of the same failure appends none, and the first successful reread appends one
that clears it, even when the content is the same.

**@2** (HO-RK-20261105-TXRM step 1) reads GitHub through [tools/github.py](../tools/github.py), the
kit's client for pull-request reads, and records what was asked for against what came back, so a
partial read shows as partial, never as zero. @1 lines stay readable and are compared with @2 reads
apart from the schema, so upgrading appends nothing for unchanged pull requests.

## The client

Read-only: it only ever sends GET. Pull-request data is read through it once, and everything else
reads the file this tool writes. (Export's public-repository check, status's CI read and the
cockpit's opt-in `--pr-state` lookup still call GitHub their own way; routing them through it is open.)

- **Pagination:** list endpoints are walked page by page (100 a page) up to `--limit`; whether more
  were available is recorded.
- **Retries:** network errors and HTTP 500/502/503/504 are retried three times, waiting 1, 2 and 4 s.
- **Rate limits:** a 429, a 403 with no requests left, or a 403 with `retry-after` (GitHub's
  secondary limit) waits for the reset when that is at most `--max-wait` seconds (default 60);
  otherwise that repository fails with the reset time. A failure gh reports without any HTTP
  response (not signed in) isn't retried.
- **Partial reads:** reviews or CI that can't be read keep their last known value and are marked
  `unread`; a pull request that can't be read again is a partial read, not a failed repository.
- **Caching:** each path is fetched once per run. `--cache DIR` keeps responses with their ETags and
  revalidates them, so an unchanged resource costs a 304. The cache holds PR text from private
  repositories, so it is off unless you ask for it; its files are readable by you only (0600 in a
  0700 folder), keyed by transport as well as path. A token is never sent past a redirect away from
  api.github.com.

- **Which repositories:** `--repo` (repeatable), else `"repos": ["owner/name", ...]` in
  `handoffs/.ledger.json`. Anything that isn't `OWNER/NAME` is refused.
- **Which pull requests:** per repository, the `--limit` most recently updated; all open ones among
  them, and closed or merged ones updated within `--since-days`.
- **How it reaches GitHub:** the `gh` CLI (`--via gh`, the default when it is installed), or the REST
  API with a token from the environment (`--via token`; `--token-env NAME`, default `GITHUB_TOKEN`
  then `GH_TOKEN`). The token is never written or printed. Standard library only.
- **What it writes:** appends to `governance/pr-state.jsonl` and nothing else, anywhere. It never
  rewrites a line; a line it can't read stops it (fix the file by hand) rather than being repaired.
  `--dry-run` prints the lines instead.

## Lines

Every line has `"schema": "relay-kit/prs@3"` and a `kind`.

**`"kind": "pr"`**, written only when something about the pull request changed since its last line
(any field but `ts`):

| Field | Meaning |
| --- | --- |
| `ts` | when this run read it (UTC) |
| `repo`, `number`, `url`, `title` | as on GitHub |
| `state` | `open`, `closed` or `merged`; `draft` true or false |
| `author` | the GitHub login that opened it. Agents often push under the owner's login, so this doesn't say who wrote it |
| `author_line` | the text of the body's first `Author:` line, or null: who says they wrote it |
| `created_at`, `updated_at`, `merged_at`, `closed_at`, `base`, `head` (`ref`, `sha`) | as on GitHub |
| `reviews` | every submitted review, and every review-style issue comment, by time: `id`, `kind` (`review` or `comment`), `by` (login), `verdict` (GitHub's state; null for a comment), `at`, `signed` (a `Reviewer:` line in the body, or null), `stated` (a verdict word in the first 160 characters, e.g. `APPROVED` or `REQUEST CHANGES`; or null), `body` (verbatim, up to 4,000 characters), `truncated`, `comments` (inline comments on that review; 0 for a comment) |
| `ci` | `state`: `failure` if any check run or status failed, else `pending` if any is still running, else `success`, or `none` when there are none; `failing`: their names |
| `body_sections` | `what`, `why`, `how`, `authorized`: the text under each heading, verbatim (HTML comments removed), up to 4000 characters each. A heading counts when its first word is the section's name ("What this changes" is `what`); headings in code fences don't |
| `missing`, `truncated` | sections absent or empty; sections cut at 4000 characters |
| `unread` | `reviews`, `comments` and/or `ci` when they couldn't be read (`reviews` also when the reviews were read but their inline comments weren't). Then the unread part keeps its last recorded value, merged by `id` so repeated failures never duplicate an entry: reviews listed this run keep their last known inline count (or the part is empty; `ci.state` is `unknown`): not read, not none |

**`"kind": "fetch"`**, one per run: `ts`, `via`, `repos`, `ok`, `failed` (repo → error),
`since_days`, `prs_seen`, `appended`. A repository that fails doesn't stop the others. @2 adds:

| Field | Meaning |
| --- | --- |
| `coverage` | per repository: `asked` (in words), `listed`, `more_available`, `reread_open` (PRs last recorded open, read again by number), `prs_read`, and `partial`: each read that failed inside a repository that otherwise succeeded (`number`, `what`: reviews, comments, inline comments, ci, pull request or repository, `error`; an `error` starting `incomplete:` is a comment list cut at 1,000) |
| `revisions` | per repository: `default_branch` and `pushed_at` when it was read |
| `client` | `requests`, `retries`, `rate_limit_waits`, `memo_hits`, `not_modified`, `rate_limit_remaining`, `first_at`, `last_at` |

Readers take the last `pr` line per `(repo, number)`, and a repository's freshness from its last
`fetch` line that lists it under `ok`. Lines of another schema version are unreadable to an @1
reader, not guessed at.

## The PR body template

[tools/templates/PULL_REQUEST_TEMPLATE.md](../tools/templates/PULL_REQUEST_TEMPLATE.md) has an
`Author:` line and the four sections. Copy it to `.github/pull_request_template.md` in a repository
to make it GitHub's default. An agent writing a PR names itself on the `Author:` line with its lane
and session; the Cockpit then shows "written by <agent>, not the owner".

## What it doesn't do

It summarises nothing, infers no authorship from commits, and decides nothing: whether a pull request
had enough reviews is for whoever reads it (the Cockpit states families; the rule is yours). It reads
issue comments only to find reviews posted as comments, and review threads only to count them. Running it is a network action;
the records hold only what GitHub returned to the account it ran as.
