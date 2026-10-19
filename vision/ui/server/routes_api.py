import asyncio

from fastapi import HTTPException
from fastapi.responses import JSONResponse


def setup_api_routes(app, server):

    async def invoke(name, callback, *args):
        if callback is None:
            return JSONResponse(
                {"ok": False, "error": "Система ещё не готова"},
                status_code=503,
            )
        try:
            accepted = await asyncio.to_thread(callback, *args)
        except Exception as exc:
            print(f"[API] {name} error: {exc}")
            return JSONResponse(
                {"ok": False, "error": str(exc)},
                status_code=500,
            )
        if accepted is False:
            return JSONResponse(
                {"ok": False, "error": f"Команда «{name}» недоступна в текущем состоянии"},
                status_code=409,
            )
        return JSONResponse({"ok": True})

    @app.get("/api/cameras")
    async def get_cameras():
        with server.lock:
            roles = list(dict.fromkeys(
                [*server.camera_roles, *server.frames.keys()]
            ))
            camera_ids = dict(server.camera_mapping)
        sorted_roles = server._sort_by_order(roles)
        return JSONResponse({
            "cameras": sorted_roles,
            "camera_ids": camera_ids,
        })

    @app.get("/api/boot")
    async def get_boot():
        with server.lock:
            steps = [
                {
                    "key":    key,
                    "label":  label,
                    "status": server.boot_steps.get(
                        key, "pending",
                    ),
                }
                for key, label in server.BOOT_STEPS
            ]
            done_count = sum(
                1 for s in steps if s["status"] == "done"
            )
            total = len(steps)
            return JSONResponse({
                "active":   server.splash_active,
                "steps":    steps,
                "current":  server.boot_current,
                "message":  server.boot_message,
                "error":    server.boot_error,
                "progress": (
                    done_count / total if total else 0
                ),
                "log": list(server.splash_log[-12:]),
            })

    @app.get("/api/status")
    async def get_status():
        # Автоподхват порогов: если thresholds.json изменился вручную,
        # перечитываем его (в рабочем потоке) до отдачи статуса.
        if server.thresholds_file_mtime_changed():
            await asyncio.to_thread(server.reload_thresholds_from_file)
        with server.lock:
            return JSONResponse({
                "splash_active": server.splash_active,
                "line_status":   server.line_status,
                "recent_parts":  server.recent_parts,
                "mode":          server.mode,
                "frame_version": server._cache_version,
                "frame_versions": dict(server._latest_frames_ver),
                "active_camera": server.active_camera_role,
                "thresholds_revision": server.thresholds_revision,
                "archive": server.archive_status_payload(),
            })

    @app.get("/api/mode")
    async def get_mode():
        return JSONResponse({"mode": server.mode})

    @app.post("/api/mode/{mode}")
    async def set_mode(mode: str):
        if mode not in ("RAW", "RULES"):
            raise HTTPException(400, "Недопустимый режим отображения")
        with server.lock:
            server.mode = mode
            server._jpeg_cache.clear()
            server._latest_stream_jpeg.clear()
            server._cache_version += 1
            frame_version = server._cache_version
        return JSONResponse({
            "mode": server.mode,
            "frame_version": frame_version,
        })

    @app.post("/api/active_camera/{role}")
    async def set_active_camera(role: str):
        if not role:
            raise HTTPException(400, "Роль камеры не указана")
        if not server.set_active_camera_role(role):
            raise HTTPException(400, "Камера не найдена или недоступна")
        return JSONResponse({
            "ok": True,
            "active_camera": server.active_camera_role,
        })

    # Пороги правил (редактирование до пуска и после полной остановки)

    @app.get("/api/thresholds")
    async def api_get_thresholds(role: str | None = None):
        return JSONResponse(
            await asyncio.to_thread(server.build_thresholds_payload, role)
        )

    @app.post("/api/thresholds")
    async def api_set_thresholds(payload: dict | None = None):
        payload = payload or {}
        role = payload.get("role")
        values = payload.get("values")
        labels = payload.get("labels")
        if not isinstance(role, str) or not role:
            raise HTTPException(400, "Роль камеры не указана")
        if not isinstance(values, dict) or not values:
            raise HTTPException(400, "Не указаны значения порогов")
        if labels is not None:
            if not isinstance(labels, dict) or any(
                not isinstance(name, str) for name in labels.values()
            ):
                raise HTTPException(
                    400, "Названия порогов должны быть объектом со строками"
                )
        if not server.thresholds_editable():
            return JSONResponse(
                {
                    "ok": False,
                    "error": "Изменение порогов доступно только до пуска "
                             "и после полной остановки",
                },
                status_code=409,
            )
        if server.on_thresholds_apply is None:
            return JSONResponse(
                {"ok": False, "error": "Система ещё не готова"},
                status_code=503,
            )
        try:
            result = await asyncio.to_thread(
                server.apply_thresholds, role, values, labels,
            )
        except ValueError as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc)},
                status_code=400,
            )
        except RuntimeError as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc)},
                status_code=409,
            )
        except Exception as exc:
            print(f"[API] thresholds error: {exc}")
            return JSONResponse(
                {"ok": False, "error": str(exc)},
                status_code=500,
            )
        return JSONResponse({"ok": True, "thresholds": result})

    # Обязательный архив партий: до начала партии меняется только корневая папка.

    @app.get("/api/archive/settings")
    async def api_get_archive_settings():
        return JSONResponse(
            await asyncio.to_thread(server.build_archive_payload, True)
        )

    @app.post("/api/archive/settings")
    async def api_set_archive_settings(payload: dict | None = None):
        try:
            result = await asyncio.to_thread(
                server.apply_archive_settings, payload or {}
            )
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except RuntimeError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
        except OSError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except Exception as exc:
            print(f"[API] archive settings error: {exc}")
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
        return JSONResponse({"ok": True, "archive": result})

    @app.post("/api/start")
    async def api_start():
        print("[API] /api/start called")
        archive_ready, archive_error = server.archive_ready_for_start()
        if not archive_ready:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "Архив недоступен: " + (archive_error or "проверьте папку"),
                },
                status_code=409,
            )
        return await invoke("ПУСК", server.on_start)

    @app.post("/api/stop")
    async def api_stop():
        print("[API] /api/stop called")
        return await invoke("СТОП", server.on_stop)

    @app.post("/api/pause")
    async def api_pause():
        print("[API] /api/pause called")
        return await invoke("ПАУЗА", server.on_pause)

    @app.post("/api/resume")
    async def api_resume():
        print("[API] /api/resume called")
        return await invoke("ПРОДОЛЖИТЬ", server.on_resume)

    @app.post("/api/exit")
    async def api_exit():
        print("[API] /api/exit called")
        return await invoke("ВЫХОД", server.on_exit)

    @app.post("/api/diagnostics/cameras")
    async def api_diagnostic_cameras():
        return await invoke(
            "ПРОВЕРКА КАМЕР",
            server.on_camera_diagnostic,
        )

    @app.post("/api/diagnostics/vision-rules")
    async def api_diagnostic_vision_rules():
        return await invoke(
            "ПРОВЕРКА МОДЕЛЕЙ И ПРАВИЛ",
            server.on_vision_rule_diagnostic,
        )

    @app.post("/api/diagnostics/selected/release")
    async def api_diagnostic_selected_release():
        return await invoke(
            "ВОЗВРАТ К ПОТОКУ",
            server.on_selected_model_release,
        )

    @app.post("/api/diagnostics/selected/{role}")
    async def api_diagnostic_selected_model(role: str):
        return await invoke(
            "АНАЛИЗ КАДРА",
            server.on_selected_model_analysis,
            role,
        )

    @app.post("/api/distributor/diagnostic/{command}")
    async def api_distributor_diagnostic(command: str):
        allowed = {
            "DIST1_HOME",
            "DIST1_OPEN",
            "DIST2_BAD",
            "DIST2_CLEANUP",
        }
        if command not in allowed:
            raise HTTPException(400, "Недопустимая команда распределителя")
        return await invoke(
            "ПРОВЕРКА РАСПРЕДЕЛИТЕЛЯ",
            server.on_distributor_diagnostic,
            command,
        )

    # JOG

    @app.post("/api/jog/enter")
    async def api_jog_enter():
        return await invoke("ВХОД В РУЧНОЙ РЕЖИМ", server.on_jog_enter)

    @app.post("/api/jog/exit")
    async def api_jog_exit():
        return await invoke("ВЫХОД ИЗ РУЧНОГО РЕЖИМА", server.on_jog_exit)

    @app.post("/api/jog/hold/start")
    async def api_jog_hold_start(payload: dict):
        direction = payload.get("direction")
        if direction not in ("+", "-"):
            raise HTTPException(400, "Недопустимое направление")
        return await invoke(
            "НАЧАЛО РУЧНОГО ДВИЖЕНИЯ", server.on_jog_hold_start, direction
        )

    @app.post("/api/jog/hold/heartbeat")
    async def api_jog_hold_heartbeat(payload: dict):
        direction = payload.get("direction")
        if direction not in ("+", "-"):
            raise HTTPException(400, "Недопустимое направление")
        return await invoke(
            "СИГНАЛ УДЕРЖАНИЯ",
            server.on_jog_hold_heartbeat,
            direction,
        )

    @app.post("/api/jog/hold/release")
    async def api_jog_hold_release(payload: dict | None = None):
        reason = "button released"
        if isinstance(payload, dict) and payload.get("reason"):
            reason = str(payload["reason"])[:100]
        return await invoke(
            "ОСТАНОВКА РУЧНОГО ДВИЖЕНИЯ", server.on_jog_hold_release, reason
        )
