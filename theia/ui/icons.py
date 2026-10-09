"""Small monochrome icons, drawn from inline SVG so they stay crisp at any scale."""

from __future__ import annotations

from functools import lru_cache

from PyQt6.QtCore import QByteArray, QRectF, QSize, Qt
from PyQt6.QtGui import QIcon, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer

from . import theme

_LINE = 'fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"'
_STROKE = f'{_LINE} stroke-width="2"'
_BOLD = f'{_LINE} stroke-width="2.4"'
_FILL = 'fill="currentColor" stroke="none"'
_ARROW_10 = (
    f'<g {_STROKE}><path d="M5.2 13.2a7 7 0 1 0 2-5.4"/><path d="M4.4 4.6v4.6H9"/></g>'
)
_TEN = (
    '<text x="12.3" y="17.1" text-anchor="middle" font-family="sans-serif" font-size="7.4" '
    'font-weight="700" fill="currentColor">10</text>'
)

_ICONS = {
    "play": f'<path {_FILL} d="M8 5.2v13.6a.8.8 0 0 0 1.22.68l10.9-6.8a.8.8 0 0 0 0-1.36L9.22 4.52A.8.8 0 0 0 8 5.2z"/>',
    "pause": f'<g {_FILL}><rect x="6" y="5" width="4.2" height="14" rx="1"/><rect x="13.8" y="5" width="4.2" height="14" rx="1"/></g>',
    "next": f'<g {_FILL}><path d="M5 5.4v13.2a.7.7 0 0 0 1.1.58L15.5 12 6.1 4.82A.7.7 0 0 0 5 5.4z"/><rect x="16.6" y="5" width="2.6" height="14" rx="1"/></g>',
    "restart": f'<g {_FILL}><rect x="4.8" y="5" width="2.6" height="14" rx="1"/><path d="M19 5.4v13.2a.7.7 0 0 1-1.1.58L8.5 12l9.4-7.18A.7.7 0 0 1 19 5.4z"/></g>',
    "back10": _ARROW_10 + _TEN,
    "forward10": f'<g transform="translate(24 0) scale(-1 1)">{_ARROW_10}</g>' + _TEN.replace('x="12.3"', 'x="11.7"'),
    "volume": f'<path {_FILL} d="M3 9.6v4.8h3.6l5 4.1a.6.6 0 0 0 1-.46V5.96a.6.6 0 0 0-1-.46l-5 4.1z"/>'
              f'<g {_STROKE}><path d="M15.8 9.2a4 4 0 0 1 0 5.6"/><path d="M18.4 6.6a7.7 7.7 0 0 1 0 10.8"/></g>',
    "volume-low": f'<path {_FILL} d="M3 9.6v4.8h3.6l5 4.1a.6.6 0 0 0 1-.46V5.96a.6.6 0 0 0-1-.46l-5 4.1z"/>'
                  f'<g {_STROKE}><path d="M15.8 9.2a4 4 0 0 1 0 5.6"/></g>',
    "mute": f'<path {_FILL} d="M3 9.6v4.8h3.6l5 4.1a.6.6 0 0 0 1-.46V5.96a.6.6 0 0 0-1-.46l-5 4.1z"/>'
            f'<g {_STROKE}><path d="M16 9.5l5 5M21 9.5l-5 5"/></g>',
    "fullscreen": f'<g {_STROKE}><path d="M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5"/></g>',
    "fullscreen-exit": f'<g {_STROKE}><path d="M9 4v5H4M20 9h-5V4M15 20v-5h5M4 15h5v5"/></g>',
    "subtitles": f'<g {_STROKE}><rect x="3" y="5" width="18" height="14" rx="2.5"/>'
                 '<path d="M7 11.5h4M14 11.5h3M7 15h2.5M12.5 15H17"/></g>',
    "download": f'<g {_STROKE}><path d="M12 4v10M7.5 10l4.5 4.5 4.5-4.5M5 19h14"/></g>',
    "audio": f'<g {_STROKE}><path d="M4 10v4M8 6.5v11M12 9v6M16 4.5v15M20 10v4"/></g>',
    "back": f'<g {_STROKE}><path d="M19 12H5M11 6l-6 6 6 6"/></g>',
    "chevron-left": f'<g {_BOLD}><path d="M15 5l-7 7 7 7"/></g>',
    "chevron-right": f'<g {_BOLD}><path d="M9 5l7 7-7 7"/></g>',
    "chevron-down": f'<g {_BOLD}><path d="M6 9.5l6 6 6-6"/></g>',
    "search": f'<g {_STROKE}><circle cx="10.8" cy="10.8" r="6.3"/><path d="M15.6 15.6L20.5 20.5"/></g>',
    "close": f'<g {_STROKE}><path d="M6 6l12 12M18 6L6 18"/></g>',
    "refresh": f'<g {_STROKE}><path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3"/><path d="M19.8 3.8v4.6h-4.6"/></g>',
    "folder": f'<g {_STROKE}><path d="M3 7.5a2 2 0 0 1 2-2h4.2l2 2.4H19a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></g>',
    "check": f'<g {_BOLD}><path d="M5 12.5l4.5 4.5L19 7.5"/></g>',
    "palette": f'<g {_STROKE}><path d="M12 3.5a8.5 8.5 0 1 0 0 17c1.4 0 2-1 1.6-2-.5-1.2.2-2.5 1.6-2.5H17a3.5 3.5 0 0 0 3.5-3.5C20.5 6.9 16.7 3.5 12 3.5z"/></g><g {_FILL}><circle cx="7.8" cy="11.5" r="1.2"/><circle cx="10.5" cy="7.6" r="1.2"/><circle cx="15" cy="7.8" r="1.2"/></g>',
    "more": f'<g {_FILL}><circle cx="12" cy="5.5" r="1.7"/><circle cx="12" cy="12" r="1.7"/><circle cx="12" cy="18.5" r="1.7"/></g>',
    "library": f'<g {_STROKE}><path d="m16 6 4 14M12 6v14M8 8v12M4 4v16"/></g>',
    "tv": f'<g {_STROKE}><rect x="2.5" y="7" width="19" height="14" rx="2.5"/><path d="M16.5 2.5L12 7 7.5 2.5"/></g>',
    "clock": f'<g {_STROKE}><circle cx="12" cy="12" r="9.5"/><path d="M12 6.5V12l3.5 2"/></g>',
    "film": f'<g {_STROKE}><rect x="3.5" y="4.5" width="17" height="15" rx="2"/>'
            '<path d="M8 4.5v15M16 4.5v15M3.5 9.5H8M3.5 14.5H8M16 9.5h4.5M16 14.5h4.5"/></g>',
}


