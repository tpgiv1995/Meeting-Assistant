"""
macOS audio capture (CoreAudio via sounddevice + BlackHole loopback).

System audio "loopback" on macOS comes from BlackHole — a free virtual audio
driver that exposes itself as both an output device (where system audio is
routed) and an input device (where we read it back). The mac_audio_bootstrap
module handles installation and aggregate-device wiring on first launch so
the user still hears audio through their normal speakers/headphones.

Mic capture goes through the default CoreAudio input or any user-selected
device. The browser-mic path (mic_index=-2) and the avfoundation subprocess
path (mic_index=-3) both work the same as on Windows because they're
platform-agnostic at the audio layer (browser sends PCM, avfoundation writes
PCM to a pipe).

Public API matches audio_capture_win.py exactly so the dispatcher can swap
backends without app.py noticing.
"""
from __future__ import annotations

import collections
import queue
import re
import subprocess
import threading
import time
import traceback
from math import gcd
from pathlib import Path

import numpy as np
import sounddevice as sd
from scipy.signal import resample_poly

from core import log as log
from capture_audio.wav_writer import WavWriter

# FFT window size for the spectrum visualizer.  2048 samples ≈ 43 ms at 48 kHz,
# giving ~23 Hz frequency resolution.
_FFT_SIZE = 4096
_N_BARS   = 32

# Substrings used to identify BlackHole's CoreAudio device — covers both
# 2ch and 16ch variants. The 2ch is what we install via Homebrew/.pkg.
_BLACKHOLE_NAME_HINTS = ("BlackHole 2ch", "BlackHole 16ch", "BlackHole")


def _is_blackhole(name: str) -> bool:
    return any(hint in name for hint in _BLACKHOLE_NAME_HINTS)


