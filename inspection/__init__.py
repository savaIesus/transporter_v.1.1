"""Inspection components with lazy imports."""

__all__ = ["InspectionResult", "Inspector", "DebugRecorder", "PartArchive"]


def __getattr__(name):
    if name == "InspectionResult":
        from inspection.result import InspectionResult
        return InspectionResult
    if name == "Inspector":
        from inspection.inspector import Inspector
        return Inspector
    if name == "DebugRecorder":
        from inspection.debug_recorder import DebugRecorder
        return DebugRecorder
    if name == "PartArchive":
        from inspection.part_archive import PartArchive
        return PartArchive
    raise AttributeError(name)
