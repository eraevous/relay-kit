#!/usr/bin/env python3
"""Cockpit: one static, self-contained page over a records store: relay-kit/cockpit@6.

    python3 tools/cockpit.py --root RECORDS --out DIR [--lane L] [--content full|metadata]
                             [--weekly-tokens N] [--drift-days N] [--coordinator LANE]
                             [--theme FILE] [--landing SURFACE] [--allow-public-dest]

Writes DIR/index.html (inline CSS and JS, no external resources) and DIR/cockpit.json (the data
the page was built from). Four surfaces:
  Choices    what waits on the owner: proposed decisions, handoffs that need the owner, open
             challenges. Accept / Reject / Defer compose a paste-ready line; nothing is written.
  Topology   information topology (lanes, actors, threads, captures, handoff flows) and
             authority topology (owner -> coordinator -> lanes, plus delegations read from
             in-force decisions, marked inferred), each drawn in SVG.
  Budget     governance/budget.jsonl (if present) against a weekly allowance.
  Run/Drift  handoffs by status, PR links from threads.md, recent decisions, open challenges,
             drift flags.
  Pull requests  governance/pr-state.jsonl (written by relay-kit prs): each PR's What / Why / How /
             Authorized in its author's words, who wrote it, reviews by family with their bodies
             verbatim (@5), CI.
  Status     status/<date>-<edition>.md (relay-kit/status@1): the newest coordinator-authored
             narrative edition rendered verbatim from a markdown subset; older editions indexed (@5).
  Choices (@5) carry decision-support notes from governance/decision-support.jsonl
             (relay-kit/decision-support@1), one block per author, authored, never generated.

It reads with the export module's readers (tools/export.py), changes nothing under --root, and
refuses a destination inside the records or inside a public (or unconfirmed) git repository,
using the export's guard. Output is deterministic: no clock is read. The page shows the records'
git revision, its commit time and the last fetch (fixed for a given clone), apart from the newest
record's timestamp; the browser counts their age. Spec: docs/cockpit.md. Standard library only."""
import argparse, datetime as dt, html, importlib.util, json, os, pathlib, re, shlex, sys, tempfile

SCHEMA = "relay-kit/cockpit@6"
OUT_FILES = {"index.html", "cockpit.json"}
BUDGET_FILE = "governance/budget.jsonl"
SETTINGS_FILE = "governance/cockpit.json"
BUDGET_FIELDS = {"ts": "ISO time (UTC) the line covers up to", "actor": "assistant:lane#session or the owner",
                 "window": "weekly | session", "tokens_in": "integer >= 0", "tokens_out": "integer >= 0",
                 "agents": "integer >= 0: agents running in that window", "note": "free text",
                 "method": "measured | proxy | estimate (relay-kit/budget@2; older lines: unlabelled)",
                 "id / correction_of / includes_children / interval_start / interval_end / scope / source / tokens_detail":
                     "relay-kit/budget@2: see docs/budget.md"}
POLICY_KEYS = ["default_effort", "spawn_route", "weekly_tokens_soft", "max_delegations_per_question",
               "chatgpt_standing_thread_cap", "claude_build_context_cap", "codex_repo_context_cap",
               "human_interruptions_per_day"]
STATE_ORDER = ["prepared", "sent", "acknowledged", "in_progress", "blocked", "completed", "cancelled"]
DELEGATION = re.compile(r"delegat|authori[sz]", re.I)
PR_LINK = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
LIMITS = [
    "Nothing on this page writes anywhere. Accept / Reject / Defer only compose text for the owner to paste "
    "(DEC-019: authority stays with the human paste).",
    "The page fetches nothing. Pull requests are as fresh as the last relay-kit prs run that wrote "
    "governance/pr-state.jsonl; their sections and reviews are the authors' own words, not a summary. A review's "
    "'Findings' line is shown only when its body states those counts.",
    "Status is the coordinator's narrative, rendered as written (a markdown subset; anything else is shown as text). "
    "Decision-support notes are authored by the people named on them; the page ranks nothing and recommends nothing itself.",
    "A reviewer's family is known only from a Reviewer: signature; a family taken from the login is marked inferred. "
    "Several people may post from one GitHub account.",
    "The coordinator's status-file cost lines are not parsed; only governance/budget.jsonl counts toward the budget.",
    "The authority diagram shows only recorded delegation (an adopted decision with delegates_to). Text-matched "
    "delegations and the assumed owner -> coordinator -> lane lines are in a closed 'hypothesised authority (inferred)' "
    "overlay. Nothing executable is derived from either graph.",
    "Composed lines carry <<REQUIRED>> placeholders: the owner supplies decider, basis and locator; the ledger check "
    "refuses a line that still holds one.",
    "'Last 7 days' and 'older than N days' are measured back from the latest record timestamp, not from today.",
    "Topology layout is automatic: position carries no meaning beyond the band (lane) a node sits in.",
]

HERE = pathlib.Path(__file__).resolve().parent
try:
    from . import __version__
except ImportError:
    __version__ = re.search(r'__version__ = "([^"]+)"', (HERE / "__init__.py").read_text(encoding="utf-8")).group(1)


