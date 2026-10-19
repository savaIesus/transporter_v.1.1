"""Шаг ленты из мастера обучения.

Проверяется главное требование: каждое нажатие «Завершить деталь»
завершается одним шагом ``G3 N<seq>``, подтверждённым по ``I2``, а сбой
оборудования не отменяет уже сохранённую разметку.
"""

from __future__ import annotations

import tempfile
import unittest
from unittest import mock

import numpy as np

from training.collector import CollectorError, TrainingCollector, TrainingConfig
from training.conveyor import ConveyorStepError, SerialConveyorStepper


class FakeFrameSource:
    def __init__(self):
        self.roles = []

    def capture_single(self, role):
        self.roles.append(role)
        return np.zeros((6, 8, 3), dtype=np.uint8)


class FakeStepper:
    def __init__(self, error=None, result=None):
        self.calls = 0
        self.error = error
        self.result = result if result is not None else {
            "moved": True,
            "step": 42,
            "port": "COM9",
        }

    def advance(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return dict(self.result)


class FakeTransport:
    """Транспорт с монотонным STEP, как у прошивки ``convey15.ino``."""

    def __init__(self, step: int = 10):
        self.step = step
        self.commands = []
        self.closed = False

    def send(self, command):
        self.commands.append(command)

    def query(self, command, delay=0.15):
        self.commands.append(command)
        if command == "I1":
            return "0"
        if command == "I2":
            return (
                f"MOV=0 WAIT=0 POS={self.step} TGT={self.step} "
                f"STEP={self.step} lastErr=0"
            )
        if command.startswith("G3"):
            expected = int(command.split("N", 1)[1])
            assert expected == self.step + 1, command
            self.step = expected
            return f"ACK G3 STEP={expected}"
        return ""

    def close(self):
        self.closed = True


CALIBRATION = {
    "conveyor_speed": 46000,
    "conveyor_accel": 46000,
    "normal_steps": 18815,
    "dist1_open_position": 360,
    "dist2_bad_position": 0,
    "dist2_cleanup_position": 360,
    "settle_time": 0.2,
    "axis_speed": 1000,
    "axis_accel": 500,
    "jog_hold_steps": 1_000_000,
}


class TrainingCollectorConveyorTest(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.config = TrainingConfig(
            root_folder=self._temp.name,
            camera_roles=("A", "B"),
            defect_classes=("glass", "other"),
        )

    def _finish_collected_part(self, collector, roles=("A", "B")):
        collector.begin_part()
        for _ in roles:
            collector.capture_next()
            collector.skip_current()
        return collector.finish_part()

    def test_each_finished_part_moves_conveyor_once(self):
        stepper = FakeStepper()
        collector = TrainingCollector(
            self.config,
            frame_source=FakeFrameSource(),
            conveyor=stepper,
        )
        collector.start_session("dataset")

        first = self._finish_collected_part(collector)
        self.assertEqual(stepper.calls, 1)
        self.assertTrue(first["conveyor"]["enabled"])
        self.assertTrue(first["conveyor"]["moved"])
        self.assertEqual(first["conveyor"]["step"], 42)
        self.assertEqual(first["finished_part"]["part_id"], 1)

        second = self._finish_collected_part(collector)
        self.assertEqual(stepper.calls, 2)
        self.assertTrue(second["conveyor"]["moved"])
        self.assertEqual(second["finished_part"]["part_id"], 2)

    def test_step_failure_keeps_finished_part(self):
        stepper = FakeStepper(error=RuntimeError("COM9 не отвечает"))
        collector = TrainingCollector(
            self.config,
            frame_source=FakeFrameSource(),
            conveyor=stepper,
        )
        collector.start_session("dataset")

        result = self._finish_collected_part(collector)

        self.assertEqual(stepper.calls, 1)
        self.assertFalse(result["conveyor"]["moved"])
        self.assertIn("COM9 не отвечает", result["conveyor"]["error"])
        self.assertEqual(result["finished_part"]["status"], "finished")
        self.assertIsNone(result["part"])
        self.assertIsNone(collector.current_part_id)

    def test_no_step_without_complete_part(self):
        stepper = FakeStepper()
        collector = TrainingCollector(
            self.config,
            frame_source=FakeFrameSource(),
            conveyor=stepper,
        )
        collector.start_session("dataset")
        collector.begin_part()
        collector.capture_next()
        collector.skip_current()

        with self.assertRaises(CollectorError):
            collector.finish_part()

        self.assertEqual(stepper.calls, 0)
        self.assertIsNone(collector.last_conveyor_result)

    def test_manual_step_is_allowed_only_between_parts(self):
        stepper = FakeStepper()
        collector = TrainingCollector(
            self.config,
            frame_source=FakeFrameSource(),
            conveyor=stepper,
        )
        collector.start_session("dataset")
        collector.begin_part()

        with self.assertRaises(CollectorError):
            collector.step_conveyor()
        self.assertEqual(stepper.calls, 0)

        for _ in range(2):
            collector.capture_next()
            collector.skip_current()
        collector.finish_part()
        self.assertEqual(stepper.calls, 1)

        result = collector.step_conveyor()

        self.assertEqual(stepper.calls, 2)
        self.assertTrue(result["conveyor"]["moved"])
        self.assertIsNone(result["part"])

    def test_disabled_conveyor_is_reported(self):
        collector = TrainingCollector(
            self.config,
            frame_source=FakeFrameSource(),
        )
        collector.start_session("dataset")

        result = self._finish_collected_part(collector)

        self.assertFalse(result["conveyor"]["enabled"])
        self.assertFalse(result["conveyor"]["moved"])


class SerialConveyorStepperTest(unittest.TestCase):
    def _build(self, transport, *, find_controller, clock=None):
        patches = [
            mock.patch(
                "hardware.port_discovery.find_controller",
                side_effect=find_controller,
            ),
            mock.patch(
                "hardware.serial_transport.SerialTransport",
                return_value=transport,
            ),
            mock.patch(
                "config.calibration_loader.load_calibration",
                return_value=dict(CALIBRATION),
            ),
            mock.patch("hardware.conveyor.time.sleep"),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        return SerialConveyorStepper(
            calibration_path="calibration.json",
            step_timeout=1.0,
            clock=clock or (lambda: 0.0),
        )

    def test_advance_sends_g3_and_confirms_step(self):
        transport = FakeTransport(step=10)
        stepper = self._build(
            transport,
            find_controller=lambda **kwargs: ("COM9", "Found"),
        )

        result = stepper.advance()

        self.assertTrue(result["moved"])
        self.assertEqual(result["step"], 11)
        self.assertEqual(result["port"], "COM9")
        self.assertIn("G3 N11", transport.commands)
        self.assertEqual(transport.commands.count("G3 N11"), 1)
        self.assertEqual(stepper.port, "COM9")
        self.assertTrue(stepper.connected)

    def test_second_advance_reuses_connection(self):
        transport = FakeTransport(step=10)
        find_controller = mock.Mock(return_value=("COM9", "Found"))
        stepper = self._build(transport, find_controller=find_controller)

        stepper.advance()
        stepper.advance()

        self.assertEqual(find_controller.call_count, 1)
        self.assertIn("G3 N11", transport.commands)
        self.assertIn("G3 N12", transport.commands)

    def test_missing_controller_raises_and_cools_down(self):
        now = [0.0]
        find_controller = mock.Mock(return_value=(None, "No COM ports"))
        stepper = self._build(
            FakeTransport(),
            find_controller=find_controller,
            clock=lambda: now[0],
        )

        with self.assertRaises(ConveyorStepError):
            stepper.advance()
        with self.assertRaises(ConveyorStepError):
            stepper.advance()
        self.assertEqual(find_controller.call_count, 1)

        now[0] = 6.0
        with self.assertRaises(ConveyorStepError):
            stepper.advance()
        self.assertEqual(find_controller.call_count, 2)

    def test_lost_ack_is_not_retried_and_connection_resets(self):
        transport = FakeTransport(step=10)

        def fake_query(command, delay=0.15):
            transport.commands.append(command)
            if command == "I2":
                return "MOV=0 WAIT=0 STEP=10 lastErr=0"
            return "Move start"

        transport.query = mock.Mock(side_effect=fake_query)
        stepper = self._build(
            transport,
            find_controller=lambda **kwargs: ("COM9", "Found"),
        )

        with self.assertRaises(ConveyorStepError):
            stepper.advance()

        self.assertIn("G3 N11", transport.commands)
        self.assertEqual(transport.commands.count("G3 N11"), 1)
        self.assertFalse(stepper.connected)
        self.assertTrue(transport.closed)


if __name__ == "__main__":
    unittest.main()


class TrainingApiConveyorTest(unittest.TestCase):
    """HTTP-контракт: результат шага приходит вместе с завершением детали."""

    def setUp(self):
        from fastapi.testclient import TestClient

        from training.server import create_app

        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.config = TrainingConfig(
            root_folder=self._temp.name,
            camera_roles=("A", "B"),
            defect_classes=("glass", "other"),
        )
        self.stepper = FakeStepper()
        self.collector = TrainingCollector(
            self.config,
            frame_source=FakeFrameSource(),
            conveyor=self.stepper,
        )
        self.client = TestClient(create_app(self.collector))

    def test_finish_part_returns_conveyor_step(self):
        response = self.client.post(
            "/api/training/session",
            json={
                "dataset_name": "dataset",
                "camera_roles": ["A", "B"],
                "defect_classes": ["glass", "other"],
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            self.client.post("/api/training/parts", json={}).status_code,
            200,
        )
        for _ in range(2):
            self.assertEqual(
                self.client.post("/api/training/capture").status_code,
                200,
            )
            self.assertEqual(
                self.client.post("/api/training/skip", json={}).status_code,
                200,
            )

        response = self.client.post("/api/training/parts/finish", json={})

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["conveyor"]["enabled"])
        self.assertTrue(payload["conveyor"]["moved"])
        self.assertEqual(payload["conveyor"]["step"], 42)
        self.assertEqual(self.stepper.calls, 1)
        self.assertIsNone(payload["part"])

    def test_manual_step_endpoint(self):
        self.client.post(
            "/api/training/session",
            json={
                "dataset_name": "dataset",
                "camera_roles": ["A", "B"],
                "defect_classes": ["glass", "other"],
            },
        )

        response = self.client.post("/api/training/conveyor/step", json={})

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["conveyor"]["moved"])
        self.assertEqual(payload["conveyor"]["step"], 42)
        self.assertEqual(self.stepper.calls, 1)

    def test_config_reports_conveyor_step(self):
        payload = self.client.get("/api/training/config").json()
        self.assertTrue(payload["conveyor_step"])
