import base64
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from lib import CONFIG
from lib import log, mimo_tts_api_call, get_video_duration
from lib import _truncate_at_sentence

SUPPORTED_TTS_ENGINES = {"mimo-tts"}


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
            return _build_tts_segment_result(i, seg, text, output_wav, existing_dur, 0.0)

    if CONFIG.get("tts_dynamic_params", True):
        rate, pitch = _compute_tts_params(text, narration, i)
    else:
        rate, pitch = "+0%", "+0Hz"

    _run_tts_engine(engine, text, output_wav, rate=rate, pitch=pitch)

    dur = _get_audio_duration(output_wav)
    seg_slot = seg["end"] - seg["start"]
    seg_pause = seg.get("pause_after_ms", CONFIG.get("breath_ms", 250)) / 1000
    available = max(0.5, seg_slot - seg_pause)
    atempo_max = 1.2
    if dur > available * atempo_max and len(text) > 5:
        chars_per_sec = len(text) / dur if dur > 0 else 3.0
        target_chars = max(5, int(available * atempo_max * chars_per_sec) - 1)
        truncated = _truncate_at_sentence(text, target_chars)
        if truncated and len(truncated) >= 5 and truncated != text:
            text = truncated
            _run_tts_engine(engine, text, output_wav, rate=rate, pitch=pitch)
            dur = _get_audio_duration(output_wav)

    return _build_tts_segment_result(i, seg, text, output_wav, dur, _parse_rate_offset(rate))


def _build_tts_segment_result(index, seg, text, output_wav, duration, rate_offset):
    result = {
        "index": index,
        "start": seg["start"],
        "end": seg["end"],
        "narration": text,
        "audio_path": str(output_wav),
        "audio_duration": duration,
        "tts_rate_offset": rate_offset,
        "pause_after_ms": seg.get("pause_after_ms", CONFIG.get("breath_ms", 250)),
        "overlaps_speech": seg.get("overlaps_speech", True),
    }
    for optional_key in ("source_start", "source_end", "source_clip_id"):
        if optional_key in seg:
            result[optional_key] = seg[optional_key]
    return result


def synthesize_tts(narration, work_dir):
    """合成解说音频（并行）"""
    tts_dir = work_dir / "tts_segments"
    tts_dir.mkdir(exist_ok=True)

    engine = resolve_tts_engine()
    if engine == "mimo-tts" and not CONFIG.get("mimo_tts_api_key"):
        key_name = CONFIG.get("mimo_tts_api_key_source", "MIMO_API_KEY")
        raise RuntimeError(f"请设置 {key_name} 环境变量用于 MiMo TTS")

    log(f"TTS 引擎: {engine}")
    if not narration:
        return [], engine

    segments = []
    failures = []
    max_workers = max(1, min(len(narration), CONFIG.get("tts_workers", 4)))

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_synthesize_segment, i, seg, narration, tts_dir, engine): i
            for i, seg in enumerate(narration)
        }
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as e:
                i = futures[future]
                failures.append((i, str(e)))
                log(f"  TTS 段 {i+1} 失败: {e}")
                continue
            if result:
                segments.append(result)
                log(f"  段 {result['index']+1}: {result['audio_duration']:.1f}s - {result['narration'][:25]}...")

    segments.sort(key=lambda x: x["index"])
    if failures and not CONFIG.get("allow_partial_tts", False):
        sample = "; ".join(f"段 {i+1}: {msg}" for i, msg in failures[:3])
        raise RuntimeError(
            f"TTS 失败 {len(failures)}/{len(narration)} 段，已中止以避免生成缺解说的视频。"
            f"示例: {sample}。如确需继续，可设置 ALLOW_PARTIAL_TTS=1 或 --allow-partial-tts。"
        )
    if failures:
        log(f"警告: TTS 部分失败 {len(failures)}/{len(narration)} 段，继续生成部分解说")
    return segments, engine


def _run_tts_engine(engine, text, output_wav, rate="+0%", pitch="+0Hz"):
    """Run one TTS engine with retry and remove partial files after failures."""
    retries = max(1, CONFIG.get("tts_retries", 3))
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            _cleanup_partial_tts_outputs(output_wav)
            if engine == "mimo-tts":
                _tts_mimo(text, output_wav, rate=rate, pitch=pitch)
            else:
                raise RuntimeError(
                    f"不支持的 TTS 引擎: {engine}。当前仅支持 mimo-tts。"
                )

            dur = _get_audio_duration(output_wav)
            if dur <= 0:
                raise RuntimeError(f"{engine} 输出音频时长无效")
            return
        except Exception as exc:
            last_error = exc
            _cleanup_partial_tts_outputs(output_wav)
            if attempt < retries:
                wait = min(2 ** (attempt - 1), 8)
                log(f"  TTS 重试 {attempt+1}/{retries}: {exc}，等待 {wait}s")
                time.sleep(wait)

    raise RuntimeError(f"{engine} 合成失败: {last_error}") from last_error


