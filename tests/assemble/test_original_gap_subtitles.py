import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'video-assemble' / 'scripts'))
"""Original-audio blocks (narration gaps) get the original dialogue (from ASR) burned as
subtitles so the band is never blank while the original speaks. Cut mode remaps ASR to output."""
import json  # noqa: E402

import assembly_settings  # noqa: E402
from assemble_constants import SUBTITLE_TEXT_NORMALIZE_VERSION  # noqa: E402
from lib import CONFIG  # noqa: E402
import source_subtitles  # noqa: E402
import subtitle_core  # noqa: E402
import subtitle_render  # noqa: E402


def _burn_on(monkeypatch):
    monkeypatch.setitem(CONFIG, "burn_subtitles", True)
    monkeypatch.setitem(CONFIG, "mask_source_subtitles", True)
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_policy", "opt_in")
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_timing", "all")
    monkeypatch.setitem(CONFIG, "subtitle_mask_opacity", 1.0)
    monkeypatch.setitem(CONFIG, "subtitle_original_in_gaps", True)


def test_load_original_asr_filters_bad_entries(tmp_path):
    (tmp_path / "asr_result.json").write_text(json.dumps([
        {"start": 1.0, "end": 2.0, "text": "你好"},
        {"start": 2.0, "end": 2.0, "text": "零长度"},   # dropped: end <= start
        {"start": 3.0, "end": 4.0, "text": "   "},       # dropped: empty text
        "not-a-dict",                                     # dropped
    ]), encoding="utf-8")
    assert source_subtitles._load_original_asr(tmp_path) == [{"start": 1.0, "end": 2.0, "text": "你好"}]


def test_load_original_asr_absent(tmp_path):
    assert source_subtitles._load_original_asr(tmp_path) == []


def test_output_clip_spans_none_in_full_mode(tmp_path):
    assert source_subtitles._output_clip_spans(tmp_path) is None


def test_output_clip_spans_from_validated_plan(tmp_path):
    (tmp_path / "clip_plan_validated.json").write_text(json.dumps({"clips": [
        {"source_start": 10.0, "source_end": 20.0, "output_start": 0.0, "output_end": 10.0},
        {"source_start": 50.0, "source_end": 56.0, "output_start": 10.0, "output_end": 16.0},
    ]}), encoding="utf-8")
    spans = source_subtitles._output_clip_spans(tmp_path)
    assert spans[0]["source_start"] == 10.0 and spans[0]["output_start"] == 0.0
    assert spans[1]["source_start"] == 50.0 and spans[1]["output_end"] == 16.0




def test_output_clip_spans_ignore_stale_validated_plan(tmp_path):
    raw = {"clips": [{"start": 40.0, "end": 45.0}]}
    (tmp_path / "clip_plan.json").write_text(json.dumps(raw), encoding="utf-8")
    (tmp_path / "clip_plan_validated.json").write_text(json.dumps({
        "raw_plan_fingerprint": "stale",
        "clips": [{"source_start": 0.0, "source_end": 10.0, "output_start": 0.0, "output_end": 10.0}],
    }), encoding="utf-8")

    spans = source_subtitles._output_clip_spans(tmp_path)

    assert spans == [{"source_start": 40.0, "source_end": 45.0, "output_start": 0.0, "output_end": 5.0}]

def test_map_asr_identity_in_full_mode():
    asr = [{"start": 1.0, "end": 2.0, "text": "x"}]
    assert source_subtitles._map_asr_to_output(asr, None) == [{"start": 1.0, "end": 2.0, "text": "x"}]


def test_map_asr_cut_intersection_and_dropout():
    spans = [{"source_start": 10.0, "source_end": 20.0, "output_start": 0.0, "output_end": 10.0}]
    # a line at source 12-15 maps to output 2-5
    assert source_subtitles._map_asr_to_output([{"start": 12.0, "end": 15.0, "text": "hi"}], spans) == \
        [{"start": 2.0, "end": 5.0, "text": "hi"}]
    # a line entirely in cut-away footage (30-32) is dropped
    assert source_subtitles._map_asr_to_output([{"start": 30.0, "end": 32.0, "text": "gone"}], spans) == []


def test_narration_gap_windows_complement():
    segs = [{"actual_place_start": 2.0, "actual_place_end": 5.0},
            {"actual_place_start": 8.0, "actual_place_end": 10.0}]
    gaps = source_subtitles._narration_gap_windows(segs, 14.0, min_gap=0.8)
    assert (0.0, 2.0) in gaps and (5.0, 8.0) in gaps and (10.0, 14.0) in gaps


