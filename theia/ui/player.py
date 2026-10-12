"""The embedded video player: libVLC rendering into a Qt widget, with controls.

libVLC draws straight into the X11 window of :class:`VideoSurface`, so nothing
Qt paints can be blended over the picture. The title bar and the control bar are
therefore opaque sibling widgets stacked above the surface; they slide out of
the way when the mouse rests.
"""

from __future__ import annotations

import os
import shlex
import time
from pathlib import Path

from PyQt6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QColor, QPainter
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from ..config import SAVE_INTERVAL_MS, SKIP_SECONDS
from ..library import Library
from ..models import Media, format_clock
from ..subtitles import ExternalSubtitle, find_external_subtitles
from . import icons, theme

_TICK_MS = 200
_HIDE_AFTER_MS = 2500
_HUD_MS = 500
_TOP_HEIGHT = 60
_BOTTOM_HEIGHT = 104


class VideoSurface(QFrame):
    """The black rectangle libVLC renders into."""

    activity = pyqtSignal()
    clicked = pyqtSignal()
    doubleClicked = pyqtSignal()
    scrolled = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAutoFillBackground(True)
        palette = self.palette()
        palette.setColor(self.backgroundRole(), QColor(0, 0, 0))
        self.setPalette(palette)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # A single click toggles pause, but only once we know it is not the first
        # half of a double click (which toggles fullscreen).
        self._click = QTimer(self)
        self._click.setSingleShot(True)
        self._click.setInterval(QApplication.doubleClickInterval())
        self._click.timeout.connect(self.clicked)

    def mouseMoveEvent(self, event) -> None:
        self.activity.emit()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._click.start()

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._click.stop()
            self.doubleClicked.emit()

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        if delta:
            self.scrolled.emit(1 if delta > 0 else -1)


class Slider(QWidget):
    """A thin horizontal slider: click anywhere to jump, drag to scrub."""

    moved = pyqtSignal(float)  # value while the handle is being dragged
    released = pyqtSignal(float)  # value when the mouse button is let go

    def __init__(self, colour: str, time_tooltip: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._colour = QColor(colour)
        self._time_tooltip = time_tooltip
        self._maximum = 1.0
        self._value = 0.0
        self._dragging = False
        self.setFixedHeight(22)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

    def set_colour(self, colour: str) -> None:
        self._colour = QColor(colour)
        self.update()

    def set_maximum(self, maximum: float) -> None:
        self._maximum = max(0.001, maximum)
        self.update()

    def maximum(self) -> float:
        return self._maximum

    def set_value(self, value: float) -> None:
        """Move the handle programmatically (ignored while the user is dragging it)."""
        if not self._dragging:
            self._value = min(self._maximum, max(0.0, value))
            self.update()

    def value(self) -> float:
        return self._value

    def is_dragging(self) -> bool:
        return self._dragging

    def _value_at(self, x: float) -> float:
        margin = 8.0
        span = max(1.0, self.width() - 2 * margin)
        return min(1.0, max(0.0, (x - margin) / span)) * self._maximum

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._value = self._value_at(event.position().x())
            self.moved.emit(self._value)
            self.update()

    def mouseMoveEvent(self, event) -> None:
        x = event.position().x()
        if self._dragging:
            self._value = self._value_at(x)
            self.moved.emit(self._value)
            self.update()
        if self._time_tooltip:
            QToolTip.showText(
                self.mapToGlobal(QPoint(round(x) - 18, -34)), format_clock(self._value_at(x)), self
            )

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            self.released.emit(self._value)
            self.update()

    def enterEvent(self, event) -> None:
        self.update()

    def leaveEvent(self, event) -> None:
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        active = self.underMouse() or self._dragging
        thickness = 6.0 if active else 4.0
        margin = 8.0
        span = self.width() - 2 * margin
        y = (self.height() - thickness) / 2
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 70))
        painter.drawRoundedRect(QRectF(margin, y, span, thickness), thickness / 2, thickness / 2)
        filled = span * (self._value / self._maximum)
        painter.setBrush(self._colour)
        painter.drawRoundedRect(QRectF(margin, y, filled, thickness), thickness / 2, thickness / 2)
        if active:
            painter.drawEllipse(QPointF(margin + filled, self.height() / 2), 7.0, 7.0)
        painter.end()


