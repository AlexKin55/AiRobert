"""Yandex Cloud integration: speech recognition, answer generation and TTS.

STT:
  ``StreamingRecognizer`` — streaming SpeechKit STT v3 recognition: opens
  RecognizeStreaming on ``start()``, accepts PCM chunks via ``feed()`` as they
  arrive (the data is streamed and recognized immediately), and on ``finish()``
  returns the assembled text — the finalization when the utterance ends (an
  audio pause). The text is assembled from final/final_refinement events; the
  last partial is a fallback inside the stream (a phrase may be cut at a VAD
  segment boundary).

GPT:
  ``ask_gpt()`` — YandexGPT v3 (REST foundationModels/v1/completion) with the
  system prompt from the ``yandex.system_prompt`` config; ``split_emotion()``
  extracts the trailing "Emotion: <name>" command from the answer.

TTS:
  ``synthesize()`` — SpeechKit TTS v3 (REST tts/v3/utteranceSynthesis) to PCM
  16 kHz/mono; voice/speed/role come from the ``yandex`` config section.
  ``ask_and_synthesize()`` — GPT answer + its speech in one call.

The gRPC stream lives in a separate thread, so the server asyncio loop is not
blocked.

Audio format — PCM int16 LE, 16 kHz mono (as the camera sends it and as
SpeechKit expects: LINEAR16_PCM).

Credentials: the ``YANDEX_API_KEY`` / ``YANDEX_FOLDER_ID`` environment
variables or the ``yandex`` section of ``config/settings.json`` (with ${VAR}
support). Dependencies (grpc, yandexcloud, requests) are imported lazily.
"""
from __future__ import annotations

import array
import base64
import logging
import os
import queue
import re
import socket
import struct
import threading
import time

logger = logging.getLogger("uvicorn")

# Protocol audio sample rate (STT), Hz.
STT_SAMPLE_RATE_HZ = 16000

# In this network the IPv6 addresses of Yandex Cloud do not respond, while
# requests/grpc take the FIRST address from getaddrinfo (that is IPv6) and
# hang until a timeout. Prefer IPv4 for api.cloud.yandex.net hosts.
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


def describe_stt_error(exc) -> str:
    """Human-readable STT error description: down/bad credentials/blocked."""
    try:
        import grpc
    except ImportError:
        return f"Yandex STT error: {exc}"
    code = getattr(exc, "code", None)
    if not callable(code):
        return f"Yandex STT error: {exc}"
    c = code()
    details = ""
    d = getattr(exc, "details", None)
    if callable(d):
        details = d() or ""
    mapping = {
        grpc.StatusCode.UNAVAILABLE:
            "Yandex STT UNAVAILABLE (no network/DNS)",
        grpc.StatusCode.DEADLINE_EXCEEDED:
            "Yandex STT: timeout (service did not respond)",
        grpc.StatusCode.UNAUTHENTICATED:
            "Yandex: INVALID CREDENTIALS (Unauthenticated) — wrong/revoked Api-Key",
        grpc.StatusCode.PERMISSION_DENIED:
            "Yandex: ACCESS DENIED (PermissionDenied/403) — check the folder and permissions",
        grpc.StatusCode.NOT_FOUND:
            "Yandex: resource not found (NotFound/404) — check folder_id",
        grpc.StatusCode.RESOURCE_EXHAUSTED:
            "Yandex: quota exceeded (ResourceExhausted/429)",
    }
    label = mapping.get(c, f"Yandex STT: error {c.name}")
    return f"{label} [{c.name}]: {details}".strip()


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


