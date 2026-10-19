from domain.defect_rules.base import BaseRule, RuleResult, detections_of_kind


class WeldingRule(BaseRule):
    """Брак сварки по камере MIDDLE.

    Перенос трёхкамерника (kind=welding, код состояния 8): любая
    детекция дефекта сварки выше порога уверенности. Как и в оригинале,
    ведёт корпус на очистку (CLEANUP), если нет BAD-дефектов выше по
    приоритету.
    """

    name = "welding"
    ROLES = ("MIDDLE",)
    TARGET_KIND = "welding"

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
                "welding_min_confidence", 0.65, role=role,
            ))
            detections = detections_of_kind(
                vision_results, role, self.TARGET_KIND, min_confidence,
            )
            triggered = len(detections) > 0

            per_role[role] = {
                "valid": True,
                "skipped": False,
                "triggered": triggered,
                "reason": "welding_defect_found" if triggered else None,
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
