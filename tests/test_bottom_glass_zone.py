"""Исключение зоны ``side_glass`` из поиска стекла на дне (``bottom_glass``).

Модель ``side_glass.pt`` обводит на кадре MIDDLE область ожидаемого стекла
сбоку. Детекции ``bottom_glass`` внутри этой зоны — не дефект «стекло на
дне», поэтому правило обязано их отбрасывать, а стекло искать на всём
остальном кадре.
"""

from __future__ import annotations

import unittest

from domain.defect_rules import BottomGlassRule
from domain.geometry.zones import (
    build_zones,
    coverage_fraction,
    detection_region,
    point_in_polygon,
    polygon_area,
    split_by_zones,
)


def _rect(x1, y1, x2, y2):
    return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]


def _det(kind, confidence, bbox, mask=None):
    return {
        "kind": kind,
        "confidence": confidence,
        "bbox": list(bbox),
        "mask": mask,
    }


# Зона «стекло сбоку» — квадрат (100, 100) .. (300, 300).
ZONE_MASK = _rect(100, 100, 300, 300)
ZONE_BBOX = [100, 100, 300, 300]


def side_glass(confidence=0.9, mask=ZONE_MASK, bbox=ZONE_BBOX):
    return _det("side_glass", confidence, bbox, mask)


def bottom_glass(confidence=0.9, bbox=(120, 120, 280, 280), mask=None):
    if mask is None:
        mask = _rect(*bbox)
    return _det("bottom_glass", confidence, bbox, mask)


class ZonesGeometryTest(unittest.TestCase):
    def test_polygon_area_of_rectangle(self):
        self.assertAlmostEqual(polygon_area(_rect(0, 0, 10, 20)), 200.0)

    def test_point_in_polygon(self):
        square = _rect(0, 0, 10, 10)
        self.assertTrue(point_in_polygon((5, 5), square))
        self.assertFalse(point_in_polygon((15, 5), square))

    def test_detection_region_prefers_mask(self):
        region = detection_region(bottom_glass(mask=_rect(120, 120, 280, 280)))
        self.assertEqual(region["bbox"], (120.0, 120.0, 280.0, 280.0))
        self.assertAlmostEqual(region["area"], 160.0 * 160.0)

    def test_detection_region_falls_back_to_bbox(self):
        region = detection_region(_det("bottom_glass", 0.9, (10, 10, 50, 40)))
        self.assertIsNone(region["polygon"])
        self.assertAlmostEqual(region["area"], 40.0 * 30.0)

    def test_detection_region_rejects_broken_geometry(self):
        self.assertIsNone(detection_region({"bbox": [1, 2], "mask": None}))
        self.assertIsNone(detection_region({"bbox": [5, 5, 5, 5], "mask": None}))
        self.assertIsNone(detection_region("not a detection"))

    def test_coverage_is_one_inside_zone(self):
        zones = build_zones([side_glass()])
        self.assertAlmostEqual(
            coverage_fraction(detection_region(bottom_glass()), zones), 1.0,
        )

    def test_coverage_is_zero_outside_zone(self):
        zones = build_zones([side_glass()])
        outside = bottom_glass(bbox=(400, 400, 500, 500))
        self.assertAlmostEqual(
            coverage_fraction(detection_region(outside), zones), 0.0,
        )

    def test_broken_zone_is_skipped(self):
        self.assertEqual(build_zones([{"mask": None, "bbox": None}]), [])

    def test_split_disabled_with_zero_coverage(self):
        zones = build_zones([side_glass()])
        det = bottom_glass()
        kept, excluded = split_by_zones([det], zones, min_coverage=0)
        self.assertEqual(kept, [det])
        self.assertEqual(excluded, [])

    def test_split_without_zones_keeps_everything(self):
        det = bottom_glass()
        kept, excluded = split_by_zones([det], [], min_coverage=0.5)
        self.assertEqual(kept, [det])
        self.assertEqual(excluded, [])


