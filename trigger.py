"""Listen for the configured shortcut and start/stop recording.

Two backends:

* Windows - low-level mouse and keyboard hooks (WH_MOUSE_LL / WH_KEYBOARD_LL)
  so the shortcut can be swallowed before the focused app sees it. Mouse
  suppression also stops the side button from navigating the browser.
* macOS / Linux - pynput listeners. They see every key and click, but cannot
  reliably suppress an event, so the chosen combination should not already be
  bound in the app you are typing into.

The backend only translates native events into shortcut tokens and calls
`on_start` / `on_stop`; recording logic stays in dictate.py.
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time
from typing import Callable

from pynput import keyboard, mouse

import shortcuts

# --------------------------------------------------------------- Windows ----
if sys.platform == "win32":
    from ctypes import wintypes

    WH_KEYBOARD_LL = 13
    WH_MOUSE_LL = 14
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    WM_SYSKEYDOWN = 0x0104
    WM_SYSKEYUP = 0x0105
    WM_MBUTTONDOWN = 0x0207
    WM_MBUTTONUP = 0x0208
    WM_XBUTTONDOWN = 0x020B
    WM_XBUTTONUP = 0x020C
    WM_QUIT = 0x0012
    XBUTTON1 = 0x0001
    XBUTTON2 = 0x0002

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = (
            ("vkCode", wintypes.DWORD),
            ("scanCode", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_void_p),
        )

    class MSLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = (
            ("pt", wintypes.POINT),
            ("mouseData", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_void_p),
        )

    HOOKPROC = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
    )


class _BaseTrigger:
    def __init__(
        self,
        get_shortcut: Callable[[], shortcuts.Shortcut],
        on_start: Callable[[], None],
        on_stop: Callable[[], None],
        active: Callable[[], bool],
        log=None,
    ):
        self.get_shortcut = get_shortcut
        self.on_start = on_start
        self.on_stop = on_stop
        self.active = active
        self.log = log

    def _log(self, level: str, message: str, *args) -> None:
        if self.log:
            getattr(self.log, level)(message, *args)

    def reset(self) -> None:
        """Forget held keys, e.g. after a pause or while recording a shortcut."""

    def stop(self) -> None:
        """Ask run() to return."""


# ---------------------------------------------------------------- Windows ----
class WindowsTrigger(_BaseTrigger):
    def __init__(self, *args, suppress_mouse: Callable[[], bool] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.suppress_mouse = suppress_mouse or (lambda: True)
        self._pressed: set[str] = set()
        self._fired_keys: set[str] = set()
        self._fired_button = ""
        self._thread_id = 0
        self._hook_mouse = None
        self._hook_keys = None
        self._stop = False
        self._portable: PortableTrigger | None = None

    # -- state ------------------------------------------------------------
    def reset(self) -> None:
        self._pressed.clear()
        self._fired_keys.clear()
        self._fired_button = ""

    def stop(self) -> None:
        self._stop = True
        if self._thread_id and sys.platform == "win32":
            try:
                ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            except Exception:
                pass
        if self._portable is not None:
            self._portable.stop()

    # -- hooks ------------------------------------------------------------
    def run(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        user32.SetWindowsHookExW.argtypes = (
            ctypes.c_int,
            HOOKPROC,
            wintypes.HINSTANCE,
            wintypes.DWORD,
        )
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.CallNextHookEx.argtypes = (
            wintypes.HHOOK,
            ctypes.c_int,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.UnhookWindowsHookEx.argtypes = (wintypes.HHOOK,)
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL

        self._thread_id = kernel32.GetCurrentThreadId()

        def key_proc(n_code, w_param, l_param):
            try:
                if n_code >= 0:
                    info = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    token = shortcuts.vk_to_token(int(info.vkCode))
                    if token:
                        if w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                            if token in self._pressed and token in self._fired_keys:
                                return 1  # swallow auto-repeat of our main key
                            self._pressed.add(token)
                            if self._try_fire_keyboard(token):
                                return 1
                        elif w_param in (WM_KEYUP, WM_SYSKEYUP):
                            self._pressed.discard(token)
                            if token in self._fired_keys:
                                self._fired_keys.discard(token)
                                if not self._fired_keys:
                                    threading.Thread(target=self.on_stop, daemon=True).start()
                                if token not in shortcuts.MODIFIERS:
                                    return 1  # swallow the release too
                return user32.CallNextHookEx(self._hook_keys, n_code, w_param, l_param)
            except Exception:
                # Never let a Python error swallow the user's input.
                return user32.CallNextHookEx(self._hook_keys, n_code, w_param, l_param)

        def mouse_proc(n_code, w_param, l_param):
            try:
                if n_code >= 0:
                    info = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                    button = ""
                    if w_param in (WM_XBUTTONDOWN, WM_XBUTTONUP):
                        code = (info.mouseData >> 16) & 0xFFFF
                        button = "x1" if code == XBUTTON1 else "x2" if code == XBUTTON2 else ""
                    elif w_param in (WM_MBUTTONDOWN, WM_MBUTTONUP):
                        button = "middle"

                    if button:
                        shortcut = self.get_shortcut()
                        is_ours = shortcut.kind == "mouse" and shortcut.button == button
                        if w_param in (WM_XBUTTONDOWN, WM_MBUTTONDOWN):
                            if is_ours and self.active() and not self._fired_button:
                                self._fired_button = button
                                threading.Thread(target=self.on_start, daemon=True).start()
                                if self.suppress_mouse():
                                    return 1
                        elif w_param in (WM_XBUTTONUP, WM_MBUTTONUP):
                            if self._fired_button == button:
                                self._fired_button = ""
                                threading.Thread(target=self.on_stop, daemon=True).start()
                                if self.suppress_mouse():
                                    return 1
                return user32.CallNextHookEx(self._hook_mouse, n_code, w_param, l_param)
            except Exception:
                return user32.CallNextHookEx(self._hook_mouse, n_code, w_param, l_param)

        self._key_proc = key_proc
        self._mouse_proc = mouse_proc
        # Keep the ctypes trampolines alive: without a reference the callback
        # is freed while Windows still holds its address.
        self._cb_keys = HOOKPROC(key_proc)
        self._cb_mouse = HOOKPROC(mouse_proc)
        module = kernel32.GetModuleHandleW(None)
        self._hook_keys = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._cb_keys, module, 0
        )
        self._hook_mouse = user32.SetWindowsHookExW(
            WH_MOUSE_LL, self._cb_mouse, module, 0
        )

        if not self._hook_keys or not self._hook_mouse:
            error = ctypes.get_last_error()
            self._log("warning", "low-level hooks failed (WinError %s); using pynput", error)
            self._log("warning", "shortcut suppression is NOT active in fallback mode")
            if self._hook_keys:
                user32.UnhookWindowsHookEx(self._hook_keys)
                self._hook_keys = None
            if self._hook_mouse:
                user32.UnhookWindowsHookEx(self._hook_mouse)
                self._hook_mouse = None
            self._portable = PortableTrigger(
                self.get_shortcut, self.on_start, self.on_stop, self.active, self.log
            )
            self._portable.run()
            return

        self._log(
            "info",
            "low-level hooks installed; shortcut is %s",
            self.get_shortcut().display(),
        )
        msg = wintypes.MSG()
        try:
            while not self._stop and user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            for name in ("_hook_keys", "_hook_mouse"):
                handle = getattr(self, name)
                if handle:
                    user32.UnhookWindowsHookEx(handle)
                    setattr(self, name, None)

    def _try_fire_keyboard(self, token: str) -> bool:
        if not self.active():
            return False
        shortcut = self.get_shortcut()
        if not shortcut.matches(self._pressed):
            return False
        if token in shortcuts.MODIFIERS:
            return False  # never swallow a bare modifier press
        self._fired_keys = set(shortcut.keys)
        threading.Thread(target=self.on_start, daemon=True).start()
        return True


# ------------------------------------------------------------ mac / linux ----
_PYNPUT_MODIFIERS = {
    "ctrl": "ctrl",
    "ctrl_l": "ctrl",
    "ctrl_r": "ctrl",
    "alt": "alt",
    "alt_l": "alt",
    "alt_r": "alt",
    "alt_gr": "alt",
    "shift": "shift",
    "shift_l": "shift",
    "shift_r": "shift",
    "cmd": "cmd",
    "cmd_l": "cmd",
    "cmd_r": "cmd",
    "super": "cmd",
    "super_l": "cmd",
    "super_r": "cmd",
    "meta": "cmd",
}

_PYNPUT_NAMES = {
    "return": "enter",
    "escape": "esc",
    "page_up": "pageup",
    "page_down": "pagedown",
    "caps_lock": "capslock",
    "scroll_lock": "scrolllock",
    "print_screen": "printscreen",
}


def pynput_token(key) -> str | None:
    """Translate a pynput key into a shortcut token."""
    if isinstance(key, keyboard.KeyCode):
        char = key.char
        if char and len(char) == 1 and char.isalnum():
            return char.lower()
        if getattr(key, "vk", None) is not None:
            return shortcuts.vk_to_token(int(key.vk))
        return None

    name = getattr(key, "name", "")
    if name in _PYNPUT_MODIFIERS:
        return _PYNPUT_MODIFIERS[name]
    if name in _PYNPUT_NAMES:
        return _PYNPUT_NAMES[name]
    if name in shortcuts.NAMED_KEYS:
        return name
    if name.startswith("f") and name[1:].isdigit():
        return name
    return None


class PortableTrigger(_BaseTrigger):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pressed: set[str] = set()
        self._fired_keys: set[str] = set()
        self._fired_button = ""
        self._stop = threading.Event()
        self._keyboard_listener: keyboard.Listener | None = None
        self._mouse_listener: mouse.Listener | None = None

    def reset(self) -> None:
        self._pressed.clear()
        self._fired_keys.clear()
        self._fired_button = ""

    def stop(self) -> None:
        self._stop.set()
        for listener in (self._keyboard_listener, self._mouse_listener):
            if listener is not None:
                try:
                    listener.stop()
                except Exception:
                    pass

    def run(self) -> None:
        try:
            self._keyboard_listener = keyboard.Listener(
                on_press=self._on_key_press, on_release=self._on_key_release
            )
            self._keyboard_listener.start()
        except Exception as exc:
            self._keyboard_listener = None
            self._log("warning", "keyboard listener unavailable: %s", exc)
        try:
            self._mouse_listener = mouse.Listener(on_click=self._on_click)
            self._mouse_listener.start()
        except Exception as exc:
            self._mouse_listener = None
            self._log("warning", "mouse listener unavailable: %s", exc)

        if self._keyboard_listener is None and self._mouse_listener is None:
            self._log("error", "no global input listener could start")
            return

        self._log(
            "info",
            "global listeners ready; shortcut is %s (no suppression)",
            self.get_shortcut().display(),
        )
        while not self._stop.is_set():
            time.sleep(0.15)
        self.stop()

    def _on_key_press(self, key) -> None:
        token = pynput_token(key)
        if not token:
            return
        if token in self._pressed:
            return
        self._pressed.add(token)
        if not self.active():
            return
        shortcut = self.get_shortcut()
        if shortcut.matches(self._pressed) and token not in shortcuts.MODIFIERS:
            self._fired_keys = set(shortcut.keys)
            self.on_start()

    def _on_key_release(self, key) -> None:
        token = pynput_token(key)
        if not token:
            return
        self._pressed.discard(token)
        if token in self._fired_keys:
            self._fired_keys.discard(token)
            if not self._fired_keys:
                self.on_stop()

    def _on_click(self, x, y, button, pressed) -> None:
        name = getattr(button, "name", "")
        if name not in ("x1", "x2", "middle"):
            return
        if not self.active():
            return
        shortcut = self.get_shortcut()
        if shortcut.kind != "mouse" or shortcut.button != name:
            return
        if pressed and not self._fired_button:
            self._fired_button = name
            self.on_start()
        elif not pressed and self._fired_button == name:
            self._fired_button = ""
            self.on_stop()


def create_trigger(**kwargs):
    """Pick the backend for this OS."""
    if sys.platform == "win32":
        return WindowsTrigger(**kwargs)
    return PortableTrigger(**kwargs)


# ================================================================ #
# Merged from capture.py: interactive shortcut recorder (tray menu).
# ================================================================ #
class ShortcutCapture:
    def __init__(
        self,
        on_done: Callable[[shortcuts.Shortcut], None],
        on_cancel: Callable[[], None],
        on_preview: Callable[[str], None] | None = None,
        log=None,
        ignore_for: float = 0.4,
    ):
        self.on_done = on_done
        self.on_cancel = on_cancel
        self.on_preview = on_preview or (lambda _text: None)
        self.log = log
        self.ignore_for = ignore_for

        self._started_at = 0.0
        self._done = False
        self._modifiers: set[str] = set()
        self._pressed: set[str] = set()
        self._keyboard_listener: keyboard.Listener | None = None
        self._mouse_listener: mouse.Listener | None = None
        self._lock = threading.Lock()

    # -- lifecycle --------------------------------------------------------
    def start(self) -> bool:
        self._started_at = time.time()
        self._done = False
        kwargs = {}
        if sys.platform == "win32":
            kwargs["win32_event_filter"] = self._win32_filter
        try:
            self._keyboard_listener = keyboard.Listener(
                on_press=self._on_key_press,
                on_release=self._on_key_release,
                **kwargs,
            )
            self._keyboard_listener.start()
        except Exception as exc:
            if self.log:
                self.log.warning("could not capture keyboard input: %s", exc)
            self._keyboard_listener = None
        try:
            self._mouse_listener = mouse.Listener(on_click=self._on_click)
            self._mouse_listener.start()
        except Exception as exc:
            if self.log:
                self.log.warning("could not capture mouse input: %s", exc)
            self._mouse_listener = None

        if self._keyboard_listener is None and self._mouse_listener is None:
            self._stop_listeners()
            return False
        return True

    def stop(self) -> None:
        """Stop listening without reporting a result (app shutdown)."""
        self._done = True
        self._stop_listeners()

    def cancel(self) -> None:
        with self._lock:
            if self._done:
                return
            self._done = True
        self._stop_listeners()
        self.on_cancel()

    def _stop_listeners(self) -> None:
        for listener in (self._keyboard_listener, self._mouse_listener):
            if listener is not None:
                try:
                    listener.stop()
                except Exception:
                    pass
        self._keyboard_listener = None
        self._mouse_listener = None

    def _ignored(self) -> bool:
        return time.time() - self._started_at < self.ignore_for

    # -- Windows suppression ---------------------------------------------
    def _win32_filter(self, msg, data):
        # False tells pynput to swallow the event. Keep delivering it to the
        # callbacks, but never to the application behind the panel.
        return not self._done

    # -- keyboard ---------------------------------------------------------
    def _on_key_press(self, key) -> None:
        if self._done or self._ignored():
            return
        token = pynput_token(key)
        if not token:
            return

        if token == "esc":
            self.cancel()
            return

        if token in self._pressed:
            return
        self._pressed.add(token)

        if token in shortcuts.MODIFIERS:
            self._modifiers.add(token)
            if self._modifiers:
                parts = [
                    shortcuts.modifier_label(item)
                    for item in sorted(self._modifiers, key=shortcuts.MODIFIER_ORDER.index)
                ]
                self.on_preview("+".join(parts) + "+…")
            return

        keys = sorted(self._modifiers, key=shortcuts.MODIFIER_ORDER.index) + [token]
        try:
            candidate = shortcuts.build(keys)
        except ValueError:
            return
        problem = candidate.problem()
        if problem:
            self.on_preview(problem)
            return
        self._finish(candidate)

    def _on_key_release(self, key) -> None:
        token = pynput_token(key)
        if not token:
            return
        self._pressed.discard(token)
        if token in shortcuts.MODIFIERS:
            self._modifiers.discard(token)
            if not self._modifiers and not self._done:
                self.on_preview("Waiting for a key, combination or click…")

    # -- mouse ------------------------------------------------------------
    def _on_click(self, x, y, button, pressed) -> None:
        if not pressed or self._done or self._ignored():
            return
        name = getattr(button, "name", "")
        if name not in ("x1", "x2", "middle"):
            return
        self._finish(shortcuts.mouse(name))

    # -- finish -----------------------------------------------------------
    def _finish(self, shortcut: shortcuts.Shortcut) -> None:
        with self._lock:
            if self._done:
                return
            self._done = True
        # Stopping a listener from its own callback can deadlock on some
        # backends, so do it on a separate thread.
        threading.Thread(target=self._stop_listeners, daemon=True).start()
        self.on_done(shortcut)
