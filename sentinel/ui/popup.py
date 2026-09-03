"""The summoned command popup.

Created once and shown/hidden, not rebuilt per summon — a launcher has to feel
instant, and constructing a Qt window on every hotkey press would add visible
latency. Phase 7 replaces the single input line with a scrolling terminal
transcript; the submitted/hide behaviour here should survive that change.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QGuiApplication, QKeyEvent, QTextCursor
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .. import status
from ..config import settings
from ..transcript import transcript
from .foreground import force_foreground
from .highlight import CommandHighlighter
from .motion import Flicker, Ticker, chase, settled
from .paint import ACCENT, advance_accent, mono
from .scrollback import ScrollbackView
from .suggest import Completer, SuggestionList

log = logging.getLogger(__name__)

#: Whether clicking away hides the prompt. Lives in settings so it can be
#: turned off while debugging without editing code.
HIDE_ON_FOCUS_LOSS = "hide_on_blur"

# The card's look is shared with the navigator, whose closing animation has to
# land on exactly this shape and these colours or the handover visibly snaps.
CARD_BG = "#0f1116"
CARD_BORDER = "#2a2f3a"
CARD_BORDER_W = 1
CARD_CORNER = 10

#: The card's own padding. Named rather than left inline in the layout call
#: because the navigator lays the scroll-back out to these same numbers while
#: it is morphing into and out of the card — the two have to agree to the pixel.
CARD_PAD_X = 16
CARD_PAD_TOP = 12
CARD_PAD_BOTTOM = 10
CARD_SPACING = 4

#: The card is a fixed width, so everything on it is laid out against this.
CARD_W = 640

#: How far down the screen the prompt row sits, as a fraction of the available
#: height. The card grows around this point rather than from its own top edge.
ANCHOR = 0.26
#: How far down the prompt may be pushed to make room for history above it.
#: Past this the card stops moving and the scroll-back scrolls instead — a
#: prompt hunting around the middle of the screen is worse than one that sits
#: low and lets you wind the history back.
ANCHOR_MAX = 0.52
#: Clearance kept between a tall card and the top of the screen.
SCROLL_TOP_MARGIN = 12
#: The same, at the bottom. Whatever hangs below the prompt — the suggestion
#: list, the status line — has to clear the edge of the screen too.
SCROLL_BOTTOM_MARGIN = 16
#: How far a pushed-down prompt lifts back up once there is something typed on
#: it. Three rows: enough to be seen happening and to keep the line you are
#: typing clear of the screen's bottom edge, not so much that the history you
#: pushed the card down to read jumps away from under you.
TYPING_LIFT = 48.0
#: Rows per wheel notch, and per press of Page Up/Down.
SCROLL_STEP = 3
PAGE_ROWS = 8

# Where the laid-out card puts its content, in card-local pixels. The opening
# and closing animations are laid out from these, so their frames at the card
# end read as this popup and nothing pops in at the swap.
#
# The border counts: a styled QFrame lays its children out inside it, so the
# content column starts one pixel further in than the padding alone says.
CARD_CONTENT_X = float(CARD_BORDER_W + CARD_PAD_X)
CARD_CONTENT_TOP = float(CARD_BORDER_W + CARD_PAD_TOP)

#: The prompt label starts at the content column; the input follows it across.
CARD_PROMPT_X = CARD_CONTENT_X
CARD_INPUT_X = 107.0
CARD_PROMPT_DY = -3.0  # prompt baseline, relative to the prompt row's centre
PLACEHOLDER = "type a command…"

_FONT = '"JetBrains Mono", "Cascadia Mono", Consolas, monospace'


def build_stylesheet() -> str:
    """Rebuilt whenever the accent changes, so the prompt follows the theme.

    The accent was baked in as a literal here, which is why recolouring left
    the closed command line green while everything else had moved on.
    """
    accent = ACCENT.name()
    return f"""
