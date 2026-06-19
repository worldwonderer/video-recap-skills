#!/usr/bin/env python3
"""Storyboard (contact-sheet) generation for video-understanding.

Two ADVISORY artifacts that help the writing agent orient on the timeline by SCANNING
one image instead of opening dozens of frames:

  - source_storyboard.{jpg,json}  — scene-anchored tiles over the SOURCE timeline.
  - edited_storyboard.{jpg,json}  — one row per kept clip over the cut OUTPUT timeline,
                                    each tile dual-labelled (output time / source time).

Both reuse the frames already extracted by understand.py (frames/frame_*.jpg at CONFIG["fps"]).
Nothing here re-extracts video. Every function returns dict|None and degrades to
None + log(...) on ANY failure (no frames, ffmpeg missing/non-zero, font probe raises),
so a storyboard quirk can NEVER block the pipeline (Principle 1: advisory, never blocking).

drawtext "mm:ss" labels are attempted when a usable font is found (labels_burned:true);
when no font is available (or drawtext errors) the sheet is still produced UNLABELLED
(labels_burned:false) and the JSON sidecar stays authoritative for all timestamps.
"""
import json
import math
import shutil
import subprocess
from pathlib import Path

from lib import CONFIG, run_cmd, log

try:  # file_fingerprint is the project's content-fingerprint helper; reuse when cheap.
    from lib import file_fingerprint
except ImportError:  # pragma: no cover - lib always ships it; degrade gracefully if not.
    file_fingerprint = None


# Candidate font files probed (in order) for burning mm:ss labels. The first that exists
# AND that drawtext can actually load wins. A probe that RAISES must never abort the sheet.
_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
)


def _fmt_mmss(seconds):
    """Format a timestamp as mm:ss (clamped to >= 0)."""
    total = int(round(max(0.0, float(seconds))))
    return f"{total // 60:02d}:{total % 60:02d}"


def _ffmpeg_available():
    return shutil.which("ffmpeg") is not None


def _probe_font():
    """Return a usable font file path, or None. Any exception → None (never raises out).

    Probe order: explicit candidate files first, then `fc-match` if available. The path
    is only returned when the file exists on disk; we do NOT shell out to drawtext here —
    a render-time drawtext error is caught separately and also degrades to unlabelled.
    """
    try:
        for candidate in _FONT_CANDIDATES:
            if Path(candidate).is_file():
                return candidate
        fc_match = shutil.which("fc-match")
        if fc_match:
            result = subprocess.run(
                [fc_match, "-f", "%{file}", "sans"],
                capture_output=True, text=True, timeout=10,
            )
            path = (result.stdout or "").strip()
            if result.returncode == 0 and path and Path(path).is_file():
                return path
    except Exception as exc:  # noqa: BLE001 - a font probe must NEVER abort the sheet
        log(f"storyboard 字体探测异常（降级为不烧时间戳）: {exc}")
        return None
    return None


def _frame_index(work_dir):
    """Return (sorted_frame_paths, sorted_numbers) for frames/frame_*.jpg, or ([], [])."""
    frames_dir = Path(work_dir) / "frames"
    if not frames_dir.is_dir():
        return [], []
    pairs = []
    for path in frames_dir.glob("frame_*.jpg"):
        stem_parts = path.stem.split("_")
        if len(stem_parts) != 2 or not stem_parts[1].isdigit():
            continue
        pairs.append((int(stem_parts[1]), path))
    pairs.sort(key=lambda item: item[0])
    numbers = [num for num, _ in pairs]
    paths = [path for _, path in pairs]
    return paths, numbers


def _nearest_existing_frame(timestamp, fps, paths, numbers):
    """Map a SOURCE timestamp → the nearest EXISTING frame file, clamped to [first,last].

    Frames are named frame_{n:05d}.jpg with t = n / fps (vlm.py convention). Rounding a
    timestamp blindly can yield a frame number that was never written (fps boundary / last
    frame gap), so we resolve to the closest number that actually exists on disk.
    """
    if not numbers:
        return None
    fps = float(fps)
    if fps <= 0:
        return None
    target = float(timestamp) * fps
    # Clamp into the real extracted range so out-of-range timestamps pin to first/last frame.
    if target <= numbers[0]:
        return paths[0]
    if target >= numbers[-1]:
        return paths[-1]
    # numbers is sorted; find the closest by absolute distance (ties → earlier frame).
    best_idx = 0
    best_dist = None
    for idx, num in enumerate(numbers):
        dist = abs(num - target)
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best_idx = idx
        elif num - target > best_dist:
            break  # sorted: distance only grows from here
    return paths[best_idx]


