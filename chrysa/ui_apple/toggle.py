"""A mobile-style toggle switch coloured by the active theme, and the Browse / Library mode switch."""

from __future__ import annotations

from PyQt6.QtCore import QEasingCurve, QPropertyAnimation, QRectF, QSize, Qt, pyqtProperty
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QAbstractButton, QHBoxLayout, QLabel, QWidget

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
        self._animation.setDuration(140)
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


class ModeSwitch(QWidget):
    """``Browse  [toggle]  Library`` — left is Search & Download, right is the library."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.toggle = ToggleSwitch()
        self.toggle.setChecked(True)
        self.toggle.setToolTip("Left: Browse (search & download) · Right: Library (Ctrl+D)")
        self.toggle.toggled.connect(lambda _checked: self.apply_theme())
        self.browse = QLabel("Browse")
        self.library = QLabel("Library")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        layout.addWidget(self.browse)
        layout.addWidget(self.toggle)
        layout.addWidget(self.library)
        for label, side in ((self.browse, False), (self.library, True)):
            label.setCursor(Qt.CursorShape.PointingHandCursor)
            label.mousePressEvent = lambda _event, side=side: self.toggle.setChecked(side)
        self.apply_theme()

    def apply_theme(self) -> None:
        for label, active in ((self.browse, not self.toggle.isChecked()), (self.library, self.toggle.isChecked())):
            colour = theme.TEXT if active else theme.TEXT_MUTED
            label.setStyleSheet(f"color: {colour}; font-size: 16px; font-weight: 700;")
