"""Deterministic narration readability/QC scanner.

This module is intentionally report-only. It flags local readability and
packaging hygiene risks for video narration; it is not an AIGC detector and it
never rewrites text.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

CONTRACT = (
    "Local readability/QC report only: this is not an AIGC detector, does not "
    "claim AI-generation accuracy, and never rewrites text. Corrections remain "
    "human/agent rewrite work."
)

EM_DASH_RE = re.compile(r"——|—")
NEGATIVE_FLIP_RE = re.compile(r"并?不是[^。！？!?；;\.]{0,40}而是")
PLACEHOLDER_RE = re.compile(r"【主角】|TA要")

CLICHE_TERMS = [
    "命运的齿轮", "殊不知", "与此同时", "一场阴谋", "背后真相", "不为人知",
    "故事就此展开", "命运", "真相", "秘密", "危机", "反转", "救赎",
]
ABSTRACT_TERMS = ["人性", "成长", "救赎", "命运", "时代", "宿命", "选择", "意义", "价值", "情感"]
REASONING_MARKERS = ["因为", "所以", "因此", "也就是说", "换句话说", "这意味着", "原因是", "可见"]
METAPHOR_MARKERS = ["像", "仿佛", "好似", "犹如", "如同", "一把", "一场", "一张", "棋局", "风暴"]
CONNECTIVE_MARKERS = ["但", "却", "而", "于是", "随后", "直到", "结果", "因为", "所以", "这时", "最后"]


def _sentence_pieces(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text:
        return []
    parts = re.split(r"([。！？!?；;\.])", text)
    out: list[str] = []
    for idx in range(0, len(parts), 2):
        body = parts[idx].strip()
        punct = parts[idx + 1] if idx + 1 < len(parts) else ""
        if body or punct:
            out.append((body + punct).strip())
    return out or [text]


def _text_units(text: str) -> int:
    text = str(text or "")
    if re.search(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", text):
        return len(re.sub(r"\s+", "", text))
    return len(re.findall(r"\b\w+\b", text))


def _normalise_segments(payload: Any, *, source: str) -> list[dict[str, Any]]:
    if isinstance(payload, str):
        return [{"source": source, "index": None, "text": payload}]
    if not isinstance(payload, list):
        return []
    segments: list[dict[str, Any]] = []
    for idx, item in enumerate(payload):
        if isinstance(item, dict):
            key = "narration" if source == "narration" else "text"
            text = str(item.get(key, item.get("text", item.get("narration", ""))) or "").strip()
        else:
            text = str(item or "").strip()
        if text:
            segments.append({"source": source, "index": idx, "text": text})
    return segments


def _load_json(path: Path) -> tuple[Any, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except Exception as exc:  # intentionally broad: malformed artifacts are QC findings
        return None, str(exc)


def _load_original_subtitles(work_dir: Path | None) -> list[dict[str, Any]]:
    if work_dir is None:
        return []
    path = work_dir / "original_subtitles.json"
    if not path.exists():
        return []
    data, err = _load_json(path)
    if err:
        return []
    return _normalise_segments(data, source="original_subtitles")


def _style_card_requirement(work_dir: Path | None) -> tuple[bool, str]:
    """Read the explicit stable requirements contract.

    Legacy/migration workspaces that do not have a valid requirements file are
    advisory-only: they must not be hard-gated by prompt text in
    agent_narration_brief.md.
    """
    if work_dir is None:
        return False, "legacy_default"
    path = work_dir / "deslop_qc_requirements.json"
    if not path.exists():
        return False, "legacy_default"
    data, err = _load_json(path)
    if err is not None or not isinstance(data, dict):
        return False, "legacy_default"
    required = data.get("style_card_required")
    if not isinstance(required, bool):
        return False, "legacy_default"
    return required, "deslop_qc_requirements.json"


def _style_card_issue(work_dir: Path | None, required: bool) -> dict[str, Any] | None:
    if work_dir is None:
        return None
    path = work_dir / "style_card.json"
    if not path.exists():
        return {
            "severity": "blocker" if required else "advisory",
            "code": "missing_style_card",
            "source": "style_card",
            "index": None,
            "message": (
                "style_card.json is required by this expression/packaging run but is missing"
                if required else
                "style_card.json is absent; legacy/migration workspaces may continue, but new expression-special runs should author it"
            ),
        }
    data, err = _load_json(path)
    if err is not None or not isinstance(data, dict) or not data:
        return {
            "severity": "blocker" if required else "advisory",
            "code": "malformed_style_card",
            "source": "style_card",
            "index": None,
            "message": (
                "style_card.json is required but missing, malformed, empty, or not a JSON object"
                if required else
                "style_card.json is present but malformed/empty; treat as migration warning unless the run requires it"
            ),
            "detail": err,
        }
    return None


def _add_issue(bucket: list[dict[str, Any]], severity: str, code: str, source: str, index: int | None, message: str, **extra: Any) -> None:
    issue = {"severity": severity, "code": code, "source": source, "index": index, "message": message}
    issue.update({k: v for k, v in extra.items() if v is not None})
    bucket.append(issue)


def analyze_deslop_qc(narration: Any, *, work_dir: str | Path | None = None, original_subtitles: Any = None) -> dict[str, Any]:
    """Return a structured report; never mutates or rewrites input text."""
    work_path = Path(work_dir) if work_dir is not None else None
    segments = _normalise_segments(narration, source="narration")
    if original_subtitles is None:
        segments.extend(_load_original_subtitles(work_path))
    else:
        segments.extend(_normalise_segments(original_subtitles, source="original_subtitles"))

    blockers: list[dict[str, Any]] = []
    advisories: list[dict[str, Any]] = []

    required, requirement_source = _style_card_requirement(work_path)
    style_issue = _style_card_issue(work_path, required)
    if style_issue:
        (blockers if style_issue["severity"] == "blocker" else advisories).append(style_issue)

    all_text = "\n".join(seg["text"] for seg in segments)
    total_units = max(1, _text_units(all_text))
    sentences: list[str] = []
    for seg in segments:
        text = seg["text"]
        if EM_DASH_RE.search(text):
            _add_issue(blockers, "blocker", "em_dash", seg["source"], seg["index"], "破折号（—/——）不得出现在 narration/original_subtitles 中", matches=EM_DASH_RE.findall(text))
        if PLACEHOLDER_RE.search(text):
            _add_issue(blockers, "blocker", "placeholder_leakage", seg["source"], seg["index"], "示例占位内容泄漏到成稿中（如【主角】/TA要）", matches=PLACEHOLDER_RE.findall(text))
        for sentence in _sentence_pieces(text):
            sentences.append(sentence)
            if NEGATIVE_FLIP_RE.search(sentence):
                _add_issue(advisories, "advisory", "negative_positive_flip", seg["source"], seg["index"], "“不是…而是…”模板化转折偏多，建议改成更具体的因果/行动表达", sentence=sentence[:120])

    def term_count(terms: list[str]) -> int:
        return sum(all_text.count(term) for term in terms)

    cliche_count = term_count(CLICHE_TERMS)
    abstract_count = term_count(ABSTRACT_TERMS)
    reasoning_count = term_count(REASONING_MARKERS)
    metaphor_count = term_count(METAPHOR_MARKERS)
    connective_count = term_count(CONNECTIVE_MARKERS)

    if cliche_count >= 4 or (cliche_count / total_units) > 0.025:
        _add_issue(advisories, "advisory", "cliche_density", "narration", None, "套话/高频抽象词偏密，建议换成具体行动、选择和后果", count=cliche_count)
    if abstract_count >= 5 or (abstract_count / total_units) > 0.03:
        _add_issue(advisories, "advisory", "abstract_summary", "narration", None, "抽象总结词偏多，容易像概括论文；补足画面证据和具体抉择", count=abstract_count)
    if reasoning_count >= 4:
        _add_issue(advisories, "advisory", "reasoning_chain", "narration", None, "解释链标记偏多，建议减少“因为/所以/这意味着”等讲理口吻", count=reasoning_count)
    if metaphor_count >= 4:
        _add_issue(advisories, "advisory", "metaphor_markers", "narration", None, "比喻/包装化标记偏多，确认没有遮住剧情事实", count=metaphor_count)
    if total_units >= 180 and connective_count <= 1:
        _add_issue(advisories, "advisory", "low_connective_density", "narration", None, "长文本缺少转折/因果连接，可能像平铺摘要", connective_count=connective_count)

    long_segments = [seg for seg in segments if seg["source"] == "narration" and _text_units(seg["text"]) > 180]
    if long_segments:
        _add_issue(advisories, "advisory", "overlong_narration_block", "narration", long_segments[0].get("index"), "单个解说块过长，建议拆成更可听的段落", max_units=max(_text_units(seg["text"]) for seg in long_segments))
    long_sentences = [s for s in sentences if _text_units(s) > 90]
    if long_sentences:
        _add_issue(advisories, "advisory", "long_paragraph", "narration", None, "长句/长段落偏多，字幕阅读压力较大", max_units=max(_text_units(s) for s in long_sentences))

    return {
        "ok": not blockers,
        "contract": CONTRACT,
        "scanner": "deslop_qc.py",
        "style_card_required": required,
        "style_card_requirement_source": requirement_source,
        "blocker_count": len(blockers),
        "advisory_count": len(advisories),
        "blockers": blockers,
        "advisories": advisories,
        "metrics": {
            "segments_scanned": len(segments),
            "text_units": total_units if all_text else 0,
            "sentence_count": len(sentences),
            "cliche_count": cliche_count,
            "abstract_count": abstract_count,
            "reasoning_marker_count": reasoning_count,
            "metaphor_marker_count": metaphor_count,
            "connective_count": connective_count,
        },
    }


__all__ = ["CONTRACT", "analyze_deslop_qc"]