def _scene_anchor_timestamps(scenes, max_tiles):
    """Scene-anchored sample timestamps: each scene midpoint; long scenes also +1/3 & +2/3.

    Returns a list of (scene_id, timestamp). A scene is "long" when adding the thirds gives
    materially distinct sample points; we treat scenes longer than `long_scene_seconds` as
    long. Total is capped at max_tiles by evenly subsampling the ordered anchor list, so the
    sheet stays legible (D2: one bounded contact sheet, not dozens of frame reads).
    """
    long_scene_seconds = float(CONFIG.get("storyboard_long_scene_seconds", 6.0) or 6.0)
    anchors = []
    for scene_id, scene in enumerate(scenes or []):
        try:
            start = float(scene["start"])
            end = float(scene["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        mid = (start + end) / 2.0
        if (end - start) >= long_scene_seconds:
            third = start + (end - start) / 3.0
            two_third = start + 2.0 * (end - start) / 3.0
            points = [third, mid, two_third]
        else:
            points = [mid]
        for ts in points:
            anchors.append((scene_id, round(ts, 3)))
    if max_tiles and len(anchors) > max_tiles:
        # Evenly subsample to the cap, preserving timeline order and scene spread.
        step = len(anchors) / float(max_tiles)
        anchors = [anchors[int(i * step)] for i in range(max_tiles)]
    return anchors


def _file_fp(path):
    if file_fingerprint is None:
        return None
    try:
        return file_fingerprint(path)
    except (OSError, ValueError):
        return None


def _labelled_frame(frame_path, label, font_path, scratch_dir, out_name):
    """Burn `label` onto a copy of frame_path via drawtext; return the labelled path or None.

    None signals the caller to fall back to the original frame (and flip labels_burned off).
    A drawtext failure here is non-fatal: the unlabelled frame still tiles fine.
    """
    out_path = scratch_dir / out_name
    safe_label = label.replace("\\", "\\\\").replace(":", "\\:").replace("'", "’")
    drawtext = (
        f"drawtext=fontfile='{font_path}':text='{safe_label}':"
        "x=8:y=8:fontsize=28:fontcolor=white:"
        "box=1:boxcolor=black@0.55:boxborderw=6"
    )
    cmd = ["ffmpeg", "-y", "-i", str(frame_path), "-vf", drawtext, "-frames:v", "1", str(out_path)]
    try:
        result = run_cmd(cmd)
    except Exception as exc:  # noqa: BLE001 - a label render must never abort the sheet
        log(f"storyboard drawtext 异常（降级为不烧时间戳）: {exc}")
        return None
    if result.returncode != 0 or not out_path.exists():
        return None
    return out_path


def _tile_pages(frame_paths, columns, out_dir, out_stem, scratch_dir):
    """Tile frame_paths into one or more contact-sheet pages; return the list of page paths.

    ffmpeg's tile filter lays one grid per page. We page so each sheet holds at most
    columns*rows tiles where rows is chosen to keep the grid roughly square but capped, then
    spill into _001.jpg, _002.jpg… Returns [] on any ffmpeg failure (caller degrades to None).
    """
    columns = max(1, int(columns))
    rows_per_page = max(1, int(CONFIG.get("storyboard_rows_per_page", 5) or 5))
    per_page = columns * rows_per_page
    pages = []
    total = len(frame_paths)
    page_count = max(1, math.ceil(total / per_page))
    for page_idx in range(page_count):
        chunk = frame_paths[page_idx * per_page:(page_idx + 1) * per_page]
        if not chunk:
            continue
        cols = min(columns, len(chunk))
        rows = max(1, math.ceil(len(chunk) / cols))
        if page_count == 1:
            page_path = out_dir / f"{out_stem}.jpg"
        else:
            page_path = out_dir / f"{out_stem}_{page_idx + 1:03d}.jpg"
        # Stage the chunk as a contiguous numbered sequence so ffmpeg's image2 demuxer +
        # tile filter consume EXACTLY these frames (the source frame numbers are sparse).
        seq_dir = scratch_dir / f"seq_{out_stem}_{page_idx:03d}"
        seq_dir.mkdir(parents=True, exist_ok=True)
        for seq_idx, src in enumerate(chunk):
            shutil.copyfile(src, seq_dir / f"f_{seq_idx:05d}.jpg")
        cmd = [
            "ffmpeg", "-y",
            "-framerate", "1",
            "-i", str(seq_dir / "f_%05d.jpg"),
            "-frames:v", "1",
            "-vf", f"tile={cols}x{rows}",
            str(page_path),
        ]
        try:
            result = run_cmd(cmd)
        except Exception as exc:  # noqa: BLE001
            log(f"storyboard tile 异常: {exc}")
            return []
        if result.returncode != 0 or not page_path.exists():
            log(f"storyboard tile 失败: {getattr(result, 'stderr', '')[-300:]}")
            return []
        pages.append(page_path)
    return pages


def _render_storyboard(work_dir, tiles, out_stem, fps):
    """Shared render path: optionally burn labels, tile to pages, return (page_paths, labels_burned).

    `tiles` is a list of dicts that ALREADY carry a resolved `frame_file` (absolute path) and a
    `label` string. Returns (None, _) on hard failure so callers degrade to None.
    """
    if not _ffmpeg_available():
        log("storyboard 跳过：未找到 ffmpeg")
        return None, False
    storyboard_dir = Path(work_dir) / "storyboard"
    storyboard_dir.mkdir(parents=True, exist_ok=True)
    scratch_dir = storyboard_dir / f".scratch_{out_stem}"
    if scratch_dir.exists():
        shutil.rmtree(scratch_dir, ignore_errors=True)
    scratch_dir.mkdir(parents=True, exist_ok=True)

    font_path = _probe_font()
    labels_burned = bool(font_path)
    render_frames = []
    if labels_burned:
        for idx, tile in enumerate(tiles):
            labelled = _labelled_frame(
                Path(tile["frame_file"]), tile["label"], font_path, scratch_dir,
                f"lbl_{out_stem}_{idx:05d}.jpg",
            )
            if labelled is None:
                # First failure → abandon labelling entirely so the WHOLE sheet is consistent
                # (no half-labelled pages). The JSON sidecar still carries every timestamp.
                labels_burned = False
                break
            render_frames.append(labelled)
    if not labels_burned:
        render_frames = [Path(tile["frame_file"]) for tile in tiles]

    try:
        pages = _tile_pages(
            render_frames, CONFIG.get("storyboard_columns", 6),
            storyboard_dir, out_stem, scratch_dir,
        )
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)
    if not pages:
        return None, labels_burned
    return pages, labels_burned


def build_source_storyboard(work_dir, video_path, scenes, fps):
    """Scene-anchored SOURCE-timeline contact sheet. Returns dict|None.

    Samples each scene midpoint (long scenes also +1/3,+2/3), maps every sample to the
    nearest EXISTING extracted frame (clamped), tiles them, and writes
    storyboard/source_storyboard.json. Returns None + log on any failure.
    """
    try:
        work_dir = Path(work_dir)
        paths, numbers = _frame_index(work_dir)
        if not paths:
            log("storyboard 跳过 source：frames/ 为空或缺失")
            return None
        max_tiles = int(CONFIG.get("storyboard_max_tiles", 30) or 30)
        columns = int(CONFIG.get("storyboard_columns", 6) or 6)
        anchors = _scene_anchor_timestamps(scenes, max_tiles)
        if not anchors:
            log("storyboard 跳过 source：无可用场景锚点")
            return None

        tiles = []
        for tile_id, (scene_id, ts) in enumerate(anchors):
            frame = _nearest_existing_frame(ts, fps, paths, numbers)
            if frame is None:
                continue
            tiles.append({
                "tile_id": tile_id,
                "timestamp": round(float(ts), 3),
                "label": _fmt_mmss(ts),
                "scene_id": scene_id,
                "frame_file": str(frame),
            })
        if not tiles:
            log("storyboard 跳过 source：未解析到任何帧")
            return None

        pages, labels_burned = _render_storyboard(work_dir, tiles, "source_storyboard", fps)
        if not pages:
            log("storyboard 跳过 source：拼贴失败")
            return None

        for tile in tiles:
            tile["frame_file"] = Path(tile["frame_file"]).name
        payload = {
            "schema_version": 1,
            "timeline": "source",
            "video_path": str(video_path),
            "video_fingerprint": _file_fp(video_path),
            "fps": float(fps) if fps else None,
            "labels_burned": labels_burned,
            "page_images": [str(p) for p in pages],
            "sample_policy": {
                "max_tiles": max_tiles,
                "columns": columns,
                "anchors": "scene_midpoint+long_scene_thirds",
            },
            "tiles": tiles,
        }
        duration = get_video_duration_safe(video_path)
        if duration:
            payload["duration"] = round(duration, 3)
        json_path = work_dir / "storyboard" / "source_storyboard.json"
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"storyboard source: {len(tiles)} tiles → {len(pages)} page(s), labels_burned={labels_burned}")
        return payload
    except Exception as exc:  # noqa: BLE001 - advisory: never propagate
        log(f"storyboard source 失败（忽略）: {exc}")
        return None


