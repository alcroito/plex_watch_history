import json
import os
import sqlite3
import unittest

from tests.support import FakeHttp, TempDirTestCase, captured_log, make_db, plex_item, pts


class ParseGuidTest(unittest.TestCase):
    def test_guids(self):
        cases = {
            "plex://episode/abc": ("plex", "episode", "abc"),
            "plex://movie/abc": ("plex", "movie", "abc"),
            "plex://show/abc": ("skip", "show/season level"),
            "plex://season/abc": ("skip", "show/season level"),
            "plex://track/abc": ("skip", "plex track"),
            "local://5938": ("skip", "local"),
            "com.plexapp.agents.thetvdb://85625/1/3?lang=en":
                ("episode", {"tvdb": "85625"}, 1, 3, "tvdb"),
            "com.plexapp.agents.thetvdb://85625/0/4?lang=en":
                ("episode", {"tvdb": "85625"}, 0, 4, "tvdb"),
            "com.plexapp.agents.thetvdb://85625/1?lang=en": ("skip", "show/season level"),
            "com.plexapp.agents.thetvdb://85625?lang=en": ("skip", "show/season level"),
            "com.plexapp.agents.themoviedb://78?lang=en": ("movie", {"tmdb": "78"}),
            "com.plexapp.agents.themoviedb://1399/2/5?lang=en":
                ("episode", {"tmdb": "1399"}, 2, 5, "tvdb"),
            "com.plexapp.agents.imdb://tt1156099?lang=en": ("movie", {"imdb": "tt1156099"}),
            "com.plexapp.agents.hama://anidb-13000/1/3?lang=en":
                ("episode", {"anidb": 13000}, 1, 3, "anidb"),
            "com.plexapp.agents.hama://20082/1/13?lang=en":
                ("episode", {"anidb": 20082}, 1, 13, "anidb"),
            # AniDB specials S1-S99 are season 0; 101+ are credits, trailers and extras.
            "com.plexapp.agents.hama://anidb-29378/0/11?lang=en":
                ("episode", {"anidb": 29378}, 0, 11, "anidb"),
            "com.plexapp.agents.hama://anidb-9243/0/101?lang=en":
                ("skip", "anidb credits and extras"),
            "com.plexapp.agents.hama://anidb-41550/0/501?lang=en":
                ("skip", "anidb credits and extras"),
            # The middle part is the TVDB season; the number is the entry's own episode.
            "com.plexapp.agents.hama://anidb-55401/3/1?lang=en":
                ("episode", {"anidb": 55401}, 1, 1, "anidb"),
            "com.plexapp.agents.hama://tvdb-733880/2/8?lang=en":
                ("episode", {"tvdb": "733880"}, 2, 8, "tvdb"),
            "com.plexapp.agents.hama://anidb-42928?lang=en": ("skip", "show/season level"),
            "com.plexapp.agents.hama://tmdb-1/1/1?lang=en": ("skip", "hama tmdb"),
            "com.plexapp.agents.lastfm://Artist/Album?lang=en": ("skip", "music"),
            "com.plexapp.agents.none://4922?lang=xn": ("skip", "no agent"),
            "iva://api.internetvideoarchive.com/2.0/DataService/1": ("skip", "iva"),
            "garbage": ("skip", "unparseable"),
        }
        for guid, expected in cases.items():
            with self.subTest(guid=guid):
                self.assertEqual(pts.parse_guid(guid), expected)


