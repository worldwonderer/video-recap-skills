import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'video-voiceover' / 'scripts'))
import json
import pytest  # noqa: F401
from subprocess import CompletedProcess  # noqa: F401
from lib import CONFIG
from voiceover import _build_tts_segment_result, _detect_tts_engine, _parse_rate_offset, _run_tts_engine, _tts_mimo, resolve_tts_engine, synthesize_tts


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


def test_synthesize_tts_handles_empty_narration(monkeypatch, tmp_path):
    monkeypatch.setitem(__import__("lib").CONFIG, "tts_engine", "edge-tts")
    segments, engine = synthesize_tts([], tmp_path)

    assert segments == []
    assert engine == "edge-tts"


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

    monkeypatch.setitem(CONFIG, "tts_engine", "edge-tts")
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

    monkeypatch.setitem(CONFIG, "tts_engine", "edge-tts")
    monkeypatch.setitem(CONFIG, "tts_workers", 2)
    monkeypatch.setitem(CONFIG, "allow_partial_tts", True)
    monkeypatch.setattr("voiceover._synthesize_segment", fake_synthesize_segment)

    segments, engine = synthesize_tts(narration, tmp_path)

    assert engine == "edge-tts"
    assert [s["index"] for s in segments] == [0]


def test_detect_tts_engine_prefers_edge_tts(monkeypatch):
    monkeypatch.setattr("voiceover.shutil.which", lambda cmd: "/usr/bin/edge-tts" if cmd == "edge-tts" else None)

    assert _detect_tts_engine() == "edge-tts"


def test_resolve_tts_engine_prefers_mimo_in_auto_and_allows_explicit_edge(monkeypatch):
    monkeypatch.setitem(CONFIG, "tts_engine", "auto")
    monkeypatch.setitem(CONFIG, "mimo_tts_api_key", "mimo-secret")
    monkeypatch.setattr("voiceover.shutil.which", lambda cmd: "/usr/bin/edge-tts" if cmd == "edge-tts" else None)

    assert resolve_tts_engine() == "mimo-tts"

    monkeypatch.setitem(CONFIG, "tts_engine", "edge-tts")
    assert resolve_tts_engine() == "edge-tts"


def test_detect_tts_engine_does_not_fallback_to_removed_say(monkeypatch):
    monkeypatch.setitem(CONFIG, "api_provider", "openai")
    monkeypatch.setitem(CONFIG, "api_url", "https://api.openai.com/v1/chat/completions")
    monkeypatch.setitem(CONFIG, "api_key", "")
    monkeypatch.setattr("voiceover.shutil.which", lambda cmd: "/usr/bin/say" if cmd == "say" else None)

    with pytest.raises(RuntimeError, match="edge-tts|MiMo"):
        _detect_tts_engine()


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
        _run_tts_engine("say", "测试。", tmp_path / "out.wav")


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


def test_detect_tts_engine_prefers_mimo_when_provider_configured(monkeypatch):
    monkeypatch.setitem(CONFIG, "api_provider", "openai")
    monkeypatch.setitem(CONFIG, "api_url", "https://example.com/v1/chat/completions")
    monkeypatch.setitem(CONFIG, "api_key", "doubao-secret")
    monkeypatch.setitem(CONFIG, "mimo_tts_api_key", "mimo-secret")
    monkeypatch.setattr("voiceover.shutil.which", lambda cmd: "/usr/bin/edge-tts" if cmd == "edge-tts" else None)

    assert _detect_tts_engine() == "mimo-tts"
