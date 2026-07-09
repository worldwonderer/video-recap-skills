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


def test_lint_block_coverage_metrics_and_warnings(monkeypatch):
    monkeypatch.setitem(CONFIG, "speech_rate", 3.5)
    monkeypatch.setitem(CONFIG, "narration_speed", 1.3)

    # Under-narrated: two tiny blocks over a long span -> coverage far below the ~0.7 target.
    sparse = lint_narration([
        {"start": 0.0, "end": 4.0, "narration": "第一句话。", "pause_after_ms": 250},
        {"start": 60.0, "end": 64.0, "narration": "很久之后的第二句话。", "pause_after_ms": 250},
    ], mode="full")
    sparse_codes = {issue["code"] for issue in sparse["warnings"]}
    assert "under_narrated" in sparse_codes
    assert sparse["metrics"]["narration_coverage"] < 0.5
    assert sparse["metrics"]["segment_count"] == 2

    # Healthy block layout: a few big blocks covering most of the span, with deliberate
    # original-audio gaps between them -> no coverage/fragmentation warnings.
    block = "范闲表面是个闲散少爷背地里却握着监察院最深的暗线这一次他押上全部身家也要查清楚母亲当年究竟为何而死"
    healthy = []
    t = 0.0
    for _ in range(6):
        healthy.append({"start": round(t, 2), "end": round(t + 12.0, 2), "narration": block, "pause_after_ms": 250})
        t += 16.0
    report = lint_narration(healthy, mode="full")
    codes = {issue["code"] for issue in report["warnings"]}
    assert "under_narrated" not in codes
    assert "no_original_blocks" not in codes
    assert "fragmented_beats" not in codes
    assert 0.45 <= report["metrics"]["narration_coverage"] <= 0.85
    assert report["metrics"]["original_block_count"] >= 2

    # cut mode -> coverage lint is skipped (measured on the mapped output timeline elsewhere)
    cut_report = lint_narration(sparse, mode="cut")
    assert cut_report["metrics"] == {}



def test_lint_narration_accepts_ascii_period_as_complete_sentence():
    report = lint_narration([
        {"start": 0.0, "end": 3.0, "narration": "It ends."},
    ], mode="full")

    assert "incomplete_sentence" not in {issue["code"] for issue in report["warnings"]}

def test_lint_flags_fragmented_beats(tmp_path, monkeypatch):
    # The forbidden pattern: many lone short sentences instead of blocks -> each synthesizes as a
    # separate choppy TTS utterance, so fragmented_beats must fire.
    monkeypatch.setitem(CONFIG, "speech_rate", 3.5)
    monkeypatch.setitem(CONFIG, "narration_speed", 1.3)
    segs = [{"start": i * 5.0, "end": i * 5.0 + 1.6, "narration": "一句短解说。"} for i in range(8)]
    report = lint_narration(segs, mode="full", work_dir=tmp_path)
    codes = {w["code"] for w in report["warnings"]}
    assert "fragmented_beats" in codes
    assert report["metrics"]["avg_block_chars"] < 16


def test_lint_flags_wall_to_wall_narration_with_no_original_blocks(monkeypatch):
    # The user's complaint: narration nearly wall-to-wall, the original never gets to breathe.
    monkeypatch.setitem(CONFIG, "speech_rate", 3.5)
    monkeypatch.setitem(CONFIG, "narration_speed", 1.3)
    rate = 3.5 * 1.3
    block = "这是一段连续不断的解说词没有给原声留下任何空隙一路讲到底"
    spoken = len(block) / rate
    segs = []
    t = 0.0
    for _ in range(6):
        segs.append({"start": round(t, 2), "end": round(t + spoken + 0.05, 2), "narration": block})
        t += spoken + 0.1                          # next block starts right after -> no original gap
    report = lint_narration(segs, mode="full")
    codes = {w["code"] for w in report["warnings"]}
    assert "no_original_blocks" in codes
    assert report["metrics"]["original_block_count"] == 0
    assert report["metrics"]["narration_coverage"] > 0.85


