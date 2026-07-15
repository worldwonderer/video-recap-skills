import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "skills" / "video-script" / "scripts")
)
sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "skills" / "video-recap" / "scripts")
)
import json

import review
import recap_review


def _seed_work_dir(work_dir):
    (work_dir / "narration.json").write_text(
        json.dumps([{"start": 1, "end": 4, "narration": "测试。"}]), encoding="utf-8"
    )
    (work_dir / "vlm_analysis.json").write_text("[]", encoding="utf-8")
    (work_dir / "asr_result.json").write_text("[]", encoding="utf-8")


def _run_review_with_finding(monkeypatch, work_dir, finding):
    _seed_work_dir(work_dir)
    payloads = []

    def fake_api(payload):
        payloads.append(payload)
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "verdict": "REVISE",
                                "summary": "s",
                                "findings": [finding],
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr("review_runner.api_call", fake_api)
    review.review_narration(work_dir)
    return payloads


def test_review_payload_is_deterministic(monkeypatch, tmp_path):
    """Q3: re-running review on identical input must be deterministic — the payload
    pins temperature to 0 and carries a fixed integer seed."""
    payloads = _run_review_with_finding(
        monkeypatch,
        tmp_path,
        {
            "segment": 0,
            "severity": "warning",
            "category": "weak_hook",
            "issue": "i",
            "fix": "f",
        },
    )
    payload = payloads[0]
    assert payload["temperature"] == 0
    assert isinstance(payload["seed"], int)


def test_craft_error_is_clamped_and_does_not_gate(monkeypatch, tmp_path):
    """Q4: a craft finding (weak_hook) marked error is clamped to warning, so it does
    NOT count as a gating error in recap_review.review_result_status."""
    _run_review_with_finding(
        monkeypatch,
        tmp_path,
        {
            "segment": 0,
            "severity": "error",
            "category": "weak_hook",
            "issue": "开头平淡",
            "fix": "加悬念",
        },
    )

    review_json = json.loads(
        (tmp_path / "narration_review.json").read_text(encoding="utf-8")
    )
    assert review_json["findings"][0]["severity"] == "warning"

    status = recap_review.review_result_status(tmp_path)
    assert status["errors"] == 0
    assert status["ok"] is True


def test_hallucination_error_still_gates(monkeypatch, tmp_path):
    """Q4 counter-case: a factual finding (hallucination) marked error keeps its
    severity and DOES gate strict mode."""
    _run_review_with_finding(
        monkeypatch,
        tmp_path,
        {
            "segment": 0,
            "severity": "error",
            "category": "hallucination",
            "issue": "凭空虚构",
            "fix": "删掉",
        },
    )

    review_json = json.loads(
        (tmp_path / "narration_review.json").read_text(encoding="utf-8")
    )
    assert review_json["findings"][0]["severity"] == "error"

    status = recap_review.review_result_status(tmp_path)
    assert status["errors"] == 1
    assert status["ok"] is False


def test_parse_clamps_craft_error_directly():
    """parse_review_response clamps non-factual error findings; factual ones are kept."""
    r = review.parse_review_response(
        json.dumps(
            {
                "verdict": "REVISE",
                "findings": [
                    {"severity": "error", "category": "disjoint_handoff", "issue": "i"},
                    {"severity": "error", "category": "incomplete", "issue": "i"},
                    {"severity": "error", "category": "hallucination", "issue": "i"},
                ],
            },
            ensure_ascii=False,
        )
    )
    sev_by_cat = {f["category"]: f["severity"] for f in r["findings"]}
    assert sev_by_cat["disjoint_handoff"] == "warning"
    assert sev_by_cat["incomplete"] == "error"
    assert sev_by_cat["hallucination"] == "error"
