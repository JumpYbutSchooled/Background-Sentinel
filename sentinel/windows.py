

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from dataclasses import dataclass

log = logging.getLogger(__name__)

_user32 = ctypes.WinDLL("user32", use_last_error=True)

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
GW_OWNER = 4

SW_MINIMIZE = 6
SW_MAXIMIZE = 3
SW_RESTORE = 9
SW_SHOWMINIMIZED = 2

#: DwmGetWindowAttribute's cloak flag. Non-zero means the window is hidden by
#: the shell even though it reports itself visible.
DWMWA_CLOAKED = 14

SPI_GETWORKAREA = 0x0030

_ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

_user32.EnumWindows.argtypes = [_ENUM_PROC, wintypes.LPARAM]
_user32.EnumWindows.restype = wintypes.BOOL
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsWindowVisible.restype = wintypes.BOOL
_user32.IsIconic.argtypes = [wintypes.HWND]
_user32.IsIconic.restype = wintypes.BOOL
_user32.IsZoomed.argtypes = [wintypes.HWND]
_user32.IsZoomed.restype = wintypes.BOOL
_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.GetWindowTextLengthW.restype = ctypes.c_int
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int
_user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetWindow.restype = wintypes.HWND
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.SetForegroundWindow.restype = wintypes.BOOL
_user32.GetForegroundWindow.argtypes = []
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.ShowWindow.restype = wintypes.BOOL
_user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
_user32.SetWindowPos.restype = wintypes.BOOL
_user32.PostMessageW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
]
_user32.PostMessageW.restype = wintypes.BOOL
_user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
_user32.MonitorFromWindow.restype = wintypes.HANDLE

# GetWindowLongPtrW only exists in the 64-bit user32; the 32-bit build has the
# non-Ptr name and the same semantics for GWL_EXSTYLE.
_get_long = getattr(_user32, "GetWindowLongPtrW", _user32.GetWindowLongW)
_get_long.argtypes = [wintypes.HWND, ctypes.c_int]
_get_long.restype = ctypes.c_ssize_t

WM_CLOSE = 0x0010
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
MONITOR_DEFAULTTONEAREST = 2


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


@dataclass(frozen=True)
class Window:
    handle: int
    title: str
    minimised: bool = False
    maximised: bool = False

    @property
    def state(self) -> str:
        if self.minimised:
            return "minimised"
        return "maximised" if self.maximised else "open"


def _cloaked(hwnd: int) -> bool:
    """Whether the shell is hiding this window. False if DWM cannot say."""
    try:
        dwm = ctypes.WinDLL("dwmapi")
        value = ctypes.c_int(0)
        result = dwm.DwmGetWindowAttribute(
            wintypes.HWND(hwnd), ctypes.c_uint(DWMWA_CLOAKED),
            ctypes.byref(value), ctypes.sizeof(value),
        )
        return result == 0 and value.value != 0
    except Exception:
        return False


def _title(hwnd: int) -> str:
    length = _user32.GetWindowTextLengthW(wintypes.HWND(hwnd))
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(wintypes.HWND(hwnd), buffer, length + 1)
    return buffer.value


def _is_task_window(hwnd: int) -> bool:
    handle = wintypes.HWND(hwnd)
    if not _user32.IsWindowVisible(handle):
        return False
    styles = _get_long(handle, GWL_EXSTYLE)
    if styles & WS_EX_TOOLWINDOW:
        return False
    # An owned window is a dialog belonging to something else, unless it has
    # explicitly asked to appear as an application in its own right.
    if _user32.GetWindow(handle, GW_OWNER) and not styles & WS_EX_APPWINDOW:
        return False
    return not _cloaked(hwnd)


