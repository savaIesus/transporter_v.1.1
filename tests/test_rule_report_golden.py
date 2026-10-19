"""Golden-master для :mod:`core.rule_report` (трёхкамерная линия).

Корпус синтетических RuleResult-ов покрывает все правила трёхкамерника,
ветки причин срабатывания и краевые случаи (skip, нераспознанные reason,
срезы по ролям, presence-карточки). Вывод хешируется (SHA-256) и
сравнивается с GOLDEN_SHA256; любое расхождение — либо намеренное
изменение формата строк (тогда обнови константу), либо регрессия.

Обновление снапшота после намеренного изменения формата:
    python -m tests.test_rule_report_golden
"""
import hashlib
import json
import os
import unittest
from types import SimpleNamespace

from core.rule_report import (
    DETAILED_RULES,
    build_rule_report_row,
    build_rule_report_rows,
    scope_rule_result_to_role,
)
from domain.part import Part
from domain.threshold_loader import ThresholdLoader

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN_SHA256 = "925bbc37c5539ab31958c3f488f0f9777ab1171f274bc2a85a5beede30a82b96"


def res(name, triggered, details):
    return SimpleNamespace(rule_name=name, triggered=triggered,
                           details=details, drawings=[])


def role(name="LEFT", **kw):
    return {name: kw}


CASES = [
    # --- part_presence ---
    res("part_presence", False, {
        "empty_tray": False,
        "windows_by_role": {"LEFT": 4, "RIGHT": 3},
        "min_windows_by_role": {"LEFT": 1, "RIGHT": 1},
        "presence_by_role": {"LEFT": True, "RIGHT": True},
    }),
    res("part_presence", False, {
        "empty_tray": True,
        "windows_by_role": {"LEFT": 0, "RIGHT": 0},
        "min_windows_by_role": {"LEFT": 1, "RIGHT": 1},
        "presence_by_role": {"LEFT": False, "RIGHT": False},
    }),
    # --- uneven_heights ---
    res("uneven_heights", False, {"per_role": role(
        "LEFT", triggered=False, reason=None, found=4, measured=4,
        heights=[30.0, 31.5], h_max=31.5, h_min=30.0, spread=1.5,
        height_min_px=20, height_max_px=42, height_difference_px=11,
    )}),
    res("uneven_heights", True, {"per_role": role(
        "RIGHT", triggered=True, reason="height_above_max",
        found=3, measured=3, heights=[80.0, 30.0],
        h_max=80.0, h_min=30.0, spread=50.0,
        height_min_px=21, height_max_px=47, height_difference_px=11,
    )}),
    res("uneven_heights", True, {"per_role": role(
        "RIGHT", triggered=True, reason="spread_exceeded",
        found=3, measured=3, heights=[25.0, 45.0],
        h_max=45.0, h_min=25.0, spread=20.0,
        height_min_px=21, height_max_px=47, height_difference_px=11,
    )}),
    # --- бинарные правила по числу детекций ---
    res("window_sinks", True, {"per_role": role(
        "LEFT", triggered=True, reason="sinks_found", found=2,
        min_confidence=0.8,
    )}),
    res("window_sinks", False, {"per_role": role(
        "RIGHT", triggered=False, reason=None, found=0, min_confidence=0.8,
    )}),
    res("bottom_glass", True, {"per_role": role(
        "MIDDLE", triggered=True, reason="glass_found", found=1,
        min_confidence=0.65,
    )}),
    res("welding", True, {"per_role": role(
        "MIDDLE", triggered=True, reason="welding_defect_found", found=1,
        min_confidence=0.65,
    )}),
    # --- skip / нераспознанный reason / пустая per_role ---
    res("welding", False, {"per_role": role(
        "MIDDLE", skipped=True, reason="rule disabled",
    )}),
    res("bottom_glass", True, {"per_role": role(
        "MIDDLE", triggered=True, reason="very_new_reason",
    )}),
    res("window_sinks", False, {}),
    # --- карточки замера, прикреплённые run_report ---
    res("uneven_heights", True, {
        "per_role": role(
            "LEFT", triggered=True, reason="height_below_min",
            found=2, measured=2, heights=[12.0], h_max=12.0, h_min=12.0,
            spread=0.0, height_min_px=20, height_max_px=42,
            height_difference_px=11,
        ),
        "measurement_cards": [{
            "role": "LEFT", "ok": False, "verdict": "отклонение",
            "found": ["объекты: 2"], "metrics": [
                {"label": "Высота ячейки, px", "value": "12", "limit": "42",
                 "ok": False, "key": "height_px"},
            ],
        }],
        "role_status": [
            {"role": "LEFT", "status": "ОТКЛОНЕНИЕ", "reason": None},
        ],
    }),
]


def _stable(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      default=str)


def render_corpus() -> str:
    lines = []
    for case in CASES:
        lines.append(_stable(build_rule_report_row(case)))
    # Срез по роли фильтрует чужие правила и срабатывания.
    for case in CASES:
        scoped = scope_rule_result_to_role(case, "LEFT")
        if scoped is not None:
            lines.append(_stable(build_rule_report_row(scoped)))
    # Полный набор строк с фильтрацией решающих.
    lines.append(_stable(build_rule_report_rows(CASES)))
    lines.append(_stable(build_rule_report_rows(CASES, role="MIDDLE")))
    return "\n".join(lines)


class RuleReportGoldenTest(unittest.TestCase):
    def test_output_matches_snapshot(self):
        self.assertEqual(
            hashlib.sha256(render_corpus().encode("utf-8")).hexdigest(),
            GOLDEN_SHA256,
        )

    def test_detailed_rules_are_known(self):
        known = {
            "part_presence", "uneven_heights", "window_sinks",
            "bottom_glass", "welding",
        }
        self.assertTrue(set(DETAILED_RULES) <= known)

    def test_triggered_rows_carry_threshold_conclusion(self):
        row = build_rule_report_row(CASES[3])
        self.assertTrue(row["triggered"])
        self.assertTrue(row["human_cause"])
        self.assertTrue(row["summary_lines"])

    def test_presence_absent_collapses_rows(self):
        rows = build_rule_report_rows([
            CASES[0],
            res("part_presence", False, {
                "empty_tray": True,
                "windows_by_role": {"LEFT": 0},
            }),
        ])
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["part_absent"])


class PartRoutingTest(unittest.TestCase):
    """Приоритеты решения Part (трёхкамерная линия)."""

    @staticmethod
    def thresholds():
        return ThresholdLoader(
            os.path.join(REPO_ROOT, "thresholds.json")
        ).get_all()

    def test_priorities(self):
        part = Part(1, 0)
        for defect in ("bottom_glass", "window_sinks"):
            part.add_input_defect(defect)
        part.mark_input_done()
        self.assertEqual(part.route_category, "BAD")
        self.assertEqual(part.final_decision, "window_sinks")

    def test_cleanup_defect_routes_to_cleanup(self):
        part = Part(2, 0)
        part.add_input_defect("welding")
        part.mark_input_done()
        self.assertEqual(part.route_category, "CLEANUP")
        self.assertEqual(part.final_decision, "welding")


if __name__ == "__main__":
    print(hashlib.sha256(render_corpus().encode("utf-8")).hexdigest())
    unittest.main()
