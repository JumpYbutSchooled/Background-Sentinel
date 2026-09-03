

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, Qt, QTimer, Signal

log = logging.getLogger(__name__)

POMODORO_MINUTES = 25
BREAK_MINUTES = 5


def human(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds}s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {rest:02d}s" if rest else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


@dataclass
class Reminder:
    text: str
    due: float
    timer: QTimer = field(repr=False, default_factory=QTimer)

    @property
    def remaining(self) -> float:
        return self.due - time.time()


class Timers(QObject):
    """Every scheduled thing the daemon is holding."""

    #: title, message — the daemon routes this to the tray.
    fired = Signal(str, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._reminders: list[Reminder] = []
        self._pomodoro: Reminder | None = None
        self._on_break = False
        self.completed = 0

    # ---------------------------------------------------------- reminders

    def remind(self, seconds: float, text: str) -> Reminder:
        reminder = Reminder(text=text, due=time.time() + seconds)
        reminder.timer.setSingleShot(True)
        reminder.timer.setTimerType(Qt.TimerType.PreciseTimer)
        reminder.timer.timeout.connect(lambda: self._deliver(reminder))
        reminder.timer.start(max(1, int(seconds * 1000)))
        self._reminders.append(reminder)
        log.info("Reminder in %s: %s", human(seconds), text)
        return reminder

    def _deliver(self, reminder: Reminder) -> None:
        if reminder in self._reminders:
            self._reminders.remove(reminder)
        log.info("Reminder fired: %s", reminder.text)
        self.fired.emit("Reminder", reminder.text)

    @property
    def reminders(self) -> list[Reminder]:
        return sorted(self._reminders, key=lambda r: r.due)

    def clear_reminders(self) -> int:
        count = len(self._reminders)
        for reminder in self._reminders:
            reminder.timer.stop()
        self._reminders.clear()
        return count

    # ---------------------------------------------------------- pomodoro

    def start_pomodoro(self, minutes: int = POMODORO_MINUTES) -> None:
        self.stop_pomodoro()
        self._on_break = False
        self._pomodoro = self._schedule(minutes * 60, self._pomodoro_done)

    def _pomodoro_done(self) -> None:
        self._pomodoro = None
        if self._on_break:
            self._on_break = False
            self.fired.emit("Pomodoro", "Break over — start another block?")
            return
        self.completed += 1
        self._on_break = True
        self._pomodoro = self._schedule(BREAK_MINUTES * 60, self._pomodoro_done)
        self.fired.emit(
            "Pomodoro",
            f"Block {self.completed} done. {BREAK_MINUTES} minute break.",
        )

    def _schedule(self, seconds: float, callback) -> Reminder:
        entry = Reminder(text="pomodoro", due=time.time() + seconds)
        entry.timer.setSingleShot(True)
        entry.timer.timeout.connect(callback)
        entry.timer.start(max(1, int(seconds * 1000)))
        return entry

    def stop_pomodoro(self) -> bool:
        if self._pomodoro is None:
            return False
        self._pomodoro.timer.stop()
        self._pomodoro = None
        self._on_break = False
        return True

    @property
    def pomodoro_running(self) -> bool:
        return self._pomodoro is not None

    @property
    def pomodoro_remaining(self) -> float:
        return 0.0 if self._pomodoro is None else max(0.0, self._pomodoro.remaining)

    @property
    def pomodoro_phase(self) -> str:
        if self._pomodoro is None:
            return "idle"
        return "break" if self._on_break else "focus"

    @property
    def pomodoro_state(self) -> str:
        if self._pomodoro is None:
            return f"idle ({self.completed} done today)"
        phase = "break" if self._on_break else "focus"
        return f"{phase}, {human(self._pomodoro.remaining)} left ({self.completed} done)"

    # ------------------------------------------------------------ summary

    def status(self) -> str:
        parts = [f"pomodoro: {self.pomodoro_state}"]
        pending = self.reminders
        if pending:
            soonest = pending[0]
            parts.append(
                f"{len(pending)} reminder(s), next in {human(soonest.remaining)}"
                f" — {soonest.text}"
            )
        else:
            parts.append("no reminders")
        return "   ·   ".join(parts)


timers = Timers()
