from dataclasses import dataclass, field


@dataclass
class InspectionResult:
    """Результат одной стадии инспекции по свежему кадру."""

    stage: str
    defects: list = field(default_factory=list)
    vision_results: dict = field(default_factory=dict)
    rule_results: list = field(default_factory=list)
    annotated: dict = field(default_factory=dict)
    raw_frames: dict = field(default_factory=dict)
    raw_overlay_frames: dict = field(default_factory=dict)

    # True устанавливается только для INPUT по part_presence.
    is_empty_tray: bool = False

