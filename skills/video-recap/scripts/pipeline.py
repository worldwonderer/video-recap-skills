import json
import os
import shutil
import sys
import time
from pathlib import Path

from config import CONFIG
from common import log, api_call, get_video_duration
from extract import extract_frames
from detect import detect_scenes, detect_silence_periods, identify_narration_zones
from asr import transcribe_audio
from vlm import analyze_scenes, analyze_narrative_structure
from narration import (
    generate_narration_zones, generate_narration,
    _validate_narration_budget, _validate_and_rewrite_narration,
    _post_dedup_narration, _align_narration_to_quiet, _zone_coverage_fill
)
from tts import synthesize_tts
from assemble import assemble_video

# ── Prerequisites ─────────────────────────────────────────────────────

def _step_done(work_dir, step_name):
    """标记步骤完成"""
    (work_dir / f".step_{step_name}.done").write_text("ok")


def _is_step_done(work_dir, step_name):
    """检查步骤是否已完成"""
    return (work_dir / f".step_{step_name}.done").exists()

def check_prerequisites(skip_asr=False):
    """检查依赖"""
    checks = {
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "ffprobe": shutil.which("ffprobe") is not None,
    }
    if not skip_asr:
        checks["asr_binary"] = os.path.exists(CONFIG["asr_bin"])
        checks["asr_model"] = os.path.exists(CONFIG["asr_model_dir"])

    missing = [k for k, v in checks.items() if not v]
    if missing:
        log(f"缺少依赖: {', '.join(missing)}")
        return False

    log("依赖检查通过")
    return True


# ── Main Pipeline ─────────────────────────────────────────────────────

