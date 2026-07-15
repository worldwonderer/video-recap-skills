"""Narration tempo fitting and sample-accurate timeline WAV placement."""

import os
import wave
from pathlib import Path

from assemble_constants import SEGMENT_AUDIO_SCHEMA_VERSION
from lib import CONFIG, get_video_duration, log, narration_tempo_budget, run_cmd

def _apply_narration_speed(
    tts_segments,
    work_dir,
    *,
    command_runner=run_cmd,
    duration_probe=get_video_duration,
    logger=log,
):
    """Globally speed up narration audio via atempo (CONFIG['narration_speed']).

    MiMo TTS reads a touch slowly for short-form recaps; a 1.1-1.2x bump makes it
    snappier without the chipmunk effect. Rewrites each segment's audio_path/duration
    to the sped copy so the rest of assembly is unchanged. No-op at speed 1.0.
    """
    speed = float(CONFIG.get("narration_speed", 1.0) or 1.0)
    if abs(speed - 1.0) <= 1e-3:
        return
    factor = max(0.5, min(2.0, speed))
    done = 0
    for seg in tts_segments:
        src = seg.get("audio_path")
        if not src or not os.path.exists(src):
            continue
        seg.setdefault("segment_audio_schema_version", SEGMENT_AUDIO_SCHEMA_VERSION)
        seg.setdefault("narration", str(seg.get("narration") or seg.get("spoken_text") or ""))
        seg.setdefault("spoken_text", str(seg.get("spoken_text") or seg.get("narration") or ""))
        seg.setdefault("truncated", False)
        seg.setdefault("truncate_reason", "none")
        seg.setdefault("source_audio_duration", seg.get("audio_duration"))
        seg["global_narration_speed"] = factor
        out = str(Path(work_dir) / f"_spd_{seg.get('index', 0)}.wav")
        res = command_runner(["ffmpeg", "-y", "-i", src, "-filter:a", f"atempo={factor:.3f}",
                              "-ar", "44100", "-ac", "1", "-acodec", "pcm_s16le", out])
        if res.returncode == 0 and os.path.exists(out):
            seg["audio_path"] = out
            seg["audio_duration"] = duration_probe(out)
            seg["effective_tempo"] = (
                factor
                * (1.0 + float(seg.get("tts_rate_offset", 0.0) or 0.0))
                * float(seg.get("segment_tempo_factor", 1.0) or 1.0)
            )
            done += 1
    logger(f"解说整体提速: atempo={factor:.2f} ({done} 段)")


