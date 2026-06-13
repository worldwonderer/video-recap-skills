import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'video-script' / 'scripts'))
"""Regression tests for narration.py punctuation + sentence-truncation fixes."""
import sys
from pathlib import Path


from narration import _truncate_at_sentence, _validate_narration_budget


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
