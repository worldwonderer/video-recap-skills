"""Snap clip boundaries to complete speech and clean shot transitions."""

import json

import re

import subprocess

from pathlib import Path

from lib import log


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


def _source_artifact_candidates(
    work_dir, filename, source_id=None, source_work_dir=None
):
    candidates = []
    if source_work_dir:
        candidates.append(Path(work_dir) / source_work_dir / filename)
    if source_id not in (None, ""):
        candidates.append(Path(work_dir) / "sources" / str(source_id) / filename)
    candidates.append(Path(work_dir) / filename)
    # Preserve precedence while avoiding duplicate reads when layouts resolve to one path.
    return list(dict.fromkeys(candidates))


def _load_sentence_boundary_windows(work_dir, source_id=None, source_work_dir=None):
    """Load reliable sentence-end pause windows produced by video-understanding.

    A sentence anchor's `time` is the acoustic pause end, while `pause_start` is already
    after the final spoken sample. Any cut within that closed interval preserves the sentence.
    Low-confidence anchors are deliberately excluded from the hard-safety path.
    """
    for path in _source_artifact_candidates(
        work_dir, "speech_boundary_anchors.json", source_id, source_work_dir
    ):
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        anchors = payload.get("sentence_anchors") if isinstance(payload, dict) else None
        windows = []
        for anchor in anchors or []:
            if not isinstance(anchor, dict):
                continue
            confidence = str(anchor.get("confidence") or "").strip().lower()
            if confidence not in {"high", "medium"}:
                continue
            try:
                end = float(anchor.get("time"))
                start = float(anchor.get("pause_start", end))
            except (TypeError, ValueError):
                continue
            start = min(start, end)
            windows.append(
                {
                    "start": round(max(0.0, start), 3),
                    "end": round(max(0.0, end), 3),
                    "kind": "sentence_anchor",
                    "confidence": confidence,
                }
            )
        return sorted(windows, key=lambda row: (row["start"], row["end"]))
    return []


def _load_source_speech_spans(work_dir, source_id=None, source_work_dir=None):
    """Best-effort ASR speech ownership spans used only to decide whether an unsafe edge blocks."""
    payload = None
    for filename in ("asr_clean.json", "asr_result.json"):
        for path in _source_artifact_candidates(
            work_dir, filename, source_id, source_work_dir
        ):
            if not path.exists():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                payload = None
            if payload is not None:
                break
        if payload is not None:
            break
    if isinstance(payload, dict):
        payload = payload.get("segments") or payload.get("items") or []
    spans = []
    for row in payload or []:
        if not isinstance(row, dict) or not str(row.get("text") or "").strip():
            continue
        try:
            start, end = float(row.get("start")), float(row.get("end"))
        except (TypeError, ValueError):
            continue
        if end > start:
            spans.append({"start": max(0.0, start), "end": end})
    merged = []
    for span in sorted(spans, key=lambda row: (row["start"], row["end"])):
        if merged and span["start"] <= merged[-1]["end"] + 0.05:
            merged[-1]["end"] = max(merged[-1]["end"], span["end"])
        else:
            merged.append(dict(span))
    return merged


def _combine_boundary_windows(*groups):
    rows = []
    for group in groups:
        rows.extend(_valid_silence_windows(group))
    unique = {(round(row["start"], 3), round(row["end"], 3)) for row in rows}
    return [{"start": start, "end": end} for start, end in sorted(unique)]


def _same_source(left, right):
    left_id, right_id = left.get("source_id"), right.get("source_id")
    if left_id not in (None, "") or right_id not in (None, ""):
        return (
            left_id not in (None, "")
            and right_id not in (None, "")
            and str(left_id) == str(right_id)
        )
    left_path, right_path = left.get("source_path"), right.get("source_path")
    if left_path or right_path:
        return bool(left_path and right_path and str(left_path) == str(right_path))
    return True  # single-source normalized plans intentionally omit source identity


def _continuous_source_join(left, right, tolerance=0.05):
    return bool(
        _same_source(left, right)
        and abs(
            float(left.get("source_end", 0.0)) - float(right.get("source_start", 0.0))
        )
        <= tolerance
        and abs(
            float(left.get("output_end", 0.0)) - float(right.get("output_start", 0.0))
        )
        <= tolerance
    )


