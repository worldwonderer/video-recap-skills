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

    assert "背景资料（context-only/advisory" in content
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

def test_auto_timeline_detects_validated_cut(tmp_path):
    """A bare work_dir reviews on the source timeline; a validated cut (clip_plan_validated.json
    + edited_source.mp4) auto-selects cut_output so manual review matches the orchestrator."""
    assert review._auto_timeline(tmp_path) == "source"
    (tmp_path / "clip_plan_validated.json").write_text("{}", encoding="utf-8")
    assert review._auto_timeline(tmp_path) == "source"  # plan alone is not enough
    (tmp_path / "edited_source.mp4").write_bytes(b"")
    assert review._auto_timeline(tmp_path) == "cut_output"


def _write_manifest(work_dir, edit_mode):
    (work_dir / "recap_run_manifest.json").write_text(
        json.dumps({"settings": {"edit_mode": edit_mode}}, ensure_ascii=False), encoding="utf-8")


def test_auto_timeline_legacy_single_pass_stays_source(tmp_path):
    """The legacy direct video-cut single-pass path writes a SOURCE-time narration.json next to
    an output-time narration_mapped.json. The cut artifacts are present but narration.json is NOT
    output time, so auto-detect must stay on source (else it inverts the review)."""
    (tmp_path / "clip_plan_validated.json").write_text("{}", encoding="utf-8")
    (tmp_path / "edited_source.mp4").write_bytes(b"")
    (tmp_path / "narration_mapped.json").write_text("[]", encoding="utf-8")
    assert review._auto_timeline(tmp_path) == "source"


def test_auto_timeline_trusts_manifest_edit_mode(tmp_path):
    """recap_run_manifest.json is authoritative: full mode stays source even with stale cut
    artifacts in a reused work_dir, and cut mode selects cut_output."""
    (tmp_path / "clip_plan_validated.json").write_text("{}", encoding="utf-8")
    (tmp_path / "edited_source.mp4").write_bytes(b"")
    _write_manifest(tmp_path, "full")
    assert review._auto_timeline(tmp_path) == "source"  # stale cut artifacts must not flip it
    _write_manifest(tmp_path, "cut")
    assert review._auto_timeline(tmp_path) == "cut_output"


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
    assert asr[0]["start"] == 3.0 and asr[0]["end"] == 5.0 and asr[0]["text"] == "这句在成片三到五秒"
    assert asr[0]["source_start"] == 13.0 and asr[0]["source_end"] == 15.0


def test_review_narration_cut_output_requires_fresh_validated_clip_spans(monkeypatch, tmp_path):
    (tmp_path / "narration.json").write_text(json.dumps([{"start": 1, "end": 2, "narration": "测试。"}]), encoding="utf-8")
    monkeypatch.setattr("review.api_call", lambda payload: {"choices": [{"message": {"content": "{}"}}]})

    # Advisory path fails open but writes warnings/QC; strict evidence blocks.
    out = review.review_narration(tmp_path, timeline="cut_output")
    assert out.get("warnings")
    with pytest.raises(SystemExit, match="clip_plan_validated"):
        review.review_narration(tmp_path, timeline="cut_output", strict_evidence=True)

    raw = [{"start": 10, "end": 20}]
    (tmp_path / "clip_plan.json").write_text(json.dumps(raw), encoding="utf-8")
    out = review.review_narration(tmp_path, timeline="cut_output")
    assert out.get("warnings")

    stale = {
        "raw_plan_fingerprint": "stale",
        "clips": [{"source_start": 10, "source_end": 20, "output_start": 0, "output_end": 10}],
    }
    (tmp_path / "clip_plan_validated.json").write_text(json.dumps(stale), encoding="utf-8")
    with pytest.raises(SystemExit, match="clip_plan_validated"):
        review.review_narration(tmp_path, timeline="cut_output", strict_evidence=True)


