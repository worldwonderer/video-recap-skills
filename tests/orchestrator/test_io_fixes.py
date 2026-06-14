import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'video-recap' / 'scripts'))
import pytest  # noqa: F401
import doctor


def _tools_present(monkeypatch):
    monkeypatch.setattr("doctor._ffmpeg_filters", lambda: {"subtitles", "ass"})
    monkeypatch.setattr(
        "doctor._command_path",
        lambda name: f"/usr/bin/{name}" if name in ("ffmpeg", "ffprobe") else None,
    )


def test_doctor_ok_when_tools_and_mimo_key_present(monkeypatch):
    _tools_present(monkeypatch)
    for k in ("api_key", "mimo_asr_api_key", "mimo_tts_api_key", "mimo_video_api_key"):
        monkeypatch.setitem(doctor.CONFIG, k, "tp-test-key")

    report = doctor.build_report()

    assert report["ok"] is True
    assert report["failures"] == []


def test_doctor_fails_without_mimo_key(monkeypatch):
    _tools_present(monkeypatch)
    monkeypatch.setitem(doctor.CONFIG, "api_key", "")
    monkeypatch.setitem(doctor.CONFIG, "mimo_asr_api_key", "")

    report = doctor.build_report()

    assert report["ok"] is False
    assert any("MIMO_API_KEY" in f for f in report["failures"])


def test_doctor_missing_ffmpeg_is_failure(monkeypatch):
    monkeypatch.setattr("doctor._ffmpeg_filters", lambda: set())
    monkeypatch.setattr("doctor._command_path", lambda name: None)
    monkeypatch.setitem(doctor.CONFIG, "api_key", "tp-x")

    report = doctor.build_report()

    assert report["ok"] is False
    assert any("ffmpeg" in f for f in report["failures"])


def test_doctor_warns_when_asr_unconfigured_but_key_present(monkeypatch):
    """api_key powers VLM/TTS; an empty ASR key is only a warning (use --skip-asr)."""
    _tools_present(monkeypatch)
    monkeypatch.setitem(doctor.CONFIG, "api_key", "tp-x")
    monkeypatch.setitem(doctor.CONFIG, "mimo_asr_api_key", "")

    report = doctor.build_report()

    assert report["ok"] is True
    assert any("ASR not configured" in w for w in report["warnings"])
