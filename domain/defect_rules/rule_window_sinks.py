from domain.defect_rules.base import BaseRule, RuleResult, detections_of_kind


class WindowSinksRule(BaseRule):
    """Раковины окон по камерам LEFT и RIGHT.

    Перенос трёхкамерника (kind=window_sinks, модель shells): любая
    детекция с уверенностью выше порога — брак (код состояния 3,
    наивысший приоритет 100).
    """

    name = "window_sinks"
    ROLES = ("LEFT", "RIGHT")
    TARGET_KIND = "window_sinks"

    def check(self, vision_results: dict, **kwargs) -> RuleResult:
        if not self.enabled:
            return self._make_skip(self.name)

        roles_to_check = [r for r in self.ROLES if r in vision_results]
        if not roles_to_check:
            roles_to_check = list(self.ROLES)

        per_role = {}
        drawings = []
        any_triggered = False

        for role in roles_to_check:
            min_confidence = float(self._get(
                "window_sinks_min_confidence", 0.8, role=role,
            ))
            detections = detections_of_kind(
                vision_results, role, self.TARGET_KIND, min_confidence,
            )
            triggered = len(detections) > 0

            per_role[role] = {
                "valid": True,
                "skipped": False,
                "triggered": triggered,
                "reason": "sinks_found" if triggered else None,
                "found": len(detections),
                "min_confidence": min_confidence,
            }

            for det in detections:
                drawings.append({
                    "type": "rule_bbox",
                    "role": role,
                    "rule": self.name,
                    "bbox": det.get("bbox"),
                    "mask": det.get("mask"),
                    "triggered": True,
                    "confidence": det.get("confidence"),
                })

            any_triggered = any_triggered or triggered

        return RuleResult(
            self.name,
            triggered=any_triggered,
            details={"per_role": per_role},
            drawings=drawings,
        )
