import base64
import json
import os
import re
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

from lib import CONFIG
from lib import log, mimo_tts_api_call, get_video_duration, narration_tempo_budget, run_cmd
from lib import _truncate_at_sentence, _text_char_count, stable_hash, file_fingerprint

SUPPORTED_TTS_ENGINES = {"mimo-tts"}
SEGMENT_AUDIO_SCHEMA_VERSION = 1
TTS_CACHE_VERSION = 2
VOICE_REFERENCE_PREP_VERSION = 1
_VOICE_REFERENCE_LOCK = Lock()


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
    prepared = _prepare_tts_segment(i, seg, narration, tts_dir, engine)
    if prepared is None:
        return None
    text, output_wav, rate, pitch, cache_key = prepared
    cached = _reuse_tts_segment_cache(i, seg, output_wav, text, rate, cache_key)
    if cached:
        return cached

    _run_tts_engine(engine, text, output_wav, rate=rate, pitch=pitch, emotion=seg.get("emotion"))

    dur = _get_audio_duration(output_wav)
    seg_slot = seg["end"] - seg["start"]
    seg_pause = seg.get("pause_after_ms", CONFIG.get("breath_ms", 250)) / 1000
    available = max(0.5, seg_slot - seg_pause)
    rate_offset = _parse_rate_offset(rate)
    budget = narration_tempo_budget(rate_offset)
    raw_budget = available * budget["max_raw_duration_factor"]
    truncated = False
    truncate_reason = "none"
    if dur > raw_budget and len(text) > 5:
        chars_per_sec = _text_char_count(text) / dur if dur > 0 else 3.0
        target_chars = max(5, int(raw_budget * chars_per_sec) - 1)
        shortened = _truncate_at_sentence(text, target_chars)
        if shortened and len(shortened) >= 5 and shortened != text:
            log(f"  段 {i+1}: 解说超出片段时长，按累计语速预算句界缩短 {len(text)}→{len(shortened)} 字以适配（建议在解说里改写得更短）")
            text = shortened
            truncated = True
            truncate_reason = "sentence_boundary"
            _run_tts_engine(engine, text, output_wav, rate=rate, pitch=pitch, emotion=seg.get("emotion"))
            dur = _get_audio_duration(output_wav)

    norm_meta = _maybe_normalize_tts_wav(output_wav)
    if norm_meta:
        dur = _get_audio_duration(output_wav)
    _write_tts_segment_cache(output_wav, cache_key, text, dur, rate_offset,
                             truncated, truncate_reason, norm_meta)
    result = _build_tts_segment_result(
        i, seg, text, output_wav, dur, rate_offset, truncated, truncate_reason, norm_meta)
    return result


def _build_tts_segment_result(index, seg, text, output_wav, duration, rate_offset,
                              truncated=False, truncate_reason="none", norm_meta=None):
    budget = narration_tempo_budget(rate_offset)
    authored_text = _clean_narration_text(str(seg.get("narration", text)))
    resolved_truncated = bool(truncated) or (text != authored_text)
    result = {
        "segment_audio_schema_version": SEGMENT_AUDIO_SCHEMA_VERSION,
        "index": index,
        "start": seg["start"],
        "end": seg["end"],
        "narration": authored_text,
        "spoken_text": text,
        "truncated": resolved_truncated,
        "truncate_reason": (truncate_reason if truncate_reason != "none" else "sentence_boundary") if resolved_truncated else "none",
        "fit_status": "pending_assembly",
        "audio_path": str(output_wav),
        "audio_duration": duration,
        "placed_audio_duration": None,
        "actual_place_start": None,
        "actual_place_end": None,
        "global_narration_speed": budget["global_narration_speed"],
        "segment_tempo_factor": 1.0,
        "effective_tempo": budget["global_narration_speed"] * budget["tts_rate_factor"],
        "rms_dbfs_before": (norm_meta or {}).get("rms_dbfs_before"),
        "rms_dbfs_after": (norm_meta or {}).get("rms_dbfs_after"),
        "peak_after": (norm_meta or {}).get("peak_after"),
        "tts_rate_offset": rate_offset,
        "pause_after_ms": seg.get("pause_after_ms", CONFIG.get("breath_ms", 250)),
        "overlaps_speech": seg.get("overlaps_speech", True),
    }
    for optional_key in ("source_start", "source_end", "source_clip_id", "emotion"):
        if optional_key in seg:
            result[optional_key] = seg[optional_key]
    return result


