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
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QRadialGradient,
)
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QToolButton, QVBoxLayout, QWidget

from ..models import Media, Show, format_duration, plural
from . import icons, theme
from .images import ImageLoader

CARD_INSET = 12  # room around a card's picture for its soft shadow and hover lift
CARD_WIDTH = 272  # a landscape card: whole widget, shadow room included
POSTER_WIDTH = 194  # a poster card: 170px picture + shadow room
CARD_GAP = 0
CAPTION_HEIGHT = 46  # title + subtitle under the picture
RADIUS = 14.0
LIFT = 4.0  # how far the picture rises on hover


def picture_height(width: int, poster: bool = False) -> int:
    """Height of the picture for a card *width* wide: 2:3 for posters, 16:9 otherwise."""
    return round((width - 2 * CARD_INSET) * (3 / 2 if poster else 9 / 16))


def card_height(width: int, poster: bool = False) -> int:
    """Widget height for a card *width* wide: the picture plus the caption."""
    return CARD_INSET + picture_height(width, poster) + CAPTION_HEIGHT


def card_width(poster: bool = False) -> int:
    return POSTER_WIDTH if poster else CARD_WIDTH


CARD_HEIGHT = card_height(CARD_WIDTH)


@dataclass(slots=True)
class CardModel:
    """What a card shows and what it stands for."""

    payload: Media | Show
    title: str
    subtitle: str = ""
    image: str | None = None
    progress: float | None = None  # 0-1 draws the progress bar
    watched: bool = False
    thumb_media_id: int | None = None  # repaint when this media's frame grab arrives
    badge: str = ""  # short translucent tag on the picture ("S1:E4", "2h 50m")
    poster: bool = False  # a tall 2:3 poster card instead of a landscape 16:9 one


def movie_card(media: Media, poster: bool = False) -> CardModel:
    details = [str(media.year)] if media.year else []
    if media.in_progress:
        details.append(f"{format_duration(media.remaining)} left")
    elif media.total_duration:
        details.append(format_duration(media.total_duration))
    return CardModel(
        media, media.title, " · ".join(details), media.image,
        media.fraction if media.in_progress else None, media.is_finished, media.id,
        format_duration(media.total_duration), poster,
    )


def show_card(show: Show, poster: bool = False) -> CardModel:
    badge = plural(show.season_count, "Season") if show.season_count else ""
    return CardModel(
        show, show.title, show.summary, show.image, thumb_media_id=show.first_media_id, badge=badge, poster=poster
    )


def continue_card(media: Media) -> CardModel:
    """Card for the *Continue Watching* row: always shows progress and time left."""
    left = f"{format_duration(media.remaining)} left"
    subtitle = f"{media.episode_code} · {left}" if media.is_episode else left
    return CardModel(media, media.heading, subtitle, media.image, media.fraction, False, media.id, media.episode_code)


