#!/usr/bin/env sh
# V2T one-line installer for macOS and Linux.
#
#   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/yuejuntan-miracle/V2T/main/install.sh)"
#
# It clones (or updates) the repo, creates a virtual environment, installs the
# Python dependencies (and, on Linux, the system packages pip cannot install),
# then asks for your Gemini API key and a couple of settings and writes
# config.json. Run it again any time to update.
set -eu

REPO_URL="https://github.com/yuejuntan-miracle/V2T.git"
DIR="${V2T_DIR:-$PWD/V2T}"

# Read interactively even when this script arrives over a pipe (curl | bash),
# where stdin is the script itself. Fall back to stdin if there is no tty.
if [ -r /dev/tty ]; then
    exec 3</dev/tty
else
    exec 3<&0
fi
ask() {                       # ask "prompt" -> echoes the trimmed reply
    printf '%s' "$1" >&2
    IFS= read -r _reply <&3 || _reply=""
    printf '%s' "$_reply"
}
ask_default() {               # ask_default "prompt" "default"
    _ans="$(ask "$1")"
    [ -n "$_ans" ] || _ans="$2"
    printf '%s' "$_ans"
}

echo "============================================================"
echo " V2T installer (built on Gemini by YJ)"
echo "============================================================"

# -- locate an interpreter (must be Python 3.10+) --------------------------
is_py310_plus() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

find_python() {
    if [ -n "${PYTHON:-}" ] && command -v "$PYTHON" >/dev/null 2>&1 && is_py310_plus "$PYTHON"; then
        printf '%s' "$PYTHON"
        return 0
    fi
    for c in \
        python3.13 python3.12 python3.11 python3.10 python3 \
        /opt/homebrew/bin/python3.12 /opt/homebrew/opt/python@3.12/bin/python3.12 \
        /usr/local/bin/python3.12 /usr/local/opt/python@3.12/bin/python3.12 \
        /Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 \
        /Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11; do
        if command -v "$c" >/dev/null 2>&1 && is_py310_plus "$c"; then
            printf '%s' "$c"
            return 0
        fi
    done
    return 1
}

PY="$(find_python || true)"

if [ -z "$PY" ]; then
    echo "Python 3.10+ not found (macOS built-in python3 is 3.9); trying to install it..."
    if [ "$(uname)" = "Darwin" ]; then
        if command -v brew >/dev/null 2>&1; then
            brew install python@3.12 python-tk@3.12 || true
        fi
    else
        if command -v apt >/dev/null 2>&1; then
            sudo apt update -y || true
            sudo apt install -y python3 python3-venv python3-tk || true
        elif command -v dnf >/dev/null 2>&1; then
            sudo dnf install -y python3 python3-tkinter || true
        elif command -v pacman >/dev/null 2>&1; then
            sudo pacman -S --noconfirm python python-virtualenv tk || true
        fi
    fi
    PY="$(find_python || true)"
fi

if [ -z "$PY" ]; then
    echo "Python 3.10+ is required but could not be installed automatically."
    if [ "$(uname)" = "Darwin" ]; then
        echo "  Install Homebrew (https://brew.sh), then: brew install python@3.12 python-tk@3.12"
        echo "  Or download: https://www.python.org/downloads/macos/"
    else
        echo "  Install it with your package manager, e.g.:"
        echo "    sudo apt install -y python3 python3-venv python3-tk"
    fi
    exit 1
fi
echo "Using $("$PY" --version 2>&1) at $(command -v "$PY")"

# -- get the code ----------------------------------------------------------
if ! command -v git >/dev/null 2>&1; then
    echo "git is required but was not found. Install git and re-run."
    exit 1
fi
if [ -d "$DIR/.git" ]; then
    echo "Updating existing checkout in $DIR ..."
    git -C "$DIR" pull --ff-only || echo "(could not pull; using the current copy)"
else
    echo "Cloning V2T into $DIR ..."
    git clone --depth 1 "$REPO_URL" "$DIR"
fi
cd "$DIR"

# -- Linux system packages pip cannot install ------------------------------
if [ "$(uname)" = "Linux" ]; then
    missing=""
    command -v dpkg >/dev/null 2>&1 && missing="libportaudio2 xclip"
    if [ -n "$missing" ] && command -v apt >/dev/null 2>&1; then
        echo "Installing Linux system packages (needs sudo) ..."
        sudo apt update -y || true
        # shellcheck disable=SC2086
        sudo apt install -y python3-venv python3-tk libportaudio2 xclip gcc python3-dev linux-libc-dev || \
            echo "(some system packages failed; continuing)"
    fi
fi

# -- virtual environment + Python deps ------------------------------------
if [ -x .venv/bin/python ] && ! is_py310_plus .venv/bin/python; then
    echo "Existing .venv uses Python < 3.10; recreating with $("$PY" --version 2>&1) ..."
    rm -rf .venv
fi
if [ ! -x .venv/bin/python ]; then
    echo "Creating virtual environment ..."
    "$PY" -m venv .venv
fi
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

# -- configuration ---------------------------------------------------------
echo
echo "Let's set up config.json (press Enter to accept the default)."
API_KEY="$(ask 'Gemini API key (required): ')"
while [ -z "$API_KEY" ]; do
    echo "The Gemini API key is required." >&2
    API_KEY="$(ask 'Gemini API key (required): ')"
done
LANGUAGE="$(ask_default 'Language [Auto]: ' 'Auto')"
TRIGGER="$(ask_default 'Push-to-talk shortcut [mouse:x2]: ' 'mouse:x2')"
FIX_ANS="$(ask_default 'Enable the English fixer (Gemma/Gemini)? [Y/n]: ' 'Y')"
case "$FIX_ANS" in
    n|N) REWRITE="false" ;;
    *)   REWRITE="true" ;;
esac

CONFIG_API_KEY="$API_KEY" CONFIG_LANG="$LANGUAGE" \
CONFIG_TRIGGER="$TRIGGER" CONFIG_REWRITE="$REWRITE" \
.venv/bin/python - <<'PY'
import json, os
path = "config.json"
cfg = {}
if os.path.exists(path):
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception:
        cfg = {}
cfg["gemini_api_key"] = os.environ["CONFIG_API_KEY"].strip()
cfg["language"] = os.environ["CONFIG_LANG"].strip() or "Auto"
cfg["trigger_shortcut"] = os.environ["CONFIG_TRIGGER"].strip() or "mouse:x2"
cfg["rewrite_enabled"] = os.environ["CONFIG_REWRITE"] == "true"
with open(path, "w", encoding="utf-8") as fh:
    json.dump(cfg, fh, indent=2)
    fh.write("\n")
print("Wrote", path)
PY

# -- done ------------------------------------------------------------------
OS="$(uname)"
if [ "$OS" = "Darwin" ]; then RUN="./macos/run_dictate.sh"; else RUN="./linux/run_dictate.sh"; fi
echo
echo "============================================================"
echo " Setup complete."
echo "   Start V2T:   cd \"$DIR\" && $RUN"
echo "   (or run in the foreground with: .venv/bin/python dictate.py)"
echo "============================================================"