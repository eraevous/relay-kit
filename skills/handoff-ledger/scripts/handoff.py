#!/usr/bin/env python3
"""Handoff ledger: create, update, list and check handoff files. Standard library only.

Enforced: handoff ids (@2: HO-LANE-YYYYMMDD-XXXX, random; @1: HO-YYYYMMDD-NNN) that
stay inside --dir; `new` never replaces a file or reuses an id seen in git; the whole
record is validated before any update, with the same rules `check` uses; legal state
moves (blocked returns only to the state it left); notes on sent, blocked and completed,
and on any step recorded --for someone else; an output file before completed;
single-line fields; log lines written inside the Log section; an actor with a session
(claude:aec#01N4) on @2 files; and --by (or --for) matching the declared sender (sent)
or recipient (acknowledged, in_progress, completed), or the owner. The owner's name
comes from <dir>/.ledger.json ({"owner": "sam"}); without it, from this copy of the skill's
relay-kit.json (written by `relay-kit zips --owner`); without either it is "owner", with a
warning. Without --dir, the folder is ./handoffs, else $RELAYKIT_RECORDS/handoffs, else a
clone of the records repository relay-kit.json names (see find_dir).
Writes go to a temporary file that replaces the original, under a lock file, and are
refused if the file changed after it was read, if --expect names another state, or if
the upstream branch changed the file since this clone last pulled. `check --base REF`
also refuses removed handoffs and changed headers or earlier log lines. --at records an event after
the fact: the log line takes the event time and the note gets "(recorded <now>)".

Not enforced: --by is a typed name, not an authenticated identity; an output
file existing says nothing about its quality; hand edits bypass all of this
(`check` finds the damage afterwards). The log is append-only by convention,
not tamper-evident: keep the folder in git for a history you can trust."""
import argparse, datetime as dt, hashlib, json, os, pathlib, re, secrets, subprocess, sys

CONTRACT = "relay-kit/handoff@2"   # file format, states and log line; see governance/contracts.json
READS = {"relay-kit/handoff@1", CONTRACT}   # @1 files stay valid; files without `contract:` are @1
STATES = ["prepared", "sent", "acknowledged", "in_progress", "completed", "blocked", "cancelled"]
FINAL = {"completed", "cancelled"}
NEXT = {
    "prepared": {"sent"},
    "sent": {"acknowledged"},
    "acknowledged": {"in_progress", "completed"},
    "in_progress": {"completed"},
}
NOTE_REQUIRED = {"sent", "blocked", "completed"}
SENDER_STATES = {"sent"}
RECIPIENT_STATES = {"acknowledged", "in_progress", "completed"}
OWNER = "owner"   # replaced at start-up by <dir>/.ledger.json, if present


LANES = {}       # optional lane codes from <dir>/.ledger.json: {"lanes": {"relay-kit": "RK"}}


SKILL_CONFIG = pathlib.Path(__file__).resolve().parent.parent / "relay-kit.json"
OWNER_RE = r"[\w.:-]+"


def repo_id(url):
    """(host, "owner/name"), lowercased, from owner/name (GitHub), https://host/owner/name[.git],
    ssh://[user@]host/owner/name[.git] or user@host:owner/name[.git]; None for anything else.
    The whole path must be owner/name, so https://elsewhere/x/owner/name doesn't pass for it."""
    u = str(url).strip()
    m = (re.fullmatch(r"([\w.-]+/[\w.-]+)", u) and ("github.com", u)
         or (lambda m: m and (m.group(1), m.group(2)))(re.fullmatch(r"(?:https|ssh)://(?:[^@/]+@)?([\w.-]+(?::\d+)?)/([\w.-]+/[\w.-]+)/?", u))
         or (lambda m: m and (m.group(1), m.group(2)))(re.fullmatch(r"[\w.-]+@([\w.-]+):([\w.-]+/[\w.-]+)/?", u)))
    if not m:
        return None
    host, path = m[0].lower().rstrip("."), m[1].lower()
    path = path[:-4] if path.endswith(".git") else path
    return (host, path) if all(x not in ("", ".", "..") for x in path.split("/")) else None


