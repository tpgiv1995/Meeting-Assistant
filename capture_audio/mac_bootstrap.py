"""macOS first-launch bootstrap + recording-time audio routing.

What this module does:

1. **BlackHole install** — checks for BlackHole 2ch via system_profiler;
   installs via Homebrew when missing. BlackHole's GitHub releases no longer
   ship a downloadable .pkg, so when brew is absent or not writable we
   surface a clear manual-install message.

2. **Aggregate output device** — creates (idempotently) a private CoreAudio
   aggregate device named "Meeting Assistant Output" that mixes BlackHole 2ch
   with the user's previous default output. This is the *output* route used
   during recording so the user keeps hearing audio while BlackHole captures
   a copy.

3. **Recording-time routing** — `prepare_recording_routing()` switches the
   system default output to the aggregate at recording start;
   `restore_recording_routing()` puts it back on stop. Without this step
   BlackHole receives silence (the loopback bug we hit on first run).

CoreAudio calls go through `ctypes` against the live `CoreAudio.framework`
binary because PyObjC's bridge for `AudioObjectGetPropertyData` (with the
`c_array_length_in_arg` annotation) is buggy in v12.1 and refuses any
caller-allocated buffer. CFDictionary construction for the aggregate
description still uses PyObjC (NSDictionary toll-free-bridges to
CFDictionaryRef cleanly).

All operations no-op on non-darwin platforms.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import shutil
import subprocess
import sys
from ctypes import (
    POINTER, Structure, byref, c_bool, c_char_p, c_int32, c_uint32, c_void_p,
)
from typing import Optional

from core import log as log


# ── Public constants ────────────────────────────────────────────────────────

_AGGREGATE_DEVICE_NAME = "Meeting Assistant Output"
_AGGREGATE_DEVICE_UID  = "com.meetingassistant.aggregate.output.v2"

# Substrings used to locate BlackHole's CoreAudio device.
_BLACKHOLE_NAME_HINTS = ("BlackHole 2ch", "BlackHole 16ch", "BlackHole")


def is_supported_platform() -> bool:
    return sys.platform == "darwin"


# ── BlackHole detection ──────────────────────────────────────────────────────

def is_blackhole_installed() -> bool:
    """True if BlackHole appears in the system audio device list."""
    if not is_supported_platform():
        return False
    try:
        r = subprocess.run(
            ["system_profiler", "SPAudioDataType"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as e:
        log.warn("mac-audio", f"system_profiler failed: {e}")
        return False
    return "BlackHole" in (r.stdout or "")


# ── BlackHole installation ───────────────────────────────────────────────────

def _install_via_homebrew() -> tuple[bool, str]:
    """Try `brew install --cask blackhole-2ch`. Returns (success, error_hint).

    error_hint is a short human-readable summary of the failure cause for the
    final user-facing message — empty on success.
    """
    brew = shutil.which("brew")
    if not brew:
        return False, "Homebrew not installed"
    log.info("mac-audio", "Installing BlackHole 2ch via Homebrew…")
    try:
        r = subprocess.run(
            [brew, "install", "--cask", "blackhole-2ch"],
            capture_output=True, text=True, timeout=300,
        )
    except Exception as e:
        log.warn("mac-audio", f"brew install failed: {e}")
        return False, f"brew exec failed: {e}"
    if r.returncode == 0:
        log.info("mac-audio", "Homebrew install succeeded")
        return True, ""
    err = (r.stderr or r.stdout or "").strip()
    log.warn("mac-audio", f"brew install returned {r.returncode}: {err[:300]}")
    if "not writable" in err or "Permission denied" in err:
        return False, "Homebrew prefix is not writable by the current user"
    if "a terminal is required" in err.lower() or "askpass" in err.lower():
        return False, "Homebrew needs sudo for the pkg installer step"
    return False, "brew install failed (see log)"


def install_blackhole() -> bool:
    """Install BlackHole if not already present. Returns True on success or
    if it was already installed."""
    if not is_supported_platform():
        return False
    if is_blackhole_installed():
        log.info("mac-audio", "BlackHole already installed")
        return True

    ok, hint = _install_via_homebrew()
    if ok:
        return is_blackhole_installed()

    if hint == "Homebrew prefix is not writable by the current user":
        guidance = (
            "Homebrew is installed but not writable by this user. "
            "Either run `sudo chown -R $(whoami) /opt/homebrew` once, or "
            "install BlackHole manually from https://existential.audio/blackhole/ "
            "and relaunch Meeting Assistant."
        )
    elif hint == "Homebrew not installed":
        guidance = (
            "Homebrew not found. Install Homebrew (https://brew.sh) and run "
            "`brew install --cask blackhole-2ch`, or download BlackHole "
            "manually from https://existential.audio/blackhole/."
        )
    elif hint == "Homebrew needs sudo for the pkg installer step":
        guidance = (
            "BlackHole's installer needs sudo access. Run "
            "`sudo brew install --cask blackhole-2ch` in a terminal once, or "
            "download the pkg manually from https://existential.audio/blackhole/."
        )
    else:
        guidance = (
            "Could not auto-install BlackHole. Run "
            "`brew install --cask blackhole-2ch` manually, or download from "
            "https://existential.audio/blackhole/, then relaunch."
        )
    log.error("mac-audio", guidance)
    return False


# ── CoreAudio C API via ctypes ───────────────────────────────────────────────

class _AudioObjectPropertyAddress(Structure):
    _fields_ = [
        ("mSelector", c_uint32),
        ("mScope",    c_uint32),
        ("mElement",  c_uint32),
    ]


_CA: ctypes.CDLL | None = None
_CF: ctypes.CDLL | None = None


def _fourcc(s: str) -> int:
    return (ord(s[0]) << 24) | (ord(s[1]) << 16) | (ord(s[2]) << 8) | ord(s[3])


# CoreAudio constant fourCCs (mirror values from <CoreAudio/AudioHardware.h>)
_kAudioObjectSystemObject                      = 1
_kAudioHardwarePropertyDevices                 = _fourcc("dev#")
_kAudioHardwarePropertyDefaultOutputDevice     = _fourcc("dOut")
_kAudioObjectPropertyScopeGlobal               = _fourcc("glob")
_kAudioObjectPropertyElementMain               = 0
_kAudioDevicePropertyDeviceUID                 = _fourcc("uid ")
_kAudioObjectPropertyName                      = _fourcc("lnam")
_kCFStringEncodingUTF8                         = 0x08000100


def _ensure_corelib() -> bool:
    """Load CoreAudio + CoreFoundation and set up function signatures. Idempotent."""
    global _CA, _CF
    if _CA is not None and _CF is not None:
        return True
    try:
        ca = ctypes.CDLL(ctypes.util.find_library("CoreAudio"))
        cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
    except Exception as e:
        log.error("mac-audio", f"Could not load CoreAudio/CoreFoundation: {e}")
        return False

    # AudioObjectGetPropertyDataSize(objID, addr*, qualSize, qual*, ioSize*) -> OSStatus
    ca.AudioObjectGetPropertyDataSize.restype = c_int32
    ca.AudioObjectGetPropertyDataSize.argtypes = [
        c_uint32, POINTER(_AudioObjectPropertyAddress), c_uint32, c_void_p, POINTER(c_uint32),
    ]
    # AudioObjectGetPropertyData(objID, addr*, qualSize, qual*, ioSize*, outData*) -> OSStatus
    ca.AudioObjectGetPropertyData.restype = c_int32
    ca.AudioObjectGetPropertyData.argtypes = [
        c_uint32, POINTER(_AudioObjectPropertyAddress), c_uint32, c_void_p,
        POINTER(c_uint32), c_void_p,
    ]
    # AudioObjectSetPropertyData(objID, addr*, qualSize, qual*, dataSize, data*) -> OSStatus
    ca.AudioObjectSetPropertyData.restype = c_int32
    ca.AudioObjectSetPropertyData.argtypes = [
        c_uint32, POINTER(_AudioObjectPropertyAddress), c_uint32, c_void_p,
        c_uint32, c_void_p,
    ]
    # AudioHardwareCreateAggregateDevice(CFDictionaryRef, AudioObjectID*) -> OSStatus
    ca.AudioHardwareCreateAggregateDevice.restype = c_int32
    ca.AudioHardwareCreateAggregateDevice.argtypes = [c_void_p, POINTER(c_uint32)]
    # AudioHardwareDestroyAggregateDevice(AudioObjectID) -> OSStatus
    ca.AudioHardwareDestroyAggregateDevice.restype = c_int32
    ca.AudioHardwareDestroyAggregateDevice.argtypes = [c_uint32]

    cf.CFStringCreateWithCString.restype = c_void_p
    cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_uint32]
    cf.CFStringGetCString.restype  = c_bool
    cf.CFStringGetCString.argtypes = [c_void_p, c_char_p, c_int32, c_uint32]
    cf.CFStringGetLength.restype   = c_int32
    cf.CFStringGetLength.argtypes  = [c_void_p]
    cf.CFRelease.restype  = None
    cf.CFRelease.argtypes = [c_void_p]

    _CA, _CF = ca, cf
    return True


def _read_cfstring(cf_ptr: int) -> Optional[str]:
    """Copy a CFStringRef's contents to a Python str. Does NOT release the input."""
    if not cf_ptr or _CF is None:
        return None
    length = _CF.CFStringGetLength(cf_ptr) * 4 + 1  # UTF-8 worst-case 4 bytes/char + null
    buf = ctypes.create_string_buffer(length)
    if _CF.CFStringGetCString(cf_ptr, buf, length, _kCFStringEncodingUTF8):
        return buf.value.decode("utf-8")
    return None