class BottomGlassZoneTest(unittest.TestCase):
    def setUp(self):
        self.thresholds = {
            "MIDDLE.bottom_glass_min_confidence": 0.65,
            "MIDDLE.side_glass_zone_min_confidence": 0.25,
            "MIDDLE.bottom_glass_exclusion_overlap": 0.5,
            "disabled_rules": [],
        }

    def rule(self, **overrides):
        thresholds = dict(self.thresholds)
        thresholds.update(overrides)
        return BottomGlassRule(thresholds)

    def check(self, detections, **overrides):
        return self.rule(**overrides).check({"MIDDLE": detections})

    def test_glass_inside_side_glass_zone_is_ignored(self):
        result = self.check([side_glass(), bottom_glass(bbox=(120, 120, 280, 280))])
        self.assertFalse(result.triggered)
        self.assertIsNone(result.defect)
        self.assertEqual(result.drawings, [])
        details = result.details["per_role"]["MIDDLE"]
        self.assertEqual(details["found"], 0)
        self.assertEqual(details["found_before_exclusion"], 1)
        self.assertEqual(details["excluded"], 1)
        self.assertEqual(details["exclusion_zone_kind"], "side_glass")
        self.assertEqual(details["exclusion_zone_count"], 1)
        self.assertIsNone(details["reason"])

    def test_glass_outside_zone_still_triggers(self):
        result = self.check([
            side_glass(),
            bottom_glass(bbox=(400, 400, 500, 500)),
        ])
        self.assertTrue(result.triggered)
        details = result.details["per_role"]["MIDDLE"]
        self.assertEqual(details["found"], 1)
        self.assertEqual(details["excluded"], 0)
        self.assertEqual(details["reason"], "glass_found")
        self.assertEqual(len(result.drawings), 1)

    def test_glass_found_beside_zone_and_inside_zone(self):
        # На кадре и дефект вне зоны, и стекло внутри зоны: дефект остаётся.
        result = self.check([
            side_glass(),
            bottom_glass(bbox=(120, 120, 280, 280)),
            bottom_glass(bbox=(400, 400, 500, 500)),
        ])
        self.assertTrue(result.triggered)
        details = result.details["per_role"]["MIDDLE"]
        self.assertEqual(details["found"], 1)
        self.assertEqual(details["excluded"], 1)

    def test_partial_overlap_below_threshold_is_defect(self):
        # Четверть площади детекции в зоне — стекло ищется по всему кадру.
        result = self.check([
            side_glass(),
            bottom_glass(bbox=(275, 120, 375, 280)),
        ])
        self.assertTrue(result.triggered)
        self.assertEqual(result.details["per_role"]["MIDDLE"]["excluded"], 0)

    def test_overlap_threshold_is_configurable(self):
        result = self.check(
            [side_glass(), bottom_glass(bbox=(275, 120, 375, 280))],
            **{"MIDDLE.bottom_glass_exclusion_overlap": 0.2},
        )
        self.assertFalse(result.triggered)
        self.assertEqual(result.details["per_role"]["MIDDLE"]["excluded"], 1)

    def test_exclusion_can_be_disabled_by_zero_overlap(self):
        result = self.check(
            [side_glass(), bottom_glass()],
            **{"MIDDLE.bottom_glass_exclusion_overlap": 0},
        )
        self.assertTrue(result.triggered)

    def test_zone_found_by_low_confidence_detection(self):
        # Уверенность ниже порога правила side_glass, но выше порога зоны.
        result = self.check([side_glass(0.4), bottom_glass()])
        self.assertFalse(result.triggered)
        self.assertEqual(result.details["per_role"]["MIDDLE"]["excluded"], 1)

    def test_zone_ignores_detection_below_zone_threshold(self):
        result = self.check([side_glass(0.1), bottom_glass()])
        self.assertTrue(result.triggered)
        self.assertEqual(result.details["per_role"]["MIDDLE"]["exclusion_zone_count"], 0)

    def test_zone_is_built_from_bbox_without_mask(self):
        result = self.check([side_glass(mask=None), bottom_glass()])
        self.assertFalse(result.triggered)
        self.assertEqual(result.details["per_role"]["MIDDLE"]["excluded"], 1)

    def test_zone_from_other_role_is_not_used(self):
        result = self.rule().check({
            "LEFT": [side_glass()],
            "MIDDLE": [bottom_glass()],
        })
        self.assertTrue(result.triggered)
        self.assertEqual(result.details["per_role"]["MIDDLE"]["exclusion_zone_count"], 0)

    def test_other_kinds_are_not_zones(self):
        welding = _det("welding", 0.99, ZONE_BBOX, ZONE_MASK)
        result = self.check([welding, bottom_glass()])
        self.assertTrue(result.triggered)
        self.assertEqual(result.details["per_role"]["MIDDLE"]["excluded"], 0)

    def test_glass_below_confidence_is_not_a_defect(self):
        result = self.check([side_glass(), bottom_glass(0.5, (400, 400, 500, 500))])
        self.assertFalse(result.triggered)
        details = result.details["per_role"]["MIDDLE"]
        self.assertEqual(details["found"], 0)
        self.assertEqual(details["found_before_exclusion"], 0)

    def test_defaults_when_new_thresholds_missing(self):
        rule = BottomGlassRule({"disabled_rules": []})
        result = rule.check({"MIDDLE": [side_glass(), bottom_glass()]})
        self.assertFalse(result.triggered)
        details = result.details["per_role"]["MIDDLE"]
        self.assertAlmostEqual(details["exclusion_zone_min_confidence"], 0.25)
        self.assertAlmostEqual(details["exclusion_overlap"], 0.5)
        self.assertEqual(details["found_before_exclusion"], 1)

    def test_disabled_rule_is_skipped(self):
        rule = self.rule(disabled_rules=["bottom_glass"])
        result = rule.check({"MIDDLE": [side_glass(), bottom_glass()]})
        self.assertFalse(result.triggered)
        self.assertEqual(result.details["skipped"], "rule disabled")


if __name__ == "__main__":
    unittest.main()
