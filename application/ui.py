"""Запуск блокирующего pywebview и консольные UI-хуки."""

from __future__ import annotations

import signal


class DesktopUI:
    def __init__(self, monitor, *, webview_module=None):
        if webview_module is None:
            # Проверяем обязательную UI-зависимость до запуска startup-потока:
            # при отсутствующем pywebview нельзя начинать homing оборудования.
            import webview

            webview_module = webview

        self.monitor = monitor
        self._webview = webview_module

    def install_signal_handler(self, exit_callback) -> None:
        def signal_handler(_signum, _frame):
            print("\n[SIGINT] Ctrl+C -> запрос выхода")
            exit_callback()

        signal.signal(signal.SIGINT, signal_handler)

    @staticmethod
    def print_startup_help() -> None:
        print("=" * 60)
        print("Система запускается.")
        print("  F5 ПУСК | F6 СТОП | TAB вид")
        print(
            "  ESC ВЫХОД "
            "(1× штатная остановка, 2× принудительный выход)"
        )
        print("=" * 60)

    def run(self) -> None:
        window = self._webview.create_window(
            title=self.monitor.window_name,
            url=f"http://{self.monitor.host}:{self.monitor.port}/",
            fullscreen=self.monitor.fullscreen,
            background_color="#0b0f13",
            js_api=self.monitor.webview_api,
        )
        self.monitor._webview_window = window
        self._webview.start()
