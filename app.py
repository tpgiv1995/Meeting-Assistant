"""
Meeting Assistant - Flask web server.
Run: python app.py
Opens http://localhost:6969 automatically.
"""
import faulthandler
faulthandler.enable()  # dump traceback on native crashes (SIGSEGV, etc.)

import json
import logging
import os
import signal
import warnings
warnings.filterwarnings("ignore", category=SyntaxWarning, module=r"pyannote\.")
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, render_template, request, send_file, stream_with_context
import flask.cli

# ── Suppress Flask / werkzeug console noise ────────────────────────────────────
# Kill the startup banner ("Serving Flask app", "Running on ...", CTRL+C hint)
# and all request logs. We print our own startup message instead.
flask.cli.show_server_banner = lambda *a, **kw: None
logging.getLogger("werkzeug").setLevel(logging.ERROR)

import numpy as np

from core import log as log

from core import config as config
from capture_video import media_edit as media_edit
from ui_desktop import notifications as notifications
from core import paths as paths
from core import settings as settings
from core import storage as storage
from ai.assistant import AIAssistant
from capture_audio import (
    AudioCapture, enumerate_audio_devices, enumerate_dshow_audio_devices,
    auto_detect_devices,
)
from capture_audio.params import (
    TRANSCRIPTION_DEFAULTS, DIARIZATION_DEFAULTS, AUTO_GAIN_DEFAULTS,
    SCREEN_RECORDING_DEFAULTS,
    TRANSCRIPTION_PRESETS, TRANSCRIPTION_DEFAULT_PRESET,
    DIARIZATION_PRESETS, DIARIZATION_DEFAULT_PRESET,
)
from capture_video import ScreenRecorder, enumerate_displays, extract_frame, capture_live_frame, flash_display_border, find_ffmpeg, kill_stale_ffmpeg, PRESETS as SCREEN_PRESETS, H264_PRESETS, DEFAULT_PRESET as SCREEN_DEFAULT_PRESET
from ml.speaker_db import SpeakerFingerprintDB
from ml import text_embeddings as text_embeddings
from ml.transcriber import (
    DIARIZER_OPTIONS,
    WHISPER_PRESETS,
    Transcriber,
    get_cuda_available,
)

config.ensure_env()
storage.init_db()
# Heal sessions left "in progress" by a previous crash, killed split, etc.
# No active recording can exist this early in startup, so we don't need to
# pass an active_session_id.
_healed = storage.heal_stale_in_progress()
if _healed:
    log.info("storage", f"Healed {_healed} stale 'in progress' session(s) on startup.")

app = Flask(__name__, template_folder="ui_web/templates", static_folder="ui_web/static")
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0   # disable static file caching
app.config["TEMPLATES_AUTO_RELOAD"]     = True  # re-read templates on every request

# Fingerprint DB stub - __init__ is called in _load_fingerprint_db() after
# all module-level globals (_state, _on_fingerprint_audio) are defined.
fingerprint_db = SpeakerFingerprintDB.__new__(SpeakerFingerprintDB)
fingerprint_db._db_path   = storage.DB_PATH
fingerprint_db._ready     = False
fingerprint_db._inference = None

# ── Global singletons ─────────────────────────────────────────────────────────

# Load preferences first so we can initialise the AI assistant with the
# saved provider/model rather than hardcoded defaults.
_saved_prefs = settings.load()

ai = AIAssistant(
    provider=_saved_prefs.get("ai_provider", "openai"),
    model=_saved_prefs.get("ai_model", "gpt-4o"),
)
log.info("ai", f"Provider: {ai.provider}, model: {ai.model}")

_audio_queue: queue.Queue = queue.Queue()
_transcriber = Transcriber(
    _audio_queue,
    lambda text, source, st=0.0, et=0.0: _on_segment(text, source, st, et),
)


def _on_diarizer_error(message: str) -> None:
    """Log diarizer failures visibly in the console."""
    log.warn("diarizer", message)


_transcriber.on_diarizer_error = _on_diarizer_error

# Apply saved model preferences
_saved_whisper_preset = _saved_prefs.get("whisper_preset", "")
_transcriber.diarization_enabled = _saved_prefs.get("diarization_enabled", True)
del _saved_prefs

# SSE: one queue per connected browser tab
_client_queues: dict[str, queue.Queue] = {}
_cq_lock = threading.Lock()

# Mutable session state - always access under _state_lock
_state: dict = {
    "session_id": None,
    "is_recording": False,
    "segments": [],          # list[{text, source}] - in-memory copy for current session
    "summary": "",
    "chat_history": [],      # list[{role, content}]
    "pending_segments": 0,       # segments since last auto-summary
    "summarized_seg_count": 0,   # segments included in the current summary
    "audio_capture": None,
    "test_capture": None,    # lightweight capture used only for visualizer testing
    "is_testing": False,
    "model_ready": False,
    "model_info": "",
    "diarizer_ready": False,
    "diarizer_failed": False,
    "speaker_labels": {},   # speaker_key → display name for the active session
    "custom_prompt": "",    # user-supplied context appended to the summary system prompt
    "is_reanalyzing": False,
    "summary_generating": False,   # True while any _run_summary call is executing
    "summary_manual_pending": False,  # True when /api/summarize was triggered; clears when it runs
    "speaker_audio_accum":    {},  # speaker_key → {"audio": np.ndarray, "total_sec": float}
    "speaker_emb_counts":     {},  # speaker_key → int (embeddings extracted this session)
    "fingerprint_dismissals": {},  # speaker_key → set[global_id]
    "fingerprint_suggestions": {},  # speaker_key → {session_id, speaker_key, current_name, matches}
    "speaker_offer_counts":   {},  # speaker_key → int (audio offers for diminishing returns)
    "last_audio_activity_at": 0.0,
    "last_transcript_activity_at": 0.0,
    "quiet_prompt_sent_at": 0.0,
    "quiet_prompt_armed": True,
    "recording_started_at_monotonic": 0.0,
}
_state_lock = threading.Lock()
_summary_lock = threading.Lock()  # serializes summary runs; prevents auto/manual overlap
_recording_cleanup_done = threading.Event()   # signalled when stop_recording cleanup finishes
_recording_cleanup_done.set()                 # initially "done" (no cleanup pending)
_screen_recorder = ScreenRecorder()
_chat_cancel: dict[str, threading.Event] = {}  # request_id → cancel event
_fp_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="fp-train")
# Thread pool for bulk auto-title regeneration. AI calls are network-bound so
# concurrency >> CPU count is fine; capped at 4 to avoid hammering the LLM API.
_retitle_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="retitle")
_tray = None  # MeetingTray instance (set in main(), None if no tray)
_server_url = f"http://localhost:{int(os.getenv('PORT', 6969))}"
_quiet_audio_rms_threshold = float(settings.get("quiet_prompt_audio_rms_threshold", 0.006))
_startup_init_lock = threading.Lock()
_startup_init_started = False

AUTO_SUMMARY_EVERY = 6  # trigger summary after this many new segments
_CUSTOM_SPEAKER_PREFIX = "custom:"


def _refresh_tray() -> None:
    """Update tray icon/menu if a tray is running. Safe to call from any thread."""
    if _tray is not None:
        _tray.refresh()


def _is_custom_speaker_key(speaker_key: str) -> bool:
    return speaker_key.startswith(_CUSTOM_SPEAKER_PREFIX)


_DEFAULT_SPEAKER_RE = re.compile(r"^speaker\s+\d+$", re.IGNORECASE)

def _is_default_speaker_name(name: str) -> bool:
    """Returns True for auto-generated names like 'Speaker 1', 'Speaker 12', etc."""
    return bool(_DEFAULT_SPEAKER_RE.match(name.strip()))


def _normalize_speaker_color(color: str | None) -> str | None:
    if color is None:
        return None
    color = color.strip()
    if not color:
        return None
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise ValueError("color must be a hex value like #58a6ff")
    return color


def _speaker_summary_update_context(rename_changes: list[tuple[str, str]]) -> str:
    """Describe speaker-label edits in plain language for summary patching."""
    if not rename_changes:
        return ""
    lines = ["Speaker label updates:"]
    for previous_name, current_name in rename_changes:
        lines.append(f'- "{previous_name}" was updated to "{current_name}".')
    lines.append("Update speaker attributions in the summary to match these labels.")
    return "\n".join(lines)


# ── SSE helpers ───────────────────────────────────────────────────────────────

def _push(event: str, data: dict) -> None:
    msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    with _cq_lock:
        dead = []
        for cid, q in _client_queues.items():
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(cid)
        for cid in dead:
            _client_queues.pop(cid, None)
    # Keep tray icon in sync with status changes
    if event == "status":
        _refresh_tray()


def _recording_prereqs_locked() -> tuple[bool, str]:
    """Return whether recording can start and, if not, why not."""
    if not _state["model_ready"]:
        info = (_state.get("model_info") or "").strip()
        return False, info or "Loading transcription model..."
    needs_diarizer = _transcriber.diarization_enabled and bool(os.getenv("HUGGING_FACE_KEY"))
    if needs_diarizer and not _state["diarizer_ready"] and not _state["diarizer_failed"]:
        return False, "Loading speaker diarization..."
    return True, _state.get("model_info") or "Ready"


def _status_payload(extra: dict | None = None) -> dict:
    with _state_lock:
        payload = {
            "recording": _state["is_recording"],
            "is_testing": _state["is_testing"],
            "session_id": _state["session_id"],
            "model_ready": _state["model_ready"],
            "model_info": _state["model_info"],
            "diarizer_ready": _state["diarizer_ready"],
            "screen_recording": _screen_recorder.is_recording,
        }
        recording_ready, recording_ready_reason = _recording_prereqs_locked()
    payload["recording_ready"] = recording_ready
    payload["recording_ready_reason"] = recording_ready_reason
    aw = _auto_record["watcher"]
    payload["auto_record"] = {
        "supported": aw is not None,
        "enabled": bool(settings.get("auto_record_enabled")),
        "in_call": bool(aw.in_call) if aw else False,
        "active_session": _auto_record["session_id"],
    }
    if extra:
        payload.update(extra)
    return payload


def _push_status(extra: dict | None = None) -> None:
    _push("status", _status_payload(extra))


# ── Transcript helpers ────────────────────────────────────────────────────────

_SOURCE_LABELS = {
    "loopback": "Desktop",
    "mic":      "Mic",
    "both":     "Desktop+Mic",
}

def _fmt_time(seconds: float) -> str:
    """Format seconds as MM:SS."""
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"

def _fmt_segment(seg: dict, speaker_labels: dict | None = None) -> str:
    """Format a {text, source} segment dict as a labelled line for AI context.

    Respects per-segment overrides:
    - label_override: a display name manually assigned to this segment
    - source_override: a speaker-key reassignment (look up in speaker_labels)
    """
    # Check for per-segment label override first (highest priority)
    label_override = seg.get("label_override")
    if label_override:
        label = label_override
    else:
        # Use source_override if set, otherwise original source
        source = seg.get("source_override") or seg["source"]
        if speaker_labels and source in speaker_labels:
            label = speaker_labels[source]
        else:
            label = _SOURCE_LABELS.get(source, source)
    start = seg.get("start_time", 0) or 0
    end = seg.get("end_time", 0) or 0
    if start > 0 or end > 0:
        return f"[{_fmt_time(start)}] [{label}] {seg['text']}"
    return f"[{label}] {seg['text']}"

def _build_transcript(segments: list[dict], speaker_labels: dict | None = None) -> str:
    """Join annotated segments into a single transcript string."""
    return "\n".join(_fmt_segment(s, speaker_labels) for s in segments)


def _build_session_meta(
    segments: list[dict],
    speaker_labels: dict | None = None,
    session_title: str = "",
    is_live: bool = False,
    started_at: str = "",
    ended_at: str = "",
    custom_prompt: str = "",
    current_summary: str = "",
) -> dict:
    """Gather rich metadata about the session for AI context."""
    # Compute speaker roster - only show user-assigned display names.
    # If multiple raw keys map to the same name, deduplicate.
    sources = set()
    for s in segments:
        src = s.get("source", "loopback")
        sources.add(src)
    seen_names = set()
    speakers = []
    for src in sorted(sources):
        if speaker_labels and src in speaker_labels:
            display = speaker_labels[src]
        elif src in _SOURCE_LABELS:
            display = _SOURCE_LABELS[src]
        else:
            display = src
        if display not in seen_names:
            seen_names.add(display)
            speakers.append(display)

    # Duration
    times = [s.get("start_time", 0) or 0 for s in segments] + [s.get("end_time", 0) or 0 for s in segments]
    max_time = max(times) if times else 0
    duration_str = _fmt_time(max_time) if max_time > 0 else "unknown"

    # Audio source breakdown
    source_types = set()
    for s in segments:
        src = s.get("source", "loopback")
        if src in _SOURCE_LABELS:
            source_types.add(src)

    return {
        "title": session_title,
        "is_live": is_live,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration": duration_str,
        "segment_count": len(segments),
        "speakers": speakers,
        "has_desktop_audio": "loopback" in source_types or "both" in source_types,
        "has_mic_audio": "mic" in source_types or "both" in source_types,
        "custom_prompt": custom_prompt,
        "current_summary": current_summary,
    }


# ── Noise / filler detection ──────────────────────────────────────────────────

_NOISE_LABEL = "[Noise]"

# Single filler words / sounds (case-insensitive, matched after stripping punctuation)
_FILLER_WORDS = frozenset({
    "um", "uh", "hm", "hmm", "huh", "mm", "mhm", "mmhmm", "ah", "oh",
    "yeah", "yep", "yup", "nah", "nope", "okay", "ok", "hey", "hi",
    "yes", "no", "so", "and", "but", "like", "right", "sure", "well",
    "sorry", "thanks", "deal", "cool", "wow", "alright", "bye",
    "heh", "hah", "ha", "lol",
})

# Patterns that are noise when they appear as the entire text
_NOISE_PATTERNS = [
    re.compile(r"^(ha|he|heh|hah|ho)+[.!?…]*$", re.I),           # laughter
    re.compile(r"^[.…!?\-\s]+$"),                                   # pure punctuation
    re.compile(r"^(um|uh|ah|oh|hm|mm|mhm|mmhmm)[\s,.…!?]*$", re.I),  # pure filler sounds
]

_NOISE_STRIP_PUNCT = re.compile(r"[^\w\s]")


def _is_noise_segment(text: str, duration: float) -> bool:
    """Return True if *text* looks like filler / noise rather than real speech.

    Criteria (all require the segment to be very short):
    - Single filler word (e.g. "Yeah.", "Um...", "Okay")
    - Two-word filler combos (e.g. "Sorry. Yeah.", "Heh heh.")
    - Trailing-off fragment ≤3 words ending with "…" or "..."
    - Matches a noise regex (laughter, pure punctuation)
    - Duration under 1.5 s with ≤2 words
    """
    stripped = text.strip()
    if not stripped:
        return True

    # Normalize
    clean = _NOISE_STRIP_PUNCT.sub("", stripped).strip().lower()
    words = clean.split()
    word_count = len(words)

    # Check noise regex patterns on full text
    for pat in _NOISE_PATTERNS:
        if pat.match(stripped):
            return True

    # Single filler word
    if word_count == 1 and words[0] in _FILLER_WORDS:
        return True

    # Two filler words (e.g. "Sorry. Yeah.", "Heh heh.", "Oh okay")
    if word_count == 2 and all(w in _FILLER_WORDS for w in words):
        return True

    # Short trailing fragment (≤3 words ending in … or ...)
    if word_count <= 3 and (stripped.endswith("…") or stripped.endswith("...")):
        return True

    # Very short duration with very few words
    if duration < 1.5 and word_count <= 2:
        return True

    return False


# ── Transcription callback ────────────────────────────────────────────────────

def _on_segment(
    text: str,
    source: str = "loopback",
    start_time: float = 0.0,
    end_time: float = 0.0,
) -> None:
    merged = False
    merge_seg_id = None

    with _state_lock:
        sid = _state["session_id"]
        if not sid:
            return

        # Auto-detect noise/filler segments from diarized speakers.
        # Only label as noise if this speaker hasn't produced any real
        # (non-noise) segments yet - once confirmed, keep the speaker label.
        original_source = None
        duration = end_time - start_time if end_time > start_time else 0.0
        confirmed = _state.get("_confirmed_speakers", set())
        if (source.startswith("Speaker")
                and source not in confirmed
                and _is_noise_segment(text, duration)):
            original_source = source
            source = _NOISE_LABEL
        elif source.startswith("Speaker"):
            confirmed.add(source)
            _state["_confirmed_speakers"] = confirmed

        segments = _state["segments"]

        # Merge with previous segment if same speaker, short gap, and
        # previous text didn't end with sentence-ending punctuation.
        if segments:
            prev = segments[-1]
            same_speaker = prev["source"] == source
            gap = (start_time - prev["end_time"]
                   if start_time > 0 and prev.get("end_time", 0) > 0
                   else float("inf"))
            prev_text = prev["text"].rstrip()
            prev_incomplete = prev_text and prev_text[-1] not in ".?!"

            if same_speaker and gap < 2.0 and prev_incomplete:
                prev["text"] = prev["text"] + " " + text
                prev["end_time"] = end_time
                merge_seg_id = prev.get("_seg_id")
                merged = True
                # Use full merged text for DB / SSE
                text = prev["text"]
                start_time = prev["start_time"]

        if not merged:
            seg_entry = {
                "text": text, "source": source,
                "start_time": start_time, "end_time": end_time,
                "_seg_id": None,  # filled after DB insert
            }
            if original_source:
                seg_entry["_original_source"] = original_source
            segments.append(seg_entry)

        # If this speaker was just confirmed (first non-noise segment),
        # retroactively reclaim any earlier noise segments from them.
        reclaim_segs = []
        if (original_source is None and source.startswith("Speaker")
                and source in confirmed):
            for seg in segments:
                if (seg["source"] == _NOISE_LABEL
                        and seg.get("_original_source") == source):
                    seg["source"] = source
                    del seg["_original_source"]
                    if seg.get("_seg_id"):
                        reclaim_segs.append(seg)

        if source != _NOISE_LABEL:
            now_mono = time.monotonic()
            _state["last_transcript_activity_at"] = now_mono
            _state["quiet_prompt_armed"] = True

        _state["pending_segments"] += 1
        should_summarize = (
            settings.get("auto_summary", True)
            and _state["pending_segments"] >= AUTO_SUMMARY_EVERY
            and not _state["is_reanalyzing"]
            and not _state["summary_generating"]
            and not _state["summary_manual_pending"]
        )
        if should_summarize:
            _state["pending_segments"] = 0
            existing_summary = _state["summary"]
            new_seg_count = len(_state["segments"])
            new_transcript = _build_transcript(
                _state["segments"], _state["speaker_labels"]
            )
            custom_prompt = _state["custom_prompt"]
            meta = _build_session_meta(
                _state["segments"],
                _state["speaker_labels"],
                is_live=True,
                custom_prompt=custom_prompt,
                current_summary=existing_summary,
            )

    if merged and merge_seg_id is not None:
        storage.update_segment(merge_seg_id, text, end_time)
        _push("transcript_update", {
            "seg_id": merge_seg_id, "text": text, "end_time": end_time,
            "session_id": sid,
        })
    else:
        seg_id = storage.save_segment(sid, text, source, start_time, end_time)
        if not merged:
            # Store DB id for future merges
            with _state_lock:
                if segments:
                    segments[-1]["_seg_id"] = seg_id
        _push("transcript", {
            "text": text, "source": source, "session_id": sid,
            "start_time": start_time, "end_time": end_time,
            "seg_id": seg_id,
        })

    # Reclaim noise segments that now belong to a confirmed speaker
    for seg in reclaim_segs:
        storage.update_segment_source(seg["_seg_id"], seg["source"])
        _push("transcript_update", {
            "seg_id": seg["_seg_id"], "text": seg["text"],
            "end_time": seg["end_time"], "source": seg["source"],
            "session_id": sid,
        })

    if should_summarize:
        threading.Thread(
            target=_run_summary,
            args=(sid, existing_summary, new_transcript, new_seg_count, custom_prompt, meta),
            daemon=True,
        ).start()


def _run_summary(
    session_id: str,
    existing_summary: str,
    transcript: str,
    seg_count: int,
    custom_prompt: str = "",
    meta: dict | None = None,
    update_context: str = "",
    is_auto: bool = True,
    clears_pending: bool = False,
) -> None:
    """Run a summary update and broadcast the result via SSE.

    Serialized via _summary_lock so auto and manual runs never overlap.

    is_auto=True  (segment-triggered): skips if a manual is pending; re-reads
                  existing_summary after acquiring the lock so it always bases
                  off the latest state even if it queued behind another run.
    is_auto=False (manual / speaker-rename / reanalysis): always runs.
    clears_pending=True: clear summary_manual_pending when we start (only for
                  the direct /api/summarize trigger).

    First summary: streams token-by-token via summary_start/chunk/done.
    Subsequent:   calls patch_summary() and pushes summary_replace.
    """
    with _summary_lock:
        with _state_lock:
            is_active = _state["session_id"] == session_id
            if is_auto and not is_active:
                return
            if clears_pending and is_active:
                _state["summary_manual_pending"] = False
            elif is_auto:
                # Bail if a manual is queued - it will run as soon as we finish
                if _state["summary_manual_pending"]:
                    return
                # Re-read in case a prior run updated the summary while we waited
                existing_summary = _state["summary"]
            if is_active:
                _state["summary_generating"] = True

        mode = "generating" if not existing_summary else "updating"
        _push("summary_busy", {"busy": True, "mode": mode, "session_id": session_id})

        try:
            def _persist(content: str) -> None:
                with _state_lock:
                    # Auto: discard result if a manual was requested during our run
                    if is_auto and _state.get("summary_manual_pending"):
                        return
                    if _state["session_id"] == session_id:
                        _state["summary"] = content
                        _state["summarized_seg_count"] = seg_count
                storage.save_summary(session_id, content)

            if existing_summary:
                # ── Incremental patch - check for preemption before the AI call ─
                with _state_lock:
                    if is_auto and _state.get("summary_manual_pending"):
                        return
                sp, sm = _resolve_tool_ai("summary")
                content = ai.patch_summary(
                    existing_summary,
                    transcript,
                    custom_prompt,
                    meta=meta,
                    update_context=update_context,
                    provider=sp, model=sm,
                )
                # Check again after the (potentially slow) AI call
                with _state_lock:
                    if is_auto and _state.get("summary_manual_pending"):
                        return
                _persist(content)
                _push("summary_replace", {"content": content, "session_id": session_id})
            else:
                # ── First summary - stream it so the user sees it appear ──────
                _push("summary_start", {"session_id": session_id})
                chunks: list[str] = []

                def on_token(t: str) -> None:
                    chunks.append(t)
                    _push("summary_chunk", {"text": t, "session_id": session_id})

                def on_done() -> None:
                    _persist("".join(chunks))
                    _push("summary_done", {"session_id": session_id})

                sp, sm = _resolve_tool_ai("summary")
                # Resolve effective system prompt: session override > global > built-in
                sess_sp = storage.get_session_summary_prompt(session_id)
                global_sp = settings.get("summary_system_prompt") or None
                effective_sp = sess_sp or global_sp
                ai.summarize(transcript, on_token, on_done, custom_prompt=custom_prompt, meta=meta,
                             provider=sp, model=sm,
                             system_prompt=effective_sp)
        finally:
            with _state_lock:
                if _state["session_id"] == session_id:
                    _state["summary_generating"] = False
            _push("summary_busy", {"busy": False, "session_id": session_id})
            # Refresh semantic embedding after summary (content is most complete now)
            update_session_embedding(session_id)


def _queue_speaker_summary_refresh(session_id: str, update_context: str) -> None:
    """Patch the current summary after speaker-label changes."""
    if not update_context.strip():
        return
    if not settings.get("auto_summary", True):
        return

    with _state_lock:
        # custom_prompt mirrors whichever session the user is viewing in the
        # textarea — always honor it regardless of active recording session.
        custom_prompt = _state["custom_prompt"]
        if _state["session_id"] == session_id:
            existing_summary = _state["summary"]
            if not existing_summary:
                return
            segments = list(_state["segments"])
            labels = dict(_state["speaker_labels"])
            transcript = _build_transcript(segments, labels)
            seg_count = len(segments)
            meta = _build_session_meta(
                segments,
                labels,
                is_live=_state["is_recording"],
                custom_prompt=custom_prompt,
                current_summary=existing_summary,
            )
        else:
            existing_summary = ""
            transcript = ""
            seg_count = 0
            meta = None

    if not existing_summary:
        sess = storage.get_session(session_id)
        if not sess:
            return
        existing_summary = sess.get("summary", "")
        if not existing_summary:
            return
        labels = sess.get("speaker_labels") or {}
        transcript = _build_transcript(sess["segments"], labels)
        seg_count = len(sess["segments"])
        meta = _build_session_meta(
            sess["segments"],
            labels,
            session_title=sess.get("title", ""),
            is_live=False,
            started_at=sess.get("started_at", ""),
            ended_at=sess.get("ended_at", ""),
            current_summary=existing_summary,
            custom_prompt=custom_prompt,
        )

    threading.Thread(
        target=_run_summary,
        args=(session_id, existing_summary, transcript, seg_count, custom_prompt, meta, update_context),
        kwargs={"is_auto": False},
        daemon=True,
    ).start()


# ── Model loading ─────────────────────────────────────────────────────────────

def _load_model() -> None:
    try:
        if _saved_whisper_preset:
            preset = next((p for p in WHISPER_PRESETS if p["id"] == _saved_whisper_preset), None)
            if preset and (not preset["requires_cuda"] or get_cuda_available()):
                _transcriber.device = preset["device"]
                _transcriber.compute_type = preset["compute_type"]
                _transcriber.model_size = preset["model_size"]
                _transcriber._auto_model_config = False
                log.info("settings", f"Restored whisper preset: {_saved_whisper_preset}")
        _transcriber.load_model()
        info = _transcriber.device_info
        with _state_lock:
            _state["model_ready"] = True
            _state["model_info"] = info
        _push_status()
    except Exception as e:
        log.error("whisper", f"Error loading model: {e}")
        with _state_lock:
            _state["model_ready"] = False
            _state["model_info"] = f"Error: {e}"
        _push_status()


def _load_diarizer() -> None:
    hf_token = os.getenv("HUGGING_FACE_KEY")
    if not hf_token:
        log.warn("diarizer", "HUGGING_FACE_KEY not set - speaker diarization disabled.")
        return
    try:
        saved_device = settings.get("diarizer_device", "")
        # Validate the saved choice against what the current machine actually
        # supports — accelerator strings ("cuda", "mps") only honored if probe
        # succeeds, falling back to auto-detection otherwise.
        from core.compute_device import best_torch_device
        _accel_ok = best_torch_device() in ("cuda", "mps")
        if saved_device and (saved_device == "cpu" or _accel_ok):
            log.info("settings", f"Restored diarizer device: {saved_device}")
            _transcriber.load_diarizer(hf_token, device=saved_device)
        else:
            _transcriber.load_diarizer(hf_token)
        with _state_lock:
            _state["diarizer_ready"] = True
        _push_status()
        log.info("diarizer", "Speaker diarization ready.")
        if fingerprint_db.ready:
            _transcriber.fingerprint_callback = _on_fingerprint_audio
    except Exception as e:
        import traceback
        log.error("diarizer", f"Error loading models: {e}")
        log.error("diarizer", traceback.format_exc().rstrip())
        log.warn("diarizer", "Transcription will continue without speaker labels.")
        with _state_lock:
            _state["diarizer_ready"] = False
            _state["diarizer_failed"] = True
        _push_status()


def _load_fingerprint_db() -> None:
    """Load the speaker embedding model. Called after all module globals are set."""
    fingerprint_db.__init__(storage.DB_PATH, os.getenv("HUGGING_FACE_KEY", ""))
    # Wire callback if diarizer already finished loading before we did
    with _state_lock:
        diarizer_ready = _state.get("diarizer_ready", False)
    if fingerprint_db.ready and diarizer_ready:
        _transcriber.fingerprint_callback = _on_fingerprint_audio


def _load_text_embeddings() -> None:
    """Load the sentence-transformers model and index any unembedded sessions."""
    text_embeddings.ensure_loaded()
    if not text_embeddings.is_ready():
        return
    # Background-index sessions that don't have embeddings yet
    _reindex_embeddings()


def _reindex_embeddings() -> None:
    """Compute embeddings for any sessions missing them."""
    if not text_embeddings.is_ready():
        return
    unembedded = storage.get_unembedded_session_ids()
    if not unembedded:
        return
    log.info("embeddings", f"Indexing {len(unembedded)} sessions for semantic search…")
    for sid in unembedded:
        text = storage.get_session_text_for_embedding(sid)
        if not text:
            continue
        vec = text_embeddings.encode(text)
        if vec is not None:
            storage.save_session_embedding(sid, text_embeddings.embedding_to_bytes(vec))
    log.info("embeddings", f"Semantic indexing complete.")


def update_session_embedding(session_id: str) -> None:
    """Recompute the embedding for a single session (call after content changes)."""
    if not text_embeddings.is_ready():
        return
    text = storage.get_session_text_for_embedding(session_id)
    if not text:
        return
    vec = text_embeddings.encode(text)
    if vec is not None:
        storage.save_session_embedding(session_id, text_embeddings.embedding_to_bytes(vec))


