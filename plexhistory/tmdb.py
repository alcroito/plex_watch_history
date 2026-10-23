"""TMDB lookups and Kometa's AniDB id mapping."""

import json
import os
import time
import urllib.parse

from plexhistory import common
from plexhistory.common import log, write_file_atomic

TMDB_API = "https://api.themoviedb.org/3"
ANIME_IDS_URL = "https://raw.githubusercontent.com/Kometa-Team/Anime-IDs/master/anime_ids.json"


class Tmdb:
    """TMDB client with an on-disk response cache; entries expire after max_age_days."""

    def __init__(self, key, cache_path, max_age_days):
        self.key = key
        self.cache_path = cache_path
        self.max_age = max_age_days * 86400
        self.cache = {}
        if self.max_age and os.path.exists(cache_path):
            with open(cache_path) as f:
                now = time.time()
                self.cache = {k: v for k, v in json.load(f).items()
                              if now - v["fetched_at"] < self.max_age}
        self.fetched = 0
        self.cache_hits = 0

    def stats(self):
        return f"{self.fetched} fetched, {self.cache_hits} from cache"

    def _get(self, path, **params):
        cache_key = f"{path}?{urllib.parse.urlencode(sorted(params.items()))}"
        if cache_key in self.cache:
            self.cache_hits += 1
            return self.cache[cache_key]["data"]
        headers = {"Accept": "application/json"}
        # v4 read access tokens are JWTs; v3 API keys go in the query string.
        if self.key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {self.key}"
        else:
            params["api_key"] = self.key
        data = common.http_json(f"{TMDB_API}{path}?{urllib.parse.urlencode(params)}", headers) or {}
        self.fetched += 1
        if self.max_age:
            self.cache[cache_key] = {"fetched_at": time.time(), "data": data}
            write_file_atomic(self.cache_path, json.dumps(self.cache))
        return data

    def find_imdb(self, imdb_id):
        """Return TMDB's /find result (movie_results, tv_results, ...) for an IMDb id."""
        return self._get(f"/find/{imdb_id}", external_source="imdb_id")

    def find(self, title, year=None):
        """Return [(kind, title, year, ids)] for TV shows and movies matching title.

        Exact title matches are preferred; without any, the top result of each kind is used.
        """
        found = {}
        for kind, name_key, date_key, year_param in (
                ("tv", "name", "first_air_date", "first_air_date_year"),
                ("movie", "title", "release_date", "primary_release_year")):
            params = {"query": title}
            if year:
                params[year_param] = year
            found[kind] = (self._get(f"/search/{kind}", **params).get("results") or [],
                           name_key, date_key)
        wanted = title.casefold()
        exact = {kind: [r for r in results
                        if wanted in (str(r.get(name_key, "")).casefold(),
                                      str(r.get(f"original_{name_key}", "")).casefold())]
                 for kind, (results, name_key, _) in found.items()}
        if not any(exact.values()):
            exact = {kind: results[:1] for kind, (results, _, _) in found.items()}
        matches = []
        for kind, results in exact.items():
            _, name_key, date_key = found[kind]
            for r in results:
                external = self._get(f"/{kind}/{r['id']}/external_ids")
                ids = {"tmdb": str(r["id"])}
                if external.get("tvdb_id"):
                    ids["tvdb"] = str(external["tvdb_id"])
                if external.get("imdb_id"):
                    ids["imdb"] = external["imdb_id"]
                matches.append((kind, r.get(name_key), (r.get(date_key) or "")[:4], ids))
        return matches


def load_anime_ids(path):
    """Return Kometa's AniDB id mapping, downloading it when the file is missing."""
    if not os.path.exists(path):
        log(f"  downloading the anime id mapping to {path}")
        data = common.http_json(ANIME_IDS_URL, {"Accept": "application/json"})
        if not isinstance(data, dict):
            raise SystemExit(f"Unexpected anime id mapping from {ANIME_IDS_URL}")
        write_file_atomic(path, json.dumps(data))
        return data
    with open(path) as f:
        return json.load(f)


def anidb_ids_for(targets, anime_ids):
    """Return the AniDB ids whose TVDB/TMDB/IMDb ids are among the --title targets."""
    found = set()
    for anidb_id, entry in anime_ids.items():
        candidates = {("tv", "tvdb", str(entry.get("tvdb_id"))),
                      ("tv", "tmdb", str(entry.get("tmdb_show_id"))),
                      ("movie", "tmdb", str(entry.get("tmdb_movie_id")))}
        # imdb_id can hold several comma-separated ids for a series of movies.
        for imdb_id in str(entry.get("imdb_id") or "").split(","):
            candidates |= {("tv", "imdb", imdb_id.strip()), ("movie", "imdb", imdb_id.strip())}
        if candidates & targets:
            found.add(anidb_id)
    return found
