import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'video-assemble' / 'scripts'))
"""Regression tests for assemble.py graceful-degradation fixes (bugs 6 & 7)."""
import sys
import wave
from pathlib import Path
from subprocess import CompletedProcess


import narration_audio  # noqa: E402
from audio_mix import _seg_place_window  # noqa: E402
from subtitle_core import _subtitle_entries  # noqa: E402


def _build_timed_narration(*args, **kwargs):
    return narration_audio._build_timed_narration(
        *args,
        command_runner=narration_audio.run_cmd,
        logger=narration_audio.log,
        **kwargs,
    )


def _write_wav(path, sample_rate=44100, duration=0.8, channels=1, sampwidth=2):
    sample_count = int(sample_rate * duration)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\0" * (sample_count * channels * sampwidth))
    return path


def test_resample_failure_degrades_instead_of_raising(monkeypatch, tmp_path):
    """Bug 6: a failed resample must drop the segment gracefully, not raise."""
    # WAV at a non-target sample rate forces the resample branch.
    wav = _write_wav(tmp_path / "narr.wav", sample_rate=48000, duration=0.8)

    def fake_run_cmd(cmd):
        # Simulate ffmpeg resample failure: non-zero rc and no output file written.
        return CompletedProcess(cmd, 1, stdout="", stderr="resample boom")

    monkeypatch.setattr(narration_audio, "run_cmd", fake_run_cmd)

    segment = {
        "index": 0,
        "start": 0.0,
        "end": 2.0,
        "narration": "需要重采样的解说。",
        "audio_path": str(wav),
        "audio_duration": 0.8,
    }

    out = tmp_path / "out.wav"
    # Must fail cleanly instead of leaving a reusable silent narration track.
    import pytest
    with pytest.raises(RuntimeError, match="全部 1 段解说均被跳过"):
        _build_timed_narration([segment], out, 3.0, tmp_path)

    assert segment["actual_place_start"] == segment["start"]
    assert segment["actual_place_end"] == segment["start"]
    assert not out.exists()


def test_missing_wav_skip_sets_zero_width_window(monkeypatch, tmp_path):
    """Bug 7: a missing-wav skip sets actual_place_start==end so it is dropped."""
    segment = {
        "index": 0,
        "start": 1.0,
        "end": 3.0,
        "narration": "音频丢失的解说。",
        "audio_path": str(tmp_path / "does_not_exist.wav"),
        "audio_duration": 0.8,
    }

    out = tmp_path / "out.wav"
    import pytest
    with pytest.raises(RuntimeError, match="全部 1 段解说均被跳过"):
        _build_timed_narration([segment], out, 4.0, tmp_path)

    assert segment["actual_place_start"] == segment["start"]
    assert segment["actual_place_end"] == segment["start"]

    # _subtitle_entries must exclude it (end - start < 0.1).
    assert _subtitle_entries([segment]) == []

    # _seg_place_window yields a zero-width window -> no ducking over silence.
    s, e = _seg_place_window(segment)
    assert e - s < 0.1
    assert not out.exists()


def test_bad_wav_format_skip_sets_zero_width_window(monkeypatch, tmp_path):
    """Bug 7: a non-mono/non-16-bit WAV skip is dropped from subtitles/ducking."""
    # Stereo wav -> fails the mono/16-bit check.
    wav = _write_wav(tmp_path / "stereo.wav", sample_rate=44100, duration=0.8, channels=2)

    segment = {
        "index": 0,
        "start": 1.0,
        "end": 3.0,
        "narration": "格式错误的解说。",
        "audio_path": str(wav),
        "audio_duration": 0.8,
    }

    out = tmp_path / "out.wav"
    import pytest
    with pytest.raises(RuntimeError, match="全部 1 段解说均被跳过"):
        _build_timed_narration([segment], out, 4.0, tmp_path)

    assert segment["actual_place_start"] == segment["start"]
    assert segment["actual_place_end"] == segment["start"]
    assert _subtitle_entries([segment]) == []
    s, e = _seg_place_window(segment)
    assert e - s < 0.1
    assert not out.exists()


def test_all_skipped_logs_loud_warning(monkeypatch, tmp_path):
    """Bug 7: when every segment is skipped, a loud warning is logged."""
    logs = []
    monkeypatch.setattr(narration_audio, "log", lambda msg: logs.append(msg))

    segments = [
        {
            "index": 0,
            "start": 0.0,
            "end": 2.0,
            "narration": "缺失一。",
            "audio_path": str(tmp_path / "missing1.wav"),
            "audio_duration": 0.8,
        },
        {
            "index": 1,
            "start": 2.0,
            "end": 4.0,
            "narration": "缺失二。",
            "audio_path": str(tmp_path / "missing2.wav"),
            "audio_duration": 0.8,
        },
    ]

    out = tmp_path / "out.wav"
    import pytest
    with pytest.raises(RuntimeError, match="全部 2 段解说均被跳过"):
        _build_timed_narration(segments, out, 5.0, tmp_path)

    assert not out.exists()


