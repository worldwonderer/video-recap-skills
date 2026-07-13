"""Timeline validation and migration at the JianYing adapter boundary."""

import copy
import math


CURRENT_SCHEMA_VERSION = 2
RESOURCE_TRACK_KINDS = {
    "face_effect", "sound", "sticker", "text_template", "video_effect",
}
SUPPORTED_TRACK_KINDS = {"audio", "image", "text", "video"} | RESOURCE_TRACK_KINDS


def _error(path, expectation):
    raise ValueError(f"invalid timeline {path}: {expectation}")


def _is_number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _field_path(path, key):
    return f"{path}.{key}" if path else key


def _require_number(container, key, path, *, minimum=None):
    field_path = _field_path(path, key)
    if key not in container or not _is_number(container[key]):
        _error(field_path, "must be a finite number")
    value = container[key]
    if minimum is not None and value < minimum:
        _error(field_path, f"must be >= {minimum}")
    return value


def _require_string(container, key, path):
    value = container.get(key)
    if not isinstance(value, str) or not value:
        _error(_field_path(path, key), "must be a non-empty string")
    return value


def _validate_span(item, path, start_key="timeline_start", end_key="timeline_end"):
    start = _require_number(item, start_key, path, minimum=0)
    end = _require_number(item, end_key, path, minimum=0)
    if end <= start:
        _error(f"{path}.{end_key}", f"must be greater than {start_key}")


def _validate_video_clip(clip, path):
    if not isinstance(clip, dict):
        _error(path, "must be an object")
    _require_string(clip, "source_path", path)
    _validate_span(clip, path)
    _validate_span(clip, path, "source_start", "source_end")
    if "audio" in clip and not isinstance(clip["audio"], dict):
        _error(f"{path}.audio", "must be an object")
    if "speed" in clip:
        speed = _require_number(clip, "speed", path)
        if speed <= 0:
            _error(f"{path}.speed", "must be greater than 0")
        source_duration = float(clip["source_end"]) - float(clip["source_start"])
        target_duration = float(clip["timeline_end"]) - float(clip["timeline_start"])
        expected_source_duration = target_duration * float(speed)
        if not math.isclose(source_duration, expected_source_duration, rel_tol=1e-6, abs_tol=1e-4):
            _error(
                path,
                "source duration must equal target duration multiplied by speed "
                f"({source_duration} != {target_duration} * {speed})",
            )
    if "reverse" in clip and not isinstance(clip["reverse"], bool):
        _error(f"{path}.reverse", "must be a boolean")
    if clip.get("reverse"):
        _require_string(clip, "reverse_path", path)
    for field in ("scale", "position", "flip"):
        if field in clip and not isinstance(clip[field], dict):
            _error(f"{path}.{field}", "must be an object")
    for field in ("transition", "mask", "lut", "chroma"):
        if field in clip and not isinstance(clip[field], (dict, str)):
            _error(f"{path}.{field}", "must be an object or resource-package name")
    if "green_background" in clip and not isinstance(clip["green_background"], dict):
        _error(f"{path}.green_background", "must be a local media object")
    if "compound" in clip and not isinstance(clip["compound"], bool):
        _error(f"{path}.compound", "must be a boolean")


def _validate_segment(segment, path, kind):
    if not isinstance(segment, dict):
        _error(path, "must be an object")
    _validate_span(segment, path)
    if "speed" in segment:
        speed = _require_number(segment, "speed", path)
        if speed <= 0:
            _error(f"{path}.speed", "must be greater than 0")
    if kind in {"audio", "image"}:
        _require_string(segment, "source_path", path)
    elif kind == "text" and not isinstance(segment.get("text"), str):
        _error(f"{path}.text", "must be a string")
    elif kind in RESOURCE_TRACK_KINDS:
        sources = [key for key in ("material", "resource_config", "resource_package") if key in segment]
        if len(sources) != 1:
            _error(path, "must define exactly one of material, resource_config, or resource_package")
        source = segment[sources[0]]
        if sources[0] == "resource_package":
            if not isinstance(source, str) or not source:
                _error(f"{path}.resource_package", "must be a non-empty string")
        elif sources[0] == "material" and not isinstance(source, dict):
            _error(f"{path}.material", "must be an object")
        elif sources[0] == "resource_config" and not isinstance(source, (dict, str)):
            _error(f"{path}.resource_config", "must be an object or JSON file path")


def _validate_track(track, path):
    if not isinstance(track, dict):
        _error(path, "must be an object")
    kind = _require_string(track, "kind", path)
    if "name" in track and (not isinstance(track["name"], str) or not track["name"]):
        _error(f"{path}.name", "must be a non-empty string")

    if kind == "video":
        clips = track.get("clips")
        if not isinstance(clips, list):
            _error(f"{path}.clips", "must be an array")
        for index, clip in enumerate(clips):
            _validate_video_clip(clip, f"{path}.clips[{index}]")
        return

    if kind in {"audio", "image", "text"} | RESOURCE_TRACK_KINDS:
        segments = track.get("segments")
        if not isinstance(segments, list):
            _error(f"{path}.segments", "must be an array")
        if kind == "audio":
            if "role" in track and not isinstance(track["role"], str):
                _error(f"{path}.role", "must be a string")
            if "loop" in track and not isinstance(track["loop"], bool):
                _error(f"{path}.loop", "must be a boolean")
        for index, segment in enumerate(segments):
            _validate_segment(segment, f"{path}.segments[{index}]", kind)
        return

    _error(f"{path}.kind", f"unsupported track kind {kind!r}")


def _validate_v2(timeline):
    canvas = timeline.get("canvas")
    if not isinstance(canvas, dict):
        _error("canvas", "must be an object")
    for dimension in ("width", "height"):
        value = canvas.get(dimension)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            _error(f"canvas.{dimension}", "must be a positive integer")
    fps = _require_number(canvas, "fps", "canvas")
    if fps <= 0:
        _error("canvas.fps", "must be greater than 0")

    _require_number(timeline, "duration", "", minimum=0)
    for registry_name in ("resource_packages", "style_presets"):
        if registry_name in timeline and not isinstance(timeline[registry_name], dict):
            _error(registry_name, "must be an object")
    tracks = timeline.get("tracks")
    if not isinstance(tracks, list):
        _error("tracks", "must be an array")
    for index, track in enumerate(tracks):
        _validate_track(track, f"tracks[{index}]")


def normalize_timeline(timeline):
    """Return a validated schema-v2 copy, migrating schema v1 when necessary."""
    if not isinstance(timeline, dict):
        _error("root", "must be an object")
    schema_version = timeline.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        _error("schema_version", "must be integer 1 or 2")
    if schema_version not in {1, CURRENT_SCHEMA_VERSION}:
        raise ValueError(
            f"unsupported timeline schema_version {schema_version}; "
            f"supported versions are 1 and {CURRENT_SCHEMA_VERSION}"
        )

    normalized = copy.deepcopy(timeline)
    if schema_version == 1:
        # Schema v2 is an additive extension of v1 (local image tracks). The
        # migration therefore preserves all authored v1 fields and only advances
        # the version before applying the current contract.
        normalized["schema_version"] = CURRENT_SCHEMA_VERSION
    _validate_v2(normalized)
    return normalized