def _list_all_device_ids() -> list[int]:
    """Return every CoreAudio device's AudioObjectID."""
    if not _ensure_corelib():
        return []
    addr = _AudioObjectPropertyAddress(
        _kAudioHardwarePropertyDevices,
        _kAudioObjectPropertyScopeGlobal,
        _kAudioObjectPropertyElementMain,
    )
    size = c_uint32(0)
    if _CA.AudioObjectGetPropertyDataSize(
        _kAudioObjectSystemObject, byref(addr), 0, None, byref(size),
    ) != 0:
        return []
    n = size.value // 4
    if n == 0:
        return []
    buf = (c_uint32 * n)()
    sz_io = c_uint32(size.value)
    if _CA.AudioObjectGetPropertyData(
        _kAudioObjectSystemObject, byref(addr), 0, None, byref(sz_io), buf,
    ) != 0:
        return []
    return list(buf)


def _get_device_uid(device_id: int) -> Optional[str]:
    """Return the device's UID string, or None if unavailable."""
    if not _ensure_corelib():
        return None
    addr = _AudioObjectPropertyAddress(
        _kAudioDevicePropertyDeviceUID,
        _kAudioObjectPropertyScopeGlobal,
        _kAudioObjectPropertyElementMain,
    )
    sz = c_uint32(8)  # CFStringRef pointer
    cf_ptr = c_void_p()
    status = _CA.AudioObjectGetPropertyData(
        device_id, byref(addr), 0, None, byref(sz), byref(cf_ptr),
    )
    if status != 0 or not cf_ptr.value:
        return None
    try:
        return _read_cfstring(cf_ptr.value)
    finally:
        _CF.CFRelease(cf_ptr)


