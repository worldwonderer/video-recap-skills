import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'video-understanding' / 'scripts'))
"""Regression tests for asr.py name-glossary correction (Q8).

ASR mishears character names as homophones (e.g. 叶青眉 → 叶轻眉). After transcription
we correct any single-character substitution of a known name from background_research.json.
The rule is tight (exactly one differing char at one position vs. a length-matched name)
to avoid over-correcting unrelated words.
"""
import json

import asr
from asr import (
    _apply_glossary_corrections,
    _correct_text_with_glossary,
    _load_name_glossary,
)


def _write_research(work_dir, characters=None, character_details=None):
    payload = {}
    if characters is not None:
        payload["characters"] = characters
    if character_details is not None:
        payload["character_details"] = character_details
    (Path(work_dir) / "background_research.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


# ── (a) homophone substitution of a known name is corrected ──

def test_corrects_homophone_substitution_of_known_name(tmp_path):
    _write_research(tmp_path, characters={"叶轻眉": "主角之母"})
    segments = [{"start": 0.0, "end": 3.0, "text": "她叫叶青眉"}]
    _apply_glossary_corrections(segments, tmp_path)
    assert segments[0]["text"] == "她叫叶轻眉"


def test_corrects_name_pulled_from_character_details_aliases(tmp_path):
    # name only reachable via character_details keys + aliases
    _write_research(
        tmp_path,
        character_details={"范闲": {"aliases": ["小范闲"]}},
    )
    names = _load_name_glossary(tmp_path)
    assert "范闲" in names and "小范闲" in names
    # 范闹 → 范闲 (single-char homophone-ish substitution)
    assert _correct_text_with_glossary("这是范闹", names) == "这是范闲"


# ── (b) no over-correction: words >1 char away from any name are untouched ──

def test_does_not_correct_word_more_than_one_char_from_name(tmp_path):
    _write_research(tmp_path, characters={"叶轻眉": "主角之母"})
    names = _load_name_glossary(tmp_path)
    # 叶轻风 differs from 叶轻眉 by exactly one char, but 王富贵 differs by 3 → must be left alone
    assert _correct_text_with_glossary("他是王富贵", names) == "他是王富贵"


def test_identical_name_is_left_unchanged(tmp_path):
    _write_research(tmp_path, characters={"叶轻眉": "主角之母"})
    names = _load_name_glossary(tmp_path)
    # an exact occurrence of the name must not be rewritten or duplicated
    assert _correct_text_with_glossary("她叫叶轻眉", names) == "她叫叶轻眉"


def test_one_char_diff_helper_is_strict(tmp_path):
    assert asr._one_char_diff("叶青眉", "叶轻眉") is True
    assert asr._one_char_diff("叶轻眉", "叶轻眉") is False   # zero diff
    assert asr._one_char_diff("王富贵", "叶轻眉") is False   # three diffs
    assert asr._one_char_diff("叶轻", "叶轻眉") is False     # length mismatch


# ── (c) absent background_research.json → text unchanged (no-op) ──

def test_absent_research_is_noop(tmp_path):
    assert not (tmp_path / "background_research.json").exists()
    segments = [{"start": 0.0, "end": 3.0, "text": "她叫叶青眉"}]
    _apply_glossary_corrections(segments, tmp_path)
    assert segments[0]["text"] == "她叫叶青眉"
    assert _load_name_glossary(tmp_path) == []


def test_research_with_no_names_is_noop(tmp_path):
    _write_research(tmp_path, characters={})
    segments = [{"start": 0.0, "end": 3.0, "text": "她叫叶青眉"}]
    _apply_glossary_corrections(segments, tmp_path)
    assert segments[0]["text"] == "她叫叶青眉"
