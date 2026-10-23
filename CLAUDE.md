# CLAUDE.md

Exports Plex watch history (including items deleted from the server) from a PMS SQLite database
to Simkl or the Plex account. Entry point `plex_history.py` with subcommands; code in the
`plexhistory/` package:

- `common.py`: logging (`VERBOSITY`), env files, atomic writes, `http_json` / `http_location` /
  `post_form`, Simkl's daily quota (`RATE_LIMIT`).
- `plexdb.py`: database readers and `parse_guid`.
- `plex.py`: `PlexResolver` (metadata lookups) and `PlexTarget` (account watch state).
- `tmdb.py`: TMDB client and Kometa's AniDB mapping.
- `filters.py`: `select` (guid/title filters, start/count).
- `simkl.py`: Simkl auth, library, history and the cached catalog (`SimklCatalog`).
- `export.py`: records, payloads, sent/not-found state and the `export` command.
- `plex_state.py`: the `plex-state` command.
- `simkl_state.py`: the `simkl-state` command (lookup, items, episodes, search, quota,
  mark/unmark; changes need `--write`).
- `listing.py`: the `list` command (offline unless `--lookup`; `--plays` lists play events).
- `audit.py`: the `audit` command (HAMA data problems, checked against Simkl's catalog).
- `cli.py`: argument parsing, settings resolution, dispatch.

Modules call `common.http_json`, `common.http_location`, `common.post_form` and
`common.VERBOSITY` through the module so
tests can replace them in one place.

See README.md for behavior and setup, README.examples.md for invocations, and
`docs/project-notes.md` for everything learned so far (database layout, API behaviour, account
state, open ideas).

## Constraints

- Python standard library only. Do not add dependencies.
- Open the Plex database read-only (`mode=ro`). The `*.db` files are personal data and must never
  be committed.
- Never send `allow_rewatch=yes` to Simkl; reruns rely on Simkl ignoring duplicate watches.
- Respect Simkl limits: 1 POST/sec, batched writes, 500 requests/day on free accounts. Do not poll
  `/sync/all-items` repeatedly; Simkl suspends client ids that do.
- Simkl numbers anime episodes AniDB-style; TVDB-numbered sources send
  `use_tvdb_anime_seasons: true` and are not compared against Simkl anime episode lists.

## Data notes

- `metadata_item_views` (play events, with titles) and `metadata_item_settings` (watch state, no
  titles) are keyed by `guid` + `account_id`, not `metadata_items.id`. Orphaned rows have no
  matching `metadata_items.guid`.
- `local://<n>` guids carry no external ids; `<n>` was the old `metadata_items.id`. For episodes
  the play row's `grandparent_guid` (show) and numbers often identify them; export uses that.
- Legacy HAMA guids with a bare number (`hama://20082/1/13`) are AniDB ids. In
  `hama://anidb-<id>/<s>/<e>`, `<s>` is the TVDB season (0 = specials) and `<e>` the AniDB entry's
  own episode number. Specials 1-99 go to Simkl as season 0 of the entry; 101+ are credits/extras.
- HAMA play guids can be wrong (another entry's episode, a show-wide number, or a whole show
  under the wrong AniDB entry); export checks them against Simkl's catalog by episode title,
  air date or episode count and skips what it can't confirm.

## Docs

- Simkl API: https://api.simkl.org/llms.txt (index), `/api-reference/simkl/add-to-history.md`,
  `/api-reference/simkl/get-all-items.md`, `/resources/rate-limits.md`, `/api-reference/oauth2-device.md`
- Plex Media Server API: https://developer.plex.tv/pms/

## Testing

Run `python3 -m unittest discover -s tests -t .` after every change. Tests use only the standard
library, build a synthetic Plex database (`tests/support.py`) and replace `http_json` /
`post_form` with fakes, so they never touch the real databases or network. Add or update tests
with each change. A manual check against a real database is
`python3 plex_history.py export --db <db> --dry-run`.

## Plex target

Plex APIs used by `--target plex` are undocumented: `metadata.provider.plex.tv` (`matches`,
`children`; paging only via `X-Plex-Container-Start/Size` headers, max 300, query-string paging
is blocked by Cloudflare), `discover.provider.plex.tv` (`userState`, `actions/scrobble`), and
`community.plex.tv/api` GraphQL (no introspection; `user(id: uuid).watchHistory`,
`updateActivity(id, input: {date})`, `removeActivity(input: {id, type: WATCH_HISTORY})`).
Never write to a real account without the user's permission.
Findings are in `docs/plex-watch-state-api.md`. For manual experiments use
`plex_history.py plex-state` (`match`, `tree`, `state`, `history`, `mark`, `unmark`, `set-date`,
`remove`); it changes nothing without `--write`. After an experiment, check `history --item <id>
--all`: `unscrobble` does not always remove history entries.

## Auth

Simkl uses AUTH V2 with an app registered as "TV, devices & command line" (no redirect URI, no
secret). Tokens need the `media:write` scope. One grant allows one live access token, so only one
process should refresh it at a time.
