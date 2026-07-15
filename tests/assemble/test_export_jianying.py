import sys
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[2] / "skills" / "video-assemble" / "scripts"),
)
import json  # noqa: E402
import pytest  # noqa: E402
from export_jianying import build_draft, export_timeline_to_jianying, us  # noqa: E402
from jianying_schema import (  # noqa: E402
    MATERIAL_KEYS,
    material_category_registry,
    validate_material_category,
)
from jianying_tracks import TRACK_LAYOUT_BANDS, TrackAllocator  # noqa: E402
import jianying_writer  # noqa: E402
from timeline import build_timeline  # noqa: E402


def _draft_texts(content):
    return [
        json.loads(item["content"])["text"] for item in content["materials"]["texts"]
    ]


def test_exporter_uses_timeline_display_subtitles_not_raw_narration_text():
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
                "text": "第一句。",
                "overlaps_speech": True,
            }
        ],
        subtitle_segments=[
            {"text": "第一句", "timeline_start": 0.0, "timeline_end": 2.0}
        ],
    )

    content, _meta, _notes = build_draft(tl, new_id=_counter_ids(), probe=_fake_probe)

    assert _draft_texts(content) == ["第一句"]


def test_exporter_keeps_original_gap_display_subtitles_from_timeline():
    tl = build_timeline(
        {"width": 100, "height": 100, "fps": 30},
        6.0,
        [
            {
                "source_path": "/s.mp4",
                "source_start": 0.0,
                "source_end": 6.0,
                "timeline_start": 0.0,
                "timeline_end": 6.0,
            }
        ],
        [
            {
                "source_path": "/n.wav",
                "timeline_start": 3.0,
                "timeline_end": 5.0,
                "text": "旁白原文。",
                "overlaps_speech": True,
            }
        ],
        subtitle_segments=[
            {"text": "「他说：「你好」」", "timeline_start": 0.5, "timeline_end": 2.0},
            {"text": "旁白原文", "timeline_start": 3.0, "timeline_end": 5.0},
        ],
    )

    content, _meta, _notes = build_draft(tl, new_id=_counter_ids(), probe=_fake_probe)

    assert _draft_texts(content) == ["「他说：「你好」」", "旁白原文"]


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
        {
            "source_path": "/orig.mp4",
            "source_start": 10.0,
            "source_end": 20.0,
            "timeline_start": 0.0,
            "timeline_end": 10.0,
        },
        {
            "source_path": "/orig.mp4",
            "source_start": 40.0,
            "source_end": 45.0,
            "timeline_start": 10.0,
            "timeline_end": 15.0,
        },
    ]
    narr = [
        {
            "source_path": "/n0.wav",
            "timeline_start": 1.0,
            "timeline_end": 4.0,
            "text": "第一句",
            "overlaps_speech": True,
        }
    ]
    bgm = {"source_path": "/bgm.mp3", "volume": 0.18, "ducking_volume": 0.1}
    ducking = {"idle": 0.85, "speech": 0.2, "quiet": 0.12, "fade": 0.25}
    return build_timeline(canvas, 15.0, video, narr, bgm=bgm, ducking=ducking)


def test_us_is_integer_microseconds():
    assert us(5.0) == 5_000_000
    assert us(1.2345) == 1_234_500
    assert isinstance(us(3.1), int)


def test_draft_root_schema_material_keys_and_meta_shape():
    content, meta, _notes = build_draft(
        _sample_timeline(), new_id=_counter_ids(), probe=_fake_probe
    )

    required_root_keys = {
        "canvas_config",
        "config",
        "duration",
        "fps",
        "keyframes",
        "materials",
        "tracks",
        "version",
        "new_version",
        "platform",
        "last_modified_platform",
    }
    assert required_root_keys <= set(content)
    assert set(MATERIAL_KEYS) <= set(content["materials"])
    assert all(isinstance(content["materials"][key], list) for key in MATERIAL_KEYS)

    assert {
        "draft_id",
        "draft_name",
        "draft_fold_path",
        "tm_duration",
        "draft_materials",
    } <= set(meta)
    assert meta["tm_duration"] == content["duration"]
    assert isinstance(meta["draft_materials"], list) and meta["draft_materials"]


