"""
Desktop audio capture using WASAPI loopback (Windows only).
Captures system audio output (loopback) AND the default microphone input,
mixing both streams into a single mono feed for transcription.
"""
import collections
import os
import queue
import re
import subprocess
import threading
import time
import traceback
from math import gcd

import numpy as np
import pyaudiowpatch as pyaudio
from scipy.signal import resample_poly

from core import log as log
from capture_audio.wav_writer import WavWriter

# ── INPUT_DEBUG ──────────────────────────────────────────────────────────
# Verbose tracing of every input stream and the mixer that joins them.
# Enable by setting the env var INPUT_DEBUG=1 before launching the app
# (or flip the default below to True for a permanent on-state during
# debugging). Output is throttled per metric to ~once per second so the
# log stays readable. Leaves no stone unturned: per-chunk byte counts,
# RMS / peak, queue depths, ffmpeg stderr in real time, mixer routing
# decisions, AEC state, and gating reasons.
INPUT_DEBUG = os.environ.get("INPUT_DEBUG", "0").strip() not in ("", "0", "false", "False")


def _idbg(msg: str) -> None:
    if INPUT_DEBUG:
        log.info("input-debug", msg)


class _Throttle:
    """Per-key one-line-per-interval throttle for INPUT_DEBUG output."""
    def __init__(self, interval: float = 1.0):
        self.interval = interval
        self._last: dict[str, float] = {}

    def ready(self, key: str) -> bool:
        if not INPUT_DEBUG:
            return False
        now = time.monotonic()
        if now - self._last.get(key, 0.0) >= self.interval:
            self._last[key] = now
            return True
        return False

    def reset(self, key: str | None = None) -> None:
        if key is None:
            self._last.clear()
        else:
            self._last.pop(key, None)

# FFT window size for the spectrum visualizer.  2048 samples ≈ 43 ms at 48 kHz,
# giving ~23 Hz frequency resolution.  The deque keeps the most recent window
# and is refilled by the mixer loop at ~512 samples per chunk.
_FFT_SIZE = 4096
_N_BARS   = 32   # number of log-spaced frequency bands sent to the frontend

# On Windows, calling Pa_StopStream / Pa_CloseStream on a WASAPI loopback stream
# invokes ExitProcess() at the C level and kills the entire Python process.
# We work around this by parking retired stream objects and their PyAudio instances
# here so Python GC never calls __del__ → close() on them.  The PortAudio atexit
# handler (registered automatically when PyAudio() is constructed) will clean up
# all open streams/handles when the process exits normally.
_stream_graveyard: list = []


