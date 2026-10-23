"""Turning watched items into records and sending them to Simkl or Plex."""

import json
import os
import re
import time
from datetime import datetime

from plexhistory import common
from plexhistory.common import Progress, iso, log, update_env_file, write_file_atomic
from plexhistory.filters import select
from plexhistory.plex import PLEX_REQUEST_INTERVAL, PlexResolver, PlexTarget
from plexhistory.plexdb import (NUMBERING_ANIDB, NUMBERING_TVDB, episode_guid_of_show,
                                load_details, load_episode_fallbacks, load_local_episodes,
                                load_mismatched_plays, load_titles, load_watched, load_years,
                                parse_guid)
from plexhistory.simkl import SIMKL_POST_INTERVAL, Simkl, SimklCatalog, SimklLibrary
from plexhistory.tmdb import Tmdb, load_anime_ids

BATCH_SIZE = 100


class IdMerger:
    """Gives the same key to items that share any external id."""

    def __init__(self):
        self.alias = {}

    def key(self, ids):
        keys = [f"{k}:{v}" for k, v in ids.items()]
        canon = next((self.alias[k] for k in keys if k in self.alias), keys[0])
        for k in keys:
            self.alias.setdefault(k, canon)
        return canon


def map_local(guid, local_episodes, resolver):
    """Identify a local:// episode through its show guid and numbers (load_local_episodes).

    Returns (parse_guid style tuple, the guid it was read as), or None. Iron Spire's files
    were never matched (local://44655) but their show is thetvdb://382752.
    """
    if guid not in local_episodes:
        return None
    show_guid, season, episode = local_episodes[guid]
    agent_guid = episode_guid_of_show(show_guid, season, episode)
    if agent_guid:
        return parse_guid(agent_guid), agent_guid
    if show_guid.startswith("plex://show/"):
        if resolver is None:
            return ("skip", "local:// of a plex:// show without --plex-token"), show_guid
        return resolver.resolve_show_episode(show_guid.rpartition("/")[2], season,
                                             episode), show_guid
    return None


def collect(watched, resolver, skipped, titles, years=None, episode_titles=None,
            local_episodes=None, counts=None):
    """Turn watched guids into merged movie and episode records.

    titles ({guid: title} from the database) fill the "label" shown in dry-run output and the
    title fallback for movies; years ({guid: year}) the year of that fallback; episode_titles
    ({guid: episode title}) are used to check fallbacks against Simkl's catalog;
    local_episodes (load_local_episodes) identify local:// episodes through their show.
    counts["local"] gets the number of local:// episodes identified that way.
    """
    years = years or {}
    episode_titles = episode_titles or {}
    local_episodes = local_episodes or {}
    counts = {} if counts is None else counts
    movie_ids, show_ids = IdMerger(), IdMerger()
    movies = {}    # key -> {"ids", "title", "year", "watched_at"}
    episodes = {}  # (show key, season, ep) -> {...}
    plex_total = sum(parse_guid(g)[0] == "plex" for g, t in watched.items()
                     if t is not None) if resolver else 0
    if plex_total:
        log(f"  {plex_total} plex:// items to resolve via the Plex metadata provider "
            f"({len(resolver.cache)} lookups cached in {resolver.cache_path})")
        progress = Progress(plex_total, "resolved plex:// items", resolver.stats)
    for guid, epoch in watched.items():
        if epoch is None:
            skipped["no watch date"] = skipped.get("no watch date", 0) + 1
            log(f"    skip {guid}: no watch date", 3)
            continue
        parsed, source = parse_guid(guid), guid
        if parsed == ("skip", "local"):
            mapped = map_local(guid, local_episodes, resolver)
            if mapped:
                parsed, source = mapped
                if parsed[0] == "episode":
                    counts["local"] = counts.get("local", 0) + 1
                    log(f"    {guid}: identified as {source}", 3)
        if parsed[0] == "plex":
            if resolver is None:
                parsed = ("skip", "plex:// without --plex-token")
            else:
                parsed = resolver.resolve(parsed[1], parsed[2])
                progress.step()
        if parsed[0] == "skip":
            skipped[parsed[1]] = skipped.get(parsed[1], 0) + 1
            log(f"    skip {guid}: {parsed[1]}", 3)
            continue
        if parsed[0] == "movie":
            ids = parsed[1]
            title, year = (parsed[2], parsed[3]) if len(parsed) > 2 else (None, None)
            rec = movies.setdefault(movie_ids.key(ids), {"ids": {}, "title": title,
                                                         "year": year, "watched_at": epoch})
            rec["label"] = rec.get("label") or title or titles.get(guid)
            rec["db_year"] = rec.get("db_year") or years.get(guid)
            rec["ids"].update(ids)
            rec["title"] = rec["title"] or title
            rec["year"] = rec["year"] or year
            rec["watched_at"] = min(rec["watched_at"], epoch)
        else:
            ids, season, ep, numbering = parsed[1:5]
            title, year = (parsed[5], parsed[6]) if len(parsed) > 5 else (None, None)
            show = show_ids.key(ids)
            rec = episodes.setdefault((show, season, ep), {
                "show": show, "ids": {}, "title": title, "year": year, "season": season,
                "episode": ep, "numbering": numbering, "watched_at": epoch})
            rec["label"] = rec.get("label") or title or titles.get(guid)
            rec["episode_title"] = rec.get("episode_title") or episode_titles.get(guid)
            if len(parsed) > 7 and parsed[7].get("tvdb"):
                rec["episode_tvdb"] = parsed[7]["tvdb"]
            if not source.startswith("plex://") and numbering == NUMBERING_TVDB:
                rec["legacy_tvdb"] = True
            rec["ids"].update(ids)
            rec["title"] = rec["title"] or title
            rec["year"] = rec["year"] or year
            rec["watched_at"] = min(rec["watched_at"], epoch)
    # Episodes of one show share its ids and title, so they are sent as one show.
    shows = {}
    for rec in episodes.values():
        show = shows.setdefault(rec["show"], {"ids": {}, "title": None, "year": None})
        show["ids"].update(rec["ids"])
        if rec["title"] and not show["title"]:
            show["title"], show["year"] = rec["title"], rec["year"]
    for rec in episodes.values():
        show = shows[rec["show"]]
        rec["ids"] = show["ids"]
        rec["title"], rec["year"] = show["title"], show["year"]
    return movies, episodes


