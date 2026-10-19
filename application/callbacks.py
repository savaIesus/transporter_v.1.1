"""Привязка UI-callbacks и координация EXIT."""

from __future__ import annotations

import threading
import time
from typing import Callable

from application.constants import (
    CYCLE_JOIN_TIMEOUT,
    GRACEFUL_EXIT_TIMEOUT,
)


class ExitCoordinator:
    """Обрабатывает первый (штатный) и повторный (форсированный) EXIT."""

    def __init__(
        self,
        runtime,
        *,
        cycle_join_timeout: float = CYCLE_JOIN_TIMEOUT,
        graceful_exit_timeout: float = GRACEFUL_EXIT_TIMEOUT,
        thread_factory=threading.Thread,
    ):
        self.runtime = runtime
        self.cycle_join_timeout = cycle_join_timeout
        self.graceful_exit_timeout = graceful_exit_timeout
        self._thread_factory = thread_factory
        self._press_count = 0
        self._lock = threading.Lock()

    def bind(self) -> None:
        # EXIT должен работать и до создания ProductionCycle.
        self.runtime.monitor.exit_callback = self.request_exit

    def request_exit(self) -> None:
        runtime = self.runtime
        runtime.shutdown_requested.set()
        self._stop_partially_initialized_hardware()

        with self._lock:
            self._press_count += 1
            count = self._press_count

        cycle = runtime.cycle
        force = count > 1 or bool(cycle and cycle.state == "FAULT")
        if force:
            print("[EXIT] Force exit")
            if cycle:
                try:
                    cycle.request_force_exit()
                except Exception as exc:
                    print(f"[EXIT] Force-exit request failed: {exc}")
                    self._send_controller_stop("Fallback stop")
        else:
            print("[EXIT] Штатная остановка -> завершение деталей на линии")
            if cycle:
                try:
                    cycle.request_exit()
                except Exception as exc:
                    print(f"[EXIT] Graceful-exit request failed: {exc}")

        # Ошибка callback цикла не должна оставить HMI навсегда открытым.
        self._schedule_close(force=force)

    def _stop_partially_initialized_hardware(self) -> None:
        runtime = self.runtime
        if runtime.cycle is not None or runtime.transport is None:
            return
        self._send_controller_stop("Startup stop")

    def _send_controller_stop(self, label: str) -> None:
        transport = self.runtime.transport
        if transport is None:
            return
        try:
            transport.send("G1")
            transport.send("G25")
        except Exception as exc:
            print(f"[EXIT] {label} failed: {exc}")

    def _schedule_close(self, *, force: bool) -> None:
        def wait_and_close():
            started = time.monotonic()
            cycle_thread = self.runtime.cycle_thread
            if cycle_thread and cycle_thread.is_alive():
                timeout = (
                    self.cycle_join_timeout
                    if force
                    else self.graceful_exit_timeout
                )
                cycle_thread.join(timeout=timeout)
                waited = time.monotonic() - started
                print(f"[EXIT] Ожидание цикла: {waited:.2f} с")
                if cycle_thread.is_alive() and not force:
                    print(
                        "[EXIT] Линия ещё выполняет штатную остановку; "
                        "окно остаётся открытым. Нажмите ВЫХОД второй раз "
                        "для принудительного завершения."
                    )
                    return
            self.runtime.monitor.close_window()

        try:
            self._thread_factory(
                target=wait_and_close,
                daemon=True,
            ).start()
        except Exception as exc:
            print(f"[EXIT] Не удалось запустить ожидание закрытия: {exc}")
            cycle_thread = self.runtime.cycle_thread
            cycle_alive = bool(cycle_thread and cycle_thread.is_alive())
            # Штатный выход не должен обрывать ещё работающий цикл. Для
            # force-exit или уже завершённого цикла закрываем окно напрямую.
            if force or not cycle_alive:
                self.runtime.monitor.close_window()


