"""Camera session orchestrator (JSON protocol to AiService /camera).

- ``WsClient`` — transport: sends ``audio``/``image``/``face``/``emotion``,
  receives server commands;
- ``Audio`` — UnitV2 microphone capture via ``arecord`` and PCM sending;
- ``Camera`` — snapshots on the ``capture`` command;
- command dispatcher: ``mute``/``unmute`` (microphone), ``capture`` (snapshot).
"""
from __future__ import annotations

import logging
import time

from .audio import Audio
from .camera import Camera
from .wsclient import WsClient

log = logging.getLogger('camera.state_machine')


class StateMachine:
    """Camera session: WsClient + Audio + Camera + server command dispatcher."""

    def __init__(self, url, sample_rate=16000, chunk_seconds=0.5,
                 audio_device=None, audio_channels=1, audio_sudo=None,
                 record_seconds=10, reconnect_delay=3.0,
                 record_restart_delay=3.0, vad_config=None, face_detect=None,
                 resolution = (640, 480), max_radius = 60, lost_timeout = 1.5):
        self.url = url
        self.sample_rate = sample_rate
        self.chunk_seconds = chunk_seconds
        self.audio_device = audio_device
        self.audio_channels = audio_channels
        self.audio_sudo = audio_sudo
        self.record_seconds = record_seconds
        self.reconnect_delay = reconnect_delay
        self.record_restart_delay = record_restart_delay
        self.vad_config = vad_config
        self.face_detect = face_detect
        self.client = None
        self.audio = None
        self.camera = None

    # --- server command dispatcher -------------------------------------
    def _on_command(self, msg):
        mtype = msg.get('type')
        if mtype == 'mute':
            self.audio.set_muted(True)
            log.info('Server command: microphone muted')
        elif mtype == 'unmute':
            self.audio.set_muted(False)
            log.info('Server command: microphone unmuted')
        elif mtype == 'capture':
            n = self.camera.capture()
            log.info('Server command: snapshot sent, %d bytes', n)
        else:
            log.warning('Unknown server command: %s', mtype)

    # --- session start ---------------------------------------------------
    def start(self):
        """Starts WsClient (auto-reconnect), Audio (arecord) and Camera.

        Does not block when the server or the microphone is unavailable:
        WsClient reconnects in a background thread, arecord is restarted
        after record_restart_delay on a device error.
        """
        self.client = WsClient(self.url, on_command=self._on_command,
                               sample_rate=self.sample_rate,
                               reconnect_delay=self.reconnect_delay)
        log.info('WsClient started (auto-reconnect), URL: %s', self.url)
        self.client.start()
        self.audio = Audio(
            self.client,
            sample_rate=self.sample_rate,
            chunk_seconds=self.chunk_seconds,
            channels=self.audio_channels,
            device=self.audio_device,
            sudo=self.audio_sudo,
            restart_delay=self.record_restart_delay,
            vad_config=self.vad_config,
        )
        self.audio.start()
        self.camera = Camera(self.client, self.face_detect)
        return self

    def run(self, seconds=None):
        """Runs (recording is already active) for seconds (None -> record_seconds).

        With seconds=0 it runs until stopped; stop() is called in finally.
        """
        if seconds is None:
            seconds = self.record_seconds
        log.info('Recording started ...')
        try:
            if seconds > 0:
                time.sleep(seconds)
            else:
                while True:
                    time.sleep(1.0)
        finally:
            self.stop()

    def stop(self):
        """Stops recording and the connection cleanly."""
        if self.client is not None:
            time.sleep(0.3)
            self.audio.stop()
            self.client.close()
        log.info('Total: chunks %d, PCM bytes %d, errors %d, snapshots %d',
                 self.audio.chunks_sent, self.audio.bytes_sent,
                 self.audio.errors, self.camera.frames_sent)

    def summary(self):
        """Session summary statistics."""
        seconds = self.audio.bytes_sent / (2 * self.sample_rate)
        print('Session summary:')
        print('  chunks sent:', self.audio.chunks_sent)
        print('  PCM bytes sent:', self.audio.bytes_sent)
        print('  about %.1f s of audio' % seconds)
        print('  snapshots sent:', self.camera.frames_sent)
        print('  errors:', self.audio.errors)
        print('  arecord restarts:', self.audio.attempts)
        if self.audio.vad is not None:
            vad = self.audio.vad
            print('  speech segments (VAD):', vad.segments)
            print('  speech sent: %.1f s'
                  % (vad.voice_bytes / (2 * self.sample_rate)))