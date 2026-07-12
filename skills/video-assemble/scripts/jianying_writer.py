"""Safe writer and portable media bundler for JianYing draft folders."""

import hashlib
import json
import os
import shutil
import tempfile
import time
import uuid


DRAFT_PATH_PLACEHOLDER = "##_draftpath_placeholder_0E685133-18CE-45ED-8CB8-2904A212EC80_##"


def validate_draft_name(draft_name):
    """Reject draft names that could escape or alias the requested parent dir."""
    if not isinstance(draft_name, str):
        raise TypeError("draft_name must be a string")
    if not draft_name or not draft_name.strip():
        raise ValueError("draft_name must not be empty")
    if os.path.isabs(draft_name):
        raise ValueError("draft_name must be a plain folder name, not an absolute path")
    if "/" in draft_name or "\\" in draft_name:
        raise ValueError("draft_name must not contain path separators")
    if draft_name in {".", ".."}:
        raise ValueError("draft_name must not be '.' or '..'")


def _resource_kind(material, materials_key):
    if materials_key == "audios":
        return "audio", "music"
    if material.get("type") == "photo":
        return "image", "photo"
    return "video", "video"


def _unused_name(directory, basename, used):
    """Return a collision-free filename within one resource directory."""
    name = basename
    stem, ext = os.path.splitext(basename)
    suffix = 1
    while name in used or os.path.exists(os.path.join(directory, name)):
        name = f"{stem}_{suffix}{ext}"
        suffix += 1
    used.add(name)
    return name


def _md5(path):
    digest = hashlib.md5(usedforsecurity=False)
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _meta_value(material, relative_path, metetype, copied_path, timestamp_ms):
    duration = int(material.get("duration") or 0)
    timestamp_s = timestamp_ms // 1000
    return {
        "duration": duration,
        "height": int(material.get("height") or 0),
        # duo-video indexes imported local resources independently from the
        # draft-content material IDs.
        "id": str(uuid.uuid4()).upper(),
        "md5": _md5(copied_path),
        "metetype": metetype,
        "type": 0,
        "width": int(material.get("width") or 0),
        "create_time": timestamp_s,
        "extra_info": os.path.basename(relative_path),
        "file_Path": f"./{relative_path}",
        "import_time": timestamp_s,
        "import_time_ms": timestamp_ms,
        "item_source": 1,
        "roughcut_time_range": {"duration": duration, "start": 0},
        "sub_time_range": {"duration": -1, "start": -1},
    }


def bundle_media(content, meta, draft_dir):
    """Copy media into duo-video's Resources/local contract and index it.

    Materials that reference the same source file and resource kind share one
    copied file and one meta entry. Missing sources remain untouched and are
    reported to the caller so a non-portable reference is never disguised as a
    successfully bundled one.
    """
    resources_root = os.path.join(draft_dir, "Resources", "local")
    copied = {}
    used = {kind: set() for kind in ("video", "audio", "image")}
    meta_values = []
    notes = []
    timestamp_ms = int(time.time() * 1000)

    for materials_key in ("videos", "audios"):
        for material in content["materials"][materials_key]:
            src = material.get("path")
            if not src:
                continue
            resource_kind, metetype = _resource_kind(material, materials_key)
            source_key = (resource_kind, os.path.abspath(src))
            existing = copied.get(source_key)
            if existing is not None:
                material["path"] = existing["draft_path"]
                continue
            if not os.path.isfile(src):
                notes.append(f"素材缺失，未打包: {src}")
                continue

            resource_dir = os.path.join(resources_root, resource_kind)
            os.makedirs(resource_dir, exist_ok=True)
            filename = _unused_name(resource_dir, os.path.basename(src), used[resource_kind])
            copied_path = os.path.join(resource_dir, filename)
            shutil.copy2(src, copied_path)

            relative_path = f"Resources/local/{resource_kind}/{filename}"
            draft_path = f"{DRAFT_PATH_PLACEHOLDER}/{relative_path}"
            material["path"] = draft_path
            copied[source_key] = {"draft_path": draft_path}
            meta_values.append(
                _meta_value(material, relative_path, metetype, copied_path, timestamp_ms)
            )

    material_group = next(group for group in meta["draft_materials"] if group["type"] == 0)
    material_group["value"] = meta_values
    meta["draft_timeline_materials_size_"] = sum(
        os.path.getsize(os.path.join(draft_dir, value["file_Path"][2:]))
        for value in meta_values
    )
    return notes


def draft_dir_has_user_content(draft_dir):
    """Return True when writing here could overwrite an existing draft/material."""
    if not os.path.exists(draft_dir):
        return False
    try:
        return any(os.scandir(draft_dir))
    except OSError:
        return True


def collision_safe_draft_dir(out_dir, draft_name):
    """Pick a fresh draft folder instead of overwriting an existing non-empty one."""
    validate_draft_name(draft_name)
    base = os.path.join(out_dir, draft_name)
    if not draft_dir_has_user_content(base):
        return base, draft_name
    idx = 2
    while True:
        candidate_name = f"{draft_name}_{idx}"
        candidate = os.path.join(out_dir, candidate_name)
        if not draft_dir_has_user_content(candidate):
            return candidate, candidate_name
        idx += 1


def write_draft(content, meta, notes, out_dir, draft_name, bundle_media_enabled=False):
    """Atomically write the three JianYing draft JSON files and optional bundle."""
    validate_draft_name(draft_name)
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    draft_dir, actual_name = collision_safe_draft_dir(out_dir, draft_name)
    notes = list(notes)
    if actual_name != draft_name:
        notes.append(f"草稿目录已存在，改写为 {actual_name} 以避免覆盖")

    tmp_parent = tempfile.mkdtemp(prefix=f".{actual_name}.", dir=out_dir)
    tmp_dir = os.path.join(tmp_parent, actual_name)
    try:
        os.makedirs(tmp_dir, exist_ok=False)
        if bundle_media_enabled:
            notes.extend(bundle_media(content, meta, tmp_dir))
        timestamp_ms = int(time.time() * 1000)
        meta["draft_name"] = actual_name
        meta["draft_fold_path"] = draft_dir
        meta["tm_draft_create"] = timestamp_ms
        meta["tm_draft_modified"] = timestamp_ms
        content["name"] = actual_name
        for fname in ("draft_content.json", "draft_info.json"):
            with open(os.path.join(tmp_dir, fname), "w", encoding="utf-8") as f:
                json.dump(content, f, ensure_ascii=False, indent=2)
        with open(os.path.join(tmp_dir, "draft_meta_info.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        if os.path.isdir(draft_dir) and not draft_dir_has_user_content(draft_dir):
            os.rmdir(draft_dir)
        os.replace(tmp_dir, draft_dir)
    except Exception:
        shutil.rmtree(tmp_parent, ignore_errors=True)
        raise
    finally:
        if os.path.exists(tmp_parent):
            shutil.rmtree(tmp_parent, ignore_errors=True)
    return draft_dir, notes
