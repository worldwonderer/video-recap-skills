import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'video-understanding' / 'scripts'))
"""Regression tests for vlm.py bug fixes (null content, retry header, partial chunk cache)."""
import json
import sys
from pathlib import Path

import pytest


from lib import CONFIG
from vlm import (
    _parse_vlm_depth_response,
    _load_mimo_partial,
    _mimo_cached_chunks_fingerprint,
    _mimo_overview_payload_fingerprint,
    _mimo_chunk_cache_key,
    _save_mimo_partial,
    analyze_scenes,
    analyze_video_overview,
    mimo_video_overview_cache_fresh,
    mimo_video_settings_fingerprint,
)


def test_parse_frame_facts_splits_chinese_and_english_punctuation():
    raw = """【描述】
门口对峙
【帧标签】
1.0s | 男子握紧拳头，女子后退一步; 门缓缓关上、灯光变暗；气氛紧张
【深层分析】
关系破裂"""

    _description, _depth, facts = _parse_vlm_depth_response(raw)

    assert facts["1.0"] == ["男子握紧拳头", "女子后退一步", "门缓缓关上", "灯光变暗", "气氛紧张"]


# ── BUG 4: 显式 null content 必须降级到 reasoning_content 再到 "" ──────────────

def _make_frame(tmp_path):
    """Create a single fake frame file named frame_00001.jpg so frame_times parses it."""
    frame = tmp_path / "frame_00001.jpg"
    frame.write_bytes(b"\xff\xd8\xff\xd9")  # minimal jpeg-ish bytes
    return frame


def test_null_content_falls_back_to_reasoning(monkeypatch, tmp_path):
    """providers returning content=null must coerce to reasoning_content, not crash on .strip()."""
    frame = _make_frame(tmp_path)
    scenes = [{"start": 0.0, "end": 1.0}]

    monkeypatch.setitem(CONFIG, "fps", 1.0)
    monkeypatch.setitem(CONFIG, "vlm_model", "mimo-v2.5")
    monkeypatch.setitem(CONFIG, "vlm_workers", 1)
    monkeypatch.setitem(CONFIG, "context_info", "")

    def fake_api_call(payload):
        # explicit JSON null for content; reasoning_content carries the real answer
        return {"choices": [{"message": {
            "content": None,
            "reasoning_content": "【描述】男子拿起茶壶",
        }}]}

    monkeypatch.setattr("vlm.api_call", fake_api_call)

    analyses = analyze_scenes(scenes, [frame], tmp_path)

    assert len(analyses) == 1
    # On the old code raw_response would be None -> AttributeError -> "(VLM 分析失败...)".
    assert "VLM 分析失败" not in analyses[0]["description"]
    assert analyses[0]["description"] == "男子拿起茶壶"


def _three_scene_setup(monkeypatch, tmp_path, workers):
    frames = []
    for n in (1, 2, 3):
        f = tmp_path / f"frame_{n:05d}.jpg"
        f.write_bytes(b"\xff\xd8\xff\xd9")
        frames.append(f)
    scenes = [{"start": 0.5, "end": 1.5}, {"start": 1.5, "end": 2.5}, {"start": 2.5, "end": 3.5}]
    monkeypatch.setitem(CONFIG, "fps", 1.0)        # frame_0000N -> t = N seconds, one frame per scene
    monkeypatch.setitem(CONFIG, "vlm_model", "mimo-v2.5")
    monkeypatch.setitem(CONFIG, "vlm_workers", workers)
    monkeypatch.setitem(CONFIG, "context_info", "")
    return scenes, frames


