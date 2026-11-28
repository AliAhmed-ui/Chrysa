"""Browse search suggestions and spelling correction (network replaced by fixtures)."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from chrysa import suggest  # noqa: E402
from chrysa.suggest import Suggestion  # noqa: E402

try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication

    from chrysa.ui_apple import theme
    from chrysa.ui_apple.download_view import DownloadPage

    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

IMDB_BREAKING = {"d": [
    {"id": "in0000274", "l": "Breaking Bad", "s": "Franchise"},
    {"id": "tt0903747", "l": "Breaking Bad", "qid": "tvSeries", "y": 2008},
    {"id": "nm0000001", "l": "Bryan Cranston", "s": "Actor"},
    {"id": "tt1", "l": "El Camino", "qid": "movie", "y": 2019},
    {"id": "tt2", "l": "Breaking Bad", "qid": "tvSeries", "y": 2008},
]}


class Suggestions(unittest.TestCase):
    def setUp(self):
        suggest._cache.clear()
        self.addCleanup(suggest._cache.clear)

    def test_keeps_titles_only_without_duplicates(self):
        with mock.patch.object(suggest, "_get", return_value=IMDB_BREAKING) as get:
            found = suggest.suggestions("Brking  ABD")
        self.assertEqual(found, [Suggestion("Breaking Bad", 2008, "TV series"), Suggestion("El Camino", 2019, "Movie")])
        self.assertIn("brking%20abd", get.call_args[0][0])
        self.assertEqual(found[0].label, "Breaking Bad (2008)  ·  TV series")

    def test_cached_and_short_queries_skip_the_network(self):
        with mock.patch.object(suggest, "_get", return_value=IMDB_BREAKING) as get:
            suggest.suggestions("brking abd")
            suggest.suggestions("brking abd")
            self.assertEqual(suggest.suggestions("b"), [])
        self.assertEqual(get.call_count, 1)

    def test_falls_back_to_tvmaze_and_never_raises(self):
        tvmaze = [{"show": {"name": "Breaking Bad", "premiered": "2008-01-20"}}]
        with mock.patch.object(suggest, "_get", side_effect=[OSError("down"), tvmaze]):
            self.assertEqual(suggest.suggestions("brking"), [Suggestion("Breaking Bad", 2008, "TV series")])
        with mock.patch.object(suggest, "_get", side_effect=OSError("offline")):
            self.assertEqual(suggest.suggestions("dune"), [])


class Correction(unittest.TestCase):
    BB = [Suggestion("Breaking Bad", 2008, "TV series")]

    def test_misspelt_title_is_corrected_keeping_narrowing_words(self):
        self.assertEqual(suggest.corrected("brking abd", self.BB), "Breaking Bad")
        self.assertEqual(suggest.corrected("brking abd s01 1080p", self.BB), "Breaking Bad s01 1080p")
        self.assertEqual(suggest.split("brking abd S01E02 2008"), ("brking abd", "S01E02 2008"))

    def test_correct_or_unrelated_queries_are_left_alone(self):
        self.assertIsNone(suggest.corrected("breaking bad", self.BB))
        self.assertIsNone(suggest.corrected("batman", [Suggestion("The Batman", 2022, "Movie")]))
        self.assertIsNone(suggest.corrected("zzqx", self.BB))
        self.assertIsNone(suggest.corrected("anything", []))


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class SearchBox(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["chrysa-tests"])
        theme.apply(cls.app)

    def setUp(self):
        suggest._cache.clear()
        self.addCleanup(suggest._cache.clear)
        self.page = DownloadPage(Path(tempfile.mkdtemp()))
        self.page.resize(900, 600)
        self.page.show()
        self.addCleanup(self.page.close)
        self.searched = []
        self.page._run_search = lambda correct=True: self.searched.append((self.page.search_box.text(), correct))

    def test_no_suggestions_before_internet_is_allowed(self):
        self.page.search_box.setEnabled(True)
        QTest.keyClicks(self.page.search_box, "brking")
        self.assertFalse(self.page.suggest_timer.isActive())

    def test_suggestions_fill_the_drop_down_and_a_click_searches(self):
        self.page.allow_internet()
        self.page.activateWindow()
        QTest.qWaitForWindowActive(self.page)
        self.page.search_box.setFocus()
        QTest.keyClicks(self.page.search_box, "brking abd s01")
        self.assertTrue(self.page.suggest_timer.isActive())
        self.page._show_suggestions(self.page._suggest_id, "brking abd", Correction.BB)
        model = self.page.suggestion_model
        self.assertEqual(model.item(0).text(), "Breaking Bad (2008)  ·  TV series")
        self.assertEqual(self.page.completer.popup().isVisible(), True)
        self.page._suggestion_picked(model.item(0).data(Qt.ItemDataRole.UserRole + 1))
        self.page._search_picked()
        self.assertEqual(self.searched, [("Breaking Bad s01", True)])

    def test_stale_suggestions_are_dropped(self):
        self.page.allow_internet()
        QTest.keyClicks(self.page.search_box, "brk")
        stale = self.page._suggest_id
        QTest.keyClicks(self.page.search_box, "ing")
        self.page._show_suggestions(stale, "brk", Correction.BB)
        self.assertEqual(self.page.suggestion_model.rowCount(), 0)

    def test_earlier_answers_show_instantly_and_late_ones_still_help(self):
        suggest._cache["breaking"] = [Suggestion("Breaking Bad", 2008, "TV series"),
                                      Suggestion("Breaking Away", 1979, "Movie")]
        self.assertEqual([s.title for s in suggest.guess("breaking b")], ["Breaking Bad"])
        self.assertEqual(suggest.guess("zebra"), [])
        self.page.allow_internet()
        self.page.activateWindow()
        QTest.qWaitForWindowActive(self.page)
        self.page.search_box.setFocus()
        QTest.keyClicks(self.page.search_box, "breaking a")
        self.assertEqual(self.page.suggestion_model.item(0).text(), "Breaking Away (1979)  ·  Movie")
        self.page.suggestion_model.clear()
        self.page._show_suggestions(self.page._suggest_id - 3, "breaking", [])  # stale, but refines from cache
        self.assertEqual(self.page.suggestion_model.rowCount(), 1)

    def test_correction_banner_offers_the_exact_search(self):
        self.page.allow_internet()
        self.page._typed_query = "brking abd"
        self.page._on_corrected(self.page._request_id, "brking abd", "Breaking Bad")
        self.assertEqual(self.page.search_box.text(), "Breaking Bad")
        self.assertIn("Search instead for “brking abd”", self.page.correction.text())
        self.page._search_exactly()
        self.assertEqual(self.searched, [("brking abd", False)])


if __name__ == "__main__":
    unittest.main()
