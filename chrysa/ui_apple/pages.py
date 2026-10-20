"""Browsing pages: Library home (hero + rows), poster grids, search results and the show detail view."""

from __future__ import annotations

import math
import os
import zlib
from collections.abc import Callable
from dataclasses import dataclass

from PyQt6.QtCore import QEasingCurve, QPoint, QPointF, QRectF, QSize, Qt, QTimer, QVariantAnimation, pyqtSignal
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListView,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..models import Media, NextUp, Show, format_duration, plural
from . import icons, theme
from . import backdrop
from .images import ImageLoader
from .widgets import (
    CARD_INSET,
    CardGrid,
    CardModel,
    CardRow,
    PlayButton,
    SmoothScrollArea,
    continue_card,
    discard,
    episode_card,
    movie_card,
    paint_pill,
    paint_placeholder,
    paint_shadow,
    show_card,
    vivid,
)


class AmbientPage(QWidget):
    """Transparent page content: the scroll area's viewport paints the base colour and themed scene.

    ``tint`` is accepted for the pages that still pass one; the canvas stays neutral.
    """

    def __init__(self, tint: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tint = QColor(tint) if tint else QColor(theme.SURFACE)

    def tint(self) -> QColor:
        return QColor(self._tint)

    def set_tint(self, colour: QColor, animate: bool = True) -> None:
        self._tint = QColor(colour)  # remembered only; it no longer colours the canvas


@dataclass(slots=True)
class HeroItem:
    """One slide of the featured carousel."""

    payload: Media | Show
    eyebrow: str
    title: str
    meta: str
    image: str | None
    label: str  # text of the white pill button
    next_up: NextUp | None = None  # what a show's button plays
    thumb_media_id: int | None = None  # repaint when this media's frame grab arrives
    tint: QColor | None = None  # average colour of the picture, sampled once it has loaded


def recently_added(library, shows: list[Show], loose: list[Media], limit: int = 20) -> list[Media | Show]:
    """Movies, other videos and shows, newest file first (a show counts by its newest episode)."""
    entries: list[tuple[float, Media | Show]] = [(m.mtime, m) for m in loose]
    for show in shows:
        entries.append((max((e.mtime for e in library.episodes(show.id)), default=0.0), show))
    entries.sort(key=lambda entry: entry[0], reverse=True)
    return [payload for _mtime, payload in entries[:limit]]


class HeroCarousel(QWidget):
    """Apple TV-style featured stage: a tall rounded picture, kicker, title and floating glass buttons."""

    HEIGHT = 508
    SHADOW = 46  # room under the card for its halo and drop shadow
    TOP = 26
    AURA = 14  # room around the Play pill for its hover glow
    LEGACY_RADIUS = 22.0
    INTERVAL_MS = 9000
    TEXT_INSET = 48  # generous negative space left of the typography

    tintChanged = pyqtSignal(QColor)  # the glow colour the page behind should take on

    def __init__(self, host, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._host = host
        self._items: list[HeroItem] = []
        self._index = 0
        self._previous = -1
        self._fade = 1.0
        self._dots: list[QRectF] = []
        self._button_top = 0
        self.setFixedHeight(self.HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self._play = PlayButton(parent=self, height=48, aura=self.AURA)
        self._play.clicked.connect(self._activate)
        self._info = QPushButton("Details", self)
        self._info.setObjectName("ghost")
        self._info.setCursor(Qt.CursorShape.PointingHandCursor)
        self._info.setFixedHeight(48)
        self._info.clicked.connect(self._more_info)

        self._animation = QVariantAnimation(self)
        self._animation.setDuration(theme.ms("slow"))
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.valueChanged.connect(self._set_fade)
        self._timer = QTimer(self)
        self._timer.setInterval(self.INTERVAL_MS)
        self._timer.timeout.connect(lambda: self.show_index((self._index + 1) % len(self._items)))
        host.thumbs.thumbnailReady.connect(self.on_thumbnail)

    # ------------------------------------------------------------- content

    @property
    def RADIUS(self) -> float:
        return float(theme.RADIUS_L) if theme.FLAT else self.LEGACY_RADIUS

    @property
    def items(self) -> list[HeroItem]:
        return self._items

    def set_items(self, items: list[HeroItem]) -> None:
        self._items = items
        self._index = min(self._index, max(0, len(items) - 1))
        self._previous, self._fade = -1, 1.0
        self.setVisible(bool(items))
        self._timer.setInterval(self.INTERVAL_MS)
        self._timer.start() if len(items) > 1 else self._timer.stop()
        self._sync_buttons()
        self._publish_tint()
        self.update()

    def show_index(self, index: int) -> None:
        if not self._items or index == self._index:
            return
        self._previous, self._index = self._index, index
        self._sync_buttons()
        self._publish_tint()
        self._timer.start()  # a manual choice restarts the countdown
        self._animation.stop()
        self._animation.start()

    def on_thumbnail(self, media_id: int, path: str) -> None:
        for item in self._items:
            if path and item.thumb_media_id == media_id and not item.image:
                item.image, item.tint = path, None
                self.update()

    def _set_fade(self, value) -> None:
        self._fade = float(value)
        if self._fade >= 1.0:
            self._previous = -1
        self.update()

    def _sync_buttons(self) -> None:
        if not self._items:
            return
        self._play.setText(self._items[self._index].label)
        self._play.adjustSize()
        self._info.adjustSize()
        self._layout_buttons()

    # ------------------------------------------------------------ ambience

    def _sample_tint(self, item: HeroItem) -> QColor | None:
        """Average colour of the item's picture, or None while it is still loading."""
        tiny = self._host.images.get(item.image, QSize(48, 27), 1.0, self)
        if tiny is None:
            return None
        return tiny.toImage().scaled(
            1, 1, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
        ).pixelColor(0, 0)

    def _publish_tint(self) -> None:
        """Tell the page behind which glow colour suits the slide on show."""
        if not self._items:
            self.tintChanged.emit(QColor(theme.SURFACE))
            return
        self.tintChanged.emit(self._item_tint(self._items[self._index]))

    def _item_tint(self, item: HeroItem) -> QColor:
        """The picture's average colour, or while it loads a stand-in hue taken from the title."""
        if item.tint is None:
            item.tint = self._sample_tint(item)
        if item.tint is not None:
            return item.tint
        return QColor.fromHsv(zlib.crc32(item.title.encode("utf-8", "replace")) % 360, 150, 120)

    def _card(self) -> QRectF:
        return QRectF(
            theme.SIDE_MARGIN, self.TOP, self.width() - 2 * theme.SIDE_MARGIN, self.HEIGHT - self.SHADOW - self.TOP
        )

    def _layout_buttons(self) -> None:
        card = self._card()
        self._button_top = round(card.bottom() - 48 - 48)  # the pills, without the aura margin
        pill_left = round(card.left() + self.TEXT_INSET)
        self._play.move(pill_left - self.AURA, self._button_top - self.AURA)
        self._info.move(pill_left + self._play.width() - 2 * self.AURA + 12, self._button_top)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._layout_buttons()

    # ------------------------------------------------------------- actions

    def _activate(self) -> None:
        if not self._items:
            return
        item = self._items[self._index]
        if isinstance(item.payload, Show):
            if item.next_up is not None:
                self._host.play(item.next_up.episode, item.next_up.action != "resume")
            else:
                self._host.open_show(item.payload)
        else:
            self._host.play(item.payload)

    def _more_info(self) -> None:
        """A show opens its page; a movie drops down its menu (resume, start over, mark watched…)."""
        if not self._items:
            return
        payload = self._items[self._index].payload
        if isinstance(payload, Show):
            self._host.open_show(payload)
        else:
            self._host.show_menu(payload, self._info.mapToGlobal(QPoint(0, self._info.height() + 6)))

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        point = event.position()
        for index, dot in enumerate(self._dots):
            if dot.adjusted(-6, -8, 6, 8).contains(point):
                self.show_index(index)
                return
        if self._card().contains(point):
            self._activate()

    # ------------------------------------------------------------ painting

    def _paint_cover(self, painter: QPainter, item: HeroItem, rect: QRectF) -> None:
        step = 240  # coarse width steps: resizing the window must not decode a new image per pixel
        request = QSize(math.ceil(rect.width() / step) * step, round(rect.height()))
        dpr = self.devicePixelRatioF()
        pixmap = self._host.images.get(item.image, request, dpr, self)
        if pixmap is not None:
            excess = (request.width() - rect.width()) * dpr
            source = QRectF(excess / 2, 0, rect.width() * dpr, rect.height() * dpr)
            painter.drawPixmap(rect, pixmap, source)
        else:
            paint_placeholder(painter, rect, item.title, dark=True)

    def paintEvent(self, _event) -> None:
        if not self._items:
            return
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
            | QPainter.RenderHint.TextAntialiasing
        )
        card = self._card()
        item = self._items[self._index]
        if theme.FLAT:
            paint_shadow(painter, card, self.RADIUS, 0.9 if theme.SCHEME == "dark" else 0.35, feather=1.4, offset=8.0, layers=7)
        else:
            paint_shadow(painter, card, self.RADIUS, 1.8, feather=1.6, offset=14.0, layers=9)

        clip = QPainterPath()
        clip.addRoundedRect(card, self.RADIUS, self.RADIUS)
        painter.save()
        painter.setClipPath(clip)
        if 0 <= self._previous < len(self._items):
            self._paint_cover(painter, self._items[self._previous], card)
        painter.setOpacity(self._fade)
        self._paint_cover(painter, self._items[self._index], card)
        painter.setOpacity(1.0)

        # Cinematic grade: #121318 dissolves from the left edge through an eased ramp, so the
        # type sits on solid shade while the picture opens up to the right.
        base = QColor(18, 18, 20) if theme.FLAT else QColor(theme.BG)  # white type needs a dark grade, even in Light
        ink = (base.red(), base.green(), base.blue())
        side = QLinearGradient(card.left(), 0, card.right(), 0)
        for stop, alpha in ((0.0, 255), (0.16, 248), (0.30, 214), (0.44, 150), (0.58, 84), (0.72, 34), (0.86, 6), (1.0, 0)):
            side.setColorAt(stop, QColor(*ink, alpha))
        painter.fillRect(card, side)
        floor = QLinearGradient(0, card.top() + card.height() * 0.45, 0, card.bottom())
        floor.setColorAt(0.0, QColor(*ink, 0))
        floor.setColorAt(0.65, QColor(*ink, 120))
        floor.setColorAt(1.0, QColor(*ink, 205))
        painter.fillRect(card, floor)
        crown = QLinearGradient(0, card.top(), 0, card.top() + card.height() * 0.28)  # darkens the top edge
        crown.setColorAt(0.0, QColor(*ink, 90))
        crown.setColorAt(1.0, QColor(*ink, 0))
        painter.fillRect(card, crown)
        light = QRadialGradient(QPointF(card.right() - card.width() * 0.12, card.top() + 10), card.width() * 0.45)
        light.setColorAt(0.0, QColor(255, 255, 255, 34))  # a soft key light from the upper right
        light.setColorAt(1.0, QColor(255, 255, 255, 0))
        if not theme.FLAT:
            painter.fillRect(card, light)
        painter.restore()

        # Ambient rim: 1px rgba(255,255,255,.12), a little brighter along the top edge.
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(255, 255, 255, 31), 1))
        painter.drawRoundedRect(card.adjusted(0.5, 0.5, -0.5, -0.5), self.RADIUS, self.RADIUS)

        left = card.left() + self.TEXT_INSET
        available = card.width() * 0.52
        baseline = float(self._button_top) - 30

        # Meta line, then the title above it, then the kicker above that.
        font = theme.ui_font(14)
        metrics = QFontMetricsF(font)
        painter.setFont(font)
        painter.setPen(QColor(255, 255, 255, 185))
        painter.drawText(QPointF(left, baseline), metrics.elidedText(item.meta, Qt.TextElideMode.ElideRight, available))
        baseline -= metrics.height() + 6

        font = theme.ui_font(38, QFont.Weight.Bold)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, -0.5)
        metrics = QFontMetricsF(font)
        while metrics.horizontalAdvance(item.title) > available and font.pixelSize() > 22:
            font.setPixelSize(font.pixelSize() - 2)
            metrics = QFontMetricsF(font)
        painter.setFont(font)
        title = metrics.elidedText(item.title, Qt.TextElideMode.ElideRight, available)
        origin = QPointF(left, baseline - metrics.descent())
        painter.setPen(QColor(0, 0, 0, 110))
        painter.drawText(origin + QPointF(0, 2), title)
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(origin, title)
        baseline -= metrics.height() + 4

        kicker = theme.ui_font(11, QFont.Weight.Bold)
        kicker.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.5)
        painter.setFont(kicker)
        spot = QPointF(left, baseline - QFontMetricsF(font).descent() + 2)
        painter.setPen(QColor(255, 255, 255, 170) if theme.FLAT else QColor(theme.ACCENT))
        painter.drawText(spot, item.eyebrow.upper())

        self._dots = []
        if len(self._items) > 1:
            x = card.right() - 36 - 14 * (len(self._items) - 1)
            painter.setPen(Qt.PenStyle.NoPen)
            for index in range(len(self._items)):
                dot = QRectF(x + 14 * index - 3, card.bottom() - 36, 6, 6)
                self._dots.append(dot)
                painter.setBrush(QColor(255, 255, 255, 235 if index == self._index else 70))
                painter.drawEllipse(dot)
        painter.end()
        if item.tint is None and item.image:  # the picture arrived since the last look: take its colour
            QTimer.singleShot(0, self._publish_tint)