def episode_card(media: Media) -> CardModel:
    """Card for an episode found by search: the episode title leads."""
    subtitle = " · ".join(p for p in (media.show_title, media.episode_code) if p)
    return CardModel(
        media, media.title, subtitle, media.image,
        media.fraction if media.in_progress else None, media.is_finished, media.id, media.episode_code,
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
    """A rounded 16:9 picture with a translucent tag and progress bar, its title and subtitle below."""

    activated = pyqtSignal(object)  # payload
    menuRequested = pyqtSignal(object, QPoint)  # payload, global position

    def __init__(self, model: CardModel, images: ImageLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.model = model
        self._images = images
        self._hover = 0.0
        self._pressed = False
        self._tint: QColor | None = None  # the artwork's dominant colour, for the hover underglow
        self._tint_of: str | None = None
        self.setFixedSize(card_width(model.poster), card_height(card_width(model.poster), model.poster))
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

    def _picture_rect(self) -> QRectF:
        """Where the picture sits now: inset for the shadow, risen a little when hovered."""
        width = self.width() - 2 * CARD_INSET
        height = picture_height(self.width(), self.model.poster)
        return QRectF(CARD_INSET, CARD_INSET - LIFT * self._hover, width, height)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
            | QPainter.RenderHint.TextAntialiasing
        )
        t = self._hover
        rect = self._picture_rect()
        paint_shadow(painter, rect, RADIUS, 0.55 + 0.45 * t, glow=t)

        outline = QPainterPath()
        outline.addRoundedRect(rect, RADIUS, RADIUS)
        painter.save()
        painter.setClipPath(outline)
        pixmap = self._images.get(self.model.image, rect.size().toSize(), self.devicePixelRatioF(), self)
        if pixmap is not None:
            painter.drawPixmap(rect.toAlignedRect(), pixmap)
            if self._tint_of != self.model.image:
                self._tint_of = self.model.image
                self._tint = vivid(
                    pixmap.toImage().scaled(
                        1, 1, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
                    ).pixelColor(0, 0)
                )
        else:
            paint_placeholder(painter, rect, self.model.title)
            self._tint = vivid(QColor.fromHsv(zlib.crc32(self.model.title.encode("utf-8", "replace")) % 360, 160, 140))
        painter.fillRect(rect, QColor(255, 255, 255, 14))  # the glass surface wash

        scrim = QLinearGradient(0, rect.top() + rect.height() * (0.7 if self.model.poster else 0.55), 0, rect.bottom())
        scrim.setColorAt(0.0, QColor(0, 0, 0, 0))
        scrim.setColorAt(1.0, QColor(0, 0, 0, 120 if self.model.progress is not None else 70))
        painter.fillRect(rect, scrim)

        if t > 0.01:
            painter.setOpacity(t)
            centre = rect.center()
            painter.setPen(QPen(QColor(255, 255, 255, 70), 1))
            painter.setBrush(QColor(255, 255, 255, 52))
            painter.drawEllipse(centre, 22.0, 22.0)
            glyph = QRectF(centre.x() - 10, centre.y() - 10, 20, 20)
            icons.paint(painter, "play" if isinstance(self.model.payload, Media) else "chevron-right", glyph)
            painter.setOpacity(1.0)
        # Inner specular hairline along the top edge: 1px rgba(255,255,255,.15), fading out at the corners.
        spec = QLinearGradient(rect.left(), 0, rect.right(), 0)
        spec.setColorAt(0.0, QColor(255, 255, 255, 0))
        spec.setColorAt(0.2, QColor(255, 255, 255, 38))
        spec.setColorAt(0.8, QColor(255, 255, 255, 38))
        spec.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.fillRect(QRectF(rect.left(), rect.top(), rect.width(), 1.0), spec)
        painter.restore()

        if self.model.progress is not None:
            track = QRectF(rect.left() + 12, rect.bottom() - 11, rect.width() - 24, 3.5)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, 80))
            painter.drawRoundedRect(track, 1.75, 1.75)
            fill = QRectF(track)
            fill.setWidth(max(3.5, track.width() * min(1.0, max(0.0, self.model.progress))))
            painter.setBrush(QColor(theme.ACCENT))  # the playhead
            painter.drawRoundedRect(fill, 1.75, 1.75)

        if self.model.badge:
            paint_pill(painter, QPointF(rect.left() + 9, rect.top() + 9), self.model.badge)
        if self.model.watched:
            badge = QRectF(rect.right() - 29, rect.top() + 9, 20, 20)
            painter.setPen(QPen(QColor(255, 255, 255, 36), 1))
            painter.setBrush(QColor(0, 0, 0, 166))
            painter.drawEllipse(badge)
            icons.paint(painter, "check", badge.adjusted(4.5, 4.5, -4.5, -4.5), theme.TEXT_SOFT)

        # Physical glass rim: specular light on the top and left edges, dark refraction on the bottom and right.
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if self.hasFocus():
            painter.setPen(QPen(QColor(theme.ACCENT), 2))
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), RADIUS, RADIUS)
        else:
            edge = rect.adjusted(0.5, 0.5, -0.5, -0.5)
            lit = QPainterPath()
            lit.addPolygon(QPolygonF([rect.topLeft(), rect.topRight(), rect.bottomLeft()]))
            painter.save()
            painter.setClipPath(lit)
            painter.setPen(QPen(QColor(255, 255, 255, round(70 + 70 * t)), 1))
            painter.drawRoundedRect(edge, RADIUS, RADIUS)
            painter.restore()
            shade = QPainterPath()
            shade.addPolygon(QPolygonF([rect.topRight(), rect.bottomRight(), rect.bottomLeft()]))
            painter.save()
            painter.setClipPath(shade)
            painter.setPen(QPen(QColor(0, 0, 0, 90), 1))
            painter.drawRoundedRect(edge, RADIUS, RADIUS)
            painter.restore()

        text_width = rect.width() - 4
        left = rect.left() + 2
        font = theme.ui_font(13, QFont.Weight.DemiBold)
        metrics = QFontMetricsF(font)
        baseline = CARD_INSET + rect.height() + 20
        painter.setFont(font)
        painter.setPen(QColor(theme.TEXT))
        painter.drawText(
            QPointF(left, baseline), metrics.elidedText(self.model.title, Qt.TextElideMode.ElideRight, text_width)
        )
        if self.model.subtitle:
            font = theme.ui_font(12)
            metrics = QFontMetricsF(font)
            painter.setFont(font)
            painter.setPen(QColor(theme.TEXT_MUTED))
            painter.drawText(
                QPointF(left, baseline + 17),
                metrics.elidedText(self.model.subtitle, Qt.TextElideMode.ElideRight, text_width),
            )
        painter.end()


