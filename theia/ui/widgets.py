"""Reusable building blocks: cards, horizontal rows, the responsive grid."""

from __future__ import annotations

import zlib
from dataclasses import dataclass

from PyQt6.QtCore import (
    QEasingCurve,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QVariantAnimation,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QLinearGradient, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QScrollArea, QToolButton, QVBoxLayout, QWidget

from ..models import Media, Show, format_duration
from . import icons, theme
from .images import ImageLoader

CARD_WIDTH = 288
CARD_HEIGHT = 162  # 16:9
CARD_GAP = 6
_REST_SCALE = 0.955  # a card's picture grows from this to 1.0 on hover
_RADIUS = 6.0


@dataclass(slots=True)
class CardModel:
    """What a card shows and what it stands for."""

    payload: Media | Show
    title: str
    subtitle: str = ""
    image: str | None = None
    progress: float | None = None  # 0-1 draws the red bar
    watched: bool = False
    thumb_media_id: int | None = None  # repaint when this media's frame grab arrives


def movie_card(media: Media) -> CardModel:
    details = [str(media.year)] if media.year else []
    if media.in_progress:
        details.append(f"{format_duration(media.remaining)} left")
    elif media.total_duration:
        details.append(format_duration(media.total_duration))
    return CardModel(
        media, media.title, " · ".join(details), media.image,
        media.fraction if media.in_progress else None, media.is_finished, media.id,
    )


def show_card(show: Show) -> CardModel:
    return CardModel(show, show.title, show.summary, show.image, thumb_media_id=show.first_media_id)


def continue_card(media: Media) -> CardModel:
    """Card for the *Continue Watching* row: always shows progress and time left."""
    left = f"{format_duration(media.remaining)} left"
    subtitle = f"{media.episode_code} · {left}" if media.is_episode else left
    return CardModel(media, media.heading, subtitle, media.image, media.fraction, False, media.id)


def episode_card(media: Media) -> CardModel:
    """Card for an episode found by search: the episode title leads."""
    subtitle = " · ".join(p for p in (media.show_title, media.episode_code) if p)
    return CardModel(
        media, media.title, subtitle, media.image,
        media.fraction if media.in_progress else None, media.is_finished, media.id,
    )


def discard(widget: QWidget) -> None:
    """Remove a widget from the screen now and free it when Qt gets round to it."""
    widget.hide()
    widget.setParent(None)
    widget.deleteLater()


class SmoothScrollArea(QScrollArea):
    """A vertical scroll area whose mouse wheel glides instead of jumping."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._target = 0
        self._animation = QPropertyAnimation(self.verticalScrollBar(), b"value", self)
        self._animation.setDuration(260)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        if not event.pixelDelta().isNull() or delta == 0:
            super().wheelEvent(event)  # touchpads already scroll smoothly
            return
        bar = self.verticalScrollBar()
        running = self._animation.state() == QPropertyAnimation.State.Running
        start = self._target if running else bar.value()
        self._target = max(bar.minimum(), min(bar.maximum(), round(start - delta / 120 * 130)))
        self._animation.stop()
        self._animation.setStartValue(bar.value())
        self._animation.setEndValue(self._target)
        self._animation.start()
        event.accept()


class MediaCard(QWidget):
    """A 16:9 picture card with title, subtitle and optional progress bar."""

    activated = pyqtSignal(object)  # payload
    menuRequested = pyqtSignal(object, QPoint)  # payload, global position

    def __init__(self, model: CardModel, images: ImageLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.model = model
        self._images = images
        self._hover = 0.0
        self._pressed = False
        self.setFixedSize(CARD_WIDTH, CARD_HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setAccessibleName(model.title)
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(140)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.valueChanged.connect(self._set_hover)

    # ------------------------------------------------------------- updates

    def set_image(self, path: str | None) -> None:
        if path and path != self.model.image:
            self.model.image = path
            self.update()

    def on_thumbnail(self, media_id: int, path: str) -> None:
        """Slot for the thumbnail service: adopt a freshly grabbed frame."""
        if path and media_id == self.model.thumb_media_id and not self.model.image:
            self.set_image(path)

    def set_card_size(self, width: int, height: int) -> None:
        if self.width() != width or self.height() != height:
            self.setFixedSize(width, height)

    # -------------------------------------------------------------- events

    def _set_hover(self, value) -> None:
        self._hover = float(value)
        self.update()

    def _animate_to(self, end: float) -> None:
        self._animation.stop()
        self._animation.setStartValue(self._hover)
        self._animation.setEndValue(end)
        self._animation.start()

    def enterEvent(self, event) -> None:
        self._animate_to(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._pressed = False
        self._animate_to(0.0)
        super().leaveEvent(event)

    def focusInEvent(self, event) -> None:
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:
        self.update()
        super().focusOutEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._pressed:
            self._pressed = False
            if self.rect().contains(event.position().toPoint()):
                self.activated.emit(self.model.payload)
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.activated.emit(self.model.payload)
        else:
            super().keyPressEvent(event)

    def contextMenuEvent(self, event) -> None:
        self.menuRequested.emit(self.model.payload, event.globalPos())

    # ------------------------------------------------------------ painting

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
            | QPainter.RenderHint.TextAntialiasing
        )
        t = self._hover
        scale = _REST_SCALE + (1.0 - _REST_SCALE) * t
        width, height = self.width() * scale, self.height() * scale
        rect = QRectF((self.width() - width) / 2, (self.height() - height) / 2, width, height)
        outline = QPainterPath()
        outline.addRoundedRect(rect, _RADIUS, _RADIUS)
        painter.setClipPath(outline)

        pixmap = self._images.get(self.model.image, self.size(), self.devicePixelRatioF(), self)
        if pixmap is not None:
            painter.drawPixmap(rect.toAlignedRect(), pixmap)
        else:
            paint_placeholder(painter, rect, self.model.title)

        scrim = QLinearGradient(0, rect.top() + rect.height() * 0.36, 0, rect.bottom())
        scrim.setColorAt(0.0, QColor(0, 0, 0, 0))
        scrim.setColorAt(0.55, QColor(0, 0, 0, 150))
        scrim.setColorAt(1.0, QColor(0, 0, 0, 235))
        painter.fillRect(rect, scrim)

        pad = 12.0
        has_bar = self.model.progress is not None
        text_width = rect.width() - 2 * pad
        baseline = rect.bottom() - pad - (8 if has_bar else 0)
        if self.model.subtitle:
            font = theme.ui_font(12)
            painter.setFont(font)
            painter.setPen(QColor(theme.TEXT_DIM))
            metrics = QFontMetricsF(font)
            text = metrics.elidedText(self.model.subtitle, Qt.TextElideMode.ElideRight, text_width)
            painter.drawText(QPointF(rect.left() + pad, baseline - metrics.descent()), text)
            baseline -= metrics.height() + 1
        font = theme.ui_font(15, QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor(theme.TEXT))
        metrics = QFontMetricsF(font)
        text = metrics.elidedText(self.model.title, Qt.TextElideMode.ElideRight, text_width)
        painter.drawText(QPointF(rect.left() + pad, baseline - metrics.descent()), text)

        if has_bar:
            track = QRectF(rect.left() + pad, rect.bottom() - pad + 1, text_width, 3.5)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, 85))
            painter.drawRoundedRect(track, 1.75, 1.75)
            fill = QRectF(track)
            fill.setWidth(max(3.5, track.width() * min(1.0, max(0.0, self.model.progress))))
            painter.setBrush(QColor(theme.ACCENT))
            painter.drawRoundedRect(fill, 1.75, 1.75)

        if self.model.watched:
            badge = QRectF(rect.right() - 30, rect.top() + 8, 22, 22)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 170))
            painter.drawEllipse(badge)
            icons.paint(painter, "check", badge.adjusted(4, 4, -4, -4), theme.TEXT_SOFT)

        if t > 0.01:
            painter.setOpacity(t)
            centre = QPointF(rect.center().x(), rect.top() + rect.height() * 0.42)
            radius = 23.0
            painter.setPen(QPen(QColor(255, 255, 255), 2))
            painter.setBrush(QColor(0, 0, 0, 150))
            painter.drawEllipse(centre, radius, radius)
            glyph = QRectF(centre.x() - 12, centre.y() - 12, 24, 24)
            icons.paint(painter, "play" if isinstance(self.model.payload, Media) else "chevron-right", glyph)
            painter.setOpacity(1.0)

        painter.setClipping(False)
        border = max(t, 1.0 if self.hasFocus() else 0.0)
        if border > 0.01:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(255, 255, 255, round(235 * border)), 2))
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), _RADIUS, _RADIUS)
        painter.end()


def paint_placeholder(painter: QPainter, rect: QRectF, title: str) -> None:
    """Stand-in artwork: a dark gradient tinted by the title, with a film glyph."""
    hue = zlib.crc32(title.encode("utf-8", "replace")) % 360
    gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
    gradient.setColorAt(0.0, QColor.fromHsv(hue, 110, 84))
    gradient.setColorAt(1.0, QColor.fromHsv((hue + 40) % 360, 130, 30))
    painter.fillRect(rect, gradient)
    side = min(rect.width(), rect.height()) * 0.3
    glyph = QRectF(rect.center().x() - side / 2, rect.top() + rect.height() * 0.42 - side / 2, side, side)
    painter.setOpacity(0.28)
    icons.paint(painter, "film", glyph)
    painter.setOpacity(1.0)


class _RowScroller(QScrollArea):
    """Horizontal strip of cards; vertical wheel motion is left to the page."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWidgetResizable(False)
        self.setFixedHeight(CARD_HEIGHT)
        self._animation = QPropertyAnimation(self.horizontalScrollBar(), b"value", self)
        self._animation.setDuration(420)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    def glide(self, pixels: int) -> None:
        bar = self.horizontalScrollBar()
        running = self._animation.state() == QPropertyAnimation.State.Running
        start = self._animation.endValue() if running else bar.value()
        target = max(bar.minimum(), min(bar.maximum(), int(start) + pixels))
        self._animation.stop()
        self._animation.setStartValue(bar.value())
        self._animation.setEndValue(target)
        self._animation.start()

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta()
        sideways = abs(delta.x()) > abs(delta.y())
        if sideways or event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            amount = delta.x() if sideways else delta.y()
            bar = self.horizontalScrollBar()
            if not event.pixelDelta().isNull() and sideways:
                bar.setValue(bar.value() - event.pixelDelta().x())
            else:
                self.glide(round(-amount / 120 * (CARD_WIDTH + CARD_GAP)))
            event.accept()
        else:
            event.ignore()  # let the page scroll vertically


class CardRow(QWidget):
    """A titled, horizontally scrolling row of cards."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._title = QLabel(title)
        self._title.setObjectName("rowTitle")
        self._title.setContentsMargins(theme.SIDE_MARGIN, 0, 0, 0)

        self._scroller = _RowScroller(self)
        self._strip = QWidget()
        self._strip_layout = QHBoxLayout(self._strip)
        self._strip_layout.setContentsMargins(theme.SIDE_MARGIN - 6, 0, theme.SIDE_MARGIN - 6, 0)
        self._strip_layout.setSpacing(CARD_GAP)
        self._strip_layout.setSizeConstraint(QHBoxLayout.SizeConstraint.SetFixedSize)
        self._scroller.setWidget(self._strip)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(self._title)
        layout.addWidget(self._scroller)

        self._left = self._arrow("chevron-left", -1)
        self._right = self._arrow("chevron-right", +1)
        bar = self._scroller.horizontalScrollBar()
        bar.valueChanged.connect(self._update_arrows)
        bar.rangeChanged.connect(self._update_arrows)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self._cards: list[MediaCard] = []

    def _arrow(self, icon_name: str, direction: int) -> QToolButton:
        button = QToolButton(self)
        button.setIcon(icons.icon(icon_name))
        button.setIconSize(QSize(30, 30))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setStyleSheet(
            "QToolButton { background: rgba(20,20,20,0.72); border-radius: 0; }"
            "QToolButton:hover { background: rgba(20,20,20,0.92); }"
        )
        button.clicked.connect(lambda: self._scroller.glide(direction * self._page()))
        button.hide()
        return button

    def _page(self) -> int:
        """Scroll distance for one arrow click: whole cards, most of the viewport."""
        step = CARD_WIDTH + CARD_GAP
        return max(step, (self._scroller.viewport().width() - theme.SIDE_MARGIN) // step * step)

    def set_cards(self, cards: list[MediaCard]) -> None:
        for card in self._cards:
            self._strip_layout.removeWidget(card)
            discard(card)
        self._cards = cards
        for card in cards:
            card.set_card_size(CARD_WIDTH, CARD_HEIGHT)
            self._strip_layout.addWidget(card)
        self._strip.adjustSize()
        self.setVisible(bool(cards))
        self._update_arrows()

    def cards(self) -> list[MediaCard]:
        return self._cards

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        top = self._scroller.geometry().top()
        inset = round(CARD_HEIGHT * (1 - _REST_SCALE) / 2)
        height = CARD_HEIGHT - 2 * inset
        self._left.setGeometry(0, top + inset, theme.SIDE_MARGIN - 8, height)
        self._right.setGeometry(self.width() - theme.SIDE_MARGIN + 8, top + inset, theme.SIDE_MARGIN - 8, height)
        self._update_arrows()

    def enterEvent(self, event) -> None:
        super().enterEvent(event)
        self._update_arrows()

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self._update_arrows()

    def _update_arrows(self, *_args) -> None:
        bar = self._scroller.horizontalScrollBar()
        hovered = self.underMouse()
        self._left.setVisible(hovered and bar.value() > bar.minimum())
        self._right.setVisible(hovered and bar.value() < bar.maximum())
        self._left.raise_()
        self._right.raise_()


class CardGrid(QWidget):
    """Cards laid out in as many equal columns as the width allows."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cards: list[MediaCard] = []
        self._gap = 8
        self._row_gap = 12
        self._laid_out_for = (-1, -1)

    def set_cards(self, cards: list[MediaCard]) -> None:
        for card in self._cards:
            discard(card)
        self._cards = cards
        for card in cards:
            card.setParent(self)
            card.show()
        self._laid_out_for = (-1, -1)
        self._relayout()

    def cards(self) -> list[MediaCard]:
        return self._cards

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._relayout()

    def _relayout(self) -> None:
        width = self.width()
        if (width, len(self._cards)) == self._laid_out_for:
            return
        self._laid_out_for = (width, len(self._cards))
        if not self._cards or width <= 0:
            self.setFixedHeight(0)
            return
        columns = max(2, round((width + self._gap) / (CARD_WIDTH + self._gap)))
        card_width = (width - (columns - 1) * self._gap) // columns
        card_height = round(card_width * 9 / 16)
        for index, card in enumerate(self._cards):
            row, column = divmod(index, columns)
            card.set_card_size(card_width, card_height)
            card.move(column * (card_width + self._gap), row * (card_height + self._row_gap))
        rows = (len(self._cards) + columns - 1) // columns
        self.setFixedHeight(rows * card_height + (rows - 1) * self._row_gap)
