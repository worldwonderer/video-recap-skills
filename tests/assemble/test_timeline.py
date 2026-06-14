import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'video-assemble' / 'scripts'))
import json  # noqa: E402
from timeline import build_timeline, ducking_keyframes, load_timeline, save_timeline, variable_ducking_keyframes  # noqa: E402


def test_ducking_keyframes_holds_idle_and_dips_under_window():
    kfs = ducking_keyframes([(2.0, 4.0)], idle=0.85, duck=0.2, fade=0.25,
                            span_start=0.0, span_end=10.0)
    assert kfs[0] == {"t": 0.0, "gain": 0.85}        # starts at idle
    assert kfs[-1] == {"t": 10.0, "gain": 0.85}      # ends at idle
    gains = {round(k["t"], 2): k["gain"] for k in kfs}
    assert gains[2.0] == 0.2 and gains[4.0] == 0.2   # ducked across the whole window
    assert gains[1.75] == 0.85 and gains[4.25] == 0.85  # ramp in before, release after
    assert kfs == sorted(kfs, key=lambda k: k["t"])  # monotonic in time


def test_ducking_keyframes_empty_without_windows():
    assert ducking_keyframes([], 0.85, 0.2, 0.25, 0.0, 5.0) == []


def test_ducking_keyframes_coalesces_near_adjacent_windows():
    # two back-to-back narration beats (gap < 2*fade) must stay ducked throughout —
    # no bounce up to idle between them.
    kfs = ducking_keyframes([(2.0, 4.0), (4.0, 6.0)], idle=0.85, duck=0.2,
                            fade=0.25, span_start=0.0, span_end=10.0)
    inside = [k["gain"] for k in kfs if 2.0 <= k["t"] <= 6.0]
    assert inside and all(g == 0.2 for g in inside), "duck must hold across adjacent beats"


def test_ducking_keyframes_preserves_real_gaps():
    # a genuine gap (>= 2*fade) keeps the idle plateau so the original swells there.
    kfs = ducking_keyframes([(2.0, 3.0), (7.0, 8.0)], idle=0.85, duck=0.2,
                            fade=0.25, span_start=0.0, span_end=10.0)
    gains = {round(k["t"], 2): k["gain"] for k in kfs}
    assert gains[3.25] == 0.85 and gains[6.75] == 0.85   # back to idle in the gap
    assert gains[3.0] == 0.2 and gains[7.0] == 0.2       # ducked under each beat


def test_build_timeline_has_all_tracks():
    canvas = {"width": 1280, "height": 720, "fps": 25}
    video = [{"source_path": "/src.mp4", "source_start": 10.0, "source_end": 20.0,
              "timeline_start": 0.0, "timeline_end": 10.0}]
    narr = [{"source_path": "/n0.wav", "timeline_start": 1.0, "timeline_end": 4.0,
             "text": "hello", "overlaps_speech": True}]
    bgm = {"source_path": "/bgm.mp3", "volume": 0.18, "ducking_volume": 0.1}
    ducking = {"idle": 0.85, "speech": 0.2, "quiet": 0.12, "fade": 0.25}
    tl = build_timeline(canvas, 10.0, video, narr, bgm=bgm, ducking=ducking)

    kinds = [t["kind"] for t in tl["tracks"]]
    assert kinds == ["video", "audio", "audio", "text"]
    vt = tl["tracks"][0]
    assert vt["clips"][0]["audio"]["volume_keyframes"], "video clip carries original ducking"
    bgm_track = [t for t in tl["tracks"] if t.get("role") == "bgm"][0]
    assert bgm_track["segments"][0]["volume_keyframes"], "bgm bed carries ducking"
    assert tl["duration"] == 10.0 and tl["canvas"]["width"] == 1280


def test_build_timeline_without_ducking_is_flat():
    tl = build_timeline({"width": 1, "height": 1, "fps": 30}, 5.0,
                        [{"source_path": "/s.mp4", "source_start": 0.0, "source_end": 5.0,
                          "timeline_start": 0.0, "timeline_end": 5.0}],
                        [{"source_path": "/n.wav", "timeline_start": 0.0, "timeline_end": 2.0,
                          "text": "t"}], bgm=None, ducking=None)
    assert tl["tracks"][0]["clips"][0]["audio"]["volume_keyframes"] == []
    # no bgm track when bgm is None
    assert all(t.get("role") != "bgm" for t in tl["tracks"])


def test_build_timeline_skips_empty_narration_track():
    tl = build_timeline({"width": 1, "height": 1, "fps": 30}, 5.0,
                        [{"source_path": "/s.mp4", "source_start": 0.0, "source_end": 5.0,
                          "timeline_start": 0.0, "timeline_end": 5.0}],
                        [], bgm=None, ducking=None)
    assert [t["kind"] for t in tl["tracks"]] == ["video"]   # no empty audio/text tracks


def test_timeline_round_trips(tmp_path):
    tl = build_timeline({"width": 100, "height": 100, "fps": 24}, 3.0,
                        [{"source_path": "/s.mp4", "source_start": 0.0, "source_end": 3.0,
                          "timeline_start": 0.0, "timeline_end": 3.0}],
                        [{"source_path": "/n.wav", "timeline_start": 0.5, "timeline_end": 2.0,
                          "text": "x"}])
    p = tmp_path / "timeline.json"
    save_timeline(tl, p)
    assert load_timeline(p) == tl
    assert json.loads(p.read_text(encoding="utf-8")) == tl



def test_build_timeline_uses_quiet_ducking_gain_for_quiet_segments():
    tl = build_timeline(
        {"width": 1280, "height": 720, "fps": 25},
        10.0,
        [{"source_path": "/src.mp4", "source_start": 0.0, "source_end": 10.0,
          "timeline_start": 0.0, "timeline_end": 10.0}],
        [{"source_path": "/n0.wav", "timeline_start": 2.0, "timeline_end": 4.0,
          "text": "quiet", "overlaps_speech": False}],
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

    between = [kf for kf in keyframes if 4.0 < kf["t"] < 5.0]
    assert between
    assert all(kf["gain"] != 0.85 for kf in between)
