"""Зоны кадра, которые исключаются из поиска дефектов другой моделью.

Модель ``side_glass.pt`` обводит на кадре MIDDLE область, где стекло
присутствует всегда (стекло сбоку корпуса). Вторая модель
(``bottom_glass.pt``) видит то же стекло и срабатывает как «стекло на дне»,
хотя дефекта нет. Поэтому поиск стекла на дне ограничивается кадром **без**
зоны, которую нашла ``side_glass.pt``.

Модуль отвечает только за геометрию: построение зон из детекций-источников
и оценку перекрытия детекции с зонами. Решение (что делать с попавшими в
зону детекциями) принимает правило, поэтому здесь нет ни порогов, ни
обращения к моделям.

Зона и проверяемая детекция описываются одинаково — «область»
(``region``): маска-полигон (``mask``) модели сегментации, а если маски
нет — прямоугольник (``bbox``).
"""

from __future__ import annotations

import math
import numbers

import cv2
import numpy as np

MIN_POLYGON_POINTS = 3

# Оценка доли перекрытия — растеризация областей на грубой сетке ячеек.
# Сетка строится по bbox проверяемой детекции, поэтому даже сотни детекций
# в кадре (модели запускаются с iou=0, без NMS-подавления) считаются
# быстро: области рисуются только внутри этого bbox.
RASTER_CELL_PX = 20.0   # желаемый размер ячейки, px
RASTER_MIN_CELLS = 8    # не меньше ячеек по короткой стороне детекции
RASTER_MAX_CELLS = 96   # не больше ячеек по длинной стороне
MAX_RASTER_VERTICES = 128   # контур прореживается: на грубой сетке точнее не нужно

# Детекция считается попавшей в зону, если зона накрывает не менее этой
# доли её площади (0.5 — «больше половины»).
DEFAULT_MIN_COVERAGE = 0.5


def _finite(value):
    """Конечное число из значения детекции или ``None``.

    Принимаются и numpy-скаляры: детекции приходят либо из
    ``VisionCluster`` (там уже ``float``), либо из тестов/симулятора.
    """
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def polygon_area(points) -> float:
    """Площадь многоугольника (формула шнурков), абсолютное значение."""
    if points is None or len(points) < MIN_POLYGON_POINTS:
        return 0.0
    points = list(points)
    total = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]):
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def point_in_polygon(point, points) -> bool:
    """Тест луча: точка внутри многоугольника (граница — снаружи)."""
    x, y = point
    inside = False
    count = len(points)
    if count < MIN_POLYGON_POINTS:
        return False
    j = count - 1
    for i in range(count):
        xi, yi = points[i]
        xj, yj = points[j]
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def bbox_area(bbox) -> float:
    """Площадь прямоугольника; невалидный прямоугольник — 0."""
    if bbox is None or len(bbox) != 4:
        return 0.0
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def _is_sequence(value) -> bool:
    """Список/кортеж/массив, но не строка и не словарь."""
    return (
        isinstance(value, (list, tuple, np.ndarray))
        and not isinstance(value, (str, bytes))
    )


def _as_polygon(det):
    """Маска детекции как список точек или ``None``, если маски нет."""
    mask = det.get("mask")
    if not _is_sequence(mask) or len(mask) < MIN_POLYGON_POINTS:
        return None
    points = []
    for item in mask:
        if not _is_sequence(item) or len(item) < 2:
            return None
        x, y = _finite(item[0]), _finite(item[1])
        if x is None or y is None:
            return None
        points.append((x, y))
    return points if polygon_area(points) > 0 else None


def _as_bbox(det):
    """bbox детекции как ``(x1, y1, x2, y2)`` или ``None``."""
    bbox = det.get("bbox")
    if not _is_sequence(bbox) or len(bbox) != 4:
        return None
    values = [_finite(value) for value in bbox]
    if any(value is None for value in values):
        return None
    x1, y1, x2, y2 = values
    x1, x2 = min(x1, x2), max(x1, x2)
    y1, y2 = min(y1, y2), max(y1, y2)
    if x2 <= x1 or y2 <= y1:
        return None
    return (x1, y1, x2, y2)


def detection_region(det):
    """Область детекции: ``{"bbox", "polygon", "area"}`` или ``None``.

    Маска приоритетнее bbox: зона ``side_glass.pt`` обводится именно
    маской, а bbox — только запасной вариант для моделей без маски и для
    ручных/тестовых детекций.
    """
    if not isinstance(det, dict):
        return None
    polygon = _as_polygon(det)
    bbox = _as_bbox(det)
    if polygon is None and bbox is None:
        return None
    if bbox is None:
        xs = [point[0] for point in polygon]
        ys = [point[1] for point in polygon]
        bbox = (min(xs), min(ys), max(xs), max(ys))
    return {
        "bbox": bbox,
        "polygon": polygon,
        "area": polygon_area(polygon) if polygon else bbox_area(bbox),
    }


def region_contains(region, point) -> bool:
    """Точка внутри области (сначала быстрая проверка по bbox)."""
    if region is None:
        return False
    x1, y1, x2, y2 = region["bbox"]
    x, y = point
    if not (x1 <= x <= x2 and y1 <= y <= y2):
        return False
    polygon = region.get("polygon")
    if polygon is None:
        return True
    return point_in_polygon(point, polygon)


