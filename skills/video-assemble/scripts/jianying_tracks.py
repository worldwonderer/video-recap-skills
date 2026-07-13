"""Semantic track ordering and overlap-safe allocation for JianYing export.

The order mirrors duo-video's authoring layout. It is used only to order track
objects; JianYing segment ``render_index`` remains the schema default and must
not be confused with this semantic layout value.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TrackBand:
    kind: str
    track_type: str
    layout_order: int
    description: str


SEGMENT_RENDER_INDEX = 2


TRACK_LAYOUT_BANDS = {
    "sound": TrackBand("sound", "audio", 10_000, "sound effects"),
    "audio": TrackBand("audio", "audio", 20_000, "narration, music, and general audio"),
    "green_screen": TrackBand("green_screen", "video", 30_000, "green-screen background"),
    "video": TrackBand("video", "video", 40_000, "base video"),
    "image": TrackBand("image", "video", 50_000, "image and photo overlays"),
    "mask": TrackBand("mask", "video", 60_000, "masks"),
    "effect": TrackBand("effect", "effect", 70_000, "video effects"),
    "video_effect": TrackBand("video_effect", "effect", 70_000, "video effects"),
    "face_effect": TrackBand("face_effect", "effect", 70_010, "face effects"),
    "sticker": TrackBand("sticker", "sticker", 80_000, "stickers"),
    "subtitle": TrackBand("subtitle", "text", 90_000, "subtitles"),
    "text": TrackBand("text", "text", 100_000, "plain text"),
    "text_template": TrackBand("text_template", "text", 110_000, "text templates"),
}


@dataclass(frozen=True)
class AllocatedTrack:
    kind: str
    name: str
    track_type: str
    layout_order: int


class TrackAllocator:
    """Allocate deterministic suffix tracks when same-name segments overlap.

    Intervals are half-open, so adjacent segments reuse a track while true
    overlap creates ``name-1``, ``name-2``, and so on.
    """

    def __init__(self):
        self._occupied = {}

    @staticmethod
    def _overlaps(start_us, duration_us, existing):
        end_us = int(start_us) + int(duration_us)
        return any(int(start_us) < old_end and end_us > old_start for old_start, old_end in existing)

    def allocate(self, kind, base_name, start_us, duration_us):
        band = TRACK_LAYOUT_BANDS.get(kind)
        if band is None:
            band = TrackBand(kind, "video", 120_000, "unregistered fallback")
        base_name = base_name or kind
        suffix = 0
        while True:
            name = base_name if suffix == 0 else f"{base_name}-{suffix}"
            key = (kind, name)
            occupied = self._occupied.setdefault(key, [])
            if not self._overlaps(start_us, duration_us, occupied):
                occupied.append((int(start_us), int(start_us) + int(duration_us)))
                return AllocatedTrack(kind, name, band.track_type, band.layout_order + suffix)
            suffix += 1
