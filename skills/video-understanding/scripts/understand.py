#!/usr/bin/env python3
"""video-understanding entrypoint.

Analyze a source video into a structured understanding index (scenes, ASR transcript,
per-scene VLM analysis, silence windows, fused timeline) plus a narration-writing brief.
Stateless: a semantic stage is skipped only when its output artifact and provenance
sidecar match the current source video plus output-affecting settings (use --force
to recompute everything).
"""
import argparse
import hashlib
import json
from pathlib import Path

from lib import CONFIG, log, get_video_duration, api_call, file_fingerprint, load_prompt
from extract import extract_frames
from detect import detect_scenes, detect_silence_periods
from asr import transcribe_audio
from vlm import (
    analyze_scenes,
    analyze_video_overview,
    mimo_video_overview_cache_fresh,
    _is_mimo_chunk_usable,
)
from brief import build_agent_brief, assess_understanding_substrate
from storyboard import build_source_storyboard, build_edited_storyboard


def _fresh(out, *inputs):
    out = Path(out)
    if not out.exists():
        return False
    ins = [Path(p) for p in inputs if p and Path(p).exists()]
    if not ins:
        return out.stat().st_size > 0
    return out.stat().st_mtime >= max(p.stat().st_mtime for p in ins)


def _artifact_meta_path(artifact_path):
    artifact_path = Path(artifact_path)
    return artifact_path.with_name(f"{artifact_path.name}.meta.json")


def _load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _artifact_fingerprint(path):
    path = Path(path)
    return file_fingerprint(path) if path.exists() else None


def _stage_cache_valid(artifact_path, expected_meta):
    artifact_path = Path(artifact_path)
    if not artifact_path.exists():
        return False
    meta_path = _artifact_meta_path(artifact_path)
    if not meta_path.exists():
        return False
    try:
        meta = _load_json(meta_path)
    except (OSError, ValueError, TypeError):
        return False
    recorded = meta.get("artifact_fingerprint") if isinstance(meta, dict) else None
    if not recorded or recorded != _artifact_fingerprint(artifact_path):
        return False
    expected = dict(expected_meta)
    expected["artifact_fingerprint"] = recorded
    return meta == expected


def _write_stage_meta(artifact_path, meta):
    meta_path = _artifact_meta_path(artifact_path)
    payload = dict(meta)
    payload["artifact_fingerprint"] = _artifact_fingerprint(artifact_path)
    meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _remove_stage_meta(artifact_path):
    _artifact_meta_path(artifact_path).unlink(missing_ok=True)


def _short_status_message(message, limit=240):
    """Compact optional-stage messages for sidecars; no tracebacks or bulky payloads."""
    text = " ".join(str(message or "").split())
    return text[:limit]


def _write_optional_stage_status(work_dir, filename, payload):
    path = Path(work_dir) / filename
    safe = dict(payload)
    safe["message"] = _short_status_message(safe.get("message", ""))
    path.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _write_mimo_overview_status(work_dir, status, message="", artifact=None, *, enabled=None):
    return _write_optional_stage_status(work_dir, "mimo_video_overview.status.json", {
        "stage": "mimo_video_overview",
        "enabled": bool(CONFIG.get("mimo_video_overview", False)) if enabled is None else bool(enabled),
        "status": status,
        "message": message,
        "artifact": artifact,
    })


