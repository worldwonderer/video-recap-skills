"""Thin internal model used at the JianYing adapter boundary."""

import os
from dataclasses import dataclass, field
from typing import Callable

from jianying_schema import us


ProbeFn = Callable[[str], tuple[int, int, int]]
NewIdFn = Callable[[], str]


@dataclass
class DraftBuildContext:
    """Normalized draft build state.

    Public `timeline.json` stays backend-neutral (seconds/gains). This context is
    the adapter-local place where canvas values and durations are normalized into
    JianYing-friendly integers and material/track arrays are accumulated.
    """

    width: int
    height: int
    fps: float
    total_us: int
    new_id: NewIdFn
    probe: ProbeFn
    materials: dict[str, list] = field(
        default_factory=lambda: {"videos": [], "audios": [], "texts": [], "speeds": []}
    )
    tracks: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @classmethod
    def from_timeline(cls, timeline, new_id, probe):
        canvas = timeline.get("canvas", {})
        return cls(
            width=int(canvas.get("width", 1920)),
            height=int(canvas.get("height", 1080)),
            fps=float(canvas.get("fps", 30)),
            total_us=us(timeline.get("duration", 0)),
            new_id=new_id,
            probe=probe,
        )

    def media_duration(self, path, fallback_us):
        if path and os.path.exists(path):
            duration_us, width, height = self.probe(path)
            return duration_us or fallback_us, width, height
        return fallback_us, 0, 0

    def note(self, message):
        self.notes.append(message)