def _get_device_name(device_id: int) -> Optional[str]:
    """Return the device's user-visible name (kAudioObjectPropertyName)."""
    if not _ensure_corelib():
        return None
    addr = _AudioObjectPropertyAddress(
        _kAudioObjectPropertyName,
        _kAudioObjectPropertyScopeGlobal,
        _kAudioObjectPropertyElementMain,
    )
    sz = c_uint32(8)
    cf_ptr = c_void_p()
    status = _CA.AudioObjectGetPropertyData(
        device_id, byref(addr), 0, None, byref(sz), byref(cf_ptr),
    )
    if status != 0 or not cf_ptr.value:
        return None
    try:
        return _read_cfstring(cf_ptr.value)
    finally:
        _CF.CFRelease(cf_ptr)


def _list_devices() -> list[tuple[int, str, str]]:
    """Return [(device_id, uid, name)] for every CoreAudio device."""
    out: list[tuple[int, str, str]] = []
    for did in _list_all_device_ids():
        uid = _get_device_uid(did) or ""
        name = _get_device_name(did) or ""
        out.append((did, uid, name))
    return out


def _find_device_by_uid(uid: str) -> Optional[int]:
    for did, dev_uid, _name in _list_devices():
        if dev_uid == uid:
            return did
    return None


def _find_blackhole_uid() -> Optional[str]:
    """Find BlackHole's UID by name substring match."""
    for _did, uid, name in _list_devices():
        if any(hint in name for hint in _BLACKHOLE_NAME_HINTS):
            return uid
    return None


def _get_default_output_device_id() -> Optional[int]:
    """Return the AudioObjectID of the system's current default output device."""
    if not _ensure_corelib():
        return None
    addr = _AudioObjectPropertyAddress(
        _kAudioHardwarePropertyDefaultOutputDevice,
        _kAudioObjectPropertyScopeGlobal,
        _kAudioObjectPropertyElementMain,
    )
    sz = c_uint32(4)
    out = c_uint32(0)
    status = _CA.AudioObjectGetPropertyData(
        _kAudioObjectSystemObject, byref(addr), 0, None, byref(sz), byref(out),
    )
    if status != 0:
        return None
    return out.value


