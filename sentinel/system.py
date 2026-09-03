

from __future__ import annotations

import ctypes
import logging
import subprocess
from ctypes import wintypes
from typing import Any

log = logging.getLogger(__name__)

_user32 = ctypes.WinDLL("user32", use_last_error=True)

# Virtual keys for the media row.
VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
VK_MEDIA_NEXT = 0xB0
VK_MEDIA_PREV = 0xB1
VK_MEDIA_STOP = 0xB2
VK_MEDIA_PLAY_PAUSE = 0xB3

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002

#: One press of volume up/down moves the system volume by this much.
VOLUME_STEP_PERCENT = 2


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    # All three members must be present: the union's size is part of the ABI,
    # and SendInput rejects a struct of the wrong size.
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


_user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
_user32.SendInput.restype = wintypes.UINT
_user32.LockWorkStation.argtypes = []
_user32.LockWorkStation.restype = wintypes.BOOL


def tap(vk: int, times: int = 1) -> bool:
    """Press and release a virtual key `times` times."""
    events = []
    for _ in range(max(1, times)):
        for flags in (0, KEYEVENTF_KEYUP):
            item = _INPUT()
            item.type = INPUT_KEYBOARD
            item.ki = _KEYBDINPUT(vk, 0, flags, 0, None)
            events.append(item)
    array = (_INPUT * len(events))(*events)
    sent = _user32.SendInput(len(events), array, ctypes.sizeof(_INPUT))
    if sent != len(events):
        log.warning("SendInput sent %d of %d events (error %d)",
                    sent, len(events), ctypes.get_last_error())
        return False
    return True


def lock() -> bool:
    if not _user32.LockWorkStation():
        log.error("LockWorkStation failed (error %d)", ctypes.get_last_error())
        return False
    return True


# -- suspending ---------------------------------------------------------------
#
# `SetSuspendState` is the obvious way to sleep and the wrong one on a modern
# laptop. It is the legacy entry point: it asks for an S1-S3 suspend, and a
# machine with S0 low-power idle — Modern Standby, which is most laptops now —
# implements none of those. `powercfg /a` on such a machine lists S1, S2 and S3
# as unavailable. What the platform does with a request it cannot honour is its
# own business, and none of the options are what was asked for: hibernate if
# hibernation is enabled, or a failed transition that cuts the power, losing
# the session and logging Kernel-Power 41 with SleepInProgress set — which
# reads, to whoever typed `sleep now`, as the laptop shutting itself down.
#
# `NtInitiatePowerAction` is what the Start menu's own Sleep item calls. It
# names the action and lets the power manager pick the state the platform
# actually implements: S0 idle where that is all there is, S3 on hardware that
# still has it. MinSystemState is a floor, not a target, so it has to stay at
# Sleeping1 — asking for Sleeping3 would demand a legacy state again and bring
# the whole problem back.

_POWER_ACTION_SLEEP = 2
_POWER_ACTION_HIBERNATE = 3
_POWER_SYSTEM_SLEEPING1 = 2
_POWER_SYSTEM_HIBERNATE = 5

# As the shell passes them: apps get asked first and may put UI up meanwhile.
_POWER_ACTION_QUERY_ALLOWED = 0x00000001
_POWER_ACTION_UI_ALLOWED = 0x00000004

_STATUS_SUCCESS = 0
_TOKEN_QUERY = 0x0008
_TOKEN_ADJUST_PRIVILEGES = 0x0020
_SE_PRIVILEGE_ENABLED = 0x00000002
_ERROR_NOT_ALL_ASSIGNED = 1300


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", _LUID), ("Attributes", wintypes.DWORD)]


class _TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [
        ("PrivilegeCount", wintypes.DWORD),
        ("Privileges", _LUID_AND_ATTRIBUTES * 1),
    ]


def _enable_shutdown_privilege() -> bool:
    """Turn on SeShutdownPrivilege for this process.

    Nothing is being elevated: every interactive user already holds this
    privilege, it just starts out disabled on the token and the power APIs
    refuse without it. Enabling it is idempotent, so this runs per call rather
    than being cached — the cost is a few microseconds and it cannot go stale.
    """
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.LookupPrivilegeValueW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(_LUID)
    ]
    advapi32.LookupPrivilegeValueW.restype = wintypes.BOOL
    advapi32.AdjustTokenPrivileges.argtypes = [
        wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(_TOKEN_PRIVILEGES),
        wintypes.DWORD, ctypes.POINTER(_TOKEN_PRIVILEGES),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.AdjustTokenPrivileges.restype = wintypes.BOOL

    luid = _LUID()
    if not advapi32.LookupPrivilegeValueW(None, "SeShutdownPrivilege",
                                          ctypes.byref(luid)):
        log.error("LookupPrivilegeValue failed (error %d)", ctypes.get_last_error())
        return False

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(),
        _TOKEN_ADJUST_PRIVILEGES | _TOKEN_QUERY,
        ctypes.byref(token),
    ):
        log.error("OpenProcessToken failed (error %d)", ctypes.get_last_error())
        return False

    try:
        privileges = _TOKEN_PRIVILEGES(
            1, (_LUID_AND_ATTRIBUTES * 1)(
                _LUID_AND_ATTRIBUTES(luid, _SE_PRIVILEGE_ENABLED)
            )
        )
        ok = advapi32.AdjustTokenPrivileges(
            token, False, ctypes.byref(privileges), 0, None, None
        )
        # It reports success for a partial job too, so the error code is the
        # only way to tell that the privilege was not actually granted.
        error = ctypes.get_last_error()
        if not ok or error == _ERROR_NOT_ALL_ASSIGNED:
            log.error("AdjustTokenPrivileges failed (error %d)", error)
            return False
        return True
    finally:
        kernel32.CloseHandle(token)


