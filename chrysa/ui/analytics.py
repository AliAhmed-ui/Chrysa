"""The Watch Analytics dashboard (a dialog that follows the active theme)."""

from __future__ import annotations

from PyQt6.QtCore import QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QPainter, QPainterPath
from PyQt6.QtWidgets import QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget

from ..analytics import Analytics, compute, format_hours
from ..library import Library
from . import theme


class BarChart(QWidget):
    """Horizontal bars with a label on the left and a value on the right."""

    ROW = 30

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[tuple[str, float]] = []
        self._empty = "Nothing watched yet"

    def set_rows(self, rows: list[tuple[str, float]], empty: str = "Nothing watched yet") -> None:
        self._rows, self._empty = rows, empty
        self.setMinimumHeight(max(1, len(rows)) * self.ROW + 4)
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(420, max(1, len(self._rows)) * self.ROW + 4)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setFont(theme.ui_font(13))
        if not self._rows:
            painter.setPen(QColor(theme.TEXT_MUTED))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignVCenter, self._empty)
            return
        label_w, value_w = 120, 64
        track_x, track_w = label_w + 10, max(10, self.width() - label_w - value_w - 20)
        peak = max(value for _, value in self._rows) or 1.0
        for i, (label, value) in enumerate(self._rows):
            top = i * self.ROW
            text_rect = QRectF(0, top, label_w, self.ROW)
            painter.setPen(QColor(theme.TEXT_SOFT))
            elided = painter.fontMetrics().elidedText(label, Qt.TextElideMode.ElideRight, label_w)
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter, elided)
            track = QRectF(track_x, top + 9, track_w, 12)
            path = QPainterPath()
            path.addRoundedRect(track, 6, 6)
            painter.fillPath(path, QColor(theme.SURFACE_RAISED))
            if value > 0:
                fill = QRectF(track_x, top + 9, max(12.0, track_w * value / peak), 12)
                path = QPainterPath()
                path.addRoundedRect(fill, 6, 6)
                painter.fillPath(path, QColor(theme.ACCENT))
            painter.setPen(QColor(theme.TEXT_MUTED))
            painter.drawText(
                QRectF(track_x + track_w + 8, top, value_w, self.ROW),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                format_hours(value),
            )


def _card(title: str) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("analyticsCard")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(18, 14, 18, 16)
    layout.setSpacing(10)
    heading = QLabel(title)
    heading.setObjectName("hint")
    layout.addWidget(heading)
    return frame, layout


class AnalyticsDashboardWidget(QDialog):
    def __init__(self, library: Library, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._library = library
        self.setWindowTitle("Watch Analytics")
        # A box well inside the window (and so the screen), opened in the middle of it.
        host = parent.window() if parent is not None else None
        bounds = host.size() if host is not None else QSize(1280, 800)
        self.resize(min(720, int(bounds.width() * 0.6)), min(780, int(bounds.height() * 0.72)))
        self.setStyleSheet(
            f"#analyticsCard {{ background: {theme.SURFACE}; border: 1px solid {theme.BORDER}; border-radius: 12px; }}"
            f"#bigNumber {{ color: {theme.TEXT}; font-size: 44px; font-weight: 700; }}"
            f"#smallNumber {{ color: {theme.TEXT}; font-size: 22px; font-weight: 600; }}"
        )

        title = QLabel("Watch Analytics")
        title.setObjectName("dialogTitle")
        self._note = QLabel()
        self._note.setObjectName("hint")
        self._note.setWordWrap(True)

        total, total_layout = _card("TOTAL HOURS WATCHED")
        self._total = QLabel()
        self._total.setObjectName("bigNumber")
        self._breakdown = QLabel()
        self._breakdown.setObjectName("hint")
        total_layout.addWidget(self._total)
        total_layout.addWidget(self._breakdown)

        completion, completion_layout = _card("COMPLETION RATE")
        row = QHBoxLayout()
        self._rate = QLabel()
        self._rate.setObjectName("smallNumber")
        self._rate_detail = QLabel()
        self._rate_detail.setObjectName("hint")
        row.addWidget(self._rate)
        row.addWidget(self._rate_detail, 1)
        completion_layout.addLayout(row)

        weekly, weekly_layout = _card("HOURS PER WEEK")
        self._weekly = BarChart()
        weekly_layout.addWidget(self._weekly)

        categories, categories_layout = _card("WHAT YOU WATCH")
        self._categories = BarChart()
        categories_layout.addWidget(self._categories)

        shows, shows_layout = _card("MOST WATCHED SHOWS")
        self._shows = BarChart()
        shows_layout.addWidget(self._shows)

        close = QPushButton("Close")
        close.setDefault(True)
        close.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(close)

        body = QWidget()
        body.setObjectName("analyticsBody")
        body.setStyleSheet("#analyticsBody { background: transparent; }")
        content = QVBoxLayout(body)
        content.setContentsMargins(0, 0, 12, 0)
        content.setSpacing(12)
        for card in (total, completion, weekly, categories, shows):
            content.addWidget(card)
        content.addWidget(self._note)
        content.addStretch(1)
        scroll = QScrollArea()  # the wheel and touchpad scroll it as well as the bar
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.viewport().setStyleSheet("background: transparent;")
        scroll.setWidget(body)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(title)
        layout.addWidget(scroll, 1)
        layout.addLayout(buttons)
        self.refresh()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        host = self.parentWidget().window() if self.parentWidget() is not None else None
        if host is not None:
            self.move(host.frameGeometry().center() - self.rect().center())

    def refresh(self) -> None:
        data: Analytics = compute(self._library.db)
        self._total.setText(format_hours(data.total_seconds))
        self._breakdown.setText(
            f"{format_hours(data.week_seconds)} in the last 7 days  ·  "
            f"{format_hours(data.month_seconds)} in the last 30 days"
        )
        self._rate.setText(f"{data.completion_rate:.0%}")
        self._rate_detail.setText(f"{data.completed} of {data.started} started titles watched to the end")
        self._weekly.set_rows(data.weekly)
        self._categories.set_rows(data.categories)
        self._shows.set_rows(data.top_shows, "No TV shows watched yet")
        self._note.setText(
            "Chrysa keeps the last position of each title, not a log of sessions: time is how far you got in "
            "each title, counted in the week you last watched it. Videos have no genre in the library, so "
            "they are grouped by type."
        )
