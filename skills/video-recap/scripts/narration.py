import re
from pathlib import Path

from config import CONFIG
from common import log


# ── Agent narration preparation and validation helpers ────────────────


def _format_frame_facts(scene):
    """将帧动作描述格式化为可注入 agent brief 的文本。"""
    facts = scene.get("frame_facts", {})
    if not facts:
        return ""
    lines = []
    for ts in sorted(facts.keys(), key=lambda x: float(x)):
        actions = facts[ts]
        lines.append(f"    {ts}s: {'; '.join(actions)}")
    return "\n  帧动作:\n" + "\n".join(lines)


def _text_char_count(text):
    """计算文本的有效字数（去除标点和空白，这些不占 TTS 朗读时间）。"""
    return len(re.sub(r'[，。！？、；：…“”‘’《》〈〉\s"\'「」『』（）()【】\[\]—～·,.!?;:\\-]', '', text or ""))


def _truncate_at_sentence(text, max_chars):
    """在句子边界截断，不产生残句。max_chars 按有效字符计（不含标点空白）。"""
    if _text_char_count(text) <= max_chars:
        return text
    eff = 0
    cutoff = len(text)
    for i, ch in enumerate(text):
        eff += 1 if _text_char_count(ch) else 0
        if eff > max_chars:
            cutoff = i + 1
            break
    for sep in ['。', '！', '？', '!', '?']:
        idx = text[:cutoff].rfind(sep)
        if idx > 0:
            return text[:idx + 1]
    for sep in ['，', '、', '；', ',']:
        idx = text[:cutoff].rfind(sep)
        if idx > 3:
            return text[:idx] + '。'
    return ""


def _char_bigrams(text):
    return {text[i:i + 2] for i in range(len(text) - 1) if text[i:i + 2].strip()}


def _post_dedup_narration(narration):
    """去除相邻相似解说段（bigram Jaccard >40% 则合并）。"""
    if len(narration) < 2:
        return narration
    result = [narration[0]]
    for seg in narration[1:]:
        prev = result[-1]
        if not prev.get("narration", "").strip() or not seg.get("narration", "").strip():
            result.append(seg)
            continue
        set_a, set_b = _char_bigrams(prev["narration"]), _char_bigrams(seg["narration"])
        if not set_a or not set_b:
            result.append(seg)
            continue
        overlap = len(set_a & set_b) / min(len(set_a), len(set_b))
        if overlap > 0.4:
            if len(seg["narration"]) > len(prev["narration"]):
                prev["narration"] = seg["narration"]
            prev["end"] = seg["end"]
            prev["pause_after_ms"] = seg.get("pause_after_ms", prev.get("pause_after_ms", CONFIG.get("breath_ms", 250)))
            log(f"  去重合并: {prev['start']:.0f}-{prev['end']:.0f}s")
        else:
            result.append(seg)
    removed = len(narration) - len(result)
    if removed:
        log(f"  去重: {len(narration)} → {len(result)} 段 (合并 {removed} 段)")
    return result


def _scene_available_seconds(start, end, pause_after_ms=None):
    pause = (CONFIG.get("breath_ms", 250) if pause_after_ms is None else pause_after_ms) / 1000
    return max(0.0, float(end) - float(start) - pause)


def _recommended_char_budget(start, end, pause_after_ms=None):
    effective_rate = CONFIG["speech_rate"] * CONFIG["speech_safety_margin"]
    available = _scene_available_seconds(start, end, pause_after_ms)
    return max(0, int(available * effective_rate))


def _find_scene_for_midpoint(scenes_analysis, start, end):
    mid = (float(start) + float(end)) / 2
    for scene in scenes_analysis:
        if scene["start"] <= mid <= scene["end"]:
            return scene
    return None


def _normalise_narration_segment(seg, scenes_analysis=None):
    if not isinstance(seg, dict):
        return None
    try:
        start = float(seg.get("start"))
        end = float(seg.get("end"))
    except (TypeError, ValueError):
        return None
    if end <= start:
        return None
    text = str(seg.get("narration", "")).strip()
    if not text:
        return None
    pause = seg.get("pause_after_ms", CONFIG.get("breath_ms", 250))
    try:
        pause = int(pause)
    except (TypeError, ValueError):
        pause = CONFIG.get("breath_ms", 250)
    item = {
        "start": round(start, 2),
        "end": round(end, 2),
        "narration": text,
        "pause_after_ms": pause,
        "overlaps_speech": bool(seg.get("overlaps_speech", True)),
    }
    for optional_key in ("source_start", "source_end", "source_clip_id"):
        if optional_key in seg:
            item[optional_key] = seg[optional_key]
    if scenes_analysis:
        parent = _find_scene_for_midpoint(scenes_analysis, item["start"], item["end"])
        if parent:
            item["start"] = round(max(parent["start"], item["start"]), 2)
            item["end"] = round(min(parent["end"], item["end"]), 2)
            if item["end"] <= item["start"]:
                return None
    return item


