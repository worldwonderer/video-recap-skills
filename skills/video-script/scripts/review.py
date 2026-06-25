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
    "grounding_risk", "original_audio_conflict", "subtitle_readability", "other",
]

SCORECARD_KEYS = [
    "promise_match", "hook_3s", "first_15s_delivery", "spine_clarity",
    "stakes_escalation", "information_gain", "spoken_language",
    "sentence_brevity", "tts_pacing", "grounding", "original_audio_use",
    "subtitle_readability",
]

# Categories whose findings are allowed to keep severity=error and thus gate strict
# mode. Everything else is craft/subjective and is clamped to at most "warning".
FACTUAL_CATEGORIES = {"hallucination", "incomplete"}

RUBRIC = """你是中文视频解说稿的严格评审。依据以下规则审阅草稿，只指出真实问题，宁缺毋滥：
1. 反幻觉（最重要）：解说里的人物、动作、因果、关系只要能由「画面证据」(frame_facts/描述) 或「对白」(ASR) 或「背景资料」(background_research) 任一支撑，即不算幻觉。只有与全部可得证据都矛盾的论断才是 severity=error, category=hallucination，并指出与哪条证据冲突。若与背景资料一致但画面/对白里看不到（合理推断、非矛盾），最多 severity=suggestion，不要判 error。
2. 钩子：开头 1-2 段要制造悬念/利害，不是交代场景。弱钩子 → weak_hook。
3. 主线：应有一条贯穿主线（目标/关系/悬念），每段推进它，不要每个场景从头讲。缺主线 → no_throughline。
4. 给信息而非念画面：观众看得见动作表情；解说要讲动机/关系/潜台词/剧情意义。复述画面 → narrating_picture。
5. 密度/节奏：连续铺底、短句、相邻段不要断太久；过疏/过密/拖沓 → density 或 pacing。
6. 去废词：删空泛形容（"危机四伏""震撼人心"）→ cliche。
7. 完整句子：半句话/未收尾 → incomplete。
8. 段落衔接：解说块要为随后的原声留白铺垫，下一块要承接原声刚呈现的内容；若两块各说各的、原声进来接不上 → disjoint_handoff。
另外给一份内容效果 scorecard（1-5，advisory：除事实矛盾/残句外不要据此给 error）：promise_match/hook_3s/first_15s_delivery/spine_clarity/stakes_escalation/information_gain/spoken_language/sentence_brevity/tts_pacing/grounding/original_audio_use/subtitle_readability。
只返回 JSON（不要额外解释），格式：
{"verdict":"PASS|REVISE|FAIL","summary":"一两句总体判断","scorecard":{"promise_match":1-5,"hook_3s":1-5,"first_15s_delivery":1-5,"spine_clarity":1-5,"stakes_escalation":1-5,"information_gain":1-5,"spoken_language":1-5,"sentence_brevity":1-5,"tts_pacing":1-5,"grounding":1-5,"original_audio_use":1-5,"subtitle_readability":1-5},"hook_candidates_review":[{"candidate":"首句","type":"suspense|contrast|stakes","score":1-5,"keep":true}],"retention_risk_points":[{"time":"00:28","risk":"为什么可能掉人","fix":"怎么改"}],"highest_return_edits":["最值得改的动作"],"information_gain_notes":[{"segment":0,"label":"motive|relationship|stakes|foreshadowing|payoff|context|visual_restatement","note":"证据/改法"}],"spoken_language_rewrites":[{"segment":0,"original":"原句","rewrite":"口语改写","why":"为什么更适合听"}],"grounding_assertions":[{"segment":0,"assertion":"人物/关系/因果断言","source":"visual|asr|research|user_context|unsupported","risk":"谨慎说明"}],"findings":[{"segment":<草稿段号(从0起)或null表示整体>,"severity":"error|warning|suggestion","category":"<上面类别之一>","issue":"问题","fix":"具体改法"}]}"""



def _load(work_dir, name):
    path = Path(work_dir) / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None




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
        })
    return overlaps


def _map_source_range_to_output(start, end, spans):
    return [(o["output_start"], o["output_end"]) for o in _source_output_overlaps(start, end, spans)]


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
            remapped_asr.append(item)

    remapped_scenes.sort(key=lambda x: (float(x.get("start", 0)), float(x.get("end", 0))))
    remapped_asr.sort(key=lambda x: (float(x.get("start", 0)), float(x.get("end", 0))))
    return remapped_scenes, remapped_asr


def _scene_grounding(vlm_analysis, limit=60):
    lines = []
    for scene in (vlm_analysis or [])[:limit]:
        if not isinstance(scene, dict):
            continue
        sid = scene.get("scene_id", "?")
        start = scene.get("start", 0)
        end = scene.get("end", 0)
        desc = str(scene.get("description", "")).strip().replace("\n", " ")
        facts = scene.get("frame_facts")
        fact_txt = ""
        picks = []
        if isinstance(facts, dict):  # canonical shape: {"<ts>": ["action", ...]} (vlm.py)
            def fact_sort_key(value):
                try:
                    return (0, float(value))
                except (TypeError, ValueError):
                    return (1, str(value))

            for ts in sorted(facts.keys(), key=fact_sort_key):
                vals = facts[ts]
                picks.extend(vals if isinstance(vals, list) else [str(vals)])
        elif isinstance(facts, list):  # defensive: legacy list shape
            for f in facts:
                picks.append(str(f.get("fact", f.get("text", ""))).strip() if isinstance(f, dict) else str(f).strip())
        if picks:
            fact_txt = " | 帧实: " + "；".join(p for p in picks[:4] if p)
        lines.append(f"[场景{sid} {float(start):.0f}-{float(end):.0f}s] {desc}{fact_txt}")
    return "\n".join(lines)