def _start_background_initializers() -> None:
    global _startup_init_started
    with _startup_init_lock:
        if _startup_init_started:
            return
        _startup_init_started = True
    threading.Thread(target=_load_model, daemon=True).start()
    threading.Thread(target=_load_diarizer, daemon=True).start()
    threading.Thread(target=_load_fingerprint_db, daemon=True).start()
    threading.Thread(target=_load_text_embeddings, daemon=True).start()
    # Warm the AI /models cache so the settings pane opens instantly on first
    # visit. Non-blocking; if the network is slow/unreachable the fallback
    # static lists are used until the fetch completes.
    threading.Thread(target=_get_all_models_live, daemon=True).start()
    # Auto-record call watcher (Windows): cheap registry polling, no-op until
    # the auto_record_enabled setting is turned on.
    try:
        from ui_desktop.call_watch import CALL_WATCH_AVAILABLE, CallWatcher
        if CALL_WATCH_AVAILABLE:
            _auto_record["watcher"] = CallWatcher(_auto_record_config, _auto_record_tick)
            _auto_record["watcher"].start()
    except Exception as e:
        log.warn("auto-record", f"Call watcher unavailable: {e}")


def _level_push_loop() -> None:
    """Push audio levels to all SSE clients at ~12 fps while recording or testing."""
    while True:
        time.sleep(0.08)
        with _state_lock:
            is_rec  = _state["is_recording"]
            is_test = _state["is_testing"]
            capture = _state["audio_capture"] if is_rec else _state["test_capture"]
        if capture and (is_rec or is_test):
            level = max(float(capture.loopback_level), float(capture.mic_level))
            if is_rec and level >= _quiet_audio_rms_threshold:
                with _state_lock:
                    _state["last_audio_activity_at"] = time.monotonic()
                    _state["quiet_prompt_armed"] = True
            payload = {
                "loopback":    round(capture.loopback_level, 4),
                "mic":         round(capture.mic_level, 4),
                "has_mic":     capture._has_mic,
                "lb_spectrum": capture.compute_spectrum(capture._lb_fft_buf),
                "mic_spectrum":capture.compute_spectrum(capture._mic_fft_buf),
                "lb_gain":     capture.loopback_gain,
                "mic_gain":    capture.mic_gain,
            }
            # Include AGC debug info when either AGC is enabled
            if capture.agc_loopback_enabled or capture.agc_mic_enabled:
                payload["agc"] = {
                    "lb_gain":     round(float(capture.agc_lb_gain), 2),
                    "lb_env":      round(float(capture.agc_lb_envelope), 5),
                    "lb_gated":    bool(capture.agc_lb_gated),
                    "lb_enabled":  bool(capture.agc_loopback_enabled),
                    "mic_gain":    round(float(capture.agc_mic_gain), 2),
                    "mic_env":     round(float(capture.agc_mic_envelope), 5),
                    "mic_gated":   bool(capture.agc_mic_gated),
                    "mic_enabled": bool(capture.agc_mic_enabled),
                    "target":      float(capture.agc_target_rms),
                    "gate":        float(capture.agc_gate_threshold),
                    "max_gain":    float(capture.agc_max_gain),
                }
            _push("audio_level", payload)


threading.Thread(target=_level_push_loop, daemon=True).start()


def _quiet_prompt_loop() -> None:
    """Send a Windows toast when an active recording has gone quiet."""
    global _quiet_audio_rms_threshold
    while True:
        time.sleep(1.0)
        cfg = settings.load()
        _quiet_audio_rms_threshold = float(cfg.get("quiet_prompt_audio_rms_threshold", 0.006))
        if not cfg.get("quiet_prompt_enabled", True):
            continue
        threshold_sec = max(5.0, float(cfg.get("quiet_prompt_threshold_sec", 30)))
        cooldown_sec = max(0.0, float(cfg.get("quiet_prompt_cooldown_sec", 120)))
        require_no_transcript = bool(cfg.get("quiet_prompt_require_no_transcript", True))
        now = time.monotonic()
        with _state_lock:
            if not _state["is_recording"] or not _state["session_id"]:
                continue
            sid = _state["session_id"]
            last_audio = _state.get("last_audio_activity_at") or _state.get("recording_started_at_monotonic") or now
            last_transcript = _state.get("last_transcript_activity_at") or _state.get("recording_started_at_monotonic") or now
            sent_at = _state.get("quiet_prompt_sent_at") or 0.0
            armed = bool(_state.get("quiet_prompt_armed", True))
            audio_quiet = now - last_audio
            transcript_quiet = now - last_transcript
            if not armed:
                continue
            if audio_quiet < threshold_sec:
                continue
            if require_no_transcript and transcript_quiet < threshold_sec:
                continue
            if sent_at and now - sent_at < cooldown_sec:
                continue
            _state["quiet_prompt_armed"] = False
            _state["quiet_prompt_sent_at"] = now

        sent = notifications.send_quiet_recording_toast(sid, _server_url)
        if sent:
            log.info("notify", f"Quiet recording toast sent for session {sid[:8]}")


threading.Thread(target=_quiet_prompt_loop, daemon=True).start()


# ── Auto-record (call detection) ──────────────────────────────────────────────

_auto_record = {
    "session_id": None,    # session auto-started by the watcher (None = none)
    "disarmed": False,     # user manually stopped mid-call → hold off until idle
    "starting": False,     # a start attempt is in flight
    "watcher": None,       # CallWatcher instance (None off-Windows)
    "fail_count": 0,       # consecutive failed start attempts this call
    "backoff_until": 0.0,  # monotonic time before which no retry is attempted
}


def _auto_record_config() -> dict:
    s = settings.load()
    return {
        "enabled": bool(s.get("auto_record_enabled")),
        "apps": str(s.get("auto_record_apps") or ""),
        "stop_delay_sec": s.get("auto_record_stop_delay_sec", 20),
        "notify": bool(s.get("auto_record_notify", True)),
    }


def _auto_record_api(path: str, body: dict) -> dict | None:
    """POST to our own HTTP API so auto start/stop runs the exact same code
    path as the Record button (session, WAV, SSE, screen recording, titling)."""
    import urllib.request
    try:
        req = urllib.request.Request(
            f"{_server_url}{path}", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=30)
        return json.loads(resp.read())
    except Exception as e:
        log.warn("auto-record", f"Internal API call {path} failed: {e}")
        return None


def _auto_record_start(apps: list[str]) -> None:
    """Start a recording for the detected call. Runs on its own thread because
    model loading can take minutes right after login."""
    try:
        deadline = time.monotonic() + 180
        while True:
            watcher = _auto_record["watcher"]
            if watcher is None or not watcher.in_call or not _auto_record_config()["enabled"]:
                return  # call ended (or feature disabled) while waiting
            with _state_lock:
                if _state["is_recording"] or _state["is_testing"]:
                    return  # user beat us to it, or an audio test is running
                ready, _reason = _recording_prereqs_locked()
            if ready:
                break
            if time.monotonic() > deadline:
                log.warn("auto-record", "Models not ready after 180 s - giving up on this call.")
                return
            time.sleep(2)

        resp = _auto_record_api("/api/recording/start", {"auto_record": True})
        if not resp or "session_id" not in resp:
            # Back off so a persistent failure (e.g. no audio devices at all)
            # doesn't create an orphan session every poll for the whole call.
            _auto_record["fail_count"] += 1
            if _auto_record["fail_count"] >= 3:
                _auto_record["disarmed"] = True
                log.warn("auto-record", "3 failed start attempts - giving up until the call ends.")
            else:
                _auto_record["backoff_until"] = time.monotonic() + 30
                log.warn("auto-record", "Start attempt failed - retrying in 30 s.")
            return
        _auto_record["fail_count"] = 0
        _auto_record["session_id"] = resp["session_id"]
        pretty = ", ".join(apps) if apps else "a meeting app"
        log.info("auto-record", f"Recording auto-started for call in {pretty}.")
        if _auto_record_config()["notify"]:
            notifications.notify(
                "Recording started",
                f"Detected a call ({pretty}). Click to open Meeting Assistant.",
                on_click=lambda _arg: webbrowser.open(_server_url),
            )
    finally:
        _auto_record["starting"] = False


def _auto_record_tick(in_call: bool, apps: list[str]) -> None:
    """Recording policy, evaluated on every watcher poll (~2 s)."""
    auto_sid = _auto_record["session_id"]
    with _state_lock:
        recording   = _state["is_recording"]
        current_sid = _state["session_id"]

    if in_call:
        if auto_sid and (not recording or current_sid != auto_sid):
            # Our auto session was stopped manually mid-call: stand down until
            # the call actually ends so we don't fight the user.
            _auto_record["session_id"] = None
            _auto_record["disarmed"] = True
            log.info("auto-record", "Manual stop during call - disarmed until mic goes idle.")
        elif (not recording and not _auto_record["disarmed"]
                and not _auto_record["starting"]
                and time.monotonic() >= _auto_record["backoff_until"]):
            _auto_record["starting"] = True
            threading.Thread(target=_auto_record_start, args=(apps,), daemon=True).start()
    else:
        _auto_record["disarmed"] = False
        _auto_record["fail_count"] = 0
        _auto_record["backoff_until"] = 0.0
        if auto_sid:
            _auto_record["session_id"] = None
            if recording and current_sid == auto_sid:
                resp = _auto_record_api("/api/recording/stop", {})
                if resp:
                    log.info("auto-record", f"Recording auto-stopped - session {auto_sid}.")
                    if _auto_record_config()["notify"]:
                        notifications.notify(
                            "Recording saved",
                            "The call ended. Transcript and summary are ready.",
                            on_click=lambda _arg: webbrowser.open(_server_url),
                        )


@app.route("/api/auto-record/status")
def auto_record_status():
    aw = _auto_record["watcher"]
    cfg = _auto_record_config()
    return jsonify({
        "supported": aw is not None,
        "enabled": cfg["enabled"],
        "apps": cfg["apps"],
        "stop_delay_sec": cfg["stop_delay_sec"],
        "notify": cfg["notify"],
        "in_call": bool(aw.in_call) if aw else False,
        "current_apps": list(aw.current_apps) if aw else [],
        "disarmed": _auto_record["disarmed"],
        "active_session": _auto_record["session_id"],
    })


# ── Obsidian export ───────────────────────────────────────────────────────────
# Drops each finalized session into an Obsidian vault folder as markdown, and
# keeps the file current when the transcript is edited afterwards (speaker
# renames, segment reassignments, cleanup, reanalysis, retitles).

_obsidian_timers: dict[str, threading.Timer] = {}
_obsidian_timers_lock = threading.Lock()


def _obsidian_export_dir() -> Path | None:
    """Resolve the configured vault folder, or None if export is off/broken."""
    if not settings.get("obsidian_export_enabled"):
        return None
    raw = str(settings.get("obsidian_export_dir") or "").strip()
    if not raw:
        return None
    p = Path(raw)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warn("obsidian", f"Export dir unavailable: {e}")
        return None
    return p


def _safe_filename(name: str, max_len: int = 80) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip().rstrip(".")
    return name[:max_len].rstrip() or "Untitled"


def _session_local_dt(iso: str):
    """Session timestamps are naive UTC (storage._now); convert to local."""
    from datetime import datetime as _dt, timezone as _tz
    return _dt.fromisoformat(iso).replace(tzinfo=_tz.utc).astimezone()


def _build_obsidian_markdown(sess: dict) -> str | None:
    """Render a session as a vault-ready markdown doc. None = nothing worth exporting."""
    from datetime import datetime as _dt

    speaker_labels = sess.get("speaker_labels") or {}
    lines: list[str] = []
    speakers_seen: list[str] = []
    for seg in sess.get("segments", []):
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        # Mirror _fmt_segment's precedence: per-segment override, then
        # speaker-key reassignment, then the session speaker name.
        label = seg.get("label_override")
        if not label:
            source = seg.get("source_override") or seg["source"]
            label = speaker_labels.get(source) or _SOURCE_LABELS.get(source, source)
        if label.strip().strip("[]").lower() == "noise":
            continue  # hidden by default in the UI; keep the vault doc clean
        if label not in speakers_seen:
            speakers_seen.append(label)
        start = seg.get("start_time", 0) or 0
        lines.append(f"**[{_fmt_time(start)}] {label}:** {text}")
    if not lines:
        return None

    title = sess.get("title") or "Untitled meeting"
    started = sess.get("started_at") or ""
    ended = sess.get("ended_at") or ""
    date = started[:10]
    started_local = started
    duration = ""
    try:
        if started:
            local = _session_local_dt(started)
            date = local.date().isoformat()
            started_local = local.isoformat(timespec="seconds")
        if started and ended:
            secs = (_dt.fromisoformat(ended) - _dt.fromisoformat(started)).total_seconds()
            duration = _fmt_time(max(0.0, secs))
    except ValueError:
        pass

    front = [
        "---",
        f'title: "{title.replace(chr(34), chr(39))}"',
        "type: meeting-transcript",
        f"date: {date}",
        f"started: {started_local}",
        f"duration: {duration}",
        "speakers: [" + ", ".join(f'"{s}"' for s in speakers_seen) + "]",
        f"session_id: {sess['id']}",
        "source: Meeting Assistant",
        f"exported: {_dt.now().astimezone().isoformat(timespec='seconds')}",
        "---",
        "",
        f"# {title}",
        "",
    ]
    body: list[str] = []
    summary = (sess.get("summary") or "").strip()
    if summary:
        body += ["## Summary", "", summary, ""]
    body += ["## Transcript", ""]
    return "\n".join(front) + "\n" + "\n".join(body) + "\n" + "\n\n".join(lines) + "\n"


def _obsidian_export_session(session_id: str) -> None:
    try:
        out_dir = _obsidian_export_dir()
        if out_dir is None:
            return
        sess = storage.get_session(session_id)
        if not sess or not sess.get("ended_at"):
            return  # only export finalized sessions
        md = _build_obsidian_markdown(sess)
        if md is None:
            return  # empty / all-noise sessions don't pollute the vault
        short = session_id[:8]
        started = sess.get("started_at") or ""
        try:
            date = _session_local_dt(started).date().isoformat() if started else ""
        except ValueError:
            date = started[:10]
        fname = f"{date} {_safe_filename(sess.get('title') or 'Untitled meeting')} [{short}].md"
        target = out_dir / fname
        # A retitle changes the filename; replace the previous export for
        # this session rather than leaving a stale duplicate behind.
        for old in out_dir.glob(f"* [{short}].md"):
            if old != target:
                try:
                    old.unlink()
                except OSError:
                    pass
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(md, encoding="utf-8")
        tmp.replace(target)
        log.info("obsidian", f"Exported session {short} -> {target.name}")
    except Exception as e:
        log.warn("obsidian", f"Export failed for {session_id}: {e}")


def _queue_obsidian_export(session_id: str, delay: float = 4.0) -> None:
    """Debounced re-export: bulk edits (multi-segment reassigns, cleanup
    apply, merge cascades) collapse into a single file write."""
    if not settings.get("obsidian_export_enabled"):
        return
    with _obsidian_timers_lock:
        prev = _obsidian_timers.pop(session_id, None)
        if prev:
            prev.cancel()
        timer = threading.Timer(delay, _obsidian_export_session, args=(session_id,))
        timer.daemon = True
        _obsidian_timers[session_id] = timer
        timer.start()


@app.route("/api/obsidian/status")
def obsidian_status():
    return jsonify({
        "enabled": bool(settings.get("obsidian_export_enabled")),
        "dir": str(settings.get("obsidian_export_dir") or ""),
    })


@app.route("/api/obsidian/export-all", methods=["POST"])
def obsidian_export_all():
    """One-shot backfill: export every finalized session with content."""
    out_dir = _obsidian_export_dir()
    if out_dir is None:
        return jsonify({"error": "Obsidian export is not enabled / configured"}), 400
    exported = skipped = 0
    for s in storage.list_sessions():
        before = len(list(out_dir.glob(f"* [{s['id'][:8]}].md")))
        _obsidian_export_session(s["id"])
        after = len(list(out_dir.glob(f"* [{s['id'][:8]}].md")))
        if after:
            exported += 1
        elif not before:
            skipped += 1
    return jsonify({"ok": True, "exported": exported, "skipped": skipped, "dir": str(out_dir)})


# ── Speaker fingerprint helpers ───────────────────────────────────────────────

def _auto_apply_fingerprint(speaker_key: str, match: dict, emb: np.ndarray, session_id: str) -> None:
    """Silently apply a high-confidence fingerprint match: link, rename, push SSEs."""
    global_id = match["global_id"]
    name  = match["name"]
    color = match.get("color")
    fingerprint_db.add_embedding(global_id, session_id, speaker_key, emb, 0.0)
    fingerprint_db.link_session_speaker(session_id, speaker_key, global_id)
    storage.save_speaker_label(session_id, speaker_key, name=name, color=color)
    with _state_lock:
        if _state["session_id"] == session_id:
            _state["speaker_labels"][speaker_key] = name
    _push("speaker_label", {"session_id": session_id, "speaker_key": speaker_key,
                             "name": name, "color": color})
    _push("fingerprint_auto_applied", {"session_id": session_id, "speaker_key": speaker_key,
                                       "global_id": global_id, "name": name,
                                       "similarity": match["similarity"]})
    _push("speaker_linked", {"session_id": session_id, "speaker_key": speaker_key,
                              "global_id": global_id, "name": name})
    log.info("fingerprint", f"Auto-applied {name!r} → {speaker_key} (sim={match['similarity']:.2f})")


def _on_fingerprint_audio(speaker_key: str, audio: np.ndarray, abs_start: float, abs_end: float) -> None:
    """Called from the transcriber thread for each recognized speaker segment.
    Accumulates audio per speaker_key; extracts embeddings once MIN_DURATION_SEC reached.
    """
    if not fingerprint_db.ready:
        return
    duration = abs_end - abs_start
    if duration <= 0 or audio is None or len(audio) == 0:
        return

    with _state_lock:
        sid = _state.get("session_id")
        if not sid:
            return
        counts = _state["speaker_emb_counts"]
        count = counts.get(speaker_key, 0)
        if count >= 15:
            return  # hard cap for this session
        if count >= 5:
            # Diminishing returns: only extract every 3rd opportunity
            offers = _state["speaker_offer_counts"]
            offers[speaker_key] = offers.get(speaker_key, 0) + 1
            if offers[speaker_key] % 3 != 0:
                return
        accum = _state["speaker_audio_accum"]
        if speaker_key not in accum:
            accum[speaker_key] = {"audio": audio.copy(), "total_sec": duration}
        else:
            accum[speaker_key]["audio"] = np.concatenate([accum[speaker_key]["audio"], audio])
            accum[speaker_key]["total_sec"] += duration
        if accum[speaker_key]["total_sec"] < fingerprint_db.MIN_DURATION_SEC:
            return

        # Snapshot and reset accumulator (keep last 0.5 s for continuity)
        seg_audio  = accum[speaker_key]["audio"].copy()
        tail_len   = min(int(0.5 * 16_000), len(accum[speaker_key]["audio"]))
        accum[speaker_key] = {"audio": accum[speaker_key]["audio"][-tail_len:], "total_sec": 0.5}
        counts[speaker_key] = counts.get(speaker_key, 0) + 1
        dismissals = {k: set(v) for k, v in _state["fingerprint_dismissals"].items()}

    # Check if already linked (strengthen profile)
    existing_link = fingerprint_db.get_link(sid, speaker_key)

    def _extract_and_match() -> None:
        emb = fingerprint_db.extract_embedding(seg_audio)
        if emb is None:
            log.info("fingerprint", f"{speaker_key}: embedding extraction failed")
            return

        # Profiles already linked to OTHER speaker_keys in this session — never
        # candidates regardless of whether speaker_key is linked or not.
        session_links = fingerprint_db.get_session_links(sid)
        other_links = {gid for k, gid in session_links.items()
                       if k != speaker_key and gid}

        if existing_link:
            fingerprint_db.add_embedding(existing_link, sid, speaker_key, emb, duration)
            return

        # Persist for the post-meeting cleanup UI — without this, embeddings
        # for unlabeled speakers would be discarded the moment _extract_and_match
        # returns and the clustering UI would have nothing to work with.
        try:
            fingerprint_db.add_unlabeled_embedding(sid, speaker_key, emb, duration)
        except Exception as e:
            log.warn("fingerprint", f"add_unlabeled_embedding failed: {e}")

        excluded = dismissals.get(speaker_key, set()) | other_links

        # Diagnostic: pull top candidates regardless of threshold so we can see
        # *why* a speaker isn't getting matched (closest profile sim too low,
        # library empty, all candidates excluded, etc.).
        all_candidates = fingerprint_db.find_matches(
            emb, exclude_global_ids=excluded, min_similarity=0.0,
        )
        if all_candidates:
            top_summary = ", ".join(
                f"{c['name']} sim={c['similarity']:.2f}"
                for c in all_candidates[:3]
            )
            log.info(
                "fingerprint",
                f"{speaker_key} closest: {top_summary} "
                f"(suggest>={fingerprint_db.SUGGEST_THRESHOLD:.2f}, "
                f"auto>={fingerprint_db.AUTO_APPLY_THRESHOLD:.2f})",
            )
        else:
            reason = "library empty" if not fingerprint_db.ready else "all candidates excluded/dismissed"
            log.info("fingerprint", f"{speaker_key}: no candidates ({reason})")

        # Actionable matches: those crossing the suggest threshold.
        matches = [c for c in all_candidates
                   if c["similarity"] >= fingerprint_db.SUGGEST_THRESHOLD]
        if not matches:
            return
        top = matches[0]
        if top["auto_apply"]:
            _auto_apply_fingerprint(speaker_key, top, emb, sid)
        else:
            with _state_lock:
                current_name = _state["speaker_labels"].get(speaker_key, speaker_key)
                suggestion = {"session_id": sid, "speaker_key": speaker_key,
                              "current_name": current_name, "matches": matches}
                _state["fingerprint_suggestions"][speaker_key] = suggestion
            _push("fingerprint_match", suggestion)

    threading.Thread(target=_extract_and_match, daemon=True).start()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def home():
    # Backwards compat: redirect /?session=xxx to /session?id=xxx
    session_param = request.args.get("session")
    if session_param:
        return redirect(f"/session?id={session_param}")
    # Redirect settings/setup to session page (which has the settings dialog)
    if request.args.get("settings") or request.args.get("setup"):
        return redirect("/session?settings=1")
    return render_template("home.html")


@app.route("/session")
def session_view():
    return render_template("index.html")


@app.route("/api/events")
def events():
    """SSE endpoint - streams all real-time events to the browser."""
    cid = str(uuid.uuid4())
    q: queue.Queue = queue.Queue(maxsize=200)
    with _cq_lock:
        _client_queues[cid] = q

    # Send initial state so a freshly-loaded page knows what's happening
    with _state_lock:
        active_sid = _state["session_id"] if _state["is_recording"] else None
    init = _status_payload()
    q.put(f"event: status\ndata: {json.dumps(init)}\n\n")

    # Replay active session so reconnecting clients catch up instantly
    if active_sid:
        after_seg_id = request.args.get("after_seg_id", 0, type=int)
        try:
            sess = storage.get_session(active_sid)
            if sess:
                segs = [s for s in sess.get("segments", [])
                        if s.get("id", 0) > after_seg_id]
                replay_payload = {
                    "session_id":      active_sid,
                    "segments":        segs,
                    "speaker_profiles": sess.get("speaker_profiles", []),
                    "summary":         sess.get("summary", "") or "",
                }
                q.put(f"event: replay\ndata: {json.dumps(replay_payload)}\n\n")
        except Exception:
            pass  # non-fatal - client will simply have a partial transcript

    def generate():
        try:
            while True:
                try:
                    yield q.get(timeout=25)
                except queue.Empty:
                    yield ": heartbeat\n\n"
        except GeneratorExit:
            pass
        finally:
            with _cq_lock:
                _client_queues.pop(cid, None)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/api/status")
def get_status():
    return jsonify(_status_payload())


@app.route("/api/audio/devices")
def get_audio_devices():
    try:
        data = enumerate_audio_devices()
        data["dshow"] = enumerate_dshow_audio_devices()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e), "loopback": [], "input": [], "dshow": []}), 500


@app.route("/api/audio/auto-detect", methods=["POST"])
def auto_detect_audio():
    """Test all audio devices simultaneously and return the best ones."""
    with _state_lock:
        if _state["is_recording"]:
            return jsonify({"error": "Cannot auto-detect while recording"}), 400
        if _state["is_testing"]:
            return jsonify({"error": "Stop audio test before auto-detecting"}), 400
    try:
        result = auto_detect_devices()
        return jsonify(result)
    except Exception as e:
        log.error("audio", f"Auto-detect failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/audio/gain", methods=["POST"])
def set_audio_gain():
    """Set loopback and/or mic gain on the active (or test) audio capture.

    No capture yet (e.g. the home page pushing stored gain values on load) is
    a normal idle state, not an error — we just report ``applied: False`` so
    the browser console stays clean.
    """
    data = request.get_json(silent=True) or {}
    with _state_lock:
        capture = _state["audio_capture"] or _state["test_capture"]
    if capture is None:
        return jsonify({"ok": False, "applied": False})
    if "lb_gain" in data:
        capture.loopback_gain = float(max(0.0, min(16.0, data["lb_gain"])))
    if "mic_gain" in data:
        capture.mic_gain = float(max(0.0, min(16.0, data["mic_gain"])))
    return jsonify({"ok": True, "applied": True})


@app.route("/api/sessions")
def list_sessions():
    return jsonify(storage.list_sessions())


@app.route("/api/search")
def search():
    """Full-text search across session titles, transcript content, and speaker names."""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    limit = request.args.get("limit", 30, type=int)
    fts_results = storage.search_sessions(q, limit=limit)
    speaker_results = storage.search_speakers(q, limit=limit)

    # Merge speaker results into FTS results — speaker matches first
    merged = {r["session_id"]: r for r in fts_results}
    for sr in speaker_results:
        sid = sr["session_id"]
        if sid in merged:
            # Prepend participant matches to existing results
            merged[sid]["matches"] = sr["matches"] + merged[sid]["matches"]
        else:
            merged[sid] = sr
    # Put sessions with participant matches first, then by original order
    has_participant = []
    no_participant = []
    for r in merged.values():
        if any(m["kind"] == "participant" for m in r["matches"]):
            has_participant.append(r)
        else:
            no_participant.append(r)
    results = has_participant + no_participant
    return jsonify(results[:limit])


@app.route("/api/search/semantic")
def search_semantic():
    """Semantic similarity search using text embeddings."""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    if not text_embeddings.is_ready():
        return jsonify({"error": "Semantic search model is still loading"}), 503
    limit = request.args.get("limit", 20, type=int)
    threshold = request.args.get("threshold", 0.25, type=float)

    query_vec = text_embeddings.encode(q)
    if query_vec is None:
        return jsonify({"error": "Failed to encode query"}), 500

    all_embs = storage.get_all_session_embeddings()
    scored = []
    for row in all_embs:
        vec = text_embeddings.bytes_to_embedding(row["embedding_bytes"])
        score = text_embeddings.cosine_similarity(query_vec, vec)
        if score >= threshold:
            scored.append({
                "session_id": row["session_id"],
                "title": row["title"],
                "score": round(score, 4),
                "matches": [{"kind": "semantic", "snippet": f"Similarity: {score:.0%}"}],
            })
    scored.sort(key=lambda x: x["score"], reverse=True)
    return jsonify(scored[:limit])


@app.route("/api/search/semantic/status")
def search_semantic_status():
    """Check if the semantic search model is ready."""
    return jsonify({
        "ready": text_embeddings.is_ready(),
        "loading": text_embeddings.is_loading(),
    })


@app.route("/api/sessions/<session_id>")
def get_session(session_id: str):
    data = storage.get_session(session_id)
    if not data:
        return jsonify({"error": "Not found"}), 404
    wav_path = paths.audio_dir() / f"{session_id}.wav"
    video_path = paths.video_dir() / f"{session_id}.mp4"
    data["has_audio"] = wav_path.exists()
    data["has_video"] = video_path.exists()
    data["video_offset"] = settings.get_video_offset(session_id)
    data["has_trim_backup"] = media_edit.has_trim_backup(session_id)
    # Split rollback: true when this session is part of a split group whose
    # pre-split backup is still on disk. The editor uses this to surface an
    # "Undo Split" action.
    group_id = data.get("split_group_id") or storage.get_session_split_group_id(session_id)
    data["split_group_id"]  = group_id
    data["has_split_backup"] = bool(group_id) and media_edit.has_split_backup(group_id)
    return jsonify(data)


@app.route("/api/audio/mic-chunk", methods=["POST"])
def mic_chunk():
    """Receive a raw mono Int16 PCM chunk from the browser mic and inject it
    into the currently active capture (recording or test)."""
    data = request.get_data()
    if data:
        with _state_lock:
            capture = (
                _state["audio_capture"] if _state["is_recording"]
                else _state["test_capture"] if _state["is_testing"]
                else None
            )
        if capture:
            capture.inject_mic_data(data)
    return ("", 204)


@app.route("/api/audio/test/start", methods=["POST"])
def start_audio_test():
    with _state_lock:
        if _state["is_recording"]:
            return jsonify({"error": "Cannot test while recording"}), 400
        if _state["is_testing"]:
            return jsonify({"error": "Already testing"}), 400

    body = request.get_json(silent=True) or {}
    loopback_device = body.get("loopback_device")
    mic_device      = body.get("mic_device")
    ffmpeg_mic_name = body.get("ffmpeg_mic_name")

    # A dummy queue - the mixer writes into it but nothing reads it.
    # We only care about the live loopback_level / mic_level attributes.
    test_queue: queue.Queue = queue.Queue(maxsize=100)
    capture = AudioCapture(test_queue)

    # Apply audio processing settings so the test reflects real behavior
    from capture_audio.params import resolve_audio_params
    _params = resolve_audio_params()
    capture.echo_cancel_enabled = bool(int(_params.get("echo_cancel_enabled", 0)))
    capture.agc_loopback_enabled = bool(int(_params.get("agc_loopback_enabled", 0)))
    capture.agc_mic_enabled = bool(int(_params.get("agc_mic_enabled", 0)))
    capture.agc_target_rms = float(_params.get("agc_target_rms", 0.15))
    capture.agc_max_gain = float(_params.get("agc_max_gain", 4.0))
    capture.agc_gate_threshold = float(_params.get("agc_gate_threshold", 0.01))

    try:
        capture.start(loopback_index=loopback_device, mic_index=mic_device,
                      ffmpeg_mic_name=ffmpeg_mic_name)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    with _state_lock:
        _state["test_capture"] = capture
        _state["is_testing"]   = True

    _push("audio_test_status", {"testing": True})
    return jsonify({"ok": True})


