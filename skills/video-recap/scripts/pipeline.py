import json
import os
import shutil
import shlex
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from config import CONFIG
from common import (
    api_call,
    file_fingerprint,
    get_video_duration,
    log,
    run_cmd,
    stable_hash,
    video_fingerprint,
)
from extract import extract_frames
from detect import detect_scenes, detect_silence_periods
from asr import transcribe_audio
from vlm import analyze_scenes, analyze_video_overview, mimo_video_settings_fingerprint
from narration import (
    build_agent_brief,
    validate_narration_or_raise,
    _validate_narration_budget,
    _align_narration_to_quiet,
    assess_understanding_substrate,
)
from tts import SUPPORTED_TTS_ENGINES, resolve_tts_engine, synthesize_tts, tts_settings_fingerprint
from assemble import assemble_video, assembly_settings_fingerprint
from edit import (
    build_edited_source_video,
    load_clip_plan,
    map_narration_to_clips,
    normalize_clip_plan,
    parse_duration_seconds,
)

# ── Workflow state / prerequisites ────────────────────────────────────

STATE_SCHEMA_VERSION = 1


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _workflow_state_path(work_dir):
    return work_dir / "workflow_state.json"


def _load_workflow_state(work_dir):
    path = _workflow_state_path(work_dir)
    if not path.exists():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log(f"警告: workflow_state.json 读取失败，重建状态: {exc}")
        return {}
    return state if isinstance(state, dict) else {}


def _write_workflow_state(work_dir, state):
    path = _workflow_state_path(work_dir)
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _ensure_workflow_state(work_dir):
    state = _load_workflow_state(work_dir)
    if not state:
        state = {
            "schema_version": STATE_SCHEMA_VERSION,
            "started_at": _utc_now(),
            "steps": {},
        }
    state.setdefault("schema_version", STATE_SCHEMA_VERSION)
    state.setdefault("started_at", _utc_now())
    state.setdefault("steps", {})
    return state


def _clear_legacy_step_markers(work_dir):
    for marker in Path(work_dir).glob(".step_*.done"):
        marker.unlink(missing_ok=True)


def _init_workflow_state(work_dir, input_video):
    """Initialize workflow_state.json and invalidate stale markers on video changes."""
    state = _ensure_workflow_state(work_dir)
    try:
        current_fp = video_fingerprint(input_video)
    except OSError as exc:
        log(f"警告: 无法计算视频内容指纹: {exc}")
        current_fp = None
    previous_fp = state.get("video_fingerprint")
    if previous_fp and current_fp and previous_fp != current_fp:
        state["invalidated_at"] = _utc_now()
        state["previous_video_fingerprint"] = previous_fp
        state["steps"] = {}
        _clear_legacy_step_markers(work_dir)
        log("检测到输入视频内容指纹变化，已清空旧步骤状态")
    state["input_video"] = str(input_video)
    if current_fp:
        state["video_fingerprint"] = current_fp
    _write_workflow_state(work_dir, state)
    return state


def _step_state_entry(work_dir, step_name):
    return (_load_workflow_state(work_dir).get("steps") or {}).get(step_name)


def _set_step_state(work_dir, step_name, status, *, progress=None, params=None, err_msg=None):
    state = _ensure_workflow_state(work_dir)
    steps = state.setdefault("steps", {})
    now = _utc_now()
    entry = dict(steps.get(step_name) or {})
    entry["status"] = status
    if params is not None:
        entry["params"] = params
        entry["params_fingerprint"] = stable_hash(params)
    if status == "processing":
        entry["started_at"] = now
        entry.pop("completed_at", None)
        entry.pop("elapsed_s", None)
        entry.pop("err_msg", None)
        entry["progress"] = 0 if progress is None else progress
    elif status == "done":
        entry.setdefault("started_at", now)
        entry["completed_at"] = now
        try:
            started = datetime.fromisoformat(entry["started_at"].replace("Z", "+00:00"))
            completed = datetime.fromisoformat(now.replace("Z", "+00:00"))
            entry["elapsed_s"] = round((completed - started).total_seconds(), 2)
        except (TypeError, ValueError):
            pass
        entry["progress"] = 100
        entry.pop("err_msg", None)
    elif status == "error":
        entry.setdefault("started_at", now)
        entry["completed_at"] = now
        entry["progress"] = progress if progress is not None else entry.get("progress", 0)
        entry["err_msg"] = str(err_msg or "")
    elif status == "wait":
        entry.setdefault("progress", 0)
        if err_msg:
            entry["err_msg"] = str(err_msg)
    steps[step_name] = entry
    state["updated_at"] = now
    _write_workflow_state(work_dir, state)
    return entry


def _step_started(work_dir, step_name, params=None):
    return _set_step_state(work_dir, step_name, "processing", params=params)


def _step_failed(work_dir, step_name, exc):
    return _set_step_state(work_dir, step_name, "error", err_msg=exc)


def _run_stateful_step(work_dir, step_name, func, *, params=None):
    _step_started(work_dir, step_name, params=params)
    try:
        result = func()
    except Exception as exc:
        _step_failed(work_dir, step_name, exc)
        raise
    _step_done(work_dir, step_name, params=params)
    return result


def _step_done(work_dir, step_name, params=None):
    """标记步骤完成"""
    (work_dir / f".step_{step_name}.done").write_text("ok")
    _set_step_state(work_dir, step_name, "done", params=params)


def _is_step_done(work_dir, step_name, params=None):
    """检查步骤是否已完成"""
    entry = _step_state_entry(work_dir, step_name)
    if entry:
        if entry.get("status") != "done":
            return False
        if params is not None and entry.get("params_fingerprint") != stable_hash(params):
            return False
        return True
    legacy_marker = work_dir / f".step_{step_name}.done"
    if legacy_marker.exists():
        _set_step_state(work_dir, step_name, "done", params=params)
        return True
    return False


def _command_available(command):
    """Return True when command is an existing path or resolvable on PATH."""
    if not command:
        return False
    if os.path.sep in command or (os.path.altsep and os.path.altsep in command):
        return os.path.exists(command)
    return shutil.which(command) is not None


