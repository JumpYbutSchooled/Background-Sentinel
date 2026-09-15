"""The machine's audio endpoints: what they are, and which one Windows uses.

pycaw is not enough here. It reaches the mixer — one application's session on
whatever device is already default — and has nothing to say about the devices
themselves. So this talks to the core audio API directly through ctypes, which
also means the listing works on a machine that never installed the optional
dependency, exactly like the rest of `system`.

One caveat, stated plainly because it is the one thing in this file that could
stop working: Windows has never published a way to *change* the default
device. The Sound control panel does it through IPolicyConfig, an interface
Microsoft documents nowhere and every tool of this kind therefore uses. Both
its known interface identities are tried, and `set_default` reports failure
rather than guessing if neither answers.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import POINTER, c_float, c_uint, c_void_p, c_wchar_p, pointer
from ctypes import wintypes
from dataclasses import dataclass

log = logging.getLogger(__name__)

_ole32 = ctypes.WinDLL("ole32", use_last_error=True)

_S_OK = 0
_S_FALSE = 1
_RPC_E_CHANGED_MODE = 0x80010106 - 0x100000000  # as a signed long
_COINIT_APARTMENTTHREADED = 0x2
_CLSCTX_ALL = 23
_STGM_READ = 0
_VT_LPWSTR = 31

#: EDataFlow and ERole, from mmdeviceapi.h.
RENDER, CAPTURE = 0, 1
_ROLES = (0, 1, 2)  # console, multimedia, communications
_DEVICE_STATE_ACTIVE = 0x1


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", wintypes.DWORD)]


class _PROPVARIANT(ctypes.Structure):
    """Only ever read for a string here, so the union is two opaque words.

    That is its real size on both architectures: eight bytes of header and a
    pointer-pair union. Nothing is interpreted without checking `vt` first.
    """

    _fields_ = [
        ("vt", wintypes.WORD),
        ("reserved1", wintypes.WORD),
        ("reserved2", wintypes.WORD),
        ("reserved3", wintypes.WORD),
        ("data", c_void_p),
        ("tail", c_void_p),
    ]


_ole32.CLSIDFromString.argtypes = [wintypes.LPCWSTR, POINTER(_GUID)]
_ole32.CLSIDFromString.restype = ctypes.c_long
_ole32.CoInitializeEx.argtypes = [c_void_p, wintypes.DWORD]
_ole32.CoInitializeEx.restype = ctypes.c_long
_ole32.CoCreateInstance.argtypes = [
    POINTER(_GUID), c_void_p, wintypes.DWORD, POINTER(_GUID), POINTER(c_void_p)
]
_ole32.CoCreateInstance.restype = ctypes.c_long
_ole32.PropVariantClear.argtypes = [POINTER(_PROPVARIANT)]
_ole32.PropVariantClear.restype = ctypes.c_long
_ole32.CoTaskMemFree.argtypes = [c_void_p]
_ole32.CoTaskMemFree.restype = None


def _guid(text: str) -> _GUID:
    value = _GUID()
    if _ole32.CLSIDFromString(text, pointer(value)) != _S_OK:
        raise ValueError(f"not a GUID: {text}")
    return value


CLSID_MMDeviceEnumerator = _guid("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
IID_IMMDeviceEnumerator = _guid("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
IID_IAudioEndpointVolume = _guid("{5CDF2C82-841E-4546-9722-0CF74078229A}")
CLSID_PolicyConfigClient = _guid("{870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}")
#: Win7 and later, then the Vista layout. The methods before SetDefaultEndpoint
#: differ between them, which is why the index differs too.
POLICY_CONFIG = (
    (_guid("{F8679F50-850A-41CF-9C72-430F290290C8}"), 13),
    (_guid("{568B9108-44BF-40B4-9006-86AFE5B5A620}"), 12),
)

PKEY_Device_FriendlyName = _PROPERTYKEY(
    _guid("{A45C254E-DF1C-4EFD-8020-67D146A850E0}"), 14
)


@dataclass(frozen=True)
class Device:
    """One endpoint, as the Sound control panel would show it."""

    id: str
    name: str
    default: bool
    #: None when the device would not report a level.
    level: int | None = None
    muted: bool | None = None


# ------------------------------------------------------------------ plumbing


def _call(interface: c_void_p, index: int, *args):
    """Invoke the interface's `index`-th method.

    Every COM object here is a pointer to a vtable pointer, so the method is
    read out of that table and called with the interface itself as the first
    argument. HRESULT as the return type is what makes a failed call raise
    rather than quietly return a number nobody checked.
    """
    vtable = ctypes.cast(interface, POINTER(POINTER(c_void_p)))[0]
    prototype = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, *(type(a) for a in args))
    return prototype(vtable[index])(interface, *args)


def _release(interface: c_void_p | None) -> None:
    if interface:
        try:
            _call(interface, 2)  # IUnknown::Release
        except OSError:
            log.debug("Release failed", exc_info=True)


class _Apartment:
    """COM initialised for the length of a call, and only if it wasn't already.

    The daemon's GUI thread is normally already in an apartment because Qt put
    it in one, and un-initialising somebody else's would be the kind of bug
    that shows up somewhere else entirely.
    """

    def __enter__(self) -> "_Apartment":
        status = _ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
        self._ours = status in (_S_OK, _S_FALSE)
        return self

    def __exit__(self, *_exc) -> None:
        if self._ours:
            _ole32.CoUninitialize()


def _enumerator() -> c_void_p:
    interface = c_void_p()
    status = _ole32.CoCreateInstance(
        pointer(CLSID_MMDeviceEnumerator), None, _CLSCTX_ALL,
        pointer(IID_IMMDeviceEnumerator), pointer(interface),
    )
    if status != _S_OK or not interface:
        raise OSError(f"MMDeviceEnumerator unavailable (0x{status & 0xFFFFFFFF:08X})")
    return interface


def _device_id(device: c_void_p) -> str:
    """IMMDevice::GetId. The string is COM-allocated, so it is freed here."""
    raw = c_wchar_p()
    _call(device, 5, pointer(raw))
    try:
        return raw.value or ""
    finally:
        _ole32.CoTaskMemFree(ctypes.cast(raw, c_void_p))


def _device_name(device: c_void_p) -> str:
    """The friendly name out of the device's property store."""
    store = c_void_p()
    _call(device, 4, wintypes.DWORD(_STGM_READ), pointer(store))  # OpenPropertyStore
    try:
        value = _PROPVARIANT()
        _call(store, 5, pointer(PKEY_Device_FriendlyName), pointer(value))  # GetValue
        try:
            if value.vt != _VT_LPWSTR or not value.data:
                return ""
            return ctypes.cast(value.data, c_wchar_p).value or ""
        finally:
            _ole32.PropVariantClear(pointer(value))
    finally:
        _release(store)