def test_original_gap_entries_gated_off(monkeypatch, tmp_path):
    # burn off
    monkeypatch.setitem(CONFIG, "burn_subtitles", False)
    monkeypatch.setitem(CONFIG, "mask_source_subtitles", True)
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_policy", "opt_in")
    monkeypatch.setitem(CONFIG, "subtitle_original_in_gaps", True)
    assert source_subtitles._original_gap_subtitle_entries([], tmp_path, 10.0) == []
    # not masking → source subs already show, so we must NOT add ours
    monkeypatch.setitem(CONFIG, "burn_subtitles", True)
    monkeypatch.setitem(CONFIG, "mask_source_subtitles", False)
    assert source_subtitles._original_gap_subtitle_entries([], tmp_path, 10.0) == []
    # explicit override off
    monkeypatch.setitem(CONFIG, "mask_source_subtitles", True)
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_policy", "opt_in")
    monkeypatch.setitem(CONFIG, "subtitle_original_in_gaps", False)
    assert source_subtitles._original_gap_subtitle_entries([], tmp_path, 10.0) == []


def test_original_gap_entries_use_user_subs_even_without_mask(monkeypatch, tmp_path):
    """A bring-your-own user_subtitles file populates the gaps even with mask OFF — the clean/foreign
    source case (no burned subs to double). Without a user file, mask OFF still yields nothing."""
    monkeypatch.setitem(CONFIG, "burn_subtitles", True)
    monkeypatch.setitem(CONFIG, "mask_source_subtitles", False)   # nothing to mask
    monkeypatch.setitem(CONFIG, "subtitle_original_in_gaps", True)
    segs = [{"actual_place_start": 5.0, "actual_place_end": 8.0, "narration": "解说"}]

    # no user file → mask OFF keeps the gate closed (don't double the source's own visible subs)
    assert source_subtitles._original_gap_subtitle_entries(segs, tmp_path, 10.0) == []

    # drop in user_subtitles.srt → gaps fill from it despite mask OFF
    (tmp_path / "user_subtitles.srt").write_text(
        "1\n00:00:01,000 --> 00:00:04,000\n原声台词\n\n", encoding="utf-8")
    entries = source_subtitles._original_gap_subtitle_entries(segs, tmp_path, 10.0)
    assert entries and all(e["text"] for e in entries)
    assert all(1.0 <= e["start"] and e["end"] <= 4.0 + 1e-6 for e in entries)


def test_original_gap_entries_full_mode(monkeypatch, tmp_path):
    _burn_on(monkeypatch)
    (tmp_path / "asr_result.json").write_text(
        json.dumps([{"start": 1.0, "end": 4.0, "text": "原声台词"}]), encoding="utf-8")
    segs = [{"actual_place_start": 5.0, "actual_place_end": 8.0, "narration": "解说"}]
    entries = source_subtitles._original_gap_subtitle_entries(segs, tmp_path, 10.0)
    assert entries and all(e["text"] for e in entries)
    # confined to the asr span clipped into the [0,5] gap
    assert all(1.0 <= e["start"] and e["end"] <= 4.0 + 1e-6 for e in entries)


def test_original_gap_entries_suppressed_when_mask_only_follows_narration(monkeypatch, tmp_path):
    _burn_on(monkeypatch)
    monkeypatch.setitem(CONFIG, "source_subtitle_mask_timing", "narration")
    (tmp_path / "asr_result.json").write_text(
        json.dumps([{"start": 1.0, "end": 4.0, "text": "原声已自带硬字幕"}]), encoding="utf-8"
    )
    segs = [{"actual_place_start": 5.0, "actual_place_end": 8.0, "narration": "解说"}]

    assert source_subtitles._original_gap_subtitle_entries(segs, tmp_path, 10.0) == []


def test_original_gap_entries_suppressed_when_mask_is_transparent(monkeypatch, tmp_path):
    _burn_on(monkeypatch)
    monkeypatch.setitem(CONFIG, "subtitle_mask_opacity", 0.0)
    (tmp_path / "asr_result.json").write_text(
        json.dumps([{"start": 1.0, "end": 4.0, "text": "原声已自带硬字幕"}]), encoding="utf-8"
    )

    assert source_subtitles._original_gap_subtitle_entries([], tmp_path, 10.0) == []


