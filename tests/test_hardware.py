"""Слой оборудования: SerialTransport, Axis, Conveyor, Distributor, JogController.

Физический COM-порт заменяется фейковым транспортом; проверяются команды,
парсинг ответов контроллера, ограничения движения и последовательности
маршрутизации распределителя.
"""

from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from hardware.axis import Axis
from hardware.conveyor import Conveyor
from hardware.distributor import Distributor
from hardware.jog_controller import JogController
from hardware.port_discovery import (
    find_controller,
    is_controller_response,
    list_available_ports,
    try_port,
)
from hardware.serial_transport import SerialTransport


class FakeSerial:
    """Минимальная имитация pyserial."""

    def __init__(self, responses=None, write_error=None):
        self.is_open = True
        self.closed = False
        self.writes = []
        self.responses = list(responses or [])
        self.write_error = write_error
        self._buffer = b""

    def write(self, data):
        if self.write_error:
            raise self.write_error
        self.writes.append(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        self._buffer = b""

    def read_all(self):
        if self.responses:
            return self.responses.pop(0)
        return b""

    def close(self):
        self.closed = True
        self.is_open = False


class RecordingTransport:
    """Транспорт-дублёр: записывает команды, отвечает по шаблону."""

    def __init__(self, responses=None):
        self.commands = []
        self.responses = responses or {}

    def send(self, command):
        self.commands.append(command)

    def query(self, command, delay=0.15):
        self.commands.append(command)
        response = self.responses.get(command, "")
        if isinstance(response, list):
            if len(response) > 1:
                return response.pop(0)
            return response[0] if response else ""
        if callable(response):
            return response()
        return response


class SerialTransportTest(unittest.TestCase):
    @mock.patch("hardware.serial_transport.serial.Serial")
    def test_open_sends_nothing_and_sleeps(self, serial_cls):
        fake = FakeSerial()
        serial_cls.return_value = fake
        with mock.patch("hardware.serial_transport.time.sleep"):
            transport = SerialTransport("COM4", 115200)
        serial_cls.assert_called_once_with(
            "COM4", 115200, timeout=0.5, write_timeout=2.0,
        )
        transport.close()
        self.assertTrue(fake.closed)

    @mock.patch("hardware.serial_transport.serial.Serial")
    def test_send_appends_newline(self, serial_cls):
        fake = FakeSerial()
        serial_cls.return_value = fake
        with mock.patch("hardware.serial_transport.time.sleep"):
            transport = SerialTransport("COM4")
        transport.send("G3")
        self.assertEqual(fake.writes, [b"G3\n"])

    @mock.patch("hardware.serial_transport.serial.Serial")
    def test_query_returns_trimmed_response(self, serial_cls):
        fake = FakeSerial(responses=[b"MOV=0 WAIT=0 lastErr=0\r\n"])
        serial_cls.return_value = fake
        with mock.patch("hardware.serial_transport.time.sleep"):
            transport = SerialTransport("COM4")
        self.assertEqual(
            transport.query("I2"), "MOV=0 WAIT=0 lastErr=0",
        )

    @mock.patch("hardware.serial_transport.serial.Serial")
    def test_query_waits_for_late_multiline_reply(self, serial_cls):
        # Регрессия: ответ читался фиксированным sleep(delay) + read_all().
        # Контроллер, ответивший чуть позже или несколькими строками (I10 —
        # по строке на ось), давал обрезанный ответ: ACK не совпадал с
        # ожидаемым и производственный шаг уходил в FAULT.
        class LateSerial(FakeSerial):
            def __init__(self, parts, first_delay):
                super().__init__()
                self.parts = list(parts)
                self.first_delay = first_delay
                self.sent_at = None

            def write(self, data):
                super().write(data)
                self.sent_at = time.monotonic()

            def read_all(self):
                late = (
                    self.sent_at is None
                    or time.monotonic() - self.sent_at < self.first_delay
                )
                if late:
                    return b""
                return self.parts.pop(0) if self.parts else b""

        serial_cls.return_value = LateSerial(
            [b"AXIS0 POS=0 MOV=0\r\n", b"AXIS1 POS=340 MOV=0\r\n"],
            first_delay=0.22,
        )
        with mock.patch("hardware.serial_transport.time.sleep"):
            transport = SerialTransport("COM4")

        reply = transport.query("I10", delay=0.15)
        self.assertIn("AXIS0 POS=0", reply)
        self.assertIn("AXIS1 POS=340", reply)

    @mock.patch("hardware.serial_transport.serial.Serial")
    def test_query_returns_promptly_on_fast_reply(self, serial_cls):
        # Быстрый ответ не должен ждать полный QUERY_TIMEOUT: цикл опрашивает
        # контроллер в горячем пути wait_stop().
        serial_cls.return_value = FakeSerial(
            responses=[b"MOV=0 WAIT=0 STEP=5 lastErr=0\r\n"],
        )
        with mock.patch("hardware.serial_transport.time.sleep"):
            transport = SerialTransport("COM4")

        started = time.monotonic()
        reply = transport.query("I2", delay=0.1)
        elapsed = time.monotonic() - started

        self.assertEqual(reply, "MOV=0 WAIT=0 STEP=5 lastErr=0")
        self.assertLess(elapsed, SerialTransport.QUERY_TIMEOUT)

    @mock.patch("hardware.serial_transport.serial.Serial")
    def test_query_returns_empty_when_controller_silent(self, serial_cls):
        serial_cls.return_value = FakeSerial()
        with mock.patch("hardware.serial_transport.time.sleep"):
            transport = SerialTransport("COM4")
        self.assertEqual(transport.query("I2", delay=0.05), "")

    @mock.patch("hardware.serial_transport.serial.Serial")
    def test_silent_controller_does_not_block_emergency_stop(self, serial_cls):
        # Безопасность: query удерживает тот же lock, что и аварийный G1.
        # Молчащий контроллер не должен задерживать dead-man стоп JOG
        # (heartbeat_timeout = 0.40 с) ожиданием полного QUERY_TIMEOUT.
        serial_cls.return_value = FakeSerial()
        with mock.patch("hardware.serial_transport.time.sleep"):
            transport = SerialTransport("COM4")

        worker = threading.Thread(
            target=lambda: transport.query("I1", delay=0.05),
            daemon=True,
        )
        worker.start()
        time.sleep(0.05)

        started = time.monotonic()
        transport.send("G1")
        blocked = time.monotonic() - started
        worker.join(timeout=2.0)

        self.assertLess(blocked, 0.40)

    @mock.patch("hardware.serial_transport.serial.Serial")
    def test_close_tolerates_missing_serial(self, serial_cls):
        fake = FakeSerial()
        serial_cls.return_value = fake
        with mock.patch("hardware.serial_transport.time.sleep"):
            transport = SerialTransport("COM4")
        transport.close()
        self.assertTrue(fake.closed)

    @mock.patch("hardware.serial_transport.serial.Serial")
    def test_close_after_open_failure_not_needed(self, serial_cls):
        serial_cls.side_effect = RuntimeError("no port")
        with mock.patch("hardware.serial_transport.time.sleep"):
            with self.assertRaises(RuntimeError):
                SerialTransport("COM4")


class FakeAxisTransport(RecordingTransport):
    """Транспорт с ответами firmware для осей."""

    def __init__(self, status_line, config_line=None):
        super().__init__()
        self.status_line = status_line
        self.config_line = config_line or (
            "AXIS0 speed=300 accel=100 limMin=0 limMax=1000"
        )

    def query(self, command, delay=0.15):
        self.commands.append(command)
        if command == "I10":
            return self.status_line
        if command == "I11":
            return self.config_line
        return ""


class AxisTest(unittest.TestCase):
    def make_axis(self, transport=None, maximum=1000):
        transport = transport or FakeAxisTransport(
            "AXIS0 POS=0 TGT=0 MOV=0 EN=1 HOME=0 HOMED=1 LIM=1 ES=0",
        )
        with mock.patch("time.sleep"):
            return Axis(transport, 0, maximum, 0, speed=300, accel=100), transport

    def test_invalid_axis_id(self):
        with self.assertRaisesRegex(ValueError, "0 или 1"):
            Axis(RecordingTransport(), 2, 1000)

    def test_invalid_limits(self):
        with self.assertRaisesRegex(ValueError, "0 <= minimum < maximum"):
            Axis(RecordingTransport(), 0, 1000, minimum=-5)
        with self.assertRaisesRegex(ValueError, "0 <= minimum < maximum"):
            Axis(RecordingTransport(), 0, maximum=0)

    def test_constructor_sends_params_and_limits(self):
        transport = FakeAxisTransport("")
        with mock.patch("time.sleep"):
            axis = Axis(transport, 0, 1000, speed=300, accel=100)
        self.assertEqual(transport.commands, [
            "G21 S300 P0", "G22 S100 P0",
            "G31 S0 P0", "G32 S1000 P0", "G33 S1 P0", "I11",
        ])
        self.assertEqual(axis.minimum, 0)
        self.assertEqual(axis.maximum, 1000)

    def test_verify_limit_config_mismatch_raises(self):
        transport = FakeAxisTransport(
            "", config_line="AXIS0 speed=300 accel=100 limMin=10 limMax=1000",
        )
        with mock.patch("time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "limMin"):
                Axis(transport, 0, 1000)

    def test_move_absolute(self):
        transport = FakeAxisTransport("")
        with mock.patch("time.sleep"):
            axis = Axis(transport, 0, 1000)
        with mock.patch("time.sleep"):
            axis.move_absolute(500)
        self.assertEqual(transport.commands[-1], "G27 S500 P0")

    def test_move_absolute_out_of_range(self):
        with mock.patch("time.sleep"):
            axis = Axis(FakeAxisTransport(""), 0, 1000)
        with self.assertRaisesRegex(ValueError, "absolute position"):
            axis.move_absolute(5000)
        with self.assertRaisesRegex(ValueError, "absolute position"):
            axis.move_absolute(5.5)

    def test_home(self):
        transport = FakeAxisTransport("")
        with mock.patch("time.sleep"):
            axis = Axis(transport, 0, 1000)
        with mock.patch("time.sleep"):
            axis.home()
        self.assertIn("G28 P0", transport.commands)

    def test_read_status_parses_fields(self):
        transport = FakeAxisTransport(
            "AXIS0 POS=100 TGT=100 MOV=1 EN=1 HOME=2 HOMED=1 LIM=1 ES=0",
        )
        with mock.patch("time.sleep"):
            axis = Axis(transport, 0, 1000)
        status = axis.read_status()
        self.assertEqual(status["position"], 100)
        self.assertEqual(status["moving"], 1)
        self.assertEqual(status["homed"], 1)
        self.assertEqual(status["raw"], transport.status_line)

    def test_read_status_unparseable(self):
        transport = FakeAxisTransport("GARBAGE")
        with mock.patch("time.sleep"):
            axis = Axis(transport, 0, 1000)
        status = axis.read_status()
        self.assertIsNone(status["position"])

    def test_verify_homed_failures(self):
        transport = FakeAxisTransport(
            "AXIS0 POS=50 TGT=50 MOV=0 EN=1 HOME=0 HOMED=0 LIM=1 ES=0",
        )
        with mock.patch("time.sleep"):
            axis = Axis(transport, 0, 1000)
        with self.assertRaisesRegex(RuntimeError, "invalid homing"):
            axis.verify_homed()

    def test_position_property(self):
        transport = FakeAxisTransport(
            "AXIS0 POS=42 TGT=42 MOV=0 EN=1 HOME=0 HOMED=1 LIM=1 ES=0",
        )
        with mock.patch("time.sleep"):
            axis = Axis(transport, 0, 1000)
        self.assertEqual(axis.position, 42)

    def test_position_missing_raises(self):
        transport = FakeAxisTransport("AXIS0 TGT=0")
        with mock.patch("time.sleep"):
            axis = Axis(transport, 0, 1000)
        with self.assertRaisesRegex(RuntimeError, "no position"):
            _ = axis.position

    def test_wait_stop(self):
        transport = FakeAxisTransport(
            "AXIS0 POS=0 TGT=0 MOV=0 EN=1 HOME=0 HOMED=1 LIM=1 ES=0",
        )
        with mock.patch("time.sleep"):
            axis = Axis(transport, 0, 1000)
        with mock.patch("time.sleep"):
            axis.wait_stop(timeout=1.0)

    def test_wait_stop_timeout(self):
        transport = FakeAxisTransport(
            "AXIS0 POS=0 TGT=0 MOV=1 EN=1 HOME=0 HOMED=1 LIM=1 ES=0",
        )
        with mock.patch("time.sleep"):
            axis = Axis(transport, 0, 1000)
        with mock.patch("time.sleep"):
            with self.assertRaises(TimeoutError):
                axis.wait_stop(timeout=0.01)

    def test_wait_stop_progress_callback(self):
        transport = FakeAxisTransport(
            "AXIS0 POS=0 TGT=0 MOV=0 EN=1 HOME=0 HOMED=1 LIM=1 ES=0",
        )
        with mock.patch("time.sleep"):
            axis = Axis(transport, 0, 1000)
        seen = []
        with mock.patch("time.sleep"):
            axis.wait_stop(timeout=1.0, progress_callback=lambda p, m: seen.append(p))
        self.assertEqual(seen, [0])


class ConveyorTest(unittest.TestCase):
    def make_conveyor(self, transport=None, **kwargs):
        transport = transport or RecordingTransport()
        with mock.patch("time.sleep"):
            conveyor = Conveyor(
                transport,
                speed=20000,
                accel=6000,
                steps_per_division=19048,
                divisions_per_movement=2,
                **kwargs,
            )
        return conveyor, transport

    def test_constructor_sets_parameters(self):
        conveyor, transport = self.make_conveyor()
        self.assertEqual(transport.commands, [
            "G5 S20000", "G4 S6000", "G7 S19048", "G6 S2",
        ])
        self.assertEqual(conveyor.speed, 20000)
        self.assertEqual(conveyor.steps_per_division, 19048)

    def test_invalid_geometry(self):
        with self.assertRaisesRegex(ValueError, "positive"):
            Conveyor(RecordingTransport(), steps_per_division=0)

    def test_move_step_requires_status_and_addressed_ack(self):
        transport = RecordingTransport({
            "I2": "MOV=0 WAIT=0 STEP=41 lastErr=0",
            "G3 N42": "Move start (RELATIVE)\nACK G3 STEP=42",
        })
        conveyor, _ = self.make_conveyor(transport)
        conveyor.move_step()
        self.assertEqual(transport.commands[-2:], ["I2", "G3 N42"])
        self.assertEqual(conveyor._pending_step_sequence, 42)

    def test_move_step_rejects_missing_ack_without_retry(self):
        transport = RecordingTransport({
            "I2": "MOV=0 WAIT=0 STEP=7 lastErr=0",
            "G3 N8": "Move start (RELATIVE)",
        })
        conveyor, _ = self.make_conveyor(transport)
        with self.assertRaisesRegex(RuntimeError, "не подтвердил приём"):
            conveyor.move_step()
        self.assertEqual(transport.commands.count("G3 N8"), 1)
        self.assertEqual(conveyor._pending_step_sequence, 8)

    def test_move_step_rejects_old_firmware_without_step(self):
        transport = RecordingTransport({
            "I2": "MOV=0 WAIT=0 lastErr=0",
        })
        conveyor, _ = self.make_conveyor(transport)
        with self.assertRaisesRegex(RuntimeError, "STEP-протокол"):
            conveyor.move_step()
        self.assertNotIn("G3 N1", transport.commands)

    def test_move_step_rejects_non_idle_baseline(self):
        transport = RecordingTransport({
            "I2": "MOV=1 WAIT=0 STEP=3 lastErr=0",
        })
        conveyor, _ = self.make_conveyor(transport)
        with self.assertRaisesRegex(RuntimeError, "не готов"):
            conveyor.move_step()

    def test_emergency_stop(self):
        conveyor, transport = self.make_conveyor()
        conveyor.emergency_stop()
        self.assertEqual(transport.commands[-1], "G1")

    def test_wait_stop_confirms_completed_sequence(self):
        transport = RecordingTransport({
            "I1": "0",
            "I2": [
                "MOV=0 WAIT=0 STEP=12 lastErr=0",
                "MOV=0 WAIT=0 STEP=13 lastErr=0",
            ],
            "G3 N13": "ACK G3 STEP=13",
        })
        conveyor, _ = self.make_conveyor(transport)
        with mock.patch("time.sleep"):
            conveyor.move_step()
            conveyor.wait_stop(timeout=1.0)
        self.assertIsNone(conveyor._pending_step_sequence)

    def test_wait_stop_does_not_accept_idle_without_completed_step(self):
        transport = RecordingTransport({
            "I1": "0",
            "I2": [
                "MOV=0 WAIT=0 STEP=20 lastErr=0",
                "MOV=0 WAIT=0 STEP=20 lastErr=0",
            ],
            "G3 N21": "ACK G3 STEP=21",
        })
        conveyor, _ = self.make_conveyor(transport)
        with mock.patch("time.sleep"):
            conveyor.move_step()
            with self.assertRaisesRegex(TimeoutError, "STEP=21"):
                conveyor.wait_stop(timeout=0.01)
        self.assertEqual(conveyor._pending_step_sequence, 21)

    def test_wait_stop_requires_accepted_command(self):
        conveyor, _ = self.make_conveyor()
        with self.assertRaisesRegex(RuntimeError, "Нет принятой команды"):
            conveyor.wait_stop(timeout=0.01)

    def test_parse_motion_reply(self):
        self.assertIsNone(Conveyor._parse_motion_reply(""))
        self.assertIsNone(Conveyor._parse_motion_reply("garbage"))
        self.assertTrue(Conveyor._parse_motion_reply("0"))
        self.assertFalse(Conveyor._parse_motion_reply("1"))
        self.assertTrue(Conveyor._parse_motion_reply("noise\n0\n"))
        self.assertFalse(Conveyor._parse_motion_reply("noise\n1\n"))

    def test_parse_status(self):
        status = Conveyor._parse_status(
            "MOV=0 WAIT=1 POS=5 TGT=5 STEP=4294967295 lastErr=-3",
        )
        self.assertEqual(status["mov"], 0)
        self.assertEqual(status["wait"], 1)
        self.assertEqual(status["pos"], 5)
        self.assertEqual(status["step"], 4294967295)
        self.assertEqual(status["lasterr"], -3)
        empty = Conveyor._parse_status("")
        self.assertIsNone(empty["mov"])
        self.assertIsNone(empty["step"])

    def test_ack_requires_exact_sequence(self):
        self.assertTrue(Conveyor._ack_confirmed(
            "Move start\nACK G3 STEP=9", 9,
        ))
        self.assertFalse(Conveyor._ack_confirmed("ACK G3 STEP=8", 9))
        self.assertFalse(Conveyor._ack_confirmed("", 9))

    def test_sequence_wraps_as_uint32(self):
        transport = RecordingTransport({
            "I2": "MOV=0 WAIT=0 STEP=4294967295 lastErr=0",
            "G3 N0": "ACK G3 STEP=0",
        })
        conveyor, _ = self.make_conveyor(transport)
        conveyor.move_step()
        self.assertEqual(conveyor._pending_step_sequence, 0)

    def test_strict_stop_confirmed(self):
        self.assertTrue(Conveyor._strict_stop_confirmed(
            "MOV=0 WAIT=0 lastErr=0",
        ))
        self.assertFalse(Conveyor._strict_stop_confirmed("MOV=0 WAIT=0 lastErr=1"))
        self.assertFalse(Conveyor._strict_stop_confirmed(""))
        self.assertFalse(Conveyor._strict_stop_confirmed("MOV=1 WAIT=0 lastErr=0"))


class FakeAxisForDistributor:
    def __init__(self, transport, axis_id=0, max_position=1000):
        self.transport = transport
        self.axis_id = axis_id
        self.position = 0
        self.max_position = max_position

    def home(self):
        self.transport.send("G28")

    def verify_homed(self):
        return {"position": 0, "moving": 0, "homed": 1}

    def move_absolute(self, position):
        self.move_absolute_async(position)

    def move_absolute_async(self, position):
        self.position = position
        self.transport.send(f"G27 S{position} P{self.axis_id}")

    def wait_stop(self, timeout=12.0, progress_callback=None):
        if progress_callback is not None:
            progress_callback(self.position, 0)

    def read_status(self):
        return {"position": self.position, "moving": 0}


class DistributorTest(unittest.TestCase):
    def make_distributor(self, dist1_open=340, dist2_bad=0, dist2_cleanup=340):
        transport = RecordingTransport()
        dist1 = FakeAxisForDistributor(transport, axis_id=0)
        dist2 = FakeAxisForDistributor(transport, axis_id=1)
        distributor = Distributor(
            dist1, dist2,
            dist1_open_position=dist1_open,
            dist2_bad_position=dist2_bad,
            dist2_cleanup_position=dist2_cleanup,
        )
        return distributor, transport

    def test_invalid_positions(self):
        with self.assertRaisesRegex(ValueError, "dist1_open_position"):
            Distributor(None, None, 0, 1, 2)
        with self.assertRaisesRegex(ValueError, "dist2_bad_position"):
            Distributor(None, None, 10, -1, 2)
        with self.assertRaisesRegex(ValueError, "различаться"):
            Distributor(None, None, 10, 5, 5)

    def test_status_shape(self):
        distributor, _ = self.make_distributor()
        status = distributor.status
        for key in (
            "dist1_position", "dist1_max", "dist1_state",
            "dist2_position", "dist2_max", "dist2_state",
            "dist2_target", "last_distributor_action",
        ):
            self.assertIn(key, status)

    def test_initialize_homes_both_axes(self):
        distributor, transport = self.make_distributor()
        distributor.initialize()
        self.assertEqual(distributor.dist1_state, "GOOD")
        self.assertEqual(distributor.dist2_state, "IDLE")
        self.assertEqual(distributor.dist2_target, "BAD")

    def test_park_production(self):
        distributor, _ = self.make_distributor()
        distributor.park_production()
        self.assertEqual(distributor.dist1_state, "GOOD")
        self.assertEqual(distributor.last_action, "PRODUCTION READY")

    def test_prepare_route_good(self):
        distributor, _ = self.make_distributor()
        distributor.prepare_route("GOOD", part_id=7)
        self.assertEqual(distributor.dist1_state, "GOOD")
        self.assertIn("PART #7 -> GOOD", distributor.last_action)

    def test_prepare_route_bad(self):
        distributor, transport = self.make_distributor()
        distributor.prepare_route("BAD", part_id=3)
        self.assertEqual(distributor.dist2_target, "BAD")
        self.assertEqual(distributor.dist2.position, 0)
        self.assertIn("PART #3 -> BAD READY", distributor.last_action)

    def test_prepare_route_cleanup(self):
        distributor, transport = self.make_distributor()
        distributor.prepare_route("CLEANUP", part_id=3)
        self.assertEqual(distributor.dist2_target, "CLEANUP")
        self.assertEqual(distributor.dist2.position, 340)
        self.assertIn("PART #3 -> CLEANUP READY", distributor.last_action)

    def test_prepare_route_invalid_category(self):
        distributor, _ = self.make_distributor()
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            distributor.prepare_route("MAYBE")

    def test_prepare_route_bad_moves_both_axes_parallel(self):
        # Обе команды G27 уходят подряд, без ожидания между ними:
        # DIST1 -> 340 и DIST2 -> 0 стартуют одновременно.
        distributor, transport = self.make_distributor()
        distributor.diagnostic_route("CLEANUP")  # dist1=0, dist2=340
        transport.commands.clear()
        distributor.prepare_route("BAD", part_id=3)
        self.assertEqual(
            transport.commands,
            ["G27 S340 P0", "G27 S0 P1"],
        )
        self.assertEqual(distributor.dist1.position, 340)
        self.assertEqual(distributor.dist2.position, 0)
        self.assertEqual(distributor.dist2_target, "BAD")

    def test_prepare_route_cleanup_moves_both_axes_parallel(self):
        distributor, transport = self.make_distributor()
        distributor.prepare_route("CLEANUP", part_id=3)
        self.assertEqual(
            transport.commands,
            ["G27 S340 P0", "G27 S340 P1"],
        )
        self.assertEqual(distributor.dist1.position, 340)
        self.assertEqual(distributor.dist2.position, 340)

    def test_prepare_route_same_route_does_not_move(self):
        # Серия одинаковых маршрутов не должна шевелить заслонки вообще.
        distributor, transport = self.make_distributor()
        distributor.prepare_route("BAD", part_id=1)
        count = len(transport.commands)
        distributor.prepare_route("BAD", part_id=2)
        self.assertEqual(len(transport.commands), count)
        self.assertEqual(distributor.dist1.position, 340)
        self.assertEqual(distributor.dist2.position, 0)

    def test_prepare_route_channel_change_moves_only_dist2(self):
        # DIST1 уже открыт (340): смена канала BAD->CLEANUP двигает
        # только DIST2, без возврата DIST1 в GOOD.
        distributor, transport = self.make_distributor()
        distributor.prepare_route("BAD", part_id=1)
        transport.commands.clear()
        distributor.prepare_route("CLEANUP", part_id=2)
        self.assertEqual(transport.commands, ["G27 S340 P1"])
        self.assertEqual(distributor.dist1.position, 340)
        self.assertEqual(distributor.dist2.position, 340)

    def test_prepare_route_good_from_bad_moves_only_dist1(self):
        distributor, transport = self.make_distributor()
        distributor.prepare_route("BAD", part_id=1)
        transport.commands.clear()
        distributor.prepare_route("GOOD", part_id=2)
        self.assertEqual(transport.commands, ["G27 S0 P0"])
        self.assertEqual(distributor.dist1.position, 0)
        self.assertIn("PART #2 -> GOOD", distributor.last_action)

    def test_park_production_moves_both_axes_parallel(self):
        distributor, transport = self.make_distributor()
        distributor.diagnostic_gate("OPEN")       # dist1=340
        distributor.diagnostic_route("CLEANUP")   # dist2=340
        transport.commands.clear()
        distributor.park_production()
        self.assertEqual(
            transport.commands,
            ["G27 S0 P0", "G27 S0 P1"],
        )
        self.assertEqual(distributor.dist1.position, 0)
        self.assertEqual(distributor.dist2.position, 0)
        self.assertEqual(distributor.dist2_target, "BAD")
        self.assertEqual(distributor.last_action, "PRODUCTION READY")

    def test_confirm_transfer(self):
        distributor, _ = self.make_distributor()
        distributor.confirm_transfer(5, "GOOD")
        self.assertIn("PART #5 -> GOOD DONE", distributor.last_action)

    def test_diagnostic_gate(self):
        distributor, _ = self.make_distributor()
        distributor.diagnostic_gate("HOME")
        self.assertEqual(distributor.dist1_state, "GOOD")
        distributor.diagnostic_gate("OPEN")
        self.assertEqual(distributor.dist1_state, "TO_DIST2")
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            distributor.diagnostic_gate("SIDE")

    def test_diagnostic_route(self):
        distributor, _ = self.make_distributor()
        distributor.diagnostic_route("BAD")
        self.assertEqual(distributor.dist2_target, "BAD")
        distributor.diagnostic_route("CLEANUP")
        self.assertEqual(distributor.dist2_target, "CLEANUP")
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            distributor.diagnostic_route("GOOD")

    def test_diagnostic_route_moves_only_dist2(self):
        # Кнопки нижнего распределителя двигают только DIST2:
        # DIST1 остаётся там, куда его поставили кнопки верхнего.
        distributor, _ = self.make_distributor()
        distributor.diagnostic_gate("OPEN")
        self.assertEqual(distributor.dist1.position, 340)
        distributor.diagnostic_route("CLEANUP")
        self.assertEqual(distributor.dist2.position, 340)
        self.assertEqual(distributor.dist1.position, 340)
        self.assertEqual(distributor.dist1_state, "TO_DIST2")
        distributor.diagnostic_route("BAD")
        self.assertEqual(distributor.dist2.position, 0)
        self.assertEqual(distributor.dist1.position, 340)

    def test_emergency_stop(self):
        distributor, transport = self.make_distributor()
        distributor.emergency_stop()
        self.assertEqual(transport.commands[-1], "G25")
        self.assertEqual(distributor.dist1_state, "FAULT")
        self.assertEqual(distributor.dist2_state, "FAULT")

    def test_state_changed_callback(self):
        distributor, _ = self.make_distributor()
        events = []
        distributor.on_state_changed = lambda: events.append(1)
        distributor.initialize()
        self.assertGreater(len(events), 0)


def _calibration():
    return {
        "jog_hold_steps": 1_000_000,
        "normal_steps": 19048,
    }


class JogControllerTest(unittest.TestCase):
    def make_jog(self, transport=None, calibration=None, timeout=0.15):
        transport = transport or RecordingTransport()
        with mock.patch("time.sleep"):
            jog = JogController(
                transport,
                calibration or _calibration(),
                heartbeat_timeout=timeout,
            )
        return jog, transport

    def test_invalid_hold_steps(self):
        with self.assertRaisesRegex(ValueError, "jog_hold_steps"):
            JogController(RecordingTransport(), {"jog_hold_steps": 5, "normal_steps": 1})

    def test_invalid_normal_steps(self):
        with self.assertRaisesRegex(ValueError, "normal_steps"):
            JogController(RecordingTransport(), {"jog_hold_steps": 100000, "normal_steps": 0})

    def test_invalid_heartbeat_timeout(self):
        with self.assertRaisesRegex(ValueError, "heartbeat_timeout"):
            JogController(RecordingTransport(), _calibration(), heartbeat_timeout=5.0)

    def test_status_shape(self):
        jog, _ = self.make_jog()
        status = jog.status
        self.assertEqual(status["hold_steps"], 1_000_000)
        self.assertFalse(status["busy"])
        self.assertIn("heartbeat_timeout_ms", status)

    def test_start_hold_invalid_direction(self):
        jog, _ = self.make_jog()
        with self.assertRaisesRegex(ValueError, "'\\+' или '-'"):
            jog.start_hold("x")

    def test_start_hold_and_release(self):
        transport = RecordingTransport({
            "I1": "0",
            "I2": "MOV=0 WAIT=0 lastErr=0",
        })
        jog, _ = self.make_jog(transport)
        self.assertTrue(jog.start_hold("+"))
        self.assertTrue(jog.busy)
        self.assertEqual(jog.last_action, "HOLD RIGHT")
        self.assertTrue(jog.heartbeat("+"))
        self.assertTrue(jog.release("test"))
        self.assertFalse(jog.busy)
        self.assertIn("STOP: test", jog.last_action)
        self.assertIn("G1", transport.commands)

    def test_start_hold_left_direction(self):
        jog, _ = self.make_jog()
        self.assertTrue(jog.start_hold("-"))
        self.assertEqual(jog.last_action, "HOLD LEFT")
        jog.release()

    def test_heartbeat_wrong_direction(self):
        jog, _ = self.make_jog()
        jog.start_hold("+")
        self.assertFalse(jog.heartbeat("-"))
        jog.release()

    def test_heartbeat_when_idle(self):
        jog, _ = self.make_jog()
        self.assertFalse(jog.heartbeat("+"))

    def test_release_without_start(self):
        jog, _ = self.make_jog()
        self.assertTrue(jog.release("nothing"))

    def test_repeated_hold_same_direction(self):
        jog, _ = self.make_jog()
        jog.start_hold("+")
        self.assertTrue(jog.start_hold("+"))
        jog.release()

    def test_hold_other_direction_while_busy(self):
        jog, _ = self.make_jog()
        jog.start_hold("+")
        self.assertFalse(jog.start_hold("-"))
        jog.release()


class PortDiscoveryTest(unittest.TestCase):
    def test_is_controller_response(self):
        self.assertTrue(is_controller_response(
            "MOV=0 WAIT=0 STEP=0 lastErr=0",
        ))
        self.assertFalse(is_controller_response("MOV=0 WAIT=0 lastErr=0"))
        self.assertFalse(is_controller_response(""))
        self.assertFalse(is_controller_response("hello"))

    def test_list_available_ports(self):
        class Info:
            device = "COM4"
            description = "USB-SERIAL"
            hwid = "USB VID:PID"
            manufacturer = "FTDI"

        with mock.patch(
            "hardware.port_discovery.serial.tools.list_ports.comports",
            return_value=[Info()],
        ):
            ports = list_available_ports()
        self.assertEqual(ports, [{
            "port": "COM4", "description": "USB-SERIAL",
            "hwid": "USB VID:PID", "manufacturer": "FTDI",
        }])

    def test_try_port_success(self):
        fake = FakeSerial(responses=[b"MOV=0 WAIT=0 STEP=0 lastErr=0"])
        with mock.patch("hardware.port_discovery.serial.Serial",
                        return_value=fake), \
             mock.patch("hardware.port_discovery.time.sleep"):
            ok, response = try_port("COM4")
        self.assertTrue(ok)
        self.assertIn("MOV=", response)

    def test_try_port_no_response(self):
        fake = FakeSerial(responses=[b""])
        with mock.patch("hardware.port_discovery.serial.Serial",
                        return_value=fake), \
             mock.patch("hardware.port_discovery.time.sleep"):
            ok, response = try_port("COM4")
        self.assertFalse(ok)
        self.assertEqual(response, "no response")

    def test_try_port_unexpected_response(self):
        fake = FakeSerial(responses=[b"hello"])
        with mock.patch("hardware.port_discovery.serial.Serial",
                        return_value=fake), \
             mock.patch("hardware.port_discovery.time.sleep"):
            ok, response = try_port("COM4")
        self.assertFalse(ok)
        self.assertIn("unexpected controller response", response)

    def test_try_port_serial_exception(self):
        with mock.patch(
            "hardware.port_discovery.serial.Serial",
            side_effect=Exception("boom"),
        ), mock.patch("hardware.port_discovery.time.sleep"):
            ok, response = try_port("COM4")
        self.assertFalse(ok)
        self.assertIn("error", response)

    def test_try_port_closes_serial(self):
        fake = FakeSerial(responses=[b""])
        with mock.patch("hardware.port_discovery.serial.Serial",
                        return_value=fake), \
             mock.patch("hardware.port_discovery.time.sleep"):
            try_port("COM4")
        self.assertTrue(fake.closed)

    def test_find_controller_no_ports(self):
        with mock.patch(
            "hardware.port_discovery.list_available_ports", return_value=[],
        ):
            port, message = find_controller()
        self.assertIsNone(port)
        self.assertIn("No COM ports", message)

    def test_find_controller_found(self):
        with mock.patch(
            "hardware.port_discovery.list_available_ports",
            return_value=[{"port": "COM4", "description": "USB"}],
        ), mock.patch(
            "hardware.port_discovery.try_port",
            return_value=(True, "MOV=0 WAIT=0 lastErr=0"),
        ):
            port, message = find_controller()
        self.assertEqual(port, "COM4")
        self.assertIn("Found on COM4", message)

    def test_find_controller_preferred_first(self):
        calls = []

        def fake_try_port(port, baudrate):
            calls.append(port)
            return (port == "COM3", "MOV=0 WAIT=0 lastErr=0")

        with mock.patch(
            "hardware.port_discovery.list_available_ports",
            return_value=[
                {"port": "COM4", "description": "USB"},
                {"port": "COM3", "description": "USB"},
            ],
        ), mock.patch("hardware.port_discovery.try_port",
                      side_effect=fake_try_port):
            port, _ = find_controller(preferred_port="COM3")
        self.assertEqual(port, "COM3")
        self.assertEqual(calls, ["COM3"])

    def test_find_controller_not_found(self):
        with mock.patch(
            "hardware.port_discovery.list_available_ports",
            return_value=[{"port": "COM4", "description": "USB"}],
        ), mock.patch(
            "hardware.port_discovery.try_port", return_value=(False, "no response"),
        ):
            port, message = find_controller()
        self.assertIsNone(port)
        self.assertIn("Controller not found", message)


if __name__ == "__main__":
    unittest.main()
