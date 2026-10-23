import io
import json
import os
import stat
import unittest
import urllib.error
from unittest import mock

from tests.support import TempDirTestCase, captured_log, common, pts


class EnvFileTest(TempDirTestCase):
    def test_read_env_file_parses_comments_export_and_quotes(self):
        with open(".env", "w") as f:
            f.write("# comment\n\nexport A=1\nB = 'two'\nC=\"three\"\nnot a setting\nD=x=y\n")
        self.assertEqual(pts.read_env_file(".env"),
                         {"A": "1", "B": "two", "C": "three", "D": "x=y"})

    def test_read_env_file_missing_returns_empty(self):
        self.assertEqual(pts.read_env_file("missing.env"), {})

    def test_update_env_file_replaces_keys_and_keeps_other_lines(self):
        with open(".env", "w") as f:
            f.write("# keep me\nexport PLEX_TOKEN=p\nSIMKL_ACCESS_TOKEN=old\n")
        pts.update_env_file(".env", {"SIMKL_ACCESS_TOKEN": "new", "SIMKL_REFRESH_TOKEN": "r"})
        with open(".env") as f:
            self.assertEqual(f.read(), "# keep me\nexport PLEX_TOKEN=p\n"
                                       "SIMKL_ACCESS_TOKEN=new\nSIMKL_REFRESH_TOKEN=r\n")

    def test_update_env_file_is_private(self):
        with open(".env", "w") as f:
            f.write("A=1\n")
        os.chmod(".env", 0o644)
        pts.update_env_file(".env", {"B": "2"})
        pts.update_env_file("new.env", {"B": "2"})
        self.assertEqual(stat.S_IMODE(os.stat(".env").st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat("new.env").st_mode), 0o600)
        self.assertFalse(os.path.exists(".env.tmp"))

    def test_update_env_file_is_private_despite_leftover_temp_file(self):
        # An interrupted earlier write can leave a world-readable .env.tmp behind.
        with open(".env.tmp", "w") as f:
            f.write("stale")
        os.chmod(".env.tmp", 0o644)
        pts.update_env_file(".env", {"SIMKL_ACCESS_TOKEN": "secret"})
        self.assertEqual(stat.S_IMODE(os.stat(".env").st_mode), 0o600)


class WriteFileAtomicTest(TempDirTestCase):
    def test_interrupted_write_keeps_previous_file(self):
        pts.write_file_atomic("cache.json", '{"a": 1}')
        with mock.patch.object(pts.os, "replace", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                pts.write_file_atomic("cache.json", '{"a": 1, "b": 2}')
        with open("cache.json") as f:
            self.assertEqual(json.load(f), {"a": 1})

    def test_replaces_content(self):
        pts.write_file_atomic("f.txt", "one")
        pts.write_file_atomic("f.txt", "two")
        with open("f.txt") as f:
            self.assertEqual(f.read(), "two")
        self.assertFalse(os.path.exists("f.txt.tmp"))


class FakeResponse(io.BytesIO):
    status = 200

    def __init__(self, body, headers=None):
        super().__init__(body)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code, body=b"{}", headers=None):
    return urllib.error.HTTPError("https://x", code, "err", headers or {}, io.BytesIO(body))


