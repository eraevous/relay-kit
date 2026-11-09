#!/usr/bin/env python3
"""Append one line to the decision or challenge ledger, safely. Standard library only.

    python3 tools/ledger_append.py --dir governance decision '{"status": "adopted", "decision": "...", ...}'
    python3 tools/ledger_append.py --dir governance challenge '{"status": "open", "concern": "...", ...}'

Decision and challenge ids are one shared count (DEC-NNN, CH-NNN; DEC-018), so two sessions
that each take "the next number" from their own copy collide (DEC-035, 2026-11-02). This
command takes the id instead of you:
  1. fetches, and takes the next id after the highest one in *either* this copy or the shared
     copy (origin's default branch);
  2. fills `id`, `date` (today, UTC) and `recorded_by` ($RELAYKIT_ACTOR) when they're missing;
  3. appends the line, then runs check_ledgers.py on the result (with --git);
  4. on any problem, takes the line back out and exits non-zero. Its exit code is the check's:
     don't pipe it into `tail`, which hides a failure.
With --expect ID=STATE[@HASH] (repeatable; the cockpit, relay-kit/cockpit@6, adds it to every line it composes), it first
checks that the item is still in the state the page showed (relay-kit/lifecycle@1: a decision `open`,
`settled` or its applicability; a challenge `open` or `closed`) and that its line hasn't changed
(the first 12 hex digits of its sha256). If not, it refuses and lists the lines since that mention
the item, so you see what changed before deciding again.
It prints the new id. If the shared copy can't be read (no remote, fetch failed), it refuses
rather than guess; --offline overrides that when you know you're the only writer. A branch
whose ledger is behind the shared one is refused too: an id must be new on the shared
branch, so merge it first. Local appends take a lock file, so two processes in one copy
can't overwrite each other, and the rollback only removes this command's own line.
What it can't close: another session taking the same id between this append and your
push. Push (or open the pull request) right away; the records check on main catches what
slips through.
"""
import argparse, datetime as dt, hashlib, importlib.util, json, os, pathlib, re, subprocess, sys

HERE = pathlib.Path(__file__).resolve().parent
KINDS = {"decision": ("decisions.jsonl", "DEC"), "challenge": ("challenges.jsonl", "CH")}


