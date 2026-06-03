#!/usr/bin/env python3
"""video-recap: agent-authored Chinese recap video pipeline.
Input video → scene/frame analysis → ASR/VLM artifacts → agent writes narration.json → TTS → assembly.
"""

import argparse
import json
import os
import sys

from config import CONFIG, normalize_api_url
from common import log
from pipeline import run_pipeline


# ── CLI ───────────────────────────────────────────────────────────────

def _apply_api_provider(provider):
    """Switch the frame-level VLM provider without changing MiMo TTS defaults."""
    if not provider:
        return
    CONFIG["api_provider"] = provider
    CONFIG["api_provider_source"] = "cli"
    if provider == "mimo":
        CONFIG["api_key"] = CONFIG.get("mimo_api_key") or CONFIG.get("api_key", "")
        CONFIG["api_key_source"] = CONFIG.get("mimo_api_key_source", "MIMO_API_KEY")
        CONFIG["api_url"] = CONFIG.get("mimo_api_url") or CONFIG.get("api_url")
        CONFIG["api_url_source"] = "cli"
        CONFIG["vlm_model"] = CONFIG.get("mimo_model") or CONFIG.get("vlm_model")
        CONFIG["vlm_model_source"] = "cli"
    elif provider == "openai":
        CONFIG["api_key"] = os.environ.get("OPENAI_API_KEY", "")
        CONFIG["api_key_source"] = "OPENAI_API_KEY"
        CONFIG["api_url"] = normalize_api_url(os.environ.get("OPENAI_API_URL"))
        CONFIG["api_url_source"] = "cli"
        CONFIG["vlm_model"] = os.environ.get("OPENAI_MODEL") or CONFIG.get("vlm_model")
        CONFIG["vlm_model_source"] = "cli"