def test_review_narration_cut_output_uses_remapped_grounding(monkeypatch, tmp_path):
    (tmp_path / "narration.json").write_text(json.dumps([{"start": 3, "end": 5, "narration": "测试。"}]), encoding="utf-8")
    (tmp_path / "vlm_analysis.json").write_text(json.dumps([
        {"scene_id": 1, "start": 12, "end": 16, "description": "保留片段", "frame_facts": {}}
    ]), encoding="utf-8")
    (tmp_path / "asr_result.json").write_text(json.dumps([{ "start": 13, "end": 15, "text": "输出三到五秒对白"}]), encoding="utf-8")
    raw_plan = [{"start": 10, "end": 20}]
    (tmp_path / "clip_plan.json").write_text(json.dumps(raw_plan), encoding="utf-8")
    (tmp_path / "clip_plan_validated.json").write_text(json.dumps({
        "raw_plan_fingerprint": review.stable_hash(raw_plan),
        "clips": [{"source_start": 10, "source_end": 20, "output_start": 0, "output_end": 10}],
    }), encoding="utf-8")
    payloads = []

    def fake_api(payload):
        payloads.append(payload)
        return {"choices": [{"message": {"content": '{"verdict":"OK","summary":"ok","findings":[]}'}}]}

    monkeypatch.setattr("review.api_call", fake_api)
    review.review_narration(tmp_path, timeline="cut_output")

    content = payloads[0]["messages"][0]["content"]
    assert "[OUTPUT 3.0-5.0s 对白" in content and "输出三到五秒对白" in content
    assert "[OUTPUT 2.0-6.0s 画面" in content and "保留片段" in content
    assert "SOURCE 13.0-15.0s" in content


def test_parse_review_scorecard_is_advisory_and_keeps_verdict():
    payload = {
        "verdict": "PASS",
        "summary": "ok",
        "scorecard": {"promise_match": 5, "hook_3s": 4, "first_15s_delivery": 4, "spine_clarity": 4, "information_gain": 5},
        "hook_candidates_review": [{"candidate": "他以为赢了，其实刚入局", "type": "contrast", "score": 5, "keep": True}],
        "retention_risk_points": [{"time": "00:28", "risk": "解释太久", "fix": "插入反问"}],
        "highest_return_edits": ["露出00:31原声"],
        "information_gain_notes": [{"segment": 0, "label": "motive", "note": "补动机"}],
        "spoken_language_rewrites": [{"segment": 0, "original": "因此", "rewrite": "所以", "why": "更口语"}],
        "grounding_assertions": [{"segment": 0, "assertion": "二人是盟友", "source": "ASR", "risk": "low"}],
        "findings": [],
    }
    r = review.parse_review_response(json.dumps(payload, ensure_ascii=False))
    assert r["verdict"] == "PASS"
    assert r["scorecard"]["hook_3s"] == 4
    md = review.format_review_md(r)
    assert "Scorecard" in md and "Highest-return edits" in md and "Grounding assertions" in md


def test_parse_review_scorecard_does_not_downgrade_weak_pass():
    """De-fanged: weak scores never override the judge's PASS verdict (advisory only)."""
    payload = {
        "verdict": "PASS",
        "summary": "weak but judge passed",
        "scorecard": {"promise_match": 1, "hook_3s": 1, "first_15s_delivery": 1, "spine_clarity": 1},
        "findings": [],
    }
    r = review.parse_review_response(json.dumps(payload, ensure_ascii=False))
    assert r["verdict"] == "PASS"


