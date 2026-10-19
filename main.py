"""Точка входа production-приложения трёхкамерной линии.

Сборка зависимостей и стадии жизненного цикла находятся в ``application``;
здесь остаётся только стабильный скриптовый entry point для ``run.bat``.
"""

from application.bootstrap import run_application


def main() -> None:
    run_application()


if __name__ == "__main__":
    main()
