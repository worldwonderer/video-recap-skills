"""Schema constants, skeleton factories, and capability registries for JianYing export.

This module is intentionally data-oriented: it owns draft version metadata, the
full `materials` parallel-array shape, and the duo-video-inspired distinction
between material categories and cross-cutting exporter capabilities.
"""

DRAFT_VERSION = 360000
NEW_VERSION = "111.0.0"
APP = {"app_id": 3704, "app_source": "lv", "app_version": "5.9.5-beta1", "os": "mac"}

# The full 剪映 materials object: ~45 parallel arrays, only a few are populated
# by the milestone-1 exporter. Keeping every array preserves draft compatibility.
MATERIAL_KEYS = (
    "ai_translates audio_balances audio_effects audio_fades audio_track_indexes audios "
    "beats canvases chromas color_curves digital_humans drafts effects flowers green_screens "
    "handwrites hsl images log_color_wheels loudnesses manual_deformations masks common_mask "
    "material_animations material_colors multi_language_refs placeholders plugin_effects "
    "primary_color_wheels realtime_denoises shapes smart_crops smart_relights "
    "sound_channel_mappings speeds stickers tail_leaders text_templates texts time_marks "
    "transitions video_effects video_trackings videos vocal_beautifys vocal_separations"
).split()


def us(seconds):
    """Seconds (float) -> integer microseconds. The single seconds->µs boundary."""
    return int(round(float(seconds) * 1_000_000))


def full_materials(filled):
    """Return a complete JianYing `materials` object with all known arrays."""
    out = {k: [] for k in MATERIAL_KEYS}
    out.update(filled)
    return out


def draft_content_skeleton(draft_id, width, height, fps, total_us, materials, tracks):
    """Build the root `draft_content.json` / `draft_info.json` skeleton."""
    return {
        "canvas_config": {"width": width, "height": height, "ratio": "original"},
        "color_space": 0,
        "config": {
            "adjust_max_index": 1,
            "attachment_info": [],
            "combination_max_index": 1,
            "export_range": None,
            "extract_audio_last_index": 1,
            "lyrics_recognition_id": "",
            "lyrics_sync": True,
            "lyrics_taskinfo": [],
            "maintrack_adsorb": True,
            "material_save_mode": 0,
            "multi_language_current": "none",
            "multi_language_list": [],
            "multi_language_main": "none",
            "multi_language_mode": "none",
            "original_sound_last_index": 1,
            "record_audio_last_index": 1,
            "sticker_max_index": 1,
            "subtitle_keywords_config": None,
            "subtitle_recognition_id": "",
            "subtitle_sync": True,
            "subtitle_taskinfo": [],
            "system_font_list": [],
            "video_mute": False,
            "zoom_info_params": None,
        },
        "cover": None,
        "create_time": 0,
        "duration": int(total_us),
        "extra_info": None,
        "fps": fps,
        "free_render_index_mode_on": False,
        "group_container": None,
        "id": draft_id,
        "keyframe_graph_list": [],
        "keyframes": {
            "adjusts": [],
            "audios": [],
            "effects": [],
            "filters": [],
            "handwrites": [],
            "stickers": [],
            "texts": [],
            "videos": [],
        },
        "last_modified_platform": dict(APP),
        "platform": dict(APP),
        "materials": full_materials(materials),
        "mutable_config": None,
        "name": "",
        "new_version": NEW_VERSION,
        "relationships": [],
        "render_index_track_mode_on": False,
        "retouch_cover": None,
        "source": "default",
        "static_cover_image_path": "",
        "time_marks": None,
        "tracks": tracks,
        "update_time": 0,
        "version": DRAFT_VERSION,
    }


