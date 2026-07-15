import sys
from pathlib import Path

sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parents[2]
        / "skills"
        / "video-understanding"
        / "scripts"
    ),
)
import json  # noqa: F401
import subprocess  # noqa: F401
from subprocess import CompletedProcess
import pytest  # noqa: F401
import asr
import extract
import understanding_runner as understand


def test_segment_cut_failure_yields_empty_text_not_stale_transcription(
    monkeypatch, tmp_path
):
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

    results = asr._segment_and_transcribe(
        audio_wav, segments_dir, total_duration=60.0, segment_length=30
    )

    assert len(results) == 2
    assert results[0]["text"] == "STALE-GARBAGE"  # 成功段照常转录
    assert results[1]["text"] == ""


def test_zero_duration_does_not_fabricate_180s_timestamps(monkeypatch, tmp_path):
    """get_video_duration 返回 0 时应警告并返回空 ASR，而不是伪造 0-180s 时间戳。"""
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"")

    def fake_run_cmd(cmd, **kwargs):
        # 音频提取这一步成功，其余不应被调用
        return CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setitem(
        asr.CONFIG, "mimo_asr_api_key", "tp-test"
    )  # 跳过无 key 提前返回，测真正的零时长分支
    monkeypatch.setattr("asr.run_cmd", fake_run_cmd)
    monkeypatch.setattr("asr.get_video_duration", lambda path: 0.0)

    def boom(*args, **kwargs):
        raise AssertionError("时长为 0 时不应进行任何转录")

    monkeypatch.setattr("asr._run_asr", boom)
    monkeypatch.setattr("asr._segment_and_transcribe", boom)

    result = asr.transcribe_audio(video_path, tmp_path)

    assert result == []
    saved = json.loads((tmp_path / "asr_result.json").read_text(encoding="utf-8"))
    assert saved == []
    # 绝不应出现伪造的 0-180s 时间戳
    assert not any(s.get("end") == 180.0 for s in saved)


def test_run_asr_builds_mimo_payload_and_parses_content(monkeypatch, tmp_path):
    """_run_asr base64-encodes the wav into a MiMo input_audio message and reads content."""
    wav = tmp_path / "seg.wav"
    wav.write_bytes(b"RIFFfake-wav-bytes")
    monkeypatch.setitem(asr.CONFIG, "mimo_asr_api_key", "tp-test")
    monkeypatch.setitem(asr.CONFIG, "mimo_asr_model", "mimo-v2.5-asr")
    monkeypatch.setitem(asr.CONFIG, "mimo_asr_language", "auto")

    seen = {}

    def fake_call(payload):
        seen["payload"] = payload
        return {"choices": [{"message": {"content": "  你好，世界。 "}}]}

    monkeypatch.setattr("asr.mimo_asr_api_call", fake_call)
    text = asr._run_asr(wav)

    assert text == "你好，世界。"
    payload = seen["payload"]
    assert payload["model"] == "mimo-v2.5-asr"
    assert payload["asr_options"] == {"language": "auto"}
    audio = payload["messages"][0]["content"][0]
    assert audio["type"] == "input_audio"
    assert audio["input_audio"]["data"].startswith("data:audio/wav;base64,")


def test_run_asr_api_failure_raises_instead_of_silent_empty(monkeypatch, tmp_path):
    """Transient ASR API failures must not become cached empty transcript text."""
    wav = tmp_path / "seg.wav"
    wav.write_bytes(b"RIFFfake-wav-bytes")
    monkeypatch.setitem(asr.CONFIG, "mimo_asr_api_key", "tp-test")

    def fail(payload):
        raise RuntimeError("quota")

    monkeypatch.setattr("asr.mimo_asr_api_call", fail)
    with pytest.raises(RuntimeError, match="MiMo ASR 调用失败"):
        asr._run_asr(wav)


