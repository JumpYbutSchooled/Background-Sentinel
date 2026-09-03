

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


def _app():
    from PySide6.QtGui import QGuiApplication

    return QGuiApplication.instance()


# --------------------------------------------------------------- clipboard


def clipboard_text() -> str:
    app = _app()
    if app is None:
        return ""
    return app.clipboard().text() or ""


def set_clipboard(text: str) -> bool:
    app = _app()
    if app is None:
        return False
    app.clipboard().setText(text)
    return True


# ------------------------------------------------------------------ screens


@dataclass(frozen=True)
class Screen:
    name: str
    width: int
    height: int
    refresh: float
    scale: float
    primary: bool


def screens() -> list[Screen]:
    from PySide6.QtGui import QGuiApplication

    app = _app()
    if app is None:
        return []
    primary = QGuiApplication.primaryScreen()
    out = []
    for screen in QGuiApplication.screens():
        size = screen.geometry()
        out.append(
            Screen(
                screen.name() or "display",
                size.width(), size.height(),
                round(screen.refreshRate(), 1),
                round(screen.devicePixelRatio(), 2),
                screen is primary,
            )
        )
    return out


# --------------------------------------------------------------- screenshot


def pictures_dir() -> Path:
    """Where a grab goes. The user's Pictures folder, or home if it is gone."""
    candidate = Path.home() / "Pictures"
    return candidate if candidate.is_dir() else Path.home()


def screenshot(target: Path | None = None) -> Path | None:
    """Grab every screen into one PNG. Returns where it landed.

    The whole virtual desktop rather than one monitor: a screenshot command
    that silently drops half a dual-monitor setup is worse than none.
    """
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QGuiApplication, QPainter, QPixmap

    app = _app()
    if app is None:
        return None
    displays = QGuiApplication.screens()
    if not displays:
        return None

    span = QRect()
    for screen in displays:
        span = span.united(screen.geometry())
    if span.isEmpty():
        return None

    canvas = QPixmap(span.size())
    canvas.fill()
    painter = QPainter(canvas)
    try:
        for screen in displays:
            frame = screen.grabWindow(0)
            spot = screen.geometry().topLeft() - span.topLeft()
            painter.drawPixmap(spot, frame)
    finally:
        painter.end()

    path = target or (
        pictures_dir() / f"sentinel-{time.strftime('%Y%m%d-%H%M%S')}.png"
    )
    if not canvas.save(str(path), "PNG"):
        log.error("Could not write the screenshot to %s", path)
        return None
    log.info("Saved a screenshot to %s", path)
    return path


# ------------------------------------------------------------ known folders

#: The folders worth reaching for by name. Resolved lazily, because a machine
#: with OneDrive redirection has some of these somewhere else entirely.
FOLDERS: dict[str, str] = {
    "home": "",
    "desktop": "Desktop",
    "downloads": "Downloads",
    "documents": "Documents",
    "pictures": "Pictures",
    "music": "Music",
    "videos": "Videos",
}


def folder(name: str) -> Path | None:
    """A known folder by name, or None if this machine has not got it."""
    key = name.strip().lower()
    if key in ("appdata", "roaming"):
        raw = os.environ.get("APPDATA")
        return Path(raw) if raw else None
    if key == "temp":
        raw = os.environ.get("TEMP") or os.environ.get("TMP")
        return Path(raw) if raw else None
    if key not in FOLDERS:
        return None
    path = Path.home() / FOLDERS[key] if FOLDERS[key] else Path.home()
    return path if path.is_dir() else None


def reveal(path: Path) -> bool:
    """Open a folder — or select a file — in Explorer."""
    try:
        if path.is_dir():
            os.startfile(path)  # noqa: S606 - Windows shell open, by design
        else:
            import subprocess

            subprocess.Popen(
                ["explorer", "/select,", str(path)],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        return True
    except OSError:
        log.exception("Could not reveal %s", path)
        return False
