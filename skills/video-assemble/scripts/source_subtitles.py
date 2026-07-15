"""Original-dialogue subtitle loading, source mapping, and gap placement."""

import json
import re
from pathlib import Path

from artifacts import _load_work_json
from audio_mix import _seg_place_window
from lib import CONFIG
from media import _load_cut_timeline_plan
from assemble_constants import (
    _AUTO_ORIGINAL_READ_CPS,
    _MAX_ORIGINAL_READ_CPS,
    _MIN_ASR_CLIP_OVERLAP,
    _MIN_GAP_TO_SUBTITLE,
    _MIN_READABLE_SECONDS,
    _SUBTITLE_CLOSING_QUOTES,
)
from subtitle_core import (
    _bracketed_original_chunks,
    _subtitle_entries,
)

def _has_user_subtitles(work_dir):
    """True when the user dropped a bring-your-own original-subtitle file into work_dir."""
    return work_dir is not None and any(
        (Path(work_dir) / name).exists()
        for name in ("user_subtitles.json", "user_subtitles.srt", "user_subtitles.ass")
    )


def _source_subtitle_mask_policy(work_dir=None):
    """Explicit source-subtitle mask policy and trigger facts for visual QC/cache keys.

    Older builds treated ``MASK_SOURCE_SUBTITLES=True`` as an ambient default black
    band. The visual contract now requires an explicit policy, so a bare truthy
    legacy flag is represented as ``legacy_implicit`` and blocks the visual gate
    instead of silently masking picture information.
    """
    burn = bool(CONFIG.get("burn_subtitles", False))
    raw_policy = str(CONFIG.get("source_subtitle_mask_policy", "") or "").strip().lower()
    legacy_flag = bool(CONFIG.get("mask_source_subtitles", False))
    allowed = {"off", "opt_in", "safe", "forced"}
    declared = bool(CONFIG.get("source_subtitle_mask_policy_declared", False)) or raw_policy in {"opt_in", "safe", "forced"}
    implicit = False
    if legacy_flag and not declared:
        raw_policy = "legacy_implicit"
        implicit = True
    elif not raw_policy:
        raw_policy = "off"
    elif raw_policy not in allowed:
        implicit = True
    user_subtitles = _has_user_subtitles(work_dir)
    active = False
    trigger = "policy_off"
    reason = "source subtitle masking disabled by explicit policy"
    if raw_policy == "off":
        active = False
    elif raw_policy in {"opt_in", "forced"}:
        active = burn and legacy_flag
        trigger = "burn_subtitles_and_legacy_mask_flag"
        reason = "explicit policy permits masking only with burned recap subtitles"
    elif raw_policy == "safe":
        active = burn and (legacy_flag or user_subtitles)
        trigger = "safe_policy_with_burned_subtitles"
        reason = "safe policy masks only when recap subtitles are burned and an original-subtitle source is declared"
    else:
        active = False
        trigger = "implicit_or_invalid_policy"
        reason = "mask_source_subtitles requires explicit SOURCE_SUBTITLE_MASK_POLICY"
    if not burn and active:
        active = False
        trigger = "burn_subtitles_disabled"
        reason = "mask-only black band is forbidden without burned recap subtitles"
    return {
        "policy": raw_policy,
        "declared": bool(declared and raw_policy in allowed),
        "active": bool(active),
        "scope": (
            "measured_source_subtitle_band"
            if active and 0 <= int(CONFIG.get("subtitle_y_top", -1)) < int(CONFIG.get("subtitle_y_bot", -1))
            else ("bottom_source_subtitle_band" if active else "none")
        ),
        "trigger": trigger,
        "reason": reason,
        "burn_subtitles": burn,
        "legacy_mask_flag": legacy_flag,
        "user_subtitles_present": user_subtitles,
        "blocking": bool(implicit),
    }


