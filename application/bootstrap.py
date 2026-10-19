"""Composition root и публичный запуск production-приложения."""

from __future__ import annotations

import os
import threading

from application.callbacks import ExitCoordinator
from application.console import show_console
from application.factory import ProductionSystemFactory
from application.lifecycle import ProductionApplication
from application.runtime import RuntimeState
from application.shutdown import ShutdownManager
from application.startup import SystemInitializer
from application.ui import DesktopUI
from json_sender import scan_and_send_all_batches, sync_local_queue
from vision.camera_calibration_console import launch_camera_calibrator
from vision.ui import LiveMonitor


CAMERA_MAPPING_PATH = "camera_mapping.json"


def ensure_camera_mapping(path: str = CAMERA_MAPPING_PATH) -> bool:
    if os.path.exists(path):
        return True
    if launch_camera_calibrator(path):
        return True
    print(
        f"[STARTUP] {path} не создан; "
        "основное приложение не запускается"
    )
    return False


def create_application() -> ProductionApplication:
    """Собирает владельцев приложения и их production-зависимости."""

    monitor = LiveMonitor(
        start_callback=None,
        stop_callback=None,
        exit_callback=None,
        fullscreen=True,
    )
    runtime = RuntimeState(monitor=monitor)
    factory = ProductionSystemFactory()
    exit_coordinator = ExitCoordinator(runtime)
    initializer = SystemInitializer(runtime, factory, exit_coordinator)
    desktop_ui = DesktopUI(monitor)
    shutdown_manager = ShutdownManager(
        runtime,
        batch_sender=scan_and_send_all_batches,
    )
    return ProductionApplication(
        runtime=runtime,
        initializer=initializer,
        exit_coordinator=exit_coordinator,
        desktop_ui=desktop_ui,
        shutdown_manager=shutdown_manager,
    )


def start_json_queue_syncer(thread_factory=threading.Thread) -> None:
    """Запустить фоновую догрузку очереди batch.json на ПК аналитика.

    Очередь копируется, пока ПК аналитика выключен; догрузка при каждом
    старте программы гарантирует, что партии не теряются. Поток
    демонический: недоступный сетевой диск не блокирует запуск линии.
    """
    def _sync() -> None:
        try:
            sync_local_queue()
        except Exception as exc:
            print(f"[STARTUP] Очередь batch.json не синхронизирована: {exc}")

    worker = thread_factory(target=_sync, daemon=True)
    worker.start()


def run_application() -> None:
    if not ensure_camera_mapping():
        return
    start_json_queue_syncer()
    try:
        create_application().run()
    except BaseException:
        # Аварийный выход: окно HMI закрылось, и консоль могла быть уже
        # скрыта. Вернуть её, иначе run.bat покажет сообщение об ошибке и
        # паузу в невидимом окне, и оператор ничего не узнает.
        show_console()
        raise
