"""Selecting which watched items to export."""

import re

from plexhistory import common
from plexhistory.common import Progress, log
from plexhistory.plexdb import parse_guid
from plexhistory.tmdb import anidb_ids_for, load_anime_ids


def describe(guid, resolver):
    """Return (kind, ids, title) of a guid, kind being "tv", "movie" or None."""
    parsed = parse_guid(guid)
    if parsed[0] == "plex" and resolver:
        parsed = resolver.resolve(parsed[1], parsed[2])
    if parsed[0] == "episode":
        return "tv", parsed[1], parsed[5] if len(parsed) > 5 else None
    if parsed[0] == "movie":
        return "movie", parsed[1], parsed[2] if len(parsed) > 2 else None
    return None, {}, None


def select(watched, filters, titles, resolver, tmdb):
    """Apply the guid regex, title filters and start/count window, in that order."""
    items = watched
    if filters.guid_regex:
        rx = re.compile(filters.guid_regex)
        items = {g: t for g, t in items.items() if rx.search(g)}
        log(f"  --guid-regex {filters.guid_regex!r}: {len(items)} items match")

    title_rx = re.compile(filters.title_regex, re.IGNORECASE) if filters.title_regex else None
    targets = set()
    if filters.title:
        matches = tmdb.find(filters.title, filters.year)
        log(f"  TMDB requests: {tmdb.stats()}", 2)
        if not matches:
            log(f"  TMDB has no match for {filters.title!r}; matching by title only")
        for kind, name, year, ids in matches:
            log(f"  TMDB match: {kind} {name!r} ({year or '?'}) "
                + ", ".join(f"{k}={v}" for k, v in ids.items()))
            targets.update((kind, k, v) for k, v in ids.items())
        anidb_ids = anidb_ids_for(targets, load_anime_ids(filters.anime_ids))
        log(f"  anime mapping: {len(anidb_ids)} AniDB entries match"
            + (f" ({', '.join(sorted(anidb_ids, key=int))})" if anidb_ids and common.VERBOSITY >= 2
               else ""))
        targets.update(("tv", "anidb", a) for a in anidb_ids)
    if title_rx or filters.title:
        wanted = filters.title.casefold() if filters.title else None

        def db_title_hit(guid):
            return wanted is not None and titles.get(guid, "").casefold() == wanted

        # Ids (and titles of plex:// items) are only looked up when the DB title can't decide.
        def needs_lookup(guid):
            return guid not in titles or (wanted is not None and not db_title_hit(guid))

        lookups = sum(needs_lookup(g) and parse_guid(g)[0] == "plex" for g in items) \
            if resolver else 0
        if lookups:
            log(f"  {lookups} plex:// items need a Plex lookup for their title or ids")
            progress = Progress(lookups, "looked up plex:// items", resolver.stats)
        kept = {}
        for guid, epoch in items.items():
            title = titles.get(guid)
            title_hit = db_title_hit(guid)
            id_hit = False
            if needs_lookup(guid):
                kind, ids, resolved_title = describe(guid, resolver)
                if resolver and parse_guid(guid)[0] == "plex":
                    progress.step()
                title = title or resolved_title
                title_hit = title_hit or (wanted is not None and title is not None
                                          and title.casefold() == wanted)
                id_hit = any((kind, k, str(v)) in targets for k, v in ids.items())
            if title_rx and not (title and title_rx.search(title)):
                continue
            if wanted is not None and not (title_hit or id_hit):
                continue
            log(f"    keep {guid}: {title}", 3)
            kept[guid] = epoch
        items = kept
        log(f"  title filters: {len(items)} items match")

    if filters.start or filters.count is not None:
        ordered = sorted(items.items(), key=lambda x: (x[1] or 0, x[0]))
        end = None if filters.count is None else filters.start + filters.count
        items = dict(ordered[filters.start:end])
        log(f"  --start {filters.start} --count {filters.count}: {len(items)} items "
            f"(ordered by first watch date)")
    return items