def meta_info(draft_id, total_us):
    """Build the companion `draft_meta_info.json` skeleton."""
    return {
        "cloud_package_completed_time": "",
        "draft_cloud_capcut_purchase_info": "",
        "draft_cloud_last_action_download": False,
        "draft_cloud_materials": [],
        "draft_cloud_purchase_info": "",
        "draft_cloud_template_id": "",
        "draft_cloud_tutorial_info": "",
        "draft_cloud_videocut_purchase_info": "",
        "draft_cover": "",
        "draft_deeplink_url": "",
        "draft_enterprise_info": {
            "draft_enterprise_extra": "",
            "draft_enterprise_id": "",
            "draft_enterprise_name": "",
            "enterprise_material": [],
        },
        "draft_fold_path": "",
        "draft_id": draft_id,
        "draft_is_ai_packaging_used": False,
        "draft_is_ai_shorts": False,
        "draft_is_ai_translate": False,
        "draft_is_article_video_draft": False,
        "draft_is_from_deeplink": "false",
        "draft_is_invisible": False,
        "draft_materials": [{"type": t, "value": []} for t in (0, 1, 2, 3, 6, 7, 8)],
        "draft_materials_copied_info": [],
        "draft_name": "",
        "draft_new_version": "",
        "draft_removable_storage_device": "",
        "draft_root_path": "",
        "draft_segment_extra_info": [],
        "draft_type": "",
        "tm_draft_cloud_completed": "",
        "tm_draft_cloud_modified": 0,
        "tm_draft_removed": 0,
        "tm_duration": int(total_us),
    }


def material_category_registry():
    """Material category support table inspired by duo-video's MaterialTypeEnum.

    Keep this separate from feature capabilities: keyframes, bundling, and path
    rewrite are exporter capabilities, not material categories.
    """
    return {
        "video": {"status": "supported", "materials_key": "videos", "track_type": "video"},
        "audio": {"status": "supported", "materials_key": "audios", "track_type": "audio"},
        "text": {"status": "supported", "materials_key": "texts", "track_type": "text"},
        "subtitle": {"status": "supported", "materials_key": "texts", "track_type": "text"},
        "speed": {"status": "supported_auxiliary", "materials_key": "speeds", "track_type": None},
        "image": {"status": "reserved", "materials_key": "images", "track_type": "video"},
        "sticker": {"status": "reserved", "materials_key": "stickers", "track_type": "sticker"},
        "sound": {"status": "reserved", "materials_key": "audios", "track_type": "audio"},
        "text_template": {"status": "reserved", "materials_key": "text_templates", "track_type": "text"},
        "lut": {"status": "reserved", "materials_key": "effects", "track_type": "video"},
        "transition": {"status": "reserved", "materials_key": "transitions", "track_type": "video"},
        "video_effect": {"status": "reserved", "materials_key": "video_effects", "track_type": "video"},
        "face_effect": {"status": "reserved", "materials_key": "effects", "track_type": "video"},
        "mask": {"status": "reserved", "materials_key": "masks", "track_type": "video"},
        "style": {"status": "reserved", "materials_key": "material_colors", "track_type": "video"},
    }


def feature_capabilities():
    """Cross-cutting exporter capabilities, deliberately not material categories."""
    return {
        "volume_automation": {
            "status": "supported",
            "property_type": "KFTypeVolume",
            "description": "timeline-absolute gain points become segment-relative JianYing keyframes",
        },
        "bgm_loop_splitting": {
            "status": "supported",
            "description": "looped BGM is split into repeated JianYing audio segments",
        },
        "media_bundling": {
            "status": "supported",
            "description": "referenced media can be copied into the draft materials folder",
        },
        "path_rewrite": {
            "status": "supported",
            "description": "bundled media paths are rewritten after atomic move",
        },
        "collision_safe_write": {
            "status": "supported",
            "description": "non-empty draft folders are never overwritten",
        },
        "lazy_export_isolation": {
            "status": "supported",
            "description": "assemble imports JianYing modules only when export is requested",
        },
    }


def validate_material_category(category):
    """Return deterministic support metadata for a public or future material kind."""
    normalized = (category or "").strip().lower()
    registry = material_category_registry()
    info = dict(registry.get(normalized, {"status": "unsupported", "materials_key": None, "track_type": None}))
    info["category"] = normalized
    info["supported"] = info["status"] in {"supported", "supported_auxiliary"}
    if not info["supported"]:
        status = "保留但暂未实现" if info["status"] == "reserved" else "未知"
        info["note"] = f"暂不支持的 JianYing material category: {normalized}（{status}），已跳过以避免生成无效草稿"
    return info
