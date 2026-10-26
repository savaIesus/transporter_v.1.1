"""ThresholdLoader: flatten секций LEFT/MIDDLE/RIGHT и валидация."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from domain.threshold_loader import (
    OPTIONAL_PARAM_LABELS,
    PARAM_LABELS,
    ROLE_SECTIONS,
    RULE_GROUPS,
    ThresholdLoader,
    describe_role_parameters,
)

SAMPLE = {
    "LEFT": {
        "part_presence_min_confidence": 0.6,
        "part_presence_min_windows": 1,
        "uneven_heights_min_confidence": 0.7,
        "uneven_heights_height_min_px": 20,
        "uneven_heights_height_max_px": 42,
        "uneven_heights_height_difference_px": 11,
        "uneven_heights_min_intersection_gap_px": 7,
        "window_sinks_min_confidence": 0.8,
    },
    "MIDDLE": {
        "bottom_glass_min_confidence": 0.65,
        "side_glass_min_confidence": 0.65,
        "welding_min_confidence": 0.65,
    },
    "RIGHT": {
        "part_presence_min_confidence": 0.6,
        "part_presence_min_windows": 1,
        "uneven_heights_min_confidence": 0.7,
        "uneven_heights_height_min_px": 21,
        "uneven_heights_height_max_px": 47,
        "uneven_heights_height_difference_px": 11,
        "uneven_heights_min_intersection_gap_px": 7,
        "window_sinks_min_confidence": 0.8,
    },
    "disabled_rules": [],
}


class ThresholdLoaderTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(self.path, "w", encoding="utf-8") as stream:
            json.dump(SAMPLE, stream)
        self.addCleanup(os.remove, self.path)

    def test_flattens_role_sections(self):
        loader = ThresholdLoader(self.path)
        data = loader.get_all()
        self.assertIn("LEFT.window_sinks_min_confidence", data)
        self.assertIn("MIDDLE.welding_min_confidence", data)
        self.assertIn("RIGHT.uneven_heights_height_min_px", data)
        self.assertEqual(data["MIDDLE.bottom_glass_min_confidence"], 0.65)

    def test_required_keys_cover_three_roles(self):
        # Каждая роль даёт хотя бы один обязательный порог.
        required = ThresholdLoader.REQUIRED_KEYS
        for role in ROLE_SECTIONS:
            self.assertTrue(
                any(k.startswith(role + ".") for k in required),
                f"нет порогов для роли {role}",
            )

    def test_rule_groups_cover_three_camera_rules(self):
        ids = {group[0] for group in RULE_GROUPS}
        self.assertEqual(
            ids,
            {
                "part_presence",
                "uneven_heights",
                "window_sinks",
                "bottom_glass",
                "side_glass",
                "welding",
            },
        )

    def test_validate_rejects_missing_key(self):
        with self.assertRaises(ValueError):
            ThresholdLoader.validate({"LEFT.window_sinks_min_confidence": 0.8})

    def test_labels_saved_and_loaded(self):
        labels = {"MIDDLE.welding_min_confidence": "Сварка"}
        thresholds = ThresholdLoader(self.path).get_all()
        ThresholdLoader.save_file(self.path, thresholds, labels=labels)
        loader = ThresholdLoader(self.path)
        self.assertEqual(
            loader.labels.get("MIDDLE.welding_min_confidence"), "Сварка",
        )

    def test_bottom_glass_zone_thresholds_are_optional(self):
        # Старый thresholds.json без порогов зоны исключения должен грузиться.
        data = ThresholdLoader(self.path).get_all()
        self.assertNotIn("MIDDLE.side_glass_zone_min_confidence", data)
        self.assertIn("side_glass_zone_min_confidence", OPTIONAL_PARAM_LABELS)
        self.assertIn("bottom_glass_exclusion_overlap", OPTIONAL_PARAM_LABELS)
        for key in ThresholdLoader.REQUIRED_KEYS:
            self.assertNotIn(key.split(".", 1)[1], OPTIONAL_PARAM_LABELS)

    def test_bottom_glass_zone_thresholds_are_grouped(self):
        thresholds = ThresholdLoader(self.path).get_all()
        thresholds["MIDDLE.side_glass_zone_min_confidence"] = 0.25
        thresholds["MIDDLE.bottom_glass_exclusion_overlap"] = 0.5
        groups = {
            group["rule"]: {param["key"] for param in group["params"]}
            for group in describe_role_parameters("MIDDLE", thresholds)
        }
        self.assertIn("bottom_glass_exclusion_overlap", groups["bottom_glass"])
        self.assertIn("side_glass_zone_min_confidence", groups["side_glass"])
        overlap = [
            param
            for group in describe_role_parameters("MIDDLE", thresholds)
            for param in group["params"]
            if param["key"] == "bottom_glass_exclusion_overlap"
        ][0]
        self.assertEqual((overlap["min"], overlap["max"]), (0, 1))

    def test_overlap_threshold_must_be_in_range(self):
        data = ThresholdLoader(self.path).get_all()
        data["MIDDLE.bottom_glass_exclusion_overlap"] = 1.5
        with self.assertRaises(ValueError):
            ThresholdLoader.validate(data)

    def test_param_labels_complete(self):
        self.assertIn("window_sinks_min_confidence", PARAM_LABELS)
        self.assertIn("welding_min_confidence", PARAM_LABELS)
        self.assertIn("side_glass_min_confidence", PARAM_LABELS)


if __name__ == "__main__":
    unittest.main()
