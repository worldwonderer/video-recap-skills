"""Build review prompts and normalize model review responses."""

import json


import re

from pathlib import Path

from lib import stable_hash

from evidence_bundle import (
    _clip_text,
    _in_ranges,
    _safe_time,
    build_evidence_bundle,
    build_review_coverage_metadata,
    render_evidence_bundle,
    validate_public_evidence_contract,
)
from review_grounding import _load, _source_fingerprint

CATEGORIES = [
    "hallucination",
    "weak_hook",
    "no_throughline",
    "narrating_picture",
    "density",
    "pacing",
    "cliche",
    "incomplete",
    "disjoint_handoff",
    "promise_mismatch",
    "low_information_gain",
    "not_write_for_ear",
    "grounding_risk",
    "original_audio_conflict",
    "subtitle_readability",
    "ai_flavor",
    "weak_payoff",
    "style_mismatch",
    "packaging_mismatch",
    "example_entity_leak",
    "other",
]

SCORECARD_KEYS = [
    "promise_match",
    "hook_3s",
    "first_15s_delivery",
    "spine_clarity",
    "stakes_escalation",
    "information_gain",
    "spoken_language",
    "sentence_brevity",
    "tts_pacing",
    "grounding",
    "original_audio_use",
    "subtitle_readability",
    "ending_payoff",
    "style_consistency",
    "ai_flavor",
    "packaging_consistency",
]

FACTUAL_CATEGORIES = {"hallucination", "incomplete"}

COVERAGE_POLICY_VERSION = "coverage_policy_v1"