class AudioCapture:
    CHUNK_SIZE = 512
    # sounddevice uses numpy dtype strings rather than PortAudio constants.
    SD_DTYPE = "int16"

    def __init__(self, audio_queue: queue.Queue):
        self.audio_queue = audio_queue
        self.is_running = False
        self._loopback_stream: sd.RawInputStream | None = None
        self._mic_stream: sd.RawInputStream | None = None
        self._loopback_thread: threading.Thread | None = None
        self._mic_thread: threading.Thread | None = None
        self._mixer_thread: threading.Thread | None = None

        self.sample_rate: int | None = None
        self.channels: int = 1

        self._loopback_q: queue.Queue = queue.Queue(maxsize=200)
        self._mic_q: queue.Queue = queue.Queue(maxsize=200)

        self._loopback_channels: int = 2  # BlackHole defaults to stereo
        self._mic_rate: int | None = None
        self._mic_channels: int = 1
        self._has_mic: bool = False
        self._mic_buf_size: int = 1024
        self._resample_up: int = 1
        self._resample_down: int = 1

        self.wav_writer: WavWriter | None = None
        self._wav_path: str | None = None
        self._wav_append: bool = False

        self.loopback_level: float = 0.0
        self.mic_level: float = 0.0

        self.loopback_gain: float = 1.0
        self.mic_gain: float = 1.0

        self.echo_cancel_enabled: bool = False

        self.agc_loopback_enabled: bool = True
        self.agc_mic_enabled: bool = True
        self.agc_target_rms: float = 0.15
        self.agc_max_gain: float = 4.0
        self.agc_gate_threshold: float = 0.005

        self.agc_lb_gain: float = 1.0
        self.agc_lb_envelope: float = 0.0
        self.agc_lb_gated: bool = True
        self.agc_mic_gain: float = 1.0
        self.agc_mic_envelope: float = 0.0
        self.agc_mic_gated: bool = True

        self._lb_fft_buf:  collections.deque = collections.deque(maxlen=_FFT_SIZE)
        self._mic_fft_buf: collections.deque = collections.deque(maxlen=_FFT_SIZE)
        self._hann_window: np.ndarray | None = None

        # avfoundation subprocess mic capture (mic_index=-3 on Mac)
        self._ffmpeg_proc: subprocess.Popen | None = None
        self._ffmpeg_mic_name: str | None = None

        # CoreAudio routing state — set in start(), restored in stop().
        # When non-None, the system default output was switched to the
        # Meeting Assistant aggregate device for the duration of recording.
        self._prev_default_output_id: int | None = None

    # ── Device discovery ──────────────────────────────────────────────────────

    @staticmethod
    def _list_input_devices() -> list[dict]:
        """Return all CoreAudio input devices (with at least one input channel)."""
        out: list[dict] = []
        try:
            for idx, dev in enumerate(sd.query_devices()):
                if dev.get("max_input_channels", 0) > 0:
                    out.append({
                        "index": idx,
                        "name": dev["name"],
                        "default_samplerate": int(dev.get("default_samplerate") or 48000),
                        "max_input_channels": int(dev["max_input_channels"]),
                    })
        except Exception as e:
            log.warn("audio", f"sd.query_devices failed: {e}")
        return out

    def _find_loopback_device(self) -> dict:
        """Return BlackHole's input device info — that's our loopback source.

        BlackHole MUST already be installed (mac_audio_bootstrap handles that
        on first launch). If it isn't, we raise with an actionable message.
        """
        for dev in self._list_input_devices():
            if _is_blackhole(dev["name"]):
                return dev
        raise RuntimeError(
            "BlackHole audio driver not found. The launcher should have "
            "installed it on first launch. Run `brew install blackhole-2ch` "
            "manually, then restart Meeting Assistant."
        )

    def _find_mic_device(self) -> dict | None:
        """Return the system default CoreAudio input device, skipping BlackHole."""
        try:
            default_in_idx = sd.default.device[0]
        except Exception:
            default_in_idx = None
        if default_in_idx is not None and default_in_idx >= 0:
            try:
                info = sd.query_devices(default_in_idx)
                if info.get("max_input_channels", 0) > 0 and not _is_blackhole(info["name"]):
                    return {
                        "index": int(default_in_idx),
                        "name": info["name"],
                        "default_samplerate": int(info.get("default_samplerate") or 48000),
                        "max_input_channels": int(info["max_input_channels"]),
                    }
            except Exception:
                pass

        # Fallback: first non-BlackHole input device.
        for dev in self._list_input_devices():
            if not _is_blackhole(dev["name"]):
                return dev
        return None

    # ── WAV recording ──────────────────────────────────────────────────────

    def start_wav(self, path: str, append: bool = False) -> None:
        self._wav_path = path
        self._wav_append = append

    def stop_wav(self) -> None:
        if self.wav_writer is not None:
            self.wav_writer.close()
            self.wav_writer = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self, loopback_index: int | None = None, mic_index: int | None = None,
              ffmpeg_mic_name: str | None = None) -> None:
        """Start capture with the same semantics as the Windows backend:
            mic_index=-1  explicitly disable mic
            mic_index=-2  receive mic audio injected from the browser
            mic_index=-3  capture via ffmpeg avfoundation subprocess
        """
        # ── Recording-time output routing ─────────────────────────────────
        # Switch the system default output to the "Meeting Assistant Output"
        # aggregate so audio reaches both the user's speakers AND BlackHole.
        # Without this, BlackHole captures silence (its input only sees what
        # was *output* through it, and the system was outputting to speakers).
        # The previous default is restored in stop().
        try:
            from capture_audio.mac_bootstrap import prepare_recording_routing
            routing = prepare_recording_routing()
            if routing.get("ok"):
                self._prev_default_output_id = routing.get("prev_default_id")
            else:
                log.warn("audio", f"Loopback routing not engaged: {routing.get('message')}. "
                                  f"System audio capture will be silent until you set "
                                  f"'Meeting Assistant Output' as the system output manually.")
                self._prev_default_output_id = None
        except Exception as e:
            log.warn("audio", f"prepare_recording_routing failed: {e}")
            self._prev_default_output_id = None

        # ── Loopback (BlackHole) ─────────────────────────────────────────
        if loopback_index is not None and loopback_index >= 0:
            try:
                lb_info_raw = sd.query_devices(loopback_index)
                lb_info = {
                    "index": int(loopback_index),
                    "name": lb_info_raw["name"],
                    "default_samplerate": int(lb_info_raw.get("default_samplerate") or 48000),
                    "max_input_channels": int(lb_info_raw["max_input_channels"]),
                }
            except Exception as e:
                log.warn("audio", f"loopback_index {loopback_index} invalid: {e}")
                lb_info = self._find_loopback_device()
        else:
            lb_info = self._find_loopback_device()

        self.sample_rate = int(lb_info["default_samplerate"])
        self._loopback_channels = max(1, lb_info["max_input_channels"])
        log.info("audio", f"Loopback: '{lb_info['name']}' @ {self.sample_rate} Hz, "
                          f"{self._loopback_channels} ch")

        self._loopback_stream = sd.RawInputStream(
            samplerate=self.sample_rate,
            channels=self._loopback_channels,
            dtype=self.SD_DTYPE,
            blocksize=self.CHUNK_SIZE,
            device=lb_info["index"],
        )
        self._loopback_stream.start()

        # ── Microphone (best-effort, multi-mode) ─────────────────────────
        mic_info = None

        if mic_index == -3:
            # avfoundation subprocess via ffmpeg.
            from capture_video.ffmpeg_util import find_ffmpeg
            ffmpeg_path = find_ffmpeg()
            if not ffmpeg_path:
                log.warn("audio", "ffmpeg not found - cannot use ffmpeg mic capture")
            elif not ffmpeg_mic_name:
                log.warn("audio", "No avfoundation mic device name provided")
            else:
                self._mic_rate = 48000
                self._mic_channels = 1
                self._has_mic = True
                self._ffmpeg_mic_name = ffmpeg_mic_name
                if self._mic_rate != self.sample_rate:
                    g = gcd(self.sample_rate, self._mic_rate)
                    self._resample_up   = self.sample_rate // g
                    self._resample_down = self._mic_rate    // g
                # avfoundation indexes audio devices like "[<idx>]" or by name.
                # We pass `:<name>` (video=none) so ffmpeg matches by name.
                cmd = [
                    ffmpeg_path,
                    "-f", "avfoundation",
                    "-audio_buffer_size", "40",
                    "-i", f":{ffmpeg_mic_name}",
                    "-f", "s16le",
                    "-acodec", "pcm_s16le",
                    "-ar", str(self._mic_rate),
                    "-ac", "1",
                    "-fflags", "+nobuffer",
                    "-flags", "+low_delay",
                    "-loglevel", "error",
                    "pipe:1",
                ]
                log.info("audio", f"Mic: ffmpeg avfoundation '{ffmpeg_mic_name}' "
                                  f"@ {self._mic_rate} Hz, 1 ch")
                self._ffmpeg_proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
        elif mic_index == -2:
            # Browser mic: data arrives via inject_mic_data().
            self._mic_rate = 48000
            self._mic_channels = 1
            self._has_mic = True
            if self._mic_rate != self.sample_rate:
                g = gcd(self.sample_rate, self._mic_rate)
                self._resample_up   = self.sample_rate // g
                self._resample_down = self._mic_rate    // g
            log.info("audio", f"Mic: browser (inject_mic_data) @ {self._mic_rate} Hz, 1 ch")
        elif mic_index == -1:
            pass  # explicitly disabled
        elif mic_index is not None:
            try:
                raw = sd.query_devices(mic_index)
                mic_info = {
                    "index": int(mic_index),
                    "name": raw["name"],
                    "default_samplerate": int(raw.get("default_samplerate") or 48000),
                    "max_input_channels": int(raw["max_input_channels"]),
                }
            except Exception as e:
                log.warn("audio", f"Specified mic device {mic_index} invalid: {e}")
                mic_info = None
        else:
            mic_info = self._find_mic_device()

        if mic_info:
            try:
                self._mic_rate = int(mic_info["default_samplerate"])
                self._mic_channels = max(1, mic_info["max_input_channels"])
                # CoreAudio is happy with small block sizes; no power-of-two dance.
                self._mic_buf_size = 1024
                self._mic_stream = sd.RawInputStream(
                    samplerate=self._mic_rate,
                    channels=self._mic_channels,
                    dtype=self.SD_DTYPE,
                    blocksize=self._mic_buf_size,
                    device=mic_info["index"],
                )
                self._mic_stream.start()
                self._has_mic = True
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
            self._has_mic = False
            if mic_index == -1:
                log.info("audio", "Microphone explicitly disabled - capturing loopback only.")
            else:
                log.info("audio", "No microphone device found - capturing loopback only.")

        if self._wav_path:
            self.wav_writer = WavWriter(self._wav_path, self.sample_rate, append=self._wav_append)
            self._wav_path = None

        self.is_running = True

        self._loopback_thread = threading.Thread(
            target=self._capture_loop,
            args=(self._loopback_stream, self._loopback_q, self._loopback_channels),
            daemon=True,
        )
        self._loopback_thread.start()

        if self._has_mic and self._ffmpeg_proc is not None:
            self._mic_thread = threading.Thread(
                target=self._ffmpeg_capture_loop,
                daemon=True,
            )
            self._mic_thread.start()
        elif self._has_mic and self._mic_stream is not None:
            self._mic_thread = threading.Thread(
                target=self._capture_loop,
                args=(self._mic_stream, self._mic_q, self._mic_channels),
                daemon=True,
            )
            self._mic_thread.start()

        self._mixer_thread = threading.Thread(target=self._mixer_loop, daemon=True)
        self._mixer_thread.start()

    def stop(self) -> None:
        self.is_running = False
        if self._ffmpeg_proc is not None:
            try:
                self._ffmpeg_proc.terminate()
            except Exception:
                pass
        for t in (self._loopback_thread, self._mic_thread, self._mixer_thread):
            if t:
                t.join(timeout=3)
        self._loopback_thread = None
        self._mic_thread = None
        self._mixer_thread = None

        self.stop_wav()

        # Clean teardown of CoreAudio streams — unlike WASAPI loopback on
        # Windows, sounddevice's stop()/close() are well-behaved on macOS.
        for s in (self._loopback_stream, self._mic_stream):
            if s is not None:
                try:
                    s.stop()
                    s.close()
                except Exception:
                    pass
        self._loopback_stream = None
        self._mic_stream = None
        self._ffmpeg_proc = None

        # Restore the system default output device that was in use before
        # recording started. Safe no-op if routing wasn't engaged.
        if self._prev_default_output_id is not None:
            try:
                from capture_audio.mac_bootstrap import restore_recording_routing
                restore_recording_routing(self._prev_default_output_id)
            except Exception as e:
                log.warn("audio", f"restore_recording_routing failed: {e}")
            self._prev_default_output_id = None

    def compute_spectrum(self, buf: collections.deque) -> list[float]:
        """Return _N_BARS log-spaced frequency magnitudes from the sample buffer."""
        if len(buf) < _FFT_SIZE // 4:
            return [0.0] * _N_BARS

        samples = np.array(buf, dtype=np.float32)
        n = len(samples)

        if self._hann_window is None or len(self._hann_window) != n:
            self._hann_window = np.hanning(n).astype(np.float32)

        windowed = samples * self._hann_window
        padded = windowed if n >= _FFT_SIZE else np.pad(windowed, (0, _FFT_SIZE - n))
        fft_mag = np.abs(np.fft.rfft(padded)) / (n * 0.5)
        freqs   = np.fft.rfftfreq(len(padded), d=1.0 / (self.sample_rate or 48000))

        f_min = 40.0
        f_max = min(20000.0, (self.sample_rate or 48000) / 2.0)
        edges = np.logspace(np.log10(f_min), np.log10(f_max), _N_BARS + 1)

        result: list[float] = []
        for i in range(_N_BARS):
            mask = (freqs >= edges[i]) & (freqs < edges[i + 1])
            val  = float(np.mean(fft_mag[mask])) if mask.any() else 0.0
            result.append(round(min(1.0, (val * 80) ** 0.5), 4))
        return result

    def inject_mic_data(self, data: bytes) -> None:
        if self.is_running and self._has_mic:
            try:
                self._mic_q.put_nowait(data)
            except queue.Full:
                pass

    # ── Capture threads ───────────────────────────────────────────────────────

    def _capture_loop(self, stream: sd.RawInputStream, out_queue: queue.Queue,
                      channels: int) -> None:
        """Read sounddevice RawInputStream into the queue. Blocks per chunk."""
        # Each frame is `channels * 2` bytes (Int16). sounddevice returns a
        # (data: bytes, overflowed: bool) tuple from .read(n_frames).
        while self.is_running:
            try:
                data, overflowed = stream.read(self.CHUNK_SIZE)
                if overflowed:
                    log.warn("audio", "sounddevice input overflow")
                # data is a CFFI buffer; convert to bytes for the queue.
                payload = bytes(data)
                try:
                    out_queue.put_nowait(payload)
                except queue.Full:
                    pass
            except Exception:
                if not self.is_running:
                    break
                time.sleep(0.01)

    def _ffmpeg_capture_loop(self) -> None:
        """Read raw PCM from an ffmpeg avfoundation subprocess."""
        read_size = self.CHUNK_SIZE * 2
        proc = self._ffmpeg_proc
        try:
            while self.is_running and proc and proc.poll() is None:
                data = proc.stdout.read(read_size)
                if not data:
                    break
                try:
                    self._mic_q.put_nowait(data)
                except queue.Full:
                    pass
        except Exception:
            if self.is_running:
                log.warn("audio", f"ffmpeg mic capture error:\n{traceback.format_exc()}")
        finally:
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

    # ── DSP helpers (mirror audio_capture_win.py exactly) ─────────────────

    @staticmethod
    def _agc_apply(chunk: np.ndarray, envelope: float, target_rms: float,
                   max_gain: float, gate_threshold: float,
                   sample_rate: int) -> tuple[np.ndarray, float, float, bool]:
        chunk_rms = float(np.sqrt(np.mean(chunk ** 2)))
        chunk_dur = len(chunk) / max(sample_rate, 1)
        attack  = 1.0 - np.exp(-chunk_dur / 0.05)
        release = 1.0 - np.exp(-chunk_dur / 1.5)
        coeff = attack if chunk_rms > envelope else release
        envelope += coeff * (chunk_rms - envelope)

        gated = envelope <= gate_threshold
        if not gated and envelope < target_rms:
            gain = min(target_rms / envelope, max_gain)
        else:
            gain = 1.0

        if chunk_rms > 1e-6 and chunk_rms * gain > target_rms:
            gain = target_rms / chunk_rms

        return np.clip(chunk * gain, -1.0, 1.0), envelope, gain, gated

    @staticmethod
    def _to_mono_float(data: bytes, channels: int) -> np.ndarray:
        samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)
        return samples

    def _mixer_loop(self) -> None:
        # Identical to audio_capture_win.py — pure DSP, no platform calls.
        lb_parts: list[np.ndarray] = []
        lb_len = 0
        mic_parts: list[np.ndarray] = []
        mic_len = 0
        max_buf_samples = int((self.sample_rate or 48000) * 3.0)

        _agc_lb_env  = 0.0
        _agc_mic_env = 0.0

        _aec_processor = None
        _aec_frame_size = 0
        _aec_mic_buf = np.array([], dtype=np.float32)
        _aec_lb_buf  = np.array([], dtype=np.float32)
        _aec_out_buf = np.array([], dtype=np.float32)

        while self.is_running:
            try:
                got_data = False

                try:
                    while True:
                        data = self._loopback_q.get_nowait()
                        chunk = self._to_mono_float(data, self._loopback_channels)
                        lb_parts.append(chunk)
                        lb_len += len(chunk)
                        got_data = True
                except queue.Empty:
                    pass

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

                lb_pos = 0
                mic_pos = 0
                while lb_pos + self.CHUNK_SIZE <= len(lb_buf):
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

                    if self._has_mic and mic_pos + self.CHUNK_SIZE <= len(mic_buf):
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

                        if self.echo_cancel_enabled:
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

                                    lb_i16 = (lf * 32767).astype(np.int16).tobytes()
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
                        elif _aec_processor is not None:
                            _aec_processor = None
                            _aec_frame_size = 0
                            _aec_mic_buf = np.array([], dtype=np.float32)
                            _aec_lb_buf  = np.array([], dtype=np.float32)
                            _aec_out_buf = np.array([], dtype=np.float32)

                        if mic_rms < 0.005 or lb_rms > mic_rms * 2.0:
                            src = "loopback"
                            mixed = lb_chunk
                        elif lb_rms < 0.005 or mic_rms > lb_rms * 2.0:
                            src = "mic"
                            mixed = mic_chunk
                        else:
                            src = "both"
                            mixed = np.clip(lb_chunk + mic_chunk, -1.0, 1.0)
                    else:
                        self.mic_level = 0.0
                        mixed = lb_chunk
                        src = "loopback"

                    int16_bytes = (mixed * 32767).astype(np.int16).tobytes()

                    sample_offset = -1
                    if self.wav_writer is not None:
                        sample_offset = self.wav_writer.write(int16_bytes)

                    try:
                        self.audio_queue.put_nowait((src, int16_bytes, sample_offset))
                    except queue.Full:
                        pass

                if lb_pos < len(lb_buf):
                    lb_parts.append(lb_buf[lb_pos:])
                    lb_len = len(lb_buf) - lb_pos
                if mic_pos < len(mic_buf):
                    mic_parts.append(mic_buf[mic_pos:])
                    mic_len = len(mic_buf) - mic_pos

                if lb_len > max_buf_samples:
                    lb_parts.clear()
                    lb_len = 0
                if mic_len > max_buf_samples:
                    mic_parts.clear()
                    mic_len = 0

                if not got_data:
                    time.sleep(0.005)

            except Exception:
                traceback.print_exc()
                time.sleep(0.05)


