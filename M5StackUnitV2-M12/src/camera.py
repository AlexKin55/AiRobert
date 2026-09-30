"""
UnitV2 camera snapshot and face detection.

- Frame capture via OpenCV.
- JPEG sending to the server.
- Face detection is performed by the UnitV2 firmware.
- Face Detector results are received from /func/result.
- Face detection runs in a separate thread so it does not block
  audio/video processing.
"""

from __future__ import annotations

import json
import math
import threading
import time

import requests

from .wsclient import WsClient


WEBSOCKET_SERVER_URL = "ws://192.168.1.X:8765"

# Разрешение, в котором Face Detector UnitV2
# возвращает координаты лица.
CAMERA_RESOLUTION = (640, 480)

# Максимальный угол отклонения.
MAX_X_ANGLE = 40.0
MAX_Y_ANGLE = 40.0

# Параметры трекера.
MAX_RADIUS = 60
LOST_TIMEOUT = 1.5

# Минимальная уверенность детектора для сопровождения цели
# (та же, что и при первичном выборе, и что на сервере в on_face).
CONFIDENCE_THRESHOLD = 0.80

# Защита от рывков камеры: максимальное изменение угла за одно
# обновление (градусы) и коэффициент экспоненциального сглаживания.
MAX_ANGLE_STEP = 10.0
SMOOTHING = 0.35

# Последние отправленные углы (состояние сглаживания).
_smooth = {"pan": None, "tilt": None}


active_target = None
next_id = 1


def calculate_angles(x, y, w, h, resolution):
    """
    Переводит положение центра лица в углы поворота.

    x, y, w, h — координаты лица от UnitV2.
    resolution — (width, height).

    Возвращает (angle_pan, angle_tilt) в градусах либо None, если рамка
    невалидна (нечисловые/неконечные значения, w <= 0 или h <= 0) — в этом
    случае вызывающий код оставляет прежние углы.

    Мусорные рамки детектора (нулевой размер, выход за границы кадра)
    не дают углов больше MAX_X_ANGLE / MAX_Y_ANGLE: координаты
    ограничиваются кадром, а итоговые углы клампируются.
    """

    try:
        x = float(x)
        y = float(y)
        w = float(w)
        h = float(h)
    except (TypeError, ValueError):
        return None

    if not (math.isfinite(x) and math.isfinite(y)
            and math.isfinite(w) and math.isfinite(h)):
        return None

    if w <= 0 or h <= 0:
        return None

    cam_w, cam_h = float(resolution[0]), float(resolution[1])

    # Ограничиваем рамку границами кадра.
    x = max(0.0, min(x, cam_w - 1))
    y = max(0.0, min(y, cam_h - 1))
    w = min(w, cam_w - x)
    h = min(h, cam_h - y)

    face_center_x = x + w / 2
    face_center_y = y + h / 2

    norm_x = (face_center_x - cam_w / 2) / (cam_w / 2)

    norm_y = (cam_h / 2 - face_center_y) / (cam_h / 2)

    angle_pan = max(
        -MAX_X_ANGLE,
        min(MAX_X_ANGLE, norm_x * MAX_X_ANGLE),
    )
    angle_tilt = max(
        -MAX_Y_ANGLE,
        min(MAX_Y_ANGLE, norm_y * MAX_Y_ANGLE),
    )

    return angle_pan, angle_tilt


def smooth_angles(pan, tilt):
    """
    Ограничивает рывки и сглаживает углы (лимит шага + EMA).

    Одиночная плохая детекция (ложное срабатывание, скачок рамки) не
    должна резко дергать камеру: изменение за одно обновление ограничено
    MAX_ANGLE_STEP, после чего значение дополнительно фильтруется
    экспоненциально (SMOOTHING).
    """
    prev_pan = _smooth["pan"]
    if prev_pan is None:
        _smooth["pan"] = pan
        _smooth["tilt"] = tilt
        return round(pan, 1), round(tilt, 1)

    # Предел изменения за одно обновление.
    step_pan = max(
        -MAX_ANGLE_STEP,
        min(MAX_ANGLE_STEP, pan - prev_pan),
    )
    step_tilt = max(
        -MAX_ANGLE_STEP,
        min(MAX_ANGLE_STEP, tilt - _smooth["tilt"]),
    )

    target_pan = prev_pan + step_pan
    target_tilt = _smooth["tilt"] + step_tilt

    # Экспоненциальное сглаживание к допустимой цели.
    new_pan = prev_pan + SMOOTHING * (target_pan - prev_pan)
    new_tilt = _smooth["tilt"] + SMOOTHING * (target_tilt - _smooth["tilt"])

    _smooth["pan"] = new_pan
    _smooth["tilt"] = new_tilt

    return round(new_pan, 1), round(new_tilt, 1)