@app.route("/api/audio/test/stop", methods=["POST"])
def stop_audio_test():
    with _state_lock:
        if not _state["is_testing"]:
            return jsonify({"error": "Not testing"}), 400
        capture = _state["test_capture"]
        _state["test_capture"] = None
        _state["is_testing"]   = False

    def _cleanup() -> None:
        if capture:
            capture.stop()
        _push("audio_test_status", {"testing": False})

    threading.Thread(target=_cleanup, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/recording/start", methods=["POST"])
def start_recording():
    with _state_lock:
        if _state["is_recording"]:
            return jsonify({"error": "Already recording"}), 400
        can_record, reason = _recording_prereqs_locked()
        if not can_record:
            return jsonify({"error": reason}), 503
        # Stop any active audio test so it doesn't conflict with the real capture
        test_cap = _state["test_capture"]
        _state["test_capture"] = None
        _state["is_testing"]   = False

    if test_cap:
        # Stop synchronously: the test capture's ffmpeg-dshow process is still
        # holding the microphone, and DirectShow won't deliver audio to a
        # second simultaneous open. Backgrounding the stop lets the new
        # recording's ffmpeg launch while the old one is still tearing down,
        # which produces a silent mic stream for ~3 seconds (and sometimes
        # the entire session, if the race lands the wrong way).
        test_cap.stop()
        _push("audio_test_status", {"testing": False})

    # Wait for any in-flight cleanup from a previous stop to finish before
    # opening new audio streams.  This prevents the old capture / transcriber
    # from racing with the new one (e.g. _transcriber.stop() killing a freshly
    # started transcriber thread, or old mixer threads still writing to the
    # shared _audio_queue).
    if not _recording_cleanup_done.wait(timeout=15):
        log.warn("recording", "Previous cleanup did not finish in 15 s – starting anyway")

    # Drain stale audio from a previous session
    while not _audio_queue.empty():
        try:
            _audio_queue.get_nowait()
        except queue.Empty:
            break

    body = request.get_json(silent=True) or {}
    title             = body.get("title")
    loopback_device   = body.get("loopback_device")   # int | None
    mic_device        = body.get("mic_device")         # int | None | -1
    ffmpeg_mic_name   = body.get("ffmpeg_mic_name")    # str | None (for mic_device=-3)
    resume_session_id = body.get("resume_session_id")  # str | None
    auto_record       = bool(body.get("auto_record"))  # started by the call watcher

    # Fall back to saved user preferences when the caller didn't specify devices
    # (e.g. recording started from the home page which has no device selectors).
    if loopback_device is None or mic_device is None:
        _saved = settings.load()
        if loopback_device is None and _saved.get("loopback_device"):
            try:
                loopback_device = int(_saved["loopback_device"])
            except (ValueError, TypeError):
                pass
        if mic_device is None and _saved.get("mic_device"):
            _mic_pref = str(_saved["mic_device"])
            if auto_record and _mic_pref == "-2":
                # Browser mic only streams while a web page is open and sending
                # getUserMedia chunks; an auto-started recording may have no
                # page open, so let AudioCapture find a WASAPI mic instead.
                log.info("recording", "Auto-record: browser-mic preference bypassed - using WASAPI auto-detect.")
            elif _mic_pref.startswith("ffmpeg:"):
                mic_device = -3
                ffmpeg_mic_name = ffmpeg_mic_name or _mic_pref[7:]
            else:
                try:
                    mic_device = int(_mic_pref)
                except (ValueError, TypeError):
                    pass

    # ── Resume an existing session ──────────────────────────────────────────
    if resume_session_id:
        sess = storage.get_session(resume_session_id)
        if not sess:
            return jsonify({"error": "Session not found"}), 404
        session_id = resume_session_id
        storage.resume_session(session_id)
        existing_segments = [
            {"text": s["text"], "source": s["source"],
             "start_time": s["start_time"], "end_time": s["end_time"]}
            for s in sess.get("segments", [])
        ]
        existing_summary   = sess.get("summary", "")
        existing_chat      = [{"role": m["role"], "content": m["content"]}
                               for m in sess.get("chat_messages", [])]
        existing_labels    = {p["speaker_key"]: p["name"]
                               for p in sess.get("speaker_profiles", [])}
        existing_seg_count = len(existing_segments)
        # Determine next speaker label number so resumed diarizer doesn't
        # collide with existing speaker keys (e.g. "Speaker 1", "Speaker 2").
        all_speaker_keys = set(existing_labels.keys()) | {
            s["source"] for s in sess.get("segments", [])
        }
        max_label = 0
        for k in all_speaker_keys:
            parts = k.rsplit(" ", 1)
            if len(parts) == 2 and parts[0] == "Speaker":
                try:
                    max_label = max(max_label, int(parts[1]))
                except ValueError:
                    pass
        next_speaker_label = max_label + 1
    else:
        session_id         = storage.create_session(title)
        existing_segments  = []
        existing_summary   = ""
        existing_chat      = []
        existing_labels    = {}
        existing_seg_count = 0
        next_speaker_label = 1

    log.info("recording", f"Device selection: loopback={loopback_device}, "
             f"mic={mic_device}, ffmpeg_mic={ffmpeg_mic_name!r}")

    # Apply echo cancellation setting to each new capture instance
    from capture_audio.params import resolve_audio_params
    _ec_params = resolve_audio_params()

    def _new_capture() -> AudioCapture:
        cap = AudioCapture(_audio_queue)
        cap.echo_cancel_enabled  = bool(int(_ec_params.get("echo_cancel_enabled", 0)))
        cap.agc_loopback_enabled = bool(int(_ec_params.get("agc_loopback_enabled", 0)))
        cap.agc_mic_enabled      = bool(int(_ec_params.get("agc_mic_enabled", 0)))
        cap.agc_target_rms       = float(_ec_params.get("agc_target_rms", 0.15))
        cap.agc_max_gain         = float(_ec_params.get("agc_max_gain", 4.0))
        cap.agc_gate_threshold   = float(_ec_params.get("agc_gate_threshold", 0.01))
        return cap

    # Set up WAV recording - append to existing file on resume
    wav_dir = paths.audio_dir()
    wav_path = str(wav_dir / f"{session_id}.wav")

    # Saved device indices go stale whenever Windows re-enumerates audio
    # devices (docking, Bluetooth, driver updates), so retry with
    # auto-detection before failing. Without this an unattended
    # (auto-record) start dies on a stale settings value.
    attempts = [(loopback_device, mic_device)]
    if loopback_device is not None:
        attempts.append((None, mic_device))
    if isinstance(mic_device, int) and mic_device >= 0:
        attempts.append((None, None))
    attempts = list(dict.fromkeys(attempts))

    capture = None
    _start_err: Exception | None = None
    _used_lb = None
    for _lb, _mic in attempts:
        capture = _new_capture()
        capture.start_wav(wav_path, append=bool(resume_session_id))
        try:
            capture.start(
                loopback_index=_lb,
                mic_index=_mic,
                ffmpeg_mic_name=ffmpeg_mic_name,
            )
            _start_err = None
            _used_lb = _lb
            break
        except Exception as e:
            _start_err = e
            try:
                capture.stop_wav()
            except Exception:
                pass
            if (_lb, _mic) != attempts[-1]:
                log.warn("recording", f"Capture start failed ({e}) - retrying with auto-detected devices.")
    if _start_err is not None:
        if not resume_session_id:
            storage.end_session(session_id)
        return jsonify({"error": str(_start_err)}), 500

    # Self-heal a stale saved loopback index: if the configured device failed
    # but auto-detection (loopback_index=None) succeeded, clear the saved value
    # so future starts go straight to auto-detect instead of failing first.
    # Device indices shift whenever Windows re-enumerates audio hardware.
    if loopback_device is not None and _used_lb is None:
        if str(settings.get("loopback_device", "")) == str(loopback_device):
            settings.put("loopback_device", "")
            log.info("recording", f"Cleared stale saved loopback device {loopback_device} - "
                                  f"auto-detect will be used from now on.")

    _transcriber.start(capture.sample_rate, capture.channels,
                       next_speaker_label=next_speaker_label)

    now_mono = time.monotonic()
    with _state_lock:
        _state.update({
            "is_recording": True,
            "session_id": session_id,
            "segments": existing_segments,
            "summary": existing_summary,
            "chat_history": existing_chat,
            "pending_segments": 0,
            "summarized_seg_count": existing_seg_count,
            "audio_capture": capture,
            "speaker_labels": existing_labels,
            "speaker_audio_accum":    {},
            "speaker_emb_counts":     {},
            "fingerprint_dismissals": {},
            "fingerprint_suggestions": {},
            "_confirmed_speakers":    set(),
            "last_audio_activity_at": now_mono,
            "last_transcript_activity_at": now_mono,
            "quiet_prompt_sent_at": 0.0,
            "quiet_prompt_armed": True,
            "recording_started_at_monotonic": now_mono,
        })

    # ── Compute video offset for resumed sessions ────────────────────────
    # When resuming, the WAV writer opened in append mode knows the existing
    # sample count. Use it so video sync knows the audio offset.
    video_offset = 0.0
    if resume_session_id and capture.wav_writer:
        video_offset = capture.wav_writer.elapsed_seconds
    settings.put_video_offset(session_id, video_offset)

    # ── Screen recording (optional) ────────────────────────────────────────
    screen_recording_active = False
    all_params = resolve_audio_params()
    if int(all_params.get("screen_record_enabled", 0)) and find_ffmpeg():
        try:
            display_idx = int(settings.get("screen_display", 0))
            # Resolve H.264 preset name from numeric index
            h264_idx = int(all_params.get("screen_h264_preset", 2))
            h264_name = H264_PRESETS[min(h264_idx, len(H264_PRESETS) - 1)]
            framerate = int(all_params.get("screen_framerate", 10))
            crf = int(all_params.get("screen_crf", 32))
            scale_w = int(all_params.get("screen_scale_width", 0))
            scale = f"{scale_w}:-2" if scale_w > 0 else ""

            video_dir = paths.video_dir()
            video_path = str(video_dir / f"{session_id}.mp4")

            # When resuming, preserve the previous video as a numbered
            # part file so it isn't overwritten by the new recording.
            if resume_session_id:
                existing_video = Path(video_path)
                if existing_video.exists():
                    # Find the next available part number
                    part_num = 0
                    while (video_dir / f"{session_id}_part{part_num}.mp4").exists():
                        part_num += 1
                    part_path = video_dir / f"{session_id}_part{part_num}.mp4"
                    existing_video.rename(part_path)
                    log.info("screen", f"Preserved previous video as {part_path.name}")

            _screen_recorder.start(
                output_path=video_path,
                display_index=display_idx,
                framerate=framerate,
                crf=crf,
                preset=h264_name,
                scale=scale,
            )
            screen_recording_active = True
        except Exception as e:
            log.warn("screen", f"Could not start screen recording: {e}")

    verb = "Resumed" if resume_session_id else "Started"
    log.info("recording", f"{verb} - session {session_id}")
    _push_status({
        "recording": True,
        "session_id": session_id,
        "resumed": bool(resume_session_id),
        "screen_recording": screen_recording_active,
    })
    return jsonify({"session_id": session_id, "screen_recording": screen_recording_active})


def _concat_video_parts(session_id: str) -> None:
    """Concatenate video part files from pause/resume cycles into one MP4.

    Part files are named {session_id}_part0.mp4, _part1.mp4, etc. and are
    created when recording is resumed (the previous video is renamed to
    preserve it).  After recording stops, this function merges all parts
    plus the final recording into a single {session_id}.mp4 and cleans up.
    """
    video_dir = paths.video_dir()
    final_path = video_dir / f"{session_id}.mp4"

    # Collect part files in order
    parts: list[Path] = []
    i = 0
    while True:
        p = video_dir / f"{session_id}_part{i}.mp4"
        if p.exists():
            parts.append(p)
            i += 1
        else:
            break

    if not parts:
        return  # no resume happened, nothing to concat

    # The final recording (most recent) is the current {session_id}.mp4
    if final_path.exists():
        parts.append(final_path)

    if len(parts) < 2:
        return  # only one file total, rename back if needed

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        log.warn("screen", "Cannot concat video parts: ffmpeg not found")
        return

    log.info("screen", f"Concatenating {len(parts)} video parts for {session_id}...")

    # Build ffmpeg concat demuxer file list
    concat_list = video_dir / f"{session_id}_concat.txt"
    try:
        with open(concat_list, "w") as f:
            for p in parts:
                # ffmpeg concat demuxer needs forward slashes and escaped quotes
                safe = str(p.resolve()).replace("\\", "/").replace("'", "'\\''")
                f.write(f"file '{safe}'\n")

        merged_path = video_dir / f"{session_id}_merged.mp4"
        import subprocess
        result = subprocess.run(
            [ffmpeg, "-y", "-f", "concat", "-safe", "0",
             "-i", str(concat_list),
             "-c", "copy", "-movflags", "+faststart",
             str(merged_path)],
            capture_output=True, text=True, timeout=120,
        )

        if result.returncode == 0 and merged_path.exists():
            # Replace final with merged
            if final_path.exists():
                final_path.unlink()
            merged_path.rename(final_path)
            # Clean up part files
            for p in parts:
                if p.exists() and p != final_path:
                    p.unlink()
            # Video now starts at audio time 0 (full session coverage)
            settings.put_video_offset(session_id, 0.0)
            log.info("screen", f"Video concat complete: {len(parts)} parts merged")
        else:
            log.warn("screen", f"Video concat failed (rc={result.returncode}): "
                     f"{result.stderr[:200] if result.stderr else 'no stderr'}")
    except Exception as e:
        log.warn("screen", f"Video concat error: {e}")
    finally:
        if concat_list.exists():
            concat_list.unlink(missing_ok=True)


@app.route("/api/recording/stop", methods=["POST"])
def stop_recording():
    with _state_lock:
        if not _state["is_recording"]:
            return jsonify({"error": "Not recording"}), 400
        sid = _state["session_id"]
        capture: AudioCapture = _state["audio_capture"]
        # Snapshot transcript now - state may change before cleanup thread runs
        # plain_snapshot is used for title generation (no source labels needed)
        plain_snapshot = " ".join(s["text"] for s in _state["segments"])
        transcript_snapshot = _build_transcript(_state["segments"], _state["speaker_labels"])
        _state["is_recording"] = False
        _state["audio_capture"] = None
        _state["quiet_prompt_armed"] = True

    # Return immediately - cleanup blocks for up to 12 s (thread join) so we
    # must not do it on the Flask request handler thread or the server hangs.
    _recording_cleanup_done.clear()
    def _cleanup() -> None:
        try:
            if capture:
                capture.stop()   # joins threads then finalizes WAV
            _transcriber.stop()
            # Stop screen recording if active
            if _screen_recorder.is_recording:
                _screen_recorder.stop()
            # Concatenate video parts from pause/resume cycles
            if sid:
                _concat_video_parts(sid)
            if sid:
                storage.end_session(sid)
                seg_count = len(_state.get("segments", []))
                log.info("recording", f"Stopped - session {sid} ({seg_count} segments)")
            _push_status({"recording": False, "session_id": sid})
            # Auto-title: use full formatted transcript (with speaker labels) for better context.
            # Skip entirely if the user has manually renamed the session — their title wins.
            if sid and (transcript_snapshot or plain_snapshot).strip():
                if storage.is_title_user_set(sid):
                    log.info("recording", f"Skipping auto-title for {sid}: user-set title is locked")
                else:
                    ctx = storage.get_title_generation_context(sid)
                    title = ai.generate_title(
                        transcript_snapshot or plain_snapshot,
                        context=ctx,
                        system_prompt=settings.get("title_system_prompt") or None,
                    )
                    if title:
                        storage.update_session_title(sid, title, user_set=False)
                        _push("session_title", {"session_id": sid, "title": title})
            # Drop the finalized transcript into the Obsidian vault (after
            # title generation so the file carries the real title).
            if sid:
                _obsidian_export_session(sid)
        finally:
            _recording_cleanup_done.set()

    threading.Thread(target=_cleanup, daemon=True).start()
    # Update semantic embedding in background after session ends
    if sid:
        threading.Thread(target=update_session_embedding, args=(sid,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/recording/quiet-prompt/dismiss", methods=["POST"])
def dismiss_quiet_prompt():
    """Acknowledge the quiet recording reminder without stopping."""
    with _state_lock:
        if not _state["is_recording"]:
            return jsonify({"ok": True, "recording": False})
        _state["quiet_prompt_armed"] = False
        _state["quiet_prompt_sent_at"] = time.monotonic()
        sid = _state["session_id"]
    return jsonify({"ok": True, "session_id": sid})


@app.route("/api/summarize", methods=["POST"])
def summarize():
    """Manually trigger a full summary regeneration for the given session."""
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id")

    # Get full transcript for this session.
    # custom_prompt is taken from _state regardless of whether this session
    # is the active recording — the summary textarea always POSTs its value
    # via /api/custom-prompt for whichever session the user is viewing.
    with _state_lock:
        active_sid = _state["session_id"]
        custom_prompt = _state["custom_prompt"]
        if session_id == active_sid:
            segments = list(_state["segments"])
            labels = dict(_state["speaker_labels"])
            transcript = _build_transcript(segments, labels)
            seg_count = len(segments)
            meta = _build_session_meta(
                segments, labels,
                is_live=_state["is_recording"],
                custom_prompt=custom_prompt,
            )
        else:
            transcript = None
            seg_count = None
            meta = None

    if transcript is None:
        # Load from DB
        sess = storage.get_session(session_id)
        if not sess:
            return jsonify({"error": "Session not found"}), 404
        labels = sess.get("speaker_labels") or {}
        transcript = _build_transcript(sess["segments"], labels)
        seg_count = len(sess["segments"])
        meta = _build_session_meta(
            sess["segments"], labels,
            session_title=sess.get("title", ""),
            is_live=False,
            started_at=sess.get("started_at", ""),
            ended_at=sess.get("ended_at", ""),
            custom_prompt=custom_prompt,
        )

    # Signal any running auto-summary to discard its result, then regenerate from scratch.
    with _state_lock:
        _state["summary_manual_pending"] = True
    threading.Thread(
        target=_run_summary,
        args=(session_id, "", transcript, seg_count, custom_prompt, meta),
        kwargs={"is_auto": False, "clears_pending": True},
        daemon=True,
    ).start()
    return jsonify({"ok": True})


@app.route("/api/custom-prompt", methods=["GET", "POST"])
def custom_prompt_endpoint():
    """Get or set the custom summary prompt for the current session."""
    if request.method == "GET":
        with _state_lock:
            return jsonify({"custom_prompt": _state["custom_prompt"]})
    data = request.get_json(silent=True) or {}
    with _state_lock:
        _state["custom_prompt"] = data.get("custom_prompt", "")
    return jsonify({"ok": True})


@app.route("/api/settings/keys", methods=["GET"])
def get_keys():
    """Return masked key values and status."""
    return jsonify(config.get_key_status())


@app.route("/api/settings/keys", methods=["POST"])
def set_keys():
    """Save one or more API keys. Triggers side-effects (client reload, etc)."""
    data = request.get_json(silent=True) or {}
    changed = []

    for key_name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "HUGGING_FACE_KEY"):
        val = data.get(key_name)
        if val is not None:
            config.save_key(key_name, val)
            changed.append(key_name)

    # Reload AI clients whose keys changed
    if "OPENAI_API_KEY" in changed or "ANTHROPIC_API_KEY" in changed:
        ai.reload_client()
        ai._clients.clear()
        ai._clients[ai.provider] = ai.client

    # If HF key was just set and diarizer isn't loaded, start loading it
    if "HUGGING_FACE_KEY" in changed and data.get("HUGGING_FACE_KEY", "").strip():
        with _state_lock:
            need_diarizer = not _state["diarizer_ready"]
        if need_diarizer:
            threading.Thread(target=_load_diarizer, daemon=True).start()

    # Refresh tray icon if present
    _push_status()
    _refresh_tray()

    return jsonify({"ok": True, "keys": config.get_key_status()})


def _startup_lnk_path() -> Path:
    appdata = os.environ.get("APPDATA", "")
    return (
        Path(appdata) / "Microsoft" / "Windows"
        / "Start Menu" / "Programs" / "Startup"
        / "Meeting Assistant.lnk"
    )


@app.route("/api/settings/startup")
def get_startup():
    if sys.platform != "win32":
        return jsonify({"supported": False, "enabled": False})
    return jsonify({"supported": True, "enabled": _startup_lnk_path().exists()})


@app.route("/api/settings/startup", methods=["POST"])
def set_startup():
    if sys.platform != "win32":
        return jsonify({"ok": False, "error": "Not supported on this platform"})
    data = request.json or {}
    enable = bool(data.get("enabled", False))
    lnk = _startup_lnk_path()
    if enable:
        root = Path(__file__).parent
        vbs  = root / "launch_hidden.vbs"
        icon = root / "ui_web" / "static" / "images" / "logo.ico"
        # wscript + launch_hidden.vbs runs the server with no console window,
        # so login startup leaves nothing on the taskbar (tray icon only).
        ps = (
            f"$ws = New-Object -ComObject WScript.Shell; "
            f"$s = $ws.CreateShortcut('{lnk}'); "
            f"$s.TargetPath = 'wscript.exe'; "
            f"$s.Arguments = '\"\"{vbs}\"\"'; "
            f"$s.WorkingDirectory = '{root}'; "
            f"$s.WindowStyle = 7; "
            + (f"$s.IconLocation = '{icon}, 0'; " if icon.exists() else "")
            + "$s.Save()"
        )
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return jsonify({"ok": False, "error": "Failed to create startup shortcut"}), 500
    else:
        try:
            lnk.unlink()
        except FileNotFoundError:
            pass
    return jsonify({"ok": True, "enabled": lnk.exists()})


@app.route("/api/settings/status")
def settings_status():
    """Combined status for the settings page: keys, CUDA, setup state."""
    provider = settings.get("ai_provider", "openai")
    return jsonify({
        "needs_setup": config.needs_setup(provider),
        "cuda_available": get_cuda_available(),
        "keys": config.get_key_status(),
    })


# ── AI provider / model settings ──────────────────────────────────────────────

# Fallback model lists — used only when the provider's /models endpoint is
# unreachable (no key, offline, rate-limited). The auto-discovery below is the
# authoritative source; keep these minimal and reasonably current.
_AI_MODELS = {
    "anthropic": [
        {"id": "claude-opus-4-6",            "label": "Opus 4.6"},
        {"id": "claude-sonnet-4-6",          "label": "Sonnet 4.6"},
        {"id": "claude-haiku-4-5-20251001",  "label": "Haiku 4.5"},
    ],
    "openai": [
        {"id": "gpt-5.4",              "label": "GPT-5.4"},
        {"id": "gpt-5.3-chat-latest",  "label": "GPT-5.3 chat"},
        {"id": "gpt-4o",               "label": "GPT-4o"},
        {"id": "gpt-4o-mini",          "label": "GPT-4o mini"},
    ],
}

_DEFAULT_MODEL = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-5.3-chat-latest",
}

# OpenAI model filtering
_OPENAI_CHAT_PREFIXES = ("gpt-5", "gpt-4", "gpt-3.5-turbo", "o1", "o3", "o4", "o5",
                         "chatgpt-4o")
_OPENAI_EXCLUDE = (
    "realtime", "-audio-", "-transcribe", "-tts", "whisper", "dall-e",
    "embedding", "davinci", "babbage", "curie", "ada", "-search-",
    "instruct", "moderation", "-image-", "-preview-", "omni-moderation",
    "-pro",  # Pro tiers (e.g. gpt-5-pro) — too pricey for this app's use cases
)

# Models cache (keyed by provider) — keeps the UI snappy and avoids hammering
# provider APIs on every page load. Short TTL so a newly-released model shows
# up within ~half an hour without a manual refresh.
_AI_MODELS_CACHE: dict[str, dict] = {"anthropic": {}, "openai": {}}
_AI_MODELS_TTL_SEC = 30 * 60
_AI_MODELS_CACHE_LOCK = threading.Lock()


def _models_for_provider(provider: str, live_models: dict | None = None) -> list[dict]:
    """Return the configured model list for a provider.

    If ``live_models`` is supplied, it's the live, cached fetch result and is
    preferred over the static fallback. That way normalization and selection
    always see the freshest set of models.
    """
    if live_models and live_models.get(provider):
        return live_models[provider]
    return _AI_MODELS.get(provider, _AI_MODELS["openai"])


def _resolve_tool_ai(tool: str) -> tuple[str | None, str | None]:
    """Return (provider, model) overrides for a tool, or (None, None) if unset."""
    p = settings.get(f"{tool}_provider")
    m = settings.get(f"{tool}_model")
    return (p, m)


def _normalize_ai_selection(
    provider: str,
    model: str | None,
    live_models: dict | None = None,
) -> tuple[str, str]:
    """Ensure provider/model are valid and aligned with each other.

    When ``live_models`` is provided, the model must be in the live fetched
    list for that provider. This keeps auto-upgrades clean: if a stored model
    id no longer exists (because its moving alias was replaced or deprecated),
    we fall back to the provider's declared default.
    """
    provider = provider if provider in _AI_MODELS else "openai"
    models = _models_for_provider(provider, live_models)
    valid_ids = {m["id"] for m in models}
    if model in valid_ids:
        return provider, model
    # Auto-upgrade within the same class for Anthropic — preserves the user's
    # intent across version bumps. If they had ``claude-opus-4-6`` saved and
    # the live list now only contains ``claude-opus-4-7``, we hand them 4-7
    # instead of silently falling through to Sonnet (the default).
    if provider == "anthropic" and model:
        prev_match = _ANTHROPIC_CLASS_RE.match(model)
        if prev_match:
            prev_cls = prev_match.group(1)
            for candidate in models:
                cm = _ANTHROPIC_CLASS_RE.match(candidate["id"])
                if cm and cm.group(1) == prev_cls:
                    return provider, candidate["id"]
    # Prefer declared default if it's still valid; otherwise pick the first
    # (most-capable / newest-first) entry from the provider's model list.
    fallback = _DEFAULT_MODEL.get(provider)
    if fallback in valid_ids:
        return provider, fallback
    if valid_ids:
        return provider, models[0]["id"]
    return provider, ""


# ── Anthropic auto-discovery ──────────────────────────────────────────────────
# Anthropic's model ids are systematically structured:
#   claude-<class>-<version>[-<date>]   e.g. claude-opus-4-6-20260101
# We parse class (opus / sonnet / haiku / …), version (tuple like (4, 6)), and
# date suffix so we can pick the latest version of each class automatically.
# When "claude-opus-4-7" is released it cleanly replaces 4-6 in the opus slot.

_ANTHROPIC_CLASS_RE = re.compile(
    # Class group is letters only ("opus"/"sonnet"/"haiku"); the version group
    # is lazy so a trailing 8-digit date like -20251001 gets captured by the
    # dedicated date group instead of being swallowed as part of the version.
    r"^claude-([a-z]+)-(\d+(?:-\d+)*?)(?:-(\d{8}))?(?:-latest)?$"
)
_ANTHROPIC_CLASS_ORDER = ["opus", "sonnet", "haiku"]  # display order

def _anthropic_label_from_id(mid: str, fallback: str = "") -> str:
    """Build a clean picker label like "Opus 4.7" from an Anthropic model id.

    The Anthropic API's ``display_name`` strips the minor version (returning
    just "Claude Opus 4" for ``claude-opus-4-7``), which is ambiguous when
    multiple minor revisions exist — we parse the id ourselves instead.
    Falls back to ``fallback`` (or the id) if the regex doesn't match.
    """
    match = _ANTHROPIC_CLASS_RE.match(mid)
    if not match:
        return fallback or mid
    cls = match.group(1).replace("-", " ").title()   # "opus" → "Opus"
    ver = match.group(2).replace("-", ".")           # "4-7"  → "4.7"
    return f"{cls} {ver}"


def _anthropic_latest_only(models: list[dict]) -> list[dict]:
    """Keep only the latest version per Anthropic class (opus/sonnet/haiku).

    Also rewrites each surviving row's ``label`` to the clean ``Opus 4.7``
    format derived from its id, regardless of what the API returned.
    """
    best: dict[str, dict] = {}
    for m in models:
        mid = m.get("id", "")
        match = _ANTHROPIC_CLASS_RE.match(mid)
        if not match:
            continue
        cls = match.group(1)
        ver = tuple(int(x) for x in match.group(2).split("-"))
        date = match.group(3) or ""
        # Prefer versioned aliases ("claude-opus-4-7") over dated snapshots
        # ("claude-opus-4-7-20260101") so the picker stores the moving alias
        # rather than a pinned snapshot — that's what makes auto-upgrade work.
        alias_preference = 0 if date else 1
        key = (ver, alias_preference, date)
        prev = best.get(cls)
        if prev is None or key > prev["_sort"]:
            best[cls] = {**m, "_sort": key}
    ordered = [best[c] for c in _ANTHROPIC_CLASS_ORDER if c in best]
    for cls in sorted(best.keys()):
        if cls not in _ANTHROPIC_CLASS_ORDER:
            ordered.append(best[cls])
    for m in ordered:
        m.pop("_sort", None)
        m["label"] = _anthropic_label_from_id(m.get("id", ""), m.get("label", ""))
    return ordered or models


def _fetch_anthropic_models() -> list[dict]:
    """Fetch Claude models from the Anthropic API. Falls back to static list."""
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return list(_AI_MODELS["anthropic"])
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        page = client.models.list()
        result = [
            {"id": m.id, "label": getattr(m, "display_name", None) or m.id}
            for m in page.data
        ]
        return _anthropic_latest_only(result) if result else list(_AI_MODELS["anthropic"])
    except Exception as e:
        log.warn("ai", f"Failed to fetch Anthropic models: {e}")
        return list(_AI_MODELS["anthropic"])


# ── OpenAI auto-discovery ─────────────────────────────────────────────────────
# OpenAI's naming is NOT monotonically versioned (e.g. gpt-5.4 may be a
# reasoning model distinct from the chat-tuned gpt-5.3), so we do NOT collapse
# to "latest per family" the way Anthropic does. Instead we expose every
# chat-capable text model from /models — the user picks what they want. New
# releases show up automatically in the picker without any code changes.

def _prettify_openai_label(mid: str) -> str:
    """Turn a raw OpenAI model id into a human-friendly picker label.

    Examples:
      gpt-5.4                  → "GPT-5.4"
      gpt-5.3-chat-latest      → "GPT-5.3 chat (latest)"
      gpt-5-mini               → "GPT-5 mini"
      gpt-4o                   → "GPT-4o"
      gpt-4o-mini              → "GPT-4o mini"
      chatgpt-4o-latest        → "ChatGPT-4o (latest)"
      o3                       → "o3 reasoning"
      o4-mini                  → "o4 mini reasoning"
      gpt-5.3-2026-02-15       → "GPT-5.3 (2026-02-15)"
    """
    s = mid
    # Split off YYYY-MM-DD date stamp
    date_suffix = ""
    date_match = re.search(r"-(20\d{2}-\d{2}-\d{2})$", s)
    if date_match:
        date_suffix = f" ({date_match.group(1)})"
        s = s[: date_match.start()]

    # Specific tails we want to format nicely
    latest = ""
    if s.endswith("-latest"):
        latest = " (latest)"
        s = s[: -len("-latest")]

    # Tier suffixes we surface inline
    tier = ""
    for t in ("-mini", "-nano", "-turbo", "-pro", "-chat", "-preview"):
        if s.endswith(t):
            tier = " " + t[1:]  # drop the leading hyphen
            s = s[: -len(t)]
            break

    # Base family: GPT-{ver}, ChatGPT-{...}, o-series, etc.
    base = s
    if s.startswith("gpt-"):
        base = "GPT-" + s[len("gpt-"):]
    elif s.startswith("chatgpt-"):
        base = "ChatGPT-" + s[len("chatgpt-"):]
    elif re.match(r"^o\d+$", s):
        # o1 / o3 / o4 — brand this as "reasoning" only at the tail so the
        # user can tell them apart from chat models at a glance.
        return f"{s}{tier if tier else ''} reasoning{date_suffix}"

    return f"{base}{tier}{latest}{date_suffix}"


_OPENAI_DATE_SUFFIX_RE = re.compile(r"-20\d{2}-\d{2}-\d{2}$")

def _collapse_openai_snapshots(entries: list[dict]) -> list[dict]:
    """Drop dated OpenAI snapshots when an undated alias exists.

    OpenAI publishes both moving aliases (``gpt-5.4-mini``) and pinned
    snapshots (``gpt-5.4-mini-2026-03-17``) — each referring to the same
    family. The alias auto-upgrades; the snapshot is frozen. We show only the
    alias when both are present, which is what "always the latest" means
    within a given family. If a family happens to exist ONLY as a snapshot
    (no alias), we keep the most recent snapshot so the family isn't lost.
    """
    by_base: dict[str, list[dict]] = {}
    order: list[str] = []
    for e in entries:
        base = _OPENAI_DATE_SUFFIX_RE.sub("", e.get("id", ""))
        if base not in by_base:
            order.append(base)
            by_base[base] = []
        by_base[base].append(e)
    kept: list[dict] = []
    for base in order:
        family = by_base[base]
        # If the undated alias ("base" itself) exists in the family, use it.
        alias = next((m for m in family if m.get("id") == base), None)
        if alias:
            kept.append(alias)
        else:
            # No moving pointer — keep the newest dated snapshot so the
            # family still appears, but rewrite its label to the clean
            # base-id form so the picker never shows a "(YYYY-MM-DD)" tag.
            kept.append(family[0])
    # Uniformly rewrite labels from the date-stripped base id so the picker
    # looks the same whether an entry is an alias or the newest snapshot.
    for e in kept:
        base = _OPENAI_DATE_SUFFIX_RE.sub("", e.get("id", ""))
        e["label"] = _prettify_openai_label(base)
    return kept


def _fetch_openai_models() -> list[dict]:
    """Fetch chat-capable models from the OpenAI API. Falls back to static list."""
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        return list(_AI_MODELS["openai"])
    try:
        from openai import OpenAI
        client = OpenAI(api_key=key)
        all_models = list(client.models.list())

        def _is_chat(mid: str) -> bool:
            m = mid.lower()
            if any(exc in m for exc in _OPENAI_EXCLUDE):
                return False
            return any(m.startswith(p) for p in _OPENAI_CHAT_PREFIXES)

        filtered = [m for m in all_models if _is_chat(m.id)]
        # Sort newest-first so within each family the aliased (undated) id is
        # encountered first where it exists, and so snapshot-only families
        # come out with their most recent dated release at index 0.
        filtered.sort(key=lambda m: (-m.created, m.id))
        staged = [
            {"id": m.id, "label": _prettify_openai_label(m.id)}
            for m in filtered
        ]
        collapsed = _collapse_openai_snapshots(staged)
        return collapsed or list(_AI_MODELS["openai"])
    except Exception as e:
        log.warn("ai", f"Failed to fetch OpenAI models: {e}")
        return list(_AI_MODELS["openai"])


# ── Cached lookup with TTL + parallel prefetch ───────────────────────────────

_AI_MODELS_FETCHERS = {
    "anthropic": _fetch_anthropic_models,
    "openai":    _fetch_openai_models,
}

def _get_models_cached(provider: str, *, force_refresh: bool = False) -> list[dict]:
    """Return the model list for a provider, re-fetching if stale.

    Thread-safe: workers racing to refresh a provider's list will coalesce on
    a single lock (so we never fire two /models requests at once for the same
    provider).
    """
    fetcher = _AI_MODELS_FETCHERS.get(provider, _fetch_openai_models)
    now = time.time()
    with _AI_MODELS_CACHE_LOCK:
        entry = _AI_MODELS_CACHE.get(provider) or {}
        cached = entry.get("data")
        expires = entry.get("expires", 0)
        if not force_refresh and cached and expires > now:
            return cached
    # Fetch outside the lock — it's a network call and may take a second.
    fresh = fetcher()
    with _AI_MODELS_CACHE_LOCK:
        _AI_MODELS_CACHE[provider] = {
            "data": fresh,
            "expires": time.time() + _AI_MODELS_TTL_SEC,
        }
    return fresh


def _get_all_models_live(*, force_refresh: bool = False) -> dict[str, list[dict]]:
    """Prefetch both providers' model lists in parallel and return as a dict
    suitable for the ``models`` field of /api/ai_settings responses."""
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="models-fetch") as ex:
        futs = {
            p: ex.submit(_get_models_cached, p, force_refresh=force_refresh)
            for p in _AI_MODELS_FETCHERS
        }
        out = {}
        for p, fut in futs.items():
            try:
                out[p] = fut.result(timeout=8)
            except Exception as e:
                log.warn("ai", f"live model fetch for {p} timed out: {e}")
                out[p] = list(_AI_MODELS.get(p, []))
    return out


