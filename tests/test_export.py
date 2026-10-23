import json
import time
import unittest
from unittest import mock

from tests.support import FakeHttp, TempDirTestCase, captured_log, make_db, pts


def episode(show, season, number, ids=None, label=None, numbering="tvdb", watched_at=100,
            title=None, year=None):
    return {"show": show, "ids": ids or {show.split(":")[0]: show.split(":")[1]},
            "season": season, "episode": number, "numbering": numbering,
            "watched_at": watched_at, "label": label, "title": title, "year": year}


def movie(ids, label=None, title=None, year=None, watched_at=100):
    return {"ids": ids, "label": label, "title": title, "year": year, "watched_at": watched_at}


class IdMergerTest(unittest.TestCase):
    def test_items_sharing_any_id_get_one_key(self):
        merger = pts.IdMerger()
        first = merger.key({"imdb": "tt1"})
        self.assertEqual(merger.key({"tmdb": "78", "imdb": "tt1"}), first)
        self.assertEqual(merger.key({"tmdb": "78"}), first)
        self.assertNotEqual(merger.key({"tmdb": "79"}), first)


class CollectTest(TempDirTestCase):
    def test_merges_guids_and_keeps_earliest_watch(self):
        self.patch_http(FakeHttp(plex={
            "e1": {"MediaContainer": {"Metadata": [{
                "type": "episode", "grandparentGuid": "plex://show/s1", "parentIndex": 1,
                "index": 2}]}},
            "s1": {"MediaContainer": {"Metadata": [{
                "type": "show", "title": "The Help Desk", "year": 2006,
                "Guid": [{"id": "tvdb://85625"}, {"id": "imdb://tt1706137"}]}]}},
            "m1": {"MediaContainer": {"Metadata": [{
                "type": "movie", "title": "Neon Harbor", "year": 1982,
                "Guid": [{"id": "imdb://tt1156099"}, {"id": "tmdb://78"}]}]}},
        }))
        watched = {
            "com.plexapp.agents.thetvdb://85625/1/2?lang=en": 50,
            "plex://episode/e1": 300,
            "com.plexapp.agents.thetvdb://85625/1/1?lang=en": 400,
            "com.plexapp.agents.imdb://tt1156099?lang=en": 500,
            "plex://movie/m1": 200,
            "local://1": 1,
            "com.plexapp.agents.thetvdb://85625?lang=en": 1,
            "plex://episode/nope": 1,
            "com.plexapp.agents.none://5": None,
        }
        titles = {"com.plexapp.agents.thetvdb://85625/1/1?lang=en": "The Help Desk (DB)"}
        skipped = {}
        movies, episodes = pts.collect(watched, pts.PlexResolver("t", "cache.json"), skipped, titles)

        self.assertEqual(len(movies), 1)
        (m,) = movies.values()
        self.assertEqual(m["ids"], {"imdb": "tt1156099", "tmdb": "78"})
        self.assertEqual((m["watched_at"], m["title"], m["year"]), (200, "Neon Harbor", 1982))

        self.assertEqual(sorted((e["season"], e["episode"]) for e in episodes.values()),
                         [(1, 1), (1, 2)])
        e12 = next(e for e in episodes.values() if e["episode"] == 2)
        self.assertEqual(e12["watched_at"], 50)
        self.assertEqual(e12["title"], "The Help Desk")
        # Ids are unified across all episodes of a show.
        e11 = next(e for e in episodes.values() if e["episode"] == 1)
        self.assertEqual(e11["ids"], {"tvdb": "85625", "imdb": "tt1706137"})
        self.assertEqual(e11["label"], "The Help Desk (DB)")

        self.assertEqual(skipped, {"local": 1, "show/season level": 1,
                                   "not found on plex metadata provider": 1, "no watch date": 1})

    def test_plex_items_skipped_without_token(self):
        skipped = {}
        movies, episodes = pts.collect({"plex://movie/m": 1, "plex://episode/e": 1}, None,
                                       skipped, {})
        self.assertEqual((movies, episodes), ({}, {}))
        self.assertEqual(skipped, {"plex:// without --plex-token": 2})


class FormattingTest(unittest.TestCase):
    def test_episode_ranges(self):
        self.assertEqual(pts.episode_ranges(1, [1]), "S01E01")
        self.assertEqual(pts.episode_ranges(2, [3, 1, 2, 5, 7, 8, 9]),
                         "S02E01-E03, S02E05, S02E07-E09")
        self.assertEqual(pts.episode_ranges(0, [101, 100]), "S00E100-E101")

    def test_batch_summary(self):
        units = [
            ("movie", movie({"imdb": "tt1"}, label="Neon Harbor", year=1982)),
            ("movie", movie({"anidb": 5})),
            ("episode", episode("tvdb:1", 1, 1, label="Show")),
            ("episode", episode("tvdb:1", 1, 2, label="Show")),
            ("episode", episode("tvdb:1", 2, 1, label="Show")),
            ("episode", episode("anidb:9", 1, 4)),
        ]
        self.assertEqual(pts.batch_summary(units), [
            "movie: Neon Harbor (1982)", "movie: anidb=5",
            "Show: S01E01-E02, S02E01", "anidb=9: S01E04"])

    def test_item_keys_and_label(self):
        e = episode("tvdb:1", 1, 3, ids={"tvdb": "1", "imdb": "tt2"}, label="Show")
        self.assertEqual(pts.item_keys("episode", e), {"tvdb:1 S01E03", "imdb:tt2 S01E03"})
        self.assertEqual(pts.item_label("episode", e), "Show S01E03")
        m = movie({"imdb": "tt1", "tmdb": "78"})
        self.assertEqual(pts.item_keys("movie", m), {"movie imdb:tt1", "movie tmdb:78"})
        self.assertEqual(pts.item_label("movie", m), "movie: imdb=tt1, tmdb=78")

    def test_iso(self):
        self.assertEqual(pts.iso(0), "1970-01-01T00:00:00Z")


