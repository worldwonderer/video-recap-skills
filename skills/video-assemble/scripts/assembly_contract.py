"""Assembly manifest/QC persistence and delivery contract helpers."""

import json
import math
import wave
from pathlib import Path

from lib import CONFIG
from assemble_constants import (
    ASSEMBLY_MANIFEST,
    ASSEMBLY_QC,
    SEGMENT_AUDIO_SCHEMA_VERSION,
    VISUAL_QC,
)
from audio_mix import _loudness_mode
from artifacts import (
    _load_work_json,
    _source_video_identity,
    _timeline_provenance_status,
)

def _assembly_manifest_payload(input_video, tts_segments, work_dir, output_path,
                               tts_meta_path=None, final_output=None, *, settings_fingerprint):
    """Slim render record. The orchestrator reads `final_output` to report the result;
    `source_video` stays None unless cut mode explicitly passed --source-video, proving a
    stale ambient SOURCE_VIDEO never leaked into a full-mode timeline / 剪映 export."""
    input_video = Path(input_video)
    output_path = Path(output_path)
    source_video, source_video_fingerprint = _source_video_identity()
    qc_path = Path(work_dir) / ASSEMBLY_QC
    qc = _load_work_json(work_dir, ASSEMBLY_QC) if qc_path.exists() else None
    payload = {
        "schema_version": 2,
        "input_video": str(input_video.resolve()),
        "source_video": source_video,
        "source_video_fingerprint": source_video_fingerprint,
        "tts_meta": str(Path(tts_meta_path).resolve()) if tts_meta_path else None,
        "tts_segments": len(tts_segments or []),
        "assembly_settings": settings_fingerprint(work_dir),
        "output_path": str(output_path.resolve()),
        "segment_audio_schema_version": SEGMENT_AUDIO_SCHEMA_VERSION,
        "qc_path": str(qc_path.resolve()) if qc_path.exists() else None,
        "qc_verdict": qc.get("verdict") if isinstance(qc, dict) else None,
        "qc_blocking_codes": qc.get("blocking_codes", []) if isinstance(qc, dict) else [],
        # The settings fingerprint records the configured/fallback loudness policy; these QC
        # fields record what the just-finished render actually used after the loudnorm probe.
        "qc_loudness_mode": qc.get("loudness_mode") if isinstance(qc, dict) else None,
        "qc_loudnorm_measurement": qc.get("loudnorm_measurement") if isinstance(qc, dict) else None,
        "audio_segments": [
            {
                "index": seg.get("index"),
                "segment_audio_schema_version": seg.get("segment_audio_schema_version", SEGMENT_AUDIO_SCHEMA_VERSION),
                "narration": seg.get("narration", ""),
                "spoken_text": seg.get("spoken_text") or seg.get("narration", ""),
                "truncated": bool(seg.get("truncated", False)),
                "truncate_reason": seg.get("truncate_reason", "none"),
                "fit_status": seg.get("fit_status"),
                "blocking": bool(seg.get("blocking", False)),
                "audio_duration": seg.get("audio_duration"),
                "placed_audio_duration": seg.get("placed_audio_duration"),
                "placed_audio_path": seg.get("placed_audio_path"),
                "actual_place_start": seg.get("actual_place_start"),
                "actual_place_end": seg.get("actual_place_end"),
                "source_duck_end": seg.get("source_duck_end"),
                "source_restore_at": seg.get("source_restore_at"),
                "source_handoff_status": seg.get("source_handoff_status"),
                "source_entry_status": seg.get("source_entry_status"),
                "global_narration_speed": seg.get("global_narration_speed"),
                "segment_tempo_factor": seg.get("segment_tempo_factor"),
                "effective_tempo": seg.get("effective_tempo"),
                "rms_dbfs_before": seg.get("rms_dbfs_before"),
                "rms_dbfs_after": seg.get("rms_dbfs_after"),
                "peak_after": seg.get("peak_after"),
            }
            for seg in (tts_segments or [])
            if isinstance(seg, dict)
        ],
    }
    if final_output is not None:
        payload["final_output"] = str(Path(final_output).resolve())
    provenance = _timeline_provenance_status(work_dir)
    if provenance:
        payload["timeline_provenance"] = provenance
    return payload


def _write_assembly_manifest(work_dir, manifest):
    path = Path(work_dir) / ASSEMBLY_MANIFEST
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _visual_qc_rollup(visual_qc):
    if not isinstance(visual_qc, dict):
        return {
            "present": False,
            "verdict": "MISSING",
            "blocking": True,
            "blocking_codes": ["missing_visual_qc"],
            "summary": {},
        }
    subtitles = visual_qc.get("subtitles") or visual_qc.get("subtitle") or {}
    overlays = visual_qc.get("overlays") or visual_qc.get("overlay") or {}
    return {
        "present": True,
        "artifact": visual_qc.get("artifact", VISUAL_QC),
        "verdict": visual_qc.get("verdict"),
        "blocking": bool(visual_qc.get("blocking", False)),
        "blocking_codes": list(visual_qc.get("blocking_codes") or []),
        "summary": visual_qc.get("summary", {}),
        "geometry": visual_qc.get("geometry", {}),
        "subtitles": {
            "entries": subtitles.get("entries", 0),
            "multi_line": subtitles.get("multi_line", False),
            "overflow": subtitles.get("overflow", False),
            "safe_area": subtitles.get("safe_area", {}),
        },
        "subtitle": subtitles,
        "mask": visual_qc.get("mask", {}),
        "overlays": {
            "present": overlays.get("present", bool(overlays.get("items"))),
            "rendered": overlays.get("rendered", len(overlays.get("items", [])) if isinstance(overlays.get("items"), list) else 0),
            "unsupported": overlays.get("unsupported", []),
            "overflow": overlays.get("overflow", []),
        },
        "overlay": overlays,
    }


