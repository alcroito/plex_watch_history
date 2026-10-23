# plex-history-to-simkl

Exports watch history from a Plex Media Server library database to [Simkl](https://simkl.com),
including items that were removed from the server.

Plex keeps play history and watch state keyed by `guid`, not by library item, so rows survive
after the media is deleted. The Plex APIs only expose state for items still in the library, so
this tool reads the SQLite database directly.

## Data sources

| Table | Contents |
|---|---|
| `metadata_item_views` | One row per play event, with titles and `viewed_at`. |
| `metadata_item_settings` | Per-item watch state: `view_count`, `view_offset`, `last_viewed_at`. No titles. |

Both are keyed by `guid` and `account_id`. An item counts as watched if it has a view row or
`view_count > 0`. The watch date sent to Simkl is the earliest play, otherwise `last_viewed_at`.

## Guid handling

| Guid | Sent to Simkl as |
|---|---|
| `com.plexapp.agents.thetvdb://<show>/<s>/<e>` | Show `tvdb` id + season/episode |
| `com.plexapp.agents.themoviedb://<id>[/<s>/<e>]` | `tmdb` id (movie or episode) |
| `com.plexapp.agents.imdb://tt...` | Movie `imdb` id |
| `com.plexapp.agents.hama://anidb-<id>/<s>/<e>` and `hama://<id>/<s>/<e>` | `anidb` id + episode `<e>`, AniDB numbering. `<s>` is the TVDB season (Harvest Final is `anidb-55401/3/1`). `<s>` `0` is specials: `<e>` 1-99 (AniDB S1-S99) is sent as a season 0 episode of the entry (Simkl records it as `S00E<n>`): special `<e>` if Simkl's catalog lists it, otherwise the one Simkl special whose title matches the play's or that aired from a day before to 14 days before the watch (Blob's "Special 24.5", AniDB S24, is Simkl's special 1); unmatched specials are skipped. 101+ (openings, endings, trailers, extras) is skipped |
| `com.plexapp.agents.hama://tvdb-<id>/<s>/<e>` | `tvdb` id, TVDB numbering |
| `plex://episode/<id>`, `plex://movie/<id>` | Resolved via the Plex metadata provider to imdb/tmdb/tvdb ids |
| `local://<n>` episodes | Through the show guid and season/episode numbers of their play rows (or the library): an agent show guid (`thetvdb`, `themoviedb`, `hama`) is read as that agent's episode guid, a `plex://show` guid is resolved like `plex://` episodes. Iron Spire's files were never matched (`local://44655`), but their plays name the show `thetvdb://382752` |

Skipped: `local://` movies and episodes whose show has no usable guid, music (`lastfm`, `plex://track|album|artist`),
`agents.none`, AniDB credits and extras (season 0, 101+), and show/season level rows (their episodes are
exported instead).

HAMA sometimes stored a play under another AniDB entry's guid: HSA S3E13 was recorded as
`anidb-43154/1/2` (S2E2) while the play's show guid (`anidb-45726`) and index (13) were right.
Plays whose guid disagrees with their show guid or index are checked against Simkl's catalog
(needs a Simkl token): the candidates are the guid's episode and the show guid's episode at
the index. A real episode title must match exactly one episode of those entries; without a
title (or with "Episode 38" and the like) exactly one candidate must have aired from a day
before to 14 days before the watch. If title and air date point to different episodes, or
nothing matches, the play is skipped (listed at `-v`). The guid's own watch date comes only
from plays that agree with it.

## Setup

1. Copy the Plex database (`com.plexapp.plugins.library.db`) from the server.
2. Get a Plex token, needed to resolve `plex://` guids. For example: in Plex Web open an item,
   "Get Info" > "View XML", and copy `X-Plex-Token` from the URL.
3. Register a Simkl app at https://simkl.com/settings/developer/ with the client type
   **"TV, devices & command line"** (no redirect URL or secret needed).
4. Put `SIMKL_CLIENT_ID` in `.env`. On the first non-dry run the script starts Simkl's device
   login: it prints a `https://simkl.com/pin?user_code=...` link, waits until you approve it in a
   browser, and saves `SIMKL_ACCESS_TOKEN` and `SIMKL_REFRESH_TOKEN` to the env file (mode 600).
   Access tokens last 7 days, refresh tokens 180 days. When Simkl rejects the access token the
   script refreshes it and saves the new one, falling back to a new device login if the refresh
   fails. `--simkl-login` forces a new login.

   To do this up front, or later on its own, run `python3 plex_history.py simkl-login` (device
   login) or `python3 plex_history.py simkl-login --refresh` (new access token from the saved
   refresh token, no browser). Both also save `SIMKL_ACCOUNT_ID`.

## Configuration

Each setting is read from a CLI flag, then an environment variable, then a `.env` file
(`--env-file`, default `.env`).

| Flag | Env / `.env` key | Default |
|---|---|---|
| `--target` | `SYNC_TARGET` | `simkl` (`plex` writes to the Plex account) |
| `--db` | `PLEX_DB_PATH` | `com.plexapp.plugins.library.db` |
| `--plex-token` | `PLEX_TOKEN` | none |
| `--simkl-client-id` | `SIMKL_CLIENT_ID` | none |
| `--simkl-token` | `SIMKL_ACCESS_TOKEN` | written by the device login |
| | `SIMKL_REFRESH_TOKEN` | written by the device login |
| `--verbosity`, `-v`, `-q` | `VERBOSITY` | `1` |
| `--tmdb-api-key` | `TMDB_API_KEY` | none (needed for `--title`) |
| `--tmdb-cache-days` | `TMDB_CACHE_DAYS` | `7` (`0` disables the TMDB cache) |
| `--simkl-cache-days` | `SIMKL_CACHE_DAYS` | `30` (`0` keeps Simkl catalog lookups for the run only) |
| `--anime-ids` | `ANIME_IDS_PATH` | `anime_ids.json` (downloaded when missing) |
| `--guid-regex` | `FILTER_GUID_REGEX` | none |
| `--title-regex` | `FILTER_TITLE_REGEX` | none |
| `--title` | `FILTER_TITLE` | none |
| `--year` | `FILTER_YEAR` | none |
| `--start` | `FILTER_START` | `0` |
| `--count` | `FILTER_COUNT` | all |

Verbosity levels (`-q` is 0, `-v` is 2, `-vv` is 3):

| Level | Output |
|---|---|
| 0 | Warnings, the Simkl login prompt, the final result |
| 1 | Step headers, totals per step, progress of Plex lookups |
| 2 | Per-table counts, skip reasons, Simkl library sections, per-batch results |
| 3 | Every skipped guid, every item already on Simkl, every HTTP request |

Other flags: `--plex-account-id` (default `1`, the server owner), `--simkl-login`, `--cache`,
`--not-found`, `--not-found-days`, `--retry-not-found`, `--sent-cache`, `--resend`, `--dry-run`, and `--only orphaned|present` to export only items no longer in the
Plex library (guid not in `metadata_items`) or only items still in it.

`--skip-synced` (export, list, audit) leaves out watch-state rows that have no play on this
server and no `created_at`. plex.tv's "Sync my watch state" copies watches from other servers
that way: a show watched only on another server shows up as "orphaned" rows here. Use it when
the other servers are exported too; watches made in Plex's own streaming apps may exist only as
such rows, so it is off by default.

Failed requests are retried: HTTP 429 after `Retry-After`, HTTP 5xx and dropped connections
after 5, 15 and 45 seconds. Plex requests are spaced 0.5 seconds apart.

### Filters

Applied after `--only`, in this order:

1. `--guid-regex`: searched in the raw Plex guid, e.g. `'^com\.plexapp\.agents\.thetvdb://'`.
2. Title filters:
   - `--title-regex`: case-insensitive, searched in the show or movie title. Titles come from
     the database (play events, library items); `plex://` items without one are looked up on Plex.
   - `--title` (optional `--year`): searches TMDB for TV shows and movies with that exact title
     (or original title), falling back to the top result of each kind, and keeps items whose
     tmdb/tvdb/imdb ids match, or whose database title equals the given title. The matches are
     logged. TMDB has no AniDB ids, so for HAMA anime guids (`hama://anidb-<id>`, `hama://<id>`)
     the TMDB/TVDB/IMDb ids are mapped to AniDB ids with
     [Kometa's anime_ids.json](https://github.com/Kometa-Team/Anime-IDs), which is downloaded
     to `--anime-ids` when missing. Delete the file to get a newer copy.
   - Matching `plex://` items by id needs a Plex lookup per item, which can take minutes the
     first time; the lookups are cached.
3. `--start` / `--count`: a window over the remaining items, ordered by first watch date.

Filters set in `.env` apply to every run until removed.

Example `.env` before the first run:

```
PLEX_DB_PATH=com.plexapp.plugins.library.db
PLEX_TOKEN=...
SIMKL_CLIENT_ID=...
```

## Usage

```
python3 plex_history.py export --dry-run   # print the payloads, post nothing
python3 plex_history.py export             # post to Simkl (runs the device login if needed)
python3 plex_history.py list               # show the watched items in the database
python3 plex_history.py audit              # report HAMA data problems (Simkl catalog checks)
python3 plex_history.py plex-state --help  # inspect / change Plex account watch state
python3 plex_history.py simkl-state --help # inspect / change Simkl watch history
```

`simkl-state` commands: `lookup --anidb 36487` (Simkl item by external id), `items [--type anime]
[--status watching] [--id anidb=36487 ...] [--episodes]` (library entries with watched episodes
and dates; several `--id` match entries with any of them), `episodes --anidb 43154 [--specials]`
(catalog episodes with air dates, TVDB numbers and titles), `search "title" [--type anime|tv|
movie|all]`, `quota` (requests left today; one counted request), `mark` / `unmark` (`--anidb|
--tvdb|--imdb|... --episode 1 2 [--season 1] [--date ...]` or `--movie`; `mark --episode
1=DATE 2=DATE` gives each episode its own date; `unmark --all` removes the whole entry from
the library). `mark` and `unmark` only print the request unless `--write` is given.

`audit` checks the whole history of `--plex-account-id` (all filters and `--only` apply)
against Simkl's catalog, whether or not items were sent before, and prints three lists: plays
whose guid disagrees with their show guid or index and what they resolve to, shows that look
matched to the wrong AniDB entry (export skips them), and absolute episode numbers with the
episode they map to. It uses only lookups that don't count against the daily quota and needs
`SIMKL_CLIENT_ID` but no access token.

Settings flags (`--db`, filters, ...) go after the subcommand, e.g.
`python3 plex_history.py export --target plex --title "Coil Runner" --dry-run`.

A dry run never starts the login. With a Simkl token available it still checks what is already on
Simkl; without one it prints everything.

Requires Python 3.9+, no dependencies.

Tests: `python3 -m unittest discover -s tests -t .` (synthetic database, no network).

- Before posting, the user's Simkl library is read and movies/episodes already marked watched are
  skipped. For anime from TVDB-numbered sources the numbering can't be compared, so those are
  sent and Simkl's own duplicate check skips them. Rewatches are never sent, so reruns are safe.
  Simkl files anime films as anime entries of type `movie`; a film counts as watched when such
  an entry with its ids has episode 1 watched.
- Plex metadata lookups are cached in `plex_metadata_cache.json`.
- Simkl catalog lookups (id searches, episode lists, title searches) are cached in
  `simkl_catalog_cache.json` (`--simkl-cache`) and fetched again after `--simkl-cache-days`.
- TMDB responses for `--title` are cached in `tmdb_cache.json` (`--tmdb-cache`) and fetched
  again after `--tmdb-cache-days`, so new TMDB entries show up eventually. The API key is not
  stored in the cache.
- Items posted successfully are recorded per Simkl account in `simkl_sent.json` (`--sent-cache`)
  and skipped in later runs; `--resend` sends them again. "Successfully" means posted and not
  listed in Simkl's `not_found` response, since Simkl does not confirm individual episodes.
- Items Simkl could not match are recorded per account in `simkl_not_found.json` (`--not-found`),
  with a label and timestamp, and skipped for `--not-found-days` / `SIMKL_NOT_FOUND_DAYS`
  (default 30, `0` always retries them) or until `--retry-not-found` is passed.
- Both records are keyed by the Simkl account id, fetched once from `/users/settings` and saved
  to the env file as `SIMKL_ACCOUNT_ID` (again after `--simkl-login`). Dry runs use the records
  but never write them.
- Each target has its own record files: `simkl_sent.json` / `simkl_not_found.json` for Simkl,
  `plex_sent.json` / `plex_not_found.json` for `--target plex`. `--sent-cache` and `--not-found`
  override the paths.
- `export --forget REGEX` only removes sent / not-found records whose key (`anidb:44015
  S01E02`, `movie imdb:tt1156099`) or not-found label matches, for every account, and stops;
  with `--dry-run` it lists them. Sent records hold ids, not titles.
- After a run, export prints how many Simkl requests are left today (Simkl's
  `X-RateLimit-Remaining`; the allowance is per Simkl user and shared by all its apps).
- Free Simkl accounts allow 500 requests per day and 1 POST per second. Items are posted in
  batches of 100. If the daily quota runs out the script stops; rerun it later.

## Retries with fallbacks (Simkl)

Each movie and episode has an ordered list of ways to identify it. The first pass sends every
item's first way; items Simkl lists in `not_found` are sent again with their next way, pass after
pass (`retry 1`, `retry 2`, ...; per-item results at `-v`). The summary says how many were found
only by a fallback, and by which.

| Order | Items | Identified by |
|---|---|---|
| 1 | `plex://` episodes | the episode's TVDB episode id (from Plex) |
| 1/2 | all episodes | show ids + season/episode numbers (TVDB-style for TVDB-numbered sources) |
| next | legacy TVDB-numbered episodes (thetvdb, hama tvdb-) | AniDB id from `anime_ids.json` (TVDB show + season + offset) |
| next | TVDB-numbered episodes | the next TVDB season: an episode number past the end of a TVDB season (count from Simkl's catalog) continues in the next one. Plex, like TMDB, keeps later cours of anime in season 1 (Lone Ascent S01E13 is TVDB S02E01) |
| last | AniDB-numbered episodes past the end of their Simkl entry | first the number as an episode of the entry's own TVDB season, found by Simkl's TVDB numbers in that entry or another AniDB entry `anime_ids.json` maps to the same season: Orbital Knights 2015 episode 13 is TVDB S01E13, its own episode 1; Agent Household episode 13 is S01E13, episode 1 of the next cour (AniDB 58103); Last Stand (2022) episode 17 is S04E17. Otherwise the absolute (show-wide) number: HSA S2E25 stored as `anidb-43154` episode 38 (13 + 25), using the earlier TVDB seasons' sizes from Simkl; that result must be an episode of the same entry and pass the title or air-date check above |
| last | TVDB-numbered episodes (needs `PLEX_TOKEN`) | the current ids of the Plex show with the same title (and year, when known), from Plex's `matches?type=2&title=`. Agent guids can hold outdated show ids: The Night Ledger was played as `thetvdb://431235`, Iron Spire as `thetvdb://382752`; Plex and Simkl know them as TVDB 469816 and 393650. Lookups are cached in the Plex cache file and cost no Simkl requests |
| 1 | movies | ids (+ title/year from Plex) |
| next | movies without a Plex title | title and year from the database |
| next | movies with only an IMDb id | TMDB id, title and year (needs `TMDB_API_KEY`) |
| last | movies titled like a numbered part (`Title 4`, `Title Part 4 - Subtitle`) | episode N of a Simkl anime entry found by `/search/anime` that holds the film series as episodes (Lancer Saga: Ren the Exiled 4 is episode 4 of Simkl 63375). With a subtitle (from TMDB or Plex) the episode title must match it; without one the entry must be a movie or OVA |

Before sending, AniDB-numbered shows with episode numbers past the end of their Simkl entry
that absolute numbering does not explain are skipped as a whole, with a warning: HAMA
probably matched the files to the wrong AniDB entry (Witch Seven TV episodes 1-12 were stored
under the 2-episode OVA, so episodes 1-2 would land on the OVA), or Simkl splits the entry
differently (Ash Witch is one film on Simkl, three parts on AniDB). The check uses every
collected episode of a show, also those already on Simkl, and costs no daily quota.

The TVDB season, absolute number, TMDB and multi-part kinds are computed only when reached, as
they cost requests (Simkl id lookups and episode lists per show, a TMDB lookup per movie, a
Simkl search per multi-part title). A dry run lists how many fallbacks exist.

## Listing the database

`plex_history.py list` prints the watched items of `--plex-account-id` without contacting any
service: first watch date, orphaned or present, type, show or movie title, `SxxExx`, episode
title, guid, external ids, and a note when the item cannot be exported (`local`,
`show/season level`, `music`, `not in the Plex lookup cache`, ...). Titles and numbers come
from the database; `plex://` items are enriched from the Plex lookup cache
(`plex_metadata_cache.json`). `--lookup` (needs `PLEX_TOKEN`) fetches and caches the ones
missing from it.

- `--by show` collapses episodes into one row per show with episode ranges and counts.
- `--type movie|show|season|episode|artist|album|track|clip|unknown` (repeatable) keeps items
  of those types; `unknown` is rows without a type (e.g. `local://` items with no title). With
  `--by show` it applies before grouping, with `--plays` to the play rows.
- `--unmappable` keeps only items export cannot map to ids, with the reason in the note column
  (`local`, `no agent`, `anidb specials`, `show/season level`, ...). It combines with `--by show`
  and `--plays`. Without `--lookup`, `plex://` items missing from the Plex cache are listed too.
- `--plays` prints one row per play event instead, with the show guid, numbers and titles
  recorded at play time, and notes HAMA plays whose guid disagrees with them. Items that are
  only marked watched have no plays.
- `--format table|tsv|json` (table cells are cut at 60 characters).
- `--only` and all filters of `export` apply, e.g.
  `plex_history.py list --only orphaned --title-regex "help desk" --by show`.

## Plex target

`--target plex` (or `SYNC_TARGET=plex`) records the history on the Plex account instead of
Simkl, so it shows up in Plex's watch history even for items on no server. It needs only
`PLEX_TOKEN`. For each movie or episode:

1. The Plex item is found: shows and movies via
   `metadata.provider.plex.tv/library/metadata/matches?type=2|1&guid=tvdb://…` (tvdb, tmdb, imdb
   in that order), episodes via the show's seasons and their episodes. HAMA AniDB episodes are
   first mapped to a TVDB season and episode with Kometa's `anime_ids.json` (`tvdb_season`,
   `tvdb_epoffset`). AniDB specials are matched against the show's Plex season 0: by title
   (the play's or Simkl's), then by Simkl's air date for the special (a day either way, one
   match only), then by number when Plex and Simkl list as many specials. Simkl's catalog is
   used when `SIMKL_CLIENT_ID` is set (no token needed). TVDB special numbers from Anime-Lists
   are not used: Plex numbers season 0 its own way. Lookups are cached in the Plex cache file.
2. Items whose `userState` already shows them watched are left alone. A dry run reads it too
   and reports them as "already watched" instead of "would be marked".
3. The item is marked watched with `discover.provider.plex.tv/actions/scrobble` (what Plex Web's
   "Mark as Watched" does), which records the current time.
4. The new entry is read from the newest watch history entries
   (`community.plex.tv/api`, GraphQL `user.watchHistory`) and its date is set with the
   `updateActivity(id, {date})` mutation (what "Edit Watched Date" does), to the second.

Items are marked in batches of `--plex-batch` (default 20): each item is checked and marked
one by one, then one GraphQL query finds the batch's new history entries, and their dates are
set with aliased `updateActivity` mutations, three per request. community.plex.tv allows 6
`updateActivity` mutations (aliased ones included) per 60 seconds, and does not limit reads;
its `RATE_LIMITED` error always says `retryAfter: 60`. Date updates are paced to stay inside
that window, so a run marks about 6 items per minute without hitting the limit (the
wait-and-retry stays as a fallback, e.g. when another run used the same minute).

Interruptions (Ctrl-C, crashes) are safe to resume by running the same command again: each item
is recorded in `plex_sent.json` / `plex_not_found.json` as soon as it is done, and an item is
journaled in `plex_pending.json` (`--pending`) from just before it is marked until its date is
set. The next run first repairs journaled items: one that is not watched was never marked and
is handled normally; a marked one still carries the date of marking, so its entry is found
among the newest history entries and gets its original date. Items reported as "date not set"
are repaired the same way. GraphQL rate limits (`RATE_LIMITED`, `retryAfter`) are waited out.

Successes and items not found are recorded per Plex account uuid in `plex_sent.json` and
`plex_not_found.json`, separate from the Simkl records. A dry run looks up the Plex ids and lists what would be marked.

These Plex APIs are undocumented and may change. `userState.lastViewedAt` keeps the time of
the run; only the watch history entry carries the original date.

## Not yet verified

The Plex metadata provider, TMDB lookups and the Plex target have been run with real
tokens (the Plex target on a single episode). The Simkl device
login and the Simkl responses are taken from documentation and tested against mocked responses
only.

Plex re-creates episode ids when it rebuilds a season, so some old `plex://episode` ids return
HTTP 404. For those, the show guid and season/episode numbers recorded in the database (play
events, then library items) are used instead, and the show's ids are sent to Simkl. Episodes with
only watch-state rows have no such record and are skipped as "not found on plex metadata
provider", as are 404 movies.
