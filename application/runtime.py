"""Изменяемое состояние ресурсов, разделяемое стадиями жизненного цикла."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Event
from typing import Any


@dataclass
class RuntimeState:
    """Ресурсы, которые появляются по мере фоновой инициализации.

    Инициализация выполняется в отдельном потоке, поэтому владельцы shutdown и
    EXIT читают зависимости через один объект, а не через набор nonlocal
    переменных и замыканий из ``main``.
    """

    monitor: Any
    shutdown_requested: Event = field(default_factory=Event)
    cameras: Any = None
    transport: Any = None
    cycle: Any = None
    cycle_thread: Any = None
    init_thread: Any = None
    archive: Any = None
    threshold_callbacks: Any = None
