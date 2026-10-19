from vision.overlay.renderers.primitives import COLOR_FAIL, DrawPrimitives


def draw_construction_error(img, drawing):
    message = str(drawing.get("message") or "NO GEOMETRY")
    bbox = drawing.get("bbox") or [0, 0, 0, 0]
    x1, y1, _x2, _y2 = map(int, bbox)
    slot = max(0, int(drawing.get("slot") or 0))
    if x1 == 0 and y1 == 0:
        position = (20, 30 + slot * 20)
    else:
        base_y = max(20, y1 - 6) if y1 >= 26 else max(20, y1 + 18)
        position = (
            max(5, x1),
            min(img.shape[0] - 4, base_y + slot * 20),
        )
    DrawPrimitives.draw_text_with_bg(
        img,
        message,
        position,
        COLOR_FAIL,
        font_scale=0.46,
        bg_pad=4,
    )
