import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'video-script' / 'scripts'))
import json
import pytest

import review


def test_parse_review_handles_fenced_raw_and_garbage():
    fenced = ('```json\n{"verdict":"REVISE","summary":"s","findings":'
              '[{"segment":0,"severity":"error","category":"hallucination","issue":"i","fix":"f"}]}\n```')
    r = review.parse_review_response(fenced)
    assert r["verdict"] == "REVISE"
    assert r["findings"][0]["category"] == "hallucination"
    assert review.parse_review_response('{"verdict":"OK","summary":"g","findings":[]}')["verdict"] == "OK"
    junk = review.parse_review_response("no json here")
    assert junk["verdict"] == "REVISE" and junk.get("parse_error")


def test_parse_review_normalizes_bad_severity_and_category_and_verdict():
    r = review.parse_review_response('{"verdict":"weird","findings":[{"severity":"BOGUS","category":"nope","issue":"i"}]}')
    assert r["verdict"] == "REVISE"
    assert r["findings"][0]["severity"] == "warning"
    assert r["findings"][0]["category"] == "other"


def test_build_review_messages_includes_draft_and_grounding():
    narration = [{"start": 1.0, "end": 4.0, "narration": "他下定决心。", "overlaps_speech": True}]
    vlm = [{"scene_id": 0, "start": 0, "end": 5, "description": "门口对峙", "frame_facts": [{"fact": "男子握紧拳头"}]}]
    asr = [{"start": 1, "end": 4, "text": "你给我站住"}]
    content = review.build_review_messages(narration, vlm, asr)[0]["content"]
    assert "他下定决心" in content and "门口对峙" in content
    assert "你给我站住" in content and "握紧拳头" in content


def test_build_review_messages_includes_bounded_research_context(tmp_path):
    (tmp_path / "background_research.json").write_text(json.dumps({
        "synopsis": "范闲卷入监察院暗线。",
        "episode_context": "本集他第一次公开试探对手。",
        "worldbuilding": "庆国朝堂暗流涌动。",
        "characters": {f"角色{i}": f"简介{i}" for i in range(20)},
        "character_details": {
            "范闲": {"role": "主角", "aliases": ["小范大人"], "relationships": ["与五竹互相信任"]},
        },
        "plot_arcs": [
            {"name": f"线索{i}", "description": f"描述{i}", "status": "进行中"}
            for i in range(12)
        ],
        "cultural_notes": [{"item": "夜宴", "explanation": "权力试探"}],
        "noise": "x" * 5000,
    }, ensure_ascii=False), encoding="utf-8")

    content = review.build_review_messages(
        [{"start": 1.0, "end": 4.0, "narration": "他开始反击。"}],
        [],
        [],
        work_dir=tmp_path,
    )[0]["content"]

    assert "背景资料（与画面/对白并列的有效依据：被其支撑的事实不算幻觉，仅与全部证据矛盾才算）" in content
    assert "范闲卷入监察院暗线" in content
    assert "角色0：简介0" in content
    assert "角色12" not in content
    assert "线索7：描述7 [进行中]" in content
    assert "线索8" not in content
    assert "noise" not in content


def test_review_narration_passes_background_research_to_reviewer(monkeypatch, tmp_path):
    (tmp_path / "narration.json").write_text(json.dumps([{"start": 1, "end": 4, "narration": "测试。"}]), encoding="utf-8")
    (tmp_path / "vlm_analysis.json").write_text("[]", encoding="utf-8")
    (tmp_path / "asr_result.json").write_text("[]", encoding="utf-8")
    (tmp_path / "background_research.json").write_text(json.dumps({"synopsis": "主角秘密查案"}, ensure_ascii=False), encoding="utf-8")
    payloads = []

    def fake_api(payload):
        payloads.append(payload)
        return {"choices": [{"message": {"content": '{"verdict":"OK","summary":"ok","findings":[]}'}}]}

    monkeypatch.setattr("review.api_call", fake_api)
    review.review_narration(tmp_path)

    assert "主角秘密查案" in payloads[0]["messages"][0]["content"]


def test_review_narration_writes_artifacts(monkeypatch, tmp_path):
    (tmp_path / "narration.json").write_text(json.dumps([{"start": 1, "end": 4, "narration": "测试。"}]), encoding="utf-8")
    (tmp_path / "vlm_analysis.json").write_text("[]", encoding="utf-8")
    (tmp_path / "asr_result.json").write_text("[]", encoding="utf-8")
    fake = {"choices": [{"message": {"content": (
        '{"verdict":"REVISE","summary":"需加钩子","findings":'
        '[{"segment":0,"severity":"warning","category":"weak_hook","issue":"开头平淡","fix":"加悬念"}]}')}}]}
    monkeypatch.setattr("review.api_call", lambda payload: fake)
    r = review.review_narration(tmp_path)
    assert r["verdict"] == "REVISE"
    assert (tmp_path / "narration_review.json").exists()
    md = (tmp_path / "narration_review.md").read_text(encoding="utf-8")
    assert "weak_hook" in md and "需加钩子" in md


