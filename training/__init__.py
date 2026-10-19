"""Сбор датасета для ручной разметки дефектов и обучения YOLO.

Модуль намеренно отделён от производственного цикла: он не создаёт
конвейер, не запускает модели и не меняет ``PartArchive``. Его можно
запустить отдельным мастером, когда линия остановлена, или подключить к
другому интерфейсу через :class:`TrainingCollector` и
:func:`training.server.create_app`.
"""

from training.collector import (
    DEFAULT_CAMERA_ROLES,
    DEFAULT_DEFECT_CLASSES,
    BoundingBox,
    CameraFrameSource,
    CollectorCameraSource,
    CollectorError,
    FrameRecord,
    TrainingCollector,
    TrainingConfig,
    TrainingDatasetCollector,
    TrainingDatasetStore,
    ValidationError,
)
from training.yolo import (
    PolygonAnnotation,
    YoloExport,
    export_yolo_dataset,
)

__all__ = [
    "DEFAULT_CAMERA_ROLES",
    "DEFAULT_DEFECT_CLASSES",
    "BoundingBox",
    "CameraFrameSource",
    "CollectorCameraSource",
    "CollectorError",
    "FrameRecord",
    "TrainingCollector",
    "TrainingConfig",
    "TrainingDatasetCollector",
    "TrainingDatasetStore",
    "ValidationError",
    "PolygonAnnotation",
    "YoloExport",
    "export_yolo_dataset",
]
