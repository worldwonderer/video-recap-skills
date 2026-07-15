"""Normalize, split, budget, and de-duplicate narration text."""

import importlib.util

import copy


import re

from pathlib import Path

from lib import CONFIG

from lib import log

try:
    from deslop_qc import analyze_deslop_qc
except ModuleNotFoundError:
    _deslop_qc_path = Path(__file__).with_name("deslop_qc.py")
    _deslop_qc_spec = importlib.util.spec_from_file_location(
        "deslop_qc", _deslop_qc_path
    )
    if _deslop_qc_spec is None or _deslop_qc_spec.loader is None:
        raise
    _deslop_qc_module = importlib.util.module_from_spec(_deslop_qc_spec)
    _deslop_qc_spec.loader.exec_module(_deslop_qc_module)
    analyze_deslop_qc = _deslop_qc_module.analyze_deslop_qc


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
    return len(
        re.sub(
            r'[，。！？、；：…“”‘’《》〈〉\s"\'「」『』（）()【】\[\]—～·,.!?;:\\-]',
            "",
            text or "",
        )
    )


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
        piece = rest[: cut + 1].strip()
        if not piece:
            piece = rest[:char_max].strip()
            cut = char_max - 1
        result.append(piece)
        rest = rest[cut + 1 :].strip()
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
            pieces.extend(
                _split_text_by_sentence_windows(
                    sentence, min_chars=min_chars, max_chars=max_chars
                )
            )
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
        timed.append(
            {
                "start": round(cursor, 2),
                "end": round(piece_end, 2),
                "text": piece,
                "char_count": _writing_text_units(piece),
            }
        )
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


def _chunk_asr_for_writing(
    segments, scenes_analysis=None, min_chars=None, max_chars=None
):
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
        chunks.append(
            {
                "chunk_id": len(chunks),
                "start": round(float(current[0]["start"]), 2),
                "end": round(float(current[-1]["end"]), 2),
                "scene_ids": sorted(
                    current_scene_ids, key=lambda sid: (isinstance(sid, str), sid)
                ),
                "char_count": current_units,
                "text": " ".join(piece["text"] for piece in current).strip(),
                "segments": current,
            }
        )
        current = []
        current_units = 0
        current_scene_ids = set()

    for piece in pieces:
        units = max(
            1, int(piece.get("char_count", _writing_text_units(piece.get("text", ""))))
        )
        piece_scene_ids = set(
            _scene_ids_for_range(scenes_analysis, piece["start"], piece["end"])
        )
        crosses_scene = (
            current
            and current_scene_ids
            and piece_scene_ids
            and not (current_scene_ids & piece_scene_ids)
        )
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
    idx = max(text[:cutoff].rfind(sep) for sep in ["。", "！", "？", "!", "?"])
    if idx > 0:
        return text[: idx + 1]
    idx = max(text[:cutoff].rfind(sep) for sep in ["，", "、", "；", ","])
    if idx > 3:
        return text[:idx] + "。"
    return ""


def _char_bigrams(text):
    return {text[i : i + 2] for i in range(len(text) - 1) if text[i : i + 2].strip()}


def _post_dedup_narration(narration):
    """去除相邻相似解说段（bigram 重叠 >60% 则合并）。"""
    if len(narration) < 2:
        return narration
    result = [narration[0]]
    for seg in narration[1:]:
        prev = result[-1]
        if (
            not prev.get("narration", "").strip()
            or not seg.get("narration", "").strip()
        ):
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
            # Validation is also the handoff boundary for renderer metadata.  When two
            # near-identical narration beats are merged, retain overlays authored on either
            # beat instead of silently discarding the second beat's visual instructions.
            merged_overlays = []
            for candidate in (prev, seg):
                raw_overlays = candidate.get("visual_overlays")
                if isinstance(raw_overlays, list):
                    merged_overlays.extend(copy.deepcopy(raw_overlays))
            if len(seg["narration"]) > len(prev["narration"]):
                prev["narration"] = seg["narration"]
            prev["end"] = seg["end"]
            prev["pause_after_ms"] = seg.get(
                "pause_after_ms", prev.get("pause_after_ms", 600)
            )
            if merged_overlays:
                prev["visual_overlays"] = merged_overlays
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
    effective_rate = (
        CONFIG["speech_rate"]
        * CONFIG["speech_safety_margin"]
        * float(CONFIG.get("narration_speed", 1.0) or 1.0)
    )
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
    for optional_key in (
        "source_start",
        "source_end",
        "source_clip_id",
        "source_entry_policy",
        "source_entry_reason",
    ):
        if optional_key in seg:
            item[optional_key] = seg[optional_key]
    # carry the per-beat emotion/tone tag (MiMo TTS instruct) through lint untouched
    emotion = seg.get("emotion")
    if isinstance(emotion, str) and emotion.strip():
        item["emotion"] = emotion.strip()
    # Preserve renderer-owned metadata across the full-mode validation rewrite.  The recap
    # orchestrator filters supported overlay kinds later; validation must not erase authored
    # overlays merely because it normalizes narration timing/text fields.
    visual_overlays = seg.get("visual_overlays")
    if isinstance(visual_overlays, list):
        item["visual_overlays"] = copy.deepcopy(visual_overlays)
    # Authored timing is a delivery contract. Scene boundaries are approximate
    # visual-analysis buckets, while source sentence anchors are measured audio
    # handoff points. Lint may warn when a block crosses a scene, but validation
    # must never silently clamp start/end and invalidate an audio-safe handoff.
    return item


def _clean_narration_punctuation(text):
    text = re.sub(r"\s+", " ", text or "").strip()
    text = re.sub(r'[，：、；,]["\']?[。！？]', "。", text)
    text = re.sub(r'["\']。$', "。", text)
    return text


def _overlap_seconds(start, end, other_start, other_end):
    return max(
        0.0, min(float(end), float(other_end)) - max(float(start), float(other_start))
    )