def first(kind, rec):
    """A batch unit using the record's first variant."""
    variants = pts.movie_variants(rec) if kind == "movie" else pts.episode_variants(rec, {})
    return kind, rec, variants[0]


ANIME_IDS = {"28891": {"tvdb_id": 256864, "tvdb_season": 2, "tvdb_epoffset": 0},
             "12597": {"tvdb_id": 256864, "tvdb_season": 1, "tvdb_epoffset": 0},
             "500": {"tvdb_id": 7, "tvdb_season": 1, "tvdb_epoffset": 0},
             "501": {"tvdb_id": 7, "tvdb_season": 1, "tvdb_epoffset": 12}}


class VariantsTest(unittest.TestCase):
    def test_anidb_episode_picks_the_cour_by_offset(self):
        self.assertEqual(pts.anidb_episode(ANIME_IDS, "256864", 2, 5), (28891, 5))
        self.assertEqual(pts.anidb_episode(ANIME_IDS, 7, 1, 12), (500, 12))
        self.assertEqual(pts.anidb_episode(ANIME_IDS, 7, 1, 13), (501, 1))
        self.assertIsNone(pts.anidb_episode(ANIME_IDS, 7, 3, 1))
        self.assertIsNone(pts.anidb_episode(ANIME_IDS, 999, 1, 1))

    def test_plex_episode_tries_tvdb_episode_id_then_numbers(self):
        rec = episode("tvdb:778312", 1, 13, ids={"tvdb": "778312"}, title="Lone Ascent")
        rec["episode_tvdb"] = "30532897"
        variants = pts.episode_variants(rec, ANIME_IDS)
        self.assertEqual([v["name"] for v in variants],
                         ["TVDB episode id", "season/episode numbers"])
        self.assertEqual(variants[0]["episode"], {"ids": {"tvdb": 30532897}})
        self.assertTrue(variants[0]["item"]["use_tvdb_anime_seasons"])

    def test_legacy_tvdb_episode_falls_back_to_anidb(self):
        rec = episode("tvdb:256864", 2, 3, ids={"tvdb": "256864"})
        rec["legacy_tvdb"] = True
        variants = pts.episode_variants(rec, ANIME_IDS)
        self.assertEqual([v["name"] for v in variants],
                         ["season/episode numbers", "AniDB id from anime_ids.json"])
        self.assertEqual((variants[1]["item"], variants[1]["episode"]),
                         ({"ids": {"anidb": 28891}}, {"season": 1, "number": 3}))
        # Not mapped without the legacy flag (Plex numbering) or without a mapping entry.
        del rec["legacy_tvdb"]
        self.assertEqual(len(pts.episode_variants(rec, ANIME_IDS)), 1)

    def test_movie_without_title_falls_back_to_database_title(self):
        rec = movie({"imdb": "tt3280739"}, label="Lancer Saga Ren the Exiled 4")
        rec["db_year"] = 2015
        variants = pts.movie_variants(rec)
        self.assertEqual([v["item"] for v in variants], [
            {"ids": {"imdb": "tt3280739"}},
            {"ids": {"imdb": "tt3280739"}, "title": "Lancer Saga Ren the Exiled 4",
             "year": 2015}])
        self.assertEqual(len(pts.movie_variants(movie({"imdb": "tt1"}, title="T", year=1))), 1)



class FakeCatalog:
    def __init__(self, sizes):
        self.sizes, self.calls = sizes, []

    def tvdb_season_sizes(self, tvdb_id):
        self.calls.append(tvdb_id)
        return self.sizes.get(tvdb_id, {})


class FakeTmdb:
    def find_imdb(self, imdb_id):
        return {"movie_results": [{"id": 728812, "title": "Ren 4",
                                   "release_date": "2015-01-01"}]} if imdb_id == "tt3280739" else {}


class FakeTitleResolver:
    token = "tok"

    def match_show(self, title, year=None):
        return {"The Night Ledger": {"title": "The Night Ledger", "year": 2017,
                                          "ids": {"tvdb": "469816", "tmdb": "85310"}},
                "Iron Spire": {"title": "Iron Spire (2017)", "year": 2017,
                                "ids": {"tvdb": "393650"}}}.get(title)


class FakeMultipartCatalog:
    def __init__(self):
        self.calls = []

    def multipart_entry(self, title, part, subtitle=None):
        self.calls.append((title, part, subtitle))
        return (63375, "Lancer Saga: Boukyaku no Ren") if title == "Lancer Saga Ren" else None


