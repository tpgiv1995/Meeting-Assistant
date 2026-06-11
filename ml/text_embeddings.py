"""Semantic text embeddings for session search.

Uses sentence-transformers (all-MiniLM-L6-v2) to encode session text into
dense vectors, enabling similarity-based search that understands meaning
rather than just keyword matches.

The model is loaded lazily on first use to avoid slowing down app startup.
"""
import logging
import struct
import threading
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

_model = None
_model_lock = threading.Lock()
_loading = False

# all-MiniLM-L6-v2: 384-dim, fast, ~80 MB download
MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
EMBED_DIM = 384


def _load_model():
    """Load the sentence-transformers model (blocking, called once)."""
    global _model, _loading
    with _model_lock:
        if _model is not None:
            return
        _loading = True
    try:
        # Suppress noisy HF Hub / model-load warnings
        import os
        import warnings
        warnings.filterwarnings("ignore", message=".*unauthenticated.*")
        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
        for _logger_name in ("huggingface_hub", "sentence_transformers",
                              "transformers", "safetensors"):
            logging.getLogger(_logger_name).setLevel(logging.ERROR)

        from sentence_transformers import SentenceTransformer
        log.info("Loading text embedding model %s …", MODEL_NAME)
        m = SentenceTransformer(MODEL_NAME)
        with _model_lock:
            _model = m
            _loading = False
        log.info("Text embedding model loaded.")
    except Exception as e:
        log.error("Failed to load text embedding model: %s", e)
        with _model_lock:
            _loading = False


def is_ready() -> bool:
    """True if the model is loaded and available."""
    return _model is not None


def is_loading() -> bool:
    return _loading


def ensure_loaded():
    """Load the model if not already loaded (blocking)."""
    if _model is None:
        _load_model()


def encode(text: str) -> np.ndarray | None:
    """Encode a text string into a normalized embedding vector.

    Returns a float32 ndarray of shape (EMBED_DIM,), or None if model unavailable.
    """
    if _model is None:
        return None
    # Truncate to ~512 tokens worth of text (~2500 chars) to stay within model limits
    truncated = text[:3000]
    try:
        vec = _model.encode(truncated, normalize_embeddings=True)
        return vec.astype(np.float32)
    except Exception as e:
        log.error("Embedding encode error: %s", e)
        return None


def encode_batch(texts: list[str]) -> list[np.ndarray] | None:
    """Encode multiple texts at once (more efficient than one-by-one)."""
    if _model is None:
        return None
    truncated = [t[:3000] for t in texts]
    try:
        vecs = _model.encode(truncated, normalize_embeddings=True, batch_size=32)
        return [v.astype(np.float32) for v in vecs]
    except Exception as e:
        log.error("Batch embedding encode error: %s", e)
        return None


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two normalized vectors (just dot product)."""
    return float(np.dot(a, b))


def embedding_to_bytes(vec: np.ndarray) -> bytes:
    """Serialize a float32 embedding to bytes for SQLite BLOB storage."""
    return vec.tobytes()


def bytes_to_embedding(data: bytes) -> np.ndarray:
    """Deserialize bytes back to a float32 embedding."""
    return np.frombuffer(data, dtype=np.float32).copy()
