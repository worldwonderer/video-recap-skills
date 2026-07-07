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
    assert {k: r[k] for k in ("characters", "relationships", "plot_points", "entities")} == {"characters": [], "relationships": [], "plot_points": [], "entities": []}
    assert r["schema_version"] == 2 and r["research_glossary"] == []
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
    meta = json.loads((tmp_path / "understanding_index.json.meta.json").read_text(encoding="utf-8"))
    assert meta["source_md5"]
    assert meta["scene_count"] == 1
    assert meta["model"] == consolidate.CONFIG.get("vlm_model", "")
    assert meta["prompt_md5"] == consolidate._prompt_fingerprint(consolidate.INDEX_PROMPT)
    md = (tmp_path / "understanding_index.md").read_text(encoding="utf-8")
    assert "张三" in md and "匕首" in md


def test_consolidate_transcript_writes_provenance_and_preserves_spans(monkeypatch, tmp_path):
    asr = [{"start": 0.0, "end": 5.0, "text": "你给我站住"}]
    (tmp_path / "asr_result.json").write_text(json.dumps(asr), encoding="utf-8")
    monkeypatch.setattr("consolidate.api_call", lambda payload: _fake('{"segments":[{"i":0,"text":"你给我站住！"}]}'))
    out = consolidate.consolidate_transcript(tmp_path)
    import hashlib
    assert out["source_md5"] == hashlib.md5((tmp_path / "asr_result.json").read_bytes()).hexdigest()
    assert out["model"] == consolidate.CONFIG.get("vlm_model", "")
    assert out["prompt_md5"] == consolidate._prompt_fingerprint(consolidate.CLEAN_PROMPT)
    assert out["segments"][0]["start"] == 0.0 and out["segments"][0]["end"] == 5.0
    assert out["segments"][0]["text"] == "你给我站住！"




def test_consolidate_index_recomputes_when_meta_missing_or_source_changes(monkeypatch, tmp_path):
    (tmp_path / "vlm_analysis.json").write_text(json.dumps(
        [{"scene_id": 0, "start": 0, "end": 5, "description": "first"}]), encoding="utf-8")
    calls = {"n": 0}

    def counting(payload):
        calls["n"] += 1
        name = "第一次" if calls["n"] == 1 else "第二次"
        return _fake(f'{{"characters":[{{"name":"{name}"}}],"relationships":[],"plot_points":[],"entities":[]}}')

    monkeypatch.setattr("consolidate.api_call", counting)
    first = consolidate.consolidate_index(tmp_path)
    assert first["characters"][0]["name"] == "第一次"

    # Removing provenance makes the otherwise-fresh index untrusted.
    (tmp_path / "understanding_index.json.meta.json").unlink()
    second = consolidate.consolidate_index(tmp_path)
    assert second["characters"][0]["name"] == "第二次"
    assert calls["n"] == 2

    # Changing VLM bytes also invalidates the cache even if mtime says fresh.
    (tmp_path / "vlm_analysis.json").write_text(json.dumps(
        [{"scene_id": 0, "start": 0, "end": 5, "description": "changed"}]), encoding="utf-8")
    stale_meta = json.loads((tmp_path / "understanding_index.json.meta.json").read_text(encoding="utf-8"))
    (tmp_path / "understanding_index.json.meta.json").write_text(json.dumps(stale_meta), encoding="utf-8")
    third = consolidate.consolidate_index(tmp_path)
    assert calls["n"] == 3
    assert third["characters"]


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



def test_consolidate_recomputes_asr_clean_when_model_or_prompt_meta_missing(monkeypatch, tmp_path):
    asr = [{"start": 0.0, "end": 5.0, "text": "你给我站住"}]
    (tmp_path / "asr_result.json").write_text(json.dumps(asr), encoding="utf-8")
    calls = {"n": 0}

    def counting(payload):
        calls["n"] += 1
        text = "第一次" if calls["n"] == 1 else "第二次"
        return _fake(f'{{"segments":[{{"i":0,"text":"{text}"}}]}}')

    monkeypatch.setattr("consolidate.api_call", counting)
    first = consolidate.consolidate_transcript(tmp_path)
    assert first["segments"][0]["text"] == "第一次"

    payload = json.loads((tmp_path / "asr_clean.json").read_text(encoding="utf-8"))
    payload.pop("model")
    (tmp_path / "asr_clean.json").write_text(json.dumps(payload), encoding="utf-8")
    second = consolidate.consolidate_transcript(tmp_path)

    assert calls["n"] == 2
    assert second["segments"][0]["text"] == "第二次"


