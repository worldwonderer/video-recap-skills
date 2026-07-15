"""Visual overlays, subtitle layout QC, masking, and video filter helpers."""

import json
import re
from pathlib import Path

from artifacts import _artifact_fingerprint
from assemble_constants import (
    SUBTITLE_STYLE_REF_H,
    VISUAL_OVERLAYS,
    VISUAL_QC,
    _SUPPORTED_VISUAL_OVERLAY_TYPES,
    _VISUAL_DELIVERY_FORBIDDEN_KEYS,
)
from audio_automation import coalesce_duck_windows
from audio_mix import _seg_place_window
from lib import CONFIG
from source_subtitles import (
    _combined_subtitle_entries,
    _original_gap_subtitle_entries,
    _source_subtitle_mask_policy,
)
from subtitle_core import (
    _measured_subtitle_safe_area,
    _normalize_subtitle_text,
    _style_for_measured_subtitle_band,
    _subtitle_style_config,
    _validate_measured_subtitle_coordinate_domain,
)

def _visual_text_units(text):
    """Approximate visual text width in em units for deterministic geometry QC."""
    units = 0.0
    for ch in str(text or ""):
        if ch.isspace():
            units += 0.35
        elif ord(ch) < 128:
            units += 0.56
        else:
            units += 1.0
    return units


def _subtitle_layout_qc(entries, canvas=None, style=None, safe_area=None):
    """Machine-check subtitle safe-area/multiline/overflow facts for visual_qc.json."""
    canvas = canvas or {}
    style = style or _subtitle_style_config(canvas)
    play_x = int(style.get("play_res_x") or canvas.get("width") or 1280)
    play_y = int(style.get("play_res_y") or canvas.get("height") or 720)
    margin_l = int(style.get("margin_l") or 0)
    margin_r = int(style.get("margin_r") or 0)
    margin_v = int(style.get("margin_v") or 0)
    font_size = float(style.get("font_size") or 1)
    max_lines = int(CONFIG.get("subtitle_max_lines", 2) or 2)
    usable_w = max(1.0, play_x - margin_l - margin_r)
    if safe_area:
        safe_area = {
            "x": int(safe_area.get("x", margin_l) or 0),
            "y": int(safe_area.get("y", margin_v) or 0),
            "width": int(safe_area.get("width", safe_area.get("w", play_x - margin_l - margin_r)) or 1),
            "height": int(safe_area.get("height", safe_area.get("h", play_y - 2 * margin_v)) or 1),
            "bottom_margin": margin_v,
        }
        usable_w = max(1.0, float(safe_area["width"]))
    else:
        safe_area = {
        "x": margin_l,
        "y": margin_v,
        "width": max(1, play_x - margin_l - margin_r),
        "height": max(1, play_y - 2 * margin_v),
        "bottom_margin": margin_v,
        }
    line_h = font_size * 1.25
    overflow_entries = []
    violations = []
    multi_line_entries = []
    max_observed_lines = 0
    entry_facts = []
    for i, entry in enumerate(entries or []):
        raw_text = _normalize_subtitle_text(entry.get("text", ""))
        lines = [ln for ln in re.split(r"(?:\\N|\n)+", raw_text) if ln != ""]
        if not lines:
            lines = [""]
        line_count = len(lines)
        max_observed_lines = max(max_observed_lines, line_count)
        widths = [_visual_text_units(line) * font_size for line in lines]
        max_w = max(widths or [0.0])
        band_h = line_count * line_h + float(style.get("outline", 0)) * 2 + float(style.get("shadow", 0))
        overflow_reasons = []
        if line_count > max_lines:
            overflow_reasons.append("max_lines_exceeded")
        if max_w > usable_w + 1e-6:
            overflow_reasons.append("safe_width_exceeded")
        if band_h > safe_area["height"] + 1e-6:
            overflow_reasons.append("safe_height_exceeded")
        fact = {
            "index": i,
            "start": round(float(entry.get("start", 0.0) or 0.0), 3),
            "end": round(float(entry.get("end", 0.0) or 0.0), 3),
            "line_count": line_count,
            "max_line_width": round(max_w, 2),
            "safe_width": round(usable_w, 2),
            "band_height": round(band_h, 2),
            "overflow": bool(overflow_reasons),
            "overflow_reasons": overflow_reasons,
        }
        entry_facts.append(fact)
        if line_count > 1:
            multi_line_entries.append(i)
        if overflow_reasons:
            overflow_entries.append(fact)
            for reason in overflow_reasons:
                kind = {
                    "max_lines_exceeded": "line_count",
                    "safe_width_exceeded": "line_width",
                    "safe_height_exceeded": "safe_area",
                }.get(reason, "safe_area")
                violations.append({"index": i, "kind": kind, "reason": reason})
    return {
        "enabled": bool(CONFIG.get("burn_subtitles", False)),
        "renderer": "ass" if CONFIG.get("burn_subtitles", False) else "sidecar_srt",
        "style": {
            "font_size": int(font_size),
            "max_chars": int(style.get("max_chars") or 0),
            "max_lines": max_lines,
            "play_res_x": play_x,
            "play_res_y": play_y,
            "alignment": int(style.get("alignment") or 0),
            "margin_l": margin_l,
            "margin_r": margin_r,
            "margin_v": margin_v,
        },
        "safe_area": safe_area,
        "entries": len(entry_facts),
        "max_lines": max_observed_lines,
        "max_observed_lines": max_observed_lines,
        "multi_line": bool(multi_line_entries),
        "multi_line_entries": multi_line_entries,
        "overflow": bool(overflow_entries),
        "overflow_entries": overflow_entries,
        "violations": violations,
        "entry_facts": entry_facts,
    }


