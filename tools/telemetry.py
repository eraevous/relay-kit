#!/usr/bin/env python3
"""relay-kit/telemetry@2: a derived view of how the work is going, rebuilt from the records every time
and never entered by hand. Standard library only; reads the records, writes nothing to them, and
never calls GitHub (pull requests come from governance/pr-state.jsonl, written by relay-kit prs).

    relay-kit telemetry --root RECORDS [--out FILE]      # prints, or writes, the view as JSON

Every metric carries its frozen definition, a version, a label (measured / proxy / estimate, as its
inputs are), its denominator, and "unknown" with a reason where the records can't say: missing is
never zero. Distributions are reported (median, p90, n), never a composite score and never a target.

Metrics (docs/telemetry.md):
  compute_consumption     budget@2 lines, corrections and child runs applied, by method and category
  delivery_throughput     merged PRs per ISO calendar week, by repository; abandoned (closed unmerged) apart
  lead_time               PR created -> merged, hours: median, p90, n (wall clock, never work time)
  review_burden           change-request review submissions per reviewed merged PR; review coverage
  post_merge_remediation  share of merged PRs with an explicitly linked revert or fix within 14 and 30
                          days, counted only for cohorts at least that old (not production CFR)
  human_intervention      self-reported active minutes; unknown otherwise
  surviving_changes       merged PRs not reverted within 14 days (cohort at least 14 days old)
  measurement_coverage    which actors, sessions, PRs and repositories have trustworthy telemetry
  idea_to_completed_handoff_latency  hours from a recorded 'Idea captured: <time>' (Task or Context section) to
                          the handoff's first completed step: DEC-162's progress proxy, measured only when
                          both exist; invalid, future or reversed markers excluded by name
  coordination_loss       stale handoffs, cancelled handoffs, overlapping in-progress claims, ledger
                          transition conflicts
"""
import argparse, datetime as dt, importlib.util, json, pathlib, re, sys

SCHEMA = "relay-kit/telemetry@2"
HERE = pathlib.Path(__file__).resolve().parent
PRS_FILE = "governance/pr-state.jsonl"
# prs@3 adds kind=comment issue comments to `reviews`; telemetry reads @3 but keeps only review submissions
# (kind=review, or no kind in @1/@2), so review_burden's /pulls/{n}/reviews boundary holds.
PRS_READS = {"relay-kit/prs@1", "relay-kit/prs@2", "relay-kit/prs@3", "relay-kit/prs@4"}   # @4 adds comments and review_requested only
FIX = re.compile(r"\b(?:revert(?:s|ed)?|hotfix|fix(?:es|ed)?|regression)\b", re.I)


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ex = _load("relaykit_export_for_telemetry", "export.py")
BG = _load("relaykit_budget_for_telemetry", "budget.py")
LC = _load("relaykit_lifecycle_for_telemetry", "lifecycle.py")


def t(s):
    """An ISO time with a zone as UTC; a time without a zone isn't guessed at."""
    try:
        d = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return d.astimezone(dt.timezone.utc) if d.tzinfo else None


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ") if d else None


