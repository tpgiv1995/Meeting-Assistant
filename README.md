# Meeting Assistant

> Real-time meeting transcription, speaker identification, and AI-powered analysis - running entirely on your machine. No audio leaves your computer.

---

## Overview

Meeting Assistant captures desktop and microphone audio simultaneously, transcribes speech in real time using OpenAI's Whisper, identifies individual speakers through neural diarization, and generates live AI summaries. All processing happens locally except for AI text analysis (Anthropic Claude or OpenAI GPT). Sessions are stored locally with full audio playback, searchable transcripts, and an interactive chat interface for querying meeting content.

---

## Features

### Core

| | |
|---|---|
| **Live Transcription** | Real-time speech-to-text via faster-whisper with configurable model sizes |
| **Speaker Diarization** | Neural speaker identification using pyannote/diart streaming pipeline |
| **AI Summaries** | Auto-updating summaries that adapt structure to meeting content |
| **Chat Interface** | Ask questions about the meeting during or after - with timestamp references |
| **Session Management** | Organized session history with folders, search, and full audio playback |
| **Voice Library** | Cross-session speaker fingerprinting that learns and improves over time |
| **Analytics Dashboard** | Real-time speaking time distribution, segment counts, and timeline visualization |
| **System Tray** | Control recording and monitor status without opening the browser |

### Transcript Tools

- **Transcript Navigator** - search with match navigation, speaker filtering pills, dual-handle time range slider
- **Speaker Manager** - rename, recolor, merge speakers with multi-select and bulk operations
- **Noise Detection** - automatic identification and filtering of filler words, laughter, and crosstalk fragments
- **Playback Sync** - audio playback tracks the transcript with filtered speaker skipping
- **Timestamp Linking** - click any `[M:SS]` reference in summaries to jump to that moment

---

## Requirements

