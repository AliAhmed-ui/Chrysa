"""Search & Download mode: an online search with keyboard-driven results and a torrent client."""

from __future__ import annotations

import html
import shutil
from pathlib import Path
from urllib.parse import urlsplit

from PyQt6.QtCore import QEvent, QTimer, QModelIndex, QObject, QRect, QRectF, QSize, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QKeyEvent, QPainter, QStandardItem, QStandardItemModel, QTransform
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCompleter,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .. import acquire, downloads, linkdl, presentation, search, sources, suggest
from ..history import History
from ..orb import ScrapingOrb
from ..search import SearchError, SearchResult
from . import icons, theme
from .toggle import SegmentedControl

RESULT_ROLE = Qt.ItemDataRole.UserRole
WARNING_TEXT = "Warning: Searching here will enable the app to access the internet."
PROXY_SETTING = "link.proxy"  # a proxy the user entered for Paste Link
BROWSER_SETTING = "link.cookies_browser"  # the browser whose sign-in yt-dlp may use for Paste Link
TITLE_ROLE = Qt.ItemDataRole.UserRole + 1  # what a suggestion puts in the search box
SUGGEST_DELAY_MS = 30  # ask almost every keystroke: early answers for the prefix show while IMDb works on the rest


def _dark() -> bool:
    return theme._luminance(theme.BG) < 0.4


class SearchWorker(QThread):
    found = pyqtSignal(int, object)  # request id, list[SearchResult]
    failed = pyqtSignal(int, str)
    progressed = pyqtSignal(int, object)  # request id, progress event dict
    corrected = pyqtSignal(int, str, str)  # request id, what was typed, what is searched instead

    def __init__(self, request_id: int, query: str, parent: QObject | None = None, correct: bool = True) -> None:
        super().__init__(parent)
        self.request_id = request_id
        self.query = query
        self.correct = correct

    def run(self) -> None:
        try:
            fixed = suggest.corrected(self.query) if self.correct else None
            if fixed:  # a misspelt title ("brking abd") is searched as the real one
                self.corrected.emit(self.request_id, self.query, fixed)
                self.query = fixed
            found = search.search(self.query, progress=lambda event: self.progressed.emit(self.request_id, event))
            self.found.emit(self.request_id, found)
        except SearchError as error:
            self.failed.emit(self.request_id, str(error))
        except Exception as error:
            self.failed.emit(self.request_id, f"{type(error).__name__}: {error}")


class ResultDelegate(QStyledItemDelegate):
    """Three lines per row: the title, what the release is, then size · health · speed · swarm · source.

    Results without ranking data (plain :class:`SearchResult`) keep the original two lines.
    """

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        result: SearchResult = index.data(RESULT_ROLE)
        return QSize(option.rect.width(), 80 if result is not None and result.candidate is not None else 62)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        result: SearchResult = index.data(RESULT_ROLE)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect.adjusted(4, 2, -4, -2)
        painter.setPen(Qt.PenStyle.NoPen)
        if option.state & QStyle.StateFlag.State_Selected:
            tint = QColor(theme.ACCENT)
            tint.setAlphaF(0.18)
            painter.setBrush(tint)
            painter.drawRoundedRect(rect, theme.RADIUS_S + 2, theme.RADIUS_S + 2)
        elif option.state & QStyle.StateFlag.State_MouseOver:
            hover = QColor(theme.TEXT)
            hover.setAlphaF(0.06)
            painter.setBrush(hover)
            painter.drawRoundedRect(rect, theme.RADIUS_S + 2, theme.RADIUS_S + 2)
        text_rect = rect.adjusted(14, 8, -14, -8)
        candidate = result.candidate
        title_font = QFont(option.font)
        title_font.setPixelSize(15)
        title_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.setPen(QColor(theme.TEXT))
        title = painter.fontMetrics().elidedText(candidate.display_title if candidate else result.title,
                                                 Qt.TextElideMode.ElideRight, text_rect.width())
        painter.drawText(QRect(text_rect.left(), text_rect.top(), text_rect.width(), 22), Qt.AlignmentFlag.AlignVCenter, title)
        meta_font = QFont(option.font)
        meta_font.setPixelSize(12)
        painter.setFont(meta_font)
        painter.setPen(QColor(theme.TEXT_MUTED))
        if candidate is None:
            painter.drawText(
                QRect(text_rect.left(), text_rect.top() + 24, text_rect.width(), 18), Qt.AlignmentFlag.AlignVCenter,
                result.metadata)
        else:
            spec = painter.fontMetrics().elidedText(presentation.spec_line(candidate) or result.title,
                                                    Qt.TextElideMode.ElideRight, text_rect.width())
            painter.drawText(QRect(text_rect.left(), text_rect.top() + 22, text_rect.width(), 18),
                             Qt.AlignmentFlag.AlignVCenter, spec)
            paint_segments(painter, presentation.status_segments(candidate),
                           QRect(text_rect.left(), text_rect.top() + 42, text_rect.width(), 18))
        painter.restore()


