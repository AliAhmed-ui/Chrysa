"""Design tokens (colour, type, spacing, motion) and the application style sheet for the Apple-style design.

The active palette comes from themes.py. Everything else a widget needs to look consistent lives
here: the type ramp, radii, spacing scale, motion durations and the reduced-motion switch."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PyQt6.QtCore import QEvent, QObject, QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPalette, QPen, QPixmap, QPolygonF
from PyQt6.QtWidgets import QAbstractButton, QApplication

from .theme_manager import ThemeManager
from .themes import Palette

SIDE_MARGIN = 32  # left/right page gutter
SIDEBAR_WIDTH = 232

# ---- spacing (4-point grid) and shape ----
SPACE = {"xs": 4, "s": 8, "m": 12, "l": 16, "xl": 24, "xxl": 32}
RADIUS_S, RADIUS_M, RADIUS_L = 6, 10, 14  # controls, cards, large surfaces

# ---- type ramp: role -> (pixel size, weight, tracking in % of the size) ----
# Large text is tightened, small text opened up slightly, as the system font does.
TYPE: dict[str, tuple[int, QFont.Weight, float]] = {
    "largeTitle": (30, QFont.Weight.Bold, -1.4),
    "title": (22, QFont.Weight.Bold, -1.0),
    "headline": (17, QFont.Weight.DemiBold, -0.4),
    "body": (14, QFont.Weight.Normal, 0.0),
    "bodyEmphasis": (14, QFont.Weight.DemiBold, 0.0),
    "callout": (13, QFont.Weight.Normal, 0.0),
    "footnote": (12, QFont.Weight.Normal, 0.2),
    "caption": (11, QFont.Weight.Medium, 0.4),
}

# ---- motion (milliseconds); every duration goes through ms() so reduced motion can shorten it ----
MOTION = {"instant": 90, "quick": 160, "standard": 240, "slow": 360}

_FONT_CANDIDATES = (
    "SF Pro Text", "SF Pro Display", "Inter", "Segoe UI Variable", "Segoe UI",
    "Ubuntu Sans", "Ubuntu", "Cantarell", "Noto Sans", "DejaVu Sans",
)

_reduce_motion: bool | None = None


def reduced_motion() -> bool:
    """True when the user asked the system (or CHRYSA_REDUCE_MOTION=1) for less animation."""
    global _reduce_motion
    if _reduce_motion is None:
        _reduce_motion = _detect_reduced_motion()
    return _reduce_motion


def set_reduced_motion(value: bool | None) -> None:
    """Override detection (None = detect again)."""
    global _reduce_motion
    _reduce_motion = value


def _detect_reduced_motion() -> bool:
    if os.environ.get("CHRYSA_REDUCE_MOTION", "") not in ("", "0"):
        return True
    try:
        if sys.platform.startswith("linux"):
            out = subprocess.run(
                ["gsettings", "get", "org.gnome.desktop.interface", "enable-animations"],
                capture_output=True, text=True, timeout=0.5,
            ).stdout.strip()
            return out == "false"
        if sys.platform == "win32":
            import ctypes

            flag = ctypes.c_int(1)
            SPI_GETCLIENTAREAANIMATION = 0x1042
            if ctypes.windll.user32.SystemParametersInfoW(SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(flag), 0):
                return flag.value == 0
    except (OSError, subprocess.SubprocessError, AttributeError):
        pass
    return False


def ms(duration: int | str) -> int:
    """A motion duration in ms: a MOTION key or a number. Reduced motion keeps only a short fade."""
    value = MOTION[duration] if isinstance(duration, str) else duration
    return min(value, 60) if reduced_motion() else value


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
HOVER = PRESSED = SELECTED = FOCUS_RING = SEPARATOR = SCRIM = ""
SUCCESS = WARNING = ERROR = ""
SCHEME = "dark"
FLAT = False
CARD_BORDER = "rgba(255, 255, 255, 0.06)"
MODE_BAR = "#000000"


def _install(palette: Palette) -> None:
    """Derive every working colour from the semantic ones and publish them."""
    global BG, SURFACE, SIDEBAR, SURFACE_RAISED, SURFACE_HOVER, BORDER
    global TEXT, TEXT_SOFT, TEXT_DIM, TEXT_MUTED
    global ACCENT, ACCENT_ALT, ACCENT_HOVER, ACCENT_PRESSED, ON_ACCENT
    global ACCENT_SOFT, ALT_SOFT, ALT_RIM, ICON_IDLE, ICON_DISABLED, CARD_BORDER, MODE_BAR
    global HOVER, PRESSED, SELECTED, FOCUS_RING, SEPARATOR, SCRIM
    global SUCCESS, WARNING, ERROR, SCHEME, FLAT

    SCHEME = palette["scheme"]
    FLAT = palette["flat"]
    dark = SCHEME == "dark"
    BG = palette["bg_base"]
    SURFACE = palette["bg_surface"]
    BORDER = palette["border"]
    TEXT = palette["text_primary"]
    TEXT_MUTED = palette["text_muted"]
    ACCENT = palette["accent"]
    ACCENT_ALT = palette["accent_alt"]
    # Flat themes get a sidebar a step below the page (the macOS source-list look);
    # the legacy themes keep the sidebar the same colour as their surface.
    SIDEBAR = (mix(BG, "#000000", 0.24 if dark else 0.0) if dark else mix(BG, "#000000", 0.035)) if FLAT else SURFACE

    SURFACE_RAISED = mix(SURFACE, TEXT, 0.05)
    SURFACE_HOVER = mix(SURFACE, TEXT, 0.10)
    TEXT_SOFT = mix(TEXT, TEXT_MUTED, 0.18)
    TEXT_DIM = mix(TEXT, TEXT_MUTED, 0.55)
    ACCENT_HOVER = mix(ACCENT, "#ffffff" if dark else "#000000", 0.14)
    ACCENT_PRESSED = mix(ACCENT, "#000000", 0.2)
    # Whichever of the page background or white reads better on a solid accent fill.
    ON_ACCENT = BG if contrast_ratio(BG, ACCENT) >= contrast_ratio("#ffffff", ACCENT) else "#ffffff"
    if FLAT:
        ON_ACCENT = "#ffffff" if contrast_ratio("#ffffff", ACCENT) >= 3.0 else BG
    ACCENT_SOFT = rgba(ACCENT, 0.18)
    ALT_SOFT = rgba(ACCENT_ALT, 0.12)
    ALT_RIM = rgba(ACCENT_ALT, 0.45)
    ICON_IDLE = TEXT_DIM
    ICON_DISABLED = mix(TEXT_MUTED, BG, 0.5)

    HOVER = rgba(TEXT, 0.06)
    PRESSED = rgba(TEXT, 0.11)
    SELECTED = rgba(TEXT, 0.10)
    FOCUS_RING = rgba(ACCENT, 0.85)
    SEPARATOR = rgba(TEXT, 0.10)
    SCRIM = "rgba(0, 0, 0, 0.45)"
    SUCCESS = "#30d158" if dark else "#248a3d"
    WARNING = "#ff9f0a" if dark else "#b25000"
    ERROR = "#ff453a" if dark else "#d70015"
    MODE_BAR = "#000000" if dark and FLAT else BG  # the strip across the top holding the mode control
    CARD_BORDER = "rgba(255, 255, 255, 0.07)" if dark else "rgba(0, 0, 0, 0.08)"


# The one ThemeManager the app uses. Its first listener updates the module colours
# above, so later listeners (the UI) always see them already switched.
SETTING_KEY = "ui.theme"  # where the choice is stored in the library's settings table
manager = ThemeManager()
manager.subscribe(_install)
_install(manager.get_current_theme())


def style_sheet() -> str:
    """The application style sheet for the active palette."""
    px = {role: spec[0] for role, spec in TYPE.items()}
    return f"""
