"""
Online speaker diarization using pyannote segmentation + embeddings.

StreamingDiarizer buffers audio in a rolling window and processes it in
configurable steps, returning only the latest step's annotation each call
to avoid duplicate segments across calls.  Labels are consistent
session-wide: "Speaker 1", "Speaker 2", etc.

This replaces the previous diart-based implementation with a lightweight
wrapper that uses pyannote 3.x models directly, eliminating the stagnating
diart dependency while preserving the same streaming interface and adding
overlap-aware speaker detection.
"""
import itertools
import math
import time
import traceback
import warnings

from core import log as log
import numpy as np
import torch
import torchaudio

# torchaudio 2.x removed several symbols that older pyannote.audio references
# at import time.  Shim them before pyannote is imported - we never use
# torchaudio for file I/O so these stubs are never actually called.
if not hasattr(torchaudio, "list_audio_backends"):
    torchaudio.list_audio_backends = lambda: ["soundfile"]
if not hasattr(torchaudio, "set_audio_backend"):
    torchaudio.set_audio_backend = lambda backend: None   # no-op; backend selection removed in 2.x
if not hasattr(torchaudio, "AudioMetaData"):
    import collections
    torchaudio.AudioMetaData = collections.namedtuple(
        "AudioMetaData",
        ["sample_rate", "num_frames", "num_channels", "bits_per_sample", "encoding"],
    )

# huggingface_hub 1.x renamed 'use_auth_token' → 'token'.  pyannote.audio 3.x
# and diart still pass the old name internally.  Shim hf_hub_download to accept
# both so we don't need to patch third-party source files.
def _compat_hf_hub_download(*args, use_auth_token=None, token=None, **kwargs):
    """Accept legacy use_auth_token kwarg; treat '' as None to avoid illegal header."""
    effective = token or use_auth_token or None
    return _orig_hf_hub_download(*args, token=effective, **kwargs)

try:
    import huggingface_hub as _hfh
    import inspect as _inspect
    _orig_hf_hub_download = _hfh.hf_hub_download
    if "use_auth_token" not in _inspect.signature(_orig_hf_hub_download).parameters:
        _hfh.hf_hub_download = _compat_hf_hub_download
except Exception:
    _orig_hf_hub_download = None  # Shim will be a no-op if called

# PyTorch 2.6 changed torch.load's default to weights_only=True, which blocks
# pyannote/lightning checkpoints that embed custom Python objects (TorchVersion,
# Specifications, Problem, etc.). Enumerating all safe globals is brittle, so we
# patch torch.load itself to use weights_only=False for these trusted local files.
try:
    import torch as _torch
    _orig_torch_load = _torch.load

    def _patched_torch_load(f, map_location=None, pickle_module=None,
                             weights_only=None, mmap=None, **kwargs):
        # Pass weights_only=False unless the caller explicitly sets it to True
        effective = False if weights_only is None else weights_only
        kw = dict(kwargs)
        if map_location is not None:
            kw["map_location"] = map_location
        if pickle_module is not None:
            kw["pickle_module"] = pickle_module
        if mmap is not None:
            kw["mmap"] = mmap
        return _orig_torch_load(f, weights_only=effective, **kw)

    _torch.load = _patched_torch_load
except Exception:
    pass

# Suppress the long torchcodec warning - irrelevant since we always pass
# pre-loaded waveform tensors, never file paths.
warnings.filterwarnings(
    "ignore",
    message="torchaudio._backend.set_audio_backend has been deprecated",
    category=UserWarning,
)
with warnings.catch_warnings():
    warnings.filterwarnings("ignore", category=UserWarning, module="pyannote")
    from pyannote.audio import Model as _PyannoteModel

# Patch the hf_hub_download reference that pyannote.audio already bound at
# import time - the global shim above only covers future callers.
try:
    import pyannote.audio.core.model as _pa_model
    if hasattr(_pa_model, "hf_hub_download"):
        _pa_model.hf_hub_download = _compat_hf_hub_download
    import pyannote.audio.core.io as _pa_io
    if hasattr(_pa_io, "hf_hub_download"):
        _pa_io.hf_hub_download = _compat_hf_hub_download
    import pyannote.audio.pipelines.utils.hook as _pa_hook
    if hasattr(_pa_hook, "hf_hub_download"):
        _pa_hook.hf_hub_download = _compat_hf_hub_download
except Exception:
    pass

