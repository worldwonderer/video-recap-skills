import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'video-assemble' / 'scripts'))
import json
import pytest  # noqa: F401
from subprocess import CompletedProcess  # noqa: F401
from assemble import _split_subtitle_chunks, _subtitle_entries, _adjust_tts_speed, _apply_narration_speed, _assembly_manifest_payload, _build_audio_filter_complex, _build_timed_narration, _build_video_clips, _emit_timeline, _resolve_final_output, _value_fingerprint, _escape_ass_text, _generate_ass, _generate_srt, _seconds_to_ass_time, _seconds_to_srt_time, _source_subtitle_mask_filter, _subtitle_burn_filter, _output_downscale_filter, assemble_video, assembly_settings_fingerprint, final_loudnorm_filter
from lib import CONFIG
import assemble


def _volume_expr_from_filter(filter_complex):
    marker = "volume='"
    start = filter_complex.index(marker) + len(marker)
    end = filter_complex.index("':eval=frame", start)
    return filter_complex[start:end]


def _eval_duck_expr(filter_complex, t):
    expr = _volume_expr_from_filter(filter_complex)
    return eval(expr, {"__builtins__": {}}, {
        "t": float(t),
        "min": min,
        "max": max,
        "between": lambda value, lo, hi: 1.0 if lo <= value <= hi else 0.0,
    })


def _adjust_result_parts(result):
    assert isinstance(result, tuple), "_adjust_tts_speed should return path, duration, metadata"
    assert len(result) >= 3, "P0 requires machine-readable fit metadata from _adjust_tts_speed"
    return result[0], result[1], result[2]


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

    adjusted, actual_dur, meta = _adjust_result_parts(
        _adjust_tts_speed(src, target_duration=2.0, work_dir=tmp_path)
    )

    assert actual_dur == 2.0
    assert Path(adjusted) == wav_parent / "narr_000_adj.wav"
    assert meta["fit_status"] in {"fit", "tempo_adjusted"}
    assert commands[-1][-1] == str(wav_parent / "narr_000_adj.wav")


def test_adjust_tts_speed_no_safe_fit_keeps_source_audio_and_metadata(monkeypatch, tmp_path):
    wav_parent = tmp_path / "episode.wav-cache"
    wav_parent.mkdir()
    src = wav_parent / "narr_000.wav"
    src.write_bytes(b"wav")
    commands = []

    def fake_run_cmd(cmd, **kw):
        commands.append(cmd)
        Path(cmd[-1]).write_bytes(b"unexpected")
        return CompletedProcess(cmd, 0, "", "")

    monkeypatch.setitem(CONFIG, "narration_speed", 1.2)
    monkeypatch.setitem(CONFIG, "narration_cumulative_tempo_max", 1.35)
    monkeypatch.setattr("assemble.get_video_duration", lambda path: 10.0 if Path(path) == src else 1.0)
    monkeypatch.setattr("assemble.run_cmd", fake_run_cmd)

    adjusted, actual_dur, meta = _adjust_result_parts(
        _adjust_tts_speed(src, target_duration=1.0, work_dir=tmp_path)
    )

    assert actual_dur == 10.0
    assert Path(adjusted) == src
    assert meta["fit_status"] == "no_safe_fit"
    assert meta.get("blocking") is True
    assert commands == []


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
        {"source_id": None, "source_path": str(original), "source_start": 10.0, "source_end": 20.0, "timeline_start": 0.0, "timeline_end": 10.0},
        {"source_id": None, "source_path": str(original), "source_start": 40.0, "source_end": 45.0, "timeline_start": 10.0, "timeline_end": 15.0},
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
        {"source_id": None, "source_path": str(original), "source_start": 40.0, "source_end": 45.0, "timeline_start": 0.0, "timeline_end": 5.0},
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


def test_assemble_main_applies_explicit_measured_subtitle_band(monkeypatch, tmp_path):
    video = tmp_path / "input.mp4"
    video.write_bytes(b"video")
    work = tmp_path / "work"
    work.mkdir()
    (work / "tts_meta.json").write_text('{"segments": []}', encoding="utf-8")
    captured = {}

    def fake_assemble(input_video, tts_segments, work_dir, output_path):
        captured.update({
            "top": CONFIG["subtitle_y_top"],
            "bot": CONFIG["subtitle_y_bot"],
            "mask": CONFIG["mask_source_subtitles"],
            "policy": CONFIG["source_subtitle_mask_policy"],
            "declared": CONFIG["source_subtitle_mask_policy_declared"],
        })
        Path(output_path).write_bytes(b"mp4")
        return output_path

    for key, value in {
        "subtitle_y_top": -1,
        "subtitle_y_bot": -1,
        "mask_source_subtitles": False,
        "source_subtitle_mask_policy": "off",
        "source_subtitle_mask_policy_declared": False,
    }.items():
        monkeypatch.setitem(CONFIG, key, value)
    monkeypatch.setattr(assemble, "_preflight_burn_subtitles", lambda: None)
    monkeypatch.setattr(assemble, "assemble_video", fake_assemble)
    monkeypatch.setattr(sys, "argv", [
        "assemble.py", str(video), "--work-dir", str(work),
        "--subtitle-y-top", "610", "--subtitle-y-bot", "660",
    ])

    assemble.main()

    assert captured == {
        "top": 610,
        "bot": 660,
        "mask": True,
        "policy": "opt_in",
        "declared": True,
    }


def test_resolve_final_output_overwrites_stable_alias(tmp_path):
    """The recap output is always the stable alias recap_<stem>.mp4 (overwritten in
    place), so the iterate-on-narration loop refreshes one file instead of spawning
    fingerprint-suffixed copies of every render."""
    (tmp_path / "recap_clip.mp4").write_bytes(b"previous-render")

    resolved = _resolve_final_output(tmp_path, "clip")

    assert resolved == tmp_path / "recap_clip.mp4"


def test_source_subtitle_mask_filter_toggles_with_effective_burn_policy(monkeypatch):
    monkeypatch.setitem(CONFIG, "burn_subtitles", False)
    monkeypatch.setitem(CONFIG, "mask_source_subtitles", False)
    assert _source_subtitle_mask_filter() is None

    # no-burn means sidecar-subtitle mode: ambient/default source masking must not
    # create a black band without burned recap subtitles.
    monkeypatch.setitem(CONFIG, "mask_source_subtitles", True)
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_policy", "opt_in")
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_ratio", 0.15)
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_timing", "all")
    assert _source_subtitle_mask_filter() is None

    monkeypatch.setitem(CONFIG, "burn_subtitles", True)
    f = _source_subtitle_mask_filter()
    assert f is not None and f.startswith("drawbox=") and "t=fill" in f

    monkeypatch.setitem(CONFIG, "source_subtitle_mask_ratio", 0.0)  # ratio 0 still allowed if style band requires it
    assert _source_subtitle_mask_filter() is not None


def test_source_subtitle_mask_can_restore_opaque_full_timeline_mode(monkeypatch):
    """The enhanced default stays reversible for projects that need the old mask look."""
    monkeypatch.setitem(CONFIG, "burn_subtitles", True)
    monkeypatch.setitem(CONFIG, "mask_source_subtitles", True)
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_policy", "opt_in")
    monkeypatch.setitem(CONFIG, "subtitle_mask_opacity", 1.0)
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_timing", "all")

    filt = _source_subtitle_mask_filter(
        {"width": 1280, "height": 720},
        tts_segments=[{"actual_place_start": 1.0, "actual_place_end": 2.0}],
    )

    assert "color=black@1.00" in filt
    assert "enable=" not in filt