def _load_visual_overlays(work_dir, *, with_source=False):
    path = Path(work_dir) / VISUAL_OVERLAYS
    if not path.exists():
        result = ([], {"present": False, "path": str(path), "fingerprint": None})
        return result if with_source else result[0]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        result = ([], {
            "present": True,
            "path": str(path),
            "fingerprint": _artifact_fingerprint(path),
            "load_error": "invalid_json",
            "load_error_detail": str(exc),
        })
        return result if with_source else result[0]
    source = {
        "present": True,
        "path": str(path),
        "fingerprint": _artifact_fingerprint(path),
        "schema_version": data.get("schema_version") if isinstance(data, dict) else None,
    }
    schema_version = data.get("schema_version") if isinstance(data, dict) else None
    valid_schema_version = (
        isinstance(schema_version, int)
        and not isinstance(schema_version, bool)
        and schema_version == 1
    )
    if isinstance(data, dict) and valid_schema_version and isinstance(data.get("overlays"), list):
        overlays = data["overlays"]
    else:
        overlays = []
        source["load_error"] = "invalid_schema"
    return (overlays, source) if with_source else overlays


def _escape_drawtext_text(text):
    return (
        str(text or "")
        .replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
        .replace("\n", "\\n")
    )


def _overlay_time_window(overlay, video_duration):
    try:
        start = float(overlay.get("start", 0.0) or 0.0)
    except (TypeError, ValueError):
        start = 0.0
    try:
        end = float(overlay.get("end", video_duration) or video_duration)
    except (TypeError, ValueError):
        end = float(video_duration or 0.0)
    end = max(start, end)
    return start, end


def _overlay_bbox(overlay, canvas, *, default_y):
    width = int((canvas or {}).get("width") or 1280)
    height = int((canvas or {}).get("height") or 720)
    text = str(overlay.get("text") or "")
    font_size = int(overlay.get("font_size") or max(18, round(height * 0.045)))
    lines = [ln for ln in text.splitlines() if ln.strip()] or [text]
    max_w = max((_visual_text_units(ln) * font_size for ln in lines), default=0.0)
    text_h = len(lines) * font_size * 1.25
    if overlay.get("type") == "top_title":
        x = max(0.0, (width - max_w) / 2)
        y = float(overlay.get("y", default_y) or default_y)
    else:
        raw_x = overlay.get("x", 0.08)
        raw_y = overlay.get("y", 0.25)
        try:
            x = float(raw_x)
            if 0.0 <= x <= 1.0:
                x *= width
        except (TypeError, ValueError):
            x = width * 0.08
        try:
            y = float(raw_y)
            if 0.0 <= y <= 1.0:
                y *= height
        except (TypeError, ValueError):
            y = height * 0.25
    return {
        "x": round(x, 2),
        "y": round(y, 2),
        "width": round(max_w, 2),
        "height": round(text_h, 2),
        "font_size": font_size,
        "line_count": len(lines),
        "overflow": x < 0 or y < 0 or x + max_w > width or y + text_h > height,
    }


