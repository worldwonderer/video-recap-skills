"""Pure function unit tests for video-recap modules."""
import json
import sys
from pathlib import Path
from subprocess import CompletedProcess

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills' / 'video-recap' / 'scripts'))

from common import _api_headers, _prepare_api_payload, _provider_uses_mimo, _retry_after_seconds, get_video_duration
from config import CONFIG, default_mimo_api_url, env_bool, env_float, env_int, normalize_api_url
from detect import detect_scenes
from extract import extract_frames
from pipeline import (
    _annotate_cut_narration_overlap,
    _assemble_settings_current,
    _command_available,
    _ffmpeg_has_filter,
    _load_run_settings,
    _mimo_video_overview_current,
    _tts_meta_current,
    run_pipeline,
)
from edit import (
    build_edited_source_video,
    map_narration_to_clips,
    normalize_clip_plan,
    parse_duration_seconds,
    source_time_to_output_time,
)
from narration import _align_narration_to_quiet, _text_char_count, build_agent_brief, lint_narration
from tts import (
    _build_tts_segment_result,
    _detect_tts_engine,
    _parse_rate_offset,
    _run_tts_engine,
    _tts_mimo,
    resolve_tts_engine,
    synthesize_tts,
    tts_settings_fingerprint,
)
from video_recap import _apply_api_provider
from assemble import (
    _build_timed_narration,
    _escape_ass_text,
    _generate_ass,
    _generate_srt,
    _seconds_to_ass_time,
    _seconds_to_srt_time,
    _subtitle_burn_filter,
    assembly_settings_fingerprint,
    assemble_video,
)
from vlm import _mimo_video_chunks, _video_data_url, analyze_scenes, analyze_video_overview


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


def test_lint_narration_warns_when_segment_spans_too_many_visual_beats(monkeypatch):
    monkeypatch.setitem(CONFIG, "visual_beat_max_seconds", 10.0)
    monkeypatch.setitem(CONFIG, "visual_beat_max_facts", 2)
    report = lint_narration([
        {"start": 0.0, "end": 20.0, "narration": "这段长解说跨过太多画面锚点，应该拆开。"},
    ], [{
        "scene_id": 0,
        "start": 0.0,
        "end": 25.0,
        "frame_facts": {
            "1.0": ["人物走入房间"],
            "6.0": ["人物坐下"],
            "12.0": ["人物起身争执"],
            "18.0": ["镜头切到门外"],
        },
    }])

    codes = {issue["code"] for issue in report["warnings"]}
    assert "visual_beat_too_broad" in codes


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


def test_mimo_token_plan_key_defaults_to_token_plan_cn_base():
    assert default_mimo_api_url("tp-example") == "https://token-plan-cn.xiaomimimo.com/v1"
    assert default_mimo_api_url("tp-example", cluster="sgp") == "https://token-plan-sgp.xiaomimimo.com/v1"
    assert default_mimo_api_url("tp-example", cluster="unknown") == "https://token-plan-cn.xiaomimimo.com/v1"
    assert default_mimo_api_url("sk-example") == "https://api.xiaomimimo.com/v1"


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


def test_align_narration_to_quiet_requires_enough_overlap_and_limits_shift(monkeypatch):
    scenes = [{"scene_id": 0, "start": 0.0, "end": 12.0}]
    monkeypatch.setitem(CONFIG, "quiet_overlap_min_ratio", 0.8)
    monkeypatch.setitem(CONFIG, "max_quiet_shift_seconds", 3.0)

    near = _align_narration_to_quiet([
        {"start": 0.0, "end": 4.0, "narration": "可以贴近稍后的安静画面。"},
    ], scenes, [{"start": 2.0, "end": 6.0, "duration": 4.0, "has_speech": False}])
    assert near[0]["start"] == 2.0
    assert near[0]["end"] == 6.0
    assert near[0]["overlaps_speech"] is False

    far = _align_narration_to_quiet([
        {"start": 0.0, "end": 4.0, "narration": "不要漂远画面。"},
    ], scenes, [{"start": 5.0, "end": 9.0, "duration": 4.0, "has_speech": False}])
    assert far[0]["start"] == 0.0
    assert far[0]["overlaps_speech"] is True


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
    monkeypatch.setitem(__import__("config").CONFIG, "tts_engine", "edge-tts")
    segments, engine = synthesize_tts([], tmp_path)

    assert segments == []
    assert engine == "edge-tts"


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

    monkeypatch.setitem(CONFIG, "tts_engine", "edge-tts")
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

    monkeypatch.setitem(CONFIG, "tts_engine", "edge-tts")
    monkeypatch.setitem(CONFIG, "tts_workers", 2)
    monkeypatch.setitem(CONFIG, "allow_partial_tts", True)
    monkeypatch.setattr("tts._synthesize_segment", fake_synthesize_segment)

    segments, engine = synthesize_tts(narration, tmp_path)

    assert engine == "edge-tts"
    assert [s["index"] for s in segments] == [0]


