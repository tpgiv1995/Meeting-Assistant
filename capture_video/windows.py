"""Windows screen recorder backend: FFmpeg gdigrab + DPI-aware EnumDisplayMonitors."""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import subprocess
import sys
import threading
from pathlib import Path

from core import log as log
from capture_video.ffmpeg_util import find_ffmpeg, subprocess_no_window_flag


# ── DPI awareness ────────────────────────────────────────────────────────────
# Enable per-monitor DPI awareness so EnumDisplayMonitors returns physical
# pixel coordinates and sizes - critical for correct gdigrab offsets on
# high-DPI / scaled displays. Call once at import time; harmless if the
# process (or a framework) already set a mode.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except (AttributeError, OSError):
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


# ── Display enumeration ──────────────────────────────────────────────────────

# MONITORINFOEXW is not in ctypes.wintypes - define it manually
class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.DWORD),
        ("rcMonitor", ctypes.wintypes.RECT),
        ("rcWork", ctypes.wintypes.RECT),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("szDevice", ctypes.c_wchar * 32),
    ]


def _get_monitor_dpi(hMonitor) -> int:
    try:
        dpiX = ctypes.c_uint()
        dpiY = ctypes.c_uint()
        ctypes.windll.shcore.GetDpiForMonitor(
            hMonitor, 0, ctypes.byref(dpiX), ctypes.byref(dpiY)
        )
        return dpiX.value or 96
    except (AttributeError, OSError):
        return 96


def enumerate_displays() -> list[dict]:
    """Return a list of display info dicts with physical and logical dims."""
    displays = []
    user32 = ctypes.windll.user32

    def _monitor_enum_proc(hMonitor, hdcMonitor, lprcMonitor, dwData):
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if user32.GetMonitorInfoW(hMonitor, ctypes.byref(info)):
            rc = info.rcMonitor
            is_primary = bool(info.dwFlags & 1)
            phys_w = rc.right - rc.left
            phys_h = rc.bottom - rc.top
            dpi = _get_monitor_dpi(hMonitor)
            scale = dpi / 96.0
            logical_w = round(phys_w / scale)
            logical_h = round(phys_h / scale)
            logical_x = round(rc.left / scale)
            logical_y = round(rc.top / scale)
            displays.append({
                "index": len(displays),
                "name": info.szDevice,
                "x": rc.left,
                "y": rc.top,
                "width": phys_w,
                "height": phys_h,
                "logical_x": logical_x,
                "logical_y": logical_y,
                "logical_width": logical_w,
                "logical_height": logical_h,
                "dpi": dpi,
                "scale": scale,
                "primary": is_primary,
            })
        return True

    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_int,
        ctypes.wintypes.HMONITOR,
        ctypes.wintypes.HDC,
        ctypes.POINTER(ctypes.wintypes.RECT),
        ctypes.wintypes.LPARAM,
    )
    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_monitor_enum_proc), 0)

    for i, d in enumerate(displays):
        suffix = " (Primary)" if d["primary"] else ""
        d["label"] = f"Display {i + 1}: {d['width']}x{d['height']}{suffix}"
    return displays


