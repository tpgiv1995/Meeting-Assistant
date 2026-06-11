"""Resolves data folder paths. Single source of truth.

The data folder contains all persistent app state — SQLite DBs, settings,
audio/video recordings, screenshots, attachments, etc. By default it lives
at ``<project>/data/`` but the user can relocate it (e.g. to a OneDrive
folder) via the System tab in Settings.

The override is stored in ``<project>/.data_location`` — a small text
file containing the absolute path to the data folder. The pointer file
deliberately lives in the project directory rather than inside the
relocatable folder so we have a stable bootstrap point.

This module has no project-internal imports so it can be loaded before
``settings``, ``storage``, etc.
"""
from __future__ import annotations

import shutil
import sqlite3
import threading
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
_POINTER_FILE = _PROJECT_ROOT / ".data_location"
_DEFAULT_DATA_DIR = _PROJECT_ROOT / "storage" / "data"

_lock = threading.Lock()
_cached: Path | None = None


def _read_pointer() -> Path | None:
    """Return the override path from the pointer file, or None if unset/invalid.

    Invalid contents (relative paths, missing directory parents, garbage)
    return None so we silently fall back to the default rather than crashing
    on startup.
    """
    if not _POINTER_FILE.exists():
        return None
    try:
        text = _POINTER_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        p = Path(text)
    except Exception:
        return None
    return p if p.is_absolute() else None


def _write_pointer(new_path: Path) -> None:
    """Atomically replace the pointer file."""
    tmp = _POINTER_FILE.with_suffix(".tmp")
    tmp.write_text(str(new_path), encoding="utf-8")
    tmp.replace(_POINTER_FILE)


def _delete_pointer() -> None:
    """Remove the pointer file (revert to default location)."""
    try:
        _POINTER_FILE.unlink()
    except FileNotFoundError:
        pass


def data_dir() -> Path:
    """Return the active data folder, creating it on demand."""
    global _cached
    with _lock:
        if _cached is None:
            override = _read_pointer()
            _cached = override if override else _DEFAULT_DATA_DIR
        _cached.mkdir(parents=True, exist_ok=True)
        return _cached


def reload() -> Path:
    """Re-read the pointer file (call after migration)."""
    global _cached
    with _lock:
        _cached = None
    return data_dir()


def default_dir() -> Path:
    """Return the default data folder (project root / data)."""
    return _DEFAULT_DATA_DIR


def is_overridden() -> bool:
    """True if the user has set a custom data folder."""
    return _read_pointer() is not None


def settings_path() -> Path:
    return data_dir() / "settings.json"


def db_path() -> Path:
    return data_dir() / "meetings.db"


def audio_dir() -> Path:
    p = data_dir() / "audio"
    p.mkdir(parents=True, exist_ok=True)
    return p


def video_dir() -> Path:
    p = data_dir() / "video"
    p.mkdir(parents=True, exist_ok=True)
    return p


def screenshots_dir() -> Path:
    p = data_dir() / "screenshots"
    p.mkdir(parents=True, exist_ok=True)
    return p


def attachments_dir() -> Path:
    p = data_dir() / "attachments"
    p.mkdir(parents=True, exist_ok=True)
    return p


def profile_dir() -> Path:
    p = data_dir() / "audio_profiles"
    p.mkdir(parents=True, exist_ok=True)
    return p


def backup_dir() -> Path:
    p = data_dir() / "backups"
    p.mkdir(parents=True, exist_ok=True)
    return p


def tmp_dir() -> Path:
    p = data_dir() / "tmp"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ── Migration helpers ───────────────────────────────────────────────────────

class MigrationError(Exception):
    """Raised when a data folder migration fails."""


def _validate_destination(src: Path, dst: Path) -> None:
    """Reject migrations that would corrupt or recurse into themselves."""
    src = src.resolve()
    dst = dst.resolve()
    if dst == src:
        raise MigrationError("Destination is the same as the current data folder.")
    # Prevent migrating *into* the current data folder (would copy into itself).
    try:
        dst.relative_to(src)
        raise MigrationError(
            "Destination is inside the current data folder. Choose a "
            "different location."
        )
    except ValueError:
        pass
    # Prevent migrating the current folder *into* a future descendant — i.e.
    # the source can't be inside the destination either, since that means
    # everything we're copying would move under itself.
    try:
        src.relative_to(dst)
        if dst != src.parent:
            raise MigrationError(
                "Current data folder is inside the destination. Choose a "
                "different location."
            )
    except ValueError:
        pass


def _backup_sqlite(src_db: Path, dst_db: Path) -> None:
    """Use the SQLite online backup API for a consistent snapshot.

    Safer than file-copying a live database — handles WAL/SHM correctly
    and produces a single consolidated DB file at the destination.
    """
    dst_db.parent.mkdir(parents=True, exist_ok=True)
    src_conn = sqlite3.connect(str(src_db))
    try:
        dst_conn = sqlite3.connect(str(dst_db))
        try:
            with dst_conn:
                src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _is_sqlite(path: Path) -> bool:
    """True if ``path`` is a SQLite main DB file (.db)."""
    return path.is_file() and path.suffix.lower() == ".db"


