"""
Cross-session speaker fingerprint database.

Stores 256-dim L2-normalized embeddings for each global speaker profile and
maintains an incremental centroid for fast cosine-similarity matching.

Usage:
    db = SpeakerFingerprintDB(storage.DB_PATH, hf_token)
    global_id = db.create_global_speaker("Alice")
    emb = db.extract_embedding(audio_np)          # float32 mono 16 kHz
    db.add_embedding(global_id, session_id, speaker_key, emb, duration_sec)
    matches = db.find_matches(emb)                # [{global_id, name, similarity, ...}]
"""
import base64
import sqlite3
import traceback
import uuid
import warnings
from contextlib import contextmanager
from datetime import datetime
import logging
import sys
from pathlib import Path

import numpy as np

from core import log as log

_SUGGEST_THRESHOLD    = 0.70   # cosine sim → push fingerprint_match SSE
_AUTO_APPLY_THRESHOLD = 0.82   # cosine sim → apply silently
_MIN_DURATION_SEC     = 2.5    # minimum audio before extracting embedding
_EMB_DIM              = 256    # WeSpeaker embedding dimension

_SPEAKER_PALETTE = [
    '#58a6ff', '#f47067', '#00b464', '#d2a8ff', '#f0883e', '#db61a2',
    '#e3b341', '#2dd4bf', '#a78bfa', '#79c0ff', '#ef6e4e', '#86e89d',
    '#f6c177', '#6cb6ff', '#ff9bce', '#768390',
]

_SUPPRESSED_LOAD_SUBSTRINGS = (
    "Warning: You are sending unauthenticated requests to the HF Hub.",
    "Please set a HF_TOKEN to enable higher rate limits and faster downloads.",
    "BertModel LOAD REPORT",
    "embeddings.position_ids | UNEXPECTED",
    "UNEXPECTED    :can be ignored",
)


class _FilteredStream:
    def __init__(self, wrapped):
        self._wrapped = wrapped
        self._buffer = ""

    def write(self, data):
        if not data:
            return 0
        self._buffer += data
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if not any(s in line for s in _SUPPRESSED_LOAD_SUBSTRINGS):
                self._wrapped.write(line + "\n")
        return len(data)

    def flush(self):
        if self._buffer and not any(s in self._buffer for s in _SUPPRESSED_LOAD_SUBSTRINGS):
            self._wrapped.write(self._buffer)
        self._buffer = ""
        self._wrapped.flush()

    def isatty(self):
        return getattr(self._wrapped, "isatty", lambda: False)()


@contextmanager
def _suppress_model_load_noise():
    import os

    warnings.filterwarnings("ignore", message=".*unauthenticated.*")
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    for logger_name in ("huggingface_hub", "sentence_transformers", "transformers", "safetensors"):
        logging.getLogger(logger_name).setLevel(logging.ERROR)

    orig_stdout, orig_stderr = sys.stdout, sys.stderr
    try:
        sys.stdout = _FilteredStream(orig_stdout)
        sys.stderr = _FilteredStream(orig_stderr)
        yield
    finally:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        finally:
            sys.stdout = orig_stdout
            sys.stderr = orig_stderr