class HttpJsonTest(TempDirTestCase):
    def test_returns_json(self):
        with mock.patch.object(pts.urllib.request, "urlopen",
                               return_value=FakeResponse(b'{"ok": 1}')) as urlopen:
            self.assertEqual(pts.http_json("https://x/a?q=1", {"H": "v"}, {"d": 1}), {"ok": 1})
        request = urlopen.call_args[0][0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("User-agent"), "plex-history-to-simkl/1.0")
        self.assertEqual(request.get_header("H"), "v")
        self.assertEqual(json.loads(request.data), {"d": 1})

    def test_rate_limit_headers_are_kept(self):
        self.assertIsNone(pts.rate_limit_text())
        response = FakeResponse(b"{}", {"X-RateLimit-Remaining": "135", "X-RateLimit-Limit": "500"})
        with mock.patch.object(pts.urllib.request, "urlopen", return_value=response):
            pts.http_json("https://x", {})
        self.assertTrue(pts.rate_limit_text().startswith("135 of 500 Simkl requests left today"))
        with mock.patch.object(pts.urllib.request, "urlopen", return_value=FakeResponse(b"{}")):
            pts.http_json("https://x", {})
        self.assertEqual(common.RATE_LIMIT, ("135", "500"))

    def test_404_returns_none(self):
        with mock.patch.object(pts.urllib.request, "urlopen", side_effect=http_error(404)):
            self.assertIsNone(pts.http_json("https://x", {}))

    def test_429_waits_and_retries(self):
        responses = [http_error(429, headers={"Retry-After": "2"}), FakeResponse(b'{"ok": 1}')]
        sleeps = []
        with mock.patch.object(pts.urllib.request, "urlopen", side_effect=responses), \
                mock.patch.object(pts.time, "sleep", sleeps.append):
            result, log = captured_log(0, pts.http_json, "https://x", {})
        self.assertEqual(result, {"ok": 1})
        self.assertEqual(sleeps, [2])
        self.assertIn("429 received, waiting 2s", log)

    def test_server_errors_and_dropped_connections_are_retried(self):
        responses = [http_error(503), urllib.error.URLError("connection reset"),
                     FakeResponse(b'{"ok": 1}')]
        sleeps = []
        with mock.patch.object(pts.urllib.request, "urlopen", side_effect=responses), \
                mock.patch.object(pts.time, "sleep", sleeps.append):
            result, log = captured_log(0, pts.http_json, "https://x/a?token=1", {})
        self.assertEqual(result, {"ok": 1})
        self.assertEqual(sleeps, [5, 15])
        self.assertIn("HTTP 503 for https://x/a, retrying in 5s", log)
        self.assertIn("connection reset for https://x/a, retrying in 15s", log)
        self.assertNotIn("token", log)

    def test_server_errors_stop_after_the_retries(self):
        with mock.patch.object(pts.urllib.request, "urlopen", side_effect=http_error(503)), \
                mock.patch.object(pts.time, "sleep", lambda s: None):
            with self.assertRaises(pts.HttpError) as ctx:
                captured_log(0, pts.http_json, "https://x", {})
        self.assertEqual(ctx.exception.code, 503)

    def test_429_daily_quota_stops(self):
        error = http_error(429, headers={"Retry-After": "40000"})
        with mock.patch.object(pts.urllib.request, "urlopen", side_effect=error):
            with self.assertRaisesRegex(SystemExit, "quota exhausted"):
                pts.http_json("https://x", {})

    def test_other_errors_raise_http_error_with_body(self):
        with mock.patch.object(pts.urllib.request, "urlopen",
                               side_effect=http_error(400, b'{"error": "max_items"}')):
            with self.assertRaises(pts.HttpError) as ctx:
                pts.http_json("https://x/path?secret=1", {})
        self.assertEqual(ctx.exception.code, 400)
        self.assertIn("max_items", ctx.exception.body)
        self.assertNotIn("secret", str(ctx.exception))

    def test_debug_log_omits_query_string(self):
        with mock.patch.object(pts.urllib.request, "urlopen", return_value=FakeResponse(b"{}")):
            _, log = captured_log(3, pts.http_json, "https://x/a?api_key=secret", {})
        self.assertEqual(log.strip(), "GET https://x/a")


class PostFormTest(TempDirTestCase):
    def test_returns_status_and_json_for_errors(self):
        with mock.patch.object(pts.urllib.request, "urlopen",
                               side_effect=http_error(400, b'{"error": "slow_down"}')):
            self.assertEqual(pts.post_form("https://x", {"a": 1}), (400, {"error": "slow_down"}))

    def test_non_json_error_body(self):
        with mock.patch.object(pts.urllib.request, "urlopen",
                               side_effect=http_error(500, b"<html>")):
            self.assertEqual(pts.post_form("https://x", {}), (500, {}))

    def test_success(self):
        with mock.patch.object(pts.urllib.request, "urlopen",
                               return_value=FakeResponse(b'{"access_token": "t"}')) as urlopen:
            self.assertEqual(pts.post_form("https://x", {"a": "b c"}), (200, {"access_token": "t"}))
        self.assertEqual(urlopen.call_args[0][0].data, b"a=b+c")


class ProgressTest(unittest.TestCase):
    def test_logs_about_every_ten_percent_and_at_the_end(self):
        def run():
            progress = pts.Progress(1000, "done", lambda: "x")
            for _ in range(1000):
                progress.step()
        _, log = captured_log(1, run)
        self.assertEqual(len(log.splitlines()), 10)
        self.assertIn("done 1000/1000 (100%, x)", log)

    def test_verbose_logs_every_fifty(self):
        def run():
            progress = pts.Progress(120, "done")
            for _ in range(120):
                progress.step()
        _, log = captured_log(2, run)
        self.assertEqual([line.split()[1] for line in log.splitlines()],
                         ["50/120", "100/120", "120/120"])


if __name__ == "__main__":
    unittest.main()
