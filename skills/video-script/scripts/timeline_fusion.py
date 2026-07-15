"""Fuse scenes, ASR, and quiet windows for narration planning."""

import importlib.util


from pathlib import Path

from lib import CONFIG


from agent_text import _overlap_seconds, _recommended_char_budget
from narration_lint import _validate_narration_budget

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


def _align_narration_to_quiet(narration, scenes_analysis, silence_periods):
    """Recompute overlaps_speech from real quiet windows; keep the agent's timing.

    The dense continuous-bed design places narration ON the pictured beat over a
    ducked original bed, so we no longer relocate segments into silence gaps. The
    old shift moved voiceover up to 3s off the moment it was written for and could
    silently blank a squeezed segment; both fought the design intent. We now only
    correct the overlaps_speech flag that the ducking stage consumes, leaving the
    agent's start/end (and text) intact.
    """
    if not silence_periods:
        for n in narration:
            n["overlaps_speech"] = True
        return _validate_narration_budget(narration, scenes_analysis)

    quiet_windows = [
        qp for qp in silence_periods if _silence_window_is_usable_quiet(qp)
    ]
    quiet_ratio_min = float(CONFIG.get("quiet_overlap_min_ratio", 0.8) or 0.8)

    # Run budget/dedup FIRST, then set the flag on the final (possibly merged) spans,
    # so a dedup-merged beat's overlaps_speech reflects its extended timing, not its
    # original shorter span (which the ducking stage in assemble.py would mis-duck).
    aligned = _validate_narration_budget(narration, scenes_analysis)
    for n in aligned:
        try:
            seg_start = float(n["start"])
            seg_end = float(n["end"])
        except (KeyError, TypeError, ValueError):
            n["overlaps_speech"] = True
            continue
        seg_dur = max(0.0, seg_end - seg_start)
        quiet_overlap = _quiet_overlap_seconds(seg_start, seg_end, quiet_windows)
        n["overlaps_speech"] = quiet_overlap < max(0.3, seg_dur * quiet_ratio_min)

    return aligned


def _scene_asr_lines(asr_result, scene):
    lines = []
    for seg in asr_result or []:
        try:
            start = float(seg.get("start", 0))
            end = float(seg.get("end", start))
        except (TypeError, ValueError):
            continue
        if scene["start"] < end and scene["end"] > start:
            text = str(seg.get("text", "")).strip()
            if text:
                lines.append(f"    [{start:.1f}-{end:.1f}] {text}")
    return lines


def _quiet_windows_for_scene(silence_periods, scene):
    windows = []
    for qp in silence_periods or []:
        if not _silence_window_is_usable_quiet(qp):
            continue
        if qp["start"] < scene["end"] and qp["end"] > scene["start"]:
            start = max(qp["start"], scene["start"])
            end = min(qp["end"], scene["end"])
            if end > start:
                windows.append((start, end))
    return windows


def _silence_window_is_usable_quiet(qp):
    if not isinstance(qp, dict):
        return False
    reason = str(qp.get("has_speech_reason", "")).lower()
    granularity = str(qp.get("asr_granularity", "")).lower()
    if reason in {
        "coarse_asr_overlap_ignored",
        "asr_overlap_low_confidence_quiet",
        "coarse_asr_no_overlap",
    }:
        return True
    if granularity == "coarse_grid" and qp.get("has_speech") is True:
        return True
    return not qp.get("has_speech", False)


def _quiet_overlap_seconds(start, end, quiet_windows):
    overlap_seconds = 0.0
    for qw in quiet_windows:
        try:
            if isinstance(qw, dict):
                q_start = float(qw.get("start", 0))
                q_end = float(qw.get("end", q_start))
            else:
                q_start, q_end = qw
                q_start = float(q_start)
                q_end = float(q_end)
        except (TypeError, ValueError):
            continue
        overlap_seconds += _overlap_seconds(start, end, q_start, q_end)
    return overlap_seconds


def _build_timeline_fusion(scenes, asr_segments, silence_periods):
    """Fuse VLM scenes, ASR dialogue and quiet narration slots on one timeline."""
    fusion = []
    quiet_windows = [
        w for w in silence_periods or [] if _silence_window_is_usable_quiet(w)
    ]
    for scene in scenes or []:
        try:
            start = float(scene.get("start", 0))
            end = float(scene.get("end", start))
        except (TypeError, ValueError):
            continue
        dialogue_segments = []
        dialogue_overlap = 0.0
        for seg in asr_segments or []:
            if not isinstance(seg, dict):
                continue
            try:
                seg_start = float(seg.get("start", 0))
                seg_end = float(seg.get("end", seg_start))
            except (TypeError, ValueError):
                continue
            overlap = _overlap_seconds(start, end, seg_start, seg_end)
            if overlap <= 0:
                continue
            text = str(seg.get("text", "")).strip()
            dialogue_overlap += overlap
            dialogue_segments.append(
                {
                    "start": round(seg_start, 2),
                    "end": round(seg_end, 2),
                    "overlap_seconds": round(overlap, 2),
                    "text": text,
                }
            )

        narration_slots = []
        for window in quiet_windows:
            try:
                w_start = float(window.get("start", 0))
                w_end = float(window.get("end", w_start))
            except (TypeError, ValueError):
                continue
            overlap = _overlap_seconds(start, end, w_start, w_end)
            if overlap <= 0:
                continue
            slot_start = max(start, w_start)
            slot_end = min(end, w_end)
            narration_slots.append(
                {
                    "start": round(slot_start, 2),
                    "end": round(slot_end, 2),
                    "duration": round(slot_end - slot_start, 2),
                    "char_budget": _recommended_char_budget(slot_start, slot_end),
                }
            )

        fusion.append(
            {
                "scene_id": scene.get("scene_id"),
                "time_range": [round(start, 2), round(end, 2)],
                "visual_description": scene.get("description", ""),
                "depth_analysis": scene.get("depth_analysis", ""),
                "frame_facts": scene.get("frame_facts", {}),
                "dialogue_segments": dialogue_segments,
                "dialogue_overlap_seconds": round(dialogue_overlap, 2),
                "narration_slots": narration_slots,
                "recommended_mode": "quiet-slot"
                if narration_slots and dialogue_overlap < (end - start) * 0.4
                else "ducked-bed",
            }
        )
    return fusion
