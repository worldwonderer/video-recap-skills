import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "skills" / "video-cut" / "scripts")
)
import types
import pytest  # noqa: F401
from subprocess import CompletedProcess  # noqa: F401
import cut
import cut_contract
import cut_render
import media_geometry
import sentence_boundaries
from cut import (
    build_edited_source_video,
    lint_mapped_narration,
    map_narration_to_clips,
    normalize_clip_plan,
    parse_duration_seconds,
    snap_clip_ends_to_lines,
    snap_clips_off_shot_changes,
    source_time_to_output_time,
)

_HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def test_lint_mapped_narration_flags_dropped_and_sparse():
    """Post-map re-lint must surface beats dropped by the mapper and a sparse cut output
    (the otherwise-invisible half of cut-mode desync)."""
    mapped = [
        {"start": 5.0, "end": 8.0, "narration": "一。"},
        {"start": 50.0, "end": 53.0, "narration": "二。"},
    ]
    report = lint_mapped_narration(mapped, original_count=8, output_duration=60.0)
    codes = {w["code"] for w in report["warnings"]}
    assert "many_beats_dropped" in codes  # 6/8 dropped
    assert "low_density_output" in codes  # 2 beats over 60s
    assert "long_gap_output" in codes  # 42s gap between the two beats
    assert report["dropped"] == 6
    assert report["drop_ratio"] == 0.75

    dense = [
        {"start": float(i * 6), "end": float(i * 6 + 4), "narration": "一句。"}
        for i in range(10)
    ]
    healthy = lint_mapped_narration(dense, original_count=10, output_duration=60.0)
    assert healthy["warnings"] == []


def test_lint_mapped_narration_blocking_and_clamped_flags():
    """Heavy drop/sparse output and any clipped narration sentence are BLOCKING."""
    sparse = [
        {"start": 5.0, "end": 8.0, "narration": "一。"},
        {"start": 50.0, "end": 53.0, "narration": "二。"},
    ]
    assert (
        lint_mapped_narration(sparse, original_count=8, output_duration=60.0)[
            "blocking"
        ]
        is True
    )

    dense = [
        {"start": float(i * 6), "end": float(i * 6 + 4), "narration": "一句。"}
        for i in range(10)
    ]
    assert (
        lint_mapped_narration(dense, original_count=10, output_duration=60.0)[
            "blocking"
        ]
        is False
    )

    clamped = [
        {"start": 0.0, "end": 4.0, "narration": "一。", "clamped": True},
        {"start": 5.0, "end": 9.0, "narration": "二。", "clamped": False},
    ]
    rep = lint_mapped_narration(clamped, original_count=2, output_duration=12.0)
    assert rep["clamped_count"] == 1
    assert any(w["code"] == "clamped_beats" for w in rep["warnings"])
    assert rep["blocking"] is True


def test_map_narration_to_clips_tags_clamped_beats():
    """Step 4: a beat trimmed to a clip edge is tagged clamped (its text may describe cut footage)."""
    plan = {
        "clips": [
            {
                "clip_id": 0,
                "source_start": 10.0,
                "source_end": 20.0,
                "output_start": 0.0,
                "output_end": 10.0,
            }
        ]
    }
    mapped = map_narration_to_clips(
        [
            {"start": 12.0, "end": 18.0, "narration": "完全在片段内。"},  # not clamped
            {
                "start": 15.0,
                "end": 22.0,
                "narration": "尾巴越界被裁。",
            },  # mid 18.5 in clip, end 22>20 -> clamped to 20
        ],
        plan,
    )
    by_text = {m["narration"]: m for m in mapped}
    assert by_text["完全在片段内。"]["clamped"] is False
    assert by_text["尾巴越界被裁。"]["clamped"] is True


def test_cut_main_normalize_only_writes_validated_plan_without_render(
    monkeypatch, tmp_path
):
    """Step 4: --normalize-only writes clip_plan_validated.json and skips the render/map."""
    import sys
    import json as _json
    import cut

    video = tmp_path / "v.mp4"
    video.write_bytes(b"v")
    (tmp_path / "clip_plan.json").write_text(
        '{"clips":[{"start":10.0,"end":20.0}]}', encoding="utf-8"
    )
    monkeypatch.setattr("cut_cli.get_video_duration", lambda p: 30.0)
    rendered = []
    monkeypatch.setattr(
        "cut_cli.build_edited_source_video", lambda *a, **k: rendered.append(1)
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["cut.py", str(video), "--work-dir", str(tmp_path), "--normalize-only"],
    )

    cut.main()

    validated = _json.loads(
        (tmp_path / "clip_plan_validated.json").read_text(encoding="utf-8")
    )
    assert validated["clips"]
    delivery_qc = validated["qc"]["delivery_qc"]
    assert delivery_qc["video_encode_passes"] == 1
    assert delivery_qc["audio_sample_rate"]["target"] == 48000
    assert delivery_qc["rendered"] is False
    assert delivery_qc["planned"] is True
    assert not (tmp_path / "cut_delivery_qc.json").exists()
    assert rendered == []  # no render in normalize-only
    assert not (tmp_path / "edited_source.mp4").exists()
    assert not (tmp_path / "visual_qc.json").exists()