# ── Module-level enumeration / auto-detect ──────────────────────────────────

def enumerate_audio_devices() -> dict:
    """Return {'loopback': [...], 'input': [...]}.

    On macOS the only loopback we expose is BlackHole. Other input devices
    are listed as mics, excluding BlackHole itself (so the user can't pick
    BlackHole as their mic by accident).
    """
    loopbacks: list[dict] = []
    inputs: list[dict] = []
    try:
        for idx, dev in enumerate(sd.query_devices()):
            if dev.get("max_input_channels", 0) <= 0:
                continue
            entry = {"index": idx, "name": dev["name"]}
            if _is_blackhole(dev["name"]):
                loopbacks.append(entry)
            else:
                inputs.append(entry)
    except Exception as e:
        log.warn("audio", f"enumerate_audio_devices failed: {e}")
    return {"loopback": loopbacks, "input": inputs}


def enumerate_dshow_audio_devices() -> list[dict]:
    """List avfoundation audio input devices via ffmpeg.

    Naming kept as 'dshow' to match the Windows API for callers; on macOS
    we query avfoundation. Returns [{'name': '...'}] as ffmpeg expects in
    `-i :<name>` arguments.
    """
    from capture_video.ffmpeg_util import find_ffmpeg
    ffmpeg_path = find_ffmpeg()
    if not ffmpeg_path:
        return []
    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-f", "avfoundation",
             "-list_devices", "true", "-i", ""],
            capture_output=True, text=True, timeout=10,
        )
        output = result.stderr or ""
    except Exception:
        return []

    devices: list[dict] = []
    in_audio_section = False
    for line in output.splitlines():
        if "AVFoundation audio devices" in line:
            in_audio_section = True
            continue
        if "AVFoundation video devices" in line:
            in_audio_section = False
            continue
        if not in_audio_section:
            continue
        # Lines look like: [AVFoundation indev @ ...] [0] MacBook Pro Microphone
        m = re.search(r"\]\s*\[\d+\]\s+(.+?)$", line.rstrip())
        if m:
            name = m.group(1).strip()
            if name and not _is_blackhole(name):
                devices.append({"name": name})
    return devices