def episode_ranges(season, numbers):
    """Format episode numbers as e.g. "S01E01-E06, S01E08"."""
    parts, numbers = [], sorted(numbers)
    start = prev = numbers[0]
    for n in numbers[1:] + [None]:
        if n is not None and n == prev + 1:
            prev = n
            continue
        parts.append(f"S{season:02}E{start:02}" + (f"-E{prev:02}" if prev != start else ""))
        start = prev = n
    return ", ".join(parts)


def batch_summary(units):
    """Return one line per movie and per show (with episode ranges) in a batch."""
    lines, shows = [], {}
    for kind, rec, *_ in units:
        if kind == "movie":
            label = rec["label"] or ", ".join(f"{k}={v}" for k, v in rec["ids"].items())
            lines.append(f"movie: {label}" + (f" ({rec['year']})" if rec["year"] else ""))
        else:
            show = shows.setdefault(rec["show"], {"label": None, "seasons": {}})
            show["label"] = show["label"] or rec["label"]
            show["seasons"].setdefault(rec["season"], []).append(rec["episode"])
            show["ids"] = rec["ids"]
    for show in shows.values():
        label = show["label"] or ", ".join(f"{k}={v}" for k, v in show["ids"].items())
        lines.append(f"{label}: " + ", ".join(
            episode_ranges(season, eps) for season, eps in sorted(show["seasons"].items())))
    return lines


def item_keys(kind, rec):
    """Cache keys of a movie or episode, one per external id."""
    if kind == "movie":
        return {f"movie {k}:{v}" for k, v in rec["ids"].items()}
    return {f"{k}:{v} S{rec['season']:02}E{rec['episode']:02}" for k, v in rec["ids"].items()}


def item_label(kind, rec):
    label = rec["label"] or ", ".join(f"{k}={v}" for k, v in rec["ids"].items())
    return f"movie: {label}" if kind == "movie" else \
        f"{label} S{rec['season']:02}E{rec['episode']:02}"


def episode_key(episode):
    """Identify an episode object of a payload: by TVDB episode id or by season and number."""
    if episode.get("ids"):
        return ("id", str(episode["ids"].get("tvdb")))
    return ("number", episode.get("season", 1), episode.get("number"))


def failed_units(batch, result):
    """Return the indexes of batch units that Simkl listed in not_found.

    not_found entries echo the input: a movie, or a show with the seasons/episodes it carried.
    """
    failed = set()
    for media, entries in (result.get("not_found") or {}).items():
        for entry in entries:
            ids = {f"{k}:{v}" for k, v in (entry.get("ids") or {}).items()}
            keys, whole_seasons = set(), set()
            for season in entry.get("seasons") or []:
                episodes = season.get("episodes") or []
                keys |= {episode_key({**e, "season": season.get("number")}) for e in episodes}
                if not episodes:
                    whole_seasons.add(season.get("number"))
            keys |= {episode_key(e) for e in entry.get("episodes") or []}
            for i, (kind, _, variant) in enumerate(batch):
                # A movie can be sent as an episode (part of a multi-part entry).
                if ("episode" not in variant) != (media == "movies"):
                    continue
                if not ids & {f"{k}:{v}" for k, v in variant["item"]["ids"].items()}:
                    continue
                episode = variant.get("episode")
                if episode and (keys or whole_seasons) \
                        and episode_key(episode) not in keys \
                        and episode.get("season") not in whole_seasons:
                    continue
                failed.add(i)
    return failed


class SyncState:
    """Per Simkl account: items already posted, and items Simkl could not match."""

    def __init__(self, sent_path, not_found_path, account, not_found_days):
        self.sent_path, self.not_found_path = sent_path, not_found_path
        self.sent_all = self._load(sent_path)
        self.not_found_all = self._load(not_found_path)
        self.sent = self.sent_all.setdefault(account, {})
        self.not_found = self.not_found_all.setdefault(account, {})
        self.max_age = not_found_days * 86400
        if self.max_age:
            now = time.time()
            for key in [k for k, v in self.not_found.items() if now - v["at"] >= self.max_age]:
                del self.not_found[key]

    @staticmethod
    def _load(path):
        if not os.path.exists(path):
            return {}
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            log(f"  ignoring {path}: not in the per-account format", 0)
            return {}
        return data

    def was_sent(self, keys):
        return any(k in self.sent for k in keys)

    def was_not_found(self, keys):
        return bool(self.max_age) and any(k in self.not_found for k in keys)

    def record(self, batch, failed):
        now = time.time()
        for i, (kind, rec) in enumerate(batch):
            keys = item_keys(kind, rec)
            for key in keys:
                if i in failed:
                    self.not_found[key] = {"at": now, "label": item_label(kind, rec)}
                else:
                    self.sent[key] = now
                    self.not_found.pop(key, None)
        write_file_atomic(self.sent_path, json.dumps(self.sent_all))
        write_file_atomic(self.not_found_path, json.dumps(self.not_found_all, indent=1))


def forget_records(sent_path, not_found_path, pattern, dry_run):
    """Remove sent / not-found records whose key (e.g. "anidb:44015 S01E02", "movie imdb:tt1")
    or label matches the case-insensitive regex pattern, for every account in the files."""
    regex = re.compile(pattern, re.IGNORECASE)
    for path, indent in ((sent_path, None), (not_found_path, 1)):
        if not os.path.exists(path):
            continue
        with open(path) as f:
            data = json.load(f)
        removed = 0
        for account, records in data.items():
            for key in [k for k, v in records.items() if regex.search(k) or (
                    isinstance(v, dict) and regex.search(v.get("label") or ""))]:
                label = records[key].get("label") if isinstance(records[key], dict) else None
                log(f"  {'would remove' if dry_run else 'removed'} {key}"
                    + (f" ({label})" if label else "") + f" from {path}, account {account}", 0)
                del records[key]
                removed += 1
        if removed and not dry_run:
            write_file_atomic(path, json.dumps(data, indent=indent))
        log(f"{path}: {removed} records {'match' if dry_run else 'removed'}", 0)


def anidb_episode(anime_ids, tvdb_id, season, episode):
    """Map a TVDB season/episode to (AniDB id, AniDB episode) with Kometa's mapping.

    A TVDB season can hold several AniDB entries (cours); the one with the largest episode
    offset below the episode is taken.
    """
    candidates = [(entry.get("tvdb_epoffset") or 0, anidb_id)
                  for anidb_id, entry in anime_ids.items()
                  if str(entry.get("tvdb_id")) == str(tvdb_id) and entry.get("tvdb_season") == season
                  and (entry.get("tvdb_epoffset") or 0) < episode]
    if not candidates:
        return None
    offset, anidb_id = max(candidates)
    return int(anidb_id), episode - offset


