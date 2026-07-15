import sys
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[2] / "skills" / "video-assemble" / "scripts"),
)
import json  # noqa: E402
import pytest  # noqa: E402
import audio_mix  # noqa: E402
from lib import CONFIG  # noqa: E402
import media  # noqa: E402
from timeline import build_timeline, ducking_keyframes, load_timeline, save_timeline  # noqa: E402
from audio_automation import variable_ducking_keyframes  # noqa: E402
import timeline_emit  # noqa: E402


def _track(timeline, kind, name=None):
    for track in timeline["tracks"]:
        if track.get("kind") == kind and (name is None or track.get("name") == name):
            return track
    raise AssertionError(f"missing track {kind}/{name}")


def test_build_timeline_uses_explicit_subtitle_segments_for_text_track():
    tl = build_timeline(
        {"width": 100, "height": 100, "fps": 30},
        5.0,
        [
            {
                "source_path": "/s.mp4",
                "source_start": 0.0,
                "source_end": 5.0,
                "timeline_start": 0.0,
                "timeline_end": 5.0,
            }
        ],
        [
            {
                "source_path": "/n.wav",
                "timeline_start": 0.0,
                "timeline_end": 2.0,
                "text": "他说完了。",
                "overlaps_speech": True,
            }
        ],
        subtitle_segments=[
            {"text": "他说完了", "timeline_start": 0.1, "timeline_end": 1.9}
        ],
    )

    audio = _track(tl, "audio", "narration")["segments"]
    text = _track(tl, "text", "subtitle")["segments"]
    assert audio[0]["text"] == "他说完了。"
    assert text == [{"text": "他说完了", "timeline_start": 0.1, "timeline_end": 1.9}]


def test_build_timeline_subtitle_segments_skip_invalid_entries():
    tl = build_timeline(
        {"width": 100, "height": 100, "fps": 30},
        5.0,
        [
            {
                "source_path": "/s.mp4",
                "source_start": 0.0,
                "source_end": 5.0,
                "timeline_start": 0.0,
                "timeline_end": 5.0,
            }
        ],
        [],
        subtitle_segments=[
            {"text": "有效", "timeline_start": 1.0, "timeline_end": 2.0},
            {"text": "", "timeline_start": 2.0, "timeline_end": 3.0},
            {"text": "倒置", "timeline_start": 3.0, "timeline_end": 2.0},
        ],
    )

    assert _track(tl, "text", "subtitle")["segments"] == [
        {"text": "有效", "timeline_start": 1.0, "timeline_end": 2.0}
    ]


def test_build_timeline_v2_normalizes_local_image_overlays():
    tl = build_timeline(
        {"width": 1280, "height": 720, "fps": 30},
        5.0,
        [],
        [],
        image_segments=[
            {
                "source_path": "/card.png",
                "timeline_start": 1,
                "timeline_end": 4,
                "opacity": 0.8,
                "rotation_degrees": 12,
                "scale": {"x": 0.5, "y": 0.6},
                "position": {"x": 0.2, "y": -0.3},
                "flip": {"horizontal": True},
            },
            {"source_path": "", "timeline_start": 0, "timeline_end": 2},
            {"source_path": "/bad.png", "timeline_start": 3, "timeline_end": 2},
        ],
    )

    assert tl["schema_version"] == 2
    assert _track(tl, "image", "image")["segments"] == [
        {
            "source_path": "/card.png",
            "timeline_start": 1.0,
            "timeline_end": 4.0,
            "opacity": 0.8,
            "rotation_degrees": 12.0,
            "scale": {"x": 0.5, "y": 0.6},
            "position": {"x": 0.2, "y": -0.3},
            "flip": {"horizontal": True, "vertical": False},
        }
    ]