def reset_smoothing():
    """Сбрасывает фильтр углов (при захвате нового собеседника)."""
    _smooth["pan"] = None
    _smooth["tilt"] = None


def track_single_face(data, client, max_radius, lost_timeout):
    """
    Выбирает и отслеживает одно лицо.

    Если лиц несколько, при появлении нового человека
    выбирается самое большое лицо с confidence >= 0.80.

    После выбора отслеживается именно оно.
    """

    global next_id
    global active_target

    faces = data.get("face", [])

    now = time.time()

    # ---------------------------------------------------------
    # Уже есть активный собеседник
    # ---------------------------------------------------------
    if active_target:

        best_match = None
        min_dist = max_radius

        old_face = active_target["coords"]

        old_area = max(
            0.0,
            float(old_face.get("w", 0)) *
            float(old_face.get("h", 0)),
        )

        old_center_x = (
            old_face["x"] +
            old_face["w"] / 2
        )

        old_center_y = (
            old_face["y"] +
            old_face["h"] / 2
        )

        for face in faces:

            # Слабые/мусорные детекции не считаем за того же человека:
            # ложное срабатывание (плечо, корпус, фон) ниже реального лица
            # увело бы камеру вниз, пока само лицо не двигается.
            if face.get("prob", 0) < CONFIDENCE_THRESHOLD:
                continue

            # Резкий скачок размера — почти наверняка другой объект.
            area = max(
                0.0,
                float(face.get("w", 0)) *
                float(face.get("h", 0)),
            )
            if old_area > 0 and (
                area < old_area / 4.0 or
                area > old_area * 4.0
            ):
                continue

            new_center_x = (
                face["x"] +
                face["w"] / 2
            )

            new_center_y = (
                face["y"] +
                face["h"] / 2
            )

            dist = math.sqrt(
                (old_center_x - new_center_x) ** 2 +
                (old_center_y - new_center_y) ** 2
            )

            if dist < min_dist:

                min_dist = dist
                best_match = face

        # -----------------------------------------------------
        # Нашли прежнего собеседника
        # -----------------------------------------------------
        if best_match:

            active_target["coords"] = best_match
            active_target["last_seen"] = now

            angles = calculate_angles(
                best_match["x"],
                best_match["y"],
                best_match["w"],
                best_match["h"],
                CAMERA_RESOLUTION,
            )

            # Мусорная рамка — не меняем углы (камера не дергается).
            if angles is None:
                return

            pan, tilt = smooth_angles(*angles)

            client.send_face(
                active_target["id"],
                True,
                pan,
                tilt,
                best_match["prob"],
            )

        # -----------------------------------------------------
        # Собеседник временно пропал
        # -----------------------------------------------------
        else:

            time_passed = (
                now -
                active_target["last_seen"]
            )

            if time_passed > lost_timeout:

                print(
                    f"[Трекер] Собеседник "
                    f"ID {active_target['id']} "
                    f"ушел из кадра "
                    f"(таймаут {time_passed:.1f}с). "
                    f"Ищем нового..."
                )

                active_target = None

            else:
                pass
                #client.send_face(
                #    active_target["id"],
                #    False,
                #    0.0,
                #   0.0,
                #    active_target["coords"]["prob"],
                #)

    # ---------------------------------------------------------
    # Активного собеседника нет
    # ---------------------------------------------------------
    elif faces:

        confident_faces = [
            face
            for face in faces
            if face.get("prob", 0) >= CONFIDENCE_THRESHOLD
        ]

        if confident_faces:

            # Если лиц несколько —
            # выбираем самое большое.
            biggest_face = max(
                confident_faces,
                key=lambda face:
                    face["w"] * face["h"],
            )

            angles = calculate_angles(
                biggest_face["x"],
                biggest_face["y"],
                biggest_face["w"],
                biggest_face["h"],
                CAMERA_RESOLUTION,
            )

            # Невалидная рамка — ждем следующий кадр, цель не захватываем.
            if angles is None:
                return

            active_target = {
                "id": next_id,
                "coords": biggest_face,
                "last_seen": now,
            }

            print(
                f"[Трекер] Нацелились "
                f"на нового человека! "
                f"Зарегистрирован ID: {next_id} "
                f"(Размер: "
                f"{biggest_face['w']:.0f}x"
                f"{biggest_face['h']:.0f}) "
                f"prob={biggest_face['prob']:.3f}"
            )

            next_id += 1

            reset_smoothing()
            pan, tilt = smooth_angles(*angles)

            client.send_face(
                active_target["id"],
                True,
                pan,
                tilt,
                biggest_face["prob"],
            )


