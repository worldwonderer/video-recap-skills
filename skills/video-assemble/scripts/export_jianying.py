"""Optional 剪映 / JianYing (CapCut) draft exporter — decoupled, stdlib + ffprobe only.

Reads a backend-neutral `timeline.json` (see timeline.py) and writes a 剪映 draft
folder (`draft_content.json` + `draft_info.json` + `draft_meta_info.json`) that the
desktop app can open and the user can keep editing: video clips on the main track,
the narration and BGM as their own audio tracks, the recap lines as a subtitle
track, and the gap-fill ducking carried as native volume keyframes.

This module is **optional and self-contained**. The core ffmpeg render never
imports it; it is only loaded when an export is explicitly requested. Nothing in
the rest of the bundle depends on it, and 剪映 does not need to be installed to
produce (or test) a draft.

Schema and the draft skeleton are reimplemented from the open-source
pyJianYingDraft (© GuanYixuan, Apache-2.0) and capcut-mate (© Hommy, Apache-2.0);
see ACKNOWLEDGEMENTS / 致谢 in the README. No third-party code is vendored — the
draft JSON is built directly here so the bundle stays stdlib-only.
"""

import json
import os
import shutil
import subprocess
import tempfile
import uuid

DRAFT_VERSION = 360000
NEW_VERSION = "110.0.0"
APP = {"app_id": 3704, "app_source": "lv", "app_version": "5.9.0", "os": "mac"}

# render_index layering: main video at 0, audio tracks above it, subtitles on top.
RI_VIDEO = 0
RI_NARRATION = 1
RI_BGM = 2
RI_TEXT = 15000  # 剪映's text-track render_index band; keeps subtitles above all media


def _us(seconds):
    """Seconds (float) -> integer microseconds. The single seconds->µs boundary."""
    return int(round(float(seconds) * 1_000_000))


def _default_id():
    return str(uuid.uuid4()).upper()


