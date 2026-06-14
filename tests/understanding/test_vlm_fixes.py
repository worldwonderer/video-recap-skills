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
    _load_mimo_partial,
    _mimo_chunk_cache_key,
    _save_mimo_partial,
    analyze_scenes,
    analyze_video_overview,
    mimo_video_settings_fingerprint,
)


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

    analyze_scenes(scenes, [frame], tmp_path)

    assert len(retry_texts) == 3
    # frame_00001.jpg @ fps=1.0 -> 1.0s ; header must appear on every attempt incl. retries
    for text in retry_texts:
        assert "帧时间点" in text
        assert "1.0s" in text
    # retry attempts also carry the explicit format reminder
    assert "请务必按格式输出，不要留空。" in retry_texts[1]
    assert "请务必按格式输出，不要留空。" in retry_texts[2]


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
    assert (tmp_path / "mimo_video_overview.json").exists()
    assert not (tmp_path / "mimo_video_overview.partial.json").exists()


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
