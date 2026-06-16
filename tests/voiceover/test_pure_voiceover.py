import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'video-voiceover' / 'scripts'))
import pytest  # noqa: F401
from subprocess import CompletedProcess  # noqa: F401
from lib import CONFIG
import voiceover
from voiceover import _build_tts_segment_result, _detect_tts_engine, _parse_rate_offset, _run_tts_engine, _synthesize_segment, _tts_mimo, resolve_tts_engine, synthesize_tts


def test_parse_rate_offset():
    assert _parse_rate_offset("+0%") == 0.0
    assert _parse_rate_offset("+20%") == 0.2
    assert _parse_rate_offset("-10%") == -0.1
    assert _parse_rate_offset("+5%") == 0.05


def test_build_tts_segment_result_preserves_source_trace(tmp_path):
    result = _build_tts_segment_result(0, {
        "start": 1.0,
        "end": 3.0,
        "source_start": 11.0,
        "source_end": 13.0,
        "source_clip_id": 2,
        "narration": "测试。",
    }, "测试。", tmp_path / "narr.wav", 1.0, 0.0)

    assert result["source_start"] == 11.0
    assert result["source_end"] == 13.0
    assert result["source_clip_id"] == 2


def test_synthesize_segment_reuses_only_matching_cache(monkeypatch, tmp_path):
    narration = [{"start": 0.0, "end": 2.0, "narration": "第一版。"}]
    tts_dir = tmp_path / "tts_segments"
    tts_dir.mkdir()
    calls = []

    def fake_run_tts(engine, text, output_wav, rate="+0%", pitch="+0Hz", emotion=None):
        calls.append(text)
        output_wav.write_bytes(f"audio:{text}".encode("utf-8"))

    monkeypatch.setitem(CONFIG, "tts_dynamic_params", False)
    monkeypatch.setitem(CONFIG, "mimo_tts_model", "mimo-v2.5-tts")
    monkeypatch.setitem(CONFIG, "mimo_tts_voice", "冰糖")
    monkeypatch.setattr("voiceover._run_tts_engine", fake_run_tts)
    monkeypatch.setattr("voiceover._get_audio_duration", lambda path: 1.0 if Path(path).exists() else 0.0)

    first = _synthesize_segment(0, narration[0], narration, tts_dir, "mimo-tts")
    second = _synthesize_segment(0, narration[0], narration, tts_dir, "mimo-tts")

    assert calls == ["第一版。"]
    assert first["narration"] == second["narration"] == "第一版。"

    changed = [{"start": 0.0, "end": 2.0, "narration": "第二版。"}]
    third = _synthesize_segment(0, changed[0], changed, tts_dir, "mimo-tts")

    assert calls == ["第一版。", "第二版。"]
    assert third["narration"] == "第二版。"
    assert (tts_dir / "narr_000.wav").read_text(encoding="utf-8") == "audio:第二版。"


def test_synthesize_segment_block_truncation_accounts_for_narration_speed(monkeypatch, tmp_path):
    # assemble speeds every segment up by narration_speed before placement, so the slot holds
    # raw_dur / narration_speed. A block whose RAW tts overflows the slot but fits after the 1.3x
    # speedup must NOT be truncated; only a block that overflows even then is trimmed.
    monkeypatch.setitem(CONFIG, "tts_dynamic_params", False)
    monkeypatch.setitem(CONFIG, "narration_speed", 1.3)
    monkeypatch.setitem(CONFIG, "mimo_tts_model", "mimo-v2.5-tts")
    calls = []

    def fake_run_tts(engine, text, output_wav, rate="+0%", pitch="+0Hz", emotion=None):
        calls.append(text)
        output_wav.write_text(text, encoding="utf-8")

    monkeypatch.setattr("voiceover._run_tts_engine", fake_run_tts)
    monkeypatch.setattr("voiceover._get_audio_duration",
                        lambda p: len(Path(p).read_text(encoding="utf-8")) * 0.3 if Path(p).exists() else 0.0)
    tts_dir = tmp_path / "tts_segments"
    tts_dir.mkdir()

    # slot 12s, pause 0.2s -> available 11.8s; raw budget = 11.8 * 1.3 * 1.2 ≈ 18.4s.
    # 50-char block -> raw 15s: over the old 14.2s (available*1.2) budget, but fits the speed-aware one.
    block = "情节推进。" * 10
    res = _synthesize_segment(0, {"start": 0.0, "end": 12.0, "narration": block, "pause_after_ms": 200},
                              [block], tts_dir, "mimo-tts")
    assert calls == [block]                       # exactly one TTS call -> NOT truncated
    assert res["narration"] == block

    # 100-char block -> raw 30s: overflows even after the speedup -> still truncated (two calls).
    calls.clear()
    huge = "情节推进。" * 20
    res2 = _synthesize_segment(1, {"start": 0.0, "end": 12.0, "narration": huge, "pause_after_ms": 200},
                               [huge], tts_dir, "mimo-tts")
    assert len(calls) == 2                         # re-synthesized after truncation
    assert len(res2["narration"]) < len(huge)