def test_run_asr_skips_oversize_segment(monkeypatch, tmp_path):
    """A segment whose base64 exceeds the MiMo cap is skipped, not sent (10MB API limit)."""
    wav = tmp_path / "big.wav"
    wav.write_bytes(b"x" * (2 * 1024 * 1024))  # ~2.7MB base64 > 1MB cap
    monkeypatch.setitem(asr.CONFIG, "mimo_asr_api_key", "tp-test")
    monkeypatch.setitem(asr.CONFIG, "mimo_asr_base64_max_mb", 1.0)

    def boom(payload):
        raise AssertionError("oversize segment must not hit the API")

    monkeypatch.setattr("asr.mimo_asr_api_call", boom)
    assert asr._run_asr(wav) == ""


def test_transcribe_audio_without_key_returns_empty(monkeypatch, tmp_path):
    """No MiMo ASR key -> skip cleanly (write []), never extract or call the API."""
    video = tmp_path / "v.mp4"
    video.write_bytes(b"")
    monkeypatch.setitem(asr.CONFIG, "mimo_asr_api_key", "")

    def boom(*a, **k):
        raise AssertionError("must not run ffmpeg/ASR without a key")

    monkeypatch.setattr("asr.run_cmd", boom)
    assert asr.transcribe_audio(video, tmp_path) == []
    assert json.loads((tmp_path / "asr_result.json").read_text(encoding="utf-8")) == []


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


def test_understand_reextracts_frames_when_source_video_changes(monkeypatch, tmp_path):
    """understanding_runner.py must not skip stale frames just because work_dir/frames exists."""
    old_video = tmp_path / "old.mp4"
    new_video = tmp_path / "new.mp4"
    old_video.write_bytes(b"old-video-bytes")
    new_video.write_bytes(b"new-video-bytes")
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    stale_frame = frames_dir / "frame_00001.jpg"
    stale_frame.write_bytes(b"stale-frame")
    understand._write_frames_manifest(tmp_path, old_video, 1.0, [stale_frame])

    calls = []

    def fake_extract(video_path, work_dir):
        calls.append(Path(video_path).name)
        stale_frame.write_bytes(b"fresh-frame")
        return [stale_frame]

    def fake_detect(video_path, work_dir, threshold=None):
        scenes = [{"start": 0.0, "end": 10.0}]
        (Path(work_dir) / "scenes.json").write_text(
            json.dumps(scenes), encoding="utf-8"
        )
        return scenes

    monkeypatch.setitem(understand.CONFIG, "fps", 1.0)
    monkeypatch.setitem(understand.CONFIG, "mimo_video_overview", False)
    monkeypatch.setitem(understand.CONFIG, "api_key", "tp-test")
    monkeypatch.setattr("understanding_runner.get_video_duration", lambda path: 10.0)
    monkeypatch.setattr(
        "understanding_runner.api_call",
        lambda payload: {"choices": [{"message": {"content": "ok"}}]},
    )
    monkeypatch.setattr("understanding_runner.extract_frames", fake_extract)
    monkeypatch.setattr("understanding_runner.detect_scenes", fake_detect)
    monkeypatch.setattr(
        "understanding_runner.detect_silence_periods", lambda *a, **k: []
    )
    monkeypatch.setattr(
        "understanding_runner.analyze_scenes",
        lambda scenes, frames, work_dir, **kwargs: [
            {"scene_id": 0, "start": 0.0, "end": 10.0, "description": "fresh"}
        ],
    )
    monkeypatch.setattr(
        "understanding_runner.build_agent_brief",
        lambda *a, **k: tmp_path / "agent_narration_brief.md",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "understanding_runner.py",
            str(new_video),
            "--work-dir",
            str(tmp_path),
            "--skip-asr",
        ],
    )

    understand.main()

    assert calls == ["new.mp4"]
    assert stale_frame.read_bytes() == b"fresh-frame"
    assert understand._frames_cache_valid(new_video, tmp_path, 1.0)


