"""Cut-style recap helpers for agent-selected source ranges."""

import hashlib
import json
import re
import subprocess
from pathlib import Path

from lib import CONFIG, get_video_duration, log, run_cmd


EDITED_SOURCE_RENDER_ALGORITHM_VERSION = "edited-source-render-v3"
GEOMETRY_RENDER_ALGORITHM_VERSION = "geometry-weighted-orientation-area-fps-v2"


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
        # QC is observability derived from the media plan, not an input range decision.
        payload.pop("qc", None)
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


def edited_source_render_cache_payload():
    """Render-affecting settings that invalidate edited_source.mp4 cache reuse.

    Keep this payload limited to inputs/algorithms that can change rendered media bytes.
    Observational QC produced after validation/render is intentionally excluded.
    """
    return {
        "render_algorithm_version": EDITED_SOURCE_RENDER_ALGORITHM_VERSION,
        "geometry_render_algorithm_version": GEOMETRY_RENDER_ALGORITHM_VERSION,
        "clip_join_audio_fade_ms": round(max(0.0, float(CONFIG.get("clip_join_audio_fade_ms", 30.0) or 0.0)), 3),
    }


def edited_source_render_fingerprint():
    return value_fingerprint(edited_source_render_cache_payload())


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
        "render_fingerprint": edited_source_render_fingerprint(),
        "render_cache": edited_source_render_cache_payload(),
        "source_fingerprints": source_fingerprints,
        "edited_source_fingerprint": file_fingerprint(output_path),
        "total_duration": validated_plan.get("total_duration"),
        "clip_count": len(validated_plan.get("clips", [])),
    }
    delivery_qc = (validated_plan.get("qc") or {}).get("delivery_qc")
    if delivery_qc:
        meta["delivery_qc"] = delivery_qc
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
    if meta.get("render_fingerprint") != edited_source_render_fingerprint():
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


def _valid_silence_windows(silence_periods):
    """Return sorted well-formed quiet windows as {start,end} floats."""
    windows = []
    for w in silence_periods or []:
        if not isinstance(w, dict):
            continue
        try:
            start = float(w.get("start"))
            end = float(w.get("end"))
        except (TypeError, ValueError):
            continue
        if end >= start:
            windows.append({"start": start, "end": end})
    return sorted(windows, key=lambda w: (w["start"], w["end"]))


def _recompute_clip_timeline(clips):
    cursor = 0.0
    for clip in clips:
        duration = round(float(clip["source_end"]) - float(clip["source_start"]), 3)
        clip["duration"] = duration
        clip["output_start"] = round(cursor, 3)
        clip["output_end"] = round(cursor + duration, 3)
        cursor += duration
    return round(cursor, 3)


def _candidate_overlaps(clips, idx, new_start, new_end):
    for j, other in enumerate(clips):
        if j == idx:
            continue
        if new_start < float(other["source_end"]) and new_end > float(other["source_start"]):
            return True
    return False


def snap_clip_starts_to_lines(plan, silence_periods, video_duration, max_prepend,
                              max_trim=0.35, min_clip_duration=0.3):
    """Snap clip starts to natural quiet boundaries, preferring safe prepend over trim.

    Policy:
    - start already inside a quiet window: keep.
    - speech start: prepend to nearest prior quiet window end if within max_prepend.
    - no usable prior quiet: keep and warn, except an extremely near next quiet start
      (<= max_trim) may trim forward if duration/overlap safety holds.
    - never overlap/collapse when allow_overlap is false; unsafe attempts keep-and-warn.
    """
    silence_periods = _valid_silence_windows(silence_periods)
    if not silence_periods:
        return plan

    clips = [dict(c) for c in plan["clips"]]
    video_duration = max(0.0, float(video_duration or 0.0))
    max_prepend = max(0.0, float(max_prepend or 0.0))
    max_trim = max(0.0, float(max_trim or 0.0))
    min_duration = max(0.05, float(min_clip_duration or 0.05))
    allow_overlap = bool(plan.get("allow_overlap", False))
    events = []

    for i, clip in enumerate(clips):
        original_start = float(clip["source_start"])
        source_end = float(clip["source_end"])
        event = {
            "clip_id": clip.get("clip_id", i),
            "source_id": clip.get("source_id"),
            "original_start": round(original_start, 3),
            "action": "kept",
        }

        if any(w["start"] <= original_start <= w["end"] for w in silence_periods):
            event["reason"] = "already_quiet"
            events.append(event)
            continue

        prior_ends = [w["end"] for w in silence_periods if w["end"] <= original_start]
        if prior_ends:
            candidate_start = max(prior_ends)
            if original_start - candidate_start <= max_prepend:
                candidate_start = round(max(0.0, candidate_start), 3)
                safe = candidate_start < source_end - min_duration + 1e-9
                if safe and not allow_overlap:
                    safe = not _candidate_overlaps(clips, i, candidate_start, source_end)
                if safe:
                    clip["source_start"] = candidate_start
                    event.update({
                        "action": "prepended",
                        "new_start": candidate_start,
                        "delta": round(original_start - candidate_start, 3),
                    })
                    events.append(event)
                    continue
                reason = "overlap_or_collapse"
            else:
                reason = "prior_quiet_too_far"
        else:
            reason = "no_prior_quiet"

        next_starts = [w["start"] for w in silence_periods if w["start"] >= original_start]
        if next_starts:
            candidate_start = min(next_starts)
            trim_delta = candidate_start - original_start
            if 0 < trim_delta <= max_trim:
                candidate_start = round(min(video_duration, candidate_start), 3)
                safe = source_end - candidate_start >= min_duration
                if safe and not allow_overlap:
                    safe = not _candidate_overlaps(clips, i, candidate_start, source_end)
                if safe:
                    clip["source_start"] = candidate_start
                    event.update({
                        "action": "trimmed",
                        "new_start": candidate_start,
                        "delta": round(trim_delta, 3),
                        "fallback_from": reason,
                    })
                    events.append(event)
                    continue
                reason = "unsafe_forward_trim"

        event["start_unsnapped_reason"] = reason
        event["warning_code"] = "clip_start_unsnapped"
        events.append(event)

    total_duration = _recompute_clip_timeline(clips)
    result = dict(plan)
    result["clips"] = clips
    result["total_duration"] = total_duration
    qc = dict(result.get("qc") or {})
    boundary = dict(qc.get("boundary_status") or {})
    boundary["start_snaps"] = events
    qc["boundary_status"] = boundary
    warnings = list(qc.get("warnings") or [])
    for event in events:
        if event.get("warning_code"):
            warnings.append({
                "code": event["warning_code"],
                "clip_id": event["clip_id"],
                "source_id": event.get("source_id"),
                "start_unsnapped_reason": event.get("start_unsnapped_reason"),
            })
    if warnings:
        qc["warnings"] = warnings
    result["qc"] = qc
    return result


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
    silence_periods = _valid_silence_windows(silence_periods)
    if not silence_periods:
        return plan

    clips = [dict(c) for c in plan["clips"]]
    video_duration = float(video_duration)
    max_extend = float(max_extend)
    allow_overlap = bool(plan.get("allow_overlap", False))
    events = []

    for i, clip in enumerate(clips):
        source_end = clip["source_end"]
        event = {
            "clip_id": clip.get("clip_id", i),
            "source_id": clip.get("source_id"),
            "original_end": round(float(source_end), 3),
            "action": "kept",
        }

        # Already inside a quiet window → already at a natural pause.
        if any(w["start"] <= source_end <= w["end"] for w in silence_periods):
            event["reason"] = "already_quiet"
            events.append(event)
            continue

        # Find the next quiet window start at or after source_end.
        candidates = [w["start"] for w in silence_periods if w["start"] >= source_end]
        if not candidates:
            event["end_unsnapped_reason"] = "no_next_quiet"
            events.append(event)
            continue
        next_quiet_start = min(candidates)

        # Only snap if the next pause is within reach (≤ max_extend away).
        if next_quiet_start > source_end + max_extend:
            event["end_unsnapped_reason"] = "next_quiet_too_far"
            events.append(event)
            continue
        candidate_end = min(next_quiet_start, video_duration)
        if candidate_end <= source_end:
            event["end_unsnapped_reason"] = "non_forward_candidate"
            events.append(event)
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
                event["end_unsnapped_reason"] = "overlap_or_collapse"
                events.append(event)
                continue

        clip["source_end"] = round(candidate_end, 3)
        event.update({
            "action": "extended",
            "new_end": clip["source_end"],
            "delta": round(float(clip["source_end"]) - float(source_end), 3),
        })
        events.append(event)

    # Recompute output timeline cursor-based (same cursor logic as normalize_clip_plan).
    total_duration = _recompute_clip_timeline(clips)

    result = dict(plan)
    result["clips"] = clips
    result["total_duration"] = total_duration
    qc = dict(result.get("qc") or {})
    boundary = dict(qc.get("boundary_status") or {})
    boundary["end_snaps"] = events
    qc["boundary_status"] = boundary
    result["qc"] = qc
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


