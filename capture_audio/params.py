"""
Default values for all tunable transcription and diarization parameters.

These are the baseline values shipped with the application.  User overrides
are stored in data/settings.json under the "audio_params" key.  The settings
UI provides per-parameter reset buttons that restore these defaults.
"""

TRANSCRIPTION_DEFAULTS = {
    "silence_threshold": {
        "value": 0.025,
        "label": "Silence Threshold",
        "description": "RMS level below which audio is considered silence.",
        "tooltip": (
            "Controls the <em>volume floor</em> for silence detection. Audio "
            "with an RMS energy below this value is treated as silence, which "
            "triggers the buffer flush to Whisper after the configured duration.<br><br>"
            "<b>Lower values</b> make the detector more sensitive \u2014 even faint "
            "background noise counts as speech, delaying flushes and producing "
            "longer segments.<br>"
            "<b>Higher values</b> treat more audio as silence, flushing sooner "
            "and producing shorter, snappier segments \u2014 but risk cutting off "
            "quiet speakers."
        ),
        "min": 0.001,
        "max": 0.2,
        "step": 0.001,
        "type": "number",
    },
    "silence_duration": {
        "value": 0.3,
        "label": "Silence Duration",
        "unit": "s",
        "description": "Seconds of silence before flushing audio to Whisper.",
        "tooltip": (
            "Once the audio level drops below the <em>Silence Threshold</em>, "
            "this timer starts. When the silence persists for this many seconds, "
            "the buffered audio is sent to Whisper for transcription.<br><br>"
            "<b>Shorter durations</b> give faster response times but may split "
            "natural pauses mid-sentence.<br>"
            "<b>Longer durations</b> allow speakers to pause without triggering "
            "a premature flush, but add visible latency."
        ),
        "min": 0.1,
        "max": 2.0,
        "step": 0.05,
        "type": "number",
    },
    "min_buffer_seconds": {
        "value": 0.5,
        "label": "Min Buffer",
        "unit": "s",
        "description": "Minimum audio before a silence flush is allowed.",
        "tooltip": (
            "Prevents the system from flushing tiny slivers of audio that "
            "would produce garbage Whisper output. No silence-triggered flush "
            "will fire until at least this much audio has been buffered.<br><br>"
            "<b>Lower values</b> allow very short utterances to be transcribed "
            "quickly.<br>"
            "<b>Higher values</b> ensure Whisper always receives enough context "
            "for accurate transcription, at the cost of some latency."
        ),
        "min": 0.1,
        "max": 3.0,
        "step": 0.1,
        "type": "number",
    },
    "max_buffer_seconds": {
        "value": 10.0,
        "label": "Max Buffer",
        "unit": "s",
        "description": "Hard cap \u2014 forces a flush regardless of silence.",
        "tooltip": (
            "A safety valve that forces the audio buffer to be flushed and "
            "transcribed even if no silence pause has been detected. Prevents "
            "runaway buffering during continuous speech.<br><br>"
            "<b>Lower values</b> ensure more frequent transcription updates but "
            "may cut sentences mid-word.<br>"
            "<b>Higher values</b> let Whisper process longer stretches for "
            "better accuracy, but the transcript updates less frequently."
        ),
        "min": 3.0,
        "max": 30.0,
        "step": 0.5,
        "type": "number",
    },
    "beam_size": {
        "value": 2,
        "label": "Beam Size",
        "description": "Whisper beam search width.",
        "tooltip": (
            "Controls how many candidate transcriptions Whisper considers in "
            "parallel during decoding. A wider beam explores more possibilities "
            "before picking the best result.<br><br>"
            "<b>Beam 1</b> (greedy) is fastest but most error-prone.<br>"
            "<b>Beam 2\u20133</b> is the sweet spot for real-time use.<br>"
            "<b>Beam 5+</b> gives marginally better accuracy but significantly "
            "increases latency and VRAM usage."
        ),
        "min": 1,
        "max": 10,
        "step": 1,
        "type": "int",
    },
    "prompt_chars": {
        "value": 800,
        "label": "Context Window",
        "unit": "chars",
        "description": "Prior transcript fed to Whisper as context.",
        "tooltip": (
            "Whisper uses recent transcript text as a <em>conditioning prompt</em> "
            "to maintain coherence across segments \u2014 preserving names, "
            "terminology, and sentence flow.<br><br>"
            "<b>More characters</b> provide richer context but increase the risk "
            "of hallucination loops if the context itself gets corrupted.<br>"
            "<b>Fewer characters</b> (or zero) make each segment independent, "
            "reducing loop risk but losing cross-segment continuity."
        ),
        "min": 0,
        "max": 2000,
        "step": 50,
        "type": "int",
    },
    "vad_min_silence_ms": {
        "value": 300,
        "label": "VAD Min Silence",
        "unit": "ms",
        "description": "Whisper\u2019s internal VAD silence split threshold.",
        "tooltip": (
            "When Whisper\u2019s built-in Voice Activity Detection is active "
            "(non-diarized mode), this controls the minimum duration of silence "
            "that causes the VAD to split the audio into separate speech regions.<br><br>"
            "<b>Lower values</b> split more aggressively at short pauses.<br>"
            "<b>Higher values</b> keep more speech in a single region, which can "
            "improve transcription quality for speakers with frequent pauses."
        ),
        "min": 50,
        "max": 1000,
        "step": 25,
        "type": "int",
    },
    "vad_speech_pad_ms": {
        "value": 150,
        "label": "VAD Speech Padding",
        "unit": "ms",
        "description": "Padding added around detected speech regions.",
        "tooltip": (
            "After the VAD identifies speech boundaries, this much padding is "
            "added to both the start and end of each speech region to avoid "
            "clipping the first or last syllable.<br><br>"
            "<b>More padding</b> reduces the chance of clipped words but may "
            "include extra silence or noise.<br>"
            "<b>Less padding</b> gives tighter segments but risks cutting off "
            "speech at boundaries."
        ),
        "min": 0,
        "max": 500,
        "step": 25,
        "type": "int",
    },
    "compression_ratio_threshold": {
        "value": 2.0,
        "label": "Hallucination Filter",
        "description": "Compression ratio above which output is discarded.",
        "tooltip": (
            "Whisper measures the <em>compression ratio</em> of its output "
            "text. Highly repetitive or hallucinated text compresses extremely "
            "well, producing a high ratio. Segments exceeding this threshold "
            "are automatically discarded and retried without context.<br><br>"
            "<b>Lower values</b> are stricter \u2014 more aggressive at catching "
            "hallucinations but may occasionally reject valid repetitive speech "
            "(e.g. a speaker saying \u201cno no no no\u201d).<br>"
            "<b>Higher values</b> are more permissive, letting more through at "
            "the risk of hallucination loops."
        ),
        "min": 1.0,
        "max": 3.0,
        "step": 0.1,
        "type": "number",
    },
}

