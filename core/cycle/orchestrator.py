"""Производственный цикл трёхкамерной линии.

``ProductionCycle`` собран из частей:

* ``CycleStepMixin``         — один шаг ленты и инспекции;
* ``CycleDiagnosticsMixin``  — предстартовые проверки;
* ``CycleJogMixin``          — ручной ход;
* ``CycleStatusMixin``       — снимок для HMI.

Публичный API (ПУСК / СТОП / пауза / JOG / диагностика) не меняется.
"""

import time
import threading
import traceback
from collections import deque

from core.cycle.diagnostics import CycleDiagnosticsMixin, make_diagnostics
from core.cycle.jog import CycleJogMixin
from core.cycle.status import CycleStatusMixin
from core.cycle.step import CycleStepMixin
from core.live_preview import LivePreview
from core.state_machine import StateMachine, State
from core.step_stages import (
    STAGE_SETTLE_SECONDS,
    STAGE_TRACE_SECONDS,
    StepSequencer,
)
from domain.part import CATEGORY_BAD, CATEGORY_UNKNOWN


RECENT_PARTS_LIMIT = 10
DRAIN_TIMEOUT = 120.0

# Пауза после обработки кадров нейросетями: просмотр
# результата анализа до начала следующего шага.
REVIEW_SECONDS = 2.0