def test_build_review_messages_includes_planning_artifacts_when_present(tmp_path):
    (tmp_path / "packaging_plan.json").write_text(json.dumps({"viewer_promise": "看到反转"}, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "recap_story_plan.json").write_text(json.dumps({
        "director_intent": {"pov": "女主", "dramatic_question": "他如何翻盘"},
        "beats": [{"beat_id": "b01", "change": "knowledge: doubt→proof"}],
    }, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "visual_audio_board.json").write_text(json.dumps({
        "items": [{"beat_id": "b01", "audio_owner": "silence", "narration_job": "none"}],
    }, ensure_ascii=False), encoding="utf-8")
    content = review.build_review_messages([{"start": 0, "end": 3, "narration": "测试。"}], [], [], work_dir=tmp_path)[0]["content"]

    assert "看到反转" in content
    assert "女主" in content and "他如何翻盘" in content and "knowledge: doubt→proof" in content
    assert "visual_audio_board.json" in content and '"audio_owner": "silence"' in content
    assert '"narration_job": "none"' in content
    assert "7:3 不是配额" in content


def test_parse_review_scorecard_marks_unscored_dimensions():
    """Dimensions the judge omits stay None (rendered 未评分), not a fabricated 3."""
    payload = {"verdict": "PASS", "summary": "s", "scorecard": {"hook_3s": 5}, "findings": []}
    r = review.parse_review_response(json.dumps(payload, ensure_ascii=False))
    assert r["scorecard"]["hook_3s"] == 5
    assert r["scorecard"]["tts_pacing"] is None
    md = review.format_review_md(r)
    assert "未评分" in md and "hook_3s: 5/5" in md


def test_coverage_policy_v1_covers_long_video_tail_and_bme():
    scenes = [{"scene_id": i, "start": i * 10.0, "end": i * 10.0 + 8.0, "description": f"scene{i}"} for i in range(100)]
    asr = [{"start": i * 5.0, "end": i * 5.0 + 2.0, "text": f"line{i}"} for i in range(200)]
    narration = [{"start": 850.0, "end": 860.0, "narration": "后段关键反转。"}]
    cov = review.coverage_policy_v1(scenes, asr, narration)
    ranges = cov["selected_ranges"]
    assert cov["coverage_policy_version"] == "coverage_policy_v1"
    assert any(r["start"] <= 0 <= r["end"] for r in ranges)
    assert any(r["start"] <= 500 <= r["end"] for r in ranges)
    assert any(r["start"] <= 990 <= r["end"] for r in ranges)
    assert any(r["start"] <= 855 <= r["end"] and "narration_window" in r["selection_reason"] for r in ranges)
    assert review.coverage_policy_v1(scenes, asr, narration)["selected_ranges"] == ranges


def test_evidence_bundle_labels_source_output_and_context_only():
    narration = [{"start": 3, "end": 5, "narration": "测试。"}]
    vlm = [{"scene_id": 1, "start": 2, "end": 6, "description": "保留", "source_start": 12, "source_end": 16, "output_segment_index": 0}]
    asr = [{"start": 3, "end": 5, "text": "对白", "source_start": 13, "source_end": 15, "output_segment_index": 0}]
    research = {"characters": {"叶轻眉": "主角之母"}, "character_details": {"叶轻眉": {"aliases": ["叶青眉"], "role": "背景人物"}}}
    bundle = review.build_evidence_bundle(vlm, asr, narration, timeline="cut_output", research=research)
    assert {item["clock"] for item in bundle["items"]} == {"output"}
    assert all("source_start" in item and "source_end" in item for item in bundle["items"])
    assert bundle["context_items"] and all(item["clock"] is None and item["support"] == "context_only" for item in bundle["context_items"])
    rendered = review.render_evidence_bundle(bundle)
    assert "clock=OUTPUT" in rendered and "SOURCE 12.0-16.0s" in rendered
    assert "clock=null" in rendered


def test_review_narration_cut_output_advisory_writes_warning_qc(monkeypatch, tmp_path):
    (tmp_path / "narration.json").write_text(json.dumps([{"start": 1, "end": 2, "narration": "测试。"}], ensure_ascii=False), encoding="utf-8")
    (tmp_path / "vlm_analysis.json").write_text("[]", encoding="utf-8")
    (tmp_path / "asr_result.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr("review.api_call", lambda payload: {"choices": [{"message": {"content": '{"verdict":"PASS","summary":"ok","findings":[]}'}}]})
    out = review.review_narration(tmp_path, timeline="cut_output")
    assert out["warnings"]
    qc = json.loads((tmp_path / "grounding_qc.json").read_text(encoding="utf-8"))
    assert qc["owner"] == "video-script.review"
    assert qc["verdict"] == "warn"
    assert qc["coverage_policy_version"] == "coverage_policy_v1"
    with pytest.raises(SystemExit):
        review.review_narration(tmp_path, timeline="cut_output", strict_evidence=True)


def test_merge_review_findings_dedup_keeps_highest_severity():
    merged = review.merge_review_findings([
        {"findings": [{"segment": 1, "category": "hallucination", "severity": "suggestion", "issue": "X", "fix": "a"}]},
        {"findings": [{"segment": 1, "category": "hallucination", "severity": "error", "issue": "X", "fix": "b"}]},
    ])
    assert len(merged) == 1 and merged[0]["severity"] == "error" and merged[0]["fix"] == "b"


def test_parse_review_downgrades_research_assertions_to_context_only():
    payload = {"verdict": "PASS", "summary": "ok", "grounding_assertions": [{"segment": 0, "assertion": "最终背叛", "source": "research", "risk": "spoiler"}], "findings": []}
    r = review.parse_review_response(json.dumps(payload, ensure_ascii=False))
    assert r["grounding_assertions"][0]["support"] == "context_only"
    assert r["grounding_assertions"][0]["clock"] is None



def test_parse_review_downgrades_user_context_assertions_to_context_only():
    payload = {
        "verdict": "PASS",
        "summary": "ok",
        "grounding_assertions": [
            {"segment": 0, "assertion": "用户说这是兄弟", "source": "user_context", "risk": "from prompt"}
        ],
        "findings": [],
    }
    r = review.parse_review_response(json.dumps(payload, ensure_ascii=False))
    assertion = r["grounding_assertions"][0]
    assert assertion["support"] == "context_only"
    assert assertion["clock"] is None
    assert "user_context-only" in assertion["risk"]


def test_bundle_fingerprint_failure_is_observable_in_bundle_warning():
    bundle = {"schema_version": 1, "clock": "source", "items": [], "context_items": []}
    bundle["coverage"] = bundle  # circular metadata should not be swallowed invisibly

    assert review._bundle_fingerprint(bundle) == ""
    warning = bundle["metadata"]["evidence_bundle_fingerprint_warning"]
    assert "fingerprint unavailable" in warning

    chunks = review._chunk_evidence_bundle(bundle)
    assert chunks[0]["metadata"]["evidence_bundle_fingerprint"] == ""
    assert warning in chunks[0]["warnings"]

def test_public_grounding_seams_are_api_free(tmp_path):
    narration = [{"start": 1, "end": 3, "narration": "测试。"}]
    vlm = [{"scene_id": 1, "start": 0, "end": 4, "description": "门口对峙", "frame_facts": {"1.0": ["男子握拳"]}}]
    asr = [{"start": 1, "end": 2, "text": "站住"}]

    bundle = review.build_evidence_bundle(vlm, asr, narration, research={"characters": {"甲": "背景"}})
    ranges = bundle["coverage"]["selected_ranges"]
    filtered = review.filter_evidence_by_ranges(vlm, asr, ranges)
    assert [item["source"] for item in filtered["items"]] == ["visual", "asr"]

    assert review.validate_public_evidence_contract(bundle)["valid"] is True
    assert review.build_review_coverage_metadata(bundle)["scene_count"] == 1
    assert "门口对峙" in review.render_evidence_bundle(bundle)

    qc = review.build_grounding_qc(tmp_path, {"findings": []}, bundle)
    assert qc["verdict"] == "pass"
    review.write_grounding_qc(tmp_path, qc)
    assert json.loads((tmp_path / "grounding_qc.json").read_text(encoding="utf-8"))["owner"] == "video-script.review"

def test_review_narration_chunks_large_evidence_and_merges(monkeypatch, tmp_path):
    narration = [{"start": 0, "end": 980, "narration": "全片复盘。"}]
    vlm = [{"scene_id": i, "start": i * 10.0, "end": i * 10.0 + 8.0, "description": f"scene{i}"} for i in range(130)]
    (tmp_path / "narration.json").write_text(json.dumps(narration, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "vlm_analysis.json").write_text(json.dumps(vlm, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "asr_result.json").write_text("[]", encoding="utf-8")
    payloads = []

    def fake(payload):
        payloads.append(payload)
        idx = len(payloads) - 1
        return {"choices": [{"message": {"content": json.dumps({
            "verdict": "REVISE",
            "summary": "chunk",
            "findings": [{"segment": idx, "severity": "warning", "category": "grounding_risk", "issue": f"issue{idx}", "fix": "fix"}],
        }, ensure_ascii=False)}}]}

    monkeypatch.setattr("review.api_call", fake)
    out = review.review_narration(tmp_path)
    assert len(payloads) > 1
    assert out["chunked_review"]["chunk_count"] == len(payloads)
    assert len(out["findings"]) == len(payloads)
    qc = json.loads((tmp_path / "grounding_qc.json").read_text(encoding="utf-8"))
    assert qc["review_coverage"]["time_ranges"]


def test_duplicate_source_clip_backrefs_remain_distinguishable():
    vlm = [{"scene_id": 1, "start": 10, "end": 20, "description": "同一源片段"}]
    asr = [{"start": 12, "end": 14, "text": "同一句对白"}]
    spans = [
        {"source_start": 10, "source_end": 20, "output_start": 0, "output_end": 10, "source_id": "0", "source_clip_id": "clip-a", "output_segment_index": 0},
        {"source_start": 10, "source_end": 20, "output_start": 30, "output_end": 40, "source_id": "0", "source_clip_id": "clip-b", "output_segment_index": 1},
    ]
    rv, ra = review.remap_grounding_to_output_timeline(vlm, asr, spans)
    assert len(rv) == 2 and len(ra) == 2
    assert {x["source_clip_id"] for x in rv} == {"clip-a", "clip-b"}
    assert {x["output_segment_index"] for x in ra} == {0, 1}
    bundle = review.build_evidence_bundle(rv, ra, [{"start": 0, "end": 40, "narration": "测试"}], timeline="cut_output")
    rendered = review.render_evidence_bundle(bundle)
    assert "clip#0" in rendered and "clip#1" in rendered


def test_research_only_assertion_stays_context_only_not_strong_fact(tmp_path):
    parsed = review.parse_review_response(json.dumps({
        "verdict": "PASS",
        "summary": "ok",
        "grounding_assertions": [{"segment": 0, "assertion": "角色已背叛", "source": "research", "risk": "spoiler"}],
        "findings": [],
    }, ensure_ascii=False))
    bundle = review.build_evidence_bundle([], [], [{"start": 0, "end": 1, "narration": "他已经背叛。"}], research={"characters": {"甲": "未来剧情"}})
    qc = review.build_grounding_qc(tmp_path, parsed, bundle)
    assertion = parsed["grounding_assertions"][0]
    assert assertion["support"] == "context_only" and assertion["clock"] is None
    assert qc["research_guardrail"]["spoiler_risk_assertions"] == 1


def test_build_review_messages_includes_style_and_deslop_artifacts_fail_open(tmp_path):
    (tmp_path / "packaging_plan.json").write_text(
        json.dumps({"viewer_promise": "看到反转"}, ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "style_card.json").write_text(
        json.dumps({"tone": "冷静克制", "avoid": ["空泛拔高"]}, ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "deslop_qc.json").write_text(
        json.dumps({"flags": [{"type": "template_transition", "text": "然而"}]}, ensure_ascii=False), encoding="utf-8"
    )

    content = review.build_review_messages(
        [{"start": 0, "end": 3, "narration": "测试。"}], [], [], work_dir=tmp_path
    )[0]["content"]

    assert "packaging_plan.json" in content and "看到反转" in content
    assert "style_card.json" in content and "冷静克制" in content and "空泛拔高" in content
    assert "deslop_qc.json" in content and "template_transition" in content and "然而" in content
    assert "可能为空" in content


def test_build_review_messages_bad_style_and_deslop_json_keep_context_titles(tmp_path):
    (tmp_path / "style_card.json").write_text("{bad json", encoding="utf-8")
    (tmp_path / "deslop_qc.json").write_text("[bad json", encoding="utf-8")

    content = review.build_review_messages(
        [{"start": 0, "end": 1, "narration": "测试。"}], [], [], work_dir=tmp_path
    )[0]["content"]

    assert "style_card.json" in content
    assert "deslop_qc.json" in content
    assert content.count("(无)") >= 2


def test_parse_review_clamps_new_craft_categories_to_warning_but_keeps_factual_errors():
    craft_categories = [
        "ai_flavor",
        "weak_payoff",
        "style_mismatch",
        "packaging_mismatch",
        "example_entity_leak",
    ]
    payload = {
        "verdict": "FAIL",
        "summary": "s",
        "findings": [
            {"segment": 0, "severity": "error", "category": category, "issue": category, "fix": "fix"}
            for category in craft_categories
        ] + [
            {"segment": 1, "severity": "error", "category": "hallucination", "issue": "fact", "fix": "fix"},
            {"segment": 2, "severity": "error", "category": "incomplete", "issue": "cut", "fix": "fix"},
        ],
    }

    parsed = review.parse_review_response(json.dumps(payload, ensure_ascii=False))
    severities = {item["category"]: item["severity"] for item in parsed["findings"]}

    assert all(severities[category] == "warning" for category in craft_categories)
    assert severities["hallucination"] == "error"
    assert severities["incomplete"] == "error"


def test_parse_review_scorecard_new_keys_and_omitted_dimensions_stay_none():
    payload = {
        "verdict": "PASS",
        "summary": "ok",
        "scorecard": {
            "ending_payoff": 5,
            "style_consistency": "4",
            "ai_flavor": 2.2,
            "packaging_consistency": 0,
        },
        "findings": [],
    }

    parsed = review.parse_review_response(json.dumps(payload, ensure_ascii=False))
    scorecard = parsed["scorecard"]

    assert scorecard["ending_payoff"] == 5
    assert scorecard["style_consistency"] == 4
    assert scorecard["ai_flavor"] == 2
    assert scorecard["packaging_consistency"] == 1
    assert scorecard["hook_3s"] is None
    assert scorecard["tts_pacing"] is None