def _tool(icon_name: str, tip: str, size: int = 28) -> QToolButton:
    button = QToolButton()
    button.setProperty("iconName", icon_name)
    button.setIcon(icons.icon(icon_name))
    button.setIconSize(QSize(size, size))
    button.setToolTip(tip)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    button.setFixedSize(44, 44)
    return button


class PlayerPage(QWidget):
    """Full-window player. ``closed`` fires when the viewer leaves it."""

    closed = pyqtSignal()

    def __init__(self, library: Library, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._library = library
        self._vlc = None  # libvlc instance, created on first use
        self._player = None
        self._media: Media | None = None
        self._next: Media | None = None
        self._externals: list[ExternalSubtitle] = []
        self._external_ids: dict[str, int] = {}  # subtitle file -> libVLC track id
        self._embedded_spu: set[int] = set()
        self._pending_external: str | None = None
        self._expected_start = 0.0
        self._position_valid = False
        self._tracks_applied = False
        self._playing_ticks = 0
        self._ended = False
        self._has_history = False
        self._vlc_module = None
        self._was_maximized = False
        self._volume = int(library.setting("volume", "100") or 100)
        self._muted = library.flag("muted", False)

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.surface = VideoSurface(self)
        self.surface.activity.connect(self._wake)
        self.surface.clicked.connect(self.toggle_pause)
        self.surface.doubleClicked.connect(self.toggle_fullscreen)
        self.surface.scrolled.connect(lambda direction: self.change_volume(5 * direction))

        self._build_top_bar()
        self._build_bottom_bar()
        self._hud = QLabel(self)
        self._hud.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hud.setObjectName("playerHud")
        self._hud.hide()
        self._hud_timer = QTimer(self)
        self._hud_timer.setSingleShot(True)
        self._hud_timer.setInterval(_HUD_MS)
        self._hud_timer.timeout.connect(self._hud.hide)

        self._tick = QTimer(self)
        self._tick.setInterval(_TICK_MS)
        self._tick.timeout.connect(self._on_tick)
        self._saver = QTimer(self)
        self._saver.setInterval(SAVE_INTERVAL_MS)
        self._saver.timeout.connect(self._save_progress)
        self._hider = QTimer(self)
        self._hider.setSingleShot(True)
        self._hider.setInterval(_HIDE_AFTER_MS)
        self._hider.timeout.connect(self._maybe_hide)
        self._note_timer = QTimer(self)
        self._note_timer.setSingleShot(True)
        self._note_timer.timeout.connect(lambda: self._note.setText(""))
        self._volume_saver = QTimer(self)
        self._volume_saver.setSingleShot(True)
        self._volume_saver.setInterval(400)
        self._volume_saver.timeout.connect(self._store_volume)

    # ------------------------------------------------------------ building

    def _build_top_bar(self) -> None:
        self.top_bar = QWidget(self)
        self.top_bar.setObjectName("playerBar")
        self.top_bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        back = _tool("back", "Back to library (Esc)", 26)
        back.clicked.connect(self.close_player)
        self._title = QLabel()
        self._title.setObjectName("playerTitle")
        self._title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)  # may be clipped
        self._note = QLabel()
        self._note.setObjectName("playerNote")
        layout = QHBoxLayout(self.top_bar)
        layout.setContentsMargins(14, 0, 24, 0)
        layout.setSpacing(10)
        layout.addWidget(back)
        layout.addWidget(self._title, 1)
        layout.addWidget(self._note)

    def _build_bottom_bar(self) -> None:
        self.controls = QWidget(self)
        self.controls.setObjectName("playerBar")
        self.controls.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self._seek = Slider(theme.ACCENT, time_tooltip=True)
        self._seek.moved.connect(self._on_scrub)
        self._seek.released.connect(self._on_seek_released)
        self._last_scrub = 0.0
        self._icon_shows_pause = True

        self._play_button = _tool("pause", "Play / Pause (Space)", 32)
        self._play_button.clicked.connect(self.toggle_pause)
        self._show_play_icon()
        back10 = _tool("back10", f"Back {SKIP_SECONDS} seconds (←)")
        back10.clicked.connect(lambda: self.skip(-SKIP_SECONDS))
        forward10 = _tool("forward10", f"Forward {SKIP_SECONDS} seconds (→)")
        forward10.clicked.connect(lambda: self.skip(SKIP_SECONDS))
        restart = _tool("restart", "Start over from the beginning", 24)
        restart.clicked.connect(self.restart)

        self._mute_button = _tool("volume", "Mute (M)", 26)
        self._mute_button.clicked.connect(self.toggle_mute)
        self._volume_slider = Slider(theme.ACCENT_ALT)
        self._volume_slider.setFixedWidth(110)
        self._volume_slider.set_maximum(100)
        self._volume_slider.set_value(self._volume)
        self._volume_slider.moved.connect(lambda value: self.set_volume(round(value)))

        self._time = QLabel("0:00 / 0:00")
        self._time.setObjectName("playerTime")

        self._next_button = QToolButton()
        self._next_button.setObjectName("textTool")
        self._next_button.setText("Next Episode")
        self._next_button.setProperty("iconName", "next")
        self._next_button.setIcon(icons.icon("next"))
        self._next_button.setIconSize(QSize(24, 24))
        self._next_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._next_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._next_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._next_button.setFixedHeight(44)
        self._next_button.clicked.connect(self.play_next)

        self._subtitle_button = _tool("subtitles", "Subtitles (S)", 26)
        self._subtitle_button.clicked.connect(self._show_subtitle_menu)
        self._audio_button = _tool("audio", "Audio track (A)", 26)
        self._audio_button.clicked.connect(self._show_audio_menu)
        self._fullscreen_button = _tool("fullscreen", "Fullscreen (F)", 26)
        self._fullscreen_button.clicked.connect(self.toggle_fullscreen)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        for widget in (self._play_button, back10, forward10, restart):
            row.addWidget(widget)
        row.addSpacing(10)
        row.addWidget(self._mute_button)
        row.addWidget(self._volume_slider)
        row.addSpacing(14)
        row.addWidget(self._time)
        row.addStretch(1)
        row.addWidget(self._next_button)
        row.addSpacing(8)
        for widget in (self._subtitle_button, self._audio_button, self._fullscreen_button):
            row.addWidget(widget)

        layout = QVBoxLayout(self.controls)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(2)
        layout.addWidget(self._seek)
        layout.addLayout(row)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        width, height = self.width(), self.height()
        self.surface.setGeometry(0, 0, width, height)
        self.top_bar.setGeometry(0, 0, width, _TOP_HEIGHT)
        self.controls.setGeometry(0, height - _BOTTOM_HEIGHT, width, _BOTTOM_HEIGHT)
        self.top_bar.raise_()
        self.controls.raise_()
        self._hud.raise_()

    # ----------------------------------------------------------------- VLC

    def _ensure_vlc(self) -> bool:
        if self._player is not None:
            return True
        try:
            import vlc

            arguments = [
                "--no-video-title-show",  # no file name splashed over the first seconds
                "--no-osd",
                "--no-snapshot-preview",
                "--quiet",
                "--no-sub-autodetect-file",  # external subtitles are attached explicitly
                "--mouse-hide-timeout=2000000000",  # the cursor is hidden by Qt, not by VLC
                *shlex.split(os.environ.get("THEIA_VLC_ARGS", "")),
            ]
            instance = vlc.Instance(arguments)
            if instance is None:
                raise RuntimeError("libVLC refused to start")
            player = instance.media_player_new()
            # Let Qt, not VLC, receive mouse and keyboard input over the picture.
            player.video_set_mouse_input(False)
            player.video_set_key_input(False)
        except Exception as error:
            QMessageBox.critical(
                self.window(), "Theia",
                "The video player could not be started.\n\n"
                f"{error}\n\nIs VLC installed? Run ./run.sh in a terminal to install it, or use your package manager "
                "(apt: vlc libvlc-dev · pacman: vlc vlc-plugins-all · dnf: vlc from RPM Fusion).",
            )
            return False
        self._vlc_module, self._vlc, self._player = vlc, instance, player
        return True

    @property
    def is_active(self) -> bool:
        return self._media is not None

    def play(self, media: Media, from_start: bool = False) -> bool:
        """Start playing *media*, resuming where it was left unless ``from_start``."""
        if not os.path.isfile(media.path):
            QMessageBox.warning(
                self.window(), "Theia",
                f"This file is not available right now:\n\n{media.path}\n\n"
                "If it lives on an external drive, connect the drive and try again.",
            )
            return False
        if not self._ensure_vlc():
            return False
        self._finish_current()

        start = 0.0 if from_start else media.resume_position
        item = self._vlc.media_new_path(media.path)
        if start > 0:
            item.add_option(f":start-time={start:.3f}")
        hardware = self._library.flag("hardware_decoding", True) and os.environ.get("THEIA_HWDEC", "1") != "0"
        if not hardware:
            # set_xwindow() resets libVLC's instance-wide --avcodec-hw, so it has to be per media.
            item.add_option(":avcodec-hw=none")

        self._media = media
        self._next = self._library.next_episode(media)
        self._externals = find_external_subtitles(media.path)
        self._external_ids, self._embedded_spu, self._pending_external = {}, set(), None
        self._expected_start = start
        self._position_valid = False
        self._tracks_applied = False
        self._playing_ticks = 0
        self._ended = False
        self._has_history = media.last_watched is not None

        self._player.set_media(item)
        self._player.set_xwindow(int(self.surface.winId()))
        # Make sure both bars are real X11 windows stacked above the video surface.
        self.top_bar.winId()
        self.controls.winId()
        self._player.play()

        self._title.setText(media.full_title)
        self._next_button.setVisible(self._next is not None)
        if self._next is not None:
            self._next_button.setToolTip(f"{self._next.episode_code} · {self._next.title} (N)")
        self._seek.set_maximum(media.total_duration or 1.0)
        self._seek.set_value(start)
        self._update_time_label(start, media.total_duration)
        self._flash(f"Resuming from {format_clock(start)}" if start > 0 else "")
        self._set_play_icon(True)
        self._tick.start()
        self._saver.start()
        self.setFocus()
        self._enter_fullscreen()
        self._hider.stop()
        self.top_bar.hide()
        self.controls.hide()
        self.surface.setCursor(Qt.CursorShape.BlankCursor)
        return True

    def _finish_current(self) -> None:
        """Persist progress and stop whatever is playing."""
        self._tick.stop()
        self._saver.stop()
        if self._media is not None and not self._ended:
            self._save_progress()
        if self._player is not None:
            self._player.stop()
        self.surface.setUpdatesEnabled(True)
        self._media = None

    def close_player(self) -> None:
        """Leave the player and return to the library."""
        self._finish_current()
        self._hider.stop()
        self.surface.unsetCursor()
        self._leave_fullscreen()
        self.closed.emit()

    def shutdown(self) -> None:
        """Called when the application quits."""
        self._finish_current()
        if self._volume_saver.isActive():
            self._volume_saver.stop()
            self._store_volume()
        if self._player is not None:
            self._player.release()
            self._vlc.release()
            self._player = self._vlc = None

    # ------------------------------------------------------------- progress

    def _duration(self) -> float:
        length = self._player.get_length() if self._player is not None else 0
        if length and length > 0:
            return length / 1000.0
        return self._media.total_duration if self._media else 0.0

    def _position(self) -> float:
        time_ms = self._player.get_time() if self._player is not None else -1
        return time_ms / 1000.0 if time_ms is not None and time_ms >= 0 else -1.0

    def _save_progress(self) -> None:
        """Write position and duration to the database (every second, on pause, on exit)."""
        if self._media is None or not self._position_valid:
            return
        position = self._position()
        if position >= 0:
            self._library.save_progress(self._media.id, position, self._duration())

    def _on_tick(self) -> None:
        if self._media is None or self._player is None:
            return
        vlc = self._vlc_module
        state = self._player.get_state()
        if state == vlc.State.Ended:
            self._on_ended()
            return
        if state == vlc.State.Error:
            self._on_error()
            return
        if state not in (vlc.State.Playing, vlc.State.Paused):
            return

        position, duration = self._position(), self._duration()
        if state == vlc.State.Playing:
            self._playing_ticks += 1
            if self._playing_ticks == 1:
                self._apply_volume()
                # From here on VLC owns the surface; Qt must not repaint black over the picture.
                self.surface.setUpdatesEnabled(False)
        if not self._position_valid and position >= 0:
            # After a resume, VLC briefly reports 0 before it has seeked: never save that.
            if position >= self._expected_start - 10 or self._playing_ticks > 25:
                self._position_valid = True
        if not self._tracks_applied and self._playing_ticks >= 2:
            self._apply_initial_tracks()
        self._adopt_new_subtitle_track()

        if position >= 0 and self._position_valid:
            self._seek.set_maximum(duration or 1.0)
            if not self._seek.is_dragging():
                self._seek.set_value(position)
                self._update_time_label(position, duration)
        self._set_play_icon(state == vlc.State.Playing)

    def _update_time_label(self, position: float, duration: float) -> None:
        self._time.setText(f"{format_clock(position)} / {format_clock(duration)}")

    def _on_ended(self) -> None:
        media, upcoming = self._media, self._next
        self._ended = True
        duration = self._duration() or max(self._position(), 1.0)
        self._library.save_progress(media.id, duration, duration)  # watched to the end
        if upcoming is not None:
            fresh = self._library.media(upcoming.id)
            if fresh is not None and self.play(fresh):
                return
        self.close_player()

    def _on_error(self) -> None:
        media = self._media
        self._ended = True  # nothing sensible to save
        self._finish_current()
        QMessageBox.warning(
            self.window(), "Theia", f"This file could not be played:\n\n{media.path if media else ''}"
        )
        self.close_player()

    # ------------------------------------------------------------- controls

    def toggle_pause(self) -> None:
        if self._media is None:
            return
        vlc = self._vlc_module
        playing = self._player.get_state() == vlc.State.Playing
        self._player.set_pause(1 if playing else 0)
        self._set_play_icon(not playing)
        if playing:
            self._save_progress()  # save immediately on pause
        self._show_hud("❚❚" if playing else "▶")

    def _set_play_icon(self, playing: bool) -> None:
        if playing != self._icon_shows_pause:
            self._icon_shows_pause = playing
            self._show_play_icon()

    def _show_play_icon(self) -> None:
        self._play_button.setIcon(icons.icon("pause" if self._icon_shows_pause else "play", theme.ACCENT))

    def apply_theme(self) -> None:
        """Recolour the controls whose icons and meters were built from the previous palette."""
        for button in self.findChildren(QToolButton):
            name = button.property("iconName")
            if name:
                button.setIcon(icons.icon(name))
        self._show_play_icon()
        self._refresh_volume_ui()
        self._fullscreen_button.setIcon(
            icons.icon("fullscreen-exit" if self.window().isFullScreen() else "fullscreen")
        )
        self._seek.set_colour(theme.ACCENT)
        self._volume_slider.set_colour(theme.ACCENT_ALT)

    def skip(self, seconds: float) -> None:
        if self._media is None:
            return
        position = self._position()
        if position < 0:
            return
        self.seek(position + seconds)
        self._show_hud(f"{seconds:+.0f}s")

    def seek(self, position: float) -> None:
        duration = self._duration()
        if duration > 0:
            position = min(position, duration - 1.0)  # never seek onto the very end
        position = max(0.0, position)
        self._player.set_time(int(position * 1000))
        self._position_valid = True  # the viewer chose this spot
        self._expected_start = position
        self._seek.set_value(position)
        self._update_time_label(position, duration)

    def restart(self) -> None:
        if self._media is not None:
            self.seek(0.0)
            self._flash("Started over")

    def _on_scrub(self, value: float) -> None:
        """The seek handle is being dragged: follow it, but do not flood libVLC with seeks."""
        self._update_time_label(value, self._duration())
        now = time.monotonic()
        if self._media is not None and now - self._last_scrub >= 0.2:
            self._last_scrub = now
            self._player.set_time(int(value * 1000))
            self._position_valid = True
        self._wake()

    def _on_seek_released(self, value: float) -> None:
        if self._media is not None:
            self.seek(value)

    def play_next(self) -> None:
        if self._next is None:
            return
        upcoming = self._library.media(self._next.id)
        if upcoming is not None:
            self.play(upcoming)

    # volume

    def _apply_volume(self) -> None:
        self._player.audio_set_volume(self._volume)
        self._player.audio_set_mute(self._muted)
        self._refresh_volume_ui()

    def set_volume(self, volume: int) -> None:
        self._volume = max(0, min(100, volume))
        if self._muted and self._volume > 0:
            self._muted = False
        if self._player is not None:
            self._player.audio_set_volume(self._volume)
            self._player.audio_set_mute(self._muted)
        self._volume_saver.start()  # written once the slider comes to rest
        self._refresh_volume_ui()

    def _store_volume(self) -> None:
        self._library.set_setting("volume", self._volume)
        self._library.set_flag("muted", self._muted)

    def change_volume(self, delta: int) -> None:
        self.set_volume(self._volume + delta)
        self._flash(f"Volume {self._volume}%")
        self._show_hud(f"Volume {self._volume}%")

    def toggle_mute(self) -> None:
        self._muted = not self._muted
        if self._player is not None:
            self._player.audio_set_mute(self._muted)
        self._store_volume()
        self._refresh_volume_ui()
        self._show_hud("Muted" if self._muted else f"Volume {self._volume}%")

    def _refresh_volume_ui(self) -> None:
        silent = self._muted or self._volume == 0
        name = "mute" if silent else ("volume-low" if self._volume < 50 else "volume")
        self._mute_button.setIcon(icons.icon(name))
        self._volume_slider.set_value(0 if self._muted else self._volume)

    # fullscreen

    def toggle_fullscreen(self) -> None:
        window = self.window()
        if window.isFullScreen():
            self._leave_fullscreen()
        else:
            self._enter_fullscreen()

    def _enter_fullscreen(self) -> None:
        window = self.window()
        if not window.isFullScreen():
            self._was_maximized = window.isMaximized()
            window.showFullScreen()
        self._fullscreen_button.setIcon(icons.icon("fullscreen-exit"))

    def _leave_fullscreen(self) -> None:
        window = self.window()
        if window.isFullScreen():
            if self._was_maximized:
                window.showMaximized()
            else:
                window.showNormal()
        self._fullscreen_button.setIcon(icons.icon("fullscreen"))

    # --------------------------------------------------------------- tracks

    def _descriptions(self, getter) -> list[tuple[int, str]]:
        """libVLC track list as ``(id, name)`` pairs, without the 'Disable' entry."""
        tracks = []
        for track_id, name in getter() or []:
            if track_id < 0:
                continue
            if isinstance(name, bytes):
                name = name.decode("utf-8", "replace")
            tracks.append((track_id, name or f"Track {track_id}"))
        return tracks

    def _apply_initial_tracks(self) -> None:
        """Choose audio and subtitles once libVLC knows the file's tracks."""
        self._tracks_applied = True
        media, player, library = self._media, self._player, self._library
        subtitles = self._descriptions(player.video_get_spu_description)
        self._embedded_spu = {track_id for track_id, _name in subtitles}

        audio = self._descriptions(player.audio_get_track_description)
        audio_ids = {track_id for track_id, _name in audio}
        preferred_audio = library.setting("preferred_audio")
        if media.audio_track is not None and media.audio_track in audio_ids:
            player.audio_set_track(media.audio_track)
        elif preferred_audio and len(audio) > 1:
            for track_id, name in audio:
                if name == preferred_audio:
                    player.audio_set_track(track_id)
                    break

        external = {s.path: s for s in self._externals}
        if self._has_history and media.subtitle_file in external:
            self._load_external(media.subtitle_file)  # this file was last watched with it
        elif self._has_history and media.subtitle_track is not None:
            if media.subtitle_track == -1 or media.subtitle_track in self._embedded_spu:
                player.video_set_spu(media.subtitle_track)
        elif library.flag("subtitles_enabled", True):
            preferred = library.setting("preferred_subtitle")
            by_name = {name: track_id for track_id, name in subtitles}
            by_label = {s.label: s for s in self._externals}
            sidecar = next((s for s in self._externals if s.sidecar), None)
            if preferred and preferred in by_name:
                player.video_set_spu(by_name[preferred])
            elif preferred and preferred in by_label:
                self._load_external(by_label[preferred].path)
            elif player.video_get_spu() < 0 and sidecar is not None:
                self._load_external(sidecar.path)  # the release's own subtitle file
            elif player.video_get_spu() < 0:
                # Nothing preferred: show whatever subtitles exist (downloaded/sidecar first).
                if self._externals:
                    self._load_external(self._externals[0].path)
                elif subtitles:
                    player.video_set_spu(next((t for t, n in subtitles if "forced" not in n.lower()), subtitles[0][0]))
        else:
            # Subtitles are switched off, but keep "forced" tracks (foreign-language dialogue).
            current = player.video_get_spu()
            name = dict(subtitles).get(current, "")
            if current >= 0 and "forced" not in name.lower():
                player.video_set_spu(-1)

    def _load_external(self, path: str) -> None:
        known = self._external_ids.get(path)
        if known is not None:
            self._player.video_set_spu(known)
            return
        if not self._tracks_applied:
            # Chosen from the menu in the first instant of playback: note which tracks
            # are the file's own, so the newcomer can be told apart from them.
            self._embedded_spu = {tid for tid, _name in self._descriptions(self._player.video_get_spu_description)}
            self._tracks_applied = True
        self._pending_external = path
        self._player.add_slave(self._vlc_module.MediaSlaveType.subtitle, Path(path).as_uri(), True)

    def _adopt_new_subtitle_track(self) -> None:
        """Work out which libVLC track id an externally loaded subtitle file received."""
        if self._pending_external is None:
            return
        known = self._embedded_spu | set(self._external_ids.values())
        fresh = [tid for tid, _name in self._descriptions(self._player.video_get_spu_description) if tid not in known]
        if fresh:
            self._external_ids[self._pending_external] = max(fresh)
            self._pending_external = None

    def _remember_tracks(self, subtitle_track: int | None, subtitle_file: str | None) -> None:
        if self._media is not None:
            audio = self._player.audio_get_track()
            self._library.save_tracks(self._media.id, audio if audio >= 0 else None, subtitle_track, subtitle_file)

    def _popup(self, menu: QMenu, button: QToolButton) -> None:
        size = menu.sizeHint()
        corner = button.mapToGlobal(QPoint(button.width() - size.width(), -size.height() - 6))
        menu.exec(corner)
        self._wake()

    def _show_subtitle_menu(self) -> None:
        if self._media is None:
            return
        player = self._player
        menu = QMenu(self)
        group = QActionGroup(menu)
        current = player.video_get_spu()
        external_ids = {track_id: path for path, track_id in self._external_ids.items()}

        def add(text: str, checked: bool, handler) -> None:
            action = QAction(text, menu, checkable=True)
            action.setChecked(checked)
            action.triggered.connect(handler)
            group.addAction(action)
            menu.addAction(action)

        add("Off", current < 0 and self._pending_external is None, self._subtitles_off)
        for track_id, name in self._descriptions(player.video_get_spu_description):
            if track_id in external_ids:
                continue
            add(name, track_id == current, lambda _=False, t=track_id, n=name: self._choose_embedded(t, n))
        if self._externals:
            menu.addSeparator()
            header = menu.addAction("Subtitle files")
            header.setEnabled(False)
            for subtitle in self._externals:
                loaded = self._external_ids.get(subtitle.path)
                checked = (loaded is not None and loaded == current) or self._pending_external == subtitle.path
                add(subtitle.label, checked, lambda _=False, s=subtitle: self._choose_external(s))
        self._popup(menu, self._subtitle_button)

    def _subtitles_off(self) -> None:
        self._pending_external = None
        self._player.video_set_spu(-1)
        self._library.set_flag("subtitles_enabled", False)
        self._remember_tracks(-1, None)
        self._flash("Subtitles off")

    def _choose_embedded(self, track_id: int, name: str) -> None:
        self._pending_external = None
        self._player.video_set_spu(track_id)
        self._library.set_flag("subtitles_enabled", True)
        self._library.set_setting("preferred_subtitle", name)
        self._remember_tracks(track_id, None)
        self._flash(f"Subtitles: {name}")

    def _choose_external(self, subtitle: ExternalSubtitle) -> None:
        self._load_external(subtitle.path)
        self._library.set_flag("subtitles_enabled", True)
        self._library.set_setting("preferred_subtitle", subtitle.label)
        self._remember_tracks(None, subtitle.path)
        self._flash(f"Subtitles: {subtitle.label}")

    def _show_audio_menu(self) -> None:
        if self._media is None:
            return
        player = self._player
        menu = QMenu(self)
        group = QActionGroup(menu)
        current = player.audio_get_track()
        tracks = self._descriptions(player.audio_get_track_description)
        if not tracks:
            menu.addAction("No audio tracks").setEnabled(False)
        for track_id, name in tracks:
            action = QAction(name, menu, checkable=True)
            action.setChecked(track_id == current)
            action.triggered.connect(lambda _=False, t=track_id, n=name: self._choose_audio(t, n))
            group.addAction(action)
            menu.addAction(action)
        self._popup(menu, self._audio_button)

    def _choose_audio(self, track_id: int, name: str) -> None:
        self._player.audio_set_track(track_id)
        self._library.set_setting("preferred_audio", name)
        media = self._library.media(self._media.id)
        if media is not None:
            self._library.save_tracks(media.id, track_id, media.subtitle_track, media.subtitle_file)
        self._flash(f"Audio: {name}")

    # ------------------------------------------------- auto-hiding controls

    def _flash(self, text: str) -> None:
        """Show a short note in the title bar for a few seconds."""
        self._note.setText(text)
        if text:
            self._note_timer.start(4500)

    def _show_hud(self, text: str) -> None:
        """Brief centred pill over the video; leaves the top and bottom bars alone."""
        self._hud.setText(text)
        self._hud.adjustSize()
        self._hud.move((self.width() - self._hud.width()) // 2, (self.height() - self._hud.height()) // 2)
        self._hud.winId()
        self._hud.show()
        self._hud.raise_()
        self._hud_timer.start()

    def _wake(self) -> None:
        """Mouse or keyboard activity: show the bars and restart the hide countdown."""
        if not self.top_bar.isVisible():
            self.top_bar.show()
            self.controls.show()
            self.top_bar.raise_()
            self.controls.raise_()
        self.surface.setCursor(Qt.CursorShape.ArrowCursor)
        self._hider.start()

    def _maybe_hide(self) -> None:
        if self._media is None:
            return
        vlc = self._vlc_module
        busy = (
            self._player.get_state() != vlc.State.Playing
            or self.top_bar.underMouse()
            or self.controls.underMouse()
            or self._seek.is_dragging()
            or self._volume_slider.is_dragging()
            or QApplication.activePopupWidget() is not None
        )
        if busy:
            self._hider.start()
            return
        self.top_bar.hide()
        self.controls.hide()
        self.surface.setCursor(Qt.CursorShape.BlankCursor)

    # ------------------------------------------------------------- keyboard

    def keyPressEvent(self, event) -> None:
        key = event.key()
        repeat = event.isAutoRepeat()
        if key == Qt.Key.Key_Left:
            self.skip(-SKIP_SECONDS)
        elif key == Qt.Key.Key_Right:
            self.skip(SKIP_SECONDS)
        elif key == Qt.Key.Key_Up:
            self.change_volume(5)
        elif key == Qt.Key.Key_Down:
            self.change_volume(-5)
        elif repeat:
            return
        elif key in (Qt.Key.Key_Space, Qt.Key.Key_K, Qt.Key.Key_MediaTogglePlayPause, Qt.Key.Key_MediaPlay, Qt.Key.Key_MediaPause):
            self.toggle_pause()
        elif key in (Qt.Key.Key_F, Qt.Key.Key_F11):
            self.toggle_fullscreen()
        elif key == Qt.Key.Key_Escape:
            if self.window().isFullScreen():
                self.toggle_fullscreen()
            else:
                self.close_player()
        elif key == Qt.Key.Key_Backspace:
            self.close_player()
        elif key == Qt.Key.Key_M:
            self.toggle_mute()
        elif key in (Qt.Key.Key_N, Qt.Key.Key_MediaNext):
            self.play_next()
        elif key == Qt.Key.Key_S:
            self._show_subtitle_menu()
        elif key == Qt.Key.Key_A:
            self._show_audio_menu()
        elif key == Qt.Key.Key_Home:
            self.restart()
        else:
            super().keyPressEvent(event)
            return
        event.accept()
