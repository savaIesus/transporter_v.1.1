"""Явные этапы производственного шага и барьеры между ними.

Шаг линии разбит на фазы с одним владельцем камер у каждой:

```text
MOTION    лента едет            камеры у live-просмотра
SETTLE    лента встала          камеры у live-просмотра, гасим вибрацию
CAPTURE   лента неподвижна      весь live заморожен, кадры стадии читаются по одной камере
ANALYSIS  модели -> геометрия -> решение -> запись  live остаётся заморожен
CAPTURE   следующая занятая стадия                  тот же exclusive-блок
ANALYSIS  ...
PUBLISH   результат на экран    live всё ещё заморожен (стоп-кадры)
MOTION    следующий шаг         камеры возвращаются live-просмотру
```

Абсолютная последовательность: занятая стадия полностью заканчивается
(кадр → модели → геометрия → решение → запись). На протяжении всего
инспекционного блока live не возобновляется.

Переход между фазами — единственное место, где меняется владелец камер.
Поэтому «снять кадр для правил во время движения» или «читать камеру из
двух потоков» невозможно не по договорённости, а по построению:

* :meth:`StepSequencer.enter_capture` не просто ставит флаг, а дожидается
  завершения уже начатых live-чтений и забирает **все** камеры;
* live возвращается только в :meth:`enter_motion` / :meth:`reset`;
* порядок фаз проверяется таблицей ``_ALLOWED``: вызов не по порядку
  поднимает :class:`StageSequenceError`, а не тихо портит шаг.

``SETTLE`` существует отдельно от ``CAPTURE``, потому что контроллер
подтверждает остановку по счётчику шагов, а механика в этот момент ещё
качается. Кадр, снятый сразу после подтверждения, может быть смазан.
``STAGE_SETTLE_SECONDS`` — консервативное значение по умолчанию, его
нужно уточнить на реальной линии.
"""

from __future__ import annotations

import threading
import time
from enum import Enum

# Пауза между подтверждённой остановкой ленты и первым кадром инспекции.
STAGE_SETTLE_SECONDS = 0.3

# Предел ожидания освобождения камер live-просмотром перед захватом.
STAGE_CAPTURE_HANDOVER_TIMEOUT = 5.0

# Наблюдательная пауза перед каждой фазой. Ноль в production; ненулевое
# значение растягивает шаг, чтобы можно было видеть фазы по отдельности при
# отладке. На физику линии не влияет: пауза берётся тогда, когда лента уже
# остановлена или ещё не тронулась.
STAGE_TRACE_SECONDS = 0.05


class StageSequenceError(RuntimeError):
    """Фазы шага вызваны не в том порядке."""


class StepStage(str, Enum):
    IDLE = "IDLE"
    MOTION = "MOTION"
    SETTLE = "SETTLE"
    CAPTURE = "CAPTURE"
    ANALYSIS = "ANALYSIS"
    PUBLISH = "PUBLISH"


# Разрешённые переходы. Возврат в IDLE доступен всегда: это сброс шага
# при STOP, FAULT и завершении работы.
# ANALYSIS → CAPTURE: следующая занятая стадия того же шага.
_ALLOWED = {
    StepStage.IDLE: (StepStage.MOTION,),
    StepStage.MOTION: (StepStage.SETTLE,),
    StepStage.SETTLE: (StepStage.CAPTURE,),
    StepStage.CAPTURE: (StepStage.ANALYSIS,),
    StepStage.ANALYSIS: (StepStage.CAPTURE, StepStage.PUBLISH),
    StepStage.PUBLISH: (StepStage.MOTION,),
}


