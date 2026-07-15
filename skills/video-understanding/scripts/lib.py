"""Self-contained config + utilities for this skill (no cross-skill imports).
Merged from the shared core; reads the same env vars as the rest of the bundle."""
import json
import hashlib
import os
import re
import subprocess
import time
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


# ── 配置 ──────────────────────────────────────────────────────────────

DEFAULT_MIMO_API_URL = "https://api.xiaomimimo.com/v1"
DEFAULT_MIMO_TOKEN_PLAN_CLUSTER = "cn"
MIMO_TOKEN_PLAN_API_URLS = {
    "cn": "https://token-plan-cn.xiaomimimo.com/v1",
    "sgp": "https://token-plan-sgp.xiaomimimo.com/v1",
    "ams": "https://token-plan-ams.xiaomimimo.com/v1",
}
DEFAULT_MIMO_MODEL = "mimo-v2.5"          # VLM / chat (vision understanding)
DEFAULT_MIMO_ASR_MODEL = "mimo-v2.5-asr"  # speech-to-text
DEFAULT_MIMO_TTS_MODEL = "mimo-v2.5-tts"  # text-to-speech


def normalize_api_url(raw_url):
    """Normalize a MiMo (OpenAI-compatible) base URL or chat/completions endpoint."""
    url = (raw_url or DEFAULT_MIMO_API_URL).rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    return f"{url}/chat/completions"


def is_mimo_token_plan_key(api_key):
    """Return True for Xiaomi MiMo Token Plan keys, which use token-plan base URLs."""
    return str(api_key or "").strip().startswith("tp-")


def default_mimo_api_url(api_key="", cluster=None):
    """Pick the correct MiMo base URL for pay-as-you-go vs Token Plan keys.

    MiMo uses independent credentials for pay-as-you-go (`sk-*`) and Token Plan
    (`tp-*`). Token Plan keys must be sent to the Token Plan cluster base URL,
    not the pay-as-you-go `api.xiaomimimo.com` endpoint.
    """
    if is_mimo_token_plan_key(api_key):
        cluster_name = (cluster or os.environ.get("MIMO_TOKEN_PLAN_CLUSTER") or DEFAULT_MIMO_TOKEN_PLAN_CLUSTER)
        cluster_name = str(cluster_name).strip().lower()
        return MIMO_TOKEN_PLAN_API_URLS.get(cluster_name, MIMO_TOKEN_PLAN_API_URLS[DEFAULT_MIMO_TOKEN_PLAN_CLUSTER])
    return DEFAULT_MIMO_API_URL