def _visual_overlay_filters(work_dir, canvas, video_duration):
    """Render the first-release canonical visual_overlays.json contract.

    Only two semantic renderers are supported: top_title and inline_label_or_callout.
    Unsupported types are QC-blocking and deliberately do not silently render.
    """
    overlays, source = _load_visual_overlays(work_dir, with_source=True)
    height = int((canvas or {}).get("height") or 720)
    default_top_y = max(24, round(height * 0.05))
    filters = []
    facts = []
    unsupported = []
    overflow = []
    for idx, overlay in enumerate(overlays):
        if not isinstance(overlay, dict):
            unsupported.append({"index": idx, "type": None, "reason": "overlay_not_object"})
            continue
        typ = str(overlay.get("type") or "").strip()
        text = str(overlay.get("text") or "").strip()
        if typ not in _SUPPORTED_VISUAL_OVERLAY_TYPES:
            unsupported.append({"index": idx, "type": typ, "reason": "unsupported_overlay_type"})
            continue
        if not text:
            unsupported.append({"index": idx, "type": typ, "reason": "missing_text"})
            continue
        start, end = _overlay_time_window(overlay, video_duration)
        bbox = _overlay_bbox(overlay, canvas, default_y=default_top_y)
        if bbox["overflow"]:
            overflow.append({"index": idx, "type": typ, "bbox": bbox})
        font_size = bbox["font_size"]
        safe_text = _escape_drawtext_text(text)
        enable = f"between(t\\,{start:.3f}\\,{end:.3f})"
        if typ == "top_title":
            filt = (
                "drawtext="
                f"text='{safe_text}':x=(w-text_w)/2:y={int(bbox['y'])}:"
                f"fontsize={font_size}:fontcolor=white:borderw=2:bordercolor=black@0.85:"
                f"box=1:boxcolor=black@0.35:boxborderw=12:enable='{enable}'"
            )
        else:
            filt = (
                "drawtext="
                f"text='{safe_text}':x={int(bbox['x'])}:y={int(bbox['y'])}:"
                f"fontsize={font_size}:fontcolor=white:borderw=2:bordercolor=black@0.85:"
                f"box=1:boxcolor=black@0.45:boxborderw=8:enable='{enable}'"
            )
        filters.append(filt)
        facts.append({
            "index": idx,
            "type": typ,
            "text_chars": len(text),
            "start": round(start, 3),
            "end": round(end, 3),
            "bbox": bbox,
        })
    qc = {
        "source": source,
        "load_error": source.get("load_error"),
        "supported_types": sorted(_SUPPORTED_VISUAL_OVERLAY_TYPES),
        "present": bool(source.get("present")),
        "count": len(overlays),
        "rendered": len(facts),
        "facts": facts,
        "unsupported": unsupported,
        "overflow": overflow,
    }
    return filters, qc


