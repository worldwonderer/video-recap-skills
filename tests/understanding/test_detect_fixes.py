import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'video-understanding' / 'scripts'))
"""Regression tests for detect.py bug fixes (BUG 2 junk filter, BUG 11 silence)."""
import sys
from pathlib import Path
from subprocess import CompletedProcess


import detect
from detect import _filter_junk_scenes, detect_scenes, detect_silence_periods


def _ok(stdout="", stderr=""):
    return CompletedProcess(args=["ffmpeg"], returncode=0, stdout=stdout, stderr=stderr)


def _fail(stderr="boom"):
    return CompletedProcess(args=["ffmpeg"], returncode=1, stdout="", stderr=stderr)


# ── BUG 2: junk filter must not delete a scene that has any non-junk frame ──

def test_filter_keeps_scene_with_nonjunk_midpoint_even_if_first_frame_is_junk(monkeypatch):
    # [{0-2 黑场}, {2-30 真实场景}] — 模拟黑场被并入长场景后：长场景起始帧是黑的
    scenes = [
        {"start": 0.0, "end": 2.0},     # all junk (black lead-in)
        {"start": 2.0, "end": 30.0},    # real scene: junk only at very start
    ]

    # junk only for timestamps strictly before 2.1s (i.e. the black lead-in and
    # the merged scene's first probe), but NOT at the midpoint/end of the long scene.
    monkeypatch.setattr("detect._is_junk_scene", lambda video, ts: ts < 2.1)

    out = _filter_junk_scenes(scenes, Path("video.mp4"))
    # The long real scene survives because its midpoint (16s) and end (29.9s) are non-junk.
    assert {"start": 2.0, "end": 30.0} in out
    # The pure black lead-in is removed.
    assert {"start": 0.0, "end": 2.0} not in out


def test_filter_drops_scene_only_when_all_probes_are_junk(monkeypatch):
    scenes = [
        {"start": 0.0, "end": 4.0},
        {"start": 4.0, "end": 8.0},
    ]
    # second scene is junk everywhere; first scene junk only at its start probe.
    monkeypatch.setattr(
        "detect._is_junk_scene",
        lambda video, ts: ts >= 4.0 or ts < 0.2,
    )
    out = _filter_junk_scenes(scenes, Path("video.mp4"))
    assert out == [{"start": 0.0, "end": 4.0}]


def test_filter_all_black_falls_back_to_keeping_everything(monkeypatch):
    scenes = [{"start": 0.0, "end": 2.0}, {"start": 2.0, "end": 4.0}]
    monkeypatch.setattr("detect._is_junk_scene", lambda video, ts: True)
    # genuinely all-black: filtering would delete everything → fall back, keep all
    assert _filter_junk_scenes(scenes, Path("video.mp4")) == scenes


# ── BUG 2: ordering — filter runs BEFORE merge ──

def test_detect_scenes_filters_junk_before_merging(monkeypatch, tmp_path):
    # scdet reports a cut at 2.0s → scenes [{0-2 黑场}, {2-30 真实}]
    stderr = "lavfi.scd.time: 2.000\n"
    monkeypatch.setattr("detect.run_cmd", lambda cmd, **kw: _ok(stderr=stderr))
    monkeypatch.setattr("detect.get_video_duration", lambda video: 30.0)
    monkeypatch.setitem(detect.CONFIG, "scene_junk_filter", True)
    monkeypatch.setitem(detect.CONFIG, "scene_merge_min", 4.0)

    order = []

    def fake_filter(scenes, video_path):
        order.append("filter")
        # the 0-2 black lead-in is still an ISOLATED short scene here (not yet merged)
        assert {"start": 0.0, "end": 2.0} in scenes
        # drop the black lead-in, keep the real scene
        return [s for s in scenes if s["start"] != 0.0]

    def fake_merge(scenes, min_duration=4.0):
        order.append("merge")
        return scenes

    monkeypatch.setattr("detect._filter_junk_scenes", fake_filter)
    monkeypatch.setattr("detect._merge_short_scenes", fake_merge)

    scenes = detect_scenes(tmp_path / "v.mp4", tmp_path)
    # filter ran before merge
    assert order == ["filter", "merge"]
    # the long real scene reached the output (was NOT deleted wholesale)
    assert {"start": 2.0, "end": 30.0} in scenes