def _cleanup_partial_tts_outputs(output_wav):
    """Remove stale partial media files before/after a failed TTS attempt."""
    wav_path = str(output_wav)
    mp3_path = wav_path.replace(".wav", ".mp3")
    for path in (wav_path, mp3_path):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def _detect_tts_engine():
    """MiMo TTS is the only engine; require a MiMo key."""
    if CONFIG.get("mimo_tts_api_key"):
        return "mimo-tts"
    key_name = CONFIG.get("mimo_tts_api_key_source", "MIMO_API_KEY")
    raise RuntimeError(f"没有可用的 TTS 引擎：请设置 {key_name}（MiMo TTS 需要）。")


def resolve_tts_engine(prefer_existing=None):
    """Resolve the TTS engine. MiMo TTS (mimo-v2.5-tts) is the only engine.

    `prefer_existing` lets an assemble-only rerun reuse already-generated audio
    even when no fresh MiMo key is configured.
    """
    try:
        return _detect_tts_engine()
    except RuntimeError:
        if prefer_existing in SUPPORTED_TTS_ENGINES:
            return prefer_existing
        raise


def tts_settings_fingerprint(engine=None):
    """Return non-secret TTS settings that materially affect generated audio."""
    resolved = engine or resolve_tts_engine()
    return {
        "engine": resolved,
        "tts_dynamic_params": bool(CONFIG.get("tts_dynamic_params", True)),
        "mimo_tts_api_url": CONFIG.get("mimo_tts_api_url"),
        "mimo_tts_model": CONFIG.get("mimo_tts_model"),
        "mimo_tts_voice": CONFIG.get("mimo_tts_voice"),
        "mimo_tts_style": CONFIG.get("mimo_tts_style"),
    }


def _mimo_tts_style_instruction(rate="+0%", pitch="+0Hz"):
    style = CONFIG.get("mimo_tts_style") or "自然、清晰、适合中文视频解说。"
    rate_offset = _parse_rate_offset(rate)
    if rate_offset >= 0.06:
        speed = "语速略快，但吐字保持清楚。"
    elif rate_offset <= -0.03:
        speed = "语速略慢，适当停顿，保留收束感。"
    else:
        speed = "语速中等，节奏稳定。"
    pitch_hint = "疑问句或情绪抬升处可自然微升调。" if pitch and pitch != "+0Hz" else "音调自然。"
    return f"{style} {speed} {pitch_hint}"


def _tts_mimo(text, output_path, rate="+0%", pitch="+0Hz"):
    """使用 Xiaomi MiMo-V2.5-TTS 合成，返回 wav 音频。"""
    payload = {
        "model": CONFIG.get("mimo_tts_model", "mimo-v2.5-tts"),
        "messages": [
            {"role": "user", "content": _mimo_tts_style_instruction(rate, pitch)},
            {"role": "assistant", "content": text},
        ],
        "audio": {
            "format": "wav",
            "voice": CONFIG.get("mimo_tts_voice", "mimo_default"),
        },
    }
    resp = mimo_tts_api_call(payload)
    try:
        audio_data = resp["choices"][0]["message"]["audio"]["data"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("MiMo-TTS 响应缺少 audio.data") from exc
    try:
        output_path.write_bytes(base64.b64decode(audio_data))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("MiMo-TTS 返回的 audio.data 不是有效 base64") from exc


def _get_audio_duration(audio_path):
    """获取音频文件时长（复用 common.get_video_duration 的 ffprobe 探测）。"""
    return get_video_duration(audio_path)


# ── Step 7: 视频组装 ─────────────────────────────────────────────────


def main():
    import argparse
    import json
    from pathlib import Path
    ap = argparse.ArgumentParser(
        description="video-voiceover: synthesize narration audio segments from narration.json.")
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--narration", default=None,
                    help="narration json (default: narration_mapped.json if present, else narration.json)")
    ap.add_argument("--mimo-voice", default=None, help="MiMo TTS voice name")
    args = ap.parse_args()
    work_dir = Path(args.work_dir)
    if args.mimo_voice:
        CONFIG["mimo_tts_voice"] = args.mimo_voice
    if args.narration:
        narration_path = Path(args.narration)
    else:
        mapped = work_dir / "narration_mapped.json"
        narration_path = mapped if mapped.exists() else work_dir / "narration.json"
    narration = json.loads(narration_path.read_text(encoding="utf-8"))
    tts_segments, engine_used = synthesize_tts(narration, work_dir)
    (work_dir / "tts_meta.json").write_text(
        json.dumps({"segments": tts_segments, "engine": engine_used, "narration": narration_path.name},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"配音完成: {len(tts_segments)} 段, 引擎 {engine_used}")
    print(json.dumps({"status": "voiced", "segments": len(tts_segments), "engine": engine_used,
                      "tts_meta": str(work_dir / "tts_meta.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
