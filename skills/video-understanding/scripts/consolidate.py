#!/usr/bin/env python3
"""video-understanding consolidation / 整理 (index build-up).

Optional, synchronous, in-pipeline. Two independent LLM passes over the video's own signal:
  - Pass B (index, default): roll up per-scene vlm_analysis into a global
    character / relationship / plot index -> understanding_index.json (+ .md).
  - Pass A (asr cleanup, opt-in): clean/punctuate/lightly speaker-attribute the raw run-on
    ASR text -> asr_clean.json. Timing is preserved BY CONSTRUCTION: the model returns cleaned
    TEXT per segment index only; each segment's original start/end is re-attached here, so a
    cleanup pass can never shift the timing-bearing spans downstream chunking depends on.

Both passes degrade gracefully (a chat-API failure logs and is skipped) and are idempotent
(a fresh artifact is reused). Mirrors review.py's pure-seam + thin-driver shape so it is
unit-testable with a mocked api_call. NON-required: the pipeline runs unchanged without it.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path

from lib import CONFIG, log, api_call
from understand import _fresh  # canonical freshness helper (same skill, safe import)

# Shared tolerance for the per-segment span check. The brief-side gate inlines the SAME
# literal (it cannot import this module without breaking the brief/narration byte-parity).
# Keep both in sync. Consolidate preserves spans exactly, so this only guards hand-edited files.
_ASR_SPAN_TOL = 0.05

CLEAN_PROMPT = """你在清洗中文视频的 ASR 逐段转写。对【每一段】做：补标点、修明显同音/错别字、（能判断时）在句首轻标说话人，让长段连读文本变成清晰可读的句子。
铁律：
- 不要合并或拆分段落，输出段数必须与输入完全一致，顺序一致。
- 不要改时间，不要输出 start/end（时间由程序保留）。
- 只清洗 text，不要增删事实、不要脑补画面。
只返回 JSON：{"segments":[{"i":0,"text":"清洗后的文本","speaker":"可选说话人"}, ...]}，i 为输入段的下标。"""

INDEX_SCHEMA_VERSION = 2

INDEX_PROMPT = """你在根据逐场景画面分析、ASR对白、background_research术语表，为一个视频建立【全局理解索引】，供后续写解说词时保持人物/关系/主线一致。
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


# ── pure seams (no I/O; unit-testable) ────────────────────────────────────────

def build_clean_messages(asr_result):
    lines = []
    for i, seg in enumerate(asr_result or []):
        if not isinstance(seg, dict):
            continue
        lines.append(f'{i}. {str(seg.get("text", "")).strip()}')
    user = f"{CLEAN_PROMPT}\n\n## 逐段转写（共 {len(lines)} 段）\n" + "\n".join(lines)
    return [{"role": "user", "content": user}]


def parse_clean_response(text, asr_result):
    """Zip cleaned TEXT onto the original segments (start/end preserved by construction).
    Returns the original asr_result UNCHANGED on any parse / shape / count problem."""
    base = [s for s in (asr_result or []) if isinstance(s, dict)]
    if not base:
        return asr_result
    data = _extract_json(text)
    if not isinstance(data, dict):
        return asr_result
    segs = data.get("segments")
    if not isinstance(segs, list) or len(segs) != len(base):
        return asr_result  # count mismatch -> reject wholesale (idempotent no-op)
    by_index = {}
    for item in segs:
        if isinstance(item, dict) and isinstance(item.get("i"), int):
            by_index[item["i"]] = item
    if len(by_index) != len(base):
        return asr_result
    out = []
    for i, seg in enumerate(base):
        cleaned = by_index.get(i, {})
        new_text = str(cleaned.get("text", "")).strip() or str(seg.get("text", ""))
        merged = {"start": seg.get("start"), "end": seg.get("end"), "text": new_text}
        speaker = str(cleaned.get("speaker", "")).strip()
        if speaker:
            merged["speaker"] = speaker
        out.append(merged)
    return out


def _json_md5_file(work_dir, name):
    path = Path(work_dir) / name
    return hashlib.md5(path.read_bytes()).hexdigest() if path.exists() else ""