def test_cut_main_blocks_on_heavy_drop_unless_allow_sparse(monkeypatch, tmp_path):
    """Step 4: a cut whose narration mostly falls outside the kept clips FAILS the preflight
    (before TTS), unless --allow-sparse-cut is given."""
    import sys
    import json as _json
    import pytest as _pytest
    import cut

    video = tmp_path / "v.mp4"
    video.write_bytes(b"v")
    (tmp_path / "clip_plan.json").write_text(
        '{"clips":[{"start":10.0,"end":20.0}]}', encoding="utf-8"
    )
    (tmp_path / "narration.json").write_text(
        _json.dumps(
            [
                {"start": 12.0, "end": 15.0, "narration": "片段内。"},
                {"start": 100.0, "end": 103.0, "narration": "片段外一。"},
                {"start": 110.0, "end": 113.0, "narration": "片段外二。"},
                {"start": 120.0, "end": 123.0, "narration": "片段外三。"},
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("cut_cli.get_video_duration", lambda p: 200.0)
    monkeypatch.setattr("cut.should_reuse_edited_source", lambda *a, **k: False)
    monkeypatch.setattr(
        "cut_cli.build_edited_source_video",
        lambda *a, **k: (tmp_path / "edited_source.mp4").write_bytes(b"e"),
    )
    base = ["cut.py", str(video), "--work-dir", str(tmp_path)]

    monkeypatch.setattr(sys, "argv", base)
    with _pytest.raises(SystemExit):
        cut.main()

    monkeypatch.setattr(sys, "argv", base + ["--allow-sparse-cut"])
    cut.main()  # override -> must not raise


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
        parse_duration_seconds("2m30x")  # trailing junk
    with pytest.raises(ValueError):
        parse_duration_seconds("m30")  # missing leading number


def test_normalize_clip_plan_clamps_and_maps_output_timeline():
    plan = normalize_clip_plan(
        {
            "target_duration": "10s",
            "clips": [
                {"start": 1.0, "end": 4.0, "reason": "开端"},
                {"start": 8.0, "end": 12.0, "reason": "反转"},
                {"start": 5.0, "end": 5.1, "reason": "too short"},
                {"start": "bad", "end": 7.0},
            ],
        },
        video_duration=10.0,
        clip_padding=0.5,
    )

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
        normalize_clip_plan(
            [
                {"start": 1.0, "end": 5.0},
                {"start": 4.5, "end": 8.0},
            ],
            video_duration=10.0,
        )


def test_source_time_and_narration_mapping_preserve_source_trace():
    plan = normalize_clip_plan(
        [
            {"start": 10.0, "end": 20.0, "reason": "A"},
            {"start": 40.0, "end": 50.0, "reason": "B"},
        ],
        video_duration=60.0,
    )

    assert source_time_to_output_time(12.5, plan["clips"]) == 2.5
    assert source_time_to_output_time(45.0, plan["clips"]) == 15.0
    assert source_time_to_output_time(30.0, plan["clips"]) is None

    mapped = map_narration_to_clips(
        [
            {"start": 12.0, "end": 16.0, "narration": "第一段。"},
            {
                "start": 43.0,
                "end": 48.0,
                "narration": "第二段。",
                "overlaps_speech": True,
            },
            {"start": 22.0, "end": 24.0, "narration": "会被丢弃。"},
        ],
        plan,
    )

    assert [(m["start"], m["end"]) for m in mapped] == [(2.0, 6.0), (13.0, 18.0)]
    assert mapped[0]["source_start"] == 12.0
    assert mapped[1]["source_clip_id"] == 1
    assert mapped[1]["overlaps_speech"] is True


def test_narration_mapping_uses_explicit_source_clip_id_for_repeated_ranges():
    plan = normalize_clip_plan(
        [
            {"start": 10.0, "end": 20.0},
            {"start": 10.0, "end": 20.0},
        ],
        video_duration=30.0,
        allow_overlap=True,
    )

    unmapped = map_narration_to_clips(
        [
            {"start": 12.0, "end": 14.0, "narration": "重复画面但没说用哪次。"},
        ],
        plan,
    )
    mapped = map_narration_to_clips(
        [
            {
                "start": 12.0,
                "end": 14.0,
                "source_clip_id": 1,
                "narration": "重复画面第二次出现。",
            },
        ],
        plan,
    )

    assert unmapped == []
    assert mapped[0]["start"] == 12.0
    assert mapped[0]["source_clip_id"] == 1


def test_build_edited_source_video_uses_ffmpeg_concat(monkeypatch, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    plan = normalize_clip_plan(
        [{"start": 0, "end": 1}, {"start": 2, "end": 3}], video_duration=4
    )
    commands = []

    def fake_run_cmd(cmd):
        commands.append(cmd)
        if cmd[0] == "ffprobe":
            return CompletedProcess(cmd, 0, stdout="0\n", stderr="")
        out = Path(cmd[-1])
        out.write_bytes(b"mp4")
        return CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("cut_render.run_cmd", fake_run_cmd)
    monkeypatch.setattr("media_geometry.run_cmd", fake_run_cmd)
    monkeypatch.setattr("cut_render.get_video_duration", lambda path: 2.0)

    output = build_edited_source_video(video, plan, work_dir)

    assert output.exists()
    ffmpeg_cmd = [cmd for cmd in commands if cmd[0] == "ffmpeg"][0]
    assert "trim=start=0.000:end=1.000" in " ".join(ffmpeg_cmd)
    assert "concat=n=2" in " ".join(ffmpeg_cmd)
    assert ffmpeg_cmd[ffmpeg_cmd.index("-pix_fmt") + 1] == "yuv420p"
    assert ffmpeg_cmd[ffmpeg_cmd.index("-ar") + 1] == "48000"
    assert ffmpeg_cmd[ffmpeg_cmd.index("-movflags") + 1] == "+faststart"


def test_cut_source_fingerprint_detects_middle_only_changes(tmp_path):
    import cut

    first = tmp_path / "a.mp4"
    second = tmp_path / "b.mp4"
    first.write_bytes(b"A" * 70000 + b"middle-one" + b"Z" * 70000)
    second.write_bytes(b"A" * 70000 + b"middle-two" + b"Z" * 70000)

    assert first.stat().st_size == second.stat().st_size
    assert cut.file_fingerprint(first) != cut.file_fingerprint(second)


def test_cut_main_does_not_reuse_edited_source_when_normalized_plan_changes(
    monkeypatch, tmp_path
):
    import json
    import sys
    import cut

    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    work = tmp_path / "work"
    work.mkdir()
    (work / "clip_plan.json").write_text(
        json.dumps([{"start": 10.0, "end": 20.0}]), encoding="utf-8"
    )
    edited = work / "edited_source.mp4"
    edited.write_bytes(b"old-edited")

    calls = []

    def fake_build(video_path, validated_plan, work_dir, output_path=None):
        calls.append(validated_plan)
        Path(output_path).write_bytes(b"new-edited")
        cut_contract._write_edited_source_meta(output_path, validated_plan, video_path)
        return Path(output_path)

    monkeypatch.setattr("cut_cli.get_video_duration", lambda path: 100.0)
    monkeypatch.setattr("cut_cli.build_edited_source_video", fake_build)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cut.py",
            str(video),
            "--work-dir",
            str(work),
            "--clip-padding",
            "5",
        ],
    )

    cut.main()

    assert len(calls) == 1
    assert (
        json.loads((work / "clip_plan_validated.json").read_text(encoding="utf-8"))[
            "clips"
        ][0]["source_start"]
        == 5.0
    )
    assert edited.read_bytes() == b"new-edited"


def test_edited_source_cache_fingerprint_includes_render_affecting_config(
    monkeypatch, tmp_path
):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    edited = tmp_path / "edited_source.mp4"
    edited.write_bytes(b"edited")
    plan = cut.normalize_clip_plan([{"start": 0.0, "end": 1.0}], video_duration=2.0)

    monkeypatch.setattr(
        "cut_contract.CONFIG", {**cut_contract.CONFIG, "clip_join_audio_fade_ms": 30.0}
    )
    cut_contract._write_edited_source_meta(edited, plan, video)
    meta = json.loads(
        (tmp_path / "edited_source.mp4.meta.json").read_text(encoding="utf-8")
    )
    assert meta["render_cache"]["clip_join_audio_fade_ms"] == 30.0
    assert (
        meta["render_cache"]["geometry_render_algorithm_version"]
        == cut_contract.GEOMETRY_RENDER_ALGORITHM_VERSION
    )
    assert cut.should_reuse_edited_source(edited, plan, video) is True

    monkeypatch.setattr(
        "cut_contract.CONFIG", {**cut_contract.CONFIG, "clip_join_audio_fade_ms": 80.0}
    )
    assert cut.edited_source_render_fingerprint() != meta["render_fingerprint"]
    assert cut.should_reuse_edited_source(edited, plan, video) is False


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
    cut_contract._write_edited_source_meta(edited, validated, video)

    def boom(*args, **kwargs):
        raise AssertionError("matching edited_source cache should be reused")

    monkeypatch.setattr("cut_cli.get_video_duration", lambda path: 100.0)
    monkeypatch.setattr("cut_cli.build_edited_source_video", boom)
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
    cut_contract._write_edited_source_meta(edited, validated, old_video)

    calls = []

    def fake_build(video_path, validated_plan, work_dir, output_path=None):
        calls.append(Path(video_path).name)
        Path(output_path).write_bytes(b"edited-from-new-source")
        cut_contract._write_edited_source_meta(output_path, validated_plan, video_path)
        return Path(output_path)

    monkeypatch.setattr("cut_cli.get_video_duration", lambda path: 100.0)
    monkeypatch.setattr("cut_cli.build_edited_source_video", fake_build)
    monkeypatch.setattr(
        sys, "argv", ["cut.py", str(new_video), "--work-dir", str(work)]
    )

    cut.main()

    assert calls == ["video_new.mp4"]
    assert edited.read_bytes() == b"edited-from-new-source"


def test_cut_main_rebuilds_when_cached_edited_source_bytes_change(
    monkeypatch, tmp_path
):
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
    cut_contract._write_edited_source_meta(edited, validated, video)
    edited.write_bytes(b"externally-mutated-edited-source")
    calls = []

    def fake_build(video_path, validated_plan, work_dir, output_path=None):
        calls.append(Path(video_path).name)
        Path(output_path).write_bytes(b"rebuilt-edited")
        cut_contract._write_edited_source_meta(output_path, validated_plan, video_path)
        return Path(output_path)

    monkeypatch.setattr("cut_cli.get_video_duration", lambda path: 100.0)
    monkeypatch.setattr("cut_cli.build_edited_source_video", fake_build)
    monkeypatch.setattr(sys, "argv", ["cut.py", str(video), "--work-dir", str(work)])

    cut.main()

    assert calls == ["video.mp4"]
    assert edited.read_bytes() == b"rebuilt-edited"


# ---------------------------------------------------------------------------
# snap_clip_ends_to_lines tests
# ---------------------------------------------------------------------------


def _make_plan(clips_spec, allow_overlap=False, video_duration=100.0):
    """Build a validated plan from a list of (source_start, source_end) tuples."""
    raw = [{"start": s, "end": e} for s, e in clips_spec]
    return normalize_clip_plan(
        raw, video_duration=video_duration, allow_overlap=allow_overlap
    )


def test_snap_extends_mid_speech_clip_to_next_quiet_window():
    """A clip ending mid-speech is extended to the next quiet-window start."""
    silence = [{"start": 12.0, "end": 13.0, "duration": 1.0}]
    plan = _make_plan([(0.0, 10.0)])
    snapped = snap_clip_ends_to_lines(
        plan, silence, video_duration=100.0, max_extend=5.0
    )
    assert snapped["clips"][0]["source_end"] == 12.0
    assert snapped["clips"][0]["duration"] == 12.0
    assert snapped["total_duration"] == 12.0


def test_snap_no_snap_when_end_already_in_quiet_window():
    """No extension when source_end already sits inside a quiet window."""
    silence = [{"start": 9.5, "end": 11.0, "duration": 1.5}]
    plan = _make_plan([(0.0, 10.0)])  # 10.0 is inside [9.5, 11.0]
    snapped = snap_clip_ends_to_lines(
        plan, silence, video_duration=100.0, max_extend=5.0
    )
    assert snapped["clips"][0]["source_end"] == 10.0  # unchanged


def test_snap_caps_at_max_extend_when_next_quiet_too_far():
    """When the next quiet window is beyond max_extend, no extension is applied."""
    silence = [{"start": 20.0, "end": 21.0, "duration": 1.0}]
    plan = _make_plan([(0.0, 10.0)])
    # next quiet at 20.0 is 10s away; max_extend=2.0 → candidate=12.0 but pause is at 20>12 → no snap
    snapped = snap_clip_ends_to_lines(
        plan, silence, video_duration=100.0, max_extend=2.0
    )
    assert snapped["clips"][0]["source_end"] == 10.0  # unchanged


def test_snap_caps_at_video_duration():
    """Extension never exceeds video_duration."""
    silence = [{"start": 99.0, "end": 100.0, "duration": 1.0}]
    plan = _make_plan([(0.0, 98.0)], video_duration=100.0)
    snapped = snap_clip_ends_to_lines(
        plan, silence, video_duration=100.0, max_extend=5.0
    )
    assert snapped["clips"][0]["source_end"] <= 100.0
    assert snapped["clips"][0]["source_end"] == 99.0


def test_snap_no_overlap_when_allow_overlap_false():
    """Extension is capped to avoid crossing into a later clip's source range."""
    silence = [{"start": 25.0, "end": 26.0, "duration": 1.0}]
    # clip 0: 0-10, clip 1: 20-30 → extending clip 0 toward pause at 25 would enter clip 1
    plan = _make_plan([(0.0, 10.0), (20.0, 30.0)], allow_overlap=False)
    snapped = snap_clip_ends_to_lines(
        plan, silence, video_duration=100.0, max_extend=20.0
    )
    # candidate_end = min(25.0, 10+20, 100) = 20.0; but clip 1 starts at 20.0 → capped to 20.0
    # 20.0 > 10.0 so extension is applied up to the boundary
    assert snapped["clips"][0]["source_end"] == 20.0
    # clip 1 is untouched
    assert snapped["clips"][1]["source_start"] == 20.0
    assert snapped["clips"][1]["source_end"] == 30.0


def test_snap_empty_silence_returns_plan_unchanged():
    """Empty silence_periods → plan returned byte-identical (no modifications)."""
    plan = _make_plan([(0.0, 10.0), (20.0, 30.0)])
    snapped_empty = snap_clip_ends_to_lines(
        plan, [], video_duration=100.0, max_extend=2.0
    )
    snapped_none = snap_clip_ends_to_lines(
        plan, None, video_duration=100.0, max_extend=2.0
    )
    assert snapped_empty is plan
    assert snapped_none is plan


def test_snap_recomputes_output_timeline_for_all_clips():
    """After snapping clip 0, the following clip's output_start shifts correctly."""
    # clip 0: 0-10, clip 1: 20-30; quiet at 12 within max_extend=5
    silence = [{"start": 12.0, "end": 13.0, "duration": 1.0}]
    plan = _make_plan([(0.0, 10.0), (20.0, 30.0)])
    snapped = snap_clip_ends_to_lines(
        plan, silence, video_duration=100.0, max_extend=5.0
    )

    c0, c1 = snapped["clips"]
    # clip 0 extended to 12.0
    assert c0["source_end"] == 12.0
    assert c0["duration"] == 12.0
    assert c0["output_start"] == 0.0
    assert c0["output_end"] == 12.0
    # clip 1 unchanged source, but output_start shifts from 10 → 12
    assert c1["source_start"] == 20.0
    assert c1["source_end"] == 30.0
    assert c1["duration"] == 10.0
    assert c1["output_start"] == 12.0
    assert c1["output_end"] == 22.0
    assert snapped["total_duration"] == 22.0


def test_snap_skips_malformed_silence_rows():
    """A stale/hand-edited silence row (missing start/end, or not a dict) is skipped, not fatal."""
    silence = [{"foo": 1}, "bad", {"start": 12.0, "end": 13.0, "duration": 1.0}]
    plan = _make_plan([(0.0, 10.0)])
    snapped = snap_clip_ends_to_lines(
        plan, silence, video_duration=100.0, max_extend=5.0
    )
    assert snapped["clips"][0]["source_end"] == 12.0  # the well-formed row still snaps


def test_sentence_anchor_pause_window_snaps_both_clip_edges(tmp_path):
    """Reliable sentence anchors are first-class cut boundaries even when long-silence is empty."""
    (tmp_path / "speech_boundary_anchors.json").write_text(
        json.dumps(
            {
                "sentence_anchors": [
                    {"time": 8.4, "pause_start": 8.2, "confidence": "high"},
                    {"time": 14.3, "pause_start": 14.0, "confidence": "medium"},
                    {"time": 18.0, "pause_start": 17.8, "confidence": "low"},
                ]
            }
        ),
        encoding="utf-8",
    )
    boundaries = sentence_boundaries._load_sentence_boundary_windows(tmp_path)
    assert boundaries == [
        {"start": 8.2, "end": 8.4, "kind": "sentence_anchor", "confidence": "high"},
        {"start": 14.0, "end": 14.3, "kind": "sentence_anchor", "confidence": "medium"},
    ]

    plan = _make_plan([(10.0, 13.5)], video_duration=30.0)
    plan = cut.snap_clip_starts_to_lines(plan, boundaries, 30.0, max_prepend=1.8)
    plan = cut.snap_clip_ends_to_lines(plan, boundaries, 30.0, max_extend=2.0)
    assert plan["clips"][0]["source_start"] == 8.4
    assert plan["clips"][0]["source_end"] == 14.0


def test_sentence_boundary_gate_blocks_unsnapped_speech_edges():
    plan = _make_plan([(3.0, 7.0)], video_duration=10.0)
    out = cut.enforce_clip_sentence_boundaries(
        plan,
        boundary_windows=[{"start": 4.8, "end": 5.0}],
        speech_spans=[{"start": 0.0, "end": 10.0}],
        video_duration=10.0,
    )
    blockers = out["qc"]["blocking"]
    assert {
        b["edge"] for b in blockers if b["code"] == "unsafe_clip_sentence_boundary"
    } == {"start", "end"}


def test_sentence_boundary_gate_allows_source_ends_and_contiguous_same_source_join():
    plan = _make_plan([(0.0, 5.0), (5.0, 10.0)], video_duration=10.0)
    out = cut.enforce_clip_sentence_boundaries(
        plan,
        boundary_windows=[],
        speech_spans=[{"start": 0.0, "end": 10.0}],
        video_duration=10.0,
    )
    assert not out["qc"].get("blocking")
    checks = out["qc"]["boundary_status"]["sentence_checks"]
    assert [c["reason"] for c in checks] == [
        "source_start",
        "continuous_source_join",
        "continuous_source_join",
        "source_end",
    ]


# ---------------------------------------------------------------------------
# snap_clips_off_shot_changes tests (avoid 闪烁 at edit points)
# ---------------------------------------------------------------------------


def _fake_detector(changes):
    """Stand in for _detect_shot_changes: return the seeded cuts that fall in the asked window."""

    def detect(video, win_start, win_end, threshold, lead=0.25):
        return sorted(c for c in changes if win_start <= c <= win_end)

    return detect


def test_scene_cut_snap_moves_start_forward_past_early_change(monkeypatch):
    """A shot-change just after source_start (old-shot sliver) pushes source_start onto the cut."""
    monkeypatch.setattr(
        sentence_boundaries, "_detect_shot_changes", _fake_detector([10.3])
    )
    plan = _make_plan([(10.0, 30.0)])
    snapped = snap_clips_off_shot_changes(
        plan, "v.mp4", video_duration=100.0, margin=0.5, threshold=0.4
    )
    c = snapped["clips"][0]
    assert c["source_start"] == 10.3 and c["source_end"] == 30.0
    assert c["duration"] == 19.7
    assert c["output_start"] == 0.0 and c["output_end"] == 19.7


def test_scene_cut_snap_pulls_end_back_before_late_change(monkeypatch):
    """A shot-change just before source_end (next-shot sliver) pulls source_end onto the cut."""
    monkeypatch.setattr(
        sentence_boundaries, "_detect_shot_changes", _fake_detector([29.7])
    )
    plan = _make_plan([(10.0, 30.0)])
    snapped = snap_clips_off_shot_changes(
        plan, "v.mp4", video_duration=100.0, margin=0.5, threshold=0.4
    )
    c = snapped["clips"][0]
    assert c["source_start"] == 10.0 and c["source_end"] == 29.7


def test_scene_cut_snap_leaves_clean_boundaries_untouched(monkeypatch):
    """A cut in the middle of the clip (far from both boundaries) triggers no snap."""
    monkeypatch.setattr(
        sentence_boundaries, "_detect_shot_changes", _fake_detector([20.0])
    )
    plan = _make_plan([(10.0, 30.0)])
    snapped = snap_clips_off_shot_changes(
        plan, "v.mp4", video_duration=100.0, margin=0.5, threshold=0.4
    )
    c = snapped["clips"][0]
    assert c["source_start"] == 10.0 and c["source_end"] == 30.0


def test_scene_cut_snap_skips_when_snap_would_collapse_clip(monkeypatch):
    """On a clip too short to keep min_keep after snapping, the boundary is left as-is (no collapse)."""
    monkeypatch.setattr(
        sentence_boundaries, "_detect_shot_changes", _fake_detector([10.4])
    )
    plan = _make_plan([(10.0, 10.6)])  # 0.6s clip, change at 10.4 inside both windows
    snapped = snap_clips_off_shot_changes(
        plan, "v.mp4", video_duration=100.0, margin=0.5, threshold=0.4
    )
    c = snapped["clips"][0]
    assert (
        c["source_start"] == 10.0 and c["source_end"] == 10.6
    )  # unchanged, still a valid clip
    assert c["duration"] == 0.6


def test_scene_cut_snap_recomputes_output_across_multiple_clips(monkeypatch):
    """Output timeline is repacked cursor-based after the source ranges shrink."""
    monkeypatch.setattr(
        sentence_boundaries, "_detect_shot_changes", _fake_detector([10.3, 49.6])
    )
    plan = _make_plan([(10.0, 20.0), (40.0, 50.0)])
    snapped = snap_clips_off_shot_changes(
        plan, "v.mp4", video_duration=100.0, margin=0.5, threshold=0.4
    )
    a, b = snapped["clips"]
    assert (
        a["source_start"] == 10.3
        and a["output_start"] == 0.0
        and a["output_end"] == 9.7
    )
    assert b["source_end"] == 49.6 and b["output_start"] == 9.7
    assert snapped["total_duration"] == round(9.7 + 9.6, 3)


def test_detect_shot_changes_offsets_by_seek_and_filters_window(monkeypatch):
    """pts_time is rebased to the seek target, so seek+pts recovers absolute time; cuts outside
    [win_start, win_end] (e.g. the seek/keyframe artifact) are dropped."""
    stderr = (
        "frame showinfo pts_time:1.762\n frame pts_time:5.866 \n frame pts_time:0.05\n"
    )
    monkeypatch.setattr(
        sentence_boundaries,
        "subprocess",
        types.SimpleNamespace(run=lambda *a, **k: CompletedProcess(a, 0, "", stderr)),
    )
    out = sentence_boundaries._detect_shot_changes(
        "v.mkv", 55.0, 68.0, 0.4
    )  # seek=54.75
    assert out == [56.512, 60.616]  # 54.8 (54.75+0.05) is < win_start → filtered


def test_normalize_multi_source_clip_plan_maps_sources_and_validates_per_source_overlap(
    tmp_path,
):
    manifest = {
        "sources": [
            {
                "source_id": "a",
                "source_path": str(tmp_path / "a.mp4"),
                "duration": 10.0,
            },
            {"source_id": "b", "source_path": str(tmp_path / "b.mp4"), "duration": 5.0},
        ]
    }
    plan = cut.normalize_multi_source_clip_plan(
        [
            {"source_id": "a", "start": 1.0, "end": 4.0, "reason": "A"},
            {"source_id": "b", "start": 4.0, "end": 8.0, "reason": "B"},
            {"source_id": "b", "start": 0.0, "end": 1.0, "reason": "B2"},
        ],
        manifest,
        clip_padding=0.5,
    )

    assert plan["total_duration"] == 7.0
    assert [c["source_id"] for c in plan["clips"]] == ["a", "b", "b"]
    assert plan["clips"][0]["source_path"].endswith("a.mp4")
    assert plan["clips"][0]["source_start"] == 0.5
    assert plan["clips"][1]["source_end"] == 5.0  # clamped to source b duration
    assert plan["clips"][2]["output_start"] == 5.5

    with pytest.raises(ValueError, match="source_id a"):
        cut.normalize_multi_source_clip_plan(
            [
                {"source_id": "a", "start": 1.0, "end": 4.0},
                {"source_id": "a", "start": 3.5, "end": 5.0},
            ],
            manifest,
        )
    with pytest.raises(ValueError, match="missing source_id"):
        cut.normalize_multi_source_clip_plan(
            [
                {"start": 1.0, "end": 2.0},
            ],
            manifest,
        )
    with pytest.raises(ValueError, match="unknown source_id"):
        cut.normalize_multi_source_clip_plan(
            [
                {"source_id": "missing", "start": 1.0, "end": 2.0},
            ],
            manifest,
        )


def test_build_edited_source_video_multi_source_uses_multiple_inputs_and_cache_meta(
    monkeypatch, tmp_path
):
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(b"aaa")
    b.write_bytes(b"bbb")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    plan = cut.normalize_multi_source_clip_plan(
        [
            {"source_id": "a", "start": 0, "end": 1},
            {"source_id": "b", "start": 2, "end": 3},
            {"source_id": "a", "start": 4, "end": 5},
        ],
        {
            "sources": [
                {"source_id": "a", "source_path": str(a), "duration": 10},
                {"source_id": "b", "source_path": str(b), "duration": 10},
            ]
        },
    )
    commands = []

    def fake_run_cmd(cmd):
        commands.append(cmd)
        if cmd[0] == "ffprobe":
            return CompletedProcess(cmd, 0, stdout="", stderr="")
        Path(cmd[-1]).write_bytes(b"edited")
        return CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("cut_render.run_cmd", fake_run_cmd)
    monkeypatch.setattr("media_geometry.run_cmd", fake_run_cmd)
    monkeypatch.setattr("cut_render.get_video_duration", lambda path: 3.0)

    out = cut.build_edited_source_video("ignored.mp4", plan, work_dir)

    ffmpeg_cmd = [cmd for cmd in commands if cmd[0] == "ffmpeg"][0]
    # Only the two media inputs — audio is now synthesized per-clip inside filter_complex,
    # not via a single global anullsrc input.
    assert ffmpeg_cmd.count("-i") == 2
    joined = " ".join(ffmpeg_cmd)
    assert f"-i {a}" in joined and f"-i {b}" in joined
    assert "[0:v]trim=start=0.000:end=1.000" in joined
    assert "[1:v]trim=start=2.000:end=3.000" in joined
    assert "[0:v]trim=start=4.000:end=5.000" in joined
    # Heterogeneous sources are normalized to one canvas before concat (probe is mocked
    # empty -> default 1280x720), and each clip gets an audio segment (silent here).
    assert (
        "scale=1280:720" in joined
        and "setsar=1" in joined
        and "format=yuv420p" in joined
    )
    assert "anullsrc=r=48000:cl=stereo" in joined
    assert "concat=n=3:v=1:a=1" in joined
    assert cut.should_reuse_edited_source(out, plan, "ignored.mp4") is True


@pytest.mark.skipif(
    not _HAVE_FFMPEG and not os.environ.get("RECAP_REQUIRE_FFMPEG"),
    reason="ffmpeg/ffprobe required for real render (set RECAP_REQUIRE_FFMPEG=1 to make a missing binary a hard failure instead of a silent skip)",
)
def test_build_edited_source_video_multi_resolution_mixed_audio_real_render(tmp_path):
    """Real ffmpeg: heterogeneous sources (different resolution/fps, one silent) must concat
    into ONE playable output with an audio track. This path was previously all-mocked, hiding
    the missing scale/setsar/fps normalization (concat would abort) and the all-or-nothing
    audio drop. On a runner where ffmpeg MUST exist, set RECAP_REQUIRE_FFMPEG=1 so a missing
    binary fails loudly instead of silently skipping this coverage."""
    import subprocess

    if not _HAVE_FFMPEG:
        pytest.fail("RECAP_REQUIRE_FFMPEG is set but ffmpeg/ffprobe is not installed")
    import cut

    a = tmp_path / "a_1080_audio.mp4"  # 1920x1080@30, WITH audio
    b = tmp_path / "b_480_silent.mp4"  # 854x480@24, NO audio
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=1920x1080:rate=30:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-shortest",
            "-pix_fmt",
            "yuv420p",
            str(a),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=854x480:rate=24:duration=2",
            "-pix_fmt",
            "yuv420p",
            str(b),
        ],
        check=True,
        capture_output=True,
    )
    work = tmp_path / "work"
    work.mkdir()
    plan = cut.normalize_multi_source_clip_plan(
        [
            {"source_id": "a", "start": 0.0, "end": 1.0},
            {"source_id": "b", "start": 0.0, "end": 1.0},
            {"source_id": "a", "start": 1.0, "end": 2.0},
        ],
        {
            "sources": [
                {"source_id": "a", "source_path": str(a), "duration": 2.0},
                {"source_id": "b", "source_path": str(b), "duration": 2.0},
            ]
        },
    )

    out = cut.build_edited_source_video(str(a), plan, work)
    assert out.exists() and out.stat().st_size > 0

    def _has_stream(kind):
        r = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                kind,
                "-show_entries",
                "stream=index",
                "-of",
                "csv=p=0",
                str(out),
            ],
            capture_output=True,
            text=True,
        )
        return bool(r.stdout.strip())

    assert _has_stream("v:0"), "no video stream in concatenated output"
    assert _has_stream("a:0"), "audio track dropped even though one source had audio"
    dur = float(
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(out),
            ],
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    assert dur > 2.0, f"expected ~3s of concatenated clips, got {dur}s"


