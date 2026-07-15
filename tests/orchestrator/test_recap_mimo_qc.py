import json
import os
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "skills" / "video-recap" / "scripts")
)

import recap_runner as recap  # noqa: E402
import recap_stage_qc  # noqa: E402
import recap_runtime  # noqa: E402
import recap_timeline  # noqa: E402


def _manifest_args(**overrides):
    values = {
        "context": "",
        "scene_threshold": None,
        "style": "纪录片",
        "edit_mode": "full",
        "target_duration": None,
        "skip_asr": False,
        "mimo_video_overview": False,
        "consolidate": True,
        "consolidate_asr": False,
        "review_narration": None,
        "require_narration_review": False,
        "allow_duration_drift": False,
        "allow_sparse_cut": False,
    }
    values.update(overrides)
    return Namespace(**values)


def _mimo_result(work, stage, status="completed"):
    return {
        "path": str(Path(work) / "mimo_qc.json"),
        "report": {
            "metadata": {"status": status},
            "finding_count": 0,
            "findings": [],
        },
    }


def test_mimo_qc_off_clears_stale_report_without_request(monkeypatch, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    stale = work / "mimo_qc.json"
    stale.write_text("{}", encoding="utf-8")
    args = Namespace(mimo_qc="off", mimo_qc_refresh=False)
    calls = []
    monkeypatch.setattr(
        recap_stage_qc.mimo_qc, "run", lambda *a, **k: calls.append((a, k))
    )

    recap_stage_qc._prepare_mimo_qc(work, args)
    result = recap_stage_qc._run_mimo_qc_stage(work, args, "pre_assemble")

    assert result is None
    assert calls == []
    assert not stale.exists()


def test_continuation_command_preserves_mimo_qc_mode_and_refresh(tmp_path):
    args = _manifest_args()
    args.mimo_qc = "both"
    args.mimo_qc_refresh = True

    command = recap_timeline._continuation_command(
        tmp_path / "video.mp4", tmp_path / "work", args
    )

    assert "--mimo-qc both" in command
    assert "--mimo-qc-refresh" in command


def test_full_pipeline_runs_pre_before_assemble_and_post_before_final_qc(
    monkeypatch, tmp_path
):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    work = tmp_path / "work"
    work.mkdir()
    (work / "narration.json").write_text(
        json.dumps([{"start": 0, "end": 1, "narration": "line"}]), encoding="utf-8"
    )
    recap_runtime._write_run_manifest(work, video.resolve(), _manifest_args())
    events = []
    final_output = tmp_path / "recap_video.mp4"

    def fake_run(_skill, script, *args):
        if script == "voiceover.py":
            (work / "tts_meta.json").write_text('{"segments": []}', encoding="utf-8")
        if script == "assemble.py":
            events.append("assemble")
            final_output.write_bytes(b"mp4")
            (work / "assembly_manifest.json").write_text(
                json.dumps({"final_output": str(final_output)}), encoding="utf-8"
            )

    def fake_mimo(work_dir, *, stage, **kwargs):
        events.append(f"mimo:{stage}")
        if stage == "post_render":
            assert kwargs["final_output"] == final_output
        return _mimo_result(work_dir, stage)

    monkeypatch.setattr(recap, "_run", fake_run)
    monkeypatch.setattr(recap, "_preflight_burn_subtitles", lambda _args: None)
    monkeypatch.setattr(recap, "run_narration_review", lambda *_a, **_k: False)
    monkeypatch.setattr(recap_stage_qc.mimo_qc, "run", fake_mimo)
    monkeypatch.setattr(
        recap,
        "_write_final_qc_reports",
        lambda *_a, **_k: (
            events.append("final_qc") or {"final_qc": {}, "golden_eval": {}}
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "recap.py",
            str(video),
            "--work-dir",
            str(work),
            "--mimo-qc",
            "both",
        ],
    )

    recap.main()

    assert events == ["mimo:pre_assemble", "assemble", "mimo:post_render", "final_qc"]


def test_multi_pipeline_uses_the_same_mimo_stage_order(monkeypatch, tmp_path):
    first, second = tmp_path / "a.mp4", tmp_path / "b.mp4"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    work = tmp_path / "project"
    work.mkdir()
    args = _manifest_args(edit_mode="cut")
    records = recap_runtime._build_multi_source_records(
        [first.resolve(), second.resolve()], args
    )
    recap_runtime._write_multi_source_manifest(work, records)
    recap_runtime._write_project_run_manifest(
        work, [first.resolve(), second.resolve()], args, records
    )
    (work / "clip_plan.json").write_text(
        json.dumps(
            {"clips": [{"source_id": records[0]["source_id"], "start": 0, "end": 1}]}
        ),
        encoding="utf-8",
    )
    (work / "narration.json").write_text(
        json.dumps([{"start": 0, "end": 1, "narration": "line"}]), encoding="utf-8"
    )
    final_output = tmp_path / "multi.mp4"
    events = []

    def fake_run(_skill, script, *cli_args):
        if script == "cut.py":
            (work / "edited_source.mp4").write_bytes(b"edited")
            (work / "clip_plan_validated.json").write_text(
                json.dumps(
                    {
                        "clips": [
                            {
                                "source_id": records[0]["source_id"],
                                "source_path": str(first.resolve()),
                                "source_start": 0,
                                "source_end": 1,
                                "output_start": 0,
                                "output_end": 1,
                                "duration": 1,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
        elif script == "voiceover.py":
            (work / "tts_meta.json").write_text('{"segments": []}', encoding="utf-8")
        elif script == "assemble.py":
            events.append("assemble")
            final_output.write_bytes(b"mp4")
            (work / "assembly_manifest.json").write_text(
                json.dumps({"final_output": str(final_output)}), encoding="utf-8"
            )

    def fake_mimo(work_dir, *, stage, **_kwargs):
        events.append(f"mimo:{stage}")
        return _mimo_result(work_dir, stage)

    monkeypatch.setattr(recap, "_run", fake_run)
    monkeypatch.setattr(recap, "_preflight_burn_subtitles", lambda _args: None)
    monkeypatch.setattr(recap, "_read_video_duration_or_raise", lambda _path: 1.0)
    monkeypatch.setattr(recap, "run_narration_review", lambda *_a, **_k: False)
    monkeypatch.setattr(recap_stage_qc.mimo_qc, "run", fake_mimo)
    monkeypatch.setattr(
        recap,
        "_write_final_qc_reports",
        lambda *_a, **_k: (
            events.append("final_qc") or {"final_qc": {}, "golden_eval": {}}
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "recap.py",
            str(first),
            str(second),
            "--work-dir",
            str(work),
            "--edit-mode",
            "cut",
            "--mimo-qc",
            "both",
        ],
    )

    recap.main()

    assert events == ["mimo:pre_assemble", "assemble", "mimo:post_render", "final_qc"]


def test_mimo_qc_stage_exception_is_visible_but_fail_open(
    monkeypatch, tmp_path, capsys
):
    args = Namespace(mimo_qc="pre-assemble", mimo_qc_refresh=False)
    monkeypatch.setattr(
        recap_stage_qc.mimo_qc,
        "run",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    assert recap_stage_qc._run_mimo_qc_stage(tmp_path, args, "pre_assemble") is None
    output = capsys.readouterr().out
    assert "MiMo QC" in output
    assert "继续流水线" in output


def test_mimo_qc_loads_its_own_client_after_another_skill_lib(tmp_path):
    root = Path(__file__).resolve().parents[2]
    script_lib = root / "skills" / "video-script" / "scripts"
    recap_lib = root / "skills" / "video-recap" / "scripts"
    code = (
        "import sys; "
        f"sys.path.insert(0, {str(script_lib)!r}); import lib; "
        f"sys.path.insert(0, {str(recap_lib)!r}); import mimo_qc_report; "
        "assert callable(mimo_qc_report.mimo_qc_api_call)"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=dict(os.environ),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_invalid_mimo_qc_environment_mode_is_rejected(monkeypatch, capsys):
    monkeypatch.setenv("MIMO_QC", "sometimes")
    monkeypatch.setattr(sys, "argv", ["recap.py"])

    with pytest.raises(SystemExit):
        recap.main()

    assert "MIMO_QC/--mimo-qc must be one of" in capsys.readouterr().err
