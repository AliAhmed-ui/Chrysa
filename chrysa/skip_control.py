"""The "Skip Intro" control of the player: a compact, understated pill that belongs over the picture.

libVLC draws straight into its own X11/Win32 window, so nothing Qt paints can be blended over the video: like the
player's bars, this control is an opaque native child window. It therefore cannot fade by alpha. Instead it
*arrives* the way modern players' prompts do: the pill grows out of its right edge while sliding up a few pixels, and
its icon and label fade in as it widens; it leaves the same way in reverse. The window is masked to the pill, so
nothing but the pill is ever drawn or clickable.

Both designs use it; each passes its own theme module for the accent colour and (optionally) reduced-motion.
"""

from __future__ import annotations

from PyQt6.QtCore import QEasingCurve, QPointF, QRectF, Qt, QVariantAnimation, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QKeyEvent, QPainter, QPainterPath, QPen, QRegion
from PyQt6.QtWidgets import QApplication, QWidget

# Always over video, so the control is always dark: the theme only supplies the accent.
_FILL = QColor("#17181c")
_FILL_HOVER = QColor("#24262c")
_FILL_PRESSED = QColor("#0f1013")
_EDGE = QColor("#3b3d44")
_TEXT = QColor("#f5f5f7")

_BASE_HEIGHT = 36  # at the 720 p reference window; scaled with the window (see set_scale)
_SLIDE = 9  # px the pill rises while arriving
_APPEAR_MS = 260
_LEAVE_MS = 170


