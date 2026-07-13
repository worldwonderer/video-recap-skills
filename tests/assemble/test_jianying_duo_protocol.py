"""Golden contracts for the JianYing protocol exposed by duo-video.

The JSON fixtures are JSON-equivalent copies from duo-video commit ``ef4eb46``. The
tests replace only authored/runtime values (IDs, paths, dimensions and timing),
then compare the remaining protocol object exactly.  Capability tests exercise
the public ``build_draft`` boundary so timeline producers do not need to know
about exporter internals.
"""

import json
import sys
import zipfile
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "video-assemble" / "scripts"
FIXTURES = Path(__file__).parent / "fixtures" / "jianying"
sys.path.insert(0, str(SCRIPTS))

from export_jianying import build_draft, export_timeline_to_jianying  # noqa: E402
from jianying_builders import base_segment  # noqa: E402
from jianying_schema import material_category_registry  # noqa: E402


def _fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _counter_ids():
    count = [0]

    def new_id():
        count[0] += 1
        return f"ID{count[0]:05d}"

    return new_id


def _probe(_path):
    return 6_000_000, 1280, 720


def _timeline(*tracks, duration=6.0):
    return {
        "schema_version": 2,
        "canvas": {"width": 1920, "height": 1080, "fps": 30},
        "duration": duration,
        "tracks": list(tracks),
    }


def _build(*tracks, duration=6.0, probe=_probe):
    return build_draft(
        _timeline(*tracks, duration=duration),
        new_id=_counter_ids(),
        probe=probe,
    )[0]


def _video_track(clip):
    return {"kind": "video", "name": "video", "clips": [clip]}


def _video_clip(path="/video.mp4", **overrides):
    clip = {
        "source_path": str(path),
        "source_start": 0.0,
        "source_end": 2.0,
        "timeline_start": 0.0,
        "timeline_end": 2.0,
    }
    clip.update(overrides)
    return clip


def _only_segment(content, track_type=None):
    tracks = content["tracks"]
    if track_type is not None:
        tracks = [track for track in tracks if track["type"] == track_type]
    segments = [segment for track in tracks for segment in track["segments"]]
    assert len(segments) == 1
    return segments[0]


def _only_material(content, materials_key):
    materials = content["materials"][materials_key]
    assert len(materials) == 1, f"expected one {materials_key} material, got {materials!r}"
    return materials[0]


def test_capability_manifest_is_pinned_to_reviewed_duo_video_revision():
    manifest = _fixture("capabilities.json")

    assert manifest["source"] == {
        "repository": "https://github.com/duoec/duo-video.git",
        "commit": "ef4eb46",
        "module": "duo-video-jy",
    }
    assert set(manifest["required_capabilities"]) == {
        "video",
        "image",
        "audio",
        "text",
        "sound",
        "sticker",
        "text_template",
        "transition",
        "mask",
        "lut",
        "video_effect",
        "face_effect",
        "green_screen",
        "chroma",
        "compound",
        "variable_speed",
        "reverse_local_source",
        "generic_transform",
        "rich_text",
        "per_character_text_style",
    }


def test_empty_project_root_config_and_materials_match_duo_template():
    content = _build(duration=3.5)
    expected = _fixture("duo_empty_project_info.json")
    expected["canvas_config"] = {"width": 1920, "height": 1080, "ratio": "original"}
    expected["duration"] = 3_500_000
    expected["fps"] = 30.0
    expected["id"] = content["id"]
    for platform_key in ("last_modified_platform", "platform"):
        for identity_key in ("device_id", "hard_disk_id", "mac_address"):
            expected[platform_key][identity_key] = ""

    assert content == expected


