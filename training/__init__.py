"""Сбор датасета для ручной разметки дефектов и обучения YOLO.

Модуль намеренно отделён от производственного цикла: он не запускает
модели и не меняет ``PartArchive``. Единственное воздействие на линию —
один подтверждённый шаг ленты (``G3 N<seq>``) по кнопке «Завершить деталь»,
чтобы под камерами оказался следующий корпус; шаг можно отключить
(``--conveyor off``), если мастер запускается без контроллера. Мастер можно
использовать отдельно, когда линия остановлена, или подключить к другому
интерфейсу через :class:`TrainingCollector` и
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
from training.conveyor import ConveyorStepError, SerialConveyorStepper
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
    "ConveyorStepError",
    "FrameRecord",
    "SerialConveyorStepper",
    "TrainingCollector",
    "TrainingConfig",
    "TrainingDatasetCollector",
    "TrainingDatasetStore",
    "ValidationError",
    "PolygonAnnotation",
    "YoloExport",
    "export_yolo_dataset",
]
