"""The radial command navigator.

Summoned by typing `open` at the Sentinel prompt. A full-screen overlay draws
the command tree as a ring of branches around a large centre dial, with the
command line docked across the bottom.

Opening is a single continuous journey rather than a fade: the prompt capsule
balls up into a circle, rises to the top of the screen, unrolls back into a
capsule, then slides down to the bottom and stays there as the command line.
Closing runs the same journey backwards.

Drilling in is likewise one morph, not a page change:

    click a branch
      -> everything else flickers out, CRT-style
      -> the clicked button travels to the middle
      -> its outline swells into the big centre dial and grows new branches
      -> or, at a leaf, that same outline unfolds into a full-screen frame

Every shape on screen is a rounded rectangle, which is what makes the morphs
possible: a rectangle whose corner radius equals half its side *is* a circle,
so a branch button can become the centre dial by interpolating two numbers.
Nothing is ever destroyed and rebuilt.
"""

from __future__ import annotations

import enum
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QElapsedTimer,
    QPointF,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QGuiApplication,
    QHideEvent,
    QKeyEvent,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import QWidget

from ..commands import lookup
from ..config import settings
from ..status import LIVE, WARN, Badge, snapshot
from ..transcript import transcript
from ..tree import Node, build_tree
from .foreground import force_foreground
from .highlight import OK as HL_OK
from .highlight import colour_for, spans
from .motion import chase, flicker_off, flicker_on, frame_interval, ms, step_seconds
from .panels import Panel, make_panel
from . import scrollback, suggest
from .suggest import Completer
from .paint import (
    ACCENT,
    BACKDROP,
    BAD,
    BAND_FILL,
    CELL_FILL,
    MUTED,
    PANEL_FILL,
    TEXT,
    advance_accent,
    blend,
    clamp01,
    fade,
    lerp,
    mono,
    smoothstep,
    smootherstep,
)
from .popup import (
    CARD_BG,
    CARD_BORDER,
    CARD_BORDER_W,
    CARD_CORNER,
    CARD_CONTENT_TOP,
    CARD_CONTENT_X,
    CARD_INPUT_X,
    CARD_PROMPT_DY,
    CARD_PROMPT_X,
    CARD_W,
    PLACEHOLDER,
)

log = logging.getLogger(__name__)


# The prompt capsule's own look, taken from the popup's stylesheet so the two
# windows hand over to each other without a visible jump.
CARD_FILL = QColor(CARD_BG)
CARD_EDGE = QColor(CARD_BORDER)

# ----------------------------------------------------------------- geometry

CENTRE_R = 150.0
NODE_W = 152.0
NODE_H = 58.0
NODE_CORNER = 8.0
ORBIT_MIN = 288.0
ORBIT_MAX = 430.0

#: The leaf frame hugs the screen edge and is sized to its panel's content, so
#: a short readout gets a short frame instead of a mostly-empty full-screen box.
PANEL_MARGIN = 22.0
PANEL_MIN_H = 210.0

#: The docked command line spans the full width, flush to the bottom edge.
COMMAND_H = 58.0
#: Vertical space it reserves out of the stage.
STAGE_FOOTER = COMMAND_H + 20.0

#: Breathing room above and below the scroll-back the command line grows
#: upward into. The rows themselves are measured by `scrollback`, which both
#: command lines draw through — the band is part of the band, not a floating
#: panel, and the accent rule that is the bar divides it from what you type.
SCROLL_PAD = 9.0
#: Kept clear between the lowest thing on the stage and the top of the
#: scroll-back. Wide enough for the argument hint drawn under a branch.
SCROLL_CLEAR = 26.0
#: What the band gets even when there is no room for it. On a cramped screen a
#: radial layout can reach most of the way to the command line, and a prompt
#: that silently swallows its own output is worse than one that covers the
#: bottom of a button.
SCROLL_MIN_ROWS = 3

#: Thickness of the flat line, and of the point that spreads into it.
LINE_H = 3.0
#: The prompt condenses to a point, not a circle.
BAR_POINT_D = 6.0
#: Flush against the very top edge of the screen.
BAR_TOP_Y = LINE_H / 2.0

# ---------------------------------------------------------------- animation

#: Baselines, scaled through `ms()` at the moment an animation starts so the
#: speed setting applies immediately. The named speeds in `config.py` are
#: calibrated against these, so shortening one here does not make the journey
#: better — it silently redefines what "cinematic" means.
OPEN_BASE = 4800
#: Escape does not replay the journey backwards — it takes the short way home,
#: so closing is deliberately brisk next to opening.
CLOSE_BASE = 1080

#: Linear, because the legs below already carry the easing — see the comment
#: under this one, which has always said so.
#:
#: This used to be InOutSine, which meant every leg was eased twice: once by
#: the curve on the driving animation and again by its own smootherstep. The
#: cost lands entirely at the two ends of the journey, and it scales with the
#: duration. At `cinematic` the capsule was under a tenth of the way balled up
#: after nine hundred milliseconds — not a gentle start but a dead one, and it
#: read as the card having hung. Driven linearly the same leg is a tenth of the
#: way in by 270ms, and smootherstep still takes its first and second
#: derivatives to zero at both ends, so nothing jerks at a handover.
OPEN_EASING = QEasingCurve.Type.Linear
CLOSE_EASING = QEasingCurve.Type.Linear

# Windows along the journey, as fractions of `intro`. The animation driving
# `intro` is linear; every curve is shaped here instead, on smootherstep, whose
# first *and* second derivatives vanish at both ends — no jerk at a handover.
#
# The windows deliberately OVERLAP. Sequencing them end-to-end would bring the
# capsule to a dead stop at every station; overlapping means it is already
# rising as it finishes balling up, and already falling as it finishes
# unrolling, so the whole journey is one continuous move.
BALL_UP = (0.00, 0.13)  # capsule balls up into a circle
RISE = (0.10, 0.34)  # circle rises to the top
FLATTEN = (0.31, 0.47)  # circle spreads into a flat line, edge to edge
DESCEND = (0.44, 0.80)  # the line sweeps all the way down to the bottom edge
LIFT = (0.79, 0.92)  # it lifts back up, opening the command line beneath it

#: The UI is wiped in behind the descending line in flat accent green. Nothing
#: about the colour moves until DESCEND[1] — the instant the line hits bottom.
COLOUR_IN = (0.80, 0.96)
PROMPT_IN = (0.86, 1.00)

GRID_STEP = 44.0
GRID_DRIFT = 7.0  # px/sec
RING_COUNT = 3
RING_PERIOD = 6.0  # sec
SCAN_PERIOD = 9.0  # sec
SCAN_BAND = 150.0
#: The dial's halo and its slowly sweeping arc.
GLOW_R = 1.55
SWEEP_PERIOD = 7.0  # sec
SWEEP_ARC = 62  # degrees

#: Typed at the navigator's own command line to close it.
CLOSE_WORDS = frozenset({"close", "exit", "quit", "back"})

#: Prompt insets for the docked command line, which the outro eases away from
#: toward the popup card's own (CARD_PROMPT_X / CARD_INPUT_X).
PROMPT_PAD = 26.0
INPUT_PAD = 122.0

#: How fast a changed status line strikes back on. Faster than a size chase —
#: it is a flicker, not a move, and it should be over before you read it.
STATUS_STRIKE_RATE = 26.0

#: Kept clear at each end of the top band for the breadcrumb and the key hints.
STATUS_RESERVE = 300.0
STATUS_GAP = 10.0
STATUS_PAD = 11.0

# ------------------------------------------------------------------ strokes
# Every outline weight in one place. Hairlines suit the drafting-table look;
# the bar's ends still resolve to the popup card's own 1px border.
STROKE_DIAL = 1.2
STROKE_DIAL_INNER = 0.7
STROKE_NODE = 0.9
STROKE_BRANCH = 0.9
STROKE_HALO = 0.7
STROKE_PANEL = 1.2
STROKE_RULE = 0.7
STROKE_BAR = 1.2

#: Each branch is routed as a staircase of axis-aligned legs rather than a
#: straight diagonal — it reads as a trace on a board, not a spoke on a wheel.
BRANCH_MIN_SEGMENTS = 2
BRANCH_MAX_SEGMENTS = 4
#: Clearance left at the dial and at the button, so a run touches neither.
BRANCH_CLEAR = 8.0
#: Half-width of the marker dropped at each turn.
BRANCH_JOINT = 1.6

