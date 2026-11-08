"""The kit's GitHub client for pull-request reads (relay-kit/prs@4, before it relay-kit/prs@3, reads through it; export's visibility
check, status's CI read and the cockpit's opt-in --pr-state lookup predate it and still call GitHub
their own way): read-only (GET only), shared by every reader that needs GitHub
(relay-kit prs today; anything that needs pull-request data reads governance/pr-state.jsonl instead
of calling this again). Standard library only.

    c = Client(gh_transport())            # or token_transport(os.environ["GITHUB_TOKEN"])
    items, more = c.pages("repos/o/r/pulls?state=all&sort=updated&direction=desc", limit=30)
    obj = c.get("repos/o/r/pulls/7/reviews")
    c.stats   # requests, retries, waits, cache hits, rate limit left: what the run cost and saw

- **Pagination:** `pages()` walks `page=1,2,...` (100 per page) until `limit` items, a short page, or
  `max_pages`; it says whether more were available, so a caller can report a partial read.
- **Retries:** network errors and 500/502/503/504 are retried with backoff (1, 2, 4 s by default).
- **Rate limits:** a 429, or a 403 with `x-ratelimit-remaining: 0`, waits for `retry-after` or
  `x-ratelimit-reset` when that is at most `max_wait` seconds, else refuses, saying until when.
- **Caching:** each path is fetched once per run. With `cache_dir`, responses are kept with their
  ETag and sent back as If-None-Match, so an unchanged resource costs a 304; the cache holds
  response bodies (private repositories' text included), so it is off unless asked for.
- **Timestamps:** `stats["first_at"]` / `["last_at"]` bracket the requests (UTC).
Errors are raised as `Refused` with the path and status; nothing is written anywhere but `cache_dir`.
"""
import datetime as dt
import email.utils
import hashlib
import json
import os
import pathlib
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

RETRY_STATUS = {500, 502, 503, 504}


class Refused(Exception):
    pass


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _split_response(raw):
    """gh api -i output: 'HTTP/2.0 200 OK' + headers + blank line + body."""
    head, _, body = raw.replace("\r\n", "\n").partition("\n\n")
    lines = head.split("\n")
    try:
        status = int(lines[0].split()[1])
    except (IndexError, ValueError):
        return None, {}, raw
    headers = {}
    for ln in lines[1:]:
        k, _, v = ln.partition(":")
        if k:
            headers[k.strip().lower()] = v.strip()
    return status, headers, body