def test_build_draft_structure_and_tracks():
    content, meta, _notes = build_draft(
        _sample_timeline(), new_id=_counter_ids(), probe=_fake_probe
    )
    assert content["version"] == 360000
    assert content["canvas_config"] == {
        "width": 1280,
        "height": 720,
        "ratio": "original",
    }
    assert content["duration"] == 15_000_000  # µs
    m = content["materials"]
    assert len(m["videos"]) == 2 and len(m["audios"]) == 2 and len(m["texts"]) == 1
    assert m["speeds"] == []  # duo-video emits speed only when rate != 1x
    types = [t["type"] for t in content["tracks"]]
    assert types == ["audio", "audio", "video", "text"]
    assert all(
        segment["render_index"] == 2 and segment["track_render_index"] == 2
        for track in content["tracks"]
        for segment in track["segments"]
    )
    assert [track["flag"] for track in content["tracks"]] == [0, 2, 0, 0]
    subtitle = content["tracks"][-1]["segments"][0]
    assert content["materials"]["texts"][0]["type"] == "subtitle"
    assert subtitle["source_timerange"] == subtitle["target_timerange"] | {"start": 0}


def test_draft_times_are_all_integer_microseconds():
    content, _meta, _ = build_draft(
        _sample_timeline(), new_id=_counter_ids(), probe=_fake_probe
    )

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
    vseg = next(t for t in content["tracks"] if t["name"] == "video")["segments"][0]
    assert vseg["target_timerange"] == {"start": 0, "duration": 10_000_000}
    assert vseg["source_timerange"] == {"start": 10_000_000, "duration": 10_000_000}


def test_ducking_becomes_volume_keyframes():
    content, _m, _n = build_draft(
        _sample_timeline(), new_id=_counter_ids(), probe=_fake_probe
    )
    vseg = next(t for t in content["tracks"] if t["name"] == "video")["segments"][0]
    kf_lists = vseg["common_keyframes"]
    assert kf_lists and kf_lists[0]["property_type"] == "KFTypeVolume"
    vals = [k["values"][0] for k in kf_lists[0]["keyframe_list"]]
    assert 0.85 in vals and 0.2 in vals  # idle + ducked
    assert all(isinstance(k["time_offset"], int) for k in kf_lists[0]["keyframe_list"])


def test_export_writes_three_files(tmp_path):
    draft_dir, notes = export_timeline_to_jianying(
        _sample_timeline(),
        str(tmp_path),
        draft_name="recap_demo",
        new_id=_counter_ids(),
        probe=_fake_probe,
    )
    files = sorted(p.name for p in Path(draft_dir).iterdir())
    assert files == ["draft_content.json", "draft_info.json", "draft_meta_info.json"]
    content = json.loads(
        (Path(draft_dir) / "draft_content.json").read_text(encoding="utf-8")
    )
    info = json.loads((Path(draft_dir) / "draft_info.json").read_text(encoding="utf-8"))
    assert content == info  # dual-file compatibility
    meta = json.loads(
        (Path(draft_dir) / "draft_meta_info.json").read_text(encoding="utf-8")
    )
    assert meta["draft_name"] == "recap_demo" and meta["tm_duration"] == 15_000_000


def test_export_rejects_draft_names_that_escape_output_parent(tmp_path):
    out = tmp_path / "out"
    unsafe_names = [
        "",
        "   ",
        ".",
        "..",
        "../escape",
        "nested/name",
        r"nested\name",
        str(tmp_path / "escape"),
    ]

    for draft_name in unsafe_names:
        with pytest.raises(ValueError):
            export_timeline_to_jianying(
                _sample_timeline(),
                str(out),
                draft_name=draft_name,
                new_id=_counter_ids(),
                probe=_fake_probe,
            )

    assert not out.exists()


