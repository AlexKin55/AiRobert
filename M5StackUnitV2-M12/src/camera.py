"""UnitV2 camera snapshot and JPEG sending via WsClient.

- frame capture via OpenCV (``cv2``, as in ``AiCamera/example/exmple.ipynb``);
- JPEG encoding and sending as ``{"type":"image","image":"<base64>"}``;
- called by the server ``capture`` command or manually.
"""
from __future__ import annotations

from .wsclient import WsClient  # noqa: F401  (client type; used externally)


class Camera:
    """UnitV2 camera snapshot and JPEG sending via WsClient.

    Called either by the server capture command (via on_command) or manually.
    Sending — a JSON message ``{"type":"image",...}``.
    """

    def __init__(self, client):
        self.client = client
        self.frames_sent = 0
        self.last_error = None

    def capture(self):
        """Takes a snapshot and sends JPEG. Returns the byte count (0 = error)."""
        try:
            import cv2
            cam = cv2.VideoCapture(0)
            ret, frame = cam.read()
            cam.release()
            if not ret:
                self.last_error = 'frame not captured'
                return 0
            ok, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if not ok:
                self.last_error = 'JPEG encoding error'
                return 0
            data = jpeg.tobytes()
            if not self.client.send_image_jpeg(data):
                self.last_error = 'no server connection'
                return 0
            self.frames_sent += 1
            return len(data)
        except Exception as exc:
            self.last_error = str(exc)
            return 0