def _is_sqlite_sidecar(path: Path) -> bool:
    """True if ``path`` is a SQLite WAL/SHM/journal sidecar (handled by backup API)."""
    name = path.name.lower()
    return name.endswith("-wal") or name.endswith("-shm") or name.endswith("-journal")


def migrate(
    src: Path | None = None,
    dst: Path | None = None,
    on_progress=None,
) -> dict:
    """Migrate the data folder from ``src`` (default: current) to ``dst``.

    Steps (in order — failure at any step rolls back):
      1. Validate destination (not the same folder, not nested into source).
      2. Create destination tree.
      3. Copy SQLite DBs via online-backup API.
      4. Copy every other file with metadata preserved.
      5. Verify the destination contains every source file (file-count check).
      6. Update the pointer file atomically.

    The source folder is *kept intact* as a backup. The caller (UI/server)
    is responsible for telling the user to restart the app so all modules
    pick up the new path.

    Returns a dict with keys: ``files_copied``, ``dbs_copied``,
    ``bytes_copied``, ``src``, ``dst``.
    """
    if src is None:
        src = data_dir()
    src = Path(src).resolve()
    if dst is None:
        raise MigrationError("Destination required.")
    dst = Path(dst).resolve()

    _validate_destination(src, dst)

    if not src.exists():
        raise MigrationError(f"Source data folder does not exist: {src}")

    dst.mkdir(parents=True, exist_ok=True)
    if any(dst.iterdir()):
        # Refuse to merge into a non-empty destination — caller can clear
        # it manually if they really want to overlay.
        raise MigrationError(
            f"Destination is not empty: {dst}. Choose an empty folder or "
            "delete its contents first."
        )

    files_copied = 0
    dbs_copied = 0
    bytes_copied = 0

    def _emit(msg: str) -> None:
        if on_progress:
            try:
                on_progress(msg)
            except Exception:
                pass

    try:
        # First pass: snapshot SQLite DBs via the backup API. This handles
        # WAL/SHM atomically and produces a clean single-file DB at the
        # destination.
        for entry in src.rglob("*"):
            if not entry.is_file():
                continue
            if _is_sqlite_sidecar(entry):
                continue  # consolidated by .backup()
            rel = entry.relative_to(src)
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if _is_sqlite(entry):
                _emit(f"Backing up database {rel}")
                _backup_sqlite(entry, target)
                dbs_copied += 1
                bytes_copied += target.stat().st_size
            else:
                _emit(f"Copying {rel}")
                shutil.copy2(entry, target)
                files_copied += 1
                bytes_copied += target.stat().st_size

        # Verify: every non-sidecar file in src exists in dst with a non-zero
        # size (DBs are reconstructed by backup, so size may differ; just
        # check existence + non-empty for those).
        _emit("Verifying copied files…")
        for entry in src.rglob("*"):
            if not entry.is_file() or _is_sqlite_sidecar(entry):
                continue
            rel = entry.relative_to(src)
            target = dst / rel
            if not target.exists():
                raise MigrationError(f"Missing in destination: {rel}")
            if not _is_sqlite(entry):
                # Non-DB files must match in size. DB files are rebuilt by
                # the backup API, so size deltas are expected.
                if target.stat().st_size != entry.stat().st_size:
                    raise MigrationError(
                        f"Size mismatch for {rel}: "
                        f"src={entry.stat().st_size} dst={target.stat().st_size}"
                    )

        # Atomically commit the new path.
        _emit("Updating pointer…")
        _write_pointer(dst)
        reload()

    except Exception as exc:
        # Roll back: tear down the partial destination so it doesn't stick
        # around as an orphan. We DON'T touch the source — it's intact.
        _emit(f"Migration failed: {exc}; rolling back destination")
        try:
            shutil.rmtree(dst)
        except Exception:
            pass
        if isinstance(exc, MigrationError):
            raise
        raise MigrationError(str(exc)) from exc

    return {
        "files_copied": files_copied,
        "dbs_copied": dbs_copied,
        "bytes_copied": bytes_copied,
        "src": str(src),
        "dst": str(dst),
    }


def reset_to_default() -> None:
    """Forget the user's override and revert to the default data folder.

    Does NOT move any files — purely a pointer reset. Caller is responsible
    for migrating data back to the default location first if needed.
    """
    _delete_pointer()
    reload()


# ── Folder picker ──────────────────────────────────────────────────────────

def pick_folder(initial_dir: str | None = None) -> str | None:
    """Show a native folder picker and return the selected path, or None.

    Spawns a subprocess running tkinter — calling tkinter directly from a
    Flask worker thread is fragile on Windows. The subprocess prints the
    selected path to stdout (empty if cancelled) and exits.
    """
    import subprocess
    import sys

    helper = (
        "import sys\n"
        "import tkinter as tk\n"
        "from tkinter import filedialog\n"
        "initial = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None\n"
        "root = tk.Tk()\n"
        "root.withdraw()\n"
        "try:\n"
        "    root.attributes('-topmost', True)\n"
        "except Exception:\n"
        "    pass\n"
        "path = filedialog.askdirectory(initialdir=initial, title='Select Data Folder', mustexist=False)\n"
        "sys.stdout.write(path or '')\n"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", helper, initial_dir or ""],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    selected = (result.stdout or "").strip()
    return selected or None