def test_representative_parity_export_with_bundle_collision_loop_and_keyframes(
    tmp_path,
):
    src = tmp_path / "src"
    src.mkdir()
    vid = src / "orig.mp4"
    narr = src / "n0.wav"
    bgm = src / "bgm.mp3"
    vid.write_bytes(b"video")
    narr.write_bytes(b"narration")
    bgm.write_bytes(b"bgm")

    tl = _sample_timeline()
    for track in tl["tracks"]:
        if track.get("kind") == "video":
            for clip in track["clips"]:
                clip["source_path"] = str(vid)
        elif track.get("role") == "narration":
            track["segments"][0]["source_path"] = str(narr)
        elif track.get("role") == "bgm":
            track["segments"][0]["source_path"] = str(bgm)

    out = tmp_path / "out"
    existing = out / "recap_demo"
    existing.mkdir(parents=True)
    (existing / "draft_content.json").write_text("manual edit", encoding="utf-8")

    def short_bgm_probe(path):
        if str(path).endswith("bgm.mp3"):
            return 5_000_000, 0, 0
        return 600_000_000, 1920, 1080

    draft_dir, notes = export_timeline_to_jianying(
        tl,
        str(out),
        draft_name="recap_demo",
        new_id=_counter_ids(),
        probe=short_bgm_probe,
        bundle_media=True,
    )

    assert Path(draft_dir).name == "recap_demo_2"
    assert (existing / "draft_content.json").read_text(
        encoding="utf-8"
    ) == "manual edit"
    assert any("避免覆盖" in note for note in notes)

    draft_path = Path(draft_dir)
    content = json.loads(
        (draft_path / "draft_content.json").read_text(encoding="utf-8")
    )
    info = json.loads((draft_path / "draft_info.json").read_text(encoding="utf-8"))
    meta = json.loads((draft_path / "draft_meta_info.json").read_text(encoding="utf-8"))
    assert content == info
    assert meta["draft_name"] == "recap_demo_2"
    assert meta["draft_fold_path"] == str(draft_path)

    materials = content["materials"]
    assert len(materials["videos"]) == 2
    assert len(materials["audios"]) == 2
    assert len(materials["texts"]) == 1
    assert materials["speeds"] == []  # 1x media does not need auxiliary speed refs

    tracks = content["tracks"]
    assert [(t["type"], t["name"]) for t in tracks] == [
        ("audio", "narration"),
        ("audio", "bgm"),
        ("video", "video"),
        ("text", "subtitle"),
    ]
    assert {
        segment["render_index"] for track in tracks for segment in track["segments"]
    } == {2}
    assert len(next(t for t in tracks if t["name"] == "bgm")["segments"]) == 3

    assert (
        next(t for t in tracks if t["name"] == "video")["segments"][0][
            "common_keyframes"
        ][0]["property_type"]
        == "KFTypeVolume"
    )
    assert (
        next(t for t in tracks if t["name"] == "bgm")["segments"][0][
            "common_keyframes"
        ][0]["property_type"]
        == "KFTypeVolume"
    )
    for material in materials["videos"] + materials["audios"]:
        assert material["path"].startswith("##_draftpath_placeholder_")
        assert "##/Resources/local/" in material["path"]


def test_export_uses_collision_safe_draft_folder(tmp_path):
    existing = tmp_path / "recap_demo"
    existing.mkdir()
    (existing / "draft_content.json").write_text("manual edit", encoding="utf-8")

    draft_dir, notes = export_timeline_to_jianying(
        _sample_timeline(),
        str(tmp_path),
        draft_name="recap_demo",
        new_id=_counter_ids(),
        probe=_fake_probe,
    )

    assert Path(draft_dir).name == "recap_demo_2"
    assert (existing / "draft_content.json").read_text(
        encoding="utf-8"
    ) == "manual edit"
    assert any("避免覆盖" in note for note in notes)
    assert (Path(draft_dir) / "draft_content.json").exists()


def test_exporter_handles_timeline_without_bgm():
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
                "text": "x",
            }
        ],
        bgm=None,
        ducking=None,
    )
    content, _m, _n = build_draft(tl, new_id=_counter_ids(), probe=_fake_probe)
    assert [t["type"] for t in content["tracks"]] == ["audio", "video", "text"]