def test_vlm_resume_cache_persists_on_failure_and_resumes(monkeypatch, tmp_path):
    """A scene that keeps failing (e.g. 429) aborts the batch, but the succeeded scenes persist to
    vlm_scene_cache.json so a re-run RESUMES — only the failed scene is re-analyzed, not all three."""
    scenes, frames = _three_scene_setup(monkeypatch, tmp_path, workers=1)
    state = {"fail_mid": True, "calls": 0}

    def fake_api_call(payload):
        state["calls"] += 1
        text = payload["messages"][0]["content"][-1]["text"]
        if state["fail_mid"] and "2.0s" in text:   # scene index 1 (the [1.5,2.5] window)
            raise RuntimeError("HTTP 429 — Too many requests")
        return {"choices": [{"message": {"content": "【描述】测试画面"}}]}

    monkeypatch.setattr("vlm.api_call", fake_api_call)

    with pytest.raises(RuntimeError, match="断点续传"):
        analyze_scenes(scenes, frames, tmp_path)
    cache = json.loads((tmp_path / "vlm_scene_cache.json").read_text())
    assert len(cache) == 2  # the two scenes that succeeded are cached for resume

    state["fail_mid"] = False
    calls_before = state["calls"]
    analyses = analyze_scenes(scenes, frames, tmp_path)
    assert len(analyses) == 3 and all(a and a["description"] == "测试画面" for a in analyses)
    assert state["calls"] - calls_before == 1   # only the previously-failed scene re-analyzed
    assert not (tmp_path / "vlm_scene_cache.json").exists()  # resume cache cleaned on full success


def test_vlm_resume_cache_invalidates_on_request_setting_flip(monkeypatch, tmp_path):
    """A leftover partial cache must NOT be reused after an output-affecting request setting
    (mimo_disable_thinking) changes — otherwise the succeeded scenes would carry the OLD setting
    while only the failed scene gets the NEW one, yielding a silent mixed/stale analysis."""
    scenes, frames = _three_scene_setup(monkeypatch, tmp_path, workers=1)
    monkeypatch.setitem(CONFIG, "mimo_disable_thinking", True)
    state = {"fail_mid": True, "calls": 0}

    def fake_api_call(payload):
        state["calls"] += 1
        text = payload["messages"][0]["content"][-1]["text"]
        if state["fail_mid"] and "2.0s" in text:   # scene index 1 fails → leaves a 2-scene cache
            raise RuntimeError("HTTP 429 — Too many requests")
        return {"choices": [{"message": {"content": "【描述】测试画面"}}]}

    monkeypatch.setattr("vlm.api_call", fake_api_call)

    with pytest.raises(RuntimeError, match="断点续传"):
        analyze_scenes(scenes, frames, tmp_path)
    assert len(json.loads((tmp_path / "vlm_scene_cache.json").read_text())) == 2

    # Flip the request setting and re-run with a non-failing API. The per-scene key now differs,
    # so NONE of the 2 cached scenes are reused — all 3 are re-analyzed under the new setting.
    state["fail_mid"] = False
    monkeypatch.setitem(CONFIG, "mimo_disable_thinking", False)
    calls_before = state["calls"]
    analyses = analyze_scenes(scenes, frames, tmp_path)
    assert len(analyses) == 3 and all(a and a["description"] == "测试画面" for a in analyses)
    assert state["calls"] - calls_before == 3   # all re-analyzed; no stale reuse of old-setting scenes


def test_vlm_auto_throttle_retry_recovers_transient_429(monkeypatch, tmp_path):
    """A scene that 429s on the first pass but succeeds when retried at lower concurrency is
    recovered within ONE run — no abort. (Default 8 workers no longer dooms a long video.)"""
    scenes, frames = _three_scene_setup(monkeypatch, tmp_path, workers=4)
    fired = {"once": False}

    def fake_api_call(payload):
        text = payload["messages"][0]["content"][-1]["text"]
        if "2.0s" in text and not fired["once"]:
            fired["once"] = True
            raise RuntimeError("HTTP 429 — Too many requests")
        return {"choices": [{"message": {"content": "【描述】测试画面"}}]}

    monkeypatch.setattr("vlm.api_call", fake_api_call)
    analyses = analyze_scenes(scenes, frames, tmp_path)  # must NOT raise
    assert len(analyses) == 3 and all(a["description"] == "测试画面" for a in analyses)
    assert not (tmp_path / "vlm_scene_cache.json").exists()