def gh_transport():
    """GET through the gh CLI (its own sign-in). Returns (status, headers, text)."""
    def t(path, headers):
        cmd = ["gh", "api", "-i", "--method", "GET"]
        for k, v in headers.items():
            cmd += ["-H", f"{k}: {v}"]
        try:
            p = subprocess.run(cmd + ["--", path], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
        except subprocess.TimeoutExpired as x:
            raise OSError(f"gh api timed out: {x}")
        status, h, body = _split_response(p.stdout or "")
        if status is None:   # no HTTP response at all (not signed in, bad flag): retrying won't help
            raise Refused(f"gh api {path}: " + ((p.stderr or p.stdout).strip()[:200] or f"exited {p.returncode}"))
        return status, h, body
    return t


class _NoAuthElsewhere(urllib.request.HTTPRedirectHandler):
    """Follow redirects (a renamed repository answers 301), but never carry the token off api.github.com."""
    def redirect_request(self, req, fp, code, msg, hdrs, newurl):
        new = super().redirect_request(req, fp, code, msg, hdrs, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).hostname != "api.github.com":
            new.headers.pop("Authorization", None)
            new.unredirected_hdrs.pop("Authorization", None)
        return new


_OPENER = urllib.request.build_opener(_NoAuthElsewhere)


def token_transport(token):
    """GET against api.github.com with a token. The token is sent only to api.github.com, redirects included."""
    def t(path, headers):
        req = urllib.request.Request("https://api.github.com/" + path, method="GET",
                                     headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                              "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "relay-kit", **headers})
        try:
            with _OPENER.open(req, timeout=60) as r:
                return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read().decode("utf-8")
        except urllib.error.HTTPError as x:
            return x.code, {k.lower(): v for k, v in (x.headers or {}).items()}, x.read().decode("utf-8", "replace")
        except urllib.error.URLError as x:
            raise OSError(str(x))
    return t


class Client:
    def __init__(self, transport, retries=3, max_wait=60, sleep=time.sleep, clock=time.time, cache_dir=None, cache_scope=""):
        """cache_scope: who is asking (e.g. 'gh' or 'token'), so one account's cached answers aren't served to another."""
        self.transport, self.retries, self.max_wait, self.sleep, self.clock = transport, retries, max_wait, sleep, clock
        self.cache_dir = pathlib.Path(cache_dir) if cache_dir else None
        self.cache_scope = cache_scope
        self.memo = {}
        self.stats = {"requests": 0, "retries": 0, "rate_limit_waits": 0, "memo_hits": 0, "not_modified": 0,
                      "rate_limit_remaining": None, "first_at": None, "last_at": None}

    def _disk(self, path):
        return self.cache_dir / (hashlib.sha256(f"{self.cache_scope}\n{path}".encode()).hexdigest() + ".json") if self.cache_dir else None

    def get(self, path):
        """Parsed JSON for a GitHub REST path (no leading slash), or Refused."""
        if path in self.memo:
            self.stats["memo_hits"] += 1
            return self.memo[path]
        disk, cached = self._disk(path), None
        if disk and disk.is_file():
            try:
                cached = json.loads(disk.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cached = None
        headers = {"If-None-Match": cached["etag"]} if cached and cached.get("etag") else {}
        attempt = 0
        while True:
            self.stats["requests"] += 1
            self.stats["first_at"] = self.stats["first_at"] or now_iso()
            try:
                status, h, body = self.transport(path, headers)
            except OSError as x:
                status, h, body = None, {}, str(x)
            self.stats["last_at"] = now_iso()
            if h.get("x-ratelimit-remaining", "").isdigit():
                self.stats["rate_limit_remaining"] = int(h["x-ratelimit-remaining"])
            if status == 304 and cached:
                self.stats["not_modified"] += 1
                self.memo[path] = cached["body"]
                return cached["body"]
            if status is not None and 200 <= status < 300:
                try:
                    obj = json.loads(body or "null")
                except ValueError:
                    raise Refused(f"GET {path}: response isn't JSON")
                if disk and h.get("etag"):     # private repositories' text: readable by this user only
                    disk.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    fd = os.open(disk, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(json.dumps({"etag": h["etag"], "body": obj}))
                self.memo[path] = obj
                return obj
            # primary limit (403 with none left) and secondary limit (403/429 with retry-after)
            limited = status == 429 or (status == 403 and (h.get("x-ratelimit-remaining") == "0" or "retry-after" in h))
            if limited:
                wait = None
                ra = h.get("retry-after", "")
                if ra.isdigit():
                    wait = int(ra)
                elif ra:
                    try:
                        wait = max(0, int(email.utils.parsedate_to_datetime(ra).timestamp() - self.clock()))
                    except (TypeError, ValueError):
                        wait = None
                elif h.get("x-ratelimit-reset", "").isdigit():
                    wait = max(1, int(h["x-ratelimit-reset"]) - int(self.clock()))
                if wait is None or wait > self.max_wait or attempt >= self.retries:
                    until = (dt.datetime.fromtimestamp(int(h["x-ratelimit-reset"]), dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                             if h.get("x-ratelimit-reset", "").isdigit() else "unknown")
                    raise Refused(f"GET {path}: rate limited (until {until})")
                self.stats["rate_limit_waits"] += 1
                self.sleep(wait)
            elif (status is None or status in RETRY_STATUS) and attempt < self.retries:
                self.stats["retries"] += 1
                self.sleep(2 ** attempt)
            else:
                msg = body.strip()[:160] if isinstance(body, str) else ""
                try:
                    msg = json.loads(body).get("message", msg)
                except (ValueError, AttributeError, TypeError):
                    pass
                raise Refused(f"GET {path}: {('HTTP ' + str(status)) if status else 'network error'}: {msg}")
            attempt += 1

    def pages(self, path, limit=100, per_page=100, max_pages=10):
        """(items, more_available): a list endpoint walked page by page for `limit` items. One item past the
        limit is asked for, so more_available is exact: True or False, or None when max_pages ran out first."""
        sep = "&" if "?" in path else "?"
        per = max(1, min(per_page, limit + 1, 100))
        items, page = [], 1
        while len(items) <= limit and page <= max_pages:
            got = self.get(f"{path}{sep}per_page={per}&page={page}")
            if not isinstance(got, list):
                raise Refused(f"GET {path}: expected a list")
            items += got
            if len(got) < per:
                return items[:limit], len(items) > limit
            page += 1
        return items[:limit], True if len(items) > limit else None
