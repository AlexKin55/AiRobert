"""Adaptive voice activity detector (VAD) for the camera firmware.

The logic comes from ``AiCamera/example/vad.py`` and is adapted for streaming
over WebSocket: instead of writing a file, ``feed()`` returns the PCM chunk
that should be sent to the server, or None (silence — do not send).

How it works:
- RMS (loudness) is computed for every chunk;
- dynamic threshold: ``max(noise_floor * multiplier, min_threshold)``;
- while there is no speech, the background level adapts exponentially
  (``noise = (1-alpha)*noise + alpha*rms``) and a ring pre-record buffer is
  filled (so the first word is not cut off);
- when RMS exceeds the threshold, a segment starts: pre-record + current
  chunk are returned, then all chunks (including short pauses) pass through;
- if silence lasts longer than ``silence_seconds``, the segment ends and
  following silence chunks are dropped until the threshold is exceeded again.

No numpy: RMS uses ``array('h')`` (int16), Python 3.8 compatible.
"""
from __future__ import annotations

import array as _array
import logging
import math
import time
from collections import deque

log = logging.getLogger('camera.vad')


class AdaptiveVad:
    """Adaptive VAD for mono PCM S16_LE (fixed-length chunks)."""

    def __init__(self, sample_rate=16000, chunk_seconds=0.5,
                 alpha=0.05, min_threshold=400.0, multiplier=1.6,
                 silence_seconds=2.0, pre_record_seconds=0.4,
                 active_multiplier=1.05, active_min_threshold=20.0,
                 calibration_seconds=3.0):
        self.sample_rate = int(sample_rate)
        self.chunk_seconds = float(chunk_seconds)
        self.alpha = float(alpha)
        self.min_threshold = float(min_threshold)
        self.multiplier = float(multiplier)
        # Threshold WHILE a speech segment is active: lowered to the noise
        # floor so a quiet dialogue continuation (after a loud "Robert")
        # is recorded.
        self.active_multiplier = float(active_multiplier)
        self.active_min_threshold = float(active_min_threshold)
        self.silence_seconds = float(silence_seconds)
        # Pre-record chunk count: pre_record_seconds / chunk_seconds (>= 1).
        self.pre_record_chunks = max(
            1, int(round(float(pre_record_seconds) / self.chunk_seconds)))
        # Startup calibration: for the first calibration_seconds the noise
        # floor quickly catches up with the real background so room noise does
        # not activate VAD and keep the segment open forever.
        self._calib_left = max(
            1, int(math.ceil(float(calibration_seconds) / self.chunk_seconds)))
        # Initial noise floor — the minimum right away; adapts quickly.
        self.noise_floor = float(min_threshold)
        self.active = False          # is a speech segment running
        self._pre = deque(maxlen=self.pre_record_chunks)
        self._silence_start = None   # monotonic time of the current pause
        # Statistics.
        self.segments = 0            # started/finished speech segments
        self.voice_bytes = 0         # PCM bytes passed out (speech + tails)

    # --- RMS ------------------------------------------------------------
    @staticmethod
    def _rms(data):
        """RMS loudness of a mono int16 chunk (0..~32768)."""
        samples = _array.array('h')
        samples.frombytes(data)
        if not samples:
            return 0.0
        total = 0
        for x in samples:
            total += x * x
        return math.sqrt(total / len(samples))

    def _update_noise(self, rms, fast=False):
        """Adapts the background level, never going below min_threshold.

        - Floor: without it the noise floor sags on digital silence (rms ~ 0)
          and ends up below the real background — VAD then treats constant
          noise as speech and the segment never closes.
        - Fast catch-up: if the measured level is above our background
          estimate, update 8x faster (fast=True during calibration) so the
          threshold quickly rises above room noise.
        """
        if fast or rms > self.noise_floor:
            a = min(0.4, self.alpha * 8)
        else:
            a = self.alpha
        self.noise_floor = max(
            self.min_threshold,
            (1.0 - a) * self.noise_floor + a * rms)

    # --- main entry -------------------------------------------------------
    def feed(self, chunk):
        """Accepts a PCM chunk; returns bytes to send or None.

        None means "silence — do not send". When a segment starts, the
        pre-record + current chunk are returned.
        """
        rms = self._rms(chunk)

        # Startup calibration: estimate the real background without activating.
        if self._calib_left > 0:
            self._calib_left -= 1
            self._update_noise(rms, fast=True)
            self._pre.append(chunk)
            log.debug('VAD: calibration %d, noise=%.0f',
                      self._calib_left, self.noise_floor)
            return None

        if self.active:
            # Segment is running: the threshold is lowered to the noise floor
            # so a quiet continuation of speech is not cut off.
            threshold = max(self.noise_floor * self.active_multiplier,
                            self.active_min_threshold)
        else:
            # Waiting for a trigger: a noticeably louder phrase is needed
            # (e.g. "Robert") so every rustle does not trigger recording.
            threshold = max(self.noise_floor * self.multiplier,
                            self.min_threshold)
        log.debug('VAD: rms=%.0f noise=%.0f threshold=%.0f active=%s -> %s',
                  rms, self.noise_floor, threshold, self.active,
                  'speech' if rms > threshold else 'silence')

        if rms > threshold:
            # --- speech ---
            if not self.active:
                self.active = True
                self._silence_start = None
                self.segments += 1
                out = b''.join(self._pre) + chunk
                self._pre.clear()
                self.voice_bytes += len(out)
                return out
            self._silence_start = None
            self.voice_bytes += len(chunk)
            return chunk

        # --- silence/background noise ---
        if not self.active:
            # Adapt the background level and fill the pre-record buffer.
            self._update_noise(rms)
            self._pre.append(chunk)
            return None

        # Active segment but a pause: send the "tail" up to silence_seconds.
        if self._silence_start is None:
            self._silence_start = time.monotonic()
        if time.monotonic() - self._silence_start >= self.silence_seconds:
            # The pause is too long — the segment is over.
            self.active = False
            self._silence_start = None
            self._pre.clear()
            return None
        # A pause inside speech — also pull the noise floor toward the real
        # background so it does not stay below the room noise level.
        self._update_noise(rms)
        self.voice_bytes += len(chunk)
        return chunk