def listing() -> list[Window]:
    """Every window a person would call a window, in z-order."""
    found: list[Window] = []

    def visit(hwnd, _param):
        try:
            if _is_task_window(hwnd):
                title = _title(hwnd)
                if title:
                    found.append(
                        Window(
                            int(hwnd), title,
                            bool(_user32.IsIconic(wintypes.HWND(hwnd))),
                            bool(_user32.IsZoomed(wintypes.HWND(hwnd))),
                        )
                    )
        except Exception:  # one bad window must not end the enumeration
            log.debug("Skipped a window during enumeration", exc_info=True)
        return True

    try:
        _user32.EnumWindows(_ENUM_PROC(visit), 0)
    except Exception:
        log.exception("EnumWindows failed")
    return found


def find(query: str) -> Window | None:
    """The best window matching `query`, by title.

    Prefix beats substring, so `focus fire` lands on Firefox rather than on
    whichever other window happens to mention it further along its title.
    """
    needle = query.strip().lower()
    if not needle:
        return None
    windows = listing()
    for test in (
        lambda t: t == needle,
        lambda t: t.startswith(needle),
        lambda t: needle in t,
    ):
        for window in windows:
            if test(window.title.lower()):
                return window
    return None


def active() -> Window | None:
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return None
    handle = int(hwnd)
    return Window(
        handle, _title(handle),
        bool(_user32.IsIconic(wintypes.HWND(handle))),
        bool(_user32.IsZoomed(wintypes.HWND(handle))),
    )


def focus(window: Window) -> bool:
    handle = wintypes.HWND(window.handle)
    if _user32.IsIconic(handle):
        _user32.ShowWindow(handle, SW_RESTORE)
    return bool(_user32.SetForegroundWindow(handle))


def show(window: Window, command: int) -> bool:
    return bool(_user32.ShowWindow(wintypes.HWND(window.handle), command))


def close(window: Window) -> bool:
    """Ask a window to close, the same way its own close button does.

    Posted rather than sent: an application that puts up a "save changes?"
    prompt would otherwise block us inside its message handler.
    """
    return bool(_user32.PostMessageW(wintypes.HWND(window.handle), WM_CLOSE, 0, 0))


def work_area(window: Window) -> tuple[int, int, int, int] | None:
    """The usable rectangle of the monitor `window` is on, taskbar excluded."""
    try:
        monitor = _user32.MonitorFromWindow(
            wintypes.HWND(window.handle), MONITOR_DEFAULTTONEAREST
        )
        if not monitor:
            return None
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        _user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MONITORINFO)]
        if not _user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        rect = info.rcWork
        return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
    except Exception:
        log.exception("Could not read the work area")
        return None


#: Fractions of the work area each snap target occupies: x, y, width, height.
SNAP_ZONES: dict[str, tuple[float, float, float, float]] = {
    "left": (0.0, 0.0, 0.5, 1.0),
    "right": (0.5, 0.0, 0.5, 1.0),
    "top": (0.0, 0.0, 1.0, 0.5),
    "bottom": (0.0, 0.5, 1.0, 0.5),
    "full": (0.0, 0.0, 1.0, 1.0),
    "centre": (0.15, 0.1, 0.7, 0.8),
}


def snap(window: Window, zone: str) -> bool:
    """Move a window into one of the fixed zones on its own monitor."""
    fractions = SNAP_ZONES.get(zone)
    area = work_area(window)
    if fractions is None or area is None:
        return False
    left, top, width, height = area
    fx, fy, fw, fh = fractions
    handle = wintypes.HWND(window.handle)
    # A maximised window ignores SetWindowPos until it is restored.
    if _user32.IsZoomed(handle) or _user32.IsIconic(handle):
        _user32.ShowWindow(handle, SW_RESTORE)
    return bool(
        _user32.SetWindowPos(
            handle, None,
            int(left + width * fx), int(top + height * fy),
            int(width * fw), int(height * fh),
            SWP_NOZORDER | SWP_NOACTIVATE,
        )
    )


def minimise_all() -> bool:
    """Clear the desktop, by minimising every task window we can see."""
    done = 0
    for window in listing():
        if not window.minimised and show(window, SW_MINIMIZE):
            done += 1
    return done > 0