#card {{
    background-color: {CARD_BG};
    border: {CARD_BORDER_W}px solid {CARD_BORDER};
    border-radius: {CARD_CORNER}px;
}}
#prompt {{
    color: {accent};
    font-family: {_FONT};
    font-size: 15px;
    font-weight: bold;
}}
#input {{
    background: transparent;
    border: none;
    color: #e6e8ee;
    font-family: {_FONT};
    font-size: 15px;
    selection-background-color: {accent};
    selection-color: {CARD_BG};
}}
#echo {{
    color: #5c6370;
    font-family: {_FONT};
    font-size: 12px;
}}
"""


def _shape(line: str) -> str:
    """A status line with its numbers removed — what it is, not what it says.

    Lets a ticking countdown be told apart from a genuinely different reading,
    which is the difference between updating text and striking a line on.
    """
    return "".join(ch for ch in line if not ch.isdigit())


class CommandEdit(QTextEdit):
    """A one-line editor that can colour its own text.

    QLineEdit cannot show per-character colour — it has no QTextDocument for a
    highlighter to attach to. A QTextEdit restrained to a single line is the
    standard way round that, and keeps a real cursor and selection.
    """

    submitted = Signal(str)
    #: Arrow/Tab pressed with a suggestion list open.
    navigated = Signal(int)
    accepted = Signal()
    #: Page Up/Down: rows to wind the scroll-back back by, positive for older.
    scrolled = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("input")
        self.setAcceptRichText(False)
        self.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameStyle(QFrame.Shape.NoFrame)
        self.setTabChangesFocus(True)
        self.setPlaceholderText(PLACEHOLDER)
        self.document().setDocumentMargin(0.0)
        # Set the font here rather than leaving it to the stylesheet: the
        # widget is measured during layout, before the parent's stylesheet has
        # been applied, and a default-font measurement makes the card too tall.
        self.setFont(mono(15))
        self.setFixedHeight(self.fontMetrics().height() + 4)
        # A QTextEdit's size hint is generous; without this it widens the row
        # and pushes the prompt label out of place.
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(0)
        self.highlighter = CommandHighlighter(self.document())

    # A QLineEdit-shaped surface, so callers do not care which it is.
    def text(self) -> str:
        return self.toPlainText()

    def setText(self, value: str) -> None:
        self.setPlainText(value)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.submitted.emit(self.toPlainText())
            return
        # Tab and the arrows drive the suggestion list rather than the editor.
        if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            self.accepted.emit()
            return
        if key == Qt.Key.Key_Down:
            self.navigated.emit(1)
            return
        if key == Qt.Key.Key_Up:
            self.navigated.emit(-1)
            return
        # The arrows already drive the suggestion list, so history gets the
        # page keys — the one pair a single-line editor has no use for.
        if key == Qt.Key.Key_PageUp:
            self.scrolled.emit(PAGE_ROWS)
            return
        if key == Qt.Key.Key_PageDown:
            self.scrolled.emit(-PAGE_ROWS)
            return
        super().keyPressEvent(event)

    def wheelEvent(self, event) -> None:
        # One line, with nothing of its own to scroll. Left accepted, the
        # editor would swallow every wheel turn aimed at the history above it.
        event.ignore()


class PopupWindow(QWidget):
    submitted = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.Tool  # keeps it out of the taskbar and alt-tab
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(build_stylesheet())
        settings.listeners.append(self._on_setting_changed)
        self.setFixedWidth(CARD_W)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        card = QFrame(self)
        card.setObjectName("card")
        outer.addWidget(card)

        self._card_layout = card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(
            CARD_PAD_X, CARD_PAD_TOP, CARD_PAD_X, CARD_PAD_BOTTOM
        )
        card_layout.setSpacing(CARD_SPACING)

        # Everything the card holds lives in one child, so it can be flickered
        # off *inside* the card — the border and fill stay lit while the
        # contents go dark. That is what lets the overlay take over a card that
        # is the shape it was, only empty.
        self._content = QWidget(card)
        card_layout.addWidget(self._content)
        content_layout = QVBoxLayout(self._content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(CARD_SPACING)
        self._content_layout = content_layout
        # The effect is installed only for the hand-over, not for the life of
        # the card: a graphics effect makes Qt render the whole subtree through
        # an offscreen pixmap on every repaint, and this one repaints on every
        # keystroke and every animation frame.
        self._content_fade: QGraphicsOpacityEffect | None = None

        # History above the prompt, terminal-fashion. The window is positioned
        # from the prompt row rather than the card's top edge (see
        # `_move_to_active_screen`), so this grows upward and the line you are
        # typing never moves out from under the cursor.
        self.scrollback = ScrollbackView(self._content)
        content_layout.addWidget(self.scrollback)
        self.scrollback.changed.connect(self._remeasure)
        transcript.listeners.append(self._wake)

        line = QHBoxLayout()
        line.setSpacing(10)

        prompt = QLabel("sentinel>")
        prompt.setObjectName("prompt")
        line.addWidget(prompt)

        self.input = CommandEdit()
        self.input.submitted.connect(lambda _text: self._on_submit())
        line.addWidget(self.input, 1)

        content_layout.addLayout(line)

        # Suggestions sit between the prompt and the echo line, so the card
        # grows downward as you type and shrinks back when it is not needed.
        self.completer = Completer()
        self.suggestions = SuggestionList(self.completer, self._content)
        content_layout.addWidget(self.suggestions)
        self.suggestions.changed.connect(self._remeasure)
        self.input.textChanged.connect(self._on_typed)
        self.input.navigated.connect(self._on_navigate)
        self.input.accepted.connect(self._on_accept)
        self.input.scrolled.connect(self._scroll_history)

        self.echo = QLabel("")
        self.echo.setObjectName("echo")
        self._echo_fade = QGraphicsOpacityEffect(self.echo)
        self._echo_fade.setOpacity(0.0)
        self.echo.setGraphicsEffect(self._echo_fade)
        content_layout.addWidget(self.echo)

        # The window has no transition of its own, so it strikes on and dies
        # out rather than popping. The status line does the same on every
        # change, so a new reading arrives rather than replacing the old one
        # between two frames.
        self._flicker = Flicker(self.setWindowOpacity, self)
        self._flicker.finished.connect(self._on_flicker_finished)
        self._echo_flicker = Flicker(self._echo_fade.setOpacity, self)

        # The hand-over to the overlay: the contents die out inside a card that
        # does not move. Whatever is waiting on that runs when it is dark.
        self._content_flicker = Flicker(self._set_content_opacity, self)
        self._content_flicker.finished.connect(self._on_content_dark)
        self._handoff: Callable[[], None] | None = None
        #: While set, the card holds its size and position and takes no new
        #: content. See `hand_off`.
        self._frozen = False

        # One frame clock for the whole card. Everything that can change size
        # or position chases its target through this rather than jumping:
        # the scroll-back, the suggestion box, the card itself and the window
        # under it. Stopped the moment it all settles, so an idle prompt costs
        # nothing — and stopped outright while the card is not on screen.
        self._ticker = Ticker(self)
        self._ticker.tick.connect(self._advance)

        # Where the prompt row sits down the screen, and where it is heading.
        # It moves for two reasons — history piling up above it, and a line
        # being typed on it — so it is chased like everything else rather than
        # teleporting: a card that jumps down the screen the moment a command
        # prints reads as a different window opening. `None` means "not placed
        # yet"; the first placement snaps.
        self._anchor: float | None = None
        self._anchor_target = 0.0

        # The echo line carries what the daemon is holding — a running timer,
        # something due. Command results used to live here too and no longer
        # do: they go into the scroll-back above, where the last ten of them
        # stay readable instead of each one wiping the one before. Ticked once
        # a second, and only while the prompt is actually up.
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(self._refresh_status)

    # ------------------------------------------------------------------ show

    def summon(self) -> None:
        """Bring the prompt up, striking on like a tube."""
        self.input.clear()
        self.completer.dismiss()
        # Whatever hand-over left the card frozen and dark is over.
        self._thaw()
        # Snapped, not chased: the card is arriving, and the whole of it —
        # history included — strikes on together. Sliding its contents into
        # place *while* it flickers in would read as two separate events. The
        # anchor goes with them: wherever the card was pushed to last time, it
        # is summoned at the position this much history calls for.
        self._anchor = None
        # Same order as `handover`, for the same reasons — the flicker hides
        # a card that re-measures itself once it is up, it does not excuse one.
        self._recolour()
        self._show_status()
        self._sync_suggestions(snap=True)
        self.setWindowOpacity(0.0)
        self.show()
        self.input.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        force_foreground(self)
        self._flicker.light()

    def handover(self) -> None:
        """Appear with no flicker, for a window morphing into this one.

        The navigator's closing animation ends drawn as this exact card, so a
        flicker here would undo a handover that is otherwise seamless.
        """
        self.input.clear()
        self.completer.dismiss()
        # Whatever hand-over left the card frozen and dark is over.
        self._thaw()
        # Snapped for the same reason the opacity is: the overlay's last frame
        # has already drawn this card at full size, history and all — and a
        # card that then slid down the screen would undo the handover.
        self._anchor = None
        # Everything that can change the card's size or its colour happens
        # before it is on screen, and in that order. The accent may have been
        # swept while the card was away and its stylesheet holds a colour
        # rather than a reference to one — but re-styling a window that is
        # already up re-polishes the whole tree in front of the user, and a
        # status line set after the measuring shows the card at a height that
        # does not account for it. Between them that is the pair of empty
        # frames the handover used to open with.
        self._recolour()
        self._show_status()
        self._sync_suggestions(snap=True)
        self.setWindowOpacity(1.0)
        self.show()
        self.input.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        force_foreground(self)

    def dismiss(self) -> None:
        """Flicker out, then hide once dark."""
        if not self.isVisible() or self._flicker.closing:
            return
        self._flicker.douse()

    def _on_flicker_finished(self) -> None:
        if not self._flicker.closing and self.windowOpacity() <= 0.01:
            self.hide()

    # -------------------------------------------------------------- hand-over

    def hand_off(self, done: Callable[[], None]) -> None:
        """Empty the card, then hand its shape to whatever comes next.

        The contents die out *inside* the card — the fill and the border stay
        lit and the card does not move a pixel while it happens. What the
        overlay then takes over is the shape that was actually on screen, and
        it balls that up: it never snaps back to the bare one-line prompt
        first, and the status line is not left to vanish between two frames.
        """
        if not self.isVisible() or self._handoff is not None:
            done()
            return
        self._freeze()
        self._handoff = done
        self._content_flicker.douse()

    def _set_content_opacity(self, value: float) -> None:
        if self._content_fade is not None:
            self._content_fade.setOpacity(value)

    @property
    def content_opacity(self) -> float:
        """How lit the card's contents are. 1.0 whenever nothing is fading."""
        return 1.0 if self._content_fade is None else self._content_fade.opacity()

    def _on_content_dark(self) -> None:
        if self._handoff is None or self.content_opacity > 0.01:
            return
        done, self._handoff = self._handoff, None
        done()

    def _freeze(self) -> None:
        """Hold the card exactly as it is — size, position and contents.

        Settled first, and that is not housekeeping. Submitting cleared the
        input, which sets the suggestion box folding shut and lets the anchor
        drift back to where a prompt at rest belongs. Both are chases, driven
        by the frame clock this is about to stop — so freezing on top of them
        held a size that was still on its way somewhere, and the card visibly
        shrank *under* the hand-over flicker, which is the one thing this is
        supposed to hold still.

        Everything downstream inherited that half-way rectangle: it is what the
        overlay balls up, and what its outro folds back into at the end. The
        popup that then takes over has long since finished the fold, so it
        landed a row short of the card the overlay had just drawn, and the
        handover snapped in exactly the place it was built not to.
        """
        self.suggestions.sync(snap=True)
        self.scrollback.settle()
        self._remeasure()
        if self._anchor is not None:
            self._anchor = self._anchor_target
            self._reposition()

        self._frozen = True
        self._ticker.stop()
        self.scrollback.frozen = True
        if self._content_fade is None:
            self._content_fade = QGraphicsOpacityEffect(self._content)
            self._content_fade.setOpacity(1.0)
            self._content.setGraphicsEffect(self._content_fade)

    def _thaw(self) -> None:
        self._frozen = False
        self._handoff = None
        self.scrollback.frozen = False
        # Stop before detaching: `setGraphicsEffect(None)` destroys the effect,
        # and a flicker still running would be writing to a dead object.
        self._content_flicker.stop()
        if self._content_fade is not None:
            self._content_fade = None
            self._content.setGraphicsEffect(None)

    # ------------------------------------------------------------ suggestions

    def _on_typed(self) -> None:
        self.completer.refresh(self.input.text())
        self._sync_suggestions()

    def _on_navigate(self, delta: int) -> None:
        if not self.completer.active:
            return
        self.completer.move(delta)
        self.suggestions.update()
        self._wake()  # the highlight slides to the new row

    def _on_accept(self) -> None:
        if not self.completer.active:
            return
        completed = self.completer.accept(self.input.text())
        if completed != self.input.text():
            self.input.setText(completed)
            cursor = self.input.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self.input.setTextCursor(cursor)
        self._sync_suggestions()

    def _wake(self) -> None:
        """Something changed shape — run the frame clock until it settles."""
        if self._frozen:
            return
        if self.isVisible() and not self._ticker.running:
            self._ticker.start(self.screen())

    def _advance(self, delta: float) -> None:
        """One frame: nudge everything toward what it should be."""
        moving = self.scrollback.advance(delta)
        moving |= self.suggestions.advance(delta)
        moving |= self.completer.advance(delta)
        moving |= self._advance_anchor(delta)
        if advance_accent(delta):
            # Normally the overlay finishes the sweep, since that is where the
            # accent gets changed. This is the case where it did not — the card
            # picks the sweep up rather than leaving it stranded half-way.
            self._recolour()
            moving = True
        self.suggestions.update()
        if not moving:
            # Nothing left to chase. A resident daemon has no business holding
            # a 120fps timer open over a card that has stopped moving.
            self._ticker.stop()

    def _advance_anchor(self, delta: float) -> bool:
        """Slide the prompt row toward where it should sit. True while moving.

        The scroll-back's ceiling comes off this, so the block grows into the
        room the card is opening up as it travels rather than in one step once
        it arrives.
        """
        if self._anchor is None or settled(self._anchor, self._anchor_target):
            if self._anchor is not None and self._anchor != self._anchor_target:
                self._anchor = self._anchor_target
                self._reposition()
            return False
        self._anchor = chase(self._anchor, self._anchor_target, delta)
        self._reposition()
        return True

    def _reposition(self, area=None) -> None:
        """Place the card at the anchor it has got to, without re-measuring."""
        if self._anchor is None:
            return
        if area is None:
            screen = (QGuiApplication.screenAt(QCursor.pos())
                      or QGuiApplication.primaryScreen())
            area = screen.availableGeometry()
        # However much history there is, the card may not grow past the top of
        # the screen. So the scroll-back is capped at the room actually above
        # the anchor — and, now that it scrolls, what will not fit is parked
        # rather than lost.
        self.scrollback.limit = self._room_above(self._anchor, area)
        x = area.x() + (area.width() - self.width()) // 2
        # The anchor is the prompt row, not the card's top edge: the card is
        # pushed up by exactly as much history as it is carrying, so the prompt
        # holds still and the handover rectangle never moves either.
        top = int(round(self._anchor)) - self._scrollback_height()
        self.move(x, max(area.y(), top))

    def _sync_suggestions(self, snap: bool = False) -> None:
        """Take a new list, and start the card moving toward its new size."""
        self.suggestions.sync(snap=snap)
        if snap:
            self.scrollback.sync(snap=True)
        self._remeasure()
        self._wake()

    def _remeasure(self) -> None:
        # The card is laid out to its contents, so it is re-measured every time
        # the list or the scroll-back changes height. The layout has to be
        # activated first: without it the cached size hint keeps the window at
        # its largest and it never shrinks back.
        # Every layout, innermost first: activating only the outer one leaves
        # the inner cached hints at their largest and the window never shrinks.
        if self._frozen:
            return  # mid-hand-over; the card is holding its shape
        for layout in (self._content_layout, self._card_layout, self.layout()):
            if layout is not None:
                layout.invalidate()
                layout.activate()
        self.resize(self.width(), self.sizeHint().height())
        self._move_to_active_screen()

    def _on_setting_changed(self, key: str, value: object) -> None:
        if key == "accent":
            self._recolour()

    def _recolour(self) -> None:
        """Take the accent's current value into the stylesheet.

        Called for each step of a sweep rather than once when the setting
        changes: `ACCENT` is now a value on its way somewhere, and a stylesheet
        built from it is a snapshot that goes stale immediately.
        """
        self.setStyleSheet(build_stylesheet())
        self.input.highlighter.rehighlight()
        # The scroll-back reads the accent at paint time, so a repaint is all
        # it needs — but it does need one; nothing else invalidates it.
        self.scrollback.update()

    def toggle(self) -> None:
        if self.isVisible() and not self._flicker.closing:
            self.dismiss()
        else:
            self.summon()

    def _scrollback_height(self) -> int:
        """How much of the card sits above the prompt row, spacing included.

        `isHidden`, not `isVisible`: the card is measured and positioned while
        the window itself is still hidden, on the way up from `summon()`. That
        makes every child un-*visible* but only an emptied scroll-back
        *hidden* — which is the same test the layout uses to decide whether the
        row is there at all.
        """
        if self.scrollback.isHidden():
            return 0
        return self.scrollback.height() + self._content_layout.spacing()

    def prompt_rise(self) -> float:
        """How far the prompt row's centre sits above the card's bottom edge.

        The navigator morphs out of the *whole* card — history included, or the
        shape would snap the moment the overlay took over — so it cannot assume
        the prompt is at the middle of what it is drawing. This is the one
        number it needs to put "sentinel>" back where the popup had it.
        """
        rect = self.geometry()
        return (rect.height() - self._scrollback_height()) / 2.0

    def _room_above(self, anchor: float, area) -> float:
        """How much of the card may sit above `anchor` on this screen."""
        return max(0.0, anchor - area.y() - CARD_SPACING - SCROLL_TOP_MARGIN)

    def _anchor_for(self, area) -> float:
        """Where the prompt row wants to sit on this screen.

        Three pulls, in order. It rests at `ANCHOR`. History that will not fit
        above it pushes it down the screen rather than being thrown away at the
        top edge — as far as `ANCHOR_MAX`, past which the scroll-back scrolls
        instead. And a line being typed lifts it back up, so the line you are
        writing and the suggestions under it are not left crammed against the
        bottom of the screen.
        """
        base = area.y() + area.height() * ANCHOR
        floor = area.y() + area.height() * ANCHOR_MAX

        push = self.scrollback.natural - self._room_above(base, area)
        push = max(0.0, min(push, max(0.0, floor - base)))
        if self.input.text():
            # Only ever gives back room the history took: a prompt at rest has
            # nothing to lift, and should not drift up as you type.
            push = max(0.0, push - TYPING_LIFT)
        anchor = base + push

        # Whatever hangs below the prompt has to clear the bottom edge too —
        # the suggestion list can be taller than the lift on a short screen.
        below = self.height() - self._scrollback_height()
        lowest = area.bottom() - below - SCROLL_BOTTOM_MARGIN
        return max(float(area.y() + CARD_SPACING + SCROLL_TOP_MARGIN),
                   min(anchor, lowest))

    def _move_to_active_screen(self) -> None:
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        self._anchor_target = self._anchor_for(area)
        if self._anchor is None:
            # First placement of this summon: the card arrives where it belongs
            # rather than sliding into position in front of you.
            self._anchor = self._anchor_target
        self._reposition(area)

    # ------------------------------------------------------------- scrolling

    def _scroll_history(self, rows: int) -> None:
        """Wind the scroll-back back through what the card cannot show."""
        if self._frozen:
            return
        if self.scrollback.scroll_by(rows):
            self._wake()

    def wheelEvent(self, event) -> None:
        steps = event.angleDelta().y() / 120.0
        if not steps:
            super().wheelEvent(event)
            return
        # At least one row, whichever way: a trackpad reports these in
        # fractions of a notch, and rounding those to zero would make a slow
        # scroll do nothing at all.
        rows = int(steps * SCROLL_STEP)
        self._scroll_history(rows or (1 if steps > 0 else -1))
        event.accept()

    # ----------------------------------------------------------------- input

    def _on_submit(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        self.input.clear()
        self.submitted.emit(text)

    def _refresh_status(self) -> None:
        """Keep the idle line current while the prompt is up.

        A *new* reading strikes on rather than being swapped in place. A
        countdown losing a second is not a new reading, though, and flickering
        the line once a second would be intolerable — so what is compared is
        the shape of the text with its numbers taken out.
        """
        line = status.summary()
        previous = self.echo.text()
        if line == previous:
            return
        self.echo.setText(line)
        if _shape(line) == _shape(previous):
            return  # the same reading, counting
        if line:
            self._echo_flicker.light()
        else:
            self._echo_flicker.douse()

    def _show_status(self) -> None:
        self.echo.setText(status.summary())
        self._echo_fade.setOpacity(0.0)
        if self.echo.text():
            self._echo_flicker.light()
        self._status_timer.start()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # Anything that changed while the card was away has to be caught up on
        # now, and the clock only runs while there is something to see.
        self._wake()

    def hideEvent(self, event) -> None:
        self._status_timer.stop()
        self._ticker.stop()
        super().hideEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.dismiss()
            return
        super().keyPressEvent(event)

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.WindowDeactivate and settings.get("hide_on_blur"):
            self.dismiss()
        return super().event(event)
