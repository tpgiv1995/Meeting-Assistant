"""System tray icon for Meeting Assistant.

Cross-platform: Windows notification area, macOS menu bar, Linux system tray.
Backend (pystray) is platform-agnostic; we adapt the icon styling per OS so
it reads correctly against macOS's dark/light menu bar.

Requires: pystray, Pillow.  If not installed the app runs without a tray.
"""
from __future__ import annotations

import sys
import urllib.request
import webbrowser
from pathlib import Path
from typing import Callable

try:
    import pystray
    from PIL import Image, ImageDraw, ImageOps

    TRAY_AVAILABLE = True
except ImportError:
    TRAY_AVAILABLE = False

from core import config as config

# macOS menu-bar icons should be rendered as "template images" — black
# silhouettes with alpha — so AppKit can invert them automatically for
# dark menu bars. We achieve this two ways:
#   1. Provide a monochrome (black) variant of each state icon.
#   2. After pystray creates the NSStatusItem, mark the NSImage as a
#      template image via setTemplate_(True). Wrapped in try/except so
#      it degrades cleanly if pystray's internals shift.
_IS_MACOS = sys.platform == "darwin"

# ── Icon loading ───────────────────────────────────────────────────────────────
_IMAGES_DIR = Path(__file__).parent.parent / "ui_web" / "static" / "images"
_TRAY_SIZE  = 64
_icons: dict[str, "Image.Image"] = {}   # populated lazily by _ensure_icons()


def _tint(img: "Image.Image", color: tuple[int, int, int]) -> "Image.Image":
    """Return a tinted copy of *img* using the given RGB colour, preserving alpha."""
    alpha   = img.split()[3]
    gray    = ImageOps.grayscale(img)
    colored = ImageOps.colorize(gray, black=(0, 0, 0), white=color).convert("RGBA")
    colored.putalpha(alpha)
    return colored


def _to_template(img: "Image.Image") -> "Image.Image":
    """Convert an icon to a black silhouette suitable for a macOS template image.

    Preserves alpha but flattens all RGB to black. AppKit then renders it
    correctly on both dark and light menu bars when setTemplate_(True) is
    set on the NSImage.
    """
    alpha = img.split()[3]
    black = Image.new("RGBA", img.size, (0, 0, 0, 255))
    black.putalpha(alpha)
    return black


def _ensure_icons() -> None:
    """Load PNG assets and derive tray variants on first call."""
    if _icons:
        return
    try:
        def _load(name: str) -> "Image.Image":
            return (
                Image.open(_IMAGES_DIR / name)
                .convert("RGBA")
                .resize((_TRAY_SIZE, _TRAY_SIZE), Image.LANCZOS)
            )

        idle      = _load("logo.png")
        recording = _load("logo_recording.png")

        if _IS_MACOS:
            # Menu bar template images: monochrome silhouette, no fill colour.
            # AppKit inverts them automatically for dark menu bars when the
            # underlying NSImage is marked template (handled in run()).
            idle_template      = _to_template(idle)
            recording_template = _to_template(recording)
            _icons["ready"]     = idle_template
            _icons["recording"] = recording_template
            _icons["loading"]   = idle_template
            _icons["setup"]     = idle_template
        else:
            _icons["ready"]     = idle
            _icons["recording"] = recording
            _icons["loading"]   = _tint(idle, (110, 118, 129))   # gray
            _icons["setup"]     = _tint(idle, (210, 153, 34))    # amber
    except Exception as e:
        print(f"[tray] Could not load PNG icons, falling back to drawn icons: {e}")


