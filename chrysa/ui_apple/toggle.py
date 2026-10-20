"""A mobile-style toggle switch coloured by the active theme, and the Browse / Library mode switch."""

from __future__ import annotations

from PyQt6.QtCore import QEasingCurve, QPropertyAnimation, QRectF, QSize, Qt, pyqtProperty
from PyQt6.QtGui import QColor, QFontMetrics, QPainter, QPen
from PyQt6.QtWidgets import QAbstractButton, QHBoxLayout, QWidget

from . import theme



class ToggleSwitch(QAbstractButton):
    """Checked = thumb on the right. ``toggled(bool)`` fires on every change."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setFixedSize(QSize(76, 40))
        self._position = 0.0  # 0 = left, 1 = right
        self._animation = QPropertyAnimation(self, b"position", self)
        self._animation.setDuration(theme.ms("quick"))
        self._animation.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self.toggled.connect(self._animate)

    def sizeHint(self) -> QSize:
        return QSize(76, 40)

    def _animate(self, checked: bool) -> None:
        self._animation.stop()
        self._animation.setStartValue(self._position)
        self._animation.setEndValue(1.0 if checked else 0.0)
        self._animation.start()

    def set_checked_silently(self, checked: bool) -> None:
        """Move the switch without emitting ``toggled`` (for syncing two switches)."""
        if checked == self.isChecked():
            return
        blocked = self.blockSignals(True)
        self.setChecked(checked)
        self.blockSignals(blocked)
        self._animate(checked)

    def _get_position(self) -> float:
        return self._position

    def _set_position(self, value: float) -> None:
        self._position = value
        self.update()

    position = pyqtProperty(float, fget=_get_position, fset=_set_position)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        radius = rect.height() / 2
        painter.setPen(QPen(QColor(theme.ACCENT if self.hasFocus() else theme.BORDER), 1.6))
        painter.setBrush(QColor(theme.SURFACE))
        painter.drawRoundedRect(rect, radius, radius)
        diameter = rect.height() - 8
        x = rect.left() + 4 + (rect.width() - diameter - 8) * self._position
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.ACCENT if self.isEnabled() else theme.BORDER))
        painter.drawEllipse(QRectF(x, rect.top() + 4, diameter, diameter))


class SegmentedControl(QAbstractButton):
    """Two labelled segments with a sliding thumb. Checked = the right segment.

    Behaves like the toggle it replaces (``toggled(bool)``, ``set_checked_silently``) and also
    answers to Left/Right and Space when focused, so it works without a mouse.
    """

    def __init__(self, left: str, right: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._labels = (left, right)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setFixedHeight(32)
        self._position = 0.0
        self._ring = False  # draw the focus ring only for keyboard focus
        self._animation = QPropertyAnimation(self, b"position", self)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animate)

    def sizeHint(self) -> QSize:
        metrics = QFontMetrics(theme.type_font("bodyEmphasis"))
        width = max(metrics.horizontalAdvance(text) for text in self._labels) + 36
        return QSize(width * 2 + 6, 32)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def _animate(self, checked: bool) -> None:
        self._animation.stop()
        self._animation.setDuration(theme.ms("quick"))
        self._animation.setStartValue(self._position)  # from where the thumb is now: interruptible
        self._animation.setEndValue(1.0 if checked else 0.0)
        self._animation.start()

    def set_checked_silently(self, checked: bool) -> None:
        """Move the control without emitting ``toggled`` (for syncing from code)."""
        if checked == self.isChecked():
            return
        blocked = self.blockSignals(True)
        self.setChecked(checked)
        self.blockSignals(blocked)
        self._animate(checked)

    def focusInEvent(self, event) -> None:
        self._ring = event.reason() in (
            Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason, Qt.FocusReason.ShortcutFocusReason
        )
        super().focusInEvent(event)

    def mousePressEvent(self, event) -> None:  # respond on press, not release
        self.setChecked(event.position().x() >= self.width() / 2)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        event.accept()

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key == Qt.Key.Key_Left:
            self.setChecked(False)
        elif key == Qt.Key.Key_Right:
            self.setChecked(True)
        elif key in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.setChecked(not self.isChecked())
        else:
            super().keyPressEvent(event)

    def _get_position(self) -> float:
        return self._position

    def _set_position(self, value: float) -> None:
        self._position = value
        self.update()

    position = pyqtProperty(float, fget=_get_position, fset=_set_position)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        radius = theme.RADIUS_S + 2
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_tint(0.09))
        painter.drawRoundedRect(track, radius, radius)
        half = track.width() / 2
        thumb = QRectF(track.left() + 2 + (half - 2) * self._position, track.top() + 2, half - 2, track.height() - 4)
        painter.setBrush(QColor("#636366") if theme.SCHEME == "dark" else QColor("#ffffff"))
        painter.drawRoundedRect(thumb, radius - 2, radius - 2)
        if self.hasFocus() and self._ring:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            ring = QColor(theme.ACCENT)
            ring.setAlphaF(0.85)
            painter.setPen(QPen(ring, 2))
            painter.drawRoundedRect(track.adjusted(-0.5, -0.5, 0.5, 0.5), radius, radius)
        painter.setFont(theme.type_font("bodyEmphasis"))
        for index, text in enumerate(self._labels):
            cell = QRectF(track.left() + half * index, track.top(), half, track.height())
            active = self.isChecked() == bool(index)
            painter.setPen(QColor(theme.TEXT if active else theme.TEXT_MUTED))
            painter.drawText(cell, Qt.AlignmentFlag.AlignCenter, text)
        painter.end()


def _tint(alpha: float) -> QColor:
    colour = QColor(theme.TEXT)
    colour.setAlphaF(alpha)
    return colour


class ModeSwitch(QWidget):
    """``Browse | Library`` — left is Search & Download, right is the library."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.toggle = SegmentedControl("Browse", "Library")
        self.toggle.setChecked(True)
        self.toggle.setToolTip("Browse (search & download) or Library (Ctrl+D)")
        self.toggle.setAccessibleName("Mode")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.toggle)

    def apply_theme(self) -> None:
        self.toggle.update()
