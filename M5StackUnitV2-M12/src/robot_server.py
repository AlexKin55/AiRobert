"""Local WebSocket server for the robot (M5StackChanAI).

The camera controls the robot locally (movement, playback, emotions, touch);
AiService may run in the cloud. This module is TRANSPORT ONLY — a minimal
RFC 6455 server (stdlib only) that accepts one robot client: the camera code
pushes frames via ``send_text()``/``send_binary()`` and receives the robot's
JSON events via ``on_message``. All robot-control logic lives in the camera
code (state_machine.py / camera.py / decay.py), not here.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import socket
import struct
import threading

log = logging.getLogger('camera.robot_server')

# RFC 6455 GUID used in the Sec-WebSocket-Accept handshake digest.
WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

# Max request size for the HTTP upgrade handshake (headers).
_HANDSHAKE_MAX = 65536

# recv() timeout in the client loop — gives the loop a chance to observe the
# stop event while keeping the connection responsive.
_RECV_TIMEOUT = 0.5
_ACCEPT_TIMEOUT = 0.5


def _accept_key(key: str) -> str:
    """Sec-WebSocket-Accept = base64(SHA1(key + GUID))."""
    digest = hashlib.sha1((key.strip() + WS_GUID).encode('utf-8')).digest()
    return base64.b64encode(digest).decode('ascii')


def _encode_frame(opcode: int, payload: bytes) -> bytes:
    """Builds a server->client frame (server frames are NOT masked)."""
    n = len(payload)
    if n < 126:
        header = bytes((0x80 | opcode, n))
    elif n < 65536:
        header = bytes((0x80 | opcode, 126)) + struct.pack('>H', n)
    else:
        header = bytes((0x80 | opcode, 127)) + struct.pack('>Q', n)
    return header + payload


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Reads exactly n bytes; raises OSError on EOF."""
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError('connection closed')
        buf += chunk
    return buf


