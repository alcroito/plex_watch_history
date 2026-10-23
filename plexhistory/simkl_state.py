"""The simkl-state command: inspect and change Simkl watch history.

For manual experiments with the Simkl API. Needs SIMKL_CLIENT_ID and SIMKL_ACCESS_TOKEN (see
simkl-login). Commands that change the account only print what they would do unless --write
is given.
"""

import argparse
import json

from plexhistory import common
from plexhistory.simkl import Simkl, SimklCatalog

ID_KEYS = ("simkl", "imdb", "tmdb", "tvdb", "anidb", "mal")


def add_arguments(p):
    p.add_argument("--simkl-client-id", help="Simkl app client_id (env: SIMKL_CLIENT_ID)")
    p.add_argument("--write", action="store_true", help="Actually change the Simkl account")
    sub = p.add_subparsers(dest="state_command", required=True)

    def add_ids(parser):
        for key in ID_KEYS:
            parser.add_argument(f"--{key}", help=f"{key} id")

    lk = sub.add_parser("lookup", help="Find a Simkl item by external id (GET /search/id)")
    add_ids(lk)
    lk.add_argument("--type", choices=("show", "movie"),
                    help="Needed with --tmdb, whose ids are shared by shows and movies")
    it = sub.add_parser("items", help="List library entries of the account")
    it.add_argument("--type", choices=("shows", "anime", "movies"), action="append",
                    help="Library section (repeatable, default: all three)")
    it.add_argument("--status", default="all",
                    choices=("all", "watching", "plantowatch", "hold", "completed", "dropped"))
    it.add_argument("--id", action="append", metavar="KEY=VALUE",
                    help="Only entries with this id, e.g. anidb=36487 (repeatable: entries "
                         "with any of them)")
    it.add_argument("--episodes", action="store_true",
                    help="Also print watched episodes with their dates")
    ep = sub.add_parser("episodes", help="Episodes of a catalog entry: number, air date, TVDB "
                                          "numbers (anime) and title")
    add_ids(ep)
    ep.add_argument("--kind", choices=("anime", "tv"), default="anime",
                    help="Catalog of a --simkl id (default: %(default)s); other ids are looked "
                         "up with /redirect, which gives the kind")
    ep.add_argument("--specials", action="store_true", help="Also list specials")
    se = sub.add_parser("search", help="Search the catalog by title (GET /search/{type})")
    se.add_argument("title")
    se.add_argument("--type", choices=("anime", "tv", "movie", "all"), default="anime",
                    help="Catalog to search (default: %(default)s)")
    sub.add_parser("quota", help="Requests left in today's Simkl quota (one counted request, "
                                 "GET /sync/activities)")
    for name, help_text in (("mark", "Add watch events (POST /sync/history)"),
                            ("unmark", "Remove watch events (POST /sync/history/remove)")):
        m = sub.add_parser(name, help=help_text)
        add_ids(m)
        m.add_argument("--movie", action="store_true", help="The ids are a movie's")
        m.add_argument("--season", type=int, default=1,
                       help="Season of --episode (default: 1, AniDB numbering for anime)")
        m.add_argument("--episode", type=episode_arg, nargs="+", metavar="N[=DATE]" if
                       name == "mark" else "N",
                       help="Episode numbers; required for shows" + (
                           ". N=DATE gives an episode its own watched_at, e.g. "
                           "1=2018-05-04T18:20:00Z" if name == "mark" else ""))
        if name == "mark":
            m.add_argument("--date", help="watched_at, e.g. 2015-01-15T20:30:45Z "
                                          "(default: Simkl uses the request time)")
        else:
            m.add_argument("--all", action="store_true",
                           help="Remove the whole entry from the library (history and list "
                                "status) instead of episodes")


def episode_arg(text):
    """ "3" -> (3, None), "3=2018-05-04T18:20:00Z" -> (3, "2018-05-04T18:20:00Z")."""
    number, _, date = text.partition("=")
    if not number.isdigit() or ("=" in text and not date):
        raise argparse.ArgumentTypeError(f"expected N or N=DATE, got {text!r}")
    return int(number), date or None


# Simkl documents these ids as integers; the others (e.g. imdb tt..., tmdb) stay strings.
INTEGER_IDS = ("simkl", "anidb", "mal")


def ids_from(args):
    ids = {k: getattr(args, k) for k in ID_KEYS if getattr(args, k, None)}
    return {k: int(v) if k in INTEGER_IDS and v.isdigit() else v for k, v in ids.items()}


def history_payload(args):
    """The /sync/history body for the ids, episodes and date in args."""
    item = {"ids": ids_from(args)}
    date = getattr(args, "date", None)
    if args.movie:
        if date:
            item["watched_at"] = date
        return {"movies": [item], "shows": []}
    if getattr(args, "all", False):
        return {"movies": [], "shows": [item]}
    episodes = [{"number": n, **({"watched_at": own or date} if own or date else {})}
                for n, own in args.episode]
    item["seasons"] = [{"number": args.season, "episodes": episodes}]
    return {"movies": [], "shows": [item]}


