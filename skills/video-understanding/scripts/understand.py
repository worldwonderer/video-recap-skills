#!/usr/bin/env python3
"""video-understanding entrypoint.

Analyze a source video into a structured understanding index (scenes, ASR transcript,
per-scene VLM analysis, silence windows, fused timeline) plus a narration-writing brief.
Stateless: a stage is skipped only when its output artifact already exists and is newer
than its input (use --force to recompute everything).
"""
import argparse
import json
from pathlib import Path

from lib import CONFIG, log, get_video_duration, api_call
from extract import extract_frames
from detect import detect_scenes, detect_silence_periods
from asr import transcribe_audio
from vlm import analyze_scenes, analyze_video_overview
from brief import build_agent_brief, assess_understanding_substrate


def _fresh(out, *inputs):
    out = Path(out)
    if not out.exists():
        return False
    ins = [Path(p) for p in inputs if p and Path(p).exists()]
    if not ins:
        return out.stat().st_size > 0
    return out.stat().st_mtime >= max(p.stat().st_mtime for p in ins)


def main():
    ap = argparse.ArgumentParser(description="Analyze a video into an understanding index + narration brief.")
    ap.add_argument("video")
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--context", default="", help="extra context (show name, character names, ...)")
    ap.add_argument("--scene-threshold", type=float, default=None)
    ap.add_argument("--style", default="纪录片")
    ap.add_argument("--skip-asr", action="store_true")
    ap.add_argument("--mimo-video-overview", action="store_true")
    ap.add_argument("--force", action="store_true", help="ignore cached artifacts and recompute")
    ap.add_argument("--consolidate", action="store_true", help="build the global understanding index (Pass B)")
    ap.add_argument("--consolidate-asr", action="store_true", help="also clean the ASR transcript (Pass A)")
    args = ap.parse_args()

    video = args.video
    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    if args.context:
        CONFIG["context_info"] = args.context
    if args.scene_threshold is not None:
        CONFIG["scene_threshold"] = args.scene_threshold
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
    if not args.force and frames_dir.exists() and any(frames_dir.glob("frame_*.jpg")):
        frames = sorted(frames_dir.glob("frame_*.jpg"))
        log(f"跳过帧提取（已存在 {len(frames)} 帧）")
    else:
        frames = extract_frames(video, work_dir)

    # Step 2: scene detection
    if not args.force and _fresh(scenes_json, video):
        scenes = json.loads(scenes_json.read_text())
        log(f"跳过场景检测（已存在 {len(scenes)} 个场景）")
    else:
        scenes = detect_scenes(video, work_dir, scene_threshold)

    # Step 3: ASR
    if args.skip_asr:
        asr_result = []
        asr_json.write_text(json.dumps(asr_result, ensure_ascii=False, indent=2))
        log("跳过 ASR（--skip-asr）")
    elif not args.force and _fresh(asr_json, video):
        asr_result = json.loads(asr_json.read_text())
        log(f"跳过 ASR（已存在 {len(asr_result)} 段）")
    else:
        try:
            asr_result = transcribe_audio(video, work_dir)
        except Exception as e:
            log(f"ASR 失败（继续无 ASR）: {e}")
            asr_result = []
            asr_json.write_text(json.dumps(asr_result, ensure_ascii=False, indent=2))

    # Step 3.5: silence detection
    if not args.force and _fresh(silence_json, asr_json):
        silence_periods = json.loads(silence_json.read_text())
        log(f"跳过静音检测（已存在 {len(silence_periods)} 个窗口）")
    else:
        silence_periods = detect_silence_periods(video, work_dir, asr_result)

    # Step 4: VLM analysis (the only stage that requires the chat API key)
    if not args.force and _fresh(vlm_json, scenes_json):
        vlm_analysis = json.loads(vlm_json.read_text())
        log(f"跳过 VLM 分析（已存在 {len(vlm_analysis)} 个场景）")
    else:
        if not CONFIG.get("api_key"):
            key_name = CONFIG.get("api_key_source", "OPENAI_API_KEY")
            raise SystemExit(f"请设置 {key_name} 环境变量（VLM 画面分析需要）")
        log("VLM API 连通性预检...")
        api_call({"model": CONFIG.get("vlm_model", ""),
                  "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5})
        vlm_analysis = analyze_scenes(scenes, frames, work_dir)

    # Step 4.1: optional MiMo scene-chunk video understanding
    if CONFIG.get("mimo_video_overview", False):
        if not CONFIG.get("mimo_video_api_key"):
            log("跳过 MiMo 分片视频概览：未设置 MIMO_API_KEY")
        elif _fresh(work_dir / "mimo_video_overview.json", scenes_json):
            log("跳过 MiMo 分片视频概览（已存在）")
        else:
            try:
                analyze_video_overview(video, work_dir, scenes)
            except Exception as e:
                log(f"MiMo 分片视频概览失败（忽略）: {e}")

    # optional consolidation (整理): build the understanding index before the brief folds it in
    if args.consolidate or args.consolidate_asr:
        from consolidate import consolidate
        try:
            consolidate(work_dir, do_asr=args.consolidate_asr, do_index=True)
        except Exception as e:
            log(f"consolidate 跳过（忽略）: {e}")

    # understanding substrate warning + writing brief
    substrate = assess_understanding_substrate(vlm_analysis, asr_result)
    if substrate["level"] != "rich":
        banner = "理解素材为空" if substrate["level"] == "empty" else "理解素材偏薄"
        log(f"⚠️  {banner}：ASR {substrate['asr_chars']} 字 | 场景 {substrate['scene_count']} | "
            f"带 frame_facts 的场景 {substrate['scenes_with_frame_facts']} | 平均画面描述 {substrate['avg_description_len']} 字")
    brief_path = build_agent_brief(vlm_analysis, asr_result, silence_periods, video_duration, work_dir, args.style)

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
