"""Совместимый импорт производственного цикла.

Новое место: ``core.cycle``. Этот модуль оставляем, чтобы
``from core.production_cycle import ProductionCycle`` и ``main.py``
не ломались.
"""

from core.cycle import ProductionCycle

__all__ = ["ProductionCycle"]