def test_original_gap_entries_suppressed_when_full_timeline_mask_is_translucent(
    monkeypatch, tmp_path
):
    _burn_on(monkeypatch)
    monkeypatch.setitem(CONFIG, "subtitle_mask_opacity", 0.6)
    (tmp_path / "asr_result.json").write_text(
        json.dumps([{"start": 1.0, "end": 4.0, "text": "原声仍透过遮罩可见"}]),
        encoding="utf-8",
    )

    assert source_subtitles._original_gap_subtitle_entries([], tmp_path, 10.0) == []


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
    entries = source_subtitles._original_gap_subtitle_entries(segs, tmp_path, 10.0)
    assert entries
    assert all(2.0 <= e["start"] and e["end"] <= 5.0 + 1e-6 for e in entries)


def test_combined_entries_sorted_no_overlap_with_narration(monkeypatch, tmp_path):
    _burn_on(monkeypatch)
    (tmp_path / "asr_result.json").write_text(
        json.dumps([{"start": 1.0, "end": 4.0, "text": "原声"}]), encoding="utf-8")
    segs = [{"actual_place_start": 5.0, "actual_place_end": 8.0, "narration": "解说词内容",
             "start": 5.0, "end": 8.0}]
    combined = source_subtitles._combined_subtitle_entries(segs, tmp_path, 10.0)
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
    subtitle_render._generate_ass(segs, tmp_path, 10.0)
    ass = (tmp_path / "subtitles.ass").read_text(encoding="utf-8")
    assert "原声台词" in ass and "解说" in ass
    # backward compatible: no video_duration -> narration only, no original gap subs
    subtitle_render._generate_ass(segs, tmp_path)
    ass2 = (tmp_path / "subtitles.ass").read_text(encoding="utf-8")
    assert "原声台词" not in ass2 and "解说" in ass2


def test_original_line_assigned_to_single_gap_by_midpoint(monkeypatch, tmp_path):
    # a WHOLE sentence whose char-proportional midpoint sits in a narration window is dropped by the
    # fallback (conservative) — it is not shown in several gaps or crammed where it isn't spoken.
    _burn_on(monkeypatch)
    (tmp_path / "asr_result.json").write_text(
        json.dumps([{"start": 3.0, "end": 10.0, "text": "一句横跨解说块的原声"}]), encoding="utf-8")
    segs = [{"actual_place_start": 5.0, "actual_place_end": 8.0, "narration": "解说"}]  # midpoint 6.5 ∈ narration
    assert source_subtitles._original_gap_subtitle_entries(segs, tmp_path, 12.0) == []


def test_original_lines_wrapped_in_brackets(monkeypatch, tmp_path):
    _burn_on(monkeypatch)
    (tmp_path / "asr_result.json").write_text(
        json.dumps([{"start": 1.0, "end": 4.0, "text": "我赶回来了"}]), encoding="utf-8")
    segs = [{"actual_place_start": 5.0, "actual_place_end": 8.0, "narration": "解说"}]
    entries = source_subtitles._original_gap_subtitle_entries(segs, tmp_path, 10.0)
    assert entries
    joined = "".join(e["text"] for e in entries)
    assert joined.startswith("「") and joined.endswith("」")


def test_original_gap_entries_strip_terminal_punctuation_inside_brackets(monkeypatch, tmp_path):
    _burn_on(monkeypatch)
    (tmp_path / "asr_result.json").write_text(
        json.dumps([{"start": 1.0, "end": 4.0, "text": "原声台词。"}]), encoding="utf-8")
    segs = [{"actual_place_start": 5.0, "actual_place_end": 8.0, "narration": "解说"}]

    entries = source_subtitles._original_gap_subtitle_entries(segs, tmp_path, 10.0)

    joined = "".join(e["text"] for e in entries)
    assert joined == "「原声台词」"
    assert "原声台词。" not in joined


def test_original_gap_entries_keep_closing_quote_with_stripped_terminal_punctuation(monkeypatch, tmp_path):
    _burn_on(monkeypatch)
    (tmp_path / "asr_result.json").write_text(
        json.dumps([{"start": 1.0, "end": 4.0, "text": "他说：「你好。」"}]), encoding="utf-8")
    segs = [{"actual_place_start": 5.0, "actual_place_end": 8.0, "narration": "解说"}]

    entries = source_subtitles._original_gap_subtitle_entries(segs, tmp_path, 10.0)

    texts = [e["text"] for e in entries]
    assert "」" not in texts
    assert "".join(texts) == "「他说：「你好」」"


