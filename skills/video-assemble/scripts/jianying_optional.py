"""Failure-isolated optional JianYing export invoked after canonical rendering."""

from pathlib import Path

from lib import CONFIG, log

def _maybe_export_jianying(work_dir, out_dir, stem):
    """Lazy-import the optional 剪映 exporter and write a draft from timeline.json."""
    timeline_path = Path(work_dir) / "timeline.json"
    if not timeline_path.exists():
        log("  ⚠️ 跳过剪映导出：未找到 timeline.json")
        return
    try:
        from export_jianying import export_timeline_to_jianying
        from timeline import load_timeline
        parent = out_dir or CONFIG.get("jianying_draft_dir") or str(work_dir)
        draft_dir, notes = export_timeline_to_jianying(
            load_timeline(timeline_path), parent, draft_name=f"recap_{stem}",
            bundle_media=CONFIG.get("jianying_bundle_media", False))
        for n in notes:
            log(f"  注意: {n}")
        log(f"剪映草稿已导出: {draft_dir}")
    except Exception as exc:  # optional feature must never fail the render
        log(f"  ⚠️ 剪映导出失败（不影响成片）: {exc}")
