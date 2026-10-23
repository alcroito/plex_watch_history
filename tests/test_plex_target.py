import json
import os
import unittest
from unittest import mock

from tests.support import FakeHttp, TempDirTestCase, captured_log, make_db, pts

SHOW = "show-dw"
SEASON0, SEASON1 = "season-dw-0", "season-dw-1"
EP1 = "ep-dw-1"


def container(items, total=None):
    return {"MediaContainer": {"Metadata": items, "size": len(items),
                               "totalSize": total if total is not None else len(items)}}


class PlexServer(FakeHttp):
    """Fake Plex metadata/discover/community APIs for Coil Runner (tvdb 303415)."""

    def __init__(self, watched=()):
        super().__init__([
            ("/matches?type=2&guid=tvdb%3A%2F%2F303415", container([{"ratingKey": SHOW}])),
            ("/matches?type=1&guid=imdb%3A%2F%2Ftt1156099", container([{"ratingKey": "movie-br"}])),
            ("/matches?", container([])),
            (f"/{SHOW}/children", container([{"index": 0, "ratingKey": SEASON0},
                                             {"index": 1, "ratingKey": SEASON1}])),
            (f"/{SEASON1}/children", self.episodes),
            (f"/{SEASON0}/children", container([
                {"index": 1, "ratingKey": "sp-dw-1", "title": "Recap",
                 "originallyAvailableAt": "2016-03-20"},
                {"index": 2, "ratingKey": "sp-dw-2", "title": "Old Ruins",
                 "originallyAvailableAt": "2016-04-01"}])),
            ("/userState", self.user_state),
            ("/actions/scrobble", self.scrobble),
            (pts.PLEX_ACCOUNT_API, {"uuid": "uuid-1"}),
            (pts.PLEX_COMMUNITY_API, self.graphql),
        ])
        self.watched = set(watched)
        self.mutations = []  # number of entries dated per mutation request
        self.history = []  # newest first: [activity id, plex id, date]
        self.dates = {}

    def episodes(self, url, headers, data):
        start = int(headers["X-Plex-Container-Start"])
        items = [{"index": n, "ratingKey": f"ep-dw-{n}"} for n in range(1, 13)]
        return container(items[start:start + int(headers["X-Plex-Container-Size"])], 12)

    def user_state(self, url, headers, data):
        key = url.split("/metadata/")[1].split("/")[0]
        return {"MediaContainer": {"UserState": [
            {"ratingKey": key, "viewCount": 1 if key in self.watched else 0}]}}

    def scrobble(self, url, headers, data):
        key = url.split("key=")[1].split("&")[0]
        self.watched.add(key)
        self.history.insert(0, [f"act-{key}", key, "now"])
        return None

    def graphql(self, url, headers, data):
        # Plex rejects other types for watchHistory's page size.
        assert "$first: Int" not in data["query"], data["query"]
        if "updateActivity" in data["query"]:
            # Aliased mutations m0, m1, ... with variables a<i> (entry id) and d<i> (input).
            variables, result = data["variables"], {}
            self.mutations.append(len([k for k in variables if k.startswith("a")]))
            for key, activity in variables.items():
                if key.startswith("a"):
                    self.dates[activity] = variables["d" + key[1:]]["date"]
                    result["m" + key[1:]] = {"id": activity}
            return {"data": result}
        nodes = [{"id": a, "metadataItem": {"id": k}} for a, k, _ in self.history[:10]]
        return {"data": {"user": {"watchHistory": {"nodes": nodes}}}}


ANIME_IDS = {"36487": {"tvdb_id": 303415, "tvdb_season": 1, "tvdb_epoffset": 0},
             "500": {"tvdb_id": 303415, "tvdb_season": 1, "tvdb_epoffset": 10},
             "600": {"tvdb_id": None}, "700": {"tvdb_id": 1, "tvdb_season": -1}}


def episode(ids, season, number, numbering="tvdb"):
    return {"show": "x", "ids": ids, "season": season, "episode": number,
            "numbering": numbering, "label": "Coil Runner", "watched_at": 1421353845}


