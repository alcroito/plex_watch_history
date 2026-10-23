# Examples

All commands go through `plex_history.py <subcommand> [options]`. Options go after the
subcommand. Settings not given on the command line come from the environment, then `.env`
(see README.md, Configuration). The examples assume a `.env` like:

```
PLEX_DB_PATH=com.plexapp.plugins.library.db
PLEX_TOKEN=...
TMDB_API_KEY=...
SIMKL_CLIENT_ID=...
```

`--help` works on every level: `plex_history.py --help`, `plex_history.py export --help`,
`plex_history.py plex-state mark --help`.

## mise tasks

`mise.toml` has tasks for common invocations (`mise tasks ls`); extra arguments are appended,
e.g. `mise run simkl -- --title-regex ren`.

```sh
mise run plex-orphaned-dry
mise run plex-orphaned
mise run simkl-quota
```

## list: look at the database (no network)

```sh
# Every watched item of the server owner, oldest first
python3 plex_history.py list

# Another database and another Plex user
python3 plex_history.py list --db other.db --plex-account-id 2

# Only items deleted from the library, grouped per show with episode ranges
python3 plex_history.py list --only orphaned --by show

# Only what is still in the library
python3 plex_history.py list --only present

# Title regex (case-insensitive) and guid regex
python3 plex_history.py list --title-regex "quiet hotel|help desk"
python3 plex_history.py list --guid-regex '^com\.plexapp\.agents\.hama://'
python3 plex_history.py list --guid-regex 'thetvdb://85625/'

# A show found via TMDB (ids, incl. AniDB through anime_ids.json); --year to disambiguate
python3 plex_history.py list --title "Coil Runner"
python3 plex_history.py list --title "The Quiet Hotel" --year 2016

# A window: items 100-149 by first watch date
python3 plex_history.py list --start 100 --count 50

# Machine readable output
python3 plex_history.py list --format tsv > history.tsv
python3 plex_history.py list --format json --by show | jq '.[] | select(.orphaned > 0) | .title'

# Fill missing plex:// details from Plex (needs PLEX_TOKEN; results are cached)
python3 plex_history.py list --lookup --only orphaned

# Only what export cannot map to ids, with the reason (skip the show/season markers)
python3 plex_history.py list --unmappable --by show | grep -v "show/season level"
python3 plex_history.py list --unmappable --format tsv | cut -f9 | sort | uniq -c

# By type (repeatable): unmappable episodes only, or movies and clips
python3 plex_history.py list --unmappable --type episode --by show
python3 plex_history.py list --type movie --type clip

# Every play event of the selected items, with the show guid and numbers recorded at play time
python3 plex_history.py list --plays --title-regex "hero school"

# Quiet (no progress lines on stderr)
python3 plex_history.py list -q --by show
```

## audit: HAMA data problems (no export)

```sh
# Mismatched plays, shows matched to the wrong AniDB entry, absolute episode numbers
python3 plex_history.py audit
python3 plex_history.py audit --db other.db --plex-account-id 2
python3 plex_history.py audit --only orphaned --title-regex "hero school"
```

## simkl-login: Simkl tokens

```sh
# Device login: open the printed link, approve; tokens and account id go to .env
python3 plex_history.py simkl-login

# New access token from the saved refresh token (no browser)
python3 plex_history.py simkl-login --refresh

# Using another env file
python3 plex_history.py simkl-login --env-file other.env
```

## export: record the history on Simkl (default target)

```sh
# Always start with a dry run: prints the batches (titles on stderr, JSON payloads on stdout)
python3 plex_history.py export --dry-run
python3 plex_history.py export --dry-run > payloads.json

# One show, real run
python3 plex_history.py export --title "Coil Runner" --guid-regex 'hama://'

# Only orphaned items, a few at a time
python3 plex_history.py export --only orphaned --count 100
python3 plex_history.py export --only orphaned --start 100 --count 100

# Verbose: per-batch results and skip reasons; -vv: every item and HTTP request
python3 plex_history.py export --only orphaned -v
python3 plex_history.py export --dry-run -vv --count 5

# Send again what an earlier run already sent, or retry what Simkl did not find
python3 plex_history.py export --resend --title "Coil Runner"
python3 plex_history.py export --retry-not-found
python3 plex_history.py export --not-found-days 0     # never skip earlier misses

# Drop sent / not-found records (keys like "anidb:44015 S01E02", not-found labels)
python3 plex_history.py export --forget "anidb:14011 |ash witch" --dry-run
python3 plex_history.py export --forget "anidb:14011 "

# Another database / user (use that person's Simkl login in its own env file)
python3 plex_history.py export --db other.db \
    --plex-account-id 2 --env-file other-user.env --dry-run

# Force a new Simkl login before exporting
python3 plex_history.py export --simkl-login --count 10
```

