#!/usr/bin/env python3
"""video-recap inspect — advisory, read-only orientation over a recap work_dir.

Pure stdlib. Reads ONLY the JSON artifacts already in work_dir (no ffmpeg, no frame
reads, no video probing, no new deps, no cross-skill import). Every missing or malformed
artifact degrades to a clear human message — never a traceback.

Two subcommands:
  state     summarize the work_dir: source video + fingerprint, full|cut mode, which stage
            artifacts are present vs missing, which file is the NEXT pause the pipeline waits
            on, stale-manifest risk, and storyboard path(s) if present.
  clip-map  read clip_plan_validated.json and map a queried window between the OUTPUT and
            SOURCE timelines using the SAME forward affine map cut.py uses
            (output = clip.output_start + (src - clip.source_start), clamped to the clip),
            reimplemented locally. Flags cross-clip boundaries and cut-out gaps.

Output: markdown by default; --json for machine-readable; --compact (default ON) truncates
long free text — pass --full to keep it.
"""
import argparse
import json
from pathlib import Path


# --- artifact catalog --------------------------------------------------------
# Stage artifacts probed by `state`. Order = rough pipeline order.
_UNDERSTANDING_ARTIFACTS = [
    "scenes.json",
    "asr_result.json",
    "silence_periods.json",
    "vlm_analysis.json",
    "understanding_index.json",
    "agent_narration_brief.md",
]
_CUT_ARTIFACTS = [
    "clip_plan.json",
    "clip_plan_validated.json",
    "edited_source.mp4",
]
_SCRIPT_ARTIFACTS = [
    "narration.json",
    "narration_lint.json",
    "narration_review.json",
    "original_subtitles.json",
]
_RENDER_ARTIFACTS = [
    "tts_meta.json",
    "narration_mapped.json",
    "timeline.json",
    "subtitles.srt",
    "subtitles.ass",
    "assembly_manifest.json",
]
_STORYBOARD_ARTIFACTS = [
    "storyboard/source_storyboard.json",
    "storyboard/edited_storyboard.json",
]
# Forward-compat: if a write-side state file ever lands, prefer it as the state source.
_FORWARD_STATE_FILES = ["manifest.json", "task_state.json"]

_COMPACT_TEXT_LIMIT = 80


def _truncate(text, compact):
    text = str(text or "").strip()
    if not compact or len(text) <= _COMPACT_TEXT_LIMIT:
        return text
    return text[: _COMPACT_TEXT_LIMIT - 1] + "…"


def _load_json(path):
    """Return (data, error). error is a human string when the file is missing/malformed."""
    path = Path(path)
    if not path.exists():
        return None, f"{path.name} 不存在"
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except ValueError:
        return None, f"{path.name} 不是合法 JSON（可能写坏了）"
    except OSError as exc:
        return None, f"{path.name} 读取失败: {exc}"


