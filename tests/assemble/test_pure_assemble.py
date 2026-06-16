import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'video-assemble' / 'scripts'))
import json
import pytest  # noqa: F401
from subprocess import CompletedProcess  # noqa: F401
from assemble import _wrap_subtitle_text, _adjust_tts_speed, _apply_narration_speed, _assembly_manifest_payload, _build_audio_filter_complex, _build_timed_narration, _build_video_clips, _emit_timeline, _resolve_final_output, _value_fingerprint, _escape_ass_text, _generate_ass, _generate_srt, _seconds_to_ass_time, _seconds_to_srt_time, _source_subtitle_mask_filter, _subtitle_burn_filter, assemble_video, assembly_settings_fingerprint, final_loudnorm_filter
from lib import CONFIG


def test_adjust_tts_speed_derives_outputs_from_audio_name_only(monkeypatch, tmp_path):
    """Parent dirs containing .wav must not redirect adjusted files outside the audio dir."""
    wav_parent = tmp_path / "episode.wav-cache"
    wav_parent.mkdir()
    src = wav_parent / "narr_000.wav"
    src.write_bytes(b"wav")
    commands = []

    def fake_run_cmd(cmd, **kw):
        commands.append(cmd)
        Path(cmd[-1]).write_bytes(b"adjusted")
        return CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("assemble.get_video_duration", lambda path: 2.2 if Path(path) == src else 2.0)
    monkeypatch.setattr("assemble.run_cmd", fake_run_cmd)

    adjusted, actual_dur = _adjust_tts_speed(src, target_duration=2.0, work_dir=tmp_path)

    assert actual_dur == 2.0
    assert Path(adjusted) == wav_parent / "narr_000_adj.wav"
    assert commands[-1][-1] == str(wav_parent / "narr_000_adj.wav")


def test_adjust_tts_speed_truncation_keeps_output_next_to_audio(monkeypatch, tmp_path):
    wav_parent = tmp_path / "episode.wav-cache"
    wav_parent.mkdir()
    src = wav_parent / "narr_000.wav"
    src.write_bytes(b"wav")
    commands = []

    def fake_run_cmd(cmd, **kw):
        commands.append(cmd)
        Path(cmd[-1]).write_bytes(b"cut")
        return CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("assemble.get_video_duration", lambda path: 10.0 if Path(path) == src else 1.0)
    monkeypatch.setattr("assemble.run_cmd", fake_run_cmd)

    adjusted, actual_dur = _adjust_tts_speed(src, target_duration=1.0, work_dir=tmp_path)

    assert actual_dur == 1.0
    assert Path(adjusted) == wav_parent / "narr_000_cut.wav"
    assert commands[-1][-1] == str(wav_parent / "narr_000_cut.wav")


def test_build_video_clips_prefers_fingerprint_matched_validated_cut_plan(monkeypatch, tmp_path):
    original = tmp_path / "original.mp4"
    edited = tmp_path / "edited_source.mp4"
    original.write_bytes(b"orig")
    edited.write_bytes(b"edited")
    import json
    raw_payload = {"clips": [{"source_start": 99.0, "source_end": 100.0}]}
    (tmp_path / "clip_plan.json").write_text(json.dumps(raw_payload), encoding="utf-8")
    (tmp_path / "clip_plan_validated.json").write_text(json.dumps({
        "raw_plan_fingerprint": _value_fingerprint(raw_payload),
        "clips": [
            {"clip_id": 0, "source_start": 10.0, "source_end": 20.0, "output_start": 0.0, "output_end": 10.0},
            {"clip_id": 1, "source_start": 40.0, "source_end": 45.0, "output_start": 10.0, "output_end": 15.0},
        ],
    }), encoding="utf-8")
    monkeypatch.setitem(CONFIG, "source_video", str(original))
    monkeypatch.setitem(CONFIG, "source_video_explicit", True)

    clips = _build_video_clips(edited, tmp_path, duration_s=15.0)

    assert clips == [
        {"source_path": str(original), "source_start": 10.0, "source_end": 20.0, "timeline_start": 0.0, "timeline_end": 10.0},
        {"source_path": str(original), "source_start": 40.0, "source_end": 45.0, "timeline_start": 10.0, "timeline_end": 15.0},
    ]


def test_build_video_clips_ignores_ambient_source_video_without_explicit_opt_in(monkeypatch, tmp_path):
    original = tmp_path / "ambient_original.mp4"
    edited = tmp_path / "edited_source.mp4"
    original.write_bytes(b"orig")
    edited.write_bytes(b"edited")
    (tmp_path / "clip_plan.json").write_text(
        json.dumps({"clips": [{"start": 10.0, "end": 20.0}]}), encoding="utf-8"
    )
    monkeypatch.setitem(CONFIG, "source_video", str(original))
    monkeypatch.setitem(CONFIG, "source_video_explicit", False)

    clips = _build_video_clips(edited, tmp_path, duration_s=15.0)
    manifest = _assembly_manifest_payload(edited, [], tmp_path, tmp_path / "output.mp4")

    assert clips == [{
        "source_path": str(edited),
        "source_start": 0.0,
        "source_end": 15.0,
        "timeline_start": 0.0,
        "timeline_end": 15.0,
    }]
    assert manifest["source_video"] is None
    assert manifest["source_video_fingerprint"] is None


def test_build_video_clips_ignores_stale_validated_cut_plan(monkeypatch, tmp_path):
    import os

    original = tmp_path / "original.mp4"
    edited = tmp_path / "edited_source.mp4"
    original.write_bytes(b"orig")
    edited.write_bytes(b"edited")
    (tmp_path / "clip_plan.json").write_text(
        '{"clips":[{"start":40.0,"end":45.0}]}', encoding="utf-8"
    )
    (tmp_path / "clip_plan_validated.json").write_text(
        '{"clips":[{"clip_id":0,"source_start":0.0,"source_end":10.0,"output_start":0.0,"output_end":10.0}]}',
        encoding="utf-8",
    )
    os.utime(tmp_path / "clip_plan_validated.json", (1_000, 1_000))
    os.utime(tmp_path / "clip_plan.json", (1_000, 1_000))
    monkeypatch.setitem(CONFIG, "source_video", str(original))
    monkeypatch.setitem(CONFIG, "source_video_explicit", True)

    clips = _build_video_clips(edited, tmp_path, duration_s=5.0)

    assert clips == [
        {"source_path": str(original), "source_start": 40.0, "source_end": 45.0, "timeline_start": 0.0, "timeline_end": 5.0},
    ]


def test_assemble_main_creates_missing_output_dir(monkeypatch, tmp_path):
    import sys
    import assemble

    video = tmp_path / "input.mp4"
    video.write_bytes(b"video")
    stale_source = tmp_path / "stale_source.mp4"
    stale_source.write_bytes(b"stale")
    monkeypatch.setitem(CONFIG, "source_video", str(stale_source))
    work = tmp_path / "work"
    work.mkdir()
    (work / "tts_meta.json").write_text(json.dumps({"segments": []}), encoding="utf-8")
    (work / "timeline.json").write_text("{}", encoding="utf-8")
    (work / "narration.wav").write_bytes(b"narration")
    (work / "subtitles.srt").write_text("", encoding="utf-8")
    missing_out = tmp_path / "missing" / "nested"

    def fake_assemble(input_video, tts_segments, work_dir, output_path):
        Path(output_path).write_bytes(b"mp4")
        return output_path

    monkeypatch.setattr("assemble.assemble_video", fake_assemble)
    monkeypatch.setattr(sys, "argv", [
        "assemble.py", str(video), "--work-dir", str(work),
        "--output-dir", str(missing_out), "--recap-stem", "demo",
    ])

    assemble.main()

    assert (missing_out / "recap_demo.mp4").read_bytes() == b"mp4"
    manifest = json.loads((work / "assembly_manifest.json").read_text(encoding="utf-8"))
    assert manifest["source_video"] is None
    assert manifest["source_video_fingerprint"] is None
    assert manifest["final_output"].endswith("recap_demo.mp4")


def test_resolve_final_output_overwrites_stable_alias(tmp_path):
    """The recap output is always the stable alias recap_<stem>.mp4 (overwritten in
    place), so the iterate-on-narration loop refreshes one file instead of spawning
    fingerprint-suffixed copies of every render."""
    (tmp_path / "recap_clip.mp4").write_bytes(b"previous-render")

    resolved = _resolve_final_output(tmp_path, "clip")

    assert resolved == tmp_path / "recap_clip.mp4"


def test_source_subtitle_mask_filter_toggles_with_config(monkeypatch):
    monkeypatch.setitem(CONFIG, "burn_subtitles", False)  # isolate the raw ratio from the 2-line band
    monkeypatch.setitem(CONFIG, "mask_source_subtitles", False)
    assert _source_subtitle_mask_filter() is None

    monkeypatch.setitem(CONFIG, "mask_source_subtitles", True)
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_ratio", 0.15)
    f = _source_subtitle_mask_filter()
    assert f is not None and f.startswith("drawbox=") and "0.150" in f and "t=fill" in f

    monkeypatch.setitem(CONFIG, "source_subtitle_mask_ratio", 0.0)  # ratio 0 -> no mask
    assert _source_subtitle_mask_filter() is None


