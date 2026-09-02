"""Leaf views drawn inside the navigator's frame.

Every panel is custom-painted rather than built from widgets. That is not
stubbornness: the frame a panel lives in is mid-morph from a branch button when
it first appears, and real widgets cannot be interpolated into existence. So a
panel gets a rectangle and a painter, and reports how tall it wants to be.

Panels own the keyboard while they are open. `wants_text` decides whether plain
characters go to the panel or fall through to the command line.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QKeyEvent, QPainter, QPen

from .. import APP_NAME, APP_VERSION
from ..apps import App
from ..apps import index as app_index
from ..apps import launch as launch_app
from ..commands import Suggestion, apply_completion, complete, lookup
from ..config import SCHEMA, settings
from ..notes import CATEGORIES, NOTE, TODO, Entry, notebook, parse_due
from ..transcript import transcript
from ..tree import Node
from .motion import chase
from .paint import ACCENT, BAD, MUTED, TEXT, WARN, fade, mono

log = logging.getLogger(__name__)

PAD_X = 34.0
PAD_Y = 26.0
HEADER_H = 74.0
ROW_H = 30.0
FOOTER_H = 34.0
MAX_ROWS = 14

#: Footer buttons.
CHIP_H = 20.0
CHIP_GAP = 7.0


# --------------------------------------------------------------- primitives


def _text(
    painter: QPainter, x: float, baseline: float, string: str,
    colour, alpha: float, size: int = 14, bold: bool = False,
) -> None:
    painter.setFont(mono(size, bold))
    painter.setPen(fade(colour, alpha))
    painter.drawText(QPointF(x, baseline), string)


def _elide(painter: QPainter, string: str, size: int, limit: float, bold: bool = False) -> str:
    painter.setFont(mono(size, bold))
    metrics = painter.fontMetrics()
    if metrics.horizontalAdvance(string) <= limit:
        return string
    while string and metrics.horizontalAdvance(string + "…") > limit:
        string = string[:-1]
    return string + "…"


def _button(
    painter: QPainter, x: float, baseline: float, action, alpha: float,
    hot: bool = False,
) -> float:
    """A footer button: its key in a box, then what it does. Returns its width.

    Drawn as a target rather than as a line of grey prose, because it is one —
    the navigator forwards clicks here, so these can be pressed as well as
    typed.
    """
    painter.setFont(mono(11, True))
    key_w = painter.fontMetrics().horizontalAdvance(action.key) + 12.0
    painter.setFont(mono(11))
    label_w = painter.fontMetrics().horizontalAdvance(action.label) + 10.0
    width = key_w + label_w
    rect = QRectF(x, baseline - CHIP_H + 5.0, width, CHIP_H)

    tone = ACCENT if (hot or action.live) else MUTED
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(fade(tone, (0.20 if hot else 0.10) * alpha))
    painter.drawRoundedRect(rect, 4.0, 4.0)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(fade(tone, (0.55 if hot or action.live else 0.25) * alpha), 0.8))
    painter.drawRoundedRect(rect, 4.0, 4.0)

    middle = rect.center().y() + 4.0
    _text(painter, x + 6.0, middle, action.key, ACCENT,
          (1.0 if hot or action.live else 0.85) * alpha, 11, True)
    _text(painter, x + key_w, middle, action.label,
          TEXT if hot else MUTED, (1.0 if hot else 0.9) * alpha, 11)
    return width


def _draft_line(
    painter: QPainter, rect: QRectF, label: str, draft: str, alpha: float
) -> QRectF:
    """An inline entry line at the top of a panel. Returns the room left below.

    A capture form that opens *inside* the list it adds to, rather than being
    somewhere else you have to navigate to and come back from.
    """
    _text(painter, rect.x(), rect.y() + 16.0, label, MUTED, alpha, 11)
    painter.setFont(mono(11))
    inset = painter.fontMetrics().horizontalAdvance(label) + 14.0
    _text(painter, rect.x() + inset, rect.y() + 17.0, draft or "…", TEXT, alpha, 15, True)
    painter.setFont(mono(15, True))
    caret = rect.x() + inset + painter.fontMetrics().horizontalAdvance(draft) + 2.0
    painter.fillRect(QRectF(caret, rect.y() + 4.0, 8.0, 17.0), fade(ACCENT, alpha))
    rule = rect.y() + 32.0
    painter.setPen(QPen(fade(ACCENT, 0.22 * alpha), 0.7))
    painter.drawLine(QPointF(rect.x(), rule), QPointF(rect.right(), rule))
    return QRectF(rect.x(), rule, rect.width(), max(0.0, rect.bottom() - rule))


def _chip(
    painter: QPainter, x: float, centre_y: float, label: str, alpha: float,
    colour=None,
) -> float:
    """A small rounded tag. Returns its width."""
    colour = colour if colour is not None else MUTED
    painter.setFont(mono(11))
    width = painter.fontMetrics().horizontalAdvance(label) + 16.0
    rect = QRectF(x, centre_y - 9.0, width, 18.0)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(fade(colour, 0.14 * alpha))
    painter.drawRoundedRect(rect, 4.0, 4.0)
    painter.setPen(fade(colour, 0.95 * alpha))
    painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), label)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    return width


class Field:
    """A single-line text input, painted rather than a QLineEdit."""

    def __init__(self, label: str, hint: str = "", value: str = "") -> None:
        self.label = label
        self.hint = hint
        self.value = value

    def type(self, char: str) -> None:
        self.value += char

    def backspace(self) -> None:
        self.value = self.value[:-1]

    def clear(self) -> None:
        self.value = ""


# --------------------------------------------------------------- base panel


@dataclass(frozen=True)
class Action:
    """One thing a panel can do, offered as a button rather than a rumour.

    The old panels only advertised their keys in a line of grey text along the
    bottom, which meant every one of them was a thing you had to already know.
    An action is the key *and* a target you can click.
    """

    key: str
    label: str
    #: Shown pressed-in, for a toggle that is currently on.
    live: bool = False


class Panel:
    """A leaf view. Subclasses paint themselves and handle their own keys."""

    wants_text = False
    title = ""
    subtitle = ""
    #: Installed by the navigator. A panel calls this to ask the whole overlay
    #: to stand down — the launcher does it so the app it just started gets
    #: the focus instead of us.
    request_close: Callable[[], None] | None = None
    #: Also installed by the navigator: run a line the way the prompt would.
    request_run: Callable[[str], None] | None = None

    def __init__(self) -> None:
        #: Where each action chip was drawn last frame, for hit-testing.
        self._chips: list[tuple[QRectF, Action]] = []
        self._hot_chip: Action | None = None

    def enter(self) -> None:
        """Called each time the panel is opened."""

    def leave(self) -> None:
        """Called when the panel is closed."""

    def advance(self, delta: float) -> None:
        """One frame of the navigator's clock. Panels that move override it."""

    def rows(self) -> int:
        """How many body rows the panel wants to show."""
        return 0

    def content_height(self) -> float:
        # Padding counts twice (top and bottom), plus the lead-in before the
        # first row. Under-counting it silently clips the last row.
        return PAD_Y * 2.0 + HEADER_H + self.rows() * ROW_H + 24.0 + FOOTER_H

    def actions(self) -> list[Action]:
        """The buttons along the footer. Escape is always there and implied."""
        return []

    def invoke(self, action: Action) -> bool:
        """Do what a button says. Same path a keypress takes."""
        return False

    def hint(self) -> str:
        """Free text shown beside the buttons, for state rather than actions."""
        return ""

    def paint_body(self, painter: QPainter, rect: QRectF, alpha: float) -> None:
        raise NotImplementedError

    def key(self, event: QKeyEvent) -> bool:
        return False

    def escape(self) -> bool:
        """First refusal on Escape. True to keep the panel open.

        Only for a panel with something *inside* it to back out of — a capture
        line that is open. Otherwise Escape belongs to the navigator.
        """
        return False

    def text(self, char: str) -> bool:
        return False

    def backspace(self) -> bool:
        return False

    # ---------------------------------------------------------------- mouse

    def click(self, point: QPointF) -> bool:
        """A left click inside the panel. True if the panel consumed it."""
        for rect, action in self._chips:
            if rect.contains(point):
                return self.invoke(action)
        return self.click_body(point)

    def click_body(self, point: QPointF) -> bool:
        return False

    def hover(self, point: QPointF) -> bool:
        """Track the pointer. True when something under it changed."""
        found = next((a for rect, a in self._chips if rect.contains(point)), None)
        changed = found is not self._hot_chip
        self._hot_chip = found
        return changed or self.hover_body(point)

    def hover_body(self, point: QPointF) -> bool:
        return False

    def targets(self) -> list[QRectF]:
        """Everything clickable, so the navigator can set the cursor."""
        return [rect for rect, _ in self._chips]

    # -------------------------------------------------------------- chrome

    def paint(self, painter: QPainter, rect: QRectF, alpha: float) -> None:
        inner = rect.adjusted(PAD_X, PAD_Y, -PAD_X, -PAD_Y)
        _text(painter, inner.x(), inner.y() + 26.0, f"/{self.title}", ACCENT, alpha, 26, True)
        _text(painter, inner.x(), inner.y() + 46.0, self.subtitle, MUTED, alpha, 12)

        rule_y = inner.y() + HEADER_H - 16.0
        painter.setPen(QPen(fade(ACCENT, 0.22 * alpha), 0.7))
        painter.drawLine(QPointF(inner.x(), rule_y), QPointF(inner.right(), rule_y))

        body = QRectF(
            inner.x(), inner.y() + HEADER_H,
            inner.width(), max(0.0, inner.height() - HEADER_H - FOOTER_H),
        )
        if body.height() > 4.0:
            self.paint_body(painter, body, alpha)

        self._paint_actions(painter, inner, alpha)

    def _paint_actions(self, painter: QPainter, inner: QRectF, alpha: float) -> None:
        """The footer: buttons on the left, whatever state there is on the right."""
        self._chips = []
        x, baseline = inner.x(), inner.bottom()
        for action in [*self.actions(), Action("esc", "back")]:
            width = _button(
                painter, x, baseline, action, alpha,
                hot=action is self._hot_chip,
            )
            self._chips.append(
                (QRectF(x, baseline - CHIP_H + 5.0, width, CHIP_H), action)
            )
            x += width + CHIP_GAP

        note = self.hint()
        if note:
            painter.setFont(mono(11))
            width = painter.fontMetrics().horizontalAdvance(note)
            if inner.right() - width > x + CHIP_GAP:
                _text(painter, inner.right() - width, baseline, note, MUTED,
                      0.8 * alpha, 11)


