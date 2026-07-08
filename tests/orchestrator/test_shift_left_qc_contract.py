import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "video-recap" / "scripts"))

import qc_contract as qc  # noqa: E402


def _deterministic_blocker(**overrides):
    data = {
        "id": "f1",
        "stage": "post_render",
        "severity": "blocker",
        "confidence": "objective",
        "sample_policy": "deterministic",
        "category": "missing_artifact",
        "code": "missing_final_mp4",
        "message": "final mp4 is missing",
        "source": {"artifact": "output.mp4"},
        "location": {"timecode": 12.3, "source_span": [10.0, 14.0]},
        "evidence": {"path": "output.mp4"},
    }
    data.update(overrides)
    return qc.build_finding(**data)


def test_required_fields_and_value_domains():
    finding = qc.build_finding(
        id="adv1",
        stage="post_tts",
        severity="advisory",
        confidence="medium",
        sample_policy="semantic",
        category="semantic",
        code="weak_hook",
        message="hook may be weak",
        deterministic=False,
        source={"artifact": "mimo_qc.json"},
        location={"timecode": 1.0, "source_span": [0.0, 2.0]},
        evidence={"note": "model critique"},
    )
    assert set(qc._REQUIRED_FINDING_FIELDS) <= set(finding)
    report = qc.build_report(artifact="mimo_qc.json", stage="post_tts", findings=[finding])
    assert report["schema_version"] == 1
    assert report["artifact"] in qc.SUPPORTED_ARTIFACTS
    assert qc.validate_report(report) is True

    with pytest.raises(qc.QCContractError, match="unsupported severity"):
        qc.build_finding(**{**finding, "severity": "fatal"})


def test_deterministic_blocking_passes():
    finding = _deterministic_blocker()
    report = qc.build_report(artifact="final_qc.json", stage="post_render", findings=[finding])

    assert report["ok"] is False
    assert report["blocker_count"] == 1
    assert qc.validate_report(report) is True


def test_mimo_semantic_default_advisory_non_blocking():
    finding = qc.build_finding(
        id="m1",
        stage="post_tts",
        severity="advisory",
        confidence="low",
        sample_policy="semantic",
        category="mimo_semantic",
        code="pacing_flat",
        message="semantic pacing concern",
        deterministic=False,
        source={"artifact": "mimo_qc.json"},
        evidence={"summary": "subjective review"},
    )
    report = qc.build_report(artifact="mimo_qc.json", stage="post_tts", findings=[finding])

    assert finding["blocking"] is False
    assert report["ok"] is True
    assert report["blocker_count"] == 0


def test_non_deterministic_blocking_without_rule_table_allowance_rejected():
    with pytest.raises(qc.QCContractError, match="non-deterministic blocking"):
        qc.build_finding(
            id="m2",
            stage="post_tts",
            severity="blocker",
            confidence="medium",
            sample_policy="semantic",
            category="semantic",
            code="bad_taste",
            message="subjective issue tries to block",
            deterministic=False,
            blocking=True,
            source={"artifact": "mimo_qc.json"},
            evidence={"summary": "subjective"},
        )


def test_non_deterministic_blocking_with_rule_table_requires_corroboration():
    rules = {
        "schema_version": qc.SCHEMA_VERSION,
        "non_deterministic_blocking_allow": [{"category": "semantic", "code": "claim_contradiction"}],
    }
    with pytest.raises(qc.QCContractError, match="objective_corroboration"):
        qc.build_finding(
            id="m3",
            stage="post_tts",
            severity="blocker",
            confidence="high",
            sample_policy="semantic",
            category="semantic",
            code="claim_contradiction",
            message="claim appears contradicted",
            deterministic=False,
            blocking=True,
            source={"artifact": "mimo_qc.json"},
            evidence={"summary": "model found contradiction"},
            rule_table=rules,
        )

    finding = qc.build_finding(
        id="m4",
        stage="post_tts",
        severity="blocker",
        confidence="high",
        sample_policy="semantic",
        category="semantic",
        code="claim_contradiction",
        message="claim is objectively contradicted by subtitle span",
        deterministic=False,
        blocking=True,
        source={"artifact": "mimo_qc.json"},
        evidence={"summary": "model found contradiction"},
        objective_corroboration={"type": "subtitle_span", "evidence": "asr_result[3]"},
        rule_table=rules,
    )
    assert finding["blocking"] is True
    assert qc.build_report(artifact="mimo_qc.json", stage="post_tts", findings=[finding], rule_table=rules)["ok"] is False