DIARIZATION_DEFAULTS = {
    "step_seconds": {
        "value": 0.25,
        "label": "Step Size",
        "unit": "s",
        "description": "How often speaker labels are updated.",
        "tooltip": (
            "The diarization pipeline advances by this many seconds each "
            "cycle. Each step produces a fresh speaker label decision for "
            "that slice of audio.<br><br>"
            "<b>Smaller steps</b> detect speaker changes faster (less lag at "
            "transitions) but double the compute load per second.<br>"
            "<b>Larger steps</b> are more efficient but speaker changes "
            "may lag by up to one full step duration.<br><br>"
            "<em>Requires session restart to take effect.</em>"
        ),
        "min": 0.1,
        "max": 1.0,
        "step": 0.05,
        "type": "number",
    },
    "duration_seconds": {
        "value": 5.0,
        "label": "Context Window",
        "unit": "s",
        "description": "Audio window fed to the segmentation model.",
        "tooltip": (
            "The segmentation model receives this much audio as context for "
            "each step. The pyannote segmentation-3.0 model was trained on "
            "5-second windows \u2014 deviating significantly may reduce accuracy.<br><br>"
            "<b>Shorter windows</b> process faster but give the model less "
            "context to distinguish speakers.<br>"
            "<b>Longer windows</b> provide more context but increase memory "
            "usage and latency.<br><br>"
            "<em>Requires session restart to take effect.</em>"
        ),
        "min": 2.0,
        "max": 10.0,
        "step": 0.5,
        "type": "number",
    },
    "tau_active": {
        "value": 0.5,
        "label": "Activity Threshold",
        "description": "Voice-activity sensitivity for speaker detection.",
        "tooltip": (
            "Controls how confident the model must be that a speaker is "
            "actively talking before assigning them a label. This is the "
            "diarizer\u2019s own VAD, separate from Whisper\u2019s.<br><br>"
            "<b>Lower values</b> are more sensitive \u2014 picks up quiet speech "
            "and distant speakers, but may also pick up background noise.<br>"
            "<b>Higher values</b> require stronger voice activity, reducing "
            "false positives but potentially missing soft-spoken participants."
        ),
        "min": 0.1,
        "max": 0.9,
        "step": 0.05,
        "type": "number",
    },
    "rho_update": {
        "value": 0.25,
        "label": "Centroid Update Rate",
        "description": "Speaker embedding adaptation speed.",
        "tooltip": (
            "Speaker embeddings (voice fingerprints) are stored as centroids "
            "that get updated as new audio arrives. This controls how much "
            "weight new audio gets versus the existing centroid.<br><br>"
            "<b>Higher values</b> adapt faster to changes in a speaker\u2019s voice "
            "(useful for varying mic distance or tone), but may cause speaker "
            "identities to drift and merge.<br>"
            "<b>Lower values</b> keep centroids stable, which is better for "
            "long recordings with consistent audio quality."
        ),
        "min": 0.1,
        "max": 1.0,
        "step": 0.01,
        "type": "number",
    },
    "delta_new": {
        "value": 0.65,
        "label": "New Speaker Threshold",
        "description": "Distance required to create a new speaker.",
        "tooltip": (
            "When a voice segment doesn\u2019t match any known speaker centroid "
            "within this distance, a new speaker is created. Think of it as "
            "how \u201cdifferent\u201d a voice must sound to be recognized as someone new.<br><br>"
            "<b>Lower values</b> create new speakers more readily \u2014 good for "
            "meetings with many participants who sound similar.<br>"
            "<b>Higher values</b> are more conservative, merging similar voices "
            "into existing speakers \u2014 better when there are fewer participants "
            "to avoid over-segmentation."
        ),
        "min": 0.1,
        "max": 2.0,
        "step": 0.05,
        "type": "number",
    },
    "merge_gap_seconds": {
        "value": 0.15,
        "label": "Merge Gap",
        "unit": "s",
        "description": "Max gap before same-speaker segments merge.",
        "tooltip": (
            "When the same speaker is detected in two consecutive segments "
            "with a gap shorter than this, they are merged into a single "
            "continuous segment.<br><br>"
            "<b>Higher values</b> merge more aggressively, producing fewer, "
            "longer segments \u2014 cleaner output but may merge across genuine "
            "pauses.<br>"
            "<b>Lower values</b> (or zero) preserve every segment boundary, "
            "giving a more granular timeline but potentially fragmenting "
            "continuous speech."
        ),
        "min": 0.0,
        "max": 1.0,
        "step": 0.05,
        "type": "number",
    },
    "segment_break_silence": {
        "value": 0.75,
        "label": "Segment Break Silence",
        "unit": "s",
        "description": "Silence gap that forces a new transcript segment for the same speaker.",
        "tooltip": (
            "When the same speaker continues after a pause longer than this "
            "threshold, a new transcript segment is created instead of appending "
            "to the current one. This breaks long monologues into readable "
            "paragraphs.<br><br>"
            "<b>Higher values</b> produce fewer, longer segments (more merging).<br>"
            "<b>Lower values</b> create more frequent breaks, improving readability "
            "of long monologues."
        ),
        "min": 0.5,
        "max": 5.0,
        "step": 0.25,
        "type": "number",
    },
}