def test_null_content_and_null_reasoning_coerce_to_empty_then_retry(monkeypatch, tmp_path):
    """content=null AND reasoning_content=null must coerce to "" so the empty-retry loop runs."""
    frame = _make_frame(tmp_path)
    scenes = [{"start": 0.0, "end": 1.0}]

    monkeypatch.setitem(CONFIG, "fps", 1.0)
    monkeypatch.setitem(CONFIG, "vlm_model", "mimo-v2.5")
    monkeypatch.setitem(CONFIG, "vlm_workers", 1)
    monkeypatch.setitem(CONFIG, "context_info", "")

    calls = {"n": 0}

    def fake_api_call(payload):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"choices": [{"message": {"content": None, "reasoning_content": None}}]}
        return {"choices": [{"message": {"content": "【描述】第二次成功"}}]}

    monkeypatch.setattr("vlm.api_call", fake_api_call)

    analyses = analyze_scenes(scenes, [frame], tmp_path)

    # null content did not crash; the empty-response retry path ran and recovered.
    assert calls["n"] >= 2
    assert analyses[0]["description"] == "第二次成功"


# ── BUG 11/retry: 重试时必须保留帧时间点表头 ────────────────────────────────

def test_retry_text_keeps_frame_timestamp_header(monkeypatch, tmp_path):
    """The empty-response retry must re-send the frame-timestamp header so 【帧标签】 anchors survive."""
    frame = _make_frame(tmp_path)
    scenes = [{"start": 0.0, "end": 1.0}]

    monkeypatch.setitem(CONFIG, "fps", 1.0)
    monkeypatch.setitem(CONFIG, "vlm_model", "mimo-v2.5")
    monkeypatch.setitem(CONFIG, "vlm_workers", 1)
    monkeypatch.setitem(CONFIG, "context_info", "")

    retry_texts = []

    def fake_api_call(payload):
        text_part = payload["messages"][0]["content"][-1]["text"]
        retry_texts.append(text_part)
        # always empty -> exhaust the 3 attempts so we observe retry payloads
        return {"choices": [{"message": {"content": ""}}]}

    monkeypatch.setattr("vlm.api_call", fake_api_call)

    with pytest.raises(RuntimeError, match="VLM 分析失败"):
        analyze_scenes(scenes, [frame], tmp_path)

    assert len(retry_texts) == 3
    # frame_00001.jpg @ fps=1.0 -> 1.0s ; header must appear on every attempt incl. retries
    for text in retry_texts:
        assert "帧时间点" in text
        assert "1.0s" in text
    # retry attempts also carry the explicit format reminder
    assert "请务必按格式输出，不要留空。" in retry_texts[1]
    assert "请务必按格式输出，不要留空。" in retry_texts[2]


def test_scene_api_failure_does_not_write_placeholder_cache(monkeypatch, tmp_path):
    """Transient VLM failures must fail the stage instead of caching placeholder analysis."""
    frame = _make_frame(tmp_path)
    scenes = [{"start": 0.0, "end": 1.0}]

    monkeypatch.setitem(CONFIG, "fps", 1.0)
    monkeypatch.setitem(CONFIG, "vlm_model", "mimo-v2.5")
    monkeypatch.setitem(CONFIG, "vlm_workers", 1)
    monkeypatch.setitem(CONFIG, "context_info", "")
    monkeypatch.setattr("vlm.api_call", lambda payload: (_ for _ in ()).throw(RuntimeError("quota")))

    with pytest.raises(RuntimeError, match="VLM 分析失败"):
        analyze_scenes(scenes, [frame], tmp_path)

    assert not (tmp_path / "vlm_analysis.json").exists()


def test_all_empty_scene_responses_do_not_write_placeholder_cache(monkeypatch, tmp_path):
    """Repeated empty VLM responses are not a successful analysis cache."""
    frame = _make_frame(tmp_path)
    scenes = [{"start": 0.0, "end": 1.0}]

    monkeypatch.setitem(CONFIG, "fps", 1.0)
    monkeypatch.setitem(CONFIG, "vlm_model", "mimo-v2.5")
    monkeypatch.setitem(CONFIG, "vlm_workers", 1)
    monkeypatch.setitem(CONFIG, "context_info", "")
    monkeypatch.setattr("vlm.api_call", lambda payload: {"choices": [{"message": {"content": ""}}]})

    with pytest.raises(RuntimeError, match="VLM 分析失败"):
        analyze_scenes(scenes, [frame], tmp_path)

    assert not (tmp_path / "vlm_analysis.json").exists()