@app.route("/api/ai_settings/models")
def get_ai_settings_models():
    """Return available models for a provider, fetched live (cached)."""
    provider = request.args.get("provider", ai.provider)
    force = request.args.get("refresh", "").lower() in ("1", "true", "yes")
    models = _get_models_cached(provider, force_refresh=force)
    return jsonify({"provider": provider, "models": models})


@app.route("/api/ai_settings/models/refresh", methods=["POST"])
def refresh_ai_models():
    """Drop caches and re-fetch both providers' model lists."""
    models = _get_all_models_live(force_refresh=True)
    return jsonify({"ok": True, "models": models})


@app.route("/api/ai_settings", methods=["GET"])
def get_ai_settings():
    """Return current AI provider, model, per-tool overrides, and available options.

    The ``models`` dict reflects the live, cached /models listing from each
    provider — so a freshly-released Claude Opus 4.7 appears automatically
    within one cache window (~30 min) or immediately after hitting the
    "Refresh models" action.
    """
    live_models = _get_all_models_live()
    provider, model = _normalize_ai_selection(ai.provider, ai.model, live_models)
    return jsonify({
        "provider": provider,
        "model": model,
        "models": live_models,
        "summary_provider": settings.get("summary_provider"),
        "summary_model": settings.get("summary_model"),
        "chat_provider": settings.get("chat_provider"),
        "chat_model": settings.get("chat_model"),
        "global_chat_provider": settings.get("global_chat_provider"),
        "global_chat_model": settings.get("global_chat_model"),
    })


@app.route("/api/ai_settings", methods=["POST"])
def set_ai_settings():
    """Update AI provider and/or model. Reloads the client immediately.

    Accepts optional ``tool`` key ("summary" or "chat") to set per-tool
    overrides instead of changing the primary provider/model.
    """
    data = request.get_json(silent=True) or {}
    tool = data.get("tool")

    if tool in ("summary", "chat", "global_chat"):
        tp = data.get("provider")
        tm = data.get("model")
        updates = {}
        if "provider" in data:
            updates[f"{tool}_provider"] = tp
        if "model" in data:
            updates[f"{tool}_model"] = tm
        if updates:
            settings.update(updates)
        return jsonify({
            "ok": True,
            "tool": tool,
            "summary_provider": settings.get("summary_provider"),
            "summary_model": settings.get("summary_model"),
            "chat_provider": settings.get("chat_provider"),
            "chat_model": settings.get("chat_model"),
            "global_chat_provider": settings.get("global_chat_provider"),
            "global_chat_model": settings.get("global_chat_model"),
            "provider": ai.provider,
            "model": ai.model,
        })

    new_provider = data.get("provider")
    new_model = data.get("model")
    target_provider = new_provider or ai.provider
    target_model = new_model if new_model is not None else ai.model
    target_provider, target_model = _normalize_ai_selection(target_provider, target_model)

    updates = {}
    if target_provider != ai.provider:
        updates["ai_provider"] = target_provider
    if target_model != ai.model:
        updates["ai_model"] = target_model

    # Clear per-tool overrides when a global model is explicitly set from
    # the Settings page — the global pick should beat any stale override.
    for k in ("summary_provider", "summary_model",
             "chat_provider", "chat_model",
             "global_chat_provider", "global_chat_model"):
        if settings.get(k) is not None:
            updates[k] = None

    if updates:
        settings.update(updates)
        if "ai_provider" in updates or "ai_model" in updates:
            ai.reload_client(
                provider=target_provider,
                model=target_model,
            )

    return jsonify({"ok": True, "provider": ai.provider, "model": ai.model})


@app.route("/api/preferences", methods=["GET"])
def get_preferences():
    """Return all saved user preferences."""
    return jsonify(settings.load())


@app.route("/api/preferences", methods=["PUT"])
def set_preferences():
    """Update one or more user preferences."""
    data = request.get_json(silent=True) or {}
    updated = settings.update(data)
    return jsonify(updated)


# ── Data folder relocation ───────────────────────────────────────────────────

@app.route("/api/data_folder", methods=["GET"])
def get_data_folder():
    """Return the active data folder path and whether it's user-overridden."""
    return jsonify({
        "current": str(paths.data_dir()),
        "default": str(paths.default_dir()),
        "overridden": paths.is_overridden(),
    })


@app.route("/api/data_folder/pick", methods=["POST"])
def pick_data_folder():
    """Show a native folder picker and return the selected path.

    Does not migrate — caller must POST to /api/data_folder/migrate to commit.
    """
    data = request.get_json(silent=True) or {}
    initial = data.get("initial") or str(paths.data_dir())
    selected = paths.pick_folder(initial_dir=initial)
    return jsonify({"selected": selected})


@app.route("/api/data_folder/migrate", methods=["POST"])
def migrate_data_folder():
    """Copy the current data folder to a new location and switch over.

    Refuses if a recording is in progress (would risk losing in-flight WAV
    writes) or if any reanalysis / batch jobs are running. After a successful
    migration, the response includes ``restart_required: True`` — the caller
    should prompt the user to restart so module-level caches re-read.
    """
    data = request.get_json(silent=True) or {}
    dst = (data.get("destination") or "").strip()
    if not dst:
        return jsonify({"error": "destination required"}), 400

    # Refuse mid-recording — moving WAVs/DBs while writers are open would
    # corrupt them. Caller can stop recording and try again.
    with _state_lock:
        if _state.get("is_recording"):
            return jsonify({
                "error": "A recording is in progress. Stop recording first.",
            }), 409
        if _state.get("reanalyzing"):
            return jsonify({
                "error": "A reanalysis is in progress. Wait for it to finish.",
            }), 409

    try:
        result = paths.migrate(dst=Path(dst))
    except paths.MigrationError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        log.error("data_folder", f"Unexpected migration error: {e}")
        return jsonify({"error": f"Unexpected error: {e}"}), 500

    log.info(
        "data_folder",
        f"Migrated data folder → {result['dst']} "
        f"({result['files_copied']} files, {result['dbs_copied']} DBs, "
        f"{result['bytes_copied'] / 1024 / 1024:.1f} MB)",
    )
    return jsonify({
        "ok": True,
        "restart_required": True,
        **result,
    })


@app.route("/api/data_folder/reset", methods=["POST"])
def reset_data_folder():
    """Forget the user override and revert to the default location.

    Does NOT move data — caller is responsible for migrating the contents
    back to the default folder first if they want it there.
    """
    paths.reset_to_default()
    return jsonify({
        "ok": True,
        "current": str(paths.data_dir()),
        "restart_required": True,
    })


@app.route("/api/audio_params", methods=["GET"])
def get_audio_params():
    """Return current audio parameter values, defaults, and metadata."""
    from capture_audio.params import (
        TRANSCRIPTION_DEFAULTS, DIARIZATION_DEFAULTS,
        AUTO_GAIN_DEFAULTS, ECHO_CANCELLATION_DEFAULTS, SCREEN_RECORDING_DEFAULTS,
        resolve_audio_params,
    )
    return jsonify({
        "current": resolve_audio_params(settings.load()),
        "transcription": TRANSCRIPTION_DEFAULTS,
        "diarization": DIARIZATION_DEFAULTS,
        "auto_gain": AUTO_GAIN_DEFAULTS,
        "echo_cancellation": ECHO_CANCELLATION_DEFAULTS,
        "screen_recording": SCREEN_RECORDING_DEFAULTS,
    })


@app.route("/api/audio_params", methods=["PUT"])
def set_audio_params():
    """Update one or more audio parameters.

    If any key being set is controlled by a non-custom preset, that
    section's preset is auto-flipped to ``"custom"`` and the *currently
    effective* values for the rest of the section are snapshotted into
    audio_params first, so untouched params keep their preset values
    while the user's edit lands on top.
    """
    from capture_audio.params import (
        get_all_defaults, resolve_audio_params, preset_keys,
        _screen_preset_overrides,
    )
    data = request.get_json(silent=True) or {}
    all_settings = settings.load()
    params = all_settings.get("audio_params", {})
    defaults = get_all_defaults()

    edited_keys = {k for k in data if k in defaults}
    screen_keys = set(_screen_preset_overrides(SCREEN_DEFAULT_PRESET).keys())

    # Determine which section presets need to flip to custom.
    flips: list[tuple[str, set]] = []
    t_preset = all_settings.get("transcription_preset", TRANSCRIPTION_DEFAULT_PRESET)
    if t_preset != "custom" and edited_keys & preset_keys(TRANSCRIPTION_PRESETS):
        flips.append(("transcription_preset", preset_keys(TRANSCRIPTION_PRESETS)))
    d_preset = all_settings.get("diarization_preset", DIARIZATION_DEFAULT_PRESET)
    if d_preset != "custom" and edited_keys & preset_keys(DIARIZATION_PRESETS):
        flips.append(("diarization_preset", preset_keys(DIARIZATION_PRESETS)))
    s_preset = all_settings.get("screen_preset", SCREEN_DEFAULT_PRESET)
    if s_preset != "custom" and edited_keys & screen_keys:
        flips.append(("screen_preset", screen_keys))

    # Snapshot effective values into audio_params for the sections we're
    # flipping, BEFORE applying the user's edit, so untouched keys retain
    # their preset values.
    if flips:
        effective = resolve_audio_params(all_settings)
        for preset_setting_key, section_keys in flips:
            for k in section_keys:
                if k in effective:
                    params[k] = effective[k]
            settings.put(preset_setting_key, "custom")

    # Apply the user's edits last — they always win over the snapshot.
    for key in edited_keys:
        params[key] = data[key]
    settings.put("audio_params", params)

    current = resolve_audio_params()
    _apply_audio_params(current)
    return jsonify({
        "ok": True,
        "audio_params": current,
        "transcription_preset": settings.get("transcription_preset", TRANSCRIPTION_DEFAULT_PRESET),
        "diarization_preset": settings.get("diarization_preset", DIARIZATION_DEFAULT_PRESET),
        "screen_preset": settings.get("screen_preset", SCREEN_DEFAULT_PRESET),
    })


@app.route("/api/audio_params/reset", methods=["POST"])
def reset_audio_param():
    """Reset one or all audio parameters to defaults."""
    from capture_audio.params import resolve_audio_params
    data = request.get_json(silent=True) or {}
    key = data.get("key")
    all_settings = settings.load()
    params = all_settings.get("audio_params", {})
    if key:
        params.pop(key, None)
    else:
        params = {}
    settings.put("audio_params", params)
    current = resolve_audio_params()
    _apply_audio_params(current)
    return jsonify({"ok": True, "audio_params": current})


@app.route("/api/audio_params/reset_section", methods=["POST"])
def reset_audio_section():
    """Reset all parameters in a specific section to defaults."""
    from capture_audio.params import resolve_audio_params
    section_map = {
        "transcription": TRANSCRIPTION_DEFAULTS,
        "diarization": DIARIZATION_DEFAULTS,
        "auto_gain": AUTO_GAIN_DEFAULTS,
        "screen_recording": SCREEN_RECORDING_DEFAULTS,
    }
    data = request.get_json(silent=True) or {}
    section = data.get("section")
    if section not in section_map:
        return jsonify({"error": "Invalid section"}), 400

    section_keys = set(section_map[section].keys())
    all_settings = settings.load()
    params = all_settings.get("audio_params", {})
    for k in section_keys:
        params.pop(k, None)
    settings.put("audio_params", params)

    # Reset preset selection to default for this section
    preset_defaults = {
        "transcription": ("transcription_preset", TRANSCRIPTION_DEFAULT_PRESET),
        "diarization": ("diarization_preset", DIARIZATION_DEFAULT_PRESET),
        "screen_recording": ("screen_preset", SCREEN_DEFAULT_PRESET),
    }
    if section in preset_defaults:
        pkey, pval = preset_defaults[section]
        settings.put(pkey, pval)

    current = resolve_audio_params()
    _apply_audio_params(current)
    return jsonify({"ok": True, "audio_params": current})


# ── Reanalysis parameter endpoints ───────────────────────────────────────────

@app.route("/api/reanalysis_params", methods=["GET"])
def get_reanalysis_params():
    """Return current reanalysis parameter values, defaults, and metadata."""
    from capture_audio.params import REANALYSIS_DEFAULTS, get_reanalysis_defaults
    saved = settings.load().get("reanalysis_params", {})
    defaults = get_reanalysis_defaults()
    current = {**defaults, **saved}
    return jsonify({
        "current": current,
        "reanalysis": REANALYSIS_DEFAULTS,
    })


@app.route("/api/reanalysis_params", methods=["PUT"])
def set_reanalysis_params():
    """Update one or more reanalysis parameters."""
    from capture_audio.params import get_reanalysis_defaults
    data = request.get_json(silent=True) or {}
    all_settings = settings.load()
    params = all_settings.get("reanalysis_params", {})
    defaults = get_reanalysis_defaults()
    for key, val in data.items():
        if key in defaults:
            params[key] = val
    settings.put("reanalysis_params", params)
    return jsonify({"ok": True, "reanalysis_params": {**defaults, **params}})


@app.route("/api/reanalysis_params/reset", methods=["POST"])
def reset_reanalysis_param():
    """Reset one or all reanalysis parameters to defaults."""
    from capture_audio.params import get_reanalysis_defaults
    data = request.get_json(silent=True) or {}
    key = data.get("key")
    all_settings = settings.load()
    params = all_settings.get("reanalysis_params", {})
    if key:
        params.pop(key, None)
    else:
        params = {}
    settings.put("reanalysis_params", params)
    defaults = get_reanalysis_defaults()
    current = {**defaults, **params}
    return jsonify({"ok": True, "reanalysis_params": current})


@app.route("/api/transcription/presets", methods=["GET"])
def get_transcription_presets():
    """Return transcription preset definitions."""
    return jsonify({
        "presets": TRANSCRIPTION_PRESETS,
        "default": TRANSCRIPTION_DEFAULT_PRESET,
        "selected": settings.get("transcription_preset", TRANSCRIPTION_DEFAULT_PRESET),
    })


@app.route("/api/transcription/presets", methods=["PUT"])
def set_transcription_preset():
    """Switch the active transcription preset.

    Non-custom presets are stored by *name only* — effective values come
    from the preset definitions at read time, so source-code updates to
    the preset propagate automatically. When switching to ``"custom"`` we
    snapshot the currently effective values for the transcription keys
    into audio_params so the user can edit from where they were.
    """
    from capture_audio.params import resolve_audio_params, preset_keys
    data = request.get_json(silent=True) or {}
    preset_id = data.get("preset", TRANSCRIPTION_DEFAULT_PRESET)
    if preset_id not in TRANSCRIPTION_PRESETS:
        return jsonify({"error": "invalid preset"}), 400

    if preset_id == "custom":
        all_settings = settings.load()
        effective = resolve_audio_params(all_settings)
        params = all_settings.get("audio_params", {})
        for k in preset_keys(TRANSCRIPTION_PRESETS):
            if k in effective:
                params[k] = effective[k]
        settings.put("audio_params", params)

    settings.put("transcription_preset", preset_id)
    current = resolve_audio_params()
    _apply_audio_params(current)
    return jsonify({"ok": True, "preset": preset_id, "audio_params": current})


@app.route("/api/diarization/presets", methods=["GET"])
def get_diarization_presets():
    """Return diarization preset definitions."""
    return jsonify({
        "presets": DIARIZATION_PRESETS,
        "default": DIARIZATION_DEFAULT_PRESET,
        "selected": settings.get("diarization_preset", DIARIZATION_DEFAULT_PRESET),
    })


@app.route("/api/diarization/presets", methods=["PUT"])
def set_diarization_preset():
    """Switch the active diarization preset. See ``set_transcription_preset``
    for the non-custom-by-name / snapshot-on-custom semantics."""
    from capture_audio.params import resolve_audio_params, preset_keys
    data = request.get_json(silent=True) or {}
    preset_id = data.get("preset", DIARIZATION_DEFAULT_PRESET)
    if preset_id not in DIARIZATION_PRESETS:
        return jsonify({"error": "invalid preset"}), 400

    if preset_id == "custom":
        all_settings = settings.load()
        effective = resolve_audio_params(all_settings)
        params = all_settings.get("audio_params", {})
        for k in preset_keys(DIARIZATION_PRESETS):
            if k in effective:
                params[k] = effective[k]
        settings.put("audio_params", params)

    settings.put("diarization_preset", preset_id)
    current = resolve_audio_params()
    _apply_audio_params(current)
    return jsonify({"ok": True, "preset": preset_id, "audio_params": current})


def _apply_audio_params(params: dict) -> None:
    """Push audio parameter values to the running transcriber and audio capture."""
    _transcriber.silence_threshold = float(params.get("silence_threshold", 0.025))
    _transcriber.silence_duration  = float(params.get("silence_duration", 0.3))
    _transcriber.min_buffer_seconds = float(params.get("min_buffer_seconds", 0.5))
    _transcriber.max_buffer_seconds = float(params.get("max_buffer_seconds", 10.0))
    _transcriber.beam_size         = int(params.get("beam_size", 2))
    _transcriber.prompt_chars      = int(params.get("prompt_chars", 800))
    _transcriber.vad_min_silence_ms = int(params.get("vad_min_silence_ms", 300))
    _transcriber.vad_speech_pad_ms  = int(params.get("vad_speech_pad_ms", 150))
    _transcriber.compression_ratio_threshold = float(
        params.get("compression_ratio_threshold", 2.0)
    )
    if _transcriber.diarizer is not None:
        _transcriber.diarizer.apply_params(params)

    # Push echo cancellation and AGC toggles to the active AudioCapture instance
    with _state_lock:
        capture = _state.get("audio_capture")
    if capture is not None:
        capture.echo_cancel_enabled = bool(int(params.get("echo_cancel_enabled", 0)))
        capture.agc_loopback_enabled = bool(int(params.get("agc_loopback_enabled", 0)))
        capture.agc_mic_enabled = bool(int(params.get("agc_mic_enabled", 0)))
        capture.agc_target_rms = float(params.get("agc_target_rms", 0.15))
        capture.agc_max_gain = float(params.get("agc_max_gain", 4.0))
        capture.agc_gate_threshold = float(params.get("agc_gate_threshold", 0.01))


@app.route("/api/screen/displays", methods=["GET"])
def get_displays():
    """Return available displays for screen recording."""
    displays = enumerate_displays()
    selected = int(settings.get("screen_display", 0))
    # Clamp to valid range in case displays changed since the setting was saved
    if selected >= len(displays):
        selected = 0
        settings.put("screen_display", 0)
    return jsonify({
        "displays": displays,
        "selected": selected,
        "ffmpeg_available": find_ffmpeg() is not None,
    })


@app.route("/api/screen/displays", methods=["PUT"])
def set_display():
    """Set the selected display for screen recording."""
    data = request.get_json(silent=True) or {}
    idx = data.get("display", 0)
    settings.put("screen_display", int(idx))
    return jsonify({"ok": True, "selected": int(idx)})


@app.route("/api/screen/identify", methods=["POST"])
def identify_display():
    """Flash a border around the given display."""
    data = request.get_json(silent=True) or {}
    idx = int(data.get("display", 0))
    flash_display_border(idx)
    return jsonify({"ok": True})


@app.route("/api/screen/presets", methods=["GET"])
def get_screen_presets():
    """Return screen recording preset definitions."""
    return jsonify({
        "presets": SCREEN_PRESETS,
        "default": SCREEN_DEFAULT_PRESET,
        "h264_presets": H264_PRESETS,
        "selected": settings.get("screen_preset", SCREEN_DEFAULT_PRESET),
    })


@app.route("/api/screen/presets", methods=["PUT"])
def set_screen_preset():
    """Switch the active screen recording preset. See
    ``set_transcription_preset`` for the non-custom-by-name /
    snapshot-on-custom semantics."""
    from capture_audio.params import resolve_audio_params, _screen_preset_overrides
    data = request.get_json(silent=True) or {}
    preset_id = data.get("preset", SCREEN_DEFAULT_PRESET)
    if preset_id not in SCREEN_PRESETS:
        return jsonify({"error": "invalid preset"}), 400

    if preset_id == "custom":
        all_settings = settings.load()
        effective = resolve_audio_params(all_settings)
        params = all_settings.get("audio_params", {})
        for k in _screen_preset_overrides(SCREEN_DEFAULT_PRESET).keys():
            if k in effective:
                params[k] = effective[k]
        settings.put("audio_params", params)

    settings.put("screen_preset", preset_id)
    current = resolve_audio_params()
    return jsonify({"ok": True, "preset": preset_id, "audio_params": current})


@app.route("/api/screen/status", methods=["GET"])
def screen_status():
    """Return current screen recording state."""
    return jsonify({
        "recording": _screen_recorder.is_recording,
        "ffmpeg_available": find_ffmpeg() is not None,
    })


