"""Отправка batch.json партий на ПК аналитика через локальную очередь.

Проверяются контракт json_sender (очередь, догрузка, атомарность),
финализация манифеста партии (CLOSED до отправки) и их интеграция:
аналитик не должен получать промежуточный OPEN-манифест.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import numpy as np

from inspection.part_archive import PartArchive
from json_sender import (
    TARGET_JSON_NAME,
    copy_json_to_local_queue,
    is_remote_available,
    scan_and_send_all_batches,
    send_batch_json,
    send_json_to_analytics,
    sync_local_queue,
)


def make_frame(width=64, height=48, value=128):
    return np.full((height, width, 3), value, dtype=np.uint8)


class JsonSenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.archive_root = Path(self.tmp.name) / "archive"
        self.queue_dir = Path(self.tmp.name) / "queue"
        self.remote_root = Path(self.tmp.name) / "remote"

    def tearDown(self):
        self.tmp.cleanup()

    def make_batch(self, name, *, root=None, payload=None):
        batch_dir = (root or self.archive_root) / "2026-09-29" / name
        batch_dir.mkdir(parents=True)
        data = payload or {"batch_id": name, "status": "CLOSED"}
        (batch_dir / TARGET_JSON_NAME).write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
        return batch_dir

    def queued(self, name, payload=None):
        path = self.queue_dir / name / TARGET_JSON_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload or {"batch_id": name}), encoding="utf-8"
        )
        return path

    @staticmethod
    def read_json(path):
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)

    def test_is_remote_available(self):
        self.remote_root.mkdir()
        self.assertTrue(is_remote_available(self.remote_root))
        self.assertFalse(is_remote_available(self.remote_root / "missing"))

    def test_send_batch_json_delivers_to_queue_and_remote(self):
        batch_dir = self.make_batch("batch_one")
        self.remote_root.mkdir()

        delivered = send_batch_json(
            batch_dir,
            queue_dir=self.queue_dir,
            remote_root=self.remote_root,
        )

        self.assertTrue(delivered)
        local = self.queue_dir / "batch_one" / TARGET_JSON_NAME
        remote = self.remote_root / "batch_one" / TARGET_JSON_NAME
        self.assertTrue(local.exists())
        self.assertEqual(
            self.read_json(remote), self.read_json(local),
        )
        self.assertEqual(self.read_json(remote)["batch_id"], "batch_one")

    def test_send_batch_json_queues_when_remote_offline(self):
        batch_dir = self.make_batch("batch_offline")

        delivered = send_batch_json(
            batch_dir,
            queue_dir=self.queue_dir,
            remote_root=self.remote_root,
        )

        self.assertFalse(delivered)
        self.assertTrue(
            (self.queue_dir / "batch_offline" / TARGET_JSON_NAME).exists()
        )

    def test_send_batch_json_without_manifest(self):
        batch_dir = self.make_batch("batch_empty")
        (batch_dir / TARGET_JSON_NAME).unlink()

        delivered = send_batch_json(
            batch_dir,
            queue_dir=self.queue_dir,
            remote_root=self.remote_root,
        )

        self.assertFalse(delivered)
        self.assertFalse(self.queue_dir.exists())

    def test_send_batch_json_missing_batch_dir(self):
        delivered = send_batch_json(
            self.archive_root / "batch_missing",
            queue_dir=self.queue_dir,
            remote_root=self.remote_root,
        )
        self.assertFalse(delivered)

    def test_sync_local_queue_delivers_pending_batches(self):
        self.queued("batch_a")
        self.queued("batch_b")

        offline = sync_local_queue(
            queue_dir=self.queue_dir,
            remote_root=self.remote_root,
        )
        self.assertEqual(offline, 0)

        self.remote_root.mkdir()
        delivered = sync_local_queue(
            queue_dir=self.queue_dir,
            remote_root=self.remote_root,
        )
        self.assertEqual(delivered, 2)
        for name in ("batch_a", "batch_b"):
            remote = self.remote_root / name / TARGET_JSON_NAME
            self.assertEqual(self.read_json(remote)["batch_id"], name)

    def test_sync_local_queue_without_queue_dir(self):
        delivered = sync_local_queue(
            queue_dir=self.queue_dir,
            remote_root=self.remote_root,
        )
        self.assertEqual(delivered, 0)
        # Очередь не создаётся: нечего догружать.
        self.assertFalse(self.queue_dir.exists())

    def test_sync_local_queue_is_idempotent(self):
        self.queued("batch_a", {"batch_id": "batch_a", "status": "CLOSED"})
        self.remote_root.mkdir()

        first = sync_local_queue(
            queue_dir=self.queue_dir, remote_root=self.remote_root,
        )
        second = sync_local_queue(
            queue_dir=self.queue_dir, remote_root=self.remote_root,
        )

        self.assertEqual((first, second), (1, 1))
        remote = self.remote_root / "batch_a" / TARGET_JSON_NAME
        self.assertEqual(self.read_json(remote)["status"], "CLOSED")
        self.assertEqual(list(self.remote_root.rglob("*.tmp")), [])

    def test_send_json_to_analytics_overwrites_stale_remote_file(self):
        queued_path = self.queued("batch_a", {"batch_id": "batch_a", "v": 2})
        self.remote_root.mkdir()
        remote = self.remote_root / "batch_a" / TARGET_JSON_NAME
        remote.parent.mkdir()
        remote.write_text(
            '{"batch_id": "batch_a", "v": 1}', encoding="utf-8"
        )

        delivered = send_json_to_analytics(queued_path, self.remote_root)

        self.assertTrue(delivered)
        self.assertEqual(self.read_json(remote)["v"], 2)
        # Атомарная замена не оставляет временных файлов.
        self.assertEqual(list(self.remote_root.rglob("*.tmp")), [])

    def test_copy_json_to_local_queue_returns_none_on_error(self):
        batch_dir = self.make_batch("batch_a")

        result = copy_json_to_local_queue(
            batch_dir, batch_dir / "missing.json", self.queue_dir,
        )

        self.assertIsNone(result)
        # Ни манифеста, ни временных файлов в очереди не появилось.
        self.assertEqual(list(self.queue_dir.rglob(TARGET_JSON_NAME)), [])
        self.assertEqual(list(self.queue_dir.rglob("*.tmp")), [])

    def test_scan_and_send_all_batches(self):
        self.make_batch("batch_001")
        no_manifest = self.make_batch("batch_002")
        (no_manifest / TARGET_JSON_NAME).unlink()
        # ZIP прошлой партии -- файл, а не папка: должен игнорироваться.
        (self.archive_root / "2026-09-29" / "batch_000.zip").write_bytes(b"")
        self.queued("batch_old")
        self.remote_root.mkdir()

        delivered = scan_and_send_all_batches(
            self.archive_root,
            queue_dir=self.queue_dir,
            remote_root=self.remote_root,
        )

        self.assertEqual(delivered, 2)
        for name in ("batch_001", "batch_old"):
            remote = self.remote_root / name / TARGET_JSON_NAME
            self.assertEqual(self.read_json(remote)["batch_id"], name)
        self.assertFalse((self.remote_root / "batch_002").exists())
        self.assertTrue(
            (self.queue_dir / "batch_001" / TARGET_JSON_NAME).exists()
        )

    def test_scan_and_send_all_batches_without_remote(self):
        self.make_batch("batch_001")

        delivered = scan_and_send_all_batches(
            self.archive_root,
            queue_dir=self.queue_dir,
            remote_root=self.remote_root,
        )

        self.assertEqual(delivered, 0)
        self.assertTrue(
            (self.queue_dir / "batch_001" / TARGET_JSON_NAME).exists()
        )
        self.assertFalse(self.remote_root.exists())

    def test_scan_and_send_all_batches_missing_root_syncs_queue(self):
        self.queued("batch_pending")
        self.remote_root.mkdir()

        delivered = scan_and_send_all_batches(
            self.archive_root / "missing",
            queue_dir=self.queue_dir,
            remote_root=self.remote_root,
        )

        self.assertEqual(delivered, 1)
        self.assertTrue(
            (self.remote_root / "batch_pending" / TARGET_JSON_NAME).exists()
        )


class PartArchiveCloseManifestTest(unittest.TestCase):
    """Финализация манифеста партии перед отправкой аналитику."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "archive")

    def tearDown(self):
        self.tmp.cleanup()

    def make_archive(self):
        return PartArchive(root_folder=self.root, batch_id="batch_test")

    def store_part(self, archive, part_id=1, category="GOOD"):
        archive.store_frames(
            part_id,
            raw_frames={"TOP": make_frame()},
            annotated_frames={"TOP": make_frame(1)},
            raw_overlay_frames={"TOP": make_frame(2)},
        )
        return archive.finalize(
            part_id, category, decision="none", defects=[], step=3,
        )

    @staticmethod
    def read_manifest(archive):
        path = os.path.join(archive.batch_folder, "batch.json")
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)

    def test_close_manifest_marks_batch_closed(self):
        archive = self.make_archive()
        self.store_part(archive)
        self.assertEqual(self.read_manifest(archive)["status"], "OPEN")

        archive.close_manifest()

        manifest = self.read_manifest(archive)
        self.assertEqual(manifest["status"], "CLOSED")
        self.assertEqual(len(manifest["parts"]), 1)

    def test_close_manifest_skips_empty_batch(self):
        archive = self.make_archive()

        archive.close_manifest()

        self.assertFalse(
            os.path.exists(os.path.join(archive.batch_folder, "batch.json"))
        )
        # Пустая партия по-прежнему не архивируется.
        self.assertIsNone(archive.compress())

    def test_close_manifest_then_scan_delivers_closed_status(self):
        archive = self.make_archive()
        self.store_part(archive)
        self.store_part(archive, part_id=2, category="BAD")
        archive.close_manifest()

        queue_dir = Path(self.tmp.name) / "queue"
        remote_root = Path(self.tmp.name) / "remote"
        remote_root.mkdir()

        delivered = scan_and_send_all_batches(
            archive.root_folder,
            queue_dir=queue_dir,
            remote_root=remote_root,
        )

        self.assertEqual(delivered, 1)
        remote = remote_root / "batch_test" / TARGET_JSON_NAME
        with open(remote, encoding="utf-8") as stream:
            manifest = json.load(stream)
        self.assertEqual(manifest["status"], "CLOSED")
        self.assertEqual(
            manifest["counts"],
            {"total": 2, "good": 1, "bad": 1, "cleanup": 0},
        )


if __name__ == "__main__":
    unittest.main()
