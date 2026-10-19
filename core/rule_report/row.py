"""Сборка строк отчёта по результатам правил."""
import copy

from core.rule_report.cards import build_presence_summary, build_rule_summary

from core.rule_report.constants import (
    DETAILED_RULES,
    METRIC_PARAM_LABELS,
    PART_PRESENCE_RULE,
)
from core.rule_report.details import _DETAIL_FORMATTERS
from core.rule_report.details.generic import _generic_failure_rows
from core.rule_report.human_cause import get_human_cause
from core.rule_report.scope import (
    filter_rule_report_rows,
    scope_rule_result_to_role,
)
from core.rule_report.summary import (
    _skip_summary,
    _status_label,
    _summary_lines,
)
from core.rule_report.thresholds import (
    _fallback_role_status,
    _threshold_breaches,
    _threshold_conclusion,
)


def build_rule_report_rows(results, role: str | None = None) -> list:
    """Собрать строки отчёта; при ``role`` — только выбранная камера."""
    rows = []
    for result in results or []:
        if role:
            scoped = scope_rule_result_to_role(result, role)
            if scoped is None:
                continue
            rows.append(build_rule_report_row(scoped))
        else:
            rows.append(build_rule_report_row(result))
    return filter_rule_report_rows(rows)


def build_rule_report_row(result) -> dict:
    """Собрать одну строку отчёта по правилу для HMI и диагностики."""
    details = getattr(result, "details", {}) or {}
    rule_name = getattr(result, "rule_name", "")
    triggered = bool(result.triggered)
    per_role = details.get("per_role")
    has_per_role = isinstance(per_role, dict) and bool(per_role)

    detail = details.get("reason") or details.get("status")
    skipped = False
    if has_per_role:
        skip_text, skipped = _skip_summary(per_role)
        if skip_text:
            detail = skip_text

    detail_lines = []
    if has_per_role:
        formatter = _DETAIL_FORMATTERS.get(rule_name)
        if formatter is not None:
            detail_lines = formatter(per_role)
            if detail_lines:
                detail = "; ".join(detail_lines)
        elif triggered:
            failure_rows = _generic_failure_rows(rule_name, per_role)
            if failure_rows:
                detail = "; ".join(failure_rows)

    if rule_name == "part_presence":
        detail = (
            "КОРПУС НЕ ОБНАРУЖЕН"
            if details.get("empty_tray")
            else "Корпус обнаружен"
        )

    human_cause = None
    if triggered:
        human_cause = get_human_cause(rule_name, triggered, details)

    if not detail:
        detail = human_cause or ("Сработало" if triggered else "Норма")

    status_label, neutral = _status_label(rule_name, triggered, details)

    part_absent = bool(
        rule_name == PART_PRESENCE_RULE and details.get("empty_tray")
    )

    if rule_name == PART_PRESENCE_RULE:
        summary_cards = build_presence_summary(details)
    else:
        summary_cards = build_rule_summary(
            rule_name, details if has_per_role else {},
        )

    summary_lines = _summary_lines(
        rule_name,
        triggered,
        skipped,
        details,
        per_role if has_per_role else {},
        detail_lines,
        str(detail),
    )
    threshold_breaches = _threshold_breaches(summary_cards)
    threshold_conclusion = _threshold_conclusion(
        triggered, human_cause, threshold_breaches,
    )

    # Единственный замер для анализа кадра: значение метрики с порогом.
    # Метрики помечаются понятными названиями порогов (METRIC_PARAM_LABELS),
    # как в панели «Пороги правил»; без сопоставления остаётся название
    # самой метрики.
    cards = copy.deepcopy(details.get("measurement_cards") or summary_cards)
    if not isinstance(cards, list):
        cards = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        for metric in card.get("metrics") or []:
            key = metric.get("key")
            if not key:
                continue
            label = METRIC_PARAM_LABELS.get((rule_name, key))
            if label:
                metric["label"] = label

    role_status = details.get("role_status") or _fallback_role_status(cards)

    return {
        "name": result.rule_name,
        "triggered": triggered,
        "skipped": skipped,
        "status_label": status_label,
        "neutral": neutral,
        "show_detail": rule_name in DETAILED_RULES,
        "detail": str(detail),
        "human_cause": human_cause,
        "detail_lines": detail_lines,
        "summary_lines": summary_lines,
        "summary_cards": summary_cards,
        "measurement_cards": cards,
        "role_status": copy.deepcopy(role_status),
        "threshold_breaches": threshold_breaches,
        "threshold_conclusion": threshold_conclusion,
        "part_absent": part_absent,
        "decisive": bool(part_absent or triggered or skipped),
    }
