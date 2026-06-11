"""Audio/video profile, trim, and split helpers."""
from __future__ import annotations

import json
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np

from core import paths as paths


def __getattr__(name):
    """Expose data directories as live properties so callers always see
    the current data folder, even after a runtime migration."""
    if name == "DATA_DIR":
        return paths.data_dir()
    if name == "AUDIO_DIR":
        return paths.audio_dir()
    if name == "VIDEO_DIR":
        return paths.video_dir()
    if name == "PROFILE_DIR":
        return paths.profile_dir()
    if name == "BACKUP_DIR":
        return paths.backup_dir()
    if name == "TMP_DIR":
        return paths.tmp_dir()
    if name == "ROOT":
        return Path(__file__).parent
    raise AttributeError(name)


def wav_path(session_id: str) -> Path:
    return paths.audio_dir() / f"{session_id}.wav"


def video_path(session_id: str) -> Path:
    return paths.video_dir() / f"{session_id}.mp4"


def backup_dir(session_id: str) -> Path:
    return paths.backup_dir() / session_id


def session_snapshot_path(session_id: str) -> Path:
    return backup_dir(session_id) / "session-original.json"


def get_wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as wf:
        rate = wf.getframerate()
        if rate <= 0:
            return 0.0
        return wf.getnframes() / rate


def _read_wav_mono_float(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())

    if sampwidth == 2:
        arr = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    elif sampwidth == 4:
        arr = np.frombuffer(frames, dtype=np.int32).astype(np.float32) / 2147483648.0
    elif sampwidth == 1:
        arr = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError(f"Unsupported WAV sample width: {sampwidth}")

    if channels > 1:
        arr = arr.reshape(-1, channels).mean(axis=1)
    return arr, rate


def build_audio_profile(
    session_id: str,
    bins: int,
    segments: list[dict],
    speaker_profiles: list[dict],
    quiet_threshold: float = 0.006,
    min_quiet_sec: float = 30.0,
) -> dict:
    path = wav_path(session_id)
    if not path.exists():
        raise FileNotFoundError("Audio not found")
    bins = max(50, min(int(bins or 1200), 5000))
    stat = path.stat()
    cache = paths.profile_dir() / f"{session_id}.{int(stat.st_mtime)}.{stat.st_size}.{bins}.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass

    audio, rate = _read_wav_mono_float(path)
    duration = len(audio) / rate if rate else 0.0
    n = len(audio)
    bin_count = min(bins, max(1, n))
    edges = np.linspace(0, n, bin_count + 1, dtype=np.int64)
    out_bins = []
    quiet_bins = []
    for i in range(bin_count):
        start = int(edges[i])
        end = int(edges[i + 1])
        chunk = audio[start:end]
        if len(chunk) == 0:
            peak = rms = 0.0
        else:
            peak = float(np.max(np.abs(chunk)))
            rms = float(np.sqrt(np.mean(chunk * chunk)))
        t0 = start / rate if rate else 0.0
        t1 = end / rate if rate else 0.0
        out_bins.append({"t0": t0, "t1": t1, "peak": peak, "rms": rms})
        quiet_bins.append(rms < quiet_threshold)

    profile_by_key = {p["speaker_key"]: p for p in speaker_profiles}
    profile_segments = []
    for seg in segments:
        key = seg.get("source_override") or seg.get("source")
        profile = profile_by_key.get(key, {})
        profile_segments.append({
            "id": seg.get("id"),
            "start": float(seg.get("start_time") or 0.0),
            "end": float(seg.get("end_time") or 0.0),
            "speaker": key,
            "label": seg.get("label_override") or profile.get("name") or key,
            "color": profile.get("color"),
        })

    quiet_spans = _detect_quiet_spans(out_bins, quiet_bins, profile_segments, min_quiet_sec)
    result = {
        "duration": duration,
        "sample_rate": rate,
        "bins": out_bins,
        "segments": profile_segments,
        "quiet_spans": quiet_spans,
    }
    paths.profile_dir().mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(result), encoding="utf-8")
    return result


