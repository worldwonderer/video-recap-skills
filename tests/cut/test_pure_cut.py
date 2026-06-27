import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'video-cut' / 'scripts'))
import types
import pytest  # noqa: F401
from subprocess import CompletedProcess  # noqa: F401
import cut
from cut import build_edited_source_video, lint_mapped_narration, map_narration_to_clips, normalize_clip_plan, parse_duration_seconds, snap_clip_ends_to_lines, snap_clips_off_shot_changes, source_time_to_output_time


def test_lint_mapped_narration_flags_dropped_and_sparse():
    """Post-map re-lint must surface beats dropped by the mapper and a sparse cut output
    (the otherwise-invisible half of cut-mode desync)."""
    mapped = [
        {"start": 5.0, "end": 8.0, "narration": "一。"},
        {"start": 50.0, "end": 53.0, "narration": "二。"},
    ]
    report = lint_mapped_narration(mapped, original_count=8, output_duration=60.0)
    codes = {w["code"] for w in report["warnings"]}
    assert "many_beats_dropped" in codes          # 6/8 dropped
    assert "low_density_output" in codes          # 2 beats over 60s
    assert "long_gap_output" in codes             # 42s gap between the two beats
    assert report["dropped"] == 6
    assert report["drop_ratio"] == 0.75

    dense = [{"start": float(i * 6), "end": float(i * 6 + 4), "narration": "一句。"} for i in range(10)]
    healthy = lint_mapped_narration(dense, original_count=10, output_duration=60.0)
    assert healthy["warnings"] == []


def test_lint_mapped_narration_blocking_and_clamped_flags():
    """Step 4: heavy drop/sparse output is BLOCKING; clamped-but-kept beats are surfaced
    (advisory, not blocking)."""
    sparse = [{"start": 5.0, "end": 8.0, "narration": "一。"}, {"start": 50.0, "end": 53.0, "narration": "二。"}]
    assert lint_mapped_narration(sparse, original_count=8, output_duration=60.0)["blocking"] is True

    dense = [{"start": float(i * 6), "end": float(i * 6 + 4), "narration": "一句。"} for i in range(10)]
    assert lint_mapped_narration(dense, original_count=10, output_duration=60.0)["blocking"] is False

    clamped = [{"start": 0.0, "end": 4.0, "narration": "一。", "clamped": True},
               {"start": 5.0, "end": 9.0, "narration": "二。", "clamped": False}]
    rep = lint_mapped_narration(clamped, original_count=2, output_duration=12.0)
    assert rep["clamped_count"] == 1
    assert any(w["code"] == "clamped_beats" for w in rep["warnings"])
    assert rep["blocking"] is False  # clamp alone (no drop/sparse) does not block


def test_map_narration_to_clips_tags_clamped_beats():
    """Step 4: a beat trimmed to a clip edge is tagged clamped (its text may describe cut footage)."""
    plan = {"clips": [{"clip_id": 0, "source_start": 10.0, "source_end": 20.0,
                       "output_start": 0.0, "output_end": 10.0}]}
    mapped = map_narration_to_clips([
        {"start": 12.0, "end": 18.0, "narration": "完全在片段内。"},   # not clamped
        {"start": 15.0, "end": 22.0, "narration": "尾巴越界被裁。"},   # mid 18.5 in clip, end 22>20 -> clamped to 20
    ], plan)
    by_text = {m["narration"]: m for m in mapped}
    assert by_text["完全在片段内。"]["clamped"] is False
    assert by_text["尾巴越界被裁。"]["clamped"] is True


def test_cut_main_normalize_only_writes_validated_plan_without_render(monkeypatch, tmp_path):
    """Step 4: --normalize-only writes clip_plan_validated.json and skips the render/map."""
    import sys
    import json as _json
    import cut
    video = tmp_path / "v.mp4"
    video.write_bytes(b"v")
    (tmp_path / "clip_plan.json").write_text('{"clips":[{"start":10.0,"end":20.0}]}', encoding="utf-8")
    monkeypatch.setattr("cut.get_video_duration", lambda p: 30.0)
    rendered = []
    monkeypatch.setattr("cut.build_edited_source_video", lambda *a, **k: rendered.append(1))
    monkeypatch.setattr(sys, "argv", ["cut.py", str(video), "--work-dir", str(tmp_path), "--normalize-only"])

    cut.main()

    validated = _json.loads((tmp_path / "clip_plan_validated.json").read_text(encoding="utf-8"))
    assert validated["clips"]
    assert rendered == []                                   # no render in normalize-only
    assert not (tmp_path / "edited_source.mp4").exists()


