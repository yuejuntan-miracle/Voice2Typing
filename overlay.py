"""A small always-on-top panel for transcripts and clipboard history.

The panel is a borderless Tk window that never takes keyboard focus: it is
created with WS_EX_NOACTIVATE and WS_EX_TOOLWINDOW, so clicking it cannot move
focus away from the window you are dictating into. It lives in its own thread
because the main thread is busy pumping the low-level mouse hook.

It has three modes:

* transcript - collapsed it shows the state or latest transcript; clicking it
  expands a short history, and it hides itself when idle.
* clipboard  - left-clicking the tray icon lists the last few clipboard texts;
  clicking one copies it back.
* capture    - the shortcut recorder: shows the detected key or click, and
  cancels with Esc, a click, or right-click.
"""
from __future__ import annotations

import ctypes
import gc
import math
import queue
import threading
import time
import tkinter as tk

import platform_support

# Theme: cloudstream's HUD (dark card, one dim/bright text ramp, one accent)
# wearing this app's own identity, the two-lamp traffic light from the tray.
BG = "#101216"          # card
BG_ENTRY = "#151a21"    # raised row / latest transcript
BORDER = "#232b36"
TEXT = "#e2e8f0"        # a finished transcript
DIM_TEXT = "#79828f"    # waiting, interim, or a notice
MUTED = "#8b98a8"       # labels, timestamps
ACCENT = "#6ee7b7"      # the level meter
BAD = "#fca5a5"

# The tray icon's lamps, unchanged, so panel and tray are the same object.
RED = "#ec3e36"
GREEN = "#34cc60"
DARK_RED = "#4a1412"
DARK_GREEN = "#0f2e1a"
HOUSING = "#22252a"
RIM = "#0a0b0d"

FONT = platform_support.ui_font()

STATE_LABEL = {
    "loading": "loading model",
    "idle": "idle",
    "recording": "recording",
    "transcribing": "transcribing",
    "paused": "paused",
    "error": "error",
}

STATE_LAMPS = {
    "loading": "red",
    "idle": "red",
    "recording": "green",
    "transcribing": "green",
    "paused": None,
    "error": "red",
}


def _caps(text: str) -> str:
    """Letterspaced capitals, the HUD's header style (a thin space, not a full one)."""
    return "\u2009".join(str(text or "").upper())


def format_elapsed(seconds: float) -> str:
    """0:03 / 1:07 style clock for the recording indicator."""
    total = max(0, int(seconds))
    return f"{total // 60}:{total % 60:02d}"


