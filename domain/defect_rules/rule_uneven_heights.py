from domain.defect_rules.base import BaseRule, RuleResult, detections_of_kind
from domain.defect_rules.window_measure import measure_window_by_intersections


class UnevenHeightsRule(BaseRule):
    """Разновысотность окон по камерам LEFT и RIGHT.

    Перенос логики трёхкамерника: для каждой найденной ячейки окна
    вертикальная секущая через середину по X даёт высоту в пикселях.
    Брак (по каждой стороне), если:

      * ``h_max >= height_max_px`` — ячейка слишком высокая;
      * ``h_min <= height_min_px`` — ячейка слишком низкая;
      * ``h_max - h_min >= height_difference_px`` — перепад высот.

    Триггеры оригинала: ``left_cam_*`` / ``right_cam_*`` из transporter.
    """

    name = "uneven_heights"
    ROLES = ("LEFT", "RIGHT")
    TARGET_KIND = "uneven_heights"

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
                "uneven_heights_min_confidence", 0.7, role=role,
            ))
            height_min = float(self._get(
                "uneven_heights_height_min_px", 20.0, role=role,
            ))
            height_max = float(self._get(
                "uneven_heights_height_max_px", 42.0, role=role,
            ))
            difference = float(self._get(
                "uneven_heights_height_difference_px", 11.0, role=role,
            ))
            min_gap = float(self._get(
                "uneven_heights_min_intersection_gap_px", 7.0, role=role,
            ))

            detections = detections_of_kind(
                vision_results, role, self.TARGET_KIND, min_confidence,
            )

            measures = []
            for det in detections:
                mask = det.get("mask")
                measure = None
                if mask and len(mask) >= 3:
                    measure = measure_window_by_intersections(
                        mask, min_gap_px=min_gap,
                    )
                measures.append({
                    "detection": det,
                    "measure": measure,
                })

            heights = [
                m["measure"]["height"]
                for m in measures
                if m["measure"] is not None
            ]

            h_max = max(heights) if heights else None
            h_min = min(heights) if heights else None
            spread = (h_max - h_min) if heights else None

            triggered = False
            reason = None
            if heights:
                if h_max >= height_max:
                    triggered, reason = True, "height_above_max"
                elif h_min <= height_min:
                    triggered, reason = True, "height_below_min"
                elif spread >= difference:
                    triggered, reason = True, "spread_exceeded"

            per_role[role] = {
                "valid": True,
                "skipped": False,
                "triggered": triggered,
                "reason": reason,
                "found": len(detections),
                "measured": len(heights),
                "heights": [round(h, 1) for h in heights],
                "h_max": round(h_max, 1) if h_max is not None else None,
                "h_min": round(h_min, 1) if h_min is not None else None,
                "spread": round(spread, 1) if spread is not None else None,
                "height_min_px": height_min,
                "height_max_px": height_max,
                "height_difference_px": difference,
                "min_confidence": min_confidence,
            }

            for item in measures:
                det = item["detection"]
                measure = item["measure"]
                drawings.append({
                    "type": "rule_bbox",
                    "role": role,
                    "rule": self.name,
                    "bbox": det.get("bbox"),
                    "mask": det.get("mask"),
                    "triggered": triggered,
                    "confidence": det.get("confidence"),
                })
                if measure is not None:
                    drawings.append({
                        "type": "uneven_height_measure",
                        "role": role,
                        "rule": self.name,
                        "x": measure["x"],
                        "y_top": measure["y_top"],
                        "y_bottom": measure["y_bottom"],
                        "height": round(measure["height"], 1),
                        "triggered": triggered,
                    })

            any_triggered = any_triggered or triggered

        return RuleResult(
            self.name,
            triggered=any_triggered,
            details={"per_role": per_role},
            drawings=drawings,
        )
