"""Load measured source-speech evidence and classify narration ownership."""

import json
import math
from pathlib import Path

from lib import CONFIG, stable_hash


def _empty_evidence(mode):
    return {
        "anchors": [],
        "speech_spans": [],
        "quiet_windows": [],
        "require_measured": mode == "cut_output",
    }


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _timed_rows(payload, key):
    rows = payload.get(key, []) if isinstance(payload, dict) else []
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        try:
            start, end = float(row.get("start")), float(row.get("end"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(start) and math.isfinite(end) and end > start:
            out.append({**row, "start": start, "end": end})
    return out


def _output_payload_is_current(payload, work_dir):
    if not isinstance(payload, dict):
        return False
    if payload.get("schema_version") != 2 or payload.get("timeline") != "cut_output":
        return False
    plan = _read_json(Path(work_dir) / "clip_plan_validated.json")
    return bool(
        isinstance(plan, dict)
        and payload.get("clip_plan_fingerprint") == stable_hash(plan)
    )


def load_source_sentence_evidence(work_dir, mode="full"):
    """Load sentence boundaries plus speech/quiet spans on the narration clock."""
    if work_dir is None:
        return _empty_evidence(mode)
    work_dir = Path(work_dir)
    output_mode = mode in {"cut", "cut_output"}
    path = work_dir / (
        "speech_boundary_anchors_output.json"
        if output_mode
        else "speech_boundary_anchors.json"
    )
    payload = _read_json(path)
    if mode == "cut_output" and not _output_payload_is_current(payload, work_dir):
        return _empty_evidence(mode)
    if not isinstance(payload, dict):
        payload = {}

    speech_spans = _timed_rows(payload, "speech_spans")
    quiet_windows = _timed_rows(payload, "quiet_windows")
    if not output_mode:
        for name in ("asr_result.json", "asr_clean.json"):
            raw = _read_json(work_dir / name)
            if isinstance(raw, list):
                candidate = {"speech_spans": raw}
            elif isinstance(raw, dict):
                candidate = {"speech_spans": raw.get("segments", [])}
            else:
                continue
            speech_spans = _timed_rows(candidate, "speech_spans")
            if speech_spans:
                break
        raw_quiet = _read_json(work_dir / "silence_periods.json")
        candidate = {
            "quiet_windows": [
                row
                for row in raw_quiet
                if isinstance(row, dict) and not bool(row.get("has_speech", False))
            ]
        } if isinstance(raw_quiet, list) else {}
        quiet_windows = _timed_rows(candidate, "quiet_windows")

    anchors = []
    for anchor in payload.get("sentence_anchors", []):
        if not isinstance(anchor, dict) or anchor.get("confidence") not in {
            "high",
            "medium",
        }:
            continue
        try:
            when = float(anchor.get("time"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(when) and when >= 0:
            anchors.append({**anchor, "time": round(when, 3)})
    return {
        "anchors": sorted(anchors, key=lambda item: item["time"]),
        "speech_spans": speech_spans,
        "quiet_windows": quiet_windows,
        "require_measured": mode == "cut_output",
    }


def _merged_intervals(start, end, rows):
    intervals = sorted(
        (max(start, row["start"]), min(end, row["end"]))
        for row in rows
        if row["end"] > start and row["start"] < end
    )
    merged = []
    for left, right in intervals:
        if right <= left:
            continue
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))
    return merged


def _interval_overlap(start, end, rows):
    return sum(right - left for left, right in _merged_intervals(start, end, rows))


def _speech_overlap_excluding_quiet(start, end, speech, quiet):
    speech_intervals = _merged_intervals(start, end, speech)
    quiet_intervals = _merged_intervals(start, end, quiet)
    overlap = sum(right - left for left, right in speech_intervals)
    for speech_left, speech_right in speech_intervals:
        overlap -= sum(
            max(0.0, min(speech_right, quiet_right) - max(speech_left, quiet_left))
            for quiet_left, quiet_right in quiet_intervals
        )
    return max(0.0, overlap)


def segment_overlaps_source_speech(seg, evidence):
    """Classify aggregate mix ownership across the complete narration interval."""
    try:
        start, end = float(seg.get("start")), float(seg.get("end"))
    except (AttributeError, TypeError, ValueError):
        return bool(getattr(seg, "get", lambda *_: True)("overlaps_speech", True))
    duration = max(0.0, end - start)
    quiet = evidence["quiet_windows"]
    quiet_min = max(
        0.3,
        duration * float(CONFIG.get("quiet_overlap_min_ratio", 0.8) or 0.8),
    )
    speech = evidence["speech_spans"]
    if speech:
        return _speech_overlap_excluding_quiet(start, end, speech, quiet) > 0.05
    if quiet and _interval_overlap(start, end, quiet) >= quiet_min:
        return False
    if evidence["anchors"] or evidence["require_measured"]:
        return True
    return bool(seg.get("overlaps_speech", True))


def entry_overlaps_source_speech(seg, evidence, tolerance=0.05):
    """Classify the entry instant; later quiet time cannot erase an unsafe start."""
    try:
        start = float(seg.get("start"))
    except (AttributeError, TypeError, ValueError):
        return True
    if any(
        row["start"] - tolerance <= start <= row["end"] + tolerance
        for row in evidence["quiet_windows"]
    ):
        return False
    if any(
        row["start"] - tolerance <= start < row["end"] - tolerance
        for row in evidence["speech_spans"]
    ):
        return True
    if evidence["speech_spans"]:
        return False
    if evidence["anchors"] or evidence["require_measured"]:
        return True
    return bool(seg.get("overlaps_speech", True))


def measure_narration_speech_ownership(narration, work_dir, mode="full"):
    """Return narration copies with aggregate ownership derived from evidence."""
    evidence = load_source_sentence_evidence(work_dir, mode=mode)
    measured = []
    for seg in narration if isinstance(narration, list) else []:
        if not isinstance(seg, dict):
            measured.append(seg)
            continue
        item = dict(seg)
        item["overlaps_speech"] = segment_overlaps_source_speech(item, evidence)
        measured.append(item)
    return measured
