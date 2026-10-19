import json
import math


DEFAULTS = {
    "conveyor_speed":         20000,
    "conveyor_accel":         6000,
    "dist1_open_position":    340,
    "dist2_bad_position":     0,
    "dist2_cleanup_position": 340,
    "axis_speed":             300,
    "axis_accel":             100,
    "jog_hold_steps":         1_000_000,
    "normal_steps":           19048,
}

# Необязательные тайминги получают значения по умолчанию.
OPTIONAL_DEFAULTS = {
    # Пауза между подтверждённой остановкой ленты и первым кадром
    # инспекции: контроллер подтверждает остановку по счётчику шагов,
    # а механика в этот момент ещё качается.
    "settle_time": 0.5,
    # Наблюдательная пауза перед каждой фазой шага. 0 в production;
    # ненулевое значение растягивает шаг для отладки и на физику линии
    # не влияет.
    "stage_trace_time": 0.5,
    # Пауза после обработки кадров нейросетями: просмотр
    # результата анализа до начала следующего шага.
    "review_time": 2.0,
}

_FLOAT_KEYS = ("settle_time", "stage_trace_time", "review_time")
_INTEGER_KEYS = tuple(key for key in DEFAULTS if key not in _FLOAT_KEYS)


def _validate(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("calibration.json должен содержать объект")
    missing = set(DEFAULTS) - set(data)
    extra = set(data) - set(DEFAULTS) - set(OPTIONAL_DEFAULTS)
    if missing or extra:
        raise ValueError(
            f"Неверные поля calibration: missing={sorted(missing)}, "
            f"extra={sorted(extra)}"
        )
    data = {**OPTIONAL_DEFAULTS, **data}
    for key in _INTEGER_KEYS:
        if type(data[key]) is not int:
            raise ValueError(f"{key} должен быть int")
    for key in _FLOAT_KEYS:
        if type(data[key]) not in (int, float):
            raise ValueError(f"{key} должен быть числом")
        if not math.isfinite(float(data[key])):
            raise ValueError(f"{key} должен быть конечным")

    positive = (
        "conveyor_speed",
        "conveyor_accel",
        "dist1_open_position",
        "axis_speed",
        "axis_accel",
        "jog_hold_steps",
        "normal_steps",
    )
    if any(data[key] <= 0 for key in positive):
        raise ValueError("Положительные calibration-параметры должны быть > 0")
    if not 10_000 <= data["jog_hold_steps"] <= 10_000_000:
        raise ValueError("jog_hold_steps должен быть в диапазоне 10000..10000000")
    if data["dist2_bad_position"] < 0 or data["dist2_cleanup_position"] < 0:
        raise ValueError("Позиции DIST2 не могут быть отрицательными")
    if data["dist2_bad_position"] == data["dist2_cleanup_position"]:
        raise ValueError("BAD и CLEANUP позиции должны различаться")
    if not 0.0 <= float(data["settle_time"]) <= 5.0:
        raise ValueError("settle_time должен быть в диапазоне 0..5 секунд")
    if not 0.0 <= float(data["stage_trace_time"]) <= 5.0:
        raise ValueError(
            "stage_trace_time должен быть в диапазоне 0..5 секунд"
        )
    if not 0.0 <= float(data["review_time"]) <= 30.0:
        raise ValueError("review_time должен быть в диапазоне 0..30 секунд")
    return dict(data)


def load_calibration(path: str = "calibration.json") -> dict:
    """Загрузить полную проверенную калибровку; unsafe defaults запрещены."""
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Файл калибровки не найден: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Ошибка чтения {path}: {exc}") from exc
    result = _validate(data)
    print(f"[CALIB] Loaded and validated from {path}")
    return result