def _probe_media(path):
    """Return (duration_us, width, height) via ffprobe; zeros on any failure."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-of", "json",
             "-show_entries", "format=duration:stream=width,height,codec_type", str(path)],
            capture_output=True, text=True, timeout=30)
        data = json.loads(r.stdout or "{}")
        dur_us = _us(float(data.get("format", {}).get("duration") or 0))
        w = h = 0
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                w, h = int(s.get("width") or 0), int(s.get("height") or 0)
                break
        return dur_us, w, h
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired):
        return 0, 0, 0


def _timerange(start_us, dur_us):
    return {"start": int(start_us), "duration": int(dur_us)}


def _speed_material(new_id):
    return {"id": new_id(), "speed": 1.0, "type": "speed", "mode": 0, "curve_speed": None}


def _volume_keyframes(keyframes, seg_start_s, new_id):
    """One KFTypeVolume keyframe list from timeline-absolute [{t,gain}] points,
    re-based to microseconds relative to the segment start. Empty list -> no entry."""
    if not keyframes:
        return []
    kfs = []
    for kf in keyframes:
        kfs.append({
            "curveType": "Line", "graphID": "",
            "left_control": {"x": 0.0, "y": 0.0},
            "right_control": {"x": 0.0, "y": 0.0},
            "id": new_id(),
            "time_offset": max(0, _us(kf["t"] - seg_start_s)),
            "values": [round(float(kf["gain"]), 4)],
        })
    return [{"id": new_id(), "keyframe_list": kfs, "material_id": "",
             "property_type": "KFTypeVolume"}]


def _windowed_volume_keyframes(keyframes, seg_start_s, seg_end_s, default_gain, new_id):
    """Keyframes that affect one split segment, without leaking earlier beats.

    The timeline stores absolute keyframes. When a looped BGM bed is split into
    several JianYing segments, each piece must receive only the automation for
    its target window. If a duck spans a piece boundary, seed the piece with the
    gain active at its start; otherwise leave flat/default pieces keyframe-free.
    """
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
    return _volume_keyframes(selected, start, new_id)


def _base_segment(material_id, target_start_us, target_dur_us, render_index,
                  volume, keyframes, new_id):
    return {
        "enable_adjust": True, "enable_color_correct_adjust": False,
        "enable_color_curves": True, "enable_color_match_adjust": False,
        "enable_color_wheels": True, "enable_lut": True,
        "enable_smart_color_adjust": False,
        "last_nonzero_volume": 1.0, "reverse": False,
        "track_attribute": 0, "track_render_index": 0, "visible": True,
        "id": new_id(), "material_id": material_id,
        "target_timerange": _timerange(target_start_us, target_dur_us),
        "common_keyframes": keyframes, "keyframe_refs": [],
        "speed": 1.0, "volume": round(float(volume), 4),
        "is_tone_modify": False, "render_index": render_index,
    }


def _audio_segment_piece(material_id, target_start_us, target_dur_us, source_start_us,
                         source_dur_us, render_index, volume, keyframes, new_id):
    seg = _base_segment(material_id, target_start_us, target_dur_us, render_index,
                        volume, keyframes, new_id)
    seg["source_timerange"] = _timerange(source_start_us, source_dur_us)
    seg["extra_material_refs"] = []
    seg["clip"] = None
    seg["hdr_settings"] = None
    return seg


def _clip_default():
    return {"alpha": 1.0, "flip": {"horizontal": False, "vertical": False},
            "rotation": 0.0, "scale": {"x": 1.0, "y": 1.0},
            "transform": {"x": 0.0, "y": 0.0}}


def build_draft(timeline, new_id=None, probe=None):
    """Build the 剪映 draft_content dict (and its companion meta) from a timeline."""
    new_id = new_id or _default_id
    probe = probe or _probe_media

    canvas = timeline.get("canvas", {})
    width = int(canvas.get("width", 1920))
    height = int(canvas.get("height", 1080))
    fps = float(canvas.get("fps", 30))
    total_us = _us(timeline.get("duration", 0))

    materials = {"videos": [], "audios": [], "texts": [], "speeds": []}
    tracks = []
    notes = []

    def media_dur(path, fallback_us):
        if path and os.path.exists(path):
            d, w, h = probe(path)
            return d or fallback_us, w, h
        return fallback_us, 0, 0

    for track in timeline.get("tracks", []):
        kind = track.get("kind")
        # skip empty audio/text tracks — some 剪映 versions dislike zero-segment tracks
        if kind in ("audio", "text") and not track.get("segments"):
            continue

        if kind == "video":
            segs = []
            for clip in track.get("clips", []):
                ts, te = float(clip["timeline_start"]), float(clip["timeline_end"])
                ss, se = float(clip["source_start"]), float(clip["source_end"])
                path = clip["source_path"]
                src_dur_us, w, h = media_dur(path, _us(se))
                mat_id = new_id()
                materials["videos"].append({
                    "audio_fade": None, "category_id": "", "category_name": "local",
                    "check_flag": 63487, "crop": {
                        "upper_left_x": 0.0, "upper_left_y": 0.0,
                        "upper_right_x": 1.0, "upper_right_y": 0.0,
                        "lower_left_x": 0.0, "lower_left_y": 1.0,
                        "lower_right_x": 1.0, "lower_right_y": 1.0},
                    "crop_ratio": "free", "crop_scale": 1.0,
                    "duration": int(src_dur_us), "height": h or height,
                    "id": mat_id, "local_material_id": mat_id, "material_id": mat_id,
                    "material_name": os.path.basename(path), "media_path": "",
                    "path": path, "type": "video", "width": w or width})
                speed = _speed_material(new_id)
                materials["speeds"].append(speed)
                audio = clip.get("audio", {})
                vol = audio.get("base_gain", 1.0) if not audio.get("volume_keyframes") else 1.0
                seg = _base_segment(mat_id, _us(ts), _us(te - ts), RI_VIDEO, vol,
                                    _volume_keyframes(audio.get("volume_keyframes"), ts, new_id),
                                    new_id)
                seg["source_timerange"] = _timerange(_us(ss), _us(se - ss))
                seg["extra_material_refs"] = [speed["id"]]
                seg["clip"] = _clip_default()
                seg["uniform_scale"] = {"on": True, "value": 1.0}
                seg["hdr_settings"] = {"intensity": 1.0, "mode": 1, "nits": 1000}
                segs.append(seg)
            tracks.append({"attribute": 0, "flag": 0, "id": new_id(),
                           "is_default_name": False, "name": "video",
                           "segments": segs, "type": "video"})

        elif kind == "audio":
            role = track.get("role", track.get("name", "audio"))
            ri = RI_BGM if role == "bgm" else RI_NARRATION
            segs = []
            for s in track.get("segments", []):
                ts, te = float(s["timeline_start"]), float(s["timeline_end"])
                path = s["source_path"]
                mat_dur_us, _w, _h = media_dur(path, _us(te - ts))
                # a single audio segment cannot exceed its material; clamp (loop is left to the editor)
                want_us = _us(te - ts)
                place_us = want_us
                if mat_dur_us and want_us > mat_dur_us:
                    if role == "bgm" and track.get("loop"):
                        place_us = want_us
                    else:
                        place_us = mat_dur_us
                    if role == "bgm" and not track.get("loop"):  # looping a bed matters; a beat clamp is sub-frame rounding
                        notes.append(f"BGM 素材({mat_dur_us/1e6:.1f}s) 短于时间线({(te - ts):.1f}s)，剪映中未循环铺满（可在剪映里手动复制延长）")
                mat_id = new_id()
                materials["audios"].append({
                    "app_id": 0, "category_id": "", "category_name": "local",
                    "check_flag": 3, "copyright_limit_type": "none",
                    "duration": int(mat_dur_us or want_us), "effect_id": "",
                    "formula_id": "", "id": mat_id, "local_material_id": mat_id,
                    "music_id": mat_id, "name": os.path.basename(path), "path": path,
                    "source_platform": 0, "type": "extract_music", "wave_points": []})
                kfs = _volume_keyframes(s.get("volume_keyframes"), ts, new_id)
                vol = s.get("gain", 1.0) if not kfs else 1.0
                if role == "bgm" and track.get("loop") and mat_dur_us and want_us > mat_dur_us:
                    cursor = 0
                    while cursor < want_us:
                        piece = min(mat_dur_us, want_us - cursor)
                        if piece <= 0:
                            break
                        piece_start_s = ts + (cursor / 1_000_000)
                        piece_end_s = ts + ((cursor + piece) / 1_000_000)
                        speed = _speed_material(new_id)
                        materials["speeds"].append(speed)
                        piece_kfs = _windowed_volume_keyframes(
                            s.get("volume_keyframes"), piece_start_s, piece_end_s,
                            s.get("gain", 1.0), new_id)
                        piece_vol = s.get("gain", 1.0) if not piece_kfs else 1.0
                        seg = _audio_segment_piece(
                            mat_id, _us(ts) + cursor, piece, 0, piece, ri, piece_vol, piece_kfs, new_id)
                        seg["extra_material_refs"] = [speed["id"]]
                        segs.append(seg)
                        cursor += piece
                else:
                    speed = _speed_material(new_id)
                    materials["speeds"].append(speed)
                    seg = _audio_segment_piece(
                        mat_id, _us(ts), place_us, 0, place_us, ri, vol, kfs, new_id)
                    seg["extra_material_refs"] = [speed["id"]]
                    segs.append(seg)
            tracks.append({"attribute": 0, "flag": 0, "id": new_id(),
                           "is_default_name": False, "name": role,
                           "segments": segs, "type": "audio"})

        elif kind == "text":
            segs = []
            for s in track.get("segments", []):
                ts, te = float(s["timeline_start"]), float(s["timeline_end"])
                text = s.get("text", "")
                mat_id = new_id()
                content = {"text": text, "styles": [{
                    "fill": {"alpha": 1.0, "content": {"render_type": "solid",
                             "solid": {"alpha": 1.0, "color": [1.0, 1.0, 1.0]}}},
                    "range": [0, len(text.encode("utf-16-le")) // 2],
                    "size": 8.0, "bold": False, "italic": False,
                    "underline": False, "strokes": []}]}
                materials["texts"].append({
                    "id": mat_id, "content": json.dumps(content, ensure_ascii=False),
                    "type": "text", "typesetting": 0,
                    "alignment": 1, "letter_spacing": 0.0, "line_spacing": 0.02,
                    "font_size": 8.0, "text_color": "#FFFFFF", "add_type": 0,
                    "check_flag": 7})
                seg = _base_segment(mat_id, _us(ts), _us(te - ts), RI_TEXT, 1.0, [], new_id)
                seg["source_timerange"] = None
                seg["extra_material_refs"] = []
                seg["clip"] = _clip_default()
                seg["uniform_scale"] = {"on": True, "value": 1.0}
                seg["type"] = "text_effect"
                seg["source_platform"] = 1
                # nudge subtitles to the lower third
                seg["clip"]["transform"]["y"] = -0.72
                segs.append(seg)
            tracks.append({"attribute": 0, "flag": 0, "id": new_id(),
                           "is_default_name": False, "name": "subtitle",
                           "segments": segs, "type": "text"})

    draft_id = new_id()
    content = {
        "canvas_config": {"width": width, "height": height, "ratio": "original"},
        "color_space": 0,
        "config": {"adjust_max_index": 1, "attachment_info": [], "combination_max_index": 1,
                   "export_range": None, "extract_audio_last_index": 1,
                   "lyrics_recognition_id": "", "lyrics_sync": True, "lyrics_taskinfo": [],
                   "maintrack_adsorb": True, "material_save_mode": 0,
                   "multi_language_current": "none", "multi_language_list": [],
                   "multi_language_main": "none", "multi_language_mode": "none",
                   "original_sound_last_index": 1, "record_audio_last_index": 1,
                   "sticker_max_index": 1, "subtitle_keywords_config": None,
                   "subtitle_recognition_id": "", "subtitle_sync": True,
                   "subtitle_taskinfo": [], "system_font_list": [], "video_mute": False,
                   "zoom_info_params": None},
        "cover": None, "create_time": 0, "duration": int(total_us), "extra_info": None,
        "fps": fps, "free_render_index_mode_on": False, "group_container": None,
        "id": draft_id, "keyframe_graph_list": [],
        "keyframes": {"adjusts": [], "audios": [], "effects": [], "filters": [],
                      "handwrites": [], "stickers": [], "texts": [], "videos": []},
        "last_modified_platform": dict(APP), "platform": dict(APP),
        "materials": _full_materials(materials),
        "mutable_config": None, "name": "", "new_version": NEW_VERSION,
        "relationships": [], "render_index_track_mode_on": False, "retouch_cover": None,
        "source": "default", "static_cover_image_path": "", "time_marks": None,
        "tracks": tracks, "update_time": 0, "version": DRAFT_VERSION,
    }
    meta = _meta_info(draft_id, total_us)
    return content, meta, notes


# the full 剪映 materials object: ~45 parallel arrays, only four of which we fill
_MATERIAL_KEYS = (
    "ai_translates audio_balances audio_effects audio_fades audio_track_indexes audios "
    "beats canvases chromas color_curves digital_humans drafts effects flowers green_screens "
    "handwrites hsl images log_color_wheels loudnesses manual_deformations masks "
    "material_animations material_colors multi_language_refs placeholders plugin_effects "
    "primary_color_wheels realtime_denoises shapes smart_crops smart_relights "
    "sound_channel_mappings speeds stickers tail_leaders text_templates texts time_marks "
    "transitions video_effects video_trackings videos vocal_beautifys vocal_separations"
).split()


def _full_materials(filled):
    out = {k: [] for k in _MATERIAL_KEYS}
    out.update(filled)
    return out


def _meta_info(draft_id, total_us):
    return {
        "cloud_package_completed_time": "", "draft_cloud_capcut_purchase_info": "",
        "draft_cloud_last_action_download": False, "draft_cloud_materials": [],
        "draft_cloud_purchase_info": "", "draft_cloud_template_id": "",
        "draft_cloud_tutorial_info": "", "draft_cloud_videocut_purchase_info": "",
        "draft_cover": "", "draft_deeplink_url": "",
        "draft_enterprise_info": {"draft_enterprise_extra": "", "draft_enterprise_id": "",
                                  "draft_enterprise_name": "", "enterprise_material": []},
        "draft_fold_path": "", "draft_id": draft_id, "draft_is_ai_packaging_used": False,
        "draft_is_ai_shorts": False, "draft_is_ai_translate": False,
        "draft_is_article_video_draft": False, "draft_is_from_deeplink": "false",
        "draft_is_invisible": False,
        "draft_materials": [{"type": t, "value": []} for t in (0, 1, 2, 3, 6, 7, 8)],
        "draft_materials_copied_info": [], "draft_name": "", "draft_new_version": "",
        "draft_removable_storage_device": "", "draft_root_path": "",
        "draft_segment_extra_info": [], "draft_type": "", "tm_draft_cloud_completed": "",
        "tm_draft_cloud_modified": 0, "tm_draft_removed": 0, "tm_duration": int(total_us),
    }


def _bundle_media(content, draft_dir):
    """Copy every referenced media file into <draft_dir>/materials/ and rewrite the
    material paths to the copies, so the draft folder is self-contained and portable
    (move/zip it to any 剪映 machine; media sits right next to the JSON for relink)."""
    mats_dir = os.path.join(draft_dir, "materials")
    os.makedirs(mats_dir, exist_ok=True)
    copied, used, notes = {}, set(), []
    for arr in (content["materials"]["videos"], content["materials"]["audios"]):
        for m in arr:
            src = m.get("path")
            if not src:
                continue
            if src in copied:
                m["path"] = copied[src]
                continue
            if not os.path.exists(src):
                notes.append(f"素材缺失，未打包: {src}")
                continue
            base = os.path.basename(src)
            name, stem_ext = base, os.path.splitext(base)
            i = 1
            while name in used:
                name = f"{stem_ext[0]}_{i}{stem_ext[1]}"
                i += 1
            used.add(name)
            dest = os.path.join(mats_dir, name)
            shutil.copy2(src, dest)
            copied[src] = dest
            m["path"] = dest
    return notes


def _draft_dir_has_user_content(draft_dir):
    """Return True when writing here could overwrite an existing draft/material."""
    if not os.path.exists(draft_dir):
        return False
    try:
        return any(os.scandir(draft_dir))
    except OSError:
        return True


def _collision_safe_draft_dir(out_dir, draft_name):
    """Pick a fresh draft folder instead of overwriting an existing non-empty one."""
    base = os.path.join(out_dir, draft_name)
    if not _draft_dir_has_user_content(base):
        return base, draft_name
    idx = 2
    while True:
        candidate_name = f"{draft_name}_{idx}"
        candidate = os.path.join(out_dir, candidate_name)
        if not _draft_dir_has_user_content(candidate):
            return candidate, candidate_name
        idx += 1


def _rewrite_material_prefix(content, old_prefix, new_prefix):
    old_prefix = os.path.abspath(old_prefix)
    new_prefix = os.path.abspath(new_prefix)
    for arr in (content["materials"]["videos"], content["materials"]["audios"]):
        for material in arr:
            path = material.get("path")
            if path and os.path.abspath(path).startswith(old_prefix + os.sep):
                material["path"] = new_prefix + os.path.abspath(path)[len(old_prefix):]


def export_timeline_to_jianying(timeline, out_dir, draft_name="recap", new_id=None,
                                probe=None, bundle_media=False):
    """Write a 剪映 draft folder under out_dir/draft_name. Returns (folder, notes).

    bundle_media=True copies the referenced media into the draft folder so it is
    self-contained and portable to another machine."""
    content, meta, notes = build_draft(timeline, new_id=new_id, probe=probe)
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    draft_dir, actual_name = _collision_safe_draft_dir(out_dir, draft_name)
    if actual_name != draft_name:
        notes.append(f"草稿目录已存在，改写为 {actual_name} 以避免覆盖")

    tmp_parent = tempfile.mkdtemp(prefix=f".{actual_name}.", dir=out_dir)
    tmp_dir = os.path.join(tmp_parent, actual_name)
    try:
        os.makedirs(tmp_dir, exist_ok=False)
        if bundle_media:
            notes = notes + _bundle_media(content, tmp_dir)
            _rewrite_material_prefix(content, tmp_dir, draft_dir)
        meta["draft_name"] = actual_name
        meta["draft_fold_path"] = draft_dir
        content["name"] = actual_name
        # write both filenames for cross-version compatibility (5.9 reads draft_info.json)
        for fname in ("draft_content.json", "draft_info.json"):
            with open(os.path.join(tmp_dir, fname), "w", encoding="utf-8") as f:
                json.dump(content, f, ensure_ascii=False, indent=2)
        with open(os.path.join(tmp_dir, "draft_meta_info.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        if os.path.isdir(draft_dir) and not _draft_dir_has_user_content(draft_dir):
            os.rmdir(draft_dir)
        os.replace(tmp_dir, draft_dir)
    except Exception:
        shutil.rmtree(tmp_parent, ignore_errors=True)
        raise
    finally:
        if os.path.exists(tmp_parent):
            shutil.rmtree(tmp_parent, ignore_errors=True)
    return draft_dir, notes


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Export a timeline.json to a 剪映/JianYing draft folder.")
    ap.add_argument("timeline", help="path to timeline.json")
    ap.add_argument("--out-dir", required=True, help="parent dir to create the draft folder in")
    ap.add_argument("--name", default="recap", help="draft folder name")
    ap.add_argument("--bundle-media", action="store_true",
                    help="copy referenced media into the draft folder (portable, self-contained)")
    args = ap.parse_args()
    with open(args.timeline, encoding="utf-8") as f:
        timeline = json.load(f)
    draft_dir, notes = export_timeline_to_jianying(timeline, args.out_dir, args.name,
                                                   bundle_media=args.bundle_media)
    for n in notes:
        print(f"  注意: {n}")
    print(json.dumps({"status": "exported", "draft_dir": draft_dir}, ensure_ascii=False))


if __name__ == "__main__":
    main()
