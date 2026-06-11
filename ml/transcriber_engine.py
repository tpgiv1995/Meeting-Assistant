"""Whisper engine abstraction — picks the right inference backend per platform.

Engines:
- FasterWhisperEngine: Windows (CUDA) or Linux. CTranslate2-optimized.
- MlxWhisperEngine: macOS Apple Silicon. Apple's MLX framework, native Metal.

Both expose a .transcribe(audio_float32, **kwargs) method that returns
(segments_iter, info_dict). Segments have a .text attribute (and .start/.end
when word_timestamps=False; both engines return at the segment level
regardless). This shape mirrors faster-whisper's API exactly so the existing
transcriber.py post-processing pipeline keeps working unchanged.

Selection is automatic based on sys.platform — no config flags.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any, Iterable, Iterator

from core import log as log


# ── Common segment shape ────────────────────────────────────────────────────

@dataclass
class _Segment:
    """Minimal segment shape compatible with faster-whisper's Segment dataclass."""
    text: str
    start: float = 0.0
    end: float = 0.0
    compression_ratio: float = 0.0
    no_speech_prob: float = 0.0


# ── Engine protocol ─────────────────────────────────────────────────────────

class _EngineBase:
    """Public surface every engine implements."""

    def transcribe(self, audio, **kwargs) -> tuple[Iterator[_Segment], dict]:
        raise NotImplementedError

    def reload(self, device: str, compute_type: str, model_size: str) -> None:
        raise NotImplementedError


# ── faster-whisper backend (Windows / CUDA / CPU) ───────────────────────────

class FasterWhisperEngine(_EngineBase):
    """Wraps faster_whisper.WhisperModel. Native passthrough — no transformation."""

    def __init__(self, model_size: str, device: str, compute_type: str):
        from faster_whisper import WhisperModel  # type: ignore[import-not-found]
        self._WhisperModel = WhisperModel
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self._inner = WhisperModel(
            model_size, device=device, compute_type=compute_type,
            local_files_only=True,
        )

    def transcribe(self, audio, **kwargs):
        # Pass straight through — faster-whisper's signature is the canonical
        # one we standardize on, so no kwarg translation needed here.
        segments, info = self._inner.transcribe(audio, **kwargs)
        if hasattr(info, "_asdict"):
            info_dict = info._asdict()
        elif hasattr(info, "__dict__"):
            info_dict = dict(info.__dict__)
        else:
            try:
                info_dict = dict(info or {})
            except TypeError:
                info_dict = {}
        return segments, info_dict

    def reload(self, device: str, compute_type: str, model_size: str) -> None:
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self._inner = self._WhisperModel(
            model_size, device=device, compute_type=compute_type,
            local_files_only=True,
        )


# ── mlx-whisper backend (macOS Apple Silicon) ───────────────────────────────

# Map our internal model size identifiers to MLX community model repo IDs.
# These are pre-quantized for MLX and pulled from HuggingFace on first use.
_MLX_MODEL_REPOS = {
    "large-v3":       "mlx-community/whisper-large-v3-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    "medium":         "mlx-community/whisper-medium-mlx",
    "small":          "mlx-community/whisper-small-mlx",
    "tiny":           "mlx-community/whisper-tiny-mlx",
}