def _merge_overview_into_scenes(scenes, overview_path):
    """Make the MiMo video-overview the PRIMARY per-scene description when present.

    The frame VLM still provides `frame_facts` (timestamped grounding) and `depth_analysis`;
    this only replaces the per-scene `description` with the motion-aware video-overview analysis,
    keeping the original frame description under `frame_description` for provenance/fallback.
    Scenes whose overview chunk was missing or moderation-rejected keep the frame description.

    In-memory only — `vlm_analysis.json` on disk stays the pure frame-VLM product, so the VLM
    cache stays coherent and the merge is re-derived (frames + overview) every run. Because
    `frame_facts` is untouched, `assess_understanding_substrate` (which grades on frame_facts +
    ASR) cannot regress; richer descriptions can only help.
    """
    overview = _load_json(overview_path) if Path(overview_path).exists() else None
    if not isinstance(overview, dict):
        return scenes
    by_scene = {}
    for chunk in overview.get("chunks") or []:
        if not isinstance(chunk, dict):
            continue
        content = str(chunk.get("content", "")).strip()
        if content and _is_mimo_chunk_usable(content):
            by_scene.setdefault(chunk.get("scene_id"), []).append(content)
    if not by_scene:
        return scenes
    enriched = 0
    for scene in scenes or []:
        if not isinstance(scene, dict):
            continue
        contents = by_scene.get(scene.get("scene_id"))
        if not contents:
            continue
        scene.setdefault("frame_description", scene.get("description", ""))
        scene["description"] = "\n".join(contents)
        scene["description_source"] = "mimo_video_overview"
        enriched += 1
    if enriched:
        log(f"已用 MiMo 视频概览增强 {enriched} 个场景的描述（逐帧 frame_facts 保留作锚点）")
    return scenes


def _write_consolidation_status(work_dir, status, message="", artifacts=None, *, enabled=True, do_asr=False, do_index=True):
    return _write_optional_stage_status(work_dir, "consolidation.status.json", {
        "stage": "consolidation",
        "enabled": bool(enabled),
        "do_asr": bool(do_asr),
        "do_index": bool(do_index),
        "status": status,
        "message": message,
        "artifacts": list(artifacts or []),
    })


def _present_consolidation_artifacts(work_dir):
    work_dir = Path(work_dir)
    return [
        name for name in ("understanding_index.json", "asr_clean.json")
        if (work_dir / name).exists()
    ]


def _scene_cache_payload(video_path):
    return {
        "schema_version": 1,
        "stage": "scenes",
        "source_video_fingerprint": file_fingerprint(video_path),
        "settings": {
            "scene_threshold": CONFIG.get("scene_threshold"),
            "scene_junk_filter": CONFIG.get("scene_junk_filter"),
            "scene_merge_min": CONFIG.get("scene_merge_min"),
            "scene_junk_dark_luma": CONFIG.get("scene_junk_dark_luma"),
            "scene_junk_bright_luma": CONFIG.get("scene_junk_bright_luma"),
            "scene_junk_pixel_ratio": CONFIG.get("scene_junk_pixel_ratio"),
        },
    }


def _asr_cache_payload(video_path, *, skip_asr=False):
    return {
        "schema_version": 1,
        "stage": "asr",
        "source_video_fingerprint": file_fingerprint(video_path),
        "settings": {
            "skip_asr": bool(skip_asr),
            "mimo_asr_api_key_present": bool(CONFIG.get("mimo_asr_api_key")),
            "mimo_asr_api_url": CONFIG.get("mimo_asr_api_url"),
            "mimo_asr_model": CONFIG.get("mimo_asr_model"),
            "mimo_asr_language": CONFIG.get("mimo_asr_language"),
            "mimo_asr_base64_max_mb": CONFIG.get("mimo_asr_base64_max_mb"),
            "asr_segment_seconds": CONFIG.get("asr_segment_seconds"),
        },
    }


def _silence_cache_payload(video_path, asr_json):
    return {
        "schema_version": 1,
        "stage": "silence",
        "source_video_fingerprint": file_fingerprint(video_path),
        "asr_result_fingerprint": _artifact_fingerprint(asr_json),
        "asr_meta": _load_json(_artifact_meta_path(asr_json)) if _artifact_meta_path(asr_json).exists() else None,
        "settings": {
            "silence_noise_threshold": CONFIG.get("silence_noise_threshold"),
            "silence_min_duration": CONFIG.get("silence_min_duration"),
            "quiet_window_min": CONFIG.get("quiet_window_min"),
            "silence_merge_gap": CONFIG.get("silence_merge_gap"),
        },
    }