def load_export():
    spec = importlib.util.spec_from_file_location("relaykit_export_for_cockpit", HERE / "export.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ex = load_export()
Refused = ex.Refused
_bspec = importlib.util.spec_from_file_location("relaykit_budget_for_cockpit", HERE / "budget.py")
BG = importlib.util.module_from_spec(_bspec)
_bspec.loader.exec_module(BG)
_tspec = importlib.util.spec_from_file_location("relaykit_telemetry_for_cockpit", HERE / "telemetry.py")
TEL = importlib.util.module_from_spec(_tspec)
_tspec.loader.exec_module(TEL)


def dumps(x):
    return json.dumps(x, ensure_ascii=False, sort_keys=True)


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%MZ")


# ---------------- reading ----------------
def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def base_of(actor, owner):
    """'claude:aec#01N4' -> 'claude:aec'; the owner's bare name -> 'owner'; anything else -> None."""
    s = str(actor or "").strip()
    if s and s.lower() in (owner, "owner"):
        return "owner"
    sp = ex.split_actor(s)
    return sp[0][0] if sp else None


def read_budget(root, rd):
    p = root / "governance" / "budget.jsonl"
    if not p.is_file() or p.is_symlink():
        return None, []
    raw = rd.read(p, "budget")
    rows, bad, raws = [], [], {}
    for n, line in enumerate(raw.split(b"\n"), 1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
            if not isinstance(r, dict):
                raise ValueError("not an object")
            for pr in BG.row_problems(r):    # the same check every budget reader makes
                raise ValueError(pr)
            raws[n] = line.decode("utf-8", "replace")
            sp = ex.span(r.get("ts"))
            if not sp:
                raise ValueError("ts isn't an ISO time")
        except ValueError as e:
            bad.append({"line": n, "reason": str(e)})
            continue
        rows.append((n, sp[0], r))
    kept, report = BG.effective([(n, raws[n], r) for n, _, r in rows])
    at = {n: (ex.span(a)[0] if ex.span(a) else None) for n, _, _, a in kept}   # counted at the replaced line's time
    ids = {n: i for n, i, _, _ in kept}
    return {"rows": rows, "bad": bad, "counted": at, "ids": ids, "corrections": report}, bad


def captures(rd, threads):
    """conversation@1 captures, read with the export's conv_records (topology nodes only)."""
    out = []
    for p in rd.walk_json():
        if rd.rel(p) == "handoffs/.ledger.json" or rd.rel(p).startswith("governance/"):
            continue
        try:
            raw = p.read_bytes()
            d = json.loads(raw)
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict):
            continue
        legacy = d.get("schema") is None and isinstance(d.get("turns"), list) and isinstance(d.get("meta"), dict)
        if d.get("schema") != ex.CONVERSATION and not legacy:
            continue
        try:
            conv, _ = ex.conv_records(rd, p, raw, d, threads)
        except (ValueError, AttributeError, TypeError):
            continue
        out.append(conv)
    return sorted(out, key=lambda c: c["rid"])


# ---------------- composing paste lines ----------------
REQ = {"decider": "<<REQUIRED: your name>>", "basis": "<<REQUIRED: your words, quoted>>",
       "locator": "<<REQUIRED: where you said it (thread, date, time)>>"}


def ledger_cmd(kind, obj, expect=None):
    """expect: 'DEC-N=open@hash', or several. ledger_append refuses the line if any item changed since this
    page (relay-kit/lifecycle@1)."""
    ex_ = "".join(f" --expect {shlex.quote(x)}" for x in ([expect] if isinstance(expect, str) else expect or []) if x)
    return f"relay-kit ledger append --dir governance{ex_} {kind} {shlex.quote(dumps(obj))}"


def state_name(rec):
    """A record's state as ledger_append --expect names it."""
    if rec.get("kind") == "challenge":
        return "open" if rec.get("open") else "closed"
    a = rec.get("applicability") or "open"
    return "standing" if a.startswith("adopted, no structured") else a


def expect_of(rec, state=None):
    rid = (rec.get("fields") or {}).get("id")
    h = str(rec.get("source_hash") or "")[:12]
    return f"{rid}={state or state_name(rec)}" + (f"@{h}" if h else "") if rid else None


def expects_for(rec, by_id):
    """The item itself, and what it proposes to replace: accepting a replacement whose target has since
    been settled some other way would adopt both (CH-035 finding 5: revalidate what it depends on)."""
    out = [expect_of(rec, "open")]
    x = (rec.get("fields") or {}).get("supersedes")
    if isinstance(x, str) and x in by_id:
        out.append(expect_of(by_id[x]))
    return [e for e in out if e]


def compose_decision(rec, owner, as_of, full, by_id=None):
    f = rec["fields"]
    did = f.get("id")
    text = f.get("decision") if full else f"As proposed in {did} (text left out of this page)."
    scope = f.get("scope") if full else f"As in {did}."
    out = {}
    for action, status in (("accept", "adopted"), ("reject", "rejected")):
        obj = {"status": status, "supersedes": did, "decision": text or f"As proposed in {did}.",
               "scope": scope or f"As in {did}.", "decider": REQ["decider"],
               "basis": REQ["basis"], "locator": REQ["locator"]}   # recorded_by: ledger_append fills it from $RELAYKIT_ACTOR
        out[action] = {"command": ledger_cmd("decision", obj, expects_for(rec, by_id or {})), "json": obj}
    out["defer"] = {"command": f"# {did}: deferred. Nothing to append; it stays proposed and stays on this page.",
                    "json": None}
    return out


def compose_challenge(rec, owner, full):
    f = rec["fields"]
    cid = f.get("id")
    concern = f.get("concern") if full else f"As raised in {cid} (text left out of this page)."
    out = {}
    for action, status, why in (("accept", "accepted-risk", "<<REQUIRED: why you accept the risk, in your words>>"),
                                ("reject", "resolved", "<<REQUIRED: why it is dismissed, or how it was fixed, in your words>>")):
        obj = {"status": status, "resolves": cid, "concern": concern or f"As raised in {cid}.",
               "raised_by": f.get("raised_by") or owner, "resolution": why}
        out[action] = {"command": ledger_cmd("challenge", obj, expect_of(rec)), "json": obj}
    out["defer"] = {"command": f"# {cid}: deferred. Nothing to append; it stays open and stays on this page.",
                    "json": None}
    return out


def next_state(h, led):
    """The state Accept moves a handoff to, by handoff.py's own rules (led.NEXT, led.legal): a
    blocked handoff returns to the last non-blocked state in its log; otherwise the earliest of
    NEXT[status] (acknowledged -> in_progress, in_progress -> completed)."""
    st = h["status"]
    states = [e["state"] for e in h["log"] if e.get("state")] or [st]
    if states[-1] != st:   # header and log disagree; trust the header's status as the current state
        states.append(st)
    if st == "blocked":
        nxt = next((s for s in reversed(states) if s != "blocked"), "prepared")
    else:
        nxt = min(led.NEXT.get(st, set()), key=STATE_ORDER.index, default=None)
    return nxt if nxt and led.legal(states, nxt) is None else None


def compose_handoff(h, owner, unauthorized, nxt, output_exists):
    hid, st = h["handoff_id"], h["status"]
    pre = f"# first write your words under '## Authorized' in handoffs/{hid}.md\n" if unauthorized else ""
    if nxt == "completed" and not output_exists:   # handoff.py refuses completed without the output file
        pre += f"# first write the result to handoffs/outputs/{hid}.md (completed needs it); the note says where it is\n"
    if nxt is None:
        acc = f"# {hid}: no legal next state from {st}; only Reject (cancel) applies."
        return {"accept": {"command": acc, "json": None, "state": None},
                "reject": {"command": (f"relay-kit handoff --dir handoffs set {hid} cancelled "
                                       f"--note {shlex.quote('<<REQUIRED: why it is cancelled, in your words>>')}"),
                           "json": None, "state": "cancelled"},
                "defer": {"command": f"# {hid}: deferred. Nothing to run; it stays {st}.", "json": None}}
    # no --by: handoff.py takes the actor from $RELAYKIT_ACTOR, i.e. whoever runs the line
    acc = pre + (f"relay-kit handoff --dir handoffs set {hid} {nxt} "
                 f"--note {shlex.quote('<<REQUIRED: your words>>')}")
    rej = (f"relay-kit handoff --dir handoffs set {hid} cancelled "
           f"--note {shlex.quote('<<REQUIRED: why it is cancelled, in your words>>')}")
    return {"accept": {"command": acc, "json": None, "state": nxt},
            "reject": {"command": rej, "json": None, "state": "cancelled"},
            "defer": {"command": f"# {hid}: deferred. Nothing to run; it stays {st}.", "json": None}}


# ---------------- what a choice is asking (HO-RK-20261105-VZH4) ----------------
ID_REF = re.compile(r"\b(?:DEC|CH)-\d{3,}\b|\bHO-[A-Z0-9]{1,12}-\d{8}-[0-9A-HJKMNP-TV-Z]{4}\b|\bHO-\d{8}-\d{3}\b")
# a pull request named in prose: a full URL, or "<repo> PR #N" / "<repo> pull request #N" (bare "#N" is too ambiguous)
PR_REF = re.compile(r"https://github\.com/([\w-][\w.-]*)/([\w-][\w.-]*)/pull/(\d+)|\b([A-Za-z][\w.-]*)\s+(?:PR|[Pp]ull request)\s*#(\d+)")
ASK_FIELDS = "ask: {question, options: [{label, effect}], default_if_unanswered}"


OVERTAKE_WORDS = re.compile(r"overtak|supersed|\bclos(?:e|es|ed|ing)\b|replac|\bdone\b|execut|\bmerged\b|\bmoot\b|"
                            r"\balready\b|instead of|carried out|implemented|amend", re.I)
NEGATION = re.compile(r"\b(?:not|no|never|nor|without|isn't|wasn't|aren't|don't|doesn't|didn't|won't|shouldn't|"
                      r"cannot|can't|keep|keeps|kept|leave|leaves|open|until|yet)\b[^.;]{0,30}$", re.I)


SEP = "\n\u00b6\n"   # between a record's fields in its searchable text


def closure_phrase(text, start, end):
    """The phrase near an id (same text, within 160 characters) saying something closed, replaced,
    amended or carried it out, unless a negation shortly before it says otherwise ('not done',
    'keep it open'). Searched in record text only, never in field names. None if there is none."""
    seg_lo = text.rfind(SEP, 0, start)
    seg_hi = text.find(SEP, end)
    seg_lo = 0 if seg_lo < 0 else seg_lo + len(SEP)   # stay within the field that names the id
    seg_hi = len(text) if seg_hi < 0 else seg_hi
    lo, hi = max(seg_lo, start - 160), min(seg_hi, end + 160)
    for m in OVERTAKE_WORDS.finditer(text, lo, hi):
        if NEGATION.search(text[max(0, m.start() - 40): m.start()]):
            continue
        s, e_ = max(lo, m.start() - 40), min(hi, m.end() + 40)
        return " ".join(text[s:e_].split())
    return None


def record_text(fields):
    """A ledger line's searchable text: its string values only, so a field name ('supersedes') never matches."""
    return SEP.join(str(v) for k, v in sorted(fields.items()) if isinstance(v, str))


def ledger_num(i):
    m = re.search(r"-(\d+)$", str(i))
    return int(m.group(1)) if m else -1


def first_sentence(s, n=220):
    s = " ".join(str(s or "").split())
    m = re.match(r"(.+?[.?!])(\s|$)", s)
    s = m.group(1) if m else s
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def written_ask(a):
    """The proposer's own `ask` object, normalised; None when it's absent or unusable (then the
    card shows a derived summary, labelled as such)."""
    if not isinstance(a, dict) or not isinstance(a.get("question"), str) or not a["question"].strip().endswith("?") \
            or len(a["question"].strip()) < 3:
        return None   # the ledger check flags it; the card falls back to the derived question
    opts = [{"label": o["label"].strip(), "effect": o["effect"].strip()} for o in (a.get("options") or [])
            if isinstance(o, dict) and isinstance(o.get("label"), str) and isinstance(o.get("effect"), str)
            and o["label"].strip() and o["effect"].strip()]
    d = a.get("default_if_unanswered")
    return {"source": "proposer", "question": a["question"].strip(), "options": opts,
            "default_if_unanswered": d.strip() if isinstance(d, str) and d.strip() else None}


def derived_ask(kind, cid, text, full, nxt=None, why=()):
    """A plain question built from the record when the proposer wrote none. Labelled 'derived':
    it is this page's reading, never the proposer's words. Effects describe what each action's
    composed line would do once you run it."""
    gist = first_sentence(text) if full and text else None
    if kind == "decision":
        q = f"Should this be adopted: “{gist}”?" if gist else f"Should {cid} be adopted?"
        opts = [("Accept", f"an adopted line supersedes {cid}: it stands as adopted (no later line ends it), with your words as the basis"),
                ("Reject", f"a rejected line supersedes {cid}: it is closed and won't be acted on"),
                ("Amend", f"an adopted line supersedes {cid} with your edited text instead of the proposal's"),
                ("Defer", "nothing is written: it stays proposed and stays here")]
    elif kind == "challenge":
        q = f"Is this a real problem: “{gist}”?" if gist else f"Is {cid} a real problem?"
        opts = [("Accept", f"an accepted-risk line resolves {cid}: you accept the risk, no fix"),
                ("Reject", f"a resolved line resolves {cid}: dismissed, or fixed (your resolution says which)"),
                ("Defer", "nothing is written: it stays open and stays here")]
    else:
        reason = "; ".join(why) or "it waits on you"
        q = (f"{cid} needs you ({reason}). Should it move to {nxt}?" if nxt
             else f"{cid} needs you ({reason}). Should it be cancelled?")
        opts = ([("Accept", f"handoff.py records {nxt}, with your note")] if nxt else []) + \
               [("Reject", "handoff.py records cancelled (owner only), with your note"),
                ("Defer", "nothing is run: it stays as it is")]
    return {"source": "derived", "question": q, "options": [{"label": l, "effect": e_} for l, e_ in opts],
            "default_if_unanswered": "nothing changes: it stays waiting on you"}


def pr_refs(text, owner_hint, known=()):
    """(owner, repo, number) for each pull request named in text; a bare '<repo> PR #N' counts only
    when <repo> is a repository the records spell out as '<owner>/<repo>' somewhere (`known`, lowercased),
    and takes the records' GitHub owner. De-duplicated, in order."""
    out = []
    for m in PR_REF.finditer(str(text or "")):
        bare_ok = owner_hint and m.group(4) and m.group(4).lower() in known
        ref = (m.group(1), m.group(2), int(m.group(3))) if m.group(1) else \
              ((owner_hint, m.group(4), int(m.group(5))) if bare_ok else None)
        if ref and ref not in out:
            out.append(ref)
    return out


def gh_pr_state(owner, repo, number):
    """'merged', 'closed' or 'open' from GitHub through the gh CLI, or None. Build time only, opt-in."""
    import subprocess
    try:
        p = subprocess.run(["gh", "api", f"repos/{owner}/{repo}/pulls/{number}", "-q",
                            '(if .merged_at then "merged" else .state end)'],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    s = p.stdout.strip()
    return s if p.returncode == 0 and s in ("merged", "closed", "open") else None


def records_github_owner(root):
    """The GitHub owner of the records' origin (for '<repo> PR #N'), or None."""
    import subprocess
    try:
        url = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"], capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"github\.com[/:]([\w.-]+)/", url)
    return m.group(1) if m else None


def compose_extra(kind, cid, owner, proposer, full, text, scope, expect=None):
    """Amend (decisions), Close as overtaken, Clarify and Inquire. All compose; none writes.
    Close as overtaken is a 'superseded' line: it closes the proposal without adopting anything.
    Clarify is a record: an open challenge with kind 'question', about the item and addressed to its
    proposer; whoever answers resolves it. Recording it delivers nothing: the owner still has to tell
    them. Inquire is not a record: a prompt for the owner to paste to an assistant."""
    out = {}
    if kind == "decision":
        amend = {"status": "adopted", "supersedes": cid, "decision": text if full and text else f"<<REQUIRED: your text for {cid}>>",
                 "scope": scope if full and scope else f"As in {cid}.", "amends": cid,
                 "decider": REQ["decider"], "basis": REQ["basis"], "locator": REQ["locator"]}
        out["amend"] = {"json": amend, "command": ledger_cmd("decision", amend, expect), "edit_field": "decision"}
        over = {"status": "superseded", "supersedes": cid, "decision": f"{cid} is closed as overtaken, not adopted: <<REQUIRED: what overtook it (a merged PR, a later decision)>>",
                "scope": scope if full and scope else f"As in {cid}.",
                "decider": REQ["decider"], "basis": REQ["basis"], "locator": REQ["locator"]}
        out["overtaken"] = {"json": over, "command": ledger_cmd("decision", over, expect), "edit_field": "decision"}
    elif kind == "challenge":
        over = {"status": "resolved", "resolves": cid, "concern": f"As raised in {cid}.",
                "raised_by": proposer or owner, "resolution": f"Overtaken: <<REQUIRED: what made {cid} moot, in your words>>"}
        out["overtaken"] = {"json": over, "command": ledger_cmd("challenge", over, expect)}
    clar = {"status": "open", "kind": "question", "about": cid, "addressed_to": proposer or "<<REQUIRED: who should answer>>",
            "concern": "<<REQUIRED: your question>>", "raised_by": owner}
    out["clarify"] = {"json": clar, "command": ledger_cmd("challenge", clar), "edit_field": "concern"}
    out["inquire"] = {"json": None, "command": None,
                      "prompt": (f"Explain {cid} from my records (relay-kit status, or the ledgers and handoffs directly): "
                                 f"what is being decided, what has changed since it was proposed, and the consequence "
                                 f"of each option. Cite record ids. This is a question, not an instruction: don't "
                                 f"change any record.")}
    return out


# ---------------- pull requests, from relay-kit prs (HO-RK-20261105-SWK5) ----------------
PRS_FILE = "governance/pr-state.jsonl"
PRS_SCHEMA = "relay-kit/prs@3"
PRS_READS = {"relay-kit/prs@1", "relay-kit/prs@2", PRS_SCHEMA}
PR_SECTIONS = ("what", "why", "how", "authorized")
APPROVING = {"APPROVED", "COMMENTED", "CHANGES_REQUESTED"}
# counts a review body states in so many words; nothing is counted by the page itself (DEC-139: no AI summarising)
FINDINGS = {"blocking": re.compile(r"\b(\d{1,3})\s+blocking\b", re.I),
            "should_fix": re.compile(r"\b(\d{1,3})\s+should[- ]fix", re.I)}


def read_pr_state(root):
    """The latest line per pull request and per repository fetch in governance/pr-state.jsonl, or None
    when the file is absent. Lines of another schema version are counted as unreadable, not guessed at."""
    path = pathlib.Path(root) / PRS_FILE
    if not path.is_file():
        return None
    latest, fetches, bad = {}, {}, 0
    for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not ln.strip():
            continue
        try:
            x = json.loads(ln)
        except json.JSONDecodeError:
            bad += 1
            continue
        if not isinstance(x, dict) or x.get("schema") not in PRS_READS:
            bad += 1
            continue
        if x.get("kind") == "pr" and isinstance(x.get("repo"), str) and isinstance(x.get("number"), int):
            latest[(x["repo"], x["number"])] = x
        elif x.get("kind") == "fetch":
            failed = x.get("failed") if isinstance(x.get("failed"), dict) else {}
            for r in x.get("repos") or []:
                f = fetches.setdefault(r, {"repo": r, "last_ok": None, "last_try": None, "error": None, "since_days": None,
                                           "partial": 0, "more_available": None})
                f["last_try"] = x.get("ts")
                if r in (x.get("ok") or []):
                    f["last_ok"], f["error"] = x.get("ts"), None
                    f["since_days"] = x.get("since_days") if isinstance(x.get("since_days"), int) else None
                    cov = (x.get("coverage") or {}).get(r) if isinstance(x.get("coverage"), dict) else None
                    f["partial"] = len(cov.get("partial") or []) if isinstance(cov, dict) else 0
                    f["more_available"] = cov.get("more_available") if isinstance(cov, dict) else None
                else:
                    f["error"] = str(failed.get(r) or "failed")[:200]
        else:
            bad += 1
    return {"latest": latest, "fetches": fetches, "unreadable": bad}


def review_family(name, families):
    """A reviewer's or author's family: governance/cockpit.json 'review_families' by login or name (case-
    insensitive, then the first word), else the part before ':' of an actor (claude:relay-kit -> claude)."""
    if not name:
        return None
    n = str(name).strip().lower()
    for k in (n, re.split(r"[\s(#]", n, maxsplit=1)[0]):
        if k in families:
            return families[k]
    return n.split(":", 1)[0] if re.match(r"[a-z][\w.-]*:", n) else None


def written_by(line, owners):
    """'owner' only when the Author: line is the owner's name (or one of owner_names), alone or with a
    note in brackets; 'agent' for anything else; 'unstated' when there is no line or it is blank."""
    if not line or not line.strip():
        return "unstated"
    l = line.strip().lower()
    bare = re.split(r"\s*[(\[]", l, maxsplit=1)[0].strip()
    return "owner" if l in owners or bare in owners else "agent"


def iso_minus_days(ts, days):
    try:
        return (dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ") - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None


def stated_findings(body):
    """{blocking: N, should_fix: M} only for the counts the body itself states ("2 blocking", "10 should-fix")."""
    out = {}
    for k, rx in FINDINGS.items():
        m = rx.search(body or "")
        if m:
            out[k] = int(m.group(1))
    return out or None


def actors_map(cfg):
    """handoffs/.ledger.json 'actors' (@5, optional): GitHub login -> family, or login -> {family, lanes}. Lower-cased
    logins; anything malformed is ignored."""
    raw = cfg.get("actors") if isinstance(cfg, dict) and isinstance(cfg.get("actors"), dict) else {}
    out = {}
    for k, v in raw.items():
        fam = v.get("family") if isinstance(v, dict) else v
        if isinstance(k, str) and isinstance(fam, str) and fam.strip():
            out[k.strip().lower()] = fam.strip()
    return out


def pr_items(ps, owner, settings, full, actors=None):
    fams = {str(k).lower(): str(v) for k, v in (settings.get("review_families") or {}).items()} \
        if isinstance(settings.get("review_families"), dict) else {}
    actors = {**(actors or {}), **fams}   # cockpit.json's explicit mapping wins over the records' actor register
    owners = {owner} | {str(x).lower() for x in settings.get("owner_names") or [] if isinstance(x, str)}
    mapped = lambda name: actors.get(str(name).strip().lower()) if name else None
    items = []
    for (repo, num), x in ps["latest"].items():
        line = x.get("author_line") if isinstance(x.get("author_line"), str) and x["author_line"].strip() else None
        written = written_by(line, owners)
        # in metadata mode nothing is derived from free text: families come from the mapping by login only
        afam = review_family(line or x.get("author"), fams) if full else mapped(x.get("author"))
        reviews, ids = [], set()
        for r in sorted((r for r in x.get("reviews") or [] if isinstance(r, dict)), key=lambda r: str(r.get("at") or "")):
            signed = r.get("signed") if full and isinstance(r.get("signed"), str) else None
            # family: a Reviewer: signature is the reviewer's own word; a login mapping (review_families or the
            # records' actors register) is an inference, and is labelled so (DEC-139: never silently)
            fam, source = (review_family(signed, fams), "signed") if full and signed else (None, None)
            if not fam:
                fam, source = mapped(r.get("by")), ("login" if mapped(r.get("by")) else None)
            kind = r.get("kind") if r.get("kind") in ("review", "comment") else "review"
            body = r.get("body") if full and isinstance(r.get("body"), str) else None
            reviews.append({"who": signed or r.get("by") or "?", "login": r.get("by"), "signed": signed, "kind": kind,
                            "verdict": r.get("verdict"), "at": r.get("at"),
                            "stated": r.get("stated") if isinstance(r.get("stated"), str) else None,
                            "family": fam, "family_source": source,
                            "body": body, "body_recorded": isinstance(r.get("body"), str),
                            "truncated": bool(r.get("truncated")), "comments": r.get("comments") if isinstance(r.get("comments"), int) else None,
                            "findings": stated_findings(r.get("body")) if full else None})
        counted = [r for r in reviews if (r["verdict"] in APPROVING or (r["kind"] == "comment" and r["stated"]))
                   and not (line and (r["who"] or "").lower() == line.lower())]
        ids = {(r["who"] or "").lower() for r in counted}
        if not counted:
            other = False
        elif afam and any(r["family"] and r["family"] != afam for r in counted):
            other = True
        else:
            other = None if (not afam or any(not r["family"] for r in counted)) else False
        secs = x.get("body_sections") if isinstance(x.get("body_sections"), dict) else {}
        url = x.get("url") if isinstance(x.get("url"), str) and x["url"].startswith("https://github.com/") else None
        ci = x.get("ci") if isinstance(x.get("ci"), dict) else {}
        fetch = ps["fetches"].get(repo) or {}
        since = iso_minus_days(fetch.get("last_ok"), fetch.get("since_days") or 14)
        when = str(x.get("merged_at") or x.get("closed_at") or x.get("updated_at") or "")
        items.append({
            "repo": repo, "number": num, "url": url, "title": x.get("title") if full else None,
            "state": x.get("state"), "draft": bool(x.get("draft")), "updated_at": x.get("updated_at"),
            "merged_at": x.get("merged_at"), "opened_by": x.get("author"), "author_line": line if full else None,
            "written_by": written, "author_family": afam, "changed_at": x.get("ts"), "read_at": fetch.get("last_ok"),
            "recent": x.get("state") == "open" or not since or when >= since,
            "sections": {k: secs[k] for k in PR_SECTIONS if isinstance(secs.get(k), str)} if full else {},
            "present": [k for k in PR_SECTIONS if isinstance(secs.get(k), str)],
            "missing": [k for k in PR_SECTIONS if not isinstance(secs.get(k), str)],
            "truncated": list(x.get("truncated") or []),
            "reviews": reviews, "reviewers_counted": len(ids), "other_family": other,
            "ci": {"state": ci.get("state") or "none", "failing": list(ci.get("failing") or [])},
            "unread": [u for u in x.get("unread") or [] if u in ("reviews", "ci")]})
    group = {"open": 0, "merged": 2, "closed": 2}
    items.sort(key=lambda i: (i["repo"], i["number"]))
    items.sort(key=lambda i: str(i["merged_at"] or i["updated_at"] or ""), reverse=True)   # newest first within a group
    items.sort(key=lambda i: group.get(i["state"], 4) + (1 if i["draft"] and i["state"] == "open" else 0))
    return items


# ---------------- status editions, relay-kit/status@1 (HO-RK-20261106-NXPR, DEC-139) ----------------
STATUS_DIR = "status"
STATUS_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})-([a-z][a-z0-9-]{0,30})\.md$")
STATUS_SECTIONS = ("Accomplishments", "Road bumps", "Status", "Next steps")
EDITION_ORDER = ["morning", "midday", "noon", "afternoon", "evening", "night"]   # within one day; others after, by name
FRONT = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n", re.S)


def edition_rank(name):
    return (EDITION_ORDER.index(name), "") if name in EDITION_ORDER else (len(EDITION_ORDER), name)


def parse_front(text):
    """(fields, body): a YAML-like block of `key: value` lines between --- fences; nothing else is parsed."""
    m = FRONT.match(text)
    if not m:
        return {}, text
    fields = {}
    for ln in m.group(1).split("\n"):
        k, sep, v = ln.partition(":")
        if sep and re.fullmatch(r"[A-Za-z_][\w-]*", k.strip()):
            fields[k.strip()] = v.strip()
    return fields, text[m.end():]


def read_status(root, full):
    """Every status/<YYYY-MM-DD>-<edition>.md: newest first (date, then edition order). A file whose name or
    front matter doesn't fit is listed as unreadable, never guessed at. Body text only with --content full."""
    d = pathlib.Path(root) / STATUS_DIR
    if not d.is_dir() or d.is_symlink():
        return {"present": False, "dir": STATUS_DIR, "editions": [], "unreadable": []}
    eds, bad = [], []
    for f in sorted(d.iterdir()):
        if not f.is_file() or f.is_symlink():
            continue
        m = STATUS_FILE.match(f.name)
        if not m:
            bad.append({"file": f.name, "reason": "name isn't <YYYY-MM-DD>-<edition>.md"})
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as x:
            bad.append({"file": f.name, "reason": f"unreadable: {type(x).__name__}"})
            continue
        fields, body = parse_front(text.replace("\r\n", "\n"))
        problems = []
        if fields.get("date") != m.group(1):
            problems.append("front matter date doesn't match the file name" if fields.get("date") else "no date in front matter")
        if fields.get("edition") != m.group(2):
            problems.append("front matter edition doesn't match the file name" if fields.get("edition") else "no edition in front matter")
        if not fields.get("author"):
            problems.append("no author")
        heads = [h.strip() for h in re.findall(r"^##[ \t]+(.+?)[ \t]*$", body, re.M)]
        missing = [sec for sec in STATUS_SECTIONS if not any(h.lower().startswith(sec.lower()) for h in heads)]
        title = next((t.strip() for t in re.findall(r"^#[ \t]+(.+?)[ \t]*$", body, re.M)), None)
        eds.append({"file": f"{STATUS_DIR}/{f.name}", "date": m.group(1), "edition": m.group(2),
                    "author": fields.get("author") or None, "covers": fields.get("covers") or None,
                    "contract": fields.get("contract") or None, "title": title if full else None,
                    "sections_missing": missing, "problems": problems, "body": body.strip() if full else None,
                    "chars": len(body)})
    eds.sort(key=lambda x: (x["date"], edition_rank(x["edition"])), reverse=True)
    return {"present": True, "dir": STATUS_DIR, "editions": eds, "unreadable": bad}


# ---------------- actions: the human action list, relay-kit/actions@1 (HO-CKPT-20261107-EBVA, DEC-151) ----------------
ACTIONS_DIR = "actions"
ACTION_LINE = re.compile(r"^\s*[-*]\s+\[([ xX])\]\s+(.*?)\s*$")
SINCE = re.compile(r"\b[Ss]ince\s+(\d{4}-\d{2}-\d{2})\b")
DUE = re.compile(r"\b[Dd]ue\s+(\d{4}-\d{2}-\d{2})\b")
SIZE = re.compile(r"(?<![\w-])(~?\d+(?:[.,]\d+)?\s*(?:min(?:ute)?s?|h(?:ours?|rs?)?|days?|words?)|one word(?: each)?)(?![\w-])", re.I)
DONE_DATE = re.compile(r"~~.*?~~\s*\(?(\d{4}-\d{2}-\d{2})?")


def parse_actions(text, file):
    """One markdown checklist as written: `- [ ]` lines are open items, `- [x]` lines (or struck-through text)
    are done; the heading above a line is its section; `Since YYYY-MM-DD`, `Due YYYY-MM-DD` and a size such as
    `~15 min`, `2 min`, `1 h`, `one word each` are read out of the line when the author wrote them, else left
    null (never guessed). Everything else in the file (title, paragraphs) is kept as prose."""
    text = (text or "").replace("\r\n", "\n")
    title, section, items, prose = None, None, [], []
    for n, raw in enumerate(text.split("\n"), 1):
        ln = raw.rstrip()
        h = re.match(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$", ln)
        if h:
            if len(h.group(1)) == 1 and title is None:
                title = h.group(2).strip()
            else:
                section = h.group(2).strip()
            continue
        m = ACTION_LINE.match(ln)
        if m:
            body = m.group(2)
            done = m.group(1).lower() == "x" or (body.startswith("~~") and "~~" in body[2:])
            since, due, size = SINCE.search(body), DUE.search(body), SIZE.search(body)
            items.append({"text": body, "done": done, "section": section, "line": n,
                          "since": since.group(1) if since else None, "due": due.group(1) if due else None,
                          "size": size.group(1).strip() if size else None, "file": file})
            continue
        if ln.strip() and not items:
            prose.append(ln.strip())
    return {"file": file, "title": title, "items": items, "intro": " ".join(prose) if prose else None}


def read_actions(root, path, full):
    """--actions FILE (one file, anywhere) or every actions/*.md in the records store. Text only with --content
    full; in metadata mode each item keeps its state, dates, size and section, not its words."""
    files, bad = [], []
    if path:
        p = pathlib.Path(path)
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as x:
            raise Refused(f"--actions {p}: {type(x).__name__}: {x}")
        try:   # name the file by its place in the records, never by the machine's absolute path
            label = str(p.resolve().relative_to(pathlib.Path(root).resolve()))
        except ValueError:
            label = p.name
        files.append(parse_actions(text, label))
        source = "--actions"
    else:
        d = pathlib.Path(root) / ACTIONS_DIR
        source = f"{ACTIONS_DIR}/"
        if d.is_dir() and not d.is_symlink():
            for f in sorted(d.iterdir()):
                if not f.is_file() or f.is_symlink() or f.suffix != ".md":
                    continue
                try:
                    files.append(parse_actions(f.read_text(encoding="utf-8"), f"{ACTIONS_DIR}/{f.name}"))
                except (OSError, UnicodeDecodeError) as x:
                    bad.append({"file": f.name, "reason": f"unreadable: {type(x).__name__}"})
    if not full:
        for fl in files:
            fl["title"] = None
            fl["intro"] = None
            for it in fl["items"]:
                it["text"] = None
    return {"present": bool(files), "source": source, "files": files, "unreadable": bad}


# ---------------- decision support, relay-kit/decision-support@1 (DEC-140 §1) ----------------
SUPPORT_FILE = "governance/decision-support.jsonl"
SUPPORT_FIELDS = [("changes_if_accepted", "What changes"), ("unchanged", "What doesn't"),
                  ("counterfactual", "If nothing is done"), ("can_wait", "Can it wait"),
                  ("depends", "Depends on it"), ("recommendation", "Recommendation")]
SUPPORT_SCHEMAS = {"relay-kit/decision-support@1", "kairos/decision-support@0"}   # @0 is the pilot form, same fields


def read_support(root, full):
    """governance/decision-support.jsonl: authored notes per item. Latest line per (item, author) is current;
    earlier ones are its history. Nothing is generated from them."""
    path = pathlib.Path(root) / SUPPORT_FILE
    if not path.is_file() or path.is_symlink():
        return {"present": False, "file": SUPPORT_FILE, "by_item": {}, "unreadable": 0, "lines": 0}
    by_item, bad, n = {}, 0, 0
    for i, ln in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if not ln.strip():
            continue
        try:
            x = json.loads(ln)
        except json.JSONDecodeError:
            bad += 1
            continue
        if not isinstance(x, dict) or not isinstance(x.get("item"), str) or not isinstance(x.get("author"), str) \
                or (x.get("contract") and x.get("contract") not in SUPPORT_SCHEMAS):
            bad += 1
            continue
        n += 1
        note = {"id": x.get("id"), "author": x["author"], "at": x.get("at"), "line": i, "contract": x.get("contract"),
                "fields": {k: (x.get(k) if full else None) for k, _ in SUPPORT_FIELDS},
                "present": [k for k, _ in SUPPORT_FIELDS if isinstance(x.get(k), str) and x[k].strip()]}
        by_item.setdefault(x["item"], []).append(note)
    return {"present": True, "file": SUPPORT_FILE, "by_item": by_item, "unreadable": bad, "lines": n}


def support_for(sup, cid, owner, lane_of, coords):
    """The current note per author for one item, in reading order: authors outside the coordinator lane first
    (the independent note is read before the coordinator's, DEC-140), then the coordinator, then by name; each
    with its earlier revisions. The order is a reading order, not a ranking."""
    notes = sup["by_item"].get(cid) or []
    by_author = {}
    for nt in notes:
        by_author.setdefault(base_of(nt["author"], owner) or nt["author"], []).append(nt)
    out = []
    for base, revs in by_author.items():
        revs = sorted(revs, key=lambda n: (str(n.get("at") or ""), n["line"]))
        ln = lane_of(base) if base != "owner" else None
        out.append({"author": revs[-1]["author"], "author_base": base, "lane": ln, "role": "coordinator" if ln in coords else
                    ("owner" if base == "owner" else "independent"), "current": revs[-1], "history": revs[:-1]})
    rank = {"independent": 0, "owner": 1, "coordinator": 2}
    out.sort(key=lambda b: (rank[b["role"]], b["author_base"]))
    return out


# ---------------- text with structure (DEC-140 §2) and a markdown subset (status@1) ----------------
ENUM = re.compile(r"(?:^|(?<=\s))\((\d{1,2})\)\s+")
INLINE = [(re.compile(r"`([^`\n]+)`"), lambda m: f"<code>{m.group(1)}</code>"),
          (re.compile(r"\*\*([^*\n]+?)\*\*"), lambda m: f"<b>{m.group(1)}</b>"),
          (re.compile(r"(?<![\w*])\*([^*\n]+?)\*(?![\w*])"), lambda m: f"<i>{m.group(1)}</i>"),
          (re.compile(r"(?<![\w_])_([^_\n]+?)_(?![\w_])"), lambda m: f"<i>{m.group(1)}</i>"),
          (re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)"), lambda m: f"{m.group(1)} <span class=\"url\">({m.group(2)})</span>")]


def inline_md(escaped):
    """Bold, italic, inline code and links-as-text over already-escaped text. Nothing becomes a tag but these."""
    out = escaped
    for rx, fn in INLINE:
        out = rx.sub(fn, out)
    return out


def structured(text, md=False):
    """Plain record text as HTML with its structure kept (DEC-140 §2): blank lines separate paragraphs;
    lines opening with '- ', '* ' or 'N. ' / 'N) ' are list items; a line holding '(1) … (2) …' is an
    introduction followed by a numbered list. Everything is escaped first; with md, the inline markdown
    subset (bold, italic, code, link text) is applied afterwards."""
    if not text:
        return ""
    fmt = (lambda t: inline_md(e(t))) if md else e
    html_, para, kind, items = [], [], None, []

    def flush_para():
        nonlocal para
        if para:
            html_.append(f'<p>{"<br>".join(fmt(x) for x in para)}</p>')
            para = []

    def flush_list():
        nonlocal items, kind
        if items:
            html_.append(f'<{kind}>' + "".join(f"<li>{fmt(x)}</li>" for x in items) + f'</{kind}>')
            items, kind = [], None

    def enum_split(line):
        """'Seven findings: (1) a. (2) b.' -> ('Seven findings:', ['a.', 'b.']) when (1)…(n) run in order."""
        ms = list(ENUM.finditer(line))
        nums = [int(m.group(1)) for m in ms]
        if len(ms) < 2 or nums != list(range(1, len(ms) + 1)):
            return None
        intro = line[:ms[0].start()].strip()
        parts = [line[ms[i].end():(ms[i + 1].start() if i + 1 < len(ms) else len(line))].strip() for i in range(len(ms))]
        return intro, parts

    for raw in str(text).replace("\r\n", "\n").split("\n"):
        ln = raw.rstrip()
        if not ln.strip():
            flush_para(); flush_list()
            continue
        m = re.match(r"^\s*(?:([-*•])|(\d{1,3})[.)])\s+(.*)$", ln)
        if m:
            k = "ul" if m.group(1) else "ol"
            if kind and kind != k:
                flush_list()
            flush_para()
            kind, items = k, items + [m.group(3)]
            continue
        en = enum_split(ln)
        if en:
            flush_para(); flush_list()
            if en[0]:
                html_.append(f"<p>{fmt(en[0])}</p>")
            html_.append("<ol>" + "".join(f"<li>{fmt(x)}</li>" for x in en[1]) + "</ol>")
            continue
        flush_list()
        para.append(ln.strip())
    flush_para(); flush_list()
    return "".join(html_)


def markdown_subset(text):
    """The status@1 rendering: ATX headings, lists, paragraphs, bold/italic/code, horizontal rules, links as
    text. Fenced code is shown as text in <pre>. HTML comments (template hints) are removed. Anything else
    (HTML, images, tables) is plain text."""
    out, buf, fence = [], [], None
    text = re.sub(r"<!--.*?-->", "", text or "", flags=re.S)
    def flush():
        if buf:
            out.append(structured("\n".join(buf), md=True))
            buf.clear()
    for ln in (text or "").replace("\r\n", "\n").split("\n"):
        if fence is not None:
            if ln.strip().startswith(fence):
                out.append(f"<pre>{e(chr(10).join(fence_buf))}</pre>")
                fence = None
            else:
                fence_buf.append(ln)
            continue
        if re.match(r"^\s*(```|~~~)", ln):
            flush()
            fence, fence_buf = ln.strip()[:3], []
            continue
        h = re.match(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$", ln)
        if h:
            flush()
            lvl = min(len(h.group(1)) + 1, 6)   # the page's own h1 is the surface title; the file's # becomes h2
            out.append(f"<h{lvl}>{inline_md(e(h.group(2)))}</h{lvl}>")
            continue
        if re.fullmatch(r"\s*(?:-{3,}|\*{3,}|_{3,})\s*", ln):
            flush()
            out.append("<hr>")
            continue
        buf.append(ln)
    if fence is not None:
        out.append(f"<pre>{e(chr(10).join(fence_buf))}</pre>")
    flush()
    return "".join(out)


def snapshot_of(root, as_of):
    """Where the records this page was built from came from, kept apart from the newest record's time
    (CH-035 finding 5): the git revision, when it was committed, and when this clone last fetched
    (FETCH_HEAD's time: the collection time). Fixed for a given clone, so the page stays deterministic
    while the browser counts their age. Anything that can't be read is "unknown", never left blank."""
    import subprocess
    def g(*args):
        try:
            p = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
                               env={**os.environ, "TZ": "UTC"})
        except (OSError, subprocess.TimeoutExpired):
            return None
        return p.stdout.strip() if p.returncode == 0 else None
    top = g("rev-parse", "--show-toplevel")
    rev = g("rev-parse", "HEAD") if top else None
    if not rev or pathlib.Path(top).resolve() != pathlib.Path(root).resolve():
        return {"revision": None, "committed_at": None, "fetched_at": None, "uncommitted": None, "newest_record": as_of,
                "note": "the records folder isn't the top of a git repository: revision and collection time unknown"}
    when = g("log", "-1", "--date=format-local:%Y-%m-%dT%H:%M:%SZ", "--format=%cd")
    gitdir = g("rev-parse", "--absolute-git-dir")
    fh = pathlib.Path(gitdir) / "FETCH_HEAD" if gitdir else None
    fetched = (dt.datetime.fromtimestamp(fh.stat().st_mtime, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
               if fh and fh.is_file() else None)
    dirty = g("status", "--porcelain")       # everything the readers may read, captures included
    return {"revision": rev[:12], "committed_at": when, "fetched_at": fetched,
            "uncommitted": None if dirty is None else bool(dirty), "newest_record": as_of}


# ---------------- collecting ----------------
def collect(root, lane=None, content="full", weekly_tokens=None, drift_days=None, coordinator=None, brand=None,
            pr_lookup=None, pr_fetched_at=None, landing=None, actions=None):
    """actions: a path to one human action list (relay-kit/actions@1); default every actions/*.md under root.
    pr_lookup(owner, repo, number) -> 'merged' | 'closed' | 'open' | None, at build time only (--pr-state);
    None leaves pull request states out. Nothing here touches the network otherwise."""
    root = pathlib.Path(root)
    if not root.is_dir():
        raise Refused(f"--root {root} isn't a folder")
    if content not in ("full", "metadata"):
        raise Refused("--content must be full or metadata")
    full = content == "full"
    rd = ex.Reader(root)
    led = ex.load_ledger()
    rd.config()
    cfg = read_json(rd.root / "handoffs" / ".ledger.json") or {}
    owner = str(cfg.get("owner") or "owner").strip().lower() if isinstance(cfg, dict) else "owner"
    settings = read_json(rd.root / SETTINGS_FILE)
    settings = settings if isinstance(settings, dict) else {}
    if weekly_tokens is None and isinstance(settings.get("weekly_tokens"), int):
        weekly_tokens = settings["weekly_tokens"]
    if drift_days is None:
        drift_days = settings["drift_days"] if isinstance(settings.get("drift_days"), int) else 3
    if brand is None:
        brand = settings["brand"] if isinstance(settings.get("brand"), str) else "relay-kit"
    if landing is None:
        landing = settings["landing"] if isinstance(settings.get("landing"), str) else "overview"
    if landing not in {k for k, _, _ in SURFACES}:
        raise Refused(f"landing {landing!r} isn't a surface ({', '.join(k for k, _, _ in SURFACES)})")
    ps = read_pr_state(rd.root)
    actors = actors_map(cfg)
    support = read_support(rd.root, full)
    status = read_status(rd.root, full)
    action_lists = read_actions(rd.root, actions, full)
    # one reader: PR states come from relay-kit prs's file. --pr-state (gh at build time) is asked first when
    # given, since it is newer; the file answers the rest. Each state says where it came from.
    latest = (ps or {"latest": {}})["latest"]
    file_states = {f"{r}#{n}".lower(): (x.get("state"), ((ps or {}).get("fetches") or {}).get(r, {}).get("last_ok"))
                   for (r, n), x in latest.items()}
    gh_lookup, pr_sources = pr_lookup, {}

    def lookup(o_, r_, n_):
        k = f"{o_}/{r_}#{n_}".lower()
        if gh_lookup:
            st = gh_lookup(o_, r_, n_)
            if st:
                pr_sources[k] = f"gh at build time ({pr_fetched_at})" if pr_fetched_at else "gh at build time"
                return st
        st, read = file_states.get(k, (None, None))
        if st:
            pr_sources[k] = f"{PRS_FILE}, read {read}" if read else PRS_FILE
        return st
    pr_lookup = lookup if (file_states or gh_lookup) and full else None
    tpath, threads = ex.parse_threads(rd)
    _, decisions = ex.ledger_records(rd, "decisions.jsonl", "decision")
    _, challenges = ex.ledger_records(rd, "challenges.jsonl", "challenge")
    _, handoffs, _ = ex.handoff_records(rd, led)
    convs = captures(rd, threads)
    budget, _ = read_budget(rd.root, rd)
    threads_text = (rd.root / "threads.md").read_text(encoding="utf-8", errors="replace") if tpath else ""

    def lane_ok(r):
        ls = [l for l in (r.get("lanes") or [r.get("lane")]) if l]
        return lane is None or any(rd.lanes.matches(lane, l) for l in ls)

    # settled ids come from the complete ledgers: a line in another lane (or by the owner, in none)
    # that supersedes a proposal or resolves a challenge still settles it under --lane
    # relay-kit/lifecycle@1 (resolved by the export readers): only an effective transition settles anything;
    # a proposed replacement leaves its target open, and invalid transitions close nothing
    superseded = {d["fields"].get("id") for d in decisions if d.get("closed_by")}
    closed = {c["fields"].get("id") for c in challenges if c.get("closed_by")}
    decisions_all, challenges_all, handoffs_all = list(decisions), list(challenges), list(handoffs)
    decisions = [d for d in decisions if lane_ok(d)]
    challenges = [c for c in challenges if lane_ok(c)]
    handoffs = [h for h in handoffs if lane_ok(h)]
    convs = [c for c in convs if lane_ok(c)]

    # as of: the latest timestamp in what was read (never the clock)
    stamps = [ex.span(r.get("ts")) for r in decisions + challenges + handoffs + convs]
    stamps += [ex.span(c.get("last_message_at")) for c in convs]   # a capture runs until its last message
    stamps += [ex.span(e["at"]) for h in handoffs for e in h["log"]]
    if budget:
        stamps += [(t, t) for _, t, _ in budget["rows"]]
    stamps = sorted(s[0] for s in stamps if s)
    as_of_dt = stamps[-1] if stamps else None
    as_of = iso(as_of_dt) if as_of_dt else None


    # ---- Choices ----
    dec_by_id = {d["fields"].get("id"): d for d in decisions_all if isinstance(d["fields"].get("id"), str)}
    choices = []
    for d in decisions:
        f = d["fields"]
        if d.get("applicability") != "open":
            continue
        item = {"kind": "decision", "id": f.get("id"), "status": "proposed", "date": f.get("date"),
                "proposed_replacements": d.get("proposed_replacements") or [],
                "replaces": ({"id": f["supersedes"], "state": state_name(dec_by_id[f["supersedes"]])}
                             if isinstance(f.get("supersedes"), str) and f["supersedes"] in dec_by_id else None),
                "proposed_by": f.get("proposed_by") or f.get("recorded_by"), "decider": f.get("decider"),
                "source": f"{d['source_path']}:{d['line']}", "compose": compose_decision(d, owner, as_of, full, dec_by_id)}
        if full:
            item.update(text=f.get("decision"), scope=f.get("scope"), basis=f.get("basis"), locator=f.get("locator"))
        choices.append(item)
    def authorized(h):
        """The handoff's Authorized section holds something other than a TODO or NOT AUTHORIZED."""
        m = re.search(r"^## Authorized[ \t]*\n(.*?)(?=^## |\Z)", h["text"], re.S | re.M)
        auth = m.group(1) if m else ""
        return bool(auth.strip()) and not led.TODO_RE.search(auth) and "NOT AUTHORIZED" not in auth

    def run_class(h):
        """'authorized' (sent, acknowledged or in progress, with an Authorized section), 'pending'
        (prepared, or no authorization), 'blocked', or None for completed and cancelled."""
        st = h["status"]
        if st in led.FINAL:
            return None
        if st == "prepared" or not authorized(h):
            return "pending"
        return "authorized" if st in ("sent", "acknowledged", "in_progress") else "blocked"

    waiting_handoffs = []
    for h in handoffs:
        st = h["status"]
        if st in led.FINAL:
            continue
        unauth = not authorized(h)
        why = []
        if st == "prepared":
            why.append("prepared, not sent")
        if unauth:
            why.append("Authorized not filled: nobody may start it")
        if base_of(h["to"], owner) == "owner":
            why.append("addressed to the owner")
        if st == "blocked":
            why.append("blocked")
        if not why:
            continue
        item = {"kind": "handoff", "id": h["handoff_id"], "status": st, "date": h["created"], "from": h["from"],
                "to": h["to"], "why": why, "source": h["source_path"],
                "compose": compose_handoff(h, owner, unauth, next_state(h, led),
                                           (rd.root / "handoffs" / "outputs" / f"{h['handoff_id']}.md").is_file())}
        if full:
            item["title"] = h["title"]
        waiting_handoffs.append(item["id"])
        choices.append(item)
    questions = []
    for c in challenges:
        f = c["fields"]
        if c["status"] != "open" or f.get("id") in closed:
            continue
        if f.get("kind") == "question":   # a Clarify: waits on whoever it's addressed to, not on the owner
            q = {"id": f.get("id"), "date": f.get("date"), "about": f.get("about"), "addressed_to": f.get("addressed_to"),
                 "raised_by": f.get("raised_by"), "source": f"{c['source_path']}:{c['line']}"}
            if full:
                q["text"] = f.get("concern")
            questions.append(q)
            continue
        item = {"kind": "challenge", "id": f.get("id"), "status": "open", "date": f.get("date"),
                "raised_by": f.get("raised_by"), "source": f"{c['source_path']}:{c['line']}",
                "compose": compose_challenge(c, owner, full)}
        if full:
            item.update(text=f.get("concern"), affects=f.get("affects"), proposed_test=f.get("proposed_test"))
        choices.append(item)
    choices.sort(key=lambda i: (["decision", "handoff", "challenge"].index(i["kind"]), str(i["id"])))
    questions.sort(key=lambda q: str(q["id"]))

    # ---- what each choice is asking, what cites it, and whether it may be overtaken ----
    owner_hint = records_github_owner(rd.root)   # a local git read; no network
    texts = {}   # every record's searchable text, by (kind, id)
    for d in decisions_all:
        texts[("decision", d["fields"].get("id"))] = (d, record_text(d["fields"]))
    for c in challenges_all:
        texts[("challenge", c["fields"].get("id"))] = (c, record_text(c["fields"]))
    for h in handoffs_all:
        texts[("handoff", h["handoff_id"])] = (h, h.get("text") or "")
    pr_states = {}
    # Clarify questions and the lines that answer them talk about an item without settling it
    q_ids = {c["fields"].get("id") for c in challenges_all if c["fields"].get("kind") == "question"}
    answer_ids = {("challenge", c["fields"].get("id")) for c in challenges_all
                  if isinstance(c["fields"].get("resolves"), str) and c["fields"]["resolves"] in q_ids}
    question_ids = {("challenge", i) for i in q_ids}
    known_repos = set()
    if owner_hint:
        own_re = re.compile(rf"\b{re.escape(owner_hint)}/([\w.-]+)", re.I)
        for _, txt in list(texts.values()) + [(None, threads_text)]:
            known_repos |= {x.lower().rstrip(".").removesuffix(".git") for x in own_re.findall(txt)}
    for item in choices:
        cid, kind = item["id"], item["kind"]
        rec, own = texts.get((kind, cid), (None, ""))
        f = (rec or {}).get("fields") or {}
        body = f.get("decision") or f.get("concern") or (rec or {}).get("text") or ""
        canon = derived_ask(kind, cid, (rec or {}).get("title") if kind == "handoff" else body, full,
                            nxt=item["compose"]["accept"].get("state") if kind == "handoff" else None,
                            why=item.get("why", ()))
        # What each action does comes from this page, always: it describes the composed lines. The
        # proposer's `ask` can word the question and explain the options, shown apart as theirs, but
        # can't redefine what Accept does. In metadata mode their text is free text, so it's left out.
        item["effects"] = canon["options"]
        w = written_ask(f.get("ask")) if kind != "handoff" and full else None
        if w:
            ask = {"source": "proposer", "by": f.get("proposed_by") or f.get("raised_by") or f.get("recorded_by"),
                   "question": w["question"], "proposer_options": w["options"],
                   "proposer_default": w["default_if_unanswered"]}
        else:
            ask = {"source": "derived", "question": canon["question"],
                   "note": "the proposer's own question is left out (--content metadata)" if f.get("ask") and not full else None}
        ask["default_if_unanswered"] = canon["default_if_unanswered"]
        item["ask"] = ask
        item["refs"] = sorted({x for x in ID_REF.findall(own) if x != cid}) if full else []
        pat = re.compile(rf"\b{re.escape(str(cid))}\b")
        cited, waiting, strong = [], [], []
        day = str(item.get("date") or "")[:10]
        for (k2, i2), (r2, txt) in texts.items():
            m = pat.search(txt)
            if i2 == cid or not m or (k2, i2) in answer_ids:   # answering a question about it settles nothing about it
                continue
            if (k2 == "decision" and i2 in superseded) or (k2 == "challenge" and i2 in closed):
                continue   # settled since: neither waiting on it nor news about it
            st2 = r2.get("status") if k2 == "handoff" else r2["fields"].get("status")
            d2 = str((r2.get("created") if k2 == "handoff" else r2["fields"].get("date")) or "")
            ref = {"kind": k2, "id": i2, "status": st2, "date": d2}
            # later: within one ledger, a higher id; otherwise a later day (same day across kinds is unordered)
            if k2 == kind and k2 != "handoff":
                later = ledger_num(i2) > ledger_num(cid)
            else:
                later = d2[:10] > day
            if (k2 == "handoff" and st2 not in led.FINAL) or (k2 == "decision" and st2 == "proposed") or \
               (k2 == "challenge" and st2 == "open"):
                waiting.append(ref)   # still open and citing it: waits on it (weak evidence of anything else)
                continue
            if not later:
                continue
            cited.append(ref)
            # strong: a settled later record that, near any mention of the id, says it closed, replaced, amended
            # or carried it out (not a question about it: those are open and listed as waiting above)
            phrase = None
            if (k2, i2) not in question_ids:
                for mm in pat.finditer(txt):
                    phrase = closure_phrase(txt, mm.start(), mm.end())
                    if phrase:
                        break
            if phrase:
                strong.append(dict(ref, phrase=phrase))
        item["waiting_on_it"] = sorted(waiting, key=lambda r: str(r["id"]))
        item["cited_later_by"] = sorted(cited, key=lambda r: str(r["id"]))
        prs = []
        if full:   # references always; their state only with --pr-state (build time)
            named_in = " ".join([body, str(f.get("evidence") or ""), str(f.get("scope") or ""),
                                 json.dumps(f.get("ask"), ensure_ascii=False) if isinstance(f.get("ask"), dict) else ""])
            for o_, r_, n_ in pr_refs(named_in, owner_hint, known_repos):
                key = f"{o_}/{r_}#{n_}".lower()
                if pr_lookup and key not in pr_states:
                    pr_states[key] = pr_lookup(o_, r_, n_)
                prs.append({"pr": key, "state": pr_states.get(key) if pr_lookup else None,
                            "source": pr_sources.get(key) if pr_lookup else None})
        item["prs"] = prs
        sp = ex.span(item.get("date"))
        item["age_days"] = (as_of_dt - sp[0]).days if sp and as_of_dt else None
        reasons = [f"{r['kind']} {r['id']} ({r['status']}): “…{r['phrase']}…”" for r in sorted(strong, key=lambda r: str(r["id"]))]
        reasons += [f"{x['pr']} is {x['state']}" for x in prs if x["state"] in ("merged", "closed")]
        item["possibly_overtaken"] = reasons
        extra = []
        if reasons and kind != "handoff":
            extra.append({"label": "Close as overtaken", "effect": f"a line closes {cid} without adopting anything; you say what overtook it"})
        extra += [{"label": "Clarify", "effect": "an open question about it goes on the record, addressed to whoever proposed it; nothing is delivered"},
                  {"label": "Inquire", "effect": "a prompt is copied for you to ask an assistant; nothing is recorded"}]
        item["effects"] = item["effects"] + extra
        if kind != "handoff":
            proposer = f.get("proposed_by") or f.get("recorded_by") if kind == "decision" else f.get("raised_by")
            item["compose"].update(compose_extra(kind, cid, owner, proposer, full, body, f.get("scope"),
                                                 expects_for(rec, dec_by_id) if kind == "decision" else expect_of(rec, "open")))
        else:
            # a handoff's question goes to its sender, who wrote what it asks for
            item["compose"].update({k: v for k, v in compose_extra(kind, cid, owner, (rec or {}).get("from"), full, "", None).items()
                                    if k in ("clarify", "inquire")})

    # ---- decision support (DEC-140 §1): authored notes per item, one block per author ----
    coord_lanes = [coordinator.lower()] if coordinator else [l for l in (set(rd.lanes.codes) | {t["lane"] for t in threads.values() if t.get("lane")}) if "coord" in l]
    for item in choices:
        item["support"] = support_for(support, item["id"], owner, rd.lanes.lane, coord_lanes)

    # handoff titles in metadata mode: ids only (the title can carry the substance)
    def htitle(h):
        return h["title"] if full else None

    # ---- Topology: information ----
    nodes, edges = {}, []

    def node(nid, typ, label, ln):
        nodes.setdefault(nid, {"id": nid, "type": typ, "label": label, "lane": ln})

    node("owner", "owner", owner, None)
    lanes_all = set(rd.lanes.codes) | {t["lane"] for t in threads.values() if t.get("lane")}
    for h in handoffs:
        for a in (h["from"], h["to"]):
            b = base_of(a, owner)
            if b and b != "owner":
                ln = rd.lanes.lane(b)
                node(b, "actor", b, ln)
                if ln:
                    lanes_all.add(ln)
    for c in convs:
        if c.get("actor"):
            b = base_of(c["actor"], owner)
            if b and b != "owner":
                node(b, "actor", b, rd.lanes.lane(b))
    if lane:
        lanes_all = {l for l in lanes_all if rd.lanes.matches(lane, l)}
    for l in sorted(lanes_all):
        node(f"lane:{l}", "lane", l, l)
    thread_ids = {}
    for cid, t in sorted(threads.items()):
        if lane and not (t.get("lane") and rd.lanes.matches(lane, t["lane"])):
            continue
        nid = "thread:" + ex.sha(cid)[:10]
        thread_ids[cid] = nid
        node(nid, "thread", f"{t['platform']} …{cid[-4:]}", t.get("lane"))
        nodes[nid]["status"] = t.get("status")
    for c in convs:
        nid = "capture:" + ex.sha(c["rid"])[:10]
        lbl = (c.get("title") if full and c.get("title") else f"{c.get('platform') or 'capture'} …{str(c.get('conversation_id') or c['rid'])[-4:]}")
        node(nid, "capture", lbl, c.get("lane"))
        if c.get("conversation_id") in thread_ids:
            edges.append({"from": nid, "to": thread_ids[c["conversation_id"]], "type": "capture-of", "count": 1, "refs": []})
        if c.get("actor") and base_of(c["actor"], owner) in nodes:
            edges.append({"from": base_of(c["actor"], owner), "to": nid, "type": "captured", "count": 1, "refs": []})
    flows = {}
    for h in handoffs:
        a, b = base_of(h["from"], owner), base_of(h["to"], owner)
        if a and b:
            flows.setdefault((a, b), []).append(h["handoff_id"])
    for (a, b), ids in sorted(flows.items()):
        edges.append({"from": a, "to": b, "type": "handoff", "count": len(ids), "refs": sorted(ids)})
    # @6 (HO-CKPT-20261106-D1C5, DEC-141 run 1 items 9–10): per-actor handoff totals, the same numbers the
    # edges carry, so a reader never has to add them off the drawing; and which lanes have no thread.
    totals = {}
    for (a, b), ids in flows.items():
        for who in (a, b):
            totals.setdefault(who, {"actor": who, "lane": nodes.get(who, {}).get("lane") if who != "owner" else None,
                                    "sends": 0, "receives": 0, "self": 0, "to": {}, "from": {}})
        if a == b:
            totals[a]["self"] += len(ids)
        totals[a]["sends"] += len(ids)
        totals[b]["receives"] += len(ids)
        totals[a]["to"][b] = totals[a]["to"].get(b, 0) + len(ids)
        totals[b]["from"][a] = totals[b]["from"].get(a, 0) + len(ids)
    actor_rows = sorted(totals.values(), key=lambda t: (-t["sends"], -t["receives"], t["actor"]))
    for t in actor_rows:
        t["to"] = dict(sorted(t["to"].items(), key=lambda kv: (-kv[1], kv[0])))
        t["from"] = dict(sorted(t["from"].items(), key=lambda kv: (-kv[1], kv[0])))
    lanes_with_thread = {t["lane"] for t in threads.values() if t.get("lane")}
    for n in nodes.values():
        if n["type"] == "lane":
            n["has_thread"] = n["label"] in lanes_with_thread
    info = {"nodes": sorted(nodes.values(), key=lambda n: (n["type"], n["id"])),
            "edges": sorted(edges, key=lambda e: (e["type"], e["from"], e["to"])),
            "actors": actor_rows,
            "lanes_without_thread": sorted(l for l in lanes_all if l not in lanes_with_thread)}

    # ---- Topology: authority ----
    lane_names = sorted(l for l in lanes_all)
    coords = [coordinator.lower()] if coordinator else [l for l in lane_names if "coord" in l]
    coords = [c for c in coords if c in lane_names] or []
    lane_nodes = [{"id": f"lane:{l}", "type": "coordinator" if l in coords else "lane", "label": l} for l in lane_names]

    def lane_targets(text):
        return sorted({l for l in lane_names if re.search(rf"(?<![\w-]){re.escape(l)}(?![\w-])", text, re.I)}
                      | {l for l in lane_names if rd.lanes.codes.get(l) and re.search(rf"\b{re.escape(rd.lanes.codes[l])}\b", text)})

    def source_of(decider):
        if base_of(decider, owner) == "owner" or str(decider or "").strip().lower() in (owner, "owner"):
            return "owner"
        if coords and re.search(r"coordinat", str(decider or ""), re.I):
            return f"lane:{coords[0]}"
        return "decider:" + str(decider or "(none)")

    # recorded: an adopted, in-force decision that names its delegate in a delegates_to field
    rec_edges = []
    for d in decisions:
        f = d["fields"]
        dt_ = f.get("delegates_to")
        if not d.get("in_force") or not dt_:
            continue
        for target in ([dt_] if isinstance(dt_, str) else [x for x in dt_ if isinstance(x, str)]):
            tos = lane_targets(target) or [None]
            for l in tos:
                rec_edges.append({"from": source_of(f.get("decider")), "to": f"lane:{l}" if l else "target:" + target,
                                  "kind": "recorded", "decision": f.get("id"), "date": f.get("date"), "inferred": False,
                                  "delegates_to": target, "scope": f.get("scope") if full else None})
    rec_ids = {e["from"] for e in rec_edges} | {e["to"] for e in rec_edges}
    rec_nodes = [{"id": "owner", "type": "owner", "label": owner}] + [n for n in lane_nodes if n["id"] in rec_ids] + \
        [{"id": x, "type": "lane", "label": x.split(":", 1)[1]} for x in sorted(rec_ids) if x.startswith(("target:", "decider:"))]

    # hypothesised (an overlay, off by default): assumed reporting lines and text-matched delegations
    hyp_edges = []
    for c in coords:
        hyp_edges.append({"from": "owner", "to": f"lane:{c}", "kind": "structure", "decision": None, "inferred": True,
                          "basis": "assumed reporting line: the owner directs the coordinator"})
    for l in lane_names:
        if l in coords:
            continue
        hyp_edges.append({"from": f"lane:{coords[0]}" if coords else "owner", "to": f"lane:{l}", "kind": "structure",
                          "decision": None, "inferred": True,
                          "basis": "assumed reporting line" + (" via the coordinator" if coords else "")})
    delegations = []
    for d in decisions:
        f = d["fields"]
        if not d.get("in_force") or not DELEGATION.search(str(f.get("decision", ""))):
            continue
        frm = source_of(f.get("decider"))
        frm = "owner" if frm.startswith("decider:") else frm
        tos = lane_targets(str(f.get("scope") or "")) or sorted(d.get("lanes") or [])
        for l in tos or [None]:
            to = f"lane:{l}" if l else "unspecified"
            if to == frm:
                continue
            e = {"from": frm, "to": to, "kind": "delegation", "decision": f.get("id"), "date": f.get("date"),
                 "inferred": True, "basis": "decision text mentions delegating or authorizing; target read from its scope"}
            hyp_edges.append(e)
            delegations.append(e)
    hyp_nodes = [{"id": "owner", "type": "owner", "label": owner}] + lane_nodes + \
        ([{"id": "unspecified", "type": "lane", "label": "(scope names no lane)"}] if any(e["to"] == "unspecified" for e in hyp_edges) else [])
    srt = lambda es: sorted(es, key=lambda e: (e["kind"], e["from"], e["to"], str(e["decision"])))
    authority = {"recorded": {"nodes": rec_nodes if rec_edges else [], "edges": srt(rec_edges)},
                 "hypothesised": {"nodes": hyp_nodes, "edges": srt(hyp_edges)},
                 "coordinator": coords, "executable": False,
                 "note": "Nothing executable is derived from either graph; the decisions are the authority."}

    # ---- Budget ----
    b = {"present": budget is not None, "file": BUDGET_FILE, "schema": BUDGET_FIELDS,
         "weekly_allowance": weekly_tokens, "status_file_costs_parsed": False}
    if budget is not None:
        start = (as_of_dt - dt.timedelta(days=7)) if as_of_dt else None
        # relay-kit/budget@2: a corrected line is replaced (and counted at the time of the line it replaces);
        # a child is dropped only where its parent is counted in the same window
        recent = [(n, budget["counted"][n] or t, r) for n, t, r in budget["rows"]
                  if n in budget["counted"] and (start is None or (budget["counted"][n] or t) > start)]
        in_window = {budget["ids"][n] for n, _, _ in recent}
        kids = budget["corrections"]["children"]
        recent = [(n, t, r) for n, t, r in recent if not (budget["ids"][n] in kids and kids[budget["ids"][n]] in in_window)]
        sess = [r for _, _, r in recent if r["window"] == "session"]
        weekly = sorted([(t, n, r) for n, t, r in recent if r["window"] == "weekly"], key=lambda x: (x[0], x[1]))
        by_actor, by_lane = {}, {}
        for r in sess:
            tot = r["tokens_in"] + r["tokens_out"]
            a = str(r.get("actor") or "(none)")
            by_actor[a] = by_actor.get(a, 0) + tot
            bb = base_of(a, owner)
            ln = rd.lanes.lane(bb) if bb and bb != "owner" else None
            by_lane[ln or "(no lane)"] = by_lane.get(ln or "(no lane)", 0) + tot
        sess_total = sum(r["tokens_in"] + r["tokens_out"] for r in sess)
        latest_weekly = weekly[-1][2] if weekly else None
        burn = sess_total if sess else None   # no session lines: no observation, never a manufactured 0
        b.update({
            "records": len(budget["rows"]), "invalid_lines": budget["bad"],
            "window": {"from": iso(start) if start else None, "to": as_of},
            "session_tokens_7d": sess_total,
            "session_in_7d": sum(r["tokens_in"] for r in sess), "session_out_7d": sum(r["tokens_out"] for r in sess),
            "session_lines_7d": len(sess),
            "session_tokens_7d_by_method": {m: sum(r["tokens_in"] + r["tokens_out"] for r in sess
                                                   if (r.get("method") if r.get("method") in BG.METHODS else "unlabelled") == m)
                                            for m in sorted({(r.get("method") if r.get("method") in BG.METHODS else "unlabelled") for r in sess})},
            "corrections": {"budget_schema": BG.SCHEMA, "replaced": budget["corrections"]["replaced"],
                            "children_included": budget["corrections"]["children"],
                            "conflicts": budget["corrections"]["conflicts"],
                            "invalid": budget["corrections"]["invalid"],
                            "duplicate_ids": budget["corrections"]["duplicates"]},
            "burn_label": "+".join(sorted({(r.get("method") if r.get("method") in BG.METHODS else "unlabelled") for r in sess})) or None,
            "weekly_reference": ({"ts": latest_weekly["ts"], "tokens": latest_weekly["tokens_in"] + latest_weekly["tokens_out"],
                                  "actor": latest_weekly.get("actor"),
                                  "note": "a separately reported weekly reading; reference only, never added to or substituted for the session sum"}
                                 if latest_weekly else None),
            "burn_7d": burn, "burn_source": f"sum of {len(sess)} session line(s) in the window" if sess else "no session lines in the window",
            "by_actor": dict(sorted(by_actor.items())), "by_lane": dict(sorted(by_lane.items())),
            "max_agents_7d": max((r.get("agents") for _, _, r in recent if isinstance(r.get("agents"), int)), default=None),
            "fraction": (burn / weekly_tokens) if (weekly_tokens and burn is not None) else None,
        })
        if full:
            b["recent_notes"] = [{"ts": r["ts"], "actor": r.get("actor"), "note": r.get("note")}
                                 for _, _, r in sorted(recent, key=lambda x: (x[1], x[0]))[-10:] if r.get("note")]

    # ---- Run / Drift ----
    by_status, commitments = {}, {"authorized": [], "pending": [], "blocked": []}
    for h in sorted(handoffs, key=lambda h: (h["created"] or "", h["handoff_id"])):
        last = h["log"][-1] if h["log"] else {}
        row = {"id": h["handoff_id"], "title": htitle(h), "from": h["from"], "to": h["to"], "created": h["created"],
               "status": h["status"], "last": {"at": last.get("at"), "state": last.get("state"), "actor": last.get("actor")}}
        by_status.setdefault(h["status"], []).append(row)
        if run_class(h):
            commitments[run_class(h)].append(row)
    by_status = {s: by_status[s] for s in sorted(by_status, key=lambda s: (STATE_ORDER.index(s) if s in STATE_ORDER else 99, s))}

    def dec_row(d):
        f = d["fields"]
        r = {"id": f.get("id"), "date": f.get("date"), "status": d["status"], "supersedes": f.get("supersedes"),
             "decider": f.get("decider"), "in_force": d.get("in_force")}
        if full:
            r["text"] = f.get("decision")
        return r

    def ch_row(c):
        f = c["fields"]
        r = {"id": f.get("id"), "date": f.get("date"), "status": c["status"], "raised_by": f.get("raised_by"),
             "resolves": f.get("resolves"), "open": c["status"] == "open" and f.get("id") not in closed}
        if full:
            r["text"] = f.get("concern")
        return r

    recent_dec = [dec_row(d) for d in sorted(decisions, key=lambda d: d["line"])[-20:]][::-1]
    open_ch = [ch_row(c) for c in challenges if c["status"] == "open" and c["fields"].get("id") not in closed]
    drift_ch = [ch_row(c) for c in challenges if "drift" in dumps(c["fields"]).lower()]
    stale = []
    for d in decisions:
        f = d["fields"]
        sp = ex.span(f.get("date"))
        if d["status"] == "proposed" and f.get("id") not in superseded and sp and as_of_dt and \
                (as_of_dt - sp[0]).days > drift_days:
            stale.append({**dec_row(d), "age_days": (as_of_dt - sp[0]).days})
    run = {"handoffs_by_status": by_status,
           "handoff_counts": {s: len(v) for s, v in by_status.items()},
           "commitments": commitments,
           "pr_links": sorted(set(PR_LINK.findall(threads_text))),
           "decisions_recent": recent_dec, "challenges_open": open_ch,
           "drift": {"challenges_mentioning_drift": drift_ch, "stale_proposals": stale, "drift_days": drift_days}}

    # ---- Project lanes ----
    lane_rows = []
    for l in lane_names:
        def mine(r):
            return any(x == l for x in (r.get("lanes") or [r.get("lane")]))
        hs = [h for h in handoffs if mine(h)]
        ds = [d for d in decisions if mine(d)]
        cs = [c for c in challenges if mine(c) and c["status"] == "open" and c["fields"].get("id") not in closed]
        # activity: records' own times, handoff log steps, every challenge line (closing ones too), capture ends
        cv = [c for c in convs if mine(c)]
        acts = [ex.span(r.get("ts")) for r in hs + ds + cv + [c for c in challenges if mine(c)]]
        acts += [ex.span(e["at"]) for h in hs for e in h["log"]] + [ex.span(c.get("last_message_at")) for c in cv]
        ts = sorted(s[0] for s in acts if s)
        lane_rows.append({"lane": l, "code": rd.lanes.codes.get(l), "coordinator": l in coords,
                          "handoffs": len(hs), "open_handoffs": sum(1 for h in hs if h["status"] not in led.FINAL),
                          "decisions": len(ds), "proposed": sum(1 for d in ds if d["status"] == "proposed"
                                                                and d["fields"].get("id") not in superseded),
                          "open_challenges": len(cs),
                          "threads": sum(1 for t in threads.values() if t.get("lane") == l),
                          "last_activity": iso(ts[-1]) if ts else None})
    intake = sorted({u.rstrip(".,;:") for line in threads_text.splitlines() if re.search(r"intake", line, re.I)
                     for u in ex.URL.findall(line)})
    policy = settings.get("policy") if isinstance(settings.get("policy"), dict) else {}
    b["policy"] = {"advisory": True, "source": SETTINGS_FILE if policy else None,
                   "caps": {k: policy.get(k) for k in POLICY_KEYS}}

    # ---- Status (DEC-139): the newest edition; stale when older than drift_days before the newest record ----
    for ed in status["editions"]:
        sp = ex.span(ed["date"])
        ed["age_days"] = (as_of_dt - sp[0]).days if sp and as_of_dt else None
        ed["stale"] = ed["age_days"] is not None and ed["age_days"] > drift_days
    latest_ed = status["editions"][0] if status["editions"] else None
    status_out = {"present": status["present"], "dir": status["dir"], "contract": "relay-kit/status@1", "latest": latest_ed,
                  "older": [{k: v for k, v in ed.items() if k != "body"} for ed in status["editions"][1:]],
                  "unreadable": status["unreadable"], "drift_days": drift_days}

    # ---- Actions (DEC-151): open items age from their stated "since" date to the newest record ----
    for fl in action_lists["files"]:
        for it in fl["items"]:
            try:
                sp = ex.span(it["since"]) if it["since"] else None
            except (ValueError, OverflowError):
                sp = None
            it["age_days"] = (as_of_dt.date() - sp[0].date()).days if sp and as_of_dt else None   # whole dates
            try:
                sd = ex.span(it["due"]) if it["due"] else None
            except (ValueError, OverflowError):
                sd = None
            it["days_to_due"] = (sd[0].date() - as_of_dt.date()).days if sd and as_of_dt else None
    action_items = [it for fl in action_lists["files"] for it in fl["items"]]
    actions_out = {"present": action_lists["present"], "source": action_lists["source"], "contract": "relay-kit/actions@1",
                   "files": action_lists["files"], "unreadable": action_lists["unreadable"],
                   "open": sum(1 for it in action_items if not it["done"]), "done": sum(1 for it in action_items if it["done"])}

    counts = {"choices": len(choices),"choices_by_kind": {k: sum(1 for c in choices if c["kind"] == k)
                                                           for k in ("decision", "handoff", "challenge")},
              "decisions": len(decisions), "challenges": len(challenges), "handoffs": len(handoffs),
              "captures": len(convs), "threads": len(thread_ids),
              "open_handoffs": sum(1 for h in handoffs if h["status"] not in led.FINAL),
              "authorized_in_progress": sum(1 for h in handoffs if run_class(h) == "authorized"),
              "pending_authorization": sum(1 for h in handoffs if run_class(h) == "pending"),
              "drift_flags": len([c for c in drift_ch if c["open"]]) + len(stale),
              "delegations_inferred": len(delegations), "delegations_recorded": len(rec_edges),
              "questions_open": len(questions), "possibly_overtaken": sum(1 for c in choices if c["possibly_overtaken"]),
              "prs_open": sum(1 for (r, n), x in (ps or {"latest": {}})["latest"].items() if x.get("state") == "open"),
              "status_editions": len(status["editions"]),
              "support_notes": sum(len(v) for v in support["by_item"].values()),
              "choices_without_support": sum(1 for c in choices if not c["support"]),
              "actions_open": actions_out["open"], "actions_done": actions_out["done"]}
    return {
        "schema": SCHEMA,
        "generator": {"tool": "relay-kit cockpit (tools/cockpit.py)", "kit_version": __version__,
                      "reads_with": "tools/export.py (relay-kit/export@1 readers)"},
        "as_of": as_of, "owner": owner,
        "query": {"lane": lane, "content": content, "weekly_tokens": weekly_tokens, "drift_days": drift_days,
                  "coordinator": coords, "brand": brand, "landing": landing},
        "counts": counts,
        "choices": choices,
        "questions": questions,
        "pr_state": ({"looked_up": True, "states": pr_states, "sources": pr_sources,
                      "fetched_at": pr_fetched_at or max((f["last_ok"] for f in ps["fetches"].values() if f["last_ok"]), default=None) if ps else pr_fetched_at,
                      "via": " and ".join(v for v in ["gh api (build time), first" if gh_lookup else None,
                                                      PRS_FILE + " (relay-kit prs)" if file_states else None] if v)}
                     if pr_lookup else {"looked_up": False}),
        "status": status_out,
        "actions": actions_out,
        "decision_support": {"present": support["present"], "file": SUPPORT_FILE, "contract": "relay-kit/decision-support@1",
                             "lines": support["lines"], "unreadable": support["unreadable"],
                             "order": "independent authors first, then the owner, then the coordinator lane; a reading order, not a ranking"},
        "pull_requests": ({"present": True, "file": PRS_FILE, "items": pr_items(ps, owner, settings, full, actors),
                           "fetches": sorted(ps["fetches"].values(), key=lambda f: f["repo"]), "unreadable": ps["unreadable"]}
                          if ps is not None else {"present": False, "file": PRS_FILE, "items": [], "fetches": [], "unreadable": 0}),
        "topology": {"information": info, "authority": authority},
        "budget": b,
        "run": run,
        "lanes": lane_rows,
        "intake_links": intake,
        "unreadable": sorted(rd.unreadable, key=lambda x: str(x["path"])),
        "limits": LIMITS,
        "snapshot": snapshot_of(rd.root, as_of),
        "telemetry": TEL.derive(rd.root, lane=lane, content=content, as_of=as_of),
        "transitions": {"lifecycle": "relay-kit/lifecycle@1",
                        "problems": sorted(({"id": r["fields"].get("id"), "message": m} for r in decisions_all + challenges_all
                                            for m in r.get("transition_problems") or []), key=lambda x: str(x["id"])),
                        "conflicts": sorted(({"id": r["fields"].get("id"), "closed_by": r.get("closed_by")}
                                             for r in decisions_all + challenges_all if r.get("conflict")), key=lambda x: str(x["id"]))},
    }


# ---------------- rendering ----------------
def e(x):
    return html.escape("" if x is None else str(x), quote=True)


def trunc(s, n):
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def fmt_n(n):
    return "—" if n is None else f"{n:,}"


def gauge(value, maximum, label, readout, sub="", tone="ok"):
    """A semicircular dial: needle at value/maximum (clamped); ticks every tenth."""
    frac = None if not maximum else max(0.0, min(1.0, value / maximum))
    cx, cy, r = 70, 70, 54
    import math
    ticks = []
    for i in range(11):
        a = math.pi * (1 - i / 10)
        r1 = r - (8 if i % 5 == 0 else 4)
        ticks.append(f'<line x1="{cx + r * math.cos(a):.1f}" y1="{cy - r * math.sin(a):.1f}" '
                     f'x2="{cx + r1 * math.cos(a):.1f}" y2="{cy - r1 * math.sin(a):.1f}"/>')
    arc = ""
    if frac:
        a = math.pi * (1 - frac)
        arc = (f'<path class="g-fill {tone}" d="M {cx - r} {cy} A {r} {r} 0 0 1 '
               f'{cx + r * math.cos(a):.1f} {cy - r * math.sin(a):.1f}"/>')
    a = math.pi * (1 - (frac or 0))
    needle = (f'<line class="g-needle" x1="{cx}" y1="{cy}" x2="{cx + (r - 12) * math.cos(a):.1f}" '
              f'y2="{cy - (r - 12) * math.sin(a):.1f}"/>') if frac is not None else ""
    return (f'<figure class="gauge"><svg viewBox="0 0 140 84" role="img" aria-label="{e(label)}: {e(readout)}">'
            f'<path class="g-track" d="M {cx - r} {cy} A {r} {r} 0 0 1 {cx + r} {cy}"/>{arc}'
            f'<g class="g-ticks">{"".join(ticks)}</g>{needle}<circle class="g-hub" cx="{cx}" cy="{cy}" r="4"/></svg>'
            f'<figcaption><b class="{tone}">{e(readout)}</b><span>{e(label)}</span>'
            f'{f"<small>{e(sub)}</small>" if sub else ""}</figcaption></figure>')


def info_svg(info):
    nodes = info["nodes"]
    bands = ["(owner)"] + sorted({n["lane"] for n in nodes if n["type"] != "owner" and n["lane"]}) + ["(no lane)"]
    rows = {b: [] for b in bands}
    for n in nodes:
        if n["type"] == "owner":
            rows["(owner)"].append(n)
        elif n["type"] == "lane":
            continue
        else:
            rows[n["lane"] if n["lane"] in rows else "(no lane)"].append(n)
    order = {"actor": 0, "thread": 1, "capture": 2}
    bands = [b for b in bands if rows[b] or b == "(owner)"]
    no_thread = set(info.get("lanes_without_thread") or [])
    # @6 layout (HO-CKPT-20261106-D1C5; DEC-141 run 1, items 7–11): taller bands so a self-loop fits above its
    # node; every handoff edge starts at the sender's rim with a dot and ends at the receiver's rim with a
    # large arrowhead; the count sits beside the arrowhead, on the outside of the curve, never mid-span;
    # opposite directions between two nodes bend to opposite sides (each bends left of its own travel).
    pos, BH, X0, DX, DXT, R = {}, 96, 170, 150, 110, 13
    COLS = min(5, max(2, max(sum(1 for n in rows[b] if n["type"] in ("actor", "owner")) for b in bands)), 5)
    COLS = 5 if len(bands) > 3 else COLS
    width = X0 + COLS * DX + DXT * max([sum(1 for n in rows[b] if n["type"] not in ("actor", "owner")) for b in bands] + [0]) + 40
    height = BH * len(bands) + 10
    parts = []
    for i, b in enumerate(bands):
        y = 10 + i * BH
        lbl = trunc(b, 20)
        tagged = b in no_thread and not b.startswith("(")
        parts.append(f'<rect class="band{" alt" if i % 2 else ""}" x="0" y="{y}" width="{width}" height="{BH}"/>'
                     f'<text class="band-label" x="10" y="{y + 26}">{e(lbl)}</text>'
                     + (f'<text class="band-tag" x="10" y="{y + 42}">no thread</text>' if tagged else ""))
        # actors step across COLS columns band by band (a staircase), so edges between bands leave and arrive
        # at distinct angles instead of sharing one vertical corridor; threads and captures follow to the right
        acts = [n for n in rows[b] if n["type"] in ("actor", "owner")]
        rest = sorted((n for n in rows[b] if n["type"] not in ("actor", "owner")), key=lambda n: (order.get(n["type"], 9), n["id"]))
        for j, n in enumerate(sorted(acts, key=lambda n: n["id"]) + rest):
            if n["type"] in ("actor", "owner"):
                x = X0 + ((2 * i + j) % COLS) * DX + 30
            else:
                x = X0 + COLS * DX + (j - len(acts)) * DXT + 30
            cy = y + 40
            pos[n["id"]] = (x, cy)
            t = n["type"]
            if t in ("actor", "owner"):
                shape = f'<circle class="n-{t}" cx="{x}" cy="{cy}" r="{R}"/>'
            elif t == "thread":
                shape = f'<rect class="n-thread" x="{x - 9}" y="{cy - 9}" width="18" height="18"/>'
            else:
                shape = f'<path class="n-capture" d="M {x} {cy - 12} L {x + 12} {cy} L {x} {cy + 12} L {x - 12} {cy} Z"/>'
            parts.append(f'<g><title>{e(n["type"])}: {e(n["label"])}{" (" + e(n["status"]) + ")" if n.get("status") else ""}</title>{shape}'
                         f'<text class="n-label" x="{x}" y="{cy + 30}">{e(trunc(n["label"], 20))}</text></g>')
    lines = []
    pair_seen = {}
    for ed in info["edges"]:
        if ed["from"] not in pos or ed["to"] not in pos:
            continue
        (x1, y1), (x2, y2) = pos[ed["from"]], pos[ed["to"]]
        cls = "e-" + ed["type"]
        handoff = ed["type"] == "handoff"
        w = 1.4 + min(ed["count"], 8) * 0.5 if handoff else 1
        tip = f'{ed["type"]}: {ed["from"]} → {ed["to"]}' + (f' ({ed["count"]}: {", ".join(ed["refs"][:8])}{"…" if len(ed["refs"]) > 8 else ""})' if ed["refs"] else "")
        if (x1, y1) == (x2, y2):
            # self-loop: a lobe above the node, rim to rim, the count at its apex
            sx, sy, ex_, ey = x1 - 9, y1 - 9, x1 + 9, y1 - 9
            d = f"M {sx} {sy} C {x1 - 40} {y1 - 50}, {x1 + 40} {y1 - 50}, {ex_} {ey}"
            lx, ly, tail = x1, y1 - 36, (sx, sy)
        else:
            key = tuple(sorted((ed["from"], ed["to"])))
            idx = pair_seen.get(key, 0)
            pair_seen[key] = idx + 1
            dx, dy = x2 - x1, y2 - y1
            L = (dx * dx + dy * dy) ** 0.5 or 1.0
            nx, ny = dy / L, -dx / L               # unit normal, left of travel (screen coordinates)
            bend = (64 if y1 == y2 else 30) + idx * 14
            cx_, cy_ = (x1 + x2) / 2 + nx * bend, (y1 + y2) / 2 + ny * bend
            # start and end on the rims, so the dot and the arrowhead sit outside the shapes
            def rim(px, py, qx, qy, r):
                vx, vy = qx - px, qy - py
                vl = (vx * vx + vy * vy) ** 0.5 or 1.0
                return px + vx / vl * r, py + vy / vl * r
            sx, sy = rim(x1, y1, cx_, cy_, R + 1)
            ex_, ey = rim(x2, y2, cx_, cy_, R + 1)
            d = f"M {sx:.1f} {sy:.1f} Q {cx_:.1f} {cy_:.1f} {ex_:.1f} {ey:.1f}"
            t = 0.82                                 # beside the arrowhead, on the outside of the curve
            px_ = (1 - t) ** 2 * sx + 2 * (1 - t) * t * cx_ + t * t * ex_
            py_ = (1 - t) ** 2 * sy + 2 * (1 - t) * t * cy_ + t * t * ey
            lx, ly, tail = px_ + nx * 13, py_ + ny * 13, (sx, sy)
        g = f'<g><title>{e(tip)}</title><path class="{cls}" d="{d}" stroke-width="{w:.1f}" marker-end="url(#arrow-{ed["type"]})"/>'
        if handoff:
            g += (f'<circle class="e-tail" cx="{tail[0]:.1f}" cy="{tail[1]:.1f}" r="3.5"/>'
                  f'<rect class="e-count-bg" x="{lx - 6 - 3.5 * len(str(ed["count"])):.1f}" y="{ly - 9:.1f}" '
                  f'width="{12 + 7 * len(str(ed["count"]))}" height="16" rx="8"/>'
                  f'<text class="e-count" x="{lx:.1f}" y="{ly + 3.5:.1f}">{ed["count"]}</text>')
        lines.append(g + "</g>")
    defs = "".join(f'<marker id="arrow-{t}" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="{s}" markerHeight="{s}" '
                   f'markerUnits="userSpaceOnUse" orient="auto">'
                   f'<path class="m-{t}" d="M 0 0 L 10 5 L 0 10 z"/></marker>' for t, s in (("handoff", 15), ("captured", 9), ("capture-of", 9)))
    return (f'<svg class="topo" viewBox="0 0 {width:.0f} {height}" width="{width:.0f}" height="{height}" role="img" '
            f'aria-label="Information topology"><defs>{defs}</defs>{"".join(parts)}{"".join(lines)}</svg>')


def actor_table(info):
    """Per-actor handoff totals beside the information graph (@6): the edge counts summed per sender and
    receiver, with the busiest counterpart named, so no reader has to add numbers off the drawing."""
    rows = info.get("actors") or []
    if not rows:
        out = '<p class="empty">No handoffs between actors yet.</p>'
        missing = sorted(info.get("lanes_without_thread") or [])
        if missing:
            out += f'<p class="hint">Lanes with no thread: {e(", ".join(missing))}.</p>'
        return out
    no_thread = set(info.get("lanes_without_thread") or [])

    def top(m):
        items = list(m.items())
        return f"{items[0][0]} ({items[0][1]})" + (f" +{len(items) - 1} more" if len(items) > 1 else "") if items else ""

    th = "".join(f"<th>{e(h)}</th>" for h in ("actor", "lane", "sent", "received", "sends most to", "receives most from"))
    tr = ""
    for t in rows:
        lane = t.get("lane")
        lane_cell = e(lane) + (' <span class="tag warn">no thread</span>' if lane in no_thread else "") if lane else "—"
        tr += (f'<tr><td data-h="actor">{e(t["actor"])}</td><td data-h="lane">{lane_cell}</td>'
               f'<td data-h="sent" class="n">{t["sends"]}{f" ({t["self"]} to itself)" if t["self"] else ""}</td>'
               f'<td data-h="received" class="n">{t["receives"]}</td>'
               f'<td data-h="sends most to">{e(top(t["to"])) or "—"}</td><td data-h="receives most from">{e(top(t["from"])) or "—"}</td></tr>')
    out = f'<div class="tw"><table><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table></div>'
    # DEC-141 run 2: "+N more" hid the rest of each actor's counterparts; the full matrix answers "to whom"
    senders = [t["actor"] for t in rows if t["sends"]]
    receivers = [t["actor"] for t in rows if t["receives"]]
    if senders and receivers:
        by = {t["actor"]: t["to"] for t in rows}
        mh = "".join(f'<th class="rot"><span>{e(r)}</span></th>' for r in receivers)
        mr = "".join(f'<tr><th>{e(a)}</th>' + "".join(f'<td class="n">{by[a].get(r) or ""}</td>' for r in receivers)
                     + f'<td class="n tot">{sum(by[a].values())}</td></tr>' for a in senders)
        tot = "".join(f'<td class="n tot">{sum(by[a].get(r, 0) for a in senders)}</td>' for r in receivers)
        out += (f'<details class="matrix"><summary>Every sender → receiver count · {len(senders)} senders × {len(receivers)} receivers</summary>'
                f'<div class="tw"><table class="mx"><thead><tr><th>sender ↓ · receiver →</th>{mh}<th class="rot"><span>sent</span></th></tr></thead>'
                f'<tbody>{mr}<tr><th>received</th>{tot}<td></td></tr></tbody></table></div></details>')
    unseen = sorted(l for l in no_thread if l not in {t.get("lane") for t in rows})
    if unseen:
        out += (f'<p class="hint">Lanes with no thread and no actor in any handoff, so no band above: '
                f'{e(", ".join(unseen))}.</p>')
    return out


def authority_svg(auth, pid="a"):
    nodes = auth["nodes"]
    owner = [n for n in nodes if n["type"] == "owner"]
    mid = [n for n in nodes if n["type"] == "coordinator"]
    low = [n for n in nodes if n["type"] == "lane"]
    DX = 128
    width = max(len(low), len(mid), 1) * DX + 40
    tiers = [(owner, 40), (mid, 140), (low, 250)] if mid else [(owner, 40), (low, 200)]
    height = tiers[-1][1] + 60
    pos, parts = {}, []
    for row, y in tiers:
        for i, n in enumerate(row):
            x = 20 + (width - 40) * (i + 0.5) / len(row)
            pos[n["id"]] = (x, y)
            cls = {"owner": "n-owner", "coordinator": "n-coord", "lane": "n-lane"}[n["type"]]
            parts.append(f'<g><title>{e(n["type"])}: {e(n["label"])}</title><rect class="{cls}" x="{x - 52}" y="{y - 15}" '
                         f'width="104" height="30" rx="4"/><text class="a-label" x="{x}" y="{y + 5}">{e(trunc(n["label"], 15))}</text></g>')
    groups = {}
    for ed in auth["edges"]:
        groups.setdefault((ed["from"], ed["to"], ed["kind"]), []).append(ed)
    lines = []
    for k, ((a, b, kind), eds) in enumerate(sorted(groups.items())):
        if a not in pos or b not in pos:
            continue
        (x1, y1), (x2, y2) = pos[a], pos[b]
        y1 += 15
        y2 -= 15
        if kind == "structure":
            d = f"M {x1:.1f} {y1} L {x2:.1f} {y2}"
            label = ""
        else:
            off = 36 + (k % 3) * 14
            d = f"M {x1:.1f} {y1} Q {(x1 + x2) / 2 + off:.1f} {(y1 + y2) / 2:.1f} {x2:.1f} {y2}"
            ids = [x["decision"] for x in eds]
            label = f'<text class="e-dec" x="{(x1 + 2 * ((x1 + x2) / 2 + off) + x2) / 4:.1f}" y="{(y1 + y2) / 2:.1f}">{e(trunc(", ".join(ids), 22))}</text>'
        ids_ = ", ".join(x["decision"] for x in eds if x["decision"])
        tip = f"{kind}: {a} → {b}" + {"delegation": f" ({ids_}; inferred from text)", "recorded": f" ({ids_}; delegates_to)"}.get(kind, " (assumed)")
        lines.append(f'<g><title>{e(tip)}</title><path class="a-{kind}" d="{d}" marker-end="url(#{pid}-arrow-{kind})"/>{label}</g>')
    defs = "".join(f'<marker id="{pid}-arrow-{t}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="9" markerHeight="9" markerUnits="userSpaceOnUse" orient="auto-start-reverse">'
                   f'<path class="m-{t}" d="M 0 0 L 10 5 L 0 10 z"/></marker>' for t in ("structure", "delegation", "recorded"))
    return (f'<svg class="topo" viewBox="0 0 {width:.0f} {height}" width="{width:.0f}" height="{height}" role="img" '
            f'aria-label="Authority and delegation topology"><defs>{defs}</defs>{"".join(lines)}{"".join(parts)}</svg>')


def dl(pairs):
    return "<dl>" + "".join(f"<dt>{e(k)}</dt><dd>{e(v)}</dd>" for k, v in pairs if v not in (None, "", [])) + "</dl>"


def enumerated_count(text):
    """How many items the record's own enumeration lists ((1)…(n) in order, or numbered lines); 0 when none.
    Shown near the title so a reader knows the count before the notes discuss it (DEC-141 run 1, finding 4)."""
    best = 0
    for ln in str(text or "").replace("\r\n", "\n").split("\n"):
        ms = [int(m.group(1)) for m in ENUM.finditer(ln)]
        if len(ms) >= 2 and ms == list(range(1, len(ms) + 1)):
            best = max(best, len(ms))
    numbered = [m.group(1) for m in re.finditer(r"^\s*(\d{1,3})[.)]\s+", str(text or ""), re.M)]
    if len(numbered) >= 2 and [int(x) for x in numbered] == list(range(1, len(numbered) + 1)):
        best = max(best, len(numbered))
    return best


def choice_card(c, pr_fetched_at=None):
    key = f'{c["kind"]}:{c["id"]}'
    n_items = enumerated_count(c.get("text")) if c["kind"] != "handoff" else 0
    items_tag = f'<span class="tag items">lists {n_items} items</span>' if n_items else ""
    head = (f'<header><span class="badge {c["kind"]}">{e(c["kind"])}</span><b>{e(c["id"])}</b>'
            f'<span class="st">{e(c["status"])}</span>{items_tag}<span class="date">{e(c.get("date"))}</span></header>')
    if c["kind"] == "decision":
        body = (f'<div class="txt rec">{structured(c.get("text"))}</div>' if c.get("text") else "") + dl(
            [("scope", c.get("scope")), ("proposed by", c.get("proposed_by")), ("decider", c.get("decider")),
             ("basis", c.get("basis")), ("locator", c.get("locator")), ("source", c["source"]),
             ("proposed replacement", (", ".join(c["proposed_replacements"]) + " (proposed, so this is still open)")
              if c.get("proposed_replacements") else None),
             ("proposes to replace", f'{c["replaces"]["id"]} ({c["replaces"]["state"]}); accepting it replaces that too'
              if c.get("replaces") else None)])
        hint = "Accept appends an adopted line that supersedes it; Reject a rejected one. You fill in decider, basis and locator."
    elif c["kind"] == "handoff":
        body = (f'<p class="txt">{e(c.get("title"))}</p>' if c.get("title") else "") + dl(
            [("from", c.get("from")), ("to", c.get("to")), ("needs the owner", "; ".join(c["why"])), ("source", c["source"])])
        nxt = c["compose"]["accept"]["state"]
        hint = (f'Accept moves it to {nxt}' if nxt else f'No legal next state from {c["status"]}') + \
            '; Reject cancels it (owner only). These are handoff.py commands; your words go in the note.'
    else:
        body = (f'<div class="txt rec">{structured(c.get("text"))}</div>' if c.get("text") else "") + dl(
            [("raised by", c.get("raised_by")), ("affects", c.get("affects")), ("source", c["source"])]) + \
            (f'<details class="rec-more"><summary>Proposed test</summary><div class="txt rec">{structured(c.get("proposed_test"))}</div></details>'
             if c.get("proposed_test") else "")
        hint = "Accept records accepted-risk; Reject records resolved (dismissed or fixed). You write the resolution."
    comp = c["compose"]
    ledger_kind = {"decision": "decision", "challenge": "challenge"}.get(c["kind"])

    def expect_attr(a):
        xs = re.findall(r" --expect ([\w=@.-]+)", str(comp.get(a, {}).get("command") or ""))
        return f' data-expect="{e(" ".join(xs))}"' if xs else ""

    def tmpl(a):
        x = comp[a]
        return (f' data-template="{e(dumps(x["json"]))}" data-kind="{ledger_kind}"{expect_attr(a)}' if x.get("json") and ledger_kind else "")
    btns = "".join(f'<button type="button" class="act {a}" data-key="{e(key)}" data-act="{a}" '
                   f'data-line="{e(comp[a]["command"])}"{tmpl(a)}>{a.title()}</button>' for a in ("accept", "reject", "defer"))
    if "overtaken" in comp and c.get("possibly_overtaken") and c["kind"] == "challenge":   # its resolution is your words
        btns += (f'<button type="button" class="act overtaken" data-key="{e(key)}" data-act="overtaken" '
                 f'data-line="{e(comp["overtaken"]["command"])}"{tmpl("overtaken")}>Close as overtaken</button>')
    # Your words, decider and locator (DEC-113 part 3): the page fills them into the line when you click.
    # The basis has no default (CH-020); decider and locator start from your saved defaults.
    words_label = {"decision": "Your words: the basis, quoted", "challenge": "Your words: the resolution",
                   "handoff": "Your words: the note"}[c["kind"]]
    who = ""
    if c["kind"] == "decision":
        who = ('<div class="who"><label>Decider<input type="text" class="in-decider" autocomplete="off"></label>'
               '<label>Locator<input type="text" class="in-locator" autocomplete="off"></label></div>')
    words = (f'<div class="words">{who}<label>{e(words_label)} <span class="req">required to settle; no default</span>'
             f'<textarea class="in-words" rows="2" placeholder="Write it yourself: the page never fills this in."></textarea></label>'
             f'<button type="button" class="secondary small same-last">Same as last</button>'
             f'<span class="words-note" role="status"></span></div>')
    edits = ""
    for a, label, what in (("overtaken", "Close as overtaken", "What overtook it (a merged PR, a later decision)? It goes into the closing line's text; your words above are the basis:"),
                           ("amend", "Amend", "Your text for the decision (it replaces the proposal's in the adopted line):"),
                           ("clarify", "Clarify", "Your question to " + (str(comp.get("clarify", {}).get("json", {}).get("addressed_to")) if comp.get("clarify") else "the proposer") + ":")):
        if a not in comp or (a == "overtaken" and not (c["kind"] == "decision" and c.get("possibly_overtaken"))):
            continue
        x = comp[a]
        cls = {"amend": "act amend", "overtaken": "act overtaken"}.get(a, "act clarify onrec")
        btns += f'<button type="button" class="{cls}" data-key="{e(key)}" data-open="{a}" aria-expanded="false">{label}</button>'
        pre = (c.get("text") or "") if a == "amend" else ""
        edits += (f'<div class="editor" data-for="{a}" hidden><label>{e(what)}<textarea rows="{4 if a == "amend" else 3}">{e(pre)}</textarea></label>'
                  f'<button type="button" class="primary small compose" data-key="{e(key)}" data-act="{a}" '
                  f'data-kind="{"challenge" if a == "clarify" else "decision"}" data-field="{e(x["edit_field"])}" '
                  f'data-template="{e(dumps(x["json"]))}"{expect_attr(a)}>Add to To apply</button></div>')
    off = ""
    if "inquire" in comp:
        off = (f'<div class="offrec"><button type="button" class="act inquire" data-key="{e(key)}" '
               f'data-prompt="{e(comp["inquire"]["prompt"])}">Inquire</button>'
               f'<span>off record: copies a question for an assistant; nothing is written, nobody is obliged</span></div>')
    onrec = ('<p class="onrec-note">Clarify is on the record: it composes an open question addressed to '
             f'{e(comp["clarify"]["json"]["addressed_to"])}, listed under Questions until a line resolves it. '
             'Recording it doesn\'t deliver it: tell them, or send a handoff, if you need the answer.</p>') if "clarify" in comp else ""
    return (f'<article class="card choice" data-key="{e(key)}">{head}{ask_block(c, pr_fetched_at, hint)}{body}'
            f'{words}<div class="btns">{btns}</div>{edits}{onrec}{off}</article>')


def support_block(c):
    """Decision support (DEC-140 §1): the authored notes for this item, one block per author, in reading
    order (independent authors before the coordinator). The page writes none of it and ranks nothing."""
    blocks = c.get("support") or []
    if not blocks:
        return ('<p class="mini-label">DECISION SUPPORT</p><p class="empty nosupport">No decision-support note yet. '
                'One is written by hand into governance/decision-support.jsonl; this page never generates one.</p>')
    out = ['<p class="mini-label">DECISION SUPPORT · AUTHORED NOTES, READ IN THIS ORDER; THE PAGE RANKS NOTHING</p>']
    # the authors' recommendations side by side, verbatim, so agreement or disagreement shows at a glance
    # (DEC-141 run 1, finding 5); the full notes follow
    glance = "".join(f'<div class="ds-glance {e(b["role"])}"><p class="mini-label">{e(b["author_base"])} · RECOMMENDATION</p>'
                     f'<div>{structured(b["current"]["fields"].get("recommendation")) if b["current"]["fields"].get("recommendation") else "<i class=\"empty\">none stated</i>"}</div></div>'
                     for b in blocks)
    out.append(f'<div class="ds-glances">{glance}</div>')
    for b in blocks:
        cur = b["current"]
        role = {"independent": "independent note", "coordinator": "coordinator's note", "owner": "owner's note"}[b["role"]]
        rows = "".join(f'<div class="ds-row"><dt>{e(label)}</dt><dd>{structured(cur["fields"].get(k))}</dd></div>'
                       for k, label in SUPPORT_FIELDS if cur["fields"].get(k))
        if not rows:
            rows = ('<p class="hint">fields left out (--content metadata); present: ' + e(", ".join(cur["present"])) + "</p>") if cur["present"] \
                else '<p class="hint">no fields filled in</p>'
        hist = ""
        if b["history"]:
            hist = ('<details class="ds-hist"><summary>' + f'{len(b["history"])} earlier revision(s)</summary>'
                    + "".join(f'<p class="hint">{e(h.get("id") or "")} · {e(h.get("at") or "?")}</p>'
                              + "".join(f'<div class="ds-row"><dt>{e(label)}</dt><dd>{structured(h["fields"].get(k))}</dd></div>'
                                        for k, label in SUPPORT_FIELDS if h["fields"].get(k)) for h in reversed(b["history"]))
                    + "</details>")
        out.append(f'<section class="ds {e(b["role"])}"><header><span class="tag ds-tag">{e(role)}</span><b>{e(b["author"])}</b>'
                   f'<span class="date">{e(cur.get("at") or "?")} · {e(cur.get("id") or "")}</span></header><dl class="ds-fields">{rows}</dl>{hist}</section>')
    return "".join(out)


def ask_block(c, pr_fetched_at=None, hint=""):
    """'So what is this asking?' above the raw record, then the proposer's own explanation of the options
    (when they wrote one), then the authored decision support (DEC-140 §1), then the facts (what waits on
    it, its age, whether it may be overtaken). What each action does to the ledger, this page's own
    description of the composed lines, is kept behind a disclosure: it is mechanics, not decision support.
    The proposer's options are substance, not mechanics, so they stay in the open (relay-kit #33, Kate's
    review of 2026-11-08): a reader sees every alternative and its claimed consequence without opening
    anything, and sees whose words they are."""
    a = c.get("ask") or {}
    whose = (f'question written by {e(a.get("by") or "the proposer")}' if a.get("source") == "proposer"
             else 'question derived by this page from the record, not written by the proposer')
    if a.get("note"):
        whose += f' ({e(a["note"])})'
    effects = "".join(f'<li><b>{e(o["label"])}</b> → {e(o["effect"])}</li>' for o in c.get("effects", []))
    theirs = ""
    if a.get("proposer_options") or a.get("proposer_default"):
        li = "".join(f'<li><b>{e(o["label"])}</b>: {e(o["effect"])}</li>' for o in a.get("proposer_options") or [])
        d_ = f'<li>If unanswered: {e(a["proposer_default"])}</li>' if a.get("proposer_default") else ""
        theirs = (f'<div class="proposer-options"><p class="mini-label theirs">THE PROPOSER\'S EXPLANATION OF THE OPTIONS · '
                  f'{e(a.get("by") or "")} · their words, not what the buttons do (that is under "how the ledger records this")</p>'
                  f'<ul class="effects theirs">{li}{d_}</ul></div>')
    dflt = f'<p class="dflt">If nobody answers: {e(a["default_if_unanswered"])}</p>' if a.get("default_if_unanswered") else ""
    refs = lambda rs: ", ".join(f'{r["kind"]} {r["id"]} ({r["status"]})' for r in rs)
    prs = ", ".join(f'{x["pr"]}' + (f' {x["state"]} (from {x["source"]})' if x["state"] and x.get("source")
                                    else f' {x["state"]}' if x["state"] else "") for x in c.get("prs") or [])
    facts = [("age", f'{c["age_days"]} d' if c.get("age_days") is not None else None),
             ("waiting on it", refs(c.get("waiting_on_it") or [])), ("cited later by", refs(c.get("cited_later_by") or [])),
             ("cites", ", ".join(c.get("refs") or [])), ("pull requests", prs)]
    over = ""
    if c.get("possibly_overtaken"):
        over = ('<p class="overtaken-flag"><span class="tag warn">POSSIBLY OVERTAKEN</span> '
                + e("; ".join(c["possibly_overtaken"])) + '. Check before settling it; "Close as overtaken" composes a '
                'line that closes it without adopting anything.</p>')
    mech = (f'<details class="mech"><summary>how the ledger records this</summary><p class="mini-label">WHAT EACH ACTION DOES (THIS PAGE)</p>'
            f'<ul class="effects">{effects}</ul>{dflt}<p class="hint">{e(hint)}</p></details>')
    return (f'<div class="ask"><p class="mini-label">SO WHAT IS THIS ASKING? · <span class="whose {e(a.get("source"))}">{whose}</span></p>'
            f'<p class="q">{e(a.get("question"))}</p>{theirs}{support_block(c)}{dl(facts)}{over}{mech}</div>')


CI_WORD = {"success": "CI passing", "failure": "CI failing", "pending": "CI running", "none": "no CI",
           "unknown": "CI not read (the reader couldn't get it)"}
VERDICT = {"APPROVED": "approved", "COMMENTED": "commented", "CHANGES_REQUESTED": "changes requested", "DISMISSED": "dismissed"}


def pr_card(x, full):
    st = "draft" if x["draft"] and x["state"] == "open" else (x["state"] or "?")
    ref = f'{x["repo"]}#{x["number"]}'
    link = f'<a class="subtle-link" href="{e(x["url"])}" rel="noreferrer noopener">{e(ref)} ↗</a>' if x["url"] else f"<b>{e(ref)}</b>"
    title = f'<p class="pr-title">{e(x["title"])}</p>' if full and x.get("title") else ""
    if x["written_by"] == "agent":
        by = f'<span class="tag agent">written by {e(x["author_line"]) if full else "an agent"}, not the owner</span>'
    elif x["written_by"] == "owner":
        by = '<span class="tag">written by the owner</span>'
    else:
        by = f'<span class="tag warn">no Author: line · opened by {e(x["opened_by"] or "?")}</span>'
    missing = (f'<span class="tag warn">sections missing: {e(", ".join(x["missing"]))}</span>' if x["missing"] else "")
    secs = ""
    if full and x["sections"]:
        cut = lambda k: " <i>(cut at 4000 characters)</i>" if k in x["truncated"] else ""
        first = next(k for k in PR_SECTIONS if k in x["sections"])
        rest = [k for k in PR_SECTIONS if k in x["sections"] and k != first]
        secs = (f'<dl class="pr-secs"><dt>{first.title()}</dt><dd class="txt">{e(x["sections"][first])}{cut(first)}</dd></dl>'
                + (f'<details class="pr-more"><summary>{" · ".join(k.title() for k in rest)}</summary><dl class="pr-secs">'
                   + "".join(f'<dt>{k.title()}</dt><dd class="txt">{e(x["sections"][k])}{cut(k)}</dd>' for k in rest)
                   + "</dl></details>" if rest else ""))
    elif not full:
        secs = f'<p class="hint">Sections present: {e(", ".join(x["present"]) or "none")} (text left out: --content metadata)</p>'
    revs = review_fold(x, full)
    other = ("not known (reviews not read)" if "reviews" in x.get("unread", []) else
             ("no reviews yet" if not x["reviews"] else "no counted reviews (only dismissed)") if not x["reviewers_counted"] else
             {True: "yes", False: "no", None: "can't tell (a family is unknown)"}[x["other_family"]])
    ci = CI_WORD.get(x["ci"]["state"], x["ci"]["state"]) + (f' ({", ".join(x["ci"]["failing"])})' if x["ci"]["failing"] else "")
    return (f'<article class="card pr pr-{e(st)}"><header><span class="badge pr-st">{e(st)}</span>{link}'
            f'<span class="date">updated {e((x["updated_at"] or "?")[:16].replace("T", " "))}</span></header>{title}'
            f'<div class="btns">{by}{missing}</div>{secs}{revs}'
            f'<dl><dt>Other family</dt><dd>{e(other)} · author family '
            f'{e(x["author_family"] or "unknown")}</dd><dt>CI</dt><dd>{e(ci)}</dd><dt>Read</dt><dd>{e(x["read_at"] or "?")}'
            f' · last changed {e(x["changed_at"] or "?")}</dd></dl></article>')


def family_label(r):
    """'copilot (inferred from login)', 'claude (signed)', or 'unsigned' (no Reviewer: line and no mapping)."""
    if r.get("family") and r.get("family_source") == "signed":
        return f'{r["family"]} (signed)'
    if r.get("family"):
        return f'{r["family"]} (inferred from login)'
    return "signed, family unknown" if r.get("signed") else "unsigned"


def review_fold(x, full):
    """The PR's reviews folded under the card (@5): one entry per review or review-style comment with author,
    family, kind, verdict, time and inline-comment count, and the body as plain paragraphs (no markdown is
    executed; URLs stay text). A Findings line appears only when the body states those counts."""
    unread = x.get("unread", [])
    if "reviews" in unread and not x["reviews"]:
        return '<dl><dt>Reviews</dt><dd>not read (the reader couldn\'t get them)</dd></dl>'
    if not x["reviews"]:
        return '<dl><dt>Reviews</dt><dd>none yet</dd></dl>'
    def one(r):
        # two verdicts can exist per review: GitHub's state and one the body states; label both so they never read
        # as a contradiction (DEC-141 run 1, finding 1)
        gh = f'GitHub: {VERDICT.get(r["verdict"], str(r["verdict"]).lower())}' if r["verdict"] else "GitHub: comment, no review state"
        stated = f'stated: {e(r["stated"])}' if r.get("stated") else "stated: none"
        fam = family_label(r)
        fam_html = '<span class="tag warn unsigned">unsigned</span>' if fam == "unsigned" else e(fam)
        meta = [e(r["who"]), fam_html, "review" if r["kind"] == "review" else "review-style comment",
                e(gh) + " · " + stated, e((r.get("at") or "?")[:16].replace("T", " "))]
        if r.get("comments"):
            meta.append(f'{r["comments"]} inline comment{"s" if r["comments"] != 1 else ""} (on GitHub)')
        f = r.get("findings") or {}
        find = (f'<p class="findings">Findings, as the review states them: '
                + " / ".join(x_ for x_ in [f'{f["blocking"]} blocking' if "blocking" in f else None,
                                          f'{f["should_fix"]} should-fix' if "should_fix" in f else None] if x_) + "</p>") if f else ""
        if not full:
            body = '<p class="hint">body left out (--content metadata)</p>'
        elif not r.get("body_recorded"):
            body = '<p class="hint">body not recorded: this line was read by relay-kit prs@2 or earlier; rerun prs to record it</p>'
        elif not (r.get("body") or "").strip():
            body = '<p class="hint empty">empty review body</p>'
        else:
            body = f'<div class="rv-body">{structured(r["body"])}</div>' + ('<p class="hint">(body cut at 4000 characters)</p>' if r.get("truncated") else "")
        return f'<article class="rv rv-{e(r["kind"])}"><p class="rv-meta">{" · ".join(meta)}</p>{find}{body}</article>'
    n = len(x["reviews"])
    by_who = {}
    for r in x["reviews"]:
        by_who.setdefault(r["who"], []).append(r)
    def who_sum(who, rs):
        ghs = [VERDICT.get(r["verdict"], "comment") if r["verdict"] else "comment" for r in rs]
        sts = [r["stated"] for r in rs if r.get("stated")]
        part = f'{e(who)}' + (f' ×{len(rs)}' if len(rs) > 1 else "") + " (GitHub: " + e(", ".join(dict.fromkeys(ghs)))
        return part + (f'; stated: {e(", ".join(sts))}' if sts else "") + ")"
    summ = (f'{n} review{"s" if n != 1 else ""} from {len(by_who)} reviewer{"s" if len(by_who) != 1 else ""} · '
            + "; ".join(who_sum(w, rs) for w, rs in by_who.items()))
    if "comments" in unread:
        summ += " · review-style comments not read this run"
    return f'<details class="pr-reviews"><summary>{summ}</summary>{"".join(one(r) for r in x["reviews"])}</details>'


def table(head, rows, cls=""):
    if not rows:
        return '<p class="empty">none</p>'
    th = "".join(f"<th>{e(h)}</th>" for h in head)
    tr = "".join("<tr>" + "".join(f'<td data-h="{e(h)}">{e(v)}</td>' for h, v in zip(head, r)) + "</tr>" for r in rows)
    return f'<div class="tw"><table class="{cls}"><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table></div>'


SURFACES = [("overview", "◈", "Overview"), ("intake", "✳", "Idea intake"), ("questions", "❖", "Questions"),
            ("choices", "⌁", "Choices"), ("topology", "◎", "Topology"), ("budget", "◉", "Budget control"),
            ("lanes", "▦", "Project lanes"), ("run", "◇", "Run commitments"), ("review", "◷", "Review"),
            ("prs", "⇄", "Pull requests"), ("status", "☷", "Status"), ("actions", "☐", "Actions")]
CREDIT = "design after the Cockpit v0.4 prototype (2026-11-04)"


def head(n, eyebrow, title, lede, tag):
    return (f'<div class="sec-head"><div><p class="eyebrow">{n:02d} / {e(eyebrow)}</p><h1>{e(title)}<em>.</em></h1>'
            f'<p class="lede">{e(lede)}</p></div><span class="tag hot">{e(tag)}</span></div>')


def stat(label, value, small, extra=""):
    return f'<div class="stat">{extra}<p class="stat-label">{e(label)}</p><strong>{e(value)}</strong><small>{e(small)}</small></div>'


def not_wired(what, links, also):
    li = "".join(f'<li><a class="subtle-link" href="{e(u)}" rel="noopener noreferrer">{e(u)}</a></li>' for u in links)
    return (f'<div class="panel pad notwired"><span class="tag">NOT WIRED TO RECORDS YET</span>'
            f'<h2>{e(what)} isn\'t read from the records yet.</h2><p>So nothing is shown here rather than placeholder '
            f'content. {e(also)}</p><p class="mini-label">INTAKE THREAD, FROM THREADS.MD</p>'
            + (f'<ul class="links">{li}</ul>' if li else '<p class="empty">threads.md lists no line mentioning intake.</p>')
            + "</div>")


def questions_table(qs):
    if not qs:
        return '<p class="empty">No open questions. Use Clarify on a choice to ask its proposer on the record.</p>'
    rows = [(q["id"], q.get("about"), q.get("addressed_to"), q.get("raised_by"), q.get("date"), q.get("text") or "(text left out)")
            for q in qs]
    return table(["question", "about", "addressed to", "asked by", "date", "question text"], rows)


def edition_meta(ed, full):
    tag = f'<span class="tag agent">written by {e(ed["author"])}, not the owner</span>' if ed.get("author") else '<span class="tag warn">no author in front matter</span>'
    stale = (f'<span class="tag warn">STALE · {ed["age_days"]} days before the newest record (limit {{}})</span>' if ed.get("stale") else "")
    probs = "".join(f'<span class="tag warn">{e(p)}</span>' for p in ed.get("problems") or [])
    miss = f'<span class="tag warn">sections missing: {e(", ".join(ed["sections_missing"]))}</span>' if ed.get("sections_missing") else ""
    return tag, stale, probs, miss


STATUS_COUNTS = ("waiting on you", "handoffs in progress", "open prs")


def stated_counts(body):
    """The counts an edition states in its Status section, in so many words ("Waiting on you: 1", "Handoffs in
    progress: 9", "Open PRs: 14"); shown as tiles above the narrative (DEC-141 run 1, finding 6). Nothing is
    counted by the page: a line without a number gives no tile."""
    text = body or ""
    m = re.search(r"^##[ \t]+Status\b.*?$(.*?)(?=^##[ \t]|\Z)", text, re.M | re.S)
    sec = m.group(1) if m else text
    out = {}
    for label in STATUS_COUNTS:
        mm = re.search(rf"^\W*{re.escape(label)}\s*:\s*(\d{{1,5}})\b", sec, re.M | re.I)
        if mm:
            out[label] = int(mm.group(1))
    return out


def status_line(data):
    st = data.get("status") or {}
    ed = st.get("latest")
    if not ed:
        return '<p class="hint status-line">No status edition yet (<a class="subtle-link" href="#status" data-goto="status">Status ↗</a>).</p>'
    return (f'<p class="status-line"><a class="subtle-link" href="#status" data-goto="status">Status ↗</a> latest edition '
            f'<b>{e(ed["date"])} · {e(ed["edition"])}</b>, written by {e(ed.get("author") or "?")}'
            + (f' · <b class="warn">stale ({ed["age_days"]} d)</b>' if ed.get("stale") else "") + "</p>")


def status_surface(data):
    st = data.get("status") or {"present": False, "latest": None, "older": [], "unreadable": [], "dir": STATUS_DIR}
    full = data["query"]["content"] == "full"
    if not st["present"] or not st["latest"]:
        body = (f'<div class="notice">No editions in <code>{e(st["dir"])}/</code> yet. An edition is a hand-written file '
                f'<code>{e(st["dir"])}/&lt;YYYY-MM-DD&gt;-&lt;edition&gt;.md</code> with front matter (date, edition, author, covers) and the '
                'four sections Accomplishments / Road bumps / Status / Next steps (relay-kit/status@1; <code>relay-kit edition new</code> '
                'scaffolds one). The page renders it as written and generates nothing.</div>')
    else:
        ed = st["latest"]
        tag, stale, probs, miss = edition_meta(ed, full)
        stale = stale.format(st["drift_days"])
        meta = (f'<p class="attrib">Written by <b>{e(ed.get("author") or "?")}</b> · {e(ed["date"])} {e(ed["edition"])}'
                + (f' · covers {e(ed["covers"])}' if ed.get("covers") else "") + f' · <code>{e(ed["file"])}</code></p>')
        counts = stated_counts(ed.get("body")) if full else {}
        tiles = ("".join(stat(label.upper(), counts[label], "as the edition states it") for label in STATUS_COUNTS if label in counts)) if counts else ""
        tiles = f'<div class="stats-row status-counts">{tiles}</div>' if tiles else ""
        text = (f'<div class="narrative">{markdown_subset(ed["body"])}</div>' if full and ed.get("body")
                else f'<p class="hint">Body left out (--content metadata): {ed["chars"]} characters, sections present: '
                     f'{e(", ".join(s_ for s_ in STATUS_SECTIONS if s_ not in ed["sections_missing"]))}.</p>')
        older = ""
        if st["older"]:
            rows = [[o["date"], o["edition"], o.get("author") or "?", o.get("covers") or "", o["file"]] for o in st["older"]]
            older = (f'<details class="panel pad"><summary>Older editions · {len(st["older"])}</summary>'
                     + table(["date", "edition", "author", "covers", "file"], rows) + "</details>")
        body = (f'<div class="panel pad status-head"><div class="btns">{tag}{stale}{probs}{miss}</div>{meta}{tiles}'
                f'<p class="hint">Rendered as written: headings, lists, bold, italic, inline code and rules; links are shown as text and '
                'not followed; anything else is plain text. Nothing here is summarised by the page.</p></div>'
                f'<article class="panel pad edition">{text}</article>{older}')
        if st["unreadable"]:
            body += '<p class="hint">Not read: ' + e("; ".join(f'{u["file"]} ({u["reason"]})' for u in st["unreadable"])) + "</p>"
    return f"""
<section class="section surface" id="status">
{head(11, "NARRATIVE", "Status", "The coordinator's account of what was done, what went wrong, where things stand and what comes next: written by hand, shown as written.", "AUTHORED · NOT GENERATED")}
{body}
</section>"""


def actions_line(data):
    ac = data.get("actions") or {}
    if not ac.get("present"):
        return ""
    oldest = max((it.get("age_days") or 0 for fl in ac["files"] for it in fl["items"] if not it["done"]), default=None)
    return (f'<p class="status-line"><a class="subtle-link" href="#actions" data-goto="actions">Actions ↗</a> '
            f'<b class="{"warn" if ac["open"] else ""}">{ac["open"]} open</b> for you'
            + (f', the oldest since {oldest} days before the newest record' if oldest else "") + f' · {ac["done"]} done</p>')


def action_item(it, full, as_of):
    """One checklist line as written (inline markdown only), with what the author stated beside it: since
    when (and how old that is, measured to the newest record), the size, the due date. Nothing is inferred."""
    meta = []
    if it.get("since"):
        meta.append(f'since {e(it["since"])}' + (f' · <b>{it["age_days"]} d</b>' if it.get("age_days") is not None else ""))
    elif not it["done"]:
        meta.append("since not stated")
    if it.get("due"):
        dd = it.get("days_to_due")
        meta.append(f'due {e(it["due"])}' + (f' · <b class="{"warn" if dd is not None and dd <= 7 else ""}">{"in " + str(dd) + " d" if dd is not None and dd >= 0 else str(-dd) + " d ago" if dd is not None else ""}</b>' if dd is not None else ""))
    meta.append(f'size {e(it["size"])}' if it.get("size") else "size not stated")
    if it.get("section") and it["section"].lower() not in ("open", "done"):
        meta.append(f'under “{e(it["section"])}”')
    text = inline_md(e(it["text"])) if full and it.get("text") else f'<span class="hint">text left out (--content metadata), line {it["line"]}</span>'
    return (f'<li class="action{" done" if it["done"] else ""}"><span class="box" aria-hidden="true">{"☑" if it["done"] else "☐"}</span>'
            f'<div><p class="action-text">{text}</p><p class="action-meta">{" · ".join(meta)}</p></div>'
            + ("" if it["done"] else '<span class="tag hot needs">NEEDS YOU</span>') + "</li>")


def actions_surface(data):
    ac = data.get("actions") or {"present": False, "source": ACTIONS_DIR + "/", "files": [], "unreadable": [], "open": 0, "done": 0}
    full = data["query"]["content"] == "full"
    if not ac["present"]:
        body = (f'<div class="notice">No action list. Pass <code>--actions FILE</code> (a markdown checklist: <code>- [ ]</code> open, '
                f'<code>- [x]</code> done, under any headings) or keep one or more under <code>{e(ACTIONS_DIR)}/*.md</code> in the records store '
                '(relay-kit/actions@1). The page shows the list as written and generates nothing.</div>')
    else:
        blocks = []
        for fl in ac["files"]:
            open_, done = [it for it in fl["items"] if not it["done"]], [it for it in fl["items"] if it["done"]]
            oldest = sorted((it for it in open_ if it.get("age_days") is not None), key=lambda it: -it["age_days"])
            items = "".join(action_item(it, full, data["as_of"]) for it in open_) or '<li class="empty">Nothing open in this list.</li>'
            folded = (f'<details class="done-fold"><summary>Done · {len(done)}</summary><ul class="actions">'
                      + "".join(action_item(it, full, data["as_of"]) for it in done) + "</ul></details>") if done else ""
            blocks.append(
                f'<article class="panel pad action-list"><div class="panel-head"><h2>{e(fl.get("title") or fl["file"])}</h2>'
                f'<span class="tag hot">{len(open_)} OPEN · {len(done)} DONE</span></div>'
                f'<p class="hint">{e(fl["file"])} · as written by its author; the page ticks nothing'
                + (f' · oldest open item: since {e(oldest[0]["since"])} ({oldest[0]["age_days"]} d)' if oldest else "")
                + f' · ages and due dates are counted to the newest record, {e(data["as_of"][:10])}, not today'
                + (f' · {sum(1 for it in open_ if not it.get("since"))} open without a stated since-when' if any(not it.get("since") for it in open_) else "")
                + (f' · {sum(1 for it in open_ if not it.get("size"))} without a stated size' if any(not it.get("size") for it in open_) else "") + "</p>"
                + (f'<p class="action-intro">{inline_md(e(fl["intro"]))}</p>' if fl.get("intro") else "")
                + f'<ul class="actions">{items}</ul>{folded}</article>')
        body = "".join(blocks)
        if ac["unreadable"]:
            body += '<p class="hint">Not read: ' + e("; ".join(f'{u["file"]} ({u["reason"]})' for u in ac["unreadable"])) + "</p>"
    return f"""
<section class="section surface" id="actions">
{head(12, "HUMAN TO-DO", "Actions", "The things only you can do, kept by hand in the records and shown as written: what, why or where, since when, how big.", "NEEDS YOU · AUTHORED, NOT GENERATED")}
<div class="stats-row">{stat("OPEN", str(ac["open"]), "waiting on you")}{stat("DONE", str(ac["done"]), "ticked in the list")}
{stat("SOURCE", "--actions" if ac["source"] == "--actions" else ACTIONS_DIR + "/", "relay-kit/actions@1")}</div>
{body}
</section>"""


def render(data):
    c, b, run, topo = data["counts"], data["budget"], data["run"], data["topology"]
    full = data["query"]["content"] == "full"
    brand = data["query"].get("brand") or "relay-kit"
    allowance, burn = b.get("weekly_allowance"), b.get("burn_7d")
    if not b["present"]:
        bg = gauge(0, None, "resource budget / 7 d", "NO RECORDS", "no budget records yet", "muted")
        budget_read = "NO RECORDS"
    elif burn is None:
        bg = gauge(0, None, "resource budget / 7 d", "NO DATA", "no session lines in the window", "muted")
        budget_read = "NO DATA"
    elif allowance:
        frac = burn / allowance
        bg = gauge(burn, allowance, "resource budget / 7 d", f"{frac:.0%}", f"{fmt_n(burn)} of {fmt_n(allowance)} tokens",
                   "bad" if frac >= 0.9 else ("warn" if frac >= 0.7 else "ok"))
        budget_read = f"{frac:.0%}"
    else:
        bg = gauge(0, None, "resource budget / 7 d", fmt_n(burn), "tokens · allowance unset", "muted")
        budget_read = "UNSET"
    kinds = c["choices_by_kind"]
    dials = (gauge(c["choices"], max(c["choices"], 10), "choice matrix / open", str(c["choices"]),
                   f'{kinds["decision"]} decisions · {kinds["handoff"]} handoff{"" if kinds["handoff"] == 1 else "s"} · {kinds["challenge"]} challenges',
                   "warn" if c["choices"] else "ok")
             + bg
             + gauge(c["authorized_in_progress"], max(c["open_handoffs"], 1), "run commitments / authorized",
                     str(c["authorized_in_progress"]), f'authorized & in progress · {c["pending_authorization"]} pending authorization', "ok")
             + gauge(c["drift_flags"], max(c["drift_flags"], 5), "drift flags", str(c["drift_flags"]),
                     f'stale proposals > {run["drift"]["drift_days"]} d · drift challenges', "bad" if c["drift_flags"] else "ok"))
    waiting = "".join(f'<a class="direction" href="#choices" data-goto="choices" data-card="{e(x["kind"])}:{e(x["id"])}">'
                      f'<span class="direction-num">{i:02d}</span><span class="direction-body"><strong>{e(x["id"])}</strong>'
                      f'<p>{e(trunc(x.get("text") or x.get("title") or x["kind"] + " · " + x["status"], 120))}</p></span>'
                      f'<span class="tag">{e(x["kind"])}</span></a>' for i, x in enumerate(data["choices"][:6], 1)) \
        or '<p class="empty">Nothing waits on you.</p>'
    s_overview = f"""
<section class="section surface" id="overview">
{head(1, "MISSION OVERVIEW", "Overview", "Generated from your records: what waits on you, what is running, what it costs, and where it drifts.", "ADVISORY ONLY")}
<div class="dials">{dials}</div>
{status_line(data)}{actions_line(data)}
<div class="overview-grid">
 <div class="panel pad"><div class="panel-head"><p class="eyebrow dark">WAITING ON YOU</p>
 <a class="subtle-link" href="#choices" data-goto="choices">Open Choices ↗</a></div><div class="direction-list">{waiting}</div></div>
 <div class="panel pad principle-panel"><p class="eyebrow dark">THE GOVERNING DISTINCTION</p>
 <div class="split-concept"><div class="concept"><span>01 / DRIFT</span><strong>Explore without obligation.</strong>
 <p>Questions, hypotheses, alternatives and uncertainty belong here.</p></div><div class="concept-divider">≠</div>
 <div class="concept run-concept"><span>02 / RUN</span><strong>Act with explicit intent.</strong>
 <p>A commitment needs your authorization and a concrete next action.</p></div></div>
 <p class="principle-footer">✳ No automatic promotions. Ever. This page writes nothing.</p></div>
</div></section>"""

    s_intake = f"""
<section class="section surface" id="intake">
{head(2, "OPEN INQUIRY", "Idea intake", "Somewhere to throw things before they need to make sense.", "DRIFT BY DEFAULT")}
{not_wired("Idea intake", data["intake_links"], "Capture in the intake thread; nothing captured there becomes a task on its own.")}
</section>"""
    s_questions = f"""
<section class="section surface" id="questions">
{head(3, "QUESTIONS & REALIZATIONS", "Questions", "Open questions on the record: each was asked with Clarify, and waits on whoever it is addressed to.", "ON THE RECORD / NO AGENT EXECUTION")}
<div class="notice"><b>A question is a record.</b> Clarify on a choice composes an open challenge with <code>"kind": "question"</code>,
<code>about</code> the item and <code>addressed_to</code> its proposer. It leaves Choices (it waits on them, not you) and shows here
until someone appends a line that <code>resolves</code> it, with the answer as its resolution. Off-record questions (Inquire)
are never recorded and never appear here.</div>
{questions_table(data.get("questions") or [])}
</section>"""

    cards = "".join(choice_card(x, (data.get("pr_state") or {}).get("fetched_at")) for x in data["choices"]) or '<p class="empty">Nothing waits on you.</p>'
    s_choices = f"""
<section class="section surface" id="choices">
{head(4, "HUMAN DECISION CONSOLE", "Choices", "Proposed decisions, handoffs that need you, and open challenges, read from the records.", "CHOICE ≠ COMMITMENT / ONLY YOU CAN SETTLE")}
<div class="stats-row">{stat("DECISIONS PROPOSED", kinds["decision"], "not superseded")}{stat("HANDOFFS WAITING", kinds["handoff"], "prepared, unauthorized or to you")}
{stat("OPEN CHALLENGES", kinds["challenge"], "not resolved")}{stat("LEDGER SYNC", "READ-ONLY", "this page appends nothing")}</div>
<div class="notice"><b>Authority stays with your paste (DEC-019).</b> Accept, Reject and Defer write nothing. Each composes a line
into <i>To apply</i>: a <code>relay-kit ledger append</code> command for decisions and challenges, a <code>relay-kit handoff set</code>
command for handoffs. <b>You supply the justification:</b> every line carries <code>&lt;&lt;REQUIRED: …&gt;&gt;</code>
placeholders for the decider, your words (basis) and where you said them (locator), and the ledger check refuses a line that
still holds one. The page never fills in who decided or why. Replace them, copy, and run the line in your records with
<code>RELAYKIT_ACTOR={e(data["owner"])}</code>, which records you as the writer. Until you do, nothing has changed.</div>
<div class="choices-grid"><div class="cards">{cards}</div>
<aside class="apply panel"><p class="eyebrow dark">TO APPLY</p><textarea id="apply" spellcheck="false" aria-label="Lines to apply"
placeholder="Choose Accept, Reject or Defer on an item."></textarea>
<div class="btns"><button type="button" class="primary small" id="copy">Copy</button><button type="button" class="secondary small" id="clear">Clear</button>
<span id="copied" role="status"></span></div></aside></div>
</section>"""

    a = topo["authority"]
    rec, hyp = a["recorded"], a["hypothesised"]
    rec_rows = [(x["decision"], x.get("date"), x["from"], x["to"], x["delegates_to"]) for x in rec["edges"]]
    deleg_rows = [(x["decision"], x.get("date"), x["from"], x["to"], "inferred from text") for x in hyp["edges"] if x["kind"] == "delegation"]
    rec_block = (f'<div class="scroll">{authority_svg(rec, "rec")}</div><p class="mini-label">RECORDED DELEGATIONS</p>'
                 + table(["decision", "date", "from", "to", "delegates_to"], rec_rows)) if rec["edges"] else \
        ('<p class="empty norec">No recorded delegation. A delegation is recorded when an adopted decision names its delegate '
         'in a <code>delegates_to</code> field; none does yet.</p>')
    s_topo = f"""
<section class="section surface" id="topology">
{head(5, "MISSION MAPS", "Topology", "Two systems drawn from the records: where information flows, and who may act.", "DRAWN FROM RECORDS / NO LIVE AGENT STATES")}
<div class="selector" role="group" aria-label="Network selector"><span class="mini-label">NETWORK SELECTOR / 01–02</span>
<button type="button" class="secondary small sel" data-net="info" aria-pressed="true">◈ Information</button>
<button type="button" class="secondary small sel" data-net="auth" aria-pressed="false">⌘ Authority / agents</button></div>
<div class="panel pad net" data-net="info"><p class="eyebrow dark">01 / INFORMATION</p>
<p class="legend"><span class="k k-actor"></span>actor (assistant:lane) <span class="k k-owner"></span>owner
<span class="k k-thread"></span>thread in threads.md <span class="k k-capture"></span>captured conversation
<span class="k k-handoff"></span>handoff: dot at the sender, arrowhead at the receiver, the count in a pill beside the arrowhead (thicker = more);
a loop above a node is handoffs to itself
<span class="k k-captured"></span>actor → capture → thread. Rows are lanes; a lane marked <b>NO THREAD</b> has no line in threads.md;
placement inside a row is automatic and means nothing. Hover a node or edge for detail. The table beside the graph carries the same counts per actor.</p>
<div class="topo-grid"><div class="scroll">{info_svg(topo["information"])}</div>
<div><p class="mini-label">HANDOFFS PER ACTOR · SENT / RECEIVED</p>{actor_table(topo["information"])}</div></div></div>
<div class="panel pad net" data-net="auth"><p class="eyebrow dark">02 / AUTHORITY / AGENTS · RECORDED ONLY</p>
<p class="legend"><span class="k k-recorded"></span>recorded delegation: an adopted, in-force decision with a <code>delegates_to</code>
field (label = decision id). Nothing executable is derived from this or the overlay below: the decisions are the authority.</p>
{rec_block}
<details class="hyp" id="hyp"><summary>Show hypothesised authority (inferred)</summary>
<p class="eyebrow dark">HYPOTHESISED AUTHORITY (INFERRED) · NOT A RECORD</p>
<p class="legend"><span class="k k-structure"></span>assumed reporting line, owner → coordinator → lanes (no decision says so)
<span class="k k-delegation"></span>text match: an in-force decision whose text mentions delegating or authorizing, drawn to the lanes
its scope names. These are readings, not delegations. Layout is automatic.{"" if a["coordinator"] else " No coordinator lane found (none is named *coord*; pass --coordinator)."}</p>
<div class="scroll">{authority_svg(hyp, "hyp")}</div>
<p class="mini-label">TEXT-MATCHED EDGES</p>{table(["decision", "date", "from", "to", "status"], deleg_rows)}</details></div>
</section>"""

    pol = b["policy"]
    pol_rows = [(k.replace("_", " "), "UNSET (unknown, not unlimited)" if v is None else v) for k, v in pol["caps"].items()]
    remaining = (allowance - burn) if (allowance and b["present"] and burn is not None) else None
    wref = b.get("weekly_reference")
    if b["present"]:
        bud = f"""
<div class="panel pad">{dl([("window", f'{b["window"]["from"]} → {b["window"]["to"]}'), ("burn", f'{fmt_n(burn)} tokens, {b.get("burn_label") or "unlabelled"} ({b["burn_source"]})'),
  ("session lines, 7 d", f'{fmt_n(b["session_tokens_7d"])} ({fmt_n(b["session_in_7d"])} in, {fmt_n(b["session_out_7d"])} out)'),
  ("by how it was measured", "; ".join(f'{m}: {fmt_n(v)}' for m, v in (b.get("session_tokens_7d_by_method") or {}).items()) or None),
  ("corrections applied", (", ".join(f'{k} replaced by {v}' for k, v in b["corrections"]["replaced"].items())
                           + ("; conflicting corrections (the later one counts): " + ", ".join(c["id"] for c in b["corrections"]["conflicts"]) if b["corrections"]["conflicts"] else ""))
                          if b.get("corrections") and b["corrections"]["replaced"] else None),
  ("children inside a parent's numbers", ", ".join(f'{c} in {p}' for c, p in b["corrections"]["children_included"].items()) or None
   if b.get("corrections") else None),
  ("budget lines that take no effect as written", "; ".join(f'{x.get("id")}: {x["why"]}' for x in b["corrections"]["invalid"])
   + ("; duplicate ids: " + ", ".join(d["id"] for d in b["corrections"]["duplicate_ids"]) if b["corrections"]["duplicate_ids"] else "") or None
   if b.get("corrections") else None),
  ("weekly reading (reference only)", f'{fmt_n(wref["tokens"])} tokens at {wref["ts"]}, reported by {wref["actor"]}; not added to the gauge' if wref else None),
  ("most agents at once", fmt_n(b["max_agents_7d"])), ("records", b["records"]),
  ("invalid lines", ", ".join(f'{x["line"]}: {x["reason"]}' for x in b["invalid_lines"]) or "none")])}
<div class="cols"><div><p class="mini-label">LAST 7 DAYS BY LANE</p>{table(["lane", "tokens"], [(k, fmt_n(v)) for k, v in b["by_lane"].items()])}</div>
<div><p class="mini-label">LAST 7 DAYS BY ACTOR</p>{table(["actor", "tokens"], [(k, fmt_n(v)) for k, v in b["by_actor"].items()])}</div></div>
{('<p class="mini-label">RECENT NOTES</p>' + table(["ts", "actor", "note"], [(x["ts"], x["actor"], x["note"]) for x in b.get("recent_notes", [])])) if full else ""}</div>"""
    else:
        bud = '<div class="panel pad"><p class="empty">No budget records yet: <code>governance/budget.jsonl</code> is absent. Its schema is below.</p></div>'
    s_budget = f"""
<section class="section surface" id="budget">
{head(6, "RESOURCE & ATTENTION GOVERNOR", "Budget control", "Recorded token use against a weekly allowance, with an advisory policy before any automated enforcement.", "RECORDED DATA · NO LIVE PROVIDER TELEMETRY")}
<div class="stats-row">{stat("CONTROL MODE", pol["caps"].get("default_effort") or "UNSET", "advisory setting")}
{stat("WEEKLY ALLOWANCE", fmt_n(allowance) if allowance else "UNSET", "tokens · --weekly-tokens or governance/cockpit.json")}
{stat("BURN / LAST 7 DAYS", fmt_n(burn) if burn is not None else "—", b.get("burn_source") or "no budget records yet")}
{stat("LIVE METERING", "OFFLINE", "no external services connected")}</div>
<div class="budget-top"><div class="gauge-big">{bg}</div>{bud}</div>
<div class="overview-grid">
<div class="panel pad"><div class="panel-head"><h2>Resource policy</h2><span class="tag hot">ADVISORY ONLY</span></div>
<p class="hint">These caps constrain the policy model shown here, not Claude, ChatGPT, Codex, AWS or any account. Set them under
<code>policy</code> in <code>governance/cockpit.json</code>{f' (read from {e(pol["source"])})' if pol["source"] else " (none set)"}.</p>
{table(["cap", "value"], pol_rows)}</div>
<div class="panel pad" id="preflight" data-remaining="{'' if remaining is None else remaining}"><p class="eyebrow dark">PRE-FLIGHT / DECISION SUPPORT</p>
<h2>Should we spend on this question?</h2><p class="hint">Estimate before delegating. This launches nothing and reserves nothing.</p>
<label class="mini-label" for="pf-tokens">ESTIMATED TOKENS</label><input id="pf-tokens" type="number" min="0" inputmode="numeric" placeholder="unknown">
<label class="mini-label" for="pf-imp">QUESTION IMPORTANCE</label><select id="pf-imp"><option value="routine">Routine / reversible</option>
<option value="meaningful">Meaningful decision</option><option value="high">High consequence / evidence-sensitive</option></select>
<button type="button" class="primary small" id="pf-go">Evaluate route ↗</button><p id="pf-out" class="pf-out" role="status">NOT ASSESSED</p></div>
</div>
<div class="principles">
<div class="concept"><span>01 / COST</span><p>Money, tokens and provider allowances are different resources. No token-to-dollar estimate without a model and rate.</p></div>
<div class="concept"><span>02 / CAPACITY</span><p>Concurrency slots are ceilings, not occupancy measurements.</p></div>
<div class="concept"><span>03 / ATTENTION</span><p>Your review and interruptions consume a budget too. A question can wait instead of interrupting.</p></div>
<div class="concept"><span>04 / AUTHORITY</span><p>A budget may constrain routing; it cannot grant agents rights to decide, spawn or execute.</p></div></div>
<p class="mini-label">BUDGET.JSONL SCHEMA</p>{table(["field", "meaning"], list(b["schema"].items()))}
<p class="hint">Append with <code>relay-kit budget-append --window session --tokens-in N --tokens-out N</code>. The gauge sums the
<i>session</i> lines inside the 7-day window; a <i>weekly</i> line is shown only as a separately labelled reference. With no session
lines the gauge says so; it never shows a zero it didn't observe. The coordinator's status-file cost lines are
<b>not</b> parsed.</p>
</section>"""

    lane_cards = "".join(
        f'<div class="lane-card panel pad"><div class="panel-head"><p class="eyebrow dark">{e(x["code"] or "—")}'
        f'{" · COORDINATOR" if x["coordinator"] else ""}</p><span class="tag">{e(x["last_activity"] or "no activity")}</span></div>'
        f'<h2>{e(x["lane"])}</h2>{dl([("handoffs", f"{x["open_handoffs"]} open of {x["handoffs"]}"), ("decisions", f"{x["decisions"]} ({x["proposed"]} proposed)"), ("open challenges", x["open_challenges"]), ("threads", x["threads"])])}</div>'
        for x in data["lanes"]) or '<p class="empty">No lanes declared in handoffs/.ledger.json or threads.md.</p>'
    s_lanes = f"""
<section class="section surface" id="lanes">
{head(7, "THE CONSTELLATION", "Project lanes", "Separate places to build, investigate, and return: lanes declared in .ledger.json and threads.md.", "FROM RECORDS · NO LIVE SYNC")}
<div class="lane-grid">{lane_cards}</div></section>"""

    hs = run["handoffs_by_status"]
    status_blocks = "".join(
        f'<details{" open" if s not in ("completed", "cancelled") else ""}><summary>{e(s)} <b>{len(v)}</b></summary>'
        + table(["id", "title", "from → to", "last"],
                [(x["id"], x["title"] if x["title"] is not None else "(title left out)", f'{x["from"]} → {x["to"]}',
                  f'{x["last"]["at"] or ""} {x["last"]["state"] or ""}') for x in (v if s not in ("completed", "cancelled") else v[-10:])])
        + "</details>" for s, v in hs.items()) or '<p class="empty">no handoffs</p>'
    prs = "".join(f'<li><a class="subtle-link" href="{e(u)}" rel="noopener noreferrer">{e(u)}</a></li>' for u in run["pr_links"])
    dr = run["drift"]
    stale_items = "".join(
        f'<div class="direction"><span class="direction-num">{e(x["age_days"])} d</span><span class="direction-body"><strong>{e(x["id"])}</strong>'
        f'<p>{e(trunc(x.get("text") or "proposed " + str(x["date"]), 140))}</p></span>'
        f'<button type="button" class="secondary small torun" data-card="decision:{e(x["id"])}">Move to Run?</button></div>'
        for x in dr["stale_proposals"]) or '<p class="empty">none</p>'
    s_run = f"""
<section class="section surface" id="run">
{head(8, "RUN COMMITMENTS", "Run commitments", "Handoffs by state, and what has drifted: proposals left waiting and challenges that name drift.", "EXPLICITLY AUTHORIZED ONLY")}
<div class="stats-row">{stat("AUTHORIZED & IN PROGRESS", c["authorized_in_progress"], "sent, acknowledged or in progress, with an Authorized section")}
{stat("PENDING AUTHORIZATION", c["pending_authorization"], "prepared, or no authorization quote: not a commitment yet")}</div>
<div class="cols"><div class="panel pad"><p class="eyebrow dark">AUTHORIZED &amp; IN PROGRESS</p>
{table(["id", "status", "from → to"], [(x["id"], x["status"], f'{x["from"]} → {x["to"]}') for x in run["commitments"]["authorized"]])}
<p class="eyebrow dark">PENDING AUTHORIZATION · NOT RUN</p>
{table(["id", "status", "from → to"], [(x["id"], x["status"], f'{x["from"]} → {x["to"]}') for x in run["commitments"]["pending"]])}
<p class="eyebrow dark">ALL HANDOFFS BY STATUS</p>{status_blocks}
<p class="mini-label">PULL REQUESTS</p><p class="hint">Open pull requests aren't fetched (no network): see GitHub. Links listed in threads.md:</p>
{f"<ul class='links'>{prs}</ul>" if prs else '<p class="empty">none in threads.md</p>'}</div>
<div class="panel pad"><p class="eyebrow dark">DRIFT</p><p class="mini-label">PROPOSALS OLDER THAN {dr["drift_days"]} DAYS</p>
<div class="direction-list">{stale_items}</div>
<p class="mini-label">CHALLENGES MENTIONING DRIFT</p>
{table(["id", "status", "raised by"] + (["concern"] if full else []), [[x["id"], ("open" if x["open"] else x["status"]), x["raised_by"]] + ([trunc(x.get("text"), 160)] if full else []) for x in dr["challenges_mentioning_drift"]])}</div></div>
</section>"""

    tr = data.get("transitions") or {"problems": [], "conflicts": []}
    tel = data.get("telemetry") or {"metrics": [], "notes": []}

    def tel_value(m):
        k = m["metric"]
        d = lambda x: f'median {x["median"]}, p90 {x["p90"]}, n {x["n"]}' if x and x.get("n") else "unknown"
        if k == "compute_consumption":
            v = "; ".join(f"{mm}: {fmt_n(n)}" for mm, n in m["tokens_by_method"].items()) or "unknown"
            return (f'{v} tokens over {m["active_days"]} active day(s), {m["lines"]} line(s)'
                    + (f'; {m["incomplete_lines"]} incomplete (lower bounds)' if m.get("incomplete_lines") else "")
                    + (f'; {len(m["invalid_lines"])} invalid line(s) not counted' if m.get("invalid_lines") else "")
                    + (f'; measured and estimated lines for the same actor: {", ".join(m["possible_double_count_actors"])}' if m.get("possible_double_count_actors") else ""))
        if k == "delivery_throughput":
            weeks = "; ".join(f'{w}: {sum(r.values())}' for w, r in m["merged_per_week"].items())
            return f'{m["merged"]} merged ({weeks or "none"}); {m["abandoned"]} abandoned'
        if k == "lead_time":
            return d(m["hours"]) + " (hours)" + (f'; excluded: {m["excluded"]}' if m.get("excluded") else "")
        if k == "review_burden":
            return d(m["rounds"]) + " change-request rounds per merged PR"
        if k == "post_merge_remediation":
            return "; ".join(f'{w}: ' + (f'{v["remediated"]} of {v["cohort"]}' if v["cohort"] else (v["unknown"] or "unknown"))
                             for w, v in m["windows"].items())
        if k == "surviving_changes":
            return f'{m["count"]} of {m["cohort"]}' if m.get("count") is not None else (m.get("unknown") or "unknown")
        if k == "human_intervention":
            return f'{m["minutes_recorded"]} min from {m["lines"]} line(s)' if m.get("minutes_recorded") is not None else "unknown"
        if k == "measurement_coverage":
            return (f'{m["with_measured_budget_line"]} of {m["agent_sessions_in_handoffs"]} agent sessions measured; '
                    f'budget lines {", ".join(f"{a}: {b}" for a, b in m["budget_lines_by_method"].items()) or "none"}; '
                    f'{len(m["repositories_read"])} repositories read'
                    + (f'; more PRs than read in {", ".join(m["repositories_with_more_than_read"])}' if m.get("repositories_with_more_than_read") else ""))
        if k == "coordination_loss":
            return (f'{len(m["stale_handoffs"])} stale handoff(s), {len(m["cancelled_handoffs"])} cancelled, '
                    f'{m["overlapping_claim_count"] if m.get("overlapping_claim_count") is not None else "unknown"} overlapping claim(s), {len(m["ledger_conflicts"])} ledger conflict(s); '
                    f'reconciliation time unknown')
        return ""
    tel_rows = [[m["metric"], m["label"], tel_value(m), m["definition"]] for m in tel["metrics"]]
    s_review = f"""
<section class="section surface" id="review">
{head(9, "REVIEW", "Review", "The recent decision timeline and the challenges still open.", "LEDGER IS THE SOURCE OF TRUTH")}
<div class="panel pad"><p class="eyebrow dark">RECENT DECISIONS / LAST 20</p>
{table(["id", "date", "status", "supersedes"] + (["decision"] if full else []), [[x["id"], x["date"], x["status"], x["supersedes"] or ""] + ([trunc(x.get("text"), 200)] if full else []) for x in run["decisions_recent"]], "timeline")}</div>
<div class="panel pad"><p class="eyebrow dark">OPEN CHALLENGES</p>
{table(["id", "date", "raised by"] + (["concern"] if full else []), [[x["id"], x["date"], x["raised_by"]] + ([trunc(x.get("text"), 160)] if full else []) for x in run["challenges_open"]])}</div>
<div class="panel pad"><p class="eyebrow dark">LEDGER TRANSITIONS THAT TAKE NO EFFECT, AND CONFLICTS (relay-kit/lifecycle@1)</p>
<p class="hint">A <code>supersedes</code> counts only from a settled line, a <code>resolves</code> only from a resolved or
accepted-risk one, and only toward an earlier record. Lines that break this are listed here and close nothing; a record closed by
more than one line is shown with all of them.</p>
{table(["id", "what"], [[x["id"], x["message"]] for x in tr["problems"]] + [[x["id"], "closed by more than one line: " + ", ".join(x["closed_by"] or [])] for x in tr["conflicts"]])
 if tr["problems"] or tr["conflicts"] else '<p class="empty">None.</p>'}</div>
<div class="panel pad"><p class="eyebrow dark">TELEMETRY (relay-kit/telemetry@1, DERIVED, AS OF {e(tel.get("as_of") or "unknown")})</p>
<p class="hint">Rebuilt from the records on every build: budget lines, pull-request state, handoffs and ledgers. Each figure says how it
was measured; distributions, not means; unknown, never zero. {" ".join(e(n) for n in tel.get("notes", [])[1:])}</p>
{table(["metric", "label", "value", "definition"], tel_rows) if tel_rows else '<p class="empty">No telemetry.</p>'}</div>
</section>"""

    prd = data.get("pull_requests") or {"present": False, "items": [], "fetches": [], "unreadable": 0, "file": PRS_FILE}
    if not prd["present"]:
        pr_body = (f'<div class="notice">No <code>{e(prd["file"])}</code> in the records yet. Run <code>relay-kit prs --root RECORDS '
                   '--repo OWNER/NAME</code> (or list the repositories under <code>"repos"</code> in handoffs/.ledger.json), then '
                   'rebuild this page. The page itself never fetches.</div>')
    else:
        fetch_rows = [[e(f["repo"]), (f'<span class="age" data-ts="{e(f["last_ok"])}">{e(f["last_ok"][:16].replace("T", " "))}</span>'
                                      if f["last_ok"] else "never"),
                       f'<span title="{e(f["error"])}">{e(trunc(f["error"], 90))}</span>' if f["error"] else ""] for f in prd["fetches"]]
        fetch_tbl = ('<div class="tw"><table><thead><tr><th>repository</th><th>last read</th><th>last error</th></tr></thead><tbody>'
                     + "".join("<tr>" + "".join(f'<td data-h="{h}">{v}</td>' for h, v in zip(("repository", "last read", "last error"), r))
                               + "</tr>" for r in fetch_rows) + "</tbody></table></div>")
        okd = [f["last_ok"] for f in prd["fetches"] if f["last_ok"]]
        nfail = sum(1 for f in prd["fetches"] if f["error"])
        npart = sum(f.get("partial") or 0 for f in prd["fetches"])
        nmore = sum(1 for f in prd["fetches"] if f.get("more_available"))
        fetch_sum = (f'{len(prd["fetches"])} repositories, last read <span class="age" data-ts="{e(max(okd))}">{e(max(okd)[:16].replace("T", " "))}</span>'
                     if okd else f'{len(prd["fetches"])} repositories, never read') + (f' · <b class="warn">{nfail} failed</b>' if nfail else "") + (f' · <b class="warn">{npart} partial read(s)</b>' if npart else "") + (f' · <b class="warn">{nmore} repo(s) had more pull requests than were read</b>' if nmore else "")
        days = sorted({f["since_days"] for f in prd["fetches"] if f.get("since_days")}) or [14]
        older = sum(1 for x in prd["items"] if not x["recent"])
        groups = [("Open", [x for x in prd["items"] if x["state"] == "open"]),
                  (f"Merged or closed in the last {days[-1]} days", [x for x in prd["items"] if x["state"] != "open" and x["recent"]])]
        pr_body = (f'<details class="panel pad pr-src"><summary>Where this comes from · {fetch_sum}</summary><p class="hint">Read from '
                   f'<code>{e(prd["file"])}</code>, written by <code>relay-kit prs</code> at build time. Sections are the PR body\'s '
                   'own words (## What / ## Why / ## How / ## Authorized); nothing here is summarised. Ages are counted in your '
                   'browser from the read time; the page fetches nothing.'
                   + (f' {prd["unreadable"]} line(s) of the file were unreadable.' if prd["unreadable"] else "") + f'</p>{fetch_tbl}</details>'
                   + "".join(f'<p class="eyebrow dark">{e(g.upper())} · {len(xs)}</p><div class="cards">'
                             + ("".join(pr_card(x, full) for x in xs) or '<p class="empty">None.</p>') + "</div>"
                             for g, xs in groups)
                   + (f'<p class="hint">{older} older merged or closed pull request(s) are in the file but not shown.</p>' if older else ""))
    s_prs = f"""
<section class="section surface" id="prs">
{head(10, "PULL REQUESTS", "Pull requests", "What each pull request changes, why, how and on whose authority, in its author's words, with its reviews and CI.", "FROM RECORDS · READ AT BUILD TIME")}
{pr_body}
</section>"""

    s_status = status_surface(data)
    s_actions = actions_surface(data)
    nav = "".join(f'<a class="nav-link" href="#{k}" data-goto="{k}" data-name="{e(t)}"><span class="nav-icon">{i}</span>{e(t)}'
                  f'<span class="nav-kbd">{n:02d}</span></a>' for n, (k, i, t) in enumerate(SURFACES, 1))
    limits = "".join(f"<li>{e(x)}</li>" for x in data["limits"])
    unread = (f'<p class="hint">Unreadable: {e(", ".join(str(u["path"]) for u in data["unreadable"]))}</p>' if data["unreadable"] else "")
    q = data["query"]
    modal = """
<dialog id="settle" aria-labelledby="settle-title" aria-describedby="settle-body"><form method="dialog"><p class="eyebrow dark" id="settle-eyebrow">SETTLE THIS CHOICE?</p><h2 id="settle-title"></h2>
<p class="hint" id="settle-body"></p><pre id="settle-line"></pre><div class="btns"><button value="ok" class="primary small" id="settle-ok">Add to To apply</button>
<button value="cancel" class="secondary small">Cancel</button></div></form></dialog>"""
    sn = data.get("snapshot") or {}
    if sn.get("revision"):
        snap_label = (f'records rev <code>{e(sn["revision"])}</code>, committed <span class="age" data-ts="{e(sn["committed_at"] or "")}">'
                      f'{e(sn["committed_at"] or "?")}</span>, last fetched '
                      + (f'<span class="age" data-ts="{e(sn["fetched_at"])}">{e(sn["fetched_at"])}</span>' if sn.get("fetched_at") else "unknown")
                      + (' <b class="warn">+ uncommitted changes</b>' if sn.get("uncommitted")
                         else ' <b class="warn">(working tree state unknown)</b>' if sn.get("uncommitted") is None else "") + " · ")
    else:
        snap_label = '<b class="warn">records revision unknown (not a git repository)</b> · '

    theme = data.get("theme")
    dflt = (theme or {}).get("default", "auto")
    html_theme = f' data-theme="{dflt}"' if theme and dflt != "auto" else ""
    toggle = ('<button type="button" class="secondary small" id="theme-toggle" aria-live="polite" '
              f'data-default="{e(dflt)}">Theme: {e(dflt)}</button>') if theme and "light" in theme else ""
    return f"""<!doctype html>
<html lang="en"{html_theme}><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow"><meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:">
<title>{e(brand.title())} · Cockpit</title><style>{CSS}{theme_css(theme)}</style>{THEME_EARLY if toggle else ""}</head>
<body data-owner="{e(data["owner"])}" data-asof="{e(data["as_of"] or "")}" data-landing="{e(q["landing"])}"><div class="shell">
<aside class="sidebar"><div class="brand"><span class="brand-mark" aria-hidden="true"><svg viewBox="0 0 44 44"><circle cx="22" cy="22" r="19" fill="none" stroke="currentColor"/>
<circle cx="22" cy="22" r="11" fill="none" stroke="currentColor" opacity=".5"/><circle cx="22" cy="22" r="3" fill="currentColor"/></svg></span>
<span class="brand-word"><b>{e(brand)}</b><small>COCKPIT / {e(SCHEMA.split("/")[1])}</small></span></div>
<p class="nav-title">WORKSPACE</p><nav class="nav-links" aria-label="Surfaces">{nav}</nav>
<div class="sidebar-section"><p class="nav-title">SYSTEM</p><div class="system-badges">
<div><span class="light light-good"></span>Generated from records</div><div><span class="light light-warn"></span>Integrations not connected</div>
<div><span class="light light-good"></span>Writes nothing</div></div></div>
<div class="side-quote"><p class="small-caps">OPERATING PRINCIPLE</p><p>Inquiry is not authority.<br>Capture is not commitment.</p></div>
<div class="version"><span>{e(SCHEMA)}</span><span>v{e(data["generator"]["kit_version"])}</span></div></aside>
<div class="content-shell"><header class="topbar"><div class="breadcrumb"><span>{e(brand.upper())} COCKPIT</span><span class="slash">/</span>
<span id="crumb">OVERVIEW</span></div><div class="topbar-right"><span class="offline-tag"><span class="light light-warn"></span>LOCAL-FIRST PREVIEW</span>
<span class="date-label">{snap_label}newest record {e(data["as_of"] or "(no timestamps)")} · lane {e(q["lane"] or "all")} · content {e(q["content"])}</span>
{toggle}<a class="primary small" href="#intake" data-goto="intake">+ Capture thought</a></div></header>
<main>{s_overview}{s_intake}{s_questions}{s_choices}{s_topo}{s_budget}{s_lanes}{s_run}{s_review}{s_prs}{s_status}{s_actions}</main>
<footer class="section"><p class="mini-label">WHAT THIS PAGE CAN'T TELL YOU</p><ul>{limits}</ul>{unread}
<p class="hint">{e(SCHEMA)} · relay-kit {e(data["generator"]["kit_version"])} · owner {e(data["owner"])} · data: cockpit.json beside this page ·
{e(CREDIT)}.</p>
<p class="hint"><b>Privacy:</b> this page and cockpit.json hold your records{"' text" if full else " (metadata: no bodies, but thread links, session ids, actor names and file paths remain)"}.
<code>--content metadata</code> is redaction, not publication safety. Serving it from a domain needs authentication on both
index.html and cockpit.json; <code>noindex</code> is not access control.</p></footer></div></div>
{modal}<script>{JS}</script></body></html>
"""


# ---------------- themes (HO-RK-20261105-SWK5) ----------------
FONT_VARS = {"display", "mono", "heading"}
THEME_VALUE = re.compile(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?(?:[0-9a-fA-F]{2})?|var\(--[a-z][a-z0-9-]*\)")
_FONT_ITEM = r"""\s*(?:"[\w .-]+"|'[\w .-]+'|[A-Za-z][\w-]*(?: [A-Za-z][\w-]*)*)\s*"""
FONT_VALUE = re.compile(rf"(?=.{{1,200}}$){_FONT_ITEM}(?:,{_FONT_ITEM})*")
# a neutral light palette; a theme's "light" block is laid over it, so a theme may name only what it changes
LIGHT_DEFAULTS = {
    "bg": "#f3f1ea", "sunk": "#ebe8df", "panel": "#fffdf8", "panel-2": "#f8f5ee", "field": "#f0ede5", "field-hover": "#e6e2d8",
    "border": "#cfd2cc", "border-strong": "#87949a", "border-hover": "#5b6e73", "line": "#dfdcd3",
    "text": "#182832", "text-2": "#34474e", "muted": "#4f6366", "soft": "#66777b",
    "teal": "#1f6b62", "teal-hi": "#2a8a7f", "on-teal": "#ffffff", "teal-bg": "#dcefe9", "teal-deep": "#8fc2b7",
    "gold": "#7a5710", "gold-deep": "#b89a5a", "gold-bg": "#f3ead2", "purple": "#5c4587", "purple-deep": "#a796c9",
    "purple-bg": "#ece6f5", "coral": "#a0383a", "blue": "#315f87", "blue-bg": "#e0eaf4", "ok": "#3f7a3a",
    "mark": "var(--teal)", "mark-word": "var(--text)", "focus": "#1a5690"}


def theme_vars():
    root = CSS[CSS.index(":root{") + 6:CSS.index("}")]
    return set(re.findall(r"--([\w-]+):", root))


def load_theme(path):
    """A theme file: {"name": str, "dark": {var: value}, "light": {var: value} (optional),
    "default": "auto" | "dark" | "light"}. Variables are the Cockpit's own (without --): colours as #hex or
    var(--other); display, mono and heading as font lists. Anything else is refused, so a theme can't
    carry markup or other CSS into the page."""
    try:
        t = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as x:
        raise Refused(f"--theme {path}: {x}")
    if not isinstance(t, dict):
        raise Refused(f"--theme {path}: not a JSON object")
    known, out = theme_vars(), {"name": str(t.get("name") or pathlib.Path(path).stem)[:60]}
    bad_keys = set(t) - {"name", "dark", "light", "default", "_meta"}
    if bad_keys:
        raise Refused(f"--theme {path}: unknown keys {sorted(bad_keys)}")
    for mode in ("dark", "light"):
        block = t.get(mode)
        if block is None:
            continue
        if not isinstance(block, dict):
            raise Refused(f"--theme {path}: {mode} isn't an object")
        clean = {}
        for k, v in block.items():
            if k not in known:
                raise Refused(f"--theme {path}: {mode}.{k} isn't a Cockpit variable ({', '.join(sorted(known))})")
            ok = FONT_VALUE.fullmatch(str(v)) if k in FONT_VARS else THEME_VALUE.fullmatch(str(v))
            if not isinstance(v, str) or not ok:
                raise Refused(f"--theme {path}: {mode}.{k} = {v!r} isn't a {'font list' if k in FONT_VARS else '#hex colour or var(--name)'}")
            clean[k] = v
        out[mode] = clean
    d = t.get("default", "auto")
    if d not in ("auto", "dark", "light") or (d == "light" and "light" not in out):
        raise Refused(f"--theme {path}: default must be auto, dark or light (light needs a light block)")
    out["default"] = d
    return out


THEME_EARLY = ("<script>try{var t=localStorage.getItem('cockpit-theme');if(t==='light'||t==='dark')"
               "document.documentElement.setAttribute('data-theme',t);else if(t==='auto')"
               "document.documentElement.removeAttribute('data-theme')}catch(e){}</script>")


def theme_css(t):
    if not t:
        return ""
    decl = lambda m: ";".join(f"--{k}:{v}" for k, v in sorted(m.items()))
    css = f":root{{{decl(t.get('dark') or {})};color-scheme:dark}}"
    if "light" in t:
        light = decl({**LIGHT_DEFAULTS, **t["light"]})
        css += (f"@media (prefers-color-scheme: light){{:root:not([data-theme=dark]){{{light};color-scheme:light}}}}"
                f":root[data-theme=light]{{{light};color-scheme:light}}")
    return css

CSS = """
:root{--bg:#0b1015;--sunk:#0c1418;--panel:#121b22;--panel-2:#162128;--field:#152026;--field-hover:#1d2e34;
--border:#26343d;--border-strong:#39474b;--border-hover:#59766c;--line:#203039;
--text:#f0f0e9;--text-2:#b2c5bd;--muted:#96a5a8;--soft:#6f8487;
--teal:#9bd5c8;--teal-hi:#c3ede0;--on-teal:#122820;--teal-bg:#183a33;--teal-deep:#25423a;
--gold:#dcc28c;--gold-deep:#7a6a3d;--gold-bg:#3a3220;--purple:#ac9dd7;--purple-deep:#5b4a73;--purple-bg:#2a2540;
--coral:#dfa696;--blue:#90b4d7;--blue-bg:#1a2c3d;--ok:#8fccad;--mark:var(--teal);--mark-word:var(--text);--focus:var(--teal);
--heading:var(--display);
--display:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Arial,sans-serif;--mono:ui-monospace,"SFMono-Regular",Consolas,monospace}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 var(--display);-webkit-font-smoothing:antialiased}
button,input,textarea,select{font:inherit}a{color:inherit;text-decoration:none}button{cursor:pointer}
button:focus-visible,a:focus-visible,input:focus-visible,textarea:focus-visible,select:focus-visible{outline:2px solid var(--focus);outline-offset:3px}
code{font-family:var(--mono);font-size:.9em;color:var(--teal)}h1,h2,h3,p{margin-top:0}
.shell{min-height:100vh;display:grid;grid-template-columns:248px minmax(0,1fr)}
.sidebar{height:100vh;position:sticky;top:0;display:flex;flex-direction:column;border-right:1px solid var(--line);padding:24px 16px 18px;background:var(--sunk);overflow-y:auto}
.brand{display:flex;align-items:center;gap:9px;padding:0 13px;margin-bottom:36px}.brand-mark{width:40px;height:40px;color:var(--mark);display:flex}
.brand-word b{color:var(--mark-word);font-weight:660;font-size:22px;letter-spacing:-1px;line-height:1}.brand-word small{display:block;color:var(--soft);font:9px var(--mono);letter-spacing:1.45px;margin-top:6px}
.nav-title{font:10px var(--mono);letter-spacing:1.75px;color:var(--soft);padding-left:13px;margin:0 0 10px}
.nav-links{display:flex;flex-direction:column;gap:3px}
.nav-link{display:flex;align-items:center;gap:13px;font-size:13px;color:var(--text-2);padding:11px 13px;border-radius:7px;border:1px solid transparent}
.nav-link:hover{color:var(--text);background:var(--field)}.nav-link.active{color:var(--text);background:var(--teal-bg);border-color:var(--teal-deep)}
.nav-icon{width:17px;text-align:center;font-size:16px;color:var(--muted)}.nav-link.active .nav-icon{color:var(--teal)}
.nav-kbd{margin-left:auto;font:10px var(--mono);color:var(--soft)}
.sidebar-section{margin-top:32px}.system-badges{display:grid;gap:10px;padding:5px 13px;color:var(--muted);font:11px var(--mono)}
.system-badges>div{display:flex;align-items:center;gap:9px}
.light{display:inline-block;width:6px;height:6px;border-radius:50%;flex-shrink:0}.light-good{background:var(--ok);box-shadow:0 0 9px var(--ok)}.light-warn{background:var(--gold)}
.side-quote{margin:26px 12px 0;border-top:1px solid var(--border);padding-top:18px}.small-caps,.mini-label{font:10px var(--mono);letter-spacing:1.5px;color:var(--muted);text-transform:uppercase}
.side-quote p:last-child{font-size:13px;line-height:1.6;color:var(--text-2);margin:8px 0 0}
.version{display:flex;justify-content:space-between;gap:8px;color:var(--soft);font:9px var(--mono);padding:24px 3px 0;margin-top:auto}
.content-shell{min-width:0}
.topbar{position:relative;z-index:15;min-height:64px;padding:10px 40px;display:flex;flex-wrap:wrap;gap:8px 20px;align-items:center;justify-content:space-between;
border-bottom:1px solid var(--line);background:color-mix(in srgb,var(--bg) 93%,transparent);backdrop-filter:blur(15px)}
.breadcrumb{display:flex;gap:12px;align-items:center;letter-spacing:1.1px;font:11px var(--mono);color:var(--text)}.slash{color:var(--soft)}
.topbar-right{display:flex;flex-wrap:wrap;align-items:center;gap:8px 20px}
.offline-tag{color:var(--gold);font:10px var(--mono);letter-spacing:.8px;display:flex;align-items:center;gap:8px}.date-label{color:var(--text-2);font:11px var(--mono)}
.primary,.secondary{border:1px solid transparent;display:inline-flex;align-items:center;justify-content:center;gap:6px;border-radius:5px;min-height:40px;padding:9px 16px;font-size:12px;font-weight:650;white-space:nowrap}
.primary{color:var(--on-teal);background:var(--teal)}.primary:hover{background:var(--teal-hi)}.small{min-height:36px;padding:7px 13px;font-size:12px}
.secondary{border-color:var(--border-strong);color:var(--text);background:var(--field)}.secondary:hover{background:var(--field-hover);border-color:var(--border-hover)}
.section{padding:32px 40px 8px}.js .surface[hidden]{display:none}
.sec-head{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:20px}
.eyebrow{font:10px var(--mono);letter-spacing:1.6px;color:var(--teal);text-transform:uppercase;margin-bottom:8px}.eyebrow.dark{color:var(--muted)}
.sec-head h1{font-family:var(--heading);font-size:clamp(30px,3.6vw,46px);line-height:1.1;letter-spacing:-1.8px;font-weight:580;margin:4px 0 8px}.sec-head h1 em{font-style:normal;color:var(--mark)}
.lede{color:var(--text-2);font-size:14px;max-width:620px;margin:0}
.tag{font:10px var(--mono);letter-spacing:.3px;color:var(--text-2);border:1px solid var(--border-strong);border-radius:3px;padding:5px 8px;white-space:nowrap}
.tag.hot{border-color:var(--gold-deep);color:var(--gold);background:transparent}
.panel{background:var(--panel);border:1px solid var(--border);border-radius:8px;min-width:0}.pad{padding:18px 20px;margin-bottom:16px}
.panel-head{display:flex;align-items:center;justify-content:space-between;gap:12px}.panel h2{font-size:18px;font-weight:560;letter-spacing:-.3px;margin:6px 0 12px}
.subtle-link{font-size:12px;color:var(--teal);overflow-wrap:anywhere}.subtle-link:hover{color:var(--text)}
.dials{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:16px}
.gauge{margin:0;background:var(--panel);border:1px solid var(--border);border-radius:7px;padding:12px 12px 14px;text-align:center}
.gauge svg{width:100%;max-width:170px;height:auto;display:block;margin:0 auto}
.g-track{fill:none;stroke:var(--line);stroke-width:9}.g-fill{fill:none;stroke-width:9}
.g-fill.ok{stroke:var(--teal)}.g-fill.warn{stroke:var(--gold)}.g-fill.bad{stroke:var(--coral)}.g-fill.muted{stroke:var(--soft)}
.g-ticks line{stroke:var(--soft);stroke-width:1.2}.g-needle{stroke:var(--text);stroke-width:2.5;stroke-linecap:round}.g-hub{fill:var(--text)}
.gauge figcaption{display:flex;flex-direction:column}.gauge figcaption b{font:560 26px/1.3 var(--display);letter-spacing:-1px}
.gauge figcaption span{font:10px var(--mono);letter-spacing:1px;text-transform:uppercase;color:var(--muted)}.gauge small{font-size:12px;color:var(--muted)}
b.ok{color:var(--teal)}b.warn{color:var(--gold)}b.bad{color:var(--coral)}b.muted{color:var(--muted)}
.stats-row{margin:0 0 18px;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.stat{background:var(--panel);border:1px solid var(--border);border-radius:7px;padding:16px 18px}
.stat-label{font:10px var(--mono);letter-spacing:1px;color:var(--muted);margin:0}.stat strong{display:block;font-size:24px;font-weight:560;letter-spacing:-.6px;line-height:1.5;color:var(--text);overflow-wrap:anywhere}
.stat small{font-size:12px;color:var(--muted)}
.overview-grid{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(280px,1fr);gap:16px}
.direction-list{display:flex;flex-direction:column}.direction{padding:12px 0;border-top:1px solid var(--line);display:flex;align-items:flex-start;gap:13px}
.direction-num{font:11px var(--mono);color:var(--muted);margin-top:2px;min-width:26px}.direction-body{flex:1;min-width:0}
.direction-body strong{font:600 13px var(--mono)}.direction-body p{font-size:13px;color:var(--text-2);margin:3px 0 0;line-height:1.5;overflow-wrap:anywhere}
a.direction:hover strong{color:var(--teal)}
.principle-panel{background:linear-gradient(155deg,var(--teal-bg),var(--panel) 70%)}.split-concept{display:flex;flex-direction:column;gap:12px;margin-top:10px}
.concept{border-left:2px solid var(--teal-deep);padding:1px 0 1px 15px}.concept span{font:10px var(--mono);letter-spacing:1.2px;color:var(--teal)}
.concept strong{font-size:15px;font-weight:600;display:block;margin:6px 0}.concept p{font-size:13px;color:var(--text-2);margin:4px 0 0}
.run-concept{border-left-color:var(--gold)}.run-concept span{color:var(--gold)}.concept-divider{color:var(--soft);margin-left:14px}
.principle-footer{border-top:1px solid var(--line);margin:18px 0 0;padding-top:14px;font-size:13px;color:var(--gold)}
.notwired h2{margin-top:12px}.links{padding-left:18px}
.notice{border-left:2px solid var(--gold);background:color-mix(in srgb,var(--gold) 8%,var(--bg));padding:12px 14px;border-radius:4px;margin-bottom:16px;font-size:14px;color:var(--text)}
.choices-grid{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:16px;align-items:start}.cards{display:grid;gap:10px}
.card{background:var(--panel-2);border:1px solid var(--border);border-radius:8px;padding:12px 14px;min-width:0}
.card.flash{border-color:var(--teal);box-shadow:0 0 0 2px color-mix(in srgb,var(--teal) 33%,transparent)}
.card.picked-accept{border-color:var(--teal)}.card.picked-reject{border-color:var(--coral)}.card.picked-defer{border-color:var(--soft)}
.card header{display:flex;flex-wrap:wrap;gap:8px;align-items:center}.card header b{font-family:var(--mono)}
.badge{font:10px var(--mono);text-transform:uppercase;letter-spacing:1px;padding:3px 7px;border-radius:3px;border:1px solid var(--border-strong)}
.badge.decision{color:var(--gold)}.badge.handoff{color:var(--blue)}.badge.challenge{color:var(--purple)}
.st,.date{font:12px var(--mono);color:var(--muted)}.date{margin-left:auto}
.txt{margin:8px 0;white-space:pre-wrap;overflow-wrap:anywhere}
dl{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:3px 12px;margin:6px 0;font-size:13px}dt{color:var(--muted)}dd{margin:0;overflow-wrap:anywhere}
.hint{color:var(--muted);font-size:13px;margin:6px 0}.empty{color:var(--muted);font-style:italic}
.btns{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:8px}
.act{min-height:36px;padding:7px 14px;border-radius:5px;border:1px solid var(--border-strong);background:var(--field);font:600 12px var(--mono)}
.act.accept{color:var(--teal)}.act.reject{color:var(--coral)}.act.defer{color:var(--muted)}.act:hover{border-color:var(--border-hover)}
.act.amend{color:var(--gold)}.act.overtaken{color:var(--gold);border-style:dotted;border-color:var(--gold-deep)}
.act.clarify{color:var(--purple);border-color:var(--purple-deep)}.act.clarify::after{content:" · on record";font-weight:400;color:var(--purple)}
.offrec{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:10px;padding-top:8px;border-top:1px dashed var(--line);color:var(--muted);font-size:12px}
.act.inquire{border-style:dashed;background:transparent;color:var(--blue)}.act.inquire::after{content:" · off record";font-weight:400;color:var(--muted)}
.onrec-note{color:var(--purple);font-size:12px;margin:6px 0 0}
.words{margin:8px 0 0;padding:8px 0 0;border-top:1px solid var(--line)}.words label{display:block;font-size:12px;color:var(--muted)}
.words .who{display:flex;flex-wrap:wrap;gap:8px}.words .who label{flex:1 1 180px}
.words input,.words textarea{display:block;width:100%;box-sizing:border-box;margin:3px 0 6px;min-height:0;height:auto;background:var(--sunk);color:inherit;border:1px solid var(--border-strong);border-radius:5px;padding:6px 8px;font:13px/1.4 var(--sans,inherit)}
.words .req{color:var(--gold);font-size:11px}.words-note{color:var(--gold);font-size:12px;margin-left:8px}
.editor{margin-top:8px}.editor label{display:block;font-size:12px;color:var(--muted)}
.editor textarea{display:block;width:100%;min-height:0;height:auto;resize:vertical;box-sizing:border-box;margin:4px 0 6px;background:var(--sunk);color:inherit;border:1px solid var(--border-strong);border-radius:5px;padding:8px;font:13px/1.4 var(--sans,inherit)}
.ask{border-left:3px solid var(--teal);padding:4px 0 4px 10px;margin:10px 0}
.ask .q{font-size:15px;font-weight:600;margin:4px 0 6px}.ask .effects{margin:0 0 6px;padding-left:18px;font-size:13px}
.ask .whose.derived{font-style:italic;color:var(--gold)}.ask .theirs{color:var(--purple)}.ask .dflt{font-size:12px;color:var(--muted);margin:2px 0}
.ask .proposer-options{margin:0 0 8px;padding:6px 10px;border-left:3px solid var(--purple);border-radius:0 6px 6px 0}.ask .proposer-options .effects{margin-bottom:0}
.overtaken-flag{font-size:12px;color:var(--gold);margin:6px 0}.tag.warn{color:var(--gold);border-color:var(--gold-deep)}
.apply{position:sticky;top:80px;padding:14px;border-color:var(--gold-deep)}
textarea,input,select{width:100%;background:var(--sunk);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:8px}
textarea{min-height:260px;color:var(--teal);font:12px/1.45 var(--mono);resize:vertical}input,select{margin:4px 0 12px}
#copied{font-size:12px;color:var(--teal)}
.selector{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-bottom:12px}.sel[aria-pressed=true]{border-color:var(--teal);color:var(--teal)}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:8px;background:var(--sunk);margin-bottom:12px}svg.topo{display:block}
.band{fill:var(--sunk)}.band.alt{fill:var(--panel)}.band-label{fill:var(--text-2);font:600 12px var(--mono);letter-spacing:.06em}
.n-actor{fill:var(--teal-bg);stroke:var(--teal);stroke-width:2}.n-owner{fill:var(--gold-bg);stroke:var(--gold);stroke-width:2}
.n-thread{fill:var(--blue-bg);stroke:var(--blue);stroke-width:1.5}.n-capture{fill:var(--purple-bg);stroke:var(--purple);stroke-width:1.5}
.n-label,.a-label{fill:var(--text);font:11px var(--mono);text-anchor:middle}
.e-handoff{fill:none;stroke:var(--gold);opacity:.85}.e-captured,.e-capture-of{fill:none;stroke:var(--purple);stroke-dasharray:3 3}
.m-handoff{fill:var(--gold)}.m-captured,.m-capture-of{fill:var(--purple)}
.e-count{fill:var(--gold);font:700 11px var(--mono);text-anchor:middle}
.e-count-bg{fill:var(--panel);stroke:var(--gold);stroke-width:1.5}.e-tail{fill:var(--gold)}
.band-tag{fill:var(--gold);font:600 10px var(--mono);letter-spacing:.08em;text-transform:uppercase}
.topo-grid{display:grid;grid-template-columns:1fr;gap:16px;align-items:start}
@media (min-width:1500px){.topo-grid{grid-template-columns:minmax(0,1fr) minmax(520px,600px)}.topo-grid .tw{max-height:620px;overflow:auto}}
.topo-grid td,.topo-grid th{font-size:12px;padding:5px 6px;vertical-align:top}.topo-grid table{min-width:520px}
.matrix{margin-top:10px}.mx th{font:600 11px var(--mono);text-align:left;white-space:nowrap}.mx th.rot{height:150px;vertical-align:bottom;padding:0 2px}
.mx th.rot span{display:inline-block;writing-mode:vertical-rl;transform:rotate(180deg);white-space:nowrap}.mx td.n{text-align:center;min-width:28px}.mx .tot{color:var(--gold)}.topo-grid td.n{text-align:right;font:600 12px var(--mono);white-space:nowrap}
.n-coord{fill:var(--teal-bg);stroke:var(--teal);stroke-width:2}.n-lane{fill:var(--blue-bg);stroke:var(--blue);stroke-width:1.5}
.a-structure{fill:none;stroke:var(--soft);stroke-dasharray:5 4;stroke-width:1.5}.m-structure{fill:var(--soft)}
.a-delegation{fill:none;stroke:var(--gold);stroke-width:1.8}.m-delegation{fill:var(--gold)}
.e-dec{fill:var(--gold);font:600 10px var(--mono);text-anchor:middle;paint-order:stroke;stroke:var(--sunk);stroke-width:3}
.legend{font-size:13px;color:var(--text-2)}
.actions{list-style:none;padding:0;margin:8px 0 0}.action{display:grid;grid-template-columns:22px minmax(0,1fr) auto;gap:10px;align-items:start;padding:10px 0;border-top:1px solid var(--line)}
.action .box{font-size:16px;color:var(--gold);line-height:1.2}.action.done .box{color:var(--muted)}.action.done .action-text{color:var(--text-2);text-decoration:line-through}
.action-text{margin:0;font-size:14px;line-height:1.45}.action-meta{margin:4px 0 0;font:11px var(--mono);color:var(--text-2)}.action-meta b{color:var(--gold)}.action-meta b.warn{color:var(--coral)}
.tag.needs{background:var(--gold-bg)}.done-fold{margin-top:10px}.done-fold summary{color:var(--text-2)}.action-intro{color:var(--text-2);font-size:13px}
.a-recorded{fill:none;stroke:var(--teal);stroke-width:2.2}.m-recorded{fill:var(--teal)}
.k-recorded{height:3px;width:22px;border-radius:0;background:var(--teal);vertical-align:3px}
.hyp{border:1px dashed var(--gold-deep);border-radius:8px;padding:12px;margin-top:10px;background:repeating-linear-gradient(135deg,var(--sunk),var(--sunk) 8px,var(--panel) 8px,var(--panel) 16px)}
.hyp summary{color:var(--gold);font:600 12px var(--mono);letter-spacing:.06em}.hyp[open] summary{margin-bottom:10px}
.k{display:inline-block;width:14px;height:14px;vertical-align:-2px;margin:0 4px 0 10px;border-radius:50%}.k:first-child{margin-left:0}
.k-actor{background:var(--teal-bg);border:2px solid var(--teal)}.k-owner{background:var(--gold-bg);border:2px solid var(--gold)}
.k-thread{background:var(--blue-bg);border:2px solid var(--blue);border-radius:2px}.k-capture{background:var(--purple-bg);border:2px solid var(--purple);transform:rotate(45deg);border-radius:1px;width:11px;height:11px}
.k-handoff,.k-delegation{height:3px;width:22px;border-radius:0;background:var(--gold);vertical-align:3px}
.k-captured{height:0;width:22px;border-radius:0;border-top:2px dashed var(--purple);vertical-align:3px}
.k-structure{height:0;width:22px;border-radius:0;border-top:2px dashed var(--soft);vertical-align:3px}
.tw{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px}
th{text-align:left;font:10px var(--mono);text-transform:uppercase;letter-spacing:1px;color:var(--muted);border-bottom:1px solid var(--border);padding:7px 8px}
td{border-bottom:1px solid var(--line);padding:7px 8px;vertical-align:top;overflow-wrap:anywhere}td:first-child{font-family:var(--mono);white-space:nowrap}
details{margin:6px 0}summary{cursor:pointer;font:600 13px var(--mono)}summary b{color:var(--gold)}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.budget-top{display:grid;grid-template-columns:240px minmax(0,1fr);gap:16px;align-items:start}
.principles{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin:6px 0 18px}
.pf-out{font:12px var(--mono);color:var(--gold);margin-top:12px}
.lane-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:14px}
dialog{background:var(--panel);color:var(--text);border:1px solid var(--gold-deep);border-radius:9px;max-width:560px;width:calc(100% - 32px);padding:20px}
dialog::backdrop{background:#000a}dialog pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px var(--mono);color:var(--teal);background:var(--sunk);padding:10px;border-radius:6px;max-height:40vh;overflow:auto}
footer{color:var(--muted);font-size:13px;border-top:1px solid var(--border);margin:24px 0 0;padding-bottom:30px}footer ul{padding-left:18px}
.card.pr header .subtle-link{font:600 13px var(--mono)}.pr-title{font-weight:600;margin:6px 0 4px}
.badge.pr-st{color:var(--muted)}.pr-open .badge.pr-st{color:var(--teal);border-color:var(--teal-deep)}.pr-merged .badge.pr-st{color:var(--purple)}
.pr-draft .badge.pr-st{border-style:dashed}.tag.agent{color:var(--blue);border-color:var(--blue)}.pr-secs dd{margin-bottom:6px}
.pr-secs dt{font:600 11px var(--mono);letter-spacing:1px;text-transform:uppercase;color:var(--muted)}.age.old{color:var(--gold)}
.cards+.eyebrow{margin-top:18px}.pr-src summary{font:600 12px var(--mono);color:var(--text-2)}.pr-src[open] summary{margin-bottom:10px}
.pr-src .warn{color:var(--gold)}.pr-more summary{font:600 11px var(--mono);letter-spacing:1px;text-transform:uppercase;color:var(--muted)}
.card.pr .tag{white-space:normal;overflow-wrap:anywhere}
.txt.rec p,.rv-body p,.ds-fields p,.narrative p{margin:0 0 8px}.txt.rec ol,.txt.rec ul,.rv-body ol,.rv-body ul,.ds-fields ol,.ds-fields ul,.narrative ol,.narrative ul{margin:4px 0 8px;padding-left:22px}
.txt.rec li,.rv-body li,.ds-fields li,.narrative li{margin:3px 0;line-height:1.5}.txt.rec{white-space:normal}
.rec-more summary{font:600 11px var(--mono);letter-spacing:1px;text-transform:uppercase;color:var(--muted)}
.ds{border:1px solid var(--border);border-left:3px solid var(--purple);border-radius:6px;padding:10px 12px;margin:8px 0;background:var(--panel)}
.ds.coordinator{border-left-color:var(--blue)}.ds.owner{border-left-color:var(--gold)}
.ds header{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:6px}.ds header b{font:600 12px var(--mono)}
.ds-tag{color:var(--purple);border-color:var(--purple-deep)}.ds.coordinator .ds-tag{color:var(--blue);border-color:var(--blue)}
.ds-fields{display:block;margin:0}.ds-row{display:grid;grid-template-columns:130px minmax(0,1fr);gap:2px 12px;padding:5px 0;border-top:1px solid var(--line)}
.ds-row dt{font:600 11px var(--mono);letter-spacing:.6px;text-transform:uppercase;color:var(--muted);padding-top:2px}.ds-row dd{font-size:13px}
.ds-hist summary{font:600 11px var(--mono);color:var(--muted)}.nosupport{font-size:13px}
.mech{margin:10px 0 0;padding:6px 10px;border:1px dashed var(--border-strong);border-radius:6px}.mech summary{font:600 11px var(--mono);letter-spacing:.6px;color:var(--muted);text-transform:uppercase}
.mech[open] summary{margin-bottom:6px}
.pr-reviews{margin:8px 0}.pr-reviews summary{font:600 12px var(--mono);color:var(--text-2);overflow-wrap:anywhere;white-space:normal}
.rv{border-left:2px solid var(--teal-deep);padding:6px 0 6px 12px;margin:8px 0}.rv-comment{border-left-style:dashed}
.rv-meta{font:12px var(--mono);color:var(--muted);margin:0 0 6px;overflow-wrap:anywhere}.rv-body{font-size:14px;line-height:1.6}
.findings{color:var(--gold);font:600 12px var(--mono);margin:0 0 6px}.url{color:var(--muted);font-size:.9em;overflow-wrap:anywhere}
.narrative{font-size:15px;line-height:1.65;max-width:78ch}.narrative h2{font-family:var(--heading);font-size:26px;letter-spacing:-.6px;margin:4px 0 14px}
.narrative h3{font-size:19px;font-weight:600;margin:26px 0 10px;padding-top:12px;border-top:1px solid var(--line);color:var(--teal)}
.narrative h4,.narrative h5,.narrative h6{font-size:15px;margin:16px 0 6px}.narrative li{margin:6px 0}.narrative hr{border:0;border-top:1px solid var(--border);margin:18px 0}
.narrative pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.45 var(--mono);background:var(--sunk);padding:10px;border-radius:6px}
.tag.unsigned{color:var(--coral);border-color:var(--coral);font-weight:700}.tag.items{color:var(--teal);border-color:var(--teal-deep)}
.ds-glances{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px;margin:6px 0 10px}
.ds-glance{border:1px solid var(--border);border-top:3px solid var(--purple);border-radius:6px;padding:8px 10px;background:var(--panel);font-size:13px}
.ds-glance.coordinator{border-top-color:var(--blue)}.ds-glance.owner{border-top-color:var(--gold)}.ds-glance .mini-label{margin:0 0 4px}
.attrib{font-size:14px;color:var(--text-2);margin:4px 0 10px}.status-counts{margin:0 0 6px;grid-template-columns:repeat(3,minmax(0,1fr))}
.edition{padding:24px 28px}.status-head .btns{margin:0 0 8px}.status-line{font-size:13px;color:var(--text-2);margin:-4px 0 14px}
@media (max-width:1000px){.choices-grid,.cols,.overview-grid,.budget-top{grid-template-columns:1fr}.apply{position:static;order:-1}
 .apply textarea{min-height:120px}
 .dials,.stats-row,.principles{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:760px){.shell{display:block}.sidebar{position:static;height:auto;padding:12px 16px;border-right:none;border-bottom:1px solid var(--line)}
 .brand{margin-bottom:10px;padding:0}.nav-title,.sidebar-section,.side-quote,.version{display:none}
 .nav-links{flex-direction:row;overflow-x:auto;gap:4px;padding-bottom:4px}.nav-link{padding:8px 10px;white-space:nowrap}.nav-kbd{display:none}
 .topbar{position:static;padding:10px 16px}.section{padding:20px 16px 4px}.date-label{font-size:10px}
 table,thead,tbody,tr,td{display:block}thead{display:none}tr{border-bottom:1px solid var(--border);padding:4px 0}
 td{border:none;padding:2px 4px}td::before{content:attr(data-h) ": ";color:var(--muted);font:10px var(--mono)}td:first-child{white-space:normal}
 dl{grid-template-columns:1fr}dt{margin-top:4px}.ds-row{grid-template-columns:1fr}.edition{padding:16px}}
@media (max-width:420px){.dials,.stats-row,.principles{grid-template-columns:1fr 1fr;gap:8px}.gauge figcaption b{font-size:20px}.stat strong{font-size:19px}}
"""

JS = """
(function(){
 var d=document,$=function(s){return [].slice.call(d.querySelectorAll(s))};d.documentElement.classList.add('js');
 var names={};$('.nav-link').forEach(function(a){names[a.getAttribute('data-goto')]=a.getAttribute('data-name')});
 var land=(d.body&&d.body.getAttribute&&d.body.getAttribute('data-landing'))||'overview';
 function show(n){if(!names[n])n=names[land]?land:'overview';$('.surface').forEach(function(s){s.hidden=s.id!==n});
  $('.nav-link').forEach(function(a){a.classList.toggle('active',a.getAttribute('data-goto')===n)});
  d.getElementById('crumb').textContent=names[n].toUpperCase();window.scrollTo(0,0)}
 function go(n){show(n);try{history.replaceState(null,'','#'+n)}catch(e){}}
 function flash(key){var c=d.querySelector('.card[data-key="'+key.replace(/"/g,'')+'"]');if(c){c.scrollIntoView({block:'center'});c.classList.add('flash');setTimeout(function(){c.classList.remove('flash')},1600)}}
 $('[data-goto]').forEach(function(a){a.addEventListener('click',function(ev){ev.preventDefault();go(a.getAttribute('data-goto'));
  var k=a.getAttribute('data-card');if(k)flash(k)})});
 show((location.hash||'').slice(1));
 $('.age').forEach(function(a){var t=Date.parse(a.getAttribute('data-ts'));if(isNaN(t))return;var h=(Date.now()-t)/36e5;
  a.textContent=a.getAttribute('data-ts')+' ('+(h<1?'under an hour':h<48?Math.round(h)+' h':Math.round(h/24)+' days')+' ago)';if(h>24)a.classList.add('old')});
 var tg=d.getElementById('theme-toggle');if(tg){var root=d.documentElement,modes=['auto','light','dark'],cur=tg.getAttribute('data-default');
  try{var sv=localStorage.getItem('cockpit-theme');if(modes.indexOf(sv)>=0)cur=sv}catch(e){}
  var apply=function(m){if(m==='auto')root.removeAttribute('data-theme');else root.setAttribute('data-theme',m);tg.textContent='Theme: '+m};apply(cur);
  tg.addEventListener('click',function(){cur=modes[(modes.indexOf(cur)+1)%3];apply(cur);try{localStorage.setItem('cockpit-theme',cur)}catch(e){}})}
 $('.sel').forEach(function(b){b.addEventListener('click',function(){var n=b.getAttribute('data-net');
  $('.sel').forEach(function(x){x.setAttribute('aria-pressed',x===b?'true':'false')});$('.net').forEach(function(p){p.hidden=p.getAttribute('data-net')!==n})})});
 $('.net').forEach(function(p){p.hidden=p.getAttribute('data-net')!=='info'});
 var picks={},box=d.getElementById('apply'),note=d.getElementById('copied'),dlg=d.getElementById('settle'),pending=null;
 var drawn='';box.addEventListener('input',function(){if(box.value===drawn)return;note.textContent='edited: later picks are added, your text is kept'});
 function draw(){var k=Object.keys(picks).sort();drawn=k.map(function(x){return picks[x]}).join('\\n\\n')+(k.length?'\\n':'');box.value=drawn}
 function pick(b){var k=b.getAttribute('data-key'),a=b.getAttribute('data-act'),line=b.getAttribute('data-line'),old=picks[k],msg='';
  if(box.value===drawn){picks[k]=line;draw()}
  else{var v=box.value;picks[k]=line;
   if(old!==undefined&&v.indexOf(old)>=0){v=v.replace(old,function(){return line})}
   else{v=v.replace(/\\s*$/,'');v=(v?v+'\\n\\n':'')+line+'\\n';if(old!==undefined)msg='your edited line for '+k.split(':')[1]+' is kept above'}
   box.value=v;drawn=null}
  var c=b.closest('.card');if(a!=='clarify')c.classList.remove('picked-accept','picked-reject','picked-defer','picked-amend','picked-overtaken');c.classList.add('picked-'+a);note.textContent=msg}
 function ask(eyebrow,title,body,line,ok,cb){if(!dlg||!dlg.showModal){cb();return}
  d.getElementById('settle-eyebrow').textContent=eyebrow;d.getElementById('settle-title').textContent=title;
  d.getElementById('settle-body').textContent=body;d.getElementById('settle-line').textContent=line||'';
  d.getElementById('settle-line').hidden=!line;d.getElementById('settle-ok').textContent=ok;pending=cb;dlg.returnValue='';dlg.showModal()}
 if(dlg)dlg.addEventListener('close',function(){if(dlg.returnValue==='ok'&&pending)pending();pending=null});
 function shq(s){return "'"+String(s).replace(/'/g,"'\\"'\\"'")+"'"}
 // Your words, decider and locator. Defaults live in this browser only (localStorage; never required).
 function ls(k,v){try{if(v===undefined)return localStorage.getItem('relaykit.cockpit.'+k);localStorage.setItem('relaykit.cockpit.'+k,v)}catch(e){return null}}
 var B=d.body,owner=(B&&B.getAttribute&&B.getAttribute('data-owner'))||'',asof=(B&&B.getAttribute&&B.getAttribute('data-asof'))||'';
 function now(){var t=new Date(),z=function(n){return(n<10?'0':'')+n};return t.getFullYear()+'-'+z(t.getMonth()+1)+'-'+z(t.getDate())+' '+z(t.getHours())+':'+z(t.getMinutes())}
 $('.in-decider').forEach(function(i){i.value=ls('decider')||owner;i.addEventListener('change',function(){ls('decider',i.value)})});
 $('.in-locator').forEach(function(i){i.value='Cockpit Choices, records as of '+asof+', '+now()});   // fresh each time: it carries the time
 $('.same-last').forEach(function(b){b.addEventListener('click',function(){var w=b.closest('.words'),v=ls('words');
  w.querySelector('.words-note').textContent=v?'':'no earlier words in this browser';if(v){w.querySelector('.in-words').value=v}})});
 var RQ=/<<REQUIRED[^>]*>>/g;
 function say(card,msg,el){var n=card.querySelector('.words-note');if(n)n.textContent=msg;if(el&&el.focus)el.focus();return null}
 // Only the owner's own fields are filled: decider, locator, and the words (basis, or a challenge's resolution).
 // Any other placeholder (a decision's text) comes from its own editor, never from the words.
 var OWN={decider:'decider',locator:'locator',basis:'words',resolution:'words'};
 function filled(b,card,obj){var wi=card.querySelector('.in-words'),w=((wi||{}).value||'').trim(),dec=card.querySelector('.in-decider'),loc=card.querySelector('.in-locator');
  if(!w)return say(card,'Write your words first: a settled line carries them, and the page never invents them.',wi);
  if(obj){var miss=null;Object.keys(obj).forEach(function(f){if(typeof obj[f]!=='string'||!OWN[f]||!RQ.test(obj[f]))return;RQ.lastIndex=0;
    var v=OWN[f]==='decider'?(dec&&dec.value.trim()):OWN[f]==='locator'?(loc&&loc.value.trim()):w;
    if(!v){miss=miss||f;return}obj[f]=obj[f].replace(RQ,function(){return v})});RQ.lastIndex=0;
   if(miss)return say(card,'Fill in the '+miss+' first (it is empty).',miss==='decider'?dec:loc);return obj}
  return w}
 function xp(b){var x=b.getAttribute('data-expect')||'',o='';x.split(' ').forEach(function(e){if(/^[\\w=@.-]+$/.test(e))o+='--expect '+e+' '});return o}
 function lineFor(b){var card=b.closest('.card'),t=b.getAttribute('data-template');
  if(t){var o=filled(b,card,JSON.parse(t));return o&&('relay-kit ledger append --dir governance '+xp(b)+b.getAttribute('data-kind')+' '+shq(JSON.stringify(o)))}
  var raw=b.getAttribute('data-line');if(!/--note '/.test(raw))return raw;   // a comment line: nothing to fill
  var w=filled(b,card,null);if(w===null)return null;
  w=w.split(/\\s*\\n\\s*/).join(' / ');   // handoff.py takes a one-line note
  return raw.replace(/--note '[^']*'/,function(){return '--note '+shq(w)})}
 function remember(card){var w=card.querySelector('.in-words');if(w&&w.value.trim())ls('words',w.value.trim())}
 $('button[data-line]').forEach(function(b){b.addEventListener('click',function(){var a=b.getAttribute('data-act');
  if(a==='defer'){pick(b);return}
  var line=lineFor(b);if(line===null)return;var card=b.closest('.card');
  var pk={getAttribute:function(n){return n==='data-line'?line:b.getAttribute(n)},closest:function(s){return b.closest(s)}};
  ask('SETTLE THIS CHOICE?',({accept:'Accept ',reject:'Reject ',overtaken:'Close as overtaken: '}[a]||'')+b.getAttribute('data-key').split(':')[1]+'?',
   'This adds a line to To apply with your words in it. Nothing is written until you copy it and run it yourself.',
   line,'Add to To apply',function(){remember(card);pick(pk)})})});
 $('.torun').forEach(function(b){b.addEventListener('click',function(){var k=b.getAttribute('data-card');
  ask('MOVE FROM DRIFT TO RUN?',k.split(':')[1]+' is still a proposal.','A commitment needs your explicit authorization and a concrete next action. Nothing moves from here: open its card in Choices and compose the ledger line.',
   '','Open in Choices',function(){go('choices');flash(k)})})});
 function clear(){picks={};draw();note.textContent='';
  $('.card').forEach(function(c){c.classList.remove('picked-accept','picked-reject','picked-defer','picked-amend','picked-overtaken','picked-clarify')})}
 d.getElementById('clear').addEventListener('click',function(){if(box.value===drawn){clear();return}
  var t='Discard your edits?',body='To apply holds text you edited. Clearing removes it; copy it first if you want to keep it.';
  if(!dlg||!dlg.showModal){if(window.confirm(t+' '+body))clear();return}
  ask('CLEAR TO APPLY?',t,body,'','Clear',clear)});
 // Amend and Clarify: an editor on the card; your text goes into the composed line, nothing is written
 $('[data-open]').forEach(function(b){b.addEventListener('click',function(){var c=b.closest('.card'),w=b.getAttribute('data-open');
  c.querySelectorAll('.editor').forEach(function(ed){var on=ed.getAttribute('data-for')===w&&ed.hidden;ed.hidden=!on;
   var o=c.querySelector('[data-open="'+ed.getAttribute('data-for')+'"]');if(o)o.setAttribute('aria-expanded',on?'true':'false');
   if(on){var ta=ed.querySelector('textarea');if(ta)ta.focus()}})})});
 $('.compose').forEach(function(b){b.addEventListener('click',function(){var a=b.getAttribute('data-act'),k=b.getAttribute('data-key'),
  o=JSON.parse(b.getAttribute('data-template')),f=b.getAttribute('data-field'),ed=b.closest('.editor').querySelector('textarea'),v=ed.value.trim();
  if(a==='overtaken'){if(!v)return say(b.closest('.card'),'Say what overtook it first.',ed);o[f]=o[f].replace(RQ,function(){return v});RQ.lastIndex=0}
  else if(v){o[f]=v}
  if(a==='amend'&&RQ.test(o[f])){RQ.lastIndex=0;return say(b.closest('.card'),'Write the decision text for your amendment first.',ed)}
  if(a==='amend'&&(!v||v===ed.defaultValue.trim()))return say(b.closest('.card'),'Your text is the same as the proposal: use Accept instead, or change it.',ed);
  if(a==='amend'||a==='overtaken'){o=filled(b,b.closest('.card'),o);if(!o)return}
  var line='relay-kit ledger append --dir governance '+xp(b)+b.getAttribute('data-kind')+' '+shq(JSON.stringify(o));
  var pk={getAttribute:function(n){return n==='data-key'?(a==='clarify'?k+'|question':k):n==='data-act'?a:n==='data-line'?line:null},
   closest:function(s){return b.closest(s)}};
  ask(a==='amend'?'AMEND THIS DECISION?':a==='overtaken'?'CLOSE AS OVERTAKEN?':'ASK ON THE RECORD?',
   (a==='amend'?'Adopt '+k.split(':')[1]+' with your text?':a==='overtaken'?'Close '+k.split(':')[1]+' without adopting it?':'Ask about '+k.split(':')[1]+' on the record?'),
   a==='amend'?'This adds an adopted line that supersedes the proposal, with your edited text, your words as the basis, and your decider and locator.'
    :a==='overtaken'?'This adds a superseded line: the proposal is closed and nothing is adopted. What overtook it goes in the decision text; your words are the basis.'
    :'This adds an open question (a challenge of kind question), listed under Questions until a line resolves it. Recording it delivers nothing: tell the person it is addressed to.',
   line,'Add to To apply',function(){pick(pk)})})});
 // Inquire: off the record. Copies a prompt; no line, no record, no obligation
 $('.inquire').forEach(function(b){b.addEventListener('click',function(){var t=b.getAttribute('data-prompt');
  function cp(){function done(){note.textContent='prompt copied (off record)'}
   if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(t).then(done,function(){note.textContent='select the prompt and copy it'})}else{note.textContent='select the prompt and copy it'}}
  ask('INQUIRE · OFF RECORD','Ask an assistant about '+b.getAttribute('data-key').split(':')[1]+'?',
   'Nothing is written and nobody is obliged. Paste the prompt into the coordinator or a lane; its answer is information, not a decision.',
   t,'Copy prompt',cp)})});
 d.getElementById('copy').addEventListener('click',function(){var t=box.value;function done(){note.textContent='copied'}
  function fb(){box.focus();box.select();try{d.execCommand('copy');done()}catch(e){note.textContent='select and copy by hand'}}
  if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(t).then(done,fb)}else{fb()}});
 var pf=d.getElementById('pf-go');if(pf)pf.addEventListener('click',function(){
  var rem=d.getElementById('preflight').getAttribute('data-remaining'),v=d.getElementById('pf-tokens').value,imp=d.getElementById('pf-imp').value,o=d.getElementById('pf-out');
  var est=v===''?null:Number(v),r=rem===''?null:Number(rem),route;
  if(est===null){route='UNKNOWN COST: '+(imp==='routine'?'reuse an existing session or evidence first.':'ask the coordinator for an estimate before delegating.')}
  else if(r===null){route='NO ALLOWANCE SET: the estimate cannot be compared. '+(imp==='high'?'Worth asking the owner.':'Prefer an existing session.')}
  else if(est>r){route='OVER: the estimate ('+est.toLocaleString()+') exceeds what is left this week ('+r.toLocaleString()+'). Ask the owner before spending.'}
  else if(est>r*0.5){route=(imp==='routine'?'HOLD: a routine question would take over half of what is left.':'PROCEED WITH CARE: over half of what is left; say so in the handoff.')}
  else{route='WITHIN: '+(imp==='routine'?'economy route, reuse existing context.':'proceed; record the actual use in budget.jsonl.')}
  o.textContent='ADVISORY · '+route});
})();
"""


# ---------------- writing ----------------
def build(root, out, lane=None, content="full", weekly_tokens=None, drift_days=None, coordinator=None,
          allow_public_dest=False, brand=None, pr_state=False, pr_lookup=None, theme=None, landing=None, actions=None):
    root, out = pathlib.Path(root), pathlib.Path(out)
    theme = load_theme(theme) if theme else None
    if not root.is_dir():
        raise Refused(f"--root {root} isn't a folder")
    ex.check_destination(out, root, allow_public_dest)   # the export's guard: not in the records, not in a public repo
    o = out.resolve()
    if o.exists():
        if not o.is_dir():
            raise Refused(f"--out {o} exists and isn't a folder")
        foreign = sorted(x.name for x in o.iterdir() if x.name not in OUT_FILES)
        if foreign:
            raise Refused(f"--out {o} holds other files ({', '.join(foreign[:5])}); give a new or empty folder, "
                          "or one holding only a previous cockpit")
    fetched = None
    if pr_state:   # opt-in, build time only: the page itself never touches the network
        pr_lookup = pr_lookup or gh_pr_state
        fetched = iso(dt.datetime.now(dt.timezone.utc))
        if content != "full":   # no text is read for references in metadata mode, so nothing is looked up
            pr_state, pr_lookup, fetched = False, None, None
            print("note: --pr-state is ignored with --content metadata (no text is read, so no references are found)",
                  file=sys.stderr)
    data = collect(root, lane=lane, content=content, weekly_tokens=weekly_tokens, drift_days=drift_days,
                   coordinator=coordinator, brand=brand, pr_lookup=pr_lookup if pr_state else None, pr_fetched_at=fetched,
                   landing=landing, actions=actions)
    if theme:
        data["theme"] = theme
        data["query"]["theme"] = theme["name"]
    page = render(data)
    o.mkdir(parents=True, exist_ok=True)
    for name, text in (("cockpit.json", json.dumps(data, ensure_ascii=False, sort_keys=True, indent=1) + "\n"),
                       ("index.html", page)):
        fd, tmp = tempfile.mkstemp(dir=o, prefix=".cockpit-")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, o / name)
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(prog="relay-kit cockpit", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True, type=pathlib.Path, help="the private records folder to read")
    ap.add_argument("--out", required=True, type=pathlib.Path, help="destination folder (new, or holding a previous cockpit)")
    ap.add_argument("--lane", help="lane name or code; default all lanes")
    ap.add_argument("--content", choices=["full", "metadata"], default="full",
                    help="metadata leaves out decision, challenge and handoff text (ids, states and actors only)")
    ap.add_argument("--weekly-tokens", type=int, help="weekly token allowance for the budget gauge")
    ap.add_argument("--drift-days", type=int, help="flag proposed decisions older than this (default 3, or governance/cockpit.json)")
    ap.add_argument("--coordinator", help="the coordinator lane (default: the lane whose name contains 'coord')")
    ap.add_argument("--brand", help="name shown in the header (default relay-kit, or 'brand' in governance/cockpit.json)")
    ap.add_argument("--pr-state", action="store_true",
                    help="also look up, with the gh CLI at build time, pull requests choices name that governance/pr-state.jsonl "
                         "doesn't hold (relay-kit prs is the reader; this fills gaps). Off by default: without it nothing "
                         "touches the network")
    ap.add_argument("--theme", type=pathlib.Path, help="a theme file (JSON: dark and optional light colour variables); see docs/cockpit.md")
    ap.add_argument("--landing", help="the surface the page opens on (default overview, or 'landing' in governance/cockpit.json), e.g. prs")
    ap.add_argument("--actions", type=pathlib.Path,
                    help="the human action list to render on Actions (a markdown checklist, relay-kit/actions@1); "
                         "default: every actions/*.md in the records store")
    ap.add_argument("--allow-public-dest", action="store_true",
                    help="write even inside a git repository that is public or whose visibility can't be confirmed")
    a = ap.parse_args(argv)
    try:
        d = build(a.root, a.out, lane=a.lane, content=a.content, weekly_tokens=a.weekly_tokens,
                  drift_days=a.drift_days, coordinator=a.coordinator, allow_public_dest=a.allow_public_dest,
                  brand=a.brand, pr_state=a.pr_state, theme=a.theme, landing=a.landing, actions=a.actions)
    except Refused as err:
        sys.exit(f"refused: {err}")
    c = d["counts"]
    print(f"cockpit written to {pathlib.Path(a.out).resolve()}/index.html ({SCHEMA}), records as of {d['as_of']}: "
          f"{c['choices']} waiting on the owner, {c['authorized_in_progress']} authorized handoffs in progress "
          f"({c['pending_authorization']} pending authorization), {c['drift_flags']} drift flags, "
          f"budget {'present' if d['budget']['present'] else 'absent'}")


if __name__ == "__main__":
    main()
