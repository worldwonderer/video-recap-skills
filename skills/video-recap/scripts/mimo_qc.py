"""Thin MiMo multimodal evidence QC adapter for video-recap.

This module is report-only by design: it gathers lightweight local evidence,
builds a compact judge payload, optionally normalizes an offline fixture/model
response, and writes a qc_contract-compliant ``mimo_qc.json`` advisory report.
It never repairs artifacts, never blocks the pipeline, and never persists API
keys or secrets.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import qc_contract

try:  # Reuse existing MiMo config defaults without requiring callers to import lib.
    from lib import CONFIG as DEFAULT_CONFIG
except Exception:  # pragma: no cover - only for unusual standalone imports
    DEFAULT_CONFIG = {}

ARTIFACT_NAME = "mimo_qc.json"
DEFAULT_STAGE = "pre_assemble"
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


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint_value(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _redact(value: Any) -> Any:
    """Return a JSON-safe copy with likely credentials removed."""
    return qc_contract.redact_secrets(value)


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return _redact(json.load(f))


def _read_text_sample(path: Path, *, max_chars: int = 4000) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return {
        "kind": "text",
        "bytes": path.stat().st_size,
        "truncated": len(text) > max_chars,
        "sample": _redact(text[:max_chars]),
    }


def _summarize(value: Any, *, max_items: int = 8, max_string: int = 700, depth: int = 0) -> Any:
    """Keep payloads small while preserving semantically useful structure."""
    value = _redact(value)
    if depth >= 4:
        return {"type": type(value).__name__, "fingerprint": _fingerprint_value(value)}
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key in list(value)[:max_items]:
            out[str(key)] = _summarize(value[key], max_items=max_items, max_string=max_string, depth=depth + 1)
        if len(value) > max_items:
            out["_omitted_keys"] = len(value) - max_items
        return out
    if isinstance(value, list):
        return {
            "count": len(value),
            "items": [_summarize(item, max_items=max_items, max_string=max_string, depth=depth + 1)
                      for item in value[:max_items]],
            "omitted": max(0, len(value) - max_items),
        }
    if isinstance(value, str) and len(value) > max_string:
        return {"text": value[:max_string], "truncated": True, "chars": len(value)}
    return value


def _collect_file(work_dir: Path, name: str) -> dict[str, Any] | None:
    path = work_dir / name
    if not path.exists() or not path.is_file():
        return None
    try:
        fingerprint = qc_contract.artifact_fingerprint(path)
        if path.suffix.lower() == ".json":
            raw = _load_json(path)
            summary = _summarize(raw)
            kind = "json"
        else:
            raw = _read_text_sample(path)
            summary = _summarize(raw)
            kind = raw.get("kind", "file")
        return {
            "path": name,
            "kind": kind,
            "bytes": path.stat().st_size,
            "fingerprint": fingerprint,
            "summary": summary,
        }
    except Exception as exc:  # keep adapter report-only and resilient
        return {"path": name, "kind": "unreadable", "error": str(exc)}


def _first_existing(work_dir: Path, names: Sequence[str]) -> str | None:
    for name in names:
        if (work_dir / name).exists():
            return name
    return None


def _final_output_metadata(work_dir: Path, final_output: str | Path | None = None) -> dict[str, Any]:
    candidates: list[Path] = []
    if final_output:
        candidates.append(Path(final_output))
    manifest_path = work_dir / "assembly_manifest.json"
    if manifest_path.exists():
        try:
            manifest = _load_json(manifest_path)
            for key in ("final_output", "output", "output_path", "video_path"):
                if isinstance(manifest, Mapping) and manifest.get(key):
                    candidates.append(Path(str(manifest[key])))
        except Exception:
            pass
    for name in ("output.mp4", "recap.mp4", "final.mp4"):
        candidates.append(work_dir / name)

    seen: set[str] = set()
    outputs = []
    for candidate in candidates:
        path = candidate if candidate.is_absolute() else work_dir / candidate
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        item: dict[str, Any] = {"path": str(candidate)}
        if path.exists() and path.is_file():
            item.update({
                "exists": True,
                "bytes": path.stat().st_size,
                "fingerprint": qc_contract.artifact_fingerprint(path),
            })
        else:
            item["exists"] = False
        outputs.append(item)
    return {"candidates": _redact(outputs)}


def collect_evidence(work_dir: str | Path, *, final_output: str | Path | None = None) -> dict[str, Any]:
    """Collect lightweight, secret-scrubbed evidence from a video-recap work_dir."""
    root = Path(work_dir)
    artifacts: dict[str, Any] = {}

    # Prefer validated clip plan when present; include clip_plan only as fallback.
    preferred_clip_plan = _first_existing(root, ("clip_plan_validated.json", "clip_plan.json"))
    for name in _JSON_ARTIFACTS:
        if name in {"clip_plan_validated.json", "clip_plan.json"} and name != preferred_clip_plan:
            continue
        item = _collect_file(root, name)
        if item is not None:
            artifacts[name] = item

    asr_subtitles = {name: item for name in _ASR_OR_SUBTITLE_ARTIFACTS if (item := _collect_file(root, name))}
    visual_metadata = {name: item for name in _OPTIONAL_VISUAL_METADATA if (item := _collect_file(root, name))}

    evidence = {
        "work_dir": str(root),
        "artifacts": artifacts,
        "asr_subtitles": asr_subtitles,
        "visual_metadata": visual_metadata,
        "final_output": _final_output_metadata(root, final_output),
    }
    evidence["fingerprint"] = _fingerprint_value(evidence)
    return _redact(evidence)


def safe_mimo_config(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return non-secret MiMo judge configuration suitable for report metadata."""
    source = dict(DEFAULT_CONFIG)
    if config:
        source.update(dict(config))
    keep = (
        "api_provider",
        "mimo_api_url",
        "mimo_api_url_source",
        "mimo_video_api_url",
        "mimo_video_api_url_source",
        "mimo_model",
        "mimo_model_source",
        "mimo_video_model",
        "mimo_video_model_source",
        "vlm_model",
        "vlm_model_source",
        "mimo_disable_thinking",
        "mimo_disable_thinking_source",
        "mimo_media_resolution",
        "mimo_media_resolution_source",
    )
    safe = {key: source[key] for key in keep if key in source}
    model = source.get("mimo_video_model") or source.get("mimo_model") or source.get("vlm_model") or "mimo-v2.5"
    safe.setdefault("model", model)
    safe["api_key_configured"] = bool(
        source.get("mimo_video_api_key") or source.get("mimo_api_key") or source.get("api_key")
    )
    return _redact(safe)