def test_synthesize_segment_rejects_cache_when_wav_bytes_change(monkeypatch, tmp_path):
    narration = [{"start": 0.0, "end": 2.0, "narration": "第一版。"}]
    tts_dir = tmp_path / "tts_segments"
    tts_dir.mkdir()
    calls = []

    def fake_run_tts(engine, text, output_wav, rate="+0%", pitch="+0Hz", emotion=None):
        calls.append(text)
        output_wav.write_bytes(f"audio:{text}:call{len(calls)}".encode("utf-8"))

    monkeypatch.setitem(CONFIG, "tts_dynamic_params", False)
    monkeypatch.setitem(CONFIG, "mimo_tts_model", "mimo-v2.5-tts")
    monkeypatch.setitem(CONFIG, "mimo_tts_voice", "冰糖")
    monkeypatch.setattr("voiceover._run_tts_engine", fake_run_tts)
    monkeypatch.setattr("voiceover._get_audio_duration", lambda path: 1.0 if Path(path).exists() else 0.0)

    _synthesize_segment(0, narration[0], narration, tts_dir, "mimo-tts")
    (tts_dir / "narr_000.wav").write_bytes(b"externally-mutated-wav")
    _synthesize_segment(0, narration[0], narration, tts_dir, "mimo-tts")

    assert calls == ["第一版。", "第一版。"]
    assert (tts_dir / "narr_000.wav").read_text(encoding="utf-8") == "audio:第一版。:call2"


def test_synthesize_tts_reuses_complete_cache_without_mimo_key(monkeypatch, tmp_path):
    narration = [{"start": 0.0, "end": 2.0, "narration": "离线复用。"}]
    tts_dir = tmp_path / "tts_segments"
    tts_dir.mkdir()

    monkeypatch.setitem(CONFIG, "mimo_tts_api_key", "")
    monkeypatch.setitem(CONFIG, "tts_dynamic_params", False)
    monkeypatch.setattr("voiceover._get_audio_duration", lambda path: 1.25 if Path(path).exists() else 0.0)

    wav = tts_dir / "narr_000.wav"
    wav.write_bytes(b"cached-wav")
    prepared = voiceover._prepare_tts_segment(0, narration[0], narration, tts_dir, "mimo-tts")
    text, output_wav, rate, _pitch, cache_key = prepared
    assert output_wav == wav
    voiceover._write_tts_segment_cache(wav, cache_key, text, 1.25, _parse_rate_offset(rate))

    def boom(*args, **kwargs):
        raise AssertionError("cache-only rerun must not call MiMo TTS")

    monkeypatch.setattr("voiceover._tts_mimo", boom)

    segments, engine = synthesize_tts(narration, tmp_path)

    assert engine == "mimo-tts"
    assert segments[0]["audio_path"] == str(wav)
    assert segments[0]["narration"] == "离线复用。"


def test_synthesize_tts_rejects_empty_narration(monkeypatch, tmp_path):
    monkeypatch.setitem(CONFIG, "mimo_tts_api_key", "tp-test")

    with pytest.raises(RuntimeError, match="没有可配音的解说段"):
        synthesize_tts([], tmp_path)


