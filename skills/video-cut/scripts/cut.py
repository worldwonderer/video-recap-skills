#!/usr/bin/env python3
"""Public API and CLI entrypoint for the self-contained video-cut skill."""

from cut_cli import main
from cut_contract import (
    cut_plan_fingerprint,
    edited_source_render_cache_payload,
    edited_source_render_fingerprint,
    file_fingerprint,
    load_clip_plan,
    normalize_clip_plan,
    normalize_multi_source_clip_plan,
    normalize_sources_manifest,
    parse_duration_seconds,
    should_reuse_edited_source,
    value_fingerprint,
)
from cut_render import (
    build_edited_source_video,
    update_delivery_qc,
    write_cut_delivery_qc,
)
from media_geometry import VideoGeometry
from narration_mapping import (
    lint_mapped_narration,
    map_narration_to_clips,
    source_time_to_output_time,
    update_cut_qc,
)
from sentence_boundaries import (
    enforce_clip_sentence_boundaries,
    snap_clip_ends_to_lines,
    snap_clip_starts_to_lines,
    snap_clips_off_shot_changes,
    snap_multi_source_clips,
)

__all__ = [
    "VideoGeometry",
    "build_edited_source_video",
    "cut_plan_fingerprint",
    "edited_source_render_cache_payload",
    "edited_source_render_fingerprint",
    "enforce_clip_sentence_boundaries",
    "file_fingerprint",
    "lint_mapped_narration",
    "load_clip_plan",
    "main",
    "map_narration_to_clips",
    "normalize_clip_plan",
    "normalize_multi_source_clip_plan",
    "normalize_sources_manifest",
    "parse_duration_seconds",
    "should_reuse_edited_source",
    "snap_clip_ends_to_lines",
    "snap_clip_starts_to_lines",
    "snap_clips_off_shot_changes",
    "snap_multi_source_clips",
    "source_time_to_output_time",
    "update_cut_qc",
    "update_delivery_qc",
    "value_fingerprint",
    "write_cut_delivery_qc",
]

if __name__ == "__main__":
    main()
