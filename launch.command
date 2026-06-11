#!/bin/bash
# Meeting Assistant launcher (macOS)
# Double-clickable in Finder. Mirrors launch.bat: ensure uv exists,
# create venv if missing, then hand off to launch.py.

set -e

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$ROOT/.venv"

# ── Ensure uv is available ───────────────────────────────────────────────────
UV=""
if command -v uv >/dev/null 2>&1; then
    UV="uv"
elif [ -x "$HOME/.local/bin/uv" ]; then
    UV="$HOME/.local/bin/uv"
    export PATH="$HOME/.local/bin:$PATH"
else
    # Try Homebrew location.
    if [ -x "/opt/homebrew/bin/uv" ]; then
        UV="/opt/homebrew/bin/uv"
        export PATH="/opt/homebrew/bin:$PATH"
    elif [ -x "/usr/local/bin/uv" ]; then
        UV="/usr/local/bin/uv"
        export PATH="/usr/local/bin:$PATH"
    fi
fi

if [ -z "$UV" ]; then
    echo
    echo "  Installing uv package manager..."
    echo
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
    if command -v uv >/dev/null 2>&1; then
        UV="uv"
    elif [ -x "$HOME/.local/bin/uv" ]; then
        UV="$HOME/.local/bin/uv"
    else
        echo
        echo "  Failed to install uv. Install manually:"
        echo "    https://docs.astral.sh/uv/getting-started/installation/"
        echo "    or: brew install uv"
        echo
        read -n 1 -s -r -p "Press any key to exit..."
        exit 1
    fi
fi

# ── Create venv if needed (uv auto-downloads Python 3.12 if not found) ──────
if [ ! -x "$VENV/bin/python" ]; then
    echo "  Creating Python environment..."
    "$UV" venv "$VENV" --python 3.12 --seed
fi

# ── Hand off to launch.py ──────────────────────────────────────────────────
exec "$VENV/bin/python" "$ROOT/launch.py"
