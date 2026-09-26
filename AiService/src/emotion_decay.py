"""Post-dialogue emotion decay: Neutral -> Sad -> Sleepy.

After a dialogue ends (the answer playback finished) the robot is left alone:
the server schedules an emotion countdown and at each stage:

1. sends the robot the emotion command (EMOTION:<name>);
2. asks the Yandex Speech Realtime model for a short phrase matching that
   emotion and synthesizes its speech in ONE call (the ``prompt`` from the
   ``emotion_decay`` config section; Processor.say_emotion);
3. plays the phrase back to the robot (binary PCM frames, the same pacing as
   the main answers);
4. right after the Sleepy phrase — plays the yawn WAV; a short while later
   (``snore_after_min``) — the snore WAV.

The delays are measured from the end of the dialogue and come from the
``emotion_decay`` config section (``neutral_min`` / ``sad_min`` /
``sleepy_min``, in MINUTES). Any recognized speech restarts the countdown
(``start()``); a new dialogue cancels it (``cancel()``). If the robot goes
offline mid-decay the countdown waits for the reconnect instead of aborting —
the face never stays stuck in an intermediate emotion. All Yandex calls are
wrapped in try/except — the server never crashes because of the decay.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, List, Optional, Tuple

from . import config as app_config

logger = logging.getLogger("uvicorn")

# Fallback WAV paths (relative to the AiService root); overridable in the
# yandex section of settings. The files are placed by the user already in the
# protocol format (PCM int16 LE, 16 kHz mono).
_DEFAULT_YAWN_FILE = "sounds/yawn.wav"
_DEFAULT_SNORE_FILE = "sounds/snore.wav"


def _load_pcm(path_str: str) -> bytes:
    """Reads a WAV as raw PCM int16 LE (already 16 kHz mono, no conversion).

    Returns b"" on any error (missing file / bad format) so the decay never
    crashes because of a sound file.
    """
    from pathlib import Path
    import wave

    try:
        path = Path(path_str)
        if not path.is_file():
            # Try relative to the AiService root.
            path = Path(__file__).resolve().parent.parent / path_str
        if not path.is_file():
            raise FileNotFoundError(path_str)
        with wave.open(str(path), "rb") as w:
            return w.readframes(w.getnframes())
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ai] sound: cannot load %s: %s", path_str, exc)
        return b""


class EmotionDecay:
    """Schedules the post-dialogue emotion decay for the robot."""

    # Emotion order and the config keys of their delays (MINUTES from the
    # dialogue end, see the "emotion_decay" section of settings).
    STAGES: Tuple[Tuple[str, str], ...] = (
        ("neutral", "neutral_min"),
        ("sad", "sad_min"),
        ("sleepy", "sleepy_min"),
    )

    # Fallback delays (minutes) when the config key is missing or zero. A
    # zero delay would start the decay instantly and the robot would speak
    # the decay phrase instead of the real answer.
    STAGE_DEFAULT_MIN = {
        "neutral_min": 0.17,   # ~10 s
        "sad_min": 0.5,        # ~30 s
        "sleepy_min": 0.75,    # ~45 s
    }

    def __init__(self, processor: Any, robot: Any,
                 send_audio: Callable[[bytes], Awaitable[bool]]) -> None:
        # Processor with say_emotion() (GPT phrase + TTS).
        self.processor = processor
        # RobotSession with send_emotion() (JSON emotion command).
        self.robot = robot
        # Sends playback PCM to the robot (binary frames with pacing).
        self.send_audio = send_audio
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        """Starts (or restarts) the decay countdown from the dialogue end."""
        self.cancel()
        delays: List[Tuple[str, int]] = []
        cfg = app_config.CONFIG.get("emotion_decay", {})
        for emotion, key in self.STAGES:
            minutes = float(cfg.get(key, 0)
                            or self.STAGE_DEFAULT_MIN.get(key, 0))
            if minutes <= 0:
                minutes = float(self.STAGE_DEFAULT_MIN.get(key, 0))
            delays.append((emotion, int(round(minutes * 60000))))
        # Sounds and timings come from the emotion_decay section (minutes).
        yawn_file = str(cfg.get("yawn_file", _DEFAULT_YAWN_FILE))
        snore_after_min = float(cfg.get("snore_after_min", 0.08))
        snore_after_ms = int(round(snore_after_min * 60000))
        if snore_after_ms < 0:
            snore_after_ms = 5000
        snore_file = str(cfg.get("snore_file", _DEFAULT_SNORE_FILE))
        logger.info("[ai] emotion decay scheduled: %s (yawn %s, snore "
                    "+%d ms %s)", delays, yawn_file, snore_after_ms,
                    snore_file)
        self._task = asyncio.create_task(
            self._run(delays, yawn_file, snore_after_ms, snore_file))

    def cancel(self) -> None:
        """Cancels the countdown (a new dialogue has begun)."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            logger.info("[ai] emotion decay cancelled (new dialogue)")
            self._task = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _wait_robot(self, what: str) -> None:
        """Waits until the robot is back online (no abort, no timeout).

        A temporary robot disconnect (Wi-Fi blip / reboot) must NOT stop the
        decay: otherwise the face stays stuck in the last emotion (e.g. Sad).
        The loop polls until the robot reconnects; a new ``start()`` /
        ``cancel()`` from a dialogue still interrupts the whole countdown.
        """
        if self.robot.connected:
            return
        logger.info("[ai] emotion decay: %s — robot offline, waiting for "
                    "reconnect", what)
        while not self.robot.connected:
            await asyncio.sleep(2.0)
        logger.info("[ai] emotion decay: robot reconnected — continuing %s",
                    what)

    async def _run(self, delays: List[Tuple[str, int]],
                   yawn_file: str = _DEFAULT_YAWN_FILE,
                   snore_after_ms: int = 5000,
                   snore_file: str = _DEFAULT_SNORE_FILE) -> None:
        t0 = time.monotonic()
        try:
            for emotion, at_ms in delays:
                wait = max(0.0, at_ms / 1000.0 - (time.monotonic() - t0))
                logger.info("[ai] emotion decay: %s in %.1f s", emotion,
                            wait)
                await asyncio.sleep(wait)
                # The stage runs only when the robot is online; an offline
                # robot just delays the stage (waits for the reconnect).
                await self._wait_robot(f"stage {emotion}")
                # Emotion command first (the face changes immediately), then
                # the GPT-generated phrase matching the emotion.
                emo_ok = await self.robot.send_emotion(emotion)
                logger.info("[ai] emotion decay -> %s (emotion %s)",
                            emotion, "ok" if emo_ok else "NO CONNECTION")
                pcm = await self.processor.say_emotion(emotion)
                if pcm:
                    ok = await self.send_audio(pcm)
                    logger.info("[ai] emotion decay: %s phrase sent (%d B "
                                "-> %s)", emotion, len(pcm),
                                "ok" if ok else "NO CONNECTION")
                else:
                    logger.info("[ai] emotion decay: %s phrase empty/skipped",
                                emotion)
                # Right after the Sleepy phrase — play the yawn.
                if emotion == "sleepy":
                    yawn = _load_pcm(yawn_file)
                    if yawn:
                        ok = await self.send_audio(yawn)
                        logger.info("[ai] emotion decay: yawn sent (%d B "
                                    "-> %s)", len(yawn),
                                    "ok" if ok else "NO CONNECTION")
                    else:
                        logger.info("[ai] emotion decay: yawn skipped "
                                    "(no WAV)")
            # Final stage: shortly after Sleepy, play the snore so the robot
            # really "falls asleep".
            snore_at_ms = delays[-1][1] + snore_after_ms
            wait = max(0.0, snore_at_ms / 1000.0 - (time.monotonic() - t0))
            logger.info("[ai] emotion decay: snore in %.1f s", wait)
            await asyncio.sleep(wait)
            await self._wait_robot("snore")
            snore = _load_pcm(snore_file)
            if snore:
                ok = await self.send_audio(snore)
                logger.info("[ai] emotion decay: snore sent (%d B -> %s)",
                            len(snore), "ok" if ok else "NO CONNECTION")
            else:
                logger.info("[ai] emotion decay: snore skipped (no WAV)")
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ai] emotion decay error: %s", exc)