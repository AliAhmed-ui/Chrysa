"""Colours, fonts and the application-wide style sheet (dark, red accents)."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPalette, QPen, QPixmap, QPolygonF
from PyQt6.QtWidgets import QApplication

BG = "#141414"
SURFACE = "#181818"
SURFACE_RAISED = "#232323"
SURFACE_HOVER = "#2f2f2f"
BORDER = "#3d3d3d"
TEXT = "#ffffff"
TEXT_SOFT = "#e5e5e5"
TEXT_DIM = "#b3b3b3"
TEXT_MUTED = "#808080"
ACCENT = "#E50914"
ACCENT_HOVER = "#f6121d"
ACCENT_PRESSED = "#b20710"

SIDE_MARGIN = 48  # left/right page gutter
TOPBAR_HEIGHT = 68

_FONT_CANDIDATES = ("Ubuntu Sans", "Ubuntu", "Inter", "Noto Sans", "Cantarell", "DejaVu Sans")

_STYLE = f"""
* {{ outline: none; }}
QWidget {{ color: {TEXT}; }}
QMainWindow, QDialog, #page, #browser {{ background: {BG}; }}
QToolTip {{
    background: {SURFACE_RAISED}; color: {TEXT_SOFT}; border: 1px solid {BORDER};
    padding: 5px 8px; font-size: 12px;
}}

/* ---- scrolling ---- */
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #4a4a4a; border-radius: 3px; min-height: 40px; }}
QScrollBar::handle:vertical:hover {{ background: #6a6a6a; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #4a4a4a; border-radius: 3px; min-width: 40px; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{
    background: none; border: none; width: 0; height: 0;
}}

/* ---- top bar ---- */
#topBar {{ background: {BG}; border-bottom: 1px solid #1f1f1f; }}
#logo {{ color: {ACCENT}; font-size: 27px; font-weight: 900; letter-spacing: 3px; }}
#navButton {{
    background: transparent; border: none; color: {TEXT_DIM};
    font-size: 14px; font-weight: 500; padding: 8px 10px;
}}
#navButton:hover {{ color: {TEXT_SOFT}; }}
#navButton:checked {{ color: {TEXT}; font-weight: 700; }}
#searchBox {{
    background: #0d0d0d; border: 1px solid {BORDER}; border-radius: 4px;
    padding: 7px 10px 7px 6px; font-size: 14px; color: {TEXT};
    selection-background-color: {ACCENT};
}}
#searchBox:focus {{ border-color: {TEXT_SOFT}; }}
#statusLabel {{ color: {TEXT_MUTED}; font-size: 12px; }}

