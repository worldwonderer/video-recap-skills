"""Production material and segment builders for the JianYing exporter."""

import json
import os
from copy import deepcopy

from jianying_schema import scrub_platform_identity, us, validate_material_category
from jianying_templates import template
from jianying_tracks import SEGMENT_RENDER_INDEX


def timerange(start_us, dur_us):
    return {"start": int(start_us), "duration": int(dur_us)}


def speed_material(new_id, speed=1.0):
    return {
        "id": new_id(),
        "speed": float(speed),
        "type": "speed",
        "mode": 0,
        "curve_speed": None,
    }


def volume_keyframes(keyframes, seg_start_s, new_id):
    """Build one KFTypeVolume keyframe list from timeline-absolute points."""
    if not keyframes:
        return []
    kfs = []
    for kf in keyframes:
        kfs.append({
            "curveType": "Line",
            "graphID": "",
            "left_control": {"x": 0.0, "y": 0.0},
            "right_control": {"x": 0.0, "y": 0.0},
            "id": new_id(),
            "time_offset": max(0, us(kf["t"] - seg_start_s)),
            "values": [round(float(kf["gain"]), 4)],
        })
    return [{
        "id": new_id(),
        "keyframe_list": kfs,
        "material_id": "",
        "property_type": "KFTypeVolume",
    }]


def windowed_volume_keyframes(keyframes, seg_start_s, seg_end_s, default_gain, new_id):
    """Window timeline-absolute keyframes for one split/looped segment."""
    if not keyframes:
        return []
    start = float(seg_start_s)
    end = float(seg_end_s)
    default_gain = float(default_gain)
    ordered = sorted((
        {"t": float(kf["t"]), "gain": float(kf["gain"])}
        for kf in keyframes
        if "t" in kf and "gain" in kf
    ), key=lambda kf: kf["t"])
    if not ordered or end <= start:
        return []

    start_gain = default_gain
    for kf in ordered:
        if kf["t"] <= start:
            start_gain = kf["gain"]
        else:
            break
    inner = [kf for kf in ordered if start <= kf["t"] <= end]
    if not inner and abs(start_gain - default_gain) < 1e-4:
        return []

    selected = [{"t": start, "gain": start_gain}]
    for kf in inner:
        if abs(kf["t"] - start) < 1e-4:
            selected[-1] = {"t": start, "gain": kf["gain"]}
        else:
            selected.append(kf)
    if all(abs(kf["gain"] - default_gain) < 1e-4 for kf in selected):
        return []
    return volume_keyframes(selected, start, new_id)


def clip_from_segment(segment=None):
    segment = segment or {}
    scale = segment.get("scale") if isinstance(segment.get("scale"), dict) else {}
    position = segment.get("position") if isinstance(segment.get("position"), dict) else {}
    flip = segment.get("flip") if isinstance(segment.get("flip"), dict) else {}
    return {
        "alpha": round(float(segment.get("opacity", 1.0)), 4),
        "flip": {
            "horizontal": bool(flip.get("horizontal", False)),
            "vertical": bool(flip.get("vertical", False)),
        },
        "rotation": float(segment.get("rotation_degrees", 0.0)),
        "scale": {
            "x": float(scale.get("x", 1.0)),
            "y": float(scale.get("y", 1.0)),
        },
        # Timeline v2 uses JianYing's normalized, canvas-center, Y-up coordinates.
        "transform": {
            "x": float(position.get("x", 0.0)),
            "y": float(position.get("y", 0.0)),
        },
    }


def base_segment(material_id, target_start_us, target_dur_us, volume, keyframes, new_id):
    segment = template("segment")
    segment.update({
        "id": new_id(),
        "material_id": material_id,
        "target_timerange": timerange(target_start_us, target_dur_us),
        "common_keyframes": keyframes,
        "track_render_index": SEGMENT_RENDER_INDEX,
        "render_index": SEGMENT_RENDER_INDEX,
        "volume": round(float(volume), 4),
    })
    return segment


def audio_segment_piece(material_id, target_start_us, target_dur_us, source_start_us,
                        source_dur_us, volume, keyframes, new_id):
    seg = base_segment(material_id, target_start_us, target_dur_us, volume, keyframes, new_id)
    seg["source_timerange"] = timerange(source_start_us, source_dur_us)
    seg["extra_material_refs"] = []
    return seg


def _hex_rgb(color):
    value = str(color or "#FFFFFF").lstrip("#")
    if len(value) == 8:
        value = value[:6]
    if len(value) != 6:
        raise ValueError(f"invalid text color: {color!r}")
    try:
        return [round(int(value[index:index + 2], 16) / 255.0, 6) for index in (0, 2, 4)]
    except ValueError as exc:
        raise ValueError(f"invalid text color: {color!r}") from exc


def _text_style(style, start, end):
    fill_color = style.get("fill_color", "#FFFFFF")
    authored = template("text_style")["styles"][0]
    authored.update({
        "fill": {
            "alpha": 1.0,
            "content": {
                "render_type": "solid",
                "solid": {"alpha": 1.0, "color": _hex_rgb(fill_color)},
            },
        },
        "range": [int(start), int(end)],
        "size": float(style.get("font_size", 8.0)),
        "bold": bool(style.get("bold", False)),
        "italic": bool(style.get("italic", False)),
        "underline": bool(style.get("underline", False)),
        "strokes": list(style.get("strokes", [])),
    })
    if style.get("font_path") or style.get("font_id"):
        authored["font"] = {
            "id": str(style.get("font_id", "")),
            "path": str(style.get("font_path", "")),
        }
    if style.get("stroke_color") and float(style.get("stroke_width", 0)) > 0:
        width = float(style["stroke_width"])
        authored["strokes"] = [{
            "alpha": 1.0,
            "content": {
                "render_type": "solid",
                "solid": {"alpha": 1.0, "color": _hex_rgb(style["stroke_color"])},
            },
            "width": round(0.00196 * (width ** 1.013), 6),
        }]
    if style.get("shadow_color"):
        authored["shadows"] = [{
            "alpha": float(style.get("shadow_opacity", 90)) / 100.0,
            "angle": int(style.get("shadow_angle", -45)),
            "distance": int(style.get("shadow_width", 5)),
            "feather": float(style.get("shadow_vague", 45)) / 100.0,
            "content": {
                "render_type": "solid",
                "solid": {"alpha": 1.0, "color": _hex_rgb(style["shadow_color"])},
            },
        }]
    if isinstance(style.get("effect_style"), dict):
        authored["effect_style"] = deepcopy(style["effect_style"])
    authored["use_letter_color"] = True
    return authored


def rich_text_content(text, base_style=None, words=None, style_presets=None):
    """Build duo-video text styles using UTF-16 code-unit ranges."""
    base_style = dict(base_style or {})
    length = len(text.encode("utf-16-le")) // 2
    style_presets = style_presets or {}
    normalized_words = []
    boundaries = {0, length}
    for word in words or []:
        start = max(0, min(length, int(word.get("index", 0))))
        end = max(start, min(length, start + int(word.get("length", 0))))
        if end <= start:
            continue
        word_style = dict(style_presets.get(str(word.get("style_id")), {}))
        word_style.update(word)
        normalized_words.append((start, end, word_style))
        boundaries.update((start, end))

    styles = []
    points = sorted(boundaries)
    for start, end in zip(points, points[1:]):
        style = dict(base_style)
        for word_start, word_end, word in normalized_words:
            if word_start <= start and end <= word_end:
                style.update({key: value for key, value in word.items() if key not in {"index", "length"}})
        styles.append(_text_style(style, start, end))
    content = template("text_style")
    content["text"] = text
    content["styles"] = styles
    return content


def unsupported_track_note(kind):
    info = validate_material_category(kind)
    if info["supported"]:
        return None
    return info.get("note")


