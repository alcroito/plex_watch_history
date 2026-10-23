"""Shared fixtures: a synthetic Plex database, a fake HTTP layer and an isolated test case."""

import contextlib
import io
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from plexhistory import (cli, common, export, filters, listing, plex, plex_state,  # noqa: E402
                         plexdb, simkl, tmdb)


class Package:
    """Read access to every plexhistory module's names, so tests can write pts.<name>.

    Patch functions where they are looked up: HTTP and VERBOSITY live in common.
    """

    modules = (common, plexdb, plex, tmdb, filters, simkl, export, listing, plex_state, cli)

    def __getattr__(self, name):
        for module in self.modules:
            if hasattr(module, name):
                return getattr(module, name)
        raise AttributeError(name)


pts = Package()

# Settings main() reads from the environment; cleared so the developer's shell can't leak in.
ENV_KEYS = ("PLEX_DB_PATH", "PLEX_TOKEN", "SIMKL_CLIENT_ID", "SIMKL_ACCESS_TOKEN",
            "SIMKL_REFRESH_TOKEN", "SIMKL_ACCOUNT_ID", "SIMKL_NOT_FOUND_DAYS", "SIMKL_CACHE_DAYS",
            "VERBOSITY", "LOG_TIMESTAMPS", "TMDB_API_KEY", "TMDB_CACHE_DAYS", "ANIME_IDS_PATH",
            "FILTER_GUID_REGEX", "FILTER_TITLE_REGEX", "FILTER_TITLE", "FILTER_YEAR", "FILTER_START", "FILTER_COUNT")

SCHEMA = """
create table metadata_items (id integer primary key, guid varchar, metadata_type integer,
    title varchar, parent_id integer, "index" integer, year integer);
create table metadata_item_views (id integer primary key, account_id integer, guid varchar,
    metadata_type integer, grandparent_title varchar, parent_index integer, "index" integer,
    title varchar, viewed_at integer, grandparent_guid varchar, originally_available_at integer);
create table metadata_item_settings (id integer primary key, account_id integer, guid varchar,
    view_count integer, view_offset integer, last_viewed_at integer, created_at integer);
"""

# Library items still present: a plex:// show with one season and one episode.
ITEMS = [
    (1, "plex://show/show1", 2, "Present Show", None, None),
    (2, "plex://season/season1", 3, "Season 1", 1, 1),
    (3, "plex://episode/ep-present", 4, "Pilot", 2, 1),
]

# (account, guid, metadata_type, grandparent_title, season, episode, title, viewed_at,
#  grandparent_guid)
VIEWS = [
    (1, "com.plexapp.agents.thetvdb://85625/1/1?lang=en", 4, "The Help Desk", 1, 1, "Monday Backlog", 300, None),
    (1, "com.plexapp.agents.thetvdb://85625/1/1?lang=en", 4, "The Help Desk", 1, 1, "Monday Backlog", 100, None),
    (1, "com.plexapp.agents.thetvdb://85625/1/2?lang=en", 4, "The Help Desk", 1, 2, "Printer Fire", 200, None),
    (1, "com.plexapp.agents.imdb://tt1156099?lang=en", 1, None, None, None, "Neon Harbor", 400, None),
    (1, "local://5938", 4, "The Night Ledger", 1, 1, "", 500, "com.plexapp.agents.thetvdb://431235?lang=en"),
    (1, "plex://episode/ep-gone", 4, "The Quiet Hotel", 1, 3, "Episode 3", 600, "plex://show/quiethotel"),
    (1, "plex://episode/ep-present", 4, "Present Show", 1, 1, "Pilot", 700, "plex://show/show1"),
    (2, "com.plexapp.agents.thetvdb://85625/1/3?lang=en", 4, "The Help Desk", 1, 3, "Coin Toss", 800, None),
]

# (account, guid, view_count, last_viewed_at)
SETTINGS = [
    (1, "com.plexapp.agents.thetvdb://85625/1/1?lang=en", 2, 300),
    (1, "com.plexapp.agents.thetvdb://85625?lang=en", 1, 300),
    (1, "com.plexapp.agents.hama://anidb-53101/1/6?lang=en", 1, 1000),
    (1, "com.plexapp.agents.hama://anidb-53101/0/101?lang=en", 1, 1001),
    (1, "com.plexapp.agents.hama://36773/1/10?lang=en", 1, 1100),
    (1, "com.plexapp.agents.hama://tvdb-733880/2/1?lang=en", 1, 1200),
    (1, "com.plexapp.agents.lastfm://Artist/Album/2?lang=en", 1, 1300),
    (1, "com.plexapp.agents.none://4922?lang=xn", 1, 1400),
    (1, "plex://movie/mv1", 1, 1500),
    (1, "plex://episode/ep-unknown", 1, 1600),
    (1, "plex://movie/in-progress", 0, 1700),
]