class ListPanel(Panel):
    """Shared selection and scrolling for the list-shaped panels.

    The selection and the scroll position are each kept twice: the real index,
    which steps a whole row at a time, and the drawn one, which chases it. A
    highlight that teleports down the list makes you re-find it every keypress;
    one that slides carries your eye with it.
    """

    def __init__(self) -> None:
        super().__init__()
        self.selected = 0
        self.offset = 0
        #: Drawn positions, chasing `selected` and `offset`.
        self.cursor = 0.0
        self.scroll = 0.0
        #: Where the rows were drawn last frame, so they can be clicked.
        self._rows: list[tuple[QRectF, int]] = []

    def enter(self) -> None:
        # Opening a panel is an arrival, not a move: the frame is still
        # unfolding from a branch button, and a highlight sliding down from
        # wherever it was last time would be a second animation over the top.
        self.settle()

    def settle(self) -> None:
        self.cursor = float(self.selected)
        self.scroll = float(self.offset)

    def advance(self, delta: float) -> None:
        self.cursor = chase(self.cursor, float(self.selected), delta, snap=0.004)
        self.scroll = chase(self.scroll, float(self.offset), delta, snap=0.004)

    def count(self) -> int:
        return 0

    def rows(self) -> int:
        return max(1, min(self.count(), MAX_ROWS))

    def visible(self) -> range:
        return range(self.offset, min(self.count(), self.offset + self.rows()))

    def sliding(self, top: float, width: float = 0.0) -> list[tuple[int, float]]:
        """`(index, baseline)` for every row worth drawing, at eased positions.

        One row wider than the window at each end, because a list mid-scroll
        has a row on the way in and a row on the way out. Both are clipped by
        the caller — see `body_clip`.

        Also records where each row landed, so the list can be clicked.
        """
        total = self.count()
        first = max(0, int(self.scroll) - 1)
        last = min(total, first + self.rows() + 2)
        placed = [(i, top + (i - self.scroll) * ROW_H) for i in range(first, last)]
        self._rows = [
            (QRectF(self._left, y - ROW_H + 8.0, width or self._width, ROW_H), i)
            for i, y in placed
        ]
        return placed

    #: The last body rectangle, remembered so a click can be mapped to a row
    #: without every panel having to pass its geometry down twice.
    _left = 0.0
    _width = 0.0

    def body_clip(self, rect: QRectF) -> QRectF:
        """`rect`, widened to admit the selection band's overhang."""
        self._left, self._width = rect.x() - 10.0, rect.width() + 20.0
        return QRectF(rect.x() - 12.0, rect.y(), rect.width() + 24.0, rect.height())

    def activate(self) -> bool:
        """What Enter does on the selected row. Clicking a row does it too."""
        return False

    def click_body(self, point: QPointF) -> bool:
        for rect, index in self._rows:
            if not rect.contains(point):
                continue
            if index == self.selected:
                return self.activate()
            self.selected = index
            self.move(0)
            return True
        return False

    def hover_body(self, point: QPointF) -> bool:
        return any(rect.contains(point) for rect, _ in self._rows)

    def targets(self) -> list[QRectF]:
        return [*super().targets(), *(rect for rect, _ in self._rows)]

    def move(self, delta: int) -> None:
        total = self.count()
        if total == 0:
            self.selected = self.offset = 0
            return
        self.selected = max(0, min(total - 1, self.selected + delta))
        window = self.rows()
        self.offset = max(0, min(self.offset, self.selected))
        if self.selected >= self.offset + window:
            self.offset = self.selected - window + 1
        self.offset = max(0, min(self.offset, max(0, total - window)))

    def key(self, event: QKeyEvent) -> bool:
        key = event.key()
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Tab):
            self.move(1)
            return True
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Backtab):
            self.move(-1)
            return True
        if key == Qt.Key.Key_PageDown:
            self.move(self.rows())
            return True
        if key == Qt.Key.Key_PageUp:
            self.move(-self.rows())
            return True
        if key == Qt.Key.Key_Home:
            self.selected = self.offset = 0
            return True
        if key == Qt.Key.Key_End:
            self.selected = max(0, self.count() - 1)
            self.move(0)
            return True
        return False

    def paint_selection(
        self, painter: QPainter, rect: QRectF, top: float, alpha: float
    ) -> None:
        """The band, at its eased row. Drawn once, before the rows."""
        if self.count() == 0:
            return
        row = top + (self.cursor - self.scroll) * ROW_H
        band = QRectF(rect.x() - 10.0, row - ROW_H + 8.0, rect.width() + 20.0, ROW_H)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fade(ACCENT, 0.10 * alpha))
        painter.drawRect(band)
        painter.setBrush(fade(ACCENT, 0.85 * alpha))
        painter.drawRect(QRectF(band.x(), band.y(), 2.0, band.height()))
        painter.setBrush(Qt.BrushStyle.NoBrush)

    def paint_empty(self, painter: QPainter, rect: QRectF, alpha: float, message: str) -> None:
        _text(painter, rect.x(), rect.y() + 22.0, message, MUTED, alpha, 13)