RUBRIC = """你是中文视频解说的创作复核编辑。依据素材证据和已有创作计划审阅草稿，只指出真实问题，宁缺毋滥：
1. 反幻觉（最重要）：解说里的人物、动作、因果、关系必须由带标签的 evidence 支撑。画面/对白是 timeline evidence（clock=SOURCE 或 OUTPUT）；背景资料/user_context 只能作为 context-only（clock=null）辅助识别/消歧。research-only 不能升级成当前画面强事实；若与 research 一致但画面/对白里看不到，最多 severity=suggestion/category=grounding_risk，不要判 error；只有与全部可得证据矛盾才是 severity=error, category=hallucination，并指出冲突证据。
2. 导演意图：若提供 recap_story_plan.json，检查草稿是否兑现 viewer promise、POV、dramatic question、情绪路径和 chosen_hypothesis；不要另起一条更“吸睛”但不属于该计划的故事。偏离主线 → no_throughline；承诺不兑现 → promise_mismatch/weak_payoff。
3. change-based beats：每个 beat 应改变知识、权力、目标、关系、情绪或风险。若一段只重复上一段、删除后什么都不损失，可报 low_information_gain/pacing；不要用固定段数或秒数代替判断。
4. 钩子：开头要提出正文真实兑现的戏剧问题/利害，不是交代场景，也不是无关的留存话术。弱钩子 → weak_hook。
5. 给信息而非念画面：观众看得见动作表情；解说只增加上下文、因果、预期、证据支持的解释或跨越。复述画面 → narrating_picture。
6. 视听分工：若提供 visual_audio_board.json，检查 narration_job=none 或 audio_owner=original_dialogue/action_sound/ambience/music/silence 的拍是否被旁白无故覆盖；必须听见的原声被盖住 → original_audio_conflict。沉默和低旁白覆盖本身不是问题。
7. 人物与反应：不要用旁白解释掉素材中已经能成立的表演、停顿或反应。当前评审只能检查计划/稿件一致性，不能凭少量帧声称最终剪点一定好坏。
8. 密度/节奏：7:3 不是配额。只在旁白没有任务、墙到墙压住原声、碎成一句一停，或无意长空档导致因果断裂时，报 density/pacing。
9. 去废词：删空泛形容（"危机四伏""震撼人心"）→ cliche。
10. 完整句子：半句话/未收尾 → incomplete。
11. 段落衔接：解说块要为随后的原声留白铺垫，下一块要承接原声刚呈现的变化；若两块各说各的、原声进来接不上 → disjoint_handoff。
12. 结尾回收：结尾要兑现开头承诺/主线情绪，不要突然停、只复述最后画面、没有情绪/信息回报；弱回收 → weak_payoff。
13. 风格一致性：若提供 style_card.json，把它当作表达意图/语气/节奏边界；不符合意图 → style_mismatch。不要把 style_card 当标题/封面/首句包装计划。
14. 包装一致性：只有提供 packaging_plan.json 时才评估标题/封面/首句/卖点承诺与正文兑现；缺失不扣分。不一致 → packaging_mismatch。不要让包装反过来改写故事判断。
15. 去AI味：若出现模板化、空泛拔高、过度对仗、机械转折、明显 agent 示例残留，可报 ai_flavor；若出现示例人物/占位实体泄漏（如未替换示例名、模板角色）→ example_entity_leak。deslop_qc.json 是 deterministic local report-only QC，不是 AIGC detector，不自动重写；只能作为证据参考，不能仅凭它判定。
另外给一份内容效果 scorecard（1-5，advisory：除事实矛盾/残句外不要据此给 error；缺项可以省略，系统会保留为 null/未评分）：promise_match/hook_3s/first_15s_delivery/spine_clarity/stakes_escalation/information_gain/spoken_language/sentence_brevity/tts_pacing/grounding/original_audio_use/subtitle_readability/ending_payoff/style_consistency/ai_flavor/packaging_consistency。ai_flavor 分数含义：5=自然、人味强，1=AI味明显。
只返回 JSON（不要额外解释），格式：
{"verdict":"PASS|REVISE|FAIL","summary":"一两句总体判断","scorecard":{"promise_match":1-5,"hook_3s":1-5,"first_15s_delivery":1-5,"spine_clarity":1-5,"stakes_escalation":1-5,"information_gain":1-5,"spoken_language":1-5,"sentence_brevity":1-5,"tts_pacing":1-5,"grounding":1-5,"original_audio_use":1-5,"subtitle_readability":1-5,"ending_payoff":1-5,"style_consistency":1-5,"ai_flavor":1-5,"packaging_consistency":1-5},"hook_candidates_review":[{"candidate":"首句","type":"suspense|contrast|stakes","score":1-5,"keep":true}],"retention_risk_points":[{"time":"00:28","risk":"为什么可能掉人","fix":"怎么改"}],"highest_return_edits":["最值得改的动作"],"information_gain_notes":[{"segment":0,"label":"motive|relationship|stakes|foreshadowing|payoff|context|visual_restatement","note":"证据/改法"}],"spoken_language_rewrites":[{"segment":0,"original":"原句","rewrite":"口语改写","why":"为什么更适合听"}],"grounding_assertions":[{"segment":0,"assertion":"人物/关系/因果断言","source":"visual|asr|research|user_context|unsupported","risk":"谨慎说明"}],"findings":[{"segment":<草稿段号(从0起)或null表示整体>,"severity":"error|warning|suggestion","category":"<上面类别之一>","issue":"问题","fix":"具体改法"}]}"""


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
            if old is None or severity_rank.get(
                f.get("severity"), 0
            ) > severity_rank.get(old.get("severity"), 0):
                by_key[key] = dict(f)
    return sorted(
        by_key.values(),
        key=lambda f: (
            f.get("segment") is None,
            f.get("segment") if f.get("segment") is not None else 10**9,
            f.get("category") or "",
            f.get("issue") or "",
        ),
    )


def _bundle_fingerprint(bundle):
    try:
        return stable_hash(
            {
                "schema_version": bundle.get("schema_version"),
                "clock": bundle.get("clock"),
                "coverage": bundle.get("coverage"),
                "items": bundle.get("items"),
                "context_items": bundle.get("context_items"),
            }
        )
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
        r_items = [
            item
            for item in items
            if _in_ranges(
                _safe_time(item, "start"),
                _safe_time(item, "end", _safe_time(item, "start")),
                [r],
            )
        ]
        if not r_items:
            continue
        for start in range(0, len(r_items), max_items):
            part = r_items[start : start + max_items]
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
        chunk["items"] = leftovers[start : start + max_items]
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
    summaries = [
        str(r.get("summary", "")).strip()
        for r in chunk_reviews
        if str(r.get("summary", "")).strip()
    ]
    merged["summary"] = summaries[0] if summaries else merged.get("summary", "")
    merged["chunked_review"] = {
        "chunk_count": len(chunk_reviews),
        "findings_before_merge": sum(
            len(r.get("findings") or []) for r in chunk_reviews
        ),
        "findings_after_merge": len(merged.get("findings") or []),
    }
    return merged


