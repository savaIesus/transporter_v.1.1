"""Формирование строк отчёта по правилам дефектов для HMI (3 камеры).

Публичный API пакета — сборка строк:
:func:`build_rule_report_row` и :func:`build_rule_report_rows`.
"""

from core.rule_report.constants import (
    DETAILED_RULES,
    METRIC_PARAM_LABELS,
    NO_MEASUREMENT,
    PART_PRESENCE_RULE,
    RULE_CAMERA_ROLES,
    RULE_LABELS,
    SUMMARY_LINES_LIMIT,
)
from core.rule_report.cards import build_presence_summary, build_rule_summary
from core.rule_report.human_cause import HUMAN_CAUSE_MAP, get_human_cause
from core.rule_report.row import build_rule_report_row, build_rule_report_rows
from core.rule_report.scope import (
    filter_rule_report_rows,
    rule_applies_to_role,
    scope_rule_result_to_role,
)

__all__ = [
    "build_rule_report_row",
    "build_rule_report_rows",
    "filter_rule_report_rows",
    "build_presence_summary",
    "build_rule_summary",
    "rule_applies_to_role",
    "scope_rule_result_to_role",
    "get_human_cause",
    "HUMAN_CAUSE_MAP",
    "DETAILED_RULES",
    "METRIC_PARAM_LABELS",
    "NO_MEASUREMENT",
    "PART_PRESENCE_RULE",
    "RULE_CAMERA_ROLES",
    "RULE_LABELS",
    "SUMMARY_LINES_LIMIT",
]
