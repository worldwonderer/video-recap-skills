"""Advisory MiMo multimodal QC for video-recap.

The feature is deliberately fail-open: it can add human-review observations,
but it cannot repair artifacts, stop rendering, or create a QC blocker. Live
network access is opt-in, bounded to one request per selected pipeline stage,
and cached by content rather than machine-specific paths.
"""
from __future__ import annotations

import argparse
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


# Load this skill's sibling client explicitly.  Several harnesses import more
# than one self-contained skill into the same interpreter, where their
# intentionally duplicated ``lib.py`` modules would otherwise collide in
# ``sys.modules`` under the bare name ``lib``.
_LOCAL_LIB_PATH = Path(__file__).with_name("lib.py")
_LOCAL_LIB_SPEC = importlib.util.spec_from_file_location("video_recap_mimo_qc_lib", _LOCAL_LIB_PATH)
if _LOCAL_LIB_SPEC is None or _LOCAL_LIB_SPEC.loader is None:  # pragma: no cover - import invariant
    raise ImportError(f"cannot load local MiMo QC client: {_LOCAL_LIB_PATH}")
_LOCAL_LIB = importlib.util.module_from_spec(_LOCAL_LIB_SPEC)
_LOCAL_LIB_SPEC.loader.exec_module(_LOCAL_LIB)
DEFAULT_CONFIG = _LOCAL_LIB.CONFIG
mimo_qc_api_call = _LOCAL_LIB.mimo_qc_api_call


ARTIFACT_NAME = "mimo_qc.json"
DEFAULT_STAGE = "pre_assemble"
MAX_OBSERVATIONS = 12
MAX_MESSAGE_CHARS = 800
MAX_FRAMES = 6
MAX_FRAME_DIMENSION = 768
FRAME_SAMPLER_VERSION = 1

_JSON_ARTIFACTS = (
    "narration.json",
    "visual_overlays.json",
    "clip_plan_validated.json",
    "clip_plan.json",
    "assembly_manifest.json",
    "tts_meta.json",
)
_ASR_OR_SUBTITLE_ARTIFACTS = (
    "asr.json",
    "asr_result.json",
    "asr_segments.json",
    "subtitles.json",
    "subtitle.json",
    "subtitles.srt",
    "subtitle.srt",
    "subtitles.vtt",
    "subtitle.vtt",
    "output.srt",
    "output.vtt",
)
_OPTIONAL_VISUAL_METADATA = (
    "sampled_frames.json",
    "frame_samples.json",
    "storyboard.json",
    "storyboard_meta.json",
    "frames_manifest.json",
)