def test_default_video_material_matches_duo_template(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    content = _build(_video_track(_video_clip(source)))
    actual = _only_material(content, "videos")
    expected = _fixture("duo_empty_video.json")
    expected.update(
        id=actual["id"],
        path=str(source),
        material_name="source.mp4",
        width=1280,
        height=720,
        duration=6_000_000,
    )

    assert actual == expected


def test_default_audio_material_matches_duo_template(tmp_path):
    source = tmp_path / "voice.wav"
    source.write_bytes(b"audio")
    track = {
        "kind": "audio",
        "name": "audio",
        "role": "audio",
        "segments": [
            {
                "source_path": str(source),
                "timeline_start": 0.0,
                "timeline_end": 2.0,
            }
        ],
    }
    content = _build(track)
    actual = _only_material(content, "audios")
    expected = _fixture("duo_empty_audio.json")
    expected.update(id=actual["id"], path=str(source), duration=6_000_000)

    assert actual == expected


def test_default_text_material_matches_duo_template():
    track = {
        "kind": "text",
        "name": "text",
        "segments": [{"text": "hello", "timeline_start": 0.0, "timeline_end": 2.0}],
    }
    content = _build(track)
    actual = _only_material(content, "texts")
    expected = _fixture("duo_empty_text.json")
    expected.update(
        id=actual["id"],
        content=actual["content"],
        type="text",
        alignment=1,
        font_size=8.0,
        text_color="#FFFFFF",
        line_spacing=0.02,
        letter_spacing=0.0,
        check_flag=15,
    )

    assert actual == expected


def test_base_segment_matches_duo_template():
    actual = base_segment("MATERIAL", 1_000_000, 2_000_000, 1.0, [], _counter_ids())
    expected = _fixture("duo_empty_segment.json")
    expected.update(
        id=actual["id"],
        material_id="MATERIAL",
        render_index=2,
        track_render_index=2,
        target_timerange={"start": 1_000_000, "duration": 2_000_000},
    )

    assert actual == expected


def test_registry_marks_every_duo_authoring_material_as_supported():
    direct_material_capabilities = {
        "video",
        "image",
        "audio",
        "text",
        "sound",
        "sticker",
        "text_template",
        "transition",
        "mask",
        "lut",
        "video_effect",
        "face_effect",
        "chroma",
        "green_screen",
        "compound",
        "style",
    }
    registry = material_category_registry()

    assert direct_material_capabilities <= registry.keys()
    assert all(registry[kind]["status"].startswith("supported") for kind in direct_material_capabilities)
    assert registry["sticker"]["status"] == "supported_offline_payload"


@pytest.mark.parametrize(
    ("kind", "materials_key", "track_type"),
    [
        ("sound", "audios", "audio"),
        ("sticker", "stickers", "sticker"),
        ("text_template", "text_templates", "text"),
        ("video_effect", "video_effects", "effect"),
        ("face_effect", "video_effects", "effect"),
    ],
)
def test_raw_resource_material_is_emitted_and_referenced(kind, materials_key, track_type):
    resource_id = f"resource.{kind}"
    track = {
        "kind": kind,
        "name": kind,
        "segments": [
            {
                "timeline_start": 1.0,
                "timeline_end": 2.5,
                "material": {
                    "resource_id": resource_id,
                    "name": f"fixture {kind}",
                    "path": f"Resources/{kind}",
                    "type": kind,
                },
            }
        ],
    }
    content = _build(track)
    materials = content["materials"][materials_key]

    assert len(materials) == 1
    assert materials[0]["resource_id"] == resource_id
    assert content["tracks"][0]["type"] == track_type
    assert content["tracks"][0]["segments"][0]["material_id"] == materials[0]["id"]
    assert content["tracks"][0]["segments"][0]["track_render_index"] == 0


def test_resource_config_builds_resource_material_without_network_access():
    track = {
        "kind": "sticker",
        "name": "sticker",
        "segments": [
            {
                "timeline_start": 0.0,
                "timeline_end": 1.0,
                "resource_config": {
                    "resource_id": "sticker.config",
                    "resources": ["Resources/sticker/config"],
                    "cover_img": "covers/config.png",
                    "main_config": {
                        "name": "configured sticker",
                        "path": "Resources/sticker/config/main.json",
                        "type": "sticker",
                    },
                },
            }
        ],
    }
    content = _build(track)
    sticker = _only_material(content, "stickers")

    assert sticker["resource_id"] == "sticker.config"
    assert sticker["name"] == "configured sticker"
    assert sticker["path"] == "Resources/sticker/config/main.json"
    assert sticker["icon_url"] == "covers/config.png"
    assert sticker["preview_cover_url"] == "covers/config.png"


def test_upstream_camel_case_jy_resource_is_accepted_without_conversion():
    track = {
        "kind": "sticker",
        "name": "sticker",
        "segments": [{
            "timeline_start": 0.0,
            "timeline_end": 1.0,
            "resource_config": {
                "resourceId": "sticker.upstream",
                "mainConfig": json.dumps({"name": "upstream sticker", "type": "sticker"}),
                "coverImg": "covers/upstream.png",
                "resources": [],
            },
        }],
    }

    content = _build(track)
    sticker = _only_material(content, "stickers")

    assert sticker["resource_id"] == "sticker.upstream"
    assert sticker["name"] == "upstream sticker"
    assert sticker["icon_url"] == "covers/upstream.png"


def test_named_resource_package_is_resolved_without_network_access():
    timeline = _timeline({
        "kind": "sticker",
        "name": "sticker",
        "segments": [{
            "timeline_start": 0.0,
            "timeline_end": 1.0,
            "resource_package": "local-sticker",
        }],
    })
    timeline["resource_packages"] = {
        "local-sticker": {
            "resource_id": "sticker.named",
            "main_config": {"name": "named sticker", "type": "sticker"},
        }
    }

    content = build_draft(timeline, new_id=_counter_ids(), probe=_probe)[0]
    sticker = _only_material(content, "stickers")

    assert sticker["resource_id"] == "sticker.named"
    assert sticker["name"] == "named sticker"


def test_transition_material_is_referenced_by_video_segment():
    transition = {
        "resource_id": "transition.fade",
        "name": "fade",
        "type": "transition",
        "duration": 400_000,
    }
    content = _build(_video_track(_video_clip(transition=transition)))
    material = _only_material(content, "transitions")
    segment = _only_segment(content, "video")

    assert material["resource_id"] == "transition.fade"
    assert material["duration"] == 400_000
    assert material["id"] in segment["extra_material_refs"]


def test_mask_material_populates_legacy_and_common_arrays_and_is_referenced():
    mask = {
        "resource_id": "mask.circle",
        "name": "circle",
        "type": "mask",
        "config": {
            "center_x": 0.25,
            "center_y": -0.1,
            "width": 0.6,
            "height": 0.4,
            "feather": 0.2,
            "invert": False,
        },
    }
    content = _build(_video_track(_video_clip(mask=mask)))
    legacy = _only_material(content, "masks")
    common = _only_material(content, "common_mask")
    segment = _only_segment(content, "video")

    assert legacy == common
    assert common["resource_id"] == "mask.circle"
    assert common["config"] == mask["config"]
    assert common["id"] in segment["extra_material_refs"]


def test_lut_material_emits_effect_with_normalized_strength_and_reference():
    lut = {
        "resource_id": "lut.film",
        "path": "Resources/lut/film.cube",
        "lumi_hub_path": "Resources/effect/lut/config.json",
        "strength": 75,
        "skin_tone_correction": 20,
    }
    content = _build(_video_track(_video_clip(lut=lut)))
    segment = _only_segment(content, "video")
    effects = content["materials"]["effects"]
    lut_effects = [effect for effect in effects if effect.get("path", "").endswith("film.cube")]

    assert len(lut_effects) == 1, f"missing LUT effect: {effects!r}"
    lut_effect = lut_effects[0]
    assert lut_effect["resource_id"] == "lut.film"
    assert lut_effect["value"] == 0.75
    assert lut_effect["id"] in segment["extra_material_refs"]
    assert any(
        effect.get("type") == "skin_tone_correction"
        and effect["value"] == 0.2
        and effect["version"] == "v3"
        and effect["path"] == "Resources/effect/lut"
        for effect in effects
    )


def test_green_screen_builds_chroma_inside_a_compound_draft(tmp_path):
    foreground = tmp_path / "foreground.mp4"
    background = tmp_path / "background.png"
    foreground.write_bytes(b"video")
    background.write_bytes(b"image")
    clip = _video_clip(
        foreground,
        compound=True,
        green_background={
            "source_path": str(background),
            "type": "photo",
            "width": 1280,
            "height": 720,
        },
        chroma={
            "resource_id": "chroma.green",
            "color": "#00FF00",
            "intensity_value": 0.8,
            "edge_smooth_value": 0.25,
            "spill_value": 0.15,
        },
    )
    content = _build(_video_track(clip))

    assert len(content["materials"]["drafts"]) == 1
    nested = content["materials"]["drafts"][0]["draft"]
    assert len(nested["materials"]["chromas"]) == 1
    assert nested["materials"]["chromas"][0]["color"] == "#00FF00"
    assert {material["type"] for material in nested["materials"]["videos"]} == {"video", "photo"}
    assert {track["name"] for track in nested["tracks"]} >= {"video", "green_background"}
    compound_segment = _only_segment(content, "video")
    assert content["materials"]["drafts"][0]["id"] in compound_segment["extra_material_refs"]


def test_compound_draft_media_is_recursively_bundled_and_indexed(tmp_path):
    foreground = tmp_path / "foreground.mp4"
    background = tmp_path / "background.png"
    foreground.write_bytes(b"video")
    background.write_bytes(b"image")
    clip = _video_clip(
        foreground,
        compound=True,
        green_background={"source_path": str(background), "type": "photo"},
        chroma={"resource_id": "chroma.green", "color": "#00FF00"},
    )

    draft_dir, _notes = export_timeline_to_jianying(
        _timeline(_video_track(clip)),
        tmp_path / "out",
        new_id=_counter_ids(),
        probe=_probe,
    )
    root = Path(draft_dir)
    content = json.loads((root / "draft_content.json").read_text(encoding="utf-8"))
    nested_videos = content["materials"]["drafts"][0]["draft"]["materials"]["videos"]

    assert (root / "Resources/local/video/foreground.mp4").read_bytes() == b"video"
    assert (root / "Resources/local/image/background.png").read_bytes() == b"image"
    assert all(material["path"].startswith("##_draftpath_placeholder_") for material in nested_videos)
    meta = json.loads((root / "draft_meta_info.json").read_text(encoding="utf-8"))
    indexed = next(group["value"] for group in meta["draft_materials"] if group["type"] == 0)
    assert {item["metetype"] for item in indexed} == {"video", "photo"}


def test_compound_preserves_outer_trim_and_expands_nested_source(tmp_path):
    foreground = tmp_path / "foreground.mp4"
    background = tmp_path / "background.png"
    foreground.write_bytes(b"video")
    background.write_bytes(b"image")

    def ten_second_probe(_path):
        return 10_000_000, 1280, 720

    clip = _video_clip(
        foreground,
        source_start=3.0,
        source_end=7.0,
        timeline_start=1.0,
        timeline_end=3.0,
        speed=2.0,
        compound=True,
        green_background={"source_path": str(background), "type": "photo"},
        chroma={"resource_id": "chroma.green", "color": "#00FF00"},
    )
    content = _build(_video_track(clip), probe=ten_second_probe)
    nested = content["materials"]["drafts"][0]["draft"]
    nested_foreground = next(
        segment
        for track in nested["tracks"] if track["name"] == "video"
        for segment in track["segments"]
    )
    outer = _only_segment(content, "video")

    assert nested_foreground["source_timerange"] == {"start": 0, "duration": 10_000_000}
    assert nested_foreground["target_timerange"] == {"start": 0, "duration": 10_000_000}
    assert outer["source_timerange"] == {"start": 3_000_000, "duration": 4_000_000}
    assert outer["target_timerange"] == {"start": 1_000_000, "duration": 2_000_000}


def test_offline_resource_package_files_are_bundled_and_private_fields_removed(tmp_path):
    sticker_file = tmp_path / "sticker.bundle"
    sticker_file.write_bytes(b"sticker")
    track = {
        "kind": "sticker",
        "name": "sticker",
        "segments": [{
            "timeline_start": 0.0,
            "timeline_end": 1.0,
            "resource_config": {
                "resource_id": "sticker.local",
                "resources": [str(sticker_file)],
                "main_config": {
                    "resource_id": "sticker.local",
                    "path": str(sticker_file),
                    "type": "sticker",
                },
            },
        }],
    }

    draft_dir, _notes = export_timeline_to_jianying(
        _timeline(track), tmp_path / "out", new_id=_counter_ids(), probe=_probe
    )
    root = Path(draft_dir)
    content = json.loads((root / "draft_content.json").read_text(encoding="utf-8"))
    sticker = content["materials"]["stickers"][0]

    assert (root / "Resources/local/sticker/sticker.bundle").read_bytes() == b"sticker"
    assert sticker["path"].startswith("##_draftpath_placeholder_")
    assert "_bundle_resources" not in sticker


def test_offline_resource_zip_is_safely_extracted_under_package_directory(tmp_path):
    archive = tmp_path / "sticker-pack.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("config/main.json", "{}")
        package.writestr("assets/image.png", b"png")
    track = {
        "kind": "sticker",
        "name": "sticker",
        "segments": [{
            "timeline_start": 0.0,
            "timeline_end": 1.0,
            "resource_config": {
                "resourceId": "sticker.zip",
                "resources": [str(archive)],
                "mainConfig": {"resource_id": "sticker.zip", "type": "sticker"},
            },
        }],
    }

    draft_dir, _notes = export_timeline_to_jianying(
        _timeline(track), tmp_path / "out", new_id=_counter_ids(), probe=_probe
    )
    package_root = Path(draft_dir) / "Resources/local/sticker/sticker-pack"

    assert (package_root / "config/main.json").read_text(encoding="utf-8") == "{}"
    assert (package_root / "assets/image.png").read_bytes() == b"png"


def test_text_template_can_bundle_explicit_text_resource_kind(tmp_path):
    text_resource = tmp_path / "template-text.json"
    text_resource.write_text("{}", encoding="utf-8")
    track = {
        "kind": "text_template",
        "name": "text_template",
        "segments": [{
            "timeline_start": 0.0,
            "timeline_end": 1.0,
            "resource_config": {
                "resourceId": "template.local",
                "mainConfig": {"resource_id": "template.local", "type": "text_template"},
                "resources": [{
                    "source_path": str(text_resource),
                    "resource_kind": "text",
                }],
                "texts": [],
                "effects": [],
            },
        }],
    }

    draft_dir, _notes = export_timeline_to_jianying(
        _timeline(track), tmp_path / "out", new_id=_counter_ids(), probe=_probe
    )

    assert (
        Path(draft_dir) / "Resources/local/text/template-text.json"
    ).read_text(encoding="utf-8") == "{}"


def test_text_template_resources_rewrite_subordinate_texts_and_effects(tmp_path):
    font = tmp_path / "template-font.ttf"
    effect = tmp_path / "template-effect.bundle"
    font.write_bytes(b"font")
    effect.write_bytes(b"effect")
    subordinate_content = json.dumps({
        "text": "Template",
        "styles": [{"font": {"id": "font.local", "path": str(font)}}],
    })
    track = {
        "kind": "text_template",
        "name": "text_template",
        "segments": [{
            "timeline_start": 0.0,
            "timeline_end": 1.0,
            "resource_config": {
                "resourceId": "template.subordinates",
                "mainConfig": {
                    "resource_id": "template.subordinates",
                    "type": "text_template",
                },
                "resources": [
                    {"source_path": str(font), "resource_kind": "fonts"},
                    {"source_path": str(effect), "resource_kind": "effect"},
                ],
                "texts": [{"id": "template-text", "content": subordinate_content}],
                "effects": [{"id": "template-effect", "path": str(effect)}],
            },
        }],
    }

    draft_dir, _notes = export_timeline_to_jianying(
        _timeline(track), tmp_path / "out", new_id=_counter_ids(), probe=_probe
    )
    root = Path(draft_dir)
    content = json.loads((root / "draft_content.json").read_text(encoding="utf-8"))
    font_path = json.loads(content["materials"]["texts"][0]["content"])["styles"][0][
        "font"
    ]["path"]
    effect_path = content["materials"]["effects"][0]["path"]

    assert (root / "Resources/local/fonts/template-font.ttf").read_bytes() == b"font"
    assert (root / "Resources/local/effect/template-effect.bundle").read_bytes() == b"effect"
    assert font_path.startswith("##_draftpath_placeholder_")
    assert effect_path.startswith("##_draftpath_placeholder_")


def test_semantic_strings_are_never_inferred_as_resource_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "README.md").write_text("do not bundle me", encoding="utf-8")
    track = {
        "kind": "sticker",
        "name": "sticker",
        "segments": [{
            "timeline_start": 0.0,
            "timeline_end": 1.0,
            "material": {"name": "README.md", "type": "sticker"},
        }],
    }

    draft_dir, _notes = export_timeline_to_jianying(
        _timeline(track), tmp_path / "out", new_id=_counter_ids(), probe=_probe
    )
    content = json.loads((Path(draft_dir) / "draft_content.json").read_text(encoding="utf-8"))

    assert content["materials"]["stickers"][0]["name"] == "README.md"
    assert not (Path(draft_dir) / "Resources/local/sticker/README.md").exists()