def snap_clips_off_shot_changes(plan, video, margin, threshold, min_keep=0.5):
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
    events = []
    for i, clip in enumerate(clips):
        s = float(clip["source_start"])
        e = float(clip["source_end"])
        new_s, new_e = s, e
        event = {
            "clip_id": clip.get("clip_id", i),
            "source_id": clip.get("source_id"),
            "original_start": round(s, 3),
            "original_end": round(e, 3),
            "start_action": "kept",
            "end_action": "kept",
        }
        # Opening: a shot-change just AFTER source_start leaves an old-shot sliver before it.
        start_changes = [c for c in _detect_shot_changes(video, s, min(e, s + margin), threshold)
                         if c > s + 1e-3]
        if start_changes:
            cand = max(start_changes)          # open after the last rapid cut in the window
            if cand < e - min_keep:
                new_s = round(cand, 3)
                event["start_action"] = "moved_forward"
                event["new_start"] = new_s
            else:
                event["start_unsnapped_reason"] = "collapse"
        # Closing: a shot-change just BEFORE source_end leaves a next-shot sliver after it.
        end_changes = [c for c in _detect_shot_changes(video, max(new_s, e - margin), e, threshold)
                       if c < e - 1e-3]
        if end_changes:
            cand = min(end_changes)            # close before the first rapid cut in the window
            if cand > new_s + min_keep:
                new_e = round(cand, 3)
                event["end_action"] = "moved_back"
                event["new_end"] = new_e
            else:
                event["end_unsnapped_reason"] = "collapse"
        n_start += new_s != s
        n_end += new_e != e
        clip["source_start"] = new_s
        clip["source_end"] = new_e
        events.append(event)

    # Recompute output timeline cursor-based (same cursor logic as snap_clip_ends_to_lines).
    total_duration = _recompute_clip_timeline(clips)

    if n_start or n_end:
        log(f"避让原片切镜头: {n_start} 个起点前移、{n_end} 个终点回收 (margin={margin}s, 阈值={threshold})")
    result = dict(plan)
    result["clips"] = clips
    result["total_duration"] = total_duration
    qc = dict(result.get("qc") or {})
    boundary = dict(qc.get("boundary_status") or {})
    boundary["shot_snaps"] = events
    qc["boundary_status"] = boundary
    result["qc"] = qc
    return result


def _load_silence_for_source(work_dir, source_id, source_work_dir=None):
    """Read a source's silence_periods.json from the multi-source work layout (best-effort)."""
    candidates = []
    if source_work_dir:
        candidates.append(Path(work_dir) / source_work_dir / "silence_periods.json")
    candidates.append(Path(work_dir) / "sources" / str(source_id) / "silence_periods.json")
    for path in candidates:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return []
            return data if isinstance(data, list) else []
    return []


def snap_multi_source_clips(plan, sources, work_dir, *, line_max_extend, scene_margin,
                            scene_threshold, do_line_snap=True, do_scene_snap=True,
                            start_max_prepend=None, start_max_trim=None):
    """Per-source line/shot snapping for a multi-source validated plan.

    Each clip is snapped using ITS OWN source's silence windows / shot changes and duration
    (a clip in source B never constrains a clip in source A), then the global OUTPUT timeline
    is recomputed once in plan order. Advisory: missing silence data or ffmpeg trouble leaves a
    boundary unchanged. Mirrors the single-source snap_clip_ends_to_lines + snap_clips_off_shot_changes.
    """
    clips = plan.get("clips") or []
    if not clips or not (do_line_snap or do_scene_snap):
        return plan
    allow_overlap = bool(plan.get("allow_overlap", False))
    groups = {}
    for clip in clips:
        groups.setdefault(str(clip.get("source_id")), []).append(clip)
    boundary_accum = {"start_snaps": [], "end_snaps": [], "shot_snaps": []}
    for sid, group in groups.items():
        source = sources.get(sid, {}) if isinstance(sources, dict) else {}
        duration = float(source.get("duration") or 0.0) or max(
            (float(c["source_end"]) for c in group), default=0.0)
        mini = {"clips": [dict(c) for c in group], "allow_overlap": allow_overlap}
        if do_line_snap:
            silence = _load_silence_for_source(work_dir, sid, source.get("source_work_dir"))
            if start_max_prepend is not None:
                mini = snap_clip_starts_to_lines(
                    mini, silence, duration, start_max_prepend,
                    max_trim=start_max_trim if start_max_trim is not None else 0.35,
                )
            mini = snap_clip_ends_to_lines(mini, silence, duration, line_max_extend)
        if do_scene_snap and source.get("source_path"):
            mini = snap_clips_off_shot_changes(
                mini, source["source_path"], scene_margin, scene_threshold
            )
        mini_boundary = ((mini.get("qc") or {}).get("boundary_status") or {})
        for key in boundary_accum:
            boundary_accum[key].extend([e for e in mini_boundary.get(key, []) if isinstance(e, dict)])
        for original, snapped in zip(group, mini["clips"]):
            original["source_start"] = snapped["source_start"]
            original["source_end"] = snapped["source_end"]
    # Recompute the global output timeline cursor-based, in plan order (not group order).
    plan["total_duration"] = _recompute_clip_timeline(clips)

    # Preserve per-source boundary QC as the single validated-plan QC source.
    if any(boundary_accum.values()):
        qc = plan.setdefault("qc", {})
        boundary = qc.setdefault("boundary_status", {})
        for key, events in boundary_accum.items():
            if events:
                boundary.setdefault(key, []).extend(events)
        warnings = qc.setdefault("warnings", [])
        for event in boundary_accum["start_snaps"]:
            if event.get("warning_code"):
                warnings.append({
                    "code": event["warning_code"],
                    "clip_id": event.get("clip_id"),
                    "source_id": event.get("source_id"),
                    "start_unsnapped_reason": event.get("start_unsnapped_reason"),
                })
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