class PlexTargetTest(TempDirTestCase):
    def target(self, server):
        self.patch_http(server)
        return pts.PlexTarget("token", {}, ANIME_IDS)

    def test_item_id_by_tvdb_and_anidb_mapping(self):
        target = self.target(PlexServer())
        self.assertEqual(target.item_id("episode", episode({"tvdb": "303415"}, 1, 1)), (EP1, None))
        self.assertEqual(target.item_id("episode", episode({"anidb": 36487}, 1, 1, "anidb")),
                         (EP1, None))
        # The episode offset moves AniDB numbering onto TVDB numbering.
        self.assertEqual(target.item_id("episode", episode({"anidb": 500}, 1, 2, "anidb")),
                         ("ep-dw-12", None))

    def test_specials(self):
        target = self.target(PlexServer())
        self.assertEqual(target.season_episodes("303415", 0),
                         [(1, "Recap", "2016-03-20"), (2, "Old Ruins", "2016-04-01")])
        self.assertIsNone(target.season_episodes("1", 0))
        rec = episode({"anidb": 36487}, 0, 3, "anidb")
        self.assertEqual(target.item_id("episode", rec),
                         (None, "AniDB specials have no TVDB mapping"))
        rec["special_reason"] = "no Plex special matches its title or air date"
        self.assertEqual(target.item_id("episode", rec), (None, rec["special_reason"]))
        rec["plex_special"] = ("303415", 0, 2)
        self.assertEqual(target.item_id("episode", rec), ("sp-dw-2", None))

    def test_item_id_reasons(self):
        target = self.target(PlexServer())
        self.assertEqual(target.item_id("episode", episode({"anidb": 600}, 1, 1, "anidb")),
                         (None, "no TVDB season mapping for the AniDB id"))
        self.assertEqual(target.item_id("episode", episode({"anidb": 36487}, 0, 1, "anidb")),
                         (None, "AniDB specials have no TVDB mapping"))
        self.assertEqual(target.item_id("episode", episode({"anidb": 700}, 1, 1, "anidb")),
                         (None, "no TVDB season mapping for the AniDB id"))
        self.assertEqual(target.item_id("episode", episode({"tvdb": "1"}, 1, 1)),
                         (None, "show not found on Plex"))
        self.assertEqual(target.item_id("episode", episode({"tvdb": "303415"}, 3, 1)),
                         (None, "season 3 not found on Plex"))
        self.assertEqual(target.item_id("episode", episode({"tvdb": "303415"}, 1, 40)),
                         (None, "S01E40 not found on Plex"))

    def test_movie_lookup_tries_each_id(self):
        target = self.target(PlexServer())
        movie = {"ids": {"tmdb": "78", "imdb": "tt1156099"}}
        self.assertEqual(target.item_id("movie", movie), ("movie-br", None))
        self.assertEqual(target.item_id("movie", {"ids": {"tmdb": "1"}}),
                         (None, "movie not found on Plex"))

    def test_lookups_are_cached_and_paged_with_headers(self):
        server = PlexServer()
        target = self.target(server)
        target.item_id("episode", episode({"tvdb": "303415"}, 1, 1))
        target.item_id("episode", episode({"tvdb": "303415"}, 1, 2))
        self.assertEqual(len(server.urls("/matches")), 1)
        self.assertEqual(len(server.urls(f"/{SEASON1}/children")), 1)
        self.assertTrue(all("X-Plex-Container" not in u for u in server.urls()))
        self.assertIn("target:items:" + SEASON1, target.cache)

    def test_pages_through_long_lists(self):
        server = PlexServer()
        long_season = [{"index": n, "ratingKey": f"e{n}"} for n in range(1, 451)]

        def children(url, headers, data):
            start = int(headers["X-Plex-Container-Start"])
            return container(long_season[start:start + 200], 450)
        server.add(f"/{SEASON1}/children", children)
        target = self.target(server)
        self.assertEqual(target.item_id("episode", episode({"tvdb": "303415"}, 1, 450)), ("e450", None))
        self.assertEqual(len(server.urls(f"/{SEASON1}/children")), 3)

    def test_mark_watched_sets_date(self):
        server = PlexServer()
        target = self.target(server)
        self.assertEqual(target.mark_watched(EP1, 1421353845), "watched")
        self.assertEqual(server.dates, {f"act-{EP1}": "2015-01-15T20:30:45Z"})
        _, headers, data = server.calls[-1]
        self.assertEqual(headers["X-Plex-Token"], "token")
        self.assertEqual(data["variables"], {"a0": f"act-{EP1}",
                                             "d0": {"date": "2015-01-15T20:30:45Z"}})

    def test_already_watched_is_left_alone(self):
        server = PlexServer(watched=[EP1])
        target = self.target(server)
        self.assertEqual(target.mark_watched(EP1, 1), "already watched")
        self.assertEqual(server.urls("/actions/scrobble"), [])

    def test_missing_history_entry(self):
        server = PlexServer()
        server.add("/actions/scrobble", None)  # marked, but no history entry appears
        target = self.target(server)
        self.assertEqual(target.mark_watched(EP1, 1), "date not set")
        self.assertEqual(server.dates, {})

    def test_batch_dates_all_entries_with_one_mutation(self):
        server = PlexServer(watched=["ep-dw-3"])
        self.patch_http(server)
        target = pts.PlexTarget("token", {}, ANIME_IDS, "pending.json")
        outcomes = target.mark_batch([(EP1, 1), ("ep-dw-2", 2), ("ep-dw-3", 3), (EP1, 4)])
        self.assertEqual(outcomes, ["watched", "watched", "already watched", "already watched"])
        self.assertEqual(server.dates, {f"act-{EP1}": "1970-01-01T00:00:01Z",
                                        "act-ep-dw-2": "1970-01-01T00:00:02Z"})
        self.assertEqual(server.mutations, [2])
        self.assertEqual(len(server.urls("/actions/scrobble")), 2)

    def test_date_updates_are_paced_to_six_per_minute(self):
        clock = [1000.0]
        sleeps = []

        def sleep(seconds):
            sleeps.append(round(seconds))
            clock[0] += seconds
        self.patch_http(PlexServer())
        target = pts.PlexTarget("token", {}, ANIME_IDS)
        with mock.patch.object(pts.time, "time", lambda: clock[0]), \
                mock.patch.object(pts.time, "sleep", sleep):
            for _ in range(5):
                target._wait_for_date_slots(3)
                clock[0] += 1  # each request takes a second
        # Groups of 3 a second apart: two fit; the third waits for the first group to leave the
        # window (59 s), the fourth fits as the second group has left, the fifth waits again.
        self.assertEqual(sleeps, [59, 59])

    def test_dates_are_sent_in_small_groups(self):
        server = PlexServer()
        self.patch_http(server)
        target = pts.PlexTarget("token", {}, ANIME_IDS, "pending.json")
        items = [(f"ep-dw-{n}", n) for n in range(1, 8)]
        self.assertEqual(target.mark_batch(items), ["watched"] * 7)
        self.assertEqual(server.mutations, [3, 3, 1])
        self.assertEqual(target.pending, {})
        self.assertEqual(target.pending, {})

    def test_item_is_pending_until_its_date_is_set(self):
        server = PlexServer()
        self.patch_http(server)
        target = pts.PlexTarget("token", {}, ANIME_IDS, "pending.json")
        dates_seen = []
        set_dates = target._set_dates

        def check_journal(dates):
            with open("pending.json") as f:
                dates_seen.append(json.load(f))
            set_dates(dates)
        target._set_dates = check_journal
        self.assertEqual(target.mark_watched(EP1, 1421353845), "watched")
        self.assertEqual(dates_seen, [{EP1: 1421353845}])
        with open("pending.json") as f:
            self.assertEqual(json.load(f), {})

    def test_date_not_set_stays_pending_and_is_repaired_by_the_next_run(self):
        server = PlexServer()
        server.add("/actions/scrobble", lambda url, headers, data: None)  # no entry appears yet
        self.patch_http(server)
        target = pts.PlexTarget("token", {}, ANIME_IDS, "pending.json")
        self.assertEqual(target.mark_watched(EP1, 1421353845), "date not set")
        # The mark went through; the entry shows up later, dated now.
        server.watched.add(EP1)
        server.history.insert(0, [f"act-{EP1}", EP1, "now"])
        target = pts.PlexTarget("token", {}, ANIME_IDS, "pending.json")
        self.assertEqual(target.pending, {EP1: 1421353845})
        self.assertEqual(target.repair_pending(), {"date set": 1})
        self.assertEqual(server.dates, {f"act-{EP1}": "2015-01-15T20:30:45Z"})
        self.assertEqual(target.pending, {})

    def test_repair_drops_unmarked_items_and_keeps_missing_entries(self):
        server = PlexServer(watched=["ep-dw-2"])
        self.patch_http(server)
        with open("pending.json", "w") as f:
            json.dump({EP1: 1, "ep-dw-2": 2}, f)
        target = pts.PlexTarget("token", {}, ANIME_IDS, "pending.json")
        counts, log = captured_log(0, target.repair_pending)
        self.assertEqual(counts, {"not marked yet": 1, "entry not found": 1})
        self.assertEqual(target.pending, {"ep-dw-2": 2})
        self.assertIn("plex-state history --item ep-dw-2 --all", log)

    def test_graphql_rate_limit_waits_and_retries(self):
        server = PlexServer()
        answers = [{"errors": [{"message": "Rate limit exceeded!",
                                "extensions": {"code": "RATE_LIMITED", "retryAfter": 60}}]}]
        graphql = server.graphql
        server.add(pts.PLEX_COMMUNITY_API,
                   lambda url, headers, data: answers.pop() if answers else graphql(url, headers, data))
        sleeps = []
        target = self.target(server)
        with mock.patch.object(pts.time, "sleep", sleeps.append):
            _, log = captured_log(0, target.mark_watched, EP1, 1421353845)
        self.assertIn(60, sleeps)
        self.assertIn("Plex GraphQL rate limit on newestHistory, waiting 60s", log)
        self.assertEqual(server.dates, {f"act-{EP1}": "2015-01-15T20:30:45Z"})

    def test_verbose_log_shows_each_step(self):
        server = PlexServer()
        self.patch_http(server)
        target = pts.PlexTarget("token", {}, ANIME_IDS, "pending.json")
        _, log = captured_log(3, target.mark_batch, [(EP1, 1421353845)])
        for line in (f"{EP1}: reading watch state", f"{EP1}: not watched",
                     f"{EP1}: journaled as pending, marking watched",
                     f"{EP1}: marked (dated now until its date is set)",
                     "reading the newest 11 watch history entries for 1 items",
                     "found the entries of 1 of 1 items",
                     f"setting dates: {EP1} entry act-{EP1} -> 2015-01-15T20:30:45Z",
                     "dates set for 1 items; 0 still pending"):
            self.assertIn(line, log)

    def test_timestamps(self):
        pts.common.TIMESTAMPS = True
        _, log = captured_log(1, pts.log, "hello")
        self.assertRegex(log, r"^\d\d:\d\d:\d\d hello$")

    def test_graphql_errors_stop(self):
        server = PlexServer()
        server.add(pts.PLEX_COMMUNITY_API, {"errors": [{"message": "nope"}]})
        target = self.target(server)
        with self.assertRaisesRegex(SystemExit, "Plex GraphQL error"):
            target.mark_watched(EP1, 1)