def _adjust_tts_speed(
    audio_path,
    target_duration,
    work_dir,
    tts_rate_offset=0.0,
    *,
    return_meta=True,
    command_runner=run_cmd,
    duration_probe=get_video_duration,
    logger=log,
):
    """Fit overlong TTS with bounded atempo; never time-trim speech in assemble.

    Assemble has no word/sentence timestamps, so if bounded atempo cannot make the
    audio fit, it returns `fit_status=no_safe_fit` and leaves the original audio
    untouched for QC to block instead of guessing a spoken_text truncation.
    """
    del work_dir, return_meta  # Retained for direct callers of the pre-v0.4 private helper.
    audio_path = Path(audio_path)
    current_dur = duration_probe(audio_path)
    budget = narration_tempo_budget(tts_rate_offset)
    meta = {
        "fit_status": "fits",
        "blocking": False,
        "tempo_factor": 1.0,
        "segment_tempo_factor": 1.0,
        "truncated": False,
        "truncate_reason": "none",
        "tts_rate_offset": float(tts_rate_offset or 0.0),
        "audio_duration": current_dur,
        "placed_audio_duration": current_dur,
        "global_narration_speed": budget["global_narration_speed"],
        "effective_tempo": budget["global_narration_speed"] * budget["tts_rate_factor"],
        "cumulative_tempo_max": budget["cumulative_tempo_max"],
        "cumulative_tempo_hard_max": budget["cumulative_tempo_hard_max"],
    }
    if current_dur <= target_duration or current_dur == 0:
        return (str(audio_path), current_dur, meta)

    ratio = current_dur / target_duration

    effective_max = budget["segment_tempo_max"]

    if ratio > effective_max:
        meta.update({
            "fit_status": "no_safe_fit",
            "blocking": True,
            "truncate_reason": "no_safe_boundary",
            "placed_audio_duration": 0.0,
            "needed_tempo_factor": ratio,
            "segment_tempo_factor": 1.0,
            "tempo_factor": 1.0,
        })
        logger(
            f"  TTS 无安全放置: {current_dur:.1f}s 需 x{ratio:.2f}，"
            f"超过段内预算 x{effective_max:.2f}（assemble 不按时间硬切）"
        )
        return (str(audio_path), current_dur, meta)

    # 温和加速。给 atempo/容器时长舍入留出 0.2% 安全余量；宁可极轻微
    # 多加速，也不能在写入时间线时裁掉最后一个音节。
    tempo = min(ratio * 1.002, effective_max)
    adjusted_path = audio_path.with_name(f"{audio_path.stem}_adj{audio_path.suffix}")
    cmd = ["ffmpeg", "-y", "-i", str(audio_path),
           "-filter:a", f"atempo={tempo:.6f}",
           "-ar", "44100", "-ac", "1", str(adjusted_path)]
    result = command_runner(cmd)
    if result.returncode == 0:
        new_dur = duration_probe(adjusted_path)
        if new_dur > target_duration + (1.0 / 44100.0):
            adjusted_path.unlink(missing_ok=True)
            meta.update({
                "fit_status": "no_safe_fit",
                "blocking": True,
                "truncate_reason": "no_safe_boundary",
                "placed_audio_duration": 0.0,
                "needed_tempo_factor": new_dur / target_duration,
            })
            logger(
                f"  TTS 加速后仍超出安全窗口 {new_dur - target_duration:.3f}s；"
                "禁止裁尾，交由 Agent 缩短/移动文本"
            )
            return (str(audio_path), current_dur, meta)
        meta.update({
            "fit_status": "tempo_adjusted",
            "tempo_factor": tempo,
            "segment_tempo_factor": tempo,
            "placed_audio_duration": new_dur,
            "effective_tempo": budget["global_narration_speed"] * budget["tts_rate_factor"] * tempo,
        })
        logger(f"  TTS 温和加速: {current_dur:.1f}s → {new_dur:.1f}s (x{tempo:.2f})")
        return (str(adjusted_path), new_dur, meta)
    meta["fit_status"] = "speed_adjust_failed"
    meta["truncate_reason"] = "resample_failed"
    return (str(audio_path), current_dur, meta)


def _edge_quiet_samples(pcm16_mono, sample_count, *, from_start, threshold=260):
    """Count near-silent PCM16 samples at one edge of a mono buffer."""
    indices = range(sample_count) if from_start else range(sample_count - 1, -1, -1)
    quiet = 0
    for index in indices:
        offset = index * 2
        value = int.from_bytes(pcm16_mono[offset:offset + 2], "little", signed=True)
        if abs(value) > threshold:
            break
        quiet += 1
    return quiet


