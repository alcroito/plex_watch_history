"""Command line: shared settings handling and the subcommands."""

import argparse
import os
import re

from plexhistory import audit, common, export, listing, plex_state, simkl, simkl_state
from plexhistory.common import HttpError, read_env_file

DESCRIPTION = """Plex watch history tools.

Reads watch history from a Plex Media Server library database (metadata_item_views and
metadata_item_settings), including items no longer in the library, and records it on Simkl
or on the Plex account itself.

Settings are read from command line flags, then environment variables, then a .env file
(KEY=value lines, --env-file). Run a subcommand with --help for its settings.
"""

# (attribute, environment / .env key, default) for settings that can come from the environment.
EXPORT_SETTINGS = (("target", "SYNC_TARGET", "simkl"),
                   ("db", "PLEX_DB_PATH", "com.plexapp.plugins.library.db"),
                   ("plex_token", "PLEX_TOKEN", None),
                   ("simkl_client_id", "SIMKL_CLIENT_ID", None),
                   ("simkl_token", "SIMKL_ACCESS_TOKEN", None),
                   ("tmdb_api_key", "TMDB_API_KEY", None),
                   ("tmdb_cache_days", "TMDB_CACHE_DAYS", 7.0),
                   ("not_found_days", "SIMKL_NOT_FOUND_DAYS", 30.0),
                   ("simkl_cache_days", "SIMKL_CACHE_DAYS", 30.0),
                   ("anime_ids", "ANIME_IDS_PATH", "anime_ids.json"),
                   ("guid_regex", "FILTER_GUID_REGEX", None),
                   ("title_regex", "FILTER_TITLE_REGEX", None),
                   ("title", "FILTER_TITLE", None),
                   ("year", "FILTER_YEAR", None),
                   ("start", "FILTER_START", 0),
                   ("count", "FILTER_COUNT", None))


def add_common_arguments(p):
    p.add_argument("--env-file", default=".env",
                   help="File with KEY=value settings (default: %(default)s)")
    p.add_argument("--verbosity", type=int, choices=range(4),
                   help="0: only warnings, login prompts and the result; 1: steps, totals and "
                        "progress; 2: per-table, per-section and per-batch detail; 3: every "
                        "skipped item and HTTP request (env: VERBOSITY, default: 1)")
    p.add_argument("-v", "--verbose", action="count", default=0,
                   help="Increase verbosity by one per flag (-v is 2, -vv is 3)")
    p.add_argument("-q", "--quiet", action="store_true", help="Same as --verbosity 0")
    p.add_argument("--timestamps", action="store_true",
                   help="Prefix log lines with the time (env: LOG_TIMESTAMPS=1)")


def add_source_arguments(p):
    """Where the history comes from and which items to take: shared by export and list."""
    p.add_argument("--db",
                   help="Plex library database (env: PLEX_DB_PATH, "
                        "default: com.plexapp.plugins.library.db)")
    p.add_argument("--plex-account-id", type=int, default=1,
                   help="Plex account whose history to read (default: %(default)s, the server "
                        "owner)")
    p.add_argument("--plex-token",
                   help="Plex token, needed to resolve plex:// guids (env: PLEX_TOKEN)")
    p.add_argument("--cache", default="plex_metadata_cache.json",
                   help="Cache file for Plex metadata lookups (default: %(default)s)")
    p.add_argument("--only", choices=("orphaned", "present"),
                   help="Only items no longer in the Plex library (orphaned), or only items "
                        "still in it (present). Default: both")
    p.add_argument("--skip-synced", action="store_true",
                   help="Leave out watch-state rows without a play on this server and without "
                        "a created_at: plex.tv's 'Sync my watch state' copies watches from other "
                        "servers that way (e.g. server B's watches in server A). Only use it when those "
                        "servers are exported too; watches made in Plex's own streaming apps "
                        "may exist only as such rows")
    f = p.add_argument_group(
        "filters", "Applied in this order: --guid-regex, title filters, then --start/--count")
    f.add_argument("--guid-regex",
                   help="Keep items whose Plex guid matches this regex (env: FILTER_GUID_REGEX)")
    f.add_argument("--title-regex",
                   help="Keep items whose show or movie title matches this case-insensitive "
                        "regex. plex:// items without a title in the DB are looked up on Plex "
                        "(env: FILTER_TITLE_REGEX)")
    f.add_argument("--title",
                   help="Keep items of the show or movie found by searching TMDB for this "
                        "title (matched by tmdb/tvdb/imdb id), or whose DB title equals it. "
                        "Needs TMDB_API_KEY (env: FILTER_TITLE)")
    f.add_argument("--year", type=int,
                   help="Release year to narrow the --title search (env: FILTER_YEAR)")
    f.add_argument("--start", type=int,
                   help="Skip this many items, ordered by first watch date (env: FILTER_START)")
    f.add_argument("--count", type=int,
                   help="Keep at most this many items after --start (env: FILTER_COUNT)")
    f.add_argument("--tmdb-api-key",
                   help="TMDB v3 API key or v4 read access token (env: TMDB_API_KEY)")
    f.add_argument("--anime-ids",
                   help="Kometa AniDB id mapping used by --title to match AniDB guids; "
                        "downloaded when missing (env: ANIME_IDS_PATH, default: anime_ids.json)")
    f.add_argument("--tmdb-cache", default="tmdb_cache.json",
                   help="Cache file for TMDB responses (default: %(default)s)")
    f.add_argument("--tmdb-cache-days", type=float,
                   help="Days before a cached TMDB response is fetched again; 0 disables the "
                        "cache (env: TMDB_CACHE_DAYS, default: 7)")


