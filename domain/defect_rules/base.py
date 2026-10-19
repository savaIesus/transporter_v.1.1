from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class RuleResult:
    rule_name: str
    triggered: bool
    details:  dict = field(default_factory=dict)
    drawings: list = field(default_factory=list)

    @property
    def defect(self) -> str | None:
        return self.rule_name if self.triggered else None

    def __repr__(self):
        status = "FAIL" if self.triggered else "OK"
        return f"<{self.rule_name}: {status} {self.details}>"


class BaseRule:
    """
    Базовый класс правила.
    Потомок реализует метод check().
    """

    name: str = ""

    def __init__(self, thresholds: dict):
        self.thresholds = thresholds
        self._enabled = self.name not in thresholds.get(
            "disabled_rules", []
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def check(self, vision_results: dict, **kwargs) -> RuleResult:
        raise NotImplementedError

    def _get(self, key: str, default=None, role: str | None = None):
        """
        Per-role lookup порога с fallback на общий.

        Порядок поиска:
          1. "{ROLE}.{key}" — если role задана и ключ существует
          2. "{key}" — общий порог
          3. default — если ничего не найдено
        """
        if role:
            role_key = f"{role}.{key}"
            if role_key in self.thresholds:
                return self.thresholds[role_key]
        return self.thresholds.get(key, default)

    @staticmethod
    def _make_skip(rule_name: str) -> RuleResult:
        """Результат для отключённого правила."""
        return RuleResult(
            rule_name, False,
            details={"skipped": "rule disabled"},
        )


def detections_of_kind(vision_results: dict, role: str, kind: str,
                       min_confidence: float = 0.0) -> list:
    """Детекции нужного типа модели на роли с фильтром по уверенности.

    ``kind`` проставляется VisionCluster'ом из model_config и повторяет
    семантику трёхкамерника: модель привязана к камере, а не к имени
    класса внутри весов.
    """
    out = []
    for det in vision_results.get(role, []) or []:
        if not isinstance(det, dict):
            continue
        if kind and det.get("kind") != kind:
            continue
        if float(det.get("confidence", 0.0)) < float(min_confidence):
            continue
        out.append(det)
    return out
