# Project notes

Everything learned while building this project (October 2026), so later work does not have to
rediscover it. More detail on specific topics:
- [plex-watch-state-api.md](plex-watch-state-api.md): Plex account watch state, history and
  date editing.
- [anime-id-mappings.md](anime-id-mappings.md): AniDB mapping sources.
- [../README.md](../README.md) and [../README.examples.md](../README.examples.md): usage.

## Goal

Recover the watch history kept in old Plex Media Server databases, including shows and movies
long deleted from the servers, and record it on a tracker (Simkl) or on the Plex account itself
(where Plex's own web "Watch History" shows it).

## The Plex database

The database files (`com.plexapp.plugins.library.db`, copied from each server) are personal
data, ignored by git and only ever opened read-only.

### Tables

- `metadata_item_views`: one row per play.
  - Columns: `account_id`, `guid`, `metadata_type`, `grandparent_title` (show),
    `parent_index` (season), `index` (episode), `title` (episode or movie title),
    `viewed_at` (epoch seconds), `grandparent_guid`.
  - It keeps titles, so it is the best source for deleted items.
- `metadata_item_settings`: watch state per item.
  - Columns: `guid`, `account_id`, `view_count`, `view_offset`, `last_viewed_at`, `rating`,
    `created_at`, `updated_at`.
  - No titles. `view_count > 0` means watched.
- `metadata_items`: the library as it is now. An item is **orphaned** when its guid is not in
  `metadata_items.guid`.
- `accounts`: id → name. `1` is the server owner; other users have their plex.tv account ids.
- `metadata_type`: 1 movie, 2 show, 3 season, 4 episode, 8 artist, 9 album, 10 track, 12 clip.

History tables are keyed by guid, not by `metadata_items.id`, which is why they survive deletion.

### What the rows mean

- **Rows synced from plex.tv:** watch-state rows with no play event and empty
  `created_at` / `view_offset` (most `plex://` rows of non-owner accounts) are very likely
  pushed down by plex.tv's "Sync my watch state" from other servers or Plex streaming. In the
  databases looked at, most of a non-owner account's rows left no trace in either server's
  play history, and a large part of the owner's orphaned rows were such rows too: shows that
  were only ever on another server. `--skip-synced` leaves them out. On the Plex target they
  are already watched (the account state they came from).
- **Plex GraphQL rate limit:** the first real Plex-target run stopped after 7 items with
  `Rate limit exceeded!` (`extensions.code: RATE_LIMITED`, `retryAfter: 60`) from
  community.plex.tv; the 7th item stayed dated "now" and was fixed with `set-date`. GraphQL
  calls now wait `retryAfter` and retry.
- **GraphQL limit counts mutations, not requests:** batching 17 `updateActivity` mutations
  under aliases in one request was rejected five times in a row (it can never fit). A probe on
  those 17 items: a 40-entry history query passed, then 1 and 2 mutations per request passed
  and 4 failed, within seconds. The limit is roughly 5-6 date updates per minute, so the Plex
  target manages about that many items per minute; dates now go out three per request.
- **GraphQL limit, measured:** a probe sent one `updateActivity` every ~2 s: 6 passed, the 7th
  was `RATE_LIMITED`. History queries every 5 s kept passing meanwhile. Updates passed again
  about 60 s after the first of the six (49 s after the refusal) and 6 more passed; `retryAfter`
  stayed 60 throughout. So: 6 date updates per 60 s, reads unlimited. The Plex target paces
  date updates to that window.
- **Plex 503s at scale:** a dry run of a few thousand items stopped on an HTTP 503 from the
  metadata service with 0.2 s between requests; requests are now 0.5 s apart and 5xx
  responses are retried.
- **Show and season rows:** `metadata_item_settings` also has show- and season-level rows
  (`thetvdb://<show>`, `thetvdb://<show>/<season>`). They carry no episode information and are
  skipped on export.
- **`local://<n>` guids:** items without an agent match; `<n>` was the old `metadata_items.id`.
  No external ids of their own; only rows with play events have titles.

### Guid formats and what is done with them

| Guid | Meaning | Export |
|---|---|---|
| `plex://episode|movie/<hex>` | New Plex agent; needs a lookup for external ids | via Plex metadata provider |
| `plex://show|season/<hex>` | Show/season level state | skipped |
| `com.plexapp.agents.thetvdb://<show>/<s>/<e>` | Legacy TVDB agent | tvdb + numbers |
| `com.plexapp.agents.themoviedb://<id>[/<s>/<e>]` | Legacy TMDB agent | tmdb |
| `com.plexapp.agents.imdb://tt…` | Legacy movie agent | imdb |
| `com.plexapp.agents.hama://anidb-<id>/1/<e>` and `hama://<id>/1/<e>` (bare = AniDB) | HAMA anime agent, AniDB numbering | anidb |
| `com.plexapp.agents.hama://anidb-<id>/0/<e>` | AniDB specials | skipped (numbering unclear) |
| `com.plexapp.agents.hama://tvdb-<id>/<s>/<e>` | HAMA with TVDB numbering | tvdb |
| `local://`, `agents.none`, `agents.lastfm`, `plex://track|album|artist`, `iva://` | no ids / music / extras | skipped |

Database reading tips:
- **Read-only access:** use `mode=ro`, or `immutable=1` when the `-wal`/`-shm` files are missing.
  `sqlite3 -readonly` failed on one database while `immutable=1` worked.
- **Hex timestamps:** `plex://` ids start with an 8-hex-digit creation timestamp
  (`5d9c…` = 2019-10, `65dd…` = 2024-02).

## Plex APIs

- **Server history:** `GET /status/sessions/history/all` on a server (documented at
  developer.plex.tv/pms) returns rows shaped like `metadata_item_views`. It only covers what
  that server has.
- **Metadata provider:** `https://metadata.provider.plex.tv/library/metadata/<id>` needs
  `X-Plex-Token` (401 "You must provide a token!" otherwise); a normal free account token
  works. `?includeGuids=1` returns a `Guid` list with `imdb://`, `tmdb://`, `tvdb://`.
- **Re-created ids:** Plex re-creates episode ids when it rebuilds a season.
  - Old ids then return 404 (several shows re-created in 2024).
  - The show id survives, so `plexdb.load_episode_fallbacks` uses `grandparent_guid` +
    season/episode from the database to recover them.
  - This works only where a play event or library row exists, not for watch-state-only rows.
- **Matching and listing:** `matches?type=1|2&guid=tvdb://…` finds shows and movies by external
  id; `children` lists seasons and episodes. The paging rules (20 per page by default; paging
  only via headers because Cloudflare blocks it in the query string; max page size 300) are in
  plex-watch-state-api.md.
- **Account watch state:** the Discover userState / scrobble / unscrobble endpoints and the
  `community.plex.tv` GraphQL API (`watchHistory`, `updateActivity`, `removeActivity`) are in
  plex-watch-state-api.md, together with the observed season/show behaviour.
- **Cloud history cleanup:** the plex.tv cloud watch history is the same GraphQL API that
  [plex-community-watch-history-cleanup](https://github.com/martadams89/plex-community-watch-history-cleanup)
  uses to delete entries.

## Simkl API

Docs: https://api.simkl.org/llms.txt (index of markdown pages).

### Auth

- **Registration:** AUTH V2. Register the app as **"TV, devices & command line"**: no redirect
  URI and no secret are needed. "Mobile, desktop & browser apps" needs a redirect URI (PKCE).
- **Device flow:**
  1. `POST /oauth2/device` (form: `client_id`, `scope=media:read media:write`);
  2. the user opens `verification_uri_complete`;
  3. poll `POST /oauth2/token` (`grant_type=urn:ietf:params:oauth:grant-type:device_code`)
     every `interval` seconds, handling `authorization_pending` and `slow_down` (+5 s).
  There is no deny signal, so use a timeout. No PKCE was needed for the device flow.
- **Scope:** without `media:write` the token is read-only, and writes fail with
  `403 insufficient_scope`. An unknown scope is silently downgraded.
- **Token lifetimes:** access tokens last 7 days; refresh tokens last 180 days and are not
  rotated. Refresh with `POST /oauth2/token grant_type=refresh_token`. One grant has one live
  access token, so only one process should refresh it.
- **Every request** needs `client_id`, `app-name` and `app-version` URL parameters and a
  `User-Agent`; user calls also need `Authorization: Bearer`. Leave `Authorization` off the
  cached catalog endpoints.
- **Account id:** `GET /users/settings` → `account.id`. Simkl asks
  not to fetch it every run, so it is kept in `.env` as `SIMKL_ACCOUNT_ID`.

### Limits

- **Rate:** 10 GET/s and 1 POST/s. A POST over the limit causes a temporary throttle block.
- **Daily quota (AUTH V2, per user):** 500 requests on free, 1,000 on PRO, 10,000 on VIP.
- **429:** a `Retry-After` comes back only on the daily-quota 429. Quotas reset at midnight
  America/New_York.
- **Batching:** write in batches (this project posts 100 items per request).

### Writing history: `POST /sync/history`

- **Matching:** send any ids (`imdb`, `tmdb`, `tvdb`, `mal`, `anidb`, `anilist`, `kitsu`, plus
  `title`/`year` as fallback); no Simkl id is needed.
- **Dates:** `watched_at` (ISO-8601 UTC) per movie or per episode.
- **Anime numbering:** anime is numbered AniDB-style (flat season 1) by default.
  `use_tvdb_anime_seasons: true` interprets TVDB seasons; it is set for TVDB-numbered sources.
- **Duplicates:** re-posting an already watched episode is a no-op. `?allow_rewatch=yes`
  creates rewatch sessions (PRO/VIP only, silently ignored on free). This project never sends
  it, so reruns are safe.
- **Response:** `added` counts plus a `not_found` that echoes the input items (a movie, or a
  show with the seasons/episodes it carried). There is no per-episode confirmation.
- **Removing:** `POST /sync/history/remove` takes the same body.

### Reading the library: `GET /sync/all-items/{shows|anime|movies}/{status}`

- **Episodes:** need `extended=full`; add `episode_watched_at=yes` for per-episode dates and
  `include_all_episodes=yes` so completed shows list their episodes.
- **Large libraries:** `400 max_items` means split the call, or use
  `include_all_episodes=original`.
- **Polling:** don't call it repeatedly; Simkl suspends client ids that poll all-items without
  `/sync/activities` + `date_from`.

### Other Simkl notes

- **ID search:** `/search/id?anidb=…` exists, but Simkl asks not to use it before writes (writes
  resolve ids themselves) and not in loops; `/redirect?to=simkl&<id>=…` plus the cached detail
  endpoints is the preferred lookup.
- **Importers:** the CSV importer page (simkl.com/apps/import/csv) is behind a Cloudflare
  challenge. Its columns (from search results) are per show (`LastEpWatched`), not per episode
  with dates, so the API is the better import path.
- **Status after a partial watch:** marking S01E01 of a 12-episode anime made it
  "anime / watching", 1/12 episodes, with the exact `watched_at`.

## TMDB

- **Auth:** a v3 API key is a 32-hex string sent as the `api_key` query parameter; v4 read
  tokens are JWTs (`eyJ…`) sent as a Bearer header.
- **Search:** `/search/tv` (`first_air_date_year`) and `/search/movie`
  (`primary_release_year`).
- **External ids:** `/tv|movie/<id>/external_ids` return imdb, tvdb (TV only), tvrage,
  wikidata and social ids, but **no AniDB or MAL ids**.
- **Search results change over time:** a title search can gain matches later (remakes,
  undated entries). The cache expires after 7 days by default for that reason.

## Other tools and services looked at

- **Trakt Plex Sync** (Trakt VIP): connects to a Plex server and imports its history.
  **PlexTraktSync** and **PlexyTrack** sync from a live server. None handles orphaned rows in
  the database.
- **Simkl's Plex integration:** a Plex Pass webhook for new plays at 90% only.
- **CrossWatch:**
  - Anime ids: nattadasu/animeApi plus anibridge-mappings.
  - Plex target: writes only to items in a server library, via server `/:/scrobble` with an
    undocumented `viewedAt` parameter, and checks the date afterwards.
- **Floppy (Yamtrack fork):**
  - REST API with per-user token; `POST /api/v1/media/tv/{source}/{id}/{s}/episodes/{e}/watch/`
    and `/media/movie/{source}/{id}/watch/` take `end_date` and an `external_id` that makes
    repeated calls safe.
  - The REST routes accept only `tmdb` (TV/movies) or `mal` (anime) sources.
  - It has a Simkl importer, so Plex DB → Simkl → Floppy works without new code.
  - Anime ids come from Kometa Anime-IDs, pinned to a commit and checked by digest.

## Design decisions in this project

- **Language:** standard library only; Python 3.9+.
- **Settings precedence:** CLI flag, then environment variable, then `.env`.
- **Verbosity:** 0–3, set by `-q` / `-v` / `-vv` / `VERBOSITY`. Progress and logs go to stderr;
  payloads and listings go to stdout.
- **Watch date:** the earliest play event, else `last_viewed_at`.
- **Merging:** items that share any external id are merged (`IdMerger`), and show ids are
  unified across episodes.
- **Avoiding duplicates on Simkl:** read the library first, skip watched items (compared only
  when numbering matches; TVDB-numbered anime is left to Simkl's own duplicate check), then the
  sent / not-found records.
- **Records:** per target (`simkl_*` / `plex_*` `_sent.json` and `_not_found.json`), keyed by
  account id. Not-found entries expire after 30 days.
- **Caches:**
  - `plex_metadata_cache.json` caches Plex lookups, 404s included.
  - `tmdb_cache.json` expires after 7 days.
  - `anime_ids.json` is downloaded when missing.
  - All files are written via a temp file + rename, so an interrupted run never truncates
    them; `.env` is written with mode 600.
- **Plex target:** per-episode scrobble plus a history date edit, never season or show scrobbles
  (those lose per-episode dates and double count).
- **`list`:** works offline from the database and caches unless `--lookup` is given.
- **Tests:** `unittest` with a synthetic database and fake HTTP (`tests/support.py`). Mutation
  checks (deliberately breaking code) were used to confirm the tests catch regressions.

## Matching anime on Simkl

- **Plex numbering vs TVDB:** `plex://` episode numbers follow TMDB, which keeps later cours in
  season 1 (some shows have 25-55 episodes in S1). Simkl maps anime by TVDB seasons, so those
  numbers are not found. Simkl's docs call this out.
- **TVDB episode ids did not help:** sending Plex's TVDB *episode* ids (`episodes[].ids.tvdb`)
  found none of them.
- **What worked:** converting to the next TVDB season using Simkl's own catalog
  (`/search/id?tvdb=` → `/anime/episodes/{id}?extended=full`, which lists each episode's
  `tvdb.season/episode`).
- **Legacy TVDB season 2 of an anime:** `thetvdb://<id>/2/*` was not routed by TVDB season
  either. Kometa's mapping to the second season's AniDB entry worked.
- **Simkl's catalog entries:** Simkl splits anime into one entry per cour. `/search/id?tvdb=`
  returns only one of them.
- **Film series as one anime entry:** Simkl can list a film series as one movie-type anime
  entry (e.g. 5 episodes) instead of five movies, and may know neither the IMDb nor TMDB id of a
  part. `/search/anime` (needs the user token) finds the entry by title; its episode N carries
  the TMDB subtitle of part N. The "part of a multi-part Simkl entry" fallback sends such movies
  as episode N.
- **Wrong HAMA guids:** about 1% of HAMA episode plays have a guid that disagrees with the
  play's show guid or index. Sometimes the show guid + index were right (confirmed by Simkl air
  dates and titles), sometimes the guid looked right and the show guid wrong. So neither field
  is trusted; Simkl's catalog decides (title, else air date within 14 days, fail closed). An
  early export had sent such a play to the wrong entry (an OVA); that watch and the library
  entry it created were removed.
- **Wrong AniDB entry for a whole show:** a show's TV episodes 1-12 were stored under the AniDB
  id of its 2-episode OVA. The export sent E01-E02 to the OVA (the entry was removed again) and
  Simkl did not find E03-E12. Export now skips such shows (numbers past the entry's end that
  absolute numbering doesn't explain). The same check flags entries Simkl splits differently
  (one film on Simkl, three parts on AniDB).
- **Later cours numbered by TVDB season:** HAMA stores some later-cour episodes under the
  previous AniDB entry with TVDB-season numbers (entry A episode 13 = TVDB S01E13 = entry B
  episode 1). Simkl's episode lists carry the TVDB numbers, so these map exactly. Episodes that
  are OVAs on TVDB (season 0, own AniDB entry) stay skipped.
- **`local://` episodes with a known show:** files Plex never matched still record the show
  guid and season/episode in their play rows (agent show guids such as `thetvdb://<id>`, and
  many `plex://show` anime seasons). Export identifies them through the show.
- **HAMA season part:** in `hama://anidb-<id>/3/1` the `1` is the AniDB entry's episode 1 and
  the `3` is the TVDB season. All such guids seen (seasons 2 and 3) agree with Simkl's TVDB
  season for that entry episode, and titles match. They were skipped as specials before; only
  season 0 is specials.
- **AniDB specials on Simkl:** `POST /sync/history` with the AniDB id and season 0 records
  `S00E<n>` on the entry (tested with one special, then removed). Simkl's special numbers equal
  AniDB's S numbers for most entries; not for all (e.g. a "Special 24.5", or an OVA Simkl numbers
  differently), which are skipped when the catalog lacks the number. HAMA's 101+
  (openings/endings, trailers, extras) are not sent.
- **Specials whose numbers differ:** Simkl's special numbers don't always follow AniDB's
  ("Special 24.5" = AniDB S24 = Simkl special 1, watched a day after it aired, a day before in
  UTC). Such specials are matched by title or air date. For the Plex target, Plex's own season
  0 is matched the same way; Anime-Lists (and anibridge, built from it) map only ~700 entries'
  specials, and only to TVDB numbers, which Plex doesn't follow (a picture drama that is TVDB
  S00E31 was a differently numbered special edition on Plex). When Plex has two specials on the
  same air date, the special stays unmatched.
- **Outdated TVDB ids in agent guids:** some legacy `thetvdb://` show ids are no longer known
  to Simkl. Neither Plex's `matches?guid=` nor Simkl resolve the old ids, but Plex's
  `matches?type=2&title=` returns the show with current ids, which Simkl knows. Export retries
  such episodes with those ids as a last fallback.
- **Free lookups:** `/redirect?to=simkl&anidb=...` (read the 301 `Location`, don't follow it)
  and the episode lists don't count against the daily quota; `/search/id` does. A miss
  redirects to `https://simkl.com`.
- **Simkl catalog episodes** (`/anime/episodes/{id}?extended=full`) carry `title`, `date` (air
  date with offset) and `tvdb{season,episode}`; Plex's HAMA titles matched them.
- **Absolute numbers:** `hama://anidb-<id>/1/38` (S2E25 stored as 13 + 25) is found by the
  "absolute episode number" fallback.
- **"Found" is not "added":** Simkl reports items it already had as neither added nor not found,
  so some recovered items count as found but not added.

## Gotchas met during the session

- **zsh:** an unquoted `$var` holding several arguments is not split into words (use bash or
  arrays), and a `--include=*.py` glob fails with "no matches found".
- **Broken pipes:** piping into a reader that exits early needs `BrokenPipeError` handling
  (done in `plex_history.py`).
- **Permissions in Claude Code:** the auto-mode permission check blocks writes to real
  accounts (Plex, Simkl) until the user explicitly allows them in chat.
- **Request bodies in Plex Web:** a `window.fetch` hook did not see Plex Web's GraphQL
  requests. The operation texts were found by searching the downloaded JS bundles instead.
- **Plex Web dates:** its "Edit Watched Date" picker stores local midnight, not the exact time.

## Open ideas

- **Plex target at scale:** paced to the measured GraphQL limit; long runs are resumable.
- **Unrecoverable Plex ids:** `plex://` episode ids with no play event and not in any library
  can't be recovered once Plex re-creates them.
- **Untested:** scrobbling a Plex show id.
- **Better anime numbering:** anibridge-mappings for AniDB → TVDB episode mapping beyond season
  plus offset.
- **Other users:** exporting accounts other than the owner needs that person's own Simkl login
  (`--env-file`), and their history is mostly plex.tv-synced already.