def test_variable_speed_updates_segment_source_duration_and_speed_material():
    clip = _video_clip(
        source_start=1.0,
        source_end=5.0,
        timeline_end=2.0,
        speed=2.0,
    )
    content = _build(_video_track(clip))
    segment = _only_segment(content, "video")
    speed = content["materials"]["speeds"][0]

    assert segment["speed"] == 2.0
    assert segment["source_timerange"] == {"start": 1_000_000, "duration": 4_000_000}
    assert speed == {
        "id": speed["id"],
        "speed": 2.0,
        "type": "speed",
        "mode": 0,
        "curve_speed": None,
    }
    assert speed["id"] in segment["extra_material_refs"]


@pytest.mark.parametrize("kind", ["image", "sticker"])
def test_non_video_speed_consumes_target_duration_times_speed(kind):
    if kind == "image":
        track = {
            "kind": "image",
            "name": "image",
            "segments": [{
                "source_path": "/image.png",
                "timeline_start": 0.0,
                "timeline_end": 2.0,
                "speed": 2.0,
            }],
        }
    else:
        track = {
            "kind": "sticker",
            "name": "sticker",
            "segments": [{
                "timeline_start": 0.0,
                "timeline_end": 2.0,
                "speed": 2.0,
                "material": {"resource_id": "sticker.speed", "type": "sticker"},
            }],
        }

    content = _build(track)
    segment = _only_segment(content)

    assert segment["source_timerange"] == {"start": 0, "duration": 4_000_000}


