"""
Meeting Assistant launcher.
Handles dependency installation then starts the app.
Run via launch.bat (double-click) or directly: python launch.py
"""
import ctypes
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# ── ANSI colour setup ─────────────────────────────────────────────────────────

def _enable_ansi():
    if sys.platform == "win32":
        try:
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)
        except Exception:
            pass

_enable_ansi()

R   = "\033[0m"
B   = "\033[1m"
DIM = "\033[2m"
RED = "\033[91m"
GRN = "\033[92m"
YLW = "\033[93m"
CYN = "\033[96m"
GRY = "\033[90m"

SEP_HEAVY = f"{CYN}{B}  {'=' * 54}{R}"
SEP_LIGHT = f"{GRY}  {'-' * 52}{R}"


def _ok(msg):   print(f"{GRN}  [OK] {msg}{R}")
def _info(msg): print(f"{GRY}       {msg}{R}")
def _warn(msg): print(f"{YLW}  [!!] {msg}{R}")
def _err(msg):  print(f"{RED}  [XX] {msg}{R}")

def _section(title):
    print()
    print(f"{B}  {title}{R}")
    print(SEP_LIGHT)

def _fatal(msg):
    print()
    _err(msg)
    print()
    input("Press Enter to exit...")
    sys.exit(1)

# ── uv helpers ────────────────────────────────────────────────────────────────

UV = ""  # resolved in main()

def _find_uv() -> str:
    """Locate the uv binary."""
    found = shutil.which("uv")
    if found:
        return found
    # Common install locations on Windows
    for candidate in [
        Path.home() / ".local" / "bin" / "uv.exe",
        Path.home() / ".local" / "bin" / "uv",
    ]:
        if candidate.exists():
            return str(candidate)
    return ""


def _uv(*args, show_output=False):
    """Run uv pip install with the given args. Returns True on success."""
    cmd = [UV, "pip", "install"]
    if not show_output:
        cmd.append("--quiet")
    cmd.extend(args)
    return subprocess.run(cmd).returncode == 0


def _uv_streaming(*args):
    """
    Run uv pip install and print filtered progress lines in real time.
    """
    cmd = [UV, "pip", "install"] + list(args)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    for raw in proc.stdout:
        line = raw.strip()
        if not line:
            continue
        lo = line.lower()
        if any(k in lo for k in ("downloading", "downloaded", "prepared",
                                   "resolved", "installed", "updated",
                                   "uninstalled")):
            print(f"{GRY}         {line}{R}", flush=True)
        elif "error" in lo:
            print(f"{RED}         {line}{R}", flush=True)
        elif line.startswith("+") or line.startswith("-"):
            print(f"{GRY}         {line}{R}", flush=True)
    proc.wait()
    return proc.returncode == 0


# ── Venv helpers ──────────────────────────────────────────────────────────────

VENV_DIR = Path(__file__).parent / ".venv"


def _venv_python() -> Path:
    """Return the expected Python executable path inside the venv."""
    if sys.platform == "win32":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def _running_in_venv() -> bool:
    """True when this process IS the venv Python."""
    try:
        return Path(sys.executable).resolve() == _venv_python().resolve()
    except Exception:
        return False


def _venv_python_version() -> tuple[int, int] | None:
    """Return (major, minor) of the Python that created the venv, or None."""
    cfg = VENV_DIR / "pyvenv.cfg"
    if not cfg.exists():
        return None
    try:
        text = cfg.read_text()
        m = re.search(r"version\s*=\s*(\d+)\.(\d+)", text)
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return None