def test_non_deterministic_blocking_rule_table_requires_matching_schema_version():
    base = dict(
        id="m5",
        stage="post_tts",
        severity="blocker",
        confidence="high",
        sample_policy="semantic",
        category="semantic",
        code="claim_contradiction",
        message="claim is objectively contradicted by subtitle span",
        deterministic=False,
        blocking=True,
        source={"artifact": "mimo_qc.json"},
        evidence={"summary": "model found contradiction"},
        objective_corroboration={"type": "subtitle_span", "evidence": "asr_result[3]"},
    )
    for rules in (
        {"non_deterministic_blocking_allow": [{"category": "semantic", "code": "claim_contradiction"}]},
        {"schema_version": qc.SCHEMA_VERSION + 1, "non_deterministic_blocking_allow": [{"category": "semantic", "code": "claim_contradiction"}]},
    ):
        with pytest.raises(qc.QCContractError, match="non-deterministic blocking"):
            qc.build_finding(**base, rule_table=rules)


def test_qc_contract_redacts_metadata_keys_values_and_urls():
    report = qc.build_report(
        artifact="preflight_qc.json",
        stage="pre_tts",
        findings=[],
        metadata={
            "api_key": "plain-password-value",
            "note": "token sk-synthetic-secret should be hidden",
            "endpoint": "https://user:pass@example.test/path/to/judge?token=tp-url-secret#frag",
        },
    )
    text = json.dumps(report, ensure_ascii=False)

    assert "plain-password-value" not in text
    assert "sk-synthetic-secret" not in text
    assert "user:pass" not in text
    assert "tp-url-secret" not in text
    assert "https://example.test/path/to/judge" in text
    assert report["metadata"]["api_key"] == qc.REDACTED


def test_artifact_fingerprint_stable(tmp_path):
    artifact = tmp_path / "final_qc.json"
    artifact.write_text(json.dumps({"ok": True}, sort_keys=True), encoding="utf-8")

    first = qc.artifact_fingerprint(artifact)
    second = qc.artifact_fingerprint(artifact)

    assert first == second
    assert len(first) == 64


def test_pre_cut_null_timecode_source_span_valid():
    finding = qc.build_finding(
        id="pc1",
        stage="pre_cut",
        severity="blocker",
        confidence="objective",
        sample_policy="deterministic",
        category="schema_invalid",
        code="missing_clip_plan",
        message="clip plan missing before time mapping exists",
        deterministic=True,
        source={"artifact": "clip_plan.json"},
        location={"timecode": None, "source_span": None},
        evidence={"path": "clip_plan.json"},
    )

    assert finding["location"] == {"timecode": None, "source_span": None}
    assert qc.validate_report(qc.build_report(artifact="golden_eval.json", stage="pre_cut", findings=[finding])) is True


def test_unknown_schema_version_and_missing_required_fields_validation_errors():
    report = qc.build_report(artifact="final_qc.json", stage="golden", findings=[])

    with pytest.raises(qc.QCContractError, match="unsupported schema_version"):
        qc.validate_report({**report, "schema_version": 999})

    missing_artifact = dict(report)
    missing_artifact.pop("artifact")
    with pytest.raises(qc.QCContractError, match="missing required fields"):
        qc.validate_report(missing_artifact)

    bad_finding = _deterministic_blocker()
    bad_finding.pop("location")
    with pytest.raises(qc.QCContractError, match="finding missing required fields"):
        qc.validate_report({**report, "findings": [bad_finding], "finding_count": 1, "blocker_count": 1, "ok": False})


def test_load_rule_table_default_has_no_blocking_nondeterministic_allowances():
    table = qc.load_rule_table()
    assert table["schema_version"] == 1
    assert table["non_deterministic_blocking_allow"] == []


def test_stage_names_match_approved_gate_matrix_exactly():
    assert qc.STAGES == frozenset({
        "pre_cut",
        "post_cut",
        "pre_tts",
        "post_tts",
        "pre_assemble",
        "post_render",
        "golden",
    })
    for old_stage in (
        "pre_voiceover",
        "post_voiceover",
        "pre_export",
        "post_export",
        "final",
        "golden_eval",
        "mimo_qc",
    ):
        with pytest.raises(qc.QCContractError, match="unsupported stage"):
            qc.build_report(artifact="final_qc.json", stage=old_stage, findings=[])


def test_sample_policy_is_object_with_valid_type():
    finding = _deterministic_blocker(sample_policy={"type": "deterministic", "basis": "full_scan"})
    assert finding["sample_policy"]["type"] == "deterministic"

    with pytest.raises(qc.QCContractError, match="unsupported sample_policy.type"):
        _deterministic_blocker(sample_policy={"type": "random_guess"})

    bad = _deterministic_blocker()
    bad["sample_policy"] = "deterministic"
    with pytest.raises(qc.QCContractError, match="sample_policy must be an object"):
        qc.validate_report(qc.build_report(artifact="final_qc.json", stage="post_render", findings=[])
                           | {"findings": [bad], "finding_count": 1, "blocker_count": 1, "ok": False})


def test_canonical_required_fields_include_runtime_decision_fields():
    finding = _deterministic_blocker(
        finding_id="canonical-1",
        model_used="local_rules_v1",
        artifact_fingerprints={"output.mp4": "0" * 64},
        next_action="regenerate_output",
    )
    assert finding["finding_id"] == "canonical-1"
    assert finding["id"] == "canonical-1"  # backward-compatible alias only
    assert finding["rule_id"] == "missing_final_mp4"
    assert finding["decision_reason"] == "final mp4 is missing"
    assert finding["model_used"] == "local_rules_v1"
    assert finding["artifact_fingerprints"] == {"output.mp4": "0" * 64}
    assert finding["next_action"] == "regenerate_output"

    for required in ("model_used", "artifact_fingerprints", "next_action"):
        bad = dict(finding)
        bad.pop(required)
        with pytest.raises(qc.QCContractError, match="finding missing required fields"):
            qc.validate_report(qc.build_report(artifact="final_qc.json", stage="post_render", findings=[])
                               | {"findings": [bad], "finding_count": 1, "blocker_count": 1, "ok": False})


def test_mimo_qc_artifact_attaches_to_actual_stage_not_stage_value():
    finding = qc.build_finding(
        finding_id="mimo-pre-assemble",
        stage="pre_assemble",
        severity="advisory",
        confidence="medium",
        sample_policy={"type": "semantic"},
        category="mimo_semantic",
        code="weak_transition",
        message="model noted a weak transition",
        deterministic=False,
        source={"artifact": "mimo_qc.json"},
        evidence={"summary": "subjective model review"},
        model_used="mimo-qc-offline-fixture",
        artifact_fingerprints={"mimo_qc.json": "fixture"},
        next_action="human_review",
    )

    assert qc.validate_report(qc.build_report(artifact="mimo_qc.json", stage="pre_assemble", findings=[finding])) is True
    with pytest.raises(qc.QCContractError, match="unsupported stage"):
        qc.build_report(artifact="mimo_qc.json", stage="mimo_qc", findings=[finding])

import recap  # noqa: E402


def test_preflight_qc_artifact_supported_without_changing_existing_artifacts():
    assert "preflight_qc.json" in qc.SUPPORTED_ARTIFACTS
    assert {"final_qc.json", "golden_eval.json", "mimo_qc.json"} <= qc.SUPPORTED_ARTIFACTS
    report = qc.build_report(artifact="preflight_qc.json", stage="pre_tts", findings=[])
    assert report["ok"] is True
    assert qc.validate_report(report) is True


def test_shift_left_helper_rolls_up_latest_report_per_stage(tmp_path):
    finding = _deterministic_blocker(finding_id="post-render-blocker")

    recap._write_shift_left_stage_qc(tmp_path, "pre_tts", metadata={"review_ran": True}, findings=[])
    recap._write_shift_left_stage_qc(tmp_path, "post_tts", metadata={"tts_meta": {"segments": []}}, findings=[])
    report = recap._write_shift_left_stage_qc(
        tmp_path,
        "post_render",
        metadata={"final_output": tmp_path / "recap.mp4"},
        findings=[finding],
    )

    path_report = json.loads((tmp_path / "preflight_qc.json").read_text(encoding="utf-8"))
    assert path_report == report
    assert qc.validate_report(path_report) is True
    assert path_report["artifact"] == "preflight_qc.json"
    assert path_report["stage"] == "post_render"
    assert path_report["ok"] is False
    assert path_report["blocker_count"] == 1
    assert [f["finding_id"] for f in path_report["findings"]] == ["post-render-blocker"]
    stages = path_report["metadata"]["stages"]
    assert set(stages) == {"pre_tts", "post_tts", "post_render"}
    assert stages["pre_tts"]["metadata"] == {"review_ran": True}
    assert stages["post_tts"]["metadata"] == {"tts_meta": {"segments": []}}
    assert stages["post_render"]["metadata"] == {"final_output": str(tmp_path / "recap.mp4")}
    for stage_report in stages.values():
        assert qc.validate_report(stage_report) is True


def test_recap_full_mode_writes_shift_left_preflight_stages(monkeypatch, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    work = tmp_path / "work"
    work.mkdir()
    (work / "narration.json").write_text(
        json.dumps([{"start": 0, "end": 1, "narration": "full。"}]),
        encoding="utf-8",
    )
    recap._write_run_manifest(work, video.resolve(), recap.argparse.Namespace(
        context="",
        scene_threshold=None,
        style="纪录片",
        edit_mode="full",
        target_duration=None,
        skip_asr=False,
        mimo_video_overview=False,
        consolidate=True,
        consolidate_asr=False,
        review_narration=None,
        require_narration_review=False,
        allow_duration_drift=False,
        allow_sparse_cut=False,
    ))

    calls = []

    def fake_run(skill, script, *cli_args):
        calls.append((skill, script, [str(arg) for arg in cli_args]))
        if script == "voiceover.py":
            (work / "tts_segments").mkdir(exist_ok=True)
            (work / "tts_segments" / "narr_000.wav").write_bytes(b"wav")
            (work / "tts_meta.json").write_text(
                json.dumps({"segments": [{"index": 0, "audio_path": "tts_segments/narr_000.wav"}]}),
                encoding="utf-8",
            )
        if script == "assemble.py":
            (work / "output.mp4").write_bytes(b"mp4")
            (work / "assembly_manifest.json").write_text(
                json.dumps({"final_output": str(tmp_path / "recap_video.mp4")}),
                encoding="utf-8",
            )

    monkeypatch.setattr("recap._run", fake_run)
    monkeypatch.setattr("recap._preflight_burn_subtitles", lambda args: None)
    monkeypatch.setattr(sys, "argv", ["recap.py", str(video), "--work-dir", str(work)])

    recap.main()

    report = json.loads((work / "preflight_qc.json").read_text(encoding="utf-8"))
    assert qc.validate_report(report) is True
    assert report["stage"] == "post_render"
    stages = report["metadata"]["stages"]
    assert list(stages) == ["pre_tts", "post_tts", "pre_assemble", "post_render"]
    assert stages["post_tts"]["metadata"]["tts_meta"]["segments"][0]["index"] == 0
    assert stages["post_tts"]["metadata"]["tts_segments"] == ["tts_segments/narr_000.wav"]
    assert stages["post_render"]["metadata"]["final_output"] == str(tmp_path / "recap_video.mp4")
    assert calls[0][:2] == ("video-script", "validate.py")