class AudioCapture:
    CHUNK_SIZE = 512
    FORMAT = pyaudio.paInt16

    def __init__(self, audio_queue: queue.Queue):
        self.audio_queue = audio_queue
        self.is_running = False
        self._pa: pyaudio.PyAudio | None = None
        self._loopback_stream = None
        self._mic_stream = None
        self._loopback_thread: threading.Thread | None = None
        self._mic_thread: threading.Thread | None = None
        self._mixer_thread: threading.Thread | None = None

        # Reported to Transcriber - always mono after mixing
        self.sample_rate: int | None = None
        self.channels: int = 1

        # Internal source queues
        self._loopback_q: queue.Queue = queue.Queue(maxsize=200)
        self._mic_q: queue.Queue = queue.Queue(maxsize=200)

        # Per-stream properties (set in start())
        self._loopback_channels: int = 1
        self._mic_rate: int | None = None
        self._mic_channels: int = 1
        self._has_mic: bool = False
        self._mic_buf_size: int = 512
        self._resample_up: int = 1
        self._resample_down: int = 1

        # WAV writer - set via start_wav() before start()
        self.wav_writer: WavWriter | None = None
        self._wav_path: str | None = None
        self._wav_append: bool = False

        # Live RMS levels - read by app.py to push to the visualizer
        self.loopback_level: float = 0.0
        self.mic_level: float = 0.0

        # Device names + first-valid-audio flags. The capture loops emit a
        # one-shot "Verified audio device ..." log line as soon as a non-zero
        # PCM chunk arrives, so we can tell at-a-glance whether each stream
        # actually produced audio (vs. opening successfully and going silent).
        self._loopback_device_name: str = ""
        self._mic_device_name: str = ""
        self._loopback_verified: bool = False
        self._mic_verified: bool = False

        # User-controlled gain multipliers (1.0 = no change, persisted via localStorage)
        self.loopback_gain: float = 1.0
        self.mic_gain: float = 1.0

        # Echo cancellation (disabled by default - enable for speaker+mic setups)
        self.echo_cancel_enabled: bool = False

        # Automatic gain control (soft compressor / normaliser)
        self.agc_loopback_enabled: bool = True
        self.agc_mic_enabled: bool = True
        self.agc_target_rms: float = 0.15
        self.agc_max_gain: float = 4.0
        self.agc_gate_threshold: float = 0.005

        # Live AGC debug state (read by the level-push loop for the UI)
        self.agc_lb_gain: float = 1.0
        self.agc_lb_envelope: float = 0.0
        self.agc_lb_gated: bool = True
        self.agc_mic_gain: float = 1.0
        self.agc_mic_envelope: float = 0.0
        self.agc_mic_gated: bool = True

        # Rolling sample buffers for the FFT spectrum visualizer (post-gain)
        self._lb_fft_buf:  collections.deque = collections.deque(maxlen=_FFT_SIZE)
        self._mic_fft_buf: collections.deque = collections.deque(maxlen=_FFT_SIZE)
        self._hann_window: np.ndarray | None = None   # precomputed; set on first use

        # FFmpeg subprocess mic capture (mic_index=-3)
        self._ffmpeg_proc: subprocess.Popen | None = None
        self._ffmpeg_mic_name: str | None = None
        self._ffmpeg_stderr_thread: threading.Thread | None = None

        # INPUT_DEBUG bookkeeping. Counters are advanced from the capture
        # threads and the mixer; the throttle ensures we emit summaries at
        # most once per second per key so the log isn't drowned.
        self._idbg_throttle = _Throttle(interval=1.0)
        self._idbg_lb_bytes = 0
        self._idbg_mic_bytes = 0
        self._idbg_lb_chunks = 0
        self._idbg_mic_chunks = 0
        self._idbg_lb_zero_chunks = 0
        self._idbg_mic_zero_chunks = 0
        self._idbg_mic_inject_bytes = 0
        self._idbg_mix_src_counts: dict[str, int] = {"loopback": 0, "mic": 0, "both": 0}
        self._idbg_mix_emitted = 0
        self._idbg_audio_q_full_drops = 0
        self._idbg_mic_q_full_drops = 0
        self._idbg_lb_q_full_drops = 0

    # ── Device discovery ──────────────────────────────────────────────────────

    @staticmethod
    def _compute_mic_buffer_size(mic_info: dict) -> int:
        """Compute a frames_per_buffer for the mic aligned with the WASAPI device period.

        WASAPI shared mode delivers data in chunks tied to the device's period
        (typically 10 ms).  A too-small buffer causes underruns/glitches because
        PortAudio's internal ring buffer can't bridge the timing gap reliably.
        We derive a safe size from the device's reported high-input latency and
        round up to the next power of two (required by some drivers, and always
        safe for FFT-friendly alignment).
        """
        rate = int(mic_info["defaultSampleRate"])
        latency = mic_info.get("defaultHighInputLatency", 0.02)
        frames = int(rate * latency)
        frames = max(1024, min(frames, 8192))
        power = 1
        while power < frames:
            power <<= 1
        return power

    def _find_loopback_device(self) -> dict:
        """
        Find the WASAPI loopback device for the current default audio output.
        Falls back gracefully when device names are truncated or don't match exactly.
        """
        wasapi_info = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_output = self._pa.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
        default_name: str = default_output["name"]

        all_loopbacks = list(self._pa.get_loopback_device_info_generator())
        if not all_loopbacks:
            raise RuntimeError(
                "No WASAPI loopback devices found. "
                "Make sure your audio driver supports WASAPI loopback capture."
            )

        # 1. Exact substring match (the common case)
        for lb in all_loopbacks:
            if default_name in lb["name"] or lb["name"].startswith(default_name):
                return lb

        # 2. Prefix match - Windows can truncate long device names differently
        #    for the output vs its loopback counterpart
        prefix = default_name[:20]
        for lb in all_loopbacks:
            if prefix and prefix in lb["name"]:
                return lb

        # 3. Word-level match - e.g. "USB Audio" appears in both names
        words = [w for w in default_name.split() if len(w) >= 4]
        for lb in all_loopbacks:
            if any(w in lb["name"] for w in words):
                return lb

        # 4. Last resort: first available loopback device
        log.warn("audio", f"No loopback device matched '{default_name}'. "
                          f"Using '{all_loopbacks[0]['name']}' as fallback.")
        return all_loopbacks[0]

    def _find_mic_device(self) -> dict | None:
        """Find the system default microphone input device (WASAPI only)."""
        try:
            wasapi_idx = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)["index"]
        except Exception:
            wasapi_idx = None

        # Collect loopback indices so we never accidentally pick one as the mic
        try:
            loopback_indices = {
                int(d["index"]) for d in self._pa.get_loopback_device_info_generator()
            }
        except Exception:
            loopback_indices = set()

        # Prefer the WASAPI default input device
        try:
            wasapi_info = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            default_idx = wasapi_info.get("defaultInputDevice", -1)
            if default_idx >= 0:
                info = self._pa.get_device_info_by_index(default_idx)
                if (info.get("maxInputChannels", 0) > 0
                        and int(info["index"]) not in loopback_indices):
                    return info
        except Exception:
            pass

        # Fallback: first WASAPI input that isn't a loopback
        try:
            for i in range(self._pa.get_device_count()):
                info = self._pa.get_device_info_by_index(i)
                if wasapi_idx is not None and info.get("hostApi") != wasapi_idx:
                    continue
                if info.get("maxInputChannels", 0) <= 0:
                    continue
                if int(info["index"]) in loopback_indices:
                    continue
                if "[Loopback]" in info.get("name", ""):
                    continue
                return info
        except Exception:
            pass

        return None

    # ── WAV recording ──────────────────────────────────────────────────────

    def start_wav(self, path: str, append: bool = False) -> None:
        """Request WAV recording.  Call before start().

        The actual WavWriter is created inside start() once the sample rate
        is known from the loopback device.
        """
        self._wav_path = path
        self._wav_append = append

    def stop_wav(self) -> None:
        """Finalize and close the WAV file.  Safe to call multiple times."""
        if self.wav_writer is not None:
            self.wav_writer.close()
            self.wav_writer = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self, loopback_index: int | None = None, mic_index: int | None = None,
              ffmpeg_mic_name: str | None = None) -> None:
        """
        Start capture.  loopback_index / mic_index override auto-detection;
        pass mic_index=-1 to explicitly disable the microphone,
        mic_index=-2 to receive mic audio injected from the browser
        (via inject_mic_data()), or mic_index=-3 to capture via an ffmpeg
        subprocess using DirectShow (requires ffmpeg_mic_name).
        """
        self._pa = pyaudio.PyAudio()

        # --- Loopback stream (required) ---
        if loopback_index is not None:
            lb_info = self._pa.get_device_info_by_index(loopback_index)
        else:
            lb_info = self._find_loopback_device()
        self.sample_rate = int(lb_info["defaultSampleRate"])
        self._loopback_channels = max(1, lb_info["maxInputChannels"])
        self._loopback_device_name = lb_info["name"]
        self._loopback_verified = False
        self._mic_verified = False
        if INPUT_DEBUG:
            log.info("input-debug", "INPUT_DEBUG enabled - verbose audio tracing on")
            log.info("input-debug",
                     f"loopback_index={loopback_index!r} mic_index={mic_index!r} "
                     f"ffmpeg_mic_name={ffmpeg_mic_name!r}")
            log.info("input-debug",
                     f"loopback dev: name='{lb_info['name']}' index={lb_info.get('index')} "
                     f"defaultSampleRate={lb_info.get('defaultSampleRate')} "
                     f"maxIn={lb_info.get('maxInputChannels')} "
                     f"hostApi={lb_info.get('hostApi')}")
            self._idbg_throttle.reset()
            self._idbg_lb_bytes = self._idbg_mic_bytes = 0
            self._idbg_lb_chunks = self._idbg_mic_chunks = 0
            self._idbg_lb_zero_chunks = self._idbg_mic_zero_chunks = 0
            self._idbg_mic_inject_bytes = 0
            self._idbg_mix_src_counts = {"loopback": 0, "mic": 0, "both": 0}
            self._idbg_mix_emitted = 0
            self._idbg_audio_q_full_drops = 0
            self._idbg_mic_q_full_drops = 0
            self._idbg_lb_q_full_drops = 0
        log.info("audio", f"Loopback: '{lb_info['name']}' @ {self.sample_rate} Hz, "
                          f"{self._loopback_channels} ch")
        self._loopback_stream = self._pa.open(
            format=self.FORMAT,
            channels=self._loopback_channels,
            rate=self.sample_rate,
            input=True,
            input_device_index=lb_info["index"],
            frames_per_buffer=self.CHUNK_SIZE,
        )

        # --- Microphone stream (best-effort) ---
        if mic_index == -3:
            # FFmpeg subprocess mic via DirectShow - completely independent of
            # Python/WASAPI audio stack for maximum reliability.
            from capture_video import find_ffmpeg
            ffmpeg_path = find_ffmpeg()
            if not ffmpeg_path:
                log.warn("audio", "ffmpeg not found - cannot use FFmpeg mic capture")
                mic_info = None
            elif not ffmpeg_mic_name:
                log.warn("audio", "No DirectShow mic device name provided for ffmpeg capture")
                mic_info = None
            else:
                # Re-resolve the saved name against the live dshow device list.
                # The friendly name we persisted may have shifted (driver update,
                # USB re-enumeration) or the device may be gone entirely. Doing
                # this here — instead of trusting the caller's stale string —
                # turns "ffmpeg silently records nothing" into a clean failure
                # or an automatic retarget onto the same physical device.
                resolved, reason = resolve_dshow_mic_name(ffmpeg_mic_name)
                if resolved is None:
                    log.warn("audio", f"Mic '{ffmpeg_mic_name}' not found in dshow "
                                      f"device list ({reason}) - capturing loopback only")
                    mic_info = None
                else:
                    if resolved != ffmpeg_mic_name:
                        log.info("audio", f"Mic name re-resolved: '{ffmpeg_mic_name}' "
                                          f"-> '{resolved}' ({reason})")
                    ffmpeg_mic_name = resolved
                    self._mic_rate     = 48000
                    self._mic_channels = 1
                    self._has_mic      = True
                    self._ffmpeg_mic_name = ffmpeg_mic_name
                    self._mic_device_name = ffmpeg_mic_name
                    if self._mic_rate != self.sample_rate:
                        g = gcd(self.sample_rate, self._mic_rate)
                        self._resample_up   = self.sample_rate // g
                        self._resample_down = self._mic_rate    // g
                    cmd = [
                        ffmpeg_path,
                        "-f", "dshow",
                        "-rtbufsize", "32k",         # small DirectShow buffer for low latency
                        "-audio_buffer_size", "40",   # dshow audio buffer in ms (default ~500)
                        "-i", f"audio={ffmpeg_mic_name}",
                        "-f", "s16le",
                        "-acodec", "pcm_s16le",
                        "-ar", str(self._mic_rate),
                        "-ac", "1",
                        "-fflags", "+nobuffer",       # minimize internal buffering
                        "-flags", "+low_delay",
                        "-loglevel", "error",
                        "pipe:1",
                    ]
                    log.info("audio", f"Mic: ffmpeg dshow '{ffmpeg_mic_name}' @ {self._mic_rate} Hz, 1 ch")
                    if INPUT_DEBUG:
                        log.info("input-debug", "ffmpeg cmd: " + " ".join(
                            f'"{a}"' if " " in a else a for a in cmd))
                    self._ffmpeg_proc = subprocess.Popen(
                        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    if INPUT_DEBUG:
                        log.info("input-debug",
                                 f"ffmpeg pid={self._ffmpeg_proc.pid} started; "
                                 f"continuous stderr drain thread spawning")
                        self._ffmpeg_stderr_thread = threading.Thread(
                            target=self._ffmpeg_stderr_drain,
                            args=(self._ffmpeg_proc,),
                            daemon=True,
                        )
                        self._ffmpeg_stderr_thread.start()
                    mic_info = None   # skip the WASAPI-open block below
        elif mic_index == -2:
            # Browser mic - no WASAPI stream; audio arrives via inject_mic_data()
            self._mic_rate     = 48000   # browser AudioContext default
            self._mic_channels = 1
            self._has_mic      = True
            self._mic_device_name = "browser (getUserMedia)"
            if self._mic_rate != self.sample_rate:
                g = gcd(self.sample_rate, self._mic_rate)
                self._resample_up   = self.sample_rate // g
                self._resample_down = self._mic_rate    // g
            log.info("audio", f"Mic: browser (inject_mic_data) @ {self._mic_rate} Hz, 1 ch")
            mic_info = None   # skip the WASAPI-open block below
        elif mic_index == -1:
            mic_info = None   # explicitly disabled by caller
        elif mic_index is not None:
            try:
                mic_info = self._pa.get_device_info_by_index(mic_index)
            except Exception as e:
                log.warn("audio", f"Specified mic device {mic_index} invalid: {e}")
                mic_info = None
        else:
            mic_info = self._find_mic_device()
        if mic_info:
            try:
                self._mic_rate = int(mic_info["defaultSampleRate"])
                self._mic_channels = max(1, mic_info["maxInputChannels"])
                self._mic_buf_size = self._compute_mic_buffer_size(mic_info)
                self._mic_stream = self._pa.open(
                    format=self.FORMAT,
                    channels=self._mic_channels,
                    rate=self._mic_rate,
                    input=True,
                    input_device_index=mic_info["index"],
                    frames_per_buffer=self._mic_buf_size,
                )
                self._has_mic = True
                self._mic_device_name = mic_info["name"]
                if self._mic_rate != self.sample_rate:
                    g = gcd(self.sample_rate, self._mic_rate)
                    self._resample_up = self.sample_rate // g
                    self._resample_down = self._mic_rate // g
                log.info("audio", f"Mic: '{mic_info['name']}' @ {self._mic_rate} Hz, "
                                  f"{self._mic_channels} ch, buf={self._mic_buf_size}")
            except Exception as e:
                log.warn("audio", f"Mic unavailable: {e}")
                self._mic_stream = None
                self._has_mic = False
        elif mic_index not in (-2, -3):
            # Neither -2 (browser) nor -3 (ffmpeg) nor a valid WASAPI mic
            self._has_mic = False
            if mic_index == -1:
                log.info("audio", "Microphone explicitly disabled - capturing loopback only.")
            else:
                log.info("audio", "No microphone device found - capturing loopback only.")

        # Open WAV writer now that sample_rate is known
        if self._wav_path:
            self.wav_writer = WavWriter(self._wav_path, self.sample_rate,
                                        append=self._wav_append)
            self._wav_path = None

        self.is_running = True

        self._loopback_thread = threading.Thread(
            target=self._capture_loop,
            args=(self._loopback_stream, self._loopback_q),
            daemon=True,
        )
        self._loopback_thread.start()

        if self._has_mic and self._ffmpeg_proc is not None:
            # FFmpeg subprocess mic - read raw PCM from stdout
            self._mic_thread = threading.Thread(
                target=self._ffmpeg_capture_loop,
                daemon=True,
            )
            self._mic_thread.start()
        elif self._has_mic and self._mic_stream is not None:
            # WASAPI mic stream.
            # NOTE: Do NOT pass _mic_buf_size here.  _mic_buf_size is the
            # frames_per_buffer for the WASAPI stream's internal ring buffer
            # (large = prevents underruns).  The *read* chunk size must stay
            # at CHUNK_SIZE (512) so mic data flows at the same cadence as
            # loopback and the mixer can interleave them without gaps.
            self._mic_thread = threading.Thread(
                target=self._capture_loop,
                args=(self._mic_stream, self._mic_q),
                daemon=True,
            )
            self._mic_thread.start()

        self._mixer_thread = threading.Thread(target=self._mixer_loop, daemon=True)
        self._mixer_thread.start()

    def stop(self) -> None:
        self.is_running = False
        # Terminate ffmpeg subprocess so the capture thread unblocks on stdout.read()
        if self._ffmpeg_proc is not None:
            try:
                self._ffmpeg_proc.terminate()
            except Exception:
                pass
        # Wait for the capture and mixer threads to finish their current iteration
        # and exit naturally (they check is_running at the top of every loop).
        # Loopback/mic streams always have data so stream.read() returns quickly.
        for t in (self._loopback_thread, self._mic_thread, self._mixer_thread):
            if t:
                t.join(timeout=3)
        self._loopback_thread = None
        self._mic_thread = None
        self._mixer_thread = None
        # Finalize WAV *after* the mixer thread has stopped - calling stop_wav()
        # while the mixer is still running is a race condition that can corrupt
        # the file or crash on a write to a closed handle.
        self.stop_wav()
        # The microphone stream is a normal WASAPI *input* stream, so closing it
        # is safe and releases the device immediately — clearing the taskbar
        # "microphone in use" indicator the moment recording stops. (The
        # ExitProcess-on-close crash is specific to WASAPI *loopback* streams;
        # a regular input stream closes cleanly.) Threads have already joined
        # above, so nothing is reading from it.
        if self._mic_stream is not None:
            try:
                self._mic_stream.stop_stream()
                self._mic_stream.close()
            except Exception:
                pass
            self._mic_stream = None
        # Park the loopback stream + PyAudio instance in the graveyard instead
        # of closing them. Pa_StopStream / Pa_CloseStream on a WASAPI loopback
        # stream calls ExitProcess() at the C level on Windows, killing the
        # whole process. Setting it to None would also trigger __del__ →
        # close(), so we keep a live reference here and let PortAudio's own
        # atexit handler clean up on exit.
        if self._loopback_stream is not None:
            _stream_graveyard.append(self._loopback_stream)
        if self._pa is not None:
            _stream_graveyard.append(self._pa)
        self._loopback_stream = None
        self._mic_stream = None
        self._ffmpeg_proc = None
        self._pa = None

    def compute_spectrum(self, buf: collections.deque) -> list[float]:
        """Return _N_BARS log-spaced frequency magnitudes from the sample buffer.

        Uses a Hann-windowed real FFT on the most recent _FFT_SIZE samples.
        Values are normalised to [0, 1] on a power-law scale suitable for display.
        Returns all-zeros if the buffer is too short.
        """
        if len(buf) < _FFT_SIZE // 4:
            return [0.0] * _N_BARS

        samples = np.array(buf, dtype=np.float32)
        n = len(samples)

        if self._hann_window is None or len(self._hann_window) != n:
            self._hann_window = np.hanning(n).astype(np.float32)

        windowed = samples * self._hann_window
        # Zero-pad to _FFT_SIZE so low-frequency bins always have enough
        # resolution (~11.7 Hz at 4096/48 kHz) regardless of buffer fill.
        padded = windowed if n >= _FFT_SIZE else np.pad(windowed, (0, _FFT_SIZE - n))
        fft_mag  = np.abs(np.fft.rfft(padded)) / (n * 0.5)   # normalise by window area
        freqs    = np.fft.rfftfreq(len(padded), d=1.0 / (self.sample_rate or 48000))

        f_min  = 40.0
        f_max  = min(20000.0, (self.sample_rate or 48000) / 2.0)
        edges  = np.logspace(np.log10(f_min), np.log10(f_max), _N_BARS + 1)

        result: list[float] = []
        for i in range(_N_BARS):
            mask = (freqs >= edges[i]) & (freqs < edges[i + 1])
            val  = float(np.mean(fft_mag[mask])) if mask.any() else 0.0
            # Power-law scale so quiet signals are still visible
            result.append(round(min(1.0, (val * 80) ** 0.5), 4))

        return result

    def inject_mic_data(self, data: bytes) -> None:
        """Push raw mono Int16 PCM bytes into the mic pipeline.

        Used by the browser-mic pathway (mic_index=-2): the browser captures
        audio via getUserMedia, converts it to Int16, and POSTs it to
        /api/audio/mic-chunk, which calls this method on the active capture.
        """
        if self.is_running and self._has_mic:
            if not self._mic_verified and data and data.strip(b"\x00"):
                self._mic_verified = True
                log.info("audio", f"Verified audio device "
                                  f"(microphone): {self._mic_device_name}")
            if INPUT_DEBUG:
                nsamp, peak, rms = self._chunk_stats(data)
                self._idbg_mic_inject_bytes += len(data)
                if self._idbg_throttle.ready("inject_mic"):
                    log.info("input-debug",
                             f"mic(inject) rd: bytes_total={self._idbg_mic_inject_bytes} "
                             f"last_n={nsamp} peak={peak} rms={rms:.4f} "
                             f"q={self._mic_q.qsize()}/{self._mic_q.maxsize}")
            try:
                self._mic_q.put_nowait(data)
            except queue.Full:
                if INPUT_DEBUG:
                    self._idbg_mic_q_full_drops += 1
                    if self._idbg_throttle.ready("mic_q_full_inject"):
                        log.warn("input-debug",
                                 f"mic queue FULL on inject — dropped "
                                 f"(total drops={self._idbg_mic_q_full_drops})")

    # ── Capture threads ───────────────────────────────────────────────────────

    def _ffmpeg_stderr_drain(self, proc: subprocess.Popen) -> None:
        """Continuously surface ffmpeg's stderr while INPUT_DEBUG is on.

        Without this, ffmpeg's stderr is only read after the process exits
        (see _ffmpeg_capture_loop's finally block) — which means a silently
        failing dshow capture leaves no breadcrumbs. With INPUT_DEBUG on we
        run this in a side thread so every line lands in the log in real
        time, including non-fatal warnings ffmpeg emits while still alive.
        """
        try:
            while self.is_running and proc and proc.poll() is None:
                line = proc.stderr.readline() if proc.stderr else b""
                if not line:
                    break
                txt = line.decode("utf-8", errors="replace").rstrip()
                if txt:
                    log.info("input-debug", f"ffmpeg[{proc.pid}] {txt}")
        except Exception:
            if self.is_running:
                log.warn("input-debug",
                         f"ffmpeg stderr drain crashed:\n{traceback.format_exc()}")

    @staticmethod
    def _chunk_stats(data: bytes) -> tuple[int, int, float]:
        """Return (n_samples, peak_abs, rms) for an Int16 PCM byte buffer."""
        if not data:
            return 0, 0, 0.0
        arr = np.frombuffer(data, dtype=np.int16)
        if arr.size == 0:
            return 0, 0, 0.0
        peak = int(np.abs(arr).max())
        rms  = float(np.sqrt(np.mean(arr.astype(np.float32) ** 2)))
        return arr.size, peak, rms

    def _capture_loop(self, stream, out_queue: queue.Queue,
                      buf_size: int = 0) -> None:
        chunk = buf_size or self.CHUNK_SIZE
        # Identify which stream this thread is servicing so the one-shot
        # "Verified audio device" log line can name the right device. We
        # compare object identity rather than passing a label arg to keep
        # the existing call sites unchanged.
        is_loopback = stream is self._loopback_stream
        while self.is_running:
            try:
                # Read however many frames WASAPI has ready, clamped to a
                # reasonable range.  This adapts to the device's actual
                # delivery cadence instead of demanding a fixed count that
                # may not align with the WASAPI shared-mode period - the
                # main cause of choppy mic input on Windows.
                avail = stream.get_read_available()
                if avail >= chunk:
                    n = min(avail, chunk * 4)  # cap to avoid huge reads
                else:
                    # Not enough data yet - do a blocking read for one
                    # buffer's worth.  The large frames_per_buffer we
                    # requested when opening the stream means this aligns
                    # with the device period and won't underrun.
                    n = chunk
                data = stream.read(n, exception_on_overflow=False)
                if is_loopback:
                    if not self._loopback_verified and data and data.strip(b"\x00"):
                        self._loopback_verified = True
                        log.info("audio", f"Verified audio device "
                                          f"(desktop/loopback): {self._loopback_device_name}")
                else:
                    if not self._mic_verified and data and data.strip(b"\x00"):
                        self._mic_verified = True
                        log.info("audio", f"Verified audio device "
                                          f"(microphone): {self._mic_device_name}")
                if INPUT_DEBUG:
                    nsamp, peak, rms = self._chunk_stats(data)
                    if is_loopback:
                        self._idbg_lb_bytes += len(data)
                        self._idbg_lb_chunks += 1
                        if peak == 0:
                            self._idbg_lb_zero_chunks += 1
                        if self._idbg_throttle.ready("cap_lb"):
                            log.info("input-debug",
                                     f"loopback rd: chunks={self._idbg_lb_chunks} "
                                     f"bytes={self._idbg_lb_bytes} "
                                     f"zero_chunks={self._idbg_lb_zero_chunks} "
                                     f"last_n={nsamp} peak={peak} rms={rms:.4f} "
                                     f"q={out_queue.qsize()}/{out_queue.maxsize}")
                    else:
                        self._idbg_mic_bytes += len(data)
                        self._idbg_mic_chunks += 1
                        if peak == 0:
                            self._idbg_mic_zero_chunks += 1
                        if self._idbg_throttle.ready("cap_mic"):
                            log.info("input-debug",
                                     f"mic(WASAPI) rd: chunks={self._idbg_mic_chunks} "
                                     f"bytes={self._idbg_mic_bytes} "
                                     f"zero_chunks={self._idbg_mic_zero_chunks} "
                                     f"last_n={nsamp} peak={peak} rms={rms:.4f} "
                                     f"q={out_queue.qsize()}/{out_queue.maxsize}")
                try:
                    out_queue.put_nowait(data)
                except queue.Full:
                    if INPUT_DEBUG:
                        if is_loopback:
                            self._idbg_lb_q_full_drops += 1
                            if self._idbg_throttle.ready("lb_q_full"):
                                log.warn("input-debug",
                                         f"loopback queue FULL — dropped chunk "
                                         f"(total drops={self._idbg_lb_q_full_drops})")
                        else:
                            self._idbg_mic_q_full_drops += 1
                            if self._idbg_throttle.ready("mic_q_full"):
                                log.warn("input-debug",
                                         f"mic queue FULL — dropped chunk "
                                         f"(total drops={self._idbg_mic_q_full_drops})")
            except Exception:
                if not self.is_running:
                    break
                time.sleep(0.01)  # brief pause to avoid a tight error loop

    def _ffmpeg_capture_loop(self) -> None:
        """Read raw PCM from an ffmpeg subprocess capturing via DirectShow."""
        # 512 frames * 2 bytes (Int16) * 1 channel = 1024 bytes per chunk
        read_size = self.CHUNK_SIZE * 2
        proc = self._ffmpeg_proc
        try:
            while self.is_running and proc and proc.poll() is None:
                data = proc.stdout.read(read_size)
                if not data:
                    if INPUT_DEBUG:
                        log.warn("input-debug",
                                 f"ffmpeg stdout returned empty — process "
                                 f"poll={proc.poll() if proc else 'n/a'}")
                    break
                if not self._mic_verified and data.strip(b"\x00"):
                    self._mic_verified = True
                    log.info("audio", f"Verified audio device "
                                      f"(microphone): {self._mic_device_name}")
                if INPUT_DEBUG:
                    nsamp, peak, rms = self._chunk_stats(data)
                    self._idbg_mic_bytes += len(data)
                    self._idbg_mic_chunks += 1
                    if peak == 0:
                        self._idbg_mic_zero_chunks += 1
                    if self._idbg_throttle.ready("cap_mic_ffmpeg"):
                        log.info("input-debug",
                                 f"mic(ffmpeg) rd: chunks={self._idbg_mic_chunks} "
                                 f"bytes={self._idbg_mic_bytes} "
                                 f"zero_chunks={self._idbg_mic_zero_chunks} "
                                 f"last_n={nsamp} peak={peak} rms={rms:.4f} "
                                 f"q={self._mic_q.qsize()}/{self._mic_q.maxsize} "
                                 f"pid={proc.pid} poll={proc.poll()}")
                try:
                    self._mic_q.put_nowait(data)
                except queue.Full:
                    if INPUT_DEBUG:
                        self._idbg_mic_q_full_drops += 1
                        if self._idbg_throttle.ready("mic_q_full_ffmpeg"):
                            log.warn("input-debug",
                                     f"mic queue FULL — dropped chunk "
                                     f"(total drops={self._idbg_mic_q_full_drops})")
        except Exception:
            if self.is_running:
                log.warn("audio", f"ffmpeg mic capture error:\n{traceback.format_exc()}")
        finally:
            # Drain stderr for diagnostics
            if proc and proc.poll() is None:
                proc.terminate()
            if proc:
                try:
                    stderr_out = proc.stderr.read() if proc.stderr else b""
                    if proc.wait(timeout=3) != 0 and stderr_out:
                        log.warn("audio", f"ffmpeg mic exited with code {proc.returncode}: "
                                          f"{stderr_out.decode(errors='replace')[:500]}")
                except Exception:
                    pass

    # ── AGC (automatic gain control) ─────────────────────────────────────────

    @staticmethod
    def _agc_apply(chunk: np.ndarray, envelope: float, target_rms: float,
                   max_gain: float, gate_threshold: float,
                   sample_rate: int) -> tuple[np.ndarray, float, float, bool]:
        """Apply soft automatic gain to a chunk.

        Returns (gained_chunk, new_envelope, applied_gain, is_gated).

        Uses a slow-tracking RMS envelope (fast attack ~50 ms, slow release
        ~1.5 s) to compute a smooth gain multiplier.  Gain is capped at
        *max_gain* and only boosts — signals already above *target_rms* are
        left untouched (gain clamped to 1.0).

        *gate_threshold* is a noise gate: if the envelope is below this level
        the signal is treated as silence/background noise and no boost is applied.
        This prevents amplifying room tone or short noise bursts.
        """
        chunk_rms = float(np.sqrt(np.mean(chunk ** 2)))
        # Envelope time constants (in per-chunk coefficients)
        chunk_dur = len(chunk) / max(sample_rate, 1)
        attack  = 1.0 - np.exp(-chunk_dur / 0.05)   # ~50 ms attack
        release = 1.0 - np.exp(-chunk_dur / 1.5)     # ~1.5 s release
        coeff = attack if chunk_rms > envelope else release
        envelope += coeff * (chunk_rms - envelope)

        # Noise gate: don't boost signals below the gate threshold
        # (silence, room tone, brief noise bursts).
        # Only boost (gain >= 1.0).  If signal is already loud, gain = 1.
        gated = envelope <= gate_threshold
        if not gated and envelope < target_rms:
            gain = min(target_rms / envelope, max_gain)
        else:
            gain = 1.0

        # Transient protection: if the *actual* chunk RMS times the computed
        # gain would overshoot the target, instantly cap the gain so the
        # output stays near target_rms.  This prevents hard-clipping when a
        # loud speaker suddenly jumps in while the envelope is still low.
        if chunk_rms > 1e-6 and chunk_rms * gain > target_rms:
            gain = target_rms / chunk_rms

        return np.clip(chunk * gain, -1.0, 1.0), envelope, gain, gated

    # ── Mixer thread ──────────────────────────────────────────────────────────

    @staticmethod
    def _to_mono_float(data: bytes, channels: int) -> np.ndarray:
        samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)
        return samples

    def _mixer_loop(self) -> None:
        # Use list-based accumulation instead of np.concatenate on every drain.
        # np.concatenate allocates a new array every call and copies all existing
        # data - O(n²) over many calls.  Lists just append pointers, and a single
        # np.concatenate at emit time is bounded by a small number of chunks.
        lb_parts: list[np.ndarray] = []
        lb_len = 0
        mic_parts: list[np.ndarray] = []
        mic_len = 0
        # Cap internal buffers at 3 seconds to prevent unbounded growth if the
        # downstream audio_queue backs up.
        max_buf_samples = int((self.sample_rate or 48000) * 3.0)

        # AGC envelope state (per-source, persists across chunks)
        _agc_lb_env  = 0.0
        _agc_mic_env = 0.0

        # WebRTC AEC state - lazily initialised when echo cancellation is enabled
        _aec_processor = None
        _aec_frame_size = 0
        _aec_mic_buf = np.array([], dtype=np.float32)
        _aec_lb_buf  = np.array([], dtype=np.float32)
        _aec_out_buf = np.array([], dtype=np.float32)

        # ── Wall-clock pacing ────────────────────────────────────────────────
        # Emit exactly one CHUNK_SIZE-sized mixed chunk per wall-clock period
        # (CHUNK_SIZE / sample_rate seconds). Without this, mic and loopback
        # arrive in independent bursts and the mixer emits a fresh chunk for
        # whichever stream's data lands first, producing 2× real-time output
        # when both are active — choppy/distorted audio and audio_queue overrun.
        # With wall-clock pacing, one tick = one chunk = one slice of real time,
        # regardless of whether one or both queues happen to have data right then.
        chunk_dur = self.CHUNK_SIZE / float(self.sample_rate or 48000)
        next_emit_time = 0.0   # set on first available data so we don't emit
                               # a long stretch of silence before audio starts

        while self.is_running:
            try:
                got_data = False

                # Drain loopback queue
                try:
                    while True:
                        data = self._loopback_q.get_nowait()
                        chunk = self._to_mono_float(data, self._loopback_channels)
                        lb_parts.append(chunk)
                        lb_len += len(chunk)
                        got_data = True
                except queue.Empty:
                    pass

                # Drain mic queue (resample to loopback rate if necessary)
                if self._has_mic:
                    try:
                        while True:
                            data = self._mic_q.get_nowait()
                            samples = self._to_mono_float(data, self._mic_channels)
                            if self._resample_up != 1 or self._resample_down != 1:
                                samples = resample_poly(
                                    samples, self._resample_up, self._resample_down
                                ).astype(np.float32)
                            mic_parts.append(samples)
                            mic_len += len(samples)
                            got_data = True
                    except queue.Empty:
                        pass

                # Bootstrap the emit clock the first time data appears, so we
                # don't fire a stretch of silence before either stream has
                # produced anything. Check the parts lists (raw drained data)
                # rather than the buffer here — the buffer hasn't been built yet.
                now = time.monotonic()
                if next_emit_time == 0.0:
                    if lb_len > 0 or mic_len > 0:
                        next_emit_time = now
                    else:
                        time.sleep(0.005)
                        continue

                # If we're behind by more than 0.5s (e.g. system was paused),
                # reset the clock instead of dumping a wall of catch-up audio.
                if now - next_emit_time > 0.5:
                    next_emit_time = now

                # If it's not yet time to emit, sleep and loop. Crucially we
                # do NOT touch the parts lists here — earlier code that
                # concatenated parts into a temporary `mic_buf` on every
                # iteration silently dropped the data on sleep iterations
                # (the buffer went out of scope before being consumed).
                if now < next_emit_time:
                    time.sleep(min(0.002, next_emit_time - now))
                    continue

                # Now we're actually emitting. Flatten the part lists into
                # contiguous arrays for this single-chunk consumption.
                if lb_parts and lb_len >= self.CHUNK_SIZE:
                    lb_buf = np.concatenate(lb_parts)
                    lb_parts.clear()
                    lb_len = 0
                else:
                    lb_buf = np.array([], dtype=np.float32)

                if mic_parts and mic_len >= self.CHUNK_SIZE:
                    mic_buf = np.concatenate(mic_parts)
                    mic_parts.clear()
                    mic_len = 0
                else:
                    mic_buf = np.array([], dtype=np.float32)

                # Emit exactly ONE chunk per wall-clock tick, taking whatever
                # is in the buffers right now. Each side that has ≥CHUNK_SIZE
                # contributes its real samples; the side that doesn't is
                # zero-filled. This decouples emission rate from the burstiness
                # of either source — mic and loopback can arrive in independent
                # bursts and the output still tracks real time exactly.
                lb_pos = 0
                mic_pos = 0
                _zero_chunk = np.zeros(self.CHUNK_SIZE, dtype=np.float32)
                next_emit_time += chunk_dur
                # Single-iteration emit (kept as a `while False`-style block
                # via `if`/`pass` only to preserve the existing nested
                # structure below; we always emit exactly one chunk per tick).
                if True:
                    have_lb  = lb_pos + self.CHUNK_SIZE <= len(lb_buf)
                    have_mic = self._has_mic and mic_pos + self.CHUNK_SIZE <= len(mic_buf)

                    # ── Loopback chunk ──────────────────────────────────────
                    if have_lb:
                        lb_chunk = np.clip(
                            lb_buf[lb_pos:lb_pos + self.CHUNK_SIZE] * self.loopback_gain,
                            -1.0, 1.0,
                        )
                        lb_pos += self.CHUNK_SIZE
                        if self.agc_loopback_enabled:
                            lb_chunk, _agc_lb_env, _g, _gated = self._agc_apply(
                                lb_chunk, _agc_lb_env, self.agc_target_rms,
                                self.agc_max_gain, self.agc_gate_threshold,
                                self.sample_rate or 48000,
                            )
                            self.agc_lb_gain = _g
                            self.agc_lb_envelope = _agc_lb_env
                            self.agc_lb_gated = _gated
                        else:
                            self.agc_lb_gain = 1.0
                            self.agc_lb_gated = True
                        lb_rms = float(np.sqrt(np.mean(lb_chunk ** 2)))
                        self.loopback_level = lb_rms
                        self._lb_fft_buf.extend(lb_chunk.tolist())
                    else:
                        lb_chunk = _zero_chunk
                        lb_rms = 0.0
                        self.loopback_level = 0.0
                        self.agc_lb_gain = 1.0
                        self.agc_lb_gated = True

                    # ── Mic chunk ───────────────────────────────────────────
                    if have_mic:
                        mic_chunk = np.clip(
                            mic_buf[mic_pos:mic_pos + self.CHUNK_SIZE] * self.mic_gain,
                            -1.0, 1.0,
                        )
                        mic_pos += self.CHUNK_SIZE
                        if self.agc_mic_enabled:
                            mic_chunk, _agc_mic_env, _g, _gated = self._agc_apply(
                                mic_chunk, _agc_mic_env, self.agc_target_rms,
                                self.agc_max_gain, self.agc_gate_threshold,
                                self.sample_rate or 48000,
                            )
                            self.agc_mic_gain = _g
                            self.agc_mic_envelope = _agc_mic_env
                            self.agc_mic_gated = _gated
                        else:
                            self.agc_mic_gain = 1.0
                            self.agc_mic_gated = True
                        mic_rms = float(np.sqrt(np.mean(mic_chunk ** 2)))
                        self.mic_level = mic_rms
                        self._mic_fft_buf.extend(mic_chunk.tolist())
                    else:
                        mic_chunk = _zero_chunk
                        mic_rms = 0.0
                        self.mic_level = 0.0
                        self.agc_mic_gain = 1.0
                        self.agc_mic_gated = True

                    # ── WebRTC AEC: only meaningful when BOTH sides are real
                    # this iteration. The reference signal (loopback) must be
                    # in lock-step with the mic; feeding either side alone
                    # desynchronises the buffers. When loopback is absent,
                    # there's no echo to cancel anyway.
                    if self.echo_cancel_enabled and have_lb and have_mic:
                        if _aec_processor is None or _aec_frame_size == 0:
                            try:
                                from aec_audio_processing import AudioProcessor
                                _aec_processor = AudioProcessor(
                                    enable_aec=True, enable_ns=False, enable_agc=False,
                                )
                                sr = self.sample_rate or 16000
                                _aec_processor.set_stream_format(sr, 1)
                                _aec_processor.set_reverse_stream_format(sr, 1)
                                _aec_frame_size = _aec_processor.get_frame_size()
                                log.info("audio", f"WebRTC AEC initialised @ {sr} Hz, "
                                                  f"frame={_aec_frame_size} samples")
                            except Exception:
                                traceback.print_exc()
                                _aec_processor = None

                        if _aec_processor is not None:
                            _aec_mic_buf = np.concatenate((_aec_mic_buf, mic_chunk))
                            _aec_lb_buf  = np.concatenate((_aec_lb_buf,  lb_chunk))
                            cleaned_parts: list[np.ndarray] = []
                            while (len(_aec_mic_buf) >= _aec_frame_size
                                   and len(_aec_lb_buf) >= _aec_frame_size):
                                mf = _aec_mic_buf[:_aec_frame_size]
                                lf = _aec_lb_buf[:_aec_frame_size]
                                _aec_mic_buf = _aec_mic_buf[_aec_frame_size:]
                                _aec_lb_buf  = _aec_lb_buf[_aec_frame_size:]
                                lb_i16  = (lf * 32767).astype(np.int16).tobytes()
                                mic_i16 = (mf * 32767).astype(np.int16).tobytes()
                                _aec_processor.process_reverse_stream(lb_i16)
                                result = _aec_processor.process_stream(mic_i16)
                                cleaned_parts.append(
                                    np.frombuffer(result, dtype=np.int16)
                                      .astype(np.float32) / 32768.0
                                )
                            if cleaned_parts:
                                _aec_out_buf = np.concatenate(
                                    (_aec_out_buf, *cleaned_parts)
                                )
                            if len(_aec_out_buf) >= self.CHUNK_SIZE:
                                mic_chunk = _aec_out_buf[:self.CHUNK_SIZE]
                                _aec_out_buf = _aec_out_buf[self.CHUNK_SIZE:]
                                mic_rms = float(np.sqrt(np.mean(mic_chunk ** 2)))
                    elif (not self.echo_cancel_enabled) and _aec_processor is not None:
                        # Echo cancellation was just disabled - tear down
                        _aec_processor = None
                        _aec_frame_size = 0
                        _aec_mic_buf = np.array([], dtype=np.float32)
                        _aec_lb_buf  = np.array([], dtype=np.float32)
                        _aec_out_buf = np.array([], dtype=np.float32)

                    # ── Mix: always sum. The previous "louder side wins"
                    # gate was muting the mic the moment desktop audio got
                    # loud, which is the opposite of what a meeting tool
                    # should do. Both sources are clipped before summing
                    # and the sum itself is clipped, so headroom is fine.
                    if have_lb and have_mic:
                        src = "both"
                    elif have_mic:
                        src = "mic"
                    else:
                        src = "loopback"
                    mixed = np.clip(lb_chunk + mic_chunk, -1.0, 1.0)

                    if INPUT_DEBUG:
                        self._idbg_mix_src_counts[src] += 1
                        self._idbg_mix_emitted += 1
                        if self._idbg_throttle.ready("mix"):
                            cnt = self._idbg_mix_src_counts
                            log.info("input-debug",
                                     f"mix: lb_rms={lb_rms:.4f} mic_rms={mic_rms:.4f} "
                                     f"have_lb={have_lb} have_mic={have_mic} "
                                     f"-> src={src} | "
                                     f"emitted={self._idbg_mix_emitted} "
                                     f"src_counts={cnt} "
                                     f"audio_q={self.audio_queue.qsize()}/"
                                     f"{self.audio_queue.maxsize} "
                                     f"agc_lb={self.agc_lb_gain:.2f}/gated={self.agc_lb_gated} "
                                     f"agc_mic={self.agc_mic_gain:.2f}/gated={self.agc_mic_gated} "
                                     f"echo_cancel={self.echo_cancel_enabled}")

                    int16_bytes = (mixed * 32767).astype(np.int16).tobytes()

                    # Write to WAV (before queue - never lose audio even if queue is full)
                    sample_offset = -1
                    if self.wav_writer is not None:
                        sample_offset = self.wav_writer.write(int16_bytes)

                    try:
                        self.audio_queue.put_nowait((src, int16_bytes, sample_offset))
                    except queue.Full:
                        if INPUT_DEBUG:
                            self._idbg_audio_q_full_drops += 1
                            if self._idbg_throttle.ready("audio_q_full"):
                                log.warn("input-debug",
                                         f"audio_queue FULL — dropped chunk "
                                         f"(total drops={self._idbg_audio_q_full_drops}, "
                                         f"src={src})")

                # Keep leftover samples (less than CHUNK_SIZE) for next iteration
                if lb_pos < len(lb_buf):
                    lb_parts.append(lb_buf[lb_pos:])
                    lb_len = len(lb_buf) - lb_pos
                if mic_pos < len(mic_buf):
                    mic_parts.append(mic_buf[mic_pos:])
                    mic_len = len(mic_buf) - mic_pos

                # Backpressure: if buffers grow beyond the cap, discard the oldest
                # data.  This prevents unbounded memory growth when the transcriber
                # can't keep up (e.g. slow diarizer).
                if lb_len > max_buf_samples:
                    lb_parts.clear()
                    lb_len = 0
                if mic_len > max_buf_samples:
                    mic_parts.clear()
                    mic_len = 0

                # Pacing sleep handled at top of loop via next_emit_time.
                # We deliberately do NOT sleep here — if we just emitted a
                # chunk and we're already past the next deadline (catch-up),
                # we should immediately loop and emit another.

            except Exception:
                # Log but never let the mixer thread die silently
                traceback.print_exc()
                time.sleep(0.05)


