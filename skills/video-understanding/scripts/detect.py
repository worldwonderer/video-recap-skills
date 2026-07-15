import json
import os
import re
import subprocess
from pathlib import Path

from lib import CONFIG
from lib import log, run_cmd, get_video_duration, file_fingerprint

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

def _compact_ffmpeg_error(stderr, limit=400):
    """Keep the actionable tail without dumping ffmpeg build/configuration banners."""
    text = " ".join(str(stderr or "").split())
    if len(text) <= limit:
        return text
    return "…" + text[-limit:]

def annotate_quiet_windows_with_asr(periods, asr_result=None, *, video_duration=None, configured_segment_seconds=None):
    """Pure helper: annotate quiet windows with ASR-overlap confidence and QC.

    Coarse grid ASR (large synthetic windows from chunking) must not turn every quiet
    window into speech. The return value is (annotated_periods, qc). Inputs are copied.
    """
    out = [dict(p) for p in (periods or []) if isinstance(p, dict)]
    qc = {"coarse_asr_windows": 0, "low_confidence_speech_flags": 0, "asr_granularity": "none"}
    for qp in out:
        qp.setdefault("speech_overlap_ratio", 0.0)
        qp.setdefault("asr_overlap_seconds", 0.0)
        qp.setdefault("asr_granularity", "none")
        qp.setdefault("has_speech", False)
        qp.setdefault("has_speech_reason", "no_asr_overlap")
    if not asr_result:
        return out, qc

    valid_segments = []
    for seg in asr_result:
        if not isinstance(seg, dict):
            continue
        try:
            ss, se = float(seg.get("start", 0)), float(seg.get("end", 0))
        except (TypeError, ValueError):
            continue
        if se > ss:
            valid_segments.append((ss, se))
    asr_coverage = sum(se - ss for ss, se in valid_segments)
    avg_seg_dur = asr_coverage / len(valid_segments) if valid_segments else 0
    if video_duration is None:
        ends = [float(p.get("end", 0) or 0) for p in out] + [se for _, se in valid_segments]
        video_duration = max(ends or [0.0])
    configured = float(configured_segment_seconds if configured_segment_seconds is not None else (CONFIG.get("asr_segment_seconds", 30) or 30))
    coarse_asr = (
        (len(valid_segments) <= 5 and asr_coverage > float(video_duration) * 0.8) or
        asr_coverage > float(video_duration) * 1.5 or
        avg_seg_dur > max(45.0, configured * 1.5) or
        (avg_seg_dur >= configured * 0.9 and asr_coverage > float(video_duration) * 0.7)
    )
    granularity = "coarse_grid" if coarse_asr else "segment"
    qc["asr_granularity"] = granularity
    qc["avg_asr_segment_seconds"] = round(avg_seg_dur, 3)
    for qp in out:
        overlap_seconds = 0.0
        for ss, se in valid_segments:
            overlap_seconds += max(0.0, min(float(qp["end"]), se) - max(float(qp["start"]), ss))
        ratio = overlap_seconds / max(0.001, float(qp.get("duration", 0.0) or 0.0))
        qp["asr_overlap_seconds"] = round(overlap_seconds, 3)
        qp["speech_overlap_ratio"] = round(ratio, 4)
        qp["asr_granularity"] = granularity
        if coarse_asr:
            qp["has_speech"] = False
            qp["has_speech_reason"] = "coarse_asr_overlap_ignored" if overlap_seconds > 0 else "coarse_asr_no_overlap"
            if overlap_seconds > 0:
                qc["coarse_asr_windows"] += 1
            continue
        if ratio >= 0.3:
            qp["has_speech"] = True
            qp["has_speech_reason"] = "asr_overlap_high_confidence"
        elif overlap_seconds > 0:
            qp["has_speech"] = False
            qp["has_speech_reason"] = "asr_overlap_low_confidence_quiet"
            qc["low_confidence_speech_flags"] += 1
        else:
            qp["has_speech"] = False
            qp["has_speech_reason"] = "no_asr_overlap"
    return out, qc