RESOURCE_TRACKS = {
    "sound": ("audios", "audio"),
    "sticker": ("stickers", "sticker"),
    "text_template": ("text_templates", "text"),
    "video_effect": ("video_effects", "effect"),
    "face_effect": ("video_effects", "effect"),
}


def _resource_value(config, snake_case, camel_case, default=None):
    if snake_case in config:
        return config[snake_case]
    return config.get(camel_case, default)


def _load_resource_config(config, kind):
    if isinstance(config, str):
        with open(config, encoding="utf-8") as source:
            config = json.load(source)
    if not isinstance(config, dict):
        raise ValueError(f"{kind} resource_config must be an object")
    return config


def _resource_material(segment, kind, new_id):
    raw = segment.get("material")
    config = segment.get("resource_config")
    if raw is not None and config is not None:
        raise ValueError(f"{kind} segment must use either material or resource_config, not both")
    if config is not None:
        config = _load_resource_config(config, kind)
        raw = _resource_value(config, "main_config", "mainConfig")
        if isinstance(raw, str):
            raw = json.loads(raw)
        if not isinstance(raw, dict):
            raise ValueError(f"{kind} resource_config.main_config must be an object")
        raw = deepcopy(raw)
        resource_id = _resource_value(config, "resource_id", "resourceId")
        if resource_id is not None:
            raw.setdefault("resource_id", resource_id)
        if config.get("resources"):
            raw["_bundle_resources"] = deepcopy(config["resources"])
        cover_img = _resource_value(config, "cover_img", "coverImg")
        if kind == "sticker" and cover_img:
            raw["icon_url"] = cover_img
            raw["preview_cover_url"] = cover_img
    elif not isinstance(raw, dict):
        raise ValueError(f"{kind} segment requires material or resource_config")
    else:
        raw = deepcopy(raw)
    raw["id"] = new_id()
    return raw


def build_resource_track(ctx, timeline_track):
    kind = timeline_track["kind"]
    materials_key, _track_type = RESOURCE_TRACKS[kind]
    track_name = timeline_track.get("name", kind)
    for item in timeline_track.get("segments", []):
        ts, te = float(item["timeline_start"]), float(item["timeline_end"])
        duration_us = us(te - ts)
        authored_item = item
        package_name = item.get("resource_package")
        if package_name is not None:
            package = ctx.resource_packages.get(str(package_name))
            if not isinstance(package, dict):
                raise ValueError(f"unknown JianYing resource package: {package_name}")
            authored_item = dict(item)
            authored_item["resource_config"] = package
        material = _resource_material(authored_item, kind, ctx.new_id)
        ctx.materials[materials_key].append(material)
        config = authored_item.get("resource_config") or {}
        if config:
            config = _load_resource_config(config, kind)
        if kind == "text_template":
            subordinate_resources = deepcopy(material.get("_bundle_resources", []))
            subordinate_texts = deepcopy(config.get("texts", []))
            subordinate_effects = deepcopy(config.get("effects", []))
            if subordinate_resources:
                for subordinate in subordinate_texts + subordinate_effects:
                    if isinstance(subordinate, dict):
                        subordinate["_bundle_resources"] = deepcopy(subordinate_resources)
            ctx.materials["texts"].extend(subordinate_texts)
            ctx.materials["effects"].extend(subordinate_effects)
        seg = base_segment(material["id"], us(ts), duration_us, 1.0, [], ctx.new_id)
        speed_value = float(item.get("speed", 1.0))
        if speed_value <= 0:
            raise ValueError(f"{kind} segment speed must be greater than 0")
        seg["source_timerange"] = timerange(0, round(duration_us * speed_value))
        seg["track_render_index"] = 0
        seg["extra_material_refs"] = []
        seg["speed"] = speed_value
        if abs(speed_value - 1.0) > 1e-9:
            speed = speed_material(ctx.new_id, speed_value)
            ctx.materials["speeds"].append(speed)
            seg["extra_material_refs"].append(speed["id"])
        seg["clip"] = clip_from_segment(item)
        ctx.add_segment(kind, track_name, us(ts), duration_us, seg)