def test_build_agent_brief_cut_mode_sizes_to_output(monkeypatch, tmp_path):
    """Cut mode must size the beat target to the OUTPUT length, not the source.

    Regression for the 2h->30min complaint: the brief used to ask for ~source/60*spm
    beats across the whole source timeline, ~75% of which the cut then dropped.
    """
    monkeypatch.setitem(CONFIG, "edit_mode", "cut")
    monkeypatch.setitem(CONFIG, "target_duration", "1m")
    monkeypatch.setitem(CONFIG, "context_info", "")
    scenes = [{"scene_id": i, "start": i * 60.0, "end": i * 60.0 + 60.0, "description": "画面"} for i in range(10)]
    text = build_agent_brief(scenes, [], [], 600.0, tmp_path).read_text(encoding="utf-8")
    assert "CUT OUTPUT" in text
    assert "narration BLOCKS across the ~1min CUT OUTPUT" in text   # sized to 1min output (~5 blocks)
    assert "47 narration BLOCKS" not in text                        # NOT the source-sized (10min) count
    assert "step 1 of 2" in text           # A1: cut-first, write clip_plan only (no edited_source yet)
    assert '"target_duration": "1m"' in text


def test_build_agent_brief_cut_pass2_is_output_timeline(monkeypatch, tmp_path):
    """Step 6: once the cut is rendered (edited_source.mp4 exists), the cut brief switches to
    the PASS-2 output-timeline variant: narrate in OUTPUT time, with the kept clips listed."""
    monkeypatch.setitem(CONFIG, "edit_mode", "cut")
    monkeypatch.setitem(CONFIG, "target_duration", "1m")
    monkeypatch.setitem(CONFIG, "context_info", "")
    (tmp_path / "edited_source.mp4").write_bytes(b"edited")
    (tmp_path / "clip_plan_validated.json").write_text(json.dumps({"clips": [
        {"clip_id": 0, "source_start": 10.0, "source_end": 20.0, "output_start": 0.0, "output_end": 10.0, "reason": "开端"},
    ]}), encoding="utf-8")
    scenes = [{"scene_id": 0, "start": 0.0, "end": 60.0, "description": "画面"}]
    text = build_agent_brief(scenes, [], [], 600.0, tmp_path).read_text(encoding="utf-8")
    assert "step 2 of 2: write `narration.json` in OUTPUT time" in text
    assert "Kept clips on the OUTPUT timeline" in text
    assert "OUTPUT 0.0–10.0s ← SOURCE[0] 10.0–20.0s" in text
    assert "step 1 of 2" not in text




def test_build_agent_brief_cut_pass2_sizes_to_actual_validated_duration(monkeypatch, tmp_path):
    monkeypatch.setitem(CONFIG, "edit_mode", "cut")
    monkeypatch.setitem(CONFIG, "target_duration", "1m")
    monkeypatch.setitem(CONFIG, "context_info", "")
    (tmp_path / "edited_source.mp4").write_bytes(b"edited")
    (tmp_path / "clip_plan_validated.json").write_text(json.dumps({
        "clips": [
            {"source_start": 10.0, "source_end": 20.0, "output_start": 0.0, "output_end": 10.0},
            {"source_start": 40.0, "source_end": 50.0, "output_start": 10.0, "output_end": 20.0},
        ],
        "total_duration": 20.0,
    }), encoding="utf-8")

    text = build_agent_brief(
        [{"scene_id": 0, "start": 10.0, "end": 50.0, "description": "保留片段"}],
        [],
        [],
        120.0,
        tmp_path,
    ).read_text(encoding="utf-8")

    assert "across the ~20s CUT OUTPUT" in text
    assert "edited_source.mp4` (~20s)" in text
    assert "~1min" not in text

