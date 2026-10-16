"""Atmospheric artwork behind the page content, one procedurally drawn scene per theme.

Everything is vector drawing (no bitmaps). The scene is painted into the scroll
area's *viewport* (see :func:`install`), under the transparent page content, so it
stays fixed to the window while cards scroll over it and always sits beneath every
card and control.

A single :class:`Backdrop` listens to the theme manager. A switch drops the old
scene and fades the new one in; the animated scenes (Deep Cosmos: twinkling stars
and shooting stars) run off one shared timer that only repaints visible pages.
"""

from __future__ import annotations

import math
import random
import weakref
from dataclasses import dataclass

from PyQt6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF, QRadialGradient
from PyQt6.QtWidgets import QAbstractScrollArea, QWidget

from . import theme
from .themes import Palette

FADE_IN_MS = 450
_FADE_TICK_MS = 33
_ANIMATION_TICK_MS = 66  # ~15 fps: smooth enough for a meteor, still light
_SHOOTING_PERIOD = 5.5  # seconds between shooting stars
_SHOOTING_LENGTH = 1.2  # seconds a streak is visible
_MAX_STARS = 420


def _colour(hex_colour: str, alpha: float) -> QColor:
    colour = QColor(hex_colour)
    colour.setAlphaF(max(0.0, min(1.0, alpha)))
    return colour


@dataclass(frozen=True, slots=True)
class _Star:
    x: float  # 0..1 across the viewport
    y: float
    radius: float
    alpha: float
    speed: float  # twinkle rate, radians per second
    phase: float
    tint: int  # 0 white, 1 ice blue, 2 violet
    bright: bool  # gets a glow and a four-point sparkle


def _make_stars() -> tuple[_Star, ...]:
    rng = random.Random(0x7E1A)  # fixed seed: the sky is the same on every launch
    stars = []
    for i in range(_MAX_STARS):
        bright = i < 16
        stars.append(
            _Star(
                x=rng.random(),
                y=rng.random(),
                radius=rng.uniform(1.6, 2.2) if bright else rng.choice((0.9, 1.0, 1.2, 1.4, 1.7)),
                alpha=rng.uniform(0.75, 1.0) if bright else rng.uniform(0.35, 0.85),
                speed=rng.uniform(0.6, 2.2),
                phase=rng.uniform(0, math.tau),
                tint=rng.choice((0, 0, 0, 1, 1, 2)),
                bright=bright,
            )
        )
    return tuple(stars)


_STARS = _make_stars()


# --------------------------------------------------------------- scenes


