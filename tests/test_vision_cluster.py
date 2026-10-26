"""VisionCluster с подменённым ultralytics (трёхкамерная линия).

Модуль ``ultralytics`` заменяется фейком через ``sys.modules``, веса —
временными файлами. Проверяются загрузка моделей, warmup, прогон
детекций, per-entry iou из model_config, проставление ``kind`` и ошибки
inference.
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from unittest import mock

import numpy as np

# Фейковый ultralytics ставится в sys.modules ДО импорта vision.vision_cluster,
# иначе реальный импорт `from ultralytics import YOLO` упадёт без пакета.
_FAKE_ULTRALYTICS = types.ModuleType("ultralytics")


class FakeYOLO:
    instances = []

    def __init__(self, path):
        self.path = path
        self.names = dict(NAMES_BY_PATH[path])
        self.predict_calls = []
        FakeYOLO.instances.append(self)

    def predict(self, frame, **kwargs):
        self.predict_calls.append(kwargs)
        boxes = FakeBoxes(
            xyxy=np.array([[10.0, 20.0, 30.0, 40.0]], dtype=np.float32),
            conf=np.array([0.9], dtype=np.float32),
            cls=np.array([0], dtype=np.float32),
        )
        masks = FakeMasks([np.array([[10, 20], [30, 20], [30, 40], [10, 40]],
                                    dtype=np.float32)])
        return [FakeResult(self.names, boxes, masks)]


_FAKE_ULTRALYTICS.YOLO = FakeYOLO
sys.modules["ultralytics"] = _FAKE_ULTRALYTICS

import vision.vision_cluster as vision_cluster  # noqa: E402


def restore_modules(saved):
    for name in list(sys.modules):
        if name not in saved:
            del sys.modules[name]


class TensorLike:
    """Имитация torch-тензора: .cpu() -> .numpy() -> ndarray."""

    def __init__(self, array):
        self.array = array

    def cpu(self):
        return self

    def numpy(self):
        return self.array


class FakeBoxes:
    def __init__(self, xyxy, conf, cls):
        self.xyxy = TensorLike(xyxy)
        self.conf = TensorLike(conf)
        self.cls = TensorLike(cls)


class FakeMasks:
    def __init__(self, polys):
        self.xy = polys

    def cpu(self):
        return self


class FakeResult:
    def __init__(self, names, boxes, masks=None):
        self.names = names
        self.boxes = boxes
        self.masks = masks


# Имена классов внутри весов трёхкамерника не используются: постобработка
# определяется полем ``kind`` из model_config. Для фейка достаточно любых
# имён; конфигурация c3 не содержит ``classes`` и верификацию не запускает.
NAMES_BY_PATH = {
    "weights/windows_4.pt": {0: "windows"},
    "weights/shells.pt": {0: "shells"},
    "weights/bottom_glass_new_v3.pt": {0: "glass"},
    "weights/welding_new_2.pt": {0: "welding"},
}


class FailingYOLO(FakeYOLO):
    def predict(self, frame, **kwargs):
        self.predict_calls.append(kwargs)
        raise RuntimeError("cuda oom")


class VisionClusterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        for group in vision_cluster.MODEL_GROUPS.values():
            for entry in group:
                full = os.path.join(cls.tmp.name, entry["path"])
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "wb") as stream:
                    stream.write(b"fake weights")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.saved_modules = set(sys.modules)
        self.addCleanup(restore_modules, self.saved_modules)
        self.weights = self.tmp.name

    def make_cluster(self, yolo_class=None, verbose=False, device="cpu"):
        with mock.patch.object(
            vision_cluster, "YOLO", yolo_class or FakeYOLO,
        ), mock.patch.object(
            vision_cluster.os.path, "isfile", return_value=True,
        ):
            return vision_cluster.VisionCluster(
                device=device, verbose=verbose,
            )

    def frame(self):
        return np.zeros((720, 1280, 3), dtype=np.uint8)

    def test_loads_all_models_once(self):
        cluster = self.make_cluster()
        self.assertEqual(len(cluster.models), 4)
        self.assertIn("weights/windows_4.pt", cluster.models)
        self.assertIn("weights/welding_new_2.pt", cluster.models)

    def test_missing_model_file_raises(self):
        with mock.patch.object(
            vision_cluster, "YOLO", FakeYOLO,
        ), mock.patch.object(
            vision_cluster.os.path, "isfile", return_value=False,
        ):
            with self.assertRaises(FileNotFoundError):
                vision_cluster.VisionCluster(device="cpu")

    def test_warmup(self):
        cluster = self.make_cluster()
        cluster.warmup()
        for model in cluster.models.values():
            self.assertGreaterEqual(len(model.predict_calls), 1)

    def test_warmup_error_raises(self):
        cluster = self.make_cluster(yolo_class=FailingYOLO)
        with self.assertRaisesRegex(RuntimeError, "warmup failed"):
            cluster.warmup()

    def test_process_all_returns_detections(self):
        cluster = self.make_cluster()
        results = cluster.process_all({
            "LEFT": self.frame(),
            "MIDDLE": self.frame(),
        })
        self.assertEqual(set(results), {"LEFT", "MIDDLE"})
        # LEFT: 2 модели группы GROUP_LEFT_RIGHT; MIDDLE: 2 модели GROUP_MIDDLE.
        self.assertEqual(len(results["LEFT"]), 2)
        self.assertEqual(len(results["MIDDLE"]), 2)
        kinds_left = {detection["kind"] for detection in results["LEFT"]}
        kinds_middle = {detection["kind"] for detection in results["MIDDLE"]}
        self.assertEqual(kinds_left, {"uneven_heights", "window_sinks"})
        self.assertEqual(kinds_middle, {"bottom_glass", "welding"})
        detection = results["LEFT"][0]
        self.assertEqual(detection["class"], "windows")
        self.assertAlmostEqual(detection["confidence"], 0.9, places=6)
        self.assertEqual(len(detection["bbox"]), 4)
        self.assertIsNotNone(detection["mask"])
        self.assertIn("model_path", detection)

    def test_process_all_health_rows(self):
        cluster = self.make_cluster()
        cluster.process_all({"LEFT": self.frame()})
        self.assertEqual(len(cluster.last_health), 2)
        self.assertTrue(all(row["ok"] for row in cluster.last_health))
        self.assertEqual(cluster.last_health[0]["role"], "LEFT")

    def test_process_all_unknown_role(self):
        cluster = self.make_cluster()
        with self.assertRaisesRegex(ValueError, "Unknown camera role"):
            cluster.process_all({"NOPE": self.frame()})

    def test_process_all_small_frame(self):
        cluster = self.make_cluster()
        with self.assertRaisesRegex(ValueError, "Invalid frame"):
            cluster.process_all({"LEFT": np.zeros((100, 100, 3), dtype=np.uint8)})

    def test_inference_failure_raises_and_health(self):
        cluster = self.make_cluster(yolo_class=FailingYOLO)
        with self.assertRaisesRegex(RuntimeError, "Model inference failed"):
            cluster.process_all({"LEFT": self.frame()})
        self.assertFalse(cluster.last_health[0]["ok"])
        self.assertIn("RuntimeError", cluster.last_health[0]["error"])

    def test_per_entry_iou_disabled_nms(self):
        """Трёхкамерник работает с iou=0.0 (без NMS-подавления)."""
        cluster = self.make_cluster()
        cluster.process_all({
            "LEFT": self.frame(),
            "RIGHT": self.frame(),
            "MIDDLE": self.frame(),
        })
        for model in cluster.models.values():
            call = model.predict_calls[-1]
            self.assertEqual(call["iou"], 0.0)

    def test_parse_predictions_without_masks(self):
        cluster = self.make_cluster()
        preds = [FakeResult(
            {0: "windows"},
            FakeBoxes(
                xyxy=np.array([[1.0, 2.0, 3.0, 4.0]], dtype=np.float32),
                conf=np.array([0.8], dtype=np.float32),
                cls=np.array([0], dtype=np.float32),
            ),
            None,
        )]
        parsed = cluster._parse_predictions(preds)
        self.assertEqual(len(parsed), 1)
        self.assertIsNone(parsed[0]["mask"])

    def test_parse_predictions_empty_boxes(self):
        cluster = self.make_cluster()
        self.assertEqual(cluster._parse_predictions([]), [])

    def test_is_valid(self):
        self.assertTrue(vision_cluster.VisionCluster._is_valid(
            {"class": "a", "confidence": 0.5},
        ))
        self.assertFalse(vision_cluster.VisionCluster._is_valid(None))
        self.assertFalse(vision_cluster.VisionCluster._is_valid({"class": "a"}))
        self.assertFalse(vision_cluster.VisionCluster._is_valid(
            {"class": "a", "confidence": float("nan")},
        ))
        self.assertFalse(vision_cluster.VisionCluster._is_valid(
            {"class": "a", "confidence": 0.5, "bbox": [1, 2]},
        ))

    def test_device_auto_resolves_without_cuda(self):
        with mock.patch.object(
            vision_cluster, "_cuda_available", return_value=False,
        ):
            cluster = self.make_cluster(device="auto")
        self.assertEqual(cluster.device, "cpu")

    def test_device_auto_uses_gpu_when_available(self):
        with mock.patch.object(
            vision_cluster, "_cuda_available", return_value=True,
        ):
            cluster = self.make_cluster(device="auto")
        self.assertEqual(cluster.device, "0")

    def test_warmup_falls_back_to_cpu_on_gpu_error(self):
        class GpuOnlyFailingYOLO(FakeYOLO):
            def predict(self, frame, **kwargs):
                if kwargs.get("device") != "cpu":
                    raise RuntimeError("cuda unavailable")
                super().predict(frame, **kwargs)

        with mock.patch.object(
            vision_cluster, "_cuda_available", return_value=True,
        ):
            cluster = self.make_cluster(
                device="auto", yolo_class=GpuOnlyFailingYOLO,
            )
        self.assertEqual(cluster.device, "0")
        cluster.warmup()
        self.assertEqual(cluster.device, "cpu")

    def test_warmup_fallback_cpu_only_keeps_cpu(self):
        # Явный cpu: без фолбэка (он не нужен), ошибка поднимается.
        cluster = self.make_cluster(yolo_class=FailingYOLO, device="cpu")
        with self.assertRaisesRegex(RuntimeError, "warmup failed"):
            cluster.warmup()
        self.assertEqual(cluster.device, "cpu")


class DeviceResolutionTest(unittest.TestCase):
    def test_auto_without_cuda(self):
        with mock.patch.object(
            vision_cluster, "_cuda_available", return_value=False,
        ):
            self.assertEqual(vision_cluster.resolve_device("auto"), "cpu")
            self.assertEqual(vision_cluster.resolve_device(None), "cpu")
            self.assertEqual(vision_cluster.resolve_device(""), "cpu")

    def test_auto_with_cuda(self):
        with mock.patch.object(
            vision_cluster, "_cuda_available", return_value=True,
        ):
            self.assertEqual(vision_cluster.resolve_device("auto"), "0")

    def test_explicit_values_trusted_as_is(self):
        # Явный выбор не проверяет доступность устройства: при сбое
        # прогрева система сама вернётся на cpu.
        with mock.patch.object(
            vision_cluster, "_cuda_available", return_value=False,
        ):
            self.assertEqual(vision_cluster.resolve_device("cpu"), "cpu")
            self.assertEqual(vision_cluster.resolve_device("CPU"), "cpu")
            self.assertEqual(vision_cluster.resolve_device("0"), "0")
            self.assertEqual(vision_cluster.resolve_device("1"), "1")
            self.assertEqual(vision_cluster.resolve_device("gpu"), "0")
            self.assertEqual(vision_cluster.resolve_device("cuda"), "0")
            self.assertEqual(vision_cluster.resolve_device("cuda:1"), "1")
            self.assertEqual(vision_cluster.resolve_device("Cuda:2"), "2")

    def test_invalid_device_raises(self):
        for bad in ("banana", "cuda:", "cuda:x", "cuda:-1", "cuda 1"):
            with self.assertRaises(ValueError, msg=bad):
                vision_cluster.resolve_device(bad)


if __name__ == "__main__":
    unittest.main()
