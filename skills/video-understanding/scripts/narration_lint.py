"""Enforce narration timing, evidence, budget, and sentence-integrity rules."""

import importlib.util


import json

import math


from pathlib import Path

from lib import CONFIG

from lib import log

from agent_text import (
    _clean_narration_punctuation,
    _find_scene_for_midpoint,
    _normalise_narration_segment,
    _post_dedup_narration,
    _recommended_char_budget,
    _scene_available_seconds,
    _text_char_count,
    _truncate_at_sentence,
)

try:
    from deslop_qc import analyze_deslop_qc
except ModuleNotFoundError:
    _deslop_qc_path = Path(__file__).with_name("deslop_qc.py")
    _deslop_qc_spec = importlib.util.spec_from_file_location(
        "deslop_qc", _deslop_qc_path
    )
    if _deslop_qc_spec is None or _deslop_qc_spec.loader is None:
        raise
    _deslop_qc_module = importlib.util.module_from_spec(_deslop_qc_spec)
    _deslop_qc_spec.loader.exec_module(_deslop_qc_module)
    analyze_deslop_qc = _deslop_qc_module.analyze_deslop_qc


def _lint_issue(level, index, code, message, **extra):
    issue = {"level": level, "index": index, "code": code, "message": message}
    issue.update(extra)
    return issue


def _scene_bounds_for_midpoint(scenes_analysis, start, end):
    scene = _find_scene_for_midpoint(scenes_analysis or [], start, end)
    if not scene:
        return None
    return float(scene.get("start", 0)), float(scene.get("end", 0))


def _frame_fact_times_for_segment(scenes_analysis, start, end):
    """Return frame-fact timestamps covered by a narration segment.

    These timestamps are cheap visual anchors. A narration slot that spans too
    many anchors is more likely to drift into "general story summary" instead
    of staying attached to what is on screen.
    """
    times = []
    scene = _find_scene_for_midpoint(scenes_analysis or [], start, end)
    if not scene:
        return times
    for raw_ts in (scene.get("frame_facts") or {}).keys():
        try:
            ts = float(raw_ts)
        except (TypeError, ValueError):
            continue
        if float(start) <= ts <= float(end):
            times.append(ts)
    return sorted(times)


def _clip_matches_for_segment(seg, clip_plan):
    if not clip_plan:
        return []
    clips = (
        clip_plan.get("clips", clip_plan) if isinstance(clip_plan, dict) else clip_plan
    )
    if not isinstance(clips, list):
        return []
    try:
        start = float(seg.get("start"))
        end = float(seg.get("end"))
    except (TypeError, ValueError):
        return []
    midpoint = (start + end) / 2
    if seg.get("source_clip_id") is not None:
        try:
            requested = int(seg.get("source_clip_id"))
        except (TypeError, ValueError):
            return []
        return [
            clip
            for clip in clips
            if clip.get("clip_id") == requested
            and float(clip.get("source_start", clip.get("start", 0)))
            <= midpoint
            <= float(clip.get("source_end", clip.get("end", 0)))
        ]
    return [
        clip
        for clip in clips
        if float(clip.get("source_start", clip.get("start", 0)))
        <= midpoint
        <= float(clip.get("source_end", clip.get("end", 0)))
    ]