class HomePage(SmoothScrollArea):
    """The Library: a featured carousel, then Continue Watching, TV Shows, Movies."""

    HERO_SLIDES = 5

    def __init__(self, host, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._host = host
        content = AmbientPage()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 18, 0, 56)
        layout.setSpacing(30)

        self.hero = HeroCarousel(host)
        self.hero.hide()
        self.hero.tintChanged.connect(content.set_tint)
        self.continue_row = CardRow("Continue Watching")
        self.shows_row = CardRow("TV Shows", poster=True)
        self.movies_row = CardRow("Movies", poster=True)
        self.others_row = CardRow("Other Videos")
        layout.addWidget(self.hero)
        for row in (self.continue_row, self.shows_row, self.movies_row, self.others_row):
            layout.addWidget(row)

        self._empty = _EmptyState(host)
        layout.addWidget(self._empty, 1)
        layout.addStretch(1)
        self.setWidget(content)
        backdrop.install(self)

    def reload(self) -> None:
        library, card = self._host.library, self._host.make_card
        shows, movies, others = library.shows(), library.movies(), library.others()
        resume = library.continue_watching()
        recent = recently_added(library, shows, movies + others)
        self.continue_row.set_cards([card(continue_card(m)) for m in resume])
        self.shows_row.set_cards([card(show_card(s, poster=True)) for s in shows])
        self.movies_row.set_cards([card(movie_card(m, poster=True)) for m in movies])
        self.others_row.set_cards([card(movie_card(m)) for m in others])
        self.hero.set_items(self._hero_items(resume, recent))
        self._empty.setVisible(not (shows or movies or others))
        self._empty.refresh()

    def _hero_items(self, resume: list[Media], recent: list[Media | Show]) -> list[HeroItem]:
        items: list[HeroItem] = []
        seen: set[tuple[str, int]] = set()
        for media in resume[:3]:
            seen.add(("media", media.id))
            if media.show_id is not None:
                seen.add(("show", media.show_id))
            left = "Watched" if media.is_finished else f"{format_duration(media.remaining)} left"
            meta = "  ·  ".join(
                p for p in ((media.episode_code, media.title, left) if media.is_episode else ("Movie", str(media.year or ""), left)) if p
            )
            items.append(HeroItem(media, "Continue Watching", media.heading, meta, media.image, "Resume" if media.in_progress else "Play", thumb_media_id=media.id))
        for payload in recent:
            if len(items) >= self.HERO_SLIDES:
                break
            kicker = "Top Pick" if not any(i.eyebrow == "Top Pick" for i in items) else "Recently Added"
            if isinstance(payload, Show):
                if ("show", payload.id) in seen:
                    continue
                upcoming = self._host.library.next_up(payload.id)
                image = payload.backdrop_path if ShowBanner.has_backdrop(payload) else payload.image
                meta = "  ·  ".join(p for p in ("TV Show", str(payload.year or ""), payload.summary) if p)
                items.append(HeroItem(
                    payload, kicker, payload.title, meta, image,
                    upcoming.label if upcoming else "Play", upcoming, payload.first_media_id,
                ))
            else:
                if ("media", payload.id) in seen:
                    continue
                meta = "  ·  ".join(p for p in ("Movie", str(payload.year or ""), format_duration(payload.total_duration)) if p)
                items.append(HeroItem(payload, kicker, payload.title, meta, payload.image, "Play", thumb_media_id=payload.id))
        return items

    def refresh_empty_state(self) -> None:
        """A scan started or ended: the 'library is empty' text depends on that."""
        self._empty.refresh()


