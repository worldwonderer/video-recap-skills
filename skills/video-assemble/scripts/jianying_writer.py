"""Safe writer and portable media bundler for JianYing draft folders."""

import hashlib
import json
import os
import shutil
import tempfile
import time
import uuid
import zipfile

from jianying_templates import template


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


RESOURCE_DIRECTORY_BY_MATERIALS_KEY = {
    "chromas": "effect",
    "common_mask": "mask",
    "effects": "effect",
    "masks": "mask",
    "stickers": "sticker",
    "texts": "text",
    "text_templates": "text_template",
    "transitions": "transition",
    "video_effects": "effect",
}


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
    value = template("meta_material")
    value.update({
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
    })
    return value


def _material_sets(content):
    """Yield root and nested compound-draft material dictionaries."""
    materials = content.get("materials")
    if not isinstance(materials, dict):
        return
    yield materials
    for draft in materials.get("drafts", []):
        nested = draft.get("draft") if isinstance(draft, dict) else None
        if isinstance(nested, dict):
            yield from _material_sets(nested)


def _replace_value(value, old, new):
    if isinstance(value, dict):
        for key, item in value.items():
            value[key] = _replace_value(item, old, new)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            value[index] = _replace_value(item, old, new)
    elif isinstance(value, str):
        if value == old:
            return new
        stripped = value.lstrip()
        if stripped.startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return value
            replaced = _replace_value(parsed, old, new)
            return json.dumps(replaced, ensure_ascii=False, separators=(",", ":"))
    return value


def _safe_resource_target(target_path):
    normalized = os.path.normpath(str(target_path).replace("\\", "/"))
    if normalized in {"", "."} or os.path.isabs(normalized):
        raise ValueError(f"invalid JianYing resource target_path: {target_path}")
    if normalized == ".." or normalized.startswith("../"):
        raise ValueError(f"JianYing resource target_path escapes package: {target_path}")
    return normalized


def _extract_zip(source, destination):
    os.makedirs(destination, exist_ok=False)
    destination_real = os.path.realpath(destination)
    with zipfile.ZipFile(source) as archive:
        for member in archive.infolist():
            member_path = os.path.realpath(os.path.join(destination, member.filename))
            if os.path.commonpath((destination_real, member_path)) != destination_real:
                raise ValueError(f"unsafe path in JianYing resource archive: {member.filename}")
        archive.extractall(destination)


def _copy_resource(source, resource_kind, resources_root, used, target_path=None):
    resource_dir = os.path.join(resources_root, resource_kind)
    os.makedirs(resource_dir, exist_ok=True)
    is_zip = os.path.isfile(source) and zipfile.is_zipfile(source)
    if target_path is None:
        basename = os.path.basename(source.rstrip(os.sep))
        if is_zip:
            basename = os.path.splitext(basename)[0]
        relative_target = _unused_name(resource_dir, basename, used[resource_kind])
    else:
        relative_target = _safe_resource_target(target_path)
    copied_path = os.path.join(resource_dir, relative_target)
    copied_real = os.path.realpath(copied_path)
    if os.path.commonpath((os.path.realpath(resource_dir), copied_real)) != os.path.realpath(resource_dir):
        raise ValueError(f"JianYing resource target escapes package: {target_path}")
    os.makedirs(os.path.dirname(copied_path), exist_ok=True)
    if os.path.exists(copied_path):
        raise FileExistsError(f"duplicate JianYing resource target: {relative_target}")
    if is_zip:
        _extract_zip(source, copied_path)
    elif os.path.isdir(source):
        shutil.copytree(source, copied_path)
    else:
        shutil.copy2(source, copied_path)
    relative_path = f"Resources/local/{resource_kind}/{relative_target.replace(os.sep, '/')}"
    return copied_path, relative_path, f"{DRAFT_PATH_PLACEHOLDER}/{relative_path}"


def _is_packaged_path(value):
    return str(value).startswith((DRAFT_PATH_PLACEHOLDER, "Resources/", "./Resources/"))