def dist(values):
    """median, p90 (nearest rank), n: never a mean."""
    v = sorted(values)
    if not v:
        return {"n": 0, "median": None, "p90": None}
    rank = lambda q: v[max(0, min(len(v) - 1, -(-int(q * len(v) * 100) // 100) - 1))]
    mid = (v[(len(v) - 1) // 2] + v[len(v) // 2]) / 2
    return {"n": len(v), "median": round(mid, 2), "p90": round(rank(0.9), 2), "min": round(v[0], 2), "max": round(v[-1], 2)}


def metric(name, definition, label, version=1, **body):
    return {"metric": name, "definition": definition, "label": label, "version": f"{name}@{version}", **body}


def read_prs(root):
    p = pathlib.Path(root) / PRS_FILE
    if not p.is_file() or p.is_symlink():
        return None
    latest, fetches, cover, bad = {}, {}, {}, 0
    for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            x = json.loads(ln) if ln.strip() else None
        except ValueError:
            bad += 1
            continue
        if x is None:
            continue
        if not isinstance(x, dict) or x.get("schema") not in PRS_READS:
            bad += 1
            continue
        if x.get("kind") == "pr" and isinstance(x.get("number"), int):
            if isinstance(x.get("reviews"), list):   # review submissions only; prs@3 issue comments never count
                x = dict(x, reviews=[r for r in x["reviews"] if isinstance(r, dict) and r.get("kind", "review") == "review"])
            latest[(x.get("repo"), x["number"])] = x   # file order: the last readable record of each PR, any schema
        elif x.get("kind") == "fetch":
            for r in x.get("ok") or []:
                fetches[r] = x.get("ts")
                c = (x.get("coverage") or {}).get(r) if isinstance(x.get("coverage"), dict) else None
                cover[r] = {"since_days": x.get("since_days"), "more_available": (c or {}).get("more_available"),
                            "partial_reads": len((c or {}).get("partial") or [])}
    return {"latest": latest, "fetches": fetches, "coverage": cover, "unreadable": bad}


def session_of(actor):
    """'claude:aec#01ABC' -> ('claude:aec', '01ABC'); the session is what is counted, not the spelling."""
    a = str(actor or "").split()[0] if actor else ""
    lane, _, sess = a.partition("#")
    return lane, sess or None


def remediation_link(y, x):
    """y explicitly reverts or fixes x: a fix verb right before x's reference in y's title or sections,
    or a 'Reverts owner/repo#N' line. References to another repository don't count."""
    n, repo = x["number"], re.escape(str(x.get("repo") or ""))
    ref = rf"(?:(?<![\w/.-]){repo}#{n}|(?<![\w/#.-])#{n}|github\.com/{repo}/pull/{n})(?!\d)"
    verb = r"(?:revert(?:s|ed)?|hotfix(?:es|ed)?(?:\s+for)?|fix(?:es|ed)?(?:\s+(?:for|(?:the\s+|a\s+)?regression\s+(?:from|in|introduced\s+(?:by|in))))?)"
    text = " ".join([str(y.get("title") or "")] + [str(v) for v in (y.get("body_sections") or {}).values()])
    return bool(re.search(rf"\b{verb}\s+{ref}", text, re.I) or re.search(rf"^\s*Reverts\s+{ref}", text, re.I | re.M))


def capture_marker(text):
    """The value of an 'Idea captured: <time>' line in a handoff's Task or Context section, or None. Other
    sections (Expected output, Log, Proposals, ...) don't count, whatever they say."""
    sections, head = {}, None
    for ln in text.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", ln)
        if m:
            head = m.group(1).strip().lower()
            continue
        if head in ("task", "context"):
            sections.setdefault(head, []).append(ln)
    for head in ("task", "context"):
        for ln in sections.get(head, []):
            m = re.match(r"^[ \t]*(?:[-*][ \t]*)?Idea captured:[ \t]*(\S+)", ln, re.I)
            if m:
                return m.group(1)
    return None


def derive(root, lane=None, content="full", as_of=None):
    """The view. lane: only that lane's handoffs, sessions and budget lines (pull requests are per
    repository, not per lane, and are all shown). content='metadata': nothing derived from free text
    (no PR body or handoff text is read; those figures are unknown). as_of: the time to measure ages
    from (the cockpit passes its own, so the page and the panel agree)."""
    root = pathlib.Path(root)
    full = content == "full"
    rd = ex.Reader(root)
    rd.config()
    led = ex.load_ledger()
    _, decisions = ex.ledger_records(rd, "decisions.jsonl", "decision")
    _, challenges = ex.ledger_records(rd, "challenges.jsonl", "challenge")
    _, handoffs, _ = ex.handoff_records(rd, led)
    in_lane = lambda r: lane is None or any(rd.lanes.matches(lane, l) for l in (r.get("lanes") or [r.get("lane")]) if l)
    actor_in_lane = lambda a: lane is None or bool(rd.lanes.lane(session_of(a)[0]) and rd.lanes.matches(lane, rd.lanes.lane(session_of(a)[0])))
    handoffs = [h for h in handoffs if in_lane(h)]
    prs = read_prs(root)
    bpath = root / "governance" / "budget.jsonl"
    brows, bbad = BG.parse(bpath.read_text(encoding="utf-8")) if bpath.is_file() and not bpath.is_symlink() else ([], [])
    invalid = [{"line": n, "reasons": BG.row_problems(o)} for n, _, o in brows if BG.row_problems(o)] + \
              [{"line": n, "reasons": [r]} for n, r in bbad]
    brows = [(n, raw, o) for n, raw, o in brows if not BG.row_problems(o)]      # the lines every reader accepts
    kept4, corr = BG.effective(brows) if brows else ([], {"replaced": {}, "children": {}, "conflicts": [], "invalid": [], "duplicates": [], "methods": {}})
    kept4 = BG.exclude_children(kept4, corr)            # all-time view: a child inside a counted parent isn't added again
    kept = [(n, i, dict(o, ts=at or o.get("ts"))) for n, i, o, at in kept4 if actor_in_lane(o.get("actor"))]

    # as of: given by the caller, else the newest record time (never the clock, and never a pr-state
    # fetch time, which is when the reader happened to run); times past the newest fetch are flagged
    future = []
    if as_of:
        now = t(as_of)
    else:
        stamps = [t(e["at"]) for h in handoffs for e in h["log"]] + [t(o.get("ts")) for _, _, o in kept]
        stamps += [t(x.get("merged_at") or x.get("updated_at")) for x in (prs or {"latest": {}})["latest"].values()]
        last_fetch = max((t(v) for v in (prs or {"fetches": {}})["fetches"].values() if t(v)), default=None)
        if last_fetch:
            future = [iso(s) for s in stamps if s and s > last_fetch + dt.timedelta(days=1)]
            stamps = [s for s in stamps if s and s <= last_fetch + dt.timedelta(days=1)]
        stamps = [s for s in stamps if s]
        if not stamps:
            stamps = [t(f"{r['fields'].get('date')}T23:59:59Z") for r in decisions + challenges if r["fields"].get("date")]
            stamps = [s for s in stamps if s]
        now = max(stamps) if stamps else None
    out = {"schema": SCHEMA, "as_of": iso(now), "query": {"lane": lane, "content": content},
           "derived_from": {"budget": "governance/budget.jsonl (relay-kit/budget@2)" if bpath.is_file() else None,
                            "pull_requests": PRS_FILE + " (relay-kit/prs@1-3; review submissions only)" if prs else None,
                            "handoffs": "handoffs/ (relay-kit/handoff@2)", "ledgers": "governance/*.jsonl (relay-kit/lifecycle@1)"},
           "notes": ["A derived view: rebuild it, don't edit it.",
                     "No composite score and no targets: DEC-100's '2-3 points' needs a baseline that doesn't exist yet.",
                     "A merged pull request is not a production deployment; remediation here is not DORA's Change Failure Rate."]
           + ([f"Times after the last pull-request read were left out of as_of: {', '.join(future[:5])}"] if future else []),
           "metrics": []}
    M = out["metrics"]

    # ---- compute consumption ----
    cats = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}
    by_method, by_actor, days, lines_, incomplete = {}, {}, set(), 0, 0
    for _, _, o in kept:
        tot = o["tokens_in"] + o["tokens_out"]
        if o.get("window") != "session" or tot == 0:     # a minutes-only line isn't compute
            continue
        lines_ += 1
        m = o.get("method") if o.get("method") in BG.METHODS else "unlabelled"
        by_method[m] = by_method.get(m, 0) + tot
        by_actor.setdefault(str(o.get("actor")), {}).setdefault(m, 0)
        by_actor[str(o.get("actor"))][m] += tot
        d = t(o.get("interval_end") or o.get("ts"))
        if d:
            days.add(d.date().isoformat())
        incomplete += 1 if o.get("complete") is False else 0
        td = o.get("tokens_detail")
        if m == "measured" and isinstance(td, dict):
            for k in cats:
                cats[k] += int(td.get(k) or 0)
    labels = set(by_method)
    mixed_actors = sorted(a for a, ms in by_actor.items() if "measured" in ms and len(ms) > 1)
    M.append(metric("compute_consumption",
                    "Input and output tokens per uniquely identified execution interval (budget@2 session lines, corrections "
                    "and included child runs applied), by how each was measured; cache reads apart, from measured lines' "
                    "tokens_detail. Unattributed use stays unattributed.", "+".join(sorted(labels)) if labels else "unknown",
                    tokens_by_method=dict(sorted(by_method.items())), tokens_by_actor=dict(sorted(by_actor.items())),
                    categories_from_measured_lines=cats, lines=lines_, incomplete_lines=incomplete, active_days=len(days),
                    corrections_applied=len(corr["replaced"]), correction_conflicts=corr["conflicts"],
                    budget_lines_taking_no_effect=corr["invalid"], invalid_lines=invalid,
                    possible_double_count_actors=mixed_actors,
                    unknown=None if by_method else "no budget lines"))

    # ---- pull-request metrics (per repository; not lane-filtered) ----
    items = list((prs or {"latest": {}})["latest"].values())
    merged = [x for x in items if x.get("state") == "merged" and t(x.get("merged_at"))]
    abandoned = [x for x in items if x.get("state") == "closed" and not x.get("merged_at")]
    weeks = {}
    for x in merged:
        y, w, _ = t(x["merged_at"]).isocalendar()
        k = f"{y}-W{w:02d}"
        weeks.setdefault(k, {}).setdefault(x["repo"], 0)
        weeks[k][x["repo"]] += 1
    cover = (prs or {"coverage": {}})["coverage"]
    M.append(metric("delivery_throughput", "Merged pull requests per ISO calendar week (UTC), by repository; abandoned (closed, "
                    "not merged) counted apart. A proxy for delivered value. Only as complete as the reads: see read_window.", "proxy",
                    merged_per_week=dict(sorted(weeks.items())), merged=len(merged), abandoned=len(abandoned),
                    read_window={r: c for r, c in sorted(cover.items())},
                    unknown=None if prs else "no governance/pr-state.jsonl: run relay-kit prs"))
    lt, no_created = [], 0
    for x in merged:
        c = t(x.get("created_at"))
        if c:
            lt.append((t(x["merged_at"]) - c).total_seconds() / 3600)
        else:
            no_created += 1
    M.append(metric("lead_time", "Wall-clock hours from pull request creation to merge (GitHub timestamps). Never human work time.",
                    "measured", hours=dist(lt), excluded={"no created_at": no_created} if no_created else {},
                    unknown=None if lt else "no merged pull requests recorded"))
    # review_burden@3 (@2 had rounds, and counted reviewed/coverage differently: see docs/telemetry.md). A merged PR is in exactly one of three review states:
    #   reviewed   - at least one GitHub review submission is recorded (kept even if the last read failed);
    #   unreviewed - reviews were read and none exist (known absence);
    #   unknown    - reviews weren't read and none are retained (absence NOT known).
    # The submissions distribution uses only reviewed PRs whose reviews were read, so it is never
    # computed from stale or missing data. Source boundary: prs reads /pulls/{n}/reviews only.
    CR = ("REQUEST CHANGES", "REQUESTING CHANGES", "CHANGES REQUESTED")
    subs, reviewed, unreviewed, unknown_, stale = [], 0, 0, 0, 0
    for x in merged:
        unread_rev = "reviews" in (x.get("unread") or [])
        rs = x.get("reviews") or []
        if rs:
            reviewed += 1
            if unread_rev:
                stale += 1              # present but possibly incomplete: not sampled for submissions
                continue
            subs.append(sum(1 for r in rs if r.get("verdict") == "CHANGES_REQUESTED" or str(r.get("stated") or "").upper() in CR))
        elif unread_rev:
            unknown_ += 1
        else:
            unreviewed += 1
    excl = {k: v for k, v in (("review data unread", unknown_), ("no review submission recorded", unreviewed),
                              ("reviews retained from an earlier read, not sampled", stale)) if v}
    if not merged:
        why = "no merged pull requests recorded"
    elif not subs:
        why = ("review data unread for every merged pull request" if unknown_ == len(merged)
               else "no merged pull request has a readable GitHub review submission")
    else:
        why = None
    M.append(metric("review_burden", "Change-request review submissions per merged pull request that has a recorded GitHub "
                    "review submission (GitHub's CHANGES_REQUESTED, or a verdict stated at the start of a review submission's "
                    "body). Submissions, not revision rounds: two reviewers requesting changes on one head count twice. "
                    "Merged pull requests are split into reviewed / unreviewed (reviews read, none) / unknown (reviews unread): "
                    "unknown is never counted as unreviewed or as zero. Source: /pulls/{n}/reviews only; ordinary PR comments, "
                    "inline comments and reviews held outside GitHub aren't captured. Denominator: all merged pull requests "
                    "retained in the observed pr-state cohort (the latest record of each PR across every pr-state read), not "
                    "repository history. read_window is metadata about each repository's latest fetch, not an exact cohort "
                    "boundary: a PR seen only by an earlier read stays in the cohort. The submissions distribution covers only "
                    "the reviewed PRs whose reviews were readable (n); reviewed PRs with retained but unread reviews are "
                    "excluded from it (see excluded).",
                    "measured", version=3, submissions=dist(subs), reviewed=reviewed, unreviewed=unreviewed,
                    review_state_unknown=unknown_, merged=len(merged),
                    coverage=round(reviewed / len(merged), 3) if merged and not unknown_ else None,
                    coverage_lower_bound=round(reviewed / len(merged), 3) if merged else None,
                    read_window={r: c for r, c in sorted(cover.items())}, excluded=excl, unknown=why))

    rem = {}
    for days_ in (14, 30):
        cohort = [x for x in merged if now and t(x["merged_at"]) <= now - dt.timedelta(days=days_)]
        if not full:
            rem[f"{days_}d"] = {"cohort": len(cohort), "remediated": None, "share": None,
                                "unknown": "--content metadata: pull-request text isn't read"}
            continue
        hit = sum(1 for x in cohort if any(
            y is not x and y.get("repo") == x.get("repo") and t(x["merged_at"]) < t(y["merged_at"]) <= t(x["merged_at"]) + dt.timedelta(days=days_)
            and remediation_link(y, x) for y in merged))
        rem[f"{days_}d"] = {"cohort": len(cohort), "remediated": hit,
                            "share": round(hit / len(cohort), 3) if cohort else None,
                            "unknown": None if cohort else f"no merged pull request is {days_} days old yet"}
    M.append(metric("post_merge_remediation", "Share of merged pull requests with an explicitly linked later revert or fix "
                    "within 14 and 30 days, counted only over cohorts at least that old. Linked means: in the same repository, "
                    "a later merged pull request whose title or sections say revert/fix/hotfix immediately before this one's "
                    "reference (#N, owner/repo#N or its URL), or 'Reverts owner/repo#N'. An internal quality proxy, not "
                    "production Change Failure Rate.", "proxy", windows=rem))
    surv = rem["14d"]
    M.append(metric("surviving_changes", "Merged pull requests not reverted or fixed by an explicitly linked change within 14 "
                    "days (DEC-123's unit of output; '>50% rewritten' isn't detectable from pr-state and isn't applied).",
                    "proxy", count=(surv["cohort"] - surv["remediated"]) if surv["cohort"] and surv["remediated"] is not None else None,
                    cohort=surv["cohort"], unknown=surv["unknown"]))
    hm = [(o.get("human_minutes"), o.get("method") if o.get("method") in BG.METHODS else "unlabelled") for _, _, o in kept
          if isinstance(o.get("human_minutes"), int) and not isinstance(o.get("human_minutes"), bool) and o["human_minutes"] >= 0]
    M.append(metric("human_intervention", "Active owner minutes recorded on budget lines (human_minutes), labelled as those lines "
                    "are: only a line the owner wrote as active time is that; a merge-window span is an upper-bound proxy. "
                    "Unknown otherwise; never inferred from session spans.",
                    "+".join(sorted({m for _, m in hm})) if hm else "unknown",
                    minutes_recorded=sum(v for v, _ in hm) if hm else None, lines=len(hm),
                    unknown=None if hm else "no line records human_minutes"))

    # ---- measurement coverage ----
    sessions = {}
    for h in handoffs:
        for e in h["log"]:
            ln, ss = session_of(e.get("actor"))
            if ":" in ln and ss and actor_in_lane(e.get("actor")):    # --lane: that lane's own sessions only
                sessions.setdefault(ss, ln)
    measured = {session_of(o.get("actor"))[1] for _, _, o in kept if o.get("method") == "measured"}
    anyline = {session_of(o.get("actor"))[1] for _, _, o in kept}
    M.append(metric("measurement_coverage", "Which agent sessions named in the handoffs (counted by session id) have a measured or "
                    "any budget line, budget lines by method, and how complete the pull-request reads were. Missing means "
                    "unmeasured, never zero.", "derived",
                    agent_sessions_in_handoffs=len(sessions), with_measured_budget_line=len([s for s in sessions if s in measured]),
                    with_any_budget_line=len([s for s in sessions if s in anyline]),
                    unmeasured_sessions=sorted(f"{l}#{s}" for s, l in sessions.items() if s not in measured),
                    budget_lines_by_method=corr["methods"], invalid_budget_lines=len(invalid),
                    repositories_read=sorted((prs or {"fetches": {}})["fetches"]),
                    repositories_with_more_than_read=sorted(r for r, c in cover.items() if c.get("more_available")),
                    pr_lines=len(items), pr_lines_with_unread=len([x for x in items if x.get("unread")]),
                    not_meterable=["ChatGPT and Codex cloud sessions (no exposed metering; estimates from captures, "
                                   "Kairos threads only, when recorded)"]))

    # ---- idea-to-completed-handoff latency (DEC-162's operational proxy) ----
    # Capture time: a "Idea captured: <ISO time>" line in the handoff's Task or Context section only (a recorded
    # time, carried forward by hand; not checked against the intake source). Completion: the handoff's first
    # `completed` step, which handoff.py records once an output file exists; not merged, used or deployed, and
    # the definition says so. A case needs both; a marker that doesn't parse, lies in the future or follows the
    # completion is excluded under its own name, never read as "no marker". The unit is a handoff carrying a
    # marker, so one idea carried by two handoffs is two cases. In --content metadata the text isn't used.
    cases, in_flight, no_marker, bad = [], [], 0, {"invalid capture time": [], "capture time in the future": [],
                                                   "capture time after completion": []}
    if full:
        for h in handoffs:
            m = capture_marker(h.get("text") or "")
            done = min((t(e["at"]) for e in h["log"] if e.get("state") == "completed" and t(e["at"])), default=None)
            if m is None:
                no_marker += 1 if done else 0
                continue
            cap = t(m)
            if cap is None:
                bad["invalid capture time"].append(h["handoff_id"])
            elif now and cap > now:
                bad["capture time in the future"].append(h["handoff_id"])
            elif done and cap > done:
                bad["capture time after completion"].append(h["handoff_id"])
            elif done:
                cases.append({"handoff": h["handoff_id"], "captured_at": iso(cap), "completed_at": iso(done),
                              "hours": round((done - cap).total_seconds() / 3600, 2)})
            elif h["status"] not in led.FINAL:
                in_flight.append({"handoff": h["handoff_id"], "captured_at": iso(cap),
                                  "open_hours": round((now - cap).total_seconds() / 3600, 2) if now else None})
    excluded = {k: v for k, v in bad.items() if v}
    if no_marker:
        excluded["completed without a capture marker"] = no_marker
    M.append(metric("idea_to_completed_handoff_latency", "Hours from a recorded idea-capture time to the first completed handoff "
                    "carrying it: the 'Idea captured: <ISO time>' line in the handoff's Task or Context section (a time written "
                    "by hand from the intake entry, not verified against it) to the handoff's first completed step, which exists "
                    "once an output file does. Completion is not merged, accepted, deployed or used, so this is a progress proxy "
                    "for DEC-162's idea-to-realization latency, not that latency. The unit is a handoff carrying a marker (one "
                    "idea in two handoffs is two cases). Markers that don't parse, lie in the future or follow completion are "
                    "excluded by name; handoffs without a marker aren't cases.",
                    "measured" if cases else "unknown", hours=dist([c["hours"] for c in cases]), cases=cases,
                    in_flight=in_flight, excluded=excluded,
                    unknown=(None if cases else ("--content metadata: handoff text isn't used" if not full
                                                 else "no completed handoff carries an 'Idea captured:' time"))))

    stale, open_ = [], []
    for h in handoffs:
        if h["status"] in led.FINAL:
            continue
        last = max((t(e["at"]) for e in h["log"] if t(e["at"])), default=None)
        open_.append(h)
        if now and last and now - last > dt.timedelta(hours=24):
            stale.append({"id": h["handoff_id"], "status": h["status"], "last_step": iso(last)})
    cancelled = [h["handoff_id"] for h in handoffs if h["status"] == "cancelled"]
    claims = {}
    if full:
        for h in open_:
            if h["status"] not in ("acknowledged", "in_progress"):
                continue
            m = re.search(r"^touches:[ \t]*(.+)$", h.get("text") or "", re.M)
            for part in (m.group(1).split(",") if m else []):
                claims.setdefault(part.strip(), []).append(h["handoff_id"])
    overlaps = {k: v for k, v in claims.items() if k and len(v) > 1}
    res = LC.resolve([r["fields"] for r in decisions], [r["fields"] for r in challenges])
    M.append(metric("coordination_loss", "Stale handoffs (open, no step in 24 h before as_of), cancelled handoffs, handoffs in "
                    "progress that claim the same path, and ledger transitions in conflict. Duplicate work and the time spent "
                    "reconciling aren't detected yet: unknown.", "derived", stale_handoffs=stale, cancelled_handoffs=cancelled,
                    overlapping_claims=overlaps if full else None,
                    overlapping_claim_count=len(overlaps) if full else None,
                    ledger_conflicts=[c["id"] for c in res["conflicts"]],
                    reconciliation_minutes=None,
                    unknown="duplicate work and reconciliation time aren't recorded"
                            + ("; claims come from handoff text, not read in --content metadata" if not full else "")))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="relay-kit telemetry", description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", required=True, type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path, help="write the view here (outside the records) instead of printing it")
    ap.add_argument("--lane", help="only this lane's handoffs, sessions and budget lines (pull requests stay per repository)")
    ap.add_argument("--content", choices=("full", "metadata"), default="full",
                    help="metadata: read no free text; figures that need it are unknown")
    ap.add_argument("--as-of", help="measure ages from this ISO time (UTC) instead of the newest record")
    a = ap.parse_args(argv)
    if a.as_of and not t(a.as_of):
        sys.exit("refused: --as-of must be an ISO time with a zone")
    try:
        view = derive(a.root, lane=a.lane, content=a.content, as_of=a.as_of)
    except ex.Refused as x:
        sys.exit(f"refused: {x}")
    text = json.dumps(view, ensure_ascii=False, sort_keys=True, indent=1) + "\n"
    if a.out:
        if a.out.resolve().is_relative_to(a.root.resolve()):
            sys.exit("refused: --out is inside the records; the view is derived, not a record")
        a.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
