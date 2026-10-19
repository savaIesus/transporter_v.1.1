import cv2
import numpy as np

# Цвета отрисовки debug-оверлея — из общего каталога ``palette``:
# статусные (PASS/FAIL/SKIP) и объектные (те же, что у детекций в
# режиме «МОДЕЛИ»).
from vision.overlay.palette import (
    COLOR_FAIL,
    COLOR_GLASS,
    COLOR_PASS,
    COLOR_PLATFORM,
    COLOR_SKIP,
    LINE_THIN,
    MASK_ALPHA,
)

FONT   = cv2.FONT_HERSHEY_SIMPLEX
FONT_S = 0.4
THICK  = 1


class DrawPrimitives:
    @staticmethod
    def pick_color(d):
        color_hint = d.get("color_hint")
        if color_hint == "glass":
            # Стекло сохраняет объектный цвет даже при сработавшем
            # правиле — как на семикамерной линии (top_glass_bad_glass):
            # объект остаётся «стеклом», статус выражает толщина/заливка.
            return COLOR_GLASS
        if d.get("triggered"):
            return COLOR_FAIL
        if color_hint == "platform":
            return COLOR_PLATFORM
        if color_hint == "skip":
            return COLOR_SKIP
        return COLOR_PASS

    @staticmethod
    def draw_rule_bbox(img, d):
        color = DrawPrimitives.pick_color(d)
        x1, y1, x2, y2 = map(int, d["bbox"])
        mask = d.get("mask")
        has_mask = mask and len(mask) >= 3
        thickness = LINE_THIN

        if has_mask:
            pts = np.array(mask, dtype=np.int32)
            overlay = img.copy()
            if d.get("color_hint") == "glass":
                # Заливка маски стекла — объектный цвет, как в сыром
                # оверлее режима «МОДЕЛИ».
                fill_color = COLOR_GLASS
            else:
                fill_color = COLOR_FAIL if d.get("triggered") else (60, 60, 60)
            cv2.fillPoly(overlay, [pts], fill_color)
            cv2.addWeighted(overlay, MASK_ALPHA, img, 1 - MASK_ALPHA, 0, img)
            cv2.polylines(img, [pts], True, color, thickness, lineType=cv2.LINE_AA)
        else:
            cv2.rectangle(img, (x1, y1), (x2, y2), color, thickness, lineType=cv2.LINE_AA)

    @staticmethod
    def draw_text_with_bg(img, text, pos, color, font_scale=FONT_S, bg_pad=2, center_x=False):
        x, y = pos
        (tw, th), _ = cv2.getTextSize(text, FONT, font_scale, THICK)
        if center_x:
            x = x - tw // 2
        x1, y1, x2, y2 = x - bg_pad, y - th - bg_pad, x + tw + bg_pad, y + bg_pad
        cv2.rectangle(img, (x1, y1), (x2, y2), (0, 0, 0), -1, lineType=cv2.LINE_AA)
        cv2.putText(img, text, (x, y), FONT, font_scale, color, THICK)

    @staticmethod
    def draw_dashed_line(img, pt1, pt2, color, thickness, dash_len=8):
        x1, y1 = pt1
        x2, y2 = pt2
        dist = np.hypot(x2 - x1, y2 - y1)
        if dist < 1:
            return
        dashes = int(dist / dash_len)
        if dashes < 1:
            cv2.line(img, pt1, pt2, color, thickness)
            return
        for i in range(0, dashes, 2):
            t1, t2 = i / dashes, min((i + 1) / dashes, 1.0)
            p1 = (int(x1 + (x2 - x1) * t1), int(y1 + (y2 - y1) * t1))
            p2 = (int(x1 + (x2 - x1) * t2), int(y1 + (y2 - y1) * t2))
            cv2.line(img, p1, p2, color, thickness)