def test_build_timeline_preserves_jianying_authoring_extensions():
    video = {
        "source_path": "/source.mp4",
        "source_start": 0,
        "source_end": 4,
        "timeline_start": 0,
        "timeline_end": 2,
        "speed": 2.0,
        "reverse": True,
        "reverse_path": "/source-reversed.mp4",
        "opacity": 0.7,
        "position": {"x": 0.2, "y": -0.1},
        "transition": "fade-package",
    }
    resource_packages = {
        "sticker-package": {"main_config": {"type": "sticker"}},
        "fade-package": {"main_config": {"type": "transition"}},
    }
    style_presets = {"title": {"font_size": 24, "fill_color": "#FF0000"}}
    sticker_track = {
        "kind": "sticker",
        "name": "sticker",
        "segments": [
            {
                "timeline_start": 0,
                "timeline_end": 1,
                "resource_package": "sticker-package",
            }
        ],
    }

    timeline = build_timeline(
        {"width": 1280, "height": 720, "fps": 30},
        2.0,
        [video],
        [],
        resource_packages=resource_packages,
        style_presets=style_presets,
        extra_tracks=[sticker_track],
    )

    authored = _track(timeline, "video")["clips"][0]
    extension_keys = (
        "speed",
        "reverse",
        "reverse_path",
        "opacity",
        "position",
        "transition",
    )
    assert {key: authored[key] for key in extension_keys} == {
        key: video[key] for key in extension_keys
    }
    assert timeline["resource_packages"] == resource_packages
    assert timeline["style_presets"] == style_presets
    assert _track(timeline, "sticker") == sticker_track


def test_ducking_keyframes_holds_idle_and_dips_under_window():
    kfs = ducking_keyframes(
        [(2.0, 4.0)], idle=0.85, duck=0.2, fade=0.25, span_start=0.0, span_end=10.0
    )
    assert kfs[0] == {"t": 0.0, "gain": 0.85}  # starts at idle
    assert kfs[-1] == {"t": 10.0, "gain": 0.85}  # ends at idle
    gains = {round(k["t"], 2): k["gain"] for k in kfs}
    assert gains[2.0] == 0.2 and gains[4.0] == 0.2  # ducked across the whole window
    assert gains[1.75] == 0.85 and gains[4.25] == 0.85  # ramp in before, release after
    assert kfs == sorted(kfs, key=lambda k: k["t"])  # monotonic in time


def test_ducking_keyframes_empty_without_windows():
    assert ducking_keyframes([], 0.85, 0.2, 0.25, 0.0, 5.0) == []


def test_ducking_keyframes_coalesces_near_adjacent_windows():
    # two back-to-back narration beats (gap < 2*fade) must stay ducked throughout —
    # no bounce up to idle between them.
    kfs = ducking_keyframes(
        [(2.0, 4.0), (4.0, 6.0)],
        idle=0.85,
        duck=0.2,
        fade=0.25,
        span_start=0.0,
        span_end=10.0,
    )
    inside = [k["gain"] for k in kfs if 2.0 <= k["t"] <= 6.0]
    assert inside and all(g == 0.2 for g in inside), (
        "duck must hold across adjacent beats"
    )


def test_ducking_keyframes_preserves_real_gaps():
    # a genuine gap (>= 2*fade) keeps the idle plateau so the original swells there.
    kfs = ducking_keyframes(
        [(2.0, 3.0), (7.0, 8.0)],
        idle=0.85,
        duck=0.2,
        fade=0.25,
        span_start=0.0,
        span_end=10.0,
    )
    gains = {round(k["t"], 2): k["gain"] for k in kfs}
    assert gains[3.25] == 0.85 and gains[6.75] == 0.85  # back to idle in the gap
    assert gains[3.0] == 0.2 and gains[7.0] == 0.2  # ducked under each beat


def test_ducking_keyframes_bridge_param_holds_across_wider_gaps():
    # an explicit bridge holds the duck across a gap that the default 2*fade would release:
    # the two beats coalesce into one held span [2,8], so no keyframe inside swells to idle.
    kfs = ducking_keyframes(
        [(2.0, 3.0), (7.0, 8.0)],
        idle=0.85,
        duck=0.2,
        fade=0.25,
        span_start=0.0,
        span_end=10.0,
        bridge=6.0,
    )
    held = [k["gain"] for k in kfs if 2.0 <= k["t"] <= 8.0]
    assert held and all(g == 0.2 for g in held), (
        "duck held across the bridged gap (no idle inside)"
    )
    gains = {round(k["t"], 2): k["gain"] for k in kfs}
    assert gains[2.0] == 0.2 and gains[8.0] == 0.2


def test_variable_ducking_bridges_gaps_below_bridge():
    # original-audio automation: a 3s gap with bridge=6 coalesces into one held span [2,9],
    # so no keyframe inside swells back to idle.
    kfs = variable_ducking_keyframes(
        [(2.0, 4.0, 0.2), (7.0, 9.0, 0.2)],
        idle=0.85,
        fade=0.25,
        span_start=0.0,
        span_end=12.0,
        bridge=6.0,
    )
    held = [k["gain"] for k in kfs if 2.0 <= k["t"] <= 9.0]
    assert held and all(g == 0.2 for g in held), (
        "duck held across the bridged gap (no idle inside)"
    )
    gains = {round(k["t"], 2): k["gain"] for k in kfs}
    assert gains[2.0] == 0.2 and gains[9.0] == 0.2


