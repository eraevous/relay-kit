#!/usr/bin/env python3
"""Check an AEC-style decision ledger and challenge ledger (JSON lines). Standard library only.

    python3 tools/check_ledgers.py --dir path/to/governance [--git]

decisions.jsonl, one object per line:
  id        DEC-NNN, numbered in order
  date      YYYY-MM-DD
  status    proposed | adopted | rejected | superseded
  decision  what was decided, in one or two sentences
  scope     what it applies to
  decider   who decided ("owner" or a name); required unless proposed
  basis     the owner's words or the option they chose, quoted; required unless proposed
  locator   where those words are (thread, date, time)
  supersedes optional: an earlier DEC id. Takes effect only from a settled line (relay-kit/lifecycle@1,
            docs/lifecycle.md): adopted closes any earlier decision; rejected and superseded settle a
            proposal only and can't retire an adopted decision; a proposed line is a proposed
            replacement. Self, forward and unknown references are problems.
  revokes   optional: earlier DEC id(s) an adopted line ends (from its effective_from, else its date)
  amends    optional: an earlier DEC id, or {"id": "DEC-N", "clauses": ["2"]}: a partial amendment
            (the target stays in force). The cockpit's Amend uses it on a line that supersedes a
            proposal with the owner's edited text.
  effective_from / effective_until  optional: YYYY-MM-DD or an ISO time in UTC
  evidence  optional
  ask       optional, on a proposal: what it asks of the owner, in the proposer's words (checked
            when it's an object; question, at least one option and the default are required):
            {"question": "...?", "options": [{"label": "Accept", "effect": "what happens"}, ...],
             "default_if_unanswered": "what happens if nobody answers"}

challenges.jsonl, one object per line:
  id        CH-NNN, numbered in order
  date      YYYY-MM-DD
  status    open | resolved | accepted-risk
  concern   the objection
  raised_by who raised it
  resolution required unless open
  resolves  optional: an earlier CH id this line closes (the earlier line stays as written); only a
            resolved or accepted-risk line closes anything
  ask       optional, as on decisions
  kind      optional: "question" for a question asked on the record (the cockpit's Clarify); it then
            needs about (the DEC, CH or HO id it asks about) and addressed_to (who it's for). Other
            values aren't checked.
            Whoever answers appends a line that resolves it, with the answer as the resolution.

Reading check (opt-in per ledger): if the ledger folder has policy.json with
{"reading_required_from": "DEC-NNN"}, every decision from that id on that isn't just proposed needs
  reading   {"said": the owner's words, quoted; "read_as": the recorder's one-line reading of them;
             "confirmed": "explicit" when the words were themselves an instruction or a chosen
             option, else {"words": the owner's yes, quoted, "locator": where he said it}}
so a decision built on a question, a musing or a half-sentence can't be recorded as adopted until the
owner has confirmed the reading (Zach, 2026-11-12).

No field of any line may still hold a "<<REQUIRED" placeholder: the cockpit composes lines with
them where only the owner can supply the words (decider, basis, locator, resolution).

With --git, also checks that both files only grew since the last commit, or since
--base REF (append-only):
earlier lines may not be edited or removed. A status change is a new line with the same
subject, not an edit. It also checks that the ledgers aren't set to git's `merge=union`
(which would join two branches' lines silently, duplicate ids included), and that every
line added since --base has `recorded_by`: the owner's name, or an agent with its session
(`"claude:aec#01N4"`: a lane is not an actor)."""
import argparse, importlib.util, json, pathlib, re, subprocess, sys