def test_synthesize_tts_rejects_cleaned_empty_narration(monkeypatch, tmp_path):
    monkeypatch.setitem(CONFIG, "mimo_tts_api_key", "tp-test")

    with pytest.raises(RuntimeError, match="没有可配音的有效文本"):
        synthesize_tts([{"start": 0.0, "end": 1.0, "narration": "[stage direction]"}], tmp_path)


def test_synthesize_tts_raises_on_failed_segment_by_default(monkeypatch, tmp_path):
    narration = [
        {"start": 0.0, "end": 1.0, "narration": "第一段。"},
        {"start": 1.0, "end": 2.0, "narration": "第二段。"},
    ]

    def fake_synthesize_segment(i, seg, narration_data, tts_dir, engine):
        if i == 1:
            raise RuntimeError("network timeout")
        return {
            "index": i,
            "start": seg["start"],
            "end": seg["end"],
            "narration": seg["narration"],
            "audio_path": str(tts_dir / f"narr_{i:03d}.wav"),
            "audio_duration": 0.5,
        }

    monkeypatch.setitem(CONFIG, "mimo_tts_api_key", "tp-test")
    monkeypatch.setitem(CONFIG, "tts_workers", 2)
    monkeypatch.setitem(CONFIG, "allow_partial_tts", False)
    monkeypatch.setattr("voiceover._synthesize_segment", fake_synthesize_segment)

    with pytest.raises(RuntimeError, match="TTS 失败 1/2 段"):
        synthesize_tts(narration, tmp_path)


def test_synthesize_tts_allows_partial_when_configured(monkeypatch, tmp_path):
    narration = [
        {"start": 0.0, "end": 1.0, "narration": "第一段。"},
        {"start": 1.0, "end": 2.0, "narration": "第二段。"},
    ]

    def fake_synthesize_segment(i, seg, narration_data, tts_dir, engine):
        if i == 1:
            raise RuntimeError("network timeout")
        return {
            "index": i,
            "start": seg["start"],
            "end": seg["end"],
            "narration": seg["narration"],
            "audio_path": str(tts_dir / f"narr_{i:03d}.wav"),
            "audio_duration": 0.5,
        }

    monkeypatch.setitem(CONFIG, "mimo_tts_api_key", "tp-test")
    monkeypatch.setitem(CONFIG, "tts_workers", 2)
    monkeypatch.setitem(CONFIG, "allow_partial_tts", True)
    monkeypatch.setattr("voiceover._synthesize_segment", fake_synthesize_segment)

    segments, engine = synthesize_tts(narration, tmp_path)

    assert engine == "mimo-tts"
    assert [s["index"] for s in segments] == [0]


def test_synthesize_tts_rejects_all_failed_segments_even_when_partial_allowed(monkeypatch, tmp_path):
    narration = [{"start": 0.0, "end": 1.0, "narration": "唯一段。"}]

    def fail(*args, **kwargs):
        raise RuntimeError("network timeout")

    monkeypatch.setitem(CONFIG, "mimo_tts_api_key", "tp-test")
    monkeypatch.setitem(CONFIG, "allow_partial_tts", True)
    monkeypatch.setattr("voiceover._synthesize_segment", fail)

    with pytest.raises(RuntimeError, match="没有生成任何有效解说音频"):
        synthesize_tts(narration, tmp_path)


def test_cleanup_partial_outputs_only_replaces_audio_suffix(tmp_path):
    work = tmp_path / "job.wav"
    tts_dir = work / "tts_segments"
    tts_dir.mkdir(parents=True)
    output_wav = tts_dir / "narr_000.wav"
    output_wav.write_bytes(b"partial-wav")
    expected_mp3 = tts_dir / "narr_000.mp3"
    expected_mp3.write_bytes(b"partial-mp3")
    cache = Path(str(output_wav) + ".cache.json")
    cache.write_text("{}", encoding="utf-8")

    unrelated = tmp_path / "job.mp3" / "tts_segments" / "narr_000.mp3"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(b"do-not-delete")

    voiceover._cleanup_partial_tts_outputs(output_wav)

    assert not output_wav.exists()
    assert not expected_mp3.exists()
    assert not cache.exists()
    assert unrelated.read_bytes() == b"do-not-delete"