* {{ outline: none; }}
QWidget {{ color: {TEXT}; }}
QMainWindow, QDialog, #browser {{ background: {BG}; }}
#modeBar {{ background: {MODE_BAR}; }}
QToolTip {{
    background: {SURFACE_RAISED}; color: {TEXT_SOFT}; border: 1px solid {SEPARATOR};
    border-radius: {RADIUS_S}px; padding: 5px 8px; font-size: {px['footnote']}px;
}}

/* ---- scrolling: thin overlay-style bars ---- */
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 12px; margin: 2px 2px 2px 0; }}
QScrollBar::handle:vertical {{ background: {rgba(TEXT, 0.22)}; border-radius: 4px; min-height: 40px; margin: 0 1px; }}
QScrollBar::handle:vertical:hover {{ background: {rgba(TEXT, 0.40)}; }}
QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 0 2px 2px 2px; }}
QScrollBar::handle:horizontal {{ background: {rgba(TEXT, 0.22)}; border-radius: 4px; min-width: 40px; margin: 1px 0; }}
QScrollBar::handle:horizontal:hover {{ background: {rgba(TEXT, 0.40)}; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{
    background: none; border: none; width: 0; height: 0;
}}

/* ---- sidebar: a source list, separated by a hairline ---- */
#sidebar {{ background: {SIDEBAR}; border-right: 1px solid {SEPARATOR}; }}
#brand {{ color: {TEXT}; font-size: {px['headline']}px; font-weight: 600; }}
#navButton {{
    background: transparent; border: none; border-radius: {RADIUS_S + 1}px; color: {TEXT};
    font-size: {px['body']}px; font-weight: 500; padding: 6px 10px; text-align: left;
}}
#navButton:hover {{ background: {HOVER}; }}
#navButton:pressed {{ background: {PRESSED}; }}
#navButton:checked {{ background: {SELECTED}; color: {TEXT}; font-weight: 600; }}
#navButton:focus {{ border: 2px solid {FOCUS_RING}; padding: 4px 8px; }}
#navCount {{ background: transparent; color: {TEXT_MUTED}; font-size: {px['footnote']}px; font-weight: 500; padding: 0 2px; }}
#groupLabel {{ color: {TEXT_MUTED}; font-size: {px['caption']}px; font-weight: 600; padding-left: 10px; }}
#searchBox {{
    background: {rgba(TEXT, 0.07)}; border: 1px solid transparent; border-radius: {RADIUS_S + 1}px;
    padding: 6px 10px 6px 4px; font-size: {px['callout']}px; color: {TEXT};
    selection-background-color: {ACCENT}; selection-color: {ON_ACCENT};
}}
#searchBox:hover {{ background: {rgba(TEXT, 0.10)}; }}
#searchBox:focus {{ background: {rgba(TEXT, 0.07)}; border: 2px solid {FOCUS_RING}; padding: 5px 9px 5px 3px; }}
#statusLabel {{ color: {TEXT_MUTED}; font-size: {px['footnote']}px; }}
QPushButton#sidebarAction {{
    background: transparent; border: none; border-radius: {RADIUS_S + 1}px; color: {TEXT_SOFT};
    font-size: {px['callout']}px; font-weight: 500; padding: 6px 10px; text-align: left;
}}
QPushButton#sidebarAction:hover {{ background: {HOVER}; color: {TEXT}; }}
QPushButton#sidebarAction:pressed {{ background: {PRESSED}; }}
QPushButton#sidebarAction:focus {{ border: 2px solid {FOCUS_RING}; padding: 4px 8px; }}
QPushButton#sidebarAction:disabled {{ color: {TEXT_MUTED}; background: transparent; }}
#folderPath {{ color: {TEXT_MUTED}; font-size: {px['footnote']}px; }}
#activityLog {{
    background: {rgba(TEXT, 0.05)}; border: none; border-radius: {RADIUS_S + 2}px;
    font-family: monospace; font-size: {px['caption']}px; color: {TEXT_MUTED}; padding: 6px;
}}