def _load_original_asr(work_dir):
    """The original speech transcription (asr_result.json), SOURCE-time, cleaned to
    {start,end,text} with text and a positive span. [] when absent/unparseable."""
    segs = []
    for s in _load_work_json(work_dir, "asr_result.json") or []:
        if not isinstance(s, dict):
            continue
        try:
            start, end = float(s["start"]), float(s["end"])
        except (KeyError, TypeError, ValueError):
            continue
        text = str(s.get("text", "")).strip()
        if text and end > start:
            segs.append({"start": start, "end": end, "text": text})
    return segs


def _load_agent_original_subtitles(work_dir):
    """Agent-calibrated original-dialogue subtitles (original_subtitles.json): OUTPUT-time
    [{start,end,text}] the writer authors alongside narration.json — the corrected, gap-aligned
    transcript of what is ACTUALLY said in each original-audio gap (ASR errors/names fixed).
    None when absent/invalid (then assemble falls back to a conservative auto-ASR mapping)."""
    data = _load_work_json(work_dir, "original_subtitles.json")
    if not isinstance(data, list):
        return None
    out = []
    for s in data:
        if not isinstance(s, dict):
            continue
        try:
            start, end = float(s["start"]), float(s["end"])
        except (KeyError, TypeError, ValueError):
            continue
        text = str(s.get("text", "")).strip()
        if text and end > start:
            out.append({"start": start, "end": end, "text": text})
    return out or None


def _clean_subtitle_segments(raw):
    """Coerce an iterable of {start,end,text} dicts to validated, positive-span segments."""
    out = []
    for s in raw or []:
        if not isinstance(s, dict):
            continue
        try:
            start, end = float(s["start"]), float(s["end"])
        except (KeyError, TypeError, ValueError):
            continue
        text = str(s.get("text", "")).strip()
        if text and end > start:
            out.append({"start": start, "end": end, "text": text})
    return out


def _parse_srt_timestamp(value):
    """Parse an SRT 'HH:MM:SS,mmm' (or ASS 'H:MM:SS.cc') timestamp into seconds, or None."""
    m = re.match(r"\s*(\d+):(\d{1,2}):(\d{1,2})[.,](\d{1,3})\s*$", str(value))
    if not m:
        return None
    h, mm, ss, frac = m.groups()
    return int(h) * 3600 + int(mm) * 60 + int(ss) + int(frac) / (10 ** len(frac))


def _parse_srt_text(text):
    """Minimal SRT parser → [{start,end,text}]. Tolerant of blank lines / missing indices."""
    segs = []
    for block in re.split(r"\n\s*\n", str(text).replace("\r\n", "\n").replace("\r", "\n")):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        if lines[0].strip().isdigit():
            lines = lines[1:]
        if not lines or "-->" not in lines[0]:
            continue
        parts = lines[0].split("-->")
        if len(parts) != 2:
            continue
        start, end = _parse_srt_timestamp(parts[0]), _parse_srt_timestamp(parts[1])
        body = " ".join(lines[1:]).strip()
        if start is not None and end is not None and end > start and body:
            segs.append({"start": start, "end": end, "text": body})
    return segs


def _parse_ass_text(text):
    """Minimal ASS Dialogue parser → [{start,end,text}] (Start, End are fields 2 and 3)."""
    segs = []
    for line in str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not line.startswith("Dialogue:"):
            continue
        fields = line[len("Dialogue:"):].split(",", 9)
        if len(fields) < 10:
            continue
        start, end = _parse_srt_timestamp(fields[1]), _parse_srt_timestamp(fields[2])
        body = re.sub(r"\{[^}]*\}", "", fields[9]).replace("\\N", " ").replace("\\n", " ").strip()
        if start is not None and end is not None and end > start and body:
            segs.append({"start": start, "end": end, "text": body})
    return segs