def test_detect_tts_engine_prefers_edge_tts(monkeypatch):
    monkeypatch.setattr("tts.shutil.which", lambda cmd: "/usr/bin/edge-tts" if cmd == "edge-tts" else None)

    assert _detect_tts_engine() == "edge-tts"


def test_resolve_tts_engine_prefers_mimo_in_auto_and_allows_explicit_edge(monkeypatch):
    monkeypatch.setitem(CONFIG, "tts_engine", "auto")
    monkeypatch.setitem(CONFIG, "mimo_tts_api_key", "mimo-secret")
    monkeypatch.setattr("tts.shutil.which", lambda cmd: "/usr/bin/edge-tts" if cmd == "edge-tts" else None)

    assert resolve_tts_engine() == "mimo-tts"

    monkeypatch.setitem(CONFIG, "tts_engine", "edge-tts")
    assert resolve_tts_engine() == "edge-tts"


def test_tts_meta_current_rejects_edge_cache_when_mimo_is_now_preferred(monkeypatch, tmp_path):
    narration = tmp_path / "narration.json"
    narration.write_text('[{"start":0,"end":2,"narration":"测试。"}]', encoding="utf-8")
    meta = tmp_path / "tts_meta.json"
    meta.write_text('{"segments":[],"engine":"edge-tts"}', encoding="utf-8")

    monkeypatch.setitem(CONFIG, "tts_engine", "auto")
    monkeypatch.setitem(CONFIG, "mimo_tts_api_key", "mimo-secret")
    monkeypatch.setattr("tts.shutil.which", lambda cmd: "/usr/bin/edge-tts" if cmd == "edge-tts" else None)

    assert _tts_meta_current(meta, narration) is False

    monkeypatch.setitem(CONFIG, "tts_engine", "edge-tts")
    assert _tts_meta_current(meta, narration) is True


