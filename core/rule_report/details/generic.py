"""Общие строки сработавших ролей для правил трёхкамерной линии.

Правила ``uneven_heights``, ``window_sinks``, ``bottom_glass`` и
``welding`` не имеют собственных форматтеров: для каждого собирается
простое описание ``reason`` с числом найденных объектов.
"""


def _uneven_heights_failures(role_details: dict) -> list:
    """Причины срабатывания правила ``uneven_heights`` (замеры ячеек)."""
    reason = role_details.get("reason")
    heights = role_details.get("heights") or []
    limits = (
        f"min={role_details.get('height_min_px')}, "
        f"max={role_details.get('height_max_px')}, "
        f"допуск разброса={role_details.get('height_difference_px')} px"
    )
    rows = []
    if reason == "height_above_max":
        rows.append(f"макс. высота {role_details.get('h_max')} px выше порога")
    elif reason == "height_below_min":
        rows.append(f"мин. высота {role_details.get('h_min')} px ниже порога")
    elif reason == "spread_exceeded":
        rows.append(f"разброс высот {role_details.get('spread')} px выше порога")
    if heights:
        rows.append("высоты: " + ", ".join(str(h) for h in heights))
    rows.append(limits)
    return rows


def _counted_failures(role_details: dict, label: str) -> list:
    """Причины срабатывания бинарного правила по числу детекций."""
    found = role_details.get("found")
    min_confidence = role_details.get("min_confidence")
    rows = [f"{label}: {int(found or 0)} шт"]
    if min_confidence is not None:
        rows.append(f"порог уверенности {min_confidence}")
    return rows


_GENERIC_RULE_BUILDERS = {
    "uneven_heights": _uneven_heights_failures,
    "window_sinks": lambda rd: _counted_failures(rd, "раковины"),
    "bottom_glass": lambda rd: _counted_failures(rd, "стекло"),
    "welding": lambda rd: _counted_failures(rd, "дефекты сварки"),
}


def _generic_failure_rows(rule_name: str, per_role: dict) -> list:
    """Строки сработавших ролей для правил без собственного форматтера.

    Для правил со своим сборщиком причин (``_GENERIC_RULE_BUILDERS``)
    вызывается он; остальные получают простое описание ``reason``.
    """
    failure_rows = []
    for role, role_details in per_role.items():
        if (
            not isinstance(role_details, dict)
            or not role_details.get("triggered")
        ):
            continue
        reason = role_details.get("reason")
        builder = _GENERIC_RULE_BUILDERS.get(rule_name)
        if builder is not None:
            failures = builder(role_details)
        elif reason:
            failures = [str(reason)]
        else:
            failures = []
        if failures:
            failure_rows.append(f"{role}: " + "; ".join(failures))
    return failure_rows