class DeferredVariantsTest(unittest.TestCase):
    def test_season_overflow_moves_to_next_tvdb_season(self):
        catalog = FakeCatalog({"778312": {1: 12}})
        rec = episode("tvdb:778312", 1, 13, ids={"tvdb": "778312"}, title="Lone Ascent")
        variant = pts.season_overflow_variant(rec, catalog)
        self.assertEqual(variant["episode"], {"season": 2, "number": 1})
        self.assertTrue(variant["item"]["use_tvdb_anime_seasons"])
        self.assertIsNone(pts.season_overflow_variant(
            episode("tvdb:778312", 1, 12, ids={"tvdb": "778312"}), catalog))
        self.assertIsNone(pts.season_overflow_variant(
            episode("tvdb:1", 1, 30, ids={"tvdb": "1"}), catalog))

    def test_tmdb_title_variant(self):
        variant = pts.tmdb_title_variant(movie({"imdb": "tt3280739"}), FakeTmdb())
        self.assertEqual(variant["item"], {"ids": {"imdb": "tt3280739", "tmdb": "728812"},
                                           "title": "Ren 4", "year": 2015})
        self.assertIsNone(pts.tmdb_title_variant(movie({"imdb": "tt1"}), FakeTmdb()))

    def test_split_part(self):
        self.assertEqual(pts.split_part("Lancer Saga: Ren the Exiled 4: Ashes of Oath"),
                         ("Lancer Saga: Ren the Exiled", 4, "Ashes of Oath"))
        self.assertEqual(pts.split_part("Glass Garden Part 5 - Spiral Stair"),
                         ("Glass Garden", 5, "Spiral Stair"))
        self.assertEqual(pts.split_part("Tin Parade 3"), ("Tin Parade", 3, None))
        for title in ("2001: A Space Odyssey", "Neon Harbor 2049", "Movie", "4", None):
            self.assertIsNone(pts.split_part(title))

    def test_multipart_variant_sends_the_movie_as_an_episode(self):
        catalog = FakeMultipartCatalog()
        rec = movie({"imdb": "tt3280739"}, title="Lancer Saga Ren 4")
        variant = pts.multipart_variant(rec, catalog, FakeTmdb())
        self.assertEqual(variant, {"name": "part of a multi-part Simkl entry",
                                   "item": {"ids": {"simkl": 63375},
                                            "title": "Lancer Saga: Boukyaku no Ren"},
                                   "episode": {"season": 1, "number": 4}})
        self.assertEqual(catalog.calls, [("Ren", 4, None), ("Lancer Saga Ren", 4, None)])
        self.assertIsNone(pts.multipart_variant(movie({"imdb": "tt1"}, title="T"), catalog, None))

    def test_plex_title_variant_replaces_outdated_show_ids(self):
        rec = episode("tvdb:431235", 1, 3, ids={"tvdb": "431235"}, label="The Night Ledger")
        variant = pts.plex_title_variant(rec, FakeTitleResolver())
        self.assertEqual(variant, {"name": "show ids from Plex by title", "item": {
            "ids": {"tvdb": "469816", "tmdb": "85310"}, "title": "The Night Ledger",
            "use_tvdb_anime_seasons": True, "year": 2017}, "episode": {"season": 1, "number": 3}})
        rec["year"] = 2016
        self.assertIsNone(pts.plex_title_variant(rec, FakeTitleResolver()))  # other year
        same = episode("tvdb:469816", 1, 3, ids={"tvdb": "469816", "tmdb": "85310"},
                       label="The Night Ledger")
        self.assertIsNone(pts.plex_title_variant(same, FakeTitleResolver()))  # nothing new
        other = episode("tvdb:382752", 1, 1, ids={"tvdb": "382752"}, label="Iron Spire")
        self.assertIsNone(pts.plex_title_variant(other, FakeTitleResolver()))  # title differs
        self.assertIsNone(pts.plex_title_variant(episode("tvdb:1", 1, 1), FakeTitleResolver()))

    def test_plex_title_variant_needs_a_plex_token(self):
        rec = episode("tvdb:431235", 1, 3, ids={"tvdb": "431235"}, label="The Night Ledger")

        def names(resolver, record=rec):
            return [v["name"] for v in pts.build_units({}, {1: record}, resolver=resolver)[0][2]]
        self.assertEqual(names(FakeTitleResolver())[-1], "show ids from Plex by title")
        self.assertNotIn("show ids from Plex by title", names(pts.PlexResolver(None, "c.json")))
        anime = episode("anidb:1", 1, 1, ids={"anidb": 1}, numbering="anidb")
        self.assertNotIn("show ids from Plex by title", names(FakeTitleResolver(), anime))

    def test_deferred_variants_are_computed_only_when_reached(self):
        catalog = FakeCatalog({"778312": {1: 12}})
        rec = episode("tvdb:778312", 1, 13, ids={"tvdb": "778312"})
        units = pts.build_units({}, {"k": rec}, catalog=catalog)
        self.assertEqual([v["name"] for v in units[0][2]],
                         ["season/episode numbers", "TVDB season from Simkl's catalog"])
        ready, exhausted = pts.ready_units(units, 0)
        self.assertEqual((len(ready), exhausted, catalog.calls), (1, [], []))
        ready, exhausted = pts.ready_units(units, 1)
        self.assertEqual(ready[0][2][1]["episode"], {"season": 2, "number": 1})
        self.assertEqual(catalog.calls, ["778312"])

    def test_deferred_variant_without_result_is_dropped(self):
        units = pts.build_units({"m": movie({"imdb": "tt1"}, title="T", year=1)}, {},
                                tmdb=FakeTmdb())
        ready, exhausted = pts.ready_units(units, 1)
        self.assertEqual((ready, len(exhausted)), ([], 1))
        self.assertEqual(len(units[0][2]), 1)

HAMA = "com.plexapp.agents.hama://"
# Air dates in Simkl's format; the watch epochs below are a day later.
S2E25_AIRED, S3E13_AIRED = "2017-09-30T17:30:00+09:00", "2018-06-30T17:30:00+09:00"
DAY_AFTER_S2E25, DAY_AFTER_S3E13 = 1506760200 + 86400, 1530347400 + 86400


def anidb_entry(count, tvdb_season, dates=None, titles=None):
    return [{"episode": n, "title": (titles or {}).get(n, f"Title {n}"),
             "date": (dates or {}).get(n), "tvdb": {"season": tvdb_season, "episode": n}}
            for n in range(1, count + 1)]


class FakeEpisodeCatalog:
    """HSA: 39511 is TVDB season 1 (13 episodes), 43154 season 2, 45726 season 3."""

    def __init__(self):
        self.entries = {
            39511: anidb_entry(13, 1),
            43154: anidb_entry(25, 2, dates={25: S2E25_AIRED}),
            45726: anidb_entry(25, 3, dates={13: S3E13_AIRED},
                               titles={13: "Dorm Day"}),
        }

    def anidb_episodes(self, anidb_id):
        return self.entries.get(int(anidb_id), [])


HSA_IDS = {"39511": {"tvdb_id": 338023, "tvdb_season": 1, "tvdb_epoffset": 0},
           "43154": {"tvdb_id": 338023, "tvdb_season": 2, "tvdb_epoffset": 0},
           "44015": {"tvdb_id": 338023, "tvdb_season": 0, "tvdb_epoffset": 0}}


class MismatchedPlaysTest(TempDirTestCase):
    def test_load_mismatched_plays(self):
        make_db("x.db", items=[], settings=[], views=[
            (1, HAMA + "anidb-43154/1/2?lang=en", 4, "HSA", 1, 2, "Rumble", 100,
             HAMA + "anidb-43154?lang=en"),
            (1, HAMA + "anidb-43154/1/2?lang=en", 4, "HSA", 1, 13, "Dorm Day", 200,
             HAMA + "anidb-45726?lang=en"),
            (1, HAMA + "44015/1/2?lang=en", 4, "HSA", 1, 24, "", 300, HAMA + "45726?lang=en"),
            (1, HAMA + "tvdb-1/1/2?lang=en", 4, "Other", 1, 3, "", 400, HAMA + "tvdb-1?lang=en"),
            (2, HAMA + "anidb-1/1/2?lang=en", 4, "Other", 1, 3, "", 500, HAMA + "anidb-1?lang=en"),
        ])
        plays, consistent = pts.load_mismatched_plays("x.db", 1)
        self.assertEqual(sorted(plays), [HAMA + "44015/1/2?lang=en", HAMA + "anidb-43154/1/2?lang=en"])
        self.assertIsNone(pts.hama_mismatch(HAMA + "anidb-1/0/2", HAMA + "anidb-1", 2))
        self.assertFalse(pts.hama_mismatch(HAMA + "anidb-55401/3/1", HAMA + "anidb-55401", 1))
        self.assertTrue(pts.hama_mismatch(HAMA + "anidb-55401/3/1", HAMA + "anidb-55401", 2))
        self.assertEqual(plays[HAMA + "anidb-43154/1/2?lang=en"], [{
            "anidb": 43154, "episode": 2, "show_anidb": 45726, "index": 13, "show_title": "HSA",
            "title": "Dorm Day", "watched_at": 200}])
        self.assertEqual(consistent, {HAMA + "anidb-43154/1/2?lang=en": 100})

    def test_verify_by_title_or_air_date(self):
        catalog = FakeEpisodeCatalog()
        candidates = [(43154, 2), (45726, 13)]
        self.assertEqual(pts.verify_episode(candidates, "dorm day!", 0, catalog),
                         ((45726, 13), "episode title"))
        self.assertEqual(pts.verify_episode(candidates, "", DAY_AFTER_S3E13, catalog),
                         ((45726, 13), "air date"))
        self.assertEqual(pts.verify_episode(candidates, "Episode 13", DAY_AFTER_S3E13, catalog),
                         ((45726, 13), "air date"))
        found, reason = pts.verify_episode(candidates, "", DAY_AFTER_S3E13 + 30 * 86400, catalog)
        self.assertIsNone(found)
        found, reason = pts.verify_episode(candidates, "Unknown", DAY_AFTER_S3E13, catalog)
        self.assertEqual((found, reason), (None, "title 'Unknown' matches no episodes"))

    def test_title_and_air_date_must_agree(self):
        catalog = FakeEpisodeCatalog()
        found, reason = pts.verify_episode([(43154, 25), (45726, 13)], "Dorm Day",
                                           DAY_AFTER_S2E25, catalog)
        self.assertIsNone(found)
        self.assertIn("point to different episodes", reason)

    def test_resolve_adds_the_verified_episode(self):
        existing = episode("anidb:45726", 1, 1, ids={"anidb": 45726}, numbering="anidb",
                           title="HSA 3", year=2018)
        episodes = {("anidb:45726", 1, 1): existing}
        plays = [{"anidb": 43154, "episode": 2, "show_anidb": 45726, "index": 13,
                  "show_title": "HSA", "title": "", "watched_at": DAY_AFTER_S3E13},
                 {"anidb": 43154, "episode": 2, "show_anidb": 45726, "index": 13,
                  "show_title": "HSA", "title": "", "watched_at": 5}]
        added = pts.resolve_mismatched_plays(plays, episodes, FakeEpisodeCatalog())
        self.assertEqual(added, 1)
        rec = episodes[("anidb:45726", 1, 13)]
        self.assertIs(rec["ids"], existing["ids"])
        self.assertEqual((rec["title"], rec["watched_at"], rec["numbering"]),
                         ("HSA 3", DAY_AFTER_S3E13, "anidb"))


# Watch times: a day after Blob's special 1 aired, and long after everything.
BLOB_SPECIAL_AIRED, DAY_AFTER_BLOB_SPECIAL = "2019-03-26T00:00:00+09:00", 1553612400
SPECIALS = {
    # Heart Swap (9675): Simkl numbers its specials like AniDB.
    62855: [{"episode": n, "type": "special", "title": t, "date": "2012-11-19T00:00:00+09:00"}
            for n, t in ((1, "Days Apart"), (2, "Unseen"),
                         (3, "Resolve and Will"), (4, "Linked"))],
    # Blob (47334): AniDB's S24 "Special 24.5" is Simkl's special 1.
    1: [{"episode": 1, "type": "special", "title": "Tales: Dragon's Diary",
         "date": BLOB_SPECIAL_AIRED},
        {"episode": 2, "type": "special", "title": "Curtain Call",
         "date": "2018-09-17T00:00:00+09:00"}],
}


class FakeSpecialsCatalog:
    def entry(self, anidb):
        return {9675: ("anime", 62855), 47334: ("anime", 1)}.get(anidb)

    def episodes(self, kind, simkl_id, specials=False):
        regular = [{"episode": n, "type": "episode"} for n in (1, 2)]
        return regular + (SPECIALS[simkl_id] if specials else [])


class FakeSpecialsTarget:
    """Plex season 0 of Heart Swap (138426) and Blob (733336)."""

    def __init__(self):
        self.seasons = {
            "138426": [(1, "Daily Cracks", "2012-12-10"),
                       (2, "Blind Spot", "2012-12-10"),
                       (3, "Resolve and Will", "2012-12-10"),
                       (4, "Linked", "2012-12-10")],
            "733336": [(1, "Dragon's Diary", "2019-03-26"),
                       (2, "Extra: The Tragedy of M?", "2019-07-09")]}

    def season_episodes(self, tvdb_id, season):
        return self.seasons.get(str(tvdb_id)) if season == 0 else None


class SpecialsTest(unittest.TestCase):
    def special(self, anidb, number, title=None, watched_at=0):
        rec = episode(f"anidb:{anidb}", 0, number, ids={"anidb": anidb}, numbering="anidb",
                      watched_at=watched_at)
        rec["episode_title"] = title
        return rec

    def test_simkl_special_by_number_title_or_air_date(self):
        catalog = FakeSpecialsCatalog()
        special, how = pts.simkl_special(self.special(9675, 3), catalog)
        self.assertEqual((special["episode"], how), (3, "AniDB number"))
        special, how = pts.simkl_special(self.special(47334, 24, "Curtain Call"), catalog)
        self.assertEqual((special["episode"], how), (2, "episode title"))
        special, how = pts.simkl_special(
            self.special(47334, 24, "Special 24.5", DAY_AFTER_BLOB_SPECIAL), catalog)
        self.assertEqual((special["episode"], how), (1, "air date"))
        special, how = pts.simkl_special(self.special(47334, 24, "Special 24.5"), catalog)
        self.assertIsNone(special)
        self.assertIn("has no special 24", how)
        self.assertEqual(pts.simkl_special(self.special(999, 1), catalog), (None, None))

    def test_match_specials_keeps_the_anidb_number_on_the_record(self):
        episodes = {1: self.special(9675, 1),
                    2: self.special(47334, 24, "Special 24.5", DAY_AFTER_BLOB_SPECIAL),
                    3: self.special(47334, 30),
                    4: self.special(999, 1),  # unknown to Simkl: left for Simkl to report
                    5: episode("anidb:9675", 1, 9, ids={"anidb": 9675}, numbering="anidb")}
        unmatched = pts.match_specials(episodes, FakeSpecialsCatalog())
        self.assertEqual(list(unmatched), [3])
        self.assertEqual((episodes[2]["episode"], episodes[2]["simkl_episode"]), (24, 1))
        ((payload, _),) = list(pts.build_payloads(pts.build_units({}, {2: episodes[2]})))
        self.assertEqual(payload["shows"][0]["seasons"][0]["episodes"][0]["number"], 1)
        self.assertEqual(pts.item_keys("episode", episodes[2]), {"anidb:47334 S00E24"})

    def test_match_plex_special(self):
        plex = FakeSpecialsTarget().seasons
        heartswap, blob = SPECIALS[62855], SPECIALS[1]
        self.assertEqual(pts.match_plex_special(self.special(9675, 3), heartswap[2], 4,
                                                plex["138426"]), (3, "episode title"))
        self.assertEqual(pts.match_plex_special(self.special(9675, 1, "Kowareta Hibi"),
                                                heartswap[0], 4, plex["138426"]),
                         (1, "same numbering on Plex and Simkl"))
        self.assertEqual(pts.match_plex_special(self.special(47334, 24, "Special 24.5"),
                                                blob[0], 2, plex["733336"]), (1, "air date"))
        found, reason = pts.match_plex_special(self.special(9675, 1), heartswap[0], 3,
                                               plex["138426"])
        self.assertIsNone(found)
        self.assertIsNone(pts.match_plex_special(self.special(9675, 1), None, 0,
                                                 plex["138426"])[0])

    def test_plan_plex_specials(self):
        anime_ids = {"9675": {"tvdb_id": 138426, "tvdb_season": 1},
                     "47334": {"tvdb_id": 733336, "tvdb_season": 1},
                     "6269": {"tvdb_id": 87249, "tvdb_season": 2},
                     "1": {"tvdb_id": None}}
        episodes = {1: self.special(9675, 3), 2: self.special(6269, 6),
                    4: self.special(1, 1),
                    5: self.special(47334, 24, "Special 24.5", DAY_AFTER_BLOB_SPECIAL),
                    6: episode("anidb:9675", 1, 9, ids={"anidb": 9675}, numbering="anidb")}
        matched = pts.plan_plex_specials(episodes, FakeSpecialsTarget(), anime_ids,
                                         FakeSpecialsCatalog())
        self.assertEqual(matched, 2)
        self.assertEqual(episodes[1]["plex_special"], (138426, 0, 3))
        self.assertEqual(episodes[2]["special_reason"], "the show has no specials season on Plex")
        self.assertEqual(episodes[4]["special_reason"], "no TVDB show for the AniDB id")
        self.assertEqual(episodes[5]["plex_special"], (733336, 0, 1))
        self.assertNotIn("plex_special", episodes[6])


class AbsoluteNumberTest(unittest.TestCase):
    def rec(self, number, watched_at, title="Episode 38"):
        rec = episode("anidb:43154", 1, number, ids={"anidb": 43154}, numbering="anidb",
                      watched_at=watched_at)
        rec["episode_title"] = title
        return rec

    def test_absolute_number_maps_into_the_same_entry(self):
        variant = pts.absolute_number_variant(self.rec(38, DAY_AFTER_S2E25), HSA_IDS,
                                              FakeEpisodeCatalog())
        self.assertEqual(variant["name"], "absolute episode number")
        self.assertEqual(variant["episode"], {"season": 1, "number": 25})
        self.assertEqual(variant["item"]["ids"], {"anidb": 43154})

    def test_unverified_or_impossible_numbers_are_not_mapped(self):
        catalog = FakeEpisodeCatalog()
        for rec in (self.rec(38, DAY_AFTER_S2E25 + 60 * 86400),  # watched long after airing
                    self.rec(25, DAY_AFTER_S2E25),               # not past the entry's end
                    self.rec(39, DAY_AFTER_S2E25)):              # past the season too
            self.assertIsNone(pts.absolute_number_variant(rec, HSA_IDS, catalog))
        no_season_one = {"43154": HSA_IDS["43154"]}
        self.assertIsNone(pts.absolute_number_variant(self.rec(38, DAY_AFTER_S2E25),
                                                      no_season_one, catalog))

    def test_number_within_the_entrys_own_tvdb_season_needs_no_check(self):
        catalog = FakeEpisodeCatalog()
        catalog.entries[15247] = [{"episode": n, "title": "", "date": "2015-01-01T00:00:00+09:00",
                                   "tvdb": {"season": 1, "episode": n + 12}} for n in range(1, 13)]
        anime_ids = {"15247": {"tvdb_id": 201206, "tvdb_season": 1, "tvdb_epoffset": 12}}
        rec = episode("anidb:15247", 1, 13, ids={"anidb": 15247}, numbering="anidb",
                      watched_at=DAY_AFTER_S2E25)
        rec["episode_title"] = ""
        variant = pts.absolute_number_variant(rec, anime_ids, catalog)
        self.assertEqual(variant["episode"], {"season": 1, "number": 1})
        rec["episode"] = 25
        self.assertIsNone(pts.absolute_number_variant(rec, anime_ids, catalog))

    def test_number_of_the_next_cour_in_the_same_tvdb_season(self):
        # Agent Household: 56544 is TVDB S1E1-12, 58103 S1E13-25; Plex has 56544 episode 13.
        catalog = FakeEpisodeCatalog()
        catalog.entries[56544] = anidb_entry(12, 1)
        catalog.entries[58103] = [{"episode": n, "title": "", "date": None,
                                   "tvdb": {"season": 1, "episode": n + 12}} for n in range(1, 14)]
        anime_ids = {"56544": {"tvdb_id": 840291, "tvdb_season": 1, "tvdb_epoffset": 0},
                     "58103": {"tvdb_id": 840291, "tvdb_season": 1, "tvdb_epoffset": 12},
                     "62225": {"tvdb_id": 840291, "tvdb_season": 2, "tvdb_epoffset": 0}}
        rec = episode("anidb:56544", 1, 13, ids={"anidb": 56544}, numbering="anidb",
                      title="Agent Household", year=2022, watched_at=0)
        variant = pts.absolute_number_variant(rec, anime_ids, catalog)
        self.assertEqual(variant["item"], {"ids": {"anidb": 58103}})
        self.assertEqual(variant["episode"], {"season": 1, "number": 1})
        self.assertEqual(pts.misidentified_shows({1: rec}, anime_ids, catalog), {})

    def test_later_season_entry_uses_its_own_tvdb_numbers(self):
        # Last Stand part 2 (56239) is TVDB S4E17-28; S4E15 is in part 1 (50892).
        catalog = FakeEpisodeCatalog()
        catalog.entries[50892] = anidb_entry(16, 4)
        catalog.entries[56239] = [{"episode": n, "title": "", "date": None,
                                   "tvdb": {"season": 4, "episode": n + 16}} for n in range(1, 13)]
        anime_ids = {"50892": {"tvdb_id": 142352, "tvdb_season": 4, "tvdb_epoffset": 0},
                     "56239": {"tvdb_id": 142352, "tvdb_season": 4, "tvdb_epoffset": 16}}
        for number, expected in ((17, (56239, 1)), (28, (56239, 12)), (15, (50892, 15))):
            rec = episode("anidb:56239", 1, number, ids={"anidb": 56239}, numbering="anidb")
            anidb, target, summed = pts.absolute_target(rec, anime_ids, catalog)
            self.assertEqual((anidb, target["episode"], summed), expected + (False,))
        rec = episode("anidb:56239", 1, 31, ids={"anidb": 56239}, numbering="anidb")
        self.assertIsNone(pts.absolute_target(rec, anime_ids, catalog))

    def test_misidentified_shows(self):
        catalog = FakeEpisodeCatalog()
        catalog.entries[14011] = anidb_entry(2, 0)
        recs = [episode("anidb:14011", 1, n, ids={"anidb": 14011}, numbering="anidb")
                for n in (1, 2, 3, 4)]
        recs += [self.rec(38, DAY_AFTER_S2E25), self.rec(1, 0)]  # explained by absolute numbering
        recs.append(episode("anidb:777", 1, 99, ids={"anidb": 777}, numbering="anidb"))  # unknown
        recs.append(episode("tvdb:1", 1, 99))
        found = pts.misidentified_shows({i: r for i, r in enumerate(recs)}, HSA_IDS, catalog)
        self.assertEqual(found, {"anidb:14011": "episodes 3, 4 are past the end of its "
                                                "2-episode Simkl entry"})

    def test_variant_is_deferred_for_anidb_episodes(self):
        units = pts.build_units({}, {"k": self.rec(38, DAY_AFTER_S2E25)}, HSA_IDS,
                                FakeEpisodeCatalog())
        self.assertEqual([v["name"] for v in units[0][2]],
                         ["season/episode numbers", "absolute episode number"])
        self.assertIn("make", units[0][2][1])


