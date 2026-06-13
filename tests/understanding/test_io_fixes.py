import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'video-understanding' / 'scripts'))
import json  # noqa: F401
import subprocess  # noqa: F401
from subprocess import CompletedProcess
import pytest  # noqa: F401
import asr
import extract


def test_segment_cut_failure_yields_empty_text_not_stale_transcription(monkeypatch, tmp_path):
    """切分失败的段应返回空文本，而不是对磁盘陈旧音频转录。"""
    segments_dir = tmp_path / "audio_segments"
    segments_dir.mkdir()
    audio_wav = tmp_path / "audio.wav"
    audio_wav.write_bytes(b"")

    def fake_run_cmd(cmd, **kwargs):
        # 第一段切分成功，第二段切分失败
        seg_target = cmd[-1] if isinstance(cmd, list) else ""
        if "seg_000.wav" in str(seg_target):
            return CompletedProcess(cmd, 0, stdout="", stderr="")
        return CompletedProcess(cmd, 1, stdout="", stderr="cut failed")

    def fake_run_asr(wav_path):
        # 如果切分失败仍调用 ASR，会返回这段污染文本
        return "STALE-GARBAGE"

    monkeypatch.setattr("asr.run_cmd", fake_run_cmd)
    monkeypatch.setattr("asr._run_asr", fake_run_asr)

    results = asr._segment_and_transcribe(audio_wav, segments_dir, total_duration=60.0, segment_length=30)

    assert len(results) == 2
    assert results[0]["text"] == "STALE-GARBAGE"   # 成功段照常转录
    assert results[1]["text"] == ""


def test_zero_duration_does_not_fabricate_180s_timestamps(monkeypatch, tmp_path):
    """get_video_duration 返回 0 时应警告并返回空 ASR，而不是伪造 0-180s 时间戳。"""
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"")

    def fake_run_cmd(cmd, **kwargs):
        # 音频提取这一步成功，其余不应被调用
        return CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("asr.run_cmd", fake_run_cmd)
    monkeypatch.setattr("asr.get_video_duration", lambda path: 0.0)

    def boom(*args, **kwargs):
        raise AssertionError("时长为 0 时不应进行任何转录")

    monkeypatch.setattr("asr._run_asr", boom)
    monkeypatch.setattr("asr._segment_and_transcribe", boom)

    result = asr.transcribe_audio(video_path, tmp_path)

    assert result == []
    saved = json.loads((tmp_path / "asr_result.json").read_text())
    assert saved == []
    # 绝不应出现伪造的 0-180s 时间戳
    assert not any(s.get("end") == 180.0 for s in saved)


def test_extract_frames_returns_only_current_run_frames(monkeypatch, tmp_path):
    """复用 work_dir 时，上一次更高编号的陈旧帧不应泄漏进结果。"""
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    # 上一次高 fps 运行残留的陈旧帧
    for n in range(1, 6):
        (frames_dir / f"frame_{n:05d}.jpg").write_bytes(b"stale")

    monkeypatch.setitem(extract.CONFIG, "fps", 1)

    def fake_run_cmd(cmd, **kwargs):
        # 本次只产出 2 帧
        (frames_dir / "frame_00001.jpg").write_bytes(b"new")
        (frames_dir / "frame_00002.jpg").write_bytes(b"new")
        return CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("extract.run_cmd", fake_run_cmd)

    frames = extract.extract_frames(tmp_path / "video.mp4", tmp_path, fps=1)

    assert len(frames) == 2
    assert [f.name for f in frames] == ["frame_00001.jpg", "frame_00002.jpg"]
