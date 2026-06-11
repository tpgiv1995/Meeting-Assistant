"""Subprocess worker: WASAPI capture-session enumeration, isolated from the app.

This is how packaged/MSIX apps (new Teams, Slack huddles) are detected — they
do NOT update the ConsentStore registry while a call is live, so the only live
signal is an active WASAPI capture session. That enumeration is native COM
(AUDIOSES.DLL) and can fault on a flaky audio stack, which is exactly why it
lives in this disposable subprocess: a native fault kills the worker, never the
app. call_watch.py respawns it with self-healing backoff.

Hardening to keep kernel-audio stress low (a tight crash/poll loop on a fragile
machine once escalated to a BSOD):
  - MTA apartment (recommended for audio session enumeration off the UI thread).
  - The device enumerator is created ONCE and reused across polls instead of
    churning a new COM object every time; it's only rebuilt after an error.
  - A parent-liveness watchdog so a hung COM call can never orphan this process.

Protocol: parent writes "poll\n" on stdin; worker answers with one JSON line —
a list of exe stems (lowercase, no .exe) with an ACTIVE capture session. The
app's own pid (argv[1]) and its children are excluded so a running recording
never reads as a call.

Run: python -m ui_desktop.mic_session_worker <app_pid>
"""
from __future__ import annotations

import json
import sys

_devenum = None  # cached IMMDeviceEnumerator (rebuilt on error)


def _get_enumerator():
    global _devenum
    if _devenum is None:
        import comtypes
        from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
        from pycaw.constants import CLSID_MMDeviceEnumerator
        _devenum = comtypes.CoCreateInstance(
            CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER)
    return _devenum


def _list_active(app_pid: int) -> list[str]:
    global _devenum
    from ctypes import POINTER, cast

    import comtypes
    import psutil
    from pycaw.pycaw import IAudioSessionControl2, IAudioSessionManager2

    try:
        devenum = _get_enumerator()
        coll = devenum.EnumAudioEndpoints(1, 1)  # eCapture, DEVICE_STATE_ACTIVE
        active: list[str] = []
        for i in range(coll.GetCount()):
            # One bad/transitional endpoint (e.g. a network audio device mid-
            # reconnect) shouldn't blank the whole result — skip it and go on.
            try:
                mgr = cast(
                    coll.Item(i).Activate(IAudioSessionManager2._iid_, comtypes.CLSCTX_ALL, None),
                    POINTER(IAudioSessionManager2))
                senum = mgr.GetSessionEnumerator()
                count = senum.GetCount()
            except Exception:
                continue
            for j in range(count):
                try:
                    ctl2 = senum.GetSession(j).QueryInterface(IAudioSessionControl2)
                    if ctl2.GetState() != 1:  # AudioSessionStateActive
                        continue
                    pid = ctl2.GetProcessId()
                except Exception:
                    continue
                if not pid or pid == app_pid:
                    continue
                try:
                    proc = psutil.Process(pid)
                    if proc.ppid() == app_pid:
                        continue  # the app's own capture subprocess
                    exe = proc.name().lower()
                except Exception:
                    continue
                active.append(exe[:-4] if exe.endswith(".exe") else exe)
        return active
    except Exception:
        # A catchable COM error (HRESULT) — drop the cached enumerator so the
        # next poll rebuilds it. (Native access violations aren't catchable
        # here; the subprocess isolation handles those.)
        _devenum = None
        raise


def _parent_watchdog(parent_pid: int) -> None:
    """Force-exit if the parent app dies. The stdin-EOF path only fires while
    we're reading stdin; if we're blocked inside a native COM call, this
    independent thread still tears us down so we never orphan."""
    import os
    import time
    if not parent_pid:
        return
    while True:
        time.sleep(5)
        alive = True
        try:
            import psutil
            alive = psutil.pid_exists(parent_pid)
        except Exception:
            try:
                os.kill(parent_pid, 0)
            except OSError:
                alive = False
        if not alive:
            os._exit(0)


def main() -> None:
    import threading
    app_pid = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    threading.Thread(target=_parent_watchdog, args=(app_pid,), daemon=True).start()

    import comtypes
    try:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except Exception:
        comtypes.CoInitialize()

    for line in sys.stdin:
        if line.strip() != "poll":
            continue
        try:
            result = _list_active(app_pid)
        except Exception:
            result = []
        sys.stdout.write(json.dumps(result) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
