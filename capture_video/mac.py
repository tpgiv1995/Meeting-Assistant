"""macOS screen recorder backend: FFmpeg avfoundation + Quartz CGGetActiveDisplayList.

Public API mirrors screen_recorder_win.py exactly so the dispatcher can
import either backend transparently.

Display indexing model:
- Quartz returns CGDirectDisplayIDs that we list in deterministic order
  (main display first, then by display ID).
- avfoundation indexes displays separately ("Capture screen 0", "Capture
  screen 1", ...). On a single-display Mac these align; on multi-display
  setups we map our display_index to the avfoundation index by querying
  the avfoundation device list at start time.
"""
from __future__ import annotations

import re
import subprocess
import threading
from pathlib import Path

from core import log as log
from capture_video.ffmpeg_util import find_ffmpeg


# ── Display enumeration via Quartz ──────────────────────────────────────────

def _list_quartz_displays() -> list[dict]:
    """Return display info using Quartz (CoreGraphics) APIs via PyObjC."""
    try:
        from Quartz import (  # type: ignore[import-not-found]
            CGGetActiveDisplayList,
            CGDisplayBounds,
            CGDisplayPixelsWide,
            CGDisplayPixelsHigh,
            CGMainDisplayID,
        )
    except ImportError:
        log.warn("screen", "pyobjc-framework-Quartz not installed; cannot enumerate displays")
        return []

    # Query active display IDs (max 32 displays, plenty in practice)
    err, display_ids, count = CGGetActiveDisplayList(32, None, None)
    if err != 0 or not display_ids:
        return []

    main_id = CGMainDisplayID()
    # Main display first, then the rest by ID for stable ordering.
    ordered = [main_id] + [d for d in display_ids if d != main_id]

    displays: list[dict] = []
    for idx, did in enumerate(ordered):
        bounds = CGDisplayBounds(did)
        # Logical (point) coordinates from CGDisplayBounds origin/size.
        # Physical pixel size from CGDisplayPixelsWide/High.
        logical_w = int(bounds.size.width)
        logical_h = int(bounds.size.height)
        logical_x = int(bounds.origin.x)
        logical_y = int(bounds.origin.y)
        phys_w = int(CGDisplayPixelsWide(did))
        phys_h = int(CGDisplayPixelsHigh(did))
        scale = (phys_w / logical_w) if logical_w else 1.0

        is_primary = (did == main_id)
        suffix = " (Primary)" if is_primary else ""

        displays.append({
            "index": idx,
            "name": f"Display {idx + 1}",
            "x": logical_x,
            "y": logical_y,
            "width": phys_w,
            "height": phys_h,
            "logical_x": logical_x,
            "logical_y": logical_y,
            "logical_width": logical_w,
            "logical_height": logical_h,
            "dpi": int(96 * scale),
            "scale": float(scale),
            "primary": is_primary,
            "label": f"Display {idx + 1}: {phys_w}x{phys_h}{suffix}",
            # macOS-only: CG display ID, used to map to avfoundation index.
            "_cg_display_id": int(did),
        })
    return displays


def enumerate_displays() -> list[dict]:
    """Public API: list displays in dispatcher-compatible dict shape."""
    return _list_quartz_displays()


# ── avfoundation device-index discovery ─────────────────────────────────────

def _avfoundation_screen_indexes() -> dict[int, int]:
    """Map Quartz display order index -> avfoundation video device index.

    ffmpeg's `-f avfoundation -list_devices true` enumerates "Capture screen 0",
    "Capture screen 1", etc. The order matches Quartz's display order on every
    macOS version we've tested, so we just identity-map by enumeration position.
    Returned dict keys are our display_index values, values are the avfoundation
    device indices to pass after `-i`.
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return {}

    try:
        r = subprocess.run(
            [ffmpeg, "-hide_banner", "-f", "avfoundation",
             "-list_devices", "true", "-i", ""],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as e:
        log.warn("screen", f"avfoundation device list failed: {e}")
        return {}

    # ffmpeg writes the device list to stderr.
    text = r.stderr or ""
    screens: list[int] = []
    in_video = False
    for line in text.splitlines():
        if "AVFoundation video devices" in line:
            in_video = True
            continue
        if "AVFoundation audio devices" in line:
            in_video = False
            continue
        if not in_video:
            continue
        m = re.search(r"\[(\d+)\]\s*Capture screen\s*(\d+)", line)
        if m:
            screens.append(int(m.group(1)))

    return {our_idx: av_idx for our_idx, av_idx in enumerate(screens)}


# ── flash_display_border (no-op on Mac for v1) ──────────────────────────────

def flash_display_border(display_index: int, duration_ms: int = 1500, thickness: int = 6):
    """Flash a colored border around a display.

    macOS implementation deferred — would require an NSWindow overlay with
    transparent borderless styleMask, which is non-trivial to set up from
    a subprocess. The display picker UI works without this hint on Mac.
    """
    log.info("screen", "flash_display_border: not implemented on macOS (no-op)")


# ── Screen recorder ─────────────────────────────────────────────────────────

class ScreenRecorder:
    """Manages an ffmpeg avfoundation subprocess for screen capture."""

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
            raise RuntimeError(
                "ffmpeg not found - install via 'brew install ffmpeg' or restart "
                "the app to auto-download"
            )

        with self._lock:
            if self._proc and self._proc.poll() is None:
                raise RuntimeError("Already recording")

        displays = enumerate_displays()
        if not displays:
            raise RuntimeError("No displays detected")
        if display_index < 0 or display_index >= len(displays):
            display_index = 0
        disp = displays[display_index]

        # Map our display index to avfoundation's screen device index.
        av_map = _avfoundation_screen_indexes()
        av_idx = av_map.get(display_index, display_index)

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        self._output_path = output_path

        log.info("screen", f"Display {display_index}: {disp['width']}x{disp['height']} "
                 f"(scale={disp['scale']:.2f}, av_idx={av_idx})")

        # avfoundation: "<video_idx>:<audio_idx>" — use 'none' for audio so we
        # only capture video; audio goes through audio_capture.py separately.
        cmd = [
            ffmpeg,
            "-y",
            "-f", "avfoundation",
            "-framerate", str(framerate),
            "-capture_cursor", "1",
            "-i", f"{av_idx}:none",
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
            )

        self._monitor_thread = threading.Thread(target=self._monitor, daemon=True)
        self._monitor_thread.start()

        log.info("screen", f"Recording display {display_index} → {output_path}")

    def _monitor(self):
        proc = self._proc
        if not proc or not proc.stderr:
            return
        try:
            for _line in proc.stderr:
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
    """Capture a single JPEG screenshot from the specified display via avfoundation."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None

    displays = enumerate_displays()
    if not displays:
        return None
    if display_index < 0 or display_index >= len(displays):
        display_index = 0

    av_map = _avfoundation_screen_indexes()
    av_idx = av_map.get(display_index, display_index)

    cmd = [
        ffmpeg,
        "-f", "avfoundation",
        "-framerate", "1",
        "-capture_cursor", "1",
        "-i", f"{av_idx}:none",
        "-frames:v", "1",
        "-vf", f"scale='min({max_width},iw)':-2",
        "-q:v", "5",
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "pipe:1",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=5)
        if result.returncode == 0 and result.stdout:
            return result.stdout
    except Exception:
        pass
    return None