def _attachment_material(spec, kind, new_id):
    if not isinstance(spec, dict):
        raise ValueError(f"video {kind} must be an object")
    material = deepcopy(spec)
    config = material.pop("main_config", material.pop("mainConfig", None))
    resources = material.pop("resources", None)
    resource_id = material.pop("resourceId", None)
    if config is not None:
        if isinstance(config, str):
            config = json.loads(config)
        if not isinstance(config, dict):
            raise ValueError(f"video {kind}.main_config must be an object")
        merged = deepcopy(config)
        merged.update(material)
        material = merged
    if resource_id is not None:
        material.setdefault("resource_id", resource_id)
    if resources:
        material["_bundle_resources"] = deepcopy(resources)
    material["id"] = new_id()
    return material


def _resolve_attachment_spec(ctx, spec, kind):
    if isinstance(spec, str):
        package = ctx.resource_packages.get(spec)
        if not isinstance(package, dict):
            raise ValueError(f"unknown JianYing {kind} resource package: {spec}")
        return package
    return spec


def apply_video_attachments(ctx, clip, segment):
    """Attach duo-video transition, mask, and LUT authoring data."""
    semantic_kind = "video"
    transition_spec = clip.get("transition")
    if transition_spec is not None:
        transition_spec = _resolve_attachment_spec(ctx, transition_spec, "transition")
        transition = _attachment_material(transition_spec, "transition", ctx.new_id)
        ctx.materials["transitions"].append(transition)
        segment["extra_material_refs"].append(transition["id"])

    mask_spec = clip.get("mask")
    if mask_spec is not None:
        mask_spec = _resolve_attachment_spec(ctx, mask_spec, "mask")
        mask = _attachment_material(mask_spec, "mask", ctx.new_id)
        ctx.materials["masks"].append(mask)
        ctx.materials["common_mask"].append(deepcopy(mask))
        segment["extra_material_refs"].append(mask["id"])
        semantic_kind = "mask"

    lut_spec = clip.get("lut")
    if lut_spec is not None:
        lut_spec = _resolve_attachment_spec(ctx, lut_spec, "lut")
        lut = _attachment_material(lut_spec, "lut", ctx.new_id)
        lut["value"] = float(lut.pop("strength", 100)) / 100.0
        lut.pop("skin_tone_correction", None)
        ctx.materials["effects"].append(lut)
        segment["extra_material_refs"].append(lut["id"])
        if lut_spec.get("skin_tone_correction") is not None:
            lumi_hub_path = str(lut.get("lumi_hub_path") or "")
            if not lumi_hub_path:
                raise ValueError(
                    "LUT skin_tone_correction requires an offline effect "
                    "main_config with lumi_hub_path"
                )
            effect_path = lumi_hub_path.rsplit("/", 1)[0]
            skin_tone = deepcopy(lut)
            skin_tone["id"] = ctx.new_id()
            skin_tone["type"] = "skin_tone_correction"
            skin_tone["version"] = "v3"
            skin_tone["value"] = float(lut_spec["skin_tone_correction"]) / 100.0
            skin_tone["path"] = effect_path
            skin_tone["lumi_hub_path"] = effect_path
            ctx.materials["effects"].append(skin_tone)
            segment["extra_material_refs"].append(skin_tone["id"])
    return semantic_kind


def _track_object(ctx, name, track_type, segments, flag):
    return {
        "attribute": 0,
        "flag": flag,
        "id": ctx.new_id(),
        "is_default_name": True,
        "name": name,
        "segments": segments,
        "type": track_type,
    }


