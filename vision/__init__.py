"""Vision components with lazy imports for lightweight tooling and tests."""

__all__ = [
    "CameraManager",
    "calibrate_cameras",
    "launch_camera_calibrator",
    "VisionCluster",
    "MODEL_GROUPS",
    "ROLE_TO_GROUP",
    "LiveMonitor",
]


def __getattr__(name):
    if name == "CameraManager":
        from vision.camera_manager import CameraManager
        return CameraManager
    if name in {"calibrate_cameras", "launch_camera_calibrator"}:
        from vision.camera_calibration_console import (
            calibrate_cameras,
            launch_camera_calibrator,
        )
        return {
            "calibrate_cameras": calibrate_cameras,
            "launch_camera_calibrator": launch_camera_calibrator,
        }[name]
    if name == "VisionCluster":
        from vision.vision_cluster import VisionCluster
        return VisionCluster
    if name in {"MODEL_GROUPS", "ROLE_TO_GROUP"}:
        from vision.model_config import MODEL_GROUPS, ROLE_TO_GROUP
        return {"MODEL_GROUPS": MODEL_GROUPS, "ROLE_TO_GROUP": ROLE_TO_GROUP}[name]
    if name == "LiveMonitor":
        from vision.ui import LiveMonitor
        return LiveMonitor
    raise AttributeError(name)