def _detect_quiet_spans(
    bins: list[dict],
    quiet_bins: list[bool],
    segments: list[dict],
    min_quiet_sec: float,
) -> list[dict]:
    spans: list[dict] = []
    start = None
    for i, is_quiet in enumerate(quiet_bins):
        if is_quiet and start is None:
            start = bins[i]["t0"]
        if (not is_quiet or i == len(quiet_bins) - 1) and start is not None:
            end = bins[i]["t1"] if is_quiet and i == len(quiet_bins) - 1 else bins[i]["t0"]
            if end - start >= min_quiet_sec:
                spans.append({"start": start, "end": end, "reason": "low_audio"})
            start = None

    sorted_segments = sorted(segments, key=lambda s: s["start"])
    for prev, nxt in zip(sorted_segments, sorted_segments[1:]):
        gap_start = float(prev.get("end") or 0.0)
        gap_end = float(nxt.get("start") or 0.0)
        if gap_end - gap_start >= min_quiet_sec:
            spans.append({"start": gap_start, "end": gap_end, "reason": "no_transcript"})

    if sorted_segments and bins:
        tail_start = float(sorted_segments[-1].get("end") or 0.0)
        tail_end = float(bins[-1]["t1"])
        if tail_end - tail_start >= min_quiet_sec:
            spans.append({"start": tail_start, "end": tail_end, "reason": "trailing_no_transcript"})

    spans.sort(key=lambda s: (s["start"], s["end"]))
    merged: list[dict] = []
    for span in spans:
        if merged and span["start"] <= merged[-1]["end"] + 1.0:
            merged[-1]["end"] = max(merged[-1]["end"], span["end"])
            if span["reason"] not in merged[-1]["reason"]:
                merged[-1]["reason"] += f"+{span['reason']}"
        else:
            merged.append(span)
    return merged


def backup_original_media(session_id: str) -> None:
    target = backup_dir(session_id)
    target.mkdir(parents=True, exist_ok=True)
    src_audio = wav_path(session_id)
    if src_audio.exists():
        dst = target / "audio-original.wav"
        if not dst.exists():
            shutil.copy2(src_audio, dst)
    src_video = video_path(session_id)
    if src_video.exists():
        dst = target / "video-original.mp4"
        if not dst.exists():
            shutil.copy2(src_video, dst)


def backup_session_snapshot(session_id: str, session: dict, video_offset: float) -> None:
    target = backup_dir(session_id)
    target.mkdir(parents=True, exist_ok=True)
    snapshot = session_snapshot_path(session_id)
    if snapshot.exists():
        return
    payload = {
        "session": {
            "title": session.get("title", ""),
            "started_at": session.get("started_at"),
            "ended_at": session.get("ended_at"),
            "summary": session.get("summary", ""),
            "chat_messages": session.get("chat_messages", []),
            "segments": session.get("segments", []),
            "speaker_profiles": session.get("speaker_profiles", []),
        },
        "video_offset": float(video_offset or 0.0),
    }
    snapshot.write_text(json.dumps(payload), encoding="utf-8")


def load_session_snapshot(session_id: str) -> dict | None:
    snapshot = session_snapshot_path(session_id)
    if not snapshot.exists():
        return None
    return json.loads(snapshot.read_text(encoding="utf-8"))


def has_trim_backup(session_id: str) -> bool:
    return session_snapshot_path(session_id).exists()


def restore_original_media(session_id: str) -> None:
    target = backup_dir(session_id)
    src_audio = target / "audio-original.wav"
    if not src_audio.exists():
        raise FileNotFoundError("No original audio backup found for this session")
    shutil.copy2(src_audio, wav_path(session_id))

    src_video = target / "video-original.mp4"
    dst_video = video_path(session_id)
    if src_video.exists():
        dst_video.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_video, dst_video)
    elif dst_video.exists():
        dst_video.unlink()


def clear_trim_backup(session_id: str) -> None:
    target = backup_dir(session_id)
    if target.exists():
        shutil.rmtree(target)


# ── Split rollback ────────────────────────────────────────────────────────────
# Keyed by a split-group UUID (not a session id) because the source session is
# deleted immediately after the split completes. The group id is written into
# every resulting part's ``sessions.split_group_id`` column so any part can
# look up the backup later.

def split_backup_dir(group_id: str) -> Path:
    return paths.backup_dir() / f"split-{group_id}"


def split_snapshot_path(group_id: str) -> Path:
    return split_backup_dir(group_id) / "pre-split.json"


def split_audio_backup_path(group_id: str) -> Path:
    return split_backup_dir(group_id) / "audio-original.wav"


def split_video_backup_path(group_id: str) -> Path:
    return split_backup_dir(group_id) / "video-original.mp4"


def has_split_backup(group_id: str | None) -> bool:
    return bool(group_id) and split_snapshot_path(group_id).exists()


