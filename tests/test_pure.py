"""Pure function unit tests for video-recap modules."""
import sys
from pathlib import Path
from subprocess import CompletedProcess

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills' / 'video-recap' / 'scripts'))

from common import _retry_after_seconds, get_video_duration
from config import CONFIG, env_bool, env_float, env_int, normalize_api_url
from detect import detect_scenes
from extract import extract_frames
from pipeline import (
    _annotate_cut_narration_overlap,
    _assemble_settings_current,
    _command_available,
    _ffmpeg_has_filter,
    run_pipeline,
)
from edit import (
    build_edited_source_video,
    map_narration_to_clips,
    normalize_clip_plan,
    parse_duration_seconds,
    source_time_to_output_time,
)
from narration import _text_char_count, lint_narration
from tts import _build_tts_segment_result, _detect_tts_engine, _parse_rate_offset, synthesize_tts
from assemble import (
    _escape_ass_text,
    _generate_ass,
    _generate_srt,
    _seconds_to_ass_time,
    _seconds_to_srt_time,
    _subtitle_burn_filter,
    assembly_settings_fingerprint,
    assemble_video,
)
from vlm import analyze_scenes


def test_text_char_count():
    assert _text_char_count("hello") == 5
    assert _text_char_count("你好世界") == 4
    assert _text_char_count("") == 0


def test_parse_rate_offset():
    assert _parse_rate_offset("+0%") == 0.0
    assert _parse_rate_offset("+20%") == 0.2
    assert _parse_rate_offset("-10%") == -0.1
    assert _parse_rate_offset("+5%") == 0.05


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


def test_lint_narration_reports_warnings_and_errors(tmp_path, monkeypatch):
    monkeypatch.setitem(CONFIG, "speech_rate", 3.5)
    monkeypatch.setitem(CONFIG, "speech_safety_margin", 0.85)
    report = lint_narration([
        {"start": 0.0, "end": 3.0, "narration": "这是一段明显超过时间预算的很长很长解说文本。"},
        {"start": 2.5, "end": 4.0, "narration": "第二段没有句号"},
        {"start": 4.0, "end": 4.5, "narration": "太短。"},
        {"start": 5.0, "end": 6.0, "narration": ""},
    ], [{"scene_id": 0, "start": 0.0, "end": 6.0}], work_dir=tmp_path)

    assert report["ok"] is False
    codes = {issue["code"] for issue in report["errors"] + report["warnings"]}
    assert "over_budget" in codes
    assert "time_overlap" in codes
    assert "slot_too_short" in codes
    assert "empty_narration" in codes
    assert "incomplete_sentence" in codes
    assert (tmp_path / "narration_lint.json").exists()


def test_lint_narration_cut_mode_requires_clip_membership():
    plan = {"clips": [{"clip_id": 0, "source_start": 10.0, "source_end": 20.0}]}
    report = lint_narration([
        {"start": 11.0, "end": 15.0, "narration": "片段内解说。"},
        {"start": 22.0, "end": 24.0, "narration": "片段外解说。"},
    ], [{"scene_id": 0, "start": 0.0, "end": 30.0}], clip_plan=plan, mode="cut")

    assert report["ok"] is False
    assert any(issue["code"] == "outside_clip_plan" for issue in report["errors"])


def test_lint_narration_density_metrics_and_warnings(monkeypatch):
    monkeypatch.setitem(CONFIG, "target_segments_per_minute", 9.6)
    monkeypatch.setitem(CONFIG, "min_segments_per_minute", 6.24)
    monkeypatch.setitem(CONFIG, "max_narration_gap_seconds", 11.0)

    # Sparse + a long gap → should warn low_density and long_gap, and emit metrics.
    sparse = lint_narration([
        {"start": 0.0, "end": 4.0, "narration": "第一句。", "pause_after_ms": 250},
        {"start": 40.0, "end": 44.0, "narration": "很久之后的第二句。", "pause_after_ms": 250},
    ], mode="full")
    sparse_codes = {issue["code"] for issue in sparse["warnings"]}
    assert "low_density" in sparse_codes
    assert "long_gap" in sparse_codes
    assert sparse["metrics"]["segment_count"] == 2
    assert sparse["metrics"]["max_gap_seconds"] == 36.0

    # Dense, continuous → no density warnings.
    dense = []
    t = 0.0
    for _ in range(10):
        dense.append({"start": round(t, 2), "end": round(t + 4.5, 2),
                      "narration": "一句紧凑的解说。", "pause_after_ms": 250})
        t += 6.0
    dense_report = lint_narration(dense, mode="full")
    dense_codes = {issue["code"] for issue in dense_report["warnings"]}
    assert "low_density" not in dense_codes
    assert "long_gap" not in dense_codes
    assert dense_report["metrics"]["segments_per_minute"] >= CONFIG["min_segments_per_minute"]

    # Cut mode measures density on the mapped timeline elsewhere → no density metrics here.
    cut_report = lint_narration(sparse, mode="cut")
    assert cut_report["metrics"] == {}


def test_final_loudnorm_filter_and_fingerprint(monkeypatch):
    from assemble import assembly_settings_fingerprint, final_loudnorm_filter

    monkeypatch.setitem(CONFIG, "final_loudnorm", True)
    monkeypatch.setitem(CONFIG, "target_lufs", -14.0)
    monkeypatch.setitem(CONFIG, "target_true_peak", -1.0)
    monkeypatch.setitem(CONFIG, "target_lra", 11.0)
    assert final_loudnorm_filter() == "loudnorm=I=-14.0:TP=-1.0:LRA=11.0"
    assert assembly_settings_fingerprint()["final_loudnorm"] == "loudnorm=I=-14.0:TP=-1.0:LRA=11.0"

    monkeypatch.setitem(CONFIG, "target_lufs", -11.9)
    assert final_loudnorm_filter() == "loudnorm=I=-11.9:TP=-1.0:LRA=11.0"

    monkeypatch.setitem(CONFIG, "final_loudnorm", False)
    assert final_loudnorm_filter() is None
    assert assembly_settings_fingerprint()["final_loudnorm"] == "off"


