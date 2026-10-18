"""The main window: sidebar, browsing pages and the player, wired together."""

from __future__ import annotations

import os
import time

from PyQt6.QtCore import QByteArray, QPoint, QSize, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QDesktopServices, QKeySequence, QPainter, QShortcut
from PyQt6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..config import APP_NAME, default_media_dir
from ..library import Library
from ..models import Media, ScanSummary, Show, format_clock
from . import icons, theme
from .dialogs import FoldersDialog
from ..downloads import DEFAULT_DIR as DEFAULT_DOWNLOAD_DIR
from .download_view import DownloadPage
from .images import ImageLoader
from .pages import GridPage, HomePage, SearchPage, ShowPage
from .player import PlayerPage
from .services import ScanWorker, SubtitleWorker, ThumbnailService
from .toggle import ModeSwitch
from .widgets import BrandLabel, CardModel, MediaCard, movie_card, show_card


def _removable_media_root() -> str | None:
    """Where this system mounts SD cards and USB sticks, so the folder picker opens there."""
    user = os.environ.get("USER") or os.path.basename(os.path.expanduser("~"))
    for root in (f"/run/media/{user}", f"/media/{user}", "/media"):
        if os.path.isdir(root):
            return root
    return None


class ElidedLabel(QLabel):
    """A label that shortens its text to whatever width it is given."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def setText(self, text: str) -> None:
        super().setText(text)
        self.setToolTip(text)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        area = self.contentsRect()
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, area.width())
        painter.drawText(area, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
        painter.end()


class NavButton(QPushButton):
    """A sidebar row: accent-coloured icon, label and (optionally) a count on the right."""

    def __init__(self, icon_name: str, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("navButton")
        self.setCheckable(True)
        self._icon_name = icon_name
        self.apply_theme()
        self.setIconSize(QSize(18, 18))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setFixedHeight(38)
        self._count = QLabel(self)
        self._count.setObjectName("navCount")
        self._count.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def apply_theme(self) -> None:
        self.setIcon(icons.icon(self._icon_name, theme.ACCENT))

    def set_count(self, count: int | None) -> None:
        self._count.setText("" if count is None else str(count))
        self._count.setVisible(count is not None)
        self._count.adjustSize()
        self._place_count()

    def _place_count(self) -> None:
        self._count.move(self.width() - self._count.width() - 12, (self.height() - self._count.height()) // 2)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place_count()


class ActivityLog(QPlainTextEdit):
    """A compact read-only mini-terminal for background work (subtitle search, scans)."""

    MAX_LINES = 500

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("activityLog")
        self.setReadOnly(True)
        self.setFixedHeight(140)
        self.setMaximumBlockCount(self.MAX_LINES)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setPlaceholderText("Activity log")

    def log(self, level: str, message: str) -> None:
        self.appendPlainText(f"{time.strftime('%H:%M:%S')} [{level}] {message}")
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())


class Sidebar(QWidget):
    """Wordmark, search, section navigation and library actions (the macOS source-list pattern)."""

    navigate = pyqtSignal(str)  # "home" | "movies" | "shows"
    searchChanged = pyqtSignal(str)
    rescanRequested = pyqtSignal()
    subtitlesRequested = pyqtSignal()
    logLocationRequested = pyqtSignal()
    addFolderRequested = pyqtSignal()
    menuRequested = pyqtSignal(QPoint)
    themeRequested = pyqtSignal()

    SECTIONS = (
        ("home", "library", "Library"),
        ("movies", "film", "Movies"),
        ("shows", "tv", "TV Shows"),
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(theme.SIDEBAR_WIDTH)

        self.search = QLineEdit()
        self.search.setObjectName("searchBox")
        self.search.setPlaceholderText("Search")
        self.search.setClearButtonEnabled(True)
        self.search.setFocusPolicy(Qt.FocusPolicy.ClickFocus)  # no focus ring on launch; "/" and Ctrl+F still jump here
        self._search_icon = self.search.addAction(
            icons.icon("search", theme.TEXT_MUTED), QLineEdit.ActionPosition.LeadingPosition
        )
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(160)
        self._debounce.timeout.connect(lambda: self.searchChanged.emit(self.search.text()))
        self.search.textChanged.connect(lambda _text: self._debounce.start())

        self._tabs = QButtonGroup(self)
        self._tabs.setExclusive(True)
        self._buttons: dict[str, NavButton] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 22, 14, 14)
        layout.setSpacing(6)
        layout.addWidget(BrandLabel())
        layout.addSpacing(10)
        layout.addWidget(self.search)
        layout.addSpacing(20)
        for key, icon_name, text in self.SECTIONS:
            button = NavButton(icon_name, text)
            button.clicked.connect(lambda _checked=False, k=key: self.navigate.emit(k))
            self._tabs.addButton(button)
            self._buttons[key] = button
            layout.addWidget(button)
        self.fetch_subtitles = self._action(
            "download", "Fetch Subtitles", "Download missing English subtitles for your movies and episodes"
        )
        self.fetch_subtitles.clicked.connect(self.subtitlesRequested)
        layout.addWidget(self.fetch_subtitles)
        self.log_location = self._action(
            "folder", "Change log location",
            "Choose the folder where Chrysa keeps your watch log (what you were watching and where you stopped)",
        )
        self.log_location.clicked.connect(self.logLocationRequested)
        layout.addWidget(self.log_location)
        layout.addStretch(1)

        self.activity = ActivityLog()
        layout.addWidget(self.activity)

        self.status = ElidedLabel()
        self.status.setObjectName("statusLabel")
        self.status.setContentsMargins(10, 0, 0, 0)
        self.status.setAlignment(Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.status)

        self.rescan = self._action("refresh", "Rescan Library", "Look for new, moved and deleted videos (F5)")
        self.rescan.clicked.connect(self.rescanRequested)
        folders = self._action("folder", "Add Folders", "Add a folder of movies or TV shows to the library")
        folders.clicked.connect(self.addFolderRequested)
        self.theme_button = self._action("palette", "", "Change Theme (Ctrl+T)")
        self.theme_button.clicked.connect(self.themeRequested)
        self.set_theme_name(theme.manager.get_theme_name())
        more = self._action("more", "More", "Settings and About")
        more.clicked.connect(lambda: self.menuRequested.emit(more.mapToGlobal(QPoint(more.width(), 0))))
        for button in (self.rescan, folders, self.theme_button, more):
            layout.addWidget(button)

    def _action(self, icon_name: str, text: str, tip: str) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("sidebarAction")
        button.setProperty("iconName", icon_name)
        button.setIcon(icons.icon(icon_name, theme.ICON_IDLE, theme.ICON_DISABLED))
        button.setIconSize(QSize(16, 16))
        button.setToolTip(tip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        return button

    def set_theme_name(self, name: str) -> None:
        self.theme_button.setText(f"Theme: {name}")
        self.theme_button.setToolTip(f"Theme: {name} - click to change (Ctrl+T)")

    def apply_theme(self) -> None:
        self._search_icon.setIcon(icons.icon("search", theme.TEXT_MUTED))
        for button in self.findChildren(QPushButton, "sidebarAction"):
            name = button.property("iconName")
            if name:
                button.setIcon(icons.icon(name, theme.ICON_IDLE, theme.ICON_DISABLED))

    def set_counts(self, counts: dict[str, int | None]) -> None:
        for key, count in counts.items():
            self._buttons[key].set_count(count)

    def set_active(self, key: str | None) -> None:
        """Highlight a section, or none when a detail or search page is showing."""
        self._tabs.setExclusive(False)
        for name, button in self._buttons.items():
            button.setChecked(name == key)
        self._tabs.setExclusive(True)

DOWNLOAD_DIR_SETTING = "download.dir"


class MainWindow(QMainWindow):
    def __init__(self, library: Library) -> None:
        super().__init__()
        self.library = library
        self.images = ImageLoader(self)
        self.thumbs = ThumbnailService(library, self)
        self._scan: ScanWorker | None = None
        self._subtitle_fetch: SubtitleWorker | None = None
        self._closing = False
        self._scan_was_manual = False
        self._rescan_queued = False
        self._return_to: QWidget | None = None
        self._before_search: QWidget | None = None

        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(960, 600)

        self.sidebar = Sidebar()
        self.top_bar = self.sidebar  # the search box, status line and Rescan button live here
        self.home = HomePage(self)
        self.shows_page = GridPage(
            self, "TV Shows", lambda: [show_card(s, poster=True) for s in library.shows()],
            "No TV shows in your library yet.", poster=True,
        )
        self.movies_page = GridPage(
            self, "Movies", lambda: [movie_card(m, poster=True) for m in library.movies()],
            "No movies in your library yet.", poster=True,
        )
        self.search_page = SearchPage(self)
        self.show_page = ShowPage(self)
        self.pages = QStackedWidget()
        for page in (self.home, self.shows_page, self.movies_page, self.search_page, self.show_page):
            self.pages.addWidget(page)
        self._tabs = {"home": self.home, "movies": self.movies_page, "shows": self.shows_page}

        self.browser = QWidget()
        self.browser.setObjectName("browser")
        row = QHBoxLayout(self.browser)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self.sidebar)
        row.addWidget(self.pages, 1)

        self.player = PlayerPage(library)
        self.player.closed.connect(self._player_closed)
        self.root = QStackedWidget()
        self.root.addWidget(self.browser)
        self.root.addWidget(self.player)
        self.download_page = DownloadPage(state_file=library.paths.data_dir / "downloads.json")
        saved_dir = self.library.setting(DOWNLOAD_DIR_SETTING)
        if saved_dir:
            self.download_page.set_download_dir(saved_dir)
        self.download_page.changeFolderRequested.connect(self.change_download_dir)
        self.root.addWidget(self.download_page)
        self.mode_switch = ModeSwitch()
        self.mode_bar = QWidget()
        self.mode_bar.setObjectName("browser")
        bar = QHBoxLayout(self.mode_bar)
        bar.setContentsMargins(0, 12, 0, 6)
        bar.addStretch(1)
        bar.addWidget(self.mode_switch)
        bar.addStretch(1)
        central = QWidget()
        column = QVBoxLayout(central)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        column.addWidget(self.mode_bar)
        column.addWidget(self.root, 1)
        self.root.currentChanged.connect(lambda _i: self.mode_bar.setVisible(self.root.currentWidget() is not self.player))
        self.setCentralWidget(central)

        self.top_bar.navigate.connect(self.show_tab)
        self.mode_switch.toggle.toggled.connect(self.set_library_mode)
        self.top_bar.searchChanged.connect(self._search)
        self.top_bar.rescanRequested.connect(lambda: self.rescan(manual=True))
        self.top_bar.addFolderRequested.connect(self.add_folder)
        self.top_bar.subtitlesRequested.connect(self.fetch_subtitles)
        self.top_bar.logLocationRequested.connect(self.change_log_location)
        self.top_bar.menuRequested.connect(self._show_more_menu)
        self.top_bar.themeRequested.connect(self.change_theme)
        theme.manager.subscribe(self._on_theme_changed)
        self.thumbs.backdropReady.connect(self.show_page.on_backdrop)

        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(lambda: self.top_bar.status.setText(""))
        self._install_shortcuts()
        self._restore_geometry()

        self.reload()
        self.show_tab("home")
        # Catch up with whatever changed on disk since the last run.
        self._startup_scan = QTimer(self)
        self._startup_scan.setSingleShot(True)
        self._startup_scan.timeout.connect(self.rescan)
        self._startup_scan.start(250)

    # ---------------------------------------------------------- navigation

    def _install_shortcuts(self) -> None:
        def bind(sequence, handler) -> None:
            shortcut = QShortcut(QKeySequence(sequence), self.browser)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)  # not while the player has focus
            shortcut.activated.connect(handler)

        bind("Ctrl+F", self._focus_search)
        bind("/", self._focus_search)
        bind("F5", lambda: self.rescan(manual=True))
        bind("Ctrl+R", lambda: self.rescan(manual=True))
        bind("Escape", self._escape)
        bind("Alt+Left", self.go_back)
        bind("F11", lambda: self.showNormal() if self.isFullScreen() else self.showFullScreen())
        bind("Ctrl+T", self.change_theme)
        bind("Ctrl+Q", self.close)
        bind("Ctrl+D", lambda: self.set_library_mode(False))
        shortcut = QShortcut(QKeySequence("Ctrl+D"), self.download_page)
        shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        shortcut.activated.connect(lambda: self.set_library_mode(True))

    def set_library_mode(self, library: bool) -> None:
        """Right (True): the standard interface. Left (False): Search & Download."""
        self.mode_switch.toggle.set_checked_silently(library)
        self.mode_switch.apply_theme()
        if library:
            self.root.setCurrentWidget(self.browser)
        else:
            self.root.setCurrentWidget(self.download_page)
            self.ask_download_dir_if_needed()
            self.download_page.focus_search()

    def ask_download_dir_if_needed(self) -> None:
        """First time in Browse: ask where downloads should go (cancelling keeps ~/Downloads)."""
        if self.library.setting(DOWNLOAD_DIR_SETTING):
            return
        if not self._pick_download_dir(str(DEFAULT_DOWNLOAD_DIR)):
            self._store_download_dir(DEFAULT_DOWNLOAD_DIR)

    def change_download_dir(self) -> None:
        self._pick_download_dir(str(self.download_page.client.directory))

    def _pick_download_dir(self, start: str) -> bool:
        chosen = QFileDialog.getExistingDirectory(self, "Choose where downloads are saved", start)
        if chosen:
            self._store_download_dir(chosen)
            self._set_status(f"Downloads will be saved to {chosen}", 6000)
        return bool(chosen)

    def _store_download_dir(self, directory) -> None:
        self.library.set_setting(DOWNLOAD_DIR_SETTING, str(directory))
        self.download_page.set_download_dir(directory)

    # --------------------------------------------------------------- theme

    def change_theme(self) -> None:
        """Switch to the next theme; the style sheet, icons and painted widgets follow."""
        theme.manager.cycle_theme()

    def _on_theme_changed(self, palette) -> None:
        self.top_bar.set_theme_name(palette["display_name"])
        self.setUpdatesEnabled(False)  # one repaint for the whole switch
        try:
            for widget in self.findChildren(QWidget):
                refresh = getattr(widget, "apply_theme", None)
                if callable(refresh):
                    refresh()
        finally:
            self.setUpdatesEnabled(True)
        for widget in self.findChildren(QWidget):
            widget.update()

    def _focus_search(self) -> None:
        self.top_bar.search.setFocus()
        self.top_bar.search.selectAll()

    def _escape(self) -> None:
        if self.top_bar.search.text():
            self.top_bar.search.clear()
        elif self.pages.currentWidget() is self.show_page:
            self.go_back()
        elif self.isFullScreen():
            self.showNormal()

    def show_tab(self, key: str) -> None:
        self._before_search = None
        self.pages.setCurrentWidget(self._tabs[key])
        self.top_bar.set_active(key)
        # Leaving a search: empty the box (its change signal is a no-op off the search page).
        self.top_bar.search.clear()

    def _current_tab(self) -> str | None:
        for key, page in self._tabs.items():
            if self.pages.currentWidget() is page:
                return key
        return None

    def open_show(self, show: Show) -> None:
        current = self.pages.currentWidget()
        if current is not self.show_page:
            self._return_to = current
        if self.show_page.open(show.id):
            self.pages.setCurrentWidget(self.show_page)
            self.top_bar.set_active(None)

    def go_back(self) -> None:
        """Leave the show detail page for wherever the viewer came from."""
        if self.pages.currentWidget() is not self.show_page:
            return
        target = self._return_to or self.home
        if target is self.search_page and not self.top_bar.search.text().strip():
            target = self._before_search or self.home  # the search was cleared meanwhile
        if target is self.show_page:
            target = self.home
        if target is self.search_page:
            self.search_page.reload()  # progress may have changed while away
        self.pages.setCurrentWidget(target)
        self.top_bar.set_active(self._current_tab())

    def _search(self, text: str) -> None:
        text = text.strip()
        current = self.pages.currentWidget()
        if text:
            if current is not self.search_page:
                self._before_search = current
            self.search_page.search(text)
            self.pages.setCurrentWidget(self.search_page)
            self.top_bar.set_active(None)
        elif current is self.search_page:
            self.pages.setCurrentWidget(self._before_search or self.home)
            self.top_bar.set_active(self._current_tab())

    # --------------------------------------------------------------- cards

    def make_card(self, model: CardModel) -> MediaCard:
        """Build a card and connect it to playback, menus and thumbnail updates."""
        card = MediaCard(model, self.images)
        card.activated.connect(self.activate)
        card.menuRequested.connect(self.show_menu)
        if model.thumb_media_id is not None and not model.image:
            self.thumbs.thumbnailReady.connect(card.on_thumbnail)
        return card

    def activate(self, payload) -> None:
        if isinstance(payload, Show):
            self.open_show(payload)
        elif isinstance(payload, Media):
            self.play(payload)

    def show_menu(self, payload, position: QPoint) -> None:
        menu = QMenu(self)
        folder = None
        if isinstance(payload, Show):
            menu.addAction("Open").triggered.connect(lambda: self.open_show(payload))
            upcoming = self.library.next_up(payload.id)
            if upcoming is not None:
                menu.addAction(upcoming.label).triggered.connect(
                    lambda: self.play(upcoming.episode, upcoming.action != "resume")
                )
            folder = payload.path
        else:
            media = self.library.media(payload.id) or payload
            if media.in_progress:
                menu.addAction(f"Resume from {format_clock(media.position)}").triggered.connect(lambda: self.play(media))
                menu.addAction("Start Over").triggered.connect(lambda: self.play(media, True))
            else:
                menu.addAction("Play").triggered.connect(lambda: self.play(media, True))
            if media.is_episode and media.show_id is not None:
                show = self.library.show(media.show_id)
                if show is not None:
                    menu.addAction("Go to Show").triggered.connect(lambda: self.open_show(show))
            menu.addSeparator()
            if not media.is_finished:
                menu.addAction("Mark as Watched").triggered.connect(lambda: self._set_watched(media, True))
            if media.position > 0:
                menu.addAction("Mark as Unwatched").triggered.connect(lambda: self._set_watched(media, False))
            folder = os.path.dirname(media.path)
        if folder and os.path.isdir(folder):
            menu.addSeparator()
            menu.addAction("Open Containing Folder").triggered.connect(
                lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
            )
        menu.exec(position)

    def _set_watched(self, media: Media, watched: bool) -> None:
        if watched:
            self.library.mark_watched(media.id)
        else:
            self.library.clear_progress(media.id)
        self.reload()

    # ------------------------------------------------------------ playback

    def play(self, media: Media, from_start: bool = False) -> None:
        fresh = self.library.media(media.id) or media  # the saved position may have moved on
        if not os.path.isfile(fresh.path):
            self.player.play(fresh, from_start)  # explains that the file is unavailable
            return
        self.root.setCurrentWidget(self.player)
        if self.player.play(fresh, from_start):
            self.thumbs.pause()  # keep ffmpeg from competing with playback
        else:
            self.root.setCurrentWidget(self.browser)

    def _player_closed(self) -> None:
        self.root.setCurrentWidget(self.browser)
        self.thumbs.resume()
        self.reload(after_playback=True)

    # ------------------------------------------------------------- library

    def reload(self, after_playback: bool = False) -> None:
        """Re-read the library into every page."""
        self.home.reload()
        self.shows_page.reload()
        self.movies_page.reload()
        counts = self.library.counts()
        shows, movies = counts.get("show", 0), counts.get("movie", 0)
        self.sidebar.set_counts({
            "home": shows + movies + counts.get("other", 0), "movies": movies, "shows": shows,
        })
        if self.pages.currentWidget() is self.search_page:
            self.search_page.reload()
        if self.show_page.show_id is not None and not self.show_page.reload(follow_next=after_playback):
            if self.pages.currentWidget() is self.show_page:  # the show was removed from disk
                self.pages.setCurrentWidget(self.home)
                self.top_bar.set_active("home")

    def rescan(self, manual: bool = False) -> None:
        if self._closing:
            return
        if self._scan is not None:
            self._rescan_queued = self._rescan_queued or manual
            return
        self._scan = ScanWorker(self.library, retry_failed=manual, parent=self)
        self._scan_was_manual = manual
        self._scan.progress.connect(self._set_status)
        self._scan.finishedScan.connect(self._scan_finished)
        self._scan.failed.connect(lambda message: self._scan_finished(None, message))
        self._scan.finished.connect(self._scan_thread_done)
        self.top_bar.rescan.setEnabled(False)
        self.top_bar.rescan.setText("Scanning…")
        self.sidebar.activity.log("INFO", "Scanning library...")
        self._scan.start()
        self.home.refresh_empty_state()

    @property
    def is_scanning(self) -> bool:
        return self._scan is not None

    def _set_status(self, text: str, linger_ms: int = 0) -> None:
        self._status_timer.stop()
        self.top_bar.status.setText(text)
        if linger_ms:
            self._status_timer.start(linger_ms)

    def _scan_finished(self, summary: ScanSummary | None, error: str = "") -> None:
        if summary is None:
            self._set_status(f"Scan failed: {error}", 12000)
            self.sidebar.activity.log("WARN", f"Scan failed: {error}")
            return
        if summary.cancelled:
            return
        self.sidebar.activity.log(
            "DONE", f"Scan complete: {summary.added} new, {summary.removed} removed, {summary.moved} moved."
        )
        if summary.changed:
            self.reload()
        queued = self.thumbs.queue_missing()
        changes = []
        if summary.added:
            changes.append(f"{summary.added} new")
        if summary.removed:
            changes.append(f"{summary.removed} removed")
        if summary.moved:
            changes.append(f"{summary.moved} moved")
        text = ", ".join(changes) if changes else "Library up to date"
        if queued:
            text += f"  ·  creating {queued} thumbnails"
        if summary.missing_roots:
            text += f"  ·  {len(summary.missing_roots)} folder(s) unavailable"
        quiet = not (self._scan_was_manual or summary.changed or queued or summary.missing_roots)
        self._set_status("" if quiet else text, 9000)  # the start-up check says nothing if all is as it was

    def _scan_thread_done(self) -> None:
        worker, self._scan = self._scan, None
        if worker is not None:
            worker.deleteLater()
        self.top_bar.rescan.setEnabled(True)
        self.top_bar.rescan.setText("Rescan Library")
        self.home.refresh_empty_state()
        if self._rescan_queued:
            self._rescan_queued = False
            self.rescan(manual=True)

    def fetch_subtitles(self) -> None:
        """Search online for the English subtitles the library is missing (background thread)."""
        if self._closing or self._subtitle_fetch is not None:
            return
        worker = self._subtitle_fetch = SubtitleWorker(self.library, parent=self)
        worker.logLine.connect(self.sidebar.activity.log)
        worker.finished.connect(self._subtitle_fetch_done)
        self.top_bar.fetch_subtitles.setEnabled(False)
        self.top_bar.fetch_subtitles.setText("Fetching…")
        worker.start()

    def _subtitle_fetch_done(self) -> None:
        worker, self._subtitle_fetch = self._subtitle_fetch, None
        if worker is not None:
            worker.deleteLater()
        self.top_bar.fetch_subtitles.setEnabled(True)
        self.top_bar.fetch_subtitles.setText("Fetch Subtitles")

    def ask_log_location_if_needed(self) -> None:
        """First launch after installing: ask where the watch log should live."""
        current = self.library.watch_log_dir()
        if current is not None:
            if not os.path.isdir(current):  # e.g. the SD card is not in this machine
                message = f"Watch log folder not available: {current}. Progress is kept here until it returns."
                self.sidebar.activity.log("WARN", message)
                self._set_status(message, 12000)
            return
        default = str(self.library.paths.data_dir)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Where should Chrysa keep your watch log?")
        box.setText("<b>Where should Chrysa keep your watch log?</b>")
        box.setInformativeText(
            "The log records which movie you stopped in and which episode of which season you "
            "left off at, for Continue Watching.\n\n"
            "Pick a folder both systems can read (for example a shared data partition) and "
            "Chrysa will keep your progress in sync between them automatically.\n\n"
            f"If you skip this, the log is kept in {default}. You can change it any time with "
            "Change log location."
        )
        choose = box.addButton("Choose Folder…", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Use Default", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(choose)
        box.exec()
        if box.clickedButton() is choose and self._pick_log_location(_removable_media_root() or default):
            return
        ok, message = self.library.set_watch_log_dir(default)
        self._after_log_change(ok, message)

    def change_log_location(self) -> None:
        self._pick_log_location(self.library.watch_log_dir() or str(self.library.paths.data_dir))

    def _pick_log_location(self, start: str) -> bool:
        """Let the user choose the log folder; True if one was chosen and accepted."""
        chosen = QFileDialog.getExistingDirectory(self, "Choose the folder for the Chrysa watch log", start)
        if not chosen:
            return False
        ok, message = self.library.set_watch_log_dir(chosen)
        self._after_log_change(ok, message)
        return ok

    def _after_log_change(self, ok: bool, message: str) -> None:
        if not ok:
            QMessageBox.warning(self, "Watch log", message)
            return
        self.sidebar.activity.log("info", f"Watch log: {message}")
        self._set_status("Watch log location updated", 6000)
        self.reload()  # progress from the log may have changed what is in Continue Watching

    def add_folder(self) -> None:
        """Pick a folder (e.g. on a USB drive), add it to the library and scan it."""
        folders = self.library.folders()
        start = os.path.dirname(folders[-1].path) if folders else str(default_media_dir().parent)
        chosen = QFileDialog.getExistingDirectory(self, "Add a folder to your library", start)
        if not chosen:
            return
        folder, message = self.library.add_folder(chosen)
        if folder is None:
            QMessageBox.information(self, "Add Folders", message)
            return
        self.reload()
        self.rescan(manual=True)

    def manage_folders(self) -> None:
        dialog = FoldersDialog(self.library, self)
        dialog.exec()
        if dialog.changed:
            self.reload()
            self.rescan(manual=True)

    def _show_more_menu(self, position: QPoint) -> None:
        menu = QMenu(self)
        menu.addAction("Library Folders…").triggered.connect(self.manage_folders)
        menu.addAction("Rescan Library").triggered.connect(lambda: self.rescan(manual=True))
        menu.addSeparator()
        hardware = QAction("Hardware Video Decoding", menu, checkable=True)
        hardware.setChecked(self.library.flag("hardware_decoding", True))
        hardware.setToolTip("Turn off if video shows green or garbled frames")
        hardware.toggled.connect(lambda on: self.library.set_flag("hardware_decoding", on))
        menu.addAction(hardware)
        menu.setToolTipsVisible(True)
        menu.addSeparator()
        menu.addAction(f"About {APP_NAME}").triggered.connect(self._about)
        menu.exec(position)

    def _about(self) -> None:
        counts = self.library.counts()
        QMessageBox.about(
            self, f"About {APP_NAME}",
            f"<h3>{APP_NAME} {__version__}</h3>"
            "<p>An offline media center for your own movies and TV shows.</p>"
            f"<p>{counts.get('movie', 0)} movies · {counts.get('show', 0)} shows · "
            f"{counts.get('episode', 0)} episodes · {counts.get('other', 0)} other videos</p>"
            f"<p>Library data: <code>{self.library.paths.data_dir}</code></p>"
            "<p>Built with PyQt6, libVLC, FFmpeg and guessit.</p>",
        )

    # -------------------------------------------------------------- window

    def bring_to_front(self) -> None:
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def _restore_geometry(self) -> None:
        saved = self.library.setting("window_geometry")
        if saved and self.restoreGeometry(QByteArray.fromBase64(saved.encode())):
            return
        screen = self.screen().availableGeometry()
        self.resize(min(1360, round(screen.width() * 0.9)), min(860, round(screen.height() * 0.9)))

    def closeEvent(self, event) -> None:
        self._closing = True  # no new background work from here on
        theme.manager.unsubscribe(self._on_theme_changed)
        self._startup_scan.stop()
        if self.player.is_active:
            self.player.close_player()  # saves the position and leaves fullscreen
        self.player.shutdown()
        self.download_page.shutdown()
        self.library.set_setting("window_geometry", bytes(self.saveGeometry().toBase64()).decode())
        if self._scan is not None:
            self._scan.requestInterruption()
            self._scan.wait(5000)
        if self._subtitle_fetch is not None:
            self._subtitle_fetch.requestInterruption()
            self._subtitle_fetch.wait(5000)
        self.thumbs.stop()
        self.images.shutdown()
        super().closeEvent(event)
