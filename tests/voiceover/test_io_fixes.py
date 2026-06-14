import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'video-voiceover' / 'scripts'))
import pytest
import voiceover as tts
from voiceover import _run_tts_engine, _tts_mimo


def test_mimo_tts_missing_audio_data_raises(monkeypatch, tmp_path):
    """A MiMo TTS response without audio.data must raise, not write a silent wav."""
    monkeypatch.setattr("voiceover.mimo_tts_api_call", lambda payload: {"choices": [{"message": {}}]})
    with pytest.raises(RuntimeError, match="缺少 audio.data"):
        _tts_mimo("测试文本", tmp_path / "out.wav")


def test_mimo_tts_invalid_base64_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "voiceover.mimo_tts_api_call",
        lambda payload: {"choices": [{"message": {"audio": {"data": "!!!not-base64!!!"}}}]},
    )
    with pytest.raises(RuntimeError, match="base64"):
        _tts_mimo("测试文本", tmp_path / "out.wav")


def test_run_tts_engine_retries_then_raises_and_cleans_partial(monkeypatch, tmp_path):
    """Repeated MiMo failures: retry up to tts_retries, remove partial output, surface the error."""
    output = tmp_path / "narr_000.wav"
    output.write_bytes(b"stale-partial")
    calls = []

    def boom(text, path, rate="+0%", pitch="+0Hz"):
        calls.append(1)
        raise RuntimeError("mimo down")

    monkeypatch.setitem(tts.CONFIG, "tts_retries", 2)
    monkeypatch.setattr("voiceover._tts_mimo", boom)
    monkeypatch.setattr("voiceover.time.sleep", lambda s: None)

    with pytest.raises(RuntimeError, match="mimo down|合成失败"):
        _run_tts_engine("mimo-tts", "测试", output)

    assert len(calls) == 2          # retried tts_retries times
    assert not output.exists()      # partial output cleaned up
