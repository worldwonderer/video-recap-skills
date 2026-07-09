#!/usr/bin/env python3
"""Final post-render QC and golden-eval reports for video-recap.

Local deterministic/report-only checks only: no network, no repair, no secrets.
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import qc_contract

FINAL_QC_ARTIFACT = "final_qc.json"
GOLDEN_EVAL_ARTIFACT = "golden_eval.json"
POST_RENDER_STAGE = "post_render"
GOLDEN_STAGE = "golden"
_COLLECT_ARTIFACTS = (
    "assembly_manifest.json",
    "assembly_qc.json",
    "visual_qc.json",
    "preflight_qc.json",
    "mimo_qc.json",
)
ProbeRunner = Callable[[Path], Mapping[str, Any]]


def redact_secrets(value: Any) -> Any:
    """Return a JSON-safe copy with likely credentials removed."""
    return qc_contract.redact_secrets(value)


def safe_load_json(path_or_value: str | Path | Mapping[str, Any] | Sequence[Any] | None) -> Any:
    """Safely load JSON from a path or return a redacted JSON-like fixture value."""
    if path_or_value is None:
        return None
    if isinstance(path_or_value, (Mapping, list, tuple)):
        return redact_secrets(path_or_value)
    path = Path(path_or_value)
    try:
        return redact_secrets(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


def fingerprint_file(path: str | Path) -> str | None:
    path = Path(path)
    try:
        if path.exists() and path.is_file():
            return qc_contract.artifact_fingerprint(path)
    except OSError:
        return None
    return None


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _resolve_in_work_dir(work_dir: Path, path: str | Path | None) -> Path | None:
    if path is None or str(path) == "":
        return None
    p = Path(path)
    return p if p.is_absolute() else work_dir / p


def _candidate_final_outputs(work_dir: Path, final_output: str | Path | None) -> list[Path]:
    candidates: list[Path] = []
    explicit = _resolve_in_work_dir(work_dir, final_output)
    if explicit is not None:
        candidates.append(explicit)
    manifest = safe_load_json(work_dir / "assembly_manifest.json")
    if isinstance(manifest, Mapping):
        for key in ("final_output", "output", "output_path", "video_path"):
            if manifest.get(key):
                p = _resolve_in_work_dir(work_dir, str(manifest[key]))
                if p is not None:
                    candidates.append(p)
    for name in ("output.mp4", "recap.mp4", "final.mp4"):
        candidates.append(work_dir / name)
    deduped: list[Path] = []
    seen: set[str] = set()
    for p in candidates:
        key = str(p)
        if key not in seen:
            seen.add(key)
            deduped.append(p)
    return deduped


def _select_final_output(work_dir: Path, final_output: str | Path | None) -> Path | None:
    candidates = _candidate_final_outputs(work_dir, final_output)
    if final_output is not None:
        return candidates[0] if candidates else _resolve_in_work_dir(work_dir, final_output)
    for p in candidates:
        try:
            if p.exists() and p.is_file() and p.stat().st_size > 0:
                return p
        except OSError:
            continue
    return candidates[0] if candidates else None


def _file_metadata(path: Path | None, work_dir: Path | None = None) -> dict[str, Any]:
    if path is None:
        return {"path": None, "exists": False, "bytes": 0, "fingerprint": None}
    display = str(path)
    if work_dir is not None:
        try:
            display = path.relative_to(work_dir).as_posix()
        except ValueError:
            pass
    item: dict[str, Any] = {"path": display}
    try:
        exists = path.exists() and path.is_file()
        item["exists"] = exists
        item["bytes"] = path.stat().st_size if exists else 0
        item["fingerprint"] = fingerprint_file(path) if exists else None
    except OSError as exc:
        item.update({"exists": False, "bytes": 0, "fingerprint": None, "error": str(exc)})
    return redact_secrets(item)


def _artifact_summary(work_dir: Path, name: str) -> dict[str, Any]:
    path = work_dir / name
    meta = _file_metadata(path, work_dir)
    if meta.get("exists") and path.suffix.lower() == ".json":
        data = safe_load_json(path)
        if isinstance(data, Mapping):
            meta["summary"] = {
                "schema_version": data.get("schema_version"),
                "artifact": data.get("artifact"),
                "stage": data.get("stage"),
                "ok": data.get("ok"),
                "blocker_count": data.get("blocker_count"),
                "finding_count": data.get("finding_count"),
            }
        elif data is not None:
            meta["summary"] = {"type": type(data).__name__}
    return redact_secrets(meta)


def _artifact_fingerprints(*paths: Path | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in paths:
        if path is None:
            continue
        fp = fingerprint_file(path)
        if fp:
            out[path.name] = fp
    return out


def _finding(*, finding_id: str, code: str, message: str, category: str = "schema_invalid",
             stage: str = POST_RENDER_STAGE, source: Mapping[str, Any] | None = None,
             evidence: Mapping[str, Any] | None = None, fingerprints: Mapping[str, Any] | None = None,
             next_action: str = "manual_review") -> dict[str, Any]:
    return qc_contract.build_finding(
        finding_id=finding_id,
        stage=stage,
        severity="blocker",
        confidence="objective",
        sample_policy={"type": "deterministic"},
        category=category,
        code=code,
        message=message,
        deterministic=True,
        blocking=True,
        source=redact_secrets(source or {}),
        evidence=redact_secrets(evidence or {}),
        artifact_fingerprints=redact_secrets(fingerprints or {}),
        next_action=next_action,
        model_used="local_deterministic_final_qc_v1",
    )


def _run_ffprobe(path: Path) -> Mapping[str, Any]:
    if shutil.which("ffprobe") is None:
        raise RuntimeError("ffprobe unavailable")
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        detail = (res.stderr or res.stdout or "ffprobe failed").strip()
        raise RuntimeError(detail)
    try:
        data = json.loads(res.stdout or "{}")
    except ValueError as exc:
        raise RuntimeError(f"ffprobe returned invalid JSON: {exc}") from exc
    if not isinstance(data, Mapping):
        raise RuntimeError("ffprobe JSON must be an object")
    return data


def _probe_metadata(path: Path, *, probe_fixture: Any = None, probe_runner: ProbeRunner | None = None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return (probe, error). Existing non-empty media gets deterministic error on failure."""
    if probe_fixture is not None:
        data = safe_load_json(probe_fixture)
        if isinstance(data, Mapping):
            return redact_secrets(dict(data)), None
        return None, {"code": "probe_failed", "message": "probe_fixture is missing or invalid JSON"}
    try:
        runner = probe_runner or _run_ffprobe
        data = runner(path)
        if not isinstance(data, Mapping):
            raise RuntimeError("probe_runner must return an object")
        return redact_secrets(dict(data)), None
    except Exception as exc:  # deterministic report-only blocker, no crash
        return None, {"code": "probe_failed", "message": str(exc) or "ffprobe failed"}


