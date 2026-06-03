import json
import os
import shutil
import shlex
import sys
import time
from pathlib import Path

from config import CONFIG
from common import log, api_call, get_video_duration, run_cmd
from extract import extract_frames
from detect import detect_scenes, detect_silence_periods
from asr import transcribe_audio
from vlm import analyze_scenes, analyze_narrative_structure, analyze_video_overview, mimo_video_settings_fingerprint
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

# ── Prerequisites ─────────────────────────────────────────────────────

def _step_done(work_dir, step_name):
    """标记步骤完成"""
    (work_dir / f".step_{step_name}.done").write_text("ok")


def _is_step_done(work_dir, step_name):
    """检查步骤是否已完成"""
    return (work_dir / f".step_{step_name}.done").exists()


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
    _step_done(work_dir, "edit")
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


def _assemble_meta_path(work_dir):
    return work_dir / "assemble_meta.json"


def _write_assemble_meta(work_dir, input_video):
    meta = {
        "input_video": str(input_video),
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
        meta.get("input_video") == str(input_video)
        and meta.get("settings") == assembly_settings_fingerprint()
    )


def _assemble_artifact_current(work_dir, output_path, source_paths, input_video):
    return _artifact_current(output_path, source_paths) and _assemble_settings_current(work_dir, input_video)


def _clear_tts_cache(work_dir):
    shutil.rmtree(work_dir / "tts_segments", ignore_errors=True)
    for path in (work_dir / "tts_meta.json", work_dir / ".step_tts.done"):
        path.unlink(missing_ok=True)


def _tts_meta_payload(tts_segments, engine_used):
    return {
        "segments": tts_segments,
        "engine": engine_used,
        "settings": tts_settings_fingerprint(engine_used),
    }


