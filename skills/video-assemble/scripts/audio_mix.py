"""Loudness, source handoffs, ducking envelopes, and audio mix graphs."""

import json
import math
import re
from pathlib import Path

from artifacts import _load_work_json, _value_fingerprint
from audio_automation import (
    coalesce_duck_windows,
    default_bridge,
    ducking_expression,
    release_ducking_expression,
)
from lib import CONFIG, log, run_cmd

def _limiter_filter():
    peak = float(CONFIG.get("final_limiter_peak", 0.98) or 0.98)
    return f"alimiter=limit={peak:.2f}:level=false"


def _loudness_mode(measured=None):
    if not CONFIG.get("final_loudnorm", True):
        return "limiter_only"
    return "two_pass_linear" if measured else "equivalent"


def final_loudnorm_filter(measured=None):
    """Final-mix loudness normalization/limiter filter from CONFIG.

    Ducking branches set only relative balance; this single stage owns the
    absolute output loudness so the recap is not left too quiet. When `measured`
    is supplied from a first loudnorm pass, ffmpeg runs the deterministic second
    pass; without it we still force the same target and peak limiter as a
    documented equivalent/fallback path.
    """
    if not CONFIG.get("final_loudnorm", True):
        return _limiter_filter()
    filt = (
        f"loudnorm=I={CONFIG.get('target_lufs', -14.0)}"
        f":TP={CONFIG.get('target_true_peak', -1.0)}"
        f":LRA={CONFIG.get('target_lra', 11.0)}"
        f":linear=true"
    )
    if measured:
        for src, dst in (
            ("input_i", "measured_I"),
            ("input_tp", "measured_TP"),
            ("input_lra", "measured_LRA"),
            ("input_thresh", "measured_thresh"),
            ("target_offset", "offset"),
        ):
            if src in measured:
                filt += f":{dst}={measured[src]}"
    filt += ":print_format=summary"
    return f"{filt},{_limiter_filter()}"


def _parse_loudnorm_json(text):
    """Extract ffmpeg loudnorm JSON from stderr/stdout."""
    for match in reversed(list(re.finditer(r"\{[\s\S]*?\}", str(text or "")))):
        try:
            data = json.loads(match.group(0))
        except ValueError:
            continue
        if isinstance(data, dict) and {"input_i", "input_tp", "input_lra", "input_thresh", "target_offset"} <= set(data):
            return data
    return None


def _loudnorm_first_pass_filter():
    return (
        f"loudnorm=I={CONFIG.get('target_lufs', -14.0)}"
        f":TP={CONFIG.get('target_true_peak', -1.0)}"
        f":LRA={CONFIG.get('target_lra', 11.0)}"
        f":print_format=json"
    )


def _run_loudnorm_first_pass(input_video, narration_wav, original_audio_input,
                             bgm_input, filter_complex, work_dir):
    """Measure the exact mixed audio graph before final render.

    Returns ffmpeg loudnorm JSON, or None when probing fails. The caller then
    falls back to the documented equivalent single-pass target+limiter filter.
    """
    if not CONFIG.get("final_loudnorm", True):
        return None
    probe_fc = f"{filter_complex};[aout]{_loudnorm_first_pass_filter()}[lnprobe]"
    probe_script = Path(work_dir) / ".filter_complex_loudnorm_probe.txt"
    probe_script.write_text(probe_fc, encoding="utf-8")
    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_video),
        "-i", str(narration_wav),
        *original_audio_input,
        *bgm_input,
        "-filter_complex_script", str(probe_script),
        "-map", "[lnprobe]",
        "-f", "null", "-",
    ]
    try:
        result = run_cmd(cmd)
    finally:
        probe_script.unlink(missing_ok=True)
    if result.returncode != 0:
        log(f"  ⚠️ loudnorm 首遍测量失败，降级到目标滤镜+limiter: {result.stderr}")
        return None
    measured = _parse_loudnorm_json((result.stdout or "") + "\n" + (result.stderr or ""))
    if not measured:
        log("  ⚠️ loudnorm 首遍未返回 JSON，降级到目标滤镜+limiter")
        return None
    return measured


def _seg_place_window(seg):
    """Return a segment's actual placed (start, end) on the output timeline."""
    s = seg.get("actual_place_start", seg.get("start", 0))
    e = seg.get("actual_place_end", seg.get("end", 0))
    return s, e