def _load_user_original_subtitles(work_dir):
    """User-supplied original-dialogue subtitles, the highest-priority source (above the agent file).

    Accepts (first existing wins):
      - user_subtitles.json: a bare list [{start,end,text}] (treated as OUTPUT-time, used verbatim),
        OR a wrapper {"timeline":"source"|"output", "lines":[...]} — "source" is remapped to OUTPUT
        via the cut clip spans, "output" (default) is used directly.
      - user_subtitles.srt / user_subtitles.ass: parsed minimally and defaulted to SOURCE-time,
        so they are remapped to OUTPUT via the cut clip spans.
    Returns OUTPUT-time [{start,end,text}], or None when absent/malformed (caller falls back)."""
    work = Path(work_dir)
    json_path = work / "user_subtitles.json"
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None
        if isinstance(data, list):
            segs, timeline = _clean_subtitle_segments(data), "output"
        elif isinstance(data, dict):
            segs = _clean_subtitle_segments(data.get("lines"))
            timeline = str(data.get("timeline", "output")).lower()
        else:
            return None
        if not segs:
            return None
        if timeline == "source":
            segs = _map_asr_to_output(segs, _output_clip_spans(work))
        return segs or None

    for name in ("user_subtitles.srt", "user_subtitles.ass"):
        path = work / name
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        parser = _parse_ass_text if name.endswith(".ass") else _parse_srt_text
        segs = _clean_subtitle_segments(parser(text))
        if not segs:
            return None
        # .srt/.ass default to SOURCE-time → remap onto the output timeline (identity in full mode).
        segs = _map_asr_to_output(segs, _output_clip_spans(work))
        return segs or None

    return None


def _output_clip_spans(work_dir):
    """Cut-mode source→output clip spans using the same freshness logic as video clips."""
    try:
        plan = _load_cut_timeline_plan(work_dir)
    except (OSError, ValueError, json.JSONDecodeError):
        plan = None
    if not plan:
        return None
    entries = plan.get("clips", plan) if isinstance(plan, dict) else plan
    if not isinstance(entries, list):
        return None
    spans, cursor = [], 0.0
    for c in entries:
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
            out_s, out_e = float(out_s), float(out_e)
            cursor = max(cursor, out_e)
        spans.append({"source_start": ss, "source_end": se, "output_start": out_s, "output_end": out_e})
    return spans or None


def _map_asr_to_output(asr_segs, clip_spans):
    """Map SOURCE-time ASR segments onto the OUTPUT timeline. Full mode (clip_spans None) is
    identity; cut mode intersects each ASR span with each kept clip (a straddling line yields one
    fragment per clip; lines in cut-away footage are dropped)."""
    if clip_spans is None:
        return [dict(s) for s in asr_segs]
    out = []
    for seg in asr_segs:
        for c in clip_spans:
            ov_s, ov_e = max(seg["start"], c["source_start"]), min(seg["end"], c["source_end"])
            if ov_e - ov_s <= _MIN_ASR_CLIP_OVERLAP:
                continue
            out.append({
                "start": c["output_start"] + (ov_s - c["source_start"]),
                "end": c["output_start"] + (ov_e - c["source_start"]),
                "text": seg["text"],
            })
    return out


def _narration_gap_windows(tts_segments, video_duration, min_gap=_MIN_GAP_TO_SUBTITLE):
    """OUTPUT-timeline stretches with NO narration (the original-audio blocks): the complement of
    the merged narration placement windows within [0, video_duration], keeping gaps >= min_gap."""
    placed = sorted(
        (_seg_place_window(s) for s in tts_segments if isinstance(s, dict)), key=lambda w: w[0])
    merged = []
    for s, e in placed:
        if e - s <= 0:
            continue
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    gaps, cursor = [], 0.0
    for s, e in merged:
        if s - cursor >= min_gap:
            gaps.append((cursor, s))
        cursor = max(cursor, e)
    if video_duration - cursor >= min_gap:
        gaps.append((cursor, float(video_duration)))
    return gaps


