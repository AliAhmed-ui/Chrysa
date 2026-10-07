"""The online subtitle downloader, with the network and ffprobe replaced by stubs."""

import os
import unittest
from unittest import mock

from tests.helpers import MB, LibraryCase, make_tree
from theia import subtitles


class FetchMissing(LibraryCase):
    def setUp(self):
        super().setUp()
        make_tree(self.media, {
            "Heat (1995)/Heat.1995.mkv": 2 * MB,
            "Deadpool (2016)/Deadpool.2016.mkv": 2 * MB,
            "Deadpool (2016)/Deadpool.2016.srt": b"1\n",
            "Friends/Season 2/Friends.S02E18.mkv": 2 * MB,
        })
        self.default_duration = 5000.0
        self.scan()
        patcher = mock.patch.object(subtitles, "has_embedded_subtitles", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.lines: list[tuple[str, str]] = []

    def run_fetch(self, **kwargs):
        kwargs.setdefault("search", lambda path: b"1\n00:00:01,000 --> 00:00:02,000\nHi\n")
        kwargs.setdefault("online", lambda: True)
        return subtitles.fetch_missing_subtitles(
            self.library.subtitle_candidates(), lambda level, text: self.lines.append((level, text)), **kwargs
        )

    def test_downloads_only_missing_and_saves_beside_video(self):
        summary = self.run_fetch()
        self.assertEqual((summary.downloaded, summary.skipped), (2, 1))
        self.assertTrue((self.media / "Heat (1995)/Heat.1995.en.srt").is_file())
        self.assertTrue((self.media / "Friends/Season 2/Friends.S02E18.en.srt").is_file())
        self.assertFalse((self.media / "Deadpool (2016)/Deadpool.2016.en.srt").exists())
        self.assertIn(("FOUND", "Friends S02E18: Downloading subtitles..."), self.lines)
        self.assertIn(("OK", "Saved Friends.S02E18.en.srt"), self.lines)
        self.assertIn(("SKIP", "Deadpool already has subtitles."), self.lines)
        self.assertEqual(self.lines[-1], ("DONE", "Subtitle check complete. 2 new subtitles downloaded."))

    def test_offline_warns_and_skips(self):
        called = []
        summary = self.run_fetch(online=lambda: False, search=lambda p: called.append(p))
        self.assertTrue(summary.offline)
        self.assertEqual(called, [])
        self.assertTrue(any(level == "WARN" for level, _ in self.lines))

    def test_provider_errors_do_not_crash(self):
        def boom(path):
            raise ConnectionError("down")

        summary = self.run_fetch(search=boom)
        self.assertEqual((summary.downloaded, summary.failed), (0, 2))

    def test_no_match_is_reported(self):
        summary = self.run_fetch(search=lambda path: None)
        self.assertEqual(summary.not_found, 2)
        self.assertFalse(os.path.exists(self.media / "Heat (1995)/Heat.1995.en.srt"))

    def test_stop_request_cancels(self):
        summary = self.run_fetch(should_stop=lambda: True)
        self.assertTrue(summary.cancelled)
        self.assertEqual(summary.downloaded, 0)


if __name__ == "__main__":
    unittest.main()
