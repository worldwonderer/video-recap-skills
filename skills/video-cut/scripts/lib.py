"""Self-contained utilities for the video-cut skill (no cross-skill imports)."""
import os
import subprocess


def log(msg):
    print(f"[video-cut] {msg}", flush=True)


def env_bool(name, default):
    """Read an env var as a boolean (1/true/yes → True; 0/false/no → False)."""
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes")


def env_float(name, default, min_val=None):
    """Read an env var as a float, with an optional minimum clamp."""
    val = os.environ.get(name)
    if val is None:
        return default
    try:
        result = float(val.strip())
    except (ValueError, AttributeError):
        return default
    if min_val is not None:
        result = max(min_val, result)
    return result


CONFIG = {
    "snap_clip_line_end": env_bool("SNAP_CLIP_LINE_END", True),
    "clip_snap_max_extend": env_float("CLIP_SNAP_MAX_EXTEND", 2.0, min_val=0.0),
    "clip_start_snap_max_prepend": env_float("CLIP_START_SNAP_MAX_PREPEND", 1.8, min_val=0.0),
    "clip_start_snap_max_trim": env_float("CLIP_START_SNAP_MAX_TRIM", 0.35, min_val=0.0),
    "clip_join_audio_fade_ms": env_float("CLIP_JOIN_AUDIO_FADE_MS", 30.0, min_val=0.0),
    # Keep clip boundaries off the ORIGINAL footage's hard cuts: a clip that opens/closes a few
    # tenths of a second from a source shot-change shows a brief sliver of the adjacent shot that
    # then hard-cuts again — a visible 闪烁/flicker at the edit point. Snap source_start forward
    # past (and source_end back before) any shot-change within the margin.
    "scene_cut_snap": env_bool("SCENE_CUT_SNAP", True),
    "scene_cut_snap_margin": env_float("SCENE_CUT_SNAP_MARGIN", 0.5, min_val=0.0),    # 边界±此秒内有切镜头才避让
    "scene_cut_detect_threshold": env_float("SCENE_CUT_DETECT_THRESHOLD", 0.4, min_val=0.0),  # ffmpeg scene 分数阈值(硬切)
}


def run_cmd(cmd, **kwargs):
    """Run a command and return the CompletedProcess (stdout/stderr captured)."""
    if isinstance(cmd, list):
        display_parts = []
        for part in cmd:
            text = str(part)
            display_parts.append(text if len(text) <= 240 else text[:237] + "...")
        display = " ".join(display_parts)
    else:
        display = str(cmd)
        if len(display) > 2000:
            display = display[:1997] + "..."
    log(f"运行: {display}")
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def get_video_duration(video_path):
    """Return media duration in seconds via ffprobe, or 0.0 on failure."""
    cmd = ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
           "-of", "csv=p=0", str(video_path)]
    result = run_cmd(cmd)
    if result.returncode != 0:
        return 0.0
    try:
        return float(result.stdout.strip())
    except (TypeError, ValueError):
        return 0.0
