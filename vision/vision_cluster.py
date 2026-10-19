import math
import os
import time

from ultralytics import YOLO
import numpy as np

from vision.model_config import MODEL_GROUPS, ROLE_TO_GROUP

INFERENCE_IMGSZ = 1280

# IoU по умолчанию; трёхкамерник работал с iou=0 (без NMS-подавления),
# поэтому per-entry "iou" в model_config имеет приоритет.
AGGRESSIVE_IOU_ROLES = set()
DEFAULT_IOU    = 0.45
AGGRESSIVE_IOU = 0.10


def _cuda_available() -> bool:
    """True, если torch видит доступное CUDA-устройство."""
    try:
        import torch
    except ImportError:
        return False
    try:
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def resolve_device(requested: str | None) -> str:
    """Нормализация вычислительного устройства для inference.

    ``auto`` (по умолчанию) — GPU ``"0"`` при доступном CUDA, иначе ``"cpu"``;
    ``gpu``/``cuda`` — алиасы для ``"0"``; ``cuda:N`` и ``"N"`` — явный индекс
    GPU; ``cpu`` — без GPU. Явный выбор (не ``auto``) доверяется как есть:
    при недоступности устройства прогрев упадёт и вернёт систему на ``cpu``.
    """
    value = str(requested or "auto").strip().lower()
    if value == "auto":
        return "0" if _cuda_available() else "cpu"
    if value in ("gpu", "cuda"):
        return "0"
    if value.startswith("cuda:"):
        index = value.split(":", 1)[1]
        if not index.isdigit():
            raise ValueError(f"Invalid CUDA device index: {requested!r}")
        return index
    if value == "cpu":
        return "cpu"
    if value.isdigit():
        return value
    raise ValueError(
        f"Invalid device {requested!r}; expected auto, cpu, gpu, cuda[:N] or index"
    )


