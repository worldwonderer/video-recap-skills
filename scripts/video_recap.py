#!/usr/bin/env python3
"""video-recap: 视频自动解说生成器
输入视频 → 场景检测 → 帧提取 → VLM视觉分析 → ASR转录 → LLM脚本生成 → TTS合成 → 视频组装
"""

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import urllib.request
import urllib.error
import wave
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

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


# ── 工具函数 ──────────────────────────────────────────────────────────

def log(msg):
    print(f"[video-recap] {msg}", flush=True)


def run_cmd(cmd, **kwargs):
    """运行命令，返回 CompletedProcess"""
    log(f"运行: {' '.join(str(c) for c in cmd) if isinstance(cmd, list) else cmd}")
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def get_video_duration(video_path):
    """获取视频时长（秒）"""
    cmd = ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
           "-of", "csv=p=0", str(video_path)]
    result = run_cmd(cmd)
    if result.returncode != 0:
        return 0.0
    return float(result.stdout.strip())


def api_call(payload, max_retries=5):
    """调用 OpenAI-compatible API，带重试"""
    headers = {
        "Authorization": f"Bearer {CONFIG['api_key']}",
        "Content-Type": "application/json",
        "User-Agent": "video-recap/1.0",
    }
    data = json.dumps(payload).encode("utf-8")

    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(CONFIG["api_url"], data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=300) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                return result
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:500]
            wait = 2 ** attempt
            if e.code == 429:
                retry_after = e.headers.get("Retry-After")
                if retry_after:
                    wait = max(wait, int(retry_after))
                log(f"API 速率限制 (尝试 {attempt+1}/{max_retries}), 等待 {wait}s")
            elif e.code == 401:
                raise RuntimeError("API 认证失败 (401)。请检查 OPENAI_API_KEY 是否正确。")
            elif e.code == 403:
                hint = "API 访问被拒绝 (403)。"
                if "1010" in body or "cloudflare" in body.lower():
                    hint += "IP 被 Cloudflare 限流，请等待几分钟后重试。"
                    raise RuntimeError(hint)
                hint += "请检查 API key 权限和 OPENAI_API_URL 设置。"
                raise RuntimeError(hint)
            elif e.code == 405:
                raise RuntimeError(f"API 端点不可用 (405)，可能被 WAF 拦截。请检查 OPENAI_API_URL 或稍后重试。")
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
            wait = 2 ** attempt
            log(f"API 调用失败 (尝试 {attempt+1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                log(f"等待 {wait}s 后重试...")
                time.sleep(wait)
            else:
                raise RuntimeError(f"API 调用失败 {max_retries} 次: {e}")


def load_prompt(name):
    """加载 prompt 模板"""
    path = PROMPTS_DIR / "prompt-templates.md"
    if not path.exists():
        return None
    content = path.read_text()
    # 用 ### NAME 和 ### 分隔提取对应 prompt
    pattern = rf"### {name}\s*\n(.*?)(?=\n### |\Z)"
    m = re.search(pattern, content, re.DOTALL)
    return m.group(1).strip() if m else None


# ── Step 1: 帧提取 ───────────────────────────────────────────────────

def extract_frames(video_path, work_dir, fps=None):
    """提取视频帧"""
    fps = fps or CONFIG["fps"]
    frames_dir = work_dir / "frames"
    frames_dir.mkdir(exist_ok=True)

    output_pattern = str(frames_dir / "frame_%05d.jpg")
    cmd = ["ffmpeg", "-y", "-i", str(video_path),
           "-vf", f"fps={fps}", "-q:v", "2", output_pattern]
    result = run_cmd(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"帧提取失败: {result.stderr}")

    frames = sorted(frames_dir.glob("frame_*.jpg"))
    log(f"提取了 {len(frames)} 帧 ({fps}fps)")
    return frames


# ── Step 2: 场景检测 ──────────────────────────────────────────────────

def detect_scenes(video_path, work_dir, threshold=None):
    """使用 ffmpeg scdet 滤镜检测场景切换"""
    threshold = threshold or CONFIG["scene_threshold"]
    scdet_threshold = int(threshold * 100)

    cmd = ["ffmpeg", "-i", str(video_path),
           "-vf", f"scdet=threshold={scdet_threshold}",
           "-f", "null", "-"]
    result = run_cmd(cmd)

    # 解析 lavfi.scd.time 和 lavfi.scd.score
    times = []
    for line in result.stderr.split("\n"):
        match = re.search(r"lavfi\.scd\.time[:=]\s*(\S+)", line)
        if match:
            times.append(float(match.group(1)))

    if not times:
        # 没有检测到场景切换，整个视频作为一个场景
        duration = get_video_duration(video_path)
        scenes = [{"start": 0.0, "end": duration}]
        log(f"未检测到场景切换，整个视频作为一个场景 ({duration:.1f}s)")
    else:
        scenes = []
        prev = 0.0
        for t in times:
            scenes.append({"start": round(prev, 2), "end": round(t, 2)})
            prev = t
        # 最后一个场景到视频结束
        duration = get_video_duration(video_path)
        scenes.append({"start": round(prev, 2), "end": round(duration, 2)})

    # 保存
    scenes_file = work_dir / "scenes.json"
    scenes_file.write_text(json.dumps(scenes, ensure_ascii=False, indent=2))

    log(f"检测到 {len(scenes)} 个场景")

    # 合并短场景（< 3s 合并到相邻场景）
    scenes = _merge_short_scenes(scenes, min_duration=CONFIG.get("scene_merge_min", 4.0))

    scenes_file.write_text(json.dumps(scenes, ensure_ascii=False, indent=2))
    for i, s in enumerate(scenes):
        log(f"  场景 {i+1}: {s['start']:.1f}s - {s['end']:.1f}s ({s['end']-s['start']:.1f}s)")

    return scenes


def _merge_short_scenes(scenes, min_duration=4.0):
    """合并过短的场景到相邻场景"""
    if len(scenes) <= 1:
        return scenes
    merged = [scenes[0]]
    for s in scenes[1:]:
        prev = merged[-1]
        # 如果前一个场景太短，合并到当前
        if prev["end"] - prev["start"] < min_duration:
            merged[-1] = {"start": prev["start"], "end": s["end"]}
        # 如果当前场景太短，合并到前一个
        elif s["end"] - s["start"] < min_duration:
            merged[-1] = {"start": prev["start"], "end": s["end"]}
        else:
            merged.append(s)
    # 如果最后一个太短，已在前一步合并
    log(f"合并短场景后: {len(scenes)} → {len(merged)} 个场景")
    return merged


# ── Step 3: ASR 转录 ──────────────────────────────────────────────────

def transcribe_audio(video_path, work_dir):
    """提取音频并使用 qwen3-asr-rs 转录，通过分段合成时间戳"""
    # 提取音频
    audio_wav = work_dir / "audio.wav"
    cmd = ["ffmpeg", "-y", "-i", str(video_path), "-vn",
           "-ar", "16000", "-ac", "1", str(audio_wav)]
    result = run_cmd(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"音频提取失败: {result.stderr}")

    # 获取音频时长
    duration = get_video_duration(video_path)
    if duration == 0:
        duration = 180.0

    segments_dir = work_dir / "audio_segments"
    segments_dir.mkdir(exist_ok=True)

    if duration <= 180:
        # 短音频，整段转录
        text = _run_asr(audio_wav)
        asr_result = [{"start": 0.0, "end": round(duration, 2), "text": text}]
    else:
        # 长音频，分段转录
        asr_result = _segment_and_transcribe(audio_wav, segments_dir, duration)

    # 保存
    asr_file = work_dir / "asr_result.json"
    asr_file.write_text(json.dumps(asr_result, ensure_ascii=False, indent=2))

    total_text = " ".join(s["text"] for s in asr_result if s["text"])
    log(f"ASR 转录完成: {len(asr_result)} 段, 共 {len(total_text)} 字")
    return asr_result


def _run_asr(wav_path):
    """调用 qwen3-asr-rs 转录单个音频文件"""
    cmd = [CONFIG["asr_bin"], CONFIG["asr_model_dir"], str(wav_path)]
    result = run_cmd(cmd, timeout=600)
    if result.returncode != 0:
        log(f"ASR 警告: {result.stderr}")
        return ""

    # 解析输出 - 格式: Language : xxx\nText     : 实际文本
    text = ""
    for line in result.stdout.strip().split("\n"):
        m = re.match(r"Text\s*:\s*(.*)", line.strip())
        if m:
            text = m.group(1).strip()
            break

    return text


def _segment_and_transcribe(audio_wav, segments_dir, total_duration):
    """分段转录长音频"""
    segment_length = 180  # 3 分钟
    results = []

    for i, start in enumerate(range(0, int(total_duration), segment_length)):
        end = min(start + segment_length, total_duration)
        seg_wav = segments_dir / f"seg_{i:03d}.wav"

        cmd = ["ffmpeg", "-y", "-i", str(audio_wav),
               "-ss", str(start), "-to", str(end),
               "-ar", "16000", "-ac", "1", str(seg_wav)]
        run_cmd(cmd)

        text = _run_asr(seg_wav)
        results.append({
            "start": round(start, 2),
            "end": round(end, 2),
            "text": text,
        })
        log(f"  段 {i+1}: {start:.0f}s-{end:.0f}s => {len(text)} 字")

    return results


# ── Step 3.5: 静音检测 ─────────────────────────────────────────────

def detect_silence_periods(video_path, work_dir, asr_result=None):
    """用 ffmpeg silencedetect 检测安静时段，作为解说插入的候选窗口"""
    audio_path = work_dir / "audio.wav"
    if not audio_path.exists():
        run_cmd([
            "ffmpeg", "-y", "-i", str(video_path),
            "-vn", "-ar", "16000", "-ac", "1", str(audio_path)
        ])

    noise = CONFIG["silence_noise_threshold"]
    min_dur = CONFIG["silence_min_duration"]
    cmd = ["ffmpeg", "-i", str(audio_path),
           "-af", f"silencedetect=noise={noise}:d={min_dur}",
           "-f", "null", "-"]
    result = run_cmd(cmd, timeout=120)
    output = result.stderr

    # 解析 silence_start / silence_end
    starts = [float(m) for m in re.findall(r'silence_start:\s*([\d.]+)', output)]
    ends = [float(m) for m in re.findall(r'silence_end:\s*([\d.]+)', output)]

    # 配对所有静音段（不过滤时长，后面合并后再过滤）
    raw_periods = []
    for s, e in zip(starts, ends):
        raw_periods.append({"start": round(s, 2), "end": round(e, 2),
                            "duration": round(e - s, 2)})
    # 末尾静音
    if len(starts) > len(ends):
        dur_start = starts[len(ends)]
        total_dur = get_video_duration(str(audio_path))
        raw_periods.append({"start": round(dur_start, 2), "end": round(total_dur, 2),
                            "duration": round(total_dur - dur_start, 2)})

    # 合并相邻静音段（间隔 < merge_gap 的合并为一个大窗口）
    merge_gap = CONFIG.get("silence_merge_gap", 0.5)
    merged = []
    for rp in sorted(raw_periods, key=lambda x: x["start"]):
        if merged and rp["start"] - merged[-1]["end"] < merge_gap:
            merged[-1]["end"] = rp["end"]
            merged[-1]["duration"] = round(merged[-1]["end"] - merged[-1]["start"], 2)
        else:
            merged.append({"start": rp["start"], "end": rp["end"],
                           "duration": rp["duration"], "has_speech": False})

    # 过滤最短时长
    quiet_min = CONFIG["quiet_window_min"]
    periods = [p for p in merged if p["duration"] >= quiet_min]

    # 与 ASR 交叉验证：标记有语音的窗口
    # 跳过条件：ASR 段太粗（无法精确判断语音位置）
    # 1. 段数少(<=5)且覆盖>80%视频时长 → 时间戳不可靠
    # 2. 覆盖率>150% → 时间戳明显异常
    # 3. 平均段长>30s → 粒度太粗，无法判断哪些窗口有语音
    if asr_result:
        video_dur = get_video_duration(str(audio_path))
        asr_coverage = sum(seg.get("end", 0) - seg.get("start", 0) for seg in asr_result)
        avg_seg_dur = asr_coverage / len(asr_result) if asr_result else 0
        skip_cross_check = (
            (len(asr_result) <= 5 and asr_coverage > video_dur * 0.8) or
            asr_coverage > video_dur * 1.5 or
            avg_seg_dur > 30
        )
        if not skip_cross_check:
            for qp in periods:
                for seg in asr_result:
                    seg_s = seg.get("start", 0)
                    seg_e = seg.get("end", 0)
                    overlap = min(qp["end"], seg_e) - max(qp["start"], seg_s)
                    if overlap > qp["duration"] * 0.3:
                        qp["has_speech"] = True
                        break

    # 保存
    (work_dir / "silence_periods.json").write_text(
        json.dumps(periods, ensure_ascii=False, indent=2))
    log(f"检测到 {len(periods)} 个安静窗口 (≥{quiet_min}s)")
    for qp in periods:
        flag = " [有语音]" if qp["has_speech"] else ""
        log(f"  {qp['start']:.1f}s-{qp['end']:.1f}s ({qp['duration']:.1f}s){flag}")
    return periods


def identify_narration_zones(silence_periods, scenes_analysis, video_duration):
    """将相邻安静窗口合并为解说区，返回解说区列表。
    每个解说区: {start, end, duration, scenes: [...]}
    """
    merge_gap = CONFIG.get("zone_merge_gap", 3.0)
    min_dur = CONFIG.get("zone_min_duration", 6.0)

    # 只取安静窗口
    quiets = sorted(
        [qp for qp in silence_periods if not qp.get("has_speech", False)],
        key=lambda x: x["start"]
    )
    if not quiets:
        return []

    # 合并相邻窗口
    merged = [dict(quiets[0])]
    for qp in quiets[1:]:
        gap = qp["start"] - merged[-1]["end"]
        if gap <= merge_gap:
            merged[-1]["end"] = qp["end"]
            merged[-1]["duration"] = merged[-1]["end"] - merged[-1]["start"]
        else:
            merged.append(dict(qp))

    # 过滤短区 + 绑定场景
    zones = []
    for m in merged:
        if m["duration"] < min_dur:
            continue
        covered = [s for s in scenes_analysis
                   if s["start"] < m["end"] and s["end"] > m["start"]]
        zones.append({
            "start": m["start"],
            "end": m["end"],
            "duration": m["duration"],
            "scenes": covered,
        })

    log(f"识别到 {len(zones)} 个解说区:")
    for i, z in enumerate(zones):
        sc_ids = [str(s["scene_id"]+1) for s in z["scenes"]]
        log(f"  解说区{i+1}: {z['start']:.1f}s-{z['end']:.1f}s ({z['duration']:.1f}s, 场景{','.join(sc_ids)})")
    return zones


# ── Step 4: VLM 视觉分析 ─────────────────────────────────────────────

def _parse_vlm_depth_response(raw_text):
    """解析 VLM 深度分析响应，提取【描述】、【帧标签】和【深层分析】"""
    if not raw_text or not raw_text.strip():
        return "(VLM 无法识别此场景画面)", "", {}

    # 提取【描述】
    desc_match = re.search(r'【描述】\s*\n?(.*?)(?=【帧标签】|【深层分析】|$)', raw_text, re.DOTALL)
    if desc_match:
        description = desc_match.group(1).strip()
    else:
        description = raw_text.strip()

    # 提取【帧标签】
    frame_facts = {}
    facts_match = re.search(r'【帧标签】\s*\n?(.*?)(?=【深层分析】|$)', raw_text, re.DOTALL)
    if facts_match:
        for line in facts_match.group(1).strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            # 格式: "12.0s | 男子拿起茶壶对嘴喝, 满脸疲惫"
            m = re.match(r'([\d.]+)\s*s?\s*\|\s*(.+)', line)
            if m:
                ts = m.group(1)
                actions = [a.strip() for a in m.group(2).split(",") if a.strip()]
                if actions:
                    frame_facts[ts] = actions

    # 提取【深层分析】
    depth_match = re.search(r'【深层分析】\s*\n?(.*?)$', raw_text, re.DOTALL)
    depth_analysis = depth_match.group(1).strip() if depth_match else ""

    if not description:
        description = "(VLM 无法识别此场景画面)"

    return description, depth_analysis, frame_facts


def analyze_scenes(scenes, frames, work_dir):
    """对每个场景的关键帧调用 VLM 进行视觉分析（并行）"""

    vlm_prompt = load_prompt("VLM_DEPTH_PROMPT")
    if not vlm_prompt:
        vlm_prompt = "仔细观察这些视频帧。分两部分输出：\n【描述】不超过80字，描述画面中正在发生什么。\n【深层分析】不超过120字，分析角色情绪、关系动态、潜台词。"

    ctx = CONFIG.get("context_info", "")
    if ctx:
        vlm_prompt = f"已知信息：{ctx}\n\n{vlm_prompt}"

    # 构建帧时间映射 (frame_NNNNN.jpg -> time in seconds)
    fps = CONFIG["fps"]
    frame_times = {}
    for f in frames:
        parts = f.stem.split("_")
        if len(parts) != 2 or not parts[1].isdigit():
            continue
        num = int(parts[1])
        t = num / fps
        frame_times[f] = t

    # base64 编码缓存
    b64_cache = {}

    def _get_b64(frame_path):
        if frame_path not in b64_cache:
            b64_cache[frame_path] = base64.b64encode(frame_path.read_bytes()).decode()
        return b64_cache[frame_path]

    def _analyze_single_scene(i, scene):
        """分析单个场景，返回 (scene_id, result_dict)"""
        scene_frames = [f for f, t in frame_times.items()
                        if scene["start"] <= t <= scene["end"]]
        if not scene_frames:
            mid = (scene["start"] + scene["end"]) / 2
            scene_frames = [min(frames, key=lambda f: abs(frame_times.get(f, 999) - mid))]

        duration = scene["end"] - scene["start"]
        if duration > 8:
            max_frames = min(6, max(3, int(duration / 3)))
        else:
            max_frames = 3

        if len(scene_frames) > max_frames:
            step = len(scene_frames) / max_frames
            scene_frames = [scene_frames[int(j * step)] for j in range(max_frames)]
        else:
            scene_frames = scene_frames[:max_frames]

        content_parts = []
        for f in scene_frames:
            b64 = _get_b64(f)
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"}
            })

        # 帧级事实标签：将帧时间注入prompt
        frame_ts_list = [f"{frame_times[f]:.1f}s" for f in scene_frames]
        frame_ts_text = "帧时间点: " + ", ".join(frame_ts_list)
        content_parts.append({"type": "text", "text": frame_ts_text + "\n\n" + vlm_prompt})

        payload = {
            "model": CONFIG["vlm_model"],
            "messages": [{"role": "user", "content": content_parts}],
            "max_tokens": 800,
        }

        log(f"VLM 分析场景 {i+1}/{len(scenes)} ({len(scene_frames)} 帧)...")

        raw_response = ""
        for attempt in range(3):
            resp = api_call(payload)
            try:
                msg = resp["choices"][0]["message"]
                raw_response = msg.get("content", "") or msg.get("reasoning_content", "")
            except (KeyError, IndexError):
                log(f"VLM 返回异常: {json.dumps(resp, ensure_ascii=False)[:200]}")

            if raw_response.strip():
                break

            if attempt < 2:
                log(f"  场景 {i+1} VLM 返回空，重试 ({attempt+2}/3)...")
                retry_parts = content_parts[:-1]
                retry_parts.append({"type": "text", "text": vlm_prompt + "\n请务必按格式输出，不要留空。"})
                payload = {
                    "model": CONFIG["vlm_model"],
                    "messages": [{"role": "user", "content": retry_parts}],
                    "max_tokens": 800,
                }

        # 解析 【描述】、【帧标签】和【深层分析】
        description, depth_analysis, frame_facts = _parse_vlm_depth_response(raw_response)

        result = {
            "scene_id": i,
            "start": scene["start"],
            "end": scene["end"],
            "description": description,
            "depth_analysis": depth_analysis,
        }
        if frame_facts:
            result["frame_facts"] = frame_facts

        return i, result

    # 并行调用 VLM
    analyses = [None] * len(scenes)
    max_workers = min(len(scenes), CONFIG.get("vlm_workers", 4))
    log(f"VLM 并行分析 {len(scenes)} 个场景 (workers={max_workers})...")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_analyze_single_scene, i, s): i for i, s in enumerate(scenes)}
        for future in as_completed(futures):
            try:
                idx, result = future.result()
                analyses[idx] = result
            except Exception as e:
                i = futures[future]
                log(f"VLM 场景 {i+1} 分析失败: {e}")
                analyses[i] = {
                    "scene_id": i, "start": scenes[i]["start"], "end": scenes[i]["end"],
                    "description": f"(VLM 分析失败: {e})", "depth_analysis": "", "frame_facts": {},
                }

    # 保存
    vlm_file = work_dir / "vlm_analysis.json"
    vlm_file.write_text(json.dumps(analyses, ensure_ascii=False, indent=2))

    log(f"VLM 分析完成: {len(analyses)} 个场景")
    return analyses


