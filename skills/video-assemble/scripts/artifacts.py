"""Artifact fingerprints and work-directory JSON helpers for video-assemble."""

import hashlib
import json
from pathlib import Path

from lib import CONFIG

def _stable_json_dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _value_fingerprint(value):
    return hashlib.md5(_stable_json_dumps(value).encode("utf-8")).hexdigest()


def _file_fingerprint(path, chunk_size=1024 * 1024):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def _artifact_fingerprint(path):
    path = Path(path)
    return _file_fingerprint(path) if path.exists() else None


def _explicit_source_video():
    """Return the cut-mode source video only when the caller opted in explicitly."""
    if not CONFIG.get("source_video_explicit", False):
        return ""
    return str(CONFIG.get("source_video", "") or "").strip()


def _source_video_identity():
    source_video = _explicit_source_video()
    if not source_video:
        return None, None
    path = Path(source_video)
    return str(path.resolve()), _artifact_fingerprint(path)


def _timeline_provenance_status(work_dir):
    data = _load_work_json(work_dir, "timeline.json")
    if not isinstance(data, dict):
        return None
    provenance = data.get("provenance")
    return provenance if isinstance(provenance, dict) else None


def _load_work_json(work_dir, name):
    path = Path(work_dir) / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