@contextmanager
def _conn(db_path: Path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 3000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.utcnow().isoformat()


def _emb_to_blob(emb: np.ndarray) -> bytes:
    return emb.astype(np.float32).tobytes()


def _blob_to_emb(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32).copy()


def _normalize(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v / n if n > 1e-8 else v


class SpeakerFingerprintDB:
    """
    Persistent cross-session speaker identity database.

    Thread-safe: each method opens its own short-lived SQLite connection.
    All embedding vectors are L2-normalized float32 (256,).
    Cosine similarity == dot product for L2-normalized vectors.
    """

    SUGGEST_THRESHOLD    = _SUGGEST_THRESHOLD
    AUTO_APPLY_THRESHOLD = _AUTO_APPLY_THRESHOLD
    MIN_DURATION_SEC     = _MIN_DURATION_SEC

    def __init__(self, db_path: Path, hf_token: str, device: str = "cpu") -> None:
        self._db_path = db_path
        self._ready   = False
        self._inference = None

        self._backfill_colors()

        if not hf_token:
            log.warn("fingerprint", "No HF token - voice library disabled.")
            return

        try:
            # PyTorch 2.6: patch torch.load to use weights_only=False for trusted
            # local pyannote checkpoints (many internal globals, not enumerable).
            import torch as _torch
            if not getattr(_torch.load, "_patched_weights_only", False):
                _orig = _torch.load
                def _patched(f, map_location=None, pickle_module=None,
                              weights_only=None, mmap=None, **kw):
                    if map_location is not None: kw["map_location"] = map_location
                    if pickle_module is not None: kw["pickle_module"] = pickle_module
                    if mmap is not None: kw["mmap"] = mmap
                    return _orig(f, weights_only=(False if weights_only is None else weights_only), **kw)
                _patched._patched_weights_only = True
                _torch.load = _patched
        except Exception:
            pass

        try:
            with _suppress_model_load_noise():
                from pyannote.audio import Inference, Model  # type: ignore
                log.info("fingerprint", "Loading embedding model…")
                model = Model.from_pretrained(
                    "pyannote/wespeaker-voxceleb-resnet34-LM",
                    use_auth_token=hf_token,
                )
                self._inference = Inference(model, window="whole")
            # Move to requested device
            if device and device != "cpu":
                import torch
                self._inference.model = self._inference.model.to(torch.device(device))
            self._ready = True
            log.info("fingerprint", f"Embedding model ready on {device}.")
        except Exception as e:
            log.warn("fingerprint", f"Could not load embedding model: {e}")

    # ── Public helpers ────────────────────────────────────────────────────────

    def _backfill_colors(self) -> None:
        """Assign palette colors to any global speakers that have NULL color."""
        try:
            with _conn(self._db_path) as c:
                rows = c.execute(
                    "SELECT id FROM global_speakers WHERE color IS NULL OR color = '' "
                    "ORDER BY created_at"
                ).fetchall()
                if not rows:
                    return
                # Count existing colored speakers to offset palette index
                offset = c.execute(
                    "SELECT COUNT(*) FROM global_speakers WHERE color IS NOT NULL AND color != ''"
                ).fetchone()[0]
                for i, r in enumerate(rows):
                    color = _SPEAKER_PALETTE[(offset + i) % len(_SPEAKER_PALETTE)]
                    c.execute(
                        "UPDATE global_speakers SET color = ? WHERE id = ?",
                        (color, r["id"]),
                    )
            log.info("fingerprint", f"Backfilled colors for {len(rows)} speaker profile(s)")
        except Exception:
            pass  # DB may not exist yet on first run

    @property
    def ready(self) -> bool:
        return self._ready

    # ── Profile CRUD ──────────────────────────────────────────────────────────

    def create_global_speaker(self, name: str, color: str | None = None) -> str:
        """Create a new global profile. Returns global_id.
        Auto-assigns a palette color if none provided."""
        gid = uuid.uuid4().hex
        now = _now()
        if not color:
            with _conn(self._db_path) as c:
                count = c.execute("SELECT COUNT(*) FROM global_speakers").fetchone()[0]
            color = _SPEAKER_PALETTE[count % len(_SPEAKER_PALETTE)]
        with _conn(self._db_path) as c:
            c.execute(
                "INSERT INTO global_speakers (id, name, color, emb_count, created_at, updated_at) "
                "VALUES (?, ?, ?, 0, ?, ?)",
                (gid, name.strip(), color, now, now),
            )
        log.info("fingerprint", f"Created global profile: {name!r} ({gid[:8]})")
        return gid

    def rename_global_speaker(
        self,
        global_id: str,
        name: str | None = None,
        color: str | None = ...,  # type: ignore[assignment]
    ) -> dict:
        """Update name and/or color. Pass color=None to clear; omit to keep.
        Propagates changes to all linked speaker_labels rows.
        Returns resolved {name, color} of the profile."""
        with _conn(self._db_path) as c:
            if name is not None:
                c.execute(
                    "UPDATE global_speakers SET name=?, updated_at=? WHERE id=?",
                    (name.strip(), _now(), global_id),
                )
            if color is not ...:
                c.execute(
                    "UPDATE global_speakers SET color=?, updated_at=? WHERE id=?",
                    (color, _now(), global_id),
                )
            # Propagate resolved name/color to all linked speaker_labels
            row = c.execute(
                "SELECT name, color FROM global_speakers WHERE id = ?",
                (global_id,),
            ).fetchone()
            if row:
                c.execute(
                    "UPDATE speaker_labels SET name=?, color=? WHERE global_id=?",
                    (row["name"], row["color"], global_id),
                )
                return {"name": row["name"], "color": row["color"]}
        return {}

    def delete_global_speaker(self, global_id: str) -> None:
        """Delete profile and all embeddings. Nulls speaker_labels.global_id via FK cascade."""
        # Manually null the FK since SQLite ON DELETE CASCADE on speaker_labels
        # requires the FK to be set up on that table - we handle it explicitly.
        with _conn(self._db_path) as c:
            c.execute(
                "UPDATE speaker_labels SET global_id = NULL WHERE global_id = ?",
                (global_id,),
            )
            c.execute("DELETE FROM global_speakers WHERE id = ?", (global_id,))
        log.info("fingerprint", f"Deleted global profile {global_id[:8]}")

    def merge_global_speakers(self, keep_id: str, merge_id: str) -> dict:
        """Move all embeddings from merge_id → keep_id, recompute centroid, delete merge_id.
        Propagates kept profile's name/color to all linked speaker_labels.
        Returns resolved {name, color} of the kept profile."""
        with _conn(self._db_path) as c:
            c.execute(
                "UPDATE speaker_embeddings SET global_id = ? WHERE global_id = ?",
                (keep_id, merge_id),
            )
            c.execute(
                "UPDATE speaker_labels SET global_id = ? WHERE global_id = ?",
                (keep_id, merge_id),
            )
            c.execute("DELETE FROM global_speakers WHERE id = ?", (merge_id,))
            # Propagate kept profile's name/color to all linked labels
            row = c.execute(
                "SELECT name, color FROM global_speakers WHERE id = ?",
                (keep_id,),
            ).fetchone()
            if row:
                c.execute(
                    "UPDATE speaker_labels SET name=?, color=? WHERE global_id=?",
                    (row["name"], row["color"], keep_id),
                )
        self.recompute_centroid(keep_id)
        log.info("fingerprint", f"Merged {merge_id[:8]} -> {keep_id[:8]}")
        return {"name": row["name"], "color": row["color"]} if row else {}

    def list_global_speakers(self) -> list[dict]:
        with _conn(self._db_path) as c:
            rows = c.execute(
                "SELECT id, name, color, emb_count, updated_at "
                "FROM global_speakers ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_global_speaker(self, global_id: str) -> dict | None:
        with _conn(self._db_path) as c:
            row = c.execute(
                "SELECT id, name, color, emb_count, created_at, updated_at "
                "FROM global_speakers WHERE id = ?",
                (global_id,),
            ).fetchone()
        return dict(row) if row else None

    def find_by_name(self, name: str) -> dict | None:
        """Case-insensitive lookup of a global profile by name. Returns first match or None."""
        with _conn(self._db_path) as c:
            row = c.execute(
                "SELECT id, name, color, emb_count, created_at, updated_at "
                "FROM global_speakers WHERE lower(name) = lower(?)",
                (name.strip(),),
            ).fetchone()
        return dict(row) if row else None

    def get_profile_sessions(self, global_id: str) -> list[dict]:
        """Return sessions where this speaker appeared, with speaker_keys and segment counts."""
        with _conn(self._db_path) as c:
            rows = c.execute(
                """
                SELECT s.id AS session_id, s.title, s.started_at,
                       sl.speaker_key,
                       (SELECT COUNT(*) FROM transcript_segments ts
                        WHERE ts.session_id = s.id AND ts.source = sl.speaker_key) AS seg_count
                FROM speaker_labels sl
                JOIN sessions s ON s.id = sl.session_id
                WHERE sl.global_id = ?
                ORDER BY s.started_at DESC
                """,
                (global_id,),
            ).fetchall()

        # Group by session
        by_session: dict[str, dict] = {}
        for r in rows:
            sid = r["session_id"]
            if sid not in by_session:
                by_session[sid] = {
                    "session_id": sid,
                    "title":      r["title"],
                    "started_at": r["started_at"],
                    "speaker_keys": [],
                    "seg_count":  0,
                }
            by_session[sid]["speaker_keys"].append(r["speaker_key"])
            by_session[sid]["seg_count"] += r["seg_count"]

        return list(by_session.values())

    def get_linked_labels(self, global_id: str) -> list[dict]:
        """Return all speaker_labels rows linked to a global profile."""
        with _conn(self._db_path) as c:
            rows = c.execute(
                "SELECT session_id, speaker_key, name, color "
                "FROM speaker_labels WHERE global_id = ?",
                (global_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_unlinked_speaker_groups(self) -> list[dict]:
        """Return distinct speaker names that need linking or have stale names.
        Includes: unlinked labels (global_id IS NULL) and labels whose display
        name doesn't match their linked profile's name.
        Excludes default 'Speaker N' names."""
        with _conn(self._db_path) as c:
            rows = c.execute(
                """
                SELECT sl.name,
                       COUNT(DISTINCT sl.session_id) AS session_count,
                       COUNT(*) AS label_count
                FROM speaker_labels sl
                LEFT JOIN global_speakers gs ON gs.id = sl.global_id
                WHERE (sl.global_id IS NULL OR lower(sl.name) != lower(gs.name))
                  AND lower(sl.name) NOT GLOB 'speaker [0-9]*'
                  AND sl.name != ''
                GROUP BY lower(sl.name)
                ORDER BY session_count DESC, lower(sl.name)
                """,
            ).fetchall()
        return [dict(r) for r in rows]

    def get_unlinked_speaker_sessions(self, name: str) -> list[dict]:
        """Return sessions where an unlinked/mismatched speaker name appears."""
        with _conn(self._db_path) as c:
            rows = c.execute(
                """
                SELECT DISTINCT s.id AS session_id, s.title, s.started_at
                FROM speaker_labels sl
                JOIN sessions s ON s.id = sl.session_id
                LEFT JOIN global_speakers gs ON gs.id = sl.global_id
                WHERE lower(sl.name) = lower(?)
                  AND (sl.global_id IS NULL OR lower(sl.name) != lower(gs.name))
                ORDER BY s.started_at DESC
                """,
                (name.strip(),),
            ).fetchall()
        return [dict(r) for r in rows]

    def bulk_link_by_name(self, name: str, global_id: str) -> list[dict]:
        """Link all speaker_labels matching name to a global profile.
        Updates their global_id, name, and color to match the profile.
        Handles both unlinked labels and already-linked labels with stale names.
        Returns list of affected {session_id, speaker_key} pairs."""
        with _conn(self._db_path) as c:
            profile = c.execute(
                "SELECT name, color FROM global_speakers WHERE id = ?",
                (global_id,),
            ).fetchone()
            if not profile:
                return []
            affected = c.execute(
                "SELECT session_id, speaker_key FROM speaker_labels "
                "WHERE lower(name) = lower(?)",
                (name.strip(),),
            ).fetchall()
            affected = [dict(r) for r in affected]
            if affected:
                c.execute(
                    "UPDATE speaker_labels SET global_id=?, name=?, color=? "
                    "WHERE lower(name) = lower(?)",
                    (global_id, profile["name"], profile["color"], name.strip()),
                )
        return affected

    # ── Embedding extraction ──────────────────────────────────────────────────

    def extract_embedding(self, audio: np.ndarray) -> np.ndarray | None:
        """
        Extract a 256-dim L2-normalized speaker embedding.
        audio: float32 mono numpy array at 16 kHz.
        Returns None if not ready or extraction fails.
        """
        if not self._ready or self._inference is None:
            return None
        try:
            import torch
            # pyannote Inference expects {"waveform": (1, samples) tensor, "sample_rate": int}
            waveform = torch.from_numpy(audio).float().unsqueeze(0)
            result = self._inference({"waveform": waveform, "sample_rate": 16_000})
            emb = np.array(result).flatten().astype(np.float32)
            return _normalize(emb)
        except Exception:
            log.warn("fingerprint", "Embedding extraction failed:")
            traceback.print_exc()
            return None

    def extract_embedding_from_wav(
        self, wav_path: str, start_sec: float, end_sec: float
    ) -> np.ndarray | None:
        """Extract a speaker embedding from a time slice of a WAV file."""
        if not self._ready or end_sec - start_sec < _MIN_DURATION_SEC:
            return None
        try:
            import wave
            from scipy import signal as scipy_signal
            with wave.open(wav_path, "rb") as wf:
                rate = wf.getframerate()
                channels = wf.getnchannels()
                wf.setpos(int(start_sec * rate))
                n_frames = int((end_sec - start_sec) * rate)
                raw = wf.readframes(n_frames)
            audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            if channels > 1:
                audio = audio.reshape(-1, channels).mean(axis=1)
            if rate != 16000:
                audio = scipy_signal.resample_poly(audio, 16000, rate)
            return self.extract_embedding(audio)
        except Exception:
            log.warn("fingerprint", f"WAV slice extraction failed ({start_sec:.1f}-{end_sec:.1f}s)")
            traceback.print_exc()
            return None

    # ── Embedding storage ─────────────────────────────────────────────────────

    def add_embedding(
        self,
        global_id: str,
        session_id: str,
        speaker_key: str,
        embedding: np.ndarray,
        duration_sec: float,
    ) -> None:
        """Store embedding and incrementally update centroid. All embeddings are kept."""
        now = _now()
        with _conn(self._db_path) as c:
            row = c.execute(
                "SELECT centroid, emb_count FROM global_speakers WHERE id = ?",
                (global_id,),
            ).fetchone()
            if row is None:
                return

            old_count    = row["emb_count"]
            old_centroid = _blob_to_emb(row["centroid"]) if row["centroid"] else None

            c.execute(
                "INSERT INTO speaker_embeddings "
                "(global_id, session_id, speaker_key, embedding, duration_sec, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (global_id, session_id, speaker_key, _emb_to_blob(embedding), duration_sec, now),
            )

            new_count = old_count + 1

            # Incremental centroid update
            if old_centroid is not None:
                new_centroid = _normalize(old_centroid * old_count + embedding)
            else:
                new_centroid = _normalize(embedding)

            c.execute(
                "UPDATE global_speakers SET centroid=?, emb_count=?, updated_at=? WHERE id=?",
                (_emb_to_blob(new_centroid), new_count, now, global_id),
            )

    # ── Matching ──────────────────────────────────────────────────────────────

    def find_matches(
        self,
        embedding: np.ndarray,
        exclude_global_ids: set | None = None,
        top_k: int = 3,
        min_similarity: float | None = None,
    ) -> list[dict]:
        """
        Compare embedding against all global profile centroids.
        Returns top_k matches sorted by similarity desc.

        ``min_similarity`` defaults to SUGGEST_THRESHOLD; callers wanting a
        diagnostic view of *all* nearest profiles (including ones that
        wouldn't trigger a suggestion) can pass ``0.0``. The ``auto_apply``
        flag still reflects AUTO_APPLY_THRESHOLD regardless.
        """
        threshold = _SUGGEST_THRESHOLD if min_similarity is None else min_similarity
        if not self._ready:
            return []

        with _conn(self._db_path) as c:
            rows = c.execute(
                "SELECT id, name, color, centroid FROM global_speakers WHERE centroid IS NOT NULL"
            ).fetchall()

        if not rows:
            return []

        exclude = exclude_global_ids or set()
        results = []
        for r in rows:
            if r["id"] in exclude:
                continue
            centroid = _blob_to_emb(r["centroid"])
            # Cosine similarity = dot product for L2-normalized vectors
            sim = float(np.dot(embedding, centroid))
            if sim >= threshold:
                results.append({
                    "global_id":   r["id"],
                    "name":        r["name"],
                    "color":       r["color"],
                    "similarity":  round(sim, 3),
                    "auto_apply":  sim >= _AUTO_APPLY_THRESHOLD,
                })

        results.sort(key=lambda x: x["similarity"], reverse=True)
        return results[:top_k]

    # ── Session linking ───────────────────────────────────────────────────────

    def link_session_speaker(
        self, session_id: str, speaker_key: str, global_id: str
    ) -> None:
        with _conn(self._db_path) as c:
            c.execute(
                "UPDATE speaker_labels SET global_id = ? "
                "WHERE session_id = ? AND speaker_key = ?",
                (global_id, session_id, speaker_key),
            )

    def unlink_session_speaker(self, session_id: str, speaker_key: str) -> None:
        with _conn(self._db_path) as c:
            c.execute(
                "UPDATE speaker_labels SET global_id = NULL "
                "WHERE session_id = ? AND speaker_key = ?",
                (session_id, speaker_key),
            )

    def get_link(self, session_id: str, speaker_key: str) -> str | None:
        with _conn(self._db_path) as c:
            row = c.execute(
                "SELECT global_id FROM speaker_labels "
                "WHERE session_id = ? AND speaker_key = ?",
                (session_id, speaker_key),
            ).fetchone()
        return row["global_id"] if row else None

    def get_session_links(self, session_id: str) -> dict[str, str]:
        """Return {speaker_key: global_id} for all linked speakers in a session."""
        with _conn(self._db_path) as c:
            rows = c.execute(
                "SELECT speaker_key, global_id FROM speaker_labels "
                "WHERE session_id = ? AND global_id IS NOT NULL",
                (session_id,),
            ).fetchall()
        return {r["speaker_key"]: r["global_id"] for r in rows}

    # ── Maintenance ───────────────────────────────────────────────────────────

    def recompute_centroid(self, global_id: str) -> None:
        """Full centroid recompute from all stored embeddings (used after merge/prune)."""
        with _conn(self._db_path) as c:
            rows = c.execute(
                "SELECT embedding FROM speaker_embeddings WHERE global_id = ?",
                (global_id,),
            ).fetchall()
            if not rows:
                c.execute(
                    "UPDATE global_speakers SET centroid=NULL, emb_count=0, updated_at=? WHERE id=?",
                    (_now(), global_id),
                )
                return
            embs = np.stack([_blob_to_emb(r["embedding"]) for r in rows])
            centroid = _normalize(embs.mean(axis=0))
            c.execute(
                "UPDATE global_speakers SET centroid=?, emb_count=?, updated_at=? WHERE id=?",
                (_emb_to_blob(centroid), len(rows), _now(), global_id),
            )

    def remove_session_embeddings(self, session_id: str) -> list[str]:
        """Delete all embeddings from a session. Returns affected global_ids for centroid recomputation."""
        with _conn(self._db_path) as c:
            rows = c.execute(
                "SELECT DISTINCT global_id FROM speaker_embeddings WHERE session_id = ?",
                (session_id,),
            ).fetchall()
            affected = [r["global_id"] for r in rows]
            if affected:
                c.execute(
                    "DELETE FROM speaker_embeddings WHERE session_id = ?",
                    (session_id,),
                )
        return affected

    def prune_embeddings(
        self,
        global_id: str,
        outlier_threshold: float = 0.55,
        dedup_threshold: float = 0.98,
    ) -> dict:
        """Quality-based pruning: remove outliers and near-duplicates.

        1. Remove outliers — embeddings with low similarity to the current centroid
           (likely noisy or misattributed audio).
        2. Remove near-duplicates — when two embeddings are almost identical
           (cosine sim ≥ dedup_threshold), drop the shorter-duration one.

        No hard cap — the pool grows naturally and stays healthy through
        outlier/dedup passes alone.

        Returns {before, after, outliers_removed, duplicates_removed}."""
        with _conn(self._db_path) as c:
            rows = c.execute(
                "SELECT id, embedding, duration_sec FROM speaker_embeddings "
                "WHERE global_id = ? ORDER BY id",
                (global_id,),
            ).fetchall()

            before = len(rows)
            if before <= 1:
                return {"before": before, "after": before,
                        "outliers_removed": 0, "duplicates_removed": 0}

            ids        = [r["id"] for r in rows]
            embs       = np.stack([_blob_to_emb(r["embedding"]) for r in rows])
            durations  = np.array([r["duration_sec"] or 0.0 for r in rows], dtype=np.float32)

            # Compute centroid from all current embeddings
            centroid = _normalize(embs.mean(axis=0))

            # Cosine similarities to centroid (embeddings are L2-normalized)
            sims = embs @ centroid

            # --- Pass 1: remove outliers ---
            outlier_mask = sims < outlier_threshold
            outlier_ids = [ids[i] for i in range(before) if outlier_mask[i]]
            keep_mask = ~outlier_mask
            ids       = [ids[i] for i in range(before) if keep_mask[i]]
            embs      = embs[keep_mask]
            durations = durations[keep_mask]

            # --- Pass 2: remove near-duplicates (keep longer-duration one) ---
            dedup_drop = set()
            n = len(ids)
            if n > 1:
                # Pairwise cosine similarity matrix
                pair_sims = embs @ embs.T
                for i in range(n):
                    if i in dedup_drop:
                        continue
                    for j in range(i + 1, n):
                        if j in dedup_drop:
                            continue
                        if pair_sims[i, j] >= dedup_threshold:
                            # Drop the one with shorter duration
                            drop = j if durations[i] >= durations[j] else i
                            dedup_drop.add(drop)

            dedup_ids = [ids[i] for i in dedup_drop]

            # --- Execute deletions ---
            all_remove = outlier_ids + dedup_ids
            if all_remove:
                c.executemany(
                    "DELETE FROM speaker_embeddings WHERE id = ?",
                    [(rid,) for rid in all_remove],
                )

        after = before - len(all_remove) if all_remove else before
        self.recompute_centroid(global_id)
        log.info("fingerprint",
                 f"Optimized {global_id[:8]}: {before}->{after} "
                 f"(outliers={len(outlier_ids)}, dupes={len(dedup_ids)})")
        return {"before": before, "after": after,
                "outliers_removed": len(outlier_ids),
                "duplicates_removed": len(dedup_ids)}

    # ── Unlabeled embedding storage (feeds the cleanup UI) ────────────────────

    def add_unlabeled_embedding(
        self,
        session_id: str,
        speaker_key: str,
        embedding: np.ndarray,
        duration_sec: float,
    ) -> None:
        """Persist an embedding for a speaker_key that isn't linked to a profile.

        Powers the post-meeting cleanup UI's clustering pass — without this,
        unlabeled speakers have no embeddings to cluster on once the live
        recording ends.
        """
        with _conn(self._db_path) as c:
            c.execute(
                "INSERT INTO unlabeled_embeddings "
                "(session_id, speaker_key, embedding, duration_sec, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, speaker_key, _emb_to_blob(embedding), duration_sec, _now()),
            )

    def get_unlabeled_embeddings(
        self, session_id: str, speaker_key: str | None = None,
    ) -> list[dict]:
        """Return all unlabeled embeddings for a session (or one speaker_key)."""
        with _conn(self._db_path) as c:
            if speaker_key is None:
                rows = c.execute(
                    "SELECT id, speaker_key, embedding, duration_sec FROM unlabeled_embeddings "
                    "WHERE session_id = ? ORDER BY id",
                    (session_id,),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT id, speaker_key, embedding, duration_sec FROM unlabeled_embeddings "
                    "WHERE session_id = ? AND speaker_key = ? ORDER BY id",
                    (session_id, speaker_key),
                ).fetchall()
        out = []
        for r in rows:
            out.append({
                "id":          r["id"],
                "speaker_key": r["speaker_key"],
                "embedding":   _blob_to_emb(r["embedding"]),
                "duration":    r["duration_sec"],
            })
        return out

    def clear_unlabeled_embeddings(
        self, session_id: str, speaker_key: str | None = None,
    ) -> int:
        """Delete unlabeled embeddings (all for session, or for one speaker_key)."""
        with _conn(self._db_path) as c:
            if speaker_key is None:
                cur = c.execute(
                    "DELETE FROM unlabeled_embeddings WHERE session_id = ?",
                    (session_id,),
                )
            else:
                cur = c.execute(
                    "DELETE FROM unlabeled_embeddings "
                    "WHERE session_id = ? AND speaker_key = ?",
                    (session_id, speaker_key),
                )
            return cur.rowcount

    def get_latest_embedding(self, global_id: str, session_id: str, speaker_key: str) -> np.ndarray | None:
        """Return the most recently added embedding for a given (session, speaker_key, global_id)."""
        with _conn(self._db_path) as c:
            row = c.execute(
                "SELECT embedding FROM speaker_embeddings "
                "WHERE global_id = ? AND session_id = ? AND speaker_key = ? "
                "ORDER BY id DESC LIMIT 1",
                (global_id, session_id, speaker_key),
            ).fetchone()
        return _blob_to_emb(row["embedding"]) if row else None

    # ── Session cluster cleanup pipeline ──────────────────────────────────────

    def _gather_session_speakers(self, session_id: str) -> list[dict]:
        """Return one row per speaker_key in the session with label + segment info.

        Includes speaker_keys that appear in transcript_segments but have no
        speaker_labels row yet — reanalysis only writes labels for matched
        speakers, so unlabeled ones (Speaker 4, 6, 7…) live only in the
        segments table until the user names them.
        """
        with _conn(self._db_path) as c:
            rows = c.execute(
                """
                WITH used_keys AS (
                    SELECT DISTINCT source AS speaker_key
                    FROM transcript_segments
                    WHERE session_id = ?
                      AND source IS NOT NULL
                      AND source != ''
                    UNION
                    SELECT speaker_key FROM speaker_labels WHERE session_id = ?
                )
                SELECT uk.speaker_key,
                       COALESCE(sl.name, uk.speaker_key)        AS name,
                       sl.color,
                       sl.global_id,
                       COALESCE(sl.is_noise, 0)                  AS is_noise,
                       gs.name  AS global_name,
                       gs.color AS global_color
                FROM used_keys uk
                LEFT JOIN speaker_labels sl
                  ON sl.session_id = ? AND sl.speaker_key = uk.speaker_key
                LEFT JOIN global_speakers gs
                  ON gs.id = sl.global_id
                """,
                (session_id, session_id, session_id),
            ).fetchall()
            speakers = []
            for r in rows:
                segs = c.execute(
                    "SELECT id, start_time, end_time FROM transcript_segments "
                    "WHERE session_id = ? AND source = ? ORDER BY start_time",
                    (session_id, r["speaker_key"]),
                ).fetchall()
                if not segs:
                    continue
                speakers.append({
                    "speaker_key":  r["speaker_key"],
                    "name":         r["name"],
                    "color":        r["color"],
                    "global_id":    r["global_id"],
                    "global_name":  r["global_name"],
                    "global_color": r["global_color"],
                    "is_noise":     bool(r["is_noise"]),
                    "segments":     [dict(s) for s in segs],
                })
        return speakers

    def _gather_speaker_embeddings(
        self, session_id: str, speaker_key: str, global_id: str | None,
    ) -> list[np.ndarray]:
        """Return all stored embeddings for a (session, speaker_key).

        Labeled speakers' embeddings live in speaker_embeddings (linked by
        global_id). Unlabeled speakers' embeddings live in unlabeled_embeddings.
        We check both so we don't lose history if the user has linked, unlinked,
        and re-linked a key.
        """
        embs: list[np.ndarray] = []
        with _conn(self._db_path) as c:
            if global_id:
                rows = c.execute(
                    "SELECT embedding FROM speaker_embeddings "
                    "WHERE session_id = ? AND speaker_key = ? AND global_id = ?",
                    (session_id, speaker_key, global_id),
                ).fetchall()
                embs.extend(_blob_to_emb(r["embedding"]) for r in rows)
            rows = c.execute(
                "SELECT embedding FROM unlabeled_embeddings "
                "WHERE session_id = ? AND speaker_key = ?",
                (session_id, speaker_key),
            ).fetchall()
            embs.extend(_blob_to_emb(r["embedding"]) for r in rows)
        return embs

    def _backfill_embedding_from_wav(
        self, session_id: str, speaker_key: str, segments: list[dict], wav_path: str,
    ) -> np.ndarray | None:
        """Re-extract a fresh embedding from this speaker_key's audio.

        Used when a session pre-dates unlabeled_embeddings (so no embeddings
        were ever persisted for this key) or the live pipeline dropped them.
        Persists the result into unlabeled_embeddings so the next call is free.

        Diarized segments are often shorter than MIN_DURATION_SEC, so we
        concatenate the longest few until we have enough audio. Falls back
        cleanly to None if total audio is still too short.
        """
        if not self._ready or not segments:
            return None
        import wave
        from scipy import signal as scipy_signal

        ranked = sorted(
            segments,
            key=lambda s: (s["end_time"] or 0) - (s["start_time"] or 0),
            reverse=True,
        )

        try:
            with wave.open(wav_path, "rb") as wf:
                rate = wf.getframerate()
                channels = wf.getnchannels()
                total_frames = wf.getnframes()
                chunks: list[np.ndarray] = []
                total_sec = 0.0
                target_sec = max(_MIN_DURATION_SEC + 0.5, 4.0)
                for seg in ranked:
                    start = seg["start_time"] or 0.0
                    end = seg["end_time"] or 0.0
                    dur = end - start
                    if dur <= 0:
                        continue
                    start_frame = int(start * rate)
                    n_frames = int(dur * rate)
                    if start_frame + n_frames > total_frames:
                        n_frames = max(0, total_frames - start_frame)
                    if n_frames <= 0:
                        continue
                    wf.setpos(start_frame)
                    raw = wf.readframes(n_frames)
                    if not raw:
                        continue
                    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                    if channels > 1:
                        audio = audio.reshape(-1, channels).mean(axis=1)
                    chunks.append(audio)
                    total_sec += dur
                    if total_sec >= target_sec:
                        break
            if not chunks or total_sec < _MIN_DURATION_SEC:
                return None
            audio = np.concatenate(chunks)
            if rate != 16000:
                audio = scipy_signal.resample_poly(audio, 16000, rate)
            emb = self.extract_embedding(audio)
            if emb is None:
                return None
            try:
                self.add_unlabeled_embedding(session_id, speaker_key, emb, total_sec)
            except Exception:
                pass
            return emb
        except Exception:
            log.warn("fingerprint", f"backfill failed for {speaker_key} in {session_id[:8]}")
            traceback.print_exc()
            return None

    @staticmethod
    def _agglomerative_clusters(
        keys: list[str], centroids: list[np.ndarray], threshold: float,
    ) -> list[list[int]]:
        """Single-linkage agglomerative clustering on cosine similarity.

        Returns a list of clusters; each cluster is a list of indices into ``keys``.
        Cluster boundary: stop merging when the closest remaining pair has
        cosine similarity below ``threshold``.
        """
        n = len(keys)
        if n == 0:
            return []
        if n == 1:
            return [[0]]
        # Cluster assignment: index → cluster id
        cluster_of = list(range(n))
        members: dict[int, list[int]] = {i: [i] for i in range(n)}
        embs = np.stack(centroids)
        sims = embs @ embs.T

        # Build candidate pairs above threshold, sorted descending by sim.
        pairs = []
        for i in range(n):
            for j in range(i + 1, n):
                if sims[i, j] >= threshold:
                    pairs.append((float(sims[i, j]), i, j))
        pairs.sort(reverse=True)

        for sim, i, j in pairs:
            ci, cj = cluster_of[i], cluster_of[j]
            if ci == cj:
                continue
            # Single-linkage: merge as soon as any cross-pair crosses threshold.
            target, source = (ci, cj) if len(members[ci]) >= len(members[cj]) else (cj, ci)
            for m in members[source]:
                cluster_of[m] = target
            members[target].extend(members[source])
            del members[source]

        return list(members.values())

    def cluster_session_speakers(
        self,
        session_id: str,
        wav_path: str | None = None,
        cluster_threshold: float = 0.70,
        suggest_threshold: float = 0.65,
    ) -> dict:
        """Cluster a session's speakers for the post-meeting cleanup UI.

        Returns a payload with two cluster lists:
          - ``labeled``: one cluster per linked global profile (members are the
            speaker_keys that resolve to that profile).
          - ``unlabeled``: agglomerative clusters of speaker_keys that aren't
            linked to a profile, plus singletons for keys with no embedding.

        Each member carries its per-key centroid (base64) so the client can
        recompute cluster centroids + library suggestions instantly on drop.
        """
        speakers = self._gather_session_speakers(session_id)

        # Step 1: collect or backfill an embedding pool per speaker_key.
        backfilled_count = 0
        for sp in speakers:
            embs = self._gather_speaker_embeddings(
                session_id, sp["speaker_key"], sp["global_id"],
            )
            if not embs and wav_path:
                emb = self._backfill_embedding_from_wav(
                    session_id, sp["speaker_key"], sp["segments"], wav_path,
                )
                if emb is not None:
                    embs = [emb]
                    backfilled_count += 1
            sp["_embeddings"] = embs

        # Step 2: per-key centroid (mean of embeddings, L2-normalized).
        for sp in speakers:
            embs = sp["_embeddings"]
            if embs:
                sp["_centroid"] = _normalize(np.stack(embs).mean(axis=0))
                sp["emb_count"] = len(embs)
            else:
                sp["_centroid"] = None
                sp["emb_count"] = 0

        # Step 3: library suggestions per key (so client can mirror our logic).
        def _suggestion_for(centroid: np.ndarray, exclude: set[str]) -> dict | None:
            if centroid is None:
                return None
            matches = self.find_matches(
                centroid, exclude_global_ids=exclude,
                top_k=3, min_similarity=suggest_threshold,
            )
            return matches[0] if matches else None

        # Step 4: build labeled clusters (grouped by global_id).
        # Noise-flagged speakers never appear in regular clusters; they show
        # up in the collapsed "Noise" section regardless of their global_id.
        labeled_groups: dict[str, dict] = {}
        for sp in speakers:
            if sp["is_noise"]:
                continue
            gid = sp["global_id"]
            if not gid:
                continue
            grp = labeled_groups.setdefault(gid, {
                "cluster_id": f"profile:{gid}",
                "kind":       "labeled",
                "global_id":  gid,
                "name":       sp["global_name"] or sp["name"],
                "color":      sp["global_color"] or sp["color"],
                "members":    [],
            })
            grp["members"].append(sp)

        # Step 5: agglomerative clustering on unlabeled speakers with centroids.
        unlabeled = [sp for sp in speakers if not sp["global_id"] and not sp["is_noise"]]
        with_centroid = [sp for sp in unlabeled if sp["_centroid"] is not None]
        without_centroid = [sp for sp in unlabeled if sp["_centroid"] is None]

        clusters_idx = self._agglomerative_clusters(
            [sp["speaker_key"] for sp in with_centroid],
            [sp["_centroid"] for sp in with_centroid],
            cluster_threshold,
        )

        unlabeled_clusters = []
        for cluster_n, idxs in enumerate(clusters_idx, start=1):
            members = [with_centroid[i] for i in idxs]
            total_emb_count = sum(m["emb_count"] for m in members)
            # Cluster centroid = emb_count-weighted mean of member centroids.
            weighted = sum(m["_centroid"] * m["emb_count"] for m in members)
            cluster_centroid = _normalize(weighted) if total_emb_count else members[0]["_centroid"]
            suggestion = _suggestion_for(cluster_centroid, exclude=set(labeled_groups.keys()))
            unlabeled_clusters.append({
                "cluster_id": f"unlabeled:{cluster_n}",
                "kind":       "unlabeled",
                "global_id":  None,
                "name":       None,
                "color":      None,
                "members":    members,
                "_centroid":  cluster_centroid,
                "suggestion": suggestion,
            })

        # Step 6: singletons for keys without any embedding (no clustering possible).
        for sp in without_centroid:
            unlabeled_clusters.append({
                "cluster_id": f"unlabeled:orphan:{sp['speaker_key']}",
                "kind":       "unlabeled",
                "global_id":  None,
                "name":       None,
                "color":      None,
                "members":    [sp],
                "_centroid":  None,
                "suggestion": None,
            })

        # Step 7: noise cluster (collapsed).
        noise_members = [sp for sp in speakers if sp["is_noise"]]
        noise_cluster = {
            "cluster_id": "noise",
            "kind":       "noise",
            "global_id":  None,
            "name":       "Noise",
            "color":      "#6e7681",
            "members":    noise_members,
        } if noise_members else None

        # Add per-labeled suggestions (so user sees "this profile might be
        # better described as someone else" when their centroid has drifted).
        for grp in labeled_groups.values():
            embs_concat = []
            for m in grp["members"]:
                if m["_centroid"] is not None:
                    embs_concat.append(m["_centroid"] * max(m["emb_count"], 1))
            if embs_concat:
                grp["_centroid"] = _normalize(np.sum(embs_concat, axis=0))
            else:
                grp["_centroid"] = None

        # Step 8: serialize for transport.
        def _ser_member(sp: dict) -> dict:
            return {
                "speaker_key": sp["speaker_key"],
                "name":        sp["name"],
                "color":       sp["color"],
                "segments":    [
                    {"id": s["id"], "start": s["start_time"], "end": s["end_time"]}
                    for s in sp["segments"]
                ],
                "segment_count": len(sp["segments"]),
                "emb_count":   sp["emb_count"],
                "centroid":    base64.b64encode(sp["_centroid"].tobytes()).decode("ascii")
                               if sp["_centroid"] is not None else None,
                "is_noise":    sp["is_noise"],
                "global_id":   sp["global_id"],
            }

        def _ser_cluster(grp: dict) -> dict:
            out = {
                "cluster_id": grp["cluster_id"],
                "kind":       grp["kind"],
                "global_id":  grp.get("global_id"),
                "name":       grp.get("name"),
                "color":      grp.get("color"),
                "members":    [_ser_member(m) for m in grp["members"]],
            }
            if grp.get("_centroid") is not None:
                out["centroid"] = base64.b64encode(grp["_centroid"].tobytes()).decode("ascii")
            if "suggestion" in grp:
                out["suggestion"] = grp["suggestion"]
            return out

        labeled_list = sorted(
            labeled_groups.values(),
            key=lambda g: sum(len(m["segments"]) for m in g["members"]),
            reverse=True,
        )
        unlabeled_clusters.sort(
            key=lambda g: sum(len(m["segments"]) for m in g["members"]),
            reverse=True,
        )

        # Library snapshot with centroids — lets the client recompute
        # similarity suggestions instantly when the user drags a pill between
        # clusters, instead of round-tripping to the server on every drop.
        library = []
        with _conn(self._db_path) as c:
            lib_rows = c.execute(
                "SELECT id, name, color, emb_count, centroid FROM global_speakers "
                "WHERE centroid IS NOT NULL ORDER BY lower(name)"
            ).fetchall()
        for r in lib_rows:
            library.append({
                "global_id": r["id"],
                "name":      r["name"],
                "color":     r["color"],
                "emb_count": r["emb_count"],
                "centroid":  base64.b64encode(r["centroid"]).decode("ascii"),
            })

        return {
            "session_id":         session_id,
            "labeled_clusters":   [_ser_cluster(g) for g in labeled_list],
            "unlabeled_clusters": [_ser_cluster(g) for g in unlabeled_clusters],
            "noise_cluster":      _ser_cluster(noise_cluster) if noise_cluster else None,
            "library":            library,
            "thresholds": {
                "cluster":   cluster_threshold,
                "suggest":   suggest_threshold,
                "auto":      _AUTO_APPLY_THRESHOLD,
            },
            "stats": {
                "speakers_total":   len(speakers),
                "speakers_labeled": sum(1 for s in speakers if s["global_id"]),
                "speakers_noise":   sum(1 for s in speakers if s["is_noise"]),
                "embedding_backfills": backfilled_count,
            },
        }

    # ── Apply corrections (the "Save" path of the cleanup UI) ─────────────────

    def apply_cluster_corrections(
        self,
        session_id: str,
        proposed: list[dict],
        noise_keys: list[str] | None = None,
    ) -> dict:
        """Apply the user's cleanup decisions.

        ``proposed`` is a list of clusters:
            [{
                "global_id": str | None,     # existing profile, or None
                "new_name":  str | None,     # create a new profile with this name
                "color":     str | None,     # optional override
                "member_keys": [speaker_key, ...],
            }, ...]

        For each cluster:
          - existing global_id → relink members; embeddings (labeled +
            unlabeled) merge into that profile.
          - new_name → create a profile, then relink as above.
          - empty → unlink members (back to "Speaker N").

        ``noise_keys`` get is_noise=1 on speaker_labels.

        After all relinks, recompute touched profiles' centroids from scratch
        (cheaper and safer than incremental updates given the bulk migration).
        Then prune_embeddings() on each.
        """
        from core import storage  # late import — avoids circular import at boot
        from capture_video import media_edit  # for wav lookup, used by backfill

        noise_keys_set = set(noise_keys or [])
        touched_profiles: set[str] = set()
        created: list[dict] = []
        relinked_members = 0
        unlinked_members = 0

        # Read current labels so we can diff.
        current = {
            sp["speaker_key"]: sp
            for sp in self._gather_session_speakers(session_id)
        }

        # Pre-pass: figure out which profiles will need their centroid rebuilt.
        for cluster in proposed:
            target_gid = cluster.get("global_id")
            if target_gid:
                touched_profiles.add(target_gid)
            for k in cluster.get("member_keys", []):
                cur = current.get(k)
                if cur and cur["global_id"]:
                    touched_profiles.add(cur["global_id"])

        # Pass 1: create new profiles from new_name clusters.
        for cluster in proposed:
            if cluster.get("global_id"):
                continue
            new_name = (cluster.get("new_name") or "").strip()
            if not new_name:
                continue
            color = cluster.get("color")
            gid = self.create_global_speaker(new_name, color=color)
            cluster["global_id"] = gid
            touched_profiles.add(gid)
            created.append({"global_id": gid, "name": new_name})

        # Pass 2: relink members + migrate unlabeled embeddings → speaker_embeddings.
        with _conn(self._db_path) as c:
            for cluster in proposed:
                target_gid = cluster.get("global_id")
                member_keys = [k for k in cluster.get("member_keys", []) if k not in noise_keys_set]
                if target_gid:
                    profile_row = c.execute(
                        "SELECT name, color FROM global_speakers WHERE id = ?",
                        (target_gid,),
                    ).fetchone()
                    if not profile_row:
                        continue
                    for k in member_keys:
                        # UPSERT — reanalysis sessions often have segments for
                        # speaker_keys with no speaker_labels row yet.
                        c.execute(
                            "INSERT INTO speaker_labels "
                            "(session_id, speaker_key, name, color, global_id, is_noise) "
                            "VALUES (?, ?, ?, ?, ?, 0) "
                            "ON CONFLICT(session_id, speaker_key) DO UPDATE SET "
                            "name=excluded.name, color=excluded.color, "
                            "global_id=excluded.global_id, is_noise=0",
                            (session_id, k, profile_row["name"], profile_row["color"], target_gid),
                        )
                        # Migrate any unlabeled embeddings for this key into
                        # the labeled embedding table under the target profile.
                        unlbl = c.execute(
                            "SELECT id, embedding, duration_sec FROM unlabeled_embeddings "
                            "WHERE session_id=? AND speaker_key=?",
                            (session_id, k),
                        ).fetchall()
                        for ue in unlbl:
                            c.execute(
                                "INSERT INTO speaker_embeddings "
                                "(global_id, session_id, speaker_key, embedding, duration_sec, created_at) "
                                "VALUES (?, ?, ?, ?, ?, ?)",
                                (target_gid, session_id, k, ue["embedding"],
                                 ue["duration_sec"], _now()),
                            )
                        if unlbl:
                            c.execute(
                                "DELETE FROM unlabeled_embeddings WHERE session_id=? AND speaker_key=?",
                                (session_id, k),
                            )
                        relinked_members += 1
                else:
                    # Empty cluster → unlink (back to plain Speaker N).
                    for k in member_keys:
                        cur = current.get(k)
                        if cur and cur["global_id"]:
                            # Move the existing labeled embeddings back to
                            # unlabeled storage so they aren't lost.
                            embs = c.execute(
                                "SELECT embedding, duration_sec FROM speaker_embeddings "
                                "WHERE session_id=? AND speaker_key=? AND global_id=?",
                                (session_id, k, cur["global_id"]),
                            ).fetchall()
                            for e in embs:
                                c.execute(
                                    "INSERT INTO unlabeled_embeddings "
                                    "(session_id, speaker_key, embedding, duration_sec, created_at) "
                                    "VALUES (?, ?, ?, ?, ?)",
                                    (session_id, k, e["embedding"], e["duration_sec"], _now()),
                                )
                            c.execute(
                                "DELETE FROM speaker_embeddings "
                                "WHERE session_id=? AND speaker_key=? AND global_id=?",
                                (session_id, k, cur["global_id"]),
                            )
                        c.execute(
                            "INSERT INTO speaker_labels "
                            "(session_id, speaker_key, name, color, global_id, is_noise) "
                            "VALUES (?, ?, ?, NULL, NULL, 0) "
                            "ON CONFLICT(session_id, speaker_key) DO UPDATE SET "
                            "global_id=NULL, is_noise=0",
                            (session_id, k, k),
                        )
                        unlinked_members += 1

            # Pass 3: noise flag (overrides cluster membership).
            for k in noise_keys_set:
                c.execute(
                    "INSERT INTO speaker_labels "
                    "(session_id, speaker_key, name, color, global_id, is_noise) "
                    "VALUES (?, ?, ?, NULL, NULL, 1) "
                    "ON CONFLICT(session_id, speaker_key) DO UPDATE SET "
                    "is_noise=1, global_id=NULL",
                    (session_id, k, k),
                )

        # Pass 4: rebuild touched profiles' centroids + prune outliers.
        for gid in touched_profiles:
            try:
                self.recompute_centroid(gid)
                self.prune_embeddings(gid)
            except Exception:
                log.warn("fingerprint", f"Centroid rebuild failed for {gid[:8]}")
                traceback.print_exc()

        log.info(
            "fingerprint",
            f"Cleanup applied: relinked={relinked_members}, unlinked={unlinked_members}, "
            f"created={len(created)}, noise={len(noise_keys_set)}, profiles_rebuilt={len(touched_profiles)}",
        )
        return {
            "created":          created,
            "relinked":         relinked_members,
            "unlinked":         unlinked_members,
            "noise_marked":     len(noise_keys_set),
            "profiles_touched": list(touched_profiles),
        }
