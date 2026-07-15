"""Run narration review and write its advisory artifacts."""

import argparse

import json


from pathlib import Path

from lib import CONFIG, log, api_call

from evidence_bundle import build_evidence_bundle
from review_grounding import (
    _load,
    _load_cut_clip_spans,
    _load_review_grounding,
    remap_grounding_to_output_timeline,
)
from review_response import (
    _append_warning_once,
    _bundle_fingerprint,
    _bundle_fingerprint_warning,
    _chunk_evidence_bundle,
    _load_review_research_context,
    _merge_chunk_reviews,
    _write_grounding_qc,
    build_review_messages,
    format_review_md,
    parse_review_response,
)

EVIDENCE_CONTRACT_VERSION = 1

COVERAGE_POLICY_VERSION = "coverage_policy_v1"


def review_narration(work_dir, *, timeline="source", strict_evidence=False):
    work_dir = Path(work_dir)
    narration = _load(work_dir, "narration.json")
    if narration is None:
        raise SystemExit(f"缺少 {work_dir / 'narration.json'}；先写解说草稿再评审")
    vlm_analysis, asr_result = _load_review_grounding(work_dir)
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
            vlm_analysis, asr_result = remap_grounding_to_output_timeline(
                vlm_analysis, asr_result, spans
            )
    elif timeline != "source":
        raise SystemExit(f"unknown review timeline: {timeline}")

    bundle = build_evidence_bundle(
        vlm_analysis,
        asr_result,
        narration,
        timeline=timeline,
        research=_load_review_research_context(work_dir),
        warnings=warnings,
    )
    bundle_fp = _bundle_fingerprint(bundle)
    fp_warning = _bundle_fingerprint_warning(bundle)
    if fp_warning:
        _append_warning_once(warnings, fp_warning)
        _append_warning_once(bundle.setdefault("warnings", []), fp_warning)
    chunk_reviews = []
    chunks = _chunk_evidence_bundle(bundle)
    for chunk in chunks:
        messages = build_review_messages(
            narration,
            vlm_analysis,
            asr_result,
            work_dir=work_dir,
            evidence_bundle=chunk,
        )
        resp = api_call(
            {
                "model": CONFIG.get("vlm_model", ""),
                "messages": messages,
                "max_tokens": 2000 if len(chunks) == 1 else 1600,
                "temperature": 0,
                "seed": 7 + int(chunk.get("chunk_index", 0)),
            }
        )
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
        json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (work_dir / "narration_review.md").write_text(
        format_review_md(review), encoding="utf-8"
    )
    n_err = sum(1 for f in review["findings"] if f["severity"] == "error")
    log(
        f"解说评审完成: {review['verdict']} | {len(review['findings'])} 条意见（error {n_err}）"
    )
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
    legacy direct single-pass path writes a SOURCE-time narration.json alongside a separate
    output-time narration_mapped.json, so its presence pins us back to source."""
    work_dir = Path(work_dir)
    manifest = work_dir / "recap_run_manifest.json"
    if manifest.exists():
        try:
            mode = (
                json.loads(manifest.read_text(encoding="utf-8"))
                .get("settings", {})
                .get("edit_mode")
            )
        except (ValueError, OSError):
            mode = None
        if mode == "cut":
            return "cut_output"
        if mode:  # "full" or any non-cut mode → narration.json is source time
            return "source"
    has_cut = (work_dir / "clip_plan_validated.json").exists() and (
        work_dir / "edited_source.mp4"
    ).exists()
    if has_cut and not (work_dir / "narration_mapped.json").exists():
        return "cut_output"
    return "source"


def main():
    ap = argparse.ArgumentParser(
        description="Review an agent-written narration.json for quality (LLM-as-judge)."
    )
    ap.add_argument("--work-dir", required=True)
    ap.add_argument(
        "--timeline",
        choices=["source", "cut_output"],
        default=None,
        help="grounding timeline for narration.json; DEFAULT auto-detects cut_output when a "
        "validated cut (clip_plan_validated.json + edited_source.mp4) is present, else source. "
        "cut_output remaps source VLM/ASR to the cut output timeline via clip_plan_validated.json",
    )
    ap.add_argument(
        "--strict-evidence",
        action="store_true",
        help="block instead of advisory fail-open when required cut-output evidence mapping is missing/stale",
    )
    args = ap.parse_args()
    timeline = args.timeline or _auto_timeline(args.work_dir)
    if args.timeline is None and timeline != "source":
        log(f"评审 grounding 时间轴自动判定为 {timeline}（检测到已校验的剪辑产物）")
    review = review_narration(
        args.work_dir, timeline=timeline, strict_evidence=args.strict_evidence
    )
    print(
        json.dumps(
            {
                "status": "reviewed",
                "verdict": review["verdict"],
                "findings": len(review["findings"]),
                "review": str(Path(args.work_dir) / "narration_review.md"),
            },
            ensure_ascii=False,
        )
    )
