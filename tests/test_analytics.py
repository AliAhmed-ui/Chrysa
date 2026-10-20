"""Watch analytics aggregation."""

from tests.helpers import MB, LibraryCase, make_tree
from chrysa.analytics import DAY, compute


class Aggregation(LibraryCase):
    def test_totals_completion_and_categories(self):
        make_tree(self.media, {
            "Heat (1995)/Heat.1995.1080p.mp4": 2048 * MB,
            "Friends/S01/Friends.S01E01.720p.mkv": 200 * MB,
            "Friends/S01/Friends.S01E02.720p.mkv": 200 * MB,
        })
        self.default_duration = 1320.0
        self.durations["Heat.1995.1080p.mp4"] = 7200.0
        self.scan()
        movie = self.library.movies()[0]
        episodes = self.library.episodes(self.library.shows()[0].id)
        self.library.save_progress(movie.id, 3600, 7200)        # half way
        self.library.save_progress(episodes[0].id, 1320, 1320)  # finished
        self.library.save_progress(episodes[1].id, 2, 1320)     # a glance: ignored

        data = compute(self.library.db)

        self.assertEqual((data.started, data.completed), (2, 1))
        self.assertAlmostEqual(data.completion_rate, 0.5)
        self.assertEqual(data.total_seconds, 3600 + 1320)
        self.assertEqual(data.week_seconds, data.total_seconds)
        self.assertEqual(dict(data.categories), {"Movies": 3600, "TV Shows": 1320})
        self.assertEqual(data.top_shows, [("Friends", 1320)])
        self.assertEqual(data.weekly[-1][1], data.total_seconds)

    def test_empty_history(self):
        data = compute(self.library.db)
        self.assertEqual((data.started, data.total_seconds, data.completion_rate), (0, 0, 0))
        self.assertEqual(len(data.weekly), 8)

    def test_old_watching_leaves_the_recent_windows(self):
        make_tree(self.media, {"Heat (1995)/Heat.1995.1080p.mp4": 2048 * MB})
        self.durations["Heat.1995.1080p.mp4"] = 7200.0
        self.scan()
        self.library.save_progress(self.library.movies()[0].id, 600, 7200)
        data = compute(self.library.db, now=__import__("time").time() + 40 * DAY)
        self.assertEqual((data.week_seconds, data.month_seconds, data.total_seconds), (0, 0, 600))
