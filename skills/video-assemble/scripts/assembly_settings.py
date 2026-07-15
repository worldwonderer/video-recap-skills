"""Render-affecting settings fingerprint for cache/resume decisions."""

from pathlib import Path

from artifacts import _artifact_fingerprint
from assemble_constants import (
    SUBTITLE_RENDER_VERSION,
    SUBTITLE_TEXT_NORMALIZE_VERSION,
    VISUAL_OVERLAYS,
)
from audio_mix import _loudness_mode, final_loudnorm_filter
from lib import CONFIG
from source_subtitles import _has_user_subtitles, _source_subtitle_mask_policy
from subtitle_core import _subtitle_style_config

def assembly_settings_fingerprint(work_dir=None):
    """Settings that affect the rendered video, used by pipeline resume cache. When work_dir is
    given, a user_subtitles presence flag is included so dropping in a user-subtitle file rebuilds
    the cached subtitles."""
    burn_subtitles = bool(CONFIG.get("burn_subtitles", False))
    mask_policy = _source_subtitle_mask_policy(work_dir)
    mask_source_subtitles = bool(mask_policy["active"])
    user_subtitles = _has_user_subtitles(work_dir)
    overlay_path = Path(work_dir) / VISUAL_OVERLAYS if work_dir is not None else None
    fingerprint = {
        "version": SUBTITLE_RENDER_VERSION,
        "subtitle_text_normalize": SUBTITLE_TEXT_NORMALIZE_VERSION,
        "user_subtitles": user_subtitles,
        "burn_subtitles": burn_subtitles,
        "force_video_reencode": bool(CONFIG.get("force_video_reencode", False)),
        "encode": {
            "output_crf": CONFIG.get("output_crf", 18),
            "output_preset": CONFIG.get("output_preset", "veryfast"),
            "output_max_height": CONFIG.get("output_max_height", 0),
        },
        "video_filters": {
            "mask_source_subtitles": mask_source_subtitles,
            "source_subtitle_mask_policy": mask_policy["policy"],
            "source_subtitle_mask_policy_declared": mask_policy["declared"],
            "source_subtitle_mask_policy_trigger": mask_policy["trigger"],
            "source_subtitle_mask_ratio": (
                CONFIG.get("source_subtitle_mask_ratio", 0.14) if mask_source_subtitles else None
            ),
            "source_subtitle_mask_timing": (
                CONFIG.get("source_subtitle_mask_timing", "narration") if mask_source_subtitles else None
            ),
            "subtitle_mask_opacity": (
                CONFIG.get("subtitle_mask_opacity", 0.6) if mask_source_subtitles else None
            ),
            "subtitle_mask_padding": (
                CONFIG.get("subtitle_mask_padding", 4) if mask_source_subtitles else None
            ),
            "subtitle_y_top": CONFIG.get("subtitle_y_top", -1),
            "subtitle_y_bot": CONFIG.get("subtitle_y_bot", -1),
            "visual_overlays": {
                "artifact": VISUAL_OVERLAYS,
                "present": bool(overlay_path and overlay_path.exists()),
                "fingerprint": _artifact_fingerprint(overlay_path) if overlay_path and overlay_path.exists() else None,
            },
        },
        "narration_timing": {
            "delay_seconds": CONFIG.get("narration_delay_seconds", 0.0),
            "tail_pad_seconds": CONFIG.get("narration_tail_pad_seconds", 0.1),
            "fade_ms": CONFIG.get("fade_ms", 120),
            "narration_speed": CONFIG.get("narration_speed", 1.0),
            "narration_cumulative_tempo_max": CONFIG.get("narration_cumulative_tempo_max", 1.35),
            "tts_segment_tempo_max": CONFIG.get("tts_segment_tempo_max", 1.20),
        },
        "audio_mix": {
            "ducking_mode": CONFIG.get("ducking_mode", "fixed"),
            "duck_fade_seconds": CONFIG.get("duck_fade_seconds", 0.3),
            "duck_bridge_seconds": CONFIG.get("duck_bridge_seconds", 1.5),
            "ducking_narr_weight": CONFIG.get("ducking_narr_weight", 1.5),
            "ducking_orig_volume": CONFIG.get("ducking_orig_volume", 0.3),
            "idle_orig_volume": CONFIG.get("idle_orig_volume", 1.0),
            "speech_ducking_volume": CONFIG.get("speech_ducking_volume", 0.2),
            "zone_ducking_volume": CONFIG.get("zone_ducking_volume", 0.12),
            "ducking_threshold": CONFIG.get("ducking_threshold", 0.15),
            "ducking_ratio": CONFIG.get("ducking_ratio", 3),
            "ducking_attack": CONFIG.get("ducking_attack", 10),
            "ducking_release": CONFIG.get("ducking_release", 300),
            "ducking_level_sc": CONFIG.get("ducking_level_sc", 2.0),
            "ducking_makeup": CONFIG.get("ducking_makeup", 1.2),
            "final_loudnorm": final_loudnorm_filter(),
            "loudness_mode": _loudness_mode(),
            "bgm_path": CONFIG.get("bgm_path", ""),
            "bgm_volume": CONFIG.get("bgm_volume", 0.18),
            "bgm_ducking_volume": CONFIG.get("bgm_ducking_volume", 0.10),
        },
    }
    if burn_subtitles:
        fingerprint["subtitle_renderer"] = "ass"
        fingerprint["subtitle_style"] = _subtitle_style_config()
    return fingerprint
