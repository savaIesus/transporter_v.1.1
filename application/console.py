"""Управление окном консоли production-приложения.

После закрытия окна HMI очистка ресурсов (остановка цикла, освобождение
камер, закрытие COM-порта, сжатие архива, отправка ``batch.json``
аналитику) продолжается ещё несколько секунд. Чтобы оператор не наблюдал
«зависшее» чёрное окно, консоль скрывается в момент закрытия окна, и
завершение проходит незаметно.

При аварийном выходе консоль возвращается на экран: ``run.bat`` печатает
причину и ждёт клавишу, поэтому ошибка не должна остаться в скрытом окне.
Вне Windows (тесты на Linux) функции ничего не делают.
"""

from __future__ import annotations

import os


SW_HIDE = 0
SW_SHOW = 5


def hide_console() -> bool:
    """Скрыть окно консоли. Возвращает ``True``, если окно было скрыто."""
    return _set_console_visibility(SW_HIDE)


def show_console() -> bool:
    """Вернуть окно консоли на экран перед аварийным выходом."""
    return _set_console_visibility(SW_SHOW)


def _set_console_visibility(command: int) -> bool:
    if os.name != "nt":
        return False
    import ctypes

    hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    if not hwnd:
        # Процесс запущен без консоли (pythonw) -- скрывать нечего.
        return False
    return bool(ctypes.windll.user32.ShowWindow(hwnd, command))