def test_variable_ducking_releases_gaps_at_or_above_bridge():
    # the same 3s gap with bridge=2 releases the original back to idle.
    kfs = variable_ducking_keyframes(
        [(2.0, 4.0, 0.2), (7.0, 9.0, 0.2)],
        idle=0.85,
        fade=0.25,
        span_start=0.0,
        span_end=12.0,
        bridge=2.0,
    )
    assert any(k["gain"] == 0.85 for k in kfs if 4.0 < k["t"] < 7.0), (
        "long gap must swell to idle"
    )


def test_variable_ducking_mixed_levels_coalesce_to_min_like_render():
    # Render/draft consistency: a bridged span mixing a speech beat (0.2) and a quiet beat
    # (0.12) must flatten to the MIN level across the whole span, exactly as the ffmpeg render
    # coalesces it — otherwise the 剪映 draft would play the original louder than the mp4.
    kfs = variable_ducking_keyframes(
        [(0.0, 2.0, 0.2), (4.0, 6.0, 0.12)],
        idle=0.85,
        fade=0.25,
        span_start=0.0,
        span_end=8.0,
        bridge=6.0,
    )
    held = [k["gain"] for k in kfs if 0.0 <= k["t"] <= 6.0]
    assert held and all(g == 0.12 for g in held), (
        "mixed bridged span holds at the min (0.12)"
    )


def test_build_timeline_has_all_tracks():
    canvas = {"width": 1280, "height": 720, "fps": 25}
    video = [
        {
            "source_path": "/src.mp4",
            "source_start": 10.0,
            "source_end": 20.0,
            "timeline_start": 0.0,
            "timeline_end": 10.0,
        }
    ]
    narr = [
        {
            "source_path": "/n0.wav",
            "timeline_start": 1.0,
            "timeline_end": 4.0,
            "text": "hello",
            "overlaps_speech": True,
        }
    ]
    bgm = {"source_path": "/bgm.mp3", "volume": 0.18, "ducking_volume": 0.1}
    ducking = {"idle": 0.85, "speech": 0.2, "quiet": 0.12, "fade": 0.25}
    tl = build_timeline(canvas, 10.0, video, narr, bgm=bgm, ducking=ducking)

    kinds = [t["kind"] for t in tl["tracks"]]
    assert kinds == ["video", "audio", "audio", "text"]
    vt = tl["tracks"][0]
    assert vt["clips"][0]["audio"]["volume_keyframes"], (
        "video clip carries original ducking"
    )
    bgm_track = [t for t in tl["tracks"] if t.get("role") == "bgm"][0]
    assert bgm_track["segments"][0]["volume_keyframes"], "bgm bed carries ducking"
    assert tl["duration"] == 10.0 and tl["canvas"]["width"] == 1280


def test_build_timeline_without_ducking_is_flat():
    tl = build_timeline(
        {"width": 1, "height": 1, "fps": 30},
        5.0,
        [
            {
                "source_path": "/s.mp4",
                "source_start": 0.0,
                "source_end": 5.0,
                "timeline_start": 0.0,
                "timeline_end": 5.0,
            }
        ],
        [
            {
                "source_path": "/n.wav",
                "timeline_start": 0.0,
                "timeline_end": 2.0,
                "text": "t",
            }
        ],
        bgm=None,
        ducking=None,
    )
    assert tl["tracks"][0]["clips"][0]["audio"]["volume_keyframes"] == []
    # no bgm track when bgm is None
    assert all(t.get("role") != "bgm" for t in tl["tracks"])


def test_build_timeline_skips_empty_narration_track():
    tl = build_timeline(
        {"width": 1, "height": 1, "fps": 30},
        5.0,
        [
            {
                "source_path": "/s.mp4",
                "source_start": 0.0,
                "source_end": 5.0,
                "timeline_start": 0.0,
                "timeline_end": 5.0,
            }
        ],
        [],
        bgm=None,
        ducking=None,
    )
    assert [t["kind"] for t in tl["tracks"]] == ["video"]  # no empty audio/text tracks


