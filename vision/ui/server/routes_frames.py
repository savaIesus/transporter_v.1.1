import asyncio

from fastapi import HTTPException, Request, Response
from fastapi.responses import StreamingResponse


STREAM_BOUNDARY = "frame"
STREAM_POLL_INTERVAL = 0.01   # 10ms между проверками новой версии
STREAM_INITIAL_WAIT  = 5.0    # ждать до 5с появления первого кадра


def setup_frame_routes(app, server):

    @app.get("/frame/{role}")
    async def get_frame(
        role: str,
        mode: str | None = None,
        preview: int = 0,
    ):
        actual_mode = (
            mode if mode in ("RAW", "RULES") else server.mode
        )
        size_kind = "preview" if preview else "main"
        # RAW/RULES overlay + JPEG-кодирование заметно тяжелее обычного HTTP.
        # Не блокируем event loop тремя стартовыми превью: статус и команды
        # управления должны отвечать, пока изображения готовятся в worker pool.
        jpeg = await asyncio.to_thread(
            server._get_or_render, role, actual_mode, size_kind,
        )
        if jpeg is None:
            raise HTTPException(404, f"No frame for role: {role}")
        return Response(
            content=jpeg,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "no-cache, no-store",
                "X-Frame-Version": str(server._cache_version),
            },
        )

    @app.get("/stream/{role}")
    async def get_stream(role: str, request: Request, mode: str | None = None):
        """MJPEG-стрим выбранной камеры в режиме RAW или RULES."""
        actual_mode = mode if mode in ("RAW", "RULES") else server.mode
        return StreamingResponse(
            _mjpeg_generator(server, role, request, actual_mode),
            media_type=(
                f"multipart/x-mixed-replace; "
                f"boundary={STREAM_BOUNDARY}"
            ),
            headers={
                "Cache-Control": "no-cache, no-store",
                "Pragma": "no-cache",
            },
        )


async def _mjpeg_generator(
    server,
    role: str,
    request: Request,
    mode: str = "RAW",
):
    """
    Async-генератор RAW/RULES кадров в multipart/x-mixed-replace.

    - Ждёт появления первого кадра до STREAM_INITIAL_WAIT сек.
    - Отдаёт новый JPEG только когда версия изменилась.
    - Корректно завершается при разрыве соединения клиентом.
    """
    last_sent_ver = -1

    # 1. Ждём первый кадр
    waited = 0.0
    while server.get_frame_version(role) == 0:
        if await request.is_disconnected():
            return
        if waited >= STREAM_INITIAL_WAIT:
            # Отдаём пустой ответ — клиент увидит закрытое соединение
            return
        await asyncio.sleep(STREAM_POLL_INTERVAL)
        waited += STREAM_POLL_INTERVAL

    # 2. Основной цикл
    while True:
        if await request.is_disconnected():
            return

        current_ver = server.get_frame_version(role)

        if current_ver == last_sent_ver:
            await asyncio.sleep(STREAM_POLL_INTERVAL)
            continue

        # Encode в thread pool чтобы не блокировать event loop
        try:
            jpeg, ver = await asyncio.to_thread(
                server.get_stream_jpeg, role, mode,
            )
        except Exception as e:
            print(f"[STREAM] {role} encode error: {e}")
            await asyncio.sleep(0.05)
            continue

        if not jpeg:
            await asyncio.sleep(STREAM_POLL_INTERVAL)
            continue

        last_sent_ver = ver

        chunk = (
            f"--{STREAM_BOUNDARY}\r\n"
            f"Content-Type: image/jpeg\r\n"
            f"Content-Length: {len(jpeg)}\r\n"
            f"\r\n"
        ).encode("ascii") + jpeg + b"\r\n"

        try:
            yield chunk
        except (ConnectionResetError, GeneratorExit):
            return
        except Exception as e:
            print(f"[STREAM] {role} yield error: {e}")
            return
