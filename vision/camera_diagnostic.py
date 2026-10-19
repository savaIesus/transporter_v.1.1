"""Консольная диагностика USB-камер без запуска линии.

Запуск (Windows):
    camera_check.bat
    .venv\\Scripts\\python.exe -m vision.camera_diagnostic
    .venv\\Scripts\\python.exe -m vision.camera_diagnostic --mapping camera_mapping.json
    .venv\\Scripts\\python.exe -m vision.camera_diagnostic --scan-limit 12

Фаза 1 — изолированная проверка каждой Camera ID 0..N-1 ровно так, как
это делает мастер калибровки: перебор backend-ов (DirectShow → Media
Foundation на Windows) до первого валидного кадра 1280x720, проверка
яркости. Показывает, какие физические камеры отвечают, под каким API и
на каком индексе. Камеры при этом не стримят одновременно — результаты
детерминированы и не зависят от гонки за полосу USB.

Фаза 2 — воспроизведение основного запуска: три роли из
camera_mapping.json последовательно проходят ``VideoCapture`` и
``isOpened()`` через тот же CameraManager, что использует run.bat.
Открытые устройства удерживаются до завершения фазы.

Инструмент ничего не изменяет: camera_mapping.json не перезаписывается.
Переназначение ролей выполняется мастером run_camera_calibration.bat.

Код выхода: 0 — найдено не менее 3 исправных ID и (если mapping задан)
все 3 роли открылись; 1 — любая проблема.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np

from config.camera_mapping import REQUIRED_ROLES, validate_camera_mapping
from vision.camera_manager import CameraManager

REQUIRED_CAMERA_COUNT = len(REQUIRED_ROLES)
DEFAULT_SCAN_LIMIT = 10
DEFAULT_MAPPING = "camera_mapping.json"


# --- Переиспользуемая логика проверки камер, вынесенная из калибратора. ---
# Калибратор (camera_calibration_console) упрощён до открытия камер как в
# основной программе; подробный перебор backend-ов и проба кадров нужны
# только диагностике и живут здесь.

_BACKEND_ALIASES = {
    "dshow": "CAP_DSHOW",
    "msmf": "CAP_MSMF",
    "v4l2": "CAP_V4L2",
    "avfoundation": "CAP_AVFOUNDATION",
    "any": "CAP_ANY",
}

EXPECTED_SIZE = (1280, 720)
PROBE_READ_INTERVAL = 0.03
NEAR_BLACK_MEAN_MAX = 5.0
NEAR_BLACK_P99_MAX = 12.0


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        print(f"[CAMERA DIAGNOSTIC] {name}={raw!r}: используется {default}")
        return default


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        print(f"[CAMERA DIAGNOSTIC] {name}={raw!r}: используется {default}")
        return default


SCAN_PROBE_ATTEMPTS = _env_int("CAMERA_SCAN_PROBE_ATTEMPTS", 30, minimum=1)
SCAN_OPEN_ATTEMPTS = _env_int("CAMERA_SCAN_OPEN_ATTEMPTS", 2, minimum=1)
SCAN_RETRY_DELAY = _env_float("CAMERA_SCAN_RETRY_DELAY", 0.25, minimum=0.0)


def _camera_backends() -> tuple:
    """Backend-ы для диагностики камер."""
    raw = os.environ.get("CAMERA_BACKENDS")
    if raw:
        configured = []
        for token in raw.split(","):
            attribute = _BACKEND_ALIASES.get(token.strip().lower())
            value = getattr(cv2, attribute, None) if attribute else None
            if value is not None:
                configured.append(value)
        if configured:
            return tuple(configured)
    if sys.platform == "win32":
        return tuple(
            backend
            for backend in (
                getattr(cv2, "CAP_DSHOW", None),
                getattr(cv2, "CAP_MSMF", None),
            )
            if backend is not None
        )
    return (getattr(cv2, "CAP_ANY", 0),)


def _backend_label(backend) -> str:
    if backend is None:
        return "default"
    for name in (
        "CAP_DSHOW",
        "CAP_MSMF",
        "CAP_V4L2",
        "CAP_AVFOUNDATION",
        "CAP_GSTREAMER",
        "CAP_ANY",
    ):
        if getattr(cv2, name, None) == backend:
            return name.replace("CAP_", "")
    return str(backend)


def _open_capture(camera_id: int, backend=None):
    """Открыть камеру выбранным backend-ом."""
    try:
        if backend is None:
            return cv2.VideoCapture(camera_id)
        return cv2.VideoCapture(camera_id, backend)
    except Exception as exc:
        print(f"[CAMERA DIAGNOSTIC] Camera {camera_id}: {exc}")
        return None


def _configure_capture(capture):
    # Тот же рабочий формат, что и CameraManager в основной программе.
    CameraManager._configure_capture(capture)


def _frame_error(frame) -> str | None:
    array = np.asarray(frame)
    if array.ndim != 3 or array.shape[2] < 3:
        return f"неверная форма кадра: {array.shape}"
    height, width = array.shape[:2]
    if (width, height) != EXPECTED_SIZE:
        return (
            f"разрешение {width}x{height}; "
            f"требуется {EXPECTED_SIZE[0]}x{EXPECTED_SIZE[1]}"
        )
    sample = array[::12, ::12, :3].astype(np.float32)
    luminance = sample.mean(axis=2)
    mean = float(luminance.mean())
    p99 = float(np.percentile(luminance, 99))
    if mean <= NEAR_BLACK_MEAN_MAX and p99 <= NEAR_BLACK_P99_MAX:
        return f"почти чёрный кадр: mean={mean:.2f}, p99={p99:.2f}"
    return None


def _probe_capture(capture, attempts: int = SCAN_PROBE_ATTEMPTS):
    """Дать камере отдать валидный кадр."""
    error = "камера не вернула кадр"
    for _ in range(max(1, int(attempts))):
        ok, frame = capture.read()
        if ok and frame is not None:
            error = _frame_error(frame)
            if error is None:
                return frame, None
        time.sleep(PROBE_READ_INTERVAL)
    return None, error


def _safe_release(capture) -> None:
    """Освободить handle камеры; ошибка закрытия не должна ломать сценарий."""
    if capture is None:
        return
    try:
        capture.release()
    except Exception as exc:
        print(f"[CAMERA DIAGNOSTIC] Ошибка освобождения камеры: {exc}")


def _factory_takes_backend(factory) -> bool:
    """Понять, принимает ли фабрика backend вторым аргументом."""
    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):
        return False
    parameters = list(signature.parameters.values())
    if any(
        parameter.kind is inspect.Parameter.VAR_POSITIONAL
        for parameter in parameters
    ):
        return True
    positional = [
        parameter
        for parameter in parameters
        if parameter.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]
    return len(positional) >= 2


def _probe_backend(camera_id, backend, factory, attempts):
    """Проверить камеру одним backend-ом; вернуть словарь результата."""
    label = _backend_label(backend)
    factory_takes_backend = _factory_takes_backend(factory)
    capture = None
    try:
        capture = (
            factory(camera_id, backend)
            if factory_takes_backend
            else factory(camera_id)
        )
        if capture is None or not capture.isOpened():
            return {"backend": label, "ok": False, "detail": "устройство не открылось"}
        _configure_capture(capture)
        frame, error = _probe_capture(capture, attempts=attempts)
        if error is None and frame is not None:
            height, width = frame.shape[:2]
            return {
                "backend": label,
                "ok": True,
                "detail": f"{width}x{height}",
                "frame": frame,
            }
        return {"backend": label, "ok": False, "detail": error or "кадр не получен"}
    except Exception as exc:
        return {
            "backend": label,
            "ok": False,
            "detail": f"{type(exc).__name__}: {exc}",
        }
    finally:
        _safe_release(capture)


def scan_isolated(scan_limit, factory) -> dict:
    """Проверить Camera ID 0..scan_limit-1 поодиночке.

    Возвращает {camera_id: {"ok": bool, "backend": str|None,
    "detail": str}} — по одному результату на ID (первый успешный backend
    или объединение ошибок всех попыток).
    """
    backends = _camera_backends() or (None,)
    results = {}
    for camera_id in range(int(scan_limit)):
        attempts = []
        ok_entry = None
        for backend in backends:
            for _ in range(SCAN_OPEN_ATTEMPTS):
                entry = _probe_backend(
                    camera_id, backend, factory, attempts=SCAN_PROBE_ATTEMPTS
                )
                if entry["ok"]:
                    ok_entry = entry
                    break
                text = f"{entry['backend']}: {entry['detail']}"
                # Повторная попытка тем же backend-ом даёт ту же ошибку —
                # не дублируем строку в отчёте.
                if not attempts or attempts[-1] != text:
                    attempts.append(text)
                if SCAN_RETRY_DELAY > 0:
                    time.sleep(SCAN_RETRY_DELAY)
            if ok_entry is not None:
                break
        if ok_entry is not None:
            results[camera_id] = ok_entry
        else:
            results[camera_id] = {
                "ok": False,
                "backend": None,
                "detail": "; ".join(attempts) or "устройство не открылось",
            }
    return results


def _load_mapping(path) -> dict | None:
    """Прочитать и проверить camera_mapping.json; None, если файла нет."""
    try:
        with open(path, encoding="utf-8") as stream:
            raw = json.load(stream)
        return validate_camera_mapping(raw)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[CAMERA DIAGNOSTIC] Ошибка чтения {path}: {exc}")
        return None


def check_mapping(mapping_path, factory) -> tuple[bool, str]:
    """Последовательно открыть роли из mapping точно как run.bat."""
    try:
        manager = CameraManager(
            config_file=mapping_path, capture_factory=factory
        )
    except RuntimeError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    count = len(manager.cameras)
    manager.release()
    return True, f"Открыто камер: {count}/{REQUIRED_CAMERA_COUNT}"


def _failed_roles_from_message(message: str) -> list:
    """Найти роль из короткой ошибки открытия CameraManager."""
    return [role for role in REQUIRED_ROLES if role in message]


def _bad_mapped_roles(scan_results, mapping) -> list:
    """Список (camera_id, role) из mapping, чей ID не отвечает в скане."""
    bad = []
    for role, camera_id in sorted(mapping.items()):
        if not scan_results.get(camera_id, {}).get("ok"):
            bad.append((camera_id, role))
    return bad


def _format_report(
    scan_results: dict,
    mapping: dict | None,
    mapping_ok: bool | None,
    mapping_message: str,
) -> str:
    """Собрать текстовый отчёт диагностики."""
    lines = []
    lines.append("=" * 64)
    lines.append(" ДИАГНОСТИКА КАМЕР")
    lines.append("=" * 64)
    lines.append(
        "Убедись, что линия (run.bat) не запущена и камеры не заняты "
        "другими программами (Zoom, Teams, OBS, «Камера» Windows)."
    )
    lines.append("")

    role_by_id = {}
    if mapping:
        role_by_id = {camera_id: role for role, camera_id in mapping.items()}

    lines.append("[1/2] Изолированная проверка Camera ID поодиночке")
    working = []
    for camera_id, entry in sorted(scan_results.items()):
        if entry["ok"]:
            working.append(camera_id)
            note = (
                f" -> в mapping: {role_by_id[camera_id]}"
                if camera_id in role_by_id else ""
            )
            lines.append(
                f"  Camera ID {camera_id:>2}: OK "
                f"({entry['backend']}, {entry['detail']}){note}"
            )
        else:
            role_note = (
                f" -> в mapping: {role_by_id[camera_id]}"
                if camera_id in role_by_id else ""
            )
            lines.append(
                f"  Camera ID {camera_id:>2}: ОШИБКА "
                f"({entry['detail']}){role_note}"
            )
    lines.append("")
    lines.append(
        f"  Исправно: {len(working)}/{len(scan_results)} "
        f"(нужно >= {REQUIRED_CAMERA_COUNT} для линии)"
    )
    lines.append("")

    if mapping is not None:
        lines.append("[2/2] Проверка ролей из camera_mapping.json (открытие как в run.bat)")
        for line in mapping_message.splitlines():
            lines.append(f"  {line}")
        lines.append("")
        if mapping_ok:
            lines.append("  Итог: все роли открылись — mapping актуален.")
        else:
            failed = _failed_roles_from_message(mapping_message)
            if failed:
                lines.append(
                    "  Итог: не открылись роли: " + ", ".join(sorted(failed))
                )
            else:
                lines.append("  Итог: роли открылись не полностью (см. выше).")
        lines.append("")

    lines.append("РЕЗЮМЕ")
    if len(working) < REQUIRED_CAMERA_COUNT:
        lines.append(
            f"  Исправных камер {len(working)} < {REQUIRED_CAMERA_COUNT}: "
            "линия не запустится."
        )
        bad_mapped = _bad_mapped_roles(scan_results, mapping) if mapping else []
        if bad_mapped:
            lines.append(
                "  Роли с неотвечающими ID: "
                + ", ".join(
                    f"{role} (id {camera_id})"
                    for camera_id, role in bad_mapped
                )
            )
        else:
            missing_ids = [
                camera_id for camera_id in sorted(scan_results)
                if not scan_results[camera_id]["ok"]
            ]
            lines.append(
                "  Не отвечают ID: "
                + (", ".join(map(str, missing_ids)) or "все")
            )
        lines.append(
            "  Проверь питание и USB-кабели этих камер, убедись, что их не "
            "заняла другая программа, и повтори диагностику."
        )
        lines.append(
            "  Если камера заработала на другом индексе — запусти "
            "run_camera_calibration.bat для переназначения ролей."
        )
    elif mapping is None:
        lines.append(
            f"  Найдено исправных камер: {len(working)}. Файл "
            f"{DEFAULT_MAPPING} отсутствует или не читается — запусти "
            "run_camera_calibration.bat для назначения ролей."
        )
    elif mapping_ok:
        lines.append(
            f"  Все {REQUIRED_CAMERA_COUNT} ролей открылись — mapping актуален, "
            "камеры готовы к запуску линии."
        )
    else:
        bad_mapped = _bad_mapped_roles(scan_results, mapping)
        if bad_mapped:
            lines.append(
                "  Роли с неотвечающими ID: "
                + ", ".join(
                    f"{role} (id {camera_id})"
                    for camera_id, role in bad_mapped
                )
            )
        lines.append(
            "  Камеры исправны, но открытие ролей из mapping не удалось: "
            "индексы изменились, камера занята другой программой или "
            "перегрелась."
        )
        lines.append(
            "  Запусти run_camera_calibration.bat для переназначения ролей "
            "и повтори диагностику."
        )
    lines.append("")
    lines.append("Ничего не изменено: camera_mapping.json не перезаписывался.")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Диагностика USB-камер без запуска линии"
    )
    parser.add_argument(
        "--scan-limit",
        type=int,
        default=DEFAULT_SCAN_LIMIT,
        help="сколько Camera ID проверять (по умолчанию 10)",
    )
    parser.add_argument(
        "--mapping",
        default=None,
        help=f"путь к camera_mapping.json (по умолчанию {DEFAULT_MAPPING})",
    )
    parser.add_argument(
        "--no-mapping",
        action="store_true",
        help="не проверять роли из mapping (только изолированный скан)",
    )
    args = parser.parse_args(argv)

    mapping_path = args.mapping
    if mapping_path is None and not args.no_mapping:
        mapping_path = DEFAULT_MAPPING

    mapping = None
    if mapping_path is not None and not args.no_mapping:
        resolved = Path(mapping_path)
        if resolved.exists():
            mapping = _load_mapping(str(resolved))
            if mapping is None:
                return 1
        else:
            print(
                f"[CAMERA DIAGNOSTIC] {mapping_path} не найден — "
                "проверка ролей пропущена."
            )
            mapping = None

    print("[CAMERA DIAGNOSTIC] Фаза 1: изолированная проверка Camera ID "
          f"0..{args.scan_limit - 1}")
    scan_results = scan_isolated(args.scan_limit, _open_capture)

    mapping_ok = None
    mapping_message = ""
    if mapping is not None:
        print(
            "[CAMERA DIAGNOSTIC] Фаза 2: последовательное открытие ролей "
            "(как run.bat)"
        )
        mapping_ok, mapping_message = check_mapping(
            mapping_path, _open_capture
        )

    report = _format_report(
        scan_results, mapping, mapping_ok, mapping_message
    )
    print()
    print(report)

    working_count = sum(1 for entry in scan_results.values() if entry["ok"])
    if working_count < REQUIRED_CAMERA_COUNT:
        return 1
    if mapping is None:
        return 0
    return 0 if mapping_ok else 1


if __name__ == "__main__":
    sys.exit(main())