def _endpoint_volume(device: c_void_p) -> c_void_p:
    interface = c_void_p()
    _call(  # IMMDevice::Activate
        device, 3, pointer(IID_IAudioEndpointVolume), wintypes.DWORD(_CLSCTX_ALL),
        c_void_p(None), pointer(interface),
    )
    return interface


def _level_and_mute(device: c_void_p) -> tuple[int | None, bool | None]:
    volume = None
    try:
        volume = _endpoint_volume(device)
        scalar = c_float()
        _call(volume, 9, pointer(scalar))  # GetMasterVolumeLevelScalar
        muted = wintypes.BOOL()
        _call(volume, 15, pointer(muted))  # GetMute
        return int(round(scalar.value * 100)), bool(muted.value)
    except OSError:
        # A device can refuse this — an exclusive-mode endpoint held by
        # something else will — and a listing is still worth printing without
        # it. Only the level is missing, not the device.
        log.debug("No endpoint volume for a device", exc_info=True)
        return None, None
    finally:
        _release(volume)


# -------------------------------------------------------------------- public


def devices(flow: int = RENDER) -> list[Device]:
    """Every active endpoint on one side, the default one marked.

    Sorted by name so the numbering a person reads off this does not shuffle
    between two calls.
    """
    try:
        with _Apartment():
            enumerator = _enumerator()
            try:
                default = ""
                try:
                    device = c_void_p()
                    # GetDefaultAudioEndpoint, console role — the one Windows
                    # sends ordinary playback to.
                    _call(enumerator, 4, c_uint(flow), c_uint(0), pointer(device))
                    try:
                        default = _device_id(device)
                    finally:
                        _release(device)
                except OSError:
                    log.debug("No default endpoint for flow %d", flow, exc_info=True)

                collection = c_void_p()
                # EnumAudioEndpoints
                _call(
                    enumerator, 3, c_uint(flow),
                    wintypes.DWORD(_DEVICE_STATE_ACTIVE), pointer(collection),
                )
                try:
                    count = c_uint()
                    _call(collection, 3, pointer(count))  # GetCount
                    found: list[Device] = []
                    for index in range(count.value):
                        device = c_void_p()
                        _call(collection, 4, c_uint(index), pointer(device))  # Item
                        try:
                            identity = _device_id(device)
                            level, muted = _level_and_mute(device)
                            found.append(Device(
                                identity, _device_name(device) or identity,
                                identity == default, level, muted,
                            ))
                        finally:
                            _release(device)
                    return sorted(found, key=lambda d: d.name.lower())
                finally:
                    _release(collection)
            finally:
                _release(enumerator)
    except (OSError, ValueError):
        log.exception("Could not enumerate audio endpoints")
        return []


