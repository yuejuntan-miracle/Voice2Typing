"""Gemini 3.5 Transcribe Live, over the public Live API (an API key).

This module streams the recorded clip to Google's speech endpoint over the
same WebSocket the Live API uses, authenticated with an API key the user keeps
in config.json. It is a documented, supported API with a free tier (about
0.005 USD per minute on the paid tier).

Wire contract (see ai.google.dev/gemini-api/docs/live-api/live-transcribe):

    wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key=<api key>

    first message:
      {"setup": {"model": "models/gemini-3.5-transcribe-live",
                 "generationConfig": {"responseModalities": ["TEXT"]},
                 "inputAudioTranscription": {"languageCodes": [...],
                                            "mode": "VERBATIM",
                                            "customVocabulary": [...]}}}

    then realtimeInput.audio frames (raw 16-bit PCM, 16 kHz, little-endian,
    100 ms / 3200 bytes each), paced at no more than 2x real time, then
      {"realtimeInput": {"audioStreamEnd": true}}

    <- {"serverContent": {"interimInputTranscription": {"text": ...}}}
       {"serverContent": {"inputTranscription": {"text": ...}}}   (final, one
                                                                  per turn)
       {"serverContent": {"generationComplete": true}}
       {"voiceActivity": {"type": "ACTIVITY_END", ...}}
"""
from __future__ import annotations

import base64
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import numpy as np

try:
    import websocket
except Exception:  # pragma: no cover - only on a broken install
    websocket = None

DEFAULT_LANGUAGE = "en"

# The app's tray menu speaks display names; the endpoint wants BCP-47 codes.
# Empty list means "auto-detect", which is the safer default for short clips.
LANGUAGE_CODES = {
    "auto": [],
    "english": ["en-US"],
    "chinese": ["zh-CN"],
    "cantonese": ["zh-HK"],
    "japanese": ["ja-JP"],
    "korean": ["ko-KR"],
    "spanish": ["es-ES"],
    "french": ["fr-FR"],
    "german": ["de-DE"],
    "russian": ["ru-RU"],
}


def code_for_language(name: str | None) -> list[str]:
    return list(LANGUAGE_CODES.get(str(name or "").strip().lower(), []))


MODEL = "gemini-3.5-transcribe-live"
WS_HOST = "generativelanguage.googleapis.com"
WS_PATH = "/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
ENDPOINT_SAMPLE_RATE = 16000
FRAME_BYTES = 3200  # 100 ms of 16 kHz mono PCM16
FRAME_SECONDS = 0.1
PACE_SECONDS = 0.05  # 2x real time keeps the server happy and halves latency
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 15.0
FINAL_TIMEOUT = 12.0

USER_AGENT = "V2T/2.0 (+local dictation client)"


class SttError(Exception):
    """Carries a sentence worth showing in the transcript panel."""


def api_key(value: str | None = None) -> str:
    """Resolve an API key from the argument, the environment, or config."""
    if value:
        text = str(value).strip()
        if text:
            return text
    for env_name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY"):
        found = os.environ.get(env_name)
        if found and found.strip():
            return found.strip()
    return ""


def _pcm16(audio: np.ndarray, sample_rate: int) -> bytes:
    """Float mono at any rate -> 16 kHz mono PCM16, which is all it accepts."""
    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    if sample_rate != ENDPOINT_SAMPLE_RATE and samples.size:
        target = max(1, int(round(samples.size * ENDPOINT_SAMPLE_RATE / sample_rate)))
        source_axis = np.linspace(0.0, 1.0, samples.size, endpoint=False)
        target_axis = np.linspace(0.0, 1.0, target, endpoint=False)
        samples = np.interp(target_axis, source_axis, samples).astype(np.float32)
    if samples.size == 0:
        return b""
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


def _url(key: str) -> str:
    return f"wss://{WS_HOST}{WS_PATH}?key={urllib.parse.quote(key, safe='')}"


def _setup_message(
    language_codes: list[str],
    keyterms: list[str] | None,
    model: str | None = None,
) -> dict:
    transcription: dict = {
        "languageCodes": list(language_codes or []),
        "mode": "VERBATIM",
    }
    terms = []
    for term in (keyterms or [])[:100]:
        clean = " ".join(str(term).split())[:64]
        if clean:
            terms.append(clean)
    if terms:
        transcription["customVocabulary"] = terms
    model_name = (model or MODEL).strip() or MODEL
    if not model_name.startswith("models/"):
        model_name = f"models/{model_name}"
    return {
        "setup": {
            "model": model_name,
            "generationConfig": {"responseModalities": ["TEXT"]},
            "inputAudioTranscription": transcription,
        }
    }


def _server_error(body: str) -> str:
    try:
        payload = json.loads(body or "{}")
    except Exception:
        return ""
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("status") or "")
    if isinstance(error, str):
        return error
    return ""


def _open_connection(key: str):
    """Open the Gemini Live WebSocket, translating HTTP errors into SttError."""
    try:
        return websocket.create_connection(
            _url(key), timeout=CONNECT_TIMEOUT, suppress_origin=True
        )
    except websocket.WebSocketBadStatusException as exc:
        status = getattr(exc, "status_code", None)
        resp_body = getattr(exc, "resp_body", None)
        body_str: str
        if resp_body is None:
            body_str = ""
        elif isinstance(resp_body, str):
            body_str = resp_body
        elif isinstance(resp_body, bytes):
            body_str = resp_body.decode("utf-8", "replace")
        else:
            body_str = str(resp_body)
        detail = _server_error(body_str)
        if status in (400, 401, 403):
            raise SttError(
                "The Gemini API key was rejected"
                + (f" ({detail})" if detail else "")
                + ". Check gemini_api_key in config.json."
            ) from None
        if status == 429:
            raise SttError(
                "Gemini quota exceeded (free tier limits). Try again later "
                "or enable billing."
            ) from None
        raise SttError(
            f"Could not reach the Gemini speech endpoint (HTTP {status})."
        ) from None
    except Exception as exc:
        raise SttError(f"Could not reach the Gemini speech endpoint: {exc}") from None


def _audio_frame_message(pcm_chunk: bytes) -> str:
    return json.dumps(
        {
            "realtimeInput": {
                "audio": {
                    "mimeType": f"audio/pcm;rate={ENDPOINT_SAMPLE_RATE}",
                    "data": base64.b64encode(pcm_chunk).decode("ascii"),
                }
            }
        }
    )