def _research_guardrail_qc(review, context_items):
    assertions = [
        a for a in (review.get("grounding_assertions") or []) if isinstance(a, dict)
    ]
    research_context_assertions = [
        a
        for a in assertions
        if str(a.get("source", "")).lower() == "research"
        or a.get("support") == "context_only"
        or a.get("clock") is None
    ]
    risk_assertions = [
        a
        for a in research_context_assertions
        if "spoiler" in str(a.get("risk", "")).lower()
        or "research-only" in str(a.get("risk", "")).lower()
        or "current-timeline" in str(a.get("risk", "")).lower()
    ]
    grounding_risk_findings = [
        f
        for f in (review.get("findings") or [])
        if isinstance(f, dict) and f.get("category") == "grounding_risk"
    ]
    return {
        "context_only_assertions": len(context_items or [])
        + len(research_context_assertions),
        "spoiler_risk_assertions": len(risk_assertions) + len(grounding_risk_findings),
        "policy": "research evidence is context_only unless visual/asr-supported",
    }


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
                bits.append(
                    "别名 "
                    + "/".join(
                        _clip_text(alias, 40)
                        for alias in aliases[:4]
                        if _clip_text(alias, 40)
                    )
                )
            role = _clip_text(info.get("role"), 80)
            if role:
                bits.append(role)
            rels = info.get("relationships")
            if isinstance(rels, list) and rels:
                bits.append(
                    "；".join(
                        _clip_text(rel, 80) for rel in rels[:4] if _clip_text(rel, 80)
                    )
                )
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
    return {
        key: (_clamp_score(source[key]) if key in source else None)
        for key in SCORECARD_KEYS
    }


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


def build_review_messages(
    narration,
    vlm_analysis,
    asr_result,
    work_dir=None,
    research_context=None,
    evidence_bundle=None,
):
    """Pure: assemble the reviewer chat messages (testable without the API)."""
    draft = _format_draft(narration)
    research_obj = (
        _load_review_research_context(work_dir) if work_dir is not None else {}
    )
    if research_context is None:
        research_context = _format_review_research_context(research_obj)
    bundle = evidence_bundle or build_evidence_bundle(
        vlm_analysis,
        asr_result,
        narration,
        timeline="cut_output"
        if any("source_start" in x for x in (vlm_analysis or []) if isinstance(x, dict))
        else "source",
        research=research_obj,
    )
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
        "recap_story_plan 是导演意图/备选假设/chosen POV/change-based beats 的基线；visual_audio_board 是画面/表演/原声/audio_owner/narration_job/剪辑锚点的基线；deslop_qc 是 deterministic local report-only QC，不是 AIGC detector，也不会自动重写，只能当证据参考。"
        "若未提供，则基于解说本身与画面/对白证据评分，但不得因计划文件缺失给 error。统一评估：hook 是否真实兑现；每段是否产生变化/信息增量而非看图说话；"
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
            item["risk"] = (
                risk + "; " if risk else ""
            ) + f"{label}: advisory/context_only, not a strong current-timeline fact"
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
            candidate = raw[first : last + 1]
    try:
        data = json.loads(candidate)
    except ValueError:
        return {
            "verdict": "REVISE",
            "summary": "评审输出无法解析为 JSON，请人工检查。",
            "findings": [],
            "parse_error": True,
            "raw": raw[:2000],
        }
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
        findings.append(
            {
                "segment": f.get("segment"),
                "severity": sev,
                "category": cat,
                "issue": str(f.get("issue", "")).strip(),
                "fix": str(f.get("fix", "")).strip(),
            }
        )
    # The scorecard and the lists below are PURE ADVISORY enrichment: they never mutate the
    # judge's verdict (deliberately not the colleague's auto-downgrade) and never gate. The
    # hard pre-TTS gate stays exactly where it was — error findings counted in recap.py.
    return {
        "verdict": verdict,
        "summary": str(data.get("summary", "")).strip(),
        "scorecard": _normalise_scorecard(data.get("scorecard")),
        "hook_candidates_review": _normalise_list_of_dicts(
            data.get("hook_candidates_review"),
            ["candidate", "type", "score", "keep", "reason"],
        ),
        "retention_risk_points": _normalise_list_of_dicts(
            data.get("retention_risk_points"), ["time", "risk", "fix", "evidence"]
        ),
        "highest_return_edits": _normalise_string_list(
            data.get("highest_return_edits")
        ),
        "information_gain_notes": _normalise_list_of_dicts(
            data.get("information_gain_notes"), ["segment", "label", "note", "rewrite"]
        ),
        "spoken_language_rewrites": _normalise_list_of_dicts(
            data.get("spoken_language_rewrites"),
            ["segment", "original", "rewrite", "why"],
        ),
        "grounding_assertions": _downgrade_context_assertions(
            _normalise_list_of_dicts(
                data.get("grounding_assertions"),
                ["segment", "assertion", "source", "risk", "support", "clock"],
            )
        ),
        "findings": findings,
    }


