import os
import stat
import unittest
from unittest import mock

from tests.support import FakeHttp, TempDirTestCase, captured_log, common, pts

DEVICE = {"device_code": "dc", "user_code": "AB-CD", "verification_uri": "https://simkl.com/pin",
          "verification_uri_complete": "https://simkl.com/pin?user_code=AB-CD",
          "expires_in": 900, "interval": 5}
TOKENS = {"access_token": "simkl_at_new", "refresh_token": "simkl_rt_new",
          "scope": "media:read media:write"}


class FakeForm:
    """Stands in for pts.post_form with queued responses per endpoint."""

    def __init__(self, device=(200, DEVICE), polls=(), refresh=None):
        self.device = device
        self.polls = list(polls)
        self.refresh = refresh
        self.calls = []

    def __call__(self, url, fields):
        self.calls.append((url.rpartition("/")[2], dict(fields)))
        if url.endswith("/device"):
            return self.device
        if fields["grant_type"] == "refresh_token":
            return self.refresh
        return self.polls.pop(0)


class SimklAuthTest(TempDirTestCase):
    def setUp(self):
        super().setUp()
        with open(".env", "w") as f:
            f.write("SIMKL_CLIENT_ID=cid\nSIMKL_ACCESS_TOKEN=old\n")

    def form(self, fake):
        patcher = mock.patch.object(common, "post_form", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def test_device_login_waits_for_approval_and_saves_tokens(self):
        sleeps = []
        form = self.form(FakeForm(polls=[(400, {"error": "authorization_pending"}),
                                         (400, {"error": "slow_down"}), (200, TOKENS)]))
        simkl = pts.Simkl("cid", None, None, ".env")
        with mock.patch.object(pts.time, "sleep", sleeps.append):
            _, log = captured_log(0, simkl.login)
        self.assertEqual(sleeps, [5, 5, 10])
        self.assertEqual(form.calls[0], ("device", {"client_id": "cid",
                                                    "scope": "media:read media:write"}))
        self.assertIn("https://simkl.com/pin?user_code=AB-CD", log)
        self.assertEqual((simkl.token, simkl.refresh_token), ("simkl_at_new", "simkl_rt_new"))
        self.assertEqual(pts.read_env_file(".env"), {
            "SIMKL_CLIENT_ID": "cid", "SIMKL_ACCESS_TOKEN": "simkl_at_new",
            "SIMKL_REFRESH_TOKEN": "simkl_rt_new"})
        self.assertEqual(stat.S_IMODE(os.stat(".env").st_mode), 0o600)

    def test_login_rejects_read_only_token(self):
        self.form(FakeForm(polls=[(200, {**TOKENS, "scope": "media:read"})]))
        with self.assertRaisesRegex(SystemExit, "media:write"):
            captured_log(0, pts.Simkl("cid", None, None, ".env").login)
        self.assertEqual(pts.read_env_file(".env")["SIMKL_ACCESS_TOKEN"], "old")

    def test_login_stops_on_expired_code_or_failed_start(self):
        self.form(FakeForm(polls=[(400, {"error": "expired_token"})]))
        with self.assertRaisesRegex(SystemExit, "expired_token"):
            captured_log(0, pts.Simkl("cid", None, None, ".env").login)
        self.form(FakeForm(device=(401, {"error": "invalid_client"})))
        with self.assertRaisesRegex(SystemExit, "invalid_client"):
            pts.Simkl("cid", None, None, ".env").login()

    def test_login_times_out(self):
        self.form(FakeForm(polls=[(400, {"error": "authorization_pending"})] * 3))
        clock = iter([0, 100, 500, 1000])
        with mock.patch.object(pts.time, "monotonic", lambda: next(clock)):
            with self.assertRaisesRegex(SystemExit, "timed out"):
                captured_log(0, pts.Simkl("cid", None, None, ".env").login)

    def test_401_refreshes_token_and_retries(self):
        form = self.form(FakeForm(refresh=(200, {**TOKENS, "access_token": "simkl_at_refreshed"})))
        seen = []

        def http(url, headers, data=None, retries=3):
            seen.append(headers["Authorization"])
            if headers["Authorization"] != "Bearer simkl_at_refreshed":
                raise pts.HttpError(401, url, "expired")
            return {"ok": 1}
        self.patch_http(http)
        simkl = pts.Simkl("cid", "old", "simkl_rt_new", ".env")
        self.assertEqual(simkl.get("/sync/activities"), {"ok": 1})
        self.assertEqual(seen, ["Bearer old", "Bearer simkl_at_refreshed"])
        self.assertEqual([c[1]["grant_type"] for c in form.calls], ["refresh_token"])
        self.assertEqual(pts.read_env_file(".env")["SIMKL_ACCESS_TOKEN"], "simkl_at_refreshed")

    def test_failed_refresh_falls_back_to_login(self):
        form = self.form(FakeForm(refresh=(400, {"error": "invalid_grant"}), polls=[(200, TOKENS)]))

        def http(url, headers, data=None, retries=3):
            if headers["Authorization"] != "Bearer simkl_at_new":
                raise pts.HttpError(401, url, "")
            return {"ok": 1}
        self.patch_http(http)
        simkl = pts.Simkl("cid", "old", "bad", ".env")
        result, _ = captured_log(0, simkl.get, "/x")
        self.assertEqual(result, {"ok": 1})
        self.assertEqual([c[0] for c in form.calls], ["token", "device", "token"])

    def test_other_errors_are_not_retried(self):
        self.form(FakeForm())

        def http(url, headers, data=None, retries=3):
            raise pts.HttpError(500, url, "boom")
        self.patch_http(http)
        with self.assertRaises(pts.HttpError):
            pts.Simkl("cid", "t", "r", ".env").get("/x")

    def test_requests_carry_app_parameters_and_token(self):
        http = self.patch_http(FakeHttp([("/users/settings", {"account": {"id": 12345}})]))
        self.assertEqual(pts.Simkl("cid", "tok", None, ".env").account_id(), "12345")
        url, headers, _ = http.calls[0]
        self.assertIn("client_id=cid", url)
        self.assertIn("app-name=plex-history-to-simkl", url)
        self.assertEqual(headers["Authorization"], "Bearer tok")
        self.assertEqual(headers["User-Agent"], "plex-history-to-simkl/1.0")



class SimklLoginCommandTest(TempDirTestCase):
    def setUp(self):
        super().setUp()
        with open(".env", "w") as f:
            f.write("SIMKL_CLIENT_ID=cid\nSIMKL_REFRESH_TOKEN=simkl_rt_old\n")
        self.http = self.patch_http(FakeHttp([("/users/settings", {"account": {"id": 777}})]))

    def form(self, fake):
        patcher = mock.patch.object(common, "post_form", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def test_device_login_saves_tokens_and_account(self):
        form = self.form(FakeForm(polls=[(200, TOKENS)]))
        _, err = self.run_main(command="simkl-login")
        self.assertEqual([c[0] for c in form.calls], ["device", "token"])
        self.assertEqual(pts.read_env_file(".env"), {
            "SIMKL_CLIENT_ID": "cid", "SIMKL_REFRESH_TOKEN": "simkl_rt_new",
            "SIMKL_ACCESS_TOKEN": "simkl_at_new", "SIMKL_ACCOUNT_ID": "777"})
        self.assertIn("https://simkl.com/pin?user_code=AB-CD", err)
        self.assertIn("Simkl access and refresh tokens and account id 777 saved to .env", err)
        self.assertNotIn("simkl_at_new", err)

    def test_refresh_uses_saved_refresh_token(self):
        form = self.form(FakeForm(refresh=(200, {**TOKENS, "access_token": "simkl_at_fresh"})))
        self.run_main("--refresh", command="simkl-login")
        self.assertEqual(form.calls, [("token", {"grant_type": "refresh_token", "client_id": "cid",
                                                 "refresh_token": "simkl_rt_old"})])
        self.assertEqual(pts.read_env_file(".env")["SIMKL_ACCESS_TOKEN"], "simkl_at_fresh")
        self.assertEqual(self.http.calls[0][1]["Authorization"], "Bearer simkl_at_fresh")

    def test_failed_refresh_stops(self):
        self.form(FakeForm(refresh=(400, {"error": "invalid_grant"})))
        with self.assertRaisesRegex(SystemExit, "without --refresh"):
            self.run_main("--refresh", command="simkl-login")
        self.assertNotIn("SIMKL_ACCESS_TOKEN", pts.read_env_file(".env"))

    def test_missing_settings(self):
        self.form(FakeForm())
        with open(".env", "w") as f:
            f.write("SIMKL_CLIENT_ID=cid\n")
        with self.assertRaises(SystemExit):
            self.run_main("--refresh", command="simkl-login")
        with self.assertRaises(SystemExit):
            self.run_main("--env-file", "missing.env", command="simkl-login")
        os.environ["SIMKL_CLIENT_ID"] = "from-env"
        form = self.form(FakeForm(polls=[(200, TOKENS)]))
        self.run_main("--env-file", "other.env", command="simkl-login")
        self.assertEqual(form.calls[0][1]["client_id"], "from-env")

LIBRARY = {
    "/sync/all-items/shows/completed": {"shows": [{
        "show": {"ids": {"simkl": 1, "tvdb": "85625", "imdb": "tt1706137"}},
        "seasons": [{"number": 1, "episodes": [{"number": 1}, {"number": 2}]}]}]},
    "/sync/all-items/anime/watching": {"anime": [{
        "show": {"ids": {"simkl": 2, "anidb": "53101", "tvdb": "733880"}},
        "seasons": [{"number": 1, "episodes": [{"number": 6}]}]}]},
    "/sync/all-items/movies/completed": {"movies": [{"movie": {"ids": {"simkl": 3, "tmdb": "78"}}}]},
}


class SimklLibraryTest(TempDirTestCase):
    def load(self, http):
        self.patch_http(http)
        library = pts.SimklLibrary()
        _, log = captured_log(2, library.load, pts.Simkl("cid", "tok", None, ".env"))
        return library, log

    def test_reads_watched_buckets_and_matches_ids(self):
        http = FakeHttp([(k, v) for k, v in LIBRARY.items()] + [("/sync/all-items/", {})])
        library, log = self.load(http)
        self.assertEqual(len(http.calls), 9)
        self.assertNotIn("plantowatch", " ".join(http.urls()))
        self.assertIn("shows/completed: 1 items", log)
        self.assertTrue(library.has_movie({"tmdb": "78", "imdb": "x"}))
        self.assertFalse(library.has_movie({"tmdb": "79"}))
        self.assertTrue(library.has_episode({"tvdb": "85625"}, 1, 2, "tvdb"))
        self.assertTrue(library.has_episode({"imdb": "tt1706137"}, 1, 1, "tvdb"))
        self.assertFalse(library.has_episode({"tvdb": "85625"}, 1, 3, "tvdb"))

    def test_anime_films_count_as_movies(self):
        film = {"anime_type": "movie", "show": {"ids": {"simkl": 4, "imdb": "tt4952006"}},
                "seasons": [{"number": 1, "episodes": [{"number": 1}]}]}
        series_part = {"anime_type": "movie", "show": {"ids": {"simkl": 5, "imdb": "tt3280739"}},
                       "seasons": [{"number": 1, "episodes": [{"number": 4}]}]}
        tv = {"anime_type": "tv", "show": {"ids": {"simkl": 6, "imdb": "tt1"}},
              "seasons": [{"number": 1, "episodes": [{"number": 1}]}]}
        library, _ = self.load(FakeHttp([
            ("/sync/all-items/anime/completed", {"anime": [film, series_part, tv]}),
            ("/sync/all-items/", {})]))
        self.assertTrue(library.has_movie({"imdb": "tt4952006"}))
        self.assertFalse(library.has_movie({"imdb": "tt3280739"}))
        self.assertFalse(library.has_movie({"imdb": "tt1"}))

    def test_anime_compared_only_for_anidb_numbering(self):
        library, _ = self.load(FakeHttp([(k, v) for k, v in LIBRARY.items()]
                                        + [("/sync/all-items/", {})]))
        self.assertTrue(library.has_episode({"anidb": 53101}, 1, 6, "anidb"))
        # TVDB-numbered sources can't be compared against Simkl's AniDB numbering.
        self.assertFalse(library.has_episode({"tvdb": "733880"}, 1, 6, "tvdb"))

    def test_max_items_falls_back_to_original_episodes(self):
        def shows(url, headers, data=None):
            if "include_all_episodes=yes" in url:
                raise pts.HttpError(400, url, '{"error": "max_items"}')
            return {}
        http = FakeHttp([("/sync/all-items/shows/", shows), ("/sync/all-items/", {})])
        self.load(http)
        self.assertEqual(len(http.urls("include_all_episodes=original")), 4)

    def test_other_errors_propagate(self):
        def fail(url, headers, data=None):
            raise pts.HttpError(400, url, '{"error": "other"}')
        self.patch_http(FakeHttp([("/sync/all-items/", fail)]))
        with self.assertRaises(pts.HttpError):
            pts.SimklLibrary().load(pts.Simkl("cid", "tok", None, ".env"))


class SimklCatalogTest(TempDirTestCase):
    def test_tvdb_season_sizes_from_catalog_without_token(self):
        http = self.patch_http(FakeHttp([
            ("/redirect", "https://simkl.com/anime/9698723/lone-ascent?client_id=cid"),
            ("/anime/episodes/9698723", [
                {"episode": n, "type": "episode", "tvdb": {"season": 1, "episode": n}}
                for n in range(1, 13)] + [{"episode": 1, "type": "special", "tvdb": None}]),
        ]))
        catalog = pts.SimklCatalog(pts.Simkl("cid", "tok", None, ".env"))
        self.assertEqual(catalog.tvdb_season_sizes("778312"), {1: 12})
        self.assertEqual(catalog.tvdb_season_sizes("778312"), {1: 12})
        self.assertEqual(len(http.calls), 2)
        self.assertIn("tvdb=778312", http.calls[0][0])
        self.assertIn("to=simkl", http.calls[0][0])
        for url, headers, _ in http.calls:
            self.assertNotIn("Authorization", headers)
        self.assertIn("extended=full", http.calls[1][0])

    def test_entry_kinds(self):
        self.patch_http(FakeHttp([("tvdb=1", "https://simkl.com/tv/59325/help-desk"),
                                  ("imdb=tt1", "https://simkl.com/movies/84359/neon-harbor"),
                                  ("/redirect", None),
                                  ("/tv/episodes/59325", [{"season": 1, "episode": 1}])]))
        catalog = pts.SimklCatalog(pts.Simkl("cid", "tok", None, ".env"))
        self.assertEqual(catalog.entry(tvdb=1), ("tv", 59325))
        self.assertEqual(catalog.entry(imdb="tt1"), ("movies", 84359))
        self.assertIsNone(catalog.entry(anidb=1))
        self.assertEqual(catalog.tvdb_season_sizes(1), {})

    def test_unknown_show_has_no_sizes(self):
        self.patch_http(FakeHttp([("/redirect", "https://simkl.com?client_id=cid")]))
        catalog = pts.SimklCatalog(pts.Simkl("cid", "tok", None, ".env"))
        self.assertEqual(catalog.tvdb_season_sizes("1"), {})

    def test_lookups_are_cached_in_a_file_until_they_expire(self):
        routes = [("/redirect", "https://simkl.com/anime/910468/hsa"),
                  ("/anime/episodes/910468", [
                      {"episode": 1, "type": "episode", "title": "A"},
                      {"episode": 1, "type": "special", "title": "S"}])]
        http = self.patch_http(FakeHttp(routes))
        simkl = pts.Simkl("cid", "tok", None, ".env")
        catalog = pts.SimklCatalog(simkl, "catalog.json", 30)
        self.assertEqual([e["title"] for e in catalog.anidb_episodes(43154)], ["A"])
        self.assertEqual(len(http.calls), 2)
        self.assertEqual([e["title"] for e in
                          pts.SimklCatalog(simkl, "catalog.json", 30).anidb_episodes(43154)], ["A"])
        self.assertEqual(len(http.calls), 2)
        with mock.patch.object(pts.time, "time", lambda: 10 ** 11):
            pts.SimklCatalog(simkl, "catalog.json", 30).anidb_episodes(43154)
        self.assertEqual(len(http.calls), 4)

    def test_no_cache_file_with_zero_days(self):
        self.patch_http(FakeHttp([("/redirect", "https://simkl.com?client_id=cid")]))
        catalog = pts.SimklCatalog(pts.Simkl("cid", "tok", None, ".env"), "catalog.json", 0)
        self.assertEqual(catalog.anidb_episodes(1), [])
        self.assertFalse(os.path.exists("catalog.json"))

    def test_multipart_entry_matches_episode_subtitle(self):
        http = self.patch_http(FakeHttp([
            ("/search/anime", [
                {"ids": {"simkl_id": 1}, "title": "Short", "ep_count": 2, "type": "tv"},
                {"ids": {"simkl_id": 63375}, "title": "Lancer Saga: Boukyaku no Ren",
                 "ep_count": 5, "type": "movie"}]),
            ("/anime/episodes/63375", [
                {"episode": 4, "type": "episode", "title": "Ashes of Oath"}]),
        ]))
        catalog = pts.SimklCatalog(pts.Simkl("cid", "tok", None, ".env"))
        self.assertEqual(catalog.multipart_entry("Ren", 4, "ashes of oath"),
                         (63375, "Lancer Saga: Boukyaku no Ren"))
        self.assertIsNone(catalog.multipart_entry("Ren", 4, "Other"))
        self.assertEqual(len(http.urls("/search/anime")), 1)
        _, headers, _ = http.calls[1]
        self.assertNotIn("Authorization", headers)

    def test_multipart_entry_without_subtitle_needs_movie_or_ova(self):
        self.patch_http(FakeHttp([("/search/anime", [
            {"ids": {"simkl_id": 1}, "title": "Series", "ep_count": 12, "type": "tv"},
            {"ids": {"simkl_id": 2}, "title": "Films", "ep_count": 3, "type": "movie"}])]))
        catalog = pts.SimklCatalog(pts.Simkl("cid", "tok", None, ".env"))
        self.assertEqual(catalog.multipart_entry("X", 3), (2, "Films"))
        self.assertIsNone(catalog.multipart_entry("X", 4))


if __name__ == "__main__":
    unittest.main()
