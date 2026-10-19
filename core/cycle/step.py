"""Один производственный шаг ленты и инспекции (3 камеры).

Часть ``ProductionCycle``: MOTION → SETTLE → единственная стадия INSPECT
на +0 → REVIEW → PUBLISH, пауза и выброс корпуса на +3.
"""

import time

from core.state_machine import State
from domain.part import (
    Part,
    CATEGORY_GOOD,
    CATEGORY_BAD,
    CATEGORY_CLEANUP,
    CATEGORY_UNKNOWN,
)


class CycleStepMixin:
    """Последовательный шаг ленты, инспекции и распределителя."""

    def _check_motion_cancelled(self):
        if self._cancel_motion.is_set() or self.sm.force_exit:
            raise RuntimeError("physical operation cancelled")

    # Core step

    def _run_once(self):
        """Один шаг линии: движение, затухание, съёмка, анализ, публикация.

        Владелец камер меняется только на границах фаз, поэтому кадры для
        defect rules физически не могут быть сняты во время движения.
        """
        self._check_motion_cancelled()
        print(f"\nШАГ {self.current_step + 1}")

        # Право принять деталь фиксируется до движения: если STOP придёт
        # уже во время проезда, вошедшая этим шагом деталь всё равно будет
        # проинспектирована и останется синхронной со своей ячейкой.
        accept_input_for_this_step = self.sm.accepts_new_parts

        self._last_vision_results = {}
        self._last_rule_results = []

        # Абсолютная последовательность: сначала механика, затем занятая
        # стадия целиком (кадр → модели → геометрия → решение → запись).
        pending_id = self._stage_motion()
        self._stage_settle(pending_id)
        self._check_pause_barrier()
        display_frames = self._inspect_occupied_stages(
            accept_input_for_this_step,
        )
        self._stage_review(display_frames)
        self._stage_publish(display_frames)

    def _stage_motion(self):
        """MOTION: подготовить маршрут и переместить ленту на шаг."""
        self.stages.enter_motion()
        self._inspection_display_roles = ()
        # Разметка прошлого шага построена по статичному кадру и на
        # движущемся изображении указывала бы мимо детали.
        try:
            self.live.clear_overlays()
        except Exception as exc:
            print(f"[LIVE] Не удалось снять разметку: {exc}")

        if self._await_initial_inspection:
            # Деталь уже стоит под камерами: сначала её контроль,
            # движение ленты начнётся со следующего шага. Счётчик шагов не
            # увеличивается — физическая позиция не изменилась.
            self._await_initial_inspection = False
            self._set_process(
                "INITIAL_INSPECTION",
                "Корпус уже под камерами: контроль без движения ленты",
            )
            self._check_motion_cancelled()
            return None

        self._pending_drop = self._find_pending_drop()
        pending_id = self._pending_drop.id if self._pending_drop else None
        self._set_process(
            "ROUTE_PREPARE",
            "Подготовка маршрута распределителя",
            part_id=pending_id,
        )
        self._prepare_drop()
        self._check_motion_cancelled()

        self._set_process(
            "CONVEYOR_COMMAND",
            "Команда движения ленты отправлена",
            part_id=pending_id,
        )
        self.conveyor.move_step()
        self.conveyor.wait_stop(progress_callback=self._on_conveyor_progress)
        self._check_motion_cancelled()
        # Логическая позиция фиксируется только после подтверждения
        # физического завершения движения.
        self.current_step += 1
        return pending_id

    def _stage_settle(self, pending_id):
        """SETTLE: подтвердить передачу корпуса и погасить вибрацию."""
        self._set_process(
            "CONVEYOR_CONFIRMED", "Позиции корпусов подтверждены контроллером",
            part_id=pending_id,
        )
        if self._pending_drop is not None:
            self._set_process(
                "PART_TRANSFER", "Корпус прошёл распределитель",
                part_id=pending_id,
            )
        self._execute_drop()
        self._check_motion_cancelled()
        self._set_process("SETTLE", "Ожидание затухания вибрации перед съёмкой")
        self.stages.enter_settle()
        self._check_motion_cancelled()

    def _occupied_inspection_stages(self, accept_input_for_this_step: bool):
        """Занятые стадии этого шага: одна стадия INSPECT на +0."""
        stages = []
        if accept_input_for_this_step:
            stages.append(("INSPECT", tuple(self.inspector.INSPECT_ROLES)))
        return stages

    def _inspect_occupied_stages(self, accept_input_for_this_step: bool):
        """Занятая стадия: свой кадр, свои модели, своё решение.

        Live заморожен на весь блок. Решение записывается до PUBLISH.
        """
        display_frames = {}
        stages = self._occupied_inspection_stages(accept_input_for_this_step)
        if not stages:
            self.stages.enter_capture(())
            self.stages.enter_analysis()
            return display_frames

        for name, roles in stages:
            frames = self._stage_capture(roles)
            self.stages.enter_analysis()
            if name == "INSPECT":
                self._set_process(
                    "INSPECT_ANALYSIS",
                    "Инспекция: модели и правила по свежему кадру",
                )
                result = self._process_inspect_stage(frames)
            else:
                raise RuntimeError(f"Неизвестная стадия инспекции: {name}")
            if result is not None:
                display_frames.update(result.raw_frames or {})
                self._refresh_monitor(display_frames)
            self._check_motion_cancelled()
        return display_frames

    def _stage_capture(self, roles):
        """CAPTURE: последовательный frozen snapshot одной стадии."""
        roles = tuple(roles or ())
        self._inspection_display_roles = tuple(dict.fromkeys(
            (*self._inspection_display_roles, *roles)
        ))
        self.stages.enter_capture(roles)
        self._set_process(
            "CAMERA_CAPTURE",
            (
                f"Последовательный захват камер: {', '.join(roles)}"
                if roles else "Нет корпуса под инспекционными камерами"
            ),
            capture_roles=roles,
        )
        if not roles:
            return {}

        # Драйвер может отдать старый кадр из буфера после движения.
        # Дренируем и читаем роли этой стадии строго по одной.
        self.cameras.drain_buffers(roles)
        frames = self.cameras.capture_roles(roles)
        if set(frames) != set(roles):
            raise RuntimeError(
                f"Неполный набор кадров для инспекции: ожидались {sorted(roles)}, "
                f"получены {sorted(frames)}"
            )
        self._check_motion_cancelled()
        # Exclusive-блок не отпускаем: live не должен затирать стоп-кадр
        # во время моделей, геометрии и ревью.
        self._refresh_monitor(frames)
        return frames

    def _stage_review(self, display_frames):
        """REVIEW: пауза на просмотр работы нейросетей после анализа.

        Кадры со статичной разметкой уже опубликованы и остаются на
        экране, а лента стоит: даётся время отсмотреть результат
        до начала следующего шага. Паузу можно прервать остановкой или
        выходом из программы.
        """
        if self.review_seconds <= 0:
            return
        self._refresh_monitor(display_frames)
        deadline = time.monotonic() + self.review_seconds
        shown_seconds = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if (
                self._cancel_motion.is_set()
                or self.sm.force_exit
                or self.sm.exit_requested
                or self.sm.state != State.RUNNING
            ):
                break
            whole = int(remaining + 0.999)
            if whole != shown_seconds:
                shown_seconds = whole
                self._set_process(
                    "ANALYSIS_REVIEW",
                    "Просмотр результатов анализа: "
                    f"{whole} с до следующего шага",
                )
            time.sleep(min(0.1, max(remaining, 0.01)))
        # FORCE EXIT во время паузы сбрасывает цепочку фаз: выходить нужно
        # штатной ошибкой отмены до входа в PUBLISH, а не сбросом шага.
        self._check_motion_cancelled()

    def _stage_publish(self, display_frames):
        """PUBLISH: маршрут годных деталей и вывод результата на экран."""
        self.stages.enter_publish()

        self._set_process("STEP_COMPLETE", "Шаг полностью завершён")
        self._refresh_monitor(display_frames)

    # Пауза

    def _check_pause_barrier(self):
        """Пауза после полной остановки шага и до работы нейронок.

        Оператор может поправить линию с помощью jog без ограничений.
        """
        if (
            self.sm.state == State.RUNNING
            and self._pause_requested.is_set()
            and not self.sm.exit_requested
        ):
            if self.sm.request_pause():
                self._enter_pause_frame()
            else:
                self._pause_requested.clear()

        while self.sm.state == State.PAUSED:
            if self.sm.exit_requested or self.sm.force_exit:
                self._pause_requested.clear()
                self._stop_pause_frame_loop()
                self.sm.request_stop()
                break
            if self.live.error:
                self._handle_fault(
                    "Ошибка камеры во время паузы: "
                    f"{self.live.error}"
                )
                break
            jog_error = (
                self.jog.status.get("error")
                if self.jog is not None else None
            )
            if jog_error:
                self._handle_fault(f"Ошибка ручного управления (JOG): {jog_error}")
                break
            self._refresh_monitor()
            time.sleep(0.05)

        if self._pause_frame_active:
            self._stop_pause_frame_loop()

    def _enter_pause_frame(self):
        """Включить режим JOG и отображение состояния паузы."""
        if not self._pause_frame_active:
            self._pause_frame_active = True
        self.enter_jog()
        # Пауза происходит ДО анализа изображения. Разметка предыдущего
        # шага построена по статичному кадру и на live-изображении из JOG
        # указывала бы мимо детали — убираем её немедленно.
        try:
            self.live.clear_overlays()
        except Exception as exc:
            print(f"[LIVE] Не удалось снять разметку: {exc}")
        print("[PAUSE] линия остановлена на границе шага после полной остановки")
        self._set_process(
            "PAUSED",
            "Пауза: доступна ручная коррекция ленты с помощью JOG",
        )

    def _stop_pause_frame_loop(self):
        if not self._pause_frame_active:
            return
        self._pause_frame_active = False
        try:
            self.exit_jog()
        except Exception as exc:
            print(f"[PAUSE] Не удалось выйти из JOG: {exc}")

    # Inspect (единственная стадия инспекции, +0)

    def _process_inspect_stage(self, frames):
        """Обработать зону инспекции (+0) по свежему кадру."""

        candidate_id = self.part_counter + 1

        # Корпус физически стоит на +0 с момента остановки ленты, поэтому
        # попадает в учёт ДО запуска моделей: HMI видит его в инспекционной
        # зоне на весь анализ, а не «внезапно» на +1 после следующего хода.
        # Модель позиций не меняется: step_created прежний — тот же шаг, на
        # котором лента остановила корпус на +0.
        part = Part(candidate_id, self.current_step)
        self.parts.append(part)

        self._set_process(
            "INSPECT_ANALYSIS",
            f"Инспекция: анализ кандидата #{candidate_id}",
            part_id=candidate_id,
        )

        try:
            result = self.inspector.inspect(
                part_id=candidate_id,
                step=self.current_step,
                frames=frames,
            )
        except Exception:
            # Сбой инспекции не оставляет неучтённый корпус в очереди:
            # FAULT — терминальное состояние, учёт строится с нуля.
            self.parts.remove(part)
            raise
        if result.is_empty_tray:
            # Пустая ячейка остаётся нейтральной: Part и архив не создаются.
            self.parts.remove(part)
            self._record_frame_analysis("INSPECT", None, result)
            self.empty_count += 1
            # Очищаем детекции, чтобы не рисовать разметку на пустой ячейке.
            for role in self.inspector.INSPECT_ROLES:
                self._last_vision_results[role] = []
            self._last_rule_results.extend(result.rule_results or [])
            self._set_process(
                "INSPECT_RESULT_RECORDED",
                "Инспекция: пустая ячейка записана",
            )
            print(
                f"[EMPTY] Пустая ячейка на step {self.current_step} "
                f"(total empty: {self.empty_count})"
            )
            return result

        self.part_counter += 1
        for defect in result.defects:
            part.add_input_defect(defect)
        # Результат правил становится состоянием Part только после того,
        # как модели и геометрия отработали для этого же набора кадров.
        part.mark_input_done()
        self._record_frame_analysis("INSPECT", part.id, result)
        print(f"[INSPECT] Деталь #{part.id}")

        self._last_vision_results.update(result.vision_results or {})
        self._last_rule_results.extend(result.rule_results or [])

        self._store_stage_frames(part.id, result)
        self._set_process(
            "INSPECT_RESULT_RECORDED",
            "Инспекция: решение стадии записано",
            part_id=part.id,
        )

        print(
            f"[INSPECT] Деталь #{part.id} "
            f"дефекты: {result.defects or ['none']} "
            f"категория={part.route_category}"
        )
        return result

    # Распределитель

    def _find_pending_drop(self):
        """Вернуть корпус на +3, который на следующем шаге пройдёт заслонки."""
        for part in self.parts:
            if part.step_created + self.OFFSET_REJECT == self.current_step:
                return part
        return None

    def _prepare_drop(self):
        part = self._pending_drop
        if part is None:
            self.distributor.reset_target()
            return
        category = part.route_category
        if category == CATEGORY_UNKNOWN:
            print(
                f"[WARN] Деталь #{part.id} не прошла полную инспекцию "
                "-> принудительно BAD"
            )
            part.route_category = CATEGORY_BAD
            part.final_decision = "incomplete_inspection"
            category = CATEGORY_BAD
        # GOOD: DIST1=0. BAD/CLEANUP: DIST1=340 и DIST2->канал едут
        # одновременно (см. _move_parallel в hardware/distributor.py).
        self.distributor.prepare_route(category, part.id)

    def _execute_drop(self):
        part = self._pending_drop
        if part is None:
            return
        category = part.route_category
        # Шаг ленты уже закончился. Заслонки остаются как есть до
        # ROUTE_PREPARE следующего шага, поэтому ждать падение отдельно
        # не нужно: к следующей смене маршрута корпус давно ушёл.
        self.distributor.confirm_transfer(part.id, category)
        if category == CATEGORY_GOOD:
            self.good_count += 1
            print(f"[PASS] #{part.id} -> GOOD ({self.good_count})")
        elif category == CATEGORY_BAD:
            self.bad_count += 1
            print(f"[REJECT] #{part.id} -> BAD ({self.bad_count})")
        elif category == CATEGORY_CLEANUP:
            self.cleanup_count += 1
            print(f"[CLEANUP] #{part.id} -> CLEANUP ({self.cleanup_count})")
        else:
            print(f"[WARN] Деталь #{part.id} неизвестная категория {category} -> BAD")
            part.route_category = CATEGORY_BAD
            category = CATEGORY_BAD
            self.bad_count += 1
        archived = self._archive_part(part)
        if archived:
            self._set_process(
                "FINAL_DECISION_ARCHIVED",
                f"Финальное решение #{part.id}: {category} записано в архив",
                part_id=part.id,
            )
        else:
            self._set_process(
                "FINAL_DECISION_NOT_ARCHIVED",
                f"Финальное решение #{part.id}: {category}; "
                "запись в архив не выполнена",
                part_id=part.id,
            )
        self._register_finished(part)
        self._remove_part(part)
        self._pending_drop = None

    def _store_stage_frames(self, part_id, result):
        """Кадры стадии в архив. Сбой диска не откатывает решение Part."""
        if not self.archive or result is None:
            return
        try:
            self.archive.store_frames(
                part_id=part_id,
                raw_frames=result.raw_frames or {},
                annotated_frames=result.annotated or {},
                raw_overlay_frames=result.raw_overlay_frames or {},
            )
        except Exception as exc:
            print(f"[ARCHIVE] Не удалось сохранить кадры #{part_id}: {exc}")