def run_pipeline(video_path, output_dir=None, step=None, style="纪录片",
                 scene_threshold=None, skip_asr=False, resume_dir=None,
                 agent_mode=False):
    """执行完整的视频解说 pipeline"""
    pipeline_start = time.time()
    if not CONFIG.get("api_key"):
        raise RuntimeError("请设置 OPENAI_API_KEY 环境变量")

    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(f"视频不存在: {video_path}")

    if not check_prerequisites(skip_asr=skip_asr):
        sys.exit(1)

    # 工作目录
    if resume_dir:
        work_dir = Path(resume_dir)
    else:
        output_dir = Path(output_dir or video_path.parent / "output")
        output_dir.mkdir(exist_ok=True)
        work_dir = output_dir / f"work_{int(time.time())}"

    work_dir.mkdir(exist_ok=True)
    log(f"工作目录: {work_dir}")
    log(f"输入视频: {video_path}")

    # 如果指定了 step，只执行那一步
    steps = {
        "extract": lambda: extract_frames(video_path, work_dir),
        "detect": lambda: detect_scenes(video_path, work_dir, scene_threshold),
        "asr": lambda: transcribe_audio(video_path, work_dir) if not skip_asr else [],
        "analyze": None,  # 需要前置数据
        "script": None,   # 需要前置数据
        "tts": None,      # 需要前置数据
        "assemble": None, # 需要前置数据
    }

    # 动态 FPS（需要在 step dispatch 之前，--step extract 需要）
    video_duration = get_video_duration(video_path)
    if CONFIG["fps"] <= 0:
        CONFIG["fps"] = 2 if video_duration <= 60 else (1.5 if video_duration <= 300 else 1)

    if step:
        if step in ("extract", "detect", "asr"):
            result = steps[step]()
            log(f"步骤 {step} 完成")
            return result
        else:
            log(f"步骤 {step} 需要完整 pipeline，自动运行全部步骤")

    # 完整 pipeline
    log("=" * 50)
    log("开始完整视频解说 pipeline")
    log("=" * 50)

    # API 连通性预检（避免跑完帧提取+ASR 才发现 API 不可用）
    if not _is_step_done(work_dir, "vlm"):
        log("API 连通性预检...")
        try:
            api_call({
                "model": CONFIG.get("vlm_model", CONFIG.get("llm_model", "")),
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 5,
            })
            log("API 连通性预检通过")
        except RuntimeError as e:
            log(f"API 预检失败: {e}")
            raise

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

    # Step 4.5: 叙事结构分析
    if CONFIG.get("skip_narrative_analysis", False):
        log("跳过叙事结构分析（skip_narrative_analysis=True）")
    elif _is_step_done(work_dir, "narrative"):
        vlm_analysis = json.loads((work_dir / "narrative_structure.json").read_text())
        log(f"跳过叙事结构分析（已存在）")
    else:
        t0 = time.time()
        vlm_analysis = analyze_narrative_structure(vlm_analysis, work_dir)
        _step_done(work_dir, "narrative")
        log(f"[{time.time()-t0:.1f}s] 叙事结构分析完成")

    # Step 5: 解说脚本
    if _is_step_done(work_dir, "script"):
        narration = json.loads((work_dir / "narration.json").read_text())
        log(f"跳过解说脚本（已存在 {len(narration)} 段）")
    elif agent_mode:
        # Agent 模式：在 Step 5 前暂停，等待 Agent 手动写解说词
        log("=" * 50)
        log("⏸  Agent 模式：Pipeline 在此暂停")
        log("   请 Agent 基于 vlm_analysis.json / asr_result.json / silence_periods.json 亲自撰写解说词")
        log(f"   写入 {work_dir}/narration.json 后执行:")
        log(f"   touch {work_dir}/.step_script.done")
        log(f"   python3 {__file__} {video_path} --resume {work_dir}")
        log("=" * 50)
        (Path(work_dir) / ".step_script.paused").write_text("")
        # 创建空 narration.json 占位，防止 resume 时 FileNotFoundError
        (Path(work_dir) / "narration.json").write_text("[]")
        return {"status": "paused", "work_dir": str(work_dir), "next_step": "write narration"}
    else:
        t0 = time.time()
        if CONFIG.get("narration_mode") == "zone":
            # 解说区模式：大段解说 + 原声交替
            zones = identify_narration_zones(silence_periods, vlm_analysis, video_duration)
            if zones:
                narration = generate_narration_zones(zones, asr_result, work_dir, style)
                narration = _validate_narration_budget(narration, vlm_analysis)
                narration, _ = _validate_and_rewrite_narration(narration, vlm_analysis, work_dir)
                narration = _post_dedup_narration(narration)
                narration = _align_narration_to_quiet(narration, vlm_analysis, silence_periods)
                # 混合补充：zone 模式覆盖率不足时，为重要未覆盖场景补充 fill 解说
                narration = _zone_coverage_fill(narration, vlm_analysis, asr_result,
                                                silence_periods, work_dir)
            else:
                log("解说区为空，fallback 到逐场景模式")
                narration = generate_narration(vlm_analysis, asr_result, work_dir, style,
                                               silence_periods=silence_periods)
                narration, _ = _validate_and_rewrite_narration(narration, vlm_analysis, work_dir)
                narration = _post_dedup_narration(narration)
                narration = _align_narration_to_quiet(narration, vlm_analysis, silence_periods)
        else:
            # 逐场景模式（原始）
            narration = generate_narration(vlm_analysis, asr_result, work_dir, style,
                                           silence_periods=silence_periods)
            narration, _ = _validate_and_rewrite_narration(narration, vlm_analysis, work_dir)
            narration = _post_dedup_narration(narration)
            narration = _align_narration_to_quiet(narration, vlm_analysis, silence_periods)
        # 保存
        (work_dir / "narration.json").write_text(
            json.dumps(narration, ensure_ascii=False, indent=2))
        _step_done(work_dir, "script")
        log(f"[{time.time()-t0:.1f}s] 解说脚本完成")

    # Step 6: TTS
    style_voice = CONFIG.get("style_voices", {}).get(style)
    if style_voice and CONFIG["tts_engine"] in ("auto", "edge-tts"):
        CONFIG["edge_tts_voice"] = style_voice
    tts_meta = work_dir / "tts_meta.json"
    if _is_step_done(work_dir, "tts") and tts_meta.exists():
        tts_info = json.loads(tts_meta.read_text())
        tts_segments = tts_info["segments"]
        engine_used = tts_info["engine"]
        log(f"跳过 TTS（已存在 {len(tts_segments)} 段, 引擎: {engine_used}）")
    else:
        t0 = time.time()
        tts_segments, engine_used = synthesize_tts(narration, work_dir)
        tts_meta.write_text(json.dumps({
            "segments": tts_segments, "engine": engine_used
        }, ensure_ascii=False, indent=2))
        _step_done(work_dir, "tts")
        log(f"[{time.time()-t0:.1f}s] TTS 完成 (引擎: {engine_used})")

    # Step 7: 组装
    output_path = work_dir / "output.mp4"
    if _is_step_done(work_dir, "assemble") and output_path.exists():
        log(f"跳过视频组装（已存在）")
    else:
        t0 = time.time()
        assemble_video(video_path, tts_segments, work_dir, output_path)
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
    log(f"场景: {len(scenes)} | 解说段: {len(narration)} | TTS: {engine_used}")

    # 质量指标（基于 vlm_analysis 场景，与解说生成一致）
    covered = set()
    for n in narration:
        n_mid = (n.get("start", 0) + n.get("end", 0)) / 2
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
        "coverage": f"{coverage_pct:.0f}%",
        "overlaps": overlaps,
        "total_seconds": round(total_time),
    }


