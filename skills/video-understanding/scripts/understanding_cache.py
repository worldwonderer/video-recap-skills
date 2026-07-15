"""Own understanding-stage cache keys, provenance, and statuses."""

import hashlib

import json

from pathlib import Path

from lib import CONFIG, log, file_fingerprint, load_prompt


from vlm import (
    _is_mimo_chunk_usable,
)


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
    meta_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


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


def _write_mimo_overview_status(
    work_dir, status, message="", artifact=None, *, enabled=None
):
    return _write_optional_stage_status(
        work_dir,
        "mimo_video_overview.status.json",
        {
            "stage": "mimo_video_overview",
            "enabled": bool(CONFIG.get("mimo_video_overview", False))
            if enabled is None
            else bool(enabled),
            "status": status,
            "message": message,
            "artifact": artifact,
        },
    )


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
        log(
            f"已用 MiMo 视频概览增强 {enriched} 个场景的描述（逐帧 frame_facts 保留作锚点）"
        )
    return scenes


def _write_consolidation_status(
    work_dir,
    status,
    message="",
    artifacts=None,
    *,
    enabled=True,
    do_asr=False,
    do_index=True,
):
    return _write_optional_stage_status(
        work_dir,
        "consolidation.status.json",
        {
            "stage": "consolidation",
            "enabled": bool(enabled),
            "do_asr": bool(do_asr),
            "do_index": bool(do_index),
            "status": status,
            "message": message,
            "artifacts": list(artifacts or []),
        },
    )


def _present_consolidation_artifacts(work_dir):
    work_dir = Path(work_dir)
    return [
        name
        for name in ("understanding_index.json", "asr_clean.json")
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
        "asr_meta": _load_json(_artifact_meta_path(asr_json))
        if _artifact_meta_path(asr_json).exists()
        else None,
        "settings": {
            "silence_noise_threshold": CONFIG.get("silence_noise_threshold"),
            "silence_min_duration": CONFIG.get("silence_min_duration"),
            "quiet_window_min": CONFIG.get("quiet_window_min"),
            "silence_merge_gap": CONFIG.get("silence_merge_gap"),
            "source_boundary_noise_threshold": CONFIG.get(
                "source_boundary_noise_threshold"
            ),
            "source_boundary_min_pause": CONFIG.get("source_boundary_min_pause"),
            "source_boundary_max_alignment_error": CONFIG.get(
                "source_boundary_max_alignment_error"
            ),
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
            "background_research_fingerprint": _artifact_fingerprint(
                Path(work_dir) / "background_research.json"
            ),
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
            name: file_fingerprint(frame) for name, frame in zip(frame_names, frames)
        },
    }


def _write_frames_manifest(work_dir, video_path, fps, frames):
    manifest_path = _frames_manifest_path(work_dir)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            _frame_cache_payload(video_path, fps, frames), ensure_ascii=False, indent=2
        ),
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