def _ensure_venv():
    """
    If not already running inside the venv, create (or rebuild) it, then
    re-exec this script using the venv Python.  Does not return on success.
    """
    if _running_in_venv():
        return  # already inside the venv - nothing to do

    need_create = not _venv_python().exists()

    # Staleness check: venv built with a different Python minor version
    if not need_create:
        venv_ver = _venv_python_version()
        cur_ver  = (sys.version_info.major, sys.version_info.minor)
        if venv_ver and venv_ver != cur_ver:
            print()
            _warn(
                f"Environment was built with Python {venv_ver[0]}.{venv_ver[1]} "
                f"but you're running {cur_ver[0]}.{cur_ver[1]}."
            )
            answer = input("      Rebuild environment? [Y/n]: ").strip().lower()
            if answer in ("", "y", "yes"):
                _info("Removing old environment...")
                shutil.rmtree(VENV_DIR, ignore_errors=True)
                need_create = True
            else:
                _warn("Skipping rebuild - things may not work correctly.")

    if need_create:
        _info("Creating Python environment (one-time setup)...")
        uv = _find_uv()
        if uv:
            r = subprocess.run([uv, "venv", str(VENV_DIR), "--python", "3.12", "--seed"])
        else:
            r = subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)])
        if r.returncode != 0:
            _fatal("Failed to create virtual environment.")

    # Re-exec using venv Python
    os.execv(str(_venv_python()), [str(_venv_python())] + sys.argv)


# ── Start Menu shortcut ───────────────────────────────────────────────────────

_SHORTCUT_NAME = "Meeting Assistant.lnk"


def _create_start_menu_shortcut():
    """
    Ensures a Start Menu shortcut exists and points to the current launch.bat
    with the right working directory and icon. Reads the existing shortcut's
    properties via PowerShell and only re-saves when something is missing or
    stale, so we don't churn the .lnk on every launch. Self-heals shortcuts
    that were created before files moved (e.g. logo.ico relocating into
    ui_web/static/images/ during the package reorganization).
    No-ops silently on non-Windows.
    """
    if sys.platform != "win32":
        return

    root       = Path(__file__).parent
    bat_path   = root / "launch.bat"
    icon_path  = root / "ui_web" / "static" / "images" / "logo.ico"
    start_menu = (
        Path(os.environ.get("APPDATA", ""))
        / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    )
    lnk_path = start_menu / _SHORTCUT_NAME

    if not bat_path.exists():
        return

    def _norm(p: str) -> str:
        try:
            return str(Path(p)).lower()
        except Exception:
            return (p or "").lower()

    # ── Check whether the existing shortcut already points here ──────────────
    already_correct = False
    if lnk_path.exists():
        try:
            # Read all four fields we care about. Sentinel between fields keeps
            # us safe even if one of them is empty.
            ps_read = (
                "$ws = New-Object -ComObject WScript.Shell; "
                f"$s  = $ws.CreateShortcut('{lnk_path}'); "
                "Write-Output $s.TargetPath; Write-Output '---'; "
                "Write-Output $s.Arguments; Write-Output '---'; "
                "Write-Output $s.WorkingDirectory; Write-Output '---'; "
                "Write-Output $s.IconLocation"
            )
            check = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_read],
                capture_output=True, text=True,
            )
            if check.returncode == 0:
                parts = [p.strip() for p in check.stdout.split("---")]
                if len(parts) >= 4:
                    cur_target, cur_args, cur_wd, cur_icon = parts[:4]
                    cur_icon_file = cur_icon.split(",", 1)[0].strip() if cur_icon else ""
                    target_ok = "cmd.exe" in cur_target.lower()
                    args_ok   = str(bat_path) in cur_args
                    wd_ok     = _norm(cur_wd) == _norm(str(root))
                    if icon_path.exists():
                        # Icon must point at the right path AND that path must
                        # still resolve — catches the case where the icon was
                        # moved out from under a previously-valid shortcut.
                        icon_ok = (
                            _norm(cur_icon_file) == _norm(str(icon_path))
                            and Path(cur_icon_file).exists()
                        )
                    else:
                        # No icon to install — leave whatever is there alone.
                        icon_ok = True
                    if target_ok and args_ok and wd_ok and icon_ok:
                        already_correct = True
        except Exception:
            pass

    # Re-save the shortcut if the icon file is newer than the .lnk, so icon
    # updates to logo.ico actually propagate (Windows keys its icon cache off
    # the .lnk's mtime).
    if already_correct and icon_path.exists():
        try:
            if icon_path.stat().st_mtime > lnk_path.stat().st_mtime:
                already_correct = False
        except Exception:
            pass

    if already_correct:
        return

    # ── Create or update the shortcut ────────────────────────────────────────
    was_existing = lnk_path.exists()
    ps_script = (
        f"$ws = New-Object -ComObject WScript.Shell; "
        f"$s  = $ws.CreateShortcut('{lnk_path}'); "
        f"$s.TargetPath       = 'cmd.exe'; "
        f"$s.Arguments        = '/c \"\"{bat_path}\"\"'; "
        f"$s.WorkingDirectory = '{root}'; "
        f"$s.WindowStyle      = 7; "          # 7 = minimised
        + (f"$s.IconLocation = '{icon_path}, 0'; " if icon_path.exists() else "")
        + "$s.Save()"
    )

    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            verb = "updated" if was_existing else "created"
            _ok(f"Start Menu shortcut {verb}")
        else:
            _warn("Could not update Start Menu shortcut (non-fatal)")
    except Exception:
        _warn("Could not update Start Menu shortcut (non-fatal)")


