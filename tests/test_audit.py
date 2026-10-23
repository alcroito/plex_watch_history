import json
import unittest

from tests.support import FakeHttp, TempDirTestCase, make_db

HAMA = "com.plexapp.agents.hama://"
# Simkl ids per AniDB id: HSA seasons 1-3, the Witch Seven OVA.
SIMKL_IDS = {39511: 1, 43154: 2, 45726: 3, 14011: 4}


def entry(count, tvdb_season, dated=None, titled=None):
    return [{"episode": n, "type": "episode", "title": (titled or {}).get(n, f"T{n}"),
             "date": (dated or {}).get(n), "tvdb": {"season": tvdb_season, "episode": n}}
            for n in range(1, count + 1)]


EPISODES = {1: entry(13, 1), 2: entry(25, 2, dated={25: "2017-09-30T17:30:00+09:00"}),
            3: entry(25, 3, titled={13: "Dorm Day"}), 4: entry(2, 0)}


class AuditTest(TempDirTestCase):
    def setUp(self):
        super().setUp()
        make_db("com.plexapp.plugins.library.db", items=[], settings=[], views=[
            (1, HAMA + "anidb-43154/1/2?lang=en", 4, "HSA 2018", 1, 13, "Dorm Day",
             1530434000, HAMA + "anidb-45726?lang=en"),
            (1, HAMA + "anidb-43154/1/38?lang=en", 4, "HSA 2017", 1, 38, "Episode 38",
             1506847000, HAMA + "anidb-43154?lang=en"),
            (1, HAMA + "anidb-14011/1/3?lang=en", 4, "Witch Seven", 1, 3, "", 1560000000,
             HAMA + "anidb-14011?lang=en")])
        with open("anime_ids.json", "w") as f:
            json.dump({"39511": {"tvdb_id": 338023, "tvdb_season": 1},
                       "43154": {"tvdb_id": 338023, "tvdb_season": 2},
                       "45726": {"tvdb_id": 338023, "tvdb_season": 3}}, f)
        self.http = self.patch_http(FakeHttp([
            ("/redirect", lambda url, headers, data: next(
                (f"https://simkl.com/anime/{simkl}/x" for anidb, simkl in SIMKL_IDS.items()
                 if f"anidb={anidb}&" in url), None)),
            ("/anime/episodes/", lambda url, headers, data:
                EPISODES[int(url.split("/anime/episodes/")[1].split("?")[0])]),
        ]))

    def test_reports_all_three_kinds(self):
        out, _ = self.run_main("--simkl-client-id", "cid", command="audit")
        self.assertIn("Plays whose guid disagrees with their show guid or index (1)", out)
        self.assertIn("anidb-45726 episode 13 by episode title", out)
        self.assertIn("Witch Seven [anidb:14011]: episode 3 is past the end of its 2-episode "
                      "Simkl entry", out)
        self.assertIn("HSA 2017 S01E38 -> anidb-43154 episode 25: checked by air date", out)
        for _, headers, data in self.http.calls:
            self.assertNotIn("Authorization", headers)
            self.assertIsNone(data)

    def test_needs_client_id(self):
        with self.assertRaises(SystemExit):
            self.run_main(command="audit")


if __name__ == "__main__":
    unittest.main()
