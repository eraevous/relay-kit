#!/usr/bin/env python3
"""relay-kit/prs@3: a build-time, opt-in reader of GitHub pull-request state into the records.

    relay-kit prs --root RECORDS [--repo OWNER/NAME ...] [--since-days N] [--limit N]
                  [--via gh|token] [--token-env NAME] [--dry-run]

Reads the pull requests of the repositories listed in handoffs/.ledger.json ("repos": ["owner/name", ...])
or given with --repo, and appends to governance/pr-state.jsonl:

- one "pr" line per pull request whose state changed since its last line (title, state, author, head,
  reviews with their bodies verbatim, review-style issue comments, CI, and the PR body's ## What / ## Why /
  ## How / ## Authorized sections verbatim, with an Author: line if the body has one, and which sections
  are missing);
- one "fetch" line per run: when it ran, which repositories it read and which failed.

It is the only part of relay-kit that talks to GitHub for pull requests: the Cockpit renders the file
and never fetches, and other readers (a telemetry collector, say) read the file rather than GitHub.
Nothing is summarised: section text is the body's own words. The file is append-only; this tool
never rewrites a line. It writes nothing else, under --root or anywhere.

Network: through the gh CLI (--via gh, the default when gh is installed) or the GitHub REST API
with a token read from an environment variable (--via token, default GITHUB_TOKEN then GH_TOKEN).
The token is never written or printed.
"""
import argparse
import importlib.util
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

SCHEMA = "relay-kit/prs@3"
READS = {"relay-kit/prs@1", "relay-kit/prs@2", SCHEMA}   # @1 (no coverage, no unread) and @2 (no review bodies) stay readable
OUT = "governance/pr-state.jsonl"
SECTIONS = ("what", "why", "how", "authorized")
MAX_SECTION = 4000      # characters kept per section; longer ones are cut and listed under "truncated"
MAX_REVIEW = 4000       # characters kept of a review's body (@3); a cut body carries truncated: true
REPO = re.compile(r"[A-Za-z0-9][\w.-]*/[\w.-]+")
HEADING = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
AUTHOR = re.compile(r"^[ \t]*\**Author\**[ \t]*:[ \t]*\**[ \t]*(\S.*?)[ \t]*$", re.I)
SIGNED = re.compile(r"^[ \t]*\**(?:Reviewer|Reviewed by|Review by)\**[ \t]*:[ \t]*\**[ \t]*(.+?)[ \t]*$", re.I)
COMMENT = re.compile(r"<!--.*?-->", re.S)
# a verdict written in a review's opening words, for reviews posted as comments ("**APPROVED (round 4)**")
STATED = re.compile(r"\b(APPROVED|APPROVE|REQUEST(?:ING)? CHANGES|CHANGES REQUESTED|LGTM|BLOCK(?:ING|ED)?)\b")
NEGATED = re.compile(r"\b(?:not|no|never|isn't|aren't|cannot|can't|won't|un)\W*$", re.I)
# an issue comment that is a review (@3): its first non-empty line opens with one of these words, or it states a verdict
REVIEW_OPENER = re.compile(r"^\W{0,6}(?:Review|Execution review|Reviewer)\b", re.I)
FENCE = re.compile(r"^[ \t]*(```|~~~)")
FAILED = {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "error"}
COMPARE_SKIP = {"ts"}


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------- the PR body ----------------
def section_key(title):
    """'What', 'What this changes', '1. Why:' -> 'what' / 'why'; anything else None."""
    w = re.sub(r"^[\d.)\s]+", "", title).strip().lower()
    w = re.split(r"[^a-z]", w, maxsplit=1)[0] if w else ""
    return w if w in SECTIONS else None


def strip_comments(text):
    """HTML comments removed outside code fences only, so a fenced example stays verbatim."""
    out, chunk, fenced = [], [], False
    for ln in text.split("\n"):
        if FENCE.match(ln):
            out.append(chunk if fenced else COMMENT.sub("", "\n".join(chunk)).split("\n"))
            out.append([ln])
            chunk, fenced = [], not fenced
            continue
        chunk.append(ln)
    out.append(chunk if fenced else COMMENT.sub("", "\n".join(chunk)).split("\n"))
    return "\n".join(ln for part in out for ln in part)