@lru_cache(maxsize=256)
def _renderer(name: str, colour: str) -> QSvgRenderer:
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">{_ICONS[name]}</svg>'
    return QSvgRenderer(QByteArray(svg.replace("currentColor", colour).encode()))


def pixmap(name: str, size: int = 24, colour: str = "#ffffff", dpr: float = 1.0) -> QPixmap:
    """Render icon *name* into a ``size`` × ``size`` (logical pixels) pixmap."""
    pixels = max(1, round(size * dpr))
    result = QPixmap(pixels, pixels)
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    _renderer(name, colour).render(painter, QRectF(0, 0, pixels, pixels))
    painter.end()
    result.setDevicePixelRatio(dpr)
    return result


def paint(painter: QPainter, name: str, rect: QRectF, colour: str | None = None) -> None:
    """Draw icon *name* straight into *rect* of an active painter."""
    _renderer(name, colour or theme.TEXT).render(painter, rect)


def icon(name: str, colour: str | None = None, disabled: str | None = None) -> QIcon:
    """A QIcon with crisp renderings for normal and high-DPI screens.

    Colours default to the active theme's text colours.
    """
    return _icon(name, colour or theme.TEXT, disabled or theme.ICON_DISABLED)


@lru_cache(maxsize=256)
def _icon(name: str, colour: str, disabled: str) -> QIcon:
    result = QIcon()
    for scale in (1.0, 2.0):
        for size in (20, 24, 32):
            result.addPixmap(pixmap(name, size, colour, scale), QIcon.Mode.Normal)
            result.addPixmap(pixmap(name, size, disabled, scale), QIcon.Mode.Disabled)
    return result


ICON_SIZE = QSize(24, 24)
