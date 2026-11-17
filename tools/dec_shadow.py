#!/usr/bin/env python3
"""DEC compatibility shadow (Docket item B, DEC-251): a read-only replay of a decision ledger.

It writes nothing to any ledger and decides nothing. The existing ledger stays authoritative.

    python3 tools/dec_shadow.py pin     --repo R [--path governance/decisions.jsonl] [--ref main] -o pin.json
    python3 tools/dec_shadow.py resolve --pin pin.json REF [REF ...]    REF = DEC-7 | DEC-7@<commit> | DEC-7#<digest>
    python3 tools/dec_shadow.py standing --repo R --at S [--path ...]   effective standing at snapshot S
    python3 tools/dec_shadow.py prefix  --pin pin.json FILE [--lines 250]
    python3 tools/dec_shadow.py models  [--cases tests/fixtures/dec_shadow_cases.json]

Three things are kept apart:

1. Reference -> bytes. `pin` walks every commit reachable from REF that touched the ledger (merged PR
   branches included, not only first parent) and indexes each line: id -> line digest
   (lifecycle.line_hash, raw bytes) -> first commit, line number, and a digest of its `decision`
   text. A bare reference resolves only when every line that ever carried that id states the same
   decision (status edits are revisions of one decision). If two different decisions ever carried the
   id (the collision era: two PRs numbering at once), a bare reference fails as `ambiguous`, listing
   the candidates. It is never silently redirected. `DEC-N@commit` or `DEC-N#digest` picks one.
2. Standing at a snapshot S. Separate output: lifecycle.resolve over the ledger bytes at commit S.
   A row's own status and its effective standing can differ (a proposed row closed by a later line).
3. New-format records, under two interpretations, against the same fixtures (`models`):
   - event keys: decisions/<key>/v1.json, one file per event (adopt, supersede, revoke, withdraw);
   - enduring key / version: decisions/<key>/vN.json, standing from the version chain.
   Both readers replay the whole observed history of main, never just the files present now: a
   record once observed and later missing is reported `inconsistent`, and the standing stays what the
   observed history says. Nothing is revived because a file disappeared.

What this does not do: migrate storage, allocate ids, prevent anything. Every conflict reported
after a merge is detection, not prevention. Standard library only."""
import argparse, hashlib, json, pathlib, re, subprocess, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import lifecycle as LC  # noqa: E402

SCHEMA = "relay-kit/dec-shadow@1"
REF = re.compile(r"^(?P<id>[A-Z]+-[0-9A-Z-]+?)(?:@(?P<commit>[0-9a-f]{4,40})|#(?P<digest>[0-9a-f]{6,64}))?$")


# ---------- 1. pinned legacy bytes ----------

def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True).stdout


def body_digest(row):
    """What a line decides, independent of its id, status and bookkeeping: sha256 of `decision`."""
    return hashlib.sha256(str(row.get("decision", "")).encode("utf-8")).hexdigest()


