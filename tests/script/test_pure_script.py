import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'video-script' / 'scripts'))
import json
import narration
import pytest  # noqa: F401
from subprocess import CompletedProcess  # noqa: F401
from lib import CONFIG
from narration import _align_narration_to_quiet, _build_timeline_fusion, _post_dedup_narration, _text_char_count, assess_understanding_substrate, build_agent_brief, lint_narration


def test_text_char_count():
    assert _text_char_count("hello") == 5
    assert _text_char_count("你好世界") == 4
    assert _text_char_count("") == 0


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


def test_lint_narration_rejects_empty_file(tmp_path):
    report = lint_narration([], work_dir=tmp_path)

    assert report["ok"] is False
    assert any(issue["code"] == "empty_narration_file" for issue in report["errors"])
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


def test_lint_narration_density_metrics_and_warnings(monkeypatch):
    monkeypatch.setitem(CONFIG, "target_segments_per_minute", 9.6)
    monkeypatch.setitem(CONFIG, "min_segments_per_minute", 6.24)
    monkeypatch.setitem(CONFIG, "max_narration_gap_seconds", 11.0)

    sparse = lint_narration([
        {"start": 0.0, "end": 4.0, "narration": "第一句。", "pause_after_ms": 250},
        {"start": 40.0, "end": 44.0, "narration": "很久之后的第二句。", "pause_after_ms": 250},
    ], mode="full")
    sparse_codes = {issue["code"] for issue in sparse["warnings"]}
    assert "low_density" in sparse_codes
    assert "long_gap" in sparse_codes
    assert sparse["metrics"]["segment_count"] == 2
    assert sparse["metrics"]["max_gap_seconds"] == 36.0

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

    cut_report = lint_narration(sparse, mode="cut")
    assert cut_report["metrics"] == {}


def test_align_narration_to_quiet_sets_overlap_flag_without_moving_beats(monkeypatch):
    """New contract: keep the agent's timing; only (re)compute overlaps_speech."""
    scenes = [{"scene_id": 0, "start": 0.0, "end": 12.0}]
    monkeypatch.setitem(CONFIG, "quiet_overlap_min_ratio", 0.8)

    # Beat fully inside a quiet window -> overlaps_speech False, timing untouched.
    inside = _align_narration_to_quiet([
        {"start": 2.5, "end": 5.5, "narration": "完全落在安静窗口里。"},
    ], scenes, [{"start": 2.0, "end": 6.0, "duration": 4.0, "has_speech": False}])
    assert inside[0]["start"] == 2.5
    assert inside[0]["end"] == 5.5
    assert inside[0]["overlaps_speech"] is False

    # Beat mostly outside the quiet window -> overlaps_speech True, timing untouched
    # (the old code would have shifted it; we no longer move it off the picture).
    outside = _align_narration_to_quiet([
        {"start": 0.0, "end": 4.0, "narration": "大部分都在对白区。"},
    ], scenes, [{"start": 3.0, "end": 6.0, "duration": 3.0, "has_speech": False}])
    assert outside[0]["start"] == 0.0
    assert outside[0]["end"] == 4.0
    assert outside[0]["overlaps_speech"] is True


def test_align_narration_to_quiet_never_blanks_agent_text(monkeypatch):
    """Regression: the old gap-cascade could blank a squeezed segment to '' and drop it."""
    scenes = [{"scene_id": 0, "start": 0.0, "end": 12.0}]
    monkeypatch.setitem(CONFIG, "quiet_overlap_min_ratio", 0.8)
    result = _align_narration_to_quiet([
        {"start": 0.0, "end": 5.0, "narration": "他终于回来了。"},
        {"start": 5.3, "end": 9.5, "narration": "屋里气氛骤然变冷。"},
    ], scenes, [{"start": 0.5, "end": 2.0, "duration": 1.5, "has_speech": False}])
    assert len(result) == 2
    assert all(seg["narration"].strip() for seg in result)


def test_post_dedup_keeps_distinct_short_beats(monkeypatch):
    """Parallel short beats sharing common chars must not be merged (threshold raised to >0.6)."""
    narration = [
        {"start": 0.0, "end": 4.0, "narration": "他不再试探。", "overlaps_speech": True},
        {"start": 4.3, "end": 8.0, "narration": "他直接赌上全力。", "overlaps_speech": True},
    ]
    result = _post_dedup_narration([dict(n) for n in narration])
    assert len(result) == 2


def test_agent_brief_includes_mimo_video_overview(monkeypatch, tmp_path):
    monkeypatch.setitem(CONFIG, "mimo_video_overview", True)
    monkeypatch.setitem(CONFIG, "mimo_video_chunk_max_seconds", 20.0)
    monkeypatch.setitem(CONFIG, "mimo_video_chunk_min_seconds", 1.0)
    chunks = [{"chunk_id": 0, "scene_id": 0, "start": 0.0, "end": 3.0, "content": "这是 MiMo 对分片汇总的故事线概览。"}]
    overview = {
        "input": "scene_chunks",
        "content": "这是 MiMo 对分片汇总的故事线概览。",
        "reasoning_content": "内部推理",
        "chunks": chunks,
        "chunks_fingerprint": narration._mimo_cached_chunks_fingerprint(chunks),
        "settings": narration._mimo_video_settings_fingerprint(),
    }
    overview["overview_fingerprint"] = narration._mimo_overview_payload_fingerprint(overview)
    (tmp_path / "mimo_video_overview.json").write_text(
        json.dumps(overview, ensure_ascii=False),
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


def test_build_agent_brief_injects_background_research(monkeypatch, tmp_path):
    monkeypatch.setitem(CONFIG, "edit_mode", "full")
    monkeypatch.setitem(CONFIG, "target_duration", "")
    monkeypatch.setitem(CONFIG, "context_info", "")
    (tmp_path / "background_research.json").write_text(
        json.dumps({
            "synopsis": "少年范闲深夜查案。",
            "characters": {"范闲": "主角", "五竹": "范闲的护卫"},
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    brief = build_agent_brief(
        [{"scene_id": 0, "start": 0.0, "end": 3.0, "description": "夜路", "frame_facts": {"1.0": ["走路"]}}],
        [{"start": 0.0, "end": 3.0, "text": "你终于来了"}],
        [],
        3.0,
        tmp_path,
    )
    text = brief.read_text(encoding="utf-8")
    assert "Story context" in text
    assert "五竹" in text
    assert "范闲的护卫" in text


def test_build_agent_brief_warns_on_empty_substrate(monkeypatch, tmp_path):
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
    assert "SUBSTRATE IS EMPTY" in text


def test_timeline_fusion_aligns_scenes_dialogue_and_quiet_slots():
    fusion = _build_timeline_fusion(
        [{"scene_id": 0, "start": 0.0, "end": 10.0, "description": "对峙", "frame_facts": {"1.0": ["看门"]}}],
        [
            {"start": 2.0, "end": 4.0, "text": "你到底是谁"},
            {"start": 8.0, "end": 12.0, "text": "跨场对白"},
        ],
        [
            {"start": 0.0, "end": 1.0, "duration": 1.0, "has_speech": False},
            {"start": 5.0, "end": 7.0, "duration": 2.0, "has_speech": False},
            {"start": 9.0, "end": 9.5, "duration": 0.5, "has_speech": True},
        ],
    )

    item = fusion[0]
    assert item["dialogue_overlap_seconds"] == 4.0
    assert [seg["text"] for seg in item["dialogue_segments"]] == ["你到底是谁", "跨场对白"]
    assert [(slot["start"], slot["end"]) for slot in item["narration_slots"]] == [(0.0, 1.0), (5.0, 7.0)]
    assert item["frame_facts"] == {"1.0": ["看门"]}


def test_assess_understanding_substrate_levels():
    empty = assess_understanding_substrate(
        [{"scene_id": 0, "start": 0.0, "end": 3.0, "description": "短"}], []
    )
    assert empty["level"] == "empty"
    rich = assess_understanding_substrate(
        [
            {"scene_id": i, "start": float(i), "end": float(i + 1),
             "description": "画面描述" * 6, "frame_facts": {"1.0": ["动作"]}}
            for i in range(4)
        ],
        [{"start": 0.0, "end": 3.0, "text": "对白" * 60}],
    )
    assert rich["level"] == "rich"


def test_cut_validate_prefers_raw_plan_when_validated_is_stale(tmp_path):
    import sys
    import importlib.util

    validate_path = Path(__file__).resolve().parents[2] / "skills" / "video-script" / "scripts" / "validate.py"
    spec = importlib.util.spec_from_file_location("video_script_validate_under_test", validate_path)
    validate = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = validate
    spec.loader.exec_module(validate)

    raw = tmp_path / "clip_plan.json"
    validated = tmp_path / "clip_plan_validated.json"
    raw.write_text(json.dumps({"clips": [{"start": 40.0, "end": 50.0}]}), encoding="utf-8")
    validated.write_text(json.dumps({"clips": [{"clip_id": 0, "source_start": 0.0, "source_end": 10.0}]}), encoding="utf-8")

    plan = validate._load_cut_clip_plan(tmp_path)

    assert plan["clips"][0]["start"] == 40.0


def test_cut_validate_uses_validated_plan_when_raw_fingerprint_matches(tmp_path):
    import sys
    import importlib.util

    validate_path = Path(__file__).resolve().parents[2] / "skills" / "video-script" / "scripts" / "validate.py"
    spec = importlib.util.spec_from_file_location("video_script_validate_fresh_under_test", validate_path)
    validate = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = validate
    spec.loader.exec_module(validate)

    raw_payload = {"clips": [{"start": 40.0, "end": 50.0}]}
    raw = tmp_path / "clip_plan.json"
    validated = tmp_path / "clip_plan_validated.json"
    raw.write_text(json.dumps(raw_payload), encoding="utf-8")
    validated.write_text(json.dumps({
        "raw_plan_fingerprint": validate._value_fingerprint(raw_payload),
        "clips": [{"clip_id": 0, "source_start": 40.0, "source_end": 50.0}],
    }), encoding="utf-8")

    plan = validate._load_cut_clip_plan(tmp_path)

    assert plan["clips"][0]["source_start"] == 40.0