def test_build_agent_brief_thin_substrate_relaxes_density(monkeypatch, tmp_path):
    """Thin/empty substrate must turn the density target into a ceiling, not a quota,
    so the agent is not forced to fill beats with 看图说话."""
    monkeypatch.setitem(CONFIG, "edit_mode", "full")
    monkeypatch.setitem(CONFIG, "target_duration", "")
    monkeypatch.setitem(CONFIG, "context_info", "")
    scenes = [{"scene_id": i, "start": i * 6.0, "end": i * 6.0 + 6.0, "description": "画面"} for i in range(4)]
    text = build_agent_brief(scenes, [], [], 24.0, tmp_path).read_text(encoding="utf-8")
    assert "do NOT chase a beat count" in text
    assert "grounded blocks" in text                # thin -> fewer, grounded blocks (no quota)
    assert "segments/min (minimum" not in text  # the strict quota line is replaced when thin


def test_build_agent_brief_research_directive_when_context_without_research(monkeypatch, tmp_path):
    """A title/context with no background_research.json must trigger a loud research-first
    directive (the root of 'cold' narration: no story context -> only pixels to narrate)."""
    monkeypatch.setitem(CONFIG, "edit_mode", "full")
    monkeypatch.setitem(CONFIG, "target_duration", "")
    monkeypatch.setitem(CONFIG, "context_info", "这是《庆余年》第一集")
    scenes = [{"scene_id": 0, "start": 0.0, "end": 6.0, "description": "范闲登场与人对峙暗藏机锋"}]
    asr = [{"start": 1.0, "end": 5.0, "text": "一句对白。"}]
    text = build_agent_brief(scenes, asr, [], 6.0, tmp_path).read_text(encoding="utf-8")
    assert "Research the story FIRST" in text
    assert "庆余年" in text  # the context is echoed into the directive

    (tmp_path / "background_research.json").write_text('{"synopsis": "范闲查案"}', encoding="utf-8")
    text2 = build_agent_brief(scenes, asr, [], 6.0, tmp_path).read_text(encoding="utf-8")
    assert "Research the story FIRST" not in text2  # already researched -> directive gone


def test_research_directive_does_not_fire_for_dialogue_rich_titled_run(monkeypatch, tmp_path):
    """Step 3: a dialogue-rich (substrate=rich) titled run with no research file must NOT be
    nagged — the directive fires only for thin/empty substrate, not merely because a title exists."""
    monkeypatch.setitem(CONFIG, "edit_mode", "full")
    monkeypatch.setitem(CONFIG, "target_duration", "")
    monkeypatch.setitem(CONFIG, "context_info", "这是《庆余年》第一集")  # a title, but no research file
    scenes = [{"scene_id": i, "start": float(i * 6), "end": float(i * 6 + 6),
               "description": "范闲与人对峙", "frame_facts": {str(i * 6): ["对峙"]}} for i in range(4)]
    asr = [{"start": 1.0, "end": 5.0, "text": "对" * 250}]  # rich dialogue spine
    assert assess_understanding_substrate(scenes, asr)["level"] == "rich"
    text = build_agent_brief(scenes, asr, [], 24.0, tmp_path).read_text(encoding="utf-8")
    assert "Research the story FIRST" not in text  # rich + titled -> no nag


