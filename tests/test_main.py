import json
import os
import unittest
from unittest import mock

from tests.support import FakeHttp, TempDirTestCase, common, make_db, pts


def payloads(stdout):
    """Decode the JSON payloads a dry run prints one after another."""
    decoder, found, pos = json.JSONDecoder(), [], 0
    while pos < len(stdout.rstrip()):
        obj, pos = decoder.raw_decode(stdout, pos)
        found.append(obj)
        while pos < len(stdout) and stdout[pos].isspace():
            pos += 1
    return found


def sent_items(payload_list):
    """(kind, ids-ish, season, episode) for every item in the payloads."""
    items = []
    for p in payload_list:
        for m in p["movies"]:
            items.append(("movie", json.dumps(m["ids"], sort_keys=True)))
        for show in p["shows"]:
            for season in show["seasons"]:
                for e in season["episodes"]:
                    items.append((json.dumps(show["ids"], sort_keys=True), season["number"],
                                  e["number"]))
    return items


class SimklServer(FakeHttp):
    """Fake Simkl + Plex: the library has Help Desk S01E01; Neon Harbor is not found."""

    def __init__(self):
        super().__init__([
            ("/users/settings", {"account": {"id": 777, "type": "free"}}),
            ("/sync/all-items/shows/completed", {"shows": [{
                "show": {"ids": {"tvdb": "85625"}},
                "seasons": [{"number": 1, "episodes": [{"number": 1}]}]}]}),
            ("/sync/all-items/", {}),
            ("/sync/history", self.history),
            ("/redirect", None),
        ])
        self.posted = []
        self.history_by_title = False

    def history(self, url, headers, data):
        self.posted.append(data)
        not_found = [m for m in data["movies"] if m["ids"].get("imdb") == "tt1156099"
                     and not (self.history_by_title and m.get("title"))]
        return {"added": {"movies": len(data["movies"]) - len(not_found), "shows": 0,
                          "episodes": sum(len(s["episodes"]) for sh in data["shows"]
                                          for s in sh["seasons"])},
                "not_found": {"movies": not_found, "shows": [], "episodes": []}}


class MainTestCase(TempDirTestCase):
    def setUp(self):
        super().setUp()
        make_db(self.path("com.plexapp.plugins.library.db"))
        with open("anime_ids.json", "w") as f:
            json.dump({}, f)

    def assertUsageError(self, *argv):
        with self.assertRaises(SystemExit) as ctx:
            self.run_main(*argv)
        self.assertEqual(ctx.exception.code, 2)


class ConfigTest(MainTestCase):
    def test_database_path_precedence(self):
        os.rename("com.plexapp.plugins.library.db", "a.db")
        with open(".env", "w") as f:
            f.write("PLEX_DB_PATH=missing-from-env-file.db\n")
        with self.assertRaises(SystemExit):
            self.run_main("--dry-run")
        os.environ["PLEX_DB_PATH"] = "a.db"
        _, err = self.run_main("--dry-run")
        self.assertIn("from a.db", err)
        _, err = self.run_main("--dry-run", "--db", "a.db")
        self.assertIn("from a.db", err)
        with self.assertRaises(SystemExit):
            self.run_main("--dry-run", "--db", "nope.db")

    def test_verbosity_sources(self):
        _, quiet = self.run_main("--dry-run", "-q")
        self.assertEqual(quiet, "")
        os.environ["VERBOSITY"] = "0"
        self.assertEqual(self.run_main("--dry-run")[1], "")
        _, verbose = self.run_main("--dry-run", "-v")
        self.assertIn("skipped     ", verbose)
        del os.environ["VERBOSITY"]
        with open(".env", "w") as f:
            f.write("VERBOSITY=3\n")
        _, debug = self.run_main("--dry-run")
        self.assertIn("    local://5938: identified as com.plexapp.agents.thetvdb://431235/1/1", debug)
        os.environ["VERBOSITY"] = "7"
        with self.assertRaises(SystemExit):
            self.run_main("--dry-run")

    def test_invalid_settings_are_rejected(self):
        for argv in (["--title-regex", "("], ["--count", "-1"], ["--title", "X"],
                     ["--tmdb-cache-days", "-1"], ["--not-found-days", "-2"]):
            with self.subTest(argv=argv):
                self.assertUsageError("--dry-run", *argv)
        for var, value in (("FILTER_START", "abc"), ("TMDB_CACHE_DAYS", "x"),
                           ("SIMKL_NOT_FOUND_DAYS", "y")):
            with self.subTest(var=var), mock.patch.dict(os.environ, {var: value}):
                self.assertUsageError("--dry-run")
        self.assertUsageError()  # posting needs a Simkl client id

    def test_filters_from_env(self):
        with open(".env", "w") as f:
            f.write("FILTER_GUID_REGEX=thetvdb\nFILTER_COUNT=1\n")
        out, err = self.run_main("--dry-run")
        self.assertIn("--start 0 --count 1: 1 items", err)
        self.assertEqual(len(sent_items(payloads(out))), 1)


