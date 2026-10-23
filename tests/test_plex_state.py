import os
import unittest

from tests.support import FakeHttp, TempDirTestCase, pts


class AccountServer(FakeHttp):
    """Fake Discover watch state and community GraphQL history for one account."""

    def __init__(self):
        super().__init__([
            (pts.PLEX_ACCOUNT_API, {"uuid": "uuid-1"}),
            ("/actions/unscrobble", self.unscrobble),
            ("/actions/scrobble", self.scrobble),
            ("/userState", self.user_state),
            ("/matches?", {"MediaContainer": {"Metadata": [{"ratingKey": "show-1"}]}}),
            ("/library/metadata/show-1/children", {"MediaContainer": {
                "Metadata": [{"index": 1, "ratingKey": "season-1"}], "totalSize": 1}}),
            ("/library/metadata/season-1/children", {"MediaContainer": {
                "Metadata": [{"index": 1, "ratingKey": "ep-1"}, {"index": 2, "ratingKey": "ep-2"}],
                "totalSize": 2}}),
            ("/library/metadata/show-1", {"MediaContainer": {"Metadata": [
                {"title": "Coil Runner", "year": 2016, "leafCount": 2}]}}),
            (pts.PLEX_COMMUNITY_API, self.graphql),
        ])
        self.view_counts = {}
        self.history = []  # newest first: dicts with id, date, item

    def scrobble(self, url, headers, data):
        key = url.split("key=")[1]
        self.view_counts[key] = self.view_counts.get(key, 0) + 1
        self.history.insert(0, {"id": f"act-{len(self.history)}", "date": "now", "item": key})

    def unscrobble(self, url, headers, data):
        self.view_counts[url.split("key=")[1]] = 0

    def user_state(self, url, headers, data):
        key = url.split("/metadata/")[1].split("/")[0]
        return {"MediaContainer": {"UserState": [{"viewCount": self.view_counts.get(key, 0)}]}}

    def graphql(self, url, headers, data):
        query, variables = data["query"], data["variables"]
        if "updateActivity" in query:
            next(h for h in self.history if h["id"] == variables["id"])["date"] = \
                variables["input"]["date"]
            return {"data": {"updateActivity": {"id": variables["id"]}}}
        if "removeActivity" in query:
            self.history = [h for h in self.history if h["id"] != variables["input"]["id"]]
            return {"data": {"removeActivity": True}}
        nodes = [{"id": h["id"], "date": h["date"],
                  "metadataItem": {"id": h["item"], "title": "t", "type": "EPISODE"}}
                 for h in self.history[:variables["first"]]]
        return {"data": {"user": {"watchHistory": {
            "nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}


class ToolTest(TempDirTestCase):
    def setUp(self):
        super().setUp()
        os.environ["PLEX_TOKEN"] = "token"
        self.server = self.patch_http(AccountServer())

    def run_tool(self, *argv):
        out, _ = self.run_main(*argv, command="plex-state")
        return out

    def test_needs_token(self):
        del os.environ["PLEX_TOKEN"]
        with self.assertRaises(SystemExit):
            self.run_tool("history")

    def test_changes_need_write(self):
        out = self.run_tool("mark", "ep-1", "--date", "2015-01-15T20:30:45Z")
        self.assertIn("would mark ep-1 watched (add --write to do it)", out)
        out += self.run_tool("remove", "act-0") + self.run_tool("set-date", "act-0", "2016")
        self.assertEqual(self.server.urls("/actions/"), [])
        self.assertEqual(self.server.urls(pts.PLEX_COMMUNITY_API), [])

    def test_mark_with_date_and_history(self):
        out = self.run_tool("--write", "mark", "ep-1", "--date", "2015-01-15T20:30:45Z")
        self.assertIn("done: dated history entry act-0 2015-01-15T20:30:45Z", out)
        self.assertEqual(self.server.history[0]["date"], "2015-01-15T20:30:45Z")
        out = self.run_tool("history", "--item", "ep-1", "--all")
        self.assertEqual(out.split(), ["act-0", "2015-01-15T20:30:45Z", "ep-1", "EPISODE", "t"])
        self.assertEqual(self.run_tool("history", "--item", "ep-2"), "")

    def test_unmark_removes_leftover_history(self):
        self.run_tool("--write", "mark", "ep-1", "ep-2")
        out = self.run_tool("--write", "unmark", "ep-1", "--remove-history")
        self.assertIn("done: removed history entry act-0", out)
        self.assertEqual([h["item"] for h in self.server.history], ["ep-2"])
        self.assertEqual(self.server.view_counts, {"ep-1": 0, "ep-2": 1})

    def test_match_tree_and_state(self):
        self.assertIn("tvdb://303415: show-1 Coil Runner", self.run_tool("match", "--tvdb", "303415"))
        self.server.view_counts["ep-2"] = 1
        out = self.run_tool("tree", "show-1")
        self.assertIn("Coil Runner (2016): leafCount 2", out)
        self.assertIn("S01E01 ep-1 viewCount 0", out)
        self.assertIn("S01E02 ep-2 viewCount 1", out)
        self.assertIn("ep-2 {'viewCount': 1}", self.run_tool("state", "ep-2"))


if __name__ == "__main__":
    unittest.main()