@app.route("/api/screen/preview", methods=["GET"])
def screen_preview():
    """Capture a live screenshot from the selected display as JPEG."""
    display_idx = int(settings.get("screen_display", 0))
    frame = capture_live_frame(display_index=display_idx)
    if frame is None:
        return jsonify({"error": "Could not capture frame"}), 500
    return Response(frame, mimetype="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.route("/api/sessions/<session_id>/screenshots/<filename>", methods=["GET"])
def get_session_screenshot(session_id, filename):
    """Serve a saved screenshot image for a session."""
    # Sanitize filename to prevent path traversal
    safe_name = Path(filename).name
    path = _SCREENSHOT_DIR / session_id / safe_name
    if not path.exists():
        return jsonify({"error": "Screenshot not found"}), 404
    return send_file(str(path), mimetype="image/jpeg")


@app.route("/api/sessions/<session_id>/video", methods=["GET"])
def get_session_video(session_id):
    """Serve the recorded video file for a session."""
    video_path = paths.video_dir() / f"{session_id}.mp4"
    if not video_path.exists():
        return jsonify({"error": "No video recording for this session"}), 404
    return send_file(str(video_path), mimetype="video/mp4")


@app.route("/api/sessions/<session_id>/frame", methods=["GET"])
def get_session_frame(session_id):
    """Extract a single JPEG frame from the session's screen recording.

    Query params:
        t: timestamp in seconds (float)
    """
    video_path = paths.video_dir() / f"{session_id}.mp4"
    if not video_path.exists():
        return jsonify({"error": "No video recording for this session"}), 404
    t = request.args.get("t", 0, type=float)
    jpeg_bytes = extract_frame(str(video_path), t)
    if not jpeg_bytes:
        return jsonify({"error": "Could not extract frame"}), 500
    return Response(jpeg_bytes, mimetype="image/jpeg")


@app.route("/api/models", methods=["GET"])
def get_models():
    """Return current model config and available presets."""
    cuda_available = get_cuda_available()
    has_hf_key = bool(os.getenv("HUGGING_FACE_KEY"))
    diarizer_device = _transcriber.diarizer_device
    with _state_lock:
        diarizer_ready = _state["diarizer_ready"]

    # If the diarizer hasn't loaded yet but an HF key exists, infer the
    # device from accelerator availability so the dropdown shows the right
    # value instead of "Disabled".
    if diarizer_device is None and has_hf_key:
        from core.compute_device import best_torch_device
        diarizer_device = best_torch_device()

    return jsonify({
        "cuda_available": cuda_available,
        "whisper": {
            "current": _transcriber.whisper_preset_id,
            "presets": [
                {**p, "available": not p["requires_cuda"] or cuda_available}
                for p in WHISPER_PRESETS
            ],
        },
        "diarizer": {
            "current": diarizer_device,
            "has_key": has_hf_key,
            "ready": diarizer_ready,
            "enabled": _transcriber.diarization_enabled,
            "options": [
                {**o, "available": not o["requires_cuda"] or cuda_available}
                for o in DIARIZER_OPTIONS
            ],
        },
    })


@app.route("/api/models/whisper", methods=["POST"])
def set_whisper_model():
    """Change the Whisper model. Cannot change while recording."""
    with _state_lock:
        if _state["is_recording"]:
            return jsonify({"error": "Cannot change model while recording"}), 400

    data = request.get_json(silent=True) or {}
    preset_id = data.get("preset_id", "").strip()
    preset = next((p for p in WHISPER_PRESETS if p["id"] == preset_id), None)
    if not preset:
        return jsonify({"error": "Unknown preset"}), 400
    if preset["requires_cuda"] and not get_cuda_available():
        return jsonify({"error": "CUDA not available"}), 400

    # Already on this preset?
    if preset_id == _transcriber.whisper_preset_id:
        return jsonify({"ok": True, "info": _transcriber.device_info})

    with _state_lock:
        _state["model_ready"] = False
        _state["model_info"] = f"Loading {preset['label']}…"
    _push_status()

    def _reload():
        try:
            _transcriber.reload_model(preset["device"], preset["compute_type"], preset["model_size"])
            settings.put("whisper_preset", preset_id)
            info = _transcriber.device_info
            with _state_lock:
                _state["model_ready"] = True
                _state["model_info"] = info
            _push_status()
        except Exception as e:
            log.error("whisper", f"Error reloading model: {e}")
            with _state_lock:
                _state["model_ready"] = False
                _state["model_info"] = f"Error: {e}"
            _push_status()

    threading.Thread(target=_reload, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/models/diarizer/enabled", methods=["POST"])
def set_diarizer_enabled():
    """Toggle speaker diarization on/off without unloading the model."""
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", True))
    _transcriber.diarization_enabled = enabled
    settings.put("diarization_enabled", enabled)
    _push_status()
    return jsonify({"ok": True, "enabled": enabled})


@app.route("/api/models/diarizer", methods=["POST"])
def set_diarizer_model():
    """Change the diarizer device. Cannot change while recording."""
    with _state_lock:
        if _state["is_recording"]:
            return jsonify({"error": "Cannot change model while recording"}), 400

    data = request.get_json(silent=True) or {}
    device = data.get("device", "").strip()
    option = next((o for o in DIARIZER_OPTIONS if o["id"] == device), None)
    if not option:
        return jsonify({"error": "Unknown device option"}), 400
    if option["requires_cuda"] and not get_cuda_available():
        return jsonify({"error": "CUDA not available"}), 400

    if device == _transcriber.diarizer_device:
        return jsonify({"ok": True})

    hf_token = os.getenv("HUGGING_FACE_KEY")
    if not hf_token:
        return jsonify({"error": "HUGGING_FACE_KEY not set"}), 400

    with _state_lock:
        _state["diarizer_ready"] = False
        _state["diarizer_failed"] = False   # reset - we're retrying
    _push_status()

    def _reload():
        try:
            _transcriber.reload_diarizer(hf_token, device)
            settings.put("diarizer_device", device)
            with _state_lock:
                _state["diarizer_ready"] = True
                _state["diarizer_failed"] = False
            _push_status()
        except Exception as e:
            log.error("diarizer", f"Error reloading: {e}")
            with _state_lock:
                _state["diarizer_ready"] = False
                _state["diarizer_failed"] = True
            _push_status()

    threading.Thread(target=_reload, daemon=True).start()
    return jsonify({"ok": True})


_ATTACH_DIR = paths.attachments_dir()
_ATTACH_DIR.mkdir(parents=True, exist_ok=True)

_SCREENSHOT_DIR = paths.screenshots_dir()
_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


def _save_screenshot(session_id: str, timestamp: float, jpeg_bytes: bytes) -> str:
    """Save screenshot JPEG to disk and return the URL path for markdown embedding."""
    session_dir = _SCREENSHOT_DIR / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{timestamp:.1f}s.jpg"
    (session_dir / filename).write_bytes(jpeg_bytes)
    return f"/api/sessions/{session_id}/screenshots/{filename}"

_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_ALLOWED_TYPES = _IMAGE_TYPES | {"application/pdf", "text/plain", "text/csv",
                                  "text/markdown", "application/json"}
_MAX_ATTACH_SIZE = 20 * 1024 * 1024  # 20 MB


@app.route("/api/chat/upload", methods=["POST"])
def chat_upload():
    """Upload a file for use as a chat attachment. Returns attachment metadata."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400

    data = f.read()
    if len(data) > _MAX_ATTACH_SIZE:
        return jsonify({"error": "File too large (max 20 MB)"}), 413

    mime = f.content_type or "application/octet-stream"
    # Accept any image or explicitly allowed type
    if mime not in _ALLOWED_TYPES and not mime.startswith("image/"):
        return jsonify({"error": f"Unsupported file type: {mime}"}), 415

    fid = str(uuid.uuid4())
    ext = Path(f.filename).suffix or ""
    stored_name = fid + ext
    (_ATTACH_DIR / stored_name).write_bytes(data)

    meta = {
        "id": fid,
        "filename": f.filename,
        "mime": mime,
        "size": len(data),
        "stored": stored_name,
    }
    return jsonify(meta)


@app.route("/api/chat/attachment/<filename>")
def chat_attachment(filename: str):
    """Serve an uploaded attachment file."""
    path = _ATTACH_DIR / filename
    if not path.exists():
        return jsonify({"error": "Not found"}), 404
    return send_file(path)


def _build_chat_history_from_messages(messages: list[dict]) -> list[dict]:
    """Convert stored chat messages (with optional attachments) to AI-ready history."""
    history = []
    for m in messages:
        att_json = m.get("attachments")
        attachments = json.loads(att_json) if att_json else None
        entry = _build_chat_entry(m["role"], m["content"], attachments)
        history.append(entry)
    return history


def _build_chat_entry(role: str, text: str, attachments: list[dict] | None = None) -> dict:
    """Build a single chat history entry, optionally with multimodal content."""
    if not attachments:
        return {"role": role, "content": text}
    # Build multimodal content blocks
    import base64
    blocks: list[dict] = []
    for att in attachments:
        mime = att.get("mime", "")
        stored = att.get("stored", "")
        fpath = _ATTACH_DIR / stored
        if not fpath.exists():
            continue
        if mime in _IMAGE_TYPES or mime.startswith("image/"):
            raw = fpath.read_bytes()
            b64 = base64.standard_b64encode(raw).decode("ascii")
            blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": mime, "data": b64},
            })
        else:
            # Text-based files: inline as text
            try:
                file_text = fpath.read_text(encoding="utf-8", errors="replace")
            except Exception:
                file_text = "(Could not read file)"
            blocks.append({
                "type": "text",
                "text": f"[Attached file: {att.get('filename', stored)}]\n{file_text}",
            })
    if text:
        blocks.append({"type": "text", "text": text})
    return {"role": role, "content": blocks}


@app.route("/api/chat", methods=["POST"])
def chat():
    """Send a chat message. Response is streamed via SSE."""
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id")
    question = (data.get("question") or "").strip()
    attachments = data.get("attachments") or []  # list of attachment metadata dicts
    if not question and not attachments:
        return jsonify({"error": "No question provided"}), 400

    request_id = str(uuid.uuid4())

    # Determine transcript, chat history, and metadata for context
    with _state_lock:
        active_sid = _state["session_id"]
        if session_id == active_sid:
            segments = list(_state["segments"])
            labels = dict(_state["speaker_labels"])
            transcript = _build_transcript(segments, labels)
            chat_history = list(_state["chat_history"])
            meta = _build_session_meta(
                segments, labels,
                is_live=_state["is_recording"],
                custom_prompt=_state["custom_prompt"],
                current_summary=_state["summary"],
            )
        else:
            transcript = None
            chat_history = []
            meta = None

    if transcript is None:
        sess = storage.get_session(session_id)
        if not sess:
            return jsonify({"error": "Session not found"}), 404
        labels = sess.get("speaker_labels") or {}
        transcript = _build_transcript(sess["segments"], labels)
        chat_history = _build_chat_history_from_messages(sess["chat_messages"])
        meta = _build_session_meta(
            sess["segments"], labels,
            session_title=sess.get("title", ""),
            is_live=False,
            started_at=sess.get("started_at", ""),
            ended_at=sess.get("ended_at", ""),
            current_summary=sess.get("summary", ""),
        )

    # Build the new user message (possibly multimodal)
    user_entry = _build_chat_entry("user", question, attachments or None)
    chat_history.append(user_entry)

    # Persist user message
    att_json = json.dumps(attachments) if attachments else None
    storage.save_chat_message(session_id, "user", question, attachments=att_json)

    # Update in-memory history if this is the active session
    with _state_lock:
        if session_id == _state["session_id"]:
            _state["chat_history"].append(user_entry)

    cancel_event = threading.Event()
    _chat_cancel[request_id] = cancel_event

    def run_chat():
        _push("chat_start", {"request_id": request_id, "question": question})
        response_chunks: list[str] = []
        tool_calls_log: list[dict] = []

        def on_token(t: str) -> None:
            if cancel_event.is_set():
                return
            response_chunks.append(t)
            _push("chat_chunk", {"request_id": request_id, "text": t})

        def on_tool_event(event_type: str, payload: dict) -> None:
            if cancel_event.is_set():
                return
            # Collect tool call data for persistence (omit large image data).
            # Match results to calls by id so parallel tool execution doesn't
            # mis-pair them; fall back to the first unresolved entry if the
            # backend somehow didn't supply an id.
            if event_type == "tool_call":
                tool_calls_log.append({
                    "id": payload.get("id"),
                    "name": payload["name"],
                    "input": payload.get("input", {}),
                    "result": None,
                })
            elif event_type == "tool_result" and tool_calls_log:
                target = None
                pid = payload.get("id")
                if pid is not None:
                    for tc in tool_calls_log:
                        if tc.get("id") == pid and tc["result"] is None:
                            target = tc
                            break
                if target is None:
                    for tc in tool_calls_log:
                        if tc["result"] is None:
                            target = tc
                            break
                if target is not None:
                    target["result"] = {
                        "success": payload.get("success", False),
                        "summary": payload.get("summary", ""),
                    }
            _push("chat_tool_event", {
                "request_id": request_id,
                "type": event_type,
                **payload,
            })

        def on_done() -> None:
            _chat_cancel.pop(request_id, None)
            full = "".join(response_chunks)
            if full.strip():
                tc_json = json.dumps(tool_calls_log) if tool_calls_log else None
                storage.save_chat_message(session_id, "assistant", full, tool_calls=tc_json)
                with _state_lock:
                    if session_id == _state["session_id"]:
                        _state["chat_history"].append({"role": "assistant", "content": full})
            _push("chat_done", {"request_id": request_id})

        # Build frame extractor - works for both live and completed recordings.
        # Returns (jpeg_bytes, url) so ai_assistant can show the image to the
        # model AND give it a markdown-embeddable URL for inline screenshots.
        fe = None
        video_path = paths.video_dir() / f"{session_id}.mp4"
        live_path = _screen_recorder.live_video_path

        display_idx = int(settings.get("screen_display", 0))

        def _saving_extractor(ts, sid=session_id, _didx=display_idx):
            """Extract frame, save to disk, return (jpeg_bytes, url)."""
            jpeg = None
            if live_path:
                jpeg = extract_frame(live_path, ts)
                if not jpeg:
                    jpeg = capture_live_frame(display_index=_didx)
            elif video_path.exists():
                jpeg = extract_frame(str(video_path), ts)
            if not jpeg:
                return None
            url = _save_screenshot(sid, ts, jpeg)
            return (jpeg, url)

        if live_path or video_path.exists():
            fe = _saving_extractor

        cp, cm = _resolve_tool_ai("chat")
        # Resolve effective system prompt: session override > global preference
        # > built-in default (handled inside ai.ask when system_prompt is None).
        session_prompt = storage.get_session_chat_prompt(session_id)
        global_prompt = settings.get("chat_system_prompt") or None
        effective_prompt = session_prompt or global_prompt
        ai.ask(transcript, chat_history, on_token, on_done, meta=meta,
               cancel=cancel_event, frame_extractor=fe,
               on_tool_event=on_tool_event,
               provider=cp, model=cm,
               system_prompt=effective_prompt)

    threading.Thread(target=run_chat, daemon=True).start()
    return jsonify({"request_id": request_id})


@app.route("/api/chat/stop", methods=["POST"])
def chat_stop():
    """Cancel an in-flight chat response."""
    data = request.get_json(silent=True) or {}
    rid = data.get("request_id")
    if rid and rid in _chat_cancel:
        _chat_cancel[rid].set()
        return jsonify({"ok": True})
    # No specific request_id - cancel all active chat streams
    for ev in _chat_cancel.values():
        ev.set()
    return jsonify({"ok": True})


@app.route("/api/chat/clear", methods=["POST"])
def chat_clear():
    """Delete all chat messages for a session."""
    data = request.get_json(silent=True) or {}
    sid = data.get("session_id")
    if not sid:
        return jsonify({"error": "session_id required"}), 400
    storage.clear_chat_messages(sid)
    with _state_lock:
        if _state["session_id"] == sid:
            _state["chat_history"] = []
    return jsonify({"ok": True})


# ── Chat system prompt (built-in default, global override, per-session override)

@app.route("/api/chat/default-prompt", methods=["GET"])
def api_chat_default_prompt():
    """Return the built-in default chat system prompt (read-only)."""
    return jsonify({"prompt": AIAssistant._SYSTEM_QA})


@app.route("/api/sessions/<sid>/chat-prompt", methods=["GET"])
def api_get_session_chat_prompt(sid):
    """Return all three prompt layers so the UI can show what's in effect."""
    return jsonify({
        "session_prompt": storage.get_session_chat_prompt(sid),
        "global_prompt":  settings.get("chat_system_prompt") or "",
        "default_prompt": AIAssistant._SYSTEM_QA,
    })


@app.route("/api/sessions/<sid>/chat-prompt", methods=["PUT"])
def api_set_session_chat_prompt(sid):
    """Store a per-session chat prompt override. Empty string or null clears."""
    data = request.get_json(silent=True) or {}
    prompt = data.get("prompt")
    if isinstance(prompt, str) and prompt.strip() == "":
        prompt = None
    if prompt is not None and not isinstance(prompt, str):
        return jsonify({"error": "prompt must be a string or null"}), 400
    storage.set_session_chat_prompt(sid, prompt)
    return jsonify({"ok": True, "session_prompt": prompt})


# ── Summary system prompt (built-in default, global override, per-session)

@app.route("/api/summary/default-prompt", methods=["GET"])
def api_summary_default_prompt():
    """Return the built-in default summary system prompt (read-only)."""
    return jsonify({"prompt": AIAssistant._SYSTEM_SUMMARY})


@app.route("/api/title/default-prompt", methods=["GET"])
def api_title_default_prompt():
    """Return the built-in default session-title system prompt (read-only)."""
    return jsonify({"prompt": AIAssistant._SYSTEM_TITLE})


@app.route("/api/sessions/<sid>/summary-prompt", methods=["GET"])
def api_get_session_summary_prompt(sid):
    """Return all three prompt layers so the UI can show what's in effect."""
    return jsonify({
        "session_prompt": storage.get_session_summary_prompt(sid),
        "global_prompt":  settings.get("summary_system_prompt") or "",
        "default_prompt": AIAssistant._SYSTEM_SUMMARY,
    })


@app.route("/api/sessions/<sid>/summary-prompt", methods=["PUT"])
def api_set_session_summary_prompt(sid):
    """Store a per-session summary prompt override. Empty string or null clears."""
    data = request.get_json(silent=True) or {}
    prompt = data.get("prompt")
    if isinstance(prompt, str) and prompt.strip() == "":
        prompt = None
    if prompt is not None and not isinstance(prompt, str):
        return jsonify({"error": "prompt must be a string or null"}), 400
    storage.set_session_summary_prompt(sid, prompt)
    return jsonify({"ok": True, "session_prompt": prompt})


# ── Notes (rich-text Quill Delta + inline attachments) ──────────────────────

_NOTES_DIR = paths.data_dir() / "notes"
_NOTES_DIR.mkdir(parents=True, exist_ok=True)

_NOTES_MAX_ATTACHMENT_SIZE = 50 * 1024 * 1024  # 50 MB per attachment


@app.route("/api/sessions/<sid>/notes", methods=["GET"])
def api_get_session_notes(sid):
    """Return the stored rich-text notes for a session, or an empty payload."""
    payload = storage.get_session_notes(sid)
    if not payload:
        return jsonify({"delta": None, "updated_at": None})
    return jsonify(payload)


@app.route("/api/sessions/<sid>/notes", methods=["PUT"])
def api_set_session_notes(sid):
    """Persist a Quill Delta document for the session. Pass null to clear."""
    data = request.get_json(silent=True, force=True) or {}
    delta = data.get("delta")
    if delta is not None and not isinstance(delta, (dict, list)):
        return jsonify({"error": "delta must be an object, list, or null"}), 400
    # Quill emits {"ops": [...]}; tolerate both shapes for forward compat.
    if isinstance(delta, dict):
        ops = delta.get("ops")
        if ops is None or (isinstance(ops, list) and not ops):
            delta = None
    elif isinstance(delta, list) and not delta:
        delta = None
    storage.set_session_notes(sid, delta)
    return jsonify({"ok": True})


@app.route("/api/sessions/<sid>/notes/attachments", methods=["POST"])
def api_upload_note_attachment(sid):
    """Upload an inline image or attached file for the notes pane.

    Returns a JSON payload with the URL for embedding into the Quill document
    plus metadata (filename, mime, size) used to render the inline chip.
    """
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400

    data_bytes = f.read()
    if len(data_bytes) > _NOTES_MAX_ATTACHMENT_SIZE:
        return jsonify({"error": "File too large (max 50 MB)"}), 413

    mime = (f.content_type or "application/octet-stream").lower()
    fid = str(uuid.uuid4())
    suffix = Path(f.filename).suffix.lower() or ""
    stored_name = fid + suffix

    session_dir = _NOTES_DIR / sid
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / stored_name).write_bytes(data_bytes)

    return jsonify({
        "id": fid,
        "filename": f.filename,
        "mime": mime,
        "size": len(data_bytes),
        "stored": stored_name,
        "url": f"/api/sessions/{sid}/notes/attachments/{stored_name}",
    })


@app.route("/api/sessions/<sid>/notes/attachments/<stored>", methods=["GET"])
def api_get_note_attachment(sid, stored):
    """Serve a previously uploaded note attachment."""
    path = _NOTES_DIR / sid / stored
    if not path.exists() or ".." in stored or "/" in stored or "\\" in stored:
        return jsonify({"error": "Not found"}), 404
    return send_file(path)


# ── Global Chat ──────────────────────────────────────────────────────────────

# Cancel events for global chat requests (separate from session chat)
_global_chat_cancel: dict[str, threading.Event] = {}


@app.route("/api/global-chat/conversations", methods=["GET"])
def list_global_conversations():
    return jsonify(storage.list_global_conversations())


@app.route("/api/global-chat/conversations", methods=["POST"])
def create_global_conversation():
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "New Chat").strip()
    cid = storage.create_global_conversation(title)
    return jsonify({"id": cid, "title": title})


@app.route("/api/global-chat/conversations/<conversation_id>", methods=["GET"])
def get_global_conversation(conversation_id: str):
    conv = storage.get_global_conversation(conversation_id)
    if not conv:
        return jsonify({"error": "Not found"}), 404
    return jsonify(conv)


@app.route("/api/global-chat/conversations/<conversation_id>", methods=["PATCH"])
def rename_global_conversation(conversation_id: str):
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title required"}), 400
    storage.rename_global_conversation(conversation_id, title)
    return jsonify({"ok": True})


@app.route("/api/global-chat/conversations/<conversation_id>", methods=["DELETE"])
def delete_global_conversation(conversation_id: str):
    storage.delete_global_conversation(conversation_id)
    return jsonify({"ok": True})


@app.route("/api/global-chat/clear", methods=["POST"])
def global_chat_clear():
    data = request.get_json(silent=True) or {}
    cid = data.get("conversation_id")
    if not cid:
        return jsonify({"error": "conversation_id required"}), 400
    storage.clear_global_chat_messages(cid)
    return jsonify({"ok": True})


def _global_tool_executor(name: str, tool_input: dict) -> tuple:
    """Execute a global chat tool call. Returns (content, is_error, summary, extra)."""
    if name == "search_transcripts":
        query = tool_input.get("query", "")
        limit = tool_input.get("limit", 10)
        results = storage.search_sessions(query, limit=limit)
        if not results:
            return "No matching sessions found.", False, f"Search: '{query}' - no results", None
        # Enrich results with folder names and summaries
        folders = {f["id"]: f["name"] for f in storage.list_folders()}
        for r in results:
            sess = storage.get_session(r["session_id"])
            if sess:
                fid = sess.get("folder_id")
                r["folder"] = folders.get(fid) if fid else None
                summary = sess.get("summary", "")
                if summary:
                    r["summary"] = summary[:500] + ("…" if len(summary) > 500 else "")
        text = json.dumps(results, indent=2)
        return text, False, f"Search: '{query}' - {len(results)} results", None

    if name == "semantic_search":
        query = tool_input.get("query", "")
        limit = tool_input.get("limit", 5)
        if not text_embeddings.is_ready():
            return "Semantic search model is still loading.", True, "Semantic search unavailable", None
        query_vec = text_embeddings.encode(query)
        if query_vec is None:
            return "Failed to encode query.", True, "Encoding failed", None
        all_embs = storage.get_all_session_embeddings()
        scored = []
        for row in all_embs:
            vec = text_embeddings.bytes_to_embedding(row["embedding_bytes"])
            score = text_embeddings.cosine_similarity(query_vec, vec)
            if score >= 0.25:
                scored.append({
                    "session_id": row["session_id"],
                    "title": row["title"],
                    "score": round(score, 4),
                })
        scored.sort(key=lambda x: x["score"], reverse=True)
        results = scored[:limit]
        if not results:
            return "No semantically similar sessions found.", False, f"Semantic: '{query}' - no results", None
        # Enrich with folder names and summaries
        folders = {f["id"]: f["name"] for f in storage.list_folders()}
        for r in results:
            sess = storage.get_session(r["session_id"])
            if sess:
                fid = sess.get("folder_id")
                r["folder"] = folders.get(fid) if fid else None
                summary = sess.get("summary", "")
                if summary:
                    r["summary"] = summary[:500] + ("…" if len(summary) > 500 else "")
        text = json.dumps(results, indent=2)
        return text, False, f"Semantic: '{query}' - {len(results)} results", None

    if name == "get_session_detail":
        session_id = tool_input.get("session_id", "")
        sess = storage.get_session(session_id)
        if not sess:
            return f"Session '{session_id}' not found.", True, "Session not found", None
        labels = sess.get("speaker_labels") or {}
        transcript = _build_transcript(sess["segments"], labels)
        # Truncate very long transcripts
        if len(transcript) > 200000:
            transcript = transcript[:200000] + "\n\n... [transcript truncated - too long to show in full]"
        summary = sess.get("summary", "")
        result = f"Session: {sess.get('title', 'Untitled')}\n"
        result += f"Started: {sess.get('started_at', 'unknown')}\n"
        if sess.get("ended_at"):
            result += f"Ended: {sess['ended_at']}\n"
        result += f"Segments: {len(sess['segments'])}\n\n"
        if summary:
            result += f"Summary:\n---\n{summary}\n---\n\n"
        result += f"Transcript:\n---\n{transcript}\n---"
        return result, False, f"Loaded session: {sess.get('title', session_id)}", None

    if name == "list_speakers":
        speakers = fingerprint_db.list_global_speakers()
        if not speakers:
            return "No speakers in the Voice Library yet.", False, "No speakers found", None
        # Enrich with session counts
        enriched = []
        for sp in speakers:
            sessions = fingerprint_db.get_profile_sessions(sp["id"])
            enriched.append({
                "id": sp["id"],
                "name": sp["name"],
                "color": sp.get("color"),
                "session_count": len(sessions),
            })
        text = json.dumps(enriched, indent=2)
        return text, False, f"Found {len(enriched)} speakers", None

    if name == "list_recent_meetings":
        from datetime import datetime, timedelta, timezone
        within_days = tool_input.get("within_days") or 0
        start_date = (tool_input.get("start_date") or "").strip()
        end_date = (tool_input.get("end_date") or "").strip()
        limit = max(1, min(200, int(tool_input.get("limit") or 30)))

        def _parse_iso(s: str):
            try:
                return datetime.fromisoformat(s.replace("Z", "+00:00"))
            except Exception:
                return None

        start_dt = _parse_iso(start_date) if start_date else None
        end_dt = _parse_iso(end_date) if end_date else None
        if end_dt:
            # Inclusive end-of-day if a bare date was given
            if "T" not in end_date and " " not in end_date:
                end_dt = end_dt + timedelta(days=1) - timedelta(seconds=1)
        cutoff = None
        if within_days > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(days=int(within_days))

        sessions = storage.list_sessions()  # already sorted started_at DESC

        def in_range(s):
            sa = s.get("started_at")
            if not sa:
                return False
            ts = _parse_iso(sa)
            if ts is None:
                return False
            # Make naive timestamps timezone-aware for comparison
            if ts.tzinfo is None and (cutoff or start_dt or end_dt):
                ts = ts.replace(tzinfo=timezone.utc)
            if cutoff and ts < cutoff:
                return False
            if start_dt:
                sd = start_dt if start_dt.tzinfo else start_dt.replace(tzinfo=timezone.utc)
                if ts < sd:
                    return False
            if end_dt:
                ed = end_dt if end_dt.tzinfo else end_dt.replace(tzinfo=timezone.utc)
                if ts > ed:
                    return False
            return True

        if cutoff or start_dt or end_dt:
            sessions = [s for s in sessions if in_range(s)]
        sessions = sessions[:limit]

        folders = {f["id"]: f["name"] for f in storage.list_folders()}
        enriched = []
        for s in sessions:
            sess = storage.get_session(s["id"])
            entry = {
                "session_id": s["id"],
                "title": s.get("title") or "Untitled",
                "started_at": s.get("started_at"),
                "ended_at": s.get("ended_at"),
                "speakers": [sp["name"] for sp in (s.get("speakers") or []) if sp.get("name")],
            }
            if sess:
                fid = sess.get("folder_id")
                entry["folder"] = folders.get(fid) if fid else None
                summary = sess.get("summary", "") or ""
                if summary:
                    entry["summary"] = summary[:300] + ("…" if len(summary) > 300 else "")
                entry["segment_count"] = len(sess.get("segments") or [])
            enriched.append(entry)

        if within_days > 0:
            range_desc = f"last {within_days} day{'s' if within_days != 1 else ''}"
        elif start_date and end_date:
            range_desc = f"{start_date} to {end_date}"
        elif start_date:
            range_desc = f"since {start_date}"
        elif end_date:
            range_desc = f"until {end_date}"
        else:
            range_desc = "all time"

        text = json.dumps(enriched, indent=2)
        return text, False, f"Listed {len(enriched)} meetings ({range_desc})", None

    if name == "get_speaker_history":
        speaker_name = tool_input.get("speaker_name", "").strip()
        if not speaker_name:
            return "Speaker name is required.", True, "Missing speaker name", None
        # Find matching global speaker(s) by name (case-insensitive)
        all_speakers = fingerprint_db.list_global_speakers()
        matched = [s for s in all_speakers if s["name"].lower() == speaker_name.lower()]
        if not matched:
            # Try partial match
            matched = [s for s in all_speakers if speaker_name.lower() in s["name"].lower()]
        if not matched:
            return f"No speaker named '{speaker_name}' found in the Voice Library.", False, f"Speaker '{speaker_name}' not found", None
        results = []
        for sp in matched:
            sessions = fingerprint_db.get_profile_sessions(sp["id"])
            # Enrich sessions with summaries and folder info
            folders = {f["id"]: f["name"] for f in storage.list_folders()}
            enriched_sessions = []
            for sess_info in sessions:
                sess = storage.get_session(sess_info["session_id"])
                entry = {
                    "session_id": sess_info["session_id"],
                    "title": sess_info["title"],
                    "started_at": sess_info["started_at"],
                    "segments_by_speaker": sess_info["seg_count"],
                }
                if sess:
                    fid = sess.get("folder_id")
                    entry["folder"] = folders.get(fid) if fid else None
                    summary = sess.get("summary", "")
                    if summary:
                        entry["summary"] = summary[:500] + ("…" if len(summary) > 500 else "")
                enriched_sessions.append(entry)
            results.append({
                "speaker_id": sp["id"],
                "speaker_name": sp["name"],
                "total_sessions": len(sessions),
                "sessions": enriched_sessions,
            })
        text = json.dumps(results, indent=2)
        total = sum(r["total_sessions"] for r in results)
        return text, False, f"Speaker '{speaker_name}': {total} sessions", None

    return f"Unknown tool: {name}", True, f"Unknown tool: {name}", None