def test_source_subtitle_mask_can_follow_custom_band_and_narration_windows(monkeypatch):
    monkeypatch.setitem(CONFIG, "burn_subtitles", True)
    monkeypatch.setitem(CONFIG, "mask_source_subtitles", True)
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_policy", "opt_in")
    monkeypatch.setitem(CONFIG, "subtitle_y_top", 610)
    monkeypatch.setitem(CONFIG, "subtitle_y_bot", 660)
    monkeypatch.setitem(CONFIG, "subtitle_mask_padding", 4)
    monkeypatch.setitem(CONFIG, "subtitle_mask_opacity", 0.6)
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_timing", "narration")

    filt = _source_subtitle_mask_filter(
        {"width": 1280, "height": 720},
        tts_segments=[
            {"actual_place_start": 1.25, "actual_place_end": 2.5},
            {"actual_place_start": 4.0, "actual_place_end": 4.0},
            {"start": 5.0, "end": 6.0},
        ],
    )

    assert filt.count("drawbox=") == 2
    assert "y=606:w=iw:h=58" in filt
    assert filt.count("color=black@0.60") == 2
    assert "between(t,1.250,2.500)" in filt
    assert "between(t,5.000,6.000)" in filt


@pytest.mark.parametrize("configured_opacity", [0.0, 0.6])
def test_source_subtitle_mask_opaquely_covers_byo_gap_subtitles(
    monkeypatch, tmp_path, configured_opacity
):
    monkeypatch.setitem(CONFIG, "burn_subtitles", True)
    monkeypatch.setitem(CONFIG, "mask_source_subtitles", True)
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_policy", "opt_in")
    monkeypatch.setitem(CONFIG, "subtitle_y_top", 610)
    monkeypatch.setitem(CONFIG, "subtitle_y_bot", 660)
    monkeypatch.setitem(CONFIG, "subtitle_mask_opacity", configured_opacity)
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_timing", "narration")
    (tmp_path / "user_subtitles.json").write_text(
        json.dumps([{"start": 1.0, "end": 4.0, "text": "用户校订原声"}]),
        encoding="utf-8",
    )

    filt = _source_subtitle_mask_filter(
        {"width": 1280, "height": 720},
        tmp_path,
        [{"actual_place_start": 5.0, "actual_place_end": 8.0, "narration": "解说"}],
        video_duration=10.0,
    )

    if configured_opacity > 0:
        assert "color=black@0.60" in filt
        assert "between(t,5.000,8.000)" in filt
    assert "color=black@1.00" in filt
    assert "between(t,1.000,4.000)" in filt


def test_source_subtitle_mask_coalesces_overlaps_to_avoid_double_opacity(monkeypatch):
    monkeypatch.setitem(CONFIG, "burn_subtitles", True)
    monkeypatch.setitem(CONFIG, "mask_source_subtitles", True)
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_policy", "opt_in")
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_timing", "narration")

    filt = _source_subtitle_mask_filter(
        {"width": 1280, "height": 720},
        tts_segments=[
            {"actual_place_start": 1.0, "actual_place_end": 2.0},
            {"actual_place_start": 1.5, "actual_place_end": 3.0},
        ],
    )

    assert filt.count("drawbox=") == 1
    assert "between(t,1.000,3.000)" in filt


def test_narration_timed_source_mask_without_narration_draws_nothing(monkeypatch):
    monkeypatch.setitem(CONFIG, "burn_subtitles", True)
    monkeypatch.setitem(CONFIG, "mask_source_subtitles", True)
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_policy", "opt_in")
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_timing", "narration")

    assert _source_subtitle_mask_filter(
        {"width": 1280, "height": 720}, tts_segments=[]
    ) is None


def test_generate_ass_places_subtitle_bottom_on_measured_y(monkeypatch, tmp_path):
    monkeypatch.setitem(CONFIG, "subtitle_y_top", 610)
    monkeypatch.setitem(CONFIG, "subtitle_y_bot", 650)

    ass = _generate_ass(
        [{"start": 0.0, "end": 1.0, "narration": "贴合原字幕"}],
        tmp_path,
        canvas={"width": 1280, "height": 720},
    ).read_text(encoding="utf-8")

    style_line = next(line for line in ass.splitlines() if line.startswith("Style: Default,"))
    assert style_line.split(",")[-2] == "70"  # 720 - measured y_bot 650
    assert style_line.split(",")[2] == "31"  # line box + outline + shadow fit above anchored bot


def test_generate_ass_rejects_measured_band_outside_canvas(monkeypatch, tmp_path):
    monkeypatch.setitem(CONFIG, "subtitle_y_top", 700)
    monkeypatch.setitem(CONFIG, "subtitle_y_bot", 760)

    with pytest.raises(ValueError, match="字幕带坐标无效"):
        _generate_ass(
            [{"start": 0.0, "end": 1.0, "narration": "越界"}],
            tmp_path,
            canvas={"width": 1280, "height": 720},
        )


def test_generate_ass_rejects_non_bottom_alignment_for_measured_band(monkeypatch, tmp_path):
    monkeypatch.setitem(CONFIG, "subtitle_y_top", 610)
    monkeypatch.setitem(CONFIG, "subtitle_y_bot", 650)
    monkeypatch.setitem(CONFIG, "subtitle_alignment", 8)

    with pytest.raises(ValueError, match="bottom-aligned"):
        _generate_ass(
            [{"start": 0.0, "end": 1.0, "narration": "不能贴合"}],
            tmp_path,
            canvas={"width": 1280, "height": 720},
        )


def test_measured_band_rejects_non_square_pixel_canvas_from_env_route(monkeypatch, tmp_path):
    monkeypatch.setitem(CONFIG, "subtitle_y_top", 300)
    monkeypatch.setitem(CONFIG, "subtitle_y_bot", 340)
    canvas = {
        "width": 720,
        "height": 1280,
        "sample_aspect_ratio": "2:1",
    }

    with pytest.raises(ValueError, match="SAR 1:1"):
        _generate_ass(
            [{"start": 0.0, "end": 1.0, "narration": "非方形像素"}],
            tmp_path,
            canvas=canvas,
        )


def test_mask_band_stays_one_line_small_when_burning(monkeypatch):
    """Subtitles are split into short ONE-LINE chunks, so the burned-in band must size for a single
    line + margin (small), NOT two lines. A 2-line band ate ~23% of the height and compressed the
    picture; a one-line band keeps it near the raw mask ratio."""
    monkeypatch.setitem(CONFIG, "mask_source_subtitles", True)
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_policy", "opt_in")
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_ratio", 0.14)
    monkeypatch.setitem(CONFIG, "subtitle_font_size", 42)
    monkeypatch.setitem(CONFIG, "subtitle_margin_v", 30)
    monkeypatch.setitem(CONFIG, "subtitle_play_res_y", 720)
    monkeypatch.setitem(CONFIG, "burn_subtitles", True)
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_timing", "all")
    import re
    ratio_on = float(re.search(r"ih-ih\*([0-9.]+)", _source_subtitle_mask_filter()).group(1))
    one_line = (30 + 42 * 1.25 + 10) / 720
    assert ratio_on == pytest.approx(max(0.14, one_line), abs=0.005), ratio_on
    assert ratio_on < 0.16, ratio_on            # stays small — never the old ~0.23 two-line band
    # not burning -> no mask-only black band
    monkeypatch.setitem(CONFIG, "burn_subtitles", False)
    assert _source_subtitle_mask_filter() is None


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


def test_generate_srt_strips_terminal_display_punctuation_without_mutating_source(tmp_path):
    narration = [
        {"start": 0.0, "end": 2.0, "actual_place_start": 0.5, "actual_place_end": 1.7, "narration": "他终于明白真相。"},
        {"start": 2.0, "end": 4.0, "actual_place_start": 2.2, "actual_place_end": 3.5, "narration": "What now?"},
        {"start": 4.0, "end": 6.0, "actual_place_start": 4.2, "actual_place_end": 5.5, "narration": "It ends."},
    ]

    _generate_srt(narration, tmp_path)

    srt = (tmp_path / "subtitles.srt").read_text(encoding="utf-8")
    assert "他终于明白真相\n" in srt
    assert "他终于明白真相。" not in srt
    assert "What now\n" in srt
    assert "What now?" not in srt
    assert "It ends\n" in srt
    assert "It ends." not in srt
    assert narration[0]["narration"].endswith("。")  # display-only; TTS source is untouched


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