def check_prerequisites(skip_asr=False):
    """检查依赖"""
    checks = {
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "ffprobe": shutil.which("ffprobe") is not None,
    }
    if not skip_asr:
        if not CONFIG.get("asr_model_dir"):
            log("未设置 ASR_MODEL_DIR，ASR 步骤失败时将继续无 ASR")
        else:
            checks["asr_binary"] = _command_available(CONFIG["asr_bin"])
            checks["asr_model"] = os.path.exists(CONFIG["asr_model_dir"])

    missing = [k for k, v in checks.items() if not v]
    if missing:
        log(f"缺少依赖: {', '.join(missing)}")
        return False

    log("依赖检查通过")
    return True


def _load_json_file(path, label):
    """Load a required pipeline JSON artifact with a clear error message."""
    try:
        return json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"缺少 {label}: {path}") from exc


def _ffmpeg_has_filter(filter_name):
    result = run_cmd(["ffmpeg", "-hide_banner", "-filters"])
    if result.returncode != 0:
        return False
    marker = f" {filter_name} "
    return any(marker in line for line in result.stdout.splitlines())


def _cut_mode_enabled():
    return CONFIG.get("edit_mode", "full") == "cut"


def _run_settings_path(work_dir):
    return work_dir / "run_settings.json"


def _persist_run_settings(work_dir):
    settings = {
        "api_provider": CONFIG.get("api_provider", "openai"),
        "api_url": CONFIG.get("api_url"),
        "vlm_model": CONFIG.get("vlm_model"),
        "tts_engine": CONFIG.get("tts_engine", "auto"),
        "edit_mode": CONFIG.get("edit_mode", "full"),
        "target_duration": CONFIG.get("target_duration", ""),
        "clip_padding": CONFIG.get("clip_padding", 0.0),
        "allow_clip_overlap": CONFIG.get("allow_clip_overlap", False),
        "burn_subtitles": CONFIG.get("burn_subtitles", False),
        # 解说/检测语义参数：resume 时若不显式重传，会被这里持久化的值恢复，
        # 避免 VLM/detect 因指纹不匹配而重跑。fps 持久化原始值（0=自动），
        # 同一视频在 resume 时会确定性地解析出相同的实际 fps。
        "context_info": CONFIG.get("context_info", ""),
        "fps": CONFIG.get("fps", 0),
        "scene_threshold": CONFIG.get("scene_threshold", 0.1),
        "style": CONFIG.get("style", "纪录片"),
        "mimo_api_url": CONFIG.get("mimo_api_url"),
        "mimo_video_api_url": CONFIG.get("mimo_video_api_url"),
        "mimo_tts_api_url": CONFIG.get("mimo_tts_api_url"),
        "mimo_model": CONFIG.get("mimo_model"),
        "mimo_video_model": CONFIG.get("mimo_video_model"),
        "mimo_tts_model": CONFIG.get("mimo_tts_model"),
        "mimo_tts_voice": CONFIG.get("mimo_tts_voice"),
        "mimo_tts_style": CONFIG.get("mimo_tts_style"),
        "mimo_video_overview": CONFIG.get("mimo_video_overview", False),
        "mimo_video_fps": CONFIG.get("mimo_video_fps", 2.0),
        "mimo_media_resolution": CONFIG.get("mimo_media_resolution", "default"),
        "mimo_disable_thinking": CONFIG.get("mimo_disable_thinking", True),
    }
    _run_settings_path(work_dir).write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    return settings


def _load_run_settings(work_dir):
    path = _run_settings_path(work_dir)
    if not path.exists():
        return {}
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log(f"警告: run_settings.json 读取失败，使用当前 CLI 配置: {exc}")
        return {}
    if not isinstance(settings, dict):
        return {}
    preserve_runtime_tts = CONFIG.get("tts_engine_source") in ("cli", "env")
    for key in (
        "api_provider", "api_url", "vlm_model", "tts_engine",
        "edit_mode", "target_duration", "clip_padding", "allow_clip_overlap", "burn_subtitles",
        "context_info", "fps", "scene_threshold", "style",
        "mimo_api_url", "mimo_video_api_url", "mimo_tts_api_url", "mimo_model", "mimo_video_model",
        "mimo_tts_model", "mimo_tts_voice", "mimo_tts_style",
        "mimo_video_overview", "mimo_video_fps", "mimo_media_resolution",
        "mimo_disable_thinking",
    ):
        if key in settings and settings[key] is not None:
            if key == "tts_engine" and preserve_runtime_tts:
                continue
            if key != "tts_engine" and _has_runtime_override(key):
                continue
            CONFIG[key] = settings[key]
            if key == "tts_engine":
                CONFIG["tts_engine_source"] = "run_settings"
    return settings



def _has_runtime_override(key):
    """Return True when a CLI/env value should beat persisted run settings."""
    source = CONFIG.get(f"{key}_source")
    if source in ("cli", "env"):
        return True
    if key in ("mimo_video_api_url", "mimo_tts_api_url"):
        return CONFIG.get("mimo_api_url_source") in ("cli", "env")
    return False


def _merge_run_settings(work_dir):
    """Load persisted settings, but keep explicit one-way CLI enables."""
    explicit_enables = {
        "burn_subtitles": bool(CONFIG.get("burn_subtitles", False)),
        "allow_clip_overlap": bool(CONFIG.get("allow_clip_overlap", False)),
    }
    settings = _load_run_settings(work_dir)
    if (
        CONFIG.get("tts_engine_source") == "run_settings"
        and CONFIG.get("tts_engine") == "edge-tts"
        and CONFIG.get("mimo_tts_api_key")
    ):
        CONFIG["tts_engine"] = "auto"
        CONFIG["tts_engine_source"] = "default"
        settings["tts_engine"] = "auto"
        log("检测到 MiMo TTS key，resume 默认优先 MiMo TTS；如需复用 edge-tts 请显式传 --tts edge-tts")
    for key, enabled in explicit_enables.items():
        if enabled:
            CONFIG[key] = True
            settings[key] = True
    return settings


def _mimo_video_overview_current(work_dir):
    """Return True only for the current scene-chunk MiMo overview artifact format."""
    if not _is_step_done(work_dir, "mimo_video_overview"):
        return False
    path = work_dir / "mimo_video_overview.json"
    if not path.exists():
        return False
    try:
        overview = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return (
        overview.get("input") == "scene_chunks"
        and bool(overview.get("chunks"))
        and overview.get("settings") == mimo_video_settings_fingerprint()
    )


