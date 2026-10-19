from vision.overlay.renderers.primitives        import DrawPrimitives
from vision.overlay.renderers.construction_error import draw_construction_error
from vision.overlay.renderers.window_heights    import WindowHeightRenderer


class DebugOverlay:
    """
    Фасад рендеринга debug-оверлея трёхкамерной линии.

    Типы drawings:
      * ``rule_bbox`` — контур/маска детекции правила (универсально);
      * ``uneven_height_measure`` — замер высоты ячейки разновысотности;
      * ``construction_error`` — сообщение о невозможном построении.
    """

    @staticmethod
    def render_frame(frame, role, rule_results):
        img = frame.copy()

        role_drawings = []
        for rr in rule_results:
            for d in rr.drawings:
                if d.get("role") == role:
                    role_drawings.append(d)

        composed_drawings = []
        construction_messages = set()
        construction_slot = 0
        for original in role_drawings:
            drawing = original
            draw_type = drawing.get("type", "")
            if draw_type == "construction_error":
                message = str(drawing.get("message") or "NO GEOMETRY")
                if message in construction_messages:
                    continue
                construction_messages.add(message)
                drawing = dict(drawing)
                drawing["slot"] = construction_slot
                construction_slot += 1
            composed_drawings.append(drawing)

        composed_drawings.sort(
            key=lambda drawing: drawing.get("type") == "construction_error"
        )
        for d in composed_drawings:
            draw_type = d.get("type", "")

            if draw_type == "stats_panel_entry":
                # Числовая статистика отображается только в правой UI-панели.
                continue

            if draw_type == "construction_error":
                draw_construction_error(img, d)

            elif draw_type == "rule_bbox":
                DrawPrimitives.draw_rule_bbox(img, d)

            elif draw_type == "uneven_height_measure":
                WindowHeightRenderer.draw_measure(img, d)

        return img