# ── BUG 11: silence detection respects ffmpeg return codes ──

def test_detect_silence_returns_empty_and_logs_on_extraction_failure(monkeypatch, tmp_path):
    logs = []
    monkeypatch.setattr("detect.log", lambda msg: logs.append(msg))
    monkeypatch.setattr("detect.run_cmd", lambda cmd, **kw: _fail("no audio stream"))

    out = detect_silence_periods(tmp_path / "v.mp4", tmp_path)
    assert out == []
    # no silence_periods.json cached on failure (so it can be retried later)
    assert not (tmp_path / "silence_periods.json").exists()
    # a clear Chinese warning was logged
    assert any("提取失败" in m for m in logs)


def test_detect_silence_does_not_leave_partial_audio_on_failure(monkeypatch, tmp_path):
    monkeypatch.setattr("detect.log", lambda msg: None)

    def fake_run(cmd, **kw):
        # simulate ffmpeg writing a partial temp file then failing
        for part in cmd:
            sp = str(part)
            if sp.endswith("audio.wav.tmp"):
                Path(sp).write_bytes(b"partial")
        return _fail("interrupted")

    monkeypatch.setattr("detect.run_cmd", fake_run)
    detect_silence_periods(tmp_path / "v.mp4", tmp_path)
    # partial temp must be cleaned up and never promoted to audio.wav
    assert not (tmp_path / "audio.wav.tmp").exists()
    assert not (tmp_path / "audio.wav").exists()


def test_detect_silence_returns_empty_on_silencedetect_nonzero(monkeypatch, tmp_path):
    # audio.wav already exists → extraction skipped, but silencedetect fails
    (tmp_path / "audio.wav").write_bytes(b"RIFFxxxx")
    logs = []
    monkeypatch.setattr("detect.log", lambda msg: logs.append(msg))
    monkeypatch.setattr("detect.run_cmd", lambda cmd, **kw: _fail("filter error"))

    out = detect_silence_periods(tmp_path / "v.mp4", tmp_path)
    assert out == []
    assert any("静音检测失败" in m for m in logs)


def test_detect_silence_extracts_to_temp_then_atomic_moves(monkeypatch, tmp_path):
    # happy path: extraction writes temp, silencedetect succeeds with no silence
    calls = []

    def fake_run(cmd, **kw):
        calls.append([str(p) for p in cmd])
        for part in cmd:
            sp = str(part)
            if sp.endswith("audio.wav.tmp"):
                Path(sp).write_bytes(b"audio")
        return _ok(stderr="")  # no silence_start lines

    monkeypatch.setattr("detect.log", lambda msg: None)
    monkeypatch.setattr("detect.run_cmd", fake_run)
    monkeypatch.setattr("detect.get_video_duration", lambda path: 10.0)

    out = detect_silence_periods(tmp_path / "v.mp4", tmp_path)
    assert out == []
    # extraction targeted a .tmp path (atomic move pattern), not audio.wav directly
    assert any(any(p.endswith("audio.wav.tmp") for p in c) for c in calls)
    # promoted into place on success
    assert (tmp_path / "audio.wav").exists()
    assert not (tmp_path / "audio.wav.tmp").exists()
    # a cache file was written for the (successful) empty result
    assert (tmp_path / "silence_periods.json").exists()


def test_silence_audio_extract_states_wav_format(monkeypatch, tmp_path):
    """BUG: extracting to audio.wav.tmp hides the format from ffmpeg (muxer 'Invalid argument').
    The extraction command must pass -f wav explicitly."""
    cmds = []

    def fake(cmd, **kw):
        cmds.append([str(c) for c in cmd])
        if any("audio.wav.tmp" in str(c) for c in cmd):
            (tmp_path / "audio.wav.tmp").write_bytes(b"RIFF")  # simulate a successful extract
        return _ok()

    monkeypatch.setattr("detect.run_cmd", fake)
    detect_silence_periods(tmp_path / "v.mp4", tmp_path, asr_result=[])
    extract = next((c for c in cmds if any("audio.wav.tmp" in x for x in c)), None)
    assert extract is not None, "no audio extraction command issued"
    assert "-f" in extract and "wav" in extract, f"extract cmd missing -f wav: {extract}"
