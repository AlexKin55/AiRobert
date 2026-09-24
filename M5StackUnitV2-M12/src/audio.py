"""UnitV2 microphone capture via ALSA ``arecord`` and PCM sending.

The camera has no PortAudio/sounddevice, so audio is captured by the system
``arecord`` (alsa-utils) through a subprocess:

- protocol rate/channels (16 kHz mono) are passed to arecord directly
  (``-r 16000 -c 1 -f S16_LE -t raw``) to get ready-to-send PCM bytes;
- on UnitV2 the audio device is root-only (``sudo arecord ...``): the
  availability of ``sudo -n`` is detected automatically, the prefix can be
  forced with the ``audio_sudo`` setting;
- ``arecord`` writes raw PCM (S16_LE) to stdout;
- a background thread reads stdout in ``chunk_bytes`` blocks
  (``sample_rate * chunk_seconds * 2 * channels``) and queues the chunks;
- a second background thread sends the chunks via ``client.send_audio_pcm()``;
- when ``muted=True`` (server ``mute`` command) chunks are not sent;
- if ``arecord`` exits with an error (no device etc.), stderr is logged and
  the process is restarted after ``restart_delay`` seconds.

With ``channels=2`` (stereo UnitV2 mics) a downmix to mono is done without
numpy — by averaging pairs of int16 samples.
"""
from __future__ import annotations

import array as _array
import logging
import queue
import subprocess
import threading

from .wsclient import WsClient  # noqa: F401  (client type; used externally)

log = logging.getLogger('camera.audio')

# arecord buffer (µs): bounds the latency between a sample and pipe reads.
RECORDER_BUFFER_US = 200000


