#!/usr/bin/env python3
"""Where things stand: one read-only summary for starting back up after a restart.

    python3 tools/status.py --root path/to/records [--no-fetch] [--no-ci]

Reads a records folder laid out like the kit's private repo (any part may be missing):
  handoffs/                 handoff files (relay-kit/handoff@1 to @4)
  governance/decisions.jsonl, governance/challenges.jsonl
  threads.md                the coordination index: which thread or session does what

and prints, in order of what needs attention:
  1. whether this copy is behind the shared copy (a stale copy is how two sessions
     created the same handoff id on 2026-10-29);
  2. the checks on the shared branch, from GitHub through `gh` (skip with --no-ci): red or
     green, since which commit, the alert issue, and whether anyone has acknowledged it;
  3. what waits on the owner: proposed decisions nobody has superseded, handoffs
     whose Authorized section is still unfilled, and waiting handoffs whose waiting_on
     names the owner (handoff@4), or, in files without the field, that are auth-required;
  4. open handoffs by state, with who holds each one (a wait's waiting_on, when set), flagging any whose output is
     already filed but whose state was never closed, and any that has waited past
     --stale-hours (sent with no acknowledgement, or claimed with no progress logged);
  5. unnoticed unblocks (DEC-237): blocked or in-progress handoffs whose latest log line names a
     handoff that has since completed or been cancelled, or a pull request (`repo#N`,
     `owner/repo#N`, a GitHub pull URL) merged or closed since, with hours since it cleared.
     Pull-request states come from governance/pr-state.jsonl (relay-kit/prs@4, the one GitHub
     reader for pull requests), so they are as current as the last `relay-kit prs` run.
     Blockers are only named in prose, so this reads the note's text: a bare `#N` is skipped,
     and a cleared item may not have been the real blocker. It is a report, never a check;
  6. open challenges;
  7. where the coordination index is and its last change-log line;
  8. what it couldn't check.

Ids are shown with their alias (AEC K7QF) and actors with the shortest session prefix that
is unique among the actors in the folder (claude:aec#01N4); the files keep the long forms.

It changes nothing; `git fetch` only updates remote-tracking refs (skip it with
--no-fetch). Standard library only."""
import argparse, datetime as dt, fnmatch, importlib.util, json, os, pathlib, re, subprocess, sys

HERE = pathlib.Path(__file__).resolve().parent
KIT = HERE.parent   # a checkout: tools/ next to skills/. Installed: skills/ sits inside the package.
SKILLS = KIT / "skills" if (KIT / "skills" / "handoff-ledger").is_dir() else HERE / "skills"
LEDGER = SKILLS / "handoff-ledger" / "scripts" / "handoff.py"
WAITING = {"submitted": "NO ACK", "acknowledged": "STALE CLAIM", "working": "STALE CLAIM"}   # timeout, as in a job queue's visibility timeout
ALERT_TITLE = "Records check failing on main"   # the issue the records workflow opens on a red push
# handoff@3 names (DEC-255); states are read through handoff.py's canon, so @2 files map here too
HOLDER = {"prepared": "from", "submitted": "to", "acknowledged": "to", "working": "to",
          "input-required": "from", "auth-required": "from", "blocked": "from"}


_LEDGER = None