def test_video_speed_rejects_inconsistent_authored_source_range():
    clip = _video_clip(source_start=0.0, source_end=2.0, timeline_end=2.0, speed=2.0)

    with pytest.raises(ValueError, match="source duration.*target duration.*speed"):
        _build(_video_track(clip))


def test_reverse_uses_local_reversed_source_and_recalculates_source_start(tmp_path):
    original = tmp_path / "original.mp4"
    reversed_source = tmp_path / "original-reversed.mp4"
    original.write_bytes(b"original")
    reversed_source.write_bytes(b"reversed")

    def ten_second_probe(_path):
        return 10_000_000, 1280, 720

    clip = _video_clip(
        original,
        source_start=2.0,
        source_end=5.0,
        timeline_end=3.0,
        reverse=True,
        reverse_path=str(reversed_source),
    )
    content = _build(_video_track(clip), probe=ten_second_probe)
    material = _only_material(content, "videos")
    segment = _only_segment(content, "video")

    assert material["path"] == str(reversed_source)
    assert segment["source_timerange"] == {"start": 5_000_000, "duration": 3_000_000}
    assert segment["reverse"] is False


def test_generic_transform_maps_to_duo_clip_shape():
    clip = _video_clip(
        opacity=0.65,
        rotation_degrees=90,
        scale={"x": 1.25, "y": 0.75},
        position={"x": 0.2, "y": -0.3},
        flip={"horizontal": True, "vertical": True},
    )
    content = _build(_video_track(clip))

    assert _only_segment(content, "video")["clip"] == {
        "alpha": 0.65,
        "flip": {"horizontal": True, "vertical": True},
        "rotation": 90.0,
        "scale": {"x": 1.25, "y": 0.75},
        "transform": {"x": 0.2, "y": -0.3},
    }


