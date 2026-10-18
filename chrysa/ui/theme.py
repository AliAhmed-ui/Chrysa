"""Colours, fonts and the application-wide style sheet, driven by the active theme (see themes.py)."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPalette, QPen, QPixmap, QPolygonF
from PyQt6.QtWidgets import QApplication

from .theme_manager import ThemeManager
from .themes import Palette

SIDE_MARGIN = 32  # left/right page gutter
SIDEBAR_WIDTH = 236

_FONT_CANDIDATES = ("SF Pro Display", "SF Pro Text", "Inter", "Ubuntu Sans", "Ubuntu", "Noto Sans", "Cantarell", "DejaVu Sans")


def mix(first: str, second: str, amount: float) -> str:
    """``first`` blended towards ``second`` by *amount* (0..1), as ``#rrggbb``."""
    a, b = QColor(first), QColor(second)
    channels = (
        round(x + (y - x) * amount)
        for x, y in ((a.red(), b.red()), (a.green(), b.green()), (a.blue(), b.blue()))
    )
    return "#{:02x}{:02x}{:02x}".format(*channels)


def rgba(colour: str, alpha: float) -> str:
    """A CSS ``rgba()`` string (Qt reads 8-digit hex as #AARRGGBB, so spell it out)."""
    c = QColor(colour)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})"


def _luminance(colour: str) -> float:
    c = QColor(colour)

    def linear(value: int) -> float:
        v = value / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    return 0.2126 * linear(c.red()) + 0.7152 * linear(c.green()) + 0.0722 * linear(c.blue())


def contrast_ratio(first: str, second: str) -> float:
    """WCAG contrast ratio between two colours (1..21)."""
    high, low = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


# The active palette. Painting code reads these as ``theme.X`` at paint time, so
# rebinding them (see _install) is enough to recolour custom-drawn widgets.
BG = SURFACE = SIDEBAR = SURFACE_RAISED = SURFACE_HOVER = BORDER = ""
TEXT = TEXT_SOFT = TEXT_DIM = TEXT_MUTED = ""
ACCENT = ACCENT_ALT = ACCENT_HOVER = ACCENT_PRESSED = ON_ACCENT = ""
ACCENT_SOFT = ALT_SOFT = ALT_RIM = ""
ICON_IDLE = ICON_DISABLED = ""
CARD_BORDER = "rgba(255, 255, 255, 0.06)"


def _install(palette: Palette) -> None:
    """Derive every working colour from the seven semantic ones and publish them."""
    global BG, SURFACE, SIDEBAR, SURFACE_RAISED, SURFACE_HOVER, BORDER
    global TEXT, TEXT_SOFT, TEXT_DIM, TEXT_MUTED
    global ACCENT, ACCENT_ALT, ACCENT_HOVER, ACCENT_PRESSED, ON_ACCENT
    global ACCENT_SOFT, ALT_SOFT, ALT_RIM, ICON_IDLE, ICON_DISABLED

    BG = palette["bg_base"]
    SURFACE = SIDEBAR = palette["bg_surface"]
    BORDER = palette["border"]
    TEXT = palette["text_primary"]
    TEXT_MUTED = palette["text_muted"]
    ACCENT = palette["accent"]
    ACCENT_ALT = palette["accent_alt"]

    SURFACE_RAISED = mix(SURFACE, TEXT, 0.05)
    SURFACE_HOVER = mix(SURFACE, TEXT, 0.10)
    TEXT_SOFT = mix(TEXT, TEXT_MUTED, 0.18)
    TEXT_DIM = mix(TEXT, TEXT_MUTED, 0.55)
    ACCENT_HOVER = mix(ACCENT, "#ffffff", 0.18)
    ACCENT_PRESSED = mix(ACCENT, "#000000", 0.18)
    # Whichever of the page background or white reads better on a solid accent fill.
    ON_ACCENT = BG if contrast_ratio(BG, ACCENT) >= contrast_ratio("#ffffff", ACCENT) else "#ffffff"
    ACCENT_SOFT = rgba(ACCENT, 0.18)
    ALT_SOFT = rgba(ACCENT_ALT, 0.12)
    ALT_RIM = rgba(ACCENT_ALT, 0.45)
    ICON_IDLE = TEXT_DIM
    ICON_DISABLED = mix(TEXT_MUTED, BG, 0.5)


# The one ThemeManager the app uses. Its first listener updates the module colours
# above, so later listeners (the UI) always see them already switched.
SETTING_KEY = "ui.theme"  # where the choice is stored in the library's settings table
manager = ThemeManager()
manager.subscribe(_install)
_install(manager.get_current_theme())


