"""relay-kit/lifecycle@1: what the decision and challenge ledgers say is open, settled, in force or
contested. One pure module, used by check_ledgers, status, export, mcp, cockpit and ledger_append,
so every reader draws the same conclusions from the same lines. Standard library only; reads nothing.

    resolve(decisions, challenges, as_of=None) -> {"decisions": {id: D}, "challenges": {id: C},
                                                   "problems": [P], "conflicts": [X], "as_of": ...}
    ledger_as_of(decisions, challenges)   the time every reader evaluates effective dates against
    line_hash(raw)                        the one hash of a ledger line (raw bytes, as stored)

Rules (docs/lifecycle.md):

- A `supersedes` (one DEC id, as text) takes effect only from a settled line. `adopted` closes any
  earlier decision (a proposal is settled by it; an adopted decision is replaced). `rejected` and
  `superseded` settle a *proposal* only: they cannot retire an adopted decision; that takes an
  adopted line that supersedes or `revokes` it. A `proposed` line that supersedes is a proposed
  replacement: its target stays as it was. Adopting such a proposal also replaces its target.
- A challenge `resolves` (one CH id) takes effect only from a `resolved` or `accepted-risk` line.
- A transition must point at an earlier, different, known record (no self, forward or unknown
  reference, so no cycles); a non-text target is a format problem. Invalid transitions are reported
  and take no effect. A later line repeating an id changes nothing (check_ledgers reports the id).
- More than one effective closing or revoking line on one record is a conflict: every one is kept
  and the conflict reported, never collapsed.
- Applicability of an adopted decision: `standing` ("adopted, no structured supersession found")
  unless an effective later line supersedes it (`superseded`) or revokes it (`revoked`, from that
  line's effective_from, else its date), or its own effective_until has passed (`expired`) or
  effective_from not come (`not yet effective`). An adopted line that only `amends` decisions that
  no longer apply lapses with them (`lapsed with what it amends`). Without `as_of`, dates aren't
  evaluated: a revocation with its own effective_from is then pending, not applied.
"""
import hashlib
import re

SCHEMA = "relay-kit/lifecycle@1"
SETTLES_PROPOSAL = {"adopted", "rejected", "superseded"}
CH_SETTLED = {"resolved", "accepted-risk"}
STANDING = "adopted, no structured supersession found"
LAPSED = "lapsed with what it amends"
WHEN = re.compile(r"^\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2})?Z)?$")


def line_hash(raw):
    """sha256 of a ledger line's raw bytes as stored, without its trailing newline ('\\r' kept)."""
    return hashlib.sha256(raw.encode("utf-8") if isinstance(raw, str) else raw).hexdigest()


def ledger_as_of(*ledgers):
    """End of the latest valid `date` in the ledgers: what every reader evaluates effective dates
    against. Never the clock; None when there is no date."""
    dates = sorted(str(r.get("date")) for led in ledgers for r in led
                   if isinstance(r, dict) and re.match(r"^\d{4}-\d{2}-\d{2}$", str(r.get("date") or "")))
    return dates[-1] + "T23:59:59Z" if dates else None


def num(rid):
    """The number of a sequential id (DEC-012, CH-003); None for a hash id (DEC-20261123-K7QF, DEC-272)."""
    m = re.fullmatch(r"[A-Z]+-(\d+)", str(rid or ""))
    return int(m.group(1)) if m else None


def one(v):
    """supersedes / resolves: one id as text, else None."""
    return v if isinstance(v, str) and v else None


def refs(v):
    """revokes / amends: 'DEC-1', ['DEC-1', 'DEC-2'] or {"id": ..., "clauses": [...]} -> [(id, clauses)]."""
    if isinstance(v, str) and v:
        return [(v, None)]
    if isinstance(v, dict) and isinstance(v.get("id"), str):
        cl = v.get("clauses")
        return [(v["id"], [str(c) for c in cl] if isinstance(cl, list) else None)]
    if isinstance(v, list):
        return [r for x in v for r in refs(x)]
    return []


