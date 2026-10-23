"""Plex metadata lookups and the Plex account (Discover) as an export target."""

import json
import os
import time
import urllib.parse

from plexhistory import common
from plexhistory.common import APP_NAME, iso, log, write_file_atomic
from plexhistory.plexdb import NUMBERING_ANIDB, NUMBERING_TVDB

PLEX_METADATA_API = "https://metadata.provider.plex.tv/library/metadata"
PLEX_DISCOVER_API = "https://discover.provider.plex.tv"
PLEX_COMMUNITY_API = "https://community.plex.tv/api"
PLEX_ACCOUNT_API = "https://plex.tv/api/v2/user"
PLEX_REQUEST_INTERVAL = 0.5
# community.plex.tv allows 6 updateActivity mutations (aliased ones included) per 60 seconds;
# reads are not limited, and its retryAfter is always 60 whatever is left. A request with more
# than the limit can never pass, so dates go out in small groups, paced to stay inside it.
DATES_PER_REQUEST = 3
DATE_UPDATES_PER_WINDOW = 6
DATE_WINDOW_SECONDS = 61  # a second of margin


# Returned by PlexResolver._fetch for an item that is not cached when lookups are off.
NOT_CACHED = object()


class PlexResolver:
    """Resolves plex:// guids to external ids, cached on disk between runs.

    Without a token only the cache is used and nothing is fetched.
    """

    def __init__(self, token, cache_path, fallbacks=None):
        self.token = token
        self.cache_path = cache_path
        self.cache = {}
        if os.path.exists(cache_path):
            with open(cache_path) as f:
                self.cache = json.load(f)
        # plex episode id -> (show id, season, episode) from the database, for ids Plex dropped.
        self.fallbacks = fallbacks or {}
        self.recovered = set()
        self.fetched = 0
        self.cache_hits = 0

    def save(self):
        write_file_atomic(self.cache_path, json.dumps(self.cache))

    def _headers(self):
        return {"X-Plex-Token": self.token, "Accept": "application/json",
                "X-Plex-Product": APP_NAME, "X-Plex-Client-Identifier": APP_NAME}

    def stats(self):
        return f"{self.fetched} fetched, {self.cache_hits} from cache"

    def _fetch(self, plex_id):
        if plex_id in self.cache:
            self.cache_hits += 1
            return self.cache[plex_id]
        if not self.token:
            return NOT_CACHED
        url = f"{PLEX_METADATA_API}/{plex_id}?includeGuids=1"
        data = common.http_json(url, self._headers())
        time.sleep(PLEX_REQUEST_INTERVAL)
        meta = None
        if data:
            items = data.get("MediaContainer", {}).get("Metadata") or []
            if items:
                m = items[0]
                ids = {}
                for g in m.get("Guid") or []:
                    scheme, _, value = g.get("id", "").partition("://")
                    if scheme in ("imdb", "tmdb", "tvdb") and value:
                        ids[scheme] = value
                meta = {
                    "type": m.get("type"),
                    "title": m.get("title"),
                    "year": m.get("year"),
                    "ids": ids,
                    "show_guid": m.get("grandparentGuid"),
                    "season": m.get("parentIndex"),
                    "episode": m.get("index"),
                }
        self.cache[plex_id] = meta
        self.fetched += 1
        if self.fetched % 100 == 0:
            self.save()
        return meta

    def match_show(self, title, year=None):
        """Return {"title", "year", "ids"} of the Plex show matching title (and year), or None.

        For shows whose agent guid holds an outdated id: The Night Ledger was played as
        thetvdb://431235, which neither Plex nor Simkl resolve; Plex's show is TVDB 469816.
        """
        key = f"match:show:{title}:{year or ''}"
        if key not in self.cache:
            if not self.token:
                return None
            params = {"type": 2, "title": title, "includeGuids": 1}
            if year:
                params["year"] = year
            data = common.http_json(f"{PLEX_METADATA_API}/matches?"
                                    + urllib.parse.urlencode(params),
                                    self._headers())
            time.sleep(PLEX_REQUEST_INTERVAL)
            items = ((data or {}).get("MediaContainer") or {}).get("Metadata") or []
            m = items[0] if items else None
            self.cache[key] = m and {
                "title": m.get("title"), "year": m.get("year"),
                "ids": {scheme: value for scheme, _, value in
                        (g.get("id", "").partition("://") for g in m.get("Guid") or [])
                        if scheme in ("imdb", "tmdb", "tvdb") and value}}
            self.fetched += 1
        return self.cache[key]

    def resolve(self, kind, plex_id):
        """Return a parse_guid style tuple for a plex:// item."""
        meta = self._fetch(plex_id)
        if meta is NOT_CACHED:
            return ("skip", "not in the Plex lookup cache")
        if not meta and kind == "episode" and plex_id in self.fallbacks:
            # Plex re-creates episode ids when it rebuilds a season; the show id survives.
            show_id, season, episode = self.fallbacks[plex_id]
            meta = {"season": season, "episode": episode, "show_guid": f"plex://show/{show_id}"}
            self.recovered.add(plex_id)
            log(f"    plex://episode/{plex_id} is gone from Plex, using show {show_id} "
                f"S{season:02}E{episode:02} from the database", 3)
        if not meta:
            return ("skip", "not found on plex metadata provider")
        if kind == "movie":
            ids = dict(meta["ids"])
            if not ids:
                return ("skip", "no external ids")
            return ("movie", ids, meta["title"], meta["year"])
        if meta["season"] is None or meta["episode"] is None or not meta["show_guid"]:
            return ("skip", "episode without numbering")
        return self.resolve_show_episode(meta["show_guid"].rpartition("/")[2], meta["season"],
                                         meta["episode"], meta.get("ids"))

    def resolve_show_episode(self, show_id, season, episode, episode_ids=None):
        """Return a parse_guid style tuple for episode season/episode of a plex:// show."""
        show = self._fetch(show_id)
        if show is NOT_CACHED:
            return ("skip", "not in the Plex lookup cache")
        if not show or not show["ids"]:
            return ("skip", "show without external ids")
        # The last element holds the episode's own external ids (e.g. its TVDB episode id).
        return ("episode", dict(show["ids"]), int(season), int(episode),
                NUMBERING_TVDB, show["title"], show["year"], dict(episode_ids or {}))