AUTO_GAIN_DEFAULTS = {
    "agc_loopback_enabled": {
        "value": 1,
        "label": "Desktop Auto Gain",
        "description": "Automatically boost quiet desktop audio to a consistent level.",
        "tooltip": (
            "Applies gentle dynamic gain to the desktop (loopback) audio so that "
            "quiet participants are brought closer in volume to louder ones. The "
            "gain envelope tracks slowly to avoid pumping artifacts.<br><br>"
            "<b>Enable</b> when meeting participants have mismatched volume levels.<br>"
            "<b>Leave disabled</b> if desktop audio levels are already consistent."
        ),
        "min": 0,
        "max": 1,
        "step": 1,
        "type": "toggle",
    },
    "agc_mic_enabled": {
        "value": 1,
        "label": "Microphone Auto Gain",
        "description": "Automatically boost quiet microphone audio to a consistent level.",
        "tooltip": (
            "Applies gentle dynamic gain to the microphone input. Useful if your "
            "mic level varies or is set low.<br><br>"
            "<b>Enable</b> if your mic audio is consistently too quiet.<br>"
            "<b>Leave disabled</b> if your mic level is already adequate."
        ),
        "min": 0,
        "max": 1,
        "step": 1,
        "type": "toggle",
    },
    "agc_target_rms": {
        "value": 0.15,
        "label": "AGC Target Level",
        "description": "Target RMS level that the auto-gain normalises toward.",
        "tooltip": (
            "The desired RMS amplitude (0\u20131) for the auto-gain output. Audio "
            "quieter than this is boosted; audio louder is left untouched.<br><br>"
            "<b>Higher values</b> produce louder normalised output.<br>"
            "<b>Lower values</b> are more conservative."
        ),
        "min": 0.05,
        "max": 0.35,
        "step": 0.01,
        "type": "number",
        "agc_param": True,
    },
    "agc_max_gain": {
        "value": 4.0,
        "label": "AGC Max Boost",
        "unit": "\u00d7",
        "description": "Maximum gain multiplier the auto-gain will apply.",
        "tooltip": (
            "Caps the auto-gain boost to prevent amplifying silence or background "
            "noise into distortion.<br><br>"
            "<b>Higher values</b> can rescue very quiet audio but risk boosting "
            "noise.<br>"
            "<b>Lower values</b> are safer but won't help extremely quiet sources."
        ),
        "min": 1.5,
        "max": 10.0,
        "step": 0.5,
        "type": "number",
        "agc_param": True,
    },
    "agc_gate_threshold": {
        "value": 0.005,
        "label": "AGC Noise Gate",
        "description": "RMS level below which auto-gain will not boost.",
        "tooltip": (
            "Acts as a noise gate for the auto-gain. Audio with an RMS level "
            "below this threshold is treated as silence or background noise and "
            "will not be boosted, preventing the gain from jumping around during "
            "quiet moments or brief noise bursts.<br><br>"
            "<b>Higher values</b> make the gate stricter \u2014 only clearly audible "
            "speech triggers boosting.<br>"
            "<b>Lower values</b> allow quieter signals to be boosted, but may "
            "amplify room tone."
        ),
        "min": 0.005,
        "max": 0.05,
        "step": 0.001,
        "type": "number",
        "agc_param": True,
    },
}

