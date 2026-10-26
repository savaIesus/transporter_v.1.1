"""Inspector трёхкамерной линии: контракт стадии INSPECT и карточки замера.

Vision подменяется фейком, правила — реальные (пороги из thresholds.json).
"""

from __future__ import annotations

import os
import unittest

import numpy as np

from core.decision_engine import DecisionEngine
from domain.threshold_loader import ThresholdLoader
from inspection.debug_recorder import DebugRecorder
from inspection.inspector import Inspector
from inspection.run_report import prepare_rule_results, summarize_model_health

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
THRESHOLDS_PATH = os.path.join(REPO_ROOT, "thresholds.json")


def make_frame(seed: int = 0) -> np.ndarray:
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    frame[:] = 30 + seed
    return frame


def window_detection(confidence: float = 0.95, x1=100, y1=100, x2=160, y2=180):
    """Детекция ячейки окна (kind=uneven_heights) с прямоугольной маской."""
    return {
        "class": "windows",
        "kind": "uneven_heights",
        "confidence": confidence,
        "bbox": [x1, y1, x2, y2],
        "mask": [[x1, y1], [x2, y1], [x2, y2], [x1, y2]],
    }


def sink_detection(confidence: float = 0.9):
    return {
        "class": "shells",
        "kind": "window_sinks",
        "confidence": confidence,
        "bbox": [10, 10, 40, 40],
        "mask": [[10, 10], [40, 10], [40, 40], [10, 40]],
    }


def glass_detection(confidence: float = 0.9):
    return {
        "class": "glass",
        "kind": "bottom_glass",
        "confidence": confidence,
        "bbox": [50, 50, 200, 200],
        "mask": [[50, 50], [200, 50], [200, 200], [50, 200]],
    }


class FakeVision:
    """process_all возвращает предзаданный словарь детекций."""

    def __init__(self, detections_by_role: dict):
        self.detections_by_role = detections_by_role
        self.last_health = [
            {"role": role, "model": "weights/windows_4.pt",
             "ok": True, "elapsed_ms": 1.0, "detections": len(items),
             "error": None}
            for role, items in detections_by_role.items()
        ]

    def process_all(self, frames: dict) -> dict:
        return {
            role: list(items)
            for role, items in self.detections_by_role.items()
            if role in frames
        }


def make_inspector(vision) -> Inspector:
    return Inspector(
        vision=vision,
        decision=DecisionEngine(
            thresholds=ThresholdLoader(THRESHOLDS_PATH).get_all(),
        ),
        recorder=DebugRecorder(folder="debug_frames", enabled=False),
    )


class InspectorContractTest(unittest.TestCase):
    def test_roles(self):
        inspector = make_inspector(FakeVision({}))
        self.assertEqual(
            inspector.INSPECT_ROLES, ("LEFT", "MIDDLE", "RIGHT"),
        )
        self.assertEqual(inspector.PRESENCE_ROLES, ("LEFT", "RIGHT"))

    def test_inspect_requires_dict_frames(self):
        inspector = make_inspector(FakeVision({}))
        with self.assertRaisesRegex(RuntimeError, "словарём"):
            inspector.inspect(1, 0, ["not", "a", "dict"])

    def test_inspect_requires_all_roles(self):
        inspector = make_inspector(FakeVision({}))
        with self.assertRaisesRegex(RuntimeError, "inspect camera frames"):
            inspector.inspect(1, 0, {"LEFT": make_frame()})

    def test_run_vision_missing_role(self):
        class PartialVision(FakeVision):
            def process_all(self, frames):
                return {"LEFT": []}

        inspector = make_inspector(PartialVision({}))
        with self.assertRaisesRegex(RuntimeError, "Missing vision results"):
            inspector.inspect(1, 0, {
                "LEFT": make_frame(),
                "MIDDLE": make_frame(1),
                "RIGHT": make_frame(2),
            })


