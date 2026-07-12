"""Shared audio automation semantics for ffmpeg render and editable timelines.

This module is stdlib-only and is the single source for ducking window coalescing,
pre-roll/hold/post-roll gain shape, timeline keyframes, and ffmpeg volume terms.
"""


def _round_keyframe(t_s, gain):
    return {"t": round(float(t_s), 4), "gain": round(float(gain), 4)}


def default_bridge(fade):
    return 2 * float(fade or 0)


def coalesce_duck_windows(windows, bridge):
    """Merge [(start, end, gain)] windows separated by gaps below `bridge`.

    A bridged mixed-level span uses the lowest gain across all members so neither
    ffmpeg nor timeline export swells louder in the bridged gap.
    """
    rel = sorted(
        ([float(s), float(e), float(g)] for s, e, g in windows if float(e) > float(s)),
        key=lambda w: w[0],
    )
    if not rel:
        return []
    bridge = float(bridge or 0)
    merged = [rel[0][:]]
    for s, e, gain in rel[1:]:
        if s - merged[-1][1] < bridge:
            merged[-1][1] = max(merged[-1][1], e)
            merged[-1][2] = min(merged[-1][2], gain)
        else:
            merged.append([s, e, gain])
    return merged


def _t_minus(value):
    value = float(value)
    if value < 0:
        return f"t+{abs(value):.2f}"
    return f"t-{value:.2f}"


def duck_ramp_expression(start, end, fade):
    """Return ffmpeg expression for the canonical duck shape.

    Semantics: ramp down during [start-fade, start], hold fully ducked on
    [start, end], and release during [end, end+fade].
    """
    start = float(start)
    end = float(end)
    fade = float(fade or 0)
    if fade <= 0:
        return f"between(t,{start:.2f},{end:.2f})"
    ramp_start = start - fade
    ramp_end = end + fade
    return f"min(1,max(0,min({_t_minus(ramp_start)},{ramp_end:.2f}-t)/{fade:.2f}))"


def ducking_expression(windows, idle, fade):
    """Build the ffmpeg volume expression for coalesced duck windows."""
    merged = list(windows or [])
    if not merged:
        return None
    idle = float(idle)
    terms = [
        f"+({float(level) - idle:.3f})*{duck_ramp_expression(s, e, fade)}"
        for s, e, level in merged
    ]
    return f"max(0,min(1,{idle}{''.join(terms)}))"


def variable_ducking_keyframes(windows, idle, fade, span_start, span_end, bridge=None):
    """Volume keyframes for per-window duck gains using canonical semantics."""
    fade = float(fade or 0)
    if bridge is None:
        bridge = default_bridge(fade)
    span_start = float(span_start)
    span_end = float(span_end)
    rel = sorted(
        (max(span_start, float(w[0])), min(span_end, float(w[1])), float(w[2]))
        for w in windows
        if float(w[1]) > span_start and float(w[0]) < span_end and float(w[1]) > float(w[0])
    )
    merged = coalesce_duck_windows(rel, bridge)
    if not merged:
        return []

    pts = [(span_start, float(idle))]
    for s, e, level in merged:
        pts.append((max(span_start, s - fade), float(idle)))
        pts.append((s, level))
        pts.append((e, level))
        pts.append((min(span_end, e + fade), float(idle)))
    pts.append((span_end, float(idle)))

    pts.sort(key=lambda p: p[0])
    out = []
    for t, gain in pts:
        if out and abs(out[-1][0] - t) < 1e-4:
            out[-1] = (t, min(out[-1][1], gain))
        else:
            out.append((t, gain))
    return [_round_keyframe(t, gain) for t, gain in out]


def fixed_ducking_keyframes(windows, idle, duck, fade, span_start, span_end, bridge=None):
    """Volume keyframes for fixed-gain duck windows using canonical semantics."""
    return variable_ducking_keyframes(
        [(float(s), float(e), float(duck)) for s, e in windows],
        idle,
        fade,
        span_start,
        span_end,
        bridge=bridge,
    )