class _EmptyState(QWidget):
    """Shown on Home when the library has no videos at all."""

    def __init__(self, host, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._host = host
        title = QLabel("Your library is empty")
        title.setObjectName("emptyTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint = QLabel()
        self._hint.setObjectName("hint")
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint.setWordWrap(True)
        add = QPushButton("Add a Folder…")
        add.setObjectName("primary")
        add.setCursor(Qt.CursorShape.PointingHandCursor)
        add.clicked.connect(host.manage_folders)
        rescan = QPushButton("Rescan Library")
        rescan.setCursor(Qt.CursorShape.PointingHandCursor)
        rescan.clicked.connect(lambda: host.rescan(manual=True))
        self._title = title
        self._actions = (add, rescan)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(add)
        buttons.addWidget(rescan)
        buttons.addStretch(1)
        buttons.setSpacing(12)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.SIDE_MARGIN, 90, theme.SIDE_MARGIN, 40)
        layout.setSpacing(14)
        layout.addWidget(title)
        layout.addWidget(self._hint)
        layout.addSpacing(10)
        layout.addLayout(buttons)

    def refresh(self) -> None:
        folders = self._host.library.folders()
        listed = ", ".join(f.path for f in folders)
        scanning = self._host.is_scanning and bool(folders)
        if scanning:
            title, text = "Looking for videos…", f"Scanning {listed}. This takes a moment the first time."
        elif not folders:
            title, text = "Your library is empty", "Add a folder that holds your movies and TV shows to get started."
        else:
            title, text = "Your library is empty", f"No videos were found in {listed}."
        self._title.setText(title)
        self._hint.setText(text)
        for button in self._actions:
            button.setVisible(not scanning)


class GridPage(SmoothScrollArea):
    """A titled, responsive grid of cards (the TV Shows and Movies tabs)."""

    def __init__(
        self, host, title: str, load: Callable[[], list[CardModel]], empty_text: str,
        poster: bool = False, tint: str = "",
    ) -> None:
        super().__init__()
        self._host, self._load = host, load
        content = AmbientPage(tint)
        self._title = QLabel(title)
        self._title.setObjectName("pageTitle")
        self._count = QLabel()
        self._count.setObjectName("hint")
        heading = QHBoxLayout()
        heading.setSpacing(14)
        heading.addWidget(self._title)
        heading.addWidget(self._count, 0, Qt.AlignmentFlag.AlignBottom)
        heading.addStretch(1)
        self._grid = CardGrid(poster)
        self._empty = QLabel(empty_text)
        self._empty.setObjectName("hint")

        layout = QVBoxLayout(content)
        layout.setContentsMargins(theme.SIDE_MARGIN - CARD_INSET, 28, theme.SIDE_MARGIN - CARD_INSET, 48)
        layout.setSpacing(18)
        heading.setContentsMargins(CARD_INSET, 0, CARD_INSET, 0)
        self._empty.setContentsMargins(CARD_INSET, 0, CARD_INSET, 0)
        layout.addLayout(heading)
        layout.addWidget(self._empty)
        layout.addWidget(self._grid)
        layout.addStretch(1)
        self.setWidget(content)
        backdrop.install(self)

    def reload(self) -> None:
        models = self._load()
        self._grid.set_cards([self._host.make_card(model) for model in models])
        self._count.setText(str(len(models)) if models else "")
        self._empty.setVisible(not models)


class SearchPage(SmoothScrollArea):
    """Results of the search box, grouped by kind."""

    def __init__(self, host) -> None:
        super().__init__()
        self._host = host
        self._query = ""
        content = AmbientPage()
        self._title = QLabel()
        self._title.setObjectName("pageTitle")
        self._title.setContentsMargins(CARD_INSET, 0, CARD_INSET, 0)
        self._nothing = QLabel("Nothing in your library matches. Try fewer or different words.")
        self._nothing.setObjectName("hint")
        self._nothing.setContentsMargins(CARD_INSET, 0, CARD_INSET, 0)

        layout = QVBoxLayout(content)
        layout.setContentsMargins(theme.SIDE_MARGIN - CARD_INSET, 28, theme.SIDE_MARGIN - CARD_INSET, 48)
        layout.setSpacing(16)
        layout.addWidget(self._title)
        layout.addWidget(self._nothing)
        self._sections: dict[str, tuple[QLabel, CardGrid]] = {}
        for key, heading in (("shows", "TV Shows"), ("movies", "Movies"), ("others", "Other Videos"), ("episodes", "Episodes")):
            label = QLabel(heading)
            label.setObjectName("rowTitle")
            label.setContentsMargins(CARD_INSET, 14, CARD_INSET, 0)
            grid = CardGrid(poster=key in ("shows", "movies"))
            layout.addWidget(label)
            layout.addWidget(grid)
            self._sections[key] = (label, grid)
        layout.addStretch(1)
        self.setWidget(content)
        backdrop.install(self)

    def search(self, query: str) -> None:
        self._query = query.strip()
        self.reload()
        self.verticalScrollBar().setValue(0)

    def reload(self) -> None:
        results = self._host.library.search(self._query)
        builders = {
            "shows": lambda show: show_card(show, poster=True),
            "movies": lambda media: movie_card(media, poster=True),
            "others": movie_card,
            "episodes": episode_card,
        }
        total = 0
        for key, (label, grid) in self._sections.items():
            items = results[key]
            total += len(items)
            grid.set_cards([self._host.make_card(builders[key](item)) for item in items])
            label.setVisible(bool(items))
            grid.setVisible(bool(items))
        self._title.setText(f"Results for “{self._query}”")
        self._nothing.setVisible(total == 0)


# ---------------------------------------------------------------------------
# show detail
# ---------------------------------------------------------------------------


class ShowBanner(QWidget):
    """The picture header of a show; clicking it plays what is next."""

    playRequested = pyqtSignal()
    backRequested = pyqtSignal()
    HEIGHT = 420

    def __init__(self, images: ImageLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._images = images
        self._show: Show | None = None
        self._next: NextUp | None = None
        self._image: str | None = None
        self.setFixedHeight(self.HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self._back = QToolButton(self)
        self._back.setIcon(icons.icon("back"))
        self._back.setIconSize(QSize(24, 24))
        self._back.setToolTip("Back (Esc)")
        self._back.setCursor(Qt.CursorShape.PointingHandCursor)
        self._back.setFixedSize(44, 44)
        self._back.setObjectName("backRound")
        self._back.move(theme.SIDE_MARGIN - 8, 22)
        self._back.clicked.connect(self.backRequested)

        self._play = PlayButton(parent=self)
        self._play.clicked.connect(self.playRequested)

    def apply_theme(self) -> None:
        self._back.setIcon(icons.icon("back"))

    def set_show(self, show: Show, next_up: NextUp | None) -> None:
        self._show, self._next = show, next_up
        self._play.setText(next_up.label if next_up else "Play")
        self._play.setEnabled(next_up is not None)
        self._play.adjustSize()
        self._play.move(theme.SIDE_MARGIN, self.HEIGHT - 44 - self._play.height())
        self._image = None
        self.set_image(show.backdrop_path if self.has_backdrop(show) else show.image)
        self.update()

    @staticmethod
    def has_backdrop(show: Show) -> bool:
        """Whether the show's wide banner frame exists (the cache may have been cleared)."""
        return bool(show.backdrop_path) and os.path.isfile(show.backdrop_path)

    def set_image(self, path: str | None) -> None:
        if path and path != self._image:
            self._image = path
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._next is not None:
            self.playRequested.emit()

    def _next_line(self) -> str:
        if self._next is None:
            return ""
        episode = self._next.episode
        parts = [episode.episode_code, episode.title]
        if self._next.action == "resume":
            parts.append(f"{format_duration(episode.remaining)} left")
        elif episode.total_duration:
            parts.append(format_duration(episode.total_duration))
        return "  ·  ".join(p for p in parts if p)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
            | QPainter.RenderHint.TextAntialiasing
        )
        rect = QRectF(self.rect())
        painter.fillRect(rect, QColor(theme.BG))
        # Ask for the picture in coarse width steps so resizing the window does not
        # trigger a fresh decode for every pixel, then crop the excess.
        step = 240
        request = QSize(math.ceil(self.width() / step) * step, self.height())
        dpr = self.devicePixelRatioF()
        pixmap = self._images.get(self._image, request, dpr, self)
        if pixmap is not None:
            excess = (request.width() - self.width()) * dpr
            source = QRectF(excess / 2, 0, self.width() * dpr, self.height() * dpr)
            painter.drawPixmap(rect, pixmap, source)
        elif self._show is not None:
            paint_placeholder(painter, rect, self._show.title)

        side = QLinearGradient(0, 0, rect.width(), 0)
        base = QColor(theme.BG)  # the banner melts into the page, so its type follows the theme
        ink = (base.red(), base.green(), base.blue())
        side.setColorAt(0.0, QColor(*ink, 235))
        side.setColorAt(0.35, QColor(*ink, 150))
        side.setColorAt(0.75, QColor(*ink, 0))
        painter.fillRect(rect, side)
        fade = QLinearGradient(0, rect.height() * 0.45, 0, rect.height())
        fade.setColorAt(0.0, QColor(*ink, 0))
        fade.setColorAt(1.0, QColor(*ink, 255))
        painter.fillRect(rect, fade)

        if self._show is None:
            painter.end()
            return
        left = float(theme.SIDE_MARGIN)
        available = rect.width() * 0.62
        baseline = float(self._play.y()) - 22

        line = self._next_line()
        if line:
            font = theme.ui_font(15, QFont.Weight.Medium)
            painter.setFont(font)
            painter.setPen(QColor(theme.TEXT_SOFT))
            metrics = QFontMetricsF(font)
            painter.drawText(QPointF(left, baseline), metrics.elidedText(line, Qt.TextElideMode.ElideRight, available))
            baseline -= metrics.height() + 6

        details = [str(self._show.year)] if self._show.year else []
        details.append(self._show.summary)
        font = theme.ui_font(15)
        painter.setFont(font)
        painter.setPen(QColor(theme.TEXT_DIM))
        painter.drawText(QPointF(left, baseline), "  ·  ".join(details))
        baseline -= QFontMetricsF(font).height() + 8

        font = theme.ui_font(52, QFont.Weight.ExtraBold)
        metrics = QFontMetricsF(font)
        while metrics.horizontalAdvance(self._show.title) > available and font.pixelSize() > 26:
            font.setPixelSize(font.pixelSize() - 2)
            metrics = QFontMetricsF(font)
        painter.setFont(font)
        painter.setPen(QColor(theme.TEXT))
        painter.drawText(
            QPointF(left, baseline - metrics.descent() + 4),
            metrics.elidedText(self._show.title, Qt.TextElideMode.ElideRight, available),
        )
        painter.end()


class EpisodeRow(QWidget):
    """One line of the episode list: number, frame, title, running time, play button."""

    playRequested = pyqtSignal(object, bool)  # media, from the beginning
    HEIGHT = 124
    _THUMB = QSize(176, 99)

    def __init__(self, media: Media, label: str, images: ImageLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.media = media
        self._label = label
        self._images = images
        self._image = media.image
        self.setFixedHeight(self.HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

        if media.in_progress:
            text, primary = "Resume", True
        elif media.is_finished:
            text, primary = "Play Again", False
        else:
            text, primary = "Play", False
        if primary:
            self._button = PlayButton(text, self)
        else:
            self._button = QPushButton(text, self)
            self._button.setObjectName("outline")
            self._button.setIcon(icons.icon("play", theme.TEXT_SOFT))
            self._button.setIconSize(QSize(14, 14))
            self._button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._button.setFixedSize(128, 38)
        self._button.clicked.connect(lambda: self.playRequested.emit(self.media, False))

        self._restart = QToolButton(self)
        self._restart.setIcon(icons.icon("restart", theme.TEXT_DIM))
        self._restart.setIconSize(QSize(20, 20))
        self._restart.setToolTip("Start over from the beginning")
        self._restart.setCursor(Qt.CursorShape.PointingHandCursor)
        self._restart.setFixedSize(38, 38)
        self._restart.setVisible(media.in_progress)
        self._restart.clicked.connect(lambda: self.playRequested.emit(self.media, True))

    def apply_theme(self) -> None:
        if self._button.objectName() == "outline":
            self._button.setIcon(icons.icon("play", theme.TEXT_SOFT))
        self._restart.setIcon(icons.icon("restart", theme.TEXT_DIM))

    def on_thumbnail(self, media_id: int, path: str) -> None:
        if path and media_id == self.media.id and not self._image:
            self._image = path
            self.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        y = (self.height() - self._button.height()) // 2
        self._button.move(self.width() - 20 - self._button.width(), y)
        self._restart.move(self._button.x() - 8 - self._restart.width(), y)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.playRequested.emit(self.media, False)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
            | QPainter.RenderHint.TextAntialiasing
        )
        rect = QRectF(self.rect())
        if self.underMouse():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, 15))
            painter.drawRoundedRect(rect.adjusted(0, 2, 0, -2), 12, 12)
        else:
            painter.setPen(QColor(theme.BORDER))
            painter.drawLine(QPointF(12, rect.bottom() - 0.5), QPointF(rect.right() - 12, rect.bottom() - 0.5))

        media = self.media
        font = theme.ui_font(22, QFont.Weight.Medium)
        painter.setFont(font)
        painter.setPen(QColor(theme.TEXT_DIM))
        painter.drawText(QRectF(0, 0, 64, rect.height()), Qt.AlignmentFlag.AlignCenter, self._label)

        thumb = QRectF(70, (rect.height() - self._THUMB.height()) / 2, self._THUMB.width(), self._THUMB.height())
        clip = QPainterPath()
        clip.addRoundedRect(thumb, 10, 10)
        painter.save()
        painter.setClipPath(clip)
        pixmap = self._images.get(self._image, self._THUMB, self.devicePixelRatioF(), self)
        if pixmap is not None:
            painter.drawPixmap(thumb.toAlignedRect(), pixmap)
        else:
            paint_placeholder(painter, thumb, media.title)
        if media.is_finished:
            painter.fillRect(thumb, QColor(0, 0, 0, 120))
        if media.in_progress or media.is_finished:
            track = QRectF(thumb.left(), thumb.bottom() - 4, thumb.width(), 4)
            painter.fillRect(track, QColor(255, 255, 255, 90))
            painter.fillRect(QRectF(track.left(), track.top(), track.width() * media.fraction, 4), QColor(theme.ACCENT))
        painter.restore()
        if media.is_finished:
            badge = QRectF(thumb.center().x() - 13, thumb.center().y() - 13, 26, 26)
            icons.paint(painter, "check", badge, "#ffffff")

        left = thumb.right() + 22
        right_limit = (self._restart.x() if self._restart.isVisible() else self._button.x()) - 18
        width = max(60.0, right_limit - left)
        font = theme.ui_font(16, QFont.Weight.DemiBold)
        metrics = QFontMetricsF(font)
        painter.setFont(font)
        painter.setPen(QColor(theme.TEXT))
        middle = rect.height() / 2
        painter.drawText(
            QPointF(left, middle - 4), metrics.elidedText(media.title, Qt.TextElideMode.ElideRight, width)
        )

        details = []
        if media.total_duration:
            details.append(format_duration(media.total_duration))
        if media.in_progress:
            details.append(f"{format_duration(media.remaining)} left")
        elif media.is_finished:
            details.append("Watched")
        font = theme.ui_font(13)
        painter.setFont(font)
        painter.setPen(QColor(theme.TEXT_DIM))
        painter.drawText(QPointF(left, middle + 20), "  ·  ".join(details))
        painter.end()

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)


