import re
import time


class Axis:
    """Управление одной NEMA-осью с обязательными host/firmware limits."""

    def __init__(
        self,
        transport,
        axis_id: int,
        maximum: int,
        minimum: int = 0,
        speed: int = 300,
        accel: int = 100,
    ):
        if axis_id not in (0, 1):
            raise ValueError("axis_id должен быть 0 или 1")
        if type(minimum) is not int or type(maximum) is not int:
            raise ValueError("Axis limits должны быть int")
        if minimum < 0 or maximum <= minimum:
            raise ValueError("Ожидается 0 <= minimum < maximum")
        self.transport = transport
        self.axis_id = axis_id
        self.minimum = minimum
        self.maximum = maximum
        self._set_params(speed, accel)
        self._set_limits(minimum, maximum)
        self.verify_limit_config()

    def move_absolute(self, position: int):
        """Переместить в абсолютную позицию внутри 0..maximum.

        Ожидание остановки выполняет вызывающий код (``wait_stop``).
        Короткая пауза после отправки оставлена для совместимости с
        однокомандными вызовами; параллельное движение осей использует
        ``move_absolute_async``, чтобы команды ушли подряд без паузы.
        """
        self.move_absolute_async(position)
        time.sleep(0.1)

    def move_absolute_async(self, position: int):
        """Отправить команду перемещения без паузы и без ожидания.

        Нужна для одновременного движения обеих осей распределителя:
        команды ``G27`` на DIST1 и DIST2 отправляются подряд, а остановки
        ожидаются после, по одной оси.
        """
        if type(position) is not int or not self.minimum <= position <= self.maximum:
            raise ValueError(
                f"Axis {self.axis_id}: absolute position must be int "
                f"in {self.minimum}..{self.maximum}"
            )
        self.transport.send(f"G27 S{position} P{self.axis_id}")

    def home(self):
        """Выполнить физический homing через концевик (firmware G28)."""
        self.transport.send(f"G28 P{self.axis_id}")
        time.sleep(0.1)

    def read_status(self) -> dict:
        data = self.transport.query("I10")
        line_match = re.search(
            rf"AXIS{self.axis_id}\s+([^\r\n]+)",
            data,
        )
        line = line_match.group(1) if line_match else ""

        def field(name):
            match = re.search(rf"\b{name}=(-?\d+)\b", line)
            return int(match.group(1)) if match else None

        return {
            "raw": data,
            "position": field("POS"),
            "target": field("TGT"),
            "moving": field("MOV"),
            "enabled": field("EN"),
            "home_phase": field("HOME"),
            "homed": field("HOMED"),
            "limits_enabled": field("LIM"),
            "endstop": field("ES"),
        }

    def read_config(self) -> dict:
        data = self.transport.query("I11")
        line_match = re.search(
            rf"AXIS{self.axis_id}\s+([^\r\n]+)",
            data,
        )
        line = line_match.group(1) if line_match else ""

        def field(name):
            match = re.search(rf"\b{name}=(-?\d+)\b", line)
            return int(match.group(1)) if match else None

        return {
            "raw": data,
            "speed": field("speed"),
            "accel": field("accel"),
            "limit_min": field("limMin"),
            "limit_max": field("limMax"),
        }

    def verify_limit_config(self):
        config = self.read_config()
        if config["limit_min"] != self.minimum:
            raise RuntimeError(
                f"Axis {self.axis_id}: firmware limMin="
                f"{config['limit_min']}, expected {self.minimum}"
            )
        if config["limit_max"] != self.maximum:
            raise RuntimeError(
                f"Axis {self.axis_id}: firmware limMax="
                f"{config['limit_max']}, expected {self.maximum}"
            )
        print(
            f"[AXIS{self.axis_id}] limits verified: "
            f"{config['limit_min']}..{config['limit_max']}"
        )
        return config

    def verify_homed(self):
        status = self.read_status()
        expected = {
            "position": 0,
            "moving": 0,
            "homed": 1,
            "limits_enabled": 1,
        }
        errors = [
            f"{name}={status[name]!r}, expected {value}"
            for name, value in expected.items()
            if status[name] != value
        ]
        if errors:
            raise RuntimeError(
                f"Axis {self.axis_id}: invalid homing postcondition: "
                + "; ".join(errors)
            )
        return status

    @property
    def position(self) -> int:
        position = self.read_status()["position"]
        if position is None:
            raise RuntimeError(
                f"Axis {self.axis_id}: controller reply has no position"
            )
        return position

    def wait_stop(self, timeout: float = 10.0, progress_callback=None):
        start = time.time()
        while True:
            status = self.read_status()
            position = status["position"]
            moving = status["moving"]
            if progress_callback is not None:
                progress_callback(position, moving)
            if moving == 0:
                time.sleep(0.05)
                return
            if time.time() - start > timeout:
                raise TimeoutError(
                    f"Axis {self.axis_id} не остановилась за {timeout}s; "
                    f"status={status['raw']!r}"
                )
            time.sleep(0.05)

    def _set_params(self, speed: int, accel: int):
        self.transport.send(f"G21 S{speed} P{self.axis_id}")
        self.transport.send(f"G22 S{accel} P{self.axis_id}")

    def _set_limits(self, minimum: int, maximum: int):
        # Firmware defaults to 300. Override before every G28.
        self.transport.send(f"G31 S{minimum} P{self.axis_id}")
        self.transport.send(f"G32 S{maximum} P{self.axis_id}")
        self.transport.send(f"G33 S1 P{self.axis_id}")
        time.sleep(0.15)
