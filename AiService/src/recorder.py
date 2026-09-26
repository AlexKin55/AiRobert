"""Recorder: debug saving of the incoming/outgoing camera media.

Files are playable/viewable with standard tools (valid WAV headers, raw JPEG):

  * incoming camera audio  -> <reception_time>_in.wav
  * outgoing robot audio   -> <time_of_first_chunk>_out.wav
  * incoming pictures      -> <reception_time>_img.jpg

Filenames start with the reception time (microsecond precision):
    records/20260923_084712_123456_in.wav
    records/20260923_084900_000001_out.wav
    records/20260923_084713_654321_img.jpg
"""
from __future__ import annotations

import logging
import struct
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("uvicorn")

_WAV_HEADER = struct.Struct("<4sI4s4sIHHIIHH4sI")


def _header(data_size: int, sample_rate: int) -> bytes:
    byte_rate = sample_rate * 2  # 16-bit mono
    return _WAV_HEADER.pack(
        b"RIFF", 36 + data_size, b"WAVE",
        b"fmt ", 16, 1, 1, sample_rate, byte_rate, 2, 16,
        b"data", data_size)


def stamp_now() -> str:
    """Reception timestamp for filenames: YYYYMMDD_HHMMSS_ffffff.

    Uses datetime.strftime: %f (microseconds) is not supported by
    time.strftime on some platforms.
    """
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


class _AudioWriter:
    """Streaming WAV writer: appends PCM, fixes the header on close."""

    def __init__(self, path: Path, sample_rate: int) -> None:
        self.path = path
        self.f = open(path, "wb")
        self.f.write(b"\x00" * 44)  # placeholder header, rewritten on close
        self.bytes = 0
        self.sample_rate = sample_rate

    def write(self, pcm: bytes) -> None:
        self.f.write(pcm)
        self.bytes += len(pcm)

    def close(self) -> None:
        try:
            self.f.seek(0)
            self.f.write(_header(self.bytes, self.sample_rate))
        finally:
            self.f.close()
        logger.info("[recorder] wav saved: %s (%d B, %.2f s)",
                    self.path, self.bytes, self.bytes / (2 * self.sample_rate))