def test_detect_tts_engine_requires_mimo_key(monkeypatch):
    """MiMo TTS is the only engine; with no key configured it must raise (no edge fallback)."""
    monkeypatch.setitem(CONFIG, "mimo_tts_api_key", "")
    with pytest.raises(RuntimeError, match="没有可用的 TTS 引擎|MiMo"):
        _detect_tts_engine()


def test_detect_tts_engine_returns_mimo_when_key_set(monkeypatch):
    monkeypatch.setitem(CONFIG, "mimo_tts_api_key", "tp-secret")
    assert _detect_tts_engine() == "mimo-tts"


def test_resolve_tts_engine_returns_mimo_when_key_set(monkeypatch):
    monkeypatch.setitem(CONFIG, "mimo_tts_api_key", "tp-secret")
    assert resolve_tts_engine() == "mimo-tts"


def test_resolve_tts_engine_raises_without_key(monkeypatch):
    monkeypatch.setitem(CONFIG, "mimo_tts_api_key", "")
    with pytest.raises(RuntimeError, match="没有可用的 TTS 引擎|MiMo"):
        resolve_tts_engine()


def test_resolve_tts_engine_prefers_existing_when_no_key(monkeypatch):
    """Assemble-only reruns may reuse already-generated audio even without a fresh key."""
    monkeypatch.setitem(CONFIG, "mimo_tts_api_key", "")
    assert resolve_tts_engine(prefer_existing="mimo-tts") == "mimo-tts"


def test_run_tts_engine_supports_mimo_branch(monkeypatch, tmp_path):
    import base64

    def fake_api_call(payload):
        return {"choices": [{"message": {"audio": {"data": base64.b64encode(b"wav").decode("ascii")}}}]}

    output = tmp_path / "out.wav"
    monkeypatch.setitem(CONFIG, "tts_retries", 1)
    monkeypatch.setattr("voiceover.mimo_tts_api_call", fake_api_call)
    monkeypatch.setattr("voiceover._get_audio_duration", lambda path: 1.0)

    _run_tts_engine("mimo-tts", "这是小米 MiMo 配音。", output)

    assert output.read_bytes() == b"wav"


def test_run_tts_engine_rejects_removed_engines(monkeypatch, tmp_path):
    monkeypatch.setitem(CONFIG, "tts_retries", 1)
    monkeypatch.setattr("voiceover._get_audio_duration", lambda path: 0.0)

    with pytest.raises(RuntimeError, match="不支持的 TTS 引擎"):
        _run_tts_engine("edge-tts", "测试。", tmp_path / "out.wav")


def test_mimo_tts_writes_decoded_audio(monkeypatch, tmp_path):
    import base64

    seen_payloads = []

    def fake_api_call(payload):
        seen_payloads.append(payload)
        return {"choices": [{"message": {"audio": {"data": base64.b64encode(b"wav-bytes").decode("ascii")}}}]}

    monkeypatch.setitem(CONFIG, "mimo_tts_model", "mimo-v2.5-tts")
    monkeypatch.setitem(CONFIG, "mimo_tts_voice", "冰糖")
    monkeypatch.setattr("voiceover.mimo_tts_api_call", fake_api_call)

    output = tmp_path / "out.wav"
    _tts_mimo("这是小米 MiMo 配音。", output, rate="-5%", pitch="+0Hz")

    assert output.read_bytes() == b"wav-bytes"
    payload = seen_payloads[0]
    assert payload["model"] == "mimo-v2.5-tts"
    assert payload["audio"] == {"format": "wav", "voice": "冰糖"}
    assert payload["messages"][1] == {"role": "assistant", "content": "这是小米 MiMo 配音。"}
    assert "语速略慢" in payload["messages"][0]["content"]