def test_cut_main_blocks_on_heavy_drop_unless_allow_sparse(monkeypatch, tmp_path):
    """Step 4: a cut whose narration mostly falls outside the kept clips FAILS the preflight
    (before TTS), unless --allow-sparse-cut is given."""
    import sys
    import json as _json
    import pytest as _pytest
    import cut
    video = tmp_path / "v.mp4"
    video.write_bytes(b"v")
    (tmp_path / "clip_plan.json").write_text('{"clips":[{"start":10.0,"end":20.0}]}', encoding="utf-8")
    (tmp_path / "narration.json").write_text(_json.dumps([
        {"start": 12.0, "end": 15.0, "narration": "片段内。"},
        {"start": 100.0, "end": 103.0, "narration": "片段外一。"},
        {"start": 110.0, "end": 113.0, "narration": "片段外二。"},
        {"start": 120.0, "end": 123.0, "narration": "片段外三。"},
    ]), encoding="utf-8")
    monkeypatch.setattr("cut.get_video_duration", lambda p: 200.0)
    monkeypatch.setattr("cut.should_reuse_edited_source", lambda *a, **k: False)
    monkeypatch.setattr("cut.build_edited_source_video",
                        lambda *a, **k: (tmp_path / "edited_source.mp4").write_bytes(b"e"))
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


# ---------------------------------------------------------------------------
# snap_clip_ends_to_lines tests
# ---------------------------------------------------------------------------

def _make_plan(clips_spec, allow_overlap=False, video_duration=100.0):
    """Build a validated plan from a list of (source_start, source_end) tuples."""
    raw = [{"start": s, "end": e} for s, e in clips_spec]
    return normalize_clip_plan(raw, video_duration=video_duration, allow_overlap=allow_overlap)


def test_snap_extends_mid_speech_clip_to_next_quiet_window():
    """A clip ending mid-speech is extended to the next quiet-window start."""
    silence = [{"start": 12.0, "end": 13.0, "duration": 1.0}]
    plan = _make_plan([(0.0, 10.0)])
    snapped = snap_clip_ends_to_lines(plan, silence, video_duration=100.0, max_extend=5.0)
    assert snapped["clips"][0]["source_end"] == 12.0
    assert snapped["clips"][0]["duration"] == 12.0
    assert snapped["total_duration"] == 12.0


def test_snap_no_snap_when_end_already_in_quiet_window():
    """No extension when source_end already sits inside a quiet window."""
    silence = [{"start": 9.5, "end": 11.0, "duration": 1.5}]
    plan = _make_plan([(0.0, 10.0)])  # 10.0 is inside [9.5, 11.0]
    snapped = snap_clip_ends_to_lines(plan, silence, video_duration=100.0, max_extend=5.0)
    assert snapped["clips"][0]["source_end"] == 10.0  # unchanged


def test_snap_caps_at_max_extend_when_next_quiet_too_far():
    """When the next quiet window is beyond max_extend, no extension is applied."""
    silence = [{"start": 20.0, "end": 21.0, "duration": 1.0}]
    plan = _make_plan([(0.0, 10.0)])
    # next quiet at 20.0 is 10s away; max_extend=2.0 → candidate=12.0 but pause is at 20>12 → no snap
    snapped = snap_clip_ends_to_lines(plan, silence, video_duration=100.0, max_extend=2.0)
    assert snapped["clips"][0]["source_end"] == 10.0  # unchanged


def test_snap_caps_at_video_duration():
    """Extension never exceeds video_duration."""
    silence = [{"start": 99.0, "end": 100.0, "duration": 1.0}]
    plan = _make_plan([(0.0, 98.0)], video_duration=100.0)
    snapped = snap_clip_ends_to_lines(plan, silence, video_duration=100.0, max_extend=5.0)
    assert snapped["clips"][0]["source_end"] <= 100.0
    assert snapped["clips"][0]["source_end"] == 99.0


def test_snap_no_overlap_when_allow_overlap_false():
    """Extension is capped to avoid crossing into a later clip's source range."""
    silence = [{"start": 25.0, "end": 26.0, "duration": 1.0}]
    # clip 0: 0-10, clip 1: 20-30 → extending clip 0 toward pause at 25 would enter clip 1
    plan = _make_plan([(0.0, 10.0), (20.0, 30.0)], allow_overlap=False)
    snapped = snap_clip_ends_to_lines(plan, silence, video_duration=100.0, max_extend=20.0)
    # candidate_end = min(25.0, 10+20, 100) = 20.0; but clip 1 starts at 20.0 → capped to 20.0
    # 20.0 > 10.0 so extension is applied up to the boundary
    assert snapped["clips"][0]["source_end"] == 20.0
    # clip 1 is untouched
    assert snapped["clips"][1]["source_start"] == 20.0
    assert snapped["clips"][1]["source_end"] == 30.0


