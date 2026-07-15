import json
import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "skills" / "video-recap" / "scripts")
)

import mimo_qc  # noqa: E402
import mimo_qc_report  # noqa: E402
import qc_contract as qc  # noqa: E402


def _write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_collects_lightweight_evidence_prefers_validated_plan(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    _write_json(work / "narration.json", [{"start": 0, "end": 1, "narration": "hello"}])
    _write_json(work / "visual_overlays.json", {"overlays": [{"text": "title"}]})
    _write_json(
        work / "clip_plan.json",
        [{"start": 0, "end": 2, "secret_token": "tp-should-redact"}],
    )
    _write_json(
        work / "clip_plan_validated.json",
        {"clips": [{"source_start": 0, "source_end": 2}]},
    )
    _write_json(work / "assembly_manifest.json", {"final_output": "output.mp4"})
    _write_json(work / "tts_meta.json", {"segments": [{"index": 0}]})
    _write_json(work / "storyboard.json", {"frames": ["f1.jpg"]})
    (work / "subtitles.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8"
    )
    (work / "output.mp4").write_bytes(b"mp4")

    evidence = mimo_qc.collect_evidence(work)

    assert "narration.json" in evidence["artifacts"]
    assert "visual_overlays.json" in evidence["artifacts"]
    assert "clip_plan_validated.json" in evidence["artifacts"]
    assert "clip_plan.json" not in evidence["artifacts"]
    assert "assembly_manifest.json" in evidence["artifacts"]
    assert "tts_meta.json" in evidence["artifacts"]
    assert "subtitles.srt" in evidence["generated_subtitles"]
    assert "storyboard.json" in evidence["visual_metadata"]
    assert evidence["final_output"]["candidates"][0]["exists"] is True
    assert "tp-should-redact" not in json.dumps(evidence)
    assert len(evidence["fingerprint"]) == 64


def test_payload_preserves_semantic_values_and_only_relevant_final_output(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    _write_json(
        work / "narration.json",
        [
            {
                "start": 0,
                "end": 8,
                "narration": "詹姆斯从青涩状元成长为联盟传奇。",
            }
        ],
    )
    _write_json(
        work / "tts_meta.json",
        {
            "segments": [{"index": 0, "audio_duration": 5.6}],
            "partial": False,
            "failures": [],
        },
    )
    _write_json(
        work / "asr_result.json",
        [
            {
                "start": 0,
                "end": 15,
                "text": "带你重走詹姆斯的二十一年。",
            }
        ],
    )
    _write_json(
        work / "storyboard.json",
        {
            "timeline": "source",
            "labels_burned": True,
        },
    )
    (work / "subtitles.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n生成解说字幕\n", encoding="utf-8"
    )
    (work / "output.mp4").write_bytes(b"mp4")

    evidence = mimo_qc.collect_evidence(work)
    pre = mimo_qc.build_payload(evidence, stage="pre_assemble")

    narration = pre["evidence"]["artifacts"]["narration.json"]["summary"]
    tts = pre["evidence"]["artifacts"]["tts_meta.json"]["summary"]
    asr = pre["evidence"]["source_asr"]["asr_result.json"]["summary"]
    assert narration["items"][0]["narration"] == "詹姆斯从青涩状元成长为联盟传奇。"
    assert tts["partial"] is False
    assert tts["failures"]["count"] == 0
    assert asr["items"][0]["text"] == "带你重走詹姆斯的二十一年。"
    assert "final_output" not in pre["evidence"]
    assert (
        "not a transcript of generated TTS"
        in pre["evidence"]["evidence_roles"]["source_asr"]
    )
    assert "generated_subtitles" not in pre["evidence"]
    assert "diagnostic" in pre["evidence"]["evidence_roles"]["visual_metadata"]
    assert "post_render_frame_limits" not in pre["evidence"]["evidence_roles"]
    assert "actual_audio_timing" not in pre["evidence"]["evidence_roles"]
    assert (
        "deliberate original-audio blocks"
        in pre["evidence"]["evidence_roles"]["narration_and_subtitle_gaps"]
    )

    post = mimo_qc.build_payload(evidence, stage="post_render")
    assert "subtitles.srt" in post["evidence"]["generated_subtitles"]
    assert "not source ASR" in post["evidence"]["evidence_roles"]["generated_subtitles"]
    assert (
        "black source-subtitle mask band"
        in post["evidence"]["evidence_roles"]["generated_subtitles"]
    )
    assert "逐字对照 generated_subtitles" in post["instructions"]
    assert "opacity=1.0" in post["instructions"]
    candidates = post["evidence"]["final_output"]["candidates"]
    assert len(candidates) == 1
    assert candidates[0]["path"] == "output.mp4"
    assert candidates[0]["exists"] is True
    assert (
        "silent still images"
        in post["evidence"]["evidence_roles"]["post_render_frame_limits"]
    )
    assert (
        "actual_place_start/actual_place_end"
        in post["evidence"]["evidence_roles"]["actual_audio_timing"]
    )


def test_multi_source_evidence_collects_per_source_asr_instead_of_stale_subtitles(
    tmp_path,
):
    work = tmp_path / "work"
    work.mkdir()
    sources = []
    for source_id, text in (("src_a", "选秀夜原声"), ("src_b", "天王山原声")):
        relative = f"sources/{source_id}"
        source_dir = work / relative
        source_dir.mkdir(parents=True)
        _write_json(
            source_dir / "asr_clean.json",
            {"segments": [{"start": 0, "end": 2, "text": text}]},
        )
        sources.append({"source_id": source_id, "source_work_dir": relative})
    _write_json(
        work / "multi_source_manifest.json", {"schema_version": 1, "sources": sources}
    )
    (work / "subtitles.srt").write_text("上一版生成字幕", encoding="utf-8")

    evidence = mimo_qc.collect_evidence(work)
    pre = mimo_qc.build_payload(evidence, stage="pre_assemble")

    assert set(pre["evidence"]["source_asr"]) == {"src_a", "src_b"}
    assert "选秀夜原声" in json.dumps(pre["evidence"]["source_asr"], ensure_ascii=False)
    assert "天王山原声" in json.dumps(pre["evidence"]["source_asr"], ensure_ascii=False)
    assert "generated_subtitles" not in pre["evidence"]


def test_post_qc_drops_source_caption_claim_when_visible_text_is_generated_cue(
    tmp_path,
):
    work = tmp_path / "work"
    work.mkdir()
    (work / "subtitles.srt").write_text(
        "1\n00:00:01,000 --> 00:00:05,000\n从成熟的面孔切回选秀夜，\n", encoding="utf-8"
    )
    payload = mimo_qc.build_payload(mimo_qc.collect_evidence(work), stage="post_render")
    findings = mimo_qc.normalize_observations(
        {
            "observations": [
                {
                    "code": "SOURCE_SUBTITLE_VISIBLE",
                    "message": "不透明遮罩后仍能看到源字幕。",
                    "evidence": {"visible_text": "从成熟的面孔切回选秀夜，"},
                }
            ]
        },
        stage="post_render",
        payload=payload,
    )

    assert findings == []


def test_fixture_normalizes_to_advisory_nonblocking_valid_report(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    _write_json(work / "narration.json", [{"narration": "hook"}])
    fixture = {
        "observations": [
            {
                "code": "weak_hook",
                "message": "Opening hook may be too flat.",
                "category": "semantic",
                "confidence": "medium",
                "sample_policy": "semantic",
                "evidence": {"span": "narration[0]"},
            },
            {
                "code": "busy_overlay",
                "message": "Overlay looks crowded.",
                "category": "aesthetic",
                "confidence": "high",
                "sample_policy": "aesthetic",
            },
        ]
    }

    result = mimo_qc.run(
        work,
        fixture=fixture,
        config={"mimo_video_model": "mimo-test", "api_key": "sk-secret"},
    )
    report = result["report"]

    assert Path(result["path"]).name == "mimo_qc.json"
    assert qc.validate_report(report) is True
    assert report["artifact"] == "mimo_qc.json"
    assert report["stage"] == "pre_assemble"
    assert report["ok"] is True
    assert report["blocker_count"] == 0
    assert [f["severity"] for f in report["findings"]] == ["advisory", "advisory"]
    assert [f["blocking"] for f in report["findings"]] == [False, False]
    assert [f["deterministic"] for f in report["findings"]] == [False, False]
    assert {f["next_action"] for f in report["findings"]} == {"human_review"}
    assert {f["model_used"] for f in report["findings"]} == {"mimo-test"}
    assert report["findings"][1]["category"] == "mimo_aesthetic"
    assert report["metadata"]["pipeline_blocking"] is False
    assert report["metadata"]["auto_repair"] is False


def test_no_secret_persistence_in_report(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    _write_json(work / "narration.json", {"api_key": "sk-file-secret", "text": "safe"})
    fixture = {
        "observations": [
            {"message": "uses fixture", "evidence": {"token": "tp-fixture-secret"}}
        ]
    }

    result = mimo_qc.run(
        work,
        fixture=fixture,
        config={
            "api_key": "sk-config-secret",
            "mimo_api_key": "tp-config-secret",
            "mimo_video_api_key": "tp-video-secret",
            "mimo_video_model": "mimo-test",
        },
    )
    text = Path(result["path"]).read_text(encoding="utf-8")

    assert "sk-file-secret" not in text
    assert "tp-fixture-secret" not in text
    assert "sk-config-secret" not in text
    assert "tp-config-secret" not in text
    assert "tp-video-secret" not in text
    # key_present must survive redaction as a real boolean (keys ARE configured here);
    # the old "api_key_configured" name matched the secret-key regex and was blanked to "<redacted>".
    assert '"key_present": true' in text
    assert "api_key_configured" not in text
    assert qc.validate_report(json.loads(text)) is True


def test_safe_mimo_config_strips_url_credentials_query_and_fragment():
    cfg = mimo_qc.safe_mimo_config(
        {
            "mimo_api_url": "https://user:pass@mimo.example.test/v1/chat?api_key=sk-url-secret#frag",
            "mimo_video_api_url": "https://video_user:tp-url-password@video.example.test/v2/judge?token=tp-query-secret",
            "mimo_video_model": "mimo-test",
        }
    )
    text = json.dumps(cfg, ensure_ascii=False)

    assert "user:pass" not in text
    assert "video_user" not in text
    assert "tp-url-password" not in text
    assert "sk-url-secret" not in text
    assert "tp-query-secret" not in text
    assert "frag" not in text
    assert cfg["mimo_api_url"] == "https://mimo.example.test/v1/chat"
    assert cfg["mimo_video_api_url"] == "https://video.example.test/v2/judge"


def test_dry_run_writes_valid_evidence_only_report_with_caller_stage(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    _write_json(work / "tts_meta.json", {"segments": []})

    result = mimo_qc.run(work, stage="post_tts", dry_run=True)
    report = json.loads((work / "mimo_qc.json").read_text(encoding="utf-8"))

    assert result["report"]["finding_count"] == 0
    assert report["stage"] == "post_tts"
    assert report["ok"] is True
    assert report["metadata"]["mode"] == "dry_run"
    assert qc.validate_report(report) is True


def test_injected_judge_payload_and_report_validation(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    _write_json(work / "narration.json", [{"narration": "line"}])
    seen = {}

    def judge(payload):
        seen.update(payload)
        return {
            "observations": [
                {
                    "code": "sampled_frame_issue",
                    "message": "sampled concern",
                    "sample_policy": "sampled",
                }
            ]
        }

    result = mimo_qc.run(work, judge=judge, config={"mimo_video_model": "mimo-judge"})

    assert seen["artifact"] == "mimo_qc.json"
    assert "instructions" in seen
    assert len(seen["payload_fingerprint"]) == 64
    finding = result["report"]["findings"][0]
    assert finding["sample_policy"]["type"] == "sampled"
    assert finding["severity"] == "advisory"
    assert finding["blocking"] is False
    assert qc.validate_report(result["report"]) is True


def _api_response(observations):
    return {
        "choices": [
            {"message": {"content": json.dumps({"observations": observations})}}
        ]
    }


def test_live_call_is_one_request_per_stage_and_uses_cache_unless_refreshed(
    monkeypatch, tmp_path
):
    work = tmp_path / "absolute" / "work"
    work.mkdir(parents=True)
    _write_json(work / "narration.json", [{"narration": "line"}])
    calls = []

    def api_call(payload, *, config=None, timeout=60):
        calls.append(payload)
        return _api_response([{"code": "pace", "message": "Pacing may feel rushed."}])

    monkeypatch.setattr(mimo_qc, "mimo_qc_api_call", api_call)
    config = {"mimo_video_api_key": "sk-test-secret", "mimo_qc_model": "mimo-qc-test"}

    first = mimo_qc.run(work, live=True, config=config)
    second = mimo_qc.run(work, live=True, config=config)
    refreshed = mimo_qc.run(work, live=True, refresh=True, config=config)

    assert len(calls) == 2
    assert first["report"]["metadata"]["status"] == "completed"
    assert second["report"]["metadata"]["status"] == "cached"
    assert refreshed["report"]["metadata"]["status"] == "completed"
    assert first["report"]["metadata"]["request_count"] == 1
    assert second["report"]["metadata"]["request_count"] == 0
    assert refreshed["report"]["metadata"]["request_count"] == 1
    cache_key = first["report"]["metadata"]["cache_key"]
    assert len(cache_key) == 64
    assert str(work) not in json.dumps(first["report"]["metadata"]["cache_input"])


def test_live_missing_key_is_unavailable_and_replaces_stale_report(
    monkeypatch, tmp_path
):
    work = tmp_path / "work"
    work.mkdir()
    (work / "mimo_qc.json").write_text('{"stale": true}', encoding="utf-8")
    monkeypatch.setattr(
        mimo_qc,
        "mimo_qc_api_call",
        lambda *args, **kwargs: pytest.fail("missing-key mode must not call the API"),
    )

    result = mimo_qc.run(
        work,
        live=True,
        config={"mimo_video_api_key": "", "mimo_api_key": "", "api_key": ""},
    )

    assert result["report"]["metadata"]["status"] == "unavailable"
    assert result["report"]["finding_count"] == 0
    assert (
        qc.validate_report(
            json.loads((work / "mimo_qc.json").read_text(encoding="utf-8"))
        )
        is True
    )


@pytest.mark.parametrize("failure", ["http_401", "http_429", "timeout"])
def test_live_transport_failures_are_fail_open_reports(monkeypatch, tmp_path, failure):
    work = tmp_path / failure
    work.mkdir()

    def fail(*_args, **_kwargs):
        raise RuntimeError(failure)

    monkeypatch.setattr(mimo_qc, "mimo_qc_api_call", fail)
    result = mimo_qc.run(
        work, live=True, config={"mimo_video_api_key": "sk-test-secret"}
    )

    assert result["report"]["metadata"]["status"] == "failed"
    assert result["report"]["ok"] is True
    assert result["report"]["blocker_count"] == 0
    assert failure in result["report"]["metadata"]["error"]


def test_malformed_live_response_is_failed_not_a_subjective_finding(
    monkeypatch, tmp_path
):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(
        mimo_qc, "mimo_qc_api_call", lambda *_a, **_k: {"choices": []}
    )

    result = mimo_qc.run(
        work, live=True, config={"mimo_video_api_key": "sk-test-secret"}
    )

    assert result["report"]["metadata"]["status"] == "failed"
    assert result["report"]["findings"] == []


def test_post_render_sends_bounded_frames_but_never_persists_base64(
    monkeypatch, tmp_path
):
    work = tmp_path / "work"
    work.mkdir()
    video = work / "output.mp4"
    video.write_bytes(b"video")
    captured = {}
    samples = [
        {
            "data_url": "data:image/jpeg;base64,BASE64SECRET1",
            "sha256": "a" * 64,
            "timestamp": 5.125,
        },
        {
            "data_url": "data:image/jpeg;base64,BASE64SECRET2",
            "sha256": "b" * 64,
            "timestamp": 15.25,
        },
    ]

    def api_call(payload, *, config=None, timeout=60):
        captured.update(payload)
        return _api_response([])

    monkeypatch.setattr(mimo_qc, "mimo_qc_api_call", api_call)
    result = mimo_qc.run(
        work,
        stage="post_render",
        live=True,
        config={"mimo_video_api_key": "sk-test-secret"},
        final_output=video,
        frame_sampler=lambda *_a, **_k: samples,
    )

    request_text = json.dumps(captured)
    report_text = (work / "mimo_qc.json").read_text(encoding="utf-8")
    assert "BASE64SECRET1" in request_text
    assert "BEGIN QC_FRAME_1: final-output timestamp 5.125s" in request_text
    assert "END QC_FRAME_1: timestamp 5.125s" in request_text
    assert "BEGIN QC_FRAME_2: final-output timestamp 15.250s" in request_text
    assert "END QC_FRAME_2: timestamp 15.250s" in request_text
    assert "BASE64SECRET1" not in report_text
    assert result["report"]["metadata"]["frame_samples"]["count"] == 2


def test_cache_input_includes_prompt_payload_fingerprint():
    evidence = {
        "artifacts": {},
        "source_asr": {},
        "generated_subtitles": {},
        "visual_metadata": {},
        "final_output": {},
    }
    frames = {"count": 0, "samples": []}

    first = mimo_qc_report._cache_input(
        "post_render",
        {"model": "m", "payload_fingerprint": "prompt-v1"},
        evidence,
        frames,
    )
    second = mimo_qc_report._cache_input(
        "post_render",
        {"model": "m", "payload_fingerprint": "prompt-v2"},
        evidence,
        frames,
    )

    assert first["payload_fingerprint"] == "prompt-v1"
    assert first != second


def test_pre_and_post_reports_are_aggregated_without_overwriting_each_other(
    monkeypatch, tmp_path
):
    work = tmp_path / "work"
    work.mkdir()
    _write_json(work / "narration.json", [{"narration": "line"}])
    calls = []

    def api_call(payload, *, config=None, timeout=60):
        calls.append(payload)
        stage = (
            "post"
            if any(
                item.get("type") == "image_url"
                for item in payload["messages"][0]["content"]
                if isinstance(item, dict)
            )
            else "pre"
        )
        return _api_response([{"code": stage, "message": stage}])

    monkeypatch.setattr(mimo_qc, "mimo_qc_api_call", api_call)
    config = {"mimo_video_api_key": "sk-test-secret"}
    mimo_qc.run(work, stage="pre_assemble", live=True, config=config)
    mimo_qc.run(
        work,
        stage="post_render",
        live=True,
        config=config,
        final_output=work / "output.mp4",
        frame_sampler=lambda *_a, **_k: [
            {"data_url": "data:image/jpeg;base64,FRAME", "sha256": "c" * 64}
        ],
    )

    report = json.loads((work / "mimo_qc.json").read_text(encoding="utf-8"))
    assert len(calls) == 2
    assert list(report["metadata"]["stages"]) == ["pre_assemble", "post_render"]
    assert {finding["stage"] for finding in report["findings"]} == {
        "pre_assemble",
        "post_render",
    }
    assert all(
        qc.validate_report(stage_report)
        for stage_report in report["metadata"]["stages"].values()
    )


def test_clear_report_removes_stale_advisory_artifact(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    path = work / "mimo_qc.json"
    path.write_text("{}", encoding="utf-8")

    assert mimo_qc.clear_report(work) is True
    assert not path.exists()
    assert mimo_qc.clear_report(work) is False
