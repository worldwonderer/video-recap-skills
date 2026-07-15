import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'video-understanding' / 'scripts'))
"""Regression tests for detect.py bug fixes (BUG 2 junk filter, BUG 11 silence)."""
import sys
from pathlib import Path
from subprocess import CompletedProcess


import detect
from detect import (
    _filter_junk_scenes,
    detect_scenes,
    detect_silence_periods,
    detect_speech_boundary_anchors,
)


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


def test_detect_silence_compacts_verbose_ffmpeg_failure(monkeypatch, tmp_path):
    logs = []
    verbose = "ffmpeg build configuration " + ("x" * 2000) + " Output file does not contain any stream"
    monkeypatch.setattr("detect.log", lambda msg: logs.append(msg))
    monkeypatch.setattr("detect.run_cmd", lambda cmd, **kw: _fail(verbose))

    assert detect_silence_periods(tmp_path / "silent.mp4", tmp_path) == []

    message = next(msg for msg in logs if "提取失败" in msg)
    assert len(message) < 500
    assert "Output file does not contain any stream" in message


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
    # audio.wav already exists for the same source → extraction skipped, but silencedetect fails
    video = tmp_path / "v.mp4"
    video.write_bytes(b"video")
    (tmp_path / "audio.wav").write_bytes(b"RIFFxxxx")
    detect._write_audio_meta(tmp_path, video)
    logs = []
    monkeypatch.setattr("detect.log", lambda msg: logs.append(msg))
    monkeypatch.setattr("detect.run_cmd", lambda cmd, **kw: _fail("filter error"))

    out = detect_silence_periods(video, tmp_path)
    assert out == []
    assert any("静音检测失败" in m for m in logs)


def test_detect_silence_extracts_to_temp_then_atomic_moves(monkeypatch, tmp_path):
    # happy path: extraction writes temp, silencedetect succeeds with no silence
    video = tmp_path / "v.mp4"
    video.write_bytes(b"video")
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

    out = detect_silence_periods(video, tmp_path)
    assert out == []
    # extraction targeted a .tmp path (atomic move pattern), not audio.wav directly
    assert any(any(p.endswith("audio.wav.tmp") for p in c) for c in calls)
    # promoted into place on success
    assert (tmp_path / "audio.wav").exists()
    assert not (tmp_path / "audio.wav.tmp").exists()
    # a cache file was written for the (successful) empty result
    assert (tmp_path / "silence_periods.json").exists()


def test_detect_speech_boundary_anchors_aligns_sentence_punctuation_to_short_pauses(monkeypatch, tmp_path):
    (tmp_path / "audio.wav").write_bytes(b"RIFF")
    stderr = "\n".join([
        "silence_start: 2.68", "silence_end: 3.19",
        "silence_start: 5.22", "silence_end: 5.81",
        "silence_start: 10.42", "silence_end: 11.02",
        "silence_start: 13.74", "silence_end: 14.34",
    ])
    monkeypatch.setattr("detect.run_cmd", lambda cmd, **kw: _ok(stderr=stderr))
    asr = [{
        "start": 0,
        "end": 15,
        "text": "在那漫长而伟大的旅途走到终点之前，带你重走詹姆斯的二十一年。二零零三年选秀人才辈出，他作为高中生状元进入联盟，二十加五加五，把自己的名字写进历史。虽然生涯前",
    }]

    report = detect_speech_boundary_anchors(tmp_path, asr)

    assert [round(item["time"], 2) for item in report["sentence_anchors"]] == [5.81, 14.34]
    assert all(item["punctuation"] == "。" for item in report["sentence_anchors"])
    assert all(item["confidence"] in {"high", "medium"} for item in report["sentence_anchors"])
    assert (tmp_path / "speech_boundary_anchors.json").exists()


def test_silence_audio_extract_states_wav_format(monkeypatch, tmp_path):
    """BUG: extracting to audio.wav.tmp hides the format from ffmpeg (muxer 'Invalid argument').
    The extraction command must pass -f wav explicitly."""
    video = tmp_path / "v.mp4"
    video.write_bytes(b"video")
    cmds = []

    def fake(cmd, **kw):
        cmds.append([str(c) for c in cmd])
        if any("audio.wav.tmp" in str(c) for c in cmd):
            (tmp_path / "audio.wav.tmp").write_bytes(b"RIFF")  # simulate a successful extract
        return _ok()

    monkeypatch.setattr("detect.run_cmd", fake)
    detect_silence_periods(video, tmp_path, asr_result=[])
    extract = next((c for c in cmds if any("audio.wav.tmp" in x for x in c)), None)
    assert extract is not None, "no audio extraction command issued"
    assert "-f" in extract and "wav" in extract, f"extract cmd missing -f wav: {extract}"