# ------------------------------------------------------------------ readout


@dataclass(frozen=True)
class Offer:
    """A row in a command panel: something to run, or something to insert."""

    kind: str  # "run" | "fill" | "info"
    text: str
    label: str
    detail: str = ""


class CommandPanel(Panel):
    """A working view of one command, not a page about it.

    The old version of this was a readout: it told you the syntax and left you
    to go and type it somewhere else. Which meant the navigator was a menu you
    looked things up in and then abandoned. Here the syntax rows *are* the
    controls — there is an argument line you type into, live completions from
    the command's own provider, and Enter runs it. The output comes back into
    the panel, so a command can be used, corrected and used again without
    leaving the place you found it.
    """

    wants_text = True

    def __init__(self, node: Node) -> None:
        super().__init__()
        self.node = node
        self.title = node.name
        self.subtitle = node.summary
        self.command = lookup(node.command or node.name)
        self.args = ""
        self.selected = 0
        #: Where the offer rows landed, so they can be clicked before the
        #: first paint has had a chance to record them.
        self._rows: list[tuple[QRectF, int]] = []
        #: The last thing this panel ran, shown underneath.
        self.output: tuple[str, ...] = ()
        self.ok = True
        #: A command that cannot be undone gets pressed twice. At the prompt
        #: the confirmation is a word you have to type; here it would be one
        #: click on a row you were only reading, so the panel arms first.
        self.armed = ""
        #: Installed by the navigator: runs a line the way the prompt does.
        self.request_run: Callable[[str], None] | None = None

    # ------------------------------------------------------------- the rows

    def line(self) -> str:
        return f"{self.title} {self.args}".rstrip()

    def offers(self) -> list[Offer]:
        """What is on the menu right now: completions, or examples to try."""
        command = self.command
        if command is None:
            return []
        found: list[Offer] = []
        if self.args.strip() or self.args.endswith(" "):
            for suggestion in complete(f"{self.title} {self.args}"):
                if suggestion.insertable:
                    found.append(
                        Offer("fill", suggestion.insert, suggestion.label,
                              suggestion.detail)
                    )
        if found:
            return found
        for example in command.examples:
            found.append(
                Offer("run", example.removeprefix(command.name).strip(),
                      example, "run it")
            )
        for form, description in command.forms:
            found.append(Offer("info", "", form, description))
        return found

    def count(self) -> int:
        return len(self.offers())

    def rows(self) -> int:
        return max(1, min(self.count(), 9))

    def content_height(self) -> float:
        body = 30.0 + 26.0  # the argument line, then the rule under it
        body += self.rows() * ROW_H + 10.0
        if self.output:
            body += 16.0 + len(self.output) * 20.0
        return PAD_Y * 2.0 + HEADER_H + body + FOOTER_H

    # ---------------------------------------------------------------- input

    def enter(self) -> None:
        self.args = ""
        self.selected = 0
        self.output = ()
        self.armed = ""

    def hint(self) -> str:
        command = self.command
        if command is None:
            return "no command by this name"
        if not command.available:
            return f"needs {command.requires} — not installed"
        if command.confirm:
            return f"needs '{command.confirm}' to confirm"
        return "  ·  ".join(command.aliases) if command.aliases else ""

    def actions(self) -> list[Action]:
        offers = self.offers()
        chosen = offers[self.selected] if self.selected < len(offers) else None
        out = [
            Action("enter", f"confirm {self.armed}" if self.armed else "run",
                   live=bool(self.armed))
        ]
        if chosen is not None and chosen.kind == "fill":
            out.append(Action("tab", "fill in"))
        if self.args:
            out.append(Action("ctrl+u", "clear"))
        return out

    def invoke(self, action: Action) -> bool:
        if action.key == "enter":
            return self._run()
        if action.key == "tab":
            return self._fill()
        if action.key == "ctrl+u":
            self.args = ""
            self.selected = 0
            return True
        return False

    def _run(self, override: str = "") -> bool:
        if self.command is None or self.request_run is None:
            return False
        line = override or self.line()
        if self._needs_arming(line):
            self.armed = line
            self.output, self.ok = (f"press again to {line}",), False
            return True
        self.armed = ""
        self.request_run(line)
        # The result is already in the shared scroll-back; take the last of it
        # rather than running the command a second time to see what it said.
        if not transcript.empty:
            last = transcript.entries[-1]
            self.output, self.ok = last.lines, last.ok
        return True

    def _needs_arming(self, line: str) -> bool:
        """Whether this exact line would do something irreversible, unasked.

        Only lines that carry the confirmation word count: `shutdown` on its
        own is harmless — the registry refuses it — and making the panel ask
        about a command that is going to refuse anyway would train the habit of
        clicking through the question.
        """
        command = self.command
        if command is None or not command.confirm or self.armed == line:
            return False
        return command.confirm in line.lower().split()

    def _fill(self) -> bool:
        offers = self.offers()
        if self.selected >= len(offers):
            return False
        chosen = offers[self.selected]
        if chosen.kind != "fill":
            return False
        whole = apply_completion(f"{self.title} {self.args}", Suggestion(chosen.text, chosen.label))
        self.args = whole[len(self.title):].lstrip()
        self.selected = 0
        return True

    def activate(self) -> bool:
        """Enter. Always runs something — Tab is what fills things in.

        Enter completing the word under the cursor instead of running would
        mean a line you had finished typing could never be submitted, which is
        precisely the trap the first version of this fell into.

        An example row only wins when you have actually moved onto it. Letting
        the highlight's *resting* position win means typing an argument and
        pressing Enter runs somebody else's example instead of your line.
        """
        offers = self.offers()
        picked = self.selected > 0 or not self.args.strip()
        if picked and self.selected < len(offers) and offers[self.selected].kind == "run":
            return self._run(f"{self.title} {offers[self.selected].text}".strip())
        return self._run()

    def move(self, delta: int) -> None:
        total = self.count()
        if total:
            self.selected = max(0, min(total - 1, self.selected + delta))
        self.armed = ""  # moving off the row disarms it

    def key(self, event: QKeyEvent) -> bool:
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            return self.activate()
        if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            return self._fill()
        if key == Qt.Key.Key_Down:
            self.move(1)
            return True
        if key == Qt.Key.Key_Up:
            self.move(-1)
            return True
        if key == Qt.Key.Key_U and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.args = ""
            return True
        return False

    def text(self, char: str) -> bool:
        self.args += char
        self.selected = 0
        self.armed = ""  # a changed line is a different intention
        return True

    def backspace(self) -> bool:
        if not self.args:
            return False
        self.args = self.args[:-1]
        self.selected = 0
        self.armed = ""
        return True

    # ---------------------------------------------------------------- mouse

    def click_body(self, point: QPointF) -> bool:
        """A row does what its own kind says, so one click is never a surprise.

        Clicking a completion inserts it — nobody clicks a half-finished
        argument expecting the command to go off.
        """
        for rect, index in self._rows:
            if not rect.contains(point):
                continue
            self.selected = index
            offers = self.offers()
            if index >= len(offers):
                return True
            if offers[index].kind == "fill":
                return self._fill()
            if offers[index].kind == "run":
                return self._run(f"{self.title} {offers[index].text}".strip())
            return True
        return False

    def hover_body(self, point: QPointF) -> bool:
        return any(rect.contains(point) for rect, _ in self._rows)

    def targets(self) -> list[QRectF]:
        return [*super().targets(), *(rect for rect, _ in self._rows)]

    # -------------------------------------------------------------- drawing

    def paint_body(self, painter: QPainter, rect: QRectF, alpha: float) -> None:
        self._rows = []
        x, y = rect.x(), rect.y() + 20.0
        command = self.command

        # The live line, exactly as it would be typed at the prompt.
        _text(painter, x, y, f"{self.title}", ACCENT, alpha, 17, True)
        painter.setFont(mono(17, True))
        inset = painter.fontMetrics().horizontalAdvance(self.title) + 12.0
        placeholder = command.argument_hint if command else ""
        if self.args:
            _text(painter, x + inset, y, self.args, TEXT, alpha, 17)
            painter.setFont(mono(17))
            caret = x + inset + painter.fontMetrics().horizontalAdvance(self.args) + 3.0
        else:
            _text(painter, x + inset, y, placeholder, MUTED, 0.55 * alpha, 15)
            caret = x + inset - 3.0
        painter.fillRect(QRectF(caret, y - 14.0, 9.0, 18.0), fade(ACCENT, alpha))

        if command is not None and not command.available:
            _chip(painter, rect.right() - 130.0, y - 6.0, "backend missing", alpha, WARN)

        y += 14.0
        painter.setPen(QPen(fade(ACCENT, 0.22 * alpha), 0.7))
        painter.drawLine(QPointF(x, y), QPointF(rect.right(), y))
        y += 24.0

        offers = self.offers()
        window = offers[: self.rows()] if self.selected < self.rows() else offers[
            self.selected - self.rows() + 1: self.selected + 1
        ]
        first = offers.index(window[0]) if window else 0

        painter.setFont(mono(13, True))
        gutter = min(
            max((painter.fontMetrics().horizontalAdvance(o.label) for o in window),
                default=0.0) + 26.0,
            rect.width() * 0.55,
        )
        for position, offer in enumerate(window):
            index = first + position
            chosen = index == self.selected
            row = QRectF(rect.x() - 10.0, y - ROW_H + 8.0, rect.width() + 20.0, ROW_H)
            self._rows.append((row, index))
            if chosen:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(fade(ACCENT, 0.10 * alpha))
                painter.drawRect(row)
                painter.setBrush(fade(ACCENT, 0.85 * alpha))
                painter.drawRect(QRectF(row.x(), row.y(), 2.0, row.height()))
                painter.setBrush(Qt.BrushStyle.NoBrush)
            mark = {"run": "▸", "fill": "+", "info": " "}[offer.kind]
            _text(painter, x - 2.0, y, mark, ACCENT, (0.9 if chosen else 0.5) * alpha, 13)
            _text(painter, x + 16.0, y,
                  _elide(painter, offer.label, 13, gutter - 34.0, chosen),
                  ACCENT if chosen else TEXT, alpha, 13, chosen)
            if offer.detail:
                _text(painter, x + gutter, y,
                      _elide(painter, offer.detail, 12, rect.width() - gutter),
                      MUTED, (0.95 if chosen else 0.75) * alpha, 12)
            y += ROW_H

        if self.output:
            y += 8.0
            painter.setPen(QPen(fade(ACCENT, 0.16 * alpha), 0.7))
            painter.drawLine(QPointF(x, y), QPointF(rect.right(), y))
            y += 18.0
            for line in self.output:
                if y > rect.bottom():
                    break
                _text(painter, x, y, _elide(painter, line, 12, rect.width()),
                      TEXT if self.ok else BAD, 0.95 * alpha, 12)
                y += 20.0


