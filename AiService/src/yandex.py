"""Yandex Cloud SpeechKit STT v3: streaming recognition of camera speech.

``StreamingRecognizer`` — streaming recognition: opens RecognizeStreaming on
``start()``, accepts PCM chunks via ``feed()`` as they arrive (data is streamed
and recognized immediately), and on ``finish()`` returns the assembled text —
the finalization at the moment the utterance ends (a pause in audio). The text
is assembled from final/final_refinement events; the last partial is a
fallback inside the stream (the phrase may be cut at a VAD segment boundary).

The gRPC stream lives in a separate thread, so the server asyncio loop is not
blocked.

Audio format — PCM int16 LE, 16 kHz mono (as the camera sends it and as
SpeechKit expects: LINEAR16_PCM).

Credentials: the ``YANDEX_API_KEY`` / ``YANDEX_FOLDER_ID`` environment
variables or the ``yandex`` section of ``config/settings.json`` (with ${VAR}
support). Dependencies (grpc, yandexcloud) are imported lazily.
"""
from __future__ import annotations

import logging
import os
import queue
import socket
import threading

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