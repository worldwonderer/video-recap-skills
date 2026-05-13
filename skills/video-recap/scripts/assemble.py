import json
import os
import shutil
import wave
from pathlib import Path

from config import CONFIG
from common import log, run_cmd, get_video_duration
from tts import _get_audio_duration

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

