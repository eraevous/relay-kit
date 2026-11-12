"""relay-kit: one command over the kit's tools. Standard library only.

    relay-kit init <folder> --owner <name> [--lanes relay-kit=RK,research=RS] [--kit-ref v0.1.0]
    relay-kit status [--root .] [...]                 tools/status.py
    relay-kit handoff [--dir handoffs] new|set|list|check [...]   the handoff-ledger skill's script
    relay-kit ledger check [--dir governance] [...]   tools/check_ledgers.py
    relay-kit ledger append [--dir governance] decision|challenge '<json>'   tools/ledger_append.py
    relay-kit new-id --prefix C --lane AEC [...]      tools/new_id.py
    relay-kit export --root RECORDS --out DIR [--lane L] [--actor A] [...]   tools/export.py (read-only)
    relay-kit cockpit --root RECORDS --out DIR [--lane L] [--content full|metadata] [...]   tools/cockpit.py (read-only)
    relay-kit budget-append --window session|weekly --tokens-in N --tokens-out N [...]   tools/budget_append.py
    relay-kit mcp --root RECORDS [--content full|metadata]   tools/mcp_server.py (MCP over stdio)
    relay-kit usage-ingest --session ID [--dir governance]   tools/usage_ingest.py (Claude Code session logs -> measured budget line)
    relay-kit prs --root RECORDS [--repo OWNER/NAME ...]   tools/prs.py (build-time GitHub PR reader; appends pr-state.jsonl)
    relay-kit edition new|check --root RECORDS [...]   tools/status_edition.py (hand-written status editions, relay-kit/status@1)
    relay-kit comments (--root RECORDS | --repo OWNER/NAME ...) (--since ISO | --state FILE)   tools/comments.py (GitHub comments since the last look; read-only)
    relay-kit zips [--out DIR] [--owner NAME --records OWNER/REPO]   one zip per skill (default ./relay-kit-upload)
    relay-kit --version

Each subcommand runs the same script the checkout runs (python3 tools/status.py and so on),
with the same arguments, output and exit code. Nothing else changes.
"""
import datetime as dt, json, pathlib, re, runpy, shutil, sys

try:
    from . import __version__
except ImportError:   # run as a file (python3 tools/cli.py) rather than as the package
    import re as _re
    __version__ = _re.search(r'__version__ = "([^"]+)"', (__import__("pathlib").Path(__file__).with_name("__init__.py")).read_text(encoding="utf-8")).group(1)

HERE = pathlib.Path(__file__).resolve().parent
SKILLS = HERE.parent / "skills" if (HERE.parent / "skills" / "handoff-ledger").is_dir() else HERE / "skills"
SCRIPTS = {
    "status": HERE / "status.py",
    "handoff": SKILLS / "handoff-ledger" / "scripts" / "handoff.py",
    "ledger check": HERE / "check_ledgers.py",
    "ledger append": HERE / "ledger_append.py",
    "new-id": HERE / "new_id.py",
    "export": HERE / "export.py",
    "cockpit": HERE / "cockpit.py",
    "prs": HERE / "prs.py",
    "edition": HERE / "status_edition.py",
    "comments": HERE / "comments.py",
    "budget-append": HERE / "budget_append.py",
    "usage-ingest": HERE / "usage_ingest.py",
    "telemetry": HERE / "telemetry.py",
    "mcp": HERE / "mcp_server.py",
    "zips": HERE / "extract.py",
}
KIT_REPO = "eraevous/relay-kit"


def run_script(path, args):
    """Run a tool exactly as `python3 <path> <args>` would: same argv, output and exit code."""
    saved = sys.argv
    sys.argv = [str(path)] + list(args)
    try:
        runpy.run_path(str(path), run_name="__main__")
    finally:
        sys.argv = saved