def enforce_clip_sentence_boundaries(
    plan, boundary_windows, speech_spans, video_duration, tolerance=0.05
):
    """Block any audible clip edge that falls inside detected source speech.

    Safe edges are: source start/end, a reliable sentence/quiet pause, or a truly contiguous
    same-source join (no media is removed). Missing ASR timing degrades to `unchecked` rather
    than inventing speech. Once ASR says an edge is speech-owned, failure to snap is blocking.
    """
    clips = plan.get("clips") or []
    windows = _valid_silence_windows(boundary_windows)
    spans = _valid_silence_windows(speech_spans)
    video_duration = max(0.0, float(video_duration or 0.0))
    tolerance = max(0.001, float(tolerance or 0.05))
    checks, new_blockers = [], []

    def in_windows(ts):
        return any(
            row["start"] - tolerance <= ts <= row["end"] + tolerance for row in windows
        )

    def in_speech(ts):
        return any(
            row["start"] - tolerance <= ts <= row["end"] + tolerance for row in spans
        )

    for idx, clip in enumerate(clips):
        for edge, ts in (
            ("start", float(clip["source_start"])),
            ("end", float(clip["source_end"])),
        ):
            contiguous = (
                edge == "start"
                and idx > 0
                and _continuous_source_join(clips[idx - 1], clip, tolerance)
            ) or (
                edge == "end"
                and idx + 1 < len(clips)
                and _continuous_source_join(clip, clips[idx + 1], tolerance)
            )
            if edge == "start" and ts <= tolerance:
                status, reason = "safe", "source_start"
            elif edge == "end" and ts >= video_duration - tolerance:
                status, reason = "safe", "source_end"
            elif contiguous:
                status, reason = "safe", "continuous_source_join"
            elif in_windows(ts):
                status, reason = "safe", "sentence_or_quiet_boundary"
            elif not spans:
                status, reason = "unchecked", "speech_timing_unavailable"
            elif not in_speech(ts):
                status, reason = "safe", "outside_detected_speech"
            else:
                status, reason = "blocking", "inside_detected_speech"
            check = {
                "clip_id": clip.get("clip_id", idx),
                "source_id": clip.get("source_id"),
                "edge": edge,
                "time": round(ts, 3),
                "status": status,
                "reason": reason,
            }
            checks.append(check)
            if status == "blocking":
                new_blockers.append(
                    {
                        "code": "unsafe_clip_sentence_boundary",
                        **check,
                        "message": "剪辑边界仍落在原声讲话区间内，必须移动到句末锚点，不能截断原声句子。",
                    }
                )

    qc = plan.setdefault("qc", {})
    qc.setdefault("boundary_status", {})["sentence_checks"] = checks
    existing = [
        row
        for row in (qc.get("blocking") or [])
        if not isinstance(row, dict)
        or row.get("code") != "unsafe_clip_sentence_boundary"
    ]
    if existing or new_blockers:
        qc["blocking"] = existing + new_blockers
    else:
        qc.pop("blocking", None)
    return plan


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
        if new_start < float(other["source_end"]) and new_end > float(
            other["source_start"]
        ):
            return True
    return False


def snap_clip_starts_to_lines(
    plan,
    silence_periods,
    video_duration,
    max_prepend,
    max_trim=0.35,
    min_clip_duration=0.3,
):
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
                    safe = not _candidate_overlaps(
                        clips, i, candidate_start, source_end
                    )
                if safe:
                    clip["source_start"] = candidate_start
                    event.update(
                        {
                            "action": "prepended",
                            "new_start": candidate_start,
                            "delta": round(original_start - candidate_start, 3),
                        }
                    )
                    events.append(event)
                    continue
                reason = "overlap_or_collapse"
            else:
                reason = "prior_quiet_too_far"
        else:
            reason = "no_prior_quiet"

        next_starts = [
            w["start"] for w in silence_periods if w["start"] >= original_start
        ]
        if next_starts:
            candidate_start = min(next_starts)
            trim_delta = candidate_start - original_start
            if 0 < trim_delta <= max_trim:
                candidate_start = round(min(video_duration, candidate_start), 3)
                safe = source_end - candidate_start >= min_duration
                if safe and not allow_overlap:
                    safe = not _candidate_overlaps(
                        clips, i, candidate_start, source_end
                    )
                if safe:
                    clip["source_start"] = candidate_start
                    event.update(
                        {
                            "action": "trimmed",
                            "new_start": candidate_start,
                            "delta": round(trim_delta, 3),
                            "fallback_from": reason,
                        }
                    )
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
            warnings.append(
                {
                    "code": event["warning_code"],
                    "clip_id": event["clip_id"],
                    "source_id": event.get("source_id"),
                    "start_unsnapped_reason": event.get("start_unsnapped_reason"),
                }
            )
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
                c["source_start"]
                for j, c in enumerate(clips)
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
        event.update(
            {
                "action": "extended",
                "new_end": clip["source_end"],
                "delta": round(float(clip["source_end"]) - float(source_end), 3),
            }
        )
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
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-ss",
        f"{seek:.3f}",
        "-i",
        str(video),
        "-t",
        f"{dur:.3f}",
        "-an",
        "-sn",
        "-filter:v",
        f"select='gt(scene,{threshold})',showinfo",
        "-f",
        "null",
        "-",
    ]
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