def paint_segments(painter: QPainter, segments: list[tuple[str, str]], rect: QRect) -> None:
    """Draw ``(text, tone)`` pieces left to right, separated by dots, eliding whatever does not fit."""
    x, right, metrics, dark = rect.left(), rect.right(), painter.fontMetrics(), _dark()
    separator = "  ·  "
    for i, (text, tone) in enumerate(segments):
        if i:
            width = metrics.horizontalAdvance(separator)
            if x + width >= right:
                break
            painter.setPen(QColor(theme.TEXT_MUTED))
            painter.drawText(QRect(x, rect.top(), width, rect.height()), Qt.AlignmentFlag.AlignVCenter, separator)
            x += width
        piece = metrics.elidedText(text, Qt.TextElideMode.ElideRight, right - x)
        width = metrics.horizontalAdvance(piece)
        painter.setPen(QColor(presentation.tone_color(tone, dark, theme)))
        painter.drawText(QRect(x, rect.top(), width, rect.height()), Qt.AlignmentFlag.AlignVCenter, piece)
        x += width
        if piece != text:
            break


class ResultList(QListWidget):
    """Up/Down move the highlight (native); Enter selects; ``d`` asks to download."""

    selectRequested = pyqtSignal(object)  # SearchResult
    downloadRequested = pyqtSignal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("resultList")
        self.setItemDelegate(ResultDelegate(self))
        self.setMouseTracking(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)  # rows elide instead
        self.itemClicked.connect(lambda item: self.selectRequested.emit(item.data(RESULT_ROLE)))
        self._empty = ("", "")  # title, detail shown while the list has no rows

    def set_placeholder(self, title: str, detail: str = "") -> None:
        self._empty = (title, detail)
        self.viewport().update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self.count() or not self._empty[0]:
            return
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = self.viewport().rect()
        icons.paint(painter, "search", QRectF(area.center().x() - 16, area.center().y() - 64, 32, 32), theme.TEXT_MUTED)
        painter.setFont(theme.type_font("headline"))
        painter.setPen(QColor(theme.TEXT_SOFT))
        painter.drawText(QRect(0, area.center().y() - 20, area.width(), 26), Qt.AlignmentFlag.AlignCenter, self._empty[0])
        painter.setFont(theme.type_font("callout"))
        painter.setPen(QColor(theme.TEXT_MUTED))
        painter.drawText(QRect(0, area.center().y() + 8, area.width(), 22), Qt.AlignmentFlag.AlignCenter, self._empty[1])
        painter.end()

    def current_result(self) -> SearchResult | None:
        item = self.currentItem()
        return item.data(RESULT_ROLE) if item else None

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        result = self.current_result()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and result:
            self.selectRequested.emit(result)
        elif key == Qt.Key.Key_D and not event.modifiers() & ~Qt.KeyboardModifier.ShiftModifier and result:
            self.downloadRequested.emit(result)
        else:
            super().keyPressEvent(event)


class ResultDetail(QTextBrowser):
    """Everything known about the highlighted result, and why it ranks where it does."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("resultDetail")
        self.setOpenLinks(False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # keyboard stays on the result list
        self.setMinimumWidth(300)
        self.setMaximumWidth(380)
        self.result: SearchResult | None = None
        self.show_result(None)

    def show_result(self, result: SearchResult | None) -> None:
        self.result = result
        self.setHtml(presentation.detail_html(
            result.candidate if result else None, _dark(), theme,
            result.title if result else "", result.metadata if result else ""))


class SourcesButton(QPushButton):
    """``Sources 6/7``: opens a menu with each source's health; untick a source to stop asking it."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Sources", parent)
        self.setObjectName("sourcesButton")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFlat(True)
        self.setToolTip("Health of each search source")
        self.clicked.connect(self.open_menu)
        self.refresh()

    def refresh(self) -> None:
        table = {row["source"]: row for row in sources.status_table()}
        text, tone = presentation.sources_button_text(table)
        self.setText(text)
        self.setStyleSheet(f"color: {presentation.tone_color(tone, _dark(), theme)}; font-size: 12px;")

    def build_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        title = menu.addAction("SOURCE STATUS — untick to stop using a source")
        title.setEnabled(False)
        for row in sources.status_table():
            label, tone, tip = presentation.source_entry(row)
            mark = {"good": "●", "warn": "●", "bad": "●"}.get(tone, "○")
            action = menu.addAction(f"{mark}  {label}")
            action.setCheckable(True)
            action.setChecked(row.get("state") != sources.DISABLED)
            action.setToolTip(tip or "No problems recorded")
            action.toggled.connect(lambda on, name=row["source"]: (sources.set_enabled(name, on), self.refresh()))
        return menu

    def open_menu(self) -> None:
        self.build_menu().exec(self.mapToGlobal(self.rect().bottomLeft()))


class DiscSpinner(QLabel):
    """A small CD that spins while a search is running and hides itself otherwise."""

    SIZE = 18

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)
        self.setVisible(False)

    def start(self) -> None:
        self.setVisible(True)
        self._timer.start()
        self._tick()

    def stop(self) -> None:
        self._timer.stop()
        self.setVisible(False)

    def _tick(self) -> None:
        self._angle = (self._angle + 18) % 360
        dpr = self.devicePixelRatioF()
        disc = icons.pixmap("disc", self.SIZE, theme.TEXT_MUTED, dpr)
        self.setPixmap(disc.transformed(QTransform().rotate(self._angle), Qt.TransformationMode.SmoothTransformation)
                       .scaled(round(self.SIZE * dpr), round(self.SIZE * dpr), Qt.AspectRatioMode.IgnoreAspectRatio,
                               Qt.TransformationMode.SmoothTransformation))