def build_compound_video(ctx, clip, material, foreground_segment, track_name, ts, te):
    """Build duo-video's nested green-screen/compound draft structure."""
    background_spec = clip.get("green_background")
    chroma_spec = _resolve_attachment_spec(ctx, clip.get("chroma"), "chroma")
    if not isinstance(background_spec, dict) or not isinstance(chroma_spec, dict):
        raise ValueError("compound green-screen clips require green_background and chroma objects")

    duration_us = us(te - ts)
    full_material_duration_us = int(material.get("duration") or duration_us)
    outer_source_timerange = deepcopy(foreground_segment["source_timerange"])
    outer_target_timerange = deepcopy(foreground_segment["target_timerange"])
    background_path = background_spec.get("source_path")
    if not isinstance(background_path, str) or not background_path:
        raise ValueError("green_background.source_path must be a non-empty string")

    background_id = ctx.new_id()
    background = template("video")
    background.update({
        "duration": full_material_duration_us,
        "height": int(background_spec.get("height") or ctx.height),
        "id": background_id,
        "material_name": os.path.basename(background_path),
        "path": background_path,
        "type": background_spec.get("type", "photo"),
        "width": int(background_spec.get("width") or ctx.width),
    })

    chroma = _attachment_material(chroma_spec, "chroma", ctx.new_id)
    foreground_segment = deepcopy(foreground_segment)
    foreground_segment["source_timerange"] = timerange(0, full_material_duration_us)
    foreground_segment["target_timerange"] = timerange(0, full_material_duration_us)
    foreground_segment["extra_material_refs"].append(chroma["id"])

    background_segment = base_segment(
        background_id, 0, full_material_duration_us, 1.0, [], ctx.new_id
    )
    background_segment["source_timerange"] = timerange(0, full_material_duration_us)
    background_segment["render_index"] = 1
    background_segment["track_render_index"] = 1
    background_segment["clip"] = clip_from_segment(background_spec)

    draft = template("draft")
    draft["id"] = ctx.new_id()
    draft["combination_id"] = ctx.new_id()
    nested = draft["draft"]
    nested["id"] = ctx.new_id()
    nested["canvas_config"] = {
        "width": ctx.width,
        "height": ctx.height,
        "ratio": "original",
    }
    nested["duration"] = full_material_duration_us
    nested["fps"] = float(ctx.fps)
    scrub_platform_identity(nested)
    nested["materials"]["videos"] = [material, background]
    nested["materials"]["chromas"] = [chroma]
    referenced = set(foreground_segment["extra_material_refs"])
    nested_speeds = [
        item for item in ctx.materials["speeds"] if item.get("id") in referenced
    ]
    if nested_speeds:
        nested["materials"]["speeds"] = nested_speeds
        ctx.materials["speeds"] = [
            item for item in ctx.materials["speeds"] if item.get("id") not in referenced
        ]
    nested["tracks"] = [
        _track_object(ctx, "green_background", "video", [background_segment], 0),
        _track_object(ctx, "video", "video", [foreground_segment], 2),
    ]
    ctx.materials["drafts"].append(draft)

    combination_material = template("combination_video")
    combination_material.update({
        "duration": full_material_duration_us,
        "height": ctx.height,
        "id": ctx.new_id(),
        "width": ctx.width,
    })
    ctx.materials["videos"].append(combination_material)

    combination_segment = template("combination_segment")
    combination_segment.update({
        "id": ctx.new_id(),
        "material_id": combination_material["id"],
        "extra_material_refs": [draft["id"]],
        "source_timerange": outer_source_timerange,
        "target_timerange": outer_target_timerange,
    })
    semantic_kind = apply_video_attachments(ctx, clip, combination_segment)
    authored_track_name = "mask" if semantic_kind == "mask" else track_name
    ctx.add_segment(
        semantic_kind, authored_track_name, us(ts), duration_us, combination_segment
    )