def test_lint_narration_cut_mode_warns_on_clip_boundary_crossing():
    """A beat that spills past its clip is silently trimmed by the mapper and ends up
    over cut-away footage -> warn so the agent tightens it inside the clip."""
    plan = {"clips": [{"clip_id": 0, "source_start": 10.0, "source_end": 20.0}]}
    crossing = lint_narration([
        {"start": 12.0, "end": 25.0, "narration": "跨过片段边界的解说。"},  # mid 18.5 in clip, end 25 > 20
    ], [{"scene_id": 0, "start": 0.0, "end": 30.0}], clip_plan=plan, mode="cut")
    assert "crosses_clip_boundary" in {i["code"] for i in crossing["warnings"]}

    inside = lint_narration([
        {"start": 12.0, "end": 18.0, "narration": "完全在片段内。"},
    ], [{"scene_id": 0, "start": 0.0, "end": 30.0}], clip_plan=plan, mode="cut")
    assert "crosses_clip_boundary" not in {i["code"] for i in inside["warnings"]}


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

    facts_scenes = [
        {"scene_id": i, "start": float(i), "end": float(i + 1),
         "description": "画面描述" * 6, "frame_facts": {"1.0": ["动作"]}}
        for i in range(4)
    ]
    # Rich requires a story SPINE: substantial dialogue (ASR >= 200 chars) ...
    rich = assess_understanding_substrate(facts_scenes, [{"start": 0.0, "end": 3.0, "text": "对白" * 120}])
    assert rich["level"] == "rich"
    # ... or researched/given story context lifts a frame-fact-rich clip to rich.
    storyful = assess_understanding_substrate(facts_scenes, [], has_story_context=True)
    assert storyful["level"] == "rich"
    # Frame-fact-rich but STORYLESS (no dialogue, no context) is thin, NOT rich, so the
    # cold-narration safeguards (sparse warning, research directive, density relief) fire.
    # This is the canonical anime case the old volume-only classifier mislabeled "rich".
    storyless = assess_understanding_substrate(facts_scenes, [])
    assert storyless["level"] == "thin"


def test_parse_target_seconds_table():
    """_parse_target_seconds must be total (never raise) and parse the documented forms."""
    from narration import _parse_target_seconds
    assert _parse_target_seconds("1:30") == 90.0
    assert _parse_target_seconds("00:30:00") == 1800.0
    assert _parse_target_seconds("30m") == 1800.0
    assert _parse_target_seconds("1h5m") == 3900.0
    assert _parse_target_seconds("600") == 600.0
    assert _parse_target_seconds(90) == 90.0
    for bad in ("", None, "abc", "0", "-5", "10x", "1:-30", "1:2:3:4", "  ", "nan", "inf"):
        assert _parse_target_seconds(bad) is None


def test_build_agent_brief_storyless_rich_video_relaxes_and_prompts_research(monkeypatch, tmp_path):
    """End-to-end for the anime complaint: a frame-fact-rich but storyless video (no
    dialogue, no research) must now be treated as thin so the density relaxes and the
    research directive fires — instead of being graded 'rich' and shipping cold."""
    monkeypatch.setitem(CONFIG, "edit_mode", "full")
    monkeypatch.setitem(CONFIG, "target_duration", "")
    monkeypatch.setitem(CONFIG, "context_info", "")
    scenes = [
        {"scene_id": i, "start": float(i * 6), "end": float(i * 6 + 6),
         "description": "人物在画面里走动" * 3, "frame_facts": {str(i * 6): ["走动"]}}
        for i in range(6)
    ]
    text = build_agent_brief(scenes, [], [], 36.0, tmp_path).read_text(encoding="utf-8")
    assert "do NOT chase a beat count" in text          # density relaxed (FIX D)
    assert "Research the story FIRST" in text            # research directive (FIX E)
    assert "segments/min (minimum" not in text           # strict quota line suppressed


def test_build_agent_brief_rich_density_is_a_guide_not_quota(monkeypatch, tmp_path):
    """Step 1: even with RICH substrate, density is framed as a GUIDE, not a hard quota,
    so the writer never pads with pixel-filler just to hit a beat count."""
    monkeypatch.setitem(CONFIG, "edit_mode", "full")
    monkeypatch.setitem(CONFIG, "target_duration", "")
    monkeypatch.setitem(CONFIG, "context_info", "")
    scenes = [{"scene_id": i, "start": float(i * 6), "end": float(i * 6 + 6),
               "description": "范闲在书房翻看卷宗神色凝重", "frame_facts": {str(i * 6): ["翻书"]}} for i in range(6)]
    asr = [{"start": 1.0, "end": 5.0, "text": "对" * 250}]  # >= 200 chars -> a real story spine -> rich
    assert assess_understanding_substrate(scenes, asr)["level"] == "rich"
    text = build_agent_brief(scenes, asr, [], 36.0, tmp_path).read_text(encoding="utf-8")
    assert "Narration in BLOCKS, ~7:3" in text       # block model is the headline guidance
    assert "never pad with" in text                  # still framed as a guide, not a quota
    assert "Narration density target:" not in text  # the old hard-quota phrasing is gone


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
        "raw_plan_fingerprint": validate.stable_hash(raw_payload),
        "clips": [{"clip_id": 0, "source_start": 40.0, "source_end": 50.0}],
    }), encoding="utf-8")

    plan = validate._load_cut_clip_plan(tmp_path)

    assert plan["clips"][0]["source_start"] == 40.0