def test_tts_meta_current_rejects_mimo_setting_changes(monkeypatch, tmp_path):
    narration = tmp_path / "narration.json"
    narration.write_text('[{"start":0,"end":2,"narration":"测试。"}]', encoding="utf-8")
    meta = tmp_path / "tts_meta.json"

    monkeypatch.setitem(CONFIG, "tts_engine", "auto")
    monkeypatch.setitem(CONFIG, "mimo_tts_api_key", "mimo-secret")
    monkeypatch.setitem(CONFIG, "mimo_tts_api_url", "https://token-plan-cn.xiaomimimo.com/v1/chat/completions")
    monkeypatch.setitem(CONFIG, "mimo_tts_model", "mimo-v2.5-tts")
    monkeypatch.setitem(CONFIG, "mimo_tts_voice", "冰糖")
    monkeypatch.setitem(CONFIG, "mimo_tts_style", "自然中文解说")
    meta.write_text(
        json.dumps({
            "segments": [],
            "engine": "mimo-tts",
            "settings": tts_settings_fingerprint("mimo-tts"),
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    assert _tts_meta_current(meta, narration) is True

    monkeypatch.setitem(CONFIG, "mimo_tts_voice", "茉莉")
    assert _tts_meta_current(meta, narration) is False


def test_tts_meta_current_rejects_unsupported_cached_engine_when_no_engine(monkeypatch, tmp_path):
    narration = tmp_path / "narration.json"
    narration.write_text('[{"start":0,"end":2,"narration":"测试。"}]', encoding="utf-8")
    meta = tmp_path / "tts_meta.json"
    meta.write_text('{"segments":[],"engine":"say"}', encoding="utf-8")

    monkeypatch.setitem(CONFIG, "tts_engine", "auto")
    monkeypatch.setitem(CONFIG, "mimo_tts_api_key", "")
    monkeypatch.setattr("tts.shutil.which", lambda cmd: None)

    assert _tts_meta_current(meta, narration) is False


def test_detect_tts_engine_does_not_fallback_to_removed_say(monkeypatch):
    monkeypatch.setitem(CONFIG, "api_provider", "openai")
    monkeypatch.setitem(CONFIG, "api_url", "https://api.openai.com/v1/chat/completions")
    monkeypatch.setitem(CONFIG, "api_key", "")
    monkeypatch.setattr("tts.shutil.which", lambda cmd: "/usr/bin/say" if cmd == "say" else None)

    with pytest.raises(RuntimeError, match="edge-tts|MiMo"):
        _detect_tts_engine()


def test_run_tts_engine_supports_mimo_branch(monkeypatch, tmp_path):
    import base64

    def fake_api_call(payload):
        return {"choices": [{"message": {"audio": {"data": base64.b64encode(b"wav").decode("ascii")}}}]}

    output = tmp_path / "out.wav"
    monkeypatch.setitem(CONFIG, "tts_retries", 1)
    monkeypatch.setattr("tts.mimo_tts_api_call", fake_api_call)
    monkeypatch.setattr("tts._get_audio_duration", lambda path: 1.0)

    _run_tts_engine("mimo-tts", "这是小米 MiMo 配音。", output)

    assert output.read_bytes() == b"wav"


def test_run_tts_engine_rejects_removed_engines(monkeypatch, tmp_path):
    monkeypatch.setitem(CONFIG, "tts_retries", 1)
    monkeypatch.setattr("tts._get_audio_duration", lambda path: 0.0)

    with pytest.raises(RuntimeError, match="不支持的 TTS 引擎"):
        _run_tts_engine("say", "测试。", tmp_path / "out.wav")


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
    }], "edge-tts"))

    result = run_pipeline(video, step="tts", resume_dir=work_dir)

    assert result["engine"] == "edge-tts"
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
    monkeypatch.setitem(CONFIG, "skip_narrative_analysis", True)
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
    monkeypatch.setitem(CONFIG, "skip_narrative_analysis", True)
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
    }], "edge-tts"))
    monkeypatch.setattr("pipeline.assemble_video", lambda video_path, tts_segments, wd, output_path: output_path.write_bytes(b"mp4"))

    result = run_pipeline(video, resume_dir=work_dir)

    assert result["tts_engine"] == "edge-tts"
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


def test_run_settings_do_not_override_explicit_runtime_options(monkeypatch, tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "run_settings.json").write_text(json.dumps({
        "api_provider": "openai",
        "api_url": "https://old.example/v1/chat/completions",
        "vlm_model": "old-model",
        "mimo_tts_voice": "旧音色",
        "mimo_tts_style": "旧风格",
        "mimo_video_overview": False,
        "edit_mode": "cut",
    }), encoding="utf-8")

    monkeypatch.setitem(CONFIG, "api_provider", "mimo")
    monkeypatch.setitem(CONFIG, "api_provider_source", "cli")
    monkeypatch.setitem(CONFIG, "api_url", "https://api.xiaomimimo.com/v1/chat/completions")
    monkeypatch.setitem(CONFIG, "api_url_source", "cli")
    monkeypatch.setitem(CONFIG, "vlm_model", "mimo-v2.5")
    monkeypatch.setitem(CONFIG, "vlm_model_source", "cli")
    monkeypatch.setitem(CONFIG, "mimo_tts_voice", "冰糖")
    monkeypatch.setitem(CONFIG, "mimo_tts_voice_source", "cli")
    monkeypatch.setitem(CONFIG, "mimo_tts_style", "新风格")
    monkeypatch.setitem(CONFIG, "mimo_tts_style_source", "env")
    monkeypatch.setitem(CONFIG, "mimo_video_overview", True)
    monkeypatch.setitem(CONFIG, "mimo_video_overview_source", "cli")

    _load_run_settings(work_dir)

    assert CONFIG["api_provider"] == "mimo"
    assert CONFIG["api_url"] == "https://api.xiaomimimo.com/v1/chat/completions"
    assert CONFIG["vlm_model"] == "mimo-v2.5"
    assert CONFIG["mimo_tts_voice"] == "冰糖"
    assert CONFIG["mimo_tts_style"] == "新风格"
    assert CONFIG["mimo_video_overview"] is True
    assert CONFIG["edit_mode"] == "cut"