def load_lifecycle():
    spec = importlib.util.spec_from_file_location("relaykit_lifecycle", HERE / "lifecycle.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def load_ledger():
    """handoff.py, the skill's standalone script, loaded once as a module (its main() is guarded)."""
    global _LEDGER
    if _LEDGER is not None:
        return _LEDGER
    spec = importlib.util.spec_from_file_location("handoff", LEDGER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    _LEDGER = m
    return m


def git(root, *args):
    p = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    if p.returncode:
        raise RuntimeError(p.stderr.strip() or f"git {' '.join(args)} failed")
    return p.stdout.strip()


def freshness(root, fetch, gaps):
    try:
        git(root, "rev-parse", "--git-dir")
    except (RuntimeError, FileNotFoundError):
        gaps.append("not a git repository: couldn't tell whether this copy is current")
        return None
    if fetch:
        try:
            git(root, "fetch", "--quiet", "--all")
        except RuntimeError as e:
            gaps.append(f"git fetch failed, so 'behind' uses the last fetch ({e.splitlines()[0] if str(e) else e})")
    else:
        gaps.append("--no-fetch: 'behind' uses the last fetch")
    try:
        upstream = git(root, "rev-parse", "--abbrev-ref", "@{upstream}")
        behind, ahead = git(root, "rev-list", "--left-right", "--count", "@{upstream}...HEAD").split()
    except RuntimeError:
        gaps.append("current branch has no upstream: couldn't compare with the shared copy")
        return None
    dirty = bool(git(root, "status", "--porcelain"))
    return f"{git(root, 'rev-parse', '--abbrev-ref', 'HEAD')} vs {upstream}: {behind} behind, {ahead} ahead" + (
        ", uncommitted changes" if dirty else "")


def age_hours(at):
    """Hours since a log time, or None if it isn't a time with a zone (never crash on a bad line)."""
    t = load_ledger().parse_utc(at)
    return None if t is None else (dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 3600


def handoffs(root, gaps, stale_hours):
    d = root / "handoffs"
    if not d.is_dir():
        gaps.append("no handoffs/ folder")
        return [], []
    led = load_ledger()
    try:
        owner = led.load_owner(d)
    except led.Refused as e:   # a broken .ledger.json is reported, never a crash (the owner check is the ledger's)
        owner = "owner"
        gaps.append(f"owner unknown ({e}); waits on the owner were matched against 'owner'")
    rows, unauthorized = [], []
    for p in sorted(d.glob("HO-*.md")):
        text = p.read_text(encoding="utf-8")
        try:
            meta, log, _, _ = led.parse_text(text, p.stem)
        except led.Refused as e:
            gaps.append(f"{p.name} couldn't be read ({e}); run handoff.py check")
            continue
        st = led.canon(meta.get("status", "?"))
        if st in led.FINAL:
            continue
        m = re.search(r"^## Authorized[ \t]*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
        auth = m.group(1) if m else ""
        if not auth.strip() or led.TODO_RE.search(auth) or "NOT AUTHORIZED" in auth:
            unauthorized.append(f"{meta.get('id')}  {meta.get('title', '')}")
        last = log[-1] if log else {}
        flag = "  OUTPUT FILED, state not closed" if (d / "outputs" / f"{meta.get('id')}.md").exists() else ""
        age = age_hours(last.get("at", "")) if last else None
        if st in WAITING and age is not None and age > stale_hours:
            flag += f"  {WAITING[st]} for {age / 24:.1f} days"
        holder = led.waiting_on(meta) if st in led.WAITING and led.waiting_on(meta) else meta.get(HOLDER.get(st, "to"), "?")
        if led.waits_on_owner(meta, owner):   # handoff@4's waiting_on names the owner, or (no field) auth-required
            flag += "  WAITS ON THE OWNER"
        rows.append((st, led.alias(meta.get("id", "")), holder, meta.get("title", ""),
                     f"{last.get('at', '')} {last.get('actor', '')}".strip() + flag))
    return rows, unauthorized


# Unnoticed unblocks (DEC-237, HO-RK-20261116-EZEJ item 4). A blocker is named only in prose, in the
# note of a handoff's latest log line ("waiting on #1 review: PlayWrite#1 open"), so these are read
# from the note's text: handoff ids, `repo#N` / `owner/repo#N`, and GitHub pull URLs. A bare `#N`
# names no repository and is skipped. A reference counts as the *named* blocker only when it sits in
# a clause that starts with a dependency marker ("waiting on", "blocked on", "needs" ...), up to the
# next ';'; anything else is only *mentioned* (Kate on #64: "cleared after the note" is chronology,
# not a dependency). A PR closed unmerged or a handoff cancelled ended without delivering: that is
# "ended, reassess", never a confirmed unblock.
DEP_MARK = re.compile(r"\b(?:waiting (?:on|for)|waits? (?:on|for)|blocked (?:on|by)|depends on|needs|queued behind|until)\b", re.I)
PR_REF = re.compile(r"(?:github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+))|(?<![\w/#.-])(?:([\w.-]+)/)?((?=[\w.-]*[A-Za-z])[\w.-]+)#(\d+)\b")


def blocker_refs(note):
    """(handoff ids, PR refs as (owner or None, repo, number)) named in a log note, in order, once each."""
    led = load_ledger()
    hos = list(dict.fromkeys(re.findall(rf"\b{led.ID_PAT}\b", note or "")))
    prs = []
    for m in PR_REF.finditer(note or ""):
        ref = (m.group(1), m.group(2), int(m.group(3))) if m.group(3) else (m.group(4), m.group(5), int(m.group(6)))
        if ref not in prs:
            prs.append(ref)
    return hos, prs


def named_spans(note):
    """Character spans of the note's clauses that follow a dependency marker (to the next ';')."""
    spans = []
    for m in DEP_MARK.finditer(note or ""):
        end = (note or "").find(";", m.end())
        spans.append((m.start(), len(note) if end < 0 else end))
    return spans


def pr_named(note, ref):
    """Whether any span matching this PR ref (in whatever form the note wrote it: repo#N,
    owner/repo#N or a pull URL) starts inside a dependency clause (Codex on #64)."""
    spans = named_spans(note)
    for m in PR_REF.finditer(note or ""):
        got = (m.group(1), m.group(2), int(m.group(3))) if m.group(3) else (m.group(4), m.group(5), int(m.group(6)))
        if got == ref and any(a <= m.start() < b for a, b in spans):
            return True
    return False


def is_named(note, ref_text):
    """Whether ref_text appears inside a clause that follows a dependency marker."""
    spans = named_spans(note)
    for m in re.finditer(re.escape(ref_text), note or ""):
        if any(a <= m.start() < b for a, b in spans):
            return True
    return False


def load_prs():
    spec = importlib.util.spec_from_file_location("relaykit_prs", HERE / "prs.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def recorded_prs(root, gaps):
    """Pull-request state as `relay-kit prs` last recorded it (governance/pr-state.jsonl, relay-kit/prs@4):
    {(owner/repo lowercased, number): line}, {owner/repo lowercased: time of its newest successful
    fetch}, and prs.read_gaps() (newest fetch failed, partial reads). status never asks GitHub about
    pull requests itself: prs@4 is the one reader (Copilot on #64). Freshness is per repository, so a
    later fetch of another repository, or a failed one, doesn't make an old state look current (Codex on #64)."""
    path = root / "governance" / "pr-state.jsonl"
    if not path.exists():
        return {}, {}, []
    try:
        m = load_prs()
        lines = m.read_lines(path)
        # latest_by_pr()'s rule (last line per PR wins), with the repository's case folded first, so an
        # older line spelled differently can't stand in for the newest (Copilot on #64).
        latest = {}
        for x in lines:
            if x.get("kind") == "pr" and x.get("schema") in m.READS:
                latest[(str(x.get("repo") or "").lower(), x.get("number"))] = x
    except Exception as e:   # a summary shows what it can
        gaps.append(f"unnoticed unblocks: governance/pr-state.jsonl couldn't be read ({str(e).splitlines()[0] if str(e) else e})")
        return {}, {}, []
    ok_at, ok_line = {}, {}
    for x in lines:   # a repository's freshness is its last fetch that lists it under ok (docs/prs.md)
        if x.get("kind") == "fetch":
            for r in x.get("ok") or []:
                if (x.get("ts") or "") >= ok_at.get(str(r).lower(), ""):
                    ok_at[str(r).lower()], ok_line[str(r).lower()] = x.get("ts") or "", (x, r)
    # A PR whose own re-read failed inside that otherwise successful fetch wasn't confirmed by it.
    failed_pr = set()
    for rl, (x, r) in ok_line.items():
        cov = (x.get("coverage") or {}).get(r) if isinstance(x.get("coverage"), dict) else None
        for q in (cov or {}).get("partial") or [] if isinstance(cov, dict) else []:
            if isinstance(q, dict) and q.get("number") is not None and q.get("what") in ("pull request", "repository"):
                failed_pr.add((rl, q.get("number")))
    try:
        rgaps = m.read_gaps(lines)
    except Exception:
        rgaps = []
    return latest, {r: (t, failed_pr) for r, t in ok_at.items()}, rgaps


def unblocks(root, gaps, now=None):
    """Handoffs waiting (input-required, auth-required, legacy blocked) or working whose latest log note names something that has since
    cleared: a handoff now completed or cancelled, or a pull request merged or closed, after the
    note was written. Read-only. One row per handoff: (alias, state, holder, [(ref, how, at, hours,
    named)], [refs confirmed still open], [refs whose state wasn't known]); `named` is True when the
    note names it after a dependency marker. A thing cleared before the note was written was context,
    not the blocker. Handoffs are found anywhere under handoffs/ but outputs/ (archived ones count, as
    in handoff.py). Pull requests come from the recorded state, so they are only as current as the
    last `relay-kit prs` run; a ref it doesn't cover is unknown, never open."""
    d = root / "handoffs"
    if not d.is_dir():
        return []
    led = load_ledger()
    now = now or dt.datetime.now(dt.timezone.utc)
    parsed = {}
    for p in sorted(d.rglob("HO-*.md")):
        rel = p.relative_to(d)
        if rel.parts[0] == "outputs" or not re.match(rf"^{led.ID_PAT}\.md$", p.name) or p.is_symlink():
            continue
        try:
            meta, log, _, _ = led.parse_text(p.read_text(encoding="utf-8"), p.stem)
        except led.Refused:
            continue   # handoffs() and handoff.py check say so
        parsed[meta.get("id", p.stem)] = (meta, log)
    owner = None
    try:
        owner = (github_repo(root, [], "") or "").split("/")[0] or None
    except Exception:   # a summary shows what it can
        owner = None
    recorded, ok_at, rgaps = recorded_prs(root, gaps)
    seen_repos = set()
    by_name = {}
    for (full, n) in recorded:
        by_name.setdefault((full.split("/")[-1], n), []).append(full)
    unread, missing = [], []

    def pr_line(o, repo, n):
        """The recorded line for a PR ref, or None (noted in unread) when the record doesn't cover it."""
        if o:
            hit = recorded.get((f"{o}/{repo}".lower(), n))
        else:
            fulls = by_name.get((repo.lower(), n), [])
            if owner and f"{owner}/{repo}".lower() in fulls:
                fulls = [f"{owner}/{repo}".lower()]
            hit = recorded.get((fulls[0], n)) if len(fulls) == 1 else None
        if hit is None:
            unread.append(f"{o + '/' if o else ''}{repo}#{n}")
        else:
            seen_repos.add(str(hit.get("repo") or "").lower())
        return hit

    rows = []
    for hid, (meta, log) in parsed.items():
        st = led.canon(meta.get("status"))
        if st not in led.WAITING | {"working"} or not log:
            continue
        last = log[-1]
        note = last.get("note", "")
        wrote = led.parse_utc(last.get("at", ""))
        if wrote is None:
            continue
        hos, prs = blocker_refs(note)
        cleared, still, unknown = [], [], []
        for h in hos:
            if h == hid:
                continue
            if h not in parsed:
                unknown.append(f"{led.alias(h)} (not in this folder)")
                missing.append(f"{led.alias(h)} (named by {led.alias(hid)})")
                continue
            m2, l2 = parsed[h]
            # @1/@2 records end in 'cancelled'; compare @3 names (Codex on #70)
            done = [e for e in l2 if led.canon(e.get("state")) in led.FINAL]
            t = led.parse_utc(done[-1].get("at", "")) if done and led.canon(m2.get("status")) in led.FINAL else None
            if t is not None and t > wrote:
                cleared.append((led.alias(h), led.canon(m2.get("status")), t, is_named(note, h)))
            elif t is None:
                still.append(f"{led.alias(h)} {m2.get('status', '?')}")
        for o, repo, n in prs:
            name = f"{o + '/' if o else ''}{repo}#{n}"
            x = pr_line(o, repo, n)
            if x is None:
                unknown.append(f"{name} (not in the recorded PR state)")
                continue
            how, at = (("merged", x.get("merged_at")) if x.get("state") == "merged" or x.get("merged_at")
                       else ("closed unmerged", x.get("closed_at")) if x.get("state") == "closed" else (None, None))
            t = led.parse_utc(at) if at else None
            if how and t is not None and t > wrote:
                cleared.append((name, how, t, pr_named(note, (o, repo, n))))
            elif how is None:
                rl = str(x.get("repo") or "").lower()
                at_ok, failed_pr = ok_at.get(rl, (None, set()))
                if at_ok and (rl, n) in failed_pr:
                    unknown.append(f"{name} (recorded open; its re-read failed in the newest fetch, {at_ok})")
                elif at_ok:
                    still.append(f"{name} open as of {at_ok}")
                else:
                    unknown.append(f"{name} (recorded open, but no successful fetch of its repository is recorded)")
        if cleared:
            rows.append((led.alias(hid), st, meta.get(HOLDER.get(st, "to"), "?"),
                         [(r, how, t.strftime("%Y-%m-%dT%H:%MZ"), (now - t).total_seconds() / 3600, nm) for r, how, t, nm in cleared],
                         still, unknown))
    if unread:
        gaps.append("unnoticed unblocks: pull requests named as blockers that the recorded PR state doesn't cover "
                    "(run `relay-kit prs` for their repositories): " + ", ".join(dict.fromkeys(unread)))
    if missing:
        gaps.append("unnoticed unblocks: handoffs named as blockers aren't in this folder, so whether they cleared "
                    "wasn't checked: " + ", ".join(dict.fromkeys(missing)))
    if not (root / "governance" / "pr-state.jsonl").exists():
        gaps.append("unnoticed unblocks: no governance/pr-state.jsonl, so pull requests named as blockers weren't checked "
                    "(run `relay-kit prs`); none listed doesn't mean none cleared")
    for r in sorted(seen_repos):
        gaps.append(f"unnoticed unblocks: {r} pull requests are as `relay-kit prs` last read them ({ok_at.get(r, (None,))[0] or 'no successful fetch'})")
        for g in rgaps:   # failed or partial reads of a repository a blocker lives in
            if g.lower().startswith(r + ":") or g.lower().startswith(r + "#"):
                gaps.append(f"unnoticed unblocks: {g}")
    return sorted(rows, key=lambda r: (not any(c[4] for c in r[3]), -max(c[3] for c in r[3])))


def short_actors(lines):
    """Shorten 'lane#session' to the shortest session prefix (at least 4) unique among the
    sessions seen. Two sessions with near-identical ids (01GZK2Kne8, 01GZK2KmPh) stay apart."""
    sessions = set(re.findall(r"[\w.-]+:[\w.-]+#([\w-]+)", " ".join(lines)))
    def short(sid):
        for n in range(4, len(sid) + 1):
            if not any(o != sid and o.startswith(sid[:n]) for o in sessions):
                return sid[:n]
        return sid
    return [re.sub(r"([\w.-]+:[\w.-]+)#([\w-]+)", lambda m: m.group(1) + "#" + short(m.group(2)), l) for l in lines]


def github_repo(root, gaps, what):
    """owner/name of root's origin on GitHub, or None (with a note in gaps)."""
    try:
        url = git(root, "remote", "get-url", "origin")
    except (RuntimeError, FileNotFoundError):
        gaps.append(f"no git remote at {root}: {what} weren't read")
        return None
    m = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?$", url)
    if not m:
        gaps.append(f"{root}'s remote isn't on GitHub: {what} weren't read")
        return None
    return m.group(1)


def api(path):
    gh = os.environ.get("RELAYKIT_GH", "gh")
    p = subprocess.run([gh, "api", path], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    if p.returncode:
        raise RuntimeError(p.stderr.strip() or "gh api failed")
    return json.loads(p.stdout)


def ci_state(root, gaps):
    """(summary line, needs-attention line or None) for the checks on the shared branch."""
    repo = github_repo(root, gaps, "checks on the shared branch")
    if not repo:
        return None, None
    try:
        branch = api(f"repos/{repo}").get("default_branch", "main")
        runs = api(f"repos/{repo}/actions/runs?branch={branch}&event=push&per_page=20").get("workflow_runs", [])
        issues = [i for i in api(f"repos/{repo}/issues?state=open&per_page=50")
                  if i.get("title") == ALERT_TITLE and "pull_request" not in i]
    except (RuntimeError, FileNotFoundError, ValueError, subprocess.TimeoutExpired) as e:
        gaps.append(f"checks on the shared branch weren't read ({str(e).splitlines()[0] if str(e) else e})")
        return None, None
    neutral = {"cancelled", "skipped", "neutral", "stale"}   # superseded or not run: neither red nor green
    flows = {}
    for r in runs:   # newest first; each workflow is its own check
        if r.get("status") == "completed" and r.get("conclusion") not in neutral:
            flows.setdefault(r.get("name") or str(r.get("workflow_id")), []).append(r)
    if not flows:
        return f"{repo}@{branch}: no finished runs", None
    lines, red_since = [], None
    for name, done in sorted(flows.items()):
        streak = []
        for r in done:
            if r.get("conclusion") == "success":
                break
            streak.append(r)
        if not streak:
            lines.append(f"{name}: green at {done[0].get('head_sha', '')[:7]}")
            continue
        since = streak[-1]
        red_since = min(red_since or since.get("created_at", ""), since.get("created_at", ""))
        lines.append(f"{name}: RED, {len(streak)} failing run(s) in a row since {since.get('head_sha', '')[:7]} "
                     f"({since.get('created_at', '')})")
    head = f"{repo}@{branch}"
    if red_since is None:
        extra = f"\nalert issue #{issues[0]['number']} still open (close it)" if issues else ""
        return head + ": all green\n" + "\n".join(lines) + extra, None
    ack = None
    if issues:
        n = issues[0]["number"]
        try:
            comments = api(f"repos/{repo}/issues/{n}/comments?per_page=100")
        except (RuntimeError, ValueError, subprocess.TimeoutExpired):
            comments = []
            gaps.append(f"alert issue #{n}'s comments weren't read: acknowledgement unknown")
        people = [c.get("user", {}).get("login") for c in comments
                  if c.get("user", {}).get("type") != "Bot" and c.get("created_at", "") >= red_since]   # only this incident
        ack = people[-1] if people else None
        alert = f"issue #{n}, " + (f"acknowledged by {ack}" if ack else "NOT ACKNOWLEDGED")
    else:
        alert = "no alert issue"
    text = f"RED {head}: {alert}\n" + "\n".join(lines)
    return text, None if ack else f"RED {head}: failing checks nobody has acknowledged (comment on the alert issue)"


def norm(x, base=None):
    """One spelling per thing: URLs lose query, fragment and trailing slash; paths use '/', lose
    './' and a trailing '/', and are made relative to `base` (the repo) when absolute inside it."""
    x = x.strip()
    if "://" in x:
        return re.sub(r"[?#].*$", "", x).rstrip("/")
    x = x.replace("\\", "/")
    if base is not None and x.startswith("/"):
        try:
            x = pathlib.Path(x).resolve().relative_to(pathlib.Path(base).resolve()).as_posix()
        except ValueError:
            pass
    while x.startswith("./"):
        x = x[2:]
    return x.rstrip("/") or x


GLOB = re.compile(r"[*?\[]")


def covers(pat, path):
    return path == pat or fnmatch.fnmatch(path, pat) or path.startswith(pat + "/")


def hits(targets, patterns, files=()):
    """Pairs (target, pattern) that overlap, both already normalized. Glob semantics are fnmatch's:
    '*' also crosses '/', so 'src/*.py' covers 'src/a/b.py'. A plain directory covers what's under
    it ('src/db' and 'src/db/models.py'), and a glob matches a target in either direction. Two
    globs overlap when some tracked file (`files`) matches both ('src/*/client.py' and
    'src/api/*.py' meet at src/api/client.py)."""
    out = []
    for t in targets:
        for pat in patterns:
            if covers(pat, t) or covers(t, pat) or (GLOB.search(t) and GLOB.search(pat)
                                                     and any(covers(t, f) and covers(pat, f) for f in files)):
                out.append((t, pat))
    return out


def gh_pages(path, per_page, gaps, what, max_pages=5):
    """All pages of a list endpoint, up to max_pages; says so in gaps if it stopped early."""
    rows = []
    for page in range(1, max_pages + 1):
        got = api(f"{path}{'&' if '?' in path else '?'}per_page={per_page}&page={page}")
        if not isinstance(got, list):
            raise ValueError(f"unexpected reply for {path}")
        rows += got
        if len(got) < per_page:
            return rows
    gaps.append(f"{what}: stopped after {max_pages * per_page}; later ones weren't read")
    return rows


def overlaps(root, targets, me, lane, hours, repo_dir, gaps):
    """Warnings, never locks (HO-20261101-005 item 5): other work already touching `targets`.
    Reads only what exists: handoffs' optional `touches:`, recent log lines from other sessions in
    the lane, open pull requests' changed files, and failing checks where the files live."""
    led, out = load_ledger(), []
    parsed_me = led.parse_actor(me) if me else None
    me_base, me_session = (parsed_me[0], parsed_me[1] or "") if parsed_me else ("", "")
    if not lane and parsed_me and not parsed_me[1]:
        gaps.append(f"RELAYKIT_ACTOR '{me}' has no session (the owner?): pass --lane to look for other sessions")
        me_base = ""
    lane = (lane or me_base).lower()
    targets = [norm(t, repo_dir) for t in targets]
    tracked = []
    if any(GLOB.search(t) for t in targets):   # two globs can only be compared through real files
        try:
            tracked = git(repo_dir, "ls-files").splitlines()
        except (RuntimeError, FileNotFoundError):
            gaps.append("not a git checkout: a glob was compared with other globs by text only")
    recent = {}
    d = root / "handoffs"
    for p in sorted(d.glob("HO-*.md")) if d.is_dir() else []:
        try:
            meta, log, _, _ = led.parse_text(p.read_text(encoding="utf-8"), p.stem)
        except led.Refused:
            continue
        hid, st = led.alias(meta.get("id", p.stem)), led.canon(meta.get("status", "?"))
        if st not in led.FINAL and meta.get("touches"):
            pats = [norm(x) for x in meta["touches"].split(",") if x.strip()]   # a comma inside a URL: write %2C
            for t, pat in hits(targets, pats, tracked):
                holder = meta.get(HOLDER.get(st, "to"), "?")
                out.append(f"handoff {hid} ({st}, with {holder}) touches {pat}: matches {t}")
        for e in log:
            parsed = led.parse_actor(e["actor"])
            if not parsed or not lane or parsed[0] != lane or not parsed[1] or parsed[1] == me_session:
                continue
            age = age_hours(e["at"])
            if age is not None and age <= hours:
                key = f"{parsed[0]}#{parsed[1]}"
                if key not in recent or e["at"] > recent[key][0]:
                    recent[key] = (e["at"], hid, e["state"])
    for key, (at, hid, st) in sorted(recent.items()):
        out.append(f"another session in {lane} was active {at}: {key} ({st} on {hid})")
    if not lane:
        gaps.append("no lane (set RELAYKIT_ACTOR or --lane): other sessions in the lane weren't looked for")
    files = [t for t in targets if "://" not in t]
    repo = github_repo(repo_dir, gaps, "open pull requests") if files else None
    if repo:
        try:
            for pr in gh_pages(f"repos/{repo}/pulls?state=open", 50, gaps, f"open pull requests on {repo}"):
                changed = [f.get("filename", "") for f in gh_pages(f"repos/{repo}/pulls/{pr['number']}/files", 100, gaps,
                                                                   f"files of PR #{pr['number']}")]
                for t, pat in hits(files, changed, tracked):
                    out.append(f"open PR #{pr['number']} ({pr.get('user', {}).get('login', '?')}: {pr.get('title', '')[:60]}) changes {pat}")
        except (RuntimeError, ValueError, TypeError, KeyError, FileNotFoundError, subprocess.TimeoutExpired) as e:
            gaps.append(f"open pull requests on {repo} weren't read ({str(e).splitlines()[0] if str(e) else e})")
    if repo and pathlib.Path(repo_dir).resolve() != pathlib.Path(root).resolve():   # root's own checks are shown above
        line, _ = ci_state(repo_dir, gaps)
        if line and line.startswith("RED"):
            out.append(f"failing checks where these files live: {line.splitlines()[0]}")
    return out


def shared_tip(d, fetch):
    """origin's default branch after an optional fetch, else HEAD: what's shared, not only what's pulled.
    Unlike handoff.shared_ref, it resolves to a commit and falls back past a dangling origin/HEAD,
    because a summary should show something rather than refuse; so it stays separate."""
    if fetch:
        try:
            git(d, "fetch", "--quiet", "origin")
        except (RuntimeError, FileNotFoundError):
            pass
    for ref in ("refs/remotes/origin/HEAD", "refs/remotes/origin/main", "refs/remotes/origin/master"):
        try:
            return git(d, "rev-parse", "--verify", "--quiet", ref)
        except RuntimeError:
            continue
    return "HEAD"


def changed_since(root, since, me, gaps, fetch=True, cap=15):
    """What other lanes learned since you last looked (RQ-07): ledger lines added, and contract
    versions that moved in the kit, read from the shared branches (fetched), not only what's
    been pulled. 'Since' is --since (a date, meaning from the start of that day UTC, or a git
    revision), else this session's last commit in the records, found by its Claude-Session /
    Codex-Session trailer ending in the session id."""
    led = load_ledger()
    parsed = led.parse_actor(me) if me else None
    try:
        git(root, "rev-parse", "--git-dir")
        git(root, "rev-parse", "--verify", "--quiet", "HEAD")
    except (RuntimeError, FileNotFoundError):
        gaps.append("records aren't a git checkout with commits: 'changed since' wasn't worked out")
        return None
    tip = shared_tip(root, fetch)
    base, label, kit_since = None, None, None   # kit_since: a time boundary for the kit, when one was asked for
    if since:
        if re.match(r"^\d{4}-\d{2}-\d{2}$", since):
            try:
                dt.date.fromisoformat(since)
            except ValueError:
                gaps.append(f"--since {since}: not a real date")
                return None
            kit_since = f"{since[:10]}T00:00:00Z"
            base = git(root, "log", tip, f"--until={kit_since}", "-1", "--format=%H").strip() or None
            label = f"since {since}"
        else:
            try:
                base, label = git(root, "rev-parse", since), f"since {since}"
            except RuntimeError:
                gaps.append(f"--since {since}: not a date or revision here")
                return None
    elif parsed and parsed[1]:
        try:
            sid = re.escape(parsed[1])   # a trailer line ending in the id, not any mention of it
            base = git(root, "log", "HEAD", "-E", f"--grep=^[A-Za-z-]*Session: (\\S*[/_:])?{sid}$", "-1", "--format=%H") or None
        except RuntimeError:
            base = None
        label = "since this session's last commit"
        if not base:
            gaps.append(f"no commit by session {parsed[1][:12]} found: 'changed since' used the last 48 hours")
            base = git(root, "log", tip, "--until=48 hours ago", "-1", "--format=%H").strip() or None
            label, kit_since = "in the last 48 hours", "48 hours ago"
    else:
        return None
    if not base:   # compare with an empty tree, so lines added in the very first commit count too
        gaps.append("no earlier commit to compare with: 'changed since' covers the whole history")
        base = git(root, "hash-object", "-t", "tree", "/dev/null")
    try:
        when = git(root, "log", "-1", "--format=%cI", base)
    except RuntimeError:   # the empty tree isn't a commit
        when = ""
    when = when or "1970-01-01T00:00:00Z"
    if when.startswith("1970"):
        label = "since the first commit"
    lines = [f"{label} ({base[:7]}, {when[:16]})" if not when.startswith("1970") else label]
    for name, key in (("decisions.jsonl", "decision"), ("challenges.jsonl", "concern")):
        try:
            diff = git(root, "diff", f"{base}..{tip}", "--", f"governance/{name}")
        except RuntimeError:
            continue
        added = []
        for l in diff.splitlines():
            if l.startswith("+") and not l.startswith("+++") and l[1:].strip().startswith("{"):
                try:
                    added.append(json.loads(l[1:]))
                except ValueError:
                    pass
        for r in added[:cap]:
            text = r.get(key) or r.get("resolution") or ""
            lines.append(f"{r.get('id')} {r.get('status', '')}: {text[:160]}")
        if len(added) > cap:
            lines.append(f"... and {len(added) - cap} more in {name}")
    try:
        if SKILLS != KIT / "skills":   # installed: never run git in whatever repository encloses site-packages
            raise RuntimeError("installed package")
        moved = git(KIT, "log", shared_tip(KIT, fetch), f"--since={kit_since or when}", "--format=%h %s", "--", "governance/contracts.json")
        lines += [f"contract change in the kit: {m}" for m in moved.splitlines()]
    except (RuntimeError, FileNotFoundError):
        gaps.append("the kit isn't a git checkout (installed package): contract changes weren't looked for")
    if len(lines) == 1:
        lines.append("nothing new in the ledgers or contracts")
    return lines


def jsonl(path, gaps):
    if not path.exists():
        gaps.append(f"no {path.parent.name}/{path.name}")
        return []
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                gaps.append(f"{path.name}:{n} isn't JSON; run tools/check_ledgers.py")
    return out


class Shown:
    """stdout that prints control characters as escapes (handoff.py's shown()). The report quotes text others wrote:
    handoff titles and notes, ledger lines, commit subjects and pull request titles from GitHub."""
    def __init__(self, out, shown):
        self.out, self.shown = out, shown

    def write(self, s):
        return self.out.write(self.shown(s))

    def __getattr__(self, name):
        return getattr(self.out, name)


def main():
    out = sys.stdout
    sys.stdout = Shown(out, load_ledger().shown)
    try:
        report()
    finally:
        sys.stdout = out


def report():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=pathlib.Path, default=None,
                    help="the records folder (default: found as handoff.py finds it: ., $RELAYKIT_RECORDS, or the clone its relay-kit.json names)")
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--no-ci", action="store_true", help="don't read the checks on the shared branch")
    ap.add_argument("--no-unblocks", action="store_true", help="skip the unnoticed-unblocks section")
    ap.add_argument("--since", help="show ledger lines and contract changes since this date (from the start of that day, UTC) or git revision "
                    "(default: this session's last commit, when RELAYKIT_ACTOR names a session)")
    ap.add_argument("--touching", nargs="+", metavar="PATH",
                    help="before writing: warn about other work touching these paths, globs or thread URLs")
    ap.add_argument("--lane", help="your lane (default: from RELAYKIT_ACTOR)")
    ap.add_argument("--repo", type=pathlib.Path, help="the checkout the --touching paths are in (default: --root)")
    ap.add_argument("--hours", type=float, default=4, help="how recent another session's activity counts (default 4)")
    ap.add_argument("--stale-hours", type=float, default=24,
                    help="flag a sent handoff with no acknowledgement, or a claimed one with no log line, after this long (default 24)")
    a = ap.parse_args()
    root, gaps = a.root, []
    led = load_ledger()
    # Without --root: the records handoff.py would find (a folder with governance/ and no handoffs/,
    # such as a kit checkout, is read as before), with the same owner check. --root is used as given.
    try:
        if root is None and not (pathlib.Path("governance").is_dir() and not pathlib.Path("handoffs").is_dir()):
            d, where = led.find_dir(None)
            root = d.parent
            led.records_folder(d, "governance")
            for n in (where, led.load_config(d)[2]):
                if n:
                    print(f"note: {n}", file=sys.stderr)
        elif root is None:   # governance/ and no handoffs/ here (a kit checkout): read as before
            led.records_folder(pathlib.Path("handoffs"), "governance")
    except led.Refused as e:
        sys.exit(f"refused: {e}")
    root = root or pathlib.Path(".")

    fresh = freshness(root, not a.no_fetch, gaps)
    rows, unauthorized = handoffs(root, gaps, a.stale_hours)
    decs = jsonl(root / "governance" / "decisions.jsonl", gaps)
    chs = jsonl(root / "governance" / "challenges.jsonl", gaps)
    lc = load_lifecycle()
    res = lc.resolve(decs, chs, lc.ledger_as_of(decs))   # relay-kit/lifecycle@1: only effective transitions close anything
    waiting = [x for x in decs if (res["decisions"].get(x.get("id")) or {}).get("open")]
    open_ch = [x for x in chs if (res["challenges"].get(x.get("id")) or {}).get("open")]

    print("== Copy")
    print(fresh or "unknown (see Not checked)")
    if fresh and re.search(r": [1-9]\d* behind", fresh):
        print("PULL FIRST: this copy is behind; ids and states below may be stale.")

    ci, alarm = (None, None) if a.no_ci else ci_state(root, gaps)
    print("\n== Checks on the shared branch")
    print(ci or ("not read (--no-ci)" if a.no_ci else "unknown (see Not checked)"))

    me = os.environ.get("RELAYKIT_ACTOR", "")
    try:
        changes = changed_since(root, a.since, me, gaps, fetch=not a.no_fetch)
    except (RuntimeError, FileNotFoundError, IndexError) as e:
        changes = None
        gaps.append(f"'changed since' failed ({str(e).splitlines()[0] if str(e) else e})")
    if changes:
        print(f"\n== Changed {changes[0]}")
        for l in changes[1:]:
            print(l)
    led = load_ledger()
    hd = root / "handoffs"
    stale = led.stale_format_warnings(hd, sorted(hd.glob("HO-*.md"))) if hd.is_dir() else []
    if stale:
        print("\n== Out of date")
        for w in short_actors([w.replace(": warning: ", ": ", 1) for w in stale]):
            print(w)

    if a.touching:
        found = overlaps(root, a.touching, me, a.lane, a.hours, a.repo or root, gaps)
        print(f"\n== Overlaps with {', '.join(a.touching)}  (warnings, not locks)")
        for l in short_actors(found) or ["none found in what was checked"]:
            print(l)
        if found:
            print("Before writing: tell the other side (an FYI handoff, or a note to the owner) or wait.")

    print("\n== Waiting on the owner")
    if alarm:
        print(alarm)
    for x in waiting:
        alt = res["decisions"][x.get("id")]["proposed_replacements"]
        print(f"{x.get('id')}  proposed {x.get('date', '')}: {x.get('decision', '')}"
              + (f"  [proposed replacement: {', '.join(alt)}]" if alt else ""))
    for u in unauthorized:
        print(f"{u}  (Authorized not filled: nobody may start it)")
    # DEC-255 made "waiting on the owner" a query on auth-required; handoff@4 (DEC-265 (6)) names who a wait is on in
    # waiting_on, so it is the waits whose waiting_on is the owner, and auth-required only where the field is absent
    auth_required = [r for r in rows if r[4].endswith("WAITS ON THE OWNER")]
    for st, hid, holder, title, last in auth_required:
        print(f"{hid}  {title}  ({st}: waits on the owner" + ("'s authorization)" if st == "auth-required" else ")"))
    if not waiting and not unauthorized and not alarm and not auth_required:
        print("nothing")

    print("\n== Open handoffs")
    order = ["auth-required", "input-required", "blocked", "prepared", "submitted", "acknowledged", "working"]
    lines = [f"{hid}  {st:<12} with {holder}  {title}  [last: {last}]"
             for st, hid, holder, title, last in sorted(rows, key=lambda r: (order.index(r[0]) if r[0] in order else 9, r[1]))]
    for l in short_actors(lines):
        print(l)
    if not rows:
        print("none")

    if not a.no_unblocks:
        ub = unblocks(root, gaps)
        print("\n== Unnoticed unblocks  (something the last log line names has cleared since; read-only)")
        print("[named] = after 'waiting on', 'blocked on', 'needs' ...; [mentioned] = may not be the blocker; "
              "'closed unmerged' / 'canceled' ended without delivering: reassess")
        lines = [f"{hid}  {st:<12} with {holder}: "
                 + "; ".join(f"[{'named' if nm else 'mentioned'}] {r} {how} {at}, {h:.1f} h ago" for r, how, at, h, nm in cleared)
                 + (f"  (still open: {', '.join(still)})" if still else "")
                 + (f"  (not known: {', '.join(unknown)})" if unknown else "")
                 for hid, st, holder, cleared, still, unknown in ub]
        for l in short_actors(lines):   # shortened together, so two holders' sessions stay apart
            print(l)
        if ub:
            print("Next: the holder resumes it, or says why the named item wasn't the real blocker (DEC-237: dispatch the next step).")
        else:
            print("none found")

    print("\n== Open challenges")
    for x in open_ch:
        print(f"{x.get('id')}  {x.get('concern', '')}")
    if not open_ch:
        print("none")

    if res["problems"] or res["conflicts"]:
        print("\n== Ledger transitions that take no effect, and conflicts")
        for pr in res["problems"]:
            print(f"{pr['id']}  {pr['message']}")
        for c in res["conflicts"]:
            print(f"{c['id']}  closed by more than one line: {', '.join(c['closed_by'])} ({', '.join(c['statuses'])})")

    print("\n== Coordination index")
    idx = root / "threads.md"
    if idx.exists():
        changes = [l for l in idx.read_text(encoding="utf-8").splitlines() if l.startswith("- 20")]
        print(f"{idx}" + (f"\nlast change: {changes[-1][:200]}" if changes else ""))
    else:
        gaps.append("no threads.md: which thread does what isn't recorded here")
        print("none")

    print("\n== Not checked")
    for g in gaps + ["whether chat threads still exist or match their records (only files are read)",
                     "work done outside the records (local Codex threads, unfiled chat replies)"]:
        print(f"- {g}")
    sys.exit(0)


if __name__ == "__main__":
    main()
