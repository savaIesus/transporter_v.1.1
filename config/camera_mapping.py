import json


CAMERA_MAPPING_FILE = "camera_mapping.json"
REQUIRED_ROLES = {
    "LEFT",
    "MIDDLE",
    "RIGHT",
}


def validate_camera_mapping(mapping: dict) -> dict:
    """Проверить полный взаимно-однозначный mapping трёх камер."""

    if not isinstance(mapping, dict):
        raise ValueError("camera_mapping должен содержать объект")
    missing = REQUIRED_ROLES - set(mapping)
    extra = set(mapping) - REQUIRED_ROLES
    if missing or extra:
        raise ValueError(
            f"camera_mapping mismatch: missing={sorted(missing)}, "
            f"extra={sorted(extra)}"
        )
    ids = list(mapping.values())
    if any(type(camera_id) is not int or camera_id < 0 for camera_id in ids):
        raise ValueError("camera IDs должны быть неотрицательными int")
    if len(ids) != len(set(ids)):
        raise ValueError("camera IDs должны быть уникальными")
    return dict(mapping)


def load_camera_mapping(path: str | None = None) -> dict:
    """Загрузить и проверить маппинг трёх камер."""

    path = path or CAMERA_MAPPING_FILE
    try:
        with open(path, encoding="utf-8") as stream:
            mapping = json.load(stream)
    except FileNotFoundError as exc:
        raise RuntimeError(f"[CAMERA_MAPPING] {path} не найден") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"[CAMERA_MAPPING] Ошибка чтения {path}: {exc}") from exc
    return validate_camera_mapping(mapping)
