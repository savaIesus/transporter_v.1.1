"""Реестр форматтеров детальной телеметрии и карточек замера по правилам.

Трёхкамерная линия не использует собственных построчных форматтеров:
карточки замера собирает :mod:`core.rule_report.cards` напрямую из
``details["per_role"]`` правил, а строки сработавших ролей строит
общий сборщик :func:`core.rule_report.details.generic._generic_failure_rows`.
"""

_DETAIL_FORMATTERS: dict = {}

ROLE_METRIC_BUILDERS: dict = {}