def _resume_command(cli_path, video_path, work_dir):
    parts = ["python3", str(cli_path), str(video_path), "--resume", str(work_dir)]
    if _cut_mode_enabled():
        parts.extend(["--edit-mode", "cut"])
        if CONFIG.get("target_duration"):
            parts.extend(["--target-duration", str(CONFIG["target_duration"])])
        try:
            clip_padding = float(CONFIG.get("clip_padding", 0.0) or 0.0)
        except (TypeError, ValueError):
            clip_padding = 0.0
        if clip_padding > 0:
            parts.extend(["--clip-padding", f"{clip_padding:g}"])
        if CONFIG.get("allow_clip_overlap", False):
            parts.append("--allow-clip-overlap")
    if CONFIG.get("burn_subtitles", False):
        parts.append("--burn-subtitles")
    if CONFIG.get("tts_engine") and CONFIG.get("tts_engine") != "auto":
        parts.extend(["--tts", str(CONFIG["tts_engine"])])
    context_info = CONFIG.get("context_info") or ""
    if context_info:
        parts.extend(["--context", str(context_info)])
    style = CONFIG.get("style", "纪录片")
    if style and style != "纪录片":
        parts.extend(["--style", str(style)])
    try:
        scene_threshold = float(CONFIG.get("scene_threshold", 0.1))
    except (TypeError, ValueError):
        scene_threshold = 0.1
    if scene_threshold != 0.1:
        parts.extend(["--scene-threshold", f"{scene_threshold:g}"])
    try:
        fps = float(CONFIG.get("fps", 0) or 0)
    except (TypeError, ValueError):
        fps = 0.0
    if fps > 0:
        parts.extend(["--fps", f"{fps:g}"])
    return " ".join(shlex.quote(part) for part in parts)


def _target_duration_seconds():
    return parse_duration_seconds(CONFIG.get("target_duration"))


def _annotate_cut_narration_overlap(narration, silence_periods):
    """Preserve source timestamps, but correct overlaps_speech from source quiet windows."""
    quiet_windows = [w for w in silence_periods or [] if not w.get("has_speech", False)]
    if not quiet_windows:
        for seg in narration or []:
            if isinstance(seg, dict):
                seg["overlaps_speech"] = True
        return narration

    for seg in narration or []:
        if not isinstance(seg, dict):
            continue
        try:
            start = float(seg.get("start"))
            end = float(seg.get("end"))
        except (TypeError, ValueError):
            seg["overlaps_speech"] = True
            continue
        duration = max(0.0, end - start)
        quiet_overlap = 0.0
        for qw in quiet_windows:
            overlap_start = max(start, float(qw.get("start", 0)))
            overlap_end = min(end, float(qw.get("end", 0)))
            quiet_overlap += max(0.0, overlap_end - overlap_start)
        seg["overlaps_speech"] = quiet_overlap < max(0.3, duration * 0.5)
    return narration


def _prepare_cut_mode_artifacts(video_path, work_dir, narration, *, validate_budget=True):
    """Validate clip_plan.json, build edited_source.mp4, and map narration to output time."""
    params = _cut_edit_params(video_path, work_dir)
    _step_started(work_dir, "edit", params=params)
    try:
        result = _prepare_cut_mode_artifacts_impl(video_path, work_dir, narration, validate_budget=validate_budget)
    except Exception as exc:
        _step_failed(work_dir, "edit", exc)
        raise
    _step_done(work_dir, "edit", params=params)
    return result


def _prepare_cut_mode_artifacts_impl(video_path, work_dir, narration, *, validate_budget=True):
    clip_plan_path = work_dir / "clip_plan.json"
    raw_plan = load_clip_plan(clip_plan_path)
    validated_plan = normalize_clip_plan(
        raw_plan,
        get_video_duration(video_path),
        target_duration=_target_duration_seconds(),
        clip_padding=CONFIG.get("clip_padding", 0.0),
        allow_overlap=bool(CONFIG.get("allow_clip_overlap", False)),
    )
    validated_path = work_dir / "clip_plan_validated.json"
    validated_path.write_text(json.dumps(validated_plan, ensure_ascii=False, indent=2), encoding="utf-8")

    edited_source_path = work_dir / "edited_source.mp4"
    source_mtime = max(
        clip_plan_path.stat().st_mtime,
        (work_dir / "narration.json").stat().st_mtime if (work_dir / "narration.json").exists() else 0,
    )
    if edited_source_path.exists() and edited_source_path.stat().st_mtime >= clip_plan_path.stat().st_mtime:
        edited_source = edited_source_path
        log(f"复用剪辑源视频: {edited_source}")
    else:
        edited_source = build_edited_source_video(video_path, validated_plan, work_dir, edited_source_path)
    mapped_narration = map_narration_to_clips(narration, validated_plan)
    if validate_budget:
        edited_scenes = [{
            "scene_id": c["clip_id"],
            "start": c["output_start"],
            "end": c["output_end"],
            "description": c.get("reason", "selected source clip"),
        } for c in validated_plan["clips"]]
        mapped_narration = _validate_narration_budget(mapped_narration, edited_scenes)
    if not mapped_narration:
        raise ValueError("narration.json 没有落入 clip_plan.json 片段内的有效解说")
    mapped_path = work_dir / "narration_mapped.json"
    mapped_path.write_text(json.dumps(mapped_narration, ensure_ascii=False, indent=2), encoding="utf-8")
    if mapped_path.stat().st_mtime < source_mtime:
        mapped_path.touch()
    log(f"剪辑模式: {len(validated_plan['clips'])} 个片段 → {validated_plan['total_duration']:.1f}s")
    return edited_source, mapped_narration, validated_plan


def _cut_artifacts_current(work_dir):
    if not _cut_mode_enabled():
        return True
    clip_plan_path = work_dir / "clip_plan.json"
    narration_path = work_dir / "narration.json"
    validated_path = work_dir / "clip_plan_validated.json"
    mapped_path = work_dir / "narration_mapped.json"
    edited_path = work_dir / "edited_source.mp4"
    required = [clip_plan_path, narration_path, validated_path, mapped_path, edited_path]
    if not all(path.exists() for path in required):
        return False
    clip_mtime = clip_plan_path.stat().st_mtime
    narration_mtime = narration_path.stat().st_mtime
    return (
        validated_path.stat().st_mtime >= clip_mtime
        and edited_path.stat().st_mtime >= clip_mtime
        and mapped_path.stat().st_mtime >= max(clip_mtime, narration_mtime)
    )


