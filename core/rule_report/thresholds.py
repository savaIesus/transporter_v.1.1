"""Пороговые отклонения правила и короткий вывод по ним."""


def _threshold_breaches(summary_cards: list) -> list:
    """Выделить только показатели, из-за которых правило не прошло.

    Карточки хранят и нормальные измерения, что полезно инженеру, но
    в анализе кадра сначала нужны факт, значение и сам порог.
    Отдельное компактное поле позволяет UI показать именно эти данные без
    разбора внутренней телеметрии правила.
    """
    breaches = []
    for card in summary_cards or []:
        for metric in card.get("metrics") or []:
            if metric.get("ok") is not False:
                continue
            breaches.append({
                "role": card.get("role", ""),
                "label": metric.get("label", "показатель"),
                "key": metric.get("key"),
                "value": metric.get("value", "—"),
                "threshold": metric.get("limit"),


            })
    return breaches


def _threshold_conclusion(
    triggered: bool, human_cause: str | None, breaches: list,
) -> str:
    """Короткий вывод, связывающий отклонение с результатом правила."""
    if not triggered:

        return "Показатели укладываются в заданные пороги"
    if breaches:
        return human_cause or "Значение вышло за заданный порог — правило сработало"
    return human_cause or "Правило сработало: проверьте причину и измерения"


def _fallback_role_status(cards: list) -> list:
    """Статус замера из карточек, если отдельный role_status не пришёл.

    По ``ok`` карточек: «В НОРМЕ» / «ОТКЛОНЕНИЕ» / «НЕТ ИЗМЕРЕНИЯ».
    """
    rows = []
    for card in cards or []:
        if not isinstance(card, dict):
            continue
        ok = card.get("ok")
        role = card.get("role", "")
        if ok is True:
            rows.append({"role": role, "status": "В НОРМЕ", "reason": None})
        elif ok is False:
            rows.append({"role": role, "status": "ОТКЛОНЕНИЕ", "reason": None})
        else:
            rows.append({"role": role, "status": "НЕТ ИЗМЕРЕНИЯ", "reason": None})
    return rows