def test_snap_empty_silence_returns_plan_unchanged():
    """Empty silence_periods → plan returned byte-identical (no modifications)."""
    plan = _make_plan([(0.0, 10.0), (20.0, 30.0)])
    snapped_empty = snap_clip_ends_to_lines(plan, [], video_duration=100.0, max_extend=2.0)
    snapped_none = snap_clip_ends_to_lines(plan, None, video_duration=100.0, max_extend=2.0)
    assert snapped_empty is plan
    assert snapped_none is plan


def test_snap_recomputes_output_timeline_for_all_clips():
    """After snapping clip 0, the following clip's output_start shifts correctly."""
    # clip 0: 0-10, clip 1: 20-30; quiet at 12 within max_extend=5
    silence = [{"start": 12.0, "end": 13.0, "duration": 1.0}]
    plan = _make_plan([(0.0, 10.0), (20.0, 30.0)])
    snapped = snap_clip_ends_to_lines(plan, silence, video_duration=100.0, max_extend=5.0)

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
    snapped = snap_clip_ends_to_lines(plan, silence, video_duration=100.0, max_extend=5.0)
    assert snapped["clips"][0]["source_end"] == 12.0  # the well-formed row still snaps


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
    monkeypatch.setattr(cut, "_detect_shot_changes", _fake_detector([10.3]))
    plan = _make_plan([(10.0, 30.0)])
    snapped = snap_clips_off_shot_changes(plan, "v.mp4", video_duration=100.0, margin=0.5, threshold=0.4)
    c = snapped["clips"][0]
    assert c["source_start"] == 10.3 and c["source_end"] == 30.0
    assert c["duration"] == 19.7
    assert c["output_start"] == 0.0 and c["output_end"] == 19.7


def test_scene_cut_snap_pulls_end_back_before_late_change(monkeypatch):
    """A shot-change just before source_end (next-shot sliver) pulls source_end onto the cut."""
    monkeypatch.setattr(cut, "_detect_shot_changes", _fake_detector([29.7]))
    plan = _make_plan([(10.0, 30.0)])
    snapped = snap_clips_off_shot_changes(plan, "v.mp4", video_duration=100.0, margin=0.5, threshold=0.4)
    c = snapped["clips"][0]
    assert c["source_start"] == 10.0 and c["source_end"] == 29.7


def test_scene_cut_snap_leaves_clean_boundaries_untouched(monkeypatch):
    """A cut in the middle of the clip (far from both boundaries) triggers no snap."""
    monkeypatch.setattr(cut, "_detect_shot_changes", _fake_detector([20.0]))
    plan = _make_plan([(10.0, 30.0)])
    snapped = snap_clips_off_shot_changes(plan, "v.mp4", video_duration=100.0, margin=0.5, threshold=0.4)
    c = snapped["clips"][0]
    assert c["source_start"] == 10.0 and c["source_end"] == 30.0


def test_scene_cut_snap_skips_when_snap_would_collapse_clip(monkeypatch):
    """On a clip too short to keep min_keep after snapping, the boundary is left as-is (no collapse)."""
    monkeypatch.setattr(cut, "_detect_shot_changes", _fake_detector([10.4]))
    plan = _make_plan([(10.0, 10.6)])  # 0.6s clip, change at 10.4 inside both windows
    snapped = snap_clips_off_shot_changes(plan, "v.mp4", video_duration=100.0, margin=0.5, threshold=0.4)
    c = snapped["clips"][0]
    assert c["source_start"] == 10.0 and c["source_end"] == 10.6  # unchanged, still a valid clip
    assert c["duration"] == 0.6


def test_scene_cut_snap_recomputes_output_across_multiple_clips(monkeypatch):
    """Output timeline is repacked cursor-based after the source ranges shrink."""
    monkeypatch.setattr(cut, "_detect_shot_changes", _fake_detector([10.3, 49.6]))
    plan = _make_plan([(10.0, 20.0), (40.0, 50.0)])
    snapped = snap_clips_off_shot_changes(plan, "v.mp4", video_duration=100.0, margin=0.5, threshold=0.4)
    a, b = snapped["clips"]
    assert a["source_start"] == 10.3 and a["output_start"] == 0.0 and a["output_end"] == 9.7
    assert b["source_end"] == 49.6 and b["output_start"] == 9.7
    assert snapped["total_duration"] == round(9.7 + 9.6, 3)