def _artifact_current(output_path, source_paths):
    if not output_path.exists():
        return False
    existing_sources = [Path(path) for path in source_paths if path and Path(path).exists()]
    if not existing_sources:
        return True
    return output_path.stat().st_mtime >= max(path.stat().st_mtime for path in existing_sources)


def _artifact_fingerprints(paths):
    fingerprints = []
    for index, raw in enumerate(paths or []):
        if not raw:
            continue
        path = Path(raw)
        if not path.exists():
            continue
        try:
            fingerprints.append({"index": index, "fingerprint": file_fingerprint(path)})
        except OSError as exc:
            log(f"警告: 无法计算产物输入指纹 {path}: {exc}")
    return fingerprints


def _optional_file_fingerprint(path):
    path = Path(path)
    if not path.exists():
        return None
    try:
        return file_fingerprint(path)
    except OSError as exc:
        log(f"警告: 无法计算文件指纹 {path}: {exc}")
        return None


def _script_step_params(work_dir, *, cut_mode, style):
    return {
        "edit_mode": CONFIG.get("edit_mode", "full"),
        "style": style,
        "target_duration": CONFIG.get("target_duration"),
        "allow_clip_overlap": bool(CONFIG.get("allow_clip_overlap", False)),
        "vlm_analysis_fingerprint": _optional_file_fingerprint(work_dir / "vlm_analysis.json"),
        "asr_result_fingerprint": _optional_file_fingerprint(work_dir / "asr_result.json"),
        "silence_periods_fingerprint": _optional_file_fingerprint(work_dir / "silence_periods.json"),
        "narration_fingerprint": _optional_file_fingerprint(work_dir / "narration.json"),
        "clip_plan_fingerprint": _optional_file_fingerprint(work_dir / "clip_plan.json") if cut_mode else None,
    }


def _extract_step_params():
    return {"fps": CONFIG["fps"]}


def _detect_step_params(scene_threshold):
    return {
        "scene_threshold": scene_threshold,
        "scene_merge_min": CONFIG.get("scene_merge_min", 4.0),
        "scene_junk_filter": CONFIG.get("scene_junk_filter", True),
        "scene_junk_dark_luma": CONFIG.get("scene_junk_dark_luma", 8),
        "scene_junk_bright_luma": CONFIG.get("scene_junk_bright_luma", 245),
    }


def _asr_step_params(skip_asr):
    return {
        "skip_asr": skip_asr,
        "asr_segment_seconds": CONFIG.get("asr_segment_seconds"),
        "asr_bin": CONFIG.get("asr_bin"),
        "asr_model_dir": CONFIG.get("asr_model_dir"),
    }


def _cut_edit_params(video_path, work_dir):
    return {
        "input_video_fingerprint": video_fingerprint(video_path),
        "clip_plan_fingerprint": _optional_file_fingerprint(work_dir / "clip_plan.json"),
        "narration_fingerprint": _optional_file_fingerprint(work_dir / "narration.json"),
        "target_duration": CONFIG.get("target_duration"),
        "clip_padding": CONFIG.get("clip_padding", 0.0),
        "allow_clip_overlap": bool(CONFIG.get("allow_clip_overlap", False)),
    }


def _tts_step_params(narration_artifact_path):
    params = {
        "narration_fingerprint": _optional_file_fingerprint(narration_artifact_path),
        "requested_engine": CONFIG.get("tts_engine"),
    }
    try:
        engine = resolve_tts_engine()
        params["resolved_engine"] = engine
        params["settings"] = tts_settings_fingerprint(engine)
    except RuntimeError as exc:
        params["resolve_error"] = str(exc)
    return params