def _set_default_output_device_id(device_id: int) -> bool:
    """Set the system's default output device. Returns True on success."""
    if not _ensure_corelib():
        return False
    addr = _AudioObjectPropertyAddress(
        _kAudioHardwarePropertyDefaultOutputDevice,
        _kAudioObjectPropertyScopeGlobal,
        _kAudioObjectPropertyElementMain,
    )
    val = c_uint32(device_id)
    status = _CA.AudioObjectSetPropertyData(
        _kAudioObjectSystemObject, byref(addr), 0, None,
        ctypes.sizeof(c_uint32), byref(val),
    )
    if status != 0:
        log.warn("mac-audio", f"Set default output to {device_id} failed (status={status})")
        return False
    return True


def _create_aggregate_device(
    name: str, uid: str,
    sub_device_uids: list[str],
    main_uid: Optional[str],
    stacked: bool = True,
    private: bool = False,
) -> Optional[int]:
    """Call AudioHardwareCreateAggregateDevice with a CFDictionary description.

    The description is built as an NSDictionary via PyObjC (toll-free
    bridged to CFDictionaryRef). Returns the new aggregate's AudioObjectID
    on success, None on failure.
    """
    if not _ensure_corelib():
        return None
    try:
        from Foundation import NSDictionary, NSArray, NSNumber  # type: ignore[import-not-found]
        import objc                                              # type: ignore[import-not-found]
    except ImportError as e:
        log.error("mac-audio", f"PyObjC Foundation not available: {e}")
        return None

    sub_array = NSArray.arrayWithArray_(
        [NSDictionary.dictionaryWithDictionary_({"uid": u}) for u in sub_device_uids]
    )

    desc_dict: dict = {
        "name":         name,
        "uid":          uid,
        "subdevices":   sub_array,
        "stacked":      NSNumber.numberWithInt_(1 if stacked else 0),
        "private":      NSNumber.numberWithInt_(1 if private else 0),
    }
    if main_uid:
        desc_dict["master"] = main_uid

    desc = NSDictionary.dictionaryWithDictionary_(desc_dict)
    desc_ptr = objc.pyobjc_id(desc)

    out_id = c_uint32(0)
    status = _CA.AudioHardwareCreateAggregateDevice(c_void_p(desc_ptr), byref(out_id))
    if status != 0:
        log.error("mac-audio", f"AudioHardwareCreateAggregateDevice failed (status={status})")
        return None
    log.info("mac-audio", f"Created aggregate '{name}' (id={out_id.value}, uid={uid})")
    return out_id.value


def _find_or_create_aggregate(speakers_uid: str, blackhole_uid: str) -> Optional[int]:
    """Idempotent: return the aggregate's AudioObjectID, creating it if missing."""
    existing = _find_device_by_uid(_AGGREGATE_DEVICE_UID)
    if existing is not None:
        log.info("mac-audio", f"Aggregate '{_AGGREGATE_DEVICE_NAME}' already exists "
                              f"(id={existing})")
        return existing
    # Order matters: list speakers first so the device's clock follows the
    # built-in audio. Set 'main' to speakers for the same reason.
    return _create_aggregate_device(
        name=_AGGREGATE_DEVICE_NAME,
        uid=_AGGREGATE_DEVICE_UID,
        sub_device_uids=[speakers_uid, blackhole_uid],
        main_uid=speakers_uid,
        stacked=True,
        private=False,
    )


# ── Public API used by audio_capture_mac and launch.py ──────────────────────

def ensure_aggregate_device(prev_default_name: Optional[str] = None) -> bool:
    """Idempotently ensure the Meeting Assistant aggregate device exists.

    `prev_default_name` is unused now (kept for backward-compat with the old
    signature called from `launch.bootstrap_first_launch`). The aggregate
    always uses the current default output as its speakers sub-device.

    Returns True if the aggregate was found or created; False otherwise.
    """
    if not is_supported_platform():
        return False
    if not is_blackhole_installed():
        log.warn("mac-audio", "Cannot create aggregate device: BlackHole not installed")
        return False

    if not _ensure_corelib():
        return False

    blackhole_uid = _find_blackhole_uid()
    if not blackhole_uid:
        log.warn("mac-audio", "BlackHole driver registered but UID lookup failed — "
                              "try `sudo killall coreaudiod` or reboot.")
        return False

    default_id = _get_default_output_device_id()
    if default_id is None:
        log.warn("mac-audio", "Could not read default output device id")
        return False
    speakers_uid = _get_device_uid(default_id)
    if not speakers_uid:
        log.warn("mac-audio", f"Default output device {default_id} has no UID")
        return False

    # If the current default is *already* the aggregate, recover the speakers
    # UID from its sub-device list — but we don't try to introspect that here;
    # just bail and let restore_recording_routing fix it.
    if speakers_uid == _AGGREGATE_DEVICE_UID:
        log.info("mac-audio", "Aggregate is already the default output (recording in progress?)")
        return True

    agg_id = _find_or_create_aggregate(speakers_uid, blackhole_uid)
    return agg_id is not None


