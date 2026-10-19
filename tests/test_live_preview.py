"""Live-просмотр: gate камер и фоновые потоки публикации кадров.

``LiveCaptureGate`` проверяется без потоков (пауза, вложенные паузы,
сброс, занятие слотов чтения), ``LivePreview`` — с фейковыми камерами
и монитором, чтобы проверить реальные потоки и их остановку.
"""

from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

import numpy as np

from core.live_preview import (
    LiveCaptureGate,
    LivePreview,
)


def make_frame():
    return np.zeros((16, 16, 3), dtype=np.uint8)


class GateTest(unittest.TestCase):
    def test_pause_resume_roundtrip(self):
        gate = LiveCaptureGate()
        self.assertTrue(gate.pause(timeout=1.0))
        with gate.live_read("TOP") as allowed:
            self.assertFalse(allowed)
        gate.resume()
        with gate.live_read("TOP") as allowed:
            self.assertTrue(allowed)

    def test_nested_pause_depth(self):
        gate = LiveCaptureGate()
        self.assertTrue(gate.pause())
        self.assertTrue(gate.pause())
        gate.resume()
        # Глубина ещё 1: live остаётся на паузе.
        with gate.live_read("TOP") as allowed:
            self.assertFalse(allowed)
        gate.resume()
        with gate.live_read("TOP") as allowed:
            self.assertTrue(allowed)

    def test_reset_clears_depth(self):
        gate = LiveCaptureGate()
        gate.pause()
        gate.pause()
        gate.reset()
        with gate.live_read("TOP") as allowed:
            self.assertTrue(allowed)

    def test_live_read_acquires_slot(self):
        gate = LiveCaptureGate()
        with gate.live_read("TOP") as allowed:
            self.assertTrue(allowed)
            self.assertEqual(gate._active_reads, 1)
        self.assertEqual(gate._active_reads, 0)

    def test_live_reads_roles(self):
        gate = LiveCaptureGate()
        with gate.live_reads(("A", "B", "A")) as roles:
            self.assertEqual(roles, ("A", "B"))

    def test_live_reads_empty(self):
        gate = LiveCaptureGate()
        with gate.live_reads(()) as roles:
            self.assertEqual(roles, ())

    def test_live_reads_blocked_on_pause(self):
        gate = LiveCaptureGate()
        gate.pause()
        with gate.live_reads(("A", "B")) as roles:
            self.assertEqual(roles, ())
        gate.resume()

    def test_pause_timeout_returns_false(self):
        gate = LiveCaptureGate()
        with gate.live_read("TOP"):
            self.assertFalse(gate.pause(timeout=0.05))
        # После таймаута пауза снята.
        with gate.live_read("TOP") as allowed:
            self.assertTrue(allowed)

    def test_pause_waits_for_active_read(self):
        gate = LiveCaptureGate()
        release = threading.Event()

        def hold_read():
            with gate.live_read("TOP"):
                release.wait(1.0)

        thread = threading.Thread(target=hold_read)
        thread.start()
        time.sleep(0.05)
        paused = gate.pause(timeout=1.0)
        release.set()
        thread.join(1.0)
        self.assertTrue(paused)


class FakeLiveCameras:
    def __init__(self, roles=("TOP", "INPUT_LEFT", "INPUT_RIGHT"),
                 fail=False):
        self.mapping = {role: index for index, role in enumerate(roles)}
        self.fail = fail
        self.reads = []

    def capture_single(self, role):
        self.reads.append(("single", role))
        if self.fail:
            raise RuntimeError("camera read failed")
        return make_frame()

    def capture_roles(self, roles):
        self.reads.append(("roles", tuple(roles)))
        if self.fail:
            raise RuntimeError("camera read failed")
        return {role: make_frame() for role in roles}

    def drain_buffers(self, roles=None):
        pass


class BlockingLiveCameras:
    """Камера, чей первый read остаётся внутри системного вызова."""

    def __init__(self):
        self.mapping = {"TOP": 0}
        self.read_started = threading.Event()
        self.release_read = threading.Event()
        self.read_count = 0

    def capture_single(self, _role):
        self.read_count += 1
        self.read_started.set()
        self.release_read.wait(2.0)
        return make_frame()

    @staticmethod
    def capture_roles(_roles):
        return {}


class FakeLiveMonitor:
    def __init__(self):
        self.updates = []
        self.active_camera_role = "TOP"

    def update(self, **kwargs):
        self.updates.append(kwargs)


