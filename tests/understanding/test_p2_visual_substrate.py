import sys
from pathlib import Path

sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parents[2]
        / "skills"
        / "video-understanding"
        / "scripts"
    ),
)
"""Regression tests for the P2 visual-substrate changes.

Q9: per-scene VLM frame sampling scales with scene length (~1 frame / vlm_seconds_per_frame,
floor 3, capped by vlm_max_frames) instead of the old hard cap of 6 that starved long scenes.

W3: the MiMo video-overview becomes the PRIMARY per-scene description when present; the frame
VLM still supplies frame_facts (so the substrate grade cannot regress) and the original frame
description is kept under frame_description as the fallback.
"""
import json

import understanding_runner as understand
from vlm import _max_frames_for_duration
from brief import assess_understanding_substrate


def test_frame_count_scales_with_duration_and_caps():
    # short scene -> floor of 3 (old behaviour kept the floor)
    assert _max_frames_for_duration(4) == 3
    assert _max_frames_for_duration(8) == 3
    # mid scene -> ~1 frame / 4s (the old code hard-capped this at 6)
    assert _max_frames_for_duration(40) == 10
    assert _max_frames_for_duration(60) == 15
    # long/merged scene -> capped at vlm_max_frames (16), NOT the old 6 -> ~1 frame/20s problem gone
    assert _max_frames_for_duration(122) == 16
    assert _max_frames_for_duration(600) == 16


def test_overview_becomes_primary_description_and_keeps_frame_facts(tmp_path):
    scenes = [
        {
            "scene_id": 0,
            "description": "frame desc 0",
            "frame_facts": {"1.0": ["a"]},
            "depth_analysis": "d0",
        },
        {"scene_id": 1, "description": "frame desc 1", "frame_facts": {"2.0": ["b"]}},
    ]
    ov = tmp_path / "mimo_video_overview.json"
    ov.write_text(
        json.dumps(
            {
                "chunks": [
                    {
                        "scene_id": 0,
                        "content": "五竹抱着婴儿在竹林中奔逃，黑衣杀手围攻。",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    out = understand._merge_overview_into_scenes(scenes, ov)

    # scene 0: description replaced by overview; frame_facts + depth kept; original frame desc preserved
    assert out[0]["description"].startswith("五竹")
    assert out[0]["description_source"] == "mimo_video_overview"
    assert out[0]["frame_description"] == "frame desc 0"
    assert out[0]["frame_facts"] == {"1.0": ["a"]}
    assert out[0]["depth_analysis"] == "d0"
    # scene 1: no overview chunk -> untouched (frame description is the fallback)
    assert out[1]["description"] == "frame desc 1"
    assert "frame_description" not in out[1]


def test_overview_merge_does_not_regress_substrate_grade(tmp_path):
    scenes = [{"scene_id": 0, "description": "frame", "frame_facts": {"1.0": ["a"]}}]
    asr = [{"text": "对" * 250}]  # rich spine
    before = assess_understanding_substrate([dict(s) for s in scenes], asr)["level"]
    ov = tmp_path / "mimo_video_overview.json"
    ov.write_text(
        json.dumps({"chunks": [{"scene_id": 0, "content": "丰富的视频理解描述。"}]}),
        encoding="utf-8",
    )
    after = assess_understanding_substrate(
        understand._merge_overview_into_scenes(scenes, ov), asr
    )["level"]
    assert before == after == "rich"  # frame_facts untouched -> grade cannot drop


def test_overview_absent_or_rejected_is_noop(tmp_path):
    scenes = [{"scene_id": 0, "description": "frame", "frame_facts": {"1.0": ["a"]}}]
    # absent overview file -> unchanged
    assert (
        understand._merge_overview_into_scenes(
            [dict(s) for s in scenes], tmp_path / "nope.json"
        )[0]["description"]
        == "frame"
    )
    # moderation-rejected chunk content -> not usable -> scene keeps frame description
    ov = tmp_path / "mimo_video_overview.json"
    ov.write_text(
        json.dumps(
            {
                "chunks": [
                    {
                        "scene_id": 0,
                        "content": "The request was rejected because it was considered high risk",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    out = understand._merge_overview_into_scenes([dict(s) for s in scenes], ov)
    assert out[0]["description"] == "frame"
    assert "frame_description" not in out[0]
