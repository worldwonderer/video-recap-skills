"""Multi-track timeline model for the recap (backend-neutral, stdlib only).

A `Timeline` is a small, serializable representation of the finished recap as a
set of tracks — exactly like a cut-tool project:

  - one **video** track: the source clip(s), each carrying its own *original
    audio* with a per-clip volume automation (the ducking: a continuous low bed
    under narration, held across short inter-sentence gaps, back up only at the
    lead-in/out and genuine long gaps);
  - one **narration** audio track: the placed TTS beats;
  - an optional **bgm** audio track: a looped music bed with its own ducking;
  - one **subtitle** (text) track: the narration lines.

The canonical renderer is still ffmpeg (`assemble.py`); this model is what it
emits as `timeline.json` and what the *optional* 剪映 exporter consumes. The
model itself knows nothing about ffmpeg or 剪映 — times are plain seconds and
volumes are plain gains, so any backend can read it.
"""

import json

SCHEMA_VERSION = 1


def _kf(t_s, gain):
    return {"t": round(float(t_s), 4), "gain": round(float(gain), 4)}


def ducking_keyframes(windows, idle, duck, fade, span_start, span_end, bridge=None):
    """Volume automation for a track that holds at `idle` and dips to `duck`
    under each narration window, with `fade`-second linear ramps.

    `windows` is a list of (start_s, end_s) narration spans (timeline-absolute).
    Returns timeline-absolute [{t, gain}] keyframes clamped to [span_start,
    span_end]; empty when there is nothing to automate (caller uses a flat gain).
    """
    if bridge is None:
        bridge = 2 * fade
    rel = sorted((max(span_start, w[0]), min(span_end, w[1]))
                 for w in windows if w[1] > span_start and w[0] < span_end and w[1] > w[0])
    if not rel:
        return []
    # Coalesce windows whose gap is below `bridge`: with no room to ramp back to idle and
    # down again between them, the duck must stay held — otherwise the original would pump
    # up and back down between sentences. Genuine gaps (>= bridge) survive as a plateau, so
    # the original still swells there.
    merged = [list(rel[0])]
    for s, e in rel[1:]:
        if s - merged[-1][1] < bridge:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    pts = [(span_start, idle)]
    for s, e in merged:
        # hold the duck across the merged window; ramp in just before, release just after
        pts.append((max(span_start, s - fade), idle))
        pts.append((s, duck))
        pts.append((e, duck))
        pts.append((min(span_end, e + fade), idle))
    pts.append((span_end, idle))
    # sort by time, collapse points at the same instant (last wins for a clean step)
    pts.sort(key=lambda p: p[0])
    out = []
    for t, g in pts:
        if out and abs(out[-1][0] - t) < 1e-4:
            out[-1] = (t, g)
        else:
            out.append((t, g))
    return [_kf(t, g) for t, g in out]


def _coalesce_windows(rel, bridge):
    """Merge sorted (start, end, level) windows whose gap is below `bridge` into one held
    span at the most-ducked (min) level. This is the SAME coalescing the ffmpeg render path
    uses (assemble._coalesce_duck_windows), so the exported 剪映 draft matches the rendered
    mix exactly even when a bridged span mixes speech (louder) and quiet (deeper) beats."""
    if not rel:
        return []
    merged = [list(rel[0])]
    for s, e, level in rel[1:]:
        if s - merged[-1][1] < bridge:
            merged[-1][1] = max(merged[-1][1], e)
            merged[-1][2] = min(merged[-1][2], level)
        else:
            merged.append([s, e, level])
    return merged


def variable_ducking_keyframes(windows, idle, fade, span_start, span_end, bridge=None):
    """Volume automation with a per-window duck gain.

    `windows` is [(start_s, end_s, duck_gain)]. Windows whose gap is below `bridge` coalesce
    into one held span at the most-ducked level (matching the renderer), so the original stays
    ducked across short inter-sentence gaps instead of swelling back to idle; only the
    lead-in/out and genuine gaps >= bridge return to idle. Defaults bridge to 2*fade.
    """
    if bridge is None:
        bridge = 2 * fade
    rel = sorted(
        (max(span_start, float(w[0])), min(span_end, float(w[1])), float(w[2]))
        for w in windows
        if float(w[1]) > span_start and float(w[0]) < span_end and float(w[1]) > float(w[0])
    )
    merged = _coalesce_windows(rel, bridge)
    if not merged:
        return []

    pts = [(span_start, idle)]
    for s, e, level in merged:
        # hold the duck across the merged span; ramp in just before, release just after
        pts.append((max(span_start, s - fade), idle))
        pts.append((s, level))
        pts.append((e, level))
        pts.append((min(span_end, e + fade), idle))
    pts.append((span_end, idle))

    pts.sort(key=lambda p: p[0])
    out = []
    for t, g in pts:
        if out and abs(out[-1][0] - t) < 1e-4:
            # coincident points (overlapping ramps): prefer the lower gain, no swell
            out[-1] = (t, min(out[-1][1], g))
        else:
            out.append((t, g))
    return [_kf(t, g) for t, g in out]


