"""The two looks, as data.

A theme is not a palette swap. It carries the colours, but also the shape
language every plate is cut to, the character of the flicker things arrive on,
the easing the journey runs through, and the face the chrome is set in.
Everything that says *which interface this is* is in one table here, and the
modules that draw and animate read it rather than each holding their own
opinion.

Deliberately free of Qt. `commands` needs the theme as much as the
painters do, and neither should have to import a UI toolkit to ask what the
accent is. Colours are strings, easings are curve *names* — `ui.paint` and
`ui.motion` resolve both against Qt on their side of the line.

Adding a third theme is adding an entry to `THEMES` and a pair to the `theme`
option in `config`. Those two lists are the only ones — the command, its
completions and the settings row all read one or the other — and they are
checked against each other at import, because a theme you can select and cannot
apply is a worse bug than either list being wrong on its own.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .config import settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Theme:
    key: str
    label: str
    #: One line, shown by `theme` and in the settings panel.
    blurb: str

    # -- colour ---------------------------------------------------------------
    #: The signature accent. Switching theme moves the accent here, and the
    #: accent setting still cycles from it afterwards — a theme decides what it
    #: looks like by default, not what you are allowed to do to it.
    accent: str
    text: str
    muted: str
    warn: str
    #: An unknown command, and a quoted argument. Fixed per theme rather than
    #: following the accent: they mean something specific, and a red that meant
    #: "invalid" would be unreadable in a theme whose accent is already red.
    bad: str
    quoted: str
    #: Fills, as #rrggbb with an alpha 0-255 alongside. Translucency matters:
    #: these sit over the desktop.
    cell_fill: tuple[str, int]
    panel_fill: tuple[str, int]
    band_fill: tuple[str, int]
    backdrop: tuple[str, int]
    card_fill: tuple[str, int]
    card_edge: tuple[str, int]
    #: Drawn under the accent on primary plates — the second brand colour, or
    #: nothing at all in a theme that does not have one.
    ballast: str = ""

    # -- shape ----------------------------------------------------------------
    #: "round" cuts corners with an arc, "chamfer" with a straight 45° face.
    corners: str = "round"
    #: Chamfer only: how far the two leading corners are cut compared with the
    #: radius asked for, and how far the other two are. Unequal on purpose —
    #: equal cuts give a neat octagon, which is not the same as a plate that
    #: has been sheared.
    cut_heavy: float = 1.0
    cut_light: float = 1.0
    #: Multiplies every outline weight in the interface.
    stroke: float = 1.0
    #: Alpha of the 45° hatch laid inside primary plates. 0 disables it.
    hatch: float = 0.0
    #: Spacing of that hatch, in pixels.
    hatch_step: float = 9.0
    #: How a branch is routed out to its button: "orthogonal" steps along one
    #: axis at a time, "diagonal" runs at 45° and squares up at the end.
    routing: str = "orthogonal"
    #: Background furniture: the drifting rule grid, the rings breathing out of
    #: the dial, and the band sweeping down the screen.
    grid: str = "square"
    rings: str = "circle"
    scan: str = "soft"
    #: Whether the dial's slowly creeping arc is drawn as an arc or as a run of
    #: hard ticks.
    sweep: str = "arc"

    # -- motion ---------------------------------------------------------------
    #: The waveform anything arriving or leaving is stuttered through.
    on_pattern: tuple[float, ...] = (0.0, 0.9, 0.1, 1.0, 0.25, 0.85, 0.55, 1.0)
    off_pattern: tuple[float, ...] = (1.0, 0.92, 0.34, 0.30, 0.11, 0.09, 0.02, 0.0)
    #: Baseline duration of one flicker, before the speed dial scales it.
    flicker_base: int = 380
    #: QEasingCurve.Type names for the navigator's journey out and back. Named
    #: rather than imported so this module stays Qt-free.
    open_easing: str = "Linear"
    close_easing: str = "Linear"
    #: And for the moves inside it — drilling into a branch, blooming back out,
    #: a panel opening. `step` is the both-ends curve for a move that starts and
    #: stops; `settle` is for one that is already going when it begins.
    step_easing: str = "InOutCubic"
    settle_easing: str = "OutCubic"

    # -- type -----------------------------------------------------------------
    #: The chrome face, in preference order. Labels, headings, the status line —
    #: everything that is not laid out in columns.
    chrome_families: tuple[str, ...] = ()
    #: Files under `ui/fonts` to register before the families above are asked
    #: for. Missing files are logged and skipped; the next family takes over.
    chrome_files: tuple[str, ...] = ()
    #: Chrome text is drawn this much larger than the mono size it replaces —
    #: a condensed face at the same pixel size reads smaller than a mono one.
    chrome_scale: float = 1.0
    #: Chrome letter spacing, as a percentage. Wide tracking on short uppercase
    #: labels is most of what makes a plate read as machined rather than typed.
    chrome_tracking: float = 100.0
    #: Whether chrome labels are put through `.upper()`.
    chrome_caps: bool = False

    #: Anything a single surface needs and no other does.
    extra: dict[str, Any] = field(default_factory=dict)


PHOSPHOR = Theme(
    key="phosphor",
    label="phosphor",
    blurb="the green tube: round plates, soft glow, a CRT striking on",
    accent="#39d353",
    text="#e6e8ee",
    muted="#687670",
    warn="#ff845c",
    bad="#ff5f56",
    quoted="#5aa9ff",
    cell_fill=("#0a0e12", 255),
    panel_fill=("#090c10", 246),
    band_fill=("#090c10", 252),
    backdrop=("#06080b", 244),
    card_fill=("#0f1116", 255),
    card_edge=("#2a2f3a", 255),
    corners="round",
    stroke=1.0,
    routing="orthogonal",
    grid="square",
    rings="circle",
    scan="soft",
    sweep="arc",
    chrome_families=("JetBrains Mono", "Cascadia Mono", "Consolas", "Courier New"),
)

#: Built from the ASCTE palette: #bf0a30 primary, #002868 secondary, and the
#: greys the site sets everything else in. The crimson is the accent because it
#: is what the site leads with; the navy is structural — it fills the plates and
#: draws the hatch, so the second colour is in the furniture rather than in the
#: text, where it would be unreadable this dark.
MECHANICAL = Theme(
    key="mechanical",
    label="mechanical",
    blurb="ASCTE crimson and navy: sheared plates, hazard hatch, hard shutter",
    accent="#bf0a30",
    text="#f8f8f8",
    muted="#8a8a8a",
    warn="#ffa62b",
    # Amber, not red: the accent is already crimson, and an error that shares a
    # colour with every valid command is no signal at all. Quoted text takes the
    # navy lifted to where it can actually be read on a dark plate.
    bad="#ffa62b",
    quoted="#5c8ed6",
    cell_fill=("#191b1f", 255),
    panel_fill=("#131313", 247),
    band_fill=("#0e0f11", 252),
    backdrop=("#0b0c0e", 245),
    card_fill=("#141518", 255),
    card_edge=("#3a3d44", 255),
    ballast="#002868",
    corners="chamfer",
    cut_heavy=1.9,
    cut_light=0.5,
    stroke=1.75,
    hatch=0.16,
    hatch_step=8.0,
    routing="diagonal",
    grid="diagonal",
    rings="diamond",
    scan="shutter",
    sweep="ticks",
    # A shutter, not a tube. It slams open, drops once as it seats, and holds —
    # no false starts, because a mechanism that caught twice would be broken
    # rather than atmospheric.
    on_pattern=(0.0, 1.0, 0.42, 1.0, 1.0, 1.0, 1.0, 1.0),
    # And on the way out it is dropped: full, a moment of hesitation, then gone
    # in two hard steps. Never brighter than the step before, same as the tube.
    off_pattern=(1.0, 1.0, 0.9, 0.38, 0.34, 0.06, 0.02, 0.0),
    flicker_base=260,
    # The journey still runs linearly — every leg carries its own smootherstep,
    # and a curve here would ease it twice, which is the bug the phosphor
    # theme's own comment is about. The close is where a mechanism can bite:
    # OutExpo drops it home fast and lets it settle.
    open_easing="Linear",
    close_easing="OutExpo",
    # Quint rather than cubic: a longer wait, then a faster middle, then a
    # harder stop. Cubic reads as something gliding; this reads as something
    # driven, which is the difference between the two themes in one curve.
    step_easing="InOutQuint",
    settle_easing="OutExpo",
    chrome_families=(
        "Roboto Condensed", "Bahnschrift Condensed", "Bahnschrift",
        "Arial Narrow", "Segoe UI",
    ),
    chrome_files=("RobotoCondensed-Regular.ttf", "RobotoCondensed-Bold.ttf"),
    chrome_scale=1.16,
    chrome_tracking=112.0,
    chrome_caps=True,
)

THEMES: dict[str, Theme] = {theme.key: theme for theme in (PHOSPHOR, MECHANICAL)}

#: For the settings schema and the completions, in declaration order.
CHOICES: tuple[tuple[str, str], ...] = tuple(
    (theme.key, theme.label) for theme in THEMES.values()
)

DEFAULT = PHOSPHOR.key

_active = THEMES[DEFAULT]

#: Run after a theme change, before the settings listeners see it. Painters
#: register here to rebuild whatever they cached under the old look — a font,
#: a tiled pixmap, a stylesheet. Kept separate from `settings.listeners` so a
#: surface cannot repaint itself with half the new theme applied.
listeners: list[Any] = []


def active() -> Theme:
    return _active


def get(key: str) -> Theme:
    return THEMES.get(key, THEMES[DEFAULT])


def apply(key: str) -> Theme:
    """Make one theme current, and tell everything that cached anything."""
    global _active
    wanted = get(key)
    if wanted is _active:
        return _active
    _active = wanted
    log.info("Theme is now %s", wanted.key)
    for listener in list(listeners):
        try:
            listener(wanted)
        except Exception:
            log.exception("Theme listener failed for %s", wanted.key)
    return _active


def _follow_settings(key: str, value: object) -> None:
    if key == "theme":
        apply(str(value))


def _check_schema() -> None:
    """Warn if the settings option and this table have drifted apart."""
    from .config import SCHEMA

    offered = {
        value for option in SCHEMA if option.key == "theme"
        for value, _label in option.choices
    }
    missing = offered - set(THEMES)
    unreachable = set(THEMES) - offered
    if missing:
        log.error("Settings offer themes that do not exist: %s", sorted(missing))
    if unreachable:
        log.warning("Themes nothing can select: %s", sorted(unreachable))


# Seeded at import, like the accent and the speed dial: the theme belongs to
# the process, not to whichever window happens to be constructed first.
_check_schema()
apply(str(settings.get("theme")))
settings.listeners.append(_follow_settings)