ECHO_CANCELLATION_DEFAULTS = {
    "echo_cancel_enabled": {
        "value": 0,
        "label": "Enable Echo Cancellation",
        "description": "Remove speaker echo from the microphone signal.",
        "tooltip": (
            "Uses WebRTC AEC (Acoustic Echo Cancellation) to remove desktop "
            "speaker audio that bleeds into the microphone. This is the same "
            "echo canceller used in Chrome and other browsers.<br><br>"
            "<b>Leave disabled</b> if you use headphones or a headset \u2014 echo "
            "cancellation is unnecessary.<br>"
            "<b>Enable</b> if you hear duplicated transcriptions caused by the mic "
            "picking up speaker output."
        ),
        "min": 0,
        "max": 1,
        "step": 1,
        "type": "toggle",
    },
}


SCREEN_RECORDING_DEFAULTS = {
    "screen_record_enabled": {
        "value": 0,
        "label": "Enable Screen Recording",
        "description": "Record the selected display during meetings.",
        "tooltip": (
            "Captures your screen using FFmpeg and saves it as an MP4 file "
            "alongside the audio recording. The video is encoded with H.264 "
            "for broad compatibility.<br><br>"
            "<b>Enable</b> to record your screen during meetings.<br>"
            "<b>Leave disabled</b> to save system resources when video isn't needed."
        ),
        "min": 0,
        "max": 1,
        "step": 1,
        "type": "toggle",
    },
    "screen_framerate": {
        "value": 10,
        "label": "Framerate",
        "unit": "fps",
        "description": "Capture frames per second.",
        "tooltip": (
            "How many frames per second to capture from the display. Screen "
            "content is mostly static, so low framerates work well.<br><br>"
            "<b>5–10 fps</b> is ideal for presentations and documents - minimal "
            "CPU usage and small files.<br>"
            "<b>15–24 fps</b> is smooth enough for video playback and demos.<br>"
            "<b>30 fps</b> produces very smooth video but significantly larger files."
        ),
        "min": 1,
        "max": 60,
        "step": 1,
        "type": "int",
    },
    "screen_crf": {
        "value": 32,
        "label": "Quality (CRF)",
        "description": "Constant Rate Factor - lower is better quality.",
        "tooltip": (
            "Controls the quality-vs-size tradeoff for H.264 encoding. CRF "
            "uses a perceptual quality model - the encoder adjusts bitrate "
            "automatically to maintain constant visual quality.<br><br>"
            "<b>18–22</b>: Visually lossless - excellent quality, large files.<br>"
            "<b>23–28</b>: Good quality - text is sharp, moderate file size.<br>"
            "<b>29–35</b>: Acceptable quality - some softness, small files.<br>"
            "<b>36+</b>: Low quality - blurry details, very small files.<br><br>"
            "Each +6 roughly halves the file size."
        ),
        "min": 0,
        "max": 51,
        "step": 1,
        "type": "int",
    },
    "screen_h264_preset": {
        "value": 2,
        "label": "Encoder Speed",
        "description": "H.264 preset - faster encoding uses more disk, less CPU.",
        "tooltip": (
            "The H.264 preset controls the trade-off between encoding speed "
            "and compression efficiency. All presets produce the same visual "
            "quality at a given CRF - faster presets just use more bits.<br><br>"
            "<b>0 (ultrafast)</b>: Minimal CPU, ~2× file size vs medium.<br>"
            "<b>2 (veryfast)</b>: Low CPU, good compression. Recommended.<br>"
            "<b>4 (fast)</b>: Moderate CPU, efficient compression.<br>"
            "<b>5 (medium)</b>: FFmpeg default - balanced but heavier.<br>"
            "<b>7+ (slow–veryslow)</b>: Maximum compression, high CPU."
        ),
        "min": 0,
        "max": 8,
        "step": 1,
        "type": "int",
    },
    "screen_scale_width": {
        "value": 0,
        "label": "Downscale Width",
        "unit": "px",
        "description": "Scale video width (0 = native resolution).",
        "tooltip": (
            "Downscale the captured video to this width (height adjusts "
            "automatically to maintain aspect ratio). Useful on high-DPI "
            "displays to reduce file size.<br><br>"
            "<b>0</b>: Native resolution (no scaling).<br>"
            "<b>1920</b>: Full HD - good for 4K displays.<br>"
            "<b>1280</b>: 720p - small files, still readable text.<br><br>"
            "Values below 960 may make small text difficult to read."
        ),
        "min": 0,
        "max": 7680,
        "step": 160,
        "type": "int",
    },
}

