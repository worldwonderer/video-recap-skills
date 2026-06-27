"""Filesystem material library helpers for video-recap.

The library is intentionally grep-friendly: JSON/MD/JSONL files on disk, no DB,
no embeddings, no raw-media copies. Current metadata lives in each material
folder; the root ``materials_index.jsonl`` is an append-only journal for grep and
history.
"""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ALLOWED_ARTIFACTS = {
    "scenes.json",
    "asr_result.json",
    "vlm_analysis.json",
    "silence_periods.json",
    "timeline_fusion.json",
    "understanding_index.json",
    "understanding_index.md",
    "agent_narration_brief.md",
    "background_research.json",
    "reference_profile.json",
    "reference_match_report.json",
    "recap_run_manifest.json",
}
# Redaction targets credential VALUE shapes, not English/Chinese dictionary words — the
# library must stay a faithful copy of the analysis. Bare words like "secret"/"token"
# legitimately appear in transcripts/summaries and must NOT be touched.
SECRET_VALUE_RES = (
    re.compile(r"\btp-[A-Za-z0-9_-]{8,}\b"),     # MiMo Token Plan keys
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),    # OpenAI-style keys
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),  # GitHub tokens
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),         # AWS access key id
    re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\b"),  # JWT
)
# `KEY=VALUE` / `"key": "value"` assignments whose key denotes a credential -> mask the VALUE.
SECRET_ASSIGN_RE = re.compile(
    r"(?i)(\b(?:mimo(?:_\w+)?_api_key|api_key|secret_key|access_token|refresh_token|"
    r"authorization|password|passwd|bearer)\b\s*[:=]\s*)(\"?)([^\s\"',;]+)(\"?)"
)
# Exact JSON/dict key names whose VALUE is a credential and must be dropped (key name kept).
SECRET_KEY_NAMES = frozenset({
    "api_key", "mimo_api_key", "mimo_asr_api_key", "mimo_tts_api_key", "mimo_video_api_key",
    "secret_key", "access_token", "refresh_token", "authorization", "password", "passwd",
})


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def file_fingerprint(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def stable_json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def settings_fingerprint(settings) -> str:
    return hashlib.sha256(stable_json_dumps(settings or {}).encode("utf-8")).hexdigest()


def source_id_from_fingerprint(fingerprint: str) -> str:
    return f"src_{str(fingerprint)[:12]}"


def assign_source_ids(sources: list[dict]) -> list[dict]:
    """Assign deterministic source_id values to manifest source records.

    Base id is ``src_<sha256[:12]>``. When the same fingerprint appears more than
    once in one project, the first keeps the base id and later distinct paths get
    ``_<pathhash6>`` suffixes. Input order does not affect the id for unique
    fingerprints, and duplicate suffixes are derived from resolved path text.
    """
    seen: dict[str, set[str]] = {}
    assigned = []
    for raw in sources:
        item = dict(raw)
        fp = str(item.get("source_video_fingerprint") or item.get("fingerprint") or "")
        if not fp:
            raise ValueError("source fingerprint is required")
        base = source_id_from_fingerprint(fp)
        path = str(Path(item.get("source_path") or item.get("path") or "").resolve())
        used = seen.setdefault(fp, set())
        if not used:
            sid = base
        else:
            suffix = hashlib.sha256(path.encode("utf-8")).hexdigest()[:6]
            sid = f"{base}_{suffix}"
        # Avoid accidental path-hash collisions within one manifest.
        while sid in used:
            suffix = hashlib.sha256((path + sid).encode("utf-8")).hexdigest()[:6]
            sid = f"{base}_{suffix}"
        used.add(sid)
        item["source_id"] = sid
        item["source_path"] = path
        assigned.append(item)
    return assigned


def _slug(text: str, max_len: int = 48) -> str:
    raw = Path(str(text or "material")).stem.lower()
    raw = re.sub(r"[^a-z0-9\u4e00-\u9fff._-]+", "-", raw).strip("-._")
    return (raw or "material")[:max_len].strip("-._") or "material"


def material_id_for(source_path: str | Path, source_fingerprint: str) -> str:
    return f"{_slug(str(source_path))}-{str(source_fingerprint)[:12]}"


def material_dir(library_dir: str | Path, material_id: str) -> Path:
    return Path(library_dir) / "materials" / material_id


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


def _safe_text(value, limit: int = 600) -> str:
    text = str(value or "").replace("\x00", " ").strip()
    return _redact_text(text)[:limit]


def _redact_text(text: str) -> str:
    """Redact credential value shapes only; leave ordinary words (secret/token/…) intact."""
    text = str(text or "")
    for rx in SECRET_VALUE_RES:
        text = rx.sub("[redacted-token]", text)
    return SECRET_ASSIGN_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted-key]{m.group(4)}", text)


