"""Audio capture dispatcher — picks the platform backend at import time.

Public API (re-exported, must match audio_capture_win.py and audio_capture_mac.py):
- AudioCapture                  class
- enumerate_audio_devices()     -> {"loopback": [...], "input": [...]}
- enumerate_dshow_audio_devices() -> [{"name": "..."}]   (avfoundation list on macOS)
- auto_detect_devices()         -> {"best_loopback", "best_mic", "loopback", "mic"}

The DSP/mixer logic is intentionally duplicated between the per-OS backends
so each can be developed and tested in isolation. They share zero stream-I/O
code; only the public surface is required to match.
"""
from __future__ import annotations

import sys

if sys.platform == "win32":
    from capture_audio.windows import (
        AudioCapture,
        enumerate_audio_devices,
        enumerate_dshow_audio_devices,
        auto_detect_devices,
    )
elif sys.platform == "darwin":
    from capture_audio.mac import (
        AudioCapture,
        enumerate_audio_devices,
        enumerate_dshow_audio_devices,
        auto_detect_devices,
    )
else:
    raise ImportError(
        f"Audio capture has no backend for platform '{sys.platform}'. "
        f"Supported: win32, darwin."
    )

__all__ = [
    "AudioCapture",
    "enumerate_audio_devices",
    "enumerate_dshow_audio_devices",
    "auto_detect_devices",
]