def _asr_grounding(asr_result, limit=80):
    lines = []
    for seg in (asr_result or [])[:limit]:
        if not isinstance(seg, dict):
            continue
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        lines.append(f"[{float(seg.get('start', 0)):.0f}-{float(seg.get('end', 0)):.0f}s] {text}")
    return "\n".join(lines)


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

    Background research is valid grounding alongside visual/ASR: a claim it supports is
    not a hallucination; only claims contradicting all available evidence are errors.
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
    # Keep a stable 12-key schema, but do NOT fabricate a judge-looking score for a dimension
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


def build_review_messages(narration, vlm_analysis, asr_result, work_dir=None, research_context=None):
    """Pure: assemble the reviewer chat messages (testable without the API)."""
    grounding = _scene_grounding(vlm_analysis)
    dialogue = _asr_grounding(asr_result)
    draft = _format_draft(narration)
    if research_context is None and work_dir is not None:
        research_context = _format_review_research_context(_load_review_research_context(work_dir))
    # Optional, agent-authored planning artifacts. Absent in most runs today — review then
    # scores from the narration + grounding alone; present (future R1 pilot) they sharpen
    # promise/spine/anchor scoring. Either way the review degrades gracefully.
    packaging = _load_optional_json(work_dir, "packaging_plan.json")
    story_plan = _load_optional_json(work_dir, "recap_story_plan.json")
    av_board = _load_optional_json(work_dir, "visual_audio_board.json")
    user = (
        f"{RUBRIC}\n\n"
        "## Scorecard 评估提示\n"
        "若 work_dir 提供了 packaging_plan/recap_story_plan/visual_audio_board，则结合评估：标题/封面/首句承诺是否一致并在前15秒兑现、主线是否清晰、关键原声是否保留；"
        "若未提供，则基于解说本身与画面/对白证据评分。统一评估：hook 是否有悬念/反差/高利害；每段是否有信息增量而非看图说话；"
        "是否写给耳朵听（短句、口语、TTS可呼吸）；人物/关系/因果断言是否有 visual/ASR/research/user_context 证据。\n"
        "审美项是 advisory：可 REVISE，但除事实矛盾/残句外不要给 error。\n\n"
        f"{_format_json_context('packaging_plan.json（点击承诺/标题/封面/首句，可能为空）', packaging)}\n\n"
        f"{_format_json_context('recap_story_plan.json（主线/beats/original moments，可能为空）', story_plan)}\n\n"
        f"{_format_json_context('visual_audio_board.json（画面/原声/字幕/剪辑锚点，可能为空）', av_board)}\n\n"
        f"## 背景资料（与画面/对白并列的有效依据：被其支撑的事实不算幻觉，仅与全部证据矛盾才算）\n{research_context or '(无)'}\n\n"
        f"## 画面证据（场景描述 + 帧实）\n{grounding or '(无)'}\n\n"
        f"## 对白（ASR）\n{dialogue or '(无对白/静音视频)'}\n\n"
        f"## 解说草稿（共 {len([s for s in (narration or []) if isinstance(s, dict)])} 段）\n{draft or '(空)'}\n"
    )
    return [{"role": "user", "content": user}]


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
        "grounding_assertions": _normalise_list_of_dicts(
            data.get("grounding_assertions"), ["segment", "assertion", "source", "risk"]),
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


def review_narration(work_dir, *, timeline="source"):
    work_dir = Path(work_dir)
    narration = _load(work_dir, "narration.json")
    if narration is None:
        raise SystemExit(f"缺少 {work_dir / 'narration.json'}；先写解说草稿再评审")
    vlm_analysis = _load(work_dir, "vlm_analysis.json") or []
    asr_result = _load(work_dir, "asr_result.json") or []
    if timeline == "cut_output":
        spans = _load_cut_clip_spans(work_dir)
        if not spans:
            raise SystemExit("cut_output review requires fresh clip_plan_validated.json with explicit source/output spans")
        vlm_analysis, asr_result = remap_grounding_to_output_timeline(vlm_analysis, asr_result, spans)
    elif timeline != "source":
        raise SystemExit(f"unknown review timeline: {timeline}")

    messages = build_review_messages(narration, vlm_analysis, asr_result, work_dir=work_dir)
    resp = api_call({
        "model": CONFIG.get("vlm_model", ""),
        "messages": messages,
        "max_tokens": 2000,
        "temperature": 0,
        "seed": 7,
    })
    content = ""
    try:
        content = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        log("评审 API 返回结构异常")
    review = parse_review_response(content)

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
    args = ap.parse_args()
    timeline = args.timeline or _auto_timeline(args.work_dir)
    if args.timeline is None and timeline != "source":
        log(f"评审 grounding 时间轴自动判定为 {timeline}（检测到已校验的剪辑产物）")
    review = review_narration(args.work_dir, timeline=timeline)
    print(json.dumps({
        "status": "reviewed",
        "verdict": review["verdict"],
        "findings": len(review["findings"]),
        "review": str(Path(args.work_dir) / "narration_review.md"),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
