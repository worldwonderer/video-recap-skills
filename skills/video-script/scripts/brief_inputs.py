"""Validate optional ASR, MiMo, and stage-status inputs for the brief."""

import hashlib

import importlib.util


import json


from pathlib import Path

from lib import CONFIG, file_fingerprint, stable_hash


from brief_context import _clean_asr_prompt_fingerprint, _consolidation_model

try:
    from deslop_qc import analyze_deslop_qc
except ModuleNotFoundError:
    _deslop_qc_path = Path(__file__).with_name("deslop_qc.py")
    _deslop_qc_spec = importlib.util.spec_from_file_location(
        "deslop_qc", _deslop_qc_path
    )
    if _deslop_qc_spec is None or _deslop_qc_spec.loader is None:
        raise
    _deslop_qc_module = importlib.util.module_from_spec(_deslop_qc_spec)
    _deslop_qc_spec.loader.exec_module(_deslop_qc_module)
    analyze_deslop_qc = _deslop_qc_module.analyze_deslop_qc

_ASR_SPAN_TOL = 0.05

_MIMO_REJECTION_MARKERS = (
    "request was rejected",
    "considered high risk",
    "high risk",
    "content policy",
    "cannot process",
    "无法处理",
    "内容审核",
    "违规",
)


def _clean_asr_fresh(out_path, source_path):
    # Local freshness check kept inline so the separately shipped byte-identical copy
    # stays self-contained and in lockstep.
    try:
        return (
            out_path.exists()
            and source_path.exists()
            and (out_path.stat().st_mtime >= source_path.stat().st_mtime)
        )
    except OSError:
        return False


def _load_clean_asr(work_dir, asr_result):
    """Return consolidate.py's cleaned ASR segments, or None to fall back to raw asr_result.
    Gated on parse + non-empty + freshness + provenance(source_md5) + timing invariant
    (len== first, then per-segment spans within _ASR_SPAN_TOL)."""
    base = [s for s in (asr_result or []) if isinstance(s, dict)]
    if not base:
        return None
    work_dir = Path(work_dir)
    clean_path = work_dir / "asr_clean.json"
    src_path = work_dir / "asr_result.json"
    if not clean_path.exists() or not _clean_asr_fresh(clean_path, src_path):
        return None
    try:
        payload = json.loads(clean_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict):
        return None
    segments = payload.get("segments")
    if not isinstance(segments, list) or len(segments) != len(base):
        return None
    try:
        if payload.get("source_md5") != hashlib.md5(src_path.read_bytes()).hexdigest():
            return None
    except OSError:
        return None
    if payload.get("model") != _consolidation_model():
        return None
    if payload.get("prompt_md5") != _clean_asr_prompt_fingerprint():
        return None
    for orig, clean in zip(base, segments):
        if not isinstance(clean, dict):
            return None
        try:
            if (
                abs(float(clean.get("start")) - float(orig.get("start")))
                > _ASR_SPAN_TOL
            ):
                return None
            if abs(float(clean.get("end")) - float(orig.get("end"))) > _ASR_SPAN_TOL:
                return None
        except (TypeError, ValueError):
            return None
    return segments


def _is_mimo_chunk_usable(content):
    text = str(content or "").strip()
    if not text:
        return False
    low = text.lower()
    return not any(marker in low for marker in _MIMO_REJECTION_MARKERS)


def _mimo_video_settings_fingerprint():
    return {
        "model": CONFIG.get("mimo_video_model")
        or CONFIG.get("mimo_model")
        or CONFIG.get("vlm_model"),
        "mimo_video_api_url": CONFIG.get("mimo_video_api_url"),
        "mimo_video_fps": CONFIG.get("mimo_video_fps", 2.0),
        "mimo_media_resolution": CONFIG.get("mimo_media_resolution", "default"),
        "mimo_video_chunk_max_seconds": CONFIG.get(
            "mimo_video_chunk_max_seconds", 20.0
        ),
        "mimo_video_chunk_min_seconds": CONFIG.get("mimo_video_chunk_min_seconds", 1.0),
        "mimo_video_base64_max_mb": CONFIG.get("mimo_video_base64_max_mb", 45.0),
        "mimo_video_prompt": CONFIG.get("mimo_video_prompt", ""),
        "mimo_disable_thinking": CONFIG.get("mimo_disable_thinking", True),
    }


def _mimo_chunk_cache_key(chunk):
    return (
        f"{chunk['chunk_id']}|{chunk['scene_id']}|"
        f"{float(chunk['start']):.3f}-{float(chunk['end']):.3f}"
    )


def _mimo_cached_chunks_fingerprint(done):
    return stable_hash(done)


def _mimo_overview_payload_fingerprint(overview):
    payload = dict(overview)
    payload.pop("overview_fingerprint", None)
    return stable_hash(payload)


