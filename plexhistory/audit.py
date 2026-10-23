"""The audit command: report HAMA data problems in the whole database, without exporting.

Checks every AniDB-numbered item of the account against Simkl's catalog, whether or not it
was sent before or is already on Simkl:
- plays whose guid disagrees with their show guid or index, and what they resolve to;
- shows whose episode numbers run past the end of their Simkl entry (wrong AniDB entry);
- absolute episode numbers and the episode they map to.

Uses only Simkl lookups that don't count against the daily quota (/redirect, episode lists),
so it needs SIMKL_CLIENT_ID but no access token.
"""

from plexhistory.common import log
from plexhistory.export import (absolute_target, check_mismatched_plays, collect, item_label,
                                misidentified_shows, play_label, resolve_mismatched_plays,
                                take_mismatched_plays, verify_episode)
from plexhistory.filters import select
from plexhistory.plexdb import NUMBERING_ANIDB, load_details, load_titles, load_watched
from plexhistory.simkl import Simkl, SimklCatalog
from plexhistory.tmdb import Tmdb, load_anime_ids


def section(title, lines):
    print(f"{title} ({len(lines)})")
    for line in lines:
        print(f"  {line}")
    print()


def run(args, error):
    if not args.simkl_client_id:
        error("SIMKL_CLIENT_ID is needed for Simkl's catalog (--simkl-client-id, environment "
              "or env file)")
    catalog = SimklCatalog(Simkl(args.simkl_client_id, None, None, args.env_file),
                           args.simkl_cache, args.simkl_cache_days)
    log(f"Reading watch history for Plex account {args.plex_account_id} from {args.db}")
    watched = load_watched(args.db, args.plex_account_id, args.only, args.skip_synced)
    titles = load_titles(args.db)
    if args.guid_regex or args.title_regex or args.title or args.start or args.count is not None:
        tmdb = Tmdb(args.tmdb_api_key, args.tmdb_cache, args.tmdb_cache_days) \
            if args.title else None
        watched = select(watched, args, titles, None, tmdb)
    plays = take_mismatched_plays(args.db, args.plex_account_id, watched)
    episode_titles = {g: d.get("item_title") for g, d in load_details(args.db).items()}
    _, episodes = collect(watched, None, {}, titles, None, episode_titles)
    shows = {e["show"] for e in episodes.values()
             if e["numbering"] == NUMBERING_ANIDB and "anidb" in e["ids"]}
    log(f"Checking {len(shows)} AniDB shows and {len(plays)} mismatched plays against "
        f"Simkl's catalog")

    lines = []
    for play, found, reason in check_mismatched_plays(plays, catalog):
        lines.append(f"{play_label(play)}: " + (
            f"anidb-{found[0]} episode {found[1]} by {reason}" if found else f"skipped, {reason}"))
    section("Plays whose guid disagrees with their show guid or index", lines)

    resolve_mismatched_plays(plays, episodes, catalog)
    anime_ids = load_anime_ids(args.anime_ids)
    wrong = misidentified_shows(episodes, anime_ids, catalog)
    labels = {e["show"]: e["label"] or e["show"] for e in episodes.values()}
    section("Shows probably matched to the wrong AniDB entry (export skips them)",
            [f"{labels[show]} [{show}]: {reason}" for show, reason in sorted(wrong.items())])

    lines = []
    for rec in sorted((e for e in episodes.values() if e["numbering"] == NUMBERING_ANIDB
                       and "anidb" in e["ids"] and e["show"] not in wrong),
                      key=lambda e: (e["show"], e["episode"])):
        found = absolute_target(rec, anime_ids, catalog)
        if not found:
            continue
        anidb, target, summed = found
        if summed:
            matched, reason = verify_episode([(anidb, target["episode"])],
                                             rec.get("episode_title"), rec["watched_at"], catalog)
            result = f"checked by {reason}" if matched else f"not used, {reason}"
        else:
            result = "direct (Simkl's TVDB numbers of the season)"
        lines.append(f"{item_label('episode', rec)} -> anidb-{anidb} episode {target['episode']}: "
                     f"{result}")
    section("Absolute episode numbers", lines)