def stated_verdict(body):
    """A verdict word in a review's opening words, unless negated ("NOT APPROVED", "not LGTM yet")."""
    head = (body or "")[:160]
    for m in STATED.finditer(head):
        if not NEGATED.search(head[max(0, m.start() - 16):m.start()]):
            return m.group(1)
    return None


def review_comment(body):
    """Whether an issue comment is a review posted as a comment (@3): its first non-empty line opens with
    Review / Execution review / Reviewer, or its opening words state a verdict. A plain remark isn't."""
    first = next((ln.strip() for ln in (body or "").split("\n") if ln.strip()), "")
    return bool(REVIEW_OPENER.match(first)) or stated_verdict(body) is not None


def cut_body(body):
    """(body, truncated): the text verbatim up to MAX_REVIEW characters."""
    b = (body or "").replace("\r\n", "\n")
    return (b[:MAX_REVIEW], True) if len(b) > MAX_REVIEW else (b, False)


def parse_body(body):
    """{sections: {key: text}, author: str|None, missing: [...], truncated: [...]}. Section text is the
    body's own words between the heading and the next heading of the same or a higher level, with HTML
    comments (template hints) removed; a section with nothing left counts as missing. Headings inside
    code fences don't count."""
    lines = (body or "").replace("\r\n", "\n").split("\n")
    heads, fenced = [], False
    for i, ln in enumerate(lines):
        if FENCE.match(ln):
            fenced = not fenced
            continue
        m = None if fenced else HEADING.match(ln)
        if m:
            heads.append((i, len(m.group(1)), section_key(m.group(2))))
    sections, truncated = {}, []
    for j, (i, level, key) in enumerate(heads):
        if not key or key in sections:
            continue
        end = next((k for k, lv, _ in heads[j + 1:] if lv <= level), len(lines))
        text = strip_comments("\n".join(lines[i + 1:end])).strip()
        if not text:
            continue
        if len(text) > MAX_SECTION:
            text, _ = text[:MAX_SECTION], truncated.append(key)
        sections[key] = text
    author, fenced = None, False
    for ln in strip_comments("\n".join(lines)).split("\n"):
        if FENCE.match(ln):
            fenced = not fenced
        elif not fenced and (m := AUTHOR.match(ln)):
            author = m.group(1)[:200]
            break
    return {"sections": sections, "author": author,
            "missing": [k for k in SECTIONS if k not in sections], "truncated": truncated}