class LivePreviewTest(unittest.TestCase):
    def make_preview(self, cameras=None, monitor=None):
        cameras = cameras or FakeLiveCameras()
        monitor = monitor or FakeLiveMonitor()
        preview = LivePreview(
            cameras,
            monitor,
            get_active_role=lambda: monitor.active_camera_role,
        )
        return preview, cameras, monitor

    def test_start_stop_roundtrip(self):
        preview, cameras, monitor = self.make_preview()
        self.assertFalse(preview.running)
        self.assertTrue(preview.start())
        self.assertTrue(preview.running)
        # Повторный start ничего не делает.
        self.assertFalse(preview.start())
        self.assertIsNone(preview.error)
        preview.stop()
        self.assertFalse(preview.running)
        self.assertGreater(len(monitor.updates), 0)
        self.assertGreater(len(cameras.reads), 0)

    def test_start_again_after_stop(self):
        preview, _, _ = self.make_preview()
        preview.start()
        preview.stop()
        self.assertTrue(preview.start())
        preview.stop()

    def test_wait_for_roles_confirms_first_frame_from_every_camera(self):
        preview, _, _ = self.make_preview()
        self.assertTrue(preview.start())
        self.assertEqual(
            preview.wait_for_roles(
                ("TOP", "INPUT_LEFT", "INPUT_RIGHT"), timeout=1.0,
            ),
            (),
        )
        preview.stop()

    def test_wait_for_roles_reports_role_without_frame(self):
        preview, _, _ = self.make_preview(
            cameras=FakeLiveCameras(roles=("TOP",)),
        )
        self.assertTrue(preview.start())
        self.assertEqual(
            preview.wait_for_roles(("TOP", "MISSING"), timeout=0.05),
            ("MISSING",),
        )
        preview.stop()

    def test_stop_timeout_does_not_restart_hidden_camera_reads(self):
        cameras = BlockingLiveCameras()
        preview, _, _ = self.make_preview(cameras=cameras)

        with mock.patch("core.live_preview.LIVE_THREAD_JOIN_TIMEOUT", 0.05):
            self.assertTrue(preview.start())
            self.assertTrue(cameras.read_started.wait(1.0))
            self.assertFalse(preview.stop())

        # stop() завершился по тайм-ауту, но старый поток всё ещё считается
        # живым и новая пара потоков поверх него не запускается.
        self.assertTrue(preview.running)
        self.assertFalse(preview.start())
        reads_before_release = cameras.read_count

        cameras.release_read.set()
        deadline = time.monotonic() + 1.0
        while preview.running and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)

        self.assertFalse(preview.running)
        self.assertEqual(cameras.read_count, reads_before_release)
        # Завершившаяся ссылка очищается при следующем штатном старте.
        self.assertTrue(preview.start())
        self.assertTrue(preview.stop())

    def test_camera_error_sets_error(self):
        preview, _, _ = self.make_preview(cameras=FakeLiveCameras(fail=True))
        preview.start()
        deadline = time.monotonic() + 5.0
        while preview.error is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIsNotNone(preview.error)
        self.assertIn("camera read failed", preview.error)
        preview.stop()

    def test_pause_resume(self):
        preview, _, _ = self.make_preview()
        self.assertTrue(preview.pause())
        self.assertTrue(preview.pause())
        preview.resume()
        preview.reset_pause()
        with preview.gate.live_read("TOP") as allowed:
            self.assertTrue(allowed)

    def test_clear_overlays(self):
        preview, _, monitor = self.make_preview()
        preview.clear_overlays()
        self.assertEqual(
            monitor.updates[-1],
            {"vision_results": {}, "rule_results": []},
        )

    def test_fps_zero_before_start(self):
        preview, _, _ = self.make_preview()
        self.assertEqual(preview.fps, 0.0)

    def test_stop_when_not_running(self):
        preview, _, _ = self.make_preview()
        preview.stop()
        self.assertFalse(preview.running)

    def test_active_role_fallback(self):
        preview, _, monitor = self.make_preview()
        monitor.active_camera_role = "NOT_A_ROLE"
        preview.start()
        time.sleep(0.05)
        preview.stop()
        # Фолбэк на первую доступную роль: ошибок нет.
        self.assertIsNone(preview.error)

    def test_gate_shared_with_cycle(self):
        gate = LiveCaptureGate()
        preview, _, _ = self.make_preview()
        preview.gate = gate
        self.assertIs(preview.gate, gate)


if __name__ == "__main__":
    unittest.main()