def test_timeline_round_trips(tmp_path):
    tl = build_timeline(
        {"width": 100, "height": 100, "fps": 24},
        3.0,
        [
            {
                "source_path": "/s.mp4",
                "source_start": 0.0,
                "source_end": 3.0,
                "timeline_start": 0.0,
                "timeline_end": 3.0,
            }
        ],
        [
            {
                "source_path": "/n.wav",
                "timeline_start": 0.5,
                "timeline_end": 2.0,
                "text": "x",
            }
        ],
    )
    p = tmp_path / "timeline.json"
    save_timeline(tl, p)
    assert load_timeline(p) == tl
    assert json.loads(p.read_text(encoding="utf-8")) == tl


def test_build_timeline_uses_quiet_ducking_gain_for_quiet_segments():
    tl = build_timeline(
        {"width": 1280, "height": 720, "fps": 25},
        10.0,
        [
            {
                "source_path": "/src.mp4",
                "source_start": 0.0,
                "source_end": 10.0,
                "timeline_start": 0.0,
                "timeline_end": 10.0,
            }
        ],
        [
            {
                "source_path": "/n0.wav",
                "timeline_start": 2.0,
                "timeline_end": 4.0,
                "text": "quiet",
                "overlaps_speech": False,
            }
        ],
        ducking={"idle": 0.85, "speech": 0.2, "quiet": 0.12, "fade": 0.25},
    )

    keyframes = tl["tracks"][0]["clips"][0]["audio"]["volume_keyframes"]
    gains = [kf["gain"] for kf in keyframes]
    assert 0.12 in gains
    assert 0.2 not in gains


def test_variable_ducking_keyframes_do_not_release_to_idle_between_close_windows():
    keyframes = variable_ducking_keyframes(
        [(1.0, 4.0, 0.2), (4.1, 5.0, 0.12)],
        idle=0.85,
        fade=0.25,
        span_start=0.0,
        span_end=8.0,
    )

    # the two close windows (gap 0.1 < 2*fade) coalesce into one held span [1,5] at the min
    # level — the duck never swells back to idle between them.
    held = [kf["gain"] for kf in keyframes if 1.0 <= kf["t"] <= 5.0]
    assert held and all(g == 0.12 for g in held)


