"""The list command: show the watched items in the database, enriched from the caches."""

import json
import sqlite3

from plexhistory.common import iso, log
from plexhistory.filters import select
from plexhistory.plex import PlexResolver
from plexhistory.export import map_local
from plexhistory.plexdb import hama_mismatch, load_details, load_episode_fallbacks, \
    load_local_episodes, load_plays, load_titles, load_watched, parse_guid
from plexhistory.tmdb import Tmdb

TYPES = {1: "movie", 2: "show", 3: "season", 4: "episode", 8: "artist", 9: "album",
         10: "track", 12: "clip"}

ITEM_COLUMNS = ("watched", "state", "type", "title", "episode", "episode_title", "guid", "ids",
                "note")
TABLE_CELL_WIDTH = 60
PLAY_COLUMNS = ("watched", "state", "type", "title", "episode", "episode_title", "guid",
                "show_guid", "note")
SHOW_COLUMNS = ("first_watched", "last_watched", "type", "title", "items", "orphaned",
                "episodes", "ids", "note")


def present_guids(db_path):
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    guids = {g for (g,) in db.execute("select guid from metadata_items where guid is not null")}
    db.close()
    return guids


def guid_level(guid):
    """"show" or "season" for agent guids like thetvdb://85625 or thetvdb://85625/1."""
    path = guid.split("?")[0].partition("://")[2]
    return {1: "show", 2: "season"}.get(len(path.split("/")), "")


def describe(guid, epoch, details, present, resolver, local_episodes=None):
    """Return a row dict for one watched guid."""
    detail = details.get(guid, {})
    parsed = parse_guid(guid)
    if parsed == ("skip", "local"):
        parsed = (map_local(guid, local_episodes or {}, resolver) or (parsed,))[0]
    kind = TYPES.get(detail.get("type"))
    title, item_title = detail.get("title"), detail.get("item_title")
    season, episode = detail.get("season"), detail.get("episode")
    ids, note = {}, ""
    if parsed[0] == "plex":
        kind, plex_id = parsed[1], parsed[2]
        parsed = resolver.resolve(kind, plex_id) if resolver else ("skip", "no Plex cache")
        # Read after resolve, which fills the cache when --lookup is on.
        cached = resolver.cache.get(plex_id) if resolver else None
        item_title = item_title or (cached or {}).get("title")
    if parsed[0] == "episode":
        kind = "episode"
        ids, season, episode = parsed[1], parsed[2], parsed[3]
        title = title or (parsed[5] if len(parsed) > 5 else None)
    elif parsed[0] == "movie":
        kind = "movie"
        ids = parsed[1]
        title = title or (parsed[2] if len(parsed) > 2 else None)
    else:
        note = parsed[1]
        if not kind and note == "show/season level":
            kind = guid_level(guid)
    has_episode = kind == "episode" and season is not None and episode is not None
    return {
        "watched": iso(epoch)[:10] if epoch else "",
        "epoch": epoch or 0,
        "state": "present" if guid in present else "orphaned",
        "type": kind or "",
        "title": title or "",
        "episode": f"S{season:02}E{episode:02}" if has_episode else "",
        "episode_title": item_title if kind == "episode" and item_title else "",
        "guid": guid,
        "ids": ", ".join(f"{k}={v}" for k, v in ids.items()),
        "note": note,
    }


def play_rows(plays, watched, present):
    """One row per play event of the guids in watched, with the play's own show guid, numbers
    and titles (as recorded at play time)."""
    rows = []
    for play in plays:
        if play["guid"] not in watched:
            continue
        season, index = play["season"], play["index"]
        mismatch = hama_mismatch(play["guid"], play["show_guid"], index)
        rows.append({
            "watched": iso(play["watched_at"]) if play["watched_at"] else "",
            "state": "present" if play["guid"] in present else "orphaned",
            "type": TYPES.get(play["type"], ""),
            "title": play["title"] or "",
            "episode": f"S{season:02}E{index:02}" if isinstance(season, int)
            and isinstance(index, int) and play["type"] == 4 else "",
            "episode_title": play["item_title"] or "",
            "guid": play["guid"],
            "show_guid": play["show_guid"] or "",
            "note": "guid disagrees with the show guid or index" if mismatch else ""})
    return rows


