#!/usr/bin/env python3
"""Make a collision-resistant id for a shared list (candidates, notes): PREFIX-LANE-XXXX,
or for a decision: DEC-YYYYMMDD-XXXX (DEC-272).

    python3 tools/new_id.py --prefix C --lane AEC [--avoid governance/candidates.md]
    python3 tools/new_id.py --decision [--avoid governance/decisions.jsonl]

Two sessions numbering one list independently produced duplicate ids on 2026-11-01
(C-11..C-13). A random Crockford base32 suffix needs no coordination: two sessions
pick the same one with probability about 1 in a million per pair. --avoid rereads a
file and retries if the id already appears there.

Decisions: DEC-001 to DEC-272 were one sequence (DEC-018), which collided whenever the previous
decision still sat in an unmerged branch. From DEC-272 on, a new decision takes a hash id like a
handoff@2 id: the UTC date of recording, then four random characters from the same alphabet.
--decision avoids governance/decisions.jsonl by default (when it exists under the current folder);
check_ledgers.py accepts both forms. Standard library only."""
import argparse, datetime as dt, pathlib, re, secrets, sys

B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--decision", action="store_true", help="a decision id, DEC-YYYYMMDD-XXXX (UTC date)")
    ap.add_argument("--prefix")
    ap.add_argument("--lane")
    ap.add_argument("--avoid", type=pathlib.Path, action="append", default=[])
    a = ap.parse_args()
    if a.decision:
        if a.prefix or a.lane:
            sys.exit("--decision takes no --prefix or --lane")
        head = "DEC-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d") + "-"
        default = pathlib.Path("governance") / "decisions.jsonl"
        if not a.avoid and default.exists():
            a.avoid = [default]
    else:
        for part, name in ((a.prefix, "--prefix"), (a.lane, "--lane")):
            if not part or not re.fullmatch(r"[A-Za-z0-9]{1,12}", part):
                sys.exit(f"{name} must be 1-12 letters or digits")
        head = f"{a.prefix.upper()}-{a.lane.upper()}-"
    seen = "".join(p.read_text(encoding="utf-8") for p in a.avoid if p.exists())
    for _ in range(50):
        new = head + "".join(secrets.choice(B32) for _ in range(4))
        if new not in seen:
            print(new)
            return
    sys.exit("couldn't find an unused id in 50 tries")


if __name__ == "__main__":
    main()
