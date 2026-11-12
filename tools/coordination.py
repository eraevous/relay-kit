#!/usr/bin/env python3
"""Coordination surface for the Cockpit (relay-kit/cockpit@7): where the owner's attention went and where work
waited between the owner's turns. Two halves, both rebuilt from the records on every build:

  Capture half   chat-telemetry's own output (the telemetry.json its script writes) saved in the records
                 under telemetry/chat/*.json. This module reads that output; it never re-measures a
                 capture, so chat-telemetry stays the one place that turns a capture into timings.
                 From its per-turn rows it draws: a turn timeline across threads, the owner's reply gaps
                 (bands, and the median of engaged gaps under 15 minutes), turns by local hour, message
                 length per turn, a per-thread table, hangs and the longest silences, and governed vs
                 autonomous (owner turns vs relayed turns, and the "stretches" of relayed turns between
                 two owner turns).
  Ledger half    the coordination numbers relay-kit/telemetry@2 already derives (coordination_loss,
                 idea_to_completed_handoff_latency, measurement_coverage), shown as they are.

Rules: missing is "unknown" with a reason, never zero; owner-turn counts are a lower bound unless the
records show every session in the window was captured (they can't today, CH-033); the independent
ledger count of the owner's decision and challenge lines in the same days is shown beside it; wall-clock
gaps are not effort. The owner's words are never carried onto the page (chat-telemetry's previews are
dropped). Standard library only; no clock is read."""
import html, json, math, pathlib, statistics

CHAT_DIR = "telemetry/chat"
SHAPE = {"threads", "engaged_seconds", "bands"}   # chat-telemetry's telemetry.json carries no schema id; read by shape
BANDS = ["live", "short break", "stepped away", "came back later"]
TURN_KEYS = ["n", "start", "local_hour", "local_weekday", "relay", "user_words", "assistant_words", "tool_steps",
             "reply_latency_s", "turn_duration_s", "longest_silence_s", "answered", "return_gap_s", "return_band"]
LEDGER_METRICS = ["coordination_loss", "idea_to_completed_handoff_latency", "measurement_coverage"]
NOT_DERIVED = [
    ("handoff stage times", "sent → acknowledged → in progress → completed, per handoff. telemetry@2 has no such "
     "metric; adding one is a new metric and needs the DEC-038 review first."),
]


def e(x):
    return html.escape("" if x is None else str(x), quote=True)