def env_int(name, default, *, minimum=None):
    """Read an integer env var; ignore malformed values instead of crashing import."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        value = max(minimum, value)
    return value


def env_bool(name, default=False):
    """Read common boolean env var forms."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_float(name, default, *, minimum=None):
    """Read a float env var; ignore malformed values instead of crashing import."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        value = max(minimum, value)
    return value


# Single MiMo credential powers ASR + VLM + TTS. Per-capability overrides
# (MIMO_VIDEO_API_KEY / MIMO_TTS_API_KEY / MIMO_ASR_API_KEY and their *_API_URL forms)
# are optional and fall back to MIMO_API_KEY / MIMO_API_URL. Token-Plan keys (tp-*) auto-
# route to the Token-Plan cluster base URL; pay-as-you-go keys use api.xiaomimimo.com.
_mimo_api_key = os.environ.get("MIMO_API_KEY", "")
_mimo_video_api_key = os.environ.get("MIMO_VIDEO_API_KEY", "") or _mimo_api_key
_mimo_tts_api_key = os.environ.get("MIMO_TTS_API_KEY", "") or _mimo_api_key
_mimo_asr_api_key = os.environ.get("MIMO_ASR_API_KEY", "") or _mimo_api_key
_raw_api_url = os.environ.get("MIMO_API_URL") or default_mimo_api_url(_mimo_api_key)
_raw_mimo_video_api_url = (
    os.environ.get("MIMO_VIDEO_API_URL")
    or os.environ.get("MIMO_API_URL")
    or default_mimo_api_url(_mimo_video_api_key)
)
_raw_mimo_tts_api_url = (
    os.environ.get("MIMO_TTS_API_URL")
    or os.environ.get("MIMO_API_URL")
    or default_mimo_api_url(_mimo_tts_api_key)
)
_raw_mimo_asr_api_url = (
    os.environ.get("MIMO_ASR_API_URL")
    or os.environ.get("MIMO_API_URL")
    or default_mimo_api_url(_mimo_asr_api_key)
)

CONFIG = {
    "api_provider": "mimo",
    "api_provider_source": "default",
    "api_url": normalize_api_url(_raw_api_url),
    "api_url_source": "env" if os.environ.get("MIMO_API_URL") else "default",
    "api_key": _mimo_api_key,
    "api_key_source": "MIMO_API_KEY",
    "mimo_api_url": normalize_api_url(_raw_api_url),
    "mimo_api_url_source": "env" if os.environ.get("MIMO_API_URL") else "default",
    "mimo_api_key": _mimo_api_key,
    "mimo_api_key_source": "MIMO_API_KEY",
    "mimo_video_api_url": normalize_api_url(_raw_mimo_video_api_url),
    "mimo_video_api_url_source": "env" if (
        os.environ.get("MIMO_VIDEO_API_URL") or os.environ.get("MIMO_API_URL")
    ) else "default",
    "mimo_video_api_key": _mimo_video_api_key,
    "mimo_video_api_key_source": "MIMO_VIDEO_API_KEY" if os.environ.get("MIMO_VIDEO_API_KEY") else "MIMO_API_KEY",
    "mimo_tts_api_url": normalize_api_url(_raw_mimo_tts_api_url),
    "mimo_tts_api_url_source": "env" if (
        os.environ.get("MIMO_TTS_API_URL") or os.environ.get("MIMO_API_URL")
    ) else "default",
    "mimo_tts_api_key": _mimo_tts_api_key,
    "mimo_tts_api_key_source": "MIMO_TTS_API_KEY" if os.environ.get("MIMO_TTS_API_KEY") else "MIMO_API_KEY",
    "mimo_asr_api_url": normalize_api_url(_raw_mimo_asr_api_url),
    "mimo_asr_api_url_source": "env" if (
        os.environ.get("MIMO_ASR_API_URL") or os.environ.get("MIMO_API_URL")
    ) else "default",
    "mimo_asr_api_key": _mimo_asr_api_key,
    "mimo_asr_api_key_source": "MIMO_ASR_API_KEY" if os.environ.get("MIMO_ASR_API_KEY") else "MIMO_API_KEY",
    "mimo_model": os.environ.get("MIMO_MODEL", DEFAULT_MIMO_MODEL),
    "mimo_model_source": "env" if os.environ.get("MIMO_MODEL") else "default",
    "mimo_video_model": os.environ.get("MIMO_VIDEO_MODEL") or os.environ.get("MIMO_MODEL", DEFAULT_MIMO_MODEL),
    "mimo_video_model_source": "env" if (
        os.environ.get("MIMO_VIDEO_MODEL") or os.environ.get("MIMO_MODEL")
    ) else "default",
    "vlm_model": os.environ.get("MIMO_MODEL", DEFAULT_MIMO_MODEL),
    "vlm_model_source": "env" if os.environ.get("MIMO_MODEL") else "default",
    "mimo_asr_model": os.environ.get("MIMO_ASR_MODEL", DEFAULT_MIMO_ASR_MODEL),
    "mimo_asr_model_source": "env" if os.environ.get("MIMO_ASR_MODEL") else "default",
    "mimo_asr_language": os.environ.get("MIMO_ASR_LANGUAGE", "auto"),  # auto | zh | en
    "mimo_asr_base64_max_mb": env_float("MIMO_ASR_BASE64_MAX_MB", 10.0, minimum=1.0),
    # ASR 分段窗口秒数。越小 → 长视频的对白时间戳越精细（默认 15s）。旧值 180s 会把 >3min
    # 视频的对白塌缩成一个时间戳，既让 brief 无法定位对白，又触发 detect.py 的粗粒度跳过，
    # 使 overlaps_speech/安静窗口判断失真。代价是更多 ASR 调用；ASR 慢时可调大。
    "asr_segment_seconds": env_float("ASR_SEGMENT_SECONDS", 15.0, minimum=5.0),
    "scene_threshold": 0.1,
    "scene_threshold_source": "default",
    "mimo_tts_model": os.environ.get("MIMO_TTS_MODEL", DEFAULT_MIMO_TTS_MODEL),
    "mimo_tts_model_source": "env" if os.environ.get("MIMO_TTS_MODEL") else "default",
    "mimo_tts_voice": os.environ.get("MIMO_TTS_VOICE", "冰糖"),
    "mimo_tts_voice_source": "env" if os.environ.get("MIMO_TTS_VOICE") else "default",
    "mimo_tts_style": os.environ.get(
        "MIMO_TTS_STYLE",
        "自然、清晰、有感染力，像在给观众讲故事；随剧情起伏，该紧张时紧张、该动情时动情，不平铺直叙。",
    ),
    "mimo_tts_style_source": "env" if os.environ.get("MIMO_TTS_STYLE") else "default",
    "mimo_media_resolution": os.environ.get("MIMO_MEDIA_RESOLUTION", "default"),
    "mimo_media_resolution_source": "env" if os.environ.get("MIMO_MEDIA_RESOLUTION") else "default",
    "mimo_video_overview": env_bool("MIMO_VIDEO_OVERVIEW", False),  # opt-in (--mimo-video-overview / =1); when on it becomes the PRIMARY per-scene description, frames stay the anchor/fallback
    "mimo_video_overview_source": "env" if os.environ.get("MIMO_VIDEO_OVERVIEW") else "default",
    "mimo_video_fps": env_float("MIMO_VIDEO_FPS", 3.0, minimum=0.1),
    "mimo_video_fps_source": "env" if os.environ.get("MIMO_VIDEO_FPS") else "default",
    "mimo_video_chunk_max_seconds": env_float("MIMO_VIDEO_CHUNK_MAX_SECONDS", 20.0, minimum=1.0),
    "mimo_video_chunk_min_seconds": env_float("MIMO_VIDEO_CHUNK_MIN_SECONDS", 1.0, minimum=0.2),
    "mimo_video_chunk_timeout": env_int("MIMO_VIDEO_CHUNK_TIMEOUT", 180, minimum=1),
    "mimo_video_base64_max_mb": env_float("MIMO_VIDEO_BASE64_MAX_MB", 45.0, minimum=1.0),
    # Per-scene frame VLM sampling — scale frames with scene length instead of a hard cap of 6
    "vlm_seconds_per_frame": env_float("VLM_SECONDS_PER_FRAME", 4.0, minimum=0.5),
    "vlm_max_frames": env_int("VLM_MAX_FRAMES", 16, minimum=3),
    "vlm_max_tokens": env_int("VLM_MAX_TOKENS", 1500, minimum=200),
    "mimo_video_prompt": os.environ.get(
        "MIMO_VIDEO_PROMPT",
        "请用中文分析这个视频分片的主要人物、场景变化、关键动作、情绪走向和剧情冲突，"
        "重点提取适合写短视频解说的故事线索。不要泛泛复述画面，要标出对后续写稿有用的信息。",
    ),
    "mimo_disable_thinking": env_bool("MIMO_DISABLE_THINKING", True),
    "mimo_disable_thinking_source": "env" if os.environ.get("MIMO_DISABLE_THINKING") else "default",
    "fps": 0,  # 0 = 自动（≤60s→2fps, ≤5min→1.5fps, >5min→1fps）
    # Storyboard contact sheets are advisory and generated locally where frames/fps are owned.
    # Keep these keys only in the stage that consumes them; other stages do not read storyboard_*.
    "storyboard": env_bool("STORYBOARD", True),  # generate source/edited storyboard contact sheets
    "storyboard_max_tiles": env_int("STORYBOARD_MAX_TILES", 30, minimum=1),  # cap tiles per sheet for legibility
    "storyboard_columns": env_int("STORYBOARD_COLUMNS", 6, minimum=1),  # tile grid columns
    # TTS 语速（字符/秒）。实测 mimo-tts 冰糖音色中位 ~3.9 字/秒，可用 SPEECH_RATE 覆盖
    # 生成解说时使用 speech_rate * safety_margin 作为约束
    "speech_rate": env_float("SPEECH_RATE", 3.9, minimum=0.5),  # 旧值 3.5 系统性偏低 ~10-17%
    "speech_safety_margin": env_float("SPEECH_SAFETY_MARGIN", 0.85, minimum=0.1),  # 保守系数：TTS 实际语速有 ±20% 波动
    # Block-coverage lint thresholds — promoted from inline .get() literals to real CONFIG keys (tunable; defaults unchanged)
    "narration_coverage_target": 0.7,   # rough first-draft/diagnostic fallback; content-led audio decisions may differ (not a quota)
    "narration_coverage_min": 0.5,      # below this coverage → under_narrated
    "narration_block_seconds": 9.0,     # block cadence used to derive target block count
    "original_block_min_seconds": 2.5,  # a deliberate original-audio gap must be at least this long
    "narration_block_min_chars": 16,    # below this avg block size → fragmented_beats
    "fade_ms": 120,  # TTS fade-in/fade-out 时长(ms); keep in sync with assemble default
    "breath_ms": 250,  # 段间呼吸空间(ms)；block recap 块内连贯、块间留原声呼吸
    # Legacy single-pass cut mapping density fields; current writing uses block coverage controls below.
    "target_segments_per_minute": 9.6,   # legacy single-pass cut mapping report only; block recap uses narration_coverage_*
    "min_segments_per_minute": 6.24,     # legacy single-pass cut mapping report only
    "max_narration_gap_seconds": 11.0,   # legacy single-pass cut mapping report only
    "ducking_mode": "fixed",  # fixed | sidechaincompress | none
    "ducking_threshold": 0.15,
    "ducking_ratio": 3,
    "ducking_attack": 10,
    "ducking_release": 300,
    "ducking_level_sc": 2.0,
    "ducking_makeup": 1.2,
    "ducking_narr_weight": 1.5,
    "ducking_orig_volume": env_float("DUCKING_ORIG_VOLUME", 0.3, minimum=0.0),  # 解说时原声基准音量
    "zone_ducking_volume": 0.12,    # 解说时原声压低到的音量
    "zone_fade_seconds": 0.5,      # 解说/原声切换的淡入淡出时长(秒)
    "idle_orig_volume": env_float("IDLE_ORIG_VOLUME", 1.0, minimum=0.0),  # 解说间隙(无旁白)时的原声音量，铺底避免顿挫
    "duck_fade_seconds": env_float("DUCK_FADE_SECONDS", 0.3, minimum=0.0),  # 原声 ducking 过渡淡入淡出(秒)
    "bgm_path": os.environ.get("BGM_PATH", "").strip(),  # 背景音乐文件(可选)，留空则不加 BGM
    "source_video": os.environ.get("SOURCE_VIDEO", "").strip(),  # 剪辑模式下的原始视频(可选)，用于时间线/剪映导出引用原片片段
    "export_jianying": env_bool("EXPORT_JIANYING", False),  # 渲染后可选导出剪映草稿(默认关；与核心解耦)
    "jianying_draft_dir": os.environ.get("JIANYING_DRAFT_DIR", "").strip(),  # 剪映草稿输出父目录(留空=work_dir)
    "jianying_bundle_media": env_bool("JIANYING_BUNDLE_MEDIA", True),  # 默认开：macOS 剪映沙箱读不到外部路径，须把素材拷进草稿目录
    "bgm_volume": env_float("BGM_VOLUME", 0.18, minimum=0.0),  # BGM 铺底音量
    "bgm_ducking_volume": env_float("BGM_DUCKING_VOLUME", 0.10, minimum=0.0),  # 旁白时 BGM 压低到的音量
    "narration_speed": env_float("NARRATION_SPEED", 1.15, minimum=0.5),  # 解说整体提速(atempo)，默认回到可懂区间；长片可设 1.0
    "narration_cumulative_tempo_max": env_float("NARRATION_CUMULATIVE_TEMPO_MAX", 1.35, minimum=1.0),  # TTS rate × 全局 atempo × 段内 atempo 的累计上限
    "narration_cumulative_tempo_hard_max": env_float("NARRATION_CUMULATIVE_TEMPO_HARD_MAX", 1.40, minimum=1.0),  # QC/阻断硬上限
    "tts_segment_tempo_max": env_float("TTS_SEGMENT_TEMPO_MAX", 1.20, minimum=1.0),  # 兼容旧段内 atempo 上限；实际会被累计预算收紧
    "mask_source_subtitles": env_bool("MASK_SOURCE_SUBTITLES", True),  # 遮挡原片烧录字幕（默认开；无烧录字幕素材设 false）
    "source_subtitle_mask_ratio": env_float("SOURCE_SUBTITLE_MASK_RATIO", 0.14, minimum=0.0),  # 底部遮挡比例
    "narration_delay_seconds": env_float("NARRATION_DELAY_SECONDS", 0.0, minimum=0.0),  # 默认严格采用 Agent 写入的 start；旧项目可显式恢复延迟
    "narration_tail_pad_seconds": 0.1,  # 解说尾部最少留白；短 slot 会自动压低 delay 避免截断
    "quiet_overlap_min_ratio": 0.8,  # 解说段至少多少比例落在安静窗口内才标记为非对白重叠
    "visual_beat_max_seconds": 18.0,  # 单段解说超过该时长且跨多个帧锚点时给 lint 提醒
    "visual_beat_max_facts": 3,  # 单段解说最多建议覆盖的 frame_facts 锚点数量
    "asr_chunk_min_chars": env_int("ASR_CHUNK_MIN_CHARS", 500, minimum=1),  # brief 中 ASR 写作分块最小字数/词数
    "asr_chunk_max_chars": env_int("ASR_CHUNK_MAX_CHARS", 800, minimum=1),  # brief 中 ASR 写作分块最大字数/词数
    "speech_ducking_volume": env_float("SPEECH_DUCKING_VOLUME", 0.2, minimum=0.0),    # 解说与对白重叠时原声音量
    "silence_noise_threshold": "-25dB",  # ffmpeg silencedetect 噪声阈值
    # Short acoustic pauses are not quiet windows; they are sentence-boundary candidates.
    # A looser threshold plus ASR-punctuation alignment lets the writer enter after a full
    # source sentence instead of ducking it halfway through.
    "source_boundary_noise_threshold": os.environ.get("SOURCE_BOUNDARY_NOISE_THRESHOLD", "-18dB"),
    "source_boundary_min_pause": env_float("SOURCE_BOUNDARY_MIN_PAUSE", 0.12, minimum=0.05),
    "source_boundary_max_alignment_error": env_float(
        "SOURCE_BOUNDARY_MAX_ALIGNMENT_ERROR", 2.1, minimum=0.2
    ),
    "silence_min_duration": 0.3,     # 静音最短持续秒数
    "quiet_window_min": 1.0,         # 可放解说的安静窗口最短秒数
    "silence_merge_gap": 0.5,        # 相邻静音段间隔<此值时合并
    "scene_merge_min": 4.0,         # 场景合并最短时长，<此值的场景合并到相邻场景
    "scene_junk_filter": env_bool("SCENE_JUNK_FILTER", True),  # 过滤连续黑/白帧无效过渡场景
    "scene_junk_dark_luma": env_float("SCENE_JUNK_DARK_LUMA", 8.0, minimum=0.0),
    "scene_junk_bright_luma": env_float("SCENE_JUNK_BRIGHT_LUMA", 245.0, minimum=0.0),
    "scene_junk_pixel_ratio": env_float("SCENE_JUNK_PIXEL_RATIO", 0.995, minimum=0.0),
    "context_info": "",              # 额外上下文（节目名、角色名等）
    "context_info_source": "default",
    "fps_source": "default",
    "style": "纪录片",               # 解说风格（resume 时随 run_settings 持久化/恢复）
    "style_source": "default",
    "tts_dynamic_params": True,  # 启用动态语速调节
    "vlm_workers": env_int("VLM_WORKERS", 8, minimum=1),  # VLM 并行分析线程数
    "tts_workers": env_int("TTS_WORKERS", 4, minimum=1),  # TTS 并行合成线程数
    "tts_timeout": env_int("TTS_TIMEOUT", 90, minimum=1),  # 单段 TTS 命令超时秒数
    "tts_retries": env_int("TTS_RETRIES", 3, minimum=1),  # 单段 TTS 失败重试次数
    "allow_partial_tts": env_bool("ALLOW_PARTIAL_TTS", False),
    "tts_segment_normalize": env_bool("TTS_SEGMENT_NORMALIZE", True),  # 单段 TTS RMS 归一，降低段间忽大忽小
    "tts_segment_target_rms_dbfs": env_float("TTS_SEGMENT_TARGET_RMS_DBFS", -20.0),
    "tts_segment_peak_limit": env_float("TTS_SEGMENT_PEAK_LIMIT", 0.98, minimum=0.1),
    "edit_mode": os.environ.get("EDIT_MODE", "full"),  # full | cut
    "edit_mode_source": "env" if os.environ.get("EDIT_MODE") else "default",
    "target_duration": os.environ.get("TARGET_DURATION", ""),  # cut 模式目标成片时长，如 10m
    "target_duration_source": "env" if os.environ.get("TARGET_DURATION") else "default",
    "clip_padding": env_float("CLIP_PADDING", 0.0, minimum=0.0),  # cut 模式片段两端扩展秒数
    "clip_padding_source": "env" if os.environ.get("CLIP_PADDING") else "default",
    "allow_clip_overlap": env_bool("ALLOW_CLIP_OVERLAP", False),  # cut 模式是否允许重复/重叠使用原片
    "burn_subtitles": env_bool("BURN_SUBTITLES", True),  # 烧录解说字幕（默认开；遮挡原字幕后需自带字幕，否则字幕区空白）
    "force_video_reencode": env_bool("FORCE_VIDEO_REENCODE", False),  # 组装时重编码视频，修复部分容器时间戳问题
    # 成片末端整体响度归一（默认混音偏轻，归一后更接近常见短视频响度；样片约 -11.9，默认取更安全的 -14）
    "final_loudnorm": env_bool("FINAL_LOUDNORM", True),  # 组装末端做一次整体响度归一
    "target_lufs": env_float("TARGET_LUFS", -14.0),       # 目标综合响度 (LUFS)
    "target_true_peak": env_float("TARGET_TRUE_PEAK", -1.0),  # 目标真峰值 (dBTP)
    "target_lra": env_float("TARGET_LRA", 11.0),          # 目标响度范围 (LU)
    "final_limiter_peak": env_float("FINAL_LIMITER_PEAK", 0.98, minimum=0.1),  # loudnorm 后峰值保护 limiter
    "subtitle_font_name": os.environ.get("SUBTITLE_FONT_NAME", "Arial"),
    "subtitle_font_size": env_int("SUBTITLE_FONT_SIZE", 42, minimum=8),
    "subtitle_primary_color": os.environ.get("SUBTITLE_PRIMARY_COLOR", "&H00FFFFFF"),
    "subtitle_outline_color": os.environ.get("SUBTITLE_OUTLINE_COLOR", "&H00000000"),
    "subtitle_outline": env_float("SUBTITLE_OUTLINE", 2.0, minimum=0.0),
    "subtitle_shadow": env_float("SUBTITLE_SHADOW", 1.0, minimum=0.0),
    "subtitle_margin_v": env_int("SUBTITLE_MARGIN_V", 48, minimum=0),
    "subtitle_margin_l": env_int("SUBTITLE_MARGIN_L", 40, minimum=0),
    "subtitle_margin_r": env_int("SUBTITLE_MARGIN_R", 40, minimum=0),
    "subtitle_alignment": env_int("SUBTITLE_ALIGNMENT", 2, minimum=1),
    "subtitle_max_chars": env_int("SUBTITLE_MAX_CHARS", 20, minimum=6),
    "subtitle_play_res_x": env_int("SUBTITLE_PLAY_RES_X", 1280, minimum=1),
    "subtitle_play_res_y": env_int("SUBTITLE_PLAY_RES_Y", 720, minimum=1),
}

SCRIPT_DIR = Path(__file__).parent
PROMPTS_DIR = SCRIPT_DIR.parent / "references"

def narration_tempo_budget(tts_rate_offset=0.0, *, config=None):
    """Return the canonical tempo budget shared by voiceover and assemble."""
    cfg = config or CONFIG
    global_speed = max(0.01, float(cfg.get("narration_speed", 1.0) or 1.0))
    rate_factor = max(0.01, 1.0 + float(tts_rate_offset or 0.0))
    cumulative_max = max(1.0, float(cfg.get("narration_cumulative_tempo_max", 1.35) or 1.35))
    hard_max = max(cumulative_max, float(cfg.get("narration_cumulative_tempo_hard_max", 1.40) or 1.40))
    legacy_segment_cap = max(1.0, float(cfg.get("tts_segment_tempo_max", 1.20) or 1.20))
    segment_tempo_max = max(1.0, min(legacy_segment_cap, cumulative_max / (global_speed * rate_factor)))
    return {
        "global_narration_speed": global_speed,
        "tts_rate_factor": rate_factor,
        "cumulative_tempo_max": cumulative_max,
        "cumulative_tempo_hard_max": hard_max,
        "segment_tempo_max": segment_tempo_max,
        "max_raw_duration_factor": global_speed * segment_tempo_max,
    }

def log(msg):
    print(f"[video-recap] {msg}", flush=True)

def run_cmd(cmd, **kwargs):
    """运行命令，返回 CompletedProcess"""
    if isinstance(cmd, list):
        display_parts = []
        for part in cmd:
            text = str(part)
            display_parts.append(text if len(text) <= 240 else text[:237] + "...")
        display = " ".join(display_parts)
    else:
        display = str(cmd)
        if len(display) > 2000:
            display = display[:1997] + "..."
    log(f"运行: {display}")
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)

def get_video_duration(video_path):
    """获取视频时长（秒）"""
    cmd = ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
           "-of", "csv=p=0", str(video_path)]
    result = run_cmd(cmd)
    if result.returncode != 0:
        return 0.0
    try:
        return float(result.stdout.strip())
    except (TypeError, ValueError):
        return 0.0

def stable_json_dumps(value):
    """Serialize values deterministically for non-secret cache fingerprints."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)