def _build_assembly_qc(tts_segments, video_duration, *, output_path=None,
                       source_has_audio=None, loudness_mode=None, loudnorm_measurement=None,
                       visual_qc=None, render_delivery=None, delivery_qc=None):
    """Machine-readable assembly release gate.

    Visual facts are rolled up from visual_qc.json. Delivery/render facts live here
    (or render/delivery QC in future), never in visual_qc.json.
    """
    hard_max = float(CONFIG.get("narration_cumulative_tempo_hard_max", 1.40) or 1.40)
    segments = [s for s in (tts_segments or []) if isinstance(s, dict)]
    no_safe = [
        int(s.get("index", i))
        for i, s in enumerate(segments)
        if s.get("fit_status") == "no_safe_fit"
        or s.get("truncate_reason") in {"no_safe_boundary", "no_room"}
        or bool(s.get("blocking", False))
    ]
    skipped = [
        int(s.get("index", i))
        for i, s in enumerate(segments)
        if s.get("fit_status") == "skipped"
    ]
    fit_failed = [
        int(s.get("index", i))
        for i, s in enumerate(segments)
        if s.get("fit_status") == "speed_adjust_failed"
    ]
    tempo_exceeded = []
    truncated = []
    handoff_failed = []
    timeline_audio_failed = []
    max_effective = 0.0
    for i, s in enumerate(segments):
        try:
            eff = float(s.get("effective_tempo", 0.0) or 0.0)
        except (TypeError, ValueError):
            eff = 0.0
        max_effective = max(max_effective, eff)
        if eff > hard_max + 1e-6:
            tempo_exceeded.append(int(s.get("index", i)))
        if bool(s.get("truncated", False)) or s.get("truncate_reason") == "tail_trim_tolerance":
            truncated.append(int(s.get("index", i)))
        if bool(s.get("source_handoff_blocking", False)):
            handoff_failed.append(int(s.get("index", i)))
        if s.get("actual_place_start") is not None and float(s.get("placed_audio_duration", 0.0) or 0.0) > 0:
            placed_path = s.get("placed_audio_path")
            valid_placed_file = False
            if placed_path and Path(placed_path).exists():
                try:
                    with wave.open(str(placed_path), "rb") as placed_wav:
                        placed_duration = placed_wav.getnframes() / placed_wav.getframerate()
                        tolerance = 1.0 / placed_wav.getframerate()
                    timeline_start = round(float(s.get("actual_place_start")), 4)
                    timeline_end = math.ceil(
                        float(s.get("actual_place_end")) * 10_000 - 1e-9
                    ) / 10_000
                    serialized_span = timeline_end - timeline_start
                    valid_placed_file = abs(
                        placed_duration - float(s.get("placed_audio_duration") or 0.0)
                    ) <= tolerance + 1e-9 and serialized_span + 1e-9 >= placed_duration
                except (OSError, wave.Error, ZeroDivisionError, TypeError, ValueError):
                    valid_placed_file = False
            if not valid_placed_file:
                timeline_audio_failed.append(int(s.get("index", i)))

    placed = [
        float(s.get("placed_audio_duration", 0.0) or 0.0)
        for s in segments
        if s.get("placed_audio_duration") is not None
    ]
    blocking_codes = []
    if not segments:
        blocking_codes.append("missing_narration")
    if skipped:
        blocking_codes.append("skipped_segments")
    if fit_failed:
        blocking_codes.append("fit_failed")
    if no_safe:
        blocking_codes.append("no_safe_fit")
    if tempo_exceeded:
        blocking_codes.append("effective_tempo_exceeded")
    if truncated:
        blocking_codes.append("truncated_speech")
    if handoff_failed:
        blocking_codes.append("unsafe_source_handoff")
    if timeline_audio_failed:
        blocking_codes.append("timeline_audio_mismatch")
    if placed and max(placed) <= 0.0 and not no_safe:
        blocking_codes.append("empty_narration")
    visual_rollup = (
        _visual_qc_rollup(visual_qc)
        if visual_qc is not None
        else {"present": False, "verdict": "NOT_RUN", "blocking": False, "blocking_codes": [], "summary": {}}
    )
    if visual_rollup.get("blocking"):
        blocking_codes.append("visual_qc_failed")
    if source_has_audio is False:
        # Not blocking: assemble can synthesize a silent original track.
        source_audio = "synthetic_silence"
    elif source_has_audio is True:
        source_audio = "present"
    else:
        source_audio = "unknown"

    output = {}
    if output_path is not None:
        output_path = Path(output_path)
        output = {
            "path": str(output_path),
            "exists": output_path.exists(),
            "bytes": output_path.stat().st_size if output_path.exists() else 0,
        }
        if output_path.exists() and output["bytes"] <= 0:
            blocking_codes.append("empty_output")

    render_delivery = render_delivery or delivery_qc or {}
    qc_payload = {
        "schema_version": 1,
        "artifact": ASSEMBLY_QC,
        "verdict": "FAIL" if blocking_codes else "PASS",
        "blocking": bool(blocking_codes),
        "blocking_codes": blocking_codes,
        "duration": round(float(video_duration or 0.0), 4),
        "source_audio": source_audio,
        "loudness_mode": loudness_mode or _loudness_mode(loudnorm_measurement),
        "loudnorm_measurement": loudnorm_measurement,
        "release_gate": {
            "verdict": "FAIL" if blocking_codes else "PASS",
            "visual_qc": visual_rollup.get("verdict"),
            "delivery_qc": "PASS",
            "audio_qc": "FAIL" if any(
                c in blocking_codes
                for c in (
                    "missing_narration", "skipped_segments", "fit_failed", "no_safe_fit",
                    "effective_tempo_exceeded", "empty_narration", "truncated_speech",
                    "unsafe_source_handoff",
                    "timeline_audio_mismatch",
                )
            ) else "PASS",
        },
        "visual_qc": visual_rollup,
        "delivery_qc": {
            "video_encode_passes": render_delivery.get("video_encode_passes"),
            "reencode_reason": render_delivery.get("reencode_reason"),
            "audio_sample_rate": render_delivery.get("audio_sample_rate"),
            "final_compat_notes": render_delivery.get("final_compat_notes", []),
        },
        "summary": {
            "segments": len(segments),
            "placed_segments": sum(1 for x in placed if x > 0.0),
            "skipped_segments": skipped,
            "fit_failed_segments": fit_failed,
            "no_safe_fit_segments": no_safe,
            "tempo_exceeded_segments": tempo_exceeded,
            "max_effective_tempo": round(max_effective, 4),
            "truncated_segments": truncated,
            "unsafe_source_handoff_segments": handoff_failed,
            "timeline_audio_mismatch_segments": timeline_audio_failed,
        },
        "output": output,
    }
    qc_payload["visual_rollup"] = visual_rollup
    qc_payload["delivery_rollup"] = qc_payload["delivery_qc"]
    return qc_payload


def _write_assembly_qc(work_dir, qc):
    path = Path(work_dir) / ASSEMBLY_QC
    path.write_text(json.dumps(qc, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _resolve_final_output(base, stem):
    """The recap output is the stable human alias recap_<stem>.mp4, overwritten in place
    on every run so the iterate-on-narration loop always refreshes the same file."""
    return Path(base) / f"recap_{stem}.mp4"