def build_video_track(ctx, timeline_track):
    track_name = timeline_track.get("name", "video")
    for clip in timeline_track.get("clips", []):
        ts, te = float(clip["timeline_start"]), float(clip["timeline_end"])
        ss, se = float(clip["source_start"]), float(clip["source_end"])
        path = clip["source_path"]
        speed_value = float(clip.get("speed", 1.0))
        if speed_value <= 0:
            raise ValueError("video clip speed must be greater than 0")
        reverse = bool(clip.get("reverse", False))
        if reverse:
            reverse_path = clip.get("reverse_path")
            if not isinstance(reverse_path, str) or not reverse_path:
                raise ValueError("reverse video clips require a local reverse_path")
            path = reverse_path
        src_dur_us, width, height = ctx.media_duration(path, us(se))
        mat_id = ctx.new_id()
        material = template("video")
        material.update({
            "duration": int(src_dur_us),
            "height": height or ctx.height,
            "id": mat_id,
            "material_name": os.path.basename(path),
            "path": path,
            "width": width or ctx.width,
        })
        audio = clip.get("audio", {})
        keyframes = volume_keyframes(audio.get("volume_keyframes"), ts, ctx.new_id)
        volume = audio.get("base_gain", 1.0) if not keyframes else 1.0
        seg = base_segment(mat_id, us(ts), us(te - ts), volume, keyframes, ctx.new_id)
        source_start = ss
        if reverse:
            source_start = max(0.0, (src_dur_us / 1_000_000) - se)
        seg["source_timerange"] = timerange(us(source_start), us(se - ss))
        seg["speed"] = speed_value
        seg["extra_material_refs"] = []
        if abs(speed_value - 1.0) > 1e-9:
            speed = speed_material(ctx.new_id, speed_value)
            ctx.materials["speeds"].append(speed)
            seg["extra_material_refs"].append(speed["id"])
        seg["clip"] = clip_from_segment(clip)
        if clip.get("compound") or clip.get("green_background") or clip.get("chroma"):
            build_compound_video(ctx, clip, material, seg, track_name, ts, te)
            continue
        ctx.materials["videos"].append(material)
        semantic_kind = apply_video_attachments(ctx, clip, seg)
        authored_track_name = "mask" if semantic_kind == "mask" else track_name
        ctx.add_segment(semantic_kind, authored_track_name, us(ts), us(te - ts), seg)


def build_audio_track(ctx, timeline_track):
    role = timeline_track.get("role", timeline_track.get("name", "audio"))
    track_name = timeline_track.get("name", role)
    for segment in timeline_track.get("segments", []):
        ts, te = float(segment["timeline_start"]), float(segment["timeline_end"])
        path = segment["source_path"]
        mat_dur_us, _width, _height = ctx.media_duration(path, us(te - ts))
        want_us = us(te - ts)
        speed_value = float(segment.get("speed", 1.0))
        if speed_value <= 0:
            raise ValueError("audio segment speed must be greater than 0")
        place_us = want_us
        required_source_us = int(round(want_us * speed_value))
        if mat_dur_us and required_source_us > mat_dur_us:
            if role == "bgm" and timeline_track.get("loop"):
                place_us = want_us
            else:
                place_us = int(mat_dur_us / speed_value)
            if role == "bgm" and not timeline_track.get("loop"):
                ctx.note(
                    f"BGM 素材({mat_dur_us/1e6:.1f}s) 短于时间线({(te - ts):.1f}s)，"
                    "剪映中未循环铺满（可在剪映里手动复制延长）"
                )
        mat_id = ctx.new_id()
        material = template("audio")
        material.update({
            "duration": int(mat_dur_us or want_us),
            "id": mat_id,
            "path": path,
        })
        ctx.materials["audios"].append(material)
        keyframes = volume_keyframes(segment.get("volume_keyframes"), ts, ctx.new_id)
        volume = segment.get("gain", 1.0) if not keyframes else 1.0
        if role == "bgm" and timeline_track.get("loop") and mat_dur_us and required_source_us > mat_dur_us:
            cursor = 0
            while cursor < want_us:
                piece = min(int(mat_dur_us / speed_value), want_us - cursor)
                if piece <= 0:
                    break
                piece_start_s = ts + (cursor / 1_000_000)
                piece_end_s = ts + ((cursor + piece) / 1_000_000)
                piece_kfs = windowed_volume_keyframes(
                    segment.get("volume_keyframes"), piece_start_s, piece_end_s,
                    segment.get("gain", 1.0), ctx.new_id)
                piece_volume = segment.get("gain", 1.0) if not piece_kfs else 1.0
                piece_seg = audio_segment_piece(
                    mat_id, us(ts) + cursor, piece, 0, int(round(piece * speed_value)),
                    piece_volume, piece_kfs, ctx.new_id)
                piece_seg["speed"] = speed_value
                if abs(speed_value - 1.0) > 1e-9:
                    speed = speed_material(ctx.new_id, speed_value)
                    ctx.materials["speeds"].append(speed)
                    piece_seg["extra_material_refs"].append(speed["id"])
                ctx.add_segment("audio", track_name, us(ts) + cursor, piece, piece_seg)
                cursor += piece
        else:
            audio_seg = audio_segment_piece(
                mat_id, us(ts), place_us, 0, int(round(place_us * speed_value)),
                volume, keyframes, ctx.new_id)
            audio_seg["speed"] = speed_value
            if abs(speed_value - 1.0) > 1e-9:
                speed = speed_material(ctx.new_id, speed_value)
                ctx.materials["speeds"].append(speed)
                audio_seg["extra_material_refs"].append(speed["id"])
            ctx.add_segment("audio", track_name, us(ts), place_us, audio_seg)