def test_consolidate_index_recomputes_when_prompt_provenance_missing(monkeypatch, tmp_path):
    (tmp_path / "vlm_analysis.json").write_text(json.dumps(
        [{"scene_id": 0, "start": 0, "end": 5, "description": "d"}]), encoding="utf-8")
    calls = {"n": 0}

    def counting(payload):
        calls["n"] += 1
        name = "第一次" if calls["n"] == 1 else "第二次"
        return _fake(f'{{"characters":[{{"name":"{name}"}}],"relationships":[],"plot_points":[],"entities":[]}}')

    monkeypatch.setattr("consolidate.api_call", counting)
    consolidate.consolidate_index(tmp_path)
    meta = json.loads((tmp_path / "understanding_index.json.meta.json").read_text(encoding="utf-8"))
    meta.pop("prompt_md5")
    (tmp_path / "understanding_index.json.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    second = consolidate.consolidate_index(tmp_path)

    assert calls["n"] == 2
    assert second["characters"][0]["name"] == "第二次"


def test_build_index_messages_v2_includes_asr_and_research_glossary():
    content = consolidate.build_index_messages(
        [{"scene_id": 0, "start": 0, "end": 5, "description": "门口对峙"}],
        asr_result=[{"start": 1, "end": 2, "text": "叶青眉留下了线索"}],
        background_research={"character_details": {"叶轻眉": {"aliases": ["叶青眉"], "role": "主角之母"}}},
    )[0]["content"]
    assert "[asr:0" in content and "叶青眉留下了线索" in content
    assert "support=context_only" in content and "叶轻眉" in content and "aliases=叶青眉" in content


def test_parse_index_v2_keeps_old_keys_and_context_only_glossary():
    r = consolidate.parse_index_response('{"characters":[{"name":"A"}],"research_glossary":[{"name":"叶轻眉","support":"direct"}]}')
    assert r["schema_version"] == 2
    assert r["characters"][0]["name"] == "A"
    assert r["relationships"] == [] and r["plot_points"] == [] and r["entities"] == []
    assert r["research_glossary"][0]["support"] == "context_only"


def test_consolidate_index_cache_invalidates_on_asr_and_research(monkeypatch, tmp_path):
    (tmp_path / "vlm_analysis.json").write_text(json.dumps([{"scene_id": 0, "start": 0, "end": 5, "description": "d"}], ensure_ascii=False), encoding="utf-8")
    (tmp_path / "asr_result.json").write_text(json.dumps([{"start": 0, "end": 1, "text": "第一版"}], ensure_ascii=False), encoding="utf-8")
    (tmp_path / "background_research.json").write_text(json.dumps({"characters": {"甲": "第一版"}}, ensure_ascii=False), encoding="utf-8")
    calls = {"n": 0}
    def fake(payload):
        calls["n"] += 1
        return _fake('{"characters":[],"relationships":[],"plot_points":[],"entities":[]}')
    monkeypatch.setattr("consolidate.api_call", fake)
    consolidate.consolidate_index(tmp_path)
    consolidate.consolidate_index(tmp_path)
    assert calls["n"] == 1
    (tmp_path / "background_research.json").write_text(json.dumps({"characters": {"甲": "第二版"}}, ensure_ascii=False), encoding="utf-8")
    consolidate.consolidate_index(tmp_path)
    assert calls["n"] == 2
    (tmp_path / "asr_result.json").write_text(json.dumps([{"start": 0, "end": 1, "text": "第二版"}], ensure_ascii=False), encoding="utf-8")
    consolidate.consolidate_index(tmp_path)
    assert calls["n"] == 3
    meta = json.loads((tmp_path / "understanding_index.json.meta.json").read_text(encoding="utf-8"))
    assert meta["schema_version"] == 2 and meta["asr_md5"] and meta["research_md5"]



def test_index_v2_deterministic_asr_mentions_when_llm_omits(monkeypatch, tmp_path):
    (tmp_path / "vlm_analysis.json").write_text(json.dumps([{"scene_id": 0, "start": 0, "end": 5, "description": "无人名画面"}], ensure_ascii=False), encoding="utf-8")
    (tmp_path / "asr_result.json").write_text(json.dumps([{"start": 1, "end": 2, "text": "叶青眉留下线索"}], ensure_ascii=False), encoding="utf-8")
    (tmp_path / "background_research.json").write_text(json.dumps({"character_details": {"叶轻眉": {"aliases": ["叶青眉"], "role": "关键人物"}}}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr("consolidate.api_call", lambda payload: _fake('{"characters":[],"relationships":[],"plot_points":[],"entities":[],"research_glossary":[]}'))
    idx = consolidate.consolidate_index(tmp_path)
    char = idx["characters"][0]
    assert char["name"] == "叶轻眉"
    assert char["asr_mentions"][0]["evidence_id"] == "asr:0"
    assert "asr:0" in char["evidence_ids"]
    assert idx["research_glossary"][0]["support"] == "context_only"


def test_index_v2_cache_invalidates_on_asr_clean(monkeypatch, tmp_path):
    (tmp_path / "vlm_analysis.json").write_text(json.dumps([{"scene_id": 0, "start": 0, "end": 5, "description": "d"}], ensure_ascii=False), encoding="utf-8")
    (tmp_path / "asr_result.json").write_text(json.dumps([{"start": 0, "end": 1, "text": "raw"}], ensure_ascii=False), encoding="utf-8")
    (tmp_path / "asr_clean.json").write_text(json.dumps({"segments": [{"start": 0, "end": 1, "text": "第一版"}]}, ensure_ascii=False), encoding="utf-8")
    calls = {"n": 0}
    def fake(payload):
        calls["n"] += 1
        return _fake('{"characters":[],"relationships":[],"plot_points":[],"entities":[]}')
    monkeypatch.setattr("consolidate.api_call", fake)
    consolidate.consolidate_index(tmp_path)
    consolidate.consolidate_index(tmp_path)
    assert calls["n"] == 1
    (tmp_path / "asr_clean.json").write_text(json.dumps({"segments": [{"start": 0, "end": 1, "text": "第二版"}]}, ensure_ascii=False), encoding="utf-8")
    consolidate.consolidate_index(tmp_path)
    assert calls["n"] == 2
