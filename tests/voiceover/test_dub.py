import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "video-voiceover" / "scripts"))
import dub  # noqa: E402


def test_atempo_chain():
    assert dub._atempo_chain(1.3) == "atempo=1.3000"
    assert dub._atempo_chain(3.0).startswith("atempo=2.0,atempo=")  # >2x is chained
    assert dub._atempo_chain(0.1) == "atempo=0.5000"  # floored at 0.5


def test_ref_window_clamps_short_video():
    start, dur = dub._ref_window(3.0, 2.0, 10.0)
    assert 0.0 <= start <= 1.0
    assert dur >= 2.0
    assert start + dur <= 3.01


def test_ref_window_normal_video():
    start, dur = dub._ref_window(60.0, 2.0, 10.0)
    assert (start, dur) == (2.0, 10.0)


def test_build_dub_track_anchors_line_at_its_start(tmp_path):
    """Each line is placed at its own source start; everything before it is silence (so the dub
    tracks the picture and never drifts/repeats)."""
    line_wav = tmp_path / "line.wav"
    with wave.open(str(line_wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(dub.CLONE_SR)
        w.writeframes(b"\x10\x10" * int(0.5 * dub.CLONE_SR))  # 0.5s of non-silence
    out = tmp_path / "track.wav"
    dub._build_dub_track([{"start": 1.0, "fitted_wav": str(line_wav)}], 3.0, out)
    with wave.open(str(out), "rb") as w:
        assert w.getframerate() == dub.CLONE_SR
        frames = w.readframes(w.getnframes())
    off = int(1.0 * dub.CLONE_SR) * 2
    assert frames[:off] == b"\x00" * off              # silence before the line's start
    assert frames[off:off + 4] == b"\x10\x10\x10\x10"  # the line lands exactly at 1.0s


def test_build_dub_track_skips_missing_and_mismatched(tmp_path):
    """A line with no fitted wav, or a wrong-rate wav, is skipped (never crashes the render)."""
    bad = tmp_path / "bad.wav"
    with wave.open(str(bad), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)  # not CLONE_SR → must be skipped
        w.writeframes(b"\x20\x20" * 1000)
    out = tmp_path / "track.wav"
    dub._build_dub_track(
        [{"start": 0.0}, {"start": 0.5, "fitted_wav": str(bad)}], 2.0, out)
    with wave.open(str(out), "rb") as w:
        frames = w.readframes(w.getnframes())
    assert frames == b"\x00" * len(frames)  # nothing placed → full silence


def test_brief_lists_windows_for_the_agent():
    md = dub._brief_md([{"start": 0.0, "end": 6.0, "text": "Hello there."}], 6.0)
    assert "dub_script.json" in md
    assert '[{"start"' in md
    assert "Hello there." in md