def build_zones(detections) -> list:
    """Построить зоны исключения из детекций-источников.

    Детекции без валидной геометрии пропускаются: зона не строится, поиск
    дефектов продолжается по всему кадру (fail-open — исключение не должно
    «съедать» кадр из-за битой маски).
    """
    zones = []
    for det in detections or []:
        region = detection_region(det)
        if region is not None:
            zones.append(region)
    return zones


def _simplified(points) -> list:
    """Проредить плотный контур маски до ``MAX_RASTER_VERTICES`` вершин.

    Маски ``retina_masks`` приходят контурами в сотни точек; для сетки
    ячеек размером ~20 px такая точность избыточна, а прореживание
    убирает основную стоимость растеризации на кадрах с десятками
    детекций (модели запускаются с ``iou=0``, без NMS).
    """
    count = len(points)
    if count <= MAX_RASTER_VERTICES:
        return list(points)
    step = math.ceil(count / MAX_RASTER_VERTICES)
    return list(points[::step])


def _raster_geometry(bbox) -> dict | None:
    """Параметры сетки ячеек, покрывающей bbox области."""
    x1, y1, x2, y2 = bbox
    width, height = x2 - x1, y2 - y1
    if width <= 0 or height <= 0:
        return None
    cell = min(RASTER_CELL_PX, width / RASTER_MIN_CELLS, height / RASTER_MIN_CELLS)
    cell = max(cell, width / RASTER_MAX_CELLS, height / RASTER_MAX_CELLS)
    if cell <= 0 or not math.isfinite(cell):
        return None
    return {
        "x1": x1,
        "y1": y1,
        "cell": cell,
        "cols": max(1, min(RASTER_MAX_CELLS, int(math.ceil(width / cell)))),
        "rows": max(1, min(RASTER_MAX_CELLS, int(math.ceil(height / cell)))),
    }


def _rasterize(region, geometry) -> np.ndarray:
    """Ячейки области в сетке ``geometry`` (cv2 обрезает всё за её краем).

    Если маска тоньше ячейки (или её нет), область рисуется по bbox: так
    узкая зона не теряется при растеризации.
    """
    canvas = np.zeros((geometry["rows"], geometry["cols"]), dtype=np.uint8)
    polygon = region.get("polygon")
    if polygon is not None:
        size = geometry["cell"]
        points = np.array([
            [
                int(round((x - geometry["x1"]) / size)),
                int(round((y - geometry["y1"]) / size)),
            ]
            for x, y in _simplified(polygon)
        ], dtype=np.int32)
        cv2.fillPoly(canvas, [points], 1)
    if not canvas.any():
        x1, y1, x2, y2 = region["bbox"]
        size = geometry["cell"]
        left = int(math.floor((x1 - geometry["x1"]) / size))
        top = int(math.floor((y1 - geometry["y1"]) / size))
        right = max(int(math.ceil((x2 - geometry["x1"]) / size)) - 1, left)
        bottom = max(int(math.ceil((y2 - geometry["y1"]) / size)) - 1, top)
        cv2.rectangle(canvas, (left, top), (right, bottom), 1, -1)
    return canvas


def _fallback_coverage(region, zones) -> float:
    """Доля перекрытия для области, не попавшей на сетку: по её центру."""
    x1, y1, x2, y2 = region["bbox"]
    center = ((x1 + x2) / 2.0, (y1 + y2) / 2.0)
    return 1.0 if any(region_contains(zone, center) for zone in zones) else 0.0


def coverage_fraction(region, zones) -> float:
    """Доля площади детекции внутри объединения зон (0..1).

    Обе стороны растеризуются на общей сетке, построенной по bbox детекции:
    доля считается по ячейкам, реально занятым маской детекции, поэтому у
    маски произвольной формы учитывается именно её площадь.
    """
    if region is None or not zones:
        return 0.0
    geometry = _raster_geometry(region["bbox"])
    if geometry is None:
        return 0.0
    detection_cells = _rasterize(region, geometry)
    total = int(np.count_nonzero(detection_cells))
    if not total:
        return _fallback_coverage(region, zones)
    zone_cells = np.zeros_like(detection_cells)
    for zone in zones:
        zone_cells |= _rasterize(zone, geometry)
    covered = int(np.count_nonzero(detection_cells & zone_cells))
    return covered / total


def split_by_zones(detections, zones, min_coverage: float = DEFAULT_MIN_COVERAGE):
    """Разделить детекции на оставшиеся и попавшие в зоны исключения.

    ``min_coverage`` — минимальная доля площади детекции внутри зон, при
    которой она исключается. ``min_coverage <= 0`` или пустые зоны
    отключают исключение (поведение «как до зоны»).

    Возвращает ``(kept, excluded)``; у исключённых копий появляются поля
    ``excluded_by_zone`` и ``zone_coverage`` — для отчёта и разметки.
    """
    detections = list(detections or [])
    if not zones or min_coverage <= 0:
        return detections, []

    threshold = float(min_coverage)
    kept, excluded = [], []
    for det in detections:
        coverage = coverage_fraction(detection_region(det), zones)
        if coverage >= threshold:
            excluded_det = dict(det)
            excluded_det["excluded_by_zone"] = True
            excluded_det["zone_coverage"] = round(coverage, 4)
            excluded.append(excluded_det)
        else:
            kept.append(det)
    return kept, excluded
