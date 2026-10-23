import os
import unittest

from tests.support import FakeHttp, TempDirTestCase, common

ENTRY = {"status": "watching", "last_watched_at": "2015-01-15T20:30:45Z",
         "watched_episodes_count": 1, "total_episodes_count": 12,
         "show": {"title": "Coil Runner", "year": 2015,
                  "ids": {"simkl": 901120, "anidb": "36487", "tvdb": "303415"}},
         "seasons": [{"number": 1, "episodes": [{"number": 1,
                                                 "watched_at": "2015-01-15T20:30:45Z"}]}]}
OTHER = {"status": "watching", "show": {"title": "Other", "ids": {"anidb": "1"}}}


class SimklStateTest(TempDirTestCase):
    def setUp(self):
        super().setUp()
        with open(".env", "w") as f:
            f.write("SIMKL_CLIENT_ID=cid\nSIMKL_ACCESS_TOKEN=tok\n")
        self.posted = []
        self.http = self.patch_http(FakeHttp([
            ("/search/id", [{"type": "anime", "title": "Coil Runner", "year": 2015,
                             "ids": {"simkl": 901120}}]),
            ("/search/anime", [{"title": "Ren", "year": 2012, "endpoint_type": "anime",
                                "type": "movie", "ep_count": 5,
                                "ids": {"simkl_id": 63375, "slug": "ren", "tmdbtv": "1"}}]),
            ("/redirect", "https://simkl.com/anime/901120/coil-runner?client_id=cid"),
            ("/anime/episodes/", [
                {"episode": 1, "type": "episode", "title": "Collapse", "date":
                 "2016-01-10T00:00:00+09:00", "tvdb": {"season": 1, "episode": 1}},
                {"episode": 1, "type": "special", "title": "Recap", "date": None}]),
            ("/sync/all-items/anime/", {"anime": [ENTRY, OTHER]}),
            ("/sync/all-items/", {}),
            ("/sync/history/remove", self.post),
            ("/sync/history", self.post),
        ]))

    def post(self, url, headers, data):
        self.posted.append((url.split("?")[0].rpartition("/sync")[2], data))
        return {"added": {"episodes": 2}, "not_found": {"shows": []}}

    def run_state(self, *argv):
        out, _ = self.run_main(*argv, command="simkl-state")
        return out

    def test_needs_tokens(self):
        os.remove(".env")
        with self.assertRaises(SystemExit):
            self.run_state("items")

    def test_lookup(self):
        out = self.run_state("lookup", "--anidb", "36487")
        self.assertEqual(out.strip(), "anime: Coil Runner (2015) [simkl=901120]")
        self.assertIn("anidb=36487", self.http.urls("/search/id")[0])
        self.assertEqual(self.http.calls[0][1]["Authorization"], "Bearer tok")
        with self.assertRaises(SystemExit):
            self.run_state("lookup")

    def test_items_filter_and_episodes(self):
        out = self.run_state("items", "--type", "anime", "--id", "anidb=36487", "--episodes")
        lines = out.splitlines()
        self.assertIn("Coil Runner (2015)", lines[0])
        self.assertIn("1/12 episodes", lines[0])
        self.assertEqual(lines[1].split(), ["S01E01", "2015-01-15T20:30:45Z"])
        self.assertEqual(len(lines), 2)
        self.assertIn("extended=full", self.http.urls("/sync/all-items/anime/all")[0])
        out = self.run_state("items")
        self.assertEqual(len(out.splitlines()), 2)
        self.assertEqual(len(self.http.urls("/sync/all-items/")), 4)

    def test_items_with_any_of_several_ids(self):
        out = self.run_state("items", "--type", "anime", "--id", "anidb=36487", "--id", "anidb=1")
        self.assertEqual(len(out.splitlines()), 2)
        out = self.run_state("items", "--type", "anime", "--id", "anidb=2")
        self.assertEqual(out, "")

    def test_episodes(self):
        out = self.run_state("episodes", "--anidb", "36487")
        self.assertEqual(out.split(), ["E01", "2016-01-10", "TVDB", "S01E01", "Collapse"])
        self.assertIn("anidb=36487", self.http.urls("/redirect")[0])
        url, headers, _ = self.http.calls[-1]
        self.assertIn("/anime/episodes/901120", url)
        self.assertNotIn("Authorization", headers)
        out = self.run_state("episodes", "--simkl", "63375", "--specials")
        self.assertEqual(out.splitlines()[1].split(), ["special", "E01", "Recap"])
        self.assertIn("/anime/episodes/63375", self.http.urls()[-1])
        with self.assertRaises(SystemExit):
            self.run_state("episodes")

    def test_search(self):
        out = self.run_state("search", "Lancer Saga Ren")
        self.assertEqual(out.strip(), "Ren (2012) [simkl_id=63375, slug=ren] anime, movie, "
                                      "5 episodes")
        self.assertIn("q=Lancer+Saga+Ren", self.http.urls("/search/anime")[0])

    def test_mark_with_dates_per_episode(self):
        self.run_state("--write", "mark", "--simkl", "846641", "--episode",
                       "1=2018-05-04T18:20:00Z", "2", "--date", "2020-01-01T00:00:00Z")
        self.assertEqual(self.posted[0][1]["shows"][0]["seasons"][0]["episodes"], [
            {"number": 1, "watched_at": "2018-05-04T18:20:00Z"},
            {"number": 2, "watched_at": "2020-01-01T00:00:00Z"}])
        for bad in (("mark", "--anidb", "1", "--episode", "x=2019"),
                    ("mark", "--anidb", "1", "--episode", "1="),
                    ("unmark", "--anidb", "1", "--episode", "1=2018-05-04T18:20:00Z")):
            with self.assertRaises(SystemExit):
                self.run_state(*bad)

    def test_quota(self):
        def activities(url, headers, data):
            common.RATE_LIMIT = ("135", "500")
            return {}
        self.http.add("/sync/activities", activities)
        self.assertTrue(self.run_state("quota").startswith("135 of 500 Simkl requests left"))

    def test_quota_not_reported(self):
        self.http.add("/sync/activities", {})
        self.assertIn("did not report", self.run_state("quota"))

    def test_unmark_all_removes_the_entry(self):
        out = self.run_state("unmark", "--anidb", "36487", "--all")
        self.assertIn('would POST /sync/history/remove {"movies": [], "shows": '
                      '[{"ids": {"anidb": 36487}}]}', out)
        self.assertEqual(self.posted, [])
        self.run_state("--write", "unmark", "--simkl", "950382", "--all")
        self.assertEqual(self.posted, [("/history/remove", {
            "movies": [], "shows": [{"ids": {"simkl": 950382}}]})])
        with self.assertRaises(SystemExit):
            self.run_state("unmark", "--anidb", "36487", "--all", "--episode", "1")

    def test_mark_needs_write_and_episodes(self):
        out = self.run_state("mark", "--anidb", "36487", "--episode", "2", "3",
                             "--date", "2016-02-01T20:00:00Z")
        self.assertIn("would POST /sync/history", out)
        self.assertEqual(self.posted, [])
        with self.assertRaises(SystemExit):
            self.run_state("--write", "mark", "--anidb", "36487")
        with self.assertRaises(SystemExit):
            self.run_state("--write", "unmark", "--episode", "1")

    def test_mark_and_unmark_with_write(self):
        out = self.run_state("--write", "mark", "--anidb", "36487", "--episode", "2", "3",
                             "--date", "2016-02-01T20:00:00Z")
        self.assertIn('added: {"episodes": 2}', out)
        self.assertEqual(self.posted[0], ("/history", {"movies": [], "shows": [{
            "ids": {"anidb": 36487}, "seasons": [{"number": 1, "episodes": [
                {"number": 2, "watched_at": "2016-02-01T20:00:00Z"},
                {"number": 3, "watched_at": "2016-02-01T20:00:00Z"}]}]}]}))
        self.run_state("--write", "unmark", "--imdb", "tt1156099", "--movie")
        self.assertEqual(self.posted[1], ("/history/remove", {
            "movies": [{"ids": {"imdb": "tt1156099"}}], "shows": []}))


if __name__ == "__main__":
    unittest.main()