def as_time(when, end=False):
    """'2026-11-04' -> '2026-11-04T00:00:00Z' (T23:59:59Z as an end); a time keeps its own, seconds added."""
    if not isinstance(when, str) or not WHEN.match(when):
        return None
    if len(when) == 10:
        return when + ("T23:59:59Z" if end else "T00:00:00Z")
    return when if when.count(":") == 2 else when[:-1] + ":00Z"


def resolve(decisions, challenges, as_of=None):
    probs, conflicts = [], []
    now = as_time(as_of) if as_of else None

    def bad(rid, field, tgt, rule, msg):
        probs.append({"id": rid, "field": field, "target": tgt, "rule": rule, "message": msg})

    def ok_ref(rid, field, tgt, table, order_):
        if tgt == rid:
            bad(rid, field, tgt, "self", f"{rid} {field} itself")
        elif tgt not in table:
            bad(rid, field, tgt, "unknown", f"{rid} {field} {tgt}, which isn't in the ledger")
        elif not (order_[tgt] < order_[rid] and (num(tgt) is None or num(rid) is None or num(tgt) < num(rid))):
            bad(rid, field, tgt, "forward", f"{rid} {field} {tgt}, which isn't earlier")
        else:
            return True
        return False

    # ---- decisions ----
    D, order, line = {}, {}, {}
    for i, r in enumerate(decisions):
        rid = r.get("id") if isinstance(r, dict) else None
        if not isinstance(rid, str) or rid in D:
            continue
        order[rid], line[rid] = i, r
        D[rid] = {"id": rid, "status": r.get("status"), "closed_by": [], "closure": None, "proposed_replacements": [],
                  "replaces": one(r.get("supersedes")), "amends": [t for t, _ in refs(r.get("amends"))],
                  "amended_by": [], "proposed_amendments": [], "revoked_by": [], "revocations_pending": [],
                  "effective_from": r.get("effective_from"), "effective_until": r.get("effective_until")}

    for rid, r in line.items():
        st = r.get("status")
        sup = r.get("supersedes")
        if sup is not None and not one(sup):
            bad(rid, "supersedes", None, "format", f"{rid} supersedes must be one DEC id, as text")
        elif one(sup) and ok_ref(rid, "supersedes", sup, D, order):
            t = D[sup]
            if st == "proposed":
                t["proposed_replacements"].append(rid)
            elif st == "adopted" or (st in SETTLES_PROPOSAL and t["status"] == "proposed"):
                t["closed_by"].append({"id": rid, "status": st})
            else:
                bad(rid, "supersedes", sup, "ineffective",
                    f"{rid} is {st} and supersedes {sup}, which is {t['status']}: a {st} line settles a proposal only; "
                    f"retiring an adopted decision takes an adopted line that supersedes or revokes it")
        for tgt, _ in refs(r.get("revokes")):
            if not ok_ref(rid, "revokes", tgt, D, order):
                continue
            if st != "adopted":
                bad(rid, "revokes", tgt, "ineffective", f"{rid} is {st}: only an adopted line revokes")
            elif D[tgt]["status"] != "adopted":
                bad(rid, "revokes", tgt, "ineffective", f"{rid} revokes {tgt}, which isn't adopted")
            else:
                own = as_time(r.get("effective_from"))
                frm = own or as_time(r.get("date"))
                if (own and not now) or (now and frm and frm > now):
                    D[tgt]["revocations_pending"].append({"id": rid, "from": frm})   # not yet, or dates not evaluated
                else:
                    D[tgt]["revoked_by"].append({"id": rid, "from": frm})
        for tgt, clauses in refs(r.get("amends")):
            if ok_ref(rid, "amends", tgt, D, order):
                (D[tgt]["amended_by"] if st == "adopted" else D[tgt]["proposed_amendments"]).append({"id": rid, "clauses": clauses})
        for f in ("effective_from", "effective_until"):
            if f in r and not as_time(r.get(f)):
                bad(rid, f, None, "format", f"{rid} {f} must be YYYY-MM-DD or an ISO time in UTC")
        for f in ("revokes", "amends"):
            if f in r and r[f] is not None and not refs(r[f]):
                bad(rid, f, None, "format", f"{rid} {f} must be a DEC id, a list of them, or {{\"id\": ..., \"clauses\": [...]}}")

    # adopting a proposal that proposed to replace X replaces X too (so a second adoption of X shows as a conflict)
    for rid, d in D.items():
        x = d["replaces"]
        if d["status"] == "proposed" and x in D and rid in D[x]["proposed_replacements"]:
            for c in list(d["closed_by"]):
                if c["status"] == "adopted":
                    D[x]["closed_by"].append({"id": c["id"], "status": "adopted", "via": rid})

    for rid, d in D.items():
        closers = d["closed_by"]
        statuses = sorted({c["status"] for c in closers})
        d["conflict"] = len(closers) + len(d["revoked_by"]) > 1
        if d["conflict"]:
            conflicts.append({"id": rid, "kind": "decision", "closed_by": [c["id"] for c in closers],
                              "revoked_by": [c["id"] for c in d["revoked_by"]], "statuses": statuses})
        d["closure"] = None if not closers else statuses[0] if len(statuses) == 1 else "conflict"
        st = d["status"]
        if st == "proposed":
            d["applicability"] = "settled" if closers else "open"
        elif st == "adopted":
            frm, until = as_time(d["effective_from"]), as_time(d["effective_until"], end=True)
            if any(c["status"] == "adopted" for c in closers):
                d["applicability"] = "superseded"
            elif d["revoked_by"]:
                d["applicability"] = "revoked"
            elif now and until and until < now:
                d["applicability"] = "expired"
            elif now and frm and frm > now:
                d["applicability"] = "not yet effective"
            else:
                d["applicability"] = STANDING
        else:
            d["applicability"] = f"not a standing decision ({st})"
    # an adopted line that only amends decisions that no longer apply lapses with them
    for d in D.values():
        if d["applicability"] == STANDING and d["amends"] and not d["replaces"] and \
                all(t in D and D[t]["applicability"] != STANDING for t in d["amends"]):
            d["applicability"] = LAPSED
    for d in D.values():
        d["in_force"] = d["applicability"] == STANDING
        d["open"] = d["applicability"] == "open"
        d["dates_evaluated"] = bool(now)

    # ---- challenges ----
    C, corder, cline = {}, {}, {}
    for i, r in enumerate(challenges):
        rid = r.get("id") if isinstance(r, dict) else None
        if not isinstance(rid, str) or rid in C:
            continue
        corder[rid], cline[rid] = i, r
        C[rid] = {"id": rid, "status": r.get("status"), "closed_by": [], "closure": None}
    for rid, r in cline.items():
        st, res = r.get("status"), r.get("resolves")
        if res is not None and not one(res):
            bad(rid, "resolves", None, "format", f"{rid} resolves must be one CH id, as text")
        elif one(res) and ok_ref(rid, "resolves", res, C, corder):
            if st in CH_SETTLED:
                C[res]["closed_by"].append({"id": rid, "status": st})
            else:
                bad(rid, "resolves", res, "ineffective",
                    f"{rid} is {st} and resolves {res}: only a resolved or accepted-risk line closes a challenge")
    for rid, c in C.items():
        closers = c["closed_by"]
        statuses = sorted({x["status"] for x in closers})
        c["conflict"] = len(closers) > 1
        if c["conflict"]:
            conflicts.append({"id": rid, "kind": "challenge", "closed_by": [x["id"] for x in closers], "statuses": statuses})
        c["closure"] = None if not closers else statuses[0] if len(statuses) == 1 else "conflict"
        c["open"] = c["status"] == "open" and not closers
    return {"schema": SCHEMA, "as_of": as_of, "decisions": D, "challenges": C, "problems": probs, "conflicts": conflicts}