# Suppress benign PyTorch std() warning from pyannote's pooling layer.
# Fires at recording start when the audio buffer has only 1 time step and
# std(correction=1) has 0 degrees of freedom. Pyannote recovers gracefully.
warnings.filterwarnings(
    "ignore",
    message="std\\(\\): degrees of freedom is <= 0",
    category=UserWarning,
)
warnings.filterwarnings(
    "ignore",
    message="Module 'speechbrain\\.",
    category=UserWarning,
)
warnings.filterwarnings(
    "ignore",
    message="Mismatch between frames",
    category=UserWarning,
    module="pyannote",
)

# ── Speechbrain lazy-module resilience ───────────────────────────────────────
# Newer speechbrain versions (>=1.1) use LazyModule for optional integrations
# (k2_fsa, huggingface, Kaldi, etc.).  If the optional dependency isn't
# installed the lazy import raises an opaque ImportError that kills the entire
# diarizer init - often triggered indirectly by inspect.stack() inside
# pytorch_lightning.
#
# Strategy: install a custom meta-path finder that intercepts ANY import under
# "speechbrain.integrations" (and the legacy "speechbrain.k2_integration") and
# returns an empty stub module.  This is future-proof - new sub-packages added
# by SpeechBrain updates won't require manual additions here.
import sys as _sys
import types as _types
import importlib.abc as _importlib_abc
import importlib.machinery as _importlib_machinery


class _SpeechBrainIntegrationStubFinder(_importlib_abc.MetaPathFinder):
    """Auto-stub any missing speechbrain.integrations.* submodule."""

    _PREFIXES = ("speechbrain.integrations", "speechbrain.k2_integration")

    def find_module(self, fullname, path=None):
        # find_module is the legacy protocol but still honoured; keep it for
        # broad Python 3.x compat alongside find_spec.
        if any(fullname == p or fullname.startswith(p + ".") for p in self._PREFIXES):
            if fullname not in _sys.modules:
                return self
        return None

    def load_module(self, fullname):
        if fullname in _sys.modules:
            return _sys.modules[fullname]
        mod = _types.ModuleType(fullname)
        mod.__path__ = []
        mod.__package__ = fullname
        mod.__loader__ = self
        _sys.modules[fullname] = mod
        return mod

    def find_spec(self, fullname, path, target=None):
        if any(fullname == p or fullname.startswith(p + ".") for p in self._PREFIXES):
            if fullname not in _sys.modules:
                return _importlib_machinery.ModuleSpec(fullname, self)
        return None

    def create_module(self, spec):
        return None  # use default semantics

    def exec_module(self, module):
        module.__path__ = []
        module.__package__ = module.__name__


# Install early, before any pyannote / diart import triggers speechbrain lazy loads
_sys.meta_path.insert(0, _SpeechBrainIntegrationStubFinder())

def _merge_turns(
    turns: list[tuple[str, float, float]],
    merge_gap: float = 0.1,
) -> list[tuple[str, float, float]]:
    """Merge consecutive same-speaker turns with small gaps between them."""
    if not turns:
        return []
    merged: list[tuple[str, float, float]] = [turns[0]]
    for label, start, end in turns[1:]:
        prev_label, prev_start, prev_end = merged[-1]
        if label == prev_label and (start - prev_end) <= merge_gap:
            merged[-1] = (prev_label, prev_start, end)
        else:
            merged.append((label, start, end))
    return merged


def _build_powerset_map(num_speakers: int, max_overlap: int) -> np.ndarray:
    """Build (num_powerset_classes, num_speakers) binary mapping matrix.

    Maps powerset class probabilities to per-speaker activity probabilities
    via matrix multiplication: speaker_probs = powerset_probs @ map.

    For segmentation-3.0 (3 speakers, max 2 overlap, 7 classes):
        class 0: {}           → [0, 0, 0]
        class 1: {spk0}       → [1, 0, 0]
        class 2: {spk1}       → [0, 1, 0]
        class 3: {spk2}       → [0, 0, 1]
        class 4: {spk0, spk1} → [1, 1, 0]
        class 5: {spk0, spk2} → [1, 0, 1]
        class 6: {spk1, spk2} → [0, 1, 1]
    """
    rows: list[np.ndarray] = []
    for k in range(max_overlap + 1):
        for combo in itertools.combinations(range(num_speakers), k):
            row = np.zeros(num_speakers, dtype=np.float32)
            for idx in combo:
                row[idx] = 1.0
            rows.append(row)
    return np.array(rows, dtype=np.float32)


