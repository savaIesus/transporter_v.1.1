import time

from domain.part import CATEGORY_BAD, CATEGORY_CLEANUP, CATEGORY_GOOD


class Distributor:
    """Двухступенчатый распределитель корпусов.

    DIST1 (axis 0) — первая заслонка. В ``0`` корпус падает в GOOD; в
    ``dist1_open_position`` корпус передаётся на вторую заслонку DIST2.
    DIST1 никого не удерживает и не является лепестком.

    DIST2 (axis 1) выбирает канал только для корпуса, переданного DIST1:
    ``dist2_bad_position`` — BAD, ``dist2_cleanup_position`` — CLEANUP.

    Маршрут всегда готовится *до* команды движения ленты. Следующая смена
    заслонок возможна только на ``ROUTE_PREPARE`` следующего шага — после
    того, как выбрасывающий шаг полностью завершился. Отдельной паузы на
    падение корпуса нет. Заслонки, которым нужно сменить позицию, едут
    **одновременно**: команды ``G27`` на обе оси уходят подряд, остановка
    ожидается после обеих. Лента в этот момент стоит, а корпус, для
    которого готовится маршрут, находится на +3 и заслонок ещё не достиг,
    поэтому ограничение «не двигать DIST2 при открытом пути на неё» снято
    (см. README, «Логика распределителя»). Ручная диагностика
    (``diagnostic_gate`` / ``diagnostic_route``) управляет заслонками
    независимо и по-прежнему по одной: кнопки верхнего распределителя
    двигают только DIST1, кнопки нижнего — только DIST2.
    """

    def __init__(self, dist1_axis, dist2_axis, dist1_open_position: int,
                 dist2_bad_position: int, dist2_cleanup_position: int):
        if type(dist1_open_position) is not int or dist1_open_position <= 0:
            raise ValueError("dist1_open_position должен быть положительным int")
        if type(dist2_bad_position) is not int or dist2_bad_position < 0:
            raise ValueError("dist2_bad_position должен быть неотрицательным int")
        if type(dist2_cleanup_position) is not int or dist2_cleanup_position <= 0:
            raise ValueError("dist2_cleanup_position должен быть положительным int")
        if dist2_bad_position == dist2_cleanup_position:
            raise ValueError("DIST2 BAD и CLEANUP должны различаться")
        self.dist1, self.dist2 = dist1_axis, dist2_axis
        # Имя поля сохранено для совместимости calibration.json.
        self.dist1_open_position = dist1_open_position
        self.dist2_bad_position = dist2_bad_position
        self.dist2_cleanup_position = dist2_cleanup_position
        self.dist1_state = "GOOD"
        self.dist2_state = "IDLE"
        self.dist2_target = CATEGORY_BAD
        self.last_action = "-"
        self._dist1_position = 0
        self._dist2_position = 0
        self.on_state_changed = None
        self.cancel_check = None

    @property
    def status(self) -> dict:
        return {
            "dist1_position": max(0, self._dist1_position),
            "dist1_max": self.dist1_open_position,
            "dist1_state": self.dist1_state,
            "dist2_position": max(0, self._dist2_position),
            "dist2_max": max(self.dist2_bad_position, self.dist2_cleanup_position, 1),
            "dist2_state": self.dist2_state,
            "dist2_target": self.dist2_target,
            "last_distributor_action": self.last_action,
        }

    def initialize(self):
        """Установить физический ноль обеих осей."""
        self._check_cancelled()
        self.dist1_state, self.dist2_state = "HOMING", "WAITING"
        self._notify()
        self.dist1.home(); self._wait_dist1(timeout=30.0); self._check_cancelled()
        self.dist1.verify_homed(); self._dist1_position = 0
        self.dist1_state, self.dist2_state = "GOOD", "HOMING"
        self._notify()
        self.dist2.home(); self._wait_dist2(timeout=30.0); self._check_cancelled()
        self.dist2.verify_homed(); self._dist2_position = 0
        self.dist2_state, self.dist2_target, self.last_action = "IDLE", CATEGORY_BAD, "HOMED"
        self._notify()

    def park_production(self):
        """Перед START выставить безопасный GOOD=0 и канал BAD=0.

        Обе заслонки едут одновременно; оси в нужной позиции не двигаются.
        """
        self.last_action = "PARK FOR PRODUCTION"
        self.dist2_target = CATEGORY_BAD
        self._move_parallel(
            dist1_position=0,
            dist2_position=self.dist2_bad_position,
        )
        self.last_action = "PRODUCTION READY"
        self._notify()

    def diagnostic_gate(self, position: str):
        """Диагностика DIST1: HOME=GOOD, OPEN=передать корпус на DIST2.

        Имена HOME/OPEN оставлены только для совместимости HTTP-команд.
        """
        if position == "HOME":
            self.last_action = "DIAGNOSTIC DIST1 -> GOOD"
            self._set_dist1_good()
        elif position == "OPEN":
            self.last_action = "DIAGNOSTIC DIST1 -> DIST2"
            self._set_dist1_to_dist2()
        else:
            raise ValueError(f"Unsupported DIST1 diagnostic position: {position}")
        self._notify()

    def diagnostic_route(self, category: str):
        if category not in (CATEGORY_BAD, CATEGORY_CLEANUP):
            raise ValueError(f"Unsupported DIST2 diagnostic route: {category}")
        # Ручная проверка двигает только свою заслонку: кнопки нижнего
        # распределителя не должны трогать DIST1. Линия в этот момент пуста
        # (диагностика разрешена только без корпусов), поэтому ограничение
        # «DIST1 в GOOD до смены DIST2» здесь не применялось и с переходом
        # на параллельное движение снято и в prepare_route/park_production.
        self.dist2_target, self.last_action = category, f"DIAGNOSTIC DIST2 -> {category}"
        self._move_dist2(category)
        self._notify()

    def prepare_route(self, category: str, part_id: int | None = None):
        """Выставить маршрут следующего корпуса до движения ленты.

        GOOD: движется только DIST1 → 0. BAD/CLEANUP: DIST1 → 340 и
        DIST2 → свой канал едут одновременно (``_move_parallel``). Оси,
        уже стоящие в целевой позиции, не двигаются: серия одинаковых
        маршрутов вообще не шевелит заслонки, а смена канала BAD↔CLEANUP
        двигает только DIST2.
        """
        if category not in (CATEGORY_GOOD, CATEGORY_BAD, CATEGORY_CLEANUP):
            raise ValueError(f"Unsupported distributor category: {category}")
        label = f"PART #{part_id}" if part_id is not None else "PART"
        if category == CATEGORY_GOOD:
            self.last_action = f"{label} -> GOOD"
            self._set_dist1_good()
            self._notify()
            return
        target = self._dist2_target_position(category)
        self.dist2_target = category
        self._move_parallel(
            dist1_position=self.dist1_open_position,
            dist2_position=target,
        )
        self.last_action = f"{label} -> {category} READY"
        self._notify()

    def confirm_transfer(self, part_id: int, category: str):
        """Подтвердить уход корпуса после остановки ленты без смены маршрута.

        Заслонки здесь не двигаются: следующая смена возможна только в
        ``prepare_route`` следующего шага, когда выбрасывающий шаг уже
        полностью завершён. Ждать отдельное время падения не нужно.
        """
        if category not in (CATEGORY_GOOD, CATEGORY_BAD, CATEGORY_CLEANUP):
            raise ValueError(f"Unsupported completed category: {category}")
        self._check_cancelled()
        self.last_action = f"PART #{part_id} -> {category} DONE"
        self._notify()

    def reset_target(self):
        self._notify()

    def emergency_stop(self):
        try:
            self.dist1.transport.send("G25")
        finally:
            self.dist1_state = self.dist2_state = "FAULT"
            self.last_action = "EMERGENCY STOP"
            self._notify()

    def _dist2_target_position(self, category):
        return self.dist2_bad_position if category == CATEGORY_BAD else self.dist2_cleanup_position

    def _move_parallel(self, dist1_position: int, dist2_position: int):
        """Одновременно переместить обе оси распределителя.

        Команды ``G27`` на обе оси отправляются подряд (без паузы между
        ними), затем ожидается остановка каждой оси. Оси, уже стоящие в
        целевой позиции, пропускаются — они не получают команды и не
        меняют состояние.

        Ранее смена DIST2 требовала сначала вернуть DIST1 в GOOD=0
        («безопасная смена»); с переходом на параллельное движение этот
        порядок отменён: лента в момент подготовки маршрута стоит, корпус
        ещё на +3 и заслонок не достиг.
        """
        self._check_cancelled()
        move1 = self._dist1_position != dist1_position
        move2 = self._dist2_position != dist2_position
        if move1:
            self.dist1.move_absolute_async(dist1_position)
        if move2:
            self.dist2.move_absolute_async(dist2_position)
        if move1:
            self.dist1_state = (
                "MOVING_TO_DIST2" if dist1_position else "MOVING_TO_GOOD"
            )
            self._notify()
        if move2:
            self.dist2_state = "MOVING"
            self._notify()
        if move1 or move2:
            self._wait_parallel(move1, move2)
        self._check_cancelled()
        if move1:
            if self.dist1.position != dist1_position:
                raise RuntimeError(
                    f"DIST1 target mismatch: expected {dist1_position}, "
                    f"got {self.dist1.position}"
                )
            self._dist1_position = dist1_position
            self.dist1_state = (
                "TO_DIST2" if dist1_position else "GOOD"
            )
        if move2:
            if self.dist2.position != dist2_position:
                raise RuntimeError(
                    f"DIST2 target mismatch: expected {dist2_position}, "
                    f"got {self.dist2.position}"
                )
            self._dist2_position = dist2_position
            self.dist2_state = "READY"
        print(
            f"[DIST] parallel move -> DIST1={self._dist1_position} "
            f"DIST2={self._dist2_position}"
        )
        self._notify()

    def _move_dist2(self, category: str):
        target = self._dist2_target_position(category)
        self._check_cancelled()
        if self._dist2_position == target:
            print(f"[DIST2] {category} already at POS={target}")
            return
        self.dist2_state = "MOVING"; self._notify()
        self.dist2.move_absolute(target); self._wait_dist2(); self._check_cancelled()
        if self.dist2.position != target:
            raise RuntimeError(f"DIST2 target mismatch: expected {target}, got {self.dist2.position}")
        self._dist2_position = target
        self.dist2_state = "READY"
        print(f"[DIST2] {category} POS={target}")
        self._notify()

    def _set_dist1_to_dist2(self):
        self._check_cancelled()
        if self._dist1_position == self.dist1_open_position:
            print("[DIST1] already TO_DIST2")
            return
        self.dist1_state = "MOVING_TO_DIST2"; self._notify()
        self.dist1.move_absolute(self.dist1_open_position); self._wait_dist1(); self._check_cancelled()
        if self.dist1.position != self.dist1_open_position:
            raise RuntimeError(f"DIST1 DIST2 mismatch: expected {self.dist1_open_position}, got {self.dist1.position}")
        self._dist1_position, self.dist1_state = self.dist1_open_position, "TO_DIST2"
        print(f"[DIST1] TO_DIST2 POS={self._dist1_position}")
        self._notify()

    def _set_dist1_good(self):
        self._check_cancelled()
        if self._dist1_position == 0:
            self.dist1_state = "GOOD"
            return
        self.dist1_state = "MOVING_TO_GOOD"; self._notify()
        self.dist1.move_absolute(0); self._wait_dist1(); self._check_cancelled()
        if self.dist1.position != 0:
            raise RuntimeError(f"DIST1 GOOD mismatch: expected 0, got {self.dist1.position}")
        self._dist1_position, self.dist1_state = 0, "GOOD"
        print("[DIST1] GOOD POS=0")
        self._notify()

    def _check_cancelled(self):
        if self.cancel_check is not None and self.cancel_check():
            raise RuntimeError("Distributor operation cancelled")

    def _wait_dist1(self, timeout=12.0):
        self.dist1.wait_stop(timeout=timeout, progress_callback=self._update_dist1_position)

    def _wait_dist2(self, timeout=12.0):
        self.dist2.wait_stop(timeout=timeout, progress_callback=self._update_dist2_position)

    def _wait_parallel(self, move1: bool, move2: bool, timeout=12.0):
        """Ожидать остановку обеих осей, публикуя прогресс обеих заслонок.

        Оси уже получили команды ``G27`` и едут одновременно. Здесь обе
        опрашиваются в одном цикле, поэтому HMI видит движение DIST1 и
        DIST2 одновременно. Раньше сначала полностью ожидалась DIST1, и
        лишь затем опрашивалась DIST2: к этому моменту DIST2 (равная
        дистанция, равные скорость/ускорение) обычно уже стояла, и UI
        получал только её конечную позицию — маркер второй заслонки
        «перепрыгивал» из одного конца в другой.
        """
        start = time.time()
        while True:
            self._check_cancelled()
            if move1:
                status1 = self.dist1.read_status()
                self._update_dist1_position(status1["position"], status1["moving"])
                move1 = status1["moving"] != 0
            if move2:
                status2 = self.dist2.read_status()
                self._update_dist2_position(status2["position"], status2["moving"])
                move2 = status2["moving"] != 0
            if not move1 and not move2:
                time.sleep(0.05)
                return
            if time.time() - start > timeout:
                raise TimeoutError(
                    f"Оси распределителя не остановились за {timeout}s"
                )
            time.sleep(0.05)

    def _update_dist1_position(self, position, _moving):
        self._check_cancelled()
        if position is not None: self._dist1_position = max(0, int(position))
        self._notify()

    def _update_dist2_position(self, position, _moving):
        self._check_cancelled()
        if position is not None: self._dist2_position = max(0, int(position))
        self._notify()

    def _notify(self):
        if self.on_state_changed: self.on_state_changed()
