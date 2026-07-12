#!/usr/bin/env python3
"""video-script narration reviewer (LLM-as-judge).

A separate *quality* pass over an agent-written narration.json — distinct from the
mechanical validate.py (which only checks budget/timing/density). The reviewer reads the
draft against the visual/ASR grounding and the writing rubric, then returns severity-rated
findings (hallucination, weak hook, no throughline, narrating-the-picture, density, pacing,
cliché, incomplete sentence). It does NOT edit narration.json — the writer revises and re-runs.

Output: narration_review.json (structured) + narration_review.md (readable). Advisory:
verdict REVISE means "fix and re-review"; it never blocks (validate.py is the hard gate).
"""
import argparse
import json
import math
import re
from pathlib import Path

from lib import CONFIG, log, api_call, stable_hash

CATEGORIES = [
    "hallucination", "weak_hook", "no_throughline", "narrating_picture",
    "density", "pacing", "cliche", "incomplete", "disjoint_handoff",
    "promise_mismatch", "low_information_gain", "not_write_for_ear",
    "grounding_risk", "original_audio_conflict", "subtitle_readability",
    "ai_flavor", "weak_payoff", "style_mismatch", "packaging_mismatch",
    "example_entity_leak", "other",
]

SCORECARD_KEYS = [
    "promise_match", "hook_3s", "first_15s_delivery", "spine_clarity",
    "stakes_escalation", "information_gain", "spoken_language",
    "sentence_brevity", "tts_pacing", "grounding", "original_audio_use",
    "subtitle_readability", "ending_payoff", "style_consistency", "ai_flavor",
    "packaging_consistency",
]

# Categories whose findings are allowed to keep severity=error and thus gate strict
# mode. Everything else is craft/subjective and is clamped to at most "warning".
FACTUAL_CATEGORIES = {"hallucination", "incomplete"}

EVIDENCE_CONTRACT_VERSION = 1
COVERAGE_POLICY_VERSION = "coverage_policy_v1"

RUBRIC = """你是中文视频解说稿的严格评审。依据以下规则审阅草稿，只指出真实问题，宁缺毋滥：
1. 反幻觉（最重要）：解说里的人物、动作、因果、关系必须由带标签的 evidence 支撑。画面/对白是 timeline evidence（clock=SOURCE 或 OUTPUT）；背景资料/user_context 只能作为 context-only（clock=null）辅助识别/消歧。research-only 不能升级成当前画面强事实；若与 research 一致但画面/对白里看不到，最多 severity=suggestion/category=grounding_risk，不要判 error；只有与全部可得证据矛盾才是 severity=error, category=hallucination，并指出冲突证据。
2. 钩子：开头 1-2 段要制造悬念/利害，不是交代场景。弱钩子 → weak_hook。
3. 主线：应有一条贯穿主线（目标/关系/悬念），每段推进它，不要每个场景从头讲。缺主线 → no_throughline。
4. 给信息而非念画面：观众看得见动作表情；解说要讲动机/关系/潜台词/剧情意义。复述画面 → narrating_picture。
5. 密度/节奏：连续铺底、短句、相邻段不要断太久；过疏/过密/拖沓 → density 或 pacing。
6. 去废词：删空泛形容（"危机四伏""震撼人心"）→ cliche。
7. 完整句子：半句话/未收尾 → incomplete。
8. 段落衔接：解说块要为随后的原声留白铺垫，下一块要承接原声刚呈现的内容；若两块各说各的、原声进来接不上 → disjoint_handoff。
9. 结尾回收：结尾要兑现开头承诺/主线情绪，不要突然停、只复述最后画面、没有情绪/信息回报；弱回收 → weak_payoff。
10. 风格一致性：若提供 style_card.json，把它当作表达意图/语气/节奏边界；不符合意图 → style_mismatch。不要把 style_card 当标题/封面/首句包装计划。
11. 包装一致性：若提供 packaging_plan.json，只评估标题/封面/首句/卖点承诺与正文兑现；不一致 → packaging_mismatch。不要把 packaging_plan 当正文风格卡。
12. 去AI味：若出现模板化、空泛拔高、过度对仗、机械转折、明显 agent 示例残留，可报 ai_flavor；若出现示例人物/占位实体泄漏（如未替换示例名、模板角色）→ example_entity_leak。deslop_qc.json 是 deterministic local report-only QC，不是 AIGC detector，不自动重写；只能作为证据参考，不能仅凭它判定。
另外给一份内容效果 scorecard（1-5，advisory：除事实矛盾/残句外不要据此给 error；缺项可以省略，系统会保留为 null/未评分）：promise_match/hook_3s/first_15s_delivery/spine_clarity/stakes_escalation/information_gain/spoken_language/sentence_brevity/tts_pacing/grounding/original_audio_use/subtitle_readability/ending_payoff/style_consistency/ai_flavor/packaging_consistency。ai_flavor 分数含义：5=自然、人味强，1=AI味明显。
只返回 JSON（不要额外解释），格式：
{"verdict":"PASS|REVISE|FAIL","summary":"一两句总体判断","scorecard":{"promise_match":1-5,"hook_3s":1-5,"first_15s_delivery":1-5,"spine_clarity":1-5,"stakes_escalation":1-5,"information_gain":1-5,"spoken_language":1-5,"sentence_brevity":1-5,"tts_pacing":1-5,"grounding":1-5,"original_audio_use":1-5,"subtitle_readability":1-5,"ending_payoff":1-5,"style_consistency":1-5,"ai_flavor":1-5,"packaging_consistency":1-5},"hook_candidates_review":[{"candidate":"首句","type":"suspense|contrast|stakes","score":1-5,"keep":true}],"retention_risk_points":[{"time":"00:28","risk":"为什么可能掉人","fix":"怎么改"}],"highest_return_edits":["最值得改的动作"],"information_gain_notes":[{"segment":0,"label":"motive|relationship|stakes|foreshadowing|payoff|context|visual_restatement","note":"证据/改法"}],"spoken_language_rewrites":[{"segment":0,"original":"原句","rewrite":"口语改写","why":"为什么更适合听"}],"grounding_assertions":[{"segment":0,"assertion":"人物/关系/因果断言","source":"visual|asr|research|user_context|unsupported","risk":"谨慎说明"}],"findings":[{"segment":<草稿段号(从0起)或null表示整体>,"severity":"error|warning|suggestion","category":"<上面类别之一>","issue":"问题","fix":"具体改法"}]}"""