class DryRunTest(MainTestCase):
    def test_dry_run_without_tokens(self):
        out, err = self.run_main("--dry-run")
        items = sent_items(payloads(out))
        self.assertIn(("movie", '{"imdb": "tt1156099"}'), items)
        self.assertIn(('{"tvdb": "85625"}', 1, 2), items)
        self.assertIn(('{"anidb": 53101}', 1, 6), items)
        self.assertIn(('{"anidb": 36773}', 1, 10), items)
        self.assertIn(('{"tvdb": "733880"}', 2, 1), items)
        self.assertIn(('{"tvdb": "431235"}', 1, 1), items)  # local://, through its show
        self.assertEqual(len(items), 7)
        self.assertIn("1 local:// episodes identified through their show's guid", err)
        for step in range(1, 7):
            self.assertIn(f"[{step}/6]", err)
        self.assertIn("no Plex token given", err)
        self.assertIn("The Help Desk: S01E01-E02", err)
        self.assertIn("movie: Neon Harbor", err)
        self.assertIn("sent/not-found records not used", err)

    def test_only_present_and_orphaned(self):
        self.patch_http(FakeHttp())
        out, _ = self.run_main("--dry-run", "--only", "present", "--plex-token", "t")
        self.assertEqual(sent_items(payloads(out)), [('{"tmdb": "222", "tvdb": "111"}', 1, 1)])
        out, _ = self.run_main("--dry-run", "--only", "orphaned", "--plex-token", "t")
        self.assertNotIn(('{"tmdb": "222", "tvdb": "111"}', 1, 1), sent_items(payloads(out)))

    def test_plex_items_and_gone_episode_fallback(self):
        http = self.patch_http(FakeHttp())
        out, err = self.run_main("--dry-run", "--plex-token", "t")
        items = sent_items(payloads(out))
        self.assertIn(('{"imdb": "tt2394302", "tmdb": "84481", "tvdb": "262699"}', 1, 3), items)
        self.assertIn(("movie", '{"imdb": "tt0000042", "tmdb": "42"}'), items)
        self.assertIn("1 episodes no longer exist on Plex", err)
        self.assertTrue(os.path.exists("plex_metadata_cache.json"))
        requests = len(http.calls)
        self.run_main("--dry-run", "--plex-token", "t")
        self.assertEqual(len(http.calls), requests)

    def test_dry_run_with_simkl_token_checks_library_but_writes_nothing(self):
        server = self.patch_http(SimklServer())
        out, err = self.run_main("--dry-run", "--simkl-client-id", "cid", "--simkl-token", "tok")
        self.assertNotIn(('{"tvdb": "85625"}', 1, 1), sent_items(payloads(out)))
        self.assertIn("already on Simkl: 0 movies, 1 episodes", err)
        self.assertEqual(server.posted, [])
        self.assertFalse(os.path.exists("simkl_sent.json"))
        self.assertEqual(pts.read_env_file(".env"), {"SIMKL_ACCOUNT_ID": "777"})