def skill_config():
    """Settings written into this copy of the skill by `relay-kit zips --owner --records`:
    {"owner": "sam", "records": "sam/records"}. {} when there are none. A file that's there
    but malformed is refused, never ignored."""
    if not SKILL_CONFIG.exists():
        return {}
    try:
        data = json.loads(SKILL_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise Refused(f"{SKILL_CONFIG} can't be read as JSON ({e})")
    if not isinstance(data, dict) or not all(isinstance(data.get(k, ""), str) for k in ("owner", "records")):
        raise Refused(f'{SKILL_CONFIG} must be a JSON object like {{"owner": "sam", "records": "sam/records"}}')
    owner, records = data.get("owner", "").strip().lower(), data.get("records", "").strip()
    if owner and not re.fullmatch(OWNER_RE, owner):
        raise Refused(f"{SKILL_CONFIG}: owner must be one word (letters, digits, . : - _)")
    if records and not repo_id(records):
        raise Refused(f"{SKILL_CONFIG}: records must be owner/name or a repository URL ending in owner/name")
    return {"owner": owner, "records": records}


def clone_of(folder, records):
    """True when folder is the top of a git clone whose origin is the records repository. (Content
    inside some other repository, or a plain folder, doesn't count: git would report the
    enclosing repository's origin for it.)"""
    env = {k: v for k, v in os.environ.items()   # git in a hook or alias would otherwise follow GIT_DIR
           if not (k.startswith("GIT_") and k not in ("GIT_SSH", "GIT_SSH_COMMAND", "GIT_ASKPASS"))}
    def g(*a):
        try:
            return subprocess.run(["git", "-C", str(folder), *a], capture_output=True, text=True, encoding="utf-8", timeout=30, env=env)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise Refused(f"git couldn't check {folder} ({e}); pass --dir to choose the records yourself")
    top = g("rev-parse", "--show-toplevel")
    if top.returncode or pathlib.Path(top.stdout.strip()).resolve() != pathlib.Path(folder).resolve():
        return False
    url = g("remote", "get-url", "origin")
    return url.returncode == 0 and repo_id(url.stdout) is not None and repo_id(url.stdout) == repo_id(records)


def contained(d, root):
    """handoffs/ and everything in it resolve inside root: no symlink leads out of the clone."""
    return d.resolve().is_relative_to(root) and all(p.resolve().is_relative_to(root) for p in d.rglob("*"))


def records_folder(d, name):
    """<records>/<name> (governance, say) next to a handoffs folder found without --dir, refused
    when it leads out of the records (a symlink): the same containment as handoffs/."""
    root, f = pathlib.Path(d).parent.resolve(), pathlib.Path(d).parent / name
    if f.exists() and not contained(f, root):
        raise Refused(f"{f} leads out of {root} (a symlink); pass the folder explicitly if that's intended")
    return f


def find_dir(given):
    """The handoffs folder to use, and a note saying how it was found (None when it was given or
    is ./handoffs). --dir is used as given: it's the caller's choice. Otherwise: ./handoffs (as
    before); $RELAYKIT_RECORDS/handoffs, which must exist; then, only when this skill's
    relay-kit.json names the records repository, a clone of exactly that repository directly below
    the current folder or the home folder. Without that setting nothing is searched for. With it,
    every implicit choice must be inside a clone of that repository. None or several are refused,
    with what to do; nothing is created."""
    if given is not None:
        return given, None
    records = skill_config().get("records", "")
    def checked(d, note):
        """An implicit choice must be the records this skill names, when it names them: a clone of
        that repository, with handoffs/ really inside it (not a symlink out)."""
        if records:
            root = d.parent.resolve()
            if not clone_of(root, records) or not contained(d, root):
                raise Refused(f"{d.resolve()} isn't in a clone of {records}, the records this copy of the skill "
                              "was made for; run from that clone, or pass --dir to choose a folder yourself")
        return d, note
    here = pathlib.Path("handoffs")
    if here.is_dir():
        return checked(here, None)
    env = os.environ.get("RELAYKIT_RECORDS", "").strip()
    if env:
        d = pathlib.Path(env) / "handoffs"
        if not d.is_dir():
            raise Refused(f"RELAYKIT_RECORDS={env} has no handoffs folder; fix the path or unset it")
        return checked(d, f"records from RELAYKIT_RECORDS: {env}")
    if not records:
        raise Refused("no handoffs folder here. Get the owner's records (a clone of their private records "
                      "repository, or the folder connected from their computer), then run from it, pass "
                      "--dir <records>/handoffs, or set RELAYKIT_RECORDS=<records>")
    found = []
    for base in dict.fromkeys([pathlib.Path.cwd().resolve(), pathlib.Path.home().resolve()]):
        for d in sorted(base.glob("*/handoffs")):
            clone = d.parent
            if d.is_dir() and not clone.is_symlink() and contained(d, clone.resolve()) and clone_of(clone, records):
                found.append(d.resolve())
    found = list(dict.fromkeys(found))
    if len(found) == 1:
        return found[0], f"records found at {found[0].parent} (a clone of {records})"
    if found:
        raise Refused(f"several clones of {records} found (" + ", ".join(str(f.parent) for f in found) + "); pass --dir <one>/handoffs")
    host, path = repo_id(records)
    raise Refused(f"no handoffs folder here. This copy of the skill keeps its records in {records}: clone it "
                  f"into this folder or your home folder (git clone https://{host}/{path}), or in Claude Code on "
                  "the web add that repository to the session; then rerun, or pass --dir <clone>/handoffs")


def load_config(d):
    """(owner, lanes, note). The folder's .ledger.json decides; this skill's relay-kit.json
    stands in when the folder has none, and must agree when it has one."""
    cfg = pathlib.Path(d) / ".ledger.json"
    baked = skill_config().get("owner", "")
    if not cfg.exists():
        if baked:
            return baked, {}, f"{cfg} is missing; the owner is '{baked}', from {SKILL_CONFIG.name}"
        return "owner", {}, (f"warning: {cfg} is missing, so the owner is called 'owner' and nobody's "
                             '--by counts as the owner; add {"owner": "<their name>"} to it')
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
        name = str(data.get("owner", "")).strip().lower()
        lanes = {str(k).lower(): str(v).upper() for k, v in (data.get("lanes") or {}).items()}
    except (ValueError, AttributeError):
        raise Refused(f"{cfg} isn't valid JSON like {{\"owner\": \"sam\"}}")
    if not name or not re.fullmatch(OWNER_RE, name):
        raise Refused(f"{cfg}: owner must be one word (letters, digits, . : - _)")
    if any(not re.fullmatch(r"[A-Z0-9]{1,12}", v) for v in lanes.values()):
        raise Refused(f"{cfg}: lane codes must be 1-12 letters or digits")
    if baked and baked != name:
        raise Refused(f"{cfg} names the owner '{name}', but this copy of the skill was made for '{baked}' "
                      f"({SKILL_CONFIG}); check these are the right records")
    return name, lanes, None


def load_owner(d):
    return load_config(d)[0]
# handoff@1: HO-YYYYMMDD-NNN (sequential per day). handoff@2: HO-<LANE>-YYYYMMDD-<4 random
# Crockford base32>, which two sessions can create offline without colliding (DEC-008, HO-20261101-005).
B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ID_PAT = r"HO-(?:\d{8}-\d{3}|[A-Z0-9]{1,12}-\d{8}-[0-9A-HJKMNP-TV-Z]{4})"
ID_RE = re.compile(rf"^{ID_PAT}$")
ACTOR_RE = re.compile(r"^([\w.:-]+?)(?:#([\w-]+))?(?: for ([\w.:-]+?)(?:#[\w-]+)?)?$")
FOR_STATES = {"sent", "acknowledged", "in_progress", "completed"}   # steps a carrier may record for a party
REQUIRED = ["id", "title", "from", "to", "created", "status", "source", "last_processed"]
SECTIONS = ["Task", "Authorized", "Proposals", "Context", "Expected output", "Limits", "Log"]
LOG_RE = re.compile(r"^- (\S+) \| (\w+) \| ([^|]+?) \|(?: (.*))?$")
TODO_RE = re.compile(r"^\s*(?:- )?TODO\b", re.M)
LOCK = ".handoff.lock"

TEMPLATE = """---
id: {id}
title: {title}
from: {frm}
to: {to}
created: {now}
status: prepared
contract: relay-kit/handoff@2
source: {source}
last_processed: {last}
reply_to: {reply_to}
touches: {touches}
---

## Task
TODO

## Authorized
- TODO: NOT AUTHORIZED YET. A recipient must not start this handoff until this line
  quotes the owner exactly, with a locator (thread URL or message id, date).
- Approving: TODO (the proposal or question their words answer, quoted or linked)
- Permits: TODO (the actions this approval covers)
- Doesn't cover: TODO (or "anything not listed above")

## Proposals
- none

## Context
TODO

## Expected output
Write the result to outputs/{id}.md.

## Limits
TODO

## Log
- {now} | prepared | {by} | drafted
"""


REVIEW_SECTIONS = {   # handoff.py new --review: an adversarial review run without a standing thread (RQ-06)
    "Task": "Review {artifact} against {spec}. Find real defects; don't praise. Assume there are bugs and\n"
            "look for them: wrong behavior, ways around a check, false refusals, tests that would pass\n"
            "with the feature broken.",
    "Context": "You get the artifact and its spec only. The builder's reasoning, conclusions and test\n"
               "results are left out on purpose, so your reading isn't anchored on theirs.",
    "Expected output": "Write the result to outputs/{id}.md: one finding per item, each with where (file and\n"
                       "line, or section), a concrete failing scenario, and a severity (blocking, should-fix,\n"
                       "nit). The requester adds which findings the existing tests had missed.",
    "Limits": "- Read-only: don't change the artifact, push, merge, or contact anyone.\n"
              "- No new standing thread (DEC-013): run in an ephemeral context (a subagent, Copilot's\n"
              "  pull-request review, a ChatGPT Temporary Chat), and the requester files your result.",
}


class Refused(Exception):
    pass


def now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def parse_utc(x):
    """An ISO time with a zone, as UTC; None for anything else (no zone, not a time). Never raises."""
    try:
        t = dt.datetime.fromisoformat(str(x).replace("Z", "+00:00"))
        return t.astimezone(dt.timezone.utc) if t.tzinfo else None
    except (ValueError, OverflowError):   # e.g. 0001-01-01T00:00+01:00 is before the first UTC moment
        return None


def event_time(at, prev_at):
    """Normalize --at to YYYY-MM-DDTHH:MMZ; must be in the past and not before the previous log line."""
    try:
        d = dt.datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        raise Refused(f"--at '{at}' isn't an ISO time like 2026-10-28T17:51Z")
    if d.tzinfo is None:
        raise Refused("--at needs a timezone (end it with Z for UTC)")
    d = d.astimezone(dt.timezone.utc)
    if d > dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=1):
        raise Refused("--at is in the future")
    s = d.strftime("%Y-%m-%dT%H:%MZ")
    if prev_at and s < prev_at:
        raise Refused(f"--at {s} is earlier than the previous log line ({prev_at})")
    return s


