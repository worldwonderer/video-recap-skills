"""Multi-track timeline model for the recap (backend-neutral, stdlib only).

A `Timeline` is a small, serializable representation of the finished recap as a
set of tracks — exactly like a cut-tool project:

  - one **video** track: the source clip(s), each carrying its own *original
    audio* with a per-clip volume automation (the ducking: a continuous low bed
    under narration, held across short inter-sentence gaps, back up only at the
    lead-in/out and genuine long gaps);
  - one **narration** audio track: the placed TTS beats;
  - an optional **bgm** audio track: a looped music bed with its own ducking;
  - one **subtitle** (text) track: the narration lines.

The canonical ducking semantics live in `audio_automation.py`; ffmpeg
(`assemble.py`) and this timeline model both derive their automation from that
shared source. This model is emitted as `timeline.json` and consumed by the
*optional* 剪映 exporter. The model itself knows nothing about ffmpeg or 剪映 —
times are plain seconds and volumes are plain gains, so any backend can read it.
"""

import json

from audio_automation import fixed_ducking_keyframes as ducking_keyframes
from audio_automation import variable_ducking_keyframes

SCHEMA_VERSION = 1


def build_timeline(canvas, duration_s, video_clips, narration_segments,
                   bgm=None, ducking=None, subtitle_segments=None):
    """Assemble a Timeline dict from resolved placement data.

    canvas: {"width", "height", "fps"}
    duration_s: total output length (seconds)
    video_clips: ordered [{"source_path", "source_start", "source_end",
                 "timeline_start", "timeline_end"}] (cut mode: one per clip;
                 full mode: a single clip spanning the whole video).
    narration_segments: placed beats [{"source_path", "timeline_start",
                 "timeline_end", "text", "overlaps_speech", "gain"?}].
    subtitle_segments: optional display-ready text cues [{"text", "timeline_start",
                 "timeline_end"}]. When present, this is authoritative for the
                 subtitle/text track; narration segment text remains raw editor metadata.
    bgm: optional {"source_path", "volume", "ducking_volume"}.
    ducking: {"idle", "speech", "quiet", "fade", "bridge"?} for the original-audio
             automation; None disables original ducking (flat original). `bridge` holds
             the duck across inter-beat gaps shorter than it (defaults to 2*fade).
    """
    windows = [(float(s["timeline_start"]), float(s["timeline_end"]))
               for s in narration_segments
               if s.get("timeline_end", 0) > s.get("timeline_start", 0)]
    duck_windows = [
        (
            float(s["timeline_start"]),
            float(s["timeline_end"]),
            float((ducking or {}).get("speech" if s.get("overlaps_speech", True) else "quiet", 1.0)),
        )
        for s in narration_segments
        if s.get("timeline_end", 0) > s.get("timeline_start", 0)
    ]

    # --- video track: each clip carries its original audio + ducking automation
    video_clip_objs = []
    for c in video_clips:
        ts, te = float(c["timeline_start"]), float(c["timeline_end"])
        audio = {"role": "original", "volume_keyframes": []}
        if ducking is not None:
            audio["volume_keyframes"] = variable_ducking_keyframes(
                duck_windows, ducking["idle"], ducking["fade"], ts, te,
                bridge=ducking.get("bridge"))
            audio["base_gain"] = round(float(ducking["idle"]), 4)
        else:
            audio["base_gain"] = 1.0
        video_clip_objs.append({
            "source_path": c["source_path"],
            "source_start": round(float(c["source_start"]), 4),
            "source_end": round(float(c["source_end"]), 4),
            "timeline_start": round(ts, 4),
            "timeline_end": round(te, 4),
            "audio": audio,
        })

    tracks = [{"kind": "video", "name": "video", "clips": video_clip_objs}]

    # --- narration track
    narr_segs = []
    for s in narration_segments:
        ts, te = float(s["timeline_start"]), float(s["timeline_end"])
        if te <= ts:
            continue
        narr_segs.append({
            "source_path": s["source_path"],
            "timeline_start": round(ts, 4),
            "timeline_end": round(te, 4),
            "gain": round(float(s.get("gain", 1.0)), 4),
            "text": s.get("text", ""),
            "overlaps_speech": bool(s.get("overlaps_speech", True)),
        })
    if narr_segs:
        tracks.append({"kind": "audio", "name": "narration", "role": "narration",
                       "segments": narr_segs})

    # --- bgm track (optional, looped, ducked under narration)
    if bgm and bgm.get("source_path"):
        base = float(bgm.get("volume", 0.18))
        duck = float(bgm.get("ducking_volume", 0.10))
        fade = float(bgm.get("fade") or (ducking or {}).get("fade", 0.25))
        kfs = ducking_keyframes(windows, base, duck, fade, 0.0, duration_s,
                                bridge=(ducking or {}).get("bridge"))
        tracks.append({
            "kind": "audio", "name": "bgm", "role": "bgm", "loop": True,
            "segments": [{
                "source_path": bgm["source_path"],
                "timeline_start": 0.0,
                "timeline_end": round(float(duration_s), 4),
                "gain": round(base, 4),
                "volume_keyframes": kfs,
            }],
        })

    # --- subtitle (text) track
    text_source = subtitle_segments if subtitle_segments is not None else narration_segments
    text_segs = []
    for s in text_source or []:
        if not isinstance(s, dict) or not s.get("text"):
            continue
        try:
            ts = float(s["timeline_start"])
            te = float(s["timeline_end"])
        except (KeyError, TypeError, ValueError):
            continue
        if te <= ts:
            continue
        text_segs.append({
            "text": s.get("text", ""),
            "timeline_start": round(ts, 4),
            "timeline_end": round(te, 4),
        })
    if text_segs:
        tracks.append({"kind": "text", "name": "subtitle", "segments": text_segs})

    return {
        "schema_version": SCHEMA_VERSION,
        "canvas": {"width": int(canvas["width"]), "height": int(canvas["height"]),
                   "fps": float(canvas.get("fps", 30))},
        "duration": round(float(duration_s), 4),
        "tracks": tracks,
    }


def save_timeline(timeline, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(timeline, f, ensure_ascii=False, indent=2)
    return path


def load_timeline(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)