def test_generate_ass_strips_terminal_display_punctuation_before_escaping(tmp_path):
    _generate_ass([
        {
            "start": 1.0,
            "end": 4.0,
            "actual_place_start": 1.25,
            "actual_place_end": 3.5,
            "narration": "他终于明白真相。",
        }
    ], tmp_path)

    ass = (tmp_path / "subtitles.ass").read_text(encoding="utf-8")
    assert "他终于明白真相" in ass
    assert "他终于明白真相。" not in ass


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


def test_assemble_video_uses_filter_script_for_long_timed_mask(monkeypatch, tmp_path):
    """Dense long-form narration must not place a >32K video graph on Windows' command line."""
    video = tmp_path / "input.mp4"
    video.write_bytes(b"video")
    output = tmp_path / "output.mp4"
    commands = []
    video_filter_scripts = []

    def fake_run_cmd(cmd):
        commands.append(cmd)
        if "-filter_script:v:0" in cmd:
            script = Path(cmd[cmd.index("-filter_script:v:0") + 1])
            video_filter_scripts.append((script, script.read_text(encoding="utf-8")))
        output.write_bytes(b"mp4")
        return CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setitem(CONFIG, "burn_subtitles", True)
    monkeypatch.setitem(CONFIG, "mask_source_subtitles", True)
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_policy", "opt_in")
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_policy_declared", True)
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_timing", "narration")
    monkeypatch.setitem(CONFIG, "subtitle_mask_opacity", 0.6)
    monkeypatch.setitem(CONFIG, "subtitle_y_top", 610)
    monkeypatch.setitem(CONFIG, "subtitle_y_bot", 660)
    monkeypatch.setattr("assemble.get_video_duration", lambda path: 3800.0)
    monkeypatch.setattr("assemble._apply_narration_speed", lambda segments, work_dir: None)
    monkeypatch.setattr(
        "assemble._build_timed_narration",
        lambda segments, out, duration, wd: Path(out).write_bytes(b"narration"),
    )
    monkeypatch.setattr("assemble.run_cmd", fake_run_cmd)

    segments = [{
        "index": i,
        "start": i * 10.0,
        "end": i * 10.0 + 6.0,
        "actual_place_start": i * 10.0,
        "actual_place_end": i * 10.0 + 6.0,
        "narration": f"第{i}段",
        "audio_path": str(tmp_path / "narr.wav"),
        "audio_duration": 1.0,
    } for i in range(375)]

    assemble_video(video, segments, tmp_path, output)

    ffmpeg_cmd = commands[-1]
    assert "-filter_script:v:0" in ffmpeg_cmd
    assert "-vf" not in ffmpeg_cmd
    assert video_filter_scripts[0][1].count("drawbox=") == 375
    assert len(" ".join(map(str, ffmpeg_cmd))) < 32767
    assert not video_filter_scripts[0][0].exists()


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


def test_assemble_video_no_burn_ignores_source_mask_default(monkeypatch, tmp_path):
    # no-burn sidecar mode must not draw a mask-only black band, even if the
    # ambient/default mask_source_subtitles setting is true.
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
        "narration": "外挂字幕不遮黑条。",
        "audio_path": str(tmp_path / "narr.wav"),
        "audio_duration": 1.0,
    }], tmp_path, output)

    ffmpeg_cmd = commands[-1]
    assert (tmp_path / "subtitles.srt").exists()
    assert "-vf" not in ffmpeg_cmd
    assert not any("drawbox=" in str(arg) for arg in ffmpeg_cmd)
    assert ffmpeg_cmd[ffmpeg_cmd.index("-c:v") + 1] == "copy"


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
    assert _eval_duck_expr(fc, 0.00) == pytest.approx(0.2)   # already ducked at spoken start
    assert _eval_duck_expr(fc, 2.00) == pytest.approx(0.2)   # holds through spoken end
    assert _eval_duck_expr(fc, 5.00) == pytest.approx(0.12)
    assert _eval_duck_expr(fc, 7.00) == pytest.approx(0.12)
    assert _eval_duck_expr(fc, 3.50) == pytest.approx(0.85)  # true gap releases to idle
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
    assert _eval_duck_expr(fc, 0.00) == pytest.approx(0.2)
    assert _eval_duck_expr(fc, 2.00) == pytest.approx(0.2)
    assert _eval_duck_expr(fc, 2.25) == pytest.approx(0.85)
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
    assert _eval_duck_expr(fc, 0.0) == pytest.approx(0.2)
    assert _eval_duck_expr(fc, 3.0) == pytest.approx(0.2)    # one held duck across both beats + gap
    assert _eval_duck_expr(fc, 6.0) == pytest.approx(0.2)
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
    assert _eval_duck_expr(fc, 0.0) == pytest.approx(0.2)
    assert _eval_duck_expr(fc, 2.0) == pytest.approx(0.2)
    assert _eval_duck_expr(fc, 6.0) == pytest.approx(0.85)   # two separate dips with idle between
    assert _eval_duck_expr(fc, 10.0) == pytest.approx(0.2)
    assert _eval_duck_expr(fc, 12.0) == pytest.approx(0.2)
    assert fc.count("(-0.650)") == 2


def test_build_audio_filter_complex_original_blocks_play_full_volume(monkeypatch):
    # New default model: narration comes in BLOCKS that duck the original, and the deliberate
    # stretches BETWEEN blocks are "original blocks" that play at FULL volume (idle=1.0). A short
    # bridge (1.5s) keeps within-block micro-gaps ducked but lets the between-block gap swell to 1.0.
    monkeypatch.setitem(CONFIG, "ducking_mode", "fixed")
    monkeypatch.setitem(CONFIG, "idle_orig_volume", 1.0)
    monkeypatch.setitem(CONFIG, "speech_ducking_volume", 0.2)
    monkeypatch.setitem(CONFIG, "duck_fade_seconds", 0.3)
    monkeypatch.setitem(CONFIG, "duck_bridge_seconds", 1.5)
    fc = _build_audio_filter_complex([
        {"actual_place_start": 0.0, "actual_place_end": 8.0, "overlaps_speech": True},    # narration block
        {"actual_place_start": 14.0, "actual_place_end": 22.0, "overlaps_speech": True},  # next block; 6s gap = original block
    ])
    assert "volume='max(0,min(1,1.0" in fc            # full-volume original baseline (was 0.85)
    assert fc.count("(-0.800)") == 2                  # 1.0 -> 0.2 under each block, two separate dips
    assert _eval_duck_expr(fc, 0.0) == pytest.approx(0.2)
    assert _eval_duck_expr(fc, 8.0) == pytest.approx(0.2)
    assert _eval_duck_expr(fc, 11.0) == pytest.approx(1.0)   # the 6s gap stays full between
    assert _eval_duck_expr(fc, 14.0) == pytest.approx(0.2)
    assert _eval_duck_expr(fc, 22.0) == pytest.approx(0.2)


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
    assert _eval_duck_expr(fc, 0.0) == pytest.approx(0.12)
    assert _eval_duck_expr(fc, 3.0) == pytest.approx(0.12)   # one coalesced span across both beats
    assert _eval_duck_expr(fc, 6.0) == pytest.approx(0.12)
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
    assert "(-0.080)" in bgm_part                      # 0.18 -> 0.10 in one coalesced BGM dip
    assert "(-0.080)" in bgm_part and bgm_part.count("(-0.080)") == 1


