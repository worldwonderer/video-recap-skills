import os
from pathlib import Path

# ── 配置 ──────────────────────────────────────────────────────────────

CONFIG = {
    "api_url": os.environ.get("OPENAI_API_URL", "https://api.openai.com/v1/chat/completions"),
    "api_key": os.environ.get("OPENAI_API_KEY", ""),
    "vlm_model": os.environ.get("OPENAI_MODEL", "gpt-4o"),
    "llm_model": os.environ.get("OPENAI_MODEL", "gpt-4o"),
    "asr_bin": os.environ.get("ASR_BIN", "local_transcribe"),
    "asr_model_dir": os.environ.get("ASR_MODEL_DIR", ""),
    "scene_threshold": 0.1,
    "tts_engine": "auto",  # auto | indextts2 | edge-tts | say
    "edge_tts_voice": "zh-CN-YunxiNeural",
    "style_voices": {
        "短剧": "zh-CN-YunxiNeural",
        "电视剧": "zh-CN-XiaoxiaoNeural",
        "电影": "zh-CN-YunjianNeural",
        "纪录片": "zh-CN-YunyangNeural",
        "科普视频": "zh-CN-XiaoyiNeural",
    },
    "say_voice": "Tingting",
    "fps": 0,  # 0 = 自动（≤60s→2fps, ≤5min→1.5fps, >5min→1fps）
    # TTS 语速（字符/秒），由校准得出。edge-tts YunxiNeural 约 3.5 字/秒
    # 生成解说时使用 speech_rate * safety_margin 作为约束
    "speech_rate": 3.5,
    "speech_safety_margin": 0.85,  # 保守系数：TTS 实际语速有 ±20% 波动
    "fade_ms": 300,  # TTS fade-in/fade-out 时长(ms)
    "breath_ms": 600,  # 段间呼吸空间(ms)，原值 0ms
    "ducking_mode": "fixed",  # fixed | sidechaincompress | none
    "ducking_threshold": 0.15,
    "ducking_ratio": 3,
    "ducking_attack": 10,
    "ducking_release": 300,
    "ducking_level_sc": 2.0,
    "ducking_makeup": 1.2,
    "ducking_narr_weight": 1.5,
    "ducking_orig_volume": 0.5,
    "narration_mode": "zone",       # "zone": 大段解说+原声交替 | "scene": 逐场景解说
    "zone_min_duration": 6.0,        # 解说区最短秒数，短于此的安静窗口不单独成区
    "zone_merge_gap": 3.0,          # 相邻安静窗口间隔<此值时合并为一个解说区
    "zone_ducking_volume": 0.12,    # 解说区原声音量（大幅压低）
    "zone_fade_seconds": 0.5,      # 解说/原声切换的淡入淡出时长(秒)
    "narration_delay_seconds": 1.5,  # 解说延迟放置秒数，让画面先出现再解说
    "quiet_ducking_volume": 0.7,     # 解说在安静窗口时原声音量(scene模式)
    "speech_ducking_volume": 0.2,    # 解说与对白重叠时原声音量(scene模式)
    "silence_noise_threshold": "-25dB",  # ffmpeg silencedetect 噪声阈值
    "silence_min_duration": 0.3,     # 静音最短持续秒数
    "quiet_window_min": 1.0,         # 可放解说的安静窗口最短秒数
    "silence_merge_gap": 0.5,        # 相邻静音段间隔<此值时合并
    "scene_merge_min": 4.0,         # 场景合并最短时长，<此值的场景合并到相邻场景
    "temporal_gap_min": 8.0,        # 长场景中最小空白间隔秒数（触发追加解说）
    "context_info": "",              # 额外上下文（节目名、角色名等）
    "tts_dynamic_params": True,  # 启用动态语速调节
    "vlm_workers": 8,            # VLM 并行分析线程数
    "tts_workers": 4,            # TTS 并行合成线程数
    "fill_workers": 4,           # 填充解说并行 API 线程数
    "skip_narrative_analysis": True,  # 跳过叙事结构分析（省57-130s，对质量影响极小）
    "burn_subtitles": False,  # 烧录字幕到视频（需要重编码）
}

SCRIPT_DIR = Path(__file__).parent
PROMPTS_DIR = SCRIPT_DIR.parent / "references"

