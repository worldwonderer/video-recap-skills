"""Sample frames and build aggregate MiMo QC reports."""

from __future__ import annotations


import base64

import hashlib

import importlib.util

import json

import math

import os


import subprocess

import tempfile

from pathlib import Path

from typing import Any, Callable, Mapping, Sequence

import qc_contract

from mimo_qc_evidence import (
    _cache_evidence,
    _effective_config,
    _existing_final_output,
    _fingerprint_value,
    _redact,
    collect_evidence,
    safe_mimo_config,
)
from mimo_qc_observations import normalize_observations
from mimo_qc_payload import _request_payload, _validated_live_output, build_payload

_LOCAL_LIB_PATH = Path(__file__).with_name("lib.py")

_LOCAL_LIB_SPEC = importlib.util.spec_from_file_location(
    "video_recap_mimo_qc_lib", _LOCAL_LIB_PATH
)

if (
    _LOCAL_LIB_SPEC is None or _LOCAL_LIB_SPEC.loader is None
):  # pragma: no cover - import invariant
    raise ImportError(f"cannot load local MiMo QC client: {_LOCAL_LIB_PATH}")

_LOCAL_LIB = importlib.util.module_from_spec(_LOCAL_LIB_SPEC)

_LOCAL_LIB_SPEC.loader.exec_module(_LOCAL_LIB)

mimo_qc_api_call = _LOCAL_LIB.mimo_qc_api_call

ARTIFACT_NAME = "mimo_qc.json"

DEFAULT_STAGE = "pre_assemble"

MAX_FRAMES = 6

MAX_FRAME_DIMENSION = 768

FRAME_SAMPLER_VERSION = 2

JudgeCallable = Callable[
    [Mapping[str, Any]], Mapping[str, Any] | Sequence[Mapping[str, Any]]
]

FrameSampler = Callable[..., Sequence[Mapping[str, Any]]]