def group_by_show(rows):
    """Collapse episode rows into one row per show; other rows stay as they are."""
    shows, out = {}, []
    for row in rows:
        if row["type"] != "episode":
            out.append({"first_watched": row["watched"], "last_watched": row["watched"],
                        "type": row["type"], "title": row["title"], "items": 1,
                        "orphaned": int(row["state"] == "orphaned"),
                        "episodes": row["episode"], "ids": row["ids"], "note": row["note"],
                        "epoch": row["epoch"]})
            continue
        key = row["title"] or row["ids"] or row["guid"]
        show = shows.get(key)
        if show is None:
            show = shows[key] = {"first_watched": row["watched"], "last_watched": row["watched"],
                                 "type": "show", "title": row["title"], "items": 0,
                                 "orphaned": 0, "episodes": [], "ids": row["ids"],
                                 "note": set(), "epoch": row["epoch"]}
            out.append(show)
        show["items"] += 1
        show["orphaned"] += row["state"] == "orphaned"
        show["first_watched"] = min(show["first_watched"], row["watched"])
        show["last_watched"] = max(show["last_watched"], row["watched"])
        show["ids"] = show["ids"] or row["ids"]
        if row["episode"]:
            show["episodes"].append(row["episode"])
        if row["note"]:
            show["note"].add(row["note"])
    for show in shows.values():
        show["episodes"] = summarize_episodes(show["episodes"])
        show["note"] = ", ".join(sorted(show["note"]))
    return out


def summarize_episodes(codes):
    """["S01E01", "S01E02", "S01E04"] -> "S01E01-E02, S01E04"."""
    by_season = {}
    for code in sorted(set(codes)):
        season, _, episode = code[1:].partition("E")
        by_season.setdefault(int(season), []).append(int(episode))
    parts = []
    for season, numbers in sorted(by_season.items()):
        start = prev = numbers[0]
        for n in numbers[1:] + [None]:
            if n is not None and n == prev + 1:
                prev = n
                continue
            parts.append(f"S{season:02}E{start:02}" + (f"-E{prev:02}" if prev != start else ""))
            start = prev = n
    return ", ".join(parts)


def print_rows(rows, columns, fmt):
    if fmt == "json":
        print(json.dumps([{c: r[c] for c in columns} for r in rows], indent=1))
        return
    if fmt == "tsv":
        print("\t".join(columns))
        for r in rows:
            print("\t".join(str(r[c]) for c in columns))
        return
    # Long titles would stretch every row; tsv and json keep the full values.
    cells = [{c: str(r[c]) if c == "guid" or len(str(r[c])) <= TABLE_CELL_WIDTH
              else str(r[c])[:TABLE_CELL_WIDTH - 3] + "..." for c in columns} for r in rows]
    widths = {c: max([len(c)] + [len(r[c]) for r in cells]) for c in columns}
    print("  ".join(c.ljust(widths[c]) for c in columns).rstrip())
    for r in cells:
        print("  ".join(r[c].ljust(widths[c]) for c in columns).rstrip())


def run(args):
    log(f"Reading watch history for Plex account {args.plex_account_id} from {args.db}", 2)
    watched = load_watched(args.db, args.plex_account_id, args.only, args.skip_synced)
    titles = load_titles(args.db)
    details = load_details(args.db)
    present = present_guids(args.db)
    # Without --lookup the resolver only reads the cache, so listing never touches the network.
    resolver = PlexResolver(args.plex_token if args.lookup else None, args.cache,
                            load_episode_fallbacks(args.db))
    try:
        if args.guid_regex or args.title_regex or args.title or args.start or args.count is not None:
            tmdb = Tmdb(args.tmdb_api_key, args.tmdb_cache, args.tmdb_cache_days) \
                if args.title else None
            watched = select(watched, args, titles, resolver, tmdb)
        local_episodes = load_local_episodes(args.db)
        rows = sorted((describe(g, t, details, present, resolver, local_episodes)
                       for g, t in watched.items()),
                      key=lambda r: (r["epoch"], r["guid"])) \
            if not args.plays or args.unmappable else []
    finally:
        if args.lookup:
            resolver.save()
    if args.unmappable:
        rows = [r for r in rows if r["note"]]
        reasons = {r["guid"]: r["note"] for r in rows}
        watched = {g: t for g, t in watched.items() if g in reasons}
    if args.type:
        rows = [r for r in rows if (r["type"] or "unknown") in args.type]
    if args.plays:
        rows = play_rows(load_plays(args.db, args.plex_account_id), watched, present)
        if args.unmappable:
            for row in rows:
                row["note"] = row["note"] or reasons[row["guid"]]
        if args.type:
            rows = [r for r in rows if (r["type"] or "unknown") in args.type]
        print_rows(rows, PLAY_COLUMNS, args.format)
        log(f"{len(rows)} plays", 1)
        return
    if args.by == "show":
        rows = sorted(group_by_show(rows), key=lambda r: (r["epoch"], r["title"]))
        print_rows(rows, SHOW_COLUMNS, args.format)
    else:
        print_rows(rows, ITEM_COLUMNS, args.format)
    log(f"{len(rows)} {'shows and movies' if args.by == 'show' else 'items'}", 1)