class MismatchedPlayTest(MainTestCase):
    """A HAMA play stored under HSA S2E2's guid that was S3E13 (show guid and index)."""

    def setUp(self):
        super().setUp()
        hama = "com.plexapp.agents.hama://"
        os.remove("com.plexapp.plugins.library.db")
        make_db("com.plexapp.plugins.library.db", items=[], settings=[], views=[
            (1, hama + "anidb-43154/1/2?lang=en", 4, "HSA", 1, 13, "Dorm Day",
             1530434000, hama + "anidb-45726?lang=en")])

    def test_resolved_with_simkl_catalog_and_cached(self):
        server = self.patch_http(SimklServer())
        server.add("/redirect", lambda url, headers, data: "https://simkl.com/anime/"
                   f"{966609 if 'anidb=45726' in url else 910468}/hsa?client_id=cid")
        server.add("/anime/episodes/", lambda url, headers, data: [
            {"episode": 13, "type": "episode",
             "title": "Dorm Day" if "/966609" in url else "Other"}])
        out, err = self.run_main("--dry-run", "--simkl-client-id", "cid", "--simkl-token", "tok")
        self.assertEqual(sent_items(payloads(out)), [('{"anidb": 45726}', 1, 13)])
        self.assertIn("1 of them identified with Simkl's catalog, 0 skipped", err)
        catalog_calls = len(server.urls("/anime/episodes/"))
        self.run_main("--dry-run", "--simkl-client-id", "cid", "--simkl-token", "tok")
        self.assertEqual(len(server.urls("/anime/episodes/")), catalog_calls)

    def test_skipped_without_simkl(self):
        out, err = self.run_main("--dry-run")
        self.assertEqual(sent_items(payloads(out)), [])
        self.assertIn("they are skipped", err)


class ForgetTest(MainTestCase):
    def setUp(self):
        super().setUp()
        with open("simkl_sent.json", "w") as f:
            json.dump({"777": {"anidb:44015 S01E02": 1, "tvdb:85625 S01E01": 2},
                       "888": {"anidb:44015 S01E02": 3}}, f)
        with open("simkl_not_found.json", "w") as f:
            json.dump({"777": {"anidb:14011 S01E03": {"at": 1, "label": "Witch Seven S01E03"},
                               "movie imdb:tt1": {"at": 1, "label": "movie: Other"}}}, f)

    def records(self):
        with open("simkl_sent.json") as f, open("simkl_not_found.json") as g:
            return json.load(f), json.load(g)

    def test_dry_run_changes_nothing(self):
        before = self.records()
        _, err = self.run_main("--forget", "44015|witch", "--dry-run")
        self.assertIn("would remove anidb:44015 S01E02 from simkl_sent.json, account 888", err)
        self.assertIn("simkl_not_found.json: 1 records match", err)
        self.assertEqual(self.records(), before)

    def test_removes_matching_keys_and_labels_of_every_account(self):
        self.run_main("--forget", "44015|witch")
        sent, not_found = self.records()
        self.assertEqual(sent, {"777": {"tvdb:85625 S01E01": 2}, "888": {}})
        self.assertEqual(list(not_found["777"]), ["movie imdb:tt1"])

    def test_invalid_regex(self):
        self.assertUsageError("--forget", "(")


class MisidentifiedShowTest(MainTestCase):
    """Witch Seven TV episodes 1-3 stored under the 2-episode OVA's AniDB id."""

    def setUp(self):
        super().setUp()
        hama = "com.plexapp.agents.hama://"
        os.remove("com.plexapp.plugins.library.db")
        make_db("com.plexapp.plugins.library.db", items=[], settings=[], views=[
            (1, f"{hama}anidb-14011/1/{n}?lang=en", 4, "Witch Seven", 1, n, "", 100 + n,
             f"{hama}anidb-14011?lang=en") for n in (1, 2, 3)] + [
            (1, "com.plexapp.agents.thetvdb://85625/1/2?lang=en", 4, "The Help Desk", 1, 2,
             "Printer Fire", 200, None)])

    def test_show_is_skipped_before_sending(self):
        server = self.patch_http(SimklServer())
        server.add("/redirect", lambda url, headers, data:
                   "https://simkl.com/anime/79948/witch" if "anidb=14011" in url else None)
        server.add("/anime/episodes/79948", [{"episode": n, "type": "episode"} for n in (1, 2)])
        out, err = self.run_main("--dry-run", "--simkl-client-id", "cid", "--simkl-token", "tok")
        self.assertEqual(sent_items(payloads(out)), [('{"tvdb": "85625"}', 1, 2)])
        self.assertIn("skipping 3 episodes of Witch Seven: episode 3 is past the end of its "
                      "2-episode Simkl entry", err)