# ── Storage layout migration ──────────────────────────────────────────────────

def _migrate_legacy_layout():
    """One-shot migration to the storage/ folder layout.

    Moves legacy project-root directories into ``storage/``:
      <project>/tools/   → <project>/storage/tools/
      <project>/models/  → <project>/storage/models/
      <project>/data/    → <project>/storage/data/   (only if at default location)

    The data folder is only migrated when it's still at the original default
    location — i.e. either ``.data_location`` is absent, or it points to
    ``<project>/data``. If the user has relocated their data via the System
    settings (pointer points elsewhere), we leave both the data folder and
    the pointer alone.

    After moving the default-located data folder, the pointer file is
    removed so ``core.paths.data_dir()`` resolves to the new default
    (``storage/data/``) without an explicit pointer.

    Idempotent — safe to call on every launch. Silent when there's nothing
    to do.
    """
    project_root = Path(__file__).parent
    storage_dir = project_root / "storage"
    pointer_file = project_root / ".data_location"

    moved: list[str] = []

    # tools/ and models/ are unconditional — always inside the project root.
    for name in ("tools", "models"):
        src = project_root / name
        dst = storage_dir / name
        if src.is_dir() and not dst.exists():
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                moved.append(f"{name}/ -> storage/{name}/")
            except OSError as exc:
                _warn(f"Could not migrate {name}/ to storage/: {exc}")

    # data/ requires the pointer check.
    src_data = project_root / "data"
    dst_data = storage_dir / "data"
    if src_data.is_dir() and not dst_data.exists():
        # Mirror core.paths._read_pointer: empty / non-absolute / unreadable
        # pointer files are treated as "no pointer" — i.e. data is at the
        # default location and should be migrated. This keeps migration in
        # lockstep with how paths.py itself resolves the data dir.
        pointer_target: Path | None = None
        if pointer_file.exists():
            try:
                text = pointer_file.read_text(encoding="utf-8").strip()
                if text:
                    p = Path(text)
                    if p.is_absolute():
                        pointer_target = p
            except (OSError, UnicodeDecodeError, ValueError):
                # Unreadable / corrupt pointer file — treat as "no pointer".
                # Mirrors paths._read_pointer's defensive posture.
                pointer_target = None

        try:
            is_default_location = (
                pointer_target is None
                or pointer_target.resolve() == src_data.resolve()
            )
        except OSError:
            # If the pointer can't be resolved (broken path on a missing
            # volume, etc.), assume the user has data elsewhere and play
            # it safe — leave both data/ and the pointer alone.
            is_default_location = False

        if is_default_location:
            try:
                dst_data.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src_data), str(dst_data))
                moved.append("data/ -> storage/data/")
            except OSError as exc:
                _warn(f"Could not migrate data/ to storage/data/: {exc}")
            else:
                # Migration succeeded: the new default IS storage/data/, so
                # the pointer becomes redundant. Best-effort cleanup — failing
                # to delete the pointer (e.g. it's a corrupt directory) does
                # NOT undo the successful data move and shouldn't surface as
                # a migration error to the user.
                try:
                    pointer_file.unlink()
                except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
                    pass

    if moved:
        _section("STORAGE LAYOUT")
        for line in moved:
            _ok(f"Migrated  {GRY}{line}{R}")


