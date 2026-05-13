from config import CONFIG
from common import log, run_cmd

# ── Step 1: 帧提取 ───────────────────────────────────────────────────

def extract_frames(video_path, work_dir, fps=None):
    """提取视频帧"""
    fps = fps or CONFIG["fps"]
    frames_dir = work_dir / "frames"
    frames_dir.mkdir(exist_ok=True)

    output_pattern = str(frames_dir / "frame_%05d.jpg")
    cmd = ["ffmpeg", "-y", "-i", str(video_path),
           "-vf", f"fps={fps}", "-q:v", "2", output_pattern]
    result = run_cmd(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"帧提取失败: {result.stderr}")

    frames = sorted(frames_dir.glob("frame_*.jpg"))
    log(f"提取了 {len(frames)} 帧 ({fps}fps)")
    return frames

