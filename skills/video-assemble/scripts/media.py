"""Media probing and source-clip provenance for video-assemble."""

import json
import os
from pathlib import Path

from artifacts import _explicit_source_video, _value_fingerprint
from lib import log, run_cmd

def _load_cut_timeline_plan(work_dir):
    raw_plan_path = Path(work_dir) / "clip_plan.json"
    validated_plan_path = Path(work_dir) / "clip_plan_validated.json"
    if not validated_plan_path.exists():
        return json.loads(raw_plan_path.read_text(encoding="utf-8")) if raw_plan_path.exists() else None
    if not raw_plan_path.exists():
        return json.loads(validated_plan_path.read_text(encoding="utf-8"))
    raw_plan = json.loads(raw_plan_path.read_text(encoding="utf-8"))
    validated_plan = json.loads(validated_plan_path.read_text(encoding="utf-8"))
    if (
        isinstance(validated_plan, dict)
        and validated_plan.get("raw_plan_fingerprint") == _value_fingerprint(raw_plan)
    ):
        return validated_plan
    return raw_plan


def _ratio_to_float(value, default=1.0):
    value = str(value or "").strip()
    if not value or value in {"0:1", "0/1", "N/A"}:
        return default
    try:
        if ":" in value:
            num, den = value.split(":", 1)
        elif "/" in value:
            num, den = value.split("/", 1)
        else:
            return float(value)
        den_f = float(den or 0)
        return float(num) / den_f if den_f else default
    except (TypeError, ValueError, ZeroDivisionError):
        return default


def _fps_from_rate(value, default=30.0):
    try:
        if "/" in str(value):
            num, den = str(value).split("/", 1)
            den_f = float(den or 0)
            return round(float(num) / den_f, 3) if den_f else default
        return round(float(value), 3)
    except (TypeError, ValueError, ZeroDivisionError):
        return default


def _stream_rotation(stream):
    """Extract rotation from tags or side_data_list in ffprobe JSON."""
    if not isinstance(stream, dict):
        return 0
    for source in (
        (stream.get("tags") or {}).get("rotate"),
        stream.get("rotation"),
    ):
        if source not in (None, ""):
            try:
                return int(round(float(source))) % 360
            except (TypeError, ValueError):
                pass
    for item in stream.get("side_data_list") or []:
        if not isinstance(item, dict):
            continue
        for key in ("rotation", "displaymatrix"):
            if item.get(key) not in (None, ""):
                try:
                    return int(round(float(item.get(key)))) % 360
                except (TypeError, ValueError):
                    pass
    return 0


def _canvas_from_stream(stream, *, default_width=1280, default_height=720, default_fps=30.0):
    storage_w = int(stream.get("width") or default_width)
    storage_h = int(stream.get("height") or default_height)
    fps = _fps_from_rate(stream.get("r_frame_rate") or stream.get("avg_frame_rate"), default_fps)
    sar_text = stream.get("sample_aspect_ratio") or "1:1"
    dar_text = stream.get("display_aspect_ratio") or ""
    sar = _ratio_to_float(sar_text, 1.0)
    rotation = _stream_rotation(stream)

    display_w = max(1, int(round(storage_w * sar)))
    display_h = max(1, storage_h)
    if dar_text and dar_text not in {"0:1", "N/A"}:
        dar = _ratio_to_float(dar_text, 0.0)
        # ffprobe sources are not consistent: some report DAR before rotation
        # (landscape value > 1 for a 90° stream), while simple line mocks and
        # some containers report the already-rotated portrait DAR (< 1). Only
        # apply DAR before swapping when it describes the stored orientation.
        if dar > 0 and not (rotation in {90, 270} and dar < 1.0):
            # Preserve height and adjust width. This keeps legacy square-pixel landscape
            # byte-identical while honoring non-square pixel DAR metadata.
            display_w = max(1, int(round(display_h * dar)))
    if rotation in {90, 270}:
        display_w, display_h = display_h, display_w

    return {
        "width": display_w,
        "height": display_h,
        "fps": fps,
        "storage_width": storage_w,
        "storage_height": storage_h,
        "rotation": rotation,
        "sample_aspect_ratio": sar_text,
        "display_aspect_ratio": dar_text or f"{display_w}:{display_h}",
        "sar": sar_text,
        "dar": dar_text or f"{display_w}:{display_h}",
        "display_width": display_w,
        "display_height": display_h,
    }