class SkipIntroControl(QWidget):
    """Shows ``»  Skip Intro``; ``activated`` fires once per showing, from the mouse or the keyboard."""

    activated = pyqtSignal()

    def __init__(self, parent: QWidget, theme) -> None:
        super().__init__(parent)
        self._theme = theme
        self._scale = 1.0
        self._progress = 0.0  # 0 = gone, 1 = fully shown
        self._shown = False  # the state asked for (the animation may still be on its way)
        self._used = False  # activated: ignore further input until shown again
        self._hover = False
        self._pressed = False
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)  # Tab reaches it; Space/Enter then press it
        self.setAccessibleName("Skip Intro")
        self.setToolTip("Skip intro  (Enter)")
        self._animation = QVariantAnimation(self)
        self._animation.valueChanged.connect(self._set_progress)
        self._animation.finished.connect(self._animation_finished)
        self._apply_metrics()
        self.hide()

    # ------------------------------------------------------------- state

    @property
    def is_presented(self) -> bool:
        """True from the moment it is asked to show until it is asked to leave."""
        return self._shown

    def present(self) -> None:
        if self._shown:
            return
        self._shown, self._used, self._pressed = True, False, False
        self.winId()  # a real window, so it stacks above libVLC's video
        self.show()
        self.raise_()
        self._animate(1.0, _APPEAR_MS, QEasingCurve.Type.OutCubic)

    def dismiss(self) -> None:
        if not self._shown:
            return
        self._shown = False
        self._animate(0.0, _LEAVE_MS, QEasingCurve.Type.InCubic)

    def reset(self) -> None:
        """Gone at once, without animation (a new video, or the player closing)."""
        self._animation.stop()
        self._shown = self._used = self._pressed = False
        self._progress = 0.0
        self.hide()

    def activate(self) -> None:
        """Press it: once per showing."""
        if self._used or not self._shown:
            return
        self._used = True
        self.activated.emit()
        self.dismiss()

    def _animate(self, target: float, milliseconds: int, curve: QEasingCurve.Type) -> None:
        reduced = getattr(self._theme, "reduced_motion", lambda: False)()
        self._animation.stop()
        if reduced:
            self._set_progress(target)
            self._animation_finished()
            return
        self._animation.setStartValue(self._progress)
        self._animation.setEndValue(target)
        self._animation.setDuration(max(1, int(milliseconds * abs(target - self._progress))))
        self._animation.setEasingCurve(curve)
        self._animation.start()

    def _animation_finished(self) -> None:
        if not self._shown and self._progress <= 0.001:
            self.hide()

    def _set_progress(self, value) -> None:
        self._progress = float(value)
        self._apply_mask()
        self.update()

    # ----------------------------------------------------------- geometry

    def set_scale(self, scale: float) -> None:
        """Scale with the window so it stays readable in fullscreen and compact in a small window."""
        if abs(scale - self._scale) > 0.01:
            self._scale = scale
            self._apply_metrics()

    def _font(self) -> QFont:
        font = QFont(QApplication.font())
        font.setPixelSize(max(11, round(13.5 * self._scale)))
        font.setWeight(QFont.Weight.DemiBold)
        font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 101)
        return font

    def _apply_metrics(self) -> None:
        s = self._scale
        self._height = round(_BASE_HEIGHT * s)
        self._icon = round(14 * s)
        self._gap = round(9 * s)
        self._pad_left, self._pad_right = round(15 * s), round(19 * s)
        text_width = QFontMetricsF(self._font()).horizontalAdvance("Skip Intro")
        self._width = round(self._pad_left + self._icon + self._gap + text_width + self._pad_right)
        self._slide = round(_SLIDE * s)
        self.setFixedSize(self._width, self._height + self._slide)  # the extra rows are the slide's travel
        self._apply_mask()

    def pill_height(self) -> int:
        return self._height

    def _pill(self) -> QRectF:
        """The visible pill now: grows from the right edge, rising into place."""
        eased = self._progress
        width = self._height + (self._width - self._height) * eased
        top = self._slide * (1.0 - eased)
        return QRectF(self._width - width, top, width, self._height)

    def _apply_mask(self) -> None:
        if self._progress <= 0.001:
            self.setMask(QRegion(0, 0, 1, 1))
            return
        rect = self._pill()
        path = QPainterPath()
        path.addRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))

    # ------------------------------------------------------------ painting

    def paintEvent(self, event) -> None:
        if self._progress <= 0.001:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self._pill()
        fill = _FILL_PRESSED if self._pressed else _FILL_HOVER if self._hover else _FILL
        radius = rect.height() / 2
        painter.setPen(QPen(_EDGE, 1.0))
        painter.setBrush(fill)
        painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        if self.hasFocus() and self.focusPolicy() != Qt.FocusPolicy.NoFocus:
            ring = QColor(getattr(self._theme, "FOCUS_RING", "") or getattr(self._theme, "ACCENT", "#ffffff"))
            painter.setPen(QPen(ring, 2.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), radius - 1, radius - 1)
        alpha = max(0.0, min(1.0, (self._progress - 0.35) / 0.65))
        accent = QColor(getattr(self._theme, "ACCENT", "#ffffff"))
        icon_colour = QColor(accent if self._hover or self.hasFocus() else _TEXT)
        icon_colour.setAlphaF(alpha)
        text_colour = QColor(_TEXT)
        text_colour.setAlphaF(alpha)
        # The contents keep their place while the pill's edge sweeps across them (a reveal, not a squash).
        painter.setClipRect(rect)
        self._paint_chevrons(painter, QPointF(self._pad_left, rect.center().y()), icon_colour)
        painter.setFont(self._font())
        painter.setPen(text_colour)
        text_rect = QRectF(self._pad_left + self._icon + self._gap, rect.top(), self._width, rect.height())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "Skip Intro")

    def _paint_chevrons(self, painter: QPainter, origin: QPointF, colour: QColor) -> None:
        """Two thin forward chevrons: the universal 'skip ahead' mark, light enough not to compete with the label."""
        size = self._icon
        pen = QPen(colour, max(1.5, 1.7 * self._scale))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        half, step = size * 0.30, size * 0.46
        for i in range(2):
            x = origin.x() + i * step
            path = QPainterPath()
            path.moveTo(x, origin.y() - half)
            path.lineTo(x + half * 0.95, origin.y())
            path.lineTo(x, origin.y() + half)
            painter.drawPath(path)

    # --------------------------------------------------------------- input

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:
        self._hover = self._pressed = False
        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and not self._used:
            self._pressed = True
            self.update()
            event.accept()
        else:
            event.ignore()

    def mouseReleaseEvent(self, event) -> None:
        pressed, self._pressed = self._pressed, False
        self.update()
        if pressed and event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.activate()
            event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space) and not event.isAutoRepeat():
            self.activate()
            event.accept()
        else:
            event.ignore()  # everything else is the player's: seeking, volume, fullscreen...

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.update()