def test_agent_subtitles_preferred_over_asr(monkeypatch, tmp_path):
    _burn_on(monkeypatch)
    # ASR has a (wrong) line; the agent-calibrated file should win
    (tmp_path / "asr_result.json").write_text(
        json.dumps([{"start": 1.0, "end": 4.0, "text": "她叫叶青眉"}]), encoding="utf-8")
    (tmp_path / "original_subtitles.json").write_text(
        json.dumps([{"start": 1.5, "end": 3.5, "text": "她叫叶轻眉"}]), encoding="utf-8")
    segs = [{"actual_place_start": 5.0, "actual_place_end": 8.0, "narration": "解说"}]
    entries = source_subtitles._original_gap_subtitle_entries(segs, tmp_path, 10.0)
    joined = "".join(e["text"] for e in entries)
    assert "叶轻眉" in joined and "叶青眉" not in joined  # calibrated text, ASR error not used


def test_over_dense_asr_line_truncated_and_shown_in_fallback(monkeypatch, tmp_path):
    # R3: a long ASR block landing in a tiny gap is FRONT-TRUNCATED to fit the gap at the max read
    # rate and shown truncated — not dropped to blank (the old behavior was to skip it entirely).
    _burn_on(monkeypatch)
    dense = "我既然回来了京都就是最安全的小姐遇害你和你的黑骑为什么不在京都我听命行事"  # 36 chars, no sentence marks
    (tmp_path / "asr_result.json").write_text(
        json.dumps([{"start": 0.5, "end": 2.0, "text": dense}]), encoding="utf-8")
    # narration fills [2, 9.7]; the dense line sits in the small [0,2] gap (~1.5s from its 0.5 onset)
    segs = [{"actual_place_start": 2.0, "actual_place_end": 9.7, "narration": "解说"}]
    entries = source_subtitles._original_gap_subtitle_entries(segs, tmp_path, 10.0)
    assert entries  # shown, not dropped
    joined = "".join(e["text"] for e in entries)
    assert joined.startswith("「") and joined.endswith("」")
    body = joined.strip("「」")
    assert body and len(body) <= 14  # ~1.5s * 9 ch/s → front-truncated to fit the gap
    assert dense.startswith(body)   # it is the LEADING (front) portion of the dense line


def test_calibrated_line_not_subject_to_density_guard(monkeypatch, tmp_path):
    # the agent file is trusted: even a dense-looking line is shown (the agent sized it)
    _burn_on(monkeypatch)
    dense = "我既然回来了京都就是最安全的小姐遇害你和你的黑骑为什么不在京都"
    (tmp_path / "original_subtitles.json").write_text(
        json.dumps([{"start": 1.0, "end": 3.0, "text": dense}]), encoding="utf-8")
    segs = [{"actual_place_start": 6.0, "actual_place_end": 9.0, "narration": "解说"}]
    assert source_subtitles._original_gap_subtitle_entries(segs, tmp_path, 10.0)  # kept


# --- Q7: 破折号 normalization -------------------------------------------------

def test_normalize_subtitle_text_collapses_em_dashes():
    # "——" → "，"; a lone "—" → "，"; never leaves a doubled comma
    assert subtitle_core._normalize_subtitle_text("他来了——然后走了") == "他来了，然后走了"
    assert subtitle_core._normalize_subtitle_text("等等—别走") == "等等，别走"
    assert subtitle_core._normalize_subtitle_text("一———二") == "一，二"  # any dash run collapses to one comma
    assert subtitle_core._normalize_subtitle_text("已经，——好") == "已经，好"  # no double comma
    assert subtitle_core._normalize_subtitle_text("") == ""
    assert subtitle_core._normalize_subtitle_text(None) == ""


def test_generated_srt_and_ass_normalize_em_dashes(monkeypatch, tmp_path):
    # narration text with a dash is normalized in BOTH generated srt and ass burned text
    segs = [{"actual_place_start": 1.0, "actual_place_end": 4.0,
             "narration": "我回来了——这一次", "start": 1.0, "end": 4.0}]
    subtitle_render._generate_srt(segs, tmp_path)
    subtitle_render._generate_ass(segs, tmp_path)
    srt = (tmp_path / "subtitles.srt").read_text(encoding="utf-8")
    ass = (tmp_path / "subtitles.ass").read_text(encoding="utf-8")
    assert "——" not in srt and "—" not in srt
    assert "——" not in ass and "—" not in ass
    assert "我回来了，这一次" in srt


