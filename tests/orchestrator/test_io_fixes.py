import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'video-recap' / 'scripts'))
import json  # noqa: F401
import subprocess
from subprocess import CompletedProcess  # noqa: F401
import pytest  # noqa: F401
import doctor


def test_doctor_smoke_timeout_is_handled_not_raised(monkeypatch):
    """edge-tts 网络挂起（TimeoutExpired）应被吞掉转为 skipped，而不是抛出 traceback。"""
    monkeypatch.setattr("doctor._command_path", lambda name: f"/usr/bin/{name}")

    def fake_run(cmd, *, timeout=20):
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr("doctor._run", fake_run)

    result = doctor._check_tts_smoke("zh-CN-YunxiNeural")

    assert result.get("skipped") is True
    assert result.get("ok") is False


def test_doctor_skipped_smoke_not_marked_as_failure(monkeypatch):
    """跳过的冒烟测试应记为 warning，doctor 不应因此判为 FAILED。"""
    # 系统工具齐备，TTS 可用，仅冒烟测试被跳过
    monkeypatch.setattr("doctor._ffmpeg_filters", lambda: {"subtitles", "ass"})
    monkeypatch.setattr(
        "doctor._command_path",
        lambda name: f"/usr/bin/{name}" if name in ("ffmpeg", "ffprobe") else None,
    )
    monkeypatch.setitem(doctor.CONFIG, "mimo_tts_api_key", "tp-test-key")
    monkeypatch.setattr(
        "doctor._check_tts_smoke",
        lambda voice: {"ok": False, "skipped": True, "reason": "edge-tts not found"},
    )

    report = doctor.build_report(tts_smoke=True)

    assert report["ok"] is True
    assert "edge-tts smoke test failed" not in report["failures"]
    assert any("smoke test skipped" in w for w in report["warnings"])