def update_cut_qc(plan, *, allow_duration_drift=False, duration_drift_allowed_by=None):
    """Populate clip_plan_validated.json['qc'] as the single cut QC source."""
    qc = dict(plan.get("qc") or {})
    warnings = list(qc.get("warnings") or [])
    blocking = list(qc.get("blocking") or [])
    total = float(plan.get("total_duration") or 0.0)
    target = plan.get("target_duration")
    if target in (None, ""):
        target_status = "missing"
        target_qc = {"status": target_status, "target_duration": None, "total_duration": round(total, 3)}
    else:
        target = float(target)
        ratio = total / target if target > 0 else 0.0
        if ratio < 0.85:
            target_status = "under"
        elif ratio > 1.15:
            target_status = "over"
        else:
            target_status = "ok"
        severity = None
        if ratio < 0.60 or ratio > 1.40:
            severity = "blocking"
        elif target_status in {"under", "over"}:
            severity = "warning"
        target_qc = {
            "status": target_status,
            "target_duration": round(target, 3),
            "total_duration": round(total, 3),
            "ratio": round(ratio, 3),
            "warning_thresholds": {"under": 0.85, "over": 1.15},
            "blocking_thresholds": {"under": 0.60, "over": 1.40},
        }
        if severity:
            warning = {
                "code": "target_duration_drift",
                "status": target_status,
                "severity": "warning" if allow_duration_drift else severity,
                "target_duration": round(target, 3),
                "total_duration": round(total, 3),
                "ratio": round(ratio, 3),
            }
            if allow_duration_drift:
                warning["allowed"] = True
                warning["duration_drift_allowed_by"] = duration_drift_allowed_by or "--allow-duration-drift"
                target_qc["duration_drift_allowed_by"] = warning["duration_drift_allowed_by"]
            warnings.append(warning)
            if severity == "blocking" and not allow_duration_drift:
                blocking.append(warning)
    qc["target_duration_status"] = target_status
    qc["target_duration"] = target_qc
    qc.setdefault("boundary_status", {})
    qc["clip_count"] = len(plan.get("clips") or [])
    qc["total_duration"] = round(total, 3)
    if warnings:
        qc["warnings"] = warnings
    if blocking:
        qc["blocking"] = blocking
    elif "blocking" in qc:
        qc.pop("blocking", None)
    plan["qc"] = qc
    return plan


def _has_audio_stream(video_path):
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=index", "-of", "csv=p=0", str(video_path),
    ]
    result = run_cmd(cmd)
    return result.returncode == 0 and bool(result.stdout.strip())


class VideoGeometry(tuple):
    """Tuple-compatible geometry with probe facts attached for QC callers."""

    def __new__(cls, width, height, fps, facts=None):
        obj = super().__new__(cls, (width, height, fps))
        obj.facts = facts or {}
        return obj


def _parse_ratio(value):
    if value in (None, "", "0:1", "0/1", "N/A"):
        return None
    text = str(value)
    sep = ":" if ":" in text else "/" if "/" in text else None
    if not sep:
        try:
            ratio = float(text)
            return ratio if ratio > 0 else None
        except ValueError:
            return None
    left, _, right = text.partition(sep)
    try:
        num, den = float(left), float(right)
    except ValueError:
        return None
    return num / den if den > 0 and num > 0 else None


def _stream_rotation(stream):
    candidates = []
    tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
    if "rotate" in tags:
        candidates.append(tags.get("rotate"))
    for side_data in stream.get("side_data_list") or []:
        if isinstance(side_data, dict):
            candidates.append(side_data.get("rotation"))
    for value in candidates:
        try:
            return int(round(float(value))) % 360
        except (TypeError, ValueError):
            continue
    return 0


def _fps_from_rate(rate):
    if not rate or "/" not in str(rate):
        return 0.0
    num, _, den = str(rate).partition("/")
    try:
        num_f, den_f = float(num), float(den)
        return num_f / den_f if den_f > 0 else 0.0
    except ValueError:
        return 0.0