class PlexTarget:
    """Marks items watched on the Plex account (Discover) and sets their watch date.

    Plex Web's "Mark as Watched" records the current time; the date is then changed on the
    resulting watch history entry, as "Edit Watched Date" does. Both APIs are undocumented.
    """

    def __init__(self, token, cache, anime_ids, pending_path=None):
        self.token = token
        self.cache = cache  # shared with PlexResolver, which saves it
        self.anime_ids = anime_ids
        self.uuid = None
        # {plex id: watch epoch} of items being marked whose date is not set yet; kept on disk so
        # an interrupted or failed date step is repaired by the next run (repair_pending).
        self.pending_path = pending_path
        self.update_times = []  # when recent date updates were sent, for _wait_for_date_slots
        self.pending = {}
        if pending_path and os.path.exists(pending_path):
            with open(pending_path) as f:
                self.pending = json.load(f)

    def _save_pending(self):
        if self.pending_path:
            write_file_atomic(self.pending_path, json.dumps(self.pending, indent=1))

    def _headers(self):
        return {"X-Plex-Token": self.token, "Accept": "application/json",
                "Content-Type": "application/json", "X-Plex-Product": APP_NAME,
                "X-Plex-Client-Identifier": APP_NAME,
                "User-Agent": f"{APP_NAME}/{common.APP_VERSION}"}

    def _gql(self, query, variables, retries=5):
        for attempt in range(retries + 1):
            result = common.http_json(PLEX_COMMUNITY_API, self._headers(),
                                      {"query": query, "variables": variables}) or {}
            errors = result.get("errors") or []
            # The community API limits request rates and says how long to wait.
            limited = [e for e in errors
                       if (e.get("extensions") or {}).get("code") == "RATE_LIMITED"]
            operation = query.split("(")[0].split()[-1]
            if limited and attempt < retries:
                wait = int((limited[0].get("extensions") or {}).get("retryAfter") or 60)
                log(f"Plex GraphQL rate limit on {operation}, waiting {wait}s", 0)
                time.sleep(wait)
                log(f"    retrying {operation} (attempt {attempt + 2})", 3)
                continue
            if errors:
                raise SystemExit(f"Plex GraphQL error: {errors}")
            return result.get("data") or {}

    def account(self):
        if not self.uuid:
            self.uuid = (common.http_json(PLEX_ACCOUNT_API, self._headers()) or {}).get("uuid")
            if not self.uuid:
                raise SystemExit("Could not read the Plex account (check PLEX_TOKEN)")
        return self.uuid

    def _cached(self, key, fetch):
        if key not in self.cache:
            self.cache[key] = fetch()
            time.sleep(PLEX_REQUEST_INTERVAL)
        return self.cache[key]

    def _match(self, plex_type, guid):
        def fetch():
            url = f"{PLEX_METADATA_API}/matches?" + urllib.parse.urlencode(
                {"type": plex_type, "guid": guid})
            items = ((common.http_json(url, self._headers()) or {}).get("MediaContainer") or {}) \
                .get("Metadata") or []
            return items[0].get("ratingKey") if items else None
        return self._cached(f"target:match:{plex_type}:{guid}", fetch)

    def _children(self, plex_id):
        """Return {index: ratingKey} for a show's seasons or a season's episodes."""
        return {index: key for index, key, *_ in self._child_items(plex_id)}

    def _child_items(self, plex_id):
        """Return [index, ratingKey, title, air date] of a show's seasons or a season's
        episodes."""
        def fetch():
            # Pages default to 20 items; paging parameters must be headers, as Cloudflare
            # rejects them in the query string, and pages over 300 items are refused.
            items, start = [], 0
            while True:
                headers = {**self._headers(), "X-Plex-Container-Start": str(start),
                           "X-Plex-Container-Size": "200"}
                page = (common.http_json(f"{PLEX_METADATA_API}/{plex_id}/children", headers) or {}) \
                    .get("MediaContainer") or {}
                items += page.get("Metadata") or []
                start += 200
                if not page.get("Metadata") or start >= (page.get("totalSize") or 0):
                    break
            return [[m["index"], m["ratingKey"], m.get("title"), m.get("originallyAvailableAt")]
                    for m in items if m.get("index") is not None]
        return self._cached(f"target:items:{plex_id}", fetch)

    def season_episodes(self, tvdb_id, season):
        """Return [(episode, title, air date)] of a TVDB show's season on Plex, or None."""
        show = self._match(2, f"tvdb://{tvdb_id}")
        season_key = self._children(show).get(season) if show else None
        if not season_key:
            return None
        return [(index, title, date) for index, _, title, date in self._child_items(season_key)]

    def item_id(self, kind, rec):
        """Return (Plex ratingKey, None) or (None, reason it could not be found)."""
        ids = dict(rec["ids"])
        season, episode = rec.get("season"), rec.get("episode")
        if kind == "episode" and rec["numbering"] == NUMBERING_ANIDB and season == 0:
            # Specials are matched to TVDB/Plex episodes beforehand (export.plan_plex_specials).
            if not rec.get("plex_special"):
                return None, rec.get("special_reason") or "AniDB specials have no TVDB mapping"
            tvdb_id, season, episode = rec["plex_special"]
            ids = {"tvdb": str(tvdb_id)}
        elif kind == "episode" and rec["numbering"] == NUMBERING_ANIDB:
            entry = self.anime_ids.get(str(ids.get("anidb"))) or {}
            tvdb_season = entry.get("tvdb_season")
            if not entry.get("tvdb_id") or not isinstance(tvdb_season, int) or tvdb_season < 0:
                return None, "no TVDB season mapping for the AniDB id"
            ids = {"tvdb": str(entry["tvdb_id"])}
            season, episode = tvdb_season, episode + (entry.get("tvdb_epoffset") or 0)
        plex_type = 1 if kind == "movie" else 2
        found = next((key for scheme in ("tvdb", "tmdb", "imdb") if scheme in ids
                      for key in [self._match(plex_type, f"{scheme}://{ids[scheme]}")] if key),
                     None)
        if not found:
            return None, f"{'movie' if kind == 'movie' else 'show'} not found on Plex"
        if kind == "movie":
            return found, None
        season_key = self._children(found).get(season)
        if not season_key:
            return None, f"season {season} not found on Plex"
        episode_key = self._children(season_key).get(episode)
        if not episode_key:
            return None, f"S{season:02}E{episode:02} not found on Plex"
        return episode_key, None

    def is_watched(self, plex_id):
        """Whether the account's watch state (Discover userState) has the item as watched."""
        url = f"{PLEX_DISCOVER_API}/library/metadata/{plex_id}/userState"
        log(f"    {plex_id}: reading watch state", 3)
        states = ((common.http_json(url, self._headers()) or {}).get("MediaContainer") or {}) \
            .get("UserState") or []
        watched = any((s.get("viewCount") or 0) > 0 for s in states)
        log(f"    {plex_id}: {'watched' if watched else 'not watched'}", 3)
        return watched

    def _newest_entries(self, plex_ids, first):
        """Return {plex id: id of its newest entry} for those among the newest `first` (2-100)
        watch history entries."""
        first = min(100, max(2, first))
        log(f"    reading the newest {first} watch history entries for {len(plex_ids)} items", 3)
        nodes = self._gql(
            "query newestHistory($id: ID!, $first: PaginationInt!) { user(id: $id) { "
            "watchHistory(first: $first) { nodes { id metadataItem { id } } } } }",
            {"id": self.account(), "first": first})["user"]["watchHistory"]["nodes"]
        found = {}
        for n in nodes:
            plex_id = (n.get("metadataItem") or {}).get("id")
            if plex_id in plex_ids and plex_id not in found:
                found[plex_id] = n["id"]
        log(f"    found the entries of {len(found)} of {len(plex_ids)} items", 3)
        return found

    def _wait_for_date_slots(self, count):
        """Wait until count more date updates fit into the rate limit's window."""
        now = time.time()
        recent = sorted(t for t in self.update_times if now - t < DATE_WINDOW_SECONDS)
        excess = len(recent) + count - DATE_UPDATES_PER_WINDOW
        if excess > 0:
            # The excess oldest updates must leave the window first.
            wait = max(0.0, recent[excess - 1] + DATE_WINDOW_SECONDS - now)
            log(f"    pacing date updates: waiting {wait:.0f}s", 3)
            time.sleep(wait)
            now += wait
            recent = recent[excess:]
        self.update_times = recent + [now] * count

    def _set_dates(self, dates):
        """Set history entries' dates, {plex id: (entry id, epoch)}, with aliased updateActivity
        mutations, DATES_PER_REQUEST per request and paced to the rate limit, clearing each
        group from the pending journal once it is set."""
        items = list(dates.items())
        for start in range(0, len(items), DATES_PER_REQUEST):
            group = items[start:start + DATES_PER_REQUEST]
            params = ", ".join(f"$a{i}: ID!, $d{i}: UpdateActivityInput!"
                               for i in range(len(group)))
            fields = " ".join(f"m{i}: updateActivity(id: $a{i}, input: $d{i}) {{ id }}"
                              for i in range(len(group)))
            variables = {}
            for i, (_, (activity, epoch)) in enumerate(group):
                variables[f"a{i}"] = activity
                variables[f"d{i}"] = {"date": iso(epoch)}
            self._wait_for_date_slots(len(group))
            log("    setting dates: " + ", ".join(
                f"{plex_id} entry {activity} -> {iso(epoch)}"
                for plex_id, (activity, epoch) in group), 3)
            self._gql(f"mutation updateActivityDates({params}) {{ {fields} }}", variables)
            for plex_id, _ in group:
                del self.pending[plex_id]
            self._save_pending()
            log(f"    dates set for {len(group)} items; {len(self.pending)} still pending", 3)

    def mark_batch(self, items):
        """Mark items [(plex id, epoch)] watched and set their dates: one history query per batch
        finds the new entries, and their dates go out DATES_PER_REQUEST per request.

        Returns an outcome per item: "already watched", "watched" or "date not set". Items are
        journaled as pending before they are marked and cleared once their date is set, so an
        interruption in between leaves a record for repair_pending.
        """
        outcomes, marked = [], {}
        for plex_id, epoch in items:
            if plex_id in marked or self.is_watched(plex_id):
                outcomes.append("already watched")
                continue
            self.pending[plex_id] = epoch
            self._save_pending()
            log(f"    {plex_id}: journaled as pending, marking watched", 3)
            common.http_json(f"{PLEX_DISCOVER_API}/actions/scrobble?" + urllib.parse.urlencode(
                {"identifier": "tv.plex.provider.discover", "key": plex_id}), self._headers())
            log(f"    {plex_id}: marked (dated now until its date is set)", 3)
            marked[plex_id] = epoch
            outcomes.append(None)
            time.sleep(PLEX_REQUEST_INTERVAL)
        # The new entries are the newest in the watch history; allow a moment for them to appear.
        dated = set()
        if marked:
            log(f"    batch: {len(marked)} marked, {len(items) - len(marked)} already watched; "
                f"setting dates", 3)
        for _ in range(3):
            missing = [p for p in marked if p not in dated]
            if not missing:
                break
            found = self._newest_entries(set(missing), len(marked) + 10)
            self._set_dates({p: (activity, marked[p]) for p, activity in found.items()})
            dated |= set(found)
            if len(dated) < len(marked):
                time.sleep(1)
        return [outcome or ("watched" if plex_id in dated else "date not set")
                for (plex_id, _), outcome in zip(items, outcomes)]

    def mark_watched(self, plex_id, epoch):
        """Mark one item watched at epoch (mark_batch)."""
        return self.mark_batch([(plex_id, epoch)])[0]

    def repair_pending(self):
        """Finish items whose date step did not complete (interrupted run, "date not set").

        An item that is not watched was never marked and is dropped from the journal (the run
        marks it normally). A marked one still carries the date of marking, so its entry is
        among the newest; its date is set. Returns {outcome: count}; items whose entry is not
        found stay pending and are listed.
        """
        counts, marked = {}, {}
        log(f"    repairing {len(self.pending)} pending items", 3)
        for plex_id, epoch in list(self.pending.items()):
            if self.is_watched(plex_id):
                marked[plex_id] = epoch
            else:
                del self.pending[plex_id]
                self._save_pending()
                counts["not marked yet"] = counts.get("not marked yet", 0) + 1
            time.sleep(PLEX_REQUEST_INTERVAL)
        if marked:
            found = self._newest_entries(set(marked), 100)
            self._set_dates({p: (activity, marked[p]) for p, activity in found.items()})
            if found:
                counts["date set"] = len(found)
            for plex_id in marked:
                if plex_id not in found:
                    counts["entry not found"] = counts.get("entry not found", 0) + 1
                    log(f"  pending {plex_id}: no recent watch history entry; check it with "
                        f"plex-state history --item {plex_id} --all", 0)
        return counts