def _asr_clean_source_md5(work_dir):
    return _json_md5_file(work_dir, "asr_clean.json")


def _research_source_md5(work_dir):
    return _json_md5_file(work_dir, "background_research.json")


def _clip_plan_source_md5(work_dir):
    return _json_md5_file(work_dir, "clip_plan_validated.json")


def _research_glossary_from_context(background_research):
    glossary = []
    if not isinstance(background_research, dict):
        return glossary
    chars = background_research.get("characters")
    if isinstance(chars, dict):
        for name, role in chars.items():
            if str(name).strip():
                glossary.append({"name": str(name).strip(), "aliases": [], "role": str(role).strip(), "support": "context_only"})
    details = background_research.get("character_details")
    if isinstance(details, dict):
        for name, info in details.items():
            if not isinstance(info, dict):
                continue
            aliases = [str(a).strip() for a in (info.get("aliases") or []) if str(a).strip()] if isinstance(info.get("aliases"), list) else []
            role = str(info.get("role", "")).strip()
            if str(name).strip() or aliases or role:
                glossary.append({"name": str(name).strip(), "aliases": aliases, "role": role, "support": "context_only"})
    return glossary




def _dialogue_segments_for_index(asr_result=None, asr_clean=None):
    clean_segments = (asr_clean or {}).get("segments") if isinstance(asr_clean, dict) else None
    source = clean_segments if isinstance(clean_segments, list) else (asr_result or [])
    out = []
    for i, seg in enumerate(source or []):
        if not isinstance(seg, dict):
            continue
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        out.append({
            "id": f"asr:{i}",
            "start": seg.get("start"),
            "end": seg.get("end"),
            "text": text,
        })
    return out


def _stable_unique(values):
    seen = set()
    out = []
    for value in values or []:
        key = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, dict) else str(value)
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def _apply_deterministic_asr_research_fallback(index, asr_result=None, asr_clean=None, background_research=None):
    """Guarantee ASR/research mentions survive even when the LLM omits them.

    The LLM still owns synthesis, but cacheable deterministic fields make ASR contribution
    observable: aliases/names from background_research are matched against ASR/asr_clean text
    and written to characters[*].asr_mentions/evidence_ids without inventing relationships or
    plot facts. Research remains context_only via research_glossary.
    """
    index = dict(index or {})
    for key in ("characters", "relationships", "plot_points", "entities", "research_glossary"):
        index.setdefault(key, [])
    glossary = index.get("research_glossary") or _research_glossary_from_context(background_research)
    if not glossary:
        glossary = _research_glossary_from_context(background_research)
    for item in glossary:
        if isinstance(item, dict):
            item["support"] = "context_only"
    index["research_glossary"] = glossary

    segments = _dialogue_segments_for_index(asr_result=asr_result, asr_clean=asr_clean)
    char_by_name = {}
    for c in index.get("characters") or []:
        if isinstance(c, dict) and str(c.get("name", "")).strip():
            char_by_name[str(c.get("name")).strip()] = c
    for g in glossary:
        if not isinstance(g, dict):
            continue
        name = str(g.get("name", "")).strip()
        aliases = [str(a).strip() for a in (g.get("aliases") or []) if str(a).strip()]
        terms = [t for t in [name] + aliases if t]
        if not terms:
            continue
        matches = []
        evidence_ids = []
        for seg in segments:
            text = seg["text"]
            hit_terms = [term for term in terms if term and term in text]
            if hit_terms:
                matches.append({"text": text, "evidence_id": seg["id"], "matched_aliases": hit_terms})
                evidence_ids.append(seg["id"])
        if not matches:
            continue
        char = char_by_name.get(name)
        if char is None:
            char = {"name": name or matches[0]["matched_aliases"][0], "description": "", "aliases": aliases, "visual_descriptions": [], "asr_mentions": [], "research_role": str(g.get("role", "")).strip(), "evidence_ids": [], "confidence": "medium"}
            index["characters"].append(char)
            char_by_name[char["name"]] = char
        char.setdefault("aliases", [])
        char["aliases"] = _stable_unique(list(char.get("aliases") or []) + aliases)
        char.setdefault("asr_mentions", [])
        char["asr_mentions"] = _stable_unique(list(char.get("asr_mentions") or []) + matches)
        char.setdefault("evidence_ids", [])
        char["evidence_ids"] = _stable_unique(list(char.get("evidence_ids") or []) + evidence_ids)
        if not char.get("research_role"):
            char["research_role"] = str(g.get("role", "")).strip()
        char.setdefault("confidence", "medium")
    return index

