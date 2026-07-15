"""Probe display geometry and select a stable output canvas."""

import json


from lib import run_cmd


def _has_audio_stream(video_path):
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=index",
        "-of",
        "csv=p=0",
        str(video_path),
    ]
    result = run_cmd(cmd)
    return result.returncode == 0 and bool(result.stdout.strip())


class VideoGeometry(tuple):
    """Tuple-compatible geometry with probe facts attached for QC callers."""

    def __new__(cls, width, height, fps, facts=None):
        obj = super().__new__(cls, (width, height, fps))
        obj.facts = facts or {}
        return obj


def _parse_ratio(value):
    if value in (None, "", "0:1", "0/1", "N/A"):
        return None
    text = str(value)
    sep = ":" if ":" in text else "/" if "/" in text else None
    if not sep:
        try:
            ratio = float(text)
            return ratio if ratio > 0 else None
        except ValueError:
            return None
    left, _, right = text.partition(sep)
    try:
        num, den = float(left), float(right)
    except ValueError:
        return None
    return num / den if den > 0 and num > 0 else None


def _stream_rotation(stream):
    candidates = []
    tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
    if "rotate" in tags:
        candidates.append(tags.get("rotate"))
    for side_data in stream.get("side_data_list") or []:
        if isinstance(side_data, dict):
            candidates.append(side_data.get("rotation"))
    for value in candidates:
        try:
            return int(round(float(value))) % 360
        except (TypeError, ValueError):
            continue
    return 0


def _fps_from_rate(rate):
    if not rate or "/" not in str(rate):
        return 0.0
    num, _, den = str(rate).partition("/")
    try:
        num_f, den_f = float(num), float(den)
        return num_f / den_f if den_f > 0 else 0.0
    except ValueError:
        return 0.0


def _geometry_from_stream(stream, *, fallback=False):
    coded_width = coded_height = 0
    try:
        coded_width = int(float(stream.get("width") or 0))
        coded_height = int(float(stream.get("height") or 0))
    except (TypeError, ValueError):
        coded_width = coded_height = 0
    if coded_width <= 0 or coded_height <= 0:
        coded_width, coded_height = 1280, 720
        fallback = True

    parsed_sar = _parse_ratio(stream.get("sample_aspect_ratio"))
    dar = _parse_ratio(stream.get("display_aspect_ratio"))
    rotation = _stream_rotation(stream)
    display_height = float(coded_height)
    if parsed_sar:
        sar = parsed_sar
        display_width = float(coded_width) * sar
        aspect_source = "sample_aspect_ratio"
    elif dar:
        sar = 1.0
        display_width = display_height * dar
        aspect_source = "display_aspect_ratio_fallback"
    else:
        sar = 1.0
        display_width = float(coded_width)
        aspect_source = "square_pixel_fallback"
    rotation_swaps_axes = rotation in {90, 270}
    if rotation_swaps_axes:
        display_width, display_height = display_height, display_width

    width, height = _clamp_even_geometry(round(display_width), round(display_height))
    fps = _fps_from_rate(stream.get("r_frame_rate")) or _fps_from_rate(
        stream.get("avg_frame_rate")
    )
    if not 0 < fps <= 120:
        fps = 30.0
    facts = {
        "coded_width": coded_width,
        "coded_height": coded_height,
        "width": width,
        "height": height,
        "fps": round(fps, 3),
        "sample_aspect_ratio": stream.get("sample_aspect_ratio") or "1:1",
        "sample_aspect_ratio_float": round(float(sar), 6),
        "display_aspect_ratio": stream.get("display_aspect_ratio"),
        "display_aspect_ratio_float": round(float(dar or 0.0), 6),
        "display_aspect_source": aspect_source,
        "display_width": width,
        "display_height": height,
        "rotation": rotation,
        "rotation_swaps_axes": rotation_swaps_axes,
        "fallback": bool(fallback),
    }
    return VideoGeometry(width, height, round(fps, 3), facts)


def _probe_video_geometry(video_path):
    """Best-effort iterable (width, height, fps), rotation/SAR/DAR-aware.

    Returned value unpacks like the historical 3-tuple while exposing `.facts`
    for QC. Used to normalize heterogeneous multi-source segments to one
    square-pixel geometry before concat (ffmpeg's concat filter rejects
    mismatched width/height/SAR/pixel-format/fps).
    """
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,r_frame_rate,avg_frame_rate,sample_aspect_ratio,display_aspect_ratio:stream_tags=rotate:stream_side_data=rotation",
        "-of",
        "json",
        str(video_path),
    ]
    try:
        result = run_cmd(cmd)
    except Exception:  # noqa: BLE001 - probing is best-effort; fall back to defaults
        result = None
    if (
        result is not None
        and getattr(result, "returncode", 1) == 0
        and (result.stdout or "").strip()
    ):
        try:
            payload = json.loads(result.stdout)
            streams = payload.get("streams") or []
            if streams:
                return _geometry_from_stream(streams[0])
        except (AttributeError, TypeError, ValueError):
            pass

    # Backward-compatible fallback for tests/mocks that still return CSV output.
    stream = {}
    if result is not None and (result.stdout or "").strip():
        parts = result.stdout.strip().splitlines()[0].split(",")
        if len(parts) >= 2:
            stream["width"], stream["height"] = parts[0], parts[1]
        if len(parts) >= 3:
            stream["r_frame_rate"] = parts[2]
        if len(parts) >= 4 and parts[3] not in ("", "N/A"):
            stream["tags"] = {"rotate": parts[3]}
        if len(parts) >= 5 and parts[4] not in ("", "N/A"):
            stream["sample_aspect_ratio"] = parts[4]
        if len(parts) >= 6 and parts[5] not in ("", "N/A"):
            stream["display_aspect_ratio"] = parts[5]
    return _geometry_from_stream(stream, fallback=True)