def test_cut_output_duration_bounds_rejects_segments_outside_output_timeline():
    import sys
    import importlib.util

    validate_path = Path(__file__).resolve().parents[2] / "skills" / "video-script" / "scripts" / "validate.py"
    spec = importlib.util.spec_from_file_location("video_script_validate_bounds_under_test", validate_path)
    validate = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = validate
    spec.loader.exec_module(validate)

    validate._validate_output_timeline_bounds([
        {"start": 0.0, "end": 9.95, "narration": "有效。"},
    ], output_duration=10.0)

    bad = [
        {"start": -0.1, "end": 1.0, "narration": "负时间。"},
        {"start": 9.0, "end": 10.2, "narration": "超出时长。"},
        {"start": 10.1, "end": 11.0, "narration": "完全在外。"},
    ]
    with pytest.raises(SystemExit) as exc:
        validate._validate_output_timeline_bounds(bad, output_duration=10.0)

    msg = str(exc.value)
    assert "output_duration=10.000" in msg
    assert "segment 0" in msg and "segment 1" in msg and "segment 2" in msg


def test_cut_output_mode_requires_output_duration(tmp_path):
    import sys
    import importlib.util

    validate_path = Path(__file__).resolve().parents[2] / "skills" / "video-script" / "scripts" / "validate.py"
    spec = importlib.util.spec_from_file_location("video_script_validate_required_duration_under_test", validate_path)
    validate = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = validate
    spec.loader.exec_module(validate)

    (tmp_path / "narration.json").write_text(
        json.dumps([{"start": 0.0, "end": 1.0, "narration": "有效。"}]),
        encoding="utf-8",
    )
    old_argv = sys.argv
    try:
        sys.argv = ["validate.py", "--work-dir", str(tmp_path), "--mode", "cut_output"]
        with pytest.raises(SystemExit, match="--output-duration is required"):
            validate.main()
    finally:
        sys.argv = old_argv


def test_cut_output_duration_bounds_rejects_non_finite_duration():
    import sys
    import importlib.util

    validate_path = Path(__file__).resolve().parents[2] / "skills" / "video-script" / "scripts" / "validate.py"
    spec = importlib.util.spec_from_file_location("video_script_validate_finite_duration_under_test", validate_path)
    validate = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = validate
    spec.loader.exec_module(validate)

    with pytest.raises(SystemExit, match="finite and positive"):
        validate._validate_output_timeline_bounds([{"start": 0.0, "end": 1.0}], output_duration=float("nan"))
    with pytest.raises(SystemExit, match="non-finite time"):
        validate._validate_output_timeline_bounds([{"start": float("nan"), "end": 1.0}], output_duration=10.0)


