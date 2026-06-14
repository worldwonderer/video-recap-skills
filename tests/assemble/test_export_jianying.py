import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'video-assemble' / 'scripts'))
import json  # noqa: E402
from export_jianying import build_draft, export_timeline_to_jianying, _us  # noqa: E402
from timeline import build_timeline  # noqa: E402


def _counter_ids():
    n = [0]

    def nid():
        n[0] += 1
        return f"ID{n[0]:05d}"
    return nid


def _fake_probe(_path):
    return 600_000_000, 1920, 1080  # 600s, 1920x1080 — deterministic, no ffprobe


def _sample_timeline():
    canvas = {"width": 1280, "height": 720, "fps": 25}
    video = [
        {"source_path": "/orig.mp4", "source_start": 10.0, "source_end": 20.0,
         "timeline_start": 0.0, "timeline_end": 10.0},
        {"source_path": "/orig.mp4", "source_start": 40.0, "source_end": 45.0,
         "timeline_start": 10.0, "timeline_end": 15.0},
    ]
    narr = [{"source_path": "/n0.wav", "timeline_start": 1.0, "timeline_end": 4.0,
             "text": "第一句", "overlaps_speech": True}]
    bgm = {"source_path": "/bgm.mp3", "volume": 0.18, "ducking_volume": 0.1}
    ducking = {"idle": 0.85, "speech": 0.2, "quiet": 0.12, "fade": 0.25}
    return build_timeline(canvas, 15.0, video, narr, bgm=bgm, ducking=ducking)


def test_us_is_integer_microseconds():
    assert _us(5.0) == 5_000_000
    assert _us(1.2345) == 1_234_500
    assert isinstance(_us(3.1), int)


def test_build_draft_structure_and_tracks():
    content, meta, _notes = build_draft(_sample_timeline(), new_id=_counter_ids(), probe=_fake_probe)
    assert content["version"] == 360000
    assert content["canvas_config"] == {"width": 1280, "height": 720, "ratio": "original"}
    assert content["duration"] == 15_000_000           # µs
    m = content["materials"]
    assert len(m["videos"]) == 2 and len(m["audios"]) == 2 and len(m["texts"]) == 1
    assert len(m["speeds"]) == 4                        # one per media segment: 2 video + 1 narration + 1 bgm
    types = [t["type"] for t in content["tracks"]]
    assert types == ["video", "audio", "audio", "text"]
    # main video track sits at render_index 0, text on top
    assert content["tracks"][0]["segments"][0]["render_index"] == 0
    assert content["tracks"][-1]["segments"][0]["render_index"] == 15000


