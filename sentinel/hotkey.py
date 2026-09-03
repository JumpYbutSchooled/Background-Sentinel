

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

from PySide6.QtCore import QAbstractNativeEventFilter, QByteArray, QCoreApplication, QObject, Signal

log = logging.getLogger(__name__)

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312
HOTKEY_ID = 1

# Qt names the raw-message event type differently depending on which Windows
# event dispatcher is in use; both carry a MSG pointer.
_WINDOWS_MSG_TYPES = (b"windows_generic_MSG", b"windows_dispatcher_MSG")

_MODIFIERS = {
    "alt": MOD_ALT,
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "super": MOD_WIN,
    "meta": MOD_WIN,
}

_VK = {
    "space": 0x20,
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "escape": 0x1B,
    "esc": 0x1B,
    "backspace": 0x08,
    "insert": 0x2D,
    "delete": 0x2E,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "`": 0xC0,
    "-": 0xBD,
    "=": 0xBB,
    ";": 0xBA,
    "'": 0xDE,
    ",": 0xBC,
    ".": 0xBE,
    "/": 0xBF,
    "\\": 0xDC,
    "[": 0xDB,
    "]": 0xDD,
}
for _i in range(1, 25):
    _VK[f"f{_i}"] = 0x6F + _i

# use_last_error=True is required: ctypes swaps the thread's last-error value
# around every foreign call, so a plain GetLastError() afterwards reports the
# wrong code. ctypes.get_last_error() reads the value captured for our call.
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
_user32.RegisterHotKey.restype = wintypes.BOOL
_user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.UnregisterHotKey.restype = wintypes.BOOL


class HotkeyError(Exception):
    pass


def parse_hotkey(spec: str) -> tuple[int, int]:
    """'ctrl+alt+space' -> (modifier mask, virtual key code)."""
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if not parts:
        raise HotkeyError(f"Empty hotkey spec: {spec!r}")

    modifiers = 0
    key: str | None = None
    for part in parts:
        if part in _MODIFIERS:
            modifiers |= _MODIFIERS[part]
        elif key is None:
            key = part
        else:
            raise HotkeyError(f"More than one non-modifier key in {spec!r}")

    if key is None:
        raise HotkeyError(f"No main key in {spec!r}")

    if key in _VK:
        vk = _VK[key]
    elif len(key) == 1 and (key.isalpha() or key.isdigit()):
        vk = ord(key.upper())
    else:
        raise HotkeyError(f"Unknown key {key!r} in {spec!r}")

    return modifiers | MOD_NOREPEAT, vk


class GlobalHotkey(QObject, QAbstractNativeEventFilter):
    """Registers one system-wide hotkey and emits `pressed` when it fires.

    Keep a reference to the instance for as long as it is installed; if Python
    garbage-collects it while Qt still holds the filter pointer, Qt crashes.
    """

    pressed = Signal()

    def __init__(self, spec: str, parent: QObject | None = None) -> None:
        QObject.__init__(self, parent)
        QAbstractNativeEventFilter.__init__(self)
        self.spec = spec
        self._registered = False

    def register(self, app: QCoreApplication) -> None:
        modifiers, vk = parse_hotkey(self.spec)
        if not _user32.RegisterHotKey(None, HOTKEY_ID, modifiers, vk):
            error = ctypes.get_last_error()
            raise HotkeyError(
                f"Could not register {self.spec!r} (Win32 error {error}). "
                "Another application probably owns this combination."
            )
        app.installNativeEventFilter(self)
        self._registered = True
        log.info("Registered global hotkey %s", self.spec)

    def unregister(self, app: QCoreApplication) -> None:
        if not self._registered:
            return
        app.removeNativeEventFilter(self)
        _user32.UnregisterHotKey(None, HOTKEY_ID)
        self._registered = False
        log.info("Unregistered global hotkey %s", self.spec)

    def nativeEventFilter(
        self,
        eventType: QByteArray | bytes | bytearray | memoryview,
        message: int,
    ) -> object:
        raw = eventType.data() if isinstance(eventType, QByteArray) else eventType
        if bytes(raw) not in _WINDOWS_MSG_TYPES:
            return False, 0

        msg = wintypes.MSG.from_address(int(message))
        if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
            self.pressed.emit()
            return True, 0
        return False, 0