def _med(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return {"median": round(statistics.median(xs), 1) if xs else None, "n": len(xs),
            "max": round(max(xs), 1) if xs else None}


def read_chat(rd):
    """Every chat-telemetry output under telemetry/chat/, plus every raw capture in the records, so captures
    nobody has analysed show up as such."""
    files, unreadable, raws = [], [], []
    for p in rd.walk_json():
        rel = rd.rel(p)
        if rel.endswith(".raw.json"):
            raws.append(rel)
        if not rel.startswith(CHAT_DIR + "/"):
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            unreadable.append(rel)
            continue
        if not isinstance(d, dict) or not SHAPE <= set(d) or not isinstance(d["threads"], list):
            unreadable.append(rel)
            continue
        files.append((rel, d))
    return sorted(files), sorted(unreadable), sorted(raws)


def derive(rd, tel, decisions, challenges, owner, content="full"):
    """Each thread in each output is its own conversation, keyed by position (file, index), never by title: two
    captures that share a title stay apart. Its label is the title (numbered when repeated), or, with
    content="metadata", "capture N (platform)" so no capture title reaches the page or cockpit.json."""
    files, unreadable, raws = read_chat(rd)
    threads, turns, covered, used = [], [], set(), set()
    for rel, d in files:
        for t in d["threads"]:
            if not isinstance(t, dict):
                continue
            src = str(t.get("source") or "")
            covered.add(src)
            base = (f"{t.get('title') or '?'} ({t.get('platform') or '?'})" if content == "full"
                    else f"capture {len(threads) + 1} ({t.get('platform') or '?'})")
            tid, k = base, 1
            while tid in used:
                k += 1
                tid = f"{base} [{k}]"
            used.add(tid)
            s = t.get("summary") or {}
            rows = []
            for x in t.get("turns") or []:
                r = {k: x.get(k) for k in TURN_KEYS}
                r["thread"] = tid
                r["author"] = "relayed" if x.get("relay") else "owner"
                r["hangs"] = [{"s": h.get("seconds") if isinstance(h, dict) else None} for h in (x.get("hangs") or [])]
                r["recorded_index"] = len(rows)   # chat-telemetry keeps recorded (causal) order; timestamps can run backwards
                rows.append(r)
            turns += rows
            threads.append({"thread": tid, "platform": t.get("platform"), "file": rel, "source": src,
                            "first": t.get("first"), "last": t.get("last"), "checks": t.get("checks") or [],
                            "turns": len(rows), "owner_turns": sum(1 for r in rows if r["author"] == "owner"),
                            "relayed_turns": sum(1 for r in rows if r["author"] == "relayed"),
                            "reply_latency_s": s.get("reply_latency_s") or _med([r["reply_latency_s"] for r in rows]),
                            "engaged_gap_s": _med([r["return_gap_s"] for r in rows if isinstance(r["return_gap_s"], (int, float))
                                                   and r["return_gap_s"] < (d.get("engaged_seconds") or 900)]),
                            "hangs": sum(len(r["hangs"]) for r in rows),
                            "timezone": d.get("timezone"), "generated": d.get("generated")})
    turns.sort(key=lambda r: (str(r["start"]), r["thread"], r["n"] or 0))
    raw_names = {pathlib.PurePosixPath(r).name for r in raws}
    analysed = {pathlib.PurePosixPath(s.replace("\\", "/")).name for s in covered}
    not_analysed = sorted(r for r in raws if pathlib.PurePosixPath(r).name not in analysed)

    out = {"files": [f for f, _ in files], "unreadable": unreadable, "raw_captures": raws,
           "raw_captures_not_analysed": not_analysed, "threads": threads,
           "timezone": sorted({t["timezone"] for t in threads if t["timezone"]}),
           "engaged_seconds": sorted({d.get("engaged_seconds") for _, d in files if d.get("engaged_seconds")}) or [900]}
    if not turns:
        out["capture"] = {"state": "unknown", "reason": (
            f"no chat-telemetry output under {CHAT_DIR}/ in the records"
            + (f"; {len(raws)} raw capture(s) are in the records but not analysed" if raws else "; and no captures are in the records")
            + ". Run chat-telemetry on the captures and save its telemetry.json there.")}
    else:
        engaged = out["engaged_seconds"][0]
        by_thread = {}
        for r in turns:
            by_thread.setdefault(r["thread"], []).append(r)
        for rows in by_thread.values():   # thread relationships follow recorded order; only the charts sort by time
            rows.sort(key=lambda r: r["recorded_index"])
            for a, b in zip(rows, rows[1:] + [None]):
                a["next_author"] = b["author"] if b else None
        # the owner's reply gaps: only gaps that end in a turn the owner typed (a relayed paste ends the others)
        mine = [r for r in turns if r["next_author"] == "owner" and isinstance(r["return_gap_s"], (int, float))]
        gaps = [r["return_gap_s"] for r in mine]
        bands = {b: sum(1 for r in mine if r["return_band"] == b) for b in BANDS}
        for t in threads:   # per thread, the same definition as the headline: gaps that end in an owner-typed turn
            t["engaged_gap_s"] = _med([r["return_gap_s"] for r in mine if r["thread"] == t["thread"] and r["return_gap_s"] < engaged])
        other_gaps = sum(1 for r in turns if r["next_author"] == "relayed" and isinstance(r["return_gap_s"], (int, float)))
        hours = [0] * 24
        for r in turns:
            if isinstance(r["local_hour"], int) and 0 <= r["local_hour"] < 24:
                hours[r["local_hour"]] += 1
        # stretches: an owner turn opens one; relayed turns after it, in the same thread, belong to it
        stretches = []
        for th, rows in by_thread.items():
            cur = None
            for r in rows:
                if r["author"] == "owner":
                    cur = {"thread": th, "start": r["start"], "relayed_turns": 0, "tool_steps": r["tool_steps"] or 0,
                           "model_words": r["assistant_words"] or 0}
                    stretches.append(cur)
                elif cur is not None:
                    cur["relayed_turns"] += 1
                    cur["tool_steps"] += r["tool_steps"] or 0
                    cur["model_words"] += r["assistant_words"] or 0
                else:   # relayed turns before any owner turn in this thread form one stretch of their own
                    cur = {"thread": th, "start": r["start"], "relayed_turns": 1, "opened_by": "relayed",
                           "tool_steps": r["tool_steps"] or 0, "model_words": r["assistant_words"] or 0}
                    stretches.append(cur)
        stretches.sort(key=lambda s: str(s["start"]))
        silences = sorted(({"thread": r["thread"], "turn": r["n"], "start": r["start"], "seconds": r["longest_silence_s"],
                            "hangs": len(r["hangs"])} for r in turns if isinstance(r["longest_silence_s"], (int, float))),
                          key=lambda x: -x["seconds"])[:10]
        days = sorted({str(r["start"])[:10] for r in turns if r["start"]})
        owner_names = {owner.lower(), "owner"}
        led_dec = [d for d in decisions if str(d["fields"].get("decider") or "").strip().lower() in owner_names
                   and days[0] <= str(d["fields"].get("date") or "")[:10] <= days[-1]]
        led_ch = [c for c in challenges if str(c["fields"].get("raised_by") or "").strip().lower() in owner_names
                  and days[0] <= str(c["fields"].get("date") or "")[:10] <= days[-1]]
        owner_turns = sum(1 for r in turns if r["author"] == "owner")
        out["capture"] = {
            "state": "measured", "turns": turns, "first": turns[0]["start"], "last": turns[-1]["start"], "days": days,
            "owner_turns": owner_turns, "relayed_turns": len(turns) - owner_turns,
            "owner_turns_bound": "lower bound",
            "owner_turns_bound_reason": ("the records can't show that every coordination session in the window was "
                                         "captured (CH-033), so sessions outside the captures are missing, not zero"),
            "ledger_owner_lines": {"decisions": len(led_dec), "challenges": len(led_ch), "days": [days[0], days[-1]],
                                   "definition": "Decision lines whose decider is the owner, and challenge lines raised by "
                                                 "the owner, dated within the captured days (the ledgers record days, not times)"},
            "return_gap_bands": bands, "gaps_ending_in_relayed_turn": other_gaps, "engaged_gap_s": _med([g for g in gaps if g < engaged]),
            "reply_latency_s": _med([r["reply_latency_s"] for r in turns]),
            "hours": hours, "stretches": stretches, "longest_silences": silences,
            "hangs": sum(len(r["hangs"]) for r in turns)}
    mets = {m.get("metric"): m for m in (tel or {}).get("metrics", [])}
    out["ledger"] = {"telemetry_schema": (tel or {}).get("schema"), "as_of": (tel or {}).get("as_of"),
                     "metrics": [mets[k] for k in LEDGER_METRICS if k in mets],
                     "missing": [k for k in LEDGER_METRICS if k not in mets],
                     "not_derived": [{"what": a, "why": b} for a, b in NOT_DERIVED]}
    return out


# ---------------- rendering (static SVG; nothing runs in the page for these charts) ----------------
def _ts(s):
    import datetime as dt
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _table(head, rows, raw=False):
    if not rows:
        return '<p class="empty">none</p>'
    th = "".join(f"<th>{e(h)}</th>" for h in head)
    tr = "".join("<tr>" + "".join(f'<td data-h="{e(h)}">{v if raw else e(v)}</td>' for h, v in zip(head, r)) + "</tr>"
                 for r in rows)
    return f'<div class="tw"><table><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table></div>'


def _fmt_s(s):
    if not isinstance(s, (int, float)):
        return "unknown"
    return f"{s:.0f} s" if s < 120 else f"{s / 60:.1f} min" if s < 7200 else f"{s / 3600:.1f} h"


def timeline_svg(c):
    turns = c["turns"]
    threads = []
    for r in turns:
        if r["thread"] not in threads:
            threads.append(r["thread"])
    times = [t for t in (_ts(r["start"]) for r in turns) if t is not None]
    t0, t1 = min(times), max(times)
    span = max(t1 - t0, 1)
    W, L, R, ROW = 900, 230, 20, 34
    H = 40 + ROW * len(threads)
    x = lambda t: L + (W - L - R) * (t - t0) / span
    parts = [f'<line class="co-grid" x1="{L}" y1="{30 + ROW * i}" x2="{W - R}" y2="{30 + ROW * i}"/>'
             f'<text class="co-lab" x="{L - 8}" y="{34 + ROW * i}" text-anchor="end">{e(th[:34] + ("…" if len(th) > 34 else ""))}</text>'
             for i, th in enumerate(threads)]
    for d in c["days"]:
        t = _ts(d + "T00:00:00+00:00")
        if t is not None and t0 <= t <= t1:
            parts.append(f'<line class="co-day" x1="{x(t):.1f}" y1="14" x2="{x(t):.1f}" y2="{H - 6}"/>'
                         f'<text class="co-tick" x="{x(t) + 3:.1f}" y="12">{e(d[5:])} 00:00 UTC</text>')
    for r in turns:
        t = _ts(r["start"])
        if t is None:
            continue
        y = 30 + ROW * threads.index(r["thread"])
        cls = "co-own" if r["author"] == "owner" else "co-rel"
        parts.append(f'<circle class="{cls}" cx="{x(t):.1f}" cy="{y}" r="5.5"><title>{e(r["thread"])} turn {e(r["n"])}: '
                     f'{e(r["author"])}, {e(r["start"])}</title></circle>')
    return (f'<figure class="co-fig"><svg viewBox="0 0 {W} {H}" role="img" aria-label="Turn timeline: {len(turns)} turns '
            f'across {len(threads)} threads; filled dots are the owner\'s turns, hollow dots relayed turns">{"".join(parts)}</svg>'
            f'<figcaption><span class="co-key co-own-k"></span> owner turn <span class="co-key co-rel-k"></span> relayed turn '
            f'(another model\'s words pasted in) · time runs left to right, UTC · {e(c["first"][:16])} → {e(c["last"][:16])}</figcaption></figure>')


def bars_svg(values, labels, aria, W=900, H=180, cls="co-bar", label_every=1, value_labels=True):
    n = len(values)
    mx = max(values) if values and max(values) > 0 else 1
    L, B, T = 52, 26, 16
    bw = (W - L - 10) / max(n, 1)
    parts = [f'<line class="co-grid" x1="{L}" y1="{H - B}" x2="{W - 10}" y2="{H - B}"/>',
             f'<text class="co-tick" x="{L - 4}" y="{T + 4}" text-anchor="end">max {e(mx)}</text>']
    for i, v in enumerate(values):
        h = (H - B - T) * v / mx
        xx = L + i * bw
        parts.append(f'<rect class="{cls}" x="{xx + bw * 0.12:.1f}" y="{H - B - h:.1f}" width="{bw * 0.76:.1f}" height="{h:.1f}">'
                     f'<title>{e(labels[i])}: {e(v)}</title></rect>')
        if value_labels and v:
            parts.append(f'<text class="co-val" x="{xx + bw / 2:.1f}" y="{H - B - h - 3:.1f}" text-anchor="middle">{e(v)}</text>')
        if i % label_every == 0:
            parts.append(f'<text class="co-tick" x="{xx + bw / 2:.1f}" y="{H - 8}" text-anchor="middle">{e(labels[i])}</text>')
    return f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{e(aria)}">{"".join(parts)}</svg>'


def lengths_svg(c):
    turns = c["turns"]
    W, H, L, B, T = 900, 220, 70, 22, 14
    mid = T + (H - T - B) * 0.35
    mo = max([r["user_words"] or 0 for r in turns] + [1])
    ma = max([r["assistant_words"] or 0 for r in turns] + [1])
    bw = (W - L - 10) / len(turns)
    parts = [f'<line class="co-grid" x1="{L}" y1="{mid:.1f}" x2="{W - 10}" y2="{mid:.1f}"/>',
             f'<text class="co-tick" x="{L - 4}" y="{T + 4}" text-anchor="end">max {mo}</text>',
             f'<text class="co-tick" x="{L - 4}" y="{H - B}" text-anchor="end">max {ma}</text>',
             f'<text class="co-lab" x="{L + 4}" y="{T + 4}">words typed into the turn (up)</text>',
             f'<text class="co-lab" x="{L + 4}" y="{H - B - 2}">assistant words in reply (down)</text>']
    for i, r in enumerate(turns):
        xx = L + i * bw + bw * 0.15
        hu = (mid - T) * (r["user_words"] or 0) / mo
        ha = (H - B - mid) * (r["assistant_words"] or 0) / ma
        cls = "co-own-b" if r["author"] == "owner" else "co-rel-b"
        tip = f'{r["thread"]} turn {r["n"]} ({r["author"]}): {r["user_words"]} words in, {r["assistant_words"]} words out'
        parts.append(f'<rect class="{cls}" x="{xx:.1f}" y="{mid - hu:.1f}" width="{bw * 0.7:.1f}" height="{hu:.1f}"><title>{e(tip)}</title></rect>'
                     f'<rect class="co-asst" x="{xx:.1f}" y="{mid:.1f}" width="{bw * 0.7:.1f}" height="{ha:.1f}"><title>{e(tip)}</title></rect>')
    return (f'<figure class="co-fig"><svg viewBox="0 0 {W} {H}" role="img" aria-label="Message length per turn, in time order: '
            f'words typed into each turn above the line, assistant words below">{"".join(parts)}</svg><figcaption>One column per turn, '
            f'in time order across all threads. Above the line: <span class="co-key co-own-k"></span> owner-typed or '
            f'<span class="co-key co-relb-k"></span> relayed words; below: the assistant\'s reply. Scales differ (labelled at left).</figcaption></figure>')


def render(data, n, head):
    co = data.get("coordination")
    if not co:
        return ""
    c = co.get("capture") or {}
    led = co.get("ledger") or {}
    tz = ", ".join(co.get("timezone") or []) or "unknown"
    engaged_min = co["engaged_seconds"][0] // 60
    caveat = (f'<div class="notice"><b>Wall-clock gaps are not effort.</b> A reply gap is the time between the assistant\'s last '
              f'event and the owner\'s next message: sleep, errands and reading count the same. Only gaps under {engaged_min} minutes '
              f'are taken as engaged. Hangs are silences inside a turn; a long tool run and a stall look the same.</div>')
    if c.get("state") != "measured":
        cap = (f'<div class="panel pad"><p class="eyebrow dark">CAPTURED SESSIONS · UNKNOWN</p><span class="tag">UNKNOWN: NO CAPTURES</span>'
               f'<p>{e(c.get("reason"))}</p><p class="hint">This half is shown as unknown, not as zero turns.</p></div>')
    else:
        lo = c["ledger_owner_lines"]
        dis = ("" if (lo["decisions"] + lo["challenges"]) <= c["owner_turns"] else
               '<p class="warn">Finding: the ledgers hold more owner lines than the captures hold owner turns in the same days. '
               'A possible coverage gap: sessions may be missing from the captures. The two counts have different units '
               '(one turn can settle several lines), so this is inconclusive.</p>')
        if (lo["decisions"] + lo["challenges"]) == 0 and c["owner_turns"]:
            dis = ('<p class="warn">Finding: the captures hold owner turns but the ledgers record no owner decision or challenge in '
                   'those days. Either the turns settled nothing on the record, or the record was written later.</p>')
        tiles = "".join(f'<div class="stat"><p class="stat-label">{e(a)}</p><strong>{e(b)}</strong><small>{e(s)}</small></div>' for a, b, s in [
            ("OWNER TURNS", f'≥ {c["owner_turns"]}', "lower bound: see below"),
            ("RELAYED / AGENT TURNS", c["relayed_turns"], "another model's words pasted in"),
            ("STRETCHES", len(c["stretches"]), "owner turn plus the relayed turns after it"),
            (f"ENGAGED REPLY GAP (<{engaged_min} MIN)", _fmt_s(c["engaged_gap_s"]["median"]), f'median, n {c["engaged_gap_s"]["n"]}'),
            ("HANGS", c["hangs"], "silences ≥ the threshold inside a turn")])
        band_vals = [c["return_gap_bands"][b] for b in BANDS]
        st = c["stretches"]
        th_rows = [[t["thread"], t["turns"], t["owner_turns"], t["relayed_turns"],
                    _fmt_s((t["reply_latency_s"] or {}).get("median")), _fmt_s((t["engaged_gap_s"] or {}).get("median")) +
                    f' (n {(t["engaged_gap_s"] or {}).get("n", 0)})', t["hangs"],
                    (t["first"] or "")[:16].replace("T", " ") + " → " + (t["last"] or "")[:16].replace("T", " ")] for t in co["threads"]]
        checks = "".join(f'<li><b>{e(t["thread"])}</b>: {e("; ".join(t["checks"]))}</li>' for t in co["threads"] if t["checks"])
        sil_rows = [[s["thread"], s["turn"], (s["start"] or "")[:16].replace("T", " "), _fmt_s(s["seconds"]), s["hangs"]]
                    for s in c["longest_silences"]]
        cap = f"""
<div class="stats-row">{tiles}</div>
<div class="panel pad"><p class="eyebrow dark">OWNER TURNS: TWO INDEPENDENT COUNTS</p>
<p><b>≥ {c["owner_turns"]}</b> owner turns in the captures ({e(c["owner_turns_bound"])}: {e(c["owner_turns_bound_reason"])}).
Beside it, from the ledgers: <b>{lo["decisions"]}</b> decision line(s) decided by the owner and <b>{lo["challenges"]}</b> challenge line(s)
raised by the owner, dated {e(lo["days"][0])} to {e(lo["days"][1])}. {e(lo["definition"])}.</p>{dis}</div>
<div class="panel pad"><p class="eyebrow dark">TURN TIMELINE ACROSS THREADS</p>{timeline_svg(c)}</div>
<div class="co-two">
<div class="panel pad"><p class="eyebrow dark">HOW LONG UNTIL THE OWNER REPLIED</p>
<figure class="co-fig">{bars_svg(band_vals, ["live <2m", "break 2–15m", "away 15m–3h", "later >3h"], "Reply gaps by band: " + ", ".join(f"{b} {v}" for b, v in zip(BANDS, band_vals)), W=520, H=190)}
<figcaption>Gaps between the assistant's last event and the next message <i>the owner typed</i>, by band
({c["gaps_ending_in_relayed_turn"]} gaps that ended in a relayed paste are left out, and a thread's first turn has no gap before it,
so the bands add up to fewer than the owner turns). Median of the engaged gaps (under {engaged_min} min):
{e(_fmt_s(c["engaged_gap_s"]["median"]))}, n {c["engaged_gap_s"]["n"]}. Wall clock, not effort.</figcaption></figure></div>
<div class="panel pad"><p class="eyebrow dark">TURNS BY LOCAL HOUR ({e(tz)})</p>
<figure class="co-fig">{bars_svg(c["hours"], [f"{h:02d}" for h in range(24)], "Turns by local hour: " + ", ".join(f"{h:02d}h {v}" for h, v in enumerate(c["hours"]) if v), W=520, H=190, label_every=3)}
<figcaption>Turns that started in each hour of the day, local time ({e(tz)}).</figcaption></figure></div></div>
<div class="panel pad"><p class="eyebrow dark">MESSAGE LENGTH PER TURN</p>{lengths_svg(c)}</div>
<div class="panel pad"><p class="eyebrow dark">GOVERNED VS AUTONOMOUS</p>
<p class="hint">A stretch opens at an owner turn and holds the relayed turns that follow it in the same thread, up to the owner's next
turn: how much happened between the owner's turns. Who typed a turn is all this shows; it doesn't show whether an exchange followed the
relay-governor rules (run cards, tiers and budgets aren't in the captures).</p>
<figure class="co-fig">{bars_svg([s["relayed_turns"] for s in st], [str(i) for i in range(1, len(st) + 1)], "Relayed turns per stretch, in time order: " + ", ".join(str(s["relayed_turns"]) for s in st), H=170, cls="co-rel-b", label_every=max(1, len(st) // 20))}
<figcaption>Relayed turns in each stretch, stretches numbered in time order. {sum(1 for s in st if s["relayed_turns"])} of {len(st)} stretches held any relayed turn;
the longest held {max([s["relayed_turns"] for s in st] + [0])}.</figcaption></figure></div>
<div class="panel pad"><p class="eyebrow dark">PER THREAD</p>{_table(["thread", "turns", "owner", "relayed", "reply latency (median)", f"engaged gap < {engaged_min} min (median)", "hangs", "first → last (UTC)"], th_rows)}
<p class="mini-label co-gap">CHECKS CHAT-TELEMETRY REPORTED</p><ul class="hint">{checks or "<li>none</li>"}</ul></div>
<div class="panel pad"><p class="eyebrow dark">HANGS AND LONGEST SILENCES (TOP 10)</p>{_table(["thread", "turn", "started (UTC)", "longest silence in the turn", "hangs"], sil_rows)}</div>"""
    srcs = (f'The captured-sessions part above is read from <code>{e(", ".join(co["files"]) or "nothing")}</code> (chat-telemetry\'s own output; this page '
            f're-measures nothing). Raw captures in the records: {len(co["raw_captures"])}; not analysed: '
            f'{len(co["raw_captures_not_analysed"])}' + (f' ({e(", ".join(co["raw_captures_not_analysed"]))})' if co["raw_captures_not_analysed"] else "")
            + (f'. Unreadable: {e(", ".join(co["unreadable"]))}' if co["unreadable"] else "") + ".")

    def lv(m):
        k = m["metric"]
        if k == "coordination_loss":
            st = m.get("stale_handoffs") or []
            return (f'{len(st)} stale handoff(s) (open, no step in 24 h): {", ".join(x["id"] for x in st) or "none"}; '
                    f'{len(m.get("cancelled_handoffs") or [])} cancelled; {m.get("overlapping_claim_count", 0)} overlapping claim(s); '
                    f'{len(m.get("ledger_conflicts") or [])} ledger conflict(s); reconciliation time: '
                    + ("unknown" if m.get("reconciliation_minutes") is None else f'{m["reconciliation_minutes"]} min'))
        if k == "idea_to_completed_handoff_latency":
            h = m.get("hours") or {}
            return (f'median {h.get("median")}, p90 {h.get("p90")}, n {h.get("n")} (hours)' if h.get("n") else
                    "unknown: " + str(m.get("unknown") or "no cases")) + (
                    "; excluded: " + "; ".join(f"{a}: {b}" for a, b in (m.get("excluded") or {}).items()) if m.get("excluded") else "")
        if k == "measurement_coverage":
            return (f'{len(m.get("unmeasured_sessions") or [])} of {m.get("agent_sessions_in_handoffs")} agent sessions named in handoffs '
                    f'have no measured budget line (estimates and proxies count as unmeasured); budget lines by method: {json.dumps(m.get("budget_lines_by_method") or {})}; '
                    f'{len(m.get("repositories_read") or [])} repositories read. Not meterable: {"; ".join(m.get("not_meterable") or [])}')
        return json.dumps(m)[:300]
    led_rows = [[m["metric"], m.get("label"), lv(m), m.get("definition")] for m in led.get("metrics", [])]
    led_rows += [[k, "unknown", "not in this telemetry view", ""] for k in led.get("missing", [])]
    led_rows += [[x["what"], "not derived", "—", x["why"]] for x in led.get("not_derived", [])]
    return f"""
<section class="section surface" id="coordination">
{head(n, "COORDINATION", "Coordination telemetry", "Where the owner's turns went and how much happened between them, from captured sessions; and where handoffs stalled, from the ledgers.", "FROM RECORDS · DERIVED")}
{caveat}
<p class="eyebrow dark">CAPTURED SESSIONS (chat-telemetry output)</p>
{cap}
<p class="hint">{srcs}</p>
<div class="panel pad"><p class="eyebrow dark">FROM THE LEDGERS (relay-kit/telemetry@2, AS OF {e(led.get("as_of") or "unknown")})</p>
<p class="hint">The same derived figures as the telemetry table under Run commitments, gathered here. Unknown is never zero.</p>
{_table(["metric", "label", "value", "definition"], led_rows)}</div>
</section>"""


CSS = """
.co-fig{margin:0}.co-fig svg{width:100%;height:auto;display:block}.co-fig figcaption{font-size:.82rem;color:var(--muted);margin-top:.4rem}
.co-two{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));gap:1rem}
.co-grid{stroke:var(--line);stroke-width:1}.co-day{stroke:var(--border);stroke-dasharray:3 4}
.co-lab{fill:var(--text-2);font-size:12px}.co-tick{fill:var(--muted);font-size:11px}.co-val{fill:var(--text-2);font-size:10px}
.co-own{fill:var(--gold);stroke:var(--gold-deep);stroke-width:1}.co-rel{fill:var(--panel);stroke:var(--teal);stroke-width:2}
.co-bar{fill:var(--gold)}.co-own-b{fill:var(--gold)}.co-rel-b{fill:var(--teal)}.co-asst{fill:var(--muted);opacity:.55}
.co-gap{margin-top:1rem}.co-key{display:inline-block;width:.8em;height:.8em;border-radius:50%;vertical-align:-.05em;margin:0 .2em 0 .5em}
.co-own-k{background:var(--gold)}.co-rel-k{border:2px solid var(--teal)}.co-relb-k{background:var(--teal);border-radius:2px}
"""
