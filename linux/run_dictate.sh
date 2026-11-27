#!/usr/bin/env sh
# Linux: run Voice Dictation in the foreground, with console output in the
# terminal. Needs an X11/Xorg session; Wayland blocks global hooks and tray
# icons.
set -eu

cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
    echo "No .venv found. Run ./setup.sh in the linux folder first."
    exit 1
fi

exec .venv/bin/python dictate.py "$@"