def stable_hash(value):
    """Return an md5 digest for deterministic JSON-serializable values."""
    return hashlib.md5(stable_json_dumps(value).encode("utf-8")).hexdigest()

def file_fingerprint(path, chunk_size=1024 * 1024):
    """Return a full-content fingerprint for cache-correct identity checks.

    This intentionally avoids mtime/path so copied videos or JSON artifacts can
    be reused when their bytes are identical, while any byte change invalidates
    the cache even if timestamps, size, head, or tail bytes are misleading.
    """
    h = hashlib.sha256()
    with open(os.fspath(path), "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()
def video_fingerprint(video_path):
    """Full video content fingerprint used as the root pipeline asset print."""
    return file_fingerprint(video_path)

def step_cache_key(video_path, step_name, params_fingerprint=""):
    """Build a cache key from video content, step name and step parameters."""
    params_digest = params_fingerprint
    if not isinstance(params_digest, str):
        params_digest = stable_hash(params_digest)
    payload = f"{video_fingerprint(video_path)}_{step_name}_{params_digest}"
    return hashlib.md5(payload.encode("utf-8")).hexdigest()

def _retry_after_seconds(value, fallback):
    """Parse Retry-After seconds or HTTP-date; return fallback on malformed input."""
    if not value:
        return fallback
    try:
        return max(fallback, max(0, int(value)))
    except (TypeError, ValueError):
        pass
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(fallback, max(0, int((retry_at - datetime.now(timezone.utc)).total_seconds())))
    except (TypeError, ValueError, IndexError, OverflowError):
        return fallback


_ERROR_DATA_URL_RE = re.compile(
    r"data:(?:audio|video|image)/[^;,\s\"'<>]+;base64,[A-Za-z0-9+/=]+",
    re.IGNORECASE,
)
_ERROR_KEY_RE = re.compile(r"\b(?:tp|sk)-[A-Za-z0-9_-]{8,}\b")


def _sanitize_api_error(value, limit=500):
    """Bound transport diagnostics without echoing request media or credentials."""
    text = _ERROR_DATA_URL_RE.sub("<redacted-data-url>", str(value or ""))
    text = _ERROR_KEY_RE.sub("<redacted-key>", text)
    return text[:limit]

def _api_headers(api_provider=None, api_url=None, api_key=None):
    """Build MiMo auth headers (OpenAI-compatible chat/completions with an api-key header)."""
    del api_provider, api_url  # MiMo is the only provider; signature kept for call sites
    key = CONFIG.get("api_key", "") if api_key is None else api_key
    return {
        "Content-Type": "application/json",
        "User-Agent": "video-recap/1.0",
        "api-key": key,
    }

def _prepare_api_payload(payload, api_provider=None, api_url=None):
    """Normalize payload fields for MiMo's OpenAI-compatible chat/completions API."""
    del api_provider, api_url
    normalized = dict(payload)
    if "max_tokens" in normalized and "max_completion_tokens" not in normalized:
        normalized["max_completion_tokens"] = normalized.pop("max_tokens")
    model = str(normalized.get("model") or "")
    if (
        CONFIG.get("mimo_disable_thinking", True)
        and not model.endswith(("-tts", "-asr"))
        and "thinking" not in normalized
    ):
        # MiMo V2.5 may spend small max_completion_tokens budgets on reasoning_content.
        # The recap pipeline needs visible text, so disable thinking unless set explicitly.
        normalized["thinking"] = {"type": "disabled"}
    return normalized

def _mimo_endpoint(kind):
    """Return per-capability MiMo endpoint settings (video understanding / TTS / ASR)."""
    by_kind = {
        "video": ("mimo_video_api_url", "mimo_video_api_key", "mimo_video_api_key_source"),
        "tts": ("mimo_tts_api_url", "mimo_tts_api_key", "mimo_tts_api_key_source"),
        "asr": ("mimo_asr_api_url", "mimo_asr_api_key", "mimo_asr_api_key_source"),
    }
    if kind not in by_kind:
        raise ValueError(f"Unsupported MiMo endpoint kind: {kind}")
    url_key, key_key, src_key = by_kind[kind]
    return {
        "api_url": CONFIG.get(url_key) or CONFIG.get("mimo_api_url"),
        "api_key": CONFIG.get(key_key) or CONFIG.get("mimo_api_key"),
        "api_key_source": CONFIG.get(src_key, "MIMO_API_KEY"),
    }

def _call_mimo_endpoint(kind, payload, max_retries=10):
    settings = _mimo_endpoint(kind)
    return api_call(
        payload,
        max_retries=max_retries,
        api_provider="mimo",
        api_url=settings["api_url"],
        api_key=settings["api_key"],
        api_key_source=settings["api_key_source"],
    )

def mimo_video_api_call(payload, max_retries=10):
    """Call the MiMo video-understanding endpoint."""
    return _call_mimo_endpoint("video", payload, max_retries=max_retries)

def mimo_asr_api_call(payload, max_retries=10):
    """Call the MiMo speech-recognition (ASR) endpoint."""
    return _call_mimo_endpoint("asr", payload, max_retries=max_retries)

def api_call(payload, max_retries=8, *, api_provider=None, api_url=None, api_key=None, api_key_source=None):
    """调用 OpenAI-compatible API，带重试。

    长视频理解会发出数百次 VLM/ASR 调用，集群的 429 限流是常态而非错误，所以重试更耐心
    （更多次数 + 退避封顶 60s + 遵从 Retry-After），避免一次瞬时限流就中止整段理解。
    集群的配额窗口常以分钟计，所以 429 在没有 Retry-After 时也至少等 10s，给窗口时间复位。
    """
    endpoint = normalize_api_url(api_url if api_url is not None else CONFIG["api_url"])
    headers = _api_headers(api_provider=api_provider, api_url=endpoint, api_key=api_key)
    data = json.dumps(_prepare_api_payload(payload, api_provider=api_provider, api_url=endpoint)).encode("utf-8")

    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(endpoint, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=300) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                return result
        except urllib.error.HTTPError as e:
            body = _sanitize_api_error(e.read().decode("utf-8", errors="replace"))
            wait = min(2 ** attempt, 60)
            if e.code == 429:
                retry_after = e.headers.get("Retry-After")
                wait = _retry_after_seconds(retry_after, max(wait, 10))
                log(f"API 速率限制 (尝试 {attempt+1}/{max_retries}), 等待 {wait}s")
            elif e.code == 401:
                key_name = api_key_source or CONFIG.get("api_key_source", "MIMO_API_KEY")
                raise RuntimeError(f"API 认证失败 (401)。请检查 {key_name} 和 API URL 是否匹配。")
            elif e.code == 403:
                hint = "API 访问被拒绝 (403)。"
                if "1010" in body or "cloudflare" in body.lower():
                    hint += "IP 被 Cloudflare 限流，请等待几分钟后重试。"
                    raise RuntimeError(hint)
                hint += "请检查 API key 权限和 API URL 设置。"
                raise RuntimeError(hint)
            elif e.code == 405:
                raise RuntimeError("API 端点不可用 (405)，可能被 WAF 拦截。请检查 MIMO_API_URL 或稍后重试。")
            elif e.code == 503:
                log(f"API 服务暂不可用 (503)，等待 {wait}s (尝试 {attempt+1}/{max_retries})")
            elif e.code == 524:
                # Cloudflare 超时：服务端处理超时，需要更长退避
                wait = max(wait, 4 * (attempt + 1))
                log(f"API 超时 (524)，等待 {wait}s (尝试 {attempt+1}/{max_retries})")
            else:
                log(f"API 调用失败 (尝试 {attempt+1}/{max_retries}): HTTP {e.code} — {body}")
            if attempt < max_retries - 1:
                time.sleep(wait)
            else:
                raise RuntimeError(f"API 调用失败 {max_retries} 次: HTTP {e.code} — {body}")
        except (urllib.error.URLError, Exception) as e:
            wait = min(2 ** attempt, 60)
            safe_error = _sanitize_api_error(e)
            log(f"API 调用失败 (尝试 {attempt+1}/{max_retries}): {safe_error}")
            if attempt < max_retries - 1:
                log(f"等待 {wait}s 后重试...")
                time.sleep(wait)
            else:
                raise RuntimeError(f"API 调用失败 {max_retries} 次: {safe_error}")

def load_prompt(name):
    """加载 prompt 模板"""
    path = PROMPTS_DIR / "prompt-templates.md"
    if not path.exists():
        return None
    content = path.read_text(encoding="utf-8")
    # 用 ### NAME 和 ### 分隔提取对应 prompt
    pattern = rf"### {name}\s*\n(.*?)(?=\n### |\Z)"
    m = re.search(pattern, content, re.DOTALL)
    return m.group(1).strip() if m else None