def _paint_deep_cosmos(p: QPainter, rect: QRectF, palette: Palette, clock: float) -> None:
    accent, alt, white = palette["accent"], palette["accent_alt"], palette["text_primary"]
    w, h = rect.width(), rect.height()
    reach = max(520.0, min(max(w, h), 1300.0))

    # Nebula: overlapping blooms, violet top-right, blue bottom-left, a violet wisp bottom-right.
    for cx, cy, radius, colour, strength in (
        (0.92, 0.02, 0.80, accent, 0.30),
        (0.70, 0.18, 0.42, accent, 0.14),
        (0.04, 1.02, 0.75, alt, 0.13),
        (0.96, 0.95, 0.50, accent, 0.17),
    ):
        centre = QPointF(rect.left() + cx * w, rect.top() + cy * h)
        glow = QRadialGradient(centre, reach * radius)
        glow.setColorAt(0.0, _colour(colour, strength))
        glow.setColorAt(0.45, _colour(colour, strength * 0.38))
        glow.setColorAt(1.0, _colour(colour, 0.0))
        p.fillRect(rect, glow)

    # Stars: plenty of them, density follows the window, sizes stay in pixels.
    count = min(_MAX_STARS, max(110, int(w * h / 3000)))
    tints = (white, alt, accent)
    p.setPen(Qt.PenStyle.NoPen)
    for star in _STARS[:count]:
        twinkle = 0.5 + 0.5 * math.sin(clock * star.speed + star.phase)
        alpha = star.alpha * (0.45 + 0.55 * twinkle)
        centre = QPointF(rect.left() + star.x * w, rect.top() + star.y * h)
        colour = tints[star.tint]
        if star.bright:
            halo = QRadialGradient(centre, 15 + 6 * twinkle)
            halo.setColorAt(0.0, _colour(colour, 0.55 * alpha))
            halo.setColorAt(1.0, _colour(colour, 0.0))
            p.setBrush(halo)
            p.drawEllipse(centre, 17, 17)
            spark = 9 + 7 * twinkle
            p.setPen(QPen(_colour(colour, 0.65 * alpha), 1.0))
            p.drawLine(QPointF(centre.x() - spark, centre.y()), QPointF(centre.x() + spark, centre.y()))
            p.drawLine(QPointF(centre.x(), centre.y() - spark), QPointF(centre.x(), centre.y() + spark))
            p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_colour(colour, alpha))
        p.drawEllipse(centre, star.radius, star.radius)

    # Shooting stars: a long bright streak with a glowing head, crossing the upper sky.
    cycle = clock % _SHOOTING_PERIOD
    if cycle < _SHOOTING_LENGTH:
        t = cycle / _SHOOTING_LENGTH
        rng = random.Random(int(clock // _SHOOTING_PERIOD) * 7919 + 3)
        start = QPointF(rect.left() + w * (0.35 + 0.6 * rng.random()), rect.top() + h * (0.04 + 0.30 * rng.random()))
        travel = 360.0
        head = QPointF(start.x() - travel * t * 0.82, start.y() + travel * t * 0.57)
        tail = QPointF(head.x() + 230 * 0.82, head.y() - 230 * 0.57)
        fade = math.sin(t * math.pi)
        streak = QLinearGradient(head, tail)
        streak.setColorAt(0.0, _colour(white, 0.95 * fade))
        streak.setColorAt(0.25, _colour(alt, 0.65 * fade))
        streak.setColorAt(1.0, _colour(alt, 0.0))
        p.setPen(QPen(streak, 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(head, tail)
        glow = QRadialGradient(head, 12)
        glow.setColorAt(0.0, _colour(white, 0.8 * fade))
        glow.setColorAt(1.0, _colour(alt, 0.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(glow)
        p.drawEllipse(head, 12, 12)


def _fir_polygon(cx: float, base: float, height: float, half_width: float, tiers: int) -> QPolygonF:
    """A conifer silhouette: a tapering stack of drooping tiers on a short trunk."""
    apex = base - height
    right, left = [], []
    for i in range(tiers):
        t = (i + 1) / tiers
        y = apex + height * (0.10 + 0.84 * t)
        reach = half_width * (0.16 + 0.84 * t)
        notch_y = y - height * 0.84 / tiers * 0.55  # the tier's underside tucks up and in
        notch_x = reach * 0.66
        right += [QPointF(cx + notch_x, notch_y), QPointF(cx + reach, y)]
        left += [QPointF(cx - notch_x, notch_y), QPointF(cx - reach, y)]
    trunk = half_width * 0.07
    pts = [QPointF(cx, apex)] + right
    pts += [QPointF(cx + trunk, base - height * 0.10), QPointF(cx + trunk, base)]
    pts += [QPointF(cx - trunk, base), QPointF(cx - trunk, base - height * 0.10)]
    pts += list(reversed(left))
    return QPolygonF(pts)


def _make_forest() -> tuple[tuple[tuple[float, float, float, int], ...], ...]:
    """Three depth layers of trees as (x 0..1, height scale, width ratio, tiers)."""
    rng = random.Random(0xF0125)
    layers = []
    for count in (52, 36, 22):  # far, middle, near
        trees = []
        for i in range(count):
            x = (i + rng.uniform(0.1, 0.9)) / count
            trees.append((x, rng.uniform(0.6, 1.0), rng.uniform(0.17, 0.25), rng.choice((7, 8, 9))))
        layers.append(tuple(trees))
    return tuple(layers)


_FOREST = _make_forest()


def _paint_boreal_forest(p: QPainter, rect: QRectF, palette: Palette, clock: float) -> None:
    mint, bg = palette["accent_alt"], palette["bg_base"]
    w, h = rect.width(), rect.height()
    unit = max(0.7, min(1.6, h / 760.0))

    # Moon glow and a few soft light shafts falling through the canopy.
    moon = QPointF(rect.left() + w * 0.80, rect.top() + h * 0.15)
    halo = QRadialGradient(moon, 340 * unit)
    halo.setColorAt(0.0, _colour(mint, 0.30))
    halo.setColorAt(0.35, _colour(mint, 0.10))
    halo.setColorAt(1.0, _colour(mint, 0.0))
    p.fillRect(rect, halo)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(_colour(mint, 0.55))
    p.drawEllipse(moon, 24 * unit, 24 * unit)
    shaft = QLinearGradient(moon, QPointF(moon.x() - 220 * unit, rect.bottom()))
    shaft.setColorAt(0.0, _colour(mint, 0.14))
    shaft.setColorAt(1.0, _colour(mint, 0.0))
    p.setBrush(shaft)
    for spread, drift in ((60, 0.0), (36, 150), (28, 320)):
        top = QPointF(moon.x() - drift * 0.04, moon.y())  # rays fan out from the moon itself
        p.drawPolygon(QPolygonF([
            QPointF(top.x() - 3, top.y()), QPointF(top.x() + 3, top.y()),
            QPointF(top.x() - 260 * unit - spread * unit - drift, rect.bottom()),
            QPointF(top.x() - 260 * unit + spread * unit - drift, rect.bottom()),
        ]))

    # Three layers of firs, far to near: hazier and lighter in the distance, near ones darker
    # than the sky, with mist pooling between the layers.
    ground = rect.bottom()
    layers = (  # (baseline above bottom, tree height, colour mix toward mint, mist strength)
        (110 * unit, 150 * unit, 0.16, 0.20),
        (52 * unit, 215 * unit, 0.08, 0.18),
        (-2 * unit, 290 * unit, 0.0, 0.0),
    )
    for trees, (lift, tree_h, haze, mist) in zip(_FOREST, layers):
        base = ground - lift
        colour = QColor(theme.mix(bg, mint, haze)) if haze else QColor(theme.mix(bg, "#000000", 0.55))
        if mist:
            band = QLinearGradient(0, base - tree_h * 0.55, 0, base + 30 * unit)
            band.setColorAt(0.0, _colour(mint, 0.0))
            band.setColorAt(1.0, _colour(mint, mist))
            p.fillRect(QRectF(rect.left(), base - tree_h * 0.55, w, tree_h * 0.55 + 30 * unit), band)
        p.setBrush(colour)
        for x, hs, width_ratio, tiers in trees:
            height = tree_h * hs
            p.drawPolygon(_fir_polygon(rect.left() + x * w, base + 1, height, height * width_ratio, tiers))
        p.drawRect(QRectF(rect.left(), base, w, max(0.0, ground - base)))


def _paint_neon_grid(p: QPainter, rect: QRectF, palette: Palette, clock: float) -> None:
    accent, line = palette["accent"], palette["accent_alt"]
    w, h = rect.width(), rect.height()
    depth = min(h * 0.46, 440.0)
    horizon = rect.bottom() - depth
    cx = rect.center().x()

    # Retro sun sitting on the horizon, cut by widening stripes.
    radius = min(w * 0.20, 190.0)
    sun = QLinearGradient(0, horizon - 2 * radius, 0, horizon)
    sun.setColorAt(0.0, _colour(accent, 0.55))
    sun.setColorAt(1.0, _colour(line, 0.35))
    body = QPainterPath()
    body.addEllipse(QPointF(cx, horizon - radius * 0.35), radius, radius)
    keep = QPainterPath()
    keep.addRect(QRectF(rect.left(), rect.top(), w, horizon - rect.top()))
    body = body.intersected(keep)
    for i in range(7):  # stripes get thicker towards the bottom of the sun
        y = horizon - radius * 0.1 - i * radius * 0.17
        cut = QPainterPath()
        cut.addRect(QRectF(cx - radius, y - 1.5 - i * 0.9, radius * 2, 3 + i * 1.8))
        body = body.subtracted(cut)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(sun)
    p.drawPath(body)

    glow = QLinearGradient(0, horizon - 90, 0, horizon + 40)
    glow.setColorAt(0.0, _colour(accent, 0.0))
    glow.setColorAt(0.7, _colour(accent, 0.22))
    glow.setColorAt(1.0, _colour(line, 0.0))
    p.fillRect(QRectF(rect.left(), horizon - 90, w, 130), glow)

    # Receding floor grid, brightest at the viewer's feet.
    fade = QLinearGradient(0, horizon, 0, rect.bottom())
    fade.setColorAt(0.0, _colour(line, 0.15))
    fade.setColorAt(1.0, _colour(line, 0.55))
    p.setPen(QPen(fade, 1.3))
    vanishing = QPointF(cx, horizon)
    spread = w * 1.6
    for i in range(-16, 17):
        p.drawLine(vanishing, QPointF(cx + i * spread / 16, rect.bottom()))
    rows = 10
    for i in range(1, rows + 1):
        y = horizon + depth * (i / rows) ** 2.2
        p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
    p.setPen(QPen(_colour(line, 0.75), 1.6))
    p.drawLine(QPointF(rect.left(), horizon), QPointF(rect.right(), horizon))

    # Circuit traces in the top corners.
    p.setBrush(Qt.BrushStyle.NoBrush)
    for sx, anchor in ((1, rect.topLeft()), (-1, rect.topRight())):
        for dy, run, jog in ((30, 220, 34), (70, 150, -28), (112, 280, 44), (158, 110, -24), (200, 190, 30)):
            x0, y0 = anchor.x(), anchor.y() + dy
            trace = QPainterPath(QPointF(x0, y0))
            trace.lineTo(x0 + sx * run * 0.55, y0)
            trace.lineTo(x0 + sx * (run * 0.55 + abs(jog)), y0 + jog)
            trace.lineTo(x0 + sx * run, y0 + jog)
            p.setPen(QPen(_colour(line, 0.42), 1.4))
            p.drawPath(trace)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_colour(line, 0.75))
            p.drawEllipse(QPointF(x0 + sx * run, y0 + jog), 3.2, 3.2)
            p.setBrush(Qt.BrushStyle.NoBrush)


def _paint_solarized_amber(p: QPainter, rect: QRectF, palette: Palette, clock: float) -> None:
    accent, cream = palette["accent"], palette["text_primary"]
    w, h = rect.width(), rect.height()

    # Phosphor bloom in the middle of the tube, darker towards the edges (CRT vignette).
    bloom = QRadialGradient(rect.center(), max(w, h) * 0.75)
    bloom.setColorAt(0.0, _colour(accent, 0.20))
    bloom.setColorAt(0.55, _colour(accent, 0.06))
    bloom.setColorAt(1.0, _colour("#000000", 0.55))
    p.fillRect(rect, bloom)

    # Analog graph-paper grid.
    p.setPen(QPen(_colour(accent, 0.12), 1.0))
    step = 72.0
    x = rect.left() + (step - (rect.left() % step)) % step
    while x < rect.right():
        p.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
        x += step
    y = rect.top() + (step - (rect.top() % step)) % step
    while y < rect.bottom():
        p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
        y += step

    # Oscilloscope reticle in the lower right: rings, cross and tick marks.
    centre = QPointF(rect.right() - min(w * 0.2, 260.0), rect.bottom() - min(h * 0.28, 230.0))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(_colour(accent, 0.38), 1.4))
    for r in (70, 130, 190):
        p.drawEllipse(centre, r, r)
    p.setPen(QPen(_colour(accent, 0.30), 1.2))
    p.drawLine(QPointF(centre.x() - 210, centre.y()), QPointF(centre.x() + 210, centre.y()))
    p.drawLine(QPointF(centre.x(), centre.y() - 210), QPointF(centre.x(), centre.y() + 210))
    for k in range(-20, 21):
        tick = 6 if k % 5 else 12
        p.drawLine(QPointF(centre.x() + k * 10, centre.y() - tick), QPointF(centre.x() + k * 10, centre.y() + tick))
        p.drawLine(QPointF(centre.x() - tick, centre.y() + k * 10), QPointF(centre.x() + tick, centre.y() + k * 10))
    wave = QPainterPath(QPointF(centre.x() - 190, centre.y()))
    for i in range(0, 381, 4):
        wave.lineTo(centre.x() - 190 + i, centre.y() - 46 * math.sin(i / 38.0) * math.sin(i / 190.0 * math.pi))
    p.setPen(QPen(_colour(palette["accent_alt"], 0.55), 1.8))
    p.drawPath(wave)

    # Scanlines over everything: 1px every 3px.
    visible = p.clipBoundingRect()
    top = max(rect.top(), visible.top()) if not visible.isEmpty() else rect.top()
    bottom = min(rect.bottom(), visible.bottom()) if not visible.isEmpty() else rect.bottom()
    p.setPen(QPen(_colour("#000000", 0.30), 1.0))
    y = math.ceil((top - rect.top()) / 3) * 3 + rect.top() + 0.5
    while y < bottom:
        p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
        y += 3

    # Corner registration brackets.
    p.setPen(QPen(_colour(accent, 0.65), 1.8))
    inset, arm = 20.0, 38.0
    for ox, oy, sx, sy in (
        (rect.left() + inset, rect.top() + inset, 1, 1),
        (rect.right() - inset, rect.top() + inset, -1, 1),
        (rect.left() + inset, rect.bottom() - inset, 1, -1),
        (rect.right() - inset, rect.bottom() - inset, -1, -1),
    ):
        p.drawLine(QPointF(ox, oy), QPointF(ox + sx * arm, oy))
        p.drawLine(QPointF(ox, oy), QPointF(ox, oy + sy * arm))


_SCENES = {
    "deep_cosmos": _paint_deep_cosmos,
    "boreal_forest": _paint_boreal_forest,
    "neon_grid": _paint_neon_grid,
    "solarized_amber": _paint_solarized_amber,
}
_ANIMATED = frozenset({"deep_cosmos"})


# ------------------------------------------------------------- lifecycle


class Backdrop(QObject):
    """Owns the theme's scene state (fade, clock) and keeps attached pages repainting."""

    def __init__(self) -> None:
        super().__init__()
        self._pages: weakref.WeakSet[QWidget] = weakref.WeakSet()
        self._key = theme.manager.current_theme_key
        self._clock = 0.0
        self._fade = 1.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        theme.manager.subscribe(self._on_theme)
        if self._key in _ANIMATED:
            self._timer.start(_ANIMATION_TICK_MS)

    def attach(self, page: QWidget) -> None:
        self._pages.add(page)

    @property
    def key(self) -> str:
        return self._key

    def _on_theme(self, palette: Palette) -> None:
        """Drop the old scene and fade the new one in."""
        self._key = palette["key"]
        self._fade = 0.0
        self._timer.start(_FADE_TICK_MS)
        self._repaint()

    def _tick(self) -> None:
        interval = self._timer.interval()
        self._clock += interval / 1000.0
        if self._fade < 1.0:
            self._fade = min(1.0, self._fade + interval / FADE_IN_MS)
            if self._fade >= 1.0:
                if self._key in _ANIMATED:
                    self._timer.setInterval(_ANIMATION_TICK_MS)
                else:
                    self._timer.stop()
        self._repaint()

    def _repaint(self) -> None:
        for page in list(self._pages):
            if page.isVisible():
                page.update()

    def paint(self, painter: QPainter, rect: QRectF, clip: QRectF | None = None) -> None:
        """Draw the active scene into *rect* (the visible window); *clip* limits the work to what is exposed."""
        scene = _SCENES.get(self._key)
        if scene is None or self._fade <= 0.0:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if clip is not None:
            painter.setClipRect(clip)
        painter.setOpacity(self._fade * self._fade * (3 - 2 * self._fade))  # smoothstep
        scene(painter, rect, theme.manager.get_current_theme(), self._clock)
        painter.restore()


_instance: Backdrop | None = None


def backdrop() -> Backdrop:
    """The shared Backdrop (created on first use, once a QApplication exists)."""
    global _instance
    if _instance is None:
        _instance = Backdrop()
    return _instance


class _ViewportPainter(QObject):
    """Paints the page base, sheen and themed scene into a scroll area's viewport."""

    def __init__(self, viewport: QWidget) -> None:
        super().__init__(viewport)
        self._viewport = viewport

    def eventFilter(self, watched, event) -> bool:
        if watched is self._viewport and event.type() == QEvent.Type.Paint:
            painter = QPainter(self._viewport)
            rect = QRectF(self._viewport.rect())
            base = QColor(theme.BG)
            painter.fillRect(event.rect(), base)
            sheen = QLinearGradient(0, 0, 0, 300)
            sheen.setColorAt(0.0, QColor(theme.SURFACE))
            sheen.setColorAt(1.0, base)
            painter.fillRect(QRectF(0, 0, rect.width(), 300), sheen)
            backdrop().paint(painter, rect, QRectF(event.rect()))
            painter.end()
        return False  # let the viewport go on to paint its children


def install(area: QAbstractScrollArea) -> None:
    """Give a scroll area the themed backdrop. Its content widget must not paint a background."""
    viewport = area.viewport()
    viewport.setAutoFillBackground(False)
    painter = _ViewportPainter(viewport)
    viewport.installEventFilter(painter)
    backdrop().attach(viewport)
    content = area.widget()
    if content is not None:
        content.setAutoFillBackground(False)  # QScrollArea turns this on by default
