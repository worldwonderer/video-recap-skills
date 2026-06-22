"""Material and segment builders for the milestone-1 JianYing exporter."""

import json
import os

from jianying_schema import us, validate_material_category
from jianying_tracks import RI_BGM, RI_NARRATION, RI_TEXT, RI_VIDEO


def timerange(start_us, dur_us):
    return {"start": int(start_us), "duration": int(dur_us)}


def speed_material(new_id):
    return {"id": new_id(), "speed": 1.0, "type": "speed", "mode": 0, "curve_speed": None}


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


def clip_default():
    return {
        "alpha": 1.0,
        "flip": {"horizontal": False, "vertical": False},
        "rotation": 0.0,
        "scale": {"x": 1.0, "y": 1.0},
        "transform": {"x": 0.0, "y": 0.0},
    }


def base_segment(material_id, target_start_us, target_dur_us, render_index, volume, keyframes, new_id):
    return {
        "enable_adjust": True,
        "enable_color_correct_adjust": False,
        "enable_color_curves": True,
        "enable_color_match_adjust": False,
        "enable_color_wheels": True,
        "enable_lut": True,
        "enable_smart_color_adjust": False,
        "last_nonzero_volume": 1.0,
        "reverse": False,
        "track_attribute": 0,
        "track_render_index": 0,
        "visible": True,
        "id": new_id(),
        "material_id": material_id,
        "target_timerange": timerange(target_start_us, target_dur_us),
        "common_keyframes": keyframes,
        "keyframe_refs": [],
        "speed": 1.0,
        "volume": round(float(volume), 4),
        "is_tone_modify": False,
        "render_index": render_index,
    }


def audio_segment_piece(material_id, target_start_us, target_dur_us, source_start_us,
                        source_dur_us, render_index, volume, keyframes, new_id):
    seg = base_segment(material_id, target_start_us, target_dur_us, render_index, volume, keyframes, new_id)
    seg["source_timerange"] = timerange(source_start_us, source_dur_us)
    seg["extra_material_refs"] = []
    seg["clip"] = None
    seg["hdr_settings"] = None
    return seg


def track(name, track_type, segments, new_id):
    return {
        "attribute": 0,
        "flag": 0,
        "id": new_id(),
        "is_default_name": False,
        "name": name,
        "segments": segments,
        "type": track_type,
    }


def unsupported_track_note(kind):
    info = validate_material_category(kind)
    if info["supported"]:
        return None
    return info.get("note")


def build_video_track(ctx, timeline_track):
    segs = []
    for clip in timeline_track.get("clips", []):
        ts, te = float(clip["timeline_start"]), float(clip["timeline_end"])
        ss, se = float(clip["source_start"]), float(clip["source_end"])
        path = clip["source_path"]
        src_dur_us, width, height = ctx.media_duration(path, us(se))
        mat_id = ctx.new_id()
        ctx.materials["videos"].append({
            "audio_fade": None,
            "category_id": "",
            "category_name": "local",
            "check_flag": 63487,
            "crop": {
                "upper_left_x": 0.0,
                "upper_left_y": 0.0,
                "upper_right_x": 1.0,
                "upper_right_y": 0.0,
                "lower_left_x": 0.0,
                "lower_left_y": 1.0,
                "lower_right_x": 1.0,
                "lower_right_y": 1.0,
            },
            "crop_ratio": "free",
            "crop_scale": 1.0,
            "duration": int(src_dur_us),
            "height": height or ctx.height,
            "id": mat_id,
            "local_material_id": mat_id,
            "material_id": mat_id,
            "material_name": os.path.basename(path),
            "media_path": "",
            "path": path,
            "type": "video",
            "width": width or ctx.width,
        })
        speed = speed_material(ctx.new_id)
        ctx.materials["speeds"].append(speed)
        audio = clip.get("audio", {})
        keyframes = volume_keyframes(audio.get("volume_keyframes"), ts, ctx.new_id)
        volume = audio.get("base_gain", 1.0) if not keyframes else 1.0
        seg = base_segment(mat_id, us(ts), us(te - ts), RI_VIDEO, volume, keyframes, ctx.new_id)
        seg["source_timerange"] = timerange(us(ss), us(se - ss))
        seg["extra_material_refs"] = [speed["id"]]
        seg["clip"] = clip_default()
        seg["uniform_scale"] = {"on": True, "value": 1.0}
        seg["hdr_settings"] = {"intensity": 1.0, "mode": 1, "nits": 1000}
        segs.append(seg)
    ctx.tracks.append(track("video", "video", segs, ctx.new_id))