def _tts_failure_record(index, seg, error):
    """Build a user-visible failure record for partial TTS output."""
    return {
        "index": int(index),
        "start": seg.get("start"),
        "end": seg.get("end"),
        "text": _clean_narration_text(str(seg.get("narration", ""))),
        "error": str(error),
    }


def _build_tts_meta(segments, engine, narration_name, failures=None):
    """Stable tts_meta.json payload, including partial-failure visibility."""
    failures = list(failures or [])
    return {
        "segments": segments,
        "engine": engine,
        "narration": narration_name,
        "partial": bool(failures),
        "failures": failures,
    }


def synthesize_tts(narration, work_dir):
    """合成解说音频（并行）"""
    synthesize_tts.last_failures = []
    voice_ref = str(CONFIG.get("voice_ref") or "").strip()
    if voice_ref:
        # Prepared bytes are invocation-scoped. Re-hash the live source once for the cache-only
        # probe; if fresh synthesis is needed, a stable snapshot below becomes the one identity
        # used by both the API request and the final segment cache keys.
        for key in (
            "voice_ref_b64",
            "voice_ref_snapshot_path",
            "voice_ref_source_signature",
            "voice_ref_fingerprint",
        ):
            CONFIG.pop(key, None)
    tts_dir = work_dir / "tts_segments"
    tts_dir.mkdir(exist_ok=True)

    if not narration:
        raise RuntimeError("narration.json 没有可配音的解说段，已中止以避免生成无解说视频")

    cache_engine = "mimo-tts"
    cached_segments = []
    needs_fresh = False
    prepared_count = 0
    for i, seg in enumerate(narration):
        prepared = _prepare_tts_segment(i, seg, narration, tts_dir, cache_engine)
        if prepared is None:
            continue
        prepared_count += 1
        text, output_wav, rate, _pitch, cache_key = prepared
        cached = _reuse_tts_segment_cache(i, seg, output_wav, text, rate, cache_key)
        if not cached:
            needs_fresh = True
            break
        cached_segments.append(cached)
    if prepared_count == 0:
        raise RuntimeError("narration.json 没有可配音的有效文本，已中止以避免生成无解说视频")
    if not needs_fresh:
        cached_segments.sort(key=lambda x: x["index"])
        log(f"TTS 引擎: {cache_engine} (cache)")
        synthesize_tts.last_failures = []
        return cached_segments, cache_engine

    engine = resolve_tts_engine()
    if engine == "mimo-tts" and not CONFIG.get("mimo_tts_api_key"):
        key_name = CONFIG.get("mimo_tts_api_key_source", "MIMO_API_KEY")
        raise RuntimeError(f"请设置 {key_name} 环境变量用于 MiMo TTS")

    if voice_ref:
        _cache_prepared_voice_reference(voice_ref)

    log(f"TTS 引擎: {engine}")

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
                failures.append(_tts_failure_record(i, narration[i], e))
                log(f"  TTS 段 {i+1} 失败: {e}")
                continue
            if result:
                segments.append(result)
                log(f"  段 {result['index']+1}: {result['audio_duration']:.1f}s - {result['narration'][:25]}...")

    segments.sort(key=lambda x: x["index"])
    if failures and not CONFIG.get("allow_partial_tts", False):
        sample = "; ".join(f"段 {f['index']+1}: {f['error']}" for f in failures[:3])
        raise RuntimeError(
            f"TTS 失败 {len(failures)}/{len(narration)} 段，已中止以避免生成缺解说的视频。"
            f"示例: {sample}。如确需继续，可设置 ALLOW_PARTIAL_TTS=1 或 --allow-partial-tts。"
        )
    synthesize_tts.last_failures = failures
    if failures:
        missing = ", ".join(str(f["index"] + 1) for f in failures[:8])
        more = "…" if len(failures) > 8 else ""
        log(
            f"警告: TTS 部分失败 {len(failures)}/{len(narration)} 段（段 {missing}{more}），"
            "成片可预览但不建议直接发布；详见 tts_meta.json failures"
        )
    if not segments:
        raise RuntimeError("TTS 没有生成任何有效解说音频，已中止以避免生成无解说视频")
    return segments, engine


