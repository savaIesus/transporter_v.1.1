"""Публичный API ProductionCycle: START/STOP/PAUSE/RESUME/EXIT и JOG.

Дополняет поведенческие тесты: покрываются краевые условия request_start,
полный цикл паузы, force-exit, JOG-режим и главный цикл ``start()``
с его завершением.
"""

from __future__ import annotations

import threading
import time
import unittest

from core.production_cycle import ProductionCycle
from domain.part import CATEGORY_BAD, Part
from inspection.result import InspectionResult


class FakeLive:
    def __init__(self):
        self.error = None
        self._running = False
        self.paused = 0
        self.resumed = 0
        self.stopped = 0

    def pause(self, timeout=5.0):
        self.paused += 1
        return True

    def resume(self):
        self.resumed += 1

    def clear_overlays(self):
        pass

    def start(self):
        self._running = True
        return True

    def stop(self):
        self._running = False
        self.stopped += 1

    def reset_pause(self):
        pass

    @property
    def running(self):
        return self._running


class FakeCameras:
    mapping = {"LEFT": 0, "MIDDLE": 1, "RIGHT": 2}

    def capture_roles(self, roles):
        return {role: object() for role in roles}

    def capture_single(self, role):
        return object()

    def capture_all(self):
        return self.capture_roles(tuple(self.mapping))

    def drain_buffers(self, roles=None):
        pass


class FakeConveyor:
    speed = 20000

    def move_step(self):
        pass

    def wait_stop(self, progress_callback=None):
        pass

    def emergency_stop(self):
        pass


class FakeInspector:
    INSPECT_ROLES = ("LEFT", "MIDDLE", "RIGHT")
    PRESENCE_ROLES = ("LEFT", "RIGHT")

    def set_progress_callback(self, callback):
        self.on_progress = callback

    def inspect(self, part_id, step, frames):
        return InspectionResult(
            stage="inspect", defects=[], raw_frames=frames,
            is_empty_tray=True,
        )


class FakeDistributor:
    dist1_open_position = 340

    def __init__(self, fail_park=False):
        self.fail_park = fail_park
        self.parked = 0
        self.on_state_changed = None
        self.cancel_check = None
        self.status = {
            "dist1_position": 0, "dist1_max": 340, "dist1_state": "GOOD",
            "dist2_position": 0, "dist2_max": 340, "dist2_state": "IDLE",
            "dist2_target": "BAD", "last_distributor_action": "-",
        }

    def park_production(self):
        self.parked += 1
        if self.fail_park:
            raise RuntimeError("park failed")

    def prepare_route(self, category, part_id=None):
        pass

    def reset_target(self):
        pass

    def confirm_transfer(self, part_id, category):
        pass

    def emergency_stop(self):
        pass

    def diagnostic_gate(self, position):
        pass

    def diagnostic_route(self, category):
        pass


class FakeJog:
    def __init__(self, error=None, busy=False):
        self.error = error
        self.busy = busy
        self.released = []
        self.started = []

    @property
    def status(self):
        return {"error": self.error, "busy": self.busy,
                "last_action": "-"}

    def start_hold(self, direction):
        self.started.append(direction)
        return True

    def heartbeat(self, direction):
        return True

    def release(self, reason=""):
        self.released.append(reason)
        self.busy = False
        return True


class FakeMonitor:
    def __init__(self):
        self.updates = 0
        self.server = type("S", (), {"active_camera_role": "LEFT"})()

    def update(self, **kwargs):
        self.updates += 1


class FakeArchive:
    def __init__(self):
        self.finalized = []

    def store_frames(self, **kwargs):
        pass

    def finalize(self, **kwargs):
        self.finalized.append(kwargs)
        return f"/archive/part_{kwargs['part_id']:04d}"

    def get_part_info(self, part_id):
        if not any(item.get("part_id") == part_id for item in self.finalized):
            return None
        return {"relative_folder": f"GOOD/part_{part_id:04d}"}


def make_cycle(*, jog=None, distributor=None, monitor=None):
    cycle = ProductionCycle(
        FakeConveyor(),
        FakeCameras(),
        FakeInspector(),
        distributor or FakeDistributor(),
        monitor=monitor or FakeMonitor(),
        archive=FakeArchive(),
        jog=jog,
        settle_seconds=0, stage_trace_seconds=0, review_seconds=0,
    )
    # Подменяем live на дублёр: тесты публичного API не нуждаются в потоках.
    cycle.live = FakeLive()
    return cycle


