"""Load and format research, consolidation, and substrate context."""

import hashlib

import importlib.util


import json


import re

from pathlib import Path

from lib import CONFIG

from lib import log

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


def _load_background_research(work_dir):
    """Load the agent-authored background_research.json if present.

    This file is the single highest-leverage quality input (character names,
    relationships, plot context). It used to be documented but never read by
    any code, so researched story knowledge never reached the writing brief.
    """
    path = Path(work_dir) / "background_research.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log(f"警告: background_research.json 读取失败，忽略: {exc}")
        return {}
    return data if isinstance(data, dict) else {}


def _clip_text(text, limit):
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value[:limit]


def _format_background_research(research, limit=1800):
    """Render background_research.json into a bounded Story-context brief section."""
    if not isinstance(research, dict) or not research:
        return []
    lines = [
        "## Story context (from background_research.json)",
        "",
        "Research is context_only: use it for aliases/names/world terms and weak background only; do NOT reveal future plot or upgrade research-only relationships/causes into current on-screen facts without visual/ASR support.",
        "",
    ]
    for key, label in (
        ("synopsis", "Synopsis"),
        ("worldbuilding", "Worldbuilding"),
        ("episode_context", "Episode context"),
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
                lines.append(f"    - {clean_name}: {clean_desc}")

    details = research.get("character_details")
    if isinstance(details, dict) and details:
        lines.append("- Character details:")
        for name, info in list(details.items())[:8]:
            if not isinstance(info, dict):
                continue
            bits = []
            aliases = info.get("aliases")
            if isinstance(aliases, list) and aliases:
                clean_aliases = [_clip_text(alias, 40) for alias in aliases[:4]]
                clean_aliases = [alias for alias in clean_aliases if alias]
                if clean_aliases:
                    bits.append("别名 " + "/".join(clean_aliases))
            role = _clip_text(info.get("role"), 80)
            if role:
                bits.append(role)
            rels = info.get("relationships")
            if isinstance(rels, list) and rels:
                clean_rels = [_clip_text(rel, 80) for rel in rels[:4]]
                clean_rels = [rel for rel in clean_rels if rel]
                if clean_rels:
                    bits.append("；".join(clean_rels))
            clean_name = _clip_text(name, 60)
            if clean_name and bits:
                lines.append(f"    - {clean_name}: {'; '.join(bits)}")

    arcs = research.get("plot_arcs")
    if isinstance(arcs, list) and arcs:
        lines.append("- Plot arcs:")
        for arc in arcs[:8]:
            if not isinstance(arc, dict):
                value = _clip_text(arc, 180)
                if value:
                    lines.append(f"    - {value}")
                continue
            name = _clip_text(arc.get("name"), 80)
            desc = _clip_text(arc.get("description"), 180)
            status = _clip_text(arc.get("status"), 40)
            if name or desc:
                tail = f" [{status}]" if status else ""
                lines.append(f"    - {name}: {desc}{tail}".rstrip())

    notes = research.get("cultural_notes")
    if isinstance(notes, list) and notes:
        lines.append("- Cultural notes:")
        for note in notes[:6]:
            if not isinstance(note, dict):
                value = _clip_text(note, 160)
                if value:
                    lines.append(f"    - {value}")
                continue
            item = _clip_text(note.get("item"), 80)
            expl = _clip_text(note.get("explanation"), 160)
            if item and expl:
                lines.append(f"    - {item}: {expl}")
            elif item or expl:
                lines.append(f"    - {item or expl}")

    lines.extend(
        [
            "",
            'Use these names, relationships, and stakes in the narration instead of generic labels like "男子"/"白发女子".',
            "",
        ]
    )
    text = "\n".join(lines)
    if len(text) <= limit:
        return lines
    clipped = text[:limit].rsplit("\n", 1)[0].rstrip()
    return clipped.splitlines() + [
        "",
        "[Story context clipped to keep ASR/visual evidence in context]",
        "",
    ]


def assess_understanding_substrate(
    scenes_analysis, asr_result, *, has_story_context=False
):
    """Measure how much real signal the writing agent has to work with.

    Recap quality collapses to generic 看图说话 when the agent has no story spine and
    only literal frame descriptions to paraphrase. A spine is substantial dialogue
    (ASR) OR researched/given story context — frame-fact VOLUME alone (a visually busy
    but storyless clip, e.g. an anime whose dialogue ASR could not read) is NOT a spine,
    so it must not grade as "rich"; otherwise the sparse-substrate warning, the research
    directive, and the density relief never fire for exactly that cold-narration case.
    """
    scenes = scenes_analysis or []
    asr_chars = sum(len(str(seg.get("text", "")).strip()) for seg in (asr_result or []))
    scenes_with_facts = sum(
        1 for s in scenes if isinstance(s, dict) and s.get("frame_facts")
    )
    desc_lens = [
        len(str(s.get("description", "")).strip())
        for s in scenes
        if isinstance(s, dict)
    ]
    avg_desc = sum(desc_lens) // len(desc_lens) if desc_lens else 0

    has_asr = asr_chars >= 20
    has_facts = scenes_with_facts > 0
    has_story_spine = asr_chars >= 200 or bool(has_story_context)
    if not has_asr and not has_facts and avg_desc < 25:
        level = "empty"
    elif (has_asr or has_facts) and has_story_spine:
        level = "rich"
    else:
        level = "thin"
    return {
        "level": level,
        "asr_chars": asr_chars,
        "scene_count": len(scenes),
        "scenes_with_frame_facts": scenes_with_facts,
        "avg_description_len": avg_desc,
        "has_story_context": bool(has_story_context),
    }


def _format_substrate_warning(assessment):
    """Render a loud brief banner when the understanding substrate is weak."""
    if not assessment or assessment.get("level") == "rich":
        return []
    if assessment["level"] == "empty":
        head = "⚠️ UNDERSTANDING SUBSTRATE IS EMPTY — narration will be generic guesswork unless you fix this first."
    else:
        head = '⚠️ Understanding substrate is THIN — narration risks generic "看图说话" without more grounding.'
    return [
        head,
        f"  ASR chars: {assessment['asr_chars']} | scenes: {assessment['scene_count']} | "
        f"scenes with frame_facts: {assessment['scenes_with_frame_facts']} | avg description: {assessment['avg_description_len']} chars",
        "  Before writing: do background research (write background_research.json with names/relationships/plot),",
        "  lean on any --context provided, and use ASR dialogue + frame_facts as the factual spine.",
        "  Do NOT invent plot. If you truly have nothing, keep beats sparse and factual rather than fabricating drama.",
        "",
    ]


def _write_json_artifact(work_dir, name, payload):
    path = Path(work_dir) / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _format_asr_chunks_for_brief(chunks, max_chunks=24):
    if not chunks:
        return []
    lines = [
        "## ASR writing chunks (semantic windows)",
        "",
        "Use these chunks as the dialogue spine instead of swallowing the full transcript at once.",
        "Full data: `asr_writing_chunks.json`.",
        "",
    ]
    for chunk in chunks[:max_chunks]:
        scene_ids = ",".join(str(sid) for sid in chunk.get("scene_ids", [])) or "n/a"
        text = str(chunk.get("text", "")).strip()
        if len(text) > 900:
            text = text[:897] + "..."
        lines.extend(
            [
                f"### ASR chunk {chunk['chunk_id'] + 1}: {chunk['start']:.1f}-{chunk['end']:.1f}s | scenes {scene_ids} | {chunk['char_count']} units",
                text or "(empty transcript chunk)",
                "",
            ]
        )
    if len(chunks) > max_chunks:
        lines.append(
            f"... {len(chunks) - max_chunks} more chunks in `asr_writing_chunks.json`."
        )
        lines.append("")
    return lines


def _format_timeline_fusion_for_brief(fusion, max_items=40):
    if not fusion:
        return []
    lines = [
        "## Timeline fusion (VLM + ASR + quiet slots)",
        "",
        "This is the pre-aligned multimodal view. Use `narration_slots` when available; otherwise write ducked-bed beats around dialogue.",
        "Full data: `timeline_fusion.json`.",
        "",
    ]
    for item in fusion[:max_items]:
        start, end = item.get("time_range", [0, 0])
        slots = item.get("narration_slots") or []
        slot_text = (
            ", ".join(
                f"{slot['start']:.1f}-{slot['end']:.1f}s/{slot['char_budget']}字"
                for slot in slots[:4]
            )
            or "none"
        )
        dialogue = item.get("dialogue_segments") or []
        dialogue_text = (
            "; ".join(
                f"{seg['start']:.1f}-{seg['end']:.1f}s {seg.get('text', '')[:80]}"
                for seg in dialogue[:3]
                if seg.get("text")
            )
            or "none"
        )
        lines.extend(
            [
                f"### Fusion scene {item.get('scene_id')}: {start:.1f}-{end:.1f}s ({item.get('recommended_mode')})",
                f"- Visual: {item.get('visual_description', '')}",
                f"- Dialogue overlap: {item.get('dialogue_overlap_seconds', 0):.1f}s | {dialogue_text}",
                f"- Narration slots: {slot_text}",
                "",
            ]
        )
    if len(fusion) > max_items:
        lines.append(
            f"... {len(fusion) - max_items} more fused scenes in `timeline_fusion.json`."
        )
        lines.append("")
    return lines


def _consolidation_model():
    return CONFIG.get("vlm_model", "")


def _clean_asr_prompt_fingerprint():
    # Keep this literal in sync with consolidate.CLEAN_PROMPT without importing it;
    # brief.py and narration.py must remain byte-identical cross-skill copies.
    prompt = """你在清洗中文视频的 ASR 逐段转写。对【每一段】做：补标点、修明显同音/错别字、（能判断时）在句首轻标说话人，让长段连读文本变成清晰可读的句子。
铁律：
- 不要合并或拆分段落，输出段数必须与输入完全一致，顺序一致。
- 不要改时间，不要输出 start/end（时间由程序保留）。
- 只清洗 text，不要增删事实、不要脑补画面。
只返回 JSON：{"segments":[{"i":0,"text":"清洗后的文本","speaker":"可选说话人"}, ...]}，i 为输入段的下标。"""
    return hashlib.md5(prompt.encode("utf-8")).hexdigest()


def _index_prompt_fingerprint():
    prompt = """你在根据逐场景画面分析、ASR对白、background_research术语表，为一个视频建立【全局理解索引】，供后续写解说词时保持人物/关系/主线一致。
规则：
- visual/asr 是当前视频事实证据；每个人物、关系、剧情节点、物件尽量给 evidence_ids。
- background_research 只能用于人名/别名/术语/身份消歧，默认 support=context_only；不能把后续剧情或未出现关系升级为当前画面事实。
- ASR 中出现但画面描述未命名的人名，应进入 characters[*].asr_mentions。
只返回 JSON：
{"characters":[{"name":"角色名或外观指代","description":"身份/特征","aliases":[],"visual_descriptions":[],"asr_mentions":[],"research_role":"","evidence_ids":[],"confidence":"high|medium|low"}],
 "relationships":[{"a":"角色","b":"角色","relation":"关系","evidence_ids":[],"support":"direct|indirect|context_only"}],
 "plot_points":[{"time":"00:00","text":"按时间顺序的关键剧情节点","evidence_ids":[]}],
 "entities":[{"name":"重要物件/地点/线索","evidence_ids":[]}],
 "research_glossary":[{"name":"名字/术语","aliases":[],"role":"说明","support":"context_only"}]}"""
    return hashlib.md5(prompt.encode("utf-8")).hexdigest()


def _load_consolidation(work_dir, scenes_analysis=None):
    """Load consolidate.py's understanding_index.json only when provenance matches VLM input."""
    work_dir = Path(work_dir)
    path = work_dir / "understanding_index.json"
    meta_path = work_dir / "understanding_index.json.meta.json"
    if not path.exists() or not meta_path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict) or not isinstance(meta, dict):
        return {}
    src_path = work_dir / "vlm_analysis.json"
    try:
        source_md5 = hashlib.md5(src_path.read_bytes()).hexdigest()
    except OSError:
        return {}
    if meta.get("source_md5") != source_md5:
        return {}
    expected_count = len([s for s in (scenes_analysis or []) if isinstance(s, dict)])
    if expected_count and meta.get("scene_count") != expected_count:
        return {}
    if meta.get("model") != _consolidation_model():
        return {}
    if meta.get("prompt_md5") != _index_prompt_fingerprint():
        return {}
    return data