def _vlm_prompt_fingerprint():
    prompt = load_prompt("VLM_DEPTH_PROMPT")
    if not prompt:
        prompt = (
            "仔细观察这些视频帧。分两部分输出：\n"
            "【描述】不超过80字，描述画面中正在发生什么。\n"
            "【深层分析】不超过120字，分析角色情绪、关系动态、潜台词。"
        )
    context = CONFIG.get("context_info", "")
    if context:
        prompt = f"已知信息：{context}\n\n{prompt}"
    return {
        "prompt_text_fingerprint": _text_fingerprint(prompt),
        "context_info_fingerprint": _text_fingerprint(context),
    }


def _text_fingerprint(value):
    return hashlib.md5(str(value or "").encode("utf-8")).hexdigest()


def _vlm_cache_payload(video_path, work_dir, scenes_json, frames):
    return {
        "schema_version": 1,
        "stage": "vlm",
        "source_video_fingerprint": file_fingerprint(video_path),
        "scenes_fingerprint": _artifact_fingerprint(scenes_json),
        "frames": _frame_cache_payload(video_path, CONFIG.get("fps"), frames),
        "prompt": _vlm_prompt_fingerprint(),
        "settings": {
            "fps": CONFIG.get("fps"),
            "vlm_model": CONFIG.get("vlm_model"),
            "api_url": CONFIG.get("api_url"),
            "mimo_disable_thinking": CONFIG.get("mimo_disable_thinking", True),
            "mimo_media_resolution": CONFIG.get("mimo_media_resolution"),
            "background_research_fingerprint": _artifact_fingerprint(Path(work_dir) / "background_research.json"),
        },
    }


def _frames_manifest_path(work_dir):
    return Path(work_dir) / "frames" / "frames_manifest.json"


def _frame_cache_payload(video_path, fps, frames):
    frame_names = [Path(frame).name for frame in frames]
    return {
        "schema_version": 1,
        "source_video_fingerprint": file_fingerprint(video_path),
        "fps": float(fps),
        "frame_count": len(frames),
        "frames": frame_names,
        "frame_fingerprints": {
            name: file_fingerprint(frame)
            for name, frame in zip(frame_names, frames)
        },
    }


