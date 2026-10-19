"""Ленивые импорты ``vision`` и чистые хелперы калибровщика камер.

``vision/__init__`` отдаёт классы через ``__getattr__`` без тяжёлых
импортов; калибровщик (``camera_calibration_console``) проверяется
на уровне функций: открытие, формат, сканирование, атомарная запись
mapping.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

import numpy as np

# vision.vision_cluster требует ultralytics; в тестах — заглушка.
_FAKE_ULTRALYTICS = types.ModuleType("ultralytics")
_FAKE_ULTRALYTICS.YOLO = object
sys.modules.setdefault("ultralytics", _FAKE_ULTRALYTICS)

import vision  # noqa: E402
import vision.camera_calibration_console as calib  # noqa: E402


class VisionLazyImportsTest(unittest.TestCase):
    def test_unknown_attribute_raises(self):
        with self.assertRaises(AttributeError):
            vision.no_such_name

    def test_camera_manager_lazy(self):
        with mock.patch.dict("sys.modules"):
            from vision.camera_manager import CameraManager
            self.assertIs(vision.CameraManager, CameraManager)

    def test_vision_cluster_lazy(self):
        with mock.patch.dict("sys.modules"):
            from vision.vision_cluster import VisionCluster
            self.assertIs(vision.VisionCluster, VisionCluster)

    def test_model_config_lazy(self):
        with mock.patch.dict("sys.modules"):
            from vision.model_config import MODEL_GROUPS
            self.assertIs(vision.MODEL_GROUPS, MODEL_GROUPS)
            self.assertIs(vision.ROLE_TO_GROUP,
                          __import__(
                              "vision.model_config", fromlist=["ROLE_TO_GROUP"],
                          ).ROLE_TO_GROUP)

    def test_calibrators_lazy(self):
        from vision.camera_calibration_console import (
            calibrate_cameras,
            launch_camera_calibrator,
        )
        self.assertIs(vision.calibrate_cameras, calibrate_cameras)
        self.assertIs(vision.launch_camera_calibrator, launch_camera_calibrator)

    def test_live_monitor_lazy(self):
        from vision.ui import LiveMonitor
        self.assertIs(vision.LiveMonitor, LiveMonitor)


class CalibrationHelpersTest(unittest.TestCase):
    def test_open_capture(self):
        with mock.patch("vision.camera_calibration_console.cv2.VideoCapture") as vc:
            calib._open_capture(3)
            vc.assert_called_once_with(3)

    def test_open_capture_error(self):
        with mock.patch(
            "vision.camera_calibration_console.cv2.VideoCapture",
            side_effect=RuntimeError("boom"),
        ):
            self.assertIsNone(calib._open_capture(1))

    def test_configure_capture(self):
        capture = mock.Mock()
        calib._configure_capture(capture)
        self.assertTrue(capture.set.called)

    def test_frame_error_valid(self):
        frame = np.full((720, 1280, 3), 100, dtype=np.uint8)
        self.assertIsNone(calib._frame_error(frame))

    def test_frame_error_cases(self):
        self.assertIn("неверная форма", calib._frame_error(np.zeros((5, 5))))
        self.assertIn("разрешение", calib._frame_error(
            np.zeros((10, 10, 3), dtype=np.uint8),
        ))
        self.assertIn("почти чёрный", calib._frame_error(
            np.zeros((720, 1280, 3), dtype=np.uint8),
        ))

    def test_grab_preview_frame(self):
        cap = mock.Mock()
        cap.read.return_value = (
            True, np.full((720, 1280, 3), 100, dtype=np.uint8),
        )
        frame, error = calib._grab_preview_frame(cap, attempts=2)
        self.assertIsNotNone(frame)
        self.assertIsNone(error)

    def test_grab_preview_frame_failure(self):
        cap = mock.Mock()
        cap.read.return_value = (False, None)
        frame, error = calib._grab_preview_frame(cap, attempts=2)
        self.assertIsNone(frame)
        self.assertIn("не вернула", error)

    def test_safe_release(self):
        capture = mock.Mock()
        calib._safe_release(capture)
        capture.release.assert_called_once()
        calib._safe_release(None)

    def test_scan_working_cameras(self):
        created = {}

        class Cap:
            def __init__(self, camera_id):
                self.camera_id = camera_id
                self.opened = camera_id != 1
                self.set_calls = 0
                self.released = 0
                created[camera_id] = self

            def isOpened(self):
                return self.opened

            def set(self, *args):
                self.set_calls += 1

            def release(self):
                self.released += 1

        def factory(camera_id):
            return Cap(camera_id)

        pool, failures = calib._scan_working_cameras(3, factory)
        self.assertEqual(set(pool), {0, 2})
        self.assertEqual(set(failures), {1})
        self.assertEqual(failures[1], "устройство не открылось")
        self.assertEqual(created[1].released, 1)
        calib._release_camera_pool(pool)
        self.assertEqual(pool, {})
        self.assertEqual(created[0].released, 1)

    def test_scan_working_cameras_exception(self):
        def factory(camera_id):
            raise RuntimeError("usb error")

        pool, failures = calib._scan_working_cameras(2, factory)
        self.assertEqual(pool, {})
        self.assertIn("RuntimeError", failures[0])

    def test_format_scan_failures(self):
        self.assertEqual(calib._format_scan_failures({}), "")
        text = calib._format_scan_failures({1: "x", 2: "y"})
        self.assertIn("Camera ID 1: x", text)
        text = calib._format_scan_failures(
            {i: f"e{i}" for i in range(10)}, limit=3,
        )
        self.assertIn("и ещё 7 камер", text)

    def test_release_camera_pool(self):
        caps = {0: mock.Mock(), 1: mock.Mock()}
        pool = dict(caps)
        calib._release_camera_pool(pool)
        self.assertEqual(pool, {})
        caps[0].release.assert_called_once()
        caps[1].release.assert_called_once()


class AtomicWriteMappingTest(unittest.TestCase):
    def test_atomic_write_mapping(self):
        mapping = {"LEFT": 0, "MIDDLE": 1, "RIGHT": 2}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sub", "camera_mapping.json")
            result = calib.atomic_write_mapping(path, mapping)
            self.assertEqual(result["RIGHT"], 2)
            with open(path, encoding="utf-8") as stream:
                saved = json.load(stream)
            self.assertEqual(len(saved), 3)
            self.assertFalse(os.path.exists(path + ".tmp"))

    def test_atomic_write_mapping_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "camera_mapping.json")
            with self.assertRaises(ValueError):
                calib.atomic_write_mapping(path, {"TOP": 0})


if __name__ == "__main__":
    unittest.main()