def test_review_reads_dict_frame_facts():
    """frame_facts is a dict {ts:[actions]} (vlm.py). The reviewer must surface those
    actions as grounding (regression guard for the list-as-dict silent-drop bug)."""
    narration = [{"start": 1.0, "end": 4.0, "narration": "他下定决心。"}]
    vlm = [{"scene_id": 0, "start": 0, "end": 5, "description": "门口对峙",
            "frame_facts": {"2.0": ["男子握紧拳头"], "4.0": ["女子后退一步"]}}]
    content = review.build_review_messages(narration, vlm, [])[0]["content"]
    assert "男子握紧拳头" in content and "女子后退一步" in content




def test_review_scene_grounding_tolerates_non_numeric_frame_fact_keys():
    content = review.build_review_messages(
        [{"start": 0.0, "end": 1.0, "narration": "测试。"}],
        [{"scene_id": 0, "start": 0, "end": 2, "description": "门口对峙",
          "frame_facts": {"intro": ["非数字锚点"], "1.0": ["数字锚点"]}}],
        [],
    )[0]["content"]

    assert "非数字锚点" in content
    assert "数字锚点" in content

def test_cut_output_review_remaps_grounding_to_output_timeline():
    spans = [{"source_start": 10.0, "source_end": 20.0, "output_start": 0.0, "output_end": 10.0}]
    vlm, asr = review.remap_grounding_to_output_timeline(
        [{"scene_id": 1, "start": 12.0, "end": 16.0, "description": "保留片段", "frame_facts": {"14.0": ["关键动作"], "21.0": ["剪掉动作"]}}],
        [
            {"start": 13.0, "end": 15.0, "text": "这句在成片三到五秒"},
            {"start": 25.0, "end": 26.0, "text": "被剪掉"},
        ],
        spans,
    )

    assert vlm[0]["start"] == 2.0
    assert vlm[0]["end"] == 6.0
    assert vlm[0]["frame_facts"] == {"4.000": ["关键动作"]}
    assert asr == [{"start": 3.0, "end": 5.0, "text": "这句在成片三到五秒"}]


def test_review_narration_cut_output_requires_fresh_validated_clip_spans(monkeypatch, tmp_path):
    (tmp_path / "narration.json").write_text(json.dumps([{"start": 1, "end": 2, "narration": "测试。"}]), encoding="utf-8")
    monkeypatch.setattr("review.api_call", lambda payload: {"choices": [{"message": {"content": "{}"}}]})

    with pytest.raises(SystemExit, match="clip_plan_validated"):
        review.review_narration(tmp_path, timeline="cut_output")

    raw = [{"start": 10, "end": 20}]
    (tmp_path / "clip_plan.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(SystemExit, match="clip_plan_validated"):
        review.review_narration(tmp_path, timeline="cut_output")

    stale = {
        "raw_plan_fingerprint": "stale",
        "clips": [{"source_start": 10, "source_end": 20, "output_start": 0, "output_end": 10}],
    }
    (tmp_path / "clip_plan_validated.json").write_text(json.dumps(stale), encoding="utf-8")
    with pytest.raises(SystemExit, match="clip_plan_validated"):
        review.review_narration(tmp_path, timeline="cut_output")


def test_review_narration_cut_output_uses_remapped_grounding(monkeypatch, tmp_path):
    (tmp_path / "narration.json").write_text(json.dumps([{"start": 3, "end": 5, "narration": "测试。"}]), encoding="utf-8")
    (tmp_path / "vlm_analysis.json").write_text(json.dumps([
        {"scene_id": 1, "start": 12, "end": 16, "description": "保留片段", "frame_facts": {}}
    ]), encoding="utf-8")
    (tmp_path / "asr_result.json").write_text(json.dumps([{ "start": 13, "end": 15, "text": "输出三到五秒对白"}]), encoding="utf-8")
    raw_plan = [{"start": 10, "end": 20}]
    (tmp_path / "clip_plan.json").write_text(json.dumps(raw_plan), encoding="utf-8")
    (tmp_path / "clip_plan_validated.json").write_text(json.dumps({
        "raw_plan_fingerprint": review._value_fingerprint(raw_plan),
        "clips": [{"source_start": 10, "source_end": 20, "output_start": 0, "output_end": 10}],
    }), encoding="utf-8")
    payloads = []

    def fake_api(payload):
        payloads.append(payload)
        return {"choices": [{"message": {"content": '{"verdict":"OK","summary":"ok","findings":[]}'}}]}

    monkeypatch.setattr("review.api_call", fake_api)
    review.review_narration(tmp_path, timeline="cut_output")

    content = payloads[0]["messages"][0]["content"]
    assert "[3-5s] 输出三到五秒对白" in content
    assert "[场景1 2-6s] 保留片段" in content
