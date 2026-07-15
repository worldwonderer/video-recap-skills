import json
import sys
import wave
from pathlib import Path

import pytest

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[2] / "skills" / "video-voiceover" / "scripts"),
)
import dub  # noqa: E402


def test_strip_reasoning_residue_removes_think_leakage():
    """Regression: MiMo -asr models are not thinking-disabled, so <think> reasoning can leak
    into the transcript in several shapes. All must be stripped; clean text is untouched."""
    s = dub._strip_reasoning_residue
    assert (
        s("think>\n 真相比逃跑更狼。").strip() == "真相比逃跑更狼。"
    )  # leading orphan residue
    assert s("think>\n Yeah.").strip() == "Yeah."
    assert (
        s("<think>推理\n</think>\n开门的却是个陌生男人").strip()
        == "开门的却是个陌生男人"
    )  # full block
    assert s("<think>未闭合的推理 后续文字").strip() == ""  # unclosed/truncated
    assert (
        s("正常一句，没有思考标签。") == "正常一句，没有思考标签。"
    )  # clean text untouched


def test_dub_asr_skips_zero_sample_wav_before_api_call(monkeypatch, tmp_path):
    empty = tmp_path / "empty.wav"
    with wave.open(str(empty), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(b"")

    monkeypatch.setattr(
        dub,
        "mimo_asr_api_call",
        lambda _payload: pytest.fail("zero-sample WAV must not reach MiMo ASR"),
    )

    assert dub._run_asr(empty) == ""


def test_dub_asr_windows_ignore_subsecond_container_tail(monkeypatch, tmp_path):
    source = tmp_path / "source.wav"
    source.write_bytes(b"source")
    cuts = []
    calls = []

    def cut(_source, output, start, duration):
        cuts.append((start, duration))
        Path(output).write_bytes(b"wav")

    monkeypatch.setattr(dub, "_cut_wav", cut)
    monkeypatch.setattr(
        dub,
        "_run_asr",
        lambda path, lang="en": calls.append(Path(path).name) or "speech",
    )

    windows = dub._asr_windows(source, tmp_path, duration=16.118, window=8.0)

    assert [item["start"] for item in windows] == [0.0, 8.0]
    assert calls == ["asr_000.wav", "asr_001.wav"]
    assert len(cuts) == 2


def test_atempo_chain():
    assert dub._atempo_chain(1.3) == "atempo=1.3000"
    assert dub._atempo_chain(3.0).startswith("atempo=2.0,atempo=")  # >2x is chained
    assert dub._atempo_chain(0.1) == "atempo=0.5000"  # floored at 0.5


def test_ref_window_clamps_short_video():
    start, dur = dub._ref_window(3.0, 2.0, 10.0)
    assert 0.0 <= start <= 1.0
    assert dur >= 2.0
    assert start + dur <= 3.01


def test_ref_window_normal_video():
    start, dur = dub._ref_window(60.0, 2.0, 10.0)
    assert (start, dur) == (2.0, 10.0)


def test_build_dub_track_anchors_line_at_its_start(tmp_path):
    """Each line is placed at its own source start; everything before it is silence (so the dub
    tracks the picture and never drifts/repeats)."""
    line_wav = tmp_path / "line.wav"
    with wave.open(str(line_wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(dub.CLONE_SR)
        w.writeframes(b"\x10\x10" * int(0.5 * dub.CLONE_SR))  # 0.5s of non-silence
    out = tmp_path / "track.wav"
    dub._build_dub_track([{"start": 1.0, "fitted_wav": str(line_wav)}], 3.0, out)
    with wave.open(str(out), "rb") as w:
        assert w.getframerate() == dub.CLONE_SR
        frames = w.readframes(w.getnframes())
    off = int(1.0 * dub.CLONE_SR) * 2
    assert frames[:off] == b"\x00" * off  # silence before the line's start
    assert (
        frames[off : off + 4] == b"\x10\x10\x10\x10"
    )  # the line lands exactly at 1.0s


def test_build_dub_track_skips_missing_and_mismatched(tmp_path):
    """A line with no fitted wav, or a wrong-rate wav, is skipped (never crashes the render)."""
    bad = tmp_path / "bad.wav"
    with wave.open(str(bad), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)  # not CLONE_SR → must be skipped
        w.writeframes(b"\x20\x20" * 1000)
    out = tmp_path / "track.wav"
    dub._build_dub_track(
        [{"start": 0.0}, {"start": 0.5, "fitted_wav": str(bad)}], 2.0, out
    )
    with wave.open(str(out), "rb") as w:
        frames = w.readframes(w.getnframes())
    assert frames == b"\x00" * len(frames)  # nothing placed → full silence


def test_brief_lists_windows_for_the_agent():
    md = dub._brief_md([{"start": 0.0, "end": 6.0, "text": "Hello there."}], 6.0)
    assert "dub_script.json" in md
    assert '[{"start"' in md
    assert "Hello there." in md


def test_dub_lint_fails_empty_overlap_and_bounds():
    script = [
        {"start": 0.0, "end": 2.0, "zh": "第一句"},
        {"start": 1.5, "end": 6.0, "zh": ""},
    ]
    lint = dub.lint_dub_script(script, duration=5.0)

    assert lint["verdict"] == "FAIL"
    codes = {issue["code"] for issue in lint["issues"]}
    assert {"overlap", "empty_translation", "time_out_of_range"} <= codes


def test_dub_lint_rejects_non_list_script(tmp_path):
    """A malformed (non-list) dub_script.json is a clean FAIL, not a traceback."""
    report = dub.lint_dub_script(
        {"start": 0, "zh": "x"}, duration=5.0, work_dir=tmp_path
    )

    assert report["verdict"] == "FAIL"
    assert report["blocking"] is True
    assert report["errors"][0]["code"] == "script_not_a_list"
    assert (tmp_path / "dub_lint.json").exists()
    # build_dub_review must also tolerate the non-list input without raising
    review = dub.build_dub_review(
        {"start": 0, "zh": "x"}, {"duration": 5.0, "windows": []}
    )
    assert review["verdict"] == "FAIL"


def test_dub_lint_tolerates_subframe_rounding():
    """~1-frame rounding (overlap / end past duration) must not hard-block an otherwise-fine script."""
    script = [
        {"start": 0.0, "end": 2.03, "zh": "第一句"},  # 30ms overlap with the next start
        {"start": 2.0, "end": 5.02, "zh": "第二句"},  # ends 20ms past duration
    ]
    lint = dub.lint_dub_script(script, duration=5.0)

    assert lint["verdict"] == "PASS"
    codes = {i["code"] for i in lint["issues"]}
    assert "overlap" not in codes and "time_out_of_range" not in codes


def test_dub_lint_warns_fast_speech_without_blocking():
    script = [{"start": 0.0, "end": 1.0, "zh": "这是一句非常非常非常长的中文配音台词"}]
    lint = dub.lint_dub_script(script, duration=3.0)

    assert lint["verdict"] == "PASS"
    assert any(issue["code"] == "fast_speech" for issue in lint["issues"])
    assert lint["summary"]["max_chars_per_second"] > 8.0


def test_dub_review_maps_lint_to_revise_edits():
    script = [{"start": 0.0, "end": 1.0, "zh": "这是一句非常非常非常长的中文配音台词"}]
    transcript = {
        "duration": 3.0,
        "windows": [{"start": 0.0, "end": 3.0, "text": "hello"}],
    }
    review = dub.build_dub_review(script, transcript)

    assert review["verdict"] == "REVISE"
    assert review["checks"]["faithful_to_source"] == "needs_agent_review"
    assert review["highest_return_edits"]


def test_dub_lint_reports_blocking_script_errors(tmp_path):
    """dub_lint.json deterministically blocks empty, overlapping, and out-of-range lines."""
    script = [
        {"start": 0.0, "end": 1.0, "zh": "第一句。"},
        {"start": 0.9, "end": 2.0, "zh": ""},
        {"start": 6.5, "end": 7.2, "zh": "越界句。"},
    ]

    report = dub.lint_dub_script(script, duration=6.0, work_dir=tmp_path)

    assert report["blocking"] is True
    assert [issue["code"] for issue in report["errors"]] == [
        "empty_translation",
        "overlap",
        "time_out_of_range",
    ]
    persisted = json.loads((tmp_path / "dub_lint.json").read_text(encoding="utf-8"))
    assert persisted == report


def test_dub_stage_lint_and_review_write_artifacts(tmp_path, capsys):
    (tmp_path / "dub_transcript.json").write_text(
        json.dumps(
            {"duration": 3.0, "windows": [{"start": 0.0, "end": 3.0, "text": "hello"}]}
        ),
        encoding="utf-8",
    )
    (tmp_path / "dub_script.json").write_text(
        json.dumps([{"start": 0.0, "end": 1.0, "zh": "你好"}]),
        encoding="utf-8",
    )

    dub.stage_lint(tmp_path)
    dub.stage_review(tmp_path)

    assert (
        json.loads((tmp_path / "dub_lint.json").read_text(encoding="utf-8"))["verdict"]
        == "PASS"
    )
    assert (
        json.loads((tmp_path / "dub_review.json").read_text(encoding="utf-8"))[
            "verdict"
        ]
        == "PASS"
    )
    assert "dub_reviewed" in capsys.readouterr().out


def test_dub_render_stops_before_tts_when_lint_blocks(monkeypatch, tmp_path):
    """Mechanical dub lint must run before clone-TTS spend."""
    (tmp_path / "dub_transcript.json").write_text(
        json.dumps(
            {"duration": 4.0, "windows": [{"start": 0, "end": 4, "text": "hello"}]}
        ),
        encoding="utf-8",
    )
    (tmp_path / "dub_script.json").write_text(
        json.dumps([{"start": 0.0, "end": 1.0, "zh": ""}]),
        encoding="utf-8",
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("clone TTS must not run when dub_lint blocks")

    monkeypatch.setattr(dub, "_clone_tts", fail_if_called)

    with pytest.raises(SystemExit, match="dub_lint.json"):
        dub.stage_render(tmp_path / "video.mp4", tmp_path, ref_start=0.0, ref_dur=2.0)


def _write_test_wav(path, *, seconds=0.1):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(dub.CLONE_SR)
        out.writeframes(b"\x10\x10" * max(1, int(seconds * dub.CLONE_SR)))


def _prepare_render_cache_fixture(tmp_path, text="你好"):
    (tmp_path / "dub_transcript.json").write_text(
        json.dumps(
            {"duration": 2.0, "windows": [{"start": 0, "end": 2, "text": "hello"}]}
        ),
        encoding="utf-8",
    )
    (tmp_path / "dub_script.json").write_text(
        json.dumps([{"start": 0.0, "end": 1.0, "zh": text}]),
        encoding="utf-8",
    )
    ref = tmp_path / "dub_reference.wav"
    _write_test_wav(ref)
    tts_dir = tmp_path / "dub_tts"
    tts_dir.mkdir()
    raw = tts_dir / "line_000_raw.wav"
    _write_test_wav(raw)
    return ref, raw


def _mock_time_fit(monkeypatch):
    def fake_time_fit(_raw, fitted, _room):
        _write_test_wav(fitted)
        return 0.1

    monkeypatch.setattr(dub, "_time_fit", fake_time_fit)


def test_dub_render_reuses_matching_voiceclone_cache(monkeypatch, tmp_path):
    ref, raw = _prepare_render_cache_fixture(tmp_path)
    dub._write_clone_cache_meta(raw, "你好", ref.read_bytes())
    _mock_time_fit(monkeypatch)

    monkeypatch.setattr(
        dub,
        "_clone_tts",
        lambda *_args, **_kwargs: pytest.fail(
            "matching voiceclone cache must skip the API"
        ),
    )
    monkeypatch.setattr(
        dub, "_build_dub_track", lambda _lines, _duration, out: _write_test_wav(out)
    )
    monkeypatch.setattr(
        dub, "_mux", lambda _video, _wav, out: Path(out).write_bytes(b"mp4")
    )

    dub.stage_render(tmp_path / "video.mp4", tmp_path, ref_start=0.0, ref_dur=2.0)

    manifest = json.loads((tmp_path / "dub_manifest.json").read_text(encoding="utf-8"))
    assert manifest["lines"][0]["tts_cache"] == "hit"


def test_dub_render_invalidates_voiceclone_cache_when_text_changes(
    monkeypatch, tmp_path
):
    ref, raw = _prepare_render_cache_fixture(tmp_path, text="新台词")
    dub._write_clone_cache_meta(raw, "旧台词", ref.read_bytes())
    _mock_time_fit(monkeypatch)
    calls = []

    def clone(text, _ref_b64, out):
        calls.append(text)
        _write_test_wav(out)

    monkeypatch.setattr(dub, "_clone_tts", clone)
    monkeypatch.setattr(
        dub, "_build_dub_track", lambda _lines, _duration, out: _write_test_wav(out)
    )
    monkeypatch.setattr(
        dub, "_mux", lambda _video, _wav, out: Path(out).write_bytes(b"mp4")
    )

    dub.stage_render(tmp_path / "video.mp4", tmp_path, ref_start=0.0, ref_dur=2.0)

    assert calls == ["新台词"]
    manifest = json.loads((tmp_path / "dub_manifest.json").read_text(encoding="utf-8"))
    assert manifest["lines"][0]["tts_cache"] == "miss"


def test_dub_mux_pins_delivery_sample_rate_after_loudnorm(monkeypatch, tmp_path):
    commands = []
    monkeypatch.setattr(dub, "run_cmd", lambda cmd, **kwargs: commands.append(cmd))

    dub._mux(tmp_path / "source.mp4", tmp_path / "dub.wav", tmp_path / "dubbed.mp4")

    command = commands[0]
    assert command[command.index("-ar") + 1] == "48000"
    assert command.index("-ar") > command.index("-af")


def test_dub_print_schema_includes_new_artifacts(capsys):
    dub.print_schemas()
    schemas = json.loads(capsys.readouterr().out)

    assert "dub_lint.json" in schemas
    assert "dub_review.json" in schemas
    assert "dub_manifest.json" in schemas


def test_p0_dub_chars_per_second_ignores_punctuation_for_density():
    assert dub._chars_per_second("你，好！……", 1.0) == pytest.approx(2.0)
    assert dub._chars_per_second("Hello, world!", 2.0) == pytest.approx(5.0)


def test_p0_dub_lint_warns_near_trim_risk_before_hard_cut():
    script = [
        {"start": 0.0, "end": 2.0, "zh": "这是一句中文台词需要稍微压缩"}
    ]  # 14 effective chars / 2s = 7 cps
    lint = dub.lint_dub_script(script, duration=3.0)

    assert lint["verdict"] == "PASS"
    codes = {issue["code"] for issue in lint["issues"]}
    assert "fast_speech" in codes
    assert "trim_risk" in codes
    assert lint["summary"]["trim_risk_lines"] == [0]
    assert lint["summary"]["max_chars_per_second"] == pytest.approx(7.0)
