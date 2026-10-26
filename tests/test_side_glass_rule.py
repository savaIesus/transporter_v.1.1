"""Правило ``side_glass``: детекционное правило по камере MIDDLE."""

from __future__ import annotations

import unittest

from domain.defect_rules import SideGlassRule


def _det(kind, confidence, bbox=(0, 0, 10, 10)):
    return {
        "kind": kind,
        "confidence": confidence,
        "bbox": list(bbox),
        "mask": None,
    }


class SideGlassRuleTest(unittest.TestCase):
    def setUp(self):
        self.thresholds = {
            "MIDDLE.side_glass_min_confidence": 0.65,
            "disabled_rules": [],
        }

    def rule(self, **overrides):
        thresholds = dict(self.thresholds)
        thresholds.update(overrides)
        return SideGlassRule(thresholds)

    def test_triggers_on_detection_above_threshold(self):
        result = self.rule().check({
            "MIDDLE": [_det("side_glass", 0.9)],
        })
        self.assertTrue(result.triggered)
        self.assertEqual(result.defect, "side_glass")
        self.assertEqual(result.details["per_role"]["MIDDLE"]["found"], 1)
        self.assertEqual(
            result.details["per_role"]["MIDDLE"]["reason"], "side_glass_found",
        )
        self.assertEqual(len(result.drawings), 1)
        self.assertEqual(result.drawings[0]["rule"], "side_glass")

    def test_ignores_detection_below_threshold(self):
        result = self.rule().check({
            "MIDDLE": [_det("side_glass", 0.5)],
        })
        self.assertFalse(result.triggered)
        self.assertIsNone(result.defect)
        self.assertEqual(result.drawings, [])

    def test_ignores_other_model_kinds(self):
        # Стекло на дне и сварка не должны срабатывать как стекло сбоку.
        result = self.rule().check({
            "MIDDLE": [
                _det("bottom_glass", 0.99),
                _det("welding", 0.99),
            ],
        })
        self.assertFalse(result.triggered)

    def test_only_middle_role_is_checked(self):
        rule = self.rule()
        self.assertEqual(rule.ROLES, ("MIDDLE",))
        result = rule.check({
            "LEFT": [_det("side_glass", 0.99)],
            "MIDDLE": [],
        })
        self.assertFalse(result.triggered)
        self.assertEqual(set(result.details["per_role"]), {"MIDDLE"})

    def test_per_role_threshold_overrides_common(self):
        rule = self.rule(**{"MIDDLE.side_glass_min_confidence": 0.95})
        result = rule.check({"MIDDLE": [_det("side_glass", 0.9)]})
        self.assertFalse(result.triggered)

    def test_default_threshold_when_missing(self):
        rule = SideGlassRule({"disabled_rules": []})
        result = rule.check({"MIDDLE": [_det("side_glass", 0.7)]})
        self.assertTrue(result.triggered)
        self.assertEqual(
            result.details["per_role"]["MIDDLE"]["min_confidence"], 0.65,
        )

    def test_disabled_rule_is_skipped(self):
        rule = self.rule(disabled_rules=["side_glass"])
        self.assertFalse(rule.enabled)
        result = rule.check({"MIDDLE": [_det("side_glass", 0.99)]})
        self.assertFalse(result.triggered)
        self.assertEqual(result.details["skipped"], "rule disabled")


if __name__ == "__main__":
    unittest.main()