def _fmt_seconds(value):
    """mm:ss for a numeric seconds value, or the raw value when not numeric."""
    try:
        total = float(value)
    except (TypeError, ValueError):
        return str(value)
    sign = "-" if total < 0 else ""
    total = abs(total)
    m = int(total // 60)
    s = total % 60
    return f"{sign}{m:02d}:{s:05.2f}"


# --- source video discovery --------------------------------------------------
def _discover_source(work_dir):
    """Find the source video path + fingerprint by reading recap artifacts, most authoritative
    first. Returns a dict {path, fingerprint, origin} with None values + origin="unknown" when
    nothing records it. Never raises."""
    # 1. recap_run_manifest.json — canonical: written before the first pause with both fields.
    data, _ = _load_json(Path(work_dir) / "recap_run_manifest.json")
    if isinstance(data, dict) and data.get("source_video"):
        return {
            "path": data.get("source_video"),
            "fingerprint": data.get("source_video_fingerprint"),
            "origin": "recap_run_manifest.json",
        }
    # 2. assembly_manifest.json — late stage; carries input_video (+ source_video in cut mode).
    data, _ = _load_json(Path(work_dir) / "assembly_manifest.json")
    if isinstance(data, dict) and (data.get("source_video") or data.get("input_video")):
        return {
            "path": data.get("source_video") or data.get("input_video"),
            "fingerprint": data.get("source_video_fingerprint"),
            "origin": "assembly_manifest.json",
        }
    # 3. edited_source.mp4.meta.json — cut mode; fingerprint only, no path.
    data, _ = _load_json(Path(work_dir) / "edited_source.mp4.meta.json")
    if isinstance(data, dict) and data.get("source_video_fingerprint"):
        return {
            "path": None,
            "fingerprint": data.get("source_video_fingerprint"),
            "origin": "edited_source.mp4.meta.json",
        }
    return {"path": None, "fingerprint": None, "origin": "unknown"}


# --- clip plan loading + forward affine map ----------------------------------
def _clip_entries(plan):
    """Normalize a clip plan (dict-with-clips or bare list) to the list of clip dicts."""
    if isinstance(plan, dict):
        entries = plan.get("clips", [])
    elif isinstance(plan, list):
        entries = plan
    else:
        return None
    return entries if isinstance(entries, list) else None


def _normalize_clips(entries):
    """Coerce raw clip entries to {clip_id, source_start, source_end, output_start, output_end}.

    Mirrors assemble._output_clip_spans / _build_video_clips field access: source_start/source_end
    fall back to start/end; when output_start/output_end are absent they are derived with the same
    forward cursor cut.py uses (sum of prior kept-clip durations). Bad rows are skipped, not fatal.
    """
    clips, cursor = [], 0.0
    for idx, c in enumerate(entries or []):
        if not isinstance(c, dict):
            continue
        try:
            ss = float(c.get("source_start", c.get("start")))
            se = float(c.get("source_end", c.get("end")))
        except (TypeError, ValueError):
            continue
        if se - ss <= 0:
            continue
        out_s, out_e = c.get("output_start"), c.get("output_end")
        if out_s is None or out_e is None:
            out_s, out_e = cursor, cursor + (se - ss)
            cursor += se - ss
        else:
            try:
                out_s, out_e = float(out_s), float(out_e)
            except (TypeError, ValueError):
                out_s, out_e = cursor, cursor + (se - ss)
                cursor += se - ss
            else:
                cursor = max(cursor, out_e)
        clips.append({
            "clip_id": c.get("clip_id", idx),
            "source_start": ss,
            "source_end": se,
            "output_start": out_s,
            "output_end": out_e,
            "reason": str(c.get("reason", c.get("note", ""))).strip(),
        })
    return clips


def _source_to_output(src, clip):
    """The forward affine map cut.py:319 uses, clamped to the clip's output span."""
    mapped = clip["output_start"] + (float(src) - clip["source_start"])
    return round(max(clip["output_start"], min(mapped, clip["output_end"])), 3)


def _output_to_source(out, clip):
    """Inverse of the forward affine map (same slope, clamped to the clip's source span)."""
    mapped = clip["source_start"] + (float(out) - clip["output_start"])
    return round(max(clip["source_start"], min(mapped, clip["source_end"])), 3)


def _overlap(a0, a1, b0, b1):
    """Intersection [lo, hi] of two closed ranges, or None when they do not overlap.

    A zero-width query (a0 == a1, e.g. --output-start 10 --output-end 10) is treated as a
    POINT lookup: it returns (a0, a0) when the point lies within [b0, b1], so a point that sits
    inside a clip is reported instead of silently falling through to "not in any clip".
    """
    if a0 == a1:
        return (a0, a0) if b0 <= a0 <= b1 else None
    lo, hi = max(a0, b0), min(a1, b1)
    return (lo, hi) if hi > lo else None


# --- state subcommand --------------------------------------------------------
def _present(work_dir, name):
    return (Path(work_dir) / name).exists()


def _detect_mode(work_dir):
    """cut when any cut artifact is present, else full."""
    if any(_present(work_dir, n) for n in _CUT_ARTIFACTS):
        return "cut"
    return "full"


def _next_pause(work_dir, mode):
    """The artifact the pipeline is currently waiting on the agent to write, or None when the
    next-needed input is already present. Mirrors recap.py's two-pause cut flow / single-pause full
    flow purely from file presence (advisory; the orchestrator owns the real decision)."""
    if mode == "cut":
        if not _present(work_dir, "clip_plan.json"):
            return ("clip_plan.json", "pass1: 写剪辑计划（只写 clip_plan.json）")
        if not _present(work_dir, "narration.json"):
            return ("narration.json", "pass2: 对着剪好的成片用 OUTPUT 时间轴写解说")
        return None
    if not _present(work_dir, "narration.json"):
        return ("narration.json", "写解说 narration.json")
    return None


def _stale_manifest_note(work_dir, mode):
    """Advisory stale-manifest risks read purely from JSON (no fingerprint recompute — that would
    need the source bytes). Surfaces the two desync traps recap.py guards: a cut narration written
    against an older clip_plan, and a missing run manifest."""
    notes = []
    if not _present(work_dir, "recap_run_manifest.json"):
        notes.append("缺少 recap_run_manifest.json：无法证明 work_dir 属于当前视频/参数（resume 会被拒绝）。")
    if mode == "cut" and _present(work_dir, "narration.json"):
        ledger, _ = _load_json(Path(work_dir) / "recap_phase.json")
        if isinstance(ledger, dict):
            recorded = ledger.get("clip_plan_fingerprint")
            if recorded is None:
                notes.append("recap_phase.json 未记录 clip_plan_fingerprint：无法判断 narration 是否对当前剪辑写的。")
        else:
            notes.append("有 narration.json 但缺少 recap_phase.json：无法判断解说是否对当前剪辑写的（可能 stale）。")
    return notes


def _present_storyboards(work_dir):
    return [n for n in _STORYBOARD_ARTIFACTS if _present(work_dir, n)]


def cmd_state(work_dir, compact):
    work_dir = Path(work_dir)
    if not work_dir.exists():
        return {"error": f"work_dir 不存在: {work_dir}"}

    forward = [n for n in _FORWARD_STATE_FILES if _present(work_dir, n)]
    mode = _detect_mode(work_dir)
    source = _discover_source(work_dir)

    groups = {
        "understanding": _UNDERSTANDING_ARTIFACTS,
        "cut": _CUT_ARTIFACTS,
        "script": _SCRIPT_ARTIFACTS,
        "render": _RENDER_ARTIFACTS,
    }
    artifacts = {}
    for group, names in groups.items():
        artifacts[group] = {
            "present": [n for n in names if _present(work_dir, n)],
            "missing": [n for n in names if not _present(work_dir, n)],
        }

    pause = _next_pause(work_dir, mode)
    return {
        "work_dir": str(work_dir),
        "mode": mode,
        "forward_state_files": forward,
        "source_video": source,
        "next_pause": (
            {"artifact": pause[0], "hint": pause[1]} if pause else None
        ),
        "artifacts": artifacts,
        "storyboards": _present_storyboards(work_dir),
        "stale_manifest_notes": _stale_manifest_note(work_dir, mode),
    }


def _render_state_md(state, compact):
    if "error" in state:
        return state["error"]
    lines = [f"# recap work_dir 状态: {state['work_dir']}", ""]
    if state["forward_state_files"]:
        lines.append(f"状态来源（write-side manifest）: {', '.join(state['forward_state_files'])}")
    lines.append(f"模式: **{state['mode']}**")
    src = state["source_video"]
    path = src["path"] or "unknown"
    fp = src["fingerprint"]
    fp_short = (fp[:12] + "…") if isinstance(fp, str) and len(fp) > 12 else (fp or "unknown")
    lines.append(f"源视频: {_truncate(path, compact)}  (fp {fp_short}, 来源 {src['origin']})")
    lines.append("")

    if state["next_pause"]:
        np = state["next_pause"]
        lines.append(f"下一步暂停 → 等待写入 **{np['artifact']}**  ({np['hint']})")
    else:
        lines.append("下一步暂停 → 无（所需输入已就绪，可继续 voiceover/assemble）")
    lines.append("")

    lines.append("## 各阶段产物")
    for group, label in (("understanding", "理解"), ("cut", "剪辑"),
                         ("script", "解说"), ("render", "渲染")):
        info = state["artifacts"][group]
        lines.append(f"### {label}")
        lines.append(f"  有: {', '.join(info['present']) or '（无）'}")
        lines.append(f"  缺: {', '.join(info['missing']) or '（无）'}")
    lines.append("")

    if state["storyboards"]:
        lines.append("## storyboard")
        for s in state["storyboards"]:
            lines.append(f"  - {s}")
        lines.append("")

    lines.append("## stale-manifest 风险")
    if state["stale_manifest_notes"]:
        for n in state["stale_manifest_notes"]:
            lines.append(f"  ⚠️ {n}")
    else:
        lines.append("  无明显 stale 风险。")
    return "\n".join(lines)


# --- clip-map subcommand -----------------------------------------------------
def cmd_clip_map(work_dir, output_start, output_end, source_start, source_end, compact):
    work_dir = Path(work_dir)
    validated = work_dir / "clip_plan_validated.json"
    if not validated.exists():
        return {"error": "clip_plan_validated.json 不存在：这不是 cut 运行，或剪辑计划尚未被 cut.py 校验。"
                         "（full 模式没有源↔输出映射；cut 模式请先跑 video-cut。）"}
    plan, err = _load_json(validated)
    if err:
        return {"error": err}
    entries = _clip_entries(plan)
    if entries is None:
        return {"error": "clip_plan_validated.json 结构异常：既不是 clips 数组也不是 {\"clips\": [...]}。"}
    clips = _normalize_clips(entries)
    if not clips:
        return {"error": "clip_plan_validated.json 没有有效的 clip（每段需要 source_start/source_end）。"}

    have_output = output_start is not None or output_end is not None
    have_source = source_start is not None or source_end is not None
    if not have_output and not have_source:
        return {"error": "请用 --output-start/--output-end 或 --source-start/--source-end 指定要查询的窗口。"}

    result = {
        "work_dir": str(work_dir),
        "clip_count": len(clips),
        "source_span": [clips[0]["source_start"], clips[-1]["source_end"]],
        "output_span": [clips[0]["output_start"], clips[-1]["output_end"]],
        "queries": [],
    }
    if have_output:
        result["queries"].append(
            _map_output_window(output_start, output_end, clips, compact))
    if have_source:
        result["queries"].append(
            _map_source_window(source_start, source_end, clips, compact))
    return result


def _map_output_window(out_start, out_end, clips, compact):
    """Map a queried OUTPUT window to its SOURCE window(s), one per touched clip."""
    out_lo = clips[0]["output_start"] if out_start is None else float(out_start)
    out_hi = clips[-1]["output_end"] if out_end is None else float(out_end)
    if out_hi < out_lo:
        out_lo, out_hi = out_hi, out_lo
    segments = []
    for clip in clips:
        ov = _overlap(out_lo, out_hi, clip["output_start"], clip["output_end"])
        if not ov:
            continue
        segments.append({
            "clip_id": clip["clip_id"],
            "output": [round(ov[0], 3), round(ov[1], 3)],
            "source": [_output_to_source(ov[0], clip), _output_to_source(ov[1], clip)],
            "reason": _truncate(clip.get("reason"), compact),
        })
    return {
        "direction": "output→source",
        "query": [round(out_lo, 3), round(out_hi, 3)],
        "clips_touched": [s["clip_id"] for s in segments],
        "cross_clip_boundary": len(segments) > 1,
        "segments": segments,
        # An OUTPUT window can't fall outside any clip (output is contiguous), so no cut-out gaps.
        "cut_out_source_gaps": [],
    }


def _map_source_window(src_start, src_end, clips, compact):
    """Map a queried SOURCE window to its OUTPUT window(s), flagging source ranges in no clip."""
    src_lo = clips[0]["source_start"] if src_start is None else float(src_start)
    src_hi = clips[-1]["source_end"] if src_end is None else float(src_end)
    if src_hi < src_lo:
        src_lo, src_hi = src_hi, src_lo
    segments = []
    covered = []
    for clip in clips:
        ov = _overlap(src_lo, src_hi, clip["source_start"], clip["source_end"])
        if not ov:
            continue
        covered.append(ov)
        segments.append({
            "clip_id": clip["clip_id"],
            "source": [round(ov[0], 3), round(ov[1], 3)],
            "output": [_source_to_output(ov[0], clip), _source_to_output(ov[1], clip)],
            "reason": _truncate(clip.get("reason"), compact),
        })
    # Cut-out gaps: parts of [src_lo, src_hi] covered by NO kept clip (footage that was cut away).
    gaps = []
    cursor = src_lo
    for lo, hi in sorted(covered):
        if lo > cursor:
            gaps.append([round(cursor, 3), round(lo, 3)])
        cursor = max(cursor, hi)
    if cursor < src_hi:
        gaps.append([round(cursor, 3), round(src_hi, 3)])
    return {
        "direction": "source→output",
        "query": [round(src_lo, 3), round(src_hi, 3)],
        "clips_touched": [s["clip_id"] for s in segments],
        "cross_clip_boundary": len(segments) > 1,
        "segments": segments,
        "cut_out_source_gaps": gaps,
    }


def _render_clip_map_md(result, compact):
    if "error" in result:
        return result["error"]
    lines = [f"# clip-map: {result['work_dir']}", ""]
    lines.append(
        f"{result['clip_count']} clips · "
        f"源 {_fmt_seconds(result['source_span'][0])}–{_fmt_seconds(result['source_span'][1])} · "
        f"输出 {_fmt_seconds(result['output_span'][0])}–{_fmt_seconds(result['output_span'][1])}")
    for q in result["queries"]:
        lines.append("")
        lines.append(f"## {q['direction']}  查询 "
                     f"{_fmt_seconds(q['query'][0])}–{_fmt_seconds(q['query'][1])}")
        if q["cross_clip_boundary"]:
            lines.append(f"  ⚠️ 跨剪辑边界（涉及 clip {q['clips_touched']}）")
        if not q["segments"]:
            lines.append("  （该窗口不落在任何保留片段内）")
        for s in q["segments"]:
            src = f"{_fmt_seconds(s['source'][0])}–{_fmt_seconds(s['source'][1])}"
            out = f"{_fmt_seconds(s['output'][0])}–{_fmt_seconds(s['output'][1])}"
            reason = f"  〔{s['reason']}〕" if s.get("reason") else ""
            lines.append(f"  clip {s['clip_id']}: 源 {src}  ↔  输出 {out}{reason}")
        if q["cut_out_source_gaps"]:
            lines.append("  ✂️ 被剪掉的源区间（不在成片里）:")
            for g in q["cut_out_source_gaps"]:
                lines.append(f"     {_fmt_seconds(g[0])}–{_fmt_seconds(g[1])}")
    return "\n".join(lines)


# --- CLI ---------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="recap_inspect.py",
        description="Advisory read-only inspection of a video-recap work_dir (pure JSON).")
    parser.add_argument("--work-dir", required=True, help="recap work_dir to inspect")
    parser.add_argument("--json", action="store_true", help="machine-readable JSON output")
    compact = parser.add_mutually_exclusive_group()
    compact.add_argument("--compact", dest="compact", action="store_true", default=True,
                         help="truncate long free text to keep agent context small (default ON)")
    compact.add_argument("--full", dest="compact", action="store_false",
                         help="do not truncate long free text")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("state", help="summarize work_dir + the next pause the pipeline is waiting on")

    cm = sub.add_parser("clip-map", help="map a window between OUTPUT and SOURCE timelines")
    cm.add_argument("--output-start", type=float, default=None)
    cm.add_argument("--output-end", type=float, default=None)
    cm.add_argument("--source-start", type=float, default=None)
    cm.add_argument("--source-end", type=float, default=None)

    args = parser.parse_args(argv)

    if args.command == "state":
        result = cmd_state(args.work_dir, args.compact)
        rendered = _render_state_md(result, args.compact)
    else:
        result = cmd_clip_map(args.work_dir, args.output_start, args.output_end,
                              args.source_start, args.source_end, args.compact)
        rendered = _render_clip_map_md(result, args.compact)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
