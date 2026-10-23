"""Reading watch history from a Plex Media Server library database."""

import re
import sqlite3
import time

from plexhistory.common import log

# Simkl numbers anime episodes AniDB-style by default; Plex sources use TVDB-style numbering.
NUMBERING_TVDB = "tvdb"
NUMBERING_ANIDB = "anidb"


def load_watched(db_path, account_id, only=None, skip_synced=False):
    """Return {guid: earliest watch epoch} for everything the account watched.

    only="orphaned" keeps items no longer in the library, only="present" the rest.
    skip_synced leaves out watch-state rows without a play on this server and without a
    created_at: plex.tv's "Sync my watch state" copies watches from other servers that way
    (e.g. watches of a show that only ever existed on another server).
    """
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    watched = {}
    for guid, viewed_at in db.execute(
            "select guid, min(viewed_at) from metadata_item_views "
            "where account_id = ? and guid is not null group by guid", (account_id,)):
        watched[guid] = viewed_at
    log(f"  {len(watched)} items with play events (metadata_item_views)", 2)
    settings_count = synced = 0
    for guid, last_viewed_at, created_at in db.execute(
            "select guid, last_viewed_at, created_at from metadata_item_settings "
            "where account_id = ? and view_count > 0 and guid is not null", (account_id,)):
        settings_count += 1
        if skip_synced and guid not in watched and not created_at:
            synced += 1
            continue
        if guid not in watched or watched[guid] is None:
            watched[guid] = last_viewed_at
    log(f"  {settings_count} items marked watched (metadata_item_settings)", 2)
    if skip_synced:
        log(f"  {synced} watch-state rows synced from other servers left out (--skip-synced)")
    log(f"  {len(watched)} distinct watched items")
    present = {g for (g,) in db.execute("select guid from metadata_items where guid is not null")}
    orphaned = sum(g not in present for g in watched)
    log(f"  {orphaned} orphaned (no longer in the library), {len(watched) - orphaned} present")
    if only:
        watched = {g: t for g, t in watched.items() if (g in present) == (only == "present")}
        log(f"  keeping {len(watched)} {only} items (--only {only})")
    db.close()
    return watched


HAMA_ANIDB_EPISODE = re.compile(r"hama://(?:anidb-)?(\d+)/[1-9]\d*/(\d+)(?:\?|$)")
HAMA_ANIDB_SHOW = re.compile(r"hama://(?:anidb-)?(\d+)(?:\?|$)")


def hama_mismatch(guid, show_guid, index):
    """Whether a HAMA AniDB episode guid disagrees with its play's show guid or index; None
    when that can't be told (not a HAMA AniDB episode, no show guid or index)."""
    episode = HAMA_ANIDB_EPISODE.search(guid or "")
    show = HAMA_ANIDB_SHOW.search(show_guid or "")
    if not episode or not show or not isinstance(index, int):
        return None
    return (int(episode[1]), int(episode[2])) != (int(show[1]), index)


def load_plays(db_path, account_id):
    """Return every play event of the account, oldest first, as dicts with guid, show_guid,
    type, title (show or movie), season, index, item_title and watched_at."""
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    plays = [{"guid": guid, "show_guid": show_guid, "type": mtype,
              "title": show_title if mtype == 4 else title, "season": season, "index": index,
              "item_title": title if mtype == 4 else None, "watched_at": viewed_at}
             for guid, show_guid, mtype, show_title, title, season, index, viewed_at
             in db.execute("select guid, grandparent_guid, metadata_type, grandparent_title, "
                           "title, parent_index, \"index\", viewed_at from metadata_item_views "
                           "where account_id = ? and guid is not null order by viewed_at, id",
                           (account_id,))]
    db.close()
    return plays


def load_mismatched_plays(db_path, account_id):
    """Find HAMA AniDB episode plays whose guid disagrees with the play's show guid or index.

    HAMA sometimes stored a file under another AniDB entry's guid, e.g. HSA S3E13 as
    anidb-43154/1/2 (S2E2) with the right show guid and index. Returns (plays, consistent):
    plays is {guid: [play]}, consistent {guid: earliest watch of its agreeing plays}.
    """
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    plays, consistent = {}, {}
    for guid, show_guid, show_title, index, title, viewed_at in db.execute(
            "select guid, grandparent_guid, grandparent_title, \"index\", title, viewed_at "
            "from metadata_item_views where account_id = ? and metadata_type = 4 "
            "and guid like '%hama://%'", (account_id,)):
        episode = HAMA_ANIDB_EPISODE.search(guid or "")
        show = HAMA_ANIDB_SHOW.search(show_guid or "")
        if not episode or not show or not isinstance(index, int) or viewed_at is None:
            continue
        anidb, number = int(episode[1]), int(episode[2])
        if (anidb, number) == (int(show[1]), index):
            consistent[guid] = min(consistent.get(guid, viewed_at), viewed_at)
            continue
        plays.setdefault(guid, []).append({
            "anidb": anidb, "episode": number, "show_anidb": int(show[1]), "index": index,
            "show_title": show_title, "title": title, "watched_at": viewed_at})
    db.close()
    return plays, {g: t for g, t in consistent.items() if g in plays}


