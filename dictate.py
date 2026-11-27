"""
Gemini Voice Dictation for Windows.

Hold a mouse side button, speak, release. The clip is streamed to Gemini 3.5
Transcribe Live with your own API key, repaired by Gemma 4 26B on the same key,
and pasted into whatever window has focus.

Target: Windows laptop (tested on Core Ultra 228V / 32 GB RAM).

Usage:
    python dictate.py              run in the foreground (console output)
    python dictate.py --doctor     check dependencies, microphone and API key
    python dictate.py --doctor --full   also check the Gemini endpoint
    pythonw dictate.py             run without a console (see dictate.log)

Configuration lives in config.json next to this file and is created on first
run. Optional hotwords (names, jargon, identifiers) live in hotwords.txt.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
import logging.handlers
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import zlib
from typing import Any

import numpy as np
import sounddevice as sd
import soundfile as sf
from PIL import Image, ImageDraw
from pynput import keyboard, mouse

import context_awareness
import platform_support
import shortcuts
import rewrite
from trigger import create_trigger

try:  # optional speech engine: needs an API key, not a login
    import gemini_stt
except Exception:  # pragma: no cover - only on a broken install
    gemini_stt = None

# Windows consoles often use a legacy code page. Never let a Chinese/accented
# transcription crash logging (and therefore skip the paste).
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None:
        try:
            _stream.reconfigure(errors="replace")
        except Exception:
            pass

# ---------------------------------------------------------------- constants --
APP_NAME = "Voice Dictation"
VERSION = "2.0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
HOTWORDS_PATH = os.path.join(BASE_DIR, "hotwords.txt")
CORRECTIONS_PATH = os.path.join(BASE_DIR, "corrections.txt")
LOG_PATH = os.path.join(BASE_DIR, "dictate.log")
TMP_WAV = os.path.join(BASE_DIR, "last_recording.wav")

# English fixer models offered in the tray: (model id, menu label). All run
# on Google's Gemini API with the same key as transcription. Users can also
# set any custom Gemini model id in config.json or via --set-rewrite-model.
REWRITE_CHOICES = (
    (rewrite.DEFAULT_MODEL, "Gemma 4 26B — Recommended (fastest, ~1.6s)"),
    ("gemma-4-31b-it", "Gemma 4 31B — Highest quality"),
    ("gemini-3.1-flash-lite", "Gemini 3.1 Flash-Lite — Fast backup"),
    ("gemini-3.5-flash-lite", "Gemini 3.5 Flash-Lite — Balanced"),
)

# Gemini Live STT models offered in the tray: (model id, menu label).
STT_MODEL_CHOICES = (
    ("gemini-3.5-transcribe-live", "Gemini 3.5 Transcribe Live — Recommended (fastest STT)"),
    ("gemini-3.1-flash-live-preview", "Gemini 3.1 Flash Live — Alternative"),
)
MUTEX_NAME = "VoiceDictation_SingleInstance_v2"

DEFAULTS: dict[str, Any] = {
    # "Auto" lets Gemini detect the language. Short or noisy clips flip to
    # the wrong language, so pinning "English" (or "Chinese") is safer if you
    # only ever dictate one language.
    "language": "Auto",
    # Speech engine. "gemini" streams the clip to Google's Gemini 3.5
    # Transcribe Live over the public Live API, using an API key in
    # gemini_api_key (free tier available). See gemini_stt.py.
    "stt_engine": "gemini",
    # API key for Gemini transcription and the English fixer. This one key
    # covers both; GEMINI_API_KEY in the environment also works.
    "gemini_api_key": "",
    # Gemini Live speech-to-text model (see STT_MODEL_CHOICES).
    "gemini_stt_model": "gemini-3.5-transcribe-live",
    # Advanced: the order cloud speech providers are tried, one clip at a time.
    # Empty means just "stt_engine". The whole clip is buffered and replayed
    # into the next provider, so a dead socket costs latency, not the sentence.
    "stt_engine_order": [],
    # Optional sentence describing the speaker, sent to the model
    # together with the hotwords. Useful for a strong accent or
    # non-native grammar: it stops the model from tidying the words up.
    "accent": "",
    "trigger": "x2",                  # legacy: "x2" (Forward) or "x1" (Back)
    "trigger_shortcut": "",           # e.g. "mouse:x2", "ctrl+alt+j", "f8"
    "min_seconds": 0.5,               # ignore taps shorter than this
    "max_seconds": 120.0,             # auto-stop runaway recordings
    "sample_rate": 16000,             # Gemini Live expects 16 kHz mono
    "device": None,                   # None = system default, or device name / index
    "cleanup_enabled": True,          # tidy numbers / punctuation / fillers
    # Rewrite weak or misheard English into the clean English you meant,
    # with Gemma 4 on the Gemini API. Your exact words stay in the log.
    "rewrite_enabled": True,
    # The English fixer model (see REWRITE_CHOICES), hosted on the Gemini API.
    "rewrite_model": rewrite.DEFAULT_MODEL,
    # Inspect the focused window title/app (<1 ms, local-only) to extract
    # on-screen technical terms as dynamic hotwords and adapt style per app.
    "context_awareness": True,
    "trim_silence": True,             # trim lead-in/tail silence before ASR
    "auto_gain": True,                # boost quiet speech before ASR
    "suppress_browser_nav": True,     # swallow the shortcut system-wide (Windows)
    "keep_recording": True,           # keep last_recording.wav for inspection
    "use_hotwords": True,             # bias transcription with hotwords.txt
    "paste_delay": 0.10,              # delay between clipboard set and Ctrl+V
    "clipboard_restore_delay": 0.35,  # delay before restoring the clipboard
    "tray_enabled": True,
    # Small non-activating panel that shows what was transcribed. It never
    # takes focus, so the paste still lands in the window you were typing in.
    "overlay_enabled": True,
    "overlay_hide_after": 8.0,        # seconds before the collapsed panel hides
    "overlay_max_entries": 12,        # how many transcripts the panel keeps
    "auto_clipboard": True,           # keep the transcript on the clipboard
    "clipboard_history_size": 5,      # left-click the tray icon: last N copies
}

TRAY_LANGUAGES = (
    "Auto", "English", "Chinese", "Cantonese", "Japanese",
    "Korean", "Spanish", "French", "German", "Russian",
)

# Tray icon: a two-lamp traffic light. Green means the app is busy (recording
# or transcribing), red means it is idle. No other colours are used.
LAMP_RED = (236, 62, 54)
LAMP_GREEN = (52, 204, 96)
HOUSING = (34, 37, 42, 255)
RIM = (10, 11, 13, 255)

STATES = ("loading", "idle", "recording", "transcribing", "paused", "error")

# Which lamp is lit for each state. None = both lamps dark.
STATE_LAMPS = {
    "loading": "red",
    "idle": "red",
    "recording": "green",
    "transcribing": "green",
    "paused": None,
    "error": "red",
}

# ------------------------------------------------------------- Win32 basics --
ERROR_ALREADY_EXISTS = 183

if sys.platform == "win32":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateMutexW.argtypes = (
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_wchar_p,
    )
    _kernel32.CreateMutexW.restype = ctypes.c_void_p
    _kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
else:
    _kernel32 = None

_single_socket: socket.socket | None = None

# ----------------------------------------------------------------- logging --
log = logging.getLogger("dictate")


def setup_logging(verbose: bool = False, console: bool | None = None) -> None:
    """Log to a rotating file (so pythonw runs are diagnosable) and a console."""
    level = logging.DEBUG if verbose else logging.INFO
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    log.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    try:
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        file_handler.setLevel(level)
        log.addHandler(file_handler)
    except Exception:
        pass

    if console is None:
        console = sys.stderr is not None
    if console and sys.stderr is not None:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(fmt)
        stream_handler.setLevel(level)
        log.addHandler(stream_handler)

    if not log.handlers:
        log.addHandler(logging.NullHandler())


# ----------------------------------------------------------------- settings --
class Settings:
    """Small dict-backed settings object that persists to config.json."""

    def __init__(self, data: dict[str, Any], path: str = CONFIG_PATH):
        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "path", path)

    def __getattr__(self, name: str) -> Any:
        try:
            return self._data[name]
        except KeyError:
            raise AttributeError(name) from None

    def set(self, name: str, value: Any, persist: bool = True) -> None:
        self._data[name] = value
        if persist:
            self.save()

    def save(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
        except Exception as exc:
            log.warning("could not save %s: %s", self.path, exc)

    def as_dict(self) -> dict[str, Any]:
        return dict(self._data)


def _coerce(key: str, value: Any) -> Any:
    """Coerce a config value to the type of its default."""
    default = DEFAULTS[key]
    if default is None:
        return value
    try:
        if isinstance(default, list):
            # A list setting (the provider chain) may arrive as a JSON array or
            # as a comma-separated string; keep only the known engine ids.
            if isinstance(value, str):
                value = value.split(",")
            if not isinstance(value, (list, tuple, set)):
                return list(default)
            return [str(item).strip().lower() for item in value if str(item).strip()]
        if isinstance(default, bool):
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            return bool(value)
        if isinstance(default, float):
            return float(value)
        if isinstance(default, int):
            return int(value)
        return str(value)
    except (TypeError, ValueError):
        log.warning("invalid value for %s in config (%r); using default", key, value)
        return default


ENGINE_IDS = ("gemini",)


def resolve_engine_order(engine: str | None, order: object = None) -> list[str]:
    """Resolve the speech-provider chain for one clip.

    An explicit ``stt_engine_order`` wins; otherwise the single ``stt_engine``
    is the chain. Unknown ids and duplicates are dropped; the result is never
    empty.
    """
    chain: list[str] = []
    if order:
        items = order.split(",") if isinstance(order, str) else order
        try:
            iterator = list(items)  # type: ignore[arg-type]
        except TypeError:
            iterator = []
        for item in iterator:
            name = str(item).strip().lower()
            if name in ENGINE_IDS and name not in chain:
                chain.append(name)

    if not chain:
        name = str(engine or "gemini").strip().lower()
        if name not in ENGINE_IDS:
            name = "gemini"
        chain.append(name)

    return chain


def load_settings(path: str = CONFIG_PATH, write_if_missing: bool = True) -> Settings:
    """Merge config.json over the defaults, coercing values to sane types."""
    data = dict(DEFAULTS)
    path = os.path.abspath(path)

    if os.path.exists(path):
        try:
            # utf-8-sig: Notepad and PowerShell can save a BOM, which plain
            # utf-8 json parsing rejects (and the app fell back to defaults).
            with open(path, "r", encoding="utf-8-sig") as handle:
                raw = json.load(handle)
            if isinstance(raw, dict):
                for key, value in raw.items():
                    if key in DEFAULTS:
                        data[key] = _coerce(key, value)
                    else:
                        log.warning("unknown config key %r ignored", key)
        except Exception as exc:
            log.error("could not read %s (%s); using defaults", path, exc)
    elif write_if_missing:
        log.info("no config found, writing defaults to %s", path)

    data["min_seconds"] = max(0.2, float(data["min_seconds"]))
    data["max_seconds"] = max(data["min_seconds"] + 1.0, float(data["max_seconds"]))
    data["sample_rate"] = int(data["sample_rate"])
    trigger = str(data["trigger"]).strip().lower()
    data["trigger"] = trigger if trigger in ("x1", "x2") else "x2"

    # Version 1 stored only the mouse button. Derive the new shortcut value
    # once, then keep both keys in sync so an older build still works.
    raw_shortcut = str(data.get("trigger_shortcut") or "").strip().lower()
    if not raw_shortcut:
        raw_shortcut = f"mouse:{data['trigger']}"
    try:
        shortcut = shortcuts.parse(raw_shortcut)
    except ValueError:
        log.warning("invalid trigger_shortcut %r; using mouse:x2", raw_shortcut)
        shortcut = shortcuts.parse("mouse:x2")
    data["trigger_shortcut"] = shortcut.canonical()
    if shortcut.is_mouse and shortcut.button in ("x1", "x2"):
        data["trigger"] = shortcut.button

    settings = Settings(data, path)
    if write_if_missing and (not os.path.exists(path) or data != DEFAULTS):
        # Only rewrite when something actually differs from what is on disk,
        # so a hand-edited file keeps its formatting until a value changes.
        try:
            on_disk = None
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8-sig") as handle:
                    on_disk = json.load(handle)
            if on_disk != data:
                settings.save()
        except Exception:
            pass
    return settings


# ------------------------------------------------------------ pure helpers --
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"))
_WHITESPACE = re.compile(r"\s+")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.!?;:)\]}]|%|°)")


def classify_duration(seconds: float, min_seconds: float, max_seconds: float) -> str:
    """Return "short", "ok" or "long" for a recording length."""
    if seconds < min_seconds:
        return "short"
    if seconds > max_seconds:
        return "long"
    return "ok"


def trim_silence(
    audio: np.ndarray,
    sample_rate: int = 16000,
    threshold_db: float = -35.0,
    frame_ms: float = 20.0,
    pad_ms: float = 200.0,
) -> np.ndarray:
    """Trim leading/trailing near-silence.

    The threshold is relative to the loudest 20 ms frame, which keeps quiet
    speech intact. If nothing stands out the audio is returned unchanged.
    """
    if audio is None or audio.size == 0:
        return audio

    samples = audio.reshape(-1).astype(np.float32, copy=False)
    frame = max(1, int(sample_rate * frame_ms / 1000.0))
    frames = len(samples) // frame
    if frames < 2:
        return audio

    window = samples[: frames * frame].reshape(frames, frame)
    rms = np.sqrt(np.mean(window.astype(np.float64) ** 2, axis=1))
    loudest = float(np.max(rms))
    if loudest <= 1e-6:
        return audio

    threshold = loudest * (10.0 ** (threshold_db / 20.0))
    above = np.flatnonzero(rms >= threshold)
    if above.size == 0:
        return audio

    pad = int(sample_rate * pad_ms / 1000.0)
    start = max(0, int(above[0]) * frame - pad)
    end = min(len(samples), (int(above[-1]) + 1) * frame + pad)
    if end <= start:
        return audio
    return samples[start:end]


def speech_modulation_db(
    audio: np.ndarray,
    sample_rate: int = 16000,
    frame_ms: float = 20.0,
) -> float:
    """How far the frame loudness swings, in dB.

    Speech moves between syllables and the gaps between them, so a loud frame
    and a quiet frame can sit 15 dB or more apart. Room tone, a fan and mains
    hum stay within a couple of dB of themselves.
    """
    if audio is None or audio.size == 0:
        return 0.0
    samples = audio.reshape(-1).astype(np.float32, copy=False)
    frame = max(1, int(sample_rate * frame_ms / 1000.0))
    frames = len(samples) // frame
    if frames < 2:
        return 0.0

    window = samples[: frames * frame].reshape(frames, frame)
    rms = np.sqrt(np.mean(window.astype(np.float64) ** 2, axis=1))
    db = 20.0 * np.log10(np.maximum(rms, 1e-9))
    return float(np.percentile(db, 90.0) - np.percentile(db, 10.0))


def normalize_loudness(
    audio: np.ndarray,
    target_peak: float = 0.5,
    max_gain: float = 20.0,
) -> tuple[np.ndarray, float]:
    """Scale quiet speech up to a level the model hears reliably.

    A laptop array mic often records speech peaking near 0.05-0.1. At that
    level the model fills mumbled words with its own guesses (it turned
    "...four. This is me and I'm in Singapore" into "...five six seven eight
    nine ten"). The peak is taken at the 99.9th percentile so a single click
    cannot hold the gain down, and the gain is capped so room noise is not
    blown up into speech. Returns (audio, gain); loud audio is left alone.
    """
    if audio is None or audio.size == 0:
        return audio, 1.0
    peak = float(np.percentile(np.abs(audio), 99.9))
    if peak <= 1e-6 or peak >= target_peak:
        return audio, 1.0
    gain = min(max_gain, target_peak / peak)
    boosted = np.clip(audio * gain, -1.0, 1.0).astype(np.float32, copy=False)
    return boosted, gain


def looks_like_speech(
    audio: np.ndarray,
    sample_rate: int = 16000,
    min_modulation_db: float = 3.0,
    min_peak: float = 1e-3,
) -> bool:
    """Reject a clip that is too flat to be speech.

    This is a cheap gate, not a detector. A held note would pass it, but
    nothing that reaches a dictation shortcut sounds like a held note, while
    the flat clips that used to produce `????` do not.
    """
    if audio is None or audio.size == 0:
        return False
    if float(np.max(np.abs(audio))) < min_peak:
        return False
    return speech_modulation_db(audio, sample_rate) >= min_modulation_db


def has_words(text: str) -> bool:
    """True when a transcript contains at least one letter or digit."""
    return any(char.isalnum() for char in (text or ""))


def is_pasteable(text: str) -> bool:
    """Reject the junk the model emits when it hears noise.

    Besides empty and symbol-only output (`????`), this drops transcripts with
    replacement characters, control bytes or lone surrogates, which show up as
    mojibake in the log and in whatever window receives the paste.
    """
    if not has_words(text):
        return False
    for char in text:
        code = ord(char)
        if code == 0xFFFD or 0xD800 <= code <= 0xDFFF:
            return False
        if code < 32 and char not in "\n\r\t":
            return False
    return True


def _words_only(text: str) -> str:
    return " ".join(
        "".join(c if c.isalnum() else " " for c in (text or "").lower()).split()
    )


def echoes_context(text: str, context: str) -> bool:
    """True when the model repeated a sentence of its own prompt.

    On near-silent audio the model sometimes "transcribes" the system prompt
    ("This is a dictation session."). Only whole prompt sentences count, so
    saying a single hotword that appears in the prompt is never dropped.
    """
    spoken = _words_only(text)
    if not spoken or not context:
        return False
    sentences = [_words_only(s) for s in re.split(r"(?<=[.!?])\s+", context)]
    return spoken == _words_only(context) or spoken in sentences


def looks_like_english(detected: str) -> bool:
    """True when the detected language is English, or was not reported."""
    name = (detected or "").strip().lower()
    return not name or name.startswith("english")


def normalize_transcript(text: str) -> str:
    """Tidy spacing and invisible characters without changing the words.

    ASR output occasionally carries zero-width characters, doubled spaces or a
    space before a full stop. NFC leaves Chinese punctuation and accented
    letters alone, unlike NFKC.
    """
    if not text:
        return ""
    clean = unicodedata.normalize("NFC", str(text))
    clean = clean.translate(_INVISIBLE)
    clean = _WHITESPACE.sub(" ", clean).strip()
    clean = _SPACE_BEFORE_PUNCT.sub(r"\1", clean)
    return clean


def parse_hotwords(raw: str) -> list[str]:
    """Parse hotwords.txt: one term per line, '#' starts a comment."""
    terms: list[str] = []
    seen: set[str] = set()
    for line in (raw or "").splitlines():
        term = line.split("#", 1)[0].strip()
        if not term:
            continue
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        terms.append(term)
    return terms


def compose_context(terms: list[str], accent: str = "") -> str:
    """Build the system prompt used to bias transcription.

    `accent` is an optional sentence about the speaker. Saying that the
    speaker has, for example, a Singapore or Malaysian accent stops the
    model from correcting their grammar and helps it keep local particles.
    """
    accent = (accent or "").strip()
    sentences = ["This is a dictation session."]
    if accent:
        sentences.append(accent)
    if terms:
        sentences.append(
            "Use these domain words and spellings exactly when they are "
            "spoken: " + ", ".join(terms) + "."
        )
    if len(sentences) == 1:
        return ""
    return " ".join(sentences)


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(round(seconds)), 60)
    return f"{minutes}m{rest:02d}s"


def should_restore_clipboard(
    auto_clipboard: bool, previous: str | None, current: str | None, text: str
) -> bool:
    """Decide whether to put the old clipboard back after a paste.

    With auto_clipboard the transcript stays on the clipboard on purpose, so it
    can be pasted somewhere else without dictating again.
    """
    return not auto_clipboard and previous is not None and current == text


class ClipboardHistory:
    """The last few texts that passed through the clipboard, newest first."""

    def __init__(self, limit: int = 5):
        self.limit = max(1, min(20, int(limit)))
        self._items: list[str] = []
        self._lock = threading.Lock()

    def add(self, text: str) -> bool:
        """Record `text`; returns True when the history actually changed."""
        text = (text or "").strip()
        if not text:
            return False
        with self._lock:
            if self._items and self._items[0] == text:
                return False
            if text in self._items:
                self._items.remove(text)
            self._items.insert(0, text)
            del self._items[self.limit:]
        return True

    def items(self) -> list[str]:
        with self._lock:
            return list(self._items)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


# ----------------------------------------------------------- autostart ------
def _pythonw_path() -> str:
    return platform_support.python_executable()


def autostart_command() -> str:
    """Command line used by the Windows Run key / .desktop / LaunchAgent."""
    return subprocess.list2cmdline(
        platform_support.autostart_command(os.path.join(BASE_DIR, "dictate.py"))
    )


def autostart_enabled() -> bool:
    try:
        return platform_support.autostart_enabled()
    except Exception as exc:
        log.debug("could not read autostart state: %s", exc)
        return False


def set_autostart(enabled: bool) -> bool:
    ok = platform_support.set_autostart(enabled)
    if ok:
        log.info(
            "autostart %s: %s",
            "enabled" if enabled else "disabled",
            autostart_command(),
        )
    else:
        log.error("could not update autostart")
    return ok


def acquire_single_instance(name: str = MUTEX_NAME):
    """Claim the single-instance lock; False if another copy already has it."""
    global _single_socket

    if sys.platform == "win32":
        handle = _kernel32.CreateMutexW(None, False, name)
        last_error = ctypes.get_last_error()
        if not handle:
            log.error("CreateMutexW failed (WinError %s)", last_error)
            return None
        if last_error == ERROR_ALREADY_EXISTS:
            _kernel32.CloseHandle(handle)
            return False
        return handle

    # A bound localhost socket is released automatically when the process
    # exits, which gives the same protection without platform APIs.
    port = 47000 + (zlib.crc32(os.path.abspath(BASE_DIR).encode()) % 1000)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
    except OSError:
        sock.close()
        return False
    _single_socket = sock
    return sock


def release_single_instance(owner) -> None:
    global _single_socket
    if not owner:
        return
    try:
        if sys.platform == "win32":
            _kernel32.CloseHandle(owner)
        else:
            owner.close()
    except Exception:
        pass
    _single_socket = None


# ---------------------------------------------------------------- tray ------
def _dim(color: tuple[int, int, int], factor: float = 0.34) -> tuple[int, int, int]:
    return tuple(int(channel * factor) for channel in color)


def create_tray_image(state: str, size: int = 64) -> Image.Image:
    """Draw a two-lamp traffic light: green while busy, red while idle.

    Rendered at 4x and downsampled so the lamps stay smooth at the 16 px
    notification-area size Windows actually uses.
    """
    supersample = 4
    canvas = size * supersample
    img = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    scale = canvas / 64.0

    def px(value: float) -> float:
        return value * scale

    active = STATE_LAMPS.get(state, "red")

    # Housing
    draw.rounded_rectangle(
        [px(13), px(0), px(51), px(64)],
        radius=px(11),
        fill=HOUSING,
        outline=RIM,
        width=max(1, int(px(2))),
    )

    for name, center_y, color in (
        ("red", 16.5, LAMP_RED),
        ("green", 47.5, LAMP_GREEN),
    ):
        center_x = px(32)
        center_y_px = px(center_y)
        radius = px(11.5)
        lit = active == name
        body = color if lit else _dim(color)

        # Socket rim, then the lens.
        draw.ellipse(
            [center_x - radius, center_y_px - radius,
             center_x + radius, center_y_px + radius],
            fill=RIM,
        )
        inner = radius - px(1.6)
        draw.ellipse(
            [center_x - inner, center_y_px - inner,
             center_x + inner, center_y_px + inner],
            fill=body,
        )
        if lit:
            # Small specular highlight so the lit lamp reads as glass.
            glow = inner * 0.34
            offset = inner * 0.5
            draw.ellipse(
                [center_x - offset - glow, center_y_px - offset - glow,
                 center_x - offset + glow, center_y_px - offset + glow],
                fill=(255, 255, 255, 115),
            )

    return img.resize((size, size), Image.LANCZOS)


# ----------------------------------------------------------- audio devices --
_SKIP_MIC_NAMES = frozenset({
    "microsoft sound mapper - input",
    "primary sound capture driver",
})


def _preferred_input_hostapi() -> int:
    """Pick the best PortAudio host API for enumerating microphones.

    On Windows, DirectSound returns full untruncated device names and
    resamples cleanly to 16 kHz (whereas MME truncates names at 31 chars
    and WASAPI rejects 16 kHz on many built-in mics).
    """
    hostapis = sd.query_hostapis()
    if platform_support.IS_WINDOWS:
        for idx, api in enumerate(hostapis):
            if "directsound" in str(api.get("name", "")).lower():
                return idx
    default_api = getattr(sd.default, "hostapi", 0)
    return int(default_api) if isinstance(default_api, int) else 0


def list_input_devices() -> list[tuple[int, str]]:
    """Return deduplicated ``(device_index, name)`` pairs for available mics."""
    try:
        devices = sd.query_devices()
    except Exception:
        return []

    preferred = _preferred_input_hostapi()
    result: list[tuple[int, str]] = []
    seen: set[str] = set()

    def _collect(hostapi_filter: int | None) -> None:
        for idx, dev in enumerate(devices):
            if int(dev.get("max_input_channels", 0)) <= 0:
                continue
            if hostapi_filter is not None and int(dev.get("hostapi", -1)) != hostapi_filter:
                continue
            name = str(dev.get("name", "")).strip()
            if not name or name.lower() in _SKIP_MIC_NAMES:
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            result.append((idx, name))

    _collect(preferred)
    if not result:
        _collect(None)
    return result


def default_input_device_name() -> str:
    """Human-readable name of the current OS default input device."""
    try:
        default_in = sd.default.device[0]
        if default_in is None or default_in < 0:
            return "none"
        raw_name = str(sd.query_devices(default_in).get("name", "")).strip()
    except Exception:
        return "unknown"

    if not raw_name:
        return "unknown"
    # MME truncates default device names at 31 characters; match the full
    # DirectSound name when available so the label is readable.
    low = raw_name.lower()
    for _, full_name in list_input_devices():
        if full_name.lower() == low or full_name.lower().startswith(low):
            return full_name
    return raw_name


def resolve_input_device(configured: int | str | None) -> tuple[int | None, str]:
    """Resolve ``settings.device`` into ``(portaudio_index_or_None, label)``."""
    if configured is None or (isinstance(configured, str) and not configured.strip()):
        return None, f"System Default ({default_input_device_name()})"

    if isinstance(configured, int):
        try:
            name = str(sd.query_devices(configured).get("name", "")).strip()
        except Exception:
            name = f"#{configured}"
        return configured, name or f"#{configured}"

    target = str(configured).strip()
    target_low = target.lower()
    mics = list_input_devices()
    for idx, name in mics:
        if name.lower() == target_low:
            return idx, name
    for idx, name in mics:
        if name.lower().startswith(target_low) or target_low in name.lower():
            return idx, name
    raise ValueError(f"microphone not found: {target}")


def short_device_label(name: str, limit: int = 34) -> str:
    """Shorten a microphone name so tray menu headers stay compact."""
    flat = " ".join(str(name or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


class DictationTray:
    def __init__(self, app: "VoiceDictationApp"):
        self.app = app
        self._icon = None
        self._current_state = "loading"

    def _shortcut_action(self, value: str):
        def handler(_icon=None, _item=None):
            self.app.set_shortcut(value)

        return handler

    def _capture_shortcut(self, icon=None, item=None):
        self.app.begin_shortcut_capture()

    def _language_action(self, language: str):
        # pystray only accepts callbacks with zero, one or two parameters, so
        # the language has to be captured by a factory rather than a default
        # argument (a default raises the parameter count).
        def handler(_icon=None, _item=None):
            self.app.set_language(language)

        return handler

    def _language_checked(self, language: str):
        def handler(_item=None):
            return self.app.settings.language == language

        return handler

    def build_menu(self):
        import pystray

        language_items = [
            pystray.MenuItem(
                lang,
                self._language_action(lang),
                checked=self._language_checked(lang),
                radio=True,
            )
            for lang in TRAY_LANGUAGES
        ]

        items = [
            pystray.MenuItem(
                lambda _item: f"● {APP_NAME} ({self._current_state.upper()})",
                None,
                enabled=False,
            ),
            pystray.MenuItem(
                lambda _item: "Speech: Gemini 3.5 Transcribe Live",
                None,
                enabled=False,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                lambda _item: "Resume Dictation" if self.app.paused else "Pause Dictation",
                self._toggle_pause,
                checked=lambda _item: self.app.paused,
            ),
            pystray.MenuItem(
                lambda _item: f"Shortcut: {self.app.shortcut.display()}",
                pystray.Menu(
                    pystray.MenuItem(
                        "Detect a new shortcut…",
                        self._capture_shortcut,
                    ),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem(
                        lambda _item: (
                            f"Current: {self.app.shortcut.display()}"
                        ),
                        None,
                        enabled=False,
                    ),
                    pystray.MenuItem(
                        "Side button 2 (Forward)",
                        self._shortcut_action("mouse:x2"),
                        checked=lambda _item: self.app.shortcut.canonical() == "mouse:x2",
                        radio=True,
                    ),
                    pystray.MenuItem(
                        "Side button 1 (Back)",
                        self._shortcut_action("mouse:x1"),
                        checked=lambda _item: self.app.shortcut.canonical() == "mouse:x1",
                        radio=True,
                    ),
                    pystray.MenuItem(
                        "Middle click",
                        self._shortcut_action("mouse:middle"),
                        checked=lambda _item: self.app.shortcut.canonical() == "mouse:middle",
                        radio=True,
                    ),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem(
                        "Reset to Side button 2",
                        self._shortcut_action("mouse:x2"),
                    ),
                ),
            ),
            pystray.MenuItem("Dictation language", pystray.Menu(*language_items)),
            pystray.MenuItem(
                lambda _item: f"Microphone: {self._current_mic_label()}",
                pystray.Menu(lambda: self._mic_menu_items()),
            ),
            pystray.MenuItem(
                "Speech model (Gemini)",
                pystray.Menu(*self._stt_model_items()),
            ),
            pystray.MenuItem(
                "Clean up dictated text",
                self._toggle_cleanup,
                checked=lambda _item: self.app.cleanup_enabled,
            ),
            pystray.MenuItem(
                "Fix my English (Gemma 4, API)",
                self._toggle_rewrite,
                checked=lambda _item: bool(self.app.settings.rewrite_enabled),
            ),
            pystray.MenuItem(
                "App context awareness",
                self._toggle_context_awareness,
                checked=lambda _item: bool(getattr(self.app.settings, "context_awareness", True)),
            ),
            pystray.MenuItem("English fixer model", pystray.Menu(*self._rewrite_model_items())),
            pystray.MenuItem(
                "Transcript panel",
                self._toggle_overlay,
                checked=lambda _item: bool(self.app.settings.overlay_enabled),
            ),
            pystray.MenuItem(
                "Expand / collapse transcript",
                self._expand_overlay,
                enabled=lambda _item: self.app.overlay is not None,
            ),
            pystray.MenuItem(
                "Clear transcript history",
                self._clear_overlay,
                enabled=lambda _item: self.app.overlay is not None,
            ),
            pystray.MenuItem(
                "Clipboard history (left-click the icon)",
                self._show_clipboard,
                default=True,
            ),
            pystray.MenuItem("Clear clipboard history", self._clear_clipboard),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Copy last transcript",
                self._copy_last,
                enabled=lambda _item: bool(self.app.last_text),
            ),
            pystray.MenuItem(
                "Re-paste last transcript",
                self._repaste_last,
                enabled=lambda _item: bool(self.app.last_text),
            ),
            pystray.MenuItem("Open Last Recording (WAV)", self._open_last_wav),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open log file", self._open_log),
            pystray.MenuItem(
                platform_support.autostart_label(),
                self._toggle_autostart,
                checked=lambda _item: autostart_enabled(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Restart as Administrator",
                self._restart_admin,
                visible=lambda _item: (
                    platform_support.IS_WINDOWS and not self.app.is_elevated()
                ),
            ),
            pystray.MenuItem(f"Exit {APP_NAME}", self._on_exit),
        ]
        return pystray.Menu(*items)

    def start(self):
        if not self.app.settings.tray_enabled:
            log.info("tray disabled by config")
            return
        try:
            import pystray

            self._icon = pystray.Icon(
                "VoiceDictation",
                create_tray_image("loading"),
                f"{APP_NAME} - starting...",
                self.build_menu(),
            )
            self._icon.run_detached()
            log.info("tray icon started")
            if platform_support.IS_WINDOWS:
                def _auto_promote():
                    try:
                        time.sleep(1.5)
                        platform_support.promote_tray_icon()
                        if not getattr(self.app.settings, "tray_pin_tip_shown", False):
                            self.app.settings.set("tray_pin_tip_shown", True)
                            self.app._notify_panel(
                                "Tip: if the traffic light is hidden, drag it from the ^ menu onto the taskbar to pin it."
                            )
                    except Exception:
                        pass
                threading.Thread(target=_auto_promote, daemon=True, name="promote-tray").start()
        except Exception:
            log.exception("could not initialise the system tray icon")
            self._icon = None

    def set_state(self, state: str, tooltip_suffix: str = ""):
        self._current_state = state
        # Keep the transcript panel in step with every state change, including
        # the ones that talk to the tray directly.
        self.app._sync_overlay(state)
        if not self._icon:
            return
        try:
            self._icon.icon = create_tray_image(state)
            tip = f"{APP_NAME}: {state.capitalize()}"
            if tooltip_suffix:
                tip += f" ({tooltip_suffix})"
            self._icon.title = tip
            self._icon.update_menu()
        except Exception:
            # The icon may already be stopping during shutdown.
            pass

    def _toggle_pause(self, icon=None, item=None):
        self.app.toggle_pause()

    def _toggle_cleanup(self, icon=None, item=None):
        enabled = not self.app.cleanup_enabled
        self.app.set_cleanup(enabled)

    def _toggle_rewrite(self, icon=None, item=None):
        self.app.set_rewrite(not bool(self.app.settings.rewrite_enabled))

    def _toggle_context_awareness(self, icon=None, item=None):
        self.app.set_context_awareness(
            not bool(getattr(self.app.settings, "context_awareness", True))
        )

    def _stt_model_items(self) -> list:
        import pystray

        choices = list(STT_MODEL_CHOICES)
        current = getattr(self.app.settings, "gemini_stt_model", "gemini-3.5-transcribe-live")
        if current not in {m for m, _ in choices}:
            choices.append((current, f"{current} (custom)"))

        def pick(model_id):
            return lambda icon=None, item=None: self.app.set_stt_model(model_id)

        return [
            pystray.MenuItem(
                text,
                pick(model_id),
                checked=lambda _item, m=model_id: getattr(
                    self.app.settings, "gemini_stt_model", "gemini-3.5-transcribe-live"
                ) == m,
                radio=True,
            )
            for model_id, text in choices
        ]

    def _rewrite_model_items(self) -> list:
        import pystray

        choices = list(REWRITE_CHOICES)
        current = self.app.settings.rewrite_model
        if current not in {m for m, _ in choices}:
            choices.append((current, f"{current.split('/')[-1]} (custom)"))

        def pick(model_id):
            return lambda icon=None, item=None: self.app.set_rewrite_model(model_id)

        return [
            pystray.MenuItem(
                text,
                pick(model_id),
                checked=lambda _item, m=model_id: self.app.settings.rewrite_model == m,
                radio=True,
            )
            for model_id, text in choices
        ]

    def _current_mic_label(self) -> str:
        configured = getattr(self.app.settings, "device", None)
        if configured is None or (isinstance(configured, str) and not configured.strip()):
            return short_device_label(f"Default ({default_input_device_name()})")
        try:
            _, name = resolve_input_device(configured)
            return short_device_label(name)
        except Exception:
            return short_device_label(f"{configured} (disconnected)")

    def _mic_menu_items(self) -> list:
        import pystray

        configured = getattr(self.app.settings, "device", None)
        mics = list_input_devices()
        default_name = default_input_device_name()

        def pick(dev):
            return lambda icon=None, item=None: self.app.set_device(dev)

        def is_selected(idx: int, name: str) -> bool:
            cur = getattr(self.app.settings, "device", None)
            if cur is None:
                return False
            if isinstance(cur, int):
                return cur == idx
            return str(cur).strip().lower() == name.lower()

        items = [
            pystray.MenuItem(
                f"System Default ({short_device_label(default_name, 38)})",
                pick(None),
                checked=lambda _item: getattr(self.app.settings, "device", None) is None,
                radio=True,
            ),
            pystray.Menu.SEPARATOR,
        ]

        matched_configured = configured is None
        for idx, name in mics:
            if is_selected(idx, name):
                matched_configured = True
            items.append(
                pystray.MenuItem(
                    name,
                    pick(name),
                    checked=lambda _item, i=idx, n=name: is_selected(i, n),
                    radio=True,
                )
            )

        if not mics:
            items.append(pystray.MenuItem("No microphones found", None, enabled=False))

        if not matched_configured:
            items.append(
                pystray.MenuItem(
                    f"{configured} (disconnected)",
                    None,
                    checked=lambda _item: True,
                    radio=True,
                    enabled=False,
                )
            )

        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem("Refresh microphones", self._refresh_mics))
        return items

    def _refresh_mics(self, icon=None, item=None):
        self.app.refresh_devices()

    def _toggle_overlay(self, icon=None, item=None):
        self.app.set_overlay_enabled(not self.app.settings.overlay_enabled)

    def _expand_overlay(self, icon=None, item=None):
        if self.app.overlay is not None:
            self.app.overlay.toggle()

    def _clear_overlay(self, icon=None, item=None):
        if self.app.overlay is not None:
            self.app.overlay.clear_history()

    def _show_clipboard(self, icon=None, item=None):
        self.app.show_clipboard_history()

    def _clear_clipboard(self, icon=None, item=None):
        self.app.clear_clipboard_history()

    def _toggle_autostart(self, icon=None, item=None):
        set_autostart(not autostart_enabled())
        if self._icon:
            try:
                self._icon.update_menu()
            except Exception:
                pass

    def _copy_last(self, icon=None, item=None):
        if self.app.last_text and self.app.copy_to_clipboard(self.app.last_text):
            log.info("last transcript copied to clipboard")

    def _repaste_last(self, icon=None, item=None):
        if self.app.last_text:
            self.app.paste_text(self.app.last_text)

    def _open_last_wav(self, icon=None, item=None):
        if os.path.exists(TMP_WAV):
            try:
                platform_support.open_path(TMP_WAV)
            except Exception as exc:
                log.warning("could not open %s: %s", TMP_WAV, exc)
        else:
            log.info("no recording file yet")

    def _open_log(self, icon=None, item=None):
        if os.path.exists(LOG_PATH):
            try:
                platform_support.open_path(LOG_PATH)
            except Exception as exc:
                log.warning("could not open %s: %s", LOG_PATH, exc)
        else:
            log.info("no log file yet")

    def _restart_admin(self, icon=None, item=None):
        self.app.restart_as_admin()

    def _on_exit(self, icon=None, item=None):
        log.info("exit requested from the tray menu")
        self.app.stop()
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass


# ------------------------------------------------------------------- app ----
class VoiceDictationApp:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or load_settings()

        self.shortcut = shortcuts.try_parse(
            getattr(self.settings, "trigger_shortcut", ""),
            fallback=f"mouse:{self.settings.trigger}",
        )
        self.paused = False
        self.running = True
        self.cleanup_enabled = bool(self.settings.cleanup_enabled)
        self.last_text = ""
        # Why the speech engine produced nothing, when it was the engine and
        # not the microphone. Keeps the useful message on screen.
        self._stt_failure = ""

        self._rewriter: rewrite.Rewriter | None = None
        self._rewriter_lock = threading.Lock()
        self._current_context = context_awareness.AppContext()
        # (time, text) of recent pastes: topic context for the English fixer.
        self._recent: list[tuple[float, str]] = []
        self._corrections_cache: tuple[float | None, list] = (None, [])

        self._frames: list[np.ndarray] = []
        self._live_session = None
        self._stream = None
        self._recording = False
        self._record_started_at = 0.0
        self._stream_error = False
        self._last_status_log = 0.0
        self._last_level_at = 0.0
        self._opening_stream = False
        self._busy = False

        self._rec_lock = threading.Lock()
        self._audio_open_lock = threading.Lock()
        self._job_queue: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._kb = keyboard.Controller()

        self._hotword_cache: tuple[
            tuple[float | None, str] | None, str
        ] = (None, "")

        self.clipboard_history = ClipboardHistory(
            int(getattr(self.settings, "clipboard_history_size", 5))
        )
        self._clip_seen = ""

        # The transcript panel is created lazily (it imports tkinter) and is
        # only entered into `self.overlay` once it is actually usable.
        self.overlay = None
        if self.settings.overlay_enabled:
            try:
                from overlay import TranscriptOverlay

                self.overlay = TranscriptOverlay(self.settings, log=log)
            except Exception:
                log.exception("the transcript panel is unavailable")
                self.overlay = None

        self.tray = DictationTray(self)
        self.trigger = None
        self._capturing = False
        self._capture = None

    # -- state ------------------------------------------------------------
    def is_elevated(self) -> bool:
        if not platform_support.IS_WINDOWS:
            return False
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    @property
    def trigger_name(self) -> str:
        """Legacy name of a mouse shortcut (x1/x2), else its display text."""
        if self.shortcut.is_mouse:
            return self.shortcut.button
        return self.shortcut.display()

    def _idle_tooltip(self) -> str:
        return f"Hold {self.shortcut.display()} to dictate"

    def _engine_order(self) -> list[str]:
        """The providers to try for one clip, in order."""
        return resolve_engine_order(
            getattr(self.settings, "stt_engine", "gemini"),
            getattr(self.settings, "stt_engine_order", []),
        )

    def _in_chain(self, engine: str) -> bool:
        """True when ``engine`` is anywhere in the chain, not only first."""
        return engine in self._engine_order()

    @property
    def primary_engine(self) -> str:
        """The engine the tray radio reflects: the first in the chain."""
        return self._engine_order()[0]

    @property
    def uses_gemini(self) -> bool:
        """True when Gemini is the primary engine (drives the tray check)."""
        return self.primary_engine == "gemini" and gemini_stt is not None

    def set_stt_engine(self, engine: str) -> None:
        engine = str(engine).lower()
        if engine not in ENGINE_IDS:
            engine = "gemini"
        if gemini_stt is None:
            log.error("the Gemini speech engine is unavailable (websocket-client missing)")
            self._notify_panel("Gemini speech is not available - check the log")
            return
        self.settings.set("stt_engine", engine)
        # A hand-edited provider chain would keep the tray radio from being
        # what actually runs, so choosing an engine here resets it to simple.
        if getattr(self.settings, "stt_engine_order", None):
            self.settings.set("stt_engine_order", [])
        log.info("speech engine: Gemini Transcribe Live (API key)")
        self._notify_panel("Speech: Gemini (online)")
        self.refresh_state()

    def refresh_state(self, busy: bool | None = None) -> None:
        if not self.running:
            return
        if busy is not None:
            self._busy = busy
        if self._busy:
            state, tooltip = "transcribing", "Transcribing..."
        elif self.paused:
            state, tooltip = "paused", "Dictation paused"
        elif self._recording:
            state, tooltip = "recording", "Recording speech..."
        else:
            state, tooltip = "idle", self._idle_tooltip()

        self.tray.set_state(state, tooltip)

    def _sync_overlay(self, state: str) -> None:
        """Mirror the app state into the transcript panel, if it is running."""
        overlay = self.overlay
        if overlay is None:
            return
        try:
            overlay.set_state(state, self._record_started_at or None)
        except Exception:
            log.debug("transcript panel state update failed", exc_info=True)

    def _add_overlay_entry(self, text: str) -> None:
        overlay = self.overlay
        if overlay is None or not text:
            return
        try:
            overlay.add_entry(text)
        except Exception:
            log.debug("could not add transcript to the panel", exc_info=True)

    def _notify_panel(self, message: str) -> None:
        """Show a single line in the panel when nothing was pasted."""
        overlay = self.overlay
        if overlay is None or not message:
            return
        try:
            overlay.notify(message)
        except Exception:
            log.debug("could not show a panel notice", exc_info=True)

    def show_clipboard_history(self) -> None:
        """Left-clicking the tray icon shows the recent clipboard items."""
        if self.overlay is None:
            self.set_overlay_enabled(True)
        items = self.clipboard_history.items()
        log.info("clipboard history opened (%d item(s))", len(items))
        if self.overlay is None:
            return
        try:
            self.overlay.show_clipboard(items)
        except Exception:
            log.debug("could not show the clipboard panel", exc_info=True)

    def clear_clipboard_history(self) -> None:
        self.clipboard_history.clear()
        log.info("clipboard history cleared")
        overlay = self.overlay
        if overlay is not None and overlay.clipboard_open:
            try:
                overlay.show_clipboard([])
            except Exception:
                pass

    def _clipboard_watcher(self) -> None:
        """Remember every text that lands on the clipboard.

        Polling is deliberate: a clipboard-format listener would need its own
        message-only window, and reading four times a second is invisible.
        """
        while self.running:
            time.sleep(0.6)
            if not self.running:
                return
            try:
                text = platform_support.get_clipboard()
            except Exception:
                continue
            if not isinstance(text, str) or not text.strip():
                self._clip_seen = ""
                continue
            if text == self._clip_seen:
                continue
            self._clip_seen = text
            self.clipboard_history.add(text)

    def set_overlay_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        self.settings.set("overlay_enabled", enabled)
        if not enabled:
            if self.overlay is not None:
                try:
                    self.overlay.stop()
                except Exception:
                    log.debug("could not stop the transcript panel", exc_info=True)
                self.overlay = None
            log.info("transcript panel disabled")
        elif self.overlay is None:
            try:
                from overlay import TranscriptOverlay

                self.overlay = TranscriptOverlay(self.settings, log=log)
                self.overlay.start()
                log.info("transcript panel enabled")
            except Exception:
                log.exception("could not start the transcript panel")
                self.overlay = None
        self.refresh_state()
        if self.tray._icon:
            try:
                self.tray._icon.update_menu()
            except Exception:
                pass

    def restart_as_admin(self):
        if not platform_support.IS_WINDOWS:
            return
        try:
            params = " ".join(f'"{arg}"' for arg in sys.argv)
            ctypes.windll.shell32.ShellExecuteW(
                None, "runas", sys.executable, params, None, 1
            )
            self.stop()
            if self.tray._icon:
                self.tray._icon.stop()
            sys.exit(0)
        except Exception as exc:
            log.error("could not relaunch as administrator: %s", exc)

    # -- shortcut ---------------------------------------------------------
    def _listener_active(self) -> bool:
        return self.running and not self.paused and not self._capturing

    def set_shortcut(self, value: str) -> bool:
        """Validate, persist and apply a new shortcut without a restart."""
        try:
            shortcut = shortcuts.parse(value)
        except ValueError as exc:
            log.warning("shortcut %r rejected: %s", value, exc)
            return False
        problem = shortcut.problem()
        if problem:
            log.warning("shortcut %r rejected: %s", value, problem)
            return False

        self.shortcut = shortcut
        self.settings.set("trigger_shortcut", shortcut.canonical())
        if shortcut.is_mouse and shortcut.button in ("x1", "x2"):
            # Keep the pre-2.0 key valid in case an older build is started.
            self.settings.set("trigger", shortcut.button)
        log.info("shortcut set to %s (%s)", shortcut.display(), shortcut.canonical())
        if self.trigger is not None:
            self.trigger.reset()
        self.refresh_state()
        if self.tray._icon:
            try:
                self.tray._icon.update_menu()
            except Exception:
                pass
        return True

    def set_trigger(self, trigger: str) -> None:
        """Backwards-compatible helper for the old x1/x2 config values."""
        if trigger in ("x1", "x2"):
            self.set_shortcut(f"mouse:{trigger}")

    def begin_shortcut_capture(self) -> None:
        if self._capturing:
            return
        if self.paused:
            log.info("resume dictation before recording a shortcut")
            return
        if self._recording:
            log.info("finish the current dictation before recording a shortcut")
            return

        if self.overlay is None:
            self.set_overlay_enabled(True)
        overlay = self.overlay
        if overlay is None:
            log.warning("the shortcut recorder needs the transcript panel")
            return

        from trigger import ShortcutCapture

        self._capturing = True
        if self.trigger is not None:
            self.trigger.reset()
        overlay.on_capture_cancel = self.cancel_shortcut_capture
        overlay.show_capture("Waiting for a key, combination or click…")
        self._capture = ShortcutCapture(
            on_done=self._shortcut_captured,
            on_cancel=self._shortcut_cancelled,
            on_preview=self._shortcut_preview,
            log=log,
        )
        if not self._capture.start():
            self._capturing = False
            self._capture = None
            overlay.finish_capture(
                "Global input is not permitted — check the system settings", ok=False
            )
            log.warning("shortcut recorder could not read global input")

    def cancel_shortcut_capture(self) -> None:
        capture = self._capture
        if capture is not None:
            capture.cancel()
        else:
            self._end_capture()
            if self.overlay is not None:
                self.overlay.hide_capture()

    def _shortcut_preview(self, text: str) -> None:
        if self.overlay is not None and self._capturing:
            self.overlay.update_capture(text)

    def _shortcut_captured(self, shortcut: shortcuts.Shortcut) -> None:
        ok = self.set_shortcut(shortcut.canonical())
        text = shortcut.display() + (" is now the trigger" if ok else " is not usable")
        if self.overlay is not None:
            self.overlay.finish_capture(text, ok=ok)
        self._end_capture()

    def _shortcut_cancelled(self) -> None:
        if self.overlay is not None:
            self.overlay.hide_capture()
        log.info("shortcut recording cancelled")
        self._end_capture()

    def _end_capture(self) -> None:
        self._capturing = False
        capture = self._capture
        self._capture = None
        if capture is not None:
            capture.stop()
        if self.overlay is not None:
            self.overlay.on_capture_cancel = None
        if self.trigger is not None:
            self.trigger.reset()

    def set_language(self, language: str) -> None:
        self.settings.set("language", language)
        log.info("dictation language set to %s", language)
        self.refresh_state()

    def set_rewrite(self, enabled: bool) -> None:
        self.settings.set("rewrite_enabled", enabled)
        log.info("English rewrite %s", "enabled" if enabled else "disabled")
        if enabled:
            self._ensure_rewriter()
        self._refresh_menu()

    def set_context_awareness(self, enabled: bool) -> None:
        enabled = bool(enabled)
        self.settings.set("context_awareness", enabled)
        log.info("app context awareness %s", "enabled" if enabled else "disabled")
        self._refresh_menu()

    def set_stt_model(self, model_id: str) -> None:
        """Swap the Gemini Live speech-to-text model."""
        model_id = (model_id or "").strip()
        if not model_id:
            return
        self.settings.set("gemini_stt_model", model_id)
        log.info("Gemini speech model set to %s", model_id)
        self._notify_panel(f"Speech model: {model_id}")
        self._refresh_menu()

    def set_rewrite_model(self, model_id: str) -> None:
        """Swap the English fixer model used on the Gemini API."""
        if model_id == self.settings.rewrite_model and model_id == getattr(
            self._rewriter, "model_id", None
        ):
            return
        self.settings.set("rewrite_model", model_id)
        log.info("English fixer model set to %s", model_id)
        with self._rewriter_lock:
            old, self._rewriter = self._rewriter, None
        if old is not None:
            old.close()
        self._ensure_rewriter()
        self._refresh_menu()

    def set_device(self, device: str | int | None) -> None:
        """Switch the active input microphone live and persist it."""
        if isinstance(device, str) and not device.strip():
            device = None
        self.settings.set("device", device)
        try:
            _, label = resolve_input_device(device)
        except Exception:
            label = str(device)
        log.info("microphone set to %s", label)
        self._refresh_menu()
        if self.paused:
            self._notify_panel(f"Microphone: {short_device_label(label, 42)}")
            return

        def _switch():
            self.close_audio_stream()
            if self._open_audio_stream():
                self._notify_panel(f"Microphone: {short_device_label(label, 42)}")
                self.refresh_state()
            else:
                self._notify_panel(f"Could not open microphone: {short_device_label(label, 34)}")
                self.refresh_state()

        threading.Thread(target=_switch, daemon=True, name="mic-switch").start()

    def refresh_devices(self) -> None:
        """Re-enumerate PortAudio devices so newly plugged-in mics appear."""
        def _work():
            was_paused = self.paused
            self.close_audio_stream()
            try:
                sd._terminate()
                sd._initialize()
            except Exception as exc:
                log.warning("could not re-enumerate audio devices: %s", exc)
            mics = list_input_devices()
            if not was_paused and self.running:
                self._open_audio_stream()
            self._refresh_menu()
            self.refresh_state()
            self._notify_panel(f"Microphones refreshed ({len(mics)} found)")
            log.info("microphones refreshed: %d input device(s) found", len(mics))

        threading.Thread(target=_work, daemon=True, name="mic-refresh").start()

    def _refresh_menu(self) -> None:
        if self.tray._icon:
            try:
                self.tray._icon.update_menu()
            except Exception:
                pass

    def _make_rewriter(self, model_id: str) -> "rewrite.Rewriter":
        key = str(getattr(self.settings, "gemini_api_key", "") or "")
        if gemini_stt is not None:
            key = gemini_stt.api_key(key)
        return rewrite.Rewriter(model_id, key)

    def _ensure_rewriter(self) -> None:
        """Create the API-backed fixer if it is not ready yet.

        Nothing is downloaded or loaded, so this is instant.
        """
        if not self.settings.rewrite_enabled:
            return
        wanted = self.settings.rewrite_model
        with self._rewriter_lock:
            current = self._rewriter
            if current is not None and current.model_id == wanted and current.ready:
                return
            new = self._make_rewriter(wanted)
            ok = new.load()
            old = current
            if ok:
                self._rewriter = new
        if not ok:
            log.warning("English fixer is unavailable: %s", new.error)
            return
        if old is not None and old is not new:
            old.close()

    def _active_rewriter(self) -> "rewrite.Rewriter | None":
        """The fixer to use now, or None (the raw text is pasted)."""
        self._ensure_rewriter()
        current = self._rewriter
        if current is not None and current.ready:
            return current
        return None

    def _load_rewriter(self) -> None:
        """Kept for callers that start the fixer at startup."""
        self._ensure_rewriter()

    def _hotword_terms(self) -> list[str]:
        try:
            with open(HOTWORDS_PATH, "r", encoding="utf-8") as handle:
                return parse_hotwords(handle.read())
        except OSError:
            return []

    def _corrections(self) -> list:
        try:
            mtime = os.path.getmtime(CORRECTIONS_PATH)
        except OSError:
            return []
        cached_mtime, pairs = self._corrections_cache
        if cached_mtime != mtime:
            try:
                with open(CORRECTIONS_PATH, "r", encoding="utf-8-sig") as handle:
                    pairs = rewrite.parse_corrections(handle.read())
            except OSError as exc:
                log.warning("could not read corrections: %s", exc)
                pairs = []
            self._corrections_cache = (mtime, pairs)
        return pairs

    def apply_corrections(self, text: str) -> str:
        pairs = self._corrections()
        if not text or not pairs:
            return text
        fixed = rewrite.apply_corrections(text, pairs)
        if fixed != text:
            log.info("corrections: %r", fixed)
        return fixed

    def recent_context(self, now: float | None = None) -> str:
        """The last two dictations from the past 3 minutes."""
        now = time.time() if now is None else now
        self._recent = [(t, s) for t, s in self._recent if now - t <= 180.0][-2:]
        return " ".join(s for _t, s in self._recent)

    def remember(self, text: str, now: float | None = None) -> None:
        self._recent.append((time.time() if now is None else now, text))
        self._recent = self._recent[-2:]

    def maybe_rewrite(
        self,
        text: str,
        alternative: str = "",
        app_context: context_awareness.AppContext | None = None,
    ) -> str:
        if not text or not self.settings.rewrite_enabled:
            return text
        rewriter = self._active_rewriter()
        if rewriter is None:
            log.info("English fixer still loading; pasting the transcript as heard")
            return text
        corrections = self._corrections()
        if alternative and corrections:
            alternative = rewrite.apply_corrections(alternative, corrections)
        terms = self._hotword_terms()
        style_hint = ""
        if app_context is not None and not app_context.sensitive:
            terms = context_awareness.merge_hotwords(terms, app_context.dynamic_terms)
            style_hint = context_awareness.style_hint_for_category(app_context.category)
        return rewriter.rewrite(
            text,
            accent=str(getattr(self.settings, "accent", "") or ""),
            terms=terms,
            alternative=alternative,
            recent=self.recent_context(),
            style_hint=style_hint,
        )

    def set_cleanup(self, enabled: bool) -> None:
        self.cleanup_enabled = enabled
        self.settings.set("cleanup_enabled", enabled)
        log.info("text cleanup %s", "enabled" if enabled else "disabled")
        if self.tray._icon:
            try:
                self.tray._icon.update_menu()
            except Exception:
                pass

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        if self.trigger is not None:
            self.trigger.reset()
        if self.paused:
            self.close_audio_stream()
            log.info("dictation PAUSED")
            self.refresh_state()
            return
        if not self._open_audio_stream():
            self.paused = True
            log.warning("resume failed: microphone unavailable")
            self.tray.set_state("paused", "Microphone unavailable")
            return
        log.info("dictation RESUMED")
        self.refresh_state()

    # -- model ------------------------------------------------------------

    def _context(self, extra_terms: tuple[str, ...] | list[str] = ()) -> str:
        accent = str(getattr(self.settings, "accent", "") or "").strip()
        terms: list[str] = []
        mtime: float | None = None
        extra_key = tuple(extra_terms)

        if self.settings.use_hotwords:
            try:
                mtime = os.path.getmtime(HOTWORDS_PATH)
            except OSError:
                mtime = None
            if mtime is not None and not extra_key:
                cached_key, cached_context = self._hotword_cache
                if cached_key == (mtime, accent):
                    return cached_context
            if mtime is not None:
                try:
                    with open(HOTWORDS_PATH, "r", encoding="utf-8") as handle:
                        terms = parse_hotwords(handle.read())
                except Exception as exc:
                    log.warning("could not read hotwords: %s", exc)
                    terms = []

        if extra_key:
            terms = context_awareness.merge_hotwords(terms, extra_key)
        context = compose_context(terms, accent)
        if mtime is not None and not extra_key:
            self._hotword_cache = ((mtime, accent), context)
        if terms:
            log.debug("hotwords in use: %s", ", ".join(terms))
        return context

    def transcribe_with_engine(self, audio, app_context=None) -> str:
        """Run one clip through the provider chain, replaying it on failure.

        Ported from cloudstream's ``DictationSession``: the whole clip is
        already buffered, so a provider that dies halfway costs latency, not
        the sentence — the identical audio is replayed into the next provider.
        A provider that returns text ends the clip. A provider that returns an
        empty result *without* an error is real silence, and stops the chain.
        """
        self._stt_failure = ""
        order = self._engine_order()
        tried: list[str] = []
        last_label = ""
        last_error = ""

        for engine in order:
            if engine in tried:
                continue
            tried.append(engine)

            if engine == "gemini":
                if gemini_stt is None:
                    last_error = "Gemini speech is not available - check the log"
                    last_label = "Gemini"
                    continue
                label = "Gemini"
                self._stt_failure = ""
                text = normalize_transcript(
                    self.transcribe_gemini(audio, app_context)
                )

            if text:
                return text

            error = self._stt_failure
            if not error:
                # The provider answered, and the answer was emptiness: the
                # user said nothing, so replaying the clip cannot help.
                return ""
            last_error = error
            last_label = label
            log.warning(
                "%s speech unavailable (%s); trying the next engine", label, error
            )

        self._stt_failure = last_error
        return ""

    def _gemini_keyterms(
        self, app_context: context_awareness.AppContext | None = None
    ) -> list[str]:
        if not getattr(self.settings, "use_hotwords", True):
            return []
        terms = self._hotword_terms()
        if app_context is not None and not app_context.sensitive:
            terms = context_awareness.merge_hotwords(terms, app_context.dynamic_terms)
        return [term for term in terms if term][:100]

    def _start_live_gemini_session(
        self, app_context: context_awareness.AppContext | None = None
    ):
        """Open a live Gemini WebSocket while the user speaks (never blocks)."""
        if self.primary_engine != "gemini" or gemini_stt is None:
            return None
        key = gemini_stt.api_key(getattr(self.settings, "gemini_api_key", ""))
        if not key:
            return None
        session = gemini_stt.LiveSession(
            sample_rate=int(self.settings.sample_rate),
            language_codes=gemini_stt.code_for_language(self.settings.language),
            keyterms=self._gemini_keyterms(app_context),
            api_key_value=key,
            model=getattr(self.settings, "gemini_stt_model", gemini_stt.MODEL),
        )
        return session if session.start() else None

    def transcribe_gemini(
        self,
        audio: np.ndarray,
        app_context: context_awareness.AppContext | None = None,
    ) -> str:
        """Transcribe one clip on Google's Gemini 3.5 Transcribe Live endpoint.

        Uses an API key from config.json (gemini_api_key) or GEMINI_API_KEY.
        Hotwords go in as custom vocabulary, which is the nearest thing this
        endpoint has to biasing. Auto language detection is used unless a
        language is pinned in the tray.
        """
        if gemini_stt is None:
            self._stt_failure = "Gemini speech is not available - check the log"
            self._notify_panel(self._stt_failure)
            return ""
        key = gemini_stt.api_key(getattr(self.settings, "gemini_api_key", ""))
        if not key:
            self._stt_failure = "No Gemini API key - add gemini_api_key to config.json"
            self._notify_panel(self._stt_failure)
            return ""
        keyterms = self._gemini_keyterms(app_context)
        try:
            return gemini_stt.transcribe(
                audio,
                int(self.settings.sample_rate),
                gemini_stt.code_for_language(self.settings.language),
                keyterms,
                api_key_value=key,
                model=getattr(self.settings, "gemini_stt_model", gemini_stt.MODEL),
            )
        except gemini_stt.SttError as exc:
            log.error("Gemini speech failed: %s", exc)
            self._stt_failure = str(exc)
            self._notify_panel(self._stt_failure)
            return ""
        except Exception:
            log.exception("Gemini speech failed")
            self._stt_failure = "Gemini speech failed - see the log"
            self._notify_panel(self._stt_failure)
            return ""

    # -- audio ------------------------------------------------------------
    def _audio_callback(self, indata, frames, time_info, status):
        # The stream stays open so the mouse hook never waits for the audio
        # device. Only collect data while a recording is active.
        if status:
            # "input overflow" only means this block arrived late, which is
            # normal while the app starts and the CPU is busy. It is not a
            # reason to tear the device down; just record it now and then.
            now = time.time()
            if now - self._last_status_log > 10.0:
                self._last_status_log = now
                log.warning("audio stream status: %s", status)
        if not self._recording:
            return
        try:
            live = None
            chunk = None
            with self._rec_lock:
                if self._recording:
                    chunk = indata.copy()
                    self._frames.append(chunk)
                    live = self._live_session
            if live is not None and chunk is not None:
                live.push(chunk)
            # Feed the panel's level meter from the same block, about 15 times
            # a second rather than once per 64 ms block.
            panel = self.overlay
            now = time.time()
            if panel is not None and now - self._last_level_at > 0.06:
                self._last_level_at = now
                level = np.sqrt(np.mean(np.square(indata, dtype=np.float64)))
                panel.set_level(float(level))
        except Exception:
            log.exception("audio callback failed")

    def _on_stream_finished(self):
        """PortAudio calls this when the stream stops without being asked,
        which is how a removed or reassigned microphone shows up."""
        if self.running and not self.paused and self._stream is not None:
            log.warning("microphone stream stopped unexpectedly; will reopen")
            self._stream_error = True

    def _open_audio_stream(self) -> bool:
        """Open the microphone once and keep it warm.

        Opening an input stream takes 200-300 ms on this laptop, which is too
        slow to do inside a low-level mouse hook (Windows removes hooks that
        block) and would clip the first word of every recording.
        """
        with self._audio_open_lock:
            if self._stream is not None:
                return True
            if self._opening_stream:
                return False
            self._opening_stream = True
            try:
                dev_idx, dev_label = resolve_input_device(self.settings.device)
                stream = sd.InputStream(
                    samplerate=int(self.settings.sample_rate),
                    channels=1,
                    dtype="float32",
                    device=dev_idx,
                    # 64 ms blocks. The default is 0, which lets PortAudio pick
                    # tiny blocks; the log showed "input overflow" whenever the
                    # model pegged the CPU, and a larger block rides that out.
                    blocksize=1024,
                    callback=self._audio_callback,
                    finished_callback=self._on_stream_finished,
                )
                stream.start()
            except Exception as exc:
                log.error("could not open the microphone: %s", exc)
                return False
            finally:
                self._opening_stream = False
            self._stream = stream
            self._stream_error = False
            idx_tag = f"#{dev_idx} " if dev_idx is not None else ""
            log.info("microphone stream open and ready: %s%s", idx_tag, dev_label)
            return True

    def _open_stream_in_background(self) -> None:
        def work():
            if self._open_audio_stream():
                self.refresh_state()

        threading.Thread(target=work, daemon=True, name="mic-open").start()

    def close_audio_stream(self) -> None:
        """Stop capture and release the microphone (used by pause/exit)."""
        with self._rec_lock:
            self._recording = False
            self._frames = []
            live, self._live_session = self._live_session, None
        if live is not None:
            live.cancel()
        with self._audio_open_lock:
            stream, self._stream = self._stream, None
            if stream is not None:
                try:
                    stream.stop()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass
                log.info("microphone stream closed")

    def resume_capture(self) -> bool:
        return self._open_audio_stream()

    def start_recording(self) -> None:
        if self.paused or not self.running:
            return
        with self._rec_lock:
            if self._recording:
                return
            self._frames = []
            self._recording = True
            self._record_started_at = time.time()
            if getattr(self.settings, "context_awareness", True):
                self._current_context = context_awareness.capture_active_context()
            else:
                self._current_context = context_awareness.AppContext()
            app_context = self._current_context
            old_live, self._live_session = self._live_session, None
        if old_live is not None:
            old_live.cancel()
        live = self._start_live_gemini_session(app_context)
        if live is not None:
            with self._rec_lock:
                if self._recording:
                    self._live_session = live
                else:
                    live.cancel()
        # Never block the mouse hook: if the mic is not open yet, open it on a
        # worker thread and start collecting as soon as frames arrive.
        if self._stream is None:
            self._open_stream_in_background()
        if self._current_context.active:
            log.debug(
                "active context: app=%s category=%s sensitive=%s terms=%s",
                self._current_context.process_name,
                self._current_context.category,
                self._current_context.sensitive,
                ", ".join(self._current_context.dynamic_terms),
            )
        log.info("recording started")
        self.refresh_state()

    def stop_recording_and_process(self) -> None:
        with self._rec_lock:
            if not self._recording:
                return
            self._recording = False
            frames, self._frames = self._frames, []
            app_context = self._current_context
            self._current_context = context_awareness.AppContext()
            live_session, self._live_session = self._live_session, None
        self._record_started_at = 0.0

        def _abort_live():
            if live_session is not None:
                live_session.cancel()

        if not frames:
            _abort_live()
            log.info("no audio captured (microphone not ready)")
            self._notify_panel("Microphone not ready")
            self.refresh_state()
            return

        audio = np.concatenate(frames).reshape(-1).astype(np.float32, copy=False)
        raw_seconds = len(audio) / float(self.settings.sample_rate)

        if self.settings.trim_silence:
            audio = trim_silence(audio, int(self.settings.sample_rate))
            audio = np.ascontiguousarray(audio, dtype=np.float32)

        seconds = len(audio) / float(self.settings.sample_rate)
        verdict = classify_duration(
            seconds, float(self.settings.min_seconds), float(self.settings.max_seconds)
        )

        if verdict == "short":
            _abort_live()
            log.info(
                "ignored a %.2fs clip (minimum is %.1fs); hold the button while speaking",
                seconds,
                float(self.settings.min_seconds),
            )
            self._notify_panel("Hold the shortcut while you speak")
            self.refresh_state()
            return

        if verdict == "long":
            cap = int(float(self.settings.max_seconds) * float(self.settings.sample_rate))
            log.warning(
                "clip was %s, trimming to the %.0fs maximum",
                format_duration(raw_seconds),
                float(self.settings.max_seconds),
            )
            audio = np.ascontiguousarray(audio[:cap], dtype=np.float32)
            seconds = len(audio) / float(self.settings.sample_rate)

        if seconds < float(self.settings.min_seconds):
            _abort_live()
            self.refresh_state()
            return

        if not looks_like_speech(audio, int(self.settings.sample_rate)):
            _abort_live()
            log.info(
                "ignored a flat %.2fs clip (no speech modulation); "
                "check the microphone",
                seconds,
            )
            self._notify_panel("Heard nothing. Check the microphone")
            self.refresh_state()
            return

        gain = 1.0
        if getattr(self.settings, "auto_gain", True):
            audio, gain = normalize_loudness(audio)
            if gain > 1.0:
                log.info("boosted quiet audio x%.1f", gain)

        log.info("queued %s of audio for transcription", format_duration(seconds))
        self._job_queue.put((audio, seconds, app_context, live_session, gain))

    def _transcription_worker(self) -> None:
        """Serialize inference so rapid dictations cannot overlap."""
        while True:
            item = self._job_queue.get()
            if item is None:
                self._job_queue.task_done()
                return
            if not self.running:
                if len(item) >= 4 and item[3] is not None:
                    item[3].cancel()
                self._job_queue.task_done()
                continue

            live_session = None
            gain = 1.0
            if len(item) >= 5:
                audio, seconds, app_context, live_session, gain = item[:5]
            elif len(item) == 3:
                audio, seconds, app_context = item
            else:
                audio, seconds = item
                app_context = context_awareness.AppContext()
            try:
                self.refresh_state(busy=True)
                if self.settings.keep_recording:
                    try:
                        sf.write(TMP_WAV, audio, int(self.settings.sample_rate))
                    except Exception as exc:
                        log.warning("could not save %s: %s", TMP_WAV, exc)

                log.info("transcribing %s of audio ...", format_duration(seconds))
                started = time.time()
                raw_text = ""
                if live_session is not None:
                    try:
                        raw_text = normalize_transcript(live_session.finish())
                    except Exception as exc:
                        log.warning(
                            "Gemini live stream failed (%s); replaying buffered clip",
                            exc,
                        )
                        raw_text = self.transcribe_with_engine(audio, app_context)
                    else:
                        if not raw_text and gain > 2.0:
                            log.info(
                                "live stream heard silence on quiet clip (gain x%.1f); "
                                "replaying boosted audio",
                                gain,
                            )
                            raw_text = self.transcribe_with_engine(audio, app_context)
                else:
                    raw_text = self.transcribe_with_engine(audio, app_context)
                text = postprocess_text(raw_text) if self.cleanup_enabled else raw_text
                elapsed = time.time() - started

                if raw_text != text:
                    log.debug("raw transcript: %r", raw_text)
                log.info("transcribed in %.2fs: %r", elapsed, text)

                if text and is_pasteable(text):
                    text = self.apply_corrections(text)
                    if self.settings.rewrite_enabled:
                        heard = text
                        text = normalize_transcript(
                            self.maybe_rewrite(heard, app_context=app_context)
                        )
                        if text != heard:
                            log.info("fixed English: %r", text)
                    self.remember(text)

                dropped = False
                if text and not is_pasteable(text):
                    log.info("discarded a transcript that is not clean text")
                    text = ""
                    dropped = True
                    self._notify_panel("Nothing clear enough to paste")

                if text and self.running:
                    self.last_text = text
                    self._add_overlay_entry(text)
                    self.paste_text(text)
                elif not text:
                    if self._stt_failure:
                        # The engine already said what went wrong; do not replace
                        # "the login was rejected" with "check your microphone".
                        log.warning("no transcript: %s", self._stt_failure)
                    else:
                        log.info("no speech detected")
                        if not dropped:
                            self._notify_panel("Heard nothing. Check the microphone")
            except Exception:
                log.exception("transcription failed")
            finally:
                self._job_queue.task_done()
                self.refresh_state(busy=False)

    # -- clipboard / paste ------------------------------------------------
    def copy_to_clipboard(self, text: str, attempts: int = 5) -> bool:
        for attempt in range(attempts):
            if platform_support.set_clipboard(text):
                return True
            if attempt == attempts - 1:
                log.error("could not write to the clipboard")
            time.sleep(0.05)
        return False

    def _clipboard_get(self) -> str | None:
        return platform_support.get_clipboard()

    def paste_text(self, text: str) -> None:
        if not text:
            return
        previous = self._clipboard_get()
        if not self.copy_to_clipboard(text):
            return

        modifier_name = platform_support.paste_modifier()
        modifier = getattr(keyboard.Key, modifier_name, keyboard.Key.ctrl)
        key = platform_support.paste_key()
        try:
            time.sleep(float(self.settings.paste_delay))
            with self._kb.pressed(modifier):
                self._kb.press(key)
                self._kb.release(key)
            log.info("pasted %d characters", len(text))
            time.sleep(float(self.settings.clipboard_restore_delay))
        except Exception:
            log.exception("could not send the paste chord")
        finally:
            # Only restore if our text is still on the clipboard, so a paste
            # that lands late never gets overwritten by the old contents.
            if should_restore_clipboard(
                bool(getattr(self.settings, "auto_clipboard", True)),
                previous,
                self._clipboard_get(),
                text,
            ):
                self.copy_to_clipboard(previous)

    # -- trigger ----------------------------------------------------------
    def create_trigger(self):
        return create_trigger(
            get_shortcut=lambda: self.shortcut,
            on_start=self.start_recording,
            on_stop=self.stop_recording_and_process,
            active=self._listener_active,
            log=log,
            suppress_mouse=lambda: bool(self.settings.suppress_browser_nav),
        )

    # -- watchdog ---------------------------------------------------------
    def _watchdog(self) -> None:
        """Auto-stop long recordings and recover a dropped microphone."""
        last_reopen = 0.0
        while self.running:
            time.sleep(0.25)
            if not self.running:
                return
            try:
                if (
                    self._recording
                    and self._record_started_at
                    and time.time() - self._record_started_at
                    > float(self.settings.max_seconds) + 2
                ):
                    log.warning("maximum recording length reached; stopping")
                    threading.Thread(
                        target=self.stop_recording_and_process, daemon=True
                    ).start()

                if self._stream_error:
                    self._stream_error = False
                    log.warning("audio stream reported an error; reopening")
                    self.close_audio_stream()
                    last_reopen = time.time()
                    if not self.paused:
                        self._open_stream_in_background()
                elif (
                    self._stream is None
                    and not self.paused
                    and not self._opening_stream
                    and time.time() - last_reopen > 3.0
                ):
                    last_reopen = time.time()
                    self._open_stream_in_background()
            except Exception:
                log.exception("watchdog iteration failed")

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        log.info("=" * 62)
        log.info("%s %s", APP_NAME, VERSION)
        log.info("language   : %s", self.settings.language)
        log.info("trigger    : %s (%s)", self.shortcut.display(), self.shortcut.canonical())
        log.info("platform   : %s", platform_support.SYSTEM)
        log.info("clipboard  : %s", platform_support.clipboard_backend_name())
        log.info("cleanup    : %s", "on" if self.cleanup_enabled else "off")
        log.info(
            "rewrite    : %s",
            self.settings.rewrite_model if self.settings.rewrite_enabled else "off",
        )
        log.info("min/max    : %.1fs / %.0fs", self.settings.min_seconds, self.settings.max_seconds)
        log.info(
            "speech     : %s",
            " -> ".join(self._engine_order()),
        )
        log.info("panel      : %s", "on" if self.overlay is not None else "off")
        log.info("clipboard  : auto=%s history=%d", "on" if self.settings.auto_clipboard else "off", self.clipboard_history.limit)
        log.info("=" * 62)

        self.tray.start()

        if self.overlay is not None:
            try:
                self.overlay.start()
                log.info("transcript panel started")
            except Exception:
                log.exception("could not start the transcript panel")
                self.overlay = None

        if self._in_chain("gemini"):
            if not gemini_stt.api_key(getattr(self.settings, "gemini_api_key", "")):
                log.warning("Gemini API key is missing; add gemini_api_key to config.json")
                self._notify_panel("Gemini API key missing - add it to config.json")

        if not self._open_audio_stream():
            log.warning("microphone not available yet; will retry on first press")

        self._worker = threading.Thread(
            target=self._transcription_worker, daemon=True, name="asr-worker"
        )
        self._worker.start()
        threading.Thread(target=self._watchdog, daemon=True, name="watchdog").start()
        self._ensure_rewriter()
        threading.Thread(
            target=self._clipboard_watcher, daemon=True, name="clipboard-watch"
        ).start()

        try:
            self.trigger = self.create_trigger()
            self.trigger.run()
        except KeyboardInterrupt:
            log.info("interrupted")
        finally:
            self.stop()

    def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        try:
            self._job_queue.put(None)
        except Exception:
            pass
        self.close_audio_stream()
        if self.overlay is not None:
            try:
                self.overlay.stop()
            except Exception:
                log.debug("could not stop the transcript panel", exc_info=True)
            self.overlay = None
        if self.trigger is not None:
            try:
                self.trigger.stop()
            except Exception:
                pass
            self.trigger = None
        capture = self._capture
        self._capture = None
        if capture is not None:
            try:
                capture.stop()
            except Exception:
                pass
        log.info("voice dictation stopped")


# ---------------------------------------------------------------- doctor ----
def run_doctor(settings: Settings, full: bool = False) -> int:
    """Check the things that most often break a fresh install."""
    import urllib.request

    problems = 0
    print(f"{APP_NAME} {VERSION} - environment check")
    print("-" * 62)
    print(f"python      : {sys.version.split()[0]} ({sys.executable})")
    print(f"config      : {settings.path}")
    print(f"platform    : {platform_support.SYSTEM}")
    try:
        hw = platform_support.system_hardware_report()
        if hw.get("cpu"):
            print(f"cpu         : {hw['cpu']} ({hw['cores']} cores)")
        if hw.get("ram_total_gb") is not None:
            free_s = f", {hw['ram_free_gb']} GB free" if hw.get("ram_free_gb") is not None else ""
            print(f"memory      : {hw['ram_total_gb']} GB total{free_s}")
        if hw.get("disk_free_gb") is not None:
            print(f"disk space  : {hw['disk_free_gb']} GB free")
        if hw.get("gpus"):
            for g in hw["gpus"]:
                print(f"gpu         : {g}")
    except Exception:
        pass
    print(f"shortcut    : {shortcuts.describe(settings.trigger_shortcut)}")
    print(f"clipboard   : {platform_support.clipboard_backend_name()}")
    for status, text in platform_support.permission_report():
        print(f"  {status:<4} {text}")
        if status == "fail":
            problems += 1

    for module in (
        "numpy",
        "sounddevice",
        "soundfile",
        "PIL",
        "pynput",
        "pyperclip",
        "pystray",
        "tkinter",
        "websocket",
    ):
        try:
            imported = __import__(module)
            version = getattr(imported, "__version__", "?")
            print(f"  ok  {module:<12} {version}")
        except Exception as exc:
            problems += 1
            print(f"  FAIL {module:<12} {exc}")

    key = ""
    if gemini_stt is not None:
        key = gemini_stt.api_key(getattr(settings, "gemini_api_key", ""))
    if key:
        print(f"  ok  gemini key   ...{key[-4:]}")
    else:
        problems += 1
        print("  FAIL gemini key   missing - put gemini_api_key in config.json or set GEMINI_API_KEY")
    print(f"  ok  speech       Gemini 3.5 Transcribe Live")
    print(f"  ok  fixer        {settings.rewrite_model if settings.rewrite_enabled else 'off'}")

    try:
        dev_idx, dev_label = resolve_input_device(settings.device)
        stream = sd.InputStream(
            samplerate=int(settings.sample_rate),
            channels=1,
            dtype="float32",
            device=dev_idx,
        )
        stream.start()
        stream.stop()
        stream.close()
        idx_tag = f"#{dev_idx} " if dev_idx is not None else ""
        print(f"  ok  microphone   {idx_tag}{dev_label} @ {settings.sample_rate} Hz")
    except Exception as exc:
        problems += 1
        print(f"  FAIL microphone   {exc}")

    print(f"  {'ok ' if autostart_enabled() else '-- '} autostart")
    print(f"  ok  hotwords     {HOTWORDS_PATH} ({'present' if os.path.exists(HOTWORDS_PATH) else 'not created yet'})")
    print(f"  ok  log          {LOG_PATH}")

    if full:
        print("-" * 62)
        print("checking the Gemini API key against both models (no tokens spent)...")
        for label, model in (
            ("transcribe", getattr(settings, "gemini_stt_model", getattr(gemini_stt, "MODEL", "gemini-3.5-transcribe-live"))),
            ("fixer", settings.rewrite_model),
        ):
            if not key:
                break
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
            request = urllib.request.Request(
                url, headers={"x-goog-api-key": key}, method="GET"
            )
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    response.read()
                print(f"  ok  {label:<12} {model}")
            except Exception as exc:
                problems += 1
                print(f"  FAIL {label:<12} {model}: {exc}")

    print("-" * 62)
    if problems:
        print(f"{problems} problem(s) found.")
    else:
        print("Everything looks good.")
    return 1 if problems else 0


# ------------------------------------------------------------------ main ----
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gemini voice dictation.")
    parser.add_argument("--config", default=CONFIG_PATH, help="path to config.json")
    parser.add_argument("--doctor", action="store_true", help="check the environment")
    parser.add_argument(
        "--full", action="store_true", help="with --doctor, also check the Gemini models"
    )
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    parser.add_argument(
        "--set-shortcut",
        metavar="KEYS",
        help='save a trigger without the tray, e.g. "ctrl+alt+j" or "mouse:x2"',
    )
    parser.add_argument(
        "--list-models",
        action="store_true",
        help="show recommended speech and English-fixer models",
    )
    parser.add_argument(
        "--set-stt-model",
        metavar="MODEL",
        help='set the Gemini speech model, e.g. "gemini-3.5-transcribe-live"',
    )
    parser.add_argument(
        "--set-rewrite-model",
        metavar="MODEL",
        help='set the English-fixer model, e.g. "gemma-4-26b-a4b-it" or "gemini-3.1-flash-lite"',
    )
    parser.add_argument(
        "--list-mics",
        action="store_true",
        help="list available microphones",
    )
    parser.add_argument(
        "--set-mic",
        metavar="DEVICE",
        help='set the microphone by name, index, or "default"',
    )
    args = parser.parse_args(argv)

    setup_logging(verbose=args.verbose, console=args.doctor or args.verbose)
    settings = load_settings(args.config)

    if args.list_mics:
        cur = getattr(settings, "device", None)
        default_name = default_input_device_name()
        mics = list_input_devices()
        print("Microphones:")
        mark_def = "*" if cur is None else " "
        print(f"  {mark_def} default   System Default ({default_name})")
        for idx, name in mics:
            is_cur = (
                (isinstance(cur, int) and cur == idx)
                or (isinstance(cur, str) and cur.strip().lower() == name.lower())
            )
            mark = "*" if is_cur else " "
            print(f"  {mark} #{idx:<8} {name}")
        print('\nTip: set a microphone with --set-mic "<name or index>" or --set-mic default.')
        return 0

    if args.set_mic is not None:
        raw = args.set_mic.strip()
        if not raw or raw.lower() in {"default", "auto", "none"}:
            settings.set("device", None)
            print(f"Microphone saved: System Default ({default_input_device_name()})")
            return 0
        chosen: int | str = int(raw) if raw.isdigit() else raw
        try:
            dev_idx, dev_label = resolve_input_device(chosen)
        except ValueError as exc:
            print(f"Invalid microphone: {exc}")
            print("Run --list-mics to see available devices.")
            return 2
        # Save by full device name so USB plug/unplug renumbering doesn't break it.
        settings.set("device", dev_label)
        print(f"Microphone saved: #{dev_idx} {dev_label}")
        return 0

    if args.list_models:
        cur_stt = getattr(settings, "gemini_stt_model", "gemini-3.5-transcribe-live")
        cur_rw = settings.rewrite_model
        print("Speech models (Gemini Live):")
        for mid, label in STT_MODEL_CHOICES:
            mark = "*" if mid == cur_stt else " "
            print(f"  {mark} {mid:<32} {label}")
        if cur_stt not in {m for m, _ in STT_MODEL_CHOICES}:
            print(f"  * {cur_stt:<32} (custom)")
        print("\nEnglish-fixer models (Gemini API):")
        for mid, label in REWRITE_CHOICES:
            mark = "*" if mid == cur_rw else " "
            print(f"  {mark} {mid:<32} {label}")
        if cur_rw not in {m for m, _ in REWRITE_CHOICES}:
            print(f"  * {cur_rw:<32} (custom)")
        print("\nTip: set any custom Gemini model with --set-stt-model or --set-rewrite-model.")
        return 0

    if args.set_stt_model:
        model_id = args.set_stt_model.strip()
        settings.set("gemini_stt_model", model_id)
        print(f"Speech model saved: {model_id}")
        return 0

    if args.set_rewrite_model:
        model_id = args.set_rewrite_model.strip()
        settings.set("rewrite_model", model_id)
        print(f"English-fixer model saved: {model_id}")
        return 0

    if args.set_shortcut:
        try:
            shortcut = shortcuts.parse(args.set_shortcut)
        except ValueError as exc:
            print(f"Invalid shortcut: {exc}")
            return 2
        problem = shortcut.problem()
        if problem:
            print(f"Invalid shortcut: {problem}")
            return 2
        settings.set("trigger_shortcut", shortcut.canonical())
        if shortcut.is_mouse and shortcut.button in ("x1", "x2"):
            settings.set("trigger", shortcut.button)
        print(f"Shortcut saved: {shortcut.display()} ({shortcut.canonical()})")
        return 0

    if args.doctor:
        return run_doctor(settings, full=args.full)

    owner = acquire_single_instance()
    if owner is False:
        message = f"{APP_NAME} is already running (look for the tray icon)."
        log.warning(message)
        if sys.stderr is None and platform_support.IS_WINDOWS:
            ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x40)
        else:
            print(message)
        return 0
    if owner is None:
        log.error("could not create the single-instance lock; continuing anyway")

    app = VoiceDictationApp(settings)
    try:
        app.start()
    finally:
        release_single_instance(owner)
    return 0




# ================================================================ #
# Merged from text_cleanup.py: deterministic number/punctuation/filler
# cleanup used before the English fixer.
# ================================================================ #
_DIGIT_WORDS = {
    "zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
}
_TEEN_WORDS = {
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19,
}
_TENS_WORDS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_SCALE_WORDS = {
    "hundred": 100, "thousand": 1_000, "million": 1_000_000,
    "billion": 1_000_000_000,
}
_ALL_NUMBER_WORDS = (
    set(_DIGIT_WORDS) | set(_TEEN_WORDS) | set(_TENS_WORDS) | set(_SCALE_WORDS)
)
_NUMBER_WORD_PATTERN = "|".join(
    sorted(_ALL_NUMBER_WORDS, key=len, reverse=True)
)

# A number phrase may contain spaces, hyphens, "and" after a scale, and a
# decimal "point"/"dot" followed by digit words.
_NUM_PHRASE_RE = re.compile(
    rf"(?i)(?<![\w'-])"
    rf"(?:(?:minus|negative|a|an)[\s-]+)?"
    rf"(?:{_NUMBER_WORD_PATTERN})"
    rf"(?:"
    rf"[\s-]+(?:and[\s-]+)?(?:{_NUMBER_WORD_PATTERN})"
    rf"|[\s-]+(?:point|dot)[\s-]+(?:{_NUMBER_WORD_PATTERN})"
    rf"(?:[\s-]+(?:{_NUMBER_WORD_PATTERN}))*"
    rf")*"
    rf"(?![\w'-])"
)


def _parse_cardinal(words: list[str]) -> int | None:
    """Parse a cardinal number phrase, or return None if it is not valid."""
    if not words:
        return None

    implicit_one = False
    if words[0] in ("a", "an"):
        implicit_one = True
        words = words[1:]
        if not words or words[0] not in _SCALE_WORDS:
            return None

    cleaned: list[str] = []
    for i, word in enumerate(words):
        if word == "and":
            # "one hundred and five" is fine; "one and two" is not a
            # number phrase and must be left as words.
            if i == 0 or i == len(words) - 1:
                return None
            if words[i - 1] not in _SCALE_WORDS:
                return None
            continue
        if word not in _ALL_NUMBER_WORDS:
            return None
        cleaned.append(word)

    if not cleaned:
        return None
    if implicit_one:
        cleaned.insert(0, "one")

    total = 0
    current = 0
    last: str | None = None
    last_scale: int | None = None

    for word in cleaned:
        if word in _DIGIT_WORDS:
            digit = _DIGIT_WORDS[word]
            if last in ("digit", "teen", "unit"):
                return None
            if last == "tens":
                if digit == 0:
                    return None
                current += digit
                last = "unit"
            else:
                current += digit
                last = "digit"

        elif word in _TEEN_WORDS:
            if last in ("digit", "teen", "tens", "unit"):
                return None
            if current % 100 != 0:
                return None
            current += _TEEN_WORDS[word]
            last = "teen"

        elif word in _TENS_WORDS:
            if last in ("digit", "teen", "tens", "unit"):
                return None
            if current % 100 != 0:
                return None
            current += _TENS_WORDS[word]
            last = "tens"

        elif word == "hundred":
            if last in ("hundred", "scale"):
                return None
            if current >= 100:
                return None
            current = (current or 1) * 100
            last = "hundred"

        elif word in _SCALE_WORDS:
            scale = _SCALE_WORDS[word]
            if last == "scale" and current == 0:
                return None
            if last_scale is not None and scale >= last_scale:
                return None
            total += (current or 1) * scale
            current = 0
            last = "scale"
            last_scale = scale

        else:
            return None

    return total + current


def _parse_decimal(words: list[str], negative: bool) -> str | None:
    """Parse a decimal phrase such as 'three point five'."""
    if sum(word in ("point", "dot") for word in words) != 1:
        return None
    index = next(
        i for i, word in enumerate(words) if word in ("point", "dot")
    )
    int_words = words[:index]
    frac_words = words[index + 1:]
    if not frac_words or any(word not in _DIGIT_WORDS for word in frac_words):
        return None

    if not int_words:
        int_str = "0"
    elif all(word in _DIGIT_WORDS for word in int_words):
        int_str = "".join(str(_DIGIT_WORDS[word]) for word in int_words)
    else:
        value = _parse_cardinal(int_words)
        if value is None:
            return None
        int_str = str(value)

    frac_str = "".join(str(_DIGIT_WORDS[word]) for word in frac_words)
    sign = "-" if negative else ""
    return f"{sign}{int_str}.{frac_str}"


def _parse_number_phrase(words: list[str]) -> str | None:
    if not words:
        return None

    negative = False
    if words[0] in ("minus", "negative"):
        negative = True
        words = words[1:]
        if not words:
            return None

    if any(word in ("point", "dot") for word in words):
        return _parse_decimal(words, negative)

    if all(word in _DIGIT_WORDS for word in words):
        if len(words) >= 2:
            digits = "".join(str(_DIGIT_WORDS[word]) for word in words)
            return ("-" if negative else "") + digits
        # A lone "one"/"two" is too ambiguous to rewrite; a negative one
        # ("minus five") is unambiguous.
        if negative:
            return str(-_DIGIT_WORDS[words[0]])
        return None

    value = _parse_cardinal(words)
    if value is None:
        return None
    if len(words) == 1 and words[0] in _SCALE_WORDS:
        if negative:
            return str(-value)
        return None
    return ("-" if negative else "") + str(value)


def _convert_numbers(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        raw = match.group(0)
        words = [w.lower() for w in re.findall(r"[A-Za-z]+", raw)]
        result = _parse_number_phrase(words)
        return raw if result is None else result

    return _NUM_PHRASE_RE.sub(repl, text)


# ---------------------------------------------------------------------------
# Fillers
# ---------------------------------------------------------------------------
_AH_WORD_RE = re.compile(r"(?i)ah+")


def _is_ah_particle(text: str, start: int) -> bool:
    """True for a clause-final "ah", as in "like that also can ah?".

    Singapore and Malaysian English use it to soften a question or a
    statement, so it carries meaning and is not a hesitation sound.
    """
    match = _AH_WORD_RE.match(text, start)
    if not match:
        return False
    rest = text[match.end():].lstrip(" \t")
    return not rest or rest[0] in "?!."


_FILLER_RE = re.compile(
    r"(?i)(?<![\w-])(?:uh+|um+|ah+|erm|hmm+)"
    r"(?![\w-])(?![ \t]*,?[ \t]*(?:oh|ha)\b)"
    r"(?:[ \t]*,)?([ \t]*)([A-Za-z])?"
)


def _remove_fillers(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        if _is_ah_particle(text, match.start()):
            return match.group(0)
        before = text[:match.start()].rstrip()
        at_sentence_start = (
            not before or before[-1] in ".?!\n\"'“‘"
        )
        letter = match.group(2)
        if letter:
            if at_sentence_start:
                return letter.upper()
            return " " + letter
        return ""

    return _FILLER_RE.sub(repl, text)


# ---------------------------------------------------------------------------
# Spoken punctuation
# ---------------------------------------------------------------------------
_DETERMINERS = {
    "the", "a", "an", "this", "that", "these", "those", "my", "your",
    "his", "her", "its", "our", "their", "some", "any", "no", "every",
    "each", "either", "neither", "another",
}
_DOT_NOUN_PRECEDERS = _DETERMINERS | {
    # common adjectives used before the noun "dot"
    "red", "blue", "green", "black", "white", "yellow", "orange",
    "purple", "pink", "gray", "grey", "brown", "small", "big", "large",
    "tiny", "little", "round", "single", "double", "bright", "dark",
    "faint", "visible", "invisible", "central", "left", "right", "top",
    "bottom", "middle", "first", "last", "next", "previous", "same",
    "different", "solid", "hollow", "polka", "quantum",
    # words that introduce a mention of the symbol itself
    "word", "symbol", "character", "letter", "mark", "sign",
    "punctuation", "separator", "notation", "product", "pattern",
    "grid", "matrix", "circle", "key", "button",
}
_PUNCT_MENTION_PREV = {
    "word", "symbol", "character", "letter", "mark", "sign",
    "punctuation", "separator", "notation", "called", "name", "named",
    "say", "says", "said", "mean", "means", "meant", "meaning",
}
_PUNCT_MENTION_NEXT = {
    "is", "are", "was", "were", "means", "mean", "symbol", "character",
    "key", "button", "sign", "mark", "punctuation", "separator",
    "notation", "indicates", "separates",
}
_PUNCT_NOUN_PRECEDERS = _DETERMINERS | _PUNCT_MENTION_PREV
_TLDS = {
    "com", "org", "net", "edu", "gov", "mil", "int", "io", "ai", "co",
    "dev", "app", "info", "biz", "xyz", "uk", "de", "cn", "jp", "fr",
    "au", "ca", "eu", "ru", "br", "nl", "se", "ch", "kr", "tw", "hk",
    "sg", "nz", "za", "mx", "ar", "cl", "ie", "il", "tr", "ua", "vn",
    "id", "ph", "th",
}
_PUNCT_WORD_PATTERN = (
    r"(?:comma|period|colon|semicolon|question[ \t]+mark|"
    r"exclamation[ \t]+(?:mark|point)|full[ \t]+stop|dot)"
)


def _prev_word(text: str, position: int) -> str:
    match = re.search(
        r"([A-Za-z]+|\d+(?:[.,]\d+)*)[\s.,!?;:'\"()\[\]{}]*$",
        text[:position],
    )
    return match.group(1).lower() if match else ""


def _next_word(text: str, position: int) -> str:
    match = re.search(
        r"^[\s.,!?;:'\"()\[\]{}]*([A-Za-z]+|\d+(?:[.,]\d+)*)",
        text[position:],
    )
    return match.group(1).lower() if match else ""


def _is_number_token(token: str) -> bool:
    return bool(re.fullmatch(r"\d+(?:[.,]\d+)*", token)) or (
        token in _ALL_NUMBER_WORDS
    )


def _dot_at_boundary(text: str, end: int) -> bool:
    tail = text[end:].lstrip(" \t")
    return not tail or tail[0] in "\r\n.,!?;:"


def _punct_guard(text: str, start: int, end: int) -> bool:
    prev = _prev_word(text, start)
    nxt = _next_word(text, end)
    if not prev:
        return False
    if prev in _PUNCT_NOUN_PRECEDERS or prev in _PUNCT_MENTION_PREV:
        return False
    if nxt in _PUNCT_MENTION_NEXT:
        return False
    # Do not rewrite lists that talk about punctuation, e.g.
    # "comma and period" or "Please say comma and period".
    tail = text[:start].rstrip()
    if re.search(
        rf"(?i)\b{_PUNCT_WORD_PATTERN}[ \t]+(?:and|or)$", tail
    ):
        return False
    after = text[end:]
    if re.match(
        rf"(?i)^[\s.,!?;:]*\b(?:and|or)\b[ \t]+{_PUNCT_WORD_PATTERN}\b",
        after,
    ):
        return False
    return True


def _replace_command(
    text: str,
    pattern: str,
    symbol: str,
    *,
    join: bool = False,
    guard=None,
) -> str:
    regex = re.compile(pattern, re.I)
    out: list[str] = []
    last = 0
    for match in regex.finditer(text):
        if guard is not None and not guard(text, match.start(), match.end()):
            continue
        left = match.start()
        while left > last and text[left - 1] in " \t":
            left -= 1
        if symbol in ".,;:!?" and left > last and text[left - 1] == symbol:
            left -= 1
        right = match.end()
        while right < len(text) and text[right] in " \t":
            right += 1
        if symbol in ".,;:!?":
            if right < len(text) and text[right] == symbol:
                right += 1
            while right < len(text) and text[right] in " \t":
                right += 1
        out.append(text[last:left])
        out.append(symbol)
        if not join and right < len(text) and text[right] not in "\n.,!?;:)]}":
            out.append(" ")
        last = right
    out.append(text[last:])
    return "".join(out)


def _apply_dot(text: str) -> str:
    regex = re.compile(r"(?i)(?<![\w'-])dot(?![\w'-])")
    out: list[str] = []
    last = 0
    for match in regex.finditer(text):
        prev = _prev_word(text, match.start())
        nxt = _next_word(text, match.end())
        prev_number = _is_number_token(prev)
        next_number = _is_number_token(nxt)
        at_boundary = _dot_at_boundary(text, match.end())

        convert = False
        if prev_number and next_number:
            # This should normally have been handled by the number parser.
            convert = False
        elif prev_number:
            convert = True
        elif nxt in _TLDS and prev and prev not in _DOT_NOUN_PRECEDERS:
            convert = True
        elif (
            at_boundary
            and prev
            and prev not in _DOT_NOUN_PRECEDERS
            and prev not in _PUNCT_MENTION_PREV
        ):
            convert = True

        if not convert:
            continue

        left = match.start()
        while left > last and text[left - 1] in " \t":
            left -= 1
        right = match.end()
        while right < len(text) and text[right] in " \t":
            right += 1
        out.append(text[last:left])
        out.append(".")
        last = right
    out.append(text[last:])
    return "".join(out)


def _apply_spoken_punctuation(text: str) -> str:
    text = _replace_command(
        text, r"(?<![\w'-])new[ \t]+paragraph(?![\w'-])", "\n\n", join=True
    )
    text = _replace_command(
        text, r"(?<![\w'-])new[ \t]+line(?![\w'-])", "\n", join=True
    )
    text = _replace_command(
        text, r"(?<![\w'-])question[ \t]+mark(?![\w'-])", "?", guard=_punct_guard
    )
    text = _replace_command(
        text,
        r"(?<![\w'-])exclamation[ \t]+(?:mark|point)(?![\w'-])",
        "!",
        guard=_punct_guard,
    )
    text = _replace_command(
        text, r"(?<![\w'-])full[ \t]+stop(?![\w'-])", ".", guard=_punct_guard
    )
    text = _replace_command(
        text, r"(?<![\w'-])period(?![\w'-])", ".", guard=_punct_guard
    )
    text = _replace_command(
        text, r"(?<![\w'-])comma(?![\w'-])", ",", guard=_punct_guard
    )
    text = _replace_command(
        text, r"(?<![\w'-])semicolon(?![\w'-])", ";", guard=_punct_guard
    )
    text = _replace_command(
        text, r"(?<![\w'-])colon(?![\w'-])", ":", guard=_punct_guard
    )
    return _apply_dot(text)


# ---------------------------------------------------------------------------
# Final cleanup
# ---------------------------------------------------------------------------
def _cleanup_punctuation(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+([,.;:!?%\)\]\}])", r"\1", text)
    text = re.sub(r"([\(\[\{])[ \t]+", r"\1", text)
    text = re.sub(r",{2,}", ",", text)
    text = re.sub(r";{2,}", ";", text)
    text = re.sub(r":{2,}", ":", text)
    text = re.sub(r"!{2,}", "!", text)
    text = re.sub(r"\?{2,}", "?", text)
    text = re.sub(r"(?<!\.)\.\.(?!\.)", ".", text)
    text = re.sub(r"[,;:]\s*([.!?])", r"\1", text)
    text = re.sub(r"([.!?])\s*[,;:]", r"\1", text)
    text = re.sub(r"^[\s,.;:]+", "", text)
    text = re.sub(r"[\s,;:]+$", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return text.strip()


def postprocess_text(text: str) -> str:
    """Apply the conservative dictation cleanup pipeline."""
    if not text:
        return text
    text = _remove_fillers(text)
    text = _convert_numbers(text)
    text = _apply_spoken_punctuation(text)
    return _cleanup_punctuation(text)


if __name__ == "__main__":
    raise SystemExit(main())