@app.route("/api/global-chat", methods=["POST"])
def global_chat():
    """Send a message to global chat. Response is streamed via SSE."""
    data = request.get_json(silent=True) or {}
    conversation_id = data.get("conversation_id")
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "No question provided"}), 400

    # Auto-create conversation if needed
    if not conversation_id:
        conversation_id = storage.create_global_conversation()

    request_id = str(uuid.uuid4())

    # Load existing conversation history
    conv = storage.get_global_conversation(conversation_id)
    chat_history = []
    if conv and conv.get("messages"):
        chat_history = _build_chat_history_from_messages(conv["messages"])

    # Add the new user message
    user_entry = _build_chat_entry("user", question)
    chat_history.append(user_entry)
    storage.save_global_chat_message(conversation_id, "user", question)

    cancel_event = threading.Event()
    _global_chat_cancel[request_id] = cancel_event

    def run_global_chat():
        _push("global_chat_start", {
            "request_id": request_id,
            "conversation_id": conversation_id,
            "question": question,
        })
        response_chunks: list[str] = []
        tool_calls_log: list[dict] = []

        def on_token(t: str) -> None:
            if cancel_event.is_set():
                return
            response_chunks.append(t)
            _push("global_chat_chunk", {"request_id": request_id, "text": t})

        def on_tool_event(event_type: str, payload: dict) -> None:
            if cancel_event.is_set():
                return
            # Match results to calls by id so parallel tool execution doesn't
            # mis-pair them; fall back to the first unresolved entry if the
            # backend somehow didn't supply an id.
            if event_type == "tool_call":
                tool_calls_log.append({
                    "id": payload.get("id"),
                    "name": payload["name"],
                    "input": payload.get("input", {}),
                    "result": None,
                })
            elif event_type == "tool_result" and tool_calls_log:
                target = None
                pid = payload.get("id")
                if pid is not None:
                    for tc in tool_calls_log:
                        if tc.get("id") == pid and tc["result"] is None:
                            target = tc
                            break
                if target is None:
                    for tc in tool_calls_log:
                        if tc["result"] is None:
                            target = tc
                            break
                if target is not None:
                    target["result"] = {
                        "success": payload.get("success", False),
                        "summary": payload.get("summary", ""),
                    }
            _push("global_chat_tool_event", {
                "request_id": request_id,
                "type": event_type,
                **payload,
            })

        def on_done() -> None:
            _global_chat_cancel.pop(request_id, None)
            full = "".join(response_chunks)
            if full.strip():
                tc_json = json.dumps(tool_calls_log) if tool_calls_log else None
                storage.save_global_chat_message(conversation_id, "assistant", full, tool_calls=tc_json)

            # Auto-title: if this is the first exchange and title is still default
            if conv and conv.get("title") in ("New Chat", None, ""):
                try:
                    title = ai.generate_title(
                        question,
                        system_prompt=settings.get("title_system_prompt") or None,
                    )
                    if title:
                        storage.rename_global_conversation(conversation_id, title)
                        _push("global_chat_title", {
                            "conversation_id": conversation_id,
                            "title": title,
                        })
                except Exception:
                    pass

            _push("global_chat_done", {"request_id": request_id})

        cp, cm = _resolve_tool_ai("global_chat")
        ai.ask_global(
            chat_history, on_token, on_done,
            cancel=cancel_event,
            on_tool_event=on_tool_event,
            tool_executor=_global_tool_executor,
            provider=cp, model=cm,
        )

    threading.Thread(target=run_global_chat, daemon=True).start()
    return jsonify({"request_id": request_id, "conversation_id": conversation_id})


@app.route("/api/global-chat/stop", methods=["POST"])
def global_chat_stop():
    data = request.get_json(silent=True) or {}
    rid = data.get("request_id")
    if rid and rid in _global_chat_cancel:
        _global_chat_cancel[rid].set()
        return jsonify({"ok": True})
    for ev in _global_chat_cancel.values():
        ev.set()
    return jsonify({"ok": True})


@app.route("/api/analytics")
def get_analytics():
    return jsonify(storage.get_dashboard_analytics())


@app.route("/api/sessions/<session_id>", methods=["DELETE"])
def delete_session(session_id: str):
    # If this is the last surviving member of a split group, the rollback
    # backup is now orphaned — clean it up to reclaim disk space.
    group_id = storage.get_session_split_group_id(session_id)
    storage.delete_session(session_id)
    if group_id:
        try:
            remaining = storage.list_split_group_members(group_id)
            if not remaining:
                media_edit.clear_split_backup(group_id)
        except Exception:
            pass  # best-effort
    return jsonify({"ok": True})


@app.route("/api/segments/<int:seg_id>/label", methods=["PATCH"])
def update_segment_label(seg_id: int):
    """Set a per-segment label override (one-off rename)."""
    data = request.get_json(silent=True) or {}
    label = (data.get("label") or "").strip()
    if not label:
        return jsonify({"error": "label is required"}), 400
    storage.save_segment_label_override(seg_id, label)

    # Persist speaker-key reassignment if provided
    source_override = (data.get("source_override") or "").strip() or None
    storage.save_segment_source_override(seg_id, source_override)

    # Train voice library from this correction (skip for noise labels)
    if fingerprint_db.ready and label != _NOISE_LABEL:
        def _train_from_override():
            seg = storage.get_segment(seg_id)
            if not seg:
                return
            wav_path = paths.audio_dir() / f"{seg['session_id']}.wav"
            if not wav_path.exists():
                return
            if seg["end_time"] - seg["start_time"] < fingerprint_db.MIN_DURATION_SEC:
                return
            profile = fingerprint_db.find_by_name(label)
            if profile is None:
                gid = fingerprint_db.create_global_speaker(label)
            else:
                gid = profile["id"]
            emb = fingerprint_db.extract_embedding_from_wav(
                str(wav_path), seg["start_time"], seg["end_time"])
            if emb is not None:
                fingerprint_db.add_embedding(gid, seg["session_id"], seg["source"], emb,
                                             seg["end_time"] - seg["start_time"])
                fingerprint_db.link_session_speaker(seg["session_id"], seg["source"], gid)
                _push("speaker_linked", {
                    "session_id": seg["session_id"], "speaker_key": seg["source"],
                    "global_id": gid, "name": label,
                })
                log.info("fingerprint", f"Trained from segment override: {label!r} (seg {seg_id})")
        _fp_executor.submit(_train_from_override)

    seg_row = storage.get_segment(seg_id)
    if seg_row:
        _queue_obsidian_export(seg_row["session_id"])
    return jsonify({"ok": True})


@app.route("/api/sessions/<session_id>/speakers", methods=["GET"])
def list_speaker_profiles(session_id: str):
    sess = storage.get_session(session_id)
    if not sess:
        return jsonify({"error": "Session not found"}), 404
    return jsonify({"speakers": storage.list_speaker_profiles(session_id)})


@app.route("/api/sessions/<session_id>/speakers", methods=["POST"])
def create_speaker_profile(session_id: str):
    sess = storage.get_session(session_id)
    if not sess:
        return jsonify({"error": "Session not found"}), 404

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400

    try:
        color = _normalize_speaker_color(data.get("color"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    speaker_key = f"{_CUSTOM_SPEAKER_PREFIX}{uuid.uuid4().hex[:8]}"
    speaker = storage.save_speaker_label(session_id, speaker_key, name=name, color=color)
    return jsonify({"ok": True, "speaker": speaker}), 201


@app.route("/api/sessions/<session_id>/speakers", methods=["PATCH"])
def update_speaker_label(session_id: str):
    sess = storage.get_session(session_id)
    if not sess:
        return jsonify({"error": "Session not found"}), 404

    data = request.get_json(silent=True) or {}
    raw_keys = data.get("speaker_keys")
    if raw_keys is None:
        speaker_key = (data.get("speaker_key") or "").strip()
        speaker_keys = [speaker_key] if speaker_key else []
    else:
        speaker_keys = [
            str(k).strip() for k in raw_keys
            if str(k).strip()
        ]
    if not speaker_keys:
        return jsonify({"error": "speaker_key or speaker_keys required"}), 400

    name = data.get("name")
    if name is not None:
        name = str(name).strip()
        if not name:
            return jsonify({"error": "name cannot be blank"}), 400

    try:
        color = _normalize_speaker_color(data.get("color"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    if name is None and color is None:
        return jsonify({"error": "name and/or color required"}), 400

    updated_speakers = []
    rename_changes: list[tuple[str, str]] = []
    seen: set[str] = set()
    for speaker_key in speaker_keys:
        if speaker_key in seen:
            continue
        seen.add(speaker_key)
        previous = storage.get_speaker_profile(session_id, speaker_key) or {}
        updated = storage.save_speaker_label(session_id, speaker_key, name=name, color=color)
        updated_speakers.append(updated)
        previous_name = (previous.get("name") or speaker_key).strip()
        if name is not None and previous_name != updated["name"] and not _is_custom_speaker_key(speaker_key):
            rename_changes.append((previous_name, updated["name"]))

    with _state_lock:
        if _state["session_id"] == session_id:
            for speaker in updated_speakers:
                speaker_key = speaker["speaker_key"]
                speaker_name = speaker["name"]

                # Detect merge: another diarized speaker key already has this display name.
                existing_key = next(
                    (
                        k for k, v in _state["speaker_labels"].items()
                        if k != speaker_key and v.lower() == speaker_name.lower() and not _is_custom_speaker_key(k)
                    ),
                    None,
                )
                if not _is_custom_speaker_key(speaker_key):
                    _state["speaker_labels"][speaker_key] = speaker_name
                    if existing_key and _transcriber.diarizer is not None:
                        _transcriber.diarizer.merge_speakers(existing_key, speaker_key)

    for speaker in updated_speakers:
        _push("speaker_label", {
            "session_id": session_id,
            "speaker_key": speaker["speaker_key"],
            "name": speaker["name"],
            "color": speaker["color"],
        })

    update_context = _speaker_summary_update_context(rename_changes)
    if update_context:
        _queue_speaker_summary_refresh(session_id, update_context)

    # ── Auto-create or link global voice profile ───────────────────────────────
    # For every speaker key that now has a user-assigned name (not a default
    # "Speaker N"), ensure a global profile exists and the key is linked to it.
    if fingerprint_db._ready and name and not _is_default_speaker_name(name):
        def _sync_voice_profile(sid, keys, label, col):
            try:
                profile = fingerprint_db.find_by_name(label)
                if profile is None:
                    gid = fingerprint_db.create_global_speaker(label, col)
                    global_color = col
                    log.info("fingerprint", f"Auto-created profile {label!r} from session label")
                else:
                    gid = profile["id"]
                    # Inherit the global profile's color unless the user explicitly
                    # set one in this request.
                    global_color = col or profile.get("color")
                for k in keys:
                    existing = fingerprint_db.get_link(sid, k)
                    if existing != gid:
                        fingerprint_db.link_session_speaker(sid, k, gid)
                    # Sync session speaker color to the global profile color
                    if global_color:
                        storage.save_speaker_label(sid, k, name=label, color=global_color)
                        _push("speaker_label", {
                            "session_id": sid, "speaker_key": k,
                            "name": label, "color": global_color,
                        })
                    _push("speaker_linked", {
                        "session_id": sid, "speaker_key": k,
                        "global_id": gid, "name": label,
                    })
                # Extract embeddings to strengthen the profile
                for k in keys:
                    # Try live accumulator first
                    with _state_lock:
                        accum = _state.get("speaker_audio_accum", {})
                        seg_audio = accum.get(k, {}).get("audio")
                        seg_audio = seg_audio.copy() if seg_audio is not None else None
                    if seg_audio is not None and len(seg_audio) / 16000 >= fingerprint_db.MIN_DURATION_SEC:
                        emb = fingerprint_db.extract_embedding(seg_audio)
                        if emb is not None:
                            fingerprint_db.add_embedding(gid, sid, k, emb, len(seg_audio) / 16000)
                            log.info("fingerprint", f"Added embedding from accumulator for {label!r}")
                            continue
                    # Fallback: extract from WAV file (past session or accumulator empty)
                    wav_path = paths.audio_dir() / f"{sid}.wav"
                    if wav_path.exists():
                        segments = storage.get_segments_by_speaker(sid, k)
                        added = 0
                        for seg in segments:
                            if added >= 5:
                                break
                            emb = fingerprint_db.extract_embedding_from_wav(
                                str(wav_path), seg["start_time"], seg["end_time"])
                            if emb is not None:
                                fingerprint_db.add_embedding(gid, sid, k, emb,
                                                             seg["end_time"] - seg["start_time"])
                                added += 1
                        if added:
                            log.info("fingerprint", f"Added {added} embeddings from WAV for {label!r}")
            except Exception as e:
                log.error("fingerprint", f"_sync_voice_profile failed: {e}")
                import traceback; traceback.print_exc()
        _fp_executor.submit(
            _sync_voice_profile,
            session_id, [s["speaker_key"] for s in updated_speakers],
            name, color,
        )
    # ── End auto-link ──────────────────────────────────────────────────────────

    _queue_obsidian_export(session_id)
    return jsonify({"ok": True, "speakers": updated_speakers})


@app.route("/api/sessions/<session_id>/speaker_clusters", methods=["GET"])
def get_speaker_clusters(session_id: str):
    """Compute speaker clusters for the post-meeting cleanup UI.

    Backfills missing embeddings from the session WAV on demand — that step
    can take a few seconds for sessions with many unlabeled speakers, so the
    client should show a loading indicator.
    """
    sess = storage.get_session(session_id)
    if not sess:
        return jsonify({"error": "Session not found"}), 404
    if not fingerprint_db.ready:
        return jsonify({"error": "Voice fingerprint model not ready"}), 503
    wav_path = paths.audio_dir() / f"{session_id}.wav"
    try:
        payload = fingerprint_db.cluster_session_speakers(
            session_id,
            wav_path=str(wav_path) if wav_path.exists() else None,
        )
        return jsonify(payload)
    except Exception as e:
        log.error("fingerprint", f"cluster_session_speakers failed: {e}")
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/sessions/<session_id>/speaker_clusters/apply", methods=["POST"])
def apply_speaker_clusters(session_id: str):
    """Apply user's cleanup decisions and retrain affected library profiles."""
    sess = storage.get_session(session_id)
    if not sess:
        return jsonify({"error": "Session not found"}), 404
    data = request.get_json(silent=True) or {}
    proposed = data.get("clusters") or []
    noise_keys = data.get("noise_keys") or []
    if not isinstance(proposed, list):
        return jsonify({"error": "clusters must be a list"}), 400

    try:
        result = fingerprint_db.apply_cluster_corrections(
            session_id, proposed, noise_keys=noise_keys,
        )
    except Exception as e:
        log.error("fingerprint", f"apply_cluster_corrections failed: {e}")
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500

    # Push a state refresh so the live UI picks up the new labels.
    with _state_lock:
        if _state["session_id"] == session_id:
            for sp in storage.list_speaker_profiles(session_id):
                _state["speaker_labels"][sp["speaker_key"]] = sp["name"]

    for sp in storage.list_speaker_profiles(session_id):
        _push("speaker_label", {
            "session_id": session_id,
            "speaker_key": sp["speaker_key"],
            "name": sp["name"],
            "color": sp["color"],
        })

    _queue_obsidian_export(session_id)
    return jsonify({"ok": True, **result})


@app.route("/api/sessions/<session_id>/audio")
def session_audio(session_id: str):
    """Serve the recorded WAV file for browser playback."""
    wav_path = paths.audio_dir() / f"{session_id}.wav"
    if not wav_path.exists():
        return jsonify({"error": "No audio recording for this session"}), 404
    return send_file(str(wav_path), mimetype="audio/wav", conditional=True)


@app.route("/api/sessions/<session_id>/audio-profile")
def session_audio_profile(session_id: str):
    sess = storage.get_session(session_id)
    if not sess:
        return jsonify({"error": "Session not found"}), 404
    try:
        bins = request.args.get("bins", 1200, type=int)
        cfg = settings.load()
        profile = media_edit.build_audio_profile(
            session_id,
            bins=bins,
            segments=sess.get("segments", []),
            speaker_profiles=sess.get("speaker_profiles", []),
            quiet_threshold=float(cfg.get("quiet_prompt_audio_rms_threshold", 0.006)),
            min_quiet_sec=float(cfg.get("quiet_prompt_threshold_sec", 30)),
        )
        return jsonify(profile)
    except FileNotFoundError:
        return jsonify({"error": "No audio recording for this session"}), 404
    except Exception as e:
        log.error("media", f"audio profile failed for {session_id[:8]}: {e}")
        return jsonify({"error": str(e)}), 500


def _validate_media_range(session_id: str, start_sec: float, end_sec: float) -> tuple[bool, str, float]:
    wav_path = media_edit.wav_path(session_id)
    if not wav_path.exists():
        return False, "No audio recording for this session", 0.0
    duration = media_edit.get_wav_duration(wav_path)
    if start_sec < 0 or end_sec <= start_sec or end_sec > duration + 0.05:
        return False, f"Invalid range. Expected 0 <= start < end <= {duration:.2f}", duration
    return True, "", duration


@app.route("/api/sessions/<session_id>/trim", methods=["POST"])
def trim_session(session_id: str):
    with _state_lock:
        if _state["is_recording"] and _state["session_id"] == session_id:
            return jsonify({"error": "Cannot trim an active recording"}), 400
    sess = storage.get_session(session_id)
    if not sess:
        return jsonify({"error": "Session not found"}), 404
    data = request.get_json(silent=True) or {}
    start_sec = float(data.get("start", 0))
    end_sec = float(data.get("end", 0))
    ok, err, _duration = _validate_media_range(session_id, start_sec, end_sec)
    if not ok:
        return jsonify({"error": err}), 400

    try:
        ffmpeg_bin = find_ffmpeg()
        video_offset = settings.get_video_offset(session_id)
        media_edit.backup_session_snapshot(session_id, sess, video_offset)
        if media_edit.video_path(session_id).exists() and not ffmpeg_bin:
            return jsonify({"error": "FFmpeg is required to trim a session with screen recording video"}), 500
        new_offset = media_edit.trim_video(session_id, start_sec, end_sec, video_offset, ffmpeg_bin)
        media_edit.trim_wav(session_id, start_sec, end_sec)
        settings.put_video_offset(session_id, new_offset)
        kept = storage.trim_session_segments(session_id, start_sec, end_sec)
        threading.Thread(target=update_session_embedding, args=(session_id,), daemon=True).start()
        return jsonify({
            "ok": True,
            "session_id": session_id,
            "duration": end_sec - start_sec,
            "segments": kept,
            "video_offset": new_offset,
        })
    except Exception as e:
        log.error("media", f"trim failed for {session_id[:8]}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/sessions/<session_id>/restore", methods=["POST"])
def restore_session(session_id: str):
    with _state_lock:
        if _state["is_recording"] and _state["session_id"] == session_id:
            return jsonify({"error": "Cannot restore an active recording"}), 400
    sess = storage.get_session(session_id)
    if not sess:
        return jsonify({"error": "Session not found"}), 404
    snapshot = media_edit.load_session_snapshot(session_id)
    if not snapshot:
        return jsonify({"error": "No trim backup found for this session"}), 404

    try:
        media_edit.restore_original_media(session_id)
        storage.restore_session_snapshot(session_id, snapshot.get("session") or {})
        settings.put_video_offset(session_id, float(snapshot.get("video_offset") or 0.0))
        media_edit.clear_trim_backup(session_id)
        threading.Thread(target=update_session_embedding, args=(session_id,), daemon=True).start()
        return jsonify({"ok": True, "session_id": session_id})
    except Exception as e:
        log.error("media", f"restore failed for {session_id[:8]}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/sessions/<session_id>/split", methods=["POST"])
def split_session(session_id: str):
    with _state_lock:
        if _state["is_recording"] and _state["session_id"] == session_id:
            return jsonify({"error": "Cannot split an active recording"}), 400
    source = storage.get_session(session_id)
    if not source:
        return jsonify({"error": "Session not found"}), 404
    data = request.get_json(silent=True) or {}
    ranges = data.get("ranges") or []
    if not isinstance(ranges, list) or not ranges:
        return jsonify({"error": "ranges required"}), 400

    source_audio = media_edit.wav_path(session_id)
    source_video = media_edit.video_path(session_id)
    source_video_offset = settings.get_video_offset(session_id)
    ffmpeg_bin = find_ffmpeg()
    if source_video.exists() and not ffmpeg_bin:
        return jsonify({"error": "FFmpeg is required to split a session with screen recording video"}), 500
    created: list[str] = []
    results: list[dict] = []
    src_title = source.get("title") or "Meeting"
    # Every part produced by this split shares one group id. Writing it into
    # the sessions table lets any part look up its siblings later (restore UI)
    # and lets the backup directory be keyed by the group rather than by the
    # about-to-be-deleted source session id.
    group_id = str(uuid.uuid4())
    should_delete_original = data.get("delete_original", True)

    # Resolve a single base time for the whole split. Every part's
    # started_at/ended_at is derived from this base + its (start_sec, end_sec)
    # so part N+1 always lands exactly when part N ended. Computing it once
    # at the call site (rather than inside create_split_session for each
    # part) makes the cumulative-offset behavior explicit and prevents any
    # silent _now() fallback from making all parts cluster at "right now".
    from datetime import datetime as _dt, timedelta as _td
    src_started = source.get("started_at")
    base_dt: _dt | None = None
    if src_started:
        try:
            base_dt = _dt.fromisoformat(src_started)
        except Exception as e:
            log.warn("media", f"split: source {session_id[:8]} started_at "
                              f"{src_started!r} could not be parsed: {e}")
    if base_dt is None:
        # Fallback: anchor at now() but rewind by total source duration so the
        # last part lands roughly at "now" — better than every part stacking
        # at the same instant.
        total_dur = max((float(r.get("end", 0)) for r in ranges), default=0.0)
        base_dt = _dt.utcnow() - _td(seconds=total_dur)
        log.warn("media", f"split: source {session_id[:8]} has no parseable "
                          f"started_at; anchoring base at now() - {total_dur:.1f}s.")

    try:
        for idx, r in enumerate(ranges, start=1):
            start_sec = float(r.get("start", 0))
            end_sec = float(r.get("end", 0))
            ok, err, _duration = _validate_media_range(session_id, start_sec, end_sec)
            if not ok:
                raise ValueError(err)
            # Default titling: Part 1 inherits the original title verbatim,
            # subsequent parts get "<title> Part N". User-supplied titles win.
            user_title = (r.get("title") or "").strip()
            if user_title:
                title = user_title
            elif idx == 1:
                title = src_title
            else:
                title = f"{src_title} Part {idx}"
            # Compute this part's absolute timeline position from the shared
            # base. Subsequent parts naturally pick up where the previous
            # left off because their start_sec is the previous end_sec.
            part_started_at = (base_dt + _td(seconds=start_sec)).isoformat()
            part_ended_at = (base_dt + _td(seconds=end_sec)).isoformat()
            # Only tag parts with the split group id if the original will be
            # deleted (i.e. a real, undoable split). If the caller chooses to
            # keep the original, the "parts" are more like clips — no rollback
            # is needed and the group link would be misleading.
            new_sid = storage.create_split_session(
                session_id, start_sec, end_sec, title=title,
                split_group_id=group_id if should_delete_original else None,
                started_at=part_started_at,
                ended_at=part_ended_at,
            )
            created.append(new_sid)
            media_edit.trim_wav_file(source_audio, media_edit.wav_path(new_sid), start_sec, end_sec)
            if source_video.exists():
                new_offset = media_edit.trim_video_file(
                    source_video,
                    media_edit.video_path(new_sid),
                    start_sec,
                    end_sec,
                    source_video_offset,
                    ffmpeg_bin,
                )
                settings.put_video_offset(new_sid, new_offset)
            else:
                new_offset = 0.0
            threading.Thread(target=update_session_embedding, args=(new_sid,), daemon=True).start()
            results.append({
                "session_id": new_sid,
                "title": title,
                "duration": end_sec - start_sec,
                "video_offset": new_offset,
            })

        # Splitting one meeting into N parts produces N sessions, not N+1 —
        # the source is replaced by its parts. Default to True; clients can
        # opt out by sending {"delete_original": false}.
        if should_delete_original:
            # MUST snapshot the source before deleting it — this is the sole
            # rollback path for splits. Raise if it fails so we don't lose the
            # ability to undo.
            try:
                media_edit.create_split_backup(
                    group_id=group_id,
                    source_session_id=session_id,
                    source_session=source,
                    video_offset=source_video_offset,
                    part_session_ids=list(created),
                )
            except Exception as e:
                # Abort: undo everything and fail the request. Safer than
                # leaving the user with split parts and no rollback.
                raise RuntimeError(f"Could not snapshot original for rollback: {e}")
            storage.delete_session(session_id)
        return jsonify({
            "ok": True,
            "sessions": results,
            "split_group_id": group_id if should_delete_original else None,
        })
    except Exception as e:
        # Roll back every new part and any split backup we managed to write
        for sid in created:
            try:
                storage.delete_session(sid)
                vp = media_edit.video_path(sid)
                if vp.exists():
                    vp.unlink()
            except Exception:
                pass
        try:
            media_edit.clear_split_backup(group_id)
        except Exception:
            pass
        log.error("media", f"split failed for {session_id[:8]}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/sessions/<session_id>/split-info", methods=["GET"])
def split_info(session_id: str):
    """Report whether a split-rollback is available for this session.

    Returns the group id, the list of current sibling parts (so the UI can
    render the "delete these parts?" checklist), and the backup metadata
    (original title + timestamp) so the confirm dialog can show a meaningful
    summary. Returns ``has_backup: false`` if this session isn't part of a
    split group or the backup on disk has been deleted.
    """
    group_id = storage.get_session_split_group_id(session_id)
    if not group_id or not media_edit.has_split_backup(group_id):
        return jsonify({"has_backup": False})
    snapshot = media_edit.load_split_snapshot(group_id) or {}
    snap_session = snapshot.get("session") or {}
    return jsonify({
        "has_backup": True,
        "group_id": group_id,
        "original": {
            "title":      snap_session.get("title"),
            "started_at": snap_session.get("started_at"),
            "ended_at":   snap_session.get("ended_at"),
        },
        "members": storage.list_split_group_members(group_id),
    })


@app.route("/api/sessions/<session_id>/restore-split", methods=["POST"])
def restore_split(session_id: str):
    """Recreate the pre-split original session from its backup.

    Body: ``{"delete_session_ids": ["..."]}`` — part sessions to delete in the
    same transaction (typically all siblings). Any sibling not in this list
    stays alive as a standalone session (its ``split_group_id`` is cleared so
    the restore button disappears for it).

    Safety rails:
      - Every id in ``delete_session_ids`` MUST belong to the same split
        group as ``session_id``. We never delete arbitrary sessions.
      - Returns 409 if the backup has gone missing between the info fetch
        and the restore click.
      - Returns the new restored session id so the client can navigate to it.
    """
    with _state_lock:
        if _state["is_recording"]:
            active = _state["session_id"]
            group_id = storage.get_session_split_group_id(session_id)
            if active and group_id == storage.get_session_split_group_id(active):
                return jsonify({"error": "Cannot restore while a related session is recording"}), 400

    group_id = storage.get_session_split_group_id(session_id)
    if not group_id:
        return jsonify({"error": "This session is not part of a split group"}), 404
    if not media_edit.has_split_backup(group_id):
        return jsonify({"error": "Split backup is missing (already restored or manually deleted)"}), 409

    snapshot = media_edit.load_split_snapshot(group_id)
    if not snapshot:
        return jsonify({"error": "Split backup manifest could not be read"}), 500
    snap_session = snapshot.get("session") or {}

    # Validate the user-chosen delete list: every id must be in this split
    # group. Defensive — the UI already enforces it, but this is the API.
    members = storage.list_split_group_members(group_id)
    member_ids = {m["id"] for m in members}
    data = request.get_json(silent=True) or {}
    raw_delete = [str(x) for x in (data.get("delete_session_ids") or []) if x]
    delete_ids = [i for i in raw_delete if i in member_ids]
    invalid = [i for i in raw_delete if i not in member_ids]
    if invalid:
        return jsonify({"error": f"Session(s) not in this split group: {invalid}"}), 400

    keep_ids = [m["id"] for m in members if m["id"] not in delete_ids]

    # Create the restored session row. We use a FRESH uuid (the original id
    # is gone; reusing it is fraught because anything that referenced it by
    # path - screenshots, attachments - was cleaned by delete_session).
    restored_id = storage.create_session(
        title=snap_session.get("title") or "Restored Meeting",
        started_at=snap_session.get("started_at"),
        ended_at=snap_session.get("ended_at"),
    )
    try:
        # Populate DB state from the snapshot. restore_session_snapshot does
        # the full rehydration (segments, speakers, chat, summary, FTS).
        storage.restore_session_snapshot(restored_id, snap_session)
        # Folder assignment — restore to the original folder if any.
        orig_folder = snap_session.get("folder_id")
        if orig_folder:
            try:
                storage.set_session_folder(restored_id, orig_folder)
            except Exception:
                pass  # folder may have been deleted since split; non-fatal
        # Copy WAV/MP4 from the backup dir into the live media paths.
        media_edit.restore_split_media(group_id, restored_id)
        # Preserve the video offset if one was stored for the original.
        try:
            settings.put_video_offset(restored_id, float(snapshot.get("video_offset") or 0.0))
        except Exception:
            pass
        # Delete the user-selected parts (and their media) in one pass.
        for sid in delete_ids:
            try:
                storage.delete_session(sid)
            except Exception as e:
                log.error("split-restore", f"failed to delete part {sid[:8]}: {e}")
        # Detach any surviving parts from the group — they become standalone.
        if keep_ids:
            storage.clear_split_group_for_sessions(keep_ids)
        # Finally drop the backup (restore is one-shot).
        media_edit.clear_split_backup(group_id)
    except Exception as e:
        # Best-effort cleanup of the partially-restored session. The backup
        # is preserved so the user can try again.
        try:
            storage.delete_session(restored_id)
        except Exception:
            pass
        log.error("split-restore", f"restore failed for group {group_id}: {e}")
        return jsonify({"error": str(e)}), 500

    _push("session_title", {"session_id": restored_id, "title": snap_session.get("title") or ""})
    return jsonify({
        "ok": True,
        "restored_session_id": restored_id,
        "deleted_part_ids": delete_ids,
        "kept_part_ids": keep_ids,
    })


def _run_reanalysis(session_id: str, wav_path: str, custom_prompt: str) -> None:
    """Worker: clear DB data, retranscribe the WAV, then regenerate summary."""
    try:
        # Remove old session embeddings from Speaker Library and recompute centroids
        if fingerprint_db.ready:
            affected_ids = fingerprint_db.remove_session_embeddings(session_id)
            for gid in affected_ids:
                fingerprint_db.recompute_centroid(gid)
            log.info("reanalysis", f"Cleared {len(affected_ids)} speaker profiles' "
                     f"embeddings for session {session_id[:8]}")

        # Clear stored data (preserves session title/timestamps)
        storage.reset_session_transcript(session_id)

        # Reset in-memory state for this session
        with _state_lock:
            if _state["session_id"] == session_id:
                _state["segments"] = []
                # Keep summary and chat_history intact across reanalysis;
                # only the transcript is being recomputed.
                _state["pending_segments"] = 0
                _state["summarized_seg_count"] = 0
                _state["speaker_labels"] = {}
                # Reset fingerprint accumulators for fresh collection
                _state["speaker_audio_accum"] = {}
                _state["speaker_emb_counts"] = {}
                _state["speaker_offer_counts"] = {}
                _state["fingerprint_dismissals"] = {}
                _state["fingerprint_suggestions"] = {}
                _state["_confirmed_speakers"] = set()

        _push("reanalysis_start", {"session_id": session_id})
        _push("transcript_reset", {"session_id": session_id})

        # Run batch pipeline (transformers + pyannote) if available,
        # otherwise fall back to the real-time pipeline.
        try:
            from ml.batch_transcriber import BatchTranscriber
            from capture_audio.params import get_reanalysis_defaults, resolve_audio_params
            saved = settings.load().get("reanalysis_params", {})
            params = {**get_reanalysis_defaults(), **saved}

            # If "Use Live Diarization Settings" is on, derive batch
            # clustering threshold from the live delta_new value.
            # delta_new is a cosine-distance threshold (0-2) for the
            # streaming pipeline's online clustering.  The batch pipeline
            # uses agglomerative clustering with a different scale.
            # Map roughly: clustering_threshold ~ delta_new * 0.75,
            # clamped to [0.35, 0.75] to avoid extreme under/over-merge.
            if params.get("reanalysis_use_live_diarization"):
                live = resolve_audio_params()
                raw = live.get("delta_new", 0.5) * 0.75
                params["reanalysis_clustering_threshold"] = max(0.35, min(0.75, raw))

            batch = BatchTranscriber(
                on_text_callback=_on_segment,
                fingerprint_callback=_on_fingerprint_audio if fingerprint_db.ready else None,
                hf_token=os.getenv("HUGGING_FACE_KEY", ""),
                on_progress_callback=lambda pct: _push(
                    "reanalysis_progress",
                    {"session_id": session_id, "progress": pct},
                ),
            )
            batch.process_wav_file(wav_path, params)
        except ImportError as ie:
            log.warn("reanalysis", f"Batch pipeline unavailable ({ie}), "
                     f"falling back to real-time pipeline")
            _transcriber.process_wav_file(wav_path)

        _push("reanalysis_done", {"session_id": session_id})
        _obsidian_export_session(session_id)
    except Exception as e:
        log.error("reanalysis", f"{e}")
        import traceback; traceback.print_exc()
        _push("reanalysis_error", {"session_id": session_id, "error": str(e)})
    finally:
        with _state_lock:
            if _state["session_id"] == session_id:
                _state["is_reanalyzing"] = False


@app.route("/api/sessions/<session_id>/reanalyze", methods=["POST"])
def reanalyze_session(session_id: str):
    """Re-transcribe + re-summarize a session from its saved WAV file."""
    wav_path = paths.audio_dir() / f"{session_id}.wav"
    if not wav_path.exists():
        return jsonify({"error": "No audio recording for this session"}), 404

    with _state_lock:
        if _state["is_recording"]:
            return jsonify({"error": "Cannot reanalyze while recording"}), 400
        if _state.get("is_reanalyzing"):
            return jsonify({"error": "Reanalysis already in progress"}), 400
        # Batch reanalysis loads its own models; only require model_ready
        # if the batch pipeline is unavailable (fallback to real-time).
        try:
            from ml.batch_transcriber import BatchTranscriber  # noqa: F401
            _batch_available = True
        except ImportError:
            _batch_available = False
        if not _batch_available and not _state["model_ready"]:
            return jsonify({"error": "Transcription model not loaded yet"}), 503
        # Load the session into active state so _on_segment callbacks work
        sess = storage.get_session(session_id)
        if not sess:
            return jsonify({"error": "Session not found"}), 404
        _state["session_id"] = session_id
        _state["is_reanalyzing"] = True
        _state["segments"] = []
        _state["pending_segments"] = 0
        _state["summarized_seg_count"] = 0
        _state["speaker_labels"] = {}

    body = request.get_json(silent=True) or {}
    custom_prompt = body.get("custom_prompt", "")

    threading.Thread(
        target=_run_reanalysis,
        args=(session_id, str(wav_path), custom_prompt),
        daemon=True,
    ).start()
    return jsonify({"ok": True})


@app.route("/api/sessions/upload", methods=["POST"])
def upload_session():
    """Create a new session from an uploaded audio or video file."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400

    with _state_lock:
        if _state["is_recording"]:
            return jsonify({"error": "Cannot upload while recording"}), 400
        if _state.get("is_reanalyzing"):
            return jsonify({"error": "Reanalysis already in progress"}), 400

    # Create session
    session_id = storage.create_session()
    audio_dir = paths.audio_dir()
    audio_dir.mkdir(parents=True, exist_ok=True)
    wav_path = audio_dir / f"{session_id}.wav"

    # Save the uploaded file to a temp location
    import tempfile
    suffix = Path(f.filename).suffix.lower()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix,
                                      dir=str(audio_dir))
    try:
        f.save(tmp)
        tmp.close()
        tmp_path = tmp.name

        # Determine if this is a video file by probing with FFmpeg
        ffmpeg_bin = find_ffmpeg()
        if not ffmpeg_bin:
            os.unlink(tmp_path)
            storage.delete_session(session_id)
            return jsonify({"error": "FFmpeg not found – required for file processing"}), 500

        # Convert any audio/video to 16-bit 16kHz mono WAV for the pipeline
        cmd = [
            ffmpeg_bin, "-y", "-i", tmp_path,
            "-vn",                     # strip video
            "-acodec", "pcm_s16le",
            "-ar", "16000",
            "-ac", "1",
            str(wav_path),
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=600)
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace")[:500]
            os.unlink(tmp_path)
            storage.delete_session(session_id)
            return jsonify({"error": f"FFmpeg conversion failed: {stderr}"}), 500

        os.unlink(tmp_path)
    except Exception as exc:
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)
        storage.delete_session(session_id)
        return jsonify({"error": str(exc)}), 500

    # Set up state and launch reanalysis (same as normal reanalysis)
    with _state_lock:
        _state["session_id"] = session_id
        _state["is_reanalyzing"] = True
        _state["segments"] = []
        _state["pending_segments"] = 0
        _state["summarized_seg_count"] = 0
        _state["speaker_labels"] = {}

    threading.Thread(
        target=_run_reanalysis,
        args=(session_id, str(wav_path), ""),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "session_id": session_id}), 201


@app.route("/api/sessions/<session_id>", methods=["PATCH"])
def patch_session(session_id: str):
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title is required"}), 400
    # Any user-initiated PATCH locks the title so post-recording auto-gen
    # (and any future auto-title pass) won't clobber it.
    storage.update_session_title(session_id, title, user_set=True)
    _queue_obsidian_export(session_id)
    return jsonify({"ok": True})


# ── Folder endpoints ──────────────────────────────────────────────────────────

@app.route("/api/folders", methods=["GET"])
def list_folders():
    return jsonify(storage.list_folders())


@app.route("/api/folders", methods=["POST"])
def create_folder():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    parent_id = data.get("parent_id") or None
    fid = storage.create_folder(name, parent_id=parent_id)
    return jsonify({"ok": True, "id": fid}), 201


@app.route("/api/folders/<folder_id>", methods=["PATCH"])
def patch_folder(folder_id: str):
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if name:
        storage.rename_folder(folder_id, name)
    return jsonify({"ok": True})


@app.route("/api/folders/<folder_id>", methods=["DELETE"])
def delete_folder(folder_id: str):
    data = request.get_json(silent=True) or {}
    delete_contents = bool(data.get("delete_contents"))
    deleted_ids = storage.delete_folder(folder_id, delete_contents=delete_contents)
    # Clear active session state if it was deleted
    if deleted_ids:
        with _state_lock:
            if _state["session_id"] in deleted_ids and not _state["is_recording"]:
                _state["session_id"] = None
    return jsonify({"ok": True, "deleted_sessions": len(deleted_ids)})


@app.route("/api/reorder", methods=["POST"])
def reorder():
    """Batch-update sort order and parent/folder assignments."""
    data = request.get_json(silent=True) or {}
    storage.bulk_reorder(
        folders=data.get("folders"),
        sessions=data.get("sessions"),
    )
    return jsonify({"ok": True})


# ── Bulk session operations ────────────────────────────────────────────────────

@app.route("/api/sessions/bulk", methods=["POST"])
def bulk_sessions():
    """Bulk operations: delete, retitle, or move sessions to a folder.

    For ``action="retitle"`` the body may carry either ``session_ids`` (list)
    or ``folder_id`` (string — server resolves all nested sessions). Retitle
    work is fanned out across a small thread pool so a folder of N meetings
    finishes in roughly the time of a single LLM call rather than N×.
    """
    data = request.get_json(silent=True) or {}
    action      = (data.get("action") or "").strip()
    session_ids = [str(s) for s in (data.get("session_ids") or []) if s]
    folder_id   = data.get("folder_id")

    # Resolve folder_id → session_ids server-side so the client can't fall out
    # of sync with the actual folder membership.
    if action == "retitle" and folder_id and not session_ids:
        try:
            session_ids = storage.list_session_ids_in_folder(str(folder_id), recursive=True)
        except Exception as e:
            return jsonify({"error": f"folder lookup failed: {e}"}), 500

    if not session_ids:
        return jsonify({"error": "session_ids or folder_id required"}), 400

    if action == "delete":
        # Track split groups we touched so we can garbage-collect orphaned
        # split backups if we deleted the last surviving member.
        touched_groups = set()
        for sid in session_ids:
            gid = storage.get_session_split_group_id(sid)
            if gid:
                touched_groups.add(gid)
            storage.delete_session(sid)
            # Clear active session state if it was one of the deleted sessions
            with _state_lock:
                if _state["session_id"] == sid and not _state["is_recording"]:
                    _state["session_id"] = None
        for gid in touched_groups:
            try:
                if not storage.list_split_group_members(gid):
                    media_edit.clear_split_backup(gid)
            except Exception:
                pass
        return jsonify({"ok": True, "deleted": len(session_ids)})

    elif action == "retitle":
        return _bulk_retitle(session_ids)

    elif action == "move":
        folder_id = data.get("folder_id")  # None = uncategorize
        storage.bulk_set_folder(session_ids, folder_id or None)
        return jsonify({"ok": True})

    else:
        return jsonify({"error": f"Unknown action: {action!r}"}), 400


def _retitle_one(sid: str) -> dict | None:
    """Generate a fresh AI title for a single session and persist it.

    Designed to be called from a worker thread: each call gets its own SQLite
    connection (via the thread-local ``_conn`` context in storage), assembles
    the title-generation context independently, then commits the new title and
    fans out an SSE event so the sidebar refreshes live.
    """
    try:
        sess = storage.get_session(sid)
        if not sess:
            return None
        labels = sess.get("speaker_labels") or {}
        segs   = sess.get("segments") or []
        if not segs:
            return None
        transcript = _build_transcript(segs, labels)
        ctx = storage.get_title_generation_context(sid)
        title = ai.generate_title(
            transcript or " ".join(s["text"] for s in segs),
            context=ctx,
            system_prompt=settings.get("title_system_prompt") or None,
        )
        if not title:
            return None
        # Bulk retitle is an explicit user action → AI title replaces any
        # prior user-set lock (they're asking for a fresh AI pass).
        storage.update_session_title(sid, title, user_set=False)
        _push("session_title", {"session_id": sid, "title": title})
        return {"session_id": sid, "title": title}
    except Exception as e:
        log.error("retitle", f"failed for {sid[:8]}: {e}")
        return None


def _bulk_retitle(session_ids: list[str]):
    """Parallel-fan-out retitle for a list of sessions, returning JSON results.

    Workers fetch their own DB snapshots before the LLM call, so all workers
    see the same pre-batch state for the title-generation context (no
    cascading drift mid-batch). SSE events fire as each worker completes, so
    the sidebar updates titles incrementally even though the HTTP response
    waits for the full batch to finish.
    """
    if not session_ids:
        return jsonify({"ok": True, "updated": []})
    # Notify the client that work has started so it can show progress
    _push("retitle_start", {"count": len(session_ids), "session_ids": session_ids})
    results: list[dict] = []
    futures = [_retitle_executor.submit(_retitle_one, sid) for sid in session_ids]
    for fut in futures:
        try:
            r = fut.result(timeout=120)
        except Exception as e:
            log.error("retitle", f"worker failed: {e}")
            r = None
        if r:
            results.append(r)
    _push("retitle_done", {"requested": len(session_ids), "updated": len(results)})
    return jsonify({"ok": True, "updated": results, "requested": len(session_ids)})


# ── Import / Export ────────────────────────────────────────────────────────────

@app.route("/api/sessions/<session_id>/export", methods=["POST"])
def export_session(session_id: str):
    """Export a meeting session as a downloadable .zip package."""
    import io
    import zipfile

    data = request.get_json(silent=True) or {}
    include_raw = data.get("include")  # list of category names or None for all
    include = set(include_raw) if include_raw else None

    # Gather structured data from the database
    pkg = storage.export_session_data(session_id, include=include)
    if pkg is None:
        return jsonify({"error": "Session not found"}), 404

    # Build ZIP in memory
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        # Compact JSON (no whitespace) for smaller manifests
        zf.writestr("manifest.json", json.dumps(pkg, separators=(",", ":"), default=str))

        # Include media files if requested
        data_dir = paths.data_dir()

        include_audio = include is None or "audio" in (include or set())
        include_video = include is None or "video" in (include or set())

        if include_audio:
            wav = data_dir / "audio" / f"{session_id}.wav"
            if wav.exists():
                # Compress WAV → Opus for much smaller export (~8x smaller than FLAC)
                # Opus at 32kbps is excellent for speech; the app converts back to WAV on import
                ffmpeg_bin = find_ffmpeg()
                if ffmpeg_bin:
                    import tempfile
                    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
                        tmp_opus = tmp.name
                    try:
                        result = subprocess.run(
                            [ffmpeg_bin, "-y", "-i", str(wav),
                             "-c:a", "libopus", "-b:a", "32k",
                             "-vbr", "on", "-application", "voip",
                             tmp_opus],
                            capture_output=True, timeout=300,
                        )
                        if result.returncode == 0 and os.path.exists(tmp_opus):
                            # Store pre-compressed audio with ZIP_STORED (Opus is already compressed)
                            zf.write(tmp_opus, "audio.opus", compress_type=zipfile.ZIP_STORED)
                        else:
                            zf.write(str(wav), "audio.wav")  # fallback
                    finally:
                        if os.path.exists(tmp_opus):
                            os.unlink(tmp_opus)
                else:
                    zf.write(str(wav), "audio.wav")  # no ffmpeg fallback

        if include_video:
            mp4 = data_dir / "video" / f"{session_id}.mp4"
            if mp4.exists():
                # MP4 is already compressed; store without re-compressing
                zf.write(str(mp4), "video.mp4", compress_type=zipfile.ZIP_STORED)

        # Include screenshots for this session (chat tool captures)
        include_chat = include is None or "chat" in (include or set())
        if include_chat:
            ss_dir = data_dir / "screenshots" / session_id
            if ss_dir.is_dir():
                for img in ss_dir.iterdir():
                    if img.is_file():
                        # JPEG is already compressed
                        zf.write(str(img), f"screenshots/{img.name}", compress_type=zipfile.ZIP_STORED)

            # Include chat attachment files referenced in messages
            attach_dir = data_dir / "attachments"
            for msg in pkg.get("chat_messages", []):
                att_json = msg.get("attachments")
                if not att_json:
                    continue
                try:
                    atts = json.loads(att_json) if isinstance(att_json, str) else att_json
                except (json.JSONDecodeError, TypeError):
                    continue
                for att in (atts if isinstance(atts, list) else []):
                    stored = att.get("stored")
                    if stored and (attach_dir / stored).is_file():
                        zf.write(str(attach_dir / stored), f"attachments/{stored}")

        # Include notes attachments (images + dropped files referenced in the
        # rich-text Delta). Stored files are already in their final compressed
        # form (PNG / PDF / etc.), so use ZIP_STORED to avoid re-compressing.
        include_notes = include is None or "notes" in (include or set())
        if include_notes:
            notes_dir = data_dir / "notes" / session_id
            if notes_dir.is_dir():
                for f in notes_dir.iterdir():
                    if f.is_file():
                        zf.write(str(f), f"notes_attachments/{f.name}",
                                 compress_type=zipfile.ZIP_STORED)

    buf.seek(0)
    title = (pkg.get("metadata", {}).get("title") or "meeting").strip()
    safe_title = re.sub(r'[^\w\s\-]', '', title)[:60].strip().replace(' ', '_') or "meeting"
    filename = f"{safe_title}.zip"

    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=filename,
    )


def _dedup_import_title(pkg: dict) -> None:
    """If a session with the same title already exists, append (1), (2), etc."""
    meta = pkg.get("metadata")
    if not meta or not meta.get("title"):
        return
    base_title = meta["title"]
    existing_titles = {s["title"] for s in storage.list_sessions()}
    if base_title not in existing_titles:
        return
    # Strip existing " (N)" suffix to find the real base
    stripped = re.sub(r"\s*\(\d+\)$", "", base_title)
    n = 1
    while True:
        candidate = f"{stripped} ({n})"
        if candidate not in existing_titles:
            meta["title"] = candidate
            return
        n += 1


@app.route("/api/sessions/import", methods=["POST"])
def import_session():
    """Import a meeting session from an exported .mtga/.zip package."""
    import io
    import zipfile

    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400
    fname_lower = f.filename.lower()
    if not fname_lower.endswith(".mtga") and not fname_lower.endswith(".zip"):
        return jsonify({"error": "File must be a .mtga or .zip archive"}), 400

    try:
        file_bytes = f.read()
        if len(file_bytes) > 2 * 1024 * 1024 * 1024:  # 2 GB safety limit
            return jsonify({"error": "File too large (max 2 GB)"}), 400

        bio = io.BytesIO(file_bytes)
        if not zipfile.is_zipfile(bio):
            return jsonify({"error": "Invalid archive file"}), 400
        bio.seek(0)

        with zipfile.ZipFile(bio, "r") as zf:
            # Validate: must have manifest.json
            names = zf.namelist()
            if "manifest.json" not in names:
                return jsonify({"error": "Invalid export package: missing manifest.json"}), 400

            # Security: reject zips with path traversal
            for name in names:
                if name.startswith("/") or ".." in name:
                    return jsonify({"error": "Invalid archive: suspicious file paths"}), 400

            # Parse manifest
            manifest_bytes = zf.read("manifest.json")
            try:
                pkg = json.loads(manifest_bytes)
            except (json.JSONDecodeError, ValueError) as e:
                return jsonify({"error": f"Corrupt manifest: {e}"}), 400

            if not isinstance(pkg, dict) or pkg.get("format_version", 0) < 1:
                return jsonify({"error": "Unsupported export format version"}), 400

            # Deduplicate title - add (1), (2), etc. if a session with the same title exists
            _dedup_import_title(pkg)

            # Import into database
            new_session_id = storage.import_session_data(pkg)

            # Extract media files
            data_dir = paths.data_dir()

            # Audio: support Opus (current), FLAC (legacy v1), and raw WAV
            _audio_src = next(
                (n for n in ("audio.opus", "audio.flac", "audio.wav") if n in names),
                None,
            )
            if _audio_src:
                audio_dir = data_dir / "audio"
                audio_dir.mkdir(parents=True, exist_ok=True)
                wav_path = audio_dir / f"{new_session_id}.wav"

                if _audio_src == "audio.wav":
                    # Raw WAV - just copy
                    with zf.open(_audio_src) as src, open(str(wav_path), "wb") as dst:
                        import shutil
                        shutil.copyfileobj(src, dst)
                else:
                    # Compressed audio (Opus/FLAC) → convert back to 16kHz mono WAV
                    import tempfile
                    suffix = Path(_audio_src).suffix
                    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                        tmp_path = tmp.name
                    try:
                        with zf.open(_audio_src) as src, open(tmp_path, "wb") as dst:
                            import shutil
                            shutil.copyfileobj(src, dst)
                        ffmpeg_bin = find_ffmpeg()
                        if ffmpeg_bin:
                            result = subprocess.run(
                                [ffmpeg_bin, "-y", "-i", tmp_path,
                                 "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
                                 str(wav_path)],
                                capture_output=True, timeout=300,
                            )
                            if result.returncode != 0:
                                log.warn("import", f"{_audio_src}→WAV conversion failed")
                        else:
                            log.warn("import", "FFmpeg not found, cannot convert audio")
                    finally:
                        if os.path.exists(tmp_path):
                            os.unlink(tmp_path)

            if "video.mp4" in names:
                video_dir = data_dir / "video"
                video_dir.mkdir(parents=True, exist_ok=True)
                mp4_path = video_dir / f"{new_session_id}.mp4"
                with zf.open("video.mp4") as src, open(str(mp4_path), "wb") as dst:
                    import shutil
                    shutil.copyfileobj(src, dst)

            # Extract screenshots
            old_session_id = pkg.get("session_id", "")
            ss_prefix = "screenshots/"
            ss_files = [n for n in names if n.startswith(ss_prefix) and not n.endswith("/")]
            if ss_files:
                ss_out = data_dir / "screenshots" / new_session_id
                ss_out.mkdir(parents=True, exist_ok=True)
                for name in ss_files:
                    fname = name[len(ss_prefix):]
                    if fname and "/" not in fname:
                        with zf.open(name) as src, open(str(ss_out / fname), "wb") as dst:
                            import shutil
                            shutil.copyfileobj(src, dst)

            # Extract chat attachments
            att_prefix = "attachments/"
            att_files = [n for n in names if n.startswith(att_prefix) and not n.endswith("/")]
            if att_files:
                att_out = data_dir / "attachments"
                att_out.mkdir(parents=True, exist_ok=True)
                for name in att_files:
                    fname = name[len(att_prefix):]
                    if fname and "/" not in fname:
                        with zf.open(name) as src, open(str(att_out / fname), "wb") as dst:
                            import shutil
                            shutil.copyfileobj(src, dst)

            # Extract notes attachments — restore each file under the new
            # session's notes dir so the in-Delta URLs (after the rewrite
            # below) resolve.
            notes_prefix = "notes_attachments/"
            notes_files = [n for n in names if n.startswith(notes_prefix) and not n.endswith("/")]
            if notes_files:
                notes_out = data_dir / "notes" / new_session_id
                notes_out.mkdir(parents=True, exist_ok=True)
                for name in notes_files:
                    fname = name[len(notes_prefix):]
                    if fname and "/" not in fname:
                        with zf.open(name) as src, open(str(notes_out / fname), "wb") as dst:
                            import shutil
                            shutil.copyfileobj(src, dst)

            # Rewrite screenshot URLs in chat/summary AND notes-attachment
            # URLs in the notes Delta to point to the new session ID.
            if old_session_id and old_session_id != new_session_id:
                old_ss_prefix = f"/api/sessions/{old_session_id}/screenshots/"
                new_ss_prefix = f"/api/sessions/{new_session_id}/screenshots/"
                old_notes_prefix = f"/api/sessions/{old_session_id}/notes/attachments/"
                new_notes_prefix = f"/api/sessions/{new_session_id}/notes/attachments/"
                with storage._conn() as conn:
                    conn.execute(
                        "UPDATE chat_messages SET content = REPLACE(content, ?, ?) "
                        "WHERE session_id = ?",
                        (old_ss_prefix, new_ss_prefix, new_session_id),
                    )
                    conn.execute(
                        "UPDATE summaries SET content = REPLACE(content, ?, ?) "
                        "WHERE session_id = ?",
                        (old_ss_prefix, new_ss_prefix, new_session_id),
                    )
                    # Notes are stored as a JSON-serialized Quill Delta in
                    # sessions.notes; embed URLs are plain strings inside
                    # that JSON, so REPLACE at the column level is safe.
                    conn.execute(
                        "UPDATE sessions SET notes = REPLACE(notes, ?, ?) "
                        "WHERE id = ? AND notes IS NOT NULL",
                        (old_notes_prefix, new_notes_prefix, new_session_id),
                    )

            # Ingest speaker embeddings into the voice library if available
            embs = pkg.get("speaker_embeddings", [])
            if embs and fingerprint_db.ready:
                _import_speaker_embeddings(new_session_id, pkg)

    except zipfile.BadZipFile:
        return jsonify({"error": "Corrupt or invalid zip file"}), 400
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        log.error("import", f"Import failed: {e}")
        return jsonify({"error": f"Import failed: {e}"}), 500

    session = storage.get_session(new_session_id)
    return jsonify({
        "ok": True,
        "session_id": new_session_id,
        "title": session["title"] if session else "",
    }), 201


def _import_speaker_embeddings(session_id: str, pkg: dict) -> None:
    """Ingest exported speaker embeddings into the local voice library."""
    import base64
    embs = pkg.get("speaker_embeddings", [])
    if not embs or not fingerprint_db.ready:
        return

    for emb_data in embs:
        try:
            raw = base64.b64decode(emb_data["embedding_b64"])
            embedding = np.frombuffer(raw, dtype=np.float32).copy()
            speaker_key = emb_data["speaker_key"]
            duration = emb_data.get("duration_sec", 0.0)
            global_name = emb_data.get("global_name")
            global_color = emb_data.get("global_color")

            if not global_name:
                continue

            # Find or create a matching global speaker profile
            existing = fingerprint_db.find_by_name(global_name)
            if existing:
                global_id = existing["id"]
            else:
                global_id = fingerprint_db.create_global_speaker(
                    global_name, global_color
                )

            fingerprint_db.add_embedding(
                global_id, session_id, speaker_key, embedding, duration
            )

            # Link the session speaker label to this global profile
            with storage._conn() as conn:
                conn.execute(
                    "UPDATE speaker_labels SET global_id = ? "
                    "WHERE session_id = ? AND speaker_key = ?",
                    (global_id, session_id, speaker_key),
                )
        except Exception as e:
            log.warn("import", f"Failed to import embedding for {emb_data.get('speaker_key')}: {e}")


# ── Fingerprint / Voice Library endpoints ─────────────────────────────────────

def _fp_unavailable():
    return jsonify({"error": "Voice library not available (no HF key or model load failed)"}), 503


@app.route("/api/fingerprint/speakers", methods=["GET"])
def fp_list_speakers():
    return jsonify(fingerprint_db.list_global_speakers())


@app.route("/api/fingerprint/speakers", methods=["POST"])
def fp_create_speaker():
    if not fingerprint_db.ready:
        return _fp_unavailable()
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    try:
        color = _normalize_speaker_color(data.get("color"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    gid = fingerprint_db.create_global_speaker(name, color)
    return jsonify({"ok": True, "global_id": gid}), 201


@app.route("/api/fingerprint/speakers/<global_id>", methods=["PATCH"])
def fp_update_speaker(global_id: str):
    if not fingerprint_db.ready:
        return _fp_unavailable()
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    if name is not None:
        name = str(name).strip()
    try:
        color = _normalize_speaker_color(data.get("color")) if "color" in data else ...
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    resolved = fingerprint_db.rename_global_speaker(global_id, name=name or None, color=color)
    # Push SSE updates to all linked sessions
    if resolved:
        for label in fingerprint_db.get_linked_labels(global_id):
            sid = label["session_id"]
            with _state_lock:
                if _state.get("session_id") == sid:
                    _state["speaker_labels"][label["speaker_key"]] = resolved["name"]
            _push("speaker_label", {
                "session_id": sid, "speaker_key": label["speaker_key"],
                "name": resolved["name"], "color": resolved["color"],
            })
    return jsonify({"ok": True})


@app.route("/api/fingerprint/speakers/<global_id>", methods=["DELETE"])
def fp_delete_speaker(global_id: str):
    if not fingerprint_db.ready:
        return _fp_unavailable()
    fingerprint_db.delete_global_speaker(global_id)
    return jsonify({"ok": True})


@app.route("/api/fingerprint/speakers/<global_id>/merge", methods=["POST"])
def fp_merge_speaker(global_id: str):
    if not fingerprint_db.ready:
        return _fp_unavailable()
    data = request.get_json(silent=True) or {}
    source_id = (data.get("source_id") or "").strip()
    if not source_id:
        return jsonify({"error": "source_id is required"}), 400
    resolved = fingerprint_db.merge_global_speakers(keep_id=global_id, merge_id=source_id)
    # Push SSE updates to all linked sessions (including newly merged ones)
    if resolved:
        for label in fingerprint_db.get_linked_labels(global_id):
            sid = label["session_id"]
            with _state_lock:
                if _state.get("session_id") == sid:
                    _state["speaker_labels"][label["speaker_key"]] = resolved["name"]
            _push("speaker_label", {
                "session_id": sid, "speaker_key": label["speaker_key"],
                "name": resolved["name"], "color": resolved["color"],
            })
    return jsonify({"ok": True})


@app.route("/api/fingerprint/speakers/<global_id>/optimize", methods=["POST"])
def fp_optimize_speaker(global_id: str):
    if not fingerprint_db.ready:
        return _fp_unavailable()
    result = fingerprint_db.prune_embeddings(global_id)
    return jsonify({"ok": True, **result})


@app.route("/api/fingerprint/speakers/<global_id>/sessions", methods=["GET"])
def fp_speaker_sessions(global_id: str):
    sessions = fingerprint_db.get_profile_sessions(global_id)
    return jsonify(sessions)


@app.route("/api/fingerprint/speakers/bulk", methods=["DELETE"])
def fp_bulk_delete():
    if not fingerprint_db.ready:
        return _fp_unavailable()
    data = request.get_json(silent=True) or {}
    ids = data.get("ids", [])
    if not ids or not isinstance(ids, list):
        return jsonify({"error": "ids list is required"}), 400
    for gid in ids:
        fingerprint_db.delete_global_speaker(str(gid))
    return jsonify({"ok": True, "deleted": len(ids)})


@app.route("/api/fingerprint/speakers/bulk/optimize", methods=["POST"])
def fp_bulk_optimize():
    if not fingerprint_db.ready:
        return _fp_unavailable()
    data = request.get_json(silent=True) or {}
    ids = data.get("ids", [])
    if not ids or not isinstance(ids, list):
        return jsonify({"error": "ids list is required"}), 400
    for gid in ids:
        fingerprint_db.prune_embeddings(str(gid))
    return jsonify({"ok": True, "optimized": len(ids)})


@app.route("/api/fingerprint/unlinked-labels", methods=["GET"])
def fp_unlinked_labels():
    """Return distinct unlinked speaker names with session counts, plus profile list."""
    if not fingerprint_db.ready:
        return _fp_unavailable()
    groups = fingerprint_db.get_unlinked_speaker_groups()
    profiles = fingerprint_db.list_global_speakers()
    return jsonify({"groups": groups, "profiles": profiles})


@app.route("/api/fingerprint/unlinked-sessions", methods=["GET"])
def fp_unlinked_sessions():
    """Return sessions where a specific unlinked speaker name appears."""
    if not fingerprint_db.ready:
        return _fp_unavailable()
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "name param is required"}), 400
    sessions = fingerprint_db.get_unlinked_speaker_sessions(name)
    return jsonify({"sessions": sessions})


def _train_from_bulk_link(global_id: str, affected: list[dict], profile_name: str,
                          max_per_session: int = 3):
    """Background: extract voice embeddings from WAV files for newly linked labels.
    Skips sessions that already have embeddings for this profile+speaker_key,
    and only uses segments with healthy duration (≥ MIN_DURATION_SEC)."""
    audio_dir = paths.audio_dir()
    added_total = 0
    for label in affected:
        sid, key = label["session_id"], label["speaker_key"]
        # Skip if this session/speaker already has embeddings for this profile
        if fingerprint_db.get_latest_embedding(global_id, sid, key) is not None:
            continue
        wav_path = audio_dir / f"{sid}.wav"
        if not wav_path.exists():
            continue
        segments = storage.get_segments_by_speaker(sid, key)
        added = 0
        for seg in segments:
            if added >= max_per_session:
                break
            duration = seg["end_time"] - seg["start_time"]
            if duration < fingerprint_db.MIN_DURATION_SEC:
                continue
            emb = fingerprint_db.extract_embedding_from_wav(
                str(wav_path), seg["start_time"], seg["end_time"])
            if emb is not None:
                fingerprint_db.add_embedding(global_id, sid, key, emb, duration)
                added += 1
        added_total += added
    if added_total:
        log.info("fingerprint",
                 f"Bulk-link training: added {added_total} embeddings for {profile_name!r}")


@app.route("/api/fingerprint/bulk-link", methods=["POST"])
def fp_bulk_link():
    """Link all unlinked speaker_labels matching a name to a global profile."""
    if not fingerprint_db.ready:
        return _fp_unavailable()
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    global_id = (data.get("global_id") or "").strip()
    create_new = data.get("create_new", False)

    if not name:
        return jsonify({"error": "name is required"}), 400
    if not global_id and not create_new:
        return jsonify({"error": "global_id or create_new is required"}), 400

    if create_new:
        existing = fingerprint_db.find_by_name(name)
        if existing:
            global_id = existing["id"]
        else:
            global_id = fingerprint_db.create_global_speaker(name)

    affected = fingerprint_db.bulk_link_by_name(name, global_id)
    profile = fingerprint_db.get_global_speaker(global_id)

    # Push SSE events for all affected labels
    for label in affected:
        sid = label["session_id"]
        with _state_lock:
            if _state.get("session_id") == sid:
                _state["speaker_labels"][label["speaker_key"]] = profile["name"]
        _push("speaker_label", {
            "session_id": sid, "speaker_key": label["speaker_key"],
            "name": profile["name"], "color": profile.get("color"),
        })
        _push("speaker_linked", {
            "session_id": sid, "speaker_key": label["speaker_key"],
            "global_id": global_id, "name": profile["name"],
        })

    # Train voice fingerprint from WAV segments in background
    if affected:
        _fp_executor.submit(_train_from_bulk_link, global_id, affected, profile["name"])

    return jsonify({"ok": True, "linked_count": len(affected), "global_id": global_id})


@app.route("/api/fingerprint/bulk-link-all", methods=["POST"])
def fp_bulk_link_all():
    """Batch link multiple speaker names to global profiles."""
    if not fingerprint_db.ready:
        return _fp_unavailable()
    data = request.get_json(silent=True) or {}
    mappings = data.get("mappings", [])
    if not mappings or not isinstance(mappings, list):
        return jsonify({"error": "mappings list is required"}), 400

    total_linked = 0
    for mapping in mappings:
        name = (mapping.get("name") or "").strip()
        global_id = (mapping.get("global_id") or "").strip()
        create_new = mapping.get("create_new", False)
        if not name:
            continue
        if not global_id and not create_new:
            continue

        if create_new:
            existing = fingerprint_db.find_by_name(name)
            if existing:
                global_id = existing["id"]
            else:
                global_id = fingerprint_db.create_global_speaker(name)

        affected = fingerprint_db.bulk_link_by_name(name, global_id)
        profile = fingerprint_db.get_global_speaker(global_id)
        if not profile:
            continue

        for label in affected:
            sid = label["session_id"]
            with _state_lock:
                if _state.get("session_id") == sid:
                    _state["speaker_labels"][label["speaker_key"]] = profile["name"]
            _push("speaker_label", {
                "session_id": sid, "speaker_key": label["speaker_key"],
                "name": profile["name"], "color": profile.get("color"),
            })
            _push("speaker_linked", {
                "session_id": sid, "speaker_key": label["speaker_key"],
                "global_id": global_id, "name": profile["name"],
            })
        # Train voice fingerprint from WAV segments in background
        if affected:
            _fp_executor.submit(_train_from_bulk_link, global_id, affected, profile["name"])
        total_linked += len(affected)

    return jsonify({"ok": True, "total_linked": total_linked})


@app.route("/api/fingerprint/confirm", methods=["POST"])
def fp_confirm():
    """User accepted a fingerprint match suggestion."""
    data = request.get_json(silent=True) or {}
    session_id = (data.get("session_id") or "").strip()
    speaker_key = (data.get("speaker_key") or "").strip()
    global_id = (data.get("global_id") or "").strip()
    if not session_id or not speaker_key or not global_id:
        return jsonify({"error": "session_id, speaker_key, global_id required"}), 400

    profile = fingerprint_db.get_global_speaker(global_id)
    if not profile:
        return jsonify({"error": "Global speaker not found"}), 404

    name  = profile["name"]
    color = profile.get("color")

    # Link all speaker_keys in the active session that share the same display name
    with _state_lock:
        sid = _state.get("session_id")
        labels = dict(_state.get("speaker_labels", {}))

    current_name = labels.get(speaker_key, speaker_key)
    keys_to_link = [k for k, n in labels.items()
                    if n.lower() == current_name.lower() and not _is_custom_speaker_key(k)]
    if speaker_key not in keys_to_link:
        keys_to_link.append(speaker_key)

    for key in keys_to_link:
        fingerprint_db.link_session_speaker(session_id, key, global_id)
        storage.save_speaker_label(session_id, key, name=name, color=color)
        if sid == session_id:
            with _state_lock:
                _state["speaker_labels"][key] = name
        _push("speaker_label", {"session_id": session_id, "speaker_key": key,
                                 "name": name, "color": color})

    # Push linked event for badge indicators
    for key in keys_to_link:
        _push("speaker_linked", {
            "session_id": session_id, "speaker_key": key,
            "global_id": global_id, "name": name,
        })

    # Add embedding for this speaker_key from the latest stored embedding
    latest = fingerprint_db.get_latest_embedding(global_id, session_id, speaker_key)
    if latest is None:
        # Try to get one from accumulator if this is the active session
        with _state_lock:
            accum = _state.get("speaker_audio_accum", {})
            seg_audio = accum.get(speaker_key, {}).get("audio")
            seg_audio = seg_audio.copy() if seg_audio is not None else None

        if seg_audio is not None and len(seg_audio) > 0:
            def _add_emb():
                emb = fingerprint_db.extract_embedding(seg_audio)
                if emb is not None:
                    fingerprint_db.add_embedding(global_id, session_id, speaker_key, emb, 0.0)
            _fp_executor.submit(_add_emb)
        else:
            # Fallback: extract from WAV file
            wav_path = paths.audio_dir() / f"{session_id}.wav"
            if wav_path.exists():
                def _add_wav_embs():
                    segments = storage.get_segments_by_speaker(session_id, speaker_key)
                    added = 0
                    for seg in segments:
                        if added >= 3:
                            break
                        emb = fingerprint_db.extract_embedding_from_wav(
                            str(wav_path), seg["start_time"], seg["end_time"])
                        if emb is not None:
                            fingerprint_db.add_embedding(global_id, session_id, speaker_key, emb,
                                                         seg["end_time"] - seg["start_time"])
                            added += 1
                    if added:
                        log.info("fingerprint", f"Added {added} WAV embeddings on confirm for {name!r}")
                _fp_executor.submit(_add_wav_embs)

    # Remove from pending suggestions
    with _state_lock:
        for key in keys_to_link:
            _state["fingerprint_suggestions"].pop(key, None)

    log.info("fingerprint", f"Confirmed {name!r} for {speaker_key} in session {session_id[:8]}")
    return jsonify({"ok": True})


@app.route("/api/fingerprint/suggestions", methods=["GET"])
def fp_suggestions():
    """Return pending speaker suggestions for the active session."""
    with _state_lock:
        sid = _state.get("session_id")
        suggestions = list(_state.get("fingerprint_suggestions", {}).values())
    return jsonify({"session_id": sid, "suggestions": suggestions})


@app.route("/api/fingerprint/dismiss", methods=["POST"])
def fp_dismiss():
    """User dismissed a fingerprint match - suppress it for this session."""
    data = request.get_json(silent=True) or {}
    session_id  = (data.get("session_id") or "").strip()
    speaker_key = (data.get("speaker_key") or "").strip()
    global_id   = (data.get("global_id") or "").strip()  # optional
    if not session_id or not speaker_key:
        return jsonify({"error": "session_id and speaker_key required"}), 400

    with _state_lock:
        if _state.get("session_id") == session_id:
            dismissals = _state["fingerprint_dismissals"]
            if speaker_key not in dismissals:
                dismissals[speaker_key] = set()
            if global_id:
                dismissals[speaker_key].add(global_id)
            _state["fingerprint_suggestions"].pop(speaker_key, None)

    return jsonify({"ok": True})


@app.route("/api/fingerprint/sessions/<session_id>/links", methods=["GET"])
def fp_session_links(session_id: str):
    links = fingerprint_db.get_session_links(session_id)
    return jsonify(links)


@app.route("/api/fingerprint/sessions/<session_id>/link", methods=["POST"])
def fp_link_session_speaker(session_id: str):
    if not fingerprint_db.ready:
        return _fp_unavailable()
    data = request.get_json(silent=True) or {}
    speaker_key = (data.get("speaker_key") or "").strip()
    global_id   = (data.get("global_id") or "").strip()
    if not speaker_key or not global_id:
        return jsonify({"error": "speaker_key and global_id required"}), 400

    profile = fingerprint_db.get_global_speaker(global_id)
    if not profile:
        return jsonify({"error": "Global speaker not found"}), 404

    fingerprint_db.link_session_speaker(session_id, speaker_key, global_id)
    _push("speaker_linked", {"session_id": session_id, "speaker_key": speaker_key,
                              "global_id": global_id, "name": profile["name"]})
    # Optionally apply the global name/color to this session speaker
    if data.get("apply_name"):
        storage.save_speaker_label(session_id, speaker_key, name=profile["name"], color=profile.get("color"))
        with _state_lock:
            if _state.get("session_id") == session_id:
                _state["speaker_labels"][speaker_key] = profile["name"]
        _push("speaker_label", {"session_id": session_id, "speaker_key": speaker_key,
                                 "name": profile["name"], "color": profile.get("color")})

    return jsonify({"ok": True})


@app.route("/api/fingerprint/sessions/<session_id>/link/<speaker_key>", methods=["DELETE"])
def fp_unlink_session_speaker(session_id: str, speaker_key: str):
    fingerprint_db.unlink_session_speaker(session_id, speaker_key)
    return jsonify({"ok": True})


def _force_quit(delay: float = 0) -> None:
    """Stop any active recording/test, clean up resources, and exit immediately.

    Safe to call from a signal handler: uses a non-blocking lock acquire so it
    cannot deadlock if the lock is already held on the interrupted thread.
    """
    global _tray
    if delay:
        time.sleep(delay)
    got_lock = _state_lock.acquire(timeout=2)
    try:
        sid      = _state.get("session_id")
        capture  = _state.get("audio_capture")
        test_cap = _state.get("test_capture")
        _state["is_recording"]  = False
        _state["is_testing"]    = False
        _state["audio_capture"] = None
        _state["test_capture"]  = None
    finally:
        if got_lock:
            _state_lock.release()
    try:
        if test_cap:
            test_cap.stop()
    except Exception:
        pass
    try:
        if capture:
            capture.stop()
    except Exception:
        pass
    try:
        _transcriber.stop()
    except Exception:
        pass
    if sid:
        try:
            storage.end_session(sid)
        except Exception:
            pass
    if _tray is not None:
        try:
            _tray.stop()
        except Exception:
            pass
        _tray = None
    os._exit(0)


@app.route("/api/instance-handshake", methods=["POST"])
def instance_handshake():
    """Called by a new instance to check if it can take over."""
    with _state_lock:
        recording = _state["is_recording"]
    if recording:
        log.warn("app", "New instance attempted takeover — declined (recording active)")
    else:
        log.info("app", "New instance requested takeover — yielding (idle)")
    return jsonify({"recording": recording})


@app.route("/api/shutdown", methods=["POST"])
def shutdown():
    """Gracefully stop recording (if active), remove tray, then exit."""
    # Small delay so the HTTP response reaches the browser before we exit.
    threading.Thread(target=_force_quit, args=(0.4,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/restart", methods=["POST"])
def restart():
    """Gracefully stop everything, then relaunch via Start Menu shortcut."""
    def _do_restart() -> None:
        global _tray
        with _state_lock:
            sid      = _state["session_id"]
            capture  = _state["audio_capture"]
            test_cap = _state["test_capture"]
            _state["is_recording"] = False
            _state["is_testing"]   = False
            _state["audio_capture"] = None
            _state["test_capture"]  = None
        if test_cap:
            test_cap.stop()
        if capture:
            capture.stop()
        _transcriber.stop()
        if sid:
            storage.end_session(sid)
        time.sleep(0.5)

        root = Path(__file__).parent
        lnk_path = (
            Path(os.environ.get("APPDATA", ""))
            / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            / "Meeting Assistant.lnk"
        )
        if lnk_path.exists():
            os.startfile(str(lnk_path))
        else:
            bat = root / "launch.bat"
            if bat.exists():
                subprocess.Popen(
                    ["cmd.exe", "/c", str(bat)],
                    cwd=str(root),
                    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_CONSOLE,
                )

        if _tray is not None:
            _tray.stop()
            _tray = None
        os._exit(0)

    threading.Thread(target=_do_restart, daemon=True).start()
    return jsonify({"ok": True})


# ── Changelog ────────────────────────────────────────────────────────────────

_CHANGELOG_CACHE_NAME = "changelog.json"
_CHANGELOG_MAX_COMMITS = 200


def _changelog_category(subject: str) -> str:
    """Crude categorization based on the first word of the commit subject.
    Drives the icon + accent color in the UI; not user-editable.

    Subject convention is past tense ("Added", "Fixed"). Imperative forms
    ("Add", "Fix") are also accepted so commits from before that
    convention landed still get their icons. Leading non-letter chars are
    stripped defensively before matching."""
    s = subject.strip().lower()
    s = re.sub(r"^[^\w]+", "", s).lstrip()
    # Fix
    if s.startswith((
        "fixed ", "fix ", "fix:", "bug ", "bug:",
        "guarded ", "guard ", "hardened ", "harden ",
    )) or s.startswith(("fix-", "self-heal")):
        return "fix"
    # Feature
    if s.startswith((
        "added ", "add ", "add:", "new ", "new:",
        "created ", "create ", "built ", "build ",
    )):
        return "feature"
    # Refactor
    if s.startswith((
        "refactored", "refactor",
        "rewrote", "rewrite",
        "restructured", "restructure",
        "reorganized", "reorganize",
        "consolidated", "consolidate",
    )):
        return "refactor"
    # Improvement
    if s.startswith((
        "updated", "update",
        "improved", "improve",
        "enhanced", "enhance",
        "polished", "polish",
        "tightened", "tighten",
        "tuned ", "tune ",
        "reworked", "rework",
        "replaced", "replace",
        "switched", "switch",
        "made ", "make ",
    )):
        return "improvement"
    # Removal
    if s.startswith((
        "removed", "remove",
        "dropped ", "drop ",
        "killed ", "kill ",
        "stripped ", "strip ",
    )):
        return "removal"
    return "other"


def _build_changelog(root: Path) -> dict:
    """Run ``git log`` and parse it into a structured payload. Caller is
    responsible for caching."""
    from datetime import datetime as _dt
    SEP = "\x1e"
    FIELD_SEP = "\x1f"
    fmt = FIELD_SEP.join(["%H", "%h", "%ad", "%s", "%b"]) + SEP
    log = subprocess.run(
        ["git", "log", f"--pretty=format:{fmt}",
         "--date=short", "--no-merges", "-n", str(_CHANGELOG_MAX_COMMITS)],
        cwd=str(root), capture_output=True, text=True, timeout=10,
        encoding="utf-8",
    )
    if log.returncode != 0:
        raise RuntimeError(log.stderr.strip() or "git log failed")

    commits = []
    # Drop trailers we don't want surfaced (Co-author signatures, generated-with
    # footers). Defensive — these shouldn't normally land in commits per repo
    # policy, but old commits may carry them.
    skip_substrings = (
        "co-authored-by:",
        "co-author-by:",
        "🤖 generated with",
        "generated with [claude",
    )
    for raw in log.stdout.split(SEP):
        raw = raw.strip()
        if not raw:
            continue
        parts = raw.split(FIELD_SEP)
        if len(parts) < 5:
            continue
        h, sh, date, subject, body = parts[0], parts[1], parts[2], parts[3], parts[4]
        body_lines = []
        for line in body.splitlines():
            if any(s in line.lower() for s in skip_substrings):
                continue
            body_lines.append(line)
        body_clean = "\n".join(body_lines).strip()
        commits.append({
            "hash":     h,
            "short":    sh,
            "date":     date,
            "subject":  subject.strip(),
            "body":     body_clean,
            "category": _changelog_category(subject),
        })

    head_hash = commits[0]["hash"] if commits else ""
    return {
        "head":         head_hash,
        "generated_at": _dt.utcnow().isoformat(),
        "count":        len(commits),
        "commits":      commits,
    }


@app.route("/api/changelog")
def api_changelog():
    """Return a parsed, locally-cached changelog from git history.

    The cache is keyed by HEAD; if HEAD hasn't moved since the last build,
    git is not invoked and the cached payload is served. Pass ``?refresh=1``
    to force a rebuild (used by the "Refresh" button on the Changelog tab).
    """
    root = Path(__file__).parent
    cache_path = paths.data_dir() / _CHANGELOG_CACHE_NAME
    refresh = bool(request.args.get("refresh"))

    # Resolve current HEAD cheaply so we can short-circuit when cache is fresh.
    try:
        rev = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root), capture_output=True, text=True, timeout=5,
        )
        if rev.returncode != 0:
            return jsonify({"error": "Not a git repository"}), 500
        head_hash = rev.stdout.strip()
    except FileNotFoundError:
        return jsonify({"error": "git not available on this machine"}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"error": "git rev-parse timed out"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if not refresh and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("head") == head_hash:
                cached["fresh"] = False
                return jsonify(cached)
        except (OSError, json.JSONDecodeError):
            pass

    try:
        payload = _build_changelog(root)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "git log timed out"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as e:
        log.warn("changelog", f"Failed to write cache: {e}")

    payload["fresh"] = True
    return jsonify(payload)


# ── Update / self-update ──────────────────────────────────────────────────────

_UPDATE_REMOTES = [
    "origin",                                                  # Azure DevOps (primary)
    "https://github.com/TyLaneTech/Meeting-Assistant.git",     # GitHub (fallback)
]


def _git_fetch(root: Path) -> tuple[bool, str, str]:
    """Try fetching from each remote in _UPDATE_REMOTES; return (ok, remote_used, error)."""
    from core.network import warp_reconnect
    warp_reconnect()
    last_err = ""
    for remote in _UPDATE_REMOTES:
        fetch = subprocess.run(
            ["git", "fetch", remote, "main"],
            cwd=str(root), capture_output=True, text=True, timeout=20,
        )
        if fetch.returncode == 0:
            return True, remote, ""
        last_err = fetch.stderr.strip() or "git fetch failed"
    return False, "", last_err


@app.route("/api/update/check")
def update_check():
    """Fetch from origin and report whether the remote main branch is ahead."""
    root = Path(__file__).parent
    try:
        ok, remote, err = _git_fetch(root)
        if not ok:
            return jsonify({"error": err}), 500

        count_r = subprocess.run(
            ["git", "rev-list", "HEAD..FETCH_HEAD", "--count"],
            cwd=str(root), capture_output=True, text=True, timeout=5,
        )
        if count_r.returncode != 0:
            return jsonify({"error": "Could not compare branches"}), 500

        count = int(count_r.stdout.strip() or "0")
        return jsonify({"up_to_date": count == 0, "commits_behind": count})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out - check your connection"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/update/apply", methods=["POST"])
def update_apply():
    """Pull latest changes then restart via the Start Menu shortcut."""
    root = Path(__file__).parent

    # Fetch first so we know which remote is reachable
    ok, remote, err = _git_fetch(root)
    if not ok:
        return jsonify({"error": err}), 500

    pull = subprocess.run(
        ["git", "pull", remote, "main"],
        cwd=str(root), capture_output=True, text=True, timeout=120,
    )
    if pull.returncode != 0:
        return jsonify({"error": pull.stderr.strip() or "git pull failed"}), 500

    def _restart() -> None:
        global _tray
        # Stop any active recording / test first (mirrors _do_shutdown)
        with _state_lock:
            sid      = _state["session_id"]
            capture  = _state["audio_capture"]
            test_cap = _state["test_capture"]
            _state["is_recording"] = False
            _state["is_testing"]   = False
            _state["audio_capture"] = None
            _state["test_capture"]  = None
        if test_cap:
            test_cap.stop()
        if capture:
            capture.stop()
        _transcriber.stop()
        if sid:
            storage.end_session(sid)
        time.sleep(0.5)  # let the HTTP response reach the browser

        # Prefer the hidden startup shortcut (no console window) when the user
        # has launch-at-login enabled; otherwise the Start Menu shortcut so
        # the experience matches a normal start.
        startup_lnk = _startup_lnk_path()
        lnk_path = startup_lnk if startup_lnk.exists() else (
            Path(os.environ.get("APPDATA", ""))
            / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            / "Meeting Assistant.lnk"
        )
        if lnk_path.exists():
            os.startfile(str(lnk_path))
        else:
            # Fallback: run launch.bat directly
            bat = root / "launch.bat"
            if bat.exists():
                subprocess.Popen(
                    ["cmd.exe", "/c", str(bat)],
                    cwd=str(root),
                    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_CONSOLE,
                )

        if _tray is not None:
            _tray.stop()
            _tray = None
        os._exit(0)

    threading.Thread(target=_restart, daemon=True).start()
    return jsonify({"ok": True})


# ── Entry point ───────────────────────────────────────────────────────────────

def _handshake_existing_instance(url: str) -> bool:
    """Check for an existing instance and negotiate takeover.

    Returns True if startup should continue, False if we must abort.
    """
    import urllib.request
    try:
        req = urllib.request.Request(
            f"{url}/api/instance-handshake", data=b"{}",
            headers={"Content-Type": "application/json"}, method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=3)
        data = json.loads(resp.read())
    except Exception:
        return True  # nothing listening — port is free

    if data.get("recording"):
        log.error("app", "Another instance is running and has an active recording. "
                         "Aborting to avoid interrupting it.")
        print("\n  *** Another Meeting Assistant instance is recording on this port. ***")
        print("  *** Stop the recording first, or shut down the other instance.   ***\n")
        return False

    # Existing instance is idle — ask it to shut down
    log.info("app", "Idle instance detected on this port — requesting shutdown…")
    try:
        req = urllib.request.Request(
            f"{url}/api/shutdown", data=b"{}",
            headers={"Content-Type": "application/json"}, method="POST",
        )
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        pass  # may fail if it exits before responding — that's fine

    # Wait for the port to free up
    for _ in range(30):
        time.sleep(0.3)
        try:
            urllib.request.urlopen(f"{url}/api/status", timeout=1)
        except Exception:
            log.info("app", "Previous instance shut down.")
            return True

    log.error("app", "Previous instance did not shut down in time. Aborting.")
    return False


def main() -> None:
    global _tray, _server_url

    kill_stale_ffmpeg()

    port = int(os.getenv("PORT", 6969))
    # Bind to 127.0.0.1 (loopback only — never expose externally), but advertise
    # the URL as ``localhost`` so the browser/tray see a friendly hostname.
    url = f"http://localhost:{port}"
    _server_url = url

    if not _handshake_existing_instance(url):
        sys.exit(1)

    _active_provider = settings.get("ai_provider", "openai")

    if config.needs_setup(_active_provider):
        log.warn("app", "First-run setup required - browser will open to configure API keys.")
    log.info("app", f"Meeting Assistant starting at {url}")

    # Start Flask in a daemon thread so the main thread is free for the tray
    flask_thread = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, debug=False, threaded=True),
        daemon=True,
    )
    flask_thread.start()

    # Try to start system tray immediately so it becomes available during the
    # same startup window as the webserver, rather than after the bind wait.
    try:
        from ui_desktop.tray import TRAY_AVAILABLE, MeetingTray
        if not TRAY_AVAILABLE:
            raise ImportError("pystray or Pillow not installed")

        def _state_snapshot() -> dict:
            snap = _status_payload()
            with _state_lock:
                snap.update({**_state})
            snap["ai_provider"] = settings.get("ai_provider", "openai")
            return snap

        def _on_tray_quit(icon) -> None:
            if icon:
                try:
                    icon.stop()
                except Exception:
                    pass
            _force_quit()

        _tray = MeetingTray(url, _state_snapshot, _on_tray_quit)
        log.info("tray", "System tray active - right-click for menu.")
        # Run tray in a daemon thread so the main thread stays in Python code
        # where it can receive signals (Ctrl+C).  pystray's Win32 message loop
        # blocks in native C, which prevents Python signal handlers from firing.
        threading.Thread(target=_tray.run, daemon=True).start()

    except ImportError:
        log.warn("tray", "pystray/Pillow not installed - running without system tray.")
        log.warn("tray", "Install with: pip install pystray Pillow")

    # Wait for Flask to bind
    import urllib.request
    for _ in range(40):
        try:
            urllib.request.urlopen(f"{url}/api/status", timeout=1)
            break
        except Exception:
            time.sleep(0.15)

    # Defer heavy model and embedding loads until after the server is accepting
    # requests so the UI can render immediately and show startup progress.
    _start_background_initializers()

    # Open browser - go to settings page if keys are missing
    if config.needs_setup(_active_provider):
        webbrowser.open(f"{url}?settings=1")
    #else: webbrowser.open(url)

    # Register SIGINT after Flask starts (werkzeug would override an earlier handler).
    # This ensures Ctrl+C in the console immediately stops recording and exits.
    signal.signal(signal.SIGINT, lambda *_: _force_quit())

    # Keep the main thread alive in a Python-level loop so signal handlers
    # (Ctrl+C) can fire.  threading.Event.wait() with a timeout releases the
    # GIL and lets the interpreter check for pending signals each iteration.
    try:
        _shutdown_event = threading.Event()
        while not _shutdown_event.wait(timeout=1):
            pass
    except KeyboardInterrupt:
        _force_quit()


if __name__ == "__main__":
    main()