def add_simkl_cache_arguments(p):
    p.add_argument("--simkl-cache", default="simkl_catalog_cache.json",
                   help="Cache file for Simkl catalog lookups (ids, episode lists, searches) "
                        "(default: %(default)s)")
    p.add_argument("--simkl-cache-days", type=float,
                   help="Days before a cached Simkl catalog lookup is fetched again; 0 keeps "
                        "them for the run only (env: SIMKL_CACHE_DAYS, default: 30)")


def add_export_arguments(p):
    add_source_arguments(p)
    p.add_argument("--target", choices=("simkl", "plex"),
                   help="Where to record the watch history: Simkl, or the Plex account itself "
                        "(Discover watch state and history, works for items on no server) "
                        "(env: SYNC_TARGET, default: simkl)")
    p.add_argument("--simkl-client-id",
                   help="Simkl app client_id (env: SIMKL_CLIENT_ID)")
    p.add_argument("--simkl-token",
                   help="Simkl user access token (env: SIMKL_ACCESS_TOKEN). If missing, a "
                        "device login is started and the tokens are saved to the env file")
    p.add_argument("--simkl-login", action="store_true",
                   help="Run the Simkl device login even if a token is configured")
    add_simkl_cache_arguments(p)
    p.add_argument("--not-found",
                   help="Items the target could not match, per account; they are skipped in "
                        "later runs until they expire (default: <target>_not_found.json, i.e. "
                        "simkl_not_found.json or plex_not_found.json)")
    p.add_argument("--plex-batch", type=int, default=20,
                   help="Plex target: items marked per batch; each batch needs two GraphQL "
                        "requests, which Plex rate-limits (default: %(default)s; 1 marks items "
                        "one at a time)")
    p.add_argument("--pending",
                   help="Plex target: items marked watched whose original date is not set yet "
                        "(interrupted run, 'date not set'); the next run sets it first "
                        "(default: plex_pending.json)")
    p.add_argument("--not-found-days", type=float,
                   help="Days before items the target could not match are tried again; 0 "
                        "always retries them (env: SIMKL_NOT_FOUND_DAYS, default: 30)")
    p.add_argument("--retry-not-found", action="store_true",
                   help="Send items listed in --not-found again even if they have not expired")
    p.add_argument("--sent-cache",
                   help="Items already recorded on the target, per account; they are skipped in "
                        "later runs (default: <target>_sent.json, i.e. simkl_sent.json or "
                        "plex_sent.json)")
    p.add_argument("--resend", action="store_true",
                   help="Send items listed in --sent-cache again")
    p.add_argument("--forget", metavar="REGEX",
                   help="Only remove --sent-cache and --not-found records whose key (e.g. "
                        "'anidb:44015 S01E02', 'movie imdb:tt1156099') or not-found label "
                        "matches this case-insensitive regex, for every account, then stop. "
                        "Sent records hold ids, not titles. With --dry-run nothing is removed")
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would be sent instead of sending it")


