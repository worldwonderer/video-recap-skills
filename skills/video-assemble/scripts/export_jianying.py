"""Optional 剪映 / JianYing (CapCut) draft exporter — decoupled, stdlib + ffprobe only.

Reads a backend-neutral `timeline.json` (see timeline.py) and writes a 剪映 draft
folder (`draft_content.json` + `draft_info.json` + `draft_meta_info.json`) that the
desktop app can open and the user can keep editing: video clips on the main track,
the narration and BGM as their own audio tracks, the recap lines as a subtitle
track, and the gap-fill ducking carried as native volume keyframes.

The public entrypoints stay small (`build_draft`, `export_timeline_to_jianying`,
`_us`, CLI). Internally the exporter is split into schema/templates, a thin
normalized build context, material/segment builders, track layout metadata, and
a safe writer/bundler. This mirrors the useful schema boundaries from duo-video
while ffmpeg remains the canonical renderer and JianYing export remains an
optional sidecar.

Schema and the draft skeleton are reimplemented from the open-source
pyJianYingDraft (© GuanYixuan, Apache-2.0) and capcut-mate (© Hommy, Apache-2.0);
see ACKNOWLEDGEMENTS / 致谢 in the README. No third-party code is vendored — the
draft JSON is built directly here so the bundle stays stdlib-only.
"""

import json
import subprocess
import uuid

from jianying_builders import build_timeline_track as _build_timeline_track
from jianying_model import DraftBuildContext as _DraftBuildContext
from jianying_schema import draft_content_skeleton as _draft_content_skeleton
from jianying_schema import meta_info as _meta_info
from jianying_schema import us as _us
from jianying_writer import write_draft as _write_draft

__all__ = ["_us", "build_draft", "export_timeline_to_jianying", "main"]


def _default_id():
    return str(uuid.uuid4()).upper()


def _probe_media(path):
    """Return (duration_us, width, height) via ffprobe; zeros on any failure."""
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error", "-of", "json",
                "-show_entries", "format=duration:stream=width,height,codec_type", str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        data = json.loads(r.stdout or "{}")
        dur_us = _us(float(data.get("format", {}).get("duration") or 0))
        width = height = 0
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
                break
        return dur_us, width, height
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired):
        return 0, 0, 0


def build_draft(timeline, new_id=None, probe=None):
    """Build the 剪映 draft_content dict and companion meta from a timeline."""
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


def export_timeline_to_jianying(timeline, out_dir, draft_name="recap", new_id=None,
                                probe=None, bundle_media=False):
    """Write a 剪映 draft folder under out_dir/draft_name. Returns (folder, notes).

    bundle_media=True copies the referenced media into the draft folder so it is
    self-contained and portable to another machine.
    """
    content, meta, notes = build_draft(timeline, new_id=new_id, probe=probe)
    return _write_draft(content, meta, notes, out_dir, draft_name, bundle_media_enabled=bundle_media)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Export a timeline.json to a 剪映/JianYing draft folder.")
    ap.add_argument("timeline", help="path to timeline.json")
    ap.add_argument("--out-dir", required=True, help="parent dir to create the draft folder in")
    ap.add_argument("--name", default="recap", help="draft folder name")
    ap.add_argument("--bundle-media", action="store_true",
                    help="copy referenced media into the draft folder (portable, self-contained)")
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
    print(json.dumps({"status": "exported", "draft_dir": draft_dir}, ensure_ascii=False))


if __name__ == "__main__":
    main()