class Camera:

    def __init__(self, client, face_detect_cfg):

        self.client = client
        self.frames_sent = 0
        self.last_error = None
        self.resolution = face_detect_cfg["resolution"]
        self.max_radius = face_detect_cfg["max_radius"]
        self.lost_timeout = face_detect_cfg["lost_timeout"]

        # -----------------------------------------------------
        # Переключаем UnitV2 в Face Detector
        # -----------------------------------------------------

        try:

            response = requests.post("http://127.0.0.1/func",
                json={
                    "type_name": "face_detector",
                    "args": [],
                },
                timeout=5,
            )

            response.raise_for_status()

            print("[Camera] UnitV2 переключен в режим Face Detector")
            print(
                f"[Camera] Ответ UnitV2: "
                f"{response.text}"
            )

        except Exception as e:

            self.last_error = str(e)

            print(
                "[Camera] Ошибка запуска "
                f"Face Detector: {e}"
            )

        # -----------------------------------------------------
        # Запускаем Face Detect в отдельном потоке.
        # -----------------------------------------------------

        self.async_face_detect()

    def async_face_detect(self):
        """
        Запускает face_detect() в отдельном потоке.

        Сам face_detect() является блокирующим методом,
        поэтому он не должен выполняться в основном потоке.
        """

        self.face_thread = threading.Thread(
            target=self.face_detect,
            daemon=True,
            name="FaceDetectThread",
        )

        self.face_thread.start()

        print(
            "[Camera] Поток Face Detect запущен"
        )

    def face_detect(self):
        """
        Получает поток результатов Face Detector
        от локального API UnitV2.

        API UnitV2:

            POST /func/result

        Формат потока:

            JSON|JSON|JSON|...

        Поэтому данные разбираются по символу '|'.
        """

        print(
            "[Camera] Подключаемся к "
            "UnitV2 Face Detector..."
        )

        local_stream = None

        try:

            local_stream = requests.post(
                "http://127.0.0.1/func/result",
                json={},
                stream=True,
                timeout=(5, None),
            )

            local_stream.raise_for_status()

            print(
                "[Camera] Поток Face Detector "
                "подключен"
            )

            buffer = b""

            for chunk in local_stream.iter_content(
                chunk_size=4096
            ):

                if not chunk:
                    continue

                buffer += chunk

                # В одном chunk может находиться:
                #
                # JSON|
                #
                # или:
                #
                # JSON|JSON|JSON|
                #
                # или даже часть JSON.
                #
                # Поэтому сначала накапливаем данные
                # в buffer.

                while b"|" in buffer:

                    raw_data, buffer = (
                        buffer.split(b"|", 1)
                    )

                    if not raw_data:
                        continue

                    try:

                        decoded = (
                            raw_data
                            .decode("utf-8")
                            .strip()
                        )

                        json_data = json.loads(
                            decoded
                        )

                        track_single_face(
                            json_data,
                            self.client,
                            self.max_radius,
                            self.lost_timeout,
                        )

                    except json.JSONDecodeError as exc:

                        print(
                            "[Camera] Ошибка JSON "
                            f"Face Detector: {exc}"
                        )

                    except Exception as exc:

                        print(
                            "[Camera] Ошибка обработки "
                            f"Face Detector: {exc}"
                        )

        except requests.RequestException as exc:

            self.last_error = str(exc)

            print(
                "[Camera] Ошибка подключения "
                f"к Face Detector: {exc}"
            )

        except Exception as exc:

            self.last_error = str(exc)

            print(
                "[Camera] Ошибка потока "
                f"Face Detector: {exc}"
            )

        finally:

            if local_stream is not None:

                try:
                    local_stream.close()

                except Exception:
                    pass

    def capture(self):
        """
        Захватывает кадр с камеры и отправляет
        JPEG на сервер.
        """

        try:

            import cv2

            cam = cv2.VideoCapture(0)

            ret, frame = cam.read()

            cam.release()

            if not ret:

                self.last_error = (
                    "frame not captured"
                )

                return 0

            ok, jpeg = cv2.imencode(
                ".jpg",
                frame,
                [
                    cv2.IMWRITE_JPEG_QUALITY,
                    80,
                ],
            )

            if not ok:

                self.last_error = (
                    "JPEG encoding error"
                )

                return 0

            data = jpeg.tobytes()

            if not self.client.send_image_jpeg(
                data
            ):

                self.last_error = (
                    "no server connection"
                )

                return 0

            self.frames_sent += 1

            return len(data)

        except Exception as exc:

            self.last_error = str(exc)

            return 0