def movie_variants(rec):
    """Ways to identify a movie to Simkl, in the order they are tried."""
    item = {"ids": rec["ids"]}
    if rec["title"]:
        item.update(title=rec["title"], year=rec["year"])
    variants = [{"name": "ids", "item": item}]
    if not rec["title"] and rec.get("label"):
        fallback = {"ids": rec["ids"], "title": rec["label"]}
        if rec.get("db_year"):
            fallback["year"] = rec["db_year"]
        variants.append({"name": "title and year", "item": fallback})
    return variants


def episode_variants(rec, anime_ids):
    """Ways to identify an episode to Simkl, in the order they are tried.

    Plex numbers anime episodes like TMDB (later cours stay in season 1), while Simkl maps
    TVDB seasons, so Plex episodes go by their TVDB episode id first.
    """
    show = {"ids": rec["ids"]}
    if rec["title"]:
        show.update(title=rec["title"], year=rec["year"])
    if rec["numbering"] == NUMBERING_TVDB:
        show["use_tvdb_anime_seasons"] = True
    numbers = {"name": "season/episode numbers", "item": show,
               "episode": {"season": rec["season"],
                           "number": rec.get("simkl_episode", rec["episode"])}}
    variants = []
    if rec.get("episode_tvdb"):
        variants.append({"name": "TVDB episode id", "item": show,
                         "episode": {"ids": {"tvdb": int(rec["episode_tvdb"])}}})
    variants.append(numbers)
    # Plex numbering is not TVDB numbering, so only legacy TVDB-numbered guids are mapped.
    if rec.get("legacy_tvdb") and "tvdb" in rec["ids"]:
        mapped = anidb_episode(anime_ids, rec["ids"]["tvdb"], rec["season"], rec["episode"])
        if mapped:
            variants.append({"name": "AniDB id from anime_ids.json",
                             "item": {"ids": {"anidb": mapped[0]}},
                             "episode": {"season": 1, "number": mapped[1]}})
    return variants


def season_overflow_variant(rec, catalog):
    """Episode numbers past the end of a TVDB season continue in the next TVDB season.

    Plex (like TMDB) keeps later cours of an anime in season 1, e.g. S01E13 when TVDB has
    12 episodes in season 1 and S02E01 for that episode; Simkl's catalog gives the season sizes.
    """
    size = catalog.tvdb_season_sizes(rec["ids"]["tvdb"]).get(rec["season"])
    if not size or rec["episode"] <= size:
        return None
    show = {"ids": rec["ids"], "use_tvdb_anime_seasons": True}
    if rec["title"]:
        show.update(title=rec["title"], year=rec["year"])
    return {"name": "TVDB season from Simkl's catalog", "item": show,
            "episode": {"season": rec["season"] + 1, "number": rec["episode"] - size}}


def plex_title_variant(rec, resolver):
    """Send a TVDB-numbered episode with the current ids of the Plex show with its title.

    Agent guids can hold outdated show ids that Simkl doesn't know: The Night Ledger
    played as thetvdb://431235 is TVDB 469816, Iron Spire thetvdb://382752 is 393650. The
    Plex show's title must equal the record's, and its year too when the record has one.
    """
    title = rec["title"] or rec.get("label")
    if not title:
        return None
    found = resolver.match_show(title, rec["year"])
    if not found or not found["ids"] or title_key(found["title"]) != title_key(title) \
            or (rec["year"] and found["year"] and int(found["year"]) != int(rec["year"])):
        return None
    if {(k, str(v)) for k, v in found["ids"].items()} <= \
            {(k, str(v)) for k, v in rec["ids"].items()}:
        return None
    show = {"ids": found["ids"], "title": found["title"], "use_tvdb_anime_seasons": True}
    if found["year"]:
        show["year"] = found["year"]
    return {"name": "show ids from Plex by title", "item": show,
            "episode": {"season": rec["season"], "number": rec["episode"]}}


def tmdb_title_variant(rec, tmdb):
    """Identify a movie known only by IMDb id with TMDB's id, title and year."""
    found = (tmdb.find_imdb(rec["ids"]["imdb"]).get("movie_results") or [None])[0]
    if not found:
        return None
    item = {"ids": {**rec["ids"], "tmdb": str(found["id"])}, "title": found.get("title")}
    if found.get("release_date"):
        item["year"] = int(found["release_date"][:4])
    return {"name": "TMDB title", "item": item}


# A watch counts as matching an episode's air date from a day before (time zones) to this
# many days after it.
AIR_DATE_WINDOW_DAYS = 14
GENERIC_TITLE = re.compile(r"^\s*(episode|ep\.?|special)?\s*\d*\s*$", re.IGNORECASE)


def title_key(title):
    return re.sub(r"\W+", "", (title or "").casefold())


def aired_near(episode, watched_at):
    try:
        aired = datetime.fromisoformat(episode.get("date") or "").timestamp()
    except ValueError:
        return False
    return aired - 86400 <= watched_at <= aired + AIR_DATE_WINDOW_DAYS * 86400


def verify_episode(candidates, title, watched_at, catalog):
    """Pick which of candidates [(AniDB id, episode)] a play was, using Simkl's catalog.

    A real episode title must match exactly one episode of the candidates' entries; without
    one, exactly one candidate must have aired shortly before the watch. When both point to
    one episode each, they must agree. Returns ((AniDB id, episode), "episode title" or
    "air date") or (None, reason).
    """
    by_date = {(anidb, number) for anidb, number in candidates
               for e in catalog.anidb_episodes(anidb)
               if e.get("episode") == number and aired_near(e, watched_at)}
    if title and not GENERIC_TITLE.match(title):
        by_title = {(anidb, e["episode"]) for anidb in dict(candidates)
                    for e in catalog.anidb_episodes(anidb)
                    if title_key(e.get("title")) == title_key(title)}
        if len(by_title) != 1:
            return None, f"title {title!r} matches {len(by_title) or 'no'} episodes"
        if len(by_date) == 1 and by_date != by_title:
            return None, f"title {title!r} and the air date point to different episodes"
        return by_title.pop(), "episode title"
    if len(by_date) == 1:
        return by_date.pop(), "air date"
    return None, f"{len(by_date) or 'no'} candidates aired shortly before the watch"


def last_episode(episodes):
    return max((e.get("episode") or 0 for e in episodes), default=0)


