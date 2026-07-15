"""Map source-time narration onto the edited output timeline."""

from lib import log


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
    clips = (
        validated_plan["clips"] if isinstance(validated_plan, dict) else validated_plan
    )
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
            clip = next(
                (c for c in clips if c.get("clip_id") == requested_clip_id), None
            )
            if clip and not (
                clip["source_start"]
                <= ((source_start + source_end) / 2)
                <= clip["source_end"]
            ):
                clip = None
        else:
            matches = _clips_for_midpoint(source_start, source_end, clips)
            if len(matches) > 1:
                log(
                    f"  丢弃重复片段中未标 source_clip_id 的解说: {source_start:.1f}-{source_end:.1f}s"
                )
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
            clipped_source_start > source_start + 1e-3
            or clipped_source_end < source_end - 1e-3
        )
        mapped.append(item)

    mapped.sort(key=lambda seg: seg["start"])
    return mapped


def lint_mapped_narration(
    mapped,
    original_count,
    output_duration,
    *,
    min_spm=6.0,
    max_gap_seconds=12.0,
    drop_ratio_limit=0.3,
):
    """Advisory re-lint of narration AFTER it is mapped onto the cut OUTPUT timeline.

    The mapper silently drops beats whose midpoint is outside every kept clip and clamps
    boundary-crossers, so a narration authored against the full source can pass the
    source-time validate yet leave the cut sparse or describing footage the viewer never
    sees. This surfaces that on the real output timeline (narration_mapped_lint.json) and
    returns a `blocking` verdict (heavy drop / too sparse / long gap) that the cut stage
    enforces unless --allow-sparse-cut. Clamped beats are always blocking because their TTS
    sentence would be cut at a clip edge; sparse-cut intent never authorizes speech truncation.
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
        warnings.append(
            {
                "code": "many_beats_dropped",
                "message": "大量解说段落落在保留片段之外被丢弃——请按保留的片段写解说，而不是整段原片。",
                "dropped": dropped,
                "original": original_count,
                "drop_ratio": round(drop_ratio, 2),
            }
        )
    if mapped_count >= 2 and spm and spm < min_spm:
        warnings.append(
            {
                "code": "low_density_output",
                "message": "映射后的解说在成片里偏稀疏——在保留片段内补充解说 beat。",
                "segments_per_minute": round(spm, 2),
                "min_segments_per_minute": min_spm,
            }
        )
    if max_gap > max_gap_seconds:
        warnings.append(
            {
                "code": "long_gap_output",
                "message": "成片里有一长段没有解说。",
                "max_gap_seconds": round(max_gap, 2),
                "max_gap_limit_seconds": max_gap_seconds,
            }
        )
    clamped = [b for b in mapped if isinstance(b, dict) and b.get("clamped")]
    if clamped:
        warnings.append(
            {
                "code": "clamped_beats",
                "message": "有解说段被裁到片段边界，旁白句子会被截断——必须移动片段边界或重写整句。",
                "count": len(clamped),
            }
        )
    blocking_codes = {
        "many_beats_dropped",
        "low_density_output",
        "long_gap_output",
        "clamped_beats",
    }
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
        target_qc = {
            "status": target_status,
            "target_duration": None,
            "total_duration": round(total, 3),
        }
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
                warning["duration_drift_allowed_by"] = (
                    duration_drift_allowed_by or "--allow-duration-drift"
                )
                target_qc["duration_drift_allowed_by"] = warning[
                    "duration_drift_allowed_by"
                ]
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
