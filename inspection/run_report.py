"""Карточки замеров и статус области для единственного кадра.

Итог правила равен его ``triggered``. Модуль только проверяет контракт
результата и кладёт в ``details`` поля ``measurement_cards`` /
``role_status`` для правой панели HMI.
"""

from __future__ import annotations

from copy import deepcopy

from core.rule_report.cards import build_presence_summary, build_rule_summary


# Причины, по которым правило «не смогло построить область» (fail-closed):
# отсутствие/невалидность области означает срабатывание, а не пропуск.
# По этим маркерам UI показывает «ОБЛАСТЬ НЕ ПОСТРОЕНА».
REGION_MISSING_MARKERS = (
    "no_detections", "missing_or_invalid_mask", "mask_too_small",
    "no_valid_omission", "no_valid_omission_top_line",
    "omission_reference_too_short",
    "no_valid_platform", "invalid_platform_bbox",
    "invalid_platform_orientation", "invalid_contact_masks",
    "insufficient_valid_contact_masks", "insufficient_valid_contacts",
    "invalid_contact_layout", "layout_groups_failed",
    "contact_boundary_not_built", "inner_platform_reference_not_fitted",
    "reference_invalid", "missing_glass_mask", "missing_pin_mask",
    "empty_case_ring", "case_central_not_inside_case",
    "invalid_case_count", "invalid_case_central_count", "invalid_case_ring",
    "invalid_window_reference_count", "invalid_window_masks",
    "invalid_sink_masks",
)


class InspectionReportError(RuntimeError):
    """Невозможно получить валидный результат инспекции."""


def summarize_model_health(model_health) -> list[dict]:
    """Свернуть строки camera/model в UI-строки одного замера."""
    summary = []
    for row in model_health or []:
        if not isinstance(row, dict):
            continue
        elapsed = float(row.get("elapsed_ms") or 0.0)
        summary.append({
            "role": row.get("role"),
            "model": row.get("model"),
            "ok": bool(row.get("ok")),
            "elapsed_ms": elapsed,
            "detections": int(row.get("detections") or 0),
            "error": row.get("error"),
        })
    return summary


def _measurement_cards(rule_name: str, result) -> list:
    details = getattr(result, "details", {}) or {}
    if rule_name == "part_presence":
        return build_presence_summary(details)
    per_role = details.get("per_role")
    if isinstance(per_role, dict) and per_role:
        return build_rule_summary(rule_name, details)
    return []


def _region_missing(role_details: dict) -> bool:
    if not isinstance(role_details, dict):
        return False
    # skipped — отдельный статус «нет измерения», не путать с fail-closed.
    if role_details.get("skipped"):
        return False
    if role_details.get("valid") is False:
        return True
    reason = role_details.get("reason")
    if isinstance(reason, str):
        return any(reason.startswith(marker) for marker in REGION_MISSING_MARKERS)
    return False


def _measurement_status(rule_name: str, result) -> list:
    details = getattr(result, "details", {}) or {}
    if rule_name == "part_presence":
        return [{
            "role": "INSPECT",
            "status": "ПУСТО" if details.get("empty_tray") else "КОРПУС",
            "reason": None,
        }]

    per_role = details.get("per_role")
    if not isinstance(per_role, dict) or not per_role:
        return []

    rows = []
    for role, role_details in per_role.items():
        if not isinstance(role_details, dict):
            continue
        if _region_missing(role_details):
            rows.append({
                "role": role,
                "status": "ОБЛАСТЬ НЕ ПОСТРОЕНА",
                "reason": role_details.get("reason"),
            })
        elif role_details.get("triggered"):
            rows.append({"role": role, "status": "ОТКЛОНЕНИЕ", "reason": None})
        elif role_details.get("skipped"):
            rows.append({"role": role, "status": "НЕТ ИЗМЕРЕНИЯ", "reason": None})
        else:
            rows.append({"role": role, "status": "В НОРМЕ", "reason": None})
    return rows


def attach_measurement(result):
    """Дописать карточки и статус области в ``details`` результата."""
    rule_name = str(getattr(result, "rule_name", "") or "")
    details = deepcopy(getattr(result, "details", {}) or {})
    details["measurement_cards"] = _measurement_cards(rule_name, result)
    details["role_status"] = _measurement_status(rule_name, result)
    result.details = details
    return result


def prepare_presence_result(result):
    """Проверить part_presence и вернуть результат с карточками замера."""
    if getattr(result, "rule_name", None) != "part_presence":
        raise InspectionReportError("part_presence: неверное правило")
    details = getattr(result, "details", None)
    if not isinstance(details, dict) or type(details.get("empty_tray")) is not bool:
        raise InspectionReportError("part_presence: нет bool empty_tray")
    final_result = deepcopy(result)
    final_result.triggered = False
    return attach_measurement(final_result)


def prepare_rule_results(results) -> list:
    """Проверить defect rules и вернуть их с карточками замера."""
    prepared = list(results or [])
    if not prepared:
        return []

    names = [str(getattr(result, "rule_name", "")) for result in prepared]
    if any(not name for name in names):
        raise InspectionReportError("defect rules: найдено правило без имени")
    if len(names) != len(set(names)):
        raise InspectionReportError("defect rules: имена правил дублируются")
    for result in prepared:
        if type(getattr(result, "triggered", None)) is not bool:
            raise InspectionReportError(
                f"{getattr(result, 'rule_name', '')}: правило вернуло не-bool triggered"
            )

    return [attach_measurement(deepcopy(result)) for result in prepared]
