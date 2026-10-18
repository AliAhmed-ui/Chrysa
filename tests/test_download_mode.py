"""Search & Download mode: toggle, view behaviour, key bindings, search parsing and the download client."""

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from chrysa import downloads, search  # noqa: E402
from chrysa.search import SearchResult  # noqa: E402
from tests.helpers import LibraryCase  # noqa: E402

try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication

    from chrysa.ui import theme
    from chrysa.ui.download_view import WARNING_TEXT, DownloadPage
    from chrysa.ui.main_window import MainWindow

    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

RESULTS = [
    SearchResult("Night of the Living Dead", "Downloads: 150", "https://example.org/a.torrent"),
    SearchResult("Nosferatu", "Downloads: 90", "magnet:?xt=urn:btih:abc"),
]


class SearchParsing(unittest.TestCase):
    def test_parse_internet_archive(self):
        payload = {"response": {"docs": [
            {"identifier": "notld", "title": "Night", "downloads": 150, "item_size": 1_500_000_000},
            {"title": "no identifier"},
        ]}}
        (result,) = search.InternetArchiveProvider.parse(payload)
        self.assertEqual(result.title, "Night")
        self.assertEqual(result.metadata, "Downloads: 150 · Size: 1.50 GB")
        self.assertEqual(result.target_url, "https://archive.org/download/notld/notld_archive.torrent")


