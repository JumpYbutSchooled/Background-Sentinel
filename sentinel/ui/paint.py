"""Palette, fonts, shapes and interpolation shared by every painted surface.

Every colour here is a single mutable QColor rather than a constant: draw calls
copy them through `fade()`, so both recolouring the accent and swapping the
whole theme are in-place mutations that need no repaint plumbing. A surface
that imported `TEXT` a hundred frames ago is already holding the new one.

The same trick does not work for shapes, so those are functions. `plate()`
stands where `drawRoundedRect` used to: it takes the same rectangle and corner
radius and cuts the corner the way the current theme cuts corners — an arc
under phosphor, a straight 45° face under mechanical. Call sites do not know
which, which is the point.

Fonts are cached, because `mono()` is called for every cell on every frame and
constructing a QFont each time is a measurable cost at 120fps. The cache is
keyed on a generation counter that the theme bumps, so a theme change drops
every font without anyone having to remember to clear it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontDatabase,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPen,
)

from .. import theme as theming
from ..config import settings

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ palette
# Seeded from the phosphor theme and then rewritten in place by `_apply_theme`,
# which runs at the bottom of this module before anything has drawn.

ACCENT = QColor("#39d353")
TEXT = QColor(230, 232, 238)
MUTED = QColor(104, 118, 112)
CELL_FILL = QColor(10, 14, 18)
PANEL_FILL = QColor(9, 12, 16, 246)
BAND_FILL = QColor(9, 12, 16, 252)
BACKDROP = QColor(6, 8, 11, 244)
WARN = QColor(255, 132, 92)

# Syntax colours for both command lines. A valid command takes the accent, so
# it follows the theme; the error and quote colours are named by the theme
# rather than derived, because they mean something specific — and a theme whose
# accent is already crimson cannot say "wrong" in red.
BAD = QColor("#ff5f56")
QUOTED = QColor("#5aa9ff")

#: The popup card's fill and border. Here rather than in `popup`, because the
#: navigator's outro has to converge on exactly these two and both modules must
#: read the same object for that to survive a theme change mid-animation.
CARD_FILL = QColor("#0f1116")
CARD_EDGE = QColor("#2a2f3a")

#: The theme's second colour, drawn under the accent on primary plates. Fully
#: transparent in a theme that has no second colour, so a draw call can use it
#: unconditionally and simply put nothing on screen.
BALLAST = QColor(0, 0, 0, 0)

MONO_FAMILIES = ["JetBrains Mono", "Cascadia Mono", "Consolas", "Courier New"]

#: Where `theme.chrome_files` are looked for.
FONT_DIR = Path(__file__).with_name("fonts")


#: What the accent is heading for. `ACCENT` is where it has got to — cycling
#: the setting sweeps the whole interface across rather than repainting it a
#: different colour between two frames.
_WANTED = QColor(ACCENT)


def set_accent(colour: str, immediate: bool = False) -> None:
    """Recolour the interface, so every reference follows.

    `immediate` is for the seed at import, where there is nothing on screen to
    sweep and no frame clock running to do the sweeping.
    """
    parsed = QColor(colour)
    if not parsed.isValid():
        return
    _WANTED.setRgb(parsed.red(), parsed.green(), parsed.blue())
    if immediate:
        ACCENT.setRgb(parsed.red(), parsed.green(), parsed.blue())


def accent_settled() -> bool:
    return ACCENT.rgb() == _WANTED.rgb()


def advance_accent(delta: float) -> bool:
    """Step the live accent toward the chosen one. True while it is moving.

    Stepped by whatever is already running a frame clock — the palette has no
    timer of its own, because a colour nobody is looking at need not move.
    """
    from .motion import chase

    if accent_settled():
        return False
    # The snap has to exceed one whole channel step. `ACCENT` stores integers,
    # so a gap of 1 rounds straight back to where it started every frame and
    # the sweep would never actually arrive.
    channels = [
        chase(float(now), float(want), delta, snap=1.5)
        for now, want in (
            (ACCENT.red(), _WANTED.red()),
            (ACCENT.green(), _WANTED.green()),
            (ACCENT.blue(), _WANTED.blue()),
        )
    ]
    ACCENT.setRgb(*(max(0, min(255, int(round(c)))) for c in channels))
    return True


# --------------------------------------------------------------------- theme


def _set(target: QColor, value: str | tuple[str, int]) -> None:
    """Rewrite one palette entry in place, keeping every existing reference."""
    if isinstance(value, tuple):
        name, alpha = value
    else:
        name, alpha = value, 255
    parsed = QColor(name)
    if not parsed.isValid():
        log.warning("Theme names an unreadable colour: %r", name)
        return
    target.setRgb(parsed.red(), parsed.green(), parsed.blue(), alpha)


#: Bumped on every theme change; part of every font cache key.
_generation = 0


def _apply_theme(active: theming.Theme, seed: bool = False) -> None:
    """Take the whole palette and type from a theme.

    The accent is deliberately *not* forced here — `_follow_settings` moves it
    when the setting changes, so switching theme sweeps the accent across with
    the same animation cycling it by hand uses.
    """
    global _generation
    _generation += 1
    _FONTS.clear()

    _set(TEXT, active.text)
    _set(MUTED, active.muted)
    _set(WARN, active.warn)
    _set(BAD, active.bad)
    _set(QUOTED, active.quoted)
    _set(CELL_FILL, active.cell_fill)
    _set(PANEL_FILL, active.panel_fill)
    _set(BAND_FILL, active.band_fill)
    _set(BACKDROP, active.backdrop)
    _set(CARD_FILL, active.card_fill)
    _set(CARD_EDGE, active.card_edge)
    if active.ballast:
        _set(BALLAST, active.ballast)
    else:
        BALLAST.setRgb(0, 0, 0, 0)

    _load_fonts(active)
    if seed:
        set_accent(active.accent, immediate=True)


def _follow_settings(key: str, value: object) -> None:
    if key == "accent":
        set_accent(str(value))
    elif key == "theme":
        # The theme registry has already applied itself by the time this runs —
        # its own listener is registered first — so the accent moves to whatever
        # the new theme leads with, and cycling from there still works.
        settings.set("accent", theming.active().accent)


def _on_theme(active: theming.Theme) -> None:
    _apply_theme(active)


# -------------------------------------------------------------------- fonts

_FONTS: dict[tuple[str, int, bool, int], QFont] = {}

#: Font files already handed to Qt, so a theme switched back and forth does not
#: register them again.
_LOADED: set[str] = set()

#: Families the current theme wants its chrome set in, in preference order.
_CHROME_FAMILIES: list[str] = list(MONO_FAMILIES)
_CHROME_SCALE = 1.0
_CHROME_TRACKING = 100.0


#: Bundled files the current theme wants, not yet handed to Qt.
_PENDING: list[str] = []


def _load_fonts(active: theming.Theme) -> None:
    """Note what the theme wants. Registration itself waits — see `_register`."""
    global _CHROME_FAMILIES, _CHROME_SCALE, _CHROME_TRACKING, _PENDING
    _PENDING = [name for name in active.chrome_files if name not in _LOADED]
    _CHROME_FAMILIES = list(active.chrome_families or MONO_FAMILIES)
    _CHROME_SCALE = active.chrome_scale
    _CHROME_TRACKING = active.chrome_tracking


def _register() -> None:
    """Hand any bundled faces to Qt, the first time a font is actually asked for.

    Not at import: this module is imported while `app` is still building its
    widget classes, before `QApplication` exists, and QFontDatabase refuses to
    do anything at all until one does. By the time something wants a font to
    draw with, there is one.

    A missing file is a warning and nothing more. The bundled face is a nicety;
    the families behind it are all system fonts, and the interface has to come
    up on a checkout that never had the binaries.
    """
    global _PENDING
    if not _PENDING:
        return
    if QGuiApplication.instance() is None:
        return
    for name in _PENDING:
        path = FONT_DIR / name
        if not path.is_file():
            log.warning("Bundled font missing, falling back: %s", path)
            _LOADED.add(name)  # do not go looking for it again every frame
            continue
        if QFontDatabase.addApplicationFont(str(path)) < 0:
            log.warning("Qt would not load the bundled font: %s", path)
            _LOADED.add(name)
            continue
        log.debug("Loaded bundled font %s", name)
        _LOADED.add(name)
    _PENDING = []


def _font(families: list[str], size: int, bold: bool, tracking: float) -> QFont:
    _register()
    size = max(1, int(size))
    key = (families[0] if families else "", size, bold, _generation)
    font = _FONTS.get(key)
    if font is None:
        font = QFont()
        font.setFamilies(families)
        font.setPixelSize(size)
        font.setBold(bold)
        if abs(tracking - 100.0) > 0.5:
            font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, tracking)
        _FONTS[key] = font
    return font


def mono(size: int, bold: bool = False) -> QFont:
    """The column-safe face. Anything padded with spaces is drawn in this."""
    return _font(MONO_FAMILIES, size, bold, 100.0)


def chrome(size: int, bold: bool = False) -> QFont:
    """The theme's own face, for labels, headings and the status line.

    Falls back to the mono list in a theme that does not name one, so a caller
    never has to ask which theme is running.
    """
    return _font(
        _CHROME_FAMILIES, int(round(size * _CHROME_SCALE)), bold, _CHROME_TRACKING
    )


def label(text: str) -> str:
    """A chrome label, cased the way the theme cases them."""
    return text.upper() if theming.active().chrome_caps else text


# ------------------------------------------------------------------- shapes


def plate_path(rect: QRectF, corner: float) -> QPainterPath:
    """The theme's plate: `rect` with its corners cut by `corner`.

    Under phosphor that is `drawRoundedRect` in a path, arcs and all. Under a
    chamfering theme the corner becomes a straight 45° face, and the two
    leading corners are cut deeper than the other two — an equal cut gives a
    tidy octagon, which reads as a decoration rather than as a plate that has
    been sheared to fit.

    Every cut is clamped to half the shorter side, so a "radius" of 150 on a
    300px dial resolves to the largest polygon that rectangle can hold instead
    of folding the path through itself.
    """
    active = theming.active()
    limit = min(rect.width(), rect.height()) / 2.0
    corner = max(0.0, min(corner, limit))

    if active.corners != "chamfer" or corner <= 0.5:
        path = QPainterPath()
        path.addRoundedRect(rect, corner, corner)
        return path

    heavy = max(0.0, min(corner * active.cut_heavy, limit))
    light = max(0.0, min(corner * active.cut_light, limit))
    left, top = rect.left(), rect.top()
    right, bottom = rect.right(), rect.bottom()

    path = QPainterPath()
    # Clockwise from below the heavy top-left cut. Top-left and bottom-right
    # take the deep face, so the plate leans the same way everywhere on screen.
    path.moveTo(left, top + heavy)
    path.lineTo(left + heavy, top)
    path.lineTo(right - light, top)
    path.lineTo(right, top + light)
    path.lineTo(right, bottom - heavy)
    path.lineTo(right - heavy, bottom)
    path.lineTo(left + light, bottom)
    path.lineTo(left, bottom - light)
    path.closeSubpath()
    return path


def plate(painter: QPainter, rect: QRectF, corner: float) -> None:
    """Draw the theme's plate with the painter's current pen and brush.

    A drop-in for `painter.drawRoundedRect(rect, corner, corner)`, which is
    what every call site used to say.
    """
    painter.drawPath(plate_path(rect, corner))


def hatch(
    painter: QPainter, rect: QRectF, corner: float, colour: QColor, alpha: float = 1.0
) -> None:
    """Lay the theme's diagonal hatch inside a plate. Nothing, in a theme
    without one.

    Clipped to the plate rather than to its bounding box, so the stripes stop
    at the chamfer instead of running out over the cut corner.
    """
    active = theming.active()
    if active.hatch <= 0.0 or alpha <= 0.0:
        return
    if rect.width() <= 2.0 or rect.height() <= 2.0:
        return

    painter.save()
    painter.setClipPath(plate_path(rect, corner), Qt.ClipOperation.IntersectClip)
    painter.setPen(_hatch_pen(colour, active.hatch * alpha))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    step = active.hatch_step
    # Start far enough left that the lines sloping down-right still cover the
    # top-right corner; the clip takes care of everything drawn outside.
    span = rect.width() + rect.height()
    x = rect.left() - rect.height()
    while x < rect.left() + span:
        painter.drawLine(
            QPointF(x, rect.bottom()), QPointF(x + rect.height(), rect.top())
        )
        x += step
    painter.restore()


def _hatch_pen(colour: QColor, alpha: float) -> QPen:
    pen = QPen(fade(colour, alpha), 1.0)
    pen.setCosmetic(True)
    return pen


def stroke(weight: float) -> float:
    """One outline weight, scaled by the theme.

    Hairlines suit a drafting table; a mechanism is drawn in heavier line.
    """
    return max(0.4, weight * theming.active().stroke)


def brush(colour: QColor, alpha: float = 1.0) -> QBrush:
    return QBrush(fade(colour, alpha))


# ------------------------------------------------------------ interpolation


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def clamp01(t: float) -> float:
    return 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)


def smoothstep(edge0: float, edge1: float, x: float) -> float:
    if edge1 <= edge0:
        return 0.0 if x < edge0 else 1.0
    t = clamp01((x - edge0) / (edge1 - edge0))
    return t * t * (3.0 - 2.0 * t)


def smootherstep(edge0: float, edge1: float, x: float) -> float:
    """Ken Perlin's smootherstep: 6t^5 - 15t^4 + 10t^3.

    Unlike smoothstep, its second derivative is also zero at both ends, so a
    move eases in and out without the faint snap you get from acceleration
    changing instantly.
    """
    if edge1 <= edge0:
        return 0.0 if x < edge0 else 1.0
    t = clamp01((x - edge0) / (edge1 - edge0))
    return t * t * t * (t * (t * 6.0 - 15.0) + 10.0)


def fade(color: QColor, alpha: float) -> QColor:
    faded = QColor(color)
    faded.setAlphaF(clamp01(color.alphaF() * alpha))
    return faded


def blend(a: QColor, b: QColor, t: float) -> QColor:
    return QColor.fromRgbF(
        lerp(a.redF(), b.redF(), t),
        lerp(a.greenF(), b.greenF(), t),
        lerp(a.blueF(), b.blueF(), t),
        lerp(a.alphaF(), b.alphaF(), t),
    )


# The palette owns the accent and the theme, rather than whichever widget
# happens to exist: recolouring must work even if no navigator has been
# constructed yet. The theme is applied first so the accent seeded from
# settings wins over the theme's own default when the two disagree.
_apply_theme(theming.active(), seed=True)
set_accent(settings.get("accent"), immediate=True)
theming.listeners.append(_on_theme)
settings.listeners.append(_follow_settings)
