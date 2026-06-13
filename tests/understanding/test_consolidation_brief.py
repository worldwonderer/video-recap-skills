import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'video-understanding' / 'scripts'))
import hashlib
import json

from brief import (build_agent_brief, _chunk_asr_for_writing, _load_clean_asr,
                   _load_consolidation, _format_consolidation)

SCENES = [{"scene_id": 0, "start": 0.0, "end": 6.0, "description": "门口对峙"}]
ASR = [{"start": 1.0, "end": 5.0, "text": "第一句对白。第二句反击。"}]
SILENCE = [{"start": 0.0, "end": 1.0, "duration": 1.0, "has_speech": False}]


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
    (tmp_path / "understanding_index.json").write_text(json.dumps(
        {"characters": [{"name": "张三", "description": "主角"}], "relationships": [],
         "plot_points": ["开端"], "entities": ["匕首"]}), encoding="utf-8")
    text = build_agent_brief(SCENES, ASR, SILENCE, 6.0, tmp_path).read_text(encoding="utf-8")
    assert "Understanding index (from consolidate.py)" in text
    assert "张三" in text and "匕首" in text


def test_consolidation_loaders_are_safe_when_absent():
    assert _load_consolidation("/nonexistent-dir") == {}
    assert _format_consolidation({}) == []


def test_clean_asr_accepted_when_fresh_provenance_timing_ok(tmp_path):
    (tmp_path / "asr_result.json").write_text(json.dumps(ASR), encoding="utf-8")
    src_md5 = hashlib.md5((tmp_path / "asr_result.json").read_bytes()).hexdigest()
    (tmp_path / "asr_clean.json").write_text(json.dumps(
        {"source_md5": src_md5,
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
        {"source_md5": src_md5, "segments": [{"start": 99.0, "end": 100.0, "text": "x"}]}), encoding="utf-8")
    assert _load_clean_asr(tmp_path, ASR) is None
    # absent asr_clean.json -> None
    (tmp_path / "asr_clean.json").unlink()
    assert _load_clean_asr(tmp_path, ASR) is None