def absolute_target(rec, anime_ids, catalog):
    """Map an AniDB episode number past the end of its Simkl entry; return (AniDB id, Simkl
    episode, whether seasons were added up) or None.

    First the number is read as an episode of the entry's own TVDB season, which HAMA uses for
    later cours: Orbital Knights 2015 episode 13 is TVDB S01E13 (its own episode 1), Agent Household
    episode 13 is S01E13, episode 1 of the next cour's AniDB entry. Simkl's TVDB numbers of
    the entries anime_ids.json maps to that season must contain it.

    Otherwise it is read as a show-wide (absolute) number: HSA S2E25 stored as anidb-43154
    episode 38 (13 + 25). The earlier TVDB seasons' sizes come from Simkl's entries of the
    AniDB ids mapped to them, and the result must be an episode of the same entry.
    """
    anidb = rec["ids"]["anidb"]
    entry = anime_ids.get(str(anidb)) or {}
    tvdb_id, season = entry.get("tvdb_id"), entry.get("tvdb_season")
    if not tvdb_id or not isinstance(season, int) or season < 1:
        return None
    own = catalog.anidb_episodes(anidb)
    if not own or rec["episode"] <= last_episode(own):
        return None

    def tvdb_episode(entry_episodes, wanted_season, number):
        return next((e for e in entry_episodes if (e.get("tvdb") or {}).get("season") == wanted_season
                     and (e.get("tvdb") or {}).get("episode") == number), None)

    same_season = [int(other_id) for other_id, other in anime_ids.items()
                   if str(other.get("tvdb_id")) == str(tvdb_id)
                   and other.get("tvdb_season") == season and int(other_id) != anidb]
    for other_id in [anidb] + same_season:
        target = tvdb_episode(own if other_id == anidb else catalog.anidb_episodes(other_id),
                              season, rec["episode"])
        if target:
            return other_id, target, False
    sizes = {}
    for other_id, other in anime_ids.items():
        other_season = other.get("tvdb_season")
        if str(other.get("tvdb_id")) != str(tvdb_id) or not isinstance(other_season, int) \
                or not 1 <= other_season < season:
            continue
        for e in catalog.anidb_episodes(int(other_id)):
            tvdb = e.get("tvdb") or {}
            if isinstance(tvdb.get("season"), int) and 1 <= tvdb["season"] < season:
                sizes[tvdb["season"]] = max(sizes.get(tvdb["season"], 0), tvdb.get("episode") or 0)
    if set(sizes) != set(range(1, season)):
        return None
    target = tvdb_episode(own, season, rec["episode"] - sum(sizes.values()))
    return (anidb, target, True) if target else None


def absolute_number_variant(rec, anime_ids, catalog):
    """Send an episode number past the end of its entry as the episode absolute_target finds.

    When seasons were added up the result must pass verify_episode. A number within the
    entry's own TVDB season is confirmed by Simkl's TVDB numbers and needs no check.
    """
    found = absolute_target(rec, anime_ids, catalog)
    if not found:
        return None
    anidb, target, summed = found
    matched, reason = verify_episode([(anidb, target["episode"])], rec.get("episode_title"),
                                     rec["watched_at"], catalog) if summed else (True, "direct")
    if not matched:
        log(f"    absolute episode number: {item_label('episode', rec)} would be anidb-{anidb} "
            f"episode {target['episode']}, not used: {reason}", 2)
        return None
    show = {"ids": rec["ids"]} if anidb == rec["ids"]["anidb"] else {"ids": {"anidb": anidb}}
    if rec["title"] and anidb == rec["ids"]["anidb"]:
        show.update(title=rec["title"], year=rec["year"])
    return {"name": "absolute episode number", "item": show,
            "episode": {"season": 1, "number": target["episode"]}}


def simkl_special(rec, catalog):
    """Find the Simkl special an AniDB special (season 0) play was.

    Simkl numbers specials like AniDB's S1, S2, ... for most entries (Heart Swap, Lancer
    Saga picture dramas), but not all: Blob's "Special 24.5" is AniDB S24 and Simkl's
    special 1. So the AniDB number is used if Simkl lists it, otherwise the one special whose
    title matches the play's, otherwise the one that aired shortly before the watch.
    Returns (Simkl special or None, how it was found or why not); (None, None) when Simkl
    doesn't know the entry.
    """
    entry = catalog.entry(anidb=rec["ids"]["anidb"])
    if not entry or entry[0] != "anime":
        return None, None
    specials = [e for e in catalog.episodes(*entry, specials=True) if e.get("type") == "special"]
    same = [e for e in specials if e.get("episode") == rec["episode"]]
    if same:
        return same[0], "AniDB number"
    title = rec.get("episode_title")
    if title and not GENERIC_TITLE.match(title):
        by_title = [e for e in specials if title_key(e.get("title")) == title_key(title)]
        if len(by_title) == 1:
            return by_title[0], "episode title"
    by_date = [e for e in specials if aired_near(e, rec["watched_at"])]
    if len(by_date) == 1:
        return by_date[0], "air date"
    return None, (f"Simkl's entry has no special {rec['episode']} and none of its "
                  f"{len(specials)} specials matches the title or aired shortly before the watch")


def is_anidb_special(rec):
    return rec["season"] == 0 and rec["numbering"] == NUMBERING_ANIDB and "anidb" in rec["ids"]


def match_specials(episodes, catalog):
    """Give AniDB specials the Simkl special number they were (simkl_special); the record keeps
    its AniDB number. Returns {record key: reason} for specials no Simkl special matches."""
    unmatched = {}
    for key, rec in episodes.items():
        if not is_anidb_special(rec):
            continue
        special, how = simkl_special(rec, catalog)
        if special:
            rec["simkl_episode"] = special["episode"]
            if special["episode"] != rec["episode"]:
                log(f"    {item_label('episode', rec)} is Simkl's special {special['episode']} "
                    f"{special.get('title')!r} (by {how})", 2)
        elif how:
            unmatched[key] = how
    return unmatched


