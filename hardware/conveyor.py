import re
import time


class Conveyor:
    """Управление лентой с подтверждением каждого завершённого шага.

    Firmware хранит монотонный ``STEP`` в статусе I2. Производственная
    команда ``G3 N<seq>`` принимается только для следующего номера и отвечает
    ``ACK G3 STEP=<seq>``. Логическая позиция может быть изменена лишь после
    того, как I2 вернул тот же номер завершённого шага.
    """

    STEP_MODULUS = 1 << 32

    def __init__(
        self,
        transport,
        speed: int = 20000,
        accel: int = 6000,
        steps_per_division: int = 19048,
        divisions_per_movement: int = 2,
    ):
        self.transport = transport
        self._pending_step_sequence: int | None = None
        if steps_per_division <= 0 or divisions_per_movement <= 0:
            raise ValueError("Conveyor geometry must be positive")
        self._set_params(
            speed,
            accel,
            steps_per_division,
            divisions_per_movement,
        )

    def move_step(self):
        """Отправить один шаг и проверить адресованное подтверждение приёма."""
        if self._pending_step_sequence is not None:
            raise RuntimeError("Предыдущий шаг конвейера ещё не подтверждён")

        status = self.transport.query("I2", delay=0.1)
        parsed = self._parse_status(status)
        current_sequence = parsed["step"]
        if not self._strict_stop_confirmed(status) or current_sequence is None:
            raise RuntimeError(
                "Конвейер не готов принять шаг или firmware не поддерживает "
                f"STEP-протокол: I2='{status}'"
            )

        expected = (current_sequence + 1) % self.STEP_MODULUS
        # Сохраняем номер до отправки. Если команда дошла, но ACK потерян,
        # состояние физики неоднозначно и повторять тот же шаг запрещено.
        self._pending_step_sequence = expected
        acknowledgement = self.transport.query(f"G3 N{expected}", delay=0.15)
        if not self._ack_confirmed(acknowledgement, expected):
            raise RuntimeError(
                "Контроллер не подтвердил приём шага; повтор команды опасен: "
                f"ожидался STEP={expected}, ответ='{acknowledgement}'"
            )

    def wait_stop(self, timeout: float = 15.0, progress_callback=None):
        """Ждать остановки и подтверждения выполнения ожидаемого STEP."""
        expected = self._pending_step_sequence
        if expected is None:
            raise RuntimeError("Нет принятой команды шага конвейера")

        start = time.time()
        data = ""
        status = ""

        while True:
            data = self.transport.query("I1", delay=0.1)
            stopped = self._parse_motion_reply(data)
            status = self.transport.query("I2", delay=0.1)
            parsed_status = self._parse_status(status)
            if progress_callback is not None:
                progress_callback(parsed_status)

            completed = parsed_status["step"] == expected
            if (
                stopped is True
                and self._strict_stop_confirmed(status)
                and completed
            ):
                self._pending_step_sequence = None
                time.sleep(0.05)
                return

            if time.time() - start > timeout:
                raise TimeoutError(
                    f"Конвейер не подтвердил STEP={expected} за {timeout}s. "
                    f"I1='{data}', I2='{status}'"
                )

            time.sleep(0.05)

    def emergency_stop(self):
        """Аварийная остановка."""
        self.transport.send("G1")

    def _set_params(
        self,
        speed: int,
        accel: int,
        steps_per_division: int,
        divisions_per_movement: int,
    ):
        self.transport.send(f"G5 S{speed}")
        self.transport.send(f"G4 S{accel}")
        self.transport.send(f"G7 S{steps_per_division}")
        self.transport.send(f"G6 S{divisions_per_movement}")
        # Сохраняем параметры: они читаются production-циклом
        # (_on_conveyor_progress) для расчёта длительности движения в UI.
        self.speed = int(speed)
        self.steps_per_division = int(steps_per_division)
        time.sleep(0.5)

    @staticmethod
    def _parse_status(data: str) -> dict:
        result = {"raw": data}
        for key in ("MOV", "WAIT", "POS", "TGT", "STEP", "lastErr"):
            match = re.search(
                rf"\b{key}\s*=\s*(-?\d+)\b",
                data or "",
                re.IGNORECASE,
            )
            result[key.lower()] = int(match.group(1)) if match else None
        return result

    @staticmethod
    def _ack_confirmed(data: str, expected: int) -> bool:
        if not data:
            return False
        return bool(re.search(
            rf"^\s*ACK\s+G3\s+STEP\s*=\s*{expected}\s*$",
            data,
            re.IGNORECASE | re.MULTILINE,
        ))

    @staticmethod
    def _strict_stop_confirmed(data: str) -> bool:
        """I2 must confirm no movement, no inter-move wait and no error."""
        if not data:
            return False
        mov = re.search(r"\bMOV\s*=\s*(\d+)\b", data, re.IGNORECASE)
        wait = re.search(r"\bWAIT\s*=\s*(\d+)\b", data, re.IGNORECASE)
        error = re.search(r"\blastErr\s*=\s*(-?\d+)\b", data, re.IGNORECASE)
        return bool(
            mov and wait and error
            and int(mov.group(1)) == 0
            and int(wait.group(1)) == 0
            and int(error.group(1)) == 0
        )

    @staticmethod
    def _parse_motion_reply(data: str) -> bool | None:
        """Разобрать I1: ``0`` — остановлен, ``1`` — движется."""
        if not data:
            return None

        for line in reversed(data.splitlines()):
            stripped = line.strip()
            if stripped == "0":
                return True
            if stripped == "1":
                return False

        return None
