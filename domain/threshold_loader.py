import json
import math
import os

ROLE_SECTIONS = ("LEFT", "MIDDLE", "RIGHT")

# (rule_id, UI label, parameter prefixes). More specific prefixes first.
RULE_GROUPS = (
    ("part_presence", "НАЛИЧИЕ ДЕТАЛИ", ("part_presence_",)),
    ("uneven_heights", "РАЗНОВЫСОТНОСТЬ ОКОН", ("uneven_heights_",)),
    ("window_sinks", "РАКОВИНЫ ОКОН", ("window_sinks_",)),
    ("bottom_glass", "СТЕКЛО НА ДНЕ", ("bottom_glass_",)),
    ("welding", "БРАК СВАРКИ", ("welding_",)),
)
_RULE_GROUPS_SORTED = tuple(sorted(RULE_GROUPS, key=lambda g: -max(len(p) for p in g[2])))
_RULE_GROUP_INDEX = {rule_id: i for i, (rule_id, _, _) in enumerate(RULE_GROUPS)}

PARAM_LABELS = {
    "part_presence_min_confidence": "Мин. уверенность окон для наличия детали",
    "part_presence_min_windows": "Мин. число найденных окон, шт.",
    "uneven_heights_min_confidence": "Мин. уверенность обнаружения окон",
    "uneven_heights_height_min_px": "Высота ячейки: мин., px",
    "uneven_heights_height_max_px": "Высота ячейки: макс., px",
    "uneven_heights_height_difference_px": "Макс. разброс высот ячеек, px",
    "uneven_heights_min_intersection_gap_px": "Мин. зазор между пересечениями, px",
    "window_sinks_min_confidence": "Мин. уверенность раковин",
    "bottom_glass_min_confidence": "Мин. уверенность стекла",
    "welding_min_confidence": "Мин. уверенность брака сварки",
}
DISPLAY_ORDER = tuple(PARAM_LABELS)
_DISPLAY_INDEX = {k: i for i, k in enumerate(DISPLAY_ORDER)}

# Какие параметры допустимы у каждой роли.
_ROLE_PARAM_RULES = {
    "LEFT": ("part_presence_", "uneven_heights_", "window_sinks_"),
    "RIGHT": ("part_presence_", "uneven_heights_", "window_sinks_"),
    "MIDDLE": ("bottom_glass_", "welding_"),
}


def _finite_number(value):
    return type(value) in (int, float) and not isinstance(value, bool) and math.isfinite(float(value))


class ThresholdLoader:
    REQUIRED_KEYS = tuple(
        f"{role}.{name}"
        for role in ROLE_SECTIONS
        for name in PARAM_LABELS
        if name.startswith(_ROLE_PARAM_RULES[role])
    )

    def __init__(self, path="thresholds.json"):
        self.path = path
        self.labels = {}
        self.thresholds = self._load()

    def _load(self):
        if not os.path.exists(self.path):
            raise RuntimeError(f"Файл не найден: {self.path}")
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Ошибка чтения {self.path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError("thresholds.json должен содержать объект")
        data, labels = self._flatten_sections(raw)
        self.labels = labels
        self.validate(data, labels)
        return data

    @classmethod
    def validate(cls, data, labels=None):
        if not isinstance(data, dict):
            raise ValueError("Пороги должны быть объектом")
        for key in cls.REQUIRED_KEYS:
            if key not in data:
                raise ValueError(f"Отсутствует ключ в thresholds.json: {key}")

        for key, value in data.items():
            if key == "disabled_rules":
                continue
            if not _finite_number(value):
                raise ValueError(f"{key} должен быть конечным числом")
            name = key.split(".", 1)[1] if "." in key else key
            v = float(value)
            if name.endswith("_min_confidence") and not 0.0 <= v <= 1.0:
                raise ValueError(f"{key} должен быть 0..1")
            if name == "part_presence_min_windows":
                if type(value) is not int or value < 1:
                    raise ValueError(f"{key} должен быть целым >= 1")
            if name.endswith("_px") and v <= 0:
                raise ValueError(f"{key} должен быть > 0")

        # Диапазоны высот: min < max для LEFT и RIGHT.
        for role in ("LEFT", "RIGHT"):
            mn = data[f"{role}.uneven_heights_height_min_px"]
            mx = data[f"{role}.uneven_heights_height_max_px"]
            if mn >= mx:
                raise ValueError(
                    f"{role}: uneven_heights_height_min_px должен быть меньше "
                    "uneven_heights_height_max_px"
                )

        disabled = data.get("disabled_rules", [])
        if not isinstance(disabled, list) or any(not isinstance(x, str) for x in disabled):
            raise ValueError("disabled_rules должен быть списком строк")
        if "part_presence" in disabled:
            raise ValueError("part_presence нельзя отключать")
        if labels is not None:
            if not isinstance(labels, dict) or any(
                not isinstance(k, str) or not str(v).strip() for k, v in labels.items()
            ):
                raise ValueError("Названия порогов должны быть непустыми строками")

    @staticmethod
    def _flatten_sections(raw):
        flat, labels = {}, {}
        for key, value in raw.items():
            if str(key).startswith("_comment"):
                continue
            if key not in ROLE_SECTIONS:
                flat[key] = value
                continue
            if not isinstance(value, dict):
                raise ValueError(f"Секция {key} должна быть объектом")
            for pkey, pval in value.items():
                if str(pkey).startswith("_comment"):
                    continue
                if str(pkey).startswith("_label."):
                    if isinstance(pval, str) and pval.strip():
                        labels[f"{key}.{str(pkey)[7:]}"] = pval.strip()
                    continue
                if isinstance(pval, (dict, list)):
                    raise ValueError(f"{key}.{pkey} должен быть простым значением")
                flat[f"{key}.{pkey}"] = pval
        return flat, labels

    @staticmethod
    def save_file(path, data, labels=None):
        grouped = {}
        for key, value in data.items():
            if key == "disabled_rules":
                continue
            role, dot, param = key.partition(".")
            if dot and role in ROLE_SECTIONS:
                grouped.setdefault(role, {})[param] = value
            else:
                grouped[key] = value
        lines = ["{"]
        ordered = [r for r in ROLE_SECTIONS if r in grouped] + [k for k in grouped if k not in ROLE_SECTIONS]
        last = len(ordered) - 1
        has_disabled = "disabled_rules" in data
        for i, role in enumerate(ordered):
            if i:
                lines.append("")
            params = grouped[role]
            comma = "," if i < last or has_disabled else ""
            if isinstance(params, dict):
                lines.append(f'    "{role}": {{')
                role_labels = sorted(k[len(role) + 1:] for k in (labels or {}) if k.startswith(role + "."))
                entries = [(False, p) for p in params] + [(True, p) for p in role_labels]
                for j, (is_label, p) in enumerate(entries):
                    c = "," if j < len(entries) - 1 else ""
                    if is_label:
                        lines.append(f'        "_label.{p}": {json.dumps((labels or {})[role + "." + p], ensure_ascii=False)}{c}')
                    else:
                        lines.append(f'        "{p}": {json.dumps(params[p], ensure_ascii=False)}{c}')
                lines.append("    }" + comma)
            else:
                lines.append(f'    {json.dumps(role, ensure_ascii=False)}: {json.dumps(params, ensure_ascii=False)}{comma}')
        if has_disabled:
            lines.append("")
            lines.append(f'    "disabled_rules": {json.dumps(data["disabled_rules"], ensure_ascii=False)}')
        lines.append("}")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def get_all(self):
        return self.thresholds


def _param_meta(key, value):
    label = PARAM_LABELS.get(key, key)
    meta = {"key": key, "label": label, "description": "", "value": value}
    if key.endswith("_min_confidence"):
        meta.update({"step": 0.01, "min": 0, "max": 1})
    elif key == "part_presence_min_windows":
        meta.update({"step": 1, "min": 1})
    elif key.endswith("_px"):
        meta.update({"step": 0.5, "min": 0})
    else:
        meta.update({"step": 0.1, "min": 0})
    return meta


def describe_role_parameters(role, thresholds):
    prefix = role + "."
    params = [(k[len(prefix):], v) for k, v in thresholds.items() if k.startswith(prefix)]
    groups, matched = [], set()
    for rule_id, label, prefixes in _RULE_GROUPS_SORTED:
        gp = [(n, v) for n, v in params if n not in matched and any(n.startswith(p) for p in prefixes)]
        if gp:
            gp.sort(key=lambda x: (_DISPLAY_INDEX.get(x[0], len(DISPLAY_ORDER)), x[0]))
            groups.append({"rule": rule_id, "label": label, "params": [_param_meta(n, v) for n, v in gp]})
            matched.update(n for n, _ in gp)
    groups.sort(key=lambda g: _RULE_GROUP_INDEX.get(g["rule"], len(_RULE_GROUP_INDEX)))
    leftovers = [(n, v) for n, v in params if n not in matched]
    if leftovers:
        leftovers.sort()
        groups.append({"rule": "other", "label": "ПРОЧИЕ ПОРОГИ", "params": [_param_meta(n, v) for n, v in leftovers]})
    return groups