class StreamingRecognizer:
    """SpeechKit STT v3 streaming recognition — as PCM arrives.

    Opens RecognizeStreaming on start(), accepts chunks via feed() and
    returns the assembled text on finish(). The gRPC stream runs in a
    separate thread so the server asyncio loop is not blocked.
    """

    def __init__(self, language_code: str = "ru-RU",
                 sample_rate_hz: int = STT_SAMPLE_RATE_HZ) -> None:
        self._q: queue.Queue = queue.Queue(maxsize=256)
        self._texts: list[str] = []
        # Last partial: fallback if final never arrives.
        self._last_partial = ""
        # Partial generation counter: each new non-empty partial increments
        # it, drain_partial() reports only the ones not yet consumed (the
        # first recognized word of an utterance).
        self._partial_epoch = 0
        self._drained_epoch = 0
        self._err: Exception | None = None
        self._done = threading.Event()
        self._started = False
        self._language = language_code
        self._rate = sample_rate_hz

    def start(self) -> None:
        """Starts the recognition thread (gRPC stream to Yandex)."""
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._run, name="stt-stream",
                         daemon=True).start()

    def feed(self, pcm: bytes) -> None:
        """Sends the next PCM chunk into the stream (int16 LE, mono).

        Non-blocking insert: on queue overflow the oldest data is dropped
        (the fresh end of the phrase matters more than its beginning) so the
        asyncio loop is never frozen.
        """
        if self._started and not self._done.is_set() and pcm:
            while self._q.qsize() >= self._q.maxsize:
                self._q.get_nowait()
            self._q.put_nowait(bytes(pcm))

    def finish(self, timeout_s: float = 120.0) -> str:
        """Closes the stream and returns the recognized utterance text.

        Priority: final/final_refinement. If the server did not send them
        (the phrase was cut by VAD at the segment boundary), the last partial
        is returned.
        """
        self._q.put(None)  # sentinel: end of audio
        if not self._done.wait(timeout_s):
            logger.warning("STT stream did not finish within %ss", timeout_s)
        if self._err is not None:
            logger.error("%s", describe_stt_error(self._err))
        result = " ".join(t for t in self._texts if t).strip()
        if not result and self._last_partial:
            logger.info("STT: no final, using the last partial: %r",
                        self._last_partial)
            return self._last_partial
        return result

    def drain_partial(self) -> str:
        """Returns the latest unrecognized partial (the first words heard).

        The STT thread appends partials continuously; this method returns the
        newest one exactly once — until the next partial arrives it returns
        "". Safe to call from the asyncio loop (the fields are only written by
        the STT thread, read here).
        """
        if self._partial_epoch > self._drained_epoch:
            self._drained_epoch = self._partial_epoch
            return self._last_partial
        return ""

    def abort(self) -> None:
        """Closes the recognition stream without waiting for the result.

        Used when a segment hangs (stuck gRPC stream): the audio-queue
        sentinel makes the generator finish and the daemon thread exits on
        its own; nobody waits for final alternatives, so the asyncio loop is
        never blocked. Safe to call more than once.
        """
        try:
            if self._started and not self._done.is_set():
                self._q.put_nowait(None)
        except Exception:  # noqa: BLE001
            pass

    def _gen(self):
        from yandex.cloud.ai.stt.v3 import stt_pb2

        yield stt_pb2.StreamingRequest(
            session_options=stt_pb2.StreamingOptions(
                recognition_model=stt_pb2.RecognitionModelOptions(
                    audio_format=stt_pb2.AudioFormatOptions(
                        raw_audio=stt_pb2.RawAudio(
                            audio_encoding=stt_pb2.RawAudio.LINEAR16_PCM,
                            sample_rate_hertz=self._rate,
                            audio_channel_count=1,
                        ),
                    ),
                    text_normalization=stt_pb2.TextNormalizationOptions(
                        text_normalization=(
                            stt_pb2.TextNormalizationOptions
                            .TEXT_NORMALIZATION_ENABLED
                        ),
                        profanity_filter=False,
                        literature_text=False,
                    ),
                    language_restriction=stt_pb2.LanguageRestrictionOptions(
                        restriction_type=(
                            stt_pb2.LanguageRestrictionOptions.WHITELIST
                        ),
                        language_code=[self._language],
                    ),
                    audio_processing_type=(
                        stt_pb2.RecognitionModelOptions.REAL_TIME
                    ),
                ),
            ),
        )
        while True:
            item = self._q.get()
            if item is None:  # end of audio — close the stream
                return
            # Yandex accepts small AudioChunks (4000 B in the example); large
            # camera chunks are split before sending.
            for i in range(0, len(item), 4000):
                yield stt_pb2.StreamingRequest(
                    chunk=stt_pb2.AudioChunk(data=item[i:i + 4000]))

    def _run(self) -> None:
        import grpc
        from yandex.cloud.ai.stt.v3 import stt_service_pb2_grpc

        key, folder = get_credentials()
        if not key or not folder:
            self._err = RuntimeError(
                "YANDEX_API_KEY / YANDEX_FOLDER_ID are not set")
            self._done.set()
            return
        channel = grpc.secure_channel(
            "stt.api.cloud.yandex.net:443", grpc.ssl_channel_credentials())
        try:
            stub = stt_service_pb2_grpc.RecognizerStub(channel)
            metadata = (
                ("authorization", f"Api-Key {key}"),
                ("x-folder-id", folder),
            )
            for resp in stub.RecognizeStreaming(self._gen(),
                                                metadata=metadata):
                ev = resp.WhichOneof("Event")
                if ev == "status_code":
                    logger.debug("STT v3 status_code=%s: %s",
                                 resp.status_code.code_type,
                                 resp.status_code.message or "")
                    continue
                if ev == "partial":
                    if (resp.partial.alternatives
                            and resp.partial.alternatives[0].text):
                        self._last_partial = resp.partial.alternatives[0].text
                        self._partial_epoch += 1
                        logger.info("STT partial: %s", self._last_partial)
                    continue
                if ev == "final":
                    alts = resp.final.alternatives
                elif ev == "final_refinement":
                    alts = resp.final_refinement.normalized_text.alternatives
                else:
                    continue
                if alts and alts[0].text:
                    self._texts.append(alts[0].text)
        except grpc.RpcError as exc:
            self._err = exc
            logger.error("%s", describe_stt_error(exc))
        except Exception as exc:  # noqa: BLE001
            self._err = exc
            logger.error("Yandex STT streaming error: %s", exc)
        finally:
            channel.close()
            self._done.set()


