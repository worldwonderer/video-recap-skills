"""Thin internal model used at the JianYing adapter boundary."""

import os
from dataclasses import dataclass, field
from typing import Callable

from jianying_schema import us
from jianying_tracks import TrackAllocator


ProbeFn = Callable[[str], tuple[int, int, int]]
NewIdFn = Callable[[], str]


@dataclass
class DraftBuildContext:
    """Normalized draft build state and overlap-safe track collection."""

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
    track_allocator: TrackAllocator = field(default_factory=TrackAllocator)

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

    def add_segment(self, kind, base_name, start_us, duration_us, segment):
        allocated = self.track_allocator.allocate(kind, base_name, start_us, duration_us)
        track = next(
            (
                item for item in self.tracks
                if item["type"] == allocated.track_type and item["name"] == allocated.name
            ),
            None,
        )
        if track is None:
            track = {
                "attribute": 0,
                "flag": 0,
                "id": self.new_id(),
                "is_default_name": True,
                "name": allocated.name,
                "segments": [],
                "type": allocated.track_type,
                "_layout_order": allocated.layout_order,
            }
            self.tracks.append(track)
        track["segments"].append(segment)
        return track

    def finalize_tracks(self):
        # Python's stable sort preserves the timeline's authored order inside
        # one semantic band (for example narration before BGM).
        self.tracks.sort(key=lambda item: item.get("_layout_order", 120_000))
        type_counts = {}
        for track in self.tracks:
            count = type_counts.get(track["type"], 0)
            track["flag"] = 0 if count == 0 else 2
            type_counts[track["type"]] = count + 1
            track.pop("_layout_order", None)
        return self.tracks

    def note(self, message):
        self.notes.append(message)