def init(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="relay-kit init", description="Create a records repository folder from the template.")
    ap.add_argument("folder", type=pathlib.Path)
    ap.add_argument("--owner", required=True, help="the person whose words authorize work (one word)")
    ap.add_argument("--lanes", default="", help="lane codes for handoff ids, e.g. relay-kit=RK,research=RS")
    ap.add_argument("--kit-ref", default=f"v{__version__}", help="relay-kit release tag (vX.Y.Z) or commit SHA the CI installs (default: this version's tag); branches are refused, since they move")
    ap.add_argument("--kit-repo", default=KIT_REPO, help="GitHub owner/name the CI installs relay-kit from")
    a = ap.parse_args(argv)
    owner = a.owner.strip().lower()
    if not re.fullmatch(r"[\w.:-]+", owner):
        sys.exit("refused: --owner must be one word (letters, digits, . : - _)")
    lanes = {}
    for pair in filter(None, (x.strip() for x in a.lanes.split(","))):
        name, _, code = (x.strip() for x in pair.partition("="))
        if not re.fullmatch(r"[\w.-]+", name) or not re.fullmatch(r"[A-Z0-9]{1,12}", code.upper()):
            sys.exit(f"refused: --lanes entry {pair!r} isn't name=CODE (name: letters, digits, . - _; CODE: 1-12 letters or digits)")
        if name.lower() in lanes or code.upper() in lanes.values():
            sys.exit(f"refused: --lanes repeats {name!r} or code {code.upper()!r}; each lane needs its own code")
        lanes[name.lower()] = code.upper()
    if not re.fullmatch(r"v\d+\.\d+\.\d+|[0-9a-f]{7,40}", a.kit_ref):
        sys.exit(f"refused: --kit-ref {a.kit_ref!r} must be a release tag (v1.2.3) or a commit SHA: a branch moves under you")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", a.kit_repo) or not re.fullmatch(r"[\w./-]+", a.kit_ref):
        sys.exit("refused: --kit-repo must be owner/name and --kit-ref a tag, branch or commit")
    if a.folder.is_symlink():
        sys.exit(f"refused: {a.folder} is a symlink; give the real folder, so nothing is written somewhere else")
    out = a.folder.resolve()
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        sys.exit(f"refused: {out} exists and isn't an empty folder; init only fills a new or empty one")
    template = HERE / "templates" / "records"
    now = dt.datetime.now(dt.timezone.utc)
    fill = {"{{OWNER}}": owner, "{{VERSION}}": __version__, "{{KIT_REF}}": a.kit_ref,
            "{{KIT_REPO}}": a.kit_repo, "{{DATE}}": now.strftime("%Y-%m-%d")}
    written = []
    for src in sorted(p for p in template.rglob("*") if p.is_file()):
        rel = pathlib.Path(*[("." + part[4:]) if part.startswith("dot-") else part
                              for part in src.relative_to(template).parts])
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        text = src.read_text(encoding="utf-8")
        for k, v in fill.items():
            text = text.replace(k, v)
        dst.write_text(text, encoding="utf-8", newline="\n")
        if src.stat().st_mode & 0o111 or rel.parts[0] == ".githooks":   # wheels may drop the exec bit
            dst.chmod(dst.stat().st_mode | 0o755)
        written.append(rel.as_posix())
    (out / "handoffs" / "outputs").mkdir(parents=True, exist_ok=True)
    (out / "handoffs" / "outputs" / ".gitkeep").write_text("", encoding="utf-8")   # so a clone has the folder
    written.append("handoffs/outputs/.gitkeep")
    if lanes:
        with open(out / "threads.md", "a", encoding="utf-8") as f:
            f.write("- lane codes (handoffs/.ledger.json): " + ", ".join(f"{n}={c}" for n, c in lanes.items()) + "\n")
    cfg = {"owner": owner, "lanes": lanes,
           "contract_since": {"relay-kit/handoff@2": now.strftime("%Y-%m-%dT%H:%MZ")}}
    (out / "handoffs" / ".ledger.json").write_text(json.dumps(cfg, indent=1) + "\n", encoding="utf-8")
    written.append("handoffs/.ledger.json")
    print(f"Created {out} (relay-kit {__version__}), owner '{owner}':")
    for w in written:
        print(f"  {w}")
    print(f"""
Next (init doesn't run git or create anything outside {out}):
  cd {out}
  git init -b main && git config core.hooksPath .githooks
  git add . && git commit -m "Records, from relay-kit init"
  # create a PRIVATE repository on GitHub, then: git remote add origin <url> && git push -u origin main
  export RELAYKIT_ACTOR=<assistant>:<lane>#<session id>
  relay-kit status --root .
  # skills that know these records: relay-kit zips --owner {owner} --records <owner/repo>""")


def zips(argv):
    """Build one upload zip per skill into a folder (default ./relay-kit-upload). For Claude Code,
    unzip them into ~/.claude/skills."""
    import argparse, importlib.util
    ap = argparse.ArgumentParser(prog="relay-kit zips", description=zips.__doc__)
    ap.add_argument("--out", type=pathlib.Path, default=pathlib.Path.cwd() / "relay-kit-upload")
    ap.add_argument("--owner", help="write the owner's name into the handoff-ledger zip, used where a handoffs folder has no .ledger.json")
    ap.add_argument("--records", help="the records repository (owner/name or URL), so a session without it is told where to get it")
    a = ap.parse_args(argv)
    settings = {}
    if a.owner:
        settings["owner"] = a.owner.strip().lower()
        if not re.fullmatch(r"[\w.:-]+", settings["owner"]):
            sys.exit("refused: --owner must be one word (letters, digits, . : - _)")
    if a.records:
        spec = importlib.util.spec_from_file_location("relaykit_handoff", SCRIPTS["handoff"])
        led = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(led)
        if not led.repo_id(a.records):
            sys.exit("refused: --records must be owner/name (GitHub) or a repository URL ending in owner/name")
        settings["records"] = a.records.strip()
    spec = importlib.util.spec_from_file_location("relaykit_extract", SCRIPTS["zips"])
    ext = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ext)
    try:
        made = ext.build_zips(a.out, root=SKILLS.parent, settings=settings or None)
    except ValueError as e:
        sys.exit(f"refused: {e}")
    print(f"{len(made)} skill zips in {a.out.resolve()}" + (f"; handoff-ledger.zip carries {settings}" if settings else ""))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    if argv[0] == "--version":
        print(f"relay-kit {__version__}")
        return
    cmd, rest = argv[0], argv[1:]
    if cmd == "init":
        return init(rest)
    if cmd == "ledger":
        if not rest or rest[0] not in ("check", "append"):
            sys.exit("usage: relay-kit ledger check|append [...]")
        cmd, rest = f"ledger {rest[0]}", rest[1:]
    if cmd not in SCRIPTS:
        sys.exit(f"relay-kit: unknown command {cmd!r}; try relay-kit --help")
    if cmd == "zips":
        return zips(rest)
    run_script(SCRIPTS[cmd], rest)


if __name__ == "__main__":
    main()