def add_list_arguments(p):
    add_source_arguments(p)
    p.add_argument("--by", choices=("item", "show"), default="item",
                   help="One row per watched item, or one per show (episodes grouped) and movie "
                        "(default: %(default)s)")
    p.add_argument("--format", choices=("table", "tsv", "json"), default="table",
                   help="Output format (default: %(default)s)")
    p.add_argument("--type", action="append",
                   choices=sorted(set(listing.TYPES.values()) | {"unknown"}),
                   help="Only items of this type (repeatable); unknown: rows without a type, "
                        "e.g. local:// items with no title. With --by show it applies before "
                        "grouping")
    p.add_argument("--unmappable", action="store_true",
                   help="Only items export cannot map to ids, with the reason in the note column "
                        "(local without a usable show, no agent, AniDB specials, show/season "
                        "level, music, ...). Without --lookup, plex:// items missing from the "
                        "Plex cache are listed too ('not in the Plex lookup cache')")
    p.add_argument("--plays", action="store_true",
                   help="One row per play event (metadata_item_views) of the selected items, "
                        "with the show guid and numbers recorded at play time; items only "
                        "marked watched have no plays. Overrides --by")
    p.add_argument("--lookup", action="store_true",
                   help="Look up plex:// items missing from the Plex cache on Plex (needs "
                        "PLEX_TOKEN) and cache them. Without it only the cache is used")


def set_verbosity(args, env, error):
    common.TIMESTAMPS = args.timestamps or \
        (os.environ.get("LOG_TIMESTAMPS") or env.get("LOG_TIMESTAMPS")) == "1"
    if args.quiet:
        common.VERBOSITY = 0
    elif args.verbose:
        common.VERBOSITY = min(3, 1 + args.verbose)
    elif args.verbosity is not None:
        common.VERBOSITY = args.verbosity
    else:
        value = os.environ.get("VERBOSITY") or env.get("VERBOSITY") or "1"
        if value not in ("0", "1", "2", "3"):
            error(f"VERBOSITY must be 0-3, got {value!r}")
        common.VERBOSITY = int(value)


def resolve_settings(args, env, error, settings):
    """Fill unset settings from the environment and .env, then validate them."""
    for attr, var, default in settings:
        if hasattr(args, attr) and getattr(args, attr) is None:
            setattr(args, attr, os.environ.get(var) or env.get(var) or default)
    for attr, var in (("year", "FILTER_YEAR"), ("start", "FILTER_START"),
                      ("count", "FILTER_COUNT")):
        value = getattr(args, attr)
        if isinstance(value, str):
            if not value.isdigit():
                error(f"{var} must be a non-negative integer, got {value!r}")
            setattr(args, attr, int(value))
    for attr, var in (("tmdb_cache_days", "TMDB_CACHE_DAYS"),
                      ("not_found_days", "SIMKL_NOT_FOUND_DAYS"),
                      ("simkl_cache_days", "SIMKL_CACHE_DAYS")):
        if not hasattr(args, attr):
            continue
        try:
            setattr(args, attr, float(getattr(args, attr)))
        except ValueError:
            error(f"{var} must be a number, got {getattr(args, attr)!r}")
        if getattr(args, attr) < 0:
            error(f"--{attr.replace('_', '-')} must not be negative")
    if args.start < 0 or (args.count is not None and args.count < 0):
        error("--start and --count must not be negative")
    for attr in ("guid_regex", "title_regex"):
        try:
            re.compile(getattr(args, attr) or "")
        except re.error as e:
            error(f"invalid --{attr.replace('_', '-')}: {e}")
    if args.title and not args.tmdb_api_key:
        error("--title needs a TMDB API key (--tmdb-api-key or TMDB_API_KEY)")
    if not os.path.exists(args.db):
        error(f"database not found: {args.db}")


def resolve_export_settings(args, env, error):
    resolve_settings(args, env, error, EXPORT_SETTINGS)
    if args.target not in ("simkl", "plex"):
        error(f"SYNC_TARGET must be simkl or plex, got {args.target!r}")
    # Each target keeps its own records, so Simkl and Plex progress never mix.
    args.sent_cache = args.sent_cache or f"{args.target}_sent.json"
    args.not_found = args.not_found or f"{args.target}_not_found.json"
    args.pending = args.pending or f"{args.target}_pending.json"
    if args.forget:
        try:
            re.compile(args.forget)
        except re.error as e:
            error(f"invalid --forget: {e}")
    elif args.target == "plex" and not args.plex_token:
        error("--target plex needs a Plex token (--plex-token or PLEX_TOKEN)")