def single_line(name, value, allow_pipe=True):
    if value is None:
        return
    # The ledger is read back with str.splitlines(), which also splits on \v, \f, \x1c-\x1e, \x85,
    # U+2028 and U+2029, not only \n and \r. Use the same definition here, so no value that
    # passes can be read back as two lines (relay-kit #22).
    if value and value.splitlines() != [value]:
        raise Refused(f"{name} must be one line (no line or paragraph separators)")
    if not allow_pipe and "|" in value:
        raise Refused(f"{name} can't contain '|'")


def parse_actor(field):
    """'claude:aec#01N4 for codex:aec' -> ('claude:aec', '01N4', 'codex:aec'). A lane names an
    activity; '#session' names who in it acted; 'for X' records X's step on X's behalf."""
    m = ACTOR_RE.match(field.strip())
    if not m:
        return None
    return m.group(1).lower(), m.group(2), (m.group(3) or "").lower() or None


def alias(hid):
    """Display alias: 'AEC K7QF' for HO-AEC-20261101-K7QF; @1 ids are shown as they are."""
    m = re.match(r"^HO-([A-Z0-9]{1,12})-\d{8}-(\w{4})$", hid)
    return f"{m.group(1)} {m.group(2)}" if m else hid


def actor_error(meta, state, field, contract, note):
    """Rules on the log line's actor field. Name matching only: typed names, not identities."""
    parsed = parse_actor(field)
    if not parsed:
        return f"actor '{field}' isn't 'name', 'name#session' or 'name#session for party'"
    base, session, party = parsed
    if contract == "relay-kit/handoff@1":
        if session or party:   # @1 readers know neither; keep @1 files readable by them
            return f"'{field}': this is a handoff@1 file; write '--by <name>' without a session or --for (or as the owner)"
    elif base != OWNER and not session:
        return f"actor '{field}' needs a session ('{base}#<session id>'): a lane isn't an actor"
    if party:
        if party == base:
            return f"'{field}': 'for' names someone else"
        if party == OWNER:
            return f"'{field}': nobody records a step for the owner; the owner's steps are written as the owner"
        if state not in FOR_STATES:
            return f"'{state}' can't be recorded for someone else (only {', '.join(sorted(FOR_STATES))})"
        if not (note or "").strip():
            return f"'{field}' records a step for {party}; the note must say where that came from"
    return owner_error(meta, state, party or base)


def owner_error(meta, state, by):
    """Name matching only: compares a typed name with the file's from/to fields."""
    by = by.strip().lower()
    if by == OWNER:
        return None
    if state == "cancelled":
        return f"only the owner can cancel (--by {OWNER})"
    if state in SENDER_STATES and by != meta.get("from", "").lower():
        return f"'{state}' is set by the sender ({meta.get('from')}) or the owner ({OWNER})"
    if state in RECIPIENT_STATES and by != meta.get("to", "").lower():
        return f"'{state}' is set by the recipient ({meta.get('to')}) or the owner ({OWNER})"
    if state == "blocked" and by not in (meta.get("from", "").lower(), meta.get("to", "").lower()):
        return f"'blocked' is set by the sender, the recipient or the owner ({OWNER})"
    if state == "prepared" and by not in (meta.get("from", "").lower(),):
        return f"'prepared' is set by the sender or the owner ({OWNER})"
    return None


def legal(prev_states, new):
    """prev_states: states logged so far. Returns an error string or None."""
    if new not in STATES:
        return f"unknown state '{new}'"
    cur = prev_states[-1]
    if cur in FINAL:
        return f"handoff is {cur}; open a new one with --reply-to"
    if new == "cancelled":
        return None
    if new == "blocked":
        return None if cur != "blocked" else "already blocked"
    if cur == "blocked":
        before = next((s for s in reversed(prev_states) if s != "blocked"), "prepared")
        return None if new == before else f"blocked from '{before}': it can only return to '{before}' (or be cancelled)"
    return None if new in NEXT.get(cur, set()) else f"can't go {cur} -> {new}"


def parse_text(text, name="handoff"):
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        raise Refused(f"{name}: no front matter")
    meta = {}
    for line in m.group(1).splitlines():
        line = re.sub(r"\s+#.*$", "", line)
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    body = m.group(2)
    log, log_span = [], None
    sec = re.search(r"^## Log[ \t]*\n(.*?)(?=^## |\Z)", body, re.S | re.M)
    if sec:
        start = m.start(2) + sec.start(1)
        log_span = (start, m.start(2) + sec.end(1))
        for line in sec.group(1).splitlines():
            lm = LOG_RE.match(line.strip())
            if lm:
                log.append(dict(at=lm.group(1), state=lm.group(2), actor=lm.group(3).strip(), note=(lm.group(4) or "").strip()))
    sections = re.findall(r"^## (.+?)\s*$", body, re.M)
    return meta, log, sections, log_span


