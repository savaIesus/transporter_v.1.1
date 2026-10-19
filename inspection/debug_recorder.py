import os
import cv2
from datetime import datetime
from domain.defect_rules import RuleResult
from vision.overlay.debug_overlay import DebugOverlay


class DebugRecorder:
    """
    Аннотирует кадры результатами правил и сохраняет на диск.
    Возвращает аннотированные кадры вызывающей стороне для UI.
    """

    def __init__(
        self,
        folder: str = "debug_frames",
        enabled: bool = True,
        save_interval: int = 1,
    ):
        self.folder        = folder
        self.enabled       = enabled
        self.save_interval = save_interval

        self._step_counter   = 0

        if self.enabled:
            os.makedirs(self.folder, exist_ok=True)

    # Public API

    def process(
        self,
        part_id: int,
        step: int,
        frames: dict,
        rule_results: list[RuleResult],
    ) -> dict:
        annotated = self._annotate(frames, rule_results)

        if self._should_save():
            self._save(part_id, step, annotated)

        return annotated

    # Internal

    def _annotate(
        self,
        frames: dict,
        rule_results: list[RuleResult],
    ) -> dict:
        annotated = {}
        for role, frame in frames.items():
            annotated[role] = DebugOverlay.render_frame(
                frame, role, rule_results
            )
        return annotated

    def _should_save(self) -> bool:
        if not self.enabled:
            return False
        self._step_counter += 1
        return (self._step_counter % self.save_interval) == 0

    def _save(self, part_id: int, step: int, annotated: dict):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        folder = os.path.join(
            self.folder,
            f"step{step:04d}_part{part_id}_{ts}",
        )
        os.makedirs(folder, exist_ok=True)

        for role, img in annotated.items():
            path = os.path.join(folder, f"{role}.jpg")
            cv2.imwrite(path, img, [cv2.IMWRITE_JPEG_QUALITY, 92])

        print(
            f"[DEBUG] Saved {len(annotated)} frames -> {folder}"
        )