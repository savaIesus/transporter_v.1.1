"""Абсолютная последовательность производственного шага."""

from __future__ import annotations

import unittest

from core.production_cycle import ProductionCycle
from core.state_machine import State
from core.step_stages import StageSequenceError, StepSequencer, StepStage
from inspection.result import InspectionResult


class FakeLive:
    def __init__(self):
        self.events = []
        self.error = None

    def pause(self, timeout=5.0):
        self.events.append("pause_all")
        return True

    def resume(self):
        self.events.append("resume_all")

    def clear_overlays(self):
        self.events.append("clear_overlays")

    def start(self):
        return True

    def stop(self):
        return None

    def reset_pause(self):
        self.events.append("reset_pause")

    @property
    def running(self):
        return False

    @property
    def fps(self):
        return 0.0


class FakeConveyor:
    speed = 20000

    def __init__(self, log):
        self.log = log

    def move_step(self):
        self.log.append("conveyor.move_step")

    def wait_stop(self, progress_callback=None):
        self.log.append("conveyor.wait_stop")

    def emergency_stop(self):
        self.log.append("conveyor.emergency_stop")


class FakeCameras:
    mapping = {"LEFT": 0, "MIDDLE": 1, "RIGHT": 2}

    def __init__(self, log):
        self.log = log

    def drain_buffers(self, roles=None):
        for role in roles or self.mapping:
            self.log.append(f"drain:{role}")

    def capture_roles(self, roles):
        frames = {}
        for role in roles:
            self.log.append(f"capture:{role}")
            frames[role] = object()
        return frames

    def capture_single(self, role):
        return self.capture_roles((role,))[role]


class FakeInspector:
    INSPECT_ROLES = ("LEFT", "MIDDLE", "RIGHT")
    PRESENCE_ROLES = ("LEFT", "RIGHT")

    def __init__(self, log):
        self.log = log

    def set_progress_callback(self, callback):
        self.on_progress = callback

    def inspect(self, part_id, step, frames):
        self.log.append("inspect:" + ",".join(frames))
        return InspectionResult(
            stage="inspect",
            defects=[],
            vision_results={role: [] for role in frames},
            raw_frames=frames,
        )


class FakeDistributor:
    dist1_open_position = 340

    def __init__(self, log):
        self.log = log
        self.on_state_changed = None
        self.cancel_check = None
        self.status = {
            "dist1_position": 0,
            "dist1_max": 340,
            "dist1_state": "GOOD",
            "dist2_position": 0,
            "dist2_max": 340,
            "dist2_state": "IDLE",
            "dist2_target": "BAD",
            "last_distributor_action": "-",
        }

    def prepare_route(self, category, part_id=None):
        self.log.append(f"prepare_route:{category}:{part_id}")

    def reset_target(self):
        self.log.append("reset_target")

    def confirm_transfer(self, part_id, category):
        self.log.append(f"confirm_transfer:{part_id}:{category}")

    def emergency_stop(self):
        self.log.append("distributor.emergency_stop")

    def park_production(self):
        self.log.append("park")


class StepSequencerSequenceTest(unittest.TestCase):
    def test_analysis_may_return_to_capture_for_next_stage(self):
        live = FakeLive()
        stages = StepSequencer(live, settle_seconds=0, trace_seconds=0)
        stages.enter_motion()
        stages.enter_settle()
        stages.enter_capture(("INPUT_LEFT",))
        self.assertEqual(live.events, ["pause_all"])
        stages.enter_analysis()
        stages.enter_capture(("TOP",))
        stages.enter_analysis()
        stages.enter_publish()
        self.assertEqual(live.events, ["pause_all"])
        self.assertTrue(stages.static)
        stages.enter_motion()
        self.assertIn("resume_all", live.events)
        self.assertFalse(stages.static)

    def test_analysis_cannot_skip_to_motion(self):
        live = FakeLive()
        stages = StepSequencer(live, settle_seconds=0, trace_seconds=0)
        stages.enter_motion()
        stages.enter_settle()
        stages.enter_capture(("INPUT_LEFT",))
        stages.enter_analysis()
        with self.assertRaises(StageSequenceError):
            stages.enter_motion()

    def test_empty_capture_does_not_freeze_live(self):
        live = FakeLive()
        stages = StepSequencer(live, settle_seconds=0, trace_seconds=0)
        stages.enter_motion()
        stages.enter_settle()
        stages.enter_capture(())
        self.assertEqual(live.events, [])
        self.assertFalse(stages.static)
        self.assertEqual(stages.stage, StepStage.CAPTURE)


class ProductionCyclePartsTest(unittest.TestCase):
    def test_public_api_stays_on_orchestrator(self):
        expected = (
            "request_start", "request_stop", "request_pause", "request_resume",
            "request_exit", "request_force_exit", "distributor_diagnostic",
            "diagnostic_check_cameras", "diagnostic_check_vision_rules",
            "enter_jog", "exit_jog", "_run_once", "_build_status",
        )
        for name in expected:
            self.assertTrue(
                callable(getattr(ProductionCycle, name, None)),
                f"missing {name}",
            )


class ProductionCycleSequenceTest(unittest.TestCase):
    def _make_cycle(self):
        log = []
        conveyor = FakeConveyor(log)
        cameras = FakeCameras(log)
        inspector = FakeInspector(log)
        distributor = FakeDistributor(log)
        cycle = ProductionCycle(
            conveyor,
            cameras,
            inspector,
            distributor,
            settle_seconds=0,
            stage_trace_seconds=0,
            review_seconds=0,
        )
        cycle.live = FakeLive()
        cycle.stages = StepSequencer(
            cycle.live, settle_seconds=0, trace_seconds=0,
        )
        cycle.sm._state = State.RUNNING
        cycle._await_initial_inspection = False
        return cycle, log

    def test_single_stage_sequential_capture(self):
        cycle, log = self._make_cycle()

        cycle._run_once()

        inspect_stage = log.index("inspect:LEFT,MIDDLE,RIGHT")
        first_capture = log.index("capture:LEFT")
        self.assertLess(first_capture, inspect_stage)
        # Захват строго последовательный: одна камера в момент времени.
        self.assertEqual(
            [item for item in log if item.startswith("capture:")],
            [
                "capture:LEFT",
                "capture:MIDDLE",
                "capture:RIGHT",
            ],
        )
        # Live заморожен на весь инспекционный блок одним exclusive-захватом.
        self.assertEqual(cycle.live.events.count("pause_all"), 1)
        self.assertNotIn("resume_all", cycle.live.events)
        self.assertTrue(cycle.stages.static)

    def test_live_resumes_only_on_next_motion(self):
        cycle, log = self._make_cycle()
        cycle._run_once()
        self.assertNotIn("resume_all", cycle.live.events)
        cycle._await_initial_inspection = False
        cycle._stage_motion()
        self.assertIn("resume_all", cycle.live.events)


if __name__ == "__main__":
    unittest.main()