def test_text_style_and_words_emit_rich_per_character_utf16_ranges():
    track = {
        "kind": "text",
        "name": "text",
        "segments": [
            {
                "text": "A\U0001f600B",
                "timeline_start": 0.0,
                "timeline_end": 2.0,
                "style": {"font_size": 22.0, "fill_color": "#FFFFFF"},
                "words": [
                    {
                        "index": 1,
                        "length": 2,
                        "font_size": 30.0,
                        "fill_color": "#FF0000",
                        "bold": True,
                        "italic": True,
                        "underline": True,
                    }
                ],
            }
        ],
    }
    content = _build(track)
    material = content["materials"]["texts"][0]
    rich_content = json.loads(material["content"])

    assert material.get("is_rich_text") is True
    assert [style["range"] for style in rich_content["styles"]] == [[0, 1], [1, 3], [3, 4]]
    emoji_style = rich_content["styles"][1]
    assert emoji_style["size"] == 30.0
    assert emoji_style["fill"]["content"]["solid"]["color"] == [1.0, 0.0, 0.0]
    assert {key: emoji_style[key] for key in ("bold", "italic", "underline")} == {
        "bold": True,
        "italic": True,
        "underline": True,
    }


def test_text_style_presets_emit_font_stroke_shadow_background_and_effect_style():
    timeline = _timeline({
        "kind": "text",
        "name": "text",
        "segments": [{
            "text": "Styled",
            "timeline_start": 0.0,
            "timeline_end": 2.0,
            "style_id": "title",
        }],
    })
    timeline["style_presets"] = {
        "title": {
            "font_size": 28,
            "fill_color": "#00FF00",
            "font_id": "font.local",
            "font_path": "Resources/local/fonts/title.ttf",
            "stroke_color": "#FF0000",
            "stroke_width": 10,
            "shadow_color": "#000000",
            "shadow_opacity": 80,
            "background_color": "#0000FF",
            "background_opacity": 50,
            "effect_style": {"id": "flower.local", "path": "Resources/local/flower/effect"},
        }
    }

    content = build_draft(timeline, new_id=_counter_ids(), probe=_probe)[0]
    material = _only_material(content, "texts")
    style = json.loads(material["content"])["styles"][0]

    assert style["font"] == {"id": "font.local", "path": "Resources/local/fonts/title.ttf"}
    assert style["strokes"][0]["content"]["solid"]["color"] == [1.0, 0.0, 0.0]
    assert style["shadows"][0]["alpha"] == 0.8
    assert style["effect_style"]["id"] == "flower.local"
    assert material["background_color"] == "#0000FF"
    assert material["background_alpha"] == 0.5


