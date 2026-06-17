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