def _geometry_from_stream(stream, *, fallback=False):
    coded_width = coded_height = 0
    try:
        coded_width = int(float(stream.get("width") or 0))
        coded_height = int(float(stream.get("height") or 0))
    except (TypeError, ValueError):
        coded_width = coded_height = 0
    if coded_width <= 0 or coded_height <= 0:
        coded_width, coded_height = 1280, 720
        fallback = True

    parsed_sar = _parse_ratio(stream.get("sample_aspect_ratio"))
    dar = _parse_ratio(stream.get("display_aspect_ratio"))
    rotation = _stream_rotation(stream)
    display_height = float(coded_height)
    if parsed_sar:
        sar = parsed_sar
        display_width = float(coded_width) * sar
        aspect_source = "sample_aspect_ratio"
    elif dar:
        sar = 1.0
        display_width = display_height * dar
        aspect_source = "display_aspect_ratio_fallback"
    else:
        sar = 1.0
        display_width = float(coded_width)
        aspect_source = "square_pixel_fallback"
    rotation_swaps_axes = rotation in {90, 270}
    if rotation_swaps_axes:
        display_width, display_height = display_height, display_width

    width, height = _clamp_even_geometry(round(display_width), round(display_height))
    fps = _fps_from_rate(stream.get("r_frame_rate")) or _fps_from_rate(stream.get("avg_frame_rate"))
    if not 0 < fps <= 120:
        fps = 30.0
    facts = {
        "coded_width": coded_width,
        "coded_height": coded_height,
        "width": width,
        "height": height,
        "fps": round(fps, 3),
        "sample_aspect_ratio": stream.get("sample_aspect_ratio") or "1:1",
        "sample_aspect_ratio_float": round(float(sar), 6),
        "display_aspect_ratio": stream.get("display_aspect_ratio"),
        "display_aspect_ratio_float": round(float(dar or 0.0), 6),
        "display_aspect_source": aspect_source,
        "display_width": width,
        "display_height": height,
        "rotation": rotation,
        "rotation_swaps_axes": rotation_swaps_axes,
        "fallback": bool(fallback),
    }
    return VideoGeometry(width, height, round(fps, 3), facts)


def _probe_video_geometry(video_path):
    """Best-effort iterable (width, height, fps), rotation/SAR/DAR-aware.

    Returned value unpacks like the historical 3-tuple while exposing `.facts`
    for QC. Used to normalize heterogeneous multi-source segments to one
    square-pixel geometry before concat (ffmpeg's concat filter rejects
    mismatched width/height/SAR/pixel-format/fps).
    """
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries",
        "stream=width,height,r_frame_rate,avg_frame_rate,sample_aspect_ratio,display_aspect_ratio:stream_tags=rotate:stream_side_data=rotation",
        "-of", "json", str(video_path),
    ]
    try:
        result = run_cmd(cmd)
    except Exception:  # noqa: BLE001 - probing is best-effort; fall back to defaults
        result = None
    if result is not None and getattr(result, "returncode", 1) == 0 and (result.stdout or "").strip():
        try:
            payload = json.loads(result.stdout)
            streams = payload.get("streams") or []
            if streams:
                return _geometry_from_stream(streams[0])
        except (AttributeError, TypeError, ValueError):
            pass

    # Backward-compatible fallback for tests/mocks that still return CSV output.
    stream = {}
    if result is not None and (result.stdout or "").strip():
        parts = result.stdout.strip().splitlines()[0].split(",")
        if len(parts) >= 2:
            stream["width"], stream["height"] = parts[0], parts[1]
        if len(parts) >= 3:
            stream["r_frame_rate"] = parts[2]
        if len(parts) >= 4 and parts[3] not in ("", "N/A"):
            stream["tags"] = {"rotate": parts[3]}
        if len(parts) >= 5 and parts[4] not in ("", "N/A"):
            stream["sample_aspect_ratio"] = parts[4]
        if len(parts) >= 6 and parts[5] not in ("", "N/A"):
            stream["display_aspect_ratio"] = parts[5]
    return _geometry_from_stream(stream, fallback=True)


def _orientation(width, height):
    if width > height:
        return "landscape"
    if height > width:
        return "portrait"
    return "square"


def _fps_bucket(fps):
    if not fps or fps <= 0:
        return 30.0
    common = [23.976, 24.0, 25.0, 29.97, 30.0, 50.0, 59.94, 60.0]
    nearest = min(common, key=lambda x: abs(float(fps) - x))
    return nearest if abs(float(fps) - nearest) <= 0.15 else round(float(fps))


def _clamp_even_geometry(width, height, max_height=None):
    width = max(2, int(width) - int(width) % 2)
    height = max(2, int(height) - int(height) % 2)
    max_height = int(max_height or 0)
    if max_height > 0 and height > max_height:
        scale = max_height / height
        height = max_height - max_height % 2
        width = max(2, int(width * scale))
        width -= width % 2
    return width, height