def build_index_messages(vlm_analysis, asr_result=None, asr_clean=None, background_research=None):
    lines = []
    for i, scene in enumerate(vlm_analysis or []):
        if not isinstance(scene, dict):
            continue
        sid = scene.get("scene_id", i)
        start = float(scene.get("start", 0) or 0)
        end = float(scene.get("end", 0) or 0)
        desc = str(scene.get("description", "")).strip().replace("\n", " ")
        facts = scene.get("frame_facts")
        fact_txt = ""
        if isinstance(facts, dict) and facts:
            actions = []
            def fact_key(x):
                try:
                    return (0, float(x))
                except (TypeError, ValueError):
                    return (1, str(x))
            for ts in sorted(facts.keys(), key=fact_key):
                vals = facts[ts]
                actions.extend(vals if isinstance(vals, list) else [str(vals)])
            if actions:
                fact_txt = " | 帧实: " + "；".join(a for a in actions[:6] if a)
        lines.append(f"[visual:{i} 场景{sid} {start:.0f}-{end:.0f}s] {desc}{fact_txt}")
    asr_lines = []
    clean_segments = (asr_clean or {}).get("segments") if isinstance(asr_clean, dict) else None
    source_asr = clean_segments if isinstance(clean_segments, list) else (asr_result or [])
    for i, seg in enumerate(source_asr or []):
        if not isinstance(seg, dict):
            continue
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        asr_lines.append(f"[asr:{i} {float(seg.get('start', 0) or 0):.0f}-{float(seg.get('end', 0) or 0):.0f}s] {text}")
    glossary = _research_glossary_from_context(background_research)
    glossary_lines = []
    for i, item in enumerate(glossary[:80]):
        aliases = "/".join(item.get("aliases") or [])
        alias_txt = f" aliases={aliases}" if aliases else ""
        glossary_lines.append(f"[research:{i} support=context_only] {item.get('name','')}{alias_txt}: {item.get('role','')}")
    user = (
        f"{INDEX_PROMPT}\n\n"
        f"## 逐场景画面分析（共 {len(lines)} 段）\n" + "\n".join(lines) + "\n\n"
        f"## ASR / cleaned dialogue（共 {len(asr_lines)} 段）\n" + ("\n".join(asr_lines) or "(无)") + "\n\n"
        f"## Research glossary（clock=null/context_only，不得升级为当前剧情事实）\n" + ("\n".join(glossary_lines) or "(无)")
    )
    return [{"role": "user", "content": user}]

def parse_index_response(text):
    data = _extract_json(text)
    if not isinstance(data, dict):
        return {"schema_version": INDEX_SCHEMA_VERSION, "characters": [], "relationships": [], "plot_points": [], "entities": [], "research_glossary": []}
    out = {"schema_version": INDEX_SCHEMA_VERSION}
    for key in ("characters", "relationships", "plot_points", "entities", "research_glossary"):
        val = data.get(key)
        out[key] = val if isinstance(val, list) else []
    for item in out["research_glossary"]:
        if isinstance(item, dict):
            item["support"] = "context_only"
    return out


