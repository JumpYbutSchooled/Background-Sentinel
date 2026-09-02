"""Palette, fonts and interpolation shared by every painted surface.

The accent is a single mutable QColor rather than a constant: every draw call
copies it through `fade()`, so recolouring the whole interface is one in-place
mutation and needs no repaint plumbing.

Fonts are cached. `mono()` is called for every cell on every frame, and
constructing a QFont each time is a measurable cost at 120fps.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont

from ..config import settings

# ------------------------------------------------------------------ palette

ACCENT = QColor("#39d353")
TEXT = QColor(230, 232, 238)
MUTED = QColor(104, 118, 112)
CELL_FILL = QColor(10, 14, 18)
PANEL_FILL = QColor(9, 12, 16, 246)
BAND_FILL = QColor(9, 12, 16, 252)
BACKDROP = QColor(6, 8, 11, 244)
WARN = QColor(255, 132, 92)

# Syntax colours for both command lines. A valid command takes the accent, so
# it follows the theme; the error red and the quoted blue are fixed, because
# they mean something specific regardless of what the accent happens to be.
BAD = QColor("#ff5f56")
QUOTED = QColor("#5aa9ff")

MONO_FAMILIES = ["JetBrains Mono", "Cascadia Mono", "Consolas", "Courier New"]


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


def _follow_settings(key: str, value: object) -> None:
    if key == "accent":
        set_accent(str(value))


# The palette owns the accent, rather than whichever widget happens to exist:
# recolouring must work even if no navigator has been constructed yet.
set_accent(settings.get("accent"), immediate=True)
settings.listeners.append(_follow_settings)


# -------------------------------------------------------------------- fonts

_FONTS: dict[tuple[int, bool], QFont] = {}


def mono(size: int, bold: bool = False) -> QFont:
    size = max(1, int(size))
    key = (size, bold)
    font = _FONTS.get(key)
    if font is None:
        font = QFont()
        font.setFamilies(MONO_FAMILIES)
        font.setPixelSize(size)
        font.setBold(bold)
        _FONTS[key] = font
    return font


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