def _load_sentence_handoff_anchors(work_dir):
    """Load high/medium sentence anchors and their measured pause windows."""
    work_dir = Path(work_dir)
    cut_mode = (work_dir / "edited_source.mp4").exists() or (
        work_dir / "clip_plan_validated.json"
    ).exists()
    candidates = [
        work_dir
        / (
            "speech_boundary_anchors_output.json"
            if cut_mode
            else "speech_boundary_anchors.json"
        )
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if cut_mode:
            plan = _load_work_json(work_dir, "clip_plan_validated.json")
            if not (
                isinstance(payload, dict)
                and payload.get("schema_version") == 2
                and payload.get("timeline") == "cut_output"
                and isinstance(plan, dict)
                and payload.get("clip_plan_fingerprint") == _value_fingerprint(plan)
            ):
                return [], None, {"require_measured": True}
            payload = {**payload, "require_measured": True}
        raw = payload.get("sentence_anchors", []) if isinstance(payload, dict) else []
        anchors = []
        for item in raw:
            if not isinstance(item, dict) or item.get("confidence") not in {"high", "medium"}:
                continue
            try:
                when = float(item.get("time"))
            except (TypeError, ValueError):
                continue
            if math.isfinite(when) and when >= 0:
                try:
                    pause_start = float(item.get("pause_start", when - 0.12))
                except (TypeError, ValueError):
                    pause_start = when - 0.12
                anchors.append({
                    "time": round(when, 4),
                    "pause_start": round(max(0.0, min(pause_start, when)), 4),
                })
        unique = {(row["time"], row["pause_start"]): row for row in anchors}
        return sorted(unique.values(), key=lambda row: row["time"]), path.name, payload
    return [], None, {"require_measured": cut_mode}


def _handoff_timed_rows(payload, key):
    rows = payload.get(key, []) if isinstance(payload, dict) else []
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        try:
            start, end = float(row.get("start")), float(row.get("end"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(start) and math.isfinite(end) and end > start:
            out.append({"start": start, "end": end})
    return out


def _handoff_speech_evidence(work_dir, artifact, payload):
    speech = _handoff_timed_rows(payload, "speech_spans")
    quiet = _handoff_timed_rows(payload, "quiet_windows")
    if payload.get("require_measured"):
        return speech, quiet
    if artifact == "speech_boundary_anchors_output.json":
        return speech, quiet
    if not speech:
        for name in ("asr_clean.json", "asr_result.json"):
            raw = _load_work_json(work_dir, name)
            if isinstance(raw, list):
                raw = {"speech_spans": raw}
            elif isinstance(raw, dict):
                raw = {"speech_spans": raw.get("segments", [])}
            speech = _handoff_timed_rows(raw, "speech_spans")
            if speech:
                break
    if not quiet:
        raw = _load_work_json(work_dir, "silence_periods.json")
        raw = {"quiet_windows": [
            row for row in raw
            if isinstance(row, dict) and not bool(row.get("has_speech", False))
        ]} if isinstance(raw, list) else {}
        quiet = _handoff_timed_rows(raw, "quiet_windows")
    return speech, quiet


def _merged_handoff_intervals(start, end, rows):
    intervals = sorted(
        (max(start, row["start"]), min(end, row["end"]))
        for row in rows
        if row["end"] > start and row["start"] < end
    )
    merged = []
    for left, right in intervals:
        if right <= left:
            continue
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))
    return merged


def _speech_overlap_excluding_quiet(start, end, speech, quiet):
    speech_intervals = _merged_handoff_intervals(start, end, speech)
    quiet_intervals = _merged_handoff_intervals(start, end, quiet)
    overlap = sum(right - left for left, right in speech_intervals)
    for speech_left, speech_right in speech_intervals:
        overlap -= sum(
            max(0.0, min(speech_right, quiet_right) - max(speech_left, quiet_left))
            for quiet_left, quiet_right in quiet_intervals
        )
    return max(0.0, overlap)


def _measured_speech_owned(
    start, end, speech, quiet, anchors, authored, require_measured=False
):
    duration = max(0.0, end - start)
    quiet_min = max(
        0.3,
        duration * float(CONFIG.get("quiet_overlap_min_ratio", 0.8) or 0.8),
    )
    if speech:
        return _speech_overlap_excluding_quiet(start, end, speech, quiet) > 0.05
    quiet_overlap = sum(
        right - left for left, right in _merged_handoff_intervals(start, end, quiet)
    )
    if quiet and quiet_overlap >= quiet_min:
        return False
    return True if anchors or require_measured else bool(authored)


def _entry_speech_owned(
    start, speech, quiet, anchors, authored, require_measured=False, tolerance=0.05
):
    if any(row["start"] - tolerance <= start <= row["end"] + tolerance for row in quiet):
        return False
    if any(row["start"] - tolerance <= start < row["end"] - tolerance for row in speech):
        return True
    if speech:
        return False
    return True if anchors or require_measured else bool(authored)


def _work_has_source_speech(work_dir, speech_spans=None, require_measured=False):
    if speech_spans or require_measured:
        return True
    for name in ("asr_clean.json", "asr_result.json"):
        payload = _load_work_json(work_dir, name)
        if isinstance(payload, dict):
            payload = payload.get("segments", [])
        if isinstance(payload, list) and any(
            isinstance(item, dict) and str(item.get("text") or "").strip()
            for item in payload
        ):
            return True
    return False


def _apply_source_sentence_handoffs(tts_segments, work_dir, video_duration):
    """Keep source audio ducked until a safe sentence boundary after narration.

    This does not move or trim narration. It only extends the ORIGINAL-audio duck
    envelope so returning the source track cannot reveal the middle of a sentence.
    """
    fade = max(0.0, float(CONFIG.get("duck_fade_seconds", 0.3) or 0.0))
    bridge = max(0.0, float(CONFIG.get("duck_bridge_seconds", 1.5) or 0.0))
    anchors, artifact, evidence_payload = _load_sentence_handoff_anchors(work_dir)
    speech_spans, quiet_windows = _handoff_speech_evidence(
        work_dir, artifact, evidence_payload
    )
    require_measured = bool(evidence_payload.get("require_measured"))
    placed = []
    for seg in tts_segments or []:
        if not isinstance(seg, dict):
            continue
        try:
            start, end = map(float, _seg_place_window(seg))
        except (TypeError, ValueError):
            continue
        if end > start:
            placed.append((start, end, seg))
    placed.sort(key=lambda item: (item[0], item[1]))
    if not placed:
        return []

    runs = []
    for start, end, seg in placed:
        if runs and start - runs[-1]["end"] <= bridge + 1e-6:
            runs[-1]["end"] = max(runs[-1]["end"], end)
            runs[-1]["segments"].append(seg)
        else:
            runs.append({"start": start, "end": end, "segments": [seg]})

    source_has_speech = _work_has_source_speech(
        work_dir, speech_spans, require_measured=require_measured
    )
    report = []
    for run in runs:
        ownership = []
        for seg in run["segments"]:
            start, end = map(float, _seg_place_window(seg))
            measured = _measured_speech_owned(
                start,
                end,
                speech_spans,
                quiet_windows,
                anchors,
                seg.get("overlaps_speech", True),
                require_measured=require_measured,
            )
            seg["overlaps_speech"] = measured
            ownership.append(measured)
        first = run["segments"][0]
        entry_owned = _entry_speech_owned(
            run["start"],
            speech_spans,
            quiet_windows,
            anchors,
            first.get("overlaps_speech", True),
            require_measured=require_measured,
        )
        speech_owned = entry_owned or any(ownership)
        if not speech_owned:
            report.append({"start": run["start"], "end": run["end"], "status": "quiet_source"})
            continue
        last = run["segments"][-1]
        start_safe = run["start"] <= 0.25 or any(
            anchor["pause_start"] - 0.05 <= run["start"] <= anchor["time"] + 0.08
            for anchor in anchors
        )
        if entry_owned and anchors and not start_safe:
            first["source_handoff_blocking"] = True
            first["source_entry_status"] = "unsafe_entry"
        elif not entry_owned:
            first["source_entry_status"] = "quiet_source"
        else:
            first["source_entry_status"] = "sentence_boundary" if anchors else "unverified"

        restore_anchor = next(
            (anchor for anchor in anchors if anchor["time"] >= run["end"] - 0.01),
            None,
        )
        if restore_anchor is not None:
            # Hold the source low through its last spoken sample, then fit the release
            # entirely inside the measured pause. Never begin the ramp `fade` seconds
            # before the anchor when that would expose the final source phoneme.
            duck_end = max(run["end"], restore_anchor["pause_start"])
            restore_at = max(duck_end, restore_anchor["time"])
            status = "sentence_boundary"
        elif anchors:
            # No later complete source sentence: never expose a fragment at the tail.
            restore_at = float(video_duration)
            duck_end = float(video_duration)
            status = "held_to_timeline_end"
        elif source_has_speech:
            first["source_handoff_blocking"] = True
            first["source_entry_status"] = "anchors_unavailable"
            restore_at = run["end"] + fade
            duck_end = run["end"]
            status = "anchors_unavailable"
        else:
            restore_at = run["end"] + fade
            duck_end = run["end"]
            status = "no_source_speech"

        last["source_duck_end"] = round(min(float(video_duration), duck_end), 4)
        last["source_restore_at"] = round(min(float(video_duration), restore_at), 4)
        last["source_handoff_status"] = status
        report.append({
            "start": round(run["start"], 4),
            "end": round(run["end"], 4),
            "restore_at": last["source_restore_at"],
            "status": status,
            "anchor_artifact": artifact,
        })
    return report


def _amix_tail(narr_vol, bgm_chain=""):
    """Mix the prepared original track [orig] (+ optional BGM bed) with the boosted
    narration [narr] into [aout]. bgm_chain, when given, defines [bgm] from input [2:a]."""
    narr = f"[1:a]volume={narr_vol},aresample=48000[narr];"
    if bgm_chain:
        return bgm_chain + narr + "[orig][bgm][narr]amix=inputs=3:duration=first:dropout_transition=0:normalize=0[aout]"
    return narr + "[orig][narr]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"


def _placement_windows(tts_segments, level_for, *, source_safe_end=False):
    """Collect [(start, end, level)] placement windows for the placed beats, using
    `level_for(seg)` to pick each beat's duck level. Skips non-dicts and empty spans."""
    windows = []
    for seg in tts_segments:
        if not isinstance(seg, dict):
            continue
        s, e = _seg_place_window(seg)
        if source_safe_end:
            try:
                e = max(float(e), float(seg.get("source_duck_end", e)))
            except (TypeError, ValueError):
                pass
        if e - s <= 0:
            continue
        windows.append((s, e, level_for(seg)))
    return windows


def _duck_envelope(tts_segments, idle, speech_vol, quiet_vol, fade, bridge=None):
    """Per-beat ducking automation for the ORIGINAL track.

    Uses the shared ducking contract: [start-fade,start] pre-roll ramp down,
    [start,end] held at the selected duck level, and [end,end+fade] release.
    Bridged spans use the most-ducked (lowest) level, matching timeline.json /
    JianYing keyframes. Returns a volume= expression, or None when no beat carries
    placement info (caller falls back to a constant).
    """
    if bridge is None:
        bridge = default_bridge(fade)
    windows = []
    for seg in tts_segments or []:
        if not isinstance(seg, dict):
            continue
        try:
            start, narration_end = map(float, _seg_place_window(seg))
            hold_end = max(narration_end, float(seg.get("source_duck_end", narration_end)))
            restore_at = max(hold_end, float(seg.get("source_restore_at", hold_end + fade)))
        except (TypeError, ValueError):
            continue
        if narration_end <= start:
            continue
        level = speech_vol if seg.get("overlaps_speech", True) else quiet_vol
        windows.append((start, hold_end, level, restore_at))
    return release_ducking_expression(windows, idle, fade, bridge=bridge)


def _bgm_envelope(tts_segments, base, duck, fade, bridge=None):
    """Per-beat ducking automation for the BGM track using the shared contract."""
    if bridge is None:
        bridge = default_bridge(fade)
    windows = _placement_windows(tts_segments, lambda _: duck)
    merged = coalesce_duck_windows(windows, bridge)
    return ducking_expression(merged, base, fade)


def _build_audio_filter_complex(
    tts_segments,
    has_bgm=False,
    *,
    original_audio_label="0:a",
    bgm_audio_label=None,
):
    """Compose the audio tracks into [aout], like a cut-software timeline.

    Tracks:
      - original (input [0:a], the video's own audio): ducked under each narration
        window by a per-beat volume envelope, but held up at `idle_orig_volume` in
        the gaps so the recap never drops to dead air between sentences.
      - bgm (input [2:a], optional): a looped music bed, gently ducked under narration.
      - narration (input [1:a]): the TTS, boosted and laid on top.
    CONFIG["ducking_mode"] (default "fixed") selects the original-track strategy:
    fixed = the gap-fill envelope above; sidechaincompress = auto-duck keyed off the
    narration; none = no ducking. Placement comes from actual_place_start/end.
    """
    ducking_mode = CONFIG.get("ducking_mode", "fixed")
    if ducking_mode == "sidechaincompress" and any(
        isinstance(seg, dict)
        and float(seg.get("source_duck_end", seg.get("actual_place_end", 0)) or 0)
        > float(seg.get("actual_place_end", 0) or 0) + 1e-6
        for seg in tts_segments or []
    ):
        log("sidechaincompress 无法保持句末交接窗口，已回退 fixed ducking")
        ducking_mode = "fixed"
    narr_vol = CONFIG.get("ducking_narr_weight", 1.5)
    fade = CONFIG.get("duck_fade_seconds", 0.3)
    original_in = f"[{original_audio_label}]"
    bgm_in = f"[{bgm_audio_label or '2:a'}]"

    # BGM bed (input [2:a]): ducked under each narration window when present.
    bgm_chain = ""
    if has_bgm:
        base = CONFIG.get("bgm_volume", 0.18)
        bgm_expr = _bgm_envelope(tts_segments, base, CONFIG.get("bgm_ducking_volume", 0.10), fade,
                                 bridge=CONFIG.get("duck_bridge_seconds", 1.5))
        if bgm_expr:
            bgm_chain = f"{bgm_in}volume='{bgm_expr}':eval=frame,aresample=48000[bgm];"
        else:
            bgm_chain = f"{bgm_in}volume={base},aresample=48000[bgm];"

    if ducking_mode == "sidechaincompress":
        # The narration keys the compressor; split it so it can also be mixed in.
        head = (
            f"{original_in}aresample=48000[o0];"
            "[1:a]aresample=48000,asplit=2[sckey][scnarr];"
            f"[o0][sckey]sidechaincompress="
            f"threshold={CONFIG['ducking_threshold']}:ratio={CONFIG['ducking_ratio']}"
            f":attack={CONFIG['ducking_attack']}:release={CONFIG['ducking_release']}"
            f":knee=2.5:makeup={CONFIG['ducking_makeup']}:level_sc={CONFIG['ducking_level_sc']}[orig];"
        )
        narr = f"[scnarr]volume={narr_vol}[narr];"
        if bgm_chain:
            return head + bgm_chain + narr + "[orig][bgm][narr]amix=inputs=3:duration=first:dropout_transition=0:normalize=0[aout]"
        return head + narr + "[orig][narr]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"

    if ducking_mode == "none":
        return f"{original_in}aresample=48000[orig];" + _amix_tail(narr_vol, bgm_chain)

    # fixed (default): gap-fill ducking envelope on the original track.
    idle = CONFIG.get("idle_orig_volume", 1.0)
    speech_vol = CONFIG.get("speech_ducking_volume", 0.2)
    quiet_vol = CONFIG.get("zone_ducking_volume", 0.12)
    bridge = CONFIG.get("duck_bridge_seconds", 1.5)
    expr = _duck_envelope(tts_segments, idle, speech_vol, quiet_vol, fade, bridge=bridge)
    if expr:
        n_overlap = sum(1 for s in tts_segments if isinstance(s, dict) and s.get("overlaps_speech", True))
        n_quiet = sum(1 for s in tts_segments if isinstance(s, dict) and not s.get("overlaps_speech", True))
        log(f"gap-fill ducking: 间隙原声={idle}, 对白段={speech_vol}({n_overlap}), 安静段={quiet_vol}({n_quiet}), 桥接间隙<{bridge}s")
        orig = f"{original_in}volume='{expr}':eval=frame,aresample=48000[orig];"
    else:
        # No placement info at all: hold the original at a constant level.
        orig = f"{original_in}volume={CONFIG.get('ducking_orig_volume', 0.3)},aresample=48000[orig];"
    return orig + _amix_tail(narr_vol, bgm_chain)