def load_episode_fallbacks(db_path):
    """Return {plex episode id: (show id, season, episode)} recorded in the database."""
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    fallbacks = {}
    rows = list(db.execute(
        "select guid, grandparent_guid, parent_index, \"index\" from metadata_item_views "
        "where guid like 'plex://episode/%' and grandparent_guid like 'plex://show/%'"))
    rows += db.execute(
        "select e.guid, sh.guid, se.\"index\", e.\"index\" from metadata_items e "
        "join metadata_items se on se.id = e.parent_id "
        "join metadata_items sh on sh.id = se.parent_id "
        "where e.guid like 'plex://episode/%' and sh.guid like 'plex://show/%'")
    for guid, show_guid, season, episode in rows:
        if isinstance(season, int) and isinstance(episode, int) and season >= 0 and episode >= 0:
            fallbacks.setdefault(guid.rpartition("/")[2],
                                 (show_guid.rpartition("/")[2], season, episode))
    db.close()
    return fallbacks


def load_local_episodes(db_path):
    """Return {local:// episode guid: (show guid, season, episode)} from play events and the
    library. local:// items carry no ids, but their show often has an agent or plex:// guid."""
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = list(db.execute(
        "select guid, grandparent_guid, parent_index, \"index\" from metadata_item_views "
        "where metadata_type = 4 and guid like 'local://%' and grandparent_guid is not null"))
    rows += db.execute(
        "select e.guid, sh.guid, se.\"index\", e.\"index\" from metadata_items e "
        "join metadata_items se on se.id = e.parent_id "
        "join metadata_items sh on sh.id = se.parent_id "
        "where e.metadata_type = 4 and e.guid like 'local://%' and sh.guid is not null")
    local = {}
    for guid, show_guid, season, episode in rows:
        if isinstance(season, int) and isinstance(episode, int) and season >= 0 and episode >= 0 \
                and not show_guid.startswith("local://"):
            local.setdefault(guid, (show_guid, season, episode))
    db.close()
    return local


def episode_guid_of_show(show_guid, season, episode):
    """The agent episode guid for an agent show guid, e.g. thetvdb://382752?lang=en, 1, 2 ->
    thetvdb://382752/1/2?lang=en; None for other guids."""
    base, _, query = show_guid.partition("?")
    if not re.match(r"com\.plexapp\.agents\.(thetvdb|themoviedb|hama)://[^/]+$", base):
        return None
    return f"{base}/{season}/{episode}" + (f"?{query}" if query else "")


def load_years(db_path):
    """Return {guid: release year} of movies, from play events and the library."""
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    years = {}
    for guid, available in db.execute(
            "select guid, max(originally_available_at) from metadata_item_views "
            "where metadata_type = 1 and guid is not null group by guid"):
        if isinstance(available, int):
            years[guid] = time.gmtime(available).tm_year
    for guid, year in db.execute(
            "select guid, year from metadata_items where metadata_type = 1 and guid is not null"):
        if isinstance(year, int) and guid not in years:
            years[guid] = year
    db.close()
    return years


def load_titles(db_path):
    """Return {guid: show or movie title} from play events and the library."""
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    titles = {}
    # metadata_type: 1 movie, 2 show, 3 season, 4 episode.
    for guid, mtype, grandparent, title in db.execute(
            "select guid, metadata_type, grandparent_title, title from metadata_item_views "
            "where guid is not null"):
        title = grandparent if mtype == 4 else title
        if title and guid not in titles:
            titles[guid] = title
    for guid, mtype, title, parent, grandparent in db.execute(
            "select i.guid, i.metadata_type, i.title, p.title, gp.title from metadata_items i "
            "left join metadata_items p on p.id = i.parent_id "
            "left join metadata_items gp on gp.id = p.parent_id where i.guid is not null"):
        title = grandparent if mtype == 4 else parent if mtype == 3 else title
        if title and guid not in titles:
            titles[guid] = title
    db.close()
    return titles