def format_index_md(index):
    out = ["# Understanding index (from consolidate.py)", ""]
    chars = index.get("characters") or []
    if chars:
        out.append("## Characters")
        for c in chars:
            if isinstance(c, dict):
                out.append(f"- **{c.get('name', '?')}** — {c.get('description', '')}".rstrip())
            else:
                out.append(f"- {c}")
        out.append("")
    rels = index.get("relationships") or []
    if rels:
        out.append("## Relationships")
        for r in rels:
            if isinstance(r, dict):
                out.append(f"- {r.get('a', '?')} — {r.get('relation', '?')} — {r.get('b', '?')}")
            else:
                out.append(f"- {r}")
        out.append("")
    plot = index.get("plot_points") or []
    if plot:
        out.append("## Plot spine")
        for i, p in enumerate(plot):
            out.append(f"{i+1}. {p.get('text', p) if isinstance(p, dict) else p}")
        out.append("")
    ents = index.get("entities") or []
    if ents:
        out.append("## Entities")
        for e in ents:
            out.append(f"- {e.get('name', e) if isinstance(e, dict) else e}")
        out.append("")
    glossary = index.get("research_glossary") or []
    if glossary:
        out.append("## Research glossary (context_only)")
        for g in glossary:
            if isinstance(g, dict):
                aliases = "/".join(g.get("aliases") or [])
                alias_txt = f" ({aliases})" if aliases else ""
                out.append(f"- {g.get('name', '?')}{alias_txt}: {g.get('role', '')} [{g.get('support', 'context_only')}]")
            else:
                out.append(f"- {g}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _extract_json(text):
    raw = str(text or "")
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    candidate = fence.group(1) if fence else raw
    if not fence:
        first, last = candidate.find("{"), candidate.rfind("}")
        if first != -1 and last > first:
            candidate = candidate[first:last + 1]
    try:
        return json.loads(candidate)
    except ValueError:
        return None


def _asr_source_md5(work_dir):
    """Provenance: md5 of the on-disk asr_result.json BYTES (writer + reader hash the same thing)."""
    path = Path(work_dir) / "asr_result.json"
    return hashlib.md5(path.read_bytes()).hexdigest() if path.exists() else ""


def _vlm_source_md5(work_dir):
    """Provenance: md5 of the on-disk vlm_analysis.json bytes."""
    path = Path(work_dir) / "vlm_analysis.json"
    return hashlib.md5(path.read_bytes()).hexdigest() if path.exists() else ""


def _index_meta_path(work_dir):
    return Path(work_dir) / "understanding_index.json.meta.json"


def _prompt_fingerprint(prompt):
    return hashlib.md5(str(prompt or "").encode("utf-8")).hexdigest()


def _write_index_meta(work_dir, vlm_analysis):
    _index_meta_path(work_dir).write_text(json.dumps({
        "schema_version": INDEX_SCHEMA_VERSION,
        "source_md5": _vlm_source_md5(work_dir),
        "vlm_md5": _vlm_source_md5(work_dir),
        "asr_md5": _asr_source_md5(work_dir),
        "asr_clean_md5": _asr_clean_source_md5(work_dir),
        "research_md5": _research_source_md5(work_dir),
        "clip_plan_md5": _clip_plan_source_md5(work_dir),
        "scene_count": len([s for s in (vlm_analysis or []) if isinstance(s, dict)]),
        "model": CONFIG.get("vlm_model", ""),
        "prompt_md5": _prompt_fingerprint(INDEX_PROMPT),
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def _index_cache_matches(work_dir, vlm_analysis):
    meta_path = _index_meta_path(work_dir)
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return (
        isinstance(meta, dict)
        and meta.get("schema_version") == INDEX_SCHEMA_VERSION
        and meta.get("source_md5") == _vlm_source_md5(work_dir)
        and meta.get("vlm_md5", meta.get("source_md5")) == _vlm_source_md5(work_dir)
        and meta.get("asr_md5", "") == _asr_source_md5(work_dir)
        and meta.get("asr_clean_md5", "") == _asr_clean_source_md5(work_dir)
        and meta.get("research_md5", "") == _research_source_md5(work_dir)
        and meta.get("clip_plan_md5", "") == _clip_plan_source_md5(work_dir)
        and meta.get("scene_count") == len([s for s in (vlm_analysis or []) if isinstance(s, dict)])
        and meta.get("model") == CONFIG.get("vlm_model", "")
        and meta.get("prompt_md5") == _prompt_fingerprint(INDEX_PROMPT)
    )


# ── thin drivers (I/O + api_call) ─────────────────────────────────────────────

def _load(work_dir, name):
    path = Path(work_dir) / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def consolidate_transcript(work_dir):
    work_dir = Path(work_dir)
    asr_result = _load(work_dir, "asr_result.json")
    if not asr_result:
        log("consolidate(asr): 无 asr_result.json，跳过")
        return None
    out_path = work_dir / "asr_clean.json"
    if _fresh(out_path, work_dir / "asr_result.json"):
        existing = _load(work_dir, "asr_clean.json") or {}
        if (
            existing.get("source_md5") == _asr_source_md5(work_dir)
            and existing.get("model") == CONFIG.get("vlm_model", "")
            and existing.get("prompt_md5") == _prompt_fingerprint(CLEAN_PROMPT)
        ):
            log("consolidate(asr): asr_clean.json 已最新，跳过")
            return existing
    resp = api_call({"model": CONFIG.get("vlm_model", ""),
                     "messages": build_clean_messages(asr_result),
                     "max_tokens": 4000, "temperature": 0.2})
    content = _response_text(resp)
    segments = parse_clean_response(content, asr_result)
    payload = {
        "source_md5": _asr_source_md5(work_dir),
        "model": CONFIG.get("vlm_model", ""),
        "prompt_md5": _prompt_fingerprint(CLEAN_PROMPT),
        "segments": segments,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"consolidate(asr): 写出 asr_clean.json（{len(segments)} 段）")
    return payload


def consolidate_index(work_dir):
    work_dir = Path(work_dir)
    vlm_analysis = _load(work_dir, "vlm_analysis.json")
    if not vlm_analysis:
        log("consolidate(index): 无 vlm_analysis.json，跳过")
        return None
    out_path = work_dir / "understanding_index.json"
    if _fresh(out_path, work_dir / "vlm_analysis.json") and _index_cache_matches(work_dir, vlm_analysis):
        log("consolidate(index): understanding_index.json 已最新，跳过")
        return _load(work_dir, "understanding_index.json")
    asr_result = _load(work_dir, "asr_result.json") or []
    asr_clean = _load(work_dir, "asr_clean.json") or {}
    background_research = _load(work_dir, "background_research.json") or {}
    resp = api_call({"model": CONFIG.get("vlm_model", ""),
                     "messages": build_index_messages(vlm_analysis, asr_result=asr_result, asr_clean=asr_clean, background_research=background_research),
                     "max_tokens": 3000, "temperature": 0.2})
    index = parse_index_response(_response_text(resp))
    index = _apply_deterministic_asr_research_fallback(
        index, asr_result=asr_result, asr_clean=asr_clean, background_research=background_research)
    out_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_index_meta(work_dir, vlm_analysis)
    (work_dir / "understanding_index.md").write_text(format_index_md(index), encoding="utf-8")
    log(f"consolidate(index): 写出 understanding_index.json（角色 {len(index['characters'])}）")
    return index


def consolidate(work_dir, do_asr=False, do_index=True):
    """Default = index-only (Pass B, zero timing risk). Pass A (asr) is opt-in."""
    result = {}
    if do_index:
        try:
            result["index"] = consolidate_index(work_dir)
        except Exception as e:
            log(f"consolidate(index) 跳过（忽略）: {e}")
    if do_asr:
        try:
            result["asr_clean"] = consolidate_transcript(work_dir)
        except Exception as e:
            log(f"consolidate(asr) 跳过（忽略）: {e}")
    return result


def _response_text(resp):
    try:
        return resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        log("consolidate: API 返回结构异常")
        return ""


def main():
    ap = argparse.ArgumentParser(description="Consolidate the understanding index (and optionally clean ASR).")
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--asr", action="store_true", help="also run Pass A (ASR cleanup)")
    ap.add_argument("--no-index", action="store_true", help="skip Pass B (index)")
    args = ap.parse_args()
    res = consolidate(args.work_dir, do_asr=args.asr, do_index=not args.no_index)
    print(json.dumps({"status": "consolidated",
                      "index": bool(res.get("index")),
                      "asr_clean": bool(res.get("asr_clean"))}, ensure_ascii=False))


if __name__ == "__main__":
    main()