def test_get_video_duration_returns_zero_for_unparseable_output(monkeypatch):
    def fake_run_cmd(cmd):
        return CompletedProcess(cmd, 0, stdout="N/A\n", stderr="")

    monkeypatch.setattr("common.run_cmd", fake_run_cmd)
    assert get_video_duration("bad.mp4") == 0.0


def test_retry_after_seconds_accepts_malformed_header():
    assert _retry_after_seconds("not-a-number-or-date", 4) == 4


def test_normalize_api_url_accepts_base_or_full_endpoint():
    assert normalize_api_url("https://example.com/v1") == "https://example.com/v1/chat/completions"
    assert normalize_api_url("https://example.com/v1/") == "https://example.com/v1/chat/completions"
    assert normalize_api_url("https://example.com/v1/chat/completions") == "https://example.com/v1/chat/completions"


def test_env_int_bool_and_float_helpers_tolerate_bad_values(monkeypatch):
    monkeypatch.setenv("BAD_INT", "not-an-int")
    monkeypatch.setenv("LOW_INT", "-3")
    monkeypatch.setenv("YES_BOOL", "on")
    monkeypatch.setenv("NO_BOOL", "0")
    monkeypatch.setenv("BAD_FLOAT", "nope")
    monkeypatch.setenv("LOW_FLOAT", "-1.5")

    assert env_int("BAD_INT", 8, minimum=1) == 8
    assert env_int("LOW_INT", 8, minimum=1) == 1
    assert env_bool("YES_BOOL") is True
    assert env_bool("NO_BOOL", default=True) is False
    assert env_float("BAD_FLOAT", 0.5, minimum=0) == 0.5
    assert env_float("LOW_FLOAT", 0.5, minimum=0) == 0


def test_parse_duration_seconds_accepts_common_forms():
    assert parse_duration_seconds("600") == 600
    assert parse_duration_seconds("10m") == 600
    assert parse_duration_seconds("00:10:00") == 600
    assert parse_duration_seconds("1:02") == 62
    assert parse_duration_seconds(90) == 90
    assert parse_duration_seconds("") is None
    with pytest.raises(ValueError):
        parse_duration_seconds("nope")
    with pytest.raises(ValueError):
        parse_duration_seconds("-1")


def test_normalize_clip_plan_clamps_and_maps_output_timeline():
    plan = normalize_clip_plan({
        "target_duration": "10s",
        "clips": [
            {"start": 1.0, "end": 4.0, "reason": "开端"},
            {"start": 8.0, "end": 12.0, "reason": "反转"},
            {"start": 5.0, "end": 5.1, "reason": "too short"},
            {"start": "bad", "end": 7.0},
        ],
    }, video_duration=10.0, clip_padding=0.5)

    assert plan["target_duration"] == 10.0
    assert plan["total_duration"] == 6.5
    assert len(plan["clips"]) == 2
    assert plan["clips"][0]["source_start"] == 0.5
    assert plan["clips"][0]["source_end"] == 4.5
    assert plan["clips"][0]["output_start"] == 0.0
    assert plan["clips"][1]["source_start"] == 7.5
    assert plan["clips"][1]["source_end"] == 10.0
    assert plan["clips"][1]["output_start"] == 4.0


def test_clip_plan_rejects_overlapping_source_ranges():
    with pytest.raises(ValueError, match="overlaps an earlier source range"):
        normalize_clip_plan([
            {"start": 1.0, "end": 5.0},
            {"start": 4.5, "end": 8.0},
        ], video_duration=10.0)


def test_source_time_and_narration_mapping_preserve_source_trace():
    plan = normalize_clip_plan([
        {"start": 10.0, "end": 20.0, "reason": "A"},
        {"start": 40.0, "end": 50.0, "reason": "B"},
    ], video_duration=60.0)

    assert source_time_to_output_time(12.5, plan["clips"]) == 2.5
    assert source_time_to_output_time(45.0, plan["clips"]) == 15.0
    assert source_time_to_output_time(30.0, plan["clips"]) is None

    mapped = map_narration_to_clips([
        {"start": 12.0, "end": 16.0, "narration": "第一段。"},
        {"start": 43.0, "end": 48.0, "narration": "第二段。", "overlaps_speech": True},
        {"start": 22.0, "end": 24.0, "narration": "会被丢弃。"},
    ], plan)

    assert [(m["start"], m["end"]) for m in mapped] == [(2.0, 6.0), (13.0, 18.0)]
    assert mapped[0]["source_start"] == 12.0
    assert mapped[1]["source_clip_id"] == 1
    assert mapped[1]["overlaps_speech"] is True


def test_narration_mapping_uses_explicit_source_clip_id_for_repeated_ranges():
    plan = normalize_clip_plan([
        {"start": 10.0, "end": 20.0},
        {"start": 10.0, "end": 20.0},
    ], video_duration=30.0, allow_overlap=True)

    unmapped = map_narration_to_clips([
        {"start": 12.0, "end": 14.0, "narration": "重复画面但没说用哪次。"},
    ], plan)
    mapped = map_narration_to_clips([
        {"start": 12.0, "end": 14.0, "source_clip_id": 1, "narration": "重复画面第二次出现。"},
    ], plan)

    assert unmapped == []
    assert mapped[0]["start"] == 12.0
    assert mapped[0]["source_clip_id"] == 1


