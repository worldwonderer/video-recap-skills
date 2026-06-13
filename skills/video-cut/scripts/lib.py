"""Self-contained utilities for the video-cut skill (no cross-skill imports)."""
import subprocess


def log(msg):
    print(f"[video-cut] {msg}", flush=True)


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
