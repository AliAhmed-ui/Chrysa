"""The "scraping" orb shown in the Browse results box while a search runs.

A port of the ``breathing`` state of thinking-orbs (https://github.com/Jakubantalik/thinking-orbs, MIT, (c) Jakub
Antalik): a dotted ring whose radius undulates. The maths is a straight translation of its ``ribbon.ts`` (face-on
variant) with the shipped 64px tuning; only the painting is Qt.
"""

from __future__ import annotations

import math
import time

from PyQt6.QtCore import QEvent, QObject, QPointF, QRect, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QPainter
from PyQt6.QtWidgets import QWidget

_SPEED = 3.24
_CAM_TILT = 0.3
_LANES = 11  # round(round(5 * sqrt(0.25)) * 3.627)
_SEGS = 44  # round(88 * sqrt(0.25))
_R_BASE = 1.1 * 0.956
_R_DEPTH = 1.7 * 0.956
_WOB_MUL = 0.368
_R_MIN = 0.3


def _frame(size: float, t: float) -> list[tuple[float, float, float, float, float]]:
    """Dots for one instant as ``(x, y, z, radius, ink)`` (ink 0..1, 0 = darkest), far to near."""
    cx = cy = size / 2
    big_r = size / 2 * 0.78
    st, ct = math.sin(_CAM_TILT), math.cos(_CAM_TILT)  # yaw is frozen at 0 (spin = 0)
    rs = (size / 300) ** 0.6

    def project(x: float, y: float, z: float) -> tuple[float, float, float]:
        y1 = y * ct - z * st
        return cx + x, cy - y1, y * st + z * ct

    # face-on plane: u = +x, v = (0, 1, 0) rotated by ta = -camTilt
    ta = -_CAM_TILT
    ux, uy, uz = 1.0, 0.0, 0.0
    vx, vy, vz = 0.0, math.cos(ta), math.sin(ta)
    nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx

    wob_amp = 0.23 * _WOB_MUL
    base_r = big_r / (1 + 0.85 * wob_amp)
    dots = []
    for w in range(_LANES):
        lane_off = (w - (_LANES - 1) / 2) * 0.075
        edge = abs(w - (_LANES - 1) / 2) / max(1, (_LANES - 1) / 2)
        for k in range(_SEGS):
            a = k / _SEGS * 2 * math.pi
            wob = (0.16 * math.sin(a * 3 - t * 1.7 + w * 0.22) + 0.07 * math.sin(a * 5 + t * 1.1)) * _WOB_MUL
            x = ux * math.cos(a) + vx * math.sin(a) + nx * lane_off
            y = uy * math.cos(a) + vy * math.sin(a) + ny * lane_off
            z = uz * math.cos(a) + vz * math.sin(a) + nz * lane_off
            length = math.sqrt(x * x + y * y + z * z)
            rr = base_r * (1 + wob)
            px, py, zr = project(x / length * rr, y / length * rr, z / length * rr)
            depth = (zr / big_r + 1) / 2
            radius = (_R_BASE + _R_DEPTH * depth) * (1 - 0.25 * edge) * rs
            dots.append((px, py, zr, max(_R_MIN, radius), 0.52 - 0.44 * depth + 0.18 * edge, 0.4 + 0.6 * depth))
    dots.sort(key=lambda dot: dot[2])
    return dots


class ScrapingOrb(QWidget):
    """Covers its parent (a view's viewport), animating a ring orb and a label until :meth:`stop`."""

    SIZE = 112

    def __init__(self, parent: QWidget, dark: bool, text: str = "Scraping", ink: str | None = None) -> None:
        super().__init__(parent)
        self._dark = dark
        self._text = text
        self._ink = QColor(ink) if ink else None  # optional tint; otherwise the library's grey ramp
        self._start = time.monotonic()
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self.update)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        parent.installEventFilter(self)
        self.hide()

    def set_style(self, dark: bool, ink: str | None = None) -> None:
        self._dark = dark
        self._ink = QColor(ink) if ink else None

    def start(self) -> None:
        self._start = time.monotonic()
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.show()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        self.hide()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self.setGeometry(self.parentWidget().rect())
        return False

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        size = self.SIZE
        origin = QPointF(self.width() / 2 - size / 2, self.height() / 2 - size / 2 - 14)
        t = (time.monotonic() - self._start) * _SPEED
        painter.setPen(Qt.PenStyle.NoPen)
        for x, y, _z, radius, white, alpha in _frame(size, t):
            if self._ink is not None:
                colour = QColor(self._ink)
                colour.setAlphaF(alpha)
            else:
                grey = round((1 - white if self._dark else white) * 255)
                colour = QColor(grey, grey, grey)
                colour.setAlphaF(alpha)
            painter.setBrush(colour)
            painter.drawEllipse(QPointF(origin.x() + x, origin.y() + y), radius, radius)
        font = QFont(self.font())
        font.setPointSize(13)
        font.setWeight(QFont.Weight.Medium)
        painter.setFont(font)
        label = QColor(self._ink) if self._ink else QColor(200, 200, 200) if self._dark else QColor(90, 90, 90)
        label.setAlphaF(0.55 + 0.35 * (0.5 + 0.5 * math.sin(t * 0.6)))
        painter.setPen(label)
        painter.drawText(QRect(0, int(origin.y() + size + 4), self.width(), 28), Qt.AlignmentFlag.AlignCenter, self._text)
        painter.end()
