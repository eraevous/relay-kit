#!/usr/bin/env python3
"""Scan the files git tracks for private material before publishing (invariant P-07).

    python3 tools/prepublish_scan.py [--names-file .private-names]

Finds email addresses, links to real chat conversations, and API-token shapes. Names to
keep out (the owner's, assistants' nicknames) go one per line in a local, untracked
file (default .private-names, which .gitignore excludes), so the list itself is never
published. Diagnostics include location and category only, never matched values. This heuristic
scans tracked working-tree UTF-8 text, not history, untracked files or binary contents;
it is not a replacement for provider secret scanning. Exit code 1 if anything is
found. Standard library only."""
import argparse, json, pathlib, re, subprocess, sys

PATTERNS = {
    "email address": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"),
    "chat link": re.compile(r"https://(?:chatgpt\.com|chat\.openai\.com|claude\.ai)/(?:c|chat|share|g)/(?!0{8}-)[0-9a-f-]{8,}"),
    "token": re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,}|eyJ[A-Za-z0-9_-]{30,})"),
}
# Deliberately recognizable credential formats, not arbitrary high-entropy text.
PATTERNS.update({
    "GitHub credential": re.compile(r"\b(?:github_pat_[A-Za-z0-9_]{22,}|gh[ousr]_[A-Za-z0-9]{20,})\b"),
    "AWS access key ID": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "private key marker": re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
})
ALLOWED = {"noreply@anthropic.com"}   # commit-trailer address used in docs
# The kit's own public repository (its Homepage in pyproject.toml, and the same name with the
# leading hyphen it had before the rename): its address isn't private, and the @tag in an
# install line isn't an email address. Only that repository is exempt, not any */relay-kit.
def own_repo_pattern(root):
    try:
        text = (pathlib.Path(root) / "pyproject.toml").read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    m = re.search(r'Homepage\s*=\s*"https://github\.com/([\w.-]+)/-?([\w.-]+)"', text)
    if not m:
        return None
    owner, name = map(re.escape, m.groups())
    return re.compile(rf"(?:https://)?(?:github\.com/)?{owner}/-?{name}(?:@[\w./-]+)?(?![\w-])")   # not relay-kit-private
NAME_EXEMPT = {"LICENSE", "CLA.md"}   # the copyright holder is named there on purpose


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--names-file", default=".private-names")
    ap.add_argument("--root", default=".")
    a = ap.parse_args()
    root = pathlib.Path(a.root)
    names = []
    nf = root / a.names_file
    if nf.exists():
        names = [l.strip() for l in nf.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
    pats = dict(PATTERNS)
    if names:
        pats["private name"] = re.compile(r"\b(?:" + "|".join(map(re.escape, names)) + r")\b", re.I)
    public = own_repo_pattern(root)
    files = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, text=True, encoding="utf-8", check=True).stdout.split("\0")[:-1]
    hits = 0
    for f in files:
        p = root / f
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if public:
                line = public.sub("", line)
            for kind, rx in pats.items():
                if kind == "private name" and f in NAME_EXEMPT:
                    continue
                for m in rx.finditer(line):
                    if m.group(0) in ALLOWED:
                        continue
                    hits += 1
                    # Quote paths to keep newlines/control characters from forging log entries.
                    # No matched value or context: names and emails can be sensitive too.
                    print(f"{json.dumps(f, ensure_ascii=True)}:{n}: {kind}: [redacted]")
    print(f"{len(files)} tracked files scanned, {hits} finding(s)" + ("" if names else " (no .private-names file: names not checked)"))
    sys.exit(1 if hits else 0)


if __name__ == "__main__":
    main()