def style_sheet() -> str:
    """The application style sheet for the active palette."""
    return f"""
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
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 3px; min-height: 40px; }}
QScrollBar::handle:vertical:hover {{ background: {TEXT_MUTED}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {BORDER}; border-radius: 3px; min-width: 40px; }}
QScrollBar::handle:horizontal:hover {{ background: {TEXT_MUTED}; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{
    background: none; border: none; width: 0; height: 0;
}}

/* ---- sidebar ---- */
#sidebar {{
    background: {SIDEBAR};
    border-right: 1px solid {BORDER};
}}
#brand {{ color: {TEXT}; font-size: 17px; font-weight: 600; letter-spacing: 6px; }}
#navButton {{
    background: transparent; border: 1px solid transparent; border-radius: 9px; color: {TEXT_SOFT};
    font-size: 14px; font-weight: 500; padding: 8px 10px; text-align: left;
}}
#navButton:hover {{ background: {ALT_SOFT}; }}
#navButton:checked {{
    background: {ALT_SOFT}; border: 1px solid {ALT_RIM};
    color: {TEXT}; font-weight: 600;
}}
#navCount {{
    background: {BG}; border: 1px solid {BORDER}; color: {TEXT_SOFT}; border-radius: 9px;
    font-size: 11px; font-weight: 500; padding: 1px 7px;
}}
#groupLabel {{ color: {TEXT_MUTED}; font-size: 11px; font-weight: 600; letter-spacing: 1px; }}
#searchBox {{
    background: {BG}; border: 1px solid {BORDER}; border-radius: 10px;
    padding: 8px 12px 8px 6px; font-size: 13px; color: {TEXT};
    selection-background-color: {ACCENT}; selection-color: {ON_ACCENT};
}}
#searchBox:hover {{ border: 1px solid {TEXT_MUTED}; }}
#searchBox:focus {{ border: 1px solid {ACCENT_ALT}; }}
#statusLabel {{ color: {TEXT_MUTED}; font-size: 11px; }}
QPushButton#sidebarAction {{
    background: transparent; border: none; border-radius: 8px; color: {TEXT_DIM};
    font-size: 13px; font-weight: 500; padding: 7px 10px; text-align: left;
}}
QPushButton#sidebarAction:hover {{ background: {ALT_SOFT}; color: {TEXT}; }}
QPushButton#sidebarAction:disabled {{ color: {TEXT_MUTED}; background: transparent; }}
#folderPath {{ color: {TEXT_MUTED}; font-size: 11px; }}
#activityLog {{
    background: {BG}; border: 1px solid {BORDER}; border-radius: 8px;
    font-family: monospace; font-size: 11px; color: {TEXT_MUTED}; padding: 6px;
}}

/* ---- buttons ---- */
QPushButton {{
    background: {SURFACE_HOVER}; border: 1px solid {BORDER}; border-radius: 8px;
    padding: 8px 16px; font-size: 14px; font-weight: 600; color: {TEXT};
}}
QPushButton:hover {{ border-color: {ACCENT_ALT}; }}
QPushButton:pressed {{ background: {SURFACE_RAISED}; }}
QPushButton:disabled {{ color: {TEXT_MUTED}; background: {SURFACE}; }}
QPushButton#primary {{ background: {ACCENT}; color: {ON_ACCENT}; border: none; border-radius: 20px; font-weight: 600; padding: 8px 24px; }}
QPushButton#primary:hover {{ background: {ACCENT_HOVER}; }}
QPushButton#primary:pressed {{ background: {ACCENT_PRESSED}; }}
QPushButton#primary:disabled {{ background: {BORDER}; color: {TEXT_MUTED}; }}
QPushButton#ghost {{
    background: {rgba(BG, 0.55)}; border: 1px solid {rgba(TEXT, 0.25)}; border-radius: 24px;
    color: {TEXT}; font-size: 15px; font-weight: 600; padding: 0 28px;
}}
QPushButton#ghost:hover {{ background: {rgba(BG, 0.75)}; border-color: {ACCENT_ALT}; }}
QPushButton#ghost:pressed {{ background: {BG}; }}
QPushButton#outline {{
    background: {SURFACE}; border: 1px solid {BORDER};
    border-radius: 16px; color: {TEXT_SOFT}; padding: 7px 14px;
}}
QPushButton#outline:hover {{ border-color: {ACCENT_ALT}; color: {TEXT}; background: {SURFACE_RAISED}; }}
QToolButton {{ background: transparent; border: none; border-radius: 8px; padding: 6px; }}
QToolButton:hover {{ background: {ALT_SOFT}; }}
QToolButton:pressed {{ background: {rgba(ACCENT_ALT, 0.22)}; }}
QToolButton::menu-indicator {{ image: none; }}
QToolButton#textTool {{ color: {TEXT_SOFT}; font-size: 13px; font-weight: 600; padding: 6px 10px; }}
QToolButton#rowArrow {{
    background: {rgba(BG, 0.72)}; border: 1px solid {BORDER}; border-radius: 12px;
}}
QToolButton#rowArrow:hover {{ background: {SURFACE_RAISED}; border-color: {ACCENT_ALT}; }}
QToolButton#backRound {{
    background: {rgba(BG, 0.72)}; border: 1px solid {BORDER}; border-radius: 22px;
}}
QToolButton#backRound:hover {{ background: {SURFACE_RAISED}; border-color: {ACCENT_ALT}; }}

/* ---- headings ---- */
#rowTitle {{ color: {TEXT}; font-size: 20px; font-weight: 700; }}
#pageTitle {{ color: {TEXT}; font-size: 28px; font-weight: 700; }}
#sectionTitle {{ color: {TEXT}; font-size: 22px; font-weight: 700; }}
#hint {{ color: {TEXT_MUTED}; font-size: 14px; }}
#emptyTitle {{ color: {TEXT_SOFT}; font-size: 24px; font-weight: 700; }}

/* ---- season selector and menus ---- */
QComboBox {{
    background: {SURFACE_RAISED}; border: 1px solid {BORDER}; border-radius: 8px;
    padding: 8px 34px 8px 14px; font-size: 15px; font-weight: 700; min-width: 120px;
    combobox-popup: 0;  /* drop the list below the box instead of over it */
}}
QComboBox:hover {{ border-color: {ACCENT_ALT}; }}
QComboBox::drop-down {{ border: none; width: 30px; }}
QComboBox QAbstractItemView {{
    background: {SURFACE_RAISED}; border: 1px solid {BORDER}; outline: none;
    selection-background-color: {ACCENT}; selection-color: {ON_ACCENT}; padding: 4px 0;
}}
QComboBox QAbstractItemView::item {{ padding: 8px 14px; min-height: 22px; color: {TEXT_SOFT}; }}
QComboBox QAbstractItemView::item:hover, QComboBox QAbstractItemView::item:selected {{
    background: {ACCENT}; color: {ON_ACCENT};
}}
QMenu {{ background: {SURFACE_RAISED}; border: 1px solid {BORDER}; border-radius: 8px; padding: 6px 4px; }}
QMenu::item {{ padding: 7px 28px 7px 24px; border-radius: 5px; font-size: 13px; color: {TEXT_SOFT}; }}
QMenu::item:selected {{ background: {ACCENT}; color: {ON_ACCENT}; }}
QMenu::item:disabled {{ color: {TEXT_MUTED}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 6px 0; }}
QMenu::indicator {{ width: 14px; height: 14px; left: 8px; }}

/* ---- dialogs ---- */
QListWidget {{
    background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 8px; font-size: 13px;
}}
QListWidget::item {{ padding: 10px 12px; border-bottom: 1px solid {BORDER}; }}
QListWidget::item:selected {{ background: {ALT_SOFT}; color: {TEXT}; }}
QListWidget#downloadList::item {{ padding: 0; }}
QWidget#downloadRow {{ background: transparent; }}
QLabel#downloadTitle {{ font-size: 14px; font-weight: 600; color: {TEXT}; background: transparent; }}
QLabel#downloadSpeed {{ font-size: 12px; font-weight: 600; color: {ACCENT_ALT}; background: transparent; }}
QProgressBar#downloadBar {{
    background: {SURFACE_RAISED}; border: 1px solid {BORDER}; border-radius: 9px;
    color: {TEXT}; font-size: 11px; font-weight: 600; text-align: center;
}}
QProgressBar#downloadBar::chunk {{ background: {ACCENT}; border-radius: 8px; }}
QLabel#dialogTitle {{ font-size: 20px; font-weight: 700; }}
QMessageBox {{ background: {SURFACE}; }}
QMessageBox QLabel {{ color: {TEXT_SOFT}; font-size: 14px; }}

/* ---- player ---- */
#playerBar {{ background: {BG}; }}
#playerTitle {{ color: {TEXT}; font-size: 17px; font-weight: 600; }}
#playerNote {{ color: {TEXT_DIM}; font-size: 13px; }}
#playerTime {{ color: {TEXT_SOFT}; font-size: 13px; font-weight: 600; }}
#playerHud {{
    background: {rgba(BG, 0.78)}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 22px;
    font-size: 22px; font-weight: 600; padding: 10px 26px;
}}
"""