def _load_source_sentence_entry_anchors(work_dir, mode="full"):
    """Load trustworthy sentence-end anchors for the narration timeline.

    Full mode uses source timestamps directly. Cut pass 2 writes a separately
    remapped OUTPUT-time artifact while building the brief; never compare output
    narration timestamps against the original source clock.
    """
    if work_dir is None:
        return []
    work_dir = Path(work_dir)
    if mode == "cut":
        path = work_dir / "speech_boundary_anchors_output.json"
        if not path.exists():
            return []
    else:
        path = work_dir / "speech_boundary_anchors.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    anchors = payload.get("sentence_anchors", []) if isinstance(payload, dict) else []
    out = []
    for anchor in anchors:
        if not isinstance(anchor, dict) or anchor.get("confidence") not in {
            "high",
            "medium",
        }:
            continue
        try:
            when = float(anchor.get("time"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(when) or when < 0:
            continue
        item = dict(anchor)
        item["time"] = round(when, 3)
        out.append(item)
    return sorted(out, key=lambda item: item["time"])


def _source_sentence_entry_issue(index, seg, start, anchors):
    """Return a blocking issue when narration enters midway through source speech.

    The validator reports a correction instead of silently moving audio. Source
    sentence integrity is invariant; an editorial policy string cannot bypass it.
    """
    if not anchors or not bool(seg.get("overlaps_speech", True)):
        return None
    # A cold-open at the first frame is not an interruption: the source sentence
    # has not been allowed to start. All later entries must use a measured anchor.
    if start <= 0.25:
        return None
    # Only the measured acoustic pause owns a safe entry. A tiny 80ms post-anchor
    # tolerance covers timestamp/sample rounding; the old +450ms allowance could
    # already be several Chinese syllables into the next sentence.
    for anchor in anchors:
        when = anchor["time"]
        try:
            pause_start = float(anchor.get("pause_start", when - 0.12))
        except (TypeError, ValueError):
            pause_start = when - 0.12
        if pause_start - 0.05 <= start <= when + 0.08:
            return None

    suggested = next(
        (anchor for anchor in anchors if anchor["time"] > start + 0.08), None
    )
    text_tail = str((suggested or {}).get("text_tail") or "").strip()
    return _lint_issue(
        "error",
        index,
        "interrupts_source_sentence",
        "Narration enters before the source sentence finishes. Move this block to the suggested "
        "sentence-end anchor, or shorten/move/remove it when there is no later verified anchor, "
        "then rerun lint before TTS. Source sentence interruption has no override.",
        entry_time=round(start, 3),
        suggested_start=round(float(suggested["time"]), 3) if suggested else None,
        source_text_tail=text_tail,
        anchor_confidence=suggested.get("confidence") if suggested else None,
    )


def lint_narration(
    narration, scenes_analysis=None, *, clip_plan=None, mode="full", work_dir=None
):
    """Preflight-check agent narration before TTS; write narration_lint.json when work_dir is set."""
    errors = []
    warnings = []
    normalized = []
    source_sentence_anchors = _load_source_sentence_entry_anchors(work_dir, mode=mode)
    if not isinstance(narration, list):
        errors.append(
            _lint_issue(
                "error",
                None,
                "invalid_json_shape",
                "narration.json must be a JSON array",
            )
        )
    else:
        for idx, seg in enumerate(narration):
            if not isinstance(seg, dict):
                errors.append(
                    _lint_issue(
                        "error",
                        idx,
                        "invalid_segment",
                        "Narration segment must be an object",
                    )
                )
                continue
            try:
                start = float(seg.get("start"))
                end = float(seg.get("end"))
            except (TypeError, ValueError):
                errors.append(
                    _lint_issue(
                        "error", idx, "invalid_time", "start/end must be numeric"
                    )
                )
                continue
            text = str(seg.get("narration", "")).strip()
            pause_raw = seg.get("pause_after_ms", CONFIG.get("breath_ms", 250))
            try:
                pause = int(pause_raw)
            except (TypeError, ValueError):
                pause = CONFIG.get("breath_ms", 250)
                warnings.append(
                    _lint_issue(
                        "warning",
                        idx,
                        "invalid_pause",
                        "pause_after_ms is invalid; default will be used",
                    )
                )
            if pause < 0:
                warnings.append(
                    _lint_issue(
                        "warning",
                        idx,
                        "negative_pause",
                        "pause_after_ms is negative; default should be used",
                        pause_after_ms=pause,
                    )
                )
            if end <= start:
                errors.append(
                    _lint_issue(
                        "error",
                        idx,
                        "invalid_time_range",
                        "end must be greater than start",
                        start=start,
                        end=end,
                    )
                )
                continue
            if not text:
                errors.append(
                    _lint_issue(
                        "error",
                        idx,
                        "empty_narration",
                        "narration text must not be empty",
                        start=start,
                        end=end,
                    )
                )
                continue

            char_count = _text_char_count(text)
            budget = _recommended_char_budget(start, end, pause)
            # estimate at the REAL playback rate (after the narration_speed atempo); otherwise a
            # beat sized to its 1.3x-sped slot looks "over budget" when it actually fits.
            play_rate = max(
                CONFIG.get("speech_rate", 3.5)
                * float(CONFIG.get("narration_speed", 1.0) or 1.0),
                0.1,
            )
            estimated_tts_seconds = char_count / play_rate
            slot_seconds = _scene_available_seconds(start, end, pause)
            if budget < 5:
                warnings.append(
                    _lint_issue(
                        "warning",
                        idx,
                        "slot_too_short",
                        "Narration slot is very short; TTS may be clipped",
                        start=start,
                        end=end,
                        budget_chars=budget,
                    )
                )
            elif estimated_tts_seconds > slot_seconds:
                warnings.append(
                    _lint_issue(
                        "warning",
                        idx,
                        "over_budget",
                        "Text may exceed the available TTS slot",
                        start=start,
                        end=end,
                        budget_chars=budget,
                        actual_chars=char_count,
                        estimated_tts_seconds=round(estimated_tts_seconds, 2),
                        slot_seconds=round(slot_seconds, 2),
                    )
                )
            if text[-1] not in "。！？!?….":
                warnings.append(
                    _lint_issue(
                        "warning",
                        idx,
                        "incomplete_sentence",
                        "Narration should end with a complete sentence punctuation",
                        text_tail=text[-8:],
                    )
                )

            # A back-to-back narration handoff does not expose the source track, so
            # the next TTS block is not a new source-speech entry. Keep this strict:
            # only authored adjacency (<=150ms) bypasses the sentence anchor gate.
            connected_predecessor = False
            for other_idx, other in enumerate(narration):
                if other_idx == idx or not isinstance(other, dict):
                    continue
                try:
                    other_start = float(other.get("start"))
                    other_end = float(other.get("end"))
                except (TypeError, ValueError):
                    continue
                if other_start < start and -0.001 <= start - other_end <= 0.15:
                    connected_predecessor = True
                    break
            if not connected_predecessor:
                entry_issue = _source_sentence_entry_issue(
                    idx, seg, start, source_sentence_anchors
                )
                if entry_issue:
                    errors.append(entry_issue)

            scene_bounds = _scene_bounds_for_midpoint(scenes_analysis, start, end)
            if scenes_analysis and not scene_bounds:
                warnings.append(
                    _lint_issue(
                        "warning",
                        idx,
                        "outside_scene",
                        "Narration midpoint does not match any detected scene",
                        start=start,
                        end=end,
                    )
                )
            elif scene_bounds and (start < scene_bounds[0] or end > scene_bounds[1]):
                warnings.append(
                    _lint_issue(
                        "warning",
                        idx,
                        "crosses_scene_boundary",
                        "Narration extends outside its midpoint scene boundary",
                        start=start,
                        end=end,
                        scene_start=scene_bounds[0],
                        scene_end=scene_bounds[1],
                    )
                )

            frame_fact_times = _frame_fact_times_for_segment(
                scenes_analysis, start, end
            )
            max_visual_seconds = float(
                CONFIG.get("visual_beat_max_seconds", 18.0) or 18.0
            )
            max_visual_facts = int(CONFIG.get("visual_beat_max_facts", 3) or 3)
            if (end - start) > max_visual_seconds and len(
                frame_fact_times
            ) > max_visual_facts:
                warnings.append(
                    _lint_issue(
                        "warning",
                        idx,
                        "visual_beat_too_broad",
                        "Narration spans many visual anchors; split or tighten timing so the voiceover stays tied to current pictures",
                        start=start,
                        end=end,
                        duration=round(end - start, 2),
                        frame_fact_times=[round(ts, 2) for ts in frame_fact_times[:8]],
                    )
                )

            if mode == "cut":
                matches = _clip_matches_for_segment(seg, clip_plan)
                if not matches:
                    errors.append(
                        _lint_issue(
                            "error",
                            idx,
                            "outside_clip_plan",
                            "Cut-mode narration must fall inside a selected clip",
                            start=start,
                            end=end,
                        )
                    )
                elif len(matches) > 1 and seg.get("source_clip_id") is None:
                    errors.append(
                        _lint_issue(
                            "error",
                            idx,
                            "ambiguous_source_clip",
                            "Repeated/overlapping clips require source_clip_id",
                            start=start,
                            end=end,
                        )
                    )
                if len(matches) == 1:
                    clip = matches[0]
                    clip_start = float(
                        clip.get("source_start", clip.get("start", start))
                    )
                    clip_end = float(clip.get("source_end", clip.get("end", end)))
                    if start < clip_start or end > clip_end:
                        warnings.append(
                            _lint_issue(
                                "warning",
                                idx,
                                "crosses_clip_boundary",
                                "Narration extends beyond its clip; it will be trimmed to the clip and may describe footage that was cut",
                                start=start,
                                end=end,
                                clip_start=round(clip_start, 3),
                                clip_end=round(clip_end, 3),
                            )
                        )
                if seg.get("source_clip_id") is not None:
                    try:
                        int(seg.get("source_clip_id"))
                    except (TypeError, ValueError):
                        errors.append(
                            _lint_issue(
                                "error",
                                idx,
                                "invalid_source_clip_id",
                                "source_clip_id must be an integer",
                            )
                        )

            normalized.append(
                {"index": idx, "start": start, "end": end, "char_count": char_count}
            )

    sorted_segments = sorted(normalized, key=lambda item: item["start"])
    if isinstance(narration, list) and not sorted_segments:
        errors.append(
            _lint_issue(
                "error",
                None,
                "empty_narration_file",
                "narration.json must contain at least one valid narration segment",
            )
        )
    for prev, curr in zip(sorted_segments, sorted_segments[1:]):
        if curr["start"] < prev["end"]:
            errors.append(
                _lint_issue(
                    "error",
                    curr["index"],
                    "time_overlap",
                    "Segment overlaps the previous narration segment",
                    previous_index=prev["index"],
                    previous_end=prev["end"],
                    start=curr["start"],
                    end=curr["end"],
                )
            )

    # Block-coverage check (full mode only; cut-mode density is measured on the mapped output
    # timeline, not the source timestamps used here). Coverage is diagnostic, not a creative quota:
    # flag wall-to-wall or sparse drafts so the Agent consciously checks the visual/audio board,
    # plus missing original-audio gaps and fragmented one-sentence TTS blocks.
    metrics = {}
    if mode == "full" and len(sorted_segments) >= 2:
        timeline_start = 0.0
        timeline_end = sorted_segments[-1]["end"]
        scene_ends = []
        for scene in scenes_analysis or []:
            try:
                scene_ends.append(float(scene.get("end")))
            except (AttributeError, TypeError, ValueError):
                continue
        if scene_ends:
            timeline_end = max(timeline_end, max(scene_ends))
        span = max(0.0, timeline_end - timeline_start)
        # Score coverage at the SAME conservative rate the writer is budgeted at
        # (_recommended_char_budget uses speech_rate * speech_safety_margin * narration_speed),
        # else the metric scores ~18% stricter than its own budget and false-flags under_narrated.
        play_rate = max(
            CONFIG["speech_rate"]
            * float(CONFIG.get("speech_safety_margin", 0.85) or 0.85)
            * float(CONFIG.get("narration_speed", 1.0) or 1.0),
            0.1,
        )
        spoken = [s["char_count"] / play_rate for s in sorted_segments]
        spoken_ends = [
            min(sorted_segments[i]["end"], sorted_segments[i]["start"] + spoken[i])
            for i in range(len(sorted_segments))
        ]
        # Measure the UNION of estimated playback intervals. Adjacent authored
        # blocks can overlap the conservative text-duration estimate; summing them
        # double-counted time and falsely called a recap wall-to-wall.
        estimated_intervals = sorted(
            (sorted_segments[i]["start"], spoken_ends[i])
            for i in range(len(sorted_segments))
            if spoken_ends[i] > sorted_segments[i]["start"]
        )
        merged_intervals = []
        for start, end in estimated_intervals:
            if merged_intervals and start <= merged_intervals[-1][1]:
                merged_intervals[-1][1] = max(merged_intervals[-1][1], end)
            else:
                merged_intervals.append([start, end])
        narrated_seconds = sum(end - start for start, end in merged_intervals)
        coverage = narrated_seconds / span if span > 0 else 0.0
        orig_min = CONFIG.get("original_block_min_seconds", 2.5)
        orig_gaps = [sorted_segments[0]["start"] - timeline_start]
        orig_gaps.extend(
            sorted_segments[i + 1]["start"] - spoken_ends[i]
            for i in range(len(sorted_segments) - 1)
        )
        orig_gaps.append(timeline_end - spoken_ends[-1])
        original_blocks = sum(1 for g in orig_gaps if g >= orig_min)
        avg_chars = sum(s["char_count"] for s in sorted_segments) / len(sorted_segments)
        cov_target = CONFIG.get("narration_coverage_target", 0.7)
        cov_max = CONFIG.get("narration_coverage_max", 0.85)
        cov_min = CONFIG.get("narration_coverage_min", 0.5)
        block_min_chars = CONFIG.get("narration_block_min_chars", 16)
        metrics = {
            "segment_count": len(sorted_segments),
            "timeline_span_seconds": round(span, 2),
            "narrated_seconds": round(narrated_seconds, 2),
            "narration_coverage": round(coverage, 2),
            "coverage_target": cov_target,
            "original_block_count": original_blocks,
            "avg_block_chars": round(avg_chars, 1),
        }
        if coverage > cov_max:
            warnings.append(
                _lint_issue(
                    "warning",
                    None,
                    "no_original_blocks",
                    "Narration is nearly wall-to-wall — the original audio never gets to breathe. Pull back at a "
                    "few strong moments and write NO narration there so the original plays at full volume. "
                    "Choose those moments by audio_owner, not by a fixed ratio.",
                    narration_coverage=round(coverage, 2),
                    coverage_max=cov_max,
                )
            )
        elif coverage < cov_min:
            warnings.append(
                _lint_issue(
                    "warning",
                    None,
                    "under_narrated",
                    "Narration coverage is sparse. This is not automatically wrong: verify that every long gap is "
                    "intentionally owned by original dialogue/action/ambience/music/silence in visual_audio_board.json. "
                    "Only add a block when it has a specific narration job.",
                    narration_coverage=round(coverage, 2),
                    coverage_min=cov_min,
                )
            )
        if original_blocks == 0 and span >= 3 * orig_min:
            warnings.append(
                _lint_issue(
                    "warning",
                    None,
                    "no_original_breaks",
                    "No deliberate original-audio blocks — narration runs end-to-end with no gap for a strong "
                    "original moment (a key line, an action beat, the music). Leave a few multi-second gaps "
                    "between blocks where the original plays alone.",
                    original_block_min_seconds=orig_min,
                )
            )
        if len(sorted_segments) >= 8 and avg_chars < block_min_chars:
            warnings.append(
                _lint_issue(
                    "warning",
                    None,
                    "fragmented_beats",
                    "Beats are fragmented into single short sentences; each is synthesized as a separate TTS "
                    "utterance, which sounds choppy. Merge adjacent sentences into BLOCKS of 2-4 sentences "
                    "(one continuous thought) so each block speaks as one fluent utterance.",
                    avg_block_chars=round(avg_chars, 1),
                    block_min_chars=block_min_chars,
                )
            )

    deslop_qc = analyze_deslop_qc(narration, work_dir=work_dir)
    for blocker in deslop_qc.get("blockers", []):
        errors.append(
            _lint_issue(
                "error",
                blocker.get("index"),
                blocker.get("code", "deslop_qc_blocker"),
                blocker.get("message", "deslop QC objective blocker"),
                source=blocker.get("source"),
                matches=blocker.get("matches"),
                sentence=blocker.get("sentence"),
            )
        )

    report = {
        "ok": not errors,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "metrics": metrics,
        "deslop_qc": deslop_qc,
        "errors": errors,
        "warnings": warnings,
    }
    if work_dir is not None:
        Path(work_dir, "deslop_qc.json").write_text(
            json.dumps(deslop_qc, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        Path(work_dir, "narration_lint.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return report


def validate_narration_or_raise(
    narration, scenes_analysis=None, *, clip_plan=None, mode="full", work_dir=None
):
    report = lint_narration(
        narration, scenes_analysis, clip_plan=clip_plan, mode=mode, work_dir=work_dir
    )
    if report["errors"]:
        sample = "; ".join(
            f"#{e.get('index')}: {e['code']}" for e in report["errors"][:3]
        )
        raise ValueError(f"narration.json 预检失败: {sample}; 详见 narration_lint.json")
    if report["warnings"]:
        log(
            f"narration lint: {len(report['warnings'])} warnings (see narration_lint.json)"
        )
    else:
        log("narration lint: ok")
    return report


def _validate_narration_budget(narration, scenes_analysis):
    """Validate agent-written narration against timing budgets; trim impossible text safely."""
    if not isinstance(narration, list):
        raise ValueError("narration.json 必须是 JSON 数组")

    cleaned = []
    for raw in narration:
        item = _normalise_narration_segment(raw, scenes_analysis)
        if not item:
            continue
        max_chars = _recommended_char_budget(
            item["start"], item["end"], item.get("pause_after_ms")
        )
        if max_chars < 5:
            log(f"  丢弃过短解说段 {item['start']:.1f}-{item['end']:.1f}s")
            continue
        if _text_char_count(item["narration"]) > max_chars * 1.25:
            truncated = _truncate_at_sentence(item["narration"], max_chars)
            if truncated and _text_char_count(truncated) >= 5:
                log(f"  解说超预算，已截短: {item['start']:.1f}-{item['end']:.1f}s")
                item["narration"] = truncated
            else:
                log(
                    f"  解说超预算且无法安全截断，已丢弃: {item['start']:.1f}-{item['end']:.1f}s"
                )
                continue
        item["narration"] = _clean_narration_punctuation(item["narration"])
        stripped = item["narration"].strip()
        if stripped and stripped[-1] in "，：、；,—":
            item["narration"] = stripped.rstrip("，：、；,—") + "。"
        cleaned.append(item)

    cleaned.sort(key=lambda n: n["start"])
    deduped = []
    for item in cleaned:
        if deduped and item["start"] < deduped[-1]["end"]:
            prev = deduped[-1]
            log(
                f"  解说时间重叠: {item['start']:.1f}-{item['end']:.1f}s vs "
                f"{prev['start']:.1f}-{prev['end']:.1f}s"
            )
            if _text_char_count(item["narration"]) > _text_char_count(
                prev["narration"]
            ):
                deduped[-1] = item
        else:
            deduped.append(item)
    return _post_dedup_narration(deduped)