# On open, the dial strikes on first and the branches follow it outward, one
# after another. Windows are fractions of `intro`; each runs the flicker
# waveform, so they stutter on rather than fading.
DIAL_STRIKE = (0.52, 0.66)
NODE_STRIKE_FROM = 0.64
#: Longest gap between one branch striking and the next. A ceiling, not the
#: figure used: the cascade has to *finish* inside the intro, so with enough
#: branches to overrun it they are dealt out faster instead. At the tuned step
#: the ninth root branch would not start until the opening was already over,
#: which is precisely as visible as it sounds — it never struck on at all, and
#: appeared at full brightness the moment the phase changed.
NODE_STRIKE_STEP = 0.055
NODE_STRIKE_LEN = 0.13


class Phase(enum.Enum):
    HIDDEN = enum.auto()
    INTRO = enum.auto()
    IDLE = enum.auto()
    FLICKER = enum.auto()
    TRAVEL = enum.auto()
    BLOOM = enum.auto()
    EXPAND = enum.auto()
    PANEL = enum.auto()
    OUTRO = enum.auto()


# ------------------------------------------------------------------- easing


def _centred(centre: QPointF, width: float, height: float) -> QRectF:
    return QRectF(
        centre.x() - width / 2.0, centre.y() - height / 2.0, width, height
    )


def _lerp_shape(
    a: tuple[QRectF, float], b: tuple[QRectF, float], t: float
) -> tuple[QRectF, float]:
    """Interpolate two rounded rectangles by centre, size and corner radius."""
    (ra, ca), (rb, cb) = a, b
    centre = QPointF(
        lerp(ra.center().x(), rb.center().x(), t),
        lerp(ra.center().y(), rb.center().y(), t),
    )
    width = lerp(ra.width(), rb.width(), t)
    height = lerp(ra.height(), rb.height(), t)
    return _centred(centre, width, height), lerp(ca, cb, t)


@dataclass
class _Step:
    """One leg of a transition: hold `phase` while driving `prop` start -> end."""

    phase: Phase
    prop: str
    start: float
    end: float
    duration: int
    easing: QEasingCurve.Type = QEasingCurve.Type.InOutCubic
    before: Callable[[], None] | None = None


