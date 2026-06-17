import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'video-assemble' / 'scripts'))
"""Original-audio blocks (narration gaps) get the original dialogue (from ASR) burned as
subtitles so the band is never blank while the original speaks. Cut mode remaps ASR to output."""
import json  # noqa: E402

import assemble  # noqa: E402


def _burn_on(monkeypatch):
    monkeypatch.setitem(assemble.CONFIG, "burn_subtitles", True)
    monkeypatch.setitem(assemble.CONFIG, "mask_source_subtitles", True)
    monkeypatch.setitem(assemble.CONFIG, "subtitle_original_in_gaps", True)


def test_load_original_asr_filters_bad_entries(tmp_path):
    (tmp_path / "asr_result.json").write_text(json.dumps([
        {"start": 1.0, "end": 2.0, "text": "你好"},
        {"start": 2.0, "end": 2.0, "text": "零长度"},   # dropped: end <= start
        {"start": 3.0, "end": 4.0, "text": "   "},       # dropped: empty text
        "not-a-dict",                                     # dropped
    ]), encoding="utf-8")
    assert assemble._load_original_asr(tmp_path) == [{"start": 1.0, "end": 2.0, "text": "你好"}]


def test_load_original_asr_absent(tmp_path):
    assert assemble._load_original_asr(tmp_path) == []


def test_output_clip_spans_none_in_full_mode(tmp_path):
    assert assemble._output_clip_spans(tmp_path) is None


def test_output_clip_spans_from_validated_plan(tmp_path):
    (tmp_path / "clip_plan_validated.json").write_text(json.dumps({"clips": [
        {"source_start": 10.0, "source_end": 20.0, "output_start": 0.0, "output_end": 10.0},
        {"source_start": 50.0, "source_end": 56.0, "output_start": 10.0, "output_end": 16.0},
    ]}), encoding="utf-8")
    spans = assemble._output_clip_spans(tmp_path)
    assert spans[0]["source_start"] == 10.0 and spans[0]["output_start"] == 0.0
    assert spans[1]["source_start"] == 50.0 and spans[1]["output_end"] == 16.0


def test_map_asr_identity_in_full_mode():
    asr = [{"start": 1.0, "end": 2.0, "text": "x"}]
    assert assemble._map_asr_to_output(asr, None) == [{"start": 1.0, "end": 2.0, "text": "x"}]


def test_map_asr_cut_intersection_and_dropout():
    spans = [{"source_start": 10.0, "source_end": 20.0, "output_start": 0.0, "output_end": 10.0}]
    # a line at source 12-15 maps to output 2-5
    assert assemble._map_asr_to_output([{"start": 12.0, "end": 15.0, "text": "hi"}], spans) == \
        [{"start": 2.0, "end": 5.0, "text": "hi"}]
    # a line entirely in cut-away footage (30-32) is dropped
    assert assemble._map_asr_to_output([{"start": 30.0, "end": 32.0, "text": "gone"}], spans) == []


def test_narration_gap_windows_complement():
    segs = [{"actual_place_start": 2.0, "actual_place_end": 5.0},
            {"actual_place_start": 8.0, "actual_place_end": 10.0}]
    gaps = assemble._narration_gap_windows(segs, 14.0, min_gap=0.8)
    assert (0.0, 2.0) in gaps and (5.0, 8.0) in gaps and (10.0, 14.0) in gaps


def test_original_gap_entries_gated_off(monkeypatch, tmp_path):
    # burn off
    monkeypatch.setitem(assemble.CONFIG, "burn_subtitles", False)
    monkeypatch.setitem(assemble.CONFIG, "mask_source_subtitles", True)
    monkeypatch.setitem(assemble.CONFIG, "subtitle_original_in_gaps", True)
    assert assemble._original_gap_subtitle_entries([], tmp_path, 10.0) == []
    # not masking → source subs already show, so we must NOT add ours
    monkeypatch.setitem(assemble.CONFIG, "burn_subtitles", True)
    monkeypatch.setitem(assemble.CONFIG, "mask_source_subtitles", False)
    assert assemble._original_gap_subtitle_entries([], tmp_path, 10.0) == []
    # explicit override off
    monkeypatch.setitem(assemble.CONFIG, "mask_source_subtitles", True)
    monkeypatch.setitem(assemble.CONFIG, "subtitle_original_in_gaps", False)
    assert assemble._original_gap_subtitle_entries([], tmp_path, 10.0) == []


