"""Safe writer and media bundler for JianYing draft folders."""

import json
import os
import shutil
import tempfile


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


def bundle_media(content, draft_dir):
    """Copy referenced media into `<draft_dir>/materials/` and rewrite paths."""
    mats_dir = os.path.join(draft_dir, "materials")
    os.makedirs(mats_dir, exist_ok=True)
    copied, used, notes = {}, set(), []
    for arr in (content["materials"]["videos"], content["materials"]["audios"]):
        for material in arr:
            src = material.get("path")
            if not src:
                continue
            if src in copied:
                material["path"] = copied[src]
                continue
            if not os.path.exists(src):
                notes.append(f"素材缺失，未打包: {src}")
                continue
            base = os.path.basename(src)
            name, stem_ext = base, os.path.splitext(base)
            i = 1
            while name in used:
                name = f"{stem_ext[0]}_{i}{stem_ext[1]}"
                i += 1
            used.add(name)
            dest = os.path.join(mats_dir, name)
            shutil.copy2(src, dest)
            copied[src] = dest
            material["path"] = dest
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


def rewrite_material_prefix(content, old_prefix, new_prefix):
    old_prefix = os.path.abspath(old_prefix)
    new_prefix = os.path.abspath(new_prefix)
    for arr in (content["materials"]["videos"], content["materials"]["audios"]):
        for material in arr:
            path = material.get("path")
            if path and os.path.abspath(path).startswith(old_prefix + os.sep):
                material["path"] = new_prefix + os.path.abspath(path)[len(old_prefix):]


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
            notes = notes + bundle_media(content, tmp_dir)
            rewrite_material_prefix(content, tmp_dir, draft_dir)
        meta["draft_name"] = actual_name
        meta["draft_fold_path"] = draft_dir
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
