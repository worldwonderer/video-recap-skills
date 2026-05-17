#!/usr/bin/env python3
"""video-recap: 视频自动解说生成器
输入视频 → 场景检测 → 帧提取 → VLM视觉分析 → ASR转录 → LLM脚本生成 → TTS合成 → 视频组装
"""

import argparse
import json
import sys

from config import CONFIG
from common import log
from pipeline import run_pipeline


# ── CLI ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="video-recap: 视频自动解说生成器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("video", help="输入视频文件路径")
    parser.add_argument("--output", "-o", help="输出目录 (默认: 视频所在目录/output)")
    parser.add_argument("--step", choices=["extract", "detect", "asr", "analyze", "script", "tts", "assemble"],
                        help="仅执行某步骤")
    parser.add_argument("--style", default="纪录片",
                        choices=["短剧", "电视剧", "电影", "纪录片", "科普视频"],
                        help="解说风格 (默认: 纪录片)")
    parser.add_argument("--scene-threshold", type=float, default=0.1,
                        help="场景检测阈值 0.0-1.0 (默认: 0.1, 对应 scdet=10)")
    parser.add_argument("--skip-asr", action="store_true",
                        help="跳过 ASR 转录")
    parser.add_argument("--resume", metavar="WORK_DIR",
                        help="从已有的工作目录继续")
    parser.add_argument("--tts", choices=["auto", "indextts2", "edge-tts", "say"],
                        default="auto", help="TTS 引擎 (默认: auto)")
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
                        help="覆盖 VLM/LLM 模型名 (默认: gpt-4o 或 OPENAI_MODEL 环境变量)")
    parser.add_argument("--vlm-model", type=str, default=None,
                        help="单独覆盖 VLM 模型名 (优先级高于 --model)")
    parser.add_argument("--llm-model", type=str, default=None,
                        help="单独覆盖 LLM 模型名 (优先级高于 --model)")
    parser.add_argument("--agent-mode", action="store_true",
                        help="Agent 模式：在解说脚本步骤暂停，等待 Agent 手动写解说词")
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

    args = parser.parse_args()

    # 覆盖配置
    CONFIG["tts_engine"] = args.tts
    CONFIG["fps"] = args.fps
    CONFIG["burn_subtitles"] = args.burn_subtitles
    CONFIG["context_info"] = args.context
    if args.model:
        CONFIG["vlm_model"] = args.model
        CONFIG["llm_model"] = args.model
    if args.vlm_model:
        CONFIG["vlm_model"] = args.vlm_model
    if args.llm_model:
        CONFIG["llm_model"] = args.llm_model
    if args.voice:
        CONFIG["edge_tts_voice"] = args.voice
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

    try:
        result = run_pipeline(
            video_path=args.video,
            output_dir=args.output,
            step=args.step,
            style=args.style,
            scene_threshold=args.scene_threshold,
            skip_asr=args.skip_asr,
            resume_dir=args.resume,
            agent_mode=args.agent_mode,
        )
        if isinstance(result, dict):
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as e:
        log(f"错误: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
