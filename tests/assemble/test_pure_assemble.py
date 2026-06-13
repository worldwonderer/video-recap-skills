import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'video-assemble' / 'scripts'))
import pytest  # noqa: F401
from subprocess import CompletedProcess  # noqa: F401
from assemble import _build_audio_filter_complex, _build_timed_narration, _escape_ass_text, _generate_ass, _generate_srt, _seconds_to_ass_time, _seconds_to_srt_time, _subtitle_burn_filter, assemble_video, assembly_settings_fingerprint, final_loudnorm_filter
from lib import CONFIG


def test_seconds_to_srt_time():
    # 3661.5s = 1h 1m 1.5s
    result = _seconds_to_srt_time(3661.5)
    assert result.startswith("01:01:01")
    # 0s
    assert _seconds_to_srt_time(0) == "00:00:00,000"


def test_seconds_to_ass_time():
    assert _seconds_to_ass_time(3661.5) == "1:01:01.50"
    assert _seconds_to_ass_time(0) == "0:00:00.00"


def test_generate_srt_uses_actual_placement(tmp_path):
    _generate_srt([
        {"start": 0.0, "end": 2.0, "actual_place_start": 0.5, "actual_place_end": 1.7, "narration": "真实放置时间。"},
        {"start": 3.0, "end": 3.05, "narration": "过短跳过。"},
    ], tmp_path)

    srt = (tmp_path / "subtitles.srt").read_text(encoding="utf-8")
    assert "00:00:00,500 --> 00:00:01,700" in srt
    assert "真实放置时间" in srt
    assert "过短跳过" not in srt


def test_generate_ass_escapes_text_and_writes_style(tmp_path):
    _generate_ass([
        {
            "start": 1.0,
            "end": 4.0,
            "actual_place_start": 1.25,
            "actual_place_end": 3.5,
            "narration": "第一行{重点}\\路径\n第二行",
        }
    ], tmp_path)

    ass = (tmp_path / "subtitles.ass").read_text(encoding="utf-8")
    assert "[V4+ Styles]" in ass
    assert "Style: Default" in ass
    assert "Dialogue: 0,0:00:01.25,0:00:03.50" in ass
    assert r"第一行\{重点\}\\路径\N第二行" in ass
    assert _escape_ass_text("{x}\\y") == r"\{x\}\\y"


