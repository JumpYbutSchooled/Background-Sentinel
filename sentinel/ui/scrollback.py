"""The scroll-back rows, drawn the same way wherever they appear.

One renderer, two surfaces — the popup's card and the navigator's docked band —
for the same reason `suggest` is built this way: the two command lines hand over
to each other mid-animation, and the handover is only seamless if the last frame
one draws is the first frame the other draws. Two implementations of "a row of
history" would drift by a pixel and the swap would show.

The caller owns the geometry. `paint()` bottom-anchors the rows inside the
rectangle it is given and clips to it, so a block that is animating open shows
its newest rows and slides the oldest out under its top edge. Text is elided to
the rectangle's width, which is what lets the same rows narrow from a
screen-wide band onto a 640px card without reflowing.

The card's own block holds more history than it can show and scrolls through
it: the block stops growing at `VIEW_ROWS`, and everything older stays a wheel
turn away rather than being thrown out of the window.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QWidget

from ..transcript import COMMAND, ERROR, VIEW_ROWS, transcript
from .motion import chase, settled
from .paint import ACCENT, BAD, CELL_FILL, MUTED, TEXT, fade, mono

#: Row height and text size. Small enough that ten commands do not dominate the
#: screen, big enough to read a listing's columns.
ROW_H = 16.0
FONT_PX = 12
#: Baseline inset from a row's bottom edge.
BASELINE = 4.0

#: Drawn before an echoed command, and what output rows are indented past.
MARKER = "> "


def height_for(rows: list[tuple[str, str]]) -> float:
    """How tall a block holding `rows` has to be. Padding is the caller's."""
    return len(rows) * ROW_H


def paint(
    painter: QPainter, rect: QRectF, rows: list[tuple[str, str]], alpha: float = 1.0,
    shift: float = 0.0,
) -> None:
    """Draw `rows` into `rect`, newest at the bottom.

    `shift` slides the whole stack down inside the rectangle, which is how the
    card scrolls back through history: the newest rows go under the bottom edge
    and older ones come in at the top. It is a float rather than a row count so
    a scroll can be eased like everything else rather than jumping a row at a
    time.
    """
    if not rows or alpha <= 0.0:
        return

    painter.save()
    painter.setClipRect(rect)
    painter.setFont(mono(FONT_PX))
    metrics = painter.fontMetrics()
    indent = float(metrics.horizontalAdvance(MARKER))
    room = max(0, int(rect.width() - indent))

    # From the bottom up: the newest row sits against the prompt and holds its
    # place, and it is the oldest that leaves when there is not enough room.
    bottom = rect.bottom() + shift
    for offset, (kind, text) in enumerate(reversed(rows)):
        baseline = bottom - offset * ROW_H - BASELINE
        if baseline < rect.top() - ROW_H:
            break
        # Scrolled-away rows are still stepped over — they are below the
        # rectangle, not before it — but there is no sense drawing them.
        if baseline > rect.bottom() + ROW_H:
            continue
        body = metrics.elidedText(text, Qt.TextElideMode.ElideRight, room)
        if kind == COMMAND:
            painter.setPen(fade(ACCENT, 0.9 * alpha))
            painter.drawText(QPointF(rect.x(), baseline), MARKER)
            painter.setPen(fade(MUTED, alpha))
        else:
            painter.setPen(fade(BAD if kind == ERROR else TEXT, 0.85 * alpha))
        painter.drawText(QPointF(rect.x() + indent, baseline), body)

    painter.restore()