def test_snap_multi_source_clips_line_snaps_each_clip_against_its_own_source(tmp_path):
    """FF-D: each clip's end snaps to ITS OWN source's next pause (a clip in source B never
    snaps to source A's silence), then the global output timeline is recomputed in plan order."""
    import json as _json

    work = tmp_path / "work"
    (work / "sources" / "a").mkdir(parents=True)
    (work / "sources" / "b").mkdir(parents=True)
    (work / "sources" / "a" / "silence_periods.json").write_text(
        _json.dumps([{"start": 4.2, "end": 5.0}]), encoding="utf-8"
    )
    (work / "sources" / "b" / "silence_periods.json").write_text(
        _json.dumps([{"start": 9.0, "end": 9.5}]), encoding="utf-8"
    )
    plan = {
        "allow_overlap": False,
        "clips": [
            {
                "clip_id": 0,
                "source_id": "a",
                "source_path": "/a.mp4",
                "source_start": 1.0,
                "source_end": 4.0,
                "output_start": 0.0,
                "output_end": 3.0,
                "duration": 3.0,
            },
            {
                "clip_id": 1,
                "source_id": "b",
                "source_path": "/b.mp4",
                "source_start": 2.0,
                "source_end": 8.5,
                "output_start": 3.0,
                "output_end": 9.5,
                "duration": 6.5,
            },
        ],
        "total_duration": 9.5,
    }
    sources = {
        "a": {"source_path": "/a.mp4", "duration": 10.0},
        "b": {"source_path": "/b.mp4", "duration": 12.0},
    }

    out = cut.snap_multi_source_clips(
        plan,
        sources,
        work,
        line_max_extend=2.0,
        scene_margin=0.5,
        scene_threshold=0.4,
        do_scene_snap=False,
    )

    assert out["clips"][0]["source_end"] == 4.2  # source a's pause
    assert out["clips"][1]["source_end"] == 9.0  # source b's pause, NOT a's 4.2
    assert (
        out["clips"][0]["output_start"] == 0.0 and out["clips"][0]["output_end"] == 3.2
    )
    assert out["clips"][1]["output_start"] == 3.2 and out["clips"][1]["duration"] == 7.0
    assert out["total_duration"] == 10.2


