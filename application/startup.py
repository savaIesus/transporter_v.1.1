"""Последовательность фоновой инициализации production-системы."""

from __future__ import annotations

import os
import threading
import time
import traceback
from typing import Callable

from application.callbacks import (
    bind_cycle_callbacks,
    bind_inspection_callbacks,
)
from application.status import make_idle_status


_FAILED = object()
INITIAL_CAMERA_FRAMES_TIMEOUT = 20.0


class StartupDisplayError(RuntimeError):
    """Ошибка, текст которой уже подготовлен для splash-экрана."""


class SystemInitializer:
    """Выполняет восемь startup-этапов и публикует их состояние в HMI."""

    def __init__(
        self,
        runtime,
        factory,
        exit_coordinator,
        *,
        thread_factory=threading.Thread,
        sleep: Callable[[float], None] = time.sleep,
        initial_camera_frames_timeout: float = INITIAL_CAMERA_FRAMES_TIMEOUT,
    ):
        self.runtime = runtime
        self.factory = factory
        self.exit_coordinator = exit_coordinator
        self._thread_factory = thread_factory
        self._sleep = sleep
        self.initial_camera_frames_timeout = max(
            0.0, float(initial_camera_frames_timeout)
        )

    @property
    def monitor(self):
        return self.runtime.monitor

    def run(self) -> None:
        try:
            self._ensure_active()
            calibration = self.factory.load_calibration()
            self._ensure_active()

            cameras = self._run_step(
                "cameras",
                "Открытие камер",
                self._initialize_cameras,
                done=lambda value: f"Открыто камер: {len(value.cameras)}",
                error=lambda exc: f"Ошибка камеры: {exc}",
                on_error=self._log_camera_error,
            )
            if cameras is _FAILED:
                return

            vision = self._run_step(
                "models_load",
                "Загрузка моделей",
                self.factory.create_vision,
                done=lambda value: f"Загружено моделей: {len(value.models)}",
                error=lambda exc: f"Ошибка загрузки моделей: {exc}",
            )
            if vision is _FAILED:
                return

            warmed = self._run_step(
                "models_warm",
                "Прогрев моделей",
                lambda: self._warmup(vision),
                done="Прогрев завершён",
                error=lambda exc: f"Ошибка прогрева моделей: {exc}",
            )
            if warmed is _FAILED:
                return

            inspection = self._run_step(
                "inspection",
                "Настройка системы контроля",
                lambda: self._initialize_inspection(vision),
                done=lambda value: (
                    f"Настроено правил: {len(value.decision.rules)}"
                ),
                error=lambda exc: f"Ошибка настройки контроля: {exc}",
            )
            if inspection is _FAILED:
                return

            serial_info = self._run_step(
                "serial",
                "Поиск контроллера",
                self._initialize_serial,
                done=lambda value: (
                    f"Контроллер: {value[0]} @ {value[1]}"
                ),
                error=self._serial_error,
            )
            if serial_info is _FAILED:
                return

            hardware = self._run_step(
                "hardware",
                "Инициализация оборудования",
                lambda: self.factory.create_hardware(
                    self.runtime.transport,
                    calibration,
                    self.runtime.shutdown_requested.is_set,
                ),
                done="Лента и две оси инициализированы",
                error=lambda exc: f"Ошибка оборудования: {exc}",
            )
            if hardware is _FAILED:
                return

            cycle = self._run_step(
                "cycle",
                "Создание производственного цикла",
                lambda: self._initialize_cycle(
                    cameras=cameras,
                    inspection=inspection,
                    hardware=hardware,
                    calibration=calibration,
                ),
                error=lambda exc: f"Ошибка создания цикла: {exc}",
            )
            if cycle is _FAILED:
                return

            self.monitor.update(
                vision_results={},
                rule_results=[],
                line_status=make_idle_status(hardware.distributor),
                recent_parts=[],
            )
            self._ensure_active()

            ready = self._run_step(
                "ready",
                "Запуск системы",
                lambda: self._start_cycle(cycle),
                done="Система готова к работе",
                error=str,
            )
            if ready is _FAILED:
                return

            self._sleep(0.6)
            self.monitor.boot_complete()
        except Exception as exc:
            traceback.print_exc()
            current = self.monitor.server.boot_current or "init"
            self.monitor.boot_step_error(current, str(exc))
            self._report_failure()

    def _run_step(
        self,
        key: str,
        label: str,
        action: Callable[[], object],
        *,
        done=None,
        error: Callable[[Exception], str],
        on_error: Callable[[Exception], None] | None = None,
    ):
        self.monitor.boot_step_start(key, label)
        try:
            result = action()
        except Exception as exc:
            if on_error is not None:
                on_error(exc)
            self.monitor.boot_step_error(key, error(exc))
            self._report_failure()
            return _FAILED

        message = done(result) if callable(done) else done
        self.monitor.boot_step_done(key, message)
        self._ensure_active()
        return result

    def _initialize_cameras(self):
        cameras = self.factory.create_cameras()
        # Публикуем ресурс сразу: shutdown сможет освободить камеры даже если
        # следующий UI-вызов завершится ошибкой.
        self.runtime.cameras = cameras
        self.monitor.set_camera_roles(cameras.mapping)
        return cameras

    @staticmethod
    def _log_camera_error(exc: Exception) -> None:
        print(
            f"[CAMERA] Ошибка инициализации: "
            f"{type(exc).__name__}: {exc}"
        )

    @staticmethod
    def _warmup(vision):
        vision.warmup()
        return vision

    def _initialize_inspection(self, vision):
        services = self.factory.create_inspection(vision)
        self.runtime.archive = services.archive
        bind_inspection_callbacks(self.runtime, services)
        return services

    def _initialize_serial(self) -> tuple[str, int]:
        serial_baud = int(os.environ.get("SERIAL_BAUD", "115200"))
        preferred_port = os.environ.get("SERIAL_PORT")
        found_port, port_message = self.factory.discover_controller(
            baudrate=serial_baud,
            preferred_port=preferred_port,
        )
        if found_port is None:
            raise StartupDisplayError(port_message)

        transport = self.factory.create_transport(
            port=found_port,
            baudrate=serial_baud,
        )
        self.runtime.transport = transport
        # Любая конфигурация начинается с остановленного контроллера.
        transport.send("G1")
        transport.send("G25")
        return found_port, serial_baud

    @staticmethod
    def _serial_error(exc: Exception) -> str:
        if isinstance(exc, StartupDisplayError):
            return str(exc)
        return f"Ошибка последовательного порта: {exc}"

    def _initialize_cycle(
        self,
        *,
        cameras,
        inspection,
        hardware,
        calibration: dict,
    ):
        self._ensure_active()
        print("[HARDWARE] Homing distributor axes...")
        hardware.distributor.initialize()
        self._ensure_active()

        cycle = self.factory.create_cycle(
            hardware=hardware,
            cameras=cameras,
            inspector=inspection.inspector,
            monitor=self.monitor,
            archive=inspection.archive,
            calibration=calibration,
        )
        self.runtime.cycle = cycle
        bind_cycle_callbacks(
            self.monitor,
            cycle,
            self.exit_coordinator.request_exit,
        )
        return cycle

    def _start_cycle(self, cycle):
        cycle_thread = self._thread_factory(
            target=cycle.start,
            daemon=True,
        )
        self.runtime.cycle_thread = cycle_thread
        cycle_thread.start()

        # В IDLE интерфейс всё равно автоматически входит в JOG для live-вида.
        # Делаем это на backend до снятия splash, чтобы готовность означала не
        # только три открытых VideoCapture, но и первый корректный кадр от
        # каждой назначенной роли.
        if not cycle.enter_jog():
            raise RuntimeError("Не удалось запустить стартовый просмотр камер")
        camera_roles = tuple(self.runtime.cameras.mapping)
        missing = cycle.live.wait_for_roles(
            camera_roles,
            timeout=self.initial_camera_frames_timeout,
        )
        if missing:
            detail = cycle.live.error
            message = "Нет первого кадра: " + ", ".join(missing)
            if detail:
                message += f"; {detail}"
            raise RuntimeError(message)

        # На этапе ready отмена проверяется до отметки «Система готова».
        self._ensure_active()
        return cycle_thread

    def _ensure_active(self) -> None:
        if self.runtime.shutdown_requested.is_set():
            raise RuntimeError("initialization cancelled by user")

    @staticmethod
    def _report_failure() -> None:
        """Оставить startup-ошибку на splash до закрытия окна."""
        print("[INIT] Startup failed; waiting to close the UI")