# ----------------------------------------------------------------- launcher


class LauncherPanel(ListPanel):
    wants_text = True
    title = "launch"
    subtitle = "every application on this machine"

    def __init__(self) -> None:
        super().__init__()
        self.query = ""
        self._hits: list[App] = []
        self._dirty = True

    def enter(self) -> None:
        self.query = ""
        self.selected = self.offset = 0
        self._dirty = True
        app_index.refresh()
        super().enter()

    def _results(self) -> list[App]:
        if self._dirty:
            self._hits = app_index.search(self.query)
            self._dirty = False
        return self._hits

    def invalidate(self) -> None:
        self._dirty = True

    def count(self) -> int:
        return len(self._results())

    def hint(self) -> str:
        if app_index.scanning and not app_index.apps:
            return "indexing the Start Menu…"
        return f"{self.count()} of {len(app_index.apps)}   ·   type to filter"

    def actions(self) -> list[Action]:
        return [Action("enter", "launch")]

    def invoke(self, action: Action) -> bool:
        return self.activate() if action.key == "enter" else False

    def text(self, char: str) -> bool:
        self.query += char
        self._retarget()
        return True

    def backspace(self) -> bool:
        if not self.query:
            return False
        self.query = self.query[:-1]
        self._retarget()
        return True

    def _retarget(self) -> None:
        # A different query is a different list. Sliding the highlight back to
        # the top of it would be animating between two things that have nothing
        # to do with each other.
        self.selected = self.offset = 0
        self.settle()
        self._dirty = True

    def activate(self) -> bool:
        hits = self._results()
        if 0 <= self.selected < len(hits) and launch_app(hits[self.selected]):
            # Get out of the way: the window about to appear should take the
            # foreground, not a full-screen overlay sitting on top.
            if self.request_close is not None:
                self.request_close()
        return True

    def key(self, event: QKeyEvent) -> bool:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            return self.activate()
        if event.key() == Qt.Key.Key_F5:
            app_index.refresh(force=True)
            self._dirty = True
            return True
        return super().key(event)

    def paint_body(self, painter: QPainter, rect: QRectF, alpha: float) -> None:
        # Search line
        _text(painter, rect.x(), rect.y() + 16.0, "search", MUTED, alpha, 11)
        shown = self.query or "—"
        _text(painter, rect.x() + 62.0, rect.y() + 17.0, shown, TEXT, alpha, 15, True)
        total = len(app_index.apps)
        status = f"{self.count()} of {total}" if total else "indexing…"
        painter.setFont(mono(11))
        _text(
            painter,
            rect.right() - painter.fontMetrics().horizontalAdvance(status),
            rect.y() + 16.0, status, MUTED, alpha, 11,
        )

        hits = self._results()
        top = rect.y() + 48.0
        if not hits:
            self.paint_empty(
                painter, QRectF(rect.x(), top - 22.0, rect.width(), ROW_H), alpha,
                "no match" if total else "no applications indexed yet",
            )
            return

        painter.save()
        painter.setClipRect(self.body_clip(rect))
        self.paint_selection(painter, rect, top, alpha)
        for i, y in self.sliding(top):
            app = hits[i]
            chosen = i == self.selected
            name = _elide(painter, app.name, 14, rect.width() - 90.0, chosen)
            _text(
                painter, rect.x() + 4.0, y, name,
                ACCENT if chosen else TEXT, alpha, 14, chosen,
            )
            painter.setFont(mono(10))
            tag = app.source
            _text(
                painter,
                rect.right() - painter.fontMetrics().horizontalAdvance(tag),
                y, tag, MUTED, 0.8 * alpha, 10,
            )
        painter.restore()


