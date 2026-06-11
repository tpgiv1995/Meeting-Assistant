"""Compute device selection — single source of truth for CUDA/MPS/CPU choice.

Used by diarizer, transcriber, batch transcriber, speaker_db, and the app
settings layer so every component agrees on which torch device to target.
"""
from __future__ import annotations

import functools


@functools.lru_cache(maxsize=1)
def best_torch_device() -> str:
    """Return the best available torch device string: 'cuda', 'mps', or 'cpu'.

    Result is cached for the process lifetime — torch's availability checks are
    not cheap and the answer never changes after startup.
    """
    try:
        import torch
    except ImportError:
        return "cpu"

    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def is_gpu_device(device: str) -> bool:
    """True for any non-CPU accelerator device string."""
    return device in ("cuda", "mps")


def empty_cache(device: str) -> None:
    """Best-effort cache flush for the given torch device. No-op on CPU."""
    try:
        import torch
    except ImportError:
        return

    if device == "cuda":
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    elif device == "mps":
        # torch.mps.empty_cache() exists on PyTorch >= 2.0 with MPS support.
        mps_ns = getattr(torch, "mps", None)
        if mps_ns is not None and hasattr(mps_ns, "empty_cache"):
            try:
                mps_ns.empty_cache()
            except Exception:
                pass
