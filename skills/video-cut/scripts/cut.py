"""Cut-style recap helpers for agent-selected source ranges."""

import hashlib
import json
import re
import subprocess
from pathlib import Path

from lib import CONFIG, get_video_duration, log, run_cmd


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


def _source_fingerprints_for_plan(validated_plan, input_video=None):
    """Fingerprint every media file that can affect edited_source.mp4 bytes."""
    paths = []
    for clip in (validated_plan.get("clips", []) if isinstance(validated_plan, dict) else []):
        if clip.get("source_path"):
            paths.append(str(clip["source_path"]))
    if not paths and input_video is not None:
        paths.append(str(input_video))
    fingerprints = {}
    for path in sorted(set(paths)):
        fingerprints[str(Path(path))] = file_fingerprint(path)
    return fingerprints


def _write_edited_source_meta(output_path, validated_plan, input_video=None):
    meta_path = _edited_source_meta_path(output_path)
    source_fingerprints = _source_fingerprints_for_plan(validated_plan, input_video)
    has_plan_sources = any(
        clip.get("source_path")
        for clip in (validated_plan.get("clips", []) if isinstance(validated_plan, dict) else [])
    )
    legacy_source_fp = (
        file_fingerprint(input_video)
        if input_video is not None and not has_plan_sources and len(source_fingerprints) == 1
        else None
    )
    meta = {
        "schema_version": 2,
        "clip_plan_fingerprint": cut_plan_fingerprint(validated_plan),
        "source_fingerprints": source_fingerprints,
        "edited_source_fingerprint": file_fingerprint(output_path),
        "total_duration": validated_plan.get("total_duration"),
        "clip_count": len(validated_plan.get("clips", [])),
    }
    # Preserve the legacy key for existing single-source callers/tests/metadata readers.
    if legacy_source_fp is not None:
        meta["source_video_fingerprint"] = legacy_source_fp
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def should_reuse_edited_source(output_path, validated_plan, input_video=None):
    """Return True only when edited_source.mp4 matches source media and cut params."""
    output_path = Path(output_path)
    if not output_path.exists():
        return False
    meta = _load_edited_source_meta(output_path)
    if not meta or meta.get("clip_plan_fingerprint") != cut_plan_fingerprint(validated_plan):
        return False
    expected_sources = _source_fingerprints_for_plan(validated_plan, input_video)
    meta_sources = meta.get("source_fingerprints")
    if meta_sources is None and input_video is not None:
        meta_sources = {str(Path(input_video)): meta.get("source_video_fingerprint")}
    return bool(
        meta_sources == expected_sources
        and meta.get("edited_source_fingerprint") == file_fingerprint(output_path)
    )



def _manifest_source_entries(sources_manifest):
    """Return source rows from common multi-source manifest shapes."""
    if isinstance(sources_manifest, dict):
        if isinstance(sources_manifest.get("sources"), list):
            return sources_manifest["sources"]
        rows = []
        for source_id, value in sources_manifest.items():
            if source_id in {"schema_version", "version"}:
                continue
            if isinstance(value, dict):
                row = dict(value)
                row.setdefault("source_id", source_id)
                rows.append(row)
        if rows:
            return rows
    elif isinstance(sources_manifest, list):
        return sources_manifest
    raise ValueError("sources manifest must be a list, a {sources:[...]} object, or a source_id map")


