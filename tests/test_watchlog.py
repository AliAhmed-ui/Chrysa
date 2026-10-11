"""The portable watch log: logging, cross-system merge and relocation."""

import json
import shutil
import time
import unittest
from pathlib import Path

from tests.helpers import MB, LibraryCase, make_tree
from theia.config import Paths
from theia.library import Library
from theia.watchlog import FILENAME, WatchLog

GB = 1024 * MB


class WatchLogSync(LibraryCase):
    def setUp(self):
        super().setUp()
        make_tree(self.media, {
            "Heat (1995) [1080p]/Heat.1995.1080p.mp4": 2 * GB,
            **{f"Friends/S02/Friends.S02E{n:02d}.720p.mkv": 200 * MB for n in (1, 2, 3)},
        })
        self.default_duration = 1300.0
        self.scan()
        self.logdir = self.tmp / "shared"
        self.logdir.mkdir()

    def entries(self):
        return json.loads((self.logdir / FILENAME).read_text())["entries"]

    def second_system(self):
        """A second library over the same videos but a different data dir and paths."""
        other_media = self.tmp / "other-os-media"
        shutil.copytree(self.media, other_media)
        library = Library.open(Paths(self.tmp / "data2"))
        self.addCleanup(library.db.close)
        library.add_folder(other_media)
        library.rescan()
        return library

    def test_nothing_is_written_until_a_location_is_chosen(self):
        self.library.save_progress(self.by_title("Heat").id, 100, 1300)
        self.assertIsNone(self.library.watch_log_dir())
        self.assertFalse((self.logdir / FILENAME).exists())

    def test_progress_is_logged_by_title_and_episode(self):
        ok, _ = self.library.set_watch_log_dir(self.logdir)
        self.assertTrue(ok)
        episode = self.library.episodes(self.library.shows()[0].id)[1]
        self.library.save_progress(episode.id, 321.0, 1300.0)
        entry = self.entries()["episode:friends:s2:e2"]
        self.assertEqual((entry["title"], entry["season"], entry["episode"], entry["position"]),
                         ("Friends", 2, 2, 321.0))

    def test_other_system_picks_progress_up_silently(self):
        self.library.set_watch_log_dir(self.logdir)
        heat = self.by_title("Heat")
        self.library.save_progress(heat.id, 500.0, 1300.0)

        other = self.second_system()
        self.assertEqual(other.continue_watching(), [])
        other.set_watch_log_dir(self.logdir)  # the user points it at the same folder
        resumed = other.continue_watching()
        self.assertEqual([m.title for m in resumed], ["Heat"])
        self.assertEqual(resumed[0].position, 500.0)

    def test_newest_progress_wins_both_ways(self):
        self.library.set_watch_log_dir(self.logdir)
        other = self.second_system()
        other.set_watch_log_dir(self.logdir)
        self.library.save_progress(self.by_title("Heat").id, 100.0, 1300.0)
        time.sleep(0.1)
        heat_there = next(m for m in other._media() if m.title == "Heat")
        other.save_progress(heat_there.id, 900.0, 1300.0)  # watched further on the other system
        self.library.sync_watch_log()
        self.assertEqual(self.by_title("Heat").position, 900.0)

    def test_reset_travels_too(self):
        self.library.set_watch_log_dir(self.logdir)
        heat = self.by_title("Heat")
        self.library.save_progress(heat.id, 500.0, 1300.0)
        other = self.second_system()
        other.set_watch_log_dir(self.logdir)
        time.sleep(0.1)
        other.clear_progress(next(m for m in other._media() if m.title == "Heat").id)
        self.library.sync_watch_log()
        self.assertEqual(self.library.continue_watching(), [])

    def test_unknown_videos_stay_in_the_log(self):
        (self.logdir / FILENAME).write_text(json.dumps({"entries": {
            "movie:somethingelse:2001": {"kind": "movie", "title": "Else", "position": 5, "duration": 50,
                                         "updated": 1.0},
        }}))
        self.library.set_watch_log_dir(self.logdir)
        self.library.save_progress(self.by_title("Heat").id, 10.0, 1300.0)
        self.assertIn("movie:somethingelse:2001", self.entries())

    def test_moving_the_log_carries_entries_over(self):
        self.library.set_watch_log_dir(self.logdir)
        self.library.save_progress(self.by_title("Heat").id, 10.0, 1300.0)
        new = self.tmp / "elsewhere"
        new.mkdir()
        ok, _ = self.library.set_watch_log_dir(new)
        self.assertTrue(ok)
        self.assertEqual(self.library.watch_log_dir(), str(new))
        self.assertIn("movie:heat:1995", json.loads((new / FILENAME).read_text())["entries"])
        self.assertTrue((self.logdir / FILENAME).exists())  # the old file is left alone

    def test_unwritable_location_is_refused(self):
        ok, message = self.library.set_watch_log_dir(self.tmp / "missing" / "deeper")
        self.assertFalse(ok)
        self.assertTrue(message)
        self.assertIsNone(self.library.watch_log_dir())

    def test_corrupt_file_is_ignored(self):
        (self.logdir / FILENAME).write_text("{ not json")
        log = WatchLog(self.logdir)
        self.assertEqual(log.entries, {})


if __name__ == "__main__":
    unittest.main()