class DownloadClient(unittest.TestCase):
    def test_rejects_garbage_and_missing_engine(self):
        client = downloads.DownloadClient(Path(tempfile.mkdtemp()))
        with self.assertRaises(downloads.DownloadError):
            client.start("not a link")
        with mock.patch.object(downloads, "available_backend", return_value=None):
            with self.assertRaisesRegex(downloads.DownloadError, "No BitTorrent engine"):
                client.start("magnet:?xt=urn:btih:abc")

    def test_runs_in_background_thread_and_reports(self):
        seen, done = [], threading.Event()
        ran_on = []

        def fake(self, download):
            ran_on.append(threading.current_thread())
            self._emit(download, state="finished", progress=1.0)

        def on_update(download):
            seen.append(download.state)
            done.set()

        with mock.patch.object(downloads, "available_backend", return_value="aria2c"), \
                mock.patch.object(downloads.DownloadClient, "_run_aria2", fake):
            client = downloads.DownloadClient(Path(tempfile.mkdtemp()), on_update)
            client.start("magnet:?xt=urn:btih:abc")
            self.assertTrue(done.wait(5))
        self.assertEqual(seen, ["finished"])
        self.assertIsNot(ran_on[0], threading.main_thread())

    def test_parses_aria2_summary(self):
        line = "[#78dfa0 1.5MiB/1.9GiB(0%) CN:3 SD:1 DL:2.1MiB ETA:12m3s]"
        m = downloads._SUMMARY.search(line)
        self.assertEqual(downloads.parse_bytes(m.group(1)), int(1.5 * 1024 * 1024))
        self.assertEqual((m.group(3), m.group(6)), ("3", "12m3s"))
        self.assertIsNone(downloads._SUMMARY.search("FileAlloc:#78dfa0 1.5GiB/1.9GiB(76%)"))

    def test_unfinished_downloads_survive_shutdown(self):
        state = Path(tempfile.mkdtemp()) / "downloads.json"
        gate = threading.Event()

        def hold(self, download):
            self._emit(download, state="downloading")
            download._stop.wait(5)
            self._emit(download, state="cancelled")
            gate.set()

        with mock.patch.object(downloads, "available_backend", return_value="aria2c"), \
                mock.patch.object(downloads.DownloadClient, "_run_aria2", hold):
            client = downloads.DownloadClient(Path(tempfile.mkdtemp()), state_file=state)
            client.start("magnet:?xt=urn:btih:abc", "Movie")
            client.start("magnet:?xt=urn:btih:def", "Dropped").cancel()
            client.shutdown()
            self.assertTrue(gate.wait(5))
        (entry,) = downloads.DownloadClient(Path("."), state_file=state).saved()
        self.assertEqual((entry["target"], entry["name"]), ("magnet:?xt=urn:btih:abc", "Movie"))

    def test_cancel_deletes_partial_files_only_inside_the_folder(self):
        root = Path(tempfile.mkdtemp())
        folder, outside = root / "dl", root / "keep.txt"
        folder.mkdir()
        outside.write_text("mine")
        (folder / "Movie").mkdir()
        (folder / "Movie" / "a.mkv").write_text("x")
        (folder / "Movie.aria2").write_text("x")
        (folder / ("a" * 40 + ".torrent")).write_text("x")
        (folder / "Other.mkv").write_text("x")
        downloads.remove_partial_files(folder, ["Movie", "../keep.txt", ".."], "magnet:?xt=urn:btih:" + "A" * 40)
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["Other.mkv"])
        self.assertTrue(outside.exists())

    def test_cancel_and_delete_stops_download_and_forgets_it(self):
        folder, state, done = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp()) / "d.json", threading.Event()
        updates = []

        def run(self, download):
            (download.directory / "Movie.mkv").write_text("partial")
            download.files.append("Movie.mkv")
            self._emit(download, state="downloading")
            download._stop.wait(5)

        def on_update(download):
            updates.append(download.state)
            if download.state == "cancelled":
                done.set()

        with mock.patch.object(downloads, "available_backend", return_value="aria2c"), \
                mock.patch.object(downloads.DownloadClient, "_run_aria2", run):
            client = downloads.DownloadClient(folder, on_update, state)
            download = client.start("magnet:?xt=urn:btih:abc")
            while "downloading" not in updates:
                threading.Event().wait(0.01)
            client.cancel_and_delete(download)
            self.assertTrue(done.wait(5))
        self.assertFalse((folder / "Movie.mkv").exists())
        self.assertEqual(client.saved(), [])
        self.assertEqual(json.loads(state.read_text()), [])

    def test_backend_failure_is_reported_not_raised(self):
        done, states = threading.Event(), []

        def boom(self, download):
            raise RuntimeError("tracker exploded")

        def on_update(download):
            states.append((download.state, download.message))
            done.set()

        with mock.patch.object(downloads, "available_backend", return_value="aria2c"), \
                mock.patch.object(downloads.DownloadClient, "_run_aria2", boom):
            downloads.DownloadClient(Path(tempfile.mkdtemp()), on_update).start("magnet:?xt=urn:btih:abc")
            self.assertTrue(done.wait(5))
        self.assertEqual(states[-1], ("failed", "tracker exploded"))


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class DownloadView(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["chrysa-tests"])
        theme.apply(cls.app)

    def setUp(self):
        self.page = DownloadPage(Path(tempfile.mkdtemp()))
        self.page.resize(900, 600)
        self.page.show()
        self.addCleanup(self.page.close)
        self.started = []
        self.page.start_download = self.started.append

    def test_search_disabled_until_internet_allowed(self):
        self.assertEqual(self.page.warning.text(), WARNING_TEXT)
        self.assertFalse(self.page.search_box.isEnabled())
        self.page.allow_button.click()
        self.assertTrue(self.page.search_box.isEnabled())
        self.assertFalse(self.page.allow_button.isEnabled())

    def test_up_down_enter_and_d(self):
        self.page.set_results(RESULTS)
        self.page.results.setFocus()
        self.assertEqual(self.page.results.currentRow(), 0)
        QTest.keyClick(self.page.results, Qt.Key.Key_Down)
        self.assertEqual(self.page.results.currentRow(), 1)
        QTest.keyClick(self.page.results, Qt.Key.Key_Up)
        self.assertEqual(self.page.results.currentRow(), 0)
        QTest.keyClick(self.page.results, Qt.Key.Key_Return)
        self.assertEqual(self.page.selected_url, RESULTS[0].target_url)
        QTest.keyClick(self.page.results, Qt.Key.Key_Down)
        QTest.keyClick(self.page.results, Qt.Key.Key_D)
        self.assertEqual(self.started, [RESULTS[1].target_url])

    def test_results_paint(self):
        self.page.set_results(RESULTS)
        self.assertFalse(self.page.grab().isNull())  # runs the delegate's paint code

    def test_click_selects(self):
        self.page.set_results(RESULTS)
        item = self.page.results.item(1)
        rect = self.page.results.visualItemRect(item)
        QTest.mouseClick(self.page.results.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
        self.assertEqual(self.page.selected_url, RESULTS[1].target_url)

    def test_d_types_normally_in_search_box(self):
        self.page.allow_internet()
        QTest.keyClicks(self.page.search_box, "dune")
        self.assertEqual(self.page.search_box.text(), "dune")
        self.assertEqual(self.started, [])

    def test_arrows_in_search_box_move_results(self):
        self.page.allow_internet()
        self.page.set_results(RESULTS)
        self.page.search_box.setFocus()
        QTest.keyClick(self.page.search_box, Qt.Key.Key_Down)
        self.assertEqual(self.page.results.currentRow(), 1)

    def test_missing_engine_shows_message(self):
        page = DownloadPage(Path(tempfile.mkdtemp()))
        self.addCleanup(page.close)
        with mock.patch.object(downloads, "available_backend", return_value=None):
            page.start_download("magnet:?xt=urn:btih:abc")
        self.assertIn("No BitTorrent engine", page.status.text())


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class ModeSwitch(LibraryCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["chrysa-tests"])
        theme.apply(cls.app)

    def test_toggle_switches_views_and_stays_in_sync(self):
        picked = tempfile.mkdtemp()
        patcher = mock.patch("chrysa.ui.main_window.QFileDialog.getExistingDirectory", return_value=picked)
        dialog = patcher.start()
        self.addCleanup(patcher.stop)
        window = MainWindow(self.library)
        self.addCleanup(window.close)
        window.show()
        self.assertIs(window.root.currentWidget(), window.browser)
        toggle = window.mode_switch.toggle
        self.assertTrue(toggle.isChecked())  # right = Library (default)
        toggle.click()  # to the left = Browse
        self.assertIs(window.root.currentWidget(), window.download_page)
        self.assertEqual(dialog.call_count, 1)  # first visit asks where to download...
        self.assertEqual(window.download_page.client.directory, Path(picked))
        self.assertEqual(self.library.setting("download.dir"), picked)
        toggle.click()  # back to the right
        self.assertIs(window.root.currentWidget(), window.browser)
        window.set_library_mode(False)
        self.assertEqual(dialog.call_count, 1)  # ...and never again
        other = tempfile.mkdtemp()
        dialog.return_value = other
        window.download_page.folder_button.click()
        self.assertEqual(window.download_page.client.directory, Path(other))
        self.assertEqual(self.library.setting("download.dir"), other)
        self.assertFalse(toggle.isChecked())
        window.set_library_mode(True)
        self.assertTrue(toggle.isChecked())


if __name__ == "__main__":
    unittest.main()
