"""Composition root для production-зависимостей.

Здесь сосредоточены конкретные классы оборудования, vision и инспекции.
Startup управляет только порядком и отображением этапов, не зная деталей их
конструирования.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable

from config import load_archive_config, load_calibration
from core.decision_engine import DecisionEngine
from core.production_cycle import ProductionCycle
from domain.threshold_loader import ThresholdLoader
from hardware.axis import Axis
from hardware.conveyor import Conveyor
from hardware.distributor import Distributor
from hardware.jog_controller import JogController
from hardware.port_discovery import find_controller
from hardware.serial_transport import SerialTransport
from inspection.debug_recorder import DebugRecorder
from inspection.inspector import Inspector
from inspection.part_archive import PartArchive
from vision.camera_manager import CameraManager
from vision.vision_cluster import VisionCluster


def vision_device_from_env() -> str:
    """Устройство inference для vision из окружения.

    ``VISION_DEVICE``: ``auto`` (по умолчанию, GPU при доступном CUDA),
    ``cpu``, ``gpu``/``cuda``, ``cuda:N`` или индекс GPU.
    """
    return os.environ.get("VISION_DEVICE", "auto")


@dataclass(frozen=True)
class InspectionServices:
    threshold_loader: Any
    thresholds: dict
    decision: Any
    inspector: Any
    archive: Any


@dataclass(frozen=True)
class HardwareServices:
    conveyor: Any
    distributor: Any
    jog: Any


class ProductionSystemFactory:
    """Создаёт конкретные зависимости реальной линии."""

    def load_calibration(self) -> dict:
        return load_calibration()

    def create_cameras(self):
        return CameraManager()

    def create_vision(self):
        # Устройство inference задаётся переменной окружения VISION_DEVICE
        # (auto | cpu | gpu | cuda[:N] | 0 | 1 ...); по умолчанию auto.
        return VisionCluster(device=vision_device_from_env())

    def create_inspection(self, vision) -> InspectionServices:
        threshold_loader = ThresholdLoader()
        thresholds = threshold_loader.get_all()
        decision = DecisionEngine(thresholds=thresholds)
        recorder = DebugRecorder(
            folder="debug_frames",
            enabled=False,
            save_interval=1,
        )
        inspector = Inspector(
            vision=vision,
            decision=decision,
            recorder=recorder,
        )
        archive_config = load_archive_config()
        archive = PartArchive(
            root_folder=archive_config["root_path"],
        )
        return InspectionServices(
            threshold_loader=threshold_loader,
            thresholds=thresholds,
            decision=decision,
            inspector=inspector,
            archive=archive,
        )

    def discover_controller(
        self,
        *,
        baudrate: int,
        preferred_port: str | None,
    ) -> tuple[str | None, str]:
        return find_controller(
            baudrate=baudrate,
            preferred_port=preferred_port,
        )

    def create_transport(self, *, port: str, baudrate: int):
        return SerialTransport(port=port, baudrate=baudrate)

    def create_hardware(
        self,
        transport,
        calibration: dict,
        cancel_check: Callable[[], bool],
    ) -> HardwareServices:
        conveyor = Conveyor(
            transport,
            speed=calibration["conveyor_speed"],
            accel=calibration["conveyor_accel"],
            steps_per_division=calibration["normal_steps"],
            divisions_per_movement=2,
        )
        dist1_axis = Axis(
            transport,
            axis_id=0,
            minimum=0,
            maximum=calibration["dist1_open_position"],
            speed=calibration["axis_speed"],
            accel=calibration["axis_accel"],
        )
        dist2_axis = Axis(
            transport,
            axis_id=1,
            minimum=0,
            maximum=max(
                calibration["dist2_bad_position"],
                calibration["dist2_cleanup_position"],
            ),
            speed=calibration["axis_speed"],
            accel=calibration["axis_accel"],
        )
        distributor = Distributor(
            dist1_axis=dist1_axis,
            dist2_axis=dist2_axis,
            dist1_open_position=calibration["dist1_open_position"],
            dist2_bad_position=calibration["dist2_bad_position"],
            dist2_cleanup_position=calibration["dist2_cleanup_position"],
        )
        self._validate_distributor_endpoints(distributor, calibration)
        distributor.cancel_check = cancel_check
        jog = JogController(
            transport=transport,
            calibration=calibration,
        )
        return HardwareServices(
            conveyor=conveyor,
            distributor=distributor,
            jog=jog,
        )

    @staticmethod
    def _validate_distributor_endpoints(distributor, calibration: dict) -> None:
        if (
            distributor.dist1_open_position
            != calibration["dist1_open_position"]
            or distributor.dist2_bad_position
            != calibration["dist2_bad_position"]
            or distributor.dist2_cleanup_position
            != calibration["dist2_cleanup_position"]
        ):
            raise RuntimeError(
                "Distributor endpoints do not match calibration.json"
            )

    def create_cycle(
        self,
        *,
        hardware: HardwareServices,
        cameras,
        inspector,
        monitor,
        archive,
        calibration: dict,
    ):
        return ProductionCycle(
            conveyor=hardware.conveyor,
            cameras=cameras,
            inspector=inspector,
            distributor=hardware.distributor,
            monitor=monitor,
            archive=archive,
            jog=hardware.jog,
            settle_seconds=calibration["settle_time"],
            stage_trace_seconds=calibration["stage_trace_time"],
            review_seconds=calibration["review_time"],
        )