def _probe_canvas(video_path, *, command_runner=run_cmd):
    """Return rotation/SAR/DAR-aware canvas facts for a video.

    ``width``/``height`` are the display canvas used by subtitle/overlay geometry.
    For legacy square-pixel landscape sources, these remain the raw storage dimensions.
    Extra storage/rotation/SAR/DAR fields are visual QC facts and are safe for callers
    that only consume width/height/fps.
    """
    defaults = {"width": 1280, "height": 720, "fps": 30.0}
    res = command_runner([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries",
        "stream=width,height,r_frame_rate,avg_frame_rate,sample_aspect_ratio,display_aspect_ratio:stream_tags=rotate:stream_side_data=rotation",
        "-of", "json", str(video_path),
    ])
    if res.returncode == 0:
        try:
            data = json.loads(res.stdout or "{}")
            stream = (data.get("streams") or [{}])[0]
            if isinstance(stream, dict) and stream:
                return _canvas_from_stream(stream)
        except (ValueError, TypeError, KeyError):
            pass
        # Tests and older mocks may still feed default=nw=1:nk=0 style lines.
        stream = {}
        for line in (res.stdout or "").splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            stream[key] = value
        if stream:
            return _canvas_from_stream(stream)
    return {
        **defaults,
        "storage_width": defaults["width"],
        "storage_height": defaults["height"],
        "rotation": 0,
        "sample_aspect_ratio": "1:1",
        "display_aspect_ratio": "16:9",
        "display_width": defaults["width"],
        "display_height": defaults["height"],
    }


def _has_audio_stream(video_path, *, command_runner=run_cmd):
    """Return True when the input has an audio stream usable as [0:a]."""
    result = command_runner([
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=index", "-of", "csv=p=0", str(video_path),
    ])
    return result.returncode == 0 and bool(result.stdout.strip())


def _build_video_clips(
    input_video,
    work_dir,
    duration_s,
    *,
    logger=log,
    source_video_getter=_explicit_source_video,
):
    """Video-track clips for the timeline.

    In cut mode each plan entry becomes a clip referencing the ORIGINAL source
    range. Multi-source validated plans carry per-clip source_path and do not
    require an explicit ambient --source-video.
    """
    explicit_source_video = source_video_getter()
    try:
        plan = _load_cut_timeline_plan(work_dir)
        if plan is not None:
            entries = plan.get("clips", plan) if isinstance(plan, dict) else plan
            # A plan is "multi-source" once any clip carries its own source_path; such
            # clips must never be silently dropped — a missing one is degraded in place.
            multi_source = any(
                isinstance(c, dict) and str(c.get("source_path") or "").strip() for c in entries
            )
            clips, cursor = [], 0.0
            for c in entries:
                per_clip_source = str(c.get("source_path") or "").strip()
                source_path = per_clip_source or explicit_source_video
                ss = float(c.get("source_start", c.get("start")))
                se = float(c.get("source_end", c.get("end")))
                timeline_start = c.get("output_start")
                timeline_end = c.get("output_end")
                dur = max(0.0, se - ss)
                if dur <= 0:
                    continue
                if timeline_start is None or timeline_end is None:
                    timeline_start = cursor
                    timeline_end = cursor + dur
                    cursor += dur
                else:
                    timeline_start = float(timeline_start)
                    timeline_end = float(timeline_end)
                    cursor = max(cursor, timeline_end)
                if not source_path or not os.path.exists(source_path):
                    if per_clip_source or multi_source:
                        # Degrade ONLY this clip — point it at the rendered cut for its own
                        # output window — and keep real provenance for every present source,
                        # instead of collapsing the whole multi-source timeline.
                        seg = max(0.0, float(timeline_end) - float(timeline_start))
                        logger(f"  时间线: source_path 不存在，该片段降级为剪后成片片段: {source_path or '(unset)'}")
                        clips.append({"source_id": c.get("source_id"),
                                      "source_path": str(input_video),
                                      "source_start": float(timeline_start),
                                      "source_end": float(timeline_start) + seg,
                                      "timeline_start": float(timeline_start),
                                      "timeline_end": float(timeline_end),
                                      "provenance_degraded": True,
                                      "provenance_reason": f"missing_source_path:{source_path or 'unset'}"})
                    continue
                clips.append({"source_id": c.get("source_id"),
                              "source_path": source_path, "source_start": ss,
                              "source_end": se, "timeline_start": timeline_start,
                              "timeline_end": timeline_end})
            if clips:
                return clips
    except (TypeError, ValueError, KeyError, OSError) as exc:
        logger(f"  时间线: clip_plan 解析失败，回退单片 ({exc})")
    return [{"source_path": str(input_video), "source_start": 0.0,
             "source_end": float(duration_s), "timeline_start": 0.0,
             "timeline_end": float(duration_s)}]
