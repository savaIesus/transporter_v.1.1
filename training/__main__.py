"""Запуск отдельного мастера сбора датасета.

Windows:
    .venv\\Scripts\\python.exe -m training

Мастер нужно запускать при остановленной производственной программе: обе
программы используют USB-камеры, и один и тот же VideoCapture нельзя
безопасно открыть одновременно.
"""

from __future__ import annotations

import argparse
import json
import os
import webbrowser
from pathlib import Path

import uvicorn

from training.collector import (
    DEFAULT_CAMERA_ROLES,
    DEFAULT_DEFECT_CLASSES,
    CollectorCameraSource,
    TrainingCollector,
    TrainingConfig,
)
from training.server import create_app


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description="Отдельный мастер ручной разметки кадров для YOLO"
    )
    parser.add_argument("--host", default=os.environ.get("TRAINING_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("TRAINING_PORT", "8010")))
    parser.add_argument("--root", default=os.environ.get("TRAINING_ROOT", "training_datasets"))
    parser.add_argument("--mapping", default="camera_mapping.json")
    parser.add_argument("--config", help="JSON с camera_roles и defect_classes")
    parser.add_argument("--open-browser", action="store_true")
    parser.add_argument(
        "--conveyor",
        choices=("auto", "off"),
        default=os.environ.get("TRAINING_CONVEYOR", "auto"),
        help=(
            "auto — после «Завершить деталь» делать шаг ленты G3, "
            "off — не управлять конвейером"
        ),
    )
    parser.add_argument(
        "--serial-port",
        default=os.environ.get("SERIAL_PORT"),
        help="COM-порт контроллера; по умолчанию ищется автоматически",
    )
    parser.add_argument(
        "--baudrate",
        type=int,
        default=int(os.environ.get("SERIAL_BAUD", "115200")),
    )
    parser.add_argument(
        "--calibration",
        default=os.environ.get("CALIBRATION_FILE", "calibration.json"),
    )
    args = parser.parse_args(argv)

    options = _load_options(args.config)
    roles = tuple(options.get("camera_roles") or DEFAULT_CAMERA_ROLES)
    classes = tuple(options.get("defect_classes") or DEFAULT_DEFECT_CLASSES)
    config = TrainingConfig(
        root_folder=args.root,
        camera_roles=roles,
        defect_classes=classes,
    )

    # Импорт камеры и её открытие происходят только при запуске CLI, поэтому
    # ``import training`` не требует наличия физического оборудования.
    from vision.camera_manager import CameraManager

    cameras = CameraManager(config_file=args.mapping)
    stepper = None
    if args.conveyor != "off":
        # Импорт pyserial и hardware происходит только здесь: запуск мастера
        # без оборудования не должен требовать наличия контроллера.
        from training.conveyor import SerialConveyorStepper

        stepper = SerialConveyorStepper(
            baudrate=args.baudrate,
            preferred_port=args.serial_port,
            calibration_path=args.calibration,
        )
    collector = TrainingCollector(
        config,
        frame_source=CollectorCameraSource(cameras),
        conveyor=stepper,
    )
    app = create_app(collector)
    url = f"http://127.0.0.1:{args.port}/"
    print("=" * 64)
    print("Мастер сбора датасета YOLO")
    print(f"Откройте {url}")
    print("Линия должна быть остановлена; камеры заняты только этим мастером.")
    if stepper is not None:
        print(
            "Шаг ленты: после «ЗАВЕРШИТЬ ДЕТАЛЬ» отправляется G3 N<seq> "
            "и ожидается подтверждение STEP."
        )
    else:
        print("Шаг ленты отключён (--conveyor off).")
    print("=" * 64)
    if args.open_browser:
        webbrowser.open(url)
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    finally:
        cameras.release()
        if stepper is not None:
            stepper.close()


def _load_options(path: str | None) -> dict:
    if not path:
        return {}
    try:
        with Path(path).open(encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Не удалось прочитать {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit("Конфигурация мастера должна быть JSON-объектом")
    return value


if __name__ == "__main__":
    main()