def auto_detect_devices() -> dict:
    """Test all audio devices simultaneously and return the best ones.

    Opens every loopback and dshow mic device in parallel, plays a system
    chime so loopback devices have signal, captures ~2 s of audio, then
    picks the devices with the highest RMS.

    Returns {"best_loopback": {...}, "best_mic": {...}, "loopback": [...], "mic": [...]}.
    """
    pa = pyaudio.PyAudio()
    stop_event = threading.Event()

    # ── Enumerate ────────────────────────────────────────────────────────
    loopbacks = list(pa.get_loopback_device_info_generator())
    dshow_mics = enumerate_dshow_audio_devices()
    log.info("auto-detect", f"Found {len(loopbacks)} loopback, {len(dshow_mics)} dshow mic devices")

    # ── Open all loopback streams (main thread, single PyAudio) ──────────
    lb_streams: list[tuple[dict, object, list]] = []  # (info, stream, data_chunks)
    for lb in loopbacks:
        try:
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=max(1, lb["maxInputChannels"]),
                rate=int(lb["defaultSampleRate"]),
                input=True,
                input_device_index=lb["index"],
                frames_per_buffer=512,
            )
            lb_streams.append((lb, stream, []))
            log.info("auto-detect", f"  Opened loopback: {lb['name']}")
        except Exception as e:
            log.warn("auto-detect", f"  Failed loopback '{lb['name']}': {e}")

    # ── Spawn ffmpeg for each dshow mic ──────────────────────────────────
    from capture_video import find_ffmpeg
    ffmpeg_path = find_ffmpeg()
    mic_procs: list[tuple[dict, subprocess.Popen, list]] = []  # (info, proc, data_chunks)
    if ffmpeg_path:
        for mic in dshow_mics:
            try:
                proc = subprocess.Popen(
                    [ffmpeg_path, "-f", "dshow",
                     "-i", f"audio={mic['name']}",
                     "-f", "s16le", "-ar", "48000", "-ac", "1",
                     "-loglevel", "error", "pipe:1"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                mic_procs.append((mic, proc, []))
                log.info("auto-detect", f"  Opened dshow mic: {mic['name']}")
            except Exception as e:
                log.warn("auto-detect", f"  Failed dshow '{mic['name']}': {e}")

    # ── Reader threads ───────────────────────────────────────────────────
    def _lb_reader(stream, buf, stop_ev):
        while not stop_ev.is_set():
            try:
                data = stream.read(512, exception_on_overflow=False)
                buf.append(data)
            except Exception:
                if not stop_ev.is_set():
                    break

    def _mic_reader(proc, buf, stop_ev):
        while not stop_ev.is_set():
            try:
                data = proc.stdout.read(1024)
                if not data:
                    break
                buf.append(data)
            except Exception:
                break

    threads: list[threading.Thread] = []
    for _, stream, buf in lb_streams:
        t = threading.Thread(target=_lb_reader, args=(stream, buf, stop_event), daemon=True)
        t.start()
        threads.append(t)
    for _, proc, buf in mic_procs:
        t = threading.Thread(target=_mic_reader, args=(proc, buf, stop_event), daemon=True)
        t.start()
        threads.append(t)

    # ── Play test sample through default audio output ──────────────────
    from pathlib import Path
    sample_path = Path(__file__).parent / "audio" / "test_sample.mp3"

    time.sleep(0.3)  # let streams stabilize

    def _play_sample():
        try:
            from playsound import playsound
            playsound(str(sample_path))
        except Exception as e:
            log.warn("auto-detect", f"  playsound failed: {e}")

    if sample_path.exists():
        log.info("auto-detect", f"  Playing test sample: {sample_path.name}")
        play_thread = threading.Thread(target=_play_sample, daemon=True)
        play_thread.start()
    else:
        log.warn("auto-detect", f"  Test sample not found: {sample_path}")

    time.sleep(3.0)  # capture window — matches the 3s sample duration
    stop_event.set()
    for t in threads:
        t.join(timeout=1)

    # ── Compute RMS per device ───────────────────────────────────────────
    def _compute_rms(chunks: list[bytes]) -> float:
        if not chunks:
            return 0.0
        raw = b"".join(chunks)
        if len(raw) < 2:
            return 0.0
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        return float(np.sqrt(np.mean(samples ** 2)))

    lb_results = []
    for info, stream, buf in lb_streams:
        rms = _compute_rms(buf)
        lb_results.append({"index": int(info["index"]), "name": info["name"],
                           "rms": round(rms, 6)})
        log.info("auto-detect", f"  Loopback '{info['name']}': RMS={rms:.6f}")

    mic_results = []
    for info, proc, buf in mic_procs:
        rms = _compute_rms(buf)
        mic_results.append({"name": info["name"], "rms": round(rms, 6)})
        log.info("auto-detect", f"  Mic '{info['name']}': RMS={rms:.6f}")

    # ── Cleanup ──────────────────────────────────────────────────────────
    for _, stream, _ in lb_streams:
        _stream_graveyard.append(stream)
    _stream_graveyard.append(pa)

    for _, proc, _ in mic_procs:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            pass

    # ── Pick winners ─────────────────────────────────────────────────────
    lb_results.sort(key=lambda d: d["rms"], reverse=True)
    mic_results.sort(key=lambda d: d["rms"], reverse=True)

    best_lb = lb_results[0] if lb_results else None
    best_mic = mic_results[0] if mic_results else None

    if best_lb:
        log.info("auto-detect", f"  >> Best loopback: '{best_lb['name']}' (RMS={best_lb['rms']:.6f})")
    if best_mic:
        log.info("auto-detect", f"  >> Best mic: '{best_mic['name']}' (RMS={best_mic['rms']:.6f})")

    # ── Play completion chime ───────────────────────────────────────────
    complete_path = Path(__file__).parent / "audio" / "complete.mp3"
    if complete_path.exists():
        def _play_complete():
            try:
                from playsound import playsound
                playsound(str(complete_path))
            except Exception:
                pass
        threading.Thread(target=_play_complete, daemon=True).start()

    return {
        "best_loopback": best_lb,
        "best_mic": best_mic,
        "loopback": lb_results,
        "mic": mic_results,
    }


def default_device_name_matches(output_name: str, loopback_name: str) -> bool:
    """Check if a loopback device corresponds to the given output device."""
    return output_name in loopback_name


def enumerate_audio_devices() -> dict:
    """
    Return lists of available loopback and microphone input devices.
    Creates and destroys a temporary PyAudio instance - safe to call
    even while recording is active.

    Input devices are filtered to WASAPI only (same API used for capture)
    to avoid showing the same physical device three times (MME / DirectSound /
    WASAPI) and to exclude loopback virtual devices from the mic list.
    """
    pa = pyaudio.PyAudio()
    try:
        loopbacks = [
            {"index": int(d["index"]), "name": d["name"]}
            for d in pa.get_loopback_device_info_generator()
        ]

        try:
            wasapi_idx = pa.get_host_api_info_by_type(pyaudio.paWASAPI)["index"]
        except Exception:
            wasapi_idx = None

        # Collect the loopback device indices so we can exclude them from mic list
        loopback_indices = {lb["index"] for lb in loopbacks}

        inputs = []
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            # WASAPI only - skip MME / DirectSound duplicates
            if wasapi_idx is not None and info.get("hostApi") != wasapi_idx:
                continue
            # Must have at least one input channel
            if info.get("maxInputChannels", 0) <= 0:
                continue
            # Exclude loopback virtual devices (they're already in the loopback list)
            if int(info["index"]) in loopback_indices:
                continue
            if "[Loopback]" in info.get("name", ""):
                continue
            inputs.append({"index": int(info["index"]), "name": info["name"]})

        return {"loopback": loopbacks, "input": inputs}
    finally:
        pa.terminate()


def enumerate_dshow_audio_devices() -> list[dict]:
    """List DirectShow audio input devices via ffmpeg.

    Returns a list of {"name": "..."} dicts.  These names are what ffmpeg
    expects in ``-i audio=<name>``.  Returns an empty list if ffmpeg is
    unavailable or the query fails.
    """
    from capture_video import find_ffmpeg
    ffmpeg_path = find_ffmpeg()
    if not ffmpeg_path:
        return []
    try:
        result = subprocess.run(
            [ffmpeg_path, "-f", "dshow", "-list_devices", "true", "-i", "dummy"],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        # ffmpeg prints device list to stderr
        output = result.stderr
    except Exception:
        return []

    devices: list[dict] = []
    for line in output.splitlines():
        # "Alternative name" lines follow the device line and carry the stable
        # @device_cm_{GUID}\wave_{GUID} ID — attach to the last device.
        if "Alternative name" in line:
            m = re.search(r'"(.+?)"', line)
            if m and devices:
                devices[-1]["alt_name"] = m.group(1)
            continue
        # Device lines look like: [in#0 @ ...] "Device Name" (audio)
        if "(audio)" not in line.lower():
            continue
        m = re.search(r'"(.+?)"', line)
        if m:
            devices.append({"name": m.group(1)})
    return devices


def resolve_dshow_mic_name(requested: str) -> tuple[str | None, str]:
    """Re-resolve a saved DirectShow mic name against the current device list.

    Device friendly names can change between sessions (driver updates, USB
    re-enumeration) and devices can be unplugged entirely, so the name we
    persisted may no longer match anything ffmpeg can open. This re-queries
    ffmpeg's live device list and picks the best surviving match.

    Resolution order:
      1. Exact match on friendly name.
      2. Exact match on the alternative (GUID) name — survives friendly-name
         changes for the same physical device.
      3. Case-insensitive friendly-name match.
      4. Substring match (requested ⊂ candidate or candidate ⊂ requested).

    Returns (resolved_name, reason). resolved_name is None if nothing matched.
    The reason string is suitable for logging (e.g. "exact", "alt-name",
    "substring", "no-match").
    """
    if not requested:
        return None, "empty-request"
    devices = enumerate_dshow_audio_devices()
    if not devices:
        return None, "enumeration-failed"

    # 1. Exact friendly-name match
    for d in devices:
        if d.get("name") == requested:
            return requested, "exact"

    # 2. Alternative-name match: requested may itself be an alt name, or a
    #    previously-resolved alt may still exist under a different friendly name.
    for d in devices:
        if d.get("alt_name") == requested:
            return d["name"], "alt-name"

    # 3. Case-insensitive
    req_lower = requested.lower()
    for d in devices:
        if d.get("name", "").lower() == req_lower:
            return d["name"], "case-insensitive"

    # 4. Substring (prefer longest candidate name)
    cand: list[tuple[int, str]] = []
    for d in devices:
        name = d.get("name", "")
        if not name:
            continue
        nl = name.lower()
        if req_lower in nl or nl in req_lower:
            cand.append((len(name), name))
    if cand:
        cand.sort(reverse=True)
        return cand[0][1], "substring"

    return None, "no-match"