class LocalEpisodesTest(TempDirTestCase):
    def test_load_local_episodes(self):
        make_db("x.db", items=[
            (1, "plex://show/s1", 2, "Present", None, None),
            (2, "plex://season/x", 3, "Season 2", 1, 2),
            (3, "local://20", 4, "Ep", 2, 5),
            (4, "local://show", 2, "Unmatched", None, None),
            (5, "local://season", 3, "Season 1", 4, 1),
            (6, "local://21", 4, "Ep", 5, 1)], settings=[], views=[
            (1, "local://44655", 4, "Iron Spire", 1, 1, "", 100,
             "com.plexapp.agents.thetvdb://382752?lang=en"),
            (1, "local://99", 4, "Guild Horizon", 1, 2, "", 100, "local://58998"),
            (1, "local://98", 4, "No numbers", None, None, "", 100,
             "com.plexapp.agents.thetvdb://1?lang=en")])
        self.assertEqual(pts.load_local_episodes("x.db"), {
            "local://44655": ("com.plexapp.agents.thetvdb://382752?lang=en", 1, 1),
            "local://20": ("plex://show/s1", 2, 5)})

    def test_episode_guid_of_show(self):
        self.assertEqual(
            pts.episode_guid_of_show("com.plexapp.agents.thetvdb://382752?lang=en", 2, 3),
            "com.plexapp.agents.thetvdb://382752/2/3?lang=en")
        self.assertEqual(pts.episode_guid_of_show("com.plexapp.agents.hama://anidb-48806", 1, 50),
                         "com.plexapp.agents.hama://anidb-48806/1/50")
        for guid in ("plex://show/1", "com.plexapp.agents.thetvdb://1/2?lang=en",
                     "com.plexapp.agents.imdb://tt1", "local://1"):
            self.assertIsNone(pts.episode_guid_of_show(guid, 1, 1))

    def test_map_local(self):
        local = {"local://1": ("com.plexapp.agents.thetvdb://382752?lang=en", 2, 3),
                 "local://2": ("com.plexapp.agents.hama://anidb-48806?lang=en", 1, 50),
                 "local://3": ("com.plexapp.agents.hama://anidb-48806?lang=en", 0, 1),
                 "local://4": ("plex://show/s1", 2, 5),
                 "local://5": ("com.plexapp.agents.none://5", 1, 1)}
        self.assertEqual(pts.map_local("local://1", local, None)[0],
                         ("episode", {"tvdb": "382752"}, 2, 3, "tvdb"))
        self.assertEqual(pts.map_local("local://2", local, None)[0],
                         ("episode", {"anidb": 48806}, 1, 50, "anidb"))
        self.assertEqual(pts.map_local("local://3", local, None)[0],
                         ("episode", {"anidb": 48806}, 0, 1, "anidb"))
        self.assertEqual(pts.map_local("local://4", local, None)[0][0], "skip")
        self.assertIsNone(pts.map_local("local://5", local, None))
        self.assertIsNone(pts.map_local("local://6", local, None))
        resolver = pts.PlexResolver(None, "cache.json")
        resolver.cache["s1"] = {"type": "show", "title": "Show", "year": 2020,
                                "ids": {"tvdb": "111"}, "show_guid": None, "season": None,
                                "episode": None}
        parsed, source = pts.map_local("local://4", local, resolver)
        self.assertEqual((parsed[:5], source),
                         (("episode", {"tvdb": "111"}, 2, 5, "tvdb"), "plex://show/s1"))

    def test_collect_marks_only_agent_tvdb_numbering_as_legacy(self):
        resolver = pts.PlexResolver(None, "cache.json")
        resolver.cache["s1"] = {"type": "show", "title": "Show", "year": 2020,
                                "ids": {"tvdb": "111"}, "show_guid": None, "season": None,
                                "episode": None}
        local = {"local://1": ("com.plexapp.agents.thetvdb://382752?lang=en", 2, 3),
                 "local://4": ("plex://show/s1", 2, 5)}
        counts = {}
        _, episodes = pts.collect({"local://1": 10, "local://4": 20}, resolver, {}, {},
                                  local_episodes=local, counts=counts)
        legacy = {e["ids"].get("tvdb"): e.get("legacy_tvdb", False) for e in episodes.values()}
        self.assertEqual(legacy, {"382752": True, "111": False})
        self.assertEqual(counts, {"local": 2})


class BuildPayloadsTest(unittest.TestCase):
    def test_payload_shape(self):
        movies = {"m": movie({"imdb": "tt1"}, title="Neon Harbor", year=1982, watched_at=0)}
        episodes = {
            ("tvdb:1", 1, 2): episode("tvdb:1", 1, 2, title="Show", year=2006, watched_at=60),
            ("tvdb:1", 1, 1): episode("tvdb:1", 1, 1, title="Show", year=2006, watched_at=0),
            ("anidb:9", 1, 4): episode("anidb:9", 1, 4, ids={"anidb": 9}, numbering="anidb"),
        }
        ((payload, batch),) = list(pts.build_payloads(pts.build_units(movies, episodes)))
        self.assertEqual(len(batch), 4)
        self.assertEqual(payload["movies"], [{"ids": {"imdb": "tt1"},
                                              "title": "Neon Harbor", "year": 1982,
                                              "watched_at": "1970-01-01T00:00:00Z"}])
        anime, show = payload["shows"]
        self.assertEqual(anime, {"ids": {"anidb": 9}, "seasons": [
            {"number": 1, "episodes": [{"number": 4, "watched_at": "1970-01-01T00:01:40Z"}]}]})
        self.assertEqual(show["ids"], {"tvdb": "1"})
        self.assertTrue(show["use_tvdb_anime_seasons"])
        self.assertEqual((show["title"], show["year"]), ("Show", 2006))
        self.assertEqual([e["number"] for e in show["seasons"][0]["episodes"]], [1, 2])

    def test_episode_ids_go_into_a_flat_episode_list(self):
        rec = episode("tvdb:778312", 1, 13, ids={"tvdb": "778312"}, watched_at=0)
        rec["episode_tvdb"] = "30532897"
        units = pts.build_units({}, {"k": rec})
        ((payload, _),) = list(pts.build_payloads(units))
        self.assertEqual(payload["shows"], [{
            "ids": {"tvdb": "778312"}, "use_tvdb_anime_seasons": True,
            "episodes": [{"ids": {"tvdb": 30532897}, "watched_at": "1970-01-01T00:00:00Z"}]}])
        ((payload, _),) = list(pts.build_payloads(units, attempt=1))
        self.assertEqual(payload["shows"][0]["seasons"],
                         [{"number": 1, "episodes": [{"number": 13,
                                                      "watched_at": "1970-01-01T00:00:00Z"}]}])

    def test_movie_sent_as_an_episode(self):
        rec = movie({"imdb": "tt3280739"}, title="Lancer Saga Ren 4", watched_at=0)
        variant = pts.multipart_variant(rec, FakeMultipartCatalog(), None)
        ((payload, _),) = list(pts.build_payloads([("movie", rec, [variant])]))
        self.assertEqual(payload["movies"], [])
        self.assertEqual(payload["shows"], [{
            "ids": {"simkl": 63375}, "title": "Lancer Saga: Boukyaku no Ren",
            "seasons": [{"number": 1, "episodes": [
                {"number": 4, "watched_at": "1970-01-01T00:00:00Z"}]}]}])
        result = {"not_found": {"shows": [{"ids": {"simkl": 63375}, "seasons": [
            {"number": 1, "episodes": [{"number": 4}]}]}]}}
        self.assertEqual(pts.failed_units([("movie", rec, variant)], result), {0})
        result = {"not_found": {"movies": [{"ids": {"simkl": 63375}}]}}
        self.assertEqual(pts.failed_units([("movie", rec, variant)], result), set())

    def test_batches_of_batch_size(self):
        episodes = {("tvdb:1", 1, n): episode("tvdb:1", 1, n) for n in range(1, 251)}
        batches = list(pts.build_payloads(pts.build_units({}, episodes)))
        self.assertEqual([len(b) for _, b in batches], [100, 100, 50])
        self.assertEqual(batches[2][0]["shows"][0]["seasons"][0]["episodes"][0]["number"], 201)