def font_family() -> str:
    """The first installed font from the preferred list (falls back to the system default)."""
    installed = set(QFontDatabase.families())
    for family in _FONT_CANDIDATES:
        if family in installed:
            return family
    return QApplication.font().family()


def apply(app: QApplication) -> None:
    """Install the font, palette and style sheet on *app* and keep them in step with the theme."""
    app.setStyle("Fusion")
    font = QFont(font_family())
    font.setPixelSize(14)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    app.setFont(font)
    _restyle(app)
    manager.subscribe(lambda _palette, app=app: _restyle(app))


def _restyle(app: QApplication) -> None:
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
        QPalette.ColorRole.HighlightedText: ON_ACCENT,
        QPalette.ColorRole.PlaceholderText: TEXT_MUTED,
        QPalette.ColorRole.Link: ACCENT_ALT,
    }.items():
        palette.setColor(role, QColor(colour))
    app.setPalette(palette)
    app.setStyleSheet(style_sheet())


def ui_font(pixel_size: int, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    """A font for custom-painted widgets."""
    font = QFont(QApplication.font())
    font.setPixelSize(pixel_size)
    font.setWeight(weight)
    return font


def app_icon() -> QIcon:
    """The Chrysa icon: the PNG shipped in assets/ if present, else a painted stand-in."""
    candidates = (
        Path(__file__).resolve().parents[2] / "assets" / "chrysa.png",
        Path.home() / ".local/share/icons/hicolor/512x512/apps/chrysa.png",
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
