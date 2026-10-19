"""Отправка манифестов партий (batch.json) на ПК аналитика.

Схема доставки::

    архив партии ──> локальная очередь ──> сетевой диск Z:
                     Protocols\\json         <партия>\\batch.json

1. При завершении программы (или вручную) ``batch.json`` каждой партии
   копируется в локальную очередь -- даже если ПК аналитика выключен.
2. Если сетевой диск доступен, файлы уезжают на него в папку партии.
3. При каждом старте программы очередь догружается
   (:func:`sync_local_queue`), поэтому партии не теряются, сколько бы
   времени ПК аналитика ни был недоступен.

Все копирования атомарны (временный файл + ``os.replace``): обрыв связи
не оставляет на ПК аналитика битый ``batch.json``.

Функции принимают пути аргументами; константы ниже -- production-значения
по умолчанию. В shutdown корень архива передаётся явно из
``archive_config.json`` (``PartArchive.root_folder``), а не дублируется
жёстко заданным путём.
"""

from __future__ import annotations

import contextlib
import os
import shutil
from pathlib import Path


# Корень архива партий по умолчанию. Используется только при ручном
# запуске модуля: shutdown передаёт фактический корень из конфигурации.
BATCHES_ROOT = Path(r"C:\Users\user\Desktop\conveyor_three\archive")

# Имя JSON-манифеста партии, который нужно отправлять.
TARGET_JSON_NAME = "batch.json"

# Локальная папка-очередь: переживает выключенный ПК аналитика.
LOCAL_QUEUE_DIR = Path(r"C:\Users\User\Desktop\Protocols\json")

# Сетевой диск ПК аналитика.
REMOTE_ROOT = Path("Z:/")


def is_remote_available(remote_root: Path | str = REMOTE_ROOT) -> bool:
    """Проверяет, доступен ли сетевой диск Z:."""
    try:
        root = Path(remote_root)
        return root.exists() and root.is_dir()
    except OSError:
        return False


def copy_json_to_local_queue(
    batch_dir,
    json_path,
    queue_dir: Path | str = LOCAL_QUEUE_DIR,
) -> Path | None:
    """Копирует ``batch.json`` партии в локальную очередь.

    В очереди создаётся папка с именем партии; копия пишется атомарно
    и с перезаписью, поэтому очередь всегда держит свежую версию
    манифеста. Возвращает путь к копии или ``None`` при ошибке.
    """
    batch_name = Path(batch_dir).name
    try:
        local_json_path = Path(queue_dir) / batch_name / TARGET_JSON_NAME
        _atomic_copy(Path(json_path), local_json_path)
        print(f"[JSON] Сохранено в локальную очередь: {local_json_path}")
        return local_json_path
    except Exception as exc:
        print(f"[JSON] Ошибка копирования в локальную очередь: {exc}")
        return None


def send_json_to_analytics(
    local_json_path,
    remote_root: Path | str = REMOTE_ROOT,
) -> bool:
    """Отправляет ``batch.json`` из очереди на ПК аналитика.

    На ПК аналитика создаётся папка партии. Возвращает ``True`` только
    при успешной доставке; при недоступном диске файл остаётся в очереди
    и уйдёт при следующем запуске программы.
    """
    local_json_path = Path(local_json_path)
    if not is_remote_available(remote_root):
        print("[JSON] ПК аналитики недоступен; файл останется в очереди")
        return False
    try:
        remote_json_path = (
            Path(remote_root) / local_json_path.parent.name / TARGET_JSON_NAME
        )
        _atomic_copy(local_json_path, remote_json_path)
        print(f"[JSON] Отправлено на ПК аналитика: {remote_json_path}")
        return True
    except Exception as exc:
        print(f"[JSON] Ошибка отправки на ПК аналитика: {exc}")
        return False


def send_batch_json(
    batch_dir,
    *,
    queue_dir: Path | str = LOCAL_QUEUE_DIR,
    remote_root: Path | str = REMOTE_ROOT,
) -> bool:
    """Отправить ``batch.json`` одной партии: копия в очередь + доставка.

    Точка вызова после окончания проверки партии. Возвращает ``True``
    только если файл добрался до ПК аналитика; при сбое копия остаётся
    в локальной очереди и уйдёт при следующем старте программы.
    """
    batch_dir = Path(batch_dir)
    if not batch_dir.is_dir():
        print(f"[JSON] Папка партии не найдена: {batch_dir}")
        return False
    json_path = batch_dir / TARGET_JSON_NAME
    if not json_path.exists():
        print(f"[JSON] В партии нет {TARGET_JSON_NAME}: {batch_dir}")
        return False
    local_json_path = copy_json_to_local_queue(
        batch_dir, json_path, queue_dir
    )
    if local_json_path is None:
        return False
    return send_json_to_analytics(local_json_path, remote_root)


def sync_local_queue(
    *,
    queue_dir: Path | str = LOCAL_QUEUE_DIR,
    remote_root: Path | str = REMOTE_ROOT,
) -> int:
    """Догружает все ``batch.json`` из локальной очереди на ПК аналитика.

    Возвращает число доставленных файлов. Очередь не создаётся: раз её
    нет -- догружать нечего.
    """
    queue_dir = Path(queue_dir)
    if not queue_dir.is_dir():
        return 0
    json_files = sorted(queue_dir.rglob(TARGET_JSON_NAME))
    if not json_files:
        return 0
    if not is_remote_available(remote_root):
        print("[JSON] ПК аналитики недоступен; догрузка очереди отложена")
        return 0
    print(f"[JSON] Догрузка очереди: файлов {len(json_files)}")
    delivered = 0
    for json_file in json_files:
        if send_json_to_analytics(json_file, remote_root):
            delivered += 1
    return delivered


def scan_and_send_all_batches(
    batches_root=None,
    *,
    queue_dir: Path | str = LOCAL_QUEUE_DIR,
    remote_root: Path | str = REMOTE_ROOT,
) -> int:
    """Поставить в очередь и отправить ``batch.json`` всех партий архива.

    Рекурсивно ищет папки ``batch_*`` (включая подпапки по датам; ZIP
    прошлых партий игнорируются), ставит их манифесты в локальную очередь,
    затем догружает очередь целиком -- в том числе то, что осталось с
    прошлых запусков. Может запускаться вручную. Возвращает число
    доставленных файлов: недоступность ПК аналитика не ошибка, копии
    остаются в очереди и уйдут при следующем старте программы.
    """
    root = Path(batches_root) if batches_root is not None else BATCHES_ROOT
    if not root.is_dir():
        print(f"[JSON] Папка архива не найдена: {root}")
        return sync_local_queue(queue_dir=queue_dir, remote_root=remote_root)
    batch_dirs = sorted(
        path for path in root.rglob("batch_*") if path.is_dir()
    )
    for batch_dir in batch_dirs:
        json_path = batch_dir / TARGET_JSON_NAME
        if json_path.exists():
            copy_json_to_local_queue(batch_dir, json_path, queue_dir)
        else:
            print(f"[JSON] В партии нет {TARGET_JSON_NAME}: {batch_dir}")
    return sync_local_queue(queue_dir=queue_dir, remote_root=remote_root)


def _atomic_copy(src: Path, dst: Path) -> None:
    """Копировать файл с перезаписью через временный файл.

    ``os.replace`` делает замену атомарной: читатель никогда не видит
    наполовину записанный файл, а при обрыве связи прежняя версия
    остаётся на месте.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    except Exception:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise
