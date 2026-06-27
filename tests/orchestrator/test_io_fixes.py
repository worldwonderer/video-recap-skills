import sys
from argparse import Namespace
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'video-recap' / 'scripts'))
import json
import pytest  # noqa: F401
import doctor
import recap


def _manifest_args(**overrides):
    defaults = {
        "context": "",
        "scene_threshold": None,
        "style": "纪录片",
        "edit_mode": "full",
        "target_duration": None,
        "skip_asr": False,
        "mimo_video_overview": False,
        "consolidate": False,
        "consolidate_asr": False,
        "review_narration": None,
    }
    defaults.update(overrides)
    return Namespace(**defaults)


def _tools_present(monkeypatch):
    monkeypatch.setattr("doctor._ffmpeg_filters", lambda: {"subtitles", "ass"})
    monkeypatch.setattr(
        "doctor._command_path",
        lambda name: f"/usr/bin/{name}" if name in ("ffmpeg", "ffprobe") else None,
    )


def _capability_names(report, group):
    return {item["name"] for item in report["capability_menu"][group]}


def test_doctor_ok_when_tools_and_mimo_key_present(monkeypatch):
    _tools_present(monkeypatch)
    for k in ("api_key", "mimo_asr_api_key", "mimo_tts_api_key", "mimo_video_api_key"):
        monkeypatch.setitem(doctor.CONFIG, k, "tp-test-key")

    report = doctor.build_report()

    assert report["ok"] is True
    assert report["failures"] == []
    assert {"ok", "repo_root", "checks", "failures", "warnings"} <= set(report)
    assert set(report["capability_menu"]) == {"ready", "blocked", "warnings/degraded", "optional_upgrades"}
    assert "default_recap_pipeline" in _capability_names(report, "ready")
    assert "mimo_asr" in _capability_names(report, "ready")
    assert "subtitle_burn" in _capability_names(report, "ready")
    assert report["capability_menu"]["blocked"] == []


def test_doctor_fails_without_mimo_key(monkeypatch):
    _tools_present(monkeypatch)
    monkeypatch.setitem(doctor.CONFIG, "api_key", "")
    monkeypatch.setitem(doctor.CONFIG, "mimo_video_api_key", "")
    monkeypatch.setitem(doctor.CONFIG, "mimo_tts_api_key", "")
    monkeypatch.setitem(doctor.CONFIG, "mimo_asr_api_key", "")

    report = doctor.build_report()

    assert report["ok"] is False
    assert any("MIMO_API_KEY" in f for f in report["failures"])
    assert "mimo_credentials" in _capability_names(report, "blocked")
    assert "default_recap_pipeline" in _capability_names(report, "blocked")


def test_doctor_missing_ffmpeg_is_failure(monkeypatch):
    monkeypatch.setattr("doctor._ffmpeg_filters", lambda: set())
    monkeypatch.setattr("doctor._command_path", lambda name: None)
    monkeypatch.setitem(doctor.CONFIG, "api_key", "tp-x")

    report = doctor.build_report()

    assert report["ok"] is False
    assert any("ffmpeg" in f for f in report["failures"])
    assert {"ffmpeg", "ffprobe"} <= _capability_names(report, "blocked")


def test_doctor_warns_when_asr_unconfigured_but_key_present(monkeypatch):
    """api_key powers VLM/TTS; an empty ASR key is only a warning (use --skip-asr)."""
    _tools_present(monkeypatch)
    monkeypatch.setitem(doctor.CONFIG, "api_key", "tp-x")
    monkeypatch.setitem(doctor.CONFIG, "mimo_video_api_key", "tp-x")
    monkeypatch.setitem(doctor.CONFIG, "mimo_tts_api_key", "tp-x")
    monkeypatch.setitem(doctor.CONFIG, "mimo_asr_api_key", "")

    report = doctor.build_report()

    assert report["ok"] is True
    assert any("ASR not configured" in w for w in report["warnings"])
    assert "mimo_asr" in _capability_names(report, "warnings/degraded")
    assert "recap_degraded_mode" in _capability_names(report, "warnings/degraded")
    assert "default_recap_pipeline" not in _capability_names(report, "ready")


def test_doctor_warns_when_subtitle_burn_degraded(monkeypatch):
    monkeypatch.setattr("doctor._ffmpeg_filters", lambda: {"ass"})
    monkeypatch.setattr(
        "doctor._command_path",
        lambda name: f"/usr/bin/{name}" if name in ("ffmpeg", "ffprobe") else None,
    )
    for k in ("api_key", "mimo_asr_api_key", "mimo_tts_api_key", "mimo_video_api_key"):
        monkeypatch.setitem(doctor.CONFIG, k, "tp-test-key")

    report = doctor.build_report()

    assert report["ok"] is True
    assert "subtitle_burn" in _capability_names(report, "warnings/degraded")
    assert "recap_degraded_mode" in _capability_names(report, "warnings/degraded")
    assert "default_recap_pipeline" not in _capability_names(report, "ready")
    assert _capability_names(report, "optional_upgrades") == {"jianying_export"}


def test_doctor_blocks_default_pipeline_when_vlm_or_tts_override_missing(monkeypatch):
    _tools_present(monkeypatch)
    monkeypatch.setitem(doctor.CONFIG, "api_key", "tp-x")
    monkeypatch.setitem(doctor.CONFIG, "mimo_asr_api_key", "tp-x")
    monkeypatch.setitem(doctor.CONFIG, "mimo_video_api_key", "")
    monkeypatch.setitem(doctor.CONFIG, "mimo_tts_api_key", "")

    report = doctor.build_report()

    assert report["ok"] is True
    assert {"mimo_vlm", "mimo_tts", "default_recap_pipeline"} <= _capability_names(report, "blocked")
    assert "default_recap_pipeline" not in _capability_names(report, "ready")


def test_doctor_optional_upgrades_include_burn_only_when_available(monkeypatch):
    _tools_present(monkeypatch)
    for k in ("api_key", "mimo_asr_api_key", "mimo_tts_api_key", "mimo_video_api_key"):
        monkeypatch.setitem(doctor.CONFIG, k, "tp-test-key")

    report = doctor.build_report()

    assert _capability_names(report, "optional_upgrades") == {"jianying_export", "burned_subtitles"}


def test_doctor_human_output_prints_capability_menu(monkeypatch, capsys):
    _tools_present(monkeypatch)
    for k in ("api_key", "mimo_asr_api_key", "mimo_tts_api_key", "mimo_video_api_key"):
        monkeypatch.setitem(doctor.CONFIG, k, "tp-test-key")

    doctor._print_human(doctor.build_report())
    out = capsys.readouterr().out

    assert "[capability menu]" in out
    assert "ready:" in out
    assert "blocked:" in out
    assert "warnings/degraded:" in out
    assert "optional_upgrades:" in out
    assert "default_recap_pipeline" in out


def test_recap_full_mode_passes_explicit_narration_json(monkeypatch, tmp_path):
    """A stale cut-mode narration_mapped.json must not override full-mode narration."""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    work = tmp_path / "work"
    work.mkdir()
    (work / "narration.json").write_text(
        json.dumps([{"start": 0, "end": 1, "narration": "full。"}]),
        encoding="utf-8",
    )
    (work / "narration_mapped.json").write_text(
        json.dumps([{"start": 0, "end": 1, "narration": "stale cut。"}]),
        encoding="utf-8",
    )

    recap._write_run_manifest(work, video.resolve(), _manifest_args())

    calls = []

    def fake_run(skill, script, *cli_args):
        calls.append((skill, script, [str(arg) for arg in cli_args]))
        if script == "assemble.py":
            (work / "output.mp4").write_bytes(b"mp4")
            (work / "assembly_manifest.json").write_text(
                json.dumps({"final_output": str(tmp_path / "recap_video.mp4")}),
                encoding="utf-8",
            )

    monkeypatch.setattr("recap._run", fake_run)
    monkeypatch.setattr(sys, "argv", ["recap.py", str(video), "--work-dir", str(work)])

    recap.main()

    voiceover_call = next(call for call in calls if call[:2] == ("video-voiceover", "voiceover.py"))
    args = voiceover_call[2]
    assert args[args.index("--narration") + 1] == str(work / "narration.json")
    order = [script for _, script, _ in calls]
    assert order.index("validate.py") < order.index("review.py") < order.index("voiceover.py")


def test_recap_cut_mode_voiceover_uses_output_time_narration(monkeypatch, tmp_path):
    """Two-pass cut: narration is authored in OUTPUT time, so voiceover gets narration.json
    directly (no narration_mapped) and assemble muxes onto edited_source.mp4."""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    work = tmp_path / "work"
    work.mkdir()
    (work / "narration.json").write_text(
        json.dumps([{"start": 2, "end": 5, "narration": "output。"}]),
        encoding="utf-8",
    )
    (work / "clip_plan.json").write_text(
        json.dumps([{"start": 10, "end": 12}]),
        encoding="utf-8",
    )
    recap._write_run_manifest(work, video.resolve(), _manifest_args(edit_mode="cut"))
    recap._write_phase_ledger(work, clip_plan_fingerprint=recap._file_md5(work / "clip_plan.json"),
                              edited_source_rendered=True)
    calls = []

    def fake_run(skill, script, *cli_args):
        calls.append((skill, script, [str(arg) for arg in cli_args]))
        if script == "cut.py":
            (work / "edited_source.mp4").write_bytes(b"edited")
        if script == "assemble.py":
            (work / "output.mp4").write_bytes(b"mp4")
            (work / "assembly_manifest.json").write_text(
                json.dumps({"final_output": str(tmp_path / "recap_video.mp4")}),
                encoding="utf-8",
            )

    monkeypatch.setattr("recap._run", fake_run)
    monkeypatch.setattr("recap._read_video_duration_or_raise", lambda path: 10.0)
    monkeypatch.setattr(sys, "argv", ["recap.py", str(video), "--work-dir", str(work), "--edit-mode", "cut"])

    recap.main()

    vo = next(c for c in calls if c[:2] == ("video-voiceover", "voiceover.py"))[2]
    assert vo[vo.index("--narration") + 1] == str(work / "narration.json")    # NOT narration_mapped
    cut_render = next(c for c in calls if c[:2] == ("video-cut", "cut.py"))[2]
    assert "--no-narration-map" in cut_render
    asm = next(c for c in calls if c[:2] == ("video-assemble", "assemble.py"))[2]
    assert asm[0] == str(work / "edited_source.mp4")
    validate_args = next(c for c in calls if c[:2] == ("video-script", "validate.py"))[2]
    assert "--output-duration" in validate_args
    assert validate_args[validate_args.index("--output-duration") + 1] == "10.000"
    review_args = next(c for c in calls if c[:2] == ("video-script", "review.py"))[2]
    assert review_args[review_args.index("--timeline") + 1] == "cut_output"
    order = [script for _, script, _ in calls]
    assert order.index("validate.py") < order.index("review.py") < order.index("voiceover.py")




def test_recap_manifest_fingerprint_detects_middle_only_source_changes(tmp_path):
    first = tmp_path / "a.mp4"
    second = tmp_path / "b.mp4"
    first.write_bytes(b"A" * 70000 + b"middle-one" + b"Z" * 70000)
    second.write_bytes(b"A" * 70000 + b"middle-two" + b"Z" * 70000)

    assert first.stat().st_size == second.stat().st_size
    assert recap._file_fingerprint(first) != recap._file_fingerprint(second)


def test_recap_phase_b_rejects_work_dir_from_different_source(monkeypatch, tmp_path):
    """Phase B must not apply an existing narration.json to a different input video."""
    old_video = tmp_path / "old.mp4"
    new_video = tmp_path / "new.mp4"
    old_video.write_bytes(b"old-video")
    new_video.write_bytes(b"new-video")
    work = tmp_path / "work"
    work.mkdir()
    (work / "narration.json").write_text(
        json.dumps([{"start": 0, "end": 1, "narration": "old。"}]),
        encoding="utf-8",
    )
    recap._write_run_manifest(work, old_video.resolve(), _manifest_args())
    monkeypatch.setattr("recap._run", lambda *args: (_ for _ in ()).throw(AssertionError("must fail before stages run")))
    monkeypatch.setattr(sys, "argv", ["recap.py", str(new_video), "--work-dir", str(work)])

    with pytest.raises(SystemExit, match="work_dir 与当前 recap 输入不匹配"):
        recap.main()


def test_recap_honors_edit_mode_and_target_duration_env(monkeypatch, tmp_path):
    """Config playbook promises EDIT_MODE/TARGET_DURATION env fallbacks for the orchestrator."""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    work = tmp_path / "work"
    work.mkdir()
    (work / "narration.json").write_text(
        json.dumps([{"start": 10, "end": 12, "narration": "source。"}]),
        encoding="utf-8",
    )
    (work / "clip_plan.json").write_text(
        json.dumps([{"start": 10, "end": 12}]),
        encoding="utf-8",
    )
    recap._write_run_manifest(work, video.resolve(), _manifest_args(edit_mode="cut", target_duration="10m"))
    recap._write_phase_ledger(work, clip_plan_fingerprint=recap._file_md5(work / "clip_plan.json"),
                              edited_source_rendered=True)
    calls = []

    def fake_run(skill, script, *cli_args):
        calls.append((skill, script, [str(arg) for arg in cli_args]))
        if script == "cut.py":
            (work / "edited_source.mp4").write_bytes(b"edited")
        if script == "assemble.py":
            (work / "output.mp4").write_bytes(b"mp4")
            (work / "assembly_manifest.json").write_text(
                json.dumps({"final_output": str(tmp_path / "recap_video.mp4")}),
                encoding="utf-8",
            )

    monkeypatch.setenv("EDIT_MODE", "cut")
    monkeypatch.setenv("TARGET_DURATION", "10m")
    monkeypatch.setattr("recap._run", fake_run)
    monkeypatch.setattr("recap._read_video_duration_or_raise", lambda path: 600.0)
    monkeypatch.setattr(sys, "argv", ["recap.py", str(video), "--work-dir", str(work)])

    recap.main()

    cut_call = next(call for call in calls if call[:2] == ("video-cut", "cut.py"))
    cut_args = cut_call[2]
    assert "--target-duration" in cut_args
    assert cut_args[cut_args.index("--target-duration") + 1] == "10m"
    assert "--no-narration-map" in cut_args      # cut-first render, no source-time mapping
    validate_call = next(call for call in calls if call[:2] == ("video-script", "validate.py"))
    validate_args = validate_call[2]
    assert validate_args[validate_args.index("--mode") + 1] == "cut_output"
    assert validate_args[validate_args.index("--output-duration") + 1] == "600.000"


def test_recap_completion_prints_manifest_final_output(monkeypatch, tmp_path, capsys):
    """If assemble avoids a basename collision, recap should report that true path."""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    work = tmp_path / "work"
    work.mkdir()
    (work / "narration.json").write_text(
        json.dumps([{"start": 0, "end": 1, "narration": "full。"}]),
        encoding="utf-8",
    )
    recap._write_run_manifest(work, video.resolve(), _manifest_args())
    collision_safe = tmp_path / "recap_video_abcd1234ef.mp4"

    def fake_run(skill, script, *cli_args):
        if script == "assemble.py":
            (work / "output.mp4").write_bytes(b"mp4")
            (work / "assembly_manifest.json").write_text(
                json.dumps({"final_output": str(collision_safe)}),
                encoding="utf-8",
            )

    monkeypatch.setattr("recap._run", fake_run)
    monkeypatch.setattr(sys, "argv", ["recap.py", str(video), "--work-dir", str(work)])

    recap.main()

    assert str(collision_safe) in capsys.readouterr().out



def test_recap_continuation_preserves_phase_b_flags(tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    work = tmp_path / "work dir"
    args = _manifest_args()
    args.mimo_tts_voice = "冰糖"
    args.burn_subtitles = True
    args.output_dir = str(tmp_path / "out dir")
    args.export_jianying = True
    args.jianying_bundle_media = True
    args.jianying_no_bundle_media = False
    args.allow_partial_tts = True
    args.review_narration = False

    cmd = recap._continuation_command(video, work, args)

    assert "--mimo-tts-voice" in cmd and "冰糖" in cmd
    assert "--allow-partial-tts" in cmd
    assert "--burn-subtitles" in cmd
    assert "--output-dir" in cmd and "out dir" in cmd
    assert "--export-jianying" in cmd
    assert "--jianying-bundle-media" in cmd
    assert "--jianying-no-bundle-media" not in cmd
    assert "--no-review-narration" in cmd


def test_recap_forwards_allow_partial_tts_to_voiceover(monkeypatch, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    work = tmp_path / "work"
    work.mkdir()
    (work / "narration.json").write_text(
        json.dumps([{"start": 0, "end": 1, "narration": "full。"}]),
        encoding="utf-8",
    )
    recap._write_run_manifest(work, video.resolve(), _manifest_args())
    calls = []

    def fake_run(skill, script, *cli_args):
        calls.append((skill, script, [str(arg) for arg in cli_args]))
        if script == "assemble.py":
            (work / "output.mp4").write_bytes(b"mp4")
            (work / "assembly_manifest.json").write_text(
                json.dumps({"final_output": str(tmp_path / "recap_video.mp4")}),
                encoding="utf-8",
            )

    monkeypatch.setattr("recap._run", fake_run)
    monkeypatch.setattr(sys, "argv", [
        "recap.py", str(video), "--work-dir", str(work), "--allow-partial-tts",
    ])

    recap.main()

    voiceover_call = next(call for call in calls if call[:2] == ("video-voiceover", "voiceover.py"))
    assert "--allow-partial-tts" in voiceover_call[2]


def test_recap_phase_b_allows_environment_changes_when_artifacts_are_unchanged(monkeypatch, tmp_path):
    """Phase-B resume is gated on source bytes + settings; harmless env drift must not block it."""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    work = tmp_path / "work"
    work.mkdir()
    (work / "narration.json").write_text(
        json.dumps([{"start": 0, "end": 1, "narration": "old。"}]),
        encoding="utf-8",
    )
    recap._write_run_manifest(work, video.resolve(), _manifest_args(skip_asr=True))

    # The minimal resume gate is keyed on source-video bytes + CLI/env settings only.
    # ASR/VLM endpoint or model env drift does not change Phase-A artifacts already on
    # disk (Phase B never re-runs them), so it must not block a legitimate resume.
    calls = []

    def fake_run(skill, script, *cli_args):
        calls.append((skill, script, [str(arg) for arg in cli_args]))
        if script == "assemble.py":
            (work / "output.mp4").write_bytes(b"mp4")
            (work / "assembly_manifest.json").write_text(
                json.dumps({"final_output": str(tmp_path / "recap_video.mp4")}),
                encoding="utf-8",
            )

    monkeypatch.setattr("recap._run", fake_run)
    monkeypatch.setattr(sys, "argv", ["recap.py", str(video), "--work-dir", str(work), "--skip-asr"])

    recap.main()

    assert any(call[:2] == ("video-assemble", "assemble.py") for call in calls)


def test_recap_advisory_review_failure_is_fail_open(monkeypatch, tmp_path, capsys):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    work = tmp_path / "work"
    work.mkdir()
    (work / "narration.json").write_text(
        json.dumps([{"start": 0, "end": 1, "narration": "full。"}]),
        encoding="utf-8",
    )
    recap._write_run_manifest(work, video.resolve(), _manifest_args())
    calls = []

    def fake_run(skill, script, *cli_args):
        calls.append((skill, script, [str(arg) for arg in cli_args]))
        if script == "review.py":
            raise SystemExit("review API unavailable")
        if script == "assemble.py":
            (work / "output.mp4").write_bytes(b"mp4")
            (work / "assembly_manifest.json").write_text(
                json.dumps({"final_output": str(tmp_path / "recap_video.mp4")}),
                encoding="utf-8",
            )

    monkeypatch.setattr("recap._run", fake_run)
    monkeypatch.setattr(sys, "argv", ["recap.py", str(video), "--work-dir", str(work)])

    recap.main()

    order = [script for _, script, _ in calls]
    assert "review.py" in order and "voiceover.py" in order
    assert order.index("review.py") < order.index("voiceover.py")
    assert "建议性评审失败" in capsys.readouterr().out


def test_recap_does_not_print_stale_review_pointer_after_fail_open(monkeypatch, tmp_path, capsys):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    work = tmp_path / "work"
    work.mkdir()
    (work / "narration.json").write_text(
        json.dumps([{"start": 0, "end": 1, "narration": "full。"}]),
        encoding="utf-8",
    )
    (work / "narration_review.md").write_text("# stale review", encoding="utf-8")
    recap._write_run_manifest(work, video.resolve(), _manifest_args())

    def fake_run(skill, script, *cli_args):
        if script == "review.py":
            raise SystemExit("review API unavailable")
        if script == "assemble.py":
            (work / "output.mp4").write_bytes(b"mp4")
            (work / "assembly_manifest.json").write_text(
                json.dumps({"final_output": str(tmp_path / "recap_video.mp4")}),
                encoding="utf-8",
            )

    monkeypatch.setattr("recap._run", fake_run)
    monkeypatch.setattr(sys, "argv", ["recap.py", str(video), "--work-dir", str(work)])

    recap.main()

    out = capsys.readouterr().out
    assert "建议性评审失败" in out
    assert "解说评审" not in out
    assert "stale" not in out


def test_recap_advisory_review_can_be_disabled(monkeypatch, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    work = tmp_path / "work"
    work.mkdir()
    (work / "narration.json").write_text(
        json.dumps([{"start": 0, "end": 1, "narration": "full。"}]),
        encoding="utf-8",
    )
    recap._write_run_manifest(work, video.resolve(), _manifest_args())
    calls = []

    def fake_run(skill, script, *cli_args):
        calls.append((skill, script, [str(arg) for arg in cli_args]))
        if script == "assemble.py":
            (work / "output.mp4").write_bytes(b"mp4")
            (work / "assembly_manifest.json").write_text(
                json.dumps({"final_output": str(tmp_path / "recap_video.mp4")}),
                encoding="utf-8",
            )

    monkeypatch.setattr("recap._run", fake_run)
    monkeypatch.setattr(sys, "argv", [
        "recap.py", str(video), "--work-dir", str(work), "--no-review-narration",
    ])

    recap.main()

    assert not any(call[:2] == ("video-script", "review.py") for call in calls)
    assert any(call[:2] == ("video-voiceover", "voiceover.py") for call in calls)


def test_recap_cut_duration_read_failure_stops_before_tts(monkeypatch, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    work = tmp_path / "work"
    work.mkdir()
    (work / "narration.json").write_text(
        json.dumps([{"start": 1, "end": 3, "narration": "解说。"}]), encoding="utf-8")
    (work / "clip_plan.json").write_text(json.dumps([{"start": 10, "end": 12}]), encoding="utf-8")
    recap._write_run_manifest(work, video.resolve(), _manifest_args(edit_mode="cut"))
    recap._write_phase_ledger(work, clip_plan_fingerprint=recap._file_md5(work / "clip_plan.json"),
                              edited_source_rendered=True)

    def fake_run(skill, script, *cli_args):
        if script == "cut.py":
            (work / "edited_source.mp4").write_bytes(b"edited")
        if script in ("validate.py", "review.py", "voiceover.py", "assemble.py"):
            raise AssertionError(f"{script} must not run when duration cannot be read")

    monkeypatch.setattr("recap._run", fake_run)
    monkeypatch.setattr("recap._read_video_duration_or_raise",
                        lambda path: (_ for _ in ()).throw(SystemExit("无法读取成片时长")))
    monkeypatch.setattr(sys, "argv", ["recap.py", str(video), "--work-dir", str(work), "--edit-mode", "cut"])

    with pytest.raises(SystemExit, match="无法读取成片时长"):
        recap.main()


def test_recap_resumes_old_manifest_missing_consolidate_key(monkeypatch, tmp_path):
    """Step 2 backward-compat: --consolidate now defaults ON, so its value lives in the run
    manifest. A work_dir whose manifest predates the consolidate setting (key absent) — or
    carries the old default false — must still resume, not hard-fail on a settings mismatch."""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    work = tmp_path / "work"
    work.mkdir()
    (work / "narration.json").write_text(
        json.dumps([{"start": 0, "end": 1, "narration": "old。"}]),
        encoding="utf-8",
    )
    recap._write_run_manifest(work, video.resolve(), _manifest_args())
    manifest = json.loads((work / recap.RUN_MANIFEST).read_text(encoding="utf-8"))
    manifest["settings"].pop("consolidate", None)        # simulate a pre-consolidate-key manifest
    manifest["settings"].pop("consolidate_asr", None)
    (work / recap.RUN_MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")

    calls = []

    def fake_run(skill, script, *cli_args):
        calls.append((skill, script, [str(arg) for arg in cli_args]))
        if script == "assemble.py":
            (work / "output.mp4").write_bytes(b"mp4")
            (work / "assembly_manifest.json").write_text(
                json.dumps({"final_output": str(tmp_path / "recap_video.mp4")}),
                encoding="utf-8",
            )

    monkeypatch.setattr("recap._run", fake_run)
    monkeypatch.setattr(sys, "argv", ["recap.py", str(video), "--work-dir", str(work)])

    recap.main()  # must NOT SystemExit on a consolidate settings mismatch

    assert any(call[:2] == ("video-assemble", "assemble.py") for call in calls)


def test_recap_pause_banner_amplifies_research_when_brief_flags_thin(monkeypatch, tmp_path, capsys):
    """Step 3: when the Phase-A brief fires the research directive (thin substrate, no research),
    the recap pause banner amplifies it so the agent researches before writing."""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    work = tmp_path / "work"
    work.mkdir()

    def fake_run(skill, script, *cli_args):
        if script == "understand.py":
            (work / "agent_narration_brief.md").write_text(
                "# Agent Narration Brief\n\n## ⚑ Research the story FIRST (do this before writing narration)\n",
                encoding="utf-8",
            )

    monkeypatch.setattr("recap._run", fake_run)
    monkeypatch.setattr(sys, "argv", ["recap.py", str(video), "--work-dir", str(work)])

    recap.main()  # Phase A (no narration.json) -> pause

    out = capsys.readouterr().out
    assert "理解素材偏薄" in out
    assert "Research the story FIRST" in out


def test_recap_pause_banner_quiet_when_brief_has_no_research_flag(monkeypatch, tmp_path, capsys):
    """A rich-substrate brief (no research directive) must NOT add a research nag to the banner."""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    work = tmp_path / "work"
    work.mkdir()

    def fake_run(skill, script, *cli_args):
        if script == "understand.py":
            (work / "agent_narration_brief.md").write_text("# Agent Narration Brief\n", encoding="utf-8")

    monkeypatch.setattr("recap._run", fake_run)
    monkeypatch.setattr(sys, "argv", ["recap.py", str(video), "--work-dir", str(work)])

    recap.main()

    assert "理解素材偏薄" not in capsys.readouterr().out


def test_recap_cut_two_pass_renders_then_pauses_for_output_narration(monkeypatch, tmp_path):
    """Step 6: cut mode is two-pass. With clip_plan present but narration absent, recap renders
    the cut (--no-narration-map, no source-time mapping) and PAUSES for OUTPUT-time narration —
    it does not run voiceover/assemble yet, and the ledger records the rendered cut."""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    work = tmp_path / "work"
    work.mkdir()
    (work / "clip_plan.json").write_text(json.dumps([{"start": 10, "end": 12}]), encoding="utf-8")
    recap._write_run_manifest(work, video.resolve(), _manifest_args(edit_mode="cut"))
    calls = []

    def fake_run(skill, script, *cli_args):
        cli = [str(a) for a in cli_args]
        calls.append((skill, script, cli))
        if script == "cut.py":
            (work / "edited_source.mp4").write_bytes(b"edited")
            (work / "clip_plan_validated.json").write_text(json.dumps({"clips": []}), encoding="utf-8")

    monkeypatch.setattr("recap._run", fake_run)
    monkeypatch.setattr(sys, "argv", ["recap.py", str(video), "--work-dir", str(work), "--edit-mode", "cut"])

    recap.main()  # PASS 2: render the cut, then pause (narration.json absent)

    cut_calls = [c for c in calls if c[:2] == ("video-cut", "cut.py")]
    assert cut_calls and all("--no-narration-map" in c[2] for c in cut_calls)
    assert all("--normalize-only" not in c[2] for c in cut_calls)        # mapping path is bypassed
    assert not any(c[1] in ("voiceover.py", "assemble.py") for c in calls)  # paused, not produced
    assert recap._read_phase_ledger(work).get("edited_source_rendered") is True


def test_cut_narration_stale_guard_logic():
    """Two-pass cut: the narration is written for the rendered cut, so any clip_plan change
    while that narration is still present makes it stale (it describes the old cut)."""
    assert recap._cut_narration_is_stale(None, "cp1") is False
    base = {"clip_plan_fingerprint": "cp1"}
    assert recap._cut_narration_is_stale(base, "cp1") is False    # clip_plan unchanged
    assert recap._cut_narration_is_stale(base, "cp2") is True     # clip_plan changed -> stale


def test_recap_cut_rejects_stale_narration_after_clip_plan_change(monkeypatch, tmp_path):
    """Step 6: a narration written for a previous clip_plan must not drive a re-cut into TTS;
    the render may run, but validate/voiceover/assemble must not."""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    work = tmp_path / "work"
    work.mkdir()
    (work / "narration.json").write_text(
        json.dumps([{"start": 1, "end": 3, "narration": "解说。"}]), encoding="utf-8")
    (work / "clip_plan.json").write_text(json.dumps([{"start": 10, "end": 12}]), encoding="utf-8")
    recap._write_run_manifest(work, video.resolve(), _manifest_args(edit_mode="cut"))
    recap._write_phase_ledger(work, clip_plan_fingerprint="OLD_DIFFERENT_FP", edited_source_rendered=True)

    def fake_run(skill, script, *cli_args):
        if script == "cut.py":
            (work / "edited_source.mp4").write_bytes(b"edited")   # render allowed
        if script in ("validate.py", "voiceover.py", "assemble.py"):
            raise AssertionError(f"{script} ran despite stale narration")

    monkeypatch.setattr("recap._run", fake_run)
    monkeypatch.setattr(sys, "argv", ["recap.py", str(video), "--work-dir", str(work), "--edit-mode", "cut"])

    with pytest.raises(SystemExit, match="clip_plan.json 已改变"):
        recap.main()


def test_recap_full_mode_writes_no_phase_ledger(monkeypatch, tmp_path):
    """Step 5: the phase ledger is cut-mode only; full mode stays unchanged."""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    work = tmp_path / "work"
    work.mkdir()
    (work / "narration.json").write_text(
        json.dumps([{"start": 0, "end": 1, "narration": "full。"}]), encoding="utf-8")
    recap._write_run_manifest(work, video.resolve(), _manifest_args())

    def fake_run(skill, script, *cli_args):
        if script == "assemble.py":
            (work / "output.mp4").write_bytes(b"mp4")
            (work / "assembly_manifest.json").write_text(
                json.dumps({"final_output": str(tmp_path / "r.mp4")}), encoding="utf-8")

    monkeypatch.setattr("recap._run", fake_run)
    monkeypatch.setattr(sys, "argv", ["recap.py", str(video), "--work-dir", str(work)])

    recap.main()

    assert not (work / "recap_phase.json").exists()


def test_strict_narration_review_blocks_voiceover_on_review_failure(monkeypatch, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    work = tmp_path / "work"
    work.mkdir()
    (work / "narration.json").write_text(json.dumps([{"start": 0, "end": 1, "narration": "full。"}]), encoding="utf-8")
    recap._write_run_manifest(work, video.resolve(), _manifest_args())

    def fake_run(skill, script, *cli_args):
        if script == "review.py":
            raise SystemExit("review unavailable")
        if script == "voiceover.py":
            raise AssertionError("voiceover must not run in strict review mode")

    monkeypatch.setattr("recap._run", fake_run)
    monkeypatch.setattr(sys, "argv", [
        "recap.py", str(video), "--work-dir", str(work), "--require-narration-review",
    ])

    with pytest.raises(SystemExit, match="严格解说评审失败"):
        recap.main()


def test_strict_narration_review_blocks_voiceover_on_error_finding(monkeypatch, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    work = tmp_path / "work"
    work.mkdir()
    (work / "narration.json").write_text(json.dumps([{"start": 0, "end": 1, "narration": "full。"}]), encoding="utf-8")
    recap._write_run_manifest(work, video.resolve(), _manifest_args())

    def fake_run(skill, script, *cli_args):
        if script == "review.py":
            (work / "narration_review.json").write_text(json.dumps({
                "verdict": "REVISE",
                "findings": [{"severity": "error", "issue": "幻觉"}],
            }), encoding="utf-8")
            return
        if script == "voiceover.py":
            raise AssertionError("voiceover must not run with strict review errors")

    monkeypatch.setattr("recap._run", fake_run)
    monkeypatch.setattr(sys, "argv", [
        "recap.py", str(video), "--work-dir", str(work), "--require-narration-review",
    ])

    with pytest.raises(SystemExit, match="error 1"):
        recap.main()


def test_strict_narration_review_requires_fresh_review_artifact(monkeypatch, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    work = tmp_path / "work"
    work.mkdir()
    (work / "narration.json").write_text(json.dumps([{"start": 0, "end": 1, "narration": "full。"}]), encoding="utf-8")
    (work / "narration_review.json").write_text(json.dumps({
        "verdict": "PASS",
        "findings": [],
    }), encoding="utf-8")
    (work / "narration_review.md").write_text("# stale pass", encoding="utf-8")
    recap._write_run_manifest(work, video.resolve(), _manifest_args())

    def fake_run(skill, script, *cli_args):
        if script == "review.py":
            return
        if script == "voiceover.py":
            raise AssertionError("voiceover must not run without a fresh review artifact")

    monkeypatch.setattr("recap._run", fake_run)
    monkeypatch.setattr(sys, "argv", [
        "recap.py", str(video), "--work-dir", str(work), "--require-narration-review",
    ])

    with pytest.raises(SystemExit, match="missing or invalid narration_review.json"):
        recap.main()
    assert not (work / "narration_review.md").exists()


def test_advisory_narration_review_still_continues_on_failure(monkeypatch, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    work = tmp_path / "work"
    work.mkdir()
    (work / "narration.json").write_text(json.dumps([{"start": 0, "end": 1, "narration": "full。"}]), encoding="utf-8")
    recap._write_run_manifest(work, video.resolve(), _manifest_args())
    calls = []

    def fake_run(skill, script, *cli_args):
        calls.append(script)
        if script == "review.py":
            raise SystemExit("review unavailable")
        if script == "assemble.py":
            (work / "assembly_manifest.json").write_text(
                json.dumps({"final_output": str(tmp_path / "r.mp4")}), encoding="utf-8")

    monkeypatch.setattr("recap._run", fake_run)
    monkeypatch.setattr(sys, "argv", ["recap.py", str(video), "--work-dir", str(work)])

    recap.main()

    assert "voiceover.py" in calls
    assert "assemble.py" in calls


def test_continuation_command_preserves_require_narration_review():
    args = _manifest_args()
    args.require_narration_review = True

    command = recap._continuation_command("/tmp/in.mp4", "/tmp/work", args)

    assert "--require-narration-review" in command


def test_review_status_does_not_gate_on_bare_model_verdict(tmp_path):
    """Strict gate must key off parse_error / factual error findings ONLY — never the model's
    holistic verdict. A subjective FAIL (or REVISE) with no error finding must not block TTS."""
    def _write(payload):
        (tmp_path / "narration_review.json").write_text(json.dumps(payload), encoding="utf-8")

    _write({"verdict": "FAIL", "findings": []})
    assert recap._review_result_status(tmp_path)["ok"] is True

    _write({"verdict": "REVISE", "findings": [{"severity": "warning", "category": "weak_hook", "issue": "x"}]})
    assert recap._review_result_status(tmp_path)["ok"] is True

    # A factual defect still gates — but via the error finding, not the verdict.
    _write({"verdict": "PASS", "findings": [{"severity": "error", "category": "hallucination", "issue": "x"}]})
    status = recap._review_result_status(tmp_path)
    assert status["ok"] is False and status["errors"] == 1


def test_recap_rejects_multi_video_non_cut(monkeypatch, tmp_path):
    v1 = tmp_path / "a.mp4"
    v2 = tmp_path / "b.mp4"
    v1.write_bytes(b"a")
    v2.write_bytes(b"b")
    monkeypatch.setattr("recap._run", lambda *args: (_ for _ in ()).throw(AssertionError("must fail first")))
    monkeypatch.setattr(sys, "argv", ["recap.py", str(v1), str(v2), "--edit-mode", "full"])

    with pytest.raises(SystemExit, match="多视频输入当前 MVP 只支持 --edit-mode cut"):
        recap.main()


def test_recap_multi_video_phase_a_writes_manifest_and_pauses(monkeypatch, tmp_path, capsys):
    v1 = tmp_path / "a.mp4"
    v2 = tmp_path / "b.mp4"
    v1.write_bytes(b"a source")
    v2.write_bytes(b"b source")
    work = tmp_path / "project"
    calls = []

    def fake_run(skill, script, *cli_args):
        calls.append((skill, script, [str(a) for a in cli_args]))
        if script == "understand.py":
            wd = Path(cli_args[cli_args.index("--work-dir") + 1])
            wd.mkdir(parents=True, exist_ok=True)
            (wd / "agent_narration_brief.md").write_text("# per-source\n", encoding="utf-8")
            (wd / "scenes.json").write_text("[]", encoding="utf-8")

    monkeypatch.setattr("recap._run", fake_run)
    monkeypatch.setattr(sys, "argv", [
        "recap.py", str(v1), str(v2), "--work-dir", str(work), "--edit-mode", "cut",
        "--material-library-dir", str(tmp_path / "lib"), "--save-materials",
    ])

    recap.main()

    manifest = json.loads((work / "multi_source_manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["sources"]) == 2
    assert all(s["source_id"].startswith("src_") for s in manifest["sources"])
    assert (work / "recap_run_manifest.json").exists()
    assert "source_id" in (work / "agent_narration_brief.md").read_text(encoding="utf-8")
    assert len([c for c in calls if c[:2] == ("video-understanding", "understand.py")]) == 2
    assert (tmp_path / "lib" / "materials_index.jsonl").exists()
    out = capsys.readouterr().out
    assert "clip_plan.json" in out
    assert "--material-library-dir" in out and "--save-materials" in out


def test_recap_multi_video_phase_b_invokes_cut_with_sources_manifest(monkeypatch, tmp_path):
    v1 = tmp_path / "a.mp4"
    v2 = tmp_path / "b.mp4"
    v1.write_bytes(b"a source")
    v2.write_bytes(b"b source")
    work = tmp_path / "project"
    work.mkdir()
    args = _manifest_args(edit_mode="cut")
    records = recap._build_multi_source_records([v1.resolve(), v2.resolve()], work, args)
    recap._write_multi_source_manifest(work, records)
    recap._write_project_run_manifest(work, [v1.resolve(), v2.resolve()], args, records)
    (work / "clip_plan.json").write_text(json.dumps({
        "clips": [{"source_id": records[0]["source_id"], "start": 0, "end": 1}]
    }), encoding="utf-8")
    calls = []

    def fake_run(skill, script, *cli_args):
        calls.append((skill, script, [str(a) for a in cli_args]))
        if script == "cut.py":
            (work / "edited_source.mp4").write_bytes(b"edited")
            (work / "clip_plan_validated.json").write_text(json.dumps({
                "clips": [{"source_id": records[0]["source_id"], "source_path": str(v1.resolve()),
                           "source_start": 0, "source_end": 1, "output_start": 0, "output_end": 1,
                           "duration": 1}]
            }), encoding="utf-8")

    monkeypatch.setattr("recap._run", fake_run)
    monkeypatch.setattr(sys, "argv", ["recap.py", str(v1), str(v2), "--work-dir", str(work), "--edit-mode", "cut"])

    recap.main()

    cut_args = next(c for c in calls if c[:2] == ("video-cut", "cut.py"))[2]
    assert "--sources-manifest" in cut_args
    assert cut_args[cut_args.index("--sources-manifest") + 1] == str(work / "multi_source_manifest.json")
    assert "--no-narration-map" in cut_args
    assert not any(c[1] in ("voiceover.py", "assemble.py") for c in calls)
    assert recap._read_phase_ledger(work)["multi_source"] is True


def test_continuation_command_preserves_multi_videos_and_material_flags(tmp_path):
    args = _manifest_args(edit_mode="cut")
    args.mimo_tts_voice = None
    args.burn_subtitles = None
    args.output_dir = None
    args.export_jianying = False
    args.jianying_bundle_media = False
    args.jianying_no_bundle_media = False
    args.allow_partial_tts = False
    args.review_narration = None
    args.require_narration_review = False
    args.material_library_dir = str(tmp_path / "lib")
    args.use_materials = True
    args.save_materials = True

    cmd = recap._continuation_command([tmp_path / "a.mp4", tmp_path / "b.mp4"], tmp_path / "work", args)

    assert "a.mp4" in cmd and "b.mp4" in cmd
    assert "--material-library-dir" in cmd
    assert "--use-materials" in cmd
    assert "--save-materials" in cmd


def test_recap_single_video_phase_a_can_save_materials(monkeypatch, tmp_path):
    video = tmp_path / "solo.mp4"
    video.write_bytes(b"solo source")
    work = tmp_path / "work"
    lib = tmp_path / "materials"

    def fake_run(skill, script, *cli_args):
        assert (skill, script) == ("video-understanding", "understand.py")
        wd = Path(cli_args[cli_args.index("--work-dir") + 1])
        wd.mkdir(parents=True, exist_ok=True)
        (wd / "agent_narration_brief.md").write_text("# solo brief", encoding="utf-8")
        (wd / "scenes.json").write_text("[]", encoding="utf-8")

    monkeypatch.setattr("recap._run", fake_run)
    monkeypatch.setattr(sys, "argv", [
        "recap.py", str(video), "--work-dir", str(work),
        "--material-library-dir", str(lib), "--save-materials",
    ])

    recap.main()

    assert (lib / "materials_index.jsonl").exists()
    saved = list((lib / "materials").glob("*/material.json"))
    assert saved
    assert (work / "recap_run_manifest.json").exists()


def test_recap_single_video_phase_a_uses_materials_without_understand(monkeypatch, tmp_path):
    video = tmp_path / "solo.mp4"
    video.write_bytes(b"solo source")
    seed_work = tmp_path / "seed"
    seed_work.mkdir()
    (seed_work / "agent_narration_brief.md").write_text("# restored brief", encoding="utf-8")
    (seed_work / "scenes.json").write_text("[]", encoding="utf-8")
    args = _manifest_args(consolidate=True)
    fp = recap._file_fingerprint(video)
    settings_fp = recap._material_settings_fingerprint(args)
    lib = tmp_path / "materials"
    recap.material_lib.save_material(lib, seed_work, video, fp, settings_fp)
    work = tmp_path / "work"

    def fake_run(*_args):
        raise AssertionError("understand.py should not run when material restore matches")

    monkeypatch.setattr("recap._run", fake_run)
    monkeypatch.setattr(sys, "argv", [
        "recap.py", str(video), "--work-dir", str(work),
        "--material-library-dir", str(lib), "--use-materials",
    ])

    recap.main()

    assert (work / "agent_narration_brief.md").read_text(encoding="utf-8") == "# restored brief"
    assert (work / "scenes.json").exists()
    assert (work / "recap_run_manifest.json").exists()


def test_recap_single_video_cut_pass2_rebuilds_output_brief_with_materials_enabled(monkeypatch, tmp_path):
    video = tmp_path / "solo.mp4"
    video.write_bytes(b"solo source")
    work = tmp_path / "work"
    work.mkdir()
    args = _manifest_args(edit_mode="cut", consolidate=True)
    # Existing Phase A state: material restore may have produced a source-time brief, but
    # pass 2 must replace it with an OUTPUT-time brief after edited_source.mp4 exists.
    recap._write_run_manifest(work, video, args)
    (work / "clip_plan.json").write_text(json.dumps({"clips": [{"start": 0, "end": 1}]}), encoding="utf-8")
    (work / "agent_narration_brief.md").write_text("SOURCE-TIME BRIEF FROM MATERIAL", encoding="utf-8")
    lib = tmp_path / "materials"
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "agent_narration_brief.md").write_text("SOURCE-TIME BRIEF FROM MATERIAL", encoding="utf-8")
    (seed / "scenes.json").write_text("[]", encoding="utf-8")
    recap.material_lib.save_material(
        lib, seed, video, recap._file_fingerprint(video),
        recap._material_settings_fingerprint(args),
    )
    calls = []

    def fake_run(skill, script, *cli_args):
        calls.append((skill, script, [str(a) for a in cli_args]))
        if script == "cut.py":
            (work / "edited_source.mp4").write_bytes(b"edited")
            (work / "clip_plan_validated.json").write_text(json.dumps({
                "raw_plan_fingerprint": "not-used-here",
                "clips": [{"source_start": 0, "source_end": 1, "output_start": 0, "output_end": 1,
                           "duration": 1}]
            }), encoding="utf-8")
        elif script == "understand.py":
            assert "--brief-only" in [str(a) for a in cli_args]
            (work / "agent_narration_brief.md").write_text("OUTPUT-TIME BRIEF", encoding="utf-8")

    monkeypatch.setattr("recap._run", fake_run)
    monkeypatch.setattr(sys, "argv", [
        "recap.py", str(video), "--work-dir", str(work), "--edit-mode", "cut",
        "--material-library-dir", str(lib), "--use-materials",
    ])

    recap.main()

    understand_calls = [c for c in calls if c[:2] == ("video-understanding", "understand.py")]
    assert len(understand_calls) == 1
    assert "--brief-only" in understand_calls[0][2]
    assert (work / "agent_narration_brief.md").read_text(encoding="utf-8") == "OUTPUT-TIME BRIEF"
