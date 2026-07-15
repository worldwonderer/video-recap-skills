"""Generate and cache source and edited storyboard sheets."""

from pathlib import Path

from lib import CONFIG, log, file_fingerprint


from storyboard import build_source_storyboard, build_edited_storyboard


from understanding_cache import (
    _artifact_fingerprint,
    _frames_manifest_path,
    _load_json,
    _stage_cache_valid,
    _write_stage_meta,
)


def _storyboard_sample_policy():
    return {
        "max_tiles": CONFIG.get("storyboard_max_tiles", 30),
        "columns": CONFIG.get("storyboard_columns", 6),
    }


def _edited_storyboard_meta(clip_plan_validated_json, frames_manifest_path):
    """Cache key for the edited storyboard: clip plan + fps + frame-set all in the key, so an
    fps change OR a re-validated plan invalidates it. font availability is deliberately NOT in
    the key (the JSON `labels_burned` flag surfaces it instead)."""
    return {
        "schema_version": 1,
        "stage": "edited_storyboard",
        "clip_plan_validated_fp": _artifact_fingerprint(clip_plan_validated_json),
        "fps": float(CONFIG.get("fps") or 0),
        "frames_manifest_fp": _artifact_fingerprint(frames_manifest_path),
        "sample_policy": _storyboard_sample_policy(),
    }


def _generate_source_storyboard(
    work_dir, video_path, scenes, scenes_json, *, force=False
):
    """Generate (or reuse cached) the source storyboard. Advisory: returns dict|None, never raises.

    Cached via _write_stage_meta/_stage_cache_valid on storyboard/source_storyboard.json; the
    meta includes fps + the frames-manifest fp so an fps-change resume rebuilds (Principle 5).
    If frames/ is absent (cache hit skipped extraction / cleaned) → skip + log; pipeline continues.
    """
    if not CONFIG.get("storyboard", True):
        return None
    frames_dir = Path(work_dir) / "frames"
    if not frames_dir.is_dir() or not any(frames_dir.glob("frame_*.jpg")):
        log("storyboard 跳过 source：frames/ 缺失（缓存命中跳过了帧提取？）")
        return None
    json_path = Path(work_dir) / "storyboard" / "source_storyboard.json"
    meta = {
        "schema_version": 1,
        "stage": "source_storyboard",
        "video_fp": file_fingerprint(video_path),
        "fps": float(CONFIG.get("fps") or 0),
        "scenes_fp": _artifact_fingerprint(scenes_json),
        "frames_manifest_fp": _artifact_fingerprint(_frames_manifest_path(work_dir)),
        "sample_policy": _storyboard_sample_policy(),
    }
    if not force and _stage_cache_valid(json_path, meta):
        try:
            cached = _load_json(json_path)
        except (OSError, ValueError):
            log(
                "storyboard source 缓存命中但文件损坏，重建"
            )  # advisory: never raise out
        else:
            log("storyboard 跳过 source（缓存匹配）")
            return cached
    result = build_source_storyboard(work_dir, video_path, scenes, CONFIG.get("fps"))
    if result is not None and json_path.exists():
        _write_stage_meta(json_path, meta)
    return result


def _generate_edited_storyboard(work_dir, source_video_path, *, force=False):
    """Generate (or reuse cached) the edited storyboard, GATED on clip_plan_validated.json
    file-presence (NOT on edit_mode — recap.py forwards --edit-mode cut in BOTH passes, so the
    validated plan presence is the only reliable pass2 signal). Advisory: returns dict|None.
    """
    if not CONFIG.get("storyboard", True):
        return None
    clip_plan_validated_json = Path(work_dir) / "clip_plan_validated.json"
    if not clip_plan_validated_json.exists():
        return None  # pass1 (no validated plan yet) → no edited storyboard
    frames_dir = Path(work_dir) / "frames"
    if not frames_dir.is_dir() or not any(frames_dir.glob("frame_*.jpg")):
        log("storyboard 跳过 edited：frames/ 缺失（缓存命中跳过了帧提取？）")
        return None
    json_path = Path(work_dir) / "storyboard" / "edited_storyboard.json"
    meta = _edited_storyboard_meta(
        clip_plan_validated_json, _frames_manifest_path(work_dir)
    )
    if not force and _stage_cache_valid(json_path, meta):
        try:
            cached = _load_json(json_path)
        except (OSError, ValueError):
            log(
                "storyboard edited 缓存命中但文件损坏，重建"
            )  # advisory: never raise out
        else:
            log("storyboard 跳过 edited（缓存匹配）")
            return cached
    try:
        clip_plan_validated = _load_json(clip_plan_validated_json)
    except (OSError, ValueError):
        log("storyboard 跳过 edited：clip_plan_validated.json 无法解析")
        return None
    result = build_edited_storyboard(
        work_dir, source_video_path, clip_plan_validated, CONFIG.get("fps")
    )
    if result is not None and json_path.exists():
        _write_stage_meta(json_path, meta)
    return result


def _prepend_storyboard_brief_header(
    brief_path, source_storyboard, edited_storyboard, *, cut_mode
):
    """Post-process the RETURNED brief markdown FILE (C1): prepend a short storyboard header.

    Editing the brief FILE on disk (not brief.py) keeps the brief⇄narration twin byte-identical.
    Branches the edited-storyboard line on clip_plan_validated presence (edited_storyboard truthy)
    so pass1 never prints a not-yet-existing path. If labels_burned:false, point to inspect clip-map.
    """
    if not source_storyboard and not edited_storyboard:
        return
    try:
        brief_path = Path(brief_path)
        lines = ["## Storyboard（先看 storyboard 再写）", ""]
        any_labels_missing = False
        if source_storyboard:
            pages = source_storyboard.get("page_images") or []
            lines.append(
                f"- 源时间线 storyboard: {', '.join(pages)}（tiles 时间戳=原片时间）"
            )
            if not source_storyboard.get("labels_burned", False):
                any_labels_missing = True
        if cut_mode and edited_storyboard:
            pages = edited_storyboard.get("page_images") or []
            lines.append(
                f"- 成片(output)时间线 storyboard: {', '.join(pages)}"
                "（每块双标 out 时间 / src 原片时间；注意区分两条时间线）"
            )
            if not edited_storyboard.get("labels_burned", False):
                any_labels_missing = True
        if any_labels_missing:
            lines.append(
                "- 时间戳未烧入 → 用 `inspect clip-map` 查时间（JSON sidecar 仍为权威时间源）"
            )
        lines.append("")
        header = "\n".join(lines) + "\n"
        existing = brief_path.read_text(encoding="utf-8") if brief_path.exists() else ""
        brief_path.write_text(header + existing, encoding="utf-8")
    except OSError as exc:
        log(f"storyboard brief 头部写入失败（忽略）: {exc}")
