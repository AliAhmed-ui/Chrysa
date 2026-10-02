"""The browsing UI, built without a display (Qt's offscreen platform).

This does not exercise the video player (that needs a real X server and libVLC);
it checks that the window builds, fills its pages from a library and navigates.
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.helpers import MB, LibraryCase, make_tree  # noqa: E402

try:
    from PyQt6.QtCore import QEventLoop, QTimer
    from PyQt6.QtWidgets import QApplication

    from theia.ui import theme
    from theia.ui.main_window import MainWindow
    from theia.ui.pages import season_name

    HAVE_QT = True
except ImportError:  # pragma: no cover - PyQt6 is a hard requirement of the app itself
    HAVE_QT = False

GB = 1024 * MB


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class BrowsingWindow(LibraryCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["theia-tests"])
        theme.apply(cls.app)

    def setUp(self):
        super().setUp()
        make_tree(self.media, {
            "Heat (1995) [1080p]/Heat.1995.1080p.mp4": 2 * GB,
            "Tenet (2020) [1080p]/Tenet.2020.1080p.mp4": 2 * GB,
            "ES_Project_Demo.mp4": 5 * MB,
            **{f"Friends/S01/Friends.S01E{n:02d}.The.One.Numbered.{n}.720p.mkv": 200 * MB for n in range(1, 5)},
            **{f"Friends/S02/Friends.S02E{n:02d}.720p.mkv": 200 * MB for n in range(1, 3)},
        })
        self.default_duration = 1300.0
        self.durations.update({"Heat.1995.1080p.mp4": 10227.0, "Tenet.2020.1080p.mp4": 9009.0, "ES_Project_Demo.mp4": 14.0})
        self.scan()
        self.window = MainWindow(self.library)
        self.window.resize(1280, 760)
        self.window.show()
        self.addCleanup(self._close)
        # Let the automatic start-up rescan run and finish before poking at the window.
        self.wait_for(lambda: not self.window._startup_scan.isActive() and not self.window.is_scanning)

    def _close(self):
        self.window.close()
        self.spin(50)

    def spin(self, ms):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    def wait_for(self, condition, timeout_ms=8000):
        waited = 0
        while not condition() and waited < timeout_ms:
            self.spin(50)
            waited += 50
        self.assertTrue(condition(), "timed out waiting for the UI")

    def titles(self, row):
        return [card.model.title for card in row.cards()]

    def test_home_rows(self):
        home = self.window.home
        self.assertEqual(self.titles(home.shows_row), ["Friends"])
        self.assertEqual(self.titles(home.movies_row), ["Heat", "Tenet"])
        self.assertEqual(self.titles(home.others_row), ["ES Project Demo"])
        self.assertEqual(home.shows_row.cards()[0].model.subtitle, "2 Seasons · 6 Episodes")
        self.assertEqual(home.movies_row.cards()[0].model.subtitle, "1995 · 2h 50m")
        self.assertTrue(home.continue_row.isHidden(), "nothing has been watched yet")
        self.assertFalse(self.window.grab().isNull())

    def test_continue_watching_row_follows_progress(self):
        heat = self.by_title("Heat")
        episode = self.library.episodes(self.library.shows()[0].id)[1]
        self.library.save_progress(heat.id, 4000, 10227)
        self.library.save_progress(episode.id, 600, 1300)
        self.window.reload()
        cards = self.window.home.continue_row.cards()
        self.assertEqual([(c.model.title, c.model.subtitle) for c in cards],
                         [("Friends", "S1:E2 · 12m left"), ("Heat", "1h 44m left")])
        self.assertAlmostEqual(cards[1].model.progress, 4000 / 10227, places=3)
        self.assertFalse(self.window.home.continue_row.isHidden())

    def test_tabs_and_search(self):
        window = self.window
        window.show_tab("movies")
        self.assertIs(window.pages.currentWidget(), window.movies_page)
        self.assertEqual(len(window.movies_page._grid.cards()), 2)
        window.show_tab("shows")
        self.assertEqual(len(window.shows_page._grid.cards()), 1)

        window.top_bar.search.setText("tenet")
        self.wait_for(lambda: window.pages.currentWidget() is window.search_page)
        movies = window.search_page._sections["movies"][1].cards()
        self.assertEqual([c.model.title for c in movies], ["Tenet"])
        window.top_bar.search.clear()
        self.wait_for(lambda: window.pages.currentWidget() is window.shows_page)

    def test_show_page_lists_seasons_and_episodes(self):
        window = self.window
        show = self.library.shows()[0]
        episodes = self.library.episodes(show.id)
        self.library.save_progress(episodes[0].id, 1290, 1300)  # finished
        self.library.save_progress(episodes[1].id, 400, 1300)   # in progress, watched last
        window.open_show(show)
        page = window.show_page
        self.assertIs(window.pages.currentWidget(), page)
        self.assertEqual([page._seasons.itemText(i) for i in range(page._seasons.count())], ["Season 1", "Season 2"])
        self.assertEqual(page.banner._play.text(), "Resume S1:E2")
        self.assertEqual([row._button.text() for row in page._rows], ["Play Again", "Resume", "Play", "Play"])
        self.assertEqual([row.media.title for row in page._rows][:2], ["The One Numbered 1", "The One Numbered 2"])

        page._seasons.setCurrentIndex(1)
        self.assertEqual([row.media.episode_code for row in page._rows], ["S2:E1", "S2:E2"])
        self.assertEqual([row.media.title for row in page._rows], ["Episode 1", "Episode 2"])
        window.go_back()
        self.assertIs(window.pages.currentWidget(), window.home)

    def test_removed_show_closes_its_page(self):
        import shutil

        window = self.window
        window.open_show(self.library.shows()[0])
        shutil.rmtree(self.media / "Friends")
        window.rescan(manual=True)
        self.wait_for(lambda: not window.is_scanning and window.pages.currentWidget() is window.home)
        self.assertEqual(self.titles(window.home.shows_row), [])

    def test_season_names(self):
        self.assertEqual([season_name(n) for n in (1, 12, 0, -1)], ["Season 1", "Season 12", "Specials", "Other Episodes"])


if __name__ == "__main__":
    unittest.main()