def _lint_issue(level, index, code, message, **extra):
    issue = {"level": level, "index": index, "code": code, "message": message}
    issue.update(extra)
    return issue


def _scene_bounds_for_midpoint(scenes_analysis, start, end):
    scene = _find_scene_for_midpoint(scenes_analysis or [], start, end)
    if not scene:
        return None
    return float(scene.get("start", 0)), float(scene.get("end", 0))


def _clip_matches_for_segment(seg, clip_plan):
    if not clip_plan:
        return []
    clips = clip_plan.get("clips", clip_plan) if isinstance(clip_plan, dict) else clip_plan
    if not isinstance(clips, list):
        return []
    try:
        start = float(seg.get("start"))
        end = float(seg.get("end"))
    except (TypeError, ValueError):
        return []
    midpoint = (start + end) / 2
    if seg.get("source_clip_id") is not None:
        try:
            requested = int(seg.get("source_clip_id"))
        except (TypeError, ValueError):
            return []
        return [
            clip for clip in clips
            if clip.get("clip_id") == requested
            and float(clip.get("source_start", clip.get("start", 0)))
            <= midpoint
            <= float(clip.get("source_end", clip.get("end", 0)))
        ]
    return [
        clip for clip in clips
        if float(clip.get("source_start", clip.get("start", 0)))
        <= midpoint
        <= float(clip.get("source_end", clip.get("end", 0)))
    ]