def test_snap_multi_source_clips_routes_shot_detection_to_each_clips_source(
    monkeypatch, tmp_path
):
    """FF-D: shot-change detection runs against each clip's OWN source video, not one ambient file."""
    seen = []
    monkeypatch.setattr(
        sentence_boundaries,
        "_detect_shot_changes",
        lambda video, *a, **k: (seen.append(str(video)), [])[1],
    )
    plan = {
        "allow_overlap": False,
        "clips": [
            {
                "clip_id": 0,
                "source_id": "a",
                "source_path": "/a.mp4",
                "source_start": 0.0,
                "source_end": 3.0,
                "output_start": 0.0,
                "output_end": 3.0,
                "duration": 3.0,
            },
            {
                "clip_id": 1,
                "source_id": "b",
                "source_path": "/b.mp4",
                "source_start": 0.0,
                "source_end": 3.0,
                "output_start": 3.0,
                "output_end": 6.0,
                "duration": 3.0,
            },
        ],
        "total_duration": 6.0,
    }
    sources = {
        "a": {"source_path": "/a.mp4", "duration": 10.0},
        "b": {"source_path": "/b.mp4", "duration": 10.0},
    }

    cut.snap_multi_source_clips(
        plan,
        sources,
        tmp_path,
        line_max_extend=2.0,
        scene_margin=0.5,
        scene_threshold=0.4,
        do_line_snap=False,
    )

    assert "/a.mp4" in seen and "/b.mp4" in seen


