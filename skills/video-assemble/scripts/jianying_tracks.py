"""Track layout bands and deterministic overlap-safe allocation for JianYing export."""

from dataclasses import dataclass


@dataclass(frozen=True)
class TrackBand:
    kind: str
    track_type: str
    render_index: int
    description: str


# Parity values for existing output are preserved: video at 0, narration/BGM at
# 1/2 via explicit role handling, and subtitles in JianYing's high text band.
RI_VIDEO = 0
RI_NARRATION = 1
RI_BGM = 2
RI_TEXT = 15000


TRACK_LAYOUT_BANDS = {
    "audio": TrackBand("audio", "audio", RI_NARRATION, "narration and general audio below text"),
    "sound": TrackBand("sound", "audio", RI_BGM, "sound/BGM bed lane"),
    "video": TrackBand("video", "video", RI_VIDEO, "base video track"),
    "image": TrackBand("image", "video", 1000, "future image/overlay lane"),
    "overlay": TrackBand("overlay", "video", 1000, "future video/image overlay lane"),
    "mask": TrackBand("mask", "video", 4000, "future mask/effect lane"),
    "effect": TrackBand("effect", "video", 5000, "future effects lane"),
    "video_effect": TrackBand("video_effect", "video", 5000, "future video effects lane"),
    "sticker": TrackBand("sticker", "sticker", 10000, "future sticker lane"),
    "subtitle": TrackBand("subtitle", "text", RI_TEXT, "subtitle text lane"),
    "text": TrackBand("text", "text", RI_TEXT, "plain text lane"),
    "text_template": TrackBand("text_template", "text", RI_TEXT + 100, "future text template lane"),
}


@dataclass(frozen=True)
class AllocatedTrack:
    kind: str
    name: str
    track_type: str
    render_index: int


class TrackAllocator:
    """Allocate deterministic suffix tracks when same-name segments overlap."""

    def __init__(self):
        self._occupied = {}

    @staticmethod
    def _overlaps(start_us, duration_us, existing):
        end_us = int(start_us) + int(duration_us)
        return any(int(start_us) < old_end and end_us > old_start for old_start, old_end in existing)

    def allocate(self, kind, base_name, start_us, duration_us):
        band = TRACK_LAYOUT_BANDS.get(kind, TRACK_LAYOUT_BANDS["video"])
        base_name = base_name or kind
        suffix = 0
        while True:
            name = base_name if suffix == 0 else f"{base_name}-{suffix}"
            key = (kind, name)
            occupied = self._occupied.setdefault(key, [])
            if not self._overlaps(start_us, duration_us, occupied):
                occupied.append((int(start_us), int(start_us) + int(duration_us)))
                return AllocatedTrack(kind, name, band.track_type, band.render_index + suffix)
            suffix += 1