def test_understand_removes_stale_mimo_overview_before_failed_recompute(
    monkeypatch, tmp_path
):
    """A failed overview refresh must not leave a stale final overview for the brief."""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    stale_overview = tmp_path / "mimo_video_overview.json"
    stale_overview.write_text(
        json.dumps({"input": "scene_chunks", "content": "STALE"}), encoding="utf-8"
    )
    frame = tmp_path / "frames" / "frame_00001.jpg"
    frame.parent.mkdir()
    frame.write_bytes(b"frame")

    def fake_detect(video_path, work_dir, threshold=None):
        scenes = [{"scene_id": 0, "start": 0.0, "end": 10.0}]
        (Path(work_dir) / "scenes.json").write_text(
            json.dumps(scenes), encoding="utf-8"
        )
        return scenes

    def fail_overview(*args, **kwargs):
        raise RuntimeError("overview refresh failed")

    monkeypatch.setitem(understand.CONFIG, "fps", 1.0)
    monkeypatch.setitem(understand.CONFIG, "api_key", "tp-test")
    monkeypatch.setitem(understand.CONFIG, "mimo_video_overview", True)
    monkeypatch.setitem(understand.CONFIG, "mimo_video_api_key", "tp-test")
    monkeypatch.setattr("understanding_runner.get_video_duration", lambda path: 10.0)
    monkeypatch.setattr(
        "understanding_runner.api_call",
        lambda payload: {"choices": [{"message": {"content": "ok"}}]},
    )
    monkeypatch.setattr(
        "understanding_runner.extract_frames", lambda video_path, work_dir: [frame]
    )
    monkeypatch.setattr("understanding_runner.detect_scenes", fake_detect)
    monkeypatch.setattr(
        "understanding_runner.detect_silence_periods", lambda *a, **k: []
    )
    monkeypatch.setattr(
        "understanding_runner.analyze_scenes",
        lambda scenes, frames, work_dir, **kwargs: [
            {"scene_id": 0, "start": 0.0, "end": 10.0, "description": "fresh"}
        ],
    )
    monkeypatch.setattr("understanding_runner.analyze_video_overview", fail_overview)
    monkeypatch.setattr(
        "understanding_runner.build_agent_brief",
        lambda *a, **k: tmp_path / "agent_narration_brief.md",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "understanding_runner.py",
            str(video),
            "--work-dir",
            str(tmp_path),
            "--skip-asr",
        ],
    )

    understand.main()

    assert not stale_overview.exists()
    status = json.loads(
        (tmp_path / "mimo_video_overview.status.json").read_text(encoding="utf-8")
    )
    assert status["stage"] == "mimo_video_overview"
    assert status["enabled"] is True
    assert status["status"] == "failed"
    assert "overview refresh failed" in status["message"]
    assert status["artifact"] is None


def test_understand_writes_overview_status_when_key_missing(monkeypatch, tmp_path):
    """Enabled-but-no-key overview skip must be visible to downstream brief/review."""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    frame = tmp_path / "frames" / "frame_00001.jpg"
    frame.parent.mkdir()
    frame.write_bytes(b"frame")

    def fake_detect(video_path, work_dir, threshold=None):
        scenes = [{"scene_id": 0, "start": 0.0, "end": 10.0}]
        (Path(work_dir) / "scenes.json").write_text(
            json.dumps(scenes), encoding="utf-8"
        )
        return scenes

    monkeypatch.setitem(understand.CONFIG, "fps", 1.0)
    monkeypatch.setitem(understand.CONFIG, "api_key", "tp-test")
    monkeypatch.setitem(understand.CONFIG, "mimo_video_overview", True)
    monkeypatch.setitem(understand.CONFIG, "mimo_video_api_key", "")
    monkeypatch.setattr("understanding_runner.get_video_duration", lambda path: 10.0)
    monkeypatch.setattr(
        "understanding_runner.api_call",
        lambda payload: {"choices": [{"message": {"content": "ok"}}]},
    )
    monkeypatch.setattr(
        "understanding_runner.extract_frames", lambda video_path, work_dir: [frame]
    )
    monkeypatch.setattr("understanding_runner.detect_scenes", fake_detect)
    monkeypatch.setattr(
        "understanding_runner.detect_silence_periods", lambda *a, **k: []
    )
    monkeypatch.setattr(
        "understanding_runner.analyze_scenes",
        lambda scenes, frames, work_dir, **kwargs: [
            {"scene_id": 0, "start": 0.0, "end": 10.0, "description": "fresh"}
        ],
    )
    monkeypatch.setattr(
        "understanding_runner.build_agent_brief",
        lambda *a, **k: tmp_path / "agent_narration_brief.md",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "understanding_runner.py",
            str(video),
            "--work-dir",
            str(tmp_path),
            "--skip-asr",
            "--no-consolidate",
        ],
    )

    understand.main()

    status = json.loads(
        (tmp_path / "mimo_video_overview.status.json").read_text(encoding="utf-8")
    )
    assert status["status"] == "skipped_no_key"
    assert status["artifact"] is None