def paint_shadow(
    painter: QPainter, rect: QRectF, radius: float, strength: float = 1.0, glow: float = 0.0,
    feather: float = 1.1, offset: float = 4.0, layers: int = 7, glow_colour: QColor | None = None,
) -> None:
    """A soft, feathered drop shadow under *rect*, plus an optional faint white/blue glow."""
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    for i in range(layers, 0, -1):
        spread = i * feather
        painter.setBrush(QColor(0, 0, 0, round(strength * 20 * (1 - i / (layers + 1)) + 3)))
        painter.drawRoundedRect(
            rect.adjusted(-spread, -spread + offset, spread, spread + offset), radius + spread, radius + spread
        )
    if glow > 0.01:  # a coloured underglow, strongest just below the card
        colour = glow_colour or QColor(255, 255, 255)
        for i in range(10, 0, -1):
            tone = QColor(colour.red(), colour.green(), colour.blue(), round(glow * 34 * (1 - i / 11)))
            painter.setBrush(tone)
            painter.drawRoundedRect(rect.adjusted(-i, -i + 7, i, i + 7), radius + i, radius + i)
    painter.restore()


def paint_pill(painter: QPainter, origin: QPointF, text: str, size: int = 11) -> QRectF:
    """The one badge style: rgba(0,0,0,.65) fill, 1px rgba(255,255,255,.14) rim, soft theme-text medium weight."""
    font = theme.ui_font(size, QFont.Weight.Medium)
    metrics = QFontMetricsF(font)
    rect = QRectF(origin.x(), origin.y(), metrics.horizontalAdvance(text) + 18, metrics.height() + 6)
    radius = rect.height() / 2
    painter.save()
    painter.setPen(QPen(QColor(255, 255, 255, 36), 1))
    painter.setBrush(QColor(0, 0, 0, 166))
    painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
    painter.setFont(font)
    painter.setPen(QColor(theme.TEXT_SOFT))
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
    painter.restore()
    return rect