# -------------------------------------------------------------------- todos


class TodoPanel(ListPanel):
    title = "todo"
    subtitle = "open items, soonest due first"

    def __init__(self) -> None:
        super().__init__()
        self.show_done = False
        #: The inline capture line, so a todo can be made where the todos are
        #: rather than by backing out to a different panel.
        self.capturing = False
        self.draft = ""

    def items(self) -> list[Entry]:
        todos = notebook.all(TODO)
        if self.show_done:
            return todos
        return [entry for entry in todos if not entry.done]

    def count(self) -> int:
        return len(self.items())

    @property
    def wants_text(self) -> bool:  # type: ignore[override]
        # Only while the capture line is open, so `d` deletes the rest of the
        # time instead of typing a letter nobody can see.
        return self.capturing

    def hint(self) -> str:
        open_count, total = notebook.counts()
        return f"{open_count} open of {total}"

    def actions(self) -> list[Action]:
        if self.capturing:
            return [Action("enter", "save"), Action("esc", "cancel")]
        return [
            Action("n", "new todo"),
            Action("space", "toggle done"),
            Action("d", "delete"),
            Action("h", "show done", live=self.show_done),
        ]

    def invoke(self, action: Action) -> bool:
        items = self.items()
        if action.key == "n":
            self.capturing, self.draft = True, ""
            return True
        if action.key == "enter" and self.capturing:
            return self._save()
        if action.key == "esc" and self.capturing:
            self.capturing, self.draft = False, ""
            return True
        if action.key == "space" and items:
            notebook.toggle(items[min(self.selected, len(items) - 1)])
            self.move(0)
            return True
        if action.key == "d" and items:
            notebook.remove(items[min(self.selected, len(items) - 1)])
            self.move(0)
            return True
        if action.key == "h":
            self.show_done = not self.show_done
            self.selected = self.offset = 0
            self.settle()
            return True
        return False

    def activate(self) -> bool:
        return self.invoke(Action("space", "toggle done"))

    def _save(self) -> bool:
        title = self.draft.strip()
        self.capturing, self.draft = False, ""
        if not title:
            return True
        notebook.add(Entry(title=title, kind=TODO))
        self.selected = self.offset = 0
        self.settle()
        return True

    def text(self, char: str) -> bool:
        if not self.capturing:
            return False
        self.draft += char
        return True

    def backspace(self) -> bool:
        if not self.capturing:
            return False
        self.draft = self.draft[:-1]
        return True

    def key(self, event: QKeyEvent) -> bool:
        key = event.key()
        if self.capturing:
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                return self._save()
            return False  # escape belongs to the navigator, which cancels us
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            return self.activate()
        for action in self.actions():
            wanted = {
                "n": Qt.Key.Key_N, "d": Qt.Key.Key_D,
                "h": Qt.Key.Key_H, "space": Qt.Key.Key_Space,
            }.get(action.key)
            if wanted is not None and key == wanted:
                return self.invoke(action)
        return super().key(event)

    def escape(self) -> bool:
        # Backs out of the capture line first, and only then out of the panel.
        if not self.capturing:
            return False
        self.capturing, self.draft = False, ""
        return True

    def leave(self) -> None:
        self.capturing, self.draft = False, ""

    def paint_body(self, painter: QPainter, rect: QRectF, alpha: float) -> None:
        if self.capturing:
            rect = _draft_line(painter, rect, "new todo", self.draft, alpha)
        items = self.items()
        if not items:
            self.paint_empty(
                painter, rect, alpha,
                "nothing here yet — press n to capture one",
            )
            return
        top = rect.y() + 20.0
        painter.save()
        painter.setClipRect(self.body_clip(rect))
        self.paint_selection(painter, rect, top, alpha)
        for i, y in self.sliding(top):
            entry = items[i]

            box = "[x]" if entry.done else "[ ]"
            _text(painter, rect.x() + 4.0, y, box, ACCENT, 0.9 * alpha, 14)

            colour = MUTED if entry.done else (WARN if entry.overdue else TEXT)
            title = _elide(painter, entry.title, 14, rect.width() - 250.0)
            _text(painter, rect.x() + 40.0, y, title, colour, alpha, 14)

            x = rect.right()
            if entry.due is not None:
                label = entry.due_label
                painter.setFont(mono(11))
                width = painter.fontMetrics().horizontalAdvance(label)
                _text(
                    painter, x - width, y, label,
                    WARN if entry.overdue else MUTED, alpha, 11,
                )
                x -= width + 14.0
            chip_w = _chip(painter, x - 74.0, y - 5.0, entry.category, alpha)
            del chip_w
        painter.restore()