class NavigatorWindow(QWidget):
    """Full-screen radial navigator over the command tree."""

    #: Emitted with the full path when a leaf is opened, e.g. "system volume".
    invoked = Signal(str)
    #: Emitted when a command is entered at the docked command line.
    submitted = Signal(str)
    #: Emitted once the overlay is gone, so the prompt can be handed back.
    closed = Signal()

    def __init__(self, root: Node | None = None) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.ArrowCursor)

        self._root = root if root is not None else build_tree()
        self._trail: list[Node] = [self._root]
        self._selected: int | None = None
        self._hover: int | None = None
        self._phase = Phase.HIDDEN
        self._origin: QRectF | None = None
        self._buffer = ""
        #: The open leaf's view, or None in the radial layout.
        self._panel: Panel | None = None
        #: Child indices still to be walked by a multi-level descent. A drill
        #: to a name is one descent per level, each starting when the last has
        #: landed, so the journey animates the whole way down. Not `_route`:
        #: that is the method that lays out a branch's staircase for painting.
        self._descent: list[int] = []
        #: How far above the origin card's bottom edge its prompt row sits.
        #: Half the docked band's height until a popup says otherwise, which is
        #: what a one-line card works out to anyway.
        self._prompt_rise = COMMAND_H / 2.0
        #: Last command result, shown beside the prompt.
        self._status = ""
        self._status_ok = True
        #: Whether closing should hand the prompt back (see dismiss()).
        self._hand_back = True
        self._completer = Completer()
        #: 0 while the wave is painting (flat accent), 1 once colours resolve.
        self._colour = 1.0
        #: Height the scroll-back is *currently drawn at*, eased toward what the
        #: transcript actually holds. Snapping the band taller the instant a
        #: command printed something would jolt the whole layout, since the
        #: stage is measured from it.
        self._scroll_v = 0.0
        #: The suggestion box's drawn height, chasing the list's real one, so
        #: it opens and shuts above the prompt instead of blinking.
        self._list_v = 0.0
        #: 0 the instant the status line changes, driven back to 1 through the
        #: flicker waveform — a new reading strikes on like everything else.
        self._status_v = 1.0

        self._intro_v = 0.0
        self._outro_v = 0.0
        self._flicker_v = 0.0
        self._travel_v = 0.0
        self._bloom_v = 0.0
        self._expand_v = 0.0

        self._chain: list[QPropertyAnimation] = []

        # Drives the living background. Stopped whenever the overlay is not on
        # screen — an always-resident daemon must not burn a timer while idle.
        self._clock = 0.0
        self._elapsed = QElapsedTimer()
        self._ticker = QTimer(self)
        # Precise, not coarse: Qt's default timer rounds to ~15ms on Windows,
        # which caps the overlay at 60fps however fast the panel is.
        self._ticker.setTimerType(Qt.TimerType.PreciseTimer)
        self._ticker.timeout.connect(self._tick)

        # Caches. The grid was ~90 drawLine calls per frame; it is now one
        # tiled blit. Shapes were recomputed several times per paint.
        self._grid_tile: QPixmap | None = None
        self._shape_cache: dict[tuple[int, int], list[tuple[QRectF, float]]] = {}
        self._cache_size = (0, 0)

        # The palette and the speed dial subscribe themselves; this listener
        # only has to react to what is specific to the overlay.
        settings.listeners.append(self._on_setting_changed)

    # ------------------------------------------------------------ frame rate

    def _frame_interval(self) -> int:
        """Milliseconds per frame, from the setting or the actual display."""
        return frame_interval(self.screen())

    def _tick(self) -> None:
        # Advance by real elapsed time, not by the nominal interval: a dropped
        # frame should not slow the animation down, it should skip.
        nanos = self._elapsed.nsecsElapsed()
        self._elapsed.restart()
        delta = step_seconds(nanos)
        self._clock += delta

        # Everything on the overlay that can change size or position without a
        # transition of its own is chased here, on the frame clock that is
        # already running for the background.
        self._scroll_v = chase(self._scroll_v, self._scroll_target(), delta)
        self._list_v = chase(self._list_v, suggest.height_for(self._completer), delta)
        self._status_v = chase(self._status_v, 1.0, delta, rate=STATUS_STRIKE_RATE)
        self._completer.advance(delta)
        if self._panel is not None:
            self._panel.advance(delta)
        if advance_accent(delta):
            # The grid tile bakes the accent in, so it has to be redrawn for
            # each step of the sweep rather than once at the end.
            self.invalidate_caches()
        self.update()

    def _on_setting_changed(self, key: str, value: object) -> None:
        if key == "frame_cap":
            self._ticker.setInterval(self._frame_interval())
        elif key == "accent":
            self.invalidate_caches()  # the grid tile bakes in the accent
        self.update()

    # --------------------------------------------------------- animated props

    def _get_intro(self) -> float:
        return self._intro_v

    def _set_intro(self, value: float) -> None:
        self._intro_v = value
        self.update()

    def _get_outro(self) -> float:
        return self._outro_v

    def _set_outro(self, value: float) -> None:
        self._outro_v = value
        self.update()

    def _get_flicker(self) -> float:
        return self._flicker_v

    def _set_flicker(self, value: float) -> None:
        self._flicker_v = value
        self.update()

    def _get_travel(self) -> float:
        return self._travel_v

    def _set_travel(self, value: float) -> None:
        self._travel_v = value
        self.update()

    def _get_bloom(self) -> float:
        return self._bloom_v

    def _set_bloom(self, value: float) -> None:
        self._bloom_v = value
        self.update()

    def _get_expand(self) -> float:
        return self._expand_v

    def _set_expand(self, value: float) -> None:
        self._expand_v = value
        self.update()

    intro = Property(float, _get_intro, _set_intro)
    outro = Property(float, _get_outro, _set_outro)
    flicker = Property(float, _get_flicker, _set_flicker)
    travel = Property(float, _get_travel, _set_travel)
    bloom = Property(float, _get_bloom, _set_bloom)
    expand = Property(float, _get_expand, _set_expand)

    # ------------------------------------------------------------- lifecycle

    @property
    def node(self) -> Node:
        return self._trail[-1]

    def open(
        self,
        origin: QRectF | None = None,
        then: str = "",
        prompt_rise: float | None = None,
    ) -> None:
        """Show the navigator, the prompt capsule travelling in from `origin`.

        `origin` is the popup's whole screen rectangle — scroll-back and all —
        so the capsule appears to be the very same card the command was just
        typed into, and balls up out of that shape instead of snapping to a
        bare prompt first. `prompt_rise` says how far above the card's bottom
        edge the prompt row's centre is, which is where the text has to go once
        the card is taller than one line. `then` names a node to drill into
        once the opening finishes, at whatever depth it sits — it runs the
        ordinary descend, so `settings` animates in rather than snapping.
        """
        if self._phase not in (Phase.HIDDEN, Phase.OUTRO):
            force_foreground(self)
            if then:
                self._descend_to(then)
            return

        self._stop_chain()
        self._trail = [self._root]
        self._selected = None
        self._hover = None
        self._buffer = ""
        self._panel = None
        self._descent = []
        self._flicker_v = self._travel_v = self._expand_v = 0.0
        self._bloom_v = 1.0
        self._intro_v = 0.0
        self._clock = 0.0
        # Snap rather than ease: the history was already on screen in the
        # popup, so the layout must open around it, not slide into it. The
        # suggestion box starts shut, and the status line starts fully struck.
        self._scroll_v = self._scroll_target()
        self._list_v = 0.0
        self._status_v = 1.0

        screen = QGuiApplication.screenAt(self.cursor().pos()) or QGuiApplication.primaryScreen()
        if screen is not None:
            self.setGeometry(screen.geometry())

        # Map the popup's global rectangle into our own coordinates.
        self._origin = (
            QRectF(
                origin.x() - self.x(), origin.y() - self.y(),
                origin.width(), origin.height(),
            )
            if origin is not None
            else None
        )
        self._prompt_rise = (
            COMMAND_H / 2.0 if prompt_rise is None else float(prompt_rise)
        )

        self.show()
        force_foreground(self)
        self._outro_v = 0.0
        self.invalidate_caches()
        self._elapsed.restart()
        self._ticker.setInterval(self._frame_interval())
        self._ticker.start()
        self._run_chain(
            [_Step(Phase.INTRO, "intro", 0.0, 1.0, ms(OPEN_BASE), OPEN_EASING)],
            lambda: self._on_opened(then),
        )

    def _on_opened(self, then: str) -> None:
        self._phase = Phase.IDLE
        if then:
            self._descend_to(then)

    def _descend_to(self, name: str) -> bool:
        """Drill to a node by name, however deep in the tree it sits.

        This used to search only the current node's children, which was right
        when the tree was hand-written and `settings` was a branch off the
        root. The tree is derived from the registry now, so the top level is
        the command *groups* and `settings` is a leaf inside `sentinel`. A
        one-level search could no longer find it: `settings` reported "opening
        settings", opened the navigator at the root ring, and left the panel
        it names nowhere to be seen.

        Breadth-first, so the shallowest match wins — a group and a leaf could
        share a name, and the branch is the more useful reading of `open x`.
        """
        descent = self._find_descent(name)
        if descent is None:
            return False
        self._descent = descent
        self._continue_descent()
        return True

    def _find_descent(self, name: str) -> list[int] | None:
        """Child indices leading from the current node down to `name`."""
        wanted = name.strip().lower()
        queue: list[tuple[Node, list[int]]] = [(self.node, [])]
        while queue:
            node, path = queue.pop(0)
            for index, child in enumerate(node.children):
                if child.name.lower() == wanted:
                    return [*path, index]
                queue.append((child, [*path, index]))
        return None

    def _continue_descent(self) -> None:
        """Take the next leg of a multi-level descent, if one is outstanding."""
        if self._descent:
            self._descend(self._descent.pop(0))

    def dismiss(self, hand_back: bool = True) -> None:
        """Collapse straight back to the command line.

        Deliberately *not* the opening journey in reverse. Escape means "put me
        back where I was typing", so the docked band takes the short way home:
        it folds directly into the prompt capsule while the scene fades, and the
        real popup takes over at exactly that spot.
        """
        if self._phase in (Phase.HIDDEN, Phase.OUTRO):
            return
        self._stop_chain()
        # Whatever we were drilling towards, we are not going there now.
        self._descent = []
        # After launching something, handing the prompt back would pull the
        # foreground off the window that is opening. Close and stay closed.
        self._hand_back = hand_back
        self._outro_v = 0.0
        # The popup summons with a cleared input, so the outro must show the
        # placeholder rather than a stale command.
        self._buffer = ""
        self._run_chain(
            [_Step(Phase.OUTRO, "outro", 0.0, 1.0, ms(CLOSE_BASE), CLOSE_EASING)],
            self._finish_dismiss,
        )

    def _finish_dismiss(self) -> None:
        self._phase = Phase.HIDDEN
        # Hand the prompt back *before* hiding. Hiding first would leave a
        # frame with neither window on screen, which reads as a blink.
        if self._hand_back:
            self.closed.emit()
        self.hide()
        # Reset only once we are off screen. Both of these repaint, and at zero
        # the paint they ask for is the bare origin rectangle with nothing
        # inside it — no prompt and no history, since both arrive right at the
        # end of the opening. Asking for that while the window was still up is
        # what put an empty card between the overlay's last frame and the
        # popup's first, at the very seam the outro exists to hide.
        self._intro_v = 0.0
        self._outro_v = 0.0

    def hideEvent(self, event: QHideEvent) -> None:
        self._ticker.stop()
        super().hideEvent(event)

    # ------------------------------------------------------------ transitions

    def _stop_chain(self) -> None:
        for anim in self._chain:
            anim.stop()
        self._chain.clear()

    def _run_chain(self, steps: list[_Step], done: Callable[[], None] | None = None) -> None:
        self._stop_chain()

        def advance(index: int) -> None:
            if index >= len(steps):
                if done is not None:
                    done()
                self.update()
                return
            step = steps[index]
            if step.before is not None:
                step.before()
            self._phase = step.phase
            anim = QPropertyAnimation(self, step.prop.encode(), self)
            anim.setStartValue(step.start)
            anim.setEndValue(step.end)
            anim.setDuration(step.duration)
            anim.setEasingCurve(QEasingCurve(step.easing))
            anim.finished.connect(lambda: advance(index + 1))
            self._chain.append(anim)
            anim.start()

        advance(0)

    def _descend(self, index: int) -> None:
        """Drill into child `index` of the current node."""
        if self._phase != Phase.IDLE or index >= len(self.node.children):
            return
        child = self.node.children[index]
        self._selected = index
        self._hover = None

        def enter() -> None:
            self._trail.append(child)
            self._selected = None
            if child.is_leaf:
                # Built before the frame opens, so _panel_shape can size to it.
                self._panel = make_panel(child)
                self._panel.request_close = lambda: self.dismiss(hand_back=False)
                # A command panel runs its command the same way the prompt
                # does, so the result lands in the shared scroll-back and the
                # panel can read its own output back out of it.
                self._panel.request_run = self.submitted.emit
                self._panel.enter()

        common = [
            _Step(Phase.FLICKER, "flicker", 0.0, 1.0, ms(440), QEasingCurve.Type.Linear),
            _Step(Phase.TRAVEL, "travel", 0.0, 1.0, ms(380), QEasingCurve.Type.InOutCubic),
        ]

        if child.is_leaf:
            self._run_chain(
                common
                + [
                    _Step(
                        Phase.EXPAND, "expand", 0.0, 1.0, ms(640),
                        QEasingCurve.Type.InOutCubic, before=enter,
                    )
                ],
                self._on_leaf_open,
            )
        else:
            self._run_chain(
                common
                + [
                    _Step(
                        Phase.BLOOM, "bloom", 0.0, 1.0, ms(560),
                        QEasingCurve.Type.OutCubic, before=enter,
                    )
                ],
                self._on_branch_open,
            )

    def _on_branch_open(self) -> None:
        # Idle first: the next leg descends only from a settled ring.
        self._phase = Phase.IDLE
        self._continue_descent()

    def _on_leaf_open(self) -> None:
        self._phase = Phase.PANEL
        self.invoked.emit(" ".join(n.name for n in self._trail[1:]))

    def _ascend(self) -> None:
        """Reverse the descent: unwind whichever morph brought us here."""
        # Going back outranks any descent still in progress — resuming one
        # would drag the user straight back down the level they just left.
        self._descent = []
        if len(self._trail) < 2:
            self.dismiss()
            return

        leaving = self._trail[-1]
        parent = self._trail[-2]
        index = parent.children.index(leaving)

        def leave() -> None:
            self._trail.pop()
            self._selected = index
            if self._panel is not None:
                self._panel.leave()
                self._panel = None

        opening = (
            _Step(Phase.EXPAND, "expand", 1.0, 0.0, ms(460), QEasingCurve.Type.InOutCubic)
            if self._phase == Phase.PANEL
            else _Step(Phase.BLOOM, "bloom", 1.0, 0.0, ms(420), QEasingCurve.Type.InCubic)
        )
        self._run_chain(
            [
                opening,
                _Step(
                    Phase.TRAVEL, "travel", 1.0, 0.0, ms(380),
                    QEasingCurve.Type.InOutCubic, before=leave,
                ),
                _Step(Phase.FLICKER, "flicker", 1.0, 0.0, ms(340), QEasingCurve.Type.Linear),
            ],
            self._on_ascended,
        )

    def _on_ascended(self) -> None:
        self._phase = Phase.IDLE
        self._selected = None
        self._bloom_v = 1.0
        self._expand_v = 0.0

    # -------------------------------------------------------------- geometry

    def _scroll_rows(self) -> list[tuple[str, str]]:
        return transcript.rows()

    def _scroll_room(self) -> float:
        """How tall the scroll-back may grow without covering the interface.

        The layout is never moved to make space. A command that prints
        something must not shunt the dial and every branch up the screen —
        which is what measuring the stage from the history did. So the history
        gets whatever room is genuinely free below the interface, and drops its
        oldest rows when that runs out: the same bargain the popup card strikes
        with the top of the screen.
        """
        floor = self.height() - COMMAND_H  # the docked band's top edge
        # Measured against the panel from the moment it exists, not from the
        # moment it finishes opening: otherwise the band swells during the
        # unfold and has to squeeze back down again as the frame arrives.
        if self._panel is not None:
            lowest = self._panel_shape(1.0)[0].bottom()
        else:
            shapes = self._node_shapes(len(self.node.children))
            lowest = max((rect.bottom() for rect, _ in shapes), default=0.0)
        # Never let it climb over the dial either, however few branches there are.
        lowest = max(lowest, self._centre().y() + CENTRE_R)
        free = max(0.0, floor - lowest - SCROLL_CLEAR)
        # ...but never nothing. A few rows are taken from the interface rather
        # than dropped, on a screen too short to give them up willingly.
        least = SCROLL_MIN_ROWS * scrollback.ROW_H + SCROLL_PAD * 2.0
        return max(free, min(least, self.height() * 0.3))

    def _scroll_target(self) -> float:
        """How tall the scroll-back wants to be, in pixels."""
        rows = self._scroll_rows()
        if not rows:
            return 0.0
        room = self._scroll_room()
        # Below one row there is no point opening at all; a band a few pixels
        # tall with nothing legible in it is just a strip of paint.
        if room < scrollback.ROW_H + SCROLL_PAD * 2.0:
            return 0.0
        return min(scrollback.height_for(rows) + SCROLL_PAD * 2.0, room)

    def _scroll_height(self) -> float:
        """How far the command line has grown upward, as currently drawn."""
        return self._scroll_v

    def _dock_block(
        self, band_top: float, scroll_h: float, rows: list[tuple[str, str]]
    ) -> QRectF:
        """Where the docked scroll-back's rows go, given the band's top edge.

        Anchored to the bottom rather than sized from the row count: while the
        block is animating open there is less room than there are rows, and the
        newest has to hold its place against the prompt while the oldest slide
        out under the top edge.
        """
        return QRectF(
            PROMPT_PAD,
            band_top - scroll_h + SCROLL_PAD,
            max(0.0, self.width() - PROMPT_PAD * 2.0),
            max(0.0, scroll_h - SCROLL_PAD * 2.0),
        )

    def _card_block(self, card: QRectF, rows: list[tuple[str, str]]) -> QRectF:
        """Where the popup card lays those same rows out, inside `card`.

        Read off the popup's own padding constants, because the outro has to
        land on the card the popup is about to draw for itself.
        """
        return QRectF(
            card.x() + CARD_CONTENT_X,
            card.y() + CARD_CONTENT_TOP,
            max(0.0, card.width() - CARD_CONTENT_X * 2.0),
            scrollback.height_for(rows),
        )

    def _stage(self) -> QRectF:
        """The area above the docked command line.

        Fixed. It used to be measured from the scroll-back, so that running one
        command that printed anything shrank the orbit by a fifth and slid the
        whole dial up the screen. The history now fits itself around the
        interface instead — see `_scroll_room`.
        """
        return QRectF(0.0, 0.0, float(self.width()), self.height() - STAGE_FOOTER)

    def _centre(self) -> QPointF:
        return self._stage().center()

    def _orbit_radius(self) -> float:
        stage = self._stage()
        span = min(stage.width(), stage.height()) * 0.36
        return min(max(span, ORBIT_MIN), ORBIT_MAX)

    def _orbit_points(self, count: int, scale: float = 1.0) -> list[QPointF]:
        """Evenly spaced around the centre, first branch straight up."""
        if count == 0:
            return []
        centre = self._centre()
        radius = self._orbit_radius() * scale
        step = 2.0 * math.pi / count
        return [
            QPointF(
                centre.x() + radius * math.cos(-math.pi / 2.0 + i * step),
                centre.y() + radius * math.sin(-math.pi / 2.0 + i * step),
            )
            for i in range(count)
        ]

    def _node_shapes(self, count: int, scale: float = 1.0) -> list[tuple[QRectF, float]]:
        # Called several times per frame with the same arguments; the geometry
        # only changes when the window resizes or the branch count does. The
        # scroll-back no longer comes into it, which is also what stops this
        # being invalidated on every frame of a chase.
        size = (self.width(), self.height())
        if size != self._cache_size:
            self._shape_cache.clear()
            self._cache_size = size
        key = (count, int(scale * 2048))
        cached = self._shape_cache.get(key)
        if cached is None:
            cached = [
                (_centred(p, NODE_W * scale, NODE_H * scale), NODE_CORNER * scale)
                for p in self._orbit_points(count, scale)
            ]
            self._shape_cache[key] = cached
        return cached

    def _centre_shape(self) -> tuple[QRectF, float]:
        return _centred(self._centre(), CENTRE_R * 2.0, CENTRE_R * 2.0), CENTRE_R

    def _panel_shape(self, t: float) -> tuple[QRectF, float]:
        """Morph a branch button at the centre into the leaf frame.

        The frame is only as tall as its panel needs, and sits close to the
        screen edge — a two-line readout should not open a full-screen box.
        """
        start = (_centred(self._centre(), NODE_W, NODE_H), NODE_CORNER)
        stage = self._stage().adjusted(
            PANEL_MARGIN, PANEL_MARGIN, -PANEL_MARGIN, -PANEL_MARGIN
        )
        wanted = self._panel.content_height() if self._panel else PANEL_MIN_H
        height = max(PANEL_MIN_H, min(stage.height(), wanted))
        end_rect = _centred(stage.center(), stage.width(), height)
        return _lerp_shape(start, (end_rect, 10.0), t)

    # -- the travelling command bar -----------------------------------------

    def _origin_shape(self) -> tuple[QRectF, float]:
        """The popup's own rectangle — and its own corner radius.

        Not a pill (height/2): the popup card is a 10px-radius rectangle, and
        rounding this differently is what made the handover snap.
        """
        if self._origin is not None:
            return self._origin, float(CARD_CORNER)
        # No popup handed us one — a bare `open()`. Fall back to where the card
        # summons, at the height it has with nothing in it.
        rect = QRectF(
            (self.width() - CARD_W) / 2.0, self.height() * 0.26,
            float(CARD_W), COMMAND_H,
        )
        return rect, float(CARD_CORNER)

    def _command_shape(self) -> tuple[QRectF, float]:
        """The docked command line: a square-cornered band across the bottom.

        Its scroll-back is part of it, not a panel floating over it — which is
        what lets the outro fold the whole thing into the popup card as one
        shape instead of dropping the history and folding what is left.
        """
        height = COMMAND_H + self._scroll_height()
        return (
            QRectF(0.0, self.height() - height, float(self.width()), height),
            0.0,
        )

    def _bar_shape(self, t: float) -> tuple[QRectF, float]:
        """Where the prompt capsule is, and what shape it holds, at `intro` == t.

        Shape and position run on separate overlapping timelines rather than as
        one chain of stations. That is what keeps the journey fluid: the capsule
        never has to arrive somewhere and stop before the next thing can start.
        """
        origin, origin_corner = self._origin_shape()
        half = BAR_POINT_D / 2.0
        full_w, full_h = float(self.width()), float(self.height())

        # Shape: capsule -> a point -> a flat line spanning the whole screen.
        ball = smootherstep(*BALL_UP, t)
        flat = smootherstep(*FLATTEN, t)
        width = lerp(lerp(origin.width(), BAR_POINT_D, ball), full_w, flat)
        height = lerp(lerp(origin.height(), BAR_POINT_D, ball), LINE_H, flat)
        corner = lerp(lerp(origin_corner, half, ball), 0.0, flat)

        # Position: origin -> top -> all the way to the bottom edge -> lift up
        # by exactly the command line's height, opening it underneath.
        rise = smootherstep(*RISE, t)
        descend = smootherstep(*DESCEND, t)
        lift = smootherstep(*LIFT, t)
        centre_y = lerp(
            lerp(
                lerp(origin.center().y(), BAR_TOP_Y, rise),
                full_h,
                descend,
            ),
            full_h - COMMAND_H,
            lift,
        )
        centre_x = lerp(origin.center().x(), full_w / 2.0, rise)
        return _centred(QPointF(centre_x, centre_y), width, height), corner

    def _reveal(self, t: float) -> float:
        """How far down the screen the UI has been wiped in.

        The front rides the bottom edge of the descending bar, so the interface
        appears to be painted into existence by the bar itself.
        """
        height = float(self.height())
        if t <= DESCEND[0]:
            return 0.0
        if t >= DESCEND[1]:
            # The line has reached the bottom edge; everything is uncovered and
            # stays that way, so the lift back up does not un-reveal anything.
            return height
        # Derived from the descent alone rather than from the line's live
        # position. The lift overlaps the tail of the descent, and the flatten
        # overlaps its head — folding either in would drag the front backwards.
        return lerp(BAR_TOP_Y, height, smootherstep(*DESCEND, t))

    # ----------------------------------------------------------------- input

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._phase == Phase.PANEL and self._panel is not None:
            # An open panel owns the pointer: its buttons and rows light up
            # under it, and the cursor says which of them can be pressed.
            if self._panel.hover(event.position()):
                self.update()
            over = any(
                rect.contains(event.position()) for rect in self._panel.targets()
            )
            self.setCursor(
                Qt.CursorShape.PointingHandCursor if over
                else Qt.CursorShape.ArrowCursor
            )
            return

        hover = self._hit_test(event.position()) if self._phase == Phase.IDLE else None
        if hover != self._hover:
            self._hover = hover
            self.setCursor(
                Qt.CursorShape.PointingHandCursor
                if hover is not None
                else Qt.CursorShape.ArrowCursor
            )
            self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            self._ascend()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._phase == Phase.PANEL:
            # Clicking inside an open panel is the panel's business; only a
            # click on the surrounding backdrop steps back out.
            frame, _ = self._panel_shape(self._expand_v)
            if not frame.contains(event.position()):
                self._ascend()
                return
            if self._panel is not None and self._panel.click(event.position()):
                self.update()
            return
        if self._phase != Phase.IDLE:
            return

        index = self._hit_test(event.position())
        if index is not None:
            self._descend(index)
            return
        centre_rect, _ = self._centre_shape()
        command_rect, _ = self._command_shape()
        if not centre_rect.contains(event.position()) and not command_rect.contains(
            event.position()
        ):
            # A click on empty space steps back out, like closing a menu.
            self._ascend()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()

        panel = self._panel if self._phase == Phase.PANEL else None

        # Escape belongs to the navigator, unless the panel has something open
        # inside it — a capture line — to back out of first.
        if key == Qt.Key.Key_Escape:
            if panel is not None and panel.escape():
                self.update()
                return
            self._ascend()
            return

        if panel is not None:
            if key == Qt.Key.Key_Backspace:
                if panel.backspace():
                    self.update()
                    return
                self._ascend()
                return
            if panel.key(event):
                self.update()
                return
            text = event.text()
            if panel.wants_text and text and len(text) == 1 and text.isprintable():
                panel.text(text)
                self.update()
                return

        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._commit()
            return

        if key == Qt.Key.Key_Backspace:
            if self._buffer:
                self._buffer = self._buffer[:-1]
                self._completer.refresh(self._buffer)
                self.update()
            else:
                self._ascend()
            return

        # With a suggestion list open it owns Tab and the arrows; otherwise Tab
        # goes back to cycling the branches around the dial.
        if self._completer.active:
            if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                self._buffer = self._completer.accept(self._buffer)
                self.update()
                return
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                self._completer.move(1 if key == Qt.Key.Key_Down else -1)
                self.update()
                return

        if key == Qt.Key.Key_Tab:
            self._cycle_highlight(1)
            return
        if key == Qt.Key.Key_Backtab:
            self._cycle_highlight(-1)
            return

        text = event.text()
        if text and len(text) == 1 and text.isprintable():
            self._buffer += text
            self._completer.refresh(self._buffer)
            self.update()
            return

        super().keyPressEvent(event)

    def set_status(self, message: str, ok: bool = True) -> None:
        """Show a command's outcome above the docked prompt.

        Restarts the strike, so a new line arrives rather than appearing
        between two frames where the old one was.
        """
        if message != self._status:
            self._status_v = 0.0
        self._status = message
        self._status_ok = ok
        self.update()

    def _cycle_highlight(self, direction: int) -> None:
        count = len(self.node.children)
        if self._phase != Phase.IDLE or count == 0:
            return
        current = self._hover
        self._hover = 0 if current is None else (current + direction) % count
        self.update()

    def _commit(self) -> None:
        """Enter: run the typed command, or open the highlighted branch."""
        command = self._buffer.strip()
        self._buffer = ""
        self._completer.dismiss()
        if not command:
            if self._phase == Phase.IDLE and self._hover is not None:
                self._descend(self._hover)
            else:
                self.update()
            return
        if command.lower() in CLOSE_WORDS:
            self.dismiss()
            return
        self.submitted.emit(command)
        self.update()

    def _hit_test(self, pos: QPointF) -> int | None:
        for index, (rect, _) in enumerate(self._node_shapes(len(self.node.children))):
            if rect.contains(pos):
                return index
        return None

    # ---------------------------------------------------------------- drawing

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        if self._outro_v > 0.0:
            self._paint_outro(painter)
            return

        t = self._intro_v
        self._colour = smootherstep(*COLOUR_IN, t)

        # Nothing exists below the wave front — not the backdrop, not the grid,
        # not the dial. The desktop shows through until the bar sweeps past.
        reveal = self._reveal(t)
        if reveal > 0.5:
            painter.save()
            painter.setClipRect(QRectF(0.0, 0.0, float(self.width()), reveal))
            painter.fillRect(self.rect(), BACKDROP)
            self._paint_background(painter, 1.0)
            if self._phase in (Phase.EXPAND, Phase.PANEL):
                self._paint_leaf(painter, 1.0)
            else:
                self._paint_radial(painter, 1.0)
            self._paint_chrome(painter, 1.0)
            painter.restore()

        self._paint_bar(painter, t)

    def _paint_outro(self, painter: QPainter) -> None:
        """The scene lets go while the band folds back into the prompt."""
        u = self._outro_v
        self._colour = 1.0

        scene = 1.0 - smootherstep(0.0, 0.62, u)
        if scene > 0.0:
            painter.save()
            painter.setOpacity(scene)
            painter.fillRect(self.rect(), BACKDROP)
            self._paint_background(painter, 1.0)
            if self._phase in (Phase.EXPAND, Phase.PANEL):
                self._paint_leaf(painter, 1.0)
            else:
                self._paint_radial(painter, 1.0)
            self._paint_chrome(painter, 1.0)
            painter.restore()

        journey = smootherstep(0.0, 1.0, u)
        band, origin = self._command_shape(), self._origin_shape()
        rect, corner = _lerp_shape(band, origin, journey)

        # Converge on the popup card's exact fill, border colour and border
        # width. The last frame drawn here has to be indistinguishable from the
        # popup that replaces it, or the handover snaps.
        settle = smootherstep(0.45, 1.0, u)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(blend(BAND_FILL, CARD_FILL, settle))
        painter.drawRoundedRect(rect, corner, corner)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(
            QPen(
                blend(ACCENT, CARD_EDGE, settle),
                lerp(STROKE_BAR, float(CARD_BORDER_W), settle),
            )
        )
        painter.drawRoundedRect(rect, corner, corner)

        # The scroll-back rides along, sliding and narrowing from the band's
        # full width onto the card's. It is drawn at full strength throughout:
        # this is the same history the popup is about to show, and blinking it
        # out and back in is the snap the whole outro exists to avoid.
        rows = self._scroll_rows()
        if rows:
            block, _ = _lerp_shape(
                (self._dock_block(band[0].bottom() - COMMAND_H,
                                  self._scroll_height(), rows), 0.0),
                (self._card_block(origin[0], rows), 0.0),
                journey,
            )
            scrollback.paint(painter, block, rows, 1.0)

        # The prompt stays lit the whole way and slides onto the card's own
        # layout. Fading it out would leave a blank final frame and the popup's
        # text would appear from nowhere — which reads as a snap. It is placed
        # from the shape's *bottom* edge, since both surfaces put the prompt at
        # the bottom and only the card carries anything above it.
        centre = rect.bottom() - lerp(COMMAND_H / 2.0, self._prompt_rise, settle)
        baseline = centre + lerp(5.0, CARD_PROMPT_DY, settle)
        painter.setFont(mono(15, bold=True))
        painter.setPen(ACCENT)
        painter.drawText(
            QPointF(rect.x() + lerp(PROMPT_PAD, CARD_PROMPT_X, settle), baseline),
            "sentinel>",
        )
        painter.setFont(mono(15))
        # Qt draws a placeholder at half the text colour's alpha; match that.
        painter.setPen(QColor(TEXT.red(), TEXT.green(), TEXT.blue(), 128))
        painter.drawText(
            QPointF(rect.x() + lerp(INPUT_PAD, CARD_INPUT_X, settle), baseline),
            PLACEHOLDER,
        )

    def _tint(self, color: QColor, alpha: float) -> QColor:
        """Hold every foreground colour at the accent until the bar docks.

        While the wave is still painting, the interface is one flat green; the
        whites and greys only resolve once the command line is home.
        """
        mix = 1.0 - self._colour
        if mix <= 0.001:
            return fade(color, alpha)
        blended = QColor.fromRgbF(
            lerp(color.redF(), ACCENT.redF(), mix),
            lerp(color.greenF(), ACCENT.greenF(), mix),
            lerp(color.blueF(), ACCENT.blueF(), mix),
        )
        blended.setAlphaF(clamp01(color.alphaF() * alpha))
        return blended

    # -- living background ---------------------------------------------------

    def _grid_pixmap(self) -> QPixmap:
        """One grid cell, drawn once and tiled.

        Drawing the grid line by line cost ~90 drawLine calls every frame. At
        120fps that is 11k calls a second for something that never changes
        shape — only its offset does.
        """
        ratio = self.devicePixelRatioF()
        step = int(round(GRID_STEP * ratio))
        if self._grid_tile is None or self._grid_tile.width() != step:
            tile = QPixmap(step, step)
            tile.setDevicePixelRatio(ratio)
            tile.fill(Qt.GlobalColor.transparent)
            scratch = QPainter(tile)
            scratch.setPen(QPen(fade(ACCENT, 0.07), 1.0))
            scratch.drawLine(0, 0, step, 0)
            scratch.drawLine(0, 0, 0, step)
            scratch.end()
            self._grid_tile = tile
        return self._grid_tile

    def invalidate_caches(self) -> None:
        self._grid_tile = None
        self._shape_cache.clear()

    def _paint_background(self, painter: QPainter, alpha: float) -> None:
        if not settings.get("effects"):
            return
        width, height = float(self.width()), float(self.height())
        centre = self._centre()

        if settings.get("glow"):
            # A soft halo under the dial. Cheap, and it lifts the centre off
            # the backdrop so the ring does not have to carry the weight.
            reach = CENTRE_R * GLOW_R * 2.0
            halo = QRadialGradient(centre, reach / 2.0)
            halo.setColorAt(0.0, fade(ACCENT, 0.10 * alpha))
            halo.setColorAt(0.55, fade(ACCENT, 0.03 * alpha))
            halo.setColorAt(1.0, fade(ACCENT, 0.0))
            painter.fillRect(_centred(centre, reach, reach), halo)

        if settings.get("grid"):
            offset = (self._clock * GRID_DRIFT) % GRID_STEP
            painter.save()
            painter.setOpacity(alpha)
            painter.drawTiledPixmap(
                self.rect(), self._grid_pixmap(), QPointF(-offset, -offset).toPoint()
            )
            painter.restore()

        if settings.get("rings"):
            # Radar rings breathing outward — the Sentinel is looking.
            reach = math.hypot(width, height) * 0.55
            for i in range(RING_COUNT):
                phase = ((self._clock / RING_PERIOD) + i / RING_COUNT) % 1.0
                radius = phase * reach
                if radius < 1.0:
                    continue
                strength = (1.0 - phase) ** 2 * 0.22 * alpha
                painter.setPen(QPen(fade(ACCENT, strength), 1.0))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(_centred(centre, radius * 2.0, radius * 2.0))

        if settings.get("scanline"):
            scan_y = ((self._clock / SCAN_PERIOD) % 1.0) * (height + SCAN_BAND) - SCAN_BAND
            gradient = QLinearGradient(0.0, scan_y, 0.0, scan_y + SCAN_BAND)
            gradient.setColorAt(0.0, fade(ACCENT, 0.0))
            gradient.setColorAt(0.5, fade(ACCENT, 0.05 * alpha))
            gradient.setColorAt(1.0, fade(ACCENT, 0.0))
            painter.fillRect(QRectF(0.0, scan_y, width, SCAN_BAND), gradient)

    def _paint_sweep(self, painter: QPainter, alpha: float) -> None:
        """A bright arc creeping round the dial, like a radar head."""
        if not (settings.get("effects") and settings.get("rings")):
            return
        rect = _centred(self._centre(), CENTRE_R * 2.0 + 16.0, CENTRE_R * 2.0 + 16.0)
        start = int((-self._clock / SWEEP_PERIOD % 1.0) * 360.0)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        # Drawn in a few steps so it fades along its own length.
        steps = 5
        for i in range(steps):
            strength = (1.0 - i / steps) * 0.5 * alpha
            painter.setPen(QPen(fade(ACCENT, strength), STROKE_DIAL))
            painter.drawArc(
                rect,
                (start - i * SWEEP_ARC // steps) * 16,
                -(SWEEP_ARC // steps) * 16,
            )

    # -- radial scene --------------------------------------------------------

    def _paint_radial(self, painter: QPainter, alpha: float) -> None:
        node = self.node
        centre_rect, centre_corner = self._centre_shape()

        if self._phase == Phase.INTRO:
            # The dial strikes on first, then each branch in turn.
            count = len(node.children)
            for i, (rect, corner) in enumerate(self._node_shapes(count)):
                lit = self._strike_alpha(self._node_strike(i, count)) * alpha
                if lit <= 0.0:
                    continue
                self._paint_branches(painter, node, lit, 1.0, None, only=i)
                self._paint_cell(painter, rect, corner, node.children[i].name, lit)
            self._paint_cell(
                painter, centre_rect, centre_corner, f"/{node.name}",
                self._strike_alpha(DIAL_STRIKE) * alpha, primary=True,
            )
            return

        if self._phase == Phase.FLICKER:
            dim = flicker_off(self._flicker_v) * alpha
            self._paint_branches(painter, node, dim, 1.0, self._selected)
            self._paint_cell(
                painter, centre_rect, centre_corner, f"/{node.name}", dim, primary=True
            )
            for i, (rect, corner) in enumerate(self._node_shapes(len(node.children))):
                chosen = i == self._selected
                self._paint_cell(
                    painter, rect, corner, node.children[i].name,
                    alpha if chosen else dim, hot=chosen,
                )
            return

        if self._phase == Phase.TRAVEL and self._selected is not None:
            shapes = self._node_shapes(len(node.children))
            start_rect, corner = shapes[self._selected]
            moved = _centred(
                QPointF(
                    lerp(start_rect.center().x(), self._centre().x(), self._travel_v),
                    lerp(start_rect.center().y(), self._centre().y(), self._travel_v),
                ),
                NODE_W,
                NODE_H,
            )
            self._paint_cell(
                painter, moved, corner, node.children[self._selected].name, alpha, hot=True
            )
            return

        if self._phase == Phase.BLOOM:
            t = self._bloom_v
            morph = smoothstep(0.0, 0.55, t)
            rect, corner = _lerp_shape(
                (_centred(self._centre(), NODE_W, NODE_H), NODE_CORNER),
                (centre_rect, centre_corner),
                morph,
            )
            grow = smoothstep(0.15, 1.0, t)
            children_alpha = smoothstep(0.45, 1.0, t) * alpha
            if grow > 0.0:
                self._paint_branches(painter, node, children_alpha, grow, None)
                for i, (child_rect, child_corner) in enumerate(
                    self._node_shapes(len(node.children), grow)
                ):
                    self._paint_cell(
                        painter, child_rect, child_corner,
                        node.children[i].name, children_alpha,
                        font_size=max(1, int(15 * grow)),
                    )
            self._paint_cell(
                painter, rect, corner, f"/{node.name}", alpha, primary=True,
                font_size=int(lerp(15.0, 34.0, morph)),
            )
            return

        # IDLE, and anything without a layout of its own (OUTRO, which fades
        # the whole scene at the painter level rather than per element).
        self._paint_branches(painter, node, alpha, 1.0, self._hover)
        self._paint_sweep(painter, alpha)
        self._paint_cell(
            painter, centre_rect, centre_corner, f"/{node.name}", alpha, primary=True
        )
        for i, (rect, corner) in enumerate(self._node_shapes(len(node.children))):
            self._paint_cell(
                painter, rect, corner, node.children[i].name, alpha,
                hot=(i == self._hover),
            )
            self._paint_usage(painter, rect, node.children[i], alpha,
                              hot=(i == self._hover))

    def _paint_usage(
        self, painter: QPainter, rect: QRectF, node: Node, alpha: float,
        hot: bool = False,
    ) -> None:
        """The command's exact syntax, under its button.

        Drawn outside the button rather than inside it: the argument shapes are
        wider than 152px and eliding them would defeat the point, whereas the
        gaps between branches are wide enough to take the whole line.
        """
        command = lookup(node.command) if node.is_leaf and node.command else None
        if command is None:
            return
        # A command that takes no arguments would only repeat the button's own
        # label, so it gets nothing.
        hint = command.argument_hint
        if not hint:
            return
        painter.setFont(mono(11))
        width = painter.fontMetrics().horizontalAdvance(hint)
        painter.setPen(fade(ACCENT if hot else MUTED, (0.95 if hot else 0.75) * alpha))
        painter.drawText(
            QPointF(rect.center().x() - width / 2.0, rect.bottom() + 17.0), hint
        )

    # -- opening stagger -----------------------------------------------------

    def _node_strike(self, index: int, count: int) -> tuple[float, float]:
        """When branch `index` of `count` strikes on, as a slice of the intro.

        The step is squeezed to fit rather than fixed. A branch whose window
        has not opened by the time the intro ends is a branch that never
        flickers on — it simply appears, fully lit, when the phase changes —
        so the last one's window has to close on the intro's last frame, not
        after it.
        """
        room = max(0.0, 1.0 - NODE_STRIKE_FROM - NODE_STRIKE_LEN)
        step = min(NODE_STRIKE_STEP, room / max(1, count - 1))
        start = NODE_STRIKE_FROM + index * step
        return start, start + NODE_STRIKE_LEN

    def _strike_alpha(self, window: tuple[float, float]) -> float:
        """Flicker an element on across its slice of the opening."""
        low, high = window
        if self._intro_v <= low:
            return 0.0
        if self._intro_v >= high or high <= low:
            return 1.0
        return flicker_on((self._intro_v - low) / (high - low))

    def _route(self, index: int, point: QPointF, grow: float) -> list[QPointF]:
        """An orthogonal staircase from the dial's edge to a branch button.

        Never diagonal: every leg runs along one axis, and 2-4 of them stepping
        alternately is what makes the pattern. The leg count comes from the
        index rather than a random draw, so a branch keeps its shape frame to
        frame instead of shimmering.

        Worked in *major/minor* terms rather than x/y. The major axis is the
        one the button mostly lies along; the run leaves the dial's pole on
        that axis and steps sideways on the minor one. Doing it in x and y
        instead means a button sitting dead in line with the dial gets a
        sideways leg it has no room for, and the run crosses the very button
        it is pointing at.
        """
        centre = self._centre()
        dx, dy = point.x() - centre.x(), point.y() - centre.y()
        if math.hypot(dx, dy) <= 1.0:
            return []

        # Rotate into the frame where the button is "ahead" and "off to one
        # side", and everything below is one case instead of four.
        upright = abs(dy) > abs(dx)
        major, minor = (dy, dx) if upright else (dx, dy)
        reach_major, reach_minor = (
            (NODE_H, NODE_W) if upright else (NODE_W, NODE_H)
        )
        reach_major = reach_major / 2.0 * grow + BRANCH_CLEAR
        reach_minor = reach_minor / 2.0 * grow + BRANCH_CLEAR
        radius = CENTRE_R * grow + BRANCH_CLEAR

        ahead = math.copysign(1.0, major)
        aside = math.copysign(1.0, minor) if minor else 1.0
        start = ahead * radius              # leaving the dial, on the major axis
        face = major - ahead * reach_major  # the near face, square on
        flank = minor - aside * reach_minor  # the side face, from beside

        # Whether there is room to turn between the dial and the button, and
        # whether the button is far enough off-axis to be approached from
        # beside it. Geometry overrules the requested count: a run that would
        # cross the dial or the very button it points at is not a style choice.
        pivot = radius + BRANCH_CLEAR
        roomy = abs(face) > pivot + 1.0
        sideways = abs(minor)

        spread = BRANCH_MAX_SEGMENTS - BRANCH_MIN_SEGMENTS + 1
        legs = BRANCH_MIN_SEGMENTS + (index % spread)
        if legs == 4 and not (roomy and sideways >= reach_minor * 2.2):
            legs = 3
        if legs == 3 and not roomy:
            legs = 2
        if legs == 2 and sideways < reach_minor * 1.05:
            legs = 1

        # Where the run steps sideways, held clear of the dial at one end and
        # of the button at the other whatever the orbit works out to.
        turn = ahead * min(max((radius + abs(face)) / 2.0, pivot), abs(face))
        if legs == 1:
            path = [(start, 0.0), (face, 0.0)]
        elif legs == 2:
            path = [(start, 0.0), (major, 0.0), (major, flank)]
        elif legs == 4:
            path = [
                (start, 0.0), (turn, 0.0), (turn, minor / 2.0),
                (major, minor / 2.0), (major, flank),
            ]
        else:
            path = [(start, 0.0), (turn, 0.0), (turn, minor), (face, minor)]

        if upright:
            return [QPointF(centre.x() + n, centre.y() + m) for m, n in path]
        return [QPointF(centre.x() + m, centre.y() + n) for m, n in path]

    def _paint_branches(
        self,
        painter: QPainter,
        node: Node,
        alpha: float,
        grow: float,
        hot: int | None,
        only: int | None = None,
    ) -> None:
        if alpha <= 0.0:
            return
        for i, point in enumerate(self._orbit_points(len(node.children), grow)):
            if only is not None and i != only:
                continue
            route = self._route(i, point, grow)
            if len(route) < 2:
                continue
            live = i == hot
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(
                QPen(
                    fade(ACCENT, (0.55 if live else 0.28) * alpha),
                    STROKE_BRANCH,
                )
            )
            for start, end in zip(route, route[1:]):
                # A leg that works out to nothing — a button dead in line with
                # the dial has no sideways travel to make — is skipped rather
                # than drawn as a dot at the corner.
                if abs(end.x() - start.x()) + abs(end.y() - start.y()) < 1.0:
                    continue
                painter.drawLine(start, end)

            # A small square at each turn, the way a trace on a board is
            # tagged where it changes direction. It also visually welds the
            # legs together, so the corner reads as one run bending.
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fade(ACCENT, (0.7 if live else 0.34) * alpha))
            for corner in route[1:-1]:
                painter.drawRect(
                    QRectF(
                        corner.x() - BRANCH_JOINT, corner.y() - BRANCH_JOINT,
                        BRANCH_JOINT * 2.0, BRANCH_JOINT * 2.0,
                    )
                )
            painter.setBrush(Qt.BrushStyle.NoBrush)

    def _paint_cell(
        self,
        painter: QPainter,
        rect: QRectF,
        corner: float,
        label: str,
        alpha: float,
        hot: bool = False,
        primary: bool = False,
        font_size: int | None = None,
    ) -> None:
        if alpha <= 0.0 or rect.width() <= 1.0 or rect.height() <= 1.0:
            return

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fade(CELL_FILL, alpha))
        painter.drawRoundedRect(rect, corner, corner)

        if hot:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(fade(ACCENT, 0.25 * alpha), STROKE_HALO))
            halo = rect.adjusted(-6.0, -6.0, 6.0, 6.0)
            painter.drawRoundedRect(halo, corner + 6.0, corner + 6.0)

        painter.setBrush(Qt.BrushStyle.NoBrush)
        ring = 0.95 if (primary or hot) else 0.5
        painter.setPen(
            QPen(fade(ACCENT, ring * alpha), STROKE_DIAL if primary else STROKE_NODE)
        )
        painter.drawRoundedRect(rect, corner, corner)

        if primary:
            inset = min(9.0, rect.width() / 6.0)
            painter.setPen(QPen(fade(ACCENT, 0.18 * alpha), STROKE_DIAL_INNER))
            painter.drawRoundedRect(
                rect.adjusted(inset, inset, -inset, -inset),
                max(0.0, corner - inset),
                max(0.0, corner - inset),
            )

        size = font_size if font_size is not None else (34 if primary else 15)
        painter.setFont(mono(size, bold=primary))
        painter.setPen(self._tint(ACCENT if (primary or hot) else TEXT, alpha))
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), label)

    # -- leaf panel ----------------------------------------------------------

    def _paint_leaf(self, painter: QPainter, alpha: float) -> None:
        t = self._expand_v
        rect, corner = self._panel_shape(t)
        node = self.node

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fade(PANEL_FILL, smoothstep(0.05, 0.6, t) * alpha))
        painter.drawRoundedRect(rect, corner, corner)

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(fade(ACCENT, alpha), STROKE_PANEL))
        painter.drawRoundedRect(rect, corner, corner)

        # The label rides the outline out, fading as the frame opens up.
        vanish = 1.0 - smoothstep(0.0, 0.32, t)
        if vanish > 0.0:
            painter.setFont(mono(15, bold=True))
            painter.setPen(fade(ACCENT, vanish * alpha))
            painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), node.name)

        body = smoothstep(0.68, 1.0, t) * alpha
        if body > 0.0 and self._panel is not None:
            painter.save()
            painter.setClipRect(rect)
            self._panel.paint(painter, rect, body)
            painter.restore()

    # -- the command bar -----------------------------------------------------

    def _paint_bar(self, painter: QPainter, t: float) -> None:
        rect, corner = self._bar_shape(t)
        # Solid from the moment it condenses: an outlined 6px dot would read as
        # a tiny ring, and the line it becomes should be a solid rule.
        solid = smootherstep(*BALL_UP, t)
        bottom = float(self.height())

        # The band the line opens up as it lifts off the bottom edge. Its
        # height comes from the lift alone — deriving it from the line's
        # position would fill the whole lower screen during the descent and
        # cover the very desktop the wave is meant to be uncovering.
        band_h = COMMAND_H * smootherstep(*LIFT, t)
        band_top = bottom - band_h

        # The scroll-back is part of the band, not a panel floating over it:
        # the command line simply becomes taller, and the accent rule the bar
        # settles into divides the history from the line being typed. It grows
        # in with the prompt, so the opening journey is not cluttered by it.
        arrival = smootherstep(*PROMPT_IN, t)
        rows = self._scroll_rows()
        scroll_h = self._scroll_height() * arrival
        if band_h > 0.5:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(BAND_FILL)
            painter.drawRect(
                QRectF(0.0, band_top - scroll_h, float(self.width()), band_h + scroll_h)
            )
            if scroll_h > 1.0:
                scrollback.paint(
                    painter, self._dock_block(band_top, scroll_h, rows), rows, arrival
                )

        # At t=0 this is drawn as the popup card exactly — same fill, same
        # border colour and width — so the overlay taking over from the popup
        # is invisible. As it condenses it becomes a solid accent mark.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(blend(CARD_FILL, ACCENT, solid))
        painter.drawRoundedRect(rect, corner, corner)

        if solid < 0.999:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(
                QPen(
                    fade(blend(CARD_EDGE, ACCENT, solid), 1.0 - solid),
                    lerp(float(CARD_BORDER_W), STROKE_BAR, solid),
                )
            )
            painter.drawRoundedRect(rect, corner, corner)

        prompt = arrival
        if prompt <= 0.0:
            return

        painter.setFont(mono(15, bold=True))
        painter.setPen(fade(ACCENT, prompt))
        baseline = (band_top + bottom) / 2.0 + 5.0
        painter.drawText(QPointF(PROMPT_PAD, baseline), "sentinel>")

        # Coloured token by token, from the same classifier the popup uses, so
        # the two prompts can never disagree about what is a valid command.
        text_x = INPUT_PAD
        painter.setFont(mono(15))
        metrics = painter.fontMetrics()
        for start, end, role in spans(self._buffer):
            x = text_x + metrics.horizontalAdvance(self._buffer[:start])
            painter.setFont(mono(15, bold=(role == HL_OK)))
            painter.setPen(fade(colour_for(role), prompt))
            painter.drawText(QPointF(x, baseline), self._buffer[start:end])
        painter.setFont(mono(15))

        # Block cursor, blinking off the same clock as the background.
        advance = painter.fontMetrics().horizontalAdvance(self._buffer)
        if int(self._clock * 2.0) % 2 == 0:
            painter.fillRect(
                QRectF(text_x + advance + 2.0, baseline - 13.0, 9.0, 17.0),
                fade(ACCENT, prompt),
            )
        elif not self._buffer:
            painter.setPen(self._tint(MUTED, 0.7 * prompt))
            painter.drawText(QPointF(text_x + 14.0, baseline), PLACEHOLDER)

        # Suggestions stack upward from the bar, so the prompt itself never
        # moves while the list grows. They clear the scroll-back rather than
        # covering it — the history is what you are consulting while you type.
        ceiling = band_top - scroll_h
        # Chased, not measured: the box grows and shrinks above the prompt.
        # It is bottom-anchored, so its rows stay put against the prompt while
        # the top edge travels and `suggest.paint` clips what does not fit yet.
        list_h = self._list_v * prompt
        if list_h > 1.0 and prompt > 0.5:
            width = min(self.width() - PROMPT_PAD * 2.0, 720.0)
            suggest.paint(
                painter,
                QRectF(PROMPT_PAD, ceiling - 10.0 - list_h, width, list_h),
                self._completer,
                prompt,
            )
        elif self._status:
            painter.setFont(mono(12))
            painter.setPen(
                fade(MUTED if self._status_ok else BAD,
                     0.95 * prompt * flicker_on(self._status_v))
            )
            painter.drawText(QPointF(PROMPT_PAD, ceiling - 12.0), self._status)

    # -- chrome --------------------------------------------------------------

    def status_layout(
        self, painter: QPainter
    ) -> tuple[list[Badge], list[float], float, float]:
        """Which badges fit, how wide each is, and where the strip starts.

        Split out from the painting so the geometry can be checked directly
        rather than inferred from pixels — half the badges are drawn in tones
        that no single brightness threshold can isolate.
        """
        badges = snapshot()
        if not badges:
            return [], [], 0.0, 0.0

        widths = []
        for badge in badges:
            painter.setFont(mono(10))
            width = painter.fontMetrics().horizontalAdvance(badge.label) + 7.0
            painter.setFont(mono(12, bold=True))
            width += (
                painter.fontMetrics().horizontalAdvance(badge.value) + STATUS_PAD * 2.0
            )
            widths.append(width)

        # The breadcrumb sits at the left of this same band and the key hints
        # at the right, so the strip gets the middle and drops what will not
        # fit rather than running into either.
        room = self.width() - STATUS_RESERVE * 2.0
        while len(widths) > 1 and sum(widths) + STATUS_GAP * (len(widths) - 1) > room:
            widths.pop()
            badges = badges[: len(widths)]

        total = sum(widths) + STATUS_GAP * (len(widths) - 1)
        return badges, widths, (self.width() - total) / 2.0, total

    def _paint_status(self, painter: QPainter, alpha: float) -> None:
        """Live badges centred along the top: timers, due items, indexing.

        The overlay repaints every frame while it is up, so the countdowns run
        in real time rather than only refreshing when something else changes.
        """
        badges, widths, x, _total = self.status_layout(painter)
        if not badges:
            return

        gap, pad, height = STATUS_GAP, STATUS_PAD, 22.0
        label_font, value_font = mono(10), mono(12, bold=True)
        y = 20.0

        for badge, width in zip(badges, widths):
            tone = {LIVE: ACCENT, WARN: BAD}.get(badge.tone, MUTED)
            rect = QRectF(x, y, width, height)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fade(tone, 0.12 * alpha))
            painter.drawRoundedRect(rect, 4.0, 4.0)
            painter.setBrush(Qt.BrushStyle.NoBrush)

            baseline = y + height / 2.0 + 4.0
            painter.setFont(label_font)
            painter.setPen(fade(MUTED, 0.95 * alpha))
            painter.drawText(QPointF(x + pad, baseline), badge.label)
            offset = painter.fontMetrics().horizontalAdvance(badge.label) + 7.0
            painter.setFont(value_font)
            painter.setPen(fade(tone, alpha))
            painter.drawText(QPointF(x + pad + offset, baseline), badge.value)
            x += width + gap

    def _paint_chrome(self, painter: QPainter, alpha: float) -> None:
        self._paint_status(painter, alpha)

        painter.setFont(mono(12))
        trail = "/".join(n.name for n in self._trail)
        painter.setPen(self._tint(MUTED, 0.9 * alpha))
        painter.drawText(QPointF(26.0, 34.0), f"sentinel /{trail}")

        if self._phase == Phase.IDLE:
            hint = "click or tab+enter  open   ·   esc  back"
        elif self._phase == Phase.PANEL:
            hint = "esc or click  back"
        else:
            hint = ""
        if hint:
            painter.setPen(self._tint(MUTED, 0.7 * alpha))
            painter.drawText(
                QRectF(0.0, 20.0, self.width() - 26.0, 20.0),
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                hint,
            )
