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


def test_understanding_brief_cut_pass2_writes_output_time_evidence(monkeypatch, tmp_path):
    monkeypatch.setitem(CONFIG, "edit_mode", "cut")
    monkeypatch.setitem(CONFIG, "target_duration", "10s")
    monkeypatch.setitem(CONFIG, "context_info", "")
    (tmp_path / "edited_source.mp4").write_bytes(b"edited")
    (tmp_path / "clip_plan_validated.json").write_text(json.dumps({
        "clips": [{
            "source_start": 100.0,
            "source_end": 110.0,
            "output_start": 0.0,
            "output_end": 10.0,
        }],
    }, ensure_ascii=False), encoding="utf-8")

    brief = build_agent_brief(
        [{"scene_id": 7, "start": 100.0, "end": 110.0, "description": "保留片段"}],
        [{"start": 101.0, "end": 105.0, "text": "输出一到五秒对白。"}],
        [{"start": 106.0, "end": 108.0, "duration": 2.0, "has_speech": False}],
        120.0,
        tmp_path,
    )

    chunks = json.loads((tmp_path / "asr_writing_chunks.json").read_text(encoding="utf-8"))
    fusion = json.loads((tmp_path / "timeline_fusion.json").read_text(encoding="utf-8"))
    text = brief.read_text(encoding="utf-8")
    assert chunks[0]["start"] == pytest.approx(1.0)
    assert chunks[0]["end"] == pytest.approx(5.0)
    assert fusion[0]["time_range"] == [0.0, 10.0]
    assert fusion[0]["narration_slots"][0]["start"] == pytest.approx(6.0)
    assert "ASR chunk 1: 1.0-5.0s" in text
    assert "ASR chunk 1: 101.0-105.0s" not in text




def test_understanding_brief_cut_pass2_sizes_to_actual_validated_duration(monkeypatch, tmp_path):
    monkeypatch.setitem(CONFIG, "edit_mode", "cut")
    monkeypatch.setitem(CONFIG, "target_duration", "1m")
    monkeypatch.setitem(CONFIG, "context_info", "")
    (tmp_path / "edited_source.mp4").write_bytes(b"edited")
    (tmp_path / "clip_plan_validated.json").write_text(json.dumps({
        "clips": [{"source_start": 100.0, "source_end": 118.0, "output_start": 0.0, "output_end": 18.0}],
        "total_duration": 18.0,
    }), encoding="utf-8")

    text = build_agent_brief(
        [{"scene_id": 7, "start": 100.0, "end": 118.0, "description": "保留片段"}],
        [],
        [],
        120.0,
        tmp_path,
    ).read_text(encoding="utf-8")

    assert "across the ~18s CUT OUTPUT" in text
    assert "edited_source.mp4` (~18s)" in text
    assert "~1min" not in text

def test_understanding_brief_cut_pass2_requires_fresh_output_spans(monkeypatch, tmp_path):
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


def test_understanding_brief_cut_pass2_rejects_non_finite_output_spans(monkeypatch, tmp_path):
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