class Recorder:
    """Saves incoming/outgoing audio (.wav), pictures (.jpg) and AI debug data.

    * _in.wav: streamed camera audio; one file per segment. If
      rotate_seconds > 0, a new file starts every N seconds of audio
      (0 = one file per camera session).
    * _out.wav: playback sent to the robot (one file per send_robot_audio
      call, closed after the EOF marker).
    * _tts.wav: answer PCM from the Yandex Realtime session (one file per
      answer).
    * _img.jpg: one file per picture.
    * _prompt.txt: model exchange (system prompt + question + answer text).
    """

    def __init__(self, record_dir: str, sample_rate: int = 16000,
                 save_audio: bool = True, save_images: bool = True,
                 save_tts_audio: bool = True, save_prompts: bool = True,
                 rotate_seconds: float = 0.0) -> None:
        self.record_dir = Path(record_dir)
        self.sample_rate = sample_rate
        self.save_audio = bool(save_audio)
        self.save_images = bool(save_images)
        # NOTE: the flag is named save_tts (not save_tts_audio) so it does not
        # shadow the save_tts_audio() method (an instance attribute would
        # override the class method -> "'bool' object is not callable").
        self.save_tts = bool(save_tts_audio)
        self.save_prompts = bool(save_prompts)
        self.rotate_seconds = max(0.0, float(rotate_seconds))
        self._audio_in: Optional[_AudioWriter] = None
        self._audio_out: Optional[_AudioWriter] = None
        self._audio_in_started_at = 0.0
        # Last opened/closed _in.wav — the STT text is written next to it.
        self.last_in_path: Optional[Path] = None
        self.audio_in_files = 0
        self.audio_out_files = 0
        self.image_files = 0
        self.transcript_files = 0
        self.tts_audio_files = 0
        self.prompt_files = 0

    # ------------------------------------------------------------------
    # Incoming camera audio (_in.wav).
    # ------------------------------------------------------------------
    def feed_audio(self, pcm: bytes) -> None:
        """Appends a camera PCM chunk to the current _in.wav."""
        if not self.save_audio:
            return
        if self._audio_in is None:
            self._start_wav("_in.wav")
        if self._audio_in is not None:
            self._audio_in.write(pcm)
            logger.info("[recorder] _in.wav: wrote +%d B (file %s, "
                        "total %d B)", len(pcm),
                        self._audio_in.path.name, self._audio_in.bytes)
            if self.rotate_seconds > 0 and \
                    time.time() - self._audio_in_started_at >= self.rotate_seconds:
                self.close_audio()

    def close_audio(self) -> bool:
        """Closes the current _in.wav (rotation / camera disconnect).

        Returns True if a file was actually closed (finalized).
        """
        if self._audio_in is not None:
            self._audio_in.close()
            self._audio_in = None
            return True
        return False

    def save_transcript(self, text: str) -> Optional[Path]:
        """Saves the recognized question transcript next to the last _in.wav:
        <stamp>_in.txt.

        Returns the file path or None (empty text / no audio file).
        """
        if not text or self.last_in_path is None:
            return None
        txt_path = self.last_in_path.with_suffix(".txt")
        try:
            txt_path.write_text(text.strip() + "\n", encoding="utf-8")
            self.transcript_files += 1
            logger.info("[recorder] transcript saved: %s", txt_path)
            return txt_path
        except OSError as exc:
            logger.warning("[recorder] failed to save transcript: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Outgoing robot audio (_out.wav).
    # ------------------------------------------------------------------
    def start_out(self) -> None:
        """Starts an _out.wav for a playback segment (first chunk)."""
        if not self.save_audio:
            return
        if self._audio_out is None:
            self._start_wav("_out.wav", out=True)

    def feed_out_audio(self, pcm: bytes) -> None:
        """Appends a playback PCM chunk to the current _out.wav."""
        if not self.save_audio:
            return
        if self._audio_out is None:
            self.start_out()
        if self._audio_out is not None:
            self._audio_out.write(pcm)

    def close_out(self) -> None:
        """Closes the current _out.wav (after the EOF marker)."""
        if self._audio_out is not None:
            self._audio_out.close()
            self._audio_out = None

    def _start_wav(self, suffix: str, out: bool = False) -> None:
        try:
            self.record_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("[recorder] cannot create %s: %s",
                           self.record_dir, exc)
            return
        path = self.record_dir / f"{stamp_now()}{suffix}"
        writer = _AudioWriter(path, self.sample_rate)
        if out:
            self._audio_out = writer
            self.audio_out_files += 1
        else:
            self._audio_in = writer
            self._audio_in_started_at = time.time()
            self.last_in_path = path
            self.audio_in_files += 1
        logger.info("[recorder] wav started: %s", path)

    # ------------------------------------------------------------------
    # AI debug: answer audio (_tts.wav) and model prompt log.
    # ------------------------------------------------------------------
    def save_tts_audio(self, pcm: bytes) -> Optional[Path]:
        """Saves the Realtime answer PCM as <stamp>_tts.wav (debug).

        Controlled by the recording.save_tts_audio setting.
        """
        if not self.save_tts or not pcm:
            return None
        try:
            self.record_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("[recorder] cannot create %s: %s",
                           self.record_dir, exc)
            return None
        path = self.record_dir / f"{stamp_now()}_tts.wav"
        try:
            writer = _AudioWriter(path, self.sample_rate)
            writer.write(pcm)
            writer.close()
            self.tts_audio_files += 1
            return path
        except OSError as exc:
            logger.warning("[recorder] failed to save TTS audio: %s", exc)
            return None

    def save_prompt_log(self, prompt: str, user_text: str,
                        answer: str) -> Optional[Path]:
        """Saves the model exchange to <stamp>_prompt.txt (debug).

        Controlled by the recording.save_prompts setting.
        """
        if not self.save_prompts:
            return None
        if not user_text or not answer:
            return None
        try:
            self.record_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("[recorder] cannot create %s: %s",
                           self.record_dir, exc)
            return None
        path = self.record_dir / f"{stamp_now()}_prompt.txt"
        body = f"[System prompt]\n{prompt}\n\n[User]\n{user_text}\n\n" \
               f"[Answer]\n{answer}\n"
        try:
            path.write_text(body, encoding="utf-8")
            self.prompt_files += 1
            logger.info("[recorder] prompt log saved: %s", path)
            return path
        except OSError as exc:
            logger.warning("[recorder] failed to save prompt log: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Pictures (_img.jpg).
    # ------------------------------------------------------------------
    def save_image(self, jpeg: bytes) -> Optional[Path]:
        """Saves a JPEG picture; filename starts with the reception time."""
        if not self.save_images:
            return None
        try:
            self.record_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("[recorder] cannot create %s: %s",
                           self.record_dir, exc)
            return None
        path = self.record_dir / f"{stamp_now()}_img.jpg"
        try:
            path.write_bytes(jpeg)
            self.image_files += 1
            logger.info("[recorder] image saved: %s (%d B)", path, len(jpeg))
            return path
        except OSError as exc:
            logger.warning("[recorder] failed to save image: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Session.
    # ------------------------------------------------------------------
    def stop(self) -> None:
        """Closes any open .wav (camera disconnected / server shutdown)."""
        self.close_audio()
        self.close_out()