def test_detect_silence_reextracts_audio_when_source_video_changes(monkeypatch, tmp_path):
    """audio.wav reuse must be tied to the source video, not merely file existence."""
    old_video = tmp_path / "old.mp4"
    new_video = tmp_path / "new.mp4"
    old_video.write_bytes(b"old")
    new_video.write_bytes(b"new")
    (tmp_path / "audio.wav").write_bytes(b"old-audio")
    detect._write_audio_meta(tmp_path, old_video)
    calls = []

    def fake_run(cmd, **kw):
        calls.append([str(part) for part in cmd])
        if any(str(part).endswith("audio.wav.tmp") for part in cmd):
            (tmp_path / "audio.wav.tmp").write_bytes(b"new-audio")
        return _ok(stderr="")

    monkeypatch.setattr("detect.log", lambda msg: None)
    monkeypatch.setattr("detect.run_cmd", fake_run)
    monkeypatch.setattr("detect.get_video_duration", lambda path: 10.0)

    detect_silence_periods(new_video, tmp_path, asr_result=[])

    assert (tmp_path / "audio.wav").read_bytes() == b"new-audio"
    assert any(any(part.endswith("audio.wav.tmp") for part in cmd) for cmd in calls)


def test_detect_silence_records_overlap_and_ignores_coarse_grid_asr(monkeypatch, tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"video")
    calls = []
    def fake_run(cmd, **kw):
        calls.append([str(c) for c in cmd])
        if any(str(c).endswith("audio.wav.tmp") for c in cmd):
            (tmp_path / "audio.wav.tmp").write_bytes(b"RIFF")
            return _ok()
        return _ok(stderr="silence_start: 5\nsilence_end: 8\nsilence_start: 20\nsilence_end: 24\n")
    monkeypatch.setattr("detect.run_cmd", fake_run)
    monkeypatch.setattr("detect.get_video_duration", lambda path: 60.0)
    monkeypatch.setattr("detect.log", lambda msg: None)
    asr = [{"start": 0, "end": 30, "text": "a"}, {"start": 30, "end": 60, "text": "b"}]
    out = detect_silence_periods(video, tmp_path, asr_result=asr)
    assert out and all(p["has_speech"] is False for p in out)
    assert all(p["asr_granularity"] == "coarse_grid" for p in out)
    assert all("speech_overlap_ratio" in p and "has_speech_reason" in p for p in out)
    qc = __import__('json').loads((tmp_path / "silence_periods.qc.json").read_text(encoding="utf-8"))
    assert qc["coarse_asr_windows"] == len(out)


def test_detect_silence_marks_true_short_asr_overlap(monkeypatch, tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"video")
    def fake_run(cmd, **kw):
        if any(str(c).endswith("audio.wav.tmp") for c in cmd):
            (tmp_path / "audio.wav.tmp").write_bytes(b"RIFF")
            return _ok()
        return _ok(stderr="silence_start: 5\nsilence_end: 9\n")
    monkeypatch.setattr("detect.run_cmd", fake_run)
    monkeypatch.setattr("detect.get_video_duration", lambda path: 30.0)
    monkeypatch.setattr("detect.log", lambda msg: None)
    out = detect_silence_periods(video, tmp_path, asr_result=[{"start": 5.2, "end": 8.8, "text": "real"}])
    assert out[0]["has_speech"] is True
    assert out[0]["has_speech_reason"] == "asr_overlap_high_confidence"
    assert out[0]["speech_overlap_ratio"] >= 0.8


def test_annotate_quiet_windows_with_asr_is_pure_helper():
    periods = [{"start": 5.0, "end": 9.0, "duration": 4.0, "has_speech": False}]
    annotated, qc = detect.annotate_quiet_windows_with_asr(
        periods,
        [{"start": 5.5, "end": 8.5, "text": "real"}],
        video_duration=30.0,
        configured_segment_seconds=30,
    )
    assert periods[0]["has_speech"] is False
    assert annotated[0]["has_speech"] is True
    assert annotated[0]["has_speech_reason"] == "asr_overlap_high_confidence"
    assert qc["asr_granularity"] == "segment"