def _redact_json(value):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            key_text = str(key)
            # Drop only the value of an exact credential-named key; keep the key name and
            # never coalesce distinct keys (so benign fields like token_economy survive).
            if key_text.strip().lower() in SECRET_KEY_NAMES:
                out[key_text] = "[redacted]"
            else:
                out[key_text] = _redact_json(item)
        return out
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def copy_artifact_redacted(src: Path, dst: Path) -> None:
    """Copy an allowed JSON/MD artifact without persisting obvious secret markers."""
    try:
        text = src.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"refusing to persist non-text material artifact: {src.name}") from exc
    if src.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except ValueError:
            dst.write_text(_redact_text(text), encoding="utf-8")
        else:
            dst.write_text(json.dumps(_redact_json(data), ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        dst.write_text(_redact_text(text), encoding="utf-8")


def summarize_work_dir(work_dir: str | Path, *, source_name: str = "") -> dict:
    """Best-effort small summary for material.md/index grep."""
    work = Path(work_dir)
    summary_parts = []
    tags = []
    idx = _load_json(work / "understanding_index.json")
    if isinstance(idx, dict):
        for key in ("summary", "story_summary", "overall_summary", "one_sentence"):
            if idx.get(key):
                summary_parts.append(_safe_text(idx.get(key)))
                break
        for key in ("characters", "entities", "keywords", "tags"):
            vals = idx.get(key)
            if isinstance(vals, list):
                tags.extend(_safe_text(v, 80) for v in vals[:12])
    vlm = _load_json(work / "vlm_analysis.json")
    if isinstance(vlm, dict):
        for key in ("summary", "overall_summary", "video_summary"):
            if vlm.get(key):
                summary_parts.append(_safe_text(vlm.get(key)))
                break
    scenes = _load_json(work / "scenes.json")
    if isinstance(scenes, list):
        tags.append(f"scenes:{len(scenes)}")
    asr = _load_json(work / "asr_result.json")
    if isinstance(asr, list):
        tags.append(f"asr:{len(asr)}")
    summary = " | ".join(p for p in summary_parts if p) or f"Analyzed video material: {source_name or work.name}"
    dedup_tags = []
    for tag in tags:
        if tag and tag not in dedup_tags:
            dedup_tags.append(tag)
    return {"summary": summary, "tags": dedup_tags[:20]}


def allowed_artifact_paths(work_dir: str | Path) -> list[Path]:
    work = Path(work_dir)
    return [work / name for name in sorted(ALLOWED_ARTIFACTS) if (work / name).is_file()]


def write_material_md(path: Path, metadata: dict, summary: str, tags: list[str]) -> None:
    artifact_lines = "\n".join(f"- `{a.get('name')}` → `{a.get('path')}`" for a in metadata.get("artifacts", []))
    tags_text = ", ".join(tags) if tags else "(none)"
    text = f"""# Material: {metadata.get('source_name') or metadata.get('material_id')}

- material_id: `{metadata.get('material_id')}`
- source: `{metadata.get('source_path')}`
- source_fingerprint: `{metadata.get('source_video_fingerprint')}`
- settings_fingerprint: `{metadata.get('settings_fingerprint')}`
- updated_at: `{metadata.get('updated_at')}`
- tags: {tags_text}

## Summary
{_safe_text(summary, 2000)}

## Artifacts
{artifact_lines or '- (none)'}
"""
    path.write_text(text, encoding="utf-8")


def save_material(
    library_dir: str | Path,
    work_dir: str | Path,
    source_path: str | Path,
    source_fingerprint: str,
    settings_fp: str,
    *,
    duration: float | None = None,
    source_id: str | None = None,
    material_id: str | None = None,
    now: str | None = None,
) -> dict:
    """Persist small reusable analysis artifacts into the filesystem library."""
    lib = Path(library_dir)
    mid = material_id or material_id_for(source_path, source_fingerprint)
    dest = material_dir(lib, mid)
    artifacts_dir = dest / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    now = now or utc_now_iso()

    copied = []
    for src in allowed_artifact_paths(work_dir):
        dst = artifacts_dir / src.name
        copy_artifact_redacted(src, dst)
        copied.append({"name": src.name, "path": f"artifacts/{src.name}", "sha256": file_fingerprint(dst)})

    existing = _load_json(dest / "material.json")
    created_at = existing.get("created_at") if isinstance(existing, dict) and existing.get("created_at") else now
    source_path = Path(source_path).resolve()
    summary_info = summarize_work_dir(work_dir, source_name=source_path.name)
    metadata = {
        "schema_version": 1,
        "material_id": mid,
        "source_id": source_id,
        "source_name": source_path.name,
        "source_path": str(source_path),
        "source_video_fingerprint": source_fingerprint,
        "duration": duration,
        "settings_fingerprint": settings_fp,
        "artifacts": copied,
        "created_at": created_at,
        "updated_at": now,
    }
    (dest / "material.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    write_material_md(dest / "material.md", metadata, summary_info["summary"], summary_info["tags"])

    index_record = {
        "schema_version": 1,
        "event": "saved",
        "material_id": mid,
        "source_name": metadata["source_name"],
        "source_path": metadata["source_path"],
        "source_video_fingerprint": source_fingerprint,
        "settings_fingerprint": settings_fp,
        "summary": summary_info["summary"],
        "tags": summary_info["tags"],
        "material_dir": str(dest),
        "updated_at": now,
    }
    lib.mkdir(parents=True, exist_ok=True)
    with (lib / "materials_index.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(index_record, ensure_ascii=False, separators=(",", ":")) + "\n")
    return metadata


def find_material_by_fingerprint(library_dir: str | Path, source_fingerprint: str) -> dict | None:
    root = Path(library_dir) / "materials"
    if not root.exists():
        return None
    candidates = []
    for meta_path in root.glob("*/material.json"):
        data = _load_json(meta_path)
        if isinstance(data, dict) and data.get("source_video_fingerprint") == source_fingerprint:
            data["material_dir"] = str(meta_path.parent)
            candidates.append(data)
    if not candidates:
        return None
    # Deterministic fallback policy for legacy/manual callers that do not know
    # the expected material_id: newest wins, then material_id for stable ties.
    candidates.sort(key=lambda d: (str(d.get("updated_at") or ""), str(d.get("material_id") or "")), reverse=True)
    return candidates[0]


def restore_material(
    library_dir: str | Path,
    work_dir: str | Path,
    *,
    source_fingerprint: str,
    settings_fp: str,
    material_id: str | None = None,
    overwrite: bool = True,
    prune_stale_allowed: bool = True,
) -> dict:
    """Restore allowed artifacts when fingerprint/settings match.

    Returns a status dict and never partially restores on mismatch.

    By default the restored material is treated as the authoritative analysis
    snapshot for ``work_dir``: allowed analysis artifacts are staged first, then
    stale allowed artifacts in the destination are pruned before replacement.
    This prevents reused work dirs from mixing old scenes/ASR/VLM files with a
    newly restored material. Non-allowed files (for example narration.json or
    clip_plan.json) are never removed here.
    """
    lib = Path(library_dir)
    if material_id:
        meta = _load_json(material_dir(lib, material_id) / "material.json")
        if isinstance(meta, dict):
            meta["material_dir"] = str(material_dir(lib, material_id))
    else:
        meta = find_material_by_fingerprint(lib, source_fingerprint)
    if not isinstance(meta, dict):
        return {"restored": False, "reason": "material not found"}
    if meta.get("source_video_fingerprint") != source_fingerprint:
        return {"restored": False, "reason": "source fingerprint mismatch", "material_id": meta.get("material_id")}
    if meta.get("settings_fingerprint") != settings_fp:
        return {"restored": False, "reason": "settings fingerprint mismatch", "material_id": meta.get("material_id")}

    src_dir = Path(meta.get("material_dir") or material_dir(lib, meta["material_id"])) / "artifacts"
    if not src_dir.exists():
        return {"restored": False, "reason": "material artifacts missing", "material_id": meta.get("material_id")}
    dest = Path(work_dir)
    dest.mkdir(parents=True, exist_ok=True)
    staged = []
    with tempfile.TemporaryDirectory(prefix=".material_restore_", dir=str(dest)) as tmp_name:
        tmp = Path(tmp_name)
        for artifact in meta.get("artifacts") or []:
            name = artifact.get("name") if isinstance(artifact, dict) else None
            if name not in ALLOWED_ARTIFACTS:
                continue
            src = src_dir / name
            if not src.exists():
                continue
            copy_artifact_redacted(src, tmp / name)
            staged.append(name)
        if not staged:
            return {
                "restored": False,
                "reason": "material artifacts empty",
                "material_id": meta.get("material_id"),
            }

        pruned = []
        if prune_stale_allowed:
            for name in sorted(ALLOWED_ARTIFACTS):
                out = dest / name
                if out.exists():
                    out.unlink()
                    pruned.append(name)

        restored = []
        for name in staged:
            out = dest / name
            if out.exists() and not overwrite:
                continue
            (tmp / name).replace(out)
            restored.append(name)
    return {
        "restored": bool(restored),
        "material_id": meta.get("material_id"),
        "artifacts": restored,
        "pruned_artifacts": pruned,
        "material": meta,
    }
