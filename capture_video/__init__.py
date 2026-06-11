"""Screen recorder dispatcher — picks the platform backend at import time.

Public API (re-exported, must not change):
- ScreenRecorder         class       (start/stop/properties)
- enumerate_displays()   function    list of display dicts
- flash_display_border() function    visual ping for a display
- capture_live_frame()   function    JPEG snapshot of a display
- extract_frame()        function    JPEG snapshot from a recorded video
- find_ffmpeg()          function    path to ffmpeg binary
- download_ffmpeg()      function    download static build into tools/
- kill_stale_ffmpeg()    function    cleanup orphan processes
- PRESETS                dict        recording quality presets
- DEFAULT_PRESET         str         default preset id
- H264_PRESETS           list        encoder speed presets

Backend selection is automatic via sys.platform — no config flags, no env
vars. App code only ever imports from `screen_recorder`; the per-OS modules
(`screen_recorder_win`, `screen_recorder_mac`) are private.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from core import log as log

# Cross-platform ffmpeg helpers (re-exported).
from capture_video.ffmpeg_util import (
    find_ffmpeg,
    download_ffmpeg,
    kill_stale_ffmpeg,
    FFMPEG_DOWNLOAD_URL,
    _LOCAL_FFMPEG,
    subprocess_no_window_flag as _no_window_flag,
)

# ── Platform backend ─────────────────────────────────────────────────────────
if sys.platform == "win32":
    from capture_video.windows import (
        ScreenRecorder,
        enumerate_displays,
        flash_display_border,
        capture_live_frame,
    )
elif sys.platform == "darwin":
    from capture_video.mac import (
        ScreenRecorder,
        enumerate_displays,
        flash_display_border,
        capture_live_frame,
    )
else:
    # Linux / other: stub so imports succeed; runtime ops raise.
    class ScreenRecorder:  # type: ignore[no-redef]
        is_recording = False
        output_path = None
        live_video_path = None

        def start(self, *a, **kw):
            raise NotImplementedError(f"Screen recording not implemented for {sys.platform}")

        def stop(self):
            return None

    def enumerate_displays() -> list[dict]:
        return []

    def flash_display_border(*a, **kw):
        pass

    def capture_live_frame(*a, **kw) -> bytes | None:
        return None


# ── Recording presets (cross-platform) ───────────────────────────────────────

PRESETS = {
    "minimal": {
        "label": "Minimal",
        "description": "Lowest resource usage - small files, reduced clarity",
        "framerate": 5,
        "crf": 38,
        "preset": "ultrafast",
        "scale": "1280:-2",
    },
    "performance": {
        "label": "Performance (Default)",
        "description": "Low CPU usage with decent quality",
        "framerate": 10,
        "crf": 32,
        "preset": "ultrafast",
        "scale": "",
    },
    "balanced": {
        "label": "Balanced",
        "description": "Good quality with moderate CPU usage",
        "framerate": 15,
        "crf": 26,
        "preset": "veryfast",
        "scale": "",
    },
    "quality": {
        "label": "Quality",
        "description": "High quality - larger files, more CPU",
        "framerate": 24,
        "crf": 22,
        "preset": "fast",
        "scale": "",
    },
    "maximum": {
        "label": "Maximum",
        "description": "Best possible quality - significant CPU usage",
        "framerate": 30,
        "crf": 18,
        "preset": "medium",
        "scale": "",
    },
    "custom": {
        "label": "Custom",
        "description": "Manually configure all parameters",
        "framerate": 10,
        "crf": 32,
        "preset": "ultrafast",
        "scale": "",
    },
}

DEFAULT_PRESET = "performance"

# H.264 encoder presets ordered from fastest to slowest
H264_PRESETS = [
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow",
]


# ── extract_frame (cross-platform: reads any MP4) ────────────────────────────

def extract_frame(video_path: str, timestamp: float, max_width: int = 1280) -> bytes | None:
    """Extract a single JPEG frame from an MP4 at the given timestamp (seconds).

    Platform-agnostic: just runs `ffmpeg -ss <ts> -i <video>`. Returns JPEG
    bytes or None on failure. Downscales to max_width for efficient inclusion
    in LLM context.
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None
    if not Path(video_path).exists():
        return None

    h = int(timestamp // 3600)
    m = int((timestamp % 3600) // 60)
    s = timestamp % 60
    ts_str = f"{h:02d}:{m:02d}:{s:06.3f}"

    cmd = [
        ffmpeg,
        "-ss", ts_str,
        "-i", video_path,
        "-frames:v", "1",
        "-vf", f"scale='min({max_width},iw)':-2",
        "-q:v", "3",
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "pipe:1",
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=10,
            creationflags=_no_window_flag(),
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout
    except Exception:
        pass
    return None
