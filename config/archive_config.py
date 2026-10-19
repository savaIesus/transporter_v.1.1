"""Настройка корневой папки обязательного архива партий.

Политика фиксирована в :class:`inspection.part_archive.PartArchive`: архив
всегда включён, JPEG сохраняется с максимальным качеством, а при выходе партия
всегда упаковывается в ZIP с удалением исходной папки после проверки CRC.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


ARCHIVE_CONFIG_FILE = "archive_config.json"

DEFAULTS = {
    "root_path": "archive",
}


def _normalise_root(value) -> str:
    text = str(value or DEFAULTS["root_path"]).strip()
    if not text:
        text = DEFAULTS["root_path"]
    return os.path.expandvars(os.path.expanduser(text))


def normalise_archive_config(data: dict | None) -> dict:
    """Оставить только путь, удаляя устаревшие переключатели политики."""
    source = data if isinstance(data, dict) else {}
    return {
        "root_path": _normalise_root(source.get("root_path")),
    }


def load_archive_config(path: str = ARCHIVE_CONFIG_FILE) -> dict:
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        result = normalise_archive_config(None)
        try:
            save_archive_config(path, result)
        except OSError as exc:
            print(f"[ARCHIVE] Не удалось создать {path}: {exc}")
        return result
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[ARCHIVE] Ошибка чтения {path}: {exc}; используются defaults")
        return normalise_archive_config(None)

    result = normalise_archive_config(data)
    if result != data:
        try:
            save_archive_config(path, result)
        except OSError as exc:
            print(f"[ARCHIVE] Не удалось обновить {path}: {exc}")
    return result


def save_archive_config(path: str, data: dict) -> dict:
    result = normalise_archive_config(data)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + ".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, destination)
    return result