def load_details(db_path):
    """Return {guid: {"type", "title", "item_title", "season", "episode"}} from the database.

    "title" is the show or movie title, "item_title" the episode title. Play events come
    first; library items fill in what they lack.
    """
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    details = {}

    def add(guid, mtype, title, item_title, season, episode):
        entry = details.setdefault(guid, {"type": mtype})
        for key, value in (("title", title), ("item_title", item_title), ("season", season),
                           ("episode", episode)):
            if entry.get(key) in (None, "") and value not in (None, ""):
                entry[key] = value

    for guid, mtype, grandparent, title, season, episode in db.execute(
            "select guid, metadata_type, grandparent_title, title, parent_index, \"index\" "
            "from metadata_item_views where guid is not null"):
        if mtype == 4:
            add(guid, mtype, grandparent, title, season, episode)
        else:
            add(guid, mtype, title, None, None, None)
    # metadata_type: 1 movie, 2 show, 3 season, 4 episode.
    for guid, mtype, title, index, parent, parent_index, grandparent in db.execute(
            "select i.guid, i.metadata_type, i.title, i.\"index\", p.title, p.\"index\", "
            "gp.title from metadata_items i left join metadata_items p on p.id = i.parent_id "
            "left join metadata_items gp on gp.id = p.parent_id where i.guid is not null"):
        if mtype == 4:
            add(guid, mtype, grandparent, title, parent_index, index)
        elif mtype == 3:
            add(guid, mtype, parent, title, index, None)
        else:
            add(guid, mtype, title, None, None, None)
    db.close()
    return details


def parse_guid(guid):
    """Classify a Plex guid.

    Returns ("episode", show_ids, season, episode, numbering), ("movie", ids),
    ("plex", kind, plex_id) or ("skip", reason).
    """
    base = guid.split("?")[0]
    m = re.match(r"([^:]+)://(.*)", base)
    if not m:
        return ("skip", "unparseable")
    scheme, path = m.groups()
    parts = path.split("/")

    if scheme == "plex":
        kind = parts[0]
        if kind in ("episode", "movie") and len(parts) == 2:
            return ("plex", kind, parts[1])
        if kind in ("show", "season"):
            return ("skip", "show/season level")
        return ("skip", f"plex {kind}")
    if scheme == "local":
        return ("skip", "local")
    if scheme == "com.plexapp.agents.thetvdb":
        if len(parts) == 3:
            return ("episode", {"tvdb": parts[0]}, int(parts[1]), int(parts[2]), NUMBERING_TVDB)
        return ("skip", "show/season level")
    if scheme == "com.plexapp.agents.themoviedb":
        if len(parts) == 1:
            return ("movie", {"tmdb": parts[0]})
        if len(parts) == 3:
            return ("episode", {"tmdb": parts[0]}, int(parts[1]), int(parts[2]), NUMBERING_TVDB)
        return ("skip", "show/season level")
    if scheme == "com.plexapp.agents.imdb":
        return ("movie", {"imdb": parts[0]})
    if scheme == "com.plexapp.agents.hama":
        if len(parts) != 3:
            return ("skip", "show/season level")
        source, _, source_id = parts[0].rpartition("-")
        # Older HAMA guids carry a bare AniDB id.
        if source in ("", "anidb"):
            # The middle part is the TVDB season (0: specials); the episode number is the
            # AniDB entry's own, e.g. Harvest Final anidb-55401/3/1 is its episode 1.
            if parts[1] == "0":
                # HAMA numbers AniDB's S1.. specials 1-99; 101+ are openings/endings,
                # 201+ trailers, 301+ parodies, 501+ other extras.
                if not 1 <= int(parts[2]) <= 99:
                    return ("skip", "anidb credits and extras")
                return ("episode", {"anidb": int(source_id)}, 0, int(parts[2]), NUMBERING_ANIDB)
            return ("episode", {"anidb": int(source_id)}, 1, int(parts[2]), NUMBERING_ANIDB)
        if source == "tvdb":
            return ("episode", {"tvdb": source_id}, int(parts[1]), int(parts[2]), NUMBERING_TVDB)
        return ("skip", f"hama {source}")
    if scheme in ("com.plexapp.agents.lastfm", "com.plexapp.agents.plexmusic"):
        return ("skip", "music")
    if scheme == "com.plexapp.agents.none":
        return ("skip", "no agent")
    return ("skip", scheme)
