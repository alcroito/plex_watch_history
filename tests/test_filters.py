import argparse
import json
import os
import time
import unittest

from tests.support import FakeHttp, TempDirTestCase, captured_log, pts

SEARCH_TV = {"results": [
    {"id": 5438, "name": "The Help Desk", "original_name": "The Help Desk", "first_air_date": "2006-02-03"},
    {"id": 9999, "name": "The Help Desk (US)", "first_air_date": "2007-01-01"},
]}
SEARCH_MOVIE = {"results": [{"id": 834563, "title": "The Help Desk Manual", "release_date": "2014-01-01"}]}
EXTERNAL_TV = {"id": 5438, "imdb_id": "tt1706137", "tvdb_id": 85625}


def tmdb_http():
    return FakeHttp([
        ("/search/tv", SEARCH_TV),
        ("/search/movie", SEARCH_MOVIE),
        ("/tv/5438/external_ids", EXTERNAL_TV),
        ("/tv/9999/external_ids", {"id": 9999}),
        ("/movie/834563/external_ids", {"id": 834563, "imdb_id": "tt4056940"}),
    ])


class TmdbTest(TempDirTestCase):
    def test_exact_title_matches_with_external_ids(self):
        http = self.patch_http(tmdb_http())
        tmdb = pts.Tmdb("0123456789abcdef0123456789abcdef", "tmdb.json", 7)
        self.assertEqual(tmdb.find("the help desk"),
                         [("tv", "The Help Desk", "2006",
                           {"tmdb": "5438", "tvdb": "85625", "imdb": "tt1706137"})])
        self.assertTrue(all("api_key=0123456789abcdef" in u for u in http.urls()))

    def test_without_exact_match_uses_top_result_of_each_kind(self):
        self.patch_http(tmdb_http())
        matches = pts.Tmdb("k", "tmdb.json", 7).find("Help Desk")
        self.assertEqual([(m[0], m[3]["tmdb"]) for m in matches], [("tv", "5438"), ("movie", "834563")])

    def test_original_title_matches(self):
        self.patch_http(FakeHttp([
            ("/search/tv", {"results": [{"id": 69798, "name": "Sea Serpent",
                                         "original_name": "ヨルムンガンド"}]}),
            ("/search/movie", {"results": []}),
            ("/tv/69798/external_ids", {"tvdb_id": 122491}),
        ]))
        matches = pts.Tmdb("k", "tmdb.json", 7).find("ヨルムンガンド")
        self.assertEqual(matches, [("tv", "Sea Serpent", "", {"tmdb": "69798", "tvdb": "122491"})])

    def test_year_is_passed_per_kind(self):
        http = self.patch_http(tmdb_http())
        pts.Tmdb("k", "tmdb.json", 7).find("The Help Desk", 2006)
        self.assertIn("first_air_date_year=2006", http.urls("/search/tv")[0])
        self.assertIn("primary_release_year=2006", http.urls("/search/movie")[0])

    def test_v4_token_uses_bearer_header(self):
        http = self.patch_http(tmdb_http())
        pts.Tmdb("eyJhbGciOi.token", "tmdb.json", 7).find("The Help Desk")
        url, headers, _ = http.calls[0]
        self.assertEqual(headers["Authorization"], "Bearer eyJhbGciOi.token")
        self.assertNotIn("api_key", url)

    def test_cache_hits_and_key_not_stored(self):
        http = self.patch_http(tmdb_http())
        pts.Tmdb("secretkey", "tmdb.json", 7).find("The Help Desk")
        requests = len(http.calls)
        again = pts.Tmdb("secretkey", "tmdb.json", 7)
        again.find("The Help Desk")
        self.assertEqual(len(http.calls), requests)
        self.assertEqual(again.stats(), f"0 fetched, {requests} from cache")
        with open("tmdb.json") as f:
            self.assertNotIn("secretkey", f.read())

    def test_expired_entries_are_fetched_again(self):
        http = self.patch_http(tmdb_http())
        pts.Tmdb("k", "tmdb.json", 7).find("The Help Desk")
        with open("tmdb.json") as f:
            cache = json.load(f)
        old = next(iter(cache))
        cache[old]["fetched_at"] = time.time() - 8 * 86400
        with open("tmdb.json", "w") as f:
            json.dump(cache, f)
        http.calls.clear()
        tmdb = pts.Tmdb("k", "tmdb.json", 7)
        tmdb.find("The Help Desk")
        self.assertEqual(tmdb.stats(), f"1 fetched, {len(cache) - 1} from cache")
        self.assertEqual(pts.Tmdb("k", "tmdb.json", 10).max_age, 10 * 86400)

    def test_zero_days_disables_cache(self):
        http = self.patch_http(tmdb_http())
        pts.Tmdb("k", "tmdb.json", 0).find("The Help Desk")
        pts.Tmdb("k", "tmdb.json", 0).find("The Help Desk")
        self.assertEqual(len(http.calls), 6)
        self.assertFalse(os.path.exists("tmdb.json"))