def _play_audio_file(p: Path, device: int | str | None = None) -> None:
    """Play an audio file through `device` (or the system default if None).

    Uses soundfile (bundled with sounddevice) for decoding — handles MP3,
    WAV, FLAC, etc. Replaces the previous `playsound` dependency, which was
    never in requirements.txt and silently failed on every call site.

    On macOS, sounddevice caches the system default at import time and does
    NOT pick up CoreAudio default-device changes that happen later. So when
    auto_detect_devices switches the default to the aggregate it must pass
    `device=<aggregate index>` explicitly here, otherwise this function
    plays through the *original* speakers and bypasses BlackHole entirely.
    """
    try:
        import soundfile as sf  # type: ignore
        data, sr = sf.read(str(p), dtype="float32", always_2d=False)
        sd.play(data, samplerate=sr, device=device, blocking=True)
    except Exception as e:
        log.warn("auto-detect", f"  audio playback failed for {p.name}: {e}")


def _find_sd_device_index(name_substring: str, kind: str = "output") -> int | None:
    """Find a sounddevice device index whose name contains `name_substring`.

    `kind` is "output" or "input" — filters by max_output_channels /
    max_input_channels respectively.
    """
    field = "max_output_channels" if kind == "output" else "max_input_channels"
    try:
        for i, d in enumerate(sd.query_devices()):
            if d.get(field, 0) > 0 and name_substring in d["name"]:
                return i
    except Exception:
        return None
    return None