def _initiate_power_action(action: int, min_state: int, what: str) -> bool:
    """Ask the power manager for a transition, the way the shell asks."""
    if not _enable_shutdown_privilege():
        return False
    try:
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        ntdll.NtInitiatePowerAction.argtypes = [
            ctypes.c_int, ctypes.c_int, wintypes.ULONG, wintypes.BOOLEAN
        ]
        ntdll.NtInitiatePowerAction.restype = wintypes.LONG
        status = ntdll.NtInitiatePowerAction(
            action, min_state,
            _POWER_ACTION_QUERY_ALLOWED | _POWER_ACTION_UI_ALLOWED,
            False,  # synchronous: this returns on the far side, after resume
        )
    except OSError:
        log.exception("NtInitiatePowerAction(%s) failed", what)
        return False
    if status != _STATUS_SUCCESS:
        log.error("NtInitiatePowerAction(%s) returned 0x%08X",
                  what, status & 0xFFFFFFFF)
        return False
    return True


def sleep() -> bool:
    """Suspend to the lightest sleep state this machine really has.

    Deliberately does not fall back to `SetSuspendState` when this fails:
    that call is what powers the machine off on a Modern Standby laptop, so
    reporting the failure is the better of the two outcomes.
    """
    return _initiate_power_action(
        _POWER_ACTION_SLEEP, _POWER_SYSTEM_SLEEPING1, "sleep"
    )


def hibernate() -> bool:
    """Suspend to disk. Reports failure if hibernation is turned off."""
    return _initiate_power_action(
        _POWER_ACTION_HIBERNATE, _POWER_SYSTEM_HIBERNATE, "hibernate"
    )


def restart() -> bool:
    return _shutdown_cmd(["/r", "/t", "0"])


def shutdown() -> bool:
    return _shutdown_cmd(["/s", "/t", "0"])


def sign_out() -> bool:
    """End the session, leaving the machine running for the next person."""
    return _shutdown_cmd(["/l"])


def _shutdown_cmd(args: list[str]) -> bool:
    try:
        subprocess.run(
            ["shutdown", *args], check=True, capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return True
    except (OSError, subprocess.CalledProcessError):
        log.exception("shutdown %s failed", " ".join(args))
        return False


# ------------------------------------------------------------- what it is on
# Straight readouts of the machine's own counters. All of these are one call
# each, so the commands that show them stay instant.


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class _SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", wintypes.BYTE),
        ("BatteryFlag", wintypes.BYTE),
        ("BatteryLifePercent", wintypes.BYTE),
        ("SystemStatusFlag", wintypes.BYTE),
        ("BatteryLifeTime", wintypes.DWORD),
        ("BatteryFullLifeTime", wintypes.DWORD),
    ]


