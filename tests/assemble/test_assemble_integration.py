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



def _make_silent_source_video(path, seconds=3):
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c=red:s=320x240:d={seconds}:r=25",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        str(path),
    ], check=True, capture_output=True)


def test_assemble_video_runs_when_source_has_no_audio_stream(tmp_path, monkeypatch):
    monkeypatch.setitem(CONFIG, "final_loudnorm", False)
    monkeypatch.setitem(CONFIG, "mask_source_subtitles", False)
    work = tmp_path / "silent_work"
    work.mkdir()
    src = tmp_path / "silent_src.mp4"
    _make_silent_source_video(src, seconds=3)
    segs = _segment(work, 0.5, 2.5, overlaps=False, dur=1.0)
    out = work / "silent_out.mp4"

    assemble_video(src, segs, work, out)

    assert out.exists() and out.stat().st_size > 0
    assert "audio" in _stream_types(out)


def _make_422_source_video(path, seconds=2, w=320, h=240):
    """A 4:2:2 source: plays on desktop but fails on WeChat/mobile if passed through as-is.
    4:2:2 (unlike 4:2:0) permits ODD dimensions, which is how an odd-height source arises."""
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c=green:s={w}x{h}:d={seconds}:r=25",
        "-f", "lavfi", "-i", f"sine=frequency=220:duration={seconds}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv422p",
        "-c:a", "aac", "-shortest", str(path),
    ], check=True, capture_output=True)


def _pix_fmt(path):
    return subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=pix_fmt", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    ).stdout.strip()


def _dims(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(path)],
        capture_output=True, text=True,
    ).stdout.strip()
    return tuple(int(x) for x in out.split("x"))


def _top_level_atoms(path):
    """Ordered list of top-level mp4 box types — enough to verify moov precedes mdat."""
    atoms = []
    data = Path(path).read_bytes()
    i, n = 0, len(data)
    while i + 8 <= n:
        size = int.from_bytes(data[i:i + 4], "big")
        atoms.append(data[i + 4:i + 8].decode("latin-1", "replace"))
        if size == 1:  # 64-bit largesize
            if i + 16 > n:
                break
            size = int.from_bytes(data[i + 8:i + 16], "big")
        if size == 0:  # box extends to EOF
            break
        if size < 8:
            break
        i += size
    return atoms


def test_output_is_yuv420p_and_faststart_on_reencode(tmp_path, monkeypatch):
    """Re-encoded recaps must be 8-bit 4:2:0 (universally decodable) with a front-loaded
    moov atom (progressive web/social playback). Regression guard for the output encode."""
    monkeypatch.setitem(CONFIG, "final_loudnorm", False)
    monkeypatch.setitem(CONFIG, "mask_source_subtitles", False)
    monkeypatch.setitem(CONFIG, "burn_subtitles", False)
    monkeypatch.setitem(CONFIG, "force_video_reencode", True)  # drives the bare libx264 branch
    work = tmp_path / "w_pf"
    work.mkdir()
    src = tmp_path / "src422.mp4"
    _make_422_source_video(src, seconds=2)
    assert _pix_fmt(src) == "yuv422p"  # precondition: a non-420 source
    out = work / "recap.mp4"
    segs = _segment(work, 0.3, 1.5, overlaps=True, dur=1.0)
    assemble_video(src, segs, work, out)
    assert out.exists() and out.stat().st_size > 0
    assert _pix_fmt(out) == "yuv420p", "re-encode must force 8-bit 4:2:0"
    atoms = _top_level_atoms(out)
    assert "moov" in atoms and "mdat" in atoms
    assert atoms.index("moov") < atoms.index("mdat"), f"moov not front-loaded: {atoms}"


def test_odd_height_422_force_reencode_does_not_abort(tmp_path, monkeypatch):
    """yuv420p needs even dims; an odd-height 4:2:2 source must still produce a valid file
    (the bare force_video_reencode branch). Without the even-normalize, libx264 aborts to 0 bytes."""
    monkeypatch.setitem(CONFIG, "final_loudnorm", False)
    monkeypatch.setitem(CONFIG, "mask_source_subtitles", False)
    monkeypatch.setitem(CONFIG, "burn_subtitles", False)
    monkeypatch.setitem(CONFIG, "force_video_reencode", True)
    work = tmp_path / "w_odd_bare"
    work.mkdir()
    src = tmp_path / "src_odd.mp4"
    _make_422_source_video(src, seconds=2, w=320, h=241)  # ODD height, 4:2:2
    assert _dims(src)[1] % 2 == 1  # precondition: odd height
    out = work / "recap_odd.mp4"
    segs = _segment(work, 0.3, 1.5, overlaps=True, dur=1.0)
    assemble_video(src, segs, work, out)
    assert out.exists() and out.stat().st_size > 0
    assert _pix_fmt(out) == "yuv420p"
    w, h = _dims(out)
    assert w % 2 == 0 and h % 2 == 0  # normalized to even


def test_odd_height_422_with_burned_subs_does_not_abort(tmp_path, monkeypatch):
    """Same hazard via the vf_chain branch (burned subtitles, no downscale): odd-height
    4:2:2 source must render to even yuv420p rather than aborting."""
    monkeypatch.setitem(CONFIG, "final_loudnorm", False)
    monkeypatch.setitem(CONFIG, "mask_source_subtitles", False)
    monkeypatch.setitem(CONFIG, "burn_subtitles", True)  # drives the vf_chain branch
    monkeypatch.setitem(CONFIG, "force_video_reencode", False)
    work = tmp_path / "w_odd_burn"
    work.mkdir()
    src = tmp_path / "src_odd_burn.mp4"
    _make_422_source_video(src, seconds=2, w=320, h=241)
    out = work / "recap_odd_burn.mp4"
    segs = _segment(work, 0.3, 1.5, overlaps=True, dur=1.0)
    assemble_video(src, segs, work, out)
    assert out.exists() and out.stat().st_size > 0
    assert _pix_fmt(out) == "yuv420p"
    w, h = _dims(out)
    assert w % 2 == 0 and h % 2 == 0