class MlxWhisperEngine(_EngineBase):
    """Wraps mlx_whisper.transcribe(). Adapts dict result to faster-whisper-shape segments.

    mlx-whisper differences vs faster-whisper:
    - Returns a dict {"text", "segments", "language"} rather than (iter, info)
    - No `vad_filter` parameter (Whisper's built-in VAD only)
    - No `beam_size` parameter at the top-level transcribe() call
    - `condition_on_previous_text` and `initial_prompt` are supported
    - `compression_ratio_threshold` is supported

    We map kwargs accordingly and emit our internal _Segment objects.
    """

    def __init__(self, model_size: str, device: str = "mlx", compute_type: str = "fp16"):
        try:
            import mlx_whisper  # type: ignore[import-not-found]
        except ImportError as e:
            raise ImportError(
                "mlx-whisper is not installed. Run `pip install mlx-whisper` "
                "or reinstall requirements.txt on macOS."
            ) from e
        self._mlx_whisper = mlx_whisper
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self._repo = _MLX_MODEL_REPOS.get(model_size, _MLX_MODEL_REPOS["small"])
        log.info("whisper", f"mlx-whisper model: {self._repo}")

    def transcribe(self, audio, **kwargs):
        # Translate faster-whisper kwargs to mlx-whisper's signature.
        # Drop unsupported kwargs silently — mlx-whisper would reject them.
        mlx_kwargs: dict[str, Any] = {
            "path_or_hf_repo": self._repo,
        }
        if "language" in kwargs:
            mlx_kwargs["language"] = kwargs["language"]
        if "initial_prompt" in kwargs:
            mlx_kwargs["initial_prompt"] = kwargs["initial_prompt"]
        if "condition_on_previous_text" in kwargs:
            mlx_kwargs["condition_on_previous_text"] = kwargs["condition_on_previous_text"]
        if "compression_ratio_threshold" in kwargs:
            mlx_kwargs["compression_ratio_threshold"] = kwargs["compression_ratio_threshold"]
        if "word_timestamps" in kwargs:
            mlx_kwargs["word_timestamps"] = kwargs["word_timestamps"]
        # vad_filter / vad_parameters / beam_size are intentionally not passed —
        # mlx-whisper doesn't expose them. The transcriber's chunk-level silence
        # detection handles the VAD job upstream.

        result = self._mlx_whisper.transcribe(audio, **mlx_kwargs)
        raw_segments: Iterable[dict] = result.get("segments", []) or []
        segments: list[_Segment] = [
            _Segment(
                text=str(seg.get("text", "")),
                start=float(seg.get("start", 0.0) or 0.0),
                end=float(seg.get("end", 0.0) or 0.0),
                compression_ratio=float(seg.get("compression_ratio", 0.0) or 0.0),
                no_speech_prob=float(seg.get("no_speech_prob", 0.0) or 0.0),
            )
            for seg in raw_segments
        ]
        info = {
            "language": result.get("language", "en"),
            "duration": result.get("duration", 0.0),
        }
        return iter(segments), info

    def reload(self, device: str, compute_type: str, model_size: str) -> None:
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self._repo = _MLX_MODEL_REPOS.get(model_size, _MLX_MODEL_REPOS["small"])
        log.info("whisper", f"mlx-whisper model: {self._repo}")
        # mlx-whisper loads the model lazily inside transcribe() based on the
        # repo path, so there's nothing to "reload" eagerly here.


# ── Factory / platform dispatch ──────────────────────────────────────────────

def make_engine(model_size: str, device: str, compute_type: str) -> _EngineBase:
    """Return the right Whisper engine for this platform.

    On macOS (sys.platform == 'darwin') always returns MlxWhisperEngine,
    regardless of the requested device/compute_type — those are Windows
    concepts. On Windows/Linux returns FasterWhisperEngine.
    """
    if sys.platform == "darwin":
        return MlxWhisperEngine(model_size, device=device, compute_type=compute_type)
    return FasterWhisperEngine(model_size, device=device, compute_type=compute_type)


# ── Per-platform default model selection ─────────────────────────────────────

def default_model_config() -> tuple[str, str, str]:
    """Return (device, compute_type, model_size) appropriate for this platform.

    macOS Apple Silicon: ("mlx", "fp16", "large-v3")  — Metal can handle it.
    Windows + CUDA:      ("cuda", "float16", "large-v3")
    Windows CPU / Linux: ("cpu", "int8", "small")
    """
    if sys.platform == "darwin":
        return "mlx", "fp16", "large-v3"
    # Defer to the existing CUDA detection for non-darwin.
    try:
        from ml.transcriber import detect_cuda_available  # local import to avoid cycles
    except Exception:
        detect_cuda_available = lambda: False  # noqa: E731
    if detect_cuda_available():
        return "cuda", "float16", "large-v3"
    return "cpu", "int8", "small"