class LiveSession:
    """Real-time Gemini 3.5 Transcribe Live session.

    Opens the WebSocket and streams 100 ms PCM frames *while* the user is
    speaking, so releasing the shortcut only needs to flush the tail and send
    ``audioStreamEnd``. That cuts post-release STT latency from ~4 s down to
    ~0.3-0.6 s.
    """

    def __init__(
        self,
        sample_rate: int = ENDPOINT_SAMPLE_RATE,
        language_codes: list[str] | None = None,
        keyterms: list[str] | None = None,
        api_key_value: str | None = None,
        mode: str = "VERBATIM",
        model: str | None = None,
    ):
        self.sample_rate = int(sample_rate)
        self.language_codes = list(language_codes or [])
        self.keyterms = list(keyterms or [])
        self.api_key_value = api_key_value
        self.mode = "SMART" if str(mode).upper() == "SMART" else "VERBATIM"
        self.model = (model or MODEL).strip() or MODEL

        self._queue: queue.Queue[np.ndarray | None] = queue.Queue()
        self._cancel = threading.Event()
        self._done = threading.Event()
        self._thread: threading.Thread | None = None
        self._conn = None
        self._conn_lock = threading.Lock()
        self._finals: list[str] = []
        self._interim = ""
        self._error: Exception | None = None
        self._started = False

    def start(self) -> bool:
        """Start connecting and streaming on a background thread (never blocks)."""
        if websocket is None or not api_key(self.api_key_value):
            return False
        self._started = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="gemini-live-stt"
        )
        self._thread.start()
        return True

    def push(self, audio_chunk: np.ndarray) -> None:
        """Enqueue a float32 audio block from the microphone callback."""
        if not self._started or self._cancel.is_set() or self._done.is_set():
            return
        try:
            self._queue.put_nowait(audio_chunk)
        except Exception:
            pass

    def cancel(self) -> None:
        """Abort the session immediately (e.g. tap too short or flat silence)."""
        self._cancel.set()
        try:
            self._queue.put_nowait(None)
        except Exception:
            pass
        with self._conn_lock:
            conn = self._conn
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def finish(self, timeout: float = FINAL_TIMEOUT) -> str:
        """Signal end-of-speech and wait for the final transcript."""
        if not self._started:
            raise SttError("Gemini live session was not started.")
        try:
            self._queue.put_nowait(None)
        except Exception:
            pass
        if not self._done.wait(timeout=timeout + CONNECT_TIMEOUT):
            self.cancel()
            raise SttError("Gemini live transcription timed out.")
        if self._error is not None:
            if isinstance(self._error, SttError):
                raise self._error
            raise SttError(str(self._error))
        parts = list(self._finals)
        tail = self._interim.strip()
        if tail:
            parts.append(tail)
        text = " ".join(parts).strip()
        return " ".join(text.split())

    def _drain_incoming(self, conn, state: dict) -> None:
        while not self._cancel.is_set():
            try:
                message = conn.recv()
            except websocket.WebSocketTimeoutException:
                return
            except Exception:
                return
            self._handle_message(message, state)

    def _handle_message(self, message, state: dict) -> None:
        if isinstance(message, bytes):
            message = message.decode("utf-8", "replace")
        err = _server_error(message)
        if err:
            raise SttError(f"Gemini speech error: {err}")
        try:
            payload = json.loads(message)
        except Exception:
            return
        sc = payload.get("serverContent") or {}
        if sc.get("interimInputTranscription"):
            text = str(sc["interimInputTranscription"].get("text") or "")
            if text:
                self._interim = text
                state["interim_pending"] = True
        if sc.get("inputTranscription"):
            text = str(sc["inputTranscription"].get("text") or "")
            if text:
                self._finals.append(text)
                self._interim = ""
                state["interim_pending"] = False
                state["input_final_seen"] = True
                if state["stream_ended"]:
                    state["post_end_final"] = True
        if sc.get("generationComplete") or sc.get("turnComplete"):
            state["generation_complete"] = True
            state["audio_after_end"] = False
            if state["stream_ended"]:
                state["post_end_done"] = True
        va = payload.get("voiceActivity") or {}
        va_type = va.get("type")
        if va_type == "ACTIVITY_START":
            state["in_speech"] = True
            state["activity_end"] = False
            state["generation_complete"] = False
        elif va_type == "ACTIVITY_END":
            state["in_speech"] = False
            state["activity_end"] = True
            state["audio_after_end"] = False
            if state["stream_ended"]:
                state["post_end_done"] = True

    def _run(self) -> None:
        key = api_key(self.api_key_value)
        setup = _setup_message(self.language_codes, self.keyterms, model=self.model)
        setup["setup"]["inputAudioTranscription"]["mode"] = self.mode
        state = {
            "input_final_seen": False,
            "generation_complete": False,
            "activity_end": False,
            "in_speech": False,
            "interim_pending": False,
            "audio_after_end": False,
            "stream_ended": False,
            "post_end_final": False,
            "post_end_done": False,
        }
        conn = None
        try:
            if self._cancel.is_set():
                return
            conn = _open_connection(key)
            with self._conn_lock:
                self._conn = conn
            if self._cancel.is_set():
                return

            conn.settimeout(CONNECT_TIMEOUT)
            conn.send(json.dumps(setup))
            deadline = time.time() + CONNECT_TIMEOUT
            while time.time() < deadline and not self._cancel.is_set():
                try:
                    message = conn.recv()
                except websocket.WebSocketTimeoutException:
                    break
                if isinstance(message, bytes):
                    message = message.decode("utf-8", "replace")
                err = _server_error(message)
                if err:
                    raise SttError(f"Gemini speech error: {err}")
                if "setupComplete" in str(message):
                    break
            else:
                if not self._cancel.is_set():
                    raise SttError("Gemini did not finish setup.")

            if self._cancel.is_set():
                return

            conn.settimeout(0.01)
            pcm_buf = bytearray()
            sent_any = False
            while not self._cancel.is_set():
                try:
                    chunk = self._queue.get(timeout=0.05)
                except queue.Empty:
                    self._drain_incoming(conn, state)
                    continue
                if chunk is None:
                    break
                pcm_buf.extend(_pcm16(chunk, self.sample_rate))
                while len(pcm_buf) >= FRAME_BYTES and not self._cancel.is_set():
                    frame = bytes(pcm_buf[:FRAME_BYTES])
                    del pcm_buf[:FRAME_BYTES]
                    try:
                        conn.send(_audio_frame_message(frame))
                        sent_any = True
                        state["audio_after_end"] = True
                    except websocket.WebSocketConnectionClosedException:
                        raise SttError(
                            "Gemini closed the connection while streaming audio."
                        ) from None
                self._drain_incoming(conn, state)

            if self._cancel.is_set():
                return

            if pcm_buf:
                try:
                    conn.send(_audio_frame_message(bytes(pcm_buf)))
                    sent_any = True
                    state["audio_after_end"] = True
                except websocket.WebSocketConnectionClosedException:
                    raise SttError(
                        "Gemini closed the connection while streaming audio."
                    ) from None
                pcm_buf.clear()

            if not sent_any:
                return

            # Wait for a fresh post-end final whenever the user kept speaking
            # (or sent audio) after an earlier mid-recording pause/ACTIVITY_END.
            needs_new_final = (
                not self._finals
                or state["in_speech"]
                or state["interim_pending"]
                or state["audio_after_end"]
            )
            state["stream_ended"] = True
            state["post_end_final"] = False
            state["post_end_done"] = False
            conn.send(json.dumps({"realtimeInput": {"audioStreamEnd": True}}))

            conn.settimeout(0.25)
            now = time.time()
            deadline = now + (FINAL_TIMEOUT if needs_new_final else 0.4)
            last_msg_at = now
            final_at: float | None = None
            while time.time() < deadline and not self._cancel.is_set():
                if state["post_end_final"] and state["post_end_done"]:
                    break
                if (
                    not needs_new_final
                    and state["post_end_done"]
                    and not state["interim_pending"]
                ):
                    break
                try:
                    message = conn.recv()
                except websocket.WebSocketTimeoutException:
                    idle = time.time() - last_msg_at
                    # If we got a post-end final but no trailing done event, don't hang.
                    if final_at is not None and time.time() - final_at >= 1.5:
                        break
                    # If we already had a prior final and only trailing silence was
                    # sent after the last ACTIVITY_END, settle after 1.5s of quiet.
                    if (
                        self._finals
                        and not state["in_speech"]
                        and not state["interim_pending"]
                        and idle >= 1.5
                    ):
                        break
                    continue
                except Exception:
                    break
                last_msg_at = time.time()
                self._handle_message(message, state)
                if state["post_end_final"] and final_at is None:
                    final_at = last_msg_at
        except Exception as exc:
            if not self._cancel.is_set():
                self._error = exc
        finally:
            with self._conn_lock:
                self._conn = None
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            self._done.set()