def prepare_recording_routing() -> dict:
    """Switch system default output to the aggregate device for recording.

    Returns a status dict:
        {"ok": bool,
         "prev_default_id": int | None,    # pass back to restore_recording_routing()
         "aggregate_id": int | None,
         "message": str}                    # short reason on failure
    """
    status: dict = {"ok": False, "prev_default_id": None, "aggregate_id": None, "message": ""}
    if not is_supported_platform():
        status["message"] = "not macOS"
        return status
    if not _ensure_corelib():
        status["message"] = "CoreAudio not available"
        return status
    if not is_blackhole_installed():
        status["message"] = "BlackHole not installed"
        return status

    blackhole_uid = _find_blackhole_uid()
    if not blackhole_uid:
        status["message"] = "BlackHole UID lookup failed"
        return status

    prev_id = _get_default_output_device_id()
    if prev_id is None:
        status["message"] = "Could not read default output device"
        return status
    prev_uid = _get_device_uid(prev_id)
    status["prev_default_id"] = prev_id

    # If the user's default is already the aggregate (e.g. they manually
    # selected it), just record without further switching — but remember
    # that we should NOT touch it on stop().
    if prev_uid == _AGGREGATE_DEVICE_UID:
        agg_id = prev_id
        status["aggregate_id"] = agg_id
        status["ok"] = True
        status["prev_default_id"] = None  # signal: don't restore
        status["message"] = "Aggregate already selected as default — leaving routing alone"
        return status

    # Speakers sub-device is whatever the user is currently listening through.
    speakers_uid = prev_uid
    if not speakers_uid:
        status["message"] = "Default output has no UID"
        return status

    agg_id = _find_or_create_aggregate(speakers_uid, blackhole_uid)
    if agg_id is None:
        status["message"] = "Could not create aggregate device"
        return status
    status["aggregate_id"] = agg_id

    if not _set_default_output_device_id(agg_id):
        status["message"] = "Could not switch default output to aggregate"
        return status

    status["ok"] = True
    status["message"] = (
        f"Routing: default output → '{_AGGREGATE_DEVICE_NAME}' "
        f"(speakers={speakers_uid}, blackhole={blackhole_uid})"
    )
    log.info("mac-audio", status["message"])
    return status


def restore_recording_routing(prev_default_id: Optional[int]) -> bool:
    """Restore the system default output to whatever it was before recording.

    Pass the `prev_default_id` returned by `prepare_recording_routing()`. If
    None, this is a no-op (the user's default was already the aggregate).
    """
    if not is_supported_platform():
        return False
    if prev_default_id is None:
        return True
    if not _ensure_corelib():
        return False
    ok = _set_default_output_device_id(prev_default_id)
    if ok:
        log.info("mac-audio", f"Restored default output to device id {prev_default_id}")
    return ok


# ── Public bootstrap entry ───────────────────────────────────────────────────

def bootstrap_first_launch() -> dict:
    """Run the macOS first-launch audio bootstrap (called from launch.py).

    Returns:
        {"installed": bool, "aggregate_ready": bool, "messages": [...]}.
    """
    status: dict = {
        "installed": False,
        "aggregate_ready": False,
        "messages": [],
    }
    if not is_supported_platform():
        return status

    status["installed"] = install_blackhole()
    if not status["installed"]:
        # Detailed guidance was already logged from install_blackhole().
        status["messages"].append(
            "BlackHole 2ch not installed — system-audio capture will be "
            "disabled. See the [mac-audio] line above for next steps."
        )
        return status

    status["aggregate_ready"] = ensure_aggregate_device()
    if not status["aggregate_ready"]:
        status["messages"].append(
            "Aggregate output device not created. Recording will work only "
            "for the microphone; system-audio capture will be silent until "
            "an aggregate device is set up."
        )

    return status
