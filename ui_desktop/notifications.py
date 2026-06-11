"""Bulletproof system toast notifications for Meeting Assistant.

Backend dispatch is automatic via sys.platform:
  - Windows: windows-toasts (WinRT ToastNotificationManager) with a registered
             AppUserModelID so toasts actually appear in the Action Center and
             support clickable buttons + activation callbacks.
  - macOS:   osascript (Notification Center; no action buttons).
  - Other:   no-op.

The Windows backend is the important one. Two things have to be true for a
Windows toast to appear AND survive long enough for the user to click it:

  1. The calling process must be associated with an AppUserModelID (AUMID)
     that is registered under HKCU\\Software\\Classes\\AppUserModelId\\<id>.
     Without this, Win11 silently drops toasts on the floor — which is
     exactly the failure mode we hit with winotify.

  2. Activation callbacks fire on a WinRT background thread *after* the
     calling Python function returns. We therefore keep a strong reference
     to every live Toast + Toaster so they aren't garbage-collected before
     the user clicks.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Callable, Optional

from core import log as log


_ROOT = Path(__file__).parent.parent
_ICON_ICO = _ROOT / "ui_web" / "static" / "images" / "logo.ico"
_ICON_PNG = _ROOT / "ui_web" / "static" / "images" / "logo.png"

# AUMID — must be unique per app and stable across runs. Format is
# "CompanyName.ProductName" (max 129 chars, no spaces).
AUMID = "MeetingAssistant.App"
APP_DISPLAY_NAME = "Meeting Assistant"


# ── Windows AUMID registration ────────────────────────────────────────────────

_aumid_registered = False
_aumid_lock = threading.Lock()


def _register_windows_aumid() -> bool:
    """Register the AUMID in HKCU so Windows treats us as a known toast source.

    Idempotent. Safe to call repeatedly. Returns True if the AUMID is usable
    after the call (already registered or freshly registered).
    """
    global _aumid_registered
    with _aumid_lock:
        if _aumid_registered:
            return True
        try:
            import winreg
            key_path = rf"Software\Classes\AppUserModelId\{AUMID}"
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_DISPLAY_NAME)
                if _ICON_ICO.exists():
                    winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(_ICON_ICO))
                # ShowInSettings=1 makes the app appear in Settings >
                # Notifications so the user can re-enable it if they muted us.
                winreg.SetValueEx(key, "ShowInSettings", 0, winreg.REG_DWORD, 1)

            # Tell the current process to use this AUMID. Without this call
            # WinRT may attribute the toast to "python.exe" and drop it.
            try:
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(AUMID)
            except Exception as e:
                log.warn("notify", f"SetCurrentProcessExplicitAppUserModelID failed: {e}")

            _aumid_registered = True
            return True
        except Exception as e:
            log.warn("notify", f"AUMID registration failed: {e}")
            return False


# ── Live-toast tracking ───────────────────────────────────────────────────────
# Toast callbacks fire on a background thread after the originating Python
# call returns. Keep strong refs so the Toast + Toaster aren't GC'd mid-flight.

_live_toaster = None  # type: ignore[var-annotated]
_live_toasts: list = []
_live_lock = threading.Lock()


def _remember_toast(toast) -> None:
    with _live_lock:
        _live_toasts.append(toast)
        # Cap retained toasts so we don't leak memory over a long session.
        if len(_live_toasts) > 32:
            del _live_toasts[: len(_live_toasts) - 32]


def _get_windows_toaster():
    """Return a process-wide InteractableWindowsToaster bound to our AUMID."""
    global _live_toaster
    if _live_toaster is not None:
        return _live_toaster
    from windows_toasts import InteractableWindowsToaster  # type: ignore[import-not-found]
    _register_windows_aumid()
    _live_toaster = InteractableWindowsToaster(APP_DISPLAY_NAME, notifierAUMID=AUMID)
    return _live_toaster


# ── Public API ────────────────────────────────────────────────────────────────


def notify(
    title: str,
    body: str = "",
    *,
    on_click: Optional[Callable[[str], None]] = None,
    actions: Optional[list[dict]] = None,
    image: Optional[Path | str] = None,
    duration: str = "short",
) -> bool:
    """Show a system toast.

    Parameters
    ----------
    title, body
        Toast headline and message.
    on_click
        Called with the activation argument (a string) when the user clicks
        the toast body or any action that doesn't define its own ``on_click``.
        Runs on a background WinRT thread; keep it short and exception-safe.
    actions
        Optional list of dicts: ``{"label": str, "arg": str, "on_click": fn}``.
        Each becomes a button on the toast. ``on_click`` is optional — if
        omitted, falls back to the toast-level ``on_click``.
    image
        Optional path to a large inline image (PNG or JPG) rendered in the
        toast body. Off by default: the small corner app icon already comes
        from the AUMID's IconUri, so toasts don't need a big inline logo.
    duration
        "short" (~5s) or "long" (~25s). Long toasts stay visible longer
        before sliding into the Action Center.

    Returns
    -------
    True if the platform notification API accepted the toast.
    """
    if sys.platform == "win32":
        return _send_windows_toast(title, body, on_click=on_click, actions=actions or [], image=image, duration=duration)
    if sys.platform == "darwin":
        return _send_macos_notification(title, body)
    log.warn("notify", f"Toast skipped: unsupported platform {sys.platform}")
    return False


def send_quiet_recording_toast(session_id: str, server_url: str) -> bool:
    """Show a system toast that routes back to the active recording session."""
    base = server_url.rstrip("/")
    session_url = f"{base}/session?id={session_id}&quiet_prompt=1"
    stop_url = f"{base}/api/recording/stop"

    def _open_session(_arg: str) -> None:
        webbrowser.open(session_url)

    def _stop_recording(_arg: str) -> None:
        try:
            import urllib.request
            req = urllib.request.Request(
                stop_url, data=b"{}",
                headers={"Content-Type": "application/json"}, method="POST",
            )
            urllib.request.urlopen(req, timeout=5).read()
        except Exception as e:
            log.warn("notify", f"Stop-from-toast failed: {e}")
        webbrowser.open(session_url)

    return notify(
        "Still in the meeting?",
        "Things have gone quiet. Click to stop the recording.",
        on_click=_open_session,
        actions=[
            {"label": "Stop recording", "arg": "stop", "on_click": _stop_recording},
            {"label": "Keep recording", "arg": "keep"},
        ],
        duration="long",
    )


def send_test_toast() -> bool:
    """Diagnostic toast — fired from the tray menu's Test Toast item."""
    def _on_body(arg: str) -> None:
        log.info("notify", f"Test toast body clicked (arg={arg!r})")

    def _on_button(arg: str) -> None:
        log.info("notify", f"Test toast button clicked (arg={arg!r})")

    return notify(
        "Meeting Assistant — Test Toast",
        "If you can see this, system toasts are working. Click a button to verify callbacks.",
        on_click=_on_body,
        actions=[
            {"label": "Click me", "arg": "primary", "on_click": _on_button},
            {"label": "Or me",   "arg": "secondary", "on_click": _on_button},
        ],
        duration="long",
    )


