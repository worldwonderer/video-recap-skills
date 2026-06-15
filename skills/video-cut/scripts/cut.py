"""Cut-style recap helpers for agent-selected source ranges."""

import hashlib
import json
import re
from pathlib import Path

from lib import get_video_duration, log, run_cmd


def parse_duration_seconds(value):
    """Parse seconds, 10m/1h forms, or HH:MM:SS into seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds <= 0:
            raise ValueError("duration must be positive")
        return seconds

    text = str(value).strip().lower()
    if not text:
        return None

    if ":" in text:
        parts = text.split(":")
        if len(parts) not in (2, 3):
            raise ValueError(f"invalid duration: {value}")
        try:
            nums = [float(p) for p in parts]
        except ValueError as exc:
            raise ValueError(f"invalid duration: {value}") from exc
        if any(n < 0 for n in nums):
            raise ValueError("duration must be positive")
        if nums[-1] >= 60 or (len(nums) == 3 and nums[-2] >= 60):
            raise ValueError(f"invalid duration: {value}")
        if len(nums) == 2:
            seconds = nums[0] * 60 + nums[1]
        else:
            seconds = nums[0] * 3600 + nums[1] * 60 + nums[2]
        if seconds <= 0:
            raise ValueError("duration must be positive")
        return seconds

    # One or more <number><unit> tokens: "600", "10m", "500ms", "2m30s", "1h5m30s".
    # A bare number is read as seconds; units may be combined (compound durations).
    factors = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}
    sign = 1.0
    body = text
    if body[:1] in "+-":
        sign = -1.0 if body[0] == "-" else 1.0
        body = body[1:]
    token_re = re.compile(r"([0-9]+(?:\.[0-9]+)?)(ms|s|m|h)?")
    pos = 0
    seconds = 0.0
    matched = False
    for m in token_re.finditer(body):
        if m.start() != pos:
            break
        pos = m.end()
        matched = True
        seconds += float(m.group(1)) * factors[m.group(2) or "s"]
    if not matched or pos != len(body):
        raise ValueError(f"invalid duration: {value}")
    seconds *= sign
    if seconds <= 0:
        raise ValueError("duration must be positive")
    return seconds


def _clip_value(raw, *names):
    for name in names:
        if name in raw:
            return raw[name]
    return None


def load_clip_plan(path):
    """Load `clip_plan.json`, accepting either a list or {"clips": [...]} object."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _stable_json_dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def value_fingerprint(value):
    """Return a stable fingerprint for JSON-serializable non-secret values."""
    return hashlib.md5(_stable_json_dumps(value).encode("utf-8")).hexdigest()


def cut_plan_fingerprint(validated_plan):
    """Hash the exact normalized clip plan that determines edited_source.mp4 bytes."""
    if isinstance(validated_plan, dict):
        payload = dict(validated_plan)
        # Provenance for raw-plan freshness is not part of the edited media bytes.
        payload.pop("raw_plan_fingerprint", None)
    else:
        payload = validated_plan
    return value_fingerprint(payload)


def file_fingerprint(path, chunk_size=1024 * 1024):
    """Full-content fingerprint for source media cache provenance."""
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def _edited_source_meta_path(output_path):
    return Path(str(output_path) + ".meta.json")


