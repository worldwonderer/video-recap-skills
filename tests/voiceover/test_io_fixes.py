import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'video-voiceover' / 'scripts'))
import json  # noqa: F401
import subprocess  # noqa: F401
from subprocess import CompletedProcess
import pytest
import voiceover as tts
from voiceover import _tts_edge


def test_edge_mp3_to_wav_failure_surfaces_error_and_keeps_mp3(monkeypatch, tmp_path):
    """mp3->wav 转换非零退出应抛错，且不应删除 mp3 源。"""
    output_wav = tmp_path / "narr_000.wav"
    mp3_path = tmp_path / "narr_000.mp3"

    monkeypatch.setitem(tts.CONFIG, "edge_tts_voice", "zh-CN-YunxiNeural")
    monkeypatch.setitem(tts.CONFIG, "tts_timeout", 90)

    def fake_run_cmd(cmd, **kwargs):
        if cmd[0] == "edge-tts":
            # 模拟 edge-tts 成功写出 mp3
            Path(mp3_path).write_bytes(b"fake-mp3")
            return CompletedProcess(cmd, 0, stdout="", stderr="")
        # ffmpeg 转换失败
        return CompletedProcess(cmd, 1, stdout="", stderr="conversion broke")

    removed = []
    monkeypatch.setattr("voiceover.run_cmd", fake_run_cmd)
    monkeypatch.setattr("voiceover.os.remove", lambda p: removed.append(p))

    with pytest.raises(RuntimeError, match="mp3 转 WAV 失败"):
        _tts_edge("测试文本", output_wav)

    # mp3 源不能在转换成功前被删除
    assert removed == []
    assert mp3_path.exists()
