"""Предстартовые диагностики ProductionCycle (core.cycle.diagnostics).

Проверяются распределитель, камеры, полный прогон моделей и правил и
анализ кадра выбранной камеры — на дублёрах, без движения линии.
"""

from __future__ import annotations

import unittest

import numpy as np

from core.cycle.diagnostics import make_diagnostics
from core.production_cycle import ProductionCycle
from domain.defect_rules import RuleResult


def make_frame():
    return np.zeros((24, 32, 3), dtype=np.uint8)


class FakeConveyor:
    speed = 20000

    def move_step(self):
        pass

    def wait_stop(self, progress_callback=None):
        pass

    def emergency_stop(self):
        pass


class FakeCameras:
    mapping = {
        "LEFT": 0, "MIDDLE": 1, "RIGHT": 2,
    }

    def __init__(self):
        self.drained = []

    def drain_buffers(self, roles=None):
        self.drained.append(tuple(roles or self.mapping))

    def capture_roles(self, roles):
        return {role: make_frame() for role in roles}

    def capture_single(self, role):
        return make_frame()

    def capture_all(self):
        return self.capture_roles(tuple(self.mapping))


class FakeDecision:
    def __init__(self, rules_by_role=None):
        self.rules_by_role = rules_by_role or {}

    def rules_for_role(self, role):
        return self.rules_by_role.get(role, [object()])


class FakeVision:
    def __init__(self, results):
        self.results = results

    def process_all(self, frames):
        return {
            role: list(self.results.get(role, [])) for role in frames
        }


class FakeInspector:
    INSPECT_ROLES = ("LEFT", "MIDDLE", "RIGHT")
    PRESENCE_ROLES = ("LEFT", "RIGHT")

    def __init__(self, vision_results=None, model_health=None, fail_all=False):
        vision_results = vision_results or {
            role: [] for role in self.INSPECT_ROLES
        }
        self.vision_results = vision_results
        self.vision = FakeVision(vision_results)
        self.model_health_rows = model_health or [{
            "role": "MIDDLE", "model": "m", "ok": True,
            "elapsed_ms": 1.0, "detections": 0,
        }]
        self.fail_all = fail_all
        self.decision = FakeDecision()
        self.set_progress_callback(None)

    def set_progress_callback(self, callback):
        self.on_progress = callback

    def evaluate_all(self, frames):
        if self.fail_all:
            raise RuntimeError("models failed")
        results = [RuleResult("part_presence", False, details={
            "empty_tray": True,
        })]
        return self.vision_results, results, self.model_health()

    def evaluate_presence(self, vision_results):
        return RuleResult("part_presence", False, details={
            "empty_tray": False,
        })

    def evaluate_rules(self, vision_results, frames=None, roles=None):
        return [RuleResult("uneven_heights", False, details={
            "per_role": {"LEFT": {"triggered": False, "found": 7}},
        })]

    def model_health(self):
        return list(self.model_health_rows)


class FakeDistributor:
    dist1_open_position = 340

    def __init__(self, log):
        self.log = log
        self.on_state_changed = None
        self.cancel_check = None
        self.status = {
            "dist1_position": 0, "dist1_max": 340, "dist1_state": "GOOD",
            "dist2_position": 0, "dist2_max": 340, "dist2_state": "IDLE",
            "dist2_target": "BAD", "last_distributor_action": "-",
        }

    def park_production(self):
        pass

    def prepare_route(self, category, part_id=None):
        pass

    def reset_target(self):
        pass

    def confirm_transfer(self, part_id, category):
        pass

    def emergency_stop(self):
        pass

    def diagnostic_gate(self, position):
        self.log.append(("gate", position))

    def diagnostic_route(self, category):
        self.log.append(("dist_route", category))


class FakeJog:
    def __init__(self, busy=False):
        self.busy = busy
        self.status = {"busy": busy, "error": None}

    def start_hold(self, direction):
        return True

    def heartbeat(self, direction):
        return True

    def release(self, reason=""):
        self.busy = False
        return True


class FakeMonitor:
    def __init__(self):
        self.updates = 0
        self.server = type("S", (), {"active_camera_role": "MIDDLE"})()

    def update(self, **kwargs):
        self.updates += 1


def make_cycle(log=None, inspector=None, jog=None):
    log = log if log is not None else []
    return ProductionCycle(
        FakeConveyor(),
        FakeCameras(),
        inspector or FakeInspector(),
        FakeDistributor(log),
        monitor=FakeMonitor(),
        archive=None,
        jog=jog,
        settle_seconds=0, stage_trace_seconds=0, review_seconds=0,
    )


class MakeDiagnosticsTest(unittest.TestCase):
    def test_default_shape(self):
        report = make_diagnostics()
        self.assertEqual(report["status"], "NOT_RUN")
        self.assertEqual(report["cameras"], [])
        self.assertEqual(report["models"], [])
        self.assertEqual(report["rules"], [])
        self.assertIsNone(report["updated_at"])

    def test_custom_values(self):
        report = make_diagnostics(
            "PASSED", "CAMERAS", "ok",
            cameras=[{"role": "TOP"}],
            models=[{"m": 1}],
            rules=[{"r": 1}],
            updated_at=123,
            extra_field=5,
        )
        self.assertEqual(report["status"], "PASSED")
        self.assertEqual(report["kind"], "CAMERAS")
        self.assertEqual(report["extra_field"], 5)

    def test_lists_are_copied(self):
        cameras = [{"role": "TOP"}]
        report = make_diagnostics(cameras=cameras)
        cameras.append({"role": "X"})
        self.assertEqual(len(report["cameras"]), 1)


