#!/usr/bin/env sh
# macOS: run Voice Dictation in the foreground, with console output in the
# terminal. The tray icon appears after the model starts loading.
set -eu

cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
    echo "No .venv found. Run ./setup.sh in the macos folder first."
    exit 1
fi

exec .venv/bin/python dictate.py "$@"