def index_bytes(data, commit, idx, first_parent, dups=None):
    here = {}
    for n, raw in enumerate(data.split(b"\n"), 1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        rid = row.get("id") if isinstance(row, dict) else None
        if not isinstance(rid, str):
            continue
        dg = LC.line_hash(raw)
        if rid in here and dups is not None:
            dups.setdefault(rid, commit)   # two lines carry one id in the same file
        here[rid] = dg
        e = idx.setdefault(rid, {}).setdefault(dg, {"commit": commit, "line": n, "body": body_digest(row),
                                                     "status": row.get("status"), "first_parent": False})
        e["first_parent"] = e["first_parent"] or first_parent


def pin(repo, path="governance/decisions.jsonl", ref="main"):
    head = _git(repo, "rev-parse", ref).decode().strip()
    fp = set(_git(repo, "rev-list", "--first-parent", head).decode().split())   # no path: path-limiting drops merges
    log = [x.split() for x in _git(repo, "log", "--full-history", "--reverse", "--format=%H %P", head, "--", path).decode().splitlines()]
    every = [x[0] for x in log]
    idx, dups, links, cache = {}, {}, {}, {}

    def bodies_at(c):   # id -> set of decision-text digests in the file at commit c
        if c not in cache:
            try:
                data = _git(repo, "show", f"{c}:{path}")
            except subprocess.CalledProcessError:
                data = b""
            m = {}
            for raw in data.split(b"\n"):
                rid = _id(raw) if raw.strip() else None
                if isinstance(rid, str):
                    m.setdefault(rid, set()).add(body_digest(json.loads(raw)))
            cache[c] = (data, m)
        return cache[c]

    every_done = []
    for c, *parents in log:   # oldest first, so an entry's commit is where those bytes were first seen
        data, now = bodies_at(c)
        if not data:
            every_done.append(c)
            continue   # the commit deleted the file
        index_bytes(data, c, idx, c in fp, dups)
        if parents:   # first parent only: a merge's second side is not this line's past
            before = bodies_at(parents[0])[1]
            alive = set().union(*now.values()) if now else set()
            for rid, olds in before.items():
                news = now.get(rid, set()) - olds
                gone = {b for b in olds - now.get(rid, set()) if b not in alive}
                if len(gone) == 1 and len(news) == 1:   # one text replaced in place, not moved to another id
                    links.setdefault(rid, []).append([gone.pop(), news.pop(), len(every_done)])
        every_done.append(c)
    # a text that another id carries at or after the replacement was moved there (a renumber), not revised
    order = {c: i for i, c in enumerate(every_done)}
    later = {}
    for rid, cands in idx.items():
        for e in cands.values():
            later.setdefault(e["body"], []).append((rid, order.get(e["commit"], 0)))
    for rid in list(links):
        links[rid] = [[a, b] for a, b, at in links[rid]
                      if not any(o != rid and when >= at for o, when in later.get(a, []))]
    cur = _git(repo, "show", f"{head}:{path}")
    lines = cur.split(b"\n")
    return {"schema": SCHEMA, "repo_ref": ref, "commit": head, "path": path, "commits_read": len(every),
            "blob_sha256": hashlib.sha256(cur).hexdigest(), "lines": len([x for x in lines if x.strip()]),
            "current": {json.loads(x)["id"]: LC.line_hash(x) for x in lines if x.strip()},
            "duplicates_in_one_file": dups, "revisions": links, "index": idx}


def _id(raw):
    try:
        row = json.loads(raw)
    except ValueError:
        return None
    return row.get("id") if isinstance(row, dict) else None


def resolve_ref(p, ref, repo=None):
    m = REF.match(ref.strip())
    if not m:
        return {"ref": ref, "result": "unparsed"}
    rid, commit, digest = m["id"], m["commit"], m["digest"]
    cands = p["index"].get(rid, {})
    if not cands:
        return {"ref": ref, "result": "unknown"}
    if digest:
        hit = [d for d in cands if d.startswith(digest)]
    elif commit:
        if repo:   # every line carrying rid at that commit, not only bytes first seen there
            try:
                data = _git(repo, "show", f"{commit}:{p['path']}")
            except subprocess.CalledProcessError:
                data = b""
            hit = [d for d in (LC.line_hash(x) for x in data.split(b"\n") if x.strip() and _id(x) == rid) if d in cands]
        else:
            hit = [d for d, e in cands.items() if e["commit"].startswith(commit)]
            if commit and rid in p.get("duplicates_in_one_file", {}):
                return {"ref": ref, "result": "unresolved", "why": f"two lines carried {rid} in one file; pass --repo "
                        "to read every line at that commit"}
        if not hit:
            return {"ref": ref, "result": "unresolved", "why": "no line with this id at that commit"
                    + ("" if repo else " was first seen there (pass --repo to read the ledger at the commit)")}
    else:
        bodies = {e["body"] for e in cands.values()}
        why = collision_why(p, rid)
        if why:
            return {"ref": ref, "result": "ambiguous", "why": why,
                    "candidates": [{"digest": d, **e} for d, e in sorted(cands.items(), key=lambda kv: kv[1]["line"])]}
        cur = p["current"].get(rid)
        hit = [cur] if cur in cands else sorted(cands, key=lambda d: cands[d]["commit"])[-1:]
    if len(hit) != 1:
        return {"ref": ref, "result": "ambiguous" if hit else "unknown", "candidates": hit}
    e = cands[hit[0]]
    return {"ref": ref, "result": "resolved", "digest": hit[0], "revisions": len(cands),
            "text_revised": len({x["body"] for x in cands.values()}) > 1, **e,
            "also_carried_by": sorted(i for i, c in p["index"].items() if i != rid and any(x["body"] == e["body"] for x in c.values()))}


def collision_why(p, rid):
    """Why a bare reference to rid can't name one decision, or None.

    A text that replaced another under rid from one commit to its first-parent child, while the old text
    left the file entirely and no other id carries it then or later, is a revision of one decision. Texts that rid carried and that no chain of
    revisions joins are different decisions: a collision (two PRs took the number, or a renumber moved
    one away). Two lines carrying rid in one file is a collision too. A text that moved to another id
    is never a revision, so a renumber can't pass as one."""
    cands = p["index"].get(rid, {})
    if rid in p.get("duplicates_in_one_file", {}):
        return f"two lines carried {rid} in one file (first at {p['duplicates_in_one_file'][rid][:7]})"
    bodies = {e["body"] for e in cands.values()}
    if len(bodies) < 2:
        return None
    group = {b: b for b in bodies}

    def root(b):
        while group[b] != b:
            b = group[b]
        return b
    for a, b in p.get("revisions", {}).get(rid, []):
        if a in group and b in group:
            group[root(a)] = root(b)
    lineages = {root(b) for b in bodies}
    if len(lineages) < 2:
        return None
    others = sorted({o for o, oc in p["index"].items() if o != rid and any(x["body"] in bodies for x in oc.values())})
    return (f"{len(lineages)} different decisions carried {rid}" + (f"; texts also carried by {', '.join(others)}" if others else "")
            + " (collision era)")


def collisions(p):
    """Ids a bare reference can't resolve, with the reason."""
    return {rid: collision_why(p, rid) for rid in sorted(p["index"]) if collision_why(p, rid)}


def prefix_check(p_or_bytes, data, n=250):
    """Are the first n lines of `data` byte-for-byte the first n lines of the pinned file?"""
    old = p_or_bytes.split(b"\n")[:n]
    new = data.split(b"\n")[:n]
    diff = [i + 1 for i, (a, b) in enumerate(zip(old, new)) if a != b]
    return {"lines": n, "identical": old == new and len(old) == n, "first_difference": diff[0] if diff else None,
            "sha256": hashlib.sha256(b"\n".join(old)).hexdigest()}


def view(legacy, records):
    """The generated decisions.jsonl a migration would publish: the legacy bytes untouched, then one
    line per new-format record. Shadow only: nothing writes this over the real ledger."""
    head = legacy if legacy.endswith(b"\n") or not legacy else legacy + b"\n"
    return head + b"".join(json.dumps(r, ensure_ascii=False).encode("utf-8") + b"\n" for r in records)


def standing(repo, at, path="governance/decisions.jsonl"):
    data = _git(repo, "show", f"{at}:{path}")
    rows = [json.loads(x) for x in data.split(b"\n") if x.strip()]
    r = LC.resolve(rows, [], LC.ledger_as_of(rows))
    differs = {i: {"row_status": d["status"], "standing": d["applicability"], "closed_by": [c["id"] for c in d["closed_by"]]}
               for i, d in r["decisions"].items()
               if not (d["status"] == "adopted" and d["in_force"]) and not (d["status"] == "proposed" and d["open"])
               and not (d["status"] == "rejected")}
    return {"at": _git(repo, "rev-parse", at).decode().strip(), "rows": len(rows),
            "row_status_differs_from_standing": differs, "conflicts": r["conflicts"], "problems": r["problems"]}


# ---------- 3. new-format records under two interpretations ----------

def _jb(obj):
    return (json.dumps(obj, sort_keys=True) + "\n").encode()


def merge3(base, ours, theirs):
    """Path-level three-way merge of {path: bytes}, as git does it for whole new or removed files."""
    out, conflicts = dict(base), []
    for p in sorted(set(base) | set(ours) | set(theirs)):
        b, o, t = base.get(p), ours.get(p), theirs.get(p)
        if o == t:
            res = o
        elif o == b:
            res = t
        elif t == b:
            res = o
        else:
            conflicts.append(p)
            res = o
        if res is None:
            out.pop(p, None)
        else:
            out[p] = res
    return out, conflicts


def play(case):
    """Run a case's steps on main; returns the list of main snapshots and any merge conflicts."""
    main, snaps, conflicts, added = {}, [], [], []
    for step in case["steps"]:
        k = step["do"]
        if k == "add":
            new = dict(main)
            for f in step["files"]:
                new[f["path"]] = _jb(f["record"])
            added.append([f["path"] for f in step["files"]])
            main = new
        elif k == "merge":
            base = dict(main)
            sides = []
            for br in step["branches"]:
                s = dict(base)
                for f in br:
                    s[f["path"]] = _jb(f["record"])
                sides.append(s)
            merged, c = merge3(base, sides[0], sides[1])
            conflicts += c
            added.append([f["path"] for br in step["branches"] for f in br])
            main = merged
        elif k == "revert":
            main = {p: b for p, b in main.items() if p not in set(added[step["step"]])}
            added.append([])
        else:
            raise ValueError(f"unknown step {k}")
        snaps.append(main)
    return snaps, conflicts


def _observed(snaps):
    """Every record ever seen on main (path -> its first bytes, read), and the paths whose history isn't
    append-only: seen once and missing at the end, or seen later with different bytes (records are
    immutable events or versions, so either is reported, never resolved quietly)."""
    seen, first, bad = {}, {}, []
    for s in snaps:
        for p, b in s.items():
            if p not in first:
                first[p], seen[p] = b, json.loads(b)
            elif b != first[p] and p not in [x[0] for x in bad]:
                bad.append((p, "observed on main earlier with different bytes"))
    bad += [(p, "observed on main earlier, missing now") for p in seen if p not in snaps[-1]]
    return seen, sorted(bad)


def read_event(snaps):
    seen, missing = _observed(snaps)
    st, problems, succ, bodies = {}, [], {}, {}
    for r in seen.values():   # in the order main first showed them
        op, key, tgt = r["op"], r["key"], r.get("target")
        if op == "adopt":
            st.setdefault(key, "adopted")
        elif op == "supersede":
            st.setdefault(key, "adopted")
            succ.setdefault(tgt, []).append(key)
            st[tgt] = "superseded"
        elif op == "revoke":
            st.setdefault(key, "adopted")
            st[tgt] = "revoked"
        elif op == "withdraw":
            st[tgt] = "withdrawn"
        elif op == "revive":
            st[tgt] = "adopted"
        if op == "act":
            continue   # an action cites decisions; it is not one
        sig = json.dumps({k: v for k, v in r.items() if k not in ("key", "session")}, sort_keys=True)
        bodies.setdefault(sig, []).append(key)
    for tgt, ks in succ.items():
        if len(ks) > 1:
            problems.append({"kind": "fork", "target": tgt, "successors": sorted(ks)})
    for sig, ks in bodies.items():
        if len(ks) > 1:
            problems.append({"kind": "duplicate", "keys": sorted(ks)})
    return st, problems, missing, seen


def read_version(snaps):
    seen, missing = _observed(snaps)
    chains = {}
    for p, r in seen.items():
        chains.setdefault(r["key"], {})[r["v"]] = r
    st, problems = {}, []
    for key, vs in chains.items():
        head = max(vs)
        gaps = [v for v in range(1, head) if v not in vs]
        if gaps:
            problems.append({"kind": "gap", "key": key, "missing_versions": gaps})
        st[key] = vs[head]["state"]
    return st, problems, missing, seen


def latest_file(snaps, model):
    """The naive reader: only the files present now. Shown to make accidental revival visible."""
    last = [snaps[-1]]
    if model == "event":
        s = read_event(last)[0]
    else:
        s = read_version(last)[0]
    return s


def citation_check(seen, standing_):
    """An action that cites a decision whose standing at S is no longer adopted (detection only)."""
    out = []
    for r in seen.values():
        c = r.get("cites")
        if c and standing_.get(c) not in ("adopted", None):
            out.append({"kind": "cited_not_in_force", "action": r["key"], "cites": c, "standing_at_S": standing_[c]})
    return out


def run_case(case, model):
    snaps, conflicts = play(case[model])
    reader = read_event if model == "event" else read_version
    st, problems, missing, seen = reader(snaps)
    problems += citation_check(seen, st)
    for p, why in missing:
        problems.append({"kind": "inconsistent", "path": p, "why": why})
    return {"standing": st, "merge_conflicts": conflicts, "problems": sorted(problems, key=json.dumps),
            "latest_file_reader": latest_file(snaps, model)}


def reading_gaps(records, policy_from="DEC-224"):
    """Legacy thresholds are numeric. A new-format adopted record must carry reading evidence itself:
    report the ones lifecycle/check_ledgers' numeric parse would skip and that lack it."""
    start = LC.num(policy_from)
    out = []
    for r in records:
        n = re.fullmatch(r"DEC-(\d+)", str(r.get("id", "")))
        legacy_parses = bool(n) and int(n.group(1)) >= start
        if r.get("status") == "adopted" and not legacy_parses and not isinstance(r.get("reading"), dict):
            out.append(r.get("id") or r.get("key"))
    return out


def models(cases):
    return {c["name"]: {"what": c["what"], "event": run_case(c, "event"), "version": run_case(c, "version")}
            for c in cases}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("pin"); a.add_argument("--repo", required=True); a.add_argument("--path", default="governance/decisions.jsonl")
    a.add_argument("--ref", default="main"); a.add_argument("-o", "--out")
    b = sub.add_parser("resolve"); b.add_argument("--pin", required=True); b.add_argument("refs", nargs="+")
    b.add_argument("--repo", help="the pinned repository, to resolve DEC-N@commit at any commit")
    c = sub.add_parser("standing"); c.add_argument("--repo", required=True); c.add_argument("--at", required=True)
    c.add_argument("--path", default="governance/decisions.jsonl")
    d = sub.add_parser("prefix"); d.add_argument("--old", required=True); d.add_argument("file"); d.add_argument("--lines", type=int, default=250)
    e = sub.add_parser("models"); e.add_argument("--cases", default=str(pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "dec_shadow_cases.json"))
    sub.add_parser("collisions").add_argument("--pin", required=True)
    v = sub.add_parser("view"); v.add_argument("legacy"); v.add_argument("records", help="JSON list of new-format records")
    v.add_argument("-o", "--out", required=True)
    a_ = ap.parse_args(argv)
    if a_.cmd == "pin":
        out = pin(a_.repo, a_.path, a_.ref)
        txt = json.dumps(out, indent=1, sort_keys=True)
        if a_.out:
            pathlib.Path(a_.out).write_text(txt + "\n", encoding="utf-8")
            print(f"pinned {out['commit']}: {out['lines']} lines, {out['commits_read']} commits, {len(out['index'])} ids")
        else:
            print(txt)
        return 0
    if a_.cmd in ("resolve", "collisions"):
        p = json.loads(pathlib.Path(a_.pin).read_text(encoding="utf-8"))
        res = [resolve_ref(p, r, a_.repo) for r in a_.refs] if a_.cmd == "resolve" else collisions(p)
        print(json.dumps(res, indent=1, sort_keys=True))
        return 1 if a_.cmd == "resolve" and any(r["result"] != "resolved" for r in res) else 0
    if a_.cmd == "view":
        data = view(pathlib.Path(a_.legacy).read_bytes(), json.loads(pathlib.Path(a_.records).read_text(encoding="utf-8")))
        pathlib.Path(a_.out).write_bytes(data)
        r = prefix_check(pathlib.Path(a_.legacy).read_bytes(), data, 250)
        print(json.dumps(r))
        return 0 if r["identical"] else 1
    if a_.cmd == "standing":
        print(json.dumps(standing(a_.repo, a_.at, a_.path), indent=1, sort_keys=True))
        return 0
    if a_.cmd == "prefix":
        r = prefix_check(pathlib.Path(a_.old).read_bytes(), pathlib.Path(a_.file).read_bytes(), a_.lines)
        print(json.dumps(r, indent=1))
        return 0 if r["identical"] else 1
    cases = json.loads(pathlib.Path(a_.cases).read_text(encoding="utf-8"))["cases"]
    print(json.dumps(models(cases), indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
