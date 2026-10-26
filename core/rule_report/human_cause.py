"""Короткие человеческие причины дефектов для отображения в UI."""

ROLE_REASON_TEXT = {
    "height_above_max": "высота ячейки выше максимума",
    "height_below_min": "высота ячейки ниже минимума",
    "spread_exceeded": "разброс высот превышен",
    "sinks_found": "найдены раковины",
    "glass_found": "найдено стекло",
    "welding_defect_found": "найден дефект сварки",
}


def role_reason_text(reason) -> str:
    """Перевести машинный ``reason`` роли в короткую подпись вердикта."""
    if not reason:
        return ""
    text = str(reason)
    return ROLE_REASON_TEXT.get(text, text.replace("_", " "))


# === Упрощённые человеческие причины дефектов (для быстрого понимания) ===
HUMAN_CAUSE_MAP = {
    ("uneven_heights", True): "РАЗНОВЫСОТНОСТЬ ОКОН",
    ("window_sinks", True): "РАКОВИНА В ОКНЕ",
    ("bottom_glass", True): "СТЕКЛО НА ДНЕ",
    ("welding", True): "БРАК СВАРКИ",
}


def get_human_cause(rule_name: str, triggered: bool, details: dict) -> str | None:
    """Возвращает короткую читаемую причину дефекта."""
    if not triggered:
        return None

    key = (rule_name, True)
    if key in HUMAN_CAUSE_MAP:
        return HUMAN_CAUSE_MAP[key]

    # Fallback: вытаскиваем первую причину сработавшей роли.
    per_role = details.get("per_role") or {}
    reasons = []
    for role, rd in per_role.items():
        if isinstance(rd, dict) and rd.get("triggered"):
            reason = rd.get("reason")
            if reason:
                reasons.append(str(reason))

    if reasons:
        return reasons[0].upper().replace("_", " ")[:60]
    return None
