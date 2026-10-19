"""Standalone, hardware-free simulator for the Conveyor Three UI (3 камеры).

Run from the repository root:
    python ui_simulation.py --host 0.0.0.0 --port 8000

It serves the same FastAPI UI as production but never opens cameras, serial
ports, models, or pywebview. Use the UI buttons to start, pause, resume and
stop the simulated conveyor.
"""
from __future__ import annotations

import argparse
import signal
import threading
from dataclasses import dataclass

import cv2
import numpy as np

from domain.threshold_loader import ThresholdLoader
from inspection.part_archive import PartArchive
from vision.ui.server.server import CAMERA_ORDER, UIServer


# Ручной ход (JOG) разрешён в тех же состояниях, что и в production:
# IDLE / STOPPED / PAUSED. Пауза — это как раз коррекция положения ленты.
JOG_ALLOWED_STATES = ("IDLE", "STOPPED", "PAUSED")


PROCESS_LABELS = {
    "IDLE": "Система готова к пуску",
    "READY": "Цикл запущен",
    "INITIAL_INSPECTION": "Контроль корпуса под камерами без движения",
    "ROUTE_PREPARE": "Подготовка маршрута распределителя",
    "MOTION": "Горизонтальное движение ленты",
    "SETTLE": "Ожидание затухания вибрации",
    "CAPTURE": "Захват стоп-кадра камеры",
    "INSPECT_MODELS": "ИНСПЕКЦИЯ: запуск моделей",
    "INSPECT_PRESENCE": "ИНСПЕКЦИЯ: проверка наличия детали",
    "INSPECT_GEOMETRY": "ИНСПЕКЦИЯ: построение геометрии и измерений",
    "INSPECT_DECISION": "ИНСПЕКЦИЯ: решение правил сформировано",
    "INSPECT_RESULT_RECORDED": "ИНСПЕКЦИЯ: решение стадии записано",
    "ANALYSIS_REVIEW": "Просмотр результатов анализа",
    "PUBLISH": "Публикация результата контроля",
    "STOPPING": "Остановка",
    "STOPPED": "Линия остановлена и пуста",
    "PAUSE_REQUESTED": "Пауза будет применена на границе шага",
    "PAUSED": "Пауза линии",
    "SELECTED_MODEL_ANALYSIS": "Анализ выбранного стоп-кадра",
    "JOG_HOLD": "JOG: удерживаемое движение ленты",
    "JOG_STOPPED": "JOG: движение ленты остановлено",
    "DISTRIBUTOR_DIAGNOSTIC": "Проверка распределителя",
    "CAMERA_DIAGNOSTIC": "Проверка трёх камер",
    "VISION_RULE_DIAGNOSTIC": "Проверка моделей и правил",
}


@dataclass
class SimRule:
    """Small compatible rule-result object consumed by DebugOverlay."""
    drawings: list[dict]


@dataclass
class SimPart:
    id: int
    position: int = 0
    category: str = ""
    defects: list[str] | None = None
    inspected: bool = False