def build_payload(evidence: Mapping[str, Any], *, stage: str = DEFAULT_STAGE,
                  config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build a concise, mockable payload for a multimodal evidence judge."""
    cfg = safe_mimo_config(config)
    instructions = (
        "You are a report-only video-recap QC judge. Review narration, overlays, clip plan, "
        "assembly/TTS metadata, subtitles/ASR, final output metadata, and optional frames/storyboard. "
        "Return concise subjective observations only; do not propose auto-repair or blocking decisions. "
        "Each observation should include code, message, category (semantic/aesthetic), confidence "
        "(low/medium/high), sample_policy (semantic/aesthetic/sampled), and evidence."
    )
    payload = {
        "stage": stage,
        "artifact": ARTIFACT_NAME,
        "model": cfg.get("model"),
        "config": cfg,
        "instructions": instructions,
        "evidence": _summarize(evidence, max_items=12),
        "evidence_fingerprint": evidence.get("fingerprint") or _fingerprint_value(evidence),
    }
    payload["payload_fingerprint"] = _fingerprint_value(payload)
    return _redact(payload)


def _extract_observations(model_output: Any) -> list[Mapping[str, Any]]:
    model_output = _redact(model_output)
    if isinstance(model_output, str):
        try:
            model_output = json.loads(model_output)
        except json.JSONDecodeError:
            return [{"code": "freeform_observation", "message": model_output, "confidence": "low"}]
    if isinstance(model_output, list):
        return [item for item in model_output if isinstance(item, Mapping)]
    if not isinstance(model_output, Mapping):
        return []
    if isinstance(model_output.get("observations"), list):
        return [item for item in model_output["observations"] if isinstance(item, Mapping)]
    if isinstance(model_output.get("findings"), list):
        return [item for item in model_output["findings"] if isinstance(item, Mapping)]
    # OpenAI-compatible fixture convenience.
    try:
        content = model_output["choices"][0]["message"]["content"]
        return _extract_observations(content)
    except Exception:
        return [model_output] if model_output else []


def _norm_choice(raw: Any, allowed: set[str], default: str) -> str:
    value = str(raw or "").strip().lower()
    return value if value in allowed else default


def normalize_observations(model_output: Any, *, stage: str = DEFAULT_STAGE,
                           payload: Mapping[str, Any] | None = None,
                           model_config: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Normalize model/fixture observations into advisory qc_contract findings."""
    payload = _redact(payload or {})
    cfg = safe_mimo_config(model_config)
    model_used = str(cfg.get("model") or "mimo-qc-offline-fixture")
    findings: list[dict[str, Any]] = []
    for idx, obs in enumerate(_extract_observations(model_output), start=1):
        category_hint = str(obs.get("category") or obs.get("type") or "semantic").lower()
        category = "mimo_aesthetic" if "aesthetic" in category_hint else "mimo_semantic"
        code = str(obs.get("code") or obs.get("rule_id") or f"mimo_observation_{idx}").strip() or f"mimo_observation_{idx}"
        message = str(obs.get("message") or obs.get("summary") or obs.get("text") or "MiMo QC observation").strip()
        confidence = _norm_choice(obs.get("confidence"), {"low", "medium", "high"}, "low")
        sample_policy_type = _norm_choice(
            obs.get("sample_policy") or obs.get("sample_policy_type"),
            {"semantic", "aesthetic", "sampled"},
            "aesthetic" if category == "mimo_aesthetic" else "semantic",
        )
        location = obs.get("location") if isinstance(obs.get("location"), Mapping) else {}
        evidence = obs.get("evidence") if isinstance(obs.get("evidence"), Mapping) else {}
        evidence = {
            **dict(evidence),
            "model": model_used,
            "config": cfg,
            "cache": {
                "mode": "fixture_or_dry_run",
                "payload_fingerprint": payload.get("payload_fingerprint"),
            },
            "fingerprint": {
                "evidence": payload.get("evidence_fingerprint"),
                "observation": _fingerprint_value(obs),
            },
        }
        findings.append(qc_contract.build_finding(
            finding_id=str(obs.get("finding_id") or obs.get("id") or f"mimo-{idx:03d}"),
            stage=stage,
            severity="advisory",
            confidence=confidence,
            sample_policy={"type": sample_policy_type},
            category=category,
            code=code,
            message=message,
            deterministic=False,
            blocking=False,
            source={"artifact": ARTIFACT_NAME, "adapter": "mimo_qc.py"},
            location=location,
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


def build_report(work_dir: str | Path, *, stage: str = DEFAULT_STAGE, fixture: Any | None = None,
                 dry_run: bool = False, judge: JudgeCallable | None = None,
                 config: Mapping[str, Any] | None = None,
                 final_output: str | Path | None = None) -> dict[str, Any]:
    """Build a validated mimo_qc report without writing it."""
    evidence = collect_evidence(work_dir, final_output=final_output)
    payload = build_payload(evidence, stage=stage, config=config)
    cfg = safe_mimo_config(config)
    if fixture is not None:
        model_output = fixture
        mode = "fixture"
    elif judge is not None and not dry_run:
        model_output = judge(payload)
        mode = "injected_judge"
    else:
        model_output = {"observations": []}
        mode = "dry_run"

    findings = normalize_observations(model_output, stage=stage, payload=payload, model_config=cfg)
    report = qc_contract.build_report(
        artifact=ARTIFACT_NAME,
        stage=stage,
        findings=findings,
        metadata={
            "mode": mode,
            "report_only": True,
            "auto_repair": False,
            "pipeline_blocking": False,
            "deterministic": False,
            "evidence": evidence,
            "payload": payload,
            "config": cfg,
        },
    )
    qc_contract.validate_report(report)
    return _redact(report)


def write_report(work_dir: str | Path, report: Mapping[str, Any], *, output: str | Path | None = None) -> Path:
    path = Path(output) if output else Path(work_dir) / ARTIFACT_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_report = _redact(dict(report))
    qc_contract.validate_report(safe_report)
    path.write_text(json.dumps(safe_report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    qc_contract.validate_report(json.loads(path.read_text(encoding="utf-8")))
    return path


def run(work_dir: str | Path, *, stage: str = DEFAULT_STAGE, fixture: Any | None = None,
        dry_run: bool = False, judge: JudgeCallable | None = None,
        config: Mapping[str, Any] | None = None,
        final_output: str | Path | None = None,
        output: str | Path | None = None) -> dict[str, Any]:
    report = build_report(
        work_dir,
        stage=stage,
        fixture=fixture,
        dry_run=dry_run,
        judge=judge,
        config=config,
        final_output=final_output,
    )
    path = write_report(work_dir, report, output=output)
    return {"path": str(path), "report": report}


def _load_fixture(path: str | Path | None) -> Any | None:
    if not path:
        return None
    with Path(path).open("r", encoding="utf-8") as f:
        return json.load(f)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write advisory MiMo QC report from local video-recap evidence.")
    parser.add_argument("--work-dir", required=True, help="video-recap work directory")
    parser.add_argument("--stage", default=DEFAULT_STAGE, choices=sorted(qc_contract.STAGES))
    parser.add_argument("--fixture", help="offline model-response fixture JSON")
    parser.add_argument("--dry-run", action="store_true", help="write evidence-only report with no model observations")
    parser.add_argument("--final-output", help="optional final video path metadata")
    parser.add_argument("--output", help="optional output path; defaults to work_dir/mimo_qc.json")
    args = parser.parse_args(argv)

    fixture = _load_fixture(args.fixture)
    result = run(
        args.work_dir,
        stage=args.stage,
        fixture=fixture,
        dry_run=args.dry_run or fixture is None,
        final_output=args.final_output,
        output=args.output,
    )
    print(json.dumps({"ok": result["report"]["ok"], "path": result["path"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
