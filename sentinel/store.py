"""Where Sentinel keeps its data, and how it reads and writes JSON.

Every write goes to a temp file first and is then replaced into place. A daemon
that runs all day will eventually be killed mid-write, and a half-written
settings or notes file that fails to parse would lose the lot.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from . import APP_SLUG

log = logging.getLogger(__name__)


def data_dir() -> Path:
    """Per-user writable directory for logs, config, plugin data."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    path = Path(base) / APP_SLUG
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(name: str, fallback: Any) -> Any:
    path = data_dir() / name
    if not path.exists():
        return fallback
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        log.exception("Could not read %s; falling back to defaults", name)
        return fallback


def write_json(name: str, payload: Any) -> bool:
    path = data_dir() / name
    temp = path.with_suffix(path.suffix + ".tmp")
    try:
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        os.replace(temp, path)  # atomic on Windows and POSIX alike
        return True
    except OSError:
        log.exception("Could not write %s", name)
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