ANIME_IDS = {
    "53101": {"tvdb_id": 733880, "tvdb_season": 2, "tvdb_epoffset": 0, "mal_id": 63466},
    "48221": {"tvdb_id": 733880, "tvdb_season": 1, "tvdb_epoffset": 0, "tmdb_show_id": 88155},
    "15000": {"tmdb_movie_id": 954801, "imdb_id": "tt0000001,tt6491447"},
    "99": {"tvdb_id": 1},
}


class AnimeIdsTest(TempDirTestCase):
    def test_downloads_when_missing_then_reads_file(self):
        http = self.patch_http(FakeHttp([(pts.ANIME_IDS_URL, ANIME_IDS)]))
        self.assertEqual(pts.load_anime_ids("anime_ids.json"), ANIME_IDS)
        self.assertEqual(pts.load_anime_ids("anime_ids.json"), ANIME_IDS)
        self.assertEqual(len(http.calls), 1)

    def test_bad_download_stops(self):
        self.patch_http(FakeHttp([(pts.ANIME_IDS_URL, ["not", "a", "dict"])]))
        with self.assertRaisesRegex(SystemExit, "Unexpected anime id mapping"):
            pts.load_anime_ids("anime_ids.json")
        self.assertFalse(os.path.exists("anime_ids.json"))

    def test_anidb_ids_for(self):
        self.assertEqual(pts.anidb_ids_for({("tv", "tvdb", "733880")}, ANIME_IDS), {"53101", "48221"})
        self.assertEqual(pts.anidb_ids_for({("tv", "tmdb", "88155")}, ANIME_IDS), {"48221"})
        self.assertEqual(pts.anidb_ids_for({("movie", "tmdb", "954801")}, ANIME_IDS), {"15000"})
        self.assertEqual(pts.anidb_ids_for({("movie", "imdb", "tt6491447")}, ANIME_IDS), {"15000"})
        # A movie tmdb id must not match a show with the same number.
        self.assertEqual(pts.anidb_ids_for({("movie", "tmdb", "88155")}, ANIME_IDS), set())


def filters(**kwargs):
    defaults = dict(guid_regex=None, title_regex=None, title=None, year=None, start=0, count=None,
                    anime_ids="anime_ids.json")
    return argparse.Namespace(**{**defaults, **kwargs})


WATCHED = {
    "com.plexapp.agents.thetvdb://85625/1/1?lang=en": 100,
    "com.plexapp.agents.thetvdb://85625/1/2?lang=en": 200,
    "com.plexapp.agents.imdb://tt1156099?lang=en": 400,
    "com.plexapp.agents.hama://anidb-53101/1/6?lang=en": 1000,
    "com.plexapp.agents.hama://tvdb-733880/2/1?lang=en": 1200,
    "plex://episode/ep-present": 700,
    "plex://movie/mv1": 1500,
    "local://5938": 500,
}
TITLES = {
    "com.plexapp.agents.thetvdb://85625/1/1?lang=en": "The Help Desk",
    "com.plexapp.agents.thetvdb://85625/1/2?lang=en": "The Help Desk",
    "com.plexapp.agents.imdb://tt1156099?lang=en": "Neon Harbor",
    "local://5938": "The Night Ledger",
}