def lint_narration(narration, scenes_analysis=None, *, clip_plan=None, mode="full", work_dir=None):
    """Preflight-check agent narration before TTS; write narration_lint.json when work_dir is set."""
    errors = []
    warnings = []
    normalized = []
    if not isinstance(narration, list):
        errors.append(_lint_issue("error", None, "invalid_json_shape", "narration.json must be a JSON array"))
    else:
        for idx, seg in enumerate(narration):
            if not isinstance(seg, dict):
                errors.append(_lint_issue("error", idx, "invalid_segment", "Narration segment must be an object"))
                continue
            try:
                start = float(seg.get("start"))
                end = float(seg.get("end"))
            except (TypeError, ValueError):
                errors.append(_lint_issue("error", idx, "invalid_time", "start/end must be numeric"))
                continue
            text = str(seg.get("narration", "")).strip()
            pause_raw = seg.get("pause_after_ms", CONFIG.get("breath_ms", 250))
            try:
                pause = int(pause_raw)
            except (TypeError, ValueError):
                pause = CONFIG.get("breath_ms", 250)
                warnings.append(_lint_issue(
                    "warning", idx, "invalid_pause",
                    "pause_after_ms is invalid; default will be used",
                ))
            if pause < 0:
                warnings.append(_lint_issue(
                    "warning", idx, "negative_pause",
                    "pause_after_ms is negative; default should be used",
                    pause_after_ms=pause,
                ))
            if end <= start:
                errors.append(_lint_issue(
                    "error", idx, "invalid_time_range",
                    "end must be greater than start", start=start, end=end,
                ))
                continue
            if not text:
                errors.append(_lint_issue(
                    "error", idx, "empty_narration",
                    "narration text must not be empty", start=start, end=end,
                ))
                continue

            char_count = _text_char_count(text)
            budget = _recommended_char_budget(start, end, pause)
            estimated_tts_seconds = char_count / max(CONFIG.get("speech_rate", 3.5), 0.1)
            slot_seconds = _scene_available_seconds(start, end, pause)
            if budget < 5:
                warnings.append(_lint_issue(
                    "warning", idx, "slot_too_short",
                    "Narration slot is very short; TTS may be clipped",
                    start=start, end=end, budget_chars=budget,
                ))
            elif char_count > budget:
                warnings.append(_lint_issue(
                    "warning", idx, "over_budget", "Text may exceed the available TTS slot",
                    start=start, end=end, budget_chars=budget, actual_chars=char_count,
                    estimated_tts_seconds=round(estimated_tts_seconds, 2), slot_seconds=round(slot_seconds, 2),
                ))
            if text[-1] not in "。！？!?…":
                warnings.append(_lint_issue(
                    "warning", idx, "incomplete_sentence",
                    "Narration should end with a complete sentence punctuation",
                    text_tail=text[-8:],
                ))

            scene_bounds = _scene_bounds_for_midpoint(scenes_analysis, start, end)
            if scenes_analysis and not scene_bounds:
                warnings.append(_lint_issue(
                    "warning", idx, "outside_scene",
                    "Narration midpoint does not match any detected scene",
                    start=start, end=end,
                ))
            elif scene_bounds and (start < scene_bounds[0] or end > scene_bounds[1]):
                warnings.append(_lint_issue(
                    "warning", idx, "crosses_scene_boundary", "Narration extends outside its midpoint scene boundary",
                    start=start, end=end, scene_start=scene_bounds[0], scene_end=scene_bounds[1],
                ))

            if mode == "cut":
                matches = _clip_matches_for_segment(seg, clip_plan)
                if not matches:
                    errors.append(_lint_issue(
                        "error", idx, "outside_clip_plan",
                        "Cut-mode narration must fall inside a selected clip",
                        start=start, end=end,
                    ))
                elif len(matches) > 1 and seg.get("source_clip_id") is None:
                    errors.append(_lint_issue(
                        "error", idx, "ambiguous_source_clip",
                        "Repeated/overlapping clips require source_clip_id",
                        start=start, end=end,
                    ))
                if seg.get("source_clip_id") is not None:
                    try:
                        int(seg.get("source_clip_id"))
                    except (TypeError, ValueError):
                        errors.append(_lint_issue("error", idx, "invalid_source_clip_id", "source_clip_id must be an integer"))

            normalized.append({"index": idx, "start": start, "end": end, "char_count": char_count})

    sorted_segments = sorted(normalized, key=lambda item: item["start"])
    for prev, curr in zip(sorted_segments, sorted_segments[1:]):
        if curr["start"] < prev["end"]:
            errors.append(_lint_issue(
                "error", curr["index"], "time_overlap", "Segment overlaps the previous narration segment",
                previous_index=prev["index"], previous_end=prev["end"], start=curr["start"], end=curr["end"],
            ))

    # Density / continuous-bed style check (full mode only; cut-mode density must be
    # measured on the mapped output timeline, not the source timestamps used here).
    metrics = {}
    if mode == "full" and len(sorted_segments) >= 2:
        span = sorted_segments[-1]["end"] - sorted_segments[0]["start"]
        gaps = [curr["start"] - prev["end"] for prev, curr in zip(sorted_segments, sorted_segments[1:])]
        max_gap = max(gaps) if gaps else 0.0
        spm = len(sorted_segments) / (span / 60) if span > 0 else 0.0
        min_spm = CONFIG.get("min_segments_per_minute", 6.24)
        target_spm = CONFIG.get("target_segments_per_minute", 9.6)
        max_gap_limit = CONFIG.get("max_narration_gap_seconds", 11.0)
        metrics = {
            "segment_count": len(sorted_segments),
            "timeline_span_seconds": round(span, 2),
            "segments_per_minute": round(spm, 2),
            "max_gap_seconds": round(max_gap, 2),
            "target_segments_per_minute": target_spm,
            "min_segments_per_minute": min_spm,
            "max_gap_limit_seconds": max_gap_limit,
        }
        if spm and spm < min_spm:
            warnings.append(_lint_issue(
                "warning", None, "low_density",
                "Narration density is below the continuous-bed target; add more short beats",
                segments_per_minute=round(spm, 2), min_segments_per_minute=min_spm,
                target_segments_per_minute=target_spm,
            ))
        if max_gap > max_gap_limit:
            warnings.append(_lint_issue(
                "warning", None, "long_gap",
                "A gap between narration beats exceeds the continuous-bed maximum",
                max_gap_seconds=round(max_gap, 2), max_gap_limit_seconds=max_gap_limit,
            ))

    report = {
        "ok": not errors,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "metrics": metrics,
        "errors": errors,
        "warnings": warnings,
    }
    if work_dir is not None:
        import json
        Path(work_dir, "narration_lint.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return report


def validate_narration_or_raise(narration, scenes_analysis=None, *, clip_plan=None, mode="full", work_dir=None):
    report = lint_narration(narration, scenes_analysis, clip_plan=clip_plan, mode=mode, work_dir=work_dir)
    if report["errors"]:
        sample = "; ".join(f"#{e.get('index')}: {e['code']}" for e in report["errors"][:3])
        raise ValueError(f"narration.json 预检失败: {sample}; 详见 narration_lint.json")
    if report["warnings"]:
        log(f"narration lint: {len(report['warnings'])} warnings (see narration_lint.json)")
    else:
        log("narration lint: ok")
    return report


def _validate_narration_budget(narration, scenes_analysis):
    """Validate agent-written narration against timing budgets; trim impossible text safely."""
    if not isinstance(narration, list):
        raise ValueError("narration.json 必须是 JSON 数组")

    cleaned = []
    for raw in narration:
        item = _normalise_narration_segment(raw, scenes_analysis)
        if not item:
            continue
        max_chars = _recommended_char_budget(item["start"], item["end"], item.get("pause_after_ms"))
        if max_chars < 5:
            log(f"  丢弃过短解说段 {item['start']:.1f}-{item['end']:.1f}s")
            continue
        if _text_char_count(item["narration"]) > max_chars * 1.25:
            truncated = _truncate_at_sentence(item["narration"], max_chars)
            if truncated and _text_char_count(truncated) >= 5:
                log(f"  解说超预算，已截短: {item['start']:.1f}-{item['end']:.1f}s")
                item["narration"] = truncated
            else:
                log(f"  解说超预算且无法安全截断，已丢弃: {item['start']:.1f}-{item['end']:.1f}s")
                continue
        item["narration"] = _clean_narration_punctuation(item["narration"])
        if item["narration"].strip()[-1] in "，：、；,—…":
            item["narration"] += "。"
        cleaned.append(item)

    cleaned.sort(key=lambda n: n["start"])
    deduped = []
    for item in cleaned:
        if deduped and item["start"] < deduped[-1]["end"]:
            prev = deduped[-1]
            log(
                f"  解说时间重叠: {item['start']:.1f}-{item['end']:.1f}s vs "
                f"{prev['start']:.1f}-{prev['end']:.1f}s"
            )
            if _text_char_count(item["narration"]) > _text_char_count(prev["narration"]):
                deduped[-1] = item
        else:
            deduped.append(item)
    return _post_dedup_narration(deduped)


def _clean_narration_punctuation(text):
    text = re.sub(r'\s+', ' ', text or '').strip()
    text = re.sub(r'[，：、；,]["\']?[。！？]', '。', text)
    text = re.sub(r'["\']。$', '。', text)
    return text


def _scene_asr_lines(asr_result, scene):
    lines = []
    for seg in asr_result or []:
        try:
            start = float(seg.get("start", 0))
            end = float(seg.get("end", start))
        except (TypeError, ValueError):
            continue
        if scene["start"] < end and scene["end"] > start:
            text = str(seg.get("text", "")).strip()
            if text:
                lines.append(f"    [{start:.1f}-{end:.1f}] {text}")
    return lines


def _quiet_windows_for_scene(silence_periods, scene):
    windows = []
    for qp in silence_periods or []:
        if qp.get("has_speech", False):
            continue
        if qp["start"] < scene["end"] and qp["end"] > scene["start"]:
            start = max(qp["start"], scene["start"])
            end = min(qp["end"], scene["end"])
            if end > start:
                windows.append((start, end))
    return windows


def build_agent_brief(scenes_analysis, asr_result, silence_periods, video_duration, work_dir):
    """Write a compact brief that tells the agent exactly how to author recap artifacts."""
    effective_rate = CONFIG["speech_rate"] * CONFIG["speech_safety_margin"]
    breath_sec = CONFIG.get("breath_ms", 250) / 1000
    target_pause_ms = CONFIG.get("breath_ms", 250)
    target_spm = CONFIG.get("target_segments_per_minute", 9.6)
    min_spm = CONFIG.get("min_segments_per_minute", 6.24)
    max_gap = CONFIG.get("max_narration_gap_seconds", 11.0)
    edit_mode = CONFIG.get("edit_mode", "full")
    target_duration = CONFIG.get("target_duration") or "(not set)"
    # 粗略估算应写多少段（连续覆盖）；真正约束是上面的每分钟密度
    target_count = max(1, round(video_duration / 60 * target_spm))
    lines = [
        "# Agent Narration Brief",
        "",
        "Write the required JSON artifact(s) manually from the analysis files in this work directory.",
        "The CLI will not generate final narration text; it will only validate timing, run TTS, and assemble the video.",
        "",
        f"- Edit mode: {edit_mode}",
        f"- Source video duration: {video_duration:.1f}s",
        f"- Target duration (cut mode): {target_duration}",
        f"- Effective speech budget: {effective_rate:.2f} Chinese chars/sec after {breath_sec:.2f}s pause allowance",
        f"- Narration density target: ~{target_spm:.1f} segments/min (minimum {min_spm:.1f}); no gap longer than {max_gap:.0f}s",
        f"- Aim for roughly {target_count} short beats across the timeline, kept continuous over a ducked original-audio bed",
        f"- Default pause between beats: {target_pause_ms}ms",
        f"- Context: {CONFIG.get('context_info') or '(none)'}",
        "",
    ]

    if edit_mode == "cut":
        lines.extend([
            "## Required files for cut mode",
            "",
            "First write `clip_plan.json` to choose source footage, then write `narration.json` using ORIGINAL source timestamps inside those clips.",
            "The CLI maps source timestamps to the edited timeline after concatenating clips.",
            "",
            "### clip_plan.json shape",
            "",
            "```json",
            "{",
            "  \"target_duration\": \"10m\",",
            "  \"clips\": [",
            "    {\"start\": 12.0, \"end\": 38.0, \"reason\": \"关键冲突开端\"}",
            "  ]",
            "}",
            "```",
            "",
            "### narration.json shape (source timestamps)",
            "",
            "```json",
            "[",
            "  {\"start\": 14.0, \"end\": 19.0, \"narration\": \"解说文本。\", \"pause_after_ms\": 250, \"overlaps_speech\": true}",
            "]",
            "```",
        ])
    else:
        lines.extend([
            "## Required JSON shape",
            "",
            "```json",
            "[",
            "  {\"start\": 5.0, \"end\": 10.0, \"narration\": \"解说文本。\", \"pause_after_ms\": 250, \"overlaps_speech\": true}",
            "]",
            "```",
        ])

    lines.extend([
        "",
        "## Writing rules (dense continuous-bed recap style)",
        "",
        "1. Narrate continuously across the whole timeline as short punchy beats, keeping the original audio alive underneath as a ducked bed.",
        f"2. Hit the density target: ~{target_spm:.1f} beats/min (at least {min_spm:.1f}); never leave a gap longer than {max_gap:.0f}s without narration.",
        "3. Default `overlaps_speech` to true — narration rides over the original audio. Set it false only for a beat you deliberately place in a real silent gap.",
        "4. Keep each beat short: roughly one short sentence (1-2 subtitle lines). Shorter is safer for edge-tts and reads better.",
        f"5. Use a short `pause_after_ms` (default {target_pause_ms}) to keep the rhythm tight; avoid long dead air between beats.",
        "6. Do not describe what the viewer can already see; explain intent, stakes, subtext, relationships, and story logic.",
        "7. Use known character names when `--context` or background research provides them.",
        "8. In cut mode, select clips for plot causality, key dialogue, reveals, and emotional turns; avoid filler and repeated shots.",
        "9. After writing, run: `python3 skills/video-recap/scripts/video_recap.py <video> --resume <work_dir>`.",
        "",
        "## Scene timing guide",
        "",
    ])

    for scene in scenes_analysis:
        duration = scene["end"] - scene["start"]
        max_chars = max(5, int(max(1.0, duration - breath_sec) * effective_rate))
        quiets = _quiet_windows_for_scene(silence_periods, scene)
        quiet_text = ", ".join(f"{s:.1f}-{e:.1f}s" for s, e in quiets) or "none"
        lines.extend([
            f"### Scene {scene['scene_id'] + 1}: {scene['start']:.1f}-{scene['end']:.1f}s",
            f"- Duration: {duration:.1f}s; max budget if fully narrated: {max_chars} chars",
            f"- Silent gaps (optional, for overlaps_speech=false beats): {quiet_text}",
            f"- Description: {scene.get('description', '')}",
        ])
        if scene.get("depth_analysis"):
            lines.append(f"- Deeper analysis: {scene['depth_analysis']}")
        facts = _format_frame_facts(scene)
        if facts:
            lines.append(facts.rstrip())
        asr_lines = _scene_asr_lines(asr_result, scene)
        if asr_lines:
            lines.append("- ASR overlap:")
            lines.extend(asr_lines[:8])
        lines.append("")

    brief_path = Path(work_dir) / "agent_narration_brief.md"
    brief_path.write_text("\n".join(lines), encoding="utf-8")
    log(f"已写入 Agent 解说写作 brief: {brief_path}")
    return brief_path
