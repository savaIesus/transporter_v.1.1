"""Полигоны разметки и экспорт собранной партии в YOLO."""

from __future__ import annotations

import json
import random
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import cv2
import numpy as np


@dataclass(frozen=True)
class PolygonAnnotation:
    """Контур одного элемента в пикселях исходного изображения."""

    class_name: str
    points: tuple[tuple[float, float], ...]

    @classmethod
    def from_payload(cls, payload: Any) -> "PolygonAnnotation":
        if not isinstance(payload, dict):
            raise ValueError("Полигон должен быть объектом")
        class_name = str(
            payload.get("class_name", payload.get("class", ""))
        ).strip()
        raw_points = payload.get("points")
        if not isinstance(raw_points, (list, tuple)):
            raise ValueError("У полигона нет списка points")
        points = []
        for raw_point in raw_points:
            if isinstance(raw_point, dict):
                points.append((float(raw_point["x"]), float(raw_point["y"])))
            elif isinstance(raw_point, (list, tuple)) and len(raw_point) == 2:
                points.append((float(raw_point[0]), float(raw_point[1])))
            else:
                raise ValueError("Точка должна содержать x и y")
        return cls(class_name=class_name, points=tuple(points))

    def to_dict(self) -> dict[str, Any]:
        return {
            "class": self.class_name,
            "points": [
                {"x": float(x), "y": float(y)}
                for x, y in self.points
            ],
        }


@dataclass(frozen=True)
class YoloExport:
    """Результат подготовки структуры для Ultralytics/YOLO."""

    dataset_folder: str
    yaml_path: str
    train_count: int
    validation_count: int
    skipped_count: int
    class_names: tuple[str, ...]
    task: str = "segment"

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_folder": self.dataset_folder,
            "yaml_path": self.yaml_path,
            "train_count": self.train_count,
            "validation_count": self.validation_count,
            "skipped_count": self.skipped_count,
            "class_names": list(self.class_names),
            "task": self.task,
        }


def normalise_box(
    box: Any,
    *,
    image_width: int,
    image_height: int,
) -> tuple[float, float, float, float]:
    """Вернуть clipped ``x1, y1, x2, y2`` для legacy bounding box."""
    try:
        if isinstance(box, dict):
            if all(key in box for key in ("x", "y")) and (
                "width" in box or "w" in box
            ) and ("height" in box or "h" in box):
                x = float(box["x"])
                y = float(box["y"])
                width = float(box.get("width", box.get("w")))
                height = float(box.get("height", box.get("h")))
            else:
                raw_bbox = box.get("bbox")
                if not isinstance(raw_bbox, (list, tuple)) or len(raw_bbox) != 4:
                    raise KeyError("x, y, width, height")
                x1, y1, x2, y2 = (float(value) for value in raw_bbox)
                x, y = x1, y1
                width, height = x2 - x1, y2 - y1
        else:
            x = float(box.x)
            y = float(box.y)
            width = float(box.width)
            height = float(box.height)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("Некорректная bounding box") from exc
    if not all(np.isfinite(value) for value in (x, y, width, height)):
        raise ValueError("Координаты bounding box должны быть конечными")
    if image_width <= 0 or image_height <= 0:
        raise ValueError("Размер изображения должен быть положительным")
    x1, x2 = sorted((x, x + width))
    y1, y2 = sorted((y, y + height))
    x1 = max(0.0, min(float(image_width), x1))
    y1 = max(0.0, min(float(image_height), y1))
    x2 = max(0.0, min(float(image_width), x2))
    y2 = max(0.0, min(float(image_height), y2))
    if x2 - x1 < 1.0 or y2 - y1 < 1.0:
        raise ValueError("Область дефекта слишком мала")
    return x1, y1, x2, y2


def normalise_polygon(
    polygon: Any,
    *,
    image_width: int,
    image_height: int,
) -> tuple[tuple[float, float], ...]:
    """Проверить и обрезать polygon в пикселях исходного изображения."""
    if isinstance(polygon, dict):
        raw_points = polygon.get("points")
    else:
        raw_points = getattr(polygon, "points", None)
    if raw_points is None:
        x1, y1, x2, y2 = normalise_box(
            polygon,
            image_width=image_width,
            image_height=image_height,
        )
        raw_points = ((x1, y1), (x2, y1), (x2, y2), (x1, y2))
    if not isinstance(raw_points, (list, tuple)):
        raise ValueError("У полигона должен быть список points")
    raw_points = list(raw_points)

    if len(raw_points) >= 2 and _same_point(raw_points[0], raw_points[-1]):
        raw_points = raw_points[:-1]
    if len(raw_points) < 3:
        raise ValueError("Область должна содержать минимум три точки")
    if image_width <= 0 or image_height <= 0:
        raise ValueError("Размер изображения должен быть положительным")

    points = []
    for raw_point in raw_points:
        try:
            if isinstance(raw_point, dict):
                x = float(raw_point["x"])
                y = float(raw_point["y"])
            else:
                x = float(raw_point[0])
                y = float(raw_point[1])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValueError("Каждая точка должна содержать x и y") from exc
        if not np.isfinite(x) or not np.isfinite(y):
            raise ValueError("Координаты точки должны быть конечными")
        points.append(
            (
                max(0.0, min(float(image_width), x)),
                max(0.0, min(float(image_height), y)),
            )
        )

    # Повторяющиеся соседние точки ломают полигон и не несут информации.
    compact = []
    for point in points:
        if not compact or not _same_point(compact[-1], point):
            compact.append(point)
    if len(compact) >= 2 and _same_point(compact[0], compact[-1]):
        compact.pop()
    if len(compact) < 3 or _polygon_area(compact) < 1.0:
        raise ValueError("Площадь области слишком мала")
    return tuple(compact)