class SeasonBox(QComboBox):
    """The season drop-down, with a chevron painted where the style sheet leaves a gap."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setView(QListView())  # a plain list view honours the style sheet's item padding
        self.setMaxVisibleItems(12)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = 16.0
        icons.paint(painter, "chevron-down", QRectF(self.width() - side - 12, (self.height() - side) / 2, side, side))
        painter.end()


def season_name(number: int) -> str:
    if number > 0:
        return f"Season {number}"
    return "Specials" if number == 0 else "Other Episodes"


class ShowPage(SmoothScrollArea):
    """Detail view of one show: banner, season selector, ordered episode list."""

    def __init__(self, host) -> None:
        super().__init__()
        self._host = host
        self._show: Show | None = None
        self._next: NextUp | None = None
        self._season: int | None = None
        self._rows: list[EpisodeRow] = []

        content = AmbientPage()
        self.banner = ShowBanner(host.images)
        self.banner.playRequested.connect(self._play_next)
        self.banner.backRequested.connect(host.go_back)

        heading = QLabel("Episodes")
        heading.setObjectName("sectionTitle")
        self._count = QLabel()
        self._count.setObjectName("hint")
        self._seasons = SeasonBox()
        self._seasons.setCursor(Qt.CursorShape.PointingHandCursor)
        self._seasons.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._seasons.currentIndexChanged.connect(self._season_chosen)
        bar = QHBoxLayout()
        bar.setContentsMargins(theme.SIDE_MARGIN, 14, theme.SIDE_MARGIN, 12)
        bar.setSpacing(14)
        bar.addWidget(heading)
        bar.addWidget(self._count, 0, Qt.AlignmentFlag.AlignBottom)
        bar.addStretch(1)
        bar.addWidget(self._seasons)

        self._list = QVBoxLayout()
        self._list.setContentsMargins(theme.SIDE_MARGIN - 12, 0, theme.SIDE_MARGIN - 12, 0)
        self._list.setSpacing(0)

        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 56)
        layout.setSpacing(0)
        layout.addWidget(self.banner)
        layout.addLayout(bar)
        layout.addLayout(self._list)
        layout.addStretch(1)
        self.setWidget(content)
        backdrop.install(self)

    @property
    def show_id(self) -> int | None:
        return self._show.id if self._show else None

    def open(self, show_id: int) -> bool:
        """Display *show_id* from the top, on the season that is up next."""
        self._season = None
        if not self._load(show_id):
            return False
        self.verticalScrollBar().setValue(0)
        return True

    def reload(self, follow_next: bool = False) -> bool:
        """Refresh after playback or a rescan. False if the show no longer exists.

        With ``follow_next`` the list jumps to the season of the episode that is
        up next (used after playback); otherwise the chosen season stays put.
        """
        if self._show is None:
            return False
        return self._load(self._show.id, None if follow_next else self._season)

    def _load(self, show_id: int, keep_season: int | None = None) -> bool:
        library = self._host.library
        show = library.show(show_id)
        if show is None:
            self._show = None
            return False
        self._show = show
        self._next = library.next_up(show_id)
        self.banner.set_show(show, self._next)
        if not self.banner.has_backdrop(show):
            self._host.thumbs.request_backdrop(show)

        seasons = library.seasons(show_id)
        wanted = keep_season
        if wanted not in seasons:
            upcoming = self._next.episode.season if self._next else None
            wanted = upcoming if upcoming in seasons else (-1 if upcoming is None and -1 in seasons else seasons[0])
        self._seasons.blockSignals(True)
        self._seasons.clear()
        for number in seasons:
            self._seasons.addItem(season_name(number), number)
        self._seasons.setCurrentIndex(seasons.index(wanted))
        self._seasons.blockSignals(False)
        self._seasons.setVisible(len(seasons) > 1)
        self._show_season(wanted)
        return True

    def on_backdrop(self, show_id: int, path: str) -> None:
        if path and self._show is not None and show_id == self._show.id:
            self.banner.set_image(path)

    def _season_chosen(self, index: int) -> None:
        if index >= 0 and self._show is not None:
            self._show_season(self._seasons.itemData(index))

    def _show_season(self, season: int) -> None:
        self._season = season
        for row in self._rows:
            self._list.removeWidget(row)
            discard(row)
        self._rows = []
        episodes = self._host.library.episodes(self._show.id, season)
        for index, episode in enumerate(episodes, start=1):
            label = str(episode.episode) if episode.episode is not None else str(index)
            row = EpisodeRow(episode, label, self._host.images)
            row.playRequested.connect(self._host.play)
            self._host.thumbs.thumbnailReady.connect(row.on_thumbnail)
            self._list.addWidget(row)
            self._rows.append(row)
        runtime = sum(e.total_duration for e in episodes)
        text = plural(len(episodes), "episode")
        if runtime:
            text += f"  ·  {format_duration(runtime)}"
        self._count.setText(text if self._seasons.count() <= 1 else f"{season_name(season)}  ·  {text}")
        self._host.thumbs.prioritise(e for e in episodes if not e.image)

    def _play_next(self) -> None:
        if self._next is not None:
            self._host.play(self._next.episode, self._next.action != "resume")
