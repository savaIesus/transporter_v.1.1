from domain.defect_rules.base import BaseRule, RuleResult, detections_of_kind


class PartPresenceRule(BaseRule):
    """Детектор наличия детали по окнам на камерах LEFT и RIGHT.

    Маркер присутствия — детекции ``uneven_heights`` (ячейки окон), как в
    трёхкамернике: деталь «есть», если хотя бы одна сторона видит не
    меньше ``part_presence_min_windows`` окон с достаточной уверенностью.

    Это служебное правило: ``triggered`` всегда False, а Inspector
    использует ``details["empty_tray"]`` для решения, создавать Part
    или считать ячейку пустой.
    """

    name = "part_presence"
    ROLES = ("LEFT", "RIGHT")
    TARGET_KIND = "uneven_heights"

    def check(self, vision_results: dict, **kwargs) -> RuleResult:
        if not self.enabled:
            return self._make_skip(self.name)

        # Если в vision_results только одна камера (ручной анализ кадра),
        # проверяем только её.
        roles_to_check = [r for r in self.ROLES if r in vision_results]
        if not roles_to_check:
            roles_to_check = list(self.ROLES)

        min_confidence_by_role = {}
        min_windows_by_role = {}
        windows_by_role = {}
        presence_by_role = {}

        for role in roles_to_check:
            min_confidence = self._get(
                "part_presence_min_confidence", 0.6, role=role,
            )
            min_windows = self._get(
                "part_presence_min_windows", 1, role=role,
            )
            if type(min_confidence) not in (int, float) or not 0.0 <= float(min_confidence) <= 1.0:
                raise ValueError(
                    f"{role}.part_presence_min_confidence должен быть числом 0..1"
                )
            if type(min_windows) is not int or min_windows < 1:
                raise ValueError(
                    f"{role}.part_presence_min_windows должен быть целым >= 1"
                )

            detections = detections_of_kind(
                vision_results, role, self.TARGET_KIND, min_confidence,
            )
            count = len(detections)

            min_confidence_by_role[role] = float(min_confidence)
            min_windows_by_role[role] = int(min_windows)
            windows_by_role[role] = count
            presence_by_role[role] = count >= min_windows

        # Деталь присутствует, если её видит хотя бы одна сторона
        # (как detected_flatness в трёхкамернике).
        is_empty = not any(presence_by_role.values())

        details = {
            "min_confidence_by_role": min_confidence_by_role,
            "min_windows_by_role": min_windows_by_role,
            "windows_left": windows_by_role.get("LEFT", 0),
            "windows_right": windows_by_role.get("RIGHT", 0),
            "windows_by_role": windows_by_role,
            "presence_by_role": presence_by_role,
            "empty_tray": is_empty,
        }

        return RuleResult(
            self.name,
            triggered=False,
            details=details,
            drawings=[],
        )