def build_text_track(ctx, timeline_track):
    track_name = timeline_track.get("name", "text")
    track_kind = "subtitle" if track_name == "subtitle" else "text"
    for segment in timeline_track.get("segments", []):
        ts, te = float(segment["timeline_start"]), float(segment["timeline_end"])
        text = segment.get("text", "")
        mat_id = ctx.new_id()
        authored_style = dict(ctx.style_presets.get(str(segment.get("style_id")), {}))
        authored_style.update(segment.get("style") or {})
        content = rich_text_content(
            text, authored_style, segment.get("words"), ctx.style_presets
        )
        material = template("text")
        material.update({
            "id": mat_id,
            "content": json.dumps(content, ensure_ascii=False),
            "type": "subtitle" if track_kind == "subtitle" else "text",
            "alignment": int(authored_style.get("text_align", 1)),
            "font_size": float(authored_style.get("font_size", 8.0)),
            "text_color": authored_style.get("fill_color", "#FFFFFF"),
            "line_spacing": float(authored_style.get("line_spacing", 0.02)),
            "letter_spacing": float(authored_style.get("letter_spacing", 0.0)),
            "check_flag": 15,
        })
        bundle_resources = []
        for content_style in content["styles"]:
            font_path = content_style.get("font", {}).get("path")
            if font_path and not str(font_path).startswith(("Resources/", "##_draftpath_placeholder_")):
                bundle_resources.append({
                    "source_path": str(font_path),
                    "resource_kind": "fonts",
                })
            effect_path = content_style.get("effect_style", {}).get("path")
            if effect_path and not str(effect_path).startswith(("Resources/", "##_draftpath_placeholder_")):
                bundle_resources.append({
                    "source_path": str(effect_path),
                    "resource_kind": "effect",
                })
        if bundle_resources:
            material["_bundle_resources"] = bundle_resources
        if authored_style.get("background_color"):
            material.update({
                "background_color": authored_style["background_color"],
                "background_alpha": float(authored_style.get("background_opacity", 100)) / 100.0,
                "background_style": 1,
                "background_height": float(authored_style.get("background_height", 14)) / 100.0,
                "background_width": float(authored_style.get("background_width", 14)) / 100.0,
                "background_horizontal_offset": float(authored_style.get("background_offset_x", 50)) * 0.02 - 1,
                "background_vertical_offset": float(authored_style.get("background_offset_y", 50)) * 0.02 - 1,
                "background_round_radius": float(authored_style.get("background_radius", 6)) / 100.0,
                "check_flag": 31,
            })
        if authored_style.get("stroke_color") and float(authored_style.get("stroke_width", 0)) > 0:
            material["bold_width"] = round(
                0.00196 * (float(authored_style["stroke_width"]) ** 1.013), 6
            )
            material["border_color"] = authored_style["stroke_color"]
        if authored_style.get("shadow_color"):
            material.update({
                "has_shadow": True,
                "shadow_color": authored_style["shadow_color"],
                "shadow_alpha": float(authored_style.get("shadow_opacity", 90)) / 100.0,
                "shadow_angle": float(authored_style.get("shadow_angle", -45)),
                "shadow_distance": float(authored_style.get("shadow_width", 5)),
                "shadow_smoothing": float(authored_style.get("shadow_vague", 45)) / 100.0,
            })
        if authored_style or segment.get("words"):
            material["is_rich_text"] = True
        ctx.materials["texts"].append(material)
        duration_us = us(te - ts)
        seg = base_segment(mat_id, us(ts), duration_us, 1.0, [], ctx.new_id)
        seg["source_timerange"] = timerange(0, duration_us)
        seg["clip"] = clip_from_segment(segment)
        if track_kind == "subtitle" and "position" not in segment:
            seg["clip"]["transform"]["y"] = -0.72
        ctx.add_segment(track_kind, track_name, us(ts), duration_us, seg)