class PostingTest(MainTestCase):
    argv = ("--simkl-client-id", "cid", "--simkl-token", "tok")

    def test_posts_records_and_skips_on_rerun(self):
        server = self.patch_http(SimklServer())
        _, err = self.run_main(*self.argv)
        # 7 exportable minus Help Desk S01E01 already on Simkl; Neon Harbor is retried by title.
        self.assertEqual([len(sent_items([p])) for p in server.posted], [6, 1])
        self.assertEqual(server.posted[1]["movies"][0]["title"], "Neon Harbor")
        posted = sent_items(server.posted[:1])
        self.assertNotIn(('{"tvdb": "85625"}', 1, 1), posted)
        self.assertIn("Done: Simkl added 0 movies and 5 episodes", err)
        self.assertIn("1 items not matched by Simkl, recorded in simkl_not_found.json", err)
        self.assertEqual(pts.read_env_file(".env")["SIMKL_ACCOUNT_ID"], "777")
        with open("simkl_not_found.json") as f:
            labels = [v["label"] for v in json.load(f)["777"].values()]
        self.assertEqual(labels, ["movie: Neon Harbor"])

        server.posted.clear()
        _, err = self.run_main(*self.argv)
        self.assertEqual(server.posted, [])
        self.assertIn("sent in earlier runs: 5 items", err)
        self.assertIn("not found on Simkl in the last 30 days: 1 items", err)
        self.assertEqual(server.urls("/users/settings"), [server.urls("/users/settings")[0]])

        self.run_main(*self.argv, "--resend")
        self.assertEqual(len(sent_items(server.posted)), 5)
        server.posted.clear()
        self.run_main(*self.argv, "--retry-not-found")
        self.assertEqual(sent_items(server.posted), [("movie", '{"imdb": "tt1156099"}')] * 2)

    def test_verbose_posting_lists_batches_and_failures(self):
        self.patch_http(SimklServer())
        _, err = self.run_main(*self.argv, "-v")
        self.assertIn("batch 1/1: sent 6 items, added 5 episodes and 0 movies, 1 not found", err)
        self.assertIn("not found by Simkl (ids): movie: Neon Harbor", err)
        self.assertIn("retry 1: 1 items not found, sending them again by title and year", err)
        self.assertIn("not found by Simkl (title and year): movie: Neon Harbor", err)

    def test_fallback_recovers_items(self):
        server = SimklServer()
        # This Simkl only finds Neon Harbor by title.
        server.history_by_title = True
        self.patch_http(server)
        _, err = self.run_main(*self.argv, "-v")
        self.assertIn("found by title and year: movie: Neon Harbor", err)
        self.assertIn("1 items found only by a fallback: 1 by title and year", err)
        self.assertNotIn("not matched", err)
        with open("simkl_sent.json") as f:
            self.assertIn("movie imdb:tt1156099", json.load(f)["777"])

    def test_quota_left_is_reported(self):
        server = self.patch_http(SimklServer())
        history = server.history

        def counted(url, headers, data):
            common.RATE_LIMIT = ("135", "500")
            return history(url, headers, data)
        server.add("/sync/history", counted)
        _, err = self.run_main(*self.argv)
        self.assertIn("135 of 500 Simkl requests left today", err)

    def test_quiet_posting_prints_only_the_result(self):
        self.patch_http(SimklServer())
        _, err = self.run_main(*self.argv, "-q")
        self.assertEqual(err.splitlines(), [
            "Done: Simkl added 0 movies and 5 episodes across 0 shows",
            "1 items not matched by Simkl, recorded in simkl_not_found.json"])

    def test_login_when_no_token(self):
        self.patch_http(SimklServer())
        with open(".env", "w") as f:
            f.write("SIMKL_CLIENT_ID=cid\nSIMKL_ACCOUNT_ID=old-account\n")

        def form(url, fields):
            if url.endswith("/device"):
                return 200, {"device_code": "d", "user_code": "U", "verification_uri": "v",
                             "verification_uri_complete": "vc", "interval": 1}
            return 200, {"access_token": "at", "refresh_token": "rt",
                         "scope": "media:read media:write"}
        with mock.patch.object(common, "post_form", form):
            _, err = self.run_main()
        env = pts.read_env_file(".env")
        self.assertEqual((env["SIMKL_ACCESS_TOKEN"], env["SIMKL_REFRESH_TOKEN"]), ("at", "rt"))
        # A new login fetches the account again instead of trusting the stored one.
        self.assertEqual(env["SIMKL_ACCOUNT_ID"], "777")
        self.assertIn("Open this to connect your Simkl account:", err)


if __name__ == "__main__":
    unittest.main()
