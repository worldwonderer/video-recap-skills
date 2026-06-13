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

INDEX_PROMPT = """你在根据逐场景画面分析，为一个视频建立【全局理解索引】，供后续写解说词时保持人物/关系/主线一致。
只依据给到的画面证据（场景描述 + 帧实动作），不要脑补画面之外的剧情。
只返回 JSON：
{"characters":[{"name":"角色名或外观指代","description":"身份/特征"}],
 "relationships":[{"a":"角色","b":"角色","relation":"关系"}],
 "plot_points":["按时间顺序的关键剧情节点"],
 "entities":["重要物件/地点/线索"]}"""


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


def build_index_messages(vlm_analysis):
    lines = []
    for scene in (vlm_analysis or []):
        if not isinstance(scene, dict):
            continue
        sid = scene.get("scene_id", "?")
        start = float(scene.get("start", 0) or 0)
        end = float(scene.get("end", 0) or 0)
        desc = str(scene.get("description", "")).strip().replace("\n", " ")
        facts = scene.get("frame_facts")
        fact_txt = ""
        if isinstance(facts, dict) and facts:  # frame_facts is a DICT {ts: [actions]}
            actions = []
            for ts in sorted(facts.keys(), key=lambda x: float(x)):
                vals = facts[ts]
                actions.extend(vals if isinstance(vals, list) else [str(vals)])
            if actions:
                fact_txt = " | 帧实: " + "；".join(a for a in actions[:6] if a)
        lines.append(f"[场景{sid} {start:.0f}-{end:.0f}s] {desc}{fact_txt}")
    user = f"{INDEX_PROMPT}\n\n## 逐场景画面分析（共 {len(lines)} 段）\n" + "\n".join(lines)
    return [{"role": "user", "content": user}]


def parse_index_response(text):
    data = _extract_json(text)
    if not isinstance(data, dict):
        return {"characters": [], "relationships": [], "plot_points": [], "entities": []}
    out = {}
    for key in ("characters", "relationships", "plot_points", "entities"):
        val = data.get(key)
        out[key] = val if isinstance(val, list) else []
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
        out.extend(f"{i+1}. {p}" for i, p in enumerate(plot))
        out.append("")
    ents = index.get("entities") or []
    if ents:
        out.append("## Entities")
        out.extend(f"- {e}" for e in ents)
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
        if existing.get("source_md5") == _asr_source_md5(work_dir):
            log("consolidate(asr): asr_clean.json 已最新，跳过")
            return existing
    resp = api_call({"model": CONFIG.get("vlm_model", ""),
                     "messages": build_clean_messages(asr_result),
                     "max_tokens": 4000, "temperature": 0.2})
    content = _response_text(resp)
    segments = parse_clean_response(content, asr_result)
    payload = {"source_md5": _asr_source_md5(work_dir), "segments": segments}
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
    if _fresh(out_path, work_dir / "vlm_analysis.json"):
        log("consolidate(index): understanding_index.json 已最新，跳过")
        return _load(work_dir, "understanding_index.json")
    resp = api_call({"model": CONFIG.get("vlm_model", ""),
                     "messages": build_index_messages(vlm_analysis),
                     "max_tokens": 2500, "temperature": 0.2})
    index = parse_index_response(_response_text(resp))
    out_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
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