class LineSimulation:
    """Small deterministic conveyor model intended for UI development."""

    ROUTE_PREPARE_SECONDS = 0.24
    # Keep the virtual transport long enough for the deliberately slower UI
    # animation to show the complete horizontal step.
    STEP_SECONDS = 1.05
    SETTLE_SECONDS = 0.28
    # Time with a stopped belt: the vertical input/output animation must
    # finish before another horizontal step starts.
    POST_STOP_SECONDS = 0.86
    CAMERA_STAGE_SECONDS = 0.13
    REVIEW_SECONDS = 2.0
    CATEGORIES = ("GOOD", "BAD", "CLEANUP", "GOOD", "GOOD", "BAD")
    INSPECT_STAGES = ("LEFT", "MIDDLE", "RIGHT")

    def __init__(self, server: UIServer):
        self.server = server
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.state = "IDLE"
        self.dist1_position = 0
        self.dist2_position = 0
        self.dist1_state = "GOOD"
        self.dist2_state = "IDLE"
        self._planned_route = "GOOD"
        self.last_distributor_action = "SIMULATION READY"
        # Ручной ход (JOG) двигает ЛЕНТУ, а не распределитель. Это отдельная
        # координата виртуального конвейера, ограниченная как физическая ось.
        self.conveyor_position = 0
        self.conveyor_max = 340
        self.jog_active = False
        self.jog_busy = False
        self.selected_role: str | None = None
        self.archive_compressed = False
        self.step = 0
        self.next_id = 1
        self.slot_counter = 0
        self.empty_count = 0
        self.parts: list[SimPart] = []
        self.egress: SimPart | None = None
        # The first production cycle may start with a body already under
        # the cameras; mirror the hardware's no-motion initial inspection.
        self._await_initial_inspection = False
        # Как в ProductionCycle: пауза запрашивается сразу, а применяется
        # только на границе шага — после SETTLE и до CAPTURE.
        self._pause_requested = False
        self.recent: list[dict] = []
        self.counts = {"total": 0, "good": 0, "bad": 0, "cleanup": 0}
        self.thread = threading.Thread(target=self._run, name="ui-simulation", daemon=True)

    def _open_next_archive_batch(self) -> None:
        """A new production launch must never append to a closed ZIP batch."""
        archive = self.server.archive
        if archive is None or not self.archive_compressed:
            return
        self.server.archive = PartArchive(
            root_folder=archive.root_folder,
        )
        self.archive_compressed = False

    def start(self) -> bool:
        with self._lock:
            # «Замёрзшая» линия: поток остановлен ВЫХОДОМ (close), а
            # состояние осталось RUNNING/PAUSED/STOPPING.
            frozen = (not self.thread.is_alive()) and self._stop.is_set()
            if self.state in ("RUNNING", "PAUSED", "STOPPING") and not frozen:
                return False
            # Повторный ПУСК после ВЫХОДА оживляет поток симуляции без
            # перезапуска сервера, чтобы цикл не оставался на последней
            # опубликованной фазе.
            if frozen:
                self._stop.clear()
                self._wake.clear()
                self.thread = threading.Thread(
                    target=self._run, name="ui-simulation", daemon=True,
                )
                self.thread.start()
            self._open_next_archive_batch()
            # Как ProductionCycle.request_start: сначала выходим из JOG,
            # иначе после удержания кнопки остаётся jog_busy и стрелки
            # «залипают» до следующего release.
            self.jog_active = False
            self.jog_busy = False
            self._pause_requested = False
            # A launch begins with the same special initial inspection as
            # ProductionCycle: do not advance the virtual belt first.
            self._await_initial_inspection = not self.parts and self.egress is None
            self.state = "RUNNING"
            self._wake.set()
        self._publish("READY")
        return True

    def _rest_phase(self) -> str:
        """Фаза после входа/выхода из JOG без движения: не перетираем
        паузу, когда ручной режим открыт на паузе."""
        return "PAUSED" if self.state == "PAUSED" else "IDLE"

    def enter_jog(self) -> bool:
        with self._lock:
            if self.state not in JOG_ALLOWED_STATES:
                return False
            self.jog_active = True
        self._publish(self._rest_phase())
        return True

    def exit_jog(self) -> bool:
        with self._lock:
            self.jog_active = False
            self.jog_busy = False
        self._publish(self._rest_phase())
        return True

    def jog_hold_start(self, direction: str) -> bool:
        with self._lock:
            if (
                not self.jog_active
                or self.state not in JOG_ALLOWED_STATES
                or self.selected_role is not None
            ):
                return False
            self.jog_busy = True
            # JOG двигает ленту: сдвигаем координату виртуального
            # конвейера. Распределитель (DIST1/DIST2) при этом не меняется —
            # им управляют только маршрутизация и ручная диагностика.
            delta = 5 if direction == "+" else -5
            self.conveyor_position = max(
                0, min(self.conveyor_max, self.conveyor_position + delta)
            )
        self._publish("JOG_HOLD")
        return True

    def jog_hold_heartbeat(self, direction: str) -> bool:
        """Dead-man: продлеваем удержание и продолжаем непрерывный ход.

        В отличие от production-``heartbeat`` (только «поддержание жизни»),
        здесь ещё сдвигаем ленту на малый шаг: в реальной линии удержание
        кнопки двигает конвейер непрерывно. Распределитель не трогаем.
        """
        with self._lock:
            if (
                not self.jog_active
                or not self.jog_busy
                or self.state not in JOG_ALLOWED_STATES
            ):
                return False
            delta = 1 if direction == "+" else -1
            self.conveyor_position = max(
                0, min(self.conveyor_max, self.conveyor_position + delta)
            )
        # Публикуем состояние, чтобы непрерывное движение ленты было
        # видно в UI во время удержания (heartbeat приходит каждые ~100 мс).
        self._publish("JOG_HOLD")
        return True

    def jog_hold_release(self, _reason: str = "") -> bool:
        with self._lock:
            self.jog_busy = False
        self._publish("JOG_STOPPED")
        return True

    def selected_analysis(self, role: str) -> bool:
        with self._lock:
            if self.state not in ("IDLE", "STOPPED") or role not in CAMERA_ORDER:
                return False
            self.selected_role = role
        self._publish("SELECTED_MODEL_ANALYSIS")
        return True

    def release_selected_analysis(self) -> bool:
        with self._lock:
            self.selected_role = None
        self._publish("IDLE")
        return True

    def distributor_diagnostic(self, command: str) -> bool:
        with self._lock:
            if self.state not in ("IDLE", "STOPPED"):
                return False
            # Ручная проверка двигает только свою заслонку: кнопки верхнего
            # распределителя управляют DIST1, кнопки нижнего — только DIST2.
            if command == "DIST1_HOME":
                self.dist1_position = 0
                self.dist1_state = "GOOD"
                self.last_distributor_action = "DIAGNOSTIC DIST1 -> GOOD"
            elif command == "DIST1_OPEN":
                self.dist1_position = 340
                self.dist1_state = "TO_DIST2"
                self.last_distributor_action = "DIAGNOSTIC DIST1 -> DIST2"
            elif command == "DIST2_BAD":
                moved = self.dist2_position != 0
                self.dist2_position = 0
                if moved:
                    self.dist2_state = "READY"
                self.last_distributor_action = "DIAGNOSTIC DIST2 -> BAD"
            elif command == "DIST2_CLEANUP":
                moved = self.dist2_position != 340
                self.dist2_position = 340
                if moved:
                    self.dist2_state = "READY"
                self.last_distributor_action = "DIAGNOSTIC DIST2 -> CLEANUP"
            else:
                return False
        self._publish("DISTRIBUTOR_DIAGNOSTIC")
        return True

    def camera_diagnostic(self) -> bool:
        self._publish("CAMERA_DIAGNOSTIC")
        return True

    def vision_rule_diagnostic(self) -> bool:
        self._publish("VISION_RULE_DIAGNOSTIC")
        return True

    def stop(self) -> bool:
        """Production-like stop: stop feeding, then drain bodies on line."""
        with self._lock:
            if self.state not in ("RUNNING", "PAUSED", "STOPPING"):
                return False
            self._pause_requested = False
            self.state = "STOPPING"
            self._wake.set()
        self._publish("STOPPING")
        return True

    def pause(self) -> bool:
        with self._lock:
            if self.state != "RUNNING":
                return False
            if self._pause_requested:
                return True
            self._pause_requested = True
        self._publish("PAUSE_REQUESTED")
        return True

    def resume(self) -> bool:
        with self._lock:
            if self.state != "PAUSED":
                return False
            if self.jog_busy:
                return False
            self._pause_requested = False
            self.jog_active = False
            self.jog_busy = False
            self.state = "RUNNING"
            self._wake.set()
        return True

    def _apply_pause_barrier(self) -> bool:
        """Пауза после остановки ленты и до захвата кадров.

        Возвращает False, если симуляцию закрыли; True — можно продолжать
        инспекцию (RUNNING) или дренаж (STOPPING).
        """
        with self._lock:
            if self.state == "RUNNING" and self._pause_requested:
                self._pause_requested = False
                self.state = "PAUSED"
                # Как ProductionCycle._enter_pause_frame: пауза сразу
                # открывает ручной ход, не дожидаясь следующего poll UI.
                self.jog_active = True
                self.jog_busy = False
                entered = True
            else:
                entered = False
        if entered:
            self._publish("PAUSED")
        while True:
            if self._stop.is_set():
                return False
            with self._lock:
                current = self.state
            if current != "PAUSED":
                return current in ("RUNNING", "STOPPING")
            self._wake.wait(0.05)
            self._wake.clear()

    def close(self) -> bool:
        self._stop.set()
        self._wake.set()
        return True

    def _new_part(self) -> SimPart | None:
        """Feed an empty tray periodically, just like a real input can."""
        self.slot_counter += 1
        if self.slot_counter % 9 == 0:
            self.empty_count += 1
            return None
        part = SimPart(self.next_id)
        self.next_id += 1
        self.counts["total"] += 1
        return part

    def _line_parts(self) -> list[dict]:
        result = []
        for part in self.parts:
            result.append({"id": part.id, "position": part.position, "category": part.category,
                           "dropping": False})
        # The output body remains logically at +3 while it visibly reaches
        # the distributor. This mirrors the production status contract.
        if self.egress:
            result.append({"id": self.egress.id, "position": 3, "category": self.egress.category,
                           "dropping": True})
        return result

    def _finalize_archive_batch(self) -> None:
        """Apply the real archive shutdown policy after graceful line drain."""
        if self.archive_compressed:
            return
        archive = self.server.archive
        if archive is None:
            return
        archive.compress()
        self.archive_compressed = True

    def _run_camera_stages(self) -> bool:
        """Run the visible production chain in the same order as hardware.

        The single INSPECT stage (+0) runs CAPTURE → MODELS → PRESENCE →
        GEOMETRY → DECISION → RECORD, matching
        ``ProductionCycle._inspect_occupied_stages``.
        """
        part = next((item for item in self.parts if item.position == 0), None)
        if part is None:
            return True
        roles = self.INSPECT_STAGES

        captured_roles: list[str] = []
        for role in roles:
            captured_roles.append(role)
            self._publish("CAPTURE", [role])
            if self._stop.wait(self.CAMERA_STAGE_SECONDS):
                return False
            with self._lock:
                if self.state not in ("RUNNING", "STOPPING"):
                    return False

        self._publish("INSPECT_MODELS", list(roles))
        if self._stop.wait(self.CAMERA_STAGE_SECONDS):
            return False
        self._publish("INSPECT_PRESENCE", list(roles))
        if self._stop.wait(self.CAMERA_STAGE_SECONDS):
            return False
        self._publish("INSPECT_GEOMETRY", list(roles))
        if self._stop.wait(self.CAMERA_STAGE_SECONDS):
            return False
        with self._lock:
            self._inspect_part(part)
        self._publish("INSPECT_DECISION", list(roles))
        if self._stop.wait(self.CAMERA_STAGE_SECONDS):
            return False
        self._publish("INSPECT_RESULT_RECORDED", list(roles))
        if self._stop.wait(self.CAMERA_STAGE_SECONDS):
            return False

        self._publish("ANALYSIS_REVIEW", captured_roles)
        if self._stop.wait(self.REVIEW_SECONDS):
            return False
        self._publish("PUBLISH", captured_roles)
        if self._stop.wait(self.CAMERA_STAGE_SECONDS):
            return False
        return True

    def _prepare_distributor(self) -> None:
        part = next((item for item in self.parts if item.position == 3), None)
        category = (part.category if part else "GOOD") or "GOOD"
        self.last_distributor_action = f"ROUTE {category}"
        self.dist1_state = "MOVING_TO_GOOD" if category == "GOOD" else "MOVING_TO_DIST2"
        # Маршрут GOOD двигает только DIST1; DIST2 остаётся в текущем
        # состоянии (IDLE после homing / READY после предыдущего маршрута),
        # как в hardware/distributor.py — prepare_route(GOOD) её не трогает.
        if category in ("BAD", "CLEANUP"):
            self.dist2_state = "MOVING"
        self._planned_route = category

    def _settle_distributor(self) -> None:
        category = self._planned_route
        self.dist1_position = 0 if category == "GOOD" else 340
        self.dist2_position = 340 if category == "CLEANUP" else 0
        self.dist1_state = "GOOD" if category == "GOOD" else "TO_DIST2"
        # DIST2 получает READY только если реально ехала (BAD/CLEANUP);
        # при маршруте GOOD она остаётся IDLE/прежней — как в эталоне.
        if category in ("BAD", "CLEANUP"):
            self.dist2_state = "READY"

    def _virtual_overlay_data(self, roles: list[str], line_parts: list[dict]) -> tuple[dict, list[SimRule]]:
        """Supply valid RAW detections and RULES drawings to the real renderers."""
        category = next((item.get("category") for item in line_parts if item.get("category")), "")
        triggered = category in ("BAD", "CLEANUP")
        raw = {}
        drawings = []
        for index, role in enumerate(roles or CAMERA_ORDER):
            x = 232 + (index % 4) * 29
            bbox = [x, 345, x + 280, 525]
            raw[role] = [{"class": "bottom_glass" if category == "CLEANUP" else "windows", "bbox": bbox}]
            drawings.append({
                "role": role,
                "type": "rule_bbox",
                "bbox": bbox,
                "triggered": triggered,
                "color_hint": "glass" if category == "CLEANUP" else None,
            })
        return raw, [SimRule(drawings=drawings)]

    def _frame_analysis_report(self, kind: str, role: str | None, part_id,
                               stage: str | None, triggered: bool,
                               defects: list[str]) -> dict:
        """Общий вид отчёта о замерах кадра для панели «АНАЛИЗ КАДРА»."""
        return {
            "available": True,
            "kind": kind,
            "role": role,
            "part_id": part_id,
            "stage": stage,
            "rules": [{
                "name": "part_presence", "title": "Наличие корпуса", "triggered": False,
                "measurement_cards": [{"type": "metric", "metrics": [{"key": "simulated_confidence", "label": "Уверенность модели", "value": "0.99", "limit": "0.40", "ok": True}]}],
            }, {
                "name": "bottom_glass" if triggered else "uneven_heights",
                "title": "Симулированная проверка",
                "triggered": triggered,
                "human_cause": ", ".join(defects) if defects else "Норма",
                "measurement_cards": [{"type": "metric", "metrics": [{"key": "simulated_result", "label": "Результат правила", "value": "СРАБОТАЛО" if triggered else "НОРМА", "limit": "—", "ok": not triggered}]}],
            }],
        }

    def _frame_analysis_payload(self) -> dict:
        """Замеры текущего кадра для панели «АНАЛИЗ КАДРА».

        Как в реальном бэкенде, во время цикла панель следует за камерой,
        выбранной в UI, и показывает замеры зоны инспекции (корпус
        на +0). В ручном анализе показывается выбранная камера.
        """
        inspect_part = next((part for part in self.parts if part.position == 0), None)
        active_role = self.server.active_camera_role

        if self.selected_role is not None:
            inspection = inspect_part
            defects = inspection.defects if inspection else []
            return self._frame_analysis_report(
                kind="selected",
                role=self.selected_role,
                part_id=inspection.id if inspection else None,
                stage="ДИАГНОСТИКА",
                triggered=bool(defects),
                defects=defects,
            )

        if inspect_part is not None:
            part, role, stage = inspect_part, active_role or "LEFT", "ИНСПЕКЦИЯ +0"
        else:
            return {"available": False}

        triggered = bool(part.defects) if part.inspected else False
        defects = part.defects or []
        return self._frame_analysis_report(
            kind="production",
            role=role,
            part_id=part.id,
            stage=stage,
            triggered=triggered,
            defects=defects,
        )

    def _publish(self, phase: str, inspection_roles: list[str] | None = None) -> None:
        with self._lock:
            state = self.state
            line_parts = self._line_parts()
            active_roles = list(inspection_roles or ())
            if not active_roles:
                if any(part.position == 0 for part in self.parts):
                    active_roles = list(self.INSPECT_STAGES)
            inspection_static = phase in {
                "INITIAL_INSPECTION", "CAPTURE", "ANALYSIS", "PUBLISH", "ANALYSIS_REVIEW",
                "INSPECT_MODELS", "INSPECT_PRESENCE", "INSPECT_GEOMETRY",
                "INSPECT_DECISION", "INSPECT_RESULT_RECORDED",
            }
            capture_roles = active_roles if phase in {
                "CAPTURE", "ANALYSIS_REVIEW", "INSPECT_MODELS",
                "INSPECT_PRESENCE", "INSPECT_GEOMETRY", "INSPECT_DECISION",
                "INSPECT_RESULT_RECORDED",
            } else []
            status = {
                "state": state,
                "exit_requested": False,
                "step": self.step,
                "in_line": len(line_parts),
                "line_parts": line_parts,
                "total": self.counts["total"],
                "good": self.counts["good"],
                "rejected": self.counts["bad"],
                "cleanup": self.counts["cleanup"],
                "empty": self.empty_count,
                "dist1_state": self.dist1_state,
                "dist1_position": self.dist1_position, "dist1_max": 340,
                "dist2_state": self.dist2_state, "dist2_position": self.dist2_position, "dist2_max": 340,
                "dist2_target": "CLEANUP" if self.dist2_position else "BAD",
                "last_distributor_action": self.last_distributor_action,
                # Production start is available while JOG is open: issuing
                # start closes JOG and transfers control to the cycle.
                "controls": {"start": state in ("IDLE", "STOPPED") and self.selected_role is None,
                             "stop": state in ("RUNNING", "PAUSED"), "pause": state == "RUNNING",
                             "resume": state == "PAUSED", "exit": True,
                             "jog_hold": self.jog_active and state in JOG_ALLOWED_STATES and self.selected_role is None,
                             "selected_model_analysis": state in ("IDLE", "STOPPED") and self.selected_role is None,
                             "selected_model_release": self.selected_role is not None,
                             "distributor_diagnostic": state in ("IDLE", "STOPPED") and self.selected_role is None,
                             "camera_diagnostic": state in ("IDLE", "STOPPED") and self.selected_role is None,
                             "vision_rule_diagnostic": state in ("IDLE", "STOPPED") and self.selected_role is None},
                "process": {"phase": phase,
                            "label": PROCESS_LABELS.get(phase, phase.replace("_", " ")),
                            "step": self.step,
                            "part_id": self.egress.id if self.egress else None,
                            "capture_roles": capture_roles,
                            "inspection_roles": active_roles,
                            "conveyor": {"speed": 18000,
                                         "position": self.conveyor_position,
                                         "max": self.conveyor_max}},
                "selected_analysis": {"active": self.selected_role is not None, "role": self.selected_role},
                "diagnostic_allowed": state in ("IDLE", "STOPPED") and self.selected_role is None,
                "diagnostic_busy": False,
                "frame_analysis": self._frame_analysis_payload(),
                "live": {"running": True,
                         "static": self.selected_role is not None or inspection_static,
                         "streaming": self.selected_role is None and not inspection_static,
                         "static_roles": [self.selected_role] if self.selected_role else (active_roles if inspection_static else []),
                         "all_roles_static": False, "fps": 25},
                "jog": {"active": self.jog_active, "busy": self.jog_busy, "can_enter": state in JOG_ALLOWED_STATES,
                        "live_fps": 25},
            }
            recent = list(self.recent)
        # Publish fresh virtual camera frames on every production state change.
        # The regular UI therefore exercises its real frame versioning, RAW/
        # RULES source switching and thumbnail refresh paths.
        vision_results, rule_results = self._virtual_overlay_data(active_roles, line_parts)
        self.server.update(
            frames=demo_frames(self.step, phase, line_parts),
            vision_results=vision_results,
            rule_results=rule_results,
            line_status=status,
            recent_parts=recent,
        )

    def _inspect_part(self, part: SimPart) -> None:
        """Virtual inspection uses the same verdict vocabulary and archive flow."""
        if part.inspected:
            return
        category = self.CATEGORIES[(part.id - 1) % len(self.CATEGORIES)]
        part.category = category
        part.inspected = True
        part.defects = {
            "GOOD": [],
            "BAD": ["СИМУЛИРОВАННАЯ РАЗНОВЫСОТНОСТЬ ОКОН"],
            "CLEANUP": ["СИМУЛИРОВАННОЕ СТЕКЛО НА ДНЕ"],
        }[category]
        archive = self.server.archive
        if archive is not None:
            frames = dict(self.server.frames)
            archive.store_frames(part.id, frames, frames)

    def _finish_egress(self) -> None:
        if not self.egress:
            return
        part = self.egress
        category = part.category or "GOOD"
        self.counts[category.lower() if category != "BAD" else "bad"] += 1
        decision = "SIMULATION · " + (", ".join(part.defects or []) or "НОРМА")
        self.recent.append({"id": part.id, "category": category, "decision": decision,
                            "human_cause": ", ".join(part.defects or [])})
        archive = self.server.archive
        if archive is not None:
            archive.finalize(part.id, category, decision, part.defects or [], self.step,
                             extra={"source": "ui_simulation"})
        self.recent = self.recent[-10:]
        self.egress = None

    def _run(self) -> None:
        # Публикуем IDLE только при первом старте потока; после оживления
        # (повторный ПУСК после ВЫХОДА) состояние уже RUNNING/STOPPING и
        # не должно перетираться стартовой фазой.
        with self._lock:
            initial_state = self.state
        if initial_state == "IDLE":
            self._publish("IDLE")
        while not self._stop.is_set():
            with self._lock:
                current = self.state
            if current not in ("RUNNING", "STOPPING"):
                self._wake.wait(0.2)
                self._wake.clear()
                continue
            if current == "STOPPING" and not self.parts and self.egress is None:
                self._finalize_archive_batch()
                with self._lock:
                    self.state = "STOPPED"
                self._publish("STOPPED")
                continue

            if self._await_initial_inspection:
                # The first body is already under the cameras. Seed that
                # cell and inspect it without ROUTE_PREPARE or movement.
                with self._lock:
                    self._await_initial_inspection = False
                    arriving = self._new_part() if self.state == "RUNNING" else None
                    if arriving is not None:
                        self.parts.insert(0, arriving)
                self._publish("INITIAL_INSPECTION", list(self.INSPECT_STAGES))
                if self._stop.wait(self.POST_STOP_SECONDS):
                    break
                if not self._apply_pause_barrier():
                    continue
                if not self._run_camera_stages():
                    continue
                continue

            # Set the distributor first, then perform one synchronous step.
            # This mirrors the physical route preparation before the belt moves.
            with self._lock:
                self._prepare_distributor()
            self._publish("ROUTE_PREPARE")
            if self._stop.wait(self.ROUTE_PREPARE_SECONDS):
                break
            with self._lock:
                self._settle_distributor()
            # Horizontal step: every currently visible part advances together.
            self._publish("MOTION")
            if self._stop.wait(self.STEP_SECONDS):
                break
            with self._lock:
                if self.state not in ("RUNNING", "STOPPING"):
                    continue
                self.egress = next((p for p in self.parts if p.position == 3), None)
                self.parts = [p for p in self.parts if p is not self.egress]
                for part in self.parts:
                    part.position += 1
                self.step += 1
            self._publish("SETTLE")
            if self._stop.wait(self.SETTLE_SECONDS):
                break
            with self._lock:
                if self.state not in ("RUNNING", "STOPPING"):
                    continue
                self._finish_egress()          # falling from +4 starts now
                # A requested stop drains the existing queue without adding
                # another body at +0.
                arriving = self._new_part() if self.state == "RUNNING" else None
                if arriving is not None:
                    self.parts.insert(0, arriving)
            self._publish("SETTLE")
            # New body falls into +0 and the output body passes the
            # distributor at +4 while the conveyor is stopped. Only after
            # that can capture own cameras.
            if self._stop.wait(self.POST_STOP_SECONDS):
                break
            if not self._apply_pause_barrier():
                continue
            if not self._run_camera_stages():
                continue


