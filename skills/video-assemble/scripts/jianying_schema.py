"""Schema constants, skeleton factories, and material registry for JianYing export.

This module is intentionally data-oriented: it owns draft version metadata, the
full `materials` parallel-array shape, and the implemented material dispatch.
"""

from jianying_templates import template

# The full 剪映 materials object: ~45 parallel arrays. Only arrays backed by a
# production builder are populated; retaining the full shape preserves compatibility.
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


def scrub_platform_identity(project):
    """Remove hardware fingerprints carried by the pinned upstream templates."""
    for platform_key in ("last_modified_platform", "platform"):
        platform = project.get(platform_key)
        if not isinstance(platform, dict):
            continue
        for identity_key in ("device_id", "hard_disk_id", "mac_address"):
            platform[identity_key] = ""
    return project


def draft_content_skeleton(draft_id, width, height, fps, total_us, materials, tracks):
    """Build the root `draft_content.json` / `draft_info.json` skeleton."""
    content = template("project")
    scrub_platform_identity(content)
    content["canvas_config"] = {"width": width, "height": height, "ratio": "original"}
    content["duration"] = int(total_us)
    content["fps"] = float(fps)
    content["id"] = draft_id
    content["materials"] = full_materials(materials)
    content["tracks"] = tracks
    return content


def meta_info(draft_id, total_us):
    """Build the companion `draft_meta_info.json` skeleton."""
    meta = template("meta")
    meta["draft_id"] = draft_id
    meta["draft_timeline_materials_size_"] = 0
    meta["tm_duration"] = int(total_us)
    return meta


def material_category_registry():
    """Material category support table inspired by duo-video's MaterialTypeEnum.

    Keyframes, bundling, and path rewrite are exporter behavior, not material
    categories, so they intentionally do not appear here.
    """
    return {
        "video": {"status": "supported", "materials_key": "videos", "track_type": "video"},
        "audio": {"status": "supported", "materials_key": "audios", "track_type": "audio"},
        "text": {"status": "supported", "materials_key": "texts", "track_type": "text"},
        "subtitle": {"status": "supported", "materials_key": "texts", "track_type": "text"},
        "speed": {"status": "supported_auxiliary", "materials_key": "speeds", "track_type": None},
        # JianYing represents photos in materials.videos with material.type=photo.
        "image": {"status": "supported", "materials_key": "videos", "track_type": "video"},
        "sticker": {"status": "supported_offline_payload", "materials_key": "stickers", "track_type": "sticker"},
        "sound": {"status": "supported_offline_payload", "materials_key": "audios", "track_type": "audio"},
        "text_template": {"status": "supported_offline_payload", "materials_key": "text_templates", "track_type": "text"},
        "lut": {"status": "supported_offline_payload", "materials_key": "effects", "track_type": "video"},
        "transition": {"status": "supported_offline_payload", "materials_key": "transitions", "track_type": "video"},
        "video_effect": {"status": "supported_offline_payload", "materials_key": "video_effects", "track_type": "effect"},
        "face_effect": {"status": "supported_offline_payload", "materials_key": "video_effects", "track_type": "effect"},
        "mask": {"status": "supported_offline_payload", "materials_key": "masks", "track_type": "video"},
        "chroma": {"status": "supported_offline_payload", "materials_key": "chromas", "track_type": "video"},
        "green_screen": {"status": "supported", "materials_key": "drafts", "track_type": "video"},
        "compound": {"status": "supported", "materials_key": "drafts", "track_type": "video"},
        # duo-video style is authoring input rather than a direct material array.
        "style": {"status": "supported", "materials_key": None, "track_type": None},
    }


def validate_material_category(category):
    """Return deterministic support metadata for a public or future material kind."""
    normalized = (category or "").strip().lower()
    registry = material_category_registry()
    info = dict(registry.get(normalized, {"status": "unsupported", "materials_key": None, "track_type": None}))
    info["category"] = normalized
    info["supported"] = info["status"] in {
        "supported", "supported_auxiliary", "supported_offline_payload",
    }
    if not info["supported"]:
        status = "保留但暂未实现" if info["status"] == "reserved" else "未知"
        info["note"] = f"暂不支持的 JianYing material category: {normalized}（{status}），已跳过以避免生成无效草稿"
    return info