def _mimo_video_chunks_for_brief(scenes):
    max_seconds = float(CONFIG.get("mimo_video_chunk_max_seconds", 20.0) or 20.0)
    min_seconds = float(CONFIG.get("mimo_video_chunk_min_seconds", 1.0) or 1.0)
    chunks = []
    for scene_index, scene in enumerate(scenes or []):
        if not isinstance(scene, dict):
            continue
        try:
            start = float(scene.get("start", 0.0))
            end = float(scene.get("end", start))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        scene_id = scene.get("scene_id", scene_index)
        cursor = start
        while cursor < end:
            chunk_end = min(end, cursor + max_seconds)
            if end - chunk_end < min_seconds and chunk_end < end:
                chunk_end = end
            if chunk_end > cursor:
                chunks.append(
                    {
                        "chunk_id": len(chunks),
                        "scene_id": scene_id,
                        "start": round(cursor, 3),
                        "end": round(chunk_end, 3),
                    }
                )
            cursor = chunk_end
    return chunks


def _mimo_overview_matches_current_inputs(overview, scenes_analysis, video_path=None):
    if not isinstance(overview, dict) or overview.get("input") != "scene_chunks":
        return False
    if overview.get("settings") != _mimo_video_settings_fingerprint():
        return False
    overview_fingerprint = overview.get("overview_fingerprint")
    if (
        not overview_fingerprint
        or overview_fingerprint != _mimo_overview_payload_fingerprint(overview)
    ):
        return False
    if video_path is not None:
        try:
            if overview.get("source_video_fingerprint") != file_fingerprint(video_path):
                return False
        except OSError:
            return False
    chunks = overview.get("chunks")
    if not isinstance(chunks, list) or not all(
        isinstance(chunk, dict) and _is_mimo_chunk_usable(chunk.get("content"))
        for chunk in chunks
    ):
        return False
    chunks_fingerprint = overview.get("chunks_fingerprint")
    if not chunks_fingerprint or chunks_fingerprint != _mimo_cached_chunks_fingerprint(
        chunks
    ):
        return False
    expected_chunks = _mimo_video_chunks_for_brief(scenes_analysis)
    if not expected_chunks or len(chunks) != len(expected_chunks):
        return False
    try:
        cached_keys = [_mimo_chunk_cache_key(chunk) for chunk in chunks]
        expected_keys = [_mimo_chunk_cache_key(chunk) for chunk in expected_chunks]
    except (KeyError, TypeError, ValueError):
        return False
    return cached_keys == expected_keys


def _load_mimo_overview_for_brief(
    work_dir, scenes_analysis, enabled=None, video_path=None
):
    overview_enabled = (
        CONFIG.get("mimo_video_overview", False) if enabled is None else enabled
    )
    if not overview_enabled:
        return {}
    path = Path(work_dir) / "mimo_video_overview.json"
    if not path.exists():
        return {}
    try:
        overview = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return (
        overview
        if _mimo_overview_matches_current_inputs(
            overview, scenes_analysis, video_path=video_path
        )
        else {}
    )


def _load_optional_stage_status(work_dir, filename):
    """Load optional-stage status sidecars defensively."""
    path = Path(work_dir) / filename
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _status_message(message, limit=180):
    text = " ".join(str(message or "").split())
    return text[:limit]


def _optional_stage_warning(stage, status, message):
    line = f"- {stage}: {status}"
    msg = _status_message(message)
    return f"{line} — {msg}" if msg else line


def _format_optional_stage_warnings(
    work_dir,
    *,
    mimo_overview_enabled=None,
    mimo_overview=None,
    consolidation_index=None,
):
    """Surface fail-open optional-stage loss near the top of the brief."""
    work_dir = Path(work_dir)
    warnings = []

    overview_enabled = (
        bool(CONFIG.get("mimo_video_overview", False))
        if mimo_overview_enabled is None
        else bool(mimo_overview_enabled)
    )
    overview_status = _load_optional_stage_status(
        work_dir, "mimo_video_overview.status.json"
    )
    if overview_status.get("enabled") and overview_status.get("status") in {
        "failed",
        "skipped_no_key",
    }:
        warnings.append(
            _optional_stage_warning(
                "mimo_video_overview",
                overview_status.get("status"),
                overview_status.get("message"),
            )
        )
    elif overview_enabled and not mimo_overview:
        warnings.append(
            _optional_stage_warning(
                "mimo_video_overview",
                "missing_artifact",
                "enabled but no valid mimo_video_overview.json is available to this brief",
            )
        )

    consolidation_status = _load_optional_stage_status(
        work_dir, "consolidation.status.json"
    )
    if consolidation_status.get("enabled"):
        if consolidation_status.get("status") == "failed":
            warnings.append(
                _optional_stage_warning(
                    "consolidation", "failed", consolidation_status.get("message")
                )
            )
        elif consolidation_status.get("do_index") and not consolidation_index:
            warnings.append(
                _optional_stage_warning(
                    "consolidation",
                    "missing_index",
                    "enabled but no valid understanding_index.json is available to this brief",
                )
            )

    if not warnings:
        return []
    return [
        "## Optional stage warnings",
        "",
        "These stages are fail-open; continue, but do not assume their missing context exists.",
        *warnings,
        "",
    ]
