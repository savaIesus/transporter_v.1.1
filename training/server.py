"""Отдельный FastAPI-мастер ручной разметки кадров."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Sequence

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from training.collector import (
    CollectorError,
    TrainingCollector,
    TrainingConfig,
    ValidationError,
)
from training.yolo import export_yolo_dataset

_UI_FOLDER = Path(__file__).parent / "ui"


def create_app(
    collector: TrainingCollector | None = None,
    *,
    frame_source=None,
    config: TrainingConfig | None = None,
    conveyor=None,
) -> FastAPI:
    """Создать независимое приложение мастера обучения.

    Для production-запуска передайте ``CameraManager``-адаптер через
    ``frame_source``, а шаг ленты — через ``conveyor``. В тестах удобно
    передать уже созданный ``TrainingCollector`` с фейковым источником
    кадров.
    """
    if collector is None:
        collector = TrainingCollector(
            config or TrainingConfig(),
            frame_source=frame_source,
            conveyor=conveyor,
        )
    else:
        if frame_source is not None:
            collector.frame_source = frame_source
        if conveyor is not None:
            collector.conveyor = conveyor

    app = FastAPI(
        title="Мастер сбора датасета YOLO",
        description="Последовательная съёмка камер и ручная разметка дефектов",
    )
    app.state.training_collector = collector

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(_UI_FOLDER / "index.html", media_type="text/html")

    app.mount(
        "/static",
        StaticFiles(directory=_UI_FOLDER / "static"),
        name="training-static",
    )

    def current_collector() -> TrainingCollector:
        return app.state.training_collector

    @app.get("/api/training/state")
    async def get_state():
        return JSONResponse(_state_with_urls(current_collector()))

    @app.get("/api/training/config")
    async def get_config():
        item = current_collector()
        return JSONResponse(
            {
                "camera_roles": list(item.config.camera_roles),
                "defect_classes": list(item.config.defect_classes),
                "yolo_task": item.config.yolo_task,
                "root_folder": str(item.config.root_folder),
                "conveyor_step": item.conveyor is not None,
            }
        )

    @app.post("/api/training/session")
    async def start_session(payload: dict[str, Any] | None = None):
        payload = payload or {}
        item = current_collector()
        try:
            await asyncio.to_thread(
                item.start_session,
                _optional_text(payload.get("dataset_name")),
                defect_classes=_list_value(
                    payload.get("defect_classes"), item.config.defect_classes
                ),
                camera_roles=_list_value(
                    payload.get("camera_roles"), item.config.camera_roles
                ),
            )
        except ValidationError as exc:
            return _error_response(exc, 400)
        except CollectorError as exc:
            return _error_response(exc, 409)
        return JSONResponse(_state_with_urls(item))

    @app.post("/api/training/classes")
    async def add_class(payload: dict[str, Any] | None = None):
        payload = payload or {}
        item = current_collector()
        try:
            classes = await asyncio.to_thread(
                item.add_defect_class,
                _optional_text(payload.get("class_name")),
            )
        except ValidationError as exc:
            return _error_response(exc, 400)
        except CollectorError as exc:
            return _error_response(exc, 409)
        return JSONResponse(
            {
                "ok": True,
                "defect_classes": classes,
                "state": _state_with_urls(item),
            }
        )

    @app.post("/api/training/parts")
    async def start_part(payload: dict[str, Any] | None = None):
        payload = payload or {}
        item = current_collector()
        try:
            await asyncio.to_thread(item.begin_part, _optional_text(payload.get("label")))
        except CollectorError as exc:
            return _error_response(exc, 409)
        return JSONResponse(_state_with_urls(item))

    @app.post("/api/training/capture")
    async def capture_next():
        item = current_collector()
        try:
            record = await asyncio.to_thread(item.capture_next)
        except CollectorError as exc:
            return _error_response(exc, 409)
        except Exception as exc:
            return _error_response(exc, 500)
        return JSONResponse(
            {
                "ok": True,
                "frame": _record_with_urls(record),
                "state": _state_with_urls(item),
            }
        )

    @app.post("/api/training/annotations")
    async def save_annotation(payload: dict[str, Any] | None = None):
        payload = payload or {}
        item = current_collector()
        regions = payload.get("annotations")
        if regions is None:
            regions = payload.get("polygons")
        if regions is None:
            regions = payload.get("boxes")
        if regions is None and payload.get("bbox") is not None:
            regions = [{"bbox": payload.get("bbox")}]
        if regions is not None and not isinstance(regions, list):
            field_name = (
                "annotations"
                if payload.get("annotations") is not None
                else "polygons"
                if payload.get("polygons") is not None
                else "boxes"
            )
            return _error_response(
                ValidationError(f"{field_name} должен быть списком областей"),
                400,
            )
        # image_id принимается для понятного контракта API, но разметка всегда
        # проверяется относительно current_frame_id: нельзя случайно записать
        # область в предыдущий кадр при двойном клике интерфейса.
        expected_id = item.current_frame_id
        if payload.get("image_id") is not None and expected_id != int(payload["image_id"]):
            return _error_response(
                CollectorError("Кадр уже изменился; обновите текущий экран"),
                409,
            )
        try:
            record = await asyncio.to_thread(
                item.annotate_current,
                defect_class=_optional_text(payload.get("defect_class")),
                annotations=regions or (),
            )
        except ValidationError as exc:
            return _error_response(exc, 400)
        except CollectorError as exc:
            return _error_response(exc, 409)
        return JSONResponse(
            {
                "ok": True,
                "frame": _record_with_urls(record),
                "state": _state_with_urls(item),
            }
        )

    @app.post("/api/training/skip")
    async def skip_annotation(payload: dict[str, Any] | None = None):
        item = current_collector()
        expected_id = item.current_frame_id
        if (
            payload
            and payload.get("image_id") is not None
            and expected_id != int(payload["image_id"])
        ):
            return _error_response(
                CollectorError("Кадр уже изменился; обновите текущий экран"),
                409,
            )
        try:
            record = await asyncio.to_thread(item.skip_current)
        except CollectorError as exc:
            return _error_response(exc, 409)
        return JSONResponse(
            {
                "ok": True,
                "frame": _record_with_urls(record),
                "state": _state_with_urls(item),
            }
        )

    @app.post("/api/training/parts/finish")
    async def finish_part():
        item = current_collector()
        try:
            result = await asyncio.to_thread(item.finish_part)
        except CollectorError as exc:
            return _error_response(exc, 409)
        except Exception as exc:
            return _error_response(exc, 500)
        return JSONResponse(_json_safe(result))

    @app.post("/api/training/conveyor/step")
    async def step_conveyor():
        item = current_collector()
        try:
            result = await asyncio.to_thread(item.step_conveyor)
        except CollectorError as exc:
            return _error_response(exc, 409)
        except Exception as exc:
            return _error_response(exc, 500)
        return JSONResponse(_json_safe(result))

    @app.post("/api/training/session/finish")
    async def finish_session():
        item = current_collector()
        try:
            result = await asyncio.to_thread(item.finish_session)
        except CollectorError as exc:
            return _error_response(exc, 409)
        except Exception as exc:
            return _error_response(exc, 500)
        return JSONResponse(_json_safe(result))

    @app.post("/api/training/export")
    async def export_dataset():
        item = current_collector()
        if item.store is None:
            return _error_response(
                CollectorError("Сначала создайте сессию обучения"),
                409,
            )
        try:
            result = await asyncio.to_thread(
                export_yolo_dataset,
                item.store,
                validation_ratio=item.config.validation_ratio,
            )
        except Exception as exc:
            return _error_response(exc, 400)
        return JSONResponse({"ok": True, "yolo": result.to_dict()})

    @app.get("/api/training/images/{image_id}/{kind}")
    async def get_image(image_id: int, kind: str):
        if kind not in (
            "original",
            "annotated",
            "detail_original",
            "detail_annotated",
        ):
            raise HTTPException(400, "Недопустимый тип изображения")
        item = current_collector()
        try:
            path = await asyncio.to_thread(item.image_path, image_id, kind)
        except CollectorError as exc:
            raise HTTPException(404, str(exc)) from exc
        return FileResponse(
            path,
            media_type="image/jpeg",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @app.get("/api/training/manifest")
    async def get_manifest():
        item = current_collector()
        if item.store is None:
            return JSONResponse({"frames": [], "parts": []})
        return JSONResponse(
            {
                "frames": [record.to_dict() for record in item.store.get_frames()],
                "parts": item.store.get_parts(),
            }
        )

    return app


def _record_with_urls(record) -> dict[str, Any]:
    data = record.to_dict()
    data["original_url"] = f"/api/training/images/{record.image_id}/original"
    if record.annotated:
        data["annotated_url"] = f"/api/training/images/{record.image_id}/annotated"
    return data


def _state_with_urls(collector: TrainingCollector) -> dict[str, Any]:
    state = collector.snapshot()
    frame = state.get("current_frame")
    if isinstance(frame, dict):
        image_id = frame.get("image_id")
        if image_id is not None:
            frame["original_url"] = f"/api/training/images/{image_id}/original"
            if frame.get("annotated"):
                frame["annotated_url"] = (
                    f"/api/training/images/{image_id}/annotated"
                )
    return state


def _list_value(value: Any, default: Sequence[str]) -> list[str]:
    if value is None:
        return list(default)
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    raise ValidationError("Значение должно быть списком или строкой через запятую")


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _error_response(exc: Exception, status_code: int) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": str(exc), "type": type(exc).__name__},
        status_code=status_code,
    )


def _json_safe(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return _json_safe(value.to_dict())
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    return value
