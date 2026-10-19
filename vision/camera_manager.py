"""Управление тремя USB-камерами.

При запуске для каждой роли создаётся обычный ``cv2.VideoCapture(id)``.
Успешного ``isOpened()`` достаточно; кадры на старте не запрашиваются.
Чтение камер строго последовательное: один ``cap.read()`` в момент времени
на весь менеджер. Ролевые блокировки защищают устройство от параллельного
доступа, глобальный I/O-замок сериализует USB.
"""

import json
import queue
import threading
import time

import cv2
import numpy as np


CONFIG_FILE = "camera_mapping.json"

_REQUIRED_ROLES = (
    "LEFT",
    "MIDDLE",
    "RIGHT",
)
_EXPECTED_SIZE = (1280, 720)

_CAPTURE_TIMEOUT = 5.0
_REQUESTED_FPS = 30.0
_BUFFER_DRAIN_COUNT = 3
_NEAR_BLACK_MEAN_MAX = 5.0
_NEAR_BLACK_P99_MAX = 12.0


class CameraManager:

    def __init__(self, config_file=CONFIG_FILE, capture_factory=None):
        self.cameras = {}
        self.mapping = {}
        self._state_lock = threading.RLock()
        self._role_locks = {}
        self._closed = False
        self._failed_reason = None
        self._config_file = config_file
        self._capture_factory = capture_factory or self._open_capture
        self._io_lock = threading.Lock()
        self.load_config()
        self._role_locks = {role: threading.Lock() for role in self.mapping}
        self.open_cameras()

    # ---------- инициализация ----------

    @staticmethod
    def _open_capture(camera_id):
        return cv2.VideoCapture(camera_id)

    def load_config(self):
        try:
            with open(self._config_file, encoding="utf-8") as stream:
                mapping = json.load(stream)
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Файл {self._config_file} не найден. Запусти калибровку."
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Ошибка чтения {self._config_file}: {exc}"
            ) from exc

        if not isinstance(mapping, dict):
            raise RuntimeError("camera_mapping.json должен содержать объект")
        missing = set(_REQUIRED_ROLES) - set(mapping)
        extra = set(mapping) - set(_REQUIRED_ROLES)
        if missing or extra:
            raise RuntimeError(
                "Неверный набор камер: "
                f"missing={sorted(missing)}, extra={sorted(extra)}"
            )
        ids = list(mapping.values())
        if any(type(i) is not int or i < 0 for i in ids):
            raise RuntimeError("Индексы камер должны быть неотрицательными int")
        if len(ids) != len(set(ids)):
            raise RuntimeError("Индексы камер должны быть уникальными")
        self.mapping = mapping

        print("Конфигурация камер:")
        for role, cam_id in self.mapping.items():
            print(f"  {role} -> {cam_id}")

    def open_cameras(self):
        """Создать VideoCapture для каждой роли и проверить isOpened()."""
        started = time.monotonic()
        opened = {}

        for role, cam_id in self.mapping.items():
            cap = None
            try:
                print(f"[CAMERA] Открытие {role} id={cam_id}")
                cap = self._capture_factory(cam_id)
                if cap is None or not cap.isOpened():
                    raise RuntimeError("VideoCapture не открыл устройство")
                opened[role] = cap
                try:
                    self._configure_capture(cap)
                except Exception as exc:
                    print(f"[CAMERA] {role}: настройки не применены: {exc}")
            except Exception as exc:
                if cap is not None:
                    try:
                        cap.release()
                    except Exception:
                        pass
                for opened_cap in opened.values():
                    try:
                        opened_cap.release()
                    except Exception:
                        pass
                raise RuntimeError(
                    f"Ошибка открытия {role} (камера {cam_id}): {exc}"
                ) from exc

        self.cameras = opened
        elapsed = time.monotonic() - started
        print(f"Открыто камер: {len(self.cameras)} за {elapsed:.1f} с")

    # ---------- формат ----------

    @staticmethod
    def _configure_capture(cap):
        """Запросить MJPG, 1280x720 и минимальный буфер драйвера."""
        mjpg = cv2.VideoWriter_fourcc(*"MJPG")
        cap.set(cv2.CAP_PROP_FOURCC, mjpg)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, _EXPECTED_SIZE[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, _EXPECTED_SIZE[1])
        cap.set(cv2.CAP_PROP_FPS, _REQUESTED_FPS)
        if hasattr(cv2, "CAP_PROP_BUFFERSIZE"):
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    # ---------- захват ----------

    def capture_all(self) -> dict:
        results = self.capture_roles(_REQUIRED_ROLES)
        if set(results) != set(_REQUIRED_ROLES):
            self._latch_failure("incomplete camera result")
            raise RuntimeError(f"Неполный набор кадров: {sorted(results)}")
        return results

    def capture_roles(self, roles) -> dict:
        """Прочитать указанные камеры строго по одной, в заданном порядке."""
        requested = tuple(dict.fromkeys(roles))
        if not requested:
            return {}
        unknown = set(requested) - set(self.cameras)
        if unknown:
            raise RuntimeError(f"Неизвестные камеры: {sorted(unknown)}")
        self._ensure_usable()
        results = {}
        for role in requested:
            results[role] = self._read_role(role)
        return results

    def capture_single(self, role: str):
        return self.capture_roles((role,))[role]

    def drain_buffers(self, roles=None):
        """Сбросить устаревшие кадры из внутреннего буфера драйвера."""
        requested = tuple(dict.fromkeys(roles or self.cameras.keys()))
        if not requested:
            return
        unknown = set(requested) - set(self.cameras)
        if unknown:
            raise RuntimeError(f"Неизвестные камеры: {sorted(unknown)}")
        self._ensure_usable()
        for role in requested:
            self._drain_role(role)

    def _read_with_timeout(self, role: str, cap):
        """``cap.read()`` с настоящим ограничением по времени.

        Драйвер USB умеет зависать в ``read()`` неограниченно долго. Прямой
        вызов заблокировал бы ``_io_lock`` вместе со всей линией, поэтому
        чтение выполняется в отдельном потоке: по истечении
        ``_CAPTURE_TIMEOUT`` менеджер защёлкивает отказ и отдаёт управление,
        не дожидаясь драйвера. Зависший поток остаётся daemon-ом и завершится
        вместе с процессом; освобождать камеру повторно нельзя, пока он
        держит устройство, — CameraManager после этого всё равно заблокирован.
        """
        result: queue.Queue = queue.Queue(maxsize=1)

        def worker():
            try:
                result.put(cap.read())
            except Exception as exc:                     # noqa: BLE001
                result.put(exc)

        thread = threading.Thread(
            target=worker,
            name=f"camera-read-{role}",
            daemon=True,
        )
        thread.start()
        try:
            outcome = result.get(timeout=_CAPTURE_TIMEOUT)
        except queue.Empty:
            self._latch_failure(f"{role}: capture timeout")
            raise RuntimeError(
                f"{role}: capture timeout ({_CAPTURE_TIMEOUT}s)"
            ) from None
        if isinstance(outcome, BaseException):
            self._latch_failure(f"{role}: read failed: {outcome}")
            raise RuntimeError(f"{role}: read failed: {outcome}") from outcome
        return outcome

    def _read_role(self, role: str):
        """Один последовательный USB-read указанной роли."""
        cap = self.cameras.get(role)
        if cap is None:
            raise RuntimeError(f"Камера {role} не найдена")
        with self._io_lock:
            with self._role_locks[role]:
                ok, frame = self._read_with_timeout(role, cap)
        if not ok or frame is None:
            self._latch_failure(f"{role}: read returned no frame")
            raise RuntimeError(f"{role}: read returned no frame")
        error = self._frame_error(frame)
        if error is not None:
            self._latch_failure(f"{role}: {error}")
            raise RuntimeError(f"{role}: {error}")
        return frame

    def _drain_role(self, role: str):
        cap = self.cameras.get(role)
        if cap is None:
            return
        lock = self._role_locks.get(role)
        with self._io_lock:
            for _ in range(_BUFFER_DRAIN_COUNT):
                try:
                    if lock is not None:
                        with lock:
                            self._read_with_timeout(role, cap)
                    else:
                        self._read_with_timeout(role, cap)
                except Exception:
                    # Сброс буфера — best-effort, но зависшая камера не
                    # должна получить ещё один параллельный read поверх
                    # уже висящего: прекращаем дренаж этой роли.
                    break

    # ---------- завершение ----------

    def release(self):
        with self._state_lock:
            self._closed = True
        for cap in list(self.cameras.values()):
            try:
                cap.release()
            except Exception as exc:
                print(f"[CAMERA] Ошибка освобождения камеры: {exc}")
        self.cameras.clear()

    def _latch_failure(self, reason: str):
        with self._state_lock:
            if self._failed_reason is None:
                self._failed_reason = reason

    def _ensure_usable(self):
        with self._state_lock:
            if self._closed:
                raise RuntimeError("CameraManager уже закрыт")
            if self._failed_reason is not None:
                raise RuntimeError(
                    "CameraManager заблокирован после ошибки: "
                    f"{self._failed_reason}"
                )

    # ---------- валидация кадра ----------

    @staticmethod
    def _frame_error(frame):
        array = np.asarray(frame)
        if array.ndim != 3 or array.shape[2] < 3:
            return f"invalid frame shape: {array.shape}"
        height, width = array.shape[:2]
        if (width, height) != _EXPECTED_SIZE:
            return (
                f"invalid resolution {width}x{height}; "
                f"expected {_EXPECTED_SIZE[0]}x{_EXPECTED_SIZE[1]}"
            )
        sample = array[::12, ::12, :3].astype(np.float32)
        luminance = sample.mean(axis=2)
        mean = float(luminance.mean())
        p99 = float(np.percentile(luminance, 99))
        if mean <= _NEAR_BLACK_MEAN_MAX and p99 <= _NEAR_BLACK_P99_MAX:
            return f"near-black frame: mean={mean:.2f}, p99={p99:.2f}"
        return None
