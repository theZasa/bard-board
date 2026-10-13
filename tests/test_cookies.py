"""YouTube cookies for servers YouTube asks to sign in: upload, checks, use by yt-dlp, error hints."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.media import (BOT_CHECK_HINT, EXPIRED_COOKIES_HINT, COOKIES_HEADER, MediaError, Resolver,
                       validate_cookies)

# Made-up values in the format the browser extensions export.
GOOD = (
    "# Netscape HTTP Cookie File\r\n"
    "# This is a generated file! Do not edit.\r\n"
    "\r\n"
    ".youtube.com\tTRUE\t/\tTRUE\t1800000000\tPREF\tf6=40000000\r\n"
    "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1800000000\tLOGIN_INFO\tAFmmF2swRQ-erfunden\r\n"
    ".youtube.com\tTRUE\t/\tFALSE\t1800000000\tSID\terfunden\r\n"
)
BOT_ERROR = ("ERROR: [youtube] baVWC2_O-_A: Sign in to confirm you’re not a bot. Use --cookies-from-browser "
             "or --cookies for the authentication.")


class ValidateTest(unittest.TestCase):
    def test_export_from_a_signed_in_browser_is_accepted(self):
        text = validate_cookies(GOOD.encode())
        self.assertTrue(text.startswith(COOKIES_HEADER + "\n"))
        self.assertEqual(text.count(COOKIES_HEADER), 1)
        self.assertNotIn("\r", text)
        self.assertIn("#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1800000000\tLOGIN_INFO", text)

    def test_header_is_added_when_missing(self):
        body = "\n".join(GOOD.splitlines()[3:])
        self.assertTrue(validate_cookies(body.encode()).startswith(COOKIES_HEADER))

    def test_useless_files_are_rejected_with_a_reason(self):
        cases = {
            "keine Textdatei": b"\xff\xfe\x00binary",
            "Netscape-Format": b"<html>nope</html>",
            "keine Cookies von youtube.com": b".example.org\tTRUE\t/\tTRUE\t0\tSID\tx\n",
            "Anmelde-Cookies": b".youtube.com\tTRUE\t/\tTRUE\t0\tPREF\tx\n",
            "zu groß": b"#" * 1_000_001,
        }
        for reason, data in cases.items():
            with self.subTest(reason), self.assertRaisesRegex(MediaError, reason):
                validate_cookies(data)


class FakeYDL:
    """Stands in for yt_dlp.YoutubeDL: records the options and what the cookie file held at run time."""

    seen: list[dict] = []
    error: Exception | None = None

    def __init__(self, opts: dict) -> None:
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, target, download=False):
        cookiefile = self.opts.get("cookiefile")
        FakeYDL.seen.append({"cookiefile": cookiefile,
                             "content": Path(cookiefile).read_text("utf-8") if cookiefile else None})
        if FakeYDL.error is not None:
            raise FakeYDL.error
        return {"webpage_url": target, "title": "Test", "url": "https://media.example/a.webm", "duration": 3}


class ResolverTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cookies = self.tmp / "youtube-cookies.txt"
        self.resolver = Resolver(self.tmp / "cache", cookies_file=self.cookies)
        FakeYDL.seen, FakeYDL.error = [], None
        patcher = mock.patch("yt_dlp.YoutubeDL", FakeYDL)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def test_each_run_gets_its_own_copy_which_is_removed_afterwards(self):
        self.cookies.write_text(validate_cookies(GOOD.encode()), "utf-8")
        asyncio.run(self.resolver.stream("https://www.youtube.com/watch?v=baVWC2_O-_A"))
        asyncio.run(self.resolver.lookup("https://www.youtube.com/watch?v=baVWC2_O-_A"))
        first, second = FakeYDL.seen
        self.assertNotEqual(first["cookiefile"], str(self.cookies), "never the uploaded file itself")
        self.assertNotEqual(first["cookiefile"], second["cookiefile"])
        self.assertEqual(first["content"], self.cookies.read_text("utf-8"))
        self.assertFalse(Path(first["cookiefile"]).exists())

    def test_without_cookies_nothing_changes(self):
        asyncio.run(self.resolver.stream("https://www.youtube.com/watch?v=baVWC2_O-_A"))
        self.assertIsNone(FakeYDL.seen[0]["cookiefile"])
        self.assertNotIn("--cookies", self.resolver.download_command("https://youtu.be/x"))

    def test_downloader_gets_a_copy_too(self):
        self.cookies.write_text(validate_cookies(GOOD.encode()), "utf-8")
        cmd = self.resolver.download_command("https://youtu.be/x")
        copy = Path(cmd[cmd.index("--cookies") + 1])
        self.assertTrue(copy.exists())
        self.assertNotEqual(copy, self.cookies)

    def test_bot_check_explains_what_to_do(self):
        import yt_dlp

        FakeYDL.error = yt_dlp.utils.DownloadError(BOT_ERROR)
        with self.assertRaises(MediaError) as caught:
            asyncio.run(self.resolver.stream("https://www.youtube.com/watch?v=baVWC2_O-_A"))
        self.assertEqual(str(caught.exception), BOT_CHECK_HINT)
        self.cookies.write_text(validate_cookies(GOOD.encode()), "utf-8")
        with self.assertRaises(MediaError) as caught:
            asyncio.run(self.resolver.stream("https://www.youtube.com/watch?v=baVWC2_O-_A"))
        self.assertEqual(str(caught.exception), EXPIRED_COOKIES_HINT, "with cookies: they are the problem")


class WebTest(unittest.TestCase):
    def put(self, app, data: bytes) -> tuple[int, dict]:
        boundary = "grenze"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"cookies.txt\"\r\n"
                f"Content-Type: text/plain\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
        sent = []

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "method": "PUT", "path": "/api/youtube-cookies", "raw_path": b"/api/youtube-cookies",
                 "query_string": b"", "scheme": "http", "server": ("localhost", 8080), "client": ("127.0.0.1", 1),
                 "http_version": "1.1", "root_path": "",
                 "headers": [(b"host", b"localhost:8080"), (b"content-length", str(len(body)).encode()),
                             (b"content-type", f"multipart/form-data; boundary={boundary}".encode())]}
        asyncio.run(app(scope, receive, send))
        status = next(m for m in sent if m["type"] == "http.response.start")["status"]
        payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        return status, json.loads(payload or b"{}")

    def test_upload_status_and_remove(self):
        from app.web import create_app
        from tests.test_auth import asgi
        from tests.test_commands import make_board

        with tempfile.TemporaryDirectory() as tmp:
            board = make_board(Path(tmp))
            app = create_app(board)
            self.assertEqual(asgi(app, "GET", "/api/youtube-cookies")[1], {"present": False, "since": None})
            status, reply = self.put(app, b"<html>falsch</html>")
            self.assertEqual(status, 422)
            self.assertIn("Netscape", reply["error"])
            status, reply = self.put(app, GOOD.encode())
            self.assertEqual(status, 200)
            self.assertTrue(reply["present"])
            self.assertNotIn("LOGIN_INFO", json.dumps(reply), "the content never comes back")
            stored = board.resolver.cookies_file
            self.assertEqual(stored.stat().st_mode & 0o777, 0o600)
            self.assertIn("hat YouTube-Cookies hinterlegt", board.activity[-1].text)
            self.assertEqual(asgi(app, "DELETE", "/api/youtube-cookies")[1], {"present": False, "since": None})
            self.assertFalse(stored.exists())


if __name__ == "__main__":
    unittest.main()