class StepSequencer:
    """Владелец фаз шага и единственная точка передачи камер.

    Класс не читает камеры сам: он только решает, кому они принадлежат
    в текущей фазе, и гарантирует, что смена владельца завершена до
    начала следующей фазы.
    """

    def __init__(
        self,
        live,
        settle_seconds: float = STAGE_SETTLE_SECONDS,
        handover_timeout: float = STAGE_CAPTURE_HANDOVER_TIMEOUT,
        trace_seconds: float = STAGE_TRACE_SECONDS,
        on_stage=None,
        sleep=time.sleep,
    ):
        self._live = live
        self._settle_seconds = float(settle_seconds)
        self._handover_timeout = float(handover_timeout)
        self._trace_seconds = float(trace_seconds)
        self._on_stage = on_stage
        self._sleep = sleep
        self._lock = threading.Lock()
        self._stage = StepStage.IDLE
        self._static_held = False
        self._stage_started_at = time.monotonic()
        # Номер поколения шага. reset() увеличивает его, поэтому передача
        # камер, начатая до сброса, понимает, что её результат уже неактуален.
        self._generation = 0

    @property
    def stage(self) -> StepStage:
        with self._lock:
            return self._stage

    @property
    def static(self) -> bool:
        """True, когда камеры принадлежат inspection, а не live."""
        with self._lock:
            return self._static_held

    @property
    def static_roles(self):
        """Роли в inspection.

        Всегда ``None``: инспекция забирает камеры целиком. Свойство
        сохранено как контракт статуса для HMI, где ``None`` означает
        «заморожены все роли» (``all_roles_static``).
        """
        return None

    def _switch(self, target: StepStage):
        """Перейти в фазу, выдержав наблюдательную паузу перед ней.

        Пауза берётся до смены фазы и вне блокировки: она не должна
        задерживать чтение ``stage`` из потоков UI.
        """
        if self._trace_seconds > 0:
            self._sleep(self._trace_seconds)

        with self._lock:
            current = self._stage
            if target not in _ALLOWED[current]:
                raise StageSequenceError(
                    f"Недопустимый переход шага: {current.value} -> "
                    f"{target.value}"
                )
            now = time.monotonic()
            elapsed = now - self._stage_started_at
            self._stage = target
            self._stage_started_at = now

        self._report(current, target, elapsed)

    def _report(self, previous: StepStage, target: StepStage, elapsed: float):
        if self._on_stage is None:
            return
        try:
            self._on_stage(previous, target, elapsed)
        except Exception as exc:
            # Наблюдение за фазами не должно ронять производственный шаг.
            print(f"[STAGE] Ошибка обработчика фаз: {exc}")

    def enter_motion(self):
        """Начать движение: камеры возвращаются live-просмотру."""
        self._switch(StepStage.MOTION)
        self._release_static()

    def enter_settle(self):
        """Лента подтвердила остановку; ждём затухания вибрации."""
        self._switch(StepStage.SETTLE)
        if self._settle_seconds > 0:
            self._sleep(self._settle_seconds)

    def enter_capture(self, roles=None):
        """Начать exclusive-захват официальных кадров стадии.

        Непустой ``roles`` забирает **все** камеры у live до следующего
        ``MOTION``: USB не делят инспекция и просмотр. Пустой список
        означает, что на этой остановке инспекция не нужна и live не
        прерывается. Повторный вход из ``ANALYSIS`` держит тот же
        exclusive-блок для следующей стадии.
        """
        self._switch(StepStage.CAPTURE)
        if roles:
            self._acquire_static()

    def enter_analysis(self):
        """Модели и правила по уже снятым кадрам; live остаётся заморожен."""
        self._switch(StepStage.ANALYSIS)

    def enter_publish(self):
        """Опубликовать результат поверх статичных кадров."""
        self._switch(StepStage.PUBLISH)

    def reset(self):
        """Сбросить шаг в IDLE и вернуть камеры live-просмотру."""
        with self._lock:
            self._stage = StepStage.IDLE
            self._generation += 1
        self._release_static()

    def _acquire_static(self):
        """Забрать все камеры у live-просмотра до конца инспекции."""
        with self._lock:
            if self._static_held:
                return
            generation = self._generation

        if not self._live.pause(self._handover_timeout):
            raise StageSequenceError(
                f"Live-просмотр не освободил камеры за "
                f"{self._handover_timeout}s; шаг остановлен"
            )
        with self._lock:
            stale = generation != self._generation
            if not stale:
                self._static_held = True
        if stale:
            # reset() успел пройти, пока live отдавал камеры: удерживать
            # паузу от имени отменённого шага нельзя.
            self._live.resume()
            raise StageSequenceError(
                "Шаг сброшен во время передачи камер инспекции"
            )

    def _release_static(self):
        with self._lock:
            if not self._static_held:
                return
            self._static_held = False
        self._live.resume()