def build_image_track(ctx, timeline_track):
    """Build local image overlays as JianYing photo materials on video tracks."""
    track_name = timeline_track.get("name", "image")
    for segment in timeline_track.get("segments", []):
        ts, te = float(segment["timeline_start"]), float(segment["timeline_end"])
        duration_us = us(te - ts)
        path = segment["source_path"]
        _ignored_duration, width, height = ctx.media_duration(path, duration_us)
        mat_id = ctx.new_id()
        material = template("video")
        material.update({
            "duration": duration_us,
            "height": height or ctx.height,
            "id": mat_id,
            "material_name": os.path.basename(path),
            "path": path,
            "type": "photo",
            "width": width or ctx.width,
        })
        ctx.materials["videos"].append(material)
        seg = base_segment(mat_id, us(ts), duration_us, 1.0, [], ctx.new_id)
        speed_value = float(segment.get("speed", 1.0))
        if speed_value <= 0:
            raise ValueError("image segment speed must be greater than 0")
        seg["source_timerange"] = timerange(0, round(duration_us * speed_value))
        seg["clip"] = clip_from_segment(segment)
        seg["speed"] = speed_value
        if abs(speed_value - 1.0) > 1e-9:
            speed = speed_material(ctx.new_id, speed_value)
            ctx.materials["speeds"].append(speed)
            seg["extra_material_refs"].append(speed["id"])
        semantic_kind = apply_video_attachments(ctx, segment, seg)
        authored_track_name = "mask" if semantic_kind == "mask" else track_name
        if semantic_kind == "video":
            semantic_kind = "image"
        ctx.add_segment(semantic_kind, authored_track_name, us(ts), duration_us, seg)


def build_timeline_track(ctx, timeline_track):
    kind = timeline_track.get("kind")
    if kind in ("audio", "text") and not timeline_track.get("segments"):
        return
    if kind == "video":
        build_video_track(ctx, timeline_track)
    elif kind == "audio":
        build_audio_track(ctx, timeline_track)
    elif kind == "text":
        build_text_track(ctx, timeline_track)
    elif kind == "image":
        build_image_track(ctx, timeline_track)
    elif kind in RESOURCE_TRACKS:
        build_resource_track(ctx, timeline_track)
    else:
        note = unsupported_track_note(kind)
        if note:
            ctx.note(note)