def match_plex_special(rec, special, simkl_count, plex_specials):
    """Pick the Plex season 0 episode [(episode, title, air date)] an AniDB special was.

    By title (the play's or the Simkl special's), then by the Simkl special's air date (a day
    either way), then by number when Plex and Simkl list as many specials. Returns
    (episode, how) or (None, reason).
    """
    titles = {title_key(t) for t in (rec.get("episode_title"), (special or {}).get("title"))
              if t and not GENERIC_TITLE.match(t)}
    by_title = [n for n, title, _ in plex_specials if title_key(title) in titles]
    if len(by_title) == 1:
        return by_title[0], "episode title"
    try:
        aired = datetime.fromisoformat((special or {}).get("date") or "").date()
        by_date = [n for n, _, date in plex_specials
                   if date and abs((datetime.fromisoformat(date).date() - aired).days) <= 1]
    except ValueError:
        by_date = []
    if len(by_date) == 1:
        return by_date[0], "air date"
    if special and len(plex_specials) == simkl_count \
            and special["episode"] in {n for n, _, _ in plex_specials}:
        return special["episode"], "same numbering on Plex and Simkl"
    return None, "no Plex special matches its title or air date"


def plan_plex_specials(episodes, target, anime_ids, catalog):
    """Find the Plex episode of each AniDB special for the Plex target.

    The show's Plex season 0 is matched against the special (match_plex_special), using
    Simkl's catalog for its title and air date when available. Anime-Lists' TVDB special
    numbers are not used: Plex numbers season 0 its own way (Lancer Saga R2's picture drama 6
    is TVDB S00E31, which is Plex's "Final Gambit" special edition). Sets rec["plex_special"]
    = (TVDB id, 0, episode), or rec["special_reason"]. Returns the number matched.
    """
    matched = 0
    for rec in episodes.values():
        if not is_anidb_special(rec):
            continue
        anidb = str(rec["ids"]["anidb"])
        tvdb_id = (anime_ids.get(anidb) or {}).get("tvdb_id")
        if not tvdb_id:
            rec["special_reason"] = "no TVDB show for the AniDB id"
            continue
        plex_specials = target.season_episodes(tvdb_id, 0)
        if not plex_specials:
            rec["special_reason"] = "the show has no specials season on Plex"
            continue
        special, simkl_count = None, 0
        if catalog:
            special, _ = simkl_special(rec, catalog)
            entry = catalog.entry(anidb=int(anidb))
            simkl_count = sum(e.get("type") == "special" for e in catalog.episodes(
                *entry, specials=True)) if entry and entry[0] == "anime" else 0
        number, how = match_plex_special(rec, special, simkl_count, plex_specials)
        if number is None:
            rec["special_reason"] = how
            continue
        rec["plex_special"] = (tvdb_id, 0, number)
        matched += 1
        log(f"    {item_label('episode', rec)} is Plex S00E{number:02} (by {how})", 2)
    return matched


def misidentified_shows(episodes, anime_ids, catalog):
    """Find AniDB-numbered shows that HAMA probably matched to the wrong AniDB entry.

    A show is misidentified when it has episode numbers past the end of its Simkl entry that
    absolute numbering (absolute_target) does not explain, e.g. Witch Seven TV episodes 1-12
    stored under the 2-episode OVA's AniDB id: episodes 1-2 would be sent to the wrong entry.
    Shows Simkl doesn't know are left for Simkl to report. Returns {show key: reason}.
    """
    shows = {}
    for rec in episodes.values():
        if rec["numbering"] == NUMBERING_ANIDB and "anidb" in rec["ids"] and rec["season"] == 1:
            shows.setdefault(rec["show"], []).append(rec)
    found = {}
    for show, recs in shows.items():
        own = catalog.anidb_episodes(recs[0]["ids"]["anidb"])
        if not own:
            continue
        end = last_episode(own)
        unexplained = sorted(r["episode"] for r in recs if r["episode"] > end
                             and not absolute_target(r, anime_ids, catalog))
        if unexplained:
            numbers = ", ".join(map(str, unexplained))
            found[show] = (f"episode{'s' * (len(unexplained) > 1)} {numbers} "
                           f"{'are' if len(unexplained) > 1 else 'is'} past the end of its "
                           f"{end}-episode Simkl entry")
    return found


def take_mismatched_plays(db_path, account_id, watched):
    """Take the mismatched HAMA plays (see load_mismatched_plays) of the guids in watched out of
    it: a guid keeps the earliest watch of its agreeing plays, or is removed. Returns the plays.
    """
    mismatched, consistent = load_mismatched_plays(db_path, account_id)
    plays = []
    for guid in [g for g in watched if g in mismatched]:
        plays += mismatched[guid]
        if guid in consistent:
            watched[guid] = consistent[guid]
        else:
            del watched[guid]
    return plays


def play_label(play):
    return (f"{play['show_title']} anidb-{play['anidb']} episode {play['episode']} "
            f"(show anidb-{play['show_anidb']}, index {play['index']}, "
            f"title {play['title']!r}, watched {iso(play['watched_at'])})")


def check_mismatched_plays(plays, catalog):
    """Decide what each mismatched play was: the candidates are the play guid's entry and
    episode, and the show guid's entry with the play's index; verify_episode decides.
    Returns [(play, (AniDB id, episode) or None, reason)].
    """
    results = []
    for play in plays:
        candidates = list(dict.fromkeys([(play["anidb"], play["episode"]),
                                         (play["show_anidb"], play["index"])]))
        found, reason = verify_episode(candidates, play["title"], play["watched_at"], catalog)
        results.append((play, found, reason))
    return results


def resolve_mismatched_plays(plays, episodes, catalog):
    """Add the episodes that mismatched plays turn out to be (check_mismatched_plays).
    Returns the number of plays added."""
    added = 0
    for play, found, reason in check_mismatched_plays(plays, catalog):
        if not found:
            log(f"    skip {play_label(play)}: {reason}", 2)
            continue
        anidb, number = found
        log(f"    {play_label(play)}: anidb-{anidb} episode {number} by {reason}", 2)
        show_key = f"anidb:{anidb}"
        same_show = next((e for e in episodes.values() if e["show"] == show_key), None)
        rec = episodes.setdefault((show_key, 1, number), {
            "show": show_key, "ids": same_show["ids"] if same_show else {"anidb": anidb},
            "title": same_show and same_show["title"], "year": same_show and same_show["year"],
            "season": 1, "episode": number, "numbering": NUMBERING_ANIDB,
            "watched_at": play["watched_at"], "label": play["show_title"],
            "episode_title": play["title"]})
        rec["watched_at"] = min(rec["watched_at"], play["watched_at"])
        added += 1
    return added


def split_part(title):
    """("Lancer Saga: Ren the Exiled", 4, "Ashes of Oath") from
    "Lancer Saga: Ren the Exiled 4: Ashes of Oath"; None without a trailing part number."""
    m = re.match(r"^(?P<base>.*?\D)[\s:,-]+(?:part\s+)?(?P<part>\d{1,2})"
                 r"(?:\s*[:-]\s*(?P<subtitle>.+))?$", title or "", re.IGNORECASE)
    if not m or not m["base"].strip(" :-"):
        return None
    return m["base"].strip(" :-"), int(m["part"]), m["subtitle"]


