

from __future__ import annotations

import time
from dataclasses import dataclass

from .apps import index as app_index
from .notes import TODO, notebook
from .timers import human, timers

#: Tones the renderer maps onto colours.
NORMAL = "normal"
LIVE = "live"
WARN = "warn"


@dataclass(frozen=True)
class Badge:
    label: str
    value: str
    tone: str = NORMAL


def snapshot() -> list[Badge]:
    """Everything worth showing right now, most urgent first."""
    out: list[Badge] = []

    if timers.pomodoro_running:
        out.append(
            Badge(timers.pomodoro_phase, human(timers.pomodoro_remaining), LIVE)
        )

    pending = timers.reminders
    if pending:
        soonest = pending[0]
        text = soonest.text if len(soonest.text) <= 34 else soonest.text[:33] + "…"
        label = "next" if len(pending) == 1 else f"next of {len(pending)}"
        out.append(Badge(label, f"{human(soonest.remaining)}  {text}", LIVE))

    todos = notebook.all(TODO)
    overdue = [entry for entry in todos if entry.overdue]
    if overdue:
        out.append(Badge("overdue", str(len(overdue)), WARN))

    due_soon = [
        entry
        for entry in todos
        if not entry.done
        and entry.due is not None
        and not entry.overdue
        and entry.due - time.time() < 3600
    ]
    if due_soon:
        nearest = min(due_soon, key=lambda e: e.due or 0.0)
        out.append(
            Badge("due", f"{human((nearest.due or 0.0) - time.time())}  {nearest.title}")
        )

    if app_index.scanning:
        out.append(Badge("indexing", "applications…"))

    return out


def summary() -> str:
    """The same thing as one line, for surfaces with no room for badges."""
    return "   ·   ".join(
        f"{badge.label} {badge.value}".strip() for badge in snapshot()
    )
