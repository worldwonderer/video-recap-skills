"""Orchestrate the video-understanding stages."""

import argparse


import json

from pathlib import Path

from lib import CONFIG, log, get_video_duration, api_call

from extract import extract_frames

from detect import detect_scenes, detect_silence_periods, detect_speech_boundary_anchors

from asr import transcribe_audio

from vlm import (
    analyze_scenes,
    analyze_video_overview,
    mimo_video_overview_cache_fresh,
)

from brief import build_agent_brief, assess_understanding_substrate


from understanding_brief import _research_context, _write_brief_from_existing_artifacts
from understanding_cache import (
    _asr_cache_payload,
    _frames_cache_valid,
    _load_json,
    _merge_overview_into_scenes,
    _present_consolidation_artifacts,
    _remove_stage_meta,
    _scene_cache_payload,
    _silence_cache_payload,
    _stage_cache_valid,
    _vlm_cache_payload,
    _write_consolidation_status,
    _write_frames_manifest,
    _write_mimo_overview_status,
    _write_stage_meta,
)
from understanding_storyboard import (
    _generate_edited_storyboard,
    _generate_source_storyboard,
    _prepend_storyboard_brief_header,
)


def main():
    ap = argparse.ArgumentParser(
        description="Analyze a video into an understanding index + narration brief."
    )
    ap.add_argument("video")
    ap.add_argument("--work-dir", required=True)
    ap.add_argument(
        "--context", default="", help="extra context (show name, character names, ...)"
    )
    ap.add_argument("--scene-threshold", type=float, default=None)
    ap.add_argument("--style", default="纪录片")
    ap.add_argument(
        "--edit-mode",
        default=None,
        choices=["full", "cut"],
        help="recap mode to document in the writing brief",
    )
    ap.add_argument(
        "--target-duration",
        default=None,
        help="cut-mode target duration to document in the writing brief",
    )
    ap.add_argument("--skip-asr", action="store_true")
    ap.add_argument("--mimo-video-overview", action="store_true")
    ap.add_argument(
        "--force", action="store_true", help="ignore cached artifacts and recompute"
    )
    ap.add_argument(
        "--brief-only",
        action="store_true",
        help="rebuild agent_narration_brief.md from existing artifacts only; no extraction/API",
    )
    ap.add_argument(
        "--consolidate",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="build the global understanding story index (Pass B); default ON, --no-consolidate to skip",
    )
    ap.add_argument(
        "--consolidate-asr",
        action="store_true",
        help="also clean the ASR transcript (Pass A)",
    )
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
        CONFIG["fps"] = (
            2 if video_duration <= 60 else (1.5 if video_duration <= 300 else 1)
        )
    log(f"FPS: {CONFIG['fps']} (视频时长: {video_duration:.1f}s)")

    if args.brief_only:
        _write_brief_from_existing_artifacts(video, work_dir, args, video_duration)
        return

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
        asr_json.write_text(
            json.dumps(asr_result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
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
            raise RuntimeError(
                f"ASR 失败；未写入可复用缓存，请修复后重试或显式使用 --skip-asr: {e}"
            ) from e
        _write_stage_meta(asr_json, asr_meta)

    # Step 3.5: silence detection
    silence_meta = _silence_cache_payload(video, asr_json)
    if not args.force and _stage_cache_valid(silence_json, silence_meta):
        silence_periods = _load_json(silence_json)
        log(f"跳过静音检测（已存在 {len(silence_periods)} 个窗口）")
        if not (work_dir / "speech_boundary_anchors.json").exists():
            detect_speech_boundary_anchors(work_dir, asr_result)
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
        api_call(
            {
                "model": CONFIG.get("vlm_model", ""),
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 5,
            }
        )
        vlm_analysis = analyze_scenes(scenes, frames, work_dir, resume=not args.force)
        _write_stage_meta(vlm_json, vlm_meta)

    # Step 4.1: optional MiMo scene-chunk video understanding
    overview_path = work_dir / "mimo_video_overview.json"
    if CONFIG.get("mimo_video_overview", False):
        if not CONFIG.get("mimo_video_api_key"):
            log("跳过 MiMo 分片视频概览：未设置 MIMO_API_KEY")
            overview_path.unlink(missing_ok=True)
            _write_mimo_overview_status(
                work_dir,
                "skipped_no_key",
                "未设置 MIMO_API_KEY，MiMo 分片视频概览未运行",
                None,
            )
        elif mimo_video_overview_cache_fresh(overview_path, video, scenes):
            log("跳过 MiMo 分片视频概览（缓存匹配）")
            _write_mimo_overview_status(
                work_dir, "cached", "缓存匹配", overview_path.name
            )
        else:
            overview_path.unlink(missing_ok=True)
            try:
                overview = analyze_video_overview(video, work_dir, scenes)
            except Exception as e:
                log(f"MiMo 分片视频概览失败（忽略）: {e}")
                _write_mimo_overview_status(work_dir, "failed", e, None)
            else:
                if overview_path.exists() and overview:
                    _write_mimo_overview_status(
                        work_dir, "ok", "MiMo 分片视频概览完成", overview_path.name
                    )
                else:
                    _write_mimo_overview_status(
                        work_dir, "failed", "MiMo 分片视频概览未产出有效 artifact", None
                    )
    else:
        overview_path.unlink(missing_ok=True)
        _write_mimo_overview_status(
            work_dir, "disabled", "MiMo 分片视频概览未启用", None, enabled=False
        )

    # Make the video-overview the primary per-scene description (frame_facts stay the anchor).
    # No-op/revert when overview is absent, so disabling it cleanly returns to frame descriptions.
    vlm_analysis = _merge_overview_into_scenes(vlm_analysis, overview_path)

    # optional consolidation (整理): build the understanding index before the brief folds it in
    if args.consolidate or args.consolidate_asr:
        from consolidate import consolidate

        try:
            consolidate(
                work_dir, do_asr=args.consolidate_asr, do_index=args.consolidate
            )
        except Exception as e:
            log(f"consolidate 跳过（忽略）: {e}")
            _write_consolidation_status(
                work_dir,
                "failed",
                e,
                _present_consolidation_artifacts(work_dir),
                do_asr=args.consolidate_asr,
                do_index=args.consolidate,
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
                    work_dir,
                    "failed",
                    f"未产出预期 artifact: {', '.join(missing)}",
                    artifacts,
                    do_asr=args.consolidate_asr,
                    do_index=args.consolidate,
                )
            elif expected:
                _write_consolidation_status(
                    work_dir,
                    "ok",
                    "consolidation 完成",
                    artifacts,
                    do_asr=args.consolidate_asr,
                    do_index=args.consolidate,
                )
            else:
                _write_consolidation_status(
                    work_dir,
                    "skipped",
                    "；".join(skipped) or "无可整理输入",
                    artifacts,
                    do_asr=args.consolidate_asr,
                    do_index=args.consolidate,
                )
    else:
        _write_consolidation_status(
            work_dir,
            "disabled",
            "consolidation 未启用",
            [],
            enabled=False,
            do_asr=False,
            do_index=False,
        )

    # Storyboard contact sheets (advisory, never blocking). Source uses scene anchors over the
    # source timeline; edited is gated on clip_plan_validated.json file-presence (NOT edit_mode —
    # recap.py forwards --edit-mode cut in BOTH passes, so the validated plan is the only reliable
    # pass2 signal). Both cache via _write_stage_meta/_stage_cache_valid with fps + frame-set in the key.
    source_storyboard = _generate_source_storyboard(
        work_dir, Path(video), scenes, scenes_json, force=args.force
    )
    edited_storyboard = _generate_edited_storyboard(work_dir, video, force=args.force)
    cut_mode = (work_dir / "clip_plan_validated.json").exists()

    # understanding substrate warning + writing brief
    substrate = assess_understanding_substrate(vlm_analysis, asr_result)
    if substrate["level"] != "rich":
        banner = "理解素材为空" if substrate["level"] == "empty" else "理解素材偏薄"
        log(
            f"⚠️  {banner}：ASR {substrate['asr_chars']} 字 | 场景 {substrate['scene_count']} | "
            f"带 frame_facts 的场景 {substrate['scenes_with_frame_facts']} | 平均画面描述 {substrate['avg_description_len']} 字"
        )
    brief_path = build_agent_brief(
        vlm_analysis,
        asr_result,
        silence_periods,
        video_duration,
        work_dir,
        args.style,
        mimo_overview_enabled=CONFIG.get("mimo_video_overview", False),
        mimo_overview_video_path=video,
    )
    # C1: post-process the RETURNED brief FILE (not brief.py) so the brief⇄narration twin stays
    # byte-identical. Prepends a storyboard header pointing the agent at the sheet(s).
    _prepend_storyboard_brief_header(
        brief_path, source_storyboard, edited_storyboard, cut_mode=cut_mode
    )

    log("=" * 50)
    log(f"理解完成。写作 brief: {brief_path}")
    print(
        json.dumps(
            {
                "status": "analyzed",
                "work_dir": str(work_dir),
                "brief": str(brief_path),
                "substrate": substrate["level"],
                "scenes": len(scenes),
                "asr_segments": len(asr_result),
            },
            ensure_ascii=False,
        )
    )