# ── BUG 5: 增量分片缓存往返 + 失败保留已付费分片 ───────────────────────────

def test_partial_cache_roundtrip_invalidates_on_settings_change(monkeypatch, tmp_path):
    """_save/_load partial round-trips; a fingerprint change drops cached chunks."""
    partial_path = tmp_path / "mimo_video_overview.partial.json"
    chunk = {"chunk_id": 0, "scene_id": 7, "start": 0.0, "end": 2.0}
    key = _mimo_chunk_cache_key(chunk)
    done = {key: {"chunk_id": 0, "scene_id": 7, "content": "缓存内容"}}

    _save_mimo_partial(partial_path, done)
    assert partial_path.exists()

    loaded = _load_mimo_partial(partial_path)
    assert loaded == done

    # changing a fingerprinted setting invalidates the whole partial
    monkeypatch.setitem(CONFIG, "mimo_video_chunk_max_seconds", 99)
    assert _load_mimo_partial(partial_path) == {}





def test_partial_cache_rejects_payload_mutation(tmp_path):
    """Partial cache metadata must not trust edited chunk JSON as paid fresh content."""
    partial_path = tmp_path / "mimo_video_overview.partial.json"
    chunk = {"chunk_id": 0, "scene_id": 7, "start": 0.0, "end": 2.0}
    key = _mimo_chunk_cache_key(chunk)
    done = {key: {"chunk_id": 0, "scene_id": 7, "content": "original paid chunk"}}

    _save_mimo_partial(partial_path, done)
    payload = json.loads(partial_path.read_text(encoding="utf-8"))
    payload["chunks"][key]["content"] = "tampered but non-empty"
    partial_path.write_text(json.dumps(payload), encoding="utf-8")

    assert _load_mimo_partial(partial_path) == {}


def test_partial_cache_rejects_source_video_mismatch(monkeypatch, tmp_path):
    """Incremental MiMo chunks from another source video must not be reused."""
    old_video = tmp_path / "old.mp4"
    new_video = tmp_path / "new.mp4"
    old_video.write_bytes(b"old-video")
    new_video.write_bytes(b"new-video")
    scenes = [{"scene_id": 7, "start": 0.0, "end": 2.0}]
    partial_path = tmp_path / "mimo_video_overview.partial.json"
    chunk = {"chunk_id": 0, "scene_id": 7, "start": 0.0, "end": 2.0}
    key = _mimo_chunk_cache_key(chunk)
    done = {key: {"chunk_id": 0, "scene_id": 7, "content": "old paid chunk"}}

    monkeypatch.setitem(CONFIG, "mimo_video_chunk_max_seconds", 2)
    monkeypatch.setitem(CONFIG, "mimo_video_chunk_min_seconds", 0.5)
    _save_mimo_partial(partial_path, done, old_video, scenes)

    assert _load_mimo_partial(partial_path, old_video, scenes) == done
    assert _load_mimo_partial(partial_path, new_video, scenes) == {}


def test_partial_cache_rejects_scene_chunk_mismatch(monkeypatch, tmp_path):
    """Incremental MiMo chunks must be tied to the current scene/chunk plan."""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    old_scenes = [{"scene_id": 7, "start": 0.0, "end": 2.0}]
    new_scenes = [{"scene_id": 7, "start": 0.0, "end": 4.0}]
    partial_path = tmp_path / "mimo_video_overview.partial.json"
    chunk = {"chunk_id": 0, "scene_id": 7, "start": 0.0, "end": 2.0}
    done = {_mimo_chunk_cache_key(chunk): {"chunk_id": 0, "scene_id": 7, "content": "old paid chunk"}}

    monkeypatch.setitem(CONFIG, "mimo_video_chunk_max_seconds", 2)
    monkeypatch.setitem(CONFIG, "mimo_video_chunk_min_seconds", 0.5)
    _save_mimo_partial(partial_path, done, video, old_scenes)

    assert _load_mimo_partial(partial_path, video, old_scenes) == done
    assert _load_mimo_partial(partial_path, video, new_scenes) == {}