def _synthesize_tts_stateful(work_dir, narration, narration_artifact_path):
    params = _tts_step_params(narration_artifact_path)
    _step_started(work_dir, "tts", params=params)
    try:
        tts_segments, engine_used = synthesize_tts(narration, work_dir)
        (work_dir / "tts_meta.json").write_text(
            json.dumps(
                _tts_meta_payload(tts_segments, engine_used, [narration_artifact_path]),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception as exc:
        _step_failed(work_dir, "tts", exc)
        raise
    _step_done(work_dir, "tts", params=params)
    return tts_segments, engine_used


def _assemble_step_params(input_video, source_paths):
    return {
        "input_video_fingerprint": video_fingerprint(input_video),
        "source_fingerprints": _artifact_fingerprints(source_paths),
        "settings": assembly_settings_fingerprint(),
    }


def _assemble_video_stateful(work_dir, input_video, tts_segments, output_path, source_paths):
    params = _assemble_step_params(input_video, source_paths)
    _step_started(work_dir, "assemble", params=params)
    try:
        assemble_video(input_video, tts_segments, work_dir, output_path)
        _write_assemble_meta_with_sources(work_dir, input_video, source_paths)
    except Exception as exc:
        _step_failed(work_dir, "assemble", exc)
        raise
    _step_done(work_dir, "assemble", params=params)


def _assemble_meta_path(work_dir):
    return work_dir / "assemble_meta.json"


def _write_assemble_meta(work_dir, input_video):
    meta = {
        "input_video": str(input_video),
        "input_video_fingerprint": video_fingerprint(input_video),
        "settings": assembly_settings_fingerprint(),
    }
    _assemble_meta_path(work_dir).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def _assemble_settings_current(work_dir, input_video):
    path = _assemble_meta_path(work_dir)
    if not path.exists():
        return False
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return (
        (
            meta.get("input_video_fingerprint") == video_fingerprint(input_video)
            if meta.get("input_video_fingerprint")
            else meta.get("input_video") == str(input_video)
        )
        and meta.get("settings") == assembly_settings_fingerprint()
    )


def _assemble_artifact_current(work_dir, output_path, source_paths, input_video):
    if not output_path.exists() or not _assemble_settings_current(work_dir, input_video):
        return False
    try:
        meta = json.loads(_assemble_meta_path(work_dir).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    meta_sources = meta.get("source_fingerprints")
    if meta_sources is not None:
        return meta_sources == _artifact_fingerprints(source_paths)
    return _artifact_current(output_path, source_paths)


def _write_assemble_meta_with_sources(work_dir, input_video, source_paths):
    meta = _write_assemble_meta(work_dir, input_video)
    meta["source_fingerprints"] = _artifact_fingerprints(source_paths)
    _assemble_meta_path(work_dir).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def _clear_tts_cache(work_dir):
    shutil.rmtree(work_dir / "tts_segments", ignore_errors=True)
    for path in (work_dir / "tts_meta.json", work_dir / ".step_tts.done"):
        path.unlink(missing_ok=True)
    _set_step_state(work_dir, "tts", "wait", err_msg="cache cleared")
    _set_step_state(work_dir, "assemble", "wait", err_msg="tts cache cleared")


def _tts_meta_payload(tts_segments, engine_used, source_paths=None):
    payload = {
        "segments": tts_segments,
        "engine": engine_used,
        "settings": tts_settings_fingerprint(engine_used),
    }
    if source_paths is not None:
        payload["source_fingerprints"] = _artifact_fingerprints(source_paths)
    return payload


def _tts_meta_current(tts_meta, narration_artifact_path):
    if not tts_meta.exists():
        return False
    try:
        tts_info = json.loads(tts_meta.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    narration_sources = tts_info.get("source_fingerprints")
    if narration_sources is not None:
        if narration_sources != _artifact_fingerprints([narration_artifact_path]):
            return False
    elif not _artifact_current(tts_meta, [narration_artifact_path]):
        return False
    existing_engine = tts_info.get("engine")
    try:
        desired_engine = resolve_tts_engine(prefer_existing=existing_engine)
    except RuntimeError:
        return existing_engine in SUPPORTED_TTS_ENGINES
    if existing_engine != desired_engine:
        return False
    existing_settings = tts_info.get("settings")
    if existing_settings is None:
        return True
    return existing_settings == tts_settings_fingerprint(desired_engine)


def _ensure_cut_tail_artifacts(video_path, work_dir):
    if _cut_mode_enabled() and not _cut_artifacts_current(work_dir):
        narration = _load_json_file(work_dir / "narration.json", "narration.json")
        _prepare_cut_mode_artifacts(video_path, work_dir, narration, validate_budget=False)


def _load_tail_narration(work_dir):
    if _cut_mode_enabled() and (work_dir / "narration_mapped.json").exists():
        return _load_json_file(work_dir / "narration_mapped.json", "narration_mapped.json")
    return _load_json_file(work_dir / "narration.json", "narration.json")


def _tail_video_path(video_path, work_dir):
    edited = work_dir / "edited_source.mp4"
    if _cut_mode_enabled() and edited.exists():
        return edited
    return video_path


def _final_output_path(video_path, work_dir, output_dir):
    """最终成片路径：指定 output_dir 时放其中，否则放 work_dir 的上级目录。"""
    base = Path(output_dir) if output_dir else work_dir.parent
    return base / f"recap_{video_path.stem}.mp4"


def _run_cached_tail_step(video_path, work_dir, step, output_dir):
    """Run tts/assemble from existing artifacts without VLM/API prerequisites."""
    if step not in ("tts", "assemble"):
        return None

    if _cut_mode_enabled():
        _ensure_cut_tail_artifacts(video_path, work_dir)

    narration_artifact_path = work_dir / "narration_mapped.json" if _cut_mode_enabled() else work_dir / "narration.json"

    if step == "tts":
        _clear_tts_cache(work_dir)
        narration = _load_tail_narration(work_dir)
        tts_segments, engine_used = _synthesize_tts_stateful(work_dir, narration, narration_artifact_path)
        log(f"步骤 tts 完成 ({len(tts_segments)} 段, 引擎: {engine_used})")
        return {"segments": tts_segments, "engine": engine_used}

    tts_meta = work_dir / "tts_meta.json"
    if _tts_meta_current(tts_meta, narration_artifact_path):
        tts_info = _load_json_file(tts_meta, "tts_meta.json")
        tts_segments = tts_info["segments"]
    else:
        _clear_tts_cache(work_dir)
        narration = _load_tail_narration(work_dir)
        tts_segments, engine_used = _synthesize_tts_stateful(work_dir, narration, narration_artifact_path)

    output_path = work_dir / "output.mp4"
    assembly_input = _tail_video_path(video_path, work_dir)
    if _is_step_done(work_dir, "assemble") and _assemble_artifact_current(
        work_dir, output_path, [tts_meta, assembly_input], assembly_input
    ):
        log("跳过视频组装（已存在）")
    else:
        _assemble_video_stateful(work_dir, assembly_input, tts_segments, output_path, [tts_meta, assembly_input])

    final_output = _final_output_path(video_path, work_dir, output_dir)
    if final_output != output_path:
        shutil.copy2(str(output_path), str(final_output))
    log(f"步骤 assemble 完成: {final_output}")
    return {"output": str(final_output), "work_dir": str(work_dir)}


# ── Main Pipeline ─────────────────────────────────────────────────────

def run_pipeline(video_path, output_dir=None, step=None, style="纪录片",
                 scene_threshold=None, skip_asr=False, resume_dir=None):
    """执行完整的视频解说 pipeline"""
    pipeline_start = time.time()
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(f"视频不存在: {video_path}")

    # 工作目录
    if resume_dir:
        work_dir = Path(resume_dir)
    else:
        output_dir = Path(output_dir or video_path.parent / "output")
        output_dir.mkdir(exist_ok=True)
        work_dir = output_dir / f"work_{int(time.time())}"

    # style/scene_threshold 既可由 CLI 传入，也可能为 None（未显式传）。
    # 落地到 CONFIG 后，_persist_run_settings 才能持久化，fresh run 也用同一套有效默认。
    if style is not None:
        CONFIG["style"] = style
    else:
        style = CONFIG.get("style", "纪录片")
    if scene_threshold is not None:
        CONFIG["scene_threshold"] = scene_threshold
    # 解析成有效阈值（detect_scenes 在 None 时也会回落到 CONFIG），
    # 让 fresh run 和 resume 的 detect 指纹一致，避免恢复后重跑场景检测。
    scene_threshold = CONFIG.get("scene_threshold", 0.1)

    work_dir.mkdir(exist_ok=True)
    if resume_dir:
        _merge_run_settings(work_dir)
        # resume 路径下，恢复后的 scene_threshold/style 存在 CONFIG 里，
        # 重新读出以让本函数后续用上恢复值（其余 fps/context_info 直接读 CONFIG）。
        scene_threshold = CONFIG.get("scene_threshold", scene_threshold)
        style = CONFIG.get("style", style)
    else:
        _persist_run_settings(work_dir)
    _init_workflow_state(work_dir, video_path)
    if not check_prerequisites(skip_asr=skip_asr):
        sys.exit(1)
    if CONFIG.get("burn_subtitles", False) and not _ffmpeg_has_filter("subtitles"):
        raise RuntimeError(
            "当前 ffmpeg 未启用 subtitles/libass 滤镜，无法压制字幕；"
            "请安装带 libass/subtitles 支持的 ffmpeg，或去掉 --burn-subtitles"
        )
    log(f"工作目录: {work_dir}")
    log(f"输入视频: {video_path}")
    log(f"成片模式: {CONFIG.get('edit_mode', 'full')}")

    # 如果指定了 step，只执行那一步
    # 仅含可直接调度的前置步骤；tts/assemble 走 _run_cached_tail_step，script 走 stop_after_script
    steps = {
        "extract": lambda: extract_frames(video_path, work_dir),
        "detect": lambda: detect_scenes(video_path, work_dir, scene_threshold),
        "asr": lambda: transcribe_audio(video_path, work_dir) if not skip_asr else [],
    }

    # 动态 FPS（需要在 step dispatch 之前，--step extract 需要）
    video_duration = get_video_duration(video_path)
    if CONFIG["fps"] <= 0:
        CONFIG["fps"] = 2 if video_duration <= 60 else (1.5 if video_duration <= 300 else 1)

    stop_after_script = False
    if step:
        if step in ("extract", "detect", "asr"):
            params = {
                "extract": _extract_step_params(),
                "detect": _detect_step_params(scene_threshold),
                "asr": _asr_step_params(skip_asr),
            }[step]
            result = _run_stateful_step(work_dir, step, steps[step], params=params)
            log(f"步骤 {step} 完成")
            return result
        if step in ("tts", "assemble"):
            cached_result = _run_cached_tail_step(video_path, work_dir, step, output_dir)
            if cached_result is not None:
                return cached_result
        elif step == "script":
            stop_after_script = True
        else:
            log(f"步骤 {step} 需要完整 pipeline，自动运行全部步骤")

    # 完整 pipeline
    log("=" * 50)
    log("开始完整视频解说 pipeline")
    log("=" * 50)

    extract_params = _extract_step_params()
    detect_params = _detect_step_params(scene_threshold)
    asr_params = _asr_step_params(skip_asr)
    vlm_params = {
        "api_provider": CONFIG.get("api_provider"),
        "api_url": CONFIG.get("api_url"),
        "vlm_model": CONFIG.get("vlm_model"),
        "fps": CONFIG["fps"],
        "vlm_workers": CONFIG.get("vlm_workers"),
        "context_info": CONFIG.get("context_info"),
    }
    if (work_dir / "scenes.json").exists():
        vlm_params["scenes_fingerprint"] = file_fingerprint(work_dir / "scenes.json")
    needs_frame_vlm_api = not _is_step_done(work_dir, "vlm", vlm_params)
    needs_mimo_video_api = (
        CONFIG.get("mimo_video_overview", False)
        and not _mimo_video_overview_current(work_dir)
    )
    if not CONFIG.get("api_key") and needs_frame_vlm_api:
        key_name = CONFIG.get("api_key_source", "OPENAI_API_KEY")
        raise RuntimeError(f"请设置 {key_name} 环境变量")
    if not CONFIG.get("mimo_video_api_key") and needs_mimo_video_api:
        key_name = CONFIG.get("mimo_video_api_key_source", "MIMO_API_KEY")
        raise RuntimeError(f"请设置 {key_name} 环境变量用于 MiMo 视频分片理解")

    # API 连通性预检（避免跑完帧提取+ASR 才发现 API 不可用）
    if needs_frame_vlm_api:
        log("VLM API 连通性预检...")
        try:
            api_call({
                "model": CONFIG.get("vlm_model", ""),
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 5,
            })
            log("API 连通性预检通过")
        except RuntimeError as e:
            log(f"API 预检失败: {e}")
            raise
    if needs_mimo_video_api:
        log("MiMo 视频理解 API 将在分片阶段预检/调用")

    # 动态 FPS
    log(f"FPS: {CONFIG['fps']} (视频时长: {video_duration:.1f}s)")

    # Step 1: 帧提取
    if _is_step_done(work_dir, "extract", extract_params):
        frames = sorted((work_dir / "frames").glob("frame_*.jpg"))
        log(f"跳过帧提取（已存在 {len(frames)} 帧）")
    else:
        t0 = time.time()
        frames = _run_stateful_step(
            work_dir,
            "extract",
            lambda: extract_frames(video_path, work_dir),
            params=extract_params,
        )
        log(f"[{time.time()-t0:.1f}s] 帧提取完成")

    # Step 2: 场景检测
    if _is_step_done(work_dir, "detect", detect_params):
        scenes = json.loads((work_dir / "scenes.json").read_text())
        log(f"跳过场景检测（已存在 {len(scenes)} 个场景）")
    else:
        t0 = time.time()
        scenes = _run_stateful_step(
            work_dir,
            "detect",
            lambda: detect_scenes(video_path, work_dir, scene_threshold),
            params=detect_params,
        )
        log(f"[{time.time()-t0:.1f}s] 场景检测完成")

    # Step 3: ASR
    if _is_step_done(work_dir, "asr", asr_params):
        asr_result = json.loads((work_dir / "asr_result.json").read_text())
        log(f"跳过 ASR（已存在 {len(asr_result)} 段）")
    elif skip_asr:
        asr_result = []
        (work_dir / "asr_result.json").write_text(
            json.dumps(asr_result, ensure_ascii=False, indent=2))
        _step_done(work_dir, "asr", params=asr_params)
        log("跳过 ASR")
    else:
        t0 = time.time()
        _step_started(work_dir, "asr", params=asr_params)
        try:
            try:
                asr_result = transcribe_audio(video_path, work_dir)
            except Exception as e:
                log(f"ASR 失败（继续无 ASR）: {e}")
                asr_result = []
                (work_dir / "asr_result.json").write_text(
                    json.dumps(asr_result, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            _step_done(work_dir, "asr", params=asr_params)
        except Exception as exc:
            _step_failed(work_dir, "asr", exc)
            raise
        log(f"[{time.time()-t0:.1f}s] ASR 完成")

    # Step 3.5: 静音检测
    silence_params = {
        "silence_noise_threshold": CONFIG.get("silence_noise_threshold"),
        "silence_min_duration": CONFIG.get("silence_min_duration"),
        "quiet_window_min": CONFIG.get("quiet_window_min"),
        "silence_merge_gap": CONFIG.get("silence_merge_gap"),
        "asr_result_fingerprint": file_fingerprint(work_dir / "asr_result.json")
        if (work_dir / "asr_result.json").exists() else None,
    }
    if _is_step_done(work_dir, "silence", silence_params):
        silence_periods = json.loads((work_dir / "silence_periods.json").read_text())
        log(f"跳过静音检测（已存在 {len(silence_periods)} 个窗口）")
    else:
        t0 = time.time()
        silence_periods = _run_stateful_step(
            work_dir,
            "silence",
            lambda: detect_silence_periods(video_path, work_dir, asr_result),
            params=silence_params,
        )
        log(f"[{time.time()-t0:.1f}s] 静音检测完成")

    # Step 4: VLM 分析
    vlm_params["scenes_fingerprint"] = file_fingerprint(work_dir / "scenes.json") if (work_dir / "scenes.json").exists() else None
    if _is_step_done(work_dir, "vlm", vlm_params):
        vlm_analysis = json.loads((work_dir / "vlm_analysis.json").read_text())
        log(f"跳过 VLM 分析（已存在 {len(vlm_analysis)} 个场景）")
    else:
        t0 = time.time()
        vlm_analysis = _run_stateful_step(
            work_dir,
            "vlm",
            lambda: analyze_scenes(scenes, frames, work_dir),
            params=vlm_params,
        )
        log(f"[{time.time()-t0:.1f}s] VLM 分析完成")

    # Step 4.1: Optional MiMo scene-chunk video understanding
    if CONFIG.get("mimo_video_overview", False):
        if _mimo_video_overview_current(work_dir):
            log("跳过 MiMo 分片视频概览（已存在）")
        else:
            t0 = time.time()
            mimo_params = mimo_video_settings_fingerprint()
            _step_started(work_dir, "mimo_video_overview", params=mimo_params)
            try:
                overview = analyze_video_overview(video_path, work_dir, scenes)
            except Exception as exc:
                _step_failed(work_dir, "mimo_video_overview", exc)
                raise
            if overview is not None:
                _step_done(work_dir, "mimo_video_overview", params=mimo_params)
                log(f"[{time.time()-t0:.1f}s] MiMo 分片视频概览完成")
            else:
                _set_step_state(work_dir, "mimo_video_overview", "wait", params=mimo_params, err_msg="skipped")

    # Step 5: Agent-authored narration script and optional clip plan
    narration_path = work_dir / "narration.json"
    clip_plan_path = work_dir / "clip_plan.json"
    cut_mode = _cut_mode_enabled()
    required_ready = narration_path.exists() and (not cut_mode or clip_plan_path.exists())
    source_narration = None
    assembly_video_path = video_path
    validated_plan = None
    script_params = _script_step_params(work_dir, cut_mode=cut_mode, style=style)

    if _is_step_done(work_dir, "script", script_params):
        try:
            source_narration = _load_json_file(narration_path, "narration.json")
            clip_plan_for_lint = None
            if cut_mode and (work_dir / "clip_plan_validated.json").exists():
                clip_plan_for_lint = _load_json_file(work_dir / "clip_plan_validated.json", "clip_plan_validated.json")
            elif cut_mode and clip_plan_path.exists():
                clip_plan_for_lint = _load_json_file(clip_plan_path, "clip_plan.json")
            validate_narration_or_raise(
                source_narration, vlm_analysis, clip_plan=clip_plan_for_lint,
                mode=CONFIG.get("edit_mode", "full"), work_dir=work_dir,
            )
            source_narration = _validate_narration_budget(source_narration, vlm_analysis)
        except Exception as exc:
            _step_failed(work_dir, "script", exc)
            raise
        log(f"跳过解说词写作（已存在 {len(source_narration)} 段）")
    elif required_ready:
        _step_started(work_dir, "script", params=script_params)
        try:
            source_narration = _load_json_file(narration_path, "narration.json")
            clip_plan_for_lint = None
            if cut_mode:
                raw_plan_for_lint = load_clip_plan(clip_plan_path)
                clip_plan_for_lint = normalize_clip_plan(
                    raw_plan_for_lint, video_duration,
                    target_duration=_target_duration_seconds(),
                    clip_padding=CONFIG.get("clip_padding", 0.0),
                    allow_overlap=bool(CONFIG.get("allow_clip_overlap", False)),
                )
            validate_narration_or_raise(
                source_narration, vlm_analysis, clip_plan=clip_plan_for_lint,
                mode=CONFIG.get("edit_mode", "full"), work_dir=work_dir,
            )
            if cut_mode:
                source_narration = _validate_narration_budget(source_narration, vlm_analysis)
            else:
                # _align_narration_to_quiet ends with _validate_narration_budget, so the
                # budget+dedup pass runs exactly once here (previously it ran twice).
                source_narration = _align_narration_to_quiet(source_narration, vlm_analysis, silence_periods)
                narration_path.write_text(json.dumps(source_narration, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            _step_failed(work_dir, "script", exc)
            raise
        script_params = _script_step_params(work_dir, cut_mode=cut_mode, style=style)
        _step_done(work_dir, "script", params=script_params)
        log(f"Agent 解说词验证完成: {len(source_narration)} 段")
    if stop_after_script and source_narration is not None:
        return {
            "status": "script_validated",
            "work_dir": str(work_dir),
            "narration_segments": len(source_narration),
            "lint": str(work_dir / "narration_lint.json"),
        }
    elif source_narration is None:
        substrate = assess_understanding_substrate(vlm_analysis, asr_result)
        if substrate["level"] != "rich":
            log("=" * 50)
            banner = "理解素材为空" if substrate["level"] == "empty" else "理解素材偏薄"
            log(f"⚠️  {banner}：ASR {substrate['asr_chars']} 字 | 场景 {substrate['scene_count']} | "
                f"带 frame_facts 的场景 {substrate['scenes_with_frame_facts']} | 平均画面描述 {substrate['avg_description_len']} 字")
            log("    解说很可能流于泛泛的“看图说话”。建议先做背景调研写 background_research.json，"
                "并确认 ASR / VLM 是否正常产出；详见 brief 顶部提示。")
        brief_path = build_agent_brief(vlm_analysis, asr_result, silence_periods, video_duration, work_dir, style)
        log("=" * 50)
        log("⏸  Pipeline 在解说词步骤暂停")
        if cut_mode:
            log(f"   请 Agent 阅读 {brief_path} 后写入 {clip_plan_path} 和 {narration_path}")
            next_step = "write clip_plan.json and narration.json"
        else:
            log(f"   请 Agent 阅读 {brief_path} 后写入 {narration_path}")
            next_step = "write narration.json"
        cli_path = Path(__file__).with_name("video_recap.py")
        log("   写完后继续执行:")
        log(f"   {_resume_command(cli_path, video_path, work_dir)}")
        log("=" * 50)
        (work_dir / ".step_script.paused").write_text("", encoding="utf-8")
        _set_step_state(work_dir, "script", "wait", params=script_params, err_msg=next_step)
        return {
            "status": "paused",
            "work_dir": str(work_dir),
            "brief": str(brief_path),
            "next_step": next_step,
            "edit_mode": CONFIG.get("edit_mode", "full"),
            "substrate": substrate["level"],
            "resume_command": _resume_command(cli_path, video_path, work_dir),
        }

    if cut_mode:
        if source_narration is not None:
            source_narration = _annotate_cut_narration_overlap(source_narration, silence_periods)
        if _is_step_done(work_dir, "edit") and _cut_artifacts_current(work_dir):
            assembly_video_path = work_dir / "edited_source.mp4"
            narration = _load_json_file(work_dir / "narration_mapped.json", "narration_mapped.json")
            if (work_dir / "clip_plan_validated.json").exists():
                validated_plan = _load_json_file(work_dir / "clip_plan_validated.json", "clip_plan_validated.json")
            log(f"跳过剪辑映射（已存在 {assembly_video_path}）")
        else:
            assembly_video_path, narration, validated_plan = _prepare_cut_mode_artifacts(video_path, work_dir, source_narration)
    else:
        narration = source_narration

    # Step 6: TTS
    tts_meta = work_dir / "tts_meta.json"
    narration_artifact_path = work_dir / "narration_mapped.json" if cut_mode else narration_path
    if _is_step_done(work_dir, "tts") and _tts_meta_current(tts_meta, narration_artifact_path):
        tts_info = json.loads(tts_meta.read_text())
        tts_segments = tts_info["segments"]
        engine_used = tts_info["engine"]
        log(f"跳过 TTS（已存在 {len(tts_segments)} 段, 引擎: {engine_used}）")
    else:
        t0 = time.time()
        _clear_tts_cache(work_dir)
        tts_segments, engine_used = _synthesize_tts_stateful(work_dir, narration, narration_artifact_path)
        log(f"[{time.time()-t0:.1f}s] TTS 完成 (引擎: {engine_used})")

    # Step 7: 组装
    output_path = work_dir / "output.mp4"
    if _is_step_done(work_dir, "assemble") and _assemble_artifact_current(
        work_dir, output_path, [tts_meta, assembly_video_path], assembly_video_path
    ):
        log("跳过视频组装（已存在）")
    else:
        t0 = time.time()
        _assemble_video_stateful(work_dir, assembly_video_path, tts_segments, output_path, [tts_meta, assembly_video_path])
        log(f"[{time.time()-t0:.1f}s] 视频组装完成")

    # 复制到输出目录
    final_output = _final_output_path(video_path, work_dir, output_dir)
    if final_output != output_path:
        shutil.copy2(str(output_path), str(final_output))

    log("=" * 50)
    log(f"完成! 输出: {final_output}")
    log(f"工作目录: {work_dir}")
    if cut_mode and validated_plan:
        log(f"剪辑片段: {len(validated_plan['clips'])} | 剪辑时长: {validated_plan['total_duration']:.1f}s")
    log(f"场景: {len(scenes)} | 解说段: {len(narration)} | TTS: {engine_used}")

    # 质量指标（基于 vlm_analysis 场景，与解说生成一致）
    covered = set()
    for n in narration:
        n_mid = (n.get("source_start", n.get("start", 0)) + n.get("source_end", n.get("end", 0))) / 2
        for s in vlm_analysis:
            if s["start"] <= n_mid <= s["end"]:
                covered.add(s["scene_id"])
                break
    coverage_pct = len(covered) / len(vlm_analysis) * 100 if vlm_analysis else 100
    # 过滤空解说段（重写链路可能产生空文本）
    before = len(narration)
    narration = [n for n in narration if n.get("narration", "").strip()]
    removed = before - len(narration)
    if removed:
        log(f"  过滤 {removed} 个空解说段")
    narration.sort(key=lambda x: x["start"])
    overlaps = sum(1 for i in range(1, len(narration)) if narration[i]["start"] < narration[i-1]["end"])
    total_time = time.time() - pipeline_start
    log(f"覆盖率: {coverage_pct:.0f}% | 重叠: {overlaps} | 总耗时: {total_time:.0f}s")
    log("=" * 50)

    return {
        "output": str(final_output),
        "work_dir": str(work_dir),
        "scenes": len(scenes),
        "narration_segments": len(narration),
        "tts_engine": engine_used,
        "edit_mode": CONFIG.get("edit_mode", "full"),
        "edited_duration": validated_plan.get("total_duration") if validated_plan else None,
        "coverage": f"{coverage_pct:.0f}%",
        "overlaps": overlaps,
        "total_seconds": round(total_time),
    }