human_size = presentation.human_size


class ElidedLabel(QLabel):
    """A one-line label that shortens its text with “…” instead of widening the window; ``text()`` is the full text."""

    def __init__(self, text: str = "", parent: QWidget | None = None,
                 mode: Qt.TextElideMode = Qt.TextElideMode.ElideRight) -> None:
        super().__init__(parent)
        self._full = ""
        self._mode = mode
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setText(text)

    def text(self) -> str:
        return self._full

    def setText(self, text: str) -> None:
        self._full = text
        self.setToolTip(text)
        self._elide()

    def minimumSizeHint(self) -> QSize:
        return QSize(0, super().minimumSizeHint().height())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        super().setText(self.fontMetrics().elidedText(self._full, self._mode, max(self.width(), 40)))


class PasteLinkPanel(QWidget):
    """Paste Link: one field and a Download button. ``submitted(text)`` fires on Enter or a click."""

    submitted = pyqtSignal(str)

    HINT = "Paste a video, audio, or supported media link."

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.link_box = QLineEdit()
        self.link_box.setObjectName("searchBox")
        self.link_box.setProperty("large", True)  # same field as Search
        self.link_box.setMinimumHeight(52)
        self.link_box.setPlaceholderText("Paste a video link…")
        self.link_box.setAccessibleName("Video link")
        self.link_box.returnPressed.connect(self._submit)
        self.link_box.textChanged.connect(self._typed)
        self.button = QPushButton("Download")
        self.button.setObjectName("primary")
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.button.setMinimumHeight(52)
        self.button.setAccessibleName("Download link")
        self.button.clicked.connect(self._submit)
        self.status = QLabel(self.HINT)
        self.status.setObjectName("hint")
        self.status.setWordWrap(True)
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(self.link_box, 1)
        row.addWidget(self.button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addLayout(row)
        layout.addWidget(self.status)
        layout.addStretch(1)
        self.setFocusProxy(self.link_box)

    def set_enabled(self, enabled: bool) -> None:
        self.link_box.setEnabled(enabled)
        self.button.setEnabled(enabled)
        self.show_info(self.HINT if enabled else "Allow internet access above to paste a link.")

    def show_info(self, message: str) -> None:
        self.status.setObjectName("hint")
        self.status.setText(message)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def show_error(self, message: str) -> None:
        self.status.setObjectName("inlineWarning")
        self.status.setText(message)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def clear(self) -> None:
        self.link_box.clear()

    def _typed(self) -> None:
        if self.status.objectName() == "inlineWarning":
            self.show_info(self.HINT)

    def _submit(self) -> None:
        if self.link_box.isEnabled():
            self.submitted.emit(self.link_box.text())


class DownloadRow(QWidget):
    """One download: name, what it is doing, progress bar, live figures, then the validation checklist."""

    actionRequested = pyqtSignal(str)  # "retry" | "proxy" | "cookies": what a failed link download offers

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("downloadRow")
        self.title = ElidedLabel(title)
        self.title.setObjectName("downloadTitle")
        self.bar = QProgressBar()
        self.bar.setObjectName("downloadBar")
        self.bar.setRange(0, 1000)
        self.bar.setFixedHeight(8)
        self.bar.setTextVisible(False)
        self.bar.setAccessibleName("Download progress")
        self.action_button = QPushButton()
        self.action_button.setObjectName("outline")
        self.action_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.action_button.setVisible(False)
        self.action_button.clicked.connect(lambda: self.actionRequested.emit(self._action))
        self._action = ""
        self.details_button = QPushButton("Download details")
        self.details_button.setObjectName("sourcesButton")
        self.details_button.setFlat(True)
        self.details_button.setCheckable(True)
        self.details_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.details_button.setVisible(False)
        self.details = QLabel()
        self.details.setObjectName("statusLabel")
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.details.setVisible(False)
        self.details_button.toggled.connect(self.details.setVisible)
        self.download: downloads.Download | None = None  # set while running
        self.entry: dict | None = None  # set while interrupted, awaiting restart
        self.state = QLabel("Starting…")
        self.state.setObjectName("statusLabel")
        self.state.setWordWrap(True)
        self.speed = QLabel()
        self.speed.setObjectName("downloadSpeed")
        self.speed.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.stats = QLabel()
        self.stats.setObjectName("statusLabel")
        self.performance = QLabel()
        self.performance.setObjectName("statusLabel")
        self.checks = QLabel()
        self.checks.setObjectName("statusLabel")
        self.checks.setTextFormat(Qt.TextFormat.RichText)
        self.checks.setWordWrap(True)
        for label in (self.stats, self.performance, self.checks):
            label.setVisible(False)
        info = QHBoxLayout()
        info.addWidget(self.state, 1)
        info.addWidget(self.speed)
        extras = QHBoxLayout()
        extras.addWidget(self.action_button)
        extras.addWidget(self.details_button)
        extras.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)
        layout.addWidget(self.title)
        layout.addLayout(info)
        layout.addWidget(self.bar)
        layout.addWidget(self.stats)
        layout.addWidget(self.performance)
        layout.addWidget(self.checks)
        layout.addLayout(extras)
        layout.addWidget(self.details)

    def show_message(self, message: str) -> None:
        self.state.setText(message)
        self.state.setStyleSheet("")
        self.speed.clear()

    def update_from(self, download: downloads.Download) -> None:
        if download.name:
            self.title.setText(download.name)
        dark = _dark()
        self.bar.setValue(round(download.progress * 1000))
        self.bar.setFormat(f"{download.progress * 100:.1f}%")
        headline, tone = presentation.download_status(download)
        self.state.setText(headline)
        self.state.setStyleSheet(f"color: {presentation.tone_color(tone, dark, theme)};" if tone != "muted" else "")
        self.speed.setText(f"↓ {human_size(download.speed)}/s" if download.speed and download.state == "downloading" else "")
        stats = presentation.download_stats(download)
        if stats and download.state == "downloading":
            stats = f"{download.progress * 100:.0f}% · {stats}"  # the slim bar carries no text
        self.stats.setText(stats)
        self.stats.setVisible(bool(stats))
        perf, perf_tone = presentation.download_performance(download)
        self.performance.setText(perf)
        self.performance.setStyleSheet(f"color: {presentation.tone_color(perf_tone, dark, theme)};")
        self.performance.setVisible(bool(perf))
        checks = list(download.checks or [])
        text = presentation.checks_html(checks, dark, theme)
        if download.kind == "link" and download.state != "rejected":
            text = ""  # a pasted link that passed every check needs no checklist
        if download.state == "rejected" and download.message:
            text += f"<br><b>{download.message}</b>"
        self.checks.setText(text)
        self.checks.setVisible(bool(text))
        self.bar.setVisible(download.state in ("queued", "downloading", "finished", "validating"))
        if download.kind == "link":
            self._show_link_extras(download)

    ACTION_TEXT = {"retry": "Try again", "proxy": "Set proxy…", "cookies": "Choose browser…"}

    def _show_link_extras(self, download: downloads.Download) -> None:
        self._action = download.action if download.state == "failed" else ""
        self.action_button.setText(self.ACTION_TEXT.get(self._action, ""))
        self.action_button.setVisible(bool(self._action))
        lines = [attempt.line() for attempt in download.attempts]
        self.details.setText("\n".join(lines))
        has_details = bool(lines) and (download.state == "failed" or len(lines) > 1)
        self.details_button.setVisible(has_details)
        if not has_details:
            self.details_button.setChecked(False)
        self.title.setToolTip(download.target)