def snap_clips_off_shot_changes(
    plan, video, video_duration, margin, threshold, min_keep=0.5
):
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
    del video_duration  # Positional compatibility for downstream direct callers.
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
        start_changes = [
            c
            for c in _detect_shot_changes(video, s, min(e, s + margin), threshold)
            if c > s + 1e-3
        ]
        if start_changes:
            cand = max(start_changes)  # open after the last rapid cut in the window
            if cand < e - min_keep:
                new_s = round(cand, 3)
                event["start_action"] = "moved_forward"
                event["new_start"] = new_s
            else:
                event["start_unsnapped_reason"] = "collapse"
        # Closing: a shot-change just BEFORE source_end leaves a next-shot sliver after it.
        end_changes = [
            c
            for c in _detect_shot_changes(video, max(new_s, e - margin), e, threshold)
            if c < e - 1e-3
        ]
        if end_changes:
            cand = min(end_changes)  # close before the first rapid cut in the window
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
        log(
            f"避让原片切镜头: {n_start} 个起点前移、{n_end} 个终点回收 (margin={margin}s, 阈值={threshold})"
        )
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
    for path in _source_artifact_candidates(
        work_dir, "silence_periods.json", source_id, source_work_dir
    ):
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return []
            return data if isinstance(data, list) else []
    return []


def snap_multi_source_clips(
    plan,
    sources,
    work_dir,
    *,
    line_max_extend,
    scene_margin,
    scene_threshold,
    do_line_snap=True,
    do_scene_snap=True,
    start_max_prepend=None,
    start_max_trim=None,
):
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
    boundary_accum = {
        "start_snaps": [],
        "end_snaps": [],
        "shot_snaps": [],
        "sentence_checks": [],
    }
    blocking_accum = []
    for sid, group in groups.items():
        source = sources.get(sid, {}) if isinstance(sources, dict) else {}
        duration = float(source.get("duration") or 0.0) or max(
            (float(c["source_end"]) for c in group), default=0.0
        )
        mini = {"clips": [dict(c) for c in group], "allow_overlap": allow_overlap}
        # Visual cleanup goes first. Sentence/quiet snapping is the final authority because
        # a clean picture is never allowed to reintroduce a mid-sentence audio cut.
        if do_scene_snap and source.get("source_path"):
            mini = snap_clips_off_shot_changes(
                mini,
                source["source_path"],
                video_duration=duration,
                margin=scene_margin,
                threshold=scene_threshold,
            )
        if do_line_snap:
            source_work_dir = source.get("source_work_dir")
            silence = _load_silence_for_source(work_dir, sid, source_work_dir)
            anchors = _load_sentence_boundary_windows(work_dir, sid, source_work_dir)
            boundaries = _combine_boundary_windows(silence, anchors)
            if start_max_prepend is not None:
                mini = snap_clip_starts_to_lines(
                    mini,
                    boundaries,
                    duration,
                    start_max_prepend,
                    max_trim=start_max_trim if start_max_trim is not None else 0.35,
                )
            mini = snap_clip_ends_to_lines(mini, boundaries, duration, line_max_extend)
            mini = enforce_clip_sentence_boundaries(
                mini,
                boundaries,
                _load_source_speech_spans(work_dir, sid, source_work_dir),
                duration,
            )
        mini_boundary = (mini.get("qc") or {}).get("boundary_status") or {}
        for key in boundary_accum:
            boundary_accum[key].extend(
                [e for e in mini_boundary.get(key, []) if isinstance(e, dict)]
            )
        blocking_accum.extend(
            [
                row
                for row in ((mini.get("qc") or {}).get("blocking") or [])
                if isinstance(row, dict)
            ]
        )
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
                warnings.append(
                    {
                        "code": event["warning_code"],
                        "clip_id": event.get("clip_id"),
                        "source_id": event.get("source_id"),
                        "start_unsnapped_reason": event.get("start_unsnapped_reason"),
                    }
                )
        if blocking_accum:
            qc.setdefault("blocking", []).extend(blocking_accum)
    return plan