def configure_simulated_thresholds(server: UIServer) -> None:
    """Expose the real threshold editor without changing its source file.

    The simulator deliberately keeps edits in memory: a user can verify
    every control and its lock state without accidentally modifying production
    calibration values in ``thresholds.json``.
    """
    loader = ThresholdLoader("thresholds.json")
    server.thresholds = dict(loader.thresholds)
    server.threshold_labels = dict(loader.labels)
    server.thresholds_revision = 1

    def apply(role: str, values: dict, _labels: dict) -> dict:
        prefix = f"{role}."
        updated = dict(server.thresholds or {})
        for key, value in values.items():
            full_key = key if str(key).startswith(prefix) else prefix + str(key)
            if full_key not in updated:
                raise ValueError(f"Неизвестный порог: {key}")
            updated[full_key] = value
        # ``UIServer.apply_thresholds`` stores labels and increments revision.
        return updated

    server.on_thresholds_apply = apply


# Виртуальные кадры — в production-разрешении 1280×720 (16:9). Совпадение
# пропорции с реальными камерами убирает letterboxing (чёрные полосы по
# бокам) в главном окне HMI.
FRAME_WIDTH = 1280
FRAME_HEIGHT = 720


def demo_frames(step: int = 0, phase: str = "IDLE", line_parts: list[dict] | None = None) -> dict:
    """Generate changing camera feeds rather than static placeholder images."""
    frames = {}
    line_parts = line_parts or []
    moving_x = 168 + (step % 7) * 122
    for index, role in enumerate(CAMERA_ORDER):
        frame = np.zeros((FRAME_HEIGHT, FRAME_WIDTH, 3), dtype=np.uint8)
        frame[:] = (20 + index * 3, 27 + index * 2, 33 + index * 2)
        cv2.putText(frame, "UI SIMULATION · VIRTUAL CAMERA", (67, 81), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (105, 205, 170), 3)
        cv2.putText(frame, role, (67, 135), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (205, 214, 224), 3)
        cv2.putText(frame, f"STEP {step:04d} · {phase}", (67, 183), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (142, 165, 185), 2)
        cv2.rectangle(frame, (67, 232), (1213, 630), (72, 94, 110), 3)
        # A moving virtual case lets the preview strip and main viewport show
        # actual image updates during every conveyor step.
        category = next((p.get("category") for p in line_parts if p.get("category")), "")
        color = {"GOOD": (80, 205, 105), "BAD": (85, 85, 225), "CLEANUP": (70, 190, 230)}.get(category, (110, 125, 135))
        x = moving_x + (index % 3) * 19
        cv2.rectangle(frame, (x, 368), (x + 264, 510), color, -1)
        cv2.rectangle(frame, (x, 368), (x + 264, 510), (220, 230, 235), 3)
        cv2.putText(frame, "VIRTUAL PART", (x + 29, 447), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (15, 20, 24), 2)
        frames[role] = frame
    return frames


