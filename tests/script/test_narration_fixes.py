import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'video-script' / 'scripts'))
"""Regression tests for narration.py punctuation + sentence-truncation fixes."""
import sys
from pathlib import Path


from narration import _truncate_at_sentence, _validate_narration_budget, _text_char_count


# ── BUG 9: trailing pause punctuation must be replaced, not appended ──


def _narrate(text, start=0.0, end=10.0):
    """Run one segment through the budget validator and return its final narration."""
    out = _validate_narration_budget(
        [{"start": start, "end": end, "narration": text}], None
    )
    assert len(out) == 1
    return out[0]["narration"]


def test_trailing_comma_replaced_not_appended():
    # Old behavior appended "。" after the cleaned text, yielding "…门，。".
    result = _narrate("他走进了那扇门，")
    assert result.endswith("门。")
    assert "，。" not in result
    assert result[-1] == "。"


def test_trailing_pause_variants_replaced():
    for tail in "，：、；,—":
        result = _narrate(f"他停在门口{tail}")
        assert result.endswith("门口。"), (tail, result)
        # No doubled punctuation of the form <pause><terminal>.
        assert result[-2] not in "，：、；,—"


def test_ellipsis_terminated_text_unchanged():
    # "…" is a valid terminal per the lint, so it must NOT get "。" appended.
    result = _narrate("他迟疑了一下…")
    assert result.endswith("…")
    assert not result.endswith("…。")


def test_proper_terminal_untouched():
    for tail in "。！？!?":
        result = _narrate(f"他走进了那扇门{tail}")
        assert result.endswith(f"门{tail}"), (tail, result)


# ── BUG 10: truncation keeps the LAST fitting sentence boundary ──


def test_truncate_keeps_all_fitting_sentences():
    # An early 。 must not win over a later ！ that also fits the budget.
    text = "他冲了进去。所有人都惊了！" + "尾巴" * 200
    # Budget large enough to fit both leading sentences but not the tail.
    result = _truncate_at_sentence(text, 20)
    assert result == "他冲了进去。所有人都惊了！"


def test_truncate_falls_back_to_last_pause_boundary():
    # No sentence terminal inside the window -> cut at the LAST fitting pause.
    text = "他走进门，环顾四周，握紧了拳头" + "尾巴" * 200
    result = _truncate_at_sentence(text, 12)
    # Should cut at the second comma (last fitting pause), not the first.
    assert result == "他走进门，环顾四周。"


def test_truncate_noop_when_within_budget():
    text = "他走进了那扇门。"
    assert _truncate_at_sentence(text, 100) == text


# ── BLOCK-TRUNCATION BUG: full-mode scene clamp must not chop a block that spans a cut ──

_TWO_SCENES = [
    {"scene_id": 0, "start": 0.0, "end": 10.0},
    {"scene_id": 1, "start": 10.0, "end": 30.0},
]


def test_multi_sentence_block_spanning_scene_cut_keeps_full_text():
    # An authored 3-sentence BLOCK spans the cut at 10s (midpoint 10.0 is on the boundary,
    # _find_scene_for_midpoint resolves it to a single scene). Before the fix the window was
    # clamped to that one scene and _truncate_at_sentence dropped trailing sentences.
    # ~50 chars: overflows the clamped single-scene window [2,10] (budget ~30) but fits the
    # authored window [2,18] (budget ~61). Old code clamped to 10s and truncated the tail.
    block = "他猛地推开那扇沉重的木门冲进房间，屋里却空无一人。桌上的台灯还亮着，茶杯里的水汽未散。窗帘在夜风里轻轻晃动着。"
    out = _validate_narration_budget(
        [{"start": 2.0, "end": 18.0, "narration": block}], _TWO_SCENES
    )
    assert len(out) == 1
    # Full authored text survives (no trailing-sentence truncation).
    assert _text_char_count(out[0]["narration"]) == _text_char_count(block)
    assert "窗帘在夜风里轻轻晃动着" in out[0]["narration"]
    # Author timing is preserved because clamping would have forced truncation.
    assert out[0]["end"] == 18.0


def test_single_sentence_beat_still_clamped_to_midpoint_scene():
    # A short single-sentence beat whose midpoint (6.0) is in scene 0 but nominal end (12)
    # spills into scene 1. The clamped window (2-10) still fits the tiny text, so the clamp
    # still applies — single-sentence quiet-window alignment is unchanged by the fix.
    out = _validate_narration_budget(
        [{"start": 2.0, "end": 12.0, "narration": "他点了点头。"}], _TWO_SCENES
    )
    assert len(out) == 1
    assert out[0]["end"] == 10.0  # tightened to the scene boundary
    assert out[0]["narration"].startswith("他点了点头")