def problems_in(text, stem, outputs_dir):
    """Every rule `check` applies to one file. `set` runs this before writing."""
    out = []
    try:
        meta, log, sections, _ = parse_text(text, stem)
    except Refused as e:
        return [str(e)], None, []
    if not ID_RE.match(stem):
        out.append(f"file name {stem} isn't a handoff id (HO-LANE-YYYYMMDD-XXXX, or @1's HO-YYYYMMDD-NNN)")
    for k in REQUIRED:
        if not meta.get(k):
            out.append(f"missing '{k}'")
    contract = meta.get("contract") or "relay-kit/handoff@1"
    if contract not in READS:
        out.append(f"contract {contract!r}; this script reads {', '.join(sorted(READS))}")
    if meta.get("id") and meta["id"] != stem:
        out.append(f"id {meta['id']} doesn't match file name")
    for s in SECTIONS:
        if s not in sections:
            out.append(f"missing section '## {s}'")
    if sections and sections[-1] != "Log":
        out.append("'## Log' must be the last section")
    if meta.get("status") not in ("prepared", "blocked", "cancelled") and TODO_RE.search(text):
        out.append("still has TODO placeholders after leaving 'prepared'")
    exp = re.search(r"^## Expected output[ \t]*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    for other in re.findall(rf"outputs/({ID_PAT})\.md", exp.group(1) if exp else ""):
        if other != stem:
            out.append(f"warning: Expected output names outputs/{other}.md, but this handoff is {stem} (renumbered?)")
    if not log:
        out.append("empty log")
        return out, meta, log
    if log[0]["state"] != "prepared":
        out.append("log must start with 'prepared'")
    for i in range(1, len(log)):
        if log[i]["at"] < log[i - 1]["at"]:
            out.append(f"log line {i + 1}: time {log[i]['at']} is earlier than the line before ({log[i - 1]['at']})")
    for i in range(1, len(log)):
        err = legal([e["state"] for e in log[:i]], log[i]["state"])
        if err:
            out.append(f"log line {i + 1}: {err}")
    for i, e in enumerate(log, 1):
        if i == 1:
            parsed = parse_actor(e["actor"])
            if contract != "relay-kit/handoff@1":
                if not parsed or parsed[2] or parsed[0] not in (meta.get("from", "").lower(), OWNER):
                    out.append(f"log line 1: drafted by '{e['actor']}', not the sender ({meta.get('from')}) or the owner")
                elif parsed[0] != OWNER and not parsed[1]:
                    out.append(f"log line 1: actor '{e['actor']}' needs a session ('{parsed[0]}#<session id>')")
        else:
            err = actor_error(meta, e["state"], e["actor"], contract, e["note"])
            if err:
                out.append(f"log line {i}: {err}")
        if e["state"] in NOTE_REQUIRED and not e["note"]:
            out.append(f"log line {i}: '{e['state']}' has no note")
    if log[-1]["state"] != meta.get("status"):
        out.append(f"status '{meta.get('status')}' but last log line says '{log[-1]['state']}'")
    if meta.get("status") == "completed" and not (outputs_dir / f"{stem}.md").exists():
        out.append(f"completed but outputs/{stem}.md is missing")
    return out, meta, log


def resolve(d, hid):
    if not ID_RE.match(hid):
        raise Refused(f"'{hid}' isn't a handoff id (HO-LANE-YYYYMMDD-XXXX, or HO-YYYYMMDD-NNN)")
    base = d.resolve()
    p = (base / f"{hid}.md").resolve()
    if p.parent != base:
        raise Refused(f"'{hid}' resolves outside {d}")
    if not p.exists():
        raise Refused(f"no handoff {hid} in {d}")
    return p


class Lock:
    def __init__(self, d):
        self.path = d / LOCK

    def __enter__(self):
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise Refused(f"another update holds {self.path}; retry, or delete it if no update is running")
        os.write(self.fd, f"{os.getpid()} {now()}\n".encode())
        return self

    def __exit__(self, *exc):
        os.close(self.fd)
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


def atomic_write(p, text):
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def ids_in_git(d):
    """Handoff ids that exist in any commit or branch git knows about, after a best-effort
    fetch. Another session may have taken an id in its own clone and pushed it."""
    def git(*args):
        return subprocess.run(["git", "-C", str(d), *args], capture_output=True, text=True, encoding="utf-8", timeout=60)
    try:
        if git("rev-parse", "--git-dir").returncode != 0:
            return set(), "not a git repository: only this folder was checked"
        fetched = git("fetch", "--quiet", "--all").returncode == 0
        names = git("log", "--all", "--name-only", "--format=", "--", ".").stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return set(), "git unavailable: only this folder was checked"
    ids = set(re.findall(rf"({ID_PAT})\.md", names))
    return ids, None if fetched else "fetch failed: checked local history and branches only"


def lane_code(frm):
    """Lane code for ids: .ledger.json's "lanes" map, else the name after ':' in letters and digits."""
    name = frm.split(":", 1)[-1].split("#", 1)[0].lower()
    if name in LANES:
        return LANES[name]
    code = re.sub(r"[^A-Z0-9]", "", name.upper())[:12]
    if not code:
        raise Refused(f"can't make a lane code from --from '{frm}'; pass --lane")
    return code


def cmd_new(a):
    for name in ("from", "to", "title", "source", "last_processed", "reply_to", "touches"):
        single_line(f"--{name.replace('_', '-')}", getattr(a, "frm" if name == "from" else name))
    single_line("--from", a.frm, allow_pipe=False)
    if "#" in a.frm:
        raise Refused("--from names the lane (claude:aec); put the session in --by (claude:aec#01N4)")
    by = a.by or os.environ.get("RELAYKIT_ACTOR") or ""
    single_line("--by", by, allow_pipe=False)
    parsed = parse_actor(by) if by else None
    if not parsed or parsed[2] or (parsed[0] != a.frm.lower() and parsed[0] != OWNER) or (parsed[0] != OWNER and not parsed[1]):
        raise Refused(f"--by (or RELAYKIT_ACTOR) must be the sender with a session, like {a.frm}#<session id>, or the owner")
    if a.reply_to and not ID_RE.match(a.reply_to):
        raise Refused(f"--reply-to '{a.reply_to}' isn't a handoff id")
    lane = (a.lane or lane_code(a.frm)).upper()
    if not re.fullmatch(r"[A-Z0-9]{1,12}", lane):
        raise Refused(f"--lane '{a.lane}' must be 1-12 letters or digits")
    a.dir.mkdir(parents=True, exist_ok=True)
    (a.dir / "outputs").mkdir(exist_ok=True)
    day = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
    text_for = lambda hid: TEMPLATE.format(id=hid, title=a.title, frm=a.frm, to=a.to, now=now(), by=by,
                                           source=a.source or "none", last=a.last_processed or "none",
                                           reply_to=a.reply_to or "", touches=a.touches or "")
    if a.review:
        if not a.to.lower().startswith("review:"):
            raise Refused("--review needs --to review:<who> (review:chatgpt, review:copilot, review:subagent)")
        for opt in ("artifact", "spec"):
            single_line(f"--{opt}", getattr(a, opt))
        base_text = text_for
        def text_for(hid):
            t = base_text(hid)
            for sec, body in REVIEW_SECTIONS.items():
                body = body.format(id=hid, artifact=a.artifact or "<the artifact>", spec=a.spec or "<the spec>")
                if sec == "Task" and not (a.artifact and a.spec):   # a TODO on its own line, so the checks see it
                    body = "TODO: name the artifact (PR, branch or file) and the spec it must meet.\n" + body
                t = re.sub(rf"(^## {re.escape(sec)}[ \t]*\n).*?(?=^## )", lambda m: m.group(1) + body + "\n\n", t, count=1, flags=re.S | re.M)
            return t
    taken, note = ids_in_git(a.dir)
    if note:
        print(f"note: {note}", file=sys.stderr)
    # Tests only: replay a collision by forcing suffixes. Picking an id gains nothing: taken ids
    # are still skipped and an existing file is never replaced.
    forced = [x for x in os.environ.get("RELAYKIT_TEST_SUFFIXES", "").split(",") if x]
    for i in range(50):
        suffix = forced[i] if i < len(forced) else "".join(secrets.choice(B32) for _ in range(4))
        hid = f"HO-{lane}-{day}-{suffix}"
        if hid in taken:   # used in another clone, branch or earlier commit: never reuse
            continue
        try:   # "x": refuses to replace a file that already exists
            with open(a.dir / f"{hid}.md", "x", encoding="utf-8", newline="\n") as f:
                f.write(text_for(hid))
        except FileExistsError:
            continue
        print(hid)
        return
    raise Refused("couldn't find a free id in 50 tries")


def shared_ref(git):
    """The shared copy: origin's default branch (origin/HEAD), else origin/main or origin/master."""
    r = git("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    for ref in ("refs/remotes/origin/main", "refs/remotes/origin/master"):
        if git("rev-parse", "--verify", "--quiet", ref).returncode == 0:
            return ref
    return None


def shared_copy_error(p, fetch):
    """Compare-and-swap against the shared copy (origin's default branch, whatever branch or
    detached HEAD this clone is on): refuse if the shared copy holds a version of this handoff
    that this clone has never had. Returns (error, note)."""
    def git(*args):
        return subprocess.run(["git", "-C", str(p.parent), *args], capture_output=True, text=True, encoding="utf-8", timeout=60)
    try:
        if git("rev-parse", "--git-dir").returncode != 0:
            return None, None
        if git("remote").stdout.strip() == "":
            return None, "no remote: the shared copy wasn't compared"
        fetched = (git("fetch", "--quiet", "origin").returncode == 0) if fetch else False
        ref = shared_ref(git)
        if not ref:
            return None, "no origin default branch found: the shared copy wasn't compared"
        rel = git("rev-parse", "--show-prefix").stdout.strip() + p.name
        theirs = git("rev-parse", f"{ref}:{rel}")
        base = git("merge-base", "HEAD", ref).stdout.strip()
        if theirs.returncode != 0:
            if base and git("rev-parse", f"{base}:{rel}").returncode == 0:   # was shared, now gone
                return (f"{p.name} was removed from the shared copy ({ref.rsplit('/', 1)[-1]}); pull and check "
                        "before writing to it"), None
            return None, None if fetched or not fetch else "fetch failed: compared with the last fetch"
        theirs = theirs.stdout.strip()
        known = set()
        for rev in (base, "HEAD"):
            r = git("rev-parse", f"{rev}:{rel}") if rev else None
            if r is not None and r.returncode == 0:
                known.add(r.stdout.strip())
        known.add(git("hash-object", "--", p.name).stdout.strip())   # identical content, e.g. after a squash merge
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None, "git unavailable: the shared copy wasn't compared"
    note = "--no-fetch: compared with the last fetch" if not fetch else (None if fetched else "fetch failed: compared with the last fetch")
    if theirs not in known:
        return (f"the shared copy of {p.name} ({ref.rsplit('/', 1)[-1]}) has a version this clone never had; "
                "pull, re-read it and retry (someone else may have taken or moved it)"), note
    return None, note


def cmd_set(a):
    a.by = a.by or os.environ.get("RELAYKIT_ACTOR") or ""
    if not a.by:
        raise Refused("--by is required (or set RELAYKIT_ACTOR)")
    single_line("--by", a.by, allow_pipe=False)
    single_line("--for", a.for_)
    if a.for_ and " " in a.for_.strip():
        raise Refused("--for is one name, like codex:aec")
    actor = a.by.strip() + (f" for {a.for_.strip()}" if a.for_ else "")
    single_line("--note", a.note)
    single_line("--last-processed", a.last_processed)
    single_line("--at", a.at)
    p = resolve(a.dir, a.id)
    err, note = shared_copy_error(p, not a.no_fetch)
    if note:
        print(f"note: {note}", file=sys.stderr)
    if err:
        raise Refused(err)
    with Lock(a.dir.resolve()):
        text = p.read_text(encoding="utf-8")
        digest = hashlib.sha256(text.encode()).hexdigest()
        problems, meta, log = problems_in(text, p.stem, a.dir / "outputs")
        problems = [x for x in problems if not x.startswith("warning:")]   # warnings never block
        if problems:
            raise Refused("the file has problems; fix them first (or record 'blocked' by hand):\n  " + "\n  ".join(problems))
        states = [e["state"] for e in log]
        if a.expect and states[-1] != a.expect:
            raise Refused(f"expected '{a.expect}' but it is '{states[-1]}': someone else moved it; re-read it first")
        contract = meta.get("contract") or "relay-kit/handoff@1"
        for err in (legal(states, a.state), actor_error(meta, a.state, actor, contract, a.note)):
            if err:
                raise Refused(err)
        if a.state in NOTE_REQUIRED and not a.note:
            raise Refused(f"'{a.state}' needs --note (how it was sent / why blocked / where the output is)")
        if a.state != "prepared" and TODO_RE.search(text) and a.state not in ("blocked", "cancelled"):
            raise Refused("TODO placeholders are still in the file; fill them before it leaves 'prepared'")
        if a.state == "completed" and not (a.dir / "outputs" / f"{a.id}.md").exists():
            raise Refused(f"completed needs outputs/{a.id}.md to exist")
        when, note = now(), a.note
        if not a.at and log and when < log[-1]["at"]:
            raise Refused(f"the clock says {when}, earlier than the last log line ({log[-1]['at']}); check the times or use --at")
        if a.at:   # recording after the fact: the line carries the event time, the note the recording time
            when = event_time(a.at, log[-1]["at"])
            note = f"{note or ''} (recorded {now()})".strip()
        line = f"- {when} | {a.state} | {actor} |" + (f" {note}" if note else "")
        _, _, _, span = parse_text(text, p.stem)
        log_text = text[span[0]:span[1]].rstrip("\n")
        new_text = text[:span[0]] + (log_text + "\n" if log_text else "") + line + "\n" + text[span[1]:].lstrip("\n")
        new_text = re.sub(r"^status:.*$", f"status: {a.state}", new_text, count=1, flags=re.M)
        if a.last_processed:
            new_text = re.sub(r"^last_processed:.*$", f"last_processed: {a.last_processed}", new_text, count=1, flags=re.M)
        after, _, _ = problems_in(new_text, p.stem, a.dir / "outputs")
        after = [x for x in after if not x.startswith("warning:")]
        if after:
            raise Refused("the update would leave the file invalid:\n  " + "\n  ".join(after))
        if hashlib.sha256(p.read_text(encoding="utf-8").encode()).hexdigest() != digest:
            raise Refused("the file changed while updating; re-read it and retry")
        atomic_write(p, new_text)
    print(f"{a.id}: {states[-1]} -> {a.state}")


def cmd_list(a):
    rows = []
    for p in sorted(a.dir.glob("HO-*.md")):
        try:
            meta, log, _, _ = parse_text(p.read_text(encoding="utf-8"), p.stem)
        except Refused as e:
            rows.append((p.stem, "?", "", "", str(e)))
            continue
        if a.status and meta.get("status") != a.status:
            continue
        last = log[-1] if log else {}
        rows.append((alias(meta.get("id", p.stem)), meta.get("status", "?"), meta.get("from", ""), meta.get("to", ""),
                     f"{meta.get('title', '')}  [{last.get('at', '')} {last.get('actor', '')}]"))
    if not rows:
        print("no handoffs")
    for r in rows:
        print(f"{r[0]}  {r[1]:<12} {r[2]} -> {r[3]}  {r[4]}")


FIXED = ("id", "from", "to", "created")


def history_problems(d, base):
    """Against git revision `base`: no handoff removed, its id/from/to/created unchanged, and its
    earlier log lines still first, in order (protected writes, after a merge as well)."""
    def git(*args):
        return subprocess.run(["git", "-C", str(d), *args], capture_output=True, text=True, encoding="utf-8", timeout=60)
    prefix = git("rev-parse", "--show-prefix").stdout.strip()
    mb = git("merge-base", base, "HEAD")
    if mb.returncode != 0:
        return [f"--base {base}: {mb.stderr.strip() or 'no common history with HEAD'}"]
    base = mb.stdout.strip()   # compare with where this branch left the base, not the base's tip
    listed = git("ls-tree", "-r", "-z", "--name-only", base, "--", ".")   # -z: names with spaces stay whole
    if listed.returncode != 0:
        return [f"--base {base}: {listed.stderr.strip() or 'not a git revision'}"]
    # Handoffs are HO-*.md anywhere under the folder (archived into a subfolder still counts),
    # except in its own outputs/ folder, which holds results under the same names. Every copy is
    # compared: one handoff may live in only one place, so a second copy is itself a problem.
    is_handoff = lambda rel: re.match(rf"^{ID_PAT}\.md$", rel.name) and rel.parts[0] != "outputs"
    here, out = {}, []
    for q in sorted(d.rglob("HO-*.md")):
        if is_handoff(q.relative_to(d)):
            if q.is_symlink():   # a link would let one file stand in for another (or for its output)
                out.append(f"{q.relative_to(d).as_posix()}: is a symlink; a handoff must be a file of its own")
                continue
            here.setdefault(q.name, []).append(q)
    out += [f"{name}: {len(ps)} copies (" + ", ".join(q.relative_to(d).as_posix() for q in ps) + "); a handoff lives in one place"
            for name, ps in sorted(here.items()) if len(ps) > 1]
    for rel in sorted({pathlib.PurePosixPath(x) for x in listed.stdout.split("\0") if x}, key=str):
        if not is_handoff(rel):
            continue
        name = rel.name
        shown = git("show", f"{base}:{prefix}{rel.as_posix()}")
        if shown.returncode != 0:   # fail closed: a base version that can't be read can't vouch for anything
            out.append(f"{rel.as_posix()}: its version at {base[:7]} can't be read ({shown.stderr.strip()})")
            continue
        old_text = shown.stdout
        for p in here.get(name, []) or [None]:
            if p is None:
                out.append(f"{name}: removed since {base[:7]}")
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                out.append(f"{p.relative_to(d).as_posix()}: can't be read ({e.__class__.__name__})")
                continue
            try:
                old_meta, old_log, _, _ = parse_text(old_text, name)
            except Refused:   # unreadable at the base: it may not change at all until it's fixed in a new handoff
                if text != old_text:
                    out.append(f"{p.relative_to(d).as_posix()}: changed since {base[:7]}, where it couldn't be read as a handoff")
                continue
            try:
                meta, log, _, _ = parse_text(text, name)
            except Refused as e:
                out.append(f"{p.relative_to(d).as_posix()}: can't be read any more ({e}), though it could at {base[:7]}")
                continue
            for k in FIXED:
                if old_meta.get(k, "") != meta.get(k, ""):
                    out.append(f"{name}: '{k}' changed since {base[:7]} ({old_meta.get(k)!r} -> {meta.get(k)!r}); open a new handoff instead")
            if log[:len(old_log)] != old_log:
                out.append(f"{name}: earlier log lines changed since {base[:7]}; the log is append-only")
            if old_meta.get("status") not in (None, "prepared"):   # delivered: what was asked and allowed only grows
                for sec in ("Task", "Authorized"):
                    pat = rf"^## {sec}[ \t]*\n(.*?)(?=^## |\Z)"
                    o, n = re.search(pat, old_text, re.S | re.M), re.search(pat, text, re.S | re.M)
                    if o and (not n or not n.group(1).rstrip().startswith(o.group(1).rstrip())):
                        out.append(f"{name}: '## {sec}' was rewritten after delivery; append an update instead")
    return out


def stale_format_warnings(d, files):
    """A handoff made in an older format after the current one was adopted means the session that
    made it runs an old copy of this skill or script, without the newer protections. The
    adoption time comes from .ledger.json: {"contract_since": {"relay-kit/handoff@2": "2026-11-02T00:03Z"}}."""
    try:
        since = json.loads((d / ".ledger.json").read_text(encoding="utf-8")).get("contract_since", {}).get(CONTRACT)
    except (FileNotFoundError, ValueError, AttributeError):
        return []
    if not since:
        return []
    out = []
    since_t = parse_utc(since)
    if since_t is None:
        return [f".ledger.json: warning: contract_since {since!r} isn't an ISO time with a zone; stale copies weren't looked for"]
    newer = re.compile(r"^relay-kit/handoff@(\d+)$")
    for p in files:
        try:
            meta, log, _, _ = parse_text(p.read_text(encoding="utf-8"), p.stem)
        except Refused:
            out.append(f"{p.name}: warning: unreadable; if it's new, the session that made it may have an old copy")
            continue
        contract = meta.get("contract") or "relay-kit/handoff@1"
        m, mine_v = newer.match(contract), int(CONTRACT.rsplit("@", 1)[1])
        if not m or int(m.group(1)) >= mine_v:   # a newer version means *this* copy is the old one
            continue
        created = parse_utc(meta.get("created", ""))
        if created is None:
            out.append(f"{p.name}: warning: 'created' {meta.get('created')!r} isn't a time with a zone; couldn't tell if it's stale")
            continue
        if created >= since_t:
            who = log[0]["actor"] if log else meta.get("from", "?")
            out.append(f"{p.name}: warning: made in {contract} at {meta.get('created')}, after {CONTRACT} was adopted "
                       f"({since}); {who} is using an old copy of the handoff-ledger skill or script")
    return out


def cmd_check(a):
    total, ids = [], {}
    files = sorted(a.dir.glob("HO-*.md"))
    total += stale_format_warnings(a.dir, files)
    if a.base:
        total += history_problems(a.dir, a.base)
    for p in files:
        problems, meta, _ = problems_in(p.read_text(encoding="utf-8"), p.stem, a.dir / "outputs")
        if meta and meta.get("id") in ids:
            problems.append(f"duplicate id with {ids[meta['id']]}")
        if meta:
            ids[meta.get("id")] = p.name
        total += [f"{p.name}: {x}" for x in problems]
    if (a.dir / LOCK).exists():
        total.append(f"{LOCK} exists: an update is running or was interrupted")
    warnings = [x for x in total if ": warning: " in x]
    total = [x for x in total if ": warning: " not in x]
    for w in warnings:
        print("WARNING", w.replace(": warning: ", ": ", 1))
    for t in total:
        print("PROBLEM", t)
    print(f"{len(files)} handoff(s), {len(total)} problem(s)" + (f", {len(warnings)} warning(s)" if warnings else ""))
    sys.exit(1 if total else 0)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", type=pathlib.Path, default=None,
                    help="the handoffs folder (default: $RELAYKIT_RECORDS/handoffs, else ./handoffs, else the records clone found nearby)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new")
    n.add_argument("--from", dest="frm", required=True)
    n.add_argument("--to", required=True)
    n.add_argument("--title", required=True)
    n.add_argument("--source")
    n.add_argument("--last-processed")
    n.add_argument("--reply-to")
    n.add_argument("--by", help="the sender with a session (claude:aec#01N4); default $RELAYKIT_ACTOR")
    n.add_argument("--lane", help="lane code in the id (default: .ledger.json 'lanes', else from --from)")
    n.add_argument("--review", action="store_true", help="an adversarial review handoff (--to review:<who>); fills Task, Context, Expected output and Limits")
    n.add_argument("--artifact", help="with --review: what to review (a PR, branch or file)")
    n.add_argument("--spec", help="with --review: what it must meet")
    n.add_argument("--touches", help="optional: comma-separated paths, globs or thread URLs this work will change; status.py --touching warns others")
    s = sub.add_parser("set")
    s.add_argument("id")
    s.add_argument("state", choices=STATES)
    s.add_argument("--by", help="who is writing, with a session (claude:aec#01N4); default $RELAYKIT_ACTOR")
    s.add_argument("--for", dest="for_", help="record this step for another party (e.g. a chat-only recipient); the note says where it came from")
    s.add_argument("--expect", choices=STATES, help="refuse unless the handoff is in this state now (compare-and-swap)")
    s.add_argument("--no-fetch", action="store_true", help="compare with the shared copy as last fetched")
    s.add_argument("--note")
    s.add_argument("--last-processed")
    s.add_argument("--at", help="when the event happened, if recording it later (ISO, e.g. 2026-10-28T17:51Z)")
    l = sub.add_parser("list")
    l.add_argument("--status", choices=STATES)
    c = sub.add_parser("check")
    c.add_argument("--base", help="git revision: also check no handoff was removed or had its header or earlier log lines changed")
    a = ap.parse_args()
    global OWNER, LANES
    try:
        a.dir, where = find_dir(a.dir)
        OWNER, LANES, note = load_config(a.dir)
        for n_ in (where, note):
            if n_:
                print(f"note: {n_}", file=sys.stderr)
        {"new": cmd_new, "set": cmd_set, "list": cmd_list, "check": cmd_check}[a.cmd](a)
    except Refused as e:
        sys.exit(f"refused: {e}")


if __name__ == "__main__":
    main()