# ----------------------------------------------------------------- new entry


class NewEntryPanel(Panel):
    wants_text = True
    title = "new"
    subtitle = "capture a todo or a note"

    def __init__(self) -> None:
        super().__init__()
        self.fields = [
            Field("title", "what needs doing"),
            Field("details", "optional description"),
            Field("due", "20m · 2h · tomorrow · 17:30 · 25/12"),
        ]
        self.focus = 0
        self.category = 0
        self.kind = TODO
        self.flash = ""

    def enter(self) -> None:
        for field in self.fields:
            field.clear()
        self.focus = 0
        self.flash = ""

    def rows(self) -> int:
        return 7

    def actions(self) -> list[Action]:
        return [
            Action("enter", "save"),
            Action("tab", "next field"),
            Action("ctrl+t", "todo" if self.kind == TODO else "note", live=True),
            Action("ctrl+←→", f"in {CATEGORIES[self.category]}"),
        ]

    def invoke(self, action: Action) -> bool:
        if action.key == "enter":
            self._save()
            return True
        if action.key == "tab":
            self.focus = (self.focus + 1) % len(self.fields)
            return True
        if action.key == "ctrl+t":
            self.kind = NOTE if self.kind == TODO else TODO
            return True
        if action.key == "ctrl+←→":
            self.category = (self.category + 1) % len(CATEGORIES)
            return True
        return False

    def hint(self) -> str:
        return self.flash

    def text(self, char: str) -> bool:
        self.fields[self.focus].type(char)
        self.flash = ""
        return True

    def backspace(self) -> bool:
        self.fields[self.focus].backspace()
        return True

    def key(self, event: QKeyEvent) -> bool:
        key = event.key()
        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)

        if key == Qt.Key.Key_Tab or (key == Qt.Key.Key_Down and not ctrl):
            self.focus = (self.focus + 1) % len(self.fields)
            return True
        if key == Qt.Key.Key_Backtab or (key == Qt.Key.Key_Up and not ctrl):
            self.focus = (self.focus - 1) % len(self.fields)
            return True
        if ctrl and key == Qt.Key.Key_Right:
            self.category = (self.category + 1) % len(CATEGORIES)
            return True
        if ctrl and key == Qt.Key.Key_Left:
            self.category = (self.category - 1) % len(CATEGORIES)
            return True
        if ctrl and key == Qt.Key.Key_T:
            self.kind = NOTE if self.kind == TODO else TODO
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._save()
            return True
        return False

    def _save(self) -> None:
        title = self.fields[0].value.strip()
        if not title:
            self.flash = "a title is required"
            return
        due = parse_due(self.fields[2].value)
        raw_due = self.fields[2].value.strip()
        entry = Entry(
            title=title,
            kind=self.kind,
            body=self.fields[1].value.strip(),
            category=CATEGORIES[self.category],
            due=due,
        )
        notebook.add(entry)
        for field in self.fields:
            field.clear()
        self.focus = 0
        if raw_due and due is None:
            self.flash = f"saved — but '{raw_due}' is not a date I understand"
        else:
            self.flash = f"saved to {entry.kind}s" + (
                f", due {entry.due_label}" if due else ""
            )

    def paint_body(self, painter: QPainter, rect: QRectF, alpha: float) -> None:
        y = rect.y() + 18.0
        for i, field in enumerate(self.fields):
            focused = i == self.focus
            _text(painter, rect.x(), y, field.label, MUTED, alpha, 11)
            box = QRectF(rect.x(), y + 6.0, rect.width(), 28.0)
            painter.setPen(QPen(fade(ACCENT, (0.8 if focused else 0.22) * alpha), 0.9))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(box, 3.0, 3.0)

            shown = field.value or ("" if focused else field.hint)
            colour = TEXT if field.value else MUTED
            painter.setFont(mono(14))
            text = _elide(painter, shown, 14, box.width() - 24.0)
            _text(painter, box.x() + 10.0, box.center().y() + 5.0, text, colour, alpha, 14)

            if focused:
                painter.setFont(mono(14))
                width = painter.fontMetrics().horizontalAdvance(field.value)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(fade(ACCENT, alpha))
                painter.drawRect(
                    QRectF(box.x() + 10.0 + width + 2.0, box.center().y() - 8.0, 8.0, 16.0)
                )
                painter.setBrush(Qt.BrushStyle.NoBrush)
            y += 54.0

        _text(painter, rect.x(), y + 8.0, "category", MUTED, alpha, 11)
        x = rect.x() + 74.0
        for i, name in enumerate(CATEGORIES):
            chosen = i == self.category
            width = _chip(
                painter, x, y + 4.0, name, alpha, ACCENT if chosen else MUTED
            )
            x += width + 8.0

        kind_label = "todo" if self.kind == TODO else "note"
        painter.setFont(mono(11))
        _text(
            painter,
            rect.right() - painter.fontMetrics().horizontalAdvance(f"kind  {kind_label}"),
            y + 9.0, f"kind  {kind_label}", ACCENT, alpha, 11,
        )