def test_understand_writes_failed_consolidation_status(monkeypatch, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    frame = tmp_path / "frames" / "frame_00001.jpg"
    frame.parent.mkdir()
    frame.write_bytes(b"frame")

    def fake_detect(video_path, work_dir, threshold=None):
        scenes = [{"scene_id": 0, "start": 0.0, "end": 10.0}]
        (Path(work_dir) / "scenes.json").write_text(
            json.dumps(scenes), encoding="utf-8"
        )
        return scenes

    def fake_consolidate(work_dir, do_asr=False, do_index=True):
        del work_dir, do_asr, do_index
        return {}

    monkeypatch.setitem(understand.CONFIG, "fps", 1.0)
    monkeypatch.setitem(understand.CONFIG, "api_key", "tp-test")
    monkeypatch.setitem(understand.CONFIG, "mimo_video_overview", False)
    monkeypatch.setattr("understanding_runner.get_video_duration", lambda path: 10.0)
    monkeypatch.setattr(
        "understanding_runner.api_call",
        lambda payload: {"choices": [{"message": {"content": "ok"}}]},
    )
    monkeypatch.setattr(
        "understanding_runner.extract_frames", lambda video_path, work_dir: [frame]
    )
    monkeypatch.setattr("understanding_runner.detect_scenes", fake_detect)
    monkeypatch.setattr(
        "understanding_runner.detect_silence_periods", lambda *a, **k: []
    )
    monkeypatch.setattr(
        "understanding_runner.analyze_scenes",
        lambda scenes, frames, work_dir, **kwargs: [
            {"scene_id": 0, "start": 0.0, "end": 10.0, "description": "fresh"}
        ],
    )
    monkeypatch.setattr(
        "understanding_runner.build_agent_brief",
        lambda *a, **k: tmp_path / "agent_narration_brief.md",
    )
    monkeypatch.setattr("consolidate.consolidate", fake_consolidate, raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "understanding_runner.py",
            str(video),
            "--work-dir",
            str(tmp_path),
            "--skip-asr",
        ],
    )

    understand.main()

    status = json.loads(
        (tmp_path / "consolidation.status.json").read_text(encoding="utf-8")
    )
    assert status["stage"] == "consolidation"
    assert status["enabled"] is True
    assert status["do_asr"] is False
    assert status["do_index"] is True
    assert status["status"] == "failed"
    assert "understanding_index.json" in status["message"]
    assert status["artifacts"] == []