def _orientation(width, height):
    if width > height:
        return "landscape"
    if height > width:
        return "portrait"
    return "square"


def _fps_bucket(fps):
    if not fps or fps <= 0:
        return 30.0
    common = [23.976, 24.0, 25.0, 29.97, 30.0, 50.0, 59.94, 60.0]
    nearest = min(common, key=lambda x: abs(float(fps) - x))
    return nearest if abs(float(fps) - nearest) <= 0.15 else round(float(fps))


def _clamp_even_geometry(width, height, max_height=None):
    width = max(2, int(width) - int(width) % 2)
    height = max(2, int(height) - int(height) % 2)
    max_height = int(max_height or 0)
    if max_height > 0 and height > max_height:
        scale = max_height / height
        height = max_height - max_height % 2
        width = max(2, int(width * scale))
        width -= width % 2
    return width, height


def _select_output_geometry(source_paths, clips, max_height=None):
    """Deterministically select canvas/fps from all used sources, not just the first."""
    used = {}
    for clip in clips or []:
        path = str(clip.get("source_path") or "")
        if not path:
            continue
        used[path] = used.get(path, 0.0) + max(0.0, float(clip.get("duration") or 0.0))
    if not used:
        for path in source_paths or []:
            used[str(path)] = 0.0
    rows = []
    for path in sorted(used):
        probed = _probe_video_geometry(path)
        width, height, fps = probed
        facts = dict(getattr(probed, "facts", {}) or {})
        facts.setdefault("width", width)
        facts.setdefault("height", height)
        facts.setdefault("fps", fps)
        facts.setdefault("rotation", 0)
        facts.setdefault("sample_aspect_ratio", "1:1")
        rows.append(
            {
                "path": path,
                "source_id": next(
                    (
                        str(c.get("source_id"))
                        for c in clips or []
                        if str(c.get("source_path") or "") == path
                        and c.get("source_id") is not None
                    ),
                    None,
                ),
                "used_duration": round(used[path], 3),
                "width": width,
                "height": height,
                "coded_width": facts.get("coded_width", width),
                "coded_height": facts.get("coded_height", height),
                "display_width": facts.get("display_width", width),
                "display_height": facts.get("display_height", height),
                "area": width * height,
                "fps": fps,
                "fps_bucket": min(60.0, _fps_bucket(fps)),
                "orientation": _orientation(width, height),
                "rotation": facts.get("rotation", 0),
                "sample_aspect_ratio": facts.get("sample_aspect_ratio", "1:1"),
                "sample_aspect_ratio_float": facts.get(
                    "sample_aspect_ratio_float", 1.0
                ),
                "display_aspect_ratio": facts.get("display_aspect_ratio"),
                "rotation_swaps_axes": bool(facts.get("rotation_swaps_axes", False)),
            }
        )
    if not rows:
        return (
            1280,
            720,
            30.0,
            {
                "width": 1280,
                "height": 720,
                "fps": 30.0,
                "reason": "fallback_no_sources",
                "source_id": None,
            },
        )

    orientation_duration = {}
    for row in rows:
        orientation_duration[row["orientation"]] = (
            orientation_duration.get(row["orientation"], 0.0) + row["used_duration"]
        )
    chosen_orientation = sorted(
        orientation_duration.items(),
        key=lambda kv: (
            kv[1],
            max(r["area"] for r in rows if r["orientation"] == kv[0]),
            kv[0],
        ),
        reverse=True,
    )[0][0]
    eligible = [r for r in rows if r["orientation"] == chosen_orientation] or rows
    selected = sorted(
        eligible,
        key=lambda r: (-r["area"], str(r.get("source_id") or ""), r["path"]),
    )[0]

    fps_duration = {}
    for row in rows:
        fps_duration[row["fps_bucket"]] = (
            fps_duration.get(row["fps_bucket"], 0.0) + row["used_duration"]
        )
    fps = sorted(fps_duration.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)[0][
        0
    ]
    fps = max(1.0, min(60.0, float(fps or 30.0)))

    width, height = _clamp_even_geometry(
        selected["width"], selected["height"], max_height=max_height
    )
    reason = {
        "width": width,
        "height": height,
        "fps": round(fps, 3),
        "reason": "weighted_orientation_area_fps",
        "source_id": selected.get("source_id"),
        "source_path": selected["path"],
        "orientation": chosen_orientation,
        "orientation_used_duration": round(
            orientation_duration.get(chosen_orientation, 0.0), 3
        ),
        "fps_bucket_used_duration": round(fps_duration.get(fps, 0.0), 3),
        "rotation": selected.get("rotation", 0),
        "sample_aspect_ratio": selected.get("sample_aspect_ratio", "1:1"),
        "display_aspect_ratio": selected.get("display_aspect_ratio"),
        "coded_width": selected.get("coded_width"),
        "coded_height": selected.get("coded_height"),
        "display_width": selected.get("display_width"),
        "display_height": selected.get("display_height"),
        "sources": rows,
    }
    return width, height, round(fps, 3), reason
