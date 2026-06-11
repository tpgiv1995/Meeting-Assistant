"""Microphone-usage call detection (Windows).

Two complementary signals, merged each poll:

1. **WASAPI capture sessions** (primary) — enumerate active audio sessions
   on every active capture endpoint; any process with an active session has
   the mic open. Sees packaged (MSIX) and classic apps identically.
2. **ConsentStore registry** (secondary) — the data behind the taskbar mic
   privacy indicator. An app holding the mic has LastUsedTimeStart != 0 and
   LastUsedTimeStop == 0. CRITICAL LIMITATION: Windows only maintains this
   *live* for classic Win32 apps; for packaged apps (new Teams!) both
   timestamps are written when usage ENDS, so the registry alone can never
   see a packaged app mid-call. Kept as a fallback for environments where
   pycaw/COM is unavailable.

Either way the approach is meeting-platform-agnostic: any app that opens
the mic — Teams, Zoom, Webex, a Google Meet tab — produces the same signal,
with no bot joining the call and no per-platform integration.

The watcher only *detects*; all recording policy (start/stop, manual
overrides, model readiness) lives in app.py's tick handler.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from typing import Callable

from core import log as log

CALL_WATCH_AVAILABLE = sys.platform == "win32"

_CONSENT_STORE = (
    r"Software\Microsoft\Windows\CurrentVersion"
    r"\CapabilityAccessManager\ConsentStore\microphone"
)

POLL_SEC = 2.0
# Consecutive active polls required before a call counts as started. Filters
# short mic grabs (voice typing, a permission prompt probe) without adding
# meaningful latency to real calls.
START_CONFIRM_POLLS = 2

# Our own process's ConsentStore keys (NonPackaged keys are exe paths with
# '#' for '\'). The recorder itself opens a WASAPI mic, so without this
# exclusion an auto-started recording would read as an ongoing call and keep
# itself alive forever. Windows tracks the *real* process image — for a uv
# venv that's the base CPython exe, not the venv shim sys.executable points
# at — so resolve both.
def _self_keys() -> set[str]:
    paths = {sys.executable}
    if sys.platform == "win32":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(2048)
            ctypes.windll.kernel32.GetModuleFileNameW(None, buf, 2048)
            if buf.value:
                paths.add(buf.value)
        except Exception:
            pass
    # ConsentStore records junction-resolved paths (uv pins venvs to a
    # versioned dir through a junction), so include resolved variants too.
    from pathlib import Path
    for p in list(paths):
        try:
            paths.add(str(Path(p).resolve()))
        except OSError:
            pass
    return {p.replace("\\", "#").lower() for p in paths if p}

_SELF_KEYS = _self_keys()


# ── Session-detection worker management ──────────────────────────────────────
# COM session enumeration once took the whole app down with a native fault
# when it ran in-process next to live WASAPI capture, so it lives in a
# disposable subprocess (mic_session_worker.py). If the worker crashes or
# hangs we kill it, fall back to registry-only detection, and retry later.

_WORKER_RESTART_BACKOFF_SEC = 60
_WORKER_REPLY_TIMEOUT_SEC = 3.0

_worker_lock = threading.Lock()
_worker = None            # subprocess.Popen | None
_worker_queue = None      # queue.Queue of stdout lines
_worker_next_spawn = 0.0  # monotonic gate for restart backoff


def _spawn_session_worker():
    import queue
    import subprocess
    global _worker, _worker_queue

    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mic_session_worker.py")
    proc = subprocess.Popen(
        [sys.executable, script, str(os.getpid())],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    q: "queue.Queue[str]" = queue.Queue()

    def _reader():
        try:
            for line in proc.stdout:
                q.put(line)
        except Exception:
            pass

    threading.Thread(target=_reader, daemon=True, name="mic-session-reader").start()
    _worker, _worker_queue = proc, q
    log.info("call-watch", f"Session worker started (pid {proc.pid}).")


def _kill_session_worker() -> None:
    global _worker, _worker_queue, _worker_next_spawn
    if _worker is not None:
        try:
            _worker.kill()
        except Exception:
            pass
    _worker, _worker_queue = None, None
    _worker_next_spawn = time.monotonic() + _WORKER_RESTART_BACKOFF_SEC


def _list_session_mic_apps() -> list[str]:
    """Exe stems of processes with an active WASAPI capture session.

    Primary signal — sees packaged (MSIX) apps like new Teams live, which
    the ConsentStore registry does not. Returns [] when the worker is down;
    the registry signal still covers classic apps meanwhile.
    """
    import json as _json
    import queue
    global _worker_next_spawn

    with _worker_lock:
        try:
            if _worker is None or _worker.poll() is not None:
                if time.monotonic() < _worker_next_spawn:
                    return []
                _spawn_session_worker()
            _worker.stdin.write("poll\n")
            _worker.stdin.flush()
            line = _worker_queue.get(timeout=_WORKER_REPLY_TIMEOUT_SEC)
            result = _json.loads(line)
            return result if isinstance(result, list) else []
        except (queue.Empty, OSError, ValueError) as e:
            log.warn("call-watch", f"Session worker unresponsive ({type(e).__name__}) - "
                                   f"restarting in {_WORKER_RESTART_BACKOFF_SEC}s; registry-only until then.")
            _kill_session_worker()
            return []
        except Exception as e:
            log.warn("call-watch", f"Session worker failed ({e}) - registry-only until restart.")
            _kill_session_worker()
            return []


def _list_registry_mic_apps() -> list[str]:
    """ConsentStore mic users: packaged apps yield their package family name
    ("msteams_8wekyb3d8bbwe"), classic apps their exe stem ("zoom"). Live
    only for classic apps — see module docstring."""
    import winreg

    active: list[str] = []

    def _check_key(root, subpath: str, name: str, packaged: bool) -> None:
        try:
            with winreg.OpenKey(root, subpath) as k:
                start, _ = winreg.QueryValueEx(k, "LastUsedTimeStart")
                stop, _ = winreg.QueryValueEx(k, "LastUsedTimeStop")
        except OSError:
            return
        if start and not stop:
            if packaged:
                active.append(name.lower())
            else:
                # NonPackaged key names are full paths with '#' for '\'
                if name.lower() in _SELF_KEYS:
                    return  # our own mic stream is not a call
                exe = name.rsplit("#", 1)[-1].lower()
                active.append(exe[:-4] if exe.endswith(".exe") else exe)

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _CONSENT_STORE) as base:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(base, i)
                except OSError:
                    break
                i += 1
                if sub == "NonPackaged":
                    continue
                _check_key(winreg.HKEY_CURRENT_USER, f"{_CONSENT_STORE}\\{sub}", sub, True)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{_CONSENT_STORE}\\NonPackaged") as base:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(base, i)
                except OSError:
                    break
                i += 1
                _check_key(
                    winreg.HKEY_CURRENT_USER,
                    f"{_CONSENT_STORE}\\NonPackaged\\{sub}", sub, False,
                )
    except OSError:
        return []
    return active


def list_active_mic_apps() -> list[str]:
    """Merged ids of apps currently holding the microphone (both signals)."""
    if not CALL_WATCH_AVAILABLE:
        return []
    merged = _list_session_mic_apps() + _list_registry_mic_apps()
    return list(dict.fromkeys(merged))


def match_apps(active: list[str], allowlist: str) -> list[str]:
    """Filter active mic users against a comma-separated allowlist.

    Matching is case-insensitive substring in either direction, so "teams"
    matches "msteams_8wekyb3d8bbwe" and "ms-teams" matches "msteams".
    """
    pats = [p.strip().lower().replace("-", "") for p in allowlist.split(",") if p.strip()]
    if not pats:
        return []
    out = []
    for app in active:
        normalized = app.replace("-", "")
        if any(p in normalized or normalized in p for p in pats):
            out.append(app)
    return out


class CallWatcher:
    """Polls mic usage and reports debounced call state via on_tick.

    on_tick(in_call: bool, apps: list[str]) fires every poll. ``in_call``
    turns True only after START_CONFIRM_POLLS consecutive matches and turns
    False only after the mic has been idle for ``stop_delay_sec`` (read from
    get_config each poll, so settings changes apply live). get_config must
    return {"enabled": bool, "apps": str, "stop_delay_sec": float}; while
    disabled the watcher idles without reading the registry.
    """

    def __init__(self, get_config: Callable[[], dict], on_tick: Callable[[bool, list[str]], None]):
        self._get_config = get_config
        self._on_tick = on_tick
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self.in_call = False
        self.current_apps: list[str] = []

    def start(self) -> None:
        if not CALL_WATCH_AVAILABLE or self._thread:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="call-watch")
        self._thread.start()
        log.info("call-watch", "Microphone call watcher started.")

    def stop(self) -> None:
        self._stop_evt.set()

    def _run(self) -> None:
        consecutive_active = 0
        idle_since: float | None = None
        while not self._stop_evt.wait(POLL_SEC):
            try:
                cfg = self._get_config()
                if not cfg.get("enabled"):
                    consecutive_active = 0
                    idle_since = None
                    if self.in_call:
                        self.in_call = False
                        self.current_apps = []
                    continue

                matched = match_apps(list_active_mic_apps(), cfg.get("apps", ""))
                stop_delay = float(cfg.get("stop_delay_sec", 20))

                if matched:
                    consecutive_active += 1
                    idle_since = None
                    self.current_apps = matched
                    if not self.in_call and consecutive_active >= START_CONFIRM_POLLS:
                        self.in_call = True
                        log.info("call-watch", f"Call detected: {', '.join(matched)}")
                else:
                    consecutive_active = 0
                    if self.in_call:
                        if idle_since is None:
                            idle_since = time.monotonic()
                        elif time.monotonic() - idle_since >= stop_delay:
                            self.in_call = False
                            self.current_apps = []
                            idle_since = None
                            log.info("call-watch", "Call ended (mic idle past stop delay).")
                    else:
                        self.current_apps = []

                self._on_tick(self.in_call, list(self.current_apps))
            except Exception as e:
                log.warn("call-watch", f"Watcher tick failed: {e}")
