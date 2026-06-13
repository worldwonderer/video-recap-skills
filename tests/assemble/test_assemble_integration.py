import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'video-assemble' / 'scripts'))
"""Integration characterization tests for the ffmpeg render path.

These run real ffmpeg/ffprobe to lock the behavior of assemble_video (ducking
branches + final-mix loudness normalization). Skipped when ffmpeg is absent.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


from assemble import assemble_video  # noqa: E402
from lib import CONFIG  # noqa: E402

_HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
pytestmark = pytest.mark.skipif(not _HAVE_FFMPEG, reason="ffmpeg/ffprobe not available")


def _make_source_video(path, seconds=4):
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c=blue:s=320x240:d={seconds}:r=25",
        "-f", "lavfi", "-i", f"sine=frequency=220:duration={seconds}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(path),
    ], check=True, capture_output=True)


def _make_narration_wav(path, seconds=1.5):
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-ar", "44100", "-ac", "1", str(path),
    ], check=True, capture_output=True)


def _stream_types(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    ).stdout
    return out.split()


def _segment(work_dir, start, end, overlaps, dur=1.5):
    wav = work_dir / "narr_000.wav"
    _make_narration_wav(wav, dur)
    return [{
        "index": 0, "start": start, "end": end, "narration": "测试解说。",
        "audio_path": str(wav), "audio_duration": dur,
        "pause_after_ms": 250, "overlaps_speech": overlaps,
    }]


def test_assemble_video_runs_with_final_loudnorm(tmp_path, monkeypatch):
    monkeypatch.setitem(CONFIG, "final_loudnorm", True)
    work = tmp_path / "work"
    work.mkdir()
    src = tmp_path / "src.mp4"
    _make_source_video(src, seconds=4)
    # overlaps_speech=True drives the "fixed" ducking branch
    segs = _segment(work, 0.5, 3.5, overlaps=True)
    out = work / "output.mp4"
    assemble_video(src, segs, work, out)
    assert out.exists() and out.stat().st_size > 0
    types = _stream_types(out)
    assert "video" in types and "audio" in types


def test_assemble_video_runs_with_loudnorm_disabled_and_quiet_branch(tmp_path, monkeypatch):
    monkeypatch.setitem(CONFIG, "final_loudnorm", False)
    work = tmp_path / "work2"
    work.mkdir()
    src = tmp_path / "src2.mp4"
    _make_source_video(src, seconds=3)
    # overlaps_speech=False drives the zone/quiet ducking branch
    segs = _segment(work, 0.5, 2.5, overlaps=False, dur=1.0)
    out = work / "out.mp4"
    assemble_video(src, segs, work, out)
    assert out.exists()
    assert "audio" in _stream_types(out)
