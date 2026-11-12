#!/usr/bin/env python3
"""Read-only export of what a records store holds about a lane: relay-kit/export@1.

    python3 tools/export.py --root RECORDS --out DIR [--lane AEC] [--actor claude:aec[#SESSION] ...]
                            [--since ISO] [--until ISO]
                            [--kinds conversations,handoffs,decisions,challenges,outputs]
                            [--granularity inventory|message] [--format jsonl|json]
                            [--content full|metadata|none] [--allow-public-dest]

Reads a records folder laid out like the kit's private repo:
  governance/decisions.jsonl, governance/challenges.jsonl   append-only ledgers
  handoffs/HO-*.md, handoffs/.ledger.json                   handoffs (relay-kit/handoff@1 or @2)
  handoffs/outputs/*.md                                     filed outputs
  threads.md                                                the coordination index
  any *.json whose schema is relay-kit/conversation@1      captured conversations

and writes two files into DIR:
  manifest.json            what was queried, counts, time bounds, hashes, filters, exclusions and
                           coverage gaps (arms referenced in the records that have no capture,
                           handoffs without outputs, hidden steps, records without timestamps)
  records.jsonl (or .json) one record per exported item

It never writes inside ROOT, never changes a source file, and refuses a destination inside a
git repository whose remote is a public GitHub repository (or whose visibility it can't
confirm) unless --allow-public-dest is passed. Spec: docs/export.md. Standard library only."""
import argparse, datetime as dt, errno, hashlib, importlib.util, json, os, pathlib, re, shutil, stat, subprocess, sys, tempfile, urllib.error, urllib.request

SCHEMA = "relay-kit/export@1"
CONVERSATION = "relay-kit/conversation@1"
PASTE_MARKERS = "relay-kit/paste-markers@1"
HANDOFF_CONTRACTS = ["relay-kit/handoff@1", "relay-kit/handoff@2"]
KINDS = ["conversations", "handoffs", "outputs", "decisions", "challenges"]
KIND_ORDER = {"conversation": 0, "message": 1, "handoff": 2, "output": 3, "decision": 4, "challenge": 5}
EVIDENCE = ["source-grounded", "derived", "decision", "proposal", "relay"]
CONTENT_POLICY = {
    "full": "Exact source text is included wherever the source has it.",
    "metadata": "Free text is left out (message text, titles, handoff bodies, log notes, ledger prose); "
                "each suppressed text keeps its sha256 and length so it can be matched to the source later.",
    "none": "Free text is left out and so are its hashes and lengths; only ids, structure, actors, lanes, "
            "states and timestamps remain.",
}
# free-text fields, by record kind, that --content metadata|none suppresses
TEXT_FIELDS = {
    "conversation": ["title"],
    "message": ["text", "sources", "label", "relay.origin_label", "relay.origin_ref"],
    "handoff": ["title", "text", "log[].note"],
    "output": ["text", "first_line"],
    "decision": ["decision", "scope", "basis", "locator", "evidence", "note", "not_covered", "(any other string field)"],
    "challenge": ["concern", "resolution", "check", "fix", "scope", "evidence", "(any other string field)"],
}
LEDGER_STRUCTURAL = {"id", "date", "status", "decider", "recorded_by", "raised_by", "proposed_by", "supersedes", "resolves"}
# amends, revokes and effective dates can hold free text: in metadata mode they are hashed like prose; the
# record's derived fields (closed_by, amended_by, applicability) carry only ids and states
KNOWN_FIELDS = {
    "decision": {"id", "date", "status", "decision", "scope", "decider", "basis", "locator", "evidence",
                 "proposed_by", "note", "recorded_by", "not_covered", "supersedes", "ask", "amends",
                 "revokes", "effective_from", "effective_until"},
    "challenge": {"id", "date", "status", "concern", "raised_by", "resolution", "check", "fix", "resolves",
                  "scope", "evidence", "recorded_by", "ask", "kind", "about", "addressed_to"},
    # conversation@1 as chat-capture's renderer writes it, plus the optional actor/lane this export reads
    "conversation": {"schema", "meta", "hidden_counts", "turns"},
    "conversation.meta": {"title", "platform", "kind", "source_url", "conversation_id", "share_id", "record_created",
                          "record_updated", "first_message_at", "last_message_at", "retrieved_at", "retrieval",
                          "default_model", "models_seen", "turns", "user_turns", "assistant_turns", "visible_messages",
                          "hidden_items", "nodes_total", "dictated_user_turns", "raw_sha256", "timezone", "checks",
                          "actor", "lane"},
    "conversation.turn": {"n", "role", "label", "messages"},
    "conversation.message": {"mid", "id", "ts", "model", "dictated", "text", "sources"},
}
LIMITS = [
    "Only files under --root are read. Platform histories that were never captured (ChatGPT, Claude, "
    "Codex, subagents, other tools) are not reachable and appear as coverage gaps, never as empty conversations.",
    "conversation@1 captures count hidden events (tool calls, reasoning) in hidden_counts; they are not listed "
    "and this export can't reconstruct them.",
    "A capture holds the active branch only; other branches are not exported.",
    "Relays are detected from paste markers (relay-kit/paste-markers@1: a user message opening with FROM:); "
    "unmarked pastes look like the user's own words.",
    "Actor and lane come from the capture's meta when present, else from threads.md or a session id in the "
    "records (lane_source / actor_source say which); the renderer doesn't write them, so many captures have neither.",
    "Evidence classes describe the record's relation to its source (verbatim, derived, ledger status), not "
    "whether what it says is true. A ledger status is reported, never inferred.",
    "Conversation Markdown files are not read; the JSON is the canonical capture.",
]
ASSISTANTS = ["claude", "chatgpt", "codex", "gemini", "copilot"]
FROM_LINE = re.compile(r"^\W*FROM:\s*\**\s*([^\n*→>]+?)\s*(?:→|->|\*|$)", re.I)   # relay-kit/paste-markers@1
PASTE = re.compile(r'^>\s*["“”]{3}|^\W*FROM:\s', re.M)
REF = re.compile(r"\bT\d+(?:u|a)\d*\b|\bHO-[A-Z0-9-]+\b|https?://\S+")
URL = re.compile(r"https://(?:chatgpt\.com|chat\.openai\.com|claude\.ai)/[^\s)>\]|`\"']+")
OUT_FILES = {"manifest.json", "records.jsonl", "records.json"}

HERE = pathlib.Path(__file__).resolve().parent