def test_build_audio_filter_complex_all_quiet_ducks_to_zone(monkeypatch):
    monkeypatch.setitem(CONFIG, "ducking_mode", "fixed")
    monkeypatch.setitem(CONFIG, "idle_orig_volume", 0.85)
    monkeypatch.setitem(CONFIG, "zone_ducking_volume", 0.12)
    monkeypatch.setitem(CONFIG, "duck_fade_seconds", 0.25)
    fc = _build_audio_filter_complex([
        {"actual_place_start": 1.0, "actual_place_end": 4.0, "overlaps_speech": False},
    ])
    assert _eval_duck_expr(fc, 1.0) == pytest.approx(0.12)
    assert _eval_duck_expr(fc, 4.0) == pytest.approx(0.12)
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
    filt = final_loudnorm_filter()
    assert "loudnorm=I=-14.0:TP=-1.0:LRA=11.0" in filt
    assert "linear=true" in filt
    assert "alimiter=limit=0.98:level=false" in filt
    assert assembly_settings_fingerprint()["audio_mix"]["final_loudnorm"] == filt
    assert assembly_settings_fingerprint()["audio_mix"]["loudness_mode"] in {"two_pass_linear", "equivalent"}

    monkeypatch.setitem(CONFIG, "target_lufs", -11.9)
    assert "loudnorm=I=-11.9:TP=-1.0:LRA=11.0" in final_loudnorm_filter()

    monkeypatch.setitem(CONFIG, "final_loudnorm", False)
    assert final_loudnorm_filter() == "alimiter=limit=0.98:level=false"
    assert assembly_settings_fingerprint()["audio_mix"]["final_loudnorm"] == "alimiter=limit=0.98:level=false"
    assert assembly_settings_fingerprint()["audio_mix"]["loudness_mode"] == "limiter_only"


def test_assembly_settings_fingerprint_tracks_render_affecting_settings(monkeypatch):
    monkeypatch.setitem(CONFIG, "burn_subtitles", False)
    monkeypatch.setitem(CONFIG, "mask_source_subtitles", False)
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_ratio", 0.14)
    monkeypatch.setitem(CONFIG, "narration_speed", 1.0)
    monkeypatch.setitem(CONFIG, "bgm_path", "")
    monkeypatch.setitem(CONFIG, "bgm_volume", 0.18)
    monkeypatch.setitem(CONFIG, "duck_fade_seconds", 0.25)
    monkeypatch.setitem(CONFIG, "output_crf", 18)
    monkeypatch.setitem(CONFIG, "output_preset", "veryfast")
    monkeypatch.setitem(CONFIG, "output_max_height", 0)
    base = assembly_settings_fingerprint()

    monkeypatch.setitem(CONFIG, "mask_source_subtitles", True)
    legacy_implicit = assembly_settings_fingerprint()
    assert legacy_implicit != base
    assert legacy_implicit["video_filters"]["source_subtitle_mask_policy"] == "legacy_implicit"
    assert legacy_implicit["video_filters"]["source_subtitle_mask_policy_declared"] is False
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_ratio", 0.20)
    assert assembly_settings_fingerprint() == legacy_implicit
    monkeypatch.setitem(CONFIG, "burn_subtitles", True)
    assert assembly_settings_fingerprint() != base
    monkeypatch.setitem(CONFIG, "burn_subtitles", False)
    monkeypatch.setitem(CONFIG, "mask_source_subtitles", False)
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
    monkeypatch.setitem(CONFIG, "duck_fade_seconds", 0.25)
    monkeypatch.setitem(CONFIG, "output_crf", 24)
    assert assembly_settings_fingerprint() != base
    monkeypatch.setitem(CONFIG, "output_crf", 18)
    monkeypatch.setitem(CONFIG, "output_preset", "slow")
    assert assembly_settings_fingerprint() != base
    monkeypatch.setitem(CONFIG, "output_preset", "veryfast")
    monkeypatch.setitem(CONFIG, "output_max_height", 720)
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


def test_split_subtitle_chunks_breaks_block_into_short_one_line_pieces():
    block = "这婴儿还在襁褓里，脑子里却装着一个现代人将死的记忆。他叫范闲，注定要搅动这座庙堂。"
    chunks = _split_subtitle_chunks(block, max_chars=20)
    assert len(chunks) >= 2                                   # a long block is split, not shown whole
    assert all(len(c) <= 20 for c in chunks)                 # every piece fits one line
    assert "".join(chunks) == block.replace(" ", "")          # no text lost
    assert all(c == c.strip() and c for c in chunks)          # no empty / whitespace-only chunk
    # a short block stays a single chunk
    assert _split_subtitle_chunks("他叫范闲。", max_chars=20) == ["他叫范闲。"]
    assert _split_subtitle_chunks("   ", max_chars=20) == []


def test_subtitle_entries_keep_timing_topology_when_stripping_display_punctuation():
    entries = _subtitle_entries([
        {
            "start": 0.0,
            "end": 10.0,
            "actual_place_start": 0.0,
            "actual_place_end": 10.0,
            "narration": "短。长长长长。",
        }
    ])

    assert [e["text"] for e in entries] == ["短", "长长长长"]
    # Raw chunks are "短。" and "长长长长。" (2:5), so display cleanup must not
    # retime the cue as stripped display text (1:4).
    assert entries[0]["end"] == pytest.approx(10.0 * 2 / 7)


def test_subtitle_entries_keep_closing_quote_with_stripped_terminal_punctuation():
    entries = _subtitle_entries([
        {
            "start": 0.0,
            "end": 2.0,
            "actual_place_start": 0.0,
            "actual_place_end": 2.0,
            "narration": "他说：「你好。」",
        }
    ])

    assert [e["text"] for e in entries] == ["他说：「你好」"]
    assert all(e["text"] != "」" for e in entries)


def test_subtitle_entries_distribute_block_window_across_chunks():
    # one placed block of 12s -> several timed one-line entries spanning [start, end] with no gaps
    entries = _subtitle_entries([
        {
            "start": 0.0, "end": 12.0,
            "actual_place_start": 2.0, "actual_place_end": 14.0,
            "narration": "这婴儿还在襁褓里，脑子里却装着一个现代人将死的记忆。他叫范闲，注定要搅动这座庙堂。",
        }
    ])
    assert len(entries) >= 2
    assert entries[0]["start"] == pytest.approx(2.0)         # first chunk starts at the audio start
    assert entries[-1]["end"] == pytest.approx(14.0)         # last chunk ends at the audio end
    for a, b in zip(entries, entries[1:]):
        assert b["start"] == pytest.approx(a["end"])         # contiguous, karaoke-style
        assert a["end"] > a["start"]
    assert all(len(e["text"]) <= 20 for e in entries)        # each line is short
    assert all(not e["text"].endswith(("。", "！", "？", "!", "?", "…")) for e in entries)


def test_subtitle_entries_never_drops_a_sub_threshold_chunk():
    # A trailing chunk whose proportional slice is < 0.05s must be folded into the previous line,
    # not silently dropped — otherwise its text would vanish from the burned subtitles.
    entries = _subtitle_entries([
        {
            "start": 0.0, "end": 0.3,
            "actual_place_start": 0.0, "actual_place_end": 0.3,
            "narration": "第一句子比较长一点点。第二句子也比较长。第三。",
        }
    ])
    joined = "".join(e["text"] for e in entries)
    assert "第三" in joined                                   # the tiny trailing chunk survives
    assert entries[-1]["end"] == pytest.approx(0.3)          # still closes at the audio end
    assert joined == "第一句子比较长一点点第二句子也比较长第三"


