"""Детерминированное завершение потоков и внешних ресурсов."""

from __future__ import annotations

import threading
import time

from application.constants import (
    COMPRESS_TIMEOUT,
    CYCLE_JOIN_TIMEOUT,
    INIT_JOIN_TIMEOUT,
)
from application.console import hide_console
from json_sender import scan_and_send_all_batches


class ShutdownManager:
    def __init__(
        self,
        runtime,
        *,
        cycle_join_timeout: float = CYCLE_JOIN_TIMEOUT,
        init_join_timeout: float = INIT_JOIN_TIMEOUT,
        compress_timeout: float = COMPRESS_TIMEOUT,
        thread_factory=threading.Thread,
        batch_sender=scan_and_send_all_batches,
        console_hider=hide_console,
    ):
        self.runtime = runtime
        self.cycle_join_timeout = cycle_join_timeout
        self.init_join_timeout = init_join_timeout
        self.compress_timeout = compress_timeout
        self._thread_factory = thread_factory
        self.batch_sender = batch_sender
        self._console_hider = console_hider

    def after_window_closed(self) -> None:
        """Остановить цикл сразу после возврата из блокирующего webview."""

        print("[UI] Окно закрыто, завершение...")
        self._hide_console()
        self._request_force_exit()
        self._join_cycle(warn=True)

    def _hide_console(self) -> None:
        # Дальнейшая очистка ресурсов оператору не видна и не нужна: консоль
        # скрывается сразу, чтобы «чёрное окно» не висело до конца shutdown.
        # Ошибка скрытия не должна мешать детерминированному завершению.
        try:
            self._console_hider()
        except Exception as exc:
            print(f"[SHUTDOWN] Не удалось скрыть консоль: {exc}")

    def shutdown(self) -> None:
        runtime = self.runtime
        shutdown_started = time.monotonic()
        print("[SHUTDOWN] Завершение...")
        runtime.shutdown_requested.set()
        self._stop_partially_initialized_hardware()
        self._join_initializer()

        self._request_force_exit()
        self._join_cycle()

        phase_started = time.monotonic()
        try:
            runtime.monitor.stop_server()
        except Exception as exc:
            print(f"[SHUTDOWN] UI server stop failed: {exc}")
        print(
            f"[SHUTDOWN] Остановка UI-сервера: "
            f"{time.monotonic() - phase_started:.2f} с"
        )

        phase_started = time.monotonic()
        cycle_thread = runtime.cycle_thread
        if cycle_thread and cycle_thread.is_alive():
            print(
                "[SHUTDOWN] Cycle still active; archive finalization skipped"
            )
        else:
            self._shutdown_compress(runtime.archive)
        print(
            f"[SHUTDOWN] Архив: "
            f"{time.monotonic() - phase_started:.2f} с"
        )

        self._release_cameras()
        self._close_transport()
        print(
            f"[SHUTDOWN] Готово за "
            f"{time.monotonic() - shutdown_started:.2f} с."
        )

    def _request_force_exit(self) -> None:
        cycle = self.runtime.cycle
        if not cycle:
            return
        try:
            if not cycle.force_exit_requested:
                cycle.request_force_exit()
        except Exception as exc:
            # Остальные ресурсы всё равно должны освобождаться. Если цикл не
            # смог выполнить собственный emergency stop, дублируем команды
            # непосредственно через транспорт до закрытия COM.
            print(f"[SHUTDOWN] Cycle force-exit request failed: {exc}")
            self._send_controller_stop("Fallback stop")

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
            print(f"[SHUTDOWN] {label} failed: {exc}")

    def _join_initializer(self) -> None:
        init_thread = self.runtime.init_thread
        if not init_thread or not init_thread.is_alive():
            return
        init_thread.join(timeout=self.init_join_timeout)
        if init_thread.is_alive():
            print(
                "[SHUTDOWN] Initialization thread did not stop in "
                f"{self.init_join_timeout}s"
            )

    def _join_cycle(self, *, warn: bool = False) -> None:
        cycle_thread = self.runtime.cycle_thread
        if not cycle_thread or not cycle_thread.is_alive():
            return
        cycle_thread.join(timeout=self.cycle_join_timeout)
        if warn and cycle_thread.is_alive():
            print(
                "[WARN] cycle thread не завершился за "
                f"{self.cycle_join_timeout}с"
            )

    def _shutdown_compress(self, archive) -> None:
        if not archive:
            return

        try:
            worker = self._thread_factory(
                target=lambda: self._finalize_archive(archive),
                daemon=True,
            )
            worker.start()
            worker.join(timeout=self.compress_timeout)
            if worker.is_alive():
                print(
                    "[SHUTDOWN] Финализация архива не завершилась за "
                    f"{self.compress_timeout}с, пропускаем"
                )
        except Exception as exc:
            # Ошибка вспомогательного потока не должна препятствовать
            # освобождению камер и закрытию контроллера.
            print(f"[SHUTDOWN] Ошибка потока финализации архива: {exc}")

    def _finalize_archive(self, archive) -> None:
        """Финализировать партию: CLOSED-манифест, отправка, сжатие.

        Отправка batch.json аналитику выполняется в том же worker-потоке,
        что и сжатие: сетевой диск с зависшим SMB-таймаутом не может
        снять детерминированность завершения -- весь блок ограничен
        ``compress_timeout``, неотправленное остаётся в локальной очереди
        и уйдёт при следующем старте программы.
        """
        try:
            self._close_manifest(archive)
        except Exception as exc:
            print(f"[SHUTDOWN] Закрытие манифеста партии не удалось: {exc}")
        try:
            self._send_batch_manifests(archive)
        except Exception as exc:
            print(f"[SHUTDOWN] Отправка JSON аналитику не удалась: {exc}")
        self._safe_compress(archive)

    def _send_batch_manifests(self, archive) -> None:
        # Дубли архива без root_folder (тестовые) отправку не запускают.
        root = getattr(archive, "root_folder", None)
        if root is None:
            return
        self.batch_sender(root)

    @staticmethod
    def _close_manifest(archive) -> None:
        close = getattr(archive, "close_manifest", None)
        if close is not None:
            close()

    @staticmethod
    def _safe_compress(archive) -> None:
        try:
            print("[SHUTDOWN] Сжатие архива...")
            archive.compress()
        except Exception as exc:
            print(f"[SHUTDOWN] Ошибка сжатия архива: {exc}")

    def _release_cameras(self) -> None:
        phase_started = time.monotonic()
        # Live останавливается до VideoCapture, иначе фоновые чтения могут
        # продолжиться на уже освобождённых камерах.
        cycle = self.runtime.cycle
        if cycle:
            try:
                cycle.live.stop()
            except Exception as exc:
                print(f"[SHUTDOWN] Live preview stop failed: {exc}")
        try:
            if self.runtime.cameras:
                self.runtime.cameras.release()
        except Exception as exc:
            print(f"[SHUTDOWN] Camera release failed: {exc}")
        print(
            f"[SHUTDOWN] Освобождение камер: "
            f"{time.monotonic() - phase_started:.2f} с"
        )

    def _close_transport(self) -> None:
        phase_started = time.monotonic()
        try:
            if self.runtime.transport:
                self.runtime.transport.close()
        except Exception as exc:
            print(f"[SHUTDOWN] Serial close failed: {exc}")
        print(
            f"[SHUTDOWN] Закрытие COM: "
            f"{time.monotonic() - phase_started:.2f} с"
        )