def auto_detect_devices() -> dict:
    """Open all input devices in parallel, play a chime, capture ~3 s, rank by RMS.

    Engages BlackHole loopback routing for the duration of the test so the
    test sample actually reaches BlackHole's input — without this, the
    loopback RMS is meaningless (always 0).
    """
    # ── Engage loopback routing for the test ─────────────────────────────
    # Without this, BlackHole captures silence because the test sample plays
    # straight to the speakers, bypassing BlackHole entirely.
    routing_status: dict | None = None
    try:
        from capture_audio.mac_bootstrap import prepare_recording_routing
        routing_status = prepare_recording_routing()
        if not routing_status.get("ok"):
            log.warn("auto-detect",
                     f"Loopback routing not engaged: {routing_status.get('message')}. "
                     f"BlackHole RMS reading will be unreliable.")
    except Exception as e:
        log.warn("auto-detect", f"Routing setup failed: {e}")
        routing_status = None

    try:
        return _auto_detect_devices_inner()
    finally:
        # Always restore the user's previous default output, even on error.
        if routing_status and routing_status.get("ok"):
            try:
                from capture_audio.mac_bootstrap import restore_recording_routing
                restore_recording_routing(routing_status.get("prev_default_id"))
            except Exception as e:
                log.warn("auto-detect", f"Routing restore failed: {e}")