class RequestStartTest(unittest.TestCase):
    def test_start_ok(self):
        cycle = make_cycle()
        self.assertTrue(cycle.request_start())
        self.assertEqual(cycle.state, "RUNNING")
        self.assertTrue(cycle.live.running)
        cycle.request_stop()
        cycle.live.stop()

    def test_start_while_analysis_active(self):
        cycle = make_cycle()
        cycle._selected_analysis_active = True
        self.assertFalse(cycle.request_start())

    def test_start_with_live_error(self):
        cycle = make_cycle()
        cycle.live.error = "camera down"
        self.assertFalse(cycle.request_start())

    def test_start_with_jog_error(self):
        cycle = make_cycle(jog=FakeJog(error="stuck"))
        self.assertFalse(cycle.request_start())

    def test_start_auto_exits_jog(self):
        jog = FakeJog()
        cycle = make_cycle(jog=jog)
        cycle.jog_active = True
        self.assertTrue(cycle.request_start())
        self.assertIn("leaving JOG mode", jog.released)
        self.assertFalse(cycle.jog_active)
        cycle.request_stop()
        cycle.live.stop()

    def test_start_while_running(self):
        cycle = make_cycle()
        cycle.request_start()
        self.assertFalse(cycle.request_start())
        cycle.request_stop()
        cycle.live.stop()

    def test_start_park_failure_faults(self):
        cycle = make_cycle(distributor=FakeDistributor(fail_park=True))
        with self.assertRaisesRegex(RuntimeError, "park failed"):
            cycle.request_start()
        self.assertEqual(cycle.state, "FAULT")

    def test_start_clears_selected_diagnostics(self):
        from core.cycle.diagnostics import make_diagnostics
        cycle = make_cycle()
        cycle._diagnostics = make_diagnostics(
            "PASSED", "SELECTED_MODEL", "x",
        )
        cycle.request_start()
        self.assertEqual(cycle._diagnostics["kind"], None)
        cycle.request_stop()
        cycle.live.stop()


class PauseResumeTest(unittest.TestCase):
    def test_pause_when_idle(self):
        cycle = make_cycle()
        self.assertFalse(cycle.request_pause())

    def test_pause_running(self):
        cycle = make_cycle()
        cycle.request_start()
        self.assertTrue(cycle.request_pause())
        self.assertTrue(cycle._pause_requested.is_set())
        # Повторная пауза — идемпотентно.
        self.assertTrue(cycle.request_pause())
        cycle.request_stop()
        cycle.live.stop()

    def test_pause_after_exit_requested(self):
        cycle = make_cycle()
        cycle.request_start()
        cycle.sm.request_exit()
        self.assertFalse(cycle.request_pause())
        cycle.live.stop()

    def test_resume_when_running(self):
        cycle = make_cycle()
        cycle.request_start()
        self.assertFalse(cycle.request_resume())
        cycle.request_stop()
        cycle.live.stop()

    def test_resume_from_pause(self):
        cycle = make_cycle()
        cycle.request_start()
        self.assertTrue(cycle.sm.request_pause())
        self.assertEqual(cycle.state, "PAUSED")
        self.assertTrue(cycle.request_resume())
        self.assertEqual(cycle.state, "RUNNING")
        cycle.request_stop()
        cycle.live.stop()

    def test_resume_with_busy_jog(self):
        cycle = make_cycle(jog=FakeJog(busy=True))
        cycle.request_start()
        cycle.sm.request_pause()
        self.assertFalse(cycle.request_resume())
        cycle.sm.request_stop()
        cycle.live.stop()

    def test_force_exit(self):
        cycle = make_cycle()
        self.assertTrue(cycle.request_force_exit())
        self.assertTrue(cycle.force_exit_requested)
        self.assertTrue(cycle._cancel_motion.is_set())