def _write_frames_manifest(work_dir, video_path, fps, frames):
    manifest_path = _frames_manifest_path(work_dir)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(_frame_cache_payload(video_path, fps, frames), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _frames_cache_valid(video_path, work_dir, fps):
    frames_dir = Path(work_dir) / "frames"
    frames = sorted(frames_dir.glob("frame_*.jpg"))
    if not frames:
        return False
    manifest_path = _frames_manifest_path(work_dir)
    if not manifest_path.exists():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = _frame_cache_payload(video_path, fps, frames)
    except (OSError, ValueError, TypeError):
        return False
    return manifest == expected


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


def _generate_source_storyboard(work_dir, video_path, scenes, scenes_json, *, force=False):
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
            log("storyboard source 缓存命中但文件损坏，重建")  # advisory: never raise out
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
    meta = _edited_storyboard_meta(clip_plan_validated_json, _frames_manifest_path(work_dir))
    if not force and _stage_cache_valid(json_path, meta):
        try:
            cached = _load_json(json_path)
        except (OSError, ValueError):
            log("storyboard edited 缓存命中但文件损坏，重建")  # advisory: never raise out
        else:
            log("storyboard 跳过 edited（缓存匹配）")
            return cached
    try:
        clip_plan_validated = _load_json(clip_plan_validated_json)
    except (OSError, ValueError):
        log("storyboard 跳过 edited：clip_plan_validated.json 无法解析")
        return None
    result = build_edited_storyboard(work_dir, source_video_path, clip_plan_validated, CONFIG.get("fps"))
    if result is not None and json_path.exists():
        _write_stage_meta(json_path, meta)
    return result


def _prepend_storyboard_brief_header(brief_path, source_storyboard, edited_storyboard, *, cut_mode):
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
            lines.append(f"- 源时间线 storyboard: {', '.join(pages)}（tiles 时间戳=原片时间）")
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
            lines.append("- 时间戳未烧入 → 用 `inspect clip-map` 查时间（JSON sidecar 仍为权威时间源）")
        lines.append("")
        header = "\n".join(lines) + "\n"
        existing = brief_path.read_text(encoding="utf-8") if brief_path.exists() else ""
        brief_path.write_text(header + existing, encoding="utf-8")
    except OSError as exc:
        log(f"storyboard brief 头部写入失败（忽略）: {exc}")


def _clip_text(text, limit):
    value = " ".join(str(text or "").split()).strip()
    return value[:limit]


def _research_context(work_dir):
    """Fold background_research.json into a compact context string for the VLM prompt.

    The agent does story research first (per references/research-guide.md) and writes
    work_dir/background_research.json; this surfaces synopsis, named characters,
    relationships, plot arcs, and cultural notes so scene VLM analysis can name people
    and read scenes with plot knowledge instead of labelling everyone "黑衣男子".
    Returns "" when no usable research file is present, so behaviour is unchanged.
    """
    path = Path(work_dir) / "background_research.json"
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    if not isinstance(data, dict):
        return ""
    parts = []
    for key in ("synopsis", "episode_context", "worldbuilding"):
        val = _clip_text(data.get(key), 320)
        if val:
            parts.append(val)
    chars = data.get("characters")
    if isinstance(chars, dict) and chars:
        named = "；".join(
            f"{_clip_text(name, 40)}（{_clip_text(desc, 120)}）"
            for name, desc in list(chars.items())[:12]
            if _clip_text(name, 40)
        )
        if named:
            parts.append("主要人物：" + named)
    details = data.get("character_details")
    if isinstance(details, dict) and details:
        detail_lines = []
        for name, info in list(details.items())[:8]:
            if not isinstance(info, dict):
                continue
            bits = []
            aliases = info.get("aliases")
            if isinstance(aliases, list) and aliases:
                clean_aliases = [_clip_text(alias, 30) for alias in aliases[:3]]
                clean_aliases = [alias for alias in clean_aliases if alias]
                if clean_aliases:
                    bits.append("别名" + "/".join(clean_aliases))
            role = _clip_text(info.get("role"), 60)
            if role:
                bits.append(role)
            rels = info.get("relationships")
            if isinstance(rels, list) and rels:
                clean_rels = [_clip_text(rel, 60) for rel in rels[:3]]
                bits.extend(rel for rel in clean_rels if rel)
            clean_name = _clip_text(name, 40)
            if clean_name and bits:
                detail_lines.append(f"{clean_name}（{'；'.join(bits)}）")
        if detail_lines:
            parts.append("人物关系：" + "；".join(detail_lines))
    arcs = data.get("plot_arcs")
    if isinstance(arcs, list) and arcs:
        arc_lines = []
        for arc in arcs[:6]:
            if not isinstance(arc, dict):
                val = _clip_text(arc, 120)
                if val:
                    arc_lines.append(val)
                continue
            name = _clip_text(arc.get("name"), 50)
            desc = _clip_text(arc.get("description"), 120)
            status = _clip_text(arc.get("status"), 30)
            if name or desc:
                tail = f"[{status}]" if status else ""
                arc_lines.append(f"{name}：{desc}{tail}".strip("："))
        if arc_lines:
            parts.append("剧情线：" + "；".join(arc_lines))
    notes = data.get("cultural_notes")
    if isinstance(notes, list) and notes:
        note_lines = []
        for note in notes[:4]:
            if not isinstance(note, dict):
                val = _clip_text(note, 100)
                if val:
                    note_lines.append(val)
                continue
            item = _clip_text(note.get("item"), 50)
            expl = _clip_text(note.get("explanation"), 100)
            if item or expl:
                note_lines.append(f"{item}：{expl}".strip("："))
        if note_lines:
            parts.append("背景注释：" + "；".join(note_lines))
    return " ".join(parts).strip()[:1200]


def main():
    ap = argparse.ArgumentParser(description="Analyze a video into an understanding index + narration brief.")
    ap.add_argument("video")
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--context", default="", help="extra context (show name, character names, ...)")
    ap.add_argument("--scene-threshold", type=float, default=None)
    ap.add_argument("--style", default="纪录片")
    ap.add_argument("--edit-mode", default=None, choices=["full", "cut"], help="recap mode to document in the writing brief")
    ap.add_argument("--target-duration", default=None, help="cut-mode target duration to document in the writing brief")
    ap.add_argument("--skip-asr", action="store_true")
    ap.add_argument("--mimo-video-overview", action="store_true")
    ap.add_argument("--force", action="store_true", help="ignore cached artifacts and recompute")
    ap.add_argument("--consolidate", action=argparse.BooleanOptionalAction, default=True,
                    help="build the global understanding story index (Pass B); default ON, --no-consolidate to skip")
    ap.add_argument("--consolidate-asr", action="store_true", help="also clean the ASR transcript (Pass A)")
    args = ap.parse_args()

    video = args.video
    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    # Story research (if the agent wrote background_research.json first) feeds the VLM
    # context, so scene analysis can name characters and read scenes with plot knowledge.
    research_ctx = _research_context(work_dir)
    context_parts = [p for p in (research_ctx, args.context) if p and p.strip()]
    if context_parts:
        CONFIG["context_info"] = "　".join(context_parts)
    if research_ctx:
        log(f"已并入 background_research.json 到理解上下文（{len(research_ctx)} 字）")
    if args.scene_threshold is not None:
        CONFIG["scene_threshold"] = args.scene_threshold
    if args.edit_mode is not None:
        CONFIG["edit_mode"] = args.edit_mode
    if args.target_duration is not None:
        CONFIG["target_duration"] = args.target_duration
    if args.mimo_video_overview:
        CONFIG["mimo_video_overview"] = True
    scene_threshold = CONFIG.get("scene_threshold")

    video_duration = get_video_duration(video)
    if CONFIG["fps"] <= 0:
        CONFIG["fps"] = 2 if video_duration <= 60 else (1.5 if video_duration <= 300 else 1)
    log(f"FPS: {CONFIG['fps']} (视频时长: {video_duration:.1f}s)")

    scenes_json = work_dir / "scenes.json"
    asr_json = work_dir / "asr_result.json"
    silence_json = work_dir / "silence_periods.json"
    vlm_json = work_dir / "vlm_analysis.json"
    frames_dir = work_dir / "frames"

    # Step 1: frame extraction
    if not args.force and _frames_cache_valid(video, work_dir, CONFIG["fps"]):
        frames = sorted(frames_dir.glob("frame_*.jpg"))
        log(f"跳过帧提取（缓存匹配 {len(frames)} 帧）")
    else:
        frames = extract_frames(video, work_dir)
        _write_frames_manifest(work_dir, video, CONFIG["fps"], frames)

    # Step 2: scene detection
    scenes_meta = _scene_cache_payload(video)
    if not args.force and _stage_cache_valid(scenes_json, scenes_meta):
        scenes = _load_json(scenes_json)
        log(f"跳过场景检测（已存在 {len(scenes)} 个场景）")
    else:
        scenes = detect_scenes(video, work_dir, scene_threshold)
        _write_stage_meta(scenes_json, scenes_meta)

    # Step 3: ASR
    asr_meta = _asr_cache_payload(video, skip_asr=args.skip_asr)
    if args.skip_asr:
        asr_result = []
        asr_json.write_text(json.dumps(asr_result, ensure_ascii=False, indent=2), encoding="utf-8")
        _write_stage_meta(asr_json, asr_meta)
        log("跳过 ASR（--skip-asr）")
    elif not args.force and _stage_cache_valid(asr_json, asr_meta):
        asr_result = _load_json(asr_json)
        log(f"跳过 ASR（已存在 {len(asr_result)} 段）")
    else:
        try:
            asr_result = transcribe_audio(video, work_dir)
        except Exception as e:
            _remove_stage_meta(asr_json)
            asr_json.unlink(missing_ok=True)
            raise RuntimeError(f"ASR 失败；未写入可复用缓存，请修复后重试或显式使用 --skip-asr: {e}") from e
        _write_stage_meta(asr_json, asr_meta)

    # Step 3.5: silence detection
    silence_meta = _silence_cache_payload(video, asr_json)
    if not args.force and _stage_cache_valid(silence_json, silence_meta):
        silence_periods = _load_json(silence_json)
        log(f"跳过静音检测（已存在 {len(silence_periods)} 个窗口）")
    else:
        silence_periods = detect_silence_periods(video, work_dir, asr_result)
        _write_stage_meta(silence_json, silence_meta)

    # Step 4: VLM analysis (the only stage that requires the chat API key)
    vlm_meta = _vlm_cache_payload(video, work_dir, scenes_json, frames)
    if not args.force and _stage_cache_valid(vlm_json, vlm_meta):
        vlm_analysis = _load_json(vlm_json)
        log(f"跳过 VLM 分析（已存在 {len(vlm_analysis)} 个场景）")
    else:
        if not CONFIG.get("api_key"):
            key_name = CONFIG.get("api_key_source", "MIMO_API_KEY")
            raise SystemExit(f"请设置 {key_name} 环境变量（VLM 画面分析需要）")
        log("VLM API 连通性预检...")
        api_call({"model": CONFIG.get("vlm_model", ""),
                  "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5})
        vlm_analysis = analyze_scenes(scenes, frames, work_dir, resume=not args.force)
        _write_stage_meta(vlm_json, vlm_meta)

    # Step 4.1: optional MiMo scene-chunk video understanding
    overview_path = work_dir / "mimo_video_overview.json"
    if CONFIG.get("mimo_video_overview", False):
        if not CONFIG.get("mimo_video_api_key"):
            log("跳过 MiMo 分片视频概览：未设置 MIMO_API_KEY")
            overview_path.unlink(missing_ok=True)
            _write_mimo_overview_status(
                work_dir, "skipped_no_key", "未设置 MIMO_API_KEY，MiMo 分片视频概览未运行", None,
            )
        elif mimo_video_overview_cache_fresh(overview_path, video, scenes):
            log("跳过 MiMo 分片视频概览（缓存匹配）")
            _write_mimo_overview_status(work_dir, "cached", "缓存匹配", overview_path.name)
        else:
            overview_path.unlink(missing_ok=True)
            try:
                overview = analyze_video_overview(video, work_dir, scenes)
            except Exception as e:
                log(f"MiMo 分片视频概览失败（忽略）: {e}")
                _write_mimo_overview_status(work_dir, "failed", e, None)
            else:
                if overview_path.exists() and overview:
                    _write_mimo_overview_status(work_dir, "ok", "MiMo 分片视频概览完成", overview_path.name)
                else:
                    _write_mimo_overview_status(work_dir, "failed", "MiMo 分片视频概览未产出有效 artifact", None)
    else:
        overview_path.unlink(missing_ok=True)
        _write_mimo_overview_status(work_dir, "disabled", "MiMo 分片视频概览未启用", None, enabled=False)

    # Make the video-overview the primary per-scene description (frame_facts stay the anchor).
    # No-op/revert when overview is absent, so disabling it cleanly returns to frame descriptions.
    vlm_analysis = _merge_overview_into_scenes(vlm_analysis, overview_path)

    # optional consolidation (整理): build the understanding index before the brief folds it in
    if args.consolidate or args.consolidate_asr:
        from consolidate import consolidate
        try:
            consolidate(work_dir, do_asr=args.consolidate_asr, do_index=args.consolidate)
        except Exception as e:
            log(f"consolidate 跳过（忽略）: {e}")
            _write_consolidation_status(
                work_dir, "failed", e, _present_consolidation_artifacts(work_dir),
                do_asr=args.consolidate_asr, do_index=args.consolidate,
            )
        else:
            expected = []
            skipped = []
            if args.consolidate:
                if vlm_analysis:
                    expected.append("understanding_index.json")
                else:
                    skipped.append("无 vlm_analysis，跳过 index")
            if args.consolidate_asr:
                if asr_result:
                    expected.append("asr_clean.json")
                else:
                    skipped.append("无 ASR 文本，跳过 ASR 清洗")
            artifacts = _present_consolidation_artifacts(work_dir)
            missing = [name for name in expected if name not in artifacts]
            if missing:
                _write_consolidation_status(
                    work_dir, "failed", f"未产出预期 artifact: {', '.join(missing)}", artifacts,
                    do_asr=args.consolidate_asr, do_index=args.consolidate,
                )
            elif expected:
                _write_consolidation_status(
                    work_dir, "ok", "consolidation 完成", artifacts,
                    do_asr=args.consolidate_asr, do_index=args.consolidate,
                )
            else:
                _write_consolidation_status(
                    work_dir, "skipped", "；".join(skipped) or "无可整理输入", artifacts,
                    do_asr=args.consolidate_asr, do_index=args.consolidate,
                )
    else:
        _write_consolidation_status(
            work_dir, "disabled", "consolidation 未启用", [], enabled=False,
            do_asr=False, do_index=False,
        )

    # Storyboard contact sheets (advisory, never blocking). Source uses scene anchors over the
    # source timeline; edited is gated on clip_plan_validated.json file-presence (NOT edit_mode —
    # recap.py forwards --edit-mode cut in BOTH passes, so the validated plan is the only reliable
    # pass2 signal). Both cache via _write_stage_meta/_stage_cache_valid with fps + frame-set in the key.
    source_storyboard = _generate_source_storyboard(work_dir, Path(video), scenes, scenes_json, force=args.force)
    edited_storyboard = _generate_edited_storyboard(work_dir, video, force=args.force)
    cut_mode = (work_dir / "clip_plan_validated.json").exists()

    # understanding substrate warning + writing brief
    substrate = assess_understanding_substrate(vlm_analysis, asr_result)
    if substrate["level"] != "rich":
        banner = "理解素材为空" if substrate["level"] == "empty" else "理解素材偏薄"
        log(f"⚠️  {banner}：ASR {substrate['asr_chars']} 字 | 场景 {substrate['scene_count']} | "
            f"带 frame_facts 的场景 {substrate['scenes_with_frame_facts']} | 平均画面描述 {substrate['avg_description_len']} 字")
    brief_path = build_agent_brief(
        vlm_analysis, asr_result, silence_periods, video_duration, work_dir, args.style,
        mimo_overview_enabled=CONFIG.get("mimo_video_overview", False),
        mimo_overview_video_path=video,
    )
    # C1: post-process the RETURNED brief FILE (not brief.py) so the brief⇄narration twin stays
    # byte-identical. Prepends a storyboard header pointing the agent at the sheet(s).
    _prepend_storyboard_brief_header(brief_path, source_storyboard, edited_storyboard, cut_mode=cut_mode)

    log("=" * 50)
    log(f"理解完成。写作 brief: {brief_path}")
    print(json.dumps({
        "status": "analyzed",
        "work_dir": str(work_dir),
        "brief": str(brief_path),
        "substrate": substrate["level"],
        "scenes": len(scenes),
        "asr_segments": len(asr_result),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