def transcribe(
    audio: np.ndarray,
    sample_rate: int = ENDPOINT_SAMPLE_RATE,
    language_codes: list[str] | None = None,
    keyterms: list[str] | None = None,
    api_key_value: str | None = None,
    mode: str = "VERBATIM",
    timeout: float = FINAL_TIMEOUT,
    model: str | None = None,
) -> str:
    """Stream one recorded clip and return the final transcript.

    The whole clip is uploaded at 2x real time, then ``audioStreamEnd`` asks
    for the final. Returns ``""`` for silence. Raises ``SttError`` with a
    readable sentence for anything the user can fix.
    """
    if websocket is None:
        raise SttError("The Gemini speech engine is unavailable (websocket-client missing)")

    key = api_key(api_key_value)
    if not key:
        raise SttError(
            "No Gemini API key. Add \"gemini_api_key\" to config.json "
            "or set GEMINI_API_KEY."
        )

    pcm = _pcm16(audio, sample_rate)
    if not pcm:
        return ""

    # VERBATIM mode is the default and keeps the spoken words intact.
    transcription_mode = "VERBATIM" if str(mode).upper() != "SMART" else "SMART"
    setup = _setup_message(language_codes or [], keyterms, model=model)
    setup["setup"]["inputAudioTranscription"]["mode"] = transcription_mode

    conn = _open_connection(key)

    finals: list[str] = []
    interim = ""
    in_speech = False
    interim_pending = False
    audio_after_end = False
    post_end_final = False
    post_end_done = False
    try:
        conn.settimeout(CONNECT_TIMEOUT)
        conn.send(json.dumps(setup))
        deadline = time.time() + CONNECT_TIMEOUT
        while time.time() < deadline:
            try:
                message = conn.recv()
            except websocket.WebSocketTimeoutException:
                break
            if isinstance(message, bytes):
                message = message.decode("utf-8", "replace")
            err = _server_error(message)
            if err:
                raise SttError(f"Gemini speech error: {err}")
            if "setupComplete" in str(message):
                break
        else:
            raise SttError("Gemini did not finish setup.")

        def _consume(raw_msg, after_end: bool = False) -> None:
            nonlocal interim, in_speech, interim_pending, audio_after_end
            nonlocal post_end_final, post_end_done
            if isinstance(raw_msg, bytes):
                raw_msg = raw_msg.decode("utf-8", "replace")
            err_msg = _server_error(raw_msg)
            if err_msg:
                raise SttError(f"Gemini speech error: {err_msg}")
            try:
                payload = json.loads(raw_msg)
            except Exception:
                return
            sc = payload.get("serverContent") or {}
            if sc.get("interimInputTranscription"):
                t = str(sc["interimInputTranscription"].get("text") or "")
                if t:
                    interim = t
                    interim_pending = True
            if sc.get("inputTranscription"):
                t = str(sc["inputTranscription"].get("text") or "")
                if t:
                    finals.append(t)
                    interim = ""
                    interim_pending = False
                    if after_end:
                        post_end_final = True
            if sc.get("generationComplete") or sc.get("turnComplete"):
                audio_after_end = False
                if after_end:
                    post_end_done = True
            va = payload.get("voiceActivity") or {}
            va_type = va.get("type")
            if va_type == "ACTIVITY_START":
                in_speech = True
            elif va_type == "ACTIVITY_END":
                in_speech = False
                audio_after_end = False
                if after_end:
                    post_end_done = True

        conn.settimeout(0.05)
        for offset in range(0, len(pcm), FRAME_BYTES):
            try:
                conn.send(_audio_frame_message(pcm[offset : offset + FRAME_BYTES]))
                audio_after_end = True
            except websocket.WebSocketConnectionClosedException:
                raise SttError(
                    "Gemini closed the connection while streaming audio."
                ) from None
            # Drain anything that arrives while we stream.
            while True:
                try:
                    message = conn.recv()
                except websocket.WebSocketTimeoutException:
                    break
                except Exception:
                    break
                _consume(message, after_end=False)
            time.sleep(PACE_SECONDS)

        needs_new_final = (
            not finals or in_speech or interim_pending or audio_after_end
        )
        post_end_final = False
        post_end_done = False
        conn.send(json.dumps({"realtimeInput": {"audioStreamEnd": True}}))

        conn.settimeout(0.25)
        now = time.time()
        deadline = now + (timeout if needs_new_final else 0.4)
        last_msg_at = now
        final_at: float | None = None
        while time.time() < deadline:
            if post_end_final and post_end_done:
                break
            if not needs_new_final and post_end_done and not interim_pending:
                break
            try:
                message = conn.recv()
            except websocket.WebSocketTimeoutException:
                idle = time.time() - last_msg_at
                if final_at is not None and time.time() - final_at >= 1.5:
                    break
                if finals and not in_speech and not interim_pending and idle >= 1.5:
                    break
                continue
            except Exception:
                break
            last_msg_at = time.time()
            _consume(message, after_end=True)
            if post_end_final and final_at is None:
                final_at = last_msg_at
    finally:
        try:
            conn.close()
        except Exception:
            pass

    parts = list(finals)
    if interim.strip():
        parts.append(interim.strip())
    text = " ".join(parts).strip()
    return " ".join(text.split())
