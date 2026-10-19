"""Ручной ход ленты (JOG).

Часть ``ProductionCycle``. Разрешён в IDLE / STOPPED / PAUSED.
"""


class CycleJogMixin:
    """Dead-man JOG поверх безопасных состояний линии."""

    # JOG mode

    def can_enter_jog(self) -> bool:
        if self.jog is None or self._shutdown:
            return False
        return (
            self.state in self.JOG_ALLOWED_STATES
            and not self.exit_requested
            and not self._operation_lock.locked()
            and not self.live.error
            and not self.jog.status.get("error")
        )

    def enter_jog(self) -> bool:
        with self._jog_lock:
            if self._shutdown:
                return False
            if self.jog is None:
                return False
            if self.jog_active:
                return True
            if not self.can_enter_jog():
                print(f"[JOG] Cannot enter (state={self.state})")
                return False

            self.jog_active = True
            self.live.start()
            print("[JOG] entered")

        self._refresh_monitor()
        return True

    def exit_jog(self):
        with self._jog_lock:
            if not self.jog_active:
                return True
            release_error = None
            try:
                if self.jog is not None:
                    self.jog.release("leaving JOG mode")
            except Exception as exc:
                release_error = exc
            finally:
                self.jog_active = False
                # Live нужен и в PAUSED (коррекция ленты), и в STOPPING
                # (опустошение). Гасим поток только когда линия реально
                # стоит и не выполняет цикл.
                if self.state in ("IDLE", "STOPPED"):
                    self.live.stop()
                print("[JOG] exited")

        self._refresh_monitor()
        if release_error is not None:
            raise release_error
        return True

    def jog_hold_start(self, direction: str) -> bool:
        if not self._operation_lock.acquire(blocking=False):
            return False
        try:
            if (
                not self.jog_active
                or self.jog is None
                or self.state not in self.JOG_ALLOWED_STATES
                or self.exit_requested
                or self._selected_analysis_active
            ):
                return False
            accepted = self.jog.start_hold(direction)
            if accepted:
                label = (
                    "Ручное движение ленты вправо" if direction == "+"
                    else "Ручное движение ленты влево"
                )
                self._set_process(
                    "JOG_HOLD",
                    label,
                )
            else:
                self._refresh_monitor()
            return accepted
        finally:
            self._operation_lock.release()

    def jog_hold_heartbeat(self, direction: str) -> bool:
        if (
            not self.jog_active
            or self.jog is None
            or self.state not in self.JOG_ALLOWED_STATES
        ):
            return False
        return self.jog.heartbeat(direction)

    def jog_hold_release(self, reason: str = "button released") -> bool:
        # A delayed UI release must never stop a production Conveyor after START.
        if (
            self.jog is None
            or not self.jog_active
            or self.state not in self.JOG_ALLOWED_STATES
        ):
            return False
        accepted = self.jog.release(reason)
        if accepted:
            self._set_process("JOG_STOPPED", f"Ручное движение остановлено: {reason}")
        else:
            self._refresh_monitor()
        return accepted