# ── Transcription Presets ─────────────────────────────────────────────────────

TRANSCRIPTION_PRESETS = {
    "responsive": {
        "label": "Responsive",
        "description": "Fast updates, shorter segments - ideal for live captioning",
        "values": {
            "silence_threshold": 0.03,
            "silence_duration": 0.2,
            "min_buffer_seconds": 0.3,
            "max_buffer_seconds": 6.0,
            "beam_size": 1,
            "prompt_chars": 400,
            "vad_min_silence_ms": 200,
            "vad_speech_pad_ms": 100,
            "compression_ratio_threshold": 2.0,
        },
    },
    "balanced": {
        "label": "Balanced (Default)",
        "description": "Good accuracy with reasonable latency - recommended for most meetings",
        "values": {
            "silence_threshold": 0.025,
            "silence_duration": 0.3,
            "min_buffer_seconds": 0.5,
            "max_buffer_seconds": 10.0,
            "beam_size": 2,
            "prompt_chars": 800,
            "vad_min_silence_ms": 300,
            "vad_speech_pad_ms": 150,
            "compression_ratio_threshold": 2.0,
        },
    },
    "accurate": {
        "label": "Accurate",
        "description": "Higher accuracy with longer context - more latency, better results",
        "values": {
            "silence_threshold": 0.02,
            "silence_duration": 0.5,
            "min_buffer_seconds": 1.0,
            "max_buffer_seconds": 15.0,
            "beam_size": 4,
            "prompt_chars": 1200,
            "vad_min_silence_ms": 400,
            "vad_speech_pad_ms": 200,
            "compression_ratio_threshold": 2.2,
        },
    },
    "quality": {
        "label": "Quality",
        "description": "Maximum accuracy - significant latency, best for post-processing",
        "values": {
            "silence_threshold": 0.015,
            "silence_duration": 0.7,
            "min_buffer_seconds": 1.5,
            "max_buffer_seconds": 20.0,
            "beam_size": 5,
            "prompt_chars": 1600,
            "vad_min_silence_ms": 500,
            "vad_speech_pad_ms": 250,
            "compression_ratio_threshold": 2.4,
        },
    },
    "custom": {
        "label": "Custom",
        "description": "Manually configure all parameters",
        "values": {},
    },
}

