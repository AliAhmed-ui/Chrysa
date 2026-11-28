"""Paste Link in the Search & Download page: the mode switch, input checks, progress states and failure actions."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from chrysa import downloads, linkdl  # noqa: E402

try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication

    from chrysa.ui_apple import theme
    from chrysa.ui_apple.download_view import DownloadPage, ElidedLabel

    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

LONG_URL = "https://www.example.com/watch/" + "very-long-segment/" * 12 + "?token=" + "z" * 120
LONG_TITLE = "A very long title that goes on and on " * 8


class Settings:
    def __init__(self):
        self.values = {}

    def setting(self, key, default=None):
        return self.values.get(key, default)

    def set_setting(self, key, value):
        self.values[key] = value


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class PasteLink(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["chrysa-tests"])
        theme.apply(cls.app)

    def setUp(self):
        self.settings = Settings()
        self.folder = Path(tempfile.mkdtemp())
        self.page = DownloadPage(self.folder, settings=self.settings)
        self.page.resize(960, 640)
        self.page.show()
        self.addCleanup(self.page.close)
        self.started = []

        def fake_start(url, **kwargs):
            download = downloads.Download(url, name=kwargs.get("name", ""), kind="link")
            self.started.append((url, kwargs))
            return download

        self.page.client.start_link = fake_start
        self.panel = self.page.link_panel

    def paste_mode(self, allow=True):
        if allow:
            self.page.allow_button.click()
        self.page.mode.setChecked(True)

    def row(self, index=0):
        return self.page.downloads_list.itemWidget(self.page.downloads_list.item(index))

    def update(self, download, **changes):
        for key, value in changes.items():
            setattr(download, key, value)
        self.page._show_update(download)

    def add_download(self, name="clip", url="https://example.com/v"):
        self.paste_mode()
        self.panel.link_box.setText(url)
        self.panel.button.click()
        row = self.row(self.page.downloads_list.count() - 1)
        return row.download, row

    # ------------------------------------------------------------------ entry

    def test_mode_switch_shows_the_link_panel_and_keeps_search(self):
        self.assertIs(self.page.stack.currentWidget(), self.page.search_view)
        self.page.mode.setChecked(True)
        self.assertIs(self.page.stack.currentWidget(), self.panel)
        self.assertIn("Enter or Download", self.page.hint.text())
        self.page.mode.setChecked(False)
        self.assertIs(self.page.stack.currentWidget(), self.page.search_view)
        self.assertIn("D downloads", self.page.hint.text())

    def test_disabled_until_internet_is_allowed_like_search(self):
        self.page.mode.setChecked(True)
        self.assertFalse(self.panel.link_box.isEnabled())
        self.assertFalse(self.panel.button.isEnabled())
        self.page.allow_button.click()
        self.assertTrue(self.panel.link_box.isEnabled() and self.panel.button.isEnabled())

    def test_empty_and_malformed_input_is_explained_and_nothing_starts(self):
        self.paste_mode()
        for text, expected in (("", "Paste a link first."), ("   ", "Paste a link first."),
                               ("not a url", "single link"), ("file:///etc/passwd", "http or https"),
                               ("https://nodots", "web link")):
            self.panel.link_box.setText(text)
            self.panel.button.click()
            self.assertIn(expected, self.panel.status.text(), text)
            self.assertEqual(self.panel.status.objectName(), "inlineWarning")
        self.assertEqual(self.started, [])
        self.panel.link_box.setText("x")  # typing clears the complaint
        self.assertEqual(self.panel.status.objectName(), "hint")

    def test_enter_and_button_both_start_a_download(self):
        self.paste_mode()
        self.panel.link_box.setText("https://example.com/a")
        QTest.keyClick(self.panel.link_box, Qt.Key.Key_Return)
        self.panel.link_box.setText("example.com/b")
        self.panel.button.click()
        self.assertEqual([u for u, _ in self.started], ["https://example.com/a", "https://example.com/b"])
        url, kwargs = self.started[0]
        self.assertEqual(kwargs["directory"], downloads.incoming_dir(self.folder))  # Chrysa's quarantine folder
        self.assertEqual(kwargs["final_dir"], self.folder)  # the folder Browse already uses
        self.assertEqual(self.panel.link_box.text(), "")
        self.assertEqual(self.page.downloads_list.count(), 2)

    def test_accessible_names(self):
        self.assertTrue(self.panel.link_box.accessibleName())
        self.assertTrue(self.panel.button.accessibleName())
        self.assertTrue(self.page.mode.accessibleName())
        self.assertEqual(self.panel.button.text(), "Download")
        self.assertEqual(self.panel.button.objectName(), "primary")  # Chrysa's existing primary button

    def test_tab_order_reaches_field_and_button(self):
        self.paste_mode()
        self.assertEqual(self.panel.link_box.focusPolicy(), Qt.FocusPolicy.StrongFocus)
        self.assertNotEqual(self.panel.button.focusPolicy(), Qt.FocusPolicy.NoFocus)

    # ------------------------------------------------------------------ states

    def test_progress_states_use_engine_numbers_and_plain_words(self):
        download, row = self.add_download()
        self.update(download, state="downloading", message="Preparing…", health="connecting")
        self.assertEqual(row.state.text(), "Preparing…")
        self.update(download, message="Connecting…")
        self.assertEqual(row.state.text(), "Connecting…")
        self.update(download, message="", health="healthy", progress=0.78, speed=4_000_000, eta="00:12",
                    size_done=31 << 20, size_total=40 << 20, name="Nightcrawler")
        self.assertEqual(row.title.text(), "Nightcrawler")
        self.assertEqual(row.state.text(), "Downloading")
        self.assertEqual(row.bar.value(), 780)
        self.assertIn("78%", row.stats.text())
        self.assertIn("ETA 00:12", row.stats.text())

    def test_recovery_is_a_quiet_one_liner(self):
        download, row = self.add_download()
        self.update(download, state="downloading", health="recovering",
                    message="The site rejected the connection. Trying browser-compatible access…")
        self.assertEqual(row.state.text(), "The site rejected the connection. Trying browser-compatible access…")
        self.assertFalse(row.action_button.isVisible())
        for word in ("impersonat", "ipv4", "yt-dlp", "proxy", "extractor"):
            self.assertNotIn(word, row.state.text().lower())

    def test_success_reads_download_complete_and_hides_the_checklist(self):
        download, row = self.add_download()
        self.update(download, state="verified", progress=1.0, name="Nightcrawler",
                    admitted=[str(self.folder / "Nightcrawler.mp4")], checks=[mock.Mock(state="pass", label="x", detail="")])
        self.assertEqual(row.state.text(), "Download complete")
        self.assertEqual(row.stats.text(), "Nightcrawler.mp4")
        self.assertFalse(row.checks.isVisible())

    def test_failure_shows_the_explanation_an_action_and_details(self):
        download, row = self.add_download()
        attempts = [linkdl.Attempt(1, [], kind="auth", detail="ERROR: sign in"), linkdl.Attempt(2, ["cookies"], ok=False, kind="auth")]
        self.update(download, state="failed", message="This link requires access through a logged-in browser session.",
                    action="cookies", attempts=attempts)
        self.assertIn("logged-in browser session", row.state.text())
        self.assertTrue(row.action_button.isVisible())
        self.assertEqual(row.action_button.text(), "Choose browser…")
        self.assertTrue(row.details_button.isVisible())
        self.assertFalse(row.details.isVisible())  # collapsed until asked for
        row.details_button.click()
        self.assertTrue(row.details.isVisible())
        self.assertIn("Attempt 1: default settings", row.details.text())
        self.assertFalse(row.bar.isVisible())

    def test_details_stay_hidden_for_a_clean_first_try(self):
        download, row = self.add_download()
        self.update(download, state="verified", attempts=[linkdl.Attempt(1, [], ok=True)], admitted=["/x/a.mp4"])
        self.assertFalse(row.details_button.isVisible())

    # ------------------------------------------------------------------ actions

    def test_choosing_a_browser_saves_it_and_retries(self):
        download, row = self.add_download(url="https://example.com/private")
        self.update(download, state="failed", message="needs login", action="cookies")
        with mock.patch.object(DownloadPage, "_choose_browser", return_value="firefox"):
            row.action_button.click()
        self.assertEqual(self.settings.values["link.cookies_browser"], "firefox")
        self.assertEqual([u for u, _ in self.started], ["https://example.com/private"] * 2)
        self.assertEqual(self.page.downloads_list.count(), 1)  # the failed row was replaced
        self.assertEqual(self.page.client.link_settings(), ("", "firefox"))

    def test_proxy_is_validated_saved_and_retried(self):
        download, row = self.add_download()
        self.update(download, state="failed", message="needs a proxy", action="proxy")
        with mock.patch("chrysa.ui_apple.download_view.QInputDialog.getText", return_value=("10.0.0.2:3128", True)):
            row.action_button.click()
        self.assertEqual(self.settings.values["link.proxy"], "http://10.0.0.2:3128")
        self.assertEqual(self.page.client.link_settings()[0], "http://10.0.0.2:3128")
        self.assertEqual(len(self.started), 2)

    def test_bad_proxy_is_rejected_and_not_saved(self):
        download, row = self.add_download()
        self.update(download, state="failed", message="needs a proxy", action="proxy")
        with mock.patch("chrysa.ui_apple.download_view.QInputDialog.getText", return_value=("ftp://nope:1", True)):
            row.action_button.click()
        self.assertNotIn("link.proxy", self.settings.values)
        self.assertEqual(len(self.started), 1)
        self.assertIn("proxy", self.panel.status.text().lower())

    def test_cancelled_dialogs_change_nothing(self):
        download, row = self.add_download()
        self.update(download, state="failed", message="x", action="proxy")
        with mock.patch("chrysa.ui_apple.download_view.QInputDialog.getText", return_value=("", False)):
            row.action_button.click()
        self.assertEqual((self.settings.values, len(self.started)), ({}, 1))

    def test_try_again_restarts_the_same_link(self):
        download, row = self.add_download(url="https://example.com/again")
        self.update(download, state="failed", message="x", action="retry")
        row.action_button.click()
        self.assertEqual([u for u, _ in self.started], ["https://example.com/again"] * 2)

    # ------------------------------------------------------------------ layout

    def test_long_titles_and_urls_never_widen_the_page(self):
        download, row = self.add_download(url=LONG_URL)
        self.update(download, state="downloading", name=LONG_TITLE, progress=0.4, size_total=100, size_done=40,
                    message="")
        self.page.resize(960, 640)
        QApplication.processEvents()
        self.assertEqual(row.title.text(), LONG_TITLE)  # the full text stays available
        self.assertEqual(row.title.minimumSizeHint().width(), 0)  # it shrinks and shows “…” rather than widening
        self.assertLessEqual(self.page.minimumSizeHint().width(), 960)
        self.assertFalse(self.page.downloads_list.horizontalScrollBar().isVisible())
        self.panel.link_box.setText(LONG_URL)
        self.assertFalse(self.page.grab().isNull())

    def test_elided_label(self):
        label = ElidedLabel("x" * 400)
        label.resize(200, 20)
        label.show()
        QApplication.processEvents()
        self.assertEqual(label.text(), "x" * 400)
        self.assertTrue(label.minimumSizeHint().width() == 0)

    def test_header_keeps_the_title_whole_at_the_smallest_window(self):
        self.page.resize(960, 640)
        QApplication.processEvents()
        title = self.page.findChild(type(self.page.hint), "pageTitle")
        self.assertGreaterEqual(title.width(), title.sizeHint().width())


if __name__ == "__main__":
    unittest.main()
