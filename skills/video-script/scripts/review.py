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
import re
from pathlib import Path

from lib import CONFIG, log, api_call

CATEGORIES = [
    "hallucination", "weak_hook", "no_throughline", "narrating_picture",
    "density", "pacing", "cliche", "incomplete", "other",
]

RUBRIC = """你是中文视频解说稿的严格评审。依据以下规则审阅草稿，只指出真实问题，宁缺毋滥：
1. 反幻觉（最重要）：解说里的人物、动作、因果、关系必须能从「画面证据」(frame_facts/描述) 或「对白」(ASR) 推断出来。凭空虚构、过度脑补 → severity=error, category=hallucination，指出是哪一句、依据缺在哪。
2. 钩子：开头 1-2 段要制造悬念/利害，不是交代场景。弱钩子 → weak_hook。
3. 主线：应有一条贯穿主线（目标/关系/悬念），每段推进它，不要每个场景从头讲。缺主线 → no_throughline。
4. 给信息而非念画面：观众看得见动作表情；解说要讲动机/关系/潜台词/剧情意义。复述画面 → narrating_picture。
5. 密度/节奏：连续铺底、短句、相邻段不要断太久；过疏/过密/拖沓 → density 或 pacing。
6. 去废词：删空泛形容（"危机四伏""震撼人心"）→ cliche。
7. 完整句子：半句话/未收尾 → incomplete。
只返回 JSON（不要额外解释），格式：
{"verdict":"REVISE|OK","summary":"一两句总体判断","findings":[{"segment":<草稿段号(从0起)或null表示整体>,"severity":"error|warning|suggestion","category":"<上面类别之一>","issue":"问题","fix":"具体改法"}]}"""


def _load(work_dir, name):
    path = Path(work_dir) / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def _scene_grounding(vlm_analysis, limit=60):
    lines = []
    for scene in (vlm_analysis or [])[:limit]:
        if not isinstance(scene, dict):
            continue
        sid = scene.get("scene_id", "?")
        start = scene.get("start", 0)
        end = scene.get("end", 0)
        desc = str(scene.get("description", "")).strip().replace("\n", " ")
        facts = scene.get("frame_facts") or []
        fact_txt = ""
        if isinstance(facts, list) and facts:
            picks = []
            for f in facts[:4]:
                if isinstance(f, dict):
                    picks.append(str(f.get("fact", f.get("text", ""))).strip())
                else:
                    picks.append(str(f).strip())
            fact_txt = " | 帧实: " + "；".join(p for p in picks if p)
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


def build_review_messages(narration, vlm_analysis, asr_result):
    """Pure: assemble the reviewer chat messages (testable without the API)."""
    grounding = _scene_grounding(vlm_analysis)
    dialogue = _asr_grounding(asr_result)
    draft = _format_draft(narration)
    user = (
        f"{RUBRIC}\n\n"
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
    if verdict not in ("REVISE", "OK"):
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
        findings.append({
            "segment": f.get("segment"),
            "severity": sev,
            "category": cat,
            "issue": str(f.get("issue", "")).strip(),
            "fix": str(f.get("fix", "")).strip(),
        })
    return {"verdict": verdict, "summary": str(data.get("summary", "")).strip(), "findings": findings}


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
        "## Findings",
    ]
    if not findings:
        out.append("- (none)")
    for f in findings:
        seg = "整体" if f["segment"] is None else f"段 {f['segment']}"
        out.append(f"- **[{f['severity']}/{f['category']}] {seg}** — {f['issue']}")
        if f["fix"]:
            out.append(f"  - 改法: {f['fix']}")
    return "\n".join(out) + "\n"


def review_narration(work_dir):
    work_dir = Path(work_dir)
    narration = _load(work_dir, "narration.json")
    if narration is None:
        raise SystemExit(f"缺少 {work_dir / 'narration.json'}；先写解说草稿再评审")
    vlm_analysis = _load(work_dir, "vlm_analysis.json") or []
    asr_result = _load(work_dir, "asr_result.json") or []

    messages = build_review_messages(narration, vlm_analysis, asr_result)
    resp = api_call({
        "model": CONFIG.get("vlm_model", ""),
        "messages": messages,
        "max_tokens": 2000,
        "temperature": 0.2,
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


def main():
    ap = argparse.ArgumentParser(description="Review an agent-written narration.json for quality (LLM-as-judge).")
    ap.add_argument("--work-dir", required=True)
    args = ap.parse_args()
    review = review_narration(args.work_dir)
    print(json.dumps({
        "status": "reviewed",
        "verdict": review["verdict"],
        "findings": len(review["findings"]),
        "review": str(Path(args.work_dir) / "narration_review.md"),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