def make_db(path, items=ITEMS, views=VIEWS, settings=SETTINGS):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.executemany("insert into metadata_items (id, guid, metadata_type, title, parent_id, "
                   "\"index\") values (?, ?, ?, ?, ?, ?)", items)
    db.executemany(
        "insert into metadata_item_views (account_id, guid, metadata_type, grandparent_title, "
        "parent_index, \"index\", title, viewed_at, grandparent_guid) "
        "values (?, ?, ?, ?, ?, ?, ?, ?, ?)", views)
    db.executemany(
        "insert into metadata_item_settings (account_id, guid, view_count, last_viewed_at) "
        "values (?, ?, ?, ?)", settings)
    db.commit()
    db.close()
    return path


def plex_item(kind, title, ids=(), year=None, show=None, season=None, episode=None):
    """Plex metadata provider response for one item."""
    m = {"type": kind, "title": title, "year": year,
         "Guid": [{"id": i} for i in ids]}
    if show:
        m.update(grandparentGuid=f"plex://show/{show}", parentIndex=season, index=episode)
    return {"MediaContainer": {"Metadata": [m]}}


# Plex metadata provider content for the fixture's plex:// guids. ep-gone and ep-unknown 404.
PLEX_ITEMS = {
    "ep-present": plex_item("episode", "Pilot", show="show1", season=1, episode=1),
    "show1": plex_item("show", "Present Show", ["tvdb://111", "tmdb://222"], year=2020),
    "quiethotel": plex_item("show", "The Quiet Hotel",
                          ["imdb://tt2394302", "tmdb://84481", "tvdb://262699"], year=2016),
    "mv1": plex_item("movie", "Some Movie", ["imdb://tt0000042", "tmdb://42"], year=2001),
}


class FakeHttp:
    """Stands in for pts.http_json: routes URLs by substring and records every call."""

    def __init__(self, routes=None, plex=PLEX_ITEMS):
        self.routes = list(routes or [])
        self.plex = plex
        self.calls = []

    def add(self, fragment, response):
        self.routes.insert(0, (fragment, response))

    def __call__(self, url, headers, data=None, retries=3):
        self.calls.append((url, headers, data))
        for fragment, response in self.routes:
            if fragment in url:
                return response(url, headers, data) if callable(response) else response
        if url.startswith(pts.PLEX_METADATA_API):
            return self.plex.get(url.rpartition("/")[2].split("?")[0])
        raise AssertionError(f"unexpected request {url}")

    def location(self, url, headers):
        """Stands in for pts.http_location: /redirect routes answer with a URL or None."""
        return self(url, headers)

    def urls(self, fragment=""):
        return [u for u, _, _ in self.calls if fragment in u]


class TempDirTestCase(unittest.TestCase):
    """Runs each test in its own directory with a clean environment and no sleeping."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, cwd)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        timestamps = mock.patch.object(common, "TIMESTAMPS", False)
        timestamps.start()
        self.addCleanup(timestamps.stop)
        rate_limit = mock.patch.object(common, "RATE_LIMIT", None)
        rate_limit.start()
        self.addCleanup(rate_limit.stop)
        sleep = mock.patch.object(pts.time, "sleep", lambda *_: None)
        sleep.start()
        self.addCleanup(sleep.stop)
        old_verbosity = common.VERBOSITY
        common.VERBOSITY = 0
        self.addCleanup(setattr, common, "VERBOSITY", old_verbosity)

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def patch_http(self, fake):
        for name, replacement in (("http_json", fake),
                                  ("http_location", getattr(fake, "location", None))):
            patcher = mock.patch.object(common, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        return fake

    def run_main(self, *argv, command="export"):
        """Run a plex_history.py subcommand with argv; return (stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            cli.main([command, *argv])
        return out.getvalue(), err.getvalue()


def captured_log(level, fn, *args, **kwargs):
    """Call fn at the given verbosity; return (result, stderr text)."""
    err = io.StringIO()
    old = common.VERBOSITY
    common.VERBOSITY = level
    try:
        with contextlib.redirect_stderr(err):
            result = fn(*args, **kwargs)
    finally:
        common.VERBOSITY = old
    return result, err.getvalue()
