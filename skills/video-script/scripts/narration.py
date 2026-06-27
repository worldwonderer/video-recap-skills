import hashlib
import json
import math
import re
import shlex
from pathlib import Path

from lib import CONFIG, file_fingerprint, stable_hash
from lib import log


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


def _contains_cjk(text):
    return bool(re.search(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", text or ""))


def _writing_text_units(text):
    """Length unit used for ASR writing chunks: CJK chars, otherwise words."""
    text = str(text or "")
    if _contains_cjk(text):
        return len(re.sub(r"\s+", "", text))
    return len(re.findall(r"\b\w+\b", text))


def _sentence_pieces(text):
    """Split text into sentence-like pieces while keeping terminal punctuation."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text:
        return []
    parts = re.split(r"([。！？!?；;.])", text)
    cleaned = []
    for idx in range(0, len(parts), 2):
        body = parts[idx].strip()
        punct = parts[idx + 1] if idx + 1 < len(parts) else ""
        if body or punct:
            cleaned.append((body + punct).strip())
    return cleaned or [text]


def _split_text_by_sentence_windows(text, min_chars=500, max_chars=800):
    """Clipto-style three-tier sentence boundary splitting for long ASR text."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text:
        return []
    if _writing_text_units(text) <= max_chars:
        return _sentence_pieces(text)

    result = []
    rest = text
    sentence_marks = "。！？!?；;."
    while _writing_text_units(rest) > max_chars:
        # The min/max thresholds are unit-based but punctuation is char-indexed.
        # For Chinese (the dominant recap target) these are nearly identical; for
        # non-CJK this remains a safe sentence-boundary heuristic around words.
        char_min = min(len(rest), max(1, min_chars))
        char_max = min(len(rest), max(1, max_chars))
        window = rest[:char_max]
        cut = max(window.rfind(mark) for mark in sentence_marks)
        if cut + 1 < char_min:
            outside = -1
            for i, ch in enumerate(rest[char_max:], start=char_max):
                if ch in sentence_marks:
                    outside = i
                    break
            if outside >= 0 and outside + 1 <= len(rest):
                cut = outside
            else:
                cut = char_max - 1
        piece = rest[:cut + 1].strip()
        if not piece:
            piece = rest[:char_max].strip()
            cut = char_max - 1
        result.append(piece)
        rest = rest[cut + 1:].strip()
    if rest:
        result.append(rest)
    return result


def _timed_sentence_pieces(seg, min_chars, max_chars):
    """Split one ASR segment into timed sentence pieces with approximate spans."""
    text = str(seg.get("text", "")).strip()
    if not text:
        return []
    try:
        start = float(seg.get("start", 0))
        end = float(seg.get("end", start))
    except (TypeError, ValueError):
        start = end = 0.0
    if end < start:
        end = start
    pieces = []
    for sentence in _sentence_pieces(text):
        if _writing_text_units(sentence) > max_chars:
            pieces.extend(_split_text_by_sentence_windows(sentence, min_chars=min_chars, max_chars=max_chars))
        else:
            pieces.append(sentence)
    total_units = sum(max(1, _writing_text_units(piece)) for piece in pieces) or 1
    duration = max(0.0, end - start)
    cursor = start
    timed = []
    for idx, piece in enumerate(pieces):
        units = max(1, _writing_text_units(piece))
        piece_duration = duration * units / total_units if duration else 0.0
        piece_end = end if idx == len(pieces) - 1 else cursor + piece_duration
        timed.append({
            "start": round(cursor, 2),
            "end": round(piece_end, 2),
            "text": piece,
            "char_count": _writing_text_units(piece),
        })
        cursor = piece_end
    return timed


def _scene_ids_for_range(scenes, start, end):
    scene_ids = []
    duration = max(0.001, float(end) - float(start))
    for scene in scenes or []:
        try:
            s_start = float(scene.get("start", 0))
            s_end = float(scene.get("end", s_start))
        except (TypeError, ValueError):
            continue
        overlap = _overlap_seconds(start, end, s_start, s_end)
        # Ignore tiny boundary tails from approximate ASR sentence timing. A
        # scene id should mean the chunk materially belongs to that scene.
        if overlap and (overlap >= duration * 0.2 or overlap >= 3.0):
            scene_ids.append(scene.get("scene_id"))
    return [sid for sid in scene_ids if sid is not None]


def _chunk_asr_for_writing(segments, scenes_analysis=None, min_chars=None, max_chars=None):
    """Chunk ASR into semantic windows before an agent writes long-dialogue recaps.

    The strategy mirrors Clipto's segment splitter: accumulate a window, prefer
    the last sentence boundary inside max length, allow a slightly longer first
    boundary outside the window, and fall back to the remaining text. CJK text is
    measured by characters; non-CJK text is measured by words.
    """
    min_chars = int(min_chars or CONFIG.get("asr_chunk_min_chars", 500))
    max_chars = int(max_chars or CONFIG.get("asr_chunk_max_chars", 800))
    min_chars = max(1, min(min_chars, max_chars))
    max_chars = max(min_chars, max_chars)

    pieces = []
    for seg in segments or []:
        if isinstance(seg, dict):
            pieces.extend(_timed_sentence_pieces(seg, min_chars, max_chars))

    chunks = []
    current = []
    current_units = 0
    current_scene_ids = set()

    def flush():
        nonlocal current, current_units, current_scene_ids
        if not current:
            return
        chunks.append({
            "chunk_id": len(chunks),
            "start": round(float(current[0]["start"]), 2),
            "end": round(float(current[-1]["end"]), 2),
            "scene_ids": sorted(current_scene_ids, key=lambda sid: (isinstance(sid, str), sid)),
            "char_count": current_units,
            "text": " ".join(piece["text"] for piece in current).strip(),
            "segments": current,
        })
        current = []
        current_units = 0
        current_scene_ids = set()

    for piece in pieces:
        units = max(1, int(piece.get("char_count", _writing_text_units(piece.get("text", "")))))
        piece_scene_ids = set(_scene_ids_for_range(scenes_analysis, piece["start"], piece["end"]))
        crosses_scene = current and current_scene_ids and piece_scene_ids and not (current_scene_ids & piece_scene_ids)
        if crosses_scene and current_units >= min_chars:
            flush()
        if current and current_units >= min_chars and current_units + units > max_chars:
            flush()
        current.append(piece)
        current_scene_ids.update(piece_scene_ids)
        current_units += units
        if current_units >= max_chars:
            flush()
    flush()
    return chunks


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
    idx = max(text[:cutoff].rfind(sep) for sep in ['。', '！', '？', '!', '?'])
    if idx > 0:
        return text[:idx + 1]
    idx = max(text[:cutoff].rfind(sep) for sep in ['，', '、', '；', ','])
    if idx > 3:
        return text[:idx] + '。'
    return ""


def _char_bigrams(text):
    return {text[i:i + 2] for i in range(len(text) - 1) if text[i:i + 2].strip()}


def _post_dedup_narration(narration):
    """去除相邻相似解说段（bigram 重叠 >60% 则合并）。"""
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
        # Only merge near-identical adjacent beats. Short Chinese beats share many
        # bigrams by chance, so a low threshold collapses intentional parallel beats
        # ("他不再试探" / "他直接赌上全力") and silently drops density below target.
        if overlap > 0.6:
            if len(seg["narration"]) > len(prev["narration"]):
                prev["narration"] = seg["narration"]
            prev["end"] = seg["end"]
            prev["pause_after_ms"] = seg.get("pause_after_ms", prev.get("pause_after_ms", 600))
            log(f"  去重合并: {prev['start']:.0f}-{prev['end']:.0f}s")
        else:
            result.append(seg)
    removed = len(narration) - len(result)
    if removed:
        log(f"  去重: {len(narration)} → {len(result)} 段 (合并 {removed} 段)")
    return result


def _scene_available_seconds(start, end, pause_after_ms=None):
    del pause_after_ms  # pause_after_ms affects the next segment gap during assembly, not current speech capacity.
    tail_pad = max(0.0, float(CONFIG.get("narration_tail_pad_seconds", 0.1) or 0.0))
    return max(0.0, float(end) - float(start) - tail_pad)


def _recommended_char_budget(start, end, pause_after_ms=None):
    # account for the global narration atempo (CONFIG['narration_speed']) so a beat's text
    # is budgeted against the FINAL sped-up audio, not the raw TTS rate — otherwise windows
    # are over-sized and the bed shows long silent gaps between sentences.
    effective_rate = CONFIG["speech_rate"] * CONFIG["speech_safety_margin"] * float(CONFIG.get("narration_speed", 1.0) or 1.0)
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
    # carry the per-beat emotion/tone tag (MiMo TTS instruct) through lint untouched
    emotion = seg.get("emotion")
    if isinstance(emotion, str) and emotion.strip():
        item["emotion"] = emotion.strip()
    if scenes_analysis:
        parent = _find_scene_for_midpoint(scenes_analysis, item["start"], item["end"])
        if parent:
            clamped_start = round(max(parent["start"], item["start"]), 2)
            clamped_end = round(min(parent["end"], item["end"]), 2)
            # Only tighten the window to the midpoint scene when the authored text
            # still fits the clamped span. A multi-sentence BLOCK is meant to span
            # the cut and play across scenes; shrinking it here would make
            # _validate_narration_budget/_truncate_at_sentence drop trailing
            # sentences the author wrote — keep the author's timing in that case.
            if clamped_end > clamped_start and (
                _text_char_count(item["narration"])
                <= _recommended_char_budget(clamped_start, clamped_end, item.get("pause_after_ms")) * 1.25
            ):
                item["start"] = clamped_start
                item["end"] = clamped_end
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


def _frame_fact_times_for_segment(scenes_analysis, start, end):
    """Return frame-fact timestamps covered by a narration segment.

    These timestamps are cheap visual anchors. A narration slot that spans too
    many anchors is more likely to drift into "general story summary" instead
    of staying attached to what is on screen.
    """
    times = []
    scene = _find_scene_for_midpoint(scenes_analysis or [], start, end)
    if not scene:
        return times
    for raw_ts in (scene.get("frame_facts") or {}).keys():
        try:
            ts = float(raw_ts)
        except (TypeError, ValueError):
            continue
        if float(start) <= ts <= float(end):
            times.append(ts)
    return sorted(times)


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
            # estimate at the REAL playback rate (after the narration_speed atempo); otherwise a
            # beat sized to its 1.3x-sped slot looks "over budget" when it actually fits.
            play_rate = max(CONFIG.get("speech_rate", 3.5) * float(CONFIG.get("narration_speed", 1.0) or 1.0), 0.1)
            estimated_tts_seconds = char_count / play_rate
            slot_seconds = _scene_available_seconds(start, end, pause)
            if budget < 5:
                warnings.append(_lint_issue(
                    "warning", idx, "slot_too_short",
                    "Narration slot is very short; TTS may be clipped",
                    start=start, end=end, budget_chars=budget,
                ))
            elif estimated_tts_seconds > slot_seconds:
                warnings.append(_lint_issue(
                    "warning", idx, "over_budget", "Text may exceed the available TTS slot",
                    start=start, end=end, budget_chars=budget, actual_chars=char_count,
                    estimated_tts_seconds=round(estimated_tts_seconds, 2), slot_seconds=round(slot_seconds, 2),
                ))
            if text[-1] not in "。！？!?….":
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

            frame_fact_times = _frame_fact_times_for_segment(scenes_analysis, start, end)
            max_visual_seconds = float(CONFIG.get("visual_beat_max_seconds", 18.0) or 18.0)
            max_visual_facts = int(CONFIG.get("visual_beat_max_facts", 3) or 3)
            if (end - start) > max_visual_seconds and len(frame_fact_times) > max_visual_facts:
                warnings.append(_lint_issue(
                    "warning", idx, "visual_beat_too_broad",
                    "Narration spans many visual anchors; split or tighten timing so the voiceover stays tied to current pictures",
                    start=start, end=end, duration=round(end - start, 2),
                    frame_fact_times=[round(ts, 2) for ts in frame_fact_times[:8]],
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
                if len(matches) == 1:
                    clip = matches[0]
                    clip_start = float(clip.get("source_start", clip.get("start", start)))
                    clip_end = float(clip.get("source_end", clip.get("end", end)))
                    if start < clip_start or end > clip_end:
                        warnings.append(_lint_issue(
                            "warning", idx, "crosses_clip_boundary",
                            "Narration extends beyond its clip; it will be trimmed to the clip and may describe footage that was cut",
                            start=start, end=end,
                            clip_start=round(clip_start, 3), clip_end=round(clip_end, 3),
                        ))
                if seg.get("source_clip_id") is not None:
                    try:
                        int(seg.get("source_clip_id"))
                    except (TypeError, ValueError):
                        errors.append(_lint_issue("error", idx, "invalid_source_clip_id", "source_clip_id must be an integer"))

            normalized.append({"index": idx, "start": start, "end": end, "char_count": char_count})

    sorted_segments = sorted(normalized, key=lambda item: item["start"])
    if isinstance(narration, list) and not sorted_segments:
        errors.append(_lint_issue(
            "error", None, "empty_narration_file",
            "narration.json must contain at least one valid narration segment",
        ))
    for prev, curr in zip(sorted_segments, sorted_segments[1:]):
        if curr["start"] < prev["end"]:
            errors.append(_lint_issue(
                "error", curr["index"], "time_overlap", "Segment overlaps the previous narration segment",
                previous_index=prev["index"], previous_end=prev["end"], start=curr["start"], end=curr["end"],
            ))

    # Block-coverage check (full mode only; cut-mode density is measured on the mapped output
    # timeline, not the source timestamps used here). Model: narration is delivered in BLOCKS — each
    # beat is a few sentences synthesized as ONE fluent TTS utterance — and the deliberate stretches
    # BETWEEN blocks are "original-audio blocks" that play at full volume. We aim for roughly 7:3
    # narrated:original, so we flag (a) wall-to-wall narration that never lets the original breathe,
    # (b) long under-narrated stretches, (c) no deliberate original-audio gaps at all, and (d) beats
    # fragmented into single short sentences that would synthesize as choppy per-sentence audio.
    metrics = {}
    if mode == "full" and len(sorted_segments) >= 2:
        span = sorted_segments[-1]["end"] - sorted_segments[0]["start"]
        # Score coverage at the SAME conservative rate the writer is budgeted at
        # (_recommended_char_budget uses speech_rate * speech_safety_margin * narration_speed),
        # else the metric scores ~18% stricter than its own budget and false-flags under_narrated.
        play_rate = max(
            CONFIG["speech_rate"]
            * float(CONFIG.get("speech_safety_margin", 0.85) or 0.85)
            * float(CONFIG.get("narration_speed", 1.0) or 1.0),
            0.1,
        )
        spoken = [s["char_count"] / play_rate for s in sorted_segments]
        spoken_ends = [sorted_segments[i]["start"] + spoken[i] for i in range(len(sorted_segments))]
        narrated_seconds = sum(spoken)
        coverage = narrated_seconds / span if span > 0 else 0.0
        orig_min = CONFIG.get("original_block_min_seconds", 2.5)
        orig_gaps = [sorted_segments[i + 1]["start"] - spoken_ends[i] for i in range(len(sorted_segments) - 1)]
        original_blocks = sum(1 for g in orig_gaps if g >= orig_min)
        avg_chars = sum(s["char_count"] for s in sorted_segments) / len(sorted_segments)
        cov_target = CONFIG.get("narration_coverage_target", 0.7)
        cov_max = CONFIG.get("narration_coverage_max", 0.85)
        cov_min = CONFIG.get("narration_coverage_min", 0.5)
        block_min_chars = CONFIG.get("narration_block_min_chars", 16)
        metrics = {
            "segment_count": len(sorted_segments),
            "timeline_span_seconds": round(span, 2),
            "narrated_seconds": round(narrated_seconds, 2),
            "narration_coverage": round(coverage, 2),
            "coverage_target": cov_target,
            "original_block_count": original_blocks,
            "avg_block_chars": round(avg_chars, 1),
        }
        if coverage > cov_max:
            warnings.append(_lint_issue(
                "warning", None, "no_original_blocks",
                "Narration is nearly wall-to-wall — the original audio never gets to breathe. Pull back at a "
                "few strong moments and write NO narration there so the original plays at full volume "
                "(aim ~7:3 narrated:original).",
                narration_coverage=round(coverage, 2), coverage_max=cov_max,
            ))
        elif coverage < cov_min:
            warnings.append(_lint_issue(
                "warning", None, "under_narrated",
                "Large stretches have no narration — the recap goes quiet too long. Add blocks so narration "
                "covers most of the timeline (aim ~7:3 narrated:original).",
                narration_coverage=round(coverage, 2), coverage_min=cov_min,
            ))
        if original_blocks == 0 and span >= 3 * orig_min:
            warnings.append(_lint_issue(
                "warning", None, "no_original_breaks",
                "No deliberate original-audio blocks — narration runs end-to-end with no gap for a strong "
                "original moment (a key line, an action beat, the music). Leave a few multi-second gaps "
                "between blocks where the original plays alone.",
                original_block_min_seconds=orig_min,
            ))
        if len(sorted_segments) >= 8 and avg_chars < block_min_chars:
            warnings.append(_lint_issue(
                "warning", None, "fragmented_beats",
                "Beats are fragmented into single short sentences; each is synthesized as a separate TTS "
                "utterance, which sounds choppy. Merge adjacent sentences into BLOCKS of 2-4 sentences "
                "(one continuous thought) so each block speaks as one fluent utterance.",
                avg_block_chars=round(avg_chars, 1), block_min_chars=block_min_chars,
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
        stripped = item["narration"].strip()
        if stripped and stripped[-1] in "，：、；,—":
            item["narration"] = stripped.rstrip("，：、；,—") + "。"
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


def _align_narration_to_quiet(narration, scenes_analysis, silence_periods):
    """Recompute overlaps_speech from real quiet windows; keep the agent's timing.

    The dense continuous-bed design places narration ON the pictured beat over a
    ducked original bed, so we no longer relocate segments into silence gaps. The
    old shift moved voiceover up to 3s off the moment it was written for and could
    silently blank a squeezed segment; both fought the design intent. We now only
    correct the overlaps_speech flag that the ducking stage consumes, leaving the
    agent's start/end (and text) intact.
    """
    if not silence_periods:
        for n in narration:
            n["overlaps_speech"] = True
        return _validate_narration_budget(narration, scenes_analysis)

    quiet_windows = [qp for qp in silence_periods if not qp.get("has_speech", False)]
    quiet_ratio_min = float(CONFIG.get("quiet_overlap_min_ratio", 0.8) or 0.8)

    # Run budget/dedup FIRST, then set the flag on the final (possibly merged) spans,
    # so a dedup-merged beat's overlaps_speech reflects its extended timing, not its
    # original shorter span (which the ducking stage in assemble.py would mis-duck).
    aligned = _validate_narration_budget(narration, scenes_analysis)
    for n in aligned:
        try:
            seg_start = float(n["start"])
            seg_end = float(n["end"])
        except (KeyError, TypeError, ValueError):
            n["overlaps_speech"] = True
            continue
        seg_dur = max(0.0, seg_end - seg_start)
        quiet_overlap = _quiet_overlap_seconds(seg_start, seg_end, quiet_windows)
        n["overlaps_speech"] = quiet_overlap < max(0.3, seg_dur * quiet_ratio_min)

    return aligned


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


def _quiet_overlap_seconds(start, end, quiet_windows):
    overlap_seconds = 0.0
    for qw in quiet_windows:
        try:
            if isinstance(qw, dict):
                q_start = float(qw.get("start", 0))
                q_end = float(qw.get("end", q_start))
            else:
                q_start, q_end = qw
                q_start = float(q_start)
                q_end = float(q_end)
        except (TypeError, ValueError):
            continue
        overlap_seconds += _overlap_seconds(start, end, q_start, q_end)
    return overlap_seconds


def _overlap_seconds(start, end, other_start, other_end):
    return max(0.0, min(float(end), float(other_end)) - max(float(start), float(other_start)))


def _build_timeline_fusion(scenes, asr_segments, silence_periods):
    """Fuse VLM scenes, ASR dialogue and quiet narration slots on one timeline."""
    fusion = []
    quiet_windows = [w for w in silence_periods or [] if not w.get("has_speech", False)]
    for scene in scenes or []:
        try:
            start = float(scene.get("start", 0))
            end = float(scene.get("end", start))
        except (TypeError, ValueError):
            continue
        dialogue_segments = []
        dialogue_overlap = 0.0
        for seg in asr_segments or []:
            if not isinstance(seg, dict):
                continue
            try:
                seg_start = float(seg.get("start", 0))
                seg_end = float(seg.get("end", seg_start))
            except (TypeError, ValueError):
                continue
            overlap = _overlap_seconds(start, end, seg_start, seg_end)
            if overlap <= 0:
                continue
            text = str(seg.get("text", "")).strip()
            dialogue_overlap += overlap
            dialogue_segments.append({
                "start": round(seg_start, 2),
                "end": round(seg_end, 2),
                "overlap_seconds": round(overlap, 2),
                "text": text,
            })

        narration_slots = []
        for window in quiet_windows:
            try:
                w_start = float(window.get("start", 0))
                w_end = float(window.get("end", w_start))
            except (TypeError, ValueError):
                continue
            overlap = _overlap_seconds(start, end, w_start, w_end)
            if overlap <= 0:
                continue
            slot_start = max(start, w_start)
            slot_end = min(end, w_end)
            narration_slots.append({
                "start": round(slot_start, 2),
                "end": round(slot_end, 2),
                "duration": round(slot_end - slot_start, 2),
                "char_budget": _recommended_char_budget(slot_start, slot_end),
            })

        fusion.append({
            "scene_id": scene.get("scene_id"),
            "time_range": [round(start, 2), round(end, 2)],
            "visual_description": scene.get("description", ""),
            "depth_analysis": scene.get("depth_analysis", ""),
            "frame_facts": scene.get("frame_facts", {}),
            "dialogue_segments": dialogue_segments,
            "dialogue_overlap_seconds": round(dialogue_overlap, 2),
            "narration_slots": narration_slots,
            "recommended_mode": "quiet-slot" if narration_slots and dialogue_overlap < (end - start) * 0.4 else "ducked-bed",
        })
    return fusion


def _load_background_research(work_dir):
    """Load the agent-authored background_research.json if present.

    This file is the single highest-leverage quality input (character names,
    relationships, plot context). It used to be documented but never read by
    any code, so researched story knowledge never reached the writing brief.
    """
    path = Path(work_dir) / "background_research.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log(f"警告: background_research.json 读取失败，忽略: {exc}")
        return {}
    return data if isinstance(data, dict) else {}


def _clip_text(text, limit):
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value[:limit]


def _format_background_research(research, limit=1800):
    """Render background_research.json into a bounded Story-context brief section."""
    if not isinstance(research, dict) or not research:
        return []
    lines = ["## Story context (from background_research.json)", ""]
    for key, label in (
        ("synopsis", "Synopsis"),
        ("worldbuilding", "Worldbuilding"),
        ("episode_context", "Episode context"),
    ):
        value = _clip_text(research.get(key), 500)
        if value:
            lines.append(f"- {label}: {value}")

    characters = research.get("characters")
    if isinstance(characters, dict) and characters:
        lines.append("- Characters:")
        for name, desc in list(characters.items())[:12]:
            clean_name = _clip_text(name, 60)
            clean_desc = _clip_text(desc, 160)
            if clean_name:
                lines.append(f"    - {clean_name}: {clean_desc}")

    details = research.get("character_details")
    if isinstance(details, dict) and details:
        lines.append("- Character details:")
        for name, info in list(details.items())[:8]:
            if not isinstance(info, dict):
                continue
            bits = []
            aliases = info.get("aliases")
            if isinstance(aliases, list) and aliases:
                clean_aliases = [_clip_text(alias, 40) for alias in aliases[:4]]
                clean_aliases = [alias for alias in clean_aliases if alias]
                if clean_aliases:
                    bits.append("别名 " + "/".join(clean_aliases))
            role = _clip_text(info.get("role"), 80)
            if role:
                bits.append(role)
            rels = info.get("relationships")
            if isinstance(rels, list) and rels:
                clean_rels = [_clip_text(rel, 80) for rel in rels[:4]]
                clean_rels = [rel for rel in clean_rels if rel]
                if clean_rels:
                    bits.append("；".join(clean_rels))
            clean_name = _clip_text(name, 60)
            if clean_name and bits:
                lines.append(f"    - {clean_name}: {'; '.join(bits)}")

    arcs = research.get("plot_arcs")
    if isinstance(arcs, list) and arcs:
        lines.append("- Plot arcs:")
        for arc in arcs[:8]:
            if not isinstance(arc, dict):
                value = _clip_text(arc, 180)
                if value:
                    lines.append(f"    - {value}")
                continue
            name = _clip_text(arc.get("name"), 80)
            desc = _clip_text(arc.get("description"), 180)
            status = _clip_text(arc.get("status"), 40)
            if name or desc:
                tail = f" [{status}]" if status else ""
                lines.append(f"    - {name}: {desc}{tail}".rstrip())

    notes = research.get("cultural_notes")
    if isinstance(notes, list) and notes:
        lines.append("- Cultural notes:")
        for note in notes[:6]:
            if not isinstance(note, dict):
                value = _clip_text(note, 160)
                if value:
                    lines.append(f"    - {value}")
                continue
            item = _clip_text(note.get("item"), 80)
            expl = _clip_text(note.get("explanation"), 160)
            if item and expl:
                lines.append(f"    - {item}: {expl}")
            elif item or expl:
                lines.append(f"    - {item or expl}")

    lines.extend([
        "",
        "Use these names, relationships, and stakes in the narration instead of generic labels like \"男子\"/\"白发女子\".",
        "",
    ])
    text = "\n".join(lines)
    if len(text) <= limit:
        return lines
    clipped = text[:limit].rsplit("\n", 1)[0].rstrip()
    return clipped.splitlines() + ["", "[Story context clipped to keep ASR/visual evidence in context]", ""]


def assess_understanding_substrate(scenes_analysis, asr_result, *, has_story_context=False):
    """Measure how much real signal the writing agent has to work with.

    Recap quality collapses to generic 看图说话 when the agent has no story spine and
    only literal frame descriptions to paraphrase. A spine is substantial dialogue
    (ASR) OR researched/given story context — frame-fact VOLUME alone (a visually busy
    but storyless clip, e.g. an anime whose dialogue ASR could not read) is NOT a spine,
    so it must not grade as "rich"; otherwise the sparse-substrate warning, the research
    directive, and the density relief never fire for exactly that cold-narration case.
    """
    scenes = scenes_analysis or []
    asr_chars = sum(len(str(seg.get("text", "")).strip()) for seg in (asr_result or []))
    scenes_with_facts = sum(1 for s in scenes if isinstance(s, dict) and s.get("frame_facts"))
    desc_lens = [len(str(s.get("description", "")).strip()) for s in scenes if isinstance(s, dict)]
    avg_desc = sum(desc_lens) // len(desc_lens) if desc_lens else 0

    has_asr = asr_chars >= 20
    has_facts = scenes_with_facts > 0
    has_story_spine = asr_chars >= 200 or bool(has_story_context)
    if not has_asr and not has_facts and avg_desc < 25:
        level = "empty"
    elif (has_asr or has_facts) and has_story_spine:
        level = "rich"
    else:
        level = "thin"
    return {
        "level": level,
        "asr_chars": asr_chars,
        "scene_count": len(scenes),
        "scenes_with_frame_facts": scenes_with_facts,
        "avg_description_len": avg_desc,
        "has_story_context": bool(has_story_context),
    }


def _format_substrate_warning(assessment):
    """Render a loud brief banner when the understanding substrate is weak."""
    if not assessment or assessment.get("level") == "rich":
        return []
    if assessment["level"] == "empty":
        head = "⚠️ UNDERSTANDING SUBSTRATE IS EMPTY — narration will be generic guesswork unless you fix this first."
    else:
        head = "⚠️ Understanding substrate is THIN — narration risks generic \"看图说话\" without more grounding."
    return [
        head,
        f"  ASR chars: {assessment['asr_chars']} | scenes: {assessment['scene_count']} | "
        f"scenes with frame_facts: {assessment['scenes_with_frame_facts']} | avg description: {assessment['avg_description_len']} chars",
        "  Before writing: do background research (write background_research.json with names/relationships/plot),",
        "  lean on any --context provided, and use ASR dialogue + frame_facts as the factual spine.",
        "  Do NOT invent plot. If you truly have nothing, keep beats sparse and factual rather than fabricating drama.",
        "",
    ]


def _write_json_artifact(work_dir, name, payload):
    path = Path(work_dir) / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _format_asr_chunks_for_brief(chunks, max_chunks=24):
    if not chunks:
        return []
    lines = [
        "## ASR writing chunks (semantic windows)",
        "",
        "Use these chunks as the dialogue spine instead of swallowing the full transcript at once.",
        "Full data: `asr_writing_chunks.json`.",
        "",
    ]
    for chunk in chunks[:max_chunks]:
        scene_ids = ",".join(str(sid) for sid in chunk.get("scene_ids", [])) or "n/a"
        text = str(chunk.get("text", "")).strip()
        if len(text) > 900:
            text = text[:897] + "..."
        lines.extend([
            f"### ASR chunk {chunk['chunk_id'] + 1}: {chunk['start']:.1f}-{chunk['end']:.1f}s | scenes {scene_ids} | {chunk['char_count']} units",
            text or "(empty transcript chunk)",
            "",
        ])
    if len(chunks) > max_chunks:
        lines.append(f"... {len(chunks) - max_chunks} more chunks in `asr_writing_chunks.json`.")
        lines.append("")
    return lines


def _format_timeline_fusion_for_brief(fusion, max_items=40):
    if not fusion:
        return []
    lines = [
        "## Timeline fusion (VLM + ASR + quiet slots)",
        "",
        "This is the pre-aligned multimodal view. Use `narration_slots` when available; otherwise write ducked-bed beats around dialogue.",
        "Full data: `timeline_fusion.json`.",
        "",
    ]
    for item in fusion[:max_items]:
        start, end = item.get("time_range", [0, 0])
        slots = item.get("narration_slots") or []
        slot_text = ", ".join(
            f"{slot['start']:.1f}-{slot['end']:.1f}s/{slot['char_budget']}字"
            for slot in slots[:4]
        ) or "none"
        dialogue = item.get("dialogue_segments") or []
        dialogue_text = "; ".join(
            f"{seg['start']:.1f}-{seg['end']:.1f}s {seg.get('text', '')[:80]}"
            for seg in dialogue[:3]
            if seg.get("text")
        ) or "none"
        lines.extend([
            f"### Fusion scene {item.get('scene_id')}: {start:.1f}-{end:.1f}s ({item.get('recommended_mode')})",
            f"- Visual: {item.get('visual_description', '')}",
            f"- Dialogue overlap: {item.get('dialogue_overlap_seconds', 0):.1f}s | {dialogue_text}",
            f"- Narration slots: {slot_text}",
            "",
        ])
    if len(fusion) > max_items:
        lines.append(f"... {len(fusion) - max_items} more fused scenes in `timeline_fusion.json`.")
        lines.append("")
    return lines


def _consolidation_model():
    return CONFIG.get("vlm_model", "")


def _clean_asr_prompt_fingerprint():
    # Keep this literal in sync with consolidate.CLEAN_PROMPT without importing it;
    # brief.py and narration.py must remain byte-identical cross-skill copies.
    prompt = """你在清洗中文视频的 ASR 逐段转写。对【每一段】做：补标点、修明显同音/错别字、（能判断时）在句首轻标说话人，让长段连读文本变成清晰可读的句子。
铁律：
- 不要合并或拆分段落，输出段数必须与输入完全一致，顺序一致。
- 不要改时间，不要输出 start/end（时间由程序保留）。
- 只清洗 text，不要增删事实、不要脑补画面。
只返回 JSON：{"segments":[{"i":0,"text":"清洗后的文本","speaker":"可选说话人"}, ...]}，i 为输入段的下标。"""
    return hashlib.md5(prompt.encode("utf-8")).hexdigest()


def _index_prompt_fingerprint():
    prompt = """你在根据逐场景画面分析，为一个视频建立【全局理解索引】，供后续写解说词时保持人物/关系/主线一致。
只依据给到的画面证据（场景描述 + 帧实动作），不要脑补画面之外的剧情。
只返回 JSON：
{"characters":[{"name":"角色名或外观指代","description":"身份/特征"}],
 "relationships":[{"a":"角色","b":"角色","relation":"关系"}],
 "plot_points":["按时间顺序的关键剧情节点"],
 "entities":["重要物件/地点/线索"]}"""
    return hashlib.md5(prompt.encode("utf-8")).hexdigest()


def _load_consolidation(work_dir, scenes_analysis=None):
    """Load consolidate.py's understanding_index.json only when provenance matches VLM input."""
    work_dir = Path(work_dir)
    path = work_dir / "understanding_index.json"
    meta_path = work_dir / "understanding_index.json.meta.json"
    if not path.exists() or not meta_path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict) or not isinstance(meta, dict):
        return {}
    src_path = work_dir / "vlm_analysis.json"
    try:
        source_md5 = hashlib.md5(src_path.read_bytes()).hexdigest()
    except OSError:
        return {}
    if meta.get("source_md5") != source_md5:
        return {}
    expected_count = len([s for s in (scenes_analysis or []) if isinstance(s, dict)])
    if expected_count and meta.get("scene_count") != expected_count:
        return {}
    if meta.get("model") != _consolidation_model():
        return {}
    if meta.get("prompt_md5") != _index_prompt_fingerprint():
        return {}
    return data


def _format_consolidation(index):
    """Render consolidate.py's understanding_index.json into a compact brief section."""
    if not index:
        return []
    lines = ["## Understanding index (from consolidate.py)", ""]
    chars = index.get("characters") or []
    if chars:
        lines.append("- Characters:")
        for c in chars:
            if isinstance(c, dict):
                lines.append(f"    - {c.get('name', '?')}: {str(c.get('description', '')).strip()}")
            else:
                lines.append(f"    - {c}")
    rels = index.get("relationships") or []
    if rels:
        lines.append("- Relationships:")
        for r in rels:
            if isinstance(r, dict):
                lines.append(f"    - {r.get('a', '?')} — {r.get('relation', '?')} — {r.get('b', '?')}")
            else:
                lines.append(f"    - {r}")
    plot = index.get("plot_points") or []
    if plot:
        lines.append("- Plot spine:")
        lines.extend(f"    {i + 1}. {p}" for i, p in enumerate(plot))
    ents = index.get("entities") or []
    if ents:
        lines.append(f"- Entities: {', '.join(str(e) for e in ents)}")
    lines.append("")
    return lines


# Span tolerance for the clean-ASR timing guard. MUST equal consolidate._ASR_SPAN_TOL
# (this file cannot import consolidate without breaking brief/narration byte-parity).
_ASR_SPAN_TOL = 0.05


def _clean_asr_fresh(out_path, source_path):
    # canonical: understand._fresh — inlined so video-script's byte-identical narration.py
    # copy (which cannot import a video-understanding module) stays in lockstep.
    try:
        return out_path.exists() and source_path.exists() and (
            out_path.stat().st_mtime >= source_path.stat().st_mtime)
    except OSError:
        return False


def _load_clean_asr(work_dir, asr_result):
    """Return consolidate.py's cleaned ASR segments, or None to fall back to raw asr_result.
    Gated on parse + non-empty + freshness + provenance(source_md5) + timing invariant
    (len== first, then per-segment spans within _ASR_SPAN_TOL)."""
    base = [s for s in (asr_result or []) if isinstance(s, dict)]
    if not base:
        return None
    work_dir = Path(work_dir)
    clean_path = work_dir / "asr_clean.json"
    src_path = work_dir / "asr_result.json"
    if not clean_path.exists() or not _clean_asr_fresh(clean_path, src_path):
        return None
    try:
        payload = json.loads(clean_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict):
        return None
    segments = payload.get("segments")
    if not isinstance(segments, list) or len(segments) != len(base):
        return None
    try:
        if payload.get("source_md5") != hashlib.md5(src_path.read_bytes()).hexdigest():
            return None
    except OSError:
        return None
    if payload.get("model") != _consolidation_model():
        return None
    if payload.get("prompt_md5") != _clean_asr_prompt_fingerprint():
        return None
    for orig, clean in zip(base, segments):
        if not isinstance(clean, dict):
            return None
        try:
            if abs(float(clean.get("start")) - float(orig.get("start"))) > _ASR_SPAN_TOL:
                return None
            if abs(float(clean.get("end")) - float(orig.get("end"))) > _ASR_SPAN_TOL:
                return None
        except (TypeError, ValueError):
            return None
    return segments


_MIMO_REJECTION_MARKERS = (
    "request was rejected", "considered high risk", "high risk",
    "content policy", "cannot process", "无法处理", "内容审核", "违规",
)


def _is_mimo_chunk_usable(content):
    text = str(content or "").strip()
    if not text:
        return False
    low = text.lower()
    return not any(marker in low for marker in _MIMO_REJECTION_MARKERS)


def _mimo_video_settings_fingerprint():
    return {
        "model": CONFIG.get("mimo_video_model") or CONFIG.get("mimo_model") or CONFIG.get("vlm_model"),
        "mimo_video_api_url": CONFIG.get("mimo_video_api_url"),
        "mimo_video_fps": CONFIG.get("mimo_video_fps", 2.0),
        "mimo_media_resolution": CONFIG.get("mimo_media_resolution", "default"),
        "mimo_video_chunk_max_seconds": CONFIG.get("mimo_video_chunk_max_seconds", 20.0),
        "mimo_video_chunk_min_seconds": CONFIG.get("mimo_video_chunk_min_seconds", 1.0),
        "mimo_video_base64_max_mb": CONFIG.get("mimo_video_base64_max_mb", 45.0),
        "mimo_video_prompt": CONFIG.get("mimo_video_prompt", ""),
        "mimo_disable_thinking": CONFIG.get("mimo_disable_thinking", True),
    }


def _mimo_chunk_cache_key(chunk):
    return (
        f"{chunk['chunk_id']}|{chunk['scene_id']}|"
        f"{float(chunk['start']):.3f}-{float(chunk['end']):.3f}"
    )


def _mimo_cached_chunks_fingerprint(done):
    return stable_hash(done)


def _mimo_overview_payload_fingerprint(overview):
    payload = dict(overview)
    payload.pop("overview_fingerprint", None)
    return stable_hash(payload)


def _mimo_video_chunks_for_brief(scenes):
    max_seconds = float(CONFIG.get("mimo_video_chunk_max_seconds", 20.0) or 20.0)
    min_seconds = float(CONFIG.get("mimo_video_chunk_min_seconds", 1.0) or 1.0)
    chunks = []
    for scene_index, scene in enumerate(scenes or []):
        if not isinstance(scene, dict):
            continue
        try:
            start = float(scene.get("start", 0.0))
            end = float(scene.get("end", start))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        scene_id = scene.get("scene_id", scene_index)
        cursor = start
        while cursor < end:
            chunk_end = min(end, cursor + max_seconds)
            if end - chunk_end < min_seconds and chunk_end < end:
                chunk_end = end
            if chunk_end > cursor:
                chunks.append({
                    "chunk_id": len(chunks),
                    "scene_id": scene_id,
                    "start": round(cursor, 3),
                    "end": round(chunk_end, 3),
                })
            cursor = chunk_end
    return chunks


def _mimo_overview_matches_current_inputs(overview, scenes_analysis, video_path=None):
    if not isinstance(overview, dict) or overview.get("input") != "scene_chunks":
        return False
    if overview.get("settings") != _mimo_video_settings_fingerprint():
        return False
    overview_fingerprint = overview.get("overview_fingerprint")
    if not overview_fingerprint or overview_fingerprint != _mimo_overview_payload_fingerprint(overview):
        return False
    if video_path is not None:
        try:
            if overview.get("source_video_fingerprint") != file_fingerprint(video_path):
                return False
        except OSError:
            return False
    chunks = overview.get("chunks")
    if not isinstance(chunks, list) or not all(
        isinstance(chunk, dict) and _is_mimo_chunk_usable(chunk.get("content"))
        for chunk in chunks
    ):
        return False
    chunks_fingerprint = overview.get("chunks_fingerprint")
    if not chunks_fingerprint or chunks_fingerprint != _mimo_cached_chunks_fingerprint(chunks):
        return False
    expected_chunks = _mimo_video_chunks_for_brief(scenes_analysis)
    if not expected_chunks or len(chunks) != len(expected_chunks):
        return False
    try:
        cached_keys = [_mimo_chunk_cache_key(chunk) for chunk in chunks]
        expected_keys = [_mimo_chunk_cache_key(chunk) for chunk in expected_chunks]
    except (KeyError, TypeError, ValueError):
        return False
    return cached_keys == expected_keys


def _load_mimo_overview_for_brief(work_dir, scenes_analysis, enabled=None, video_path=None):
    overview_enabled = CONFIG.get("mimo_video_overview", False) if enabled is None else enabled
    if not overview_enabled:
        return {}
    path = Path(work_dir) / "mimo_video_overview.json"
    if not path.exists():
        return {}
    try:
        overview = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return overview if _mimo_overview_matches_current_inputs(overview, scenes_analysis, video_path=video_path) else {}


def _load_optional_stage_status(work_dir, filename):
    """Load optional-stage status sidecars defensively."""
    path = Path(work_dir) / filename
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _status_message(message, limit=180):
    text = " ".join(str(message or "").split())
    return text[:limit]


def _optional_stage_warning(stage, status, message):
    line = f"- {stage}: {status}"
    msg = _status_message(message)
    return f"{line} — {msg}" if msg else line


def _format_optional_stage_warnings(work_dir, *, mimo_overview_enabled=None, mimo_overview=None, consolidation_index=None):
    """Surface fail-open optional-stage loss near the top of the brief."""
    work_dir = Path(work_dir)
    warnings = []

    overview_enabled = (
        bool(CONFIG.get("mimo_video_overview", False))
        if mimo_overview_enabled is None else bool(mimo_overview_enabled)
    )
    overview_status = _load_optional_stage_status(work_dir, "mimo_video_overview.status.json")
    if overview_status.get("enabled") and overview_status.get("status") in {"failed", "skipped_no_key"}:
        warnings.append(_optional_stage_warning(
            "mimo_video_overview", overview_status.get("status"), overview_status.get("message")
        ))
    elif overview_enabled and not mimo_overview:
        warnings.append(_optional_stage_warning(
            "mimo_video_overview", "missing_artifact",
            "enabled but no valid mimo_video_overview.json is available to this brief",
        ))

    consolidation_status = _load_optional_stage_status(work_dir, "consolidation.status.json")
    if consolidation_status.get("enabled"):
        if consolidation_status.get("status") == "failed":
            warnings.append(_optional_stage_warning(
                "consolidation", "failed", consolidation_status.get("message")
            ))
        elif consolidation_status.get("do_index") and not consolidation_index:
            warnings.append(_optional_stage_warning(
                "consolidation", "missing_index",
                "enabled but no valid understanding_index.json is available to this brief",
            ))

    if not warnings:
        return []
    return [
        "## Optional stage warnings",
        "",
        "These stages are fail-open; continue, but do not assume their missing context exists.",
        *warnings,
        "",
    ]


def _parse_target_seconds(value):
    """Parse a cut-mode target duration ("30m" / "600" / "1h5m" / "00:30:00") to seconds.

    Mirrors video-cut's parser closely enough to size the brief; returns None on anything
    unparseable so the brief simply falls back to the source duration.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    text = str(value).strip().lower()
    if not text:
        return None
    try:
        if ":" in text:
            parts = [float(p) for p in text.split(":")]
            if any(p < 0 for p in parts):
                return None
            if len(parts) == 2:
                seconds = parts[0] * 60 + parts[1]
            elif len(parts) == 3:
                seconds = parts[0] * 3600 + parts[1] * 60 + parts[2]
            else:
                return None
            return seconds if seconds > 0 else None
        factors = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}
        seconds = 0.0
        matched = False
        pos = 0
        for m in re.finditer(r"([0-9]+(?:\.[0-9]+)?)(ms|s|m|h)?", text):
            if m.start() != pos:
                break
            pos = m.end()
            matched = True
            seconds += float(m.group(1)) * factors[m.group(2) or "s"]
        if not matched or pos != len(text):
            return None
        return seconds if seconds > 0 else None
    except (ValueError, TypeError):
        return None


def _format_research_directive(work_dir, substrate):
    """A loud, actionable research-first directive — but ONLY when the substrate is too thin
    for real commentary (no dialogue/story spine) and no background_research.json exists yet.

    The narration's quality ceiling is how much story context the agent has: with only frame
    descriptions it can only narrate pixels, so research the title FIRST (see
    references/research-guide.md). Fires only for thin/empty substrate — NOT merely because a
    title was given — so a dialogue-rich titled run is never nagged.
    """
    if (Path(work_dir) / "background_research.json").exists():
        return []  # already researched; _format_background_research surfaces it
    if not (substrate and substrate.get("level") in ("thin", "empty")):
        return []  # rich enough to write from dialogue/spine; do not nag
    context = str(CONFIG.get("context_info") or "").strip()
    return [
        "## ⚑ Research the story FIRST (do this before writing narration)",
        "",
        "Reason: the understanding substrate is thin — no dialogue/story spine, only frame",
        "descriptions, so without research the narration can only describe pixels.",
        "1. Pull the title/keywords from `--context`, the filename, or the user's description"
        + (f" (context: {context})." if context else "."),
        "2. Use any available web-search/browser tool to look up synopsis, characters, and",
        "   relationships (see `references/research-guide.md`).",
        "3. Write `work_dir/background_research.json`, then re-read this brief and write narration",
        "   that names people and reads the picture through the plot.",
        "4. If no tool/network or nothing found: skip — keep beats sparse and strictly grounded in",
        "   the visible ASR/frame evidence rather than inventing drama.",
        "",
    ]


def _load_cut_output_spans_for_brief(work_dir, *, required=False):
    """Load fresh source→output spans for cut pass 2 brief evidence.

    Pass 2 narration is authored against edited_source.mp4's OUTPUT timeline, so
    ASR chunks and timeline_fusion must use the same OUTPUT clock. Before pass 2
    (no edited_source.mp4 yet), callers may fall back to source-time evidence; once
    pass 2 exists, missing/stale validated spans are a hard contract failure.
    """
    def fail(reason):
        if required:
            raise SystemExit(
                "cut pass2 brief requires fresh clip_plan_validated.json with explicit "
                f"finite source/output spans ({reason})"
            )
        return None

    work_dir = Path(work_dir)
    if not (work_dir / "edited_source.mp4").exists():
        return None
    validated_path = work_dir / "clip_plan_validated.json"
    if not validated_path.exists():
        return fail("missing clip_plan_validated.json")
    try:
        plan = json.loads(validated_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return fail("invalid clip_plan_validated.json")
    if not isinstance(plan, dict):
        return fail("clip_plan_validated.json is not an object")
    raw_path = work_dir / "clip_plan.json"
    if raw_path.exists():
        try:
            raw_plan = json.loads(raw_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return fail("invalid clip_plan.json")
        if plan.get("raw_plan_fingerprint") != stable_hash(raw_plan):
            return fail("stale clip_plan_validated.json")
    clips = plan.get("clips")
    if not isinstance(clips, list):
        return fail("clips is not a list")
    spans = []
    for clip in clips:
        if not isinstance(clip, dict):
            continue
        if not all(key in clip for key in ("source_start", "source_end", "output_start", "output_end")):
            return fail("clip missing source/output span fields")
        try:
            ss = float(clip["source_start"])
            se = float(clip["source_end"])
            os_ = float(clip["output_start"])
            oe = float(clip["output_end"])
        except (TypeError, ValueError):
            return fail("non-numeric clip span")
        if not all(math.isfinite(x) for x in (ss, se, os_, oe)):
            return fail("non-finite clip span")
        if se <= ss or oe <= os_:
            return fail("non-positive clip span")
        spans.append({"source_start": ss, "source_end": se, "output_start": os_, "output_end": oe})
    return spans or fail("no valid clips")


def _source_output_overlaps_for_brief(start, end, spans):
    overlaps = []
    for span in spans or []:
        source_start = max(float(start), span["source_start"])
        source_end = min(float(end), span["source_end"])
        if source_end <= source_start:
            continue
        output_start = span["output_start"] + (source_start - span["source_start"])
        output_end = span["output_start"] + (source_end - span["source_start"])
        overlaps.append({
            "source_start": source_start,
            "source_end": source_end,
            "output_start": output_start,
            "output_end": output_end,
        })
    return overlaps


def _remap_frame_facts_for_brief(frame_facts, overlap):
    if not isinstance(frame_facts, dict):
        return frame_facts
    out = {}
    for raw_ts, vals in frame_facts.items():
        try:
            ts = float(raw_ts)
        except (TypeError, ValueError):
            continue
        if not (overlap["source_start"] <= ts <= overlap["source_end"]):
            continue
        out_ts = overlap["output_start"] + (ts - overlap["source_start"])
        out[f"{out_ts:.3f}"] = vals
    return out


def _remap_scenes_to_output_for_brief(scenes, spans):
    if not spans:
        return scenes or []
    out = []
    for scene in scenes or []:
        if not isinstance(scene, dict):
            continue
        try:
            start = float(scene.get("start", 0))
            end = float(scene.get("end", start))
        except (TypeError, ValueError):
            continue
        overlaps = _source_output_overlaps_for_brief(start, end, spans)
        for part_idx, overlap in enumerate(overlaps):
            item = dict(scene)
            item["start"] = round(overlap["output_start"], 3)
            item["end"] = round(overlap["output_end"], 3)
            item["frame_facts"] = _remap_frame_facts_for_brief(scene.get("frame_facts"), overlap)
            if len(overlaps) > 1:
                item["scene_id"] = f"{scene.get('scene_id', '?')}.{part_idx}"
            out.append(item)
    out.sort(key=lambda x: (float(x.get("start", 0)), float(x.get("end", 0))))
    return out


def _remap_segments_to_output_for_brief(segments, spans):
    if not spans:
        return segments or []
    out = []
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        try:
            start = float(seg.get("start", 0))
            end = float(seg.get("end", start))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        for overlap in _source_output_overlaps_for_brief(start, end, spans):
            item = dict(seg)
            item["start"] = round(overlap["output_start"], 3)
            item["end"] = round(overlap["output_end"], 3)
            if "duration" in item:
                item["duration"] = round(item["end"] - item["start"], 3)
            out.append(item)
    out.sort(key=lambda x: (float(x.get("start", 0)), float(x.get("end", 0))))
    return out


def _remap_brief_evidence_to_output_timeline(work_dir, scenes_analysis, asr_result, silence_periods, *, required=False):
    spans = _load_cut_output_spans_for_brief(work_dir, required=required)
    if not spans:
        return scenes_analysis, asr_result, silence_periods
    return (
        _remap_scenes_to_output_for_brief(scenes_analysis, spans),
        _remap_segments_to_output_for_brief(asr_result, spans),
        _remap_segments_to_output_for_brief(silence_periods, spans),
    )


def _format_output_clip_list(work_dir):
    """List the kept clips on the OUTPUT timeline (cut-first/narrate-second pass 2), so the
    agent narrates against the real rendered cut instead of the source timeline."""
    path = Path(work_dir) / "clip_plan_validated.json"
    if not path.exists():
        return []
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    clips = (plan.get("clips") if isinstance(plan, dict) else plan) or []
    out = ["## Kept clips on the OUTPUT timeline", ""]
    for c in clips:
        if not isinstance(c, dict):
            continue
        try:
            os_, oe = float(c.get("output_start")), float(c.get("output_end"))
            ss, se = float(c.get("source_start")), float(c.get("source_end"))
        except (TypeError, ValueError):
            continue
        reason = str(c.get("reason", "")).strip()
        out.append(f"- output {os_:.1f}–{oe:.1f}s ← source {ss:.1f}–{se:.1f}s" + (f" — {reason}" if reason else ""))
    out.append("")
    return out if len(out) > 2 else []


def build_agent_brief(scenes_analysis, asr_result, silence_periods, video_duration, work_dir, style="纪录片", *, mimo_overview_enabled=None, mimo_overview_video_path=None):
    """Write a compact brief that tells the agent exactly how to author recap artifacts."""
    # account for the global narration atempo (CONFIG['narration_speed']) so a beat's text
    # is budgeted against the FINAL sped-up audio, not the raw TTS rate — otherwise windows
    # are over-sized and the bed shows long silent gaps between sentences.
    effective_rate = CONFIG["speech_rate"] * CONFIG["speech_safety_margin"] * float(CONFIG.get("narration_speed", 1.0) or 1.0)
    breath_sec = CONFIG.get("breath_ms", 250) / 1000
    target_pause_ms = CONFIG.get("breath_ms", 250)
    edit_mode = CONFIG.get("edit_mode", "full")
    target_duration = CONFIG.get("target_duration") or "(not set)"
    # Cut mode sizes narration to the OUTPUT (the kept clips), not the full source.
    # In pass 2, prefer the actual validated edited_source duration; target_duration is
    # only a planning goal and can differ after clip snapping or under/over-selection.
    output_seconds = video_duration
    if edit_mode == "cut":
        spans = _load_cut_output_spans_for_brief(work_dir, required=False)
        if spans:
            output_seconds = max(span["output_end"] for span in spans)
        else:
            target_seconds = _parse_target_seconds(CONFIG.get("target_duration"))
            if target_seconds:
                output_seconds = min(video_duration, target_seconds)
    # Block count from coverage, not beats/min: narrate ~coverage_target of the timeline in blocks of
    # ~block_seconds each, leaving the rest as original-audio blocks. Big blocks ⇒ far fewer beats
    # than the old per-sentence model (a 5min recap ⇒ ~20 blocks, not ~48 sentences).
    cov_target = CONFIG.get("narration_coverage_target", 0.7)
    block_seconds = CONFIG.get("narration_block_seconds", 9.0)
    target_count = max(1, round(output_seconds * cov_target / block_seconds))

    has_story_context = (Path(work_dir) / "background_research.json").exists()
    substrate = assess_understanding_substrate(scenes_analysis, asr_result, has_story_context=has_story_context)
    thin_substrate = substrate.get("level") in ("thin", "empty")
    beat_count_phrase = f"at most ~{target_count}" if thin_substrate else f"roughly {target_count}"
    output_label = f"{output_seconds / 60:.0f}min" if output_seconds >= 60 else f"{output_seconds:.0f}s"
    source_label = f"{video_duration / 60:.0f}min" if video_duration >= 60 else f"{video_duration:.0f}s"

    lines = [
        "# Agent Narration Brief",
        "",
        "Write the required JSON artifact(s) manually from the analysis files in this work directory.",
        "The CLI will not generate final narration text; it will only validate timing, run TTS, and assemble the video.",
        "",
        f"- Style: {style}",
        f"- Edit mode: {edit_mode}",
        f"- Source video duration: {video_duration:.1f}s",
        f"- Target duration (cut mode): {target_duration}",
        f"- Effective speech budget: {effective_rate:.2f} Chinese chars/sec after {breath_sec:.2f}s pause allowance",
    ]
    if thin_substrate:
        lines.append(
            f"- Narration density: substrate is {substrate['level']} — do NOT chase a beat count. "
            f"Write fewer, grounded blocks; skipping a stretch beats narrating pixels."
        )
    else:
        lines.append(
            "- Narration in BLOCKS, ~7:3. Write narration as BLOCKS — each beat is a few sentences (one "
            "continuous thought) that gets synthesized as ONE fluent TTS utterance. Aim for narration to "
            "cover roughly 70% of the timeline and leave ~30% as deliberate ORIGINAL-AUDIO blocks: "
            "multi-second gaps with NO narration where a strong original moment (a key line, an action beat, "
            "the music) plays at full volume."
        )
        lines.append(
            "- A block alternates with an original block: speak a block, then step back for a few seconds and "
            "let the scene play, then the next block. What is FORBIDDEN is the per-sentence stutter — one "
            "short sentence, a gap, one short sentence, a gap — and wall-to-wall talk that never lets the "
            "original breathe. Size each block's window to its own text (chars / the speech budget above)."
        )
    if edit_mode == "cut":
        lines.append(
            f"- Aim for {beat_count_phrase} narration BLOCKS across the ~{output_label} CUT OUTPUT "
            f"(sized to the kept clips, NOT the {source_label} source), ~7:3 over the original audio"
        )
    else:
        lines.append(
            f"- Aim for {beat_count_phrase} narration BLOCKS across the timeline, ~7:3 narrated:original audio"
        )
    lines.extend([
        f"- Default pause between beats: {target_pause_ms}ms",
        f"- Context: {CONFIG.get('context_info') or '(none)'}",
        "",
    ])

    consolidation_index = _load_consolidation(work_dir, scenes_analysis)
    mimo_overview = _load_mimo_overview_for_brief(
        work_dir, scenes_analysis, enabled=mimo_overview_enabled, video_path=mimo_overview_video_path,
    )
    lines.extend(_format_optional_stage_warnings(
        work_dir,
        mimo_overview_enabled=mimo_overview_enabled,
        mimo_overview=mimo_overview,
        consolidation_index=consolidation_index,
    ))
    lines.extend(_format_substrate_warning(substrate))
    lines.extend(_format_research_directive(work_dir, substrate))
    lines.extend(_format_background_research(_load_background_research(work_dir)))
    lines.extend(_format_consolidation(consolidation_index))

    asr_for_chunks = _load_clean_asr(work_dir, asr_result) or asr_result
    chunk_scenes, chunk_asr = scenes_analysis, asr_for_chunks
    fusion_scenes, fusion_asr, fusion_silence = scenes_analysis, asr_result, silence_periods
    if edit_mode == "cut" and (Path(work_dir) / "edited_source.mp4").exists():
        chunk_scenes, chunk_asr, _ = _remap_brief_evidence_to_output_timeline(
            work_dir, scenes_analysis, asr_for_chunks, [], required=True)
        fusion_scenes, fusion_asr, fusion_silence = _remap_brief_evidence_to_output_timeline(
            work_dir, scenes_analysis, asr_result, silence_periods, required=True)
    asr_chunks = _chunk_asr_for_writing(chunk_asr, chunk_scenes)
    timeline_fusion = _build_timeline_fusion(fusion_scenes, fusion_asr, fusion_silence)
    _write_json_artifact(work_dir, "asr_writing_chunks.json", asr_chunks)
    _write_json_artifact(work_dir, "timeline_fusion.json", timeline_fusion)
    lines.extend(_format_asr_chunks_for_brief(asr_chunks))
    lines.extend(_format_timeline_fusion_for_brief(timeline_fusion))

    overview_text = (mimo_overview.get("content") or "").strip()
    if overview_text:
        lines.extend([
            "## MiMo scene-chunk video overview",
            "",
            overview_text[:2000],
            "",
        ])

    if edit_mode == "cut":
        cut_target_example = target_duration if target_duration != "(not set)" else "30m"
        if not (Path(work_dir) / "edited_source.mp4").exists():
            # PASS 1 of 2 (cut-first): pick the footage. Narration comes AFTER the cut is
            # rendered, so it can be written against the real OUTPUT timeline — no source->output
            # mapping, no silent drop/clamp, no desync.
            lines.extend([
                "## Cut mode — step 1 of 2: write `clip_plan.json` ONLY",
                "",
                f"Goal: a ~{output_label} recap cut from a {source_label} source. First choose the footage; the CLI",
                "then renders the cut and asks you to narrate against that real output. Do NOT write narration.json yet.",
                "How to choose clips (use the Scene timing guide + ASR + index below):",
                "- Build ONE complete arc: a hook, the key turns of the plot, and a cliffhanger/payoff at the end — not a flat highlights reel.",
                "- Keep clips that carry causality, a reveal, a decision, or a strong emotional beat; cut establishing/transition/repeated/static shots.",
                "- SKIP non-story footage: 片头/片尾 credits, 演职员表, 广告/赞助, 台标/水印 stretches, and any scene the analysis marks rejected/无法描述 (often a watermark) — they look bad on screen and add nothing.",
                "- Prefer scenes that have a real visual description in the guide; favor faces, action and dialogue over scenery.",
                "- Clip length ~3–15s; vary the pace; order clips in story order so the cut reads as a coherent story.",
                "- End a clip on a COMPLETE spoken line — set the clip end at or just after an ASR line-end (or inside the quiet window that follows it), never mid-sentence, so the original dialogue is never chopped off. Use the ASR [start–end] times + Quiet windows below as safe cut points; the CLI also snaps clip ends to the nearest line-end as a safety net.",
                "",
                "### clip_plan.json shape (original source timestamps)",
                "",
                "```json",
                "{",
                f"  \"target_duration\": \"{cut_target_example}\",",
                "  \"clips\": [",
                "    {\"start\": 12.0, \"end\": 38.0, \"reason\": \"关键冲突开端\"}",
                "  ]",
                "}",
                "```",
            ])
        else:
            # PASS 2 of 2: the cut is rendered (edited_source.mp4); narrate in OUTPUT time.
            lines.extend(_format_output_clip_list(work_dir))
            lines.extend([
                "## Cut mode — step 2 of 2: write `narration.json` in OUTPUT time",
                "",
                f"The cut is rendered as `edited_source.mp4` (~{output_label}). Write narration timed to THAT output",
                "timeline (0 .. total), NOT the original source — your timestamps play exactly where you put them, with",
                "no mapping and no dropping. Use the kept-clip OUTPUT ranges above to know what is on screen when, tell",
                "one continuous arc across the cut, and aim for the density guide in the header.",
                "",
                "### narration.json shape (OUTPUT timestamps, 0..total)",
                "",
                "Each beat is a BLOCK (a few sentences spoken as one fluent utterance); size end-start to the",
                "block's text, then leave a few-second gap before the next block for an original-audio moment.",
                "```json",
                "[",
                "  {\"start\": 2.0, \"end\": 13.0, \"narration\": \"范闲表面是个闲散少爷，背地里却握着监察院的暗线。这一次，他要赌上身家去查母亲的死。\", \"pause_after_ms\": 250, \"overlaps_speech\": true, \"emotion\": \"紧张\"}",
                "]",
                "```",
            ])
    else:
        lines.extend([
            "## Required JSON shape",
            "",
            "Each beat is a BLOCK (a few sentences spoken as one fluent utterance); size end-start to the",
            "block's text, then leave a few-second gap before the next block for an original-audio moment.",
            "```json",
            "[",
            "  {\"start\": 5.0, \"end\": 16.0, \"narration\": \"范闲表面是个闲散少爷，背地里却握着监察院的暗线。这一次，他要赌上身家去查母亲的死。\", \"pause_after_ms\": 250, \"overlaps_speech\": true, \"emotion\": \"平静\"}",
            "]",
            "```",
        ])

    lines.extend([
        "",
        "## Writing rules (block recap style)",
        "",
        "1. Narrate in BLOCKS, ~7:3. Tell the story as a sequence of narration BLOCKS over the original audio; in cut mode that means across the kept clips, in output order. Leave ~30% of the timeline as deliberate original-audio blocks (no narration) where a strong moment plays at full volume.",
        "2. Follow the density line near the top of this brief: it already accounts for thin substrate and cut output. Narration should cover most (~70%) of the timeline, but never pad with filler just to hit a number; a meaningful block beats a filler block.",
        "3. Default `overlaps_speech` to true. The CLI auto-marks a beat as non-overlapping only when it actually lands inside a real silent window.",
        "4. Make each beat a BLOCK of 2-4 COMPLETE sentences (one continuous thought), NOT a lone short fragment — the whole block is synthesized in ONE TTS call, so write it to read aloud naturally end-to-end; that connected prosody is what makes the voice flow instead of sounding choppy. Size its end-start to fit the block's text at the speech budget above.",
        "5. Do not describe what the viewer can already see; explain intent, stakes, subtext, relationships, and story logic.",
        "6. Keep timing visually local: anchor each block's start/end to the stretch of footage it covers; don't let a block run far past what is on screen.",
        "7. In cut mode, select clips for plot causality, key dialogue, reveals, and emotional turns; avoid filler and repeated shots.",
        "8. Give every block an `emotion` that fits its whole arc; keep it STEADY across the block (it is one utterance) and shift only at a real emotional turn between blocks. Use a calm base (平静/深沉/严肃) for most of a section and save 震惊/悲伤/紧张 for the actual turns.",
        "9. Between blocks, leave a gap of a few seconds with NO narration so the original audio plays alone — pick those spots at genuinely strong original moments, not arbitrarily. BRIDGE them: the block right BEFORE a gap must lead INTO that original moment (end on a line that makes the viewer want to hear what comes next), and the block right AFTER it must pick UP / react to what the original just said or showed — the narration and the original it brackets are ONE continuous beat, not 各说各的.",
        "10. 不要在解说文本里使用破折号（——、—）：破折号烧进字幕里很突兀，该停顿就用逗号，该断句就用句号；同理 `original_subtitles.json` 里也不要用破折号。",
        f"11. After writing, run: `python3 {shlex.quote(str(Path(__file__).resolve().parents[2] / 'video-recap' / 'scripts' / 'recap.py'))} <video> --work-dir <work_dir>`.",
        "",
        "## 原声留白字幕 `original_subtitles.json`（校对原声台词）",
        "",
        "你在解说块之间留出的原声留白，会把【原声台词】烧成字幕（和解说字幕用 「」 区分开）。请额外写一个 `original_subtitles.json`，把每段留白里真正听得到的原声台词，按 OUTPUT 时间轴写成 `[{\"start\": 秒, \"end\": 秒, \"text\": \"台词\"}]`：",
        "- 只写留白里【实际出声】的台词；被解说盖过、或已经被剪掉的句子，不要写进来（这正是自动 ASR 兜底会出错的地方）。",
        "- 订正 ASR 的错字和人名（例：叶青眉 → 叶轻眉），删掉口胡和语气词。",
        "- 每条短到一行（≤ 约 20 字），`start`/`end` 对齐它在留白里出声的时间；拿不准就贴着所在留白的区间写。",
        "- 没有清晰原声的留白可以不写；整个文件也可省略——省略时系统会用 ASR 粗略兜底（可能偏多偏乱）。",
        "",
        "## Per-block emotion (`emotion` field → MiMo TTS instruct)",
        "",
        "Each block's `emotion` is a short Chinese tone tag MiMo-v2.5-tts follows for the whole utterance. Pick 1-2 that fit the block:",
        "- 基础情绪: 开心 悲伤 愤怒 恐惧 惊讶 兴奋 委屈 平静 冷漠",
        "- 复合情绪: 怅然 欣慰 无奈 愧疚 释然 嫉妒 厌倦 忐忑 动情",
        "- 整体语调: 温柔 高冷 活泼 严肃 慵懒 俏皮 深沉 干练 凌厉",
        "You may combine, e.g. \"紧张 深沉\" or \"无奈\". Default to 平静 only for neutral setup; a recap mostly lives in 紧张/深沉/惊讶/悲伤/动情.",
        "",
        "## Recap craft (what separates a real recap from captions)",
        "",
        "- Hook: the first 1-2 beats must create a question or stakes, not set the scene. Make the viewer need the next line.",
        "- Through-line: pick ONE spine (a goal, a relationship, a mystery) and let every beat advance it; don't reset each scene.",
        "- Escalation: raise the stakes or reveal new information as you go; later beats should land harder than earlier ones.",
        "- Curiosity gaps: tease consequences before they happen (\"他还不知道，这一步会要命\") and pay them off later.",
        "- Payoff: the final 1-2 beats must resolve or twist the spine, leaving an aftertaste — never trail off on a generic line.",
        "- Information, not narration of pixels: every beat should add something the picture alone can't tell (who, why, what's at stake).",
        "- Voice: concrete nouns and verbs, specific names; cut adjectives and vague grandeur (\"危机四伏\"/\"震撼人心\" are filler).",
        "- Use the real names, relationships and stakes from the Story context / index above — never generic labels like 男子/白衣女子.",
        "- Show motive and consequence, not actions: say WHY a character does it and what it costs, not what they are doing on screen.",
        "- 衔接 hand-off: a narration block and the original-audio gap beside it are ONE beat — tee up the original before it plays, then have the next block answer what it showed; never let a block end self-contained and the original come in cold.",
        "",
        "看图说话 (bad) vs recap (good) — same shot:",
        "- ✗ \"一个蒙眼的男人抱着一个篮子走在雨里。\"  (just describes the frame)",
        "- ✓ \"杀手五竹本可以一走了之，却为了一个不是自己孩子的婴儿，把整座京都的追兵引向自己。\"  (who, why, stakes)",
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
            f"- Quiet windows: {quiet_text}",
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