/* ---- buttons: secondary by default, one filled primary ---- */
QPushButton {{
    background: {SURFACE_HOVER if not FLAT else rgba(TEXT, 0.09)}; border: 1px solid transparent; border-radius: {RADIUS_S + 1}px;
    padding: 6px 16px; font-size: {px['body']}px; font-weight: 500; color: {TEXT};
}}
QPushButton:hover {{ background: {rgba(TEXT, 0.14)}; }}
QPushButton:pressed {{ background: {rgba(TEXT, 0.20)}; }}
QPushButton:focus {{ border: 2px solid {FOCUS_RING}; padding: 5px 15px; }}
QPushButton:disabled {{ color: {TEXT_MUTED}; background: {rgba(TEXT, 0.04)}; }}
QPushButton#primary {{
    background: {ACCENT}; color: {ON_ACCENT}; border: 1px solid transparent; border-radius: {RADIUS_S + 2}px;
    font-weight: 600; padding: 7px 20px;
}}
QPushButton#primary:hover {{ background: {ACCENT_HOVER}; }}
QPushButton#primary:pressed {{ background: {ACCENT_PRESSED}; }}
QPushButton#primary:focus {{ border: 2px solid {TEXT}; padding: 6px 19px; }}
QPushButton#primary:disabled {{ background: {rgba(TEXT, 0.10)}; color: {TEXT_MUTED}; }}
QPushButton#ghost {{
    background: {rgba('#000000', 0.40)}; border: 1px solid {rgba('#ffffff', 0.22)}; border-radius: {RADIUS_M}px;
    color: #ffffff; font-size: {px['body']}px; font-weight: 600; padding: 0 22px;
}}
QPushButton#ghost:hover {{ background: {rgba('#000000', 0.55)}; }}
QPushButton#ghost:pressed {{ background: {rgba('#000000', 0.70)}; }}
QPushButton#ghost:focus {{ border: 2px solid {FOCUS_RING}; }}
QPushButton#skipIntro {{
    background: {rgba('#ffffff', 0.94)}; border: 1px solid {rgba('#ffffff', 0.60)}; border-radius: 21px;
    color: #111114; font-size: {px['body']}px; font-weight: 700; letter-spacing: 0.3px; padding: 0 22px 0 24px;
}}
QPushButton#skipIntro:hover {{ background: {ACCENT}; border-color: {ACCENT}; color: {ON_ACCENT}; }}
QPushButton#skipIntro:pressed {{ background: {ACCENT_PRESSED}; border-color: {ACCENT_PRESSED}; color: {ON_ACCENT}; }}
QPushButton#skipIntro:focus {{ border: 2px solid {FOCUS_RING}; }}
QPushButton#outline {{
    background: transparent; border: 1px solid {SEPARATOR}; border-radius: {RADIUS_M}px;
    color: {TEXT_SOFT}; padding: 5px 14px;
}}
QPushButton#outline:hover {{ background: {HOVER}; color: {TEXT}; }}
QPushButton#outline:pressed {{ background: {PRESSED}; }}
QToolButton {{ background: transparent; border: none; border-radius: {RADIUS_S + 1}px; padding: 6px; }}
QToolButton:hover {{ background: {HOVER}; }}
QToolButton:pressed {{ background: {PRESSED}; }}
QToolButton:focus {{ border: 2px solid {FOCUS_RING}; padding: 4px; }}
QToolButton::menu-indicator {{ image: none; }}
QToolButton#textTool {{ color: {TEXT_SOFT}; font-size: {px['callout']}px; font-weight: 500; padding: 6px 10px; }}
QToolButton#rowArrow {{
    background: {rgba(SURFACE if not FLAT else BG, 0.80)}; border: 1px solid {SEPARATOR}; border-radius: 14px;
}}
QToolButton#rowArrow:hover {{ background: {SURFACE_RAISED}; }}
QToolButton#backRound {{
    background: {rgba('#000000', 0.40)}; border: 1px solid {rgba('#ffffff', 0.18)}; border-radius: 18px;
}}
QToolButton#backRound:hover {{ background: {rgba('#000000', 0.58)}; }}

