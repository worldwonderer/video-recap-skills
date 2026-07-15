"""Optional 剪映 / JianYing (CapCut) draft exporter — decoupled, stdlib + ffprobe only.

Reads a backend-neutral `timeline.json` (see timeline.py) and writes a 剪映 draft
folder (`draft_content.json` + `draft_info.json` + `draft_meta_info.json`) that the
desktop app can open and the user can keep editing: video clips on the main track,
the narration and BGM as their own audio tracks, the recap lines as a subtitle
track, and the gap-fill ducking carried as native volume keyframes.

The public entrypoints stay small (`us`, `build_draft`,
`export_timeline_to_jianying`, CLI). Internally the exporter is split into schema/templates, a thin
normalized build context, material/segment builders, track layout metadata, and
a safe writer/bundler. This mirrors the useful schema boundaries from duo-video
while ffmpeg remains the canonical renderer and JianYing export remains an
optional sidecar.

Schema and the draft skeleton are informed by the open-source pyJianYingDraft
(© GuanYixuan, Apache-2.0) and capcut-mate (© Hommy, Apache-2.0). JSON protocol
templates pinned from duo-video are vendored under its MIT license; builders
are implemented locally and no upstream executable code, resource package,
adapter binary, or credential is included. See ACKNOWLEDGEMENTS / 致谢.
"""

import copy
import json
import os
import subprocess
import tempfile
import uuid

from jianying_builders import build_timeline_track as _build_timeline_track
from jianying_model import DraftBuildContext as _DraftBuildContext
from jianying_schema import draft_content_skeleton as _draft_content_skeleton
from jianying_schema import meta_info as _meta_info
from jianying_schema import us
from jianying_timeline_contract import normalize_timeline as _normalize_timeline
from jianying_writer import write_draft as _write_draft

__all__ = ["us", "build_draft", "export_timeline_to_jianying", "main"]


def _default_id():
    return str(uuid.uuid4()).upper()


def _probe_media(path):
    """Return (duration_us, width, height) via ffprobe; zeros on any failure."""
    try:
        r = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-of",
                "json",
                "-show_entries",
                "format=duration:stream=width,height,codec_type",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        data = json.loads(r.stdout or "{}")
        dur_us = us(float(data.get("format", {}).get("duration") or 0))
        width = height = 0
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                width, height = (
                    int(stream.get("width") or 0),
                    int(stream.get("height") or 0),
                )
                break
        return dur_us, width, height
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired):
        return 0, 0, 0


def build_draft(timeline, new_id=None, probe=None):
    """Build the 剪映 draft_content dict and companion meta from a timeline."""
    timeline = _normalize_timeline(timeline)
    new_id = new_id or _default_id
    probe = probe or _probe_media
    ctx = _DraftBuildContext.from_timeline(timeline, new_id, probe)

    for timeline_track in timeline.get("tracks", []):
        _build_timeline_track(ctx, timeline_track)
    ctx.finalize_tracks()

    draft_id = new_id()
    content = _draft_content_skeleton(
        draft_id,
        ctx.width,
        ctx.height,
        ctx.fps,
        ctx.total_us,
        ctx.materials,
        ctx.tracks,
    )
    meta = _meta_info(draft_id, ctx.total_us)
    return content, meta, ctx.notes


def _generate_reversed_media(source_path, output_path):
    commands = [
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            source_path,
            "-vf",
            "reverse",
            "-af",
            "areverse",
            output_path,
        ],
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            source_path,
            "-vf",
            "reverse",
            "-an",
            output_path,
        ],
    ]
    errors = []
    for command in commands:
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode == 0 and os.path.isfile(output_path):
            return
        errors.append(
            (result.stderr or result.stdout or "unknown ffmpeg error").strip()
        )
    raise RuntimeError(
        f"failed to reverse JianYing source {source_path}: {'; '.join(errors)}"
    )


def _prepare_reverse_sources(timeline, temporary_dir):
    prepared = copy.deepcopy(timeline)
    generated = []
    for track in prepared.get("tracks", []):
        if not isinstance(track, dict) or track.get("kind") != "video":
            continue
        for clip in track.get("clips", []):
            if (
                not isinstance(clip, dict)
                or not clip.get("reverse")
                or clip.get("reverse_path")
            ):
                continue
            source_path = clip.get("source_path")
            if not isinstance(source_path, str) or not os.path.isfile(source_path):
                raise ValueError(f"reverse source does not exist: {source_path}")
            output_path = os.path.join(
                temporary_dir, f"reversed-{uuid.uuid4().hex}.mp4"
            )
            _generate_reversed_media(source_path, output_path)
            clip["reverse_path"] = output_path
            generated.append(source_path)
    return prepared, generated


def export_timeline_to_jianying(
    timeline, out_dir, draft_name="recap", new_id=None, probe=None, bundle_media=True
):
    """Write a 剪映 draft folder under out_dir/draft_name. Returns (folder, notes).

    Referenced media is bundled by default so the draft is self-contained and
    portable. Pass bundle_media=False only when external absolute paths are
    intentionally required.
    """
    needs_generated_reverse = any(
        isinstance(track, dict)
        and track.get("kind") == "video"
        and any(
            isinstance(clip, dict)
            and clip.get("reverse")
            and not clip.get("reverse_path")
            for clip in track.get("clips", [])
        )
        for track in timeline.get("tracks", [])
    )
    if needs_generated_reverse and not bundle_media:
        raise ValueError("automatic reverse generation requires media bundling")
    if not needs_generated_reverse:
        content, meta, notes = build_draft(timeline, new_id=new_id, probe=probe)
        return _write_draft(
            content,
            meta,
            notes,
            out_dir,
            draft_name,
            bundle_media_enabled=bundle_media,
        )

    os.makedirs(out_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="jianying-reverse-", dir=out_dir
    ) as temporary_dir:
        prepared, generated = _prepare_reverse_sources(timeline, temporary_dir)
        content, meta, notes = build_draft(prepared, new_id=new_id, probe=probe)
        notes.extend(f"已生成倒放素材: {source}" for source in generated)
        return _write_draft(
            content,
            meta,
            notes,
            out_dir,
            draft_name,
            bundle_media_enabled=True,
        )


def main():
    import argparse

    ap = argparse.ArgumentParser(
        description="Export a timeline.json to a 剪映/JianYing draft folder."
    )
    ap.add_argument("timeline", help="path to timeline.json")
    ap.add_argument(
        "--out-dir", required=True, help="parent dir to create the draft folder in"
    )
    ap.add_argument("--name", default="recap", help="draft folder name")
    bundle_group = ap.add_mutually_exclusive_group()
    bundle_group.add_argument(
        "--bundle-media",
        dest="bundle_media",
        action="store_true",
        help="copy referenced media into the draft folder (default)",
    )
    bundle_group.add_argument(
        "--no-bundle-media",
        dest="bundle_media",
        action="store_false",
        help="keep external media paths instead of making a portable draft",
    )
    ap.set_defaults(bundle_media=True)
    args = ap.parse_args()
    with open(args.timeline, encoding="utf-8") as f:
        timeline = json.load(f)
    draft_dir, notes = export_timeline_to_jianying(
        timeline,
        args.out_dir,
        args.name,
        bundle_media=args.bundle_media,
    )
    for note in notes:
        print(f"  注意: {note}")
    print(
        json.dumps({"status": "exported", "draft_dir": draft_dir}, ensure_ascii=False)
    )


if __name__ == "__main__":
    main()
