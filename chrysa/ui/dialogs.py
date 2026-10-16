"""The library-folders dialog."""

from __future__ import annotations

import os

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..config import default_media_dir
from ..library import Library
from ..models import plural


class FoldersDialog(QDialog):
    """Add or remove the folders Chrysa scans. ``changed`` tells the caller to rescan."""

    def __init__(self, library: Library, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._library = library
        self.changed = False
        self.setWindowTitle("Library Folders")
        self.setModal(True)
        self.resize(640, 420)

        title = QLabel("Library Folders")
        title.setObjectName("dialogTitle")
        hint = QLabel(
            "Chrysa looks for movies, TV shows and other videos in these folders, including "
            "everything inside them. Nothing on disk is ever modified."
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        self._list = QListWidget()
        self._list.itemSelectionChanged.connect(self._update_buttons)

        add = QPushButton("Add Folder…")
        add.setObjectName("primary")
        add.clicked.connect(self._add)
        self._remove = QPushButton("Remove")
        self._remove.clicked.connect(self._remove_selected)
        done = QPushButton("Done")
        done.clicked.connect(self.accept)
        done.setDefault(True)
        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addWidget(add)
        buttons.addWidget(self._remove)
        buttons.addStretch(1)
        buttons.addWidget(done)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(14)
        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addWidget(self._list, 1)
        layout.addLayout(buttons)
        self._refresh()

    def _refresh(self) -> None:
        self._list.clear()
        for folder in self._library.folders():
            status = plural(folder.media_count, "video") if folder.available else "not available right now"
            item = QListWidgetItem(f"{folder.path}\n{status}")
            item.setData(Qt.ItemDataRole.UserRole, folder.id)
            item.setToolTip(folder.path)
            self._list.addItem(item)
        self._update_buttons()

    def _update_buttons(self) -> None:
        self._remove.setEnabled(bool(self._list.selectedItems()))

    def _add(self) -> None:
        folders = self._library.folders()
        start = os.path.dirname(folders[-1].path) if folders else str(default_media_dir().parent)
        chosen = QFileDialog.getExistingDirectory(self, "Add a folder to your library", start)
        if not chosen:
            return
        folder, message = self._library.add_folder(chosen)
        if folder is None:
            QMessageBox.information(self, "Library Folders", message)
            return
        self.changed = True
        self._refresh()

    def _remove_selected(self) -> None:
        items = self._list.selectedItems()
        if not items:
            return
        folder_id = items[0].data(Qt.ItemDataRole.UserRole)
        path = items[0].text().split("\n", 1)[0]
        answer = QMessageBox.question(
            self, "Remove folder",
            f"Remove this folder from the library?\n\n{path}\n\n"
            "The files stay on disk, but Chrysa forgets how far you watched them.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._library.remove_folder(folder_id)
            self.changed = True
            self._refresh()