# ------------------------------------------------------------------- browse


class NoteListPanel(ListPanel):
    title = "browse"
    subtitle = "everything captured so far"

    def items(self) -> list[Entry]:
        return notebook.all()

    def count(self) -> int:
        return len(self.items())

    def hint(self) -> str:
        todos = sum(1 for entry in self.items() if entry.kind == TODO)
        return f"{todos} todo(s), {self.count() - todos} note(s)"

    def actions(self) -> list[Action]:
        return [
            Action("enter", "toggle done"),
            Action("t", "next category"),
            Action("d", "delete"),
        ]

    def invoke(self, action: Action) -> bool:
        items = self.items()
        if not items:
            return False
        entry = items[min(self.selected, len(items) - 1)]
        if action.key == "enter":
            if entry.kind != TODO:
                return False
            notebook.toggle(entry)
            self.move(0)
            return True
        if action.key == "t":
            spot = (CATEGORIES.index(entry.category) + 1) % len(CATEGORIES) \
                if entry.category in CATEGORIES else 0
            entry.category = CATEGORIES[spot]
            notebook.save()
            return True
        if action.key == "d":
            notebook.remove(entry)
            self.move(0)
            return True
        return False

    def activate(self) -> bool:
        return self.invoke(Action("enter", "toggle done"))

    def key(self, event: QKeyEvent) -> bool:
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            return self.activate()
        if key == Qt.Key.Key_D:
            return self.invoke(Action("d", "delete"))
        if key == Qt.Key.Key_T:
            return self.invoke(Action("t", "next category"))
        return super().key(event)

    def paint_body(self, painter: QPainter, rect: QRectF, alpha: float) -> None:
        items = self.items()
        if not items:
            self.paint_empty(painter, rect, alpha, "nothing captured yet")
            return
        top = rect.y() + 20.0
        painter.save()
        painter.setClipRect(self.body_clip(rect))
        self.paint_selection(painter, rect, top, alpha)
        for i, y in self.sliding(top):
            entry = items[i]

            mark = "·" if entry.kind == NOTE else ("x" if entry.done else "o")
            _text(painter, rect.x() + 4.0, y, mark, ACCENT, 0.9 * alpha, 14)

            body = f"{entry.title}" + (f"   {entry.body}" if entry.body else "")
            body = _elide(painter, body, 14, rect.width() - 240.0)
            _text(
                painter, rect.x() + 30.0, y, body,
                MUTED if entry.done else TEXT, alpha, 14,
            )

            label = entry.created_label
            painter.setFont(mono(11))
            width = painter.fontMetrics().horizontalAdvance(label)
            _text(painter, rect.right() - width, y, label, MUTED, alpha, 11)
            _chip(painter, rect.right() - width - 88.0, y - 5.0, entry.category, alpha)
        painter.restore()