TRANSCRIPTION_DEFAULT_PRESET = "balanced"


# ── Reanalysis Defaults ──────────────────────────────────────────────────────
# These are used by the batch reanalysis pipeline (batch_transcriber.py) and
# are stored under a separate "reanalysis_params" key in settings.json.

REANALYSIS_DEFAULTS = {
    "reanalysis_whisper_model": {
        "value": "openai/whisper-large-v3-turbo",
        "label": "Whisper Model",
        "description": "HuggingFace model for batch transcription.",
        "tooltip": (
            "The Whisper model used for batch reanalysis. Unlike the real-time "
            "pipeline (faster-whisper / CTranslate2), this uses the HuggingFace "
            "transformers backend with batched inference for maximum throughput.<br><br>"
            "<b>large-v3</b>: Best accuracy, highest VRAM usage.<br>"
            "<b>large-v3-turbo</b>: Near large-v3 accuracy, significantly faster.<br>"
            "<b>medium / small</b>: Lower accuracy, less VRAM."
        ),
        "type": "select",
        "options": [
            {"id": "openai/whisper-large-v3-turbo", "label": "large-v3-turbo (recommended)"},
            {"id": "openai/whisper-large-v3", "label": "large-v3 (best accuracy, slow)"},
            {"id": "openai/whisper-medium", "label": "medium"},
            {"id": "openai/whisper-small", "label": "small (fastest)"},
        ],
    },
    "reanalysis_batch_size": {
        "value": 8,
        "label": "Batch Size",
        "description": "Segments transcribed in parallel per batch.",
        "tooltip": (
            "Controls how many diarized audio segments are sent to Whisper "
            "simultaneously. Higher values process faster but use more VRAM.<br><br>"
            "<b>4\u20138</b>: Safe for most GPUs (8\u201312 GB VRAM).<br>"
            "<b>12\u201316</b>: Good for 16+ GB VRAM.<br>"
            "<b>24+</b>: High-end GPUs (24+ GB).<br><br>"
            "Reduce if you encounter out-of-memory errors."
        ),
        "min": 1,
        "max": 64,
        "step": 1,
        "type": "int",
    },
    "reanalysis_device": {
        "value": "auto",
        "label": "Device",
        "description": "Processing device for batch reanalysis.",
        "tooltip": (
            "Which device to use for diarization and transcription.<br><br>"
            "<b>Auto</b>: Uses GPU if available, falls back to CPU.<br>"
            "<b>GPU</b>: Forces CUDA (fails if no compatible GPU).<br>"
            "<b>CPU</b>: Slower but always available."
        ),
        "type": "select",
        "options": [
            {"id": "auto", "label": "Auto (GPU if available)"},
            {"id": "cuda", "label": "GPU (CUDA)"},
            {"id": "cpu", "label": "CPU"},
        ],
    },
    "reanalysis_num_speakers": {
        "value": 0,
        "label": "Number of Speakers",
        "description": "Expected speaker count (0 = auto-detect).",
        "tooltip": (
            "Set the exact number of speakers if known. This significantly "
            "improves diarization accuracy.<br><br>"
            "<b>0</b>: Auto-detect (pyannote estimates from the audio).<br>"
            "<b>1\u201320</b>: Force exact speaker count."
        ),
        "min": 0,
        "max": 20,
        "step": 1,
        "type": "int",
    },
    "reanalysis_min_speakers": {
        "value": 0,
        "label": "Min Speakers",
        "description": "Minimum expected speakers (0 = no minimum).",
        "tooltip": (
            "Lower bound for auto-detection. Only used when Number of Speakers "
            "is set to 0 (auto-detect)."
        ),
        "min": 0,
        "max": 20,
        "step": 1,
        "type": "int",
    },
    "reanalysis_max_speakers": {
        "value": 0,
        "label": "Max Speakers",
        "description": "Maximum expected speakers (0 = no maximum).",
        "tooltip": (
            "Upper bound for auto-detection. Only used when Number of Speakers "
            "is set to 0 (auto-detect)."
        ),
        "min": 0,
        "max": 20,
        "step": 1,
        "type": "int",
    },
    "reanalysis_merge_gap": {
        "value": 0.8,
        "label": "Merge Gap",
        "unit": "s",
        "description": "Merge same-speaker segments closer than this.",
        "tooltip": (
            "After diarization, consecutive segments from the same speaker "
            "with a gap shorter than this are merged into one.<br><br>"
            "<b>Lower values</b>: More granular segments.<br>"
            "<b>Higher values</b>: Fewer, longer segments."
        ),
        "min": 0.0,
        "max": 3.0,
        "step": 0.1,
        "type": "number",
    },
    "reanalysis_use_live_diarization": {
        "value": 0,
        "label": "Use Live Diarization Settings",
        "description": "Copy diarization thresholds from the live settings.",
        "tooltip": (
            "When enabled, the reanalysis pipeline uses the same diarization "
            "thresholds (activity, centroid update, new speaker) as the live "
            "pipeline instead of the reanalysis-specific values below.<br><br>"
            "Enable this for consistent behavior between live and reanalysis."
        ),
        "min": 0,
        "max": 1,
        "step": 1,
        "type": "toggle",
        "inverts_siblings": True,
    },
    "reanalysis_clustering_threshold": {
        "value": 0.70,
        "label": "Clustering Threshold",
        "description": "Speaker clustering sensitivity.",
        "tooltip": (
            "Controls how different voices must be to be assigned separate "
            "speaker labels. This is the agglomerative clustering distance "
            "threshold.<br><br>"
            "<b>Lower values</b>: Create more speakers (more sensitive to "
            "voice differences).<br>"
            "<b>Higher values</b>: Merge similar voices into fewer speakers."
        ),
        "min": 0.1,
        "max": 0.95,
        "step": 0.05,
        "type": "number",
    },
    "reanalysis_min_duration_off": {
        "value": 0.3,
        "label": "Min Silence Duration",
        "unit": "s",
        "description": "Minimum silence before a speaker turn can end.",
        "tooltip": (
            "After the segmentation model detects a speaker stops talking, "
            "it must stay silent for at least this long before the turn is "
            "considered over.<br><br>"
            "<b>0</b>: Turns end immediately when speech stops.<br>"
            "<b>Higher values</b>: Require longer silence before closing "
            "a turn, reducing fragmentation but potentially merging distinct "
            "utterances."
        ),
        "min": 0.0,
        "max": 2.0,
        "step": 0.1,
        "type": "number",
    },
}