def set_default(device_id: str) -> bool:
    """Make one endpoint the default for every role.

    All three roles together: leaving communications pointed at the old device
    is what makes a voice call come out of the speakers you just switched away
    from, and nobody who typed this meant that.
    """
    try:
        with _Apartment():
            for iid, method in POLICY_CONFIG:
                interface = c_void_p()
                status = _ole32.CoCreateInstance(
                    pointer(CLSID_PolicyConfigClient), None, _CLSCTX_ALL,
                    pointer(iid), pointer(interface),
                )
                if status != _S_OK or not interface:
                    continue
                try:
                    for role in _ROLES:
                        _call(interface, method, c_wchar_p(device_id), c_uint(role))
                    return True
                except OSError:
                    log.debug("PolicyConfig %s refused the call", iid, exc_info=True)
                finally:
                    _release(interface)
    except (OSError, ValueError):
        log.exception("Could not set the default endpoint")
    return False


def set_level(device_id: str, percent: int) -> bool:
    """Set one endpoint's own master level, default or not."""
    percent = max(0, min(100, percent))
    try:
        with _Apartment():
            enumerator = _enumerator()
            try:
                device = c_void_p()
                _call(enumerator, 5, c_wchar_p(device_id), pointer(device))  # GetDevice
                volume = None
                try:
                    volume = _endpoint_volume(device)
                    # SetMasterVolumeLevelScalar, with no notification context.
                    _call(volume, 7, c_float(percent / 100.0), c_void_p(None))
                    return True
                finally:
                    _release(volume)
                    _release(device)
            finally:
                _release(enumerator)
    except (OSError, ValueError):
        log.exception("Could not set the level on %s", device_id)
        return False


def find(name: str, flow: int = RENDER) -> list[Device]:
    """Endpoints whose name contains `name`, case-insensitively."""
    needle = name.strip().lower()
    if not needle:
        return []
    matches = [d for d in devices(flow) if needle in d.name.lower()]
    # An exact name beats a substring, so "speakers" can still pick the device
    # actually called Speakers out of three that mention it.
    exact = [d for d in matches if d.name.lower() == needle]
    return exact or matches
