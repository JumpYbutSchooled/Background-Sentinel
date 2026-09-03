

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

log = logging.getLogger(__name__)

ERROR_ALREADY_EXISTS = 183

# HANDLE is pointer-sized. Without an explicit restype ctypes assumes int and
# silently truncates the handle on 64-bit Windows, so CloseHandle would later
# fail (or close an unrelated handle). use_last_error is what makes
# ctypes.get_last_error() report the code from *our* call.
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL


class SingleInstance:
    def __init__(self, name: str) -> None:
        self.name = name
        self._handle: int | None = None
        self.already_running = False

        handle = _kernel32.CreateMutexW(None, False, f"Local\\{name}")
        last_error = ctypes.get_last_error()

        if not handle:
            log.warning("CreateMutexW failed (error %s); skipping instance check", last_error)
            return

        self._handle = handle
        self.already_running = last_error == ERROR_ALREADY_EXISTS

    def release(self) -> None:
        if self._handle is not None:
            _kernel32.CloseHandle(self._handle)
            self._handle = None