def main() -> None:
    parser = argparse.ArgumentParser(description="Hardware-free Conveyor Three UI simulator")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (0.0.0.0 for Arena preview)")
    parser.add_argument("--port", default=8000, type=int)
    args = parser.parse_args()

    server = UIServer()
    # The real archive implementation writes only into ignored sandbox data.
    # Its settings dialog and validation therefore behave exactly as in the app.
    server.archive = PartArchive(root_folder="archive/ui_simulation")
    simulation = LineSimulation(server)
    server.on_start = simulation.start
    server.on_stop = simulation.stop
    server.on_pause = simulation.pause
    server.on_resume = simulation.resume
    server.on_exit = simulation.close
    server.on_jog_enter = simulation.enter_jog
    server.on_jog_exit = simulation.exit_jog
    server.on_jog_hold_start = simulation.jog_hold_start
    server.on_jog_hold_heartbeat = simulation.jog_hold_heartbeat
    server.on_jog_hold_release = simulation.jog_hold_release
    server.on_distributor_diagnostic = simulation.distributor_diagnostic
    server.on_camera_diagnostic = simulation.camera_diagnostic
    server.on_vision_rule_diagnostic = simulation.vision_rule_diagnostic
    server.on_selected_model_analysis = simulation.selected_analysis
    server.on_selected_model_release = simulation.release_selected_analysis
    configure_simulated_thresholds(server)
    server.update(frames=demo_frames())
    # Имитируем camera_mapping.json: роль -> физический Camera ID. Маппинг
    # отдаётся в /api/cameras как в production (в названиях камер в UI
    # Camera ID не показывается).
    server.set_camera_roles({
        role: index for index, role in enumerate(CAMERA_ORDER)
    })
    server.set_active_camera_role(CAMERA_ORDER[0])
    for key, _ in server.BOOT_STEPS:
        server.boot_step_done(key, "Симулятор UI готов")
    server.boot_complete()
    simulation.thread.start()
    server.start_server(host=args.host, port=args.port)
    print(f"[SIMULATION] Open http://{args.host}:{args.port}; press ПУСК to begin")

    stopping = threading.Event()
    def stop_signal(*_args):
        stopping.set()
        simulation.close()
    signal.signal(signal.SIGINT, stop_signal)
    signal.signal(signal.SIGTERM, stop_signal)
    try:
        while not stopping.wait(0.5):
            pass
    finally:
        simulation.close()
        server.stop_server()


if __name__ == "__main__":
    main()
