"""Write shift-left, MiMo, and final QC stage reports."""

import json


from pathlib import Path

import qc_contract

import final_qc

import mimo_qc


from recap_runtime import _load_json

ASSEMBLY_MANIFEST = "assembly_manifest.json"


def _json_safe(value):
    """Return a JSON-serializable, secret-redacted copy for local QC metadata."""

    def convert(item):
        if isinstance(item, Path):
            return str(item)
        if isinstance(item, dict):
            return {str(k): convert(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [convert(v) for v in item]
        return item

    return qc_contract.redact_secrets(convert(value))


def _load_preflight_stage_reports(work_dir):
    path = Path(work_dir) / "preflight_qc.json"
    if not path.exists():
        return {}
    data = _load_json(path)
    if not isinstance(data, dict):
        raise qc_contract.QCContractError("preflight_qc.json must be a JSON object")
    qc_contract.validate_report(data)
    stages = None
    metadata = data.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("stages"), dict):
        stages = metadata.get("stages")
    elif isinstance(data.get("stages"), dict):
        stages = data.get("stages")
    if stages is None:
        return {data["stage"]: data}
    stage_reports = {}
    for stage, report in stages.items():
        if not isinstance(report, dict):
            raise qc_contract.QCContractError(
                f"preflight stage report must be an object: {stage}"
            )
        qc_contract.validate_report(report)
        if report.get("stage") != stage:
            raise qc_contract.QCContractError(
                f"preflight stage key does not match report stage: {stage}"
            )
        stage_reports[stage] = report
    return stage_reports


def _write_shift_left_stage_qc(work_dir, stage, metadata=None, findings=None):
    """Write/roll up local shift-left QC for one pipeline stage.

    This is a local contract artifact only: no MiMo/deep eval calls, no repair, and
    no credential persistence. Any validation/write failure is allowed to raise.
    """
    work_dir = Path(work_dir)
    path = work_dir / "preflight_qc.json"
    stage_report = qc_contract.build_report(
        artifact="preflight_qc.json",
        stage=stage,
        findings=findings or [],
        metadata=_json_safe(metadata or {}),
    )
    stage_reports = _load_preflight_stage_reports(work_dir)
    stage_reports[stage] = stage_report
    aggregate_findings = []
    for report in stage_reports.values():
        aggregate_findings.extend(dict(f) for f in report.get("findings", []))
    top_metadata = dict(stage_report.get("metadata") or {})
    top_metadata["latest_stage"] = stage
    top_metadata["stages"] = stage_reports
    top_report = qc_contract.build_report(
        artifact="preflight_qc.json",
        stage=stage,
        findings=aggregate_findings,
        metadata=_json_safe(top_metadata),
    )
    qc_contract.validate_report(top_report)
    path.write_text(
        json.dumps(top_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    qc_contract.validate_report(_load_json(path))
    return top_report


def _tts_qc_metadata(work_dir):
    work_dir = Path(work_dir)
    metadata = {}
    tts_meta = _load_json(work_dir / "tts_meta.json")
    if tts_meta is not None:
        metadata["tts_meta"] = tts_meta
    tts_dir = work_dir / "tts_segments"
    if tts_dir.exists() and tts_dir.is_dir():
        metadata["tts_segments"] = [
            p.relative_to(work_dir).as_posix()
            for p in sorted(tts_dir.iterdir())
            if p.is_file()
        ]
    return metadata


def _post_render_qc_metadata(work_dir, final_output):
    work_dir = Path(work_dir)
    metadata = {"final_output": str(final_output) if final_output is not None else None}
    assembly_manifest = _load_json(work_dir / ASSEMBLY_MANIFEST)
    if assembly_manifest is not None:
        metadata["assembly_manifest"] = assembly_manifest
    return metadata


def _write_final_qc_reports(work_dir, final_output):
    """Write report-only final QC artifacts after render.

    final_qc.run converts ffprobe unavailability/failure into deterministic
    blockers; only unexpected schema/write errors propagate.
    """
    return final_qc.run(work_dir, final_output=final_output)


def _print_final_qc_pointer(result):
    """Surface a report-only final_qc/golden_eval FAIL so the shift-left QC is
    not a silent no-op. Advisory only: it never changes the exit status."""
    if not isinstance(result, dict):
        return
    problems = []
    for key in ("final_qc", "golden_eval"):
        section = result.get(key)
        if isinstance(section, dict) and section.get("ok") is False:
            problems.append(f"{key} blocker_count={section.get('blocker_count', '?')}")
    if problems:
        print(
            "[video-recap] ⚠️  最终 QC 未通过（仅报告，不阻断）: "
            + "; ".join(problems)
            + "；详见 final_qc.json / golden_eval.json"
        )


def _mimo_qc_stage_enabled(args, stage):
    mode = getattr(args, "mimo_qc", "off") or "off"
    return (
        mode == "both"
        or (mode == "pre-assemble" and stage == "pre_assemble")
        or (mode == "post-render" and stage == "post_render")
    )


def _prepare_mimo_qc(work_dir, args):
    """Remove an old advisory artifact when this run has MiMo QC disabled."""
    if getattr(args, "mimo_qc", "off") == "off":
        mimo_qc.clear_report(work_dir)


def _print_mimo_qc_pointer(result, stage):
    report = result.get("report", {}) if isinstance(result, dict) else {}
    metadata = report.get("metadata", {}) if isinstance(report, dict) else {}
    status = metadata.get("status", "unknown")
    path = (
        result.get("path", "mimo_qc.json")
        if isinstance(result, dict)
        else "mimo_qc.json"
    )
    stage_findings = [
        finding
        for finding in report.get("findings", [])
        if isinstance(finding, dict) and finding.get("stage") == stage
    ]
    if status in {"failed", "unavailable"}:
        reason = metadata.get("error") or "unavailable"
        print(
            f"[video-recap] ⚠ MiMo QC {stage}: {status} ({reason})；建议性检查不可用，继续流水线"
        )
        return
    print(
        f"[video-recap] ℹ MiMo QC {stage}: {status}, {len(stage_findings)} 条建议；详见 {path}"
    )
    for finding in stage_findings[:5]:
        print(
            f"[video-recap]   - {finding.get('message') or finding.get('decision_reason')}"
        )


def _run_mimo_qc_stage(work_dir, args, stage, *, final_output=None):
    """Run one selected advisory stage and never propagate a failure."""
    if not _mimo_qc_stage_enabled(args, stage):
        return None
    try:
        result = mimo_qc.run(
            work_dir,
            stage=stage,
            live=True,
            refresh=bool(getattr(args, "mimo_qc_refresh", False)),
            final_output=final_output,
        )
    except Exception as exc:
        print(
            f"[video-recap] ⚠ MiMo QC {stage}: {type(exc).__name__}；"
            "建议性检查失败，继续流水线"
        )
        return None
    _print_mimo_qc_pointer(result, stage)
    return result