def _run_understand_for_cache_tests(monkeypatch, tmp_path, video, *, argv_extra=None):
    frame = tmp_path / "frames" / "frame_00001.jpg"
    frame.parent.mkdir(exist_ok=True)
    frame.write_bytes(b"frame")

    monkeypatch.setitem(understand.CONFIG, "fps", 1.0)
    monkeypatch.setitem(understand.CONFIG, "api_key", "tp-test")
    monkeypatch.setitem(understand.CONFIG, "mimo_video_api_key", "")
    monkeypatch.setitem(understand.CONFIG, "mimo_video_overview", False)
    monkeypatch.setattr("understanding_runner.get_video_duration", lambda path: 10.0)
    monkeypatch.setattr(
        "understanding_runner.api_call",
        lambda payload: {"choices": [{"message": {"content": "ok"}}]},
    )
    monkeypatch.setattr(
        "understanding_runner.extract_frames", lambda video_path, work_dir: [frame]
    )
    monkeypatch.setattr(
        "understanding_runner.build_agent_brief",
        lambda *a, **k: tmp_path / "agent_narration_brief.md",
    )
    # These tests exercise VLM cache-key behavior, not the consolidate index (now default-on,
    # whose api_call is the real lib.api_call, not the mocked understand.api_call). Skip it.
    argv = [
        "understanding_runner.py",
        str(video),
        "--work-dir",
        str(tmp_path),
        "--no-consolidate",
        *(argv_extra or []),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    understand.main()


def test_stage_cache_rejects_artifact_mutation_with_stale_sidecar(tmp_path):
    artifact = tmp_path / "scenes.json"
    artifact.write_text(json.dumps([{"start": 0.0, "end": 10.0}]), encoding="utf-8")
    meta = {
        "schema_version": 1,
        "stage": "scenes",
        "source_video_fingerprint": "source",
        "settings": {},
    }
    understand._write_stage_meta(artifact, meta)

    assert understand._stage_cache_valid(artifact, meta)

    artifact.write_text(json.dumps([{"start": 0.0, "end": 5.0}]), encoding="utf-8")

    assert not understand._stage_cache_valid(artifact, meta)


def test_understand_recomputes_stage_when_artifact_bytes_change(monkeypatch, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    calls = []

    def fake_detect(video_path, work_dir, threshold=None):
        calls.append(len(calls) + 1)
        scenes = [{"start": 0.0, "end": 10.0, "run": calls[-1]}]
        (Path(work_dir) / "scenes.json").write_text(
            json.dumps(scenes), encoding="utf-8"
        )
        return scenes

    monkeypatch.setattr("understanding_runner.detect_scenes", fake_detect)
    monkeypatch.setattr("understanding_runner.transcribe_audio", lambda *a, **k: [])
    monkeypatch.setattr(
        "understanding_runner.detect_silence_periods", lambda *a, **k: []
    )
    monkeypatch.setattr("understanding_runner.analyze_scenes", lambda *a, **k: [])

    _run_understand_for_cache_tests(monkeypatch, tmp_path, video)
    (tmp_path / "scenes.json").write_text(
        json.dumps([{"start": 0.0, "end": 10.0, "run": "externally-mutated"}]),
        encoding="utf-8",
    )
    _run_understand_for_cache_tests(monkeypatch, tmp_path, video)

    assert calls == [1, 2]
    assert (
        json.loads((tmp_path / "scenes.json").read_text(encoding="utf-8"))[0]["run"]
        == 2
    )


def test_understand_recomputes_scenes_when_scene_settings_change(monkeypatch, tmp_path):
    """Scene cache freshness must include threshold/junk settings, not just video mtime."""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    calls = []

    def fake_detect(video_path, work_dir, threshold=None):
        calls.append(threshold)
        scenes = [{"start": 0.0, "end": 10.0, "threshold": threshold}]
        (Path(work_dir) / "scenes.json").write_text(
            json.dumps(scenes), encoding="utf-8"
        )
        return scenes

    monkeypatch.setattr("understanding_runner.detect_scenes", fake_detect)
    monkeypatch.setattr("understanding_runner.transcribe_audio", lambda *a, **k: [])
    monkeypatch.setattr(
        "understanding_runner.detect_silence_periods", lambda *a, **k: []
    )
    monkeypatch.setattr("understanding_runner.analyze_scenes", lambda *a, **k: [])

    _run_understand_for_cache_tests(
        monkeypatch, tmp_path, video, argv_extra=["--scene-threshold", "0.1"]
    )
    _run_understand_for_cache_tests(
        monkeypatch, tmp_path, video, argv_extra=["--scene-threshold", "0.4"]
    )

    assert calls == [0.1, 0.4]
    assert (
        json.loads((tmp_path / "scenes.json").read_text(encoding="utf-8"))[0][
            "threshold"
        ]
        == 0.4
    )


def test_understand_recomputes_asr_when_asr_settings_change(monkeypatch, tmp_path):
    """ASR cache freshness must include ASR settings and source provenance."""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    calls = []

    def fake_asr(video_path, work_dir):
        calls.append(understand.CONFIG["asr_segment_seconds"])
        result = [{"start": 0.0, "end": 1.0, "text": f"seg-{calls[-1]}"}]
        (Path(work_dir) / "asr_result.json").write_text(
            json.dumps(result), encoding="utf-8"
        )
        return result

    monkeypatch.setattr(
        "understanding_runner.detect_scenes",
        lambda video_path, work_dir, threshold=None: (
            (Path(work_dir) / "scenes.json").write_text(
                json.dumps([{"start": 0.0, "end": 10.0}]), encoding="utf-8"
            )
            and [{"start": 0.0, "end": 10.0}]
        ),
    )
    monkeypatch.setattr("understanding_runner.transcribe_audio", fake_asr)
    monkeypatch.setattr(
        "understanding_runner.detect_silence_periods", lambda *a, **k: []
    )
    monkeypatch.setattr("understanding_runner.analyze_scenes", lambda *a, **k: [])

    monkeypatch.setitem(understand.CONFIG, "asr_segment_seconds", 30.0)
    _run_understand_for_cache_tests(monkeypatch, tmp_path, video)
    monkeypatch.setitem(understand.CONFIG, "asr_segment_seconds", 12.0)
    _run_understand_for_cache_tests(monkeypatch, tmp_path, video)

    assert calls == [30.0, 12.0]
    assert (
        json.loads((tmp_path / "asr_result.json").read_text(encoding="utf-8"))[0][
            "text"
        ]
        == "seg-12.0"
    )


def test_understand_recomputes_silence_when_asr_content_changes(monkeypatch, tmp_path):
    """Silence cache freshness must include ASR artifact bytes/provenance."""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    calls = []
    asr_payload = {"value": [{"start": 0.0, "end": 1.0, "text": "first"}]}

    def fake_asr(video_path, work_dir):
        (Path(work_dir) / "asr_result.json").write_text(
            json.dumps(asr_payload["value"]), encoding="utf-8"
        )
        return asr_payload["value"]

    def fake_silence(video_path, work_dir, asr_result):
        calls.append([seg["text"] for seg in asr_result])
        result = [
            {"start": 0.0, "end": 1.0, "duration": 1.0, "has_speech": bool(calls[-1])}
        ]
        (Path(work_dir) / "silence_periods.json").write_text(
            json.dumps(result), encoding="utf-8"
        )
        return result

    monkeypatch.setattr(
        "understanding_runner.detect_scenes",
        lambda video_path, work_dir, threshold=None: (
            (Path(work_dir) / "scenes.json").write_text(
                json.dumps([{"start": 0.0, "end": 10.0}]), encoding="utf-8"
            )
            and [{"start": 0.0, "end": 10.0}]
        ),
    )
    monkeypatch.setattr("understanding_runner.transcribe_audio", fake_asr)
    monkeypatch.setattr("understanding_runner.detect_silence_periods", fake_silence)
    monkeypatch.setattr("understanding_runner.analyze_scenes", lambda *a, **k: [])

    _run_understand_for_cache_tests(monkeypatch, tmp_path, video)
    asr_payload["value"] = [{"start": 0.0, "end": 1.0, "text": "second"}]
    (tmp_path / "asr_result.json").unlink()
    (tmp_path / "asr_result.json.meta.json").unlink()
    _run_understand_for_cache_tests(monkeypatch, tmp_path, video)

    assert calls == [["first"], ["second"]]


def test_understand_recomputes_vlm_when_context_changes(monkeypatch, tmp_path):
    """VLM cache freshness must include prompt/context/model/frame/scene provenance."""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    calls = []

    def fake_vlm(scenes, frames, work_dir, **kwargs):
        calls.append(understand.CONFIG.get("context_info", ""))
        result = [{"scene_id": 0, "start": 0.0, "end": 10.0, "description": calls[-1]}]
        (Path(work_dir) / "vlm_analysis.json").write_text(
            json.dumps(result), encoding="utf-8"
        )
        return result

    monkeypatch.setattr(
        "understanding_runner.detect_scenes",
        lambda video_path, work_dir, threshold=None: (
            (Path(work_dir) / "scenes.json").write_text(
                json.dumps([{"start": 0.0, "end": 10.0}]), encoding="utf-8"
            )
            and [{"start": 0.0, "end": 10.0}]
        ),
    )
    monkeypatch.setattr("understanding_runner.transcribe_audio", lambda *a, **k: [])
    monkeypatch.setattr(
        "understanding_runner.detect_silence_periods", lambda *a, **k: []
    )
    monkeypatch.setattr("understanding_runner.analyze_scenes", fake_vlm)

    _run_understand_for_cache_tests(
        monkeypatch, tmp_path, video, argv_extra=["--context", "角色A"]
    )
    monkeypatch.setitem(understand.CONFIG, "context_info", "")
    _run_understand_for_cache_tests(
        monkeypatch, tmp_path, video, argv_extra=["--context", "角色B"]
    )

    assert calls == ["角色A", "角色B"]
    assert (
        json.loads((tmp_path / "vlm_analysis.json").read_text(encoding="utf-8"))[0][
            "description"
        ]
        == "角色B"
    )


def test_understand_recomputes_vlm_when_api_endpoint_changes(monkeypatch, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    calls = []

    def fake_vlm(scenes, frames, work_dir, **kwargs):
        calls.append(understand.CONFIG.get("api_url", ""))
        result = [{"scene_id": 0, "start": 0.0, "end": 10.0, "description": calls[-1]}]
        (Path(work_dir) / "vlm_analysis.json").write_text(
            json.dumps(result), encoding="utf-8"
        )
        return result

    monkeypatch.setattr(
        "understanding_runner.detect_scenes",
        lambda video_path, work_dir, threshold=None: (
            (Path(work_dir) / "scenes.json").write_text(
                json.dumps([{"start": 0.0, "end": 10.0}]), encoding="utf-8"
            )
            and [{"start": 0.0, "end": 10.0}]
        ),
    )
    monkeypatch.setattr("understanding_runner.transcribe_audio", lambda *a, **k: [])
    monkeypatch.setattr(
        "understanding_runner.detect_silence_periods", lambda *a, **k: []
    )
    monkeypatch.setattr("understanding_runner.analyze_scenes", fake_vlm)

    monkeypatch.setitem(
        understand.CONFIG, "api_url", "https://one.example/v1/chat/completions"
    )
    _run_understand_for_cache_tests(monkeypatch, tmp_path, video)
    monkeypatch.setitem(
        understand.CONFIG, "api_url", "https://two.example/v1/chat/completions"
    )
    _run_understand_for_cache_tests(monkeypatch, tmp_path, video)

    assert calls == [
        "https://one.example/v1/chat/completions",
        "https://two.example/v1/chat/completions",
    ]


def test_understand_recomputes_vlm_when_thinking_behavior_changes(
    monkeypatch, tmp_path
):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    calls = []

    def fake_vlm(scenes, frames, work_dir, **kwargs):
        calls.append(understand.CONFIG.get("mimo_disable_thinking"))
        result = [
            {"scene_id": 0, "start": 0.0, "end": 10.0, "description": str(calls[-1])}
        ]
        (Path(work_dir) / "vlm_analysis.json").write_text(
            json.dumps(result), encoding="utf-8"
        )
        return result

    monkeypatch.setattr(
        "understanding_runner.detect_scenes",
        lambda video_path, work_dir, threshold=None: (
            (Path(work_dir) / "scenes.json").write_text(
                json.dumps([{"start": 0.0, "end": 10.0}]), encoding="utf-8"
            )
            and [{"start": 0.0, "end": 10.0}]
        ),
    )
    monkeypatch.setattr("understanding_runner.transcribe_audio", lambda *a, **k: [])
    monkeypatch.setattr(
        "understanding_runner.detect_silence_periods", lambda *a, **k: []
    )
    monkeypatch.setattr("understanding_runner.analyze_scenes", fake_vlm)

    monkeypatch.setitem(understand.CONFIG, "mimo_disable_thinking", True)
    _run_understand_for_cache_tests(monkeypatch, tmp_path, video)
    monkeypatch.setitem(understand.CONFIG, "mimo_disable_thinking", False)
    _run_understand_for_cache_tests(monkeypatch, tmp_path, video)

    assert calls == [True, False]


def test_understand_asr_exception_does_not_cache_empty_transcript(
    monkeypatch, tmp_path
):
    """Unexpected ASR failures must fail fast and leave no reusable empty asr_result.json."""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")

    monkeypatch.setattr(
        "understanding_runner.detect_scenes",
        lambda video_path, work_dir, threshold=None: (
            (Path(work_dir) / "scenes.json").write_text(
                json.dumps([{"start": 0.0, "end": 10.0}]), encoding="utf-8"
            )
            and [{"start": 0.0, "end": 10.0}]
        ),
    )
    monkeypatch.setattr(
        "understanding_runner.transcribe_audio",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("quota")),
    )
    monkeypatch.setattr(
        "understanding_runner.detect_silence_periods", lambda *a, **k: []
    )
    monkeypatch.setattr("understanding_runner.analyze_scenes", lambda *a, **k: [])

    with pytest.raises(RuntimeError, match="ASR 失败"):
        _run_understand_for_cache_tests(monkeypatch, tmp_path, video)

    assert not (tmp_path / "asr_result.json").exists()
    assert not (tmp_path / "asr_result.json.meta.json").exists()


def test_understand_omits_stale_mimo_overview_when_overview_disabled(
    monkeypatch, tmp_path
):
    """Direct understand runs must not let an old overview leak into a new brief when disabled."""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    stale_overview = tmp_path / "mimo_video_overview.json"
    stale_overview.write_text(
        json.dumps({"input": "scene_chunks", "content": "STALE OVERVIEW"}),
        encoding="utf-8",
    )
    frame = tmp_path / "frames" / "frame_00001.jpg"
    frame.parent.mkdir()
    frame.write_bytes(b"frame")

    def fake_detect(video_path, work_dir, threshold=None):
        scenes = [{"scene_id": 0, "start": 0.0, "end": 10.0}]
        (Path(work_dir) / "scenes.json").write_text(
            json.dumps(scenes), encoding="utf-8"
        )
        return scenes

    monkeypatch.setitem(understand.CONFIG, "fps", 1.0)
    monkeypatch.setitem(understand.CONFIG, "api_key", "tp-test")
    monkeypatch.setitem(understand.CONFIG, "mimo_video_overview", False)
    monkeypatch.setattr("understanding_runner.get_video_duration", lambda path: 10.0)
    monkeypatch.setattr(
        "understanding_runner.api_call",
        lambda payload: {"choices": [{"message": {"content": "ok"}}]},
    )
    monkeypatch.setattr(
        "understanding_runner.extract_frames", lambda video_path, work_dir: [frame]
    )
    monkeypatch.setattr("understanding_runner.detect_scenes", fake_detect)
    monkeypatch.setattr(
        "understanding_runner.detect_silence_periods", lambda *a, **k: []
    )
    monkeypatch.setattr(
        "understanding_runner.analyze_scenes",
        lambda scenes, frames, work_dir, **kwargs: [
            {"scene_id": 0, "start": 0.0, "end": 10.0, "description": "fresh"}
        ],
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "understanding_runner.py",
            str(video),
            "--work-dir",
            str(tmp_path),
            "--skip-asr",
        ],
    )

    understand.main()

    assert not stale_overview.exists()
    assert "STALE OVERVIEW" not in (tmp_path / "agent_narration_brief.md").read_text(
        encoding="utf-8"
    )
