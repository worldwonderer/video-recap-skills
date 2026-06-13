import os
import shutil
import wave
from pathlib import Path

from config import CONFIG
from common import log, run_cmd, get_video_duration
from tts import _get_audio_duration

SUBTITLE_RENDER_VERSION = 1


def _seconds_to_srt_time(seconds):
    """将秒数转为 SRT 时间格式 HH:MM:SS,mmm"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _seconds_to_ass_time(seconds):
    """将秒数转为 ASS 时间格式 H:MM:SS.cc"""
    centiseconds = int(round(float(seconds) * 100))
    h = centiseconds // 360000
    centiseconds %= 360000
    m = centiseconds // 6000
    centiseconds %= 6000
    s = centiseconds // 100
    cs = centiseconds % 100
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _subtitle_style_config():
    """Return the internal default burn-in subtitle style."""
    return {
        "font_name": CONFIG.get("subtitle_font_name", "Arial"),
        "font_size": CONFIG.get("subtitle_font_size", 42),
        "primary_color": CONFIG.get("subtitle_primary_color", "&H00FFFFFF"),
        "outline_color": CONFIG.get("subtitle_outline_color", "&H00000000"),
        "outline": CONFIG.get("subtitle_outline", 2),
        "shadow": CONFIG.get("subtitle_shadow", 1),
        "alignment": CONFIG.get("subtitle_alignment", 2),
        "margin_l": CONFIG.get("subtitle_margin_l", 40),
        "margin_r": CONFIG.get("subtitle_margin_r", 40),
        "margin_v": CONFIG.get("subtitle_margin_v", 48),
        "max_chars": CONFIG.get("subtitle_max_chars", 20),
        "play_res_x": CONFIG.get("subtitle_play_res_x", 1280),
        "play_res_y": CONFIG.get("subtitle_play_res_y", 720),
    }


def assembly_settings_fingerprint():
    """Settings that affect the rendered video, used by pipeline resume cache."""
    fingerprint = {
        "version": SUBTITLE_RENDER_VERSION,
        "burn_subtitles": bool(CONFIG.get("burn_subtitles", False)),
        "force_video_reencode": bool(CONFIG.get("force_video_reencode", False)),
        "narration_timing": {
            "delay_seconds": CONFIG.get("narration_delay_seconds", 1.5),
            "tail_pad_seconds": CONFIG.get("narration_tail_pad_seconds", 0.1),
            "fade_ms": CONFIG.get("fade_ms", 300),
        },
        "audio_mix": {
            "ducking_mode": CONFIG.get("ducking_mode", "fixed"),
            "ducking_narr_weight": CONFIG.get("ducking_narr_weight", 1.5),
            "ducking_orig_volume": CONFIG.get("ducking_orig_volume", 0.5),
            "speech_ducking_volume": CONFIG.get("speech_ducking_volume", 0.2),
            "zone_ducking_volume": CONFIG.get("zone_ducking_volume", 0.12),
            "zone_fade_seconds": CONFIG.get("zone_fade_seconds", 0.5),
            "final_loudnorm": final_loudnorm_filter() or "off",
        },
    }
    if fingerprint["burn_subtitles"]:
        fingerprint["subtitle_renderer"] = "ass"
        fingerprint["subtitle_style"] = _subtitle_style_config()
    return fingerprint


def _wrap_subtitle_text(text, max_chars=20, line_break="\n"):
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
    return line_break.join(lines)


def _subtitle_entries(narration):
    """Collect subtitle entries from final TTS segment placement."""
    entries = []
    for seg in narration:
        if not isinstance(seg, dict):
            continue
        text = str(seg.get("narration", "")).strip()
        if not text:
            continue
        try:
            start = float(seg.get("actual_place_start", seg["start"]))
            end = float(seg.get("actual_place_end", seg["end"]))
        except (KeyError, TypeError, ValueError):
            continue
        if end - start < 0.1:
            continue
        entries.append({"start": start, "end": end, "text": text})
    return entries


def _generate_srt(narration, work_dir):
    """将解说脚本转为 SRT 字幕文件，使用实际音频放置时间"""
    srt_lines = []
    max_chars = int(CONFIG.get("subtitle_max_chars", 20))
    for idx, entry in enumerate(_subtitle_entries(narration), start=1):
        start_ts = _seconds_to_srt_time(entry["start"])
        end_ts = _seconds_to_srt_time(entry["end"])
        srt_lines.append(str(idx))
        srt_lines.append(f"{start_ts} --> {end_ts}")
        srt_lines.append(_wrap_subtitle_text(entry["text"], max_chars=max_chars))
        srt_lines.append("")
    srt_path = work_dir / "subtitles.srt"
    srt_path.write_text("\n".join(srt_lines), encoding="utf-8")
    return srt_path


def _escape_ass_text(text):
    """Escape user text for an ASS dialogue Text field."""
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\N")
    )


def _generate_ass(narration, work_dir):
    """Generate an ASS subtitle file for readable hard-sub rendering."""
    style = _subtitle_style_config()
    ass_lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        f"PlayResX: {int(style['play_res_x'])}",
        f"PlayResY: {int(style['play_res_y'])}",
        "",
        "[V4+ Styles]",
        (
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding"
        ),
        (
            "Style: Default,"
            f"{style['font_name']},{style['font_size']},{style['primary_color']},&H000000FF,"
            f"{style['outline_color']},&H64000000,0,0,0,0,100,100,0,0,1,"
            f"{style['outline']},{style['shadow']},{style['alignment']},"
            f"{style['margin_l']},{style['margin_r']},{style['margin_v']},1"
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    max_chars = int(style["max_chars"])
    for entry in _subtitle_entries(narration):
        text = _wrap_subtitle_text(_escape_ass_text(entry["text"]), max_chars=max_chars, line_break="\\N")
        ass_lines.append(
            "Dialogue: 0,"
            f"{_seconds_to_ass_time(entry['start'])},{_seconds_to_ass_time(entry['end'])},"
            f"Default,,0,0,0,,{text}"
        )

    ass_path = work_dir / "subtitles.ass"
    ass_path.write_text("\n".join(ass_lines) + "\n", encoding="utf-8")
    return ass_path


def _escape_subtitle_filter_path(path):
    """Escape a path for ffmpeg subtitle/ass video filter arguments."""
    text = str(path).replace("\\", "/")
    for raw, escaped in (
        ("\\", "\\\\"),
        (":", "\\:"),
        ("'", "\\'"),
        (",", "\\,"),
        ("[", "\\["),
        ("]", "\\]"),
    ):
        text = text.replace(raw, escaped)
    return text


def _subtitle_burn_filter(subtitle_path):
    """Build the ffmpeg video filter used for hard-sub rendering."""
    return f"subtitles=filename='{_escape_subtitle_filter_path(subtitle_path)}'"


def final_loudnorm_filter():
    """Final-mix loudness normalization filter from CONFIG, or None when disabled.

    Ducking branches set only relative balance; this single stage owns the
    absolute output loudness so the recap is not left too quiet.
    """
    if not CONFIG.get("final_loudnorm", True):
        return None
    return (
        f"loudnorm=I={CONFIG.get('target_lufs', -14.0)}"
        f":TP={CONFIG.get('target_true_peak', -1.0)}"
        f":LRA={CONFIG.get('target_lra', 11.0)}"
    )


def _seg_place_window(seg):
    """Return a segment's actual placed (start, end) on the output timeline."""
    s = seg.get("actual_place_start", seg.get("start", 0))
    e = seg.get("actual_place_end", seg.get("end", 0))
    return s, e