def _sudo_available():
    """Checks whether ``sudo -n`` works (no password prompt).

    On UnitV2 the audio device opens only via sudo. If sudo requires a
    password, run the whole script through sudo (root does not need sudo -n).
    """
    try:
        r = subprocess.run(['sudo', '-n', 'true'],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        return r.returncode == 0
    except OSError:
        return False


def _stereo_to_mono(data):
    """Downmix stereo S16_LE -> mono S16_LE (average sample pairs)."""
    samples = _array.array('h')
    samples.frombytes(data)
    mono = _array.array('h')
    for i in range(0, len(samples) - 1, 2):
        mono.append(int((samples[i] + samples[i + 1]) / 2))
    return mono.tobytes()


class Audio:
    """Capture via arecord and PCM sending via WsClient (two threads).

    The capture thread reads the arecord stdout and queues chunks; the send
    thread sends them via client.send_audio_pcm(). With muted=True (server
    mute command) chunks are not sent — the robot plays audio at that moment.
    """

    def __init__(self, client, sample_rate=16000, chunk_seconds=0.5,
                 channels=1, device=None, sudo=None, restart_delay=3.0,
                 muted=False, vad_config=None):
        self.client = client
        self.sample_rate = int(sample_rate)
        self.channels = max(1, min(2, int(channels)))
        self.device = device or 'default'   # ALSA device (plughw:1,0 etc.)
        # None = auto-detect (sudo -n true); True/False — force.
        self._sudo = _sudo_available() if sudo is None else bool(sudo)
        self.restart_delay = restart_delay
        self.muted = muted
        # Read block size: sample_rate * chunk_seconds * 2 bytes * channels.
        self.chunk_bytes = int(self.sample_rate * chunk_seconds) * 2 * self.channels
        # Adaptive VAD (settings['vad'] section); None = continuous sending.
        self.vad = None
        if vad_config and vad_config.get('enabled', False):
            from .vad import AdaptiveVad
            self.vad = AdaptiveVad(
                sample_rate=self.sample_rate,
                chunk_seconds=chunk_seconds,
                alpha=float(vad_config.get('alpha', 0.05)),
                min_threshold=float(vad_config.get('min_threshold', 400)),
                multiplier=float(vad_config.get('multiplier', 1.6)),
                silence_seconds=float(vad_config.get('silence_seconds', 2.0)),
                pre_record_seconds=float(vad_config.get('pre_record_seconds', 0.4)),
                active_multiplier=float(vad_config.get('active_multiplier', 1.05)),
                active_min_threshold=float(vad_config.get('active_min_threshold', 20)),
                calibration_seconds=float(vad_config.get('calibration_seconds', 3.0)),
            )
            log.info('VAD enabled: min threshold %.0f, multiplier %.1f, '
                     'silence %.1f s, pre-record %d chunk(s)',
                     self.vad.min_threshold, self.vad.multiplier,
                     self.vad.silence_seconds, self.vad.pre_record_chunks)
        self._q = queue.Queue(maxsize=64)
        self._stop = threading.Event()
        self._thread = None          # sending
        self._capture_thread = None  # capture (arecord)
        self.proc = None             # active arecord process
        self.chunks_sent = 0
        self.bytes_sent = 0
        self.errors = 0
        self.attempts = 0            # arecord restarts

    # --- lifecycle --------------------------------------------------------
    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._send_loop, daemon=True)
        self._capture_thread = threading.Thread(
            target=self._capture_loop, daemon=True)
        try:
            self._thread.start()
            self._capture_thread.start()
        except RuntimeError as exc:
            self._stop.set()
            raise RuntimeError(
                'cannot start audio threads (%s): thread/memory limit. '
                'Kill stale camera processes '
                '(ps aux | grep -E "run_camera|arecord") or raise the '
                'limit (ulimit -u)' % exc) from exc

    def stop(self):
        self._stop.set()
        if self.proc is not None:
            try:
                self.proc.terminate()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=2)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        if self._capture_thread is not None:
            self._capture_thread.join(timeout=2.0)
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    # --- dispatcher -------------------------------------------------------
    def set_muted(self, value):
        """Turns audio sending on/off (server mute/unmute commands)."""
        self.muted = bool(value)

    def push(self, pcm_bytes):
        """Queues a PCM chunk (mono S16_LE) for sending."""
        if self.muted:
            return  # microphone muted by the server
        try:
            self._q.put_nowait(pcm_bytes)
        except queue.Full:
            pass  # drop overflow: better to keep real-time pace

    # --- capture (arecord) ------------------------------------------------
    def _recorder_cmd(self):
        cmd = [
            'arecord', '-q',
            '-D', self.device,
            '-f', 'S16_LE',
            '-r', str(self.sample_rate),
            '-c', str(self.channels),
            '-t', 'raw',
            '--buffer-time=%d' % RECORDER_BUFFER_US,
        ]
        if self._sudo:
            cmd = ['sudo', '-n'] + cmd
        return cmd

    def _start_recorder(self):
        cmd = self._recorder_cmd()
        log.info('arecord: %s', ' '.join(cmd))
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            log.error('cannot start arecord (%s): install alsa-utils', exc)
            self.errors += 1
            self.proc = None
            self._stop.wait(self.restart_delay)

    def _capture_loop(self):
        """Loop: start arecord -> read stdout -> (drop) -> pause -> repeat."""
        while not self._stop.is_set():
            self._start_recorder()
            if self._stop.is_set() or self.proc is None:
                break
            self._read_loop()
            if self._stop.is_set():
                break
            self.attempts += 1
            log.warning('arecord exited, retry in %.1f s (attempt %d)',
                        self.restart_delay, self.attempts)
            self._stop.wait(self.restart_delay)

    def _read_loop(self):
        """Reads the arecord stdout in chunk_bytes blocks and queues chunks."""
        while not self._stop.is_set():
            try:
                data = self.proc.stdout.read(self.chunk_bytes)
            except Exception as exc:
                log.warning('arecord read: %s', exc)
                break
            if not data:
                break  # process exited
            if len(data) < self.chunk_bytes:
                continue  # tail shorter than a chunk — drop
            if self.channels > 1:
                data = _stereo_to_mono(data)
            if self.vad is not None:
                was_active = self.vad.active
                data = self.vad.feed(data)
                if data is None:
                    if was_active and not self.vad.active:
                        log.info('VAD: segment %d finished (%.1f s of speech)',
                                 self.vad.segments,
                                 self.vad.voice_bytes / (2 * self.sample_rate))
                    continue  # silence — do not send
                if not was_active and self.vad.active:
                    log.info('VAD: segment %d started', self.vad.segments)
            self.push(data)
        # Collect stderr (device open errors etc.).
        err = b''
        try:
            err = self.proc.stderr.read() or b''
        except Exception:
            pass
        if err:
            log.warning('arecord stderr: %s',
                        err.decode('utf-8', errors='replace').strip())
        try:
            self.proc.wait(timeout=2)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass

    # --- sending ----------------------------------------------------------
    def _send_loop(self):
        while not self._stop.is_set():
            try:
                pcm = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                ok = self.client.send_audio_pcm(pcm)
                if ok:
                    self.chunks_sent += 1
                    self.bytes_sent += len(pcm)
                    log.info('Audio sent to server: chunk #%d, %d bytes '
                             '(total %d B)', self.chunks_sent, len(pcm),
                             self.bytes_sent)
                # False = no connection: the chunk is dropped, reconnect runs
            except Exception as exc:
                self.errors += 1
                log.warning('Send error: %s', exc)