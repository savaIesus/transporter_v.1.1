"""Доменная логика мастера сбора изображений для обучения.

Здесь нет привязки к FastAPI или к конкретному UI. Коллектор получает кадр
через небольшой протокол ``CameraFrameSource``, сразу сохраняет неизменённый
оригинал, а после решения оператора создаёт отдельную картинку с разметкой и
YOLO label-файл. Это позволяет использовать тот же класс в HMI, в отдельном
веб-мастере и в автоматических тестах.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, Sequence, runtime_checkable

import cv2
import numpy as np

from training.yolo import (
    PolygonAnnotation,
    annotation_to_yolo_lines,
    draw_annotations,
    normalise_polygon,
)


DEFAULT_CAMERA_ROLES = (
    "INPUT_LEFT",
    "INPUT_RIGHT",
    "SPIDER_LEFT",
    "SPIDER_RIGHT",
    "SPIDER_IN",
    "SPIDER_OUT",
    "TOP",
)

# Список можно переопределить через форму мастера. Названия намеренно
# нейтральные: конкретный набор моделей может иметь собственную таксономию.
DEFAULT_DEFECT_CLASSES = (
    "glass",
    "black_spot",
    "window_sink",
    "long_omission",
    "short_omission",
    "missing_contact",
    "other",
)

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


class CollectorError(RuntimeError):
    """Ошибка состояния или сохранения набора обучения."""


class ValidationError(CollectorError, ValueError):
    """Некорректные данные оператора или изображения."""


@runtime_checkable
class CameraFrameSource(Protocol):
    """Минимальный интерфейс источника кадров.

    ``CameraManager`` из production-приложения уже реализует этот API через
    ``capture_single(role)``. Протокол позволяет не импортировать и не
    запускать производственный цикл внутри мастера обучения.
    """

    def capture_single(self, role: str) -> Any:
        """Вернуть свежий BGR-кадр выбранной роли."""


class CollectorCameraSource:
    """Адаптер для явного описания зависимости от CameraManager."""

    def __init__(self, cameras: CameraFrameSource):
        if not hasattr(cameras, "capture_single"):
            raise TypeError("Источник камер должен иметь capture_single(role)")
        self.cameras = cameras

    def capture_single(self, role: str):
        return self.cameras.capture_single(role)


@dataclass(frozen=True)
class TrainingConfig:
    """Настройки одной партии учебных изображений."""

    root_folder: str | os.PathLike[str] = "training_datasets"
    camera_roles: tuple[str, ...] = DEFAULT_CAMERA_ROLES
    defect_classes: tuple[str, ...] = DEFAULT_DEFECT_CLASSES
    jpeg_quality: int = 98
    validation_ratio: float = 0.2
    yolo_task: str = "segment"

    def __post_init__(self) -> None:
        root = str(self.root_folder).strip()
        if not root:
            raise ValidationError("Не указана папка датасетов")
        roles = _normalise_roles(self.camera_roles)
        classes = _normalise_classes(self.defect_classes)
        quality = int(self.jpeg_quality)
        if not 1 <= quality <= 100:
            raise ValidationError("Качество JPEG должно быть от 1 до 100")
        ratio = float(self.validation_ratio)
        if not 0.0 <= ratio < 1.0:
            raise ValidationError("validation_ratio должен быть в диапазоне [0, 1)")
        task = str(self.yolo_task or "segment").strip().lower()
        if task not in ("segment", "detect"):
            raise ValidationError("yolo_task должен быть segment или detect")
        object.__setattr__(self, "root_folder", root)
        object.__setattr__(self, "camera_roles", roles)
        object.__setattr__(self, "defect_classes", classes)
        object.__setattr__(self, "jpeg_quality", quality)
        object.__setattr__(self, "validation_ratio", ratio)
        object.__setattr__(self, "yolo_task", task)


@dataclass(frozen=True)
class BoundingBox:
    """Прямоугольная область дефекта в пикселях исходного кадра."""

    class_name: str
    x: float
    y: float
    width: float
    height: float

    @classmethod
    def from_payload(cls, payload: Any) -> "BoundingBox":
        if isinstance(payload, BoundingBox):
            return payload
        if not isinstance(payload, dict):
            raise ValidationError("Область дефекта должна быть объектом")
        class_name = str(
            payload.get("class_name", payload.get("class", ""))
        ).strip()
        try:
            values = (
                payload.get("x"),
                payload.get("y"),
                payload.get("width", payload.get("w")),
                payload.get("height", payload.get("h")),
            )
            if any(value is None for value in values):
                # Допускаем привычный [x1, y1, x2, y2] для интеграций без UI.
                raw_bbox = payload.get("bbox")
                if isinstance(raw_bbox, (list, tuple)) and len(raw_bbox) == 4:
                    x1, y1, x2, y2 = (float(value) for value in raw_bbox)
                    values = (x1, y1, x2 - x1, y2 - y1)
            x, y, width, height = (float(value) for value in values)
        except (TypeError, ValueError) as exc:
            raise ValidationError("Координаты области должны быть числами") from exc
        return cls(class_name, x, y, width, height)

    @property
    def xyxy(self) -> tuple[float, float, float, float]:
        return (
            self.x,
            self.y,
            self.x + self.width,
            self.y + self.height,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "class": self.class_name,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
        }


@dataclass
class FrameRecord:
    """Метаданные одного уникального кадра одной камеры."""

    image_id: int
    frame_uid: str
    part_id: int
    camera_role: str
    sequence: int
    width: int
    height: int
    original: str
    annotated: str | None = None
    label: str | None = None
    # Удобные копии внутри папки конкретной детали. Глобальные пути выше
    # остаются для YOLO-экспорта и не меняют нумерацию кадров.
    detail_original: str | None = None
    detail_annotated: str | None = None
    detail_label: str | None = None
    status: str = "pending"
    annotations: list[Any] = field(default_factory=list)
    captured_at: str = ""
    annotated_at: str | None = None

    @property
    def has_defect(self) -> bool:
        return bool(self.annotations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "image_id": self.image_id,
            "frame_uid": self.frame_uid,
            "part_id": self.part_id,
            "camera_role": self.camera_role,
            # role — короткий алиас для внешних интеграций.
            "role": self.camera_role,
            "sequence": self.sequence,
            "width": self.width,
            "height": self.height,
            "original": self.original,
            "annotated": self.annotated,
            "label": self.label,
            "detail_original": self.detail_original,
            "detail_annotated": self.detail_annotated,
            "detail_label": self.detail_label,
            "status": self.status,
            "has_defect": self.has_defect,
            "annotations": [item.to_dict() for item in self.annotations],
            "captured_at": self.captured_at,
            "annotated_at": self.annotated_at,
        }


class TrainingDatasetStore:
    """Файловое хранилище одной учебной партии.

    Каталог намеренно плоско читается и без этого Python-модуля:

    ``images/`` — оригиналы, ``annotated/`` — копии с polygon-контурами,
    ``details/part_0001/`` — пары original/annotated по каждой камере,
    ``labels/`` — YOLO txt, ``manifest.jsonl`` — таблица соответствий.
    ``yolo/`` после завершения содержит готовую структуру train/val.
    """

    SCHEMA_VERSION = 1

    def __init__(
        self,
        config: TrainingConfig,
        *,
        dataset_name: str | None = None,
        session_id: str | None = None,
    ):
        self.config = config
        root = Path(config.root_folder).expanduser()
        self.root_folder = root.resolve()
        self.root_folder.mkdir(parents=True, exist_ok=True)

        prefix = _safe_name(dataset_name or "dataset")
        if not prefix:
            prefix = "dataset"
        if session_id:
            safe_session = _safe_name(session_id)
            if not safe_session:
                raise ValidationError("Некорректный идентификатор партии")
            self.session_id = safe_session
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.session_id = f"{prefix}_{timestamp}_{uuid.uuid4().hex[:6]}"

        self.session_folder = self.root_folder / self.session_id
        if self.session_folder.exists() and any(self.session_folder.iterdir()):
            raise CollectorError(
                f"Папка партии уже существует и не пуста: {self.session_folder}"
            )
        self.session_folder.mkdir(parents=True, exist_ok=True)
        self.images_folder = self.session_folder / "images"
        self.annotated_folder = self.session_folder / "annotated"
        self.labels_folder = self.session_folder / "labels"
        self.details_folder = self.session_folder / "details"
        self.yolo_folder = self.session_folder / "yolo"
        for folder in (
            self.images_folder,
            self.annotated_folder,
            self.labels_folder,
            self.details_folder,
        ):
            folder.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._next_image_id = 1
        self._frames: dict[int, FrameRecord] = {}
        self._parts: dict[int, dict[str, Any]] = {}
        self._next_part_id = 1
        self.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.finished_at: str | None = None
        self._write_classes()
        self._write_state()

    @property
    def frame_count(self) -> int:
        return len(self._frames)

    @property
    def part_count(self) -> int:
        return len(self._parts)

    def create_part(self, label: str | None = None) -> dict[str, Any]:
        with self._lock:
            part_id = self._next_part_id
            self._next_part_id += 1
            part = {
                "part_id": part_id,
                "label": str(label or "").strip() or None,
                "status": "collecting",
                "started_at": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ),
                "finished_at": None,
                "frame_ids": [],
            }
            self._parts[part_id] = part
            self._write_state()
            return dict(part)

    def finish_part(self, part_id: int) -> dict[str, Any]:
        with self._lock:
            part = self._parts.get(int(part_id))
            if part is None:
                raise CollectorError(f"Деталь #{part_id} не найдена")
            frames = [self._frames[frame_id] for frame_id in part["frame_ids"]]
            pending = [frame.image_id for frame in frames if frame.status != "annotated"]
            if pending:
                raise CollectorError(
                    "Нельзя завершить деталь: не размечены кадры "
                    + ", ".join(str(value) for value in pending)
                )
            part["status"] = "finished"
            part["finished_at"] = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )
            self._write_state()
            return dict(part)

    def create_frame(
        self,
        *,
        part_id: int,
        camera_role: str,
        sequence: int,
        frame: Any,
    ) -> FrameRecord:
        """Сохранить кадр сразу, ещё до решения оператора."""
        role = str(camera_role).strip()
        if not role:
            raise ValidationError("Не указана роль камеры")
        if role not in self.config.camera_roles:
            raise ValidationError(f"Камера {role} не входит в конфигурацию")
        array = _validate_frame(frame)
        height, width = array.shape[:2]
        with self._lock:
            part = self._parts.get(int(part_id))
            if part is None or part.get("status") != "collecting":
                raise CollectorError(f"Деталь #{part_id} недоступна для съёмки")
            image_id = self._next_image_id
            self._next_image_id += 1
            stem = f"frame_{image_id:06d}_{_safe_name(role)}"
            original_path = self.images_folder / f"{stem}.jpg"
            _write_jpeg(original_path, array, self.config.jpeg_quality)
            # В папке детали лежат понятные оператору пары файлов по камерам:
            # ROLE_original.jpg и ROLE_annotated.jpg.
            detail_folder = self.details_folder / f"part_{int(part_id):04d}"
            detail_folder.mkdir(parents=True, exist_ok=True)
            detail_original_path = detail_folder / f"{_safe_name(role)}_original.jpg"
            _write_jpeg(detail_original_path, array, self.config.jpeg_quality)
            captured_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            record = FrameRecord(
                image_id=image_id,
                frame_uid=f"{self.session_id}__{stem}",
                part_id=int(part_id),
                camera_role=role,
                sequence=int(sequence),
                width=int(width),
                height=int(height),
                original=_relative_path(original_path, self.session_folder),
                detail_original=_relative_path(
                    detail_original_path,
                    self.session_folder,
                ),
                captured_at=captured_at,
            )
            self._frames[image_id] = record
            part["frame_ids"].append(image_id)
            self._write_manifest()
            self._write_state()
            return _copy_record(record)

    def annotate_frame(
        self,
        image_id: int,
        annotations: Sequence[Any],
    ) -> FrameRecord:
        """Сохранить overlay и txt для уже сохранённого оригинала."""
        with self._lock:
            record = self._frames.get(int(image_id))
            if record is None:
                raise CollectorError(f"Кадр #{image_id} не найден")
            if record.status == "annotated":
                raise CollectorError(f"Кадр #{image_id} уже размечен")
            boxes = list(annotations)
            for box in boxes:
                if box.class_name not in self.config.defect_classes:
                    raise ValidationError(
                        f"Неизвестный класс дефекта: {box.class_name}"
                    )
                try:
                    normalise_polygon(
                        box,
                        image_width=record.width,
                        image_height=record.height,
                    )
                except ValueError as exc:
                    raise ValidationError(
                        f"Некорректная область кадра #{image_id}: {exc}"
                    ) from exc

            original_path = self.session_folder / record.original
            source = cv2.imread(str(original_path), cv2.IMREAD_COLOR)
            if source is None:
                raise CollectorError(
                    f"Не удалось прочитать оригинал кадра #{image_id}"
                )
            marked = draw_annotations(
                source,
                boxes,
                image_width=record.width,
                image_height=record.height,
            )
            annotated_path = self.annotated_folder / original_path.name
            _write_jpeg(annotated_path, marked, self.config.jpeg_quality)

            detail_folder = self.details_folder / f"part_{record.part_id:04d}"
            detail_annotated_path = (
                detail_folder / f"{_safe_name(record.camera_role)}_annotated.jpg"
            )
            _write_jpeg(
                detail_annotated_path,
                marked,
                self.config.jpeg_quality,
            )
            detail_label_path = (
                detail_folder / f"{_safe_name(record.camera_role)}.txt"
            )

            label_path = self.labels_folder / f"{original_path.stem}.txt"
            lines = annotation_to_yolo_lines(
                boxes,
                image_width=record.width,
                image_height=record.height,
                class_names=self.config.defect_classes,
                task=self.config.yolo_task,
            )
            label_content = "\n".join(lines) + ("\n" if lines else "")
            _write_text(label_path, label_content)
            _write_text(detail_label_path, label_content)

            record.annotations = boxes
            record.annotated = _relative_path(annotated_path, self.session_folder)
            record.label = _relative_path(label_path, self.session_folder)
            record.detail_annotated = _relative_path(
                detail_annotated_path,
                self.session_folder,
            )
            record.detail_label = _relative_path(
                detail_label_path,
                self.session_folder,
            )
            record.status = "annotated"
            record.annotated_at = datetime.now(timezone.utc).isoformat(
                timespec="milliseconds"
            )
            self._write_manifest()
            self._write_state()
            return _copy_record(record)

    def get_frame(self, image_id: int) -> FrameRecord | None:
        with self._lock:
            record = self._frames.get(int(image_id))
            return _copy_record(record) if record else None

    def get_frames(self) -> list[FrameRecord]:
        with self._lock:
            return [_copy_record(item) for item in self._frames.values()]

    def get_parts(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._parts.values()]

    def image_path(self, image_id: int, kind: str = "original") -> Path:
        record = self._frames.get(int(image_id))
        if record is None:
            raise CollectorError(f"Кадр #{image_id} не найден")
        relative = {
            "original": record.original,
            "annotated": record.annotated,
            "label": record.label,
            "detail_original": record.detail_original,
            "detail_annotated": record.detail_annotated,
            "detail_label": record.detail_label,
        }.get(kind)
        if not relative:
            raise CollectorError(f"Кадр #{image_id} ещё не имеет файла {kind}")
        path = (self.session_folder / relative).resolve()
        if self.session_folder.resolve() not in path.parents:
            raise CollectorError("Недопустимый путь файла кадра")
        if not path.exists():
            raise CollectorError(f"Файл кадра отсутствует: {path.name}")
        return path

    def mark_finished(self) -> dict[str, Any]:
        with self._lock:
            pending = [
                item.image_id
                for item in self._frames.values()
                if item.status != "annotated"
            ]
            open_parts = [
                part["part_id"]
                for part in self._parts.values()
                if part.get("status") != "finished"
            ]
            if pending:
                raise CollectorError(
                    "Нельзя завершить набор: есть неразмеченные кадры "
                    + ", ".join(str(value) for value in pending)
                )
            if open_parts:
                raise CollectorError(
                    "Сначала завершите детали: "
                    + ", ".join(str(value) for value in open_parts)
                )
            self.finished_at = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )
            self._write_state()
            return self.state_payload()

    def state_payload(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema_version": self.SCHEMA_VERSION,
                "session_id": self.session_id,
                "root_folder": str(self.root_folder),
                "session_folder": str(self.session_folder),
                "details_folder": str(self.details_folder),
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "camera_roles": list(self.config.camera_roles),
                "defect_classes": list(self.config.defect_classes),
                "yolo_task": self.config.yolo_task,
                "frame_count": len(self._frames),
                "part_count": len(self._parts),
                "parts": [dict(item) for item in self._parts.values()],
            }

    def _write_classes(self) -> None:
        _write_json(
            self.session_folder / "classes.json",
            {
                "schema_version": self.SCHEMA_VERSION,
                "classes": list(self.config.defect_classes),
                "camera_roles": list(self.config.camera_roles),
                "yolo_task": self.config.yolo_task,
            },
        )

    def _write_manifest(self) -> None:
        lines = []
        for record in self._frames.values():
            lines.append(json.dumps(record.to_dict(), ensure_ascii=False))
        _write_text(
            self.session_folder / "manifest.jsonl",
            "\n".join(lines) + ("\n" if lines else ""),
        )

    def _write_state(self) -> None:
        _write_json(
            self.session_folder / "session.json",
            {
                **self.state_payload(),
                "parts": [dict(item) for item in self._parts.values()],
            },
        )


class TrainingCollector:
    """Последовательный мастер: деталь → камеры → разметка → YOLO."""

    def __init__(
        self,
        config: TrainingConfig | None = None,
        *,
        frame_source: CameraFrameSource | None = None,
        conveyor: Any | None = None,
    ):
        self.config = config or TrainingConfig()
        self.frame_source = frame_source
        # Объект с методом ``advance()``: шаг ленты к следующей детали после
        # нажатия «Завершить деталь». ``None`` отключает движение, чтобы
        # мастер можно было использовать без подключённого контроллера.
        self.conveyor = conveyor
        self.last_conveyor_result: dict[str, Any] | None = None
        self.store: TrainingDatasetStore | None = None
        self.current_part_id: int | None = None
        self.current_frame_id: int | None = None
        self._lock = threading.RLock()

    @property
    def session_active(self) -> bool:
        return self.store is not None and self.store.finished_at is None

    def start_session(
        self,
        dataset_name: str | None = None,
        *,
        defect_classes: Sequence[str] | None = None,
        camera_roles: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if self.store is not None and self.store.finished_at is None:
                raise CollectorError("Сессия уже создана; сначала завершите её")
            # После завершённой партии тот же сервер можно использовать для
            # следующего набора; старый каталог остаётся на диске.
            if self.store is not None and self.store.finished_at is not None:
                self.store = None
            config = TrainingConfig(
                root_folder=self.config.root_folder,
                camera_roles=tuple(camera_roles or self.config.camera_roles),
                defect_classes=tuple(
                    defect_classes or self.config.defect_classes
                ),
                jpeg_quality=self.config.jpeg_quality,
                validation_ratio=self.config.validation_ratio,
                yolo_task=self.config.yolo_task,
            )
            self.config = config
            self.store = TrainingDatasetStore(config, dataset_name=dataset_name)
            self.current_part_id = None
            self.current_frame_id = None
            return self.snapshot()

    def add_defect_class(self, class_name: str) -> list[str]:
        """Добавить элемент в палитру текущей сессии."""
        with self._lock:
            self._require_session()
            name = str(class_name or "").strip()
            if not name:
                raise ValidationError("Название элемента не может быть пустым")
            if name in self.config.defect_classes:
                raise ValidationError(f"Элемент уже есть в палитре: {name}")
            classes = _normalise_classes((*self.config.defect_classes, name))
            self.config = TrainingConfig(
                root_folder=self.config.root_folder,
                camera_roles=self.config.camera_roles,
                defect_classes=classes,
                jpeg_quality=self.config.jpeg_quality,
                validation_ratio=self.config.validation_ratio,
                yolo_task=self.config.yolo_task,
            )
            self.store.config = self.config
            self.store._write_classes()
            self.store._write_state()
            return list(classes)

    def begin_part(self, label: str | None = None) -> dict[str, Any]:
        with self._lock:
            self._require_session()
            if self.current_part_id is not None:
                raise CollectorError(
                    f"Деталь #{self.current_part_id} ещё не завершена"
                )
            part = self.store.create_part(label)
            self.current_part_id = int(part["part_id"])
            self.current_frame_id = None
            return self.snapshot()

    def capture_next(self) -> FrameRecord:
        with self._lock:
            self._require_session()
            if self.current_part_id is None:
                raise CollectorError("Сначала начните новую деталь")
            if self.current_frame_id is not None:
                raise CollectorError(
                    "Сначала сохраните разметку текущего кадра"
                )
            frames = [
                item
                for item in self.store.get_frames()
                if item.part_id == self.current_part_id
            ]
            sequence = len(frames)
            if sequence >= len(self.config.camera_roles):
                raise CollectorError(
                    "Все камеры этой детали уже сняты; завершите деталь"
                )
            role = self.config.camera_roles[sequence]
            if self.frame_source is None:
                raise CollectorError("Источник камер не подключён")
            try:
                frame = self.frame_source.capture_single(role)
            except Exception as exc:
                raise CollectorError(
                    f"Не удалось получить кадр камеры {role}: {exc}"
                ) from exc
            record = self.store.create_frame(
                part_id=self.current_part_id,
                camera_role=role,
                sequence=sequence,
                frame=frame,
            )
            self.current_frame_id = record.image_id
            return record

    def annotate_current(
        self,
        *,
        defect_class: str | None = None,
        boxes: Sequence[BoundingBox | dict[str, Any]] | None = None,
        polygons: Sequence[PolygonAnnotation | dict[str, Any]] | None = None,
        annotations: Sequence[Any] | None = None,
    ) -> FrameRecord:
        with self._lock:
            self._require_session()
            if self.current_frame_id is None:
                raise CollectorError("Нет кадра, ожидающего разметки")
            regions = (
                annotations
                if annotations is not None
                else polygons
                if polygons is not None
                else boxes
            )
            parsed = self._parse_annotations(defect_class, regions)
            record = self.store.annotate_frame(self.current_frame_id, parsed)
            self.current_frame_id = None
            return record

    def skip_current(self) -> FrameRecord:
        """Отметить кадр как отрицательный пример без дефекта."""
        return self.annotate_current(defect_class=None, boxes=())

    def finish_part(self) -> dict[str, Any]:
        with self._lock:
            self._require_session()
            if self.current_part_id is None:
                raise CollectorError("Нет открытой детали")
            if self.current_frame_id is not None:
                raise CollectorError("Сначала сохраните разметку текущего кадра")
            frames = [
                item
                for item in self.store.get_frames()
                if item.part_id == self.current_part_id
            ]
            if len(frames) != len(self.config.camera_roles):
                missing = self.config.camera_roles[len(frames):]
                raise CollectorError(
                    "Не сняты камеры: " + ", ".join(missing)
                )
            part = self.store.finish_part(self.current_part_id)
            self.current_part_id = None
            self.current_frame_id = None

        # Шаг выполняется вне блокировки: опрос ``/api/training/state`` не
        # должен ждать, пока лента физически пройдёт расстояние до
        # следующего корпуса.
        movement = self._advance_conveyor()
        with self._lock:
            return self.snapshot(
                extra={
                    "finished_part": part,
                    "conveyor": movement,
                }
            )

    def _advance_conveyor(self) -> dict[str, Any]:
        """Сделать один шаг ленты к следующей детали.

        Разметка к этому моменту уже сохранена на диске, поэтому сбой
        оборудования не отменяет завершение детали: оператор получает
        результат шага в поле ``conveyor`` и может повторить его кнопкой
        «Сдвинуть ленту», не теряя открытую сессию.
        """
        if self.conveyor is None:
            result: dict[str, Any] = {
                "enabled": False,
                "moved": False,
                "error": None,
            }
        else:
            try:
                result = {
                    "enabled": True,
                    **dict(self.conveyor.advance() or {}),
                }
                result.setdefault("moved", False)
            except Exception as exc:
                result = {
                    "enabled": True,
                    "moved": False,
                    "error": str(exc),
                }
        self.last_conveyor_result = result
        return result

    def step_conveyor(self) -> dict[str, Any]:
        """Повторить шаг ленты вручную после сбоя оборудования.

        Двигать ленту посреди детали нельзя: кадры и разметка относятся к
        текущему положению корпуса. Поэтому шаг разрешён только между
        деталями.
        """
        with self._lock:
            self._require_session()
            if self.current_frame_id is not None:
                raise CollectorError("Сначала сохраните разметку текущего кадра")
            if self.current_part_id is not None:
                raise CollectorError(
                    f"Деталь #{self.current_part_id} ещё не завершена"
                )
        movement = self._advance_conveyor()
        with self._lock:
            return self.snapshot(extra={"conveyor": movement})

    def finish_session(self) -> dict[str, Any]:
        with self._lock:
            self._require_session()
            if self.current_frame_id is not None:
                raise CollectorError("Сначала сохраните разметку текущего кадра")
            if self.current_part_id is not None:
                raise CollectorError("Сначала завершите текущую деталь")
            from training.yolo import export_yolo_dataset

            # Сначала строим экспорт: если на диске не хватает файла, оператор
            # получает ошибку и может исправить её, не теряя открытую сессию.
            yolo = export_yolo_dataset(
                self.store,
                validation_ratio=self.config.validation_ratio,
            ).to_dict()
            result = self.store.mark_finished()
            result["yolo"] = yolo
            return result

    def get_current_record(self) -> FrameRecord | None:
        with self._lock:
            if self.store is None or self.current_frame_id is None:
                return None
            return self.store.get_frame(self.current_frame_id)

    def snapshot(self, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            if self.store is None:
                result = {
                    "active": False,
                    "session": None,
                    "part": None,
                    "current_frame": None,
                    "next_role": None,
                    "camera_roles": list(self.config.camera_roles),
                    "defect_classes": list(self.config.defect_classes),
                }
            else:
                frames = self.store.get_frames()
                part_frames = [
                    item
                    for item in frames
                    if item.part_id == self.current_part_id
                ] if self.current_part_id is not None else []
                next_role = (
                    self.config.camera_roles[len(part_frames)]
                    if self.current_part_id is not None
                    and len(part_frames) < len(self.config.camera_roles)
                    else None
                )
                part = next(
                    (
                        item
                        for item in self.store.get_parts()
                        if item["part_id"] == self.current_part_id
                    ),
                    None,
                )
                result = {
                    "active": self.session_active,
                    "session": self.store.state_payload(),
                    "part": part,
                    "current_frame": (
                        self.store.get_frame(self.current_frame_id)
                        if self.current_frame_id is not None
                        else None
                    ),
                    "next_role": next_role,
                    "camera_roles": list(self.config.camera_roles),
                    "defect_classes": list(self.config.defect_classes),
                }
            if extra:
                result.update(extra)
            return _json_safe(result)

    def image_path(self, image_id: int, kind: str = "original") -> Path:
        with self._lock:
            self._require_session()
            return self.store.image_path(image_id, kind)

    def _parse_annotations(
        self,
        defect_class: str | None,
        regions: Sequence[Any] | None,
    ) -> list[Any]:
        selected = str(defect_class or "").strip()
        if selected and selected not in self.config.defect_classes:
            raise ValidationError(f"Неизвестный класс дефекта: {selected}")

        parsed = []
        for item in regions or ():
            try:
                if isinstance(item, (BoundingBox, PolygonAnnotation)):
                    parsed.append(item)
                elif isinstance(item, dict) and "points" in item:
                    parsed.append(PolygonAnnotation.from_payload(item))
                else:
                    # Legacy API: bbox {x, y, width, height} тоже принимается
                    # и превращается в прямоугольный polygon при экспорте.
                    parsed.append(BoundingBox.from_payload(item))
            except (TypeError, ValueError, KeyError, IndexError) as exc:
                raise ValidationError(f"Некорректная область: {exc}") from exc

        if selected:
            if not parsed:
                raise ValidationError(
                    "Для выбранного элемента нужно обвести хотя бы одну область"
                )
            parsed = [
                _with_class_name(selected, item)
                for item in parsed
            ]

        for item in parsed:
            if not item.class_name:
                raise ValidationError("У области не указан элемент палитры")
            if item.class_name not in self.config.defect_classes:
                raise ValidationError(
                    f"Неизвестный класс дефекта: {item.class_name}"
                )
        return parsed

    def _require_session(self) -> None:
        if self.store is None:
            raise CollectorError("Сначала создайте сессию обучения")
        if self.store.finished_at is not None:
            raise CollectorError("Сессия уже завершена")


TrainingDatasetCollector = TrainingCollector


def _with_class_name(class_name: str, annotation: Any) -> Any:
    if isinstance(annotation, PolygonAnnotation):
        return PolygonAnnotation(class_name, annotation.points)
    return BoundingBox(
        class_name,
        annotation.x,
        annotation.y,
        annotation.width,
        annotation.height,
    )


def _normalise_roles(values: Sequence[str] | None) -> tuple[str, ...]:
    roles = tuple(str(value).strip() for value in (values or ()) if str(value).strip())
    if not roles:
        raise ValidationError("Нужна хотя бы одна камера")
    if len(set(roles)) != len(roles):
        raise ValidationError("Роли камер должны быть уникальными")
    return roles


def _normalise_classes(values: Sequence[str] | None) -> tuple[str, ...]:
    classes = tuple(str(value).strip() for value in (values or ()) if str(value).strip())
    if not classes:
        raise ValidationError("Нужен хотя бы один класс дефекта")
    if len(set(classes)) != len(classes):
        raise ValidationError("Классы дефектов должны быть уникальными")
    if any("\n" in value or "\r" in value for value in classes):
        raise ValidationError("Название класса не может содержать перевод строки")
    return classes


def _safe_name(value: str) -> str:
    value = str(value or "").strip()
    value = _SAFE_NAME_RE.sub("_", value)
    return value.strip("._-")[:80]


def _validate_frame(frame: Any) -> np.ndarray:
    if frame is None:
        raise ValidationError("Камера вернула пустой кадр")
    array = np.asarray(frame)
    if array.ndim not in (2, 3) or array.shape[0] <= 0 or array.shape[1] <= 0:
        raise ValidationError(f"Неверная форма кадра: {array.shape}")
    if array.ndim == 2:
        array = cv2.cvtColor(array, cv2.COLOR_GRAY2BGR)
    if array.shape[2] == 4:
        array = cv2.cvtColor(array, cv2.COLOR_BGRA2BGR)
    if array.shape[2] != 3:
        raise ValidationError(f"Неверное число каналов кадра: {array.shape}")
    if array.dtype != np.uint8:
        array = np.clip(array, 0, 255).astype(np.uint8)
    return np.ascontiguousarray(array)


def _copy_record(record: FrameRecord | None) -> FrameRecord | None:
    if record is None:
        return None
    return FrameRecord(
        image_id=record.image_id,
        frame_uid=record.frame_uid,
        part_id=record.part_id,
        camera_role=record.camera_role,
        sequence=record.sequence,
        width=record.width,
        height=record.height,
        original=record.original,
        annotated=record.annotated,
        label=record.label,
        detail_original=record.detail_original,
        detail_annotated=record.detail_annotated,
        detail_label=record.detail_label,
        status=record.status,
        annotations=list(record.annotations),
        captured_at=record.captured_at,
        annotated_at=record.annotated_at,
    )


def _relative_path(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _write_jpeg(path: Path, image: np.ndarray, quality: int) -> None:
    ok, encoded = cv2.imencode(
        ".jpg",
        image,
        [cv2.IMWRITE_JPEG_QUALITY, int(quality)],
    )
    if not ok or encoded is None:
        raise CollectorError(f"Не удалось закодировать изображение: {path.name}")
    _write_bytes(path, encoded.tobytes())


def _write_text(path: Path, content: str) -> None:
    _write_bytes(path, content.encode("utf-8"))


def _write_json(path: Path, value: Any) -> None:
    _write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
    )


def _write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        with temporary.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _json_safe(value: Any) -> Any:
    if isinstance(value, FrameRecord):
        return value.to_dict()
    if isinstance(value, BoundingBox):
        return value.to_dict()
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    return value