# ----------------------------------------------------------------- settings


class SettingsPanel(ListPanel):
    title = "settings"
    subtitle = "appearance, motion and startup — saved as you change them"

    def count(self) -> int:
        return len(SCHEMA)

    def rows(self) -> int:
        return len(SCHEMA)

    def hint(self) -> str:
        # The selected row's help goes here rather than under the row itself,
        # where it would overlap the row below.
        return SCHEMA[min(self.selected, len(SCHEMA) - 1)].help

    def actions(self) -> list[Action]:
        return [Action("enter", "change"), Action("←", "back"), Action("→", "on")]

    def invoke(self, action: Action) -> bool:
        option = SCHEMA[min(self.selected, len(SCHEMA) - 1)]
        settings.cycle(option.key, -1 if action.key == "←" else 1)
        return True

    def activate(self) -> bool:
        return self.invoke(Action("enter", "change"))

    def key(self, event: QKeyEvent) -> bool:
        key = event.key()
        if key in (Qt.Key.Key_Right, Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            return self.invoke(Action("→", "on"))
        if key == Qt.Key.Key_Left:
            return self.invoke(Action("←", "back"))
        return super().key(event)

    def paint_body(self, painter: QPainter, rect: QRectF, alpha: float) -> None:
        top = rect.y() + 20.0
        painter.save()
        painter.setClipRect(self.body_clip(rect))
        self.paint_selection(painter, rect, top, alpha)
        for i, y in self.sliding(top):
            option = SCHEMA[i]
            live = settings.enabled(option.key)
            row_alpha = alpha * (1.0 if live else 0.4)
            chosen = i == self.selected

            _text(
                painter, rect.x() + 4.0, y, option.label,
                ACCENT if chosen else TEXT, row_alpha, 14, chosen,
            )
            value = settings.label_for(option.key)
            painter.setFont(mono(13, True))
            width = painter.fontMetrics().horizontalAdvance(value)
            _text(painter, rect.right() - width, y, value, ACCENT, row_alpha, 13, True)
        painter.restore()


# -------------------------------------------------------------------- about


class AboutPanel(Panel):
    title = "about"
    subtitle = f"{APP_NAME} {APP_VERSION}"

    def rows(self) -> int:
        return 10

    def paint_body(self, painter: QPainter, rect: QRectF, alpha: float) -> None:
        from ..commands import commands as registered
        from ..commands import groups as registered_groups

        open_count, total = notebook.counts()
        lines = [
            ("what", "an always-resident command layer for Windows"),
            ("summon", "a global hotkey, then `open` for this navigator"),
            ("", ""),
            ("commands", f"{len(registered())} in {len(registered_groups())} groups"),
            ("applications", f"{len(app_index.apps)} indexed from the Start Menu"),
            ("captured", f"{open_count} open of {total} todos"),
            ("accent", settings.label_for("accent")),
            ("", ""),
            ("anywhere", "every branch here is a command you can type"),
        ]
        y = rect.y() + 20.0
        for label, value in lines:
            if not label:
                y += 12.0
                continue
            _text(painter, rect.x(), y, label, MUTED, alpha, 12)
            _text(painter, rect.x() + 130.0, y, value, TEXT, alpha, 13)
            y += 26.0


# ------------------------------------------------------------------ factory

_SINGLETONS: dict[str, Panel] = {}

_BUILDERS = {
    "launcher": LauncherPanel,
    "todo": TodoPanel,
    "new": NewEntryPanel,
    "notes": NoteListPanel,
    "settings": SettingsPanel,
    "about": AboutPanel,
}


def make_panel(node: Node) -> Panel:
    """One instance per panel kind, so selection survives between visits."""
    builder = _BUILDERS.get(node.panel)
    if builder is None:
        # Every other leaf is a command, and gets a panel that runs it. Not
        # cached: there is one of these per command, and they are cheap.
        return CommandPanel(node)
    panel = _SINGLETONS.get(node.panel)
    if panel is None:
        panel = builder()
        _SINGLETONS[node.panel] = panel
    return panel