def _auto_detect_devices_inner() -> dict:
    """The actual auto-detect body — separated so auto_detect_devices() can
    wrap it in a routing-engagement try/finally."""
    stop_event = threading.Event()

    all_inputs = AudioCapture._list_input_devices()
    log.info("auto-detect", f"Found {len(all_inputs)} CoreAudio input devices")

    streams: list[tuple[dict, sd.RawInputStream, list, str]] = []
    for dev in all_inputs:
        kind = "loopback" if _is_blackhole(dev["name"]) else "mic"
        try:
            stream = sd.RawInputStream(
                samplerate=dev["default_samplerate"],
                channels=max(1, dev["max_input_channels"]),
                dtype="int16",
                blocksize=512,
                device=dev["index"],
            )
            stream.start()
            streams.append((dev, stream, [], kind))
            log.info("auto-detect", f"  Opened {kind}: {dev['name']}")
        except Exception as e:
            log.warn("auto-detect", f"  Failed {kind} '{dev['name']}': {e}")

    def _reader(stream, buf, channels, stop_ev):
        while not stop_ev.is_set():
            try:
                data, _of = stream.read(512)
                buf.append(bytes(data))
            except Exception:
                if not stop_ev.is_set():
                    break

    threads: list[threading.Thread] = []
    for dev, stream, buf, _kind in streams:
        t = threading.Thread(
            target=_reader,
            args=(stream, buf, max(1, dev["max_input_channels"]), stop_event),
            daemon=True,
        )
        t.start()
        threads.append(t)

    sample_path = Path(__file__).parent / "audio" / "test_sample.mp3"
    time.sleep(0.3)

    # Pick the correct sounddevice index for playback. With routing engaged
    # we want the aggregate so the test sample reaches BOTH the user's
    # speakers and BlackHole. If routing failed (e.g. BlackHole missing),
    # fall back to the system default output.
    play_device = _find_sd_device_index("Meeting Assistant Output", kind="output")

    if sample_path.exists():
        log.info("auto-detect", f"  Playing test sample: {sample_path.name}"
                                f" (device idx={play_device if play_device is not None else 'default'})")
        threading.Thread(
            target=_play_audio_file, args=(sample_path, play_device), daemon=True,
        ).start()

    time.sleep(3.0)
    stop_event.set()
    for t in threads:
        t.join(timeout=1)

    def _compute_rms(chunks: list[bytes]) -> float:
        if not chunks:
            return 0.0
        raw = b"".join(chunks)
        if len(raw) < 2:
            return 0.0
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        return float(np.sqrt(np.mean(samples ** 2)))

    lb_results: list[dict] = []
    mic_results: list[dict] = []
    for dev, stream, buf, kind in streams:
        rms = _compute_rms(buf)
        entry = {"index": int(dev["index"]), "name": dev["name"], "rms": round(rms, 6)}
        if kind == "loopback":
            lb_results.append(entry)
        else:
            mic_results.append(entry)
        log.info("auto-detect", f"  {kind} '{dev['name']}': RMS={rms:.6f}")

    for _, stream, _, _ in streams:
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass

    lb_results.sort(key=lambda d: d["rms"], reverse=True)
    mic_results.sort(key=lambda d: d["rms"], reverse=True)

    best_lb = lb_results[0] if lb_results else None
    best_mic = mic_results[0] if mic_results else None

    if best_lb:
        log.info("auto-detect", f"  >> Best loopback: '{best_lb['name']}' (RMS={best_lb['rms']:.6f})")
    if best_mic:
        log.info("auto-detect", f"  >> Best mic: '{best_mic['name']}' (RMS={best_mic['rms']:.6f})")

    complete_path = Path(__file__).parent / "audio" / "complete.mp3"
    if complete_path.exists():
        # The completion chime should be audible to the user, so play it
        # through the *speakers* (system default) rather than the aggregate
        # — the user is watching the UI, not analysing the chime's loopback.
        threading.Thread(target=_play_audio_file, args=(complete_path,), daemon=True).start()

    return {
        "best_loopback": best_lb,
        "best_mic": best_mic,
        "loopback": lb_results,
        "mic": mic_results,
    }