def test_detect_scenes_respects_zero_threshold(monkeypatch, tmp_path):
    commands = []

    def fake_run_cmd(cmd, **kwargs):
        commands.append(cmd)
        return CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("detect.run_cmd", fake_run_cmd)
    monkeypatch.setattr("detect.get_video_duration", lambda video: 12.5)
    scenes = detect_scenes(Path("video.mp4"), tmp_path, threshold=0.0)

    assert "scdet=threshold=0" in commands[0]
    assert scenes == [{"start": 0.0, "end": 12.5}]


def test_extract_frames_rejects_non_positive_fps(tmp_path):
    with pytest.raises(ValueError, match="fps 必须大于 0"):
        extract_frames(Path("video.mp4"), tmp_path, fps=0)


def test_analyze_scenes_rejects_empty_frames(tmp_path):
    with pytest.raises(RuntimeError, match="frames 为空"):
        analyze_scenes([{"start": 0.0, "end": 1.0}], [], tmp_path)


def test_annotate_cut_narration_overlap_preserves_source_times():
    narration = [
        {"start": 1.0, "end": 3.0, "narration": "安静窗口。", "overlaps_speech": True},
        {"start": 5.0, "end": 7.0, "narration": "对白窗口。", "overlaps_speech": False},
    ]

    result = _annotate_cut_narration_overlap(narration, [{"start": 0.5, "end": 3.5, "has_speech": False}])

    assert result[0]["start"] == 1.0
    assert result[0]["overlaps_speech"] is False
    assert result[1]["start"] == 5.0
    assert result[1]["overlaps_speech"] is True


def test_build_tts_segment_result_preserves_source_trace(tmp_path):
    result = _build_tts_segment_result(0, {
        "start": 1.0,
        "end": 3.0,
        "source_start": 11.0,
        "source_end": 13.0,
        "source_clip_id": 2,
        "narration": "测试。",
    }, "测试。", tmp_path / "narr.wav", 1.0, 0.0)

    assert result["source_start"] == 11.0
    assert result["source_end"] == 13.0
    assert result["source_clip_id"] == 2


def test_synthesize_tts_handles_empty_narration(monkeypatch, tmp_path):
    monkeypatch.setitem(__import__("config").CONFIG, "tts_engine", "say")
    segments, engine = synthesize_tts([], tmp_path)

    assert segments == []
    assert engine == "say"


def test_synthesize_tts_raises_on_failed_segment_by_default(monkeypatch, tmp_path):
    narration = [
        {"start": 0.0, "end": 1.0, "narration": "第一段。"},
        {"start": 1.0, "end": 2.0, "narration": "第二段。"},
    ]

    def fake_synthesize_segment(i, seg, narration_data, tts_dir, engine):
        if i == 1:
            raise RuntimeError("network timeout")
        return {
            "index": i,
            "start": seg["start"],
            "end": seg["end"],
            "narration": seg["narration"],
            "audio_path": str(tts_dir / f"narr_{i:03d}.wav"),
            "audio_duration": 0.5,
        }

    monkeypatch.setitem(CONFIG, "tts_engine", "say")
    monkeypatch.setitem(CONFIG, "tts_workers", 2)
    monkeypatch.setitem(CONFIG, "allow_partial_tts", False)
    monkeypatch.setattr("tts._synthesize_segment", fake_synthesize_segment)

    with pytest.raises(RuntimeError, match="TTS 失败 1/2 段"):
        synthesize_tts(narration, tmp_path)


def test_synthesize_tts_allows_partial_when_configured(monkeypatch, tmp_path):
    narration = [
        {"start": 0.0, "end": 1.0, "narration": "第一段。"},
        {"start": 1.0, "end": 2.0, "narration": "第二段。"},
    ]

    def fake_synthesize_segment(i, seg, narration_data, tts_dir, engine):
        if i == 1:
            raise RuntimeError("network timeout")
        return {
            "index": i,
            "start": seg["start"],
            "end": seg["end"],
            "narration": seg["narration"],
            "audio_path": str(tts_dir / f"narr_{i:03d}.wav"),
            "audio_duration": 0.5,
        }

    monkeypatch.setitem(CONFIG, "tts_engine", "say")
    monkeypatch.setitem(CONFIG, "tts_workers", 2)
    monkeypatch.setitem(CONFIG, "allow_partial_tts", True)
    monkeypatch.setattr("tts._synthesize_segment", fake_synthesize_segment)

    segments, engine = synthesize_tts(narration, tmp_path)

    assert engine == "say"
    assert [s["index"] for s in segments] == [0]


def test_detect_tts_engine_prefers_edge_tts(monkeypatch):
    monkeypatch.setattr("tts.shutil.which", lambda cmd: "/usr/bin/edge-tts" if cmd == "edge-tts" else None)

    assert _detect_tts_engine() == "edge-tts"


def test_command_available_checks_path_and_executable_name(tmp_path, monkeypatch):
    executable = tmp_path / "tool"
    executable.write_text("#!/bin/sh\n")
    monkeypatch.setattr("pipeline.shutil.which", lambda cmd: "/bin/echo" if cmd == "echo" else None)

    assert _command_available(str(executable))
    assert _command_available("echo")
    assert not _command_available("missing-command")


def test_ffmpeg_has_filter_parses_filter_table(monkeypatch):
    def fake_run_cmd(cmd):
        return CompletedProcess(cmd, 0, stdout=" T.C subtitles        V->V       Render text subtitles\n", stderr="")

    monkeypatch.setattr("pipeline.run_cmd", fake_run_cmd)
    assert _ffmpeg_has_filter("subtitles") is True
    assert _ffmpeg_has_filter("missing") is False