def test_original_gap_text_normalizes_em_dashes(monkeypatch, tmp_path):
    # the dash normalization also applies to original-gap subtitle text in the burned output
    _burn_on(monkeypatch)
    (tmp_path / "original_subtitles.json").write_text(
        json.dumps([{"start": 1.0, "end": 4.0, "text": "活着——让我看看"}]), encoding="utf-8")
    segs = [{"actual_place_start": 5.0, "actual_place_end": 8.0, "narration": "解说",
             "start": 5.0, "end": 8.0}]
    subtitle_render._generate_ass(segs, tmp_path, 10.0)
    ass = (tmp_path / "subtitles.ass").read_text(encoding="utf-8")
    assert "——" not in ass and "—" not in ass
    assert "活着，让我看看" in ass


def test_fingerprint_includes_subtitle_text_normalize_version():
    fp = assembly_settings.assembly_settings_fingerprint()
    assert fp["subtitle_text_normalize"] == SUBTITLE_TEXT_NORMALIZE_VERSION


# --- R1: user-provided subtitle file as override primary ----------------------

def test_user_json_output_time_used_verbatim_above_agent(monkeypatch, tmp_path):
    _burn_on(monkeypatch)
    # agent file would say one thing; the user file (bare list = OUTPUT-time) must override it
    (tmp_path / "original_subtitles.json").write_text(
        json.dumps([{"start": 1.0, "end": 4.0, "text": "代理版本"}]), encoding="utf-8")
    (tmp_path / "user_subtitles.json").write_text(
        json.dumps([{"start": 1.0, "end": 4.0, "text": "用户版本"}]), encoding="utf-8")
    segs = [{"actual_place_start": 5.0, "actual_place_end": 8.0, "narration": "解说"}]
    joined = "".join(e["text"] for e in source_subtitles._original_gap_subtitle_entries(segs, tmp_path, 10.0))
    assert "用户版本" in joined and "代理版本" not in joined
    # used verbatim at its OUTPUT time (1-4), no remap
    entries = source_subtitles._original_gap_subtitle_entries(segs, tmp_path, 10.0)
    assert all(1.0 <= e["start"] and e["end"] <= 4.0 + 1e-6 for e in entries)


def test_user_json_wrapper_source_time_remapped(monkeypatch, tmp_path):
    _burn_on(monkeypatch)
    # SOURCE-time user json (timeline=source) is remapped to OUTPUT via the clip spans
    (tmp_path / "clip_plan_validated.json").write_text(json.dumps({"clips": [
        {"source_start": 10.0, "source_end": 20.0, "output_start": 0.0, "output_end": 10.0}]}),
        encoding="utf-8")
    (tmp_path / "user_subtitles.json").write_text(json.dumps({
        "timeline": "source",
        "lines": [{"start": 12.0, "end": 15.0, "text": "源时间台词"}],  # source 12-15 → output 2-5
    }), encoding="utf-8")
    segs = [{"actual_place_start": 6.0, "actual_place_end": 9.0, "narration": "解说"}]
    entries = source_subtitles._original_gap_subtitle_entries(segs, tmp_path, 10.0)
    assert entries
    assert all(2.0 <= e["start"] and e["end"] <= 5.0 + 1e-6 for e in entries)


def test_user_srt_default_source_time_remapped(monkeypatch, tmp_path):
    _burn_on(monkeypatch)
    # .srt defaults to SOURCE-time → remapped via clip spans (source 12-15 → output 2-5)
    (tmp_path / "clip_plan_validated.json").write_text(json.dumps({"clips": [
        {"source_start": 10.0, "source_end": 20.0, "output_start": 0.0, "output_end": 10.0}]}),
        encoding="utf-8")
    (tmp_path / "user_subtitles.srt").write_text(
        "1\n00:00:12,000 --> 00:00:15,000\n源时间字幕\n", encoding="utf-8")
    segs = [{"actual_place_start": 6.0, "actual_place_end": 9.0, "narration": "解说"}]
    entries = source_subtitles._original_gap_subtitle_entries(segs, tmp_path, 10.0)
    joined = "".join(e["text"] for e in entries)
    assert "源时间字幕" in joined
    assert all(2.0 <= e["start"] and e["end"] <= 5.0 + 1e-6 for e in entries)