def _amix_tail(narr_vol):
    """Original [orig] + boosted narration [narr] -> [aout]; shared by every mode."""
    return (
        f"[1:a]volume={narr_vol},aresample=48000[narr];"
        "[orig][narr]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
    )


def _build_audio_filter_complex(tts_segments):
    """Build the ffmpeg filter_complex that ducks the original audio under narration.

    CONFIG["ducking_mode"] (default "fixed") selects the strategy:
      - sidechaincompress: classic auto-duck keyed off the narration track.
      - none: no ducking; just boost narration and mix.
      - fixed (the default): a per-segment volume envelope on the original track,
        whose shape is chosen by the beats' overlaps_speech flags — lower under
        speech-overlapping beats, lower still under beats in quiet windows,
        otherwise an idle level. When every beat is quiet, a smooth trapezoid
        fade is used instead of hard switches; with no overlap info at all it
        falls back to a constant level.
    The narration's placed timing comes from actual_place_start/end.
    """
    ducking_mode = CONFIG.get("ducking_mode", "fixed")
    narr_vol = CONFIG.get("ducking_narr_weight", 1.5)
    has_overlaps = any(seg.get("overlaps_speech") for seg in tts_segments if isinstance(seg, dict))
    has_quiet = any(not seg.get("overlaps_speech", True) for seg in tts_segments if isinstance(seg, dict))

    if ducking_mode == "sidechaincompress":
        return (
            "[0:a]aresample=48000[orig];"
            "[1:a]aresample=48000[narr];"
            f"[orig][narr]sidechaincompress="
            f"threshold={CONFIG['ducking_threshold']}:ratio={CONFIG['ducking_ratio']}"
            f":attack={CONFIG['ducking_attack']}:release={CONFIG['ducking_release']}"
            f":knee=2.5:makeup={CONFIG['ducking_makeup']}:level_sc={CONFIG['ducking_level_sc']}"
            f"[ducked];"
            f"[ducked][narr]amix=inputs=2:duration=first:dropout_transition=0"
            f":weights=1 {CONFIG['ducking_narr_weight']}:normalize=0[aout]"
        )

    if ducking_mode == "none":
        return "[0:a]aresample=48000[orig];" + _amix_tail(narr_vol)

    quiet_vol = CONFIG.get("zone_ducking_volume", 0.12)

    if has_overlaps and has_quiet:
        # Per-segment volume envelope. between(t,s,e) indicators select beats;
        # commas inside the expr are fine here because the whole volume value is
        # single-quoted, so ffmpeg does not read them as filter separators.
        speech_vol = CONFIG.get("speech_ducking_volume", 0.2)
        default_vol = CONFIG.get("ducking_orig_volume", 0.5)
        overlap_exprs, quiet_exprs = [], []
        for seg in tts_segments:
            if not isinstance(seg, dict):
                continue
            s, e = _seg_place_window(seg)
            ind = f"if(between(t,{s:.2f},{e:.2f}),1,0)"
            (overlap_exprs if seg.get("overlaps_speech") else quiet_exprs).append(ind)
        vol_expr = f"{default_vol}"
        if overlap_exprs:
            vol_expr += f"+{speech_vol-default_vol}*({'+'.join(overlap_exprs)})"
        if quiet_exprs:
            vol_expr += f"+{quiet_vol-default_vol}*({'+'.join(quiet_exprs)})"
        vol_expr = f"max(0,min(1,{vol_expr}))"
        log(f"动态 ducking: 语音重叠段={len(overlap_exprs)}, 安静段={len(quiet_exprs)}")
        return f"[0:a]volume='{vol_expr}':eval=frame,aresample=48000[orig];" + _amix_tail(narr_vol)

    if has_quiet and not has_overlaps:
        # All beats in quiet windows: smooth trapezoid fade to quiet_vol under each
        # beat, full original elsewhere.
        default_vol = 1.0
        fade = CONFIG.get("zone_fade_seconds", 0.5)
        exprs = []
        for seg in tts_segments:
            if not isinstance(seg, dict):
                continue
            s, e = _seg_place_window(seg)
            exprs.append(f"min(1,max(0,min(t-{s:.2f},{e:.2f}-t)/{fade:.1f}))")
        vol_expr = f"{default_vol}"
        if exprs:
            vol_expr += f"+{quiet_vol - default_vol}*({'+'.join(exprs)})"
        vol_expr = f"max(0,min(1,{vol_expr}))"
        log(f"zone ducking: 解说时原声={quiet_vol}, 非解说时原声={default_vol}")
        return f"[0:a]volume='{vol_expr}':eval=frame,aresample=48000[orig];" + _amix_tail(narr_vol)

    # fixed (no per-segment overlap info): hold the original at a constant level.
    orig_vol = CONFIG.get("ducking_orig_volume", 0.5)
    return f"[0:a]volume={orig_vol},aresample=48000[orig];" + _amix_tail(narr_vol)


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
    ass_path = None
    if CONFIG.get("burn_subtitles", False):
        ass_path = _generate_ass(tts_segments, work_dir)
        log(f"压制字幕文件: {ass_path}")

    # 混合原始音频 + 解说音频
    filter_complex = _build_audio_filter_complex(tts_segments)

    # 对于超长 volume 表达式（多段解说），使用 -filter_complex_script 避免命令行溢出
    # 末端整体响度归一：ducking 只管相对平衡，这一步统一成片绝对响度
    aout_label = "[aout]"
    final_ln = final_loudnorm_filter()
    if final_ln:
        filter_complex += f";[aout]{final_ln}[aoutln]"
        aout_label = "[aoutln]"
        log(f"成片响度归一: {final_ln}")

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
            "-map", "0:v", "-map", aout_label,
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_video),
            "-i", str(narration_wav),
            "-filter_complex", filter_complex,
            "-map", "0:v", "-map", aout_label,
        ]

    if CONFIG.get("burn_subtitles", False):
        cmd += ["-vf", _subtitle_burn_filter(ass_path), "-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]
        log("压制解说字幕（ASS，需要重编码）...")
    elif CONFIG.get("force_video_reencode", False):
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]
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
    skipped_count = 0  # 因 WAV 缺失/损坏/重采样失败而被跳过的段数

    for seg in tts_segments:
        wav_path = seg["audio_path"]
        seg_pause_ms = seg.get("pause_after_ms", CONFIG.get("breath_ms", 250))

        if not os.path.exists(wav_path):
            seg["actual_place_start"] = seg["start"]
            seg["actual_place_end"] = seg["start"]
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
                    log(f"  跳过非标准 WAV: {wav_path} (channels={wf_channels}, sampwidth={wf_sampwidth}), 需要 mono 16-bit")
                    seg["actual_place_start"] = seg["start"]
                    seg["actual_place_end"] = seg["start"]
                    prev_pause_samples = int(seg_pause_ms * sample_rate / 1000)
                    skipped_count += 1
                    continue
                if wf_check.getframerate() != sample_rate:
                    _do_resample = True
        except Exception as e:
            log(f"  WAV 读取失败: {wav_path}: {e}")
            seg["actual_place_start"] = seg["start"]
            seg["actual_place_end"] = seg["start"]
            prev_pause_samples = int(seg_pause_ms * sample_rate / 1000)
            skipped_count += 1
            continue

        tts_rate_offset = seg.get("tts_rate_offset", 0.0)
        tts_dur = seg.get("audio_duration", 0)

        configured_delay = max(0.0, float(CONFIG.get("narration_delay_seconds", 1.5) or 0.0))
        tail_pad = max(0.0, float(CONFIG.get("narration_tail_pad_seconds", 0.1) or 0.0))
        slot_duration = max(0.0, float(seg["end"]) - float(seg["start"]))
        max_delay = max(0.0, slot_duration - float(tts_dur or 0.0) - tail_pad)
        narration_delay = min(configured_delay, max_delay)
        start_sample = int((seg["start"] + narration_delay) * sample_rate)
        end_boundary = int(min(seg["end"], video_duration) * sample_rate)

        # 段间间隔：使用前一段的 pause_after_ms（来自 narration.json）
        # 第一段无延迟，后续段在前段结束后等待前段的 pause
        min_start_with_pause = last_written_end + prev_pause_samples
        actual_start = max(start_sample, min_start_with_pause)
        actual_start = min(actual_start, end_boundary)  # 不超出 slot 边界

        # 根据实际可用空间决定是否加速
        available_samples = end_boundary - actual_start
        available_duration = max(available_samples / sample_rate, 0)
        if tts_dur > available_duration > 0:
            wav_path, _actual_dur = _adjust_tts_speed(wav_path, available_duration, work_dir, tts_rate_offset)
        else:
            pass  # tts_dur <= available_duration, no speed adjust needed

        # _adjust_tts_speed 输出固定 44100Hz mono 16bit，若文件被替换则无需 resample
        if wav_path != original_wav_path:
            _do_resample = False
        if _do_resample:
            tmp_path = str(Path(work_dir) / f"_rs_{seg.get('index', 0)}.wav")
            rs_result = run_cmd(["ffmpeg", "-y", "-i", wav_path,
                                 "-ar", str(sample_rate), "-ac", "1",
                                 "-acodec", "pcm_s16le", tmp_path])
            if rs_result.returncode != 0 or not os.path.exists(tmp_path):
                log(f"  重采样失败，跳过本段: {wav_path}: {rs_result.stderr}")
                seg["actual_place_start"] = seg["start"]
                seg["actual_place_end"] = seg["start"]
                prev_pause_samples = int(seg_pause_ms * sample_rate / 1000)
                skipped_count += 1
                continue
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
        fade_len = min(int(CONFIG.get("fade_ms", 300) * sample_rate / 1000), write_samples // 4)
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

    if tts_segments and skipped_count >= len(tts_segments):
        log(f"  ⚠️ 严重警告: 全部 {len(tts_segments)} 段解说均被跳过（WAV 缺失/损坏/重采样失败），成片将没有解说音频")

    log(f"解说音轨: {video_duration:.1f}s, {len(tts_segments)} 段")