def detect_silence_periods(video_path, work_dir, asr_result=None):
    """用 ffmpeg silencedetect 检测安静时段，作为解说插入的候选窗口"""
    audio_path = work_dir / "audio.wav"
    if not _audio_cache_matches(audio_path, video_path):
        if audio_path.exists():
            audio_path.unlink()
        # 提取到临时文件，成功后原子移动到位，避免被中断的 -y 运行留下半截 audio.wav
        tmp_path = work_dir / "audio.wav.tmp"
        extract = run_cmd([
            "ffmpeg", "-y", "-i", str(video_path),
            "-vn", "-ar", "16000", "-ac", "1",
            "-f", "wav", str(tmp_path)  # .tmp extension hides the format from ffmpeg; state it
        ])
        if extract.returncode != 0 or not tmp_path.exists():
            log(
                "音频提取失败，无法检测静音窗口（视频可能无音轨）: "
                f"{_compact_ffmpeg_error(extract.stderr)}"
            )
            if tmp_path.exists():
                tmp_path.unlink()
            return []
        os.replace(str(tmp_path), str(audio_path))
        _write_audio_meta(work_dir, video_path)

    # Sentence-entry anchors use short acoustic pauses aligned to terminal ASR punctuation.
    # They are deliberately separate from silence_periods.json: a 200ms sentence pause is a
    # safe place to ENTER narration, but not a multi-second quiet window that can own a block.
    detect_speech_boundary_anchors(work_dir, asr_result or [])

    noise = CONFIG["silence_noise_threshold"]
    min_dur = CONFIG["silence_min_duration"]
    cmd = ["ffmpeg", "-i", str(audio_path),
           "-af", f"silencedetect=noise={noise}:d={min_dur}",
           "-f", "null", "-"]
    result = run_cmd(cmd, timeout=120)
    if result.returncode != 0:
        log(f"静音检测失败: {_compact_ffmpeg_error(result.stderr)}")
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

    # 与 ASR 交叉验证：标记有语音的窗口，并记录可观测 confidence/reason。
    periods, qc = annotate_quiet_windows_with_asr(
        periods,
        asr_result,
        video_duration=get_video_duration(str(audio_path)) if asr_result else None,
        configured_segment_seconds=CONFIG.get("asr_segment_seconds", 30),
    )

    (work_dir / "silence_periods.qc.json").write_text(
        json.dumps(qc, ensure_ascii=False, indent=2), encoding="utf-8")

    # 保存
    (work_dir / "silence_periods.json").write_text(
        json.dumps(periods, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"检测到 {len(periods)} 个安静窗口 (≥{quiet_min}s)")
    for qp in periods:
        flag = " [有语音]" if qp["has_speech"] else ""
        log(f"  {qp['start']:.1f}s-{qp['end']:.1f}s ({qp['duration']:.1f}s){flag}")
    return periods


def _asr_segments(asr_result):
    if isinstance(asr_result, dict):
        asr_result = asr_result.get("segments", [])
    return [item for item in (asr_result or []) if isinstance(item, dict)]


def detect_speech_boundary_anchors(work_dir, asr_result):
    """Write sentence-end entry anchors by aligning ASR punctuation to short pauses.

    MiMo ASR timestamps are window-level, not word-level. We therefore estimate each terminal
    punctuation time from its character position inside the ASR window, then snap it to the
    closest short acoustic pause. The output is guidance + a deterministic pre-TTS gate; it
    never rewrites narration timing on its own.
    """
    work_dir = Path(work_dir)
    audio_path = work_dir / "audio.wav"
    out_path = work_dir / "speech_boundary_anchors.json"
    segments = _asr_segments(asr_result)
    report = {
        "schema_version": 1,
        "artifact": "speech_boundary_anchors.json",
        "status": "completed",
        "detector": {
            "noise_threshold": CONFIG.get("source_boundary_noise_threshold", "-18dB"),
            "min_pause_seconds": float(CONFIG.get("source_boundary_min_pause", 0.12)),
            "alignment": "terminal_punctuation_to_nearest_acoustic_pause",
        },
        "sentence_anchors": [],
        "acoustic_pauses": [],
    }
    if not audio_path.exists() or not segments:
        report["status"] = "unavailable"
        report["reason"] = "missing_audio_or_asr"
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report

    result = run_cmd([
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(audio_path),
        "-af", (
            "silencedetect="
            f"noise={CONFIG.get('source_boundary_noise_threshold', '-18dB')}:"
            f"d={float(CONFIG.get('source_boundary_min_pause', 0.12))}"
        ),
        "-f", "null", "-",
    ], timeout=120)
    if result.returncode != 0:
        report["status"] = "failed"
        report["reason"] = "silencedetect_failed"
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report

    starts = [float(value) for value in re.findall(r"silence_start:\s*([\d.]+)", result.stderr or "")]
    ends = [float(value) for value in re.findall(r"silence_end:\s*([\d.]+)", result.stderr or "")]
    pauses = []
    for index, (start, end) in enumerate(zip(starts, ends)):
        if end <= start:
            continue
        pauses.append({
            "index": index,
            "start": round(start, 3),
            "end": round(end, 3),
            "midpoint": round((start + end) / 2.0, 3),
            "duration": round(end - start, 3),
        })
    report["acoustic_pauses"] = pauses

    max_error = float(CONFIG.get("source_boundary_max_alignment_error", 2.1))
    used = set()
    anchors = []
    for asr_index, segment in enumerate(segments):
        try:
            seg_start = float(segment.get("start"))
            seg_end = float(segment.get("end"))
        except (TypeError, ValueError):
            continue
        text = str(segment.get("text") or "").strip()
        if seg_end <= seg_start or not text:
            continue
        last_midpoint = seg_start - 1e-6
        for match in re.finditer(r"[。！？!?；;]", text):
            expected = seg_start + (seg_end - seg_start) * (match.end() / max(1, len(text)))
            candidates = [
                pause for pause in pauses
                if pause["index"] not in used
                and seg_start - 0.3 <= pause["midpoint"] <= seg_end + 0.3
                and pause["midpoint"] > last_midpoint
                and abs(pause["midpoint"] - expected) <= max_error
            ]
            if not candidates:
                continue
            pause = min(candidates, key=lambda item: abs(item["midpoint"] - expected))
            used.add(pause["index"])
            last_midpoint = pause["midpoint"]
            error = abs(pause["midpoint"] - expected)
            confidence = "high" if error <= 0.6 else ("medium" if error <= 1.2 else "low")
            anchors.append({
                "time": pause["end"],
                "pause_start": pause["start"],
                "pause_end": pause["end"],
                "expected_time": round(expected, 3),
                "alignment_error": round(error, 3),
                "confidence": confidence,
                "punctuation": match.group(0),
                "text_tail": text[max(0, match.end() - 32):match.end()],
                "asr_segment_index": asr_index,
            })
    report["sentence_anchors"] = sorted(anchors, key=lambda item: item["time"])
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"检测到 {len(anchors)} 个原声句末安全切入点")
    return report


def _audio_meta_path(work_dir):
    return Path(work_dir) / "audio.wav.meta.json"


def _audio_cache_matches(audio_path, video_path):
    audio_path = Path(audio_path)
    if not audio_path.exists():
        return False
    meta_path = _audio_meta_path(audio_path.parent)
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    try:
        expected = file_fingerprint(video_path)
    except OSError:
        return False
    return meta.get("source_video_fingerprint") == expected


def _write_audio_meta(work_dir, video_path):
    _audio_meta_path(work_dir).write_text(
        json.dumps({
            "schema_version": 1,
            "source_video_fingerprint": file_fingerprint(video_path),
            "audio": "audio.wav",
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
