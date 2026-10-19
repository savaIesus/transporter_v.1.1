"""Доменные типы трёхкамерной линии.

Импорты ленивые, чтобы ``from domain.part import Part`` не тянул OpenCV
через правила дефектов.
"""

__all__ = ["Part", "ThresholdLoader", "RuleResult"]


def __getattr__(name):
    if name == "Part":
        from domain.part import Part
        return Part
    if name == "ThresholdLoader":
        from domain.threshold_loader import ThresholdLoader
        return ThresholdLoader
    if name == "RuleResult":
        from domain.defect_rules.base import RuleResult
        return RuleResult
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