def _load(work_dir, name):
    path = Path(work_dir) / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None




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
    if raw_plan is not None and plan.get("raw_plan_fingerprint") != stable_hash(raw_plan):
        return None
    clips = plan.get("clips")
    if not isinstance(clips, list):
        return None
    spans = []
    for clip in clips:
        if not isinstance(clip, dict):
            continue
        if not all(key in clip for key in ("source_start", "source_end", "output_start", "output_end")):
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
        spans.append({
            "source_start": source_start,
            "source_end": source_end,
            "output_start": output_start,
            "output_end": output_end,
            "source_id": str(clip.get("source_id", clip.get("source", "0"))),
            "source_clip_id": clip.get("source_clip_id", clip.get("id", clip.get("clip_id"))),
            "output_segment_index": len(spans),
        })
    return spans or None


def _source_output_overlaps(start, end, spans):
    overlaps = []
    for span in spans or []:
        source_start = max(start, span["source_start"])
        source_end = min(end, span["source_end"])
        if source_end <= source_start:
            continue
        output_start = span["output_start"] + (source_start - span["source_start"])
        output_end = span["output_start"] + (source_end - span["source_start"])
        overlaps.append({
            "source_start": source_start,
            "source_end": source_end,
            "output_start": output_start,
            "output_end": output_end,
            "source_id": span.get("source_id", "0"),
            "source_clip_id": span.get("source_clip_id"),
            "output_segment_index": span.get("output_segment_index"),
        })
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
        overlaps = _source_output_overlaps(start, end, clip_spans)
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
        for overlap in _source_output_overlaps(start, end, clip_spans):
            item = dict(seg)
            item["start"] = round(overlap["output_start"], 3)
            item["end"] = round(overlap["output_end"], 3)
            item["source_start"] = round(overlap["source_start"], 3)
            item["source_end"] = round(overlap["source_end"], 3)
            item["source_id"] = overlap.get("source_id", "0")
            item["source_clip_id"] = overlap.get("source_clip_id")
            item["output_segment_index"] = overlap.get("output_segment_index")
            remapped_asr.append(item)

    remapped_scenes.sort(key=lambda x: (float(x.get("start", 0)), float(x.get("end", 0))))
    remapped_asr.sort(key=lambda x: (float(x.get("start", 0)), float(x.get("end", 0))))
    return remapped_scenes, remapped_asr


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
    return {"start": round(start, 3), "end": round(end, 3), "selection_reason": reason, "priority": priority}


def _merge_ranges(ranges):
    merged = []
    for r in sorted((r for r in ranges if r["end"] > r["start"]), key=lambda x: (x["start"], x["end"])):
        if not merged or r["start"] > merged[-1]["end"]:
            merged.append(dict(r))
            continue
        merged[-1]["end"] = max(merged[-1]["end"], r["end"])
        reasons = {part for part in str(merged[-1].get("selection_reason", "")).split("+") if part}
        reasons.update(part for part in str(r.get("selection_reason", "")).split("+") if part)
        order = {"beginning": 0, "middle": 1, "end": 2, "longest_gap": 3, "narration_window": 4}
        merged[-1]["selection_reason"] = "+".join(sorted(reasons, key=lambda x: order.get(x, 99)))
        merged[-1]["priority"] = min(merged[-1].get("priority", 2), r.get("priority", 2))
    return merged