/* ---- headings ---- */
#rowTitle {{ color: {TEXT}; font-size: {px['headline'] + 2}px; font-weight: 600; }}
#pageTitle {{ color: {TEXT}; font-size: {px['largeTitle']}px; font-weight: 700; }}
#sectionTitle {{ color: {TEXT}; font-size: {px['title']}px; font-weight: 700; }}
#hint {{ color: {TEXT_MUTED}; font-size: {px['body']}px; }}
#emptyTitle {{ color: {TEXT_SOFT}; font-size: {px['title']}px; font-weight: 600; }}

/* ---- pop-ups: season selector and menus ---- */
QComboBox {{
    background: {rgba(TEXT, 0.08)}; border: 1px solid transparent; border-radius: {RADIUS_S + 1}px;
    padding: 6px 34px 6px 12px; font-size: {px['body']}px; font-weight: 600; min-width: 120px;
    combobox-popup: 0;  /* drop the list below the box instead of over it */
}}
QComboBox:hover {{ background: {rgba(TEXT, 0.12)}; }}
QComboBox:focus {{ border: 2px solid {FOCUS_RING}; padding: 5px 33px 5px 11px; }}
QComboBox::drop-down {{ border: none; width: 30px; }}
QComboBox QAbstractItemView {{
    background: {SURFACE_RAISED}; border: 1px solid {SEPARATOR}; border-radius: {RADIUS_S}px; outline: none;
    selection-background-color: {ACCENT}; selection-color: {ON_ACCENT}; padding: 4px;
}}
QComboBox QAbstractItemView::item {{ padding: 6px 12px; min-height: 22px; border-radius: {RADIUS_S - 1}px; color: {TEXT}; }}
QComboBox QAbstractItemView::item:hover, QComboBox QAbstractItemView::item:selected {{
    background: {ACCENT}; color: {ON_ACCENT};
}}
QMenu {{ background: {SURFACE_RAISED}; border: 1px solid {SEPARATOR}; border-radius: {RADIUS_M}px; padding: 5px; }}
QMenu::item {{ padding: 5px 26px 5px 22px; border-radius: {RADIUS_S - 1}px; font-size: {px['callout']}px; color: {TEXT}; }}
QMenu::item:selected {{ background: {ACCENT}; color: {ON_ACCENT}; }}
QMenu::item:disabled {{ color: {TEXT_MUTED}; }}
QMenu::separator {{ height: 1px; background: {SEPARATOR}; margin: 5px 8px; }}
QMenu::indicator {{ width: 14px; height: 14px; left: 4px; }}