class VisionCluster:

    def __init__(self, device: str = "auto", verbose: bool = True):
        self.requested_device = str(device)
        self.device = resolve_device(device)
        self.verbose = verbose
        self.models = {}
        self.last_health = []
        self._load_all_models()
        if self.verbose:
            self._log_device()

    def _log_device(self):
        if self.device != "cpu":
            try:
                import torch
                name = torch.cuda.get_device_name(int(self.device))
                cuda = torch.version.cuda or "?"
                print(f"[VISION] Device: {self.device} ({name}, CUDA {cuda})")
                return
            except Exception:
                pass
        print(f"[VISION] Device: {self.device}")

    def _load_all_models(self):
        for model_list in MODEL_GROUPS.values():
            for entry in model_list:
                path = entry["path"]
                if path not in self.models:
                    if not os.path.isfile(path):
                        raise FileNotFoundError(f"Model file not found: {path}")
                    if self.verbose:
                        print(f"[VISION] Loading {path}")
                    model = YOLO(path)
                    self._verify_model_classes(
                        path,
                        model,
                        tuple(entry.get("classes", ())),
                    )
                    self.models[path] = model
        if self.verbose:
            print(f"[VISION] Models loaded: {len(self.models)}")

    @staticmethod
    def _verify_model_classes(path: str, model, expected: tuple[str, ...]):
        if not expected:
            return
        names = getattr(model, "names", None)
        if isinstance(names, dict):
            actual = tuple(str(names[index]) for index in sorted(names))
        elif isinstance(names, (list, tuple)):
            actual = tuple(str(name) for name in names)
        else:
            raise RuntimeError(
                f"Model {path} has no readable class names; expected {expected}"
            )
        if actual != expected:
            raise RuntimeError(
                f"Model class mismatch for {path}: actual={actual}, expected={expected}"
            )

    def warmup(self):
        # Прогрев на выбранном устройстве; если GPU не выдержал (драйвер,
        # OOM и т.п.) — автоматический повтор на cpu с предупреждением.
        errors = []
        if self._warmup_once(errors):
            if self.verbose:
                print("[VISION] Warmup done")
            return
        if self.device != "cpu":
            print(
                f"[VISION WARN] Warmup failed on device {self.device!r}; "
                "falling back to cpu"
            )
            self.device = "cpu"
            if self._warmup_once(errors):
                if self.verbose:
                    print("[VISION] Warmup done (cpu fallback)")
                return
        raise RuntimeError("Model warmup failed: " + "; ".join(errors))

    def _warmup_once(self, errors: list) -> bool:
        dummy = np.zeros((720, 1280, 3), dtype=np.uint8)
        errors.clear()
        for path, model in self.models.items():
            try:
                model.predict(
                    dummy,
                    device=self.device,
                    verbose=False,
                    imgsz=INFERENCE_IMGSZ,
                    retina_masks=True,
                )
            except Exception as e:
                errors.append(f"{path}: {type(e).__name__}: {e}")
        return not errors

    def process_all(self, frames: dict) -> dict:
        results = {}
        health = []
        self.last_health = []

        for role, frame in frames.items():
            group_name = ROLE_TO_GROUP.get(role)
            if not group_name:
                raise ValueError(f"Unknown camera role: {role}")
            array = np.asarray(frame)
            if array.ndim != 3 or array.shape[0] < 240 or array.shape[1] < 320:
                raise ValueError(f"Invalid frame for {role}: shape={array.shape}")

            role_iou = (
                AGGRESSIVE_IOU
                if role in AGGRESSIVE_IOU_ROLES
                else DEFAULT_IOU
            )

            detections = []

            for entry in MODEL_GROUPS[group_name]:
                path  = entry["path"]
                conf  = entry["conf"]
                kind  = entry.get("kind", "")
                # Per-entry iou (трёхкамерник использовал iou=0).
                entry_iou = entry.get("iou")
                iou = role_iou if entry_iou is None else float(entry_iou)
                model = self.models[path]

                started = time.perf_counter()
                try:
                    preds = model.predict(
                        frame,
                        device=self.device,
                        conf=conf,
                        imgsz=INFERENCE_IMGSZ,
                        iou=iou,
                        retina_masks=True,
                        verbose=False,
                    )
                except Exception as e:
                    health.append({
                        "role": role,
                        "model": path,
                        "ok": False,
                        "elapsed_ms": (time.perf_counter() - started) * 1000,
                        "detections": 0,
                        "error": f"{type(e).__name__}: {e}",
                    })
                    self.last_health = health
                    raise RuntimeError(
                        f"Model inference failed for {role} / {path}: "
                        f"{type(e).__name__}: {e}"
                    ) from e

                parsed = self._parse_predictions(preds)
                health.append({
                    "role": role,
                    "model": path,
                    "ok": True,
                    "elapsed_ms": (time.perf_counter() - started) * 1000,
                    "detections": len(parsed),
                    "error": None,
                })
                for detection in parsed:
                    detection["model_path"] = path
                    detection["kind"] = kind
                if self.verbose:
                    print(
                        f"[VISION] {role} | "
                        f"{path.split('/')[-1]} "
                        f"-> {len(parsed)} det"
                    )
                detections.extend(parsed)

            valid = [d for d in detections if self._is_valid(d)]
            if len(valid) != len(detections):
                print(
                    f"[VISION WARN] {role}: dropped "
                    f"{len(detections) - len(valid)} invalid detections"
                )

            results[role] = valid

        self.last_health = health
        return results

    def _parse_predictions(self, preds) -> list:
        out = []

        for result in preds:
            names   = result.names
            boxes   = result.boxes
            masks   = result.masks

            if boxes is None:
                continue

            xyxy    = boxes.xyxy.cpu().numpy()
            confs   = boxes.conf.cpu().numpy()
            cls_ids = boxes.cls.cpu().numpy().astype(int)

            mask_polys = None
            if masks is not None and masks.xy is not None:
                mask_polys = masks.xy

            for i in range(len(xyxy)):
                det = {
                    "class":      names[cls_ids[i]],
                    "confidence": float(confs[i]),
                    "bbox":       [float(v) for v in xyxy[i]],
                    "mask": (
                        [
                            [float(p[0]), float(p[1])]
                            for p in mask_polys[i]
                        ]
                        if mask_polys is not None
                        and i < len(mask_polys)
                        else None
                    ),
                }
                out.append(det)

        return out

    @staticmethod
    def _is_valid(det: dict) -> bool:
        """Проверка минимальной корректности детекции."""
        if not isinstance(det, dict):
            return False
        if "class" not in det or "confidence" not in det:
            return False
        confidence = det.get("confidence")
        if not isinstance(confidence, (int, float)) or not math.isfinite(confidence):
            return False

        bbox = det.get("bbox")
        if bbox is not None:
            if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
                return False
            if any(
                not isinstance(v, (int, float)) or not math.isfinite(v)
                for v in bbox
            ):
                return False

        return True