def test_multi_source_sentence_snap_runs_after_shot_snap(monkeypatch, tmp_path):
    """Visual cleanup may move an edge, but the final pass must restore sentence-safe audio edges."""
    source_work = tmp_path / "sources" / "a"
    source_work.mkdir(parents=True)
    (source_work / "silence_periods.json").write_text("[]", encoding="utf-8")
    (source_work / "speech_boundary_anchors.json").write_text(
        json.dumps(
            {
                "sentence_anchors": [
                    {"time": 9.5, "pause_start": 9.3, "confidence": "high"},
                    {"time": 20.4, "pause_start": 20.2, "confidence": "high"},
                ]
            }
        ),
        encoding="utf-8",
    )
    (source_work / "asr_result.json").write_text(
        json.dumps([{"start": 0.0, "end": 30.0, "text": "持续讲话。"}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sentence_boundaries, "_detect_shot_changes", _fake_detector([10.2, 19.8])
    )
    plan = {
        "allow_overlap": False,
        "clips": [
            {
                "clip_id": 0,
                "source_id": "a",
                "source_path": "/a.mp4",
                "source_start": 10.0,
                "source_end": 20.0,
                "output_start": 0.0,
                "output_end": 10.0,
                "duration": 10.0,
            }
        ],
        "total_duration": 10.0,
    }
    out = cut.snap_multi_source_clips(
        plan,
        {"a": {"source_path": "/a.mp4", "duration": 30.0}},
        tmp_path,
        line_max_extend=2.0,
        scene_margin=0.5,
        scene_threshold=0.4,
        start_max_prepend=1.8,
        start_max_trim=0.35,
    )
    assert out["clips"][0]["source_start"] == 9.5
    assert out["clips"][0]["source_end"] == 20.2
    assert not out["qc"].get("blocking")


def test_snap_multi_source_clips_noops_without_silence_or_flags(tmp_path):
    """No per-source silence data and scene-snap off -> boundaries unchanged (advisory degrade)."""
    plan = {
        "allow_overlap": False,
        "clips": [
            {
                "clip_id": 0,
                "source_id": "a",
                "source_path": "/a.mp4",
                "source_start": 1.0,
                "source_end": 4.0,
                "output_start": 0.0,
                "output_end": 3.0,
                "duration": 3.0,
            }
        ],
        "total_duration": 3.0,
    }
    out = cut.snap_multi_source_clips(
        plan,
        {"a": {"source_path": "/a.mp4", "duration": 10.0}},
        tmp_path,
        line_max_extend=2.0,
        scene_margin=0.5,
        scene_threshold=0.4,
        do_scene_snap=False,
    )
    assert out["clips"][0]["source_end"] == 4.0
    assert out["total_duration"] == 3.0


def test_snap_clip_starts_prepends_to_prior_quiet_end():
    plan = _make_plan([(10.0, 14.0)])
    silence = [{"start": 7.5, "end": 8.4}, {"start": 15.0, "end": 16.0}]

    out = cut.snap_clip_starts_to_lines(
        plan, silence, video_duration=30.0, max_prepend=1.8, max_trim=0.35
    )

    assert out["clips"][0]["source_start"] == 8.4
    assert out["clips"][0]["duration"] == 5.6
    assert out["qc"]["boundary_status"]["start_snaps"][0]["action"] == "prepended"


def test_snap_clip_starts_keeps_when_already_quiet():
    plan = _make_plan([(10.2, 14.0)])
    silence = [{"start": 10.0, "end": 10.5}]

    out = cut.snap_clip_starts_to_lines(
        plan, silence, video_duration=30.0, max_prepend=1.8, max_trim=0.35
    )

    assert out["clips"][0]["source_start"] == 10.2
    assert out["qc"]["boundary_status"]["start_snaps"][0]["reason"] == "already_quiet"


def test_snap_clip_starts_warns_when_prepend_would_overlap():
    plan = _make_plan([(8.0, 10.0), (10.0, 14.0)])
    silence = [{"start": 7.5, "end": 8.4}]

    out = cut.snap_clip_starts_to_lines(
        plan, silence, video_duration=30.0, max_prepend=1.8, max_trim=0.35
    )
    second = out["clips"][1]

    assert second["source_start"] == 10.0
    event = out["qc"]["boundary_status"]["start_snaps"][1]
    assert event["start_unsnapped_reason"] == "overlap_or_collapse"
    assert any(w["code"] == "clip_start_unsnapped" for w in out["qc"]["warnings"])


def test_snap_clip_starts_no_prior_quiet_keeps_and_warns_by_default():
    plan = _make_plan([(10.0, 14.0)])
    silence = [{"start": 12.0, "end": 12.5}]

    out = cut.snap_clip_starts_to_lines(
        plan, silence, video_duration=30.0, max_prepend=1.8, max_trim=0.35
    )

    assert out["clips"][0]["source_start"] == 10.0
    assert (
        out["qc"]["boundary_status"]["start_snaps"][0]["start_unsnapped_reason"]
        == "no_prior_quiet"
    )


def test_snap_clip_starts_allows_tiny_forward_trim_to_next_quiet():
    plan = _make_plan([(10.0, 14.0)])
    silence = [{"start": 10.25, "end": 10.8}]

    out = cut.snap_clip_starts_to_lines(
        plan, silence, video_duration=30.0, max_prepend=1.8, max_trim=0.35
    )

    assert out["clips"][0]["source_start"] == 10.25
    assert out["qc"]["boundary_status"]["start_snaps"][0]["action"] == "trimmed"


def test_snap_multi_source_clips_start_snaps_each_source_silence(tmp_path):
    work = tmp_path / "work"
    (work / "sources" / "a").mkdir(parents=True)
    (work / "sources" / "b").mkdir(parents=True)
    (work / "sources" / "a" / "silence_periods.json").write_text(
        json.dumps([{"start": 1.0, "end": 1.4}]), encoding="utf-8"
    )
    (work / "sources" / "b" / "silence_periods.json").write_text(
        json.dumps([{"start": 5.0, "end": 5.3}]), encoding="utf-8"
    )
    plan = {
        "allow_overlap": False,
        "clips": [
            {
                "clip_id": 0,
                "source_id": "a",
                "source_path": "/a.mp4",
                "source_start": 2.0,
                "source_end": 4.0,
                "output_start": 0.0,
                "output_end": 2.0,
                "duration": 2.0,
            },
            {
                "clip_id": 1,
                "source_id": "b",
                "source_path": "/b.mp4",
                "source_start": 6.0,
                "source_end": 8.0,
                "output_start": 2.0,
                "output_end": 4.0,
                "duration": 2.0,
            },
        ],
        "total_duration": 4.0,
    }
    out = cut.snap_multi_source_clips(
        plan,
        {
            "a": {"source_path": "/a.mp4", "duration": 10.0},
            "b": {"source_path": "/b.mp4", "duration": 10.0},
        },
        work,
        line_max_extend=0.0,
        scene_margin=0.0,
        scene_threshold=0.4,
        do_scene_snap=False,
        start_max_prepend=1.8,
        start_max_trim=0.35,
    )

    assert out["clips"][0]["source_start"] == 1.4
    assert out["clips"][1]["source_start"] == 5.3
    assert out["clips"][1]["output_start"] == 2.6


def test_select_output_geometry_uses_all_used_sources_not_first(monkeypatch):
    probes = {
        "/low.mp4": (854, 480, 24.0),
        "/hd.mp4": (1920, 1080, 30.0),
    }
    monkeypatch.setattr(
        media_geometry, "_probe_video_geometry", lambda p: probes[str(p)]
    )
    clips = [
        {"source_id": "low", "source_path": "/low.mp4", "duration": 1.0},
        {"source_id": "hd", "source_path": "/hd.mp4", "duration": 5.0},
    ]

    w, h, fps, qc = media_geometry._select_output_geometry(
        ["/low.mp4", "/hd.mp4"], clips
    )

    assert (w, h, fps) == (1920, 1080, 30.0)
    assert qc["source_id"] == "hd"
    assert qc["reason"] == "weighted_orientation_area_fps"


def test_select_output_geometry_orientation_duration_and_fps_ties(monkeypatch):
    probes = {
        "/portrait.mp4": (1080, 1920, 24.0),
        "/landscape.mp4": (1280, 720, 60.0),
        "/landscape2.mp4": (1920, 1080, 30.0),
    }
    monkeypatch.setattr(
        media_geometry, "_probe_video_geometry", lambda p: probes[str(p)]
    )
    clips = [
        {"source_id": "p", "source_path": "/portrait.mp4", "duration": 2.0},
        {"source_id": "l1", "source_path": "/landscape.mp4", "duration": 1.0},
        {"source_id": "l2", "source_path": "/landscape2.mp4", "duration": 2.0},
    ]

    w, h, fps, qc = media_geometry._select_output_geometry(
        ["/portrait.mp4", "/landscape.mp4", "/landscape2.mp4"], clips
    )

    assert (w, h) == (1920, 1080)  # landscape wins by total used duration (3s > 2s)
    assert fps == 30.0  # 24/30/60 all tie by duration? 30 bucket has 2s, wins
    assert qc["orientation"] == "landscape"


def test_probe_video_geometry_is_iterable_and_rotation_sar_aware(monkeypatch):
    payload = {
        "streams": [
            {
                "width": 1920,
                "height": 1080,
                "r_frame_rate": "30000/1001",
                "sample_aspect_ratio": "2:1",
                "display_aspect_ratio": "16:9",
                "tags": {"rotate": "90"},
            }
        ]
    }
    monkeypatch.setattr(
        media_geometry,
        "run_cmd",
        lambda cmd: CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr=""),
    )

    geometry = media_geometry._probe_video_geometry("/rotated.mp4")
    width, height, fps = geometry

    assert (width, height) == (1080, 3840)
    assert fps == 29.97
    assert geometry.facts["coded_width"] == 1920
    assert geometry.facts["coded_height"] == 1080
    assert geometry.facts["rotation"] == 90
    assert geometry.facts["sample_aspect_ratio"] == "2:1"
    assert geometry.facts["rotation_swaps_axes"] is True
    assert geometry.facts["display_aspect_ratio"] == "16:9"
    assert geometry.facts["display_aspect_source"] == "sample_aspect_ratio"


def test_select_output_geometry_qc_exposes_rotation_sar_dar_facts(monkeypatch):
    probes = {
        "/rotated.mp4": cut.VideoGeometry(
            1080,
            1920,
            30.0,
            {
                "coded_width": 1920,
                "coded_height": 1080,
                "display_width": 1080,
                "display_height": 1920,
                "rotation": 90,
                "rotation_swaps_axes": True,
                "sample_aspect_ratio": "1:1",
                "sample_aspect_ratio_float": 1.0,
                "display_aspect_ratio": "16:9",
            },
        ),
        "/landscape.mp4": (1280, 720, 30.0),
    }
    monkeypatch.setattr(
        media_geometry, "_probe_video_geometry", lambda p: probes[str(p)]
    )
    clips = [
        {"source_id": "r", "source_path": "/rotated.mp4", "duration": 5.0},
        {"source_id": "l", "source_path": "/landscape.mp4", "duration": 1.0},
    ]

    w, h, fps, qc = media_geometry._select_output_geometry(
        ["/rotated.mp4", "/landscape.mp4"], clips
    )

    assert (w, h, fps) == (1080, 1920, 30.0)
    assert qc["orientation"] == "portrait"
    assert qc["rotation"] == 90
    assert qc["coded_width"] == 1920
    assert qc["sources"][0]["rotation"] == 0
    assert qc["sources"][1]["rotation"] == 90
    assert qc["sources"][1]["display_aspect_ratio"] == "16:9"


def test_build_edited_source_video_writes_delivery_qc_and_meta_without_visual_qc(
    monkeypatch, tmp_path
):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    plan = normalize_clip_plan(
        [{"start": 0, "end": 1}, {"start": 2, "end": 3}], video_duration=4
    )
    commands = []

    def fake_run_cmd(cmd):
        commands.append(cmd)
        joined = " ".join(cmd)
        if cmd[0] == "ffprobe" and "-of json" in joined:
            return CompletedProcess(
                cmd,
                0,
                stdout=json.dumps(
                    {
                        "streams": [
                            {
                                "width": 1280,
                                "height": 720,
                                "r_frame_rate": "30/1",
                                "sample_aspect_ratio": "1:1",
                            }
                        ]
                    }
                ),
                stderr="",
            )
        if cmd[0] == "ffprobe" and "stream=sample_rate" in joined:
            return CompletedProcess(cmd, 0, stdout="48000\n", stderr="")
        if cmd[0] == "ffprobe":
            return CompletedProcess(cmd, 0, stdout="0\n", stderr="")
        Path(cmd[-1]).write_bytes(b"mp4")
        return CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("cut_render.run_cmd", fake_run_cmd)
    monkeypatch.setattr("media_geometry.run_cmd", fake_run_cmd)
    monkeypatch.setattr("cut_render.get_video_duration", lambda path: 2.0)

    output = build_edited_source_video(video, plan, work_dir)

    delivery = json.loads(
        (work_dir / "cut_delivery_qc.json").read_text(encoding="utf-8")
    )
    validated_delivery = plan["qc"]["delivery_qc"]
    meta = json.loads(
        (work_dir / "edited_source.mp4.meta.json").read_text(encoding="utf-8")
    )
    assert output.exists()
    assert delivery == validated_delivery == meta["delivery_qc"]
    assert delivery["video_encode_passes"] == 1
    assert delivery["audio_sample_rate"] == {"target": 48000, "probed": 48000}
    assert "trim_concat_filter_requires_reencode" in delivery["reencode_reason"]
    assert delivery["stream_copy_risk"]["status"] == "avoided"
    assert delivery["output_geometry"]["width"] == 1280
    assert not (work_dir / "visual_qc.json").exists()


def test_audio_join_fade_is_in_filter_graph(monkeypatch, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    plan = normalize_clip_plan(
        [{"start": 0, "end": 1}, {"start": 2, "end": 3}], video_duration=4
    )
    commands = []

    def fake_run_cmd(cmd):
        commands.append(cmd)
        if cmd[0] == "ffprobe":
            return CompletedProcess(cmd, 0, stdout="0\n", stderr="")
        Path(cmd[-1]).write_bytes(b"mp4")
        return CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("cut_render.run_cmd", fake_run_cmd)
    monkeypatch.setattr("media_geometry.run_cmd", fake_run_cmd)
    monkeypatch.setattr("cut_render.get_video_duration", lambda path: 2.0)
    monkeypatch.setattr(
        "cut_render.CONFIG", {**cut_render.CONFIG, "clip_join_audio_fade_ms": 30.0}
    )

    build_edited_source_video(video, plan, work_dir)

    joined = " ".join([cmd for cmd in commands if cmd[0] == "ffmpeg"][0])
    assert "afade=t=in:st=0:d=0.030" in joined
    assert "afade=t=out:st=0.970:d=0.030" in joined


def test_contiguous_same_source_join_does_not_fade_inside_sentence(
    monkeypatch, tmp_path
):
    """A no-gap source continuation is one sentence stream; do not attenuate its join."""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    plan = normalize_clip_plan(
        [{"start": 0, "end": 1}, {"start": 1, "end": 2}], video_duration=2
    )
    commands = []

    def fake_run_cmd(cmd):
        commands.append(cmd)
        if cmd[0] == "ffprobe":
            return CompletedProcess(cmd, 0, stdout="0\n", stderr="")
        Path(cmd[-1]).write_bytes(b"mp4")
        return CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("cut_render.run_cmd", fake_run_cmd)
    monkeypatch.setattr("media_geometry.run_cmd", fake_run_cmd)
    monkeypatch.setattr("cut_render.get_video_duration", lambda path: 2.0)
    monkeypatch.setattr(
        "cut_render.CONFIG", {**cut_render.CONFIG, "clip_join_audio_fade_ms": 30.0}
    )
    build_edited_source_video(video, plan, work_dir)
    joined = " ".join([cmd for cmd in commands if cmd[0] == "ffmpeg"][0])
    first_audio = joined.split("[a0]", 1)[0]
    second_audio = joined.split("[a0]", 1)[1].split("[a1]", 1)[0]
    assert "afade=t=out" not in first_audio
    assert "afade=t=in" not in second_audio
    assert (
        "[0:a]atrim=start=1.000:end=2.000,asetpts=PTS-STARTPTS,afade=t=in" not in joined
    )


def test_update_cut_qc_duration_status_and_allow_drift():
    plan = {
        "clips": [{"duration": 4.0}],
        "total_duration": 4.0,
        "target_duration": 10.0,
    }

    cut.update_cut_qc(plan)
    assert plan["qc"]["target_duration_status"] == "under"
    assert plan["qc"]["blocking"][0]["code"] == "target_duration_drift"

    allowed_primary = {
        "clips": [{"duration": 4.0}],
        "total_duration": 4.0,
        "target_duration": 10.0,
    }
    cut.update_cut_qc(allowed_primary, allow_duration_drift=True)
    assert "blocking" not in allowed_primary["qc"]
    assert (
        allowed_primary["qc"]["target_duration"]["duration_drift_allowed_by"]
        == "--allow-duration-drift"
    )

    allowed_compat = {
        "clips": [{"duration": 4.0}],
        "total_duration": 4.0,
        "target_duration": 10.0,
    }
    cut.update_cut_qc(
        allowed_compat,
        allow_duration_drift=True,
        duration_drift_allowed_by="--allow-sparse-cut",
    )
    assert "blocking" not in allowed_compat["qc"]
    assert (
        allowed_compat["qc"]["target_duration"]["duration_drift_allowed_by"]
        == "--allow-sparse-cut"
    )


def test_cut_probe_video_geometry_is_rotation_sar_dar_aware(monkeypatch):
    """Cut geometry should use display geometry, not raw encoded width/height, so rotated
    portrait sources and non-square pixels feed the same canvas truth as assemble."""
    outputs = iter(
        [
            "1920,1080,30000/1001,90,1:1,9:16\n",
            "720,576,25/1,0,16:15,4:3\n",
        ]
    )

    def fake_run_cmd(cmd):
        return CompletedProcess(cmd, 0, stdout=next(outputs), stderr="")

    monkeypatch.setattr(media_geometry, "run_cmd", fake_run_cmd)

    assert media_geometry._probe_video_geometry("rotated.mp4") == (1080, 1920, 29.97)
    # 720x576 PAL with SAR 16:15 displays as 768x576 and should stay even.
    assert media_geometry._probe_video_geometry("pal_4x3.mp4") == (768, 576, 25.0)


def test_cut_probe_video_geometry_dar_does_not_override_rotated_sar_axes(monkeypatch):
    """When SAR is usable, coded dimensions × SAR are rotated; DAR is only observed."""
    payload = {
        "streams": [
            {
                "width": 1920,
                "height": 1080,
                "r_frame_rate": "30/1",
                "sample_aspect_ratio": "1:1",
                "display_aspect_ratio": "16:9",
                "tags": {"rotate": "90"},
            }
        ]
    }
    monkeypatch.setattr(
        media_geometry,
        "run_cmd",
        lambda cmd: CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr=""),
    )

    geometry = media_geometry._probe_video_geometry("rotated_with_dar.mp4")

    assert geometry == (1080, 1920, 30.0)
    assert geometry.facts["rotation_swaps_axes"] is True
    assert geometry.facts["display_aspect_ratio"] == "16:9"
    assert geometry.facts["display_aspect_source"] == "sample_aspect_ratio"
