import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'video-assemble' / 'scripts'))
"""Subtitle style must scale to the probed canvas so 竖屏 (9:16) text is not stretched.

Regression guard for the hardcoded 1280x720 PlayRes bug: libass interprets all style
metrics in the PlayRes coordinate space, so a 16:9 PlayRes on a 9:16 frame squished
glyphs horizontally and mis-sized the source-subtitle mask band. Landscape output must
stay byte-identical; portrait must use a frame-matching PlayRes that fits the width.
"""
from assemble import _subtitle_style_config, _generate_ass  # noqa: E402


def test_canvas_none_is_legacy_default():
    # the bug was THIS PlayRes being a hardcoded 16:9 box regardless of the frame
    s = _subtitle_style_config()
    assert (s["play_res_x"], s["play_res_y"]) == (1280, 720)


def test_reference_canvas_equals_legacy_exactly():
    # a 1280x720 source must reproduce the canvas-less defaults bit for bit
    assert _subtitle_style_config({"width": 1280, "height": 720}) == _subtitle_style_config()


def test_1080p_16x9_is_uniformly_scaled_without_distortion():
    base = _subtitle_style_config()
    s = _subtitle_style_config({"width": 1920, "height": 1080})
    assert (s["play_res_x"], s["play_res_y"]) == (1920, 1080)
    # PlayRes aspect == frame aspect => libass applies no horizontal stretch
    assert abs(s["play_res_x"] / s["play_res_y"] - 1920 / 1080) < 1e-9
    # 16:9 => width never binds, so the font is purely height-proportional (the legacy look)
    assert s["font_size"] == int(base["font_size"] * 1080 / 720)
    assert s["margin_v"] == round(base["margin_v"] * 1080 / 720)
    assert s["margin_l"] == round(base["margin_l"] * 1920 / 1280)


def test_portrait_matches_frame_aspect_and_fits_width():
    cw, ch = 1080, 1920
    s = _subtitle_style_config({"width": cw, "height": ch})
    # the fix: PlayRes follows the frame instead of a hardcoded 16:9 box
    assert (s["play_res_x"], s["play_res_y"]) == (cw, ch)
    assert abs(s["play_res_x"] / s["play_res_y"] - cw / ch) < 1e-9
    # a full max_chars line must fit the usable width -> no horizontal overflow off-frame
    usable_w = cw - s["margin_l"] - s["margin_r"]
    assert s["font_size"] * s["max_chars"] <= usable_w
    # outline scales with the glyph, not the frame, so it stays proportionate
    assert s["outline"] >= 1


def test_env_pinned_playres_disables_canvas_scaling(monkeypatch):
    # explicit SUBTITLE_PLAY_RES_* is the escape hatch: canvas must be ignored
    monkeypatch.setenv("SUBTITLE_PLAY_RES_X", "1280")
    monkeypatch.setenv("SUBTITLE_PLAY_RES_Y", "720")
    s = _subtitle_style_config({"width": 1080, "height": 1920})
    assert (s["play_res_x"], s["play_res_y"]) == (1280, 720)
    assert s["font_size"] == 42  # unscaled


def test_generate_ass_writes_canvas_driven_playres(tmp_path):
    segs = [{"narration": "竖屏解说测试文本", "start": 1.0, "end": 4.0}]
    _generate_ass(segs, tmp_path, None, {"width": 1080, "height": 1920})
    ass = (tmp_path / "subtitles.ass").read_text(encoding="utf-8")
    assert "PlayResX: 1080" in ass
    assert "PlayResY: 1920" in ass
    # and the canvas-less call still emits the legacy 16:9 PlayRes
    _generate_ass(segs, tmp_path, None)
    legacy = (tmp_path / "subtitles.ass").read_text(encoding="utf-8")
    assert "PlayResX: 1280" in legacy and "PlayResY: 720" in legacy