def coverage_policy_v1(scenes, asr_result, narration, *, max_coverage_ranges=12, min_baseline_ranges=6, window_seconds=30.0):
    """Deterministic B/M/E floor → longest-gap fill → narration-window coverage."""
    duration = _overall_duration(scenes, asr_result, narration)
    if duration <= 0:
        return {"coverage_policy_version": COVERAGE_POLICY_VERSION, "selected_ranges": [], "dropped_ranges": [], "duration": 0.0}
    width = min(float(window_seconds), max(5.0, duration / 8.0))
    ranges = [
        _range(0.0, min(duration, width), "beginning", 0),
        _range(max(0.0, duration / 2.0 - width / 2.0), min(duration, duration / 2.0 + width / 2.0), "middle", 0),
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
        r = _range(max(0.0, start - 3.0), min(duration, end + 3.0), "narration_window", priority)
        r["narration_segment"] = i
        ranges.append(r)
    merged = _merge_ranges(ranges)
    dropped = []
    if len(merged) > max_coverage_ranges:
        protected = [r for r in merged if any(x in r["selection_reason"] for x in ("beginning", "middle", "end"))]
        candidates = [r for r in merged if r not in protected]
        candidates.sort(key=lambda r: (r.get("priority", 2), -float(r["end"] - r["start"]), r["start"]))
        keep = protected + candidates[:max(0, max_coverage_ranges - len(protected))]
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
    return any(float(r["start"]) < end and float(r["end"]) > start for r in ranges or [])


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
            picks.append(str(f.get("fact", f.get("text", ""))).strip() if isinstance(f, dict) else str(f).strip())
    fact_txt = "；".join(p for p in picks[:4] if p)
    return (desc + (" | 帧实: " + fact_txt if fact_txt else "")).strip()


def _research_context_items(research):
    if not isinstance(research, dict) or not research:
        return []
    items = []
    for key in ("synopsis", "episode_context", "worldbuilding"):
        value = _clip_text(research.get(key), 400)
        if value:
            items.append({"id": f"research:{key}", "source": "research", "clock": None, "source_id": "context", "start": None, "end": None, "text": value, "support": "context_only", "confidence": "medium"})
    for name, desc in list((research.get("characters") or {}).items())[:12] if isinstance(research.get("characters"), dict) else []:
        text = f"{_clip_text(name, 80)}：{_clip_text(desc, 200)}"
        items.append({"id": f"research:character:{len(items)}", "source": "research", "clock": None, "source_id": "context", "start": None, "end": None, "text": text, "support": "context_only", "confidence": "medium"})
    details = research.get("character_details")
    if isinstance(details, dict):
        for name, info in list(details.items())[:8]:
            if not isinstance(info, dict):
                continue
            aliases = info.get("aliases") if isinstance(info.get("aliases"), list) else []
            bits = []
            if aliases:
                bits.append("aliases=" + "/".join(_clip_text(a, 40) for a in aliases[:4]))
            role = _clip_text(info.get("role"), 120)
            if role:
                bits.append(role)
            if bits:
                items.append({"id": f"research:detail:{len(items)}", "source": "research", "clock": None, "source_id": "context", "start": None, "end": None, "text": f"{name}: {'; '.join(bits)}", "support": "context_only", "confidence": "medium"})
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
        item = {"id": f"visual:{i}", "source": "visual", "clock": clock, "source_id": str(scene.get("source_id", "0")), "start": round(start, 3), "end": round(end, 3), "text": _scene_evidence_text(scene), "support": "direct", "confidence": "high"}
        for k in ("source_start", "source_end", "source_clip_id", "output_segment_index"):
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
        item = {"id": f"asr:{i}", "source": "asr", "clock": clock, "source_id": str(seg.get("source_id", "0")), "start": round(start, 3), "end": round(end, 3), "text": text, "support": "direct", "confidence": "high"}
        for k in ("source_start", "source_end", "source_clip_id", "output_segment_index"):
            if k in seg:
                item[k] = seg.get(k)
        items.append(item)
    return {"items": items, "dropped_visual_count": dropped["visual"], "dropped_asr_count": dropped["asr"]}


def build_review_coverage_metadata(bundle):
    """Pure compatibility seam: summarize the evidence coverage contract."""
    items = bundle.get("items") or [] if isinstance(bundle, dict) else []
    coverage = bundle.get("coverage", {}) if isinstance(bundle, dict) else {}
    metadata = bundle.get("metadata", {}) if isinstance(bundle, dict) else {}
    return {
        "coverage_policy_version": coverage.get("coverage_policy_version", COVERAGE_POLICY_VERSION),
        "time_ranges": coverage.get("selected_ranges", []),
        "dropped_ranges": coverage.get("dropped_ranges", []),
        "dropped_range_count": coverage.get("dropped_range_count", len(coverage.get("dropped_ranges", []))),
        "scene_count": metadata.get("reviewed_scene_count", sum(1 for item in items if item.get("source") == "visual")),
        "asr_count": metadata.get("reviewed_asr_count", sum(1 for item in items if item.get("source") == "asr")),
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


def build_evidence_bundle(vlm_analysis, asr_result, narration, *, timeline="source", research=None, warnings=None):
    clock = "output" if timeline == "cut_output" else "source"
    coverage = coverage_policy_v1(vlm_analysis, asr_result, narration)
    ranges = coverage.get("selected_ranges", [])
    filtered = filter_evidence_by_ranges(vlm_analysis, asr_result, ranges, timeline=timeline)
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
        if item.get("clock") == "output" and "source_start" in item and "source_end" in item:
            backref = f" ← SOURCE {float(item['source_start']):.1f}-{float(item['source_end']):.1f}s"
            if item.get("output_segment_index") is not None:
                backref += f" clip#{item.get('output_segment_index')}"
        lines.append(f"[{clock} {float(item.get('start', 0)):.1f}-{float(item.get('end', 0)):.1f}s {label} id={item.get('id')}{backref}] {item.get('text','')}")
    if len(items) > limit_items:
        lines.append(f"... dropped from prompt: {len(items) - limit_items} items (artifact metadata keeps counts)")
    context = bundle.get("context_items") or []
    lines.extend(["", "## Context-only evidence (clock=null; source=research/user_context; advisory, not current-timeline fact)"])
    if not context:
        lines.append("(无 context-only evidence)")
    for item in context[:40]:
        lines.append(f"[clock=null {item.get('source')} support=context_only id={item.get('id')}] {item.get('text','')}")
    return "\n".join(lines)


def merge_review_findings(chunks):
    """Merge chunk review findings deterministically, keeping highest severity."""
    severity_rank = {"error": 3, "warning": 2, "suggestion": 1}
    by_key = {}
    for chunk in chunks or []:
        for f in (chunk or {}).get("findings", []) or []:
            if not isinstance(f, dict):
                continue
            key = (f.get("segment"), f.get("category"), f.get("issue"))
            old = by_key.get(key)
            if old is None or severity_rank.get(f.get("severity"), 0) > severity_rank.get(old.get("severity"), 0):
                by_key[key] = dict(f)
    return sorted(by_key.values(), key=lambda f: (f.get("segment") is None, f.get("segment") if f.get("segment") is not None else 10**9, f.get("category") or "", f.get("issue") or ""))




def _bundle_fingerprint(bundle):
    try:
        return stable_hash({
            "schema_version": bundle.get("schema_version"),
            "clock": bundle.get("clock"),
            "coverage": bundle.get("coverage"),
            "items": bundle.get("items"),
            "context_items": bundle.get("context_items"),
        })
    except (TypeError, ValueError, RecursionError) as exc:
        if isinstance(bundle, dict):
            bundle.setdefault("metadata", {})["evidence_bundle_fingerprint_warning"] = (
                f"evidence bundle fingerprint unavailable: {type(exc).__name__}"
            )
        return ""


def _bundle_fingerprint_warning(bundle):
    if not isinstance(bundle, dict):
        return "evidence bundle fingerprint unavailable: invalid bundle"
    warning = (bundle.get("metadata") or {}).get("evidence_bundle_fingerprint_warning")
    if warning:
        return warning
    return ""


def _append_warning_once(target, warning):
    if warning and warning not in target:
        target.append(warning)


def _bundle_prompt_size(bundle):
    return len(render_evidence_bundle(bundle))


def _chunk_evidence_bundle(bundle, *, max_items=80, max_chars=12000):
    """Split oversized evidence bundles for actual review calls.

    Chunks are deterministic and range-oriented: selected coverage ranges remain the
    contract, but each chunk carries only the timeline items whose spans overlap that
    range. Context-only research remains advisory and is repeated in each chunk so the
    judge can still use alias/background hints without upgrading them to timeline facts.
    """
    items = list(bundle.get("items") or [])
    if len(items) <= max_items and _bundle_prompt_size(bundle) <= max_chars:
        one = dict(bundle)
        one["chunk_index"] = 0
        one["chunk_count"] = 1
        fp = _bundle_fingerprint(bundle)
        fp_warning = _bundle_fingerprint_warning(bundle)
        one.setdefault("metadata", {})["evidence_bundle_fingerprint"] = fp
        if fp_warning:
            one["metadata"]["evidence_bundle_fingerprint_warning"] = fp_warning
            one.setdefault("warnings", list(bundle.get("warnings") or []))
            _append_warning_once(one["warnings"], fp_warning)
        return [one]
    ranges = bundle.get("coverage", {}).get("selected_ranges") or []
    chunks = []
    used_ids = set()
    for r in ranges:
        r_items = [item for item in items if _in_ranges(_safe_time(item, "start"), _safe_time(item, "end", _safe_time(item, "start")), [r])]
        if not r_items:
            continue
        for start in range(0, len(r_items), max_items):
            part = r_items[start:start + max_items]
            used_ids.update(id(item) for item in part)
            chunk = dict(bundle)
            chunk["items"] = part
            chunk["coverage"] = dict(bundle.get("coverage") or {})
            chunk["coverage"]["selected_ranges"] = [r]
            chunk["chunk_index"] = len(chunks)
            chunks.append(chunk)
    leftovers = [item for item in items if id(item) not in used_ids]
    for start in range(0, len(leftovers), max_items):
        chunk = dict(bundle)
        chunk["items"] = leftovers[start:start + max_items]
        chunk["coverage"] = dict(bundle.get("coverage") or {})
        chunk["coverage"]["selected_ranges"] = []
        chunk["chunk_index"] = len(chunks)
        chunks.append(chunk)
    if not chunks:
        chunk = dict(bundle)
        chunk["items"] = []
        chunk["chunk_index"] = 0
        chunks = [chunk]
    count = len(chunks)
    fp = _bundle_fingerprint(bundle)
    fp_warning = _bundle_fingerprint_warning(bundle)
    for chunk in chunks:
        chunk["chunk_count"] = count
        chunk.setdefault("metadata", {})["chunked_review"] = count > 1
        chunk["metadata"]["evidence_bundle_fingerprint"] = fp
        if fp_warning:
            chunk["metadata"]["evidence_bundle_fingerprint_warning"] = fp_warning
            chunk.setdefault("warnings", list(bundle.get("warnings") or []))
            _append_warning_once(chunk["warnings"], fp_warning)
    return chunks


def _merge_chunk_reviews(chunk_reviews):
    if not chunk_reviews:
        return parse_review_response("")
    if len(chunk_reviews) == 1:
        return chunk_reviews[0]
    verdict_rank = {"PASS": 0, "OK": 0, "REVISE": 1, "FAIL": 2}
    best = max(chunk_reviews, key=lambda r: verdict_rank.get(r.get("verdict"), 1))
    merged = dict(best)
    merged["findings"] = merge_review_findings(chunk_reviews)
    if any(f.get("severity") == "error" for f in merged["findings"]):
        merged["verdict"] = "FAIL" if best.get("verdict") == "FAIL" else "REVISE"
    else:
        merged["verdict"] = best.get("verdict", "REVISE")
    summaries = [str(r.get("summary", "")).strip() for r in chunk_reviews if str(r.get("summary", "")).strip()]
    merged["summary"] = summaries[0] if summaries else merged.get("summary", "")
    merged["chunked_review"] = {
        "chunk_count": len(chunk_reviews),
        "findings_before_merge": sum(len(r.get("findings") or []) for r in chunk_reviews),
        "findings_after_merge": len(merged.get("findings") or []),
    }
    return merged


def _research_guardrail_qc(review, context_items):
    assertions = [a for a in (review.get("grounding_assertions") or []) if isinstance(a, dict)]
    research_context_assertions = [
        a for a in assertions
        if str(a.get("source", "")).lower() == "research"
        or a.get("support") == "context_only"
        or a.get("clock") is None
    ]
    risk_assertions = [
        a for a in research_context_assertions
        if "spoiler" in str(a.get("risk", "")).lower()
        or "research-only" in str(a.get("risk", "")).lower()
        or "current-timeline" in str(a.get("risk", "")).lower()
    ]
    grounding_risk_findings = [
        f for f in (review.get("findings") or [])
        if isinstance(f, dict) and f.get("category") == "grounding_risk"
    ]
    return {
        "context_only_assertions": len(context_items or []) + len(research_context_assertions),
        "spoiler_risk_assertions": len(risk_assertions) + len(grounding_risk_findings),
        "policy": "research evidence is context_only unless visual/asr-supported",
    }

def _clip_text(text, limit):
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value[:limit]


def _load_review_research_context(work_dir):
    path = Path(work_dir) / "background_research.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _format_review_research_context(research, limit=1200):
    """Compact background_research.json for the quality reviewer.

    Background research is rendered as context-only/advisory: it can help aliases and
    disambiguation, but it is not timeline grounding for current visual/ASR facts.
    """
    if not isinstance(research, dict) or not research:
        return ""
    lines = []
    for key, label in (
        ("synopsis", "Synopsis"),
        ("episode_context", "Episode context"),
        ("worldbuilding", "Worldbuilding"),
    ):
        value = _clip_text(research.get(key), 500)
        if value:
            lines.append(f"- {label}: {value}")

    characters = research.get("characters")
    if isinstance(characters, dict) and characters:
        lines.append("- Characters:")
        for name, desc in list(characters.items())[:12]:
            clean_name = _clip_text(name, 60)
            clean_desc = _clip_text(desc, 160)
            if clean_name:
                lines.append(f"    - {clean_name}：{clean_desc}")

    details = research.get("character_details")
    if isinstance(details, dict) and details:
        lines.append("- Character details:")
        for name, info in list(details.items())[:8]:
            if not isinstance(info, dict):
                continue
            bits = []
            aliases = info.get("aliases")
            if isinstance(aliases, list) and aliases:
                bits.append("别名 " + "/".join(_clip_text(alias, 40) for alias in aliases[:4] if _clip_text(alias, 40)))
            role = _clip_text(info.get("role"), 80)
            if role:
                bits.append(role)
            rels = info.get("relationships")
            if isinstance(rels, list) and rels:
                bits.append("；".join(_clip_text(rel, 80) for rel in rels[:4] if _clip_text(rel, 80)))
            clean_name = _clip_text(name, 60)
            if clean_name and bits:
                lines.append(f"    - {clean_name}：{'；'.join(bits)}")

    arcs = research.get("plot_arcs")
    if isinstance(arcs, list) and arcs:
        lines.append("- Plot arcs:")
        for arc in arcs[:8]:
            if isinstance(arc, dict):
                name = _clip_text(arc.get("name"), 80)
                desc = _clip_text(arc.get("description"), 180)
                status = _clip_text(arc.get("status"), 40)
                if name or desc:
                    tail = f" [{status}]" if status else ""
                    lines.append(f"    - {name}：{desc}{tail}")
            else:
                val = _clip_text(arc, 180)
                if val:
                    lines.append(f"    - {val}")

    text = "\n".join(lines).strip()
    return text[:limit]


def _load_optional_json(work_dir, name):
    if work_dir is None:
        return None
    return _load(Path(work_dir), name)


def _format_json_context(title, value, limit=3000):
    if value is None:
        return f"## {title}\n(无)"
    try:
        text = json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        text = str(value)
    return f"## {title}\n{text[:limit]}"


def _clamp_score(value, default=3):
    try:
        score = int(round(float(value)))
    except (TypeError, ValueError):
        score = default
    return max(1, min(5, score))


def _normalise_scorecard(raw):
    # Keep a stable scorecard schema, but do NOT fabricate a judge-looking score for a dimension
    # the model omitted: those stay None ("未评分") rather than a neutral-looking 3.
    source = raw if isinstance(raw, dict) else {}
    return {key: (_clamp_score(source[key]) if key in source else None) for key in SCORECARD_KEYS}


def _normalise_list_of_dicts(value, allowed_keys):
    out = []
    for item in value or []:
        if isinstance(item, dict):
            out.append({key: item.get(key) for key in allowed_keys if key in item})
    return out


def _normalise_string_list(value, limit=12):
    out = []
    for item in value or []:
        text = str(item).strip()
        if text:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _format_draft(narration):
    lines = []
    for i, seg in enumerate(narration or []):
        if not isinstance(seg, dict):
            continue
        start = seg.get("start", 0)
        end = seg.get("end", 0)
        text = str(seg.get("narration", "")).strip()
        overlap = seg.get("overlaps_speech")
        tag = "" if overlap is None else (" [盖原声]" if overlap else " [静音槽]")
        lines.append(f"{i}. [{float(start):.1f}-{float(end):.1f}s]{tag} {text}")
    return "\n".join(lines)


def build_review_messages(narration, vlm_analysis, asr_result, work_dir=None, research_context=None, evidence_bundle=None):
    """Pure: assemble the reviewer chat messages (testable without the API)."""
    draft = _format_draft(narration)
    research_obj = _load_review_research_context(work_dir) if work_dir is not None else {}
    if research_context is None:
        research_context = _format_review_research_context(research_obj)
    bundle = evidence_bundle or build_evidence_bundle(
        vlm_analysis, asr_result, narration,
        timeline="cut_output" if any("source_start" in x for x in (vlm_analysis or []) if isinstance(x, dict)) else "source",
        research=research_obj)
    evidence_text = render_evidence_bundle(bundle)
    # Optional, agent-authored planning/QC artifacts. Bad JSON returns None via _load, matching
    # existing fail-open optional artifact behavior; these only sharpen advisory scoring.
    packaging = _load_optional_json(work_dir, "packaging_plan.json")
    story_plan = _load_optional_json(work_dir, "recap_story_plan.json")
    av_board = _load_optional_json(work_dir, "visual_audio_board.json")
    style_card = _load_optional_json(work_dir, "style_card.json")
    deslop_qc = _load_optional_json(work_dir, "deslop_qc.json")
    user = (
        f"{RUBRIC}\n\n"
        "## Scorecard 评估提示\n"
        "若 work_dir 提供了 packaging_plan/recap_story_plan/visual_audio_board/style_card/deslop_qc，则结合评估："
        "packaging_plan 只负责标题/封面/首句/卖点承诺与正文兑现；style_card 只负责表达意图、语气、节奏和禁忌；"
        "recap_story_plan 负责主线/beats；visual_audio_board 负责画面/原声/字幕/剪辑锚点；deslop_qc 是 deterministic local report-only QC，不是 AIGC detector，也不会自动重写，只能当证据参考。"
        "若未提供，则基于解说本身与画面/对白证据评分。统一评估：hook 是否有悬念/反差/高利害；每段是否有信息增量而非看图说话；"
        "结尾是否兑现开头承诺/主线情绪；是否写给耳朵听（短句、口语、TTS可呼吸）；人物/关系/因果断言是否有 visual/ASR timeline evidence；research/user_context 仅可作为 context-only 辅助。\n"
        "审美/风格/包装/去AI味项是 advisory：可 REVISE，但除事实矛盾/残句外不要给 error。\n\n"
        f"{_format_json_context('packaging_plan.json（标题/封面/首句/卖点包装承诺，可能为空）', packaging)}\n\n"
        f"{_format_json_context('recap_story_plan.json（主线/beats/original moments，可能为空）', story_plan)}\n\n"
        f"{_format_json_context('visual_audio_board.json（画面/原声/字幕/剪辑锚点，可能为空）', av_board)}\n\n"
        f"{_format_json_context('style_card.json（表达意图/语气/节奏/禁忌，不负责包装，可能为空）', style_card)}\n\n"
        f"{_format_json_context('deslop_qc.json（deterministic report-only QC；非AIGC检测器；不自动重写，可能为空）', deslop_qc)}\n\n"
        f"## 背景资料（context-only/advisory：只辅助识别/消歧/弱背景，不是当前画面强事实）\n"
        f"Guardrail: clock=null/context_only；不得把未来剧情或 research-only 关系/因果升级为当前事实。\n"
        f"{research_context or '(无)'}\n\n"
        f"{evidence_text}\n\n"
        f"## 解说草稿（共 {len([s for s in (narration or []) if isinstance(s, dict)])} 段）\n{draft or '(空)'}\n"
    )
    return [{"role": "user", "content": user}]


def _downgrade_context_assertions(assertions):
    out = []
    for item in assertions or []:
        src = str(item.get("source", "")).strip().lower()
        if src in {"research", "user_context"}:
            item = dict(item)
            item["support"] = "context_only"
            item["clock"] = None
            risk = str(item.get("risk", "")).strip()
            label = "research-only" if src == "research" else "user_context-only"
            item["risk"] = (risk + "; " if risk else "") + f"{label}: advisory/context_only, not a strong current-timeline fact"
        out.append(item)
    return out


def parse_review_response(text):
    """Pure: robustly extract the reviewer JSON; fall back to a REVISE-unknown shell."""
    raw = str(text or "")
    candidate = raw
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fence:
        candidate = fence.group(1)
    else:
        first, last = raw.find("{"), raw.rfind("}")
        if first != -1 and last > first:
            candidate = raw[first:last + 1]
    try:
        data = json.loads(candidate)
    except ValueError:
        return {"verdict": "REVISE", "summary": "评审输出无法解析为 JSON，请人工检查。",
                "findings": [], "parse_error": True, "raw": raw[:2000]}
    verdict = str(data.get("verdict", "REVISE")).upper()
    # PASS/REVISE/FAIL is the new vocabulary; OK is kept as a backward-compatible alias.
    if verdict not in ("PASS", "REVISE", "FAIL", "OK"):
        verdict = "REVISE"
    findings = []
    for f in data.get("findings", []) or []:
        if not isinstance(f, dict):
            continue
        sev = str(f.get("severity", "warning")).lower()
        if sev not in ("error", "warning", "suggestion"):
            sev = "warning"
        cat = str(f.get("category", "other")).lower()
        if cat not in CATEGORIES:
            cat = "other"
        # Only factual defects may gate strict mode (severity=error). Craft findings
        # (weak_hook, narrating_picture, cliche, disjoint_handoff, ...) are advisory:
        # clamp them to at most "warning" so they never block on subjective judgement.
        if cat not in FACTUAL_CATEGORIES and sev == "error":
            sev = "warning"
        findings.append({
            "segment": f.get("segment"),
            "severity": sev,
            "category": cat,
            "issue": str(f.get("issue", "")).strip(),
            "fix": str(f.get("fix", "")).strip(),
        })
    # The scorecard and the lists below are PURE ADVISORY enrichment: they never mutate the
    # judge's verdict (deliberately not the colleague's auto-downgrade) and never gate. The
    # hard pre-TTS gate stays exactly where it was — error findings counted in recap.py.
    return {
        "verdict": verdict,
        "summary": str(data.get("summary", "")).strip(),
        "scorecard": _normalise_scorecard(data.get("scorecard")),
        "hook_candidates_review": _normalise_list_of_dicts(
            data.get("hook_candidates_review"), ["candidate", "type", "score", "keep", "reason"]),
        "retention_risk_points": _normalise_list_of_dicts(
            data.get("retention_risk_points"), ["time", "risk", "fix", "evidence"]),
        "highest_return_edits": _normalise_string_list(data.get("highest_return_edits")),
        "information_gain_notes": _normalise_list_of_dicts(
            data.get("information_gain_notes"), ["segment", "label", "note", "rewrite"]),
        "spoken_language_rewrites": _normalise_list_of_dicts(
            data.get("spoken_language_rewrites"), ["segment", "original", "rewrite", "why"]),
        "grounding_assertions": _downgrade_context_assertions(_normalise_list_of_dicts(
            data.get("grounding_assertions"), ["segment", "assertion", "source", "risk", "support", "clock"])),
        "findings": findings,
    }


def format_review_md(review):
    order = {"error": 0, "warning": 1, "suggestion": 2}
    findings = sorted(review.get("findings", []), key=lambda f: order.get(f["severity"], 3))
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in ("error", "warning", "suggestion")}
    out = [
        "# Narration review",
        "",
        f"Verdict: **{review.get('verdict', 'REVISE')}**  "
        f"(errors {counts['error']}, warnings {counts['warning']}, suggestions {counts['suggestion']})",
        "",
        review.get("summary", "") or "_(no summary)_",
        "",
        "## Scorecard",
    ]
    scorecard = review.get("scorecard") or {}
    if scorecard:
        for key in SCORECARD_KEYS:
            v = scorecard.get(key)
            out.append(f"- {key}: {v}/5" if v is not None else f"- {key}: 未评分")
    else:
        out.append("- (none)")
    out.extend(["", "## Highest-return edits"])
    edits = review.get("highest_return_edits") or []
    out.extend([f"- {edit}" for edit in edits] or ["- (none)"])
    out.extend(["", "## Retention risk points"])
    risks = review.get("retention_risk_points") or []
    if risks:
        for item in risks:
            out.append(f"- {item.get('time', '?')}: {item.get('risk', '')} — {item.get('fix', '')}")
    else:
        out.append("- (none)")
    out.extend(["", "## Hook candidates review"])
    hooks = review.get("hook_candidates_review") or []
    if hooks:
        for item in hooks:
            out.append(f"- {item.get('type', '?')} {item.get('score', '-')}/5: {item.get('candidate', '')} ({'keep' if item.get('keep') else 'drop'}) {item.get('reason', '')}")
    else:
        out.append("- (none)")
    out.extend(["", "## Information gain / write-for-ear / grounding", "### Information gain"])
    notes = review.get("information_gain_notes") or []
    out.extend([f"- 段 {n.get('segment')}: {n.get('label')} — {n.get('note', '')} {n.get('rewrite', '')}" for n in notes] or ["- (none)"])
    out.append("### Spoken rewrites")
    rewrites = review.get("spoken_language_rewrites") or []
    out.extend([f"- 段 {r.get('segment')}: {r.get('original', '')} → {r.get('rewrite', '')}（{r.get('why', '')}）" for r in rewrites] or ["- (none)"])
    out.append("### Grounding assertions")
    assertions = review.get("grounding_assertions") or []
    out.extend([f"- 段 {a.get('segment')}: {a.get('assertion', '')} [{a.get('source', '')}] {a.get('risk', '')}" for a in assertions] or ["- (none)"])
    out.extend(["", "## Findings"])
    if not findings:
        out.append("- (none)")
    for f in findings:
        seg = "整体" if f["segment"] is None else f"段 {f['segment']}"
        out.append(f"- **[{f['severity']}/{f['category']}] {seg}** — {f['issue']}")
        if f["fix"]:
            out.append(f"  - 改法: {f['fix']}")
    return "\n".join(out) + "\n"


def build_grounding_qc(work_dir, review, bundle, *, timeline="source"):
    """Pure-ish compatibility seam: build grounding QC payload without writing it.

    It reads optional source fingerprints/QC from work_dir to preserve the existing artifact
    contract, but has no side effects.
    """
    work_dir = Path(work_dir)
    items = bundle.get("items") or []
    context = bundle.get("context_items") or []
    warnings = list(bundle.get("warnings") or []) + list(review.get("warnings") or [])
    verdict = "warn" if warnings else "pass"
    if any(f.get("severity") == "error" for f in (review.get("findings") or []) if isinstance(f, dict)):
        verdict = "fail"
    if any(item.get("clock") not in ("source", "output") for item in items):
        verdict = "warn" if verdict == "pass" else verdict
    coverage_meta = build_review_coverage_metadata(bundle)
    return {
        "schema_version": 1,
        "owner": "video-script.review",
        "timeline": timeline,
        "coverage_policy_version": COVERAGE_POLICY_VERSION,
        "source_fingerprints": {
            "vlm": _source_fingerprint(work_dir, "vlm_analysis.json"),
            "asr": _source_fingerprint(work_dir, "asr_result.json"),
            "research": _source_fingerprint(work_dir, "background_research.json"),
            "clip_plan": _source_fingerprint(work_dir, "clip_plan_validated.json"),
        },
        "review_coverage": {
            "time_ranges": coverage_meta["time_ranges"],
            "scene_count": coverage_meta["scene_count"],
            "asr_count": coverage_meta["asr_count"],
            "dropped_ranges": coverage_meta["dropped_ranges"],
        },
        "evidence_contract": {
            "source_items": sum(1 for item in items if item.get("clock") == "source"),
            "output_items": sum(1 for item in items if item.get("clock") == "output"),
            "unclocked_items": sum(1 for item in items if item.get("clock") not in ("source", "output")),
            "context_only_items": len(context),
            "validation": validate_public_evidence_contract(bundle),
        },
        "index_inputs": {
            "vlm": bool(_source_fingerprint(work_dir, "vlm_analysis.json")),
            "asr": bool(_source_fingerprint(work_dir, "asr_result.json")),
            "research": bool(_source_fingerprint(work_dir, "background_research.json")),
        },
        "speech_window_qc": _load_optional_json(work_dir, "silence_periods.qc.json") or {"coarse_asr_windows": 0, "low_confidence_speech_flags": 0},
        "research_guardrail": _research_guardrail_qc(review, context),
        "warnings": warnings,
        "verdict": verdict,
    }


def write_grounding_qc(work_dir, qc):
    """Compatibility seam: write a prebuilt grounding_qc.json payload."""
    work_dir = Path(work_dir)
    (work_dir / "grounding_qc.json").write_text(json.dumps(qc, ensure_ascii=False, indent=2), encoding="utf-8")
    return qc


def _write_grounding_qc(work_dir, review, bundle, *, timeline="source"):
    qc = build_grounding_qc(work_dir, review, bundle, timeline=timeline)
    return write_grounding_qc(work_dir, qc)


def review_narration(work_dir, *, timeline="source", strict_evidence=False):
    work_dir = Path(work_dir)
    narration = _load(work_dir, "narration.json")
    if narration is None:
        raise SystemExit(f"缺少 {work_dir / 'narration.json'}；先写解说草稿再评审")
    vlm_analysis = _load(work_dir, "vlm_analysis.json") or []
    asr_result = _load(work_dir, "asr_result.json") or []
    warnings = []
    if timeline == "cut_output":
        spans = _load_cut_clip_spans(work_dir)
        if not spans:
            msg = "cut_output review missing/stale clip_plan_validated.json; advisory fail-open, no strong OUTPUT-clock facts"
            if strict_evidence:
                raise SystemExit(msg)
            warnings.append(msg)
            vlm_analysis, asr_result = [], []
        else:
            vlm_analysis, asr_result = remap_grounding_to_output_timeline(vlm_analysis, asr_result, spans)
    elif timeline != "source":
        raise SystemExit(f"unknown review timeline: {timeline}")

    bundle = build_evidence_bundle(vlm_analysis, asr_result, narration, timeline=timeline,
                                   research=_load_review_research_context(work_dir), warnings=warnings)
    bundle_fp = _bundle_fingerprint(bundle)
    fp_warning = _bundle_fingerprint_warning(bundle)
    if fp_warning:
        _append_warning_once(warnings, fp_warning)
        _append_warning_once(bundle.setdefault("warnings", []), fp_warning)
    chunk_reviews = []
    chunks = _chunk_evidence_bundle(bundle)
    for chunk in chunks:
        messages = build_review_messages(narration, vlm_analysis, asr_result, work_dir=work_dir, evidence_bundle=chunk)
        resp = api_call({
            "model": CONFIG.get("vlm_model", ""),
            "messages": messages,
            "max_tokens": 2000 if len(chunks) == 1 else 1600,
            "temperature": 0,
            "seed": 7 + int(chunk.get("chunk_index", 0)),
        })
        content = ""
        try:
            content = resp["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            log("评审 API 返回结构异常")
        parsed = parse_review_response(content)
        parsed["chunk_index"] = chunk.get("chunk_index", 0)
        parsed["chunk_count"] = chunk.get("chunk_count", len(chunks))
        chunk_reviews.append(parsed)
    review = _merge_chunk_reviews(chunk_reviews)
    if warnings:
        review["warnings"] = list(warnings)
    review["evidence_contract"] = {
        "schema_version": EVIDENCE_CONTRACT_VERSION,
        "timeline": timeline,
        "clock": bundle.get("clock"),
        "coverage_policy_version": COVERAGE_POLICY_VERSION,
        "selected_ranges": bundle.get("coverage", {}).get("selected_ranges", []),
        "evidence_bundle_fingerprint": bundle_fp,
        "chunk_count": len(chunks),
        "warnings": warnings,
    }
    _write_grounding_qc(work_dir, review, bundle, timeline=timeline)

    (work_dir / "narration_review.json").write_text(
        json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
    (work_dir / "narration_review.md").write_text(format_review_md(review), encoding="utf-8")
    n_err = sum(1 for f in review["findings"] if f["severity"] == "error")
    log(f"解说评审完成: {review['verdict']} | {len(review['findings'])} 条意见（error {n_err}）")
    return review


def _auto_timeline(work_dir):
    """Default the grounding timeline so a manual `review.py --work-dir` matches what the
    orchestrator does: cut_output when narration.json is in the cut OUTPUT timeline, else
    source. Without this, reviewing a cut narration on the default 'source' timeline compares
    OUTPUT-time narration against SOURCE-time evidence and floods false-positive 'hallucination'
    findings (and the inverse flood for a legacy source-time narration mis-read as cut_output).

    Detection is authoritative-first: the orchestrator records the run's edit_mode in
    recap_run_manifest.json. In orchestrated cut mode narration.json is OUTPUT time; in full
    mode it is SOURCE time. Trusting edit_mode is correct even when stale cut artifacts from a
    prior run linger in a reused work_dir. Only when no manifest is present (standalone review
    or a hand-built work_dir) do we fall back to artifact sniffing — and even then the legacy
    direct video-cut single-pass path writes a SOURCE-time narration.json alongside a separate
    output-time narration_mapped.json, so its presence pins us back to source."""
    work_dir = Path(work_dir)
    manifest = work_dir / "recap_run_manifest.json"
    if manifest.exists():
        try:
            mode = json.loads(manifest.read_text(encoding="utf-8")).get("settings", {}).get("edit_mode")
        except (ValueError, OSError):
            mode = None
        if mode == "cut":
            return "cut_output"
        if mode:  # "full" or any non-cut mode → narration.json is source time
            return "source"
    has_cut = (work_dir / "clip_plan_validated.json").exists() and (work_dir / "edited_source.mp4").exists()
    if has_cut and not (work_dir / "narration_mapped.json").exists():
        return "cut_output"
    return "source"


def main():
    ap = argparse.ArgumentParser(description="Review an agent-written narration.json for quality (LLM-as-judge).")
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--timeline", choices=["source", "cut_output"], default=None,
                    help="grounding timeline for narration.json; DEFAULT auto-detects cut_output when a "
                         "validated cut (clip_plan_validated.json + edited_source.mp4) is present, else source. "
                         "cut_output remaps source VLM/ASR to the cut output timeline via clip_plan_validated.json")
    ap.add_argument("--strict-evidence", action="store_true",
                    help="block instead of advisory fail-open when required cut-output evidence mapping is missing/stale")
    args = ap.parse_args()
    timeline = args.timeline or _auto_timeline(args.work_dir)
    if args.timeline is None and timeline != "source":
        log(f"评审 grounding 时间轴自动判定为 {timeline}（检测到已校验的剪辑产物）")
    review = review_narration(args.work_dir, timeline=timeline, strict_evidence=args.strict_evidence)
    print(json.dumps({
        "status": "reviewed",
        "verdict": review["verdict"],
        "findings": len(review["findings"]),
        "review": str(Path(args.work_dir) / "narration_review.md"),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