class FailedUnitsTest(unittest.TestCase):
    batch = [
        first("movie", movie({"imdb": "tt1"})),
        first("movie", movie({"imdb": "tt2"})),
        first("episode", episode("tvdb:1", 1, 1)),
        first("episode", episode("tvdb:1", 1, 2)),
        first("episode", episode("tvdb:1", 2, 1)),
        first("episode", episode("tvdb:5", 1, 1)),
    ]

    def test_movie_and_whole_show(self):
        result = {"not_found": {"movies": [{"ids": {"imdb": "tt2"}}],
                                "shows": [{"ids": {"tvdb": "5"}}], "episodes": []}}
        self.assertEqual(pts.failed_units(self.batch, result), {1, 5})

    def test_listed_seasons_and_episodes_only(self):
        result = {"not_found": {"shows": [{"ids": {"tvdb": "1"}, "seasons": [
            {"number": 1, "episodes": [{"number": 2}]}, {"number": 2}]}]}}
        self.assertEqual(pts.failed_units(self.batch, result), {3, 4})

    def test_top_level_episodes_mean_season_one(self):
        result = {"not_found": {"shows": [{"ids": {"tvdb": "1"}, "episodes": [{"number": 1}]}]}}
        self.assertEqual(pts.failed_units(self.batch, result), {2})

    def test_episodes_identified_by_id(self):
        rec = episode("tvdb:9", 1, 13, ids={"tvdb": "9"})
        rec["episode_tvdb"] = "555"
        other = episode("tvdb:9", 1, 14, ids={"tvdb": "9"})
        other["episode_tvdb"] = "556"
        batch = [first("episode", rec), first("episode", other)]
        result = {"not_found": {"shows": [{"ids": {"tvdb": "9"},
                                           "episodes": [{"ids": {"tvdb": 556}}]}]}}
        self.assertEqual(pts.failed_units(batch, result), {1})

    def test_unrelated_or_empty(self):
        self.assertEqual(pts.failed_units(self.batch, {}), set())
        result = {"not_found": {"shows": [{"ids": {"tvdb": "999"}}],
                                "movies": [{"ids": {"tvdb": "1"}}]}}
        self.assertEqual(pts.failed_units(self.batch, result), set())


class SyncStateTest(TempDirTestCase):
    batch = [("movie", movie({"imdb": "tt1"}, label="Neon Harbor")),
             ("episode", episode("tvdb:1", 1, 1, ids={"tvdb": "1", "imdb": "tt9"}, label="Show"))]

    def test_records_sent_and_not_found_per_account(self):
        state = pts.SyncState("sent.json", "nf.json", "777", 30)
        state.record(self.batch, {1})
        self.assertTrue(state.was_sent({"movie imdb:tt1"}))
        self.assertTrue(state.was_not_found({"imdb:tt9 S01E01"}))
        with open("nf.json") as f:
            self.assertEqual(json.load(f)["777"]["tvdb:1 S01E01"]["label"], "Show S01E01")

        reloaded = pts.SyncState("sent.json", "nf.json", "777", 30)
        self.assertTrue(reloaded.was_sent({"movie imdb:tt1"}))
        other = pts.SyncState("sent.json", "nf.json", "888", 30)
        self.assertFalse(other.was_sent({"movie imdb:tt1"}))
        self.assertFalse(other.was_not_found({"tvdb:1 S01E01"}))

    def test_success_clears_not_found(self):
        state = pts.SyncState("sent.json", "nf.json", "777", 30)
        state.record(self.batch, {0, 1})
        state.record(self.batch, set())
        self.assertFalse(state.was_not_found({"movie imdb:tt1"}))
        self.assertTrue(state.was_sent({"tvdb:1 S01E01"}))

    def test_not_found_expiry_and_disable(self):
        pts.SyncState("sent.json", "nf.json", "777", 30).record(self.batch, {0})
        with mock.patch.object(pts.time, "time", return_value=time.time() + 31 * 86400):
            expired = pts.SyncState("sent.json", "nf.json", "777", 30)
        self.assertFalse(expired.was_not_found({"movie imdb:tt1"}))
        self.assertTrue(pts.SyncState("sent.json", "nf.json", "777", 60)
                        .was_not_found({"movie imdb:tt1"}))
        self.assertFalse(pts.SyncState("sent.json", "nf.json", "777", 0)
                         .was_not_found({"movie imdb:tt1"}))

    def test_old_list_format_is_ignored(self):
        with open("nf.json", "w") as f:
            json.dump([{"type": "shows", "ids": {}}], f)
        state, log = captured_log(0, pts.SyncState, "sent.json", "nf.json", "777", 30)
        self.assertEqual(state.not_found, {})
        self.assertIn("not in the per-account format", log)


if __name__ == "__main__":
    unittest.main()