def test_build_video_clips_prefers_per_clip_source_path_without_explicit_source_video(
    monkeypatch, tmp_path
):
    sys.path.insert(
        0,
        str(
            Path(__file__).resolve().parents[2]
            / "skills"
            / "video-assemble"
            / "scripts"
        ),
    )
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    rendered = tmp_path / "edited_source.mp4"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    rendered.write_bytes(b"e")
    work = tmp_path / "work"
    work.mkdir()
    (work / "clip_plan_validated.json").write_text(
        json.dumps(
            {
                "clips": [
                    {
                        "source_path": str(a),
                        "source_start": 1.0,
                        "source_end": 2.0,
                        "output_start": 0.0,
                        "output_end": 1.0,
                    },
                    {
                        "source_path": str(b),
                        "source_start": 3.0,
                        "source_end": 5.0,
                        "output_start": 1.0,
                        "output_end": 3.0,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setitem(CONFIG, "source_video_explicit", False)
    monkeypatch.setitem(CONFIG, "source_video", "")

    clips = media._build_video_clips(rendered, work, 3.0)

    assert [c["source_path"] for c in clips] == [str(a), str(b)]
    assert clips[1]["timeline_start"] == 1.0


def test_build_video_clips_degrades_only_missing_clip_keeps_present_provenance(
    monkeypatch, tmp_path
):
    """One stale source must degrade ONLY its own clip; clips whose source is present keep
    their real source_id/source_path provenance (no whole-timeline collapse)."""
    sys.path.insert(
        0,
        str(
            Path(__file__).resolve().parents[2]
            / "skills"
            / "video-assemble"
            / "scripts"
        ),
    )
    rendered = tmp_path / "edited_source.mp4"
    rendered.write_bytes(b"e")
    present = tmp_path / "present.mp4"
    present.write_bytes(b"p")
    work = tmp_path / "work"
    work.mkdir()
    (work / "clip_plan_validated.json").write_text(
        json.dumps(
            {
                "clips": [
                    {
                        "source_id": "src_miss",
                        "source_path": str(tmp_path / "missing.mp4"),
                        "source_start": 1.0,
                        "source_end": 2.0,
                        "output_start": 0.0,
                        "output_end": 1.0,
                    },
                    {
                        "source_id": "src_ok",
                        "source_path": str(present),
                        "source_start": 3.0,
                        "source_end": 5.0,
                        "output_start": 1.0,
                        "output_end": 3.0,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setitem(CONFIG, "source_video_explicit", False)
    monkeypatch.setitem(CONFIG, "source_video", "")

    clips = media._build_video_clips(rendered, work, 3.0)

    assert len(clips) == 2
    ok = clips[1]
    assert ok["source_path"] == str(present) and ok["source_id"] == "src_ok"
    assert ok["source_start"] == 3.0 and ok["source_end"] == 5.0
    assert not ok.get("provenance_degraded")
    bad = clips[0]
    assert bad["provenance_degraded"] is True and bad["source_id"] == "src_miss"
    assert bad["source_path"] == str(rendered)
    assert bad["timeline_start"] == 0.0 and bad["timeline_end"] == 1.0
    assert bad["provenance_reason"].startswith("missing_source_path:")


def test_emit_timeline_marks_degraded_multi_source_fallback(monkeypatch, tmp_path):
    sys.path.insert(
        0,
        str(
            Path(__file__).resolve().parents[2]
            / "skills"
            / "video-assemble"
            / "scripts"
        ),
    )
    rendered = tmp_path / "edited_source.mp4"
    rendered.write_bytes(b"e")
    work = tmp_path / "work"
    work.mkdir()
    (work / "clip_plan_validated.json").write_text(
        json.dumps(
            {
                "clips": [
                    {
                        "source_path": str(tmp_path / "missing.mp4"),
                        "source_start": 1.0,
                        "source_end": 2.0,
                        "output_start": 0.0,
                        "output_end": 1.0,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setitem(CONFIG, "source_video_explicit", False)
    monkeypatch.setitem(CONFIG, "source_video", "")
    monkeypatch.setattr(
        timeline_emit,
        "_probe_canvas",
        lambda _path: {"width": 100, "height": 100, "fps": 25},
    )
    monkeypatch.setattr(timeline_emit, "_combined_subtitle_entries", lambda *_args: [])

    timeline = timeline_emit._emit_timeline(rendered, [], work, 3.0, has_bgm=False)

    assert timeline["provenance"]["degraded"] is True
    assert timeline["provenance"]["degraded_clips"][0]["reason"].startswith(
        "missing_source_path:"
    )
    assert (
        json.loads((work / "timeline.json").read_text(encoding="utf-8"))["provenance"][
            "degraded"
        ]
        is True
    )


def _eval_filter_gain(filter_complex, t):
    start = filter_complex.index("volume='") + len("volume='")
    end = filter_complex.index("':eval=frame", start)
    expr = filter_complex[start:end]
    return eval(expr, {"__builtins__": {}}, {"t": float(t), "min": min, "max": max})


def _interp_keyframe_gain(keyframes, t):
    pts = sorted((float(k["t"]), float(k["gain"])) for k in keyframes)
    if t <= pts[0][0]:
        return pts[0][1]
    for (t0, g0), (t1, g1) in zip(pts, pts[1:]):
        if t <= t1:
            if abs(t1 - t0) < 1e-9:
                return g1
            ratio = (t - t0) / (t1 - t0)
            return g0 + (g1 - g0) * ratio
    return pts[-1][1]


def test_p0_ducking_ffmpeg_expression_matches_timeline_keyframes(monkeypatch):
    """Rendered mp4 ducking and timeline/JianYing ducking share the same outside-ramp semantics."""
    monkeypatch.setitem(CONFIG, "ducking_mode", "fixed")
    monkeypatch.setitem(CONFIG, "idle_orig_volume", 0.85)
    monkeypatch.setitem(CONFIG, "speech_ducking_volume", 0.2)
    monkeypatch.setitem(CONFIG, "zone_ducking_volume", 0.12)
    monkeypatch.setitem(CONFIG, "duck_fade_seconds", 0.25)
    monkeypatch.setitem(CONFIG, "duck_bridge_seconds", 6.0)
    segments = [
        {"actual_place_start": 2.0, "actual_place_end": 4.0, "overlaps_speech": True},
        {"actual_place_start": 6.0, "actual_place_end": 8.0, "overlaps_speech": False},
    ]

    fc = audio_mix._build_audio_filter_complex(segments)
    kfs = variable_ducking_keyframes(
        [(2.0, 4.0, 0.2), (6.0, 8.0, 0.12)],
        idle=0.85,
        fade=0.25,
        span_start=0.0,
        span_end=10.0,
        bridge=6.0,
    )

    # Mixed-level bridged windows flatten to the min level (0.12) and ramp outside [s,e].
    for t in [1.75, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 8.25]:
        assert _eval_filter_gain(fc, t) == pytest.approx(
            _interp_keyframe_gain(kfs, t), abs=0.015
        )