class SelectTest(TempDirTestCase):
    def test_no_lookup_needed_for_guid_regex_and_window(self):
        result = pts.select(WATCHED, filters(guid_regex=r"thetvdb://85625/", start=1, count=1),
                            TITLES, None, None)
        self.assertEqual(list(result), ["com.plexapp.agents.thetvdb://85625/1/2?lang=en"])

    def test_window_orders_by_watch_date(self):
        result = pts.select(WATCHED, filters(count=3), TITLES, None, None)
        self.assertEqual(list(result.values()), [100, 200, 400])
        self.assertEqual(len(pts.select(WATCHED, filters(start=7), TITLES, None, None)), 1)

    def test_title_regex_uses_db_titles_without_plex(self):
        http = self.patch_http(FakeHttp())
        resolver = pts.PlexResolver("t", "cache.json")
        result = pts.select(WATCHED, filters(title_regex="help desk|ledger"), TITLES, resolver, None)
        self.assertEqual(set(result), {"com.plexapp.agents.thetvdb://85625/1/1?lang=en",
                                       "com.plexapp.agents.thetvdb://85625/1/2?lang=en",
                                       "local://5938"})
        # Only plex:// items without a DB title were looked up.
        self.assertEqual({u.rpartition("/")[2].split("?")[0] for u in http.urls()},
                         {"ep-present", "show1", "mv1"})

    def test_title_regex_matches_plex_lookup_title(self):
        self.patch_http(FakeHttp())
        resolver = pts.PlexResolver("t", "cache.json")
        result = pts.select(WATCHED, filters(title_regex="^some movie$"), TITLES, resolver, None)
        self.assertEqual(list(result), ["plex://movie/mv1"])

    def test_title_matches_by_tmdb_ids_and_anime_mapping(self):
        http = tmdb_http()
        http.add("/search/tv", {"results": [{"id": 88155, "name": "Student Council Wars"}]})
        http.add("/search/movie", {"results": []})
        http.add("/tv/88155/external_ids", {"tvdb_id": 733880, "imdb_id": "tt5716548"})
        http.add(pts.ANIME_IDS_URL, ANIME_IDS)
        self.patch_http(http)
        tmdb = pts.Tmdb("k", "tmdb.json", 7)
        result, log = captured_log(2, pts.select, WATCHED, filters(title="Student Council Wars"),
                                   TITLES, None, tmdb)
        self.assertEqual(set(result), {"com.plexapp.agents.hama://anidb-53101/1/6?lang=en",
                                       "com.plexapp.agents.hama://tvdb-733880/2/1?lang=en"})
        self.assertIn("TMDB match: tv 'Student Council Wars'", log)
        self.assertIn("anime mapping: 2 AniDB entries match (48221, 53101)", log)

    def test_title_matches_plex_items_by_ids(self):
        http = tmdb_http()
        http.add("/search/tv", {"results": [{"id": 222, "name": "Present Show"}]})
        http.add("/search/movie", {"results": []})
        http.add("/tv/222/external_ids", {"tvdb_id": 111})
        http.add(pts.ANIME_IDS_URL, {})
        self.patch_http(http)
        resolver = pts.PlexResolver("t", "cache.json")
        tmdb = pts.Tmdb("k", "tmdb.json", 7)
        result = pts.select(WATCHED, filters(title="Present Show"), {}, resolver, tmdb)
        self.assertEqual(list(result), ["plex://episode/ep-present"])

    def test_title_matches_db_title_when_tmdb_has_nothing(self):
        http = FakeHttp([("/search/", {"results": []}), (pts.ANIME_IDS_URL, {})])
        self.patch_http(http)
        tmdb = pts.Tmdb("k", "tmdb.json", 7)
        result, log = captured_log(1, pts.select, WATCHED, filters(title="the night ledger"),
                                   TITLES, None, tmdb)
        self.assertEqual(list(result), ["local://5938"])
        self.assertIn("TMDB has no match", log)

    def test_title_does_not_match_other_shows(self):
        http = tmdb_http()
        http.add(pts.ANIME_IDS_URL, {})
        self.patch_http(http)
        tmdb = pts.Tmdb("k", "tmdb.json", 7)
        result = pts.select(WATCHED, filters(title="The Help Desk"), TITLES, None, tmdb)
        self.assertEqual(set(result), {"com.plexapp.agents.thetvdb://85625/1/1?lang=en",
                                       "com.plexapp.agents.thetvdb://85625/1/2?lang=en"})


class DescribeTest(TempDirTestCase):
    def test_describe(self):
        self.patch_http(FakeHttp())
        resolver = pts.PlexResolver("t", "cache.json")
        self.assertEqual(pts.describe("plex://movie/mv1", resolver),
                         ("movie", {"imdb": "tt0000042", "tmdb": "42"}, "Some Movie"))
        self.assertEqual(pts.describe("com.plexapp.agents.thetvdb://85625/1/1", None),
                         ("tv", {"tvdb": "85625"}, None))
        self.assertEqual(pts.describe("local://1", None), (None, {}, None))
        self.assertEqual(pts.describe("plex://movie/mv1", None), (None, {}, None))


if __name__ == "__main__":
    unittest.main()
