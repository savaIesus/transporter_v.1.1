"""Консольная диагностика камер (vision.camera_diagnostic).

Камеры заменяются фабрикой фейковых ``VideoCapture``; проверяются
перебор backend-ов, проба кадров, валидация кадра, построение отчёта
и код возврата ``main``.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

import vision.camera_diagnostic as diag


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
        self.released = 0
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
        self.released += 1


class DiagTestBase(unittest.TestCase):
    def make_factory(self, captures, opened_ids=None):
        opened_ids = opened_ids if opened_ids is not None else set(captures)

        def factory(camera_id, backend=None):
            cap = captures.get(camera_id)
            if cap is None or camera_id not in opened_ids:
                return FakeCapture(camera_id, opened=False)
            return cap

        return factory


class EnvHelpersTest(unittest.TestCase):
    def tearDown(self):
        for key in ("TEST_INT", "TEST_FLOAT"):
            os.environ.pop(key, None)

    def test_env_int_default(self):
        self.assertEqual(diag._env_int("TEST_INT", 5), 5)

    def test_env_int_parsed_and_clamped(self):
        os.environ["TEST_INT"] = "10"
        self.assertEqual(diag._env_int("TEST_INT", 5, minimum=2), 10)
        os.environ["TEST_INT"] = "1"
        self.assertEqual(diag._env_int("TEST_INT", 5, minimum=2), 2)

    def test_env_int_invalid(self):
        os.environ["TEST_INT"] = "abc"
        self.assertEqual(diag._env_int("TEST_INT", 5), 5)

    def test_env_float_default_and_parse(self):
        self.assertEqual(diag._env_float("TEST_FLOAT", 0.5), 0.5)
        os.environ["TEST_FLOAT"] = "0.75"
        self.assertEqual(diag._env_float("TEST_FLOAT", 0.5), 0.75)

    def test_env_float_invalid(self):
        os.environ["TEST_FLOAT"] = "x"
        self.assertEqual(diag._env_float("TEST_FLOAT", 0.5), 0.5)


class BackendsTest(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("CAMERA_BACKENDS", None)

    def test_camera_backends_default_non_windows(self):
        if sys.platform != "win32":
            backends = diag._camera_backends()
            self.assertEqual(backends, (getattr(__import__("cv2"), "CAP_ANY", 0),))

    def test_camera_backends_configured(self):
        os.environ["CAMERA_BACKENDS"] = "any"
        backends = diag._camera_backends()
        self.assertTrue(backends)

    def test_camera_backends_unknown_token_ignored(self):
        os.environ["CAMERA_BACKENDS"] = "nope"
        backends = diag._camera_backends()
        self.assertTrue(backends)

    def test_backend_label(self):
        self.assertEqual(diag._backend_label(None), "default")
        import cv2
        label = diag._backend_label(cv2.CAP_ANY)
        self.assertEqual(label, "ANY")
        self.assertEqual(diag._backend_label(123456), "123456")


class CaptureProbeTest(unittest.TestCase):
    def test_open_capture_with_backend(self):
        with mock.patch("vision.camera_diagnostic.cv2.VideoCapture") as vc:
            diag._open_capture(3, backend=5)
            vc.assert_called_once_with(3, 5)
            diag._open_capture(4)
            vc.assert_called_with(4)

    def test_open_capture_error_returns_none(self):
        with mock.patch(
            "vision.camera_diagnostic.cv2.VideoCapture",
            side_effect=RuntimeError("boom"),
        ):
            self.assertIsNone(diag._open_capture(1))

    def test_configure_capture(self):
        cap = FakeCapture(0)
        diag._configure_capture(cap)
        self.assertTrue(cap.settings)

    def test_frame_error_valid(self):
        self.assertIsNone(diag._frame_error(good_frame()))

    def test_frame_error_bad_shape(self):
        error = diag._frame_error(np.zeros((10, 10), dtype=np.uint8))
        self.assertIn("неверная форма", error)

    def test_frame_error_bad_resolution(self):
        error = diag._frame_error(np.zeros((100, 100, 3), dtype=np.uint8))
        self.assertIn("разрешение", error)

    def test_frame_error_left_black(self):
        error = diag._frame_error(np.zeros((720, 1280, 3), dtype=np.uint8))
        self.assertIn("почти чёрный", error)

    def test_probe_capture_success(self):
        cap = FakeCapture(0)
        frame, error = diag._probe_capture(cap, attempts=3)
        self.assertIsNotNone(frame)
        self.assertIsNone(error)

    def test_probe_capture_failure(self):
        cap = FakeCapture(0, fail_reads={0})
        frame, error = diag._probe_capture(cap, attempts=2)
        self.assertIsNone(frame)
        self.assertEqual(error, "камера не вернула кадр")

    def test_probe_capture_bad_frame(self):
        cap = FakeCapture(0, frame=np.zeros((10, 10), dtype=np.uint8))
        frame, error = diag._probe_capture(cap, attempts=2)
        self.assertIsNone(frame)
        self.assertIn("неверная форма", error)

    def test_safe_release(self):
        cap = FakeCapture(0)
        diag._safe_release(cap)
        self.assertEqual(cap.released, 1)
        diag._safe_release(None)

    def test_safe_release_error_swallowed(self):
        cap = FakeCapture(0)

        def boom():
            raise RuntimeError("close failed")

        cap.release = boom
        diag._safe_release(cap)

    def test_factory_takes_backend(self):
        self.assertTrue(diag._factory_takes_backend(
            lambda a, b: None,
        ))
        self.assertTrue(diag._factory_takes_backend(
            lambda *args: None,
        ))
        self.assertFalse(diag._factory_takes_backend(
            lambda a: None,
        ))
        self.assertFalse(diag._factory_takes_backend(42))

    def test_probe_backend_success(self):
        cap = FakeCapture(0)
        entry = diag._probe_backend(
            0, None, lambda cid: cap, attempts=3,
        )
        self.assertTrue(entry["ok"])
        self.assertEqual(entry["detail"], "1280x720")
        self.assertIn("frame", entry)

    def test_probe_backend_not_opened(self):
        entry = diag._probe_backend(
            0, None, lambda cid: FakeCapture(0, opened=False), attempts=3,
        )
        self.assertFalse(entry["ok"])
        self.assertEqual(entry["detail"], "устройство не открылось")

    def test_probe_backend_exception(self):
        def factory(cid):
            raise RuntimeError("driver crash")

        entry = diag._probe_backend(0, None, factory, attempts=3)
        self.assertFalse(entry["ok"])
        self.assertIn("RuntimeError", entry["detail"])

    def test_probe_backend_releases_capture(self):
        cap = FakeCapture(0)
        diag._probe_backend(0, None, lambda cid: cap, attempts=3)
        self.assertEqual(cap.released, 1)


class ScanTest(DiagTestBase):
    def test_scan_all_working(self):
        captures = {0: FakeCapture(0), 1: FakeCapture(1)}
        results = diag.scan_isolated(2, self.make_factory(captures))
        self.assertTrue(results[0]["ok"])
        self.assertTrue(results[1]["ok"])

    def test_scan_missing_camera(self):
        captures = {0: FakeCapture(0)}
        results = diag.scan_isolated(3, self.make_factory(captures))
        self.assertTrue(results[0]["ok"])
        self.assertFalse(results[1]["ok"])
        self.assertFalse(results[2]["ok"])
        self.assertIn("устройство не открылось", results[1]["detail"])

    def test_scan_bad_frame_reported(self):
        captures = {0: FakeCapture(0, frame=np.zeros((10, 10), dtype=np.uint8))}
        results = diag.scan_isolated(1, self.make_factory(captures))
        self.assertFalse(results[0]["ok"])
        self.assertIn("неверная форма", results[0]["detail"])

    def test_scan_dedups_repeated_errors(self):
        captures = {0: FakeCapture(0, fail_reads={0})}
        results = diag.scan_isolated(1, self.make_factory(captures))
        self.assertEqual(
            results[0]["detail"].count("камера не вернула кадр"), 1,
        )


class MappingTest(unittest.TestCase):
    def make_mapping(self, path):
        with open(path, "w", encoding="utf-8") as stream:
            json.dump({
                "LEFT": 0, "MIDDLE": 1, "RIGHT": 2,
            }, stream)
        return path

    def test_load_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.make_mapping(os.path.join(tmp, "m.json"))
            mapping = diag._load_mapping(path)
            self.assertEqual(len(mapping), 3)

    def test_load_mapping_missing(self):
        self.assertIsNone(diag._load_mapping("/nonexistent/m.json"))

    def test_load_mapping_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m.json")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("{broken")
            self.assertIsNone(diag._load_mapping(path))

    def test_check_mapping_ok(self):
        class FakeManager:
            def __init__(self, config_file, capture_factory):
                self.cameras = {i: object() for i in range(3)}

            def release(self):
                pass

        with mock.patch(
            "vision.camera_diagnostic.CameraManager", FakeManager,
        ):
            ok, message = diag.check_mapping("x.json", None)
        self.assertTrue(ok)
        self.assertIn("3/3", message)

    def test_check_mapping_runtime_error(self):
        class FailingManager:
            def __init__(self, config_file, capture_factory):
                raise RuntimeError("Ошибка открытия RIGHT (камера 9): x")

        with mock.patch(
            "vision.camera_diagnostic.CameraManager", FailingManager,
        ):
            ok, message = diag.check_mapping("x.json", None)
        self.assertFalse(ok)
        self.assertIn("RIGHT", message)

    def test_check_mapping_unexpected_error(self):
        class BoomManager:
            def __init__(self, config_file, capture_factory):
                raise ValueError("bad config")

        with mock.patch(
            "vision.camera_diagnostic.CameraManager", BoomManager,
        ):
            ok, message = diag.check_mapping("x.json", None)
        self.assertFalse(ok)
        self.assertIn("ValueError", message)

    def test_failed_roles_from_message(self):
        self.assertEqual(
            diag._failed_roles_from_message("Ошибка открытия RIGHT (камера 9)"),
            ["RIGHT"],
        )
        self.assertEqual(diag._failed_roles_from_message("no role"), [])

    def test_bad_mapped_roles(self):
        scan = {0: {"ok": True}, 1: {"ok": False}}
        mapping = {"LEFT": 0, "MIDDLE": 1}
        self.assertEqual(
            diag._bad_mapped_roles(scan, mapping),
            [(1, "MIDDLE")],
        )


class ReportTest(unittest.TestCase):
    def test_format_report_all_ok(self):
        scan = {0: {"ok": True, "backend": "ANY", "detail": "1280x720"}}
        report = diag._format_report(scan, None, None, "")
        self.assertIn("ДИАГНОСТИКА КАМЕР", report)
        self.assertIn("Camera ID  0: OK", report)

    def test_format_report_with_mapping_ok(self):
        scan = {i: {"ok": True, "backend": "ANY", "detail": "1280x720"}
                for i in range(3)}
        mapping = {role: i for i, role in enumerate(sorted(
            diag.REQUIRED_ROLES,
        ))}
        report = diag._format_report(scan, mapping, True, "Открыто камер: 3/3")
        self.assertIn("mapping актуален", report)
        self.assertIn("все 3 ролей открылись".lower(),
                      report.lower())

    def test_format_report_with_failed_roles(self):
        scan = {i: {"ok": True, "backend": "ANY", "detail": "1280x720"}
                for i in range(3)}
        mapping = {role: i for i, role in enumerate(sorted(
            diag.REQUIRED_ROLES,
        ))}
        report = diag._format_report(
            scan, mapping, False,
            "Ошибка открытия RIGHT (камера 9): x",
        )
        self.assertIn("не открылись роли: RIGHT", report)

    def test_format_report_missing_cameras(self):
        scan = {0: {"ok": True, "backend": "ANY", "detail": "1280x720"},
                1: {"ok": False, "backend": "ANY", "detail": "нет камеры"}}
        report = diag._format_report(scan, None, None, "")
        self.assertIn("линия не запустится", report)
        self.assertIn("Не отвечают ID: 1", report)

    def test_format_report_missing_cameras_with_mapping(self):
        scan = {0: {"ok": True, "backend": "ANY", "detail": "1280x720"},
                1: {"ok": False, "backend": "ANY", "detail": "нет камеры"}}
        mapping = {"LEFT": 0, "MIDDLE": 1}
        report = diag._format_report(scan, mapping, None, "")
        self.assertIn("Роли с неотвечающими ID", report)
        self.assertIn("MIDDLE (id 1)", report)

    def test_format_report_working_but_mapping_failed(self):
        scan = {i: {"ok": True, "backend": "ANY", "detail": "1280x720"}
                for i in range(3)}
        mapping = {role: i for i, role in enumerate(sorted(
            diag.REQUIRED_ROLES,
        ))}
        report = diag._format_report(scan, mapping, False, "какая-то ошибка")
        self.assertIn("Камеры исправны, но открытие ролей", report)


class MainTest(DiagTestBase):
    def test_main_with_working_cameras(self):
        with mock.patch.object(
            diag, "scan_isolated",
            return_value={i: {"ok": True, "backend": "ANY",
                              "detail": "1280x720"} for i in range(3)},
        ), mock.patch.object(
            diag, "check_mapping",
            return_value=(True, "Открыто камер: 3/3"),
        ), mock.patch.object(
            diag, "_load_mapping",
            return_value={role: i for i, role in enumerate(
                sorted(diag.REQUIRED_ROLES))},
        ):
            code = diag.main(["--mapping", "camera_mapping.json",
                              "--scan-limit", "7"])
        self.assertEqual(code, 0)

    def test_main_failure_code(self):
        with mock.patch.object(
            diag, "scan_isolated",
            return_value={0: {"ok": False, "backend": "ANY",
                              "detail": "нет камеры"}},
        ), mock.patch.object(
            diag, "_load_mapping", return_value=None,
        ):
            code = diag.main(["--scan-limit", "1"])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