def test_mimo_tts_injects_per_beat_emotion(monkeypatch, tmp_path):
    import base64

    seen = []

    def fake_api_call(payload):
        seen.append(payload)
        return {"choices": [{"message": {"audio": {"data": base64.b64encode(b"x").decode("ascii")}}}]}

    monkeypatch.setattr("voiceover.mimo_tts_api_call", fake_api_call)
    # emotion routed into the user-message instruction (MiMo instruct-TTS)
    _tts_mimo("就在这一刻，所有人都沉默了。", tmp_path / "e.wav", emotion="紧张 深沉")
    instruction = seen[0]["messages"][0]["content"]
    assert "紧张 深沉" in instruction
    # no emotion -> still an expressive (non-robotic) directive, no leftover tag
    _tts_mimo("普通解说。", tmp_path / "n.wav")
    assert "「" not in seen[1]["messages"][0]["content"]
    assert "有起伏" in seen[1]["messages"][0]["content"]


def test_tts_cache_key_changes_with_emotion():
    base = {"start": 0.0, "end": 2.0, "narration": "测试。"}
    k_plain = voiceover._tts_segment_cache_key("mimo-tts", 0, base, "测试。", "+0%", "+0Hz")
    k_emo = voiceover._tts_segment_cache_key("mimo-tts", 0, {**base, "emotion": "悲伤"}, "测试。", "+0%", "+0Hz")
    assert k_plain != k_emo, "changing a beat's emotion must invalidate its TTS cache"



def test_voiceover_cli_prefers_mapped_narration_when_present(monkeypatch, tmp_path):
    """Cut mode remaps narration to the output timeline; a direct run must voice the
    mapped file (the documented default) rather than the source-timeline narration."""
    import json

    (tmp_path / "narration.json").write_text(
        json.dumps([{"start": 0.0, "end": 1.0, "narration": "full source。"}]),
        encoding="utf-8",
    )
    (tmp_path / "narration_mapped.json").write_text(
        json.dumps([{"start": 0.0, "end": 1.0, "narration": "cut mapped。"}]),
        encoding="utf-8",
    )
    seen = {}

    def fake_synthesize(narration, work_dir):
        seen["narration"] = narration
        return ([{
            "index": 0,
            "start": narration[0]["start"],
            "end": narration[0]["end"],
            "narration": narration[0]["narration"],
            "audio_path": str(Path(work_dir) / "tts_segments" / "narr_000.wav"),
            "audio_duration": 0.5,
        }], "mimo-tts")

    monkeypatch.setattr("voiceover.synthesize_tts", fake_synthesize)
    monkeypatch.setattr(sys, "argv", ["voiceover.py", "--work-dir", str(tmp_path)])

    voiceover.main()

    meta = json.loads((tmp_path / "tts_meta.json").read_text(encoding="utf-8"))
    assert seen["narration"][0]["narration"] == "cut mapped。"
    assert meta["narration"] == "narration_mapped.json"


def test_voiceover_cli_accepts_allow_partial_tts(monkeypatch, tmp_path):
    narration = [
        {"start": 0.0, "end": 1.0, "narration": "第一段。"},
        {"start": 1.0, "end": 2.0, "narration": "第二段。"},
    ]
    (tmp_path / "narration.json").write_text(__import__("json").dumps(narration), encoding="utf-8")

    def fake_synthesize_segment(i, seg, narration_data, tts_dir, engine):
        if i == 1:
            raise RuntimeError("network timeout")
        return {
            "index": i,
            "start": seg["start"],
            "end": seg["end"],
            "narration": seg["narration"],
            "audio_path": str(tts_dir / f"narr_{i:03d}.wav"),
            "audio_duration": 0.5,
        }

    monkeypatch.setitem(CONFIG, "mimo_tts_api_key", "tp-test")
    monkeypatch.setitem(CONFIG, "allow_partial_tts", False)
    monkeypatch.setattr("voiceover._synthesize_segment", fake_synthesize_segment)
    monkeypatch.setattr(sys, "argv", ["voiceover.py", "--work-dir", str(tmp_path), "--allow-partial-tts"])

    voiceover.main()

    meta = __import__("json").loads((tmp_path / "tts_meta.json").read_text(encoding="utf-8"))
    assert len(meta["segments"]) == 1
    assert CONFIG["allow_partial_tts"] is True
