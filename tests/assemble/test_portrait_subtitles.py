import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'video-assemble' / 'scripts'))
"""Subtitle style must scale to the probed canvas so 竖屏 (9:16) text is not stretched.

Regression guard for the hardcoded 1280x720 PlayRes bug: libass interprets all style
metrics in the PlayRes coordinate space, so a 16:9 PlayRes on a 9:16 frame squished
glyphs horizontally and mis-sized the source-subtitle mask band. Landscape output must
stay byte-identical; portrait must use a frame-matching PlayRes that fits the width.
"""
from subprocess import CompletedProcess
from assemble import _subtitle_style_config, _generate_ass, _probe_canvas  # noqa: E402
import assemble  # noqa: E402


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


def test_probe_canvas_preserves_legacy_landscape(monkeypatch, tmp_path):
    def fake_run_cmd(cmd):
        return CompletedProcess(cmd, 0, json_text({
            "streams": [{
                "width": 1280,
                "height": 720,
                "r_frame_rate": "30000/1001",
                "sample_aspect_ratio": "1:1",
                "display_aspect_ratio": "16:9",
            }]
        }), "")

    monkeypatch.setattr(assemble, "run_cmd", fake_run_cmd)
    canvas = _probe_canvas(tmp_path / "landscape.mp4")
    assert canvas["width"] == 1280
    assert canvas["height"] == 720
    assert canvas["fps"] == 29.97
    assert canvas["rotation"] == 0


def test_probe_canvas_applies_rotation_and_sar(monkeypatch, tmp_path):
    def fake_run_cmd(cmd):
        return CompletedProcess(cmd, 0, json_text({
            "streams": [{
                "width": 1920,
                "height": 1080,
                "r_frame_rate": "30/1",
                "sample_aspect_ratio": "4:3",
                "display_aspect_ratio": "64:27",
                "tags": {"rotate": "90"},
            }]
        }), "")

    monkeypatch.setattr(assemble, "run_cmd", fake_run_cmd)
    canvas = _probe_canvas(tmp_path / "rotated.mp4")
    assert canvas["storage_width"] == 1920
    assert canvas["storage_height"] == 1080
    assert canvas["rotation"] == 90
    # DAR widens the display canvas first (1080 * 64/27 = 2560), then rotation swaps.
    assert canvas["width"] == 1080
    assert canvas["height"] == 2560
    assert canvas["sample_aspect_ratio"] == "4:3"


def json_text(value):
    import json
    return json.dumps(value)


def test_probe_canvas_applies_rotation_metadata_and_preserves_landscape(monkeypatch):
    """Rotation metadata changes display canvas; unrotated landscape remains legacy width x height."""
    import assemble
    from subprocess import CompletedProcess

    outputs = iter([
        "width=1920\nheight=1080\nr_frame_rate=30000/1001\nrotation=90\nsample_aspect_ratio=1:1\ndisplay_aspect_ratio=9:16\n",
        "width=1280\nheight=720\nr_frame_rate=30/1\nrotation=0\nsample_aspect_ratio=1:1\ndisplay_aspect_ratio=16:9\n",
    ])

    def fake_run_cmd(cmd):
        return CompletedProcess(cmd, 0, stdout=next(outputs), stderr="")

    monkeypatch.setattr(assemble, "run_cmd", fake_run_cmd)

    rotated = assemble._probe_canvas("rotated_portrait.mp4")
    assert rotated["width"] == 1080
    assert rotated["height"] == 1920
    assert rotated["rotation"] == 90
    assert rotated["sar"] == "1:1"
    assert rotated["dar"] == "9:16"

    landscape = assemble._probe_canvas("landscape.mp4")
    assert landscape["width"] == 1280
    assert landscape["height"] == 720
    assert landscape["rotation"] == 0


def test_subtitle_layout_qc_flags_multiline_safe_area_overflow():
    """Subtitle layout QC must be multi-line aware and fail/warn when text exceeds safe area."""
    import assemble

    layout_qc = getattr(assemble, "_subtitle_layout_qc", None)
    assert callable(layout_qc), "assemble must expose subtitle layout QC for overflow checks"

    ok = layout_qc(
        [{"start": 0.0, "end": 2.0, "text": "第一行\n第二行"}],
        canvas={"width": 1080, "height": 1920},
        safe_area={"x": 54, "y": 96, "w": 972, "h": 1728},
    )
    assert ok["max_lines"] == 2
    assert ok["overflow"] is False

    overflow = layout_qc(
        [{"start": 0.0, "end": 2.0, "text": "超长字幕" * 120}],
        canvas={"width": 360, "height": 640},
        safe_area={"x": 36, "y": 64, "w": 288, "h": 120},
    )
    assert overflow["overflow"] is True
    assert any(v.get("kind") in {"safe_area", "line_width", "line_count"} for v in overflow["violations"])