class ScrollbackView(QWidget):
    """The popup's block. The navigator paints its own, in its own space.

    Painted rather than a rich-text label so it is the *same* drawing code the
    navigator morphs out of — a QLabel would lay these rows out to its own
    metrics, and the card would no longer match the overlay's first frame.

    It holds the whole transcript and shows a window onto it. The block grows
    to `VIEW_ROWS` and then stops: past that the card would be a log viewer
    rather than a prompt, so the rest is reached by scrolling.
    """

    #: The card has to re-measure and reposition itself when this changes size.
    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.rows: list[tuple[str, str]] = []
        self._shown = 0.0
        #: Ceiling set by whatever is holding this, in pixels. The block is
        #: bottom-anchored and clipped, so a lower ceiling simply shows fewer
        #: of the oldest rows rather than squashing any of them.
        self.limit = float("inf")
        #: While set, new history is ignored. The card sets this during a
        #: hand-over so the rows do not shuffle up by one behind a fade.
        self.frozen = False
        #: How many rows back from the newest the view is parked, and how far
        #: down the stack has actually got — the second chases the first, so a
        #: wheel turn slides rather than jumping.
        self.offset = 0
        self._scrolled = 0.0
        self.setFixedHeight(0)
        transcript.listeners.append(self.sync)
        self.sync(snap=True)

    # ---------------------------------------------------------------- sizing

    @property
    def natural(self) -> float:
        """Tall enough for everything it holds, up to the view budget.

        What the card asks for before any ceiling is applied — which is also
        what tells the card whether it needs to shift itself down the screen.
        """
        return min(height_for(self.rows), VIEW_ROWS * ROW_H)

    @property
    def target(self) -> float:
        return min(self.natural, max(0.0, self.limit))

    # -------------------------------------------------------------- scrolling

    @property
    def capacity(self) -> int:
        """How many whole rows the block has room for at its settled height."""
        return max(1, int(self.target // ROW_H))

    @property
    def max_offset(self) -> int:
        """The oldest row the view may be parked on."""
        return max(0, len(self.rows) - self.capacity)

    def scroll_by(self, rows: int) -> bool:
        """Step back (positive) or forward through history. True if it moved."""
        offset = max(0, min(self.max_offset, self.offset + rows))
        if offset == self.offset:
            return False
        self.offset = offset
        self.update()
        return True

    def to_bottom(self, snap: bool = False) -> None:
        """Back to the newest row, where the prompt is."""
        self.offset = 0
        if snap:
            self._scrolled = 0.0
        self.update()

    # ------------------------------------------------------------------ state

    def sync(self, snap: bool = False) -> None:
        """Take the current history. The block's height follows separately."""
        if self.frozen and not snap:
            return
        # The whole transcript, not the view budget's worth: the budget bounds
        # how much is *shown*, and everything older than that is what there is
        # to scroll to.
        rows = transcript.rows(limit=None)
        # Rows are kept while the block collapses. `clear` empties the
        # transcript outright, and dropping the text on the same frame would
        # leave an empty box folding shut with nothing in it — the history has
        # to still be there to be seen leaving.
        if rows or snap:
            self.rows = rows
        # New output pulls the view back to the newest row. A prompt that
        # answered you while you were reading history, and left the answer off
        # screen, would be worse than one that never scrolled at all.
        self.to_bottom(snap=snap)
        if snap:
            self._shown = self.target
            self._apply()
        self.update()

    def advance(self, delta: float) -> bool:
        """Step the height and the scroll toward where they should be."""
        moving = False

        # A shorter block holds fewer rows, so a ceiling that has just come
        # down can leave the view parked past the end of what there is.
        if self.offset > self.max_offset:
            self.offset = self.max_offset

        goal = self.offset * ROW_H
        if settled(self._scrolled, goal):
            if self._scrolled != goal:
                self._scrolled = goal
                self.update()
        else:
            self._scrolled = chase(self._scrolled, goal, delta)
            self.update()
            moving = True

        target = self.target
        if settled(self._shown, target):
            if self._shown != target:
                self._shown = target
                self._apply()
            if transcript.empty:
                self.rows = []
            return moving
        self._shown = chase(self._shown, target, delta)
        self._apply()
        return True

    def _apply(self) -> None:
        height = int(round(self._shown))
        if height != self.height():
            self.setFixedHeight(height)
        # Hidden rather than merely empty: a zero-height widget still takes a
        # slot in the layout, and the spacing around it would leave a gap.
        self.setVisible(height > 0)
        self.update()
        self.changed.emit()

    # ---------------------------------------------------------------- drawing

    def paintEvent(self, event) -> None:
        if not self.rows:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        # Fades as the last of it folds away, so a collapsing block dims out
        # rather than being guillotined by its own top edge. Measured against
        # the height it is *allowed*, not the height it would like: a block
        # held short by the card's ceiling is doing its job, not disappearing.
        full = max(1.0, min(self.natural, max(0.0, self.limit)))
        alpha = min(1.0, 0.35 + 0.65 * (self.height() / full))
        rect = QRectF(0.0, 0.0, float(self.width()), float(self.height()))
        paint(painter, rect, self.rows, alpha, shift=self._scrolled)
        self._mark_scrolled(painter, rect, alpha)

    def _mark_scrolled(self, painter: QPainter, rect: QRectF, alpha: float) -> None:
        """Say how much newer history is parked below the view.

        Without it, a scrolled-back card is indistinguishable from one whose
        last command printed something else — the rows above the prompt would
        simply be lying about what just happened.
        """
        if self.offset <= 0 or rect.height() < ROW_H:
            return
        painter.setFont(mono(FONT_PX))
        label = f"↓{self.offset}"
        metrics = painter.fontMetrics()
        width = float(metrics.horizontalAdvance(label))
        pill = QRectF(
            rect.right() - width - 8.0, rect.bottom() - ROW_H + 1.0,
            width + 8.0, ROW_H - 1.0,
        )
        # Filled, because it sits over the newest visible row's text.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fade(CELL_FILL, alpha))
        painter.drawRoundedRect(pill, 3.0, 3.0)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(fade(ACCENT, 0.8 * alpha))
        painter.drawText(
            QPointF(pill.x() + 4.0, rect.bottom() - BASELINE), label
        )