def test_failed_chunk_preserves_completed_chunks_and_resume_skips(monkeypatch, tmp_path):
    """A mid-loop chunk failure keeps completed chunks; resume only redoes the missing one."""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake-video")

    monkeypatch.setitem(CONFIG, "mimo_video_overview", True)
    monkeypatch.setitem(CONFIG, "mimo_video_api_key", "secret")
    monkeypatch.setitem(CONFIG, "mimo_video_model", "mimo-v2.5")
    monkeypatch.setitem(CONFIG, "vlm_model", "mimo-v2.5")
    monkeypatch.setitem(CONFIG, "mimo_video_fps", 2)
    monkeypatch.setitem(CONFIG, "mimo_video_chunk_max_seconds", 2)
    monkeypatch.setitem(CONFIG, "mimo_video_chunk_min_seconds", 0.5)

    def fake_extract(video_path, chunk, output_path):
        output_path.write_bytes(b"tiny-chunk")
        return output_path

    monkeypatch.setattr("vlm._extract_video_chunk", fake_extract)

    # 5s span @ max 2s -> 3 chunks. First run: chunk 2 (chunk_id=2) fails.
    analyzed = []

    def fake_chunk_api_fail_on_last(chunk_path, chunk):
        analyzed.append(chunk["chunk_id"])
        if chunk["chunk_id"] == 2:
            raise RuntimeError("MiMo 分片失败")
        return {
            "chunk_id": chunk["chunk_id"],
            "scene_id": chunk["scene_id"],
            "start": chunk["start"],
            "end": chunk["end"],
            "model": "mimo-v2.5",
            "content": f"分片{chunk['chunk_id']}",
            "reasoning_content": "",
            "usage": {},
            "clip_path": f"mimo_video_chunks/{chunk_path.name}",
        }

    monkeypatch.setattr("vlm._analyze_mimo_video_chunk", fake_chunk_api_fail_on_last)

    scenes = [{"scene_id": 7, "start": 0.0, "end": 5.0}]
    with pytest.raises(RuntimeError):
        analyze_video_overview(video, tmp_path, scenes)

    # paid chunks (0 and 1) were persisted to the partial; final file not yet written
    partial_path = tmp_path / "mimo_video_overview.partial.json"
    assert partial_path.exists()
    assert not (tmp_path / "mimo_video_overview.json").exists()
    partial = json.loads(partial_path.read_text(encoding="utf-8"))
    assert len(partial["chunks"]) == 2
    assert analyzed == [0, 1, 2]

    # Resume: chunks 0 and 1 are cached, only chunk 2 is re-analyzed.
    analyzed.clear()

    def fake_chunk_api_all_ok(chunk_path, chunk):
        analyzed.append(chunk["chunk_id"])
        return {
            "chunk_id": chunk["chunk_id"],
            "scene_id": chunk["scene_id"],
            "start": chunk["start"],
            "end": chunk["end"],
            "model": "mimo-v2.5",
            "content": f"分片{chunk['chunk_id']}",
            "reasoning_content": "",
            "usage": {},
            "clip_path": f"mimo_video_chunks/{chunk_path.name}",
        }

    monkeypatch.setattr("vlm._analyze_mimo_video_chunk", fake_chunk_api_all_ok)

    overview = analyze_video_overview(video, tmp_path, scenes)

    # only the previously-failed chunk was re-billed
    assert analyzed == [2]
    # final canonical artifact matches today's format
    assert overview["input"] == "scene_chunks"
    assert overview["chunk_count"] == 3
    assert overview["settings"] == mimo_video_settings_fingerprint()
    assert len(overview["chunks"]) == 3
    assert [c["chunk_id"] for c in overview["chunks"]] == [0, 1, 2]
    # final file written and partial cleaned up
    assert (tmp_path / "mimo_video_overview.json").exists()
    assert not partial_path.exists()