def _select_output_geometry(source_paths, clips, max_height=None):
    """Deterministically select canvas/fps from all used sources, not just the first."""
    used = {}
    for clip in clips or []:
        path = str(clip.get("source_path") or "")
        if not path:
            continue
        used[path] = used.get(path, 0.0) + max(0.0, float(clip.get("duration") or 0.0))
    if not used:
        for path in source_paths or []:
            used[str(path)] = 0.0
    rows = []
    for path in sorted(used):
        probed = _probe_video_geometry(path)
        width, height, fps = probed
        facts = dict(getattr(probed, "facts", {}) or {})
        facts.setdefault("width", width)
        facts.setdefault("height", height)
        facts.setdefault("fps", fps)
        facts.setdefault("rotation", 0)
        facts.setdefault("sample_aspect_ratio", "1:1")
        rows.append({
            "path": path,
            "source_id": next((str(c.get("source_id")) for c in clips or []
                               if str(c.get("source_path") or "") == path and c.get("source_id") is not None), None),
            "used_duration": round(used[path], 3),
            "width": width,
            "height": height,
            "coded_width": facts.get("coded_width", width),
            "coded_height": facts.get("coded_height", height),
            "display_width": facts.get("display_width", width),
            "display_height": facts.get("display_height", height),
            "area": width * height,
            "fps": fps,
            "fps_bucket": min(60.0, _fps_bucket(fps)),
            "orientation": _orientation(width, height),
            "rotation": facts.get("rotation", 0),
            "sample_aspect_ratio": facts.get("sample_aspect_ratio", "1:1"),
            "sample_aspect_ratio_float": facts.get("sample_aspect_ratio_float", 1.0),
            "display_aspect_ratio": facts.get("display_aspect_ratio"),
            "rotation_swaps_axes": bool(facts.get("rotation_swaps_axes", False)),
        })
    if not rows:
        return 1280, 720, 30.0, {
            "width": 1280, "height": 720, "fps": 30.0,
            "reason": "fallback_no_sources", "source_id": None,
        }

    orientation_duration = {}
    for row in rows:
        orientation_duration[row["orientation"]] = orientation_duration.get(row["orientation"], 0.0) + row["used_duration"]
    chosen_orientation = sorted(
        orientation_duration.items(),
        key=lambda kv: (kv[1], max(r["area"] for r in rows if r["orientation"] == kv[0]), kv[0]),
        reverse=True,
    )[0][0]
    eligible = [r for r in rows if r["orientation"] == chosen_orientation] or rows
    selected = sorted(
        eligible,
        key=lambda r: (-r["area"], str(r.get("source_id") or ""), r["path"]),
    )[0]

    fps_duration = {}
    for row in rows:
        fps_duration[row["fps_bucket"]] = fps_duration.get(row["fps_bucket"], 0.0) + row["used_duration"]
    fps = sorted(fps_duration.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)[0][0]
    fps = max(1.0, min(60.0, float(fps or 30.0)))

    width, height = _clamp_even_geometry(selected["width"], selected["height"], max_height=max_height)
    reason = {
        "width": width,
        "height": height,
        "fps": round(fps, 3),
        "reason": "weighted_orientation_area_fps",
        "source_id": selected.get("source_id"),
        "source_path": selected["path"],
        "orientation": chosen_orientation,
        "orientation_used_duration": round(orientation_duration.get(chosen_orientation, 0.0), 3),
        "fps_bucket_used_duration": round(fps_duration.get(fps, 0.0), 3),
        "rotation": selected.get("rotation", 0),
        "sample_aspect_ratio": selected.get("sample_aspect_ratio", "1:1"),
        "display_aspect_ratio": selected.get("display_aspect_ratio"),
        "coded_width": selected.get("coded_width"),
        "coded_height": selected.get("coded_height"),
        "display_width": selected.get("display_width"),
        "display_height": selected.get("display_height"),
        "sources": rows,
    }
    return width, height, round(fps, 3), reason


def _audio_segment_filter(label_in, label_out, start, end, duration, fade_ms, extra_filters=""):
    fade = max(0.0, min(float(fade_ms or 0.0) / 1000.0, max(0.0, float(duration or 0.0)) / 2))
    base = f"{label_in}atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS"
    if fade > 0:
        base += f",afade=t=in:st=0:d={fade:.3f},afade=t=out:st={max(0.0, duration - fade):.3f}:d={fade:.3f}"
    if extra_filters:
        base += f",{extra_filters}"
    return f"{base}{label_out}"


def _write_filter_script(filter_complex, work_dir):
    script_path = Path(work_dir) / "edit_filter_complex.txt"
    script_path.write_text(filter_complex, encoding="utf-8")
    return script_path


def _probe_audio_sample_rate(video_path):
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=sample_rate", "-of", "csv=p=0", str(video_path),
    ]
    try:
        result = run_cmd(cmd)
    except Exception:  # noqa: BLE001 - delivery QC is observational
        return None
    if result.returncode != 0:
        return None
    try:
        return int(float((result.stdout or "").strip().splitlines()[0]))
    except (IndexError, TypeError, ValueError):
        return None


