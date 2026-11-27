#!/bin/sh
# macOS: double-click this file in Finder to start Voice Dictation with a
# console that stays open, so errors are readable.
# The app lives in the folder above this one.
cd "$(dirname "$0")/.." || exit 1

if [ ! -x .venv/bin/python ]; then
    echo "No .venv found. Run ./setup.sh in the macos folder first."
    printf "Press Return to close."
    read -r _
    exit 1
fi

.venv/bin/python dictate.py
status=$?
if [ "$status" -ne 0 ]; then
    printf "Voice Dictation exited with status %s. Press Return to close." "$status"
    read -r _
fi