def _first_video_stream(probe: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if not isinstance(probe, Mapping):
        return None
    streams = probe.get("streams")
    if not isinstance(streams, list):
        return None
    for stream in streams:
        if isinstance(stream, Mapping) and stream.get("codec_type") == "video":
            return stream
    return None


def _parse_positive_finite_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        if "/" in value:
            numerator, denominator = value.split("/", 1)
            try:
                den = float(denominator)
                if den == 0:
                    return None
                number = float(numerator) / den
            except (TypeError, ValueError):
                return None
        else:
            try:
                number = float(value)
            except (TypeError, ValueError):
                return None
    else:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
    if math.isfinite(number) and number > 0:
        return number
    return None


def _probe_duration_status(probe: Mapping[str, Any] | None) -> tuple[float | None, str | None, Any]:
    if not isinstance(probe, Mapping):
        return None, "missing_duration", None
    candidates: list[Any] = []
    fmt = probe.get("format")
    if isinstance(fmt, Mapping):
        candidates.append(fmt.get("duration"))
    candidates.append(probe.get("duration"))
    seen = [value for value in candidates if value not in (None, "")]
    if not seen:
        return None, "missing_duration", None
    for value in seen:
        duration = _parse_positive_finite_number(value)
        if duration is not None:
            return duration, None, value
    return None, "invalid_duration", seen[0]


def _probe_fps_status(video_stream: Mapping[str, Any] | None, probe: Mapping[str, Any] | None) -> tuple[float | None, str | None, Any]:
    candidates: list[Any] = []
    if isinstance(video_stream, Mapping):
        for key in ("avg_frame_rate", "r_frame_rate", "fps", "frame_rate"):
            candidates.append(video_stream.get(key))
    if isinstance(probe, Mapping):
        for key in ("fps", "frame_rate"):
            candidates.append(probe.get(key))
    seen = [value for value in candidates if value not in (None, "")]
    if not seen:
        return None, "missing_fps", None
    for value in seen:
        fps = _parse_positive_finite_number(value)
        if fps is not None:
            return fps, None, value
    return None, "invalid_fps", seen[0]

def _duration_from_probe(probe: Mapping[str, Any] | None) -> float | None:
    if not isinstance(probe, Mapping):
        return None
    candidates: list[Any] = []
    fmt = probe.get("format")
    if isinstance(fmt, Mapping):
        candidates.append(fmt.get("duration"))
    candidates.append(probe.get("duration"))
    for value in candidates:
        try:
            duration = float(value)
        except (TypeError, ValueError):
            continue
        if duration >= 0:
            return duration
    return None


def _codec_from_probe(probe: Mapping[str, Any] | None) -> str | None:
    vs = _first_video_stream(probe)
    name = vs.get("codec_name") if isinstance(vs, Mapping) else None
    return str(name) if name else None



def _probe_contract_findings(probe: Mapping[str, Any] | None, *, final_meta: Mapping[str, Any], fingerprints: Mapping[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    video_stream = _first_video_stream(probe)
    if video_stream is None:
        findings.append(_finding(
            finding_id="final-qc-missing-video-stream",
            code="missing_video_stream",
            message="final output probe metadata has no video stream",
            category="stream",
            source={"artifact": final_meta.get("path")},
            evidence={"streams": probe.get("streams") if isinstance(probe, Mapping) else None},
            fingerprints=fingerprints,
            next_action="rerender_final_output_with_video_stream",
        ))

    _duration, duration_code, duration_value = _probe_duration_status(probe)
    if duration_code is not None:
        findings.append(_finding(
            finding_id=f"final-qc-{duration_code.replace('_', '-')}",
            code=duration_code,
            message="final output probe metadata is missing a positive finite duration" if duration_code == "missing_duration" else "final output probe metadata duration is not positive and finite",
            category="duration",
            source={"artifact": final_meta.get("path")},
            evidence={"duration": duration_value},
            fingerprints=fingerprints,
            next_action="rerender_final_output_with_valid_duration",
        ))

    codec = str(video_stream.get("codec_name")).strip() if isinstance(video_stream, Mapping) and video_stream.get("codec_name") is not None else ""
    if not codec:
        findings.append(_finding(
            finding_id="final-qc-missing-codec",
            code="missing_codec",
            message="final output probe metadata is missing a video codec",
            category="stream",
            source={"artifact": final_meta.get("path")},
            evidence={"video_stream": video_stream},
            fingerprints=fingerprints,
            next_action="rerender_final_output_with_video_codec",
        ))

    _fps, fps_code, fps_value = _probe_fps_status(video_stream, probe)
    if fps_code is not None:
        findings.append(_finding(
            finding_id=f"final-qc-{fps_code.replace('_', '-')}",
            code=fps_code,
            message="final output probe metadata is missing a positive finite video fps" if fps_code == "missing_fps" else "final output probe metadata video fps is not positive and finite",
            category="stream",
            source={"artifact": final_meta.get("path")},
            evidence={"fps": fps_value, "video_stream": video_stream},
            fingerprints=fingerprints,
            next_action="rerender_final_output_with_valid_fps",
        ))
    return findings


def _blocking_codes(value: Any) -> list[str]:
    if isinstance(value, str):
        values: Sequence[Any] = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        values = value
    else:
        return []
    codes: list[str] = []
    for item in values:
        code = str(item).strip()
        if code:
            codes.append(code)
    return codes


def _failing_verdict(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    verdict = value.strip().lower()
    return verdict in {"fail", "failed", "failing", "blocker", "blocked", "error", "errored", "reject", "rejected"}


def _upstream_blockers(work_dir: Path, artifact_name: str) -> list[dict[str, Any]]:
    path = work_dir / artifact_name
    data = safe_load_json(path)
    if not isinstance(data, Mapping):
        return []
    findings = data.get("findings")
    blocker_count = data.get("blocker_count")
    ok = data.get("ok")
    blocked: list[Mapping[str, Any]] = []
    if isinstance(findings, list):
        blocked.extend(f for f in findings if isinstance(f, Mapping) and f.get("blocking") is True)
    elif ok is False or (isinstance(blocker_count, int) and blocker_count > 0):
        blocked.append({"code": "reported_blocker", "message": f"{artifact_name} reported blockers"})

    blocking_codes = _blocking_codes(data.get("blocking_codes"))
    if data.get("blocking") is True or _failing_verdict(data.get("verdict")):
        if not blocking_codes:
            blocking_codes = ["reported_blocker"]
        for code in blocking_codes:
            blocked.append({
                "code": code,
                "message": f"{artifact_name} reported {code}",
                "artifact": data.get("artifact"),
                "verdict": data.get("verdict"),
                "blocking": data.get("blocking"),
            })

    out = []
    fp = fingerprint_file(path)
    for idx, item in enumerate(blocked):
        code = str(item.get("code") or item.get("rule_id") or "reported_blocker")
        out.append(_finding(
            finding_id=f"final-qc-upstream-{artifact_name}-{idx}",
            code=f"upstream_{artifact_name.replace('.', '_')}_{code}",
            message=f"{artifact_name} has blocking finding: {item.get('message') or item.get('decision_reason') or code}",
            category="schema_invalid",
            source={"artifact": artifact_name, "finding_id": item.get("finding_id") or item.get("id")},
            evidence={
                "upstream_code": code,
                "upstream_message": item.get("message") or item.get("decision_reason"),
                "upstream_artifact": item.get("artifact"),
                "upstream_verdict": item.get("verdict"),
                "upstream_blocking": item.get("blocking"),
            },
            fingerprints={artifact_name: fp} if fp else {},
            next_action="fix_upstream_qc_blocker",
        ))
    return out


def collect_metadata(work_dir: str | Path, *, final_output: str | Path | None = None,
                     probe_fixture: Any = None, probe_runner: ProbeRunner | None = None) -> dict[str, Any]:
    root = Path(work_dir)
    selected = _select_final_output(root, final_output)
    probe = probe_error = None
    final_meta = _file_metadata(selected, root)
    if final_meta.get("exists") and final_meta.get("bytes", 0) > 0:
        probe, probe_error = _probe_metadata(selected, probe_fixture=probe_fixture, probe_runner=probe_runner)
    artifacts = {name: _artifact_summary(root, name) for name in _COLLECT_ARTIFACTS}
    metadata = {
        "work_dir": str(root),
        "final_output": final_meta,
        "final_output_candidates": [_file_metadata(p, root) for p in _candidate_final_outputs(root, final_output)],
        "artifacts": artifacts,
        "probe": probe,
        "probe_error": probe_error,
        # mimo_qc.json is advisory metadata only and is not rolled into final blockers.
        "auto_repair": False,
    }
    return redact_secrets(_json_safe(metadata))


def build_final_qc(work_dir: str | Path, final_output: str | Path | None = None,
                   probe_fixture: Any = None, probe_runner: ProbeRunner | None = None) -> dict[str, Any]:
    root = Path(work_dir)
    selected = _select_final_output(root, final_output)
    metadata = collect_metadata(root, final_output=final_output, probe_fixture=probe_fixture, probe_runner=probe_runner)
    final_meta = metadata["final_output"]
    findings: list[dict[str, Any]] = []
    fps = _artifact_fingerprints(selected)
    if not final_meta.get("exists"):
        findings.append(_finding(
            finding_id="final-qc-missing-final-output",
            code="missing_final_output",
            message="final output mp4 is missing",
            category="missing_artifact",
            source={"artifact": str(final_output) if final_output else "final_output"},
            evidence={"final_output": final_meta},
            fingerprints=fps,
            next_action="render_final_output",
        ))
    elif int(final_meta.get("bytes") or 0) <= 0:
        findings.append(_finding(
            finding_id="final-qc-empty-final-output",
            code="empty_final_output",
            message="final output mp4 is empty",
            category="missing_artifact",
            source={"artifact": final_meta.get("path")},
            evidence={"final_output": final_meta},
            fingerprints=fps,
            next_action="rerender_final_output",
        ))
    else:
        probe_error = metadata.get("probe_error")
        if isinstance(probe_error, Mapping):
            findings.append(_finding(
                finding_id="final-qc-probe-failed",
                code="probe_failed",
                message="ffprobe failed or was unavailable for existing non-empty final output",
                category="stream",
                source={"artifact": final_meta.get("path")},
                evidence=probe_error,
                fingerprints=fps,
                next_action="inspect_or_rerender_final_output",
            ))
        else:
            probe = metadata.get("probe")
            findings.extend(_probe_contract_findings(
                probe if isinstance(probe, Mapping) else None,
                final_meta=final_meta,
                fingerprints=fps,
            ))
    findings.extend(_upstream_blockers(root, "assembly_qc.json"))
    findings.extend(_upstream_blockers(root, "visual_qc.json"))
    report = qc_contract.build_report(
        artifact=FINAL_QC_ARTIFACT,
        stage=POST_RENDER_STAGE,
        findings=findings,
        metadata=metadata,
    )
    qc_contract.validate_report(report)
    return report


def _load_or_build_final_qc(work_dir: Path, final_qc_report: Mapping[str, Any] | None) -> dict[str, Any]:
    if final_qc_report is not None:
        report = dict(redact_secrets(final_qc_report))
    else:
        existing = safe_load_json(work_dir / FINAL_QC_ARTIFACT)
        report = dict(existing) if isinstance(existing, Mapping) else build_final_qc(work_dir)
    qc_contract.validate_report(report)
    return report


def build_golden_eval(work_dir: str | Path, final_qc_report: Mapping[str, Any] | None = None,
                      golden_fixture: Any = None) -> dict[str, Any]:
    root = Path(work_dir)
    final_report = _load_or_build_final_qc(root, final_qc_report)
    fixture = safe_load_json(golden_fixture)
    fixture = fixture if isinstance(fixture, Mapping) else {}
    metadata = {
        "work_dir": str(root),
        "fixture": fixture,
        "final_qc": {
            "ok": final_report.get("ok"),
            "blocker_count": final_report.get("blocker_count"),
            "artifact": final_report.get("artifact"),
            "stage": final_report.get("stage"),
        },
        "final_qc_fingerprint": fingerprint_file(root / FINAL_QC_ARTIFACT),
        "auto_repair": False,
    }
    findings: list[dict[str, Any]] = []
    expected_ok = fixture.get("expected_final_qc_ok", True)
    if isinstance(expected_ok, bool) and bool(final_report.get("ok")) != expected_ok:
        findings.append(_finding(
            finding_id="golden-final-qc-ok-mismatch",
            stage=GOLDEN_STAGE,
            code="expected_final_qc_ok_mismatch",
            message="final_qc ok state does not match golden expectation",
            category="schema_invalid",
            source={"artifact": FINAL_QC_ARTIFACT},
            evidence={"expected": expected_ok, "actual": final_report.get("ok")},
            fingerprints={FINAL_QC_ARTIFACT: metadata["final_qc_fingerprint"]} if metadata["final_qc_fingerprint"] else {},
            next_action="fix_final_qc_blockers",
        ))
    final_meta = {}
    probe = None
    if isinstance(final_report.get("metadata"), Mapping):
        final_meta = final_report["metadata"].get("final_output") or {}
        probe = final_report["metadata"].get("probe")
    duration = _duration_from_probe(probe)
    codec = _codec_from_probe(probe)
    if fixture.get("min_duration") is not None and (duration is None or duration < float(fixture["min_duration"])):
        findings.append(_finding(
            finding_id="golden-min-duration-mismatch",
            stage=GOLDEN_STAGE,
            code="min_duration_mismatch",
            message="final output duration is below golden minimum",
            category="duration",
            source={"artifact": final_meta.get("path")},
            evidence={"expected_min_duration": fixture.get("min_duration"), "actual_duration": duration},
            next_action="adjust_render_duration",
        ))
    if fixture.get("max_duration") is not None and (duration is None or duration > float(fixture["max_duration"])):
        findings.append(_finding(
            finding_id="golden-max-duration-mismatch",
            stage=GOLDEN_STAGE,
            code="max_duration_mismatch",
            message="final output duration is above golden maximum",
            category="duration",
            source={"artifact": final_meta.get("path")},
            evidence={"expected_max_duration": fixture.get("max_duration"), "actual_duration": duration},
            next_action="adjust_render_duration",
        ))
    expected_codec = fixture.get("expected_codec") or fixture.get("expected_video_codec")
    if expected_codec is not None and codec != str(expected_codec):
        findings.append(_finding(
            finding_id="golden-codec-mismatch",
            stage=GOLDEN_STAGE,
            code="codec_mismatch",
            message="final output video codec does not match golden expectation",
            category="stream",
            source={"artifact": final_meta.get("path")},
            evidence={"expected_codec": expected_codec, "actual_codec": codec},
            next_action="adjust_render_codec",
        ))
    required = fixture.get("required_artifacts") or []
    if isinstance(required, Sequence) and not isinstance(required, (str, bytes)):
        for idx, name in enumerate(required):
            artifact_path = root / str(name)
            if not artifact_path.exists() or not artifact_path.is_file() or artifact_path.stat().st_size <= 0:
                findings.append(_finding(
                    finding_id=f"golden-required-artifact-missing-{idx}",
                    stage=GOLDEN_STAGE,
                    code="required_artifact_missing",
                    message="golden fixture requires an artifact that is missing or empty",
                    category="missing_artifact",
                    source={"artifact": str(name)},
                    evidence={"required_artifact": str(name)},
                    next_action="produce_required_artifact",
                ))
    metadata["observed"] = {"duration": duration, "codec": codec, "final_output": final_meta}
    report = qc_contract.build_report(
        artifact=GOLDEN_EVAL_ARTIFACT,
        stage=GOLDEN_STAGE,
        findings=findings,
        metadata=redact_secrets(_json_safe(metadata)),
    )
    qc_contract.validate_report(report)
    return report


def _write_report(path: Path, report: Mapping[str, Any]) -> None:
    qc_contract.validate_report(report)
    path.write_text(json.dumps(redact_secrets(report), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    qc_contract.validate_report(json.loads(path.read_text(encoding="utf-8")))


def run(work_dir: str | Path, final_output: str | Path | None = None,
        probe_fixture: Any = None, golden_fixture: Any = None,
        probe_runner: ProbeRunner | None = None, only: str | None = None) -> dict[str, Any]:
    root = Path(work_dir)
    root.mkdir(parents=True, exist_ok=True)
    mode = {"final": "final_qc", "golden": "golden_eval"}.get(only or "all", only or "all")
    if mode not in {"all", "final_qc", "golden_eval"}:
        raise ValueError("only must be one of all, final_qc, golden_eval, final, golden")
    result: dict[str, Any] = {"work_dir": str(root), "written": []}
    final_report: dict[str, Any] | None = None
    if mode in {"all", "final_qc"}:
        final_report = build_final_qc(root, final_output=final_output, probe_fixture=probe_fixture, probe_runner=probe_runner)
        _write_report(root / FINAL_QC_ARTIFACT, final_report)
        result.update({"final_qc": {"ok": final_report["ok"], "blocker_count": final_report["blocker_count"]}})
        result["written"].append(FINAL_QC_ARTIFACT)
    if mode in {"all", "golden_eval"}:
        golden_report = build_golden_eval(root, final_qc_report=final_report, golden_fixture=golden_fixture)
        _write_report(root / GOLDEN_EVAL_ARTIFACT, golden_report)
        result.update({"golden_eval": {"ok": golden_report["ok"], "blocker_count": golden_report["blocker_count"]}})
        result["written"].append(GOLDEN_EVAL_ARTIFACT)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Write final_qc.json and golden_eval.json for a video-recap work_dir.")
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--final-output", default=None)
    ap.add_argument("--probe-fixture", default=None)
    ap.add_argument("--golden-fixture", default=None)
    ap.add_argument("--only", choices=["all", "final_qc", "golden_eval", "final", "golden"], default="all")
    args = ap.parse_args(argv)
    summary = run(
        args.work_dir,
        final_output=args.final_output,
        probe_fixture=args.probe_fixture,
        golden_fixture=args.golden_fixture,
        only=args.only,
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
