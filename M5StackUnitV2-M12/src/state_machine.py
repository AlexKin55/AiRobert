"""Camera session: cloud (AiService) + LOCAL robot control.

* WsClient — JSON to AiService (audio/image/face/touch/decay/hb), receives
  mute/unmute/capture/emotion/play;
* Audio — arecord capture + VAD (speech restarts the decay timers);
* Camera — face tracking: angles go to the robot DIRECTLY (local WS);
* RobotServer — the robot connects here; the camera plays audio answers
  (split into chunks with real-time pacing), sends movement/emotions and
  handles touch locally;
* EmotionDecay — decay timers on the camera (phrases synthesized by AiService
  on request; without the cloud the emotion still switches locally).

Everything that can work without the cloud works; cloud-dependent parts only
log the missing connection.
"""
from __future__ import annotations

import base64
import json
import logging
import queue
import threading
import time

from .audio import Audio
from .camera import Camera
from .decay import EmotionDecay, load_pcm
from .robot_server import RobotServer
from .wsclient import WsClient

log = logging.getLogger('camera.state_machine')

# Binary playback frame layout sent to the robot:
# [byte0=type=1][byte1=codec=1][raw PCM int16 LE]; empty payload = EOF.
ROBOT_AUDIO_FRAME_TYPE = 1
ROBOT_AUDIO_CODEC_PCM = 1


