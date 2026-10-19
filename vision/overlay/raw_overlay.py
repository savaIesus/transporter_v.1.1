"""
Отрисовка ВСЕХ сырых детекций от нейросетей.
Показывает абсолютно всё что нашли модели,
независимо от правил.
"""

import cv2
import numpy as np

from vision.overlay.palette import (
    CLASS_COLORS,
    DEFAULT_COLOR,
    LINE_THIN,
    MASK_ALPHA,
)


class RawOverlay:
    """
    Рисует ВСЕ сырые детекции на кадре.
    Каждый класс — своим цветом из общего каталога ``palette``
    (тот же цвет, что и у объекта в режиме «ПРАВИЛА»).
    Тонкие линии, без подписей.
    """

    @staticmethod
    def render(frame, detections: list) -> np.ndarray:
        img = frame.copy()

        for det in detections:
            cls_name = det.get("class", "?")
            bbox     = det.get("bbox")
            mask     = det.get("mask")

            color = CLASS_COLORS.get(cls_name, DEFAULT_COLOR)

            if mask and len(mask) >= 3:
                RawOverlay._draw_mask(img, mask, color)
            elif bbox:
                RawOverlay._draw_bbox(img, bbox, color)

        return img

    @staticmethod
    def _draw_mask(img, mask, color):
        pts = np.array(mask, dtype=np.int32)

        overlay = img.copy()
        cv2.fillPoly(overlay, [pts], color)
        cv2.addWeighted(
            overlay, MASK_ALPHA,
            img, 1 - MASK_ALPHA,
            0, img,
        )

        cv2.polylines(img, [pts], True, color, LINE_THIN, lineType=cv2.LINE_AA)

    @staticmethod
    def _draw_bbox(img, bbox, color):
        x1, y1, x2, y2 = map(int, bbox)
        cv2.rectangle(img, (x1, y1), (x2, y2), color, LINE_THIN, lineType=cv2.LINE_AA)