def _recv_frame(sock: socket.socket):
    """Reads one client->server frame; returns (opcode, payload).

    Client frames MUST be masked (RFC 6455); unmasked data is refused.
    """
    b0, b1 = _recv_exact(sock, 2)
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    length = b1 & 0x7F
    if length == 126:
        length = struct.unpack('>H', _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack('>Q', _recv_exact(sock, 8))[0]
    if not (b1 & 0x80):
        raise OSError('unmasked client frame')
    mask = _recv_exact(sock, 4)
    payload = _recv_exact(sock, length)
    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    if not fin:
        # Not supported: the robot never fragments its small JSON messages.
        raise OSError('fragmented frame not supported')
    return opcode, payload


class RobotServer:
    """One-client WebSocket server for the robot (thread-based).

    ``start()`` runs the accept loop in a background thread; the robot's
    receive loop runs in another thread. All sends are serialized by a lock
    so face-tracking (camera thread) and playback (WS thread) never interleave
    frames. Callbacks are invoked from the server threads and must be fast —
    the heavy robot-control logic lives in the camera state machine.
    """

    def __init__(self, host='0.0.0.0', port=8765, path='/robot',
                 on_message=None, on_connected=None, on_disconnected=None):
        self.host = host
        self.port = int(port)
        self.path = path or '/robot'
        self.on_message = on_message           # fn(text: str)
        self.on_connected = on_connected       # fn()
        self.on_disconnected = on_disconnected  # fn()
        self._sock = None
        self._client = None
        self._accept_thread = None
        self._recv_thread = None
        self._stop = threading.Event()
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._connected = False

    # --- lifecycle --------------------------------------------------------
    @property
    def connected(self) -> bool:
        with self._state_lock:
            return self._connected

    def start(self):
        """Starts the accepting thread (non-blocking)."""
        self._stop.clear()
        self._accept_thread = threading.Thread(
            target=self._accept_loop, daemon=True, name='RobotServerAccept')
        try:
            self._accept_thread.start()
        except RuntimeError as exc:
            raise RuntimeError(
                'cannot start robot WS server thread (%s): thread/memory '
                'limit' % exc) from exc

    def stop(self):
        """Stops the server and closes the robot connection."""
        self._stop.set()
        with self._send_lock:
            if self._client is not None:
                try:
                    self._client.close()
                except OSError:
                    pass
                self._client = None
            self._set_connected_locked(False)
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        for t in (self._accept_thread, self._recv_thread):
            if t is not None:
                t.join(timeout=2.0)

    # --- sending ----------------------------------------------------------
    def send_text(self, text: str) -> bool:
        """Sends a TEXT frame to the robot (JSON command)."""
        return self._send_frame(OP_TEXT, text.encode('utf-8'))

    def send_binary(self, data: bytes) -> bool:
        """Sends a BINARY frame to the robot (audio playback chunk)."""
        return self._send_frame(OP_BINARY, bytes(data))

    def _send_frame(self, opcode: int, payload: bytes) -> bool:
        if not self.connected:
            return False
        with self._send_lock:
            if self._client is None:
                return False
            try:
                self._client.sendall(_encode_frame(opcode, payload))
                return True
            except OSError as exc:
                log.warning('[robot] send error: %s', exc)
                self._drop_client_locked()
                return False

    # --- accept loop ------------------------------------------------------
    def _accept_loop(self):
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind((self.host, self.port))
            self._sock.listen(1)
            self._sock.settimeout(_ACCEPT_TIMEOUT)
        except OSError as exc:
            log.error('[robot] cannot listen on %s:%d: %s',
                      self.host, self.port, exc)
            return
        log.info('[robot] robot WS server: ws://%s:%d%s',
                 self.host, self.port, self.path)
        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            log.info('[robot] connection from %s:%d', addr[0], addr[1])
            # One robot at a time — drop any previous connection.
            with self._send_lock:
                self._close_client_locked()
            if not self._handshake(conn):
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            with self._send_lock:
                if self._stop.is_set():
                    try:
                        conn.close()
                    except OSError:
                        pass
                    break
                self._client = conn
                self._set_connected_locked(True)
            self._recv_thread = threading.Thread(
                target=self._recv_loop, args=(conn,), daemon=True,
                name='RobotServerRecv')
            self._recv_thread.start()

    def _handshake(self, conn: socket.socket) -> bool:
        """Performs the HTTP Upgrade handshake; True on success."""
        conn.settimeout(5.0)
        try:
            request = b''
            while b'\r\n\r\n' not in request:
                chunk = conn.recv(4096)
                if not chunk:
                    return False
                request += chunk
                if len(request) > _HANDSHAKE_MAX:
                    return False
            headers = {}
            lines = request.decode('utf-8', errors='replace').split('\r\n')
            for line in lines[1:]:
                if ':' in line:
                    key, _, value = line.partition(':')
                    headers[key.strip().lower()] = value.strip()
            key = headers.get('sec-websocket-key', '')
            if not key:
                log.warning('[robot] handshake: no Sec-WebSocket-Key')
                return False
            response = (
                'HTTP/1.1 101 Switching Protocols\r\n'
                'Upgrade: websocket\r\n'
                'Connection: Upgrade\r\n'
                'Sec-WebSocket-Accept: %s\r\n'
                '\r\n' % _accept_key(key)
            )
            conn.sendall(response.encode('ascii'))
            return True
        except OSError as exc:
            log.warning('[robot] handshake error: %s', exc)
            return False

    # --- receive loop -----------------------------------------------------
    def _recv_loop(self, conn: socket.socket):
        conn.settimeout(_RECV_TIMEOUT)
        try:
            while not self._stop.is_set():
                try:
                    opcode, payload = _recv_frame(conn)
                except socket.timeout:
                    continue
                except OSError as exc:
                    if not self._stop.is_set():
                        log.warning('[robot] recv error: %s', exc)
                    break
                if opcode == OP_CLOSE:
                    log.info('[robot] close frame from robot')
                    self._reply_close(conn)
                    break
                if opcode == OP_PING:
                    self._send_frame(OP_PONG, payload)
                    continue
                if opcode == OP_PONG:
                    continue
                if opcode == OP_TEXT:
                    try:
                        text = payload.decode('utf-8')
                    except UnicodeDecodeError:
                        log.warning('[robot] non-UTF8 text frame')
                        continue
                    if self.on_message:
                        try:
                            self.on_message(text)
                        except Exception as exc:  # noqa: BLE001
                            log.warning('[robot] on_message error: %s', exc)
                    continue
                if opcode == OP_BINARY:
                    log.warning('[robot] unexpected binary from robot '
                                '(%d B)', len(payload))
                    continue
                log.warning('[robot] unknown opcode %d', opcode)
        finally:
            with self._send_lock:
                self._close_client_locked()
            try:
                conn.close()
            except OSError:
                pass

    def _reply_close(self, conn: socket.socket):
        try:
            conn.sendall(_encode_frame(OP_CLOSE, b''))
        except OSError:
            pass

    # --- state helpers (call under _send_lock) ----------------------------
    def _set_connected_locked(self, value: bool):
        was = self._connected
        self._connected = bool(value)
        if value and not was:
            log.info('[robot] robot connected (local WS)')
            if self.on_connected:
                try:
                    self.on_connected()
                except Exception as exc:  # noqa: BLE001
                    log.warning('[robot] on_connected error: %s', exc)
        elif not value and was:
            log.info('[robot] robot disconnected (local WS)')
            if self.on_disconnected:
                try:
                    self.on_disconnected()
                except Exception as exc:  # noqa: BLE001
                    log.warning('[robot] on_disconnected error: %s', exc)

    def _close_client_locked(self):
        if self._client is not None:
            try:
                self._client.close()
            except OSError:
                pass
            self._client = None
        self._set_connected_locked(False)

    def _drop_client_locked(self):
        self._close_client_locked()