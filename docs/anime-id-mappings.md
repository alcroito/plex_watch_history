# Anime id mappings

Plex libraries using the HAMA agent store anime by AniDB id (`hama://anidb-<id>/<season>/<episode>`,
or `hama://<id>/...` in older versions). TMDB's `external_ids` endpoint returns only `imdb_id`,
`tvdb_id` (TV), `tvrage_id`, `wikidata_id`, Freebase and social ids, never AniDB or MyAnimeList
ids, so a separate mapping is needed to connect AniDB ids to TMDB/TVDB/IMDb.

This document summarises the mapping sources found while adding `--title` support, as checked on
2026-10-23.

## Sources

| Source | Format | Keyed by | Ids | Episode mapping | Updated |
|---|---|---|---|---|---|
| [Anime-Lists/anime-lists](https://github.com/Anime-Lists/anime-lists) | XML (`anime-list.xml`, `anime-list-full.xml`) | AniDB | TVDB, TMDB, IMDb | Yes: TVDB season, episode offset, per-episode overrides | Hand edits plus generated lists; 50 commits in the last 3 months |
| [Fribb/anime-lists](https://github.com/Fribb/anime-lists) | JSON (`anime-list-full.json`) | per entry | AniDB, TVDB, TMDB, IMDb, MAL, AniList, Kitsu, others | Series level only | Weekly automated rebuild of anime-offline-database and Anime-Lists; no hand edits |
| [Kometa-Team/Anime-IDs](https://github.com/Kometa-Team/Anime-IDs) | JSON (`anime_ids.json`, ~1.7 MB, ~17,000 entries) | AniDB | TVDB (+ `tvdb_season`, `tvdb_epoffset`), TMDB show/movie, IMDb, MAL, AniList | Season and offset per AniDB entry | Daily automated rebuild; 55 commits in the last 3 months |
| [nattadasu/animeApi](https://github.com/nattadasu/animeApi) | TSV (`database/animeapi.tsv` on branch `v3`, ~38,000 rows) | per title | AniDB, AniList, MAL, Kitsu, Simkl, Trakt, TMDB (+ season id, type), TVDB (+ season id), IMDb, Letterboxd, others | No | Several automated updates per day |
| [anibridge/anibridge-mappings](https://github.com/anibridge/anibridge-mappings) | JSON release assets (`mappings.json`, `mappings.min.json`, tag `v3`) | AniDB/AniList/MAL/TMDB/TVDB descriptors | AniDB, AniList, MAL, TMDB, TVDB, IMDb | Yes: episode ranges between providers, validated against metadata | Daily release; built from Anime-Lists, anime-offline-database, AnimeAggregations, shinkro-mapping, Wikidata (QLever), plus 4,000+ hand edits |
| Wikidata | SPARQL | item | AniDB anime id property (believed to be P5646, not verified) | No | Community edited; coverage is incomplete (e.g. TMDB entries without a Wikidata id) |

Anime-Lists is the hand-curated base most of the others build on. Fribb and Kometa are automated
repackagings of it combined with anime-offline-database.

## What other projects use

- [CrossWatch](https://github.com/cenodude/CrossWatch) (`cw_platform/anime_mapping/updater.py`):
  nattadasu/animeApi `animeapi.tsv` for title-level ids, and anibridge-mappings `mappings.min.json`
  for episode-level mapping, combined into a local SQLite index with update checks and user
  overrides.
- [Floppy](https://github.com/dannyvfilms/Floppy) (`src/integrations/anime_mapping.py`):
  Kometa-Team/Anime-IDs `anime_ids.json`, pinned to a fixed commit and checked against a SHA-256
  digest, indexed by MAL id to go from MAL to TMDB/TVDB.

## What this project uses

Kometa's `anime_ids.json`, for `--title` only: the tvdb/tmdb/imdb ids of the TMDB match are looked
up in it to find the AniDB ids, so HAMA AniDB guids match too. It is downloaded to `--anime-ids`
(default `anime_ids.json`) when missing; delete the file to refresh it.

Reasons:

- It is keyed by AniDB id, which is what HAMA guids contain.
- One small JSON file, no parsing of XML or TSV.
- It includes `tvdb_season` and `tvdb_epoffset`, enough to convert AniDB episode numbers to TVDB
  numbering if that is ever needed. Example check: an AniDB entry mapped to TVDB season 2 matched the
  `hama://tvdb-<id>/2/...` guids of the same show.

The exported payload itself is unaffected: Simkl accepts AniDB ids directly.

## When to switch

- anibridge-mappings, if episode numbering needs to be converted between providers where a simple
  season plus offset is not enough (split cours, recaps, specials).
- nattadasu/animeApi, if Simkl or Trakt ids per title become useful, e.g. to check the Simkl
  library by Simkl id.
