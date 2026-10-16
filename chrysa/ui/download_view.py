"""Search & Download mode: an online search with keyboard-driven results and a torrent client."""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QModelIndex, QObject, QRect, QSize, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QKeyEvent, QPainter
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from .. import downloads, search
from ..search import SearchError, SearchResult
from . import theme

RESULT_ROLE = Qt.ItemDataRole.UserRole
WARNING_TEXT = "Warning: Searching here will enable the app to access the internet."


class SearchWorker(QThread):
    found = pyqtSignal(int, object)  # request id, list[SearchResult]
    failed = pyqtSignal(int, str)

    def __init__(self, request_id: int, query: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.request_id = request_id
        self.query = query

    def run(self) -> None:
        try:
            self.found.emit(self.request_id, search.search(self.query))
        except SearchError as error:
            self.failed.emit(self.request_id, str(error))
        except Exception as error:
            self.failed.emit(self.request_id, f"{type(error).__name__}: {error}")


class ResultDelegate(QStyledItemDelegate):
    """Two lines per row: the title, then the metadata string."""

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        return QSize(option.rect.width(), 62)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        result: SearchResult = index.data(RESULT_ROLE)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect.adjusted(4, 2, -4, -2)
        if option.state & QStyle.StateFlag.State_Selected:
            painter.setBrush(QColor(theme.SURFACE_RAISED))
            painter.setPen(QColor(theme.ACCENT_ALT))
            painter.drawRoundedRect(rect, 8, 8)
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.setBrush(QColor(theme.SURFACE))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect, 8, 8)
        text_rect = rect.adjusted(14, 8, -14, -8)
        title_font = QFont(option.font)
        title_font.setPixelSize(15)
        title_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.setPen(QColor(theme.TEXT))
        title = painter.fontMetrics().elidedText(result.title, Qt.TextElideMode.ElideRight, text_rect.width())
        painter.drawText(QRect(text_rect.left(), text_rect.top(), text_rect.width(), 22), Qt.AlignmentFlag.AlignVCenter, title)
        meta_font = QFont(option.font)
        meta_font.setPixelSize(12)
        painter.setFont(meta_font)
        painter.setPen(QColor(theme.TEXT_MUTED))
        painter.drawText(
            QRect(text_rect.left(), text_rect.top() + 24, text_rect.width(), 18), Qt.AlignmentFlag.AlignVCenter, result.metadata
        )
        painter.restore()


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
        self.itemClicked.connect(lambda item: self.selectRequested.emit(item.data(RESULT_ROLE)))

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


