"""Asynchronous artwork loading with an in-memory LRU cache.

Cards ask for an image at the exact size they paint. Decoding and scaling
happen on a thread pool; the widget is repainted when its image arrives, so
scrolling never waits on disk.
"""

from __future__ import annotations

import weakref
from collections import OrderedDict

from PyQt6.QtCore import QObject, QRect, QRunnable, QSize, Qt, QThreadPool, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QImageReader, QPainter, QPixmap
from PyQt6.QtWidgets import QWidget

_CACHE_BYTES = 160 * 1024 * 1024


class _Signals(QObject):
    done = pyqtSignal(str, QImage)


class _LoadTask(QRunnable):
    def __init__(self, key: str, path: str, width: int, height: int, signals: _Signals) -> None:
        super().__init__()
        self._key, self._path, self._width, self._height = key, path, width, height
        self._signals = signals

    def run(self) -> None:
        try:
            self._signals.done.emit(self._key, render_cover(self._path, self._width, self._height))
        except RuntimeError:
            pass  # the application is shutting down


def render_cover(path: str, width: int, height: int) -> QImage:
    """Load *path* and fit it into ``width`` × ``height`` pixels.

    Landscape frames are scaled to cover the area and centre-cropped. A portrait
    poster would lose its top and bottom that way, so it is shown whole on top of
    a blurred, darkened copy of itself instead.
    """
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    image = reader.read()
    if image.isNull() or width <= 0 or height <= 0:
        return QImage()
    target = QSize(width, height)
    portrait = image.height() > image.width() * 1.15 and width > height
    cover = image.scaled(
        target, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation
    )
    x, y = (cover.width() - width) // 2, (cover.height() - height) // 2
    cover = cover.copy(QRect(x, y, width, height))
    if not portrait:
        return cover

    # Cheap blur: shrink to a few pixels and stretch back.
    blurred = cover.scaled(
        max(1, width // 24), max(1, height // 24),
        Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation,
    ).scaled(target, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    canvas = blurred.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    whole = image.scaled(target, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    painter = QPainter(canvas)
    painter.fillRect(canvas.rect(), QColor(0, 0, 0, 120))
    painter.drawImage((width - whole.width()) // 2, (height - whole.height()) // 2, whole)
    painter.end()
    return canvas


class ImageLoader(QObject):
    """Hands out scaled pixmaps, loading them in the background on first request."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._cache: OrderedDict[str, QPixmap] = OrderedDict()
        self._bytes = 0
        self._waiting: dict[str, list[weakref.ref]] = {}
        self._dpr: dict[str, float] = {}
        self._failed: set[str] = set()
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(3)
        self._signals = _Signals(self)
        self._signals.done.connect(self._on_loaded)

    def get(self, path: str | None, size: QSize, dpr: float, requester: QWidget) -> QPixmap | None:
        """The pixmap for *path* at *size*, or None while it is being loaded.

        *requester* is repainted (``update()``) once the image is available.
        """
        if not path or size.isEmpty():
            return None
        width, height = round(size.width() * dpr), round(size.height() * dpr)
        key = f"{path}\n{width}x{height}"
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        if key in self._failed:
            return None
        waiting = self._waiting.get(key)
        if waiting is None:
            self._waiting[key] = [weakref.ref(requester)]
            self._dpr[key] = dpr
            self._pool.start(_LoadTask(key, path, width, height, self._signals))
        elif not any(ref() is requester for ref in waiting):
            waiting.append(weakref.ref(requester))
        return None

    def forget(self, path: str) -> None:
        """Drop cached renderings of *path* (its file was replaced)."""
        prefix = path + "\n"
        for key in [k for k in self._cache if k.startswith(prefix)]:
            pixmap = self._cache.pop(key)
            self._bytes -= pixmap.width() * pixmap.height() * 4
        self._failed = {k for k in self._failed if not k.startswith(prefix)}

    def shutdown(self) -> None:
        self._pool.clear()
        self._pool.waitForDone(2000)

    def _on_loaded(self, key: str, image: QImage) -> None:
        waiting = self._waiting.pop(key, [])
        dpr = self._dpr.pop(key, 1.0)
        if image.isNull():
            self._failed.add(key)
        else:
            pixmap = QPixmap.fromImage(image)
            pixmap.setDevicePixelRatio(dpr)
            self._cache[key] = pixmap
            self._bytes += pixmap.width() * pixmap.height() * 4
            while self._bytes > _CACHE_BYTES and len(self._cache) > 1:
                _old_key, old = self._cache.popitem(last=False)
                self._bytes -= old.width() * old.height() * 4
        for ref in waiting:
            widget = ref()
            if widget is not None:
                try:
                    widget.update()
                except RuntimeError:
                    pass  # the Qt object was deleted while its image was loading
