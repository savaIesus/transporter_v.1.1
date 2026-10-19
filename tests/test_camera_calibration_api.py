"""Мастер калибровки камер: пошаговое назначение ролей.

``CameraCalibrationApi`` проверяется на фейковых камерах: полный цикл
сканирование → предпросмотр → назначение 3 ролей → сохранение mapping,
плюс все ветки ошибок и отмены.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import numpy as np

import vision.camera_calibration_console as calib


def good_frame():
    return np.full((720, 1280, 3), 128, dtype=np.uint8)


class FakeCap:
    def __init__(self, camera_id, opened=True, bad_frame=False):
        self.camera_id = camera_id
        self.opened = opened
        self.bad_frame = bad_frame
        self.released = 0

    def isOpened(self):
        return self.opened

    def set(self, *args):
        pass

    def read(self):
        if self.bad_frame:
            return True, np.zeros((10, 10), dtype=np.uint8)
        return True, good_frame()

    def release(self):
        self.released += 1


def make_factory(working_ids):
    def factory(camera_id):
        if camera_id in working_ids:
            return FakeCap(camera_id)
        return FakeCap(camera_id, opened=False)

    return factory


class CameraCalibrationApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = os.path.join(self.tmp.name, "camera_mapping.json")

    def tearDown(self):
        self.tmp.cleanup()

    def make_api(self, working_ids, scan_limit=10, **kwargs):
        return calib.CameraCalibrationApi(
            self.config,
            scan_limit=scan_limit,
            capture_factory=make_factory(working_ids),
            **kwargs,
        )

    def test_scan_success(self):
        api = self.make_api(set(range(3)))
        state = api.scan()
        self.assertEqual(state["status"], "READY")
        self.assertEqual(state["found"], 3)
        self.assertEqual(state["current_role"], calib.ROLE_ORDER[0])
        self.assertEqual(state["step"], 1)

    def test_scan_not_enough_cameras(self):
        api = self.make_api({0, 1})
        state = api.scan()
        self.assertEqual(state["status"], "ERROR")
        self.assertIn("Открылось камер: 2/3", state["error"])
        # Доступные камеры остаются известными мастеру.
        self.assertEqual(state["free_camera_ids"], [0, 1])

    def test_scan_exception(self):
        api = self.make_api(set(range(3)))
        with mock.patch(
            "vision.camera_calibration_console._scan_working_cameras",
            side_effect=RuntimeError("usb failure"),
        ):
            api.scan()
        self.assertEqual(api.status, "ERROR")
        self.assertIn("Ошибка поиска камер", api.error)

    def test_scan_after_close(self):
        api = self.make_api(set(range(3)))
        api.shutdown()
        api.scan()
        self.assertTrue(api.closed)

    def test_scan_keeps_available_after_error(self):
        api = self.make_api({0, 1})
        api.scan()
        self.assertEqual(api.status, "ERROR")
        self.assertEqual(api.available_cameras, [0, 1])

    def test_rescan_from_error(self):
        api = self.make_api(set(range(3)))
        api.scan()
        api._release_all_captures_locked()
        api.status = "ERROR"
        state = api.rescan()
        self.assertEqual(state["status"], "SCANNING")
        # Фоновый поток завершится сам; дожидаемся.
        api._scan_thread.join(5.0)
        self.assertEqual(api.status, "READY")

    def test_rescan_only_from_error(self):
        api = self.make_api(set(range(3)))
        api.scan()
        state = api.rescan()
        self.assertEqual(state["status"], "READY")

    def test_next_previous_camera(self):
        api = self.make_api(set(range(3)))
        api.scan()
        state = api.next_camera()
        self.assertEqual(state["candidate_position"], 2)
        state = api.previous_camera()
        self.assertEqual(state["candidate_position"], 1)

    def test_move_candidate_without_free_cameras(self):
        api = self.make_api(set(range(3)))
        api.scan()
        api.assignments = {role: i for i, role in enumerate(calib.ROLE_ORDER)}
        with self.assertRaisesRegex(RuntimeError, "Нет свободной камеры"):
            api.next_camera()

    def test_assign_requires_preview(self):
        api = self.make_api(set(range(3)))
        api.scan()
        with self.assertRaisesRegex(RuntimeError, "живого кадра"):
            api.assign_current()

    def test_full_assignment_flow(self):
        api = self.make_api(set(range(3)))
        api.scan()
        for index in range(3):
            frame = api.get_frame()
            self.assertTrue(frame["ok"], frame)
            state = api.assign_current()
            if index < 2:
                self.assertEqual(state["status"], "READY")
            else:
                self.assertEqual(state["status"], "REVIEW")
        self.assertEqual(len(api.assignments), 3)
        self.assertTrue(all(
            role in api.assignments for role in calib.ROLE_ORDER
        ))

    def test_get_frame_before_scan(self):
        api = self.make_api(set(range(3)))
        result = api.get_frame()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "preview unavailable")

    def test_get_frame_camera_failure(self):
        caps = {}

        class BrokenCap(FakeCap):
            def read(self):
                return False, None

        def factory(camera_id):
            cap = BrokenCap(camera_id)
            caps[camera_id] = cap
            return cap

        api = calib.CameraCalibrationApi(
            self.config, scan_limit=3, capture_factory=factory,
        )
        api.scan()
        self.assertEqual(api.status, "READY")
        result = api.get_frame()
        self.assertFalse(result["ok"])
        self.assertEqual(api.status, "ERROR")
        self.assertIn("потеряла валидный кадр", api.error)
        self.assertEqual(caps[0].released, 1)

    def test_back(self):
        api = self.make_api(set(range(3)))
        api.scan()
        api.get_frame()
        api.assign_current()
        state = api.back()
        self.assertEqual(state["status"], "READY")
        self.assertEqual(state["step"], 1)
        self.assertEqual(api.assignments, {})

    def test_back_when_idle(self):
        api = self.make_api(set(range(3)))
        api.scan()
        state = api.back()
        self.assertEqual(state["status"], "READY")
        self.assertEqual(state["step"], 1)

    def test_save_flow(self):
        api = self.make_api(set(range(3)))
        api.scan()
        for _ in range(3):
            api.get_frame()
            api.assign_current()
        state = api.save()
        self.assertEqual(state["status"], "SAVED")
        self.assertTrue(state["saved"])
        self.assertTrue(os.path.exists(self.config))
        self.assertTrue(api.finish())

    def test_finish_without_save(self):
        api = self.make_api(set(range(3)))
        self.assertFalse(api.finish())

    def test_save_error_sets_error(self):
        api = self.make_api(set(range(3)))
        api.scan()
        for _ in range(3):
            api.get_frame()
            api.assign_current()
        with mock.patch(
            "vision.camera_calibration_console.atomic_write_mapping",
            side_effect=OSError("disk full"),
        ):
            state = api.save()
        self.assertEqual(state["status"], "ERROR")
        self.assertIn("Не удалось сохранить mapping", state["error"])

    def test_cancel(self):
        api = self.make_api(set(range(3)))
        api.scan()
        calls = []
        api.set_close_callback(lambda: calls.append(1))
        self.assertTrue(api.cancel())
        self.assertEqual(api.status, "CANCELLED")
        self.assertTrue(api.closed)
        self.assertEqual(calls, [1])

    def test_shutdown_releases(self):
        api = self.make_api(set(range(3)))
        api.scan()
        api.shutdown()
        self.assertTrue(api.closed)
        self.assertEqual(api._captures, {})

    def test_state_shape(self):
        api = self.make_api(set(range(3)))
        api.scan()
        state = api.get_state()
        for key in ("status", "found", "required", "current_role",
                    "assignments", "roles", "saved", "config_path"):
            self.assertIn(key, state)
        self.assertEqual(len(state["roles"]), 3)
        self.assertEqual(state["roles"][0]["status"], "current")

    def test_state_after_assignment_rows(self):
        api = self.make_api(set(range(3)))
        api.scan()
        api.get_frame()
        api.assign_current()
        state = api.get_state()
        self.assertEqual(state["roles"][0]["status"], "assigned")
        self.assertEqual(state["roles"][1]["status"], "current")

    def test_set_close_callback(self):
        api = self.make_api(set(range(3)))

        def callback():
            return None

        api.set_close_callback(callback)
        self.assertIs(api._close_callback, callback)


if __name__ == "__main__":
    unittest.main()