def handoff_module():
    """The handoff-ledger skill's script, which defines how the shared copy is found."""
    import importlib.util
    skills = HERE.parent / "skills" if (HERE.parent / "skills" / "handoff-ledger").is_dir() else HERE / "skills"
    path = skills / "handoff-ledger" / "scripts" / "handoff.py"   # a checkout, or the installed package
    if not path.exists():
        sys.exit(f"refused: {path} is missing; ledger_append.py runs from the kit, next to skills/")
    spec = importlib.util.spec_from_file_location("handoff", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def lifecycle():
    spec = importlib.util.spec_from_file_location("relaykit_lifecycle", HERE / "lifecycle.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


EXPECT = re.compile(r"^((?:DEC|CH)-\d{3,})=([a-z][a-z ,()-]*?)(?:@([0-9a-f]{6,64}))?$")


def check_expect(d, expects):
    """None if every expectation holds, else the refusal text with what changed."""
    rows = {}
    for name in ("decisions.jsonl", "challenges.jsonl"):
        p = d / name
        rows[name] = []
        if p.exists():
            for ln in p.read_bytes().split(b"\n"):    # raw bytes, as the readers hash them
                try:
                    obj = json.loads(ln) if ln.strip() else None
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    rows[name].append((ln, obj))
    lc = lifecycle()
    decs = [o for _, o in rows["decisions.jsonl"]]
    res = lc.resolve(decs, [o for _, o in rows["challenges.jsonl"]], lc.ledger_as_of(decs))
    out = []
    for e in expects:
        m = EXPECT.match(e)
        if not m:
            return f"refused: --expect {e!r} isn't ID=STATE or ID=STATE@HASH"
        rid, want, h = m.groups()
        want = "standing" if want == lc.STANDING else want
        name = "decisions.jsonl" if rid.startswith("DEC-") else "challenges.jsonl"
        line = next((ln for ln, o in rows[name] if o.get("id") == rid), None)
        r = (res["decisions"] if name == "decisions.jsonl" else res["challenges"]).get(rid)
        if line is None or r is None:
            out.append(f"{rid} isn't in {name} any more")
            continue
        now = r["applicability"] if name == "decisions.jsonl" else ("open" if r["open"] else "closed")
        if now == "adopted, no structured supersession found":
            now = "standing"
        changed = []
        if want != now:
            changed.append(f"{rid} is now {now}, not {want}")
        if h and not lc.line_hash(line).startswith(h):
            changed.append(f"{rid}'s own line has changed since the page was built")
        if changed:
            pat = re.compile(rf"\b{re.escape(rid)}\b".encode())
            later = [f"  {o.get('id')} ({o.get('status')}, {o.get('date')})"
                     for nm in ("decisions.jsonl", "challenges.jsonl") for ln, o in rows[nm]
                     if o.get("id") != rid and pat.search(ln)]
            out.append("; ".join(changed) + (":\n" + "\n".join(later) if later else ""))
    if out:
        return ("refused: the records changed since the page you are applying from was built (lines that mention it are listed):\n" + "\n".join(out)
                + "\nNothing was appended. Rebuild the page, or look at those lines, and decide again.")
    return None


def git(d, *args):
    return subprocess.run(["git", "-C", str(d), *args], capture_output=True, text=True, encoding="utf-8", timeout=60)


def max_id(text, prefix):
    """Highest top-level id in a JSON-lines text (parsed, so spacing and nested ids don't matter)."""
    nums = []
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        m = re.fullmatch(rf"{prefix}-(\d+)", str(obj.get("id", ""))) if isinstance(obj, dict) else None
        if m:
            nums.append(int(m.group(1)))
    return max(nums, default=0)


def shared_text(path):
    """The ledger as the shared copy has it, after a fetch. (text, note). Works when the ledger's
    folder doesn't exist here yet: git runs from the nearest folder that does."""
    d = path.parent
    while not d.exists() and d != d.parent:
        d = d.parent
    if git(d, "rev-parse", "--git-dir").returncode != 0:
        return None, "not a git repository: the shared copy can't be read"
    if git(d, "remote").stdout.strip() == "":
        return None, "no remote: the shared copy can't be read"
    if git(d, "fetch", "--quiet", "origin").returncode != 0:
        return None, "fetch failed: the shared copy can't be read"
    note = None
    ref = handoff_module().shared_ref(lambda *args: git(d, *args))   # the same rule handoff.py uses
    if not ref:
        return None, "no shared branch found on origin"
    top = pathlib.Path(git(d, "rev-parse", "--show-toplevel").stdout.strip())
    rel = path.resolve().relative_to(top.resolve()).as_posix()   # resolve() works for paths not created yet
    if git(d, "cat-file", "-e", f"{ref}^{{commit}}").returncode != 0:
        return None, f"{ref} can't be read (a dangling origin/HEAD?): the shared copy can't be read"
    listed = git(d, "ls-tree", "--full-tree", "--name-only", ref, "--", rel)
    if listed.returncode != 0:
        return None, f"{ref} can't be listed: the shared copy can't be read"
    if not listed.stdout.strip():
        return "", note   # not on the shared branch yet: genuinely empty
    shown = git(d, "show", f"{ref}:{rel}")
    if shown.returncode != 0:
        return None, f"{ref}:{rel} can't be read: the shared copy can't be read"
    return shown.stdout, note


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", type=pathlib.Path, default=None,
                    help="the governance folder (default: ./governance, else next to the handoffs folder handoff.py finds)")
    ap.add_argument("kind", choices=sorted(KINDS))
    ap.add_argument("json", help="the line's fields as a JSON object; id, date and recorded_by are filled if missing")
    ap.add_argument("--offline", action="store_true", help="allow appending without reading the shared copy")
    ap.add_argument("--expect", action="append", default=[], metavar="ID=STATE[@HASH]",
                    help="refuse unless the item is still in this state (and its line unchanged); the cockpit adds it")
    a = ap.parse_args()
    if a.dir is None:
        # ./governance as before; else next to the handoffs folder handoff.py finds. Either way the
        # owner check handoff.py makes runs when there's a handoffs folder beside it, and the
        # governance folder must not lead out of the records.
        m = handoff_module()
        try:
            if pathlib.Path("governance").is_dir():
                d, where = pathlib.Path("handoffs"), None
                a.dir = m.records_folder(d, "governance")
            else:
                d, where = m.find_dir(None)
                a.dir = m.records_folder(d, "governance")
                if not a.dir.is_dir():
                    sys.exit(f"refused: {a.dir} doesn't exist; these records have no governance folder (pass --dir to create one)")
            note = m.load_config(d)[2] if d.is_dir() else None
        except m.Refused as e:
            sys.exit(f"refused: {e}")
        for n in (where, note):
            if n:
                print(f"note: {n}", file=sys.stderr)
    name, prefix = KINDS[a.kind]
    path = a.dir / name
    try:
        row = json.loads(a.json)
    except ValueError as e:
        sys.exit(f"refused: not JSON ({e})")
    if not isinstance(row, dict):
        sys.exit("refused: give one JSON object")
    if "id" in row:
        sys.exit("refused: leave 'id' out; this command assigns it")
    theirs, note = shared_text(path)
    if theirs is None:
        if not a.offline:
            sys.exit(f"refused: {note}; an id taken without it may collide (use --offline if you're the only writer)")
        print(f"note: {note}; --offline", file=sys.stderr)
        theirs = ""
    lock = a.dir / ".ledger_append.lock"
    a.dir.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        sys.exit(f"refused: another append holds {lock}; retry, or delete it if none is running")
    try:
        sys.exit(append(a, path, prefix, row, theirs))
    finally:
        os.close(fd)
        try:
            os.unlink(lock)
        except FileNotFoundError:
            pass


def append(a, path, prefix, row, theirs):
    existed = path.exists()
    mine = path.read_text(encoding="utf-8") if existed else ""
    lines_mine = [l for l in mine.splitlines() if l.strip()]
    lines_theirs = [l for l in theirs.splitlines() if l.strip()]
    if lines_mine[:len(lines_theirs)] != lines_theirs:
        if lines_theirs[:len(lines_mine)] == lines_mine:
            return (f"refused: the shared copy has {len(lines_theirs) - len(lines_mine)} line(s) this copy doesn't "
                    f"(up to {prefix}-{max_id(theirs, prefix):03d}); merge or pull the shared branch first, so the new id is new there too")
        return ("refused: this copy and the shared copy have different lines under the same ids "
                "(someone appended the same id elsewhere); reconcile them before appending")
    if a.expect:
        why = check_expect(a.dir, a.expect)
        if why:
            return why
    new_id = f"{prefix}-{max(max_id(mine, prefix), max_id(theirs, prefix)) + 1:03d}"
    row = {"id": new_id, **row}
    row.setdefault("date", dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"))
    actor = os.environ.get("RELAYKIT_ACTOR", "").strip()
    if "recorded_by" not in row:
        if not actor:
            return "refused: set RELAYKIT_ACTOR (lane#session, or the owner's name) or give recorded_by"
        row["recorded_by"] = actor
    # The recorder rule is checked here too, so it holds even where the ledger check can't compare
    # with a previous commit (a repository with no commits yet).
    who = str(row.get("recorded_by", "")).strip()
    first = who.split()[0] if who else ""
    if not who or (":" in first and not re.fullmatch(r"[\w.-]+:[\w.-]+#[\w-]+", first)):
        return f"refused: recorded_by {who!r} must be the owner's name or lane#session"
    line = ("" if not mine or mine.endswith("\n") else "\n") + json.dumps(row, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:   # append, never rewrite: nothing else is touched
        f.write(line)
    base = "HEAD" if git(a.dir, "rev-parse", "--verify", "--quiet", "HEAD").returncode == 0 else None
    cmd = [sys.executable, "-I", str(HERE / "check_ledgers.py"), "--dir", str(a.dir)] + (["--git", "--base", base] if base else [])
    chk = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if chk.returncode != 0:
        now_text = path.read_text(encoding="utf-8")
        if now_text == mine + line:   # only ours was added: take exactly that back out
            if existed:
                tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
                tmp.write_text(mine, encoding="utf-8")
                os.replace(tmp, path)   # atomic: readers see the old file or the new one, never half
            else:
                path.unlink()
            sys.stderr.write(chk.stdout + chk.stderr)
            return f"refused: the ledger check failed with {new_id} added; nothing was appended"
        sys.stderr.write(chk.stdout + chk.stderr)
        return f"FAILED: the ledger check failed with {new_id} added, and the file changed meanwhile; {new_id} was left in place: fix it by hand"
    print(new_id)
    return 0


if __name__ == "__main__":
    main()
