CATEGORY_GOOD    = "GOOD"
CATEGORY_BAD     = "BAD"
CATEGORY_CLEANUP = "CLEANUP"
CATEGORY_UNKNOWN = "UNKNOWN"

# Дефекты, которые ведут на CLEANUP, если других (BAD-) дефектов нет.
# Соответствует распределению транспортера: стекло и брак сварки уходят
# «на чистку» (обе оси 340), разновысотность и раковины — в «брак».
CLEANUP_DEFECTS = {
    "bottom_glass",
    "welding",
}

# Приоритет выбора решающего дефекта (как merge_states трёхкамерника):
# раковины(100) > разновысотность(99) > стекло(80) > сварка(70).
DEFECT_PRIORITY = {
    "window_sinks":   100,
    "uneven_heights": 99,
    "bottom_glass":   80,
    "welding":        70,
}


class Part:
    """Корпус трёхкамерной линии: одна инспекционная стадия на +0."""

    def __init__(self, part_id: int, step_created: int):
        self.id = part_id
        self.step_created = step_created

        # Корпус попадает в учёт до анализа (он физически стоит на +0),
        # но появляется в «Пути корпусов» только после подтверждения
        # наличия правилом part_presence: пустая ячейка не рисует
        # «призрак» в инспекционной зоне.
        self.present: bool = False

        self.defects: list = []
        self.inspected: bool = False

        self.final_decision: str = "none"
        self.route_category: str = CATEGORY_UNKNOWN

    # Совместимость с интерфейсом семикамерного ProductionCycle/архива.
    @property
    def input_defects(self) -> list:
        """Совместимость со стадийным интерфейсом: единая стадия inspect."""
        return self.defects

    def add_input_defect(self, defect: str):
        if defect:
            self.defects.append(defect)

    def mark_input_done(self):
        """Зафиксировать завершение (единственной) инспекции."""
        self.inspected = True
        self._recompute()

    def get_all_defects(self) -> list:
        return list(self.defects)

    @property
    def fully_inspected(self) -> bool:
        return self.inspected

    def _recompute(self):
        """Пересчитать категорию и финальный дефект после инспекции."""
        defects = self.get_all_defects()

        if not defects:
            self.final_decision = "none"
            self.route_category = (
                CATEGORY_GOOD if self.fully_inspected else CATEGORY_UNKNOWN
            )
            return

        # Решающий дефект — с наивысшим приоритетом (логика трёхкамерника).
        self.final_decision = max(
            defects, key=lambda d: DEFECT_PRIORITY.get(d, 0),
        )

        only_cleanup = all(d in CLEANUP_DEFECTS for d in defects)
        self.route_category = (
            CATEGORY_CLEANUP if only_cleanup else CATEGORY_BAD
        )

    def __repr__(self):
        return (
            f"<Part #{self.id} "
            f"step={self.step_created} "
            f"category={self.route_category} "
            f"inspected={'full' if self.fully_inspected else 'partial'} "
            f"defects={self.get_all_defects()}>"
        )
