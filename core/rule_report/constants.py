"""Константы отчёта по правилам трёхкамерной линии.

Лейблы метрик для UI, списки правил с детальной телеметрией, привязка
правил к ролям камер LEFT / MIDDLE / RIGHT.
"""

# Названия порогов для анализа кадра: (правило, ключ метрики) -> понятный
# в UI label (как в панели «Пороги правил»). UI показывает порог
# рядом с названием правила и замер под ним.
METRIC_PARAM_LABELS = {
    # uneven_heights
    ("uneven_heights", "height_px"): "Высота ячейки, px",
    ("uneven_heights", "height_max_px"): "Высота ячейки: макс., px",
    ("uneven_heights", "height_min_px"): "Высота ячейки: мин., px",
    ("uneven_heights", "height_difference_px"): "Макс. разброс высот ячеек, px",
    # бинарные правила по числу детекций
    ("window_sinks", "found"): "Найдено раковин, шт",
    ("bottom_glass", "found"): "Найдено стекла, шт",
    ("welding", "found"): "Найдено дефектов сварки, шт",
    # part_presence
    ("part_presence", "part_presence_min_windows"): "Мин. число найденных окон, шт",
}

# Правила, у которых есть развёрнутая построчная телеметрия в правой панели.
DETAILED_RULES = (
    "uneven_heights",
    "window_sinks",
    "bottom_glass",
    "welding",
)

NO_MEASUREMENT = "нет измерения"

SUMMARY_LINES_LIMIT = 4

PART_PRESENCE_RULE = "part_presence"

# Камеры, для которых правило имеет смысл в анализе кадра.
# Панель «Анализ кадра» показывает только вычисления выбранной камеры.
RULE_CAMERA_ROLES = {
    "part_presence": ("LEFT", "RIGHT"),
    "uneven_heights": ("LEFT", "RIGHT"),
    "window_sinks": ("LEFT", "RIGHT"),
    "bottom_glass": ("MIDDLE",),
    "welding": ("MIDDLE",),
}

# Названия правил -> человекочитаемые заголовки.
RULE_LABELS = {
    "part_presence": "НАЛИЧИЕ ДЕТАЛИ",
    "uneven_heights": "РАЗНОВЫСОТНОСТЬ ОКОН",
    "window_sinks": "РАКОВИНЫ ОКОН",
    "bottom_glass": "СТЕКЛО НА ДНЕ",
    "welding": "БРАК СВАРКИ",
}