def _delivery_reencode_reason(source_paths, clips):
    reasons = ["trim_concat_filter_requires_reencode"]
    if len(source_paths or []) > 1:
        reasons.append("multi_source_geometry_audio_normalization")
    if any(not clip.get("source_path") for clip in clips or []):
        reasons.append("single_source_filter_concat_no_stream_copy")
    return "+".join(reasons)


def update_delivery_qc(validated_plan, *, source_paths=None, output_path=None, rendered=False):
    """Attach cut delivery facts to qc.delivery_qc without writing visual_qc."""
    qc = validated_plan.setdefault("qc", {})
    clips = validated_plan.get("clips") or []
    if source_paths is None:
        source_paths = sorted({
            str(clip.get("source_path") or "")
            for clip in clips
            if str(clip.get("source_path") or "")
        })
    target_sample_rate = 48000
    probed_sample_rate = _probe_audio_sample_rate(output_path) if output_path and Path(output_path).exists() else None
    output_geometry = qc.get("output_geometry")
    delivery_qc = {
        "schema_version": 1,
        "video_encode_passes": 1,
        "reencode_reason": _delivery_reencode_reason(source_paths, clips),
        "stream_copy_risk": {
            "status": "avoided",
            "reason": "cut uses trim/concat/filtergraph with explicit libx264/aac encode; no risky stream-copy path",
        },
        "audio_sample_rate": {
            "target": target_sample_rate,
            "probed": probed_sample_rate,
        },
        "final_compat_notes": [
            "video encoded with libx264/yuv420p-compatible filter path",
            "audio encoded as AAC with 48000 Hz target for delivery compatibility",
            "edited_source.mp4 is an intermediate; downstream assembly may perform another intentional encode",
        ],
        "output_geometry": output_geometry,
        "rendered": bool(rendered),
        "planned": not bool(rendered),
    }
    if probed_sample_rate and probed_sample_rate != target_sample_rate:
        delivery_qc["final_compat_notes"].append(
            f"probed audio sample rate {probed_sample_rate} differs from target {target_sample_rate}"
        )
    qc["delivery_qc"] = delivery_qc
    return delivery_qc


