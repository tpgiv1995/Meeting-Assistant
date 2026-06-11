"""Subprocess worker: WASAPI capture-session enumeration, isolated from the app.

COM session enumeration crashed the main process once when it ran on a
watcher thread while our own WASAPI capture streams were live, so it runs
here instead — a native fault kills this disposable worker, never the app.
call_watch.py restarts the worker with backoff if it dies.

Protocol: parent writes "poll\n" on stdin; worker answers with one JSON line
— a list of exe stems (lowercase, no .exe) that have an ACTIVE capture
session on any active mic endpoint. The app's own pid is passed as argv[1];
that process and its children (our recording stream, ffmpeg-dshow capture)
are excluded so a running recording never reads as a call.

Run: python -m ui_desktop.mic_session_worker <app_pid>
"""
from __future__ import annotations

import json
import sys


def _list_active(app_pid: int) -> list[str]:
    from ctypes import POINTER, cast

    import psutil
    from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
    from pycaw.constants import CLSID_MMDeviceEnumerator
    from pycaw.pycaw import IAudioSessionControl2, IAudioSessionManager2
    import comtypes

    active: list[str] = []
    devenum = comtypes.CoCreateInstance(
        CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER)
    coll = devenum.EnumAudioEndpoints(1, 1)  # eCapture, DEVICE_STATE_ACTIVE
    for i in range(coll.GetCount()):
        mgr = cast(
            coll.Item(i).Activate(IAudioSessionManager2._iid_, comtypes.CLSCTX_ALL, None),
            POINTER(IAudioSessionManager2))
        senum = mgr.GetSessionEnumerator()
        for j in range(senum.GetCount()):
            ctl2 = senum.GetSession(j).QueryInterface(IAudioSessionControl2)
            if ctl2.GetState() != 1:  # AudioSessionStateActive
                continue
            pid = ctl2.GetProcessId()
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


def main() -> None:
    app_pid = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    import comtypes
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
