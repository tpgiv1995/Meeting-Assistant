"""Simple WAV file writer that tracks sample offsets for timestamp syncing."""
import os
import struct
import wave
from math import gcd

import numpy as np
from scipy.signal import resample_poly

from core import log as log


class WavWriter:
    """Write mono Int16 PCM to a WAV file, tracking position for sync.

    When ``append=True`` and the file already exists, new audio is appended
    and the RIFF/data headers are patched on close so the WAV remains valid.

    If the existing file's sample rate differs from the new capture rate,
    incoming audio is automatically resampled to match the file's rate so
    the WAV stays consistent (prevents chipmunk / slow-motion playback).
    """

    def __init__(self, path: str, sample_rate: int, append: bool = False) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._path = path
        self._sample_rate = sample_rate   # rate of the WAV file on disk
        self._input_rate = sample_rate    # rate of incoming audio data
        self._wf = None    # wave.open handle (new files only)
        self._raw = None   # raw binary handle (append mode only)
        self._resample_up = 1
        self._resample_down = 1
        self._closed = False

        # Tracks position in the *caller's* sample rate so that sample
        # offsets returned by write() stay consistent with the transcriber's
        # clock regardless of any WAV-side resampling.
        self._input_samples_written = 0

        if append and os.path.isfile(path):
            existing_rate = sample_rate
            existing_channels = 1
            try:
                with wave.open(path, "rb") as wf:
                    self._total_samples = wf.getnframes()
                    existing_rate = wf.getframerate()
                    existing_channels = wf.getnchannels()
            except Exception:
                self._total_samples = 0

            if existing_channels != 1:
                log.warn("audio", f"WAV file has {existing_channels} channels, "
                                  f"expected mono - file may be corrupted on append")

            # If the existing file has a different sample rate, resample
            # incoming audio to match so playback speed stays correct.
            if existing_rate != sample_rate:
                self._sample_rate = existing_rate
                g = gcd(existing_rate, sample_rate)
                self._resample_up = existing_rate // g
                self._resample_down = sample_rate // g
                log.warn("audio",
                         f"WAV sample rate mismatch: file={existing_rate} Hz, "
                         f"capture={sample_rate} Hz - resampling to {existing_rate} Hz")

            # Convert existing WAV samples to equivalent input-rate samples
            # so offset tracking is continuous across recordings.
            if self._resample_up != self._resample_down:
                self._input_samples_written = int(
                    self._total_samples * self._resample_down / self._resample_up
                )
            else:
                self._input_samples_written = self._total_samples

            # Open for raw binary append - write PCM after existing data
            self._raw = open(path, "r+b")
            self._raw.seek(0, 2)  # seek to end
        else:
            self._wf = wave.open(path, "wb")
            self._wf.setnchannels(1)
            self._wf.setsampwidth(2)  # 16-bit
            self._wf.setframerate(sample_rate)
            self._total_samples = 0

    @property
    def total_samples(self) -> int:
        return self._total_samples

    @property
    def sample_rate(self) -> int:
        """The sample rate of the WAV file on disk."""
        return self._sample_rate

    @property
    def elapsed_seconds(self) -> float:
        return self._total_samples / self._sample_rate

    def write(self, int16_bytes: bytes) -> int:
        """Write PCM data.  Returns the sample offset *before* this write,
        expressed in the input (capture) sample rate so the transcriber can
        compute wall-clock timestamps via ``offset / capture_rate``.
        """
        if self._closed or (self._wf is None and self._raw is None):
            return -1

        # Count input samples before any resampling
        input_sample_count = len(int16_bytes) // 2
        offset = self._input_samples_written

        # Resample if the capture rate differs from the WAV file rate
        if self._resample_up != 1 or self._resample_down != 1:
            samples = np.frombuffer(int16_bytes, dtype=np.int16).astype(np.float32)
            resampled = resample_poly(
                samples, self._resample_up, self._resample_down,
            ).astype(np.float32)
            int16_bytes = np.clip(resampled, -32768, 32767).astype(np.int16).tobytes()

        if self._wf is not None:
            self._wf.writeframes(int16_bytes)
        else:
            self._raw.write(int16_bytes)

        # Track both WAV-file samples and input-rate samples
        self._total_samples += len(int16_bytes) // 2
        self._input_samples_written += input_sample_count
        return offset

    def close(self) -> None:
        """Finalize and close the WAV file.  Safe to call multiple times."""
        if self._closed:
            return
        self._closed = True

        if self._wf is not None:
            self._wf.close()
            self._wf = None
        if self._raw is not None:
            # Patch RIFF and data chunk sizes so the WAV is valid
            self._raw.flush()
            data_size = self._total_samples * 2  # 16-bit mono = 2 bytes/sample
            riff_size = data_size + 36  # 36 = header bytes after RIFF size field
            self._raw.seek(4)
            self._raw.write(struct.pack("<I", riff_size))
            self._raw.seek(40)
            self._raw.write(struct.pack("<I", data_size))
            self._raw.close()
            self._raw = None