def _tts_meta_current(tts_meta, narration_artifact_path):
    if not _artifact_current(tts_meta, [narration_artifact_path]):
        return False
    try:
        tts_info = json.loads(tts_meta.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
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


def _run_cached_tail_step(video_path, work_dir, step, style, output_dir):
    """Run tts/assemble from existing artifacts without VLM/API prerequisites."""
    if step not in ("tts", "assemble"):
        return None

    if _cut_mode_enabled():
        _ensure_cut_tail_artifacts(video_path, work_dir)

    narration_artifact_path = work_dir / "narration_mapped.json" if _cut_mode_enabled() else work_dir / "narration.json"

    if step == "tts":
        _clear_tts_cache(work_dir)
        narration = _load_tail_narration(work_dir)
        tts_segments, engine_used = synthesize_tts(narration, work_dir)
        tts_meta = work_dir / "tts_meta.json"
        tts_meta.write_text(json.dumps(_tts_meta_payload(tts_segments, engine_used), ensure_ascii=False, indent=2))
        _step_done(work_dir, "tts")
        log(f"步骤 tts 完成 ({len(tts_segments)} 段, 引擎: {engine_used})")
        return {"segments": tts_segments, "engine": engine_used}

    tts_meta = work_dir / "tts_meta.json"
    if _tts_meta_current(tts_meta, narration_artifact_path):
        tts_info = _load_json_file(tts_meta, "tts_meta.json")
        tts_segments = tts_info["segments"]
    else:
        _clear_tts_cache(work_dir)
        narration = _load_tail_narration(work_dir)
        tts_segments, engine_used = synthesize_tts(narration, work_dir)
        tts_meta.write_text(json.dumps(_tts_meta_payload(tts_segments, engine_used), ensure_ascii=False, indent=2))
        _step_done(work_dir, "tts")

    output_path = work_dir / "output.mp4"
    assembly_input = _tail_video_path(video_path, work_dir)
    if _is_step_done(work_dir, "assemble") and _assemble_artifact_current(
        work_dir, output_path, [tts_meta, assembly_input], assembly_input
    ):
        log("跳过视频组装（已存在）")
    else:
        assemble_video(assembly_input, tts_segments, work_dir, output_path)
        _write_assemble_meta(work_dir, assembly_input)
        _step_done(work_dir, "assemble")

    final_output = Path(output_dir) / f"recap_{video_path.stem}.mp4" if output_dir else work_dir.parent / f"recap_{video_path.stem}.mp4"
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

    work_dir.mkdir(exist_ok=True)
    if resume_dir:
        _merge_run_settings(work_dir)
    else:
        _persist_run_settings(work_dir)
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
    steps = {
        "extract": lambda: extract_frames(video_path, work_dir),
        "detect": lambda: detect_scenes(video_path, work_dir, scene_threshold),
        "asr": lambda: transcribe_audio(video_path, work_dir) if not skip_asr else [],
        "analyze": None,  # 需要前置数据
        "script": None,   # Agent-authored narration.json / clip_plan.json validation
        "tts": None,      # 需要前置数据
        "assemble": None, # 需要前置数据
    }

    # 动态 FPS（需要在 step dispatch 之前，--step extract 需要）
    video_duration = get_video_duration(video_path)
    if CONFIG["fps"] <= 0:
        CONFIG["fps"] = 2 if video_duration <= 60 else (1.5 if video_duration <= 300 else 1)

    stop_after_script = False
    if step:
        if step in ("extract", "detect", "asr"):
            result = steps[step]()
            log(f"步骤 {step} 完成")
            return result
        if step in ("tts", "assemble"):
            cached_result = _run_cached_tail_step(video_path, work_dir, step, style, output_dir)
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

    needs_frame_vlm_api = not _is_step_done(work_dir, "vlm")
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
    if _is_step_done(work_dir, "extract"):
        frames = sorted((work_dir / "frames").glob("frame_*.jpg"))
        log(f"跳过帧提取（已存在 {len(frames)} 帧）")
    else:
        t0 = time.time()
        frames = extract_frames(video_path, work_dir)
        _step_done(work_dir, "extract")
        log(f"[{time.time()-t0:.1f}s] 帧提取完成")

    # Step 2: 场景检测
    if _is_step_done(work_dir, "detect"):
        scenes = json.loads((work_dir / "scenes.json").read_text())
        log(f"跳过场景检测（已存在 {len(scenes)} 个场景）")
    else:
        t0 = time.time()
        scenes = detect_scenes(video_path, work_dir, scene_threshold)
        _step_done(work_dir, "detect")
        log(f"[{time.time()-t0:.1f}s] 场景检测完成")

    # Step 3: ASR
    if _is_step_done(work_dir, "asr"):
        asr_result = json.loads((work_dir / "asr_result.json").read_text())
        log(f"跳过 ASR（已存在 {len(asr_result)} 段）")
    elif skip_asr:
        asr_result = []
        (work_dir / "asr_result.json").write_text(
            json.dumps(asr_result, ensure_ascii=False, indent=2))
        _step_done(work_dir, "asr")
        log("跳过 ASR")
    else:
        t0 = time.time()
        try:
            asr_result = transcribe_audio(video_path, work_dir)
        except Exception as e:
            log(f"ASR 失败（继续无 ASR）: {e}")
            asr_result = []
        _step_done(work_dir, "asr")
        log(f"[{time.time()-t0:.1f}s] ASR 完成")

    # Step 3.5: 静音检测
    if _is_step_done(work_dir, "silence"):
        silence_periods = json.loads((work_dir / "silence_periods.json").read_text())
        log(f"跳过静音检测（已存在 {len(silence_periods)} 个窗口）")
    else:
        t0 = time.time()
        silence_periods = detect_silence_periods(video_path, work_dir, asr_result)
        _step_done(work_dir, "silence")
        log(f"[{time.time()-t0:.1f}s] 静音检测完成")

    # Step 4: VLM 分析
    if _is_step_done(work_dir, "vlm"):
        vlm_analysis = json.loads((work_dir / "vlm_analysis.json").read_text())
        log(f"跳过 VLM 分析（已存在 {len(vlm_analysis)} 个场景）")
    else:
        t0 = time.time()
        vlm_analysis = analyze_scenes(scenes, frames, work_dir)
        _step_done(work_dir, "vlm")
        log(f"[{time.time()-t0:.1f}s] VLM 分析完成")

    # Step 4.1: Optional MiMo scene-chunk video understanding
    if CONFIG.get("mimo_video_overview", False):
        if _mimo_video_overview_current(work_dir):
            log("跳过 MiMo 分片视频概览（已存在）")
        else:
            t0 = time.time()
            overview = analyze_video_overview(video_path, work_dir, scenes)
            if overview is not None:
                _step_done(work_dir, "mimo_video_overview")
                log(f"[{time.time()-t0:.1f}s] MiMo 分片视频概览完成")

    # Step 4.5: 叙事结构分析
    if CONFIG.get("skip_narrative_analysis", False):
        log("跳过叙事结构分析（skip_narrative_analysis=True）")
    elif _is_step_done(work_dir, "narrative"):
        vlm_analysis = json.loads((work_dir / "narrative_structure.json").read_text())
        log("跳过叙事结构分析（已存在）")
    else:
        t0 = time.time()
        vlm_analysis = analyze_narrative_structure(vlm_analysis, work_dir)
        _step_done(work_dir, "narrative")
        log(f"[{time.time()-t0:.1f}s] 叙事结构分析完成")

    # Step 5: Agent-authored narration script and optional clip plan
    narration_path = work_dir / "narration.json"
    clip_plan_path = work_dir / "clip_plan.json"
    cut_mode = _cut_mode_enabled()
    required_ready = narration_path.exists() and (not cut_mode or clip_plan_path.exists())
    source_narration = None
    assembly_video_path = video_path
    validated_plan = None

    if _is_step_done(work_dir, "script"):
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
        log(f"跳过解说词写作（已存在 {len(source_narration)} 段）")
    elif required_ready:
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
        _step_done(work_dir, "script")
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
        tts_segments, engine_used = synthesize_tts(narration, work_dir)
        tts_meta.write_text(json.dumps(_tts_meta_payload(tts_segments, engine_used), ensure_ascii=False, indent=2))
        _step_done(work_dir, "tts")
        log(f"[{time.time()-t0:.1f}s] TTS 完成 (引擎: {engine_used})")

    # Step 7: 组装
    output_path = work_dir / "output.mp4"
    if _is_step_done(work_dir, "assemble") and _assemble_artifact_current(
        work_dir, output_path, [tts_meta, assembly_video_path], assembly_video_path
    ):
        log("跳过视频组装（已存在）")
    else:
        t0 = time.time()
        assemble_video(assembly_video_path, tts_segments, work_dir, output_path)
        _write_assemble_meta(work_dir, assembly_video_path)
        _step_done(work_dir, "assemble")
        log(f"[{time.time()-t0:.1f}s] 视频组装完成")

    # 复制到输出目录
    if output_dir:
        final_output = Path(output_dir) / f"recap_{video_path.stem}.mp4"
    else:
        final_output = work_dir.parent / f"recap_{video_path.stem}.mp4"
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
