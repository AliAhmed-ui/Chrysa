"""The upgraded Browse page in both designs: rich result rows, details, search status, sources and downloads."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from chrysa import bd, downloads, ranking, releases, search, sources, validate  # noqa: E402
from chrysa.history import History  # noqa: E402
from chrysa.search import SearchResult  # noqa: E402

try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication

    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

H1, H2 = "1" * 40, "2" * 40


def results():
    rows = [
        {"source": "LimeTorrents", "name": "The.Incredible.Hulk.2008.1080p.WEB-DL.H264.AAC-GRP", "size": 4 << 30,
         "seeders": 24, "leechers": 8, "magnet": bd.magnet(H1, "a")},
        {"source": "TPB", "name": "The.Incredible.Hulk.2008.2160p.BluRay.HEVC.EAC3-UHD", "size": 14 << 30,
         "seeders": 12, "leechers": 4, "magnet": bd.magnet(H2, "b")},
    ]
    found = ranking.rank(rows, releases.parse_query("incredible hulk"), [], History(None), {})
    return [SearchResult(c.name, "meta", c.target, c) for c in found.candidates]


class BrowseCase:
    design = ""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["chrysa-tests"])
        cls.view = __import__(f"chrysa.{cls.design}.download_view", fromlist=["DownloadPage"])
        cls.theme = __import__(f"chrysa.{cls.design}.theme", fromlist=["apply"])
        cls.theme.apply(cls.app)

    def setUp(self):
        self.page = self.view.DownloadPage(Path(tempfile.mkdtemp()))
        self.page.resize(1100, 700)
        self.page.show()
        self.addCleanup(self.page.close)

    def test_rich_rows_paint_and_detail_follows_the_highlight(self):
        found = results()
        self.page.set_results(found)
        self.assertTrue(self.page.detail.isVisible())
        self.assertFalse(self.page.grab().isNull())  # three-line delegate and the detail panel paint
        self.assertIn("WHY THIS RESULT", self.page.detail.toHtml())
        self.assertIn("The Incredible Hulk (2008)", self.page.detail.toPlainText())
        self.page.results.setFocus()
        QTest.keyClick(self.page.results, Qt.Key.Key_Down)
        self.assertIn("2160p", self.page.detail.toPlainText())

    def test_plain_results_still_work(self):
        self.page.set_results([SearchResult("Nosferatu", "Downloads: 90", "magnet:?xt=urn:btih:abc")])
        self.assertFalse(self.page.grab().isNull())
        self.assertIn("Downloads: 90", self.page.detail.toPlainText())

    def test_search_status_counts_sources_as_they_answer(self):
        self.page._request_id = 3
        self.page._on_progress(3, {"kind": "start", "sources": ["A", "B", "C"]})
        self.page._on_progress(3, {"kind": "source", "source": "A", "status": "ok", "count": 4})
        self.page._on_progress(3, {"kind": "source", "source": "B", "status": "offline", "count": 0})
        self.assertEqual(self.page.status.text(), "Searching 3 sources…   ✓ 1   ✕ 1")
        self.page._on_progress(2, {"kind": "source", "source": "C", "status": "ok", "count": 1})  # stale search
        self.assertEqual(self.page.status.text(), "Searching 3 sources…   ✓ 1   ✕ 1")

    def test_sources_menu_lists_health_and_toggles(self):
        sources._memory["TPB"] = {"source": "TPB", "state": sources.OFFLINE, "last_error": "name lookup failed"}
        self.addCleanup(sources._memory.clear)
        self.addCleanup(setattr, sources.REGISTRY["Nyaa"], "enabled", True)
        self.page.sources_button.refresh()
        menu = self.page.sources_button.build_menu()
        entries = {a.text(): a for a in menu.actions() if a.isCheckable()}
        tpb = next(a for t, a in entries.items() if "TPB" in t)
        self.assertIn("Offline", tpb.text())
        self.assertEqual(tpb.toolTip(), "name lookup failed · parser v1")
        next(a for t, a in entries.items() if "Nyaa" in t).setChecked(False)
        self.assertFalse(sources.REGISTRY["Nyaa"].enabled)

    def test_duplicate_in_library_asks_before_downloading(self):
        found = results()
        self.page.set_results(found)
        started = []
        with mock.patch.object(self.page.client, "start", side_effect=lambda *a, **k: started.append(a) or
                               downloads.Download(a[0], name="x")), \
                mock.patch("chrysa.acquire.library_match", return_value="The Incredible Hulk (2008)"):
            self.page.start_download(found[0].target_url)
            self.assertEqual(started, [])
            self.assertIn("Press D again", self.page.status.text())
            self.page.start_download(found[0].target_url)
        self.assertEqual(len(started), 1)

    def test_downloads_go_to_quarantine_with_fallbacks(self):
        found = results()
        self.page.set_results(found)
        with mock.patch.object(self.page.client, "start", return_value=downloads.Download("m", name="x")) as start:
            self.page.start_download(found[0].target_url)
        kwargs = start.call_args.kwargs
        self.assertEqual(kwargs["directory"], downloads.incoming_dir(self.page.client.directory))
        self.assertEqual(kwargs["final_dir"], self.page.client.directory)
        self.assertEqual(kwargs["meta"]["source"], "LimeTorrents")
        self.assertTrue(kwargs["meta"]["release_key"])

    def test_download_row_through_its_life(self):
        row = self.view.DownloadRow("Hulk")
        d = downloads.Download("magnet:?xt=urn:btih:" + H1, name="The Incredible Hulk", state="downloading",
                               progress=0.78, size_done=3 << 30, size_total=4 << 30, speed=8 << 20, eta="6m42s",
                               peers=17, seeds=21, avg_speed=7 << 20, peak_speed=9 << 20, health="healthy")
        row.update_from(d)
        self.assertEqual(row.state.text(), "Downloading")
        self.assertIn("ETA 6m42s", row.stats.text())
        self.assertIn("17 peers • 21 seeds", row.stats.text())
        self.assertIn("Excellent", row.performance.text())
        d.state, d.checks = "validating", [validate.Check("pass", "Valid media container", "mp4")]
        d.message = "Checking contents…"
        row.update_from(d)
        self.assertEqual(row.state.text(), "Checking contents…")
        self.assertIn("Valid media container", row.checks.text())
        d.state = "verified"
        row.update_from(d)
        self.assertIn("added to Library", row.state.text())
        self.assertFalse(row.bar.isVisibleTo(row))
        d.state, d.message = "rejected", "Validation failed: not a video. The file was NOT added to the Library and was deleted."
        row.update_from(d)
        self.assertIn("NOT added", row.checks.text())

    def test_finished_or_verified_downloads_cannot_be_cancelled(self):
        item, row = self.page._add_row("Film")
        row.download = downloads.Download("magnet:?xt=urn:btih:" + H1, state="verified")
        with mock.patch.object(self.page.client, "cancel_and_delete") as cancel:
            self.page._cancel_item(item)
        cancel.assert_not_called()
        self.assertEqual(self.page.status.text(), "That download already finished.")

    def test_admitted_media_is_announced(self):
        seen = []
        self.page.mediaAdmitted.connect(seen.append)
        self.page._admitted(["/videos/Film"])
        QApplication.processEvents()
        self.assertEqual(seen, [["/videos/Film"]])


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class ClassicBrowse(BrowseCase, unittest.TestCase):
    design = "ui"


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class AppleBrowse(BrowseCase, unittest.TestCase):
    design = "ui_apple"


if __name__ == "__main__":
    unittest.main()