def load_github():
    spec = importlib.util.spec_from_file_location("relaykit_github_for_export", HERE / "github.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


GH = load_github()


def load_lifecycle():
    spec = importlib.util.spec_from_file_location("relaykit_lifecycle", HERE / "lifecycle.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m
SKILLS = HERE.parent / "skills" if (HERE.parent / "skills" / "handoff-ledger").is_dir() else HERE / "skills"
LEDGER = SKILLS / "handoff-ledger" / "scripts" / "handoff.py"
try:
    from . import __version__
except ImportError:
    __version__ = re.search(r'__version__ = "([^"]+)"', (HERE / "__init__.py").read_text(encoding="utf-8")).group(1)


class Refused(Exception):
    pass


def load_ledger():
    spec = importlib.util.spec_from_file_location("relaykit_handoff_export", LEDGER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def sha(b):
    return hashlib.sha256(b if isinstance(b, bytes) else b.encode("utf-8")).hexdigest()


def dumps(x):
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# ---------------- time ----------------
def span(ts):
    """ISO timestamp or date -> (start, end) in UTC, or None. A date covers its whole day."""
    if not ts or not isinstance(ts, str):
        return None
    s = ts.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        try:   # shape-valid but impossible (2026-13-03) is no date, like any other unparsable value (Kate 4 on #33)
            d = dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            return None
        return d, d + dt.timedelta(days=1) - dt.timedelta(microseconds=1)
    s = s.replace("Z", "+00:00")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?:[+-]\d{2}:\d{2})?", s):   # no seconds (handoff times)
        s = s[:16] + ":00" + s[16:]
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    d = d.astimezone(dt.timezone.utc)
    return d, d


def bound(x, name):
    if x is None:
        return None
    sp = span(x)
    if not sp:
        raise Refused(f"--{name} {x!r} isn't an ISO date or time (2026-11-01 or 2026-11-01T12:00Z)")
    return sp[0] if name == "since" else sp[1]


# ---------------- actors and lanes ----------------
def split_actor(a):
    """'claude:aec#01N4 for codex:aec' -> [('claude:aec', '01N4'), ('codex:aec', None)]. Non-actors -> []."""
    out = []
    for part in re.split(r"\s+for\s+", str(a or "").strip()):
        m = re.fullmatch(r"([A-Za-z][\w.-]*):([\w.-]+)(?:#([\w-]+))?", part.strip())
        if m:   # the assistant and lane are case-insensitive names; a session id keeps its case
            out.append((f"{m.group(1).lower()}:{m.group(2).lower()}", m.group(3)))
    return out


def actor_str(base, session):
    return f"{base}#{session}" if session else base


def url_segments(url):
    """The path segments of a link, with claude.ai's 'session_' prefix removed, for whole-segment matching."""
    segs = [x for x in re.split(r"[/?#]", re.sub(r"^\w+://[^/]+", "", url or "")) if x]
    return set(segs) | {x[len("session_"):] for x in segs if x.startswith("session_")}


class Lanes:
    """Lane names declared by handoffs/.ledger.json or threads.md, and their codes. An actor's
    name part ('nova' in chatgpt:nova) is a lane only when it is declared (or is a declared lane's
    unique code); otherwise the actor is reported without a lane."""
    def __init__(self, codes):
        self.codes = {k.lower(): str(v).upper() for k, v in (codes or {}).items()}
        self.declared = set(self.codes)

    def declare(self, name):
        if name:
            self.declared.add(name.lower())

    def lane(self, base):
        if not base or ":" not in base:
            return None
        name = base.split(":", 1)[1].lower()
        if name in self.declared:
            return name
        by_code = self.from_code(name)
        return by_code[0] if len(by_code) == 1 else None

    def matches(self, query, lane):
        """--lane AEC matches lane 'aec' and any lane whose code is AEC (aec-release)."""
        if not lane:
            return False
        q, l = query.lower(), lane.lower()
        return l == q or self.codes.get(l, "").lower() == q or (self.codes.get(q) and self.codes.get(q) == self.codes.get(l))

    def from_code(self, code):
        return sorted(n for n, c in self.codes.items() if c == code.upper())


def actor_matches(filters, actors):
    """claude:aec matches every session of claude:aec; claude:aec#01N4 matches sessions starting 01N4."""
    for f in filters:
        for base, sess in split_actor(f):
            for a in actors:
                for ab, asess in split_actor(a):
                    if ab == base and (not sess or (asess and asess.startswith(sess))):
                        return True
    return False


# ---------------- destination safety ----------------
GIT = "git"


def git_dirs(start):
    """Every enclosing folder with a .git, nearest first: [(folder, git config path or None)]."""
    out, p = [], start
    while True:
        g = p / ".git"
        if g.exists():
            cfg = None
            if g.is_dir():
                cfg = g / "config"
            else:
                m = re.match(r"gitdir:\s*(.+)", g.read_text(encoding="utf-8", errors="replace").strip())
                if m:
                    gd = (p / m.group(1).strip()).resolve()
                    common = gd / "commondir"
                    base = (gd / common.read_text(encoding="utf-8").strip()).resolve() if common.exists() else gd
                    cfg = base / "config"
            out.append((p, cfg if cfg and cfg.exists() else None))
        if p.parent == p:
            return out
        p = p.parent


def remote_urls(cfg):
    """Remote URLs in a git config, with the same file's url.<base>.insteadOf rewrites applied."""
    urls, rewrites, section, base = [], [], None, None
    for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if s.startswith("["):
            m = re.match(r'\[\s*(\w+)(?:\s+"([^"]*)")?\s*\]', s)
            section, base = (m.group(1).lower(), m.group(2)) if m else (None, None)
        elif section == "remote":
            m = re.match(r"(?:push)?url\s*=\s*(.+)", s, re.I)
            if m:
                urls.append(m.group(1).strip().strip('"'))
        elif section == "url" and base is not None:
            m = re.match(r"(?:push)?insteadof\s*=\s*(.+)", s, re.I)
            if m:
                rewrites.append((m.group(1).strip().strip('"'), base))
    out = []
    for u in urls:
        hits = sorted((p for p in rewrites if u.startswith(p[0])), key=lambda p: -len(p[0]))
        out.append(hits[0][1] + u[len(hits[0][0]):] if hits else u)
    return out


def effective_remotes(folder, cfg):
    """The repository's remote URLs as git itself resolves them (include/includeIf, insteadOf, global
    config), or None when they can't be determined. Without git, the local config is read only if it
    has no include sections; otherwise None."""
    try:
        p = subprocess.run([GIT, f"--git-dir={folder / '.git'}", "remote", "-v"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=15, env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    except FileNotFoundError:
        if cfg is None or re.search(r"^\s*\[\s*include", cfg.read_text(encoding="utf-8", errors="replace"), re.M | re.I):
            return None
        return remote_urls(cfg)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    return sorted({l.split()[1] for l in p.stdout.splitlines() if len(l.split()) >= 2})


def github_repo(url):
    m = re.match(r"^(?:https?://(?:[^@/]+@)?github\.com/|ssh://git@github\.com(?::\d+)?/|git@github\.com:)([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", url)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def authenticated_visibility(repo):
    """'private' or 'public' as GitHub reports it to an authenticated caller (GH_TOKEN or GITHUB_TOKEN,
    else the gh command), or None when no credentials can say."""
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        try:
            status, _, body = GH.token_transport(token)(f"repos/{repo}", {})
            if status == 200:
                v = json.loads(body).get("private")
                return "private" if v is True else ("public" if v is False else None)
        except Exception:
            pass
    if shutil.which("gh"):
        try:
            p = subprocess.run(["gh", "api", f"repos/{repo}", "--jq", ".private"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
            if p.returncode == 0 and p.stdout.strip() in ("true", "false"):
                return "private" if p.stdout.strip() == "true" else "public"
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def repo_visibility(repo):
    """'public', 'private' or 'unknown'. An unauthenticated 200 means anyone can read the repository.
    Privacy counts only when an authenticated call says so: a 404 without credentials may also mean
    renamed, deleted or unreachable, so on its own it is 'unknown'."""
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "relay-kit-export"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            json.load(r)
            return "public"
    except Exception:
        pass
    return authenticated_visibility(repo) or "unknown"


def check_destination(out, root, allow_public):
    if out.is_symlink():
        raise Refused(f"{out} is a symlink; give the real folder")
    o = out.resolve()
    r = root.resolve()
    if o == r or r in o.parents:
        raise Refused(f"{o} is inside the records root {r}; an export never writes into the records it reads")
    existing = o
    while not existing.exists():
        existing = existing.parent
    found = []
    for folder, cfg in git_dirs(existing):
        if cfg is None:   # a .git we can't read: its remotes are unknown
            found.append({"repo_folder": str(folder), "remote": "(git config unreadable)", "visibility": "unknown"})
            continue
        urls = effective_remotes(folder, cfg)
        if urls is None:   # git couldn't resolve them (broken repo, unresolved include): unknown
            found.append({"repo_folder": str(folder), "remote": "(remotes couldn't be resolved)", "visibility": "unknown"})
            continue
        for u in urls:
            # only a github.com remote can be checked; anything else (GitLab, an ssh host alias,
            # an unresolved insteadOf alias) can't be confirmed private, so it counts as unknown
            repo = github_repo(u)
            vis = repo_visibility(repo) if repo else "unknown"
            found.append({"repo_folder": str(folder), "remote": repo or "(unrecognized remote)", "visibility": vis})
        if not urls:
            found.append({"repo_folder": str(folder), "remote": None, "visibility": "no remote"})
    risky = [f for f in found if f["visibility"] in ("public", "unknown")]
    if risky and not allow_public:
        why = "; ".join(f"{f['repo_folder']} has remote {f['remote']} ({f['visibility']})" for f in risky)
        raise Refused(f"{o} is inside a git repository that is public or whose visibility couldn't be confirmed ({why}). "
                      "Exports can hold private transcripts: write outside any public repository, or pass "
                      "--allow-public-dest if you are sure.")
    if found:
        print(f"note: {o} is inside a git repository ({found[0]['repo_folder']}); don't commit the export by accident",
              file=sys.stderr)
    # local paths stay out of the manifest: the out folder's name, and each repository relative to it
    for f in found:
        f["repo_folder"] = os.path.relpath(f["repo_folder"], o)
    return {"name": o.name, "inside_git": found, "allow_public_dest": bool(allow_public)}


# ---------------- reading the records ----------------
class Reader:
    def __init__(self, root):
        self.root = root.resolve()
        self._root_fd = None
        if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
            self._root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.files = {}          # relpath -> {"kind", "sha256", "status"}
        self.unreadable = []
        self.symlinks = []
        self.non_conversation_json = 0
        self.raw_captures = []   # chat-capture's raw downloads, not yet rendered to conversation@1
        self.unknown_fields = {}
        self.lanes = Lanes({})
        self.session_actor = {}  # session id -> set of actor strings seen with it
        self.referenced = {}     # base actor -> {"sessions": set, "in": set}

    def rel(self, p):
        return p.relative_to(self.root).as_posix()

    def __del__(self):
        fd = getattr(self, "_root_fd", None)
        if fd is not None:
            os.close(fd)
            self._root_fd = None

    def read_bytes(self, p):
        """Read under the selected root, refusing symlink traversal on POSIX.

        Platforms without descriptor-relative no-follow opens use a resolved-path
        boundary check requiring a trusted, stable filesystem; that fallback does
        not protect against concurrent renames and is not POSIX security parity.
        """
        try:
            parts = p.relative_to(self.root).parts
        except ValueError:
            raise Refused("record path is outside the records root; refusing to read it")
        if not parts or any(part in ("..", ".") for part in parts):
            raise Refused("record path is outside the records root; refusing to read it")
        if self._root_fd is None:
            resolved = p.resolve()
            if resolved == self.root or self.root not in resolved.parents:
                raise Refused("record path resolves outside the records root; refusing to read it")
            if not stat.S_ISREG(resolved.stat().st_mode):
                raise Refused("record path is not a regular file; refusing to read it")
            return resolved.read_bytes()
        parent = os.dup(self._root_fd)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                os.close(parent)
                parent = child
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as f:
                if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                    raise Refused("record path is not a regular file; refusing to read it")
                return f.read()
        except OSError as e:
            if e.errno in (errno.ELOOP, errno.ENOTDIR):
                raise Refused("record path traverses a symlink or non-directory outside the records root; refusing to read it") from e
            raise  # preserve callers' handling of missing or unreadable records
        finally:
            os.close(parent)

    def read(self, p, kind):
        b = self.read_bytes(p)
        self.files[self.rel(p)] = {"kind": kind, "sha256": sha(b), "bytes": len(b)}
        return b

    def bad(self, p, why):
        self.unreadable.append({"path": self.rel(p) if isinstance(p, pathlib.Path) else p, "reason": why})

    def unknown(self, obj, kind):
        for k in set(obj) - KNOWN_FIELDS[kind]:
            self.unknown_fields.setdefault(kind, set()).add(k)

    def note_actor(self, a, where):
        for base, sess in split_actor(a):
            r = self.referenced.setdefault(base, {"sessions": set(), "in": set()})
            r["in"].add(where)
            if sess:
                r["sessions"].add(sess)
                self.session_actor.setdefault(sess, set()).add(base)

    def config(self):
        p = self.root / "handoffs" / ".ledger.json"
        if p.is_file() and not p.is_symlink():
            try:
                cfg = json.loads(self.read(p, "config"))
                self.lanes = Lanes(cfg.get("lanes") or {})
            except (ValueError, AttributeError) as e:
                self.bad(p, f"not a JSON object: {e}")

    def walk_json(self):
        for d, dirs, files in os.walk(self.root, followlinks=False):
            dp = pathlib.Path(d)
            for x in sorted(dirs):
                if (dp / x).is_symlink():
                    self.symlinks.append(self.rel(dp / x))
            dirs[:] = sorted(x for x in dirs if x != ".git" and not (dp / x).is_symlink())
            for f in sorted(files):
                p = dp / f
                if not f.endswith(".json"):
                    continue
                if p.is_symlink():
                    self.symlinks.append(self.rel(p))
                    continue
                yield p

    def ledger(self, name, kind):
        p = self.root / "governance" / name
        if not p.is_file() or p.is_symlink():
            return None
        raw = self.read(p, kind)
        rows = []
        for n, line in enumerate(raw.split(b"\n"), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("not an object")
            except ValueError as e:
                self.bad(f"{self.rel(p)}:{n}", f"not a JSON object: {e}")
                continue
            self.unknown(row, kind)
            for f in ("recorded_by", "raised_by", "proposed_by"):
                self.note_actor(row.get(f), self.rel(p))
            rows.append((n, line, row))
        return p, rows


def relay_info(m, role):
    """A user message whose first line is FROM: <source> is relayed material (paste-markers@1)."""
    text = m.get("text") if isinstance(m.get("text"), str) else ""
    first = next((l for l in text.split("\n") if l.strip()), "")
    if role == "user":
        f = FROM_LINE.match(first)
        if f:
            label = f.group(1).strip().rstrip(":")
            refs = REF.findall(first)
            return {"origin_label": label, "origin_ref": refs[0] if refs else None, "marker": PASTE_MARKERS}, False
    return None, bool(PASTE.search(text))


def conv_records(rd, p, raw, d, threads_by_id):
    meta = d.get("meta") if isinstance(d.get("meta"), dict) else {}
    turns = d.get("turns")
    if not isinstance(turns, list):
        raise ValueError("no turns list")
    cid = meta.get("conversation_id") or meta.get("share_id")
    fh = sha(raw)
    key = f"{meta.get('platform') or 'unknown'}:{cid}" if cid else f"file:{fh[:16]}"
    url = meta.get("source_url") or ""
    segs = url_segments(url) | ({cid} if cid else set())
    # actor and lane: the capture's own meta first, then the records
    actor, actor_source = meta.get("actor"), "capture" if meta.get("actor") else None
    if not actor:
        for sess, bases in sorted(rd.session_actor.items()):
            if sess in segs and len(bases) == 1:
                actor, actor_source = actor_str(next(iter(bases)), sess), "records session id"
                break
    lane, lane_source = meta.get("lane"), "capture" if meta.get("lane") else None
    if not lane:
        t = threads_by_id.get(cid) if cid else None
        if t is None:
            t = next((v for k, v in sorted(threads_by_id.items()) if k and k in segs), None)
        if t and t.get("lane"):
            lane, lane_source = t["lane"], "threads.md"
        elif actor:
            lane, lane_source = rd.lanes.lane(split_actor(actor)[0][0]) if split_actor(actor) else None, "actor"
    rd.unknown(d, "conversation")
    rd.unknown(meta, "conversation.meta")
    for t in turns:
        if isinstance(t, dict):
            rd.unknown(t, "conversation.turn")
            for m in t.get("messages") or []:
                if isinstance(m, dict):
                    rd.unknown(m, "conversation.message")
    hidden = d.get("hidden_counts") if isinstance(d.get("hidden_counts"), dict) else {}
    msgs, seq, models, tss = [], 0, set(), []
    for t in turns:
        if not isinstance(t, dict):
            raise ValueError("a turn isn't an object")
        role = t.get("role")
        for k, m in enumerate(t.get("messages") or [], 1):
            seq += 1
            mid = m.get("mid")
            derived = not mid
            if derived:
                mid = f"T{t.get('n')}" + ("u" if role == "user" else "a") + (str(k) if k > 1 else "")
            relay, contains_paste = relay_info(m, role)
            text = m.get("text") if isinstance(m.get("text"), str) else None
            if m.get("model"):
                models.add(m["model"])
            if m.get("ts"):
                tss.append(m["ts"])
            msgs.append({
                "kind": "message", "seq": seq, "turn": t.get("n"), "role": role, "label": t.get("label"),
                "mid": mid, "mid_derived": derived, "id": m.get("id"), "ts": m.get("ts"), "model": m.get("model"),
                "dictated": m.get("dictated"), "text": text, "sources": m.get("sources"),
                "relay": relay, "contains_paste": contains_paste,
                "evidence_class": "relay" if relay else "source-grounded",
            })
    conv = {
        "kind": "conversation", "conversation_key": key, "conversation_id": cid, "platform": meta.get("platform"),
        "title": meta.get("title"), "schema_read": d.get("schema") or f"{CONVERSATION} (no schema field: pre-schema capture)",
        "messages": len(msgs), "relayed_messages": sum(1 for m in msgs if m["relay"]),
        "hidden_counts": hidden, "hidden_total": sum(v for v in hidden.values() if isinstance(v, int)),
        "checks": meta.get("checks"), "models_seen": sorted(models | set(meta.get("models_seen") or [])),
        "raw_sha256": meta.get("raw_sha256"), "first_message_at": meta.get("first_message_at") or (min(tss) if tss else None),
        "last_message_at": meta.get("last_message_at") or (max(tss) if tss else None),
        "ts": meta.get("first_message_at") or (min(tss) if tss else None),
        "evidence_class": "derived",
    }
    common = {"source_path": rd.rel(p), "source_hash": fh, "lane": lane, "lane_source": lane_source,
              "actor": actor, "actor_source": actor_source, "actors": [actor] if actor else [],
              "lanes": [lane] if lane else []}
    conv.update(common, rid=f"conv:{key}")
    for m in msgs:
        m.update(common, rid=f"msg:{key}:{m['mid']}", conversation_rid=conv["rid"])
    return conv, msgs


def parse_threads(rd):
    """threads.md -> [{lane, ref, conversation_id, platform, status}], and every actor it names."""
    p = rd.root / "threads.md"
    if not p.is_file() or p.is_symlink():
        return None, {}
    text = rd.read(p, "threads").decode("utf-8", errors="replace")
    section, lane, out = None, None, {}
    actor_re = re.compile(r"\b((?:" + "|".join(ASSISTANTS) + r"):[\w.-]+(?:#[\w-]+)?)")
    for line in text.splitlines():
        if line.startswith("## "):
            section, lane = line[3:].strip().lower(), None
        elif line.startswith("### ") and section == "lanes":
            h = line[4:].strip()
            cands = [h] + re.findall(r"\(([^)]+)\)", h) + h.split()[:1]
            lane = next((n for c in cands for n in ([c.lower()] if c.lower() in rd.lanes.codes else rd.lanes.from_code(c))), None) \
                or h.lower()
            rd.lanes.declare(lane)   # a lane heading in threads.md declares the lane
        actors = actor_re.findall(line)
        for a in actors:
            rd.note_actor(a, "threads.md")
        line_lane = lane if section == "lanes" else (rd.lanes.lane(split_actor(actors[0])[0][0]) if actors and split_actor(actors[0]) else None)
        for u in URL.findall(line):
            u = u.rstrip(".,;:")
            m = re.search(r"/(?:c|chat|share)/([\w-]+)|/code/session_([\w-]+)|/code/project/([\w-]+)", u)
            if not m:
                continue
            cid = next(g for g in m.groups() if g)
            if cid in out:
                continue
            plat = "chatgpt" if "chatgpt.com" in u or "openai.com" in u else ("claude-code" if "/code/" in u else "claude")
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            status = cells[-1].strip("* ").lower() if section == "lanes" and len(cells) >= 3 else None
            out[cid] = {"lane": line_lane, "ref": u, "conversation_id": cid, "platform": plat,
                        "status": status or None, "listed_in": "lanes table" if section == "lanes" else section}
    for m in re.finditer(r"chatgpt\.com/local/([\w-]+)", text):   # Codex local threads
        out.setdefault(m.group(1), {"lane": None, "ref": m.group(0), "conversation_id": m.group(1),
                                    "platform": "codex-local", "status": None, "listed_in": "threads.md"})
    return p, out


def handoff_records(rd, led):
    d = rd.root / "handoffs"
    recs, gaps = [], []
    if not d.is_dir():
        return None, recs, gaps
    id_re = re.compile(rf"^{led.ID_PAT}$")
    for p in sorted(d.glob("HO-*.md")):
        if p.is_symlink():
            rd.symlinks.append(rd.rel(p))
            continue
        raw = rd.read(p, "handoff")
        text = raw.decode("utf-8", errors="replace")
        try:
            meta, log, sections, _ = led.parse_text(text, p.stem)
        except led.Refused as e:
            rd.bad(p, str(e))
            continue
        missing = [f for f in ("id", "status", "from", "created") if not meta.get(f)]
        if missing or not sections:
            rd.bad(p, "not a readable handoff: " + (f"front matter lacks {', '.join(missing)}" if missing else "no sections"))
            continue
        hid = meta.get("id")
        actors = []
        for f in ("from", "to"):
            rd.note_actor(meta.get(f), rd.rel(p))
        for e in log:
            rd.note_actor(e["actor"], rd.rel(p))
            for b, s in split_actor(e["actor"]):
                actors.append(actor_str(b, s))
        for f in ("from", "to"):
            for b, s in split_actor(meta.get(f)):
                actors.append(actor_str(b, s))
        lanes = {l for a in actors for b, _ in split_actor(a) for l in [rd.lanes.lane(b)] if l}
        frm = split_actor(meta.get("from"))
        lane = rd.lanes.lane(frm[0][0]) if frm else None
        m = re.match(r"^HO-([A-Z0-9]{1,12})-\d{8}-\w{4}$", hid)
        if m:   # the id's lane code: one lane with that code is that lane; a shared code adds nothing
            by_code = rd.lanes.from_code(m.group(1))
            if len(by_code) == 1:
                lanes.add(by_code[0])
                lane = lane if lane == by_code[0] else (lane or by_code[0])
        lanes = sorted(lanes)
        expected = [f"{p.stem}.md"]   # the file handoff.py's completed check requires
        present = [x for x in expected if (d / "outputs" / x).is_file()]
        status = meta.get("status")
        if not present:
            gaps.append({"handoff": hid, "status": status, "expected": [f"handoffs/outputs/{x}" for x in expected],
                         "inconsistent": status == "completed"})
        completed_at = next((e["at"] for e in reversed(log) if e["state"] == "completed"), None)
        recs.append({
            "kind": "handoff", "rid": f"handoff:{hid}", "handoff_id": hid, "id_valid": bool(id_re.match(hid)),
            "contract": meta.get("contract") or "relay-kit/handoff@1", "status": status, "title": meta.get("title"),
            "from": meta.get("from"), "to": meta.get("to"), "reply_to": meta.get("reply_to") or None,
            "created": meta.get("created"), "ts": meta.get("created"), "completed_at": completed_at,
            "log": [{"at": e["at"], "state": e["state"], "actor": e["actor"], "note": e["note"]} for e in log],
            "outputs": [f"handoffs/outputs/{x}" for x in present], "text": text,
            "source_path": rd.rel(p), "source_hash": sha(raw), "lane": lane, "lanes": lanes,
            "actor": actor_str(*frm[0]) if frm else None, "actors": sorted(set(actors)),
            "evidence_class": "source-grounded",
        })
    return d, recs, gaps


def output_records(rd, handoffs):
    d = rd.root / "handoffs" / "outputs"
    recs = []
    if not d.is_dir():
        return recs
    by_id = {h["handoff_id"]: h for h in handoffs}
    by_out = {o: h for h in handoffs for o in h["outputs"]}
    for p in sorted(d.rglob("*.md")):
        if p.is_symlink():
            rd.symlinks.append(rd.rel(p))
            continue
        raw = rd.read(p, "output")
        text = raw.decode("utf-8", errors="replace")
        rel = rd.rel(p)
        h = by_out.get(rel) or by_id.get(p.name.split(".")[0])
        recs.append({
            "kind": "output", "rid": f"output:{rel}", "handoff_id": h["handoff_id"] if h else None,
            "handoff_completed_at": h["completed_at"] if h else None, "ts": None,
            "first_line": next((l for l in text.splitlines() if l.strip()), ""), "text": text,
            "source_path": rel, "source_hash": sha(raw), "lane": h["lane"] if h else None,
            "lanes": h["lanes"] if h else [], "actor": h["actor"] if h else None,
            "actors": h["actors"] if h else [], "evidence_class": "source-grounded",
        })
    return recs


def ledger_records(rd, name, kind):
    got = rd.ledger(name, kind)
    if not got:
        return None, []
    p, rows = got
    fh = rd.files[rd.rel(p)]["sha256"]
    recs, seen = [], {}
    lc = load_lifecycle()
    as_of = lc.ledger_as_of([r for _, _, r in rows]) if kind == "decision" else None   # the ledger's own latest date, never the clock
    res = lc.resolve([r for _, _, r in rows], [], as_of) if kind == "decision" else lc.resolve([], [r for _, _, r in rows])
    table = res["decisions"] if kind == "decision" else res["challenges"]
    own = {}
    for pr in res["problems"]:
        own.setdefault(pr["id"], []).append(pr["message"])
    for n, line, row in rows:
        rid = f"{kind}:{row.get('id') or 'line' + str(n)}"
        seen[rid] = seen.get(rid, 0) + 1
        if seen[rid] > 1:
            rid += f"~line{n}"
        actors = sorted({actor_str(b, s) for f in ("recorded_by", "raised_by", "proposed_by") for b, s in split_actor(row.get(f))})
        lanes = sorted({l for a in actors for l in [rd.lanes.lane(split_actor(a)[0][0])] if l})
        st = row.get("status")
        if kind == "decision":
            ev = "decision" if st in ("adopted", "rejected", "superseded") else "proposal"
        else:
            ev = "source-grounded"
        rec = {"kind": kind, "rid": rid, "ledger_id": row.get("id"), "line": n, "status": st, "fields": row,
               "ts": row.get("date"), "source_path": rd.rel(p), "source_hash": sha(line), "source_file_hash": fh,
               "lane": lanes[0] if len(lanes) == 1 else None, "lanes": lanes,
               "actor": actors[0] if len(actors) == 1 else None, "actors": actors, "evidence_class": ev}
        r_ = table.get(row.get("id")) or {}
        if kind == "decision":   # relay-kit/lifecycle@1: adopted, and no effective later line ends it
            rec["in_force"] = bool(r_.get("in_force"))
            rec["applicability"] = r_.get("applicability")
            rec["proposed_replacements"] = r_.get("proposed_replacements") or []
            rec["amended_by"] = [a["id"] for a in r_.get("amended_by") or []]
            rec["revocations_pending"] = [a["id"] for a in r_.get("revocations_pending") or []]
            rec["applicability_as_of"] = as_of
        else:
            rec["open"] = bool(r_.get("open"))
        rec["closed_by"] = [c["id"] for c in r_.get("closed_by") or []]
        rec["conflict"] = bool(r_.get("conflict"))
        if own.get(row.get("id")):
            rec["transition_problems"] = own[row.get("id")]
        if kind == "decision" and ev == "decision" and not str(row.get("basis", "")).strip():
            rec["authority_basis_missing"] = True
        recs.append(rec)
    return p, recs


# ---------------- content policy ----------------
def suppress(rec, content):
    if content == "full":
        return rec
    keep_hash = content == "metadata"

    def gone(value):
        if not keep_hash or value is None:
            return None
        v = value if isinstance(value, str) else dumps(value)
        return {"sha256": sha(v), "chars": len(v)}

    r = dict(rec)
    k = r["kind"]
    for f in ("text", "title", "first_line", "sources", "label"):
        if f in r and r[f] is not None:
            h = gone(r.pop(f))
            if h:
                r[f + "_hash"] = h
            r[f + "_suppressed"] = True
    if k == "message" and r.get("relay"):
        r["relay"] = {"marker": r["relay"]["marker"], "origin_suppressed": True}
    if k == "handoff":
        log = []
        for e in r["log"]:
            e2 = {kk: vv for kk, vv in e.items() if kk != "note"}
            h = gone(e.get("note"))
            if h:
                e2["note_hash"] = h
            e2["note_suppressed"] = True
            log.append(e2)
        r["log"] = log
    if k in ("decision", "challenge"):
        f = {}
        for kk, vv in r["fields"].items():
            if kk in LEDGER_STRUCTURAL:
                f[kk] = vv
            else:
                h = gone(vv)
                f[kk] = {"suppressed": True, **({"sha256": h["sha256"], "chars": h["chars"]} if h else {})}
        r["fields"] = f
    return r


# ---------------- manifest sanitizing ----------------
SANITIZE_POLICY = ("Links, local paths (source paths, output and capture file names, the root and destination "
                   "names, repository folders and remotes), session ids and conversation ids are replaced by "
                   "'h:' + the first 16 hex digits of their sha256, everywhere in the records and manifest, so "
                   "records still cross-reference each other but name nothing. Content hashes (source_hash, "
                   "root_hash, text hashes in metadata mode) stay: whoever holds a source can match it.")
URL_ANY = re.compile(r"https?://[^\s\"'<>)\]|`]+")


class Sanitizer:
    def __init__(self, whole, substrings, sessions):
        self.whole = {w for w in whole if w}
        self.sessions = {x for x in sessions if x}
        subs = sorted({x for x in substrings if x and len(x) >= 8}, key=lambda x: (-len(x), x))
        alt = "|".join(map(re.escape, subs)) or r"(?!)"
        self.pat = re.compile(rf"({URL_ANY.pattern})|#([\w-]+)|({alt})")
        self.counts = {"links": 0, "sessions": 0, "paths_and_ids": 0}

    @staticmethod
    def h(x):
        return "h:" + sha(x)[:16]

    def text(self, v):
        if v in self.whole or v in self.sessions:
            self.counts["sessions" if v in self.sessions else "paths_and_ids"] += 1
            return self.h(v)

        def sub(m):
            if m.group(1):
                self.counts["links"] += 1
                return self.h(m.group(1))
            if m.group(2) is not None:
                if m.group(2) in self.sessions:
                    self.counts["sessions"] += 1
                    return "#" + self.h(m.group(2))
                return m.group(0)
            self.counts["paths_and_ids"] += 1
            return self.h(m.group(3))
        return self.pat.sub(sub, v)

    def __call__(self, x):
        if isinstance(x, str):
            return self.text(x)
        if isinstance(x, list):
            return [self(v) for v in x]
        if isinstance(x, dict):
            return {self(k): self(v) for k, v in x.items()}
        return x


# ---------------- export ----------------
def export(root, out, lane=None, actors=(), since=None, until=None, kinds=None, granularity="message",
           fmt="jsonl", content="full", allow_public_dest=False, sanitize=False):
    root = pathlib.Path(root)
    out = pathlib.Path(out)
    if not root.is_dir():
        raise Refused(f"--root {root} isn't a folder")
    kinds = list(kinds or KINDS)
    bad = [k for k in kinds if k not in KINDS]
    if bad:
        raise Refused(f"--kinds: unknown {bad}; choose from {','.join(KINDS)}")
    t0, t1 = bound(since, "since"), bound(until, "until")
    destination = check_destination(out, root, allow_public_dest)
    o = out.resolve()
    if o.exists():
        if not o.is_dir():
            raise Refused(f"--out {o} exists and isn't a folder")
        foreign = sorted(x.name for x in o.iterdir() if x.name not in OUT_FILES)
        if foreign:
            raise Refused(f"--out {o} holds other files ({', '.join(foreign[:5])}); give a new or empty folder, "
                          "or one holding only a previous export")

    rd = Reader(root)
    led = load_ledger()
    rd.config()
    tpath, threads = parse_threads(rd)   # first: its lane headings declare lanes
    queried = {}
    # ledgers and handoffs first: they name the actors and sessions a capture may belong to
    dpath, decisions = ledger_records(rd, "decisions.jsonl", "decision")
    cpath, challenges = ledger_records(rd, "challenges.jsonl", "challenge")
    hdir, handoffs, missing_outputs = handoff_records(rd, led)
    outputs = output_records(rd, handoffs)
    queried["governance/decisions.jsonl"] = "read" if dpath else "absent"
    queried["governance/challenges.jsonl"] = "read" if cpath else "absent"
    queried["handoffs/"] = "read" if hdir else "absent"
    queried["handoffs/outputs/"] = "read" if (rd.root / "handoffs" / "outputs").is_dir() else "absent"
    queried["threads.md"] = "read" if tpath else "absent"

    convs, msgs = [], []
    for p in rd.walk_json():
        rel = rd.rel(p)
        if rel == "handoffs/.ledger.json":
            continue
        try:
            raw = rd.read_bytes(p)
            d = json.loads(raw)
        except (OSError, ValueError):
            rd.non_conversation_json += 1
            continue
        if not isinstance(d, dict):
            rd.non_conversation_json += 1
            continue
        if isinstance(d.get("_capture"), dict):
            rd.raw_captures.append(rel)
        schema = d.get("schema")
        legacy = schema is None and isinstance(d.get("turns"), list) and isinstance(d.get("meta"), dict)
        if schema != CONVERSATION and not legacy:
            if isinstance(schema, str) and schema.startswith("relay-kit/conversation@"):
                rd.files[rel] = {"kind": "conversation", "sha256": sha(raw), "bytes": len(raw)}
                rd.bad(p, f"unsupported conversation version {schema} (this export reads {CONVERSATION})")
            else:
                rd.non_conversation_json += 1
            continue
        rd.files[rel] = {"kind": "conversation", "sha256": sha(raw), "bytes": len(raw)}
        try:
            c, ms = conv_records(rd, p, raw, d, threads)
        except (ValueError, AttributeError, TypeError) as e:
            rd.bad(p, f"not a readable {CONVERSATION} capture: {e}")
            continue
        convs.append(c)
        msgs.extend(ms)
    queried["**/*.json (conversation@1 captures)"] = "read"

    # identical conversation keys (two captures of one conversation) get a file-hash suffix
    by_key = {}
    for c in convs:
        by_key.setdefault(c["rid"], []).append(c)
    for rid, cs in by_key.items():
        if len(cs) > 1:
            for c in cs:
                new = f"{rid}~{c['source_hash'][:12]}"
                for m in msgs:
                    if m["conversation_rid"] == c["rid"] and m["source_path"] == c["source_path"]:
                        m["conversation_rid"] = new
                        m["rid"] = f"msg:{new[5:]}:{m['mid']}"
                c["rid"] = new

    seen = {"conversations": len(convs), "messages": len(msgs), "handoffs": len(handoffs), "outputs": len(outputs),
            "decisions": len(decisions), "challenges": len(challenges)}
    pool = []
    if "conversations" in kinds:
        pool += convs + (msgs if granularity == "message" else [])
    pool += handoffs if "handoffs" in kinds else []
    pool += outputs if "outputs" in kinds else []
    pool += decisions if "decisions" in kinds else []
    pool += challenges if "challenges" in kinds else []

    excl = {"by_lane": 0, "lane_unknown": 0, "by_actor": 0, "actor_unknown": 0, "by_time": 0,
            "no_timestamp_under_time_filter": 0}

    def lane_ok(r):
        ls = [l for l in (r.get("lanes") or [r.get("lane")]) if l]
        return lane is None or any(rd.lanes.matches(lane, l) for l in ls)

    def actor_ok(r):
        return not actors or actor_matches(actors, r.get("actors") or [])

    def in_time(r):
        """True or False, or None when the record has no timestamp to place it by."""
        if t0 is None and t1 is None:
            return True
        if r["kind"] == "conversation":
            a, b = span(r.get("first_message_at")), span(r.get("last_message_at"))
            sp = (a[0], b[1]) if a and b else (a or b)
        else:
            sp = span(r.get("ts"))
        if not sp:
            return None
        return (t0 is None or sp[1] >= t0) and (t1 is None or sp[0] <= t1)

    def keep(r):
        if not lane_ok(r):
            excl["lane_unknown" if not (r.get("lanes") or r.get("lane")) else "by_lane"] += 1
            return False
        if not actor_ok(r):
            excl["actor_unknown" if not r.get("actors") else "by_actor"] += 1
            return False
        t = in_time(r)
        if not t:
            excl["no_timestamp_under_time_filter" if t is None else "by_time"] += 1
            return False
        return True

    kept = [r for r in pool if r["kind"] != "conversation" and keep(r)]
    if "conversations" in kinds:
        if granularity == "message":   # a conversation row goes with the messages that passed
            live = {m["conversation_rid"] for m in kept if m["kind"] == "message"}
            kept += [c for c in convs if c["rid"] in live]
        else:
            kept += [c for c in convs if keep(c)]
    scoped_handoffs = {h["rid"] for h in handoffs if lane_ok(h) and actor_ok(h) and in_time(h) is not False}

    for r in kept:
        r["ts_missing"] = not r.get("ts")
        r.setdefault("ts", None)
    kept.sort(key=lambda r: (KIND_ORDER[r["kind"]], r.get("conversation_rid") or r["rid"], r.get("seq") or 0,
                             r.get("line") or 0, r["rid"]))
    rids = [r["rid"] for r in kept]
    assert len(rids) == len(set(rids)), "duplicate record ids"
    final = [suppress(r, content) for r in kept]

    # coverage gaps, in the scope of the query
    in_scope_lane = lambda l: lane is None or rd.lanes.matches(lane, l)
    in_scope_actor = lambda a: not actors or actor_matches(actors, [a])
    captured_ids = {c["conversation_id"] for c in convs if c["conversation_id"]}
    conv_urls = []
    for c in convs:
        f = rd.root / c["source_path"]
        try:
            conv_urls.append(json.loads(rd.read_bytes(f)).get("meta", {}).get("source_url") or "")
        except (OSError, ValueError, AttributeError):
            pass
    actor_sessions = {s for b, i in rd.referenced.items() for s in i["sessions"]
                      if actor_matches(actors, [actor_str(b, s)])} if actors else set()
    threads_gap = [dict(t) for cid, t in sorted(threads.items())
                   if cid not in captured_ids and not any(cid in url_segments(u) for u in conv_urls)
                   and (lane is None or (t["lane"] and in_scope_lane(t["lane"])))
                   and (not actors or cid in actor_sessions)]
    captured_actor_sessions = {}
    for c in convs:
        for b, s in split_actor(c["actor"]):
            captured_actor_sessions.setdefault(b, set()).add(s)
    actors_gap = []
    for base, info in sorted(rd.referenced.items()):
        if not in_scope_lane(rd.lanes.lane(base)) or not (not actors or actor_matches(actors, [base] + [actor_str(base, s) for s in info["sessions"]])):
            continue
        have = captured_actor_sessions.get(base, set())
        missing = sorted(s for s in info["sessions"] if s not in have and not any(s in url_segments(u) for u in conv_urls))
        if base not in captured_actor_sessions or missing:
            actors_gap.append({"actor": base, "lane": rd.lanes.lane(base), "uncaptured_sessions": missing,
                               "no_capture_at_all": base not in captured_actor_sessions and not any(
                                   s in url_segments(u) for s in info["sessions"] for u in conv_urls),
                               "referenced_in": sorted(info["in"])[:20], "referenced_in_count": len(info["in"])})
    all_lanes = set(rd.lanes.codes) | {t["lane"] for t in threads.values() if t["lane"]} | \
        {l for b in rd.referenced for l in [rd.lanes.lane(b)] if l}
    captured_lanes = {c["lane"] for c in convs if c["lane"]}
    lanes_gap = sorted(l for l in all_lanes if l and in_scope_lane(l) and l not in captured_lanes)
    kept_ids = {r["rid"] for r in kept}
    ts_missing = {}
    for r in kept:
        if r["ts_missing"]:
            ts_missing.setdefault(r["kind"], []).append(r["rid"])
    gaps = {
        "threads_without_capture": threads_gap,
        "actors_without_capture": actors_gap,
        "lanes_without_capture": lanes_gap,
        "handoffs_missing_output": [g for g in missing_outputs if f"handoff:{g['handoff']}" in scoped_handoffs],
        "conversations_with_hidden_steps": [{"rid": c["rid"], "hidden_counts": c["hidden_counts"]}
                                            for c in convs if c["hidden_total"] and c["rid"] in kept_ids],
        "incomplete_captures": [{"rid": c["rid"], "checks": c["checks"]} for c in convs
                                if c["rid"] in kept_ids and c["checks"] not in (["ok"],)],
        "raw_captures_not_rendered": sorted(rd.raw_captures),
        "messages_with_derived_ids": sum(1 for r in kept if r["kind"] == "message" and r["mid_derived"]),
        "records_missing_timestamps": {k: {"count": len(v), "rids": v[:50]} for k, v in sorted(ts_missing.items())},
        "decisions_without_basis": [r["rid"] for r in kept if r.get("authority_basis_missing")],
        "unreadable": sorted(rd.unreadable, key=lambda x: x["path"]),
        "unknown_fields": {k: sorted(v) for k, v in sorted(rd.unknown_fields.items())},
    }
    gaps["summary"] = {k: (len(v) if isinstance(v, list) else (sum(x["count"] for x in v.values()) if k == "records_missing_timestamps"
                                                              else (len(v) if isinstance(v, dict) else v)))
                       for k, v in gaps.items()}

    sanitize = bool(sanitize or content == "none")
    san = None
    if sanitize:
        paths = set(rd.files) | set(rd.symlinks) | set(rd.raw_captures) | {r["source_path"] for r in kept} | \
            {x for g in missing_outputs for x in g["expected"]} | {u["path"] for u in rd.unreadable}
        sessions = {x for i in rd.referenced.values() for x in i["sessions"]} | \
            {s for r in kept for a in r.get("actors") or [] for _, s in split_actor(a) if s}
        ids = {c["conversation_id"] for c in convs if c["conversation_id"]} | set(threads)
        whole = paths | ids | {rd.root.name, destination["name"]} | \
            {f[k] for f in destination["inside_git"] for k in ("repo_folder", "remote") if f.get(k)}
        san = Sanitizer(whole, paths | ids, sessions)
        final = san(final)

    # write
    o.mkdir(parents=True, exist_ok=True)
    name = "records.jsonl" if fmt == "jsonl" else "records.json"
    body = "".join(dumps(r) + "\n" for r in final) if fmt == "jsonl" else \
        json.dumps(final, ensure_ascii=False, sort_keys=True, indent=1) + "\n"
    stale = o / ("records.json" if fmt == "jsonl" else "records.jsonl")
    counts = {}
    for r in final:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1
    tsl = sorted(s for r in kept for s in [span(r.get("ts"))] if s)
    manifest = {
        "schema": SCHEMA,
        "generator": {"tool": "relay-kit export (tools/export.py)", "kit_version": __version__},
        "contracts_read": {"conversation": CONVERSATION, "handoff": HANDOFF_CONTRACTS, "paste_markers": PASTE_MARKERS},
        "root": {"name": rd.root.name,
                 "root_hash": sha("".join(f"{k}\t{v['sha256']}\n" for k, v in sorted(rd.files.items())))},
        "query": {"lane": lane, "actors": sorted(actors), "since": since, "until": until, "kinds": sorted(kinds),
                  "granularity": granularity, "format": fmt, "content": content, "sanitize_manifest": sanitize},
        "redaction": {"content": content, "policy": CONTENT_POLICY[content],
                      "suppressed_fields": {} if content == "full" else TEXT_FIELDS},
        "destination": destination,
        "sources": {"queried": dict(sorted(queried.items())),
                    "files": {k: v for k, v in sorted(rd.files.items())}},
        "seen": seen,
        "counts": dict(sorted(counts.items())),
        "time_bounds": {"earliest": tsl[0][0].strftime("%Y-%m-%dT%H:%M:%SZ") if tsl else None,
                        "latest": tsl[-1][1].strftime("%Y-%m-%dT%H:%M:%SZ") if tsl else None},
        "outputs": {name: {"sha256": sha(body), "records": len(final)}},
        "exclusions": {**excl, "kinds_not_requested": sorted(set(KINDS) - set(kinds)),
                       "messages_not_expanded": granularity != "message",
                       "non_conversation_json_files": rd.non_conversation_json,
                       "symlinks_not_followed": sorted(set(rd.symlinks)), "folders_skipped": [".git"]},
        "evidence_classes": EVIDENCE,
        "coverage_gaps": gaps,
        "limits": LIMITS,
        "nondeterministic": {"generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
    }
    if san:
        manifest = san(manifest)
    manifest["sanitize"] = {"enabled": sanitize, "implied_by_content_none": content == "none",
                            "policy": SANITIZE_POLICY if sanitize else
                            "Off: links, paths, session and conversation ids appear as recorded. --content "
                            "metadata/none redact content; they don't anonymise. Use --sanitize-manifest.",
                            "replaced": dict(san.counts) if san else {}}
    for fname, text in ((name, body), ("manifest.json", json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=1) + "\n")):
        fd, tmp = tempfile.mkstemp(dir=o, prefix=".export-")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, o / fname)
    if stale.exists():
        stale.unlink()
    return manifest


def main(argv=None):
    ap = argparse.ArgumentParser(prog="relay-kit export", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True, type=pathlib.Path, help="the private records folder to read")
    ap.add_argument("--out", required=True, type=pathlib.Path, help="destination folder (new, or holding a previous export)")
    ap.add_argument("--lane", help="lane name or code (AEC matches aec and aec-release)")
    ap.add_argument("--actor", action="append", default=[], help="assistant:lane[#session]; repeat or comma-separate")
    ap.add_argument("--since", help="ISO date or time (inclusive)")
    ap.add_argument("--until", help="ISO date or time (inclusive; a date covers its whole day)")
    ap.add_argument("--kinds", default=",".join(KINDS), help=f"comma-separated subset of {','.join(KINDS)}")
    ap.add_argument("--granularity", choices=["inventory", "message"], default="message")
    ap.add_argument("--format", choices=["jsonl", "json"], default="jsonl")
    ap.add_argument("--content", choices=["full", "metadata", "none"], default="full")
    ap.add_argument("--sanitize-manifest", action="store_true",
                    help="replace links, paths, session ids and conversation ids with stable hashes (implied by --content none)")
    ap.add_argument("--allow-public-dest", action="store_true",
                    help="write even inside a git repository that is public or whose visibility can't be confirmed")
    a = ap.parse_args(argv)
    actors = sorted({x.strip() for v in a.actor for x in v.split(",") if x.strip()})
    for x in actors:
        if not split_actor(x):
            sys.exit(f"refused: --actor {x!r} isn't assistant:lane or assistant:lane#session")
    try:
        m = export(a.root, a.out, lane=a.lane, actors=actors, since=a.since, until=a.until,
                   kinds=[k.strip() for k in a.kinds.split(",") if k.strip()], granularity=a.granularity,
                   fmt=a.format, content=a.content, allow_public_dest=a.allow_public_dest,
                   sanitize=a.sanitize_manifest)
    except Refused as e:
        sys.exit(f"refused: {e}")
    s = m["coverage_gaps"]["summary"]
    print(f"exported {sum(m['counts'].values())} records to {pathlib.Path(a.out).resolve()} ({SCHEMA}): "
          + ", ".join(f"{k} {v}" for k, v in m["counts"].items()))
    print("coverage gaps: " + ", ".join(f"{k} {v}" for k, v in s.items() if v))


if __name__ == "__main__":
    main()