def get_reanalysis_defaults() -> dict:
    """Return a flat dict of param_name -> default_value for reanalysis parameters."""
    return {k: v["value"] for k, v in REANALYSIS_DEFAULTS.items()}


# ── Diarization Presets ──────────────────────────────────────────────────────

DIARIZATION_PRESETS = {
    "responsive": {
        "label": "Responsive",
        "description": "Faster speaker detection - may create extra speakers in noisy audio",
        "values": {
            "step_seconds": 0.15,
            "duration_seconds": 4.0,
            "tau_active": 0.45,
            "rho_update": 0.35,
            "delta_new": 0.55,
            "merge_gap_seconds": 0.10,
        },
    },
    "balanced": {
        "label": "Balanced (Default)",
        "description": "Optimized for typical 2-6 person meetings - recommended",
        "values": {
            "step_seconds": 0.25,
            "duration_seconds": 5.0,
            "tau_active": 0.5,
            "rho_update": 0.25,
            "delta_new": 0.65,
            "merge_gap_seconds": 0.15,
        },
    },
    "conservative": {
        "label": "Conservative",
        "description": "Fewer speakers - prefer merging over splitting",
        "values": {
            "step_seconds": 0.30,
            "duration_seconds": 5.0,
            "tau_active": 0.55,
            "rho_update": 0.20,
            "delta_new": 0.75,
            "merge_gap_seconds": 0.25,
        },
    },
    "large_meeting": {
        "label": "Large Meeting",
        "description": "Tuned for 5+ speakers - more sensitive to new voices",
        "values": {
            "step_seconds": 0.20,
            "duration_seconds": 5.0,
            "tau_active": 0.45,
            "rho_update": 0.30,
            "delta_new": 0.55,
            "merge_gap_seconds": 0.12,
        },
    },
    "custom": {
        "label": "Custom",
        "description": "Manually configure all parameters",
        "values": {},
    },
}

