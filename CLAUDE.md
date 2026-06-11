# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Meeting Assistant is a local Flask desktop app that captures system + microphone audio, runs streaming Whisper transcription, diart/pyannote speaker diarization, and Anthropic/OpenAI summaries — all from a single `app.py` Flask server fronted by a single SPA in `ui_web/static/app.js`. Audio never leaves the machine; only transcript text is sent to LLM providers.

Most architecture detail (transcription pipeline, audio backends, voice library, AI providers, data layout) is documented thoroughly in `README.md` — read it before making non-trivial changes. This file covers what the README doesn't.

## Running the app

The app is **launcher-managed**, not pip-installed. Don't run `pip install -r requirements.txt` or `python app.py` directly when starting fresh — the launcher decides which requirements file, which torch build (CUDA/MPS/CPU), and which Whisper backend (`faster-whisper` vs `mlx-whisper`) to install based on the detected platform.

```
launch.bat           # Windows — bootstraps uv, creates .venv\, runs launch.py
launch.command       # macOS — same role, bash
launch.py            # The actual setup automation (detects GPU, installs deps, then runs app.py)
start_dev.bat        # Windows dev shortcut: activates .venv and re-runs launch.py in a sticky cmd
```

`launch.py` is uv-driven (`uv pip install`). If `requirements.txt` hasn't changed since last run it's a no-op. After setup it `exec`s `app.py main()`.

Once the venv exists, you can call `.venv\Scripts\python.exe app.py` directly to skip the setup probe — useful when iterating on Python code without modifying dependencies. The launcher's GPU probing adds noticeable startup time.

Default port: **6969**. Override with `PORT=` in `.env`. Only one instance can run per port — `_handshake_existing_instance()` (`app.py:6198`) asks an idle instance on the same port to shut down before binding, and refuses to start if the existing instance has an active recording.

## Tests / lint

There are **no test suite, linter, or CI config** in this repo. Manual verification is the workflow — run the launcher, open `http://localhost:6969`, exercise the recording/transcript/chat flows. The two `ml/eval_diarization.py` and `ml/optimize_diarization.py` scripts are standalone evaluation/tuning tools, not pytest tests.

## Architecture beyond what README covers

### Single-process server with cooperative singletons

Despite the seven-package layout, `app.py` (6.3k lines) holds nearly all orchestration state as module-level singletons: `_transcriber`, `_audio_capture`, `_screen_recorder`, `_state` (current session dict), `_state_lock`, `_sse_clients`, `_tray`, `ai`, `fingerprint_db`. The packages under `core/`, `ml/`, `capture_audio/`, `capture_video/`, `ai/`, `ui_desktop/` are utilities — the `app.py` glue owns the lifecycle and the locks. When changing behavior, expect to touch `app.py` even if the logic lives in a package.

### SSE is the only push channel

Frontend state updates flow through one Server-Sent Events stream at `/api/events`. The Python side calls `_push(event, data)` (`app.py:214`) which fans out to all connected client queues. JS subscribes once on page load and dispatches by event name. There is no WebSocket and no client polling for recording state. When adding new realtime behavior, define a new SSE event name on both sides rather than inventing a separate transport.

### Platform dispatch happens at import time, not call time

`capture_audio/__init__.py` and `capture_video/__init__.py` re-export the active platform's module (`windows.py` or `mac.py`) by inspecting `sys.platform` at import. Callers do `from capture_audio import AudioCapture` and never reference the per-OS module. The two backends **intentionally duplicate** DSP/mixer logic so each can evolve independently — don't try to factor shared code between `capture_audio/windows.py` and `capture_audio/mac.py`. Linux raises ImportError for audio; video falls back to a stub that raises on use.

### The transcription pipeline is queue-based

`_audio_queue` (a `queue.Queue`) is the boundary between the audio thread and the ML thread. Audio capture pushes raw int16 chunks; `ml/transcriber.py` runs a background thread that pulls from the queue, runs silence-based chunking, hands chunks to diart (if loaded) for per-speaker segmentation, then to Whisper for transcription, and finally invokes `_on_segment` (`app.py:431`) for each finalized segment. `_on_segment` is the place where noise detection, segment merging, fingerprint extraction, SSE push, and storage write all happen — it's the natural integration point for transcript-stage features.

### Models load lazily on background threads

`_start_background_initializers()` (`app.py:845`) launches `_load_model`, `_load_diarizer`, `_load_fingerprint_db`, `_load_text_embeddings` as daemon threads **after** the Flask server is bound and accepting requests. The UI renders immediately with a "loading model" state and the status SSE event flips when each model is ready. Don't move heavy imports to top of file — startup time matters.

### Storage is SQLite with implicit migrations

`core/storage.py:init_db()` runs `CREATE TABLE IF NOT EXISTS` for the base schema then runs a flat list of `ALTER TABLE` / `CREATE TABLE` / `CREATE INDEX` migrations in a try/except-pass loop. Every database — fresh or upgraded — re-runs every migration on startup; idempotency is guaranteed by `IF NOT EXISTS` and by swallowing "duplicate column" errors. **When adding a column, append it to that list, don't edit the original `CREATE TABLE` block** — existing databases already have the base table and only see the ALTER.