def _lifecycle():
    spec = importlib.util.spec_from_file_location("relaykit_lifecycle", pathlib.Path(__file__).resolve().parent / "lifecycle.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


LC = _lifecycle()

DEC_STATUS = {"proposed", "adopted", "rejected", "superseded"}
CH_STATUS = {"open", "resolved", "accepted-risk"}
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def load(path):
    rows, problems = [], []
    if not path.exists():
        return rows, problems
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError as e:
            problems.append(f"{path.name}:{n}: not JSON ({e})")
            continue
        if not isinstance(obj, dict):
            problems.append(f"{path.name}:{n}: not an object")
            continue
        rows.append((n, obj))
    return rows, problems


def check_ids(name, rows, prefix):
    out = []
    for i, (n, r) in enumerate(rows, 1):
        want = f"{prefix}-{i:03d}"
        if r.get("id") != want:
            out.append(f"{name}:{n}: id {r.get('id')!r}, expected {want} (ids are numbered in order)")
        if not DATE.match(str(r.get("date", ""))):
            out.append(f"{name}:{n}: date must be YYYY-MM-DD")
    return out


PLACEHOLDER = "<<REQUIRED"   # the cockpit's composed lines carry these until the owner fills them in


def check_placeholders(name, rows):
    """A line still holding a <<REQUIRED: ...>> placeholder was pasted without the owner's own words."""
    out = []

    def walk(v):
        if isinstance(v, str):
            return PLACEHOLDER in v
        if isinstance(v, dict):
            return any(walk(x) for x in v.values())
        if isinstance(v, list):
            return any(walk(x) for x in v)
        return False
    for n, r in rows:
        bad = sorted(k for k, v in r.items() if walk(v))
        if bad:
            out.append(f"{name}:{n}: {', '.join(bad)} still hold a {PLACEHOLDER}...>> placeholder; replace it with your own words")
    return out


ASK_KEYS = {"question", "options", "default_if_unanswered"}
REF_RE = re.compile(r"^(?:DEC-\d{3,}|CH-\d{3,}|HO-[A-Z0-9]{1,12}-\d{8}-[0-9A-HJKMNP-TV-Z]{4}|HO-\d{8}-\d{3})$")


def check_ask(where, a):
    """The optional `ask` object: what a proposal asks of the owner, in the proposer's words.
    Checked only in its object form; another value in an 'ask' field (allowed before) is left alone,
    and the cockpit ignores it."""
    if not isinstance(a, dict):
        return []
    out = [f"{where}: 'ask' has unknown key {k!r}" for k in sorted(set(a) - ASK_KEYS)]
    q = a.get("question")
    if not isinstance(q, str) or not q.strip().endswith("?") or len(q.strip()) < 3:
        out.append(f"{where}: ask.question must be one sentence ending in '?'")
    opts = a.get("options")
    if not isinstance(opts, list) or not opts or not all(isinstance(o, dict) and isinstance(o.get("label"), str) and o["label"].strip()
                                             and isinstance(o.get("effect"), str) and o["effect"].strip() for o in opts):
        out.append(f"{where}: ask.options must be a non-empty list of {{label, effect}}, both non-empty text")
    d = a.get("default_if_unanswered")
    if not (isinstance(d, str) and d.strip()):
        out.append(f"{where}: ask.default_if_unanswered must say what happens if nobody answers")
    return out


def check_decisions(rows):
    out = check_ids("decisions.jsonl", rows, "DEC") + check_placeholders("decisions.jsonl", rows)
    ids = {r.get("id") for _, r in rows}
    for n, r in rows:
        where = f"decisions.jsonl:{n}"
        if r.get("status") not in DEC_STATUS:
            out.append(f"{where}: status must be one of {sorted(DEC_STATUS)}")
        for f in ("decision", "scope"):
            if not str(r.get(f, "")).strip():
                out.append(f"{where}: '{f}' is required")
        if r.get("status") != "proposed":
            for f in ("decider", "basis", "locator"):
                if not str(r.get(f, "")).strip():
                    out.append(f"{where}: '{f}' is required once a decision isn't just proposed")
        sup = r.get("supersedes")
        if isinstance(sup, str) and sup and sup not in ids:
            out.append(f"{where}: supersedes unknown {sup}")
        out += check_ask(where, r.get("ask"))
    return out


def _num(dec_id):
    m = re.fullmatch(r"DEC-(\d+)", str(dec_id or ""))
    return int(m.group(1)) if m else None


def check_readings(rows, policy):
    """Opt-in: decisions from policy['reading_required_from'] on carry a confirmed reading."""
    start = _num((policy or {}).get("reading_required_from"))
    if start is None:
        return []
    out = []
    for n, r in rows:
        num = _num(r.get("id"))
        if num is None or num < start or r.get("status") == "proposed":
            continue
        where = f"decisions.jsonl:{n} ({r.get('id')})"
        rd = r.get("reading")
        if not isinstance(rd, dict):
            out.append(f"{where}: 'reading' is required (said, read_as, confirmed): see the reading check")
            continue
        for f in ("said", "read_as"):
            if not isinstance(rd.get(f), str) or not rd[f].strip():
                out.append(f"{where}: reading.{f} is required")
        c = rd.get("confirmed")
        if c == "explicit":
            continue
        if not (isinstance(c, dict) and all(isinstance(c.get(k), str) and c[k].strip() for k in ("words", "locator"))):
            out.append(f"{where}: reading.confirmed must be \"explicit\" or the owner's yes "
                       "({\"words\": ..., \"locator\": ...}); an unconfirmed reading stays proposed")
    return out


def check_challenges(rows):
    out = check_ids("challenges.jsonl", rows, "CH") + check_placeholders("challenges.jsonl", rows)
    seen = set()
    for n, r in rows:
        res = r.get("resolves")
        if isinstance(res, str) and res and res not in seen:
            out.append(f"challenges.jsonl:{n}: resolves {res!r}, which isn't an earlier challenge")
        seen.add(r.get("id"))
        where = f"challenges.jsonl:{n}"
        out += check_ask(where, r.get("ask"))
        if r.get("kind") == "question":   # other kinds were allowed before and still are
            if not isinstance(r.get("about"), str) or not REF_RE.match(r["about"]):
                out.append(f"{where}: a question needs 'about': the DEC, CH or HO id it asks about")
            if not isinstance(r.get("addressed_to"), str) or not r["addressed_to"].strip():
                out.append(f"{where}: a question needs 'addressed_to': who it's addressed to")
        if r.get("status") not in CH_STATUS:
            out.append(f"{where}: status must be one of {sorted(CH_STATUS)}")
        for f in ("concern", "raised_by"):
            if not str(r.get(f, "")).strip():
                out.append(f"{where}: '{f}' is required")
        if r.get("status") != "open" and not str(r.get("resolution", "")).strip():
            out.append(f"{where}: 'resolution' is required unless the challenge is open")
    return out


def check_transitions(dec_rows, ch_rows, new=None):
    """relay-kit/lifecycle@1: transitions that would take no effect (self, forward, a proposal or an open
    challenge closing something, a rejected/superseded line retiring an adopted decision, a non-text
    target, a malformed revokes/amends/date). Unknown decision targets and bad challenge targets are
    already reported by the checks above. With `new` (ids added since --base), problems on older lines
    are returned separately as warnings: an append-only ledger can't take them back, and they mustn't
    block every later append."""
    res = LC.resolve([r for _, r in dec_rows], [r for _, r in ch_rows])
    line = {}
    for n, r in dec_rows:
        line.setdefault(("decisions.jsonl", r.get("id")), n)
    for n, r in ch_rows:
        line.setdefault(("challenges.jsonl", r.get("id")), n)
    out, warn = [], []
    for p in res["problems"]:
        name = "challenges.jsonl" if str(p["id"]).startswith("CH-") else "decisions.jsonl"
        if p["rule"] == "unknown" and p["field"] == "supersedes":
            continue
        if name == "challenges.jsonl" and p["rule"] in ("unknown", "forward", "self"):
            continue
        msg = f"{name}:{line.get((name, p['id']), '?')}: {p['message']}"
        (warn if new is not None and p["id"] not in new else out).append(msg)
    return out, warn


def new_ids(path, base):
    """Ids of the lines added since base (None when base can't be read: then every line counts as new)."""
    if not path.exists():
        return set()
    try:
        rel = subprocess.run(["git", "-C", str(path.parent), "ls-files", "--full-name", path.name],
                             capture_output=True, text=True, encoding="utf-8", check=True).stdout.strip()
        old = subprocess.run(["git", "-C", str(path.parent), "show", f"{base}:{rel}"],
                             capture_output=True, text=True, encoding="utf-8", check=True).stdout if rel else ""
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    out = set()
    for ln in path.read_text(encoding="utf-8").splitlines()[len(old.splitlines()):]:
        try:
            out.add(json.loads(ln).get("id"))
        except (ValueError, AttributeError):
            continue
    return out


def check_merge_attr(path):
    p = subprocess.run(["git", "-C", str(path.parent), "check-attr", "merge", "--", path.name],
                       capture_output=True, text=True, encoding="utf-8")
    if p.returncode == 0 and p.stdout.strip().endswith(": union"):
        return [f"{path.name}: .gitattributes sets merge=union; concurrent appends must conflict, not be joined"]
    return []


def check_new_recorders(path, base):
    """Lines added since base: an agent recorder ('x:y') must carry a session ('x:y#id')."""
    try:
        rel = subprocess.run(["git", "-C", str(path.parent), "ls-files", "--full-name", path.name],
                             capture_output=True, text=True, encoding="utf-8", check=True).stdout.strip()
        old = subprocess.run(["git", "-C", str(path.parent), "show", f"{base}:{rel}"],
                             capture_output=True, text=True, encoding="utf-8", check=True).stdout if rel else ""
    except (subprocess.CalledProcessError, FileNotFoundError):
        old = ""
    out = []
    lines = path.read_text(encoding="utf-8").splitlines()
    for n, line in enumerate(lines[len(old.splitlines()):], len(old.splitlines()) + 1):
        try:
            who = str(json.loads(line).get("recorded_by", "")).strip()
        except (ValueError, AttributeError):
            continue
        first = who.split()[0] if who else ""
        if not who:
            out.append(f"{path.name}:{n}: new lines need 'recorded_by' (the owner's name, or lane#session)")
        elif ":" in first and not re.fullmatch(r"[\w.-]+:[\w.-]+#[\w-]+", first):
            out.append(f"{path.name}:{n}: recorded_by {who!r} names a lane, not a session (e.g. {first.split('#')[0]}#<session>)")
    return out


def check_append_only(path, base):
    try:
        rel = subprocess.run(["git", "-C", str(path.parent), "ls-files", "--full-name", path.name],
                             capture_output=True, text=True, encoding="utf-8", check=True).stdout.strip()
        if not rel:
            return []   # not committed yet
        old = subprocess.run(["git", "-C", str(path.parent), "show", f"{base}:{rel}"],
                             capture_output=True, text=True, encoding="utf-8", check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    new = path.read_text(encoding="utf-8")
    if not new.startswith(old.rstrip("\n")):
        return [f"{path.name}: earlier lines changed since {base}; ledgers are append-only"]
    return []


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", type=pathlib.Path, default=pathlib.Path("governance"))
    ap.add_argument("--git", action="store_true", help="also check append-only against --base")
    ap.add_argument("--base", default="HEAD", help="git revision to compare with (default HEAD)")
    a = ap.parse_args()
    problems = []
    dec_rows, p = load(a.dir / "decisions.jsonl"); problems += p
    ch_rows, p = load(a.dir / "challenges.jsonl"); problems += p
    problems += check_decisions(dec_rows) + check_challenges(ch_rows)
    policy_path = a.dir / "policy.json"
    if policy_path.exists():
        try:
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
        except ValueError:
            policy, problems = {}, problems + ["policy.json is not valid JSON"]
        problems += check_readings(dec_rows, policy if isinstance(policy, dict) else {})
    new = None
    if a.git:
        nd, nc = new_ids(a.dir / "decisions.jsonl", a.base), new_ids(a.dir / "challenges.jsonl", a.base)
        new = None if nd is None or nc is None else nd | nc
    tp, warnings = check_transitions(dec_rows, ch_rows, new)
    problems += tp
    if a.git:
        for f in ("decisions.jsonl", "challenges.jsonl"):
            if (a.dir / f).exists():
                problems += check_append_only(a.dir / f, a.base)
                problems += check_merge_attr(a.dir / f) + check_new_recorders(a.dir / f, a.base)
    for x in warnings:
        print("WARNING", x, "(an earlier line: it takes no effect; append a correcting line)")
    for x in problems:
        print("PROBLEM", x)
    print(f"{len(dec_rows)} decision(s), {len(ch_rows)} challenge(s), {len(problems)} problem(s)")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
