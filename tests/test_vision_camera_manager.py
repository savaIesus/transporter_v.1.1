"""CameraManager: открытие, захват, drain, ошибки и блокировка после сбоя.

Реальные USB-камеры заменяются фабрикой фейковых ``VideoCapture``,
конфигурация пишется во временный ``camera_mapping.json``.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

import numpy as np

from vision import camera_manager
from vision.camera_manager import CameraManager

ROLES = (
    "LEFT", "MIDDLE", "RIGHT",
)


def mapping():
    return {role: index for index, role in enumerate(ROLES)}


def good_frame():
    return np.full((720, 1280, 3), 128, dtype=np.uint8)


class FakeCapture:
    def __init__(self, camera_id, frame=None, opened=True, reads=None,
                 fail_reads=()):
        self.camera_id = camera_id
        self.frame = frame if frame is not None else good_frame()
        self.opened = opened
        self.reads = reads if reads is not None else []
        self.fail_reads = set(fail_reads)
        self.released = False
        self.settings = []

    def isOpened(self):
        return self.opened

    def set(self, prop, value):
        self.settings.append((prop, value))

    def read(self):
        self.reads.append(self.camera_id)
        if self.camera_id in self.fail_reads:
            return False, None
        return True, self.frame.copy()

    def release(self):
        self.released = True


class FakeCaptureFactory:
    def __init__(self, captures):
        self.captures = dict(captures)

    def __call__(self, camera_id):
        return self.captures[camera_id]


class CameraManagerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = os.path.join(self.tmp.name, "camera_mapping.json")
        with open(self.config, "w", encoding="utf-8") as stream:
            json.dump(mapping(), stream)

    def tearDown(self):
        self.tmp.cleanup()

    def make_manager(self, captures=None):
        if captures is None:
            captures = {
                index: FakeCapture(index) for index in range(3)
            }
        factory = FakeCaptureFactory(captures)
        manager = CameraManager(
            config_file=self.config,
            capture_factory=factory,
        )
        return manager, factory

    def test_opens_all_cameras(self):
        manager, factory = self.make_manager()
        self.assertEqual(set(manager.cameras), set(ROLES))
        self.assertEqual(manager.mapping, mapping())

    def test_capture_all_returns_all_roles(self):
        manager, _ = self.make_manager()
        frames = manager.capture_all()
        self.assertEqual(set(frames), set(ROLES))
        self.assertEqual(frames["RIGHT"].shape, (720, 1280, 3))

    def test_capture_roles_order_and_single(self):
        manager, factory = self.make_manager()
        frames = manager.capture_roles(("RIGHT", "LEFT"))
        self.assertEqual(list(frames), ["RIGHT", "LEFT"])
        frame = manager.capture_single("RIGHT")
        self.assertEqual(frame.shape, (720, 1280, 3))

    def test_capture_roles_empty(self):
        manager, _ = self.make_manager()
        self.assertEqual(manager.capture_roles(()), {})

    def test_capture_unknown_role(self):
        manager, _ = self.make_manager()
        with self.assertRaisesRegex(RuntimeError, "Неизвестные камеры"):
            manager.capture_roles(("NOPE",))

    def test_drain_buffers(self):
        captures = {index: FakeCapture(index) for index in range(3)}
        manager, _ = self.make_manager(captures)
        manager.drain_buffers(("RIGHT",))
        self.assertGreaterEqual(len(captures[2].reads), 3)
        manager.drain_buffers()
        self.assertGreaterEqual(len(captures[0].reads), 3)

    def test_read_failure_latches_and_blocks(self):
        captures = {index: FakeCapture(index) for index in range(3)}
        captures[2].fail_reads.add(2)
        manager, _ = self.make_manager(captures)
        with self.assertRaisesRegex(RuntimeError, "read returned no frame"):
            manager.capture_all()
        with self.assertRaisesRegex(RuntimeError, "заблокирован"):
            manager.capture_single("RIGHT")

    def test_invalid_resolution_rejected(self):
        captures = {index: FakeCapture(index) for index in range(3)}
        captures[1].frame = np.zeros((100, 100, 3), dtype=np.uint8)
        manager, _ = self.make_manager(captures)
        with self.assertRaisesRegex(RuntimeError, "invalid resolution"):
            manager.capture_single("MIDDLE")

    def test_left_black_frame_rejected(self):
        captures = {index: FakeCapture(index) for index in range(3)}
        captures[1].frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        manager, _ = self.make_manager(captures)
        with self.assertRaisesRegex(RuntimeError, "near-black"):
            manager.capture_single("MIDDLE")

    def test_hanging_read_times_out_and_latches(self):
        # Регрессия: таймаут проверялся ПОСЛЕ блокирующего cap.read(), то
        # есть зависший USB-драйвер держал _io_lock бесконечно и вешал
        # и live-просмотр, и производственный шаг.
        class HangingCapture(FakeCapture):
            def read(self):
                threading.Event().wait()

        captures = {index: FakeCapture(index) for index in range(3)}
        captures[2] = HangingCapture(2)
        manager, _ = self.make_manager(captures)

        with mock.patch.object(camera_manager, "_CAPTURE_TIMEOUT", 0.2):
            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, "capture timeout"):
                manager.capture_single("RIGHT")
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 2.0)
        # Отказ защёлкнут, а io_lock свободен: менеджер отвечает сразу.
        with self.assertRaisesRegex(RuntimeError, "заблокирован"):
            manager.capture_single("LEFT")

    def test_read_exception_is_wrapped(self):
        class ExplodingCapture(FakeCapture):
            def read(self):
                raise OSError("USB device disconnected")

        captures = {index: FakeCapture(index) for index in range(3)}
        captures[2] = ExplodingCapture(2)
        manager, _ = self.make_manager(captures)
        with self.assertRaisesRegex(RuntimeError, "read failed"):
            manager.capture_single("RIGHT")

    def test_release_closes_cameras(self):
        captures = {index: FakeCapture(index) for index in range(3)}
        manager, _ = self.make_manager(captures)
        manager.release()
        self.assertTrue(all(captures[index].released for index in range(3)))
        self.assertEqual(manager.cameras, {})
        with self.assertRaises(RuntimeError):
            manager.capture_all()

    def test_open_failure_releases_opened(self):
        captures = {index: FakeCapture(index) for index in range(3)}
        captures[2].opened = False
        factory = FakeCaptureFactory(captures)
        with self.assertRaisesRegex(RuntimeError, "Ошибка открытия"):
            CameraManager(config_file=self.config, capture_factory=factory)
        self.assertTrue(captures[0].released)
        self.assertTrue(captures[1].released)

    def test_config_missing_file(self):
        with self.assertRaisesRegex(RuntimeError, "не найден"):
            CameraManager(config_file=os.path.join(self.tmp.name, "nope.json"))

    def test_config_invalid_json(self):
        path = os.path.join(self.tmp.name, "bad.json")
        with open(path, "w", encoding="utf-8") as stream:
            stream.write("{bad")
        with self.assertRaisesRegex(RuntimeError, "Ошибка чтения"):
            CameraManager(config_file=path)

    def test_config_bad_roles(self):
        path = os.path.join(self.tmp.name, "bad_roles.json")
        with open(path, "w", encoding="utf-8") as stream:
            json.dump({"RIGHT": 0}, stream)
        with self.assertRaisesRegex(RuntimeError, "Неверный набор камер"):
            CameraManager(config_file=path)

    def test_config_duplicate_ids(self):
        data = mapping()
        data["RIGHT"] = 0
        path = os.path.join(self.tmp.name, "dup.json")
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(data, stream)
        with self.assertRaisesRegex(RuntimeError, "уникальными"):
            CameraManager(config_file=path)

    def test_config_non_int_id(self):
        data = mapping()
        data["RIGHT"] = "2"
        path = os.path.join(self.tmp.name, "str.json")
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(data, stream)
        with self.assertRaisesRegex(RuntimeError, "неотрицательными int"):
            CameraManager(config_file=path)

    def test_configure_capture_called(self):
        captures = {index: FakeCapture(index) for index in range(3)}
        manager, _ = self.make_manager(captures)
        self.assertTrue(captures[0].settings)

    def test_frame_error_validation(self):
        self.assertIsNone(CameraManager._frame_error(good_frame()))
        error = CameraManager._frame_error(np.zeros((10, 10), dtype=np.uint8))
        self.assertIn("invalid frame shape", error)


if __name__ == "__main__":
    unittest.main()
