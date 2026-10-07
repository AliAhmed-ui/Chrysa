"""Colours, fonts and the application-wide style sheet (matte dark, Apple blue accents)."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPalette, QPen, QPixmap, QPolygonF
from PyQt6.QtWidgets import QApplication

BG = "#0B0C0E"  # obsidian; pages add only a faint graphite sheen at the top (pages.AmbientPage)
SURFACE = "#16181D"
SIDEBAR = "#111215"
SURFACE_RAISED = "#22252d"
SURFACE_HOVER = "#2a2d36"
BORDER = "#252830"
CARD_BORDER = "rgba(255, 255, 255, 0.06)"  # Qt reads 8-digit hex as #AARRGGBB, so spell it out
TEXT = "#F5F5F7"
TEXT_SOFT = "#e3e3e8"
TEXT_DIM = "#a1a1a6"
TEXT_MUTED = "#86868B"
ACCENT = "#0A84FF"
ACCENT_HOVER = "#3d9bff"
ACCENT_PRESSED = "#0670dd"
ACCENT_SOFT = "rgba(10, 132, 255, 0.18)"

SIDE_MARGIN = 32  # left/right page gutter
SIDEBAR_WIDTH = 236

_FONT_CANDIDATES = ("SF Pro Display", "SF Pro Text", "Inter", "Ubuntu Sans", "Ubuntu", "Noto Sans", "Cantarell", "DejaVu Sans")

_STYLE = f"""
* {{ outline: none; }}
QWidget {{ color: {TEXT}; }}
QMainWindow, QDialog, #browser {{ background: {BG}; }}
QToolTip {{
    background: {SURFACE_RAISED}; color: {TEXT_SOFT}; border: 1px solid {BORDER};
    padding: 5px 8px; font-size: 12px;
}}

/* ---- scrolling ---- */
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #3a3d46; border-radius: 3px; min-height: 40px; }}
QScrollBar::handle:vertical:hover {{ background: #5a5e6a; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #3a3d46; border-radius: 3px; min-width: 40px; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{
    background: none; border: none; width: 0; height: 0;
}}

/* ---- sidebar ---- */
#sidebar {{
    background: {SIDEBAR};
    border-right: 1px solid rgba(255, 255, 255, 0.06);
}}
#brand {{ color: {TEXT}; font-size: 17px; font-weight: 600; letter-spacing: 6px; }}
#navButton {{
    background: transparent; border: 1px solid transparent; border-radius: 9px; color: {TEXT_SOFT};
    font-size: 14px; font-weight: 500; padding: 8px 10px; text-align: left;
}}
#navButton:hover {{ background: rgba(255, 255, 255, 0.05); }}
#navButton:checked {{
    background: rgba(255, 255, 255, 0.08); border: 1px solid rgba(255, 255, 255, 0.12);
    color: #FFFFFF; font-weight: 600;
}}
#navCount {{
    background: rgba(0, 0, 0, 0.65); border: 1px solid rgba(255, 255, 255, 0.14); color: #E5E7EB; border-radius: 9px;
    font-size: 11px; font-weight: 500; padding: 1px 7px;
}}
#groupLabel {{ color: {TEXT_MUTED}; font-size: 11px; font-weight: 600; letter-spacing: 1px; }}
#searchBox {{
    background: rgba(255, 255, 255, 0.05); border: 1px solid rgba(255, 255, 255, 0.08); border-radius: 10px;
    padding: 8px 12px 8px 6px; font-size: 13px; color: #FFFFFF;
    selection-background-color: {ACCENT};
}}
#searchBox:hover {{ background: rgba(255, 255, 255, 0.07); }}
#searchBox:focus {{ background: rgba(255, 255, 255, 0.08); border: 1px solid rgba(255, 255, 255, 0.2); }}
#statusLabel {{ color: {TEXT_MUTED}; font-size: 11px; }}
QPushButton#sidebarAction {{
    background: transparent; border: none; border-radius: 8px; color: {TEXT_DIM};
    font-size: 13px; font-weight: 500; padding: 7px 10px; text-align: left;
}}
QPushButton#sidebarAction:hover {{ background: rgba(255, 255, 255, 0.06); color: {TEXT}; }}
QPushButton#sidebarAction:disabled {{ color: {TEXT_MUTED}; background: transparent; }}
#folderPath {{ color: {TEXT_MUTED}; font-size: 11px; }}

