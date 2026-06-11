"""
Shared console logging helpers with ANSI colour.
Imported by app.py, transcriber.py, diarizer.py, etc.
"""
import ctypes
import sys


def _enable_ansi() -> None:
    if sys.platform == "win32":
        try:
            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        except Exception:
            pass


_enable_ansi()

_R   = "\033[0m"
_RED = "\033[91m"
_GRN = "\033[92m"
_YLW = "\033[93m"
_BLU = "\033[94m"
_MAG = "\033[95m"
_CYN = "\033[96m"
_GRY = "\033[90m"

_TAG_COLORS: dict[str, str] = {
    "whisper":     _CYN,
    "transcriber": _CYN,
    "diarizer":    _MAG,
    "ai":          _GRN,
    "summary":     _GRN,
    "recording":   _BLU,
    "reanalysis":  _BLU,
    "settings":    _GRY,
    "fingerprint": _YLW,
    "audio":       _GRY,
    "tray":        _GRY,
    "storage":     _GRY,
    "app":         _GRY,
}


def _fmt_tag(tag: str) -> str:
    color = _TAG_COLORS.get(tag.lower(), _GRY)
    return f"{color}[{tag}]{_R}"


def info(tag: str, msg: str) -> None:
    print(f"  {_fmt_tag(tag)}  {msg}")


def warn(tag: str, msg: str) -> None:
    print(f"  {_YLW}[{tag}]{_R}  {_YLW}{msg}{_R}")


def error(tag: str, msg: str) -> None:
    print(f"  {_RED}[{tag}]{_R}  {_RED}{msg}{_R}")