class InspectStageTest(unittest.TestCase):
    def frames(self) -> dict:
        return {
            "LEFT": make_frame(0),
            "MIDDLE": make_frame(1),
            "RIGHT": make_frame(2),
        }

    def test_empty_tray_stops_before_defect_rules(self):
        vision = FakeVision({"LEFT": [], "MIDDLE": [], "RIGHT": []})
        inspector = make_inspector(vision)
        result = inspector.inspect(7, 3, self.frames())
        self.assertTrue(result.is_empty_tray)
        self.assertEqual(result.defects, [])
        # Только служебное part_presence без defect rules.
        self.assertEqual(len(result.rule_results), 1)
        self.assertEqual(result.rule_results[0].rule_name, "part_presence")
        self.assertTrue(
            result.rule_results[0].details["measurement_cards"],
        )

    def test_good_part_full_pipeline(self):
        vision = FakeVision({
            "LEFT": [window_detection(0.95, 100, 100, 160, 130)],
            "MIDDLE": [],
            "RIGHT": [window_detection(0.95, 300, 100, 360, 130)],
        })
        inspector = make_inspector(vision)
        result = inspector.inspect(1, 0, self.frames())
        self.assertFalse(result.is_empty_tray)
        self.assertEqual(result.defects, [])
        self.assertEqual(result.stage, "inspect")
        # part_presence + 4 defect rules трёхкамерника.
        names = [item.rule_name for item in result.rule_results]
        self.assertEqual(
            names,
            ["part_presence", "uneven_heights", "window_sinks",
             "bottom_glass", "welding"],
        )
        self.assertIn("LEFT", result.annotated)
        self.assertIn("LEFT", result.raw_overlay_frames)

    def test_triggered_rule_becomes_defect(self):
        vision = FakeVision({
            "LEFT": [window_detection(0.95, 100, 100, 160, 130)],
            "MIDDLE": [glass_detection(0.9)],
            "RIGHT": [window_detection(0.95, 300, 100, 360, 130)],
        })
        inspector = make_inspector(vision)
        result = inspector.inspect(2, 1, self.frames())
        self.assertIn("bottom_glass", result.defects)

    def test_progress_callback_roles(self):
        calls = []

        def on_progress(phase, label, *, part_id=None, roles=()):
            calls.append((phase, tuple(roles)))

        vision = FakeVision({
            "LEFT": [window_detection(0.95, 100, 100, 160, 130)],
            "MIDDLE": [],
            "RIGHT": [window_detection(0.95, 300, 100, 360, 130)],
        })
        inspector = make_inspector(vision)
        inspector.set_progress_callback(on_progress)
        inspector.inspect(1, 0, self.frames())
        self.assertTrue(calls)
        self.assertIn(("INSPECT_MODELS", ("LEFT", "MIDDLE", "RIGHT")), calls)
        self.assertIn(("INSPECT_PRESENCE", ("LEFT", "RIGHT")), calls)

    def test_progress_callback_errors_swallowed(self):
        def broken_callback(*_args, **_kwargs):
            raise RuntimeError("ui is gone")

        vision = FakeVision({
            "LEFT": [window_detection(0.95, 100, 100, 160, 130)],
            "MIDDLE": [],
            "RIGHT": [window_detection(0.95, 300, 100, 360, 130)],
        })
        inspector = make_inspector(vision)
        inspector.set_progress_callback(broken_callback)
        result = inspector.inspect(1, 0, self.frames())
        self.assertFalse(result.is_empty_tray)


class DiagnosticsApiTest(unittest.TestCase):
    def test_evaluate_all_empty_tray(self):
        vision = FakeVision({"LEFT": [], "MIDDLE": [], "RIGHT": []})
        inspector = make_inspector(vision)
        frames = {
            "LEFT": make_frame(0),
            "MIDDLE": make_frame(1),
            "RIGHT": make_frame(2),
        }
        vision_results, rule_results, model_rows = inspector.evaluate_all(
            frames,
        )
        self.assertEqual(len(rule_results), 1)
        self.assertEqual(rule_results[0].rule_name, "part_presence")
        self.assertTrue(model_rows)

    def test_evaluate_rules_limited_by_roles(self):
        vision = FakeVision({})
        inspector = make_inspector(vision)
        # Только MIDDLE: стекло и сварка, без правил окон.
        rules = inspector.decision.rules_for_roles(("MIDDLE",))
        self.assertEqual(
            sorted(rule.name for rule in rules),
            ["bottom_glass", "welding"],
        )

    def test_summarize_model_health(self):
        rows = summarize_model_health([
            {"role": "LEFT", "model": "a.pt", "ok": True,
             "elapsed_ms": 2.5, "detections": 3, "error": None},
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["detections"], 3)

    def test_prepare_rule_results_rejects_non_bool(self):
        from domain.defect_rules.base import RuleResult

        with self.assertRaisesRegex(RuntimeError, "не-bool"):
            prepare_rule_results([
                RuleResult("uneven_heights", triggered="yes"),
            ])


if __name__ == "__main__":
    unittest.main()