def test_full_success_writes_canonical_file_without_partial(monkeypatch, tmp_path):
    """When all chunks succeed in one pass, only the canonical file remains (no leftover partial)."""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake-video")

    monkeypatch.setitem(CONFIG, "mimo_video_overview", True)
    monkeypatch.setitem(CONFIG, "mimo_video_api_key", "secret")
    monkeypatch.setitem(CONFIG, "mimo_video_model", "mimo-v2.5")
    monkeypatch.setitem(CONFIG, "vlm_model", "mimo-v2.5")
    monkeypatch.setitem(CONFIG, "mimo_video_chunk_max_seconds", 2)
    monkeypatch.setitem(CONFIG, "mimo_video_chunk_min_seconds", 0.5)

    def fake_extract(video_path, chunk, output_path):
        output_path.write_bytes(b"tiny-chunk")
        return output_path

    def fake_chunk(chunk_path, chunk):
        return {
            "chunk_id": chunk["chunk_id"],
            "scene_id": chunk["scene_id"],
            "start": chunk["start"],
            "end": chunk["end"],
            "model": "mimo-v2.5",
            "content": f"分片{chunk['chunk_id']}",
            "reasoning_content": "",
            "usage": {},
            "clip_path": f"mimo_video_chunks/{chunk_path.name}",
        }

    monkeypatch.setattr("vlm._extract_video_chunk", fake_extract)
    monkeypatch.setattr("vlm._analyze_mimo_video_chunk", fake_chunk)

    overview = analyze_video_overview(video, tmp_path, [{"scene_id": 1, "start": 0.0, "end": 3.0}])

    assert overview["input"] == "scene_chunks"
    assert overview["source_video_fingerprint"]
    assert mimo_video_overview_cache_fresh(tmp_path / "mimo_video_overview.json", video, [{"scene_id": 1, "start": 0.0, "end": 3.0}])
    assert (tmp_path / "mimo_video_overview.json").exists()
    assert not (tmp_path / "mimo_video_overview.partial.json").exists()



def test_final_overview_cache_rejects_payload_mutation(monkeypatch, tmp_path):
    """The final MiMo overview skip path must reject byte/content edits to cached chunks."""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake-video")

    monkeypatch.setitem(CONFIG, "mimo_video_overview", True)
    monkeypatch.setitem(CONFIG, "mimo_video_api_key", "secret")
    monkeypatch.setitem(CONFIG, "mimo_video_model", "mimo-v2.5")
    monkeypatch.setitem(CONFIG, "vlm_model", "mimo-v2.5")
    monkeypatch.setitem(CONFIG, "mimo_video_chunk_max_seconds", 2)
    monkeypatch.setitem(CONFIG, "mimo_video_chunk_min_seconds", 0.5)

    def fake_extract(video_path, chunk, output_path):
        output_path.write_bytes(b"tiny-chunk")
        return output_path

    def fake_chunk(chunk_path, chunk):
        return {
            "chunk_id": chunk["chunk_id"],
            "scene_id": chunk["scene_id"],
            "start": chunk["start"],
            "end": chunk["end"],
            "model": "mimo-v2.5",
            "content": f"分片{chunk['chunk_id']}",
            "reasoning_content": "",
            "usage": {},
            "clip_path": f"mimo_video_chunks/{chunk_path.name}",
        }

    scenes = [{"scene_id": 1, "start": 0.0, "end": 3.0}]
    monkeypatch.setattr("vlm._extract_video_chunk", fake_extract)
    monkeypatch.setattr("vlm._analyze_mimo_video_chunk", fake_chunk)
    analyze_video_overview(video, tmp_path, scenes)

    overview_path = tmp_path / "mimo_video_overview.json"
    payload = json.loads(overview_path.read_text(encoding="utf-8"))
    payload["chunks"][0]["content"] = "tampered but non-empty"
    overview_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    assert not mimo_video_overview_cache_fresh(overview_path, video, scenes)

    payload["chunks_fingerprint"] = _mimo_cached_chunks_fingerprint(payload["chunks"])
    overview_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert not mimo_video_overview_cache_fresh(overview_path, video, scenes)

    payload["overview_fingerprint"] = _mimo_overview_payload_fingerprint(payload)
    overview_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert mimo_video_overview_cache_fresh(overview_path, video, scenes)