def _run_tts_engine(engine, text, output_wav, rate="+0%", pitch="+0Hz", emotion=None):
    """Run one TTS engine with retry and remove partial files after failures."""
    retries = max(1, CONFIG.get("tts_retries", 3))
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            _cleanup_partial_tts_outputs(output_wav)
            if engine == "mimo-tts":
                _tts_mimo(text, output_wav, rate=rate, pitch=pitch, emotion=emotion)
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
    wav_path = Path(output_wav)
    mp3_path = wav_path.with_suffix(".mp3")
    cache_path = str(_tts_segment_cache_path(output_wav))
    for path in (str(wav_path), str(mp3_path), cache_path):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def _normalize_tts_wav_rms(input_wav, output_wav, *, target_rms_dbfs=-20.0, peak_limit=0.98):
    """Normalize a mono/stereo 16-bit WAV to a target RMS with peak guard.

    This helper is intentionally dependency-free so QC/assembly can reuse the
    returned metadata even when normalization is applied in a later lane.
    """
    import math
    import wave

    input_wav = Path(input_wav)
    output_wav = Path(output_wav)
    with wave.open(str(input_wav), "rb") as wf:
        channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        frames = wf.getnframes()
        data = wf.readframes(frames)
    if sampwidth != 2:
        raise ValueError(f"仅支持 16-bit PCM WAV: {input_wav}")

    samples = [int.from_bytes(data[i:i + 2], "little", signed=True) for i in range(0, len(data), 2)]
    if not samples:
        output_wav.write_bytes(input_wav.read_bytes())
        return {
            "rms_dbfs_before": None,
            "rms_dbfs_after": None,
            "peak_after": 0.0,
            "gain_db": 0.0,
        }
    rms = math.sqrt(sum(s * s for s in samples) / len(samples))
    peak = max(abs(s) for s in samples) / 32768.0
    rms_dbfs_before = 20 * math.log10(max(rms, 1e-9) / 32768.0)
    target_linear = 10 ** (float(target_rms_dbfs) / 20.0) * 32768.0
    gain = target_linear / max(rms, 1e-9)
    if peak > 0:
        gain = min(gain, float(peak_limit) / peak)
    normalized = []
    for sample in samples:
        value = int(round(sample * gain))
        value = max(-32768, min(32767, value))
        normalized.append(value.to_bytes(2, "little", signed=True))
    out_data = b"".join(normalized)
    with wave.open(str(output_wav), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(framerate)
        wf.writeframes(out_data)

    out_samples = [int.from_bytes(out_data[i:i + 2], "little", signed=True) for i in range(0, len(out_data), 2)]
    out_rms = math.sqrt(sum(s * s for s in out_samples) / len(out_samples))
    out_peak = max(abs(s) for s in out_samples) / 32768.0
    return {
        "rms_dbfs_before": rms_dbfs_before,
        "rms_dbfs_after": 20 * math.log10(max(out_rms, 1e-9) / 32768.0),
        "peak_after": out_peak,
        "gain_db": 20 * math.log10(max(gain, 1e-9)),
    }


def _maybe_normalize_tts_wav(output_wav):
    """Normalize a synthesized TTS block in-place when possible.

    Unit tests often stub TTS with text bytes rather than real WAV. In that case
    normalization is skipped safely; real MiMo WAV output gets RMS/peak metadata.
    """
    if not CONFIG.get("tts_segment_normalize", True):
        return None
    output_wav = Path(output_wav)
    tmp = output_wav.with_name(f"{output_wav.stem}_norm{output_wav.suffix}")
    try:
        meta = _normalize_tts_wav_rms(
            output_wav,
            tmp,
            target_rms_dbfs=float(CONFIG.get("tts_segment_target_rms_dbfs", -20.0) or -20.0),
            peak_limit=float(CONFIG.get("tts_segment_peak_limit", 0.98) or 0.98),
        )
        os.replace(tmp, output_wav)
        return meta
    except Exception as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        log(f"  TTS RMS 归一跳过: {exc}")
        return None


def _tts_segment_cache_path(output_wav):
    return Path(str(output_wav) + ".cache.json")


def _prepare_tts_segment(index, seg, narration, tts_dir, engine):
    text = _clean_narration_text(seg["narration"])
    if not text or not text.strip():
        return None
    output_wav = tts_dir / f"narr_{index:03d}.wav"
    if CONFIG.get("tts_dynamic_params", True):
        rate, pitch = _compute_tts_params(text, narration, index)
    else:
        rate, pitch = "+0%", "+0Hz"
    cache_key = _tts_segment_cache_key(engine, index, seg, text, rate, pitch)
    return text, output_wav, rate, pitch, cache_key


def _reuse_tts_segment_cache(index, seg, output_wav, source_text, rate, cache_key):
    cached = _load_tts_segment_cache(output_wav, cache_key)
    if not cached:
        return None
    existing_dur = _get_audio_duration(output_wav)
    if existing_dur <= 0:
        return None
    spoken_text = str(cached.get("spoken_text") or source_text)
    rate_offset = float(cached.get("tts_rate_offset", _parse_rate_offset(rate)) or 0.0)
    truncated = bool(cached.get("truncated", False))
    truncate_reason = str(cached.get("truncate_reason") or ("sentence_boundary" if truncated else "none"))
    norm_meta = cached.get("normalization") if isinstance(cached.get("normalization"), dict) else None
    log(f"  段 {index+1}: 复用已有 ({existing_dur:.1f}s)")
    return _build_tts_segment_result(index, seg, spoken_text, output_wav, existing_dur,
                                     rate_offset, truncated, truncate_reason, norm_meta)


def _tts_segment_cache_key(engine, index, seg, source_text, rate, pitch):
    """Fingerprint the exact inputs that make a cached segment safe to reuse."""
    pause = seg.get("pause_after_ms", CONFIG.get("breath_ms", 250))
    try:
        pause = int(pause)
    except (TypeError, ValueError):
        pause = CONFIG.get("breath_ms", 250)
    payload = {
        "version": TTS_CACHE_VERSION,
        "engine": engine,
        "source_text": source_text,
        "segment_index": int(index),
        "start": round(float(seg.get("start", 0.0)), 3),
        "end": round(float(seg.get("end", 0.0)), 3),
        "pause_after_ms": pause,
        "rate": rate,
        "pitch": pitch,
        "emotion": (str(seg.get("emotion")).strip() if seg.get("emotion") else ""),
        "settings": tts_settings_fingerprint(engine),
    }
    return stable_hash(payload)


def _load_tts_segment_cache(output_wav, cache_key):
    """Return cache metadata only when the sidecar proves the WAV matches narration."""
    if not output_wav.exists():
        return None
    cache_path = _tts_segment_cache_path(output_wav)
    if not cache_path.exists():
        return None
    try:
        import json
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("cache_key") != cache_key:
        return None
    try:
        if data.get("audio_fingerprint") != file_fingerprint(output_wav):
            return None
    except OSError:
        return None
    return data


def _write_tts_segment_cache(output_wav, cache_key, spoken_text, duration, rate_offset,
                             truncated=False, truncate_reason="none", norm_meta=None):
    """Persist non-secret provenance for safe per-segment TTS reuse."""
    try:
        import json
        _tts_segment_cache_path(output_wav).write_text(
            json.dumps({
                "version": TTS_CACHE_VERSION,
                "cache_key": cache_key,
                "audio_fingerprint": file_fingerprint(output_wav),
                "spoken_text": spoken_text,
                "audio_duration": duration,
                "tts_rate_offset": rate_offset,
                "truncated": bool(truncated),
                "truncate_reason": truncate_reason if truncated else "none",
                "normalization": norm_meta or None,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        log(f"  TTS 缓存元数据写入失败（忽略）: {exc}")


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
    settings = {
        "engine": resolved,
        "tts_dynamic_params": bool(CONFIG.get("tts_dynamic_params", True)),
        "mimo_tts_api_url": CONFIG.get("mimo_tts_api_url"),
        "mimo_tts_model": CONFIG.get("mimo_tts_model"),
        "mimo_tts_voice": CONFIG.get("mimo_tts_voice"),
        "mimo_tts_style": CONFIG.get("mimo_tts_style"),
        "narration_speed": float(CONFIG.get("narration_speed", 1.0) or 1.0),
        "narration_cumulative_tempo_max": float(CONFIG.get("narration_cumulative_tempo_max", 1.35) or 1.35),
        "narration_cumulative_tempo_hard_max": float(CONFIG.get("narration_cumulative_tempo_hard_max", 1.40) or 1.40),
        "tts_segment_tempo_max": float(CONFIG.get("tts_segment_tempo_max", 1.20) or 1.20),
        "tts_segment_normalize": bool(CONFIG.get("tts_segment_normalize", True)),
        "tts_segment_target_rms_dbfs": float(CONFIG.get("tts_segment_target_rms_dbfs", -20.0) or -20.0),
        "tts_segment_peak_limit": float(CONFIG.get("tts_segment_peak_limit", 0.98) or 0.98),
    }
    voice_ref = str(CONFIG.get("voice_ref") or "").strip()
    if voice_ref:
        ref_path = Path(voice_ref).expanduser()
        settings.pop("mimo_tts_voice", None)  # ignored by the voiceclone API
        settings["voice_ref_fingerprint"] = _voice_reference_fingerprint(ref_path)
        settings["voice_ref_preparation"] = (
            f"pcm_s16le:24000hz:mono:30s:v{VOICE_REFERENCE_PREP_VERSION}"
        )
        settings["mimo_tts_model"] = "mimo-v2.5-tts-voiceclone"
    return settings


def _mimo_tts_style_instruction(rate="+0%", pitch="+0Hz", emotion=None):
    style = CONFIG.get("mimo_tts_style") or "自然、清晰、适合中文视频解说。"
    emo = str(emotion).strip() if emotion else ""
    if emo:
        tone = f"用「{emo}」的情绪和语气演绎这句解说，代入感强、有起伏，不要平铺直叙。"
    else:
        tone = "语气有感染力、有起伏，像在给观众讲故事，不要平淡机械。"
    rate_offset = _parse_rate_offset(rate)
    if rate_offset >= 0.06:
        speed = "语速略快，但吐字保持清楚。"
    elif rate_offset <= -0.03:
        speed = "语速略慢，适当停顿，保留收束感。"
    else:
        speed = "语速中等，节奏稳定。"
    pitch_hint = "疑问句或情绪抬升处可自然微升调。" if pitch and pitch != "+0Hz" else "音调自然。"
    return f"{style} {tone} {speed} {pitch_hint}"


def _prepare_voice_reference(ref_path):
    """Normalize arbitrary reference audio to MiMo voiceclone's 24 kHz mono WAV."""
    ref = Path(ref_path).expanduser()
    if not ref.is_file():
        raise FileNotFoundError(f"参考音频不存在或不是文件: {ref}")
    with tempfile.TemporaryDirectory(prefix="video-recap-voice-ref-") as temp_dir:
        normalized = Path(temp_dir) / "voice_ref.wav"
        result = run_cmd([
            "ffmpeg", "-y", "-i", str(ref), "-vn", "-ar", "24000", "-ac", "1",
            "-t", "30", "-acodec", "pcm_s16le", str(normalized),
        ])
        if result.returncode != 0 or not normalized.is_file() or normalized.stat().st_size <= 44:
            detail = (result.stderr or "").strip()
            raise RuntimeError(f"参考音频转码失败: {detail or ref}")
        return base64.b64encode(normalized.read_bytes()).decode("ascii")


def _voice_reference_signature(ref_path):
    """Cheaply identify the prepared source so a reused process cannot serve stale audio."""
    ref = Path(ref_path).expanduser().resolve()
    stat = ref.stat()
    return f"{ref}:{stat.st_size}:{stat.st_mtime_ns}"


def _voice_reference_fingerprint(ref_path):
    ref = Path(ref_path).expanduser()
    if not ref.is_file():
        return f"missing:{ref.resolve()}"
    resolved = str(ref.resolve())
    if (
        CONFIG.get("voice_ref_b64")
        and CONFIG.get("voice_ref_snapshot_path") == resolved
        and CONFIG.get("voice_ref_fingerprint")
    ):
        return CONFIG["voice_ref_fingerprint"]
    signature = _voice_reference_signature(ref)
    if (
        CONFIG.get("voice_ref_source_signature") == signature
        and CONFIG.get("voice_ref_fingerprint")
    ):
        return CONFIG["voice_ref_fingerprint"]
    fingerprint = file_fingerprint(ref)
    CONFIG["voice_ref_source_signature"] = signature
    CONFIG["voice_ref_fingerprint"] = fingerprint
    return fingerprint


def _cache_prepared_voice_reference(ref_path):
    with _VOICE_REFERENCE_LOCK:
        ref = Path(ref_path).expanduser().resolve()
        signature = _voice_reference_signature(ref)
        if (
            CONFIG.get("voice_ref_b64")
            and CONFIG.get("voice_ref_snapshot_path") == str(ref)
        ):
            return CONFIG["voice_ref_b64"]
        if not ref.is_file():
            raise FileNotFoundError(f"参考音频不存在或不是文件: {ref}")
        # ffmpeg and the cache fingerprint must consume the same immutable bytes. Copy first,
        # then derive both the normalized WAV and identity from that snapshot rather than reading
        # a caller-mutable source twice.
        with tempfile.TemporaryDirectory(prefix="video-recap-voice-ref-snapshot-") as temp_dir:
            snapshot = Path(temp_dir) / f"source{ref.suffix or '.audio'}"
            shutil.copyfile(ref, snapshot)
            fingerprint = file_fingerprint(snapshot)
            encoded = _prepare_voice_reference(snapshot)
        CONFIG["voice_ref_b64"] = encoded
        CONFIG["voice_ref_snapshot_path"] = str(ref)
        CONFIG["voice_ref_source_signature"] = signature
        CONFIG["voice_ref_fingerprint"] = fingerprint
        return encoded


def _tts_mimo(text, output_path, rate="+0%", pitch="+0Hz", emotion=None):
    """使用 Xiaomi MiMo-V2.5-TTS 合成，按需用参考音频克隆音色。

    MiMo-v2.5-tts 是 instruct-TTS：user 消息里的自然语言指令控制整句的情绪/语气/语速。
    每段 narration 的 `emotion` 标签即写进该指令，让解说有起伏、不机械。"""
    voice_ref = str(CONFIG.get("voice_ref") or "").strip()
    voice_ref_b64 = CONFIG.get("voice_ref_b64")
    if voice_ref:
        resolved = str(Path(voice_ref).expanduser().resolve())
        snapshot_path = CONFIG.get("voice_ref_snapshot_path")
        cached_signature = CONFIG.get("voice_ref_source_signature")
        legacy_cache_valid = (
            voice_ref_b64
            and not snapshot_path
            and cached_signature == _voice_reference_signature(voice_ref)
        )
        if not voice_ref_b64 or not (
            snapshot_path == resolved or legacy_cache_valid
        ):
            voice_ref_b64 = _cache_prepared_voice_reference(voice_ref)
    payload = {
        "model": "mimo-v2.5-tts-voiceclone" if voice_ref else CONFIG.get("mimo_tts_model", "mimo-v2.5-tts"),
        "messages": [
            {"role": "user", "content": _mimo_tts_style_instruction(rate, pitch, emotion)},
            {"role": "assistant", "content": text},
        ],
        "audio": {
            "format": "wav",
            "voice": (
                f"data:audio/wav;base64,{voice_ref_b64}"
                if voice_ref else CONFIG.get("mimo_tts_voice", "mimo_default")
            ),
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
    from pathlib import Path
    ap = argparse.ArgumentParser(
        description="video-voiceover: synthesize narration audio segments from narration.json.")
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--narration", default=None,
                    help="narration json (default: <work-dir>/narration.json; pass narration_mapped.json explicitly for legacy cut runs)")
    ap.add_argument("--mimo-voice", default=None, help="MiMo TTS voice name")
    ap.add_argument("--voice-ref", default=None,
                    help="reference audio (wav/mp3/etc.) for mimo-v2.5-tts-voiceclone")
    ap.add_argument("--allow-partial-tts", action="store_true",
                    help="allow output when some narration segments fail TTS")
    args = ap.parse_args()
    work_dir = Path(args.work_dir)
    effective_voice_ref = (
        args.voice_ref if args.voice_ref is not None else os.environ.get("VOICE_REF", "").strip()
    )
    CONFIG["voice_ref"] = effective_voice_ref
    for key in (
        "voice_ref_b64",
        "voice_ref_snapshot_path",
        "voice_ref_fingerprint",
        "voice_ref_source_signature",
    ):
        CONFIG.pop(key, None)
    if args.mimo_voice:
        CONFIG["mimo_tts_voice"] = args.mimo_voice
    if args.mimo_voice and CONFIG.get("voice_ref"):
        ap.error("--mimo-voice and --voice-ref are mutually exclusive")
    # Voice-reference normalization is intentionally lazy: a fully cached rerun should not
    # invoke ffmpeg. _tts_mimo uses a process-wide lock so a fresh parallel run still converts
    # the reference exactly once.
    if args.allow_partial_tts:
        CONFIG["allow_partial_tts"] = True
    if args.narration:
        narration_path = Path(args.narration)
    else:
        # Canonical cut mode is cut-first/narrate-second: narration.json is already on the
        # output timeline. A stale legacy narration_mapped.json in the same work_dir must not
        # silently override it; legacy direct-cut callers can still pass --narration explicitly.
        narration_path = work_dir / "narration.json"
    narration = json.loads(narration_path.read_text(encoding="utf-8"))
    tts_segments, engine_used = synthesize_tts(narration, work_dir)
    failures = getattr(synthesize_tts, "last_failures", [])
    meta = _build_tts_meta(tts_segments, engine_used, narration_path.name, failures)
    (work_dir / "tts_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    if failures:
        log(f"配音完成但缺 {len(failures)} 段：成片可预览但不建议直接发布")
    log(f"配音完成: {len(tts_segments)} 段, 引擎 {engine_used}")
    print(json.dumps({"status": "voiced", "segments": len(tts_segments), "engine": engine_used,
                      "partial": bool(failures), "failures": len(failures),
                      "tts_meta": str(work_dir / "tts_meta.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
