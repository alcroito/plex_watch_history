import contextlib
import io
import json
import os
import unittest

from tests.support import FakeHttp, TempDirTestCase, make_db, pts

# Plex lookup cache as PlexResolver writes it: ep-present, its show and one movie are cached;
# ep-unknown is not, ep-gone is cached as gone (404).
CACHE = {
    "ep-present": {"type": "episode", "title": "Pilot", "year": None, "ids": {},
                   "show_guid": "plex://show/show1", "season": 1, "episode": 1},
    "show1": {"type": "show", "title": "Present Show", "year": 2020,
              "ids": {"tvdb": "111", "tmdb": "222"}, "show_guid": None, "season": None,
              "episode": None},
    "mv1": {"type": "movie", "title": "Some Movie", "year": 2001,
            "ids": {"imdb": "tt0000042", "tmdb": "42"}, "show_guid": None, "season": None,
            "episode": None},
    "ep-gone": None,
}


class ListTest(TempDirTestCase):
    def setUp(self):
        super().setUp()
        make_db(self.path("com.plexapp.plugins.library.db"))
        with open("plex_metadata_cache.json", "w") as f:
            json.dump(CACHE, f)
        self.http = self.patch_http(FakeHttp())

    def rows(self, *argv):
        out, _ = self.run_main("--format", "json", *argv, command="list")
        return json.loads(out)

    def by_guid(self, rows):
        return {r["guid"]: r for r in rows}

    def test_items_with_details_and_no_network(self):
        rows = self.rows()
        self.assertEqual(self.http.calls, [])
        self.assertEqual(len(rows), 15)
        self.assertEqual([r["watched"] for r in rows], sorted(r["watched"] for r in rows))
        r = self.by_guid(rows)
        self.assertEqual(r["com.plexapp.agents.thetvdb://85625/1/1?lang=en"], {
            "watched": "1970-01-01", "state": "orphaned", "type": "episode",
            "title": "The Help Desk", "episode": "S01E01", "episode_title": "Monday Backlog",
            "guid": "com.plexapp.agents.thetvdb://85625/1/1?lang=en", "ids": "tvdb=85625",
            "note": ""})
        present = r["plex://episode/ep-present"]
        self.assertEqual((present["state"], present["title"], present["episode"],
                          present["episode_title"], present["ids"]),
                         ("present", "Present Show", "S01E01", "Pilot", "tvdb=111, tmdb=222"))
        movie = r["plex://movie/mv1"]
        self.assertEqual((movie["type"], movie["title"], movie["ids"]),
                         ("movie", "Some Movie", "imdb=tt0000042, tmdb=42"))
        self.assertEqual(r["plex://episode/ep-unknown"]["note"], "not in the Plex lookup cache")
        # local:// with an agent show guid in its play row is identified through the show.
        self.assertEqual(r["local://5938"]["note"], "")
        self.assertEqual(r["local://5938"]["ids"], "tvdb=431235")
        self.assertEqual(r["local://5938"]["episode"], "S01E01")
        self.assertEqual(r["local://5938"]["title"], "The Night Ledger")
        self.assertEqual(r["com.plexapp.agents.hama://anidb-53101/0/101?lang=en"]["note"],
                         "anidb credits and extras")
        self.assertEqual(r["com.plexapp.agents.lastfm://Artist/Album/2?lang=en"]["note"], "music")
        level = r["com.plexapp.agents.thetvdb://85625?lang=en"]
        self.assertEqual((level["type"], level["note"]), ("show", "show/season level"))
        self.assertEqual(pts.guid_level("com.plexapp.agents.hama://anidb-1/2?lang=en"), "season")

    def test_gone_episode_uses_database_fallback_from_cache(self):
        CACHE_WITH_SHOW = dict(CACHE, quiethotel={
            "type": "show", "title": "The Quiet Hotel", "year": 2016, "ids": {"tvdb": "262699"},
            "show_guid": None, "season": None, "episode": None})
        with open("plex_metadata_cache.json", "w") as f:
            json.dump(CACHE_WITH_SHOW, f)
        row = self.by_guid(self.rows())["plex://episode/ep-gone"]
        self.assertEqual((row["title"], row["episode"], row["ids"], row["note"]),
                         ("The Quiet Hotel", "S01E03", "tvdb=262699", ""))

    def test_filters_apply(self):
        rows = self.rows("--guid-regex", "thetvdb://85625/1/")
        self.assertEqual([r["episode"] for r in rows], ["S01E01", "S01E02"])
        self.assertEqual([r["guid"] for r in self.rows("--only", "present")],
                         ["plex://episode/ep-present"])
        self.assertEqual(len(self.rows("--start", "2", "--count", "3")), 3)
        titles = {r["title"] for r in self.rows("--title-regex", "some movie|ledger")}
        self.assertEqual(titles, {"Some Movie", "The Night Ledger"})
        self.assertEqual(self.http.calls, [])

    def test_plays(self):
        rows = self.rows("--plays", "--guid-regex", "85625/1/1")
        self.assertEqual([(r["watched"], r["episode"], r["episode_title"]) for r in rows], [
            ("1970-01-01T00:01:40Z", "S01E01", "Monday Backlog"),
            ("1970-01-01T00:05:00Z", "S01E01", "Monday Backlog")])
        self.assertEqual(set(rows[0]), set(pts.PLAY_COLUMNS))

    def test_unmappable(self):
        rows = self.by_guid(self.rows("--unmappable"))
        self.assertEqual({g: r["note"] for g, r in rows.items()}, {
            "com.plexapp.agents.thetvdb://85625?lang=en": "show/season level",
            "com.plexapp.agents.hama://anidb-53101/0/101?lang=en": "anidb credits and extras",
            "com.plexapp.agents.lastfm://Artist/Album/2?lang=en": "music",
            "com.plexapp.agents.none://4922?lang=xn": "no agent",
            # Gone from Plex; its show (via the database fallback) is not cached either.
            "plex://episode/ep-gone": "not in the Plex lookup cache",
            "plex://episode/ep-unknown": "not in the Plex lookup cache"})
        shows = self.rows("--unmappable", "--by", "show")
        self.assertEqual(sum(r["items"] for r in shows), 6)

    def test_type_filter(self):
        rows = self.rows("--unmappable", "--type", "show", "--type", "track")
        self.assertEqual({r["guid"]: r["type"] for r in rows},
                         {"com.plexapp.agents.thetvdb://85625?lang=en": "show"})
        self.assertEqual({r["type"] for r in self.rows("--type", "movie")}, {"movie"})
        unknown = self.rows("--type", "unknown")
        self.assertTrue(unknown and all(r["type"] == "" for r in unknown))
        plays = self.rows("--plays", "--type", "movie")
        self.assertEqual([r["title"] for r in plays], ["Neon Harbor"])
        with self.assertRaises(SystemExit):
            self.rows("--type", "song")

    def test_unmappable_plays_carry_the_reason(self):
        hama = "com.plexapp.agents.hama://"
        os.remove(self.path("com.plexapp.plugins.library.db"))
        make_db(self.path("com.plexapp.plugins.library.db"), items=[], settings=[], views=[
            (1, "local://5", 4, "Unmatched", 1, 1, "", 100, "local://4"),
            (1, "local://6", 4, "Iron Spire", 1, 1, "", 200,
             "com.plexapp.agents.thetvdb://382752?lang=en"),
            (1, hama + "anidb-1/0/151?lang=en", 4, "Ending", 0, 151, "", 300, hama + "anidb-1")])
        rows = self.rows("--unmappable", "--plays")
        self.assertEqual([(r["guid"], r["note"]) for r in rows], [
            ("local://5", "local"), (hama + "anidb-1/0/151?lang=en", "anidb credits and extras")])

    def test_plays_flag_hama_mismatches(self):
        hama = "com.plexapp.agents.hama://"
        os.remove(self.path("com.plexapp.plugins.library.db"))
        make_db(self.path("com.plexapp.plugins.library.db"), items=[], settings=[], views=[
            (1, hama + "anidb-43154/1/2?lang=en", 4, "HSA", 1, 13, "Dorm Day", 200,
             hama + "anidb-45726?lang=en"),
            (1, hama + "anidb-43154/1/3?lang=en", 4, "HSA", 1, 3, "", 300,
             hama + "anidb-43154?lang=en")])
        rows = self.rows("--plays")
        self.assertEqual([r["note"] for r in rows],
                         ["guid disagrees with the show guid or index", ""])
        self.assertEqual(rows[0]["show_guid"], hama + "anidb-45726?lang=en")

    def test_by_show(self):
        out, _ = self.run_main("--format", "json", "--by", "show", command="list")
        rows = {r["title"]: r for r in json.loads(out)}
        desk = rows["The Help Desk"]
        self.assertEqual((desk["type"], desk["items"], desk["orphaned"], desk["episodes"],
                          desk["first_watched"]), ("show", 2, 2, "S01E01-E02", "1970-01-01"))
        self.assertEqual(rows["Some Movie"]["type"], "movie")
        self.assertEqual(rows["Present Show"]["orphaned"], 0)

    def test_table_and_tsv(self):
        out, _ = self.run_main("--guid-regex", "thetvdb://85625/1/1", command="list")
        header, row = out.splitlines()
        self.assertEqual(header.split(), list(pts.ITEM_COLUMNS))
        self.assertIn("The Help Desk  S01E01   Monday Backlog", row)
        out, _ = self.run_main("--format", "tsv", "--guid-regex", "thetvdb://85625/1/1",
                               command="list")
        self.assertEqual(out.splitlines()[1].split("\t")[:5],
                         ["1970-01-01", "orphaned", "episode", "The Help Desk", "S01E01"])

    def test_table_truncates_long_cells(self):
        long_title = "x" * 100
        rows = [{c: "" for c in pts.ITEM_COLUMNS} | {"title": long_title, "guid": "g" * 80}]
        out = self.capture(pts.print_rows, rows, pts.ITEM_COLUMNS, "table")
        self.assertIn("x" * 57 + "...", out)
        self.assertIn("g" * 80, out)

    def capture(self, fn, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn(*args)
        return buf.getvalue()

    def test_lookup_fetches_and_caches_missing_items(self):
        self.http.plex["ep-unknown"] = {"MediaContainer": {"Metadata": [{
            "type": "episode", "title": "Found", "grandparentGuid": "plex://show/show1",
            "parentIndex": 2, "index": 5}]}}
        with self.assertRaises(SystemExit):
            self.run_main("--lookup", command="list")
        rows = self.rows("--lookup", "--plex-token", "t")
        row = self.by_guid(rows)["plex://episode/ep-unknown"]
        self.assertEqual((row["title"], row["episode"], row["episode_title"]),
                         ("Present Show", "S02E05", "Found"))
        with open("plex_metadata_cache.json") as f:
            self.assertIn("ep-unknown", json.load(f))
        self.assertTrue(os.path.exists("plex_metadata_cache.json"))


if __name__ == "__main__":
    unittest.main()