def _probe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if result.returncode != 0:
        return 0.0
    try:
        return max(0.0, float(json.loads(result.stdout)["format"]["duration"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return 0.0


def sample_video_frames(
    video_path: str | Path,
    *,
    max_frames: int = MAX_FRAMES,
    max_dimension: int = MAX_FRAME_DIMENSION,
) -> list[dict[str, Any]]:
    """Extract at most six bounded JPEGs; returned base64 is request-only."""
    path = Path(video_path)
    if not path.is_file() or max_frames <= 0:
        return []
    try:
        duration = _probe_duration(path)
    except (OSError, subprocess.SubprocessError):
        return []
    if duration <= 0:
        return []
    count = min(int(max_frames), max(1, int(math.ceil(duration / 15.0))))
    samples = []
    with tempfile.TemporaryDirectory(prefix="video-recap-mimo-qc-") as tmp:
        for index in range(count):
            timestamp = duration * (index + 0.5) / count
            destination = Path(tmp) / f"frame-{index:02d}.jpg"
            try:
                result = subprocess.run(
                    [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-ss",
                        f"{timestamp:.3f}",
                        "-i",
                        str(path),
                        "-frames:v",
                        "1",
                        "-vf",
                        (
                            f"scale={int(max_dimension)}:{int(max_dimension)}:"
                            "force_original_aspect_ratio=decrease"
                        ),
                        "-q:v",
                        "4",
                        str(destination),
                    ],
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if result.returncode != 0 or not destination.is_file():
                continue
            raw = destination.read_bytes()
            samples.append(
                {
                    "data_url": "data:image/jpeg;base64,"
                    + base64.b64encode(raw).decode("ascii"),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "timestamp": round(timestamp, 3),
                }
            )
    return samples[:MAX_FRAMES]


def _frame_metadata(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "count": min(len(samples), MAX_FRAMES),
        "max_frames": MAX_FRAMES,
        "max_dimension": MAX_FRAME_DIMENSION,
        "sampler_version": FRAME_SAMPLER_VERSION,
        "samples": [
            {
                "sha256": sample.get("sha256"),
                "timestamp": sample.get("timestamp"),
            }
            for sample in list(samples)[:MAX_FRAMES]
        ],
    }


def _cache_input(
    stage: str,
    payload: Mapping[str, Any],
    evidence: Mapping[str, Any],
    frames: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "stage": stage,
        "model": payload.get("model"),
        "payload_fingerprint": payload.get("payload_fingerprint"),
        "evidence": _cache_evidence(evidence),
        "frames": frames,
        "contract": qc_contract.SCHEMA_VERSION,
    }


def _stage_reports(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        qc_contract.validate_report(report)
    except (OSError, ValueError, TypeError, qc_contract.QCContractError):
        return {}
    stages = report.get("metadata", {}).get("stages")
    if isinstance(stages, Mapping):
        valid = {}
        for stage, stage_report in stages.items():
            try:
                qc_contract.validate_report(stage_report)
            except (TypeError, qc_contract.QCContractError):
                continue
            valid[str(stage)] = dict(stage_report)
        return valid
    return {str(report["stage"]): report}


def _error_name(exc: Exception) -> str:
    text = str(_redact(str(exc))).strip()
    return (text or type(exc).__name__)[:120]


def build_report(
    work_dir: str | Path,
    *,
    stage: str = DEFAULT_STAGE,
    fixture: Any | None = None,
    dry_run: bool = False,
    judge: JudgeCallable | None = None,
    config: Mapping[str, Any] | None = None,
    final_output: str | Path | None = None,
    live: bool = False,
    refresh: bool = False,
    frame_sampler: FrameSampler | None = None,
    existing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one validated stage report; all live failures remain successful QC."""
    root = Path(work_dir)
    evidence = collect_evidence(root, final_output=final_output)
    payload = build_payload(evidence, stage=stage, config=config)
    cfg = safe_mimo_config(config)
    samples: Sequence[Mapping[str, Any]] = []
    if stage == "post_render" and (fixture is not None or judge is not None or live):
        output = _existing_final_output(root, final_output)
        if output is not None:
            sampler = frame_sampler or sample_video_frames
            try:
                samples = list(
                    sampler(
                        output, max_frames=MAX_FRAMES, max_dimension=MAX_FRAME_DIMENSION
                    )
                )[:MAX_FRAMES]
            except Exception:
                samples = []
    frame_meta = _frame_metadata(samples)
    cache_input = _cache_input(stage, payload, evidence, frame_meta)
    cache_key = _fingerprint_value(cache_input)

    if (
        not refresh
        and existing
        and existing.get("metadata", {}).get("cache_key") == cache_key
    ):
        cached = json.loads(json.dumps(existing))
        cached["metadata"]["status"] = "cached"
        cached["metadata"]["mode"] = "live_cache"
        cached["metadata"]["request_count"] = 0
        qc_contract.validate_report(cached)
        return cached

    status = "completed"
    error = None
    if fixture is not None:
        model_output = fixture
        mode = "fixture"
    elif judge is not None and not dry_run:
        mode = "injected_judge"
        try:
            model_output = judge(payload)
        except Exception as exc:
            model_output = {"observations": []}
            status, error = "failed", _error_name(exc)
    elif live and not dry_run:
        mode = "live"
        if not cfg["key_present"]:
            model_output = {"observations": []}
            status, error = "unavailable", "missing_key"
        else:
            try:
                response = mimo_qc_api_call(
                    _request_payload(payload, samples),
                    config=_effective_config(config),
                    timeout=60,
                )
                model_output = _validated_live_output(response)
            except Exception as exc:
                model_output = {"observations": []}
                status, error = "failed", _error_name(exc)
    else:
        model_output = {"observations": []}
        mode = "dry_run"
        status = "dry_run"

    findings = normalize_observations(
        model_output, stage=stage, payload=payload, model_config=config
    )
    metadata = {
        "mode": mode,
        "status": status,
        "error": error,
        "report_only": True,
        "auto_repair": False,
        "pipeline_blocking": False,
        "deterministic": False,
        "request_count": 1
        if mode == "live" and status in {"completed", "failed"}
        else 0,
        "cache_key": cache_key,
        "cache_input": cache_input,
        "frame_samples": frame_meta,
        "evidence": evidence,
        "payload": payload,
        "config": cfg,
    }
    report = qc_contract.build_report(
        artifact=ARTIFACT_NAME,
        stage=stage,
        findings=findings,
        metadata=metadata,
    )
    qc_contract.validate_report(report)
    return _redact(report)


def _aggregate_reports(
    stage_reports: Mapping[str, Mapping[str, Any]], current_stage: str
) -> dict[str, Any]:
    current = stage_reports[current_stage]
    findings = [
        finding
        for stage_report in stage_reports.values()
        for finding in stage_report.get("findings", [])
    ]
    metadata = dict(current.get("metadata", {}))
    metadata["stages"] = {
        stage: dict(report) for stage, report in stage_reports.items()
    }
    return qc_contract.build_report(
        artifact=ARTIFACT_NAME,
        stage=current_stage,
        findings=findings,
        metadata=metadata,
    )


def write_report(
    work_dir: str | Path, report: Mapping[str, Any], *, output: str | Path | None = None
) -> tuple[Path, dict[str, Any]]:
    """Atomically merge one stage into mimo_qc.json and return the aggregate."""
    path = Path(output) if output else Path(work_dir) / ARTIFACT_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_stage = _redact(dict(report))
    qc_contract.validate_report(safe_stage)
    stages = _stage_reports(path)
    stages[safe_stage["stage"]] = safe_stage
    aggregate = _aggregate_reports(stages, safe_stage["stage"])
    qc_contract.validate_report(aggregate)
    serialized = json.dumps(_redact(aggregate), ensure_ascii=False, indent=2) + "\n"
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return path, aggregate