def _visual_qc_has_forbidden_delivery_facts(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in _VISUAL_DELIVERY_FORBIDDEN_KEYS:
                return True
            if _visual_qc_has_forbidden_delivery_facts(child):
                return True
    elif isinstance(value, list):
        return any(_visual_qc_has_forbidden_delivery_facts(item) for item in value)
    return False


def _build_visual_qc(tts_segments, work_dir, video_duration, canvas, *, overlay_qc=None, mask_filter=None):
    entries = _combined_subtitle_entries(tts_segments, work_dir, video_duration)
    style = _style_for_measured_subtitle_band(_subtitle_style_config(canvas), canvas)
    subtitle_layout = _subtitle_layout_qc(
        entries, canvas, style, safe_area=_measured_subtitle_safe_area(style, canvas)
    )
    mask = _source_subtitle_mask_policy(work_dir)
    ratio = None
    if mask.get("active"):
        ratio = max(0.0, min(0.5, float(CONFIG.get("source_subtitle_mask_ratio", 0.14) or 0.0)))
    mask.update({
        "ratio": ratio,
        "filter": "drawbox" if mask_filter else None,
        "opacity": float(CONFIG.get("subtitle_mask_opacity", 0.6)),
        "timing": str(CONFIG.get("source_subtitle_mask_timing", "narration")),
        "subtitle_y_top": int(CONFIG.get("subtitle_y_top", -1)),
        "subtitle_y_bot": int(CONFIG.get("subtitle_y_bot", -1)),
    })
    overlay_qc = overlay_qc or _visual_overlay_filters(work_dir, canvas, video_duration)[1]
    blocking_codes = []
    if mask.get("blocking"):
        blocking_codes.append("mask_policy_not_explicit")
    if subtitle_layout.get("overflow"):
        blocking_codes.append("subtitle_overflow")
    if overlay_qc.get("load_error"):
        blocking_codes.append("invalid_visual_overlays_json")
    if overlay_qc.get("unsupported"):
        blocking_codes.append("unsupported_visual_overlay")
    if overlay_qc.get("overflow"):
        blocking_codes.append("visual_overlay_overflow")
    qc = {
        "schema_version": 1,
        "artifact": VISUAL_QC,
        "verdict": "FAIL" if blocking_codes else "PASS",
        "blocking": bool(blocking_codes),
        "blocking_codes": blocking_codes,
        "geometry": {
            "canvas": {
                "width": int(canvas.get("width", 1280)),
                "height": int(canvas.get("height", 720)),
                "fps": float(canvas.get("fps", 30.0)),
            },
            "storage": {
                "width": int(canvas.get("storage_width", canvas.get("width", 1280))),
                "height": int(canvas.get("storage_height", canvas.get("height", 720))),
            },
            "rotation": int(canvas.get("rotation", 0) or 0),
            "sample_aspect_ratio": canvas.get("sample_aspect_ratio", "1:1"),
            "display_aspect_ratio": canvas.get("display_aspect_ratio"),
        },
        "subtitles": subtitle_layout,
        "mask": mask,
        "overlays": overlay_qc,
        "summary": {
            "subtitle_entries": subtitle_layout.get("entries", 0),
            "subtitle_overflow": bool(subtitle_layout.get("overflow")),
            "subtitle_multi_line": bool(subtitle_layout.get("multi_line")),
            "mask_policy": mask.get("policy"),
            "mask_active": bool(mask.get("active")),
            "overlay_rendered": int(overlay_qc.get("rendered", 0) or 0),
            "overlay_unsupported": len(overlay_qc.get("unsupported") or []),
        },
    }
    if _visual_qc_has_forbidden_delivery_facts(qc):
        qc["verdict"] = "FAIL"
        qc["blocking"] = True
        qc["blocking_codes"].append("visual_qc_contains_delivery_fact")
    return qc


def _write_visual_qc(work_dir, qc):
    path = Path(work_dir) / VISUAL_QC
    path.write_text(json.dumps(qc, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _escape_subtitle_filter_path(path):
    """Escape a path for ffmpeg subtitle/ass video filter arguments."""
    text = str(path).replace("\\", "/")
    for raw, escaped in (
        ("\\", "\\\\"),
        (":", "\\:"),
        ("'", "\\'"),
        (",", "\\,"),
        ("[", "\\["),
        ("]", "\\]"),
    ):
        text = text.replace(raw, escaped)
    return text


def _subtitle_burn_filter(subtitle_path):
    """Build the ffmpeg video filter used for hard-sub rendering."""
    return f"subtitles=filename='{_escape_subtitle_filter_path(subtitle_path)}'"


def _output_downscale_filter(max_h):
    """Lanczos downscale that forces BOTH output dimensions even (libx264/yuv420p need it).

    -2 keeps the aspect ratio with an even width; 2*trunc(min(ih,H)/2) caps the height at H
    yet forces it even, so an odd OUTPUT_MAX_HEIGHT (e.g. 721) cannot produce an odd height
    that makes libx264 abort with an empty output file. 'min(ih,H)' only ever shrinks.
    """
    return f"scale=-2:'2*trunc(min(ih,{max_h})/2)':flags=lanczos"


def _source_subtitle_mask_filter(
    canvas=None, work_dir=None, tts_segments=None, video_duration=None
):
    """Return source-subtitle drawbox filters, optionally scoped to narration windows.

    Many source videos (e.g. 庆余年) ship hardcoded subtitles; without this the recap
    shows the original subs AND our narration subs stacked. Once masking is explicitly enabled,
    the enhanced default is a measured, translucent narration-only band; opacity and timing
    remain configurable.
    """
    policy = _source_subtitle_mask_policy(work_dir)
    if not policy.get("active"):
        return None
    opacity = max(0.0, min(1.0, float(CONFIG.get("subtitle_mask_opacity", 0.6))))

    _validate_measured_subtitle_coordinate_domain(canvas)

    canvas_h = int((canvas or {}).get("height", 0) or 0)
    y_top = int(CONFIG.get("subtitle_y_top", -1))
    y_bot = int(CONFIG.get("subtitle_y_bot", -1))
    custom_band = canvas_h > 0 and 0 <= y_top < y_bot <= canvas_h
    if custom_band:
        padding = int(CONFIG.get("subtitle_mask_padding", 4) or 0)
        mask_top = max(0, y_top - padding)
        mask_bot = min(canvas_h, y_bot + padding)
        geometry = f"x=0:y={mask_top}:w=iw:h={mask_bot - mask_top}"
    else:
        ratio = max(0.0, min(0.5, float(CONFIG.get("source_subtitle_mask_ratio", 0.14) or 0.0)))
        # Our subtitle cues are one line. Keep the mask large enough for that line and its
        # margin, but never regress to the old two-line bar that hid ~23% of the image.
        style = _subtitle_style_config(canvas)
        play_res_y = max(1.0, float(style["play_res_y"]))
        line_h = float(style["font_size"]) * 1.25
        pad = 10.0 * play_res_y / SUBTITLE_STYLE_REF_H
        sub_band = (float(style["margin_v"]) + line_h + pad) / play_res_y
        ratio = min(0.5, max(ratio, sub_band))
        if ratio <= 0:
            return None
        geometry = f"x=0:y=ih-ih*{ratio:.3f}:w=iw:h=ih*{ratio:.3f}"

    base = f"drawbox={geometry}:color=black@{opacity:.2f}:t=fill"
    timing = str(CONFIG.get("source_subtitle_mask_timing", "narration") or "narration").lower()
    if timing not in {"all", "narration"}:
        timing = "narration"
    filters = []
    if timing == "all" and opacity > 0:
        filters.append(base)
    elif timing == "narration" and opacity > 0:
        windows = []
        for seg in tts_segments or []:
            if not isinstance(seg, dict):
                continue
            start, end = _seg_place_window(seg)
            try:
                start, end = float(start), float(end)
            except (TypeError, ValueError):
                continue
            if end <= start:
                continue
            windows.append((start, end, 0.0))
        # Avoid overlapping drawboxes: stacking two 60%-black masks would darken the overlap
        # to 84%. Coalescing also keeps long filter chains smaller.
        filters.extend(
            f"{base}:enable='between(t,{start:.3f},{end:.3f})'"
            for start, end, _ in coalesce_duck_windows(windows, bridge=0.001)
        )

    # A translucent mask deliberately leaves the source glyphs visible. Whenever we burn a
    # replacement original-dialogue subtitle into a gap, cover that exact window opaquely first;
    # otherwise the source hard-sub and replacement text are stacked on top of each other.
    if video_duration is not None and not (timing == "all" and opacity >= 1.0 - 1e-9):
        replacement_entries = _original_gap_subtitle_entries(
            tts_segments or [], work_dir, video_duration
        )
        replacement_windows = [
            (entry["start"], entry["end"], 0.0) for entry in replacement_entries
        ]
        opaque = f"drawbox={geometry}:color=black@1.00:t=fill"
        filters.extend(
            f"{opaque}:enable='between(t,{start:.3f},{end:.3f})'"
            for start, end, _ in coalesce_duck_windows(replacement_windows, bridge=0.001)
        )
    return ",".join(filters) if filters else None