def multipart_variant(rec, catalog, tmdb):
    """Send a movie as episode N of a Simkl anime entry that holds a numbered film series."""
    titles = []
    if tmdb and "imdb" in rec["ids"]:
        found = (tmdb.find_imdb(rec["ids"]["imdb"]).get("movie_results") or [{}])[0]
        titles.append(found.get("title"))
    titles += [rec["title"], rec.get("label")]
    for title in titles:
        parsed = split_part(title)
        entry = parsed and catalog.multipart_entry(*parsed)
        if entry:
            return {"name": "part of a multi-part Simkl entry",
                    "item": {"ids": {"simkl": entry[0]}, "title": entry[1]},
                    "episode": {"season": 1, "number": parsed[1]}}
    return None


def build_units(movies, episodes, anime_ids=None, catalog=None, tmdb=None, resolver=None):
    """Return (kind, record, variants) for every movie and episode.

    Variants with "make" are computed only if the earlier ones fail, as they cost requests.
    """
    units = []
    for m in movies.values():
        variants = movie_variants(m)
        if tmdb and "imdb" in m["ids"] and "tmdb" not in m["ids"]:
            variants.append({"name": "TMDB title", "make": lambda m=m: tmdb_title_variant(m, tmdb)})
        if catalog:
            variants.append({"name": "part of a multi-part Simkl entry",
                             "make": lambda m=m: multipart_variant(m, catalog, tmdb)})
        units.append(("movie", m, variants))
    for e in sorted(episodes.values(), key=lambda e: (e["show"], e["season"], e["episode"])):
        variants = episode_variants(e, anime_ids or {})
        if catalog and e["numbering"] == NUMBERING_TVDB and "tvdb" in e["ids"]:
            variants.append({"name": "TVDB season from Simkl's catalog",
                             "make": lambda e=e: season_overflow_variant(e, catalog)})
        if catalog and anime_ids and e["numbering"] == NUMBERING_ANIDB and "anidb" in e["ids"] \
                and e["season"] == 1:
            variants.append({"name": "absolute episode number",
                             "make": lambda e=e: absolute_number_variant(e, anime_ids, catalog)})
        if resolver and resolver.token and e["numbering"] == NUMBERING_TVDB:
            variants.append({"name": "show ids from Plex by title",
                             "make": lambda e=e: plex_title_variant(e, resolver)})
        units.append(("episode", e, variants))
    return units


def ready_units(units, attempt):
    """Compute deferred variants at position attempt; return (ready units, units out of variants)."""
    ready, exhausted = [], []
    for unit in units:
        variants = unit[2]
        while attempt < len(variants) and "make" in variants[attempt]:
            made = variants[attempt]["make"]()
            if made:
                variants[attempt] = made
            else:
                del variants[attempt]
        (ready if attempt < len(variants) else exhausted).append(unit)
    return ready, exhausted


def build_payloads(units, attempt=0):
    """Yield (payload, batch) per batch of up to BATCH_SIZE units, using each unit's variant
    number `attempt`; batch holds (kind, record, variant)."""
    for start in range(0, len(units), BATCH_SIZE):
        payload = {"movies": [], "shows": []}
        shows = {}
        batch = [(kind, rec, variants[attempt])
                 for kind, rec, variants in units[start:start + BATCH_SIZE]]
        for kind, rec, variant in batch:
            watched_at = iso(rec["watched_at"])
            if "episode" not in variant:
                payload["movies"].append({**variant["item"], "watched_at": watched_at})
                continue
            item, episode = variant["item"], variant["episode"]
            key = json.dumps(item, sort_keys=True)
            show = shows.get(key)
            if show is None:
                show = shows[key] = {**item, "ids": dict(item["ids"])}
                payload["shows"].append(show)
            if "ids" in episode:
                show.setdefault("episodes", []).append({**episode, "watched_at": watched_at})
                continue
            seasons = show.setdefault("seasons", [])
            if not seasons or seasons[-1]["number"] != episode["season"]:
                seasons.append({"number": episode["season"], "episodes": []})
            seasons[-1]["episodes"].append({"number": episode["number"], "watched_at": watched_at})
        yield payload, batch


def send_to_plex(target, movies, episodes, state, dry_run, batch_size=20):
    """Mark each movie and episode watched on the Plex account with its watch date.

    Items are marked in batches of batch_size (PlexTarget.mark_batch): two GraphQL requests
    per batch instead of two or more per item, as Plex limits GraphQL request rates.
    """
    units = [("movie", m) for m in movies.values()]
    units += [("episode", e) for e in sorted(
        episodes.values(), key=lambda e: (e["show"], e["season"], e["episode"]))]
    log(f"[6/6] {'Dry run: looking up' if dry_run else 'Marking'} {len(units)} items "
        f"{'on' if dry_run else 'watched on'} the Plex account")
    if target.pending:
        if dry_run:
            log(f"  {len(target.pending)} items from an earlier run are marked but their date is "
                f"not set yet; a real run repairs them first")
        else:
            repaired = target.repair_pending()
            log("  items from an earlier run whose date was not set: " + ", ".join(
                f"{n} {outcome}" for outcome, n in sorted(repaired.items())))
    counts = {}
    for start in range(0, len(units), max(1, batch_size)):
        batch = units[start:start + max(1, batch_size)]
        found = [(i, *target.item_id(kind, rec)) for i, (kind, rec) in enumerate(batch)]
        results = {i: ("not found on Plex", reason) for i, plex_id, reason in found if not plex_id}
        to_mark = [(i, plex_id) for i, plex_id, _ in found if plex_id]
        if dry_run:
            for i, plex_id in to_mark:
                rec = batch[i][1]
                results[i] = ("already watched" if target.is_watched(plex_id) else "would be marked",
                              f"at {iso(rec['watched_at'])}, plex id {plex_id}")
                time.sleep(PLEX_REQUEST_INTERVAL)
        elif to_mark:
            outcomes = target.mark_batch([(plex_id, batch[i][1]["watched_at"])
                                          for i, plex_id in to_mark])
            for (i, _), outcome in zip(to_mark, outcomes):
                results[i] = (outcome, iso(batch[i][1]["watched_at"]))
        for i, (kind, rec) in enumerate(batch):
            outcome, detail = results[i]
            counts[outcome] = counts.get(outcome, 0) + 1
            log(f"  {start + i + 1}/{len(units)} {item_label(kind, rec)}: {outcome} ({detail})",
                1 if dry_run or outcome == "not found on Plex" else 2)
        # Recorded per batch; an interruption inside one leaves its marked items in the
        # pending journal, and the rest are checked again cheaply.
        if state and not dry_run:
            state.record(batch, {i for i, (outcome, _) in results.items()
                                 if outcome == "not found on Plex"})
    summary = ", ".join(f"{n} {outcome}" for outcome, n in sorted(counts.items()))
    log(f"{'Dry run' if dry_run else 'Done'}: {summary or 'nothing to send'}", 0)