# ── GPU detection ─────────────────────────────────────────────────────────────

TORCH_INDEX = "https://download.pytorch.org/whl/"

def _detect_gpu():
    """Return (wheel_tag, gpu_name, cuda_ver_str) or ('cpu', '', '').

    On macOS the result is ('mps', <chip>, '') if Apple Silicon Metal is
    available, else ('cpu', '', ''). The 'mps' tag is purely informational —
    we don't pin a torch wheel on Mac because the default arm64 wheel is
    Metal-enabled out of the box.
    """
    if sys.platform == "darwin":
        # Cheap check: arm64 Apple Silicon. We don't probe torch here because
        # this runs before the venv has torch installed.
        try:
            arch = subprocess.run(
                ["uname", "-m"], capture_output=True, text=True
            ).stdout.strip()
            if arch == "arm64":
                # Try to read the chip name from sysctl. Falls back to a
                # generic label if the sysctl key isn't present.
                try:
                    chip = subprocess.run(
                        ["sysctl", "-n", "machdep.cpu.brand_string"],
                        capture_output=True, text=True,
                    ).stdout.strip() or "Apple Silicon"
                except Exception:
                    chip = "Apple Silicon"
                return "mps", chip, ""
        except Exception:
            pass
        return "cpu", "", ""

    try:
        r = subprocess.run(["nvidia-smi"], capture_output=True, text=True)
        if r.returncode != 0:
            return "cpu", "", ""

        m = re.search(r"CUDA Version: (\d+)\.(\d+)", r.stdout)
        if not m:
            return "cpu", "", ""

        major, minor = int(m.group(1)), int(m.group(2))

        nr = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True,
        )
        gpu_name = (
            nr.stdout.strip().splitlines()[0].strip()
            if nr.returncode == 0 else "NVIDIA GPU"
        )

        if   major >= 13:                  whl = "cu130"
        elif major == 12 and minor >= 8:   whl = "cu128"
        elif major == 12 and minor >= 6:   whl = "cu126"
        elif major == 12 and minor >= 4:   whl = "cu124"
        elif major == 12 and minor >= 1:   whl = "cu121"
        elif major == 11 and minor >= 8:   whl = "cu118"
        else:                              whl = "cpu"

        return whl, gpu_name, f"{major}.{minor}"

    except FileNotFoundError:
        return "cpu", "", ""