def build_audio_track(ctx, timeline_track):
    role = timeline_track.get("role", timeline_track.get("name", "audio"))
    render_index = RI_BGM if role == "bgm" else RI_NARRATION
    segs = []
    for segment in timeline_track.get("segments", []):
        ts, te = float(segment["timeline_start"]), float(segment["timeline_end"])
        path = segment["source_path"]
        mat_dur_us, _width, _height = ctx.media_duration(path, us(te - ts))
        want_us = us(te - ts)
        place_us = want_us
        if mat_dur_us and want_us > mat_dur_us:
            if role == "bgm" and timeline_track.get("loop"):
                place_us = want_us
            else:
                place_us = mat_dur_us
            if role == "bgm" and not timeline_track.get("loop"):
                ctx.note(
                    f"BGM 素材({mat_dur_us/1e6:.1f}s) 短于时间线({(te - ts):.1f}s)，"
                    "剪映中未循环铺满（可在剪映里手动复制延长）"
                )
        mat_id = ctx.new_id()
        ctx.materials["audios"].append({
            "app_id": 0,
            "category_id": "",
            "category_name": "local",
            "check_flag": 3,
            "copyright_limit_type": "none",
            "duration": int(mat_dur_us or want_us),
            "effect_id": "",
            "formula_id": "",
            "id": mat_id,
            "local_material_id": mat_id,
            "music_id": mat_id,
            "name": os.path.basename(path),
            "path": path,
            "source_platform": 0,
            "type": "extract_music",
            "wave_points": [],
        })
        keyframes = volume_keyframes(segment.get("volume_keyframes"), ts, ctx.new_id)
        volume = segment.get("gain", 1.0) if not keyframes else 1.0
        if role == "bgm" and timeline_track.get("loop") and mat_dur_us and want_us > mat_dur_us:
            cursor = 0
            while cursor < want_us:
                piece = min(mat_dur_us, want_us - cursor)
                if piece <= 0:
                    break
                piece_start_s = ts + (cursor / 1_000_000)
                piece_end_s = ts + ((cursor + piece) / 1_000_000)
                speed = speed_material(ctx.new_id)
                ctx.materials["speeds"].append(speed)
                piece_kfs = windowed_volume_keyframes(
                    segment.get("volume_keyframes"), piece_start_s, piece_end_s,
                    segment.get("gain", 1.0), ctx.new_id)
                piece_volume = segment.get("gain", 1.0) if not piece_kfs else 1.0
                piece_seg = audio_segment_piece(
                    mat_id, us(ts) + cursor, piece, 0, piece, render_index,
                    piece_volume, piece_kfs, ctx.new_id)
                piece_seg["extra_material_refs"] = [speed["id"]]
                segs.append(piece_seg)
                cursor += piece
        else:
            speed = speed_material(ctx.new_id)
            ctx.materials["speeds"].append(speed)
            audio_seg = audio_segment_piece(
                mat_id, us(ts), place_us, 0, place_us, render_index, volume, keyframes, ctx.new_id)
            audio_seg["extra_material_refs"] = [speed["id"]]
            segs.append(audio_seg)
    ctx.tracks.append(track(role, "audio", segs, ctx.new_id))


def build_text_track(ctx, timeline_track):
    segs = []
    for segment in timeline_track.get("segments", []):
        ts, te = float(segment["timeline_start"]), float(segment["timeline_end"])
        text = segment.get("text", "")
        mat_id = ctx.new_id()
        content = {"text": text, "styles": [{
            "fill": {
                "alpha": 1.0,
                "content": {"render_type": "solid", "solid": {"alpha": 1.0, "color": [1.0, 1.0, 1.0]}},
            },
            "range": [0, len(text.encode("utf-16-le")) // 2],
            "size": 8.0,
            "bold": False,
            "italic": False,
            "underline": False,
            "strokes": [],
        }]}
        ctx.materials["texts"].append({
            "id": mat_id,
            "content": json.dumps(content, ensure_ascii=False),
            "type": "text",
            "typesetting": 0,
            "alignment": 1,
            "letter_spacing": 0.0,
            "line_spacing": 0.02,
            "font_size": 8.0,
            "text_color": "#FFFFFF",
            "add_type": 0,
            "check_flag": 7,
        })
        seg = base_segment(mat_id, us(ts), us(te - ts), RI_TEXT, 1.0, [], ctx.new_id)
        seg["source_timerange"] = None
        seg["extra_material_refs"] = []
        seg["clip"] = clip_default()
        seg["uniform_scale"] = {"on": True, "value": 1.0}
        seg["type"] = "text_effect"
        seg["source_platform"] = 1
        seg["clip"]["transform"]["y"] = -0.72
        segs.append(seg)
    ctx.tracks.append(track("subtitle", "text", segs, ctx.new_id))


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
    else:
        note = unsupported_track_note(kind)
        if note:
            ctx.note(note)