# ---------------------------------------------------------------------------
# YandexGPT (answer generation).
# ---------------------------------------------------------------------------

def default_system_prompt() -> str:
    """Default system prompt (from the yandex.system_prompt config)."""
    try:
        from . import config as app_config
        return str(app_config.CONFIG.get("yandex", {}).get(
            "system_prompt", ""))
    except Exception:  # noqa: BLE001
        return ""


def default_emotion_decay_prompt() -> str:
    """Emotion-decay prompt (from the yandex.emotion_decay_prompt config):
    asks GPT to generate a short phrase matching the given emotion when the
    robot is left alone after a dialogue."""
    try:
        from . import config as app_config
        return str(app_config.CONFIG.get("yandex", {}).get(
            "emotion_decay_prompt", ""))
    except Exception:  # noqa: BLE001
        return ""


# Robot emotion names — the EMOTION:<name> command in the robot protocol.
# GPT appends them to the end of the answer after the "Emotion:" keyword
# (exactly in this form, untranslated).
ROBOT_EMOTIONS = ("neutral", "happy", "angry", "sad", "doubt", "sleepy",
                  "dancing")


def split_emotion(text: str) -> tuple[str, str | None]:
    """Extracts the emotion command from the end of the GPT answer.

    Following the prompt, GPT appends a line like "\n\nEmotion: Happy"
    (values limited to ROBOT_EMOTIONS) to the end of the answer — a command
    for the robot to show an emotion, not part of the speech: it must not
    reach TTS and is sent to the robot as a separate EMOTION:<name> text
    command.

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


def ask_gpt(user_text: str, system_prompt: str = "",
            temperature: float = 0.5, max_tokens: int = 1000) -> str:
    """Sends text to YandexGPT v3 and returns the answer (or "" on error).

    The model and timeout come from the config (yandex.gpt_model,
    yandex.gpt_timeout_s). Every stage is logged with a timestamp and duration
    so that a hung request can be localized.
    """
    import requests

    key, folder = get_credentials()
    if not key or not folder:
        raise RuntimeError("YANDEX_API_KEY / YANDEX_FOLDER_ID are not set")

    model = "yandexgpt/latest"
    timeout_s = 90.0
    try:
        from . import config as app_config
        y = app_config.CONFIG.get("yandex", {})
        model = str(y.get("gpt_model", model))
        timeout_s = float(y.get("gpt_timeout_s", timeout_s))
    except Exception:  # noqa: BLE001
        pass

    messages = []
    if system_prompt:
        messages.append({"role": "system", "text": system_prompt})
    messages.append({"role": "user", "text": user_text})

    payload = {
        "modelUri": f"gpt://{folder}/{model}",
        "completionOptions": {
            "stream": False,
            "temperature": temperature,
            "maxTokens": str(max_tokens),
        },
        "messages": messages,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Api-Key {key}",
    }
    url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

    t0 = time.monotonic()
    logger.info("GPT: POST %s model=%s text=%d chars (timeout=%ss)",
                url, model, len(user_text), timeout_s)
    try:
        resp = requests.post(url, json=payload, headers=headers,
                             timeout=(15, timeout_s))
        dt = time.monotonic() - t0
        logger.info("GPT: HTTP %d in %.1fs", resp.status_code, dt)
        resp.raise_for_status()
        data = resp.json()
        alternatives = data.get("result", {}).get("alternatives", [])
        if alternatives:
            text = alternatives[0]["message"]["text"]
            logger.info("YandexGPT: %.1fs, %d chars", time.monotonic() - t0,
                        len(text))
            return text
        logger.warning("YandexGPT: %.1fs, no alternatives (reply: %s)",
                       time.monotonic() - t0, str(data)[:300])
    except Exception as exc:  # noqa: BLE001
        body = ""
        status = ""
        r = getattr(exc, "response", None)
        if r is not None:
            status = r.status_code
            body = (r.text or "")[:300]
        logger.exception("YandexGPT error after %.1fs (HTTP=%s): %s%s%s",
                         time.monotonic() - t0, status, exc,
                         "; body: " if body else "", body)
    return ""


# ---------------------------------------------------------------------------
# SpeechKit TTS v3 (speech synthesis).
# ---------------------------------------------------------------------------

def _tts_settings() -> dict:
    """TTS parameters from the config (voice/speed/role)."""
    try:
        from . import config as app_config
        y = app_config.CONFIG.get("yandex", {})
        return {
            "voice": str(y.get("tts_voice", "zahar")),
            "speed": float(y.get("tts_speed", 1.00)),
            "role": str(y.get("tts_role", "friendly")),
        }
    except Exception:  # noqa: BLE001
        return {"voice": "zahar", "speed": 1.00, "role": "friendly"}


def _extract_pcm_wav(raw: bytes) -> tuple[bytes, int, int]:
    """Extracts PCM from WAV container(s); returns (pcm, rate, channels).

    SpeechKit TTS v3 returns audioChunk as WAV (RIFF/WAVE) with its own header
    carrying the real sample rate and channel count; a single response may
    contain several WAV sections in a row. If the buffer is not RIFF, it is
    treated as raw PCM (16 kHz/mono).
    """
    if len(raw) < 12 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        return raw, 16000, 1
    out = bytearray()
    rate = 0
    channels = 0
    pos = 0
    while pos + 12 <= len(raw):
        if raw[pos:pos + 4] != b"RIFF" or raw[pos + 8:pos + 12] != b"WAVE":
            break
        riff_size = struct.unpack_from("<I", raw, pos + 4)[0]
        end = min(pos + 8 + riff_size, len(raw))
        off = pos + 12
        got_data = False
        while off + 8 <= end:
            cid = raw[off:off + 4]
            size = struct.unpack_from("<I", raw, off + 4)[0]
            body = raw[off + 8:off + 8 + size]
            if cid == b"fmt " and len(body) >= 16:
                _, channels, rate, _, _, _ = struct.unpack_from(
                    "<HHIIHH", body, 0)
            elif cid == b"data":
                out += body
                got_data = True
            off += 8 + size + (size & 1)
        if not got_data:
            break
        pos = end
    return bytes(out), rate or 16000, channels or 1


def _resample_pcm(pcm: bytes, src_rate: int, dst_rate: int,
                  channels: int = 1) -> bytes:
    """Resamples int16 PCM to mono (linear interpolation).

    With stereo input the channels are first mixed down to mono; returns
    dst_rate PCM.
    """
    samples = array.array("h")
    samples.frombytes(pcm)
    if channels > 1:
        samples = array.array(
            "h", (samples[i] for i in range(0, len(samples), channels)))
    if src_rate == dst_rate:
        return samples.tobytes()
    ratio = src_rate / dst_rate
    n_out = round(len(samples) * dst_rate / src_rate)
    out = array.array("h")
    pos = 0.0
    for _ in range(n_out):
        idx = int(pos)
        frac = pos - idx
        a = samples[idx] if idx < len(samples) else samples[-1]
        b = samples[idx + 1] if idx + 1 < len(samples) else a
        out.append(int(a * (1.0 - frac) + b * frac))
        pos += ratio
    return out.tobytes()


def clean_for_speech(text: str) -> str:
    """Prepares text for speech synthesis: removes likely 400 triggers.

    YandexGPT answers with markdown (lists '*', bold '**', links [x](url),
    code '`', headings '#', quotes '>'), and SpeechKit TTS v3 may reject such
    text (400 Bad Request) or read the markup literally. SSML-like '<...>'
    (v3 parses angle brackets as markup), emojis, control characters and bare
    URLs are additionally stripped; the text is truncated to a safe length.
    Voice parameters are set via hints, not the text itself.
    """
    # markdown links [text](url) -> text, then bare URLs
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    # markup ** / __ / * / _ / ` (asterisks and underscores are not needed)
    text = re.sub(r"[*_`]", "", text)
    # headings "# ", "## " and quotes "> " at the start of lines
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)
    # list markers "- " / "+ " at the start of lines
    text = re.sub(r"^\s*[-+]\s+", "", text, flags=re.MULTILINE)
    # SSML-like tags <...> and remaining angle brackets
    text = re.sub(r"<[^>]*>", "", text)
    text = text.replace("<", " ").replace(">", " ")
    # emojis and pictographs (including variation selectors)
    text = re.sub(
        r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF"
        r"\uFE0F\u200D\u2190-\u21FF]",
        "", text)
    # control characters (except \n and \t)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    # excessive newlines and spaces
    text = re.sub(r"\n{2,}", "\n", text)
    text = re.sub(r" {2,}", " ", text)
    text = text.strip()
    # protection against excessive length (SpeechKit v3: up to ~5000 chars)
    if len(text) > 5000:
        text = text[:5000].rstrip()
    return text


def _tts_post(url: str, headers: dict, payload: dict) -> bytes:
    """Single SpeechKit TTS v3 utteranceSynthesis request -> PCM (16 kHz/mono).

    The response is an NDJSON stream where each chunk contains base64-WAV; the
    real PCM is extracted from the body and resampled to 16 kHz/mono if needed.
    On an HTTP error raises requests.HTTPError (the body is in
    exc.response.text).
    """
    import json

    import requests

    resp = requests.post(url, json=payload, headers=headers,
                         stream=True, timeout=120)
    resp.raise_for_status()
    raw = bytearray()
    for line in resp.iter_lines():
        if not line:
            continue
        chunk = json.loads(line)
        b64 = chunk.get("result", {}).get("audioChunk", {}).get("data")
        if b64:
            raw += base64.b64decode(b64)
    pcm, rate, channels = _extract_pcm_wav(bytes(raw))
    if rate != STT_SAMPLE_RATE_HZ or channels != 1:
        pcm = _resample_pcm(pcm, rate, STT_SAMPLE_RATE_HZ, channels)
    return pcm


def _split_half(text: str) -> tuple[str, str]:
    """Splits text into two non-empty parts at a boundary near the middle.

    Looks for punctuation (sentence/half-phrase) near the middle; if there is
    none, cuts at a space, otherwise in half (safe for recursion).
    """
    mid = len(text) // 2
    cut = mid
    bounds = []
    for sep in ".!?;…:,—":
        i = text.rfind(sep, 0, mid)
        if i != -1:
            bounds.append(i + 1)
        i = text.find(sep, mid)
        if i != -1:
            bounds.append(i + 1)
    if bounds:
        cut = min(bounds, key=lambda i: abs(i - mid))
    else:
        space = text.rfind(" ", 0, mid)
        if space > 0:
            cut = space + 1
    left, right = text[:cut].strip(), text[cut:].strip()
    if not left or not right:
        cut = max(mid, 1)
        left, right = text[:cut].strip(), text[cut:].strip()
    return left, right


def _synthesize_part(text: str, url: str, headers: dict,
                     voice: str, speed: float, role: str,
                     sample_rate_hz: int, depth: int = 0) -> bytes:
    """Synthesizes a part of the text; on 400 'Too long text' splits in half.

    The per-request text limit of SpeechKit TTS v3 can be much smaller than
    the official 5000 characters (on some plans 'Too long text' already fires
    at ~260 characters), so on such a 400 the text is recursively split in
    half and each half is synthesized separately, then the PCM is joined.
    """
    from requests import HTTPError

    payload = {
        "text": text,
        "outputAudioSpec": {
            "rawData": {
                "audioEncoding": "LINEAR16_PCM",
                "sampleRateHertz": sample_rate_hz,
            }
        },
        "hints": [
            {"voice": voice},
            {"speed": speed},
            {"role": role},
        ],
    }
    try:
        t0 = time.monotonic()
        pcm = _tts_post(url, headers, payload)
        logger.info("TTS part(%d): %.1fs, %d B pcm", depth,
                    time.monotonic() - t0, len(pcm))
        return pcm
    except HTTPError as exc:
        body = ""
        try:
            body = getattr(exc.response, "text", "") or ""  # noqa: BLE001
        except Exception:  # noqa: BLE001
            pass
        if "too long" not in body.lower():
            raise
        if len(text) <= 8:
            # Even a minimal chunk was rejected — it is no longer about length.
            logger.exception("Yandex TTS: short text rejected: %r (body: %s)",
                             text, body[:400])
            raise
        left, right = _split_half(text)
        logger.warning("Yandex TTS: 'Too long text' (%d chars) — splitting: "
                       "%d + %d", len(text), len(left), len(right))
        out = _synthesize_part(left, url, headers, voice, speed, role,
                               sample_rate_hz, depth + 1)
        out += _synthesize_part(right, url, headers, voice, speed, role,
                                sample_rate_hz, depth + 1)
        return out


def synthesize(text: str, sample_rate_hz: int = 16000,
               voice: str | None = None,
               speed: float | None = None,
               role: str | None = None) -> bytes:
    """Speech synthesis to PCM (16 kHz/mono/16-bit) via SpeechKit TTS v3.

    URL: https://tts.api.cloud.yandex.net/tts/v3/utteranceSynthesis
    The text is cleaned of markdown/special characters (clean_for_speech); on
    400 'Too long text' it is split in half and synthesized in parts (see
    _synthesize_part). Returns PCM bytes (or b"" on error/empty text).
    """
    if not text or not text.strip():
        return b""
    text = clean_for_speech(text)
    if not text:
        return b""
    key, folder = get_credentials()
    if not key or not folder:
        raise RuntimeError("YANDEX_API_KEY / YANDEX_FOLDER_ID are not set")

    url = "https://tts.api.cloud.yandex.net:443/tts/v3/utteranceSynthesis"
    headers = {
        "Authorization": f"Api-Key {key}",
        "x-folder-id": folder,
        "Content-Type": "application/json",
    }
    s = _tts_settings()
    speed_eff = float(speed if speed else s["speed"])
    voice_eff = voice if voice else s["voice"]
    role_eff = role if role else s["role"]
    logger.info("TTS: voice=%s speed=%.2f role=%s (%d chars)",
                voice_eff, speed_eff, role_eff, len(text))

    t0 = time.monotonic()
    try:
        pcm = _synthesize_part(text, url, headers, voice_eff, speed_eff,
                               role_eff, sample_rate_hz)
    except Exception as exc:  # noqa: BLE001
        body = ""
        try:
            body = getattr(getattr(exc, "response", None), "text",
                           "")[:800]  # noqa: BLE001
        except Exception:  # noqa: BLE001
            pass
        logger.exception("Yandex TTS error after %.1fs: %s%s%s",
                         time.monotonic() - t0, exc,
                         "\nbody: " if body else "", body)
        return b""
    logger.info("Yandex TTS: %.1fs, %d B pcm (16k mono)",
                time.monotonic() - t0, len(pcm))
    return pcm


def ask_and_synthesize(user_text: str, system_prompt: str | None = None,
                       temperature: float = 0.5, max_tokens: int = 1000
                       ) -> tuple[str, bytes]:
    """Single step: user text + prompt -> (answer text, synthesized PCM).

    1. Sends the text to YandexGPT with the system prompt (from the
       yandex.system_prompt config when not given);
    2. Synthesizes the answer via SpeechKit TTS v3 (voice/speed/role from the
       config).

    Returns (answer, pcm); pcm may be b"" on error/empty. The answer keeps the
    trailing "Emotion: <name>" line — use split_emotion() before TTS.
    """
    if system_prompt is None:
        system_prompt = default_system_prompt()
    answer = ask_gpt(user_text, system_prompt=system_prompt,
                     temperature=temperature, max_tokens=max_tokens)
    if not answer:
        return "", b""
    pcm = synthesize(answer)
    return answer, pcm