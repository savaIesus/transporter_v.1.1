from domain.defect_rules.base import BaseRule, RuleResult, detections_of_kind
from domain.geometry.zones import build_zones, split_by_zones


class BottomGlassRule(BaseRule):
    """Стекло на дне изделия по камере MIDDLE.

    Перенос трёхкамерника (kind=bottom_glass, код состояния 4): любая
    детекция стекла выше порога уверенности отправляет корпус на
    очистку (CLEANUP), если нет BAD-дефектов выше по приоритету.

    Поиск ограничен кадром **без** зоны, которую обводит модель
    ``side_glass.pt``: эта модель размечает область ожидаемого стекла
    сбоку, и ``bottom_glass`` на ней срабатывает ложно. Зоны строятся по
    детекциям ``side_glass`` того же кадра и той же камеры (маска, при её
    отсутствии — bbox); детекция стекла на дне, площадь которой не менее
    ``bottom_glass_exclusion_overlap`` лежит в такой зоне, дефектом не
    считается и попадает в ``details`` как исключённая.

    Порог уверенности самой зоны — ``side_glass_zone_min_confidence``:
    он ниже порога правила ``side_glass``, чтобы зона находилась даже
    тогда, когда уверенность модели недостаточна для брака.
    """

    name = "bottom_glass"
    ROLES = ("MIDDLE",)
    TARGET_KIND = "bottom_glass"

    # Модель, чья зона исключается из поиска стекла на дне.
    ZONE_KIND = "side_glass"
    ZONE_MIN_CONFIDENCE_KEY = "side_glass_zone_min_confidence"
    ZONE_OVERLAP_KEY = "bottom_glass_exclusion_overlap"
    DEFAULT_ZONE_MIN_CONFIDENCE = 0.25
    DEFAULT_ZONE_OVERLAP = 0.5

    def _zone_thresholds(self, role: str) -> tuple[float, float]:
        """Порог уверенности зоны и минимальная доля перекрытия."""
        zone_min_confidence = float(self._get(
            self.ZONE_MIN_CONFIDENCE_KEY,
            self.DEFAULT_ZONE_MIN_CONFIDENCE,
            role=role,
        ))
        overlap = float(self._get(
            self.ZONE_OVERLAP_KEY,
            self.DEFAULT_ZONE_OVERLAP,
            role=role,
        ))
        return zone_min_confidence, overlap

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
                "bottom_glass_min_confidence", 0.65, role=role,
            ))
            zone_min_confidence, overlap = self._zone_thresholds(role)

            zones = build_zones(detections_of_kind(
                vision_results, role, self.ZONE_KIND, zone_min_confidence,
            ))
            found_all = detections_of_kind(
                vision_results, role, self.TARGET_KIND, min_confidence,
            )
            detections, excluded = split_by_zones(
                found_all, zones, min_coverage=overlap,
            )
            triggered = len(detections) > 0

            per_role[role] = {
                "valid": True,
                "skipped": False,
                "triggered": triggered,
                "reason": "glass_found" if triggered else None,
                "found": len(detections),
                "min_confidence": min_confidence,
                # Диагностика зоны исключения: сколько детекций модель
                # нашла всего и сколько из них убрано зоной side_glass.
                "found_before_exclusion": len(found_all),
                "excluded": len(excluded),
                "exclusion_zone_kind": self.ZONE_KIND,
                "exclusion_zone_count": len(zones),
                "exclusion_zone_min_confidence": zone_min_confidence,
                "exclusion_overlap": overlap,
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
                    "color_hint": "glass",
                })

            any_triggered = any_triggered or triggered

        return RuleResult(
            self.name,
            triggered=any_triggered,
            details={"per_role": per_role},
            drawings=drawings,
        )