def test_mask_band_grows_to_cover_two_line_burned_subtitle(monkeypatch):
    """Bug: a wrapped 2-line subtitle spilled above the 14% mask band onto the picture.
    When burning, the band must be tall enough to sit behind the whole 2-line block."""
    monkeypatch.setitem(CONFIG, "mask_source_subtitles", True)
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_ratio", 0.14)
    monkeypatch.setitem(CONFIG, "subtitle_font_size", 42)
    monkeypatch.setitem(CONFIG, "subtitle_margin_v", 30)
    monkeypatch.setitem(CONFIG, "subtitle_play_res_y", 720)
    # burning -> band covers margin_v + 2 lines, larger than the raw 0.14
    monkeypatch.setitem(CONFIG, "burn_subtitles", True)
    import re
    ratio_on = float(re.search(r"ih-ih\*([0-9.]+)", _source_subtitle_mask_filter()).group(1))
    expected = (30 + 2 * 42 * 1.25 + 12) / 720
    assert ratio_on >= 0.18, ratio_on
    assert abs(ratio_on - expected) < 0.01, (ratio_on, expected)
    # not burning -> stays at the raw 0.14
    monkeypatch.setitem(CONFIG, "burn_subtitles", False)
    ratio_off = float(re.search(r"ih-ih\*([0-9.]+)", _source_subtitle_mask_filter()).group(1))
    assert abs(ratio_off - 0.14) < 0.001, ratio_off


def test_apply_narration_speed_atempos_each_segment(monkeypatch, tmp_path):
    src = tmp_path / "narr_000.wav"
    src.write_bytes(b"RIFFwav")
    segs = [{"index": 0, "audio_path": str(src), "audio_duration": 5.0}]
    cmds = []

    def fake_run_cmd(cmd, **kw):
        cmds.append(cmd)
        Path(cmd[-1]).write_bytes(b"sped")
        return CompletedProcess(cmd, 0, "", "")

    monkeypatch.setitem(CONFIG, "narration_speed", 1.12)
    monkeypatch.setattr("assemble.run_cmd", fake_run_cmd)
    monkeypatch.setattr("assemble.get_video_duration", lambda p: 4.46)

    _apply_narration_speed(segs, tmp_path)

    assert any("atempo=1.120" in " ".join(c) for c in cmds)
    assert segs[0]["audio_path"].endswith("_spd_0.wav")
    assert segs[0]["audio_duration"] == 4.46


def test_apply_narration_speed_noop_at_1x(monkeypatch, tmp_path):
    src = tmp_path / "narr_000.wav"
    src.write_bytes(b"x")
    segs = [{"index": 0, "audio_path": str(src), "audio_duration": 5.0}]

    def boom(*a, **k):
        raise AssertionError("must not re-encode at speed 1.0")

    monkeypatch.setitem(CONFIG, "narration_speed", 1.0)
    monkeypatch.setattr("assemble.run_cmd", boom)
    _apply_narration_speed(segs, tmp_path)
    assert segs[0]["audio_path"] == str(src)  # unchanged


def test_seconds_to_srt_time():
    # 3661.5s = 1h 1m 1.5s
    result = _seconds_to_srt_time(3661.5)
    assert result.startswith("01:01:01")
    # 0s
    assert _seconds_to_srt_time(0) == "00:00:00,000"


def test_seconds_to_ass_time():
    assert _seconds_to_ass_time(3661.5) == "1:01:01.50"
    assert _seconds_to_ass_time(0) == "0:00:00.00"


def test_generate_srt_uses_actual_placement(tmp_path):
    _generate_srt([
        {"start": 0.0, "end": 2.0, "actual_place_start": 0.5, "actual_place_end": 1.7, "narration": "真实放置时间。"},
        {"start": 3.0, "end": 3.05, "narration": "过短跳过。"},
    ], tmp_path)

    srt = (tmp_path / "subtitles.srt").read_text(encoding="utf-8")
    assert "00:00:00,500 --> 00:00:01,700" in srt
    assert "真实放置时间" in srt
    assert "过短跳过" not in srt


def test_generate_ass_escapes_text_and_writes_style(tmp_path):
    _generate_ass([
        {
            "start": 1.0,
            "end": 4.0,
            "actual_place_start": 1.25,
            "actual_place_end": 3.5,
            "narration": "第一行{重点}\\路径\n第二行",
        }
    ], tmp_path)

    ass = (tmp_path / "subtitles.ass").read_text(encoding="utf-8")
    assert "[V4+ Styles]" in ass
    assert "Style: Default" in ass
    assert "Dialogue: 0,0:00:01.25,0:00:03.50" in ass
    assert r"第一行\{重点\}\\路径\N第二行" in ass
    assert _escape_ass_text("{x}\\y") == r"\{x\}\\y"


