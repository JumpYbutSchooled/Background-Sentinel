"""Run-on-boot support via HKCU\\...\\CurrentVersion\\Run.

HKCU rather than HKLM: no admin prompt, and a per-user daemon is what we want.

Stubbed in the sense that it is wired to a tray toggle and works when running
from source, but the command it writes is not final. Once there is a packaged
.exe (Phase 9) `_launch_command` should just point at that, and the whole
pythonw/script branch below can go away.
"""

from __future__ import annotations

import logging
import sys
import winreg
from pathlib import Path

from . import APP_SLUG

log = logging.getLogger(__name__)

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = APP_SLUG


def _launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'

    interpreter = Path(sys.executable).resolve()
    # pythonw.exe launches without a console window, which is what a daemon wants.
    pythonw = interpreter.with_name("pythonw.exe")
    if pythonw.exists():
        interpreter = pythonw

    script = Path(__file__).resolve().parent.parent / "main.py"
    return f'"{interpreter}" "{script}"'


def is_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.QueryValueEx(key, _VALUE_NAME)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        log.exception("Could not read autostart registry value")
        return False


def enable() -> bool:
    command = _launch_command()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, _VALUE_NAME, 0, winreg.REG_SZ, command)
        log.info("Autostart enabled: %s", command)
        return True
    except OSError:
        log.exception("Could not enable autostart")
        return False


def disable() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, _VALUE_NAME)
        log.info("Autostart disabled")
        return True
    except FileNotFoundError:
        return True
    except OSError:
        log.exception("Could not disable autostart")
        return False


def set_enabled(enabled: bool) -> bool:
    return enable() if enabled else disable()
