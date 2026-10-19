"""Отрисовка замера разновысотности: вертикальная секущая и значение H.

Перенос визуализации трёхкамерника: синяя измерительная линия через
середину ячейки, подпись ``H:<высота px>``; при браке — красная линия.
"""

import cv2

from vision.overlay.renderers.primitives import (
    COLOR_PASS, COLOR_FAIL, DrawPrimitives, LINE_THIN,
)

COLOR_MEASURE = (255, 0, 0)  # синяя линия замера (как в трёхкамернике)


class WindowHeightRenderer:

    @staticmethod
    def draw_measure(img, d):
        x = int(d.get("x", 0))
        y_top = int(d.get("y_top", 0))
        y_bottom = int(d.get("y_bottom", 0))
        height = d.get("height")
        triggered = bool(d.get("triggered"))

        line_color = COLOR_FAIL if triggered else COLOR_MEASURE
        cv2.line(
            img, (x, y_top), (x, y_bottom),
            line_color, LINE_THIN, cv2.LINE_AA,
        )

        if height is not None:
            label = f"H:{float(height):.0f}"
            pos = (x + 5, (y_top + y_bottom) // 2)
            DrawPrimitives.draw_text_with_bg(
                img, label, pos,
                COLOR_FAIL if triggered else COLOR_PASS,
            )