def test_detect_shot_changes_offsets_by_seek_and_filters_window(monkeypatch):
    """pts_time is rebased to the seek target, so seek+pts recovers absolute time; cuts outside
    [win_start, win_end] (e.g. the seek/keyframe artifact) are dropped."""
    stderr = "frame showinfo pts_time:1.762\n frame pts_time:5.866 \n frame pts_time:0.05\n"
    monkeypatch.setattr(cut, "subprocess",
                        types.SimpleNamespace(run=lambda *a, **k: CompletedProcess(a, 0, "", stderr)))
    out = cut._detect_shot_changes("v.mkv", 55.0, 68.0, 0.4)  # seek=54.75
    assert out == [56.512, 60.616]  # 54.8 (54.75+0.05) is < win_start → filtered


def test_normalize_multi_source_clip_plan_maps_sources_and_validates_per_source_overlap(tmp_path):
    manifest = {
        "sources": [
            {"source_id": "a", "source_path": str(tmp_path / "a.mp4"), "duration": 10.0},
            {"source_id": "b", "source_path": str(tmp_path / "b.mp4"), "duration": 5.0},
        ]
    }
    plan = cut.normalize_multi_source_clip_plan([
        {"source_id": "a", "start": 1.0, "end": 4.0, "reason": "A"},
        {"source_id": "b", "start": 4.0, "end": 8.0, "reason": "B"},
        {"source_id": "b", "start": 0.0, "end": 1.0, "reason": "B2"},
    ], manifest, clip_padding=0.5)

    assert plan["total_duration"] == 7.0
    assert [c["source_id"] for c in plan["clips"]] == ["a", "b", "b"]
    assert plan["clips"][0]["source_path"].endswith("a.mp4")
    assert plan["clips"][0]["source_start"] == 0.5
    assert plan["clips"][1]["source_end"] == 5.0  # clamped to source b duration
    assert plan["clips"][2]["output_start"] == 5.5

    with pytest.raises(ValueError, match="source_id a"):
        cut.normalize_multi_source_clip_plan([
            {"source_id": "a", "start": 1.0, "end": 4.0},
            {"source_id": "a", "start": 3.5, "end": 5.0},
        ], manifest)
    with pytest.raises(ValueError, match="missing source_id"):
        cut.normalize_multi_source_clip_plan([
            {"start": 1.0, "end": 2.0},
        ], manifest)
    with pytest.raises(ValueError, match="unknown source_id"):
        cut.normalize_multi_source_clip_plan([
            {"source_id": "missing", "start": 1.0, "end": 2.0},
        ], manifest)


def test_build_edited_source_video_multi_source_uses_multiple_inputs_and_cache_meta(monkeypatch, tmp_path):
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(b"aaa")
    b.write_bytes(b"bbb")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    plan = cut.normalize_multi_source_clip_plan([
        {"source_id": "a", "start": 0, "end": 1},
        {"source_id": "b", "start": 2, "end": 3},
        {"source_id": "a", "start": 4, "end": 5},
    ], {"sources": [
        {"source_id": "a", "source_path": str(a), "duration": 10},
        {"source_id": "b", "source_path": str(b), "duration": 10},
    ]})
    commands = []

    def fake_run_cmd(cmd):
        commands.append(cmd)
        if cmd[0] == "ffprobe":
            return CompletedProcess(cmd, 0, stdout="", stderr="")
        Path(cmd[-1]).write_bytes(b"edited")
        return CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("cut.run_cmd", fake_run_cmd)
    monkeypatch.setattr("cut.get_video_duration", lambda path: 3.0)

    out = cut.build_edited_source_video("ignored.mp4", plan, work_dir)

    ffmpeg_cmd = [cmd for cmd in commands if cmd[0] == "ffmpeg"][0]
    assert ffmpeg_cmd.count("-i") == 3  # two media inputs + generated silent audio
    joined = " ".join(ffmpeg_cmd)
    assert f"-i {a}" in joined and f"-i {b}" in joined
    assert "[0:v]trim=start=0.000:end=1.000" in joined
    assert "[1:v]trim=start=2.000:end=3.000" in joined
    assert "[0:v]trim=start=4.000:end=5.000" in joined
    assert "concat=n=3" in joined
    assert cut.should_reuse_edited_source(out, plan, "ignored.mp4") is True