class DownloadList(QListWidget):
    """The running downloads. Select one (click, or Tab then Up/Down) and press C to cancel it."""

    cancelRequested = pyqtSignal(object)  # QListWidgetItem

    def keyPressEvent(self, event: QKeyEvent) -> None:
        item = self.currentItem()
        if event.key() == Qt.Key.Key_C and not event.modifiers() & ~Qt.KeyboardModifier.ShiftModifier and item:
            self.cancelRequested.emit(item)
        else:
            super().keyPressEvent(event)


class DownloadPage(QWidget):
    """The whole Search & Download view."""

    changeFolderRequested = pyqtSignal()
    mediaAdmitted = pyqtSignal(list)  # final paths of newly verified downloads
    suggestionsReady = pyqtSignal(int, str, object)  # request id, query, list[suggest.Suggestion] (from a pool thread)

    def __init__(self, client_dir=None, parent: QWidget | None = None, state_file=None, db=None,
                 settings=None) -> None:
        super().__init__(parent)
        self.setObjectName("browser")
        self.internet_allowed = False
        self._settings = settings  # anything with setting(key) / set_setting(key, value), e.g. the Library
        self._memory: dict[str, str] = {}
        self.selected_url: str | None = None
        self._request_id = 0
        self._worker: SearchWorker | None = None
        self.db = db
        self.history = History(db)
        search.configure(self.history)  # search ranking and source health remember through the library database
        if state_file:
            sources.load_overrides(Path(state_file).parent / "sources.json")
        self.pipeline = acquire.Pipeline(self.history, on_admitted=self._admitted)
        self.client = downloads.DownloadClient(
            client_dir or downloads.DEFAULT_DIR, self._download_updated, state_file, pipeline=self.pipeline)
        self.client.link_settings = lambda: (self._get(PROXY_SETTING), self._get(BROWSER_SETTING))

        self._download_items: dict[int, tuple[QListWidgetItem | None, DownloadRow]] = {}
        self._titles: dict[str, str] = {}  # target url -> result title, for the download row
        self._results: dict[str, SearchResult] = {}  # target url -> result, for ranking data at download time
        self._confirm: str | None = None  # target the user was warned about; pressing D again downloads it
        self._progress: dict[str, str] = {}
        self._expected = 0
        self.updateReceived.connect(self._show_update)
        self.admittedReceived.connect(self.mediaAdmitted)

        header = QHBoxLayout()
        title = QLabel("Search & Download")
        title.setObjectName("pageTitle")
        header.addWidget(title)
        header.addSpacing(16)
        self.mode = SegmentedControl("Search", "Paste Link")
        self.mode.setAccessibleName("Download mode")
        self.mode.setToolTip("Search for a title, or paste a link to download")
        self.mode.toggled.connect(self._set_mode)
        header.addWidget(self.mode)
        self.folder_label = ElidedLabel(mode=Qt.TextElideMode.ElideMiddle)  # gives way to the title when narrow
        self.folder_label.setObjectName("statusLabel")
        self.folder_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.folder_button = QPushButton("Change download folder")
        self.folder_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.folder_button.clicked.connect(self.changeFolderRequested)
        header.addWidget(self.folder_label, 1)
        header.addWidget(self.folder_button)
        self.set_download_dir(self.client.directory)

        self.search_box = QLineEdit()
        self.search_box.setObjectName("searchBox")
        self.search_box.setMinimumHeight(52)
        self.search_box.setProperty("large", True)  # styled in theme.py
        self.search_box.setPlaceholderText("Search for a video…")
        self.search_box.setEnabled(False)
        self.search_box.returnPressed.connect(self._run_search)
        self.search_box.textEdited.connect(self._search_typed)
        self.search_box.installEventFilter(self)
        self._suggest_id = 0
        self._searched = False  # a search ran since the last keystroke: no drop-down until typing resumes
        self._typed_query = ""  # the last search as typed, for "Search instead for …"
        self._picked: str | None = None  # a suggestion clicked in the drop-down, searched once it is in the box
        self.suggestion_model = QStandardItemModel(self)
        self.completer = QCompleter(self.suggestion_model, self)
        self.completer.setCompletionMode(QCompleter.CompletionMode.UnfilteredPopupCompletion)  # the server matched
        self.completer.setCompletionRole(TITLE_ROLE)
        self.completer.setMaxVisibleItems(suggest.LIMIT)
        self.completer.popup().setObjectName("suggestions")
        self.completer.activated[str].connect(self._suggestion_picked)
        self.search_box.setCompleter(self.completer)
        self.suggestionsReady.connect(self._show_suggestions)
        self.suggest_timer = QTimer(self)
        self.suggest_timer.setSingleShot(True)
        self.suggest_timer.setInterval(SUGGEST_DELAY_MS)
        self.suggest_timer.timeout.connect(self._fetch_suggestions)
        self.correction = QLabel()  # "Showing results for Breaking Bad · Search instead for brking abd"
        self.correction.setObjectName("hint")
        self.correction.setVisible(False)
        self.correction.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.correction.linkActivated.connect(self._search_exactly)

        self.warning = QLabel(WARNING_TEXT)
        self.warning.setWordWrap(True)
        self.warning.setObjectName("inlineWarning")
        self.allow_button = QPushButton("Allow Internet Access")
        self.allow_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.allow_button.clicked.connect(self.allow_internet)
        warning_row = QHBoxLayout()
        warning_row.addWidget(self.warning, 1)
        warning_row.addWidget(self.allow_button)

        self.results = ResultList()
        self.results.selectRequested.connect(self._select)
        self.results.downloadRequested.connect(self._download_result)
        self.results.set_placeholder("Search for movies and shows", "Allow internet access above to begin.")
        self.results.currentItemChanged.connect(
            lambda item, _old: self.detail.show_result(item.data(RESULT_ROLE) if item else None))
        self.orb = ScrapingOrb(self.results.viewport(), dark=_dark())
        self.detail = ResultDetail()
        self.detail.setVisible(False)
        self.status = QLabel("Allow internet access to start searching.")
        self.status.setObjectName("hint")
        self.status.setWordWrap(True)
        self.sources_button = SourcesButton()
        self.downloads_list = DownloadList()
        self.downloads_list.setObjectName("downloadList")
        self.downloads_list.setMinimumHeight(200)
        self.downloads_list.setMaximumHeight(320)
        self.downloads_list.setVisible(False)
        self.downloads_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.downloads_list.setToolTip("Select a download and press C to cancel it and delete the partial file")
        self.downloads_list.cancelRequested.connect(self._cancel_item)

        self.link_panel = PasteLinkPanel()
        self.link_panel.set_enabled(False)
        self.link_panel.submitted.connect(self.start_link)

        self.search_view = QWidget()
        search_layout = QVBoxLayout(self.search_view)
        search_layout.setContentsMargins(0, 0, 0, 0)
        search_layout.setSpacing(12)
        search_layout.addWidget(self.search_box)
        search_layout.addWidget(self.correction)
        self.spinner = DiscSpinner()
        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        status_row.addWidget(self.spinner)
        status_row.addWidget(self.status, 1)
        status_row.addWidget(self.sources_button)
        search_layout.addLayout(status_row)
        body = QHBoxLayout()
        body.setSpacing(12)
        body.addWidget(self.results, 1)
        body.addWidget(self.detail)
        search_layout.addLayout(body, 1)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.search_view)
        self.stack.addWidget(self.link_panel)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.SIDE_MARGIN, 24, theme.SIDE_MARGIN, 20)
        layout.setSpacing(12)
        layout.addLayout(header)
        layout.addSpacing(6)
        layout.addLayout(warning_row)
        layout.addWidget(self.stack, 1)
        layout.addWidget(self.downloads_list)
        for entry in self.client.saved():
            row = self._add_row(entry.get("name") or entry["target"][:80], "Interrupted — resumes when internet access is allowed")[1]
            row.entry = entry
        self.hint = QLabel()
        self.hint.setObjectName("statusLabel")
        layout.addWidget(self.hint)
        self._set_mode(False)

    # -------------------------------------------------------- download folder

    def set_download_dir(self, directory) -> None:
        """New downloads go to *directory*; ones already running keep their folder."""
        self.client.directory = Path(directory)
        text = str(self.client.directory)
        self.folder_label.setText(f"Saving to {text}")
        self.folder_label.setToolTip(text)

    # ------------------------------------------------------------- internet

    def allow_internet(self) -> None:
        self.internet_allowed = True
        self.allow_button.setEnabled(False)
        self.allow_button.setText("Internet Access Allowed")
        self.search_box.setEnabled(True)
        self.link_panel.set_enabled(True)
        (self.link_panel if self.mode.isChecked() else self.search_box).setFocus()
        self.results.set_placeholder("Search for movies and shows", "Type a title and press Enter.")
        self.status.setText("Type a title and press Enter.")
        suggest.warm()
        self._resume_interrupted()

    def _resume_interrupted(self) -> None:
        rows = [self.downloads_list.itemWidget(self.downloads_list.item(i)) for i in range(self.downloads_list.count())]
        for row in rows:
            if row.entry is None:
                continue
            entry, row.entry = row.entry, None
            try:
                download = self.client.start(
                    entry["target"], entry.get("name", ""), entry.get("directory"), entry.get("files"),
                    final_dir=entry.get("final_dir"), alternatives=entry.get("alternatives"), meta=entry.get("meta"))
            except downloads.DownloadError as error:
                row.show_message(str(error))
                continue
            self._bind_row(download, row)
            row.show_message("Resuming where it left off…")

    def focus_search(self) -> None:
        if self.mode.isChecked() and self.link_panel.link_box.isEnabled():
            self.link_panel.link_box.setFocus()
        elif self.search_box.isEnabled():
            self.search_box.setFocus()
        else:
            self.allow_button.setFocus()

    # ----------------------------------------------------------- paste link

    def _get(self, key: str) -> str:
        if self._settings is not None:
            return self._settings.setting(key) or ""
        return self._memory.get(key, "")

    def _put(self, key: str, value: str) -> None:
        if self._settings is not None:
            self._settings.set_setting(key, value)
        else:
            self._memory[key] = value

    def _set_mode(self, paste: bool) -> None:
        """Search (a title, ranked results) or Paste Link (one address, yt-dlp)."""
        self.stack.setCurrentWidget(self.link_panel if paste else self.search_view)
        self.hint.setText(
            "Enter or Download starts it   ·   select a download below and press C to cancel it" if paste else
            "↑/↓ move   ·   Enter or click selects   ·   D downloads the highlighted result   ·   "
            "select a download and press C to cancel it")
        if self.isVisible():
            self.focus_search()

    def start_link(self, text: str, *, keep_text: bool = False) -> None:
        """Download the pasted *text* with the local yt-dlp, unless it is not a usable link."""
        try:
            url = linkdl.normalize_url(text)
        except linkdl.LinkError as error:
            self.link_panel.show_error(str(error))
            return
        host = urlsplit(url).hostname or url
        try:
            download = self.client.start_link(
                url, name=host.removeprefix("www."), directory=downloads.incoming_dir(self.client.directory),
                final_dir=self.client.directory, meta={"source": "link", "title": host})
        except downloads.DownloadError as error:
            self.link_panel.show_error(str(error))
            return
        row = self._add_row(download.name)[1]
        row.actionRequested.connect(lambda action, row=row: self._link_action(row, action))
        self._bind_row(download, row)
        if not keep_text:
            self.link_panel.clear()
        self.link_panel.show_info("Downloading in the background. You can keep using Chrysa.")

    def _link_action(self, row: DownloadRow, action: str) -> None:
        download = row.download
        if download is None:
            return
        if action == "cookies":
            browser = self._choose_browser(row.action_button)
            if not browser:
                return
            self._put(BROWSER_SETTING, "" if browser == "none" else browser)
        elif action == "proxy":
            current = self._get(PROXY_SETTING)
            text, ok = QInputDialog.getText(
                self, "Proxy", "Proxy address (http://host:port or socks5://host:port).\nLeave empty to stop using one.",
                text=current)
            if not ok:
                return
            try:
                self._put(PROXY_SETTING, linkdl.normalize_proxy(text))
            except linkdl.LinkError as error:
                self.link_panel.show_error(str(error))
                return
        item = self._item_of(row)
        if item is not None:
            self._download_items.pop(id(download), None)
            self._remove_item(item)
        self.start_link(download.target, keep_text=True)

    def _choose_browser(self, anchor: QWidget) -> str:
        """Ask which browser's sign-in yt-dlp may use (it reads the cookies itself, locally). "" = cancelled."""
        menu = QMenu(self)
        names = [b for b in linkdl.BROWSERS if shutil.which(b) or shutil.which(f"{b}-browser")
                 or (b == "chrome" and shutil.which("google-chrome"))] or list(linkdl.BROWSERS)
        picked = {}
        for name in names:
            menu.addAction(name.capitalize()).triggered.connect(lambda _=False, n=name: picked.update(browser=n))
        menu.addSeparator()
        menu.addAction("Don't use a browser").triggered.connect(lambda _=False: picked.update(browser="none"))
        menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))
        return picked.get("browser", "")

    # --------------------------------------------------------------- search

    def _search_typed(self, text: str) -> None:
        """The user changed the search text: ask for suggestions once they pause."""
        self._suggest_id += 1  # whatever is in flight is now stale
        self._searched = False
        if self.internet_allowed and len(suggest.split(text)[0].strip()) >= 2:
            guess = suggest.guess(suggest.split(text)[0])
            if guess:  # earlier answers that still fit show at once; the lookup refines them
                self._fill_suggestions(guess)
            self.suggest_timer.start()
        else:
            self.suggest_timer.stop()
            self.completer.popup().hide()

    def _fetch_suggestions(self) -> None:
        request_id, query = self._suggest_id, suggest.split(self.search_box.text())[0]
        suggest.fetch(query, lambda found: self.suggestionsReady.emit(request_id, query, found))

    def _show_suggestions(self, request_id: int, query: str, found: list) -> None:
        if self._searched or not self.search_box.hasFocus():
            return
        if request_id != self._suggest_id:  # typed on since: an older answer may still fit what is typed now
            found = suggest.guess(suggest.split(self.search_box.text())[0])
            if not found:
                return
        self._fill_suggestions(found)

    def _fill_suggestions(self, found: list) -> None:
        extra = suggest.split(self.search_box.text())[1]
        self.suggestion_model.clear()
        for suggestion in found:
            item = QStandardItem(suggestion.label)
            item.setData(f"{suggestion.title} {extra}".strip(), TITLE_ROLE)  # keeps "s01", "1080p" and the like
            self.suggestion_model.appendRow(item)
        if found:
            self.completer.complete()
        else:
            self.completer.popup().hide()

    def _suggestion_picked(self, title: str) -> None:
        # Enter in the drop-down also reaches the box as returnPressed, which searches; a click does not.
        self._picked = title
        QTimer.singleShot(0, self._search_picked)

    def _search_picked(self) -> None:
        if self._picked is not None:
            self.search_box.setText(self._picked)
            self._run_search()

    def _search_exactly(self, _link: str = "") -> None:
        """"Search instead for …": run what was typed, without correcting it."""
        self.search_box.setText(self._typed_query)
        self._run_search(correct=False)

    def eventFilter(self, obj, event) -> bool:  # Up/Down in the search box drive the result list
        if (obj is self.search_box and event.type() == QEvent.Type.KeyPress and self.results.count()
                and not self.completer.popup().isVisible()):
            if event.key() in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                self.results.setFocus()
                self.results.keyPressEvent(event)
                return True
        return super().eventFilter(obj, event)

    def _run_search(self, correct: bool = True) -> None:
        query = self.search_box.text().strip()
        self._picked = None
        self._searched = True  # suggestions still on their way would cover the results
        self.suggest_timer.stop()
        self.completer.popup().hide()
        if not self.internet_allowed or not query:
            return
        self._request_id += 1
        self._typed_query = query
        self.correction.setVisible(False)
        self._progress, self._expected = {}, 0
        self.status.setText("Fetching results…")
        self.spinner.start()
        self.results.clear()  # the old results make way for the orb until the new ones arrive
        self.detail.show_result(None)
        self.selected_url = None
        self._confirm = None
        if hasattr(self.results, "set_placeholder"):
            self.results.set_placeholder("")
        self.orb.set_style(_dark())
        self.orb.start()
        worker = SearchWorker(self._request_id, query, self, correct=correct)
        worker.corrected.connect(self._on_corrected)
        worker.found.connect(self._on_found)
        worker.failed.connect(self._on_failed)
        worker.progressed.connect(self._on_progress)
        worker.finished.connect(worker.deleteLater)
        self._worker = worker
        worker.start()

    def _on_corrected(self, request_id: int, typed: str, fixed: str) -> None:
        if request_id != self._request_id:
            return
        self.search_box.setText(fixed)
        self.correction.setText(
            f"Showing results for <b>{html.escape(fixed)}</b>  ·  "
            f"<a href=\"exact\">Search instead for “{html.escape(typed)}”</a>")
        self.correction.setVisible(True)

    def _on_progress(self, request_id: int, event: dict) -> None:
        if request_id != self._request_id:
            return
        if event["kind"] == "start":
            self._expected = len(event["sources"])
        elif event["kind"] == "source":
            self._progress[event["source"]] = event["status"]
        self.status.setText(presentation.progress_line(self._expected, self._progress))

    def _on_found(self, request_id: int, found: list[SearchResult]) -> None:
        if request_id != self._request_id:
            return  # a newer search superseded this one
        self.spinner.stop()
        self.orb.stop()
        self.results.set_placeholder("No results", "Try a different title or fewer words.")
        self.set_results(found)
        self.sources_button.refresh()
        summary = f" ({search.last_summary})" if search.last_summary else ""
        self.status.setText(f"{len(found)} result(s).{summary}" if found else f"No results.{summary}")
        if found:
            self.results.setFocus()

    def _on_failed(self, request_id: int, message: str) -> None:
        if request_id == self._request_id:
            self.spinner.stop()
            self.orb.stop()
            self.sources_button.refresh()
            self.status.setText(message)

    def set_results(self, found: list[SearchResult]) -> None:
        self.results.clear()
        self.selected_url = None
        for result in found:
            self._titles[result.target_url] = result.candidate.display_title if result.candidate else result.title
            self._results[result.target_url] = result
            item = QListWidgetItem()
            item.setData(RESULT_ROLE, result)
            self.results.addItem(item)
        self.detail.setVisible(bool(found))
        if found:
            self.results.setCurrentRow(0)

    # ------------------------------------------------------------ selecting

    def _select(self, result: SearchResult) -> None:
        self.selected_url = result.target_url
        self.detail.show_result(result)
        self.status.setText(f"Selected: {result.title} — press D to download.")

    def _download_result(self, result: SearchResult) -> None:
        self._select(result)
        self.start_download(result.target_url)

    # ------------------------------------------------------------ downloads

    def start_download(self, target_url: str) -> None:
        """Hand *target_url* (magnet link or .torrent) to the background client, unless it would be a duplicate."""
        result = self._results.get(target_url)
        candidate = result.candidate if result else None
        reason, hard = acquire.duplicate_reason(
            self.client, self.history, self.db, target_url, candidate.release if candidate else None,
            candidate.info_hash if candidate else sources.info_hash(target_url))
        if reason and (hard or self._confirm != target_url):
            self._confirm = None if hard else target_url
            self.status.setText(reason if hard else f"{reason} Press D again to download it anyway.")
            return
        self._confirm = None
        meta = {**candidate.meta(), "release_key": acquire.release_key(candidate.release)} if candidate else {}
        try:
            download = self.client.start(
                target_url, directory=downloads.incoming_dir(self.client.directory), final_dir=self.client.directory,
                alternatives=candidate.alternatives if candidate else None, meta=meta)
        except downloads.DownloadError as error:
            self.status.setText(str(error))
            return
        title = self._titles.get(target_url) or target_url[:80]
        download.name = download.name or title
        self._bind_row(download, self._add_row(title)[1])
        self.status.setText("Download started in the background. Its file list is checked before anything is fetched.")

    def _add_row(self, title: str, message: str = "") -> tuple[QListWidgetItem, DownloadRow]:
        row = DownloadRow(title)
        if message:
            row.show_message(message)
        item = QListWidgetItem()
        item.setSizeHint(row.sizeHint())
        self.downloads_list.addItem(item)
        self.downloads_list.setItemWidget(item, row)
        self.downloads_list.setVisible(True)
        self.downloads_list.scrollToBottom()
        return item, row

    def _bind_row(self, download: downloads.Download, row: DownloadRow) -> None:
        row.download = download
        self._download_items[id(download)] = (None, row)

    updateReceived = pyqtSignal(object)
    admittedReceived = pyqtSignal(list)

    def _download_updated(self, download: downloads.Download) -> None:  # worker thread
        self.updateReceived.emit(download)

    def _admitted(self, paths: list[str]) -> None:  # worker thread
        self.admittedReceived.emit(list(paths))

    def _item_of(self, row: DownloadRow) -> QListWidgetItem | None:
        return next((self.downloads_list.item(i) for i in range(self.downloads_list.count())
                     if self.downloads_list.itemWidget(self.downloads_list.item(i)) is row), None)

    def _cancel_item(self, item: QListWidgetItem) -> None:
        row = self.downloads_list.itemWidget(item)
        if row.download is not None:
            if row.download.state in ("finished", "verified", "rejected"):
                self.status.setText("That download already finished.")
                return
            row.show_message("Cancelling…")
            self.client.cancel_and_delete(row.download)  # the row goes away when the update arrives
        elif row.entry is not None:
            self.client.discard_saved(row.entry)
            self._remove_item(item)
            self.status.setText("Download cancelled and partial files deleted.")

    def _remove_item(self, item: QListWidgetItem) -> None:
        self.downloads_list.takeItem(self.downloads_list.row(item))
        self.downloads_list.setVisible(self.downloads_list.count() > 0)

    def _show_update(self, download: downloads.Download) -> None:
        entry = self._download_items.pop(id(download), None) if download.discard and download.state == "cancelled" \
            else self._download_items.get(id(download))
        if entry is None:
            return
        item = self._item_of(entry[1])
        if download.discard and download.state == "cancelled":
            if item is not None:
                self._remove_item(item)
            self.status.setText(f"Cancelled “{download.name or 'download'}” and deleted the partial download.")
            return
        entry[1].update_from(download)
        if item is not None:
            item.setSizeHint(entry[1].sizeHint())
        if download.state == "verified":
            self.status.setText(f"“{download.name}” passed validation and was added to {download.final_dir or download.directory}.")
        elif download.state == "rejected":
            self.status.setText(f"“{download.name}” failed validation and was NOT added to the Library.")

    def shutdown(self) -> None:
        self.client.cancel_all()