def build_timeline(canvas, duration_s, video_clips, narration_segments,
                   bgm=None, ducking=None, subtitle_segments=None):
    """Assemble a Timeline dict from resolved placement data.

    canvas: {"width", "height", "fps"}
    duration_s: total output length (seconds)
    video_clips: ordered [{"source_path", "source_start", "source_end",
                 "timeline_start", "timeline_end"}] (cut mode: one per clip;
                 full mode: a single clip spanning the whole video).
    narration_segments: placed beats [{"source_path", "timeline_start",
                 "timeline_end", "text", "overlaps_speech", "gain"?}].
    subtitle_segments: optional display-ready text cues [{"text", "timeline_start",
                 "timeline_end"}]. When present, this is authoritative for the
                 subtitle/text track; narration segment text remains raw editor metadata.
    bgm: optional {"source_path", "volume", "ducking_volume"}.
    ducking: {"idle", "speech", "quiet", "fade", "bridge"?} for the original-audio
             automation; None disables original ducking (flat original). `bridge` holds
             the duck across inter-beat gaps shorter than it (defaults to 2*fade).
    """
    windows = [(float(s["timeline_start"]), float(s["timeline_end"]))
               for s in narration_segments
               if s.get("timeline_end", 0) > s.get("timeline_start", 0)]
    duck_windows = [
        (
            float(s["timeline_start"]),
            float(s["timeline_end"]),
            float((ducking or {}).get("speech" if s.get("overlaps_speech", True) else "quiet", 1.0)),
        )
        for s in narration_segments
        if s.get("timeline_end", 0) > s.get("timeline_start", 0)
    ]

    # --- video track: each clip carries its original audio + ducking automation
    video_clip_objs = []
    for c in video_clips:
        ts, te = float(c["timeline_start"]), float(c["timeline_end"])
        audio = {"role": "original", "volume_keyframes": []}
        if ducking is not None:
            audio["volume_keyframes"] = variable_ducking_keyframes(
                duck_windows, ducking["idle"], ducking["fade"], ts, te,
                bridge=ducking.get("bridge"))
            audio["base_gain"] = round(float(ducking["idle"]), 4)
        else:
            audio["base_gain"] = 1.0
        video_clip_objs.append({
            "source_path": c["source_path"],
            "source_start": round(float(c["source_start"]), 4),
            "source_end": round(float(c["source_end"]), 4),
            "timeline_start": round(ts, 4),
            "timeline_end": round(te, 4),
            "audio": audio,
        })

    tracks = [{"kind": "video", "name": "video", "clips": video_clip_objs}]

    # --- narration track
    narr_segs = []
    for s in narration_segments:
        ts, te = float(s["timeline_start"]), float(s["timeline_end"])
        if te <= ts:
            continue
        narr_segs.append({
            "source_path": s["source_path"],
            "timeline_start": round(ts, 4),
            "timeline_end": round(te, 4),
            "gain": round(float(s.get("gain", 1.0)), 4),
            "text": s.get("text", ""),
            "overlaps_speech": bool(s.get("overlaps_speech", True)),
        })
    if narr_segs:
        tracks.append({"kind": "audio", "name": "narration", "role": "narration",
                       "segments": narr_segs})

    # --- bgm track (optional, looped, ducked under narration)
    if bgm and bgm.get("source_path"):
        base = float(bgm.get("volume", 0.18))
        duck = float(bgm.get("ducking_volume", 0.10))
        fade = float(bgm.get("fade") or (ducking or {}).get("fade", 0.25))
        kfs = ducking_keyframes(windows, base, duck, fade, 0.0, duration_s,
                                bridge=(ducking or {}).get("bridge"))
        tracks.append({
            "kind": "audio", "name": "bgm", "role": "bgm", "loop": True,
            "segments": [{
                "source_path": bgm["source_path"],
                "timeline_start": 0.0,
                "timeline_end": round(float(duration_s), 4),
                "gain": round(base, 4),
                "volume_keyframes": kfs,
            }],
        })

    # --- subtitle (text) track
    text_source = subtitle_segments if subtitle_segments is not None else narration_segments
    text_segs = []
    for s in text_source or []:
        if not isinstance(s, dict) or not s.get("text"):
            continue
        try:
            ts = float(s["timeline_start"])
            te = float(s["timeline_end"])
        except (KeyError, TypeError, ValueError):
            continue
        if te <= ts:
            continue
        text_segs.append({
            "text": s.get("text", ""),
            "timeline_start": round(ts, 4),
            "timeline_end": round(te, 4),
        })
    if text_segs:
        tracks.append({"kind": "text", "name": "subtitle", "segments": text_segs})

    return {
        "schema_version": SCHEMA_VERSION,
        "canvas": {"width": int(canvas["width"]), "height": int(canvas["height"]),
                   "fps": float(canvas.get("fps", 30))},
        "duration": round(float(duration_s), 4),
        "tracks": tracks,
    }


def save_timeline(timeline, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(timeline, f, ensure_ascii=False, indent=2)
    return path


def load_timeline(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)
