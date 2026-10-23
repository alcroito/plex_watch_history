"""Shared helpers: logging, settings files and HTTP."""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

APP_NAME = "plex-history-to-simkl"
APP_VERSION = "1.0"

# 0: warnings, login prompts and the final result; 1: steps, totals and progress;
# 2: per-table, per-section and per-batch detail; 3: every skipped item and HTTP request.
VERBOSITY = 1

# (remaining, limit) of the daily request quota, from the last response that reported it
# (Simkl's X-RateLimit-* headers); None until then.
RATE_LIMIT = None


# Prefix log lines with the local time (--timestamps).
TIMESTAMPS = False


def log(msg, level=1):
    if level <= VERBOSITY:
        print(f"{time.strftime('%H:%M:%S')} {msg}" if TIMESTAMPS else msg, file=sys.stderr)


class Progress:
    """Logs "label done/total" about every 10% (every 50 items at -v)."""

    def __init__(self, total, label, details=None):
        self.total, self.label, self.details = total, label, details
        self.done = 0
        self.every = 50 if VERBOSITY >= 2 else max(50, total // 10)

    def step(self):
        self.done += 1
        if self.done % self.every == 0 or self.done == self.total:
            extra = f", {self.details()}" if self.details else ""
            log(f"  {self.label} {self.done}/{self.total} "
                f"({100 * self.done // self.total}%{extra})")


def read_env_file(path):
    values = {}
    if not os.path.exists(path):
        return values
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.removeprefix("export ").partition("=")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            values[key.strip()] = value
    return values


def write_file_atomic(path, text, mode=None):
    """Write via a temp file and rename, so an interrupted write never truncates path."""
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode or 0o666)
    if mode:
        os.fchmod(fd, mode)
    with os.fdopen(fd, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def update_env_file(path, values):
    """Set keys in a .env file, keeping all other lines."""
    lines = []
    if os.path.exists(path):
        with open(path) as f:
            lines = f.read().splitlines()
    remaining = dict(values)
    for i, line in enumerate(lines):
        key = line.strip().removeprefix("export ").partition("=")[0].strip()
        if key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"
    lines += [f"{k}={v}" for k, v in remaining.items()]
    write_file_atomic(path, "\n".join(lines) + "\n", 0o600)


class HttpError(Exception):
    def __init__(self, code, url, body):
        super().__init__(f"HTTP {code} for {url.split('?')[0]}: {body[:500]!r}")
        self.code = code
        self.body = body


# Seconds to wait before retrying a server error or dropped connection, per attempt.
SERVER_ERROR_WAITS = (5, 15, 45)


def http_json(url, headers, data=None, retries=3):
    body = json.dumps(data).encode() if data is not None else None
    log(f"    {'POST' if body is not None else 'GET'} {url.split('?')[0]}", 3)
    for attempt in range(retries + 1):
        # Without one, urllib sends a generic Python-urllib agent.
        headers = {"User-Agent": f"{APP_NAME}/{APP_VERSION}", **headers}
        req = urllib.request.Request(url, data=body, headers=headers,
                                     method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                if resp.headers.get("X-RateLimit-Remaining") is not None:
                    global RATE_LIMIT
                    RATE_LIMIT = (resp.headers["X-RateLimit-Remaining"],
                                  resp.headers.get("X-RateLimit-Limit"))
                raw = resp.read()
                return json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                retry_after = int(e.headers.get("Retry-After") or 60)
                # A long Retry-After is Simkl's daily quota; waiting it out is not useful.
                if retry_after > 600:
                    raise SystemExit(f"Rate limit quota exhausted for {url.split('?')[0]}, "
                                     f"retry in {retry_after}s. Rerunning later is safe.")
                log(f"429 received, waiting {retry_after}s", 0)
                time.sleep(retry_after)
                continue
            if e.code >= 500 and attempt < retries:
                # Plex's metadata service answers 503 when it is overloaded.
                wait = SERVER_ERROR_WAITS[min(attempt, len(SERVER_ERROR_WAITS) - 1)]
                log(f"HTTP {e.code} for {url.split('?')[0]}, retrying in {wait}s", 0)
                time.sleep(wait)
                continue
            if e.code == 404:
                return None
            raise HttpError(e.code, url, e.read().decode(errors="replace"))
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            if attempt >= retries:
                raise
            wait = SERVER_ERROR_WAITS[min(attempt, len(SERVER_ERROR_WAITS) - 1)]
            log(f"{getattr(e, 'reason', e)} for {url.split('?')[0]}, retrying in {wait}s", 0)
            time.sleep(wait)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def http_location(url, headers):
    """GET url without following a redirect; return its Location header, or None."""
    log(f"    GET {url.split('?')[0]}", 3)
    try:
        with urllib.request.build_opener(_NoRedirect).open(
                urllib.request.Request(url, headers=headers), timeout=60) as resp:
            return resp.headers.get("Location")
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            return e.headers.get("Location")
        if e.code == 404:
            return None
        raise HttpError(e.code, url, e.read().decode(errors="replace"))


def post_form(url, fields):
    """POST an OAuth form request, returning (status, json body) also for error statuses."""
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": f"{APP_NAME}/{APP_VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except ValueError:
            return e.code, {}


def rate_limit_text():
    """ "135 of 500 Simkl requests left today ..." or None if no response reported it."""
    if not RATE_LIMIT:
        return None
    return (f"{RATE_LIMIT[0]} of {RATE_LIMIT[1]} Simkl requests left today (shared by every app "
            f"of the account; resets at midnight US Eastern)")


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