def _create_fallback_icon(color: tuple[int, int, int], size: int = 64) -> "Image.Image":
    """Programmatically draw a simple mic icon - used only if PNG assets are missing."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d   = ImageDraw.Draw(img)
    m   = 2
    d.ellipse([m, m, size - m, size - m], fill=color)
    cw, ch = int(size * 0.26), int(size * 0.34)
    cx, cy = (size - cw) // 2, int(size * 0.16)
    d.rounded_rectangle([cx, cy, cx + cw, cy + ch], radius=cw // 2, fill="white")
    aw = int(cw + size * 0.14)
    ax = (size - aw) // 2
    ay = cy + ch - int(size * 0.08)
    lw = max(2, size // 22)
    d.arc([ax, ay, ax + aw, ay + int(size * 0.22)], start=0, end=180, fill="white", width=lw)
    mid = size // 2
    lt  = ay + int(size * 0.11)
    lb  = lt + int(size * 0.10)
    d.line([(mid, lt), (mid, lb)], fill="white", width=lw)
    d.line([(mid - int(size * 0.10), lb), (mid + int(size * 0.10), lb)], fill="white", width=lw)
    return img


class MeetingTray:
    """Manages the system tray icon and its context menu.

    Parameters
    ----------
    server_url : str
        e.g. "http://127.0.0.1:6969"
    state_getter : callable
        Returns a dict snapshot of app state (called under the app's state lock).
    on_quit : callable
        Called when the user clicks Quit.  Receives the pystray Icon as argument.
    """

    def __init__(
        self,
        server_url: str,
        state_getter: Callable[[], dict],
        on_quit: Callable[["pystray.Icon"], None],
    ) -> None:
        self._url = server_url
        self._get_state = state_getter
        self._on_quit = on_quit
        self._icon: pystray.Icon | None = None

    # ── Public ────────────────────────────────────────────────────────────────

    def run(self) -> None:
        """Start the tray icon.  Blocks the calling thread (must be main)."""
        self._icon = pystray.Icon(
            name="Meeting Assistant",
            icon=self._pick_icon(),
            title=self._pick_tooltip(),
            menu=self._build_menu(),
        )
        self._icon.run(setup=self._on_setup)

    def refresh(self) -> None:
        """Update the icon image and tooltip to reflect current state. Thread-safe."""
        if self._icon is None:
            return
        try:
            self._icon.icon = self._pick_icon()
            self._icon.title = self._pick_tooltip()
            self._icon.update_menu()
            # Re-apply template-image flag — pystray rebuilds the NSImage
            # whenever .icon is reassigned, so the flag is lost on each refresh.
            if _IS_MACOS:
                self._mark_template_image(self._icon)
        except Exception:
            pass

    def stop(self) -> None:
        """Remove the tray icon and unblock run()."""
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass

    # ── Private ───────────────────────────────────────────────────────────────

    def _on_setup(self, icon: "pystray.Icon") -> None:
        """Called once after the icon enters its event loop."""
        icon.visible = True
        if _IS_MACOS:
            self._mark_template_image(icon)

    @staticmethod
    def _mark_template_image(icon: "pystray.Icon") -> None:
        """Mark the menu-bar NSImage as a template so AppKit inverts it
        automatically for dark menu bars. pystray exposes the underlying
        NSStatusItem via internal attrs; we walk it defensively."""
        try:
            status_item = getattr(icon, "_status_item", None) or getattr(icon, "_status_bar_item", None)
            if status_item is None:
                return
            ns_button = status_item.button() if callable(getattr(status_item, "button", None)) else None
            ns_image = ns_button.image() if ns_button is not None else None
            if ns_image is not None and hasattr(ns_image, "setTemplate_"):
                ns_image.setTemplate_(True)
        except Exception:
            pass  # styling polish only — not worth crashing the tray over

    def _get_tray_state(self) -> str:
        """Return the current tray state key."""
        st = self._get_state()
        provider = st.get("ai_provider", "anthropic")
        if config.needs_setup(provider):
            return "setup"
        if st.get("is_recording"):
            return "recording"
        if st.get("recording_ready"):
            return "ready"
        return "loading"

    def _pick_icon(self) -> "Image.Image":
        _ensure_icons()
        key = self._get_tray_state()
        if key in _icons:
            return _icons[key]
        # Fallback: drawn icon if PNG assets were not found
        fallbacks = {
            "setup":     (210, 153, 34),
            "recording": (248,  81, 73),
            "ready":     ( 88, 166, 255),
            "loading":   (110, 118, 129),
        }
        return _create_fallback_icon(fallbacks[key])

    def _pick_tooltip(self) -> str:
        """Return a tooltip string reflecting current state."""
        key = self._get_tray_state()
        tooltips = {
            "setup":     "Meeting Assistant | Setup required",
            "recording": "Meeting Assistant | Recording",
            "ready":     "Meeting Assistant | Ready",
            "loading":   "Meeting Assistant | Loading models…",
        }
        return tooltips.get(key, "Meeting Assistant")

    def _build_menu(self) -> "pystray.Menu":
        S = pystray.MenuItem  # shorthand
        SEP = pystray.Menu.SEPARATOR

        return pystray.Menu(
            S("Meeting Assistant", None, enabled=False),
            SEP,
            # ── Status ────────────────────────────────────────────────────
            S(lambda _: self._status_text(), None, enabled=False),
            SEP,
            # ── Actions ───────────────────────────────────────────────────
            S("Open Web Interface", self._open_browser, default=True),
            S(
                lambda _: "Stop Recording" if self._get_state().get("is_recording") else "Start Recording",
                self._toggle_recording,
                enabled=lambda _: self._get_state().get("recording_ready", False),
            ),
            S("Settings...", self._open_settings),
            S("Test Toast", self._test_toast),
            SEP,
            # ── Server ───────────────────────────────────────────────────
            S("Check for Updates", self._check_updates),
            S("Restart Server", self._restart_server),
            SEP,
            S("Quit", self._quit),
        )

    def _status_text(self) -> str:
        st = self._get_state()
        if config.needs_setup():
            return "Setup required"
        if st.get("is_recording"):
            return "Recording..."
        if st.get("recording_ready"):
            return "Ready"
        return st.get("recording_ready_reason", "Loading models...")

    def _diarizer_text(self) -> str:
        st = self._get_state()
        if st.get("diarizer_ready"):
            return "Diarizer: Ready"
        if config.get_key_status()["HUGGING_FACE_KEY"]["is_set"]:
            return "Diarizer: Loading..."
        return "Diarizer: No HF key"

    def _key_line(self, key_name: str) -> str:
        info = config.get_key_status().get(key_name, {})
        label = info.get("label", key_name)
        if info.get("is_set"):
            return f"{label}: {info['masked']}"
        suffix = "" if info.get("required") else " (optional)"
        return f"{label}: not set{suffix}"

    # ── Menu callbacks ────────────────────────────────────────────────────────

    def _open_browser(self, icon=None, item=None) -> None:
        webbrowser.open(self._url)

    def _open_settings(self, icon=None, item=None) -> None:
        webbrowser.open(f"{self._url}/session?settings=1")

    def _check_updates(self, icon=None, item=None) -> None:
        """Open the web UI with the settings panel on the System tab to check for updates."""
        webbrowser.open(f"{self._url}/session?settings=1&section=system")

    def _restart_server(self, icon=None, item=None) -> None:
        """Restart the server via the API."""
        try:
            req = urllib.request.Request(
                f"{self._url}/api/restart",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass  # server is restarting, connection will drop

    def _toggle_recording(self, icon=None, item=None) -> None:
        """Start or stop recording via the local Flask API."""
        st = self._get_state()
        if st.get("is_recording"):
            # Stop: direct API call is fine
            try:
                req = urllib.request.Request(
                    f"{self._url}/api/recording/stop",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=5)
            except Exception as e:
                print(f"[tray] stop recording failed: {e}")
        else:
            # Start: open the session page with ?autostart so the recording
            # goes through the same audio-initialisation path as a normal
            # session-page start (avoids DirectShow echo issues).
            webbrowser.open(f"{self._url}/session?autostart=1")

    def _test_toast(self, icon=None, item=None) -> None:
        """Fire a diagnostic system toast — verifies callbacks + visibility."""
        try:
            from ui_desktop import notifications
            ok = notifications.send_test_toast()
            if not ok:
                print("[tray] Test toast failed to dispatch — see [notify] log lines above.")
        except Exception as e:
            print(f"[tray] Test toast error: {e}")

    def _quit(self, icon=None, item=None) -> None:
        self._on_quit(self._icon)