def test_step_tts_runs_from_cached_narration_without_api(monkeypatch, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "narration.json").write_text(
        '[{"start":0.0,"end":3.0,"narration":"你好世界。"}]',
        encoding="utf-8",
    )

    monkeypatch.setitem(CONFIG, "api_key", "")
    monkeypatch.setitem(CONFIG, "fps", 1)
    monkeypatch.setitem(CONFIG, "burn_subtitles", False)
    monkeypatch.setattr("pipeline.check_prerequisites", lambda skip_asr=False: True)
    monkeypatch.setattr("pipeline.get_video_duration", lambda path: 3.0)
    monkeypatch.setattr("pipeline.api_call", lambda payload: (_ for _ in ()).throw(AssertionError("API should not be called")))
    monkeypatch.setattr("pipeline.synthesize_tts", lambda narration, wd: ([{
        "index": 0,
        "start": 0.0,
        "end": 3.0,
        "narration": narration[0]["narration"],
        "audio_path": str(wd / "narr_000.wav"),
        "audio_duration": 1.0,
    }], "say"))

    result = run_pipeline(video, step="tts", resume_dir=work_dir)

    assert result["engine"] == "say"
    assert len(result["segments"]) == 1
    assert (work_dir / ".step_tts.done").exists()
    assert (work_dir / "tts_meta.json").exists()