class ProductionCycle(
    CycleStepMixin,
    CycleDiagnosticsMixin,
    CycleJogMixin,
    CycleStatusMixin,
):
    """Производственный цикл трёхкамерной линии.

    Один шаг — абсолютная последовательность:
    MOTION → SETTLE → единственная стадия INSPECT (+0):
    CAPTURE → модели → геометрия → решение → запись → REVIEW → PUBLISH.
    Live заморожен на весь инспекционный блок; USB читается по одной камере.
    """

    OFFSET_INSPECT = 0
    # Позиция сортировки: на следующем шаге корпус проходит распределитель.
    # До движения DIST1 выбирает GOOD (0) или передачу на DIST2 (340);
    # DIST2 выбирает BAD (0) или CLEANUP (340).
    OFFSET_REJECT = 3

    JOG_ALLOWED_STATES = ("IDLE", "STOPPED", "PAUSED")

    FRAME_ANALYSIS_GROUPS = ("INSPECT",)

    def __init__(
        self,
        conveyor,
        cameras,
        inspector,
        distributor,
        monitor=None,
        archive=None,
        jog=None,
        settle_seconds=STAGE_SETTLE_SECONDS,
        stage_trace_seconds=STAGE_TRACE_SECONDS,
        review_seconds=REVIEW_SECONDS,
    ):
        self.conveyor    = conveyor
        self.cameras     = cameras
        self.inspector   = inspector
        self.distributor = distributor
        self.monitor     = monitor
        self.archive     = archive
        self.jog         = jog
        self.review_seconds = max(0.0, float(review_seconds))

        self.sm = StateMachine(on_transition=self._on_state_change)

        self._init_counters()
        self._init_runtime_state()
        self._init_camera_pipeline(settle_seconds, stage_trace_seconds)

        # Распределитель сообщает о смене заслонок в HMI и умеет прервать
        # движение по запросу отмены.
        self.distributor.on_state_changed = self._refresh_monitor
        self.distributor.cancel_check = self._cancel_motion.is_set

        # Инспектор сообщает внутренние этапы (модели, геометрия,
        # решение, запись) в тот же telemetry-поток, что и движение линии.
        self.inspector.set_progress_callback(self._on_inspection_progress)

    def _init_counters(self):
        """Учёт деталей на линии и итоги партии."""
        self.parts: list = []
        self.part_counter = 0
        self.current_step = 0

        self.good_count    = 0
        self.bad_count     = 0
        self.cleanup_count = 0
        self.empty_count   = 0   # счётчик пустых лотков

        self.recent_parts = deque(maxlen=RECENT_PARTS_LIMIT)
        self._pending_drop = None

    def _init_runtime_state(self):
        """Состояние шага, блокировки и телеметрия для HMI."""
        self._last_vision_results: dict = {}
        self._last_rule_results: list = []
        self._frame_analysis_groups = self._empty_frame_analysis_groups()

        self._drain_start_time: float = 0
        self._fault_reason = None
        self._operation_lock = threading.Lock()
        self._cancel_motion = threading.Event()

        # Снимки inspection остаются стоп-кадром до следующего
        # движения: live заморожен на весь инспекционный блок.
        self._inspection_display_roles = ()
        self._diagnostics = make_diagnostics()
        self._process = {
            "phase": "IDLE",
            "label": "Система готова к пуску",
            "step": 0,
            "part_id": None,
            "conveyor": {},
        }

        # JOG
        self.jog_active: bool = False
        self._jog_lock = threading.Lock()
        self._selected_analysis_active = False
        self._selected_analysis_role = None
        self._shutdown = False

        # Пауза в рабочем цикле
        self._pause_requested = threading.Event()
        self._pause_frame_active = False

        # Первый шаг после пуска: сначала контроль того, что уже стоит под
        # камерами, и только потом движение ленты.
        self._await_initial_inspection = False

    def _init_camera_pipeline(self, settle_seconds, stage_trace_seconds):
        """Живой просмотр и фазы шага — владельцы камер."""
        # Живой просмотр: работает и в JOG, и во время движения ленты.
        self.live = LivePreview(
            cameras=self.cameras,
            monitor=self.monitor,
            get_active_role=self._get_active_camera_role,
        )

        # Фазы шага и передача камер между live-просмотром и инспекцией.
        self.stages = StepSequencer(
            self.live,
            settle_seconds=settle_seconds,
            trace_seconds=stage_trace_seconds,
            on_stage=self._on_stage_change,
        )

    # Process telemetry

    def _set_process(
        self,
        phase: str,
        label: str,
        *,
        part_id=None,
        conveyor_status=None,
        capture_roles=None,
    ):
        self._process = {
            "phase": phase,
            "label": label,
            "step": self.current_step,
            "part_id": part_id,
            "conveyor": dict(conveyor_status or {}),
            # Роли только что захваченных камер. UI использует это, чтобы
            # было видно, какая стадия Part действительно снималась.
            "capture_roles": list(capture_roles or []),
            "inspection_roles": list(self._inspection_display_roles),
        }
        self._refresh_monitor()

    def _on_inspection_progress(
        self,
        phase: str,
        label: str,
        *,
        part_id=None,
        roles=(),
    ):
        """Показать внутренний этап инспекции в статусе линии.

        Callback наблюдательный: решение уже выполняется Inspector'ом, а
        этот метод только публикует текущую фазу для HMI и не меняет порядок
        обработки.
        """
        prefix = str(phase or "").upper()
        if prefix == "INSPECT_PRESENT" and part_id is not None:
            # Корпус подтверждён правилом наличия — показываем его в
            # «Пути корпусов». Вызов синхронный (один поток цикла),
            # поэтому статус не успеет уйти в HMI раньше разметки.
            part = next(
                (p for p in self.parts if p.id == part_id), None,
            )
            if part is not None:
                part.present = True
        self._set_process(
            prefix,
            label,
            part_id=part_id,
            capture_roles=roles,
        )

    def _on_conveyor_progress(self, status: dict):
        current = self._process
        conveyor_info = dict(status or {})
        # Expose speed for frontend animation timing (higher = faster motion)
        conveyor_info["speed"] = int(self.conveyor.speed)
        self._set_process(
            "CONVEYOR_MOVING",
            "Лента перемещает корпуса на следующую позицию",
            part_id=current.get("part_id"),
            conveyor_status=conveyor_info,
        )

    # Public API

    def request_start(self):
        if not self._operation_lock.acquire(blocking=False):
            return False
        try:
            if self._selected_analysis_active:
                return False
            if self.live.error:
                return False
            if self.jog is not None and self.jog.status.get("error"):
                return False
            if self.jog_active:
                print("[JOG] auto-exit on START")
                self.exit_jog()
            # The frame thread may fail while START waits for JOG shutdown.
            if self.live.error:
                return False
            if self.jog is not None and self.jog.status.get("error"):
                return False
            if self.state not in ("IDLE", "STOPPED"):
                return False
            self._cancel_motion.clear()
            self._set_process(
                "START_POSITIONING",
                "Возврат распределителя в рабочее положение",
            )
            try:
                self.distributor.park_production()
            except Exception as exc:
                self._handle_fault(
                    "Не удалось установить распределитель "
                    f"в рабочее положение: {exc}"
                )
                raise
            accepted = self.sm.request_start()
            if accepted:
                self._drain_start_time = 0
                self._fault_reason = None
                self._reset_frame_analysis()
                # Деталь могла остаться под камерами ещё до пуска:
                # первый шаг выполняется без движения ленты, чтобы она
                # попала в учёт, а не уехала дальше непроверенной.
                self._pause_requested.clear()
                self._await_initial_inspection = True
                if self._diagnostics.get("kind") == "SELECTED_MODEL":
                    self._diagnostics = make_diagnostics(
                        message="Анализ кадра ещё не выполнялся",
                    )
                # Оператор видит поток всё время, пока линия работает;
                # на статических этапах шага он приостанавливается.
                self.live.start()
                self._set_process("READY", "Цикл запущен")
            return accepted
        finally:
            self._operation_lock.release()

    def request_stop(self):
        self._pause_requested.clear()
        # Сначала STOPPING: exit_jog не должен гасить live, пока линия
        # ещё дренирует корпуса. Если сначала выйти из JOG в PAUSED,
        # is_active=False и теряется видеопоток на всём стопе.
        accepted = self.sm.request_stop()
        if self._pause_frame_active:
            self._stop_pause_frame_loop()
        return accepted

    def request_exit(self):
        self._pause_requested.clear()
        accepted = self.sm.request_exit()
        if self._pause_frame_active:
            self._stop_pause_frame_loop()
        return accepted

    def request_pause(self) -> bool:
        """Запросить паузу перед началом нового цикла анализа."""
        if self.state != "RUNNING" or self.exit_requested:
            return False
        if self._pause_requested.is_set():
            return True
        self._pause_requested.set()
        self._set_process(
            "PAUSE_REQUESTED",
            "Пауза будет применена перед началом нового цикла анализа",
        )
        self._refresh_monitor()
        return True

    def request_resume(self) -> bool:
        """Возобновить работу линии из паузы."""
        if not self._operation_lock.acquire(blocking=False):
            return False
        try:
            if self.state != "PAUSED" or self.exit_requested:
                return False
            if self.jog is not None and (self.jog.busy or self.jog.status.get("error")):
                return False
            # После возобновления стадия всё равно проходит полный свежий
            # захват, модели, геометрию и принятие решения.
            self._pause_requested.clear()
            accepted = self.sm.request_resume()
            if not accepted:
                return False
            self._stop_pause_frame_loop()
            print("[PAUSE] resume; работа возобновлена")
            self._set_process(
                "RESUMED",
                "Работа возобновлена после паузы",
            )
            self._refresh_monitor()
            return True
        finally:
            self._operation_lock.release()

    def request_force_exit(self):
        self._cancel_motion.set()
        self._pause_requested.clear()
        if self._pause_frame_active:
            self._stop_pause_frame_loop()
        self.stages.reset()
        self.live.reset_pause()
        accepted = self.sm.request_force_exit()
        if self.jog_active:
            try:
                self.exit_jog()
            except Exception as exc:
                print(f"[JOG] release during force exit failed: {exc}")
        self._safe_emergency_stop()
        return accepted

    # Properties для UI и main.py

    @property
    def state(self) -> str:
        return self.sm.state.value

    @property
    def exit_requested(self) -> bool:
        return self.sm.exit_requested

    @property
    def force_exit_requested(self) -> bool:
        return self.sm.force_exit

    @property
    def dist1_open_position(self) -> int:
        return self.distributor.dist1_open_position

    # Main loop

    def start(self):
        print("Система готова. Ожидание команды START.")

        try:
            while True:
                if self.sm.force_exit:
                    print("[EXIT] Force exit.")
                    break

                if self.sm.is_active:
                    # STOP on an already empty line must not advance Conveyor.
                    if self.sm.state == State.STOPPING and not self.parts:
                        self.sm.notify_line_empty()
                        self._refresh_monitor()
                        if self.sm.exit_requested:
                            print("[EXIT] Line empty -> exit.")
                            break
                        continue

                    self._run_once_safe()

                    if (
                        self.sm.state == State.STOPPING
                        and not self.parts
                    ):
                        self.sm.notify_line_empty()
                        self._refresh_monitor()

                        if self.sm.exit_requested:
                            print("[EXIT] Line empty -> exit.")
                            break
                else:
                    if self.sm.exit_requested:
                        print("[EXIT] Not active -> exit.")
                        break

                    if self.live.error and self.sm.state != State.FAULT:
                        self._handle_fault(
                            f"Ошибка камеры в режиме ручного управления: {self.live.error}"
                        )
                        continue
                    if self.jog is not None:
                        jog_status = self.jog.status
                        jog_error = jog_status.get("error")
                        if jog_error and self.sm.state != State.FAULT:
                            self._handle_fault(f"Ошибка ручного управления лентой: {jog_error}")
                            continue
                        if (
                            self._process.get("phase") == "JOG_HOLD"
                            and not jog_status.get("busy")
                        ):
                            self._set_process(
                                "JOG_STOPPED",
                                f"JOG остановлен: {jog_status.get('last_action', '-')}",
                            )
                            continue
                    self._refresh_monitor()
                    time.sleep(0.1)

        except Exception as e:
            print(f"[CYCLE] Critical error: {e}")
            traceback.print_exc()
            self._handle_fault(f"Критическая ошибка цикла: {e}")
        finally:
            self._shutdown = True
            self._cancel_motion.set()
            self._pause_requested.clear()
            try:
                self._stop_pause_frame_loop()
            except Exception as e:
                print(f"[SHUTDOWN] stop pause frame loop failed: {e}")
            self.stages.reset()
            self.live.reset_pause()
            self.live.stop()
            try:
                self.exit_jog()
            except Exception as e:
                print(f"[SHUTDOWN] exit_jog failed: {e}")
            self._safe_emergency_stop()
            self._archive_inflight("runtime_shutdown")
            print("Цикл конвейера завершён.")

    # Fault

    def _handle_fault(self, reason: str):
        self._cancel_motion.set()
        self._pause_requested.clear()
        self._stop_pause_frame_loop()
        self._selected_analysis_active = False
        self._selected_analysis_role = None
        self.stages.reset()
        self.live.reset_pause()
        self.live.stop()
        self._fault_reason = reason
        print(f"[FAULT] {reason}")
        print(
            f"[FAULT] В очереди осталось "
            f"{len(self.parts)} деталей"
        )
        self.sm.notify_fault()
        if self.jog_active:
            try:
                self.exit_jog()
            except Exception as exc:
                print(f"[JOG] release during fault failed: {exc}")
        self._set_process("FAULT", reason)
        self._safe_emergency_stop()
        self._refresh_monitor()

    # Safe run

    def _run_once_safe(self):
        if self.sm.state == State.STOPPING and self.parts:
            if self._drain_start_time == 0:
                self._drain_start_time = time.time()
            elif time.time() - self._drain_start_time > DRAIN_TIMEOUT:
                self._handle_fault(
                    f"Превышено время штатной остановки {DRAIN_TIMEOUT} с; "
                    f"на линии осталось корпусов: {len(self.parts)}"
                )
                return

        try:
            self._run_once()
        except Exception as e:
            # Повтор неудачного физического шага теряет соответствие
            # деталь/ячейка, поэтому падаем в FAULT на первой же ошибке.
            print(f"[CYCLE] Error in _run_once: {e}")
            traceback.print_exc()
            self._handle_fault(f"Ошибка производственного шага: {e}")

    def _safe_emergency_stop(self):
        errors = []
        try:
            self.conveyor.emergency_stop()
        except Exception as e:
            errors.append(f"conveyor: {e}")
        try:
            self.distributor.emergency_stop()
        except Exception as e:
            errors.append(f"distributor: {e}")
        if errors:
            print(f"[SHUTDOWN] Emergency stop errors: {'; '.join(errors)}")

    # Archive

    def _archive_part(self, part, extra=None) -> bool:
        """Записать деталь и вернуть подтверждённый результат записи."""
        if not self.archive:
            return False
        kwargs = {
            "part_id": part.id,
            "category": part.route_category,
            "decision": part.final_decision,
            "defects": part.get_all_defects(),
            "step": part.step_created,
        }
        archive_extra = {}
        if extra:
            archive_extra.update(extra)
        if archive_extra:
            kwargs["extra"] = archive_extra
        try:
            return bool(self.archive.finalize(**kwargs))
        except Exception as exc:
            print(f"[ARCHIVE] Не удалось записать деталь #{part.id}: {exc}")
            return False

    def _archive_inflight(self, reason: str):
        for part in list(self.parts):
            if part.route_category == CATEGORY_UNKNOWN:
                part.route_category = CATEGORY_BAD
            part.final_decision = f"aborted_{reason}"
            try:
                self._archive_part(
                    part,
                    extra={"aborted": True, "abort_reason": reason},
                )
            except Exception as e:
                print(f"[ARCHIVE] Failed to archive aborted part #{part.id}: {e}")
            self._remove_part(part)
        self._pending_drop = None

    # Helpers

    def _remove_part(self, part):
        if part in self.parts:
            self.parts.remove(part)

    def _register_finished(self, part):
        record = {
            "id":       part.id,
            "decision": part.final_decision,
            "category": part.route_category,
            "time":      time.time(),
        }
        # UI получает только лёгкую ссылку на архивную запись. Само
        # изображение не копируется в recent-кэш и не исчезает из архива,
        # когда деталь покидает последние десять.
        if self.archive:
            try:
                archive_info = self.archive.get_part_info(part.id)
            except Exception as exc:
                print(f"[ARCHIVE] Не удалось прочитать карточку #{part.id}: {exc}")
                archive_info = None
            if archive_info:
                record["batch_id"] = self.archive.batch_id
                record["archive_folder"] = archive_info.get("relative_folder")
        self.recent_parts.append(record)

    def _on_stage_change(self, previous, current, elapsed: float):
        """Печать границы фаз шага: видно, где именно проводится время."""
        print(
            f"[STAGE] {previous.value} -> {current.value} "
            f"(предыдущая фаза {elapsed:.2f} с)"
        )

    def _on_state_change(self, _old, new, _action: str):
        if new == State.STOPPING:
            self._set_process("DRAINING", "Остановка")
        elif new == State.STOPPED:
            # Линия пуста: последние кадры с разметкой остаются на экране,
            # пока не будет включён JOG или не запустится цикл заново.
            self.stages.reset()
            self.live.stop()
            self._set_process("STOPPED", "Линия остановлена и пуста")
        elif new == State.FAULT:
            self._set_process("FAULT", "Цикл остановлен из-за ошибки")
        else:
            self._refresh_monitor()