/* ---- lists, dialogs, downloads ---- */
QListWidget {{
    background: {SURFACE}; border: 1px solid {SEPARATOR}; border-radius: {RADIUS_M}px; font-size: {px['callout']}px;
}}
QListWidget::item {{ padding: 9px 12px; border-bottom: 1px solid {SEPARATOR}; }}
QListWidget::item:selected {{ background: {ACCENT_SOFT}; color: {TEXT}; }}
QListWidget::item:hover:!selected {{ background: {HOVER}; }}
QListWidget#downloadList::item {{ padding: 0; }}
QWidget#downloadRow {{ background: transparent; }}
QLabel#downloadTitle {{ font-size: {px['body']}px; font-weight: 600; color: {TEXT}; background: transparent; }}
QLabel#downloadSpeed {{ font-size: {px['footnote']}px; font-weight: 600; color: {ACCENT}; background: transparent; }}
QProgressBar#downloadBar {{
    background: {rgba(TEXT, 0.10)}; border: none; border-radius: 4px;
    color: {TEXT_SOFT}; font-size: {px['caption']}px; font-weight: 600; text-align: center;
    max-height: 8px; min-height: 8px;
}}
QProgressBar#downloadBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}
QLabel#inlineWarning {{ color: {WARNING}; font-size: {px['callout']}px; font-weight: 500; }}
QLineEdit#searchBox[large="true"] {{ font-size: {px['headline']}px; padding: 12px 14px 12px 14px; border-radius: {RADIUS_M}px; }}
QLineEdit#searchBox[large="true"]:focus {{ padding: 11px 13px 11px 13px; }}
QLabel#dialogTitle {{ font-size: {px['headline'] + 2}px; font-weight: 600; }}
QMessageBox {{ background: {SURFACE}; }}
QMessageBox QLabel {{ color: {TEXT_SOFT}; font-size: {px['body']}px; }}

/* ---- player ---- */
#playerBar {{ background: {BG}; }}
#playerTitle {{ color: {TEXT}; font-size: {px['headline']}px; font-weight: 600; }}
#playerNote {{ color: {TEXT_DIM}; font-size: {px['callout']}px; }}
#playerTime {{ color: {TEXT_SOFT}; font-size: {px['callout']}px; font-weight: 500; }}
#playerHud {{
    background: {rgba('#000000', 0.62)}; color: #ffffff; border: none; border-radius: {RADIUS_L}px;
    font-size: {px['title']}px; font-weight: 600; padding: 10px 24px;
}}
"""


class _FocusPolicy(QObject):
    """Buttons take keyboard focus (so Tab shows the ring) but not on a mouse click."""

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.Type.Polish and isinstance(watched, QAbstractButton):
            if watched.focusPolicy() == Qt.FocusPolicy.StrongFocus:
                watched.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        return False


_focus_policy: _FocusPolicy | None = None


def font_family() -> str:
    """The first installed font from the preferred list (falls back to the system default)."""
    installed = set(QFontDatabase.families())
    for family in _FONT_CANDIDATES:
        if family in installed:
            return family
    return QApplication.font().family()


def apply(app: QApplication) -> None:
    """Install the font, palette and style sheet on *app* and keep them in step with the theme."""
    global _focus_policy
    app.setStyle("Fusion")
    font = QFont(font_family())
    font.setPixelSize(TYPE["body"][0])
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    app.setFont(font)
    _focus_policy = _FocusPolicy(app)
    app.installEventFilter(_focus_policy)
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


def ui_font(pixel_size: int, weight: QFont.Weight = QFont.Weight.Normal, tracking: float | None = None) -> QFont:
    """A font for custom-painted widgets. *tracking* is a % of the size; default follows the size
    (tight when large, slightly open when small)."""
    font = QFont(QApplication.font())
    font.setPixelSize(pixel_size)
    font.setWeight(weight)
    if tracking is None:
        tracking = -1.2 if pixel_size >= 26 else -0.6 if pixel_size >= 17 else 0.0 if pixel_size >= 13 else 0.3
    font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 100 + tracking)
    return font


def type_font(role: str) -> QFont:
    """The font for a type-ramp role (see TYPE)."""
    size, weight, tracking = TYPE[role]
    return ui_font(size, weight, tracking)


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
