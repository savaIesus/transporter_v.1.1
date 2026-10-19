import threading
from enum import Enum
from collections.abc import Callable


class State(str, Enum):
    IDLE     = "IDLE"
    RUNNING  = "RUNNING"
    PAUSED   = "PAUSED"
    STOPPING = "STOPPING"
    STOPPED  = "STOPPED"
    FAULT    = "FAULT"


_TRANSITIONS = {
    (State.IDLE,     "START"):  State.RUNNING,
    (State.STOPPED,  "START"):  State.RUNNING,
    (State.RUNNING,  "STOP"):   State.STOPPING,
    (State.STOPPING, "EMPTY"):  State.STOPPED,
    (State.RUNNING,  "PAUSE"):  State.PAUSED,
    (State.PAUSED,   "RESUME"): State.RUNNING,
    (State.PAUSED,   "STOP"):   State.STOPPING,
    (State.IDLE,     "FAULT"):  State.FAULT,
    (State.RUNNING,  "FAULT"):  State.FAULT,
    (State.PAUSED,   "FAULT"):  State.FAULT,
    (State.STOPPING, "FAULT"):  State.FAULT,
    (State.STOPPED,  "FAULT"):  State.FAULT,
}


class StateMachine:
    """
    Потокобезопасный конечный автомат производственной линии.
    """

    def __init__(self, on_transition: Callable | None = None):
        self._state = State.IDLE
        self._exit_requested = False
        self._force_exit = False
        self._on_transition = on_transition
        self._lock = threading.Lock()

    @property
    def state(self) -> State:
        with self._lock:
            return self._state

    @property
    def exit_requested(self) -> bool:
        with self._lock:
            return self._exit_requested

    @property
    def force_exit(self) -> bool:
        with self._lock:
            return self._force_exit

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self._state in (State.RUNNING, State.STOPPING)

    @property
    def accepts_new_parts(self) -> bool:
        with self._lock:
            return self._state == State.RUNNING

    def request_start(self) -> bool:
        return self._apply("START")

    def request_stop(self) -> bool:
        return self._apply("STOP")

    def request_pause(self) -> bool:
        return self._apply("PAUSE")

    def request_resume(self) -> bool:
        return self._apply("RESUME")

    def request_exit(self):
        callback_args = None
        with self._lock:
            self._exit_requested = True
            # Штатный ВЫХОД с паузы должен дренировать линию так же,
            # как ВЫХОД из RUNNING: иначе состояние остаётся PAUSED,
            # а live-поток гасится в exit_jog.
            if self._state in (State.RUNNING, State.PAUSED):
                key = (self._state, "STOP")
                new_state = _TRANSITIONS.get(key)
                if new_state is not None:
                    old = self._state
                    self._state = new_state
                    print(
                        f"[STATE] {old.value} --STOP--> "
                        f"{new_state.value}"
                    )
                    callback_args = (old, new_state, "STOP")

        if callback_args and self._on_transition:
            self._on_transition(*callback_args)
        return True

    def request_force_exit(self):
        with self._lock:
            self._exit_requested = True
            self._force_exit = True
        print("[STATE] FORCE EXIT requested")
        return True

    def notify_line_empty(self) -> bool:
        return self._apply("EMPTY")

    def notify_fault(self) -> bool:
        return self._apply("FAULT")

    def get_snapshot(self) -> dict:
        """Атомарный снимок всех полей для UI."""
        with self._lock:
            return {
                "state":          self._state.value,
                "exit_requested": self._exit_requested,
                "force_exit":     self._force_exit,
            }

    def _apply(self, action: str) -> bool:
        callback_args = None

        with self._lock:
            key = (self._state, action)
            new_state = _TRANSITIONS.get(key)

            if new_state is None:
                print(
                    f"[STATE] {action} ignored "
                    f"in {self._state.value}"
                )
                return False

            old = self._state
            self._state = new_state
            print(
                f"[STATE] {old.value} --{action}--> "
                f"{new_state.value}"
            )
            callback_args = (old, new_state, action)

        # Callback ПОСЛЕ отпускания lock
        if callback_args and self._on_transition:
            self._on_transition(*callback_args)

        return True