def _load_edited_source_meta(output_path):
    meta_path = _edited_source_meta_path(output_path)
    if not meta_path.exists():
        return None
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_edited_source_meta(output_path, validated_plan, input_video):
    meta_path = _edited_source_meta_path(output_path)
    meta_path.write_text(json.dumps({
        "schema_version": 1,
        "clip_plan_fingerprint": cut_plan_fingerprint(validated_plan),
        "source_video_fingerprint": file_fingerprint(input_video),
        "edited_source_fingerprint": file_fingerprint(output_path),
        "total_duration": validated_plan.get("total_duration"),
        "clip_count": len(validated_plan.get("clips", [])),
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def should_reuse_edited_source(output_path, validated_plan, input_video):
    """Return True only when edited_source.mp4 matches source media and cut params."""
    output_path = Path(output_path)
    if not output_path.exists():
        return False
    meta = _load_edited_source_meta(output_path)
    return bool(
        meta
        and meta.get("clip_plan_fingerprint") == cut_plan_fingerprint(validated_plan)
        and meta.get("source_video_fingerprint") == file_fingerprint(input_video)
        and meta.get("edited_source_fingerprint") == file_fingerprint(output_path)
    )


def normalize_clip_plan(raw_plan, video_duration, target_duration=None, clip_padding=0.0, min_clip_duration=0.3, allow_overlap=False):
    """Validate and enrich an agent-authored clip plan.

    Returns a dict with validated `clips`, `total_duration`, and target metadata.
    Clip order follows the agent-provided order, so montage ordering is possible.
    """
    if isinstance(raw_plan, dict):
        raw_clips = raw_plan.get("clips", [])
        plan_target = raw_plan.get("target_duration") or raw_plan.get("target_duration_seconds")
        if target_duration is None and plan_target not in (None, ""):
            target_duration = parse_duration_seconds(plan_target)
    elif isinstance(raw_plan, list):
        raw_clips = raw_plan
    else:
        raise ValueError("clip_plan.json must be a JSON array or an object with a clips array")

    if not isinstance(raw_clips, list):
        raise ValueError("clip_plan.json field `clips` must be an array")

    video_duration = max(0.0, float(video_duration or 0.0))
    padding = max(0.0, float(clip_padding or 0.0))
    min_duration = max(0.05, float(min_clip_duration or 0.05))
    clips = []
    source_ranges = []
    cursor = 0.0

    for idx, raw in enumerate(raw_clips):
        if not isinstance(raw, dict):
            log(f"  跳过无效 clip #{idx + 1}: not an object")
            continue
        try:
            raw_start = float(_clip_value(raw, "start", "source_start", "in"))
            raw_end = float(_clip_value(raw, "end", "source_end", "out"))
        except (TypeError, ValueError):
            log(f"  跳过无效 clip #{idx + 1}: missing numeric start/end")
            continue
        if raw_end - raw_start < min_duration:
            log(f"  跳过过短 clip #{idx + 1}: {raw_start:.1f}-{raw_end:.1f}s")
            continue
        start = round(max(0.0, min(raw_start - padding, video_duration)), 3)
        end = round(max(0.0, min(raw_end + padding, video_duration)), 3)
        if end - start < min_duration:
            log(f"  跳过过短 clip #{idx + 1}: {start:.1f}-{end:.1f}s")
            continue
        overlaps = [r for r in source_ranges if start < r[1] and end > r[0]]
        if overlaps and not allow_overlap:
            raise ValueError(
                f"clip #{idx + 1} overlaps an earlier source range; "
                "split or remove duplicate source footage before mapping narration"
            )
        source_ranges.append((start, end))

        duration = round(end - start, 3)
        clip = {
            "clip_id": len(clips),
            "source_start": start,
            "source_end": end,
            "output_start": round(cursor, 3),
            "output_end": round(cursor + duration, 3),
            "duration": duration,
            "reason": str(raw.get("reason", raw.get("note", ""))).strip(),
        }
        clips.append(clip)
        cursor += duration

    if not clips:
        raise ValueError("clip_plan.json has no valid clips")

    total_duration = round(sum(c["duration"] for c in clips), 3)
    plan = {
        "clips": clips,
        "total_duration": total_duration,
        "target_duration": round(float(target_duration), 3) if target_duration else None,
        "source_duration": round(video_duration, 3),
        "allow_overlap": bool(allow_overlap),
    }
    if target_duration and total_duration > target_duration * 1.15:
        plan["warning"] = (
            f"validated clips total {total_duration:.1f}s exceeds target "
            f"{float(target_duration):.1f}s by more than 15%"
        )
        log(f"警告: {plan['warning']}")
    return plan


def source_time_to_output_time(source_time, clips):
    """Map a source timestamp into the post-concat output timeline."""
    ts = float(source_time)
    for clip in clips:
        start = clip["source_start"]
        end = clip["source_end"]
        if start <= ts <= end:
            mapped = clip["output_start"] + (ts - start)
            return round(max(clip["output_start"], min(mapped, clip["output_end"])), 3)
    return None


def _clips_for_midpoint(start, end, clips):
    mid = (float(start) + float(end)) / 2
    return [clip for clip in clips if clip["source_start"] <= mid <= clip["source_end"]]


def map_narration_to_clips(narration, validated_plan, min_duration=0.3):
    """Convert source-time narration segments to edited-output timeline segments."""
    clips = validated_plan["clips"] if isinstance(validated_plan, dict) else validated_plan
    mapped = []
    for raw in narration or []:
        if not isinstance(raw, dict):
            continue
        try:
            source_start = float(raw.get("start"))
            source_end = float(raw.get("end"))
        except (TypeError, ValueError):
            continue
        text = str(raw.get("narration", "")).strip()
        if source_end <= source_start or not text:
            continue
        if raw.get("source_clip_id") is not None:
            try:
                requested_clip_id = int(raw.get("source_clip_id"))
            except (TypeError, ValueError):
                requested_clip_id = None
            clip = next((c for c in clips if c.get("clip_id") == requested_clip_id), None)
            if clip and not (clip["source_start"] <= ((source_start + source_end) / 2) <= clip["source_end"]):
                clip = None
        else:
            matches = _clips_for_midpoint(source_start, source_end, clips)
            if len(matches) > 1:
                log(f"  丢弃重复片段中未标 source_clip_id 的解说: {source_start:.1f}-{source_end:.1f}s")
                continue
            clip = matches[0] if matches else None
        if not clip:
            log(f"  丢弃未落入剪辑片段的解说: {source_start:.1f}-{source_end:.1f}s")
            continue
        clipped_source_start = max(source_start, clip["source_start"])
        clipped_source_end = min(source_end, clip["source_end"])
        if clipped_source_end - clipped_source_start < min_duration:
            log(f"  丢弃过短映射解说: {source_start:.1f}-{source_end:.1f}s")
            continue
        output_start = source_time_to_output_time(clipped_source_start, [clip])
        output_end = source_time_to_output_time(clipped_source_end, [clip])
        if output_start is None or output_end is None or output_end <= output_start:
            continue
        item = dict(raw)
        item["source_start"] = round(clipped_source_start, 3)
        item["source_end"] = round(clipped_source_end, 3)
        item["source_clip_id"] = clip["clip_id"]
        item["start"] = output_start
        item["end"] = output_end
        mapped.append(item)

    mapped.sort(key=lambda seg: seg["start"])
    return mapped


def lint_mapped_narration(mapped, original_count, output_duration, *, min_spm=6.0, max_gap_seconds=12.0, drop_ratio_limit=0.3):
    """Advisory re-lint of narration AFTER it is mapped onto the cut OUTPUT timeline.

    The mapper silently drops beats whose midpoint is outside every kept clip and clamps
    boundary-crossers, so a narration authored against the full source can pass the
    source-time validate yet leave the cut sparse or describing footage the viewer never
    sees. This surfaces that on the real output timeline (loud log + narration_mapped_lint.json);
    it never blocks the render — a sparse recap can be intentional.
    """
    mapped = sorted(mapped or [], key=lambda s: float(s.get("start", 0.0)))
    mapped_count = len(mapped)
    original_count = int(original_count or 0)
    dropped = max(0, original_count - mapped_count)
    drop_ratio = dropped / original_count if original_count else 0.0
    out_dur = float(output_duration or 0.0)
    spm = mapped_count / (out_dur / 60) if out_dur > 0 else 0.0
    gaps = [float(b["start"]) - float(a["end"]) for a, b in zip(mapped, mapped[1:])]
    max_gap = max(gaps) if gaps else 0.0
    covered = sum(max(0.0, float(b["end"]) - float(b["start"])) for b in mapped)
    coverage = covered / out_dur if out_dur > 0 else 0.0

    warnings = []
    if drop_ratio >= drop_ratio_limit:
        warnings.append({
            "code": "many_beats_dropped",
            "message": "大量解说段落落在保留片段之外被丢弃——请按保留的片段写解说，而不是整段原片。",
            "dropped": dropped, "original": original_count, "drop_ratio": round(drop_ratio, 2),
        })
    if mapped_count >= 2 and spm and spm < min_spm:
        warnings.append({
            "code": "low_density_output",
            "message": "映射后的解说在成片里偏稀疏——在保留片段内补充解说 beat。",
            "segments_per_minute": round(spm, 2), "min_segments_per_minute": min_spm,
        })
    if max_gap > max_gap_seconds:
        warnings.append({
            "code": "long_gap_output",
            "message": "成片里有一长段没有解说。",
            "max_gap_seconds": round(max_gap, 2), "max_gap_limit_seconds": max_gap_seconds,
        })
    return {
        "mapped_count": mapped_count,
        "dropped": dropped,
        "drop_ratio": round(drop_ratio, 2),
        "output_duration": round(out_dur, 2),
        "segments_per_minute": round(spm, 2),
        "max_gap_seconds": round(max_gap, 2),
        "coverage": round(coverage, 2),
        "warnings": warnings,
    }


def _has_audio_stream(video_path):
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=index", "-of", "csv=p=0", str(video_path),
    ]
    result = run_cmd(cmd)
    return result.returncode == 0 and bool(result.stdout.strip())


def _write_filter_script(filter_complex, work_dir):
    script_path = Path(work_dir) / "edit_filter_complex.txt"
    script_path.write_text(filter_complex, encoding="utf-8")
    return script_path


def build_edited_source_video(input_video, validated_plan, work_dir, output_path=None):
    """Build `edited_source.mp4` by concatenating validated source ranges."""
    work_dir = Path(work_dir)
    output_path = Path(output_path or work_dir / "edited_source.mp4")
    clips = validated_plan["clips"]
    if not clips:
        raise ValueError("validated clip plan has no clips")

    has_audio = _has_audio_stream(input_video)
    parts = []
    concat_inputs = []
    for clip in clips:
        idx = clip["clip_id"]
        start = clip["source_start"]
        end = clip["source_end"]
        parts.append(
            f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS[v{idx}]"
        )
        concat_inputs.append(f"[v{idx}]")
        if has_audio:
            parts.append(
                f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[a{idx}]"
            )
            concat_inputs.append(f"[a{idx}]")

    if has_audio:
        parts.append("".join(concat_inputs) + f"concat=n={len(clips)}:v=1:a=1[v][a]")
        maps = ["-map", "[v]", "-map", "[a]"]
        extra_inputs = []
    else:
        total = validated_plan.get("total_duration") or sum(c["duration"] for c in clips)
        parts.append("".join(concat_inputs) + f"concat=n={len(clips)}:v=1:a=0[v]")
        maps = ["-map", "[v]", "-map", "1:a", "-shortest"]
        extra_inputs = ["-f", "lavfi", "-t", f"{float(total):.3f}", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]

    filter_complex = ";".join(parts)
    if len(filter_complex.encode("utf-8")) > 7000:
        filter_script = _write_filter_script(filter_complex, work_dir)
        filter_args = ["-filter_complex_script", str(filter_script)]
    else:
        filter_args = ["-filter_complex", filter_complex]

    cmd = ["ffmpeg", "-y", "-i", str(input_video), *extra_inputs, *filter_args, *maps,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "192k", str(output_path)]
    result = run_cmd(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"剪辑源视频失败: {result.stderr}")

    _write_edited_source_meta(output_path, validated_plan, input_video)
    duration = get_video_duration(output_path)
    log(f"剪辑源视频: {output_path} ({duration:.1f}s, {len(clips)} clips)")
    return output_path


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="video-cut: build an edited source video from an agent clip plan and map narration onto the cut timeline.")
    parser.add_argument("video", help="source video path")
    parser.add_argument("--work-dir", required=True, help="dir holding clip_plan.json (and optionally narration.json)")
    parser.add_argument("--clip-plan", default=None, help="clip plan json (default: <work-dir>/clip_plan.json)")
    parser.add_argument("--narration", default=None, help="narration json to map (default: <work-dir>/narration.json)")
    parser.add_argument("--target-duration", default=None, help="target output duration, e.g. 10m / 600 / 00:10:00")
    parser.add_argument("--clip-padding", type=float, default=0.0, help="seconds to pad each clip on both ends")
    parser.add_argument("--allow-overlap", action="store_true", help="allow overlapping/duplicate source ranges")
    args = parser.parse_args()

    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    clip_plan_path = Path(args.clip_plan) if args.clip_plan else work_dir / "clip_plan.json"
    raw_plan = load_clip_plan(clip_plan_path)

    target_seconds = parse_duration_seconds(args.target_duration) if args.target_duration else None
    validated_plan = normalize_clip_plan(
        raw_plan,
        get_video_duration(args.video),
        target_duration=target_seconds,
        clip_padding=args.clip_padding,
        allow_overlap=args.allow_overlap,
    )
    if isinstance(validated_plan, dict):
        validated_plan["raw_plan_fingerprint"] = value_fingerprint(raw_plan)
    (work_dir / "clip_plan_validated.json").write_text(
        json.dumps(validated_plan, ensure_ascii=False, indent=2), encoding="utf-8")

    edited_source_path = work_dir / "edited_source.mp4"
    if should_reuse_edited_source(edited_source_path, validated_plan, args.video):
        log(f"复用剪辑源视频: {edited_source_path}")
    else:
        build_edited_source_video(args.video, validated_plan, work_dir, edited_source_path)

    narration_path = Path(args.narration) if args.narration else work_dir / "narration.json"
    if narration_path.exists():
        narration = json.loads(narration_path.read_text(encoding="utf-8"))
        mapped = map_narration_to_clips(narration, validated_plan)
        if not mapped:
            raise SystemExit("narration 没有落入 clip_plan 片段内的有效解说")
        (work_dir / "narration_mapped.json").write_text(
            json.dumps(mapped, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"映射解说 {len(mapped)} 段 → narration_mapped.json")
        report = lint_mapped_narration(mapped, len(narration), validated_plan["total_duration"])
        (work_dir / "narration_mapped_lint.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        for w in report["warnings"]:
            log(f"  ⚠️ 剪后解说同步: {w['message']} [{w['code']}]")
    log(f"剪辑模式: {len(validated_plan['clips'])} 个片段 → {validated_plan['total_duration']:.1f}s")


if __name__ == "__main__":
    main()
