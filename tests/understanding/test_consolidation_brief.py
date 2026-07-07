import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'video-understanding' / 'scripts'))
import hashlib
import json

from brief import (CONFIG, build_agent_brief, _chunk_asr_for_writing, _load_clean_asr,
                   _load_consolidation, _format_consolidation, _clean_asr_prompt_fingerprint,
                   _index_prompt_fingerprint, _load_optional_stage_status)

SCENES = [{"scene_id": 0, "start": 0.0, "end": 6.0, "description": "门口对峙"}]
ASR = [{"start": 1.0, "end": 5.0, "text": "第一句对白。第二句反击。"}]
SILENCE = [{"start": 0.0, "end": 1.0, "duration": 1.0, "has_speech": False}]


def _write_index_with_meta(work_dir, index, scenes=SCENES):
    (work_dir / "vlm_analysis.json").write_text(json.dumps(scenes), encoding="utf-8")
    src_md5 = hashlib.md5((work_dir / "vlm_analysis.json").read_bytes()).hexdigest()
    (work_dir / "understanding_index.json").write_text(json.dumps(index), encoding="utf-8")
    (work_dir / "understanding_index.json.meta.json").write_text(json.dumps({
        "schema_version": 1,
        "source_md5": src_md5,
        "scene_count": len(scenes),
        "model": CONFIG.get("vlm_model", ""),
        "prompt_md5": _index_prompt_fingerprint(),
    }), encoding="utf-8")


def test_brief_noop_without_consolidation(tmp_path):
    """GOLDEN: with no consolidation artifacts, the brief gains no index section and
    asr chunking uses RAW asr (byte-identical to the pre-consolidate behavior)."""
    brief_path = build_agent_brief(SCENES, ASR, SILENCE, 6.0, tmp_path)
    text = brief_path.read_text(encoding="utf-8")
    requirements = json.loads((tmp_path / "deslop_qc_requirements.json").read_text(encoding="utf-8"))
    assert requirements == {
        "schema_version": 1,
        "owner": "video-understanding.brief",
        "style_card_required": True,
        "packaging_plan_expected": True,
        "deslop_qc": {
            "report_only": True,
            "aigc_detector": False,
            "auto_rewrite": False,
        },
    }
    assert "Understanding index (from consolidate.py)" not in text
    written = json.loads((tmp_path / "asr_writing_chunks.json").read_text(encoding="utf-8"))
    assert written == _chunk_asr_for_writing(ASR, SCENES)
    assert (tmp_path / "timeline_fusion.json").exists()


def test_chunk_asr_tolerates_mixed_int_str_scene_ids():
    """Regression (cut-mode pass2): _remap_brief_evidence_to_output_timeline gives a SPLIT
    scene a str id like '5.0' while unsplit scenes keep int ids. A chunk spanning both must
    not crash sorted(current_scene_ids) with 'int < str'."""
    scenes = [
        {"scene_id": 5, "start": 0.0, "end": 3.0, "description": "a"},
        {"scene_id": "5.0", "start": 3.0, "end": 6.0, "description": "b"},
    ]
    asr = [{"start": 1.0, "end": 5.0, "text": "一句横跨两个场景的较长原声对白内容。"}]
    chunks = _chunk_asr_for_writing(asr, scenes)  # must not raise TypeError
    ids = chunks[0]["scene_ids"]
    assert 5 in ids and "5.0" in ids  # both id types survive the type-safe sort


def test_optional_stage_warnings_surface_failed_overview_and_consolidation(tmp_path):
    (tmp_path / "mimo_video_overview.status.json").write_text(json.dumps({
        "stage": "mimo_video_overview",
        "enabled": True,
        "status": "failed",
        "message": "quota timeout with stack trace that should not be repeated" * 5,
        "artifact": None,
    }), encoding="utf-8")
    (tmp_path / "consolidation.status.json").write_text(json.dumps({
        "stage": "consolidation",
        "enabled": True,
        "do_asr": False,
        "do_index": True,
        "status": "failed",
        "message": "index api failed",
        "artifacts": [],
    }), encoding="utf-8")

    text = build_agent_brief(SCENES, ASR, SILENCE, 6.0, tmp_path, mimo_overview_enabled=True).read_text(encoding="utf-8")

    assert "Optional stage warnings" in text
    assert "mimo_video_overview: failed" in text
    assert "consolidation: failed" in text
    assert "quota timeout" in text


