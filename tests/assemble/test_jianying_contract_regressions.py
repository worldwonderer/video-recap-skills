import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "video-assemble" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import export_jianying  # noqa: E402
from export_jianying import build_draft, export_timeline_to_jianying  # noqa: E402


def _counter_ids():
    count = [0]

    def new_id():
        count[0] += 1
        return f"ID{count[0]:05d}"

    return new_id


def _fake_probe(_path):
    return 5_000_000, 100, 100


def _video_timeline(source_path="/source.mp4", schema_version=2):
    return {
        "schema_version": schema_version,
        "canvas": {"width": 100, "height": 100, "fps": 30},
        "duration": 2.0,
        "tracks": [
            {
                "kind": "video",
                "name": "video",
                "clips": [
                    {
                        "source_path": str(source_path),
                        "source_start": 0.0,
                        "source_end": 2.0,
                        "timeline_start": 0.0,
                        "timeline_end": 2.0,
                    }
                ],
            }
        ],
    }


def test_same_named_overlapping_video_and_image_keep_distinct_tracks():
    timeline = _video_timeline()
    timeline["tracks"][0]["name"] = "shared"
    timeline["tracks"].append(
        {
            "kind": "image",
            "name": "shared",
            "segments": [
                {
                    "source_path": "/overlay.png",
                    "timeline_start": 0.5,
                    "timeline_end": 1.5,
                }
            ],
        }
    )

    content, _meta, _notes = build_draft(
        timeline, new_id=_counter_ids(), probe=_fake_probe
    )

    tracks = content["tracks"]
    assert [(track["type"], track["name"]) for track in tracks] == [
        ("video", "shared"),
        ("video", "shared"),
    ]
    assert [len(track["segments"]) for track in tracks] == [1, 1]
    assert [track["flag"] for track in tracks] == [0, 2]
    assert "_semantic_kind" not in tracks[0]
    assert "_semantic_kind" not in tracks[1]


def test_public_export_bundles_media_by_default(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")

    draft_dir, _notes = export_timeline_to_jianying(
        _video_timeline(source),
        tmp_path / "out",
        new_id=_counter_ids(),
        probe=_fake_probe,
    )

    root = Path(draft_dir)
    assert (root / "Resources/local/video/source.mp4").read_bytes() == b"video"
    content = json.loads((root / "draft_content.json").read_text(encoding="utf-8"))
    assert content["materials"]["videos"][0]["path"].startswith(
        "##_draftpath_placeholder_"
    )


def test_public_export_can_explicitly_disable_media_bundling(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")

    draft_dir, _notes = export_timeline_to_jianying(
        _video_timeline(source),
        tmp_path / "out",
        new_id=_counter_ids(),
        probe=_fake_probe,
        bundle_media=False,
    )

    root = Path(draft_dir)
    assert not (root / "Resources").exists()
    content = json.loads((root / "draft_content.json").read_text(encoding="utf-8"))
    assert content["materials"]["videos"][0]["path"] == str(source)


def test_public_export_generates_and_bundles_missing_reverse_source(monkeypatch, tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"original")
    timeline = _video_timeline(source)
    timeline["tracks"][0]["clips"][0]["reverse"] = True

    def fake_reverse(_source, output):
        Path(output).write_bytes(b"reversed")

    monkeypatch.setattr(export_jianying, "_generate_reversed_media", fake_reverse)
    draft_dir, notes = export_timeline_to_jianying(
        timeline, tmp_path / "out", new_id=_counter_ids(), probe=_fake_probe
    )
    root = Path(draft_dir)
    reversed_files = list((root / "Resources/local/video").glob("reversed-*.mp4"))

    assert len(reversed_files) == 1
    assert reversed_files[0].read_bytes() == b"reversed"
    assert any("已生成倒放素材" in note for note in notes)
    assert "reverse_path" not in timeline["tracks"][0]["clips"][0]


def test_automatic_reverse_requires_bundling(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"original")
    timeline = _video_timeline(source)
    timeline["tracks"][0]["clips"][0]["reverse"] = True

    with pytest.raises(ValueError, match="requires media bundling"):
        export_timeline_to_jianying(
            timeline, tmp_path / "out", bundle_media=False,
            new_id=_counter_ids(), probe=_fake_probe,
        )


@pytest.mark.parametrize(
    ("extra_args", "expected"),
    [([], True), (["--no-bundle-media"], False), (["--bundle-media"], True)],
)
def test_direct_cli_defaults_to_bundle_with_explicit_opt_out(
    monkeypatch, tmp_path, extra_args, expected
):
    timeline_path = tmp_path / "timeline.json"
    timeline_path.write_text(json.dumps(_video_timeline()), encoding="utf-8")
    captured = {}

    def fake_export(_timeline, _out_dir, _name, *, bundle_media):
        captured["bundle_media"] = bundle_media
        return str(tmp_path / "draft"), []

    monkeypatch.setattr(export_jianying, "export_timeline_to_jianying", fake_export)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "export_jianying.py",
            str(timeline_path),
            "--out-dir",
            str(tmp_path / "out"),
            *extra_args,
        ],
    )

    export_jianying.main()

    assert captured == {"bundle_media": expected}