The DB lives at `<data_dir>/meetings.db`. `data_dir` defaults to `storage/data/` but the user can relocate it via Settings; the override path is stored in `.data_location` at the project root (not inside the data dir, so we have a stable bootstrap pointer). All code paths must go through `core/paths.py` to resolve directories — never hardcode `storage/data/`.

### Voice library is a separate DB layer in the same SQLite file

`global_speakers`, `speaker_embeddings`, `unlabeled_embeddings`, `speaker_labels.global_id` form the cross-session voice fingerprint system. `ml/speaker_db.py` (the `SpeakerFingerprintDB` class) wraps embedding extraction (256-dim wespeaker vectors) and cosine-similarity matching. Auto-apply threshold is **0.82**, suggest threshold **0.70** — these are tuned, not arbitrary. Live matching is driven from `_on_fingerprint_audio` (`app.py:970`).

### The frontend is one ~17k-line `app.js`

`ui_web/static/app.js` is a hand-written vanilla-JS SPA with no build step, no framework, no module bundler. Edits go in directly and the browser picks them up (Flask is configured with `SEND_FILE_MAX_AGE_DEFAULT=0`). When extending UI features, follow the existing convention: top-level IIFE modules per panel, manual SSE event handlers, plain `fetch` for API calls. `index.html` is the session view shell; `home.html` is the home page.

### Provider switching is hot

`ai/assistant.py:AIAssistant` supports Anthropic and OpenAI as drop-in providers via `reload_client(provider, model)`. The active provider/model lives in `settings.json` and changes at runtime through `/api/settings/ai` without restarting. All summary/chat code should go through `ai` rather than instantiating provider clients directly.

The `core/config.py` import has a side effect: it installs `truststore` into the TLS stack so provider clients trust the OS certificate store (matters for corporate proxies/Cloudflare WARP). `ai/assistant.py` re-imports `core.config` explicitly to make that side effect ordering-independent.

### Auto-record watches mic usage, not meeting apps

`ui_desktop/call_watch.py` polls two signals every 2s and merges them: **WASAPI capture sessions** (primary — run in a disposable subprocess, `mic_session_worker.py`) and the **ConsentStore registry** (`HKCU\...\CapabilityAccessManager\ConsentStore\microphone`, fallback). The registry is only updated live for classic Win32 apps; packaged (MSIX) apps — **including new Teams** — get their timestamps written when the call *ends*, so the registry alone can never see them mid-call. Session enumeration sees both kinds, but COM enumeration once took the whole app down with a native fault while in-process WASAPI capture was live, hence the worker subprocess: if it dies or hangs, the watcher kills it, falls back to registry-only, and respawns after 60s.

The watcher only detects and debounces; all recording policy lives in `_auto_record_tick` in `app.py`, which starts/stops recordings via internal HTTP POSTs to the app's own `/api/recording/*` endpoints so the auto path is byte-identical to clicking Record. Self-exclusion matters in both signals (or an auto-started recording reads as an ongoing call and keeps itself alive forever): the worker excludes the app's pid and its children; the registry path excludes the real process image path (`GetModuleFileNameW`, junction-resolved — uv venv shims won't match `sys.executable`).

### System tray and main app are decoupled

`ui_desktop/tray.py` runs in a daemon thread, polls a `_state_snapshot()` callback for icon/tooltip state, and calls back into `app.py` for start/stop/quit actions. The tray must run on a Python-side thread (not the main thread) so the main thread can receive SIGINT — pystray's Win32 message loop blocks in native C and would eat Ctrl+C otherwise. Don't move tray to the main thread.

## Conventions worth knowing

- **Logging:** use `from core import log` and `log.info("tag", "msg")` / `log.warn` / `log.error` — not `print` and not `logging.getLogger`. The `tag` is a free-form short string like `"app"`, `"storage"`, `"diarizer"`.
- **Speaker keys vs names vs global IDs:** `speaker_key` is the per-session diart label (`"Speaker 1"`). `name` is the user-facing display name (e.g. `"Pat"`). `global_id` is the cross-session voice library row id. Keep these distinct — `_is_default_speaker_name()` and `_is_custom_speaker_key()` in `app.py` exist precisely because the three concepts collide.
- **Timestamps:** transcript segments store `start_time`/`end_time` as float seconds relative to session start. UI displays `[M:SS]` via `_fmt_time` and `_fmt_segment`.
- **Settings vs env:** API keys live in `.env`; user preferences (provider, model, prompts, device choices, custom summary template) live in `settings.json` via `core/settings.py`. Don't mix the two — `.env` is bootstrap config, `settings.json` is runtime state.
- **ffmpeg:** auto-downloaded into `storage/tools/` if not on PATH. Use `find_ffmpeg()` from `capture_video` rather than assuming a system install.
- **Comments:** the existing code is sparingly commented — comments explain *why* (a non-obvious constraint, an OS quirk, a tuning rationale), never *what*. Match that style.