class DiagnosticsMixinTest(unittest.TestCase):
    def test_distributor_diagnostic_gate(self):
        log = []
        cycle = make_cycle(log)
        self.assertTrue(cycle.distributor_diagnostic("DIST1_HOME"))
        self.assertIn(("gate", "HOME"), log)
        self.assertEqual(cycle._process["phase"], "DIAGNOSTIC_DONE")

    def test_distributor_diagnostic_open(self):
        log = []
        cycle = make_cycle(log)
        self.assertTrue(cycle.distributor_diagnostic("DIST1_OPEN"))
        self.assertIn(("gate", "OPEN"), log)

    def test_distributor_diagnostic_routes(self):
        log = []
        cycle = make_cycle(log)
        self.assertTrue(cycle.distributor_diagnostic("DIST2_BAD"))
        self.assertIn(("dist_route", "BAD"), log)
        self.assertTrue(cycle.distributor_diagnostic("DIST2_CLEANUP"))
        self.assertIn(("dist_route", "CLEANUP"), log)

    def test_distributor_diagnostic_unknown(self):
        cycle = make_cycle()
        with self.assertRaises(ValueError):
            cycle.distributor_diagnostic("DIST9")

    def test_distributor_diagnostic_busy_lock(self):
        cycle = make_cycle()
        cycle._operation_lock.acquire()
        try:
            self.assertFalse(cycle.distributor_diagnostic("DIST1_HOME"))
        finally:
            cycle._operation_lock.release()

    def test_diagnostic_check_cameras(self):
        cycle = make_cycle()
        self.assertTrue(cycle.diagnostic_check_cameras())
        report = cycle._diagnostics
        self.assertEqual(report["status"], "PASSED")
        self.assertEqual(report["kind"], "CAMERAS")
        self.assertEqual(len(report["cameras"]), 3)
        self.assertEqual(report["cameras"][0]["width"], 32)

    def test_diagnostic_check_cameras_not_allowed_in_running(self):
        cycle = make_cycle()
        cycle.sm.request_start()
        self.assertFalse(cycle.diagnostic_check_cameras())

    def test_diagnostic_check_vision_rules(self):
        cycle = make_cycle()
        self.assertTrue(cycle.diagnostic_check_vision_rules())
        report = cycle._diagnostics
        self.assertEqual(report["kind"], "VISION_RULES")
        self.assertEqual(report["status"], "PASSED")
        self.assertGreaterEqual(len(report["rules"]), 1)

    def test_diagnostic_check_vision_rules_failure(self):
        cycle = make_cycle(inspector=FakeInspector(fail_all=True))
        with self.assertRaisesRegex(RuntimeError, "models failed"):
            cycle.diagnostic_check_vision_rules()
        self.assertEqual(cycle._diagnostics["kind"], "VISION_RULES")
        self.assertEqual(cycle._diagnostics["status"], "ERROR")

    def test_analyze_selected_camera(self):
        cycle = make_cycle()
        self.assertTrue(cycle.diagnostic_analyze_selected_camera("MIDDLE"))
        report = cycle._diagnostics
        self.assertEqual(report["kind"], "SELECTED_MODEL")
        self.assertEqual(report["status"], "PASSED")
        self.assertEqual(report["selected_role"], "MIDDLE")
        self.assertTrue(cycle._selected_analysis_active)

    def test_analyze_selected_camera_input_role(self):
        cycle = make_cycle()
        self.assertTrue(cycle.diagnostic_analyze_selected_camera("LEFT"))
        report = cycle._diagnostics
        self.assertGreaterEqual(len(report["rules"]), 1)

    def test_analyze_unknown_role(self):
        cycle = make_cycle()
        with self.assertRaisesRegex(ValueError, "Неизвестная роль"):
            cycle.diagnostic_analyze_selected_camera("NOPE")
        self.assertFalse(cycle._selected_analysis_active)

    def test_analyze_busy_lock(self):
        cycle = make_cycle()
        cycle._operation_lock.acquire()
        try:
            self.assertFalse(cycle.diagnostic_analyze_selected_camera("MIDDLE"))
        finally:
            cycle._operation_lock.release()

    def test_release_selected_camera(self):
        cycle = make_cycle()
        self.assertFalse(cycle.diagnostic_release_selected_camera())
        cycle.diagnostic_analyze_selected_camera("MIDDLE")
        self.assertTrue(cycle.diagnostic_release_selected_camera())
        self.assertFalse(cycle._selected_analysis_active)
        self.assertEqual(cycle._diagnostics["status"], "NOT_RUN")

    def test_rule_report_row(self):
        result = RuleResult("bottom_glass", True, details={
            "per_role": {"MIDDLE": {"triggered": True, "reason": "glass_found"}},
        })
        row = make_cycle()._rule_report_row(result)
        self.assertEqual(row["name"], "bottom_glass")
        self.assertTrue(row["triggered"])

    def test_rule_report_rows_filter_by_role(self):
        results = [
            RuleResult("a", False, details={
                "per_role": {"MIDDLE": {"triggered": False}},
            }),
            RuleResult("b", False, details={
                "per_role": {"LEFT": {"triggered": False}},
            }),
        ]
        rows = make_cycle()._rule_report_rows(results, role="MIDDLE")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "a")

    def test_prestart_not_allowed_with_jog_busy(self):
        cycle = make_cycle(jog=FakeJog(busy=True))
        self.assertFalse(cycle._prestart_diagnostic_allowed())

    def test_prestart_allowed_idle(self):
        cycle = make_cycle(jog=FakeJog(busy=False))
        self.assertTrue(cycle._prestart_diagnostic_allowed())


if __name__ == "__main__":
    unittest.main()
