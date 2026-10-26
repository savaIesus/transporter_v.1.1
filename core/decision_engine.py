from domain.threshold_loader import ThresholdLoader
from domain.defect_rules import (
    BaseRule,
    UnevenHeightsRule,
    WindowSinksRule,
    BottomGlassRule,
    WeldingRule,
)


class DecisionEngine:
    """
    Оркестратор правил трёхкамерной линии.
    """

    def __init__(self, thresholds: dict | None = None):
        if thresholds is None:
            thresholds = ThresholdLoader().get_all()

        # Сохраняем thresholds для доступа снаружи
        # (нужно например Inspector'у для создания служебных правил
        # вроде PartPresenceRule без пересоздания загрузчика).
        self.thresholds = thresholds

        all_rules: list[BaseRule] = [
            UnevenHeightsRule(thresholds),
            WindowSinksRule(thresholds),
            BottomGlassRule(thresholds),
            WeldingRule(thresholds),
        ]

        self.rules: list[BaseRule] = [
            r for r in all_rules if r.enabled
        ]

        disabled = [r.name for r in all_rules if not r.enabled]
        if disabled:
            print(f"[RULES] Disabled: {disabled}")
        if not self.rules:
            raise RuntimeError("No active defect rules; production inspection blocked")
        print(f"[RULES] Active: {[r.name for r in self.rules]}")

    def evaluate_all_detailed(self, vision_results, frames=None):
        return self.evaluate_rules_detailed(
            self.rules,
            vision_results,
            frames=frames,
        )

    def rules_for_roles(self, roles):
        """Активные правила, у которых есть хотя бы одна из ролей стадии."""
        wanted = set(roles or ())
        if not wanted:
            return []
        return [
            rule
            for rule in self.rules
            if wanted.intersection(tuple(getattr(rule, "ROLES", ())))
        ]

    def rules_for_role(self, role: str):
        """Активные правила, в которых участвует выбранная камера."""
        return self.rules_for_roles((role,))

    def evaluate_rules_detailed(self, rules, vision_results, frames=None):
        """Выполнить правила по детекциям одного frozen-кадра.

        Каждое ``rule.check`` сначала строит свои опорные области и измерения
        (геометрию), а затем возвращает тот же ``RuleResult`` с решением
        ``triggered`` и drawings для последующей разметки.
        """
        if not vision_results:
            return []
        return [
            self._run_rule(rule, vision_results, frames)
            for rule in rules
        ]

    @staticmethod
    def _run_rule(rule, vision_results, frames=None):
        kwargs = {}
        if frames:
            kwargs["frames"] = frames
        return rule.check(vision_results, **kwargs)
