"""Общие строители карточек замера для «Анализа кадра»."""

METRICS_PER_ROLE_LIMIT = 80

_UNKNOWN = "—"


def number(value, digits=1):
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return str(value)
    if parsed == int(parsed) and abs(parsed) < 1e9:
        return str(int(parsed))
    return f"{parsed:.{digits}f}"


def metric(label, value, limit=None, ok=None, unit="", key=None, object=None):
    value_text = number(value)
    if value_text is None:
        return None
    limit_text = number(limit)
    value_raw = None
    limit_raw = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value_raw = float(value)
    if isinstance(limit, (int, float)) and not isinstance(limit, bool):
        limit_raw = float(limit)
    payload = {
        "label": label,
        "value": f"{value_text}{unit}",
        "limit": f"{limit_text}{unit}" if limit_text is not None else None,
        "ok": None if ok is None else bool(ok),
        "value_raw": value_raw,
        "limit_raw": limit_raw,
        "key": key,
    }
    if object:
        payload["object"] = str(object)
    return payload


def within(value, limit):
    try:
        return float(value) <= float(limit)
    except (TypeError, ValueError):
        return None


def at_least(value, limit):
    try:
        return float(value) >= float(limit)
    except (TypeError, ValueError):
        return None


def finite_numbers(values) -> list:
    numbers = []
    for value in values or []:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed == parsed:
            numbers.append(parsed)
    return numbers


def count_found(role_details: dict) -> list:
    found = []
    pairs = (
        ("окна", "windows_found", "expected_count"),
        ("объекты", "found", "expected_count"),
        ("объекты (сырые)", "found_raw", "expected_count"),
        ("раковины", "sinks_total", None),
        ("стёкла", "glasses_total", None),
        ("контакты valid", "valid_contacts", None),
    )
    seen = set()
    for label, key, expected_key in pairs:
        if key not in role_details:
            continue
        value = role_details.get(key)
        if value is None:
            continue
        expected = role_details.get(expected_key) if expected_key else None
        text = f"{label}: {number(value)}"
        if expected is not None:
            text += f"/{number(expected)}"
        if text not in seen:
            seen.add(text)
            found.append(text)
    ignored = role_details.get("ignored") or role_details.get("ignored_windows")
    if ignored:
        found.append(f"отфильтровано: {number(ignored)}")
    confirmed = role_details.get("confirmed_sinks")
    if confirmed is not None:
        found.append(f"подтверждено раковин: {number(confirmed)}")
    return found


class Metrics(list):
    """Аккумулятор замеров: ``add`` отбрасывает пустые замеры (``None``)."""

    def add(self, item):
        if item is not None:
            self.append(item)
