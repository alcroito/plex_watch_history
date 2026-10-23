"""Simkl API: device login, token refresh, library and history."""

import json
import os
import re
import time
import urllib.parse

from plexhistory import common
from plexhistory.common import (APP_NAME, APP_VERSION, HttpError, log, update_env_file,
                                write_file_atomic)
from plexhistory.plexdb import NUMBERING_ANIDB

SIMKL_API = "https://api.simkl.com"
SIMKL_POST_INTERVAL = 1.1


class Simkl:
    def __init__(self, client_id, token, refresh_token, env_path):
        self.client_id = client_id
        self.token = token
        self.refresh_token = refresh_token
        self.env_path = env_path

    def _save_tokens(self, data):
        if "media:write" not in (data.get("scope") or "").split():
            raise SystemExit(f"Simkl token lacks the media:write scope (got {data.get('scope')!r})")
        self.token = data["access_token"]
        self.refresh_token = data.get("refresh_token") or self.refresh_token
        update_env_file(self.env_path, {"SIMKL_ACCESS_TOKEN": self.token,
                                        "SIMKL_REFRESH_TOKEN": self.refresh_token})
        log(f"Saved Simkl tokens to {self.env_path}")

    def login(self):
        """Run the OAuth device flow; the user approves the app in a browser."""
        status, data = common.post_form(f"{SIMKL_API}/oauth2/device",
                                 {"client_id": self.client_id, "scope": "media:read media:write"})
        if status != 200:
            raise SystemExit(f"Simkl device login failed: HTTP {status} {data}")
        log("Open this to connect your Simkl account:", 0)
        log(f"  {data['verification_uri_complete']}", 0)
        log(f"Or go to {data['verification_uri']} and enter: {data['user_code']}", 0)
        interval = data.get("interval", 5)
        # Simkl never reports a denial, so the code's expiry is the only way out.
        deadline = time.monotonic() + data.get("expires_in", 900)
        while time.monotonic() < deadline:
            time.sleep(interval)
            status, token = common.post_form(f"{SIMKL_API}/oauth2/token", {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": self.client_id, "device_code": data["device_code"]})
            if status == 200:
                self._save_tokens(token)
                return
            error = token.get("error")
            if error == "slow_down":
                interval += 5
            elif error != "authorization_pending":
                raise SystemExit(f"Simkl device login failed: HTTP {status} {token}")
        raise SystemExit("Simkl device login timed out")

    def refresh(self):
        """Get a new access token with the refresh token; return whether it worked."""
        if not self.refresh_token:
            return False
        status, data = common.post_form(f"{SIMKL_API}/oauth2/token", {
            "grant_type": "refresh_token", "client_id": self.client_id,
            "refresh_token": self.refresh_token})
        if status != 200:
            log(f"Simkl token refresh failed: HTTP {status} {data}", 0)
            return False
        self._save_tokens(data)
        return True

    def _request(self, url, data=None):
        try:
            return common.http_json(url, self._headers(), data)
        except HttpError as e:
            if e.code != 401:
                raise
        log("Simkl access token rejected, renewing it")
        if not self.refresh():
            self.login()
        return common.http_json(url, self._headers(), data)

    def _url(self, path, **params):
        params.update({"client_id": self.client_id, "app-name": APP_NAME,
                       "app-version": APP_VERSION})
        return f"{SIMKL_API}{path}?{urllib.parse.urlencode(params)}"

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}",
                "User-Agent": f"{APP_NAME}/{APP_VERSION}",
                "Content-Type": "application/json"}

    def get(self, path, **params):
        return self._request(self._url(path, **params))

    def post(self, path, data):
        return self._request(self._url(path), data)

    def account_id(self):
        return str(((self.get("/users/settings") or {}).get("account") or {}).get("id") or "")