class StateMachine:
    """Camera session: cloud WsClient + Audio + Camera + local RobotServer."""

    def __init__(self, url, sample_rate=16000, chunk_seconds=0.5,
                 audio_device=None, audio_channels=1, audio_sudo=None,
                 record_seconds=10, reconnect_delay=3.0,
                 record_restart_delay=3.0, vad_config=None, face_detect=None,
                 robot_cfg=None, touch_cfg=None, playback_cfg=None,
                 decay_cfg=None):
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
        self.robot_cfg = robot_cfg or {}
        self.touch_cfg = touch_cfg or {}
        self.playback_cfg = playback_cfg or {}
        self.decay_cfg = decay_cfg or {}
        self.client = None
        self.audio = None
        self.camera = None
        self.robot = None
        self.decay = None
        # Playback chunking (the camera splits incoming audio itself):
        self.play_chunk_seconds = max(0.01, float(
            self.playback_cfg.get('chunk_seconds', 0.15)))
        self.play_speed = max(0.01, float(
            self.playback_cfg.get('speed', 1.0)))
        self.drop_tail_seconds = max(0.0, float(
            self.playback_cfg.get('drop_tail_seconds', 2.0)))
        # Serialized playback: a worker thread drains this queue; audio answers
        # from the cloud arrive as ONE message and are split into chunks here.
        self._play_q = queue.Queue(maxsize=8)
        self._play_thread = None
        self._stop = threading.Event()

    # --- server command dispatcher (from AiService) ----------------------
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
        elif mtype == 'play':
            self._on_play(msg)
        elif mtype == 'emotion':
            # Emotion command for the robot (model function call) — relayed
            # locally, no cloud round-trip.
            name = msg.get('name')
            if self.robot is not None:
                ok = self.robot.send_text(json.dumps(
                    {'type': 'emotion', 'name': name}))
                log.info('Server emotion %r -> robot (%s)', name,
                         'ok' if ok else 'no connection')
            else:
                log.warning('Server emotion %r but robot server is off',
                            name)
        else:
            log.warning('Unknown server command: %s', mtype)

    def _on_play(self, msg):
        """Full audio answer from the cloud: {"type":"play","audio":"<b64>"}.

        An optional ``"decay":"<emotion>"`` marks a decay phrase: it is played
        only while the decay stage is still relevant (stale answers are
        dropped so an old phrase never mixes into a new dialogue).
        """
        try:
            pcm = base64.b64decode(str(msg.get('audio', '')))
        except Exception as exc:  # noqa: BLE001
            log.warning('play: invalid base64: %s', exc)
            return
        if not pcm:
            return
        decay_emotion = msg.get('decay')
        if decay_emotion:
            if self.decay is not None and \
                    self.decay.matches_stage(str(decay_emotion)):
                self.decay.notify_answer()
                self.play_pcm(pcm, dialogue=False)
            else:
                log.info('play: stale decay phrase %r dropped', decay_emotion)
            return
        # Regular dialogue answer — restarts/cancels the decay accordingly.
        self.play_pcm(pcm, dialogue=True)

    # --- playback (camera splits the audio into chunks) ------------------
    def play_pcm(self, pcm, dialogue=False):
        """Queues PCM for playback on the robot (serialized worker).

        ``dialogue=True`` for regular cloud answers: cancels the emotion
        decay while playing and restarts it after the EOF tail. Local sounds
        (touch/yawn/snore) and decay phrases pass ``dialogue=False``.
        """
        if not pcm or self.robot is None:
            return False
        try:
            self._play_q.put_nowait((pcm, bool(dialogue)))
            return True
        except queue.Full:
            log.warning('Playback queue full — chunk dropped')
            return False

    def _playback_worker(self):
        while not self._stop.is_set():
            try:
                pcm, dialogue = self._play_q.get(timeout=0.5)
            except queue.Empty:
                continue
            if dialogue and self.decay is not None:
                self.decay.cancel()
            # Mute the microphone while the robot speaks (echo-loop guard).
            self.audio.set_muted(True)
            try:
                self._send_chunks(pcm)
                # End-of-playback marker.
                self.robot.send_binary(bytes(
                    (ROBOT_AUDIO_FRAME_TYPE, ROBOT_AUDIO_CODEC_PCM)))
                # Tail: let the robot drain its playback queue/DMA before the
                # mic hears the speaker echo again.
                if self.drop_tail_seconds > 0:
                    self._stop.wait(self.drop_tail_seconds)
            except Exception as exc:  # noqa: BLE001
                log.warning('Playback error: %s', exc)
            finally:
                self.audio.set_muted(False)
            if dialogue and self.decay is not None:
                # The dialogue finished — start the decay countdown.
                self.decay.start()

    def _send_chunks(self, pcm):
        """Splits PCM into chunks and sends them with real-time pacing.

        Frame = [type=1][codec=1][raw PCM int16 LE 16 kHz mono]; the pause
        between frames equals the chunk duration / play_speed (1.0 = real
        time), so the robot's playback queue never overflows.
        """
        chunk_size = round(self.sample_rate * self.play_chunk_seconds) * 2
        if chunk_size < 2:
            chunk_size = 3200
        chunk_dur = chunk_size / (2.0 * self.sample_rate)
        n_chunks = (len(pcm) + chunk_size - 1) // chunk_size
        for idx in range(n_chunks):
            if self._stop.is_set():
                break
            part = pcm[idx * chunk_size:(idx + 1) * chunk_size]
            ok = self.robot.send_binary(
                bytes((ROBOT_AUDIO_FRAME_TYPE, ROBOT_AUDIO_CODEC_PCM)) + part)
            if not ok:
                log.warning('Playback: robot offline at chunk %d/%d',
                            idx + 1, n_chunks)
                break
            self._stop.wait(chunk_dur / self.play_speed)

    # --- robot events (from the local RobotServer) -----------------------
    def _on_robot_message(self, text):
        try:
            msg = json.loads(text)
        except Exception:
            log.warning('Robot non-JSON text: %s', str(text)[:80])
            return
        if not isinstance(msg, dict):
            return
        mtype = msg.get('type')
        if mtype == 'hb':
            log.info('Robot HB (ip=%s)', msg.get('ip', '?'))
        elif mtype == 'ack':
            log.info('Robot ack: %s', msg.get('command', ''))
        elif mtype == 'touch':
            action = str(msg.get('action', ''))
            log.info('Robot touch: %r', action)
            # Relay to AiService (statistics only; cloud may be offline).
            if self.client is not None:
                self.client.send_touch(action)
            self._handle_touch()
        else:
            log.info('Robot message: %s', str(text)[:120])

    def _handle_touch(self):
        """LOCAL touch reaction: happy emotion + touch sound on the robot.

        Initialized in the camera code — works even when the cloud is offline.
        """
        emotion = str(self.touch_cfg.get('emotion', 'happy') or 'happy')
        if self.robot is not None and self.robot.connected:
            self.robot.send_text(json.dumps(
                {'type': 'emotion', 'name': emotion}))
        if self.touch_cfg.get('enabled', True):
            pcm = load_pcm(str(self.touch_cfg.get(
                'sound_file', 'sounds/touch.wav')))
            if pcm:
                self.play_pcm(pcm, dialogue=False)
            else:
                log.info('Touch sound skipped (no WAV)')

    def _on_robot_connected(self):
        log.info('Robot connected to camera (local WS)')
        if self.client is not None:
            self.client.robot_connected = True

    def _on_robot_disconnected(self):
        log.info('Robot disconnected from camera (local WS)')
        if self.client is not None:
            self.client.robot_connected = False

    # --- session start ---------------------------------------------------
    def start(self):
        """Starts WsClient (auto-reconnect), Audio (arecord), RobotServer,
        decay scheduler and Camera (face tracking). Does not block when the
        server, the microphone or the robot is unavailable."""
        self.client = WsClient(self.url, on_command=self._on_command,
                               sample_rate=self.sample_rate,
                               reconnect_delay=self.reconnect_delay)
        log.info('WsClient started (auto-reconnect), URL: %s', self.url)
        self.client.start()

        self.robot = RobotServer(
            host=self.robot_cfg.get('host', '0.0.0.0'),
            port=int(self.robot_cfg.get('port', 8765)),
            path=str(self.robot_cfg.get('path', '/robot')),
            on_message=self._on_robot_message,
            on_connected=self._on_robot_connected,
            on_disconnected=self._on_robot_disconnected,
        )
        if self.robot_cfg.get('enabled', True):
            self.robot.start()
        else:
            log.info('Robot server disabled (settings robot.enabled=false)')

        self.decay = EmotionDecay(self.robot, self.client,
                                  play_pcm=self.play_pcm,
                                  cfg=self.decay_cfg)
        if not self.decay_cfg.get('enabled', True):
            log.info('Emotion decay disabled (settings decay.enabled=false)')

        self.audio = Audio(
            self.client,
            sample_rate=self.sample_rate,
            chunk_seconds=self.chunk_seconds,
            channels=self.audio_channels,
            device=self.audio_device,
            sudo=self.audio_sudo,
            restart_delay=self.record_restart_delay,
            vad_config=self.vad_config,
            on_speech_started=self._on_speech_started,
        )
        self.audio.start()

        self.camera = Camera(self.client, self.face_detect, robot=self.robot)

        self._play_thread = threading.Thread(
            target=self._playback_worker, daemon=True,
            name='PlaybackWorker')
        try:
            self._play_thread.start()
        except RuntimeError as exc:
            raise RuntimeError(
                'cannot start playback thread (%s): thread/memory limit' % exc
            ) from exc
        return self

    def _on_speech_started(self):
        """The user started speaking (VAD) — restart the decay countdown."""
        log.info('Speech started — restarting emotion decay')
        if self.decay is not None and self.decay_cfg.get('enabled', True):
            self.decay.start()

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
        """Stops recording, the robot server and the connection cleanly."""
        self._stop.set()
        if self.decay is not None:
            self.decay.cancel()
        if self.client is not None:
            time.sleep(0.3)
            self.audio.stop()
            self.client.close()
        if self.robot is not None:
            self.robot.stop()
        if self._play_thread is not None:
            self._play_thread.join(timeout=2.0)
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
        print('  robot connected:', bool(self.robot and self.robot.connected))
        if self.audio.vad is not None:
            vad = self.audio.vad
            print('  speech segments (VAD):', vad.segments)
            print('  speech sent: %.1f s'
                  % (vad.voice_bytes / (2 * self.sample_rate)))