"""The scroll-back both command lines share.

A prompt that forgets everything the moment you press Enter is a prompt you
cannot check your own work at. This holds the last handful of commands and what
they printed, so the popup and the navigator's docked line are two views of one
session rather than two amnesiac boxes.

It lives outside the UI on purpose: the registry records into it, and both
surfaces read from it, so neither has to know the other exists. Listeners are
called on every change, which is what lets the popup re-measure its card as the
history grows.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: How many commands are kept. Enough to see what you have been doing without
#: the prompt turning into a log viewer.
LIMIT = 10

#: How many *rows* either command line draws at once. One command can print
#: many lines — `help` prints one per command — so the row budget is what
#: actually bounds how far the surfaces grow.
#:
#: Shared by both, and deliberately: the two command lines hand over to each
#: other mid-animation, and a card showing more rows than the overlay's last
#: frame drew would snap at the swap. Rows past the budget are not lost — the
#: popup's block scrolls back through them — so this is how much history is
#: *up* at once, not how much is kept.
VIEW_ROWS = 24

# Row kinds, so a painter can colour without re-deriving what it is looking at.
COMMAND = "command"
OUTPUT = "output"
ERROR = "error"


@dataclass(frozen=True)
class Entry:
    #: The line as typed, without the prompt.
    command: str
    #: What it printed, already split into display lines.
    lines: tuple[str, ...] = ()
    ok: bool = True


class Transcript:
    def __init__(self, limit: int = LIMIT) -> None:
        self.limit = limit
        self._entries: list[Entry] = []
        self.listeners: list[Callable[[], None]] = []

    @property
    def entries(self) -> list[Entry]:
        return list(self._entries)

    @property
    def empty(self) -> bool:
        return not self._entries

    def record(self, command: str, lines: Iterable[str] = (), ok: bool = True) -> Entry:
        entry = Entry(command.strip(), tuple(lines), ok)
        self._entries.append(entry)
        # Trim from the front: the oldest command is the one you have stopped
        # caring about.
        overflow = len(self._entries) - max(1, self.limit)
        if overflow > 0:
            del self._entries[:overflow]
        self._notify()
        return entry

    def amend(self, entry: Entry, lines: Iterable[str] = (), ok: bool = True) -> Entry | None:
        """Replace a row already on screen with what finally came back.

        A command that has to reach the network prints "looking it up…" the
        instant you press Enter and the answer some moments later, and both are
        the same row: the alternative is a two-line stutter where the first
        line is a lie the moment the second arrives.

        Compared by identity rather than value, because two runs of the same
        command are two rows and amending the wrong one is a real possibility.
        Returns `None` when the entry has since been trimmed or cleared, which
        is not a failure — you asked, then moved on, and the answer missed you.
        """
        for index, held in enumerate(self._entries):
            if held is entry:
                revised = Entry(entry.command, tuple(lines), ok)
                self._entries[index] = revised
                self._notify()
                return revised
        return None

    def clear(self) -> int:
        """Drop everything. Returns how many commands were let go."""
        count = len(self._entries)
        self._entries.clear()
        self._notify()
        return count

    def rows(self, limit: int | None = VIEW_ROWS) -> list[tuple[str, str]]:
        """Flattened `(kind, text)` display rows, oldest first.

        Trimmed from the front when there are more than `limit`, so the newest
        output is always the row nearest the prompt — a command whose output
        overflows the budget still shows its *end*, which is where the answer is.
        """
        out: list[tuple[str, str]] = []
        for entry in self._entries:
            if entry.command:
                out.append((COMMAND, entry.command))
            kind = OUTPUT if entry.ok else ERROR
            out.extend((kind, line) for line in entry.lines)
        if limit is not None and len(out) > limit:
            out = out[len(out) - limit:]
        return out

    def _notify(self) -> None:
        for listener in list(self.listeners):
            try:
                listener()
            except Exception:  # a broken view must not break the registry
                log.exception("Transcript listener failed")


transcript = Transcript()
