"""Карточки замеров правила для панели анализа кадра (3 камеры).

Карточка — ``{"role", "ok", "verdict", "found", "metrics"}``.
Метрика — ``{"label", "value", "limit", "ok", "key", "value_raw",
"limit_raw", "object"?}``. Формат совместим с панелью
«Анализ кадра» трёхкамерной версии.
"""

METRICS_PER_ROLE_LIMIT = 80

_UNKNOWN = "—"

_REASON_LABELS = {
    "height_above_max": "высота ячейки выше максимума",
    "height_below_min": "высота ячейки ниже минимума",
    "spread_exceeded": "разброс высот превышен",
    "sinks_found": "найдены раковины",
    "glass_found": "найдено стекло",
    "welding_defect_found": "найден дефект сварки",
}


def _number(value, digits=1):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number == int(number) and abs(number) < 1e9:
        return str(int(number))
    return f"{number:.{digits}f}"


def _metric(label, value, limit=None, ok=None, key=None, object=None):
    value_text = _number(value)
    if value_text is None:
        return None
    limit_text = _number(limit)
    value_raw = None
    limit_raw = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value_raw = float(value)
    if isinstance(limit, (int, float)) and not isinstance(limit, bool):
        limit_raw = float(limit)
    metric = {
        "label": label,
        "value": value_text,
        "limit": limit_text,
        "ok": None if ok is None else bool(ok),
        "value_raw": value_raw,
        "limit_raw": limit_raw,
        "key": key,
    }
    if object:
        metric["object"] = str(object)
    return metric


def _reason_text(reason):
    if not reason:
        return ""
    return _REASON_LABELS.get(str(reason), str(reason).replace("_", " "))


def _role_verdict(role_details: dict) -> tuple:
    if role_details.get("skipped"):
        return None, "нет измерения · правило отключено"
    if role_details.get("valid") is False:
        return False, "нет валидного измерения"
    if role_details.get("triggered"):
        reason = _reason_text(role_details.get("reason"))
        return False, f"отклонение{f' · {reason}' if reason else ''}"
    return True, "в допуске"


def _count_found(role_details: dict) -> list:
    found = []
    value = role_details.get("found")
    if value is not None:
        found.append(f"объекты: {_number(value)}")
    measured = role_details.get("measured")
    if measured is not None:
        found.append(f"измерено: {_number(measured)}")
    return found


def _role_metrics(rule_name: str, role_details: dict) -> list:
    metrics = []
    add = lambda m: metrics.append(m) if m is not None else None

    if rule_name == "uneven_heights":
        heights = role_details.get("heights") or []
        height_min = role_details.get("height_min_px")
        height_max = role_details.get("height_max_px")
        difference = role_details.get("height_difference_px")
        h_max = role_details.get("h_max")
        h_min = role_details.get("h_min")
        spread = role_details.get("spread")

        # По каждому окну — блок со своим замером.
        for index, h in enumerate(heights[:METRICS_PER_ROLE_LIMIT], start=1):
            h_ok = None
            if isinstance(h, (int, float)) and isinstance(height_min, (int, float)) and isinstance(height_max, (int, float)):
                h_ok = float(height_min) < float(h) < float(height_max)
            add(_metric(
                "Высота ячейки, px", h,
                limit=height_max,
                ok=h_ok,
                key="height_px",
                object=f"Окно #{index}",
            ))
        add(_metric(
            "Макс. высота, px", h_max,
            limit=height_max,
            ok=(None if h_max is None or height_max is None
                else float(h_max) < float(height_max)),
            key="height_max_px",
        ))
        add(_metric(
            "Мин. высота, px", h_min,
            limit=height_min,
            ok=(None if h_min is None or height_min is None
                else float(h_min) > float(height_min)),
            key="height_min_px",
        ))
        add(_metric(
            "Разброс высот, px", spread,
            limit=difference,
            ok=(None if spread is None or difference is None
                else float(spread) < float(difference)),
            key="height_difference_px",
        ))
        return metrics

    # Раковины / стекло / сварка: бинарные правила по числу детекций.
    found = role_details.get("found")
    add(_metric(
        "Найдено дефектов, шт", found,
        limit=0,
        ok=(None if found is None else int(found) == 0),
        key="found",
    ))
    return metrics


def build_rule_summary(rule_name: str, details: dict) -> list:
    per_role = details.get("per_role")
    if not isinstance(per_role, dict) or not per_role:
        return []

    cards = []
    for role, role_details in per_role.items():
        if not isinstance(role_details, dict):
            continue
        ok, verdict = _role_verdict(role_details)
        cards.append({
            "role": role,
            "ok": ok,
            "verdict": verdict,
            "found": _count_found(role_details),
            "metrics": _role_metrics(rule_name, role_details),
        })
    cards.sort(key=lambda card: (card["ok"] is not False, card["role"]))
    return cards


def build_presence_summary(details: dict) -> list:
    min_windows_by_role = details.get("min_windows_by_role") or {}
    windows_by_role = details.get("windows_by_role") or {}
    presence_by_role = details.get("presence_by_role") or {}

    cards = []
    for role in ("LEFT", "RIGHT"):
        if role not in windows_by_role and role not in min_windows_by_role:
            continue
        found = windows_by_role.get(role)
        limit = min_windows_by_role.get(role)
        present = presence_by_role.get(role)
        if present is None and isinstance(found, int) and isinstance(limit, int):
            present = found >= limit
        metrics = [
            metric for metric in (
                _metric(
                    "Найдено окон, шт", found, limit,
                    ok=present if present is not None else None,
                    key="part_presence_min_windows",
                ),
            ) if metric is not None
        ]
        cards.append({
            "role": role,
            "ok": present,
            "verdict": (
                "деталь видна" if present
                else ("деталь не видна" if present is False else _UNKNOWN)
            ),
            "found": [f"окна: {_number(found)}"],
            "metrics": metrics,
        })
    return cards
