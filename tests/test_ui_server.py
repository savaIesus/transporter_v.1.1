"""UI-сервер: публикация кадров/статуса, пороги, архив, HTTP-маршруты.

``UIServer`` тестируется напрямую (методы) и через FastAPI TestClient
(маршруты /api/*, /frame/*, /stream/*). Сервер не поднимает uvicorn —
только приложение FastAPI.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from unittest import mock

import numpy as np
from fastapi.testclient import TestClient

from domain.threshold_loader import ThresholdLoader
from vision.ui.server.server import UIServer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
THRESHOLDS_PATH = os.path.join(REPO_ROOT, "thresholds.json")


def make_frame(value=100):
    return np.full((64, 96, 3), value, dtype=np.uint8)


class UIServerTest(unittest.TestCase):
    def setUp(self):
        self.server = UIServer()
        self.client = TestClient(self.server.app)

    # ---------- update / кадры ----------

    def test_update_publishes_frames_and_increments_version(self):
        version_before = self.server._cache_version
        frame = make_frame()
        self.server.update(frames={"TOP": frame})
        self.assertIs(self.server.frames["TOP"], frame)
        self.assertEqual(self.server._cache_version, version_before + 1)
        self.assertEqual(self.server.get_frame_version("TOP"), 1)

    def test_update_same_frame_object_no_invalidate(self):
        frame = make_frame()
        self.server.update(frames={"TOP": frame})
        version = self.server._cache_version
        self.server.update(frames={"TOP": frame})
        self.assertEqual(self.server._cache_version, version)

    def test_mutated_vision_results_invalidate_cache(self):
        # Регрессия: ProductionCycle дополняет один и тот же dict
        # (_last_vision_results.update(...)) по стадиям шага. Сравнение по
        # identity считало такую публикацию «без изменений», и RAW-разметка
        # не попадала в кэш JPEG — в UI отображался кадр без детекций.
        frame = make_frame()
        vision = {}
        self.server.update(frames={"TOP": frame}, vision_results=vision)
        before = self.server._get_or_render("TOP", "RAW", "main")
        version = self.server._cache_version

        vision["TOP"] = [{
            "class": "glass", "confidence": 0.9,
            "bbox": [1.0, 1.0, 50.0, 50.0], "mask": None,
        }]
        self.server.update(frames={"TOP": frame}, vision_results=vision)

        self.assertGreater(self.server._cache_version, version)
        self.assertNotEqual(
            before, self.server._get_or_render("TOP", "RAW", "main"),
        )

    def test_unchanged_vision_results_do_not_invalidate(self):
        # Обратная сторона: повторная публикация того же содержимого не
        # должна дёргать кэш, иначе фронтенд перезапрашивает кадры зря.
        vision = {"TOP": [{"class": "glass", "confidence": 0.5}]}
        self.server.update(frames={"TOP": make_frame()}, vision_results=vision)
        version = self.server._cache_version
        self.server.update(vision_results=dict(vision))
        self.assertEqual(self.server._cache_version, version)

    def test_vision_comparison_is_cheap_under_lock(self):
        # Сравнение выполняется под общим self.lock, который держат и
        # /api/status, и рендер кадров. Полный рекурсивный обход масок
        # (тысячи точек) занимал десятки миллисекунд и подтормаживал UI.
        detections = {
            role: [{
                "class": "platform", "confidence": 0.83,
                "bbox": [1.0, 2.0, 3.0, 4.0],
                "mask": [[float(i), float(i)] for i in range(400)],
            } for _ in range(12)]
            for role in ("INPUT_LEFT", "SPIDER_LEFT", "TOP")
        }
        started = time.monotonic()
        for _ in range(10):
            UIServer._vision_signature(detections)
        elapsed = (time.monotonic() - started) / 10

        self.assertLess(elapsed, 0.005)

    def test_vision_results_snapshot_is_isolated(self):
        # Опубликованное состояние не должно меняться «задним числом»
        # вместе с исходным dict вызывающей стороны.
        vision = {"TOP": [{"class": "glass", "confidence": 0.5}]}
        self.server.update(vision_results=vision)
        vision["TOP"].append({"class": "pin", "confidence": 0.7})
        self.assertEqual(len(self.server.vision_results["TOP"]), 1)

    def test_update_rule_results_invalidate(self):
        from domain.defect_rules import RuleResult
        self.server.update(rule_results=[RuleResult("a", False)])
        version = self.server._cache_version
        self.server.update(rule_results=[RuleResult("a", False)])
        self.assertEqual(self.server._cache_version, version)
        self.server.update(rule_results=[RuleResult("a", True)])
        self.assertGreater(self.server._cache_version, version)

    def test_update_line_status_and_recent_parts(self):
        self.server.update(line_status={"state": "IDLE"},
                           recent_parts=[1, 2])
        self.assertEqual(self.server.line_status["state"], "IDLE")
        self.assertEqual(self.server.recent_parts, [1, 2])

    def test_frame_update_invalidates_only_that_role_cache(self):
        # Обновление кадра одной камеры не должно сбрасывать готовые
        # превью остальных (иначе вторичные камеры не успевают грузиться).
        self.server.update(frames={
            "TOP": make_frame(10), "INPUT_LEFT": make_frame(20),
        })
        for role in ("TOP", "INPUT_LEFT"):
            response = self.client.get(f"/frame/{role}?preview=1")
            self.assertEqual(response.status_code, 200)
        mode = self.server.mode
        self.assertIn(("TOP", mode, "preview"), self.server._jpeg_cache)
        self.assertIn(("INPUT_LEFT", mode, "preview"), self.server._jpeg_cache)
        # Меняется только кадр TOP.
        self.server.update(frames={"TOP": make_frame(99)})
        self.assertNotIn(("TOP", mode, "preview"), self.server._jpeg_cache)
        self.assertIn(("INPUT_LEFT", mode, "preview"), self.server._jpeg_cache)

    def test_rules_equal_with_numpy(self):
        self.assertTrue(UIServer._rules_equal(
            [{"mask": np.zeros((2, 2))}], [{"mask": np.zeros((2, 2))}],
        ))
        self.assertFalse(UIServer._rules_equal(
            [{"mask": np.zeros((2, 2))}], [{"mask": np.ones((2, 2))}],
        ))

    # ---------- камеры ----------

    def test_set_camera_roles(self):
        self.server.set_camera_roles({
            "TOP": 2, "INPUT_LEFT": 0,
        })
        self.assertEqual(self.server.camera_roles, ["TOP", "INPUT_LEFT"])
        self.assertEqual(self.server.camera_mapping, {"TOP": 2, "INPUT_LEFT": 0})
        self.assertEqual(self.server.active_camera_role, "TOP")

    def test_set_active_camera_role(self):
        self.server.set_camera_roles({"TOP": 2, "INPUT_LEFT": 0})
        self.assertTrue(self.server.set_active_camera_role("INPUT_LEFT"))
        self.assertEqual(self.server.active_camera_role, "INPUT_LEFT")
        self.assertFalse(self.server.set_active_camera_role("NOPE"))

    def test_set_active_camera_callback(self):
        self.server.set_camera_roles({"TOP": 2, "INPUT_LEFT": 0})
        calls = []
        self.server.on_active_camera_changed = lambda role: calls.append(role)
        self.server.set_active_camera_role("INPUT_LEFT")
        self.assertEqual(calls, ["INPUT_LEFT"])

    # ---------- пороги ----------

    def test_thresholds_editable(self):
        self.server.splash_active = False
        self.server.line_status = {"state": "IDLE"}
        self.assertTrue(self.server.thresholds_editable())
        self.server.line_status = {"state": "RUNNING"}
        self.assertFalse(self.server.thresholds_editable())
        self.server.splash_active = True
        self.assertFalse(self.server.thresholds_editable())

    def test_thresholds_payload_without_data(self):
        payload = self.server.build_thresholds_payload()
        self.assertFalse(payload["available"])

    def test_thresholds_payload_with_data(self):
        thresholds = ThresholdLoader(THRESHOLDS_PATH).get_all()
        self.server.thresholds = thresholds
        payload = self.server.build_thresholds_payload("LEFT")
        self.assertTrue(payload["available"])
        self.assertIn("rules", payload)
        self.assertIn("uneven_heights", {
            group["rule"] for group in payload["rules"]
        })

    def test_thresholds_payload_all_roles(self):
        thresholds = ThresholdLoader(THRESHOLDS_PATH).get_all()
        self.server.thresholds = thresholds
        payload = self.server.build_thresholds_payload()
        self.assertIn("LEFT", payload["roles"])

    def test_apply_thresholds_requires_callback(self):
        self.server.splash_active = False
        self.server.line_status = {"state": "IDLE"}
        with self.assertRaisesRegex(RuntimeError, "не подключено"):
            self.server.apply_thresholds("TOP", {})

    def test_apply_thresholds_calls_callback(self):
        self.server.splash_active = False
        self.server.line_status = {"state": "IDLE"}
        self.server.thresholds = {
            "LEFT.uneven_heights_min_confidence": 0.7,
        }
        self.server.on_thresholds_apply = mock.Mock(return_value={
            "LEFT.uneven_heights_min_confidence": 0.75,
        })
        result = self.server.apply_thresholds(
            "LEFT", {"uneven_heights_min_confidence": 0.75},
        )
        self.assertTrue(result["available"])
        self.assertEqual(
            self.server.thresholds["LEFT.uneven_heights_min_confidence"], 0.75,
        )
        self.assertEqual(self.server.thresholds_revision, 1)
        self.server.on_thresholds_apply.assert_called_once_with(
            "LEFT", {"uneven_heights_min_confidence": 0.75}, {},
        )

    def test_reload_thresholds_from_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "thresholds.json")
            ThresholdLoader.save_file(
                path, ThresholdLoader(THRESHOLDS_PATH).get_all(),
            )
            self.server.thresholds_path = path
            self.server.thresholds_file_mtime = None
            self.server.splash_active = False
            self.server.line_status = {"state": "IDLE"}
            self.assertTrue(self.server.reload_thresholds_from_file())
            self.assertEqual(self.server.thresholds_revision, 1)
            # Повторный вызов без изменения файла — False.
            self.assertFalse(self.server.reload_thresholds_from_file())

    def test_reload_thresholds_not_editable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "thresholds.json")
            ThresholdLoader.save_file(
                path, ThresholdLoader(THRESHOLDS_PATH).get_all(),
            )
            self.server.thresholds_path = path
            self.server.splash_active = False
            self.server.line_status = {"state": "RUNNING"}
            self.assertFalse(self.server.reload_thresholds_from_file())

    def test_reload_thresholds_missing_path(self):
        self.assertFalse(self.server.reload_thresholds_from_file())

    def test_reload_thresholds_broken_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "thresholds.json")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("{broken")
            self.server.thresholds_path = path
            self.assertFalse(self.server.reload_thresholds_from_file())

    def test_thresholds_file_mtime_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "thresholds.json")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("{}")
            self.server.thresholds_path = path
            self.assertTrue(self.server.thresholds_file_mtime_changed())
            self.server.thresholds_file_mtime = os.path.getmtime(path)
            self.assertFalse(self.server.thresholds_file_mtime_changed())

    # ---------- boot ----------

    def test_boot_steps(self):
        self.server.boot_step_start("cameras", "Камеры")
        self.assertEqual(self.server.boot_steps["cameras"], "running")
        self.server.boot_step_done("cameras")
        self.assertEqual(self.server.boot_steps["cameras"], "done")
        self.server.boot_step_error("serial", "boom")
        self.assertEqual(self.server.boot_steps["serial"], "error")
        self.assertEqual(self.server.boot_error, "boom")
        self.server.boot_complete()
        self.assertFalse(self.server.splash_active)

    def test_splash_log_capped(self):
        for i in range(40):
            self.server._append_log(f"line {i}")
        self.assertEqual(len(self.server.splash_log), 30)

    def test_set_splash_status(self):
        self.server.set_splash_status("текст")
        self.assertEqual(self.server.boot_message, "текст")

    # ---------- архив ----------

    def test_archive_payload_without_archive(self):
        payload = self.server.build_archive_payload()
        self.assertFalse(payload.get("available", False))

    def test_archive_ready_for_start(self):
        self.server.archive = mock.Mock(root_folder="/archive")
        ok, message = self.server.archive_ready_for_start()
        self.assertTrue(ok)
        self.assertIsNone(message)
        self.server.archive.validate_root.assert_called_once_with("/archive")

    def test_archive_is_required_for_start(self):
        ok, message = self.server.archive_ready_for_start()
        self.assertFalse(ok)
        self.assertIn("не инициализирован", message)

    def test_apply_archive_settings_requires_archive(self):
        with self.assertRaisesRegex(RuntimeError, "не инициализирован"):
            self.server.apply_archive_settings({})

    def test_apply_archive_settings(self):
        archive = mock.Mock()
        archive.can_reconfigure.return_value = True
        archive.reconfigure.return_value = {"root_path": "/tmp/new"}
        archive.get_settings.return_value = {"root_path": "/tmp/x"}
        self.server.archive = archive
        self.server.splash_active = False
        self.server.line_status = {"state": "IDLE"}
        with mock.patch(
            "config.archive_config.save_archive_config",
        ) as save:
            result = self.server.apply_archive_settings({
                "root_path": "/tmp/new",
                "enabled": False,
                "jpeg_quality": 70,
            })
        self.assertEqual(result["root_path"], "/tmp/new")
        archive.reconfigure.assert_called_once_with(root_folder="/tmp/new")
        save.assert_called_once()

    # ---------- stream / frame ----------

    def test_get_stream_jpeg_no_frame(self):
        jpeg, version = self.server.get_stream_jpeg("TOP")
        self.assertIsNone(jpeg)
        self.assertEqual(version, 0)

    def test_get_stream_jpeg_with_frame(self):
        self.server.update(frames={"TOP": make_frame()})
        jpeg, version = self.server.get_stream_jpeg("TOP", "RAW")
        self.assertIsNotNone(jpeg)
        self.assertGreater(version, 0)
        self.assertEqual(jpeg[:2], b"\xff\xd8")  # JPEG magic

    def test_get_stream_jpeg_rules_mode(self):
        self.server.update(frames={"TOP": make_frame()})
        jpeg, _ = self.server.get_stream_jpeg("TOP", "RULES")
        self.assertIsNotNone(jpeg)

    def test_render_and_encode(self):
        rendered = self.server._render(
            make_frame(), "TOP", "RAW", [], [],
        )
        self.assertEqual(rendered.shape, (64, 96, 3))
        jpeg = self.server._encode_jpeg(make_frame())
        self.assertEqual(jpeg[:2], b"\xff\xd8")

    def test_resize_for_preview(self):
        resized = UIServer._resize_for_preview(np.zeros((720, 1280, 3), dtype=np.uint8))
        self.assertEqual(resized.shape[1], UIServer.PREVIEW_MAX_WIDTH)

    def test_sort_by_order(self):
        self.assertEqual(
            UIServer._sort_by_order(["RIGHT", "LEFT", "X"]),
            ["LEFT", "RIGHT", "X"],
        )

    # ---------- HTTP ----------

    def test_api_cameras(self):
        self.server.set_camera_roles({"TOP": 2})
        response = self.client.get("/api/cameras")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["camera_ids"], {"TOP": 2})

    def test_api_boot(self):
        self.server.boot_step_start("cameras")
        response = self.client.get("/api/boot")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["steps"][0]["status"], "running")

    def test_api_status(self):
        self.server.update(frames={"TOP": make_frame()},
                           line_status={"state": "IDLE"})
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["line_status"]["state"], "IDLE")

    def test_api_mode(self):
        self.assertEqual(self.client.get("/api/mode").json()["mode"], "RULES")
        response = self.client.post("/api/mode/RAW")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.server.mode, "RAW")

    def test_api_active_camera(self):
        self.server.set_camera_roles({"TOP": 2})
        response = self.client.post("/api/active_camera/TOP")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.server.active_camera_role, "TOP")

    def test_api_thresholds_get(self):
        self.server.thresholds = ThresholdLoader(THRESHOLDS_PATH).get_all()
        response = self.client.get("/api/thresholds?role=LEFT")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["available"])

    def test_api_thresholds_post_without_callback(self):
        self.server.splash_active = False
        self.server.line_status = {"state": "IDLE"}
        response = self.client.post("/api/thresholds", json={
            "role": "LEFT", "values": {"uneven_heights_min_confidence": 0.75},
        })
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()["ok"])

    def test_api_thresholds_post_empty_values_400(self):
        response = self.client.post("/api/thresholds", json={
            "role": "TOP", "values": {},
        })
        self.assertEqual(response.status_code, 400)

    def test_api_start_503_without_callback(self):
        self.server.archive = mock.Mock(root_folder="/archive")
        response = self.client.post("/api/start")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()["ok"])

    def test_api_start_callback_false_409(self):
        self.server.archive = mock.Mock(root_folder="/archive")
        self.server.on_start = lambda: False
        response = self.client.post("/api/start")
        self.assertEqual(response.status_code, 409)

    def test_api_start_callback_exception_500(self):
        self.server.archive = mock.Mock(root_folder="/archive")

        def boom():
            raise RuntimeError("cycle broken")

        self.server.on_start = boom
        response = self.client.post("/api/start")
        self.assertEqual(response.status_code, 500)
        self.assertIn("cycle broken", response.json()["error"])

    def test_api_start_callback_ok(self):
        self.server.archive = mock.Mock(root_folder="/archive")
        self.server.on_start = mock.Mock(return_value=True)
        response = self.client.post("/api/start")
        self.assertEqual(response.status_code, 200)
        self.server.on_start.assert_called_once()

    def test_api_diagnostics_endpoints(self):
        self.server.on_camera_diagnostic = mock.Mock(return_value=True)
        response = self.client.post("/api/diagnostics/cameras")
        self.assertEqual(response.status_code, 200)
        self.server.on_vision_rule_diagnostic = mock.Mock(return_value=True)
        self.assertEqual(
            self.client.post("/api/diagnostics/vision-rules").status_code, 200,
        )

    def test_api_distributor_diagnostic(self):
        self.server.on_distributor_diagnostic = mock.Mock(return_value=True)
        response = self.client.post("/api/distributor/diagnostic/DIST1_HOME")
        self.assertEqual(response.status_code, 200)
        self.server.on_distributor_diagnostic.assert_called_once_with(
            "DIST1_HOME",
        )

    def test_api_jog(self):
        self.server.on_jog_enter = mock.Mock(return_value=True)
        self.assertEqual(self.client.post("/api/jog/enter").status_code, 200)
        self.server.on_jog_exit = mock.Mock(return_value=True)
        self.assertEqual(self.client.post("/api/jog/exit").status_code, 200)

    def test_frame_route_404_without_frames(self):
        response = self.client.get("/frame/TOP")
        self.assertEqual(response.status_code, 404)

    def test_frame_route_with_frame(self):
        self.server.update(frames={"TOP": make_frame()})
        response = self.client.get("/frame/TOP")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/jpeg")
        self.assertEqual(response.content[:2], b"\xff\xd8")

    def test_frame_route_preview(self):
        self.server.update(frames={"TOP": make_frame()})
        response = self.client.get("/frame/TOP?preview=1")
        self.assertEqual(response.status_code, 200)

    def test_mjpeg_generator_disconnected(self):
        import asyncio
        from vision.ui.server.routes_frames import _mjpeg_generator

        class FakeRequest:
            async def is_disconnected(self):
                return True

        async def run():
            chunks = []
            async for chunk in _mjpeg_generator(
                self.server, "TOP", FakeRequest(), "RAW",
            ):
                chunks.append(chunk)
            return chunks

        chunks = asyncio.run(run())
        self.assertEqual(chunks, [])

    def test_mjpeg_generator_sends_first_frame_then_disconnects(self):
        import asyncio
        from vision.ui.server.routes_frames import _mjpeg_generator

        self.server.update(frames={"TOP": make_frame()})
        calls = {"n": 0}

        class FakeRequest:
            async def is_disconnected(self):
                calls["n"] += 1
                return calls["n"] >= 3

        async def run():
            chunks = []
            async for chunk in _mjpeg_generator(
                self.server, "TOP", FakeRequest(), "RAW",
            ):
                chunks.append(chunk)
            return chunks

        chunks = asyncio.run(run())
        self.assertEqual(len(chunks), 1)
        self.assertIn(b"--frame", chunks[0])
        self.assertIn(b"\xff\xd8", chunks[0])

    def test_archive_part_route_without_archive(self):
        response = self.client.get("/api/archive/part/1")
        self.assertEqual(response.status_code, 404)

    def test_archive_part_route_with_archive(self):
        import tempfile as _tempfile

        with _tempfile.TemporaryDirectory() as tmp:
            meta_path = os.path.join(tmp, "meta.json")
            with open(meta_path, "w", encoding="utf-8") as stream:
                json.dump({"part_id": 3, "category": "GOOD"}, stream)
            archive = mock.Mock()
            archive.get_part_info.return_value = {"folder": tmp}
            archive.get_part_images.return_value = {
                "TOP": {
                    "raw": os.path.join(tmp, "TOP.jpg"),
                    "debug": os.path.join(tmp, "TOP_debug.jpg"),
                },
            }
            self.server.archive = archive
            response = self.client.get("/api/archive/part/3")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["meta"]["category"], "GOOD")
            self.assertEqual(data["roles"][0]["role"], "TOP")
            self.assertIn("raw_url", data["roles"][0])

    def test_archive_part_route_not_found(self):
        archive = mock.Mock()
        archive.get_part_info.return_value = None
        self.server.archive = archive
        response = self.client.get("/api/archive/part/99")
        self.assertEqual(response.status_code, 404)

    def test_archive_image_route(self):
        import tempfile as _tempfile

        with _tempfile.TemporaryDirectory() as tmp:
            image_path = os.path.join(tmp, "TOP.jpg")
            with open(image_path, "wb") as stream:
                stream.write(b"jpeg-data")
            archive = mock.Mock()
            archive.get_part_images.return_value = {
                "TOP": {"raw": image_path},
            }
            self.server.archive = archive
            response = self.client.get(
                "/api/archive/image/1/TOP/raw",
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b"jpeg-data")
            self.assertEqual(response.headers["content-type"], "image/jpeg")
            # part_id начинается с единицы в каждой партии, поэтому WebView
            # не должен переиспользовать снимок с тем же URL после перезапуска.
            self.assertIn("no-store", response.headers["cache-control"])
            self.assertEqual(response.headers["pragma"], "no-cache")
            self.assertEqual(response.headers["expires"], "0")

    def test_archive_image_route_bad_kind(self):
        self.server.archive = mock.Mock()
        response = self.client.get("/api/archive/image/1/TOP/other")
        self.assertEqual(response.status_code, 400)

    def test_archive_image_route_missing_image(self):
        archive = mock.Mock()
        archive.get_part_images.return_value = {}
        self.server.archive = archive
        response = self.client.get("/api/archive/image/1/TOP/raw")
        self.assertEqual(response.status_code, 404)

    def test_index_route(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn("типа 3", html)
        self.assertIn("+0 · ИНСПЕКЦИЯ", html)
        self.assertIn("+3 · СОРТИРОВКА", html)
        self.assertIn("+4 · СБРОС", html)
        self.assertIn('data-pos="4"', html)
        self.assertNotIn('data-pos="8"', html)
        self.assertNotIn("+7 · СОРТИРОВКА", html)
        self.assertIn("0 / 4", html)

    def test_static_css(self):
        response = self.client.get("/static/css/base.css")
        self.assertEqual(response.status_code, 200)

    # ---------- uvicorn thread ----------

    def test_start_server_with_fake_uvicorn(self):
        import threading as _threading

        class FakeUvicornServer:
            def __init__(self, config):
                self.config = config
                self.should_exit = False
                self.started = _threading.Event()

            def run(self):
                self.started.set()
                while not self.should_exit:
                    time.sleep(0.01)

        with mock.patch(
            "vision.ui.server.server.uvicorn.Server", FakeUvicornServer,
        ), mock.patch(
            "vision.ui.server.server.uvicorn.Config",
        ):
            self.server.start_server("127.0.0.1", 8000)
            self.assertIsNotNone(self.server.get_server_thread())
            self.server.stop_server(timeout=2.0)
            self.assertIsNone(self.server.get_server_thread())

    def test_configure_windows_event_loop_policy_on_linux(self):
        self.assertFalse(UIServer._configure_windows_event_loop_policy())


if __name__ == "__main__":
    unittest.main()
