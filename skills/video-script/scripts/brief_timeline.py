"""Remap cut evidence and format output-timeline brief directives."""

import importlib.util


import json

import math

import re

from pathlib import Path

from lib import CONFIG, stable_hash


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


def _parse_target_seconds(value):
    """Parse a cut-mode target duration ("30m" / "600" / "1h5m" / "00:30:00") to seconds.

    Mirrors the local clip-plan contract closely enough to size the brief; returns None
    on unparseable input so the brief simply falls back to the source duration.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    text = str(value).strip().lower()
    if not text:
        return None
    try:
        if ":" in text:
            parts = [float(p) for p in text.split(":")]
            if any(p < 0 for p in parts):
                return None
            if len(parts) == 2:
                seconds = parts[0] * 60 + parts[1]
            elif len(parts) == 3:
                seconds = parts[0] * 3600 + parts[1] * 60 + parts[2]
            else:
                return None
            return seconds if seconds > 0 else None
        factors = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}
        seconds = 0.0
        matched = False
        pos = 0
        for m in re.finditer(r"([0-9]+(?:\.[0-9]+)?)(ms|s|m|h)?", text):
            if m.start() != pos:
                break
            pos = m.end()
            matched = True
            seconds += float(m.group(1)) * factors[m.group(2) or "s"]
        if not matched or pos != len(text):
            return None
        return seconds if seconds > 0 else None
    except (ValueError, TypeError):
        return None


def _format_research_directive(work_dir, substrate):
    """A loud, actionable research-first directive — but ONLY when the substrate is too thin
    for real commentary (no dialogue/story spine) and no background_research.json exists yet.

    The narration's quality ceiling is how much story context the agent has: with only frame
    descriptions it can only narrate pixels, so research the title FIRST (see
    references/research-guide.md). Fires only for thin/empty substrate — NOT merely because a
    title was given — so a dialogue-rich titled run is never nagged.
    """
    if (Path(work_dir) / "background_research.json").exists():
        return []  # already researched; _format_background_research surfaces it
    if not (substrate and substrate.get("level") in ("thin", "empty")):
        return []  # rich enough to write from dialogue/spine; do not nag
    context = str(CONFIG.get("context_info") or "").strip()
    return [
        "## ⚑ Research the story FIRST (do this before writing narration)",
        "",
        "Reason: the understanding substrate is thin — no dialogue/story spine, only frame",
        "descriptions, so without research the narration can only describe pixels.",
        "1. Pull the title/keywords from `--context`, the filename, or the user's description"
        + (f" (context: {context})." if context else "."),
        "2. Use any available web-search/browser tool to look up synopsis, characters, and",
        "   relationships (see `references/research-guide.md`).",
        "3. Write `work_dir/background_research.json`, then re-read this brief and write narration",
        "   that names people and reads the picture through the plot.",
        "4. If no tool/network or nothing found: skip — keep beats sparse and strictly grounded in",
        "   the visible ASR/frame evidence rather than inventing drama.",
        "",
    ]


def _load_cut_output_spans_for_brief(work_dir, *, required=False):
    """Load fresh source→output spans for cut pass 2 brief evidence.

    Pass 2 narration is authored against edited_source.mp4's OUTPUT timeline, so
    ASR chunks and timeline_fusion must use the same OUTPUT clock. Before pass 2
    (no edited_source.mp4 yet), callers may fall back to source-time evidence; once
    pass 2 exists, missing/stale validated spans are a hard contract failure.
    """

    def fail(reason):
        if required:
            raise SystemExit(
                "cut pass2 brief requires fresh clip_plan_validated.json with explicit "
                f"finite source/output spans ({reason})"
            )
        return None

    work_dir = Path(work_dir)
    if not (work_dir / "edited_source.mp4").exists():
        return None
    validated_path = work_dir / "clip_plan_validated.json"
    if not validated_path.exists():
        return fail("missing clip_plan_validated.json")
    try:
        plan = json.loads(validated_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return fail("invalid clip_plan_validated.json")
    if not isinstance(plan, dict):
        return fail("clip_plan_validated.json is not an object")
    raw_path = work_dir / "clip_plan.json"
    if raw_path.exists():
        try:
            raw_plan = json.loads(raw_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return fail("invalid clip_plan.json")
        if plan.get("raw_plan_fingerprint") != stable_hash(raw_plan):
            return fail("stale clip_plan_validated.json")
    clips = plan.get("clips")
    if not isinstance(clips, list):
        return fail("clips is not a list")
    spans = []
    for clip in clips:
        if not isinstance(clip, dict):
            continue
        if not all(
            key in clip
            for key in ("source_start", "source_end", "output_start", "output_end")
        ):
            return fail("clip missing source/output span fields")
        try:
            ss = float(clip["source_start"])
            se = float(clip["source_end"])
            os_ = float(clip["output_start"])
            oe = float(clip["output_end"])
        except (TypeError, ValueError):
            return fail("non-numeric clip span")
        if not all(math.isfinite(x) for x in (ss, se, os_, oe)):
            return fail("non-finite clip span")
        if se <= ss or oe <= os_:
            return fail("non-positive clip span")
        spans.append(
            {
                "source_start": ss,
                "source_end": se,
                "output_start": os_,
                "output_end": oe,
            }
        )
    return spans or fail("no valid clips")


def _source_output_overlaps_for_brief(start, end, spans):
    overlaps = []
    for span in spans or []:
        source_start = max(float(start), span["source_start"])
        source_end = min(float(end), span["source_end"])
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
            }
        )
    return overlaps


def _remap_frame_facts_for_brief(frame_facts, overlap):
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


def _remap_scenes_to_output_for_brief(scenes, spans):
    if not spans:
        return scenes or []
    out = []
    for scene in scenes or []:
        if not isinstance(scene, dict):
            continue
        try:
            start = float(scene.get("start", 0))
            end = float(scene.get("end", start))
        except (TypeError, ValueError):
            continue
        overlaps = _source_output_overlaps_for_brief(start, end, spans)
        for part_idx, overlap in enumerate(overlaps):
            item = dict(scene)
            item["start"] = round(overlap["output_start"], 3)
            item["end"] = round(overlap["output_end"], 3)
            item["frame_facts"] = _remap_frame_facts_for_brief(
                scene.get("frame_facts"), overlap
            )
            if len(overlaps) > 1:
                item["scene_id"] = f"{scene.get('scene_id', '?')}.{part_idx}"
            out.append(item)
    out.sort(key=lambda x: (float(x.get("start", 0)), float(x.get("end", 0))))
    return out


def _remap_segments_to_output_for_brief(segments, spans):
    if not spans:
        return segments or []
    out = []
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        try:
            start = float(seg.get("start", 0))
            end = float(seg.get("end", start))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        for overlap in _source_output_overlaps_for_brief(start, end, spans):
            item = dict(seg)
            item["start"] = round(overlap["output_start"], 3)
            item["end"] = round(overlap["output_end"], 3)
            if "duration" in item:
                item["duration"] = round(item["end"] - item["start"], 3)
            out.append(item)
    out.sort(key=lambda x: (float(x.get("start", 0)), float(x.get("end", 0))))
    return out


def _remap_brief_evidence_to_output_timeline(
    work_dir, scenes_analysis, asr_result, silence_periods, *, required=False
):
    spans = _load_cut_output_spans_for_brief(work_dir, required=required)
    if not spans:
        return scenes_analysis, asr_result, silence_periods
    return (
        _remap_scenes_to_output_for_brief(scenes_analysis, spans),
        _remap_segments_to_output_for_brief(asr_result, spans),
        _remap_segments_to_output_for_brief(silence_periods, spans),
    )


def _sentence_entry_anchors_for_brief(work_dir, edit_mode):
    """Load sentence anchors and remap them to cut OUTPUT time when needed."""
    work_dir = Path(work_dir)
    source_path = work_dir / "speech_boundary_anchors.json"
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    anchors = payload.get("sentence_anchors", []) if isinstance(payload, dict) else []
    anchors = [dict(item) for item in anchors if isinstance(item, dict)]
    if edit_mode != "cut" or not (work_dir / "edited_source.mp4").exists():
        return anchors

    spans = _load_cut_output_spans_for_brief(work_dir, required=True)
    remapped = []
    for anchor in anchors:
        try:
            source_time = float(anchor.get("time"))
        except (TypeError, ValueError):
            continue
        for span in spans:
            if span["source_start"] - 0.05 <= source_time <= span["source_end"] + 0.05:
                item = dict(anchor)
                item["source_time"] = round(source_time, 3)
                item["time"] = round(
                    span["output_start"] + source_time - span["source_start"], 3
                )
                try:
                    source_pause_start = float(
                        anchor.get("pause_start", source_time - 0.12)
                    )
                except (TypeError, ValueError):
                    source_pause_start = source_time - 0.12
                # Preserve the measured safe pause in OUTPUT time too. Leaving pause_start
                # in SOURCE time made cut-mode lint compare two different clocks.
                source_pause_start = max(
                    span["source_start"], min(source_pause_start, source_time)
                )
                item["source_pause_start"] = round(source_pause_start, 3)
                item["pause_start"] = round(
                    span["output_start"] + source_pause_start - span["source_start"], 3
                )
                remapped.append(item)
                break
    out_payload = {
        "schema_version": 1,
        "artifact": "speech_boundary_anchors_output.json",
        "timeline": "cut_output",
        "source_artifact": "speech_boundary_anchors.json",
        "sentence_anchors": sorted(remapped, key=lambda item: float(item["time"])),
    }
    (work_dir / "speech_boundary_anchors_output.json").write_text(
        json.dumps(out_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return out_payload["sentence_anchors"]


def _format_sentence_entry_anchors_for_brief(work_dir, edit_mode):
    anchors = []
    for anchor in _sentence_entry_anchors_for_brief(work_dir, edit_mode):
        if anchor.get("confidence") not in {"high", "medium"}:
            continue
        try:
            when = float(anchor.get("time"))
        except (TypeError, ValueError):
            continue
        anchors.append((when, anchor))
    if not anchors:
        return []
    lines = [
        "## 原声句末安全切入点",
        "",
        "这些时间是 ASR 句末标点与短声学停顿对齐后的旁白安全入口。旁白在原声已开始后切入时，"
        "必须从其中一个点开始；否则会在 TTS 前被 `interrupts_source_sentence` 硬阻断。",
        "- 调整方式：优先把 `start` 移到建议锚点；放不下时缩短文本、移动整块或删除该旁白，不能让脚本静默挪音频。",
        '- 原声句子完整性是硬约束：切入块写 `"source_entry_policy": "sentence_boundary"`；'
        "不存在 `intentional_interrupt` 绕过方式。没有后续可靠句末锚点时，移动、缩短或删除旁白块。",
    ]
    for when, anchor in anchors:
        tail = str(anchor.get("text_tail") or "").strip()
        confidence = anchor.get("confidence", "unknown")
        source_suffix = ""
        if "source_time" in anchor:
            source_suffix = f" (SOURCE {float(anchor['source_time']):.2f}s)"
        lines.append(f"- {when:.2f}s [{confidence}]{source_suffix} {tail}")
    lines.append("")
    return lines


def _format_output_clip_list(work_dir):
    """List the kept clips on the OUTPUT timeline (cut-first/narrate-second pass 2), so the
    agent narrates against the real rendered cut instead of the source timeline."""
    path = Path(work_dir) / "clip_plan_validated.json"
    if not path.exists():
        return []
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    clips = (plan.get("clips") if isinstance(plan, dict) else plan) or []
    out = ["## Kept clips on the OUTPUT timeline", ""]
    for c in clips:
        if not isinstance(c, dict):
            continue
        try:
            os_, oe = float(c.get("output_start")), float(c.get("output_end"))
            ss, se = float(c.get("source_start")), float(c.get("source_end"))
        except (TypeError, ValueError):
            continue
        reason = str(c.get("reason", "")).strip()
        source_id = c.get("source_id", c.get("source", "0"))
        clip_id = c.get("id", c.get("clip_id", "?"))
        out.append(
            f"- OUTPUT {os_:.1f}–{oe:.1f}s ← SOURCE[{source_id}] {ss:.1f}–{se:.1f}s (clip_id={clip_id})"
            + (f" — {reason}" if reason else "")
        )
    out.append("")
    return out if len(out) > 2 else []


def _write_deslop_qc_requirements(work_dir):
    """Write the stable deslop QC contract consumed by deslop_qc.py.

    style_card_required defaults to False (advisory): a missing style_card.json is a
    warning, not a render-blocking error. A future opt-in run can set it True to make
    style_card.json a hard requirement — deslop_qc.py reads that field.
    """
    payload = {
        "schema_version": 1,
        "style_card_required": False,
    }
    path = Path(work_dir) / "deslop_qc_requirements.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