def build_parser():
    p = argparse.ArgumentParser(description=DESCRIPTION,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    e = sub.add_parser(
        "export", help="Record the watch history on Simkl or the Plex account",
        description=export.__doc__ + "\nItems already watched on the target, sent before or "
                                     "not found before are skipped.")
    add_common_arguments(e)
    add_export_arguments(e)
    ls = sub.add_parser(
        "list", help="List the watched items in the database",
        description=listing.__doc__ + "\nItems are enriched from the Plex lookup cache; the "
                                      "same filters as export apply.")
    add_common_arguments(ls)
    add_list_arguments(ls)
    au = sub.add_parser("audit", help="Report HAMA data problems in the whole database",
                        description=audit.__doc__,
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_arguments(au)
    add_source_arguments(au)
    au.add_argument("--simkl-client-id", help="Simkl app client_id (env: SIMKL_CLIENT_ID)")
    add_simkl_cache_arguments(au)
    lg = sub.add_parser(
        "simkl-login", help="Get or refresh the Simkl access token",
        description="Run Simkl's device login (open the printed link and approve the app), or "
                    "with --refresh get a new access token from the saved refresh token. The "
                    "tokens and the Simkl account id are saved to the env file.")
    add_common_arguments(lg)
    lg.add_argument("--simkl-client-id", help="Simkl app client_id (env: SIMKL_CLIENT_ID)")
    lg.add_argument("--refresh", action="store_true",
                    help="Refresh the access token with SIMKL_REFRESH_TOKEN instead of logging in")
    s = sub.add_parser("plex-state", help="Inspect and change Plex account watch state",
                       description=plex_state.__doc__,
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_arguments(s)
    plex_state.add_arguments(s)
    ss = sub.add_parser("simkl-state", help="Inspect and change Simkl watch history",
                        description=simkl_state.__doc__,
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_arguments(ss)
    simkl_state.add_arguments(ss)
    return p, {"export": e, "list": ls, "audit": au, "simkl-login": lg, "plex-state": s,
               "simkl-state": ss}


def main(argv=None):
    p, subparsers = build_parser()
    args = p.parse_args(argv)
    error = subparsers[args.command].error
    env = read_env_file(args.env_file)
    set_verbosity(args, env, error)
    try:
        if args.command == "export":
            resolve_export_settings(args, env, error)
            export.run(args, env, error)
        elif args.command == "list":
            resolve_settings(args, env, error, EXPORT_SETTINGS)
            if args.lookup and not args.plex_token:
                error("--lookup needs a Plex token (--plex-token or PLEX_TOKEN)")
            listing.run(args)
        elif args.command == "audit":
            resolve_settings(args, env, error, EXPORT_SETTINGS)
            audit.run(args, error)
        elif args.command == "simkl-login":
            args.simkl_client_id = (args.simkl_client_id or os.environ.get("SIMKL_CLIENT_ID")
                                    or env.get("SIMKL_CLIENT_ID"))
            simkl.run_login(args, env, error)
        elif args.command == "simkl-state":
            setting = lambda key: os.environ.get(key) or env.get(key)  # noqa: E731
            client_id = args.simkl_client_id or setting("SIMKL_CLIENT_ID")
            if not client_id or not setting("SIMKL_ACCESS_TOKEN"):
                error("SIMKL_CLIENT_ID and SIMKL_ACCESS_TOKEN are needed (run simkl-login)")
            simkl_state.run(args, client_id, setting("SIMKL_ACCESS_TOKEN"),
                            setting("SIMKL_REFRESH_TOKEN"), args.env_file, error)
        elif args.command == "plex-state":
            token = args.plex_token or os.environ.get("PLEX_TOKEN") or env.get("PLEX_TOKEN")
            if not token:
                error("PLEX_TOKEN is not set in the environment or the env file")
            plex_state.run(args, token)
    except HttpError as e:
        raise SystemExit(str(e))