def _distribute_asr_by_scenes(asr_result, scenes_analysis):
    """将 ASR 文本按语义分配到场景（LLM 优先，按时长比例 fallback）"""
    if not asr_result:
        return ""

    full_text = " ".join(seg["text"] for seg in asr_result if seg["text"])
    if not full_text:
        return ""

    # 尝试 LLM 语义对齐
    align_prompt = load_prompt("ASR_ALIGN_PROMPT")
    if align_prompt and scenes_analysis:
        scenes_desc = ""
        for s in scenes_analysis:
            scenes_desc += f"- scene_id={s['scene_id']} ({s['start']:.1f}s-{s['end']:.1f}s): {s['description']}\n"

        payload = {
            "model": CONFIG["vlm_model"],
            "messages": [
                {"role": "user", "content": f"{align_prompt}\n\n场景列表：\n{scenes_desc.strip()}\n\nASR 转录文本：\n{full_text}"},
            ],
            "max_tokens": 1000,
        }

        try:
            resp = api_call(payload)
            msg = resp["choices"][0]["message"]
            result_text = msg.get("content", "") or msg.get("reasoning_content", "")
            alignments = _parse_narration_json(result_text)
            if isinstance(alignments, list) and alignments:
                lines = []
                for a in alignments:
                    sid = a.get("scene_id", 0)
                    text = a.get("text", "")
                    if text and 0 <= sid < len(scenes_analysis):
                        s = scenes_analysis[sid]
                        lines.append(f"[{s['start']:.0f}s-{s['end']:.0f}s] {text}")
                if lines:
                    log("ASR 语义对齐成功")
                    return "\n".join(lines)
        except Exception as e:
            log(f"ASR 语义对齐失败，使用时长比例分配: {e}")

    # Fallback: 按时长比例分配
    return _distribute_asr_by_time(asr_result, scenes_analysis)


def _distribute_asr_by_time(asr_result, scenes_analysis):
    """按时长比例分配 ASR 文本到场景（fallback）"""
    full_text = " ".join(seg["text"] for seg in asr_result if seg["text"])
    if not full_text:
        return ""

    total_duration = sum(s["end"] - s["start"] for s in scenes_analysis)
    if total_duration == 0:
        return full_text

    words = full_text.split()
    total_words = len(words)
    if total_words == 0:
        return ""

    lines = []
    word_offset = 0
    for s in scenes_analysis:
        duration = s["end"] - s["start"]
        n_words = max(1, round(total_words * duration / total_duration))
        end_offset = min(word_offset + n_words, total_words)
        segment_text = " ".join(words[word_offset:end_offset])
        if segment_text:
            lines.append(f"[{s['start']:.0f}s-{s['end']:.0f}s] {segment_text}")
        word_offset = end_offset

    if word_offset < total_words:
        remaining = " ".join(words[word_offset:])
        if lines:
            lines[-1] += " " + remaining
        else:
            lines.append(remaining)

    return "\n".join(lines)


def _annotate_asr_temporal(asr_result, scenes_analysis):
    """利用帧动作描述中的对白/字幕信息为 ASR 大块文本添加模糊时间标注"""

    # 从帧动作描述中收集带时间的锚点文本
    anchors = []  # [(timestamp, snippet_text)]
    for scene in scenes_analysis:
        facts = scene.get("frame_facts", {})
        for ts, actions in facts.items():
            t = float(ts)
            for action in actions:
                # 提取引号内的文本（对话/字幕）
                quoted = re.findall(r'[""“”\'\'「」](.+?)[""“”\'\'「」]', action)
                for q in quoted:
                    if len(q) >= 2:
                        anchors.append((t, q))
                # 提取"字幕:"后的文本
                sub_match = re.search(r'字幕[：:]\s*(.+)', action)
                if sub_match:
                    sub_text = sub_match.group(1).strip()
                    if len(sub_text) >= 2:
                        anchors.append((t, sub_text))

    if not anchors:
        log("ASR 时间标注: 无帧级字幕/对白锚点，回退到原有分配")
        return _distribute_asr_by_scenes(asr_result, scenes_analysis)

    # 按时间排序锚点
    anchors.sort(key=lambda x: x[0])

    # 对每个 ASR 块，尝试用锚点分割
    annotated_lines = []
    for seg in asr_result:
        text = seg.get("text", "").strip()
        if not text:
            continue
        seg_start = seg.get("start", 0)
        seg_end = seg.get("end", 0)

        # 找属于该时间段的锚点
        seg_anchors = [(t, s) for t, s in anchors if seg_start <= t <= seg_end]

        if not seg_anchors:
            # 无锚点，整体标注
            annotated_lines.append(f"[~{seg_start:.0f}s-{seg_end:.0f}s] {text}")
            continue

        # 尝试按锚点分割文本
        chunks = []  # [(approx_start, approx_end, text_chunk)]
        remaining = text
        prev_time = seg_start

        for anchor_time, anchor_text in seg_anchors:
            # 在剩余文本中查找锚点文本
            # 模糊匹配：取锚点文本的前3-4个字
            key = anchor_text[:min(4, len(anchor_text))]
            idx = remaining.find(key)
            if idx >= 0:
                # 找到锚点，分割
                before = remaining[:idx].strip()
                if before:
                    chunks.append((prev_time, anchor_time, before))
                remaining = remaining[idx:].strip()
                prev_time = anchor_time

        # 最后的剩余文本
        if remaining:
            chunks.append((prev_time, seg_end, remaining))

        if chunks:
            for cs, ce, ct in chunks:
                annotated_lines.append(f"[~{cs:.0f}s-{ce:.0f}s] {ct}")
        else:
            annotated_lines.append(f"[~{seg_start:.0f}s-{seg_end:.0f}s] {text}")

    result = "\n".join(annotated_lines)
    log(f"ASR 时间标注完成: {len(annotated_lines)} 个子段, {len(anchors)} 个锚点")
    return result


# ── Step 4.5: 叙事结构分析 ───────────────────────────────────────────

