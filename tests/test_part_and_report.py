"""Маршрутизация детали и формат отчёта по правилам (3 камеры)."""

from __future__ import annotations

import types
import unittest

from core.rule_report import (
    PART_PRESENCE_RULE,
    RULE_CAMERA_ROLES,
    RULE_LABELS,
    build_rule_report_row,
    build_rule_report_rows,
    rule_applies_to_role,
    scope_rule_result_to_role,
)
from domain.part import (
    CATEGORY_BAD,
    CATEGORY_CLEANUP,
    CATEGORY_GOOD,
    CLEANUP_DEFECTS,
    Part,
)


def _result(rule_name, triggered=False, details=None):
    return types.SimpleNamespace(
        rule_name=rule_name,
        triggered=triggered,
        details=details or {},
        drawings=[],
    )


class PartRoutingTest(unittest.TestCase):
    def test_good_when_no_defects(self):
        part = Part(1, 0)
        part.mark_input_done()
        self.assertEqual(part.route_category, CATEGORY_GOOD)
        self.assertEqual(part.final_decision, "none")

    def test_cleanup_defects_route_to_cleanup(self):
        for defect in CLEANUP_DEFECTS:
            part = Part(1, 0)
            part.add_input_defect(defect)
            part.mark_input_done()
            self.assertEqual(part.route_category, CATEGORY_CLEANUP, defect)
            self.assertEqual(part.final_decision, defect)

    def test_window_sinks_routes_bad(self):
        part = Part(1, 0)
        part.add_input_defect("window_sinks")
        part.mark_input_done()
        self.assertEqual(part.route_category, CATEGORY_BAD)

    def test_priority_window_sinks_over_glass(self):
        part = Part(1, 0)
        part.add_input_defect("bottom_glass")
        part.add_input_defect("window_sinks")
        part.mark_input_done()
        # Раковины имеют больший приоритет -> BAD.
        self.assertEqual(part.route_category, CATEGORY_BAD)
        self.assertEqual(part.final_decision, "window_sinks")

    def test_input_defects_compat_property(self):
        part = Part(1, 0)
        part.add_input_defect("welding")
        self.assertEqual(part.input_defects, ["welding"])
        self.assertEqual(part.get_all_defects(), ["welding"])


class RuleCameraRolesTest(unittest.TestCase):
    def test_middle_rules(self):
        self.assertEqual(RULE_CAMERA_ROLES["bottom_glass"], ("MIDDLE",))
        self.assertEqual(RULE_CAMERA_ROLES["welding"], ("MIDDLE",))

    def test_side_rules(self):
        for rule in ("uneven_heights", "window_sinks", "part_presence"):
            self.assertTrue(
                set(RULE_CAMERA_ROLES[rule]).issubset({"LEFT", "RIGHT"}),
                rule,
            )

    def test_rule_applies_to_role(self):
        self.assertTrue(rule_applies_to_role("welding", "MIDDLE"))
        self.assertFalse(rule_applies_to_role("welding", "LEFT"))
        # Без роли правило относится ко всем.
        self.assertTrue(rule_applies_to_role("welding", None))

    def test_all_rules_have_labels(self):
        for rule in (
            "part_presence",
            "uneven_heights",
            "window_sinks",
            "bottom_glass",
            "welding",
        ):
            self.assertIn(rule, RULE_LABELS)


class RuleReportRowTest(unittest.TestCase):
    def test_ok_row_shape(self):
        row = build_rule_report_row(_result("window_sinks", False))
        self.assertEqual(row["name"], "window_sinks")
        self.assertFalse(row["triggered"])
        self.assertEqual(row["status_label"], "НОРМА")
        self.assertIn("summary_cards", row)
        self.assertIn("measurement_cards", row)
        self.assertIn("role_status", row)

    def test_triggered_row_has_human_cause(self):
        row = build_rule_report_row(_result("bottom_glass", True))
        self.assertTrue(row["triggered"])
        self.assertEqual(row["human_cause"], "СТЕКЛО НА ДНЕ")
        self.assertTrue(row["decisive"])

    def test_part_absent_collapses_rows(self):
        absent = _result(
            PART_PRESENCE_RULE, True, {"empty_tray": True},
        )
        rows = build_rule_report_rows([
            absent,
            _result("window_sinks", False),
            _result("welding", True),
        ])
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["part_absent"])

    def test_scope_to_role_filters_other_roles(self):
        result = _result(
            "welding", True,
            {"per_role": {"MIDDLE": {"triggered": True, "found": 1}}},
        )
        self.assertIsNone(scope_rule_result_to_role(result, "LEFT"))
        scoped = scope_rule_result_to_role(result, "MIDDLE")
        self.assertIsNotNone(scoped)
        self.assertTrue(scoped.triggered)


if __name__ == "__main__":
    unittest.main()