def normalize_sources_manifest(sources_manifest):
    """Normalize source manifest rows to {source_id: {source_path, duration}}."""
    sources = {}
    for idx, raw in enumerate(_manifest_source_entries(sources_manifest)):
        if not isinstance(raw, dict):
            raise ValueError(f"source #{idx + 1} must be an object")
        source_id = raw.get("source_id", raw.get("id", raw.get("name")))
        if source_id in (None, ""):
            raise ValueError(f"source #{idx + 1} is missing source_id")
        source_id = str(source_id)
        source_path = raw.get("source_path", raw.get("path", raw.get("video_path", raw.get("video", raw.get("file")))))
        if not source_path:
            raise ValueError(f"source {source_id} is missing source_path/path")
        duration = raw.get("duration", raw.get("duration_seconds", raw.get("source_duration")))
        if duration in (None, ""):
            duration = get_video_duration(source_path)
        try:
            duration = max(0.0, float(duration or 0.0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"source {source_id} has invalid duration") from exc
        sources[source_id] = {"source_id": source_id, "source_path": str(source_path), "duration": duration}
    if not sources:
        raise ValueError("sources manifest has no sources")
    return sources


def normalize_multi_source_clip_plan(raw_plan, sources_manifest, target_duration=None, clip_padding=0.0, min_clip_duration=0.3, allow_overlap=False):
    """Validate a multi-source clip plan and map source_id clips to source paths/durations.

    Clip order follows the raw plan; overlap validation is isolated per source_id.
    """
    sources = normalize_sources_manifest(sources_manifest)
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

    padding = max(0.0, float(clip_padding or 0.0))
    min_duration = max(0.05, float(min_clip_duration or 0.05))
    clips = []
    source_ranges = {}
    cursor = 0.0

    for idx, raw in enumerate(raw_clips):
        if not isinstance(raw, dict):
            log(f"  跳过无效 clip #{idx + 1}: not an object")
            continue
        source_id = raw.get("source_id", raw.get("id"))
        if source_id in (None, ""):
            raise ValueError(f"clip #{idx + 1} is missing source_id")
        source_id = str(source_id)
        source = sources.get(source_id)
        if not source:
            raise ValueError(f"clip #{idx + 1} references unknown source_id: {source_id}")
        try:
            raw_start = float(_clip_value(raw, "start", "source_start", "in"))
            raw_end = float(_clip_value(raw, "end", "source_end", "out"))
        except (TypeError, ValueError):
            log(f"  跳过无效 clip #{idx + 1}: missing numeric start/end")
            continue
        if raw_end - raw_start < min_duration:
            log(f"  跳过过短 clip #{idx + 1}: {raw_start:.1f}-{raw_end:.1f}s")
            continue
        source_duration = source["duration"]
        start = round(max(0.0, min(raw_start - padding, source_duration)), 3)
        end = round(max(0.0, min(raw_end + padding, source_duration)), 3)
        if end - start < min_duration:
            log(f"  跳过过短 clip #{idx + 1}: {start:.1f}-{end:.1f}s")
            continue
        ranges = source_ranges.setdefault(source_id, [])
        overlaps = [r for r in ranges if start < r[1] and end > r[0]]
        if overlaps and not allow_overlap:
            raise ValueError(
                f"clip #{idx + 1} overlaps an earlier source range for source_id {source_id}; "
                "split or remove duplicate source footage before mapping narration"
            )
        ranges.append((start, end))

        duration = round(end - start, 3)
        clip = {
            "clip_id": len(clips),
            "source_id": source_id,
            "source_path": source["source_path"],
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
        "sources": {sid: {"source_path": s["source_path"], "duration": round(s["duration"], 3)} for sid, s in sources.items()},
        "allow_overlap": bool(allow_overlap),
    }
    if target_duration and total_duration > target_duration * 1.15:
        plan["warning"] = (
            f"validated clips total {total_duration:.1f}s exceeds target "
            f"{float(target_duration):.1f}s by more than 15%"
        )
        log(f"警告: {plan['warning']}")
    return plan

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


def snap_clip_ends_to_lines(plan, silence_periods, video_duration, max_extend):
    """Extend each clip's source_end forward to the next natural pause, preventing mid-sentence cuts.

    - If silence_periods is empty/None, returns plan unchanged.
    - If a clip's source_end already falls inside a quiet window, no snap is applied.
    - Otherwise, extends to the next quiet window start, capped by max_extend and video_duration.
    - When plan["allow_overlap"] is False, will not extend into another clip's source range.
    - After snapping, recomputes output_start/output_end/duration for all clips cursor-based.
    - Returns the updated plan dict (all other keys preserved).
    """
    # silence_periods is loaded from disk; tolerate a stale/hand-edited row by keeping only
    # well-formed quiet windows so a single bad entry can't KeyError-abort the whole cut.
    silence_periods = [
        w for w in (silence_periods or [])
        if isinstance(w, dict) and isinstance(w.get("start"), (int, float))
        and isinstance(w.get("end"), (int, float))
    ]
    if not silence_periods:
        return plan

    clips = [dict(c) for c in plan["clips"]]
    video_duration = float(video_duration)
    max_extend = float(max_extend)
    allow_overlap = bool(plan.get("allow_overlap", False))

    for i, clip in enumerate(clips):
        source_end = clip["source_end"]

        # Already inside a quiet window → already at a natural pause.
        if any(w["start"] <= source_end <= w["end"] for w in silence_periods):
            continue

        # Find the next quiet window start at or after source_end.
        candidates = [w["start"] for w in silence_periods if w["start"] >= source_end]
        if not candidates:
            continue
        next_quiet_start = min(candidates)

        # Only snap if the next pause is within reach (≤ max_extend away).
        if next_quiet_start > source_end + max_extend:
            continue
        candidate_end = min(next_quiet_start, video_duration)
        if candidate_end <= source_end:
            continue

        # When overlaps are forbidden, cap against every other clip's source range.
        if not allow_overlap:
            other_starts = [
                c["source_start"] for j, c in enumerate(clips)
                if j != i and c["source_start"] > source_end
            ]
            if other_starts:
                nearest_other_start = min(other_starts)
                candidate_end = min(candidate_end, nearest_other_start)
            if candidate_end <= source_end:
                continue

        clip["source_end"] = round(candidate_end, 3)

    # Recompute output timeline cursor-based (same cursor logic as normalize_clip_plan).
    cursor = 0.0
    for clip in clips:
        duration = round(clip["source_end"] - clip["source_start"], 3)
        clip["duration"] = duration
        clip["output_start"] = round(cursor, 3)
        clip["output_end"] = round(cursor + duration, 3)
        cursor += duration

    result = dict(plan)
    result["clips"] = clips
    result["total_duration"] = round(sum(c["duration"] for c in clips), 3)
    return result


def _detect_shot_changes(video, win_start, win_end, threshold, lead=0.25):
    """Absolute source-time hard cuts inside [win_start, win_end] via ffmpeg's scene metric.

    Input-seek to a little before the window: the rebased output PTS restarts at ~0 at the seek
    target, so `seek + pts_time` recovers absolute source time. The `lead` keeps the seek/keyframe
    settling artifact frame outside [win_start, win_end] so it is filtered out, not mistaken for a
    cut. Returns [] on any ffmpeg trouble (advisory pass must never block the cut).
    """
    win_start = max(0.0, float(win_start))
    win_end = max(win_start, float(win_end))
    if win_end - win_start < 1e-3:
        return []
    seek = max(0.0, win_start - lead)
    dur = (win_end - seek) + 0.1
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{seek:.3f}", "-i", str(video),
           "-t", f"{dur:.3f}", "-an", "-sn",
           "-filter:v", f"select='gt(scene,{threshold})',showinfo", "-f", "null", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except (OSError, ValueError):
        return []
    changes = []
    for m in re.finditer(r"pts_time:([0-9.]+)", proc.stderr or ""):
        t = seek + float(m.group(1))
        if win_start <= t <= win_end:
            changes.append(round(t, 3))
    return sorted(set(changes))


def snap_clips_off_shot_changes(plan, video, video_duration, margin, threshold, min_keep=0.5):
    """Nudge each clip's boundaries clear of the ORIGINAL footage's hard cuts to avoid 闪烁.

    A clip whose source_start sits just before a shot-change opens on a brief sliver of the old
    shot that then hard-cuts again; one whose source_end sits just after a shot-change closes on a
    sliver of the next shot. Both flash at the edit point. So:
      - move source_start FORWARD onto a shot-change in (start, start+margin]  → clean open
      - move source_end   BACK   onto a shot-change in [end-margin, end)       → clean close
    Boundaries already on a cut, or with no nearby cut, are left untouched. Snaps that would shrink
    a clip below `min_keep` are skipped. Recomputes the output timeline cursor-based. Advisory: any
    detection failure leaves that boundary as-is.
    """
    margin = float(margin)
    if margin <= 0 or not plan.get("clips"):
        return plan
    clips = [dict(c) for c in plan["clips"]]
    n_start = n_end = 0
    for clip in clips:
        s = float(clip["source_start"])
        e = float(clip["source_end"])
        new_s, new_e = s, e
        # Opening: a shot-change just AFTER source_start leaves an old-shot sliver before it.
        start_changes = [c for c in _detect_shot_changes(video, s, min(e, s + margin), threshold)
                         if c > s + 1e-3]
        if start_changes:
            cand = max(start_changes)          # open after the last rapid cut in the window
            if cand < e - min_keep:
                new_s = round(cand, 3)
        # Closing: a shot-change just BEFORE source_end leaves a next-shot sliver after it.
        end_changes = [c for c in _detect_shot_changes(video, max(new_s, e - margin), e, threshold)
                       if c < e - 1e-3]
        if end_changes:
            cand = min(end_changes)            # close before the first rapid cut in the window
            if cand > new_s + min_keep:
                new_e = round(cand, 3)
        n_start += new_s != s
        n_end += new_e != e
        clip["source_start"] = new_s
        clip["source_end"] = new_e

    # Recompute output timeline cursor-based (same cursor logic as snap_clip_ends_to_lines).
    cursor = 0.0
    for clip in clips:
        duration = round(clip["source_end"] - clip["source_start"], 3)
        clip["duration"] = duration
        clip["output_start"] = round(cursor, 3)
        clip["output_end"] = round(cursor + duration, 3)
        cursor += duration

    if n_start or n_end:
        log(f"避让原片切镜头: {n_start} 个起点前移、{n_end} 个终点回收 (margin={margin}s, 阈值={threshold})")
    result = dict(plan)
    result["clips"] = clips
    result["total_duration"] = round(sum(c["duration"] for c in clips), 3)
    return result


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
        # Tag beats trimmed to a clip edge: their TEXT was written for a longer span and may
        # now describe footage that was cut away (a stale-text desync the lint surfaces).
        item["clamped"] = bool(
            clipped_source_start > source_start + 1e-3 or clipped_source_end < source_end - 1e-3
        )
        mapped.append(item)

    mapped.sort(key=lambda seg: seg["start"])
    return mapped


def lint_mapped_narration(mapped, original_count, output_duration, *, min_spm=6.0, max_gap_seconds=12.0, drop_ratio_limit=0.3):
    """Advisory re-lint of narration AFTER it is mapped onto the cut OUTPUT timeline.

    The mapper silently drops beats whose midpoint is outside every kept clip and clamps
    boundary-crossers, so a narration authored against the full source can pass the
    source-time validate yet leave the cut sparse or describing footage the viewer never
    sees. This surfaces that on the real output timeline (narration_mapped_lint.json) and
    returns a `blocking` verdict (heavy drop / too sparse / long gap) that the cut stage
    enforces unless --allow-sparse-cut. Clamped-but-kept beats are surfaced as advisory only.
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
    clamped = [b for b in mapped if isinstance(b, dict) and b.get("clamped")]
    if clamped:
        warnings.append({
            "code": "clamped_beats",
            "message": "有解说段被裁到片段边界，文本可能在描述被剪掉的画面——核对并改写这些行。",
            "count": len(clamped),
        })
    blocking_codes = {"many_beats_dropped", "low_density_output", "long_gap_output"}
    return {
        "mapped_count": mapped_count,
        "dropped": dropped,
        "drop_ratio": round(drop_ratio, 2),
        "output_duration": round(out_dur, 2),
        "segments_per_minute": round(spm, 2),
        "max_gap_seconds": round(max_gap, 2),
        "coverage": round(coverage, 2),
        "clamped_count": len(clamped),
        "warnings": warnings,
        "blocking": any(w["code"] in blocking_codes for w in warnings),
    }


def _has_audio_stream(video_path):
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=index", "-of", "csv=p=0", str(video_path),
    ]
    result = run_cmd(cmd)
    return result.returncode == 0 and bool(result.stdout.strip())


def _probe_video_geometry(video_path):
    """Best-effort (width, height, fps) for a source; falls back to a safe even-sized canvas.

    Used to normalize heterogeneous multi-source segments to one geometry before concat
    (ffmpeg's concat filter rejects mismatched width/height/SAR/pixel-format/fps).
    """
    width = height = 0
    fps = 0.0
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate",
        "-of", "csv=p=0:s=,", str(video_path),
    ]
    try:
        result = run_cmd(cmd)
    except Exception:  # noqa: BLE001 - probing is best-effort; fall back to defaults
        result = None
    if result is not None and getattr(result, "returncode", 1) == 0 and (result.stdout or "").strip():
        parts = result.stdout.strip().splitlines()[0].split(",")
        try:
            width, height = int(float(parts[0])), int(float(parts[1]))
        except (IndexError, ValueError):
            width = height = 0
        if len(parts) >= 3 and "/" in parts[2]:
            num, _, den = parts[2].partition("/")
            try:
                num_f, den_f = float(num), float(den)
                fps = num_f / den_f if den_f > 0 else 0.0
            except ValueError:
                fps = 0.0
    if width <= 0 or height <= 0:
        width, height = 1280, 720
    width -= width % 2          # libx264/yuv420p require even dimensions
    height -= height % 2
    if not 0 < fps <= 120:
        fps = 30.0
    return width, height, round(fps, 3)


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

    source_paths = []
    for clip in clips:
        source_path = str(clip.get("source_path") or input_video)
        if source_path not in source_paths:
            source_paths.append(source_path)
    source_index = {path: idx for idx, path in enumerate(source_paths)}
    audio_by_input = {path: _has_audio_stream(path) for path in source_paths}

    parts = []
    concat_inputs = []
    extra_inputs = []
    if len(source_paths) > 1:
        # Distinct sources almost always differ in resolution/SAR/fps/pixel-format (and
        # some may lack audio), which the bare concat filter rejects. Normalize every video
        # segment to one canvas and give every clip an audio segment (real or synthesized
        # silence) so concat always succeeds with a continuous track and no source's audio
        # is dropped just because a sibling source is silent.
        canvas_w, canvas_h, canvas_fps = _probe_video_geometry(source_paths[0])
        vnorm = (f"scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=decrease,"
                 f"pad={canvas_w}:{canvas_h}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
                 f"fps={canvas_fps},format=yuv420p")
        for clip in clips:
            idx = clip["clip_id"]
            clip_source = str(clip.get("source_path") or input_video)
            input_idx = source_index[clip_source]
            start = clip["source_start"]
            end = clip["source_end"]
            dur = max(0.0, float(end) - float(start))
            parts.append(
                f"[{input_idx}:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS,{vnorm}[v{idx}]"
            )
            if audio_by_input.get(clip_source):
                parts.append(
                    f"[{input_idx}:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS,"
                    f"aresample=48000,aformat=sample_rates=48000:channel_layouts=stereo[a{idx}]"
                )
            else:
                parts.append(
                    f"anullsrc=r=48000:cl=stereo,atrim=duration={dur:.3f},asetpts=PTS-STARTPTS,"
                    f"aformat=sample_rates=48000:channel_layouts=stereo[a{idx}]"
                )
            concat_inputs.append(f"[v{idx}][a{idx}]")
        parts.append("".join(concat_inputs) + f"concat=n={len(clips)}:v=1:a=1[v][a]")
        maps = ["-map", "[v]", "-map", "[a]"]
    else:
        has_audio = all(audio_by_input.values())
        for clip in clips:
            idx = clip["clip_id"]
            input_idx = source_index[str(clip.get("source_path") or input_video)]
            start = clip["source_start"]
            end = clip["source_end"]
            parts.append(
                f"[{input_idx}:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS[v{idx}]"
            )
            concat_inputs.append(f"[v{idx}]")
            if has_audio:
                parts.append(
                    f"[{input_idx}:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[a{idx}]"
                )
                concat_inputs.append(f"[a{idx}]")

        if has_audio:
            parts.append("".join(concat_inputs) + f"concat=n={len(clips)}:v=1:a=1[v][a]")
            maps = ["-map", "[v]", "-map", "[a]"]
        else:
            total = validated_plan.get("total_duration") or sum(c["duration"] for c in clips)
            parts.append("".join(concat_inputs) + f"concat=n={len(clips)}:v=1:a=0[v]")
            maps = ["-map", "[v]", "-map", f"{len(source_paths)}:a", "-shortest"]
            extra_inputs = ["-f", "lavfi", "-t", f"{float(total):.3f}", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]

    filter_complex = ";".join(parts)
    if len(filter_complex.encode("utf-8")) > 7000:
        filter_script = _write_filter_script(filter_complex, work_dir)
        filter_args = ["-filter_complex_script", str(filter_script)]
    else:
        filter_args = ["-filter_complex", filter_complex]

    input_args = []
    for source_path in source_paths:
        input_args.extend(["-i", str(source_path)])
    cmd = ["ffmpeg", "-y", *input_args, *extra_inputs, *filter_args, *maps,
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
    parser.add_argument("--sources-manifest", default=None,
                        help="multi-source manifest json mapping source_id values to source media")
    parser.add_argument("--narration", default=None, help="narration json to map (default: <work-dir>/narration.json)")
    parser.add_argument("--target-duration", default=None, help="target output duration, e.g. 10m / 600 / 00:10:00")
    parser.add_argument("--clip-padding", type=float, default=0.0, help="seconds to pad each clip on both ends")
    parser.add_argument("--allow-overlap", action="store_true", help="allow overlapping/duplicate source ranges")
    parser.add_argument("--normalize-only", action="store_true",
                        help="only normalize the clip plan -> clip_plan_validated.json (no render/map); "
                             "lets validate lint the SAME padded/pruned plan the mapper uses")
    parser.add_argument("--no-narration-map", action="store_true",
                        help="render edited_source.mp4 but do NOT map narration.json onto the cut "
                             "(cut-first/narrate-second: narration is authored in OUTPUT time, no mapping)")
    parser.add_argument("--allow-sparse-cut", action="store_true",
                        help="do not block on heavy narration drop / sparse output (e.g. an intentional montage)")
    args = parser.parse_args()

    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    clip_plan_path = Path(args.clip_plan) if args.clip_plan else work_dir / "clip_plan.json"
    raw_plan = load_clip_plan(clip_plan_path)

    target_seconds = parse_duration_seconds(args.target_duration) if args.target_duration else None
    sources_manifest = json.loads(Path(args.sources_manifest).read_text(encoding="utf-8")) if args.sources_manifest else None
    if sources_manifest is not None:
        validated_plan = normalize_multi_source_clip_plan(
            raw_plan,
            sources_manifest,
            target_duration=target_seconds,
            clip_padding=args.clip_padding,
            allow_overlap=args.allow_overlap,
        )
        video_duration = None
    else:
        video_duration = get_video_duration(args.video)
        validated_plan = normalize_clip_plan(
            raw_plan,
            video_duration,
            target_duration=target_seconds,
            clip_padding=args.clip_padding,
            allow_overlap=args.allow_overlap,
        )

    if sources_manifest is None and CONFIG.get("snap_clip_line_end", True):
        silence_path = work_dir / "silence_periods.json"
        silence_periods = []
        if silence_path.exists():
            try:
                silence_periods = json.loads(silence_path.read_text(encoding="utf-8"))
                if not isinstance(silence_periods, list):
                    silence_periods = []
            except (OSError, ValueError):
                silence_periods = []
        validated_plan = snap_clip_ends_to_lines(
            validated_plan,
            silence_periods,
            video_duration,
            CONFIG.get("clip_snap_max_extend", 2.0),
        )

    # Keep boundaries off the original footage's hard cuts (avoids 闪烁 at the edit point).
    # Runs after the line-snap so it refines the final source ranges; video-clean wins on the rare
    # boundary that is both in a quiet window and beside a shot-change.
    if sources_manifest is None and CONFIG.get("scene_cut_snap", True):
        validated_plan = snap_clips_off_shot_changes(
            validated_plan,
            args.video,
            video_duration,
            CONFIG.get("scene_cut_snap_margin", 0.5),
            CONFIG.get("scene_cut_detect_threshold", 0.4),
        )

    if isinstance(validated_plan, dict):
        validated_plan["raw_plan_fingerprint"] = value_fingerprint(raw_plan)
    (work_dir / "clip_plan_validated.json").write_text(
        json.dumps(validated_plan, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.normalize_only:
        print(json.dumps({"status": "normalized", "clips": len(validated_plan["clips"]),
                          "total_duration": validated_plan["total_duration"]}, ensure_ascii=False))
        return

    edited_source_path = work_dir / "edited_source.mp4"
    if should_reuse_edited_source(edited_source_path, validated_plan, args.video):
        log(f"复用剪辑源视频: {edited_source_path}")
    else:
        build_edited_source_video(args.video, validated_plan, work_dir, edited_source_path)

    narration_path = Path(args.narration) if args.narration else work_dir / "narration.json"
    if narration_path.exists() and not args.no_narration_map:
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
        if report.get("blocking") and not args.allow_sparse_cut:
            raise SystemExit(
                "剪后解说与保留片段对不上：丢弃过多或成片过稀疏。改 narration.json / clip_plan.json "
                "让解说落在保留片段内后重跑，或加 --allow-sparse-cut 接受当前映射。详见 narration_mapped_lint.json")
    log(f"剪辑模式: {len(validated_plan['clips'])} 个片段 → {validated_plan['total_duration']:.1f}s")


if __name__ == "__main__":
    main()