def _speech_safe_fade_lengths(pcm16_mono, sample_count, sample_rate, configured_ms):
    """Limit fades to edge silence so first/last syllables are never attenuated.

    When TTS has no measurable edge silence, retain only a 5ms anti-click ramp.
    """
    configured = min(int(max(0.0, float(configured_ms)) * sample_rate / 1000), sample_count // 4)
    if configured <= 0:
        return 0, 0
    anti_click = min(int(0.005 * sample_rate), configured)
    leading = _edge_quiet_samples(pcm16_mono, sample_count, from_start=True)
    trailing = _edge_quiet_samples(pcm16_mono, sample_count, from_start=False)
    return min(configured, max(anti_click, leading)), min(configured, max(anti_click, trailing))


def _build_timed_narration(
    tts_segments,
    output_wav,
    video_duration,
    work_dir,
    *,
    adjust_speed=_adjust_tts_speed,
    command_runner=run_cmd,
    logger=log,
):
    """将 TTS 片段按时间轴放置到一条与视频等长的音轨上"""
    sample_rate = 44100
    total_samples = int(video_duration * sample_rate)
    buffer = bytearray(total_samples * 2)
    last_written_end = 0  # 追踪已写入位置，防止重叠
    prev_pause_samples = 0  # 前一段的 pause_after_ms，控制段间间隔
    skipped_count = 0  # 因 WAV 缺失/损坏/重采样失败而被跳过的段数
    placed_count = 0  # 真正写入音频的段数；防止"成功"生成全静音旁白
    no_safe_fit_count = 0  # 超预算但不能安全截断；交由 QC/manifest 阻断
    prev_authored_end = None  # 上一段作者标注的结束时间，用于判断"段落"边界
    run_gap = float(CONFIG.get("narration_run_gap_seconds", 1.6))   # 作者留白 > 此值 = 新段落
    tighten = bool(CONFIG.get("narration_tighten", True))
    tight_pause_samples = int(max(0.0, float(CONFIG.get("narration_tight_pause_seconds", 0.35))) * sample_rate)
    # 漂移上限：收紧时一句最多比作者标注的时间提前 max_pull 秒，避免整段解说被全部压到前面、与画面脱节
    max_pull_samples = int(max(0.0, float(CONFIG.get("narration_max_pull_seconds", 2.5))) * sample_rate)

    for seg in tts_segments:
        seg.setdefault("segment_audio_schema_version", SEGMENT_AUDIO_SCHEMA_VERSION)
        seg.setdefault("narration", str(seg.get("narration") or seg.get("spoken_text") or ""))
        seg.setdefault("spoken_text", str(seg.get("spoken_text") or seg.get("narration") or ""))
        seg.setdefault("truncated", False)
        seg.setdefault("truncate_reason", "none")
        seg.setdefault("fit_status", "pending_assembly")
        seg.setdefault("blocking", False)
        seg.setdefault("segment_tempo_factor", 1.0)
        seg.setdefault("global_narration_speed", float(CONFIG.get("narration_speed", 1.0) or 1.0))
        rate_factor = 1.0 + float(seg.get("tts_rate_offset", 0.0) or 0.0)
        seg.setdefault("effective_tempo", float(seg["global_narration_speed"]) * rate_factor * float(seg.get("segment_tempo_factor", 1.0) or 1.0))
        seg.setdefault("rms_dbfs_before", None)
        seg.setdefault("rms_dbfs_after", None)
        seg.setdefault("peak_after", None)
        wav_path = seg["audio_path"]
        seg_pause_ms = seg.get("pause_after_ms", CONFIG.get("breath_ms", 250))
        # 段落收紧：同一段落内（与上一句作者留白 <= run_gap）把这一句紧贴上一句的实际收尾播放，
        # 句间间隔固定为 tight_pause，不受 slot 内居中延迟 / TTS 时长波动影响。段落之间（作者特意留
        # 的大留白，让精彩原声透出）才放回原声。这样句间间隔稳定、不会出现"一句解说一段空白"。
        cur_authored_start = float(seg.get("start", 0.0))
        is_run_start = (placed_count == 0 or prev_authored_end is None
                        or cur_authored_start - prev_authored_end > run_gap)
        prev_authored_end = float(seg.get("end", cur_authored_start))

        if not os.path.exists(wav_path):
            seg["actual_place_start"] = seg["start"]
            seg["actual_place_end"] = seg["start"]
            seg["placed_audio_duration"] = 0.0
            seg["fit_status"] = "skipped"
            seg["truncate_reason"] = "missing_wav"
            prev_pause_samples = int(seg_pause_ms * sample_rate / 1000)
            skipped_count += 1
            continue

        # WAV 格式验证 + 采样率检查（合并为一次 wave.open）
        original_wav_path = wav_path
        _do_resample = False
        try:
            with wave.open(wav_path, 'rb') as wf_check:
                wf_channels = wf_check.getnchannels()
                wf_sampwidth = wf_check.getsampwidth()
                if wf_sampwidth != 2 or wf_channels != 1:
                    logger(f"  跳过非标准 WAV: {wav_path} (channels={wf_channels}, sampwidth={wf_sampwidth}), 需要 mono 16-bit")
                    seg["actual_place_start"] = seg["start"]
                    seg["actual_place_end"] = seg["start"]
                    seg["placed_audio_duration"] = 0.0
                    seg["fit_status"] = "skipped"
                    seg["truncate_reason"] = "resample_failed"
                    prev_pause_samples = int(seg_pause_ms * sample_rate / 1000)
                    skipped_count += 1
                    continue
                if wf_check.getframerate() != sample_rate:
                    _do_resample = True
        except Exception as e:
            logger(f"  WAV 读取失败: {wav_path}: {e}")
            seg["actual_place_start"] = seg["start"]
            seg["actual_place_end"] = seg["start"]
            seg["placed_audio_duration"] = 0.0
            seg["fit_status"] = "skipped"
            seg["truncate_reason"] = "missing_wav"
            prev_pause_samples = int(seg_pause_ms * sample_rate / 1000)
            skipped_count += 1
            continue

        tts_rate_offset = seg.get("tts_rate_offset", 0.0)
        tts_dur = seg.get("audio_duration", 0)

        configured_delay = max(0.0, float(CONFIG.get("narration_delay_seconds", 0.0) or 0.0))
        tail_pad = max(0.0, float(CONFIG.get("narration_tail_pad_seconds", 0.1) or 0.0))
        slot_duration = max(0.0, float(seg["end"]) - float(seg["start"]))
        max_delay = max(0.0, slot_duration - float(tts_dur or 0.0) - tail_pad)
        narration_delay = min(configured_delay, max_delay)
        start_sample = int((seg["start"] + narration_delay) * sample_rate)
        end_boundary = int(min(seg["end"], video_duration) * sample_rate)

        # 段间间隔：使用前一段的 pause_after_ms（来自 narration.json）
        min_start_with_pause = last_written_end + prev_pause_samples
        if tighten and not is_run_start:
            # 段落内：紧贴上一句的实际收尾播放，句间间隔固定为 tight_pause（不被 slot 内居中延迟撑大），
            # 但不早于"作者标注起始 - max_pull"，防止整段被压到前面与画面脱节。
            drift_floor = int(cur_authored_start * sample_rate) - max_pull_samples
            actual_start = max(last_written_end + tight_pause_samples, drift_floor)
        else:
            # 段落起点（或关闭收紧）：尊重作者标注的起始 + 入场延迟，让画面/原声先立住
            actual_start = max(start_sample, min_start_with_pause)
        actual_start = min(actual_start, end_boundary)  # 不超出 slot 边界

        # 根据实际可用空间决定是否加速
        available_samples = end_boundary - actual_start
        available_duration = max(available_samples / sample_rate, 0)
        if tts_dur > available_duration > 0:
            adjusted_result = adjust_speed(
                wav_path, available_duration, work_dir, tts_rate_offset)
            if len(adjusted_result) == 2:
                wav_path, _actual_dur = adjusted_result
                budget = narration_tempo_budget(tts_rate_offset)
                fit_meta = {
                    "fit_status": "tempo_adjusted" if wav_path != original_wav_path else "fits",
                    "segment_tempo_factor": 1.0,
                    "effective_tempo": budget["global_narration_speed"],
                    "global_narration_speed": budget["global_narration_speed"],
                    "truncate_reason": "none",
                }
            else:
                wav_path, _actual_dur, fit_meta = adjusted_result
            seg.update({
                "fit_status": fit_meta["fit_status"],
                "segment_tempo_factor": fit_meta.get("segment_tempo_factor", 1.0),
                "effective_tempo": fit_meta.get("effective_tempo", seg.get("effective_tempo")),
                "global_narration_speed": fit_meta.get("global_narration_speed", seg.get("global_narration_speed")),
                "blocking": bool(fit_meta.get("blocking", False)),
            })
            if fit_meta["fit_status"] == "no_safe_fit":
                seg["actual_place_start"] = actual_start / sample_rate
                seg["actual_place_end"] = actual_start / sample_rate
                seg["placed_audio_duration"] = 0.0
                seg["truncate_reason"] = fit_meta["truncate_reason"]
                prev_pause_samples = int(seg_pause_ms * sample_rate / 1000)
                skipped_count += 1
                no_safe_fit_count += 1
                continue
        else:
            budget = narration_tempo_budget(tts_rate_offset)
            seg["fit_status"] = "fits"
            seg["segment_tempo_factor"] = 1.0
            seg["global_narration_speed"] = budget["global_narration_speed"]
            seg["effective_tempo"] = budget["global_narration_speed"] * budget["tts_rate_factor"]

        # _adjust_tts_speed 输出固定 44100Hz mono 16bit，若文件被替换则无需 resample
        if wav_path != original_wav_path:
            _do_resample = False
        if _do_resample:
            tmp_path = str(Path(work_dir) / f"_rs_{seg.get('index', 0)}.wav")
            rs_result = command_runner(["ffmpeg", "-y", "-i", wav_path,
                                        "-ar", str(sample_rate), "-ac", "1",
                                        "-acodec", "pcm_s16le", tmp_path])
            if rs_result.returncode != 0 or not os.path.exists(tmp_path):
                logger(f"  重采样失败，跳过本段: {wav_path}: {rs_result.stderr}")
                seg["actual_place_start"] = seg["start"]
                seg["actual_place_end"] = seg["start"]
                seg["placed_audio_duration"] = 0.0
                seg["fit_status"] = "skipped"
                seg["truncate_reason"] = "resample_failed"
                prev_pause_samples = int(seg_pause_ms * sample_rate / 1000)
                skipped_count += 1
                continue
            wav_path = tmp_path

        with wave.open(wav_path, "rb") as wf:
            wf_data = bytearray(wf.readframes(wf.getnframes()))

        # 按场景边界裁剪
        audio_samples = len(wf_data) // 2
        available = end_boundary - actual_start
        write_samples = audio_samples

        if write_samples <= 0 or available <= 0:
            logger(f"  跳过: {seg['start']:.1f}s-{seg['end']:.1f}s (无空间)")
            seg["actual_place_start"] = seg["start"]
            seg["actual_place_end"] = seg["start"]
            seg["placed_audio_duration"] = 0.0
            seg["fit_status"] = "no_safe_fit"
            seg["blocking"] = True
            seg["truncate_reason"] = "no_room"
            prev_pause_samples = int(seg_pause_ms * sample_rate / 1000)
            no_safe_fit_count += 1
            continue

        if audio_samples > available:
            # No tolerance-based trimming: even a few milliseconds may contain a
            # consonant/vowel release. _adjust_tts_speed must produce a complete file
            # that fits; otherwise block and ask the Agent to shorten/move the block.
            over = (audio_samples - available) / sample_rate
            logger(f"  TTS 无安全放置: 段 {seg.get('index', '?')} 超出可用窗口 {over:.3f}s；禁止裁尾，交由 QC 阻断")
            seg["actual_place_start"] = actual_start / sample_rate
            seg["actual_place_end"] = actual_start / sample_rate
            seg["placed_audio_duration"] = 0.0
            seg["fit_status"] = "no_safe_fit"
            seg["blocking"] = True
            seg["truncate_reason"] = "no_safe_boundary"
            prev_pause_samples = int(seg_pause_ms * sample_rate / 1000)
            skipped_count += 1
            no_safe_fit_count += 1
            continue

        # 重叠检测：跳过与前段重叠的部分（在 fade 之前，避免截断后丢失 fade-in）
        if actual_start < last_written_end:
            overlap_ms = (last_written_end - actual_start) * 1000 / sample_rate
            if last_written_end >= actual_start + write_samples:
                logger(f"  跳过重叠段: {actual_start/sample_rate:.1f}s "
                       f"(与前段重叠 {overlap_ms:.0f}ms)")
                seg["actual_place_start"] = seg["start"]
                seg["actual_place_end"] = seg["start"]
                seg["placed_audio_duration"] = 0.0
                seg["fit_status"] = "no_safe_fit"
                seg["blocking"] = True
                seg["truncate_reason"] = "no_room"
                prev_pause_samples = int(seg_pause_ms * sample_rate / 1000)
                no_safe_fit_count += 1
                continue
            actual_start = last_written_end
            available = end_boundary - actual_start
            if write_samples > available:
                logger(f"  重叠 {overlap_ms:.0f}ms 后无安全完整窗口，跳过")
                seg["actual_place_start"] = actual_start / sample_rate
                seg["actual_place_end"] = actual_start / sample_rate
                seg["placed_audio_duration"] = 0.0
                seg["fit_status"] = "no_safe_fit"
                seg["blocking"] = True
                seg["truncate_reason"] = "no_safe_boundary"
                prev_pause_samples = int(seg_pause_ms * sample_rate / 1000)
                skipped_count += 1
                no_safe_fit_count += 1
                continue

        # fade-in / fade-out（在 overlap 裁剪之后应用，确保正确的音频包络）
        fade_in_len, fade_out_len = _speech_safe_fade_lengths(
            wf_data, write_samples, sample_rate, CONFIG.get("fade_ms", 120)
        )
        for i in range(fade_in_len):
            gain = i / fade_in_len
            s = i * 2
            sample = int.from_bytes(wf_data[s:s+2], 'little', signed=True)
            sample = int(sample * gain)
            wf_data[s:s+2] = sample.to_bytes(2, 'little', signed=True)
        for i in range(fade_out_len):
            gain = 1.0 - i / fade_out_len
            s = (write_samples - 1 - i) * 2
            if s < 0:
                break
            sample = int.from_bytes(wf_data[s:s+2], 'little', signed=True)
            sample = int(sample * gain)
            wf_data[s:s+2] = sample.to_bytes(2, 'little', signed=True)

        # Persist the exact complete per-beat PCM used by the canonical mix. Editable
        # exports must reference this file, not the longer pre-fit TTS input; otherwise
        # their timeline_end silently chops the final word even when ffmpeg is correct.
        placed_path = Path(work_dir) / f"_placed_{int(seg.get('index', placed_count)):04d}.wav"
        with wave.open(str(placed_path), "wb") as placed_wav:
            placed_wav.setnchannels(1)
            placed_wav.setsampwidth(2)
            placed_wav.setframerate(sample_rate)
            placed_wav.writeframes(bytes(wf_data))
        seg["placed_audio_path"] = str(placed_path)

        buffer[actual_start * 2: actual_start * 2 + write_samples * 2] = wf_data
        seg["actual_place_start"] = actual_start / sample_rate
        seg["actual_place_end"] = (actual_start + write_samples) / sample_rate
        seg["placed_audio_duration"] = write_samples / sample_rate
        if seg.get("fit_status") == "pending_assembly":
            seg["fit_status"] = "fits"
        if seg.get("truncate_reason") in (None, ""):
            seg["truncate_reason"] = "none"
        last_written_end = actual_start + write_samples
        prev_pause_samples = int(seg_pause_ms * sample_rate / 1000)
        placed_count += 1

    with wave.open(str(output_wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(buffer))

    if tts_segments and placed_count == 0 and no_safe_fit_count == 0:
        output_wav.unlink(missing_ok=True)
        raise RuntimeError(
            f"全部 {len(tts_segments)} 段解说均被跳过或未能写入"
            f"（WAV 缺失/损坏/重采样失败/无可用时间；跳过 {skipped_count} 段），"
            "已中止以避免生成无解说视频"
        )

    logger(f"解说音轨: {video_duration:.1f}s, {len(tts_segments)} 段")