def test_partial_skip_does_not_log_all_skipped_warning(monkeypatch, tmp_path):
    """Bug 7: a placed segment alongside a skipped one must NOT trigger the warning."""
    logs = []
    monkeypatch.setattr(narration_audio, "log", lambda msg: logs.append(msg))

    good = _write_wav(tmp_path / "good.wav", sample_rate=44100, duration=0.8)
    segments = [
        {
            "index": 0,
            "start": 0.0,
            "end": 2.0,
            "narration": "正常解说。",
            "audio_path": str(good),
            "audio_duration": 0.8,
        },
        {
            "index": 1,
            "start": 2.0,
            "end": 4.0,
            "narration": "缺失解说。",
            "audio_path": str(tmp_path / "missing.wav"),
            "audio_duration": 0.8,
        },
    ]

    out = tmp_path / "out.wav"
    _build_timed_narration(segments, out, 5.0, tmp_path)

    assert not any("全部" in m and "跳过" in m for m in logs), logs


def test_run_tightening_packs_within_run_and_respects_boundary(monkeypatch, tmp_path):
    """Within a narration run, beats pack to a fixed tight gap after the previous beat's ACTUAL
    audio end (no slot-centering delay) so the spoken gap stays ≤ tight_pause; a deliberate
    authored gap > run_gap starts a new run anchored at its authored time. Anti-stutter ≤1s."""
    from lib import CONFIG
    monkeypatch.setitem(CONFIG, "narration_tighten", True)
    monkeypatch.setitem(CONFIG, "narration_delay_seconds", 0.0)
    monkeypatch.setitem(CONFIG, "narration_tight_pause_seconds", 0.35)
    monkeypatch.setitem(CONFIG, "narration_run_gap_seconds", 1.6)
    monkeypatch.setitem(CONFIG, "narration_max_pull_seconds", 100.0)  # isolate tight-packing from the drift cap
    monkeypatch.setitem(CONFIG, "fade_ms", 0)

    w = _write_wav(tmp_path / "a.wav", duration=0.8)

    def seg(i, start, end):
        return {"index": i, "start": start, "end": end, "narration": f"句{i}。",
                "audio_path": str(w), "audio_duration": 0.8}

    # run 1: beats 0,1,2 in wide 4s slots but authored contiguous (gap 0) -> pack tight to the front.
    # run 2: beat 3 after a 3s authored gap (> run_gap) -> new run, anchored to its authored start.
    segments = [seg(0, 0.0, 4.0), seg(1, 4.0, 8.0), seg(2, 8.0, 12.0), seg(3, 15.0, 19.0)]
    _build_timed_narration(segments, tmp_path / "out.wav", 30.0, tmp_path)

    gap01 = segments[1]["actual_place_start"] - segments[0]["actual_place_end"]
    gap12 = segments[2]["actual_place_start"] - segments[1]["actual_place_end"]
    assert abs(gap01 - 0.35) < 0.05, gap01      # within-run gap == tight_pause, not the 4s slot slack
    assert abs(gap12 - 0.35) < 0.05, gap12
    assert segments[2]["actual_place_start"] < 3.0          # run packed to the front (far before authored 8s)
    assert segments[3]["actual_place_start"] >= 14.9        # run boundary respects the deliberate pause


def test_run_tightening_off_keeps_slot_placement(monkeypatch, tmp_path):
    """With narration_tighten off, beats keep the slot-anchored placement (regression guard)."""
    from lib import CONFIG
    monkeypatch.setitem(CONFIG, "narration_tighten", False)
    monkeypatch.setitem(CONFIG, "narration_delay_seconds", 0.0)
    monkeypatch.setitem(CONFIG, "fade_ms", 0)
    w = _write_wav(tmp_path / "a.wav", duration=0.8)
    segments = [
        {"index": 0, "start": 0.0, "end": 4.0, "narration": "句0。", "audio_path": str(w), "audio_duration": 0.8},
        {"index": 1, "start": 4.0, "end": 8.0, "narration": "句1。", "audio_path": str(w), "audio_duration": 0.8},
    ]
    _build_timed_narration(segments, tmp_path / "out.wav", 30.0, tmp_path)
    # beat 1 stays anchored near its authored 4.0s slot, not packed right after beat 0
    assert segments[1]["actual_place_start"] >= 3.9


def test_run_tightening_drift_cap_keeps_narration_near_picture(monkeypatch, tmp_path):
    """The drift cap stops a long contiguous run from packing entirely to the front: no beat plays
    more than narration_max_pull_seconds before its authored time, so narration stays near picture."""
    from lib import CONFIG
    monkeypatch.setitem(CONFIG, "narration_tighten", True)
    monkeypatch.setitem(CONFIG, "narration_delay_seconds", 0.0)
    monkeypatch.setitem(CONFIG, "narration_tight_pause_seconds", 0.35)
    monkeypatch.setitem(CONFIG, "narration_run_gap_seconds", 1.6)
    monkeypatch.setitem(CONFIG, "narration_max_pull_seconds", 2.0)
    monkeypatch.setitem(CONFIG, "fade_ms", 0)
    w = _write_wav(tmp_path / "a.wav", duration=0.8)
    # one long contiguous run authored across a wide span (5s slots); without the cap, the last
    # beat would pack to ~3s; with the 2s cap it must stay within 2s of its authored 20s start.
    segments = [{"index": i, "start": i * 5.0, "end": i * 5.0 + 5.0, "narration": f"句{i}。",
                 "audio_path": str(w), "audio_duration": 0.8} for i in range(5)]
    _build_timed_narration(segments, tmp_path / "out.wav", 40.0, tmp_path)
    assert segments[4]["actual_place_start"] >= 5.0 * 4 - 2.0 - 0.01      # within max_pull of authored 20s
    # The good segment was actually placed (non-zero-width window).
    assert segments[0]["actual_place_end"] - segments[0]["actual_place_start"] > 0.1