def test_v1_timeline_is_migrated_without_mutating_the_caller():
    timeline = _video_timeline(schema_version=1)
    original = deepcopy(timeline)

    v1_content, _meta, _notes = build_draft(
        timeline, new_id=_counter_ids(), probe=_fake_probe
    )
    v2_content, _meta, _notes = build_draft(
        {**timeline, "schema_version": 2},
        new_id=_counter_ids(),
        probe=_fake_probe,
    )

    assert timeline == original
    assert v1_content == v2_content


@pytest.mark.parametrize("schema_version", [3, 999])
def test_unknown_future_timeline_schema_is_rejected(schema_version):
    with pytest.raises(ValueError, match="unsupported timeline schema_version"):
        build_draft(
            _video_timeline(schema_version=schema_version),
            new_id=_counter_ids(),
            probe=_fake_probe,
        )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda timeline: timeline.pop("schema_version"), "schema_version"),
        (lambda timeline: timeline.update(schema_version=True), "schema_version"),
        (lambda timeline: timeline.pop("canvas"), "canvas"),
        (lambda timeline: timeline["canvas"].update(width=True), "canvas.width"),
        (lambda timeline: timeline.update(duration="two"), "duration"),
        (lambda timeline: timeline.update(tracks={}), "tracks"),
        (lambda timeline: timeline["tracks"].append("video"), "tracks\\[1\\]"),
        (lambda timeline: timeline["tracks"][0].pop("kind"), "tracks\\[0\\].kind"),
        (lambda timeline: timeline["tracks"][0].update(clips={}), "tracks\\[0\\].clips"),
        (
            lambda timeline: timeline["tracks"][0]["clips"][0].pop("source_path"),
            "source_path",
        ),
        (
            lambda timeline: timeline["tracks"][0]["clips"][0].update(
                timeline_end=0.0
            ),
            "timeline_end",
        ),
        (
            lambda timeline: timeline["tracks"][0]["clips"][0].update(scale="ignored"),
            "scale",
        ),
        (
            lambda timeline: timeline["tracks"][0]["clips"][0].update(
                green_background="not-a-background-package"
            ),
            "green_background",
        ),
    ],
)
def test_v2_timeline_contract_rejects_malformed_required_structure(mutate, message):
    timeline = _video_timeline()
    mutate(timeline)

    with pytest.raises((TypeError, ValueError), match=message):
        build_draft(timeline, new_id=_counter_ids(), probe=_fake_probe)


def test_missing_relative_media_is_reported_during_portable_export(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    timeline = _video_timeline("definitely-missing.mp4")

    _draft_dir, notes = export_timeline_to_jianying(
        timeline, tmp_path / "out", new_id=_counter_ids(), probe=_fake_probe
    )

    assert any("definitely-missing.mp4" in note for note in notes)


def test_runtime_project_and_compound_drafts_scrub_upstream_hardware_ids(tmp_path):
    foreground = tmp_path / "foreground.mp4"
    background = tmp_path / "background.png"
    foreground.write_bytes(b"video")
    background.write_bytes(b"image")
    timeline = _video_timeline(foreground)
    timeline["tracks"][0]["clips"][0].update(
        compound=True,
        green_background={"source_path": str(background), "type": "photo"},
        chroma={"resource_id": "chroma.green", "color": "#00FF00"},
    )

    content, _meta, _notes = build_draft(
        timeline, new_id=_counter_ids(), probe=_fake_probe
    )
    projects = [content, content["materials"]["drafts"][0]["draft"]]

    for project in projects:
        for platform_key in ("last_modified_platform", "platform"):
            platform = project[platform_key]
            assert platform["device_id"] == ""
            assert platform["hard_disk_id"] == ""
            assert platform["mac_address"] == ""