def annotation_to_yolo_lines(
    annotations: Sequence[Any],
    *,
    image_width: int,
    image_height: int,
    class_names: Sequence[str],
    task: str = "segment",
) -> list[str]:
    """Преобразовать полигоны в строки YOLO segmentation.

    ``BoundingBox`` остаётся поддерживаемым legacy-входом и превращается в
    прямоугольный polygon. Режим ``detect`` оставлен для внешних интеграций,
    но мастер по умолчанию экспортирует именно segmentation-разметку.
    """
    if task not in ("segment", "detect"):
        raise ValueError("task должен быть segment или detect")
    indices = {name: index for index, name in enumerate(class_names)}
    lines = []
    for annotation in annotations:
        class_name = _annotation_class_name(annotation)
        if class_name not in indices:
            raise ValueError(f"Неизвестный класс YOLO: {class_name}")
        points = normalise_polygon(
            annotation,
            image_width=image_width,
            image_height=image_height,
        )
        if task == "detect":
            x_values = [point[0] for point in points]
            y_values = [point[1] for point in points]
            x1, x2 = min(x_values), max(x_values)
            y1, y2 = min(y_values), max(y_values)
            values = (
                (x1 + x2) / 2.0 / image_width,
                (y1 + y2) / 2.0 / image_height,
                (x2 - x1) / image_width,
                (y2 - y1) / image_height,
            )
        else:
            values = tuple(
                coordinate / size
                for point in points
                for coordinate, size in (
                    (point[0], image_width),
                    (point[1], image_height),
                )
            )
        lines.append(
            f"{indices[class_name]} "
            + " ".join(f"{value:.6f}" for value in values)
        )
    return lines


