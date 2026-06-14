import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'video-cut' / 'scripts'))
import pytest  # noqa: F401
from subprocess import CompletedProcess  # noqa: F401
from cut import build_edited_source_video, map_narration_to_clips, normalize_clip_plan, parse_duration_seconds, source_time_to_output_time


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


def test_parse_duration_seconds_accepts_compound_unit_forms():
    # Natural compound durations must not crash the cut pipeline (regression: "2m30s").
    assert parse_duration_seconds("2m30s") == 150
    assert parse_duration_seconds("1h5m") == 3900
    assert parse_duration_seconds("1h5m30s") == 3930
    assert parse_duration_seconds("500ms") == 0.5
    assert parse_duration_seconds("90s") == 90
    with pytest.raises(ValueError):
        parse_duration_seconds("2m30x")   # trailing junk
    with pytest.raises(ValueError):
        parse_duration_seconds("m30")     # missing leading number


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

    monkeypatch.setattr("cut.run_cmd", fake_run_cmd)
    monkeypatch.setattr("cut.get_video_duration", lambda path: 2.0)

    output = build_edited_source_video(video, plan, work_dir)

    assert output.exists()
    ffmpeg_cmd = [cmd for cmd in commands if cmd[0] == "ffmpeg"][0]
    assert "trim=start=0.000:end=1.000" in " ".join(ffmpeg_cmd)
    assert "concat=n=2" in " ".join(ffmpeg_cmd)




def test_cut_source_fingerprint_detects_middle_only_changes(tmp_path):
    import cut

    first = tmp_path / "a.mp4"
    second = tmp_path / "b.mp4"
    first.write_bytes(b"A" * 70000 + b"middle-one" + b"Z" * 70000)
    second.write_bytes(b"A" * 70000 + b"middle-two" + b"Z" * 70000)

    assert first.stat().st_size == second.stat().st_size
    assert cut.file_fingerprint(first) != cut.file_fingerprint(second)


def test_cut_main_does_not_reuse_edited_source_when_normalized_plan_changes(monkeypatch, tmp_path):
    import json
    import sys
    import cut

    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    work = tmp_path / "work"
    work.mkdir()
    (work / "clip_plan.json").write_text(json.dumps([{"start": 10.0, "end": 20.0}]), encoding="utf-8")
    edited = work / "edited_source.mp4"
    edited.write_bytes(b"old-edited")

    calls = []

    def fake_build(video_path, validated_plan, work_dir, output_path=None):
        calls.append(validated_plan)
        Path(output_path).write_bytes(b"new-edited")
        cut._write_edited_source_meta(output_path, validated_plan, video_path)
        return Path(output_path)

    monkeypatch.setattr("cut.get_video_duration", lambda path: 100.0)
    monkeypatch.setattr("cut.build_edited_source_video", fake_build)
    monkeypatch.setattr(sys, "argv", [
        "cut.py", str(video), "--work-dir", str(work), "--clip-padding", "5",
    ])

    cut.main()

    assert len(calls) == 1
    assert json.loads((work / "clip_plan_validated.json").read_text(encoding="utf-8"))["clips"][0]["source_start"] == 5.0
    assert edited.read_bytes() == b"new-edited"


def test_cut_main_reuses_edited_source_when_fingerprint_matches(monkeypatch, tmp_path):
    import json
    import sys
    import cut

    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    work = tmp_path / "work"
    work.mkdir()
    raw_plan = [{"start": 10.0, "end": 20.0}]
    (work / "clip_plan.json").write_text(json.dumps(raw_plan), encoding="utf-8")
    validated = cut.normalize_clip_plan(raw_plan, video_duration=100.0)
    edited = work / "edited_source.mp4"
    edited.write_bytes(b"cached-edited")
    cut._write_edited_source_meta(edited, validated, video)

    def boom(*args, **kwargs):
        raise AssertionError("matching edited_source cache should be reused")

    monkeypatch.setattr("cut.get_video_duration", lambda path: 100.0)
    monkeypatch.setattr("cut.build_edited_source_video", boom)
    monkeypatch.setattr(sys, "argv", ["cut.py", str(video), "--work-dir", str(work)])

    cut.main()

    assert edited.read_bytes() == b"cached-edited"


def test_cut_main_rebuilds_when_source_video_bytes_change(monkeypatch, tmp_path):
    import json
    import sys
    import cut

    old_video = tmp_path / "video_old.mp4"
    new_video = tmp_path / "video_new.mp4"
    old_video.write_bytes(b"old source bytes")
    new_video.write_bytes(b"new source bytes")
    work = tmp_path / "work"
    work.mkdir()
    raw_plan = [{"start": 10.0, "end": 20.0}]
    (work / "clip_plan.json").write_text(json.dumps(raw_plan), encoding="utf-8")
    validated = cut.normalize_clip_plan(raw_plan, video_duration=100.0)
    edited = work / "edited_source.mp4"
    edited.write_bytes(b"edited-from-old-source")
    cut._write_edited_source_meta(edited, validated, old_video)

    calls = []

    def fake_build(video_path, validated_plan, work_dir, output_path=None):
        calls.append(Path(video_path).name)
        Path(output_path).write_bytes(b"edited-from-new-source")
        cut._write_edited_source_meta(output_path, validated_plan, video_path)
        return Path(output_path)

    monkeypatch.setattr("cut.get_video_duration", lambda path: 100.0)
    monkeypatch.setattr("cut.build_edited_source_video", fake_build)
    monkeypatch.setattr(sys, "argv", ["cut.py", str(new_video), "--work-dir", str(work)])

    cut.main()

    assert calls == ["video_new.mp4"]
    assert edited.read_bytes() == b"edited-from-new-source"


def test_cut_main_rebuilds_when_cached_edited_source_bytes_change(monkeypatch, tmp_path):
    import json
    import sys
    import cut

    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    work = tmp_path / "work"
    work.mkdir()
    raw_plan = [{"start": 10.0, "end": 20.0}]
    (work / "clip_plan.json").write_text(json.dumps(raw_plan), encoding="utf-8")
    validated = cut.normalize_clip_plan(raw_plan, video_duration=100.0)
    edited = work / "edited_source.mp4"
    edited.write_bytes(b"cached-edited")
    cut._write_edited_source_meta(edited, validated, video)
    edited.write_bytes(b"externally-mutated-edited-source")
    calls = []

    def fake_build(video_path, validated_plan, work_dir, output_path=None):
        calls.append(Path(video_path).name)
        Path(output_path).write_bytes(b"rebuilt-edited")
        cut._write_edited_source_meta(output_path, validated_plan, video_path)
        return Path(output_path)

    monkeypatch.setattr("cut.get_video_duration", lambda path: 100.0)
    monkeypatch.setattr("cut.build_edited_source_video", fake_build)
    monkeypatch.setattr(sys, "argv", ["cut.py", str(video), "--work-dir", str(work)])

    cut.main()

    assert calls == ["video.mp4"]
    assert edited.read_bytes() == b"rebuilt-edited"
