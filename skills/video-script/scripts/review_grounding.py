"""Load and remap source evidence for narration review."""

import json

import math


from pathlib import Path

from lib import stable_hash


def _load(work_dir, name):
    path = Path(work_dir) / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def _asr_segments(payload):
    """Accept both raw ASR lists and consolidated ``{"segments": [...]}`` artifacts."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("segments"), list):
        return payload["segments"]
    return []


def _load_review_grounding(work_dir):
    """Load grounding for single-source and project-level multi-source work dirs.

    Multi-source recap intentionally stores each source's understanding artifacts below
    ``sources/<source_id>``.  Reviewing only project-root ``vlm_analysis.json`` /
    ``asr_result.json`` therefore produced a formally valid but empty evidence bundle.  Load
    and label each source here so output-timeline remapping can retain provenance.
    """
    work_dir = Path(work_dir)
    manifest = _load(work_dir, "multi_source_manifest.json")
    sources = manifest.get("sources") if isinstance(manifest, dict) else None
    if isinstance(sources, list):
        combined_vlm = []
        combined_asr = []
        for source in sources:
            if not isinstance(source, dict):
                continue
            source_id = str(source.get("source_id") or "").strip()
            relative_dir = source.get("source_work_dir")
            if not source_id or not relative_dir:
                continue
            source_dir = (work_dir / str(relative_dir)).resolve(strict=False)
            try:
                source_dir.relative_to(work_dir.resolve(strict=False))
            except ValueError:
                continue
            for scene in _load(source_dir, "vlm_analysis.json") or []:
                if isinstance(scene, dict):
                    item = dict(scene)
                    item["source_id"] = source_id
                    combined_vlm.append(item)
            clean_asr = _load(source_dir, "asr_clean.json")
            raw_asr = (
                clean_asr
                if clean_asr is not None
                else _load(source_dir, "asr_result.json")
            )
            for segment in _asr_segments(raw_asr):
                if isinstance(segment, dict):
                    item = dict(segment)
                    item["source_id"] = source_id
                    combined_asr.append(item)
        if combined_vlm or combined_asr:
            return combined_vlm, combined_asr

    return _load(work_dir, "vlm_analysis.json") or [], _asr_segments(
        _load(work_dir, "asr_result.json")
    )


def _source_fingerprint(work_dir, name):
    path = Path(work_dir) / name
    if not path.exists():
        return ""
    try:
        return stable_hash(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, OSError):
        try:
            return stable_hash(path.read_text(encoding="utf-8"))
        except OSError:
            return ""


def _load_cut_clip_spans(work_dir):
    """Load explicit source→output spans from the validated cut plan only.

    `cut_output` review compares output-time narration to output-time evidence. A raw
    clip_plan can be pre-padding/pre-snap and may omit output spans, so using it would
    look grounded while disagreeing with edited_source.mp4. Missing/stale validated data
    should fail this advisory stage and let the orchestrator fail-open visibly.
    """
    work_dir = Path(work_dir)
    plan = _load(work_dir, "clip_plan_validated.json")
    if not isinstance(plan, dict):
        return None
    raw_plan = _load(work_dir, "clip_plan.json")
    if raw_plan is not None and plan.get("raw_plan_fingerprint") != stable_hash(
        raw_plan
    ):
        return None
    clips = plan.get("clips")
    if not isinstance(clips, list):
        return None
    spans = []
    for clip in clips:
        if not isinstance(clip, dict):
            continue
        if not all(
            key in clip
            for key in ("source_start", "source_end", "output_start", "output_end")
        ):
            return None
        try:
            source_start = float(clip["source_start"])
            source_end = float(clip["source_end"])
            output_start = float(clip["output_start"])
            output_end = float(clip["output_end"])
        except (TypeError, ValueError):
            return None
        values = (source_start, source_end, output_start, output_end)
        if not all(math.isfinite(value) for value in values):
            return None
        if source_end <= source_start or output_end <= output_start:
            return None
        spans.append(
            {
                "source_start": source_start,
                "source_end": source_end,
                "output_start": output_start,
                "output_end": output_end,
                "source_id": str(clip.get("source_id", clip.get("source", "0"))),
                "source_clip_id": clip.get(
                    "source_clip_id", clip.get("id", clip.get("clip_id"))
                ),
                "output_segment_index": len(spans),
            }
        )
    return spans or None


def _source_output_overlaps(start, end, spans, *, source_id=None):
    source_id = str(source_id) if source_id is not None else None
    if source_id not in (None, "", "0"):
        spans = [
            span for span in (spans or []) if str(span.get("source_id")) == source_id
        ]
    overlaps = []
    for span in spans or []:
        source_start = max(start, span["source_start"])
        source_end = min(end, span["source_end"])
        if source_end <= source_start:
            continue
        output_start = span["output_start"] + (source_start - span["source_start"])
        output_end = span["output_start"] + (source_end - span["source_start"])
        overlaps.append(
            {
                "source_start": source_start,
                "source_end": source_end,
                "output_start": output_start,
                "output_end": output_end,
                "source_id": span.get("source_id", "0"),
                "source_clip_id": span.get("source_clip_id"),
                "output_segment_index": span.get("output_segment_index"),
            }
        )
    return overlaps


def _remap_frame_facts(frame_facts, overlap):
    if not isinstance(frame_facts, dict):
        return frame_facts
    out = {}
    for raw_ts, vals in frame_facts.items():
        try:
            ts = float(raw_ts)
        except (TypeError, ValueError):
            continue
        if not (overlap["source_start"] <= ts <= overlap["source_end"]):
            continue
        out_ts = overlap["output_start"] + (ts - overlap["source_start"])
        out[f"{out_ts:.3f}"] = vals
    return out


def remap_grounding_to_output_timeline(vlm_analysis, asr_result, clip_spans):
    """Return VLM/ASR grounding clipped/remapped from source time to cut output time."""
    if not clip_spans:
        return vlm_analysis or [], asr_result or []

    remapped_scenes = []
    for scene in vlm_analysis or []:
        if not isinstance(scene, dict):
            continue
        try:
            start = float(scene.get("start", 0))
            end = float(scene.get("end", start))
        except (TypeError, ValueError):
            continue
        overlaps = _source_output_overlaps(
            start,
            end,
            clip_spans,
            source_id=scene.get("source_id") if "source_id" in scene else None,
        )
        for part_idx, overlap in enumerate(overlaps):
            item = dict(scene)
            item["start"] = round(overlap["output_start"], 3)
            item["end"] = round(overlap["output_end"], 3)
            item["frame_facts"] = _remap_frame_facts(scene.get("frame_facts"), overlap)
            item["source_start"] = round(overlap["source_start"], 3)
            item["source_end"] = round(overlap["source_end"], 3)
            item["source_id"] = overlap.get("source_id", "0")
            item["source_clip_id"] = overlap.get("source_clip_id")
            item["output_segment_index"] = overlap.get("output_segment_index")
            if len(overlaps) > 1:
                item["scene_id"] = f"{scene.get('scene_id', '?')}.{part_idx}"
            remapped_scenes.append(item)

    remapped_asr = []
    for seg in asr_result or []:
        if not isinstance(seg, dict):
            continue
        try:
            start = float(seg.get("start", 0))
            end = float(seg.get("end", start))
        except (TypeError, ValueError):
            continue
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        for overlap in _source_output_overlaps(
            start,
            end,
            clip_spans,
            source_id=seg.get("source_id") if "source_id" in seg else None,
        ):
            item = dict(seg)
            item["start"] = round(overlap["output_start"], 3)
            item["end"] = round(overlap["output_end"], 3)
            item["source_start"] = round(overlap["source_start"], 3)
            item["source_end"] = round(overlap["source_end"], 3)
            item["source_id"] = overlap.get("source_id", "0")
            item["source_clip_id"] = overlap.get("source_clip_id")
            item["output_segment_index"] = overlap.get("output_segment_index")
            remapped_asr.append(item)

    remapped_scenes.sort(
        key=lambda x: (float(x.get("start", 0)), float(x.get("end", 0)))
    )
    remapped_asr.sort(key=lambda x: (float(x.get("start", 0)), float(x.get("end", 0))))
    return remapped_scenes, remapped_asr