def test_output_compression_knobs_default_and_override(monkeypatch):
    """OUTPUT_CRF / OUTPUT_PRESET / OUTPUT_MAX_HEIGHT drive the re-encode; defaults keep the prior
    visually-lossless behaviour (crf 18, veryfast, no scaling)."""
    import importlib
    import lib as _lib
    try:
        for var in ("OUTPUT_CRF", "OUTPUT_PRESET", "OUTPUT_MAX_HEIGHT"):
            monkeypatch.delenv(var, raising=False)
        importlib.reload(_lib)
        assert _lib.CONFIG["output_crf"] == 18
        assert _lib.CONFIG["output_preset"] == "veryfast"
        assert _lib.CONFIG["output_max_height"] == 0          # 0 → no downscale

        monkeypatch.setenv("OUTPUT_CRF", "24")
        monkeypatch.setenv("OUTPUT_PRESET", "slow")
        monkeypatch.setenv("OUTPUT_MAX_HEIGHT", "720")
        importlib.reload(_lib)
        assert _lib.CONFIG["output_crf"] == 24
        assert _lib.CONFIG["output_preset"] == "slow"
        assert _lib.CONFIG["output_max_height"] == 720

        monkeypatch.setenv("OUTPUT_CRF", "0")          # lossless is a valid CRF, must not be coerced away
        importlib.reload(_lib)
        assert _lib.CONFIG["output_crf"] == 0
    finally:
        for var in ("OUTPUT_CRF", "OUTPUT_PRESET", "OUTPUT_MAX_HEIGHT"):
            monkeypatch.delenv(var, raising=False)
        importlib.reload(_lib)


def test_output_crf_zero_is_not_overridden_to_default():
    """CRF 0 (x264 lossless) is falsy but valid; the mux must pass '0', never silently fall back to 18."""
    import importlib
    import lib as _lib
    import assemble
    try:
        _lib.CONFIG["output_crf"] = 0
        importlib.reload(assemble)  # assemble reads CONFIG from the reloaded lib at call time
        assert str(assemble.CONFIG.get("output_crf", 18)) == "0"
    finally:
        importlib.reload(_lib)
        importlib.reload(assemble)


def test_output_downscale_filter_forces_even_height():
    """An odd OUTPUT_MAX_HEIGHT must still yield an even output height (libx264/yuv420p reject odd),
    and the filter must never regress to the width-only `-2:'min(ih,H)'` form that crashed the mux."""
    import math
    # Regression guard: the exact even-forcing filter string is pinned.
    assert _output_downscale_filter(721) == "scale=-2:'2*trunc(min(ih,721)/2)':flags=lanczos"
    assert _output_downscale_filter(720) == "scale=-2:'2*trunc(min(ih,720)/2)':flags=lanczos"
    # The embedded expression is even for any (source height, cap) pair, and only ever shrinks.
    for ih in (720, 1080, 2160, 723):
        for max_h in (480, 720, 721, 1080, 1081):
            height = 2 * math.trunc(min(ih, max_h) / 2)   # mirrors the ffmpeg expression
            assert height % 2 == 0
            assert height <= max_h and height <= ih


def test_foreign_source_audio_near_mutes_original_under_narration(monkeypatch):
    """FOREIGN_SOURCE_AUDIO near-mutes the original UNDER narration so a foreign-language
    soundtrack (e.g. Japanese) doesn't bleed under Chinese narration as 怪音. Gaps stay full
    (idle_orig_volume), and an explicit SPEECH_DUCKING_VOLUME still overrides the foreign default."""
    import importlib
    import lib as _lib
    try:
        monkeypatch.setenv("FOREIGN_SOURCE_AUDIO", "1")
        monkeypatch.delenv("SPEECH_DUCKING_VOLUME", raising=False)
        monkeypatch.delenv("ZONE_DUCKING_VOLUME", raising=False)
        importlib.reload(_lib)
        assert _lib.CONFIG["foreign_source_audio"] is True
        assert _lib.CONFIG["speech_ducking_volume"] == 0.05   # under-narration original near-silent
        assert _lib.CONFIG["zone_ducking_volume"] == 0.05
        assert _lib.CONFIG["idle_orig_volume"] == 1.0          # gap/original blocks stay full volume

        monkeypatch.setenv("SPEECH_DUCKING_VOLUME", "0.15")
        importlib.reload(_lib)
        assert _lib.CONFIG["speech_ducking_volume"] == 0.15    # explicit override wins over foreign default
    finally:
        monkeypatch.delenv("FOREIGN_SOURCE_AUDIO", raising=False)
        monkeypatch.delenv("SPEECH_DUCKING_VOLUME", raising=False)
        monkeypatch.delenv("ZONE_DUCKING_VOLUME", raising=False)
        importlib.reload(_lib)  # restore default CONFIG for any later tests


def test_p0_adjust_tts_speed_respects_cumulative_tempo_cap(monkeypatch, tmp_path):
    """Segment atempo must be budgeted against global narration_speed and TTS rate offset."""
    src = tmp_path / "narr_000.wav"
    src.write_bytes(b"wav")
    commands = []

    def fake_run_cmd(cmd, **kw):
        commands.append(cmd)
        Path(cmd[-1]).write_bytes(b"adjusted")
        return CompletedProcess(cmd, 0, "", "")

    monkeypatch.setitem(CONFIG, "narration_speed", 1.2)
    monkeypatch.setitem(CONFIG, "narration_cumulative_tempo_max", 1.35)
    monkeypatch.setattr("assemble.get_video_duration", lambda path: 11.2 if Path(path) == src else 10.0)
    monkeypatch.setattr("assemble.run_cmd", fake_run_cmd)

    out, dur, meta = _adjust_result_parts(
        _adjust_tts_speed(src, target_duration=10.0, work_dir=tmp_path, tts_rate_offset=0.05)
    )

    effective_tempo = float(meta["global_narration_speed"]) * (1.0 + float(meta["tts_rate_offset"])) * float(meta["segment_tempo_factor"])
    assert effective_tempo <= 1.35 + 1e-6
    assert meta["fit_status"] in {"fit", "tempo_adjusted", "no_safe_fit"}
    if commands:
        afilter = " ".join(commands[-1])
        assert "atempo=1.120" not in afilter, "raw overrun ratio must not be used after global/rate tempo budget"


def test_p0_adjust_tts_speed_no_safe_fit_does_not_time_cut(monkeypatch, tmp_path):
    """When cumulative cap cannot fit a segment, assemble records blocking metadata, not time-only cuts."""
    src = tmp_path / "narr_000.wav"
    src.write_bytes(b"wav")
    commands = []

    def fake_run_cmd(cmd, **kw):
        commands.append(cmd)
        Path(cmd[-1]).write_bytes(b"unexpected")
        return CompletedProcess(cmd, 0, "", "")

    monkeypatch.setitem(CONFIG, "narration_speed", 1.2)
    monkeypatch.setitem(CONFIG, "narration_cumulative_tempo_max", 1.35)
    monkeypatch.setattr("assemble.get_video_duration", lambda path: 15.0 if Path(path) == src else 10.0)
    monkeypatch.setattr("assemble.run_cmd", fake_run_cmd)

    out, dur, meta = _adjust_result_parts(
        _adjust_tts_speed(src, target_duration=10.0, work_dir=tmp_path, tts_rate_offset=0.05)
    )

    assert Path(out) == src
    assert dur == 15.0
    assert meta["fit_status"] == "no_safe_fit"
    assert meta["truncate_reason"] in {"no_safe_boundary", "no_room"}
    assert meta.get("blocking") is True
    assert not any("-t" in cmd for cmd in commands), "P0 forbids assemble-side time-only speech cuts"
    assert not any(str(cmd[-1]).endswith("_cut.wav") for cmd in commands)