def test_draft_times_are_all_integer_microseconds():
    content, _meta, _ = build_draft(_sample_timeline(), new_id=_counter_ids(), probe=_fake_probe)

    def check(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("start", "duration", "time_offset"):
                    assert isinstance(v, int), f"{k}={v!r} must be int µs"
                check(v)
        elif isinstance(o, list):
            for x in o:
                check(x)
    check(content)
    # a video clip placed at source 10s for 10s
    vseg = content["tracks"][0]["segments"][0]
    assert vseg["target_timerange"] == {"start": 0, "duration": 10_000_000}
    assert vseg["source_timerange"] == {"start": 10_000_000, "duration": 10_000_000}


def test_ducking_becomes_volume_keyframes():
    content, _m, _n = build_draft(_sample_timeline(), new_id=_counter_ids(), probe=_fake_probe)
    vseg = content["tracks"][0]["segments"][0]
    kf_lists = vseg["common_keyframes"]
    assert kf_lists and kf_lists[0]["property_type"] == "KFTypeVolume"
    vals = [k["values"][0] for k in kf_lists[0]["keyframe_list"]]
    assert 0.85 in vals and 0.2 in vals               # idle + ducked
    assert all(isinstance(k["time_offset"], int) for k in kf_lists[0]["keyframe_list"])


def test_export_writes_three_files(tmp_path):
    draft_dir, notes = export_timeline_to_jianying(
        _sample_timeline(), str(tmp_path), draft_name="recap_demo",
        new_id=_counter_ids(), probe=_fake_probe)
    files = sorted(p.name for p in Path(draft_dir).iterdir())
    assert files == ["draft_content.json", "draft_info.json", "draft_meta_info.json"]
    content = json.loads((Path(draft_dir) / "draft_content.json").read_text(encoding="utf-8"))
    info = json.loads((Path(draft_dir) / "draft_info.json").read_text(encoding="utf-8"))
    assert content == info                              # dual-file compatibility
    meta = json.loads((Path(draft_dir) / "draft_meta_info.json").read_text(encoding="utf-8"))
    assert meta["draft_name"] == "recap_demo" and meta["tm_duration"] == 15_000_000


def test_export_uses_collision_safe_draft_folder(tmp_path):
    existing = tmp_path / "recap_demo"
    existing.mkdir()
    (existing / "draft_content.json").write_text("manual edit", encoding="utf-8")

    draft_dir, notes = export_timeline_to_jianying(
        _sample_timeline(), str(tmp_path), draft_name="recap_demo",
        new_id=_counter_ids(), probe=_fake_probe)

    assert Path(draft_dir).name == "recap_demo_2"
    assert (existing / "draft_content.json").read_text(encoding="utf-8") == "manual edit"
    assert any("避免覆盖" in note for note in notes)
    assert (Path(draft_dir) / "draft_content.json").exists()


def test_exporter_handles_timeline_without_bgm():
    tl = build_timeline({"width": 100, "height": 100, "fps": 30}, 5.0,
                        [{"source_path": "/s.mp4", "source_start": 0.0, "source_end": 5.0,
                          "timeline_start": 0.0, "timeline_end": 5.0}],
                        [{"source_path": "/n.wav", "timeline_start": 0.0, "timeline_end": 2.0,
                          "text": "x"}], bgm=None, ducking=None)
    content, _m, _n = build_draft(tl, new_id=_counter_ids(), probe=_fake_probe)
    assert [t["type"] for t in content["tracks"]] == ["video", "audio", "text"]


def test_bundle_media_copies_and_rewrites_paths(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    vid, wav = src / "orig.mp4", src / "n0.wav"
    vid.write_bytes(b"video")
    wav.write_bytes(b"audio")
    tl = build_timeline({"width": 100, "height": 100, "fps": 30}, 5.0,
                        [{"source_path": str(vid), "source_start": 0.0, "source_end": 5.0,
                          "timeline_start": 0.0, "timeline_end": 5.0}],
                        [{"source_path": str(wav), "timeline_start": 0.0, "timeline_end": 2.0,
                          "text": "x"}], bgm=None, ducking=None)
    draft_dir, _notes = export_timeline_to_jianying(
        tl, str(tmp_path / "out"), draft_name="d", new_id=_counter_ids(),
        probe=_fake_probe, bundle_media=True)
    mats = Path(draft_dir) / "materials"
    assert (mats / "orig.mp4").exists() and (mats / "n0.wav").exists()
    content = json.loads((Path(draft_dir) / "draft_content.json").read_text(encoding="utf-8"))
    for m in content["materials"]["videos"] + content["materials"]["audios"]:
        assert m["path"].startswith(str(mats)), "material paths rewritten into the bundle"


def test_exporter_skips_empty_audio_track():
    tl = {"schema_version": 1, "canvas": {"width": 100, "height": 100, "fps": 30},
          "duration": 5.0, "tracks": [
              {"kind": "video", "name": "video", "clips": [
                  {"source_path": "/s.mp4", "source_start": 0.0, "source_end": 5.0,
                   "timeline_start": 0.0, "timeline_end": 5.0,
                   "audio": {"role": "original", "base_gain": 1.0, "volume_keyframes": []}}]},
              {"kind": "audio", "name": "narration", "role": "narration", "segments": []}]}
    content, _m, _n = build_draft(tl, new_id=_counter_ids(), probe=_fake_probe)
    assert [t["type"] for t in content["tracks"]] == ["video"]   # empty audio track dropped


def test_core_assemble_does_not_import_exporter():
    # The exporter must stay optional: importing the render path must NOT pull it in.
    # Run in a clean interpreter so we don't perturb this process's already-imported modules.
    import subprocess
    scripts = str(Path(__file__).resolve().parents[2] / 'skills' / 'video-assemble' / 'scripts')
    code = (f"import sys; sys.path.insert(0, {scripts!r}); import assemble; "
            "assert 'export_jianying' not in sys.modules, 'core imported the 剪映 exporter'")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr



def test_exporter_repeats_looped_bgm_to_cover_timeline(tmp_path):
    bgm = tmp_path / "bgm.mp3"
    bgm.write_bytes(b"bgm")

    def short_bgm_probe(path):
        if str(path).endswith("bgm.mp3"):
            return 5_000_000, 0, 0
        return 15_000_000, 1920, 1080

    timeline = _sample_timeline()
    for track in timeline["tracks"]:
        if track.get("role") == "bgm":
            track["segments"][0]["source_path"] = str(bgm)
    content, _meta, notes = build_draft(timeline, new_id=_counter_ids(), probe=short_bgm_probe)
    bgm_track = next(t for t in content["tracks"] if t["type"] == "audio" and t["name"] == "bgm")
    starts = [seg["target_timerange"]["start"] for seg in bgm_track["segments"]]
    durations = [seg["target_timerange"]["duration"] for seg in bgm_track["segments"]]

    assert starts == [0, 5_000_000, 10_000_000]
    assert durations == [5_000_000, 5_000_000, 5_000_000]
    assert sum(durations) == 15_000_000
    assert not any("BGM 素材" in note for note in notes)


def test_looped_bgm_keyframes_are_windowed_per_repeated_piece(tmp_path):
    bgm = tmp_path / "bgm.mp3"
    bgm.write_bytes(b"bgm")

    def short_bgm_probe(path):
        if str(path).endswith("bgm.mp3"):
            return 5_000_000, 0, 0
        return 15_000_000, 1920, 1080

    timeline = _sample_timeline()
    for track in timeline["tracks"]:
        if track.get("role") == "bgm":
            track["segments"][0]["source_path"] = str(bgm)
    content, _meta, _notes = build_draft(timeline, new_id=_counter_ids(), probe=short_bgm_probe)
    bgm_track = next(t for t in content["tracks"] if t["type"] == "audio" and t["name"] == "bgm")
    keyframe_counts = [len(seg["common_keyframes"]) for seg in bgm_track["segments"]]

    assert keyframe_counts[0] == 1
    assert keyframe_counts[1:] == [0, 0]