def _mask_to_regions(
    mask: np.ndarray, frame_dur: float, offset: float = 0.0,
) -> list[tuple[float, float]]:
    """Convert a binary frame mask to a list of (start_sec, end_sec) regions."""
    if not mask.any():
        return []
    diff = np.diff(mask.astype(np.int8), prepend=0, append=0)
    starts = np.where(diff == 1)[0]
    ends = np.where(diff == -1)[0]
    return [(offset + s * frame_dur, offset + e * frame_dur)
            for s, e in zip(starts, ends)]


def _normalize(v: np.ndarray) -> np.ndarray:
    """L2-normalize a vector."""
    n = np.linalg.norm(v)
    return v / n if n > 1e-8 else v


# ── StreamingDiarizer ────────────────────────────────────────────────────────

class StreamingDiarizer:
    """
    Online speaker diarization using pyannote models directly.

    Replaces DiartDiarizer.  Uses pyannote's segmentation-3.0 model for voice
    activity and local speaker detection, wespeaker embeddings for speaker
    identification, and incremental centroid clustering for cross-window
    speaker consistency.

    Key improvements over the diart-based approach:
    - Overlap-aware: both speakers preserved during simultaneous speech
      (the old implementation discarded the second speaker at transitions)
    - No diart dependency: uses pyannote models directly
    - Same public interface: drop-in replacement for DiartDiarizer

    Measured latency: ~40 ms/step on GPU, ~150 ms/step on CPU.
    """

    SAMPLE_RATE = 16_000
    _MIN_EMBEDDING_SAMPLES = 8_000    # 0.5 s — minimum to attempt any match
    _MIN_CREATE_SAMPLES    = 16_000   # 1.0 s — minimum to spawn a new speaker
    # Splitting these matters: short clips give noisy embeddings, fine for
    # confirming an existing speaker (max(centroid, anchor) absorbs the
    # noise) but liable to spawn ghost speakers if treated as authoritative.

    def __init__(self, hf_token: str, device: str | None = None) -> None:
        from core.compute_device import best_torch_device
        if device is None:
            device = best_torch_device()
        elif device == "cuda" and not torch.cuda.is_available():
            # User has "cuda" saved but this machine has no CUDA — auto-route
            # to whatever accelerator is actually available (MPS on Mac, else CPU).
            # This keeps backward-compat with settings.json values from another
            # machine while letting the right hardware be used.
            device = best_torch_device()
        self._device_name = device
        self._dev = torch.device(device)
        log.info("diarizer", f"Loading streaming diarizer on {self._dev}…")

        # ── Neutralise speechbrain LazyModules already in sys.modules ────────
        try:
            from speechbrain.utils.importutils import LazyModule as _LM
            for _key in list(_sys.modules):
                if _key.startswith("speechbrain."):
                    _mod = _sys.modules[_key]
                    if isinstance(_mod, _LM):
                        _stub = _types.ModuleType(_key)
                        _stub.__path__ = []
                        _stub.__package__ = _key
                        _sys.modules[_key] = _stub
        except ImportError:
            pass

        # ── Load segmentation model ─────────────────────────────────────────
        log.info("diarizer", "Loading segmentation model…")
        try:
            self._seg_model = _PyannoteModel.from_pretrained(
                "pyannote/segmentation-3.0", use_auth_token=hf_token,
            )
            self._seg_model.eval()
            self._seg_model.to(self._dev)
        except Exception as e:
            raise RuntimeError(
                f"Failed to load segmentation model (pyannote/segmentation-3.0). "
                f"Check your HuggingFace token and network: {e}"
            ) from e

        # ── Load embedding model ────────────────────────────────────────────
        log.info("diarizer", "Loading embedding model…")
        try:
            self._emb_model = _PyannoteModel.from_pretrained(
                "pyannote/wespeaker-voxceleb-resnet34-LM",
                use_auth_token=hf_token,
            )
            self._emb_model.eval()
            self._emb_model.to(self._dev)
        except Exception as e:
            raise RuntimeError(
                f"Failed to load embedding model (wespeaker-voxceleb-resnet34-LM). "
                f"Check your HuggingFace token and network: {e}"
            ) from e

        # ── Load effective audio params ─────────────────────────────────────
        # resolve_audio_params() honours the active diarization preset, so
        # changes to the preset definitions in default_audio_params.py are
        # picked up automatically — users on a non-custom preset don't need
        # to re-select it after an update.
        from capture_audio.params import resolve_audio_params
        p = resolve_audio_params()

        self._step_seconds     = float(p["step_seconds"])
        self._duration_seconds = float(p["duration_seconds"])
        self._merge_gap        = float(p["merge_gap_seconds"])
        self._tau_active       = float(p["tau_active"])
        self._rho_update       = float(p["rho_update"])
        self._delta_new        = float(p["delta_new"])

        # ── Probe model structure ───────────────────────────────────────────
        duration_samples = int(self._duration_seconds * self.SAMPLE_RATE)
        with torch.no_grad():
            _test = self._seg_model(
                torch.zeros(1, 1, duration_samples, device=self._dev)
            )
        self._num_frames = _test.shape[1]
        num_ps_classes = _test.shape[2]
        self._frame_duration = self._duration_seconds / self._num_frames

        # Detect powerset structure from class count
        self._num_local_speakers, self._max_overlap = (
            self._detect_powerset(num_ps_classes)
        )
        self._powerset_map = _build_powerset_map(
            self._num_local_speakers, self._max_overlap
        )
        self._powerset_map_tensor = torch.from_numpy(
            self._powerset_map
        ).to(self._dev)

        log.info(
            "diarizer",
            f"Segmentation: {num_ps_classes} powerset -> "
            f"{self._num_local_speakers} local speakers, "
            f"max {self._max_overlap} overlap, "
            f"{self._num_frames} frames per {self._duration_seconds}s window",
        )

        # ── Internal state ──────────────────────────────────────────────────
        self._buf = np.zeros(0, dtype=np.float32)
        self._buf_start_sec = 0.0
        self._total_fed_sec = 0.0

        # Online clustering: global speaker label → {"centroid", "anchor"}.
        # Both are L2-normalised. The centroid drifts slowly with each match;
        # the anchor is the first embedding seen for the speaker and never
        # changes — matching uses max(sim_to_centroid, sim_to_anchor) so a
        # drifting centroid can't cause self-reinforcing misclassification.
        self._centroids: dict[str, dict[str, np.ndarray]] = {}
        self._next_label = 1

        # Fired after a successful merge_speakers() so consumers (e.g. the
        # transcriber's per-speaker prompt context) can mirror the merge.
        # Signature: (keep_label, merge_label) -> None.
        self.on_merge_speakers = None  # type: ignore[assignment]

        log.info("diarizer", f"Streaming diarizer ready on {self._dev}.")

    # ── Public ────────────────────────────────────────────────────────────────

    def process(
        self,
        audio: np.ndarray,
        new_from_samples: int = 0,
    ) -> list[tuple[str, float, float]]:
        """Feed one audio chunk to the streaming diarizer.

        Returns sorted [(speaker_label, start_sec, end_sec)] with timestamps
        relative to the start of ``audio``.  Returns [] until enough audio has
        been accumulated.

        Unlike the previous diart-based implementation, overlapping speakers
        are preserved — both speakers are emitted when simultaneous speech is
        detected.
        """
        if len(audio) < int(self.SAMPLE_RATE * 0.1):
            return []

        new_audio_abs_sec = self._total_fed_sec
        self._total_fed_sec += len(audio) / self.SAMPLE_RATE
        audio_duration_sec = len(audio) / self.SAMPLE_RATE

        self._buf = np.concatenate([self._buf, audio])

        duration_samples = int(self._duration_seconds * self.SAMPLE_RATE)
        step_samples     = int(self._step_seconds     * self.SAMPLE_RATE)

        all_segs: list[tuple[str, float, float]] = []

        while len(self._buf) >= duration_samples:
            chunk = self._buf[:duration_samples]

            # 1) Run segmentation → per-speaker activity probabilities
            speaker_activity = self._run_segmentation(chunk)

            if speaker_activity is not None:
                # 2) Extract embeddings, cluster, emit step-clipped segments
                step_end_abs   = self._buf_start_sec + self._duration_seconds
                step_start_abs = step_end_abs - self._step_seconds

                window_segs = self._process_window(
                    chunk, speaker_activity, self._buf_start_sec,
                    step_start_abs, step_end_abs,
                    new_audio_abs_sec, audio_duration_sec,
                )
                all_segs.extend(window_segs)

            # Advance the buffer by one step
            self._buf = self._buf[step_samples:]
            self._buf_start_sec += self._step_seconds

        all_segs.sort(key=lambda x: x[1])
        return _merge_turns(all_segs, self._merge_gap)

    def reset(self, next_label: int = 1) -> None:
        """Clear all state for a new recording session.

        Args:
            next_label: Starting number for new speaker labels.  On resume,
                        pass max-existing + 1 so new speakers don't collide
                        with labels from previous recording segments.
        """
        self._buf = np.zeros(0, dtype=np.float32)
        self._buf_start_sec = 0.0
        self._total_fed_sec = 0.0
        self._centroids.clear()
        self._next_label = next_label

    def apply_params(self, params: dict) -> None:
        """Update runtime-tunable diarization parameters.

        step_seconds and duration_seconds are baked into the segmentation
        windowing at init time — changing them requires a diarizer reload.
        Everything else (merge_gap, tau_active, rho_update, delta_new) is
        consulted on every call and can be tuned mid-session without a
        restart.
        """
        self._merge_gap   = float(params.get("merge_gap_seconds", self._merge_gap))
        self._tau_active  = float(params.get("tau_active",        self._tau_active))
        self._rho_update  = float(params.get("rho_update",        self._rho_update))
        self._delta_new   = float(params.get("delta_new",         self._delta_new))

    def merge_speakers(self, keep_label: str, merge_label: str) -> None:
        """Merge one speaker's centroid into another. Anchor of `keep` is preserved."""
        if merge_label not in self._centroids:
            return
        if keep_label in self._centroids:
            keep = self._centroids[keep_label]
            merge = self._centroids[merge_label]
            keep["centroid"] = _normalize(keep["centroid"] + merge["centroid"])
        else:
            self._centroids[keep_label] = self._centroids[merge_label]
        del self._centroids[merge_label]
        # Notify consumers so per-speaker state (e.g. transcriber prompt
        # contexts) can mirror the merge.  Failures here must not break
        # the caller — the centroid merge has already succeeded.
        if self.on_merge_speakers is not None:
            try:
                self.on_merge_speakers(keep_label, merge_label)
            except Exception:
                log.error("diarizer", "on_merge_speakers callback failed:")
                traceback.print_exc()

    # ── Private ───────────────────────────────────────────────────────────────

    @staticmethod
    def _detect_powerset(num_classes: int) -> tuple[int, int]:
        """Infer (num_speakers, max_overlap) from the powerset class count."""
        for n in range(1, 8):
            for k in range(1, n + 1):
                total = sum(math.comb(n, i) for i in range(k + 1))
                if total == num_classes:
                    return n, k
        raise RuntimeError(
            f"Cannot determine powerset structure from {num_classes} classes"
        )

    def _run_segmentation(self, audio_chunk: np.ndarray) -> np.ndarray | None:
        """Run segmentation and return per-speaker activity (num_frames, num_local_speakers)."""
        try:
            waveform = torch.from_numpy(audio_chunk).float()
            waveform = waveform.unsqueeze(0).unsqueeze(0).to(self._dev)
            with torch.no_grad():
                logits = self._seg_model(waveform)
                probs = torch.softmax(logits, dim=-1)
                activity = torch.matmul(probs, self._powerset_map_tensor)
            return activity[0].cpu().numpy()
        except Exception:
            log.error("diarizer", "Segmentation failed:")
            traceback.print_exc()
            return None

    def _process_window(
        self,
        chunk: np.ndarray,
        speaker_activity: np.ndarray,
        buf_start: float,
        step_start: float,
        step_end: float,
        new_audio_abs: float,
        audio_duration: float,
    ) -> list[tuple[str, float, float]]:
        """Process one window: extract embeddings, cluster, emit step-clipped segments."""
        segments: list[tuple[str, float, float]] = []

        for local_spk in range(self._num_local_speakers):
            active_mask = speaker_activity[:, local_spk] > self._tau_active
            if not active_mask.any():
                continue

            # All active regions in the full window (for embedding quality)
            regions = _mask_to_regions(
                active_mask, self._frame_duration, buf_start,
            )
            if not regions:
                continue

            # Concatenate all active audio for this local speaker
            parts: list[np.ndarray] = []
            for rstart, rend in regions:
                s = max(0, int((rstart - buf_start) * self.SAMPLE_RATE))
                e = min(len(chunk), int((rend - buf_start) * self.SAMPLE_RATE))
                if e > s:
                    parts.append(chunk[s:e])
            if not parts:
                continue

            combined = np.concatenate(parts)
            if len(combined) < self._MIN_EMBEDDING_SAMPLES:
                continue

            # Extract embedding and match to global speaker.
            # ``can_create`` gates new-speaker spawning on a longer minimum
            # clip — short embeddings can still match existing speakers but
            # never create new ones (avoids spawning ghosts from noisy frags).
            embedding = self._extract_embedding(combined)
            if embedding is None:
                continue
            can_create = len(combined) >= self._MIN_CREATE_SAMPLES

            global_label = self._match_or_create(embedding, can_create=can_create)
            if global_label is None:
                continue  # short clip with no match found — drop the segment

            # Emit only segments that overlap with the latest step window
            # and with the newly received audio
            for rstart, rend in regions:
                clipped_start = max(rstart, step_start)
                clipped_end   = min(rend,   step_end)
                if clipped_end - clipped_start < 0.05:
                    continue
                if clipped_end <= new_audio_abs:
                    continue

                rel_start = max(0.0, clipped_start - new_audio_abs)
                rel_end   = min(audio_duration, clipped_end - new_audio_abs)
                if rel_end > rel_start:
                    segments.append((global_label, rel_start, rel_end))

        return segments

    def _extract_embedding(self, audio: np.ndarray) -> np.ndarray | None:
        """Extract a 256-dim L2-normalised speaker embedding."""
        try:
            # Call the embedding model directly rather than through pyannote's
            # Inference wrapper, which rebuilds tensors on CPU internally.
            waveform = torch.from_numpy(audio).float().unsqueeze(0).unsqueeze(0)
            # shape: (batch=1, channels=1, samples)
            waveform = waveform.to(self._dev)
            with torch.no_grad():
                emb_tensor = self._emb_model(waveform)
            emb = emb_tensor.squeeze().cpu().numpy().astype(np.float32)
            return _normalize(emb)
        except Exception as exc:
            log.error("diarizer", f"Embedding extraction failed: {exc}")
            return None

    def _match_or_create(
        self, embedding: np.ndarray, can_create: bool = True,
    ) -> str | None:
        """Match an embedding to an existing speaker or create a new one.

        Each speaker keeps both a drifting `centroid` (running average) and an
        immutable `anchor` (the first embedding observed for that speaker).
        Matching uses ``max(sim_to_centroid, sim_to_anchor)`` so a centroid
        that has drifted toward another voice can't cause permanent
        misclassification — the anchor still gives the original distance.

        - New speaker if best similarity < ``1 - delta_new``.
        - Otherwise label as the best match. The centroid is only updated
          when the match is *confidently* above threshold (hysteresis margin),
          so borderline matches don't poison the centroid.
        - When ``can_create`` is False, a low-confidence embedding will not
          spawn a new speaker; if it doesn't match anything well we return
          None so the caller can drop the segment instead of creating a ghost.
        """
        if not self._centroids:
            return self._create_new(embedding) if can_create else None

        best_sim = -1.0
        best_label = ""
        for label, info in self._centroids.items():
            sim = max(
                float(np.dot(embedding, info["centroid"])),
                float(np.dot(embedding, info["anchor"])),
            )
            if sim > best_sim:
                best_sim = sim
                best_label = label

        similarity_threshold = 1.0 - self._delta_new
        if best_sim < similarity_threshold:
            return self._create_new(embedding) if can_create else None

        # Hysteresis: only update the centroid when the match is clearly above
        # threshold (margin = 0.10 on cosine sim). This prevents drift from
        # ambiguous matches gradually merging two voices into one centroid.
        update_threshold = similarity_threshold + 0.10
        if best_sim >= update_threshold:
            info = self._centroids[best_label]
            info["centroid"] = _normalize(
                info["centroid"] * (1.0 - self._rho_update)
                + embedding * self._rho_update
            )
        return best_label

    def _create_new(self, embedding: np.ndarray) -> str:
        label = f"Speaker {self._next_label}"
        self._next_label += 1
        self._centroids[label] = {
            "centroid": embedding.copy(),
            "anchor": embedding.copy(),
        }
        return label


# Backward-compat alias — transcriber.py historically imported DiartDiarizer
DiartDiarizer = StreamingDiarizer