def _one_line(text: str, limit: int = 58) -> str:
    """Collapse a transcript to a single readable line for the header."""
    flat = " ".join(str(text).split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def _clamped(text: str, limit: int = 160) -> str:
    """Collapse to one paragraph, cut to about three lines in the card."""
    return _one_line(text, limit)


def clipboard_preview(text: str, limit: int = 150) -> str:
    """One short, whitespace-flattened preview line for a clipboard item."""
    return _one_line(text, limit)


def work_area() -> tuple[int, int, int, int]:
    """Desktop area excluding the taskbar / menu bar, in pixels."""
    return platform_support.screen_work_area(None)


class TranscriptOverlay:
    WIDTH = 390
    MAX_ENTRY_CHARS = 280

    def __init__(self, settings, log=None):
        self.settings = settings
        self.log = log
        self.max_entries = int(getattr(settings, "overlay_max_entries", 12))
        self.hide_after = float(getattr(settings, "overlay_hide_after", 8.0))

        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._root: tk.Tk | None = None

        # Everything below is touched only from the Tk thread.
        self._entries: list[tuple[str, str]] = []
        self._state = "loading"
        self._started_at = 0.0
        self._expanded = False
        self._hide_job = None
        self._timer_job = None
        self._visible = False
        self._container = None
        self._state_label = None
        self._timer_label = None
        self._body_label = None
        self._meter = None
        self._meter_fill = None
        self._level = 0.0
        self._hwnd = 0

        # "transcript", "clipboard" (left-click tray popup) or "capture"
        # (the shortcut recorder).
        self._mode = "transcript"
        self._clipboard: list[str] = []
        self._flash = ""
        self._notice = ""

        # Shortcut recorder view.
        self._capture_preview = ""
        self._capture_done = False
        self._capture_ok = True
        self.on_capture_cancel = None

        # Focus guard: remember the last real foreground window and never let
        # the panel keep it.
        self._guard_stop = threading.Event()
        self._last_foreground = 0

    # -- public API (any thread) -----------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="overlay", daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout=5.0)
        if platform_support.IS_WINDOWS:
            self._guard_stop.clear()
            threading.Thread(
                target=self._focus_guard, name="overlay-focus", daemon=True
            ).start()

    def stop(self) -> None:
        self._guard_stop.set()
        self._queue.put(("stop", None, None))
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def set_state(self, state: str, started_at: float | None = None) -> None:
        self._queue.put(("state", state, started_at))

    def add_entry(self, text: str) -> None:
        self._queue.put(("entry", text, None))

    def set_level(self, rms: float) -> None:
        """Microphone RMS (0..1) from the audio callback, for the level meter."""
        self._queue.put(("level", float(rms or 0.0), None))

    def notify(self, text: str, seconds: float = 5.0) -> None:
        """Show one line for a few seconds without adding it to the history."""
        self._queue.put(("notice", str(text or ""), float(seconds)))

    def clear_history(self) -> None:
        self._queue.put(("clear", None, None))

    def toggle(self) -> None:
        self._queue.put(("toggle", None, None))

    def show_clipboard(self, items: list[str]) -> None:
        """Open the clipboard list (left-click on the tray icon)."""
        self._queue.put(("clipboard", list(items), None))

    @property
    def clipboard_open(self) -> bool:
        return self._mode == "clipboard" and self._visible

    def show_capture(self, preview: str = "Waiting for a key, combination or click…") -> None:
        """Open the shortcut recorder view (tray → Set shortcut)."""
        self._queue.put(("capture", preview, None))

    def update_capture(self, preview: str) -> None:
        self._queue.put(("capture-update", preview, None))

    def finish_capture(self, text: str, ok: bool = True) -> None:
        self._queue.put(("capture-done", text, ok))

    def hide_capture(self) -> None:
        self._queue.put(("capture-close", None, None))

    @property
    def capture_open(self) -> bool:
        return self._mode == "capture" and self._visible

    # -- Tk thread --------------------------------------------------------
    def _run(self) -> None:
        try:
            root = tk.Tk()
        except Exception as exc:  # no Tk at all (rare)
            if self.log:
                self.log.warning("transcript overlay unavailable: %s", exc)
            self._ready.set()
            return

        self._root = root
        root.withdraw()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=BORDER)

        root.update_idletasks()
        self._make_non_activating(root)
        self._build()
        root.update_idletasks()
        self._place(hidden=True)
        # Map it once, off-screen: showing it later is just a move, which
        # never activates the window.
        root.deiconify()
        self._ready.set()

        self._pump()
        try:
            root.mainloop()
        except Exception:
            pass
        finally:
            # The Tcl interpreter has to be released on the thread that made
            # it, otherwise Tkinter prints "async handler deleted by the wrong
            # thread" as the process exits.
            self._container = None
            self._state_label = None
            self._timer_label = None
            self._body_label = None
            self._meter = None
            self._meter_fill = None
            self._root = None
            try:
                root.destroy()
            except Exception:
                pass
            try:
                if tk._default_root is root:
                    tk._default_root = None
            except Exception:
                pass
            del root
            gc.collect()

    def _make_non_activating(self, root: tk.Tk) -> None:
        try:
            self._hwnd = platform_support.prepare_panel(root)
        except Exception as exc:
            if self.log:
                self.log.debug("could not mark the overlay non-activating: %s", exc)

    def _pump(self) -> None:
        try:
            while True:
                kind, value, extra = self._queue.get_nowait()
                if kind == "stop":
                    self._shutdown()
                    return
                self._handle(kind, value, extra)
        except queue.Empty:
            pass
        except Exception as exc:
            if self.log:
                self.log.warning("overlay update failed: %s", exc)

        if self._root is not None:
            self._root.after(40, self._pump)

    def _handle(self, kind: str, value, extra) -> None:
        if kind == "level":
            self._set_level(float(value or 0.0))
        elif kind == "state":
            self._mode = "transcript"
            self._flash = ""
            if (value or "idle") != "recording":
                # The meter only means anything while the mic is live.
                self._level = 0.0
            self._state = value or "idle"
            self._started_at = extra or time.time()
            if self._state == "recording":
                # A new recording makes any "heard nothing" line stale.
                self._notice = ""
            if self._state == "idle":
                self._stop_timer()
                self._visible = True
                self._build()
                self._place()
                self._schedule_hide()
            elif self._state == "paused":
                self._stop_timer()
                self._build()
                self._place()
                self._schedule_hide()
            else:
                self._cancel_hide()
                self._visible = True
                self._build()
                self._place()
                if self._state == "recording":
                    self._start_timer()
                else:
                    self._stop_timer()
        elif kind == "entry":
            if value:
                self._mode = "transcript"
                stamp = time.strftime("%H:%M")
                self._entries.insert(0, (stamp, value))
                del self._entries[self.max_entries:]
                self._visible = True
                self._build()
                self._place()
                self._schedule_hide()
        elif kind == "notice":
            self._mode = "transcript"
            self._level = 0.0
            self._stop_timer()
            self._cancel_hide()
            self._notice = str(value or "")
            self._visible = True
            self._build()
            self._place()
            self._schedule_hide(delay=float(extra or 5.0))
        elif kind == "clear":
            self._entries.clear()
            self._build()
            self._place()
        elif kind == "toggle":
            self._expanded = not self._expanded
            self._cancel_hide()
            self._visible = True
            self._build()
            self._place()
            if not self._expanded:
                self._schedule_hide()
        elif kind == "clipboard":
            self._mode = "clipboard"
            self._flash = ""
            self._clipboard = [str(item) for item in (value or []) if str(item).strip()]
            self._stop_timer()
            self._cancel_hide()
            self._visible = True
            self._build()
            self._place()
            self._schedule_hide(delay=max(15.0, self.hide_after))
        elif kind == "capture":
            self._mode = "capture"
            self._capture_done = False
            self._capture_ok = True
            self._capture_preview = str(value or "Waiting…")
            self._stop_timer()
            self._cancel_hide()
            self._visible = True
            self._build()
            self._place()
        elif kind == "capture-update":
            if self._mode != "capture" or self._capture_done:
                return
            self._capture_preview = str(value or "Waiting…")
            self._build()
            self._place()
        elif kind == "capture-done":
            self._mode = "capture"
            self._capture_done = True
            self._capture_ok = bool(extra) if extra is not None else True
            self._capture_preview = str(value or "")
            self._build()
            self._place()
            self._schedule_hide(delay=2.5)
        elif kind == "capture-close":
            if self._mode == "capture":
                self._cancel_hide()
                self._hide()

    # -- rendering --------------------------------------------------------
    def _build(self) -> None:
        root = self._root
        if root is None:
            return

        if self._container is not None:
            self._container.destroy()
        self._state_label = None
        self._timer_label = None
        self._body_label = None
        self._meter = None
        self._meter_fill = None
        self._container = tk.Frame(root, bg=BG)
        self._container.pack(fill="both", expand=True, padx=1, pady=1)

        if self._mode == "clipboard":
            self._build_clipboard()
        elif self._mode == "capture":
            self._build_capture()
        else:
            self._build_header()
            if self._expanded:
                self._build_entries()

        # Clicking anywhere toggles; the window never takes focus.
        for widget in (self._container,):
            widget.bind("<Button-1>", self._on_click)
            widget.bind("<Button-3>", self._on_right_click)

    def _build_header(self) -> None:
        head = tk.Frame(self._container, bg=BG, padx=14)
        head.pack(fill="x", pady=(11, 0))
        head.bind("<Button-1>", self._on_click)
        head.bind("<Button-3>", self._on_right_click)

        lamps = tk.Canvas(
            head, width=13, height=26, bg=BG, bd=0,
            highlightthickness=0, takefocus=0,
        )
        lamps.pack(side="left")
        self._draw_lamps(lamps)
        lamps.bind("<Button-1>", self._on_click)
        lamps.bind("<Button-3>", self._on_right_click)

        label = tk.Label(
            head, text=_caps(STATE_LABEL.get(self._state, self._state)),
            bg=BG, fg=MUTED, font=(FONT, 8), anchor="w",
        )
        label.pack(side="left", padx=(8, 0))
        label.bind("<Button-1>", self._on_click)
        self._state_label = label

        chevron = tk.Label(
            head, text="⌃" if self._expanded else "⌄", bg=BG, fg=MUTED,
            font=(FONT, 9),
        )
        chevron.pack(side="right")
        chevron.bind("<Button-1>", self._on_click)

        timer = tk.Label(
            head, text=self._timer_text(), bg=BG, fg=MUTED,
            font=(FONT, 8), anchor="e",
        )
        timer.pack(side="right", padx=(0, 8))
        timer.bind("<Button-1>", self._on_click)
        self._timer_label = timer

        body = tk.Label(
            self._container, text=self._status_text(), bg=BG,
            fg=self._body_color(), font=(FONT, 12), anchor="w",
            justify="left", wraplength=self.WIDTH - 28,
        )
        body.pack(fill="x", padx=14, pady=(7, 0))
        body.bind("<Button-1>", self._on_click)
        body.bind("<Button-3>", self._on_right_click)
        self._body_label = body

        meter = tk.Frame(self._container, bg=BG, padx=14)
        meter.pack(fill="x", pady=(9, 11))
        track = tk.Canvas(
            meter, height=3, bg="#1b2027", bd=0,
            highlightthickness=0, takefocus=0,
        )
        track.pack(fill="x")
        fill = track.create_rectangle(0, 0, 0, 3, fill=ACCENT, width=0)
        track.bind("<Configure>", self._draw_level)
        track.bind("<Button-1>", self._on_click)
        track.bind("<Button-3>", self._on_right_click)
        self._meter = track
        self._meter_fill = fill

    def _draw_lamps(self, canvas: tk.Canvas) -> None:
        """The tray icon's two-lamp traffic light, drawn at panel scale."""
        active = STATE_LAMPS.get(self._state, "red")
        canvas.create_rectangle(0, 0, 13, 26, fill=HOUSING, outline=RIM)
        for center_y, name, lit, dark in (
            (7.5, "red", RED, DARK_RED),
            (18.5, "green", GREEN, DARK_GREEN),
        ):
            canvas.create_oval(
                3, center_y - 4, 11, center_y + 4,
                fill=lit if active == name else dark, outline=RIM,
            )

    def _timer_text(self) -> str:
        if self._state != "recording":
            return ""
        return format_elapsed(time.time() - self._started_at)

    def _draw_level(self, _event=None) -> None:
        """Paint the meter from the smoothed microphone level."""
        if self._meter is None or self._meter_fill is None:
            return
        try:
            width = max(1, self._meter.winfo_width())
            self._meter.coords(
                self._meter_fill, 0, 0, int(width * self._level), 3
            )
        except Exception:
            pass

    def _set_level(self, rms: float) -> None:
        """RMS -> dBFS, shown on a -55..-12 dB scale, with a little smoothing.

        Only the app sends this, and only while it is recording, so the panel
        does not gate on its own state: it may still read "loading model"
        while the microphone is already live.
        """
        db = 20.0 * math.log10(max(float(rms), 1e-6))
        level = min(1.0, max(0.0, (db + 55.0) / 43.0))
        self._level = self._level * 0.55 + level * 0.45
        self._draw_level()

    def _build_entries(self) -> None:
        tk.Frame(self._container, bg=BORDER, height=1).pack(fill="x", padx=8)

        if not self._entries:
            empty = tk.Label(
                self._container, text="No transcripts yet",
                bg=BG, fg=MUTED, font=(FONT, 9), anchor="w",
            )
            empty.pack(fill="x", padx=12, pady=10)
            empty.bind("<Button-1>", self._on_click)
        else:
            for index, (stamp, text) in enumerate(self._entries):
                self._build_entry(index, stamp, text)

        hint = tk.Label(
            self._container,
            text="Click to collapse  ·  right-click copies the history",
            bg=BG, fg=MUTED, font=(FONT, 8), anchor="w",
        )
        hint.pack(fill="x", padx=12, pady=(4, 6))
        hint.bind("<Button-1>", self._on_click)

    def _build_entry(self, index: int, stamp: str, text: str) -> None:
        row = tk.Frame(
            self._container,
            bg=BG_ENTRY if index == 0 else BG,
            padx=10, pady=5,
        )
        row.pack(fill="x", padx=6, pady=(2, 0))

        meta = tk.Label(
            row, text=stamp, bg=row["bg"], fg=MUTED,
            font=(FONT, 8), anchor="nw",
        )
        meta.pack(side="left", anchor="n")

        snippet = text if len(text) <= self.MAX_ENTRY_CHARS else text[: self.MAX_ENTRY_CHARS - 1] + "…"
        body = tk.Label(
            row, text=snippet, bg=row["bg"], fg=TEXT,
            font=(FONT, 9), anchor="w", justify="left",
            wraplength=self.WIDTH - 70,
        )
        body.pack(side="left", padx=(8, 0), fill="x", expand=True)

        for widget in (row, meta, body):
            widget.bind("<Button-1>", self._on_click)

    def _build_capture(self) -> None:
        row = tk.Frame(self._container, bg=BG, padx=10, pady=8)
        row.pack(fill="x")
        row.bind("<Button-1>", self._on_click)
        row.bind("<Button-3>", self._on_right_click)

        dot = tk.Label(
            row, text="●", bg=BG,
            fg=GREEN if self._capture_ok else RED,
            font=(FONT, 8),
        )
        dot.pack(side="left")
        dot.bind("<Button-1>", self._on_click)

        title = tk.Label(
            row,
            text=_caps(
                "shortcut set" if self._capture_done and self._capture_ok
                else "shortcut not usable" if self._capture_done
                else "set shortcut"
            ),
            bg=BG, fg=MUTED, font=(FONT, 8), anchor="w",
        )
        title.pack(side="left", padx=(8, 6), fill="x", expand=True)
        title.bind("<Button-1>", self._on_click)

        close = tk.Label(row, text="✕", bg=BG, fg=MUTED, font=(FONT, 9))
        close.pack(side="right")
        close.bind("<Button-1>", self._on_close)
        close.bind("<Enter>", lambda _e: close.configure(fg=TEXT))
        close.bind("<Leave>", lambda _e: close.configure(fg=MUTED))

        tk.Frame(self._container, bg=BORDER, height=1).pack(fill="x", padx=8)

        preview = tk.Label(
            self._container,
            text=self._capture_preview or "Waiting…",
            bg=BG, fg=GREEN if (self._capture_done and self._capture_ok) else
            RED if self._capture_done else TEXT,
            font=(FONT, 13, "bold"), anchor="w", justify="left",
            wraplength=self.WIDTH - 44,
        )
        preview.pack(fill="x", padx=14, pady=(12, 4))
        preview.bind("<Button-1>", self._on_click)
        preview.bind("<Button-3>", self._on_right_click)

        hint = tk.Label(
            self._container,
            text=(
                "This is the new trigger"
                if self._capture_done and self._capture_ok
                else "Pick something else from the tray menu"
                if self._capture_done
                else "Hold modifiers, then press a key or mouse button  ·  Esc to cancel"
            ),
            bg=BG, fg=MUTED, font=(FONT, 8), anchor="w", justify="left",
            wraplength=self.WIDTH - 44,
        )
        hint.pack(fill="x", padx=14, pady=(0, 12))
        hint.bind("<Button-1>", self._on_click)
        hint.bind("<Button-3>", self._on_right_click)

    def _build_clipboard(self) -> None:
        row = tk.Frame(self._container, bg=BG, padx=10, pady=8)
        row.pack(fill="x")
        row.bind("<Button-1>", self._on_click)
        row.bind("<Button-3>", self._on_right_click)

        if self._flash:
            title, color = self._flash, ACCENT
        else:
            count = len(self._clipboard)
            title = "Clipboard history" + (f" · {count}" if count else "")
            color = MUTED

        label = tk.Label(
            row, text=_caps(title), bg=BG, fg=color,
            font=(FONT, 8), anchor="w", justify="left",
        )
        label.pack(side="left", padx=(0, 6), fill="x", expand=True)
        label.bind("<Button-1>", self._on_click)

        close = tk.Label(row, text="✕", bg=BG, fg=MUTED, font=(FONT, 9))
        close.pack(side="right")
        close.bind("<Button-1>", self._on_close)
        close.bind("<Enter>", lambda _e: close.configure(fg=TEXT))
        close.bind("<Leave>", lambda _e: close.configure(fg=MUTED))

        tk.Frame(self._container, bg=BORDER, height=1).pack(fill="x", padx=8)

        if not self._clipboard:
            empty = tk.Label(
                self._container, text="Nothing copied yet",
                bg=BG, fg=MUTED, font=(FONT, 9), anchor="w",
            )
            empty.pack(fill="x", padx=12, pady=10)
            empty.bind("<Button-1>", self._on_click)
        else:
            for index, text in enumerate(self._clipboard):
                self._build_clipboard_item(index, text)

        hint = tk.Label(
            self._container,
            text="Click an item to copy  ·  right-click to close",
            bg=BG, fg=MUTED, font=(FONT, 8), anchor="w",
        )
        hint.pack(fill="x", padx=12, pady=(4, 6))
        hint.bind("<Button-1>", self._on_click)

    def _build_clipboard_item(self, index: int, text: str) -> None:
        row = tk.Frame(self._container, bg=BG, padx=10, pady=5)
        row.pack(fill="x", padx=6, pady=(2, 0))

        number = tk.Label(
            row, text=str(index + 1), bg=BG, fg=MUTED,
            font=(FONT, 8), anchor="nw", width=2,
        )
        number.pack(side="left", anchor="n")

        body = tk.Label(
            row, text=clipboard_preview(text), bg=BG, fg=TEXT,
            font=(FONT, 9), anchor="w", justify="left",
            wraplength=self.WIDTH - 80,
        )
        body.pack(side="left", padx=(4, 0), fill="x", expand=True)

        widgets = (row, number, body)
        for widget in widgets:
            widget.bind("<Button-1>", lambda _event, value=text: self._copy_clipboard_item(value))
            widget.bind("<Button-3>", self._on_right_click)
        self._bind_hover(widgets)

    def _bind_hover(self, widgets) -> None:
        def enter(_event=None):
            for widget in widgets:
                try:
                    widget.configure(bg=BG_ENTRY)
                except Exception:
                    pass

        def leave(_event=None):
            for widget in widgets:
                try:
                    widget.configure(bg=BG)
                except Exception:
                    pass

        for widget in widgets:
            widget.bind("<Enter>", enter)
            widget.bind("<Leave>", leave)

    def _copy_clipboard_item(self, text: str):
        if not platform_support.set_clipboard(text):
            return "break"
        self._flash = "Copied to clipboard"
        self._build()
        self._place()
        self._schedule_hide(delay=1.5)
        return "break"

    def _status_text(self) -> str:
        """The one line under the header: the transcript, or what is happening."""
        if self._state == "recording":
            return "Listening…"
        if self._state == "transcribing":
            return "Transcribing…"
        if self._state == "loading":
            return "Loading the speech model…"
        if self._state == "paused":
            return "Dictation paused"
        if self._state == "error":
            return "Model error — open the log"
        if self._notice:
            return self._notice
        if self._expanded and self._entries:
            count = len(self._entries)
            return f"Transcript history · {count}" if count > 1 else "Transcript history"
        if self._entries:
            return _clamped(self._entries[0][1])
        return "Ready — hold your shortcut to dictate"

    def _body_color(self) -> str:
        """Dim while nothing has been said yet, bright once it is final."""
        if self._state == "error":
            return BAD
        if self._state in ("idle", "loading") and not self._expanded \
                and not self._notice and self._entries:
            return TEXT
        return DIM_TEXT

    def _place(self, hidden: bool = False) -> None:
        root = self._root
        if root is None:
            return
        before = 0
        if platform_support.IS_WINDOWS:
            before = ctypes.windll.user32.GetForegroundWindow()
        root.update_idletasks()
        height = max(root.winfo_reqheight(), 34)
        width = self.WIDTH

        if hidden:
            root.geometry(f"{width}x{height}+-10000+-10000")
            self._guard_focus(before)
            return

        left, top, right, bottom = work_area()
        x = max(left + 8, right - width - 14)
        y = max(top + 8, bottom - height - 14)
        root.geometry(f"{width}x{height}+{x}+{y}")
        self._guard_focus(before)

    def _guard_focus(self, before: int) -> None:
        """Never keep focus.

        Showing or moving the panel must not make it the active window: the
        paste would land nowhere. If Windows still handed it focus, give it
        straight back to whatever was active a moment ago.
        """
        if not self._hwnd:
            return
        if not platform_support.IS_WINDOWS:
            return
        try:
            user32 = ctypes.windll.user32
            if user32.GetForegroundWindow() == self._hwnd and before not in (0, self._hwnd):
                user32.SetForegroundWindow(before)
        except Exception:
            pass

    def _focus_guard(self) -> None:
        """Bounce focus back if the panel ever becomes the active window.

        Showing a topmost window can occasionally activate it (notably right
        after a tray click). This runs on its own thread so the panel can never
        sit in front as the paste target.
        """
        if not platform_support.IS_WINDOWS:
            return
        user32 = ctypes.windll.user32
        while not self._guard_stop.is_set():
            time.sleep(0.15)
            try:
                now = user32.GetForegroundWindow()
                if not now:
                    continue
                if now == self._hwnd:
                    previous = self._last_foreground
                    if previous and previous != self._hwnd and user32.IsWindow(previous):
                        user32.SetForegroundWindow(previous)
                else:
                    self._last_foreground = now
            except Exception:
                continue

    # -- timers -----------------------------------------------------------
    def _start_timer(self) -> None:
        self._stop_timer()

        def tick():
            if self._state != "recording" or self._root is None:
                return
            try:
                if self._timer_label is not None:
                    self._timer_label.configure(text=self._timer_text())
                # Let the meter fall back when the microphone goes quiet.
                self._level *= 0.7
                self._draw_level()
            except Exception:
                pass
            self._timer_job = self._root.after(250, tick)

        tick()

    def _stop_timer(self) -> None:
        if self._timer_job is not None and self._root is not None:
            try:
                self._root.after_cancel(self._timer_job)
            except Exception:
                pass
        self._timer_job = None

    def _schedule_hide(self, delay: float | None = None) -> None:
        self._cancel_hide()
        if self._mode == "capture" and not self._capture_done:
            return
        if self._mode == "transcript" and self._expanded:
            return
        if self._root is None:
            return
        seconds = max(1.0, self.hide_after if delay is None else delay)
        self._hide_job = self._root.after(int(seconds * 1000), self._hide)

    def _cancel_hide(self) -> None:
        if self._hide_job is not None and self._root is not None:
            try:
                self._root.after_cancel(self._hide_job)
            except Exception:
                pass
        self._hide_job = None

    def _hide(self) -> None:
        self._hide_job = None
        self._visible = False
        self._notice = ""
        self._place(hidden=True)

    # -- events -----------------------------------------------------------
    def _on_click(self, _event=None):
        if self._mode == "capture":
            if self._capture_done:
                self._cancel_hide()
                self._hide()
            else:
                self._cancel_capture()
            return "break"
        if self._mode == "clipboard":
            self._cancel_hide()
            self._hide()
            return "break"
        self._expanded = not self._expanded
        self._cancel_hide()
        self._visible = True
        self._build()
        self._place()
        if not self._expanded:
            self._schedule_hide()
        return "break"

    def _on_close(self, _event=None):
        if self._mode == "capture":
            if self._capture_done:
                self._cancel_hide()
                self._hide()
            else:
                self._cancel_capture()
            return "break"
        self._cancel_hide()
        self._hide()
        return "break"

    def _cancel_capture(self) -> None:
        """Tell the app to stop the recorder, then close this view."""
        callback = self.on_capture_cancel
        self._cancel_hide()
        self._hide()
        if callable(callback):
            try:
                callback()
            except Exception:
                pass

    def _on_right_click(self, _event=None):
        if self._mode == "capture":
            if self._capture_done:
                self._cancel_hide()
                self._hide()
            else:
                self._cancel_capture()
            return "break"
        if self._mode == "clipboard":
            self._cancel_hide()
            self._hide()
            return "break"
        try:
            text = "\n".join(
                f"[{stamp}] {body}" for stamp, body in reversed(self._entries)
            )
            if text:
                platform_support.set_clipboard(text)
        except Exception:
            pass
        return "break"

    def _shutdown(self) -> None:
        self._stop_timer()
        self._cancel_hide()
        if self._root is not None:
            try:
                self._root.destroy()
            except Exception:
                pass
            self._root = None
