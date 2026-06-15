import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'video-understanding' / 'scripts'))
import hashlib
import json

from brief import (CONFIG, build_agent_brief, _chunk_asr_for_writing, _load_clean_asr,
                   _load_consolidation, _format_consolidation, _clean_asr_prompt_fingerprint,
                   _index_prompt_fingerprint)

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
    assert "Understanding index (from consolidate.py)" not in text
    written = json.loads((tmp_path / "asr_writing_chunks.json").read_text(encoding="utf-8"))
    assert written == _chunk_asr_for_writing(ASR, SCENES)
    assert (tmp_path / "timeline_fusion.json").exists()


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
    monkeypatch.setitem(CONFIG, "target_segments_per_minute", 10.0)
    monkeypatch.setitem(CONFIG, "context_info", "")
    scenes = [{"scene_id": i, "start": i * 60.0, "end": i * 60.0 + 60.0, "description": "画面"} for i in range(10)]
    text = build_agent_brief(scenes, [], [], 600.0, tmp_path).read_text(encoding="utf-8")
    assert "CUT OUTPUT" in text
    assert "10 short beats" in text
    assert "100 short beats" not in text
    assert "Keep each beat INSIDE one clip" in text