def test_rich_text_template_matches_pinned_duo_shape():
    content = _build({
        "kind": "text",
        "name": "text",
        "segments": [{"text": "test", "timeline_start": 0.0, "timeline_end": 1.0}],
    })
    actual = json.loads(_only_material(content, "texts")["content"])
    expected = _fixture("duo_empty_text_styles.json")
    expected["text"] = "test"
    expected["styles"][0]["range"] = [0, 4]
    expected["styles"][0]["size"] = 8.0
    expected["styles"][0].update({
        "bold": False,
        "italic": False,
        "underline": False,
        "strokes": [],
        "use_letter_color": True,
    })

    assert actual == expected


def test_rich_text_font_and_effect_paths_are_bundled_and_rewritten(tmp_path):
    font = tmp_path / "title.ttf"
    effect = tmp_path / "flower.bundle"
    font.write_bytes(b"font")
    effect.write_bytes(b"effect")
    timeline = _timeline({
        "kind": "text",
        "name": "text",
        "segments": [{
            "text": "Styled",
            "timeline_start": 0.0,
            "timeline_end": 2.0,
            "style": {
                "font_path": str(font),
                "effect_style": {"id": "flower.local", "path": str(effect)},
            },
        }],
    })

    draft_dir, _notes = export_timeline_to_jianying(
        timeline, tmp_path / "out", new_id=_counter_ids(), probe=_probe
    )
    root = Path(draft_dir)
    output = json.loads((root / "draft_content.json").read_text(encoding="utf-8"))
    style = json.loads(output["materials"]["texts"][0]["content"])["styles"][0]

    assert (root / "Resources/local/fonts/title.ttf").read_bytes() == b"font"
    assert (root / "Resources/local/effect/flower.bundle").read_bytes() == b"effect"
    assert style["font"]["path"].startswith("##_draftpath_placeholder_")
    assert style["effect_style"]["path"].startswith("##_draftpath_placeholder_")