def describe_entry(media, entry):
    item = entry.get("show") or entry.get("movie") or {}
    ids = ", ".join(f"{k}={v}" for k, v in (item.get("ids") or {}).items() if k in ID_KEYS)
    watched = "" if media == "movies" else \
        f" {entry.get('watched_episodes_count')}/{entry.get('total_episodes_count')} episodes"
    return f"{media:6} {entry.get('status', ''):11} {item.get('title')} ({item.get('year')}) " \
           f"[{ids}]{watched} last watched {entry.get('last_watched_at')}"


def run(args, client_id, token, refresh_token, env_path, error):
    simkl = Simkl(client_id, token, refresh_token, env_path)
    if args.state_command == "lookup":
        params = ids_from(args)
        if not params:
            error("give at least one id, e.g. --anidb 36487")
        if args.type:
            params["type"] = args.type
        for item in simkl.get("/search/id", **params) or []:
            ids = ", ".join(f"{k}={v}" for k, v in (item.get("ids") or {}).items())
            print(f"{item.get('type')}: {item.get('title')} ({item.get('year')}) [{ids}]")
    elif args.state_command == "items":
        wanted = {tuple(i.split("=", 1)) for i in args.id or []}
        for media in args.type or ("shows", "anime", "movies"):
            params = {"extended": "full", "episode_watched_at": "yes"} if args.episodes else {}
            data = simkl.get(f"/sync/all-items/{media}/{args.status}", **params) or {}
            for entry in data.get(media) or []:
                ids = ((entry.get("show") or entry.get("movie") or {}).get("ids") or {})
                if wanted and not wanted & {(k, str(v)) for k, v in ids.items()}:
                    continue
                print(describe_entry(media, entry))
                for season in entry.get("seasons") or [] if args.episodes else []:
                    for ep in season.get("episodes") or []:
                        print(f"    S{season['number']:02}E{ep['number']:02} "
                              f"{ep.get('watched_at') or ''}")
    elif args.state_command == "episodes":
        ids = ids_from(args)
        catalog = SimklCatalog(simkl)
        kind, simkl_id = args.kind, ids.pop("simkl", None)
        if not simkl_id:
            if not ids:
                error("give at least one id, e.g. --anidb 36487")
            found = catalog.entry(**ids)
            if not found or found[0] == "movies":
                raise SystemExit("not found on Simkl" if not found else "a movie has no episodes")
            kind, simkl_id = found
        for e in catalog.episodes(kind, simkl_id, args.specials):
            tvdb = e.get("tvdb") or {}
            number = f"S{e['season']:02}E{e['episode']:02}" if kind == "tv" \
                else f"E{e.get('episode') or 0:02}"
            mapped = f"TVDB S{tvdb['season']:02}E{tvdb['episode']:02}" \
                if tvdb.get("season") is not None and tvdb.get("episode") is not None else ""
            print(f"{'special ' if e.get('type') == 'special' else ''}{number:7} "
                  f"{(e.get('date') or '')[:10]:10} {mapped:13} {e.get('title') or ''}")
    elif args.state_command == "quota":
        simkl.get("/sync/activities")
        print(common.rate_limit_text() or "Simkl did not report the quota (the first request "
                                          "with a new token carries no rate-limit headers)")
    elif args.state_command == "search":
        for item in simkl.get(f"/search/{args.type}", q=args.title, extended="full") or []:
            ids = ", ".join(f"{k}={v}" for k, v in (item.get("ids") or {}).items()
                            if k in ("simkl_id", "slug"))
            details = list(dict.fromkeys([item.get("endpoint_type"), item.get("type")]))
            if item.get("ep_count"):
                details.append(f"{item['ep_count']} episodes")
            print(f"{item.get('title')} ({item.get('year')}) [{ids}] "
                  f"{', '.join(d for d in details if d)}")
    else:
        if not ids_from(args):
            error("give at least one id, e.g. --anidb 36487")
        if getattr(args, "all", False) and args.episode:
            error("--all removes the whole entry; leave out --episode")
        if not args.movie and not args.episode and not getattr(args, "all", False):
            error("--episode is required for shows (or pass --movie, or --all to unmark)")
        if args.state_command == "unmark" and any(date for _, date in args.episode or []):
            error("unmark takes episode numbers without dates")
        payload = history_payload(args)
        path = "/sync/history" if args.state_command == "mark" else "/sync/history/remove"
        if not args.write:
            print(f"would POST {path} {json.dumps(payload)} (add --write to do it)")
            return
        result = simkl.post(path, payload) or {}
        print(f"done: POST {path}")
        for key in ("added", "deleted", "not_found"):
            if key in result:
                print(f"  {key}: {json.dumps(result[key])}")