def uptime() -> float:
    """Seconds since the machine last booted."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetTickCount64.restype = ctypes.c_ulonglong
    return kernel32.GetTickCount64() / 1000.0


def memory() -> tuple[int, int, int] | None:
    """`(used, total, percent)` bytes of physical RAM."""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        status = _MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        total = int(status.ullTotalPhys)
        return total - int(status.ullAvailPhys), total, int(status.dwMemoryLoad)
    except Exception:
        log.exception("GlobalMemoryStatusEx failed")
        return None


def disk(path: str = "C:\\") -> tuple[int, int] | None:
    """`(free, total)` bytes on the volume holding `path`."""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        free, total = ctypes.c_ulonglong(), ctypes.c_ulonglong()
        ok = kernel32.GetDiskFreeSpaceExW(
            ctypes.c_wchar_p(path), None, ctypes.byref(total), ctypes.byref(free)
        )
        return (int(free.value), int(total.value)) if ok else None
    except Exception:
        log.exception("GetDiskFreeSpaceExW failed for %s", path)
        return None


def battery() -> tuple[int, bool, int] | None:
    """`(percent, on_mains, seconds_left)`. None on a desktop with no battery.

    `seconds_left` is -1 when Windows will not estimate it, which it never does
    while charging.
    """
    try:
        # In kernel32, despite every other power call living in user32.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        status = _SYSTEM_POWER_STATUS()
        if not kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return None
        percent = status.BatteryLifePercent & 0xFF
        if percent == 255:  # documented "unknown", i.e. no battery
            return None
        remaining = int(status.BatteryLifeTime)
        return (
            percent,
            (status.ACLineStatus & 0xFF) == 1,
            -1 if remaining == 0xFFFFFFFF else remaining,
        )
    except Exception:
        log.exception("GetSystemPowerStatus failed")
        return None


def processes(limit: int = 12) -> list[tuple[str, int]]:
    """`(name, kilobytes)` for the heaviest processes, biggest first.

    Read from `tasklist` rather than psutil: it ships with Windows, so this
    works whether or not the optional mixer dependency was installed.
    """
    try:
        out = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh"],
            check=True, capture_output=True, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        log.exception("tasklist failed")
        return []

    import csv
    import io

    totals: dict[str, int] = {}
    for row in csv.reader(io.StringIO(out)):
        if len(row) < 5:
            continue
        name = row[0].removesuffix(".exe")
        digits = "".join(ch for ch in row[4] if ch.isdigit())
        if not digits:
            continue
        # One name, many processes: a browser's dozen tabs are one entry.
        totals[name] = totals.get(name, 0) + int(digits)
    ranked = sorted(totals.items(), key=lambda item: -item[1])
    return ranked[:limit]


def kill(name: str) -> tuple[bool, str]:
    """End every process with this image name. `(ok, what happened)`."""
    image = name if name.lower().endswith(".exe") else f"{name}.exe"
    try:
        done = subprocess.run(
            ["taskkill", "/f", "/im", image],
            capture_output=True, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        log.exception("taskkill failed for %s", image)
        return False, str(exc)
    if done.returncode == 0:
        return True, f"ended {image}"
    message = (done.stderr or done.stdout or "").strip().splitlines()
    return False, message[-1] if message else f"could not end {image}"


# ---------------------------------------------------------------- the mixer


def _mixer():
    """pycaw's AudioUtilities, or None when it is not installed."""
    try:
        from pycaw.utils import AudioUtilities  # pycaw >= 2023

        return AudioUtilities
    except Exception:
        pass
    try:
        from pycaw.pycaw import AudioUtilities  # older layout

        return AudioUtilities
    except Exception:
        return None


def mixer_available() -> bool:
    return _mixer() is not None


def _endpoint_volume() -> Any:
    """IAudioEndpointVolume for the default output device.

    Recent pycaw hands back an AudioDevice that exposes EndpointVolume
    directly; older releases return a raw IMMDevice that has to be Activated.
    Support both, so an upgrade or downgrade of the dependency cannot silently
    break volume control.

    Typed as Any deliberately: these are COM interfaces resolved at runtime,
    and no static description of them is available to check against.
    """
    utils = _mixer()
    if utils is None:
        return None
    speakers: Any = utils.GetSpeakers()
    endpoint = getattr(speakers, "EndpointVolume", None)
    if endpoint is not None:
        return endpoint

    from ctypes import POINTER, cast

    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import IAudioEndpointVolume

    interface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(interface, POINTER(IAudioEndpointVolume))


def master_volume() -> int | None:
    """Current master output as a percentage, or None without pycaw."""
    try:
        volume = _endpoint_volume()
        if volume is None:
            return None
        return int(round(volume.GetMasterVolumeLevelScalar() * 100))
    except Exception:
        log.exception("Could not read the master volume")
        return None


def set_master_volume(percent: int) -> bool:
    try:
        volume = _endpoint_volume()
        if volume is None:
            return False
        volume.SetMasterVolumeLevelScalar(max(0, min(100, percent)) / 100.0, None)
        return True
    except Exception:
        log.exception("Could not set the master volume")
        return False


def sessions() -> list[str]:
    """Names of the applications currently holding an audio session."""
    utils = _mixer()
    if utils is None:
        return []
    try:
        names = []
        for session in utils.GetAllSessions():
            if session.Process and session.Process.name():
                names.append(session.Process.name().removesuffix(".exe"))
        return sorted(set(names))
    except Exception:
        log.exception("Could not enumerate audio sessions")
        return []


def mute_app(name: str, state: bool | None = None) -> bool | None:
    """Mute, unmute, or toggle one application's mixer session.

    Returns the resulting mute state, or None if pycaw is missing or no
    session matched.
    """
    utils = _mixer()
    if utils is None:
        return None
    needle = name.lower().removesuffix(".exe")
    try:
        for session in utils.GetAllSessions():
            if not session.Process:
                continue
            process = session.Process.name().lower().removesuffix(".exe")
            if needle not in process:
                continue
            volume = session.SimpleAudioVolume
            wanted = (not volume.GetMute()) if state is None else state
            volume.SetMute(bool(wanted), None)
            log.info("%s %s", "Muted" if wanted else "Unmuted", session.Process.name())
            return bool(wanted)
    except Exception:
        log.exception("Could not mute %s", name)
    return None