## export --target plex: record the history on the Plex account

```sh
# Dry run: shows the Plex ids it found and what it would mark
python3 plex_history.py export --target plex --title "Coil Runner" --guid-regex 'hama://' --dry-run

# Real run: marks each item watched on Plex and sets the original watch date
python3 plex_history.py export --target plex --title "Coil Runner" --guid-regex 'hama://'

# Same via environment
SYNC_TARGET=plex python3 plex_history.py export --only orphaned --count 20 -v

# Long runs: timestamps on every line; -vv also logs each step (watch state, mark, history
# query, date updates, rate-limit waits and retries)
python3 plex_history.py export --target plex --only orphaned --skip-synced -vv --timestamps
```

Records go to `plex_sent.json` / `plex_not_found.json` (Simkl: `simkl_sent.json` /
`simkl_not_found.json`).

## plex-state: inspect and change the Plex account directly

Nothing is changed unless `--write` is given.

```sh
# Find a show or movie id by external id
python3 plex_history.py plex-state match --tvdb 303415
python3 plex_history.py plex-state match --movie --imdb tt1156099

# Seasons and episodes of a show with watch state
python3 plex_history.py plex-state tree 5d9c00000000000000000001

# userState of items
python3 plex_history.py plex-state state 5d9c00000000000000000002 5d9c00000000000000000003

# Watch history: newest entries, or every entry of one item
python3 plex_history.py plex-state history --first 20
python3 plex_history.py plex-state history --item 5d9c00000000000000000002 --all

# Mark watched with a date / unmark and remove leftover history entries
python3 plex_history.py plex-state --write mark 5d9c00000000000000000002 --date 2015-01-15T20:30:45Z
python3 plex_history.py plex-state --write unmark 5d9c00000000000000000002 --remove-history

# Edit or delete a history entry by its activity id (from `history`)
python3 plex_history.py plex-state --write set-date 00000000-0000-4000-8000-000000000001 2015-01-15T20:30:45Z
python3 plex_history.py plex-state --write remove 00000000-0000-4000-8000-000000000001
```

## simkl-state: inspect and change Simkl directly

Needs `SIMKL_CLIENT_ID` and `SIMKL_ACCESS_TOKEN` (run `simkl-login`). Nothing is changed unless
`--write` is given.

```sh
# Simkl item for an external id (tmdb ids need --type)
python3 plex_history.py simkl-state lookup --anidb 36487
python3 plex_history.py simkl-state lookup --tmdb 85134 --type show

# Library entries; filter by id (several: any of them); show watched episodes with dates
python3 plex_history.py simkl-state items --type anime --status watching
python3 plex_history.py simkl-state items --id anidb=36487 --episodes
python3 plex_history.py simkl-state items --type anime --id anidb=43154 --id anidb=39511
python3 plex_history.py simkl-state items --type movies --status completed

# Catalog: episodes with air date, TVDB numbers and title; search by title
python3 plex_history.py simkl-state episodes --anidb 43154
python3 plex_history.py simkl-state episodes --simkl 63375 --specials
python3 plex_history.py simkl-state episodes --tvdb 85625
python3 plex_history.py simkl-state search "Lancer Saga Ren"
python3 plex_history.py simkl-state search "Some Title" --type all

# Add / remove watch events
python3 plex_history.py simkl-state --write mark --anidb 36487 --episode 2 3 --date 2016-02-01T20:00:00Z
python3 plex_history.py simkl-state --write mark --tvdb 85625 --season 2 --episode 1
python3 plex_history.py simkl-state --write mark --movie --imdb tt1156099 --date 2015-12-18T20:00:00Z

# Each episode with its own date
python3 plex_history.py simkl-state --write mark --simkl 846641 \
    --episode 1=2018-05-04T18:20:00Z 2=2018-05-05T18:20:00Z

# Requests left in today's quota
python3 plex_history.py simkl-state quota
python3 plex_history.py simkl-state --write unmark --anidb 36487 --episode 2 3

# Remove a whole entry from the library (history and list status)
python3 plex_history.py simkl-state --write unmark --simkl 950382 --all
```

## Tests

```sh
python3 -m unittest discover -s tests -t .
python3 -m unittest tests.test_list -v
```