class DownloadPage(QWidget):
    """The whole Search & Download view."""

    def __init__(self, client_dir=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("browser")
        self.internet_allowed = False
        self.selected_url: str | None = None
        self._request_id = 0
        self._worker: SearchWorker | None = None
        self.client = downloads.DownloadClient(client_dir or downloads.DEFAULT_DIR, self._download_updated)
        self._download_items: dict[int, QListWidgetItem] = {}
        self.updateReceived.connect(self._show_update)

        header = QHBoxLayout()
        title = QLabel("Search & Download")
        title.setObjectName("pageTitle")
        header.addWidget(title)
        header.addStretch(1)

        self.search_box = QLineEdit()
        self.search_box.setObjectName("searchBox")
        self.search_box.setMinimumHeight(52)
        self.search_box.setStyleSheet("font-size: 17px; padding-left: 14px;")
        self.search_box.setPlaceholderText("Search for a video…")
        self.search_box.setEnabled(False)
        self.search_box.returnPressed.connect(self._run_search)
        self.search_box.installEventFilter(self)

        self.warning = QLabel(WARNING_TEXT)
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet("color: #ff4d4d; font-size: 13px; font-weight: 600;")
        self.allow_button = QPushButton("Allow Internet Access")
        self.allow_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.allow_button.clicked.connect(self.allow_internet)
        warning_row = QHBoxLayout()
        warning_row.addWidget(self.warning, 1)
        warning_row.addWidget(self.allow_button)

        self.results = ResultList()
        self.results.selectRequested.connect(self._select)
        self.results.downloadRequested.connect(self._download_result)
        self.status = QLabel("Allow internet access to start searching.")
        self.status.setObjectName("hint")
        self.status.setWordWrap(True)
        self.downloads_list = QListWidget()
        self.downloads_list.setObjectName("downloadList")
        self.downloads_list.setMaximumHeight(110)
        self.downloads_list.setVisible(False)
        self.downloads_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.SIDE_MARGIN, 24, theme.SIDE_MARGIN, 20)
        layout.setSpacing(12)
        layout.addLayout(header)
        layout.addSpacing(6)
        layout.addWidget(self.search_box)
        layout.addLayout(warning_row)
        layout.addWidget(self.results, 1)
        layout.addWidget(self.status)
        layout.addWidget(self.downloads_list)
        hint = QLabel("↑/↓ move   ·   Enter or click selects   ·   D downloads the highlighted result")
        hint.setObjectName("statusLabel")
        layout.addWidget(hint)

    # ------------------------------------------------------------- internet

    def allow_internet(self) -> None:
        self.internet_allowed = True
        self.allow_button.setEnabled(False)
        self.allow_button.setText("Internet Access Allowed")
        self.search_box.setEnabled(True)
        self.search_box.setFocus()
        self.status.setText("Type a title and press Enter.")

    def focus_search(self) -> None:
        if self.search_box.isEnabled():
            self.search_box.setFocus()
        else:
            self.allow_button.setFocus()

    # --------------------------------------------------------------- search

    def eventFilter(self, obj, event) -> bool:  # Up/Down in the search box drive the result list
        if obj is self.search_box and event.type() == QEvent.Type.KeyPress and self.results.count():
            if event.key() in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                self.results.setFocus()
                self.results.keyPressEvent(event)
                return True
        return super().eventFilter(obj, event)

    def _run_search(self) -> None:
        query = self.search_box.text().strip()
        if not self.internet_allowed or not query:
            return
        self._request_id += 1
        self.status.setText(f"Searching for “{query}”…")
        worker = SearchWorker(self._request_id, query, self)
        worker.found.connect(self._on_found)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(worker.deleteLater)
        self._worker = worker
        worker.start()

    def _on_found(self, request_id: int, found: list[SearchResult]) -> None:
        if request_id != self._request_id:
            return  # a newer search superseded this one
        self.set_results(found)
        self.status.setText(f"{len(found)} result(s)." if found else "No results.")
        if found:
            self.results.setFocus()

    def _on_failed(self, request_id: int, message: str) -> None:
        if request_id == self._request_id:
            self.status.setText(message)

    def set_results(self, found: list[SearchResult]) -> None:
        self.results.clear()
        self.selected_url = None
        for result in found:
            item = QListWidgetItem()
            item.setData(RESULT_ROLE, result)
            self.results.addItem(item)
        if found:
            self.results.setCurrentRow(0)

    # ------------------------------------------------------------ selecting

    def _select(self, result: SearchResult) -> None:
        self.selected_url = result.target_url
        self.status.setText(f"Selected: {result.title} — press D to download.")

    def _download_result(self, result: SearchResult) -> None:
        self._select(result)
        self.start_download(result.target_url)

    # ------------------------------------------------------------ downloads

    def start_download(self, target_url: str) -> None:
        """Hand *target_url* (magnet link or .torrent) to the background client."""
        try:
            download = self.client.start(target_url)
        except downloads.DownloadError as error:
            self.status.setText(str(error))
            return
        item = QListWidgetItem(f"Starting… {target_url[:80]}")
        self.downloads_list.addItem(item)
        self._download_items[id(download)] = item
        self.downloads_list.setVisible(True)
        self.status.setText("Download started in the background.")

    updateReceived = pyqtSignal(object)

    def _download_updated(self, download: downloads.Download) -> None:  # worker thread
        self.updateReceived.emit(download)

    def _show_update(self, download: downloads.Download) -> None:
        item = self._download_items.get(id(download))
        if item:
            label = download.name or download.target[:60]
            item.setText(f"{label} — {download.state} {download.progress * 100:.0f}%  {download.message}")

    def shutdown(self) -> None:
        self.client.cancel_all()