def get_video_duration_safe(video_path):
    """Best-effort source duration via ffprobe; returns None on any failure (advisory)."""
    if not shutil.which("ffprobe"):
        return None
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(video_path)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return None
        return float((result.stdout or "").strip())
    except (ValueError, OSError, subprocess.SubprocessError):
        return None


def _clip_plan_clips(clip_plan_validated):
    """Return the clips list from a validated plan (list or {"clips":[...]})."""
    if isinstance(clip_plan_validated, dict):
        clips = clip_plan_validated.get("clips", [])
    else:
        clips = clip_plan_validated
    return clips if isinstance(clips, list) else []


def _source_to_output(source_time, clip):
    """Forward affine source→output map for ONE clip (reimplements cut.py:319 locally).

    output = clip.output_start + (src − clip.source_start), clamped to [output_start, output_end].
    Read the authoritative numbers from clip_plan_validated.json; do NOT import cut.py.
    """
    src = float(source_time)
    out_start = float(clip["output_start"])
    out_end = float(clip["output_end"])
    mapped = out_start + (src - float(clip["source_start"]))
    return round(max(out_start, min(mapped, out_end)), 3)


def build_edited_storyboard(work_dir, source_video_path, clip_plan_validated, fps):
    """OUTPUT-timeline contact sheet: one row per kept clip over the cut. Returns dict|None.

    For each kept clip, samples source start / mid / (end − 0.5s), maps each to the nearest
    EXISTING SOURCE frame (SOURCE fps — frames are reused, NO re-extraction), and FRAME-IDENTITY
    dedupes so a ≤1s clip yields 1-2 tiles (not 3 identical). Each tile is dual-labelled with
    both `output_timestamp` and `source_timestamp` (+ `source_clip_id`). Writes
    storyboard/edited_storyboard.json. Returns None + log on any failure.
    """
    try:
        work_dir = Path(work_dir)
        paths, numbers = _frame_index(work_dir)
        if not paths:
            log("storyboard 跳过 edited：frames/ 为空或缺失")
            return None
        clips = _clip_plan_clips(clip_plan_validated)
        if not clips:
            log("storyboard 跳过 edited：clip_plan_validated 无 clips")
            return None
        columns = int(CONFIG.get("storyboard_columns", 6) or 6)
        max_tiles = int(CONFIG.get("storyboard_max_tiles", 30) or 30)

        tiles = []
        seen_frames = set()  # frame-identity dedupe (NOT luma de-dupe; that stays deferred)
        tile_id = 0
        for clip in clips:
            try:
                source_start = float(clip["source_start"])
                source_end = float(clip["source_end"])
                clip_id = clip.get("clip_id")
            except (KeyError, TypeError, ValueError):
                continue
            if source_end <= source_start:
                continue
            mid = (source_start + source_end) / 2.0
            end_sample = max(source_start, source_end - 0.5)
            for src_ts in (source_start, mid, end_sample):
                frame = _nearest_existing_frame(src_ts, fps, paths, numbers)
                if frame is None:
                    continue
                key = (clip_id, str(frame))
                if key in seen_frames:
                    continue  # same clip resolving to the same frame → drop the duplicate tile
                seen_frames.add(key)
                out_ts = _source_to_output(src_ts, clip)
                tiles.append({
                    "tile_id": tile_id,
                    "output_timestamp": out_ts,
                    "source_timestamp": round(float(src_ts), 3),
                    "source_clip_id": clip_id,
                    "label": f"out {_fmt_mmss(out_ts)} / src {_fmt_mmss(src_ts)}",
                    "frame_file": str(frame),
                })
                tile_id += 1
        if not tiles:
            log("storyboard 跳过 edited：未解析到任何帧")
            return None
        if len(tiles) > max_tiles:
            tiles = tiles[:max_tiles]

        pages, labels_burned = _render_storyboard(work_dir, tiles, "edited_storyboard", fps)
        if not pages:
            log("storyboard 跳过 edited：拼贴失败")
            return None

        for tile in tiles:
            tile["frame_file"] = Path(tile["frame_file"]).name
        edited_source = Path(work_dir) / "edited_source.mp4"
        payload = {
            "schema_version": 1,
            "timeline": "output",
            "source_video_path": str(source_video_path),
            "edited_video_path": str(edited_source) if edited_source.exists() else None,
            "clip_plan_fingerprint": _clip_plan_fingerprint(clip_plan_validated),
            "labels_burned": labels_burned,
            "page_images": [str(p) for p in pages],
            "sample_policy": {
                "max_tiles": max_tiles,
                "columns": columns,
                "per_clip": "source_start+mid+(end-0.5s), frame-identity deduped",
            },
            "tiles": tiles,
        }
        json_path = work_dir / "storyboard" / "edited_storyboard.json"
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"storyboard edited: {len(tiles)} tiles → {len(pages)} page(s), labels_burned={labels_burned}")
        return payload
    except Exception as exc:  # noqa: BLE001 - advisory: never propagate
        log(f"storyboard edited 失败（忽略）: {exc}")
        return None


def _clip_plan_fingerprint(clip_plan_validated):
    """Stable fingerprint of the validated plan that drives the edited tiles."""
    try:
        return _stable_hash(clip_plan_validated)
    except Exception:  # noqa: BLE001
        return None


def _stable_hash(value):
    import hashlib
    blob = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.md5(blob.encode("utf-8")).hexdigest()
