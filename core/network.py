"""
Network utilities - Cloudflare WARP management and cache-first model loading.

Corporate WARP uses TLS inspection which breaks pip/uv downloads.
Git operations require WARP connected for routing.  HuggingFace model
downloads may fail with WARP in either state depending on timing.

Strategy:
- launch.py toggles WARP off for pip, back on after.
- Runtime model loads use _load_hf_pipeline() which tries the local cache
  first, then toggles WARP off to attempt a fresh download if needed.
"""
import shutil
import subprocess

from core import log as log

_warp_cli: str | None = None  # cached path


def _find_warp_cli() -> str:
    global _warp_cli
    if _warp_cli is None:
        _warp_cli = shutil.which("warp-cli") or ""
    return _warp_cli


def _is_connected() -> bool | None:
    """Return True if connected, False if disconnected, None if unknown."""
    cli = _find_warp_cli()
    if not cli:
        return None
    try:
        r = subprocess.run([cli, "status"], capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return None
        if "Disconnected" in r.stdout:
            return False
        if "Connected" in r.stdout:
            return True
        return None
    except Exception:
        return None


def warp_disconnect() -> bool:
    """Disconnect WARP. Returns True on success or if already disconnected."""
    cli = _find_warp_cli()
    if not cli:
        return True
    if _is_connected() is not True:
        return True
    try:
        subprocess.run([cli, "disconnect"], capture_output=True, timeout=10)
        log.info("network", "Cloudflare WARP disconnected (TLS inspection breaks pip)")
        return True
    except Exception as e:
        log.warn("network", f"Failed to disconnect WARP: {e}")
        return False


def warp_reconnect() -> bool:
    """Reconnect WARP. Returns True on success or if already connected."""
    cli = _find_warp_cli()
    if not cli:
        return True
    if _is_connected() is not False:
        return True
    try:
        subprocess.run([cli, "connect"], capture_output=True, timeout=10)
        log.info("network", "Cloudflare WARP reconnected")
        return True
    except Exception as e:
        log.warn("network", f"Failed to reconnect WARP: {e}")
        return False


def _is_model_cached(model_id: str) -> bool:
    """Check if a HuggingFace model/pipeline is already in the local cache."""
    import os
    from pathlib import Path
    hf_home = os.environ.get("HF_HOME", "")
    if not hf_home:
        return False
    # HF hub stores models under hub/models--<org>--<name>/snapshots/
    cache_dir = Path(hf_home) / "hub" / ("models--" + model_id.replace("/", "--"))
    snapshots = cache_dir / "snapshots"
    # If at least one snapshot directory with files exists, it's cached
    if snapshots.is_dir():
        for snap in snapshots.iterdir():
            if snap.is_dir() and any(snap.iterdir()):
                return True
    return False


def _load_hf_pipeline(model_id: str, hf_token: str):
    """Load a pyannote Pipeline, using cache when available.

    pyannote's Pipeline.from_pretrained does NOT support local_files_only,
    so we check the HF cache manually first.  If cached, we set
    HF_HUB_OFFLINE=1 for the duration of the load to prevent any network
    calls.  On cache miss, try downloading with WARP off then WARP on.

    Returns the Pipeline object or None on failure.
    """
    import os
    from pyannote.audio import Pipeline as PyannotePipeline

    # Attempt 1: load from cache (no network)
    if _is_model_cached(model_id):
        old_val = os.environ.get("HF_HUB_OFFLINE")
        os.environ["HF_HUB_OFFLINE"] = "1"
        try:
            return PyannotePipeline.from_pretrained(
                model_id,
                use_auth_token=hf_token,
            )
        except Exception:
            log.warn("network", f"Cache hit for '{model_id}' but load failed, re-downloading...")
        finally:
            if old_val is None:
                os.environ.pop("HF_HUB_OFFLINE", None)
            else:
                os.environ["HF_HUB_OFFLINE"] = old_val

    log.info("network", f"Model '{model_id}' not in cache, downloading...")

    # Attempt 2: download with WARP off
    warp_disconnect()
    try:
        return PyannotePipeline.from_pretrained(
            model_id,
            use_auth_token=hf_token,
        )
    except Exception:
        pass

    # Attempt 3: download with WARP on
    warp_reconnect()
    try:
        return PyannotePipeline.from_pretrained(
            model_id,
            use_auth_token=hf_token,
        )
    except Exception as e:
        log.error("network", f"Failed to load '{model_id}' (tried cache, WARP off, WARP on): {e}")
        return None
