# Plex account watch state and history

Findings from implementing `--target plex`, as tested on 2026-10-23 against a real Plex
account (two anime shows). None of these endpoints are officially documented except
where noted; they are what Plex Web uses and may change.

## Account-level vs server-level state

- **Server level**: a Plex Media Server stores watch state per library item in
  `metadata_item_settings` / `metadata_item_views`. Its `PUT /:/scrobble?key=<ratingKey>&identifier=com.plexapp.plugins.library`
  is documented at https://developer.plex.tv/pms/ ("does not create any view history ... just
  sets the state as played"). It only works for items in that server's library.
  [CrossWatch](https://github.com/cenodude/CrossWatch) writes history this way and adds an
  undocumented `viewedAt=<epoch>` parameter, then checks `lastViewedAt` with a tolerance because
  it is not sure the date is applied. Items not in the library are reported as unresolved.
- **Account level (Discover)**: Plex also keeps watch state and a watch history on the plex.tv
  account, independent of any server. Plex Web's "Mark as Watched" on Discover pages and the
  profile watch history use this. It works for items on no server, which is what this project
  needs for orphaned history.

## Endpoints

All requests send `X-Plex-Token` (header or query parameter). Sending `X-Plex-Product` and
`X-Plex-Client-Identifier` headers is advisable.

| Purpose | Request |
|---|---|
| Account uuid | `GET https://plex.tv/api/v2/user` → `uuid` |
| Item metadata (external ids in `Guid`) | `GET https://metadata.provider.plex.tv/library/metadata/<id>?includeGuids=1` |
| Find an item by external id | `GET https://metadata.provider.plex.tv/library/metadata/matches?type=<1 movie, 2 show>&guid=tvdb://<id>` (also `tmdb://`, `imdb://`, and legacy `com.plexapp.agents.thetvdb://<id>`) |
| Seasons of a show / episodes of a season | `GET https://metadata.provider.plex.tv/library/metadata/<id>/children` |
| Watch state of an item | `GET https://discover.provider.plex.tv/library/metadata/<id>/userState` → `viewCount`, `viewState`, `lastViewedAt`; for shows and seasons `viewedLeafCount` |
| Mark watched | `GET https://discover.provider.plex.tv/actions/scrobble?identifier=tv.plex.provider.discover&key=<id>` (Plex Web uses this exact call; empty response body) |
| Mark unwatched | same with `actions/unscrobble` |
| Watch history | GraphQL (below) `user(id: <uuid>) { watchHistory(first, after) { nodes { id date metadataItem { id title } } pageInfo { hasNextPage endCursor } } }` |
| Set a history entry's date | GraphQL `mutation updateActivityDate($id: ID!, $input: UpdateActivityInput!) { updateActivity(id: $id, input: $input) { id } }` with `{"id": <activity id>, "input": {"date": "2015-01-15T20:30:45Z"}}` |
| Delete a history entry | GraphQL `mutation removeActivity($input: RemoveActivityInput!) { removeActivity(input: $input) }` with `{"input": {"id": <activity id>, "type": "WATCH_HISTORY"}}` |

GraphQL is `POST https://community.plex.tv/api` with a JSON body `{"query", "variables"}`.

- **Introspection is disabled**: schema queries fail. The operation texts above were found in
  Plex Web's JavaScript bundle (`app.plex.tv/desktop/js/main-*.js`); `GetActivityFeed`,
  `activityByID`, `activityFeed(metadataID, types)` also exist there.
- **Where the date edit lives**: Plex Web exposes the date edit as "Edit Watched Date" in the ⋮
  menu of an entry on the profile's Watch History page.

## Paging and Cloudflare

- **Small default pages**: `children` and other lists return 20 items by default; `totalSize`
  gives the full count.
- **Paging in headers only**: paging must be sent as **headers** `X-Plex-Container-Start` /
  `X-Plex-Container-Size`. The same parameters in the query string are blocked by Cloudflare
  (HTTP 403 with an HTML "Attention Required" page).
- **Page size limit**: sizes up to 300 work; 500 and 1000 return HTTP 400.
- **Specials**: `metadata.provider` lists specials (season 0); `discover.provider` omitted
  them for the show tested.

## Setting a watch date

- **Mark watched always uses "now"**: no date parameter was found for the account-level
  scrobble (python-plexapi's `MyPlexAccount.markPlayed` only sends `key` and `identifier`).
- **Change the date afterwards**: the history entry can be dated with `updateActivity`.
  Through the API the full timestamp is kept (`2015-01-15T20:30:45.000Z` read back exactly).
- **UI dates are day-only**: Plex Web's date picker only offers a day; it stored local midnight
  (`2015-01-14T23:00:00.000Z` for 15 January in CET).
- **`lastViewedAt` is not changed**: `userState.lastViewedAt` keeps the time of the scrobble;
  only the history entry carries the edited date.
- **Finding the entry**: right after a scrobble, the new entry is among the newest
  `watchHistory` nodes, matched on `metadataItem.id`.

## Seasons and shows

Tested with an anime show (season 1 with 12 episodes, specials season 0 with 6):

- **Season progress is derived**: marking all 12 season 1 episodes individually made season 1
  `viewedLeafCount` 12 of `leafCount` 12, so the season counts as fully watched without any
  extra call. Seasons and shows have no own `viewCount`; their progress is the
  `viewedLeafCount` of their episodes.
- **The show includes specials**: the show's `leafCount` was 18 (12 + 6 specials), so with all
  of season 1 watched the show was at 12/18, i.e. not fully watched even though it has a
  single regular season.
- **One history entry per episode**: individual episode scrobbles create one watch history
  entry each, so each can carry its own date.
- **Season scrobble**: scrobbling the season id marks all its episodes watched in one call,
  but:
  - it creates a single history entry for the season (not per episode), so per-episode dates
    are lost;
  - it increments `viewCount` of episodes that were already watched (episode 1 went from 1 to 2).

  Scrobbling the show id was not tested.

## Unmarking

- **`unscrobble` resets the count**: it sets `viewCount` to 0.
- **History entries may survive**: unscrobbling episodes 2–12 also removed the history
  entries just created for them, but unscrobbling episode 1 did not remove its older entry
  whose date had been edited to 2016. It needed an explicit `removeActivity`. Which entries
  `unscrobble` removes (recent ones, unedited ones, or both) is not known; always check the
  history afterwards.
- **Re-marking can duplicate**: unscrobble + scrobble on an item that still had a history
  entry left two entries for it.

## How this project uses it

`PlexTarget` in `plexhistory/plex.py`:
1. matches items by tvdb/tmdb/imdb (AniDB via Kometa's `anime_ids.json`), and finds episodes
   through `children`;
2. skips items whose `userState.viewCount > 0`;
3. scrobbles per episode or movie;
4. sets the history entry's date.

It never scrobbles seasons or shows.

`plex_history.py plex-state` wraps these calls for manual experiments (see its `--help`).