def _format_consolidation(index):
    """Render consolidate.py's understanding_index.json into a compact brief section."""
    if not index:
        return []
    lines = ["## Understanding index (from consolidate.py)", ""]
    chars = index.get("characters") or []
    if chars:
        lines.append("- Characters:")
        for c in chars:
            if isinstance(c, dict):
                lines.append(
                    f"    - {c.get('name', '?')}: {str(c.get('description', '')).strip()}"
                )
            else:
                lines.append(f"    - {c}")
    rels = index.get("relationships") or []
    if rels:
        lines.append("- Relationships:")
        for r in rels:
            if isinstance(r, dict):
                lines.append(
                    f"    - {r.get('a', '?')} — {r.get('relation', '?')} — {r.get('b', '?')}"
                )
            else:
                lines.append(f"    - {r}")
    plot = index.get("plot_points") or []
    if plot:
        lines.append("- Plot spine:")
        lines.extend(
            f"    {i + 1}. {p.get('text', p) if isinstance(p, dict) else p}"
            for i, p in enumerate(plot)
        )
    ents = index.get("entities") or []
    if ents:
        ent_names = [str(e.get("name", e) if isinstance(e, dict) else e) for e in ents]
        lines.append(f"- Entities: {', '.join(ent_names)}")
    lines.append("")
    return lines