def test_optional_stage_warnings_flag_missing_enabled_artifacts(tmp_path):
    (tmp_path / "mimo_video_overview.status.json").write_text(json.dumps({
        "stage": "mimo_video_overview",
        "enabled": True,
        "status": "ok",
        "message": "ok",
        "artifact": "mimo_video_overview.json",
    }), encoding="utf-8")
    (tmp_path / "consolidation.status.json").write_text(json.dumps({
        "stage": "consolidation",
        "enabled": True,
        "do_asr": False,
        "do_index": True,
        "status": "ok",
        "message": "ok",
        "artifacts": ["understanding_index.json"],
    }), encoding="utf-8")

    text = build_agent_brief(SCENES, ASR, SILENCE, 6.0, tmp_path, mimo_overview_enabled=True).read_text(encoding="utf-8")

    assert "mimo_video_overview: missing_artifact" in text
    assert "consolidation: missing_index" in text


def test_optional_stage_status_loader_is_defensive(tmp_path):
    assert _load_optional_stage_status(tmp_path, "missing.status.json") == {}
    (tmp_path / "bad.status.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert _load_optional_stage_status(tmp_path, "bad.status.json") == {}


def test_brief_folds_in_index_when_present(tmp_path):
    _write_index_with_meta(tmp_path, {"characters": [{"name": "张三", "description": "主角"}],
                                      "relationships": [], "plot_points": ["开端"], "entities": ["匕首"]})
    text = build_agent_brief(SCENES, ASR, SILENCE, 6.0, tmp_path).read_text(encoding="utf-8")
    assert "Understanding index (from consolidate.py)" in text
    assert "张三" in text and "匕首" in text


def test_brief_rejects_stale_index_without_matching_vlm_provenance(tmp_path):
    stale_index = {"characters": [{"name": "旧角色", "description": "旧素材"}],
                   "relationships": [], "plot_points": [], "entities": []}
    (tmp_path / "vlm_analysis.json").write_text(json.dumps(SCENES), encoding="utf-8")
    (tmp_path / "understanding_index.json").write_text(json.dumps(stale_index), encoding="utf-8")
    (tmp_path / "understanding_index.json.meta.json").write_text(json.dumps({
        "schema_version": 1,
        "source_md5": "deadbeef",
        "scene_count": len(SCENES),
        "model": CONFIG.get("vlm_model", ""),
        "prompt_md5": _index_prompt_fingerprint(),
    }), encoding="utf-8")

    text = build_agent_brief(SCENES, ASR, SILENCE, 6.0, tmp_path).read_text(encoding="utf-8")

    assert "Understanding index (from consolidate.py)" not in text
    assert "旧角色" not in text


def test_consolidation_loaders_are_safe_when_absent():
    assert _load_consolidation("/nonexistent-dir") == {}
    assert _format_consolidation({}) == []


def test_clean_asr_accepted_when_fresh_provenance_timing_ok(tmp_path):
    (tmp_path / "asr_result.json").write_text(json.dumps(ASR), encoding="utf-8")
    src_md5 = hashlib.md5((tmp_path / "asr_result.json").read_bytes()).hexdigest()
    (tmp_path / "asr_clean.json").write_text(json.dumps(
        {"source_md5": src_md5,
         "model": CONFIG.get("vlm_model", ""),
         "prompt_md5": _clean_asr_prompt_fingerprint(),
         "segments": [{"start": 1.0, "end": 5.0, "text": "第一句对白。第二句反击。CLEANED"}]}), encoding="utf-8")
    got = _load_clean_asr(tmp_path, ASR)
    assert got is not None and got[0]["text"].endswith("CLEANED")


def test_clean_asr_rejected_on_bad_provenance_and_mistiming(tmp_path):
    (tmp_path / "asr_result.json").write_text(json.dumps(ASR), encoding="utf-8")
    # wrong provenance -> None
    (tmp_path / "asr_clean.json").write_text(json.dumps(
        {"source_md5": "deadbeef", "segments": [{"start": 1.0, "end": 5.0, "text": "x"}]}), encoding="utf-8")
    assert _load_clean_asr(tmp_path, ASR) is None
    # correct provenance but mis-timed span -> None (timing guard)
    src_md5 = hashlib.md5((tmp_path / "asr_result.json").read_bytes()).hexdigest()
    (tmp_path / "asr_clean.json").write_text(json.dumps(
        {"source_md5": src_md5,
         "model": CONFIG.get("vlm_model", ""),
         "prompt_md5": _clean_asr_prompt_fingerprint(),
         "segments": [{"start": 99.0, "end": 100.0, "text": "x"}]}), encoding="utf-8")
    assert _load_clean_asr(tmp_path, ASR) is None
    # absent asr_clean.json -> None
    (tmp_path / "asr_clean.json").unlink()
    assert _load_clean_asr(tmp_path, ASR) is None



def test_brief_rejects_index_when_model_or_prompt_provenance_differs(monkeypatch, tmp_path):
    _write_index_with_meta(tmp_path, {"characters": [{"name": "旧模型角色", "description": "旧模型"}],
                                      "relationships": [], "plot_points": [], "entities": []})
    monkeypatch.setitem(CONFIG, "vlm_model", "different-model")

    text = build_agent_brief(SCENES, ASR, SILENCE, 6.0, tmp_path).read_text(encoding="utf-8")

    assert "旧模型角色" not in text
    assert "Understanding index (from consolidate.py)" not in text


def test_clean_asr_rejected_when_model_or_prompt_provenance_differs(monkeypatch, tmp_path):
    (tmp_path / "asr_result.json").write_text(json.dumps(ASR), encoding="utf-8")
    src_md5 = hashlib.md5((tmp_path / "asr_result.json").read_bytes()).hexdigest()
    (tmp_path / "asr_clean.json").write_text(json.dumps({
        "source_md5": src_md5,
        "model": "old-model",
        "prompt_md5": _clean_asr_prompt_fingerprint(),
        "segments": [{"start": 1.0, "end": 5.0, "text": "旧模型清洗。"}],
    }), encoding="utf-8")

    assert _load_clean_asr(tmp_path, ASR) is None

    (tmp_path / "asr_clean.json").write_text(json.dumps({
        "source_md5": src_md5,
        "model": CONFIG.get("vlm_model", ""),
        "prompt_md5": "deadbeef",
        "segments": [{"start": 1.0, "end": 5.0, "text": "旧提示清洗。"}],
    }), encoding="utf-8")

    assert _load_clean_asr(tmp_path, ASR) is None


def test_brief_ignores_stale_mimo_overview_when_disabled_or_chunk_mismatch(monkeypatch, tmp_path):
    monkeypatch.setitem(CONFIG, "mimo_video_overview", False)
    monkeypatch.setitem(CONFIG, "mimo_video_chunk_max_seconds", 2)
    monkeypatch.setitem(CONFIG, "mimo_video_chunk_min_seconds", 0.5)
    (tmp_path / "mimo_video_overview.json").write_text(json.dumps({
        "input": "scene_chunks",
        "content": "STALE MIMO OVERVIEW",
        "chunks": [{"chunk_id": 0, "scene_id": 99, "start": 0.0, "end": 2.0, "content": "STALE"}],
        "settings": {
            "model": CONFIG.get("mimo_video_model") or CONFIG.get("mimo_model") or CONFIG.get("vlm_model"),
            "mimo_video_fps": CONFIG.get("mimo_video_fps", 2.0),
            "mimo_media_resolution": CONFIG.get("mimo_media_resolution", "default"),
            "mimo_video_chunk_max_seconds": 2,
            "mimo_video_chunk_min_seconds": 0.5,
            "mimo_video_base64_max_mb": CONFIG.get("mimo_video_base64_max_mb", 45.0),
            "mimo_video_prompt": CONFIG.get("mimo_video_prompt", ""),
            "mimo_disable_thinking": CONFIG.get("mimo_disable_thinking", True),
        },
    }), encoding="utf-8")

    disabled_text = build_agent_brief(SCENES, ASR, SILENCE, 6.0, tmp_path).read_text(encoding="utf-8")
    assert "STALE MIMO OVERVIEW" not in disabled_text

    monkeypatch.setitem(CONFIG, "mimo_video_overview", True)
    mismatch_text = build_agent_brief(SCENES, ASR, SILENCE, 6.0, tmp_path).read_text(encoding="utf-8")
    assert "STALE MIMO OVERVIEW" not in mismatch_text


def test_build_agent_brief_cut_mode_sizes_to_output(monkeypatch, tmp_path):
    """Cut mode sizes the beat target to the OUTPUT, not the source (brief.py copy)."""
    monkeypatch.setitem(CONFIG, "edit_mode", "cut")
    monkeypatch.setitem(CONFIG, "target_duration", "1m")
    monkeypatch.setitem(CONFIG, "context_info", "")
    scenes = [{"scene_id": i, "start": i * 60.0, "end": i * 60.0 + 60.0, "description": "画面"} for i in range(10)]
    text = build_agent_brief(scenes, [], [], 600.0, tmp_path).read_text(encoding="utf-8")
    assert "CUT OUTPUT" in text
    assert "narration BLOCKS across the ~1min CUT OUTPUT" in text   # sized to 1min output
    assert "47 narration BLOCKS" not in text                        # NOT the source-sized (10min) count
    assert "step 1 of 2" in text           # A1: cut-first, write clip_plan only (no edited_source yet)


def test_cut_pass1_brief_encourages_cold_open_but_preserves_story_spine(monkeypatch, tmp_path):
    monkeypatch.setitem(CONFIG, "edit_mode", "cut")
    monkeypatch.setitem(CONFIG, "target_duration", "1m")
    monkeypatch.setitem(CONFIG, "context_info", "")
    text = build_agent_brief(SCENES, ASR, SILENCE, 60.0, tmp_path).read_text(encoding="utf-8")

    assert "0–1 optional cold-open/high-impact clip" in text
    assert "story spine, not unordered highlights" in text
    assert "cold_open" in text and "setup" in text and "turn" in text and "payoff" in text
    assert "not a flat highlights reel" in text


def test_build_agent_brief_preserves_freeform_style_and_artifact_contract(tmp_path):
    style = "悬疑冷幽默，但每句都像朋友复盘：别端着，保留东北味儿"

    text = build_agent_brief(SCENES, ASR, SILENCE, 6.0, tmp_path, style=style).read_text(encoding="utf-8")

    assert f"- Style (--style, freeform verbatim guidance): {style}" in text
    assert "Do not translate `--style` into a preset, enum, switch, or fallback ladder" in text
    assert "style_card.json" in text
    assert "packaging_plan.json" in text
    assert "deterministic report-only tool QC" in text
    assert "not treat it as an AIGC detector" in text
    assert "do not auto-rewrite" in text
    assert "not a preset enum, fixed taxonomy" in text


def test_understanding_brief_does_not_leak_hardcoded_example_entities(tmp_path):
    text = build_agent_brief(SCENES, ASR, SILENCE, 6.0, tmp_path, style="纪实复盘").read_text(encoding="utf-8")

    for leaked in ["范闲", "监察院", "五竹", "京都"]:
        assert leaked not in text
