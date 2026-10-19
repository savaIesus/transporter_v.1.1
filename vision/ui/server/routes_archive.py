import os
from fastapi import HTTPException, Response
from fastapi.responses import JSONResponse


def setup_archive_routes(app, server):

    @app.get("/api/archive/part/{part_id}")
    async def get_archive_part(part_id: int):
        if not server.archive:
            raise HTTPException(404, "Archive not available")

        info = server.archive.get_part_info(part_id)
        if not info:
            raise HTTPException(
                404, f"Part #{part_id} not found in archive"
            )

        images = server.archive.get_part_images(part_id)

        meta = {}
        meta_path = os.path.join(info["folder"], "meta.json")
        if os.path.exists(meta_path):
            import json
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)

        sorted_role_names = server._sort_by_order(list(images.keys()))

        roles_data = []
        for role in sorted_role_names:
            paths = images[role]
            entry = {"role": role}
            if "raw" in paths:
                entry["raw_url"] = (
                    f"/api/archive/image/{part_id}"
                    f"/{role}/raw"
                )
            if "raw_overlay" in paths:
                entry["raw_overlay_url"] = (
                    f"/api/archive/image/{part_id}"
                    f"/{role}/raw_overlay"
                )
            if "debug" in paths:
                entry["debug_url"] = (
                    f"/api/archive/image/{part_id}"
                    f"/{role}/debug"
                )
            roles_data.append(entry)

        return JSONResponse({
            "part_id": part_id,
            "meta":    meta,
            "roles":   roles_data,
        })

    @app.get("/api/archive/image/{part_id}/{role}/{kind}")
    async def get_archive_image(part_id: int, role: str, kind: str):
        if not server.archive:
            raise HTTPException(404, "Archive not available")

        valid_kinds = ("raw", "raw_overlay", "debug")
        if kind not in valid_kinds:
            raise HTTPException(
                400,
                f"kind must be one of {valid_kinds}",
            )

        images = server.archive.get_part_images(part_id)
        if role not in images or kind not in images[role]:
            raise HTTPException(
                404,
                f"Image not found: part={part_id} "
                f"role={role} kind={kind}",
            )

        path = images[role][kind]

        with open(path, "rb") as f:
            data = f.read()

        return Response(
            content=data,
            media_type="image/jpeg",
            headers={
                # Номер детали начинается заново в каждой партии, а URL не
                # содержит batch_id. Запрещаем долговременный кэш WebView,
                # иначе новая деталь #1 могла показать снимок прошлой партии.
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )
