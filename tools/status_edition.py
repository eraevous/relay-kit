#!/usr/bin/env python3
"""relay-kit/status@1: hand-written status editions, scaffolded and checked.

    relay-kit edition new --root RECORDS --edition morning [--date YYYY-MM-DD] [--author ACTOR] [--covers TEXT]
    relay-kit edition check --root RECORDS

An edition is one markdown file, status/<YYYY-MM-DD>-<edition>.md, written by a person or an agent
(the coordinator, usually) and rendered by the Cockpit as written. `new` writes the front matter
(date, edition, author, covers, contract) and the four section headings, Accomplishments / Road bumps /
Status / Next steps, and nothing else: the narrative is yours. It refuses to overwrite an existing
edition; editions are only ever added (HO-RK-20261106-NXPR: the coordinator owns status/, and a
published edition isn't edited).

`check` reads every file in status/ and reports names, front matter and sections that don't fit the
contract. Exit status 1 if anything is wrong. Standard library only; nothing else is read or written.
"""
import argparse, datetime as dt, importlib.util, os, pathlib, re, sys

CONTRACT = "relay-kit/status@1"
HERE = pathlib.Path(__file__).resolve().parent
EDITION_NAME = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ACTOR = re.compile(r"^[\w.:-]+(?:#[\w-]+)?$")


def _cockpit():
    spec = importlib.util.spec_from_file_location("relaykit_cockpit_for_status", HERE / "cockpit.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Refused(Exception):
    pass


def scaffold(date, edition, author, covers):
    return (f"---\ndate: {date}\nedition: {edition}\nauthor: {author}\ncovers: {covers}\ncontract: {CONTRACT}\n---\n\n"
            f"# Status · {date}, {edition}\n\n"
            "<!-- Written by hand, for the owner's reading. The Cockpit renders this file as written: headings, lists,\n"
            "     bold, italic, inline code and rules; links are shown as text. Nothing here is summarised by a page. -->\n\n"
            "## Accomplishments\n\n- \n\n## Road bumps\n\n- \n\n## Status\n\n- \n\n## Next steps\n\n**Yours:**\n1. \n\n**Coordinator:**\n1. \n")


def one_line(name, value):
    """A front-matter value is one line of printable text: a line break could add a key (a second author:
    overrides the first) or end the front matter early (Kate 2 on #33). Refused, not encoded."""
    if value is not None and any(not ch.isprintable() and ch != "\t" for ch in value):
        raise Refused(f"{name} must be one line of printable text (no line breaks or control characters)")
    return value


def new(root, edition, date, author, covers):
    root = pathlib.Path(root)
    if not root.is_dir() or not (root / "governance").is_dir():
        raise Refused(f"--root {root} isn't a records store (no governance/ folder)")
    if not EDITION_NAME.fullmatch(edition):
        raise Refused(f"--edition {edition!r} must be lower-case letters, digits and hyphens (morning, midday, evening)")
    if not DATE.fullmatch(date):
        raise Refused(f"--date {date!r} must be YYYY-MM-DD")
    dt.date.fromisoformat(date)
    one_line("--covers", covers)
    if not author or not ACTOR.fullmatch(author):
        raise Refused("--author (or RELAYKIT_ACTOR) must be the writer, like claude:kairos-coord#01AB or the owner")
    d = root / "status"
    if d.is_symlink():
        raise Refused("status/ is a symlink; refusing to write through it")
    d.mkdir(exist_ok=True)
    path = d / f"{date}-{edition}.md"
    taken = f"{path.relative_to(root)} exists; editions are added, never rewritten (pick another edition name)"
    if path.is_symlink():   # exists() is False for a dangling link, and writing would follow it (Kate 3 on #33)
        raise Refused(f"{path.relative_to(root)} is a symlink; refusing to write through it")
    if path.exists():
        raise Refused(taken)
    try:   # exclusive creation: a file or link that appears after the checks above is refused, not followed
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    except FileExistsError:
        raise Refused(taken)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(scaffold(date, edition, author, covers or f"up to {date}"))
    return path


def check(root):
    """Problems per file, from the Cockpit's own reader, so a file that passes here renders there."""
    ck = _cockpit()
    st = ck.read_status(pathlib.Path(root), full=True)
    if not st["present"]:
        return [("status/", "no status/ folder")]
    out = [(u["file"], u["reason"]) for u in st["unreadable"]]
    for ed in st["editions"]:
        for p in ed["problems"]:
            out.append((ed["file"], p))
        if ed["sections_missing"]:
            out.append((ed["file"], "sections missing: " + ", ".join(ed["sections_missing"])))
        if not ed.get("contract"):   # required by the contract (docs/status.md); missing or empty fails (Kate 5 on #33)
            out.append((ed["file"], f"no contract in front matter (expected {CONTRACT})"))
        elif ed["contract"].split(" ")[0] not in (CONTRACT, "kairos/status@0"):
            out.append((ed["file"], f"contract {ed['contract']!r} isn't {CONTRACT}"))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="relay-kit edition", description=__doc__.split("\n\n")[1], formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new", help="write the front matter and the four headings of a new edition")
    n.add_argument("--root", required=True, type=pathlib.Path, help="the records store")
    n.add_argument("--edition", required=True, help="morning, midday, evening, or another short lower-case name")
    n.add_argument("--date", help="YYYY-MM-DD (default: today, local time)")
    n.add_argument("--author", help="who writes it, with a session (default $RELAYKIT_ACTOR)")
    n.add_argument("--covers", help="the period the edition covers, in your words")
    c = sub.add_parser("check", help="report editions that don't fit the contract")
    c.add_argument("--root", required=True, type=pathlib.Path)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "new":
            p = new(a.root, a.edition, a.date or dt.date.today().isoformat(), a.author or os.environ.get("RELAYKIT_ACTOR"), a.covers)
            print(f"wrote {p}; fill in the four sections, then commit it. The Cockpit shows the newest edition on its Status surface.")
        else:
            probs = check(a.root)
            for f, why in probs:
                print(f"{f}: {why}")
            if probs:
                sys.exit(1)
            print("status/: every edition fits relay-kit/status@1")
    except Refused as x:
        sys.exit(f"refused: {x}")


if __name__ == "__main__":
    main()