def draw_annotations(
    image: np.ndarray,
    annotations: Sequence[Any],
    *,
    image_width: int,
    image_height: int,
) -> np.ndarray:
    """Создать отдельную копию кадра с polygon-контурами и подписями."""
    marked = np.ascontiguousarray(image.copy())
    for index, annotation in enumerate(annotations):
        points = normalise_polygon(
            annotation,
            image_width=image_width,
            image_height=image_height,
        )
        color = (
            int((37 * (index + 1)) % 255),
            int((97 * (index + 1)) % 255),
            int((173 * (index + 1)) % 255),
        )
        polygon = np.asarray(
            [[int(round(x)), int(round(y))] for x, y in points],
            dtype=np.int32,
        )
        overlay = marked.copy()
        cv2.fillPoly(overlay, [polygon], color)
        marked = cv2.addWeighted(overlay, 0.22, marked, 0.78, 0)
        cv2.polylines(marked, [polygon], isClosed=True, color=color, thickness=3)

        label = _annotation_class_name(annotation) or "defect"
        left = int(min(point[0] for point in points))
        top = int(min(point[1] for point in points))
        (text_width, text_height), baseline = cv2.getTextSize(
            label,
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            2,
        )
        label_top = max(0, top - text_height - baseline - 6)
        label_bottom = max(text_height + baseline + 2, top)
        cv2.rectangle(
            marked,
            (left, label_top),
            (left + text_width + 8, label_bottom),
            color,
            -1,
        )
        cv2.putText(
            marked,
            label,
            (left + 4, label_bottom - baseline - 2),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
    return marked


def export_yolo_dataset(
    store: Any,
    *,
    validation_ratio: float | None = None,
    seed: int = 42,
) -> YoloExport:
    """Скопировать размеченные кадры в готовую train/val-структуру YOLO."""
    class_names = tuple(store.config.defect_classes)
    task = getattr(store.config, "yolo_task", "segment")
    ratio = (
        store.config.validation_ratio
        if validation_ratio is None
        else float(validation_ratio)
    )
    if task not in ("segment", "detect"):
        raise ValueError("task должен быть segment или detect")
    if not 0.0 <= ratio < 1.0:
        raise ValueError("validation_ratio должен быть в диапазоне [0, 1)")

    yolo_root = Path(store.yolo_folder)
    if yolo_root.exists():
        shutil.rmtree(yolo_root)
    train_images = yolo_root / "images" / "train"
    train_labels = yolo_root / "labels" / "train"
    val_images = yolo_root / "images" / "val"
    val_labels = yolo_root / "labels" / "val"
    for folder in (train_images, train_labels, val_images, val_labels):
        folder.mkdir(parents=True, exist_ok=True)

    records = [
        record
        for record in store.get_frames()
        if record.status == "annotated"
    ]
    skipped_count = store.frame_count - len(records)
    groups: dict[int, list[Any]] = {}
    for record in records:
        groups.setdefault(int(record.part_id), []).append(record)
    part_ids = sorted(groups)
    random.Random(seed).shuffle(part_ids)

    validation_parts: set[int] = set()
    if ratio > 0 and len(part_ids) >= 2:
        count = max(1, int(round(len(part_ids) * ratio)))
        count = min(count, len(part_ids) - 1)
        validation_parts = set(part_ids[:count])

    train_count = 0
    validation_count = 0
    split_rows = []
    for part_id in part_ids:
        split = "val" if part_id in validation_parts else "train"
        image_folder = val_images if split == "val" else train_images
        label_folder = val_labels if split == "val" else train_labels
        for record in sorted(groups[part_id], key=lambda item: item.image_id):
            source_image = Path(store.session_folder) / record.original
            source_label = Path(store.session_folder) / record.label
            if not source_image.exists() or not source_label.exists():
                raise RuntimeError(
                    f"Файлы кадра #{record.image_id} отсутствуют перед экспортом"
                )
            shutil.copy2(source_image, image_folder / source_image.name)
            shutil.copy2(source_label, label_folder / source_label.name)
            split_rows.append(
                {
                    "image_id": record.image_id,
                    "part_id": record.part_id,
                    "camera_role": record.camera_role,
                    "split": split,
                    "image": str(
                        (image_folder / source_image.name)
                        .relative_to(store.session_folder)
                        .as_posix()
                    ),
                    "label": str(
                        (label_folder / source_label.name)
                        .relative_to(store.session_folder)
                        .as_posix()
                    ),
                }
            )
            if split == "val":
                validation_count += 1
            else:
                train_count += 1

    yaml_path = yolo_root / "dataset.yaml"
    dataset_root = str(store.session_folder.resolve()).replace("\\", "/")
    val_path = "yolo/images/val" if validation_parts else "yolo/images/train"
    yaml_lines = [
        f"path: {json.dumps(dataset_root, ensure_ascii=False)}",
        f"task: {task}",
        "train: yolo/images/train",
        f"val: {val_path}",
        "names:",
    ]
    yaml_lines.extend(
        f"  {index}: {json.dumps(name, ensure_ascii=False)}"
        for index, name in enumerate(class_names)
    )
    _write_text(yaml_path, "\n".join(yaml_lines) + "\n")
    _write_json(
        yolo_root / "classes.json",
        {"names": list(class_names), "nc": len(class_names), "task": task},
    )
    _write_json(
        yolo_root / "split.json",
        {
            "seed": seed,
            "task": task,
            "validation_ratio": ratio,
            "validation_parts": sorted(validation_parts),
            "items": split_rows,
        },
    )
    return YoloExport(
        dataset_folder=str(yolo_root),
        yaml_path=str(yaml_path),
        train_count=train_count,
        validation_count=validation_count,
        skipped_count=skipped_count,
        class_names=class_names,
        task=task,
    )


def _annotation_class_name(annotation: Any) -> str:
    if isinstance(annotation, dict):
        return str(
            annotation.get("class_name", annotation.get("class", ""))
        ).strip()
    return str(getattr(annotation, "class_name", "")).strip()


def _same_point(left: Any, right: Any) -> bool:
    try:
        if isinstance(left, dict):
            left_values = (left["x"], left["y"])
        else:
            left_values = (left[0], left[1])
        if isinstance(right, dict):
            right_values = (right["x"], right["y"])
        else:
            right_values = (right[0], right[1])
        return (
            float(left_values[0]) == float(right_values[0])
            and float(left_values[1]) == float(right_values[1])
        )
    except (IndexError, KeyError, TypeError, ValueError):
        return False


def _polygon_area(points: Sequence[tuple[float, float]]) -> float:
    return abs(
        sum(
            points[index][0] * points[(index + 1) % len(points)][1]
            - points[(index + 1) % len(points)][0] * points[index][1]
            for index in range(len(points))
        )
        / 2.0
    )


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _write_json(path: Path, payload: Any) -> None:
    _write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )
