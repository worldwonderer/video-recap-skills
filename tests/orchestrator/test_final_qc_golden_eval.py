import json
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "skills" / "video-recap" / "scripts")
)

import final_qc  # noqa: E402
import qc_contract as qc  # noqa: E402
import recap_stage_qc as recap  # noqa: E402


def _write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _probe(duration=12.5, codec="h264"):
    return {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": codec,
                "width": 1920,
                "height": 1080,
                "avg_frame_rate": "30000/1001",
            },
            {"codec_type": "audio", "codec_name": "aac"},
        ],
        "format": {"duration": str(duration), "format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
    }


def _upstream_blocker(stage="post_render", artifact="assembly_qc.json"):
    finding = qc.build_finding(
        finding_id=f"{artifact}-blocker",
        stage=stage,
        severity="blocker",
        confidence="objective",
        sample_policy="deterministic",
        category="schema_invalid",
        code="bad_upstream",
        message="upstream deterministic failure",
        deterministic=True,
        source={"artifact": artifact},
        evidence={"detail": "bad"},
    )
    return qc.build_report(
        artifact="final_qc.json", stage=stage, findings=[finding]
    ) | {"artifact": artifact}


def test_tail_decode_flags_container_valid_but_undecodable_stream(tmp_path):
    """Regression: header probing alone passes a container-valid but media-truncated/corrupt
    render (moov intact + mdat cut). The tail-decode check must flag it as a blocker."""
    out = tmp_path / "output.mp4"
    out.write_bytes(
        b"\x00" * 4096
    )  # exists + non-empty so we reach the probe/decode branch
    report = final_qc.build_final_qc(
        tmp_path,
        final_output="output.mp4",
        probe_runner=lambda p: _probe(),  # header probe passes cleanly
        decode_runner=lambda p: (False, "Invalid NAL unit size (505 > 107)"),
    )
    assert report["ok"] is False
    assert any(f["code"] == "undecodable_stream" for f in report["findings"])


def test_tail_decode_skip_does_not_false_block(tmp_path):
    """When ffmpeg is unavailable the decode check returns None and must NOT create a blocker."""
    out = tmp_path / "output.mp4"
    out.write_bytes(b"\x00" * 4096)
    report = final_qc.build_final_qc(
        tmp_path,
        final_output="output.mp4",
        probe_runner=lambda p: _probe(),
        decode_runner=lambda p: (None, "ffmpeg unavailable"),
    )
    assert report["ok"] is True
    assert report["blocker_count"] == 0
    assert not any(f["code"] == "undecodable_stream" for f in report["findings"])


def test_missing_and_empty_final_output_are_valid_blockers(tmp_path):
    missing = tmp_path / "missing.mp4"
    report = final_qc.build_final_qc(
        tmp_path,
        final_output=missing,
        probe_runner=lambda p: (_ for _ in ()).throw(AssertionError("no probe")),
    )

    assert report["ok"] is False
    assert report["artifact"] == "final_qc.json"
    assert report["stage"] == "post_render"
    assert report["findings"][0]["code"] == "missing_final_output"
    assert qc.validate_report(report) is True

    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    report = final_qc.build_final_qc(
        tmp_path,
        final_output=empty,
        probe_runner=lambda p: (_ for _ in ()).throw(AssertionError("no probe")),
    )

    assert report["ok"] is False
    assert any(f["code"] == "empty_final_output" for f in report["findings"])
    assert qc.validate_report(report) is True


def test_probe_fixture_success_writes_valid_final_qc_and_passing_golden_eval(tmp_path):
    output = tmp_path / "recap.mp4"
    output.write_bytes(b"fake mp4 bytes")
    _write_json(tmp_path / "assembly_manifest.json", {"final_output": str(output)})
    probe_fixture = tmp_path / "probe.json"
    _write_json(probe_fixture, _probe(duration=9.5, codec="h264"))
    golden_fixture = tmp_path / "golden.json"
    _write_json(
        golden_fixture,
        {
            "expected_final_qc_ok": True,
            "min_duration": 9.0,
            "max_duration": 10.0,
            "expected_codec": "h264",
            "required_artifacts": ["assembly_manifest.json"],
        },
    )

    summary = final_qc.run(
        tmp_path,
        final_output=output,
        probe_fixture=probe_fixture,
        golden_fixture=golden_fixture,
    )
    final_report = json.loads((tmp_path / "final_qc.json").read_text(encoding="utf-8"))
    golden_report = json.loads(
        (tmp_path / "golden_eval.json").read_text(encoding="utf-8")
    )

    assert summary["final_qc"] == {"ok": True, "blocker_count": 0}
    assert summary["golden_eval"] == {"ok": True, "blocker_count": 0}
    assert qc.validate_report(final_report) is True
    assert qc.validate_report(golden_report) is True
    assert final_report["metadata"]["probe"]["format"]["duration"] == "9.5"
    assert golden_report["metadata"]["observed"]["codec"] == "h264"


def test_probe_fixture_missing_objective_media_metadata_are_valid_blockers(tmp_path):
    output = tmp_path / "recap.mp4"
    output.write_bytes(b"fake mp4 bytes")

    report = final_qc.build_final_qc(
        tmp_path,
        final_output=output,
        probe_fixture={
            "streams": [{"codec_type": "audio", "codec_name": "aac"}],
            "format": {},
        },
    )

    codes = {f["code"] for f in report["findings"]}
    assert {
        "missing_video_stream",
        "missing_duration",
        "missing_codec",
        "missing_fps",
    } <= codes
    categories = {f["code"]: f["category"] for f in report["findings"]}
    assert categories["missing_video_stream"] == "stream"
    assert categories["missing_duration"] == "duration"
    assert categories["missing_codec"] == "stream"
    assert categories["missing_fps"] == "stream"
    assert all(
        f["deterministic"] is True and f["blocking"] is True for f in report["findings"]
    )
    assert qc.validate_report(report) is True


def test_probe_fixture_accepts_numeric_and_rational_video_fps(tmp_path):
    output = tmp_path / "recap.mp4"
    output.write_bytes(b"fake mp4 bytes")

    rational = final_qc.build_final_qc(
        tmp_path, final_output=output, probe_fixture=_probe()
    )
    numeric = final_qc.build_final_qc(
        tmp_path,
        final_output=output,
        probe_fixture={
            "streams": [{"codec_type": "video", "codec_name": "h264", "fps": 29.97}],
            "format": {"duration": "1.5"},
        },
    )

    assert rational["ok"] is True
    assert numeric["ok"] is True
    assert qc.validate_report(rational) is True
    assert qc.validate_report(numeric) is True


def test_probe_failure_on_existing_nonempty_mp4_is_deterministic_blocker(tmp_path):
    output = tmp_path / "recap.mp4"
    output.write_bytes(b"fake mp4 bytes")

    report = final_qc.build_final_qc(
        tmp_path,
        final_output=output,
        probe_runner=lambda p: (_ for _ in ()).throw(RuntimeError("ffprobe boom")),
    )

    assert report["ok"] is False
    assert any(f["code"] == "probe_failed" for f in report["findings"])
    assert qc.validate_report(report) is True


def test_assembly_and_visual_qc_blocking_are_rolled_into_deterministic_blockers(
    tmp_path,
):
    output = tmp_path / "recap.mp4"
    output.write_bytes(b"fake mp4 bytes")
    _write_json(
        tmp_path / "assembly_qc.json", _upstream_blocker(artifact="assembly_qc.json")
    )
    _write_json(
        tmp_path / "visual_qc.json", _upstream_blocker(artifact="visual_qc.json")
    )

    report = final_qc.build_final_qc(
        tmp_path, final_output=output, probe_fixture=_probe()
    )

    codes = {f["code"] for f in report["findings"]}
    assert any(code.startswith("upstream_assembly_qc_json") for code in codes)
    assert any(code.startswith("upstream_visual_qc_json") for code in codes)
    assert report["blocker_count"] == 2
    assert all(
        f["deterministic"] is True and f["blocking"] is True for f in report["findings"]
    )
    assert qc.validate_report(report) is True


def test_assembly_and_visual_qc_artifact_verdict_blocking_codes_are_blockers(tmp_path):
    output = tmp_path / "recap.mp4"
    output.write_bytes(b"fake mp4 bytes")
    _write_json(
        tmp_path / "assembly_qc.json",
        {
            "artifact": "assembly_qc.json",
            "verdict": "pass",
            "blocking": True,
            "blocking_codes": ["missing_scene", "bad_audio_mux"],
        },
    )
    _write_json(
        tmp_path / "visual_qc.json",
        {
            "artifact": "visual_qc.json",
            "verdict": "failed",
            "blocking": False,
            "blocking_codes": "black_frames",
        },
    )

    report = final_qc.build_final_qc(
        tmp_path, final_output=output, probe_fixture=_probe()
    )

    codes = [f["code"] for f in report["findings"]]
    assert codes == [
        "upstream_assembly_qc_json_missing_scene",
        "upstream_assembly_qc_json_bad_audio_mux",
        "upstream_visual_qc_json_black_frames",
    ]
    assert report["blocker_count"] == 3
    assert all(
        f["deterministic"] is True and f["blocking"] is True for f in report["findings"]
    )
    assert qc.validate_report(report) is True


def test_golden_fixture_mismatch_blocker(tmp_path):
    output = tmp_path / "recap.mp4"
    output.write_bytes(b"fake mp4 bytes")
    final_report = final_qc.build_final_qc(
        tmp_path, final_output=output, probe_fixture=_probe(duration=5.0, codec="h264")
    )

    golden = final_qc.build_golden_eval(
        tmp_path,
        final_qc_report=final_report,
        golden_fixture={
            "expected_final_qc_ok": True,
            "min_duration": 8.0,
            "expected_codec": "hevc",
        },
    )

    codes = {f["code"] for f in golden["findings"]}
    assert {"min_duration_mismatch", "codec_mismatch"} <= codes
    assert golden["ok"] is False
    assert qc.validate_report(golden) is True


def test_recap_post_render_helper_calls_final_qc_run(monkeypatch, tmp_path):
    seen = {}

    def fake_run(work_dir, final_output=None):
        seen["work_dir"] = Path(work_dir)
        seen["final_output"] = Path(final_output)
        return {"written": ["final_qc.json", "golden_eval.json"]}

    monkeypatch.setattr(recap.final_qc, "run", fake_run)
    result = recap._write_final_qc_reports(tmp_path, tmp_path / "fake.mp4")

    assert result["written"] == ["final_qc.json", "golden_eval.json"]
    assert seen == {"work_dir": tmp_path, "final_output": tmp_path / "fake.mp4"}


def test_no_secret_persistence_in_final_qc_or_golden_eval(tmp_path):
    output = tmp_path / "recap.mp4"
    output.write_bytes(b"fake mp4 bytes")
    _write_json(
        tmp_path / "assembly_manifest.json",
        {"final_output": str(output), "api_key": "sk-manifest-secret"},
    )
    _write_json(tmp_path / "mimo_qc.json", {"ok": True, "token": "tp-mimo-secret"})

    final_qc.run(
        tmp_path,
        final_output=output,
        probe_fixture={
            "format": {"duration": "3.0", "secret": "sk-probe-secret"},
            "streams": [
                {"codec_type": "video", "codec_name": "h264", "avg_frame_rate": "30/1"}
            ],
        },
        golden_fixture={
            "expected_final_qc_ok": True,
            "required_artifacts": ["assembly_manifest.json"],
            "password": "tp-golden-secret",
        },
    )
    text = (tmp_path / "final_qc.json").read_text(encoding="utf-8") + (
        tmp_path / "golden_eval.json"
    ).read_text(encoding="utf-8")

    assert "sk-manifest-secret" not in text
    assert "tp-mimo-secret" not in text
    assert "sk-probe-secret" not in text
    assert "tp-golden-secret" not in text
    assert "<redacted>" in text
    assert (
        qc.validate_report(
            json.loads((tmp_path / "final_qc.json").read_text(encoding="utf-8"))
        )
        is True
    )
    assert (
        qc.validate_report(
            json.loads((tmp_path / "golden_eval.json").read_text(encoding="utf-8"))
        )
        is True
    )


def test_recap_shift_left_redacts_direct_tts_and_assembly_metadata(tmp_path):
    output = tmp_path / "recap.mp4"
    _write_json(
        tmp_path / "tts_meta.json",
        {
            "segments": [{"index": 0, "audio_path": "tts_segments/narr_000.wav"}],
            "api_key": "plain-tts-password",
            "note": "synthetic sk-tts-secret",
        },
    )
    _write_json(
        tmp_path / "assembly_manifest.json",
        {
            "final_output": str(output),
            "callback": "https://user:pass@example.test/render?token=tp-assembly-secret#frag",
            "credential": "plain-assembly-password",
        },
    )

    recap._write_shift_left_stage_qc(
        tmp_path,
        "pre_tts",
        metadata={
            "direct": "sk-direct-secret",
            "secret": "plain-direct-password",
        },
    )
    recap._write_shift_left_stage_qc(
        tmp_path, "post_tts", metadata=recap._tts_qc_metadata(tmp_path)
    )
    recap._write_shift_left_stage_qc(
        tmp_path,
        "post_render",
        metadata=recap._post_render_qc_metadata(tmp_path, output),
    )
    text = (tmp_path / "preflight_qc.json").read_text(encoding="utf-8")
    report = json.loads(text)

    assert "sk-direct-secret" not in text
    assert "plain-direct-password" not in text
    assert "plain-tts-password" not in text
    assert "sk-tts-secret" not in text
    assert "user:pass" not in text
    assert "tp-assembly-secret" not in text
    assert "plain-assembly-password" not in text
    assert "https://example.test/render" in text
    assert qc.validate_report(report) is True