DIARIZATION_DEFAULT_PRESET = "balanced"


_ALL_DEFAULTS_DICTS = (
    TRANSCRIPTION_DEFAULTS, DIARIZATION_DEFAULTS,
    AUTO_GAIN_DEFAULTS, ECHO_CANCELLATION_DEFAULTS, SCREEN_RECORDING_DEFAULTS,
)


def get_all_defaults() -> dict:
    """Return a flat dict of param_name -> default_value for all parameters."""
    flat = {}
    for d in _ALL_DEFAULTS_DICTS:
        for key, spec in d.items():
            flat[key] = spec["value"]
    return flat


def get_default(key: str):
    """Return the default value for a single parameter, or None."""
    for d in _ALL_DEFAULTS_DICTS:
        if key in d:
            return d[key]["value"]
    return None


def preset_keys(presets: dict) -> set[str]:
    """Union of parameter keys controlled by any variant in a preset dict."""
    keys: set[str] = set()
    for p in presets.values():
        keys.update((p.get("values") or {}).keys())
    return keys


def _screen_preset_overrides(preset_name: str) -> dict:
    """Translate a screen_recorder preset name into audio_params keys.

    Screen presets live in screen_recorder.py with a different shape
    (framerate/crf/preset/scale) than audio_params (screen_framerate/etc).
    Lazy-imported to avoid a circular dep at module load.
    """
    if not preset_name or preset_name == "custom":
        return {}
    try:
        from capture_video import PRESETS as _SCREEN_PRESETS, H264_PRESETS as _H264
    except Exception:
        return {}
    if preset_name not in _SCREEN_PRESETS:
        return {}
    p = _SCREEN_PRESETS[preset_name]
    h264_idx = _H264.index(p["preset"]) if p.get("preset") in _H264 else 2
    scale = p.get("scale") or ""
    scale_w = int(scale.split(":")[0]) if scale else 0
    return {
        "screen_framerate": p["framerate"],
        "screen_crf": p["crf"],
        "screen_h264_preset": h264_idx,
        "screen_scale_width": scale_w,
    }


def resolve_audio_params(settings_dict: dict | None = None) -> dict:
    """Compute the effective audio_params for the current presets.

    Resolution order per parameter:
      1. Default value from this file.
      2. Saved per-key value in ``audio_params``.
      3. Active preset value, if the active preset for the section is a
         known non-custom preset.

    Effect: when a non-custom preset is selected, that preset's values are
    *the* source of truth — updates to the preset definitions in this file
    propagate to all users on that preset on next read, without any
    migration. Per-key overrides only take effect when the section is set
    to ``"custom"``.
    """
    if settings_dict is None:
        from core import settings as _settings
        settings_dict = _settings.load()

    out = get_all_defaults()
    saved = settings_dict.get("audio_params", {}) or {}
    out.update(saved)

    t_preset = settings_dict.get("transcription_preset", TRANSCRIPTION_DEFAULT_PRESET)
    if t_preset in TRANSCRIPTION_PRESETS and t_preset != "custom":
        out.update(TRANSCRIPTION_PRESETS[t_preset].get("values") or {})

    d_preset = settings_dict.get("diarization_preset", DIARIZATION_DEFAULT_PRESET)
    if d_preset in DIARIZATION_PRESETS and d_preset != "custom":
        out.update(DIARIZATION_PRESETS[d_preset].get("values") or {})

    out.update(_screen_preset_overrides(settings_dict.get("screen_preset")))

    return out