def test_build_timed_narration_clamps_delay_to_slot(monkeypatch, tmp_path):
    import wave

    wav = tmp_path / "narr.wav"
    sample_rate = 44100
    sample_count = int(sample_rate * 0.8)
    with wave.open(str(wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\0\0" * sample_count)

    segment = {
        "index": 0,
        "start": 0.0,
        "end": 1.0,
        "narration": "短槽位解说。",
        "audio_path": str(wav),
        "audio_duration": 0.8,
    }
    monkeypatch.setitem(CONFIG, "narration_delay_seconds", 1.5)
    monkeypatch.setitem(CONFIG, "narration_tail_pad_seconds", 0.1)

    _build_timed_narration([segment], tmp_path / "out.wav", 2.0, tmp_path)

    assert segment["actual_place_start"] == pytest.approx(0.1, abs=0.02)
    assert segment["actual_place_end"] == pytest.approx(0.9, abs=0.02)


def test_subtitle_burn_filter_escapes_path():
    path = Path("/tmp/video recap/a:b,c[1].ass")
    filt = _subtitle_burn_filter(path)
    assert filt.startswith("subtitles=")
    assert "video recap" in filt
    assert r"a\:b\,c\[1\].ass" in filt


def test_assemble_video_burns_ass_subtitles(monkeypatch, tmp_path):
    video = tmp_path / "input.mp4"
    video.write_bytes(b"video")
    wav = tmp_path / "narr.wav"
    wav.write_bytes(b"wav")
    output = tmp_path / "output.mp4"
    commands = []

    def fake_run_cmd(cmd):
        commands.append(cmd)
        output.write_bytes(b"mp4")
        return CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setitem(CONFIG, "burn_subtitles", True)
    monkeypatch.setattr("assemble.get_video_duration", lambda path: 4.0)
    monkeypatch.setattr("assemble._build_timed_narration", lambda segments, out, duration, wd: Path(out).write_bytes(b"narration"))
    monkeypatch.setattr("assemble.run_cmd", fake_run_cmd)

    assemble_video(video, [{
        "start": 0.0,
        "end": 3.0,
        "actual_place_start": 0.2,
        "actual_place_end": 2.5,
        "narration": "压制字幕。",
        "audio_path": str(wav),
        "audio_duration": 1.0,
    }], tmp_path, output)

    ffmpeg_cmd = commands[-1]
    assert (tmp_path / "subtitles.srt").exists()
    assert (tmp_path / "subtitles.ass").exists()
    assert "-vf" in ffmpeg_cmd
    assert any(str(arg).startswith("subtitles=") for arg in ffmpeg_cmd)
    assert "-c:v" in ffmpeg_cmd
    assert "libx264" in ffmpeg_cmd


def test_assemble_video_without_burn_keeps_video_copy(monkeypatch, tmp_path):
    video = tmp_path / "input.mp4"
    video.write_bytes(b"video")
    output = tmp_path / "output.mp4"
    commands = []

    def fake_run_cmd(cmd):
        commands.append(cmd)
        output.write_bytes(b"mp4")
        return CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setitem(CONFIG, "burn_subtitles", False)
    monkeypatch.setitem(CONFIG, "force_video_reencode", False)
    monkeypatch.setattr("assemble.get_video_duration", lambda path: 4.0)
    monkeypatch.setattr("assemble._build_timed_narration", lambda segments, out, duration, wd: Path(out).write_bytes(b"narration"))
    monkeypatch.setattr("assemble.run_cmd", fake_run_cmd)

    assemble_video(video, [{
        "start": 0.0,
        "end": 3.0,
        "narration": "外挂字幕仍生成。",
        "audio_path": str(tmp_path / "narr.wav"),
        "audio_duration": 1.0,
    }], tmp_path, output)

    ffmpeg_cmd = commands[-1]
    assert (tmp_path / "subtitles.srt").exists()
    assert not (tmp_path / "subtitles.ass").exists()
    assert "-vf" not in ffmpeg_cmd
    assert ffmpeg_cmd[ffmpeg_cmd.index("-c:v") + 1] == "copy"


def test_build_audio_filter_complex_dynamic_envelope_default(monkeypatch):
    monkeypatch.setitem(CONFIG, "ducking_mode", "fixed")
    monkeypatch.setitem(CONFIG, "ducking_orig_volume", 0.5)
    monkeypatch.setitem(CONFIG, "speech_ducking_volume", 0.2)
    monkeypatch.setitem(CONFIG, "zone_ducking_volume", 0.12)
    fc = _build_audio_filter_complex([
        {"actual_place_start": 0.0, "actual_place_end": 2.0, "overlaps_speech": True},
        {"actual_place_start": 5.0, "actual_place_end": 7.0, "overlaps_speech": False},
    ])
    assert "volume='max(0,min(1," in fc  # per-segment envelope
    assert "eval=frame" in fc
    assert "between(t,0.00,2.00)" in fc
    assert "between(t,5.00,7.00)" in fc
    assert fc.endswith("[aout]")


def test_build_audio_filter_complex_fixed_when_all_overlap(monkeypatch):
    monkeypatch.setitem(CONFIG, "ducking_mode", "fixed")
    monkeypatch.setitem(CONFIG, "ducking_orig_volume", 0.5)
    fc = _build_audio_filter_complex([
        {"actual_place_start": 0.0, "actual_place_end": 2.0, "overlaps_speech": True},
    ])
    assert "volume=0.5,aresample=48000[orig]" in fc  # constant, not an envelope
    assert "eval=frame" not in fc
    assert fc.endswith("[aout]")


def test_build_audio_filter_complex_trapezoid_when_all_quiet(monkeypatch):
    monkeypatch.setitem(CONFIG, "ducking_mode", "fixed")
    monkeypatch.setitem(CONFIG, "zone_ducking_volume", 0.12)
    monkeypatch.setitem(CONFIG, "zone_fade_seconds", 0.5)
    fc = _build_audio_filter_complex([
        {"actual_place_start": 1.0, "actual_place_end": 4.0, "overlaps_speech": False},
    ])
    assert "min(t-1.00,4.00-t)/0.5" in fc  # smooth trapezoid fade
    assert "eval=frame" in fc


def test_build_audio_filter_complex_explicit_modes(monkeypatch):
    segs = [{"actual_place_start": 0.0, "actual_place_end": 2.0, "overlaps_speech": True}]
    monkeypatch.setitem(CONFIG, "ducking_mode", "none")
    none_fc = _build_audio_filter_complex(segs)
    assert "sidechaincompress" not in none_fc
    assert "volume='" not in none_fc  # no envelope in 'none' mode
    monkeypatch.setitem(CONFIG, "ducking_mode", "sidechaincompress")
    assert "sidechaincompress" in _build_audio_filter_complex(segs)


def test_assembly_settings_fingerprint_tracks_burn_style(monkeypatch):
    monkeypatch.setitem(CONFIG, "burn_subtitles", False)
    plain = assembly_settings_fingerprint()
    monkeypatch.setitem(CONFIG, "burn_subtitles", True)
    burned = assembly_settings_fingerprint()
    monkeypatch.setitem(CONFIG, "subtitle_font_size", 50)
    bigger = assembly_settings_fingerprint()

    assert plain["burn_subtitles"] is False
    assert burned["burn_subtitles"] is True
    assert burned["subtitle_renderer"] == "ass"
    assert bigger != burned


def test_final_loudnorm_filter_and_fingerprint(monkeypatch):
    monkeypatch.setitem(CONFIG, "final_loudnorm", True)
    monkeypatch.setitem(CONFIG, "target_lufs", -14.0)
    monkeypatch.setitem(CONFIG, "target_true_peak", -1.0)
    monkeypatch.setitem(CONFIG, "target_lra", 11.0)
    assert final_loudnorm_filter() == "loudnorm=I=-14.0:TP=-1.0:LRA=11.0"
    assert assembly_settings_fingerprint()["audio_mix"]["final_loudnorm"] == "loudnorm=I=-14.0:TP=-1.0:LRA=11.0"

    monkeypatch.setitem(CONFIG, "target_lufs", -11.9)
    assert final_loudnorm_filter() == "loudnorm=I=-11.9:TP=-1.0:LRA=11.0"

    monkeypatch.setitem(CONFIG, "final_loudnorm", False)
    assert final_loudnorm_filter() is None
    assert assembly_settings_fingerprint()["audio_mix"]["final_loudnorm"] == "off"
