"""Yandex AI Studio Realtime API: audio question -> audio answer.

Replaces the old STT -> YandexGPT -> TTS chain with a SINGLE voice channel:

  * ``RealtimeDialog`` — a persistent WebSocket session (one per service
    lifetime): camera PCM goes into ``input_audio_buffer.append``, the
    server-side VAD detects the end of the utterance, the Speech Realtime
    model recognizes the speech, generates the answer and synthesizes the
    speech in one pass. The finished answer arrives as
    ``response.output_audio.delta`` chunks (PCM int16 LE mono) together with
    the response text (``response.output_text.delta`` — used to extract the
    trailing "Emotion: <name>" command, see ``split_emotion()``).
  * ``ask_audio()`` — one-shot TEXT request -> audio answer over a short
    Realtime session (used by the post-dialogue emotion decay phrases).

Realtime API protocol (Yandex AI Studio, OpenAI-compatible events):

  * endpoint: wss://ai.api.cloud.yandex.net/v1/realtime/openai
      ?model=gpt://<folder_id>/speech-realtime-260528
  * auth:     HTTP header ``Authorization: Api-Key <key>``
  * events:   JSON objects; client -> server: ``session.update``,
    ``input_audio_buffer.append``, ``conversation.item.create``,
    ``response.create``; server -> client: ``session.created/updated``,
    ``input_audio_buffer.speech_started/stopped``,
    ``conversation.item.input_audio_transcription.completed`` (question
    transcript), ``response.created``, ``response.output_audio.delta``,
    ``response.output_text.delta``, ``response.done`` / ``response.cancelled``.

The Realtime WS reader runs as an asyncio task, so the server asyncio loop is
never blocked; callbacks are invoked from that task (async callbacks are
scheduled, delivery order is preserved).

Audio format — PCM int16 LE, 16 kHz mono (as the camera sends it and as the
robot expects it), so no resampling is needed.

Credentials: the ``YANDEX_API_KEY`` / ``YANDEX_FOLDER_ID`` environment
variables or the ``yandex`` section of ``config/settings.json`` (with ${VAR}
support). The websockets dependency is imported lazily.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import socket
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("uvicorn")

# websockets protocol state enum (new API); used to check the connection
# state across websockets 10.x–17.x.
try:
    from websockets.protocol import State as _WSState  # type: ignore
except Exception:  # noqa: BLE001
    _WSState = None


def _ws_open(ws: Any) -> bool:
    """True when a websockets connection is in the OPEN state.

    New API (14+): ``ws.state`` is a ``State`` enum. Old API (10–13):
    ``ws.closed`` is a bool. Returns False for any other/unknown state.
    """
    state = getattr(ws, "state", None)
    if _WSState is not None and state is _WSState.OPEN:
        return True
    name = getattr(state, "name", None)
    if name is not None:
        return str(name).upper() == "OPEN"
    closed = getattr(ws, "closed", None)
    return closed is False


# Protocol audio sample rate (camera input and robot output), Hz.
SAMPLE_RATE_HZ = 16000

# Yandex Realtime API (AI Studio) endpoint.
REALTIME_HOST = "ai.api.cloud.yandex.net"
REALTIME_PATH = "/v1/realtime/openai"

# Default Realtime model (overridable via yandex.realtime_model).
DEFAULT_MODEL = "speech-realtime-260528"

# Default TTS voice for answers (overridable via yandex.voice).
DEFAULT_VOICE = "alena"

# Server-side VAD parameters for the incoming audio stream.
VAD_THRESHOLD = 0.5
VAD_SILENCE_MS = 400

# In this network the IPv6 addresses of Yandex Cloud do not respond, while
# requests/websockets take the FIRST address from getaddrinfo (that is IPv6)
# and hang until a timeout. Prefer IPv4 for *.api.cloud.yandex.net hosts.
_orig_getaddrinfo = socket.getaddrinfo
if socket.has_ipv6:
    def _getaddrinfo_ipv4_first(host: str, *args, **kwargs):
        result = _orig_getaddrinfo(host, *args, **kwargs)
        if str(host).endswith("api.cloud.yandex.net"):
            return sorted(result, key=lambda item: item[0] != socket.AF_INET)
        return result
    socket.getaddrinfo = _getaddrinfo_ipv4_first


def get_credentials() -> tuple[str, str]:
    """Returns (api_key, folder_id) from env or config/settings.json."""
    key = os.environ.get("YANDEX_API_KEY", "").strip()
    folder = os.environ.get("YANDEX_FOLDER_ID", "").strip()
    if key and folder:
        return key, folder
    try:
        from . import config as app_config
        y = app_config.CONFIG.get("yandex", {})
        key = key or str(y.get("api_key", "")).strip()
        folder = folder or str(y.get("folder_id", "")).strip()
    except Exception:  # noqa: BLE001
        pass
    return key, folder


def credentials_ok() -> bool:
    key, folder = get_credentials()
    return bool(key and folder)


def check_connectivity(timeout_s: float = 8.0) -> dict:
    """Checks the internet and Yandex credentials (called at startup).

    Internet — GET to api.cloud.yandex.net (already IPv4-first). Credentials —
    a minimal YandexGPT request (maxTokens=1, spends one token): 200 — ok;
    401 — wrong Api-Key; 403 — no access to the folder; otherwise an HTTP
    error. Returns {"internet": bool, "status": ..., "detail": ...}.
    """
    import requests

    key, folder = get_credentials()
    if not key or not folder:
        return {"internet": False, "status": "no_credentials",
                "detail": "YANDEX_API_KEY / YANDEX_FOLDER_ID are not set"}
    result = {"internet": False, "status": "checking", "detail": ""}
    try:
        requests.get("https://api.cloud.yandex.net/", timeout=timeout_s)
        result["internet"] = True
    except Exception as exc:  # noqa: BLE001
        result.update(status="no_internet",
                      detail=f"No connection to Yandex Cloud: {exc}")
        return result
    try:
        resp = requests.post(
            "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
            json={
                "modelUri": f"gpt://{folder}/yandexgpt/latest",
                "completionOptions": {"stream": False, "temperature": 0.0,
                                      "maxTokens": "1"},
                "messages": [{"role": "user", "text": "check"}],
            },
            headers={"Content-Type": "application/json",
                     "Authorization": f"Api-Key {key}"},
            timeout=timeout_s)
        code = resp.status_code
        if code == 200:
            result.update(status="ok",
                          detail="credentials valid (Yandex replied 200)")
        elif code == 401:
            result.update(status="invalid_credentials",
                          detail="Yandex: Unauthenticated — wrong or revoked "
                                 "Api-Key")
        elif code == 403:
            result.update(status="forbidden",
                          detail=f"Yandex: Forbidden — no access to the folder "
                                 f"'{folder}' (check the folder/permissions)")
        else:
            result.update(status=f"http_{code}",
                          detail=f"Yandex: HTTP {code}: {resp.text[:300]}")
    except Exception as exc:  # noqa: BLE001
        result.update(status="request_error",
                      detail=f"Credential check error: {exc}")
    return result


# ---------------------------------------------------------------------------
# System prompts and the emotion command extraction.
# ---------------------------------------------------------------------------

def default_system_prompt() -> str:
    """System prompt for the voice model (from yandex.system_prompt)."""
    try:
        from . import config as app_config
        return str(app_config.CONFIG.get("yandex", {}).get(
            "system_prompt", ""))
    except Exception:  # noqa: BLE001
        return ""


def default_emotion_decay_prompt() -> str:
    """Emotion-decay prompt (from yandex.emotion_decay_prompt config):
    asks the model to generate a short phrase matching the given emotion when
    the robot is left alone after a dialogue."""
    try:
        from . import config as app_config
        return str(app_config.CONFIG.get("yandex", {}).get(
            "emotion_decay_prompt", ""))
    except Exception:  # noqa: BLE001
        return ""


# Robot emotion names — the EMOTION:<name> command in the robot protocol.
# The model appends them to the end of the answer after the "Emotion:"
# keyword (exactly in this form, untranslated).
ROBOT_EMOTIONS = ("neutral", "happy", "angry", "sad", "doubt", "sleepy",
                  "dancing")


def split_emotion(text: str) -> tuple[str, Optional[str]]:
    """Extracts the emotion command from the end of the model answer.

    Following the prompt, the model appends a line like "\n\nEmotion: Happy"
    (values limited to ROBOT_EMOTIONS) to the end of the answer — a command
    for the robot to show an emotion, not part of the speech.

    Returns (text without the emotion command, emotion name or None).
    """
    if not text:
        return text, None
    stripped = text.strip()
    m = re.search(r"(?is)\bemotion\s*[:：]\s*([a-z]+)\s*$", stripped)
    if not m:
        return text, None
    name = m.group(1).strip().lower()
    if name in ROBOT_EMOTIONS:
        return stripped[:m.start()].rstrip(), name
    return text, None


# ---------------------------------------------------------------------------
# Realtime session payloads.
# ---------------------------------------------------------------------------

def _realtime_url(folder: str, model: str) -> str:
    """WebSocket URL of the Realtime API for the given model URI."""
    return (f"wss://{REALTIME_HOST}{REALTIME_PATH}"
            f"?model=gpt://{folder}/{model}")


def _session_payload(*, instructions: str, output_modalities: list[str],
                     input_rate: int, output_rate: int, language: str,
                     voice: str, role: str,
                     turn_detection: bool,
                     tools: Optional[list] = None) -> dict:
    """Builds the ``session.update`` payload of a Realtime session.

    output_modalities — ONE modality only (Yandex accepts either "audio" or
    "text"; the dialog uses ["audio"], but the model still reports the
    answer text via response.output_text.delta — used for debug/fallback).
    turn_detection — enable the server-side VAD (continuous dialog) or not
    (one-shot text requests).
    tools — list of function-calling tools exposed to the model (e.g. the
    ``emotion`` command). Instead of speaking "Emotion: Happy", the model
    CALLS the function and the server executes it (robot emotion command).
    """
    session: dict[str, Any] = {
        "type": "realtime",
        "instructions": instructions,
        "output_modalities": output_modalities,
        "audio": {
            "input": {
                "format": {"type": "audio/pcm", "rate": input_rate},
                "languages": [language],
            },
            "output": {
                "format": {"type": "audio/pcm", "rate": output_rate},
                "voice": voice,
            },
        },
    }
    if role:
        session["audio"]["output"]["role"] = role
    if turn_detection:
        session["audio"]["input"]["turn_detection"] = {
            "type": "server_vad",
            "threshold": VAD_THRESHOLD,
            "silence_duration_ms": VAD_SILENCE_MS,
        }
    if tools:
        session["tools"] = tools
    return {"type": "session.update", "session": session}


# The tool schemas (e.g. emotion) and their server-side handlers live in
# callbacks.py (see TOOLS / dispatch); yandex.py only transports them.


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


# ---------------------------------------------------------------------------
# Persistent Realtime dialog (audio question -> audio answer).
# ---------------------------------------------------------------------------

class RealtimeDialog:
    """Persistent voice channel: camera PCM -> model answer PCM.

    One WebSocket connection lives for the whole service lifetime; the server
    keeps the dialog context in the session. Camera audio is fed via
    ``feed()``; the server-side VAD closes the utterance and the model
    responds automatically. The finished answer is delivered via the
    ``on_answer(pcm, text)`` callback; the question transcript via
    ``on_user_text(transcript)``; speech start via ``on_speech_started()``.

    The reader runs as an asyncio task; async callbacks are scheduled as
    tasks (delivery order is preserved), sync ones are called directly.
    """

    def __init__(self, *, model: str = DEFAULT_MODEL, voice: str = DEFAULT_VOICE,
                 role: str = "", instructions: str = "",
                 input_rate: int = SAMPLE_RATE_HZ,
                 output_rate: int = SAMPLE_RATE_HZ,
                 language: str = "ru-RU",
                 tools: Optional[list] = None,
                 on_answer: Optional[Callable[[bytes, str], Any]] = None,
                 on_user_text: Optional[Callable[[str], Any]] = None,
                 on_speech_started: Optional[Callable[[], Any]] = None,
                 on_function_call: Optional[Callable[[str, dict], Any]] = None,
                 on_error: Optional[Callable[[Exception], Any]] = None) -> None:
        self.model = model
        self.voice = voice
        self.role = role
        self.instructions = instructions
        self.input_rate = input_rate
        self.output_rate = output_rate
        self.language = language
        self.tools = list(tools) if tools else []
        self.on_answer = on_answer
        self.on_user_text = on_user_text
        self.on_speech_started = on_speech_started
        # Called with (function_name, arguments_dict) when the model calls a
        # tool (e.g. emotion) during its answer.
        self.on_function_call = on_function_call
        self.on_error = on_error
        self._ws: Any = None
        self._reader: Optional[asyncio.Task] = None
        self._closed = False
        # Current answer being accumulated (reset on response.created).
        self._pcm = bytearray()
        self._text: list[str] = []
        self._answering = False
        self.started_at = 0.0

    # ------------------------------------------------------------------
    # Connection.
    # ------------------------------------------------------------------
    @property
    def connected(self) -> bool:
        return (not self._closed and self._ws is not None
                and _ws_open(self._ws))

    # NOTE: Yandex Realtime accepts only ONE output modality at a time
    # ("Modalities can be either audio or text"). The dialog uses ["audio"];
    # whether the model also reports response.output_text.delta / the audio
    # transcript depends on the server — it is logged for diagnostics.

    async def start(self) -> None:
        """Opens the Realtime WebSocket session and starts the reader."""
        import websockets

        key, folder = get_credentials()
        if not key or not folder:
            raise RuntimeError("YANDEX_API_KEY / YANDEX_FOLDER_ID are not set")
        url = _realtime_url(folder, self.model)
        logger.info("[realtime] connecting: %s (voice=%s role=%r rate=%d/%d)",
                    url, self.voice, self.role, self.input_rate,
                    self.output_rate)
        self._ws = await websockets.connect(
            url,
            additional_headers={"Authorization": f"Api-Key {key}"},
            open_timeout=20.0,
            ping_interval=20.0,
            ping_timeout=20.0,
            max_size=2 ** 24,
        )
        self._closed = False
        self.started_at = time.monotonic()
        await self._send(_session_payload(
            instructions=self.instructions,
            output_modalities=["audio"],
            input_rate=self.input_rate,
            output_rate=self.output_rate,
            language=self.language,
            voice=self.voice,
            role=self.role,
            turn_detection=True,
            tools=self.tools,
        ))
        self._reader = asyncio.create_task(self._read_loop())
        logger.info("[realtime] session started")

    async def close(self) -> None:
        """Closes the session (idempotent)."""
        self._closed = True
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None
        self._answering = False
        self._pcm = bytearray()
        self._text = []

    # ------------------------------------------------------------------
    # Sending audio (camera -> model).
    # ------------------------------------------------------------------
    async def feed(self, pcm: bytes) -> None:
        """Sends the next camera PCM chunk into the session."""
        if not self.connected or not pcm:
            return
        try:
            await self._ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": _b64(bytes(pcm)),
            }))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[realtime] send error: %s", exc)

    async def _send(self, obj: dict) -> None:
        if self.connected:
            await self._ws.send(json.dumps(obj, ensure_ascii=False))

    # ------------------------------------------------------------------
    # Reader loop and event handling.
    # ------------------------------------------------------------------
    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    logger.warning("[realtime] non-JSON message ignored")
                    continue
                try:
                    self._handle(msg)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[realtime] handler error: %s", exc)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.error("[realtime] connection lost: %s", exc)
            self._fire(self.on_error, exc)
        finally:
            if not self._closed:
                logger.warning("[realtime] reader finished (session closed)")
                self._closed = True
            self._ws = None

    def _handle(self, msg: dict) -> None:
        mtype = msg.get("type", "")
        if mtype in ("session.created", "session.updated"):
            sid = (msg.get("session") or {}).get("id", "")
            logger.info("[realtime] %s (session id=%s)", mtype, sid)
        elif mtype == "input_audio_buffer.speech_started":
            logger.info("[realtime] user started speaking")
            self._fire(self.on_speech_started)
        elif mtype == "conversation.item.input_audio_transcription.completed":
            transcript = str(msg.get("transcript", "")).strip()
            if transcript:
                logger.info("[realtime] user: %r", transcript)
                self._fire(self.on_user_text, transcript)
        elif mtype == "response.created":
            self._pcm = bytearray()
            self._text = []
            self._answering = True
        elif mtype == "response.output_audio.delta":
            if self._answering:
                try:
                    self._pcm += base64.b64decode(msg.get("delta", ""))
                except Exception:  # noqa: BLE001
                    logger.warning("[realtime] bad audio delta ignored")
        elif mtype == "response.output_text.delta":
            if self._answering:
                self._text.append(str(msg.get("delta", "")))
        elif mtype == "response.output_audio.done" and self._answering:
            transcript = ""
            item = msg.get("item") or {}
            content = item.get("content") or []
            if content and isinstance(content, list):
                parts = [c.get("transcript") for c in content
                         if isinstance(c, dict) and c.get("transcript")]
                transcript = " ".join(str(p) for p in parts if p)
            logger.info("[realtime] audio done (server transcript: %r)",
                        transcript[:120])
        elif mtype == "response.output_item.done":
            # Function calling: the model calls a server tool (e.g.
            # emotion(name)) instead of speaking the command. Execute it and
            # send back the function_call_output so the session stays
            # consistent; the answer then finishes without extra speech.
            item = msg.get("item") or {}
            if item.get("type") == "function_call":
                self._handle_function_call(item)
        elif mtype == "response.done":
            if self._answering:
                self._answering = False
                pcm = bytes(self._pcm)
                text = "".join(self._text).strip()
                self._pcm = bytearray()
                self._text = []
                if pcm:
                    logger.info("[realtime] answer ready: %d B pcm, %d chars",
                                len(pcm), len(text))
                    self._fire(self.on_answer, pcm, text)
                else:
                    logger.info("[realtime] answer finished but empty")
        elif mtype == "response.cancelled":
            # The user interrupted the playback — drop the partial answer.
            self._answering = False
            self._pcm = bytearray()
            self._text = []
            logger.info("[realtime] answer cancelled")
        elif mtype == "error":
            detail = json.dumps(msg, ensure_ascii=False)
            logger.error("[realtime] server error: %s", detail)
            err = RuntimeError(f"Yandex Realtime error: {detail}")
            self._fire(self.on_error, err)
        else:
            logger.debug("[realtime] event: %s", mtype)

    def _handle_function_call(self, item: dict) -> None:
        """Executes a model function call (tools / function calling).

        Calls ``on_function_call(name, args)``; its return value (a string
        or an awaitable of a string, e.g. the weather summary) is sent back
        to the model as ``function_call_output`` followed by
        ``response.create`` (the Realtime contract — without it the server
        closes the session). Side-effect-only calls (emotion) return None
        and the model must not speak anything after them.
        """
        call_id = str(item.get("call_id", ""))
        name = str(item.get("name", ""))
        args: dict = {}
        try:
            raw = item.get("arguments") or "{}"
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(parsed, dict):
                args = parsed
        except (json.JSONDecodeError, TypeError):
            logger.warning("[realtime] function_call %s: bad arguments %r",
                           name, item.get("arguments"))
        logger.info("[realtime] function call: %s(%s)", name, args)
        result = None
        if self.on_function_call is not None:
            try:
                result = self.on_function_call(name, args)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[realtime] function_call callback error: %s",
                               exc)
        asyncio.create_task(self._complete_tool_call(call_id, result))

    async def _complete_tool_call(self, call_id: str,
                                  result: Any = None) -> None:
        """Sends function_call_output + response.create (Realtime contract).

        ``result`` may be a plain string, None (→ "ok") or an awaitable
        returning the string (e.g. the weather callback).
        """
        if asyncio.iscoroutine(result):
            try:
                result = await result
            except Exception as exc:  # noqa: BLE001
                logger.warning("[realtime] function_call result error: %s",
                               exc)
                result = None
        output = str(result).strip() if result is not None else "ok"
        logger.info("[realtime] function_call_output -> %s", output[:200])
        await self._send({
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": output,
            },
        })
        await self._send({"type": "response.create"})

    def _fire(self, cb: Optional[Callable], *args: Any) -> None:
        """Invokes a callback; coroutines are scheduled (order preserved)."""
        if cb is None:
            return
        try:
            result = cb(*args)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[realtime] callback error: %s", exc)
            return
        if asyncio.iscoroutine(result):
            asyncio.create_task(self._run_coro(result))

    async def _run_coro(self, coro: Any) -> None:
        try:
            await coro
        except Exception as exc:  # noqa: BLE001
            logger.warning("[realtime] async callback error: %s", exc)


# ---------------------------------------------------------------------------
# One-shot text request -> audio answer (emotion-decay phrases).
# ---------------------------------------------------------------------------

async def ask_audio(text: str, *, instructions: str = "",
                    model: str = DEFAULT_MODEL, voice: str = DEFAULT_VOICE,
                    role: str = "",
                    input_rate: int = SAMPLE_RATE_HZ,
                    output_rate: int = SAMPLE_RATE_HZ,
                    timeout_s: float = 90.0) -> bytes:
    """One-shot Realtime request: TEXT -> synthesized speech (PCM 16k mono).

    Opens a short Realtime session, sends the text as a user message and
    collects the ``response.output_audio.delta`` stream until ``response.done``.
    Used by the post-dialogue emotion decay: the model both generates the
    phrase and speaks it in a single call — no separate GPT+TTS chain.

    Returns the PCM (int16 LE mono, ``output_rate`` Hz) or b"" on error/
    timeout/empty answer. Blocks the caller up to ``timeout_s``.
    """
    import websockets

    if not text or not text.strip():
        return b""
    key, folder = get_credentials()
    if not key or not folder:
        raise RuntimeError("YANDEX_API_KEY / YANDEX_FOLDER_ID are not set")
    url = _realtime_url(folder, model)
    pcm = bytearray()
    deadline = time.monotonic() + timeout_s
    t0 = time.monotonic()
    logger.info("[realtime] ask_audio: %d chars (voice=%s role=%r, "
                "timeout=%ss)", len(text), voice, role, timeout_s)
    try:
        ws = await websockets.connect(
            url,
            additional_headers={"Authorization": f"Api-Key {key}"},
            open_timeout=20.0,
            ping_interval=20.0,
            ping_timeout=20.0,
            max_size=2 ** 24,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[realtime] ask_audio connect error: %s", exc)
        return b""
    try:
        await ws.send(json.dumps(_session_payload(
            instructions=instructions,
            output_modalities=["audio"],
            input_rate=input_rate,
            output_rate=output_rate,
            language="ru-RU",
            voice=voice,
            role=role,
            turn_detection=False,
        ), ensure_ascii=False))
        await ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        }, ensure_ascii=False))
        await ws.send(json.dumps({"type": "response.create"}))
        answered = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning("[realtime] ask_audio timeout after %.1fs "
                               "(%d B collected)", time.monotonic() - t0,
                               len(pcm))
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                logger.warning("[realtime] ask_audio timeout after %.1fs "
                               "(%d B collected)", time.monotonic() - t0,
                               len(pcm))
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning("[realtime] ask_audio recv error: %s", exc)
                break
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            mtype = msg.get("type")
            if mtype == "response.output_audio.delta":
                try:
                    pcm += base64.b64decode(msg.get("delta", ""))
                except Exception:  # noqa: BLE001
                    pass
            elif mtype == "response.output_text.delta":
                logger.info("[realtime] ask_audio partial text: %s",
                            msg.get("delta", ""))
            elif mtype == "response.done":
                answered = True
                break
            elif mtype == "error":
                logger.error("[realtime] ask_audio server error: %s",
                             json.dumps(msg, ensure_ascii=False))
                break
    finally:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
    result = bytes(pcm)
    logger.info("[realtime] ask_audio done in %.1fs: %d B pcm "
                "(answered=%s)", time.monotonic() - t0, len(result), answered)
    return result if answered else b""