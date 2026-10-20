"""Search & Download mode: an online search with keyboard-driven results and a torrent client."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QEvent, QTimer, QModelIndex, QObject, QRect, QSize, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QKeyEvent, QPainter, QTransform
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from .. import downloads, search
from ..search import SearchError, SearchResult
from . import icons, theme

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


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


class DownloadRow(QWidget):
    """One download: name, progress bar, then ``done of total · speed · ETA · peers``."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("downloadRow")
        self.title = QLabel(title)
        self.title.setObjectName("downloadTitle")
        self.bar = QProgressBar()
        self.bar.setObjectName("downloadBar")
        self.bar.setRange(0, 1000)
        self.bar.setFixedHeight(18)
        self.download: downloads.Download | None = None  # set while running
        self.entry: dict | None = None  # set while interrupted, awaiting restart
        self.state = QLabel("Starting…")
        self.state.setObjectName("statusLabel")
        self.speed = QLabel()
        self.speed.setObjectName("downloadSpeed")
        self.speed.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        info = QHBoxLayout()
        info.addWidget(self.state, 1)
        info.addWidget(self.speed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(6)
        layout.addWidget(self.title)
        layout.addWidget(self.bar)
        layout.addLayout(info)

    def show_message(self, message: str) -> None:
        self.state.setText(message)
        self.speed.clear()

    def update_from(self, download: downloads.Download) -> None:
        if download.name:
            self.title.setText(download.name)
        self.bar.setValue(round(download.progress * 1000))
        self.bar.setFormat(f"{download.progress * 100:.1f}%")
        if download.state == "downloading" and download.size_total:
            parts = [f"{human_size(download.size_done)} of {human_size(download.size_total)}"]
            if download.peers:
                parts.append(f"{download.peers} peers")
            if download.eta and download.speed:
                parts.append(f"{download.eta} left")
            self.state.setText(" · ".join(parts))
            self.speed.setText(f"↓ {human_size(download.speed)}/s")
        else:
            self.state.setText(download.message or download.state.capitalize())
            self.speed.setText(f"↓ {human_size(download.speed)}/s" if download.speed else "")


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

    def __init__(self, client_dir=None, parent: QWidget | None = None, state_file=None) -> None:
        super().__init__(parent)
        self.setObjectName("browser")
        self.internet_allowed = False
        self.selected_url: str | None = None
        self._request_id = 0
        self._worker: SearchWorker | None = None
        self.client = downloads.DownloadClient(
            client_dir or downloads.DEFAULT_DIR, self._download_updated, state_file)
        
        self._download_items: dict[int, tuple[QListWidgetItem | None, DownloadRow]] = {}
        self._titles: dict[str, str] = {}  # target url -> result title, for the download row
        self.updateReceived.connect(self._show_update)

        header = QHBoxLayout()
        title = QLabel("Search & Download")
        title.setObjectName("pageTitle")
        header.addWidget(title)
        header.addStretch(1)
        self.folder_label = QLabel()
        self.folder_label.setObjectName("statusLabel")
        self.folder_button = QPushButton("Change download folder")
        self.folder_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.folder_button.clicked.connect(self.changeFolderRequested)
        header.addWidget(self.folder_label)
        header.addWidget(self.folder_button)
        self.set_download_dir(self.client.directory)

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
        self.downloads_list = DownloadList()
        self.downloads_list.setObjectName("downloadList")
        self.downloads_list.setMinimumHeight(200)
        self.downloads_list.setMaximumHeight(280)
        self.downloads_list.setVisible(False)
        self.downloads_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.downloads_list.setToolTip("Select a download and press C to cancel it and delete the partial file")
        self.downloads_list.cancelRequested.connect(self._cancel_item)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.SIDE_MARGIN, 24, theme.SIDE_MARGIN, 20)
        layout.setSpacing(12)
        layout.addLayout(header)
        layout.addSpacing(6)
        layout.addWidget(self.search_box)
        layout.addLayout(warning_row)
        layout.addWidget(self.results, 1)
        self.spinner = DiscSpinner()
        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        status_row.addWidget(self.spinner)
        status_row.addWidget(self.status, 1)
        layout.addLayout(status_row)
        layout.addWidget(self.downloads_list)
        for entry in self.client.saved():
            row = self._add_row(entry.get("name") or entry["target"][:80], "Interrupted — resumes when internet access is allowed")[1]
            row.entry = entry
        hint = QLabel("↑/↓ move   ·   Enter or click selects   ·   D downloads the highlighted result   ·   select a download and press C to cancel it")
        hint.setObjectName("statusLabel")
        layout.addWidget(hint)

    # -------------------------------------------------------- download folder

    def set_download_dir(self, directory) -> None:
        """New downloads go to *directory*; ones already running keep their folder."""
        self.client.directory = Path(directory)
        text = str(self.client.directory)
        self.folder_label.setText(self.folder_label.fontMetrics().elidedText(
            f"Saving to {text}", Qt.TextElideMode.ElideMiddle, 420))
        self.folder_label.setToolTip(text)

    # ------------------------------------------------------------- internet

    def allow_internet(self) -> None:
        self.internet_allowed = True
        self.allow_button.setEnabled(False)
        self.allow_button.setText("Internet Access Allowed")
        self.search_box.setEnabled(True)
        self.search_box.setFocus()
        self.status.setText("Type a title and press Enter.")
        self._resume_interrupted()

    def _resume_interrupted(self) -> None:
        rows = [self.downloads_list.itemWidget(self.downloads_list.item(i)) for i in range(self.downloads_list.count())]
        for row in rows:
            if row.entry is None:
                continue
            entry, row.entry = row.entry, None
            try:
                download = self.client.start(entry["target"], entry.get("name", ""), entry.get("directory"), entry.get("files"))
            except downloads.DownloadError as error:
                row.show_message(str(error))
                continue
            self._bind_row(download, row)
            row.show_message("Resuming…")

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
        self.status.setText("Fetching results…")
        self.spinner.start()
        worker = SearchWorker(self._request_id, query, self)
        worker.found.connect(self._on_found)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(worker.deleteLater)
        self._worker = worker
        worker.start()

    def _on_found(self, request_id: int, found: list[SearchResult]) -> None:
        if request_id != self._request_id:
            return  # a newer search superseded this one
        self.spinner.stop()
        self.set_results(found)
        self.status.setText(f"{len(found)} result(s)." if found else "No results.")
        if found:
            self.results.setFocus()

    def _on_failed(self, request_id: int, message: str) -> None:
        if request_id == self._request_id:
            self.spinner.stop()
            self.status.setText(message)

    def set_results(self, found: list[SearchResult]) -> None:
        self.results.clear()
        self.selected_url = None
        for result in found:
            self._titles[result.target_url] = result.title
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
        title = self._titles.get(target_url) or target_url[:80]
        download.name = download.name or title
        self._bind_row(download, self._add_row(title)[1])
        self.status.setText("Download started in the background.")

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

    def _download_updated(self, download: downloads.Download) -> None:  # worker thread
        self.updateReceived.emit(download)

    def _cancel_item(self, item: QListWidgetItem) -> None:
        row = self.downloads_list.itemWidget(item)
        if row.download is not None:
            if row.download.state == "finished":
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
        if download.discard and download.state == "cancelled":
            item = next((self.downloads_list.item(i) for i in range(self.downloads_list.count())
                         if self.downloads_list.itemWidget(self.downloads_list.item(i)) is entry[1]), None)
            if item is not None:
                self._remove_item(item)
            self.status.setText(f"Cancelled “{download.name or 'download'}” and deleted the partial download.")
        else:
            entry[1].update_from(download)

    def shutdown(self) -> None:
        self.client.cancel_all()