def analyze_narrative_structure(scenes_analysis, work_dir):
    """分析视频的叙事结构，为每个场景标注 narrative_role 和 info_weight"""
    analysis_prompt = load_prompt("NARRATIVE_ANALYSIS_PROMPT")
    if not analysis_prompt:
        return scenes_analysis

    scenes_text = ""
    for s in scenes_analysis:
        scenes_text += f"- scene_id={s['scene_id']} ({s['start']:.1f}s-{s['end']:.1f}s): {s['description']}\n"

    payload = {
        "model": CONFIG.get("llm_model", CONFIG["vlm_model"]),
        "messages": [
            {"role": "user", "content": f"{analysis_prompt}\n\n场景列表：\n{scenes_text.strip()}"},
        ],
        "max_tokens": 1000,
    }

    log("分析叙事结构 (Step 4.5)...")
    resp = api_call(payload)

    try:
        msg = resp["choices"][0]["message"]
        result_text = msg.get("content", "") or msg.get("reasoning_content", "")
    except (KeyError, IndexError):
        log("叙事结构分析失败，使用默认值")
        return scenes_analysis

    # 解析 JSON
    roles = []
    try:
        roles = json.loads(result_text)
    except json.JSONDecodeError:
        for pattern in [r"```json\s*(.*?)\s*```", r"(\[.*\])"]:
            m = re.search(pattern, result_text, re.DOTALL)
            if m:
                try:
                    roles = json.loads(m.group(1))
                    break
                except json.JSONDecodeError:
                    continue

    if not isinstance(roles, list):
        log("叙事结构解析失败，使用默认值")
        return scenes_analysis

    # 将 narrative_role 和 info_weight 合并到 scenes_analysis
    role_map = {r.get("scene_id"): r for r in roles if isinstance(r, dict)}
    for s in scenes_analysis:
        sid = s["scene_id"]
        if sid in role_map:
            s["narrative_role"] = role_map[sid].get("narrative_role", "setup")
            s["info_weight"] = role_map[sid].get("info_weight", 3)
        else:
            s["narrative_role"] = "setup"
            s["info_weight"] = 3

    # 保存
    narr_file = work_dir / "narrative_structure.json"
    narr_file.write_text(json.dumps(scenes_analysis, ensure_ascii=False, indent=2))

    log(f"叙事结构分析完成: {len(roles)}/{len(scenes_analysis)} 场景已标注")
    return scenes_analysis


# ── Step 5 (zone mode): 解说区模式 ────────────────────────────────────

def generate_narration_zones(zones, asr_result, work_dir, style="纪录片"):
    """按解说区生成大段解说（zone 模式）。"""
    system_prompt = load_prompt("NARRATION_SYSTEM_PROMPT")
    if not system_prompt:
        system_prompt = "你是专业的视频解说文案撰写师。"

    style_prompt = load_prompt(f"NARRATION_STYLE_{style}")
    if style_prompt:
        system_prompt = system_prompt + "\n\n" + style_prompt

    ctx = CONFIG.get("context_info", "")
    if ctx:
        system_prompt = system_prompt + f"\n\n已知背景：{ctx}"

    # 加载背景调研（browser-cdp 或 agent 手动写入）
    research_path = Path(work_dir) / "background_research.json"
    if research_path.exists():
        try:
            research = json.loads(research_path.read_text("utf-8"))
            parts = []
            if research.get("synopsis"):
                parts.append(f"剧情梗概: {research['synopsis']}")
            if research.get("characters"):
                chars = "\n".join(f"  - {k}: {v}" for k, v in research["characters"].items())
                parts.append(f"角色信息:\n{chars}")
            if research.get("worldbuilding"):
                parts.append(f"世界观设定: {research['worldbuilding']}")
            if research.get("episode_context"):
                parts.append(f"当前集上下文: {research['episode_context']}")
            if parts:
                system_prompt += "\n\n【背景知识（从网络调研获得）】\n" + "\n".join(parts)
                log(f"  注入背景调研: {len(parts)} 个维度")
        except (json.JSONDecodeError, KeyError):
            pass

    # 组装解说区描述
    effective_rate = CONFIG["speech_rate"] * CONFIG["speech_safety_margin"]
    breath_sec = CONFIG.get("breath_ms", 600) / 1000

    zones_text = ""
    for i, z in enumerate(zones):
        dur = z["duration"]
        max_chars = max(10, int((dur - breath_sec) * effective_rate))
        scenes_desc = ""
        for s in z["scenes"]:
            depth = s.get("depth_analysis", "")
            depth_text = f"\n    深层分析: {depth}" if depth else ""
            facts_text = _format_frame_facts(s)
            scenes_desc += f"    场景{s['scene_id']+1} ({s['start']:.1f}s-{s['end']:.1f}s): {s['description']}{depth_text}{facts_text}\n"
        zones_text += (
            f"【解说区{i+1}】({z['start']:.1f}s-{z['end']:.1f}s, "
            f"时长{dur:.1f}s, 最多{max_chars}字)\n"
            f"  覆盖场景:\n{scenes_desc}\n"
        )

    # ASR 文本
    asr_text = ""
    if asr_result:
        for seg in asr_result:
            asr_text += seg.get("text", "") + " "
    asr_text = asr_text.strip() or "(无原始语音)"

    user_template = load_prompt("NARRATION_ZONE_USER_PROMPT")
    if not user_template:
        user_template = (
            "解说区:\n{zones_text}\n\n"
            "原始对白: {asr_result}\n\n"
            "风格: {style}\n\n"
            "请为每个解说区生成一段解说，JSON 数组输出。"
        )

    user_content = user_template.format(
        zones_text=zones_text.strip(),
        asr_result=asr_text,
        style=style,
    )

    payload = {
        "model": CONFIG.get("llm_model", CONFIG["vlm_model"]),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": 4000,
    }

    log(f"按解说区模式生成 (风格: {style}, {len(zones)} 个区)...")

    narration_text = ""
    for attempt in range(3):
        resp = api_call(payload)
        try:
            msg = resp["choices"][0]["message"]
            narration_text = msg.get("content", "") or ""
            if not narration_text:
                narration_text = msg.get("reasoning_content", "") or ""
            if narration_text:
                break
        except (KeyError, IndexError):
            pass
        if attempt < 2:
            log(f"  LLM 返回空，重试 ({attempt+2}/3)...")

    narration = _parse_narration_json(narration_text)
    if not narration:
        log("  LLM 未返回有效 JSON，逐区 fallback")
        narration = _fallback_zone_narration(zones)

    # 过滤空段
    narration = [n for n in narration if n.get("narration", "").strip()]

    # 对齐每个解说段的 start 到对应区的开头
    for n in narration:
        n["overlaps_speech"] = False
        # 找最近的区
        for z in zones:
            if z["start"] <= n.get("start", 0) <= z["end"]:
                n["start"] = z["start"]
                if n.get("end", 0) > z["end"]:
                    n["end"] = z["end"]
                break

    log(f"解说区模式生成完成: {len(narration)} 段")
    for n in narration:
        log(f"  {n['start']:.1f}s-{n['end']:.1f}s: {n['narration'][:30]}...")

    return narration


def _fallback_zone_narration(zones):
    """逐区简单生成 fallback。"""
    results = []
    for z in zones:
        # 从场景描述中提取关键信息
        scenes_desc = "，".join(s.get("description", "") for s in z["scenes"])
        prompt = (
            f"根据以下场景描述，生成一段不超过{int(z['duration'] * 2.5)}字的中文解说（讲故事风格，不要描述画面）：\n"
            f"{scenes_desc}\n"
            f"已知背景：{CONFIG.get('context_info', '无')}\n"
            f"只输出解说文本，不要其他内容。"
        )
        payload = {
            "model": CONFIG.get("llm_model", CONFIG["vlm_model"]),
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 500,
        }
        resp = api_call(payload)
        text = resp.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        if text:
            results.append({
                "start": z["start"],
                "end": z["end"],
                "narration": text,
                "pause_after_ms": 600,
                "overlaps_speech": False,
            })
    return results


def _format_frame_facts(scene):
    """将帧动作描述格式化为可注入 scenes_text/zones_text 的文本"""
    facts = scene.get("frame_facts", {})
    if not facts:
        return ""
    lines = []
    for ts in sorted(facts.keys(), key=lambda x: float(x)):
        actions = facts[ts]
        lines.append(f"    {ts}s: {'; '.join(actions)}")
    return "\n  帧动作:\n" + "\n".join(lines)


# ── Step 5: 解说脚本生成 ─────────────────────────────────────────────

def generate_narration(scenes_analysis, asr_result, work_dir, style="纪录片",
                       silence_periods=None):
    """使用 LLM 生成解说脚本"""
    system_prompt = load_prompt("NARRATION_SYSTEM_PROMPT")
    if not system_prompt:
        system_prompt = "你是专业的视频解说文案撰写师。根据视频的场景分析和原始音频转录，生成流畅自然的中文解说词。每段 15-40 字，适合口语播报。"

    # 加载风格化追加 prompt
    style_prompt = load_prompt(f"NARRATION_STYLE_{style}")
    if style_prompt:
        system_prompt = system_prompt + "\n\n" + style_prompt

    ctx = CONFIG.get("context_info", "")
    if ctx:
        system_prompt = system_prompt + f"\n\n已知背景：{ctx}"

    user_prompt_template = load_prompt("NARRATION_USER_PROMPT")
    if not user_prompt_template:
        user_prompt_template = """场景分析：
{scenes_analysis}

原始音频转录：
{asr_result}

解说风格：{style}

请生成视频解说脚本，严格按 JSON 数组格式输出:
[{{"start": 开始秒数, "end": 结束秒数, "narration": "解说文本"}}]

每段 narration 控制在 15-40 个中文字。"""

    # 组装场景描述（含精确字数预算 + 段数建议 + 叙事角色）
    # 预算基于实际可用时长（减去呼吸+fade），用保守安全系数确保 TTS 不超
    effective_rate = CONFIG["speech_rate"] * CONFIG["speech_safety_margin"]
    breath_sec = CONFIG.get("breath_ms", 600) / 1000
    scenes_text = ""
    for s in scenes_analysis:
        duration = s["end"] - s["start"]
        # 实际可用时长 = 场景时长 - 呼吸空间（fade 只是振幅包络，不消耗时间）
        available = max(1.0, duration - breath_sec)
        # info_weight 影响预算分配
        info_weight = s.get("info_weight", 3)
        weight_multiplier = 0.7 if info_weight <= 2 else (1.5 if info_weight >= 4 else 1.0)
        max_chars = max(5, int(available * effective_rate * weight_multiplier))
        n_seg_hint = max(1, round(duration / 5))
        per_seg_max = max(5, int(max_chars / n_seg_hint))
        role = s.get("narrative_role", "")
        role_tag = f" [{role}]" if role else ""
        # 深层分析（如果有）
        depth = s.get("depth_analysis", "")
        depth_text = f"\n  深层分析: {depth}" if depth else ""

        # 安静窗口标注（如果有）
        quiet_text = ""
        if silence_periods:
            scene_quiets = [qp for qp in silence_periods
                           if not qp.get("has_speech", False)
                           and qp["start"] < s["end"] and qp["end"] > s["start"]]
            if scene_quiets:
                windows = ", ".join(f"{max(qp['start'],s['start']):.1f}s-{min(qp['end'],s['end']):.1f}s"
                                   for qp in scene_quiets)
                quiet_text = f"\n  安静时段(适合放解说): [{windows}]"
            else:
                mid = (s["start"] + s["end"]) / 2
                quiet_text = f"\n  (无明显安静窗口，放在{mid:.1f}s附近即可，后续会自动对齐)"

        if n_seg_hint > 1:
            scenes_text += f"- 场景{s['scene_id']+1}{role_tag} ({s['start']:.1f}s-{s['end']:.1f}s, 总预算{max_chars}字, 每段≤{per_seg_max}字, 建议{n_seg_hint}段): {s['description']}{depth_text}{_format_frame_facts(s)}{quiet_text}\n"
        else:
            scenes_text += f"- 场景{s['scene_id']+1}{role_tag} ({s['start']:.1f}s-{s['end']:.1f}s, 总预算{max_chars}字, 每段≤{per_seg_max}字): {s['description']}{depth_text}{_format_frame_facts(s)}{quiet_text}\n"

    # 组装 ASR 文本（按场景时间段分配）
    asr_text = _annotate_asr_temporal(asr_result, scenes_analysis)

    user_content = user_prompt_template.format(
        scenes_analysis=scenes_text.strip(),
        asr_result=asr_text.strip() or "(无原始语音)",
        style=style,
    )

    payload = {
        "model": CONFIG.get("llm_model", CONFIG["vlm_model"]),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": 4000,
    }

    log(f"生成解说脚本 (风格: {style})...")

    # 推理模型可能将思考放在 reasoning_content，实际输出在 content
    # 如果 content 为空则重试（最多 3 次）
    narration_text = ""
    for attempt in range(3):
        resp = api_call(payload)
        try:
            msg = resp["choices"][0]["message"]
            narration_text = msg.get("content", "") or ""
            if not narration_text:
                reasoning = msg.get("reasoning_content", "") or ""
                # 尝试从 reasoning 中提取 JSON
                narration_text = _extract_json_from_text(reasoning)
                if narration_text:
                    log(f"  从推理输出中提取 JSON (attempt {attempt+1})")
                    break
                log(f"  content 为空，重试 ({attempt+2}/3)...")
                continue
            break
        except (KeyError, IndexError):
            raise RuntimeError(f"LLM 返回异常: {json.dumps(resp, ensure_ascii=False)[:300]}")

    if not narration_text:
        raise RuntimeError("LLM 多次返回空 content，请检查模型或 API 配置")

    # 解析 JSON
    narration = _parse_narration_json(narration_text)

    # 后验证：按场景检查总字数，超预算按比例缩减
    narration = _validate_narration_budget(narration, scenes_analysis)

    # Debug: 保存初始解说用于分析覆盖率
    initial_covered = set()
    for n in narration:
        n_mid = (n["start"] + n["end"]) / 2
        for s in scenes_analysis:
            if s["start"] <= n_mid <= s["end"]:
                initial_covered.add(s["scene_id"])
                break
    initial_pct = len(initial_covered) / len(scenes_analysis) * 100 if scenes_analysis else 100
    log(f"初始覆盖率: {initial_pct:.0f}% ({len(initial_covered)}/{len(scenes_analysis)})")
    (work_dir / "narration_initial.json").write_text(
        json.dumps(narration, ensure_ascii=False, indent=2))

    # 覆盖率检查：逐场景补充，3 轮递进 (90% → 95% → 100%)
    # 构建 per-scene ASR 对白映射（fill 和 temporal fill 共用）
    scene_asr = {}
    for line in asr_text.strip().split("\n"):
        m = re.match(r'\[(\d+)s-(\d+)s\]\s*(.*)', line)
        if m:
            a_start, a_end = float(m.group(1)), float(m.group(2))
            dialog = m.group(3).strip()
            if not dialog:
                continue
            for s in scenes_analysis:
                if s["start"] < a_end and s["end"] > a_start:
                    scene_asr.setdefault(s["scene_id"], []).append(dialog)

    fill_thresholds = [0.40, 0.60]
    for fill_round, threshold in enumerate(fill_thresholds):
        covered_scenes = set()
        for n in narration:
            if not n.get("narration", "").strip():
                continue
            n_mid = (n["start"] + n["end"]) / 2
            for s in scenes_analysis:
                if s["start"] <= n_mid <= s["end"]:
                    covered_scenes.add(s["scene_id"])
                    break
        coverage = len(covered_scenes) / len(scenes_analysis) if scenes_analysis else 1.0
        if coverage >= threshold:
            log(f"覆盖率 {coverage:.0%} 已达标 ({len(covered_scenes)}/{len(scenes_analysis)})")
            continue
        uncovered = [s for s in scenes_analysis if s["scene_id"] not in covered_scenes and (s["end"] - s["start"]) >= 5.0]
        if not uncovered:
            break
        log(f"覆盖率 {coverage:.0%} 不足 (轮次{fill_round+1}, 阈值{threshold:.0%})，逐场景补充 {len(uncovered)} 个未覆盖场景")
        narration.sort(key=lambda x: x["start"])
        existing_ctx = "\n".join(
            f"  [{n['start']:.1f}-{n['end']:.1f}] {n['narration']}"
            for n in narration
        )
        fill_workers = min(len(uncovered), CONFIG.get("fill_workers", 4))
        with ThreadPoolExecutor(max_workers=fill_workers) as executor:
            futures = {executor.submit(
                _generate_single_fill, scene, silence_periods, existing_ctx,
                "; ".join(scene_asr.get(scene["scene_id"], []))
            ): scene for scene in uncovered}
            for future in as_completed(futures):
                try:
                    seg = future.result()
                except Exception as e:
                    scene = futures[future]
                    log(f"  fill 场景{scene['scene_id']+1} 失败: {e}")
                    log(f"  详细错误: {traceback.format_exc()}")
                    continue
                if seg:
                    narration.append(seg)
        narration.sort(key=lambda x: x["start"])
        narration = _validate_narration_budget(narration, scenes_analysis)

    # Temporal gap fill: 长场景中空白时段追加解说（并行）
    temporal_gap_min = CONFIG.get("temporal_gap_min", 8.0)
    temporal_tasks = []  # (scene, gap_start, gap_end, ctx, dialogue)
    for scene in scenes_analysis:
        scene_dur = scene["end"] - scene["start"]
        if scene_dur < 6.0:
            continue
        scene_segs = sorted(
            [n for n in narration
             if scene["start"] <= (n["start"] + n["end"]) / 2 <= scene["end"]
             and n.get("narration", "").strip()],
            key=lambda x: x["start"]
        )
        if not scene_segs:
            continue
        covered = sum(
            min(n["end"], scene["end"]) - max(n["start"], scene["start"])
            for n in scene_segs
        )
        if covered / scene_dur >= 0.5:
            continue
        gaps = []
        if scene_segs[0]["start"] - scene["start"] > temporal_gap_min:
            gaps.append((scene["start"], scene_segs[0]["start"]))
        for i in range(1, len(scene_segs)):
            gs, ge = scene_segs[i - 1]["end"], scene_segs[i]["start"]
            if ge - gs > temporal_gap_min:
                gaps.append((gs, ge))
        if scene["end"] - scene_segs[-1]["end"] > temporal_gap_min:
            gaps.append((scene_segs[-1]["end"], scene["end"]))
        if not gaps:
            continue
        existing_ctx = "\n".join(
            f"  [{n['start']:.1f}-{n['end']:.1f}] {n['narration']}"
            for n in narration
        )
        dialogue = "; ".join(scene_asr.get(scene["scene_id"], []))
        for gap_start, gap_end in gaps:
            temporal_tasks.append((scene, gap_start, gap_end, existing_ctx, dialogue))

    temporal_filled = 0
    if temporal_tasks:
        fill_workers = min(len(temporal_tasks), CONFIG.get("fill_workers", 4))
        with ThreadPoolExecutor(max_workers=fill_workers) as executor:
            futures = {
                executor.submit(_generate_temporal_fill, scene, gs, ge, ctx, dlg): (scene, gs, ge)
                for scene, gs, ge, ctx, dlg in temporal_tasks
            }
            for future in as_completed(futures):
                try:
                    seg = future.result()
                except Exception as e:
                    log(f"  temporal fill 失败: {e}")
                    log(f"  详细错误: {traceback.format_exc()}")
                    continue
                if seg:
                    narration.append(seg)
                    temporal_filled += 1
    if temporal_filled:
        narration.sort(key=lambda x: x["start"])
        narration = _validate_narration_budget(narration, scenes_analysis)
        log(f"时间覆盖率补充: +{temporal_filled} 段")

    # Fallback: API 反复失败的场景用 depth_analysis 生成简短解说
    covered_scenes = set()
    for n in narration:
        n_mid = (n.get("start", 0) + n.get("end", 0)) / 2
        for s in scenes_analysis:
            if s["start"] <= n_mid <= s["end"]:
                covered_scenes.add(s["scene_id"])
                break
    still_uncovered = [s for s in scenes_analysis if s["scene_id"] not in covered_scenes]
    # Fallback: 跳过未覆盖场景（避免低质量硬拼）
    # 注意：不再从 depth_analysis 截取中文字符拼凑解说，
    # 覆盖率 60% 优于垃圾内容 100%
    uncovered_count = 0
    too_short_count = 0
    for scene in still_uncovered:
        dur = scene["end"] - scene["start"]
        if dur < 5.0:
            too_short_count += 1
            log(f"  未覆盖场景{scene['scene_id']+1} ({dur:.1f}s): 过短，跳过")
        else:
            uncovered_count += 1
            log(f"  未覆盖场景{scene['scene_id']+1} ({dur:.1f}s): 跳过（无可用解说源）")
    if uncovered_count:
        log(f"{uncovered_count} 个场景无解说覆盖，建议手动补充或增大 fill_thresholds")
    if too_short_count:
        log(f"{too_short_count} 个场景过短（<5s）未覆盖")

    # 过滤空段
    narration = [n for n in narration if n.get("narration", "").strip()]

    # 保存
    narration_file = work_dir / "narration.json"
    narration_file.write_text(json.dumps(narration, ensure_ascii=False, indent=2))

    log(f"解说脚本生成完成: {len(narration)} 段")
    for n in narration:
        log(f"  {n['start']:.1f}s-{n['end']:.1f}s: {n['narration'][:30]}...")

    return narration


def _fill_api_call(prompt, max_chars):
    """公共 fill API 调用：发送 prompt，解析并验证返回的解说 JSON。返回 dict 或 None"""
    system_prompt = (
        "你是视频解说精炼师。只输出 JSON，不要多余文字。\n"
        "核心规则：口语化自然表达，揭示角色真实意图和潜台词，"
        "禁止'象征''隐喻''展现了'等空洞/文学词汇，"
        "用口语化方式揭示角色潜台词，每个场景用不同的表达方式。"
        "注意画面字幕与内容的反差（如结尾字幕揭示结局与画面形成讽刺对比），优先捕捉。"
    )
    ctx = CONFIG.get("context_info", "")
    if ctx:
        system_prompt += f"\n已知背景：{ctx}"
    for _attempt in range(2):
        try:
            resp = api_call({
                "model": CONFIG["llm_model"],
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 200,
            })
            msg = resp["choices"][0]["message"]
            text = msg.get("content", "") or ""
            if not text:
                text = msg.get("reasoning_content", "") or ""
                text = _extract_json_from_text(text) or text
            if not text or text.strip() in ("0", "1", ""):
                if _attempt == 0:
                    time.sleep(1)
                    continue
                return None
            segments = _parse_narration_json(text)
            if isinstance(segments, dict):
                segments = [segments]
            if not segments or not isinstance(segments, list):
                # Fallback: 尝试从纯文本提取解说
                clean = text.strip().strip('`').strip('"').strip("'")
                if 5 <= len(clean) <= max_chars * 1.5:
                    return {"narration": clean[:max_chars], "start": 0, "end": 0,
                            "pause_after_ms": 600}
                if _attempt == 0:
                    time.sleep(1)
                    continue
                return None
            seg = segments[0]
            narr_text = seg.get("narration", "")
            if len(narr_text) > max_chars:
                truncated = _truncate_at_sentence(narr_text, max_chars)
                if truncated and len(truncated) >= 5:
                    seg["narration"] = truncated
                else:
                    if _attempt == 0:
                        time.sleep(1)
                        continue
                    return None
            if not seg.get("narration", "").strip():
                if _attempt == 0:
                    time.sleep(1)
                    continue
                return None
            return seg
        except Exception as e:
            if _attempt == 0:
                time.sleep(2)
                continue
    return None


def _generate_single_fill(scene, silence_periods=None, existing_narration="", scene_dialogue=""):
    """为单个未覆盖场景生成一句简短解说，返回 dict 或 None"""
    duration = scene["end"] - scene["start"]
    if duration < 5.0:
        return None

    effective_rate = CONFIG["speech_rate"] * CONFIG["speech_safety_margin"]
    breath_sec = CONFIG.get("breath_ms", 600) / 1000
    available = max(1.0, duration - breath_sec)
    max_chars = max(5, int(available * effective_rate))
    target_chars = max(5, int(max_chars * 0.85))

    # 安静窗口
    quiet_info = ""
    best_start = scene["start"]
    if silence_periods:
        quiets = [qp for qp in silence_periods
                  if not qp.get("has_speech", False)
                  and qp["start"] < scene["end"] and qp["end"] > scene["start"]]
        if quiets:
            windows = [f"{max(qp['start'], scene['start']):.1f}-{min(qp['end'], scene['end']):.1f}"
                       for qp in quiets]
            quiet_info = f" 安静窗口:{','.join(windows)}"
            longest = max(quiets, key=lambda q: q["duration"])
            best_start = max(longest["start"], scene["start"])

    depth = scene.get("depth_analysis", "")
    depth_text = f" ({depth})" if depth else ""

    ctx_block = ""
    if existing_narration:
        ctx_block = f"\n已有解说（保持叙事连贯，承接上下文）:\n{existing_narration}\n"

    dialog_block = ""
    if scene_dialogue:
        dialog_block = f"\n原始对白(英文，翻译融入解说): {scene_dialogue}\n"

    prompt = (
        f"为这个视频场景写一句中文解说。\n"
        f"场景: {scene['start']:.1f}s-{scene['end']:.1f}s "
        f"({target_chars}-{max_chars}字){quiet_info}\n"
        f"画面: {scene['description']}{depth_text}"
        f"{ctx_block}{dialog_block}\n"
        f"要求：{target_chars}-{max_chars}字，揭示角色意图或潜台词，与前后解说自然衔接，以句号/感叹号结尾。避免重复用词和相同句式，用不同的表达方式。\n"
        f"严格输出 JSON: {{\"start\": {best_start:.1f}, \"end\": {scene['end']:.1f}, "
        f"\"narration\": \"...\", \"pause_after_ms\": 600}}"
    )

    seg = _fill_api_call(prompt, max_chars)
    if seg:
        seg["start"] = scene["start"]
        seg["end"] = scene["end"]
        log(f"  补充场景{scene['scene_id']+1}: \"{seg['narration']}\"")
    return seg



def _generate_temporal_fill(scene, gap_start, gap_end, existing_narration="", scene_dialogue=""):
    """为场景内的空白时段生成追加解说"""
    gap_dur = gap_end - gap_start
    if gap_dur < 3.0:
        return None
    effective_rate = CONFIG["speech_rate"] * CONFIG["speech_safety_margin"]
    breath_sec = CONFIG.get("breath_ms", 600) / 1000
    max_chars = max(5, int((gap_dur - breath_sec) * effective_rate))
    target_chars = max(5, int(max_chars * 0.85))
    depth = scene.get("depth_analysis", "")
    depth_text = f" ({depth})" if depth else ""
    ctx_block = ""
    if existing_narration:
        ctx_block = f"\n已有解说（保持叙事连贯，承接上下文）:\n{existing_narration}\n"
    dialog_block = ""
    if scene_dialogue:
        dialog_block = f"\n原始对白(英文，翻译融入解说): {scene_dialogue}\n"
    prompt = (
        f"为这个视频场景的空白时段追加一句中文解说。\n"
        f"场景: {scene['start']:.1f}s-{scene['end']:.1f}s\n"
        f"空白时段: {gap_start:.1f}s-{gap_end:.1f}s ({target_chars}-{max_chars}字)\n"
        f"画面: {scene['description']}{depth_text}"
        f"{ctx_block}{dialog_block}\n"
        f"要求：{target_chars}-{max_chars}字，揭示角色意图或潜台词，与前后解说自然衔接，以句号/感叹号结尾。避免重复用词和相同句式，用不同的表达方式。\n"
        f"严格输出 JSON: {{\"start\": {gap_start:.1f}, \"end\": {gap_end:.1f}, "
        f"\"narration\": \"...\", \"pause_after_ms\": 600}}"
    )
    seg = _fill_api_call(prompt, max_chars)
    if seg:
        seg["start"] = gap_start
        seg["end"] = gap_end
        log(f"  时间填充场景{scene['scene_id']+1} [{gap_start:.1f}-{gap_end:.1f}]: \"{seg['narration']}\"")
    return seg


def _text_char_count(text):
    """计算文本的有效字数（去除标点和空白，这些不占 TTS 朗读时间）"""
    return len(re.sub(r'[，。！？、；：…“”‘’《》〈〉\s"\'「」『』（）()【】\[\]—～·,.!?;:\\-]', '', text))


def _truncate_at_sentence(text, max_chars):
    """在句子边界截断，不产生残句。max_chars 按有效字符计（不含标点空白）"""
    if _text_char_count(text) <= max_chars:
        return text
    # 将有效字符预算转换为字符串位置
    eff = 0
    cutoff = len(text)
    for i, ch in enumerate(text):
        eff += 1 if _text_char_count(ch) else 0
        if eff > max_chars:
            cutoff = i + 1
            break
    # 先尝试在句号/感叹号/问号处截断
    for sep in ['。', '！', '？', '!', '?']:
        idx = text[:cutoff].rfind(sep)
        if idx > 0:
            return text[:idx + 1]
    # 回退：在最后一个逗号/顿号处截断，补句号
    for sep in ['，', '、', '；', ',']:
        idx = text[:cutoff].rfind(sep)
        if idx > 3:
            return text[:idx] + '。'
    # 无法在合理边界截断，跳过该段（避免产生不通顺的半句话）
    return ""


def _validate_narration_budget(narration, scenes_analysis):
    """后验证：按场景检查总字数，超预算在句子边界缩减"""
    effective_rate = CONFIG["speech_rate"] * CONFIG["speech_safety_margin"]
    breath_sec = CONFIG.get("breath_ms", 600) / 1000

    # 第一轮：逐段校验 — 超预算则截断到句子边界（保留覆盖，避免碎片）
    for n in narration:
        text = n.get("narration", "")
        if not text.strip():
            continue
        seg_duration = n.get("end", 0) - n.get("start", 0)
        seg_pause = n.get("pause_after_ms", CONFIG.get("breath_ms", 600)) / 1000
        available = max(0.5, seg_duration - seg_pause)
        # TTS 至少需要 2s 可用时间，否则即使最短文本也放不下
        if available < 2.0:
            n["narration"] = ""
            continue
        max_chars = max(5, int(available * effective_rate))
        # 短段弹性系数更低：3s以下不弹性，3-5s 允许 20%，5s+ 允许 30%
        flex = 1.0 if seg_duration < 3.0 else (1.2 if seg_duration < 5.0 else 1.3)
        if _text_char_count(text) > max_chars * flex:
            truncated = _truncate_at_sentence(text, max_chars)
            if truncated and len(truncated) >= 5:
                n["narration"] = truncated
            else:
                n["narration"] = ""

    # 第二轮：按场景总预算校验
    # 为每个场景计算预算（可用时间 = 场景时长 - 呼吸空间）
    scene_budgets = {}
    for s in scenes_analysis:
        duration = s["end"] - s["start"]
        available = max(1.0, duration - breath_sec)
        scene_budgets[s["scene_id"]] = max(5, int(available * effective_rate))

    # 收集需要重写的段（超预算但不截断，让 LLM 重写）
    rewrite_tasks = []  # [(narration_item, max_chars, scene_id)]
    for s in scenes_analysis:
        sid = s["scene_id"]
        budget = scene_budgets[sid]
        segs = [n for n in narration
                if s["start"] <= (n.get("start", 0) + n.get("end", 0)) / 2 <= s["end"]]
        if not segs:
            continue

        total_chars = sum(len(n.get("narration", "")) for n in segs)
        if total_chars <= budget:
            continue

        # 超预算：分配每段预算，找出超出的段
        n_segs = len(segs)
        per_seg_budget = max(5, budget // max(n_segs, 1))
        remaining = budget
        for n in segs:
            text = n.get("narration", "")
            seg_budget = min(per_seg_budget + 5, remaining)  # 允许微调
            if len(text) > seg_budget and remaining > 5:
                rewrite_tasks.append((n, seg_budget, sid))
                remaining = max(0, remaining - seg_budget)
                log(f"  场景{sid+1} 段超预算: \"{text[:20]}...\" ({len(text)}字 > {seg_budget}字), 标记重写")
            else:
                remaining = max(0, remaining - len(text))

    # 批量让 LLM 重写超预算的段
    if rewrite_tasks:
        prompt_parts = []
        for i, (n, mc, sid) in enumerate(rewrite_tasks):
            text = n.get("narration", "")
            slot_dur = n["end"] - n["start"]
            prompt_parts.append(f"{i+1}. 原文({len(text)}字，需压缩到≤{mc}字，时段{slot_dur:.1f}s): {text}")

        rewrite_prompt = (
            "以下解说段落超出时间预算，请重写每段使其在字数限制内。\n"
            "要求：\n"
            "- 用一针见血的短句表达核心洞察，而不是概括事件\n"
            "- 好的短解说示例：「她在试探他的底线。」「他就是想找借口靠近。」\n"
            "- 差的短解说示例：「女方客气作评价。」「男子尝酱满脸嫌弃。」\n"
            "- 必须是完整的一句话，以句号/感叹号/问号结尾\n"
            "- 如果字数限制少于6字，输出空字符串表示跳过该段\n"
            "- 严格按 JSON 数组格式输出，顺序对应\n\n"
            + "\n".join(prompt_parts) + "\n\n"
            + "输出格式: [\"重写后的第1段\", \"重写后的第2段\", ...]"
        )

        payload = {
            "model": CONFIG.get("llm_model", CONFIG["vlm_model"]),
            "messages": [
                {"role": "system", "content": "你是视频解说文案精炼师。将过长的解说压缩到指定字数内。核心要求：揭示角色潜台词或真实意图，而不是概括事件。宁可空字符串跳过，也不要写空洞概括。" + (f"\n已知背景：{CONFIG.get('context_info', '')}" if CONFIG.get("context_info", "") else "")},
                {"role": "user", "content": rewrite_prompt},
            ],
            "max_tokens": 2000,
        }

        log(f"  重写 {len(rewrite_tasks)} 段超预算解说...")
        try:
            resp = api_call(payload)
            content = resp["choices"][0]["message"].get("content", "") or ""
            if not content:
                content = resp["choices"][0]["message"].get("reasoning_content", "")
            # 解析 JSON 数组
            rewrites = _parse_narration_json(content)
            if isinstance(rewrites, list) and len(rewrites) == len(rewrite_tasks):
                for i, (n, max_chars, sid) in enumerate(rewrite_tasks):
                    if i < len(rewrites):
                        new_text = rewrites[i] if isinstance(rewrites[i], str) else rewrites[i].get("narration", "")
                        if new_text and len(new_text) <= max_chars * 1.1:
                            n["narration"] = new_text
                            log(f"    重写: \"{new_text}\" ({len(new_text)}字)")
                        else:
                            n["narration"] = ""
                            log(f"    重写超限，丢弃: \"{new_text[:20]}...\"")
            else:
                # 解析失败，尝试简单分割
                import json as _json
                try:
                    parsed = _json.loads(content)
                    if isinstance(parsed, list):
                        for i, item in enumerate(parsed):
                            if i < len(rewrite_tasks):
                                n, mc, sid = rewrite_tasks[i]
                                text = item if isinstance(item, str) else item.get("narration", "")
                                if text and len(text) <= mc * 1.1:
                                    n["narration"] = text
                except (_json.JSONDecodeError, TypeError):
                    log(f"    重写结果解析失败，丢弃超预算段")
                    for n, mc, sid in rewrite_tasks:
                        n["narration"] = ""
        except (KeyError, IndexError, RuntimeError):
            log(f"    重写 API 异常，丢弃超预算段")
            for n, mc, sid in rewrite_tasks:
                n["narration"] = ""

    # 移除空文本段、过短残段、零时长段和以不完整标点结尾的段
    narration = [n for n in narration
                 if n.get("narration", "").strip()
                 and len(n.get("narration", "").strip()) >= 5
                 and n.get("end", 0) - n.get("start", 0) >= 1.0
                 and n["narration"].strip()[-1] not in "，：、；,—…"]

    # 清理标点错误
    for n in narration:
        text = n.get("narration", "")
        # 修复：[标点]。→ 。
        text = re.sub(r'[，：、；,]["\']?[。！？]', '。', text)
        # 清理末尾不匹配的左引号："好。→ 好。
        text = re.sub(r'["\']。$', '。', text)
        n["narration"] = text

    # 修正时间边界：确保每段在场景范围内，且 start < end
    valid = []
    for n in narration:
        if n["start"] >= n["end"]:
            continue
        for s in scenes_analysis:
            if s["start"] <= n["start"] < s["end"]:
                n["end"] = min(n["end"], s["end"])
                break
        # 边界修正后再次检查时长（可能被裁到很短）
        if n["end"] - n["start"] >= 1.0:
            valid.append(n)
    narration = valid

    # 去重：相同文本只保留一段
    seen_text = set()
    unique = []
    for n in narration:
        key = n["narration"].strip()
        if key not in seen_text:
            seen_text.add(key)
            unique.append(n)
        else:
            log(f"  去重: 删除重复文本 '{key[:25]}...'")
    narration = unique

    # 按时间排序并检测重叠
    narration.sort(key=lambda n: n["start"])
    deduped = []
    for n in narration:
        if deduped and n["start"] < deduped[-1]["end"]:
            prev = deduped[-1]
            log(f"  重叠: 段({n['start']:.1f}-{n['end']:.1f}) vs "
                f"({prev['start']:.1f}-{prev['end']:.1f})")
            if len(n["narration"]) > len(prev["narration"]):
                deduped[-1] = n
        else:
            deduped.append(n)
    narration = deduped

    return narration


# ── Phase 2: 画面对齐检测 + 自动重写 ──────────────────────────────────

# 中文停用词（简化版，覆盖常见虚词/代词/量词）
_STOP_WORDS = set("的了是在不有人我他她它们这那个一上下来去说到做会能要就和又"
                  "也都被从把让给向与而为则若虽却已之于以及其中".split())
_STOP_WORDS.update(["什么", "怎么", "这个", "那个", "一个", "自己", "已经",
                     "可以", "因为", "所以", "但是", "如果", "虽然", "而且"])


def _extract_noun_phrases(text):
    """从中文文本中提取2-4字名词短语（无需分词库）"""
    phrases = set()
    if not text:
        return phrases
    # 过滤标点，保留中文和数字
    cleaned = re.sub(r'[^一-鿿0-9]', '', text)
    # 滑动窗口提取2-4字片段
    for length in (4, 3, 2):
        for i in range(len(cleaned) - length + 1):
            phrase = cleaned[i:i + length]
            # 跳过全停用词的片段
            if all(c in _STOP_WORDS for c in phrase):
                continue
            # 至少包含一个实词字符（非停用词）
            if any(c not in _STOP_WORDS for c in phrase):
                phrases.add(phrase)
    return phrases


def _extract_frame_entities(vlm_analysis, start, end):
    """从帧动作描述中收集时间段内所有关键实体"""
    entities = set()
    for scene in vlm_analysis:
        if not (scene["start"] < end and scene["end"] > start):
            continue
        facts = scene.get("frame_facts", {})
        for ts, actions in facts.items():
            t = float(ts)
            if start <= t <= end:
                for action in actions:
                    entities.update(_extract_noun_phrases(action))
    return entities


def _post_dedup_narration(narration):
    """去除相邻相似解说段（字符级 Jaccard >60% 则合并）"""
    if len(narration) < 2:
        return narration
    result = [narration[0]]
    for seg in narration[1:]:
        prev = result[-1]
        if not prev["narration"].strip() or not seg["narration"].strip():
            result.append(seg)
            continue
        # 字符级 Jaccard 相似度
        set_a, set_b = set(prev["narration"]), set(seg["narration"])
        if not set_a or not set_b:
            result.append(seg)
            continue
        overlap = len(set_a & set_b) / min(len(set_a), len(set_b))
        if overlap > 0.6:
            # 保留更长的版本，合并时间范围
            if len(seg["narration"]) > len(prev["narration"]):
                prev["narration"] = seg["narration"]
            prev["end"] = seg["end"]
            prev["pause_after_ms"] = seg.get("pause_after_ms", prev.get("pause_after_ms", 600))
            log(f"  去重合并: {prev['start']:.0f}-{prev['end']:.0f}s")
        else:
            result.append(seg)
    removed = len(narration) - len(result)
    if removed:
        log(f"  去重: {len(narration)} → {len(result)} 段 (合并 {removed} 段)")
    return result


def _validate_and_rewrite_narration(narration, vlm_analysis, work_dir):
    """关键词检测 → 约束重写闭环"""
    pass_threshold = 0.30
    warn_threshold = 0.10
    report_segments = []

    for n in narration:
        text = n.get("narration", "")
        if not text.strip():
            continue

        seg_start = n.get("start", 0)
        seg_end = n.get("end", 0)

        # 收集该时段的帧实体
        frame_entities = _extract_frame_entities(vlm_analysis, seg_start, seg_end)
        if not frame_entities:
            report_segments.append({
                "start": seg_start, "end": seg_end,
                "match_rate": 1.0, "level": "PASS",
                "reason": "no frame_facts for this time range",
            })
            continue

        # 从解说中提取实体
        narr_entities = _extract_noun_phrases(text)
        if not narr_entities:
            report_segments.append({
                "start": seg_start, "end": seg_end,
                "match_rate": 1.0, "level": "PASS",
                "reason": "no entities extracted from narration",
            })
            continue

        # 计算匹配率
        matched = 0
        mismatched = []
        for ne in narr_entities:
            found = False
            for fe in frame_entities:
                if ne in fe or fe in ne:
                    found = True
                    break
            if found:
                matched += 1
            else:
                mismatched.append(ne)

        match_rate = matched / len(narr_entities) if narr_entities else 0

        # 确定级别
        if match_rate >= pass_threshold:
            level = "PASS"
        elif match_rate >= warn_threshold:
            level = "WARN"
        else:
            level = "WARN_HIGH"

        seg_report = {
            "start": seg_start, "end": seg_end,
            "match_rate": round(match_rate, 2), "level": level,
            "narration_preview": text[:50],
            "frame_entities_count": len(frame_entities),
            "mismatched_sample": mismatched[:5],
        }
        report_segments.append(seg_report)

        # WARN_HIGH 且启用自动重写 → 约束重写
        if level == "WARN_HIGH":
            log(f"  对齐检测 WARN_HIGH ({match_rate:.0%}): \"{text[:30]}...\" → 触发重写")
            rewritten = _rewrite_segment_with_constraints(n, vlm_analysis, mismatched)
            if rewritten:
                n["narration"] = rewritten
                n["alignment_rewritten"] = True
                seg_report["rewritten"] = True
                seg_report["new_narration_preview"] = rewritten[:50]

    # 汇总
    levels = [s["level"] for s in report_segments]
    summary = {
        "total": len(report_segments),
        "pass": levels.count("PASS"),
        "warn": levels.count("WARN"),
        "warn_high": levels.count("WARN_HIGH"),
        "rewritten": sum(1 for s in report_segments if s.get("rewritten")),
    }

    report = {"segments": report_segments, "summary": summary}
    report_path = Path(work_dir) / "alignment_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    log(f"对齐检测完成: {summary['pass']} PASS, {summary['warn']} WARN, {summary['warn_high']} WARN_HIGH"
        + (f", {summary['rewritten']} rewritten" if summary["rewritten"] else ""))

    return narration, report


def _rewrite_segment_with_constraints(seg, vlm_analysis, mismatched):
    """对不匹配的解说段执行约束重写（最多1轮）"""
    start = seg.get("start", 0)
    end = seg.get("end", 0)
    original = seg.get("narration", "")
    effective_rate = CONFIG["speech_rate"] * CONFIG["speech_safety_margin"]
    breath_sec = CONFIG.get("breath_ms", 600) / 1000
    max_chars = max(10, int((end - start - breath_sec) * effective_rate))

    # 收集该时段的帧动作描述
    facts_lines = []
    for scene in vlm_analysis:
        if not (scene["start"] < end and scene["end"] > start):
            continue
        for ts, actions in sorted(scene.get("frame_facts", {}).items(),
                                   key=lambda x: float(x[0])):
            t = float(ts)
            if start <= t <= end:
                facts_lines.append(f"{ts}s: {'; '.join(actions)}")

    if not facts_lines:
        return None

    facts_text = "\n".join(facts_lines)
    constraints = (
        f"你之前为 {start:.1f}s-{end:.1f}s 时段写的解说提到了: {', '.join(mismatched[:5])}\n"
        f"但画面数据中不包含这些内容。该时段实际画面:\n{facts_text}\n\n"
        f"请仅基于以上画面数据重新生成该时段的解说（不超过{max_chars}字）。\n"
        f"要求：讲故事风格，不要描述画面本身，讲画面背后的故事。只输出解说文本。"
    )

    payload = {
        "model": CONFIG.get("llm_model", CONFIG["vlm_model"]),
        "messages": [{"role": "user", "content": constraints}],
        "max_tokens": 500,
    }

    try:
        resp = api_call(payload)
        text = resp.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        if text and len(text) <= max_chars * 1.3 and len(text) >= 5:
            return text
    except Exception as e:
        log(f"  约束重写失败: {e}")
    return None


def _extract_json_from_text(text):
    """从推理模型的思考文本中提取 JSON（数组或单个对象）"""
    if not text:
        return ""
    for pattern in [r"```json\s*(.*?)\s*```", r"```\s*(.*?)\s*```"]:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            candidate = m.group(1).strip()
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, (list, dict)):
                    return candidate
            except json.JSONDecodeError:
                continue
    # 尝试匹配裸 JSON 数组或对象
    for pattern in [r"(\[.*\])", r"(\{.*\})"]:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            candidate = m.group(1).strip()
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, (list, dict)):
                    return candidate
            except json.JSONDecodeError:
                continue
    return ""


def _zone_coverage_fill(narration, scenes_analysis, asr_result, silence_periods, work_dir):
    """混合模式补充：zone 解说后，为未覆盖的重要场景生成 fill 解说。
    fill 段标记 overlaps_speech=True，组装时使用 speech ducking。
    """
    if not narration or not scenes_analysis:
        return narration

    # 计算当前覆盖的场景
    covered = set()
    for n in narration:
        if not n.get("narration", "").strip():
            continue
        n_mid = (n["start"] + n["end"]) / 2
        for s in scenes_analysis:
            if s["start"] <= n_mid <= s["end"]:
                covered.add(s["scene_id"])
                break
    coverage = len(covered) / len(scenes_analysis)
    target = 0.60
    if coverage >= target:
        log(f"Zone+Fill 覆盖率 {coverage:.0%} 已达标 ({len(covered)}/{len(scenes_analysis)})")
        return narration

    # 找未覆盖的重要场景（>5s），长场景拆分为子段
    uncovered_raw = [s for s in scenes_analysis
                     if s["scene_id"] not in covered and (s["end"] - s["start"]) >= 5.0]
    uncovered = []
    for s in uncovered_raw:
        dur = s["end"] - s["start"]
        if dur <= 30:
            uncovered.append(s)
        else:
            # 拆分为 ~20s 子段
            sub_dur = 20.0
            pos = s["start"]
            idx = 0
            while pos < s["end"]:
                sub_end = min(pos + sub_dur, s["end"])
                sub = dict(s)
                sub["start"] = pos
                sub["end"] = sub_end
                sub["is_sub"] = True
                sub["sub_idx"] = idx
                uncovered.append(sub)
                pos = sub_end
                idx += 1
    if not uncovered:
        return narration
    log(f"Zone 覆盖率 {coverage:.0%}，补充 {len(uncovered)} 个重要未覆盖场景 (ducking)")

    # 构建 ASR 场景映射
    scene_asr = {}
    if asr_result:
        for seg in asr_result:
            text = seg.get("text", "").strip()
            if not text:
                continue
            a_start = seg.get("start", 0)
            a_end = seg.get("end", 0)
            for s in scenes_analysis:
                if s["start"] < a_end and s["end"] > a_start:
                    scene_asr.setdefault(s["scene_id"], []).append(text)

    # 构建 context
    narration_sorted = sorted(narration, key=lambda x: x["start"])
    existing_ctx = "\n".join(
        f"  [{n['start']:.1f}-{n['end']:.1f}] {n['narration']}"
        for n in narration_sorted
    )

    # 并行 fill
    fill_workers = min(len(uncovered), CONFIG.get("fill_workers", 4))
    with ThreadPoolExecutor(max_workers=fill_workers) as executor:
        futures = {executor.submit(
            _generate_single_fill, scene, silence_periods, existing_ctx,
            "; ".join(scene_asr.get(scene["scene_id"], []))
        ): scene for scene in uncovered}
        for future in as_completed(futures):
            try:
                seg = future.result()
            except Exception:
                continue
            if seg:
                seg["overlaps_speech"] = True
                narration.append(seg)

    narration.sort(key=lambda x: x["start"])
    # 去重
    narration = _post_dedup_narration(narration)

    # 报告最终覆盖率
    covered2 = set()
    for n in narration:
        if not n.get("narration", "").strip():
            continue
        n_mid = (n["start"] + n["end"]) / 2
        for s in scenes_analysis:
            if s["start"] <= n_mid <= s["end"]:
                covered2.add(s["scene_id"])
                break
    final_cov = len(covered2) / len(scenes_analysis) if scenes_analysis else 1.0
    zone_count = sum(1 for n in narration if not n.get("overlaps_speech"))
    fill_count = sum(1 for n in narration if n.get("overlaps_speech"))
    log(f"混合模式完成: {len(narration)} 段 (zone={zone_count}, fill={fill_count}), 覆盖率 {final_cov:.0%}")
    return narration


def _align_narration_to_quiet(narration, scenes_analysis, silence_periods):
    """将解说段移到同场景内的安静窗口，标记是否与语音重叠"""
    if not silence_periods:
        for n in narration:
            n["overlaps_speech"] = False
        return narration

    quiet_windows = [qp for qp in silence_periods if not qp.get("has_speech", False)]

    for n in narration:
        seg_start = n["start"]
        seg_end = n["end"]
        seg_dur = seg_end - seg_start
        best_window = None
        best_overlap = 0

        # 找同时间范围内最佳安静窗口
        for qw in quiet_windows:
            overlap_start = max(seg_start, qw["start"])
            overlap_end = min(seg_end, qw["end"])
            overlap = overlap_end - overlap_start
            if overlap > best_overlap:
                best_overlap = overlap
                best_window = qw

        if best_window and best_overlap > 0:
            # 尝试将段移到安静窗口内
            new_start = max(best_window["start"], seg_start - (seg_dur * 0.5))
            new_start = min(new_start, best_window["end"] - seg_dur)

            # 确保不超出场景边界（用 midpoint 查找，与覆盖率/验证一致）
            seg_mid = (seg_start + seg_end) / 2
            parent_scene = None
            for s in scenes_analysis:
                if s["start"] <= seg_mid <= s["end"]:
                    parent_scene = s
                    break
            if parent_scene:
                new_start = max(parent_scene["start"], new_start)
                new_start = min(new_start, parent_scene["end"] - seg_dur)

            # 不允许移到视频之前
            new_start = max(0.0, new_start)
            new_start = round(new_start, 2)
            new_end = round(new_start + seg_dur, 2)

            if new_end > new_start:
                n["start"] = new_start
                n["end"] = new_end
                n["overlaps_speech"] = False
            else:
                n["overlaps_speech"] = True
        else:
            n["overlaps_speech"] = True

    # 后处理：解决段间重叠（按时间排序，确保后段不与前段冲突）
    narration.sort(key=lambda x: x["start"])
    for i in range(1, len(narration)):
        prev = narration[i - 1]
        curr = narration[i]
        min_gap = 0.3  # 段间最小间隔
        min_start = prev["end"] + min_gap
        if curr["start"] < min_start:
            seg_dur = curr["end"] - curr["start"]
            curr["start"] = round(min_start, 2)
            curr["end"] = round(min_start + seg_dur, 2)
            # 检查是否超出父场景边界
            mid = (curr["start"] + curr["end"]) / 2
            for s in scenes_analysis:
                if s["start"] <= mid <= s["end"]:
                    if curr["end"] > s["end"]:
                        curr["end"] = round(s["end"], 2)
                    break
            # 段太短则清空（后续会被过滤）
            if curr["end"] - curr["start"] < 1.5:
                curr["narration"] = ""

    # 后处理：预算重校验 — alignment 可能缩短了段时长，截断超预算文本
    effective_rate = CONFIG["speech_rate"] * CONFIG["speech_safety_margin"]
    breath_sec = CONFIG.get("breath_ms", 600) / 1000
    for n in narration:
        dur = n["end"] - n["start"]
        if dur < 1.0:
            n["narration"] = ""
            continue
        max_chars = max(5, int((dur - breath_sec) * effective_rate))
        text = n.get("narration", "")
        chars = len(re.sub(r'[，。！？、…\s]', '', text))
        if chars > max_chars:
            truncated = _truncate_at_sentence(text, max_chars)
            if truncated and len(re.sub(r'[，。！？、…\s]', '', truncated)) >= 5:
                n["narration"] = truncated
            else:
                n["narration"] = ""

    return narration


def _parse_narration_json(text):
    """从 LLM 输出中解析 JSON 解说脚本"""
    if not text or not text.strip():
        return []

    # 尝试直接解析
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return [result]
    except json.JSONDecodeError:
        pass

    # 尝试提取 JSON 块（数组或对象）
    patterns = [
        r"```json\s*(.*?)\s*```",
        r"```\s*(.*?)\s*```",
        r"(\[.*\])",
        r"(\{.*\})",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            try:
                result = json.loads(m.group(1))
                if isinstance(result, list) and len(result) > 0:
                    return result
                if isinstance(result, dict):
                    return [result]
            except json.JSONDecodeError:
                continue

    log(f"警告: 无法解析 LLM JSON 输出 ({len(text)} 字符)")
    return []


# ── Step 6: TTS 合成 ─────────────────────────────────────────────────


def _parse_rate_offset(rate_str):
    """'+5%' -> 0.05, '-3%' -> -0.03, '+0%' -> 0.0"""
    m = re.match(r'([+-])(\d+)%', rate_str)
    if m:
        return float(m.group(1) + m.group(2)) / 100.0
    return 0.0


def _compute_tts_params(text, narration, seg_index):
    """根据内容特征和位置计算 TTS 语速/音高参数"""
    rate = "+5%"
    pitch = "+0Hz"
    total = len(narration)
    last_char = text[-1] if text else ""

    # 位置相关
    if seg_index == 0:
        rate = "+5%"       # 开头稍快，抓住注意力
    elif seg_index >= total - 1:
        rate = "-5%"       # 结尾放慢，收束感
    elif seg_index >= total - 2:
        rate = "-2%"       # 倒数第二段略慢

    # 内容相关
    has_exclamation = any(c in text for c in "！!")
    has_question = "？" in text or "?" in text
    has_ellipsis = "……" in text or "..." in text

    if has_exclamation:
        rate = "+8%"
        pitch = "+3Hz"    # 感叹句加速+微升调
    elif has_question:
        pitch = "+5Hz"    # 疑问句升调
    elif has_ellipsis:
        rate = "-3%"      # 省略号（悬念/犹豫）放慢

    # 长文本稍快
    if len(text) > 35 and not has_ellipsis:
        rate = max(rate, "+6%", key=lambda x: int(x.rstrip('%+-')))

    return rate, pitch

def _clean_narration_text(text):
    """清理解说文本中 TTS 不应读出的内容"""
    if not text:
        return text
    # 移除 markdown 格式标记
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)  # **bold** → bold
    text = re.sub(r'\*(.+?)\*', r'\1', text)       # *italic* → italic
    text = re.sub(r'「(.+?)」', r'\1', text)        # 「quote」 → quote
    text = re.sub(r'」|「|『|』', '', text)
    # 移除方括号标注（[climax]、[suspense] 等舞台指示）
    text = re.sub(r'\[[^\]]*\]', '', text)
    # 移除圆括号标注（（旁白）、（转场）等）
    text = re.sub(r'[（(][^）)]*[）)]', '', text)
    # 规范化省略号和重复标点
    text = re.sub(r'\.{3,}|…{2,}', '……', text)
    text = re.sub(r'……+', '……', text)
    text = re.sub(r'([。！？，；：])\1+', r'\1', text)  # 重复标点 → 单个
    # 移除 emoji
    text = re.sub(r'[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF'
                  r'\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U0000FE00-\U0000FEFF]', '', text)
    # 清理多余空白
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _synthesize_segment(i, seg, narration, tts_dir, engine):
    """合成单个 TTS 段（线程安全），支持 resume 跳过已有文件"""
    text = _clean_narration_text(seg["narration"])
    if not text or not text.strip():
        return None

    output_wav = tts_dir / f"narr_{i:03d}.wav"

    # Resume: 已有 WAV 文件直接复用（narration 在 step 5 后不变）
    if output_wav.exists():
        existing_dur = _get_audio_duration(output_wav)
        if existing_dur > 0:
            log(f"  段 {i+1}: 复用已有 ({existing_dur:.1f}s)")
            return {
                "index": i, "start": seg["start"], "end": seg["end"],
                "narration": text, "audio_path": str(output_wav),
                "audio_duration": existing_dur,
                "tts_rate_offset": 0.0,
                "pause_after_ms": seg.get("pause_after_ms", CONFIG.get("breath_ms", 600)),
                "overlaps_speech": seg.get("overlaps_speech", False),
            }

    if CONFIG.get("tts_dynamic_params", True):
        rate, pitch = _compute_tts_params(text, narration, i)
    else:
        rate, pitch = "+0%", "+0Hz"

    if engine == "indextts2":
        _tts_indextts2(text, output_wav, rate=rate, pitch=pitch)
    elif engine == "edge-tts":
        _tts_edge(text, output_wav, rate=rate, pitch=pitch)
    else:
        _tts_say(text, output_wav, rate=rate, pitch=pitch)

    dur = _get_audio_duration(output_wav)
    seg_slot = seg["end"] - seg["start"]
    seg_pause = seg.get("pause_after_ms", CONFIG.get("breath_ms", 600)) / 1000
    available = max(0.5, seg_slot - seg_pause)
    atempo_max = 1.2
    if dur > available * atempo_max and len(text) > 5:
        chars_per_sec = len(text) / dur if dur > 0 else 3.0
        target_chars = max(5, int(available * atempo_max * chars_per_sec) - 1)
        truncated = _truncate_at_sentence(text, target_chars)
        if truncated and len(truncated) >= 5 and truncated != text:
            text = truncated
            if engine == "indextts2":
                _tts_indextts2(text, output_wav, rate=rate, pitch=pitch)
            elif engine == "edge-tts":
                _tts_edge(text, output_wav, rate=rate, pitch=pitch)
            else:
                _tts_say(text, output_wav, rate=rate, pitch=pitch)
            dur = _get_audio_duration(output_wav)

    return {
        "index": i,
        "start": seg["start"],
        "end": seg["end"],
        "narration": text,
        "audio_path": str(output_wav),
        "audio_duration": dur,
        "tts_rate_offset": _parse_rate_offset(rate),
        "pause_after_ms": seg.get("pause_after_ms", CONFIG.get("breath_ms", 600)),
        "overlaps_speech": seg.get("overlaps_speech", False),
    }


def synthesize_tts(narration, work_dir):
    """合成解说音频（并行）"""
    tts_dir = work_dir / "tts_segments"
    tts_dir.mkdir(exist_ok=True)

    engine = CONFIG["tts_engine"]
    if engine == "auto":
        engine = _detect_tts_engine()

    log(f"TTS 引擎: {engine}")

    segments = []
    max_workers = CONFIG.get("tts_workers", 4)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_synthesize_segment, i, seg, narration, tts_dir, engine): i
            for i, seg in enumerate(narration)
        }
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as e:
                log(f"  TTS 段失败: {e}")
                continue
            if result:
                segments.append(result)
                log(f"  段 {result['index']+1}: {result['audio_duration']:.1f}s - {result['narration'][:25]}...")

    segments.sort(key=lambda x: x["index"])
    return segments, engine


def _detect_tts_engine():
    """自动检测可用的 TTS 引擎"""
    # 优先 IndexTTS2
    try:
        from indextts.infer_v2 import IndexTTS2  # noqa: F401
        return "indextts2"
    except ImportError:
        pass

    # Edge-TTS
    if shutil.which("edge-tts"):
        return "edge-tts"

    # macOS say
    if shutil.which("say"):
        return "say"

    raise RuntimeError("没有可用的 TTS 引擎。请安装 edge-tts: pip3 install edge-tts")


def _tts_edge(text, output_path, rate="+0%", pitch="+0Hz"):
    """使用 Edge-TTS 合成"""
    mp3_path = str(output_path).replace(".wav", ".mp3")
    cmd = ["edge-tts", "--voice", CONFIG["edge_tts_voice"],
           "--text", text, "--rate", rate, "--pitch", pitch,
           "--write-media", mp3_path]
    result = run_cmd(cmd, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"Edge-TTS 失败: {result.stderr}")

    # 转为 WAV + highpass 去除低频隆隆声
    cmd = ["ffmpeg", "-y", "-i", mp3_path,
           "-af", "highpass=f=80", "-ar", "44100", "-ac", "1", str(output_path)]
    run_cmd(cmd)
    os.remove(mp3_path)


def _tts_indextts2(text, output_path, rate="+0%", pitch="+0Hz"):
    """使用 IndexTTS2 合成"""
    from indextts.infer_v2 import IndexTTS2
    tts = IndexTTS2(cfg_path="checkpoints/config.yaml", model_dir="checkpoints")
    tts.infer(
        spk_audio_prompt="",
        text=text,
        output_path=str(output_path),
        verbose=False,
    )


def _tts_say(text, output_path, rate="+0%", pitch="+0Hz"):
    """使用 macOS say 合成"""
    aiff_path = str(output_path).replace(".wav", ".aiff")
    cmd = ["say", "-v", CONFIG["say_voice"], text, "-o", aiff_path]
    run_cmd(cmd)

    cmd = ["ffmpeg", "-y", "-i", aiff_path,
           "-af", "highpass=f=80", "-ar", "44100", "-ac", "1", str(output_path)]
    run_cmd(cmd)
    if os.path.exists(aiff_path):
        os.remove(aiff_path)


def _get_audio_duration(audio_path):
    """获取音频文件时长"""
    cmd = ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
           "-of", "csv=p=0", str(audio_path)]
    result = run_cmd(cmd)
    if result.returncode == 0 and result.stdout.strip():
        try:
            return float(result.stdout.strip())
        except ValueError:
            pass
    return 0.0


# ── Step 7: 视频组装 ─────────────────────────────────────────────────


def _seconds_to_srt_time(seconds):
    """将秒数转为 SRT 时间格式 HH:MM:SS,mmm"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _wrap_srt_text(text, max_chars=20):
    """将长文本按标点/字数换行，适配字幕显示"""
    if len(text) <= max_chars:
        return text
    # 优先在标点处断行
    lines = []
    current = ""
    for ch in text:
        current += ch
        if ch in "，。！？、；：—" and len(current) >= max_chars * 0.6:
            lines.append(current)
            current = ""
        elif len(current) >= max_chars + 5:
            # 强制断行
            lines.append(current)
            current = ""
    if current:
        lines.append(current)
    # SRT 最多两行
    if len(lines) > 2:
        lines = [lines[0], "".join(lines[1:])]
    return "\n".join(lines)


def _generate_srt(narration, work_dir):
    """将解说脚本转为 SRT 字幕文件，使用实际音频放置时间"""
    srt_lines = []
    idx = 0
    for seg in narration:
        start = seg.get("actual_place_start", seg["start"])
        end = seg.get("actual_place_end", seg["end"])
        if end - start < 0.1 or not seg.get("narration", "").strip():
            continue
        idx += 1
        start_ts = _seconds_to_srt_time(start)
        end_ts = _seconds_to_srt_time(end)
        srt_lines.append(str(idx))
        srt_lines.append(f"{start_ts} --> {end_ts}")
        srt_lines.append(_wrap_srt_text(seg["narration"]))
        srt_lines.append("")
    srt_path = work_dir / "subtitles.srt"
    srt_path.write_text("\n".join(srt_lines), encoding="utf-8")
    return srt_path


def assemble_video(input_video, tts_segments, work_dir, output_path):
    """组装最终视频"""
    if not tts_segments:
        log("没有解说音频，直接复制原视频")
        shutil.copy2(str(input_video), str(output_path))
        return output_path

    video_duration = get_video_duration(input_video)

    # 将所有 TTS 片段按时间位置合成到与视频等长的音轨上
    narration_wav = work_dir / "narration.wav"
    _build_timed_narration(tts_segments, narration_wav, video_duration, work_dir)

    # 始终生成 SRT 字幕文件
    srt_path = _generate_srt(tts_segments, work_dir)
    log(f"字幕文件: {srt_path}")

    # 混合原始音频 + 解说音频
    ducking_mode = CONFIG.get("ducking_mode", "fixed")

    # 构建动态音量控制：解说在安静窗口时原声大，与对白重叠时原声小
    has_overlaps = any(seg.get("overlaps_speech") for seg in tts_segments if isinstance(seg, dict))
    has_quiet = any(not seg.get("overlaps_speech", True) for seg in tts_segments if isinstance(seg, dict))

    if ducking_mode == "sidechaincompress":
        filter_complex = (
            "[0:a]aresample=48000,loudnorm=I=-16:TP=-1.5:LRA=11[orig];"
            "[1:a]aresample=48000,loudnorm=I=-14:TP=-1.5:LRA=11[narr];"
            f"[orig][narr]sidechaincompress="
            f"threshold={CONFIG['ducking_threshold']}:ratio={CONFIG['ducking_ratio']}"
            f":attack={CONFIG['ducking_attack']}:release={CONFIG['ducking_release']}"
            f":knee=2.5:makeup={CONFIG['ducking_makeup']}:level_sc={CONFIG['ducking_level_sc']}"
            f"[ducked];"
            f"[ducked][narr]amix=inputs=2:duration=first:dropout_transition=0"
            f":weights=1 {CONFIG['ducking_narr_weight']}:normalize=0[aout]"
        )
    elif ducking_mode == "none":
        filter_complex = (
            "[0:a]aresample=48000,loudnorm=I=-16:TP=-1.5:LRA=11[orig];"
            "[1:a]aresample=48000,loudnorm=I=-14:TP=-1.5:LRA=11[narr];"
            "[orig][narr]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
        )
    elif has_overlaps and has_quiet:
        # 动态 ducking：根据 overlaps_speech 切换原声音量
        if CONFIG.get("narration_mode") == "zone":
            quiet_vol = CONFIG.get("zone_ducking_volume", 0.12)
        else:
            quiet_vol = CONFIG.get("quiet_ducking_volume", 0.7)
        speech_vol = CONFIG.get("speech_ducking_volume", 0.2)
        narr_vol = CONFIG.get("ducking_narr_weight", 1.5)
        # 构建 if(between(t,s,e),1,0) 指示器，逗号用 \, 转义避免 ffmpeg 解析为 filter 分隔符
        # 使用实际放置时间（actual_place_start/end），非 LLM 指定时间
        def _seg_indicator(seg):
            s = seg.get("actual_place_start", seg.get("start", 0))
            e = seg.get("actual_place_end", seg.get("end", 0))
            return f"if(between(t,{s:.2f},{e:.2f}),1,0)"

        overlap_exprs = []
        quiet_exprs = []
        for seg in tts_segments:
            if not isinstance(seg, dict):
                continue
            ind = _seg_indicator(seg)
            if seg.get("overlaps_speech"):
                overlap_exprs.append(ind)
            else:
                quiet_exprs.append(ind)

        # 原声音量：解说重叠语音时=低，解说在安静窗口时=高，其他时间=正常
        default_vol = CONFIG.get("ducking_orig_volume", 0.5)
        vol_expr = f"{default_vol}"
        if overlap_exprs:
            vol_expr += f"+{speech_vol-default_vol}*({'+'.join(overlap_exprs)})"
        if quiet_exprs:
            vol_expr += f"+{quiet_vol-default_vol}*({'+'.join(quiet_exprs)})"
        vol_expr = f"max(0,min(1,{vol_expr}))"

        filter_complex = (
            f"[0:a]volume='{vol_expr}':eval=frame,aresample=48000[orig];"
            f"[1:a]volume={narr_vol},aresample=48000[narr];"
            "[orig][narr]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
        )
        log(f"动态 ducking: 语音重叠段={len(overlap_exprs)}, 安静段={len(quiet_exprs)}")
    elif has_quiet and not has_overlaps:
        if CONFIG.get("narration_mode") == "zone":
            # Zone 模式：解说时原声大幅压低，非解说时原声满音量，带平滑过渡
            duck_vol = CONFIG.get("zone_ducking_volume", 0.12)
            narr_vol = CONFIG.get("ducking_narr_weight", 1.5)
            default_vol = 1.0
            fade = CONFIG.get("zone_fade_seconds", 0.5)

            def _seg_ind_z(seg):
                s = seg.get("actual_place_start", seg.get("start", 0))
                e = seg.get("actual_place_end", seg.get("end", 0))
                # 平滑梯形：两端 fade_sec 线性渐变，中间满值
                return f"min(1,max(0,min(t-{s:.2f},{e:.2f}-t)/{fade:.1f}))"

            exprs = [_seg_ind_z(seg) for seg in tts_segments if isinstance(seg, dict)]
            vol_expr = f"{default_vol}"
            if exprs:
                vol_expr += f"+{duck_vol - default_vol}*({'+'.join(exprs)})"
            vol_expr = f"max(0,min(1,{vol_expr}))"

            filter_complex = (
                f"[0:a]volume='{vol_expr}':eval=frame,aresample=48000[orig];"
                f"[1:a]volume={narr_vol},aresample=48000[narr];"
                "[orig][narr]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
            )
            log(f"zone ducking: 解说时原声={duck_vol}, 非解说时原声={default_vol}")
        else:
            orig_vol = CONFIG.get("quiet_ducking_volume", 0.7)
            narr_vol = CONFIG.get("ducking_narr_weight", 1.5)
            filter_complex = (
                f"[0:a]volume={orig_vol},aresample=48000[orig];"
                f"[1:a]volume={narr_vol},aresample=48000[narr];"
                "[orig][narr]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
            )
            log(f"ducking: 全部安静窗口 (原声={orig_vol})")
    else:  # fixed (no narration overlap info)
        orig_vol = CONFIG.get("ducking_orig_volume", 0.5)
        narr_vol = CONFIG.get("ducking_narr_weight", 1.5)
        filter_complex = (
            f"[0:a]volume={orig_vol},aresample=48000[orig];"
            f"[1:a]volume={narr_vol},aresample=48000[narr];"
            "[orig][narr]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
        )

    # 对于超长 volume 表达式（多段解说），使用 -filter_complex_script 避免命令行溢出
    filter_complex_bytes = filter_complex.encode('utf-8')
    if len(filter_complex_bytes) > 8000:
        fc_script = Path(work_dir) / ".filter_complex.txt"
        fc_script.write_text(filter_complex)
        log(f"使用 filter_complex_script (表达式长度 {len(filter_complex_bytes)} bytes)")
        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_video),
            "-i", str(narration_wav),
            "-filter_complex_script", str(fc_script),
            "-map", "0:v", "-map", "[aout]",
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_video),
            "-i", str(narration_wav),
            "-filter_complex", filter_complex,
            "-map", "0:v", "-map", "[aout]",
        ]

    if CONFIG.get("burn_subtitles", False):
        # ffmpeg subtitles filter 需要转义 : 和 \
        srt_escaped = str(srt_path).replace("\\", "/").replace(":", "\\:")
        cmd += ["-vf", f"subtitles={srt_escaped}", "-c:v", "libx264", "-crf", "18"]
        log("烧录字幕（需要重编码）...")
    else:
        cmd += ["-c:v", "copy"]

    cmd += ["-c:a", "aac", "-b:a", "192k", "-t", str(video_duration), str(output_path)]
    try:
        result = run_cmd(cmd)
        if result.returncode != 0:
            raise RuntimeError(f"视频组装失败: {result.stderr}")
    finally:
        # 清理临时 filter_complex 脚本（无论 ffmpeg 是否成功）
        if len(filter_complex_bytes) > 8000:
            fc_script.unlink(missing_ok=True)

    log(f"最终视频: {output_path} ({output_path.stat().st_size / 1024 / 1024:.1f}MB)")
    return output_path


def _adjust_tts_speed(audio_path, target_duration, work_dir, tts_rate_offset=0.0):
    """如果 TTS 音频超过目标时长，用 ffmpeg atempo 温和加速"""
    current_dur = _get_audio_duration(audio_path)
    if current_dur <= target_duration or current_dur == 0:
        return str(audio_path), current_dur

    ratio = current_dur / target_duration

    # 累积上限：TTS rate × atempo ≤ 1.2x
    effective_max = 1.2 / (1.0 + tts_rate_offset)
    effective_max = max(1.0, effective_max)

    if ratio > effective_max:
        # 实际截断到目标时长，加 fade-out 防止爆音
        truncated_path = Path(str(audio_path).replace(".wav", "_cut.wav"))
        fade_out = min(0.15, target_duration * 0.1)
        cmd = ["ffmpeg", "-y", "-i", str(audio_path),
               "-t", f"{target_duration:.3f}",
               "-af", f"afade=t=out:st={max(0, target_duration - fade_out):.3f}:d={fade_out:.3f}",
               "-ar", "44100", "-ac", "1", str(truncated_path)]
        result = run_cmd(cmd)
        if result.returncode == 0:
            new_dur = _get_audio_duration(truncated_path)
            log(f"  TTS 截断: {current_dur:.1f}s → {new_dur:.1f}s (无法加速到 x{ratio:.2f})")
            return str(truncated_path), new_dur
        log(f"  警告: TTS 截断失败，保留原音频 ({current_dur:.1f}s)")
        return str(audio_path), current_dur

    # 温和加速
    tempo = min(ratio, effective_max)
    adjusted_path = Path(str(audio_path).replace(".wav", "_adj.wav"))
    cmd = ["ffmpeg", "-y", "-i", str(audio_path),
           "-filter:a", f"atempo={tempo:.3f}",
           "-ar", "44100", "-ac", "1", str(adjusted_path)]
    result = run_cmd(cmd)
    if result.returncode == 0:
        new_dur = _get_audio_duration(adjusted_path)
        log(f"  TTS 温和加速: {current_dur:.1f}s → {new_dur:.1f}s (x{tempo:.2f})")
        return str(adjusted_path), new_dur
    return str(audio_path), current_dur


def _build_timed_narration(tts_segments, output_wav, video_duration, work_dir):
    """将 TTS 片段按时间轴放置到一条与视频等长的音轨上"""
    sample_rate = 44100
    total_samples = int(video_duration * sample_rate)
    buffer = bytearray(total_samples * 2)
    last_written_end = 0  # 追踪已写入位置，防止重叠
    prev_pause_samples = 0  # 前一段的 pause_after_ms，控制段间间隔

    for seg in tts_segments:
        wav_path = seg["audio_path"]
        seg_pause_ms = seg.get("pause_after_ms", CONFIG.get("breath_ms", 600))

        if not os.path.exists(wav_path):
            prev_pause_samples = int(seg_pause_ms * sample_rate / 1000)
            continue

        # WAV 格式验证 + 采样率检查（合并为一次 wave.open）
        original_wav_path = wav_path
        _do_resample = False
        try:
            with wave.open(wav_path, 'rb') as wf_check:
                wf_channels = wf_check.getnchannels()
                wf_sampwidth = wf_check.getsampwidth()
                if wf_sampwidth != 2 or wf_channels != 1:
                    log(f"  跳过非标准 WAV: {wav_path} (channels={wf_channels}, sampwidth={wf_sampwidth}), 需要 mono 16-bit")
                    prev_pause_samples = int(seg_pause_ms * sample_rate / 1000)
                    continue
                if wf_check.getframerate() != sample_rate:
                    _do_resample = True
        except Exception as e:
            log(f"  WAV 读取失败: {wav_path}: {e}")
            prev_pause_samples = int(seg_pause_ms * sample_rate / 1000)
            continue

        tts_rate_offset = seg.get("tts_rate_offset", 0.0)
        tts_dur = seg.get("audio_duration", 0)

        narration_delay = CONFIG.get("narration_delay_seconds", 2.0)
        start_sample = int((seg["start"] + narration_delay) * sample_rate)
        end_boundary = int(min(seg["end"], video_duration) * sample_rate)

        # 段间间隔：使用前一段的 pause_after_ms（来自 LLM）
        # 第一段无延迟，后续段在前段结束后等待前段的 pause
        min_start_with_pause = last_written_end + prev_pause_samples
        actual_start = max(start_sample, min_start_with_pause)
        actual_start = min(actual_start, end_boundary)  # 不超出 slot 边界

        # 根据实际可用空间决定是否加速
        available_samples = end_boundary - actual_start
        available_duration = max(available_samples / sample_rate, 0)
        if tts_dur > available_duration > 0:
            wav_path, actual_dur = _adjust_tts_speed(wav_path, available_duration, work_dir, tts_rate_offset)
        else:
            actual_dur = tts_dur

        # _adjust_tts_speed 输出固定 44100Hz mono 16bit，若文件被替换则无需 resample
        if wav_path != original_wav_path:
            _do_resample = False
        if _do_resample:
            tmp_path = str(Path(work_dir) / f"_rs_{seg.get('index', 0)}.wav")
            run_cmd(["ffmpeg", "-y", "-i", wav_path,
                     "-ar", str(sample_rate), "-ac", "1",
                     "-acodec", "pcm_s16le", tmp_path])
            wav_path = tmp_path

        with wave.open(wav_path, "rb") as wf:
            wf_data = bytearray(wf.readframes(wf.getnframes()))

        # 按场景边界裁剪
        audio_samples = len(wf_data) // 2
        available = end_boundary - actual_start
        write_samples = min(audio_samples, max(available, 0))

        if write_samples <= 0:
            log(f"  跳过: {seg['start']:.1f}s-{seg['end']:.1f}s (无空间)")
            seg["actual_place_start"] = seg["start"]
            seg["actual_place_end"] = seg["start"]
            prev_pause_samples = int(seg_pause_ms * sample_rate / 1000)
            continue

        # 裁剪到写入长度
        wf_data = wf_data[:write_samples * 2]

        # 重叠检测：跳过与前段重叠的部分（在 fade 之前，避免截断后丢失 fade-in）
        if actual_start < last_written_end:
            overlap_ms = (last_written_end - actual_start) * 1000 / sample_rate
            if last_written_end >= actual_start + write_samples:
                log(f"  跳过重叠段: {actual_start/sample_rate:.1f}s "
                    f"(与前段重叠 {overlap_ms:.0f}ms)")
                seg["actual_place_start"] = seg["start"]
                seg["actual_place_end"] = seg["start"]
                prev_pause_samples = int(seg_pause_ms * sample_rate / 1000)
                continue
            log(f"  重叠 {overlap_ms:.0f}ms，截断前部")
            skip_samples = last_written_end - actual_start
            wf_data = wf_data[skip_samples * 2:]
            write_samples -= skip_samples
            actual_start = last_written_end

        # fade-in / fade-out（在 overlap 裁剪之后应用，确保正确的音频包络）
        fade_len = min(int(CONFIG.get("fade_ms", 50) * sample_rate / 1000), write_samples // 4)
        for i in range(fade_len):
            gain = i / fade_len
            s = i * 2
            sample = int.from_bytes(wf_data[s:s+2], 'little', signed=True)
            sample = int(sample * gain)
            wf_data[s:s+2] = sample.to_bytes(2, 'little', signed=True)
        for i in range(fade_len):
            gain = 1.0 - i / fade_len
            s = (write_samples - 1 - i) * 2
            if s < 0:
                break
            sample = int.from_bytes(wf_data[s:s+2], 'little', signed=True)
            sample = int(sample * gain)
            wf_data[s:s+2] = sample.to_bytes(2, 'little', signed=True)

        buffer[actual_start * 2: actual_start * 2 + write_samples * 2] = wf_data
        seg["actual_place_start"] = actual_start / sample_rate
        seg["actual_place_end"] = (actual_start + write_samples) / sample_rate
        last_written_end = actual_start + write_samples
        prev_pause_samples = int(seg_pause_ms * sample_rate / 1000)

    with wave.open(str(output_wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(buffer))

    log(f"解说音轨: {video_duration:.1f}s, {len(tts_segments)} 段")


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
                        default=None, help="音频 ducking 模式 (默认: sidechaincompress)")
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
    if args.scene_threshold:
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
