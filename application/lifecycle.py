"""Верхнеуровневая оркестрация владельцев жизненного цикла."""

from __future__ import annotations

import threading


class ProductionApplication:
    """Связывает startup, UI и shutdown, не создавая их зависимости."""

    def __init__(
        self,
        runtime,
        initializer,
        exit_coordinator,
        desktop_ui,
        shutdown_manager,
        *,
        thread_factory=threading.Thread,
    ):
        self.runtime = runtime
        self.initializer = initializer
        self.exit_coordinator = exit_coordinator
        self.desktop_ui = desktop_ui
        self.shutdown_manager = shutdown_manager
        self._thread_factory = thread_factory

    def run(self) -> None:
        monitor = self.runtime.monitor
        # Даже частично запущенный UI-сервер должен быть остановлен, если
        # start_server, сборка callbacks, startup-поток или окно дадут сбой.
        try:
            monitor.server.start_server(
                host=monitor.host,
                port=monitor.port,
            )
            self.exit_coordinator.bind()

            init_thread = self._thread_factory(
                target=self.initializer.run,
                daemon=True,
            )
            self.runtime.init_thread = init_thread
            init_thread.start()

            self.desktop_ui.install_signal_handler(
                self.exit_coordinator.request_exit
            )
            self.desktop_ui.print_startup_help()
            # pywebview блокирует текущий поток до закрытия окна.
            self.desktop_ui.run()
            self.shutdown_manager.after_window_closed()
        finally:
            self.shutdown_manager.shutdown()
