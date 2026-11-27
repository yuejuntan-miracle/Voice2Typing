#!/usr/bin/env sh
# macOS: create .venv, install the pinned dependencies and run the environment
# check. The app lives in the folder above this one, so everything is written
# there. For Windows use windows/setup.bat, for Linux linux/setup.sh.
set -eu

cd "$(dirname "$0")/.."

is_py310_plus() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

PYTHON="${PYTHON:-}"
if [ -n "$PYTHON" ]; then
    if ! command -v "$PYTHON" >/dev/null 2>&1 || ! is_py310_plus "$PYTHON"; then
        echo "Specified PYTHON ($PYTHON) is missing or older than Python 3.10."
        PYTHON=""
    fi
fi

if [ -z "$PYTHON" ]; then
    for candidate in \
        python3.13 python3.12 python3.11 python3.10 python3 \
        /opt/homebrew/bin/python3.12 /opt/homebrew/opt/python@3.12/bin/python3.12 \
        /usr/local/bin/python3.12 /usr/local/opt/python@3.12/bin/python3.12 \
        /Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 \
        /Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11; do
        if command -v "$candidate" >/dev/null 2>&1 && is_py310_plus "$candidate"; then
            PYTHON="$candidate"
            break
        fi
    done
fi

if [ -z "$PYTHON" ]; then
    echo "============================================================"
    echo "Python 3.10+ was not found on your system."
    echo "(macOS built-in /usr/bin/python3 is 3.9, which is too old.)"
    echo "Install Python 3.12 via Homebrew:"
    echo "  brew install python@3.12 python-tk@3.12"
    echo "Or download the official macOS installer from:"
    echo "  https://www.python.org/downloads/macos/"
    echo "============================================================"
    exit 1
fi

echo "Using $("$PYTHON" --version 2>&1) at $(command -v "$PYTHON")"

if [ -x .venv/bin/python ] && ! is_py310_plus .venv/bin/python; then
    echo "Existing .venv uses Python < 3.10; recreating..."
    rm -rf .venv
fi

if [ ! -d .venv ]; then
    echo "Creating .venv (this takes a moment)..."
    "$PYTHON" -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

echo
echo "Setup complete. Put your Gemini API key in config.json"
echo "(or set GEMINI_API_KEY), then run: ./macos/run_dictate.sh"
echo
.venv/bin/python dictate.py --doctor || true
