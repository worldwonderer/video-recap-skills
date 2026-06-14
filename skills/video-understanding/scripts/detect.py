import json
import os
import re
import subprocess

from lib import CONFIG
from lib import log, run_cmd, get_video_duration

# ── Step 2: 场景检测 ──────────────────────────────────────────────────

def detect_scenes(video_path, work_dir, threshold=None):
    """使用 ffmpeg scdet 滤镜检测场景切换"""
    threshold = CONFIG["scene_threshold"] if threshold is None else threshold
    scdet_threshold = int(threshold * 100)

    cmd = ["ffmpeg", "-i", str(video_path),
           "-vf", f"scdet=threshold={scdet_threshold}",
           "-f", "null", "-"]
    result = run_cmd(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"场景检测失败: {result.stderr}")

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

    log(f"检测到 {len(scenes)} 个场景")

    # 先过滤黑/白帧过渡场景，再合并短场景
    # （合并会保留短场景的 start，若黑场被并入长场景，单点采样会误删整段，故顺序在前）
    if CONFIG.get("scene_junk_filter", True):
        scenes = _filter_junk_scenes(scenes, video_path)
    # 合并短场景（< 3s 合并到相邻场景）
    scenes = _merge_short_scenes(scenes, min_duration=CONFIG.get("scene_merge_min", 4.0))

    # 保存
    scenes_file = work_dir / "scenes.json"
    scenes_file.write_text(json.dumps(scenes, ensure_ascii=False, indent=2), encoding="utf-8")
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


def _sample_frame_luma(video_path, timestamp, sample_size=64):
    """Extract one frame via ffmpeg and return luma values without extra deps."""
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(0.0, float(timestamp)):.3f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-vf",
        f"scale={sample_size}:{sample_size},format=rgb24",
        "-f",
        "rawvideo",
        "-",
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace")[:300])
    data = result.stdout
    return [
        0.2126 * data[i] + 0.7152 * data[i + 1] + 0.0722 * data[i + 2]
        for i in range(0, len(data) - 2, 3)
    ]


def _is_junk_scene(video_path, timestamp, threshold_dark=None, threshold_bright=None):
    """Return True for near-black or near-white scene-start frames."""
    threshold_dark = CONFIG.get("scene_junk_dark_luma", 8.0) if threshold_dark is None else threshold_dark
    threshold_bright = CONFIG.get("scene_junk_bright_luma", 245.0) if threshold_bright is None else threshold_bright
    pixel_ratio = min(1.0, max(0.0, float(CONFIG.get("scene_junk_pixel_ratio", 0.995))))
    try:
        lumas = _sample_frame_luma(video_path, timestamp)
    except Exception as exc:
        log(f"场景亮度采样失败，保留场景 {timestamp:.1f}s: {exc}")
        return False
    if not lumas:
        return False
    avg_luma = sum(lumas) / len(lumas)
    dark_ratio = sum(1 for value in lumas if value <= threshold_dark) / len(lumas)
    bright_ratio = sum(1 for value in lumas if value >= threshold_bright) / len(lumas)
    return (
        avg_luma <= threshold_dark and dark_ratio >= pixel_ratio
    ) or (
        avg_luma >= threshold_bright and bright_ratio >= pixel_ratio
    )


def _filter_junk_scenes(scenes, video_path):
    """Filter black/white transition scenes while never deleting the whole video."""
    if len(scenes) <= 1:
        return scenes
    filtered = []
    removed = []
    for scene in scenes:
        start = float(scene["start"])
        end = float(scene["end"])
        # 多点采样：起始、中点、结尾各探一帧；只有全部为垃圾帧才删除，
        # 含任意非垃圾帧的场景必须保留（避免短黑场并入长真实场景后被整段误删）
        probe_times = [
            min(end, start + 0.1),
            (start + end) / 2.0,
            max(start, end - 0.1),
        ]
        if all(_is_junk_scene(video_path, t) for t in probe_times):
            removed.append(scene)
        else:
            filtered.append(scene)
    if not filtered:
        log("场景黑/白帧过滤会删除全部场景，已放弃过滤")
        return scenes
    if removed:
        log(f"过滤黑/白帧过渡场景: {len(scenes)} → {len(filtered)}")
    return filtered



# ── Step 3.5: 静音检测 ─────────────────────────────────────────────

def detect_silence_periods(video_path, work_dir, asr_result=None):
    """用 ffmpeg silencedetect 检测安静时段，作为解说插入的候选窗口"""
    audio_path = work_dir / "audio.wav"
    if not audio_path.exists():
        # 提取到临时文件，成功后原子移动到位，避免被中断的 -y 运行留下半截 audio.wav
        tmp_path = work_dir / "audio.wav.tmp"
        extract = run_cmd([
            "ffmpeg", "-y", "-i", str(video_path),
            "-vn", "-ar", "16000", "-ac", "1",
            "-f", "wav", str(tmp_path)  # .tmp extension hides the format from ffmpeg; state it
        ])
        if extract.returncode != 0 or not tmp_path.exists():
            log(f"音频提取失败，无法检测静音窗口（视频可能无音轨）: {extract.stderr}")
            if tmp_path.exists():
                tmp_path.unlink()
            return []
        os.replace(str(tmp_path), str(audio_path))

    noise = CONFIG["silence_noise_threshold"]
    min_dur = CONFIG["silence_min_duration"]
    cmd = ["ffmpeg", "-i", str(audio_path),
           "-af", f"silencedetect=noise={noise}:d={min_dur}",
           "-f", "null", "-"]
    result = run_cmd(cmd, timeout=120)
    if result.returncode != 0:
        log(f"静音检测失败: {result.stderr}")
        return []
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
    # 3. 平均段长过大 → 粒度太粗，无法判断哪些窗口有语音
    #    阈值 45s 给默认 ASR 窗口(asr_segment_seconds=30s)留出余量，使交叉验证重新生效；
    #    粗粒度(如 180s 旧窗口)仍会被正确跳过。
    if asr_result:
        video_dur = get_video_duration(str(audio_path))
        asr_coverage = sum(seg.get("end", 0) - seg.get("start", 0) for seg in asr_result)
        avg_seg_dur = asr_coverage / len(asr_result) if asr_result else 0
        skip_cross_check = (
            (len(asr_result) <= 5 and asr_coverage > video_dur * 0.8) or
            asr_coverage > video_dur * 1.5 or
            avg_seg_dur > 45
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
        json.dumps(periods, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"检测到 {len(periods)} 个安静窗口 (≥{quiet_min}s)")
    for qp in periods:
        flag = " [有语音]" if qp["has_speech"] else ""
        log(f"  {qp['start']:.1f}s-{qp['end']:.1f}s ({qp['duration']:.1f}s){flag}")
    return periods