class PlayButton(QPushButton):
    """The primary pill: accent fill, contrasting label, fully rounded, eased hover and press."""

    def __init__(self, text: str = "", parent: QWidget | None = None, height: int = 40, aura: int = 0) -> None:
        super().__init__(text, parent)
        self._height = height
        self._aura = aura  # transparent margin the hover glow is painted into
        self.setObjectName("playButton")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.apply_theme()
        self.setIconSize(QSize(15, 15))
        self.setFixedHeight(height + 2 * aura)
        self._hover = 0.0
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(160)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.valueChanged.connect(self._set_hover)

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
        self._animate_to(0.0)
        super().leaveEvent(event)

    def apply_theme(self) -> None:
        self.setIcon(icons.icon("play", theme.ON_ACCENT, theme.ICON_DISABLED))
        self.update()

    def _font(self) -> QFont:
        return theme.ui_font(15 if self._height >= 46 else 14, QFont.Weight.DemiBold)

    def sizeHint(self) -> QSize:
        text = QFontMetricsF(self._font()).horizontalAdvance(self.text())
        icon = self.iconSize().width() + 8 if not self.icon().isNull() else 0
        return QSize(round(text + icon + (56 if self._height >= 46 else 48)) + 2 * self._aura, self._height + 2 * self._aura)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        rect = QRectF(self.rect()).adjusted(self._aura, self._aura, -self._aura, -self._aura)
        enabled = self.isEnabled()
        if self._aura and self._hover > 0.01 and enabled:  # a soft white aura that swells on hover
            painter.setPen(Qt.PenStyle.NoPen)
            for i in range(self._aura, 0, -1):
                glow = QColor(theme.ACCENT)
                glow.setAlpha(round(self._hover * 60 * (1 - i / (self._aura + 1))))
                painter.setBrush(glow)
                painter.drawRoundedRect(rect.adjusted(-i, -i, i, i), rect.height() / 2 + i, rect.height() / 2 + i)
        fill = QColor(theme.ACCENT_PRESSED if self.isDown() else theme.mix(theme.ACCENT, theme.ACCENT_HOVER, self._hover))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill if enabled else QColor(theme.BORDER))
        painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)

        font = self._font()
        painter.setFont(font)
        text_width = QFontMetricsF(font).horizontalAdvance(self.text())
        side = float(self.iconSize().width()) if not self.icon().isNull() else 0.0
        gap = 8.0 if side else 0.0
        x = rect.left() + (rect.width() - (side + gap + text_width)) / 2
        if side:
            mode = QIcon.Mode.Normal if enabled else QIcon.Mode.Disabled
            self.icon().paint(painter, QRectF(x, rect.top() + (rect.height() - side) / 2, side, side).toRect(), mode=mode)
        painter.setPen(QColor(theme.ON_ACCENT) if enabled else QColor(theme.TEXT_MUTED))
        painter.drawText(
            QRectF(x + side + gap, rect.top(), text_width + 2, rect.height()), Qt.AlignmentFlag.AlignVCenter, self.text()
        )
        painter.end()


def vivid(colour: QColor, value: float = 0.85, floor: float = 0.55) -> QColor:
    """A saturated, bright version of a sampled colour (greys fall back to violet)."""
    hue = colour.hsvHueF()
    if hue < 0:
        hue = 0.74
    return QColor.fromHsvF(hue, min(1.0, max(colour.hsvSaturationF() * 1.2, floor)), value)


