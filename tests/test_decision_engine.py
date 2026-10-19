"""DecisionEngine и контракт правил трёхкамерной линии."""

from __future__ import annotations

import unittest

from core.decision_engine import DecisionEngine
from domain.defect_rules import (
    BottomGlassRule,
    RuleResult,
    UnevenHeightsRule,
    WeldingRule,
    WindowSinksRule,
)


def _thresholds(**overrides):
    base = {
        "LEFT.uneven_heights_min_confidence": 0.7,
        "LEFT.uneven_heights_height_min_px": 20,
        "LEFT.uneven_heights_height_max_px": 42,
        "LEFT.uneven_heights_height_difference_px": 11,
        "LEFT.uneven_heights_min_intersection_gap_px": 7,
        "LEFT.window_sinks_min_confidence": 0.8,
        "RIGHT.uneven_heights_min_confidence": 0.7,
        "RIGHT.uneven_heights_height_min_px": 21,
        "RIGHT.uneven_heights_height_max_px": 47,
        "RIGHT.uneven_heights_height_difference_px": 11,
        "RIGHT.uneven_heights_min_intersection_gap_px": 7,
        "RIGHT.window_sinks_min_confidence": 0.8,
        "MIDDLE.bottom_glass_min_confidence": 0.65,
        "MIDDLE.welding_min_confidence": 0.65,
        "LEFT.part_presence_min_confidence": 0.6,
        "LEFT.part_presence_min_windows": 1,
        "RIGHT.part_presence_min_confidence": 0.6,
        "RIGHT.part_presence_min_windows": 1,
        "disabled_rules": [],
    }
    base.update(overrides)
    return base


class RuleResultTest(unittest.TestCase):
    def test_defect_none_when_ok(self):
        self.assertIsNone(RuleResult("x", False).defect)

    def test_defect_is_name_when_triggered(self):
        self.assertEqual(RuleResult("x", True).defect, "x")

    def test_defaults(self):
        result = RuleResult("x", False)
        self.assertEqual(result.details, {})
        self.assertEqual(result.drawings, [])


class DecisionEngineTest(unittest.TestCase):
    def test_active_rules_are_three_camera_set(self):
        engine = DecisionEngine(_thresholds())
        self.assertEqual(
            sorted(r.name for r in engine.rules),
            sorted([
                "uneven_heights",
                "window_sinks",
                "bottom_glass",
                "welding",
            ]),
        )

    def test_disabled_rules_are_excluded(self):
        engine = DecisionEngine(
            _thresholds(disabled_rules=["welding", "bottom_glass"]),
        )
        names = {r.name for r in engine.rules}
        self.assertNotIn("welding", names)
        self.assertNotIn("bottom_glass", names)

    def test_rules_for_role(self):
        engine = DecisionEngine(_thresholds())
        middle = {r.name for r in engine.rules_for_role("MIDDLE")}
        self.assertEqual(middle, {"bottom_glass", "welding"})
        side = {r.name for r in engine.rules_for_role("LEFT")}
        self.assertEqual(side, {"uneven_heights", "window_sinks"})

    def test_empty_vision_returns_no_results(self):
        engine = DecisionEngine(_thresholds())
        self.assertEqual(engine.evaluate_all_detailed({}), [])

    def test_all_disabled_raises(self):
        with self.assertRaises(RuntimeError):
            DecisionEngine(_thresholds(
                disabled_rules=[
                    "uneven_heights",
                    "window_sinks",
                    "bottom_glass",
                    "welding",
                ],
            ))


class RuleAttributesTest(unittest.TestCase):
    def setUp(self):
        self.thresholds = _thresholds()

    def test_window_sinks_uses_side_cameras(self):
        rule = WindowSinksRule(self.thresholds)
        self.assertEqual(rule.name, "window_sinks")
        self.assertTrue(set(rule.ROLES).issubset({"LEFT", "RIGHT"}))

    def test_bottom_glass_uses_middle(self):
        rule = BottomGlassRule(self.thresholds)
        self.assertEqual(rule.ROLES, ("MIDDLE",))

    def test_welding_uses_middle(self):
        rule = WeldingRule(self.thresholds)
        self.assertEqual(rule.ROLES, ("MIDDLE",))

    def test_uneven_heights_uses_side_cameras(self):
        rule = UnevenHeightsRule(self.thresholds)
        self.assertEqual(rule.name, "uneven_heights")
        self.assertTrue(set(rule.ROLES).issubset({"LEFT", "RIGHT"}))


if __name__ == "__main__":
    unittest.main()
