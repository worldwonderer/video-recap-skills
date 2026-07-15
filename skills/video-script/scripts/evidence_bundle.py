"""Select, validate, and render bounded review evidence."""

import math

import re


EVIDENCE_CONTRACT_VERSION = 1

COVERAGE_POLICY_VERSION = "coverage_policy_v1"


def _safe_time(item, key, default=0.0):
    try:
        value = float(item.get(key, default))
    except (AttributeError, TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _overall_duration(scenes, asr, narration):
    ends = []
    for seq in (scenes or [], asr or [], narration or []):
        for item in seq:
            if isinstance(item, dict):
                ends.append(_safe_time(item, "end", _safe_time(item, "start", 0.0)))
    return max(ends or [0.0])


def _range(start, end, reason, priority=2):
    start = max(0.0, float(start))
    end = max(start, float(end))
    return {
        "start": round(start, 3),
        "end": round(end, 3),
        "selection_reason": reason,
        "priority": priority,
    }


def _merge_ranges(ranges):
    merged = []
    for r in sorted(
        (r for r in ranges if r["end"] > r["start"]),
        key=lambda x: (x["start"], x["end"]),
    ):
        if not merged or r["start"] > merged[-1]["end"]:
            merged.append(dict(r))
            continue
        merged[-1]["end"] = max(merged[-1]["end"], r["end"])
        reasons = {
            part
            for part in str(merged[-1].get("selection_reason", "")).split("+")
            if part
        }
        reasons.update(
            part for part in str(r.get("selection_reason", "")).split("+") if part
        )
        order = {
            "beginning": 0,
            "middle": 1,
            "end": 2,
            "longest_gap": 3,
            "narration_window": 4,
        }
        merged[-1]["selection_reason"] = "+".join(
            sorted(reasons, key=lambda x: order.get(x, 99))
        )
        merged[-1]["priority"] = min(
            merged[-1].get("priority", 2), r.get("priority", 2)
        )
    return merged


def coverage_policy_v1(
    scenes,
    asr_result,
    narration,
    *,
    max_coverage_ranges=12,
    min_baseline_ranges=6,
    window_seconds=30.0,
):
    """Deterministic B/M/E floor → longest-gap fill → narration-window coverage."""
    duration = _overall_duration(scenes, asr_result, narration)
    if duration <= 0:
        return {
            "coverage_policy_version": COVERAGE_POLICY_VERSION,
            "selected_ranges": [],
            "dropped_ranges": [],
            "duration": 0.0,
        }
    width = min(float(window_seconds), max(5.0, duration / 8.0))
    ranges = [
        _range(0.0, min(duration, width), "beginning", 0),
        _range(
            max(0.0, duration / 2.0 - width / 2.0),
            min(duration, duration / 2.0 + width / 2.0),
            "middle",
            0,
        ),
        _range(max(0.0, duration - width), duration, "end", 0),
    ]
    ranges = _merge_ranges(ranges)
    while len(ranges) < min_baseline_ranges:
        ranges = _merge_ranges(ranges)
        gaps = []
        cursor = 0.0
        for r in ranges:
            if r["start"] > cursor:
                gaps.append((r["start"] - cursor, cursor, r["start"]))
            cursor = max(cursor, r["end"])
        if cursor < duration:
            gaps.append((duration - cursor, cursor, duration))
        if not gaps:
            break
        gap_len, gs, ge = max(gaps, key=lambda g: (g[0], -g[1]))
        if gap_len <= 0.001:
            break
        center = (gs + ge) / 2.0
        half = min(width / 2.0, gap_len / 2.0)
        ranges.append(_range(center - half, center + half, "longest_gap", 2))
    before_narration = len(ranges)
    for i, seg in enumerate(narration or []):
        if not isinstance(seg, dict):
            continue
        start = _safe_time(seg, "start", None)
        end = _safe_time(seg, "end", start)
        if start is None or end <= start:
            continue
        priority = 1 if str(seg.get("narration", "")).strip() else 2
        r = _range(
            max(0.0, start - 3.0),
            min(duration, end + 3.0),
            "narration_window",
            priority,
        )
        r["narration_segment"] = i
        ranges.append(r)
    merged = _merge_ranges(ranges)
    dropped = []
    if len(merged) > max_coverage_ranges:
        protected = [
            r
            for r in merged
            if any(x in r["selection_reason"] for x in ("beginning", "middle", "end"))
        ]
        candidates = [r for r in merged if r not in protected]
        candidates.sort(
            key=lambda r: (
                r.get("priority", 2),
                -float(r["end"] - r["start"]),
                r["start"],
            )
        )
        keep = protected + candidates[: max(0, max_coverage_ranges - len(protected))]
        keep_ids = {id(r) for r in keep}
        dropped = [r for r in merged if id(r) not in keep_ids]
        merged = sorted(keep, key=lambda r: (r["start"], r["end"]))
    for r in merged + dropped:
        r.pop("priority", None)
    return {
        "coverage_policy_version": COVERAGE_POLICY_VERSION,
        "selected_ranges": merged,
        "dropped_ranges": dropped,
        "dropped_range_count": len(dropped),
        "duration": round(duration, 3),
        "baseline_range_count_before_narration": before_narration,
    }


def _in_ranges(start, end, ranges):
    return any(
        float(r["start"]) < end and float(r["end"]) > start for r in ranges or []
    )


def _scene_evidence_text(scene):
    desc = str(scene.get("description", "")).strip().replace("\n", " ")
    facts = scene.get("frame_facts")
    picks = []
    if isinstance(facts, dict):

        def fact_sort_key(value):
            try:
                return (0, float(value))
            except (TypeError, ValueError):
                return (1, str(value))

        for ts in sorted(facts.keys(), key=fact_sort_key):
            vals = facts[ts]
            picks.extend(vals if isinstance(vals, list) else [str(vals)])
    elif isinstance(facts, list):
        for f in facts:
            picks.append(
                str(f.get("fact", f.get("text", ""))).strip()
                if isinstance(f, dict)
                else str(f).strip()
            )
    fact_txt = "；".join(p for p in picks[:4] if p)
    return (desc + (" | 帧实: " + fact_txt if fact_txt else "")).strip()


def _research_context_items(research):
    if not isinstance(research, dict) or not research:
        return []
    items = []
    for key in ("synopsis", "episode_context", "worldbuilding"):
        value = _clip_text(research.get(key), 400)
        if value:
            items.append(
                {
                    "id": f"research:{key}",
                    "source": "research",
                    "clock": None,
                    "source_id": "context",
                    "start": None,
                    "end": None,
                    "text": value,
                    "support": "context_only",
                    "confidence": "medium",
                }
            )
    for name, desc in (
        list((research.get("characters") or {}).items())[:12]
        if isinstance(research.get("characters"), dict)
        else []
    ):
        text = f"{_clip_text(name, 80)}：{_clip_text(desc, 200)}"
        items.append(
            {
                "id": f"research:character:{len(items)}",
                "source": "research",
                "clock": None,
                "source_id": "context",
                "start": None,
                "end": None,
                "text": text,
                "support": "context_only",
                "confidence": "medium",
            }
        )
    details = research.get("character_details")
    if isinstance(details, dict):
        for name, info in list(details.items())[:8]:
            if not isinstance(info, dict):
                continue
            aliases = (
                info.get("aliases") if isinstance(info.get("aliases"), list) else []
            )
            bits = []
            if aliases:
                bits.append(
                    "aliases=" + "/".join(_clip_text(a, 40) for a in aliases[:4])
                )
            role = _clip_text(info.get("role"), 120)
            if role:
                bits.append(role)
            if bits:
                items.append(
                    {
                        "id": f"research:detail:{len(items)}",
                        "source": "research",
                        "clock": None,
                        "source_id": "context",
                        "start": None,
                        "end": None,
                        "text": f"{name}: {'; '.join(bits)}",
                        "support": "context_only",
                        "confidence": "medium",
                    }
                )
    return items


def filter_evidence_by_ranges(vlm_analysis, asr_result, ranges, *, timeline="source"):
    """Pure compatibility seam: collect visual/ASR evidence items inside ranges."""
    clock = "output" if timeline == "cut_output" else "source"
    items = []
    dropped = {"visual": 0, "asr": 0}
    for i, scene in enumerate(vlm_analysis or []):
        if not isinstance(scene, dict):
            continue
        start = _safe_time(scene, "start")
        end = _safe_time(scene, "end", start)
        if end <= start or not _in_ranges(start, end, ranges):
            dropped["visual"] += 1
            continue
        item = {
            "id": f"visual:{i}",
            "source": "visual",
            "clock": clock,
            "source_id": str(scene.get("source_id", "0")),
            "start": round(start, 3),
            "end": round(end, 3),
            "text": _scene_evidence_text(scene),
            "support": "direct",
            "confidence": "high",
        }
        for k in (
            "source_start",
            "source_end",
            "source_clip_id",
            "output_segment_index",
        ):
            if k in scene:
                item[k] = scene.get(k)
        items.append(item)
    for i, seg in enumerate(asr_result or []):
        if not isinstance(seg, dict):
            continue
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        start = _safe_time(seg, "start")
        end = _safe_time(seg, "end", start)
        if end <= start or not _in_ranges(start, end, ranges):
            dropped["asr"] += 1
            continue
        item = {
            "id": f"asr:{i}",
            "source": "asr",
            "clock": clock,
            "source_id": str(seg.get("source_id", "0")),
            "start": round(start, 3),
            "end": round(end, 3),
            "text": text,
            "support": "direct",
            "confidence": "high",
        }
        for k in (
            "source_start",
            "source_end",
            "source_clip_id",
            "output_segment_index",
        ):
            if k in seg:
                item[k] = seg.get(k)
        items.append(item)
    return {
        "items": items,
        "dropped_visual_count": dropped["visual"],
        "dropped_asr_count": dropped["asr"],
    }


def build_review_coverage_metadata(bundle):
    """Pure compatibility seam: summarize the evidence coverage contract."""
    items = bundle.get("items") or [] if isinstance(bundle, dict) else []
    coverage = bundle.get("coverage", {}) if isinstance(bundle, dict) else {}
    metadata = bundle.get("metadata", {}) if isinstance(bundle, dict) else {}
    return {
        "coverage_policy_version": coverage.get(
            "coverage_policy_version", COVERAGE_POLICY_VERSION
        ),
        "time_ranges": coverage.get("selected_ranges", []),
        "dropped_ranges": coverage.get("dropped_ranges", []),
        "dropped_range_count": coverage.get(
            "dropped_range_count", len(coverage.get("dropped_ranges", []))
        ),
        "scene_count": metadata.get(
            "reviewed_scene_count",
            sum(1 for item in items if item.get("source") == "visual"),
        ),
        "asr_count": metadata.get(
            "reviewed_asr_count",
            sum(1 for item in items if item.get("source") == "asr"),
        ),
        "dropped_scene_count": metadata.get("dropped_scene_count", 0),
        "dropped_asr_count": metadata.get("dropped_asr_count", 0),
    }


def validate_public_evidence_contract(bundle):
    """Pure compatibility seam: validate the public evidence bundle shape.

    Returns a non-throwing report so callers can use it in tests or advisory QC paths.
    """
    errors = []
    warnings = []
    if not isinstance(bundle, dict):
        return {"valid": False, "errors": ["bundle must be a dict"], "warnings": []}
    if bundle.get("schema_version") != EVIDENCE_CONTRACT_VERSION:
        errors.append("unsupported schema_version")
    if bundle.get("clock") not in ("source", "output"):
        errors.append("clock must be source or output")
    for idx, item in enumerate(bundle.get("items") or []):
        if not isinstance(item, dict):
            errors.append(f"items[{idx}] must be a dict")
            continue
        if item.get("source") not in ("visual", "asr"):
            errors.append(f"items[{idx}].source must be visual or asr")
        if item.get("clock") not in ("source", "output"):
            errors.append(f"items[{idx}].clock must be source or output")
        if item.get("support") != "direct":
            warnings.append(f"items[{idx}].support is not direct")
        start, end = item.get("start"), item.get("end")
        try:
            if float(end) <= float(start):
                errors.append(f"items[{idx}] has non-positive time range")
        except (TypeError, ValueError):
            errors.append(f"items[{idx}] has invalid time range")
    for idx, item in enumerate(bundle.get("context_items") or []):
        if not isinstance(item, dict):
            errors.append(f"context_items[{idx}] must be a dict")
            continue
        if item.get("clock") is not None:
            errors.append(f"context_items[{idx}].clock must be null")
        if item.get("support") != "context_only":
            errors.append(f"context_items[{idx}].support must be context_only")
    return {"valid": not errors, "errors": errors, "warnings": warnings}


def build_evidence_bundle(
    vlm_analysis,
    asr_result,
    narration,
    *,
    timeline="source",
    research=None,
    warnings=None,
):
    clock = "output" if timeline == "cut_output" else "source"
    coverage = coverage_policy_v1(vlm_analysis, asr_result, narration)
    ranges = coverage.get("selected_ranges", [])
    filtered = filter_evidence_by_ranges(
        vlm_analysis, asr_result, ranges, timeline=timeline
    )
    items = filtered["items"]
    dropped_scenes = filtered["dropped_visual_count"]
    dropped_asr = filtered["dropped_asr_count"]
    context_items = _research_context_items(research)
    return {
        "schema_version": EVIDENCE_CONTRACT_VERSION,
        "timeline": timeline,
        "clock": clock,
        "items": items,
        "context_items": context_items,
        "coverage": coverage,
        "warnings": list(warnings or []),
        "metadata": {
            "reviewed_scene_count": sum(1 for x in items if x["source"] == "visual"),
            "reviewed_asr_count": sum(1 for x in items if x["source"] == "asr"),
            "dropped_scene_count": dropped_scenes,
            "dropped_asr_count": dropped_asr,
        },
    }


def render_evidence_bundle(bundle, *, limit_items=220):
    clock = str(bundle.get("clock") or "source").upper()
    lines = [f"## Timeline evidence (clock={clock}; source=visual/asr)"]
    items = bundle.get("items") or []
    if not items:
        lines.append("(无 timeline evidence)")
    for item in items[:limit_items]:
        label = "画面" if item.get("source") == "visual" else "对白"
        backref = ""
        if (
            item.get("clock") == "output"
            and "source_start" in item
            and "source_end" in item
        ):
            backref = f" ← SOURCE {float(item['source_start']):.1f}-{float(item['source_end']):.1f}s"
            if item.get("output_segment_index") is not None:
                backref += f" clip#{item.get('output_segment_index')}"
        lines.append(
            f"[{clock} {float(item.get('start', 0)):.1f}-{float(item.get('end', 0)):.1f}s {label} id={item.get('id')}{backref}] {item.get('text', '')}"
        )
    if len(items) > limit_items:
        lines.append(
            f"... dropped from prompt: {len(items) - limit_items} items (artifact metadata keeps counts)"
        )
    context = bundle.get("context_items") or []
    lines.extend(
        [
            "",
            "## Context-only evidence (clock=null; source=research/user_context; advisory, not current-timeline fact)",
        ]
    )
    if not context:
        lines.append("(无 context-only evidence)")
    for item in context[:40]:
        lines.append(
            f"[clock=null {item.get('source')} support=context_only id={item.get('id')}] {item.get('text', '')}"
        )
    return "\n".join(lines)


def _clip_text(text, limit):
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value[:limit]