| Requirement | Details |
|---|---|
| **Operating System** | Windows 10/11 (WASAPI loopback) **or** macOS 12+ on Apple Silicon (BlackHole loopback) |
| **Python** | 3.10 or higher - [python.org/downloads](https://www.python.org/downloads/) (check "Add to PATH") |
| **AI API Key** | [Anthropic](https://console.anthropic.com/settings/keys) or [OpenAI](https://platform.openai.com/api-keys) - for summaries and chat |
| **HuggingFace Token** | *(Optional)* - enables speaker diarization ([get one here](https://huggingface.co/settings/tokens)) |
| **GPU acceleration** | *(Optional)* - NVIDIA GPU with CUDA on Windows, or Apple Silicon Metal on macOS. Falls back to CPU otherwise. |
| **macOS audio loopback** | [BlackHole 2ch](https://existential.audio/blackhole/) - auto-installed via Homebrew on first launch |

---

## Quick Start

**Windows:**
```
1. Install Python 3.10+ (add to PATH)
2. Double-click launch.bat
3. Enter your API key in Settings
4. Hit Record
```

**macOS (Apple Silicon):**
```
1. Install Homebrew if you don't have it: https://brew.sh
2. brew install ffmpeg blackhole-2ch    # may prompt for sudo on the BlackHole pkg step
3. sudo killall coreaudiod              # register the BlackHole driver without rebooting
4. ./launch.command                     # or: python launch.py
5. Enter your API key in Settings
6. Hit Record
```

The launcher handles everything on first run — virtual environment creation, accelerator detection (CUDA on Windows, Metal/MPS on macOS), PyTorch + Whisper backend installation (`faster-whisper` on Windows/Linux, `mlx-whisper` on macOS), model downloads, BlackHole aggregate device setup (macOS), and browser launch. Subsequent starts are fast.

### Updating

The program can be updated from the web page via the Settings pane. Or from the command line with the following:

```bash
git pull
```

Then run `launch.bat`. Dependencies are installed automatically if `requirements.txt` has changed.

---

## Architecture

### Transcription Pipeline

```
Audio Input (WASAPI)
    │
    ├── Loopback (desktop audio)─────────┐
    └── Microphone (WASAPI or browser) ──┤
                                         ▼
                              Source-Gated Mixer
                           (echo/duplicate prevention)
                                         │
                                         ▼
                              Silence Detection & Chunking
                           (0.3s pause trigger, 10s max buffer)
                                         │
                                         ▼
                              Speaker Diarization (diart)
                           (5s window, 0.5s step, streaming)
                                         │
                                         ▼
                              Whisper Transcription
                           (per-speaker audio segments)
                                         │
                         ┌───────────────┼───────────────┐
                         ▼              ▼               ▼
                  Hallucination     Noise/Filler     Segment Merge
                   Detection        Detection      (same speaker,
                 (n-gram ratio)   (filler words,     gap < 2s)
                                   fragments)
                         │               │               │
                         └───────┬───────┘               │
                                 ▼                       │
                           SSE Push to Frontend ◄────────┘
```

### Audio Capture

- **Windows backend:** `pyaudiowpatch` (PyAudio fork) for native WASAPI loopback. System audio is captured directly without any virtual driver.
- **macOS backend:** `sounddevice` + `BlackHole 2ch` virtual audio device. The launcher creates a multi-output aggregate device that fans system audio to both your speakers (so you still hear it) and BlackHole's input (which we capture). System default output is auto-switched to the aggregate during recording and restored on stop.
- **Microphone:** WASAPI input device on Windows, AVFoundation on macOS, or browser `getUserMedia` injection.
- **Source-gated mixing:** Prevents echo duplication by analyzing RMS levels — when one source dominates, the other is suppressed.
- **Sample rate:** Device native (typically 48 kHz), resampled to 16 kHz for Whisper via `scipy.signal.resample_poly`.
- **Recording format:** WAV, 16-bit signed integer, mono.

### Transcription

The Whisper engine is selected per platform — `ml/transcriber_engine.py` is the factory.

| Platform | Engine | Backend |
|---|---|---|
| Windows / Linux + NVIDIA GPU | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (CTranslate2) | CUDA float16 |
| macOS Apple Silicon | [mlx-whisper](https://github.com/ml-explore/mlx-examples/tree/main/whisper) | Metal fp16 |
| Any | faster-whisper | CPU int8 |

The model picker only shows presets the current OS can actually run (via the `platforms` tuple on each preset row).

| Preset | Model | Compute | Available on |
|---|---|---|---|
| GPU - Large | `large-v3` | CUDA float16 | Windows / Linux |
| GPU - Medium | `medium` | CUDA float16 | Windows / Linux |
| GPU - Small | `small` | CUDA float16 | Windows / Linux |
| Metal - Large | `mlx-community/whisper-large-v3-mlx` | Metal fp16 | macOS |
| Metal - Medium | `mlx-community/whisper-medium-mlx` | Metal fp16 | macOS |
| Metal - Small | `mlx-community/whisper-small-mlx` | Metal fp16 | macOS |
| CPU - Medium | `medium` | CPU int8 | All |
| CPU - Small | `small` | CPU int8 | All |
| CPU - Tiny | `tiny` | CPU int8 | All |

**Optimizations:**
- **Rolling context:** 800 characters of prior text passed as `initial_prompt` to maintain coherence across chunks
- **Hallucination detection:** N-gram repetition ratio analysis (4-gram, threshold 0.35) catches and discards Whisper's tendency to loop on silence or noise
- **Context recovery:** Automatically retries transcription without context prompt if compression ratio exceeds 2.0
- **VAD integration:** `min_silence_duration_ms=300`, `speech_pad_ms=150` when diarization is disabled

### Speaker Diarization (diart + pyannote)

[diart](https://github.com/juanmc2005/diart) provides **online streaming diarization** - speaker identification happens incrementally as audio arrives, not as a batch process after recording.

| Component | Model | Purpose |
|---|---|---|
| Segmentation | `pyannote/segmentation-3.0` | Determines voice activity regions |
| Embedding | `pyannote/wespeaker-voxceleb-resnet34-LM` | 256-dim speaker voice vectors |

**Configuration:**
- **Window:** 5-second rolling buffer with 0.5-second step advancement
- **Latency:** ~50ms/step on GPU, ~165ms/step on CPU
- **Voice activity threshold:** `tau_active=0.5`
- **New speaker threshold:** `delta_new=0.5` (minimum embedding distance to create a new speaker)
- **Centroid update rate:** `rho_update=0.422`

**Processing:** Overlapping segments are deduplicated (first speaker by start time wins). Same-speaker segments within 100ms gaps are merged. Speaker labels (`Speaker 1`, `Speaker 2`, etc.) remain consistent throughout the session.

**Requirement:** A HuggingFace token and acceptance of the [pyannote model terms](https://huggingface.co/pyannote/segmentation-3.0). The default `.env` template includes a token with public model download access.

### Noise Detection

Short filler segments are automatically detected and labeled as `[Noise]` to reduce transcript clutter in multi-speaker meetings. Detection criteria:

- Single filler words (um, uh, yeah, okay, etc.)
- Two-word filler combinations (e.g., "Sorry. Yeah.")
- Trailing fragments of 3 words or fewer ending in "..."
- Laughter patterns and pure punctuation
- Very short duration (< 1.5s) with minimal word count

**Speaker-aware:** Noise labeling only applies to speakers who haven't yet produced substantive content. Once a speaker produces a real segment, any earlier noise segments from that speaker are retroactively reclaimed and relabeled with their correct speaker identity.

Noise segments are hidden by default and can be toggled visible via the Noise pill in the Transcript Navigator.

---

## Voice Library

The Voice Library provides **cross-session speaker identification** - once you name a speaker in one meeting, they're automatically recognized in future meetings.

### How It Works

1. **Embedding extraction:** During recording, 256-dimensional speaker embeddings are extracted from 2.5+ seconds of speech using the wespeaker-voxceleb-resnet34-LM model
2. **Centroid matching:** New embeddings are compared against stored global speaker profiles using cosine similarity
3. **Auto-apply:** Matches with cosine similarity ≥ 0.82 are applied silently
4. **Suggest:** Matches between 0.70–0.82 trigger a confirmation toast for the user
5. **Learning:** Renaming speakers, confirming matches, and overriding segment labels all feed new embeddings back into the voice library

### Profile Management

- **Automatic creation:** Renaming a speaker during a session creates or updates their global voice profile
- **Manual profiles:** Create empty profiles in advance and link them during recording
- **Merge profiles:** Combine duplicate identities - all embeddings are reassigned and the centroid is recomputed
- **Optimize:** Prune redundant embeddings (keeps newest 30 per profile)
- **Bulk operations:** Multi-select profiles for bulk delete, merge, or optimize
- **Search & filter:** Find profiles by name in the Voice Library panel

### Storage

Voice data is stored in the same SQLite database alongside sessions:

| Table | Contents |
|---|---|
| `global_speakers` | Profile name, color, centroid vector, embedding count |
| `speaker_embeddings` | Individual 256-dim vectors with session/speaker provenance |
| `speaker_labels` | Per-session speaker-to-profile links |

---

## Session Management

### Folders

Sessions can be organized into folders via the sidebar. Drag sessions between folders, rename folders, or delete them (sessions are moved back to the root, not deleted).

### Session Lifecycle

1. **Start recording** - a new session is created with a UUID
2. **During recording** - transcript segments, summaries, and audio are saved continuously
3. **Stop recording** - the session is finalized and receives an AI-generated title
4. **After recording** - browse history, replay audio synced to transcript, chat about content, or reanalyze with different settings

### Reanalyze

Re-run the full transcription and diarization pipeline on a completed session's saved audio. Useful when:
- You've upgraded to a better Whisper model
- You want to try different diarization settings
- The original transcription had issues

---

## Analytics Dashboard

The analytics panel (chart icon in the transcript header) provides real-time session statistics:

- **Donut chart** - visual speaking time distribution across all speakers
- **KPI cards** - session duration, speaker count, total segments, average words per minute
- **Timeline swimlanes** - animated participation chart showing when each speaker was active
- **Speaking time bars** - horizontal bar chart with duration and percentage share
- **Segment count bars** - segment distribution across speakers

All charts animate on scroll with staggered reveal effects.

---

## AI Integration

### Providers

Meeting Assistant supports two AI providers, switchable at runtime via Settings:

| Provider | Models | Notes |
|---|---|---|
| **Anthropic** | Claude Opus 4.6, Sonnet 4.6, Haiku 4.5 | Default; supports extended thinking and tool use |
| **OpenAI** | GPT-5.4, GPT-5.3, GPT-4o, GPT-4o mini, o4-mini | JSON response format for structured outputs |

### Summary Generation

- **Incremental updates:** Only sections with genuinely new high-level content are modified
- **Adaptive structure:** No rigid template - the summary structure adapts to meeting content
- **Timestamp markers:** Key moments include `[M:SS]` references that link to audio playback
- **Custom prompts:** Shape the summary with context like *"This is a quarterly board review"*
- **Auto-summary:** Triggers every 6 new segments (configurable, can be disabled)

### Chat

Full conversational interface with access to the complete transcript, summary, and session metadata. Responses stream in real time via SSE. The AI includes timestamp references so you can jump to relevant moments.

---

## System Tray

A notification-area icon provides quick access without opening the browser:

| Icon Color | State |
|---|---|
| Blue | Ready to record |
| Red | Recording in progress |
| Gray | Models loading |
| Amber | Setup required (missing API key) |

**Menu options:** Start/Stop recording, open web interface, configure API keys, quit.

Dynamic tooltips show the current status on hover.

---

## Privacy

**All audio processing happens locally.** No audio data ever leaves your machine.

The only outbound network calls are:
- **AI API** (Anthropic or OpenAI) - transcript text only, for summaries and chat
- **HuggingFace Hub** - model files downloaded once and cached locally

All data is stored in `data/` next to the application:

```
data/
├── meetings.db      ← SQLite database (sessions, transcripts, summaries, chat, voice profiles)
├── settings.json    ← User preferences
└── audio/           ← Recorded WAV files (one per session)
```

---

## Configuration

### Environment Variables (`.env`)

A `.env` file is created automatically from the template on first run:

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | If using Anthropic | API key for Claude summaries and chat |
| `OPENAI_API_KEY` | If using OpenAI | API key for GPT summaries and chat |
| `HUGGING_FACE_KEY` | Optional | Enables speaker diarization (default included for public models) |
| `PORT` | Optional | Server port (default: `6969`) |

### Settings Panel

Runtime-configurable options accessible from the web interface:

- **Audio devices** - loopback and microphone source selection
- **Whisper model** - auto-detected optimal preset or manual selection
- **Diarizer device** - GPU or CPU for speaker identification
- **AI provider & model** - switch between Anthropic and OpenAI at any time
- **Auto-summary** - toggle automatic summary generation
- **Custom prompt** - persistent context that shapes all AI outputs

---

## Troubleshooting

<details>
<summary><strong>"Loading model…" never goes away</strong></summary>
<br>

The Whisper model downloads on first run - `large-v3` is ~3 GB. Check the terminal window for download progress.

</details>

<details>
<summary><strong>No audio / flat visualizer</strong></summary>
<br>

- Click the refresh button next to the desktop device selector to re-scan audio devices
- Ensure something is actively playing - WASAPI loopback only captures active audio output
- Use **Test Audio** to verify sources before recording

</details>

<details>
<summary><strong>Speaker labels not appearing</strong></summary>
<br>

Speaker diarization requires a HuggingFace token. Add one in Settings. On first use, pyannote model files download automatically (~500 MB). You must also accept the [pyannote model terms](https://huggingface.co/pyannote/segmentation-3.0) on HuggingFace.

</details>

<details>
<summary><strong>GPU not detected</strong></summary>
<br>

- Ensure NVIDIA drivers are up to date
- The launcher auto-detects CUDA via `nvidia-smi` and installs the appropriate PyTorch build
- Check terminal output on startup for GPU detection messages
- CPU fallback is automatic - the app works without a GPU

</details>

<details>
<summary><strong>Repeated phrases in transcript (hallucination loops)</strong></summary>
<br>

Whisper can loop on silence or noise. The app includes multi-layer detection:
1. N-gram repetition ratio analysis discards looping segments
2. Context is automatically cleared when contamination is detected
3. Retry without prompt conditioning if compression ratio is abnormal

If it persists, try a smaller model or reduce background noise.

</details>

<details>
<summary><strong>Port conflict</strong></summary>
<br>

Set `PORT=7000` (or any available port) in your `.env` file.

</details>

---

## Project Structure

Code is organized into seven packages by responsibility. Alphabetical order follows the grouping (`capture_audio` and `capture_video` sit together; `ui_desktop` and `ui_web` sit together).

```
Meeting Assistant/
├── launch.bat             ← Windows entry point (double-click to run)
├── launch.command         ← macOS entry point
├── launch.py              ← Setup automation (venv, GPU, deps, launch)
├── app.py                 ← Flask server, SSE streaming, session state, API routes
├── requirements.txt       ← Windows/Linux Python dependencies
├── requirements-macos.txt ← macOS dependencies (mlx-whisper, pyobjc, arm64 torch)
├── .env                   ← API keys and configuration (gitignored)
│
├── ai/                    ← LLM assistant
│   └── assistant.py        — Summary generation and chat via Anthropic/OpenAI
│
├── capture_audio/         ← Audio input pipeline
│   ├── __init__.py         — Platform dispatcher (selects backend at import)
│   ├── windows.py          — WASAPI loopback + mic capture (DSP, mixer, AGC)
│   ├── mac.py              — BlackHole loopback + AVFoundation mic capture
│   ├── mac_bootstrap.py    — BlackHole install + aggregate device routing
│   ├── wav_writer.py       — WAV recording with sample-accurate timestamps
│   ├── params.py           — Default audio parameters and presets
│   └── audio/              — Bundled MP3s used by input-device auto-detection
│       ├── test_sample.mp3
│       └── complete.mp3
│
├── capture_video/         ← Screen recording + media editing
│   ├── __init__.py         — Platform dispatcher + cross-platform presets
│   ├── windows.py          — gdigrab + DPI-aware EnumDisplayMonitors
│   ├── mac.py              — AVFoundation screen capture
│   ├── ffmpeg_util.py      — find_ffmpeg / download_ffmpeg / kill_stale_ffmpeg
│   └── media_edit.py       — Trim, split, concatenate audio/video files
│
├── core/                  ← Foundational utilities (no domain knowledge)
│   ├── log.py              — Structured logging
│   ├── config.py           — .env management, API key status, first-run detection
│   ├── paths.py            — Data directory resolution (configurable via .data_location)
│   ├── settings.py         — JSON user preferences
│   ├── network.py          — HuggingFace token + pipeline download helpers
│   ├── compute_device.py   — CUDA/MPS/CPU probe (single source of truth)
│   └── storage.py          — SQLite CRUD (sessions, segments, summaries, chat)
│
├── ml/                    ← Transcription, diarization, speakers
│   ├── transcriber.py            — Streaming Whisper, pause-based flush logic
│   ├── transcriber_engine.py     — faster-whisper / mlx-whisper backend wrapper
│   ├── batch_transcriber.py      — Reanalysis pipeline (full-file pyannote + Whisper)
│   ├── diarizer.py               — pyannote streaming speaker diarization
│   ├── speaker_db.py             — Voice library: embeddings, cross-session matching
│   ├── text_embeddings.py        — Text embedding helpers for chat memory
│   ├── eval_diarization.py       — Diarization evaluation script
│   └── optimize_diarization.py   — Hyperparameter tuning script
│
├── ui_desktop/            ← Desktop OS integration
│   ├── tray.py             — System tray icon (pystray + Pillow)
│   └── notifications.py    — Toast/banner notifications (winotify / osascript)
│
├── ui_web/                ← Flask web UI assets
│   ├── templates/
│   │   ├── index.html       — Session SPA shell
│   │   └── home.html        — Home page
│   └── static/
│       ├── app.js           — Frontend application logic
│       ├── home.js          — Home page logic
│       ├── style.css        — Dark-theme CSS
│       └── images/          — Logo and tray icon assets
│
└── storage/               ← Runtime data + bundled binaries (gitignored, auto-created)
    ├── data/                — SQLite DB, settings.json, recorded audio/video,
    │                          attachments, screenshots, voice profiles
    │                          (location overridable via .data_location pointer)
    ├── models/              — HuggingFace model cache
    └── tools/               — ffmpeg(.exe), auto-downloaded if not on PATH
```

**Platform dispatch:** `capture_audio` and `capture_video` are packages whose `__init__.py` re-exports the active platform backend (`windows.py` on win32, `mac.py` on darwin). All callers do `from capture_audio import AudioCapture` and remain platform-agnostic.

---

## Dependencies

| Package | Purpose |
|---|---|
| `faster-whisper` | CTranslate2-optimized Whisper inference |
| `diart` | Online streaming speaker diarization |
| `torch` + `torchaudio` | PyTorch backend for neural models |
| `pyaudiowpatch` | Windows WASAPI audio capture with loopback |
| `anthropic` | Anthropic Claude API client |
| `openai` | OpenAI GPT API client |
| `flask` | Web server and API framework |
| `scipy` + `numpy` | Signal processing and resampling |
| `pystray` + `Pillow` | System tray icon and image processing |
| `python-dotenv` | Environment variable loading |
| `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` | CUDA runtime libraries (Windows GPU support) |