def test_user_subtitles_malformed_falls_back_no_crash(monkeypatch, tmp_path):
    _burn_on(monkeypatch)
    # garbage user json must not crash and must fall back to the agent file
    (tmp_path / "user_subtitles.json").write_text("{ this is not json", encoding="utf-8")
    (tmp_path / "original_subtitles.json").write_text(
        json.dumps([{"start": 1.0, "end": 4.0, "text": "代理兜底"}]), encoding="utf-8")
    segs = [{"actual_place_start": 5.0, "actual_place_end": 8.0, "narration": "解说"}]
    joined = "".join(e["text"] for e in source_subtitles._original_gap_subtitle_entries(segs, tmp_path, 10.0))
    assert "代理兜底" in joined
    # an empty list is also malformed-ish → returns None and falls through the ladder
    (tmp_path / "user_subtitles.json").write_text("[]", encoding="utf-8")
    assert source_subtitles._load_user_original_subtitles(tmp_path) is None


def test_user_subtitles_absent_returns_none(tmp_path):
    assert source_subtitles._load_user_original_subtitles(tmp_path) is None


def test_fingerprint_user_subtitles_flag(tmp_path):
    assert assembly_settings.assembly_settings_fingerprint(tmp_path)["user_subtitles"] is False
    (tmp_path / "user_subtitles.json").write_text("[]", encoding="utf-8")
    assert assembly_settings.assembly_settings_fingerprint(tmp_path)["user_subtitles"] is True
    # no work_dir → flag is constant False (back-compat for no-arg callers)
    assert assembly_settings.assembly_settings_fingerprint()["user_subtitles"] is False


# --- R2: precise interval-clip path -------------------------------------------

def test_precise_line_straddling_gap_boundary_is_split_across_gaps(monkeypatch, tmp_path):
    # a calibrated/user line whose [start,end] straddles a narration window is CLIPPED into each
    # gap it overlaps (split), not snapped to one gap (midpoint) and not dropped.
    _burn_on(monkeypatch)
    # line 3-10 straddles narration window [5,7]: overlaps gap [0,5] (3-5) and gap [7,12] (7-10)
    (tmp_path / "user_subtitles.json").write_text(
        json.dumps([{"start": 3.0, "end": 10.0, "text": "横跨解说块的原声台词"}]), encoding="utf-8")
    segs = [{"actual_place_start": 5.0, "actual_place_end": 7.0, "narration": "解说"}]
    entries = source_subtitles._original_gap_subtitle_entries(segs, tmp_path, 12.0)
    assert entries
    before = [e for e in entries if e["end"] <= 5.0 + 1e-6]
    after = [e for e in entries if e["start"] >= 7.0 - 1e-6]
    assert before, "expected a fragment clipped into the pre-narration gap [3,5]"
    assert after, "expected a fragment clipped into the post-narration gap [7,10]"
    # no fragment ever bleeds into the narration window [5,7]
    for e in entries:
        assert e["end"] <= 5.0 + 1e-6 or e["start"] >= 7.0 - 1e-6
    # each gap shows only ITS portion — the FULL line is never duplicated whole into both gaps
    assert all("横跨解说块的原声台词" not in e["text"] for e in entries)
    # the per-gap pieces are different slices (no duplication)
    assert before[0]["text"] != after[0]["text"]


# --- R3: coarse-ASR fallback hardening ----------------------------------------

def test_asr_multi_sentence_entry_is_split(monkeypatch, tmp_path):
    # a single ASR block with several sentences is pre-split on 。！？ before gap assignment,
    # so each sentence is placed by its own midpoint instead of the whole block by one midpoint.
    _burn_on(monkeypatch)
    # one ASR entry 1-10 spanning two narration gaps; its two sentences land in different gaps
    (tmp_path / "asr_result.json").write_text(json.dumps([
        {"start": 1.0, "end": 10.0, "text": "第一句话。第二句话。"}]), encoding="utf-8")
    # narration window [5.0,5.4] splits [0,5] and [5.4,12]; sentence midpoints fall either side
    segs = [{"actual_place_start": 5.0, "actual_place_end": 5.4, "narration": "解说"}]
    entries = source_subtitles._original_gap_subtitle_entries(segs, tmp_path, 12.0)
    joined = "".join(e["text"] for e in entries)
    assert "第一句话" in joined and "第二句话" in joined
    # the two sentences are placed in different gaps (one before, one after the narration window)
    assert any(e["end"] <= 5.0 + 1e-6 for e in entries)
    assert any(e["start"] >= 5.4 - 1e-6 for e in entries)