def test_original_gap_entries_full_mode(monkeypatch, tmp_path):
    _burn_on(monkeypatch)
    (tmp_path / "asr_result.json").write_text(
        json.dumps([{"start": 1.0, "end": 4.0, "text": "原声台词"}]), encoding="utf-8")
    segs = [{"actual_place_start": 5.0, "actual_place_end": 8.0, "narration": "解说"}]
    entries = assemble._original_gap_subtitle_entries(segs, tmp_path, 10.0)
    assert entries and all(e["text"] for e in entries)
    # confined to the asr span clipped into the [0,5] gap
    assert all(1.0 <= e["start"] and e["end"] <= 4.0 + 1e-6 for e in entries)


def test_original_gap_entries_cut_mode_remap(monkeypatch, tmp_path):
    _burn_on(monkeypatch)
    # source line at 12-15 -> output 2-5 (clip maps source 10-20 -> output 0-10)
    (tmp_path / "asr_result.json").write_text(
        json.dumps([{"start": 12.0, "end": 15.0, "text": "原声"}]), encoding="utf-8")
    (tmp_path / "clip_plan_validated.json").write_text(json.dumps({"clips": [
        {"source_start": 10.0, "source_end": 20.0, "output_start": 0.0, "output_end": 10.0}]}),
        encoding="utf-8")
    # narration occupies output [6,9]; the mapped line (2-5) sits in the [0,6] gap
    segs = [{"actual_place_start": 6.0, "actual_place_end": 9.0, "narration": "解说"}]
    entries = assemble._original_gap_subtitle_entries(segs, tmp_path, 10.0)
    assert entries
    assert all(2.0 <= e["start"] and e["end"] <= 5.0 + 1e-6 for e in entries)


def test_combined_entries_sorted_no_overlap_with_narration(monkeypatch, tmp_path):
    _burn_on(monkeypatch)
    (tmp_path / "asr_result.json").write_text(
        json.dumps([{"start": 1.0, "end": 4.0, "text": "原声"}]), encoding="utf-8")
    segs = [{"actual_place_start": 5.0, "actual_place_end": 8.0, "narration": "解说词内容",
             "start": 5.0, "end": 8.0}]
    combined = assemble._combined_subtitle_entries(segs, tmp_path, 10.0)
    assert combined[0]["start"] < 5.0  # original gap entry sorts first
    for e in combined:
        if e["start"] < 5.0:  # gap entries must not bleed into the narration window
            assert e["end"] <= 5.0 + 1e-6


def test_generate_ass_includes_original_only_with_duration(monkeypatch, tmp_path):
    _burn_on(monkeypatch)
    (tmp_path / "asr_result.json").write_text(
        json.dumps([{"start": 1.0, "end": 4.0, "text": "原声台词"}]), encoding="utf-8")
    segs = [{"actual_place_start": 5.0, "actual_place_end": 8.0, "narration": "解说",
             "start": 5.0, "end": 8.0}]
    assemble._generate_ass(segs, tmp_path, 10.0)
    ass = (tmp_path / "subtitles.ass").read_text(encoding="utf-8")
    assert "原声台词" in ass and "解说" in ass
    # backward compatible: no video_duration -> narration only, no original gap subs
    assemble._generate_ass(segs, tmp_path)
    ass2 = (tmp_path / "subtitles.ass").read_text(encoding="utf-8")
    assert "原声台词" not in ass2 and "解说" in ass2


def test_original_entry_straddling_narration_window_is_split(monkeypatch, tmp_path):
    _burn_on(monkeypatch)
    # one long original line (3-10) straddles a narration window at output [5,8]
    (tmp_path / "asr_result.json").write_text(
        json.dumps([{"start": 3.0, "end": 10.0, "text": "一句很长的原声台词横跨了解说块"}]), encoding="utf-8")
    segs = [{"actual_place_start": 5.0, "actual_place_end": 8.0, "narration": "解说"}]
    entries = assemble._original_gap_subtitle_entries(segs, tmp_path, 12.0)
    assert entries
    # nothing bleeds into the narration window [5,8]
    for e in entries:
        assert e["end"] <= 5.0 + 1e-6 or e["start"] >= 8.0 - 1e-6
    # both gap fragments (before 5 and after 8) are represented
    assert any(e["end"] <= 5.0 + 1e-6 for e in entries)
    assert any(e["start"] >= 8.0 - 1e-6 for e in entries)