def test_full_pipeline_cut_mode_pauses_for_clip_plan_and_narration(monkeypatch, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake")
    output = tmp_path / "out"

    monkeypatch.setitem(CONFIG, "api_key", "test-key")
    monkeypatch.setitem(CONFIG, "fps", 1)
    monkeypatch.setitem(CONFIG, "burn_subtitles", False)
    monkeypatch.setitem(CONFIG, "skip_narrative_analysis", True)
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
    monkeypatch.setitem(CONFIG, "skip_narrative_analysis", True)
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
    monkeypatch.setitem(CONFIG, "skip_narrative_analysis", True)
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
    monkeypatch.setitem(CONFIG, "skip_narrative_analysis", True)
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
    monkeypatch.setitem(CONFIG, "skip_narrative_analysis", True)
    monkeypatch.setitem(CONFIG, "edit_mode", "cut")
    monkeypatch.setitem(CONFIG, "target_duration", "4s")
    monkeypatch.setitem(CONFIG, "clip_padding", 0.0)
    monkeypatch.setitem(CONFIG, "allow_clip_overlap", False)
    monkeypatch.setattr("pipeline.check_prerequisites", lambda skip_asr=False: True)
    monkeypatch.setattr("pipeline.get_video_duration", lambda path: 10.0 if Path(path) == video else 4.0)
    monkeypatch.setattr("pipeline._align_narration_to_quiet", lambda narration, scenes, silence: narration)
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
    }], "edge-tts"))
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
    monkeypatch.setattr("pipeline.synthesize_tts", lambda narration, wd: calls.append(narration) or ([{"index":0,"start":0,"end":2,"narration":narration[0]["narration"],"audio_path":str(wd / "n.wav"),"audio_duration":1}], "edge-tts"))
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
    tts_meta.write_text('{"segments":[{"start":0,"end":2,"narration":"旧设置。","audio_path":"n.wav","audio_duration":1}],"engine":"edge-tts"}')

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
    (work_dir / "tts_meta.json").write_text('{"segments":[{"start":0,"end":2,"narration":"测试。","audio_path":"n.wav","audio_duration":1}],"engine":"edge-tts"}')
    output = work_dir / "output.mp4"
    output.write_bytes(b"old")

    monkeypatch.setitem(CONFIG, "api_key", "")
    monkeypatch.setitem(CONFIG, "edit_mode", "full")
    monkeypatch.setitem(CONFIG, "skip_narrative_analysis", True)
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


def test_mimo_api_headers_and_payload_mapping(monkeypatch):
    monkeypatch.setitem(CONFIG, "api_provider", "mimo")
    monkeypatch.setitem(CONFIG, "api_url", "https://api.xiaomimimo.com/v1/chat/completions")
    monkeypatch.setitem(CONFIG, "api_key", "secret")

    assert _provider_uses_mimo() is True
    headers = _api_headers()
    assert headers["api-key"] == "secret"
    assert "Authorization" not in headers
    payload = _prepare_api_payload({"model": "mimo-v2.5", "max_tokens": 7})
    assert payload["max_completion_tokens"] == 7
    assert payload["thinking"] == {"type": "disabled"}
    assert "max_tokens" not in payload

    tts_payload = _prepare_api_payload({"model": "mimo-v2.5-tts", "max_tokens": 7})
    assert tts_payload["max_completion_tokens"] == 7
    assert "thinking" not in tts_payload

    monkeypatch.setitem(CONFIG, "api_provider", "openai")
    monkeypatch.setitem(CONFIG, "api_url", "https://api.openai.com/v1/chat/completions")
    headers = _api_headers()
    assert headers["Authorization"] == "Bearer secret"
    assert "api-key" not in headers
    assert _prepare_api_payload({"max_tokens": 7})["max_tokens"] == 7