def run(args, env, error):
    """The export command: args are resolved settings, env the .env values, error reports
    usage errors."""
    if args.forget:
        forget_records(args.sent_cache, args.not_found, args.forget, args.dry_run)
        return
    refresh_token = os.environ.get("SIMKL_REFRESH_TOKEN") or env.get("SIMKL_REFRESH_TOKEN")
    to_plex = args.target == "plex"
    if not to_plex and not args.simkl_client_id and (not args.dry_run or args.simkl_login):
        error("--simkl-client-id is required unless --dry-run is used")
    simkl = None
    logged_in = False
    if args.simkl_client_id and not to_plex:
        simkl = Simkl(args.simkl_client_id, args.simkl_token, refresh_token, args.env_file)
        logged_in = args.simkl_login or (not args.simkl_token and not args.dry_run)
        if logged_in:
            simkl.login()
        if not simkl.token:
            simkl = None
    account = None if logged_in or to_plex else \
        os.environ.get("SIMKL_ACCOUNT_ID") or env.get("SIMKL_ACCOUNT_ID")
    if simkl and not account:
        # Simkl asks apps not to fetch settings on every run, so the id is kept in the env file.
        account = simkl.account_id()
        if account:
            update_env_file(args.env_file, {"SIMKL_ACCOUNT_ID": account})
            log(f"Saved Simkl account id {account} to {args.env_file}")
    # The catalog's lookups need only the client id, so the Plex target can use them too.
    catalog = SimklCatalog(simkl or Simkl(args.simkl_client_id, None, None, args.env_file),
                           args.simkl_cache, args.simkl_cache_days) \
        if simkl or (to_plex and args.simkl_client_id) else None
    log(f"[1/6] Reading watch history for Plex account {args.plex_account_id} from {args.db}")
    watched = load_watched(args.db, args.plex_account_id, args.only, args.skip_synced)
    titles = load_titles(args.db)
    years = load_years(args.db)
    episode_titles = {g: d.get("item_title") for g, d in load_details(args.db).items()}

    resolver = PlexResolver(args.plex_token, args.cache, load_episode_fallbacks(args.db)) \
        if args.plex_token else None
    target = None
    if to_plex:
        target = PlexTarget(args.plex_token, resolver.cache, load_anime_ids(args.anime_ids),
                            args.pending)
        account = target.account()
    try:
        log("[2/6] Filtering items")
        if args.guid_regex or args.title_regex or args.title or args.start or args.count is not None:
            tmdb = Tmdb(args.tmdb_api_key, args.tmdb_cache, args.tmdb_cache_days) \
                if args.title else None
            watched = select(watched, args, titles, resolver, tmdb)
        else:
            log("  no filters given, keeping all items")

        log("[3/6] Mapping Plex guids to Simkl ids")
        if not args.plex_token:
            log("  no Plex token given: plex:// items will be skipped")
        mismatched_plays = take_mismatched_plays(args.db, args.plex_account_id, watched)
        skipped, counts = {}, {}
        movies, episodes = collect(watched, resolver, skipped, titles, years, episode_titles,
                                   load_local_episodes(args.db), counts)
    finally:
        if resolver:
            resolver.save()
    if mismatched_plays:
        log(f"  {len(mismatched_plays)} plays whose guid disagrees with their show or episode "
            f"number (HAMA matching errors)")
        if catalog:
            added = resolve_mismatched_plays(mismatched_plays, episodes, catalog)
            log(f"  {added} of them identified with Simkl's catalog, "
                f"{len(mismatched_plays) - added} skipped"
                f"{'' if common.VERBOSITY >= 2 else ' (use -v to see them)'}")
        else:
            log("  they are skipped: checking them needs Simkl's catalog (Simkl token)")
    log(f"  {len(movies)} movies and {len(episodes)} episodes "
        f"from {len({e['show'] for e in episodes.values()})} shows can be exported")
    if counts.get("local"):
        log(f"  {counts['local']} local:// episodes identified through their show's guid and "
            f"the season/episode numbers in the database")
    if resolver and resolver.recovered:
        log(f"  {len(resolver.recovered)} episodes no longer exist on Plex; "
            f"mapped through their show and the season/episode numbers in the database")
    log(f"  {sum(skipped.values())} items skipped"
        f"{'' if common.VERBOSITY >= 2 else ' (use -v to see the reasons)'}")
    for reason, count in sorted(skipped.items(), key=lambda x: -x[1]):
        log(f"  skipped {count:5}  {reason}", 2)

    log(f"[4/6] Reading the {'Simkl library' if simkl or not to_plex else 'Plex account'}")
    if to_plex:
        log("  each item's Plex watch state is checked before it is marked")
    elif simkl:
        library = SimklLibrary()
        library.load(simkl)
    else:
        log("  no Simkl token: not checking what is already on Simkl")

    collected = dict(episodes)
    log(f"[5/6] Removing items already watched on {args.target.capitalize()}, sent before "
        f"or not found before")
    if simkl:
        before = (len(movies), len(episodes))
        for k, m in list(movies.items()):
            if library.has_movie(m["ids"]):
                log(f"    on Simkl: movie {m['title'] or m['ids']}", 3)
                del movies[k]
        for k, e in list(episodes.items()):
            if library.has_episode(e["ids"], e["season"], e["episode"], e["numbering"]):
                log(f"    on Simkl: {e['title'] or e['ids']} "
                    f"S{e['season']:02}E{e['episode']:02}", 3)
                del episodes[k]
        log(f"  already on Simkl: {before[0] - len(movies)} movies, "
            f"{before[1] - len(episodes)} episodes")
    elif not to_plex:
        log("  Simkl library not checked (no Simkl token)")
    state = SyncState(args.sent_cache, args.not_found, account, args.not_found_days) \
        if account else None
    if state:
        sent_before = not_found_before = 0
        for records, kind in ((movies, "movie"), (episodes, "episode")):
            for k, rec in list(records.items()):
                keys = item_keys(kind, rec)
                if not args.resend and state.was_sent(keys):
                    sent_before += 1
                elif not args.retry_not_found and state.was_not_found(keys):
                    not_found_before += 1
                else:
                    continue
                log(f"    {'sent before' if keys & state.sent.keys() else 'not found before'}: "
                    f"{item_label(kind, rec)}", 3)
                del records[k]
        log(f"  sent in earlier runs: {sent_before} items"
            + ("" if args.resend else " (--resend to send them again)"))
        if args.not_found_days:
            log(f"  not found on {args.target.capitalize()} in the last {args.not_found_days:g} days: "
                f"{not_found_before} items"
                + ("" if args.retry_not_found else " (--retry-not-found to retry them)"))
        else:
            log(f"  retrying items {args.target.capitalize()} did not find before "
                f"(--not-found-days 0)")
    else:
        log("  sent/not-found records not used (Simkl account unknown, no Simkl token yet)")
    needs_anime_ids = any(e.get("legacy_tvdb") or (catalog and e["numbering"] == NUMBERING_ANIDB)
                          for e in episodes.values())
    anime_ids = load_anime_ids(args.anime_ids) if needs_anime_ids else {}
    if catalog and not to_plex:
        # Judged on every collected episode of a show, also those already on Simkl.
        left = {e["show"] for e in episodes.values()}
        wrong = misidentified_shows({k: e for k, e in collected.items() if e["show"] in left},
                                    anime_ids, catalog)
        for key, reason in sorted(match_specials(episodes, catalog).items()):
            log(f"  skipping {item_label('episode', episodes[key])}: {reason}", 2)
            del episodes[key]
        for show, reason in sorted(wrong.items()):
            dropped = [k for k, e in episodes.items() if e["show"] == show]
            label = item_label("episode", episodes[dropped[0]]).rpartition(" S")[0]
            log(f"  skipping {len(dropped)} episodes of {label}: {reason} (the files were "
                f"probably matched to the wrong AniDB entry, or Simkl splits it differently)", 0)
            for k in dropped:
                del episodes[k]
    log(f"  left to send: {len(movies)} movies, {len(episodes)} episodes")

    if to_plex:
        specials = [e for e in episodes.values() if is_anidb_special(e)]
        if specials:
            matched = plan_plex_specials(episodes, target, target.anime_ids, catalog)
            log(f"  {matched} of {len(specials)} AniDB specials matched to Plex episodes"
                + ("" if catalog else " (without SIMKL_CLIENT_ID, Simkl's special titles and "
                                     "air dates are not used)"))
        try:
            send_to_plex(target, movies, episodes, state, args.dry_run, args.plex_batch)
        finally:
            resolver.save()
        return

    units = build_units(movies, episodes, anime_ids, catalog,
                        Tmdb(args.tmdb_api_key, args.tmdb_cache, args.tmdb_cache_days)
                        if args.tmdb_api_key else None, resolver)
    if args.dry_run:
        payloads = list(build_payloads(units))
        log(f"[6/6] Dry run: printing {len(payloads)} batches instead of posting")
        for i, (payload, batch) in enumerate(payloads, 1):
            log(f"  batch {i}/{len(payloads)}:")
            for line in batch_summary(batch):
                log(f"    {line}")
            print(json.dumps(payload, indent=2))
        fallbacks = {}
        for _, _, variants in units:
            for variant in variants[1:]:  # deferred ones are counted by name, not computed
                fallbacks[variant["name"]] = fallbacks.get(variant["name"], 0) + 1
        if fallbacks:
            log("  fallbacks for items Simkl does not find: " + ", ".join(
                f"{n} by {name}" for name, n in sorted(fallbacks.items())))
        return
    added = {"movies": 0, "shows": 0, "episodes": 0}
    recovered, not_found, pending, attempt = {}, [], units, 0
    while pending:
        pending, exhausted = ready_units(pending, attempt)
        not_found += exhausted
        if not pending:
            break
        payloads = list(build_payloads(pending, attempt))
        if attempt == 0:
            log(f"[6/6] Posting {len(payloads)} batches of up to {BATCH_SIZE} items to Simkl")
        else:
            names = sorted({variants[attempt]["name"] for _, _, variants in pending})
            log(f"  retry {attempt}: {len(pending)} items not found, sending them again by "
                f"{', '.join(names)}")
        failed_now = []
        for i, (payload, batch) in enumerate(payloads, 1):
            result = simkl.post("/sync/history", payload) or {}
            for k in added:
                added[k] += result.get("added", {}).get(k, 0)
            failed = failed_units(batch, result)
            for j, (kind, rec, variant) in enumerate(batch):
                if j in failed:
                    log(f"    not found by Simkl ({variant['name']}): {item_label(kind, rec)}", 2)
                elif attempt:
                    recovered[variant["name"]] = recovered.get(variant["name"], 0) + 1
                    log(f"    found by {variant['name']}: {item_label(kind, rec)}", 2)
            if state:
                state.record([(k, r) for j, (k, r, _) in enumerate(batch) if j not in failed],
                             set())
            log(f"  {'retry ' + str(attempt) + ' ' if attempt else ''}batch {i}/{len(payloads)}: "
                f"sent {len(batch)} items, added {result.get('added', {}).get('episodes', 0)} "
                f"episodes and {result.get('added', {}).get('movies', 0)} movies, "
                f"{len(failed)} not found", 2)
            failed_now += [pending[(i - 1) * BATCH_SIZE + j] for j in sorted(failed)]
            time.sleep(SIMKL_POST_INTERVAL)
        attempt += 1
        pending = [u for u in failed_now if len(u[2]) > attempt]
        not_found += [u for u in failed_now if len(u[2]) <= attempt]
    if state and not_found:
        state.record([(kind, rec) for kind, rec, _ in not_found], set(range(len(not_found))))
    if resolver:
        resolver.save()  # title matches made by fallbacks

    log(f"Done: Simkl added {added['movies']} movies and {added['episodes']} episodes "
        f"across {added['shows']} shows", 0)
    if recovered:
        log(f"{sum(recovered.values())} items found only by a fallback: " + ", ".join(
            f"{n} by {name}" for name, n in sorted(recovered.items())), 0)
    if not_found:
        log(f"{len(not_found)} items not matched by Simkl"
            + (f", recorded in {args.not_found}" if state else ""), 0)
    if common.rate_limit_text():
        log(common.rate_limit_text())