/* ---- buttons ---- */
QPushButton {{
    background: {SURFACE_HOVER}; border: 1px solid {CARD_BORDER}; border-radius: 8px;
    padding: 8px 16px; font-size: 14px; font-weight: 600; color: {TEXT};
}}
QPushButton:hover {{ background: #343842; }}
QPushButton:pressed {{ background: #2f323b; }}
QPushButton:disabled {{ color: {TEXT_MUTED}; background: #1d1f26; }}
QPushButton#primary {{ background: #FFFFFF; color: #000000; border: none; border-radius: 20px; font-weight: 600; padding: 8px 24px; }}
QPushButton#primary:hover {{ background: #ececf1; }}
QPushButton#primary:pressed {{ background: #d6d6dc; }}
QPushButton#primary:disabled {{ background: rgba(255, 255, 255, 0.2); color: rgba(255, 255, 255, 0.5); }}
QPushButton#ghost {{ background: rgba(255, 255, 255, 0.12); border: 1px solid rgba(255, 255, 255, 0.15); border-radius: 20px; }}
QPushButton#ghost:hover {{ background: rgba(255, 255, 255, 0.2); border-color: rgba(255, 255, 255, 0.25); }}
QPushButton#outline {{
    background: rgba(255, 255, 255, 0.06); border: 1px solid rgba(255, 255, 255, 0.14);
    border-radius: 16px; color: {TEXT_SOFT}; padding: 7px 14px;
}}
QPushButton#outline:hover {{ border-color: rgba(255, 255, 255, 0.3); color: {TEXT}; background: rgba(255,255,255,0.12); }}
QToolButton {{ background: transparent; border: none; border-radius: 8px; padding: 6px; }}
QToolButton:hover {{ background: rgba(255, 255, 255, 0.12); }}
QToolButton:pressed {{ background: rgba(255, 255, 255, 0.2); }}
QToolButton::menu-indicator {{ image: none; }}
QToolButton#textTool {{ color: {TEXT_SOFT}; font-size: 13px; font-weight: 600; padding: 6px 10px; }}

/* ---- headings ---- */
#rowTitle {{ color: {TEXT}; font-size: 20px; font-weight: 700; }}
#pageTitle {{ color: {TEXT}; font-size: 28px; font-weight: 700; }}
#sectionTitle {{ color: {TEXT}; font-size: 22px; font-weight: 700; }}
#hint {{ color: {TEXT_MUTED}; font-size: 14px; }}
#emptyTitle {{ color: {TEXT_SOFT}; font-size: 24px; font-weight: 700; }}

/* ---- season selector and menus ---- */
QComboBox {{
    background: {SURFACE_RAISED}; border: 1px solid {CARD_BORDER}; border-radius: 8px;
    padding: 8px 34px 8px 14px; font-size: 15px; font-weight: 700; min-width: 120px;
    combobox-popup: 0;  /* drop the list below the box instead of over it */
}}
QComboBox:hover {{ border-color: rgba(255, 255, 255, 0.28); }}
QComboBox::drop-down {{ border: none; width: 30px; }}
QComboBox QAbstractItemView {{
    background: {SURFACE_RAISED}; border: 1px solid {BORDER}; outline: none;
    selection-background-color: {ACCENT}; padding: 4px 0;
}}
QComboBox QAbstractItemView::item {{ padding: 8px 14px; min-height: 22px; color: {TEXT_SOFT}; }}
QComboBox QAbstractItemView::item:hover, QComboBox QAbstractItemView::item:selected {{
    background: {ACCENT}; color: {TEXT};
}}
QMenu {{ background: {SURFACE_RAISED}; border: 1px solid {BORDER}; border-radius: 8px; padding: 6px 4px; }}
QMenu::item {{ padding: 7px 28px 7px 24px; border-radius: 5px; font-size: 13px; color: {TEXT_SOFT}; }}
QMenu::item:selected {{ background: {ACCENT}; color: {TEXT}; }}
QMenu::item:disabled {{ color: {TEXT_MUTED}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 6px 0; }}
QMenu::indicator {{ width: 14px; height: 14px; left: 8px; }}

/* ---- dialogs ---- */
QListWidget {{
    background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 8px; font-size: 13px;
}}
QListWidget::item {{ padding: 10px 12px; border-bottom: 1px solid {BORDER}; }}
QListWidget::item:selected {{ background: {ACCENT_SOFT}; color: {TEXT}; }}
QLabel#dialogTitle {{ font-size: 20px; font-weight: 700; }}
QMessageBox {{ background: {SURFACE}; }}
QMessageBox QLabel {{ color: {TEXT_SOFT}; font-size: 14px; }}

/* ---- player ---- */
#playerBar {{ background: #0B0C0E; }}
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
        QPalette.ColorRole.Base: SURFACE,
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
