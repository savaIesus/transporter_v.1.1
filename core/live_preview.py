"""Живой просмотр камер параллельно с работой производственной линии.

Кадры для UI и инспекции берутся с одних камер, поэтому доступ
разграничивает :class:`LiveCaptureGate`. Перед официальным захватом
production-цикл замораживает **весь** live и держит его до следующего
``MOTION``. USB-чтения сериализует ``CameraManager``: один ``cap.read()``
в момент времени.

Раскладка потоков повторяет реальную нагрузку USB: выбранная в UI
камера обновляется с частотой ``LIVE_TARGET_FPS``, остальные две — одним
пакетом и заметно реже, иначе три камеры не помещаются в шину.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections import deque

LIVE_TARGET_FPS = 30.0
LIVE_FRAME_INTERVAL = 1.0 / LIVE_TARGET_FPS
LIVE_AUX_BATCH_INTERVAL = 0.20
LIVE_THREAD_JOIN_TIMEOUT = 6.0
LIVE_PAUSE_DRAIN_TIMEOUT = 5.0

_FPS_WINDOW_SECONDS = 2.0


class LiveCaptureGate:
    """Разграничение live- и inspection-чтений камер.

    Инспекция и UI смотрят в одни и те же камеры, поэтому владелец
    задаётся счётчиком пауз. Production-шаг замораживает **все** роли на
    весь инспекционный блок (``CAPTURE``…``PUBLISH``), диагностика
    выбранной камеры — на время своего анализа. Промежуточного режима
    «часть ролей у инспекции, часть у live» нет: USB не выдерживает
    параллельного чтения, а стоп-кадр не должен затираться live-потоком.
    """

    def __init__(self):
        self._condition = threading.Condition()
        self._pause_depth = 0
        self._active_reads = 0

    def pause(self, timeout: float = LIVE_PAUSE_DRAIN_TIMEOUT) -> bool:
        """Приостановить live и дождаться завершения начатых чтений.

        ``False`` означает, что чтения не завершились за ``timeout``;
        пауза при этом снимается, чтобы не оставить live заблокированным.
        """
        deadline = time.monotonic() + timeout
        with self._condition:
            self._pause_depth += 1
            while self._active_reads:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._pause_depth -= 1
                    if not self._pause_depth:
                        self._condition.notify_all()
                    return False
                self._condition.wait(remaining)
            return True

    def resume(self):
        with self._condition:
            if self._pause_depth:
                self._pause_depth -= 1
            if not self._pause_depth:
                self._condition.notify_all()

    def reset(self):
        with self._condition:
            self._pause_depth = 0
            self._condition.notify_all()

    def _acquire(self, count: int) -> bool:
        """Занять ``count`` слотов чтения, если live не на паузе."""
        with self._condition:
            if self._pause_depth:
                return False
            self._active_reads += count
            return True

    def _release(self, count: int):
        with self._condition:
            self._active_reads -= count
            self._condition.notify_all()

    @contextlib.contextmanager
    def live_read(self, role=None):
        """Занять слот live-чтения; ``False`` означает паузу инспекции."""
        allowed = self._acquire(1)
        try:
            yield allowed
        finally:
            if allowed:
                self._release(1)

    @contextlib.contextmanager
    def live_reads(self, roles):
        """Занять слоты пакета ролей; пустой кортеж означает паузу."""
        roles = tuple(dict.fromkeys(roles or ()))
        allowed_roles = roles if roles and self._acquire(len(roles)) else ()
        try:
            yield allowed_roles
        finally:
            if allowed_roles:
                self._release(len(allowed_roles))


class LivePreview:
    """Фоновая публикация кадров камер, уступающая их инспекции."""

    def __init__(self, cameras, monitor, get_active_role, gate=None):
        self._cameras = cameras
        self._monitor = monitor
        self._get_active_role = get_active_role
        self.gate = gate if gate is not None else LiveCaptureGate()

        # _lifecycle_lock сериализует start/stop целиком; _state_lock
        # защищает только поля, которые читают рабочие потоки и UI.
        self._lifecycle_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._ready_condition = threading.Condition(self._state_lock)
        self._stop_event = threading.Event()
        self._threads = []
        self._frame_times = deque(maxlen=240)
        self._published_roles = set()
        self._error = None

    # Состояние

    @property
    def running(self) -> bool:
        with self._state_lock:
            return any(thread.is_alive() for thread in self._threads)

    @property
    def error(self):
        with self._state_lock:
            return self._error

    @property
    def fps(self) -> float:
        now = time.monotonic()
        recent = [
            value for value in list(self._frame_times)
            if now - value <= _FPS_WINDOW_SECONDS
        ]
        if len(recent) < 2:
            return 0.0
        elapsed = recent[-1] - recent[0]
        measured = 0.0 if elapsed <= 0 else (len(recent) - 1) / elapsed
        return min(LIVE_TARGET_FPS, measured)

    # Управление потоками

    def start(self) -> bool:
        """Запустить потоки просмотра. Повторный вызов ничего не делает.

        ``_lifecycle_lock`` сериализует start и stop целиком: иначе старт
        мог бы поднять потоки на ещё не снятом стоп-сигнале предыдущей
        остановки и мгновенно их погасить.
        """
        with self._lifecycle_lock:
            with self._state_lock:
                # После тайм-аута stop() хранит ссылку на ещё живой поток и
                # не снимает стоп-сигнал. Когда зависшее чтение всё же
                # вернётся, поток завершится; перед новым стартом удаляем
                # только такие уже завершившиеся ссылки.
                self._threads = [
                    thread for thread in self._threads if thread.is_alive()
                ]
                if self._threads:
                    return False
                self._stop_event.clear()
                self._frame_times.clear()
                self._published_roles.clear()
                self._error = None
                self._threads = [
                    threading.Thread(
                        target=self._selected_loop,
                        daemon=True,
                        name="live-selected-camera",
                    ),
                    threading.Thread(
                        target=self._auxiliary_loop,
                        daemon=True,
                        name="live-aux-cameras",
                    ),
                ]
                threads = list(self._threads)
            self.clear_overlays()
            for thread in threads:
                thread.start()
            print("[LIVE] preview started")
            return True

    def stop(self) -> bool:
        """Остановить потоки просмотра и дождаться их завершения.

        Если системный вызов камеры не вернулся до тайм-аута, стоп-сигнал
        остаётся выставленным, а ссылка на поток сохраняется. Поэтому после
        разблокировки старый поток завершится, а не возобновит скрытые чтения
        камер. ``False`` означает отложенное завершение такого потока.
        """
        with self._lifecycle_lock:
            # Стоп-сигнал выставляется до ожидания потоков. Пока хотя бы один
            # поток жив, start() не должен поднять вторую пару читателей.
            self._stop_event.set()
            with self._ready_condition:
                threads = list(self._threads)
                self._ready_condition.notify_all()
            if not threads:
                self._stop_event.clear()
                return True

            deadline = time.monotonic() + LIVE_THREAD_JOIN_TIMEOUT
            for thread in threads:
                thread.join(max(0.0, deadline - time.monotonic()))

            alive = [thread for thread in threads if thread.is_alive()]
            with self._state_lock:
                self._threads = alive

            if alive:
                for thread in alive:
                    print(
                        f"[LIVE] поток {thread.name} не остановился за "
                        f"{LIVE_THREAD_JOIN_TIMEOUT}s; стоп-сигнал сохранён"
                    )
                return False

            self._stop_event.clear()
            print("[LIVE] preview stopped")
            return True

    def wait_for_roles(self, roles, timeout: float) -> tuple[str, ...]:
        """Дождаться первого опубликованного кадра каждой требуемой роли.

        Возвращает пустой кортеж при готовности либо роли без кадра при
        ошибке, остановке live или истечении тайм-аута. Это стартовый барьер:
        HMI не объявляет систему готовой только по факту ``isOpened()``.
        """
        wanted = {str(role) for role in (roles or ()) if role}
        if not wanted:
            return ()
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._ready_condition:
            while True:
                missing = tuple(sorted(wanted - self._published_roles))
                if not missing:
                    return ()
                if self._error is not None or self._stop_event.is_set():
                    return missing
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return missing
                self._ready_condition.wait(remaining)

    # Пауза на время статической инспекции

    def pause(self, timeout: float = LIVE_PAUSE_DRAIN_TIMEOUT) -> bool:
        return self.gate.pause(timeout)

    def resume(self):
        self.gate.resume()

    def reset_pause(self):
        self.gate.reset()

    def clear_overlays(self):
        """Убрать геометрию правил перед показом движущихся кадров.

        Разметка построена по статичному кадру, поэтому на движущемся
        изображении она указывала бы не на те места.
        """
        if self._monitor is None:
            return
        self._monitor.update(vision_results={}, rule_results=[])

    # Внутреннее

    def _available_roles(self) -> list:
        return list(self._cameras.mapping)

    def _active_role(self, available_roles: list):
        try:
            role = self._get_active_role()
        except Exception:
            role = None
        if available_roles and role not in available_roles:
            return available_roles[0]
        return role

    def _publish(self, frames: dict):
        if not frames:
            return
        if self._monitor is not None:
            self._monitor.update(frames=frames)
        with self._ready_condition:
            self._published_roles.update(str(role) for role in frames)
            self._ready_condition.notify_all()

    def _fail(self, exc: Exception, source: str):
        if self._stop_event.is_set():
            return
        message = f"{type(exc).__name__}: {exc}"
        with self._ready_condition:
            if self._error is None:
                self._error = message
            self._stop_event.set()
            self._ready_condition.notify_all()
        print(f"[LIVE] {source} error: {message}")

    def _run_loop(self, interval: float, iteration, source: str):
        """Цикл live-чтения; iteration сама занимает слоты у gate."""
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                frames = iteration()
                self._publish(frames)
            except Exception as exc:
                self._fail(exc, source)
                break
            elapsed = time.monotonic() - started
            self._stop_event.wait(max(0.0, interval - elapsed))

    def _selected_loop(self):
        def iteration():
            available_roles = self._available_roles()
            active_role = self._active_role(available_roles)
            if active_role is None:
                return None
            with self.gate.live_read(active_role) as allowed:
                if not allowed:
                    return None
                frame = self._cameras.capture_single(active_role)
            self._frame_times.append(time.monotonic())
            return {active_role: frame}
        self._run_loop(LIVE_FRAME_INTERVAL, iteration, "selected camera loop")

    def _auxiliary_loop(self):
        def iteration():
            available_roles = self._available_roles()
            active_role = self._active_role(available_roles)
            auxiliary_roles = [role for role in available_roles if role != active_role]
            with self.gate.live_reads(auxiliary_roles) as allowed_roles:
                if not allowed_roles:
                    return None
                return self._cameras.capture_roles(allowed_roles)
        self._run_loop(LIVE_AUX_BATCH_INTERVAL, iteration, "auxiliary loop")