def test_bundle_media_copies_and_rewrites_paths(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    vid, wav = src / "orig.mp4", src / "n0.wav"
    vid.write_bytes(b"video")
    wav.write_bytes(b"audio")
    tl = build_timeline(
        {"width": 100, "height": 100, "fps": 30},
        5.0,
        [
            {
                "source_path": str(vid),
                "source_start": 0.0,
                "source_end": 5.0,
                "timeline_start": 0.0,
                "timeline_end": 5.0,
            }
        ],
        [
            {
                "source_path": str(wav),
                "timeline_start": 0.0,
                "timeline_end": 2.0,
                "text": "x",
            }
        ],
        bgm=None,
        ducking=None,
    )
    draft_dir, _notes = export_timeline_to_jianying(
        tl,
        str(tmp_path / "out"),
        draft_name="d",
        new_id=_counter_ids(),
        probe=_fake_probe,
        bundle_media=True,
    )
    resources = Path(draft_dir) / "Resources" / "local"
    assert (resources / "video" / "orig.mp4").exists()
    assert (resources / "audio" / "n0.wav").exists()
    content = json.loads(
        (Path(draft_dir) / "draft_content.json").read_text(encoding="utf-8")
    )
    for m in content["materials"]["videos"] + content["materials"]["audios"]:
        assert m["path"].startswith("##_draftpath_placeholder_")
        assert "##/Resources/local/" in m["path"]


def test_exporter_skips_empty_audio_track():
    tl = {
        "schema_version": 1,
        "canvas": {"width": 100, "height": 100, "fps": 30},
        "duration": 5.0,
        "tracks": [
            {
                "kind": "video",
                "name": "video",
                "clips": [
                    {
                        "source_path": "/s.mp4",
                        "source_start": 0.0,
                        "source_end": 5.0,
                        "timeline_start": 0.0,
                        "timeline_end": 5.0,
                        "audio": {
                            "role": "original",
                            "base_gain": 1.0,
                            "volume_keyframes": [],
                        },
                    }
                ],
            },
            {"kind": "audio", "name": "narration", "role": "narration", "segments": []},
        ],
    }
    content, _m, _n = build_draft(tl, new_id=_counter_ids(), probe=_fake_probe)
    assert [t["type"] for t in content["tracks"]] == [
        "video"
    ]  # empty audio track dropped


def test_core_assemble_does_not_import_exporter():
    # The exporter must stay optional: importing the render path must NOT pull it in.
    # Run in a clean interpreter so we don't perturb this process's already-imported modules.
    import subprocess

    scripts = str(
        Path(__file__).resolve().parents[2] / "skills" / "video-assemble" / "scripts"
    )
    code = (
        f"import sys; sys.path.insert(0, {scripts!r}); import assemble; "
        "assert 'export_jianying' not in sys.modules, 'core imported the 剪映 exporter'; "
        "assert not any(name.startswith('jianying') for name in sys.modules), "
        "'core imported JianYing helper modules'"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_material_category_registry_matches_implemented_builders():
    categories = material_category_registry()
    assert categories["video"]["status"] == "supported"
    assert categories["audio"]["status"] == "supported"
    assert categories["text"]["status"] == "supported"
    assert categories["subtitle"]["status"] == "supported"
    assert categories["speed"]["status"] == "supported_auxiliary"
    assert categories["image"] == {
        "status": "supported",
        "materials_key": "videos",
        "track_type": "video",
    }
    for supported in (
        "sticker",
        "sound",
        "text_template",
        "lut",
        "transition",
        "video_effect",
        "face_effect",
        "mask",
        "style",
        "chroma",
        "green_screen",
        "compound",
    ):
        assert categories[supported]["status"].startswith("supported")
    for offline_payload in (
        "sticker",
        "sound",
        "text_template",
        "lut",
        "transition",
        "video_effect",
        "face_effect",
        "mask",
        "chroma",
    ):
        assert categories[offline_payload]["status"] == "supported_offline_payload"
    assert categories["face_effect"]["materials_key"] == "video_effects"
    assert categories["style"]["materials_key"] is None
    unsupported = validate_material_category("transition")
    assert unsupported["supported"] is True
    assert unsupported["status"] == "supported_offline_payload"


def test_local_image_overlay_builds_photo_material_and_overlap_safe_tracks():
    tl = {
        "schema_version": 1,
        "canvas": {"width": 100, "height": 100, "fps": 30},
        "duration": 3.0,
        "tracks": [
            {
                "kind": "image",
                "name": "overlay",
                "segments": [
                    {
                        "source_path": "/overlay.png",
                        "timeline_start": 0.0,
                        "timeline_end": 2.0,
                        "opacity": 0.75,
                        "rotation_degrees": 15,
                        "scale": {"x": 0.5, "y": 0.6},
                        "position": {"x": 0.25, "y": -0.4},
                        "flip": {"horizontal": True, "vertical": False},
                    },
                    {
                        "source_path": "/overlay-2.png",
                        "timeline_start": 1.0,
                        "timeline_end": 3.0,
                    },
                ],
            }
        ],
    }

    content, _meta, notes = build_draft(tl, new_id=_counter_ids(), probe=_fake_probe)

    assert notes == []
    assert content["materials"]["images"] == []
    assert [item["type"] for item in content["materials"]["videos"]] == [
        "photo",
        "photo",
    ]
    assert [(track["name"], track["flag"]) for track in content["tracks"]] == [
        ("overlay", 0),
        ("overlay-1", 2),
    ]
    clip = content["tracks"][0]["segments"][0]["clip"]
    assert clip == {
        "alpha": 0.75,
        "flip": {"horizontal": True, "vertical": False},
        "rotation": 15.0,
        "scale": {"x": 0.5, "y": 0.6},
        "transform": {"x": 0.25, "y": -0.4},
    }


def test_track_allocator_uses_layout_bands_and_suffixes_overlaps():
    assert (
        TRACK_LAYOUT_BANDS["video"].layout_order
        < TRACK_LAYOUT_BANDS["image"].layout_order
    )
    assert (
        TRACK_LAYOUT_BANDS["image"].layout_order
        < TRACK_LAYOUT_BANDS["subtitle"].layout_order
    )
    assert {
        "audio",
        "sound",
        "video",
        "image",
        "mask",
        "sticker",
        "subtitle",
        "text_template",
    } <= set(TRACK_LAYOUT_BANDS)

    allocator = TrackAllocator()
    assert allocator.allocate("subtitle", "subtitle", 0, 5_000_000).name == "subtitle"
    assert (
        allocator.allocate("subtitle", "subtitle", 5_000_000, 1_000_000).name
        == "subtitle"
    )
    assert (
        allocator.allocate("subtitle", "subtitle", 4_500_000, 2_000_000).name
        == "subtitle-1"
    )
    assert (
        allocator.allocate("subtitle", "subtitle", 4_500_000, 2_000_000).name
        == "subtitle-2"
    )
    assert (
        allocator.allocate("image", "overlay", 4_500_000, 2_000_000).name == "overlay"
    )


def test_bundle_uses_duo_resources_contract_and_indexes_deduped_media(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(jianying_writer.time, "time", lambda: 1_700_000_000.123)
    src = tmp_path / "src"
    src.mkdir()
    video = src / "same.mp4"
    audio = src / "voice.wav"
    image = src / "card.png"
    for path, payload in ((video, b"video"), (audio, b"audio"), (image, b"image")):
        path.write_bytes(payload)
    timeline = {
        "schema_version": 2,
        "canvas": {"width": 1280, "height": 720, "fps": 30},
        "duration": 3.0,
        "tracks": [
            {
                "kind": "video",
                "name": "video",
                "clips": [
                    {
                        "source_path": str(video),
                        "source_start": 0,
                        "source_end": 3,
                        "timeline_start": 0,
                        "timeline_end": 3,
                        "audio": {"base_gain": 1},
                    },
                ],
            },
            {
                "kind": "audio",
                "name": "narration",
                "role": "narration",
                "segments": [
                    {"source_path": str(audio), "timeline_start": 0, "timeline_end": 2},
                ],
            },
            {
                "kind": "image",
                "name": "overlay",
                "segments": [
                    {
                        "source_path": str(image),
                        "timeline_start": 0.5,
                        "timeline_end": 2.5,
                    },
                ],
            },
        ],
    }

    draft_dir, _ = export_timeline_to_jianying(
        timeline,
        tmp_path / "out",
        "portable",
        new_id=_counter_ids(),
        probe=_fake_probe,
        bundle_media=True,
    )
    root = Path(draft_dir)
    assert (root / "Resources/local/video/same.mp4").read_bytes() == b"video"
    assert (root / "Resources/local/audio/voice.wav").read_bytes() == b"audio"
    assert (root / "Resources/local/image/card.png").read_bytes() == b"image"

    content = json.loads((root / "draft_content.json").read_text(encoding="utf-8"))
    paths = [
        m["path"]
        for m in content["materials"]["videos"] + content["materials"]["audios"]
    ]
    assert all(path.startswith("##_draftpath_placeholder_") for path in paths)
    assert any("/Resources/local/image/card.png" in path for path in paths)

    meta = json.loads((root / "draft_meta_info.json").read_text(encoding="utf-8"))
    values = next(
        group["value"] for group in meta["draft_materials"] if group["type"] == 0
    )
    assert {value["metetype"] for value in values} == {"video", "music", "photo"}
    assert {value["file_Path"] for value in values} == {
        "./Resources/local/video/same.mp4",
        "./Resources/local/audio/voice.wav",
        "./Resources/local/image/card.png",
    }
    assert all(value["md5"] for value in values)
    assert all(value["id"] for value in values)
    assert {value["extra_info"] for value in values} == {
        "same.mp4",
        "voice.wav",
        "card.png",
    }
    assert {value["create_time"] for value in values} == {1_700_000_000}
    assert {value["import_time"] for value in values} == {1_700_000_000}
    assert {value["import_time_ms"] for value in values} == {1_700_000_000_123}


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
    content, _meta, notes = build_draft(
        timeline, new_id=_counter_ids(), probe=short_bgm_probe
    )
    bgm_track = next(
        t for t in content["tracks"] if t["type"] == "audio" and t["name"] == "bgm"
    )
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
    content, _meta, _notes = build_draft(
        timeline, new_id=_counter_ids(), probe=short_bgm_probe
    )
    bgm_track = next(
        t for t in content["tracks"] if t["type"] == "audio" and t["name"] == "bgm"
    )
    keyframe_counts = [len(seg["common_keyframes"]) for seg in bgm_track["segments"]]

    assert keyframe_counts[0] == 1
    assert keyframe_counts[1:] == [0, 0]