def format_review_md(review):
    order = {"error": 0, "warning": 1, "suggestion": 2}
    findings = sorted(
        review.get("findings", []), key=lambda f: order.get(f["severity"], 3)
    )
    counts = {
        s: sum(1 for f in findings if f["severity"] == s)
        for s in ("error", "warning", "suggestion")
    }
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
            out.append(
                f"- {item.get('time', '?')}: {item.get('risk', '')} — {item.get('fix', '')}"
            )
    else:
        out.append("- (none)")
    out.extend(["", "## Hook candidates review"])
    hooks = review.get("hook_candidates_review") or []
    if hooks:
        for item in hooks:
            out.append(
                f"- {item.get('type', '?')} {item.get('score', '-')}/5: {item.get('candidate', '')} ({'keep' if item.get('keep') else 'drop'}) {item.get('reason', '')}"
            )
    else:
        out.append("- (none)")
    out.extend(
        ["", "## Information gain / write-for-ear / grounding", "### Information gain"]
    )
    notes = review.get("information_gain_notes") or []
    out.extend(
        [
            f"- 段 {n.get('segment')}: {n.get('label')} — {n.get('note', '')} {n.get('rewrite', '')}"
            for n in notes
        ]
        or ["- (none)"]
    )
    out.append("### Spoken rewrites")
    rewrites = review.get("spoken_language_rewrites") or []
    out.extend(
        [
            f"- 段 {r.get('segment')}: {r.get('original', '')} → {r.get('rewrite', '')}（{r.get('why', '')}）"
            for r in rewrites
        ]
        or ["- (none)"]
    )
    out.append("### Grounding assertions")
    assertions = review.get("grounding_assertions") or []
    out.extend(
        [
            f"- 段 {a.get('segment')}: {a.get('assertion', '')} [{a.get('source', '')}] {a.get('risk', '')}"
            for a in assertions
        ]
        or ["- (none)"]
    )
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
    if any(
        f.get("severity") == "error"
        for f in (review.get("findings") or [])
        if isinstance(f, dict)
    ):
        verdict = "fail"
    if any(item.get("clock") not in ("source", "output") for item in items):
        verdict = "warn" if verdict == "pass" else verdict
    coverage_meta = build_review_coverage_metadata(bundle)
    visual_items = [item for item in items if item.get("source") == "visual"]
    asr_items = [item for item in items if item.get("source") == "asr"]
    visual_fp = _source_fingerprint(work_dir, "vlm_analysis.json") or (
        stable_hash(visual_items) if visual_items else ""
    )
    asr_fp = _source_fingerprint(work_dir, "asr_result.json") or (
        stable_hash(asr_items) if asr_items else ""
    )
    return {
        "schema_version": 1,
        "owner": "video-script.review",
        "timeline": timeline,
        "coverage_policy_version": COVERAGE_POLICY_VERSION,
        "source_fingerprints": {
            "vlm": visual_fp,
            "asr": asr_fp,
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
            "unclocked_items": sum(
                1 for item in items if item.get("clock") not in ("source", "output")
            ),
            "context_only_items": len(context),
            "validation": validate_public_evidence_contract(bundle),
        },
        "index_inputs": {
            "vlm": bool(visual_items),
            "asr": bool(asr_items),
            "research": bool(_source_fingerprint(work_dir, "background_research.json")),
        },
        "speech_window_qc": _load_optional_json(work_dir, "silence_periods.qc.json")
        or {"coarse_asr_windows": 0, "low_confidence_speech_flags": 0},
        "research_guardrail": _research_guardrail_qc(review, context),
        "warnings": warnings,
        "verdict": verdict,
    }


def write_grounding_qc(work_dir, qc):
    """Compatibility seam: write a prebuilt grounding_qc.json payload."""
    work_dir = Path(work_dir)
    (work_dir / "grounding_qc.json").write_text(
        json.dumps(qc, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return qc


def _write_grounding_qc(work_dir, review, bundle, *, timeline="source"):
    qc = build_grounding_qc(work_dir, review, bundle, timeline=timeline)
    return write_grounding_qc(work_dir, qc)
