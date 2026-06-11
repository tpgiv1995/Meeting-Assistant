"""
Auto-optimize diarization parameters using corrected session data.

Uses sessions where the user has renamed/merged speakers as implicit
ground truth.  The "true" speaker count is derived from the number of
distinct custom names assigned.  The optimizer scores parameter
configurations by how close the batch pipeline's speaker count comes
to the actual count, without requiring manual transcript annotation.

Usage:
    # Analyze current sessions and recommend better defaults
    python optimize_diarization.py analyze

    # Run a batch reanalysis parameter sweep on a specific session
    python optimize_diarization.py sweep --session <session_id>

    # Run sweep on N random corrected sessions
    python optimize_diarization.py sweep --sample 5

    # Apply recommended defaults to settings
    python optimize_diarization.py apply
"""
import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

import numpy as np

from core import paths as paths


def _conn():
    c = sqlite3.connect(str(paths.db_path()))
    c.row_factory = sqlite3.Row
    return c


def get_corrected_sessions() -> list[dict]:
    """Return sessions with user-corrected speaker labels."""
    conn = _conn()
    rows = conn.execute("""
        SELECT s.id, s.title,
               COUNT(DISTINCT sl.speaker_key) AS diarizer_speakers,
               COUNT(DISTINCT CASE
                   WHEN sl.name NOT GLOB 'Speaker [0-9]*' AND sl.name != ''
                   THEN LOWER(sl.name)
               END) AS actual_speakers,
               (SELECT COUNT(*) FROM transcript_segments ts
                WHERE ts.session_id = s.id) AS seg_count,
               (SELECT MAX(end_time) - MIN(start_time)
                FROM transcript_segments ts
                WHERE ts.session_id = s.id) AS duration_sec
        FROM sessions s
        JOIN speaker_labels sl ON sl.session_id = s.id
        WHERE s.id IN (
            SELECT DISTINCT session_id FROM speaker_labels
            WHERE name NOT GLOB 'Speaker [0-9]*' AND name != ''
        )
        GROUP BY s.id
        ORDER BY diarizer_speakers DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def analyze(sessions: list[dict]) -> dict:
    """Analyze over-segmentation across corrected sessions.

    Returns summary statistics and recommended parameter adjustments.
    """
    if not sessions:
        return {"error": "No corrected sessions found"}

    over_seg = [s for s in sessions
                if s["diarizer_speakers"] - s["actual_speakers"] >= 3]
    correct = [s for s in sessions
               if abs(s["diarizer_speakers"] - s["actual_speakers"]) <= 1]

    excess_ratios = []
    for s in sessions:
        actual = max(1, s["actual_speakers"])
        excess_ratios.append(s["diarizer_speakers"] / actual)

    segs_per_min = []
    for s in sessions:
        dur = s["duration_sec"] or 1
        segs_per_min.append(s["seg_count"] / (dur / 60))

    # Compute speaker count distribution
    actual_counts = [s["actual_speakers"] for s in sessions]
    median_actual = int(np.median(actual_counts))
    mean_actual = np.mean(actual_counts)

    return {
        "total_sessions": len(sessions),
        "over_segmented": len(over_seg),
        "correct": len(correct),
        "over_seg_pct": round(100 * len(over_seg) / len(sessions), 1),
        "mean_excess_ratio": round(np.mean(excess_ratios), 2),
        "median_actual_speakers": median_actual,
        "mean_actual_speakers": round(mean_actual, 1),
        "mean_segs_per_min": round(np.mean(segs_per_min), 1),
        "median_segs_per_min": round(np.median(segs_per_min), 1),
    }


def recommend_streaming_params(analysis: dict) -> dict:
    """Recommend streaming diarization parameters based on analysis.

    Heuristic: if over-segmentation is dominant, raise delta_new and
    lower rho_update to make the clustering more conservative.
    """
    over_pct = analysis.get("over_seg_pct", 0)
    excess = analysis.get("mean_excess_ratio", 1.0)

    # NOTE: delta_new is cosine *distance*; the match threshold is 1 - delta_new.
    # Keep delta_new <= ~0.65 — pushing higher drops the match threshold below
    # typical inter-speaker similarity (~0.2-0.4) and collapses distinct voices
    # into a single speaker.
    if over_pct >= 50:
        # Severe over-segmentation — aggressive correction (still bounded)
        delta_new = 0.65
        rho_update = 0.25
        merge_gap = 0.15
    elif over_pct >= 25:
        # Moderate over-segmentation
        delta_new = 0.60
        rho_update = 0.30
        merge_gap = 0.12
    else:
        # Mild or no over-segmentation — keep balanced
        delta_new = 0.55
        rho_update = 0.40
        merge_gap = 0.10

    return {
        "delta_new": delta_new,
        "rho_update": rho_update,
        "tau_active": 0.5,
        "merge_gap_seconds": merge_gap,
        "step_seconds": 0.25,
        "duration_seconds": 5.0,
    }


def recommend_reanalysis_params(analysis: dict) -> dict:
    """Recommend batch reanalysis parameters based on analysis."""
    over_pct = analysis.get("over_seg_pct", 0)
    median_spk = analysis.get("median_actual_speakers", 3)

    if over_pct >= 50:
        clustering_threshold = 0.70
        min_duration_off = 0.3
        merge_gap = 0.8
    elif over_pct >= 25:
        clustering_threshold = 0.60
        min_duration_off = 0.2
        merge_gap = 0.6
    else:
        clustering_threshold = 0.50
        min_duration_off = 0.1
        merge_gap = 0.5

    return {
        "reanalysis_clustering_threshold": clustering_threshold,
        "reanalysis_min_duration_off": min_duration_off,
        "reanalysis_merge_gap": merge_gap,
        "reanalysis_max_speakers": min(median_spk + 4, 12),
    }


def apply_recommendations(streaming: dict, reanalysis: dict) -> None:
    """Write recommended parameters to settings.json."""
    settings_file = paths.settings_path()
    if settings_file.exists():
        with open(settings_file) as f:
            settings = json.load(f)
    else:
        settings = {}

    ap = settings.setdefault("audio_params", {})
    for k, v in streaming.items():
        ap[k] = v
    for k, v in reanalysis.items():
        ap[k] = v

    with open(settings_file, "w") as f:
        json.dump(settings, f, indent=2)


def run_sweep(session_id: str, hf_token: str, device: str = "cpu") -> list[dict]:
    """Sweep reanalysis clustering threshold on a single session."""
    conn = _conn()

    # Get actual speaker count
    row = conn.execute("""
        SELECT COUNT(DISTINCT CASE
            WHEN name NOT GLOB 'Speaker [0-9]*' AND name != ''
            THEN LOWER(name)
        END) AS actual
        FROM speaker_labels WHERE session_id = ?
    """, (session_id,)).fetchone()
    actual_speakers = row["actual"] if row else 0

    # Check WAV exists
    wav_path = paths.audio_dir() / f"{session_id}.wav"
    if not wav_path.exists():
        print(f"  WAV not found: {wav_path}")
        conn.close()
        return []

    title = conn.execute("SELECT title FROM sessions WHERE id=?",
                         (session_id,)).fetchone()
    title = title["title"] if title else session_id[:12]
    conn.close()

    print(f"\nSession: {title}")
    print(f"Actual speakers: {actual_speakers}")
    print(f"WAV: {wav_path}")

    thresholds = [0.35, 0.45, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85]
    results = []

    for ct in thresholds:
        print(f"  threshold={ct:.2f} ... ", end="", flush=True)
        try:
            import torch
            from ml.batch_transcriber import BatchTranscriber

            segments = []
            def on_text(text, speaker, start, end):
                segments.append((speaker, start, end))

            bt = BatchTranscriber(
                on_text_callback=on_text,
                hf_token=hf_token,
            )
            params = {
                "reanalysis_device": device,
                "reanalysis_whisper_model": "openai/whisper-small",
                "reanalysis_batch_size": 4,
                "reanalysis_clustering_threshold": ct,
                "reanalysis_min_duration_off": 0.3,
                "reanalysis_merge_gap": 0.5,
            }
            bt.process_wav_file(str(wav_path), params)

            detected = len(set(s for s, _, _ in segments))
            error = abs(detected - actual_speakers)
            results.append({
                "threshold": ct,
                "detected_speakers": detected,
                "actual_speakers": actual_speakers,
                "error": error,
                "segments": len(segments),
            })
            print(f"detected={detected} (error={error:+d}, {len(segments)} segs)")

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception as e:
            print(f"FAILED: {e}")
            results.append({"threshold": ct, "error": 999, "exception": str(e)})

    results.sort(key=lambda x: (x.get("error", 999), x.get("threshold", 0)))
    return results


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Auto-optimize diarization from corrected sessions"
    )
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("analyze", help="Analyze sessions and recommend parameters")

    sweep_p = sub.add_parser("sweep", help="Run batch parameter sweep")
    sweep_p.add_argument("--session", help="Session ID to sweep")
    sweep_p.add_argument("--sample", type=int, help="Sweep N random sessions")
    sweep_p.add_argument("--device", default="cpu")

    sub.add_parser("apply", help="Apply recommended parameters to settings")

    args = parser.parse_args()

    if args.cmd == "analyze" or args.cmd is None:
        sessions = get_corrected_sessions()
        analysis = analyze(sessions)

        print("=" * 60)
        print("DIARIZATION QUALITY ANALYSIS")
        print("=" * 60)
        print(f"  Sessions analyzed:         {analysis['total_sessions']}")
        print(f"  Over-segmented (3+ excess): {analysis['over_segmented']} "
              f"({analysis['over_seg_pct']}%)")
        print(f"  Correct (within 1):        {analysis['correct']}")
        print(f"  Mean speaker excess ratio: {analysis['mean_excess_ratio']}x")
        print(f"  Median actual speakers:    {analysis['median_actual_speakers']}")
        print(f"  Mean segments/min:         {analysis['mean_segs_per_min']}")

        streaming = recommend_streaming_params(analysis)
        reanalysis = recommend_reanalysis_params(analysis)

        print(f"\n{'=' * 60}")
        print("RECOMMENDED STREAMING PARAMETERS")
        print("=" * 60)
        for k, v in streaming.items():
            print(f"  {k}: {v}")

        print(f"\n{'=' * 60}")
        print("RECOMMENDED REANALYSIS PARAMETERS")
        print("=" * 60)
        for k, v in reanalysis.items():
            print(f"  {k}: {v}")

        print(f"\nRun `python optimize_diarization.py apply` to write these to settings.")

    elif args.cmd == "sweep":
        from dotenv import load_dotenv
        load_dotenv()
        from core import config as config
        hf_token = os.getenv("HUGGING_FACE_KEY", "")

        if args.session:
            results = run_sweep(args.session, hf_token, args.device)
        elif args.sample:
            sessions = get_corrected_sessions()
            rng = np.random.RandomState(42)
            sample = rng.choice(sessions, min(args.sample, len(sessions)),
                                replace=False)
            all_results = {}
            for s in sample:
                results = run_sweep(s["id"], hf_token, args.device)
                for r in results:
                    t = r["threshold"]
                    all_results.setdefault(t, []).append(r.get("error", 999))

            print(f"\n{'=' * 60}")
            print("AGGREGATE SWEEP RESULTS")
            print("=" * 60)
            for t in sorted(all_results):
                errors = all_results[t]
                print(f"  threshold={t:.2f}  mean_error={np.mean(errors):.1f}  "
                      f"perfect={sum(1 for e in errors if e == 0)}/{len(errors)}")
        else:
            print("Specify --session <id> or --sample N")

    elif args.cmd == "apply":
        sessions = get_corrected_sessions()
        analysis = analyze(sessions)
        streaming = recommend_streaming_params(analysis)
        reanalysis = recommend_reanalysis_params(analysis)
        apply_recommendations(streaming, reanalysis)
        print("Settings updated. Changes take effect on next session start.")
        print(f"Streaming:  delta_new={streaming['delta_new']}, "
              f"rho_update={streaming['rho_update']}")
        print(f"Reanalysis: clustering_threshold="
              f"{reanalysis['reanalysis_clustering_threshold']}")


if __name__ == "__main__":
    main()
