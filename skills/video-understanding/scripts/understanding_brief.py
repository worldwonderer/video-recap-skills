"""Format research context and rebuild briefs from cached analysis."""

import json

from pathlib import Path

from lib import CONFIG, log


from detect import detect_speech_boundary_anchors


from brief import build_agent_brief, assess_understanding_substrate


from understanding_cache import _load_json, _merge_overview_into_scenes
from understanding_storyboard import (
    _generate_edited_storyboard,
    _generate_source_storyboard,
    _prepend_storyboard_brief_header,
)


def _clip_text(text, limit):
    value = " ".join(str(text or "").split()).strip()
    return value[:limit]


def _research_context(work_dir):
    """Fold background_research.json into a compact context string for the VLM prompt.

    The agent does story research first (per references/research-guide.md) and writes
    work_dir/background_research.json; this surfaces synopsis, named characters,
    relationships, plot arcs, and cultural notes so scene VLM analysis can name people
    and read scenes with plot knowledge instead of labelling everyone "黑衣男子".
    Returns "" when no usable research file is present, so behaviour is unchanged.
    """
    path = Path(work_dir) / "background_research.json"
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    if not isinstance(data, dict):
        return ""
    parts = []
    for key in ("synopsis", "episode_context", "worldbuilding"):
        val = _clip_text(data.get(key), 320)
        if val:
            parts.append(val)
    chars = data.get("characters")
    if isinstance(chars, dict) and chars:
        named = "；".join(
            f"{_clip_text(name, 40)}（{_clip_text(desc, 120)}）"
            for name, desc in list(chars.items())[:12]
            if _clip_text(name, 40)
        )
        if named:
            parts.append("主要人物：" + named)
    details = data.get("character_details")
    if isinstance(details, dict) and details:
        detail_lines = []
        for name, info in list(details.items())[:8]:
            if not isinstance(info, dict):
                continue
            bits = []
            aliases = info.get("aliases")
            if isinstance(aliases, list) and aliases:
                clean_aliases = [_clip_text(alias, 30) for alias in aliases[:3]]
                clean_aliases = [alias for alias in clean_aliases if alias]
                if clean_aliases:
                    bits.append("别名" + "/".join(clean_aliases))
            role = _clip_text(info.get("role"), 60)
            if role:
                bits.append(role)
            rels = info.get("relationships")
            if isinstance(rels, list) and rels:
                clean_rels = [_clip_text(rel, 60) for rel in rels[:3]]
                bits.extend(rel for rel in clean_rels if rel)
            clean_name = _clip_text(name, 40)
            if clean_name and bits:
                detail_lines.append(f"{clean_name}（{'；'.join(bits)}）")
        if detail_lines:
            parts.append("人物关系：" + "；".join(detail_lines))
    arcs = data.get("plot_arcs")
    if isinstance(arcs, list) and arcs:
        arc_lines = []
        for arc in arcs[:6]:
            if not isinstance(arc, dict):
                val = _clip_text(arc, 120)
                if val:
                    arc_lines.append(val)
                continue
            name = _clip_text(arc.get("name"), 50)
            desc = _clip_text(arc.get("description"), 120)
            status = _clip_text(arc.get("status"), 30)
            if name or desc:
                tail = f"[{status}]" if status else ""
                arc_lines.append(f"{name}：{desc}{tail}".strip("："))
        if arc_lines:
            parts.append("剧情线：" + "；".join(arc_lines))
    notes = data.get("cultural_notes")
    if isinstance(notes, list) and notes:
        note_lines = []
        for note in notes[:4]:
            if not isinstance(note, dict):
                val = _clip_text(note, 100)
                if val:
                    note_lines.append(val)
                continue
            item = _clip_text(note.get("item"), 50)
            expl = _clip_text(note.get("explanation"), 100)
            if item or expl:
                note_lines.append(f"{item}：{expl}".strip("："))
        if note_lines:
            parts.append("背景注释：" + "；".join(note_lines))
    return " ".join(parts).strip()[:1200]


def _load_understanding_artifacts_for_brief(work_dir):
    """Load existing analysis artifacts for a brief-only regeneration pass.

    Material-library restores are allowed to reuse expensive analysis artifacts,
    but cut pass 2 must still rebuild `agent_narration_brief.md` against the
    rendered OUTPUT timeline. This helper deliberately performs no extraction,
    ASR, VLM, or external API calls; it only reads already-present JSON.
    """
    work_dir = Path(work_dir)
    scenes = []
    for name in ("vlm_analysis.json", "scenes.json"):
        path = work_dir / name
        if not path.exists():
            continue
        try:
            data = _load_json(path)
        except (OSError, ValueError, TypeError):
            continue
        if isinstance(data, list):
            scenes = data
            break
    asr_result = []
    if (work_dir / "asr_result.json").exists():
        try:
            data = _load_json(work_dir / "asr_result.json")
        except (OSError, ValueError, TypeError):
            data = []
        if isinstance(data, list):
            asr_result = data
    silence_periods = []
    if (work_dir / "silence_periods.json").exists():
        try:
            data = _load_json(work_dir / "silence_periods.json")
        except (OSError, ValueError, TypeError):
            data = []
        if isinstance(data, list):
            silence_periods = data
    return scenes, asr_result, silence_periods


def _write_brief_from_existing_artifacts(video, work_dir, args, video_duration):
    """Regenerate only the agent brief/timeline-fusion from existing artifacts."""
    scenes, asr_result, silence_periods = _load_understanding_artifacts_for_brief(
        work_dir
    )
    if not (Path(work_dir) / "speech_boundary_anchors.json").exists():
        detect_speech_boundary_anchors(work_dir, asr_result)
    overview_path = Path(work_dir) / "mimo_video_overview.json"
    if CONFIG.get("mimo_video_overview", False):
        scenes = _merge_overview_into_scenes(scenes, overview_path)

    source_storyboard = None
    edited_storyboard = None
    scenes_json = Path(work_dir) / "scenes.json"
    if scenes_json.exists():
        source_storyboard = _generate_source_storyboard(
            work_dir, Path(video), scenes, scenes_json, force=False
        )
    edited_storyboard = _generate_edited_storyboard(work_dir, video, force=False)
    cut_mode = (Path(work_dir) / "clip_plan_validated.json").exists()

    substrate = assess_understanding_substrate(scenes, asr_result)
    if substrate["level"] != "rich":
        banner = "理解素材为空" if substrate["level"] == "empty" else "理解素材偏薄"
        log(
            f"⚠️  {banner}：ASR {substrate['asr_chars']} 字 | 场景 {substrate['scene_count']} | "
            f"带 frame_facts 的场景 {substrate['scenes_with_frame_facts']} | 平均画面描述 {substrate['avg_description_len']} 字"
        )
    brief_path = build_agent_brief(
        scenes,
        asr_result,
        silence_periods,
        video_duration,
        work_dir,
        args.style,
        mimo_overview_enabled=CONFIG.get("mimo_video_overview", False),
        mimo_overview_video_path=video,
    )
    _prepend_storyboard_brief_header(
        brief_path, source_storyboard, edited_storyboard, cut_mode=cut_mode
    )
    log("=" * 50)
    log(f"brief-only 完成。写作 brief: {brief_path}")
    print(
        json.dumps(
            {
                "status": "brief_only",
                "work_dir": str(work_dir),
                "brief": str(brief_path),
                "substrate": substrate["level"],
                "scenes": len(scenes),
                "asr_segments": len(asr_result),
            },
            ensure_ascii=False,
        )
    )
