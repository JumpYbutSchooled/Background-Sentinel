"""Todos and notes, held by the resident daemon and flushed to disk.

Sentinel is already running all day, which is the whole reason quick capture
belongs here rather than in a separate app: there is no launch cost and the
list survives between summons.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

from .store import read_json, write_json

log = logging.getLogger(__name__)

NOTES_FILE = "notes.json"

TODO = "todo"
NOTE = "note"

CATEGORIES = ("general", "work", "home", "build", "later")


@dataclass
class Entry:
    title: str
    kind: str = TODO
    body: str = ""
    category: str = "general"
    created: float = field(default_factory=time.time)
    due: float | None = None
    done: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    # ------------------------------------------------------------- rendering

    @property
    def created_label(self) -> str:
        return _friendly(datetime.fromtimestamp(self.created))

    @property
    def due_label(self) -> str:
        if self.due is None:
            return ""
        return _friendly(datetime.fromtimestamp(self.due))

    @property
    def overdue(self) -> bool:
        return self.due is not None and not self.done and self.due < time.time()


def _friendly(when: datetime) -> str:
    """'today 14:05', 'tomorrow 09:00', 'Fri 3 Oct', '3 Oct 2024'."""
    today = date.today()
    delta = (when.date() - today).days
    clock = when.strftime("%H:%M")
    if delta == 0:
        return f"today {clock}"
    if delta == 1:
        return f"tomorrow {clock}"
    if delta == -1:
        return f"yesterday {clock}"
    if 0 < delta < 7:
        return when.strftime(f"%a {when.day} %b")
    if when.year == today.year:
        return when.strftime(f"{when.day} %b")
    return when.strftime(f"{when.day} %b %Y")


_RELATIVE = re.compile(r"^\s*(?:in\s+)?(\d+)\s*([mhdw])\w*\s*$", re.I)
_CLOCK = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*$")
_DAY_MONTH = re.compile(r"^\s*(\d{1,2})\s*[/-]\s*(\d{1,2})\s*$")


def parse_due(text: str) -> float | None:
    """Read a due date the way a person would type one.

    Accepts '20m', 'in 2h', '3d', 'tomorrow', '17:30', '25/12'. Returns None
    when nothing sensible is there, so a typo becomes "no due date" rather than
    a wrong one.
    """
    raw = text.strip().lower()
    if not raw:
        return None
    now = datetime.now()

    if raw in ("today", "tonight"):
        return now.replace(hour=18, minute=0, second=0, microsecond=0).timestamp()
    if raw == "tomorrow":
        target = now + timedelta(days=1)
        return target.replace(hour=9, minute=0, second=0, microsecond=0).timestamp()

    match = _RELATIVE.match(raw)
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        span = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}[unit]
        return (now + timedelta(**{span: amount})).timestamp()

    match = _CLOCK.match(raw)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if 0 <= hour < 24 and 0 <= minute < 60:
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target < now:  # a time already past means tomorrow
                target += timedelta(days=1)
            return target.timestamp()

    match = _DAY_MONTH.match(raw)
    if match:
        day, month = int(match.group(1)), int(match.group(2))
        try:
            target = now.replace(
                month=month, day=day, hour=9, minute=0, second=0, microsecond=0
            )
        except ValueError:
            return None
        if target < now:
            try:
                target = target.replace(year=target.year + 1)
            except ValueError:
                return None
        return target.timestamp()

    return None


class Notebook:
    """All entries, newest first, persisted on every change."""

    def __init__(self) -> None:
        self._entries: list[Entry] = []
        self._load()

    def _load(self) -> None:
        rows = read_json(NOTES_FILE, [])
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict) or "title" not in row:
                continue
            try:
                self._entries.append(
                    Entry(
                        title=str(row["title"]),
                        kind=str(row.get("kind", TODO)),
                        body=str(row.get("body", "")),
                        category=str(row.get("category", "general")),
                        created=float(row.get("created", time.time())),
                        due=None if row.get("due") is None else float(row["due"]),
                        done=bool(row.get("done", False)),
                        id=str(row.get("id", uuid.uuid4().hex[:12])),
                    )
                )
            except (TypeError, ValueError):
                log.warning("Skipping malformed note row: %r", row)
        self._sort()

    def _sort(self) -> None:
        # Open items first, then soonest due, then newest.
        self._entries.sort(
            key=lambda e: (e.done, e.due if e.due is not None else 1e18, -e.created)
        )

    def save(self) -> None:
        write_json(NOTES_FILE, [asdict(entry) for entry in self._entries])

    # ------------------------------------------------------------------- api

    def all(self, kind: str | None = None) -> list[Entry]:
        if kind is None:
            return list(self._entries)
        return [entry for entry in self._entries if entry.kind == kind]

    def add(self, entry: Entry) -> Entry:
        self._entries.append(entry)
        self._sort()
        self.save()
        log.info("Captured %s: %s", entry.kind, entry.title)
        return entry

    def toggle(self, entry: Entry) -> None:
        entry.done = not entry.done
        self._sort()
        self.save()

    def remove(self, entry: Entry) -> None:
        self._entries = [e for e in self._entries if e.id != entry.id]
        self.save()

    def counts(self) -> tuple[int, int]:
        todos = self.all(TODO)
        return sum(1 for t in todos if not t.done), len(todos)


notebook = Notebook()