/* ---- buttons ---- */
QPushButton {{
    background: {SURFACE_HOVER}; border: none; border-radius: 4px;
    padding: 8px 16px; font-size: 14px; font-weight: 600; color: {TEXT};
}}
QPushButton:hover {{ background: #454545; }}
QPushButton:pressed {{ background: #3a3a3a; }}
QPushButton:disabled {{ color: {TEXT_MUTED}; background: #222222; }}
QPushButton#primary {{ background: {ACCENT}; color: {TEXT}; }}
QPushButton#primary:hover {{ background: {ACCENT_HOVER}; }}
QPushButton#primary:pressed {{ background: {ACCENT_PRESSED}; }}
QPushButton#ghost {{ background: rgba(109, 109, 110, 0.55); }}
QPushButton#ghost:hover {{ background: rgba(109, 109, 110, 0.8); }}
QPushButton#outline {{
    background: transparent; border: 1px solid #6d6d6d; color: {TEXT_SOFT}; padding: 7px 14px;
}}
QPushButton#outline:hover {{ border-color: {TEXT}; color: {TEXT}; background: rgba(255,255,255,0.06); }}
QToolButton {{ background: transparent; border: none; border-radius: 4px; padding: 6px; }}
QToolButton:hover {{ background: rgba(255, 255, 255, 0.12); }}
QToolButton:pressed {{ background: rgba(255, 255, 255, 0.2); }}
QToolButton::menu-indicator {{ image: none; }}
QToolButton#textTool {{ color: {TEXT_SOFT}; font-size: 13px; font-weight: 600; padding: 6px 10px; }}

/* ---- headings ---- */
#rowTitle {{ color: {TEXT_SOFT}; font-size: 20px; font-weight: 700; }}
#pageTitle {{ color: {TEXT}; font-size: 30px; font-weight: 800; }}
#sectionTitle {{ color: {TEXT}; font-size: 22px; font-weight: 700; }}
#hint {{ color: {TEXT_MUTED}; font-size: 14px; }}
#emptyTitle {{ color: {TEXT_SOFT}; font-size: 24px; font-weight: 700; }}

/* ---- season selector and menus ---- */
QComboBox {{
    background: #242424; border: 1px solid #4d4d4d; border-radius: 4px;
    padding: 8px 34px 8px 14px; font-size: 15px; font-weight: 700; min-width: 120px;
    combobox-popup: 0;  /* drop the list below the box instead of over it */
}}
QComboBox:hover {{ border-color: {TEXT_SOFT}; }}
QComboBox::drop-down {{ border: none; width: 30px; }}
QComboBox QAbstractItemView {{
    background: #242424; border: 1px solid #4d4d4d; outline: none;
    selection-background-color: #424242; padding: 4px 0;
}}
QComboBox QAbstractItemView::item {{ padding: 8px 14px; min-height: 22px; color: {TEXT_SOFT}; }}
QComboBox QAbstractItemView::item:hover, QComboBox QAbstractItemView::item:selected {{
    background: #424242; color: {TEXT};
}}
QMenu {{ background: #1c1c1c; border: 1px solid {BORDER}; padding: 6px 0; }}
QMenu::item {{ padding: 8px 28px 8px 26px; font-size: 13px; color: {TEXT_SOFT}; }}
QMenu::item:selected {{ background: #3a3a3a; color: {TEXT}; }}
QMenu::item:disabled {{ color: {TEXT_MUTED}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 6px 0; }}
QMenu::indicator {{ width: 14px; height: 14px; left: 8px; }}

/* ---- dialogs ---- */
QListWidget {{
    background: #0f0f0f; border: 1px solid {BORDER}; border-radius: 4px; font-size: 13px;
}}
QListWidget::item {{ padding: 10px 12px; border-bottom: 1px solid #1f1f1f; }}
QListWidget::item:selected {{ background: #333333; color: {TEXT}; }}
QLabel#dialogTitle {{ font-size: 20px; font-weight: 700; }}
QMessageBox {{ background: {SURFACE}; }}
QMessageBox QLabel {{ color: {TEXT_SOFT}; font-size: 14px; }}

/* ---- player ---- */
#playerBar {{ background: #0c0c0c; }}
#playerTitle {{ color: {TEXT}; font-size: 17px; font-weight: 600; }}
#playerNote {{ color: {TEXT_DIM}; font-size: 13px; }}
#playerTime {{ color: {TEXT_SOFT}; font-size: 13px; font-weight: 600; }}
"""


def font_family() -> str:
    """The first installed font from the preferred list (falls back to the system default)."""
    installed = set(QFontDatabase.families())
    for family in _FONT_CANDIDATES:
        if family in installed:
            return family
    return QApplication.font().family()


def apply(app: QApplication) -> None:
    """Install the dark palette, font and style sheet on *app*."""
    app.setStyle("Fusion")
    font = QFont(font_family())
    font.setPixelSize(14)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    app.setFont(font)

    palette = QPalette()
    for role, colour in {
        QPalette.ColorRole.Window: BG,
        QPalette.ColorRole.WindowText: TEXT,
        QPalette.ColorRole.Base: "#0f0f0f",
        QPalette.ColorRole.AlternateBase: SURFACE,
        QPalette.ColorRole.Text: TEXT,
        QPalette.ColorRole.Button: SURFACE_RAISED,
        QPalette.ColorRole.ButtonText: TEXT,
        QPalette.ColorRole.ToolTipBase: SURFACE_RAISED,
        QPalette.ColorRole.ToolTipText: TEXT_SOFT,
        QPalette.ColorRole.Highlight: ACCENT,
        QPalette.ColorRole.HighlightedText: TEXT,
        QPalette.ColorRole.PlaceholderText: TEXT_MUTED,
        QPalette.ColorRole.Link: TEXT_SOFT,
    }.items():
        palette.setColor(role, QColor(colour))
    app.setPalette(palette)
    app.setStyleSheet(_STYLE)


def ui_font(pixel_size: int, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    """A font for custom-painted widgets."""
    font = QFont(QApplication.font())
    font.setPixelSize(pixel_size)
    font.setWeight(weight)
    return font


def app_icon() -> QIcon:
    """The Theia icon: the SVG written by run.sh if present, else a painted stand-in."""
    candidates = (
        Path(__file__).resolve().parents[2] / "assets" / "theia.svg",
        Path.home() / ".local/share/icons/hicolor/scalable/apps/theia.svg",
    )
    for candidate in candidates:
        if candidate.is_file():
            icon = QIcon(str(candidate))
            if not icon.isNull():
                return icon
    icon = QIcon()
    for size in (32, 64, 128, 256):
        icon.addPixmap(_painted_icon(size))
    return icon


def _painted_icon(size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(BG))
    painter.drawRoundedRect(QRectF(0, 0, size, size), size * 0.22, size * 0.22)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(QColor(ACCENT), size * 0.07))
    painter.drawEllipse(QPointF(size / 2, size / 2), size * 0.30, size * 0.30)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(TEXT))
    painter.drawPolygon(
        QPolygonF([QPointF(size * 0.43, size * 0.34), QPointF(size * 0.43, size * 0.66), QPointF(size * 0.69, size * 0.5)])
    )
    painter.end()
    return pixmap
