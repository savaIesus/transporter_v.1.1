"""Скрытие консольного окна при закрытии HMI.

После закрытия окна программы консоль не должна висеть до конца фоновой
очистки ресурсов. Проверяются обе платформы и аварийный возврат консоли.
"""

from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from application.console import SW_HIDE, SW_SHOW, hide_console, show_console


class FakeCtypes:
    """Заглушка ctypes для Windows-ветки без реальной консоли."""

    def __init__(self, hwnd=101):
        self.calls = []
        self.windll = SimpleNamespace(
            kernel32=SimpleNamespace(GetConsoleWindow=lambda: hwnd),
            user32=SimpleNamespace(ShowWindow=self._show_window),
        )

    def _show_window(self, hwnd, command):
        self.calls.append((hwnd, command))
        return 1


class ConsoleVisibilityTest(unittest.TestCase):
    def test_outside_windows_is_noop(self):
        with mock.patch(
            "application.console.os", SimpleNamespace(name="posix")
        ):
            self.assertFalse(hide_console())
            self.assertFalse(show_console())

    def test_windows_hide_and_show_console_window(self):
        fake = FakeCtypes(hwnd=101)
        with mock.patch(
            "application.console.os", SimpleNamespace(name="nt")
        ), mock.patch.dict(sys.modules, {"ctypes": fake}):
            self.assertTrue(hide_console())
            self.assertTrue(show_console())

        self.assertEqual(fake.calls, [(101, SW_HIDE), (101, SW_SHOW)])

    def test_windows_without_console_window_is_noop(self):
        # Запуск через pythonw: консольного окна нет вообще.
        fake = FakeCtypes(hwnd=0)
        with mock.patch(
            "application.console.os", SimpleNamespace(name="nt")
        ), mock.patch.dict(sys.modules, {"ctypes": fake}):
            self.assertFalse(hide_console())

        self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()