class BrandLabel(QWidget):
    """The THEIA wordmark: light, widely tracked, with a soft metallic gradient."""

    def __init__(self, text: str = "THEIA", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._text = text
        self.setFixedHeight(34)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        font = theme.ui_font(19, QFont.Weight.DemiBold)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 6)
        painter.setFont(font)
        sheen = QLinearGradient(0, 0, 0, self.height())
        sheen.setColorAt(0.0, QColor(theme.TEXT))
        sheen.setColorAt(1.0, QColor(theme.TEXT_DIM))
        painter.setPen(QPen(QBrush(sheen), 1))
        painter.drawText(self.rect().adjusted(8, 0, 0, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self._text)
        painter.end()


def paint_placeholder(painter: QPainter, rect: QRectF, title: str) -> None:
    """Stand-in artwork: a gradient of the theme's surface tones with a film glyph."""
    gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
    gradient.setColorAt(0.0, QColor(theme.SURFACE_HOVER))
    gradient.setColorAt(1.0, QColor(theme.SURFACE))
    painter.fillRect(rect, gradient)
    side = min(rect.width(), rect.height()) * 0.3
    glyph = QRectF(rect.center().x() - side / 2, rect.top() + rect.height() * 0.42 - side / 2, side, side)
    painter.setOpacity(0.28)
    icons.paint(painter, "film", glyph)
    painter.setOpacity(1.0)


class _RowScroller(QScrollArea):
    """Horizontal strip of cards; vertical wheel motion is left to the page."""

    def __init__(self, poster: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._step = card_width(poster) + CARD_GAP
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWidgetResizable(False)
        self.setFixedHeight(card_height(card_width(poster), poster))
        self.viewport().setAutoFillBackground(False)
        self.viewport().setStyleSheet("background: transparent;")
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
                self.glide(round(-amount / 120 * self._step))
            event.accept()
        else:
            event.ignore()  # let the page scroll vertically


class CardRow(QWidget):
    """A titled, horizontally scrolling row of cards."""

    def __init__(self, title: str, poster: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._poster = poster
        self._title = QLabel(title)
        self._title.setObjectName("rowTitle")
        self._title.setContentsMargins(theme.SIDE_MARGIN, 0, 0, 0)

        self._scroller = _RowScroller(poster, self)
        self._strip = QWidget()
        self._strip_layout = QHBoxLayout(self._strip)
        self._strip_layout.setContentsMargins(theme.SIDE_MARGIN - CARD_INSET, 0, theme.SIDE_MARGIN - CARD_INSET, 0)
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
        button.setIconSize(QSize(18, 18))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setObjectName("rowArrow")
        button.setProperty("iconName", icon_name)
        button.clicked.connect(lambda: self._scroller.glide(direction * self._page()))
        button.hide()
        return button

    def apply_theme(self) -> None:
        for button in (self._left, self._right):
            button.setIcon(icons.icon(button.property("iconName")))

    def _page(self) -> int:
        """Scroll distance for one arrow click: whole cards, most of the viewport."""
        step = card_width(self._poster) + CARD_GAP
        return max(step, (self._scroller.viewport().width() - theme.SIDE_MARGIN) // step * step)

    def set_cards(self, cards: list[MediaCard]) -> None:
        for card in self._cards:
            self._strip_layout.removeWidget(card)
            discard(card)
        self._cards = cards
        for card in cards:
            card.set_card_size(card_width(self._poster), card_height(card_width(self._poster), self._poster))
            self._strip_layout.addWidget(card)
        self._strip.adjustSize()
        self.setVisible(bool(cards))
        self._update_arrows()

    def cards(self) -> list[MediaCard]:
        return self._cards

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        top = self._scroller.geometry().top()
        height = picture_height(card_width(self._poster), self._poster)  # the picture, not the caption
        width = theme.SIDE_MARGIN - 6
        self._left.setGeometry(4, top + CARD_INSET, width, height)
        self._right.setGeometry(self.width() - width - 4, top + CARD_INSET, width, height)
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

    def __init__(self, poster: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._poster = poster
        self._cards: list[MediaCard] = []
        self._gap = CARD_GAP
        self._row_gap = 6
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
        columns = max(2, round((width + self._gap) / (card_width(self._poster) + self._gap)))
        each = (width - (columns - 1) * self._gap) // columns
        height = card_height(each, self._poster)
        for index, card in enumerate(self._cards):
            row, column = divmod(index, columns)
            card.set_card_size(each, height)
            card.move(column * (each + self._gap), row * (height + self._row_gap))
        rows = (len(self._cards) + columns - 1) // columns
        self.setFixedHeight(rows * height + (rows - 1) * self._row_gap)
