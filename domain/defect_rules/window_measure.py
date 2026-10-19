"""Измерение высоты ячеек окон по маске сегментации.

Перенос алгоритма трёхкамерника (transporter):
  * по контуру окна проводится вертикальная секущая через середину
    bounding-box по X;
  * считаются все пересечения прямой с рёбрами контура;
  * верх — первое пересечение, низ — первое с зазором > min_gap_px
    (иначе последнее);
  * высота ячейки — длина полученного отрезка в пикселях.

Резервный вариант (если двух пересечений не нашлось) — оценка через
measure_window_height: высота от вершины контура до точки контура под
серединой по X.
"""

from __future__ import annotations

import numpy as np


def _contour_points(mask) -> np.ndarray:
    """Привести маску детекции к массиву точек (N, 2) int."""
    pts = np.asarray(mask, dtype=float)
    if pts.ndim == 3:
        pts = pts[:, 0, :]
    return pts


def get_vertical_intersections(contour, x: float) -> list:
    """Все y-координаты пересечений вертикали x=x с контуром.

    Учитывает вертикальные рёбра (добавляет оба конца). Порт функции
    transporter/utils/intersections.py:get_vertical_intersections.
    """
    intersections = []
    n = len(contour)
    for i in range(n):
        pt1 = contour[i]
        pt2 = contour[(i + 1) % n]

        # Вертикальное ребро точно на x — добавляем оба конца.
        if abs(pt2[0] - pt1[0]) < 1e-5 and abs(pt1[0] - x) < 1e-5:
            intersections.append(pt1[1])
            intersections.append(pt2[1])
            continue

        # Обычное пересечение ребра.
        if (pt1[0] <= x <= pt2[0]) or (pt2[0] <= x <= pt1[0]):
            denom = pt2[0] - pt1[0]
            if abs(denom) < 1e-5:
                continue
            t = (x - pt1[0]) / denom
            y = pt1[1] + t * (pt2[1] - pt1[1])
            intersections.append(y)

    return sorted(set(intersections))


def measure_window_height(contour) -> tuple:
    """Резервный замер высоты ячейки: вершина -> точка под серединой по X.

    Порт transporter/processing/process_frame.py:measure_window_height.
    """
    seg = _contour_points(contour)
    if seg.size == 0:
        return None, None
    xs = seg[:, 0]
    ys = seg[:, 1]
    x_min, x_max = int(xs.min()), int(xs.max())
    y_min = int(ys.min())
    x_mid = int((x_min + x_max) / 2)

    nears = seg[
        (seg[:, 0] >= x_mid - 10)
        & (seg[:, 0] <= x_mid + 10)
        & (seg[:, 1] > y_min + 3)
    ]
    if nears.size == 0:
        y_mid = int(ys[seg[:, 0].argmin()]) if seg.shape[0] else y_min
        nears = np.array([[x_mid, y_mid]], dtype=int)
    y_mid = int(nears[:, 1].min())
    height = y_mid - y_min
    return height, (x_mid, y_min, y_mid)


def measure_window_by_intersections(mask, min_gap_px: float = 7.0) -> dict | None:
    """Основной замер: вертикальная секущая через середину bbox по X.

    Возвращает dict:
        height   — длина отрезка в px;
        x        — x секущей;
        y_top    — верхняя точка;
        y_bottom — нижняя точка;
        intersections — число пересечений (-1 = резервный замер).
    """
    seg = _contour_points(mask)
    if seg.size == 0 or len(seg) < 3:
        return None

    xs = seg[:, 0]
    x_min, x_max = int(np.min(xs)), int(np.max(xs))
    x_center = x_min + (x_max - x_min) // 2

    ys = get_vertical_intersections(seg, x_center)

    if len(ys) >= 2:
        ys_sorted = sorted(ys)
        y_top = int(ys_sorted[0])
        count = len(ys_sorted)
        y_bottom = None
        if len(ys_sorted) == 2:
            y_bottom = int(ys_sorted[1])
        else:
            for y in ys_sorted[1:]:
                if y - y_top > min_gap_px:
                    y_bottom = int(y)
                    break
            if y_bottom is None:
                y_bottom = int(ys_sorted[-1])
        return {
            "height": float(y_bottom - y_top),
            "x": int(x_center),
            "y_top": y_top,
            "y_bottom": y_bottom,
            "intersections": count,
        }

    # Резервный вариант (два пересечения не найдены).
    h_val, coord = measure_window_height(seg)
    if h_val is None or coord is None:
        return None
    x_mid, y_top, y_bottom = coord
    return {
        "height": float(h_val),
        "x": int(x_mid),
        "y_top": int(y_top),
        "y_bottom": int(y_bottom),
        "intersections": -1,
    }