# ---------------- GitHub (through tools/github.py, the kit's one GitHub client) ----------------
def _github():
    spec = importlib.util.spec_from_file_location("relaykit_github", pathlib.Path(__file__).resolve().parent / "github.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


GH = _github()
Refused = GH.Refused     # one exception for the reader and the client


def refused(x):
    """A Refused from any copy of the client module (a caller may load github.py under another name)."""
    return type(x).__name__ == "Refused"


class FnClient:
    """A bare get(path) function (tests) behind the client's interface."""
    def __init__(self, get):
        self.get, self.stats = get, {}

    def pages(self, path, limit=100, per_page=100, max_pages=10):
        """Walked page by page like github.Client.pages, so a test fixture past 100 items is read the same way."""
        sep = "&" if "?" in path else "?"
        per = max(1, min(per_page, limit + 1, 100))
        items, page = [], 1
        while len(items) <= limit and page <= max_pages:
            got = self.get(f"{path}{sep}per_page={per}&page={page}")
            if not isinstance(got, list):
                raise Refused(f"GET {path}: expected a list")
            items += got
            if len(got) < per:
                return items[:limit], len(items) > limit
            page += 1
        return items[:limit], True if len(items) > limit else None


def ci_state(get, repo, sha):
    """{state: success|failure|pending|none, failing: [names]} from check runs and commit statuses."""
    runs = (get(f"repos/{repo}/commits/{sha}/check-runs?per_page=100") or {}).get("check_runs") or []
    stat = get(f"repos/{repo}/commits/{sha}/status") or {}
    failing, pending, seen = [], False, 0
    for r in runs:
        seen += 1
        if r.get("status") != "completed":
            pending = True
        elif (r.get("conclusion") or "") in FAILED:
            failing.append(r.get("name") or "?")
    for s in stat.get("statuses") or []:
        seen += 1
        if s.get("state") == "pending":
            pending = True
        elif s.get("state") in ("failure", "error"):
            failing.append(s.get("context") or "?")
    state = "failure" if failing else "pending" if pending else "success" if seen else "none"
    return {"state": state, "failing": sorted(set(failing))}


COMMENT_LIMIT = 1000   # comments read per PR and endpoint (10 pages of 100); past it, coverage is reported as partial


def incomplete(n, more):
    return f"incomplete: read the first {n}; " + ("more are available" if more else "more may be available (page limit reached)")


def carry_forward(current, previous):
    """Entries of one kind when this run's read of them was incomplete, merged by id so a repeated failure
    never duplicates them (REV-1 on #33). Listed this run: this run's entries, each keeping its last known
    inline-comment count (the count is the part that wasn't read). Not listed: the last recorded entries."""
    if not current:
        return list(previous)
    last = {r.get("id"): r for r in previous if r.get("id") is not None}
    return [dict(r, comments=last[r.get("id")].get("comments", r.get("comments"))) if r.get("id") in last else r
            for r in current]


def pr_record(get, repo, pr, ts, partial=None, pages=None):
    """One pr line. Reviews, review-style comments or CI that can't be read are listed under `unread`
    (and in `partial`), not recorded as none: missing isn't zero. `pages(path, limit)` lists an endpoint."""
    n = pr["number"]
    pages = pages or (lambda path, limit=100: (get(f"{path}?per_page={limit}") or [], False))
    body = parse_body(pr.get("body"))
    reviews, unread = [], []
    try:
        got, more = pages(f"repos/{repo}/pulls/{n}/reviews", limit=COMMENT_LIMIT)   # paged like the comments (Kate 7 on #33)
        if more is not False and partial is not None:   # a review past the cut isn't seen: say so
            partial.append({"number": n, "what": "reviews", "error": incomplete(len(got), more)})
        for r in got if isinstance(got, list) else []:
            if not isinstance(r, dict) or r.get("state") in (None, "PENDING"):
                continue
            sig = next((m.group(1)[:120] for ln in (r.get("body") or "").split("\n") if (m := SIGNED.match(ln))), None)
            body_, cut = cut_body(r.get("body"))
            reviews.append({"id": r.get("id"), "kind": "review", "by": (r.get("user") or {}).get("login"),
                            "verdict": r.get("state"), "at": r.get("submitted_at"), "signed": sig,
                            "stated": stated_verdict(r.get("body")), "body": body_, "truncated": cut, "comments": 0})
        if reviews:   # inline (line) comments, counted per review; their text stays on GitHub
            inline, more = pages(f"repos/{repo}/pulls/{n}/comments", limit=COMMENT_LIMIT)
            if more is not False and partial is not None:   # counts past the cut would read as complete (REV-1 on #33)
                partial.append({"number": n, "what": "inline comments", "error": incomplete(len(inline), more)})
            counts = {}
            for c in inline:
                if isinstance(c, dict):
                    counts[c.get("pull_request_review_id")] = counts.get(c.get("pull_request_review_id"), 0) + 1
            for rv in reviews:
                rv["comments"] = counts.get(rv["id"], 0)
    except Exception as x:
        if not refused(x):
            raise
        unread.append("reviews")
        if partial is not None:
            partial.append({"number": n, "what": "reviews", "error": str(x)[:200]})
    try:   # @3: issue comments that are reviews in all but GitHub's type (a verdict stated, or a Review: opener)
        issue_comments, more = pages(f"repos/{repo}/issues/{n}/comments", limit=COMMENT_LIMIT)
        if more is not False and partial is not None:   # a review-style comment past the cut isn't seen: say so
            partial.append({"number": n, "what": "comments", "error": incomplete(len(issue_comments), more)})
        for c in issue_comments:
            if not isinstance(c, dict) or not review_comment(c.get("body")):
                continue
            sig = next((m.group(1)[:120] for ln in (c.get("body") or "").split("\n") if (m := SIGNED.match(ln))), None)
            body_, cut = cut_body(c.get("body"))
            reviews.append({"id": c.get("id"), "kind": "comment", "by": (c.get("user") or {}).get("login"),
                            "verdict": None, "at": c.get("created_at"), "signed": sig,
                            "stated": stated_verdict(c.get("body")), "body": body_, "truncated": cut, "comments": 0})
    except Exception as x:
        if not refused(x):
            raise
        unread.append("comments")
        if partial is not None:
            partial.append({"number": n, "what": "comments", "error": str(x)[:200]})
    reviews.sort(key=lambda r: (str(r.get("at") or ""), r["kind"], str(r.get("id") or "")))
    sha = (pr.get("head") or {}).get("sha")
    ci = {"state": "none", "failing": []}
    if sha:
        try:
            ci = ci_state(get, repo, sha)
        except Exception as x:
            if not refused(x):
                raise
            ci = {"state": "unknown", "failing": []}
            unread.append("ci")
            if partial is not None:
                partial.append({"number": n, "what": "ci", "error": str(x)[:200]})
    state = "merged" if pr.get("merged_at") else pr.get("state")
    return {"schema": SCHEMA, "kind": "pr", "ts": ts, "repo": repo, "number": n,
            "url": pr.get("html_url"), "title": pr.get("title"), "state": state, "draft": bool(pr.get("draft")),
            "author": (pr.get("user") or {}).get("login"), "author_line": body["author"],
            "created_at": pr.get("created_at"), "updated_at": pr.get("updated_at"),
            "merged_at": pr.get("merged_at"), "closed_at": pr.get("closed_at"),
            "base": (pr.get("base") or {}).get("ref"), "head": {"ref": (pr.get("head") or {}).get("ref"), "sha": sha},
            "reviews": reviews, "ci": ci, "unread": unread,
            "body_sections": body["sections"], "missing": body["missing"], "truncated": body["truncated"]}


# ---------------- the file ----------------
def read_lines(path):
    out = []
    if path.exists():
        for i, ln in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if ln.strip():
                try:
                    out.append(json.loads(ln))
                except json.JSONDecodeError:
                    raise Refused(f"{OUT} line {i} isn't JSON; fix it by hand before appending")
    return out


def latest_by_pr(lines):
    last = {}
    for x in lines:
        if x.get("kind") == "pr" and x.get("schema") in READS:
            last[(x.get("repo"), x.get("number"))] = x
    return last


def same(a, b):
    """Unchanged apart from when it was read and, for an @1 line, the schema. `unread` is compared too
    (an @1 line has none: read as nothing unread), so a read that starts failing with unchanged content is
    recorded, and a successful reread clears it (Kate 1 on #33); a repeated failure still adds no line."""
    skip = COMPARE_SKIP | {"unread"} | ({"schema"} if a.get("schema") != b.get("schema") else set())
    if sorted(a.get("unread") or []) != sorted(b.get("unread") or []):
        return False
    return {k: v for k, v in a.items() if k not in skip} == {k: v for k, v in b.items() if k not in skip}


def repos_of(root, given):
    if given:
        repos = list(given)
    else:
        try:
            cfg = json.loads((root / "handoffs" / ".ledger.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cfg = {}
        repos = list(cfg.get("repos") or [])
        if not repos:
            raise Refused('no repositories: pass --repo OWNER/NAME or add "repos": ["owner/name"] '
                          "to handoffs/.ledger.json")
    bad = [r for r in repos if not isinstance(r, str) or not REPO.fullmatch(r)]
    if bad:
        raise Refused(f"not OWNER/NAME: {bad}")
    return list(dict.fromkeys(repos))


def run(root, client, via, repos=None, since_days=14, limit=30, ts=None, dry_run=False):
    """Read, compare with the last line per PR, append what changed and a fetch line. Returns the
    fetch line. `client` is a github.Client, or a bare get(path) function (tests)."""
    client = client if hasattr(client, "pages") else FnClient(client)
    get = client.get
    root = pathlib.Path(root)
    if not root.is_dir():
        raise Refused(f"--root {root} isn't a folder")
    if not (root / "governance").is_dir():
        raise Refused(f"{root} has no governance/ folder; is it a records store?")
    path = root / OUT
    if path.is_symlink() or (root / "governance").is_symlink() or \
            not path.parent.resolve().is_relative_to(root.resolve()):
        raise Refused(f"{OUT} or governance/ is a symlink; refusing to append through it")
    repos = repos_of(root, repos)
    ts = ts or now_iso()
    cutoff = (dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ") - dt.timedelta(days=since_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    last = latest_by_pr(read_lines(path))
    new, ok, failed, seen, coverage, revisions = [], [], {}, 0, {}, {}
    for repo in repos:
        found, partial = [], []
        lim = max(1, min(limit, 100))
        cov = {"asked": f"the {lim} most recently updated, plus any last recorded open", "listed": 0,
               "more_available": None, "reread_open": 0, "prs_read": 0, "partial": partial}
        try:
            try:   # revision identity: what the repository looked like when it was read
                meta = get(f"repos/{repo}")
                if isinstance(meta, dict):
                    revisions[repo] = {"default_branch": meta.get("default_branch"), "pushed_at": meta.get("pushed_at")}
            except Exception as x:
                if not refused(x):
                    raise
                partial.append({"number": None, "what": "repository", "error": str(x)[:200]})
            prs, more = client.pages(f"repos/{repo}/pulls?state=all&sort=updated&direction=desc", limit=lim)
            cov["listed"], cov["more_available"] = len(prs), more
            listed = {pr.get("number") for pr in prs if isinstance(pr, dict)}
            # a PR last recorded open is re-read whatever its age, so a closed one never stays "open"
            for (r_, n_), prev in sorted(last.items(), key=lambda kv: kv[0][1]):
                if r_ == repo and prev.get("state") == "open" and n_ not in listed:
                    try:
                        one = get(f"repos/{repo}/pulls/{n_}")
                    except Exception as x:     # one PR that can't be read again doesn't fail the repository
                        if not refused(x):
                            raise
                        partial.append({"number": n_, "what": "pull request", "error": str(x)[:200]})
                        continue
                    if isinstance(one, dict):
                        prs.append(one)
                        cov["reread_open"] += 1
            for pr in prs:
                if not isinstance(pr, dict) or not isinstance(pr.get("number"), int):
                    raise Refused(f"repos/{repo}/pulls: unexpected response")
                was_open = (last.get((repo, pr["number"])) or {}).get("state") == "open"
                if pr.get("state") != "open" and not was_open and (pr.get("updated_at") or "") < cutoff:
                    continue
                seen += 1
                cov["prs_read"] += 1
                rec = pr_record(get, repo, pr, ts, partial, client.pages)
                prev = last.get((repo, rec["number"]))
                if prev:   # what couldn't be read this time keeps its last known value (still marked unread)
                    kept = [r for r in prev.get("reviews") or [] if isinstance(r, dict)]
                    cur_rv = [r for r in rec["reviews"] if r.get("kind") == "review"]
                    cur_cm = [r for r in rec["reviews"] if r.get("kind") == "comment"]
                    if "reviews" in rec["unread"]:   # reviews unread, or listed with their inline counts unread
                        cur_rv = carry_forward(cur_rv, [r for r in kept if r.get("kind", "review") == "review"])
                    if "comments" in rec["unread"]:
                        cur_cm = carry_forward(cur_cm, [r for r in kept if r.get("kind") == "comment"])
                    if "reviews" in rec["unread"] or "comments" in rec["unread"]:
                        rec["reviews"] = sorted(cur_rv + cur_cm, key=lambda r: (str(r.get("at") or ""), str(r.get("kind") or "review"),
                                                                                str(r.get("id") or "")))
                    if "ci" in rec["unread"] and isinstance(prev.get("ci"), dict):
                        rec["ci"] = prev["ci"]
                if prev is None or not same(prev, rec):
                    found.append(rec)
            new += found          # a repository whose list fails part-way contributes nothing
            ok.append(repo)
            coverage[repo] = cov
        except Exception as x:
            if not refused(x):
                raise
            failed[repo] = str(x)[:300]
    stats = dict(getattr(client, "stats", {}) or {})
    fetch = {"schema": SCHEMA, "kind": "fetch", "ts": ts, "via": via, "repos": repos, "ok": ok, "failed": failed,
             "since_days": since_days, "prs_seen": seen, "appended": len(new), "coverage": coverage,
             "revisions": revisions, "client": {k: stats.get(k) for k in ("requests", "retries", "rate_limit_waits",
                                                                           "memo_hits", "not_modified", "rate_limit_remaining",
                                                                           "first_at", "last_at") if k in stats}}
    lines = new + [fetch]
    if dry_run:
        for x in lines:
            print(json.dumps(x, ensure_ascii=False, sort_keys=True))
        return fetch
    with open(path, "a+b") as f:     # a file not ending in a newline gets one first, so no line is joined
        f.seek(0, os.SEEK_END)
        if f.tell():
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                f.write(b"\n")
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        for x in lines:
            f.write(json.dumps(x, ensure_ascii=False, sort_keys=True) + "\n")
    return fetch


def main(argv=None):
    ap = argparse.ArgumentParser(prog="relay-kit prs", description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", required=True, type=pathlib.Path, help="the records store")
    ap.add_argument("--repo", action="append", help="OWNER/NAME (repeatable); default: .ledger.json 'repos'")
    ap.add_argument("--since-days", type=int, default=14, help="closed or merged PRs updated within N days (default 14); open ones among the --limit read, and any last recorded open")
    ap.add_argument("--limit", type=int, default=30, help="most recently updated PRs read per repository (max 100)")
    ap.add_argument("--via", choices=["gh", "token"], help="gh CLI (default when installed) or a token from the environment")
    ap.add_argument("--token-env", help="environment variable holding the token (default GITHUB_TOKEN, then GH_TOKEN)")
    ap.add_argument("--dry-run", action="store_true", help="print the lines instead of appending them")
    ap.add_argument("--max-wait", type=int, default=60, help="longest rate-limit wait, in seconds, before giving up (default 60)")
    ap.add_argument("--cache", type=pathlib.Path, help="keep responses with their ETags here and revalidate them (holds PR text: "
                                                         "keep it private); off by default")
    a = ap.parse_args(argv)
    via = a.via or ("gh" if shutil.which("gh") else "token")
    try:
        if via == "gh":
            if not shutil.which("gh"):
                raise Refused("gh isn't installed; use --via token")
            transport = GH.gh_transport()
        else:
            names = [a.token_env] if a.token_env else ["GITHUB_TOKEN", "GH_TOKEN"]
            token = next((os.environ[n] for n in names if os.environ.get(n)), None)
            if not token:
                raise Refused(f"no token in {' or '.join(names)}")
            transport = GH.token_transport(token)
        client = GH.Client(transport, max_wait=a.max_wait, cache_dir=a.cache, cache_scope=via)
        f = run(a.root, client, via, a.repo, a.since_days, a.limit, dry_run=a.dry_run)
    except Refused as x:
        sys.exit(f"refused: {x}")
    print(f"{f['prs_seen']} PR(s) read from {len(f['ok'])} repo(s), {f['appended']} changed line(s) "
          f"{'printed' if a.dry_run else 'appended to ' + OUT}"
          + (f"; failed: {', '.join(f['failed'])}" if f["failed"] else ""), file=sys.stderr)
    if f["failed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