def _descriptor(raw, default_kind, *, required):
    if isinstance(raw, str):
        source = raw
        resource_kind = default_kind
        target_path = None
    elif isinstance(raw, dict):
        source = raw.get("source_path") or raw.get("source")
        resource_kind = raw.get("resource_kind") or raw.get("kind") or default_kind
        target_path = raw.get("target_path")
    else:
        raise ValueError("JianYing resources entries must be paths or objects")
    if not isinstance(source, str) or not source:
        raise ValueError("JianYing resource source_path must be a non-empty string")
    if not isinstance(resource_kind, str) or resource_kind not in {
        "audio", "effect", "fonts", "image", "lut", "mask", "sticker",
        "text", "text_template", "transition", "video",
    }:
        raise ValueError(f"invalid JianYing resource kind: {resource_kind}")
    suffix = os.path.splitext(source)[1].lower()
    if suffix == ".cube":
        resource_kind = "lut"
    elif suffix in {".ttf", ".otf"}:
        resource_kind = "fonts"
    return {
        "source_path": source,
        "resource_kind": resource_kind,
        "target_path": target_path,
        "required": required,
    }


def _material_resource_descriptors(material, default_kind):
    descriptors = [
        _descriptor(raw, default_kind, required=True)
        for raw in material.get("_bundle_resources", [])
    ]
    path = material.get("path")
    if isinstance(path, str) and path and not _is_packaged_path(path):
        if not any(item["source_path"] == path for item in descriptors):
            descriptors.append(_descriptor(path, default_kind, required=False))
    return descriptors


def bundle_media(content, meta, draft_dir):
    """Copy media into duo-video's Resources/local contract and index it.

    Materials that reference the same source file and resource kind share one
    copied file and one meta entry. Missing sources remain untouched and are
    reported to the caller so a non-portable reference is never disguised as a
    successfully bundled one.
    """
    resources_root = os.path.join(draft_dir, "Resources", "local")
    copied = {}
    resource_kinds = {
        "audio", "effect", "fonts", "image", "lut", "mask", "sticker",
        "text", "text_template", "transition", "video",
    }
    used = {kind: set() for kind in resource_kinds}
    meta_values = []
    notes = []
    timestamp_ms = int(time.time() * 1000)

    material_sets = list(_material_sets(content))
    for materials in material_sets:
        for materials_key in ("videos", "audios"):
            for material in materials.get(materials_key, []):
                src = material.get("path")
                if not src:
                    continue
                resource_kind, metetype = _resource_kind(material, materials_key)
                source_key = (resource_kind, os.path.realpath(src))
                existing = copied.get(source_key)
                if existing is not None:
                    material["path"] = existing["draft_path"]
                    continue
                if not os.path.isfile(src):
                    if not _is_packaged_path(src):
                        notes.append(f"素材缺失，未打包: {src}")
                    continue

                copied_path, relative_path, draft_path = _copy_resource(
                    src, resource_kind, resources_root, used
                )
                material["path"] = draft_path
                copied[source_key] = {"draft_path": draft_path}
                meta_values.append(
                    _meta_value(material, relative_path, metetype, copied_path, timestamp_ms)
                )

        for materials_key, resource_kind in RESOURCE_DIRECTORY_BY_MATERIALS_KEY.items():
            for material in materials.get(materials_key, []):
                descriptors = _material_resource_descriptors(
                    material, resource_kind
                )
                seen_descriptors = set()
                for descriptor in descriptors:
                    src = descriptor["source_path"]
                    if _is_packaged_path(src):
                        continue
                    descriptor_key = (
                        descriptor["resource_kind"],
                        os.path.realpath(src),
                        descriptor["target_path"],
                    )
                    if descriptor_key in seen_descriptors:
                        continue
                    seen_descriptors.add(descriptor_key)
                    if not os.path.exists(src):
                        message = f"声明的剪映资源缺失: {src}"
                        if descriptor["required"]:
                            raise ValueError(message)
                        notes.append(message)
                        continue
                    kind = descriptor["resource_kind"]
                    source_key = (kind, os.path.realpath(src), descriptor["target_path"])
                    existing = copied.get(source_key)
                    if existing is None:
                        _copied_path, _relative_path, draft_path = _copy_resource(
                            src,
                            kind,
                            resources_root,
                            used,
                            target_path=descriptor["target_path"],
                        )
                        copied[source_key] = {"draft_path": draft_path}
                    else:
                        draft_path = existing["draft_path"]
                    _replace_value(material, src, draft_path)

    material_group = next(group for group in meta["draft_materials"] if group["type"] == 0)
    material_group["value"] = meta_values
    meta["draft_timeline_materials_size_"] = sum(
        os.path.getsize(os.path.join(draft_dir, value["file_Path"][2:]))
        for value in meta_values
    )
    return notes


def strip_internal_resource_fields(content):
    for materials in _material_sets(content):
        for entries in materials.values():
            if not isinstance(entries, list):
                continue
            for material in entries:
                if isinstance(material, dict):
                    material.pop("_bundle_resources", None)


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
        strip_internal_resource_fields(content)
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