def flash_display_border(display_index: int, duration_ms: int = 1500, thickness: int = 6):
    """Flash a colored border around the given display via Win32 popup windows."""
    displays = enumerate_displays()
    if display_index < 0 or display_index >= len(displays):
        return

    d = displays[display_index]
    x, y, w, h = d["x"], d["y"], d["width"], d["height"]
    t = max(2, min(int(thickness), max(2, min(w, h) // 8)))
    duration_ms = max(100, int(duration_ms))

    rects = [
        (x, y, w, t),
        (x, y + h - t, w, t),
        (x, y, t, h),
        (x + w - t, y, t, h),
    ]
    rects_json = json.dumps(rects)

    script = f"""
import ctypes
import ctypes.wintypes as wt
import json

try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

rects = json.loads({rects_json!r})
duration_ms = {duration_ms}
color = 0xFFA658

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

WM_DESTROY = 0x0002
WM_NCHITTEST = 0x0084
WM_TIMER = 0x0113
HTTRANSPARENT = -1
WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
SW_SHOWNOACTIVATE = 4
HWND_TOPMOST = -1
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
COLOR_WINDOW = 5

class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HANDLE),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HANDLE),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]

class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wt.HWND),
        ("message", wt.UINT),
        ("wParam", wt.WPARAM),
        ("lParam", wt.LPARAM),
        ("time", wt.DWORD),
        ("pt", wt.POINT),
        ("lPrivate", wt.DWORD),
    ]

windows = []
brush = gdi32.CreateSolidBrush(color)

def wndproc(hwnd, msg, wparam, lparam):
    if msg == WM_NCHITTEST:
        return HTTRANSPARENT
    if msg == WM_TIMER:
        user32.DestroyWindow(hwnd)
        return 0
    if msg == WM_DESTROY:
        if hwnd in windows:
            windows.remove(hwnd)
        if not windows:
            user32.PostQuitMessage(0)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

wndproc_ref = WNDPROC(wndproc)
class_name = "MeetingAssistantDisplayHighlight"
hinstance = kernel32.GetModuleHandleW(None)

wc = WNDCLASSW()
wc.lpfnWndProc = wndproc_ref
wc.hInstance = hinstance
wc.lpszClassName = class_name
wc.hbrBackground = brush
wc.hCursor = user32.LoadCursorW(None, 32512)

atom = user32.RegisterClassW(ctypes.byref(wc))
if not atom:
    raise ctypes.WinError(ctypes.get_last_error())

for left, top, width, height in rects:
    hwnd = user32.CreateWindowExW(
        WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE,
        class_name,
        None,
        WS_POPUP | WS_VISIBLE,
        left,
        top,
        width,
        height,
        None,
        None,
        hinstance,
        None,
    )
    if not hwnd:
        continue
    windows.append(hwnd)
    user32.SetWindowPos(hwnd, HWND_TOPMOST, left, top, width, height, SWP_NOACTIVATE | SWP_SHOWWINDOW)
    user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
    user32.UpdateWindow(hwnd)
    user32.SetTimer(hwnd, 1, duration_ms, None)

msg = MSG()
while windows and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
    user32.TranslateMessage(ctypes.byref(msg))
    user32.DispatchMessageW(ctypes.byref(msg))

if brush:
    gdi32.DeleteObject(brush)
"""
    subprocess.Popen(
        [sys.executable, "-c", script],
        creationflags=subprocess_no_window_flag(),
    )


# ── Screen recorder class ───────────────────────────────────────────────────

class ScreenRecorder:
    """Manages an ffmpeg gdigrab subprocess for screen capture."""

    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._output_path: str | None = None
        self._frag_path: str | None = None
        self._lock = threading.Lock()
        self._monitor_thread: threading.Thread | None = None

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    @property
    def output_path(self) -> str | None:
        return self._output_path

    @property
    def live_video_path(self) -> str | None:
        if self.is_recording and self._frag_path and Path(self._frag_path).exists():
            return self._frag_path
        return None

    def start(
        self,
        output_path: str,
        display_index: int = 0,
        framerate: int = 10,
        crf: int = 32,
        preset: str = "ultrafast",
        scale: str = "",
    ) -> None:
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            raise RuntimeError("ffmpeg not found - install it or restart the app to auto-download")

        with self._lock:
            if self._proc and self._proc.poll() is None:
                raise RuntimeError("Already recording")

        displays = enumerate_displays()
        if display_index < 0 or display_index >= len(displays):
            display_index = 0
        disp = displays[display_index]

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        self._output_path = output_path

        cap_x = disp["x"]
        cap_y = disp["y"]
        cap_w = disp["width"]
        cap_h = disp["height"]

        log.info("screen", f"Display {display_index}: {cap_w}x{cap_h} physical "
                 f"(scale={disp['scale']:.2f})")

        cmd = [
            ffmpeg,
            "-y",
            "-f", "gdigrab",
            "-framerate", str(framerate),
            "-offset_x", str(cap_x),
            "-offset_y", str(cap_y),
            "-video_size", f"{cap_w}x{cap_h}",
            "-draw_mouse", "1",
            "-i", "desktop",
        ]

        vf_parts = []
        if scale:
            vf_parts.append(f"scale={scale}")
        if vf_parts:
            cmd.extend(["-vf", ",".join(vf_parts)])

        self._frag_path = output_path + ".frag.mp4"

        cmd.extend([
            "-c:v", "libx264",
            "-preset", preset,
            "-crf", str(crf),
            "-pix_fmt", "yuv420p",
            "-an",
            "-movflags", "frag_keyframe+empty_moov",
            self._frag_path,
        ])

        log.info("screen", f"Starting: {' '.join(cmd)}")

        with self._lock:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess_no_window_flag(),
            )

        self._monitor_thread = threading.Thread(target=self._monitor, daemon=True)
        self._monitor_thread.start()

        log.info("screen", f"Recording display {display_index} → {output_path}")

    def _monitor(self):
        proc = self._proc
        if not proc or not proc.stderr:
            return
        try:
            for line in proc.stderr:
                pass
        except Exception:
            pass

    def stop(self) -> str | None:
        with self._lock:
            proc = self._proc
            self._proc = None

        if not proc:
            return None

        try:
            proc.stdin.write(b"q")
            proc.stdin.flush()
        except (OSError, BrokenPipeError):
            pass

        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            log.warn("screen", "ffmpeg did not exit in time - killing")
            proc.kill()
            proc.wait(timeout=5)

        final_path = self._output_path
        frag_path = getattr(self, "_frag_path", None)

        if not frag_path or not Path(frag_path).exists() or Path(frag_path).stat().st_size == 0:
            log.warn("screen", "Recording file is missing or empty")
            return None

        ffmpeg = find_ffmpeg()
        if ffmpeg and final_path:
            try:
                remux = subprocess.run(
                    [ffmpeg, "-y", "-i", frag_path,
                     "-c", "copy", "-movflags", "+faststart", final_path],
                    capture_output=True, timeout=60,
                    creationflags=subprocess_no_window_flag(),
                )
                if remux.returncode == 0 and Path(final_path).exists():
                    Path(frag_path).unlink(missing_ok=True)
                    size_mb = Path(final_path).stat().st_size / (1024 * 1024)
                    log.info("screen", f"Saved: {final_path} ({size_mb:.1f} MB)")
                    return final_path
                else:
                    log.warn("screen", "Remux failed - keeping fragmented file")
            except Exception as e:
                log.warn("screen", f"Remux error: {e} - keeping fragmented file")

        try:
            Path(frag_path).rename(final_path)
        except OSError:
            final_path = frag_path
        size_mb = Path(final_path).stat().st_size / (1024 * 1024)
        log.info("screen", f"Saved: {final_path} ({size_mb:.1f} MB)")
        return final_path


def capture_live_frame(display_index: int = 0, max_width: int = 960) -> bytes | None:
    """Capture a single JPEG screenshot from the specified display via gdigrab."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None

    displays = enumerate_displays()
    if display_index < 0 or display_index >= len(displays):
        display_index = 0
    disp = displays[display_index]

    cap_x = disp["x"]
    cap_y = disp["y"]
    cap_w = disp["width"]
    cap_h = disp["height"]

    cmd = [
        ffmpeg,
        "-f", "gdigrab",
        "-framerate", "1",
        "-offset_x", str(cap_x),
        "-offset_y", str(cap_y),
        "-video_size", f"{cap_w}x{cap_h}",
        "-i", "desktop",
        "-frames:v", "1",
        "-vf", f"scale='min({max_width},iw)':-2",
        "-q:v", "5",
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "pipe:1",
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=5,
            creationflags=subprocess_no_window_flag(),
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout
    except Exception:
        pass
    return None