class DatabaseTest(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.db = make_db(self.path("library.db"))

    def test_load_watched_merges_views_and_settings_per_account(self):
        watched = pts.load_watched(self.db, 1)
        # Earliest play event wins over a later play and the settings row.
        self.assertEqual(watched["com.plexapp.agents.thetvdb://85625/1/1?lang=en"], 100)
        # Settings-only rows use last_viewed_at; unwatched (view_count 0) rows are left out.
        self.assertEqual(watched["plex://movie/mv1"], 1500)
        self.assertNotIn("plex://movie/in-progress", watched)
        # Other accounts' rows are excluded.
        self.assertNotIn("com.plexapp.agents.thetvdb://85625/1/3?lang=en", watched)
        self.assertEqual(len(watched), 15)

    def test_load_watched_skip_synced(self):
        db = sqlite3.connect(self.db)
        # One watch-state row was created on this server; the others came from plex.tv's sync.
        db.execute("update metadata_item_settings set created_at = 1 "
                   "where guid = 'plex://movie/mv1'")
        db.commit()
        db.close()
        path = self.db
        everything = pts.load_watched(path, 1)
        kept, log = captured_log(1, pts.load_watched, path, 1, None, True)
        self.assertIn("plex://movie/mv1", kept)
        self.assertNotIn("com.plexapp.agents.hama://36773/1/10?lang=en", kept)
        # Rows with plays keep their play date either way.
        self.assertEqual(kept["com.plexapp.agents.thetvdb://85625/1/1?lang=en"],
                         everything["com.plexapp.agents.thetvdb://85625/1/1?lang=en"])
        self.assertIn("watch-state rows synced from other servers left out", log)

    def test_load_watched_only_orphaned_or_present(self):
        orphaned = pts.load_watched(self.db, 1, "orphaned")
        present = pts.load_watched(self.db, 1, "present")
        self.assertEqual(set(present), {"plex://episode/ep-present"})
        self.assertNotIn("plex://episode/ep-present", orphaned)
        self.assertEqual(len(orphaned) + len(present), 15)

    def test_load_watched_logs_counts(self):
        _, log = captured_log(2, pts.load_watched, self.db, 1, "orphaned")
        self.assertIn("6 items with play events", log)
        self.assertIn("10 items marked watched", log)
        self.assertIn("14 orphaned (no longer in the library), 1 present", log)
        self.assertIn("keeping 14 orphaned items", log)

    def test_load_titles(self):
        titles = pts.load_titles(self.db)
        self.assertEqual(titles["com.plexapp.agents.thetvdb://85625/1/2?lang=en"], "The Help Desk")
        self.assertEqual(titles["com.plexapp.agents.imdb://tt1156099?lang=en"], "Neon Harbor")
        self.assertEqual(titles["plex://episode/ep-present"], "Present Show")
        self.assertEqual(titles["plex://season/season1"], "Present Show")
        self.assertEqual(titles["plex://show/show1"], "Present Show")
        self.assertNotIn("plex://movie/mv1", titles)

    def test_load_episode_fallbacks(self):
        self.assertEqual(pts.load_episode_fallbacks(self.db), {
            "ep-gone": ("quiethotel", 1, 3),
            "ep-present": ("show1", 1, 1),
        })

    def test_database_is_opened_read_only(self):
        pts.load_watched(self.db, 1)
        with self.assertRaises(Exception):
            db = pts.sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
            db.execute("delete from metadata_items")


class PlexResolverTest(TempDirTestCase):
    def resolver(self, fallbacks=None):
        return pts.PlexResolver("token", "cache.json", fallbacks)

    def test_match_show_by_title_cached(self):
        http = self.patch_http(FakeHttp([
            ("title=The+Night+Ledger", {"MediaContainer": {"Metadata": [{
                "title": "The Night Ledger", "year": 2017, "Guid": [
                    {"id": "imdb://tt5604999"}, {"id": "tvdb://469816"}, {"id": "x://1"}]}]}}),
            ("/matches?", {"MediaContainer": {}})]))
        resolver = self.resolver()
        expected = {"title": "The Night Ledger", "year": 2017,
                    "ids": {"imdb": "tt5604999", "tvdb": "469816"}}
        self.assertEqual(resolver.match_show("The Night Ledger"), expected)
        self.assertEqual(resolver.match_show("The Night Ledger"), expected)
        self.assertIsNone(resolver.match_show("Unknown", 2001))
        self.assertEqual(len(http.calls), 2)
        self.assertIn("year=2001", http.calls[1][0])
        self.assertIsNone(pts.PlexResolver(None, "other.json").match_show("Iron Spire"))

    def test_resolves_episode_through_its_show(self):
        http = self.patch_http(FakeHttp())
        result = self.resolver().resolve("episode", "ep-present")
        self.assertEqual(result, ("episode", {"tvdb": "111", "tmdb": "222"}, 1, 1, "tvdb",
                                  "Present Show", 2020, {}))
        self.assertEqual(http.calls[0][1], {
            "X-Plex-Token": "token", "Accept": "application/json",
            "X-Plex-Product": "plex-history-to-simkl",
            "X-Plex-Client-Identifier": "plex-history-to-simkl"})
        self.assertIn("includeGuids=1", http.calls[0][0])

    def test_resolves_movie(self):
        self.patch_http(FakeHttp())
        self.assertEqual(self.resolver().resolve("movie", "mv1"),
                         ("movie", {"imdb": "tt0000042", "tmdb": "42"}, "Some Movie", 2001))

    def test_movie_without_ids_and_episode_without_numbering_are_skipped(self):
        self.patch_http(FakeHttp(plex={
            "m": plex_item("movie", "No ids"),
            "e": plex_item("episode", "Loose"),
        }))
        resolver = self.resolver()
        self.assertEqual(resolver.resolve("movie", "m"), ("skip", "no external ids"))
        self.assertEqual(resolver.resolve("episode", "e"), ("skip", "episode without numbering"))

    def test_show_without_ids_is_skipped(self):
        self.patch_http(FakeHttp(plex={
            "e": plex_item("episode", "E", show="s", season=1, episode=1),
            "s": plex_item("show", "S"),
        }))
        self.assertEqual(self.resolver().resolve("episode", "e"),
                         ("skip", "show without external ids"))

    def test_cache_is_used_and_saved(self):
        http = self.patch_http(FakeHttp())
        resolver = self.resolver()
        resolver.resolve("episode", "ep-present")
        resolver.resolve("episode", "ep-present")
        self.assertEqual(len(http.calls), 2)  # episode and show, once each
        self.assertEqual(resolver.stats(), "2 fetched, 2 from cache")
        resolver.save()
        again = self.resolver()
        again.resolve("episode", "ep-present")
        self.assertEqual(len(http.calls), 2)
        self.assertEqual(again.stats(), "0 fetched, 2 from cache")

    def test_not_found_is_cached(self):
        http = self.patch_http(FakeHttp())
        resolver = self.resolver()
        self.assertEqual(resolver.resolve("episode", "missing"),
                         ("skip", "not found on plex metadata provider"))
        resolver.resolve("episode", "missing")
        self.assertEqual(len(http.calls), 1)
        self.assertIsNone(resolver.cache["missing"])

    def test_gone_episode_uses_database_fallback(self):
        self.patch_http(FakeHttp())
        resolver = self.resolver({"ep-gone": ("quiethotel", 1, 3)})
        result, log = captured_log(3, resolver.resolve, "episode", "ep-gone")
        self.assertEqual(result, ("episode",
                                  {"imdb": "tt2394302", "tmdb": "84481", "tvdb": "262699"},
                                  1, 3, "tvdb", "The Quiet Hotel", 2016, {}))
        self.assertEqual(resolver.recovered, {"ep-gone"})
        self.assertIn("plex://episode/ep-gone is gone from Plex, using show quiethotel S01E03", log)

    def test_fallback_not_used_for_movies(self):
        self.patch_http(FakeHttp())
        resolver = self.resolver({"gone-movie": ("show", 1, 1)})
        self.assertEqual(resolver.resolve("movie", "gone-movie"),
                         ("skip", "not found on plex metadata provider"))

    def test_saves_every_hundred_fetches(self):
        self.patch_http(FakeHttp(plex={}))
        resolver = self.resolver()
        for i in range(100):
            resolver.resolve("episode", f"e{i}")
        with open("cache.json") as f:
            self.assertEqual(len(json.load(f)), 100)

    def test_interrupted_run_resumes_from_cache(self):
        http = FakeHttp(plex={})
        self.patch_http(http)
        resolver = self.resolver()
        try:
            for i in range(10):
                if i == 6:
                    raise KeyboardInterrupt
                resolver.resolve("episode", f"e{i}")
        except KeyboardInterrupt:
            resolver.save()
        resumed = self.resolver()
        for i in range(10):
            resumed.resolve("episode", f"e{i}")
        self.assertEqual(resumed.stats(), "4 fetched, 6 from cache")
        self.assertFalse(os.path.exists("cache.json.tmp"))


if __name__ == "__main__":
    unittest.main()
