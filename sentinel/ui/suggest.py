"""The suggestion list that follows what you are typing.

One controller and one row renderer, shared by the popup and the navigator's
docked prompt, so both behave identically: arrows move, Tab inserts, and the
right-hand column always shows the shape of what comes next — `<app>`, `[on|off]`
— rather than making you remember it.

Values containing spaces are inserted already quoted, so `launch visual studio
code` becomes `launch "Visual Studio Code"` and parses as one argument.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import QWidget

from ..commands import Suggestion, apply_completion, complete
from .motion import chase, settled
from .paint import ACCENT, CELL_FILL, MUTED, TEXT, fade, mono

ROW_H = 24.0
PAD = 10.0
MAX_ROWS = 8


class Completer:
    """Which suggestions apply, and which one is highlighted.

    It owns the *eased* highlight position as well as the real one, because
    both command lines drive the same list and the band should slide between
    rows on either of them rather than teleporting.
    """

    def __init__(self) -> None:
        self.items: list[Suggestion] = []
        self.index = 0
        #: Where the highlight is drawn, chasing `index`.
        self.cursor = 0.0
        self._line = ""

    def advance(self, delta: float) -> bool:
        """Step the highlight toward the selected row. True while moving."""
        if settled(self.cursor, float(self.index)):
            self.cursor = float(self.index)
            return False
        self.cursor = chase(self.cursor, float(self.index), delta)
        return True

    @property
    def active(self) -> bool:
        return bool(self.items)

    @property
    def current(self) -> Suggestion | None:
        if not self.items:
            return None
        return self.items[min(self.index, len(self.items) - 1)]

    def refresh(self, line: str) -> None:
        if line == self._line:
            return
        self._line = line
        self.items = complete(line) if line.strip() or line else []
        self.index = 0
        # A different list is a different set of rows; sliding the band across
        # from wherever it was in the old one would mean nothing.
        self.cursor = 0.0

    def dismiss(self) -> None:
        self.items = []
        self.index = 0
        self.cursor = 0.0

    def move(self, delta: int) -> None:
        if not self.items:
            return
        previous = self.index
        self.index = (self.index + delta) % len(self.items)
        # Wrapping round the ends is a jump, not a step: sliding the whole
        # height of the list would read as scrolling to the other end.
        if abs(self.index - previous) > 1:
            self.cursor = float(self.index)

    def accept(self, line: str) -> str:
        """The line with the highlighted suggestion inserted."""
        chosen = self.current
        if chosen is None or not chosen.insertable:
            return line
        completed = apply_completion(line, chosen)
        self._line = completed
        self.items = complete(completed)
        self.index = 0
        return completed

    def ghost(self, line: str) -> str:
        """Faint continuation drawn after the cursor, Minecraft-style."""
        chosen = self.current
        if chosen is None:
            return ""
        if chosen.insertable:
            _, _, prefix = _split(line)
            if chosen.label.lower().startswith(prefix.lower()) and len(chosen.label) > len(prefix):
                return chosen.label[len(prefix):]
            return ""
        # Reference row: show the argument shape that would come next.
        return ""

    def rows(self) -> int:
        return min(len(self.items), MAX_ROWS)

    def window(self) -> list[tuple[int, Suggestion]]:
        """The visible slice, keeping the highlighted row in view."""
        count = self.rows()
        if not count:
            return []
        start = max(0, min(self.index - count + 1, len(self.items) - count))
        start = min(start, self.index)
        return [(i, self.items[i]) for i in range(start, start + count)]


def _split(line: str) -> tuple[str, int, str]:
    from ..commands import _position

    tokens, index, prefix = _position(line)
    return line, index, prefix


def height_for(completer: Completer) -> float:
    rows = completer.rows()
    return 0.0 if not rows else rows * ROW_H + PAD * 2.0


def paint(
    painter: QPainter, rect: QRectF, completer: Completer, alpha: float = 1.0,
    framed: bool = True, held: list[tuple[int, Suggestion]] | None = None,
) -> None:
    """Draw the list into `rect`. The caller owns the geometry.

    Clipped, because the caller may be animating the rectangle open or shut:
    the rows are laid out at their settled positions and the box reveals as
    much of them as it currently has room for.
    """
    visible = completer.window() or (held or [])
    if not visible:
        return

    painter.save()
    painter.setClipRect(rect)

    if framed:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fade(CELL_FILL, 0.98 * alpha))
        painter.drawRoundedRect(rect, 6.0, 6.0)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(fade(ACCENT, 0.28 * alpha), 0.9))
        painter.drawRoundedRect(rect, 6.0, 6.0)

    # One band at a fractional row, drawn before the rows and slid into place,
    # rather than a hard rectangle switched on under whichever row happens to
    # be selected. The *text* still resolves instantly — smearing bold across
    # two rows would read as a rendering fault, not as motion.
    first = visible[0][0]
    band_y = rect.y() + PAD + (completer.cursor - first) * ROW_H
    band = QRectF(rect.x() + 1.0, band_y, rect.width() - 2.0, ROW_H)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(fade(ACCENT, 0.14 * alpha))
    painter.drawRect(band)
    painter.setBrush(fade(ACCENT, 0.85 * alpha))
    painter.drawRect(QRectF(band.x(), band.y(), 2.0, band.height()))
    painter.setBrush(Qt.BrushStyle.NoBrush)

    # The detail column is right-aligned, so the syntax lines up down the list.
    y = rect.y() + PAD
    for position, item in visible:
        chosen = position == completer.index
        baseline = y + ROW_H / 2.0 + 4.5
        painter.setFont(mono(13, bold=chosen))
        painter.setPen(fade(ACCENT if chosen else TEXT,
                            (1.0 if chosen else 0.85) * alpha))
        painter.drawText(QPointF(rect.x() + PAD + 4.0, baseline), item.label)

        if item.detail:
            painter.setFont(mono(11))
            width = painter.fontMetrics().horizontalAdvance(item.detail)
            available = rect.width() - PAD * 2.0 - 8.0
            painter.setPen(fade(MUTED, (0.95 if chosen else 0.7) * alpha))
            painter.drawText(
                QPointF(rect.right() - PAD - min(width, available), baseline),
                item.detail,
            )
        y += ROW_H

    painter.restore()


class SuggestionList(QWidget):
    """The popup's list. The navigator paints its own, in its own space.

    The box opens and shuts rather than blinking in and out, so the card grows
    into it. Its rows are held for the length of a close: the completer drops
    them the instant the line changes, and an empty box folding shut is not the
    same thing as a list going away.
    """

    #: The card re-measures itself whenever the box changes height.
    changed = Signal()

    def __init__(self, completer: Completer, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.completer = completer
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._shown = 0.0
        self._held: list[tuple[int, Suggestion]] = []
        self.setFixedHeight(0)

    @property
    def target(self) -> float:
        return height_for(self.completer)

    def advance(self, delta: float) -> bool:
        """Step the box toward the list's height. True while moving."""
        target = self.target
        if self.completer.active:
            self._held = self.completer.window()
        if settled(self._shown, target):
            if self._shown != target:
                self._shown = target
                self._apply()
            if not self.completer.active:
                self._held = []
            return False
        self._shown = chase(self._shown, target, delta)
        self._apply()
        return True

    def sync(self, snap: bool = False) -> None:
        if snap:
            self._shown = self.target
            self._held = self.completer.window()
            self._apply()
        self.update()

    def _apply(self) -> None:
        height = int(round(self._shown))
        if height != self.height():
            self.setFixedHeight(height)
        self.setVisible(height > 0)
        self.update()
        self.changed.emit()

    def paintEvent(self, event) -> None:
        if not self.completer.active and not self._held:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        paint(
            painter,
            QRectF(0.0, 0.0, float(self.width()), float(self.height())),
            self.completer,
            framed=False,
            held=self._held,
        )
