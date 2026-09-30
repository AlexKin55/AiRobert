"""Emotion decay on the camera: Neutral -> Sad -> Sleepy (+ yawn, + snore).

Timers run on the CAMERA (the cloud may be unreachable): at each stage the
emotion is switched on the robot locally and the cloud is asked to synthesize
the phrase ({"type":"decay","emotion":...}); the answer arrives as a play
message with the ``decay`` marker. If the cloud is offline or silent within
``timeout_s`` — the emotion stays switched, the audio is skipped. After
Sleepy the camera plays its local yawn/snore WAVs. User speech (VAD) or a new
dialogue restarts the countdown.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Optional, Tuple

log = logging.getLogger('camera.decay')


def load_pcm(path: str) -> bytes:
    """Reads a WAV file as raw PCM int16 LE (16 kHz mono assumed).

    Returns b'' on any error (missing file / bad format) so the decay never
    crashes because of a sound file.
    """
    import wave

    try:
        with wave.open(path, 'rb') as w:
            return w.readframes(w.getnframes())
    except Exception as exc:  # noqa: BLE001
        log.warning('[decay] cannot load %s: %s', path, exc)
        return b''


class EmotionDecay:
    """Background emotion-decay scheduler running on the camera."""

    # (emotion, config key of the delay in MINUTES from the decay start).
    STAGES: Tuple[Tuple[str, str], ...] = (
        ('neutral', 'neutral_min'),
        ('sad', 'sad_min'),
        ('sleepy', 'sleepy_min'),
    )

    # Fallback delays (minutes) when a config key is missing or zero.
    STAGE_DEFAULT_MIN = {
        'neutral_min': 0.17,   # ~10 s (test-friendly defaults)
        'sad_min': 0.5,        # ~30 s
        'sleepy_min': 0.75,    # ~45 s
    }

    def __init__(self, robot, client, play_pcm: Callable[[bytes], bool],
                 cfg: Optional[dict] = None):
        self.robot = robot       # RobotServer (local WS to the robot)
        self.client = client     # WsClient (to AiService, for phrase requests)
        self.play_pcm = play_pcm  # fn(pcm) -> bool: plays PCM on the robot
        self.cfg = cfg or {}
        self.timeout_s = float(self.cfg.get('timeout_s', 25.0))
        self._thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._answer = threading.Event()
        self._lock = threading.Lock()
        self._active = False
        self._stage = ''         # current awaited stage ('', neutral, sad...)

    def start(self) -> None:
        """Starts (or restarts) the decay countdown from now."""
        with self._lock:
            self._cancel.set()
            cancel_ev = threading.Event()
            self._cancel = cancel_ev
            self._active = True
            self._stage = ''
        t = threading.Thread(target=self._run, args=(cancel_ev,),
                             daemon=True, name='EmotionDecay')
        self._thread = t
        try:
            t.start()
        except RuntimeError as exc:
            with self._lock:
                self._active = False
            log.error('[decay] cannot start thread: %s', exc)

    def cancel(self) -> None:
        """Cancels the countdown (a new dialogue has begun)."""
        with self._lock:
            self._cancel.set()
            self._active = False
            self._stage = ''

    def matches_stage(self, emotion: str) -> bool:
        """True if a decay phrase answer for ``emotion`` is still relevant."""
        with self._lock:
            return self._active and self._stage == emotion

    def notify_answer(self) -> None:
        """Called by the state machine when the cloud answered a phrase."""
        self._answer.set()

    # ------------------------------------------------------------------
    def _delays(self) -> list:
        delays = []
        for emotion, key in self.STAGES:
            minutes = float(self.cfg.get(key, 0)
                            or self.STAGE_DEFAULT_MIN.get(key, 0))
            if minutes <= 0:
                minutes = float(self.STAGE_DEFAULT_MIN.get(key, 0))
            delays.append((emotion, int(round(minutes * 60000))))
        return delays

    def _run(self, ev: threading.Event) -> None:
        t0 = time.monotonic()
        try:
            delays = self._delays()
            snore_after_ms = int(round(float(
                self.cfg.get('snore_after_min', 0.08)) * 60000))
            if snore_after_ms < 0:
                snore_after_ms = 5000
            for emotion, at_ms in delays:
                if ev.is_set():
                    return
                wait = max(0.0, at_ms / 1000.0 - (time.monotonic() - t0))
                log.info('[decay] stage %s in %.1f s', emotion, wait)
                if ev.wait(wait):
                    return
                self._stage_phrase(emotion, ev)
                if ev.is_set():
                    return
            # Final stage: shortly after Sleepy — the local snore WAV.
            snore_at_ms = delays[-1][1] + snore_after_ms
            wait = max(0.0, snore_at_ms / 1000.0 - (time.monotonic() - t0))
            log.info('[decay] snore in %.1f s', wait)
            if ev.wait(wait):
                return
            self._play_local(self.cfg.get('snore_file', 'sounds/snore.wav'))
        except Exception as exc:  # noqa: BLE001
            log.warning('[decay] error: %s', exc)
        finally:
            with self._lock:
                self._active = False
                self._stage = ''

    def _stage_phrase(self, emotion: str, ev: threading.Event) -> None:
        """One decay stage: local emotion + cloud phrase (best effort)."""
        # 1) Switch the robot emotion LOCALLY — works without the cloud.
        if self.robot is not None and self.robot.connected:
            ok = self.robot.send_text(json.dumps(
                {'type': 'emotion', 'name': emotion}))
            log.info('[decay] emotion %s -> robot (%s)', emotion,
                     'ok' if ok else 'NO CONNECTION')
        with self._lock:
            if self._active:
                self._stage = emotion
        # 2) Ask the cloud for the phrase. If the request cannot be sent —
        # the emotion is already set, we just skip the audio.
        sent = bool(self.client) and self.client.send_decay_request(emotion)
        if not sent:
            log.warning('[decay] cloud offline: emotion %s set WITHOUT '
                        'phrase', emotion)
            return
        # 3) Wait for the answer up to timeout_s. No answer — proceed to the
        # next stage with the emotion only (the camera never blocks forever).
        self._answer.clear()
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            if ev.is_set() or self._answer.is_set():
                return
            time.sleep(0.1)
        log.warning('[decay] no phrase answer for %s within %.0f s — '
                    'emotion only', emotion, self.timeout_s)
        # 4) Right after the Sleepy phrase — the local yawn WAV.
        if emotion == 'sleepy':
            self._play_local(self.cfg.get('yawn_file', 'sounds/yawn.wav'))

    def _play_local(self, path: str) -> None:
        pcm = load_pcm(path)
        if not pcm:
            log.info('[decay] local sound skipped (no WAV): %s', path)
            return
        try:
            ok = self.play_pcm(pcm)
            log.info('[decay] local sound %s sent (%d B -> %s)', path,
                     len(pcm), 'ok' if ok else 'NO CONNECTION')
        except Exception as exc:  # noqa: BLE001
            log.warning('[decay] local sound play error: %s', exc)