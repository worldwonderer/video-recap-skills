"""Collect and redact local evidence for MiMo QC."""

from __future__ import annotations


import hashlib

import json


import os


from pathlib import Path

from typing import Any, Mapping, Sequence

import qc_contract
from mimo_qc_client import DEFAULT_CONFIG

_JSON_ARTIFACTS = (
    "narration.json",
    "visual_overlays.json",
    "clip_plan_validated.json",
    "clip_plan.json",
    "assembly_manifest.json",
    "tts_meta.json",
)

_SOURCE_ASR_ARTIFACTS = (
    "asr_clean.json",
    "asr.json",
    "asr_result.json",
    "asr_segments.json",
)

_GENERATED_SUBTITLE_ARTIFACTS = (
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


def _summarize(
    value: Any, *, max_items: int = 8, max_string: int = 700, depth: int = 0
) -> Any:
    """Keep request/report evidence bounded while retaining useful structure."""
    value = _redact(value)
    if depth >= 4:
        return {"type": type(value).__name__, "fingerprint": _fingerprint_value(value)}
    if isinstance(value, Mapping):
        out = {
            str(key): _summarize(
                item, max_items=max_items, max_string=max_string, depth=depth + 1
            )
            for key, item in list(value.items())[:max_items]
        }
        if len(value) > max_items:
            out["_omitted_keys"] = len(value) - max_items
        return out
    if isinstance(value, list):
        return {
            "count": len(value),
            "items": [
                _summarize(
                    item, max_items=max_items, max_string=max_string, depth=depth + 1
                )
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


def _collect_multi_source_asr(work_dir: Path) -> dict[str, Any]:
    """Collect per-source ASR when a multi-source project has no root transcript."""
    manifest_path = work_dir / "multi_source_manifest.json"
    if not manifest_path.is_file():
        return {}
    try:
        manifest = _load_json(manifest_path)
    except Exception:
        return {}
    sources = manifest.get("sources") if isinstance(manifest, Mapping) else None
    if not isinstance(sources, list):
        return {}
    collected = {}
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        source_id = str(source.get("source_id") or "").strip()
        relative_dir = str(source.get("source_work_dir") or "").strip()
        if not source_id or not relative_dir:
            continue
        source_dir = (work_dir / relative_dir).resolve(strict=False)
        try:
            source_dir.relative_to(work_dir.resolve(strict=False))
        except ValueError:
            continue
        relative_name = _first_existing(source_dir, _SOURCE_ASR_ARTIFACTS)
        if not relative_name:
            continue
        item = _collect_file(work_dir, str(Path(relative_dir) / relative_name))
        if item is not None:
            collected[source_id] = item
    return collected


def _resolve_candidate(work_dir: Path, candidate: str | Path) -> Path:
    path = Path(candidate)
    return path if path.is_absolute() else work_dir / path


def _final_output_candidates(
    work_dir: Path, final_output: str | Path | None
) -> list[tuple[Path, str]]:
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


def _final_output_metadata(
    work_dir: Path, final_output: str | Path | None = None
) -> dict[str, Any]:
    outputs = []
    for path, display in _final_output_candidates(work_dir, final_output):
        item: dict[str, Any] = {"path": display, "exists": path.is_file()}
        if item["exists"]:
            item.update(
                {
                    "bytes": path.stat().st_size,
                    "fingerprint": qc_contract.artifact_fingerprint(path),
                }
            )
        outputs.append(item)
    return {"candidates": _redact(outputs)}


def _existing_final_output(
    work_dir: Path, final_output: str | Path | None
) -> Path | None:
    return next(
        (
            path
            for path, _display in _final_output_candidates(work_dir, final_output)
            if path.is_file()
        ),
        None,
    )


def collect_evidence(
    work_dir: str | Path, *, final_output: str | Path | None = None
) -> dict[str, Any]:
    """Collect lightweight, secret-scrubbed evidence from one work directory."""
    root = Path(work_dir)
    artifacts: dict[str, Any] = {}
    preferred_plan = _first_existing(
        root, ("clip_plan_validated.json", "clip_plan.json")
    )
    for name in _JSON_ARTIFACTS:
        if (
            name in {"clip_plan_validated.json", "clip_plan.json"}
            and name != preferred_plan
        ):
            continue
        item = _collect_file(root, name)
        if item is not None:
            artifacts[name] = item
    evidence = {
        # Display only; excluded from cache_input so moving the work directory is a cache hit.
        "work_dir": str(root),
        "artifacts": artifacts,
        "source_asr": {
            name: item
            for name in _SOURCE_ASR_ARTIFACTS
            if (item := _collect_file(root, name)) is not None
        },
        "generated_subtitles": {
            name: item
            for name in _GENERATED_SUBTITLE_ARTIFACTS
            if (item := _collect_file(root, name)) is not None
        },
        "visual_metadata": {
            name: item
            for name in _OPTIONAL_VISUAL_METADATA
            if (item := _collect_file(root, name)) is not None
        },
        "final_output": _final_output_metadata(root, final_output),
    }
    if not evidence["source_asr"]:
        evidence["source_asr"] = _collect_multi_source_asr(root)
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
                final_items.append(
                    {
                        key: item.get(key)
                        for key in ("exists", "bytes", "fingerprint")
                        if key in item
                    }
                )
    return {
        "artifacts": _cache_file_group(evidence.get("artifacts", {})),
        "source_asr": _cache_file_group(evidence.get("source_asr", {})),
        "generated_subtitles": _cache_file_group(
            evidence.get("generated_subtitles", {})
        ),
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
        source.get("mimo_video_api_key")
        or source.get("mimo_api_key")
        or source.get("api_key")
    )
    safe = _redact(safe)
    safe["key_present"] = key_present
    return safe