def _torch_build() -> str:
    """
    Return the installed torch build variant, e.g. 'cu126', 'cpu', or ''
    if torch is not installed.  Reads torch.__version__ directly so it works
    regardless of the index URL used to install it.
    """
    try:
        r = subprocess.run(
            [sys.executable, "-c", "import torch; print(torch.__version__)"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return ""
        ver = r.stdout.strip()          # e.g. "2.10.0+cu126" or "2.10.0+cpu"
        if "+" in ver:
            return ver.split("+", 1)[1] # "cu126" or "cpu"
        return ""                       # plain version string - treat as unknown
    except Exception:
        return ""

# ── Model pre-download ───────────────────────────────────────────────────────

# Models that need to be cached before the app starts.
# Each entry: (display_name, download_function_name)
# The download functions are defined inside _predownload_models to avoid
# importing heavy libraries at module level.

def _is_model_cached(hf_model_id: str) -> bool:
    """Search common cache roots for a HuggingFace model directory.

    The target directory name is models--<org>--<name>.  We search
    recursively (up to depth 4) under ~/.cache/ and the project-local
    models/ dir so that models cached by any library (huggingface_hub,
    pyannote, torch, etc.) are found regardless of the exact path.
    """
    target = "models--" + hf_model_id.replace("/", "--")
    roots = [
        Path(__file__).parent / "storage" / "models",   # project-local HF_HOME
        Path.home() / ".cache",             # covers huggingface/, torch/, etc.
    ]
    for root in roots:
        if not root.is_dir():
            continue
        if _find_model_dir(root, target, max_depth=4):
            return True
    return False


def _find_model_dir(base: Path, target: str, max_depth: int) -> bool:
    """Recursively search for a directory named `target` under `base`."""
    if max_depth <= 0:
        return False
    try:
        for entry in base.iterdir():
            if not entry.is_dir():
                continue
            if entry.name == target:
                # Confirm it has actual content (snapshots or model files)
                try:
                    if any(entry.rglob("*")):
                        return True
                except (PermissionError, OSError):
                    pass
            if _find_model_dir(entry, target, max_depth - 1):
                return True
    except (PermissionError, OSError):
        pass
    return False


# Map task IDs → HuggingFace model IDs for cache checking.
# On macOS the streaming Whisper engine is mlx-whisper (faster-whisper has no
# Metal backend), so we predownload the MLX-quantized large-v3 repo instead.
_MODEL_IDS = {
    "faster-whisper":        (
        "mlx-community/whisper-large-v3-mlx"
        if sys.platform == "darwin"
        else "Systran/faster-whisper-large-v3"
    ),
    "pyannote-segmentation": "pyannote/segmentation-3.0",
    "pyannote-embedding":    "pyannote/wespeaker-voxceleb-resnet34-LM",
    "pyannote-pipeline":     "pyannote/speaker-diarization-3.1",
    "whisper-turbo":         "openai/whisper-large-v3-turbo",
    "sentence-transformers": "sentence-transformers/all-MiniLM-L6-v2",
}


def _predownload_models():
    """Ensure all HuggingFace models are cached locally.

    First does a fast filesystem check for each model.  Only spawns the
    heavy download subprocess for models that aren't cached yet.
    """
    script_template = '''
import sys, os, warnings
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
warnings.filterwarnings("ignore")

# Load config which sets HF_TOKEN from .env or bundled token
from core import config as config
hf_token = os.environ.get("HF_TOKEN", "")

task = sys.argv[1]

if task == "faster-whisper":
    if sys.platform == "darwin":
        # mlx-whisper pulls from MLX-quantized repos under mlx-community/.
        from huggingface_hub import snapshot_download
        snapshot_download("mlx-community/whisper-large-v3-mlx")
    else:
        from faster_whisper import WhisperModel
        WhisperModel("large-v3", device="cpu", compute_type="int8")

elif task == "pyannote-segmentation":
    # Import diarizer to get all the torchaudio/speechbrain shims
    from ml import diarizer as diarizer  # noqa: F401
    from diart.models import SegmentationModel
    SegmentationModel.from_pretrained("pyannote/segmentation-3.0", use_hf_token=hf_token)

elif task == "pyannote-embedding":
    from ml import diarizer as diarizer  # noqa: F401
    from pyannote.audio import Model
    Model.from_pretrained("pyannote/wespeaker-voxceleb-resnet34-LM", use_auth_token=hf_token)

elif task == "pyannote-pipeline":
    from ml import diarizer as diarizer  # noqa: F401
    from pyannote.audio import Pipeline
    Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", use_auth_token=hf_token)

elif task == "whisper-turbo":
    from transformers import AutoFeatureExtractor, AutoModelForSpeechSeq2Seq
    AutoFeatureExtractor.from_pretrained("openai/whisper-large-v3-turbo")
    AutoModelForSpeechSeq2Seq.from_pretrained("openai/whisper-large-v3-turbo")

elif task == "sentence-transformers":
    from sentence_transformers import SentenceTransformer
    SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
'''

    models = [
        ("faster-whisper",        "Whisper large-v3 (streaming)"),
        ("pyannote-segmentation", "Pyannote segmentation"),
        ("pyannote-embedding",    "Pyannote speaker embedding"),
        ("pyannote-pipeline",     "Pyannote diarization pipeline"),
        ("whisper-turbo",         "Whisper large-v3-turbo (reanalysis)"),
        ("sentence-transformers", "Sentence embeddings"),
    ]

    # Fast check: skip models already in the local cache
    need_download = []
    for task_id, display_name in models:
        hf_id = _MODEL_IDS.get(task_id)
        if hf_id and _is_model_cached(hf_id):
            _ok(f"{display_name}")
        else:
            need_download.append((task_id, display_name))

    if not need_download:
        _ok("All models cached")
        return

    # Only write the download script if we actually need to download something
    script_path = Path(__file__).parent / ".model_download.py"
    script_path.write_text(script_template)

    max_attempts = 3
    all_ok = True
    for task_id, display_name in need_download:
        success = False
        for attempt in range(1, max_attempts + 1):
            r = subprocess.run(
                [sys.executable, str(script_path), task_id],
                capture_output=True, text=True, timeout=600,
            )
            if r.returncode == 0:
                success = True
                break
            stderr = r.stderr.strip()
            if "already" in stderr.lower() or not stderr:
                success = True
                break
            if attempt < max_attempts:
                _info(f"{display_name} - retrying ({attempt}/{max_attempts})...")
        if success:
            _ok(f"{display_name}")
        else:
            _warn(f"{display_name} - download failed after {max_attempts} attempts")
            all_ok = False

    # Clean up
    try:
        script_path.unlink()
    except OSError:
        pass

    if all_ok:
        _ok("All models cached")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global UV

    os.chdir(Path(__file__).parent)
    _ensure_venv()   # re-execs into .venv if not already running inside it

    # Migrate legacy <project>/{tools,models,data}/ into storage/ on update.
    # Runs before any module imports that resolve those paths (HF_HOME,
    # ffmpeg lookup, settings/paths bootstrap).
    _migrate_legacy_layout()

    # Ensure uv can find the active venv
    os.environ["VIRTUAL_ENV"] = str(VENV_DIR)

    UV = _find_uv()
    if not UV:
        _fatal("uv not found. Please run launch.bat or install uv: https://docs.astral.sh/uv/")

    os.system("cls" if sys.platform == "win32" else "clear")

    # Banner
    print()
    print(SEP_HEAVY)
    print(f"{CYN}{B}   MEETING ASSISTANT{R}")
    print(f"{GRY}   Real-time transcription + AI  |  local  |  private{R}")
    print(SEP_HEAVY)

    # ── System ────────────────────────────────────────────────────────────────
    _section("SYSTEM")

    # Python version gate
    vi = sys.version_info
    if vi < (3, 10):
        _err(f"Python {vi.major}.{vi.minor}.{vi.micro} -- 3.10+ required")
        print()
        print("      Upgrade from: https://www.python.org/downloads/")
        _fatal("Python version too old")
    _ok(f"Python {vi.major}.{vi.minor}.{vi.micro}")

    # Start Menu shortcut (first run only)
    _create_start_menu_shortcut()

    # GPU
    whl, gpu_name, cuda_ver = _detect_gpu()
    if whl == "cpu":
        print(f"{GRY}  [--] No accelerator detected -- CPU mode{R}")
    elif whl == "mps":
        print(f"{GRN}  [OK] {gpu_name}  {GRY}|  Metal (MPS){R}")
    else:
        print(f"{GRN}  [OK] {gpu_name}  {GRY}|  CUDA {cuda_ver}{R}")

    # uv version
    try:
        uv_ver = subprocess.run([UV, "--version"], capture_output=True, text=True)
        _ok(f"uv  {GRY}{uv_ver.stdout.strip()}{R}")
    except Exception:
        _ok("uv")

    # ── Packages ──────────────────────────────────────────────────────────────
    _section("PACKAGES")

    # Cloudflare WARP's TLS inspection breaks pip/uv (untrusted CA).
    # Disconnect before package installs, reconnect after (git/HF need it).
    from core.network import warp_disconnect
    warp_disconnect()

    # PyTorch - only install/replace when the installed variant doesn't match
    installed_build = _torch_build()   # e.g. "cu126", "cpu", or "" (not installed)

    if sys.platform == "darwin":
        # macOS: the default PyPI arm64 torch wheel ships Metal/MPS support.
        # No special index URL, no reinstall dance.
        if installed_build:
            _ok(f"PyTorch  {GRY}[arm64 | Metal/MPS]{R}")
        else:
            _info(f"PyTorch [arm64]  {GRY}(downloading...){R}")
            if not _uv_streaming("torch", "torchaudio"):
                _fatal("PyTorch install failed -- check your connection and retry")
            _ok(f"PyTorch  {GRY}[arm64 | Metal/MPS]{R}")
    elif whl == "cpu":
        if installed_build == "cpu":
            _ok(f"PyTorch  {GRY}[CPU]{R}")
        else:
            _info(f"PyTorch [CPU]  {GRY}(downloading...){R}")
            if not _uv_streaming("torch", "torchaudio", "--index-url", TORCH_INDEX + "cpu"):
                _fatal("PyTorch install failed -- check your connection and retry")
            _ok(f"PyTorch  {GRY}[CPU]{R}")
    else:
        if installed_build == whl:
            _ok(f"PyTorch  {GRY}[{whl} | GPU-accelerated]{R}")
        else:
            if installed_build:
                _info(f"PyTorch  {GRY}replacing {installed_build} → {whl}{R}")
            else:
                _info(f"PyTorch [{whl}]  {GRY}(downloading...){R}")
            # --reinstall-package is more targeted than pip's --force-reinstall
            if _uv_streaming("torch", "torchaudio",
                             "--reinstall-package", "torch",
                             "--reinstall-package", "torchaudio",
                             "--index-url", TORCH_INDEX + whl):
                _ok(f"PyTorch  {GRY}[{whl} | GPU-accelerated]{R}")
            else:
                _warn(f"GPU build failed -- falling back to CPU...")
                if not _uv_streaming("torch", "torchaudio", "--index-url", TORCH_INDEX + "cpu"):
                    _fatal("PyTorch install failed -- check your connection and retry")
                _ok(f"PyTorch  {GRY}[CPU | fallback]{R}")

    # Pre-install matplotlib from a binary wheel so diart's transitive pull
    # never triggers a source build (which requires MSVC on Windows).
    _uv("matplotlib>=3.8.0", "--only-binary", "matplotlib")

    # All other deps
    _info("Dependencies...")
    req_file = "requirements-macos.txt" if sys.platform == "darwin" else "requirements.txt"
    if not _uv_streaming("-r", req_file):
        _warn("Some packages failed -- retrying with full output...")
        print()
        if not _uv("-r", req_file, show_output=True):
            _fatal("Dependency install failed -- see errors above")
    _ok("All packages ready")

    # ── Pre-download models ──────────────────────────────────────────────────
    # Download all HuggingFace models now, while WARP is off and we control
    # the network state.  At runtime the cache-first approach means no network
    # calls are needed.  This step is fast when models are already cached.
    _section("MODELS")
    _predownload_models()

    # Leave WARP disconnected through app startup. Models are already cached
    # (above), so the app needs no network to start, and skipping WARP's TLS
    # inspection shaves a little off startup. If a corporate always-on policy
    # reconnects WARP anyway, that's fine now: core.config trusts WARP's CA via
    # the OS store (truststore), so HTTPS still verifies. The update-check
    # endpoint reconnects on demand via core.network.warp_reconnect().

    # ── FFmpeg ────────────────────────────────────────────────────────────────
    _section("FFMPEG")

    from capture_video import find_ffmpeg, download_ffmpeg, _LOCAL_FFMPEG

    ffmpeg_path = find_ffmpeg()
    if ffmpeg_path:
        # Get version string
        try:
            fv = subprocess.run(
                [ffmpeg_path, "-version"],
                capture_output=True, text=True, timeout=5,
            )
            ver_line = fv.stdout.split("\n")[0].strip() if fv.returncode == 0 else "ffmpeg"
            _ok(f"{ver_line}  {GRY}({ffmpeg_path}){R}")
        except Exception:
            _ok(f"ffmpeg  {GRY}({ffmpeg_path}){R}")
    else:
        _info(f"ffmpeg not found - downloading...  {GRY}(needed for screen recording){R}")
        try:
            download_ffmpeg(progress_cb=lambda msg: _info(msg))
            _ok(f"ffmpeg  {GRY}({_LOCAL_FFMPEG}){R}")
        except Exception as e:
            _warn(f"Could not download ffmpeg: {e}")
            _warn("Screen recording will be unavailable. Install ffmpeg manually to enable it.")

    # ── macOS audio bootstrap (BlackHole + aggregate device) ──────────────
    if sys.platform == "darwin":
        _section("macOS AUDIO")
        from capture_audio.mac_bootstrap import bootstrap_first_launch
        try:
            mac_status = bootstrap_first_launch()
            if mac_status["installed"]:
                _ok(f"BlackHole 2ch installed")
            else:
                _warn("BlackHole 2ch not installed automatically")
            if mac_status["aggregate_ready"]:
                _ok(f"Aggregate output device ready")
            for msg in mac_status.get("messages", []):
                _warn(msg)
        except Exception as e:
            _warn(f"macOS audio bootstrap failed: {e}")

    # ── Launch ────────────────────────────────────────────────────────────────
    print()
    print(SEP_HEAVY)
    print(f"{B}   Launching...{R}")
    print(f"{GRY}   Your browser will open automatically.{R}")
    print(f"{GRY}   Close this window or use the tray icon to exit.{R}")
    print(SEP_HEAVY)
    print()

    # Force HF Hub offline so runtime model loaders (pyannote, transformers,
    # faster-whisper) load straight from the cache the pre-download populated,
    # skipping online revision checks entirely. This is a speed/robustness
    # fast-path; truststore (core.config) is what actually makes online TLS
    # work when a model isn't cached or WARP reconnects.
    child_env = os.environ.copy()
    child_env["HF_HUB_OFFLINE"] = "1"
    child_env["TRANSFORMERS_OFFLINE"] = "1"

    result = subprocess.run(
        [sys.executable, "-u", "-X", "faulthandler", "app.py"],
        stderr=subprocess.PIPE, text=True, env=child_env,
    )
    if result.returncode != 0:
        print()
        _err(f"Meeting Assistant exited with an error (code {result.returncode}).")
        if result.stderr:
            # Show the last portion of stderr (the traceback or fault info)
            lines = result.stderr.strip().splitlines()
            # Find the last traceback or Fatal Python error block
            tb_start = 0
            for i, line in enumerate(lines):
                if line.startswith("Traceback") or line.startswith("Fatal Python error"):
                    tb_start = i
            relevant = lines[tb_start:]
            if len(relevant) > 40:
                relevant = relevant[-40:]
            print()
            for line in relevant:
                print(f"  {RED}{line}{R}")
        else:
            print()
            print(f"  {RED}No error output captured. The process may have crashed in native code.{R}")
            print(f"  {RED}Exit code: {result.returncode}{R}")
        print()
        input("Press Enter to exit...")


if __name__ == "__main__":
    main()
