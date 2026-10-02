"""Browsing pages: Home rows, poster grids, search results and the show detail view."""

from __future__ import annotations

import math
import os
from collections.abc import Callable

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QLinearGradient, QPainter, QPainterPath
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
from .images import ImageLoader
from .widgets import (
    CardGrid,
    CardModel,
    CardRow,
    SmoothScrollArea,
    continue_card,
    discard,
    episode_card,
    movie_card,
    paint_placeholder,
    show_card,
)


class HomePage(SmoothScrollArea):
    """Continue Watching, TV Shows, Movies and Other Videos as horizontal rows."""

    def __init__(self, host, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._host = host
        content = QWidget()
        content.setObjectName("page")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 26, 0, 48)
        layout.setSpacing(34)

        self.continue_row = CardRow("Continue Watching")
        self.shows_row = CardRow("TV Shows")
        self.movies_row = CardRow("Movies")
        self.others_row = CardRow("Other Videos")
        for row in (self.continue_row, self.shows_row, self.movies_row, self.others_row):
            layout.addWidget(row)

        self._empty = _EmptyState(host)
        layout.addWidget(self._empty, 1)
        layout.addStretch(1)
        self.setWidget(content)

    def reload(self) -> None:
        library, card = self._host.library, self._host.make_card
        shows, movies, others = library.shows(), library.movies(), library.others()
        self.continue_row.set_cards([card(continue_card(m)) for m in library.continue_watching()])
        self.shows_row.set_cards([card(show_card(s)) for s in shows])
        self.movies_row.set_cards([card(movie_card(m)) for m in movies])
        self.others_row.set_cards([card(movie_card(m)) for m in others])
        self._empty.setVisible(not (shows or movies or others))
        self._empty.refresh()

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

    def __init__(self, host, title: str, load: Callable[[], list[CardModel]], empty_text: str) -> None:
        super().__init__()
        self._host, self._load = host, load
        content = QWidget()
        content.setObjectName("page")
        self._title = QLabel(title)
        self._title.setObjectName("pageTitle")
        self._count = QLabel()
        self._count.setObjectName("hint")
        heading = QHBoxLayout()
        heading.setSpacing(14)
        heading.addWidget(self._title)
        heading.addWidget(self._count, 0, Qt.AlignmentFlag.AlignBottom)
        heading.addStretch(1)
        self._grid = CardGrid()
        self._empty = QLabel(empty_text)
        self._empty.setObjectName("hint")

        layout = QVBoxLayout(content)
        layout.setContentsMargins(theme.SIDE_MARGIN - 6, 30, theme.SIDE_MARGIN - 6, 48)
        layout.setSpacing(18)
        heading.setContentsMargins(6, 0, 6, 0)
        self._empty.setContentsMargins(6, 0, 6, 0)
        layout.addLayout(heading)
        layout.addWidget(self._empty)
        layout.addWidget(self._grid)
        layout.addStretch(1)
        self.setWidget(content)

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
        content = QWidget()
        content.setObjectName("page")
        self._title = QLabel()
        self._title.setObjectName("pageTitle")
        self._title.setContentsMargins(6, 0, 6, 0)
        self._nothing = QLabel("Nothing in your library matches. Try fewer or different words.")
        self._nothing.setObjectName("hint")
        self._nothing.setContentsMargins(6, 0, 6, 0)

        layout = QVBoxLayout(content)
        layout.setContentsMargins(theme.SIDE_MARGIN - 6, 30, theme.SIDE_MARGIN - 6, 48)
        layout.setSpacing(16)
        layout.addWidget(self._title)
        layout.addWidget(self._nothing)
        self._sections: dict[str, tuple[QLabel, CardGrid]] = {}
        for key, heading in (("shows", "TV Shows"), ("movies", "Movies"), ("others", "Other Videos"), ("episodes", "Episodes")):
            label = QLabel(heading)
            label.setObjectName("rowTitle")
            label.setContentsMargins(6, 14, 6, 0)
            grid = CardGrid()
            layout.addWidget(label)
            layout.addWidget(grid)
            self._sections[key] = (label, grid)
        layout.addStretch(1)
        self.setWidget(content)

    def search(self, query: str) -> None:
        self._query = query.strip()
        self.reload()
        self.verticalScrollBar().setValue(0)

    def reload(self) -> None:
        results = self._host.library.search(self._query)
        builders = {"shows": show_card, "movies": movie_card, "others": movie_card, "episodes": episode_card}
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
        self._back.setStyleSheet(
            "QToolButton { background: rgba(20,20,20,0.7); border-radius: 22px; }"
            "QToolButton:hover { background: rgba(60,60,60,0.9); }"
        )
        self._back.move(theme.SIDE_MARGIN - 8, 22)
        self._back.clicked.connect(self.backRequested)

        self._play = QPushButton(self)
        self._play.setObjectName("primary")
        self._play.setIcon(icons.icon("play"))
        self._play.setIconSize(QSize(22, 22))
        self._play.setCursor(Qt.CursorShape.PointingHandCursor)
        self._play.setStyleSheet("padding: 11px 24px 11px 18px; font-size: 16px; font-weight: 700;")
        self._play.clicked.connect(self.playRequested)

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
        side.setColorAt(0.0, QColor(20, 20, 20, 235))
        side.setColorAt(0.35, QColor(20, 20, 20, 150))
        side.setColorAt(0.75, QColor(20, 20, 20, 0))
        painter.fillRect(rect, side)
        fade = QLinearGradient(0, rect.height() * 0.45, 0, rect.height())
        fade.setColorAt(0.0, QColor(20, 20, 20, 0))
        fade.setColorAt(1.0, QColor(20, 20, 20, 255))
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
        self._button = QPushButton(text, self)
        self._button.setObjectName("primary" if primary else "outline")
        self._button.setIcon(icons.icon("play"))
        self._button.setIconSize(QSize(16, 16))
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
            painter.setBrush(QColor("#262626"))
            painter.drawRoundedRect(rect.adjusted(0, 2, 0, -2), 6, 6)
        else:
            painter.setPen(QColor("#2b2b2b"))
            painter.drawLine(QPointF(12, rect.bottom() - 0.5), QPointF(rect.right() - 12, rect.bottom() - 0.5))

        media = self.media
        font = theme.ui_font(22, QFont.Weight.Medium)
        painter.setFont(font)
        painter.setPen(QColor("#d2d2d2"))
        painter.drawText(QRectF(0, 0, 64, rect.height()), Qt.AlignmentFlag.AlignCenter, self._label)

        thumb = QRectF(70, (rect.height() - self._THUMB.height()) / 2, self._THUMB.width(), self._THUMB.height())
        clip = QPainterPath()
        clip.addRoundedRect(thumb, 4, 4)
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
            icons.paint(painter, "check", badge, theme.TEXT_SOFT)

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

        content = QWidget()
        content.setObjectName("page")
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