def test_cut_pass2_agent_brief_writes_output_time_evidence(monkeypatch, tmp_path):
    monkeypatch.setitem(CONFIG, "edit_mode", "cut")
    monkeypatch.setitem(CONFIG, "target_duration", "10s")
    monkeypatch.setitem(CONFIG, "context_info", "")
    raw_plan = {"clips": [{"start": 100.0, "end": 110.0}]}
    (tmp_path / "clip_plan.json").write_text(json.dumps(raw_plan, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "clip_plan_validated.json").write_text(json.dumps({
        "raw_plan_fingerprint": narration.stable_hash(raw_plan),
        "clips": [{
            "source_start": 100.0,
            "source_end": 110.0,
            "output_start": 0.0,
            "output_end": 10.0,
        }],
    }, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "edited_source.mp4").write_bytes(b"edited")
    asr_payload = [{"start": 101.0, "end": 105.0, "text": "输出一到五秒对白。"}]
    (tmp_path / "asr_result.json").write_text(json.dumps(asr_payload, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "asr_clean.json").write_text(json.dumps({
        "segments": [{"start": 101.0, "end": 105.0, "text": "清洗后一到五秒对白。"}],
        "source_md5": __import__("hashlib").md5((tmp_path / "asr_result.json").read_bytes()).hexdigest(),
        "model": narration._consolidation_model(),
        "prompt_md5": narration._clean_asr_prompt_fingerprint(),
    }, ensure_ascii=False), encoding="utf-8")

    brief = build_agent_brief(
        [{"scene_id": 7, "start": 100.0, "end": 110.0, "description": "保留片段",
          "frame_facts": {"102.0": ["抬头"]}}],
        asr_payload,
        [{"start": 106.0, "end": 108.0, "duration": 2.0, "has_speech": False}],
        120.0,
        tmp_path,
    )

    chunks = json.loads((tmp_path / "asr_writing_chunks.json").read_text(encoding="utf-8"))
    fusion = json.loads((tmp_path / "timeline_fusion.json").read_text(encoding="utf-8"))
    text = brief.read_text(encoding="utf-8")

    assert chunks[0]["start"] == pytest.approx(1.0)
    assert chunks[0]["end"] == pytest.approx(5.0)
    assert chunks[0]["text"] == "清洗后一到五秒对白。"
    assert fusion[0]["time_range"] == [0.0, 10.0]
    assert fusion[0]["dialogue_segments"][0]["start"] == pytest.approx(1.0)
    assert fusion[0]["dialogue_segments"][0]["end"] == pytest.approx(5.0)
    assert fusion[0]["narration_slots"][0]["start"] == pytest.approx(6.0)
    assert "ASR chunk 1: 1.0-5.0s" in text
    assert "ASR chunk 1: 101.0-105.0s" not in text


def test_cut_pass2_agent_brief_requires_fresh_output_spans(monkeypatch, tmp_path):
    monkeypatch.setitem(CONFIG, "edit_mode", "cut")
    monkeypatch.setitem(CONFIG, "target_duration", "10s")
    monkeypatch.setitem(CONFIG, "context_info", "")
    (tmp_path / "edited_source.mp4").write_bytes(b"edited")

    with pytest.raises(SystemExit, match="cut pass2 brief requires fresh clip_plan_validated.json"):
        build_agent_brief(
            [{"scene_id": 7, "start": 100.0, "end": 110.0, "description": "保留片段"}],
            [{"start": 101.0, "end": 105.0, "text": "源时间对白。"}],
            [],
            120.0,
            tmp_path,
        )


def test_cut_pass2_agent_brief_rejects_non_finite_output_spans(monkeypatch, tmp_path):
    monkeypatch.setitem(CONFIG, "edit_mode", "cut")
    monkeypatch.setitem(CONFIG, "target_duration", "10s")
    monkeypatch.setitem(CONFIG, "context_info", "")
    (tmp_path / "edited_source.mp4").write_bytes(b"edited")
    (tmp_path / "clip_plan_validated.json").write_text(json.dumps({
        "clips": [{
            "source_start": 100.0,
            "source_end": float("nan"),
            "output_start": 0.0,
            "output_end": 10.0,
        }],
    }), encoding="utf-8")

    with pytest.raises(SystemExit, match="non-finite clip span"):
        build_agent_brief(
            [{"scene_id": 7, "start": 100.0, "end": 110.0, "description": "保留片段"}],
            [{"start": 101.0, "end": 105.0, "text": "源时间对白。"}],
            [],
            120.0,
            tmp_path,
        )
