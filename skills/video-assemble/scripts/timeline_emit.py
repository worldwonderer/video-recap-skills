"""Backend-neutral timeline emission for the video-assemble skill."""

from pathlib import Path

from audio_mix import _seg_place_window
from lib import CONFIG, log
from media import _build_video_clips, _probe_canvas
from source_subtitles import _combined_subtitle_entries
from timeline import build_timeline, save_timeline

def _timeline_subtitle_segments(tts_segments, work_dir, duration_s):
    """Display-ready subtitle cues for timeline/export text tracks.

    The narration audio track keeps raw semantic text for editor reference; this
    payload mirrors SRT/ASS display policy, including terminal-punctuation cleanup
    and original-dialogue gap subtitles when configured.
    """
    return [
        {
            "text": entry["text"],
            "timeline_start": float(entry["start"]),
            "timeline_end": float(entry["end"]),
        }
        for entry in _combined_subtitle_entries(tts_segments, work_dir, duration_s)
    ]


def _emit_timeline(input_video, tts_segments, work_dir, duration_s, has_bgm):
    """Build and persist the backend-neutral multi-track timeline.json."""
    canvas = _probe_canvas(input_video)
    video_clips = _build_video_clips(input_video, work_dir, duration_s)
    narration_segments = []
    for seg in tts_segments:
        if not isinstance(seg, dict):
            continue
        s, e = _seg_place_window(seg)
        if e <= s:
            continue
        narration_item = {
            # JianYing must consume the exact WAV written into narration.wav. In
            # particular, a tempo-adjusted beat cannot reference its longer pre-fit
            # source or the editor will trim its final words at timeline_end.
            "source_path": seg.get("placed_audio_path") or seg.get("audio_path", ""),
            "timeline_start": s, "timeline_end": e,
            "text": seg.get("narration", ""),
            "overlaps_speech": seg.get("overlaps_speech", True),
            "gain": 1.0,
        }
        for key in ("source_duck_end", "source_restore_at", "source_handoff_status", "source_entry_status"):
            if key in seg:
                narration_item[key] = seg[key]
        narration_segments.append(narration_item)
    fade = CONFIG.get("duck_fade_seconds", 0.3)
    bgm = None
    if has_bgm:
        bgm = {"source_path": CONFIG.get("bgm_path", ""),
               "volume": CONFIG.get("bgm_volume", 0.18),
               "ducking_volume": CONFIG.get("bgm_ducking_volume", 0.10),
               "fade": fade}
    # carry ducking automation whenever ducking is on at all; even under sidechain
    # mode the draft gets editable volume keyframes (ffmpeg stays the canonical mix)
    ducking = None
    if CONFIG.get("ducking_mode", "fixed") != "none":
        ducking = {"idle": CONFIG.get("idle_orig_volume", 1.0),
                   "speech": CONFIG.get("speech_ducking_volume", 0.2),
                   "quiet": CONFIG.get("zone_ducking_volume", 0.12),
                   "fade": fade,
                   "bridge": CONFIG.get("duck_bridge_seconds", 1.5)}
    subtitle_segments = _timeline_subtitle_segments(tts_segments, work_dir, duration_s)
    timeline = build_timeline(canvas, duration_s, video_clips,
                              narration_segments, bgm=bgm, ducking=ducking,
                              subtitle_segments=subtitle_segments)
    degraded = [
        {
            "source_path": clip.get("source_path"),
            "reason": clip.get("provenance_reason") or "unknown",
        }
        for clip in video_clips
        if clip.get("provenance_degraded")
    ]
    if degraded:
        timeline["provenance"] = {"degraded": True, "degraded_clips": degraded}
        log(f"  ⚠️ 时间线 provenance 降级: {degraded[0]['reason']} ({len(degraded)} clip)")
    else:
        timeline["provenance"] = {"degraded": False}
    out = Path(work_dir) / "timeline.json"
    save_timeline(timeline, out)
    log(f"时间线模型: {out} ({len(timeline['tracks'])} 轨)")
    return timeline
