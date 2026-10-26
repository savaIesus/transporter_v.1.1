import asyncio
import os
import sys
import threading
import time
from pathlib import Path

import cv2
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles as StarletteStaticFiles
from fastapi.responses import FileResponse
import uvicorn

from vision.overlay.raw_overlay import RawOverlay
from vision.overlay.debug_overlay import DebugOverlay

from vision.ui.server.routes_frames  import setup_frame_routes
from vision.ui.server.routes_api     import setup_api_routes
from vision.ui.server.routes_archive import setup_archive_routes


_UI_DIR        = Path(__file__).parent.parent
_TEMPLATES_DIR = _UI_DIR / "templates"
_STATIC_DIR    = _UI_DIR / "static"

# HMI всегда должен загружать актуальные файлы: в предпросмотре браузер
# и прокси агрессивно кэшируют страницу, из-за чего старый index.html со
# старым DOM мог сочетаться со свежими скриптами и интерфейс «зависал»
# на сплэше. Поэтому HTML и статику отдаём с запретом кэширования.
NO_CACHE = "no-store, no-cache, must-revalidate"


class NoCacheStaticFiles(StarletteStaticFiles):
    """Статика без кэширования (версии ?v= в HTML и так делают её уникальной)."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = NO_CACHE
        return response


BOOT_STEPS = [
    ("cameras",       "Камеры"),
    ("models_load",   "Загрузка моделей"),
    ("models_warm",   "Прогрев моделей"),
    ("inspection",    "Система контроля"),
    ("serial",        "Контроллер"),
    ("hardware",      "Оборудование"),
    ("cycle",         "Производственный цикл"),
    ("ready",         "Готовность"),
]


CAMERA_ORDER = [
    "LEFT",
    "MIDDLE",
    "RIGHT",
]

# Правило отчёта -> группа порогов (RULE_GROUPS в thresholds.json). Нужна
# только для ручных названий порогов (_label.*): по полному имени параметра
# подставляется пользовательское название вместо встроенного перевода.
RULE_THRESHOLD_GROUPS = {
    "part_presence": "part_presence",
    "uneven_heights": "uneven_heights",
    "window_sinks": "window_sinks",
    "bottom_glass": "bottom_glass",
    "welding": "welding",
}


class UIServer:

    JPEG_QUALITY        = 70
    STREAM_JPEG_QUALITY = 60
    PREVIEW_MAX_WIDTH   = 320
    BOOT_STEPS          = BOOT_STEPS

    def __init__(self):
        self.frames: dict         = {}
        self.camera_roles: list   = []
        # role -> физический Camera ID (индекс устройства) из camera_mapping.json
        self.camera_mapping: dict = {}
        self.vision_results: dict = {}
        # Подпись последней опубликованной порции детекций (см.
        # _vision_signature): сравнение под общим lock должно быть дешёвым.
        self._vision_signature_cache: tuple = ()
        self.rule_results: list   = []
        self.line_status: dict    = {}
        self.recent_parts: list   = []

        self.splash_active = True
        self.splash_log    = []
        self.boot_steps    = {
            key: "pending" for key, _ in BOOT_STEPS
        }
        self.boot_current  = None
        self.boot_message  = "Запуск..."
        self.boot_error    = None

        self.mode = "RULES"

        self.active_camera_role: str | None = None

        self.on_active_camera_changed: callable | None = None

        self.on_start:  callable | None = None
        self.on_stop:   callable | None = None
        self.on_pause:  callable | None = None
        self.on_resume: callable | None = None
        self.on_exit:   callable | None = None
        self.on_distributor_diagnostic: callable | None = None
        self.on_camera_diagnostic: callable | None = None
        self.on_vision_rule_diagnostic: callable | None = None
        self.on_selected_model_analysis: callable | None = None
        self.on_selected_model_release: callable | None = None

        self.on_jog_enter: callable | None = None
        self.on_jog_exit: callable | None = None
        self.on_jog_hold_start: callable | None = None
        self.on_jog_hold_heartbeat: callable | None = None
        self.on_jog_hold_release: callable | None = None

        # Пороги правил: плоский dict (ROLE.parameter -> value), которым
        # UI отвечает на GET /api/thresholds. Применение изменений выполняет
        # внешний callback (валидация + пересоздание DecisionEngine).
        # Помимо сохранения через UI, пороги автоматически перечитываются
        # из thresholds.json (см. reload_thresholds_from_file).
        self.thresholds: dict | None = None
        self.thresholds_revision = 0
        self.on_thresholds_apply: callable | None = None

        # Понятные названия порогов в интерфейсе: ROLE.parameter -> строка.
        # Не влияют на логику правил, только на отображение в панели.
        self.threshold_labels: dict = {}

        # Автоподхват порогов из файла: путь и время последней проверки.
        self.thresholds_path: str | None = None
        self.thresholds_file_mtime: float | None = None
        # Внешний callback: получает свежий dict порогов из файла, может
        # пересоздать DecisionEngine и вернуть итоговый dict.
        self.on_thresholds_reload: callable | None = None

        self.archive = None
        self.archive_config_path = "archive_config.json"

        # Кэш JPEG для pull-механики (/frame): ключ (role, mode, size)
        self._jpeg_cache: dict = {}
        self._cache_version = 0

        # Версия каждого кадра (растёт при update)
        self._latest_frames_ver: dict = {}

        # Ленивый кэш JPEG для stream: role -> (jpeg_bytes, version)
        self._latest_stream_jpeg: dict = {}

        self.lock = threading.Lock()

        self.app = FastAPI(title="Роботехнический комплекс конвейерного типа 3")

        self._setup_static()
        self._setup_routes()

        self._server_thread: threading.Thread | None = None
        self._uvicorn_server: uvicorn.Server | None = None

    # Public API

    @staticmethod
    def _snapshot_vision_results(vision_results: dict) -> dict:
        """Снимок детекций по ролям, независимый от источника.

        ``ProductionCycle`` держит один и тот же ``_last_vision_results`` и
        дополняет его по стадиям (``update()`` внутри шага). Если сохранить
        сам объект, то published-состояние и состояние источника станут
        одним и тем же dict: следующее сравнение всегда даст «не менялось»,
        и RAW-разметка не попадёт в кэш JPEG. Поэтому публикуется копия.
        """
        return {
            role: list(detections) if isinstance(detections, list)
            else detections
            for role, detections in (vision_results or {}).items()
        }

    @staticmethod
    def _vision_signature(vision_results: dict):
        """Дешёвая подпись публикации детекций.

        Полное рекурсивное сравнение здесь недопустимо: маски несут тысячи
        точек, и обход занимает десятки миллисекунд под общим ``self.lock``,
        который держат и ``/api/status``, и рендер кадров. Для инвалидации
        кэша достаточно заметить смену набора ролей и состава детекций —
        внутри одного шага модели не переписывают уже опубликованные
        объекты, а лишь добавляют новые роли.
        """
        signature = []
        for role in sorted(vision_results or {}):
            detections = vision_results.get(role) or []
            if isinstance(detections, list):
                signature.append((
                    role,
                    len(detections),
                    tuple(
                        (
                            item.get("class"),
                            item.get("confidence"),
                            tuple(item.get("bbox") or ()),
                        )
                        if isinstance(item, dict) else id(item)
                        for item in detections
                    ),
                ))
            else:
                signature.append((role, -1, id(detections)))
        return tuple(signature)

    @staticmethod
    def _rules_equal(left, right) -> bool:
        """Глубокое сравнение опубликованных данных, безопасное для numpy.

        Используется и для результатов правил, и для детекций моделей.
        ``details``/``drawings`` правил могут нести numpy-массивы и
        numpy-скаляры: обычное ``!=`` на них бросает ValueError («truth
        value of an array is ambiguous») и ломает сравнение. Обход:
        - тот же объект — без изменений;
        - списки/словари/датаклассы — рекурсивно;
        - остальное — ``bool(left == right)`` с фолбэком на
          ``np.array_equal``.
        """
        if left is right:
            return True
        if type(left) is not type(right):
            return False
        if isinstance(left, list):
            if len(left) != len(right):
                return False
            return all(
                UIServer._rules_equal(a, b)
                for a, b in zip(left, right)
            )
        if isinstance(left, dict):
            if left.keys() != right.keys():
                return False
            return all(
                UIServer._rules_equal(left[key], right[key])
                for key in left
            )
        fields = getattr(left, "__dataclass_fields__", None)
        if fields is not None:
            return all(
                UIServer._rules_equal(getattr(left, name), getattr(right, name))
                for name in fields
            )
        try:
            return bool(left == right)
        except Exception:
            import numpy as np
            try:
                return bool(np.array_equal(left, right))
            except Exception:
                return False

    def update(
        self,
        frames=None,
        vision_results=None,
        rule_results=None,
        line_status=None,
        recent_parts=None,
    ):
        """Атомарно опубликовать снимок: кадры + результаты + статус линии.

        Версия ``_cache_version`` (в UI — ``frame_version``) растёт только
        когда реально изменилось визуальное содержимое (кадры, разметка,
        правила). Повторные публикации тех же массивов (REVIEW/PUBLISH)
        кэш не трогают: иначе фронтенд лишний раз перезапрашивал бы кадры,
        а «появление обрисовки правил» разъезжалось бы с цветом корпуса
        на линии.
        """
        with self.lock:
            should_invalidate = False
            stream_overlay_changed = False
            changed_frame_roles = set()
            raw_overlay_changed = False
            rules_overlay_changed = False
            if frames is not None:
                for role, frame in frames.items():
                    if self.frames.get(role) is not frame:
                        self.frames[role] = frame
                        self._latest_frames_ver[role] = (
                            self._latest_frames_ver.get(role, 0) + 1
                        )
                        should_invalidate = True
                        changed_frame_roles.add(role)
            if vision_results is not None:
                # Сравнение по подписи, а не по identity: источник дополняет
                # один и тот же dict по стадиям шага, поэтому identity всегда
                # совпадала бы и RAW-разметка не доезжала до кэша JPEG.
                new_signature = self._vision_signature(vision_results)
                if new_signature != self._vision_signature_cache:
                    self.vision_results = self._snapshot_vision_results(
                        vision_results
                    )
                    self._vision_signature_cache = new_signature
                    should_invalidate = True
                    stream_overlay_changed = True
                    raw_overlay_changed = True
            if rule_results is not None:
                new_rules = list(rule_results)
                if not UIServer._rules_equal(self.rule_results, new_rules):
                    self.rule_results = new_rules
                    should_invalidate = True
                    stream_overlay_changed = True
                    rules_overlay_changed = True
            if line_status is not None:
                self.line_status = line_status
                self._apply_custom_threshold_labels(line_status)
            if recent_parts is not None:
                self.recent_parts = list(recent_parts)
            if should_invalidate:
                # Точечная инвалидация JPEG-кэша: обновление кадра одной
                # камеры (например, выбранной, 30 кадров/с) не должно
                # сбрасывать готовые превью остальных — иначе вторичные
                # камеры не успевают отрисоваться между публикациями.
                for role in changed_frame_roles:
                    for mode in ("RAW", "RULES"):
                        self._jpeg_cache.pop((role, mode, "preview"), None)
                        self._jpeg_cache.pop((role, mode, "main"), None)
                if raw_overlay_changed:
                    for role in self.frames:
                        self._jpeg_cache.pop((role, "RAW", "preview"), None)
                        self._jpeg_cache.pop((role, "RAW", "main"), None)
                if rules_overlay_changed:
                    for role in self.frames:
                        self._jpeg_cache.pop((role, "RULES", "preview"), None)
                        self._jpeg_cache.pop((role, "RULES", "main"), None)
                if stream_overlay_changed:
                    self._latest_stream_jpeg.clear()
                self._cache_version += 1

    def _apply_custom_threshold_labels(self, line_status: dict) -> None:
        """Подставить ручные названия порогов (_label.*) в анализ кадра.

        Каждое правило несёт ``measurement_cards`` — замеры текущего кадра
        с ключами метрик. Для метрик, у которых есть ручное название в
        thresholds.json (``_label.<параметр>``), заменяем встроенный перевод
        на пользовательский, как и в панели «Пороги правил».
        """
        custom_labels = dict(self.threshold_labels or {})
        if not custom_labels:
            return
        frame_analysis = (line_status or {}).get("frame_analysis")
        rules = (
            frame_analysis.get("rules")
            if isinstance(frame_analysis, dict) else None
        )
        if not isinstance(rules, list):
            return
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            group_id = RULE_THRESHOLD_GROUPS.get(rule.get("name"))
            if not group_id:
                continue
            for card in rule.get("measurement_cards") or []:
                if not isinstance(card, dict):
                    continue
                role = card.get("role")
                if not role:
                    continue
                for metric in card.get("metrics") or []:
                    if not isinstance(metric, dict):
                        continue
                    key = metric.get("key")
                    if not key:
                        continue
                    full_key = f"{group_id}_{key}"
                    custom = custom_labels.get(f"{role}.{full_key}")
                    if custom:
                        metric["label"] = custom

    def set_camera_roles(self, roles) -> None:
        """Опубликовать роли открытых камер без обязательного чтения кадров.

        ``roles`` — словарь ``{роль: Camera ID}`` из camera_mapping.json.
        Сохраняется и сам маппинг, чтобы UI мог показать, какой
        физический Camera ID соответствует каждой роли.
        """
        mapping = {}
        for role, camera_id in (roles or {}).items():
            if role:
                mapping[str(role)] = int(camera_id)
        normalized = [str(role) for role in dict.fromkeys(roles or ()) if role]
        with self.lock:
            self.camera_mapping = mapping
            self.camera_roles = normalized
            if self.active_camera_role not in normalized:
                self.active_camera_role = normalized[0] if normalized else None

    def set_active_camera_role(self, role: str) -> bool:
        with self.lock:
            available = set(self.camera_roles) | set(self.frames)
            if not role or role not in available:
                return False
            if self.active_camera_role == role:
                return True
            self.active_camera_role = role
        cb = self.on_active_camera_changed
        if cb is not None:
            try:
                cb(role)
            except Exception as exc:
                print(f"[UI] on_active_camera_changed error: {exc}")
        return True

    # ─── Пороги правил ─────────────────────────────────────────

    def thresholds_editable(self) -> bool:
        """Пороги меняются только до пуска и после полной остановки."""
        if self.splash_active:
            return False
        line_state = (self.line_status or {}).get("state")
        return line_state in ("IDLE", "STOPPED")

    def thresholds_file_mtime_changed(self) -> bool:
        """Дёшево проверить (syscall getmtime), изменился ли thresholds.json.

        Вызывается на каждом тике /api/status; тяжёлая работа выполняется
        только при реальном изменении файла.
        """
        if not self.thresholds_path:
            return False
        try:
            mtime = os.path.getmtime(self.thresholds_path)
        except OSError:
            return False
        with self.lock:
            known = self.thresholds_file_mtime
        return mtime != known

    def reload_thresholds_from_file(self) -> bool:
        """Перечитать thresholds.json, если файл изменился.

        Применяется только когда линия остановлена (IDLE/STOPPED): в этот
        момент правила можно безопасно пересоздать. При изменении содержимого
        вызывается on_thresholds_reload (пересоздание DecisionEngine),
        обновляется dict порогов и увеличивается revision, по которому
        фронтенд перерисовывает панель.
        """
        if not self.thresholds_path:
            return False
        try:
            mtime = os.path.getmtime(self.thresholds_path)
        except OSError:
            return False
        with self.lock:
            known = self.thresholds_file_mtime
        if mtime == known:
            return False
        if not self.thresholds_editable():
            # Линия работает: не запоминаем mtime, чтобы применить изменения
            # сразу после остановки (проверка по mtime дёшева).
            return False
        try:
            from domain.threshold_loader import ThresholdLoader
            loader = ThresholdLoader(self.thresholds_path)
            fresh = loader.get_all()
            fresh_labels = dict(loader.labels)
        except Exception as exc:
            # Битый файл: запоминаем время проверки, чтобы не пытаться
            # перечитывать и не спамить ошибкой на каждом тике статуса.
            # Повторная попытка будет после следующей правки файла.
            print(f"[THRESHOLDS] Ошибка перечитывания thresholds.json: {exc}")
            with self.lock:
                self.thresholds_file_mtime = mtime
            return False
        with self.lock:
            current = dict(self.thresholds or {})
            current_labels = dict(self.threshold_labels)
        if fresh == current and fresh_labels == current_labels:
            # Файл тронут, но содержимое не изменилось (например, после
            # сохранения через UI) — просто запоминаем время проверки.
            with self.lock:
                self.thresholds_file_mtime = mtime
            return False
        if self.on_thresholds_reload is not None:
            try:
                fresh = self.on_thresholds_reload(fresh) or fresh
            except Exception as exc:
                print(
                    f"[THRESHOLDS] Ошибка применения порогов из файла: {exc}"
                )
                with self.lock:
                    self.thresholds_file_mtime = mtime
                return False
        with self.lock:
            self.thresholds = dict(fresh)
            self.threshold_labels = dict(fresh_labels)
            self.thresholds_file_mtime = mtime
            self.thresholds_revision += 1
        print("[THRESHOLDS] thresholds.json перечитан автоматически")
        return True

    def build_thresholds_payload(self, role: str | None = None) -> dict:
        """Ответ для GET /api/thresholds: пороги роли, сгруппированные по
        правилам, с метаданными для редактора (подпись, шаг, границы).
        """
        from domain.threshold_loader import describe_role_parameters

        # Сначала синхронизация с файлом: thresholds.json мог быть
        # изменён вручную — панель покажет актуальные значения.
        self.reload_thresholds_from_file()

        with self.lock:
            thresholds = dict(self.thresholds or {})
            revision = self.thresholds_revision
            editable = self.thresholds_editable()

        if not thresholds:
            return {
                "role": role,
                "available": False,
                "editable": False,
                "rules": [],
                "values": {},
                "revision": revision,
            }

        if role:
            role_keys = [
                key for key in thresholds if key.startswith(f"{role}.")
            ]
            if not role_keys:
                return {
                    "role": role,
                    "available": False,
                    "editable": False,
                    "rules": [],
                    "values": {},
                    "labels": {},
                    "revision": revision,
                }
            rules = describe_role_parameters(role, thresholds)
            values = {
                param["key"]: param["value"]
                for group in rules
                for param in group["params"]
            }
            # Названия, переопределённые вручную в thresholds.json
            # (_label.<parameter>), заменяют встроенный перевод.
            with self.lock:
                labels = {
                    key.split(".", 1)[1]: name
                    for key, name in self.threshold_labels.items()
                    if key.startswith(f"{role}.")
                }
            for group in rules:
                for param in group["params"]:
                    custom = labels.get(param["key"])
                    if custom:
                        param["label"] = custom
            return {
                "role": role,
                "available": True,
                "editable": editable,
                "rules": rules,
                "values": values,
                "labels": labels,
                "revision": revision,
            }

        roles = sorted({
            key.split(".", 1)[0]
            for key in thresholds
            if "." in key
        })
        return {
            "role": None,
            "available": True,
            "editable": editable,
            "roles": roles,
            "rules": [],
            "values": {},
            "revision": revision,
        }

    def apply_thresholds(
        self, role: str, values: dict, labels: dict | None = None,
    ) -> dict:
        """Применить изменения порогов через внешний callback.

        Доступно только когда линия остановлена (IDLE/STOPPED). ``labels`` —
        понятные названия порогов роли (parameter -> строка); пустая строка
        удаляет название. Возвращает обновлённый payload GET /api/thresholds.
        """
        if not self.thresholds_editable():
            raise RuntimeError(
                "Изменение порогов доступно только до пуска "
                "и после полной остановки"
            )
        # Синхронизация с файлом перед применением: база порогов не должна
        # затирать внешние правки thresholds.json.
        self.reload_thresholds_from_file()
        if self.on_thresholds_apply is None:
            raise RuntimeError("Применение порогов ещё не подключено")
        updated = self.on_thresholds_apply(role, values, labels or {})
        if not isinstance(updated, dict):
            raise RuntimeError("Backend не вернул обновлённые пороги")
        with self.lock:
            self.thresholds = dict(updated)
            self.threshold_labels = dict(self.threshold_labels or {})
            for key, name in (labels or {}).items():
                full_key = (
                    f"{role}.{key}"
                    if not str(key).startswith(f"{role}.")
                    else str(key)
                )
                if name is None or not str(name).strip():
                    self.threshold_labels.pop(full_key, None)
                else:
                    self.threshold_labels[full_key] = str(name).strip()
            self.thresholds_revision += 1
        return self.build_thresholds_payload(role)

    # ─── Архив партий ──────────────────────────────────────────

    def archive_editable(self) -> bool:
        """Настройки архива меняются только до начала текущей партии."""
        if self.splash_active or self.archive is None:
            return False
        line_state = (self.line_status or {}).get("state")
        return line_state in ("IDLE", "STOPPED") and self.archive.can_reconfigure()

    def archive_status_payload(self) -> dict:
        archive = self.archive
        if archive is None:
            return {"available": False, "editable": False}
        settings = archive.get_settings(validate=False)
        settings["available"] = True
        settings["editable"] = self.archive_editable()
        return settings

    def build_archive_payload(self, validate: bool = True) -> dict:
        archive = self.archive
        if archive is None:
            return {"available": False, "editable": False}
        settings = archive.get_settings(validate=validate)
        settings["available"] = True
        settings["editable"] = self.archive_editable()
        return settings

    def archive_ready_for_start(self) -> tuple[bool, str | None]:
        archive = self.archive
        if archive is None:
            return False, "архив не инициализирован"
        try:
            archive.validate_root(archive.root_folder)
        except ValueError as exc:
            return False, str(exc)
        return True, None

    def apply_archive_settings(self, payload: dict) -> dict:
        if self.archive is None:
            raise RuntimeError("Архив ещё не инициализирован")
        if not self.archive_editable():
            raise RuntimeError(
                "Папку архива можно менять только до начала текущей партии"
            )

        from config.archive_config import normalise_archive_config, save_archive_config

        current = self.archive.get_settings(validate=False)
        incoming = dict(current)
        incoming.update(payload or {})
        config = normalise_archive_config(incoming)
        settings = self.archive.reconfigure(
            root_folder=config["root_path"],
        )
        save_archive_config(self.archive_config_path, config)
        settings["available"] = True
        settings["editable"] = self.archive_editable()
        return settings

    def boot_step_start(self, key, message=None):
        with self.lock:
            if key in self.boot_steps:
                self.boot_steps[key] = "running"
                self.boot_current = key
            if message:
                self.boot_message = message
                self._append_log(message)

    def boot_step_done(self, key, message=None):
        with self.lock:
            if key in self.boot_steps:
                self.boot_steps[key] = "done"
            if message:
                self._append_log(message)

    def boot_step_error(self, key, message):
        label = dict(BOOT_STEPS).get(key, key)
        print(f"[BOOT] Ошибка этапа '{label}': {message}")
        with self.lock:
            if key in self.boot_steps:
                self.boot_steps[key] = "error"
            self.boot_error = message
            self.boot_message = f"ОШИБКА: {message}"
            self._append_log(f"[ОШИБКА] {message}")

    def boot_complete(self):
        with self.lock:
            self.splash_active = False
            self.boot_message = "Готово"

    def set_splash_status(self, text):
        with self.lock:
            self.boot_message = text
            self._append_log(text)

    def _append_log(self, text):
        self.splash_log.append(text)
        if len(self.splash_log) > 30:
            self.splash_log = self.splash_log[-30:]

    @staticmethod
    def _configure_windows_event_loop_policy():
        """Избежать шумных WinError 10054 от Proactor при закрытии MJPEG."""
        if sys.platform != "win32":
            return False
        policy_class = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
        if policy_class is None:
            return False
        if not isinstance(asyncio.get_event_loop_policy(), policy_class):
            asyncio.set_event_loop_policy(policy_class())
        return True

    @staticmethod
    def _quiet_connection_reset_handler(loop, context):
        exception = context.get("exception")
        if isinstance(exception, ConnectionResetError):
            return
        loop.default_exception_handler(context)

    def _run_server_thread(self):
        if sys.platform != "win32":
            self._uvicorn_server.run()
            return

        # Явно создаём SelectorEventLoop: одной смены policy недостаточно для
        # некоторых сочетаний Python 3.11 + WebView2 + uvicorn.
        loop = asyncio.SelectorEventLoop()
        asyncio.set_event_loop(loop)
        loop.set_exception_handler(self._quiet_connection_reset_handler)
        try:
            loop.run_until_complete(self._uvicorn_server.serve())
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()

    def start_server(self, host="127.0.0.1", port=8000):
        self._configure_windows_event_loop_policy()
        config = uvicorn.Config(
            self.app, host=host, port=port,
            log_level="warning", access_log=False,
        )
        self._uvicorn_server = uvicorn.Server(config)
        self._server_thread = threading.Thread(
            target=self._run_server_thread, daemon=True,
        )
        self._server_thread.start()
        time.sleep(0.5)
        if not self._server_thread.is_alive():
            raise RuntimeError(f"UI server failed to start on {host}:{port}")
        print(f"[UI SERVER] http://{host}:{port}")

    def stop_server(self, timeout: float = 5.0):
        if self._uvicorn_server:
            self._uvicorn_server.should_exit = True
        thread = self._server_thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout)
            if thread.is_alive():
                raise RuntimeError("UI server thread did not stop")
        self._server_thread = None
        self._uvicorn_server = None

    def get_server_thread(self) -> threading.Thread | None:
        return self._server_thread

    # Stream helpers

    def get_frame_version(self, role: str) -> int:
        with self.lock:
            return self._latest_frames_ver.get(role, 0)

    def get_stream_jpeg(self, role: str, mode: str = "RAW") -> tuple:
        """Вернуть JPEG и версию кадра для RAW/RULES MJPEG-стрима."""
        actual_mode = mode if mode in ("RAW", "RULES") else self.mode
        cache_key = (role, actual_mode)
        with self.lock:
            frame = self.frames.get(role)
            if frame is None:
                return None, 0

            current_ver = self._latest_frames_ver.get(role, 0)
            cached = self._latest_stream_jpeg.get(cache_key)

            if cached is not None:
                cached_jpeg, cached_ver = cached
                if cached_ver == current_ver:
                    return cached_jpeg, cached_ver

            frame_copy = frame.copy()
            vision_dets = (
                list(self.vision_results.get(role, []))
                if actual_mode == "RAW" else None
            )
            rule_results = (
                list(self.rule_results)
                if actual_mode == "RULES" else None
            )

        rendered = self._render(
            frame_copy,
            role,
            actual_mode,
            vision_dets,
            rule_results,
        )
        jpeg = self._encode_jpeg(rendered, self.STREAM_JPEG_QUALITY)

        with self.lock:
            actual_ver = self._latest_frames_ver.get(role, 0)
            if actual_ver == current_ver:
                self._latest_stream_jpeg[cache_key] = (jpeg, current_ver)

        return jpeg, current_ver

    # Internal setup

    def _setup_static(self):
        if _STATIC_DIR.exists():
            self.app.mount(
                "/static",
                NoCacheStaticFiles(directory=str(_STATIC_DIR)),
                name="static",
            )

    def _setup_routes(self):
        @self.app.get("/")
        async def index():
            return FileResponse(
                str(_TEMPLATES_DIR / "index.html"),
                headers={"Cache-Control": NO_CACHE},
            )

        setup_frame_routes(self.app, self)
        setup_api_routes(self.app, self)
        setup_archive_routes(self.app, self)

    # Rendering & caching (pull)

    def _get_or_render(self, role, mode, size_kind):
        """Кадр камеры (JPEG) с текущей разметкой."""
        cache_key = (role, mode, size_kind)
        with self.lock:
            cached = self._jpeg_cache.get(cache_key)
            if cached is not None:
                return cached
            frame = self.frames.get(role)
            if frame is None:
                return None
            version_before = self._cache_version
            frame_copy = frame.copy()
            vision_dets = (
                list(self.vision_results.get(role, []))
                if mode == "RAW" else None
            )
            rule_results = (
                list(self.rule_results) if mode == "RULES" else None
            )

        rendered = self._render(
            frame_copy, role, mode, vision_dets, rule_results,
        )
        if size_kind == "preview":
            rendered = self._resize_for_preview(rendered)
        jpeg = self._encode_jpeg(rendered, self.JPEG_QUALITY)

        with self.lock:
            if self._cache_version == version_before:
                self._jpeg_cache[cache_key] = jpeg
        return jpeg

    def _render(
        self, frame, role, mode, vision_dets, rule_results,
    ):
        if mode == "RAW":
            if vision_dets:
                return RawOverlay.render(frame, vision_dets)
            return frame.copy()

        if rule_results:
            return DebugOverlay.render_frame(
                frame, role, rule_results,
            )
        return frame.copy()

    @staticmethod
    def _resize_for_preview(frame):
        h, w = frame.shape[:2]
        if w <= UIServer.PREVIEW_MAX_WIDTH:
            return frame
        scale = UIServer.PREVIEW_MAX_WIDTH / w
        return cv2.resize(
            frame,
            (UIServer.PREVIEW_MAX_WIDTH, int(h * scale)),
            interpolation=cv2.INTER_AREA,
        )

    @staticmethod
    def _encode_jpeg(frame, quality: int | None = None):
        q = quality if quality is not None else UIServer.JPEG_QUALITY
        ok, buf = cv2.imencode(
            ".jpg", frame,
            [cv2.IMWRITE_JPEG_QUALITY, q],
        )
        return buf.tobytes() if ok else b""

    # Helpers

    @staticmethod
    def _sort_by_order(roles: list) -> list:
        known = [r for r in CAMERA_ORDER if r in roles]
        unknown = sorted(
            r for r in roles if r not in CAMERA_ORDER
        )
        return known + unknown
