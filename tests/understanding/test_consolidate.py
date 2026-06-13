import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'video-understanding' / 'scripts'))
import json

import consolidate


def _fake(content):
    return {"choices": [{"message": {"content": content}}]}


# ── Pass A: parse_clean_response (timing preserved by construction) ────────────

def test_parse_clean_preserves_spans_and_applies_cleaned_text():
    asr = [{"start": 0.0, "end": 5.0, "text": "你给我站住"}, {"start": 5.0, "end": 9.0, "text": "我不会放手"}]
    resp = '```json\n{"segments":[{"i":0,"text":"你给我站住！","speaker":"男"},{"i":1,"text":"我不会放手。"}]}\n```'
    out = consolidate.parse_clean_response(resp, asr)
    assert [s["start"] for s in out] == [0.0, 5.0]      # spans untouched
    assert [s["end"] for s in out] == [5.0, 9.0]
    assert out[0]["text"] == "你给我站住！" and out[0]["speaker"] == "男"
    assert out[1]["text"] == "我不会放手。"


def test_parse_clean_rejects_count_mismatch_and_garbage():
    asr = [{"start": 0, "end": 5, "text": "a"}, {"start": 5, "end": 9, "text": "b"}]
    # garbage -> original unchanged
    assert consolidate.parse_clean_response("not json", asr) == asr
    # count mismatch (1 != 2) -> original unchanged
    assert consolidate.parse_clean_response('{"segments":[{"i":0,"text":"x"}]}', asr) == asr


# ── Pass B: index ─────────────────────────────────────────────────────────────

def test_parse_index_normalizes_to_four_list_keys():
    r = consolidate.parse_index_response('garbage no json')
    assert r == {"characters": [], "relationships": [], "plot_points": [], "entities": []}
    r2 = consolidate.parse_index_response('{"characters":[{"name":"A"}],"plot_points":["p1"]}')
    assert r2["characters"][0]["name"] == "A" and r2["plot_points"] == ["p1"]
    assert r2["relationships"] == [] and r2["entities"] == []


def test_build_index_messages_reads_dict_frame_facts():
    # frame_facts is a DICT {ts: [actions]} (guards against the review.py list-shape bug)
    vlm = [{"scene_id": 0, "start": 0, "end": 5, "description": "门口对峙",
            "frame_facts": {"2.0": ["男子握紧拳头"], "4.0": ["女子后退一步"]}}]
    content = consolidate.build_index_messages(vlm)[0]["content"]
    assert "门口对峙" in content and "男子握紧拳头" in content and "女子后退一步" in content


def test_build_clean_messages_includes_transcript():
    asr = [{"start": 0, "end": 5, "text": "第一句对白"}]
    assert "第一句对白" in consolidate.build_clean_messages(asr)[0]["content"]


# ── drivers (mocked api) ──────────────────────────────────────────────────────

def test_consolidate_index_writes_artifacts(monkeypatch, tmp_path):
    (tmp_path / "vlm_analysis.json").write_text(json.dumps(
        [{"scene_id": 0, "start": 0, "end": 5, "description": "d", "frame_facts": {"1.0": ["act"]}}]), encoding="utf-8")
    monkeypatch.setattr("consolidate.api_call", lambda payload: _fake(
        '{"characters":[{"name":"张三","description":"主角"}],"relationships":[],"plot_points":["开端"],"entities":["匕首"]}'))
    idx = consolidate.consolidate_index(tmp_path)
    assert idx["characters"][0]["name"] == "张三"
    assert (tmp_path / "understanding_index.json").exists()
    md = (tmp_path / "understanding_index.md").read_text(encoding="utf-8")
    assert "张三" in md and "匕首" in md


def test_consolidate_transcript_writes_provenance_and_preserves_spans(monkeypatch, tmp_path):
    asr = [{"start": 0.0, "end": 5.0, "text": "你给我站住"}]
    (tmp_path / "asr_result.json").write_text(json.dumps(asr), encoding="utf-8")
    monkeypatch.setattr("consolidate.api_call", lambda payload: _fake('{"segments":[{"i":0,"text":"你给我站住！"}]}'))
    out = consolidate.consolidate_transcript(tmp_path)
    import hashlib
    assert out["source_md5"] == hashlib.md5((tmp_path / "asr_result.json").read_bytes()).hexdigest()
    assert out["segments"][0]["start"] == 0.0 and out["segments"][0]["end"] == 5.0
    assert out["segments"][0]["text"] == "你给我站住！"


def test_consolidate_index_is_idempotent(monkeypatch, tmp_path):
    (tmp_path / "vlm_analysis.json").write_text(json.dumps(
        [{"scene_id": 0, "start": 0, "end": 5, "description": "d"}]), encoding="utf-8")
    calls = {"n": 0}

    def counting(payload):
        calls["n"] += 1
        return _fake('{"characters":[],"relationships":[],"plot_points":[],"entities":[]}')
    monkeypatch.setattr("consolidate.api_call", counting)
    consolidate.consolidate_index(tmp_path)
    consolidate.consolidate_index(tmp_path)  # fresh artifact -> skip, no 2nd api call
    assert calls["n"] == 1


def test_consolidate_graceful_when_inputs_absent(tmp_path):
    # no vlm_analysis.json / asr_result.json -> no crash, returns empty-ish
    res = consolidate.consolidate(tmp_path, do_asr=True, do_index=True)
    assert res.get("index") is None and res.get("asr_clean") is None