class ThresholdCallbacks:
    """Callbacks редактора порогов, отделённые от startup-пайплайна."""

    def __init__(
        self,
        runtime,
        inspector,
        *,
        threshold_store=None,
        decision_factory=None,
        path: str = "thresholds.json",
    ):
        self.runtime = runtime
        self.inspector = inspector
        self.threshold_store = threshold_store
        self.decision_factory = decision_factory
        self.path = path

    def reload_from_file(self, fresh: dict) -> dict:
        if self.inspector is None:
            raise RuntimeError("Система контроля ещё не инициализирована")
        decision_factory = self._get_decision_factory()
        self.inspector.decision = decision_factory(thresholds=fresh)
        print(
            "[THRESHOLDS] Пороги перечитаны из thresholds.json; "
            "правила пересозданы"
        )
        return fresh

    def apply(self, role: str, values: dict, labels: dict | None) -> dict:
        cycle = self.runtime.cycle
        if cycle is None or self.inspector is None:
            raise RuntimeError("Система контроля ещё не инициализирована")
        if cycle.state not in ("IDLE", "STOPPED"):
            raise RuntimeError(
                "Изменение порогов доступно только до пуска "
                "и после полной остановки"
            )
        if cycle.jog is not None and cycle.jog.status.get("busy"):
            raise RuntimeError(
                "Нельзя менять пороги во время движения ленты"
            )
        if not isinstance(values, dict) or not values:
            raise ValueError("Нет изменённых порогов")

        updated = dict(self.inspector.decision.thresholds)
        changed = []
        for key, value in values.items():
            full_key = self._role_key(role, key)
            if full_key not in updated:
                raise ValueError(f"Неизвестный порог: {full_key}")
            updated[full_key] = value
            changed.append(full_key)

        # Выполняется полная валидация, как при загрузке файла.
        threshold_store = self._get_threshold_store()
        threshold_store.validate(updated)
        full_labels = dict(
            self.runtime.monitor.server.threshold_labels or {}
        )
        for key, name in (labels or {}).items():
            full_key = self._role_key(role, key)
            if name is None or not str(name).strip():
                full_labels.pop(full_key, None)
            else:
                full_labels[full_key] = str(name).strip()

        threshold_store.save_file(
            self.path,
            updated,
            labels=full_labels,
        )
        decision_factory = self._get_decision_factory()
        self.inspector.decision = decision_factory(thresholds=updated)
        print(
            "[THRESHOLDS] Применено "
            f"{len(changed)} изменение(й) для {role}: "
            f"{', '.join(sorted(changed))}"
        )
        return updated

    def _get_threshold_store(self):
        if self.threshold_store is None:
            from domain.threshold_loader import ThresholdLoader

            self.threshold_store = ThresholdLoader
        return self.threshold_store

    def _get_decision_factory(self):
        if self.decision_factory is None:
            from core.decision_engine import DecisionEngine

            self.decision_factory = DecisionEngine
        return self.decision_factory

    @staticmethod
    def _role_key(role: str, key) -> str:
        text = str(key)
        return text if text.startswith(f"{role}.") else f"{role}.{text}"


def bind_inspection_callbacks(runtime, services) -> ThresholdCallbacks:
    """Публикует архив/пороги в UI и возвращает владельца callbacks."""

    monitor = runtime.monitor
    monitor.server.archive = services.archive
    monitor.server.archive_config_path = "archive_config.json"
    monitor.server.thresholds = dict(services.thresholds)
    monitor.server.threshold_labels = dict(
        services.threshold_loader.labels or {}
    )
    monitor.server.thresholds_path = "thresholds.json"

    callbacks = ThresholdCallbacks(runtime, services.inspector)
    monitor.thresholds_reload_callback = callbacks.reload_from_file
    monitor.thresholds_apply_callback = callbacks.apply
    # Сильная ссылка делает время жизни владельца явным даже для UI-фасадов,
    # которые могут оборачивать присвоенные callables.
    runtime.threshold_callbacks = callbacks
    return callbacks


def bind_cycle_callbacks(
    monitor,
    cycle,
    exit_callback: Callable[[], None],
) -> None:
    """Подключает публичный API ProductionCycle к HMI."""

    monitor.start_callback = cycle.request_start
    monitor.stop_callback = cycle.request_stop
    monitor.pause_callback = cycle.request_pause
    monitor.resume_callback = cycle.request_resume
    monitor.exit_callback = exit_callback
    monitor.distributor_diagnostic_callback = cycle.distributor_diagnostic
    monitor.camera_diagnostic_callback = cycle.diagnostic_check_cameras
    monitor.vision_rule_diagnostic_callback = (
        cycle.diagnostic_check_vision_rules
    )
    monitor.selected_model_analysis_callback = (
        cycle.diagnostic_analyze_selected_camera
    )
    monitor.selected_model_release_callback = (
        cycle.diagnostic_release_selected_camera
    )
    monitor.active_camera_callback = lambda _role: cycle._refresh_monitor()

    monitor.jog_enter_callback = cycle.enter_jog
    monitor.jog_exit_callback = cycle.exit_jog
    monitor.jog_hold_start_callback = cycle.jog_hold_start
    monitor.jog_hold_heartbeat_callback = cycle.jog_hold_heartbeat
    monitor.jog_hold_release_callback = cycle.jog_hold_release