class JogModeTest(unittest.TestCase):
    def test_can_enter_jog_without_jog(self):
        cycle = make_cycle()
        self.assertFalse(cycle.can_enter_jog())

    def test_can_enter_jog_idle(self):
        cycle = make_cycle(jog=FakeJog())
        self.assertTrue(cycle.can_enter_jog())

    def test_can_enter_jog_blocked_by_live_error(self):
        cycle = make_cycle(jog=FakeJog())
        cycle.live.error = "x"
        self.assertFalse(cycle.can_enter_jog())

    def test_can_enter_jog_blocked_by_jog_error(self):
        cycle = make_cycle(jog=FakeJog(error="x"))
        self.assertFalse(cycle.can_enter_jog())

    def test_can_enter_jog_blocked_after_shutdown(self):
        cycle = make_cycle(jog=FakeJog())
        cycle._shutdown = True
        self.assertFalse(cycle.can_enter_jog())

    def test_can_enter_jog_blocked_while_running(self):
        cycle = make_cycle(jog=FakeJog())
        cycle.request_start()
        self.assertFalse(cycle.can_enter_jog())
        cycle.request_stop()
        cycle.live.stop()

    def test_enter_jog_without_jog(self):
        cycle = make_cycle()
        self.assertFalse(cycle.enter_jog())

    def test_enter_jog_ok(self):
        cycle = make_cycle(jog=FakeJog())
        self.assertTrue(cycle.enter_jog())
        self.assertTrue(cycle.jog_active)
        self.assertTrue(cycle.live.running)
        # Повторный вход — уже в режиме.
        self.assertTrue(cycle.enter_jog())

    def test_exit_jog_releases(self):
        jog = FakeJog()
        cycle = make_cycle(jog=jog)
        cycle.enter_jog()
        self.assertTrue(cycle.exit_jog())
        self.assertFalse(cycle.jog_active)
        self.assertIn("leaving JOG mode", jog.released)
        self.assertFalse(cycle.live.running)

    def test_exit_jog_not_active(self):
        cycle = make_cycle(jog=FakeJog())
        self.assertTrue(cycle.exit_jog())

    def test_jog_hold_start(self):
        cycle = make_cycle(jog=FakeJog())
        cycle.enter_jog()
        self.assertTrue(cycle.jog_hold_start("+"))
        self.assertEqual(cycle._process["phase"], "JOG_HOLD")
        self.assertTrue(cycle.jog_hold_heartbeat("+"))
        self.assertTrue(cycle.jog_hold_release("test"))
        cycle.exit_jog()

    def test_jog_hold_left(self):
        cycle = make_cycle(jog=FakeJog())
        cycle.enter_jog()
        self.assertTrue(cycle.jog_hold_start("-"))
        cycle.exit_jog()

    def test_jog_hold_without_jog_mode(self):
        cycle = make_cycle(jog=FakeJog())
        self.assertFalse(cycle.jog_hold_start("+"))
        self.assertFalse(cycle.jog_hold_heartbeat("+"))
        self.assertFalse(cycle.jog_hold_release())

    def test_jog_hold_in_running_state(self):
        cycle = make_cycle(jog=FakeJog())
        cycle.request_start()
        self.assertFalse(cycle.jog_hold_start("+"))
        cycle.request_stop()
        cycle.live.stop()

    def test_jog_hold_invalid_direction_propagates(self):
        class StrictJog(FakeJog):
            def start_hold(self, direction):
                raise ValueError("bad direction")

        cycle = make_cycle(jog=StrictJog())
        cycle.enter_jog()
        with self.assertRaises(ValueError):
            cycle.jog_hold_start("x")
        cycle.exit_jog()


class MainLoopTest(unittest.TestCase):
    def test_start_with_force_exit_breaks(self):
        cycle = make_cycle(jog=FakeJog())
        cycle.request_force_exit()
        cycle.start()
        self.assertTrue(cycle._shutdown)

    def test_start_exits_when_not_active(self):
        cycle = make_cycle(jog=FakeJog())

        def runner():
            cycle.start()

        thread = threading.Thread(target=runner)
        thread.start()
        time.sleep(0.2)
        cycle.sm.request_exit()
        thread.join(5.0)
        self.assertFalse(thread.is_alive())
        self.assertTrue(cycle._shutdown)

    def test_start_handles_live_error_in_idle(self):
        cycle = make_cycle(jog=FakeJog())

        def runner():
            cycle.start()

        thread = threading.Thread(target=runner)
        thread.start()
        time.sleep(0.1)
        cycle.live.error = "camera died"
        time.sleep(0.3)
        self.assertEqual(cycle.state, "FAULT")
        cycle.sm.request_exit()
        thread.join(5.0)
        self.assertFalse(thread.is_alive())


class ArchiveInflightTest(unittest.TestCase):
    def test_archive_inflight_unknown_parts(self):
        cycle = make_cycle()
        part = Part(1, 0)
        cycle.parts.append(part)
        cycle._archive_inflight("test")
        self.assertEqual(part.route_category, CATEGORY_BAD)
        self.assertEqual(part.final_decision, "aborted_test")
        self.assertEqual(cycle.parts, [])
        self.assertIsNone(cycle._pending_drop)


class TelemetryTest(unittest.TestCase):
    def test_on_conveyor_progress(self):
        cycle = make_cycle()
        cycle._on_conveyor_progress({"MOV": 1})
        self.assertEqual(cycle._process["phase"], "CONVEYOR_MOVING")
        self.assertEqual(cycle._process["conveyor"]["speed"], 20000)

    def test_on_inspection_progress(self):
        cycle = make_cycle()
        cycle._on_inspection_progress(
            "inspect_models", "метка", part_id=5, roles=("LEFT",),
        )
        self.assertEqual(cycle._process["phase"], "INSPECT_MODELS")
        self.assertEqual(cycle._process["capture_roles"], ["LEFT"])

    def test_properties(self):
        cycle = make_cycle()
        self.assertEqual(cycle.state, "IDLE")
        self.assertFalse(cycle.exit_requested)
        self.assertEqual(cycle.dist1_open_position, 340)


if __name__ == "__main__":
    unittest.main()