def write_cut_delivery_qc(work_dir, validated_plan):
    delivery_qc = (validated_plan.get("qc") or {}).get("delivery_qc")
    if not delivery_qc or not delivery_qc.get("rendered"):
        return None
    path = Path(work_dir) / "cut_delivery_qc.json"
    path.write_text(json.dumps(delivery_qc, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


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
    join_fade_ms = max(0.0, float(CONFIG.get("clip_join_audio_fade_ms", 30.0) or 0.0))
    qc = validated_plan.setdefault("qc", {})
    qc["join_fade_ms"] = round(join_fade_ms, 3)
    if not qc.get("output_geometry"):
        _, _, _, geometry_qc = _select_output_geometry(source_paths, clips)
        qc["output_geometry"] = geometry_qc
        qc["output_geometry_reason"] = geometry_qc.get("reason")

    parts = []
    concat_inputs = []
    extra_inputs = []
    if len(source_paths) > 1:
        # Distinct sources almost always differ in resolution/SAR/fps/pixel-format (and
        # some may lack audio), which the bare concat filter rejects. Normalize every video
        # segment to one canvas and give every clip an audio segment (real or synthesized
        # silence) so concat always succeeds with a continuous track and no source's audio
        # is dropped just because a sibling source is silent.
        # Reuse the geometry already probed+stored above (guard) or by main() — the
        # selection is deterministic, so re-probing every source here is wasted ffprobe work.
        geometry_qc = qc.get("output_geometry")
        if isinstance(geometry_qc, dict) and all(geometry_qc.get(k) for k in ("width", "height", "fps")):
            canvas_w, canvas_h, canvas_fps = int(geometry_qc["width"]), int(geometry_qc["height"]), geometry_qc["fps"]
        else:
            canvas_w, canvas_h, canvas_fps, geometry_qc = _select_output_geometry(source_paths, clips)
            qc["output_geometry"] = geometry_qc
            qc["output_geometry_reason"] = geometry_qc.get("reason")
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
                parts.append(_audio_segment_filter(
                    f"[{input_idx}:a]", f"[a{idx}]", start, end, dur, join_fade_ms,
                    extra_filters="aresample=48000,aformat=sample_rates=48000:channel_layouts=stereo",
                ))
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
                parts.append(_audio_segment_filter(
                    f"[{input_idx}:a]", f"[a{idx}]", start, end, max(0.0, float(end) - float(start)), join_fade_ms
                ))
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
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", str(output_path)]
    result = run_cmd(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"剪辑源视频失败: {result.stderr}")

    update_delivery_qc(validated_plan, source_paths=source_paths, output_path=output_path, rendered=True)
    write_cut_delivery_qc(work_dir, validated_plan)
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
    parser.add_argument("--allow-duration-drift", action="store_true",
                        help="do not block when validated clip duration is far from --target-duration")
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
        validated_plan = snap_clip_starts_to_lines(
            validated_plan,
            silence_periods,
            video_duration,
            CONFIG.get("clip_start_snap_max_prepend", 1.8),
            max_trim=CONFIG.get("clip_start_snap_max_trim", 0.35),
        )
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
            CONFIG.get("scene_cut_snap_margin", 0.5),
            CONFIG.get("scene_cut_detect_threshold", 0.4),
        )

    # Multi-source: snap each clip against ITS OWN source's pauses/shot-changes (single-source
    # snaps above can't, since silence_periods.json and args.video are per-project, not per-source).
    if sources_manifest is not None and (
        CONFIG.get("snap_clip_line_end", True) or CONFIG.get("scene_cut_snap", True)
    ):
        validated_plan = snap_multi_source_clips(
            validated_plan,
            validated_plan.get("sources", {}),
            work_dir,
            line_max_extend=CONFIG.get("clip_snap_max_extend", 2.0),
            scene_margin=CONFIG.get("scene_cut_snap_margin", 0.5),
            scene_threshold=CONFIG.get("scene_cut_detect_threshold", 0.4),
            do_line_snap=CONFIG.get("snap_clip_line_end", True),
            do_scene_snap=CONFIG.get("scene_cut_snap", True),
            start_max_prepend=CONFIG.get("clip_start_snap_max_prepend", 1.8),
            start_max_trim=CONFIG.get("clip_start_snap_max_trim", 0.35),
        )

    if isinstance(validated_plan, dict):
        validated_plan["raw_plan_fingerprint"] = value_fingerprint(raw_plan)
        validated_plan.setdefault("qc", {})["join_fade_ms"] = round(
            max(0.0, float(CONFIG.get("clip_join_audio_fade_ms", 30.0) or 0.0)), 3
        )
        source_paths = []
        for clip in validated_plan.get("clips", []):
            source_path = str(clip.get("source_path") or "")
            if source_path and source_path not in source_paths:
                source_paths.append(source_path)
        if not source_paths:
            source_paths = [str(args.video)]
        _, _, _, geometry_qc = _select_output_geometry(source_paths, validated_plan.get("clips", []))
        validated_plan["qc"]["output_geometry"] = geometry_qc
        validated_plan["qc"]["output_geometry_reason"] = geometry_qc.get("reason")
        allow_duration_drift = bool(args.allow_duration_drift or args.allow_sparse_cut)
        drift_source = "--allow-duration-drift" if args.allow_duration_drift else (
            "--allow-sparse-cut" if args.allow_sparse_cut else None
        )
        update_cut_qc(
            validated_plan,
            allow_duration_drift=allow_duration_drift,
            duration_drift_allowed_by=drift_source,
        )
        update_delivery_qc(validated_plan, source_paths=source_paths, output_path=work_dir / "edited_source.mp4")
    (work_dir / "clip_plan_validated.json").write_text(
        json.dumps(validated_plan, ensure_ascii=False, indent=2), encoding="utf-8")
    if (validated_plan.get("qc") or {}).get("blocking"):
        raise SystemExit(
            "clip_plan duration QC blocking: use --allow-duration-drift to accept current target-duration drift. "
            "See clip_plan_validated.json['qc']."
        )
    if args.normalize_only:
        # normalize-only produces planned delivery facts in clip_plan_validated.json, but no
        # rendered/reused media exists in this run, so remove any stale final delivery artifact.
        (work_dir / "cut_delivery_qc.json").unlink(missing_ok=True)
        print(json.dumps({"status": "normalized", "clips": len(validated_plan["clips"]),
                          "total_duration": validated_plan["total_duration"]}, ensure_ascii=False))
        return

    edited_source_path = work_dir / "edited_source.mp4"
    if should_reuse_edited_source(edited_source_path, validated_plan, args.video):
        log(f"复用剪辑源视频: {edited_source_path}")
        update_delivery_qc(validated_plan, source_paths=source_paths, output_path=edited_source_path, rendered=True)
        write_cut_delivery_qc(work_dir, validated_plan)
        _write_edited_source_meta(edited_source_path, validated_plan, args.video)
        (work_dir / "clip_plan_validated.json").write_text(
            json.dumps(validated_plan, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        build_edited_source_video(args.video, validated_plan, work_dir, edited_source_path)
        (work_dir / "clip_plan_validated.json").write_text(
            json.dumps(validated_plan, ensure_ascii=False, indent=2), encoding="utf-8")

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