JudgeCallable = Callable[[Mapping[str, Any]], Mapping[str, Any] | Sequence[Mapping[str, Any]]]
FrameSampler = Callable[..., Sequence[Mapping[str, Any]]]


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint_value(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _redact(value: Any) -> Any:
    return qc_contract.redact_secrets(value)


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return _redact(json.load(handle))


def _read_text_sample(path: Path, *, max_chars: int = 4000) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return {
        "kind": "text",
        "bytes": path.stat().st_size,
        "truncated": len(text) > max_chars,
        "sample": _redact(text[:max_chars]),
    }


def _summarize(value: Any, *, max_items: int = 8, max_string: int = 700, depth: int = 0) -> Any:
    """Keep request/report evidence bounded while retaining useful structure."""
    value = _redact(value)
    if depth >= 4:
        return {"type": type(value).__name__, "fingerprint": _fingerprint_value(value)}
    if isinstance(value, Mapping):
        out = {
            str(key): _summarize(item, max_items=max_items, max_string=max_string, depth=depth + 1)
            for key, item in list(value.items())[:max_items]
        }
        if len(value) > max_items:
            out["_omitted_keys"] = len(value) - max_items
        return out
    if isinstance(value, list):
        return {
            "count": len(value),
            "items": [
                _summarize(item, max_items=max_items, max_string=max_string, depth=depth + 1)
                for item in value[:max_items]
            ],
            "omitted": max(0, len(value) - max_items),
        }
    if isinstance(value, str) and len(value) > max_string:
        return {"text": value[:max_string], "truncated": True, "chars": len(value)}
    return value


def _collect_file(work_dir: Path, name: str) -> dict[str, Any] | None:
    path = work_dir / name
    if not path.is_file():
        return None
    try:
        if path.suffix.lower() == ".json":
            summary = _summarize(_load_json(path))
            kind = "json"
        else:
            summary = _summarize(_read_text_sample(path))
            kind = "text"
        return {
            "path": name,
            "kind": kind,
            "bytes": path.stat().st_size,
            "fingerprint": qc_contract.artifact_fingerprint(path),
            "summary": summary,
        }
    except Exception as exc:  # evidence collection is advisory too
        return {"path": name, "kind": "unreadable", "error": type(exc).__name__}


def _first_existing(work_dir: Path, names: Sequence[str]) -> str | None:
    return next((name for name in names if (work_dir / name).is_file()), None)


def _resolve_candidate(work_dir: Path, candidate: str | Path) -> Path:
    path = Path(candidate)
    return path if path.is_absolute() else work_dir / path


def _final_output_candidates(work_dir: Path, final_output: str | Path | None) -> list[tuple[Path, str]]:
    raw: list[str | Path] = []
    if final_output:
        raw.append(final_output)
    manifest_path = work_dir / "assembly_manifest.json"
    if manifest_path.is_file():
        try:
            manifest = _load_json(manifest_path)
            if isinstance(manifest, Mapping):
                for key in ("final_output", "output", "output_path", "video_path"):
                    if manifest.get(key):
                        raw.append(str(manifest[key]))
        except Exception:
            pass
    raw.extend(("output.mp4", "recap.mp4", "final.mp4"))
    seen: set[str] = set()
    result = []
    for item in raw:
        path = _resolve_candidate(work_dir, item)
        key = str(path.resolve(strict=False))
        if key not in seen:
            seen.add(key)
            result.append((path, str(item)))
    return result


def _final_output_metadata(work_dir: Path, final_output: str | Path | None = None) -> dict[str, Any]:
    outputs = []
    for path, display in _final_output_candidates(work_dir, final_output):
        item: dict[str, Any] = {"path": display, "exists": path.is_file()}
        if item["exists"]:
            item.update({
                "bytes": path.stat().st_size,
                "fingerprint": qc_contract.artifact_fingerprint(path),
            })
        outputs.append(item)
    return {"candidates": _redact(outputs)}


def _existing_final_output(work_dir: Path, final_output: str | Path | None) -> Path | None:
    return next(
        (path for path, _display in _final_output_candidates(work_dir, final_output) if path.is_file()),
        None,
    )


def collect_evidence(work_dir: str | Path, *, final_output: str | Path | None = None) -> dict[str, Any]:
    """Collect lightweight, secret-scrubbed evidence from one work directory."""
    root = Path(work_dir)
    artifacts: dict[str, Any] = {}
    preferred_plan = _first_existing(root, ("clip_plan_validated.json", "clip_plan.json"))
    for name in _JSON_ARTIFACTS:
        if name in {"clip_plan_validated.json", "clip_plan.json"} and name != preferred_plan:
            continue
        item = _collect_file(root, name)
        if item is not None:
            artifacts[name] = item
    evidence = {
        # Display only; excluded from cache_input so moving the work directory is a cache hit.
        "work_dir": str(root),
        "artifacts": artifacts,
        "asr_subtitles": {
            name: item for name in _ASR_OR_SUBTITLE_ARTIFACTS
            if (item := _collect_file(root, name)) is not None
        },
        "visual_metadata": {
            name: item for name in _OPTIONAL_VISUAL_METADATA
            if (item := _collect_file(root, name)) is not None
        },
        "final_output": _final_output_metadata(root, final_output),
    }
    evidence["fingerprint"] = _fingerprint_value(_cache_evidence(evidence))
    return _redact(evidence)


def _cache_file_group(group: Mapping[str, Any]) -> dict[str, Any]:
    return {
        name: {
            key: item.get(key)
            for key in ("kind", "bytes", "fingerprint")
            if isinstance(item, Mapping) and item.get(key) is not None
        }
        for name, item in sorted(group.items())
    }


def _cache_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    final_items = []
    final_output = evidence.get("final_output")
    if isinstance(final_output, Mapping):
        for item in final_output.get("candidates", []):
            if isinstance(item, Mapping):
                final_items.append({
                    key: item.get(key)
                    for key in ("exists", "bytes", "fingerprint")
                    if key in item
                })
    return {
        "artifacts": _cache_file_group(evidence.get("artifacts", {})),
        "asr_subtitles": _cache_file_group(evidence.get("asr_subtitles", {})),
        "visual_metadata": _cache_file_group(evidence.get("visual_metadata", {})),
        "final_output": final_items,
    }


def _effective_config(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    source = dict(DEFAULT_CONFIG)
    if config:
        source.update(dict(config))
        if not config.get("mimo_qc_model"):
            source["mimo_qc_model"] = (
                config.get("mimo_video_model")
                or config.get("mimo_model")
                or source.get("mimo_qc_model")
            )
    # MIMO_QC_MODEL is intentionally read at call time for embedded/CLI tests and
    # long-running agent processes whose environment may be adjusted between runs.
    if os.environ.get("MIMO_QC_MODEL") and not (config and config.get("mimo_qc_model")):
        source["mimo_qc_model"] = os.environ["MIMO_QC_MODEL"]
    return source


def safe_mimo_config(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return non-secret settings suitable for persisted report metadata."""
    source = _effective_config(config)
    keep = (
        "api_provider",
        "mimo_api_url",
        "mimo_api_url_source",
        "mimo_video_api_url",
        "mimo_video_api_url_source",
        "mimo_qc_model",
        "mimo_qc_model_source",
        "mimo_model",
        "mimo_model_source",
        "mimo_video_model",
        "mimo_video_model_source",
        "mimo_disable_thinking",
        "mimo_disable_thinking_source",
        "mimo_media_resolution",
        "mimo_media_resolution_source",
    )
    safe = {key: source[key] for key in keep if key in source}
    safe["model"] = (
        source.get("mimo_qc_model")
        or source.get("mimo_video_model")
        or source.get("mimo_model")
        or "mimo-v2.5"
    )
    key_present = bool(
        source.get("mimo_video_api_key") or source.get("mimo_api_key") or source.get("api_key")
    )
    safe = _redact(safe)
    safe["key_present"] = key_present
    return safe


def build_payload(evidence: Mapping[str, Any], *, stage: str = DEFAULT_STAGE,
                  config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build the report-safe semantic payload (never contains image base64)."""
    cfg = safe_mimo_config(config)
    payload = {
        "stage": stage,
        "artifact": ARTIFACT_NAME,
        "model": cfg["model"],
        "config": cfg,
        "instructions": (
            "你是 video-recap 的建议性质量审阅器。结合解说、剪辑计划、字幕/ASR、TTS、"
            "组装元数据和抽样画面，指出语义或审美问题。只返回主观观察，不做自动修复，"
            "不得提出阻断决定。返回 JSON：{\"observations\":[{\"code\":...,"
            "\"message\":...,\"category\":\"semantic|aesthetic\","
            "\"confidence\":\"low|medium|high\",\"sample_policy\":"
            "\"semantic|aesthetic|sampled\",\"evidence\":{...}}]}。最多 12 条。"
        ),
        "evidence": _summarize(evidence, max_items=12),
        "evidence_fingerprint": evidence.get("fingerprint") or _fingerprint_value(_cache_evidence(evidence)),
    }
    payload["payload_fingerprint"] = _fingerprint_value(payload)
    return _redact(payload)


def _request_payload(payload: Mapping[str, Any], frame_samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    request_evidence = {
        "stage": payload["stage"],
        "instructions": payload["instructions"],
        "evidence": payload["evidence"],
    }
    content: list[dict[str, Any]] = [{
        "type": "text",
        "text": json.dumps(request_evidence, ensure_ascii=False, separators=(",", ":")),
    }]
    for sample in list(frame_samples)[:MAX_FRAMES]:
        data_url = sample.get("data_url") if isinstance(sample, Mapping) else None
        if isinstance(data_url, str) and data_url.startswith("data:image/jpeg;base64,"):
            content.append({"type": "image_url", "image_url": {"url": data_url}})
    return {
        "model": payload["model"],
        "messages": [{"role": "user", "content": content}],
        "max_completion_tokens": 1600,
        "thinking": {"type": "disabled"},
    }


def _strip_json_fence(text: str) -> str:
    value = text.strip()
    if value.startswith("```") and value.endswith("```"):
        lines = value.splitlines()
        if len(lines) >= 3:
            value = "\n".join(lines[1:-1]).strip()
    return value


def _validated_live_output(response: Any) -> Any:
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise ValueError("malformed_response") from None
    if isinstance(content, str):
        try:
            content = json.loads(_strip_json_fence(content))
        except (TypeError, ValueError):
            raise ValueError("malformed_json_content") from None
    if isinstance(content, list):
        return content
    if isinstance(content, Mapping) and isinstance(content.get("observations"), list):
        return content
    if isinstance(content, Mapping) and isinstance(content.get("findings"), list):
        return content
    raise ValueError("malformed_observations")


def _extract_observations(model_output: Any) -> list[Mapping[str, Any]]:
    model_output = _redact(model_output)
    if isinstance(model_output, str):
        try:
            model_output = json.loads(_strip_json_fence(model_output))
        except (TypeError, ValueError):
            return [{"code": "freeform_observation", "message": model_output, "confidence": "low"}]
    if isinstance(model_output, list):
        return [item for item in model_output if isinstance(item, Mapping)][:MAX_OBSERVATIONS]
    if not isinstance(model_output, Mapping):
        return []
    for key in ("observations", "findings"):
        if isinstance(model_output.get(key), list):
            return [item for item in model_output[key] if isinstance(item, Mapping)][:MAX_OBSERVATIONS]
    try:
        return _extract_observations(model_output["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError):
        return [model_output] if model_output else []


def _norm_choice(raw: Any, allowed: set[str], default: str) -> str:
    value = str(raw or "").strip().lower()
    return value if value in allowed else default


def normalize_observations(model_output: Any, *, stage: str = DEFAULT_STAGE,
                           payload: Mapping[str, Any] | None = None,
                           model_config: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Normalize bounded model output into permanently non-blocking findings."""
    payload = _redact(payload or {})
    cfg = safe_mimo_config(model_config)
    model_used = str(cfg.get("model") or "mimo-qc-offline-fixture")
    findings = []
    for index, observation in enumerate(_extract_observations(model_output), start=1):
        obs = _summarize(observation, max_items=10, max_string=MAX_MESSAGE_CHARS)
        if not isinstance(obs, Mapping):
            continue
        category_hint = str(obs.get("category") or obs.get("type") or "semantic").lower()
        category = "mimo_aesthetic" if "aesthetic" in category_hint else "mimo_semantic"
        code = str(obs.get("code") or obs.get("rule_id") or f"mimo_observation_{index}").strip()
        code = (code or f"mimo_observation_{index}")[:96]
        message = str(obs.get("message") or obs.get("summary") or obs.get("text") or "MiMo QC observation")
        message = message.strip()[:MAX_MESSAGE_CHARS] or "MiMo QC observation"
        confidence = _norm_choice(obs.get("confidence"), {"low", "medium", "high"}, "low")
        sample_type = _norm_choice(
            obs.get("sample_policy") or obs.get("sample_policy_type"),
            {"semantic", "aesthetic", "sampled"},
            "aesthetic" if category == "mimo_aesthetic" else "semantic",
        )
        raw_evidence = obs.get("evidence")
        supplied_evidence: Mapping[str, Any] = raw_evidence if isinstance(raw_evidence, Mapping) else {}
        evidence = {
            **dict(supplied_evidence),
            "model": model_used,
            "config": cfg,
            "fingerprint": {
                "evidence": payload.get("evidence_fingerprint"),
                "observation": _fingerprint_value(observation),
            },
        }
        findings.append(qc_contract.build_finding(
            finding_id=str(obs.get("finding_id") or obs.get("id") or f"mimo-{stage}-{index:03d}")[:160],
            stage=stage,
            severity="advisory",
            confidence=confidence,
            sample_policy={"type": sample_type},
            category=category,
            code=code,
            message=message,
            deterministic=False,
            blocking=False,
            source={"artifact": ARTIFACT_NAME, "adapter": "mimo_qc.py"},
            location=obs.get("location") if isinstance(obs.get("location"), Mapping) else {},
            evidence=_redact(evidence),
            model_used=model_used,
            artifact_fingerprints={
                "payload": str(payload.get("payload_fingerprint") or "fixture"),
                "evidence": str(payload.get("evidence_fingerprint") or "fixture"),
            },
            next_action="human_review",
            decision_reason=message,
        ))
    return findings


def _probe_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
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


def sample_video_frames(video_path: str | Path, *, max_frames: int = MAX_FRAMES,
                        max_dimension: int = MAX_FRAME_DIMENSION) -> list[dict[str, Any]]:
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
                        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-ss", f"{timestamp:.3f}", "-i", str(path), "-frames:v", "1",
                        "-vf", (
                            f"scale={int(max_dimension)}:{int(max_dimension)}:"
                            "force_original_aspect_ratio=decrease"
                        ),
                        "-q:v", "4", str(destination),
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
            samples.append({
                "data_url": "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii"),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "timestamp": round(timestamp, 3),
            })
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


def _cache_input(stage: str, payload: Mapping[str, Any], evidence: Mapping[str, Any],
                 frames: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "stage": stage,
        "model": payload.get("model"),
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


def build_report(work_dir: str | Path, *, stage: str = DEFAULT_STAGE, fixture: Any | None = None,
                 dry_run: bool = False, judge: JudgeCallable | None = None,
                 config: Mapping[str, Any] | None = None,
                 final_output: str | Path | None = None,
                 live: bool = False, refresh: bool = False,
                 frame_sampler: FrameSampler | None = None,
                 existing: Mapping[str, Any] | None = None) -> dict[str, Any]:
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
                samples = list(sampler(output, max_frames=MAX_FRAMES, max_dimension=MAX_FRAME_DIMENSION))[:MAX_FRAMES]
            except Exception:
                samples = []
    frame_meta = _frame_metadata(samples)
    cache_input = _cache_input(stage, payload, evidence, frame_meta)
    cache_key = _fingerprint_value(cache_input)

    if not refresh and existing and existing.get("metadata", {}).get("cache_key") == cache_key:
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

    findings = normalize_observations(model_output, stage=stage, payload=payload, model_config=config)
    metadata = {
        "mode": mode,
        "status": status,
        "error": error,
        "report_only": True,
        "auto_repair": False,
        "pipeline_blocking": False,
        "deterministic": False,
        "request_count": 1 if mode == "live" and status in {"completed", "failed"} else 0,
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


def _aggregate_reports(stage_reports: Mapping[str, Mapping[str, Any]], current_stage: str) -> dict[str, Any]:
    current = stage_reports[current_stage]
    findings = [
        finding
        for stage_report in stage_reports.values()
        for finding in stage_report.get("findings", [])
    ]
    metadata = dict(current.get("metadata", {}))
    metadata["stages"] = {stage: dict(report) for stage, report in stage_reports.items()}
    return qc_contract.build_report(
        artifact=ARTIFACT_NAME,
        stage=current_stage,
        findings=findings,
        metadata=metadata,
    )


def write_report(work_dir: str | Path, report: Mapping[str, Any], *,
                 output: str | Path | None = None) -> tuple[Path, dict[str, Any]]:
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


def run(work_dir: str | Path, *, stage: str = DEFAULT_STAGE, fixture: Any | None = None,
        dry_run: bool = False, judge: JudgeCallable | None = None,
        config: Mapping[str, Any] | None = None,
        final_output: str | Path | None = None,
        output: str | Path | None = None,
        live: bool = False, refresh: bool = False,
        frame_sampler: FrameSampler | None = None) -> dict[str, Any]:
    path = Path(output) if output else Path(work_dir) / ARTIFACT_NAME
    existing = _stage_reports(path).get(stage)
    report = build_report(
        work_dir,
        stage=stage,
        fixture=fixture,
        dry_run=dry_run,
        judge=judge,
        config=config,
        final_output=final_output,
        live=live,
        refresh=refresh,
        frame_sampler=frame_sampler,
        existing=existing,
    )
    written_path, aggregate = write_report(work_dir, report, output=output)
    return {"path": str(written_path), "report": aggregate}


def clear_report(work_dir: str | Path, *, output: str | Path | None = None) -> bool:
    path = Path(output) if output else Path(work_dir) / ARTIFACT_NAME
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def _load_fixture(path: str | Path | None) -> Any | None:
    if not path:
        return None
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write advisory MiMo QC from local recap evidence.")
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--stage", default=DEFAULT_STAGE, choices=sorted(qc_contract.STAGES))
    parser.add_argument("--fixture", help="offline model-response fixture JSON")
    parser.add_argument("--live", action="store_true", help="make one live MiMo request for this stage")
    parser.add_argument("--refresh", action="store_true", help="ignore a matching stage cache")
    parser.add_argument("--dry-run", action="store_true", help="write evidence only; never access the network")
    parser.add_argument("--model", help="override MIMO_QC_MODEL for this call")
    parser.add_argument("--final-output")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    fixture = _load_fixture(args.fixture)
    config = {"mimo_qc_model": args.model} if args.model else None
    result = run(
        args.work_dir,
        stage=args.stage,
        fixture=fixture,
        live=args.live and fixture is None,
        refresh=args.refresh,
        dry_run=args.dry_run or (not args.live and fixture is None),
        config=config,
        final_output=args.final_output,
        output=args.output,
    )
    print(json.dumps({
        "ok": result["report"]["ok"],
        "status": result["report"]["metadata"]["status"],
        "path": result["path"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