def test_full_pipeline_pauses_for_agent_brief_without_script_api(monkeypatch, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake")
    output = tmp_path / "out"

    monkeypatch.setitem(CONFIG, "api_key", "test-key")
    monkeypatch.setitem(CONFIG, "fps", 1)
    monkeypatch.setitem(CONFIG, "burn_subtitles", False)
    monkeypatch.setattr("pipeline.check_prerequisites", lambda skip_asr=False: True)
    monkeypatch.setattr("pipeline.get_video_duration", lambda path: 8.0)
    monkeypatch.setattr("pipeline.api_call", lambda payload: {"choices": [{"message": {"content": "ok"}}]})
    monkeypatch.setattr("pipeline.extract_frames", lambda video_path, work_dir: [work_dir / "frames" / "frame_00001.jpg"])
    monkeypatch.setattr("pipeline.detect_scenes", lambda video_path, work_dir, threshold: [{"start": 0.0, "end": 8.0}])
    monkeypatch.setattr("pipeline.transcribe_audio", lambda video_path, work_dir: [])
    monkeypatch.setattr("pipeline.detect_silence_periods", lambda video_path, work_dir, asr: [{"start": 1.0, "end": 6.0, "duration": 5.0, "has_speech": False}])
    monkeypatch.setattr("pipeline.analyze_scenes", lambda scenes, frames, work_dir: [{
        "scene_id": 0,
        "start": 0.0,
        "end": 8.0,
        "description": "角色沉默对视。",
        "depth_analysis": "关系紧张。",
    }])
    monkeypatch.setattr("pipeline.synthesize_tts", lambda narration, wd: (_ for _ in ()).throw(AssertionError("TTS should wait for narration")))

    result = run_pipeline(video, output_dir=output)

    work_dir = Path(result["work_dir"])
    assert result["status"] == "paused"
    assert (work_dir / "agent_narration_brief.md").exists()
    assert not (work_dir / "narration.json").exists()
    assert not (work_dir / ".step_script.done").exists()


def test_resume_validates_existing_agent_narration_without_api(monkeypatch, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / ".step_extract.done").write_text("ok")
    (work_dir / ".step_detect.done").write_text("ok")
    (work_dir / ".step_asr.done").write_text("ok")
    (work_dir / ".step_silence.done").write_text("ok")
    (work_dir / ".step_vlm.done").write_text("ok")
    (work_dir / "scenes.json").write_text('[{"start":0.0,"end":6.0}]')
    (work_dir / "asr_result.json").write_text('[]')
    (work_dir / "silence_periods.json").write_text('[{"start":0.0,"end":6.0,"duration":6.0,"has_speech":false}]')
    (work_dir / "vlm_analysis.json").write_text('[{"scene_id":0,"start":0.0,"end":6.0,"description":"测试场景"}]')
    (work_dir / "narration.json").write_text('[{"start":0.5,"end":5.5,"narration":"他终于意识到，沉默比争吵更伤人。"}]')

    monkeypatch.setitem(CONFIG, "api_key", "")
    monkeypatch.setitem(CONFIG, "fps", 1)
    monkeypatch.setitem(CONFIG, "burn_subtitles", False)
    monkeypatch.setattr("pipeline.check_prerequisites", lambda skip_asr=False: True)
    monkeypatch.setattr("pipeline.get_video_duration", lambda path: 6.0)
    monkeypatch.setattr("pipeline.api_call", lambda payload: (_ for _ in ()).throw(AssertionError("API should not be called")))
    monkeypatch.setattr("pipeline.synthesize_tts", lambda narration, wd: ([{
        "index": 0,
        "start": narration[0]["start"],
        "end": narration[0]["end"],
        "narration": narration[0]["narration"],
        "audio_path": str(wd / "narr_000.wav"),
        "audio_duration": 1.0,
    }], "say"))
    monkeypatch.setattr("pipeline.assemble_video", lambda video_path, tts_segments, wd, output_path: output_path.write_bytes(b"mp4"))

    result = run_pipeline(video, resume_dir=work_dir)

    assert result["tts_engine"] == "say"
    assert (work_dir / ".step_script.done").exists()
    assert (work_dir / "output.mp4").exists()


def test_assemble_metadata_invalidates_when_burn_setting_changes(monkeypatch, tmp_path):
    import pipeline

    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake")
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    monkeypatch.setitem(CONFIG, "burn_subtitles", False)
    pipeline._write_assemble_meta(work_dir, video)
    assert _assemble_settings_current(work_dir, video)

    monkeypatch.setitem(CONFIG, "burn_subtitles", True)
    assert not _assemble_settings_current(work_dir, video)


def test_run_settings_persist_and_restore_cut_mode(monkeypatch, tmp_path):
    import pipeline
    from pipeline import _load_run_settings, _persist_run_settings, _resume_command

    monkeypatch.setitem(CONFIG, "edit_mode", "cut")
    monkeypatch.setitem(CONFIG, "target_duration", "10m")
    monkeypatch.setitem(CONFIG, "clip_padding", 0.5)
    monkeypatch.setitem(CONFIG, "allow_clip_overlap", True)
    monkeypatch.setitem(CONFIG, "burn_subtitles", True)
    _persist_run_settings(tmp_path)

    monkeypatch.setitem(CONFIG, "edit_mode", "full")
    monkeypatch.setitem(CONFIG, "target_duration", "")
    monkeypatch.setitem(CONFIG, "clip_padding", 0.0)
    monkeypatch.setitem(CONFIG, "allow_clip_overlap", False)
    monkeypatch.setitem(CONFIG, "burn_subtitles", False)
    _load_run_settings(tmp_path)

    assert CONFIG["edit_mode"] == "cut"
    assert CONFIG["target_duration"] == "10m"
    assert CONFIG["clip_padding"] == 0.5
    assert CONFIG["allow_clip_overlap"] is True
    assert CONFIG["burn_subtitles"] is True
    cmd = _resume_command(Path("video recap.py"), Path("input video.mp4"), tmp_path)
    assert "'video recap.py'" in cmd
    assert "'input video.mp4'" in cmd
    assert "--edit-mode cut" in cmd
    assert "--target-duration 10m" in cmd
    assert "--clip-padding 0.5" in cmd
    assert "--allow-clip-overlap" in cmd
    assert "--burn-subtitles" in cmd

    # Simulate a fresh-process resume: persisted settings restore cut mode
    # before tail-step artifact generation.
    video = tmp_path / "input.mp4"
    video.write_bytes(b"fake")
    (tmp_path / "clip_plan.json").write_text('{"clips":[{"start":0,"end":2}]}')
    (tmp_path / "narration.json").write_text('[{"start":0.1,"end":1.5,"narration":"测试解说。"}]')
    monkeypatch.setitem(CONFIG, "edit_mode", "full")
    monkeypatch.setitem(CONFIG, "target_duration", "")
    monkeypatch.setitem(CONFIG, "clip_padding", 0.0)
    monkeypatch.setitem(CONFIG, "allow_clip_overlap", False)
    monkeypatch.setitem(CONFIG, "burn_subtitles", False)
    monkeypatch.setattr("pipeline.get_video_duration", lambda path: 3.0)
    monkeypatch.setattr("pipeline.build_edited_source_video", lambda video_path, plan, wd, output_path=None: Path(output_path or wd / "edited_source.mp4").write_bytes(b"edited") or Path(output_path or wd / "edited_source.mp4"))
    pipeline._load_run_settings(tmp_path)
    pipeline._ensure_cut_tail_artifacts(video, tmp_path)
    assert (tmp_path / "narration_mapped.json").exists()


def test_full_pipeline_cut_mode_pauses_for_clip_plan_and_narration(monkeypatch, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake")
    output = tmp_path / "out"

    monkeypatch.setitem(CONFIG, "api_key", "test-key")
    monkeypatch.setitem(CONFIG, "fps", 1)
    monkeypatch.setitem(CONFIG, "burn_subtitles", False)
    monkeypatch.setitem(CONFIG, "edit_mode", "cut")
    monkeypatch.setitem(CONFIG, "target_duration", "4s")
    monkeypatch.setattr("pipeline.check_prerequisites", lambda skip_asr=False: True)
    monkeypatch.setattr("pipeline.get_video_duration", lambda path: 8.0)
    monkeypatch.setattr("pipeline.api_call", lambda payload: {"choices": [{"message": {"content": "ok"}}]})
    monkeypatch.setattr("pipeline.extract_frames", lambda video_path, work_dir: [work_dir / "frames" / "frame_00001.jpg"])
    monkeypatch.setattr("pipeline.detect_scenes", lambda video_path, work_dir, threshold: [{"start": 0.0, "end": 8.0}])
    monkeypatch.setattr("pipeline.transcribe_audio", lambda video_path, work_dir: [])
    monkeypatch.setattr("pipeline.detect_silence_periods", lambda video_path, work_dir, asr: [{"start": 1.0, "end": 6.0, "duration": 5.0, "has_speech": False}])
    monkeypatch.setattr("pipeline.analyze_scenes", lambda scenes, frames, work_dir: [{
        "scene_id": 0,
        "start": 0.0,
        "end": 8.0,
        "description": "角色沉默对视。",
    }])

    result = run_pipeline(video, output_dir=output)

    work_dir = Path(result["work_dir"])
    assert result["status"] == "paused"
    assert result["edit_mode"] == "cut"
    assert result["next_step"] == "write clip_plan.json and narration.json"
    brief = (work_dir / "agent_narration_brief.md").read_text(encoding="utf-8")
    assert "clip_plan.json" in brief
    assert "ORIGINAL source timestamps" in brief


def test_pause_resume_command_preserves_burn_subtitles(monkeypatch, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake")

    monkeypatch.setitem(CONFIG, "api_key", "test-key")
    monkeypatch.setitem(CONFIG, "fps", 1)
    monkeypatch.setitem(CONFIG, "edit_mode", "full")
    monkeypatch.setitem(CONFIG, "burn_subtitles", True)
    monkeypatch.setattr("pipeline.check_prerequisites", lambda skip_asr=False: True)
    monkeypatch.setattr("pipeline._ffmpeg_has_filter", lambda filter_name: True)
    monkeypatch.setattr("pipeline.get_video_duration", lambda path: 4.0)
    monkeypatch.setattr("pipeline.api_call", lambda payload: {"choices": [{"message": {"content": "ok"}}]})
    monkeypatch.setattr("pipeline.extract_frames", lambda video_path, work_dir: [work_dir / "frames" / "frame_00001.jpg"])
    monkeypatch.setattr("pipeline.detect_scenes", lambda video_path, work_dir, threshold: [{"start": 0.0, "end": 4.0}])
    monkeypatch.setattr("pipeline.transcribe_audio", lambda video_path, work_dir: [])
    monkeypatch.setattr("pipeline.detect_silence_periods", lambda video_path, work_dir, asr: [{"start": 0.0, "end": 4.0, "duration": 4.0, "has_speech": False}])
    monkeypatch.setattr("pipeline.analyze_scenes", lambda scenes, frames, work_dir: [{
        "scene_id": 0,
        "start": 0.0,
        "end": 4.0,
        "description": "测试场景。",
    }])

    result = run_pipeline(video, output_dir=tmp_path / "out")
    work_dir = Path(result["work_dir"])
    settings = (work_dir / "run_settings.json").read_text(encoding="utf-8")

    assert result["status"] == "paused"
    assert '"burn_subtitles": true' in settings
    assert "--burn-subtitles" in result["resume_command"]


def test_resume_cli_burn_subtitles_overrides_saved_false(monkeypatch, tmp_path):
    import pipeline

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "run_settings.json").write_text('{"edit_mode":"full","burn_subtitles":false}')

    monkeypatch.setitem(CONFIG, "burn_subtitles", True)
    pipeline._merge_run_settings(work_dir)

    assert CONFIG["burn_subtitles"] is True


def test_resume_persisted_burn_subtitles_preflight_runs_after_load(monkeypatch, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "run_settings.json").write_text('{"edit_mode":"full","burn_subtitles":true}')

    monkeypatch.setitem(CONFIG, "burn_subtitles", False)
    monkeypatch.setattr("pipeline.check_prerequisites", lambda skip_asr=False: True)
    filter_checks = []
    monkeypatch.setattr("pipeline._ffmpeg_has_filter", lambda filter_name: filter_checks.append(filter_name) or False)

    with pytest.raises(RuntimeError, match="未启用 subtitles/libass"):
        run_pipeline(video, resume_dir=work_dir, step="assemble")

    assert filter_checks == ["subtitles"]


def test_cut_artifacts_rebuild_when_source_files_change(monkeypatch, tmp_path):
    import time
    from pipeline import _cut_artifacts_current

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    paths = {name: work_dir / name for name in [
        "clip_plan.json", "narration.json", "clip_plan_validated.json", "narration_mapped.json", "edited_source.mp4"
    ]}
    for path in paths.values():
        path.write_text("{}")
    monkeypatch.setitem(CONFIG, "edit_mode", "cut")

    assert _cut_artifacts_current(work_dir)

    time.sleep(0.01)
    paths["narration.json"].write_text("[]")
    assert not _cut_artifacts_current(work_dir)


def test_step_script_writes_narration_lint_and_stops_before_tts(monkeypatch, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    for step in ("extract", "detect", "asr", "silence", "vlm"):
        (work_dir / f".step_{step}.done").write_text("ok")
    (work_dir / "scenes.json").write_text('[{"scene_id":0,"start":0.0,"end":6.0,"description":"测试场景"}]')
    (work_dir / "asr_result.json").write_text('[]')
    (work_dir / "silence_periods.json").write_text('[{"start":0.0,"end":6.0,"duration":6.0,"has_speech":false}]')
    (work_dir / "vlm_analysis.json").write_text('[{"scene_id":0,"start":0.0,"end":6.0,"description":"测试场景"}]')
    (work_dir / "narration.json").write_text('[{"start":0.5,"end":5.5,"narration":"他终于意识到，沉默比争吵更伤人。"}]')

    monkeypatch.setitem(CONFIG, "api_key", "")
    monkeypatch.setitem(CONFIG, "edit_mode", "full")
    monkeypatch.setitem(CONFIG, "burn_subtitles", False)
    monkeypatch.setattr("pipeline.check_prerequisites", lambda skip_asr=False: True)
    monkeypatch.setattr("pipeline.get_video_duration", lambda path: 6.0)
    monkeypatch.setattr("pipeline.synthesize_tts", lambda narration, wd: (_ for _ in ()).throw(AssertionError("TTS should not run for --step script")))

    result = run_pipeline(video, resume_dir=work_dir, step="script")

    assert result["status"] == "script_validated"
    assert (work_dir / "narration_lint.json").exists()
    assert (work_dir / ".step_script.done").exists()
    assert not (work_dir / ".step_tts.done").exists()


def test_step_script_fails_on_lint_errors(monkeypatch, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    for step in ("extract", "detect", "asr", "silence", "vlm"):
        (work_dir / f".step_{step}.done").write_text("ok")
    (work_dir / "scenes.json").write_text('[{"scene_id":0,"start":0.0,"end":6.0,"description":"测试场景"}]')
    (work_dir / "asr_result.json").write_text('[]')
    (work_dir / "silence_periods.json").write_text('[]')
    (work_dir / "vlm_analysis.json").write_text('[{"scene_id":0,"start":0.0,"end":6.0,"description":"测试场景"}]')
    (work_dir / "narration.json").write_text('[{"start":0.5,"end":1.5,"narration":""}]')

    monkeypatch.setitem(CONFIG, "api_key", "")
    monkeypatch.setitem(CONFIG, "edit_mode", "full")
    monkeypatch.setitem(CONFIG, "burn_subtitles", False)
    monkeypatch.setattr("pipeline.check_prerequisites", lambda skip_asr=False: True)
    monkeypatch.setattr("pipeline.get_video_duration", lambda path: 6.0)

    with pytest.raises(ValueError, match="narration.json 预检失败"):
        run_pipeline(video, resume_dir=work_dir, step="script")

    assert (work_dir / "narration_lint.json").exists()
    assert not (work_dir / ".step_script.done").exists()


def test_resume_cut_mode_maps_narration_and_assembles_edited_source(monkeypatch, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    for step in ("extract", "detect", "asr", "silence", "vlm"):
        (work_dir / f".step_{step}.done").write_text("ok")
    (work_dir / "scenes.json").write_text('[{"start":0.0,"end":10.0}]')
    (work_dir / "asr_result.json").write_text('[]')
    (work_dir / "silence_periods.json").write_text('[{"start":0.0,"end":10.0,"duration":10.0,"has_speech":false}]')
    (work_dir / "vlm_analysis.json").write_text('[{"scene_id":0,"start":0.0,"end":10.0,"description":"测试场景"}]')
    (work_dir / "clip_plan.json").write_text('{"clips":[{"start":2.0,"end":6.0,"reason":"关键段落"}]}')
    (work_dir / "narration.json").write_text('[{"start":2.5,"end":5.5,"narration":"他终于意识到，沉默比争吵更伤人。"}]')

    monkeypatch.setitem(CONFIG, "api_key", "")
    monkeypatch.setitem(CONFIG, "fps", 1)
    monkeypatch.setitem(CONFIG, "burn_subtitles", False)
    monkeypatch.setitem(CONFIG, "edit_mode", "cut")
    monkeypatch.setitem(CONFIG, "target_duration", "4s")
    monkeypatch.setitem(CONFIG, "clip_padding", 0.0)
    monkeypatch.setitem(CONFIG, "allow_clip_overlap", False)
    monkeypatch.setattr("pipeline.check_prerequisites", lambda skip_asr=False: True)
    monkeypatch.setattr("pipeline.get_video_duration", lambda path: 10.0 if Path(path) == video else 4.0)
    monkeypatch.setattr("pipeline.api_call", lambda payload: (_ for _ in ()).throw(AssertionError("API should not be called")))

    edited_source_calls = []

    def fake_build_edited_source(video_path, validated_plan, wd, output_path=None):
        edited_source_calls.append((video_path, validated_plan))
        out = Path(output_path or wd / "edited_source.mp4")
        out.write_bytes(b"edited")
        return out

    assembled = []

    def fake_assemble(video_path, tts_segments, wd, output_path):
        assembled.append((Path(video_path), tts_segments))
        output_path.write_bytes(b"mp4")
        return output_path

    monkeypatch.setattr("pipeline.build_edited_source_video", fake_build_edited_source)
    monkeypatch.setattr("pipeline.synthesize_tts", lambda narration, wd: ([{
        "index": 0,
        "start": narration[0]["start"],
        "end": narration[0]["end"],
        "source_start": narration[0]["source_start"],
        "source_end": narration[0]["source_end"],
        "narration": narration[0]["narration"],
        "audio_path": str(wd / "narr_000.wav"),
        "audio_duration": 1.0,
    }], "say"))
    monkeypatch.setattr("pipeline.assemble_video", fake_assemble)

    result = run_pipeline(video, resume_dir=work_dir)

    mapped = (work_dir / "narration_mapped.json").read_text(encoding="utf-8")
    assert result["edit_mode"] == "cut"
    assert result["edited_duration"] == 4.0
    assert edited_source_calls
    assert assembled[0][0] == work_dir / "edited_source.mp4"
    assert '"start": 0.5' in mapped
    assert '"source_start": 2.5' in mapped
    assert (work_dir / "clip_plan_validated.json").exists()
    assert (work_dir / "output.mp4").exists()


def test_step_assemble_rebuilds_stale_tts_after_mapped_narration_changes(monkeypatch, tmp_path):
    import time

    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    edited = work_dir / "edited_source.mp4"
    edited.write_bytes(b"edited")
    mapped = work_dir / "narration_mapped.json"
    mapped.write_text('[{"start":0.0,"end":2.0,"narration":"新稿。"}]')
    meta = work_dir / "tts_meta.json"
    meta.write_text('{"segments":[],"engine":"old"}')
    (work_dir / "run_settings.json").write_text('{"edit_mode":"cut"}')
    (work_dir / "clip_plan.json").write_text('{"clips":[{"start":0,"end":2}]}')
    (work_dir / "narration.json").write_text('[{"start":0,"end":2,"narration":"更新后的稿。"}]')
    (work_dir / "clip_plan_validated.json").write_text('{"clips":[{"clip_id":0,"source_start":0,"source_end":2,"output_start":0,"output_end":2,"duration":2}],"total_duration":2}')
    (work_dir / ".step_edit.done").write_text("ok")
    (work_dir / ".step_tts.done").write_text("ok")
    time.sleep(0.01)
    mapped.write_text('[{"start":0.0,"end":2.0,"narration":"更新后的稿。"}]')

    monkeypatch.setitem(CONFIG, "edit_mode", "full")
    monkeypatch.setitem(CONFIG, "burn_subtitles", False)
    monkeypatch.setattr("pipeline.check_prerequisites", lambda skip_asr=False: True)
    monkeypatch.setattr("pipeline.get_video_duration", lambda path: 2.0)
    monkeypatch.setattr("pipeline.build_edited_source_video", lambda video_path, plan, wd, output_path=None: Path(output_path or wd / "edited_source.mp4").write_bytes(b"edited") or Path(output_path or wd / "edited_source.mp4"))
    calls = []
    monkeypatch.setattr("pipeline.synthesize_tts", lambda narration, wd: calls.append(narration) or ([{"index":0,"start":0,"end":2,"narration":narration[0]["narration"],"audio_path":str(wd / "n.wav"),"audio_duration":1}], "say"))
    monkeypatch.setattr("pipeline.assemble_video", lambda video_path, tts_segments, wd, output_path: output_path.write_bytes(b"mp4"))

    run_pipeline(video, resume_dir=work_dir, step="assemble")

    assert calls and calls[0][0]["narration"] == "更新后的稿。"


def test_step_assemble_rebuilds_when_assemble_settings_change(monkeypatch, tmp_path):
    import pipeline

    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    output = work_dir / "output.mp4"
    output.write_bytes(b"old")
    (work_dir / ".step_assemble.done").write_text("ok")
    tts_meta = work_dir / "tts_meta.json"
    tts_meta.write_text('{"segments":[{"start":0,"end":2,"narration":"旧设置。","audio_path":"n.wav","audio_duration":1}],"engine":"say"}')

    monkeypatch.setitem(CONFIG, "burn_subtitles", False)
    pipeline._write_assemble_meta(work_dir, video)
    monkeypatch.setitem(CONFIG, "burn_subtitles", True)
    monkeypatch.setattr("pipeline.check_prerequisites", lambda skip_asr=False: True)
    monkeypatch.setattr("pipeline._ffmpeg_has_filter", lambda filter_name: True)
    monkeypatch.setattr("pipeline.get_video_duration", lambda path: 2.0)
    calls = []
    monkeypatch.setattr("pipeline.assemble_video", lambda video_path, tts_segments, wd, output_path: calls.append(video_path) or output_path.write_bytes(b"new"))

    run_pipeline(video, resume_dir=work_dir, step="assemble")

    assert calls == [video]
    assert _assemble_settings_current(work_dir, video)


def test_full_pipeline_reassembles_when_burn_setting_changes(monkeypatch, tmp_path):
    import pipeline

    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    for step in ("extract", "detect", "asr", "silence", "vlm", "script", "tts", "assemble"):
        (work_dir / f".step_{step}.done").write_text("ok")
    (work_dir / "scenes.json").write_text('[{"start":0.0,"end":3.0}]')
    (work_dir / "asr_result.json").write_text('[]')
    (work_dir / "silence_periods.json").write_text('[{"start":0.0,"end":3.0,"duration":3.0,"has_speech":false}]')
    (work_dir / "vlm_analysis.json").write_text('[{"scene_id":0,"start":0.0,"end":3.0,"description":"测试"}]')
    (work_dir / "narration.json").write_text('[{"start":0.0,"end":2.0,"narration":"测试。"}]')
    (work_dir / "tts_meta.json").write_text('{"segments":[{"start":0,"end":2,"narration":"测试。","audio_path":"n.wav","audio_duration":1}],"engine":"say"}')
    output = work_dir / "output.mp4"
    output.write_bytes(b"old")

    monkeypatch.setitem(CONFIG, "api_key", "")
    monkeypatch.setitem(CONFIG, "edit_mode", "full")
    monkeypatch.setitem(CONFIG, "burn_subtitles", False)
    pipeline._write_assemble_meta(work_dir, video)
    monkeypatch.setitem(CONFIG, "burn_subtitles", True)
    monkeypatch.setattr("pipeline.check_prerequisites", lambda skip_asr=False: True)
    monkeypatch.setattr("pipeline._ffmpeg_has_filter", lambda filter_name: True)
    monkeypatch.setattr("pipeline.get_video_duration", lambda path: 3.0)
    calls = []
    monkeypatch.setattr("pipeline.assemble_video", lambda video_path, tts_segments, wd, output_path: calls.append(video_path) or output_path.write_bytes(b"new"))

    run_pipeline(video, resume_dir=work_dir)

    assert calls == [video]
    assert _assemble_settings_current(work_dir, video)


def test_build_edited_source_video_uses_ffmpeg_concat(monkeypatch, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    plan = normalize_clip_plan([{"start": 0, "end": 1}, {"start": 2, "end": 3}], video_duration=4)
    commands = []

    def fake_run_cmd(cmd):
        commands.append(cmd)
        if cmd[0] == "ffprobe":
            return CompletedProcess(cmd, 0, stdout="0\n", stderr="")
        out = Path(cmd[-1])
        out.write_bytes(b"mp4")
        return CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("edit.run_cmd", fake_run_cmd)
    monkeypatch.setattr("edit.get_video_duration", lambda path: 2.0)

    output = build_edited_source_video(video, plan, work_dir)

    assert output.exists()
    ffmpeg_cmd = [cmd for cmd in commands if cmd[0] == "ffmpeg"][0]
    assert "trim=start=0.000:end=1.000" in " ".join(ffmpeg_cmd)
    assert "concat=n=2" in " ".join(ffmpeg_cmd)
