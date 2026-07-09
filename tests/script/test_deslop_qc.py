import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "video-script" / "scripts"))

from deslop_qc import analyze_deslop_qc
from narration import build_agent_brief as build_script_agent_brief, lint_narration


def _write_deslop_requirements(work_dir, *, owner="video-script.narration", style_card_required=True):
    (work_dir / "deslop_qc_requirements.json").write_text(json.dumps({
        "schema_version": 1,
        "owner": owner,
        "style_card_required": style_card_required,
        "packaging_plan_expected": True,
        "deslop_qc": {
            "report_only": True,
            "aigc_detector": False,
            "auto_rewrite": False,
        },
    }, ensure_ascii=False), encoding="utf-8")


def test_deslop_qc_ta_pronoun_not_placeholder_but_scaffold_copy_is():
    """Regression: the gender-neutral pronoun 'TA' (他/她) followed by 要 (idiomatic 解说
    suspense device) must NOT trip placeholder_leakage and hard-abort the render; only the
    example scaffolding tokens 【主角】 / the exact phrase 'TA要赌上' should."""
    legit = analyze_deslop_qc([{"start": 0, "end": 4, "narration": "凶手就在其中，TA要做的下一件事，就是灭口。"}])
    assert legit["ok"] is True
    assert legit["blocker_count"] == 0
    legit2 = analyze_deslop_qc([{"start": 0, "end": 4, "narration": "第二天一早，TA要离开这座城市。"}])
    assert legit2["ok"] is True
    scaffold = analyze_deslop_qc([{"start": 0, "end": 5, "narration": "【主角】表面只是旁观者，这一次，TA要赌上全部去查清旧案真相。"}])
    assert scaffold["ok"] is False
    assert any(b["code"] == "placeholder_leakage" for b in scaffold["blockers"])


def test_deslop_qc_separates_objective_blockers_from_advisories(tmp_path):
    (tmp_path / "style_card.json").write_text('{"voice":"冷静"}', encoding="utf-8")
    report = analyze_deslop_qc([
        {"start": 0, "end": 4, "narration": "他不是退缩而是在等证据——他早有准备。"},
        {"start": 5, "end": 9, "narration": "因为秘密背后真相牵动命运，所以这意味着一场风暴像棋局一样展开。"},
    ], work_dir=tmp_path)

    assert report["ok"] is False
    assert report["contract"].startswith("Local readability/QC report only")
    # em-dash is an objective blocker; the idiomatic 不是…而是 is now advisory-only.
    assert {item["code"] for item in report["blockers"]} == {"em_dash"}
    assert {item["severity"] for item in report["advisories"]} == {"advisory"}
    advisory_codes = {item["code"] for item in report["advisories"]}
    assert "negative_positive_flip" in advisory_codes
    assert "cliche_density" in advisory_codes


def test_lint_narration_embeds_deslop_qc_and_writes_sibling_report(tmp_path):
    _write_deslop_requirements(tmp_path, style_card_required=True)
    (tmp_path / "agent_narration_brief.md").write_text("Brief text no longer controls style-card gating", encoding="utf-8")
    (tmp_path / "original_subtitles.json").write_text(
        json.dumps([{"start": 1, "end": 2, "text": "原声台词——带破折号"}], ensure_ascii=False),
        encoding="utf-8",
    )

    report = lint_narration([
        {"start": 0.0, "end": 3.0, "narration": "正常解说。"},
    ], work_dir=tmp_path)

    codes = {issue["code"] for issue in report["errors"]}
    assert {"missing_style_card", "em_dash"}.issubset(codes)
    assert report["deslop_qc"]["style_card_required"] is True
    assert report["deslop_qc"]["style_card_requirement_source"] == "deslop_qc_requirements.json"
    assert (tmp_path / "deslop_qc.json").exists()
    assert (tmp_path / "narration_lint.json").exists()


def test_prompt_style_card_mention_without_requirements_does_not_gate(tmp_path):
    (tmp_path / "agent_narration_brief.md").write_text("Please author style_card.json first", encoding="utf-8")

    report = lint_narration([
        {"start": 0.0, "end": 3.0, "narration": "正常解说。"},
    ], work_dir=tmp_path)

    codes = {issue["code"] for issue in report["errors"]}
    assert "missing_style_card" not in codes
    assert report["deslop_qc"]["style_card_required"] is False
    assert report["deslop_qc"]["style_card_requirement_source"] == "legacy_default"


def test_script_narration_brief_does_not_leak_hardcoded_example_entities(tmp_path):
    scenes = [{"scene_id": 0, "start": 0.0, "end": 6.0, "description": "门口对峙"}]
    asr = [{"start": 1.0, "end": 5.0, "text": "第一句对白。第二句反击。"}]
    silence = [{"start": 0.0, "end": 1.0, "duration": 1.0, "has_speech": False}]

    text = build_script_agent_brief(scenes, asr, silence, 6.0, tmp_path, style="纪实复盘").read_text(encoding="utf-8")
    requirements = json.loads((tmp_path / "deslop_qc_requirements.json").read_text(encoding="utf-8"))

    assert requirements == {
        "schema_version": 1,
        "style_card_required": False,
    }
    for leaked in ["范闲", "监察院", "五竹", "京都"]:
        assert leaked not in text


def test_deslop_qc_is_report_only_with_blocker_advisory_split(tmp_path):
    (tmp_path / "style_card.json").write_text('{"voice":"冷静"}', encoding="utf-8")

    report = analyze_deslop_qc([
        {"start": 0, "end": 4, "narration": "他不是退缩而是在等证据——他早有准备。"},
        {
            "start": 5,
            "end": 9,
            "narration": "因为命运的齿轮和背后真相牵动秘密、人性、成长、救赎、时代、选择、意义，所以因此这意味着抽象总结仿佛一场风暴像棋局如同一张网。",
        },
    ], work_dir=tmp_path)

    assert report["ok"] is False
    assert "not an AIGC detector" in report["contract"]
    assert "never rewrites text" in report["contract"]
    assert report["style_card_required"] is False
    assert {item["code"] for item in report["blockers"]} == {"em_dash"}

    advisory_codes = {item["code"] for item in report["advisories"]}
    assert {"negative_positive_flip", "cliche_density", "abstract_summary", "reasoning_chain", "metaphor_markers"}.issubset(advisory_codes)
    assert all(item["severity"] == "advisory" for item in report["advisories"])
    assert not ({item["code"] for item in report["blockers"]} & advisory_codes)
    assert "rewrite" not in report
    assert "rewrites" not in report