class SimklCatalog:
    """Read-only lookups in Simkl's catalog, cached in a file; entries expire after
    max_age_days (0 keeps them for the run only)."""

    def __init__(self, simkl, cache_path=None, max_age_days=0):
        self.simkl = simkl
        self.cache_path = cache_path
        self.max_age = max_age_days * 86400
        self.cache = {}
        if cache_path and self.max_age and os.path.exists(cache_path):
            with open(cache_path) as f:
                now = time.time()
                self.cache = {k: v for k, v in json.load(f).items()
                              if now - v["fetched_at"] < self.max_age}

    def _cached(self, key, fetch):
        if key not in self.cache:
            self.cache[key] = {"fetched_at": time.time(), "data": fetch()}
            if self.cache_path and self.max_age:
                write_file_atomic(self.cache_path, json.dumps(self.cache))
        return self.cache[key]["data"]

    def _get(self, path, public=False, **params):
        # Catalog endpoints are Cloudflare-cached; Simkl asks to leave the token off them.
        fetch = (lambda: common.http_json(self.simkl._url(path, **params),
                                          {"User-Agent": f"{APP_NAME}/{APP_VERSION}"})) \
            if public else (lambda: self.simkl.get(path, **params))
        return self._cached(f"{path}?{urllib.parse.urlencode(sorted(params.items()))}", fetch)

    def entry(self, **ids):
        """Return ("anime" | "tv" | "movies", simkl id) of the entry with these external ids,
        or None. Uses /redirect, which unlike /search/id does not count against the daily
        quota."""
        def fetch():
            location = common.http_location(
                self.simkl._url("/redirect", to="simkl", **ids),
                {"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
            m = re.search(r"simkl\.com/(anime|tv|movies)/(\d+)", location or "")
            return [m[1], int(m[2])] if m else None
        found = self._cached(f"/redirect?{urllib.parse.urlencode(sorted(ids.items()))}", fetch)
        return tuple(found) if found else None

    def episodes(self, kind, simkl_id, specials=False):
        """Episodes of a Simkl entry (kind "anime" or "tv") with titles, air dates and, for
        anime, TVDB numbers; regular ones only unless specials is set."""
        return [e for e in self._get(f"/{kind}/episodes/{simkl_id}", public=True,
                                     extended="full") or []
                if specials or e.get("type") != "special"]

    def tvdb_season_sizes(self, tvdb_id):
        """Return {TVDB season: regular episodes} of the Simkl entry with this TVDB show id."""
        sizes = {}
        found = self.entry(tvdb=tvdb_id)
        if found and found[0] in ("anime", "tv"):
            for e in self.episodes(*found):
                tvdb = e.get("tvdb") or {}
                if tvdb.get("season") is not None:
                    sizes[tvdb["season"]] = sizes.get(tvdb["season"], 0) + 1
        return sizes

    def anidb_episodes(self, anidb_id):
        """Return the regular episodes (number, title, air date, TVDB numbers) of the Simkl
        anime entry with this AniDB id."""
        found = self.entry(anidb=anidb_id)
        return self.episodes("anime", found[1]) if found and found[0] == "anime" else []

    def multipart_entry(self, title, part, subtitle=None):
        """Find an anime entry that holds a film series as episodes, e.g. Lancer Saga: Ren the
        Exiled with part 4 as episode 4. Return (simkl id, title) or None.

        With a subtitle the episode title must match it; without one the entry must be a
        movie or OVA.
        """
        for found in self._get("/search/anime", q=title, extended="full") or []:
            simkl_id = (found.get("ids") or {}).get("simkl_id")
            if not simkl_id or (found.get("ep_count") or 0) < part:
                continue
            if subtitle:
                if not any(e.get("episode") == part
                           and (e.get("title") or "").casefold() == subtitle.casefold()
                           for e in self.episodes("anime", simkl_id)):
                    continue
            elif found.get("type") not in ("movie", "ova"):
                continue
            return simkl_id, found.get("title")
        return None


class SimklLibrary:
    """Index of what the Simkl user already watched, keyed by external ids."""

    def __init__(self):
        self.movies = set()
        self.episodes = {}  # "tvdb:123" -> list of (simkl_type, status, {(season, ep)})

    def load(self, simkl):
        for media_type, statuses in (("shows", ("watching", "hold", "dropped", "completed")),
                                     ("anime", ("watching", "hold", "dropped", "completed")),
                                     ("movies", ("completed",))):
            for status in statuses:
                data = self._fetch(simkl, media_type, status)
                entries = (data or {}).get(media_type, [])
                for entry in entries:
                    self._add(media_type, status, entry)
                log(f"  {media_type}/{status}: {len(entries)} items", 2)

    @staticmethod
    def _fetch(simkl, media_type, status):
        path = f"/sync/all-items/{media_type}/{status}"
        if media_type == "movies":
            return simkl.get(path, extended="ids_only")
        try:
            return simkl.get(path, extended="full", include_all_episodes="yes")
        except HttpError as e:
            if "max_items" not in e.body:
                raise
            return simkl.get(path, extended="full", include_all_episodes="original")

    def _add(self, media_type, status, entry):
        item = entry.get("movie") or entry.get("show") or {}
        keys = [f"{k}:{v}" for k, v in (item.get("ids") or {}).items()
                if k in ("imdb", "tmdb", "tvdb", "anidb") and v]
        if media_type == "movies":
            self.movies.update(keys)
            return
        eps = {(s["number"], e["number"]) for s in entry.get("seasons") or []
               for e in s.get("episodes") or []}
        # Simkl files anime films as anime entries of type movie, not in the movies list. A
        # film series' entry carries one film's ids, so only its first part counts.
        if media_type == "anime" and entry.get("anime_type") == "movie" and (1, 1) in eps:
            self.movies.update(keys)
        record = (media_type, status, eps)
        for key in keys:
            self.episodes.setdefault(key, []).append(record)

    def has_movie(self, ids):
        return any(f"{k}:{v}" in self.movies for k, v in ids.items())

    def has_episode(self, ids, season, episode, numbering):
        for k, v in ids.items():
            for media_type, _, eps in self.episodes.get(f"{k}:{v}", []):
                # Simkl lists anime episodes with AniDB numbering, so TVDB-numbered
                # sources can't be compared; Simkl itself ignores duplicate posts.
                if media_type == "anime" and numbering != NUMBERING_ANIDB:
                    continue
                if (season, episode) in eps:
                    return True
        return False


def run_login(args, env, error):
    """The simkl-login command: get new Simkl tokens and the account id into the env file."""
    if not args.simkl_client_id:
        error("SIMKL_CLIENT_ID is not set (--simkl-client-id, environment or env file)")
    refresh_token = os.environ.get("SIMKL_REFRESH_TOKEN") or env.get("SIMKL_REFRESH_TOKEN")
    simkl = Simkl(args.simkl_client_id, None, refresh_token, args.env_file)
    if args.refresh:
        if not refresh_token:
            error("no SIMKL_REFRESH_TOKEN to refresh with; run simkl-login without --refresh")
        if not simkl.refresh():
            raise SystemExit("Refreshing the Simkl token failed; run simkl-login without "
                             "--refresh to log in again")
    else:
        simkl.login()
    account = simkl.account_id()
    if account:
        update_env_file(args.env_file, {"SIMKL_ACCOUNT_ID": account})
    log(f"Simkl access and refresh tokens{' and account id ' + account if account else ''} "
        f"saved to {args.env_file}", 0)
