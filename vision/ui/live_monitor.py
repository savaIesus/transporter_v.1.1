from vision.ui.server.server import UIServer


class LiveMonitorApi:
    """Небольшой native bridge для действий, недоступных обычному браузеру."""

    def __init__(self, monitor):
        self.monitor = monitor

    def choose_archive_folder(self):
        """Открыть системный диалог выбора папки в pywebview."""
        window = self.monitor._webview_window
        if window is None:
            return {"ok": False, "error": "Окно интерфейса ещё не готово"}
        try:
            import webview
            selected = window.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if not selected:
            return {"ok": False, "cancelled": True}
        return {"ok": True, "path": str(selected[0])}


class LiveMonitor:
    """
    Фасад UI: FastAPI сервер + pywebview окно.
    """

    def __init__(
        self,
        window_name: str = "РОБОТЕХНИЧЕСКИЙ КОМПЛЕКС КОНВЕЙЕРНОГО ТИПА 3",
        host: str = "127.0.0.1",
        port: int = 8000,
        fullscreen: bool = True,
        start_callback=None,
        stop_callback=None,
        exit_callback=None,
    ):
        self.window_name = window_name
        self.host = host
        self.port = port
        self.fullscreen = fullscreen

        self.start_callback  = start_callback
        self.stop_callback   = stop_callback
        self.pause_callback  = None
        self.resume_callback = None
        self.exit_callback   = exit_callback
        self.distributor_diagnostic_callback = None
        self.camera_diagnostic_callback = None
        self.vision_rule_diagnostic_callback = None
        self.selected_model_analysis_callback = None
        self.selected_model_release_callback = None
        self.active_camera_callback = None

        # JOG callbacks
        self.jog_enter_callback = None
        self.jog_exit_callback = None
        self.jog_hold_start_callback = None
        self.jog_hold_heartbeat_callback = None
        self.jog_hold_release_callback = None

        # Пороги правил: callback получает (role, values) и возвращает
        # обновлённый плоский dict порогов (или бросает исключение).
        self.thresholds_apply_callback = None
        # Callback автоподхвата: получает свежий dict порогов из файла,
        # может пересоздать DecisionEngine и вернуть итоговый dict.
        self.thresholds_reload_callback = None

        self.server = UIServer()
        self._bind_server_callbacks()

        self._webview_window = None
        self.webview_api = LiveMonitorApi(self)
        self._close_requested = False

    # Public API

    def set_splash_status(self, text: str):
        self.server.set_splash_status(text)

    def set_camera_roles(self, roles):
        self.server.set_camera_roles(roles)

    def boot_step_start(self, key: str, message: str | None = None):
        self.server.boot_step_start(key, message)

    def boot_step_done(self, key: str, message: str | None = None):
        self.server.boot_step_done(key, message)

    def boot_step_error(self, key: str, message: str):
        self.server.boot_step_error(key, message)

    def boot_complete(self):
        self.server.boot_complete()

    def update(self, **kwargs):
        self.server.update(**kwargs)

    def close_window(self):
        """Закрыть webview окно. Безопасно вызывать повторно."""
        if self._webview_window is None:
            return
        if self._close_requested:
            return

        self._close_requested = True
        try:
            self._webview_window.destroy()
        except Exception as e:
            print(f"[UI] window close error: {e}")

    def stop_server(self, timeout: float = 3.0):
        """Остановить uvicorn сервер."""
        try:
            self.server.stop_server()
        except Exception as exc:
            print(f"[UI] Ошибка остановки сервера: {exc}")

        server_thread = self.server.get_server_thread()
        if server_thread and server_thread.is_alive():
            server_thread.join(timeout=timeout)
            if server_thread.is_alive():
                print("[UI] Server thread did not stop")

    # Internal

    def _bind_server_callbacks(self):
        self.server.on_start = (
            lambda: self._invoke(self.start_callback)
        )
        self.server.on_stop = (
            lambda: self._invoke(self.stop_callback)
        )
        self.server.on_pause = (
            lambda: self._invoke(self.pause_callback)
        )
        self.server.on_resume = (
            lambda: self._invoke(self.resume_callback)
        )
        self.server.on_exit = (
            lambda: self._invoke(self.exit_callback)
        )
        self.server.on_distributor_diagnostic = (
            lambda command: self._invoke_args(
                self.distributor_diagnostic_callback, command,
            )
        )
        self.server.on_camera_diagnostic = (
            lambda: self._invoke(self.camera_diagnostic_callback)
        )
        self.server.on_vision_rule_diagnostic = (
            lambda: self._invoke(self.vision_rule_diagnostic_callback)
        )
        self.server.on_selected_model_analysis = (
            lambda role: self._invoke_args(
                self.selected_model_analysis_callback, role,
            )
        )
        self.server.on_selected_model_release = (
            lambda: self._invoke(self.selected_model_release_callback)
        )
        self.server.on_active_camera_changed = (
            lambda role: self._invoke_args(
                self.active_camera_callback, role,
            )
        )

        # JOG
        self.server.on_jog_enter = (
            lambda: self._invoke(self.jog_enter_callback)
        )
        self.server.on_jog_exit = (
            lambda: self._invoke(self.jog_exit_callback)
        )
        self.server.on_jog_hold_start = (
            lambda direction: self._invoke_args(
                self.jog_hold_start_callback, direction,
            )
        )
        self.server.on_jog_hold_heartbeat = (
            lambda direction: self._invoke_args(
                self.jog_hold_heartbeat_callback, direction,
            )
        )
        self.server.on_jog_hold_release = (
            lambda reason="button released": self._invoke_args(
                self.jog_hold_release_callback, reason,
            )
        )
        self.server.on_thresholds_apply = (
            lambda role, values, labels: self._invoke_args(
                self.thresholds_apply_callback, role, values, labels,
            )
        )
        self.server.on_thresholds_reload = (
            lambda fresh: self._invoke_args(
                self.thresholds_reload_callback, fresh,
            )
        )

    def _invoke(self, cb):
        if cb is None:
            return False
        return cb()

    def _invoke_args(self, cb, *args, **kwargs):
        if cb is None:
            return False
        return cb(*args, **kwargs)