def _original_gap_subtitle_entries(tts_segments, work_dir, video_duration):
    """Subtitle entries for the ORIGINAL dialogue during the original-audio blocks (narration
    gaps), so the band is not blank while the original speaks. Off unless we are burning and
    subtitle_original_in_gaps is set; no-op when there is no ASR. Cut mode remaps ASR to output."""
    # Fill the gaps when either (a) we are masking the source's own burned-in subs (so the band is
    # blank without us), or (b) the user supplied their own subtitle file — a clear signal they want
    # the original dialogue shown, e.g. a clean/foreign source with mask OFF (no burned subs to
    # double). Without a user file we keep the mask requirement so we don't double the source's own
    # visible subs. subtitle_original_in_gaps is the explicit override either way.
    mask_covers_gaps = _source_subtitle_mask_covers_gaps(work_dir)
    if not (CONFIG.get("burn_subtitles", False)
            and CONFIG.get("subtitle_original_in_gaps", True)
            and (mask_covers_gaps or _has_user_subtitles(work_dir))):
        return []
    gaps = _narration_gap_windows(tts_segments, video_duration)
    if not gaps:
        return []

    # Source ladder (highest priority first): user-supplied file → agent-calibrated transcript →
    # conservative auto-ASR mapping. The user file and the agent file are time-precise (their spans
    # are the real on-screen windows), so they take the interval-clip "precise" path; raw ASR is
    # coarse and stays on the midpoint+over-render-guard fallback path.
    user = _load_user_original_subtitles(work_dir)
    if user is not None:
        candidates, precise = user, True
    else:
        agent = _load_agent_original_subtitles(work_dir)
        if agent is not None:
            candidates, precise = agent, True
        else:
            asr = _load_original_asr(work_dir)
            if not asr:
                return []
            candidates, precise = _map_asr_to_output(asr, _output_clip_spans(work_dir)), False

    max_chars = int(CONFIG.get("subtitle_max_chars", 20))
    if precise:
        return _precise_gap_entries(candidates, gaps, max_chars)
    return _fallback_gap_entries(candidates, gaps, max_chars)


def _precise_gap_entries(candidates, gaps, max_chars):
    """Precise path for time-accurate sources (user / agent-calibrated): interval-CLIP each line
    across the gap boundaries it overlaps, emitting one sub-entry per overlapped gap (clipped to
    that gap). A line straddling two gaps is split, not snapped to one or dropped; only sub-fragments
    shorter than _MIN_READABLE_SECONDS are dropped. No over-render guard (the source is trusted)."""
    entries = []
    for seg in candidates:
        text = str(seg["text"]).strip()
        if not text:
            continue
        seg_start, seg_end = float(seg["start"]), float(seg["end"])
        seg_dur = seg_end - seg_start
        overlaps = []
        for gs, ge in gaps:
            cs, ce = max(seg_start, gs), min(seg_end, ge)
            if ce - cs >= _MIN_READABLE_SECONDS:
                overlaps.append((cs, ce))
        if not overlaps:
            continue
        if len(overlaps) == 1 or seg_dur <= 0:
            # the common case (a line authored within one gap): show it whole in that gap
            cs, ce = overlaps[0]
            entries.extend(_bracketed_original_chunks(text, cs, ce, max_chars))
            continue
        # the line straddles a narration block: show each gap only ITS portion of the text
        # (proportional to the time the line overlaps that gap) instead of the whole line twice.
        n = len(text)
        for cs, ce in overlaps:
            lo = max(0, int(round((cs - seg_start) / seg_dur * n)))
            hi = min(n, int(round((ce - seg_start) / seg_dur * n)))
            piece = text[lo:hi].strip()
            if piece:
                entries.extend(_bracketed_original_chunks(piece, cs, ce, max_chars))
    return entries


def _split_sentences_keep_delims(text):
    """Split on terminal CJK sentence marks 。！？ keeping each delimiter with its sentence. A
    fragment that is only closing quotes/brackets (e.g. a trailing 」 after a 。 inside a quote) is
    re-attached to the previous sentence so quoted speech is never split off into a bare 」."""
    parts = [p.strip() for p in re.split(r"(?<=[。！？])", str(text)) if p.strip()]
    merged = []
    for part in parts:
        if merged and all(ch in _SUBTITLE_CLOSING_QUOTES for ch in part):
            merged[-1] += part
        else:
            merged.append(part)
    return merged


