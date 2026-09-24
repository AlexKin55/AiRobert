"""Camera WebSocket transport to AiService (/camera), JSON protocol.

Binary data (audio, pictures) is sent base64-encoded (see
``AiService/src/protocol.py``).

**Auto-reconnect.** The connection is maintained by a background thread:
- on a connect error or a dropped link the client does NOT crash; after
  ``reconnect_delay`` seconds it tries again (cyclically);
- when the link is restored, ``hello`` is sent again and command receiving
  continues;
- send methods return ``False`` when there is no connection (data is dropped,
  queues do not grow).

Send: ``audio``/``image``/``face``/``emotion``/``hb``. Receive: server commands
``mute``/``unmute``/``capture`` -> ``on_command(msg)``; ``ok``/``error``
replies are logged.
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time

import websocket

log = logging.getLogger('camera.wsclient')

# Heartbeat: the camera is silent (VAD) and the server sends nothing — without
# hb, recv() times out and drops the connection. hb every N seconds gets a
# server "ok" reply and keeps the link alive.
WS_HEARTBEAT_INTERVAL = 5.0


class WsClient:
    """JSON transport to AiService (/camera) with auto-reconnect.

    A background thread maintains the connection: on a connect error or a
    dropped link it retries every reconnect_delay seconds; on success it
    sends hello and continues receiving. Send methods return False if there
    is no connection.

    Every outgoing message carries 'timestamp' — Unix time in microseconds
    (added automatically in send_message).
    """

    def __init__(self, url, on_command=None, sample_rate=16000,
                 reconnect_delay=3.0):
        self.url = url
        self.on_command = on_command
        self.sample_rate = sample_rate
        self.reconnect_delay = reconnect_delay
        self.ws = None
        self.connected = False
        self.attempts = 0       # number of reconnect attempts
        self._thread = None
        self._stop = threading.Event()

    # --- lifecycle --------------------------------------------------------
    def start(self):
        """Starts the background connection-maintenance thread (non-blocking)."""
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        try:
            self._thread.start()
        except RuntimeError as exc:
            raise RuntimeError(
                'cannot start WS thread (%s): thread/memory limit. '
                'Kill stale camera processes '
                '(ps aux | grep -E "run_camera|arecord") or raise the '
                'limit (ulimit -u)' % exc) from exc

    def _run_loop(self):
        """Loop: connect -> receive -> (drop) -> pause -> repeat."""
        while not self._stop.is_set():
            try:
                self.ws = websocket.create_connection(self.url, timeout=5)
                self.connected = True
                log.info('WS connected: %s', self.url)
                self._send_hello()
                self._recv_loop()
            except Exception as exc:
                if not self._stop.is_set():
                    log.warning('WS error/drop: %s', exc)
            finally:
                self.connected = False
                if self.ws is not None:
                    try:
                        self.ws.close()
                    except Exception:
                        pass
                    self.ws = None
            if self._stop.is_set():
                break
            self.attempts += 1
            log.warning('WS reconnect in %.1f s (attempt %d)',
                        self.reconnect_delay, self.attempts)
            time.sleep(self.reconnect_delay)

    def _send_hello(self):
        ok = self.send_message({
            'type': 'hello',
            'device': 'unitv2',
            'format': {
                'audio': {'rate': self.sample_rate, 'channels': 1, 'bits': 16},
                'image': {'codec': 'jpeg'},
            },
        })
        if ok:
            log.info('hello sent')
        else:
            log.warning('hello not sent')

    def wait_connected(self, timeout=5.0):
        """Waits for a connection up to timeout seconds (does not crash)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.connected:
                return True
            time.sleep(0.1)
        return False

    def close(self):
        self._stop.set()
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None
        self.connected = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    # --- sending ----------------------------------------------------------
    @staticmethod
    def _now_us():
        """Unix time in microseconds."""
        return int(time.time() * 1_000_000)

    def send_message(self, payload):
        """Sends a JSON object (+timestamp). False if there is no connection."""
        if not self.connected or self.ws is None:
            return False
        try:
            msg = dict(payload)
            msg['timestamp'] = self._now_us()
            self.ws.send(json.dumps(msg))
            return True
        except Exception as exc:
            log.warning('WS send error: %s', exc)
            return False

    def send_audio_pcm(self, pcm):
        """Microphone audio: {"type":"audio","audio":"<base64>"}."""
        return self.send_message({'type': 'audio',
                                  'audio': base64.b64encode(pcm).decode('ascii')})

    def send_image_jpeg(self, jpeg):
        """Picture: {"type":"image","image":"<base64>"}."""
        return self.send_message({'type': 'image',
                                  'image': base64.b64encode(jpeg).decode('ascii')})

    def send_face(self, face_id, confidence=None):
        """Recognized face: {"type":"face","face_id":...,...}."""
        payload = {'type': 'face', 'face_id': face_id}
        if confidence is not None:
            payload['confidence'] = confidence
        return self.send_message(payload)

    def send_emotion(self, emotion):
        """Face emotion: {"type":"emotion","emotion":...}."""
        return self.send_message({'type': 'emotion', 'emotion': emotion})

    def send_hb(self):
        """Heartbeat: {"type":"hb"}."""
        return self.send_message({'type': 'hb'})

    # --- receiving --------------------------------------------------------
    def _recv_loop(self):
        """Receive loop; the wait timeout is used as a heartbeat tick.

        The server replies "ok" to every camera message (including hb), so a
        regular hb keeps the connection alive even when VAD is silent and no
        audio is sent.
        """
        last_hb = 0.0
        while not self._stop.is_set():
            try:
                msg = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                # No data — a good moment to remind the server about us.
                if time.monotonic() - last_hb >= WS_HEARTBEAT_INTERVAL:
                    self.send_hb()
                    last_hb = time.monotonic()
                continue
            except Exception as exc:
                if not self._stop.is_set():
                    log.warning('WS receive finished: %s', exc)
                break
            self._dispatch(msg)

    def _dispatch(self, msg):
        if not msg or not str(msg).strip():
            return  # empty frame (connection close) — do not log
        if isinstance(msg, (bytes, bytearray)):
            log.warning('WS binary (not JSON): %d bytes', len(msg))
            return
        try:
            data = json.loads(msg)
        except Exception:
            log.warning('WS text (not JSON): %s', msg)
            return
        if not isinstance(data, dict):
            return
        mtype = data.get('type')
        if mtype in ('ok', 'error'):
            log.info('server: %s', data.get('detail', mtype))
            return
        if self.on_command is not None:
            try:
                self.on_command(data)
            except Exception as exc:
                log.warning('on_command error: %s', exc)