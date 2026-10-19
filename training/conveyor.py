"""Шаг ленты из мастера сбора датасета.

Мастер обучения работает с остановленной линией, но после разметки каждой
детали под камерами должен оказаться следующий корпус. Поэтому завершение
детали сопровождается одним шагом ленты через ту же прошивку, что и
production-цикл: ``G3 N<seq>`` с адресным ACK и подтверждением ``STEP`` по
``I2``. Движение выполняется только после того, как разметка сохранена на
диске, поэтому сбой контроллера не приводит к потере кадров.

Модуль намеренно не импортирует pyserial и ``hardware`` на уровне модуля:
``import training`` должен работать на машине без оборудования, а поиск
порта выполняется лениво — при первом нажатии «Завершить деталь».
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Protocol, runtime_checkable

DEFAULT_BAUDRATE = 115200
DEFAULT_CALIBRATION_PATH = "calibration.json"
# Ожидание физического завершения шага. Лента проходит один шаг за доли
# секунды, но запас нужен на разгон/торможение и медленную калибровку.
STEP_TIMEOUT = 20.0
# После неудачного поиска контроллера не сканируем COM-порты при каждом
# нажатии: каждый порт проверяется ~2.5 с.
DISCOVERY_COOLDOWN = 5.0


class ConveyorStepError(RuntimeError):
    """Шаг ленты не выполнен; разметка при этом уже сохранена."""


@runtime_checkable
class ConveyorStepper(Protocol):
    """Минимальный интерфейс шага ленты для :class:`TrainingCollector`."""

    def advance(self) -> dict[str, Any]:
        """Сделать один подтверждённый шаг и вернуть его результат."""


class SerialConveyorStepper:
    """Один шаг ленты по команде ``G3`` на каждое завершение детали."""

    def __init__(
        self,
        *,
        baudrate: int = DEFAULT_BAUDRATE,
        preferred_port: str | None = None,
        calibration_path: str = DEFAULT_CALIBRATION_PATH,
        step_timeout: float = STEP_TIMEOUT,
        discovery_cooldown: float = DISCOVERY_COOLDOWN,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.baudrate = int(baudrate)
        self.preferred_port = preferred_port or None
        self.calibration_path = calibration_path
        self.step_timeout = float(step_timeout)
        self.discovery_cooldown = max(0.0, float(discovery_cooldown))
        self._clock = clock
        self._lock = threading.Lock()
        self._transport = None
        self._conveyor = None
        self._calibration: dict[str, Any] | None = None
        self._port: str | None = None
        self._discovery_error: str | None = None
        self._last_discovery_attempt: float | None = None

    @property
    def port(self) -> str | None:
        return self._port

    @property
    def connected(self) -> bool:
        return self._conveyor is not None

    def advance(self) -> dict[str, Any]:
        """Выполнить ровно один шаг и дождаться подтверждения ``STEP``."""
        with self._lock:
            conveyor = self._ensure_conveyor()
            try:
                conveyor.move_step()
                conveyor.wait_stop(timeout=self.step_timeout)
                sequence = self._read_sequence(conveyor)
            except Exception as exc:
                # Состояние движения после ошибки неоднозначно. Следующее
                # нажатие начнёт с нового чтения I2, а не повторит команду.
                self._reset()
                raise ConveyorStepError(
                    f"Шаг конвейера не выполнен: {exc}"
                ) from exc
            return {
                "moved": True,
                "step": sequence,
                "port": self._port,
            }

    def close(self) -> None:
        with self._lock:
            self._reset()

    # Internal

    def _ensure_conveyor(self):
        if self._conveyor is not None:
            return self._conveyor

        now = self._clock()
        if (
            self._discovery_error is not None
            and self._last_discovery_attempt is not None
            and now - self._last_discovery_attempt < self.discovery_cooldown
        ):
            raise ConveyorStepError(
                f"Контроллер недоступен: {self._discovery_error}"
            )
        self._last_discovery_attempt = now

        try:
            # Ленивые импорты: мастер обучения не требует оборудования,
            # если шаг ленты отключён или контроллер ещё не понадобился.
            from config.calibration_loader import load_calibration
            from hardware.conveyor import Conveyor
            from hardware.port_discovery import find_controller
            from hardware.serial_transport import SerialTransport

            port, message = find_controller(
                baudrate=self.baudrate,
                preferred_port=self.preferred_port,
            )
            if port is None:
                raise RuntimeError(f"Контроллер не найден: {message}")

            transport = SerialTransport(port=port, baudrate=self.baudrate)
            self._transport = transport
            if self._calibration is None:
                self._calibration = load_calibration(self.calibration_path)
            calibration = self._calibration
            self._conveyor = Conveyor(
                transport,
                speed=calibration["conveyor_speed"],
                accel=calibration["conveyor_accel"],
                steps_per_division=calibration["normal_steps"],
                divisions_per_movement=2,
            )
        except Exception as exc:
            self._reset(keep_error=str(exc))
            raise ConveyorStepError(
                f"Контроллер недоступен: {exc}"
            ) from exc

        self._port = port
        self._discovery_error = None
        self._last_discovery_attempt = None
        return self._conveyor

    @staticmethod
    def _read_sequence(conveyor) -> int | None:
        """Прочитать подтверждённый номер шага из статуса ``I2``."""
        from hardware.conveyor import Conveyor

        status = conveyor.transport.query("I2", delay=0.1)
        return Conveyor._parse_status(status)["step"]

    def _reset(self, *, keep_error: str | None = None) -> None:
        transport = self._transport
        self._transport = None
        self._conveyor = None
        self._port = None
        self._discovery_error = keep_error
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass
