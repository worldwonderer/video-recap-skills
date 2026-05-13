import json
import os
import re
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import CONFIG
from common import log, run_cmd

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