def test_final_overview_cache_invalidates_on_settings_source_or_chunks(monkeypatch, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake-video")

    monkeypatch.setitem(CONFIG, "mimo_video_overview", True)
    monkeypatch.setitem(CONFIG, "mimo_video_api_key", "secret")
    monkeypatch.setitem(CONFIG, "mimo_video_model", "mimo-v2.5")
    monkeypatch.setitem(CONFIG, "vlm_model", "mimo-v2.5")
    monkeypatch.setitem(CONFIG, "mimo_video_chunk_max_seconds", 2)
    monkeypatch.setitem(CONFIG, "mimo_video_chunk_min_seconds", 0.5)
    original_prompt = CONFIG.get("mimo_video_prompt")

    def fake_extract(video_path, chunk, output_path):
        output_path.write_bytes(b"tiny-chunk")
        return output_path

    def fake_chunk(chunk_path, chunk):
        return {
            "chunk_id": chunk["chunk_id"],
            "scene_id": chunk["scene_id"],
            "start": chunk["start"],
            "end": chunk["end"],
            "model": "mimo-v2.5",
            "content": f"分片{chunk['chunk_id']}",
            "reasoning_content": "",
            "usage": {},
            "clip_path": f"mimo_video_chunks/{chunk_path.name}",
        }

    scenes = [{"scene_id": 1, "start": 0.0, "end": 3.0}]
    monkeypatch.setattr("vlm._extract_video_chunk", fake_extract)
    monkeypatch.setattr("vlm._analyze_mimo_video_chunk", fake_chunk)
    analyze_video_overview(video, tmp_path, scenes)

    overview_path = tmp_path / "mimo_video_overview.json"
    assert mimo_video_overview_cache_fresh(overview_path, video, scenes)

    monkeypatch.setitem(CONFIG, "mimo_video_prompt", "changed prompt")
    assert not mimo_video_overview_cache_fresh(overview_path, video, scenes)

    monkeypatch.setitem(CONFIG, "mimo_video_prompt", original_prompt)
    original_url = CONFIG.get("mimo_video_api_url")
    monkeypatch.setitem(CONFIG, "mimo_video_api_url", "https://changed.example/v1/chat/completions")
    assert not mimo_video_overview_cache_fresh(overview_path, video, scenes)

    monkeypatch.setitem(CONFIG, "mimo_video_api_url", original_url)
    other_video = tmp_path / "other.mp4"
    other_video.write_bytes(b"other-video")
    assert not mimo_video_overview_cache_fresh(overview_path, other_video, scenes)

    changed_scenes = [{"scene_id": 1, "start": 0.0, "end": 5.0}]
    assert not mimo_video_overview_cache_fresh(overview_path, video, changed_scenes)


def test_all_rejected_chunks_skip_overview(monkeypatch, tmp_path):
    """When MiMo rejects every chunk (content moderation), the overview degrades gracefully:
    return None, write no canonical file, leave no partial — instead of polluting the brief."""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake-video")
    monkeypatch.setitem(CONFIG, "mimo_video_overview", True)
    monkeypatch.setitem(CONFIG, "mimo_video_api_key", "secret")
    monkeypatch.setitem(CONFIG, "mimo_video_model", "mimo-v2.5")
    monkeypatch.setitem(CONFIG, "vlm_model", "mimo-v2.5")
    monkeypatch.setitem(CONFIG, "mimo_video_chunk_max_seconds", 2)
    monkeypatch.setitem(CONFIG, "mimo_video_chunk_min_seconds", 0.5)

    def fake_extract(video_path, chunk, output_path):
        output_path.write_bytes(b"x")
        return output_path

    def rejected(chunk_path, chunk):
        return {"chunk_id": chunk["chunk_id"], "scene_id": chunk["scene_id"],
                "start": chunk["start"], "end": chunk["end"], "model": "mimo-v2.5",
                "content": "The request was rejected because it was considered high risk",
                "reasoning_content": "", "usage": {}, "clip_path": "x"}

    monkeypatch.setattr("vlm._extract_video_chunk", fake_extract)
    monkeypatch.setattr("vlm._analyze_mimo_video_chunk", rejected)

    overview = analyze_video_overview(video, tmp_path, [{"scene_id": 1, "start": 0.0, "end": 3.0}])
    assert overview is None
    assert not (tmp_path / "mimo_video_overview.json").exists()
    assert not (tmp_path / "mimo_video_overview.partial.json").exists()


def test_is_mimo_chunk_usable():
    from vlm import _is_mimo_chunk_usable
    assert _is_mimo_chunk_usable("范闲在竹林中打斗，剑光凌厉") is True
    assert _is_mimo_chunk_usable("") is False
    assert _is_mimo_chunk_usable("The request was rejected because it was considered high risk") is False
    assert _is_mimo_chunk_usable("内容审核未通过") is False



def test_mixed_unusable_chunks_degrade_to_usable_overview(monkeypatch, tmp_path):
    """A single empty/refused MiMo chunk no longer aborts the whole overview: it degrades to the
    usable chunks (partial=true) so enabling overview by default is safe on moderated sources;
    the scene whose chunk was rejected falls back to the frame description downstream."""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake-video")
    monkeypatch.setitem(CONFIG, "mimo_video_overview", True)
    monkeypatch.setitem(CONFIG, "mimo_video_api_key", "secret")
    monkeypatch.setitem(CONFIG, "mimo_video_model", "mimo-v2.5")
    monkeypatch.setitem(CONFIG, "vlm_model", "mimo-v2.5")
    monkeypatch.setitem(CONFIG, "mimo_video_chunk_max_seconds", 2)
    monkeypatch.setitem(CONFIG, "mimo_video_chunk_min_seconds", 0.5)

    def fake_extract(video_path, chunk, output_path):
        output_path.write_bytes(b"tiny-chunk")
        return output_path

    def mixed(chunk_path, chunk):
        return {
            "chunk_id": chunk["chunk_id"],
            "scene_id": chunk["scene_id"],
            "start": chunk["start"],
            "end": chunk["end"],
            "model": "mimo-v2.5",
            "content": "" if chunk["chunk_id"] == 1 else f"分片{chunk['chunk_id']}",
            "reasoning_content": "",
            "usage": {},
            "clip_path": f"mimo_video_chunks/{chunk_path.name}",
        }

    monkeypatch.setattr("vlm._extract_video_chunk", fake_extract)
    monkeypatch.setattr("vlm._analyze_mimo_video_chunk", mixed)

    scenes = [{"scene_id": 1, "start": 0.0, "end": 5.0}]
    overview = analyze_video_overview(video, tmp_path, scenes)

    # degraded, not aborted: a final overview is written from the usable chunks only
    assert overview is not None
    assert overview["partial"] is True
    assert overview["unusable_chunk_count"] >= 1
    final = json.loads((tmp_path / "mimo_video_overview.json").read_text(encoding="utf-8"))
    assert len(final["chunks"]) >= 1
    assert all(item["content"] for item in final["chunks"])
    # the incremental partial cache is cleaned up once the final overview is written
    assert not (tmp_path / "mimo_video_overview.partial.json").exists()


def test_final_overview_cache_rejects_unusable_cached_chunk(monkeypatch, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake-video")
    monkeypatch.setitem(CONFIG, "mimo_video_model", "mimo-v2.5")
    monkeypatch.setitem(CONFIG, "vlm_model", "mimo-v2.5")
    monkeypatch.setitem(CONFIG, "mimo_video_chunk_max_seconds", 2)
    monkeypatch.setitem(CONFIG, "mimo_video_chunk_min_seconds", 0.5)
    scenes = [{"scene_id": 1, "start": 0.0, "end": 3.0}]
    chunks = [
        {"chunk_id": 0, "scene_id": 1, "start": 0.0, "end": 2.0, "content": "有效"},
        {"chunk_id": 1, "scene_id": 1, "start": 2.0, "end": 3.0, "content": ""},
    ]
    (tmp_path / "mimo_video_overview.json").write_text(json.dumps({
        "input": "scene_chunks",
        "content": "有效\n(MiMo 未返回内容)",
        "chunks": chunks,
        "chunks_fingerprint": _mimo_cached_chunks_fingerprint(chunks),
        "overview_fingerprint": "stale",
        "source_video_fingerprint": __import__("vlm").file_fingerprint(video),
        "settings": mimo_video_settings_fingerprint(),
    }), encoding="utf-8")

    assert not mimo_video_overview_cache_fresh(tmp_path / "mimo_video_overview.json", video, scenes)