def test_build_timed_narration_clamps_delay_to_slot(monkeypatch, tmp_path):
    import wave

    wav = tmp_path / "narr.wav"
    sample_rate = 44100
    sample_count = int(sample_rate * 0.8)
    with wave.open(str(wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\0\0" * sample_count)

    segment = {
        "index": 0,
        "start": 0.0,
        "end": 1.0,
        "narration": "短槽位解说。",
        "audio_path": str(wav),
        "audio_duration": 0.8,
    }
    monkeypatch.setitem(CONFIG, "narration_delay_seconds", 1.5)
    monkeypatch.setitem(CONFIG, "narration_tail_pad_seconds", 0.1)

    _build_timed_narration([segment], tmp_path / "out.wav", 2.0, tmp_path)

    assert segment["actual_place_start"] == pytest.approx(0.1, abs=0.02)
    assert segment["actual_place_end"] == pytest.approx(0.9, abs=0.02)


def test_subtitle_burn_filter_escapes_path():
    path = Path("/tmp/video recap/a:b,c[1].ass")
    filt = _subtitle_burn_filter(path)
    assert filt.startswith("subtitles=")
    assert "video recap" in filt
    assert r"a\:b\,c\[1\].ass" in filt


def test_assemble_video_burns_ass_subtitles(monkeypatch, tmp_path):
    video = tmp_path / "input.mp4"
    video.write_bytes(b"video")
    wav = tmp_path / "narr.wav"
    wav.write_bytes(b"wav")
    output = tmp_path / "output.mp4"
    commands = []

    def fake_run_cmd(cmd):
        commands.append(cmd)
        output.write_bytes(b"mp4")
        return CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setitem(CONFIG, "burn_subtitles", True)
    monkeypatch.setitem(CONFIG, "mask_source_subtitles", False)  # isolate burn behavior from the mask default
    monkeypatch.setattr("assemble.get_video_duration", lambda path: 4.0)
    monkeypatch.setattr("assemble._build_timed_narration", lambda segments, out, duration, wd: Path(out).write_bytes(b"narration"))
    monkeypatch.setattr("assemble.run_cmd", fake_run_cmd)

    assemble_video(video, [{
        "start": 0.0,
        "end": 3.0,
        "actual_place_start": 0.2,
        "actual_place_end": 2.5,
        "narration": "压制字幕。",
        "audio_path": str(wav),
        "audio_duration": 1.0,
    }], tmp_path, output)

    ffmpeg_cmd = commands[-1]
    assert (tmp_path / "subtitles.srt").exists()
    assert (tmp_path / "subtitles.ass").exists()
    assert "-vf" in ffmpeg_cmd
    assert any(str(arg).startswith("subtitles=") for arg in ffmpeg_cmd)
    assert "-c:v" in ffmpeg_cmd
    assert "libx264" in ffmpeg_cmd


def test_assemble_video_rejects_empty_tts_segments(tmp_path):
    video = tmp_path / "input.mp4"
    video.write_bytes(b"video")

    with pytest.raises(RuntimeError, match="没有有效解说音频"):
        assemble_video(video, [], tmp_path, tmp_path / "output.mp4")


def test_emit_timeline_failure_is_not_swallowed(monkeypatch, tmp_path):
    def boom(*args, **kwargs):
        raise RuntimeError("timeline schema failure")

    monkeypatch.setattr("assemble._probe_canvas", lambda path: {"width": 1280, "height": 720, "fps": 30.0})
    monkeypatch.setattr(
        "assemble._build_video_clips",
        lambda input_video, work_dir, duration_s: [{
            "source_path": str(input_video),
            "source_start": 0.0,
            "source_end": 1.0,
            "timeline_start": 0.0,
            "timeline_end": 1.0,
        }],
    )
    monkeypatch.setattr("assemble.build_timeline", boom)

    with pytest.raises(RuntimeError, match="timeline schema failure"):
        _emit_timeline(tmp_path / "input.mp4", [{"start": 0.0, "end": 1.0}], tmp_path, 1.0, False)

    assert not (tmp_path / "timeline.json").exists()


def test_assemble_video_without_burn_keeps_video_copy(monkeypatch, tmp_path):
    video = tmp_path / "input.mp4"
    video.write_bytes(b"video")
    output = tmp_path / "output.mp4"
    commands = []

    def fake_run_cmd(cmd):
        commands.append(cmd)
        output.write_bytes(b"mp4")
        return CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setitem(CONFIG, "burn_subtitles", False)
    monkeypatch.setitem(CONFIG, "force_video_reencode", False)
    monkeypatch.setitem(CONFIG, "mask_source_subtitles", False)  # nothing should force a re-encode here
    monkeypatch.setattr("assemble.get_video_duration", lambda path: 4.0)
    monkeypatch.setattr("assemble._build_timed_narration", lambda segments, out, duration, wd: Path(out).write_bytes(b"narration"))
    monkeypatch.setattr("assemble.run_cmd", fake_run_cmd)

    assemble_video(video, [{
        "start": 0.0,
        "end": 3.0,
        "narration": "外挂字幕仍生成。",
        "audio_path": str(tmp_path / "narr.wav"),
        "audio_duration": 1.0,
    }], tmp_path, output)

    ffmpeg_cmd = commands[-1]
    assert (tmp_path / "subtitles.srt").exists()
    assert not (tmp_path / "subtitles.ass").exists()
    assert "-vf" not in ffmpeg_cmd
    assert ffmpeg_cmd[ffmpeg_cmd.index("-c:v") + 1] == "copy"


def test_assemble_video_masks_source_subs_by_default(monkeypatch, tmp_path):
    # mask_source_subtitles defaults to True now: even with burn off, the bottom
    # band is drawn, which forces a video re-encode (no -c:v copy).
    video = tmp_path / "input.mp4"
    video.write_bytes(b"video")
    output = tmp_path / "output.mp4"
    commands = []

    def fake_run_cmd(cmd):
        commands.append(cmd)
        output.write_bytes(b"mp4")
        return CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setitem(CONFIG, "burn_subtitles", False)
    monkeypatch.setitem(CONFIG, "force_video_reencode", False)
    # mask_source_subtitles left at its default (True) on purpose
    monkeypatch.setattr("assemble.get_video_duration", lambda path: 4.0)
    monkeypatch.setattr("assemble._build_timed_narration", lambda segments, out, duration, wd: Path(out).write_bytes(b"narration"))
    monkeypatch.setattr("assemble.run_cmd", fake_run_cmd)

    assemble_video(video, [{
        "start": 0.0,
        "end": 3.0,
        "narration": "默认遮挡原字幕。",
        "audio_path": str(tmp_path / "narr.wav"),
        "audio_duration": 1.0,
    }], tmp_path, output)

    ffmpeg_cmd = commands[-1]
    assert "-vf" in ffmpeg_cmd
    assert any("drawbox=" in str(arg) for arg in ffmpeg_cmd)
    assert ffmpeg_cmd[ffmpeg_cmd.index("-c:v") + 1] == "libx264"


def test_build_audio_filter_complex_gap_fill_envelope(monkeypatch):
    monkeypatch.setitem(CONFIG, "ducking_mode", "fixed")
    monkeypatch.setitem(CONFIG, "idle_orig_volume", 0.85)
    monkeypatch.setitem(CONFIG, "speech_ducking_volume", 0.2)
    monkeypatch.setitem(CONFIG, "zone_ducking_volume", 0.12)
    monkeypatch.setitem(CONFIG, "duck_fade_seconds", 0.25)
    monkeypatch.setitem(CONFIG, "duck_bridge_seconds", 0.5)  # keep the 3s gap unbridged so each beat keeps its own level
    fc = _build_audio_filter_complex([
        {"actual_place_start": 0.0, "actual_place_end": 2.0, "overlaps_speech": True},
        {"actual_place_start": 5.0, "actual_place_end": 7.0, "overlaps_speech": False},
    ])
    assert "volume='max(0,min(1,0.85" in fc          # idle baseline holds the original up in the gaps
    assert "eval=frame" in fc
    assert "min(t-0.00,2.00-t)/0.25" in fc            # faded duck under the dialogue-overlap beat
    assert "min(t-5.00,7.00-t)/0.25" in fc            # faded duck under the quiet-window beat
    assert "(-0.650)" in fc                           # 0.85 -> 0.20 under dialogue
    assert "(-0.730)" in fc                           # 0.85 -> 0.12 in quiet window
    assert fc.endswith("[aout]")


def test_build_audio_filter_complex_all_overlap_still_fills_gaps(monkeypatch):
    # Regression: all-overlap beats used to fall back to a constant duck, leaving the
    # original quiet across the whole video (dead air). A single beat ducks only under its
    # own window; the lead-in/out around it still swells back to idle.
    monkeypatch.setitem(CONFIG, "ducking_mode", "fixed")
    monkeypatch.setitem(CONFIG, "idle_orig_volume", 0.85)
    monkeypatch.setitem(CONFIG, "speech_ducking_volume", 0.2)
    monkeypatch.setitem(CONFIG, "duck_fade_seconds", 0.25)
    fc = _build_audio_filter_complex([
        {"actual_place_start": 0.0, "actual_place_end": 2.0, "overlaps_speech": True},
    ])
    assert "volume='max(0,min(1,0.85" in fc           # NOT a constant; idle baseline present
    assert "min(t-0.00,2.00-t)/0.25" in fc            # ducks only under the narration window
    assert "eval=frame" in fc
    assert fc.endswith("[aout]")


def test_build_audio_filter_complex_bridges_short_gaps(monkeypatch):
    # The fix: beats separated by a gap smaller than duck_bridge_seconds stay ducked across
    # the gap (one held span) so the source dialogue does not pop back up between sentences.
    monkeypatch.setitem(CONFIG, "ducking_mode", "fixed")
    monkeypatch.setitem(CONFIG, "idle_orig_volume", 0.85)
    monkeypatch.setitem(CONFIG, "speech_ducking_volume", 0.2)
    monkeypatch.setitem(CONFIG, "duck_fade_seconds", 0.25)
    monkeypatch.setitem(CONFIG, "duck_bridge_seconds", 6.0)
    fc = _build_audio_filter_complex([
        {"actual_place_start": 0.0, "actual_place_end": 2.0, "overlaps_speech": True},
        {"actual_place_start": 4.0, "actual_place_end": 6.0, "overlaps_speech": True},  # gap 2.0 < 6.0
    ])
    assert "min(t-0.00,6.00-t)/0.25" in fc            # one held duck across both beats + the gap
    assert "min(t-0.00,2.00-t)" not in fc             # NOT two separate dips
    assert "min(t-4.00,6.00-t)" not in fc
    assert fc.count("(-0.650)") == 1                  # a single coalesced duck term


def test_build_audio_filter_complex_releases_long_gaps(monkeypatch):
    # A genuine gap >= duck_bridge_seconds still releases the original to idle so the
    # picture can breathe (e.g. a deliberate long pause between sections).
    monkeypatch.setitem(CONFIG, "ducking_mode", "fixed")
    monkeypatch.setitem(CONFIG, "idle_orig_volume", 0.85)
    monkeypatch.setitem(CONFIG, "speech_ducking_volume", 0.2)
    monkeypatch.setitem(CONFIG, "duck_fade_seconds", 0.25)
    monkeypatch.setitem(CONFIG, "duck_bridge_seconds", 6.0)
    fc = _build_audio_filter_complex([
        {"actual_place_start": 0.0, "actual_place_end": 2.0, "overlaps_speech": True},
        {"actual_place_start": 10.0, "actual_place_end": 12.0, "overlaps_speech": True},  # gap 8.0 >= 6.0
    ])
    assert "min(t-0.00,2.00-t)/0.25" in fc            # two separate dips with idle between
    assert "min(t-10.00,12.00-t)/0.25" in fc
    assert fc.count("(-0.650)") == 2


def test_build_audio_filter_complex_bridged_mixed_levels_flatten_to_min(monkeypatch):
    # A bridged span mixing a speech beat (0.2) and a quiet beat (0.12) flattens to the MIN
    # level across the span — matching variable_ducking_keyframes so the 剪映 draft == the mp4.
    monkeypatch.setitem(CONFIG, "ducking_mode", "fixed")
    monkeypatch.setitem(CONFIG, "idle_orig_volume", 0.85)
    monkeypatch.setitem(CONFIG, "speech_ducking_volume", 0.2)
    monkeypatch.setitem(CONFIG, "zone_ducking_volume", 0.12)
    monkeypatch.setitem(CONFIG, "duck_fade_seconds", 0.25)
    monkeypatch.setitem(CONFIG, "duck_bridge_seconds", 6.0)
    fc = _build_audio_filter_complex([
        {"actual_place_start": 0.0, "actual_place_end": 2.0, "overlaps_speech": True},   # 0.2
        {"actual_place_start": 4.0, "actual_place_end": 6.0, "overlaps_speech": False},  # 0.12, gap 2 < 6
    ])
    assert "min(t-0.00,6.00-t)/0.25" in fc            # one coalesced span across both beats
    assert "(-0.730)" in fc                           # held at the MIN level (0.12)
    assert "(-0.650)" not in fc                       # NOT the speech level (0.2)


def test_build_audio_filter_complex_bgm_envelope_bridges_short_gaps(monkeypatch):
    # The BGM bed bridges the same way: two beats within the bridge coalesce to one BGM dip.
    monkeypatch.setitem(CONFIG, "ducking_mode", "fixed")
    monkeypatch.setitem(CONFIG, "bgm_volume", 0.18)
    monkeypatch.setitem(CONFIG, "bgm_ducking_volume", 0.10)
    monkeypatch.setitem(CONFIG, "duck_fade_seconds", 0.25)
    monkeypatch.setitem(CONFIG, "duck_bridge_seconds", 6.0)
    fc = _build_audio_filter_complex([
        {"actual_place_start": 0.0, "actual_place_end": 2.0, "overlaps_speech": True},
        {"actual_place_start": 4.0, "actual_place_end": 6.0, "overlaps_speech": True},  # gap 2 < 6
    ], has_bgm=True)
    bgm_part = fc.split("[bgm]")[0]                    # the bgm chain comes first
    assert "min(t-0.00,6.00-t)/0.25" in bgm_part      # one coalesced BGM dip across both beats
    assert "min(t-0.00,2.00-t)" not in bgm_part


def test_build_audio_filter_complex_all_quiet_ducks_to_zone(monkeypatch):
    monkeypatch.setitem(CONFIG, "ducking_mode", "fixed")
    monkeypatch.setitem(CONFIG, "idle_orig_volume", 0.85)
    monkeypatch.setitem(CONFIG, "zone_ducking_volume", 0.12)
    monkeypatch.setitem(CONFIG, "duck_fade_seconds", 0.25)
    fc = _build_audio_filter_complex([
        {"actual_place_start": 1.0, "actual_place_end": 4.0, "overlaps_speech": False},
    ])
    assert "min(t-1.00,4.00-t)/0.25" in fc            # smooth fade
    assert "(-0.730)" in fc                           # 0.85 -> 0.12
    assert "eval=frame" in fc


def test_build_audio_filter_complex_bgm_adds_third_track(monkeypatch):
    monkeypatch.setitem(CONFIG, "ducking_mode", "fixed")
    monkeypatch.setitem(CONFIG, "bgm_volume", 0.18)
    monkeypatch.setitem(CONFIG, "bgm_ducking_volume", 0.10)
    monkeypatch.setitem(CONFIG, "duck_fade_seconds", 0.25)
    segs = [{"actual_place_start": 0.0, "actual_place_end": 2.0, "overlaps_speech": True}]
    fc = _build_audio_filter_complex(segs, has_bgm=True)
    assert "[2:a]volume='" in fc                      # BGM bed from input 2, ducked under narration
    assert "[bgm]" in fc
    assert "amix=inputs=3" in fc
    assert fc.endswith("[aout]")
    # default (no BGM) stays a two-track mix with no third input referenced
    fc2 = _build_audio_filter_complex(segs, has_bgm=False)
    assert "amix=inputs=2" in fc2
    assert "[2:a]" not in fc2


def test_build_audio_filter_complex_explicit_modes(monkeypatch):
    segs = [{"actual_place_start": 0.0, "actual_place_end": 2.0, "overlaps_speech": True}]
    monkeypatch.setitem(CONFIG, "ducking_mode", "none")
    none_fc = _build_audio_filter_complex(segs)
    assert "sidechaincompress" not in none_fc
    assert "volume='" not in none_fc  # no envelope in 'none' mode
    monkeypatch.setitem(CONFIG, "ducking_mode", "sidechaincompress")
    assert "sidechaincompress" in _build_audio_filter_complex(segs)


def test_assembly_settings_fingerprint_tracks_burn_style(monkeypatch):
    monkeypatch.setitem(CONFIG, "burn_subtitles", False)
    plain = assembly_settings_fingerprint()
    monkeypatch.setitem(CONFIG, "burn_subtitles", True)
    burned = assembly_settings_fingerprint()
    monkeypatch.setitem(CONFIG, "subtitle_font_size", 50)
    bigger = assembly_settings_fingerprint()

    assert plain["burn_subtitles"] is False
    assert burned["burn_subtitles"] is True
    assert burned["subtitle_renderer"] == "ass"
    assert bigger != burned


def test_final_loudnorm_filter_and_fingerprint(monkeypatch):
    monkeypatch.setitem(CONFIG, "final_loudnorm", True)
    monkeypatch.setitem(CONFIG, "target_lufs", -14.0)
    monkeypatch.setitem(CONFIG, "target_true_peak", -1.0)
    monkeypatch.setitem(CONFIG, "target_lra", 11.0)
    assert final_loudnorm_filter() == "loudnorm=I=-14.0:TP=-1.0:LRA=11.0"
    assert assembly_settings_fingerprint()["audio_mix"]["final_loudnorm"] == "loudnorm=I=-14.0:TP=-1.0:LRA=11.0"

    monkeypatch.setitem(CONFIG, "target_lufs", -11.9)
    assert final_loudnorm_filter() == "loudnorm=I=-11.9:TP=-1.0:LRA=11.0"

    monkeypatch.setitem(CONFIG, "final_loudnorm", False)
    assert final_loudnorm_filter() is None
    assert assembly_settings_fingerprint()["audio_mix"]["final_loudnorm"] == "off"


def test_assembly_settings_fingerprint_tracks_render_affecting_settings(monkeypatch):
    monkeypatch.setitem(CONFIG, "burn_subtitles", False)
    monkeypatch.setitem(CONFIG, "mask_source_subtitles", False)
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_ratio", 0.14)
    monkeypatch.setitem(CONFIG, "narration_speed", 1.0)
    monkeypatch.setitem(CONFIG, "bgm_path", "")
    monkeypatch.setitem(CONFIG, "bgm_volume", 0.18)
    monkeypatch.setitem(CONFIG, "duck_fade_seconds", 0.25)
    base = assembly_settings_fingerprint()

    monkeypatch.setitem(CONFIG, "mask_source_subtitles", True)
    assert assembly_settings_fingerprint() != base
    monkeypatch.setitem(CONFIG, "mask_source_subtitles", False)
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_ratio", 0.20)
    assert assembly_settings_fingerprint() != base
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_ratio", 0.14)
    monkeypatch.setitem(CONFIG, "narration_speed", 1.2)
    assert assembly_settings_fingerprint() != base
    monkeypatch.setitem(CONFIG, "narration_speed", 1.0)
    monkeypatch.setitem(CONFIG, "bgm_path", "/tmp/bgm.mp3")
    assert assembly_settings_fingerprint() != base
    monkeypatch.setitem(CONFIG, "bgm_path", "")
    monkeypatch.setitem(CONFIG, "bgm_volume", 0.30)
    assert assembly_settings_fingerprint() != base
    monkeypatch.setitem(CONFIG, "bgm_volume", 0.18)
    monkeypatch.setitem(CONFIG, "duck_fade_seconds", 0.50)
    assert assembly_settings_fingerprint() != base



def test_assemble_video_uses_silent_original_track_when_source_has_no_audio(monkeypatch, tmp_path):
    video = tmp_path / "silent.mp4"
    video.write_bytes(b"video")
    output = tmp_path / "output.mp4"
    commands = []

    def fake_run_cmd(cmd):
        commands.append(cmd)
        output.write_bytes(b"mp4")
        return CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setitem(CONFIG, "burn_subtitles", False)
    monkeypatch.setitem(CONFIG, "mask_source_subtitles", False)
    monkeypatch.setitem(CONFIG, "final_loudnorm", False)
    monkeypatch.setattr("assemble.get_video_duration", lambda path: 4.0)
    monkeypatch.setattr("assemble._has_audio_stream", lambda path: False)
    monkeypatch.setattr("assemble._build_timed_narration", lambda segments, out, duration, wd: Path(out).write_bytes(b"narration"))
    monkeypatch.setattr("assemble.run_cmd", fake_run_cmd)

    assemble_video(video, [{
        "start": 0.0,
        "end": 3.0,
        "actual_place_start": 0.2,
        "actual_place_end": 2.0,
        "narration": "无原声音轨也应能混音。",
        "audio_path": str(tmp_path / "narr.wav"),
        "audio_duration": 1.0,
    }], tmp_path, output)

    ffmpeg_cmd = commands[-1]
    joined = " ".join(str(part) for part in ffmpeg_cmd)
    assert "anullsrc=channel_layout=stereo:sample_rate=48000" in joined
    assert "[2:a]volume=" in joined
    assert output.exists()


def test_wrap_subtitle_text_balances_and_never_orphans_punctuation():
    # Bug: a 2-line wrap force-broke at max_chars+5 and dropped the trailing "。" alone on line 2.
    out = _wrap_subtitle_text("这婴儿还在襁褓里，脑子里却装着一个现代人将死的记忆。", max_chars=20)
    lines = out.split("\n")
    assert len(lines) == 2
    assert lines[0].endswith("，") and lines[1].endswith("。")   # punctuation stays on its line, no orphan
    assert all(len(c) >= 3 for c in lines)                        # neither line is a lone punctuation mark
    # short text stays on one line
    assert _wrap_subtitle_text("短句。", max_chars=20) == "短句。"