def create_split_backup(
    group_id: str,
    source_session_id: str,
    source_session: dict,
    video_offset: float,
    part_session_ids: list[str],
) -> None:
    """Snapshot a session and its media immediately before a split deletes it.

    Writes:
      - ``pre-split.json``       : full session payload (title, timestamps,
                                   segments, chat, speakers, summary, folder
                                   membership, etc.)
      - ``audio-original.wav``   : copy of the pre-split WAV
      - ``video-original.mp4``   : copy of the pre-split MP4 (if any)

    All three writes are fatal on failure — the caller must not proceed with
    the destructive ``delete_session`` if this raises.
    """
    target = split_backup_dir(group_id)
    target.mkdir(parents=True, exist_ok=True)

    payload = {
        "source_session_id": source_session_id,
        "group_id": group_id,
        "part_session_ids": list(part_session_ids or []),
        "video_offset": float(video_offset or 0.0),
        "session": {
            "title":            source_session.get("title", ""),
            "started_at":       source_session.get("started_at"),
            "ended_at":         source_session.get("ended_at"),
            "summary":          source_session.get("summary", ""),
            "folder_id":        source_session.get("folder_id"),
            "chat_messages":    source_session.get("chat_messages", []),
            "segments":         source_session.get("segments", []),
            "speaker_profiles": source_session.get("speaker_profiles", []),
        },
    }
    # Atomic-ish: write snapshot to a temp file and rename into place so we
    # never leave a half-written JSON behind if the process crashes mid-write.
    tmp = split_snapshot_path(group_id).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(split_snapshot_path(group_id))

    src_audio = wav_path(source_session_id)
    if src_audio.exists():
        shutil.copy2(src_audio, split_audio_backup_path(group_id))
    src_video = video_path(source_session_id)
    if src_video.exists():
        shutil.copy2(src_video, split_video_backup_path(group_id))


def load_split_snapshot(group_id: str) -> dict | None:
    path = split_snapshot_path(group_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def restore_split_media(group_id: str, target_session_id: str) -> None:
    """Copy the backed-up WAV/MP4 into ``target_session_id``'s live media paths."""
    src_audio = split_audio_backup_path(group_id)
    if src_audio.exists():
        dst = wav_path(target_session_id)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_audio, dst)
    src_video = split_video_backup_path(group_id)
    if src_video.exists():
        dst = video_path(target_session_id)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_video, dst)


def clear_split_backup(group_id: str) -> None:
    target = split_backup_dir(group_id)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)


def trim_wav_file(src: Path, dst: Path, start_sec: float, end_sec: float) -> float:
    paths.tmp_dir().mkdir(parents=True, exist_ok=True)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(src), "rb") as wf:
        params = wf.getparams()
        rate = wf.getframerate()
        total = wf.getnframes()
        start_frame = max(0, min(total, int(round(start_sec * rate))))
        end_frame = max(start_frame + 1, min(total, int(round(end_sec * rate))))
        wf.setpos(start_frame)
        frames = wf.readframes(end_frame - start_frame)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    with wave.open(str(tmp), "wb") as out:
        out.setparams(params)
        out.writeframes(frames)
    tmp.replace(dst)
    return get_wav_duration(dst)


def trim_wav(session_id: str, start_sec: float, end_sec: float) -> float:
    backup_original_media(session_id)
    return trim_wav_file(wav_path(session_id), wav_path(session_id), start_sec, end_sec)


def trim_video_file(
    src: Path,
    dst: Path,
    start_sec: float,
    end_sec: float,
    video_offset: float,
    ffmpeg_bin: str | None,
) -> float:
    if not src.exists():
        return max(0.0, video_offset - start_sec)
    if not ffmpeg_bin:
        raise RuntimeError("FFmpeg is required to edit a session with screen recording video")
    dst.parent.mkdir(parents=True, exist_ok=True)
    video_start = max(0.0, start_sec - video_offset)
    video_end = max(0.0, end_sec - video_offset)
    duration = video_end - video_start
    if duration <= 0:
        if dst.exists():
            dst.unlink()
        return 0.0
    tmp = dst.with_suffix(dst.suffix + ".tmp.mp4")
    if tmp.exists():
        tmp.unlink()

    # Re-encode instead of stream-copying. Frame-exact audio edits must be
    # paired with frame-exact video cuts; MP4 stream copy can only cut cleanly
    # on keyframes and may preserve timestamps that desync browser playback.
    cmd = [
        ffmpeg_bin, "-y",
        "-i", str(src),
        "-ss", f"{video_start:.3f}",
        "-t", f"{duration:.3f}",
        "-map", "0:v:0",
        "-an",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-avoid_negative_ts", "make_zero",
        str(tmp),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "ffmpeg video trim failed").strip())
    if not tmp.exists() or tmp.stat().st_size == 0:
        raise RuntimeError("ffmpeg video trim produced an empty file")
    tmp.replace(dst)
    return max(0.0, video_offset - start_sec)


def trim_video(session_id: str, start_sec: float, end_sec: float, video_offset: float, ffmpeg_bin: str | None) -> float:
    backup_original_media(session_id)
    path = video_path(session_id)
    return trim_video_file(path, path, start_sec, end_sec, video_offset, ffmpeg_bin)
