"""The plex-state command: inspect and change Plex account watch state and history.

For manual experiments with the APIs described in docs/plex-watch-state-api.md. Commands that
change the account only print what they would do unless --write is given.
"""

import time
import urllib.parse

from plexhistory import common
from plexhistory.plex import PLEX_DISCOVER_API, PLEX_METADATA_API, PlexTarget

HISTORY_QUERY = ("query h($id: ID!, $first: PaginationInt!, $after: String) { user(id: $id) { "
                 "watchHistory(first: $first, after: $after) { nodes { id date metadataItem "
                 "{ id title type } } pageInfo { hasNextPage endCursor } } } }")


class Plex(PlexTarget):
    def __init__(self, token):
        super().__init__(token, {}, {})

    def state(self, plex_id):
        url = f"{PLEX_DISCOVER_API}/library/metadata/{plex_id}/userState"
        states = ((common.http_json(url, self._headers()) or {}).get("MediaContainer") or {}) \
            .get("UserState") or [{}]
        return states[0]

    def metadata(self, plex_id):
        data = common.http_json(f"{PLEX_METADATA_API}/{plex_id}", self._headers()) or {}
        return ((data.get("MediaContainer") or {}).get("Metadata") or [{}])[0]

    def history(self, first=20, scan_all=False):
        """Yield watch history nodes, newest first."""
        after = None
        while True:
            data = self._gql(HISTORY_QUERY, {"id": self.account(), "first": 100 if scan_all
                                             else first, "after": after})
            page = data["user"]["watchHistory"]
            yield from page["nodes"]
            if not scan_all or not page["pageInfo"]["hasNextPage"]:
                return
            after = page["pageInfo"]["endCursor"]

    def scrobble(self, plex_id, undo=False):
        action = "unscrobble" if undo else "scrobble"
        common.http_json(f"{PLEX_DISCOVER_API}/actions/{action}?" + urllib.parse.urlencode(
            {"identifier": "tv.plex.provider.discover", "key": plex_id}), self._headers())

    def set_date(self, activity_id, date):
        self._gql("mutation updateActivityDate($id: ID!, $input: UpdateActivityInput!) "
                  "{ updateActivity(id: $id, input: $input) { id } }",
                  {"id": activity_id, "input": {"date": date}})

    def remove(self, activity_id):
        self._gql("mutation removeActivity($input: RemoveActivityInput!) "
                  "{ removeActivity(input: $input) }",
                  {"input": {"id": activity_id, "type": "WATCH_HISTORY"}})


def show_entry(node):
    item = node.get("metadataItem") or {}
    print(f"{node['id']}  {node['date']}  {item.get('id')}  {item.get('type')}  {item.get('title')}")


def add_arguments(p):
    p.add_argument("--plex-token", help="Plex token (env: PLEX_TOKEN)")
    p.add_argument("--write", action="store_true", help="Actually change the Plex account")
    sub = p.add_subparsers(dest="state_command", required=True)
    m = sub.add_parser("match", help="Find a Plex id by external id")
    m.add_argument("--movie", action="store_true", help="Look up a movie instead of a show")
    for scheme in ("tvdb", "tmdb", "imdb"):
        m.add_argument(f"--{scheme}")
    t = sub.add_parser("tree", help="List the seasons and episodes of a show with watch state")
    t.add_argument("show")
    s = sub.add_parser("state", help="Print userState of items")
    s.add_argument("ids", nargs="+")
    h = sub.add_parser("history", help="Print watch history entries, newest first")
    h.add_argument("--first", type=int, default=20)
    h.add_argument("--item", action="append", help="Only entries of this Plex id (repeatable)")
    h.add_argument("--all", action="store_true", help="Scan the whole history")
    mk = sub.add_parser("mark", help="Mark items watched, optionally setting the history date")
    mk.add_argument("ids", nargs="+")
    mk.add_argument("--date", help="ISO-8601 date for the new history entries, e.g. "
                                   "2015-01-15T20:30:45Z")
    um = sub.add_parser("unmark", help="Mark items unwatched")
    um.add_argument("ids", nargs="+")
    um.add_argument("--remove-history", action="store_true",
                    help="Also delete history entries of the items that unscrobble leaves")
    sd = sub.add_parser("set-date", help="Set the date of a history entry")
    sd.add_argument("activity")
    sd.add_argument("date")
    rm = sub.add_parser("remove", help="Delete history entries")
    rm.add_argument("activities", nargs="+")


def run(args, token):
    plex = Plex(token)

    def write(description, action):
        if not args.write:
            print(f"would {description} (add --write to do it)")
            return False
        action()
        print(f"done: {description}")
        return True

    if args.state_command == "match":
        for scheme in ("tvdb", "tmdb", "imdb"):
            value = getattr(args, scheme)
            if value:
                key = plex._match(1 if args.movie else 2, f"{scheme}://{value}")
                print(f"{scheme}://{value}: {key} {plex.metadata(key).get('title') if key else ''}")
    elif args.state_command == "tree":
        show = plex.metadata(args.show)
        print(f"{show.get('title')} ({show.get('year')}): leafCount {show.get('leafCount')}, "
              f"userState {plex.state(args.show)}")
        for season, season_key in sorted(plex._children(args.show).items()):
            print(f"  season {season} {season_key}: {plex.state(season_key)}")
            for episode, key in sorted(plex._children(season_key).items()):
                state = plex.state(key)
                print(f"    S{season:02}E{episode:02} {key} viewCount {state.get('viewCount', 0)}")
    elif args.state_command == "state":
        for plex_id in args.ids:
            print(plex_id, plex.state(plex_id))
    elif args.state_command == "history":
        wanted = set(args.item or [])
        for node in plex.history(args.first, args.all):
            if not wanted or (node.get("metadataItem") or {}).get("id") in wanted:
                show_entry(node)
    elif args.state_command == "mark":
        for plex_id in args.ids:
            if not write(f"mark {plex_id} watched", lambda: plex.scrobble(plex_id)):
                continue
            if args.date:
                time.sleep(1)
                node = next((n for n in plex.history(10)
                             if (n.get("metadataItem") or {}).get("id") == plex_id), None)
                if node:
                    plex.set_date(node["id"], args.date)
                    print(f"done: dated history entry {node['id']} {args.date}")
                else:
                    print(f"no new history entry found for {plex_id}; date not set")
    elif args.state_command == "unmark":
        for plex_id in args.ids:
            write(f"mark {plex_id} unwatched", lambda: plex.scrobble(plex_id, undo=True))
        if args.remove_history and args.write:
            time.sleep(1)
            wanted = set(args.ids)
            for node in [n for n in plex.history(scan_all=True)
                         if (n.get("metadataItem") or {}).get("id") in wanted]:
                plex.remove(node["id"])
                print(f"done: removed history entry {node['id']} ({node['date']})")
    elif args.state_command == "set-date":
        write(f"set history entry {args.activity} to {args.date}",
              lambda: plex.set_date(args.activity, args.date))
    elif args.state_command == "remove":
        for activity in args.activities:
            write(f"remove history entry {activity}", lambda: plex.remove(activity))