class PlexTargetMainTest(TempDirTestCase):
    def setUp(self):
        super().setUp()
        make_db(self.path("com.plexapp.plugins.library.db"), items=[], settings=[], views=[
            (1, "com.plexapp.agents.hama://36487/1/1?lang=en", 4, "Coil Runner", 1, 1,
             "Collector", 1421353845, None),
            (1, "com.plexapp.agents.thetvdb://1/1/1?lang=en", 4, "Unknown", 1, 1, "", 5, None),
        ])
        with open("anime_ids.json", "w") as f:
            json.dump(ANIME_IDS, f)

    def test_requires_plex_token(self):
        with self.assertRaises(SystemExit):
            self.run_main("--target", "plex", "--dry-run")
        os.environ["SYNC_TARGET"] = "trakt"
        with self.assertRaises(SystemExit):
            self.run_main("--dry-run")

    def test_dry_run_writes_nothing(self):
        server = self.patch_http(PlexServer())
        _, err = self.run_main("--target", "plex", "--plex-token", "t", "--dry-run")
        self.assertIn("Coil Runner S01E01: would be marked (at 2015-01-15T20:30:45Z, plex id ep-dw-1)", err)
        self.assertIn("Unknown S01E01: not found on Plex (show not found on Plex)", err)
        self.assertEqual(server.urls("/actions/scrobble"), [])
        self.assertEqual(len(server.urls("/userState")), 1)
        self.assertFalse(os.path.exists("plex_sent.json"))
        self.assertFalse(os.path.exists("simkl_sent.json"))

    def test_dry_run_reports_items_already_watched(self):
        self.patch_http(PlexServer(watched=[EP1]))
        _, err = self.run_main("--target", "plex", "--plex-token", "t", "--dry-run")
        self.assertIn("Coil Runner S01E01: already watched (at 2015-01-15T20:30:45Z, plex id ep-dw-1)",
                      err)
        self.assertIn("Dry run: 1 already watched, 1 not found on Plex", err)

    def test_marks_records_and_skips_on_rerun(self):
        server = self.patch_http(PlexServer())
        os.environ["SYNC_TARGET"] = "plex"
        _, err = self.run_main("--plex-token", "t")
        self.assertIn("Done: 1 not found on Plex, 1 watched", err)
        self.assertEqual(server.dates, {"act-ep-dw-1": "2015-01-15T20:30:45Z"})
        with open("plex_sent.json") as f:
            self.assertEqual(list(json.load(f)), ["uuid-1"])
        with open("plex_not_found.json") as f:
            self.assertEqual([v["label"] for v in json.load(f)["uuid-1"].values()],
                             ["Unknown S01E01"])
        self.assertFalse(os.path.exists("simkl_sent.json"))
        self.assertFalse(os.path.exists("simkl_not_found.json"))
        _, err = self.run_main("--plex-token", "t")
        self.assertIn("sent in earlier runs: 1 items", err)
        self.assertIn("not found on Plex in the last 30 days: 1 items", err)
        self.assertEqual(len(server.urls("/actions/scrobble")), 1)

    def test_interrupted_run_keeps_progress_and_resumes(self):
        server = self.patch_http(PlexServer())
        os.environ["SYNC_TARGET"] = "plex"
        calls = []
        mark = pts.PlexTarget.mark_batch

        def interrupt_second(target, items):
            calls.append(items)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return mark(target, items)
        with open("pending.json", "w") as f:
            json.dump({}, f)
        make_db(self.path("two.db"), items=[], settings=[], views=[
            (1, f"com.plexapp.agents.hama://36487/1/{n}?lang=en", 4, "Coil Runner", 1, n, "",
             1421353845 + n, None) for n in (1, 2)])
        with mock.patch.object(pts.PlexTarget, "mark_batch", interrupt_second):
            with self.assertRaises(KeyboardInterrupt):
                self.run_main("--plex-token", "t", "--db", "two.db", "--plex-batch", "1")
        with open("plex_sent.json") as f:
            self.assertEqual(len(json.load(f)["uuid-1"]), 1)  # the first item is kept
        calls.clear()
        _, err = self.run_main("--plex-token", "t", "--db", "two.db")
        self.assertIn("sent in earlier runs: 1 items", err)
        self.assertEqual(len(server.urls("/actions/scrobble")), 2)

    def test_items_of_a_batch_are_dated_with_one_mutation(self):
        server = self.patch_http(PlexServer())
        make_db(self.path("two.db"), items=[], settings=[], views=[
            (1, f"com.plexapp.agents.hama://36487/1/{n}?lang=en", 4, "Coil Runner", 1, n, "",
             1421353845 + n, None) for n in (1, 2)])
        _, err = self.run_main("--target", "plex", "--plex-token", "t", "--db", "two.db")
        self.assertIn("Done: 2 watched", err)
        self.assertEqual(server.mutations, [2])

    def test_dry_run_reports_pending_items(self):
        self.patch_http(PlexServer())
        with open("plex_pending.json", "w") as f:
            json.dump({EP1: 1}, f)
        _, err = self.run_main("--target", "plex", "--plex-token", "t", "--dry-run")
        self.assertIn("1 items from an earlier run are marked but their date is not set yet", err)

    def test_record_paths_can_be_overridden(self):
        self.patch_http(PlexServer())
        self.run_main("--target", "plex", "--plex-token", "t", "--sent-cache", "mine.json",
                      "--not-found", "missing.json")
        self.assertTrue(os.path.exists("mine.json"))
        self.assertTrue(os.path.exists("missing.json"))
        self.assertFalse(os.path.exists("plex_sent.json"))

    def test_simkl_is_not_contacted(self):
        server = self.patch_http(PlexServer())
        self.run_main("--target", "plex", "--plex-token", "t", "--simkl-client-id", "c",
                      "--simkl-token", "s", "--dry-run")
        self.assertEqual(server.urls("simkl.com"), [])


if __name__ == "__main__":
    unittest.main()
