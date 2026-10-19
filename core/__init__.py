"""Пакет ядра линии.

Импорты ленивые: ``from core.production_cycle import ProductionCycle``
(или ``from core.cycle import ProductionCycle``) не должен тянуть
YOLO/OpenCV через DecisionEngine.
"""

__all__ = [
    "StateMachine",
    "State",
    "DecisionEngine",
    "ProductionCycle",
]


def __getattr__(name):
    if name in ("StateMachine", "State"):
        from core.state_machine import State, StateMachine
        return StateMachine if name == "StateMachine" else State
    if name == "DecisionEngine":
        from core.decision_engine import DecisionEngine
        return DecisionEngine
    if name == "ProductionCycle":
        from core.production_cycle import ProductionCycle
        return ProductionCycle
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
