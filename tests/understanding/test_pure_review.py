import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'video-understanding' / 'scripts'))
import json  # noqa: F401
import pytest  # noqa: F401
from lib import CONFIG
from brief import _chunk_asr_for_writing, build_agent_brief


def test_asr_chunks_split_on_sentences_and_track_scene_ids(monkeypatch):
    monkeypatch.setitem(CONFIG, "asr_chunk_min_chars", 8)
    monkeypatch.setitem(CONFIG, "asr_chunk_max_chars", 16)
    chunks = _chunk_asr_for_writing(
        [{
            "start": 0.0,
            "end": 40.0,
            "text": "第一句很重要。第二句继续推进。第三句制造悬念。第四句收尾。",
        }],
        [
            {"scene_id": 0, "start": 0.0, "end": 20.0},
            {"scene_id": 1, "start": 20.0, "end": 40.0},
        ],
    )

    assert len(chunks) >= 2
    assert chunks[0]["text"].endswith("。")
    assert all(chunk["char_count"] <= 16 for chunk in chunks)
    assert chunks[0]["scene_ids"] == [0]
    assert chunks[-1]["scene_ids"] == [1]


def test_agent_brief_writes_asr_chunks_and_timeline_fusion(monkeypatch, tmp_path):
    monkeypatch.setitem(CONFIG, "edit_mode", "full")
    monkeypatch.setitem(CONFIG, "target_duration", "")
    monkeypatch.setitem(CONFIG, "context_info", "")
    monkeypatch.setitem(CONFIG, "asr_chunk_min_chars", 5)
    monkeypatch.setitem(CONFIG, "asr_chunk_max_chars", 12)

    brief = build_agent_brief(
        [{"scene_id": 0, "start": 0.0, "end": 6.0, "description": "门口对峙"}],
        [{"start": 1.0, "end": 5.0, "text": "第一句对白。第二句反击。"}],
        [{"start": 0.0, "end": 1.0, "duration": 1.0, "has_speech": False}],
        6.0,
        tmp_path,
    )

    text = brief.read_text(encoding="utf-8")
    assert "ASR writing chunks" in text
    assert "Timeline fusion" in text
    assert (tmp_path / "asr_writing_chunks.json").exists()
    assert (tmp_path / "timeline_fusion.json").exists()
    fusion = json.loads((tmp_path / "timeline_fusion.json").read_text(encoding="utf-8"))
    assert fusion[0]["dialogue_segments"][0]["text"] == "第一句对白。第二句反击。"