def test_cli_api_provider_mimo_uses_mimo_key_with_openai_config(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("OPENAI_API_URL", "https://openai.example/v1")
    monkeypatch.setenv("OPENAI_MODEL", "openai-model")
    monkeypatch.setitem(CONFIG, "api_key", "openai-secret")
    monkeypatch.setitem(CONFIG, "api_key_source", "OPENAI_API_KEY")
    monkeypatch.setitem(CONFIG, "api_url", "https://openai.example/v1/chat/completions")
    monkeypatch.setitem(CONFIG, "api_provider", "openai")
    monkeypatch.setitem(CONFIG, "mimo_api_key", "mimo-secret")
    monkeypatch.setitem(CONFIG, "mimo_api_key_source", "MIMO_API_KEY")
    monkeypatch.setitem(CONFIG, "mimo_api_url", "https://api.xiaomimimo.com/v1/chat/completions")
    monkeypatch.setitem(CONFIG, "mimo_model", "mimo-v2.5")

    _apply_api_provider("mimo")

    assert CONFIG["api_provider"] == "mimo"
    assert CONFIG["api_key"] == "mimo-secret"
    assert CONFIG["api_key_source"] == "MIMO_API_KEY"
    assert CONFIG["api_url"] == "https://api.xiaomimimo.com/v1/chat/completions"
    assert CONFIG["vlm_model"] == "mimo-v2.5"

    _apply_api_provider("openai")

    assert CONFIG["api_provider"] == "openai"
    assert CONFIG["api_key"] == "openai-secret"
    assert CONFIG["api_url"] == "https://openai.example/v1/chat/completions"
    assert CONFIG["vlm_model"] == "openai-model"


def test_mimo_tts_writes_decoded_audio(monkeypatch, tmp_path):
    import base64

    seen_payloads = []

    def fake_api_call(payload):
        seen_payloads.append(payload)
        return {"choices": [{"message": {"audio": {"data": base64.b64encode(b"wav-bytes").decode("ascii")}}}]}

    monkeypatch.setitem(CONFIG, "mimo_tts_model", "mimo-v2.5-tts")
    monkeypatch.setitem(CONFIG, "mimo_tts_voice", "冰糖")
    monkeypatch.setattr("tts.mimo_tts_api_call", fake_api_call)

    output = tmp_path / "out.wav"
    _tts_mimo("这是小米 MiMo 配音。", output, rate="-5%", pitch="+0Hz")

    assert output.read_bytes() == b"wav-bytes"
    payload = seen_payloads[0]
    assert payload["model"] == "mimo-v2.5-tts"
    assert payload["audio"] == {"format": "wav", "voice": "冰糖"}
    assert payload["messages"][1] == {"role": "assistant", "content": "这是小米 MiMo 配音。"}
    assert "语速略慢" in payload["messages"][0]["content"]


def test_detect_tts_engine_prefers_mimo_when_provider_configured(monkeypatch):
    monkeypatch.setitem(CONFIG, "api_provider", "openai")
    monkeypatch.setitem(CONFIG, "api_url", "https://example.com/v1/chat/completions")
    monkeypatch.setitem(CONFIG, "api_key", "doubao-secret")
    monkeypatch.setitem(CONFIG, "mimo_tts_api_key", "mimo-secret")
    monkeypatch.setattr("tts.shutil.which", lambda cmd: "/usr/bin/edge-tts" if cmd == "edge-tts" else None)

    assert _detect_tts_engine() == "mimo-tts"


def test_mimo_video_overview_uses_scene_chunks(monkeypatch, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake-video")
    seen_payloads = []

    def fake_api_call(payload):
        seen_payloads.append(payload)
        return {
            "model": "mimo-v2.5",
            "choices": [{"message": {"content": "分片概览", "reasoning_content": "推理内容"}}],
            "usage": {"prompt_tokens_details": {"video_tokens": 12}},
        }

    def fake_extract(video_path, chunk, output_path):
        output_path.write_bytes(b"tiny-chunk")
        return output_path

    monkeypatch.setitem(CONFIG, "api_provider", "openai")
    monkeypatch.setitem(CONFIG, "vlm_model", "doubao-seed-2-0-lite-260428")
    monkeypatch.setitem(CONFIG, "mimo_video_api_key", "mimo-secret")
    monkeypatch.setitem(CONFIG, "mimo_video_model", "mimo-v2.5")
    monkeypatch.setitem(CONFIG, "mimo_video_overview", True)
    monkeypatch.setitem(CONFIG, "mimo_video_fps", 2)
    monkeypatch.setitem(CONFIG, "mimo_video_chunk_max_seconds", 2)
    monkeypatch.setitem(CONFIG, "mimo_video_chunk_min_seconds", 0.5)
    monkeypatch.setitem(CONFIG, "mimo_media_resolution", "max")
    monkeypatch.setattr("vlm._extract_video_chunk", fake_extract)
    monkeypatch.setattr("vlm.mimo_video_api_call", fake_api_call)

    overview = analyze_video_overview(video, tmp_path, [{"scene_id": 7, "start": 0.0, "end": 5.0}])

    assert overview["input"] == "scene_chunks"
    assert overview["chunk_count"] == 3
    assert overview["model"] == "mimo-v2.5"
    assert len(seen_payloads) == 3
    for payload in seen_payloads:
        video_part = payload["messages"][0]["content"][0]
        assert payload["model"] == "mimo-v2.5"
        assert payload["max_tokens"] == 1200
        assert video_part["type"] == "video_url"
        assert video_part["video_url"]["url"].startswith("data:video/mp4;base64,")
        assert video_part["fps"] == 2
        assert video_part["media_resolution"] == "max"
    assert "分片概览" in overview["content"]
    assert overview["settings"]["mimo_video_chunk_max_seconds"] == 2
    assert all(not Path(item["clip_path"]).is_absolute() for item in overview["chunks"])
    assert all(item["clip_path"].startswith("mimo_video_chunks/") for item in overview["chunks"])
    assert (tmp_path / "mimo_video_overview.json").exists()


def test_mimo_video_chunks_split_on_scene_boundaries(monkeypatch):
    monkeypatch.setitem(CONFIG, "mimo_video_chunk_max_seconds", 2)
    monkeypatch.setitem(CONFIG, "mimo_video_chunk_min_seconds", 0.5)

    chunks = _mimo_video_chunks([{"scene_id": 3, "start": 0.0, "end": 5.0}])

    assert [(c["scene_id"], c["start"], c["end"]) for c in chunks] == [
        (3, 0.0, 2.0),
        (3, 2.0, 4.0),
        (3, 4.0, 5.0),
    ]


def test_mimo_video_overview_current_rejects_old_artifacts(tmp_path):
    (tmp_path / ".step_mimo_video_overview.done").write_text("ok")
    (tmp_path / "mimo_video_overview.json").write_text('{"input":"base64","content":"old"}', encoding="utf-8")
    assert _mimo_video_overview_current(tmp_path) is False

    (tmp_path / "mimo_video_overview.json").write_text(
        json.dumps({
            "input": "scene_chunks",
            "chunks": [{"chunk_id": 0}],
            "settings": {
                "model": CONFIG.get("mimo_video_model") or CONFIG.get("mimo_model") or CONFIG.get("vlm_model"),
                "mimo_video_fps": CONFIG.get("mimo_video_fps", 2.0),
                "mimo_media_resolution": CONFIG.get("mimo_media_resolution", "default"),
                "mimo_video_chunk_max_seconds": CONFIG.get("mimo_video_chunk_max_seconds", 20.0),
                "mimo_video_chunk_min_seconds": CONFIG.get("mimo_video_chunk_min_seconds", 1.0),
                "mimo_video_base64_max_mb": CONFIG.get("mimo_video_base64_max_mb", 45.0),
                "mimo_video_prompt": CONFIG.get("mimo_video_prompt", ""),
                "mimo_disable_thinking": CONFIG.get("mimo_disable_thinking", True),
            },
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    assert _mimo_video_overview_current(tmp_path) is True

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setitem(CONFIG, "mimo_video_chunk_max_seconds", 99)
        assert _mimo_video_overview_current(tmp_path) is False
    finally:
        monkeypatch.undo()


def test_mimo_video_overview_embeds_small_local_chunk(monkeypatch, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"tiny")
    monkeypatch.setitem(CONFIG, "mimo_video_base64_max_mb", 1)

    data_url = _video_data_url(video)

    assert data_url.startswith("data:video/mp4;base64,")


def test_agent_brief_includes_mimo_video_overview(monkeypatch, tmp_path):
    (tmp_path / "mimo_video_overview.json").write_text(
        '{"input":"scene_chunks","content":"这是 MiMo 对分片汇总的故事线概览。","reasoning_content":"内部推理"}',
        encoding="utf-8",
    )
    monkeypatch.setitem(CONFIG, "edit_mode", "full")
    monkeypatch.setitem(CONFIG, "target_duration", "")
    monkeypatch.setitem(CONFIG, "context_info", "")

    brief = build_agent_brief(
        [{"scene_id": 0, "start": 0.0, "end": 3.0, "description": "场景"}],
        [],
        [],
        3.0,
        tmp_path,
    )

    text = brief.read_text(encoding="utf-8")
    assert "MiMo scene-chunk video overview" in text
    assert "这是 MiMo 对分片汇总的故事线概览。" in text
    assert "内部推理" not in text