def _fallback_gap_entries(candidates, gaps, max_chars):
    """Coarse-ASR fallback. Each coarse-ASR line spans a whole window with no per-sentence onset, so
    it is split into WHOLE sentences (never mid-word); each sentence is assigned to the gap its
    char-proportional midpoint lands in, and within a gap the assigned sentences are packed
    SEQUENTIALLY from the first one's estimated onset at a comfortable read rate — so two lines in
    one gap never overlap or scatter to char-proportional tail slots — capped at the gap end. An
    over-dense gap front-truncates (shown) rather than dropping to blank."""
    # 1) split each coarse line into WHOLE sentences (never mid-word) and assign each to the gap
    #    its char-proportional midpoint lands in (the only "which gap" signal coarse ASR gives).
    buckets = {}  # gap_index -> [(estimated_onset, sentence_text)]
    for seg in candidates:
        for sentence in _split_sentences_keep_delims(seg["text"]) or [str(seg["text"]).strip()]:
            text = sentence.strip()
            if not text:
                continue
            sub = _sentence_subspan(seg, sentence)
            mid = (sub["start"] + sub["end"]) / 2.0
            gi = next((i for i, (gs, ge) in enumerate(gaps) if gs <= mid < ge), None)
            if gi is None:
                continue
            buckets.setdefault(gi, []).append((sub["start"], text))
    # 2) within each gap, pack the assigned sentences SEQUENTIALLY from the gap onset at a
    #    comfortable read rate. Anchoring to the gap onset (vs each sentence's char-proportional
    #    tail position) stops a line heard early from being shoved to the END of its window — the
    #    coarse-ASR lag. Full mode keeps the real ASR onset (the first sentence's own start); an
    #    over-dense gap front-truncates (shown) rather than dropping to blank.
    entries = []
    for gi, items in buckets.items():
        gs, ge = gaps[gi]
        items.sort(key=lambda it: it[0])
        # start at the first assigned sentence's estimated onset (clamped into the gap), then pack
        # the rest sequentially so they never overlap or scatter to char-proportional tail slots.
        cursor = min(ge, max(gs, min(start for start, _ in items)))
        for _, text in items:
            if cursor >= ge - _MIN_READABLE_SECONDS:
                break
            ce2 = min(ge, cursor + max(_MIN_READABLE_SECONDS, len(text) / _AUTO_ORIGINAL_READ_CPS))
            if ce2 - cursor < _MIN_READABLE_SECONDS:
                break
            max_len = int((ce2 - cursor) * _MAX_ORIGINAL_READ_CPS)
            shown = text if len(text) <= max_len else text[:max_len]
            entries.extend(_bracketed_original_chunks(shown, cursor, ce2, max_chars))
            cursor = ce2
    return entries


def _sentence_subspan(seg, sentence):
    """The slice of seg's [start,end] window that this sentence occupies, by character proportion.
    Single-sentence lines return the whole span unchanged."""
    full = str(seg["text"]).strip()
    if not full or sentence.strip() == full:
        return {"start": seg["start"], "end": seg["end"]}
    idx = full.find(sentence.strip())
    if idx < 0:
        return {"start": seg["start"], "end": seg["end"]}
    span = seg["end"] - seg["start"]
    s = seg["start"] + span * (idx / len(full))
    e = seg["start"] + span * ((idx + len(sentence.strip())) / len(full))
    return {"start": s, "end": e}


def _combined_subtitle_entries(narration, work_dir, video_duration):
    """Narration subtitle entries plus original-dialogue entries in the gaps, sorted by start.
    Original entries are confined to narration gaps, so they never overlap narration entries."""
    entries = list(_subtitle_entries(narration))
    entries.extend(_original_gap_subtitle_entries(narration, work_dir, video_duration))
    entries.sort(key=lambda x: (x["start"], x["end"]))
    return entries


def _source_subtitle_mask_covers_gaps(work_dir=None):
    """Whether the effective source mask hides hardcoded subtitles outside narration."""
    if not _source_subtitle_mask_policy(work_dir).get("active"):
        return False
    opacity = max(0.0, min(1.0, float(CONFIG.get("subtitle_mask_opacity", 0.6))))
    timing = str(CONFIG.get("source_subtitle_mask_timing", "narration") or "narration").lower()
    return opacity >= 1.0 - 1e-9 and timing == "all"
