"""Pulling a window to the foreground on Windows.

Windows refuses SetForegroundWindow to processes that do not currently own the
foreground, *except* in a few sanctioned cases — one of which is a process whose
registered hotkey was just pressed. That is exactly how Sentinel is summoned, so
the call succeeds for us where it would fail for an ordinary background app.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

from PySide6.QtWidgets import QWidget

log = logging.getLogger(__name__)

# A window handle is pointer-sized. Without an explicit HWND argtype ctypes
# marshals it as a 32-bit int and raises OverflowError on 64-bit Windows.
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.SetForegroundWindow.restype = wintypes.BOOL


def force_foreground(widget: QWidget) -> None:
    """Raise, activate and focus `widget`, falling back to Qt alone on failure."""
    widget.raise_()
    widget.activateWindow()
    try:
        _user32.SetForegroundWindow(wintypes.HWND(int(widget.winId())))
    except Exception:
        log.debug("SetForegroundWindow failed", exc_info=True)