def test_p0_build_timed_narration_propagates_no_safe_fit_metadata(monkeypatch, tmp_path):
    """_build_timed_narration must preserve audio/text truth and expose no-safe-fit for QC."""
    wav = tmp_path / "long.wav"
    import wave
    with wave.open(str(wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(44100)
        wf.writeframes(b"\x00\x10" * int(2.0 * 44100))

    def fake_adjust(path, target_duration, work_dir, tts_rate_offset=0.0):
        return str(path), 2.0, {
            "fit_status": "no_safe_fit",
            "truncate_reason": "no_safe_boundary",
            "blocking": True,
            "segment_tempo_factor": 1.0,
            "effective_tempo": 1.2,
        }

    monkeypatch.setitem(CONFIG, "narration_delay_seconds", 0.0)
    monkeypatch.setitem(CONFIG, "narration_tail_pad_seconds", 0.0)
    monkeypatch.setitem(CONFIG, "narration_tighten", False)
    monkeypatch.setattr("assemble._adjust_tts_speed", fake_adjust)
    seg = {
        "index": 0,
        "start": 0.0,
        "end": 1.0,
        "narration": "原始长文案，不能猜测截断。",
        "spoken_text": "已合成但仍太长的文案。",
        "audio_path": str(wav),
        "audio_duration": 2.0,
        "tts_rate_offset": 0.0,
    }

    _build_timed_narration([seg], tmp_path / "narration.wav", 1.5, tmp_path)

    assert seg["fit_status"] == "no_safe_fit"
    assert seg["truncate_reason"] == "no_safe_boundary"
    assert seg["blocking"] is True
    assert seg["spoken_text"] == "已合成但仍太长的文案。"
    assert seg["narration"] == "原始长文案，不能猜测截断。"


def test_p0_build_timed_narration_trims_subframe_overrun_instead_of_dropping(monkeypatch, tmp_path):
    """Regression: a rounding-level overrun (<=50ms of post-atempo tail) must be trimmed and
    PLACED, not dropped as no_safe_fit — which would lose the whole narration block and trip
    final QC. Previously any 0.00-0.01s overrun discarded the block."""
    import wave
    wav = tmp_path / "orig.wav"
    with wave.open(str(wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(44100)
        wf.writeframes(b"\x00\x10" * int(2.1 * 44100))  # longer than the 2.0s slot -> triggers fit

    def fake_adjust(path, target_duration, work_dir, tts_rate_offset=0.0, *, return_meta=True):
        # simulate atempo landing ~10ms over the fit target (real ffmpeg rounding drift)
        over = tmp_path / "over.wav"
        n = int((target_duration + 0.010) * 44100)
        with wave.open(str(over), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(b"\x00\x10" * n)
        return str(over), target_duration + 0.010, {
            "fit_status": "tempo_adjusted", "truncate_reason": "none", "blocking": False,
            "segment_tempo_factor": 1.0, "effective_tempo": 1.2, "global_narration_speed": 1.15,
        }

    monkeypatch.setitem(CONFIG, "narration_delay_seconds", 0.0)
    monkeypatch.setitem(CONFIG, "narration_tail_pad_seconds", 0.0)
    monkeypatch.setitem(CONFIG, "narration_tighten", False)
    monkeypatch.setattr("assemble._adjust_tts_speed", fake_adjust)
    seg = {
        "index": 0,
        "start": 0.0,
        "end": 2.0,
        "narration": "一段刚好超出零点几帧的解说。",
        "spoken_text": "一段刚好超出零点几帧的解说。",
        "audio_path": str(wav),
        "audio_duration": 2.1,
        "tts_rate_offset": 0.0,
    }

    _build_timed_narration([seg], tmp_path / "narration.wav", 2.0, tmp_path)

    assert seg["fit_status"] != "no_safe_fit"
    assert seg.get("blocking") is not True
    assert seg["truncate_reason"] == "tail_trim_tolerance"
    assert seg["placed_audio_duration"] > 1.9
    assert seg["narration"] == "一段刚好超出零点几帧的解说。"  # authored text is preserved, only the tail is trimmed


def test_p0_subtitles_use_spoken_text_not_authored_narration(tmp_path):
    segs = [
        {
            "start": 0.0,
            "end": 3.0,
            "actual_place_start": 0.25,
            "actual_place_end": 2.0,
            "narration": "作者原文第一句。作者原文第二句不应出现在字幕。",
            "spoken_text": "实际说出的第一句。",
        }
    ]

    entries = _subtitle_entries(segs)
    _generate_srt(segs, tmp_path)
    _generate_ass(segs, tmp_path)
    srt = (tmp_path / "subtitles.srt").read_text(encoding="utf-8")
    ass = (tmp_path / "subtitles.ass").read_text(encoding="utf-8")

    assert "实际说出的第一句" in "".join(e["text"] for e in entries)
    assert "作者原文第二句" not in srt
    assert "作者原文第二句" not in ass
    assert "实际说出的第一句" in srt
    assert "实际说出的第一句" in ass


def test_p0_final_loudness_filter_records_mode_and_peak_protection(monkeypatch):
    monkeypatch.setitem(CONFIG, "final_loudnorm", True)
    monkeypatch.setitem(CONFIG, "target_lufs", -14.0)
    monkeypatch.setitem(CONFIG, "target_true_peak", -1.0)
    monkeypatch.setitem(CONFIG, "target_lra", 11.0)
    filt = final_loudnorm_filter()
    assert "loudnorm=" in filt
    assert "linear=true" in filt
    assert "alimiter=limit=0.98:level=false" in filt
    fp = assembly_settings_fingerprint()["audio_mix"]
    assert fp["loudness_mode"] in {"two_pass_linear", "equivalent"}
    assert fp["final_loudnorm"] == filt

    monkeypatch.setitem(CONFIG, "final_loudnorm", False)
    off_filt = final_loudnorm_filter()
    assert "loudnorm" not in off_filt
    assert "alimiter=limit=0.98:level=false" in off_filt
    assert assembly_settings_fingerprint()["audio_mix"]["loudness_mode"] == "limiter_only"


def test_p0_manifest_references_audio_qc_artifact(tmp_path):
    video = tmp_path / "input.mp4"
    output = tmp_path / "out.mp4"
    video.write_bytes(b"v")
    output.write_bytes(b"o")
    qc = tmp_path / "assembly_qc.json"
    qc.write_text(json.dumps({
        "schema_version": 1,
        "verdict": "FAIL",
        "blocking_codes": ["no_safe_fit"],
        "loudness_mode": "two_pass_linear",
        "loudnorm_measurement": {"input_i": "-18.0", "target_offset": "0.1"},
    }), encoding="utf-8")

    manifest = _assembly_manifest_payload(video, [], tmp_path, output)

    assert manifest["qc_path"].endswith("assembly_qc.json")
    assert manifest["qc_verdict"] == "FAIL"
    assert manifest["qc_blocking_codes"] == ["no_safe_fit"]
    assert manifest["qc_loudness_mode"] == "two_pass_linear"
    assert manifest["qc_loudnorm_measurement"] == {"input_i": "-18.0", "target_offset": "0.1"}
    assert manifest["assembly_settings"]["audio_mix"]["loudness_mode"] in {
        "two_pass_linear", "equivalent", "limiter_only"
    }


def test_p0_assembly_qc_blocks_skipped_segments(tmp_path):
    qc = assemble._build_assembly_qc(
        [
            {
                "index": 0,
                "fit_status": "fits",
                "placed_audio_duration": 0.5,
                "effective_tempo": 1.15,
            },
            {
                "index": 1,
                "fit_status": "skipped",
                "truncate_reason": "missing_wav",
                "placed_audio_duration": 0.0,
                "effective_tempo": 1.15,
            },
        ],
        3.0,
        source_has_audio=True,
        loudness_mode="two_pass_linear",
    )

    assert qc["verdict"] == "FAIL"
    assert "skipped_segments" in qc["blocking_codes"]
    assert qc["summary"]["skipped_segments"] == [1]


def test_p0_assembly_qc_blocks_speed_adjust_failed(tmp_path):
    qc = assemble._build_assembly_qc(
        [
            {
                "index": 0,
                "fit_status": "speed_adjust_failed",
                "truncate_reason": "resample_failed",
                "placed_audio_duration": 0.0,
                "effective_tempo": 1.15,
            }
        ],
        3.0,
        source_has_audio=True,
        loudness_mode="two_pass_linear",
    )

    assert qc["verdict"] == "FAIL"
    assert "fit_failed" in qc["blocking_codes"]
    assert qc["summary"]["fit_failed_segments"] == [0]


# --- Subtitle / visual-presentation special plan contracts -------------------

_VISUAL_QC_ALLOWED_TOP_LEVEL = {
    "schema_version",
    "artifact",
    "verdict",
    "blocking",
    "blocking_codes",
    "geometry",
    "subtitles",
    "overlays",
    "mask",
    "summary",
}
_VISUAL_QC_FORBIDDEN_DELIVERY_KEYS = {
    "video_encode_passes",
    "reencode_reason",
    "audio_sample_rate",
    "final_compat_notes",
    "double_encode",
    "delivery_compatibility",
    "codec",
    "audio_codec",
}


def _flatten_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _flatten_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _flatten_keys(child)


def test_visual_qc_builder_excludes_delivery_facts_from_visual_layer(monkeypatch, tmp_path):
    """visual_qc.json is a visual-facts artifact only; delivery/encode facts belong to
    assembly_qc rollup, never to the visual layer."""
    monkeypatch.setitem(CONFIG, "burn_subtitles", True)
    monkeypatch.setitem(CONFIG, "mask_source_subtitles", False)
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_policy", "off")
    (tmp_path / "visual_overlays.json").write_text(json.dumps({"schema_version": 1, "overlays": [
        {"type": "top_title", "text": "第一章", "start": 0.0, "end": 2.0},
    ]}), encoding="utf-8")

    filters, overlay_qc = assemble._visual_overlay_filters(tmp_path, {"width": 1080, "height": 1920}, 5.0)
    qc = assemble._build_visual_qc(
        [{"start": 0.0, "end": 2.0, "actual_place_start": 0.0, "actual_place_end": 2.0, "narration": "多行\n字幕"}],
        tmp_path,
        5.0,
        {"width": 1080, "height": 1920, "fps": 30.0, "rotation": 90, "sample_aspect_ratio": "1:1", "display_aspect_ratio": "9:16"},
        overlay_qc=overlay_qc,
    )

    assert set(qc) <= _VISUAL_QC_ALLOWED_TOP_LEVEL
    assert not (_VISUAL_QC_FORBIDDEN_DELIVERY_KEYS & set(_flatten_keys(qc)))
    assert qc["geometry"]["canvas"]["width"] == 1080
    assert qc["subtitles"]["multi_line"] is True
    assert qc["mask"]["policy"] == "off"
    assert qc["overlays"]["facts"][0]["type"] == "top_title"
    assert filters and all("drawtext=" in f for f in filters)


def test_subtitle_layout_qc_records_multiline_safe_area_and_overflow(monkeypatch):
    monkeypatch.setitem(CONFIG, "subtitle_max_lines", 2)
    style = {
        "font_size": 42,
        "outline": 2,
        "shadow": 1,
        "margin_l": 40,
        "margin_r": 40,
        "margin_v": 48,
        "max_chars": 20,
        "play_res_x": 640,
        "play_res_y": 360,
        "alignment": 2,
    }
    qc = assemble._subtitle_layout_qc(
        [{"start": 0, "end": 2, "text": "第一行\n第二行\n第三行"}],
        {"width": 640, "height": 360},
        style,
    )

    assert qc["safe_area"]["width"] == 560
    assert qc["multi_line"] is True
    assert qc["overflow"] is True
    assert qc["overflow_entries"][0]["overflow_reasons"] == ["max_lines_exceeded"]


def test_assembly_qc_rolls_up_visual_and_delivery_facts_without_polluting_visual():
    """assembly_qc.json is the release-gate rollup: it consumes visual_qc plus
    delivery facts while preserving visual_qc as a pure visual artifact."""
    visual_qc = {
        "artifact": "visual_qc.json",
        "verdict": "PASS",
        "blocking": False,
        "blocking_codes": [],
        "geometry": {"canvas": {"width": 1080, "height": 1920}, "rotation": 90},
        "subtitles": {"overflow": False, "entries": 1, "multi_line": True, "safe_area": {"x": 54}},
        "overlays": {"present": True, "rendered": 1, "facts": [{"type": "inline_label_or_callout", "overflow": False}]},
        "mask": {"policy": "safe", "scope": "bottom_band", "trigger": "explicit", "reason": "source_subtitles"},
        "summary": {"subtitle_entries": 1, "overlay_rendered": 1},
    }
    delivery_qc = {
        "video_encode_passes": 1,
        "reencode_reason": "burn_subtitles",
        "audio_sample_rate": 48000,
        "final_compat_notes": ["faststart", "yuv420p"],
    }

    qc = assemble._build_assembly_qc(
        [{"index": 0, "fit_status": "fit", "placed_audio_duration": 1.0, "effective_tempo": 1.0}],
        2.0,
        source_has_audio=True,
        loudness_mode="limiter_only",
        visual_qc=visual_qc,
        delivery_qc=delivery_qc,
    )

    assert qc["visual_qc"]["geometry"] == visual_qc["geometry"]
    assert qc["visual_qc"]["subtitles"]["overflow"] is False
    assert qc["visual_qc"]["mask"] == visual_qc["mask"]
    assert qc["delivery_qc"]["audio_sample_rate"] == 48000
    assert qc["delivery_qc"]["reencode_reason"] == "burn_subtitles"
    assert qc["release_gate"]["visual_qc"] == "PASS"
    assert not (_VISUAL_QC_FORBIDDEN_DELIVERY_KEYS & set(_flatten_keys(visual_qc)))


def test_mask_policy_must_be_explicit_and_cache_fingerprint_safe(monkeypatch):
    """Changing source-subtitle mask policy changes rendered pixels, so the
    effective policy must be declared and included in the assembly cache fingerprint."""
    monkeypatch.setitem(CONFIG, "burn_subtitles", True)
    monkeypatch.setitem(CONFIG, "mask_source_subtitles", True)
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_policy", "safe")
    safe_fp = assembly_settings_fingerprint()
    assert safe_fp["video_filters"]["source_subtitle_mask_policy"] == "safe"

    monkeypatch.setitem(CONFIG, "source_subtitle_mask_policy", "forced")
    forced_fp = assembly_settings_fingerprint()
    assert forced_fp["video_filters"]["source_subtitle_mask_policy"] == "forced"
    assert forced_fp != safe_fp

    monkeypatch.delitem(CONFIG, "source_subtitle_mask_policy", raising=False)
    qc = assemble._source_subtitle_mask_policy()
    assert qc["policy"] == "legacy_implicit"
    assert qc.get("blocking") is True


def test_visual_overlay_loader_uses_canonical_artifact_and_rejects_platform_expansion(tmp_path):
    """visual_overlays.json is the sole first-release overlay input, and only
    top_title plus inline_label_or_callout are supported."""
    (tmp_path / "overlays.json").write_text(json.dumps([{"type": "card", "text": "wrong file"}]), encoding="utf-8")
    (tmp_path / "visual_overlays.json").write_text(json.dumps({"schema_version": 1, "overlays": [
        {"type": "top_title", "text": "第一章", "start": 0.0, "end": 2.0},
        {"type": "inline_label_or_callout", "text": "关键证据", "start": 1.0, "end": 3.0, "x": 0.2, "y": 0.3},
    ]}), encoding="utf-8")

    overlays = assemble._load_visual_overlays(tmp_path)
    assert [item["type"] for item in overlays] == ["top_title", "inline_label_or_callout"]
    filters, qc = assemble._visual_overlay_filters(tmp_path, {"width": 1280, "height": 720}, 5.0)
    assert len(filters) == 2
    assert qc["rendered"] == 2
    assert qc["unsupported"] == []

    (tmp_path / "visual_overlays.json").write_text(json.dumps({"schema_version": 1, "overlays": [
        {"type": "top_title", "text": "allowed", "start": 0.0, "end": 1.0},
        {"type": "chapter_card", "text": "not in first release", "start": 1.0, "end": 2.0},
    ]}), encoding="utf-8")
    filters, qc = assemble._visual_overlay_filters(tmp_path, {"width": 1280, "height": 720}, 5.0)
    assert len(filters) == 1
    assert qc["unsupported"] == [{"index": 1, "type": "chapter_card", "reason": "unsupported_overlay_type"}]


def test_assemble_video_render_failure_does_not_leave_pass_assembly_qc(monkeypatch, tmp_path):
    """A stale/pre-render PASS assembly_qc.json must be removed before render, and a
    failed ffmpeg delivery must not leave a PASS final assembly_qc behind."""
    from subprocess import CompletedProcess

    input_video = tmp_path / "input.mp4"
    output = tmp_path / "out.mp4"
    input_video.write_bytes(b"video")
    (tmp_path / "assembly_qc.json").write_text(json.dumps({"verdict": "PASS"}), encoding="utf-8")
    segs = [{
        "index": 0,
        "start": 0.0,
        "end": 1.0,
        "actual_place_start": 0.0,
        "actual_place_end": 1.0,
        "placed_audio_duration": 1.0,
        "fit_status": "fit",
        "effective_tempo": 1.0,
        "narration": "hello",
    }]

    monkeypatch.setitem(CONFIG, "burn_subtitles", False)
    monkeypatch.setitem(CONFIG, "mask_source_subtitles", False)
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_policy", "off")
    monkeypatch.setattr(assemble, "get_video_duration", lambda path: 2.0)
    monkeypatch.setattr(assemble, "_probe_canvas", lambda path: {"width": 1280, "height": 720, "fps": 30.0})
    monkeypatch.setattr(assemble, "_apply_narration_speed", lambda segments, work_dir: None)
    monkeypatch.setattr(assemble, "_build_timed_narration", lambda segments, wav, duration, work_dir: Path(wav).write_bytes(b"wav"))
    monkeypatch.setattr(assemble, "_generate_srt", lambda segments, work_dir, duration: Path(work_dir) / "subtitles.srt")
    monkeypatch.setattr(assemble, "_emit_timeline", lambda *args, **kwargs: None)
    monkeypatch.setattr(assemble, "_has_audio_stream", lambda path: True)
    monkeypatch.setattr(assemble, "_run_loudnorm_first_pass", lambda *args, **kwargs: None)
    monkeypatch.setattr(assemble, "run_cmd", lambda cmd, **kwargs: CompletedProcess(cmd, 1, "", "ffmpeg failed"))

    with pytest.raises(RuntimeError, match="视频组装失败"):
        assemble_video(input_video, segs, tmp_path, output)

    qc_path = tmp_path / "assembly_qc.json"
    assert not qc_path.exists() or json.loads(qc_path.read_text(encoding="utf-8")).get("verdict") != "PASS"
    assert (tmp_path / "visual_qc.json").exists()


def test_malformed_visual_overlays_blocks_visual_qc(tmp_path, monkeypatch):
    monkeypatch.setitem(CONFIG, "burn_subtitles", False)
    monkeypatch.setitem(CONFIG, "mask_source_subtitles", False)
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_policy", "off")
    (tmp_path / "visual_overlays.json").write_text("{not json", encoding="utf-8")

    overlays, source = assemble._load_visual_overlays(tmp_path, with_source=True)
    filters, overlay_qc = assemble._visual_overlay_filters(tmp_path, {"width": 1280, "height": 720}, 5.0)
    qc = assemble._build_visual_qc([], tmp_path, 5.0, {"width": 1280, "height": 720, "fps": 30.0}, overlay_qc=overlay_qc)

    assert overlays == []
    assert filters == []
    assert source["load_error"] == "invalid_json"
    assert overlay_qc["load_error"] == "invalid_json"
    assert qc["verdict"] == "FAIL"
    assert "invalid_visual_overlays_json" in qc["blocking_codes"]


@pytest.mark.parametrize("payload", [
    [{"type": "top_title", "text": "legacy top-level list"}],
    {"overlays": []},
    {"schema_version": 2, "overlays": []},
    {"schema_version": "1", "overlays": []},
    {"schema_version": True, "overlays": []},
    {"overlays": {"type": "top_title", "text": "not a list"}},
    "not a schema",
    42,
    {"schema_version": 1},
])
def test_invalid_visual_overlays_schema_blocks_visual_qc(tmp_path, monkeypatch, payload):
    monkeypatch.setitem(CONFIG, "burn_subtitles", False)
    monkeypatch.setitem(CONFIG, "mask_source_subtitles", False)
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_policy", "off")
    (tmp_path / "visual_overlays.json").write_text(json.dumps(payload), encoding="utf-8")

    overlays, source = assemble._load_visual_overlays(tmp_path, with_source=True)
    filters, overlay_qc = assemble._visual_overlay_filters(tmp_path, {"width": 1280, "height": 720}, 5.0)
    qc = assemble._build_visual_qc([], tmp_path, 5.0, {"width": 1280, "height": 720, "fps": 30.0}, overlay_qc=overlay_qc)

    assert overlays == []
    assert filters == []
    assert source["load_error"] == "invalid_schema"
    assert overlay_qc["load_error"] == "invalid_schema"
    assert qc["verdict"] == "FAIL"
    assert "invalid_visual_overlays_json" in qc["blocking_codes"]


def test_measured_subtitle_band_is_the_visual_qc_safe_area(tmp_path, monkeypatch):
    """A band too narrow for even the minimum font must block instead of passing canvas QC."""
    monkeypatch.setitem(CONFIG, "burn_subtitles", True)
    monkeypatch.setitem(CONFIG, "mask_source_subtitles", True)
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_policy", "opt_in")
    monkeypatch.setitem(CONFIG, "subtitle_y_top", 610)
    monkeypatch.setitem(CONFIG, "subtitle_y_bot", 620)
    monkeypatch.setitem(CONFIG, "subtitle_mask_padding", 0)

    qc = assemble._build_visual_qc(
        [{"start": 0.0, "end": 1.0, "narration": "窄字幕带"}],
        tmp_path,
        2.0,
        {"width": 1280, "height": 720, "fps": 30.0, "sample_aspect_ratio": "1:1"},
    )

    assert qc["subtitles"]["safe_area"]["y"] == 610
    assert qc["subtitles"]["safe_area"]["height"] == 10
    assert qc["subtitles"]["overflow"] is True
    assert "subtitle_overflow" in qc["blocking_codes"]


def test_measured_subtitle_qc_contains_normal_line_above_anchored_bottom(
    tmp_path, monkeypatch
):
    monkeypatch.setitem(CONFIG, "burn_subtitles", True)
    monkeypatch.setitem(CONFIG, "mask_source_subtitles", True)
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_policy", "opt_in")
    monkeypatch.setitem(CONFIG, "subtitle_y_top", 610)
    monkeypatch.setitem(CONFIG, "subtitle_y_bot", 650)
    monkeypatch.setitem(CONFIG, "subtitle_mask_padding", 4)

    qc = assemble._build_visual_qc(
        [{"start": 0.0, "end": 1.0, "narration": "正常字幕带"}],
        tmp_path,
        2.0,
        {"width": 1280, "height": 720, "fps": 30.0, "sample_aspect_ratio": "1:1"},
    )

    safe = qc["subtitles"]["safe_area"]
    entry = qc["subtitles"]["entry_facts"][0]
    assert safe == {"x": 40, "y": 606, "width": 1200, "height": 44, "bottom_margin": 70}
    assert entry["band_height"] <= safe["height"]
    assert qc["subtitles"]["overflow"] is False


def test_legacy_mask_env_without_explicit_policy_is_blocking(monkeypatch):
    import importlib
    import lib as assemble_lib

    snapshot = dict(CONFIG)
    try:
        monkeypatch.setenv("MASK_SOURCE_SUBTITLES", "1")
        monkeypatch.delenv("SOURCE_SUBTITLE_MASK_POLICY", raising=False)
        importlib.reload(assemble_lib)

        policy = assemble._source_subtitle_mask_policy()

        assert CONFIG["mask_source_subtitles"] is True
        assert CONFIG["source_subtitle_mask_policy"] == "off"
        assert CONFIG["source_subtitle_mask_policy_declared"] is False
        assert policy["policy"] == "legacy_implicit"
        assert policy["declared"] is False
        assert policy["blocking"] is True
    finally:
        CONFIG.clear()
        CONFIG.update(snapshot)