# ── Windows backend ───────────────────────────────────────────────────────────


def _send_windows_toast(
    title: str,
    body: str,
    *,
    on_click: Optional[Callable[[str], None]],
    actions: list[dict],
    image: Optional[Path | str],
    duration: str,
) -> bool:
    try:
        from windows_toasts import (  # type: ignore[import-not-found]
            Toast, ToastButton, ToastDisplayImage, ToastDuration,
        )
    except ImportError as e:
        log.warn("notify", f"windows-toasts not installed ({e}). Run: pip install windows-toasts")
        return False

    try:
        toaster = _get_windows_toaster()

        # Map button arg → callback so we can dispatch in on_activated.
        button_callbacks: dict[str, Callable[[str], None]] = {}
        toast_buttons = []
        for spec in actions:
            label = str(spec.get("label", "")).strip()
            if not label:
                continue
            arg = str(spec.get("arg", label))
            cb = spec.get("on_click")
            if callable(cb):
                button_callbacks[arg] = cb
            toast_buttons.append(ToastButton(content=label, arguments=arg))

        def _on_activated(event_args) -> None:
            try:
                arg = getattr(event_args, "arguments", "") or ""
                cb = button_callbacks.get(arg)
                if cb is None and on_click is not None:
                    cb = on_click
                if cb is not None:
                    cb(arg)
            except Exception as e:
                log.warn("notify", f"Toast on_activated callback raised: {e}")

        def _on_failed(event_args) -> None:
            try:
                err = getattr(event_args, "error_code", event_args)
                log.warn("notify", f"Toast failed to display: {err}")
            except Exception:
                pass

        text_fields = [title]
        if body:
            text_fields.append(body)

        toast = Toast(
            text_fields=text_fields,
            duration=ToastDuration.Long if duration == "long" else ToastDuration.Short,
            on_activated=_on_activated,
            on_failed=_on_failed,
            actions=toast_buttons,
        )

        # Only attach an inline image when a caller explicitly passes one. We
        # deliberately do NOT default to the app logo: an inline
        # ToastDisplayImage renders large in the toast body, and the small
        # corner branding is already provided by the AUMID's IconUri.
        img_path = Path(image) if image else None
        if img_path is not None and img_path.exists():
            try:
                toast.AddImage(ToastDisplayImage.fromPath(str(img_path)))
            except Exception as e:
                log.warn("notify", f"Toast image attach failed: {e}")

        toaster.show_toast(toast)
        _remember_toast(toast)
        return True
    except Exception as e:
        log.warn("notify", f"windows-toasts show failed: {e}")
        return False


# ── macOS backend ─────────────────────────────────────────────────────────────


def _osascript_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _send_macos_notification(title: str, body: str) -> bool:
    script = (
        f'display notification "{_osascript_escape(body)}" '
        f'with title "{_osascript_escape(title)}" sound name "Pop"'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=True, capture_output=True, timeout=5,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.warn("notify", f"osascript notification failed: {e}")
        return False