def main():
    parser = argparse.ArgumentParser(
        description="video-recap: Agent 写解说词的视频 recap pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("video", nargs="?", help="输入视频文件路径")
    parser.add_argument("--output", "-o", help="输出目录 (默认: 视频所在目录/output)")
    parser.add_argument("--step", choices=["extract", "detect", "asr", "analyze", "script", "tts", "assemble"],
                        help="仅执行某步骤；script 只验证 Agent 写好的 narration.json")
    parser.add_argument("--style", default="纪录片",
                        choices=["短剧", "电视剧", "电影", "纪录片", "科普视频"],
                        help="解说风格 (默认: 纪录片)")
    parser.add_argument("--scene-threshold", type=float, default=0.1,
                        help="场景检测阈值 0.0-1.0 (默认: 0.1, 对应 scdet=10)")
    parser.add_argument("--skip-asr", action="store_true",
                        help="跳过 ASR 转录")
    parser.add_argument("--resume", metavar="WORK_DIR",
                        help="从已有的工作目录继续")
    parser.add_argument("--tts", choices=["auto", "edge-tts", "mimo-tts"],
                        default=None, help="TTS 引擎 (默认: TTS_ENGINE 或 auto；auto 有 MiMo key 时优先 mimo-tts)")
    parser.add_argument("--fps", type=float, default=0,
                        help="帧提取 fps (默认: 自动，≤60s→2fps, ≤5min→1.5fps, >5min→1fps)")
    parser.add_argument("--burn-subtitles", action="store_true",
                        help="烧录字幕到视频（会增加处理时间）")
    parser.add_argument("--ducking", choices=["sidechaincompress", "fixed", "none"],
                        default=None,
                        help=f"音频 ducking 模式；默认使用配置值 {CONFIG['ducking_mode']}")
    parser.add_argument("--context", type=str, default="",
                        help="额外上下文（节目名、角色名等）")
    parser.add_argument("--model", type=str, default=None,
                        help="覆盖 VLM 模型名 (默认: OPENAI_MODEL 或 doubao-seed-2-0-lite-260428)")
    parser.add_argument("--vlm-model", type=str, default=None,
                        help="单独覆盖 VLM 模型名 (优先级高于 --model)")
    parser.add_argument("--api-provider", choices=["openai", "mimo"], default=None,
                        help="API 兼容提供方；mimo 会使用 api-key 头和 max_completion_tokens")
    parser.add_argument("--mimo-api-url", type=str, default=None,
                        help="MiMo shared base URL；默认同时用于 MiMo 视频理解和 TTS")
    parser.add_argument("--mimo-video-overview", action="store_true",
                        help="使用 MiMo 视频理解按 ffmpeg scene 分片生成概览并写入 brief")
    parser.add_argument("--mimo-tts-voice", type=str, default=None,
                        help="MiMo TTS 音色，如 冰糖/茉莉/苏打/白桦/mimo_default")
    parser.add_argument("--mimo-tts-style", type=str, default=None,
                        help="MiMo TTS 自然语言播报风格指令")
    parser.add_argument("--doctor", action="store_true",
                        help="检查 ffmpeg / edge-tts / API 配置后退出")
    parser.add_argument("--doctor-tts-smoke", action="store_true",
                        help="doctor 时额外运行一个 edge-tts 试合成")
    parser.add_argument("--voice", type=str, default=None,
                        help="覆盖 edge-tts 音色 (如 zh-CN-YunxiNeural)")
    parser.add_argument("--vlm-workers", type=int, default=None,
                        help="VLM 并行线程数；代理超时时建议设为 1 (默认: VLM_WORKERS 或配置值)")
    parser.add_argument("--tts-workers", type=int, default=None,
                        help="TTS 并行线程数 (默认: TTS_WORKERS 或配置值)")
    parser.add_argument("--tts-timeout", type=int, default=None,
                        help="单段 TTS 命令超时秒数 (默认: TTS_TIMEOUT 或配置值)")
    parser.add_argument("--allow-partial-tts", action="store_true",
                        help="允许部分 TTS 段失败后继续组装（默认失败即中止）")
    parser.add_argument("--edit-mode", choices=["full", "cut"], default=None,
                        help="成片模式：full 保留全片；cut 使用 Agent 写的 clip_plan.json 剪成短解说")
    parser.add_argument("--target-duration", type=str, default=None,
                        help="cut 模式目标成片时长，如 600、10m、00:10:00")
    parser.add_argument("--clip-padding", type=float, default=None,
                        help="cut 模式每个片段两端扩展秒数 (默认: 0)")
    parser.add_argument("--allow-clip-overlap", action="store_true",
                        help="cut 模式允许重复/重叠使用原片；重复片段的 narration 需写 source_clip_id")

    args = parser.parse_args()

    # 覆盖配置
    if args.tts is not None:
        CONFIG["tts_engine"] = args.tts
        CONFIG["tts_engine_source"] = "cli"
    CONFIG["fps"] = args.fps
    CONFIG["burn_subtitles"] = args.burn_subtitles
    CONFIG["context_info"] = args.context
    if args.api_provider:
        _apply_api_provider(args.api_provider)
    if args.mimo_api_url:
        mimo_url = normalize_api_url(args.mimo_api_url)
        CONFIG["mimo_api_url"] = mimo_url
        CONFIG["mimo_api_url_source"] = "cli"
        CONFIG["mimo_video_api_url"] = mimo_url
        CONFIG["mimo_video_api_url_source"] = "cli"
        CONFIG["mimo_tts_api_url"] = mimo_url
        CONFIG["mimo_tts_api_url_source"] = "cli"
        if CONFIG.get("api_provider") == "mimo":
            CONFIG["api_url"] = mimo_url
            CONFIG["api_url_source"] = "cli"
    if args.model:
        CONFIG["vlm_model"] = args.model
        CONFIG["vlm_model_source"] = "cli"
    if args.vlm_model:
        CONFIG["vlm_model"] = args.vlm_model
        CONFIG["vlm_model_source"] = "cli"
    if args.voice:
        CONFIG["edge_tts_voice"] = args.voice
        CONFIG["edge_tts_voice_source"] = "cli"
    if args.mimo_video_overview:
        CONFIG["mimo_video_overview"] = True
        CONFIG["mimo_video_overview_source"] = "cli"
    if args.mimo_tts_voice:
        CONFIG["mimo_tts_voice"] = args.mimo_tts_voice
        CONFIG["mimo_tts_voice_source"] = "cli"
    if args.mimo_tts_style:
        CONFIG["mimo_tts_style"] = args.mimo_tts_style
        CONFIG["mimo_tts_style_source"] = "cli"
    if args.vlm_workers is not None:
        CONFIG["vlm_workers"] = max(1, args.vlm_workers)
    if args.tts_workers is not None:
        CONFIG["tts_workers"] = max(1, args.tts_workers)
    if args.tts_timeout is not None:
        CONFIG["tts_timeout"] = max(1, args.tts_timeout)
    if args.allow_partial_tts:
        CONFIG["allow_partial_tts"] = True
    if args.scene_threshold is not None:
        CONFIG["scene_threshold"] = args.scene_threshold
    if args.ducking:
        CONFIG["ducking_mode"] = args.ducking
    if args.edit_mode:
        CONFIG["edit_mode"] = args.edit_mode
    if args.target_duration is not None:
        CONFIG["target_duration"] = args.target_duration
    if args.clip_padding is not None:
        CONFIG["clip_padding"] = max(0.0, args.clip_padding)
    if args.allow_clip_overlap:
        CONFIG["allow_clip_overlap"] = True

    if args.doctor:
        from doctor import main as doctor_main

        doctor_args = ["doctor"]
        if args.doctor_tts_smoke:
            doctor_args.append("--tts-smoke")
        old_argv = sys.argv
        try:
            sys.argv = doctor_args
            sys.exit(doctor_main())
        finally:
            sys.argv = old_argv

    if not args.video:
        parser.error("video is required unless --doctor is used")

    try:
        result = run_pipeline(
            video_path=args.video,
            output_dir=args.output,
            step=args.step,
            style=args.style,
            scene_threshold=args.scene_threshold,
            skip_asr=args.skip_asr,
            resume_dir=args.resume,
        )
        if isinstance(result, dict):
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as e:
        log(f"错误: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
