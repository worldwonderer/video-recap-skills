import hashlib
import json
import os
import re
import wave
from pathlib import Path

from lib import CONFIG
from lib import log, run_cmd, get_video_duration
from timeline import build_timeline, save_timeline

SUBTITLE_RENDER_VERSION = 4  # bumped: BYO user_subtitles now fill gaps even with mask off
ASSEMBLY_MANIFEST = "assembly_manifest.json"
_SUBTITLE_TERMINAL_PUNCTUATION = "。！？!?…."
_SUBTITLE_CLOSING_QUOTES = "」』”’）)]】》〉\"'"


def _stable_json_dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _value_fingerprint(value):
    return hashlib.md5(_stable_json_dumps(value).encode("utf-8")).hexdigest()


def _file_fingerprint(path, chunk_size=1024 * 1024):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def _artifact_fingerprint(path):
    path = Path(path)
    return _file_fingerprint(path) if path.exists() else None


def _explicit_source_video():
    """Return the cut-mode source video only when the caller opted in explicitly."""
    if not CONFIG.get("source_video_explicit", False):
        return ""
    return str(CONFIG.get("source_video", "") or "").strip()


def _source_video_identity():
    source_video = _explicit_source_video()
    if not source_video:
        return None, None
    path = Path(source_video)
    return str(path.resolve()), _artifact_fingerprint(path)


def _assembly_manifest_payload(input_video, tts_segments, work_dir, output_path,
                               tts_meta_path=None, final_output=None):
    """Slim render record. The orchestrator reads `final_output` to report the result;
    `source_video` stays None unless cut mode explicitly passed --source-video, proving a
    stale ambient SOURCE_VIDEO never leaked into a full-mode timeline / 剪映 export."""
    input_video = Path(input_video)
    output_path = Path(output_path)
    source_video, source_video_fingerprint = _source_video_identity()
    payload = {
        "schema_version": 2,
        "input_video": str(input_video.resolve()),
        "source_video": source_video,
        "source_video_fingerprint": source_video_fingerprint,
        "tts_meta": str(Path(tts_meta_path).resolve()) if tts_meta_path else None,
        "tts_segments": len(tts_segments or []),
        "assembly_settings": assembly_settings_fingerprint(work_dir),
        "output_path": str(output_path.resolve()),
    }
    if final_output is not None:
        payload["final_output"] = str(Path(final_output).resolve())
    return payload


def _write_assembly_manifest(work_dir, manifest):
    path = Path(work_dir) / ASSEMBLY_MANIFEST
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _resolve_final_output(base, stem):
    """The recap output is the stable human alias recap_<stem>.mp4, overwritten in place
    on every run so the iterate-on-narration loop always refreshes the same file."""
    return Path(base) / f"recap_{stem}.mp4"


def _load_cut_timeline_plan(work_dir):
    raw_plan_path = Path(work_dir) / "clip_plan.json"
    validated_plan_path = Path(work_dir) / "clip_plan_validated.json"
    if not validated_plan_path.exists():
        return json.loads(raw_plan_path.read_text(encoding="utf-8")) if raw_plan_path.exists() else None
    if not raw_plan_path.exists():
        return json.loads(validated_plan_path.read_text(encoding="utf-8"))
    raw_plan = json.loads(raw_plan_path.read_text(encoding="utf-8"))
    validated_plan = json.loads(validated_plan_path.read_text(encoding="utf-8"))
    if (
        isinstance(validated_plan, dict)
        and validated_plan.get("raw_plan_fingerprint") == _value_fingerprint(raw_plan)
    ):
        return validated_plan
    return raw_plan


def _probe_canvas(video_path):
    """Return {width, height, fps} for a video via ffprobe (best-effort defaults)."""
    res = run_cmd(["ffprobe", "-v", "error", "-select_streams", "v:0",
                   "-show_entries", "stream=width,height,r_frame_rate",
                   "-of", "default=nw=1:nk=0", str(video_path)])
    width, height, fps = 1280, 720, 30.0
    if res.returncode == 0:
        for line in res.stdout.splitlines():
            line = line.strip()
            if line.startswith("width="):
                width = int(line.split("=", 1)[1] or width)
            elif line.startswith("height="):
                height = int(line.split("=", 1)[1] or height)
            elif line.startswith("r_frame_rate="):
                rate = line.split("=", 1)[1]
                if "/" in rate:
                    num, den = rate.split("/", 1)
                    if float(den or 0):
                        fps = round(float(num) / float(den), 3)
    return {"width": width, "height": height, "fps": fps}


def _has_audio_stream(video_path):
    """Return True when the input has an audio stream usable as [0:a]."""
    result = run_cmd([
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=index", "-of", "csv=p=0", str(video_path),
    ])
    return result.returncode == 0 and bool(result.stdout.strip())


def _build_video_clips(input_video, work_dir, duration_s):
    """Video-track clips for the timeline.

    In cut mode (a clip_plan.json + a known original source) each plan entry
    becomes a clip referencing the ORIGINAL source range, so an editor sees the
    real cuts. Otherwise (or full mode) the rendered input is a single clip.
    """
    source_video = _explicit_source_video()
    if source_video and os.path.exists(source_video):
        try:
            plan = _load_cut_timeline_plan(work_dir)
            if plan is None:
                raise ValueError("missing clip_plan.json")
            entries = plan.get("clips", plan) if isinstance(plan, dict) else plan
            clips, cursor = [], 0.0
            for c in entries:
                ss = float(c.get("source_start", c.get("start")))
                se = float(c.get("source_end", c.get("end")))
                timeline_start = c.get("output_start")
                timeline_end = c.get("output_end")
                dur = max(0.0, se - ss)
                if dur <= 0:
                    continue
                if timeline_start is None or timeline_end is None:
                    timeline_start = cursor
                    timeline_end = cursor + dur
                    cursor += dur
                else:
                    timeline_start = float(timeline_start)
                    timeline_end = float(timeline_end)
                    cursor = max(cursor, timeline_end)
                clips.append({"source_path": source_video, "source_start": ss,
                              "source_end": se, "timeline_start": timeline_start,
                              "timeline_end": timeline_end})
            if clips:
                return clips
        except (TypeError, ValueError, KeyError, OSError) as exc:
            log(f"  时间线: clip_plan 解析失败，回退单片 ({exc})")
    return [{"source_path": str(input_video), "source_start": 0.0,
             "source_end": float(duration_s), "timeline_start": 0.0,
             "timeline_end": float(duration_s)}]


def _timeline_subtitle_segments(tts_segments, work_dir, duration_s):
    """Display-ready subtitle cues for timeline/export text tracks.

    The narration audio track keeps raw semantic text for editor reference; this
    payload mirrors SRT/ASS display policy, including terminal-punctuation cleanup
    and original-dialogue gap subtitles when configured.
    """
    return [
        {
            "text": entry["text"],
            "timeline_start": float(entry["start"]),
            "timeline_end": float(entry["end"]),
        }
        for entry in _combined_subtitle_entries(tts_segments, work_dir, duration_s)
    ]


def _emit_timeline(input_video, tts_segments, work_dir, duration_s, has_bgm):
    """Build and persist the backend-neutral multi-track timeline.json."""
    canvas = _probe_canvas(input_video)
    video_clips = _build_video_clips(input_video, work_dir, duration_s)
    narration_segments = []
    for seg in tts_segments:
        if not isinstance(seg, dict):
            continue
        s, e = _seg_place_window(seg)
        if e <= s:
            continue
        narration_segments.append({
            "source_path": seg.get("audio_path", ""),
            "timeline_start": s, "timeline_end": e,
            "text": seg.get("narration", ""),
            "overlaps_speech": seg.get("overlaps_speech", True),
            "gain": 1.0,
        })
    fade = CONFIG.get("duck_fade_seconds", 0.3)
    bgm = None
    if has_bgm:
        bgm = {"source_path": CONFIG.get("bgm_path", ""),
               "volume": CONFIG.get("bgm_volume", 0.18),
               "ducking_volume": CONFIG.get("bgm_ducking_volume", 0.10),
               "fade": fade}
    # carry ducking automation whenever ducking is on at all; even under sidechain
    # mode the draft gets editable volume keyframes (ffmpeg stays the canonical mix)
    ducking = None
    if CONFIG.get("ducking_mode", "fixed") != "none":
        ducking = {"idle": CONFIG.get("idle_orig_volume", 1.0),
                   "speech": CONFIG.get("speech_ducking_volume", 0.2),
                   "quiet": CONFIG.get("zone_ducking_volume", 0.12),
                   "fade": fade,
                   "bridge": CONFIG.get("duck_bridge_seconds", 1.5)}
    subtitle_segments = _timeline_subtitle_segments(tts_segments, work_dir, duration_s)
    timeline = build_timeline(canvas, duration_s, video_clips,
                              narration_segments, bgm=bgm, ducking=ducking,
                              subtitle_segments=subtitle_segments)
    out = Path(work_dir) / "timeline.json"
    save_timeline(timeline, out)
    log(f"时间线模型: {out} ({len(timeline['tracks'])} 轨)")
    return timeline


def _seconds_to_srt_time(seconds):
    """将秒数转为 SRT 时间格式 HH:MM:SS,mmm"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _seconds_to_ass_time(seconds):
    """将秒数转为 ASS 时间格式 H:MM:SS.cc"""
    centiseconds = int(round(float(seconds) * 100))
    h = centiseconds // 360000
    centiseconds %= 360000
    m = centiseconds // 6000
    centiseconds %= 6000
    s = centiseconds // 100
    cs = centiseconds % 100
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _subtitle_style_config():
    """Return the internal default burn-in subtitle style."""
    return {
        "font_name": CONFIG.get("subtitle_font_name", "Arial"),
        "font_size": CONFIG.get("subtitle_font_size", 42),
        "primary_color": CONFIG.get("subtitle_primary_color", "&H00FFFFFF"),
        "outline_color": CONFIG.get("subtitle_outline_color", "&H00000000"),
        "outline": CONFIG.get("subtitle_outline", 2),
        "shadow": CONFIG.get("subtitle_shadow", 1),
        "alignment": CONFIG.get("subtitle_alignment", 2),
        "margin_l": CONFIG.get("subtitle_margin_l", 40),
        "margin_r": CONFIG.get("subtitle_margin_r", 40),
        "margin_v": CONFIG.get("subtitle_margin_v", 30),
        "max_chars": CONFIG.get("subtitle_max_chars", 20),
        "play_res_x": CONFIG.get("subtitle_play_res_x", 1280),
        "play_res_y": CONFIG.get("subtitle_play_res_y", 720),
    }


def _has_user_subtitles(work_dir):
    """True when the user dropped a bring-your-own original-subtitle file into work_dir."""
    return work_dir is not None and any(
        (Path(work_dir) / name).exists()
        for name in ("user_subtitles.json", "user_subtitles.srt", "user_subtitles.ass")
    )


def assembly_settings_fingerprint(work_dir=None):
    """Settings that affect the rendered video, used by pipeline resume cache. When work_dir is
    given, a user_subtitles presence flag is included so dropping in a user-subtitle file rebuilds
    the cached subtitles."""
    burn_subtitles = bool(CONFIG.get("burn_subtitles", False))
    mask_source_subtitles = burn_subtitles and bool(CONFIG.get("mask_source_subtitles", False))
    user_subtitles = _has_user_subtitles(work_dir)
    fingerprint = {
        "version": SUBTITLE_RENDER_VERSION,
        "subtitle_text_normalize": SUBTITLE_TEXT_NORMALIZE_VERSION,
        "user_subtitles": user_subtitles,
        "burn_subtitles": burn_subtitles,
        "force_video_reencode": bool(CONFIG.get("force_video_reencode", False)),
        "video_filters": {
            "mask_source_subtitles": mask_source_subtitles,
            "source_subtitle_mask_ratio": (
                CONFIG.get("source_subtitle_mask_ratio", 0.14) if mask_source_subtitles else None
            ),
        },
        "narration_timing": {
            "delay_seconds": CONFIG.get("narration_delay_seconds", 1.5),
            "tail_pad_seconds": CONFIG.get("narration_tail_pad_seconds", 0.1),
            "fade_ms": CONFIG.get("fade_ms", 120),
            "narration_speed": CONFIG.get("narration_speed", 1.0),
        },
        "audio_mix": {
            "ducking_mode": CONFIG.get("ducking_mode", "fixed"),
            "duck_fade_seconds": CONFIG.get("duck_fade_seconds", 0.3),
            "duck_bridge_seconds": CONFIG.get("duck_bridge_seconds", 1.5),
            "ducking_narr_weight": CONFIG.get("ducking_narr_weight", 1.5),
            "ducking_orig_volume": CONFIG.get("ducking_orig_volume", 0.3),
            "idle_orig_volume": CONFIG.get("idle_orig_volume", 1.0),
            "speech_ducking_volume": CONFIG.get("speech_ducking_volume", 0.2),
            "zone_ducking_volume": CONFIG.get("zone_ducking_volume", 0.12),
            "ducking_threshold": CONFIG.get("ducking_threshold", 0.15),
            "ducking_ratio": CONFIG.get("ducking_ratio", 3),
            "ducking_attack": CONFIG.get("ducking_attack", 10),
            "ducking_release": CONFIG.get("ducking_release", 300),
            "ducking_level_sc": CONFIG.get("ducking_level_sc", 2.0),
            "ducking_makeup": CONFIG.get("ducking_makeup", 1.2),
            "final_loudnorm": final_loudnorm_filter() or "off",
            "bgm_path": CONFIG.get("bgm_path", ""),
            "bgm_volume": CONFIG.get("bgm_volume", 0.18),
            "bgm_ducking_volume": CONFIG.get("bgm_ducking_volume", 0.10),
        },
    }
    if burn_subtitles:
        fingerprint["subtitle_renderer"] = "ass"
        fingerprint["subtitle_style"] = _subtitle_style_config()
    return fingerprint


def _wrap_subtitle_text(text, max_chars=20, line_break="\n"):
    """把过长字幕折成均衡的两行：在最接近中点的标点处断开，标点跟在上一行末尾，
    绝不让结尾的句号/逗号单独掉到第二行（之前的强制断行会孤立标点、两行严重失衡）。

    遗留工具：现在字幕由 _subtitle_entries / _split_subtitle_chunks 拆成短的单行块，烧录链路
    不再调用本函数；保留仅作单元测试与兜底。"""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    puncts = "，。！？、；：…—,.!?;:"
    mid = len(text) / 2
    # 候选断点 = 标点之后的位置（标点留在上一行）；排除最后一个字符，避免孤立结尾标点
    best = None
    for i, ch in enumerate(text[:-1]):
        if ch in puncts:
            cut = i + 1
            if best is None or abs(cut - mid) < abs(best - mid):
                best = cut
    if best is None:                       # 没有可用标点 → 从中点附近断开
        best = int(round(mid))
    line1, line2 = text[:best].strip(), text[best:].strip()
    if not line1 or not line2:             # 退化情形 → 保持一行，不强行制造孤行
        return text
    return line_break.join([line1, line2])


def _subtitle_display_text(text):
    """Return display-only subtitle text with trailing sentence punctuation removed.

    Narration/TTS source text stays untouched; this is applied only to SRT/ASS cue text.
    Closing quotes/brackets are preserved, so 「原声台词。」 renders as 「原声台词」.
    """
    text = str(text or "").strip()
    if not text:
        return ""
    suffix = ""
    while text and text[-1] in _SUBTITLE_CLOSING_QUOTES:
        suffix = text[-1] + suffix
        text = text[:-1].rstrip()
    text = text.rstrip(_SUBTITLE_TERMINAL_PUNCTUATION).rstrip()
    return (text + suffix).strip()


def _subtitle_chunk_weight(text):
    """Weight raw subtitle chunks for timing, independent of display punctuation cleanup."""
    core = re.sub(r"\s+", "", str(text or ""))
    return max(1, len(core))


def _subtitle_entry_chunks(raw_chunks):
    """Pair raw chunks used for timing with their final display text.

    Timing remains based on the raw split topology. Terminal punctuation is stripped only
    on the emitted text, while quote-only suffix chunks are folded into the previous cue
    so a closing bracket never renders alone.
    """
    chunks = [str(c).strip() for c in (raw_chunks or []) if str(c).strip()]
    out = []
    for i, chunk in enumerate(chunks):
        display = _subtitle_display_text(chunk)
        if not display:
            continue
        if all(ch in _SUBTITLE_CLOSING_QUOTES for ch in display):
            if out:
                out[-1]["text"] += display
            continue
        out.append({"raw": chunk, "text": display})
    return out


SUBTITLE_TEXT_NORMALIZE_VERSION = 1  # bump to rebuild cached subtitles when normalization changes


def _normalize_subtitle_text(s):
    """Normalize Chinese em-dashes in burned subtitle text: a run of one-or-more "—" (incl. "——")
    collapses to a single "，". Then collapse any resulting double commas ("，，"→"，") so the dash
    swap never leaves a doubled comma. Empty/None passes through as "" unchanged."""
    text = str(s or "")
    if not text:
        return text
    text = re.sub(r"—+", "，", text)
    text = re.sub(r"，{2,}", "，", text)
    return text


def _split_subtitle_chunks(text, max_chars):
    """Split one narration block (often several sentences) into short display chunks.

    A block is synthesized as one continuous TTS utterance for fluent prosody, but showing the
    whole paragraph as a single subtitle would force a tall multi-line band and lag the picture.
    So we cut the block at punctuation into clauses, then greedily pack adjacent clauses into
    chunks of at most `max_chars` — each chunk renders as ONE readable line synced to its slice of
    the block's audio. Punctuation stays attached here for lossless splitting; the display layer
    strips terminal sentence marks per subtitle-cue style."""
    text = str(text).strip()
    if not text:
        return []
    breakers = "，。！？、；：…—,.!?;:"
    clauses, buf = [], ""
    for ch in text:
        buf += ch
        if ch in breakers:
            clauses.append(buf)
            buf = ""
    if buf.strip():
        clauses.append(buf)
    # Any single clause longer than max_chars is hard-wrapped so no chunk ever exceeds one line.
    sized = []
    for clause in clauses:
        if len(clause) <= max_chars:
            sized.append(clause)
        else:
            for i in range(0, len(clause), max_chars):
                sized.append(clause[i:i + max_chars])
    chunks, cur = [], ""
    for clause in sized:
        sentence_closed = cur.rstrip().endswith(tuple(_SUBTITLE_TERMINAL_PUNCTUATION))
        if cur and (sentence_closed or len(cur) + len(clause) > max_chars):
            chunks.append(cur)
            cur = clause
        else:
            cur += clause
    if cur.strip():
        chunks.append(cur)
    return [c.strip() for c in chunks if c.strip()]


def _subtitle_entries(narration):
    """Collect subtitle entries from final TTS segment placement.

    Each placed segment is split into short one-line chunks and its played window
    [actual_place_start, actual_place_end] is distributed across them in proportion to character
    count — karaoke-style timing that keeps each line on screen only while it is roughly being
    spoken, instead of holding a whole paragraph for the segment's full duration."""
    max_chars = int(CONFIG.get("subtitle_max_chars", 20))
    entries = []
    for seg in narration:
        if not isinstance(seg, dict):
            continue
        text = str(seg.get("narration", "")).strip()
        if not text:
            continue
        try:
            start = float(seg.get("actual_place_start", seg["start"]))
            end = float(seg.get("actual_place_end", seg["end"]))
        except (KeyError, TypeError, ValueError):
            continue
        if end - start < 0.1:
            continue
        chunks = _subtitle_entry_chunks(_split_subtitle_chunks(text, max_chars))
        if not chunks:
            continue
        if len(chunks) == 1:
            entries.append({"start": start, "end": end, "text": chunks[0]["text"]})
            continue
        total_chars = sum(_subtitle_chunk_weight(c["raw"]) for c in chunks) or 1
        span = end - start
        cursor = start
        for i, chunk in enumerate(chunks):
            weight = _subtitle_chunk_weight(chunk["raw"])
            chunk_end = end if i == len(chunks) - 1 else cursor + span * (weight / total_chars)
            if i > 0 and chunk_end - cursor < 0.05:
                # slice too short to show on its own — fold the text into the previous line of THIS
                # block (i>0) and extend its end, so no chunk is ever silently dropped.
                entries[-1]["text"] += chunk["text"]
                entries[-1]["end"] = chunk_end
            else:
                entries.append({"start": cursor, "end": chunk_end, "text": chunk["text"]})
            cursor = chunk_end
    return entries


_MIN_GAP_TO_SUBTITLE = 0.8       # shortest original-audio gap (s) worth subtitling
_MIN_READABLE_SECONDS = 0.3      # shortest on-screen time (s) for an original-dialogue line
_MIN_ASR_CLIP_OVERLAP = 0.05     # shortest ASR↔clip overlap (s) to keep when remapping to output
_MAX_ORIGINAL_READ_CPS = 9.0     # densest an AUTO (uncalibrated) original line may be shown — skip cram
_AUTO_ORIGINAL_READ_CPS = 6.0    # comfortable read rate when packing coarse-ASR lines from a gap start


def _distribute_chunks(chunks, start, end):
    """Distribute [start,end] across raw chunks while emitting display-clean text.

    Terminal subtitle punctuation is visual-only: it is stripped from final cue text,
    but the raw split chunks remain the timing topology.
    """
    chunks = _subtitle_entry_chunks(chunks)
    if not chunks or end - start < 0.1:
        return []
    if len(chunks) == 1:
        return [{"start": start, "end": end, "text": chunks[0]["text"]}]
    total_chars = sum(_subtitle_chunk_weight(c["raw"]) for c in chunks) or 1
    span = end - start
    out, cursor = [], start
    for i, chunk in enumerate(chunks):
        weight = _subtitle_chunk_weight(chunk["raw"])
        chunk_end = end if i == len(chunks) - 1 else cursor + span * (weight / total_chars)
        if out and chunk_end - cursor < 0.05:
            out[-1]["text"] += chunk["text"]
            out[-1]["end"] = chunk_end
        else:
            out.append({"start": cursor, "end": chunk_end, "text": chunk["text"]})
        cursor = chunk_end
    return out


def _karaoke_chunks(text, start, end, max_chars):
    """Split narration `text` into short one-line chunks distributed across [start,end]."""
    return _distribute_chunks(_split_subtitle_chunks(text, max_chars), start, end)


def _bracketed_original_chunks(text, start, end, max_chars):
    """Like _karaoke_chunks, but for ORIGINAL dialogue: wrap the whole line in 「」 so it reads
    as quoted original speech, visually distinct from the recap's own narration."""
    raw = str(text).strip()
    if raw.startswith("「") and raw.endswith("」"):
        raw = raw[1:-1].strip()
    chunks = _split_subtitle_chunks(raw, max_chars)
    if chunks:
        chunks = list(chunks)
        chunks[0] = "「" + chunks[0]
        chunks[-1] = chunks[-1] + "」"
    return _distribute_chunks(chunks, start, end)


def _load_work_json(work_dir, name):
    path = Path(work_dir) / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


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
    if not (CONFIG.get("burn_subtitles", False)
            and CONFIG.get("subtitle_original_in_gaps", True)
            and (CONFIG.get("mask_source_subtitles", False) or _has_user_subtitles(work_dir))):
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


def _generate_srt(narration, work_dir, video_duration=None):
    """将解说脚本转为 SRT 字幕文件，使用实际音频放置时间。video_duration 给定时，原声留白处补烧原声字幕。"""
    srt_lines = []
    entries = (_subtitle_entries(narration) if video_duration is None
               else _combined_subtitle_entries(narration, work_dir, video_duration))
    # entries are already split into short one-line chunks, so no wrapping here.
    for idx, entry in enumerate(entries, start=1):
        start_ts = _seconds_to_srt_time(entry["start"])
        end_ts = _seconds_to_srt_time(entry["end"])
        srt_lines.append(str(idx))
        srt_lines.append(f"{start_ts} --> {end_ts}")
        srt_lines.append(_normalize_subtitle_text(entry["text"]))
        srt_lines.append("")
    srt_path = work_dir / "subtitles.srt"
    srt_path.write_text("\n".join(srt_lines), encoding="utf-8")
    return srt_path


def _escape_ass_text(text):
    """Escape user text for an ASS dialogue Text field."""
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\N")
    )


def _generate_ass(narration, work_dir, video_duration=None):
    """Generate an ASS subtitle file for readable hard-sub rendering. video_duration given ⇒ also
    burn the original dialogue (from ASR) during the original-audio gaps."""
    style = _subtitle_style_config()
    ass_lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        f"PlayResX: {int(style['play_res_x'])}",
        f"PlayResY: {int(style['play_res_y'])}",
        "",
        "[V4+ Styles]",
        (
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding"
        ),
        (
            "Style: Default,"
            f"{style['font_name']},{style['font_size']},{style['primary_color']},&H000000FF,"
            f"{style['outline_color']},&H64000000,0,0,0,0,100,100,0,0,1,"
            f"{style['outline']},{style['shadow']},{style['alignment']},"
            f"{style['margin_l']},{style['margin_r']},{style['margin_v']},1"
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    entries = (_subtitle_entries(narration) if video_duration is None
               else _combined_subtitle_entries(narration, work_dir, video_duration))
    # entries are already split into short one-line chunks, so no wrapping here.
    for entry in entries:
        text = _escape_ass_text(_normalize_subtitle_text(entry["text"]))
        ass_lines.append(
            "Dialogue: 0,"
            f"{_seconds_to_ass_time(entry['start'])},{_seconds_to_ass_time(entry['end'])},"
            f"Default,,0,0,0,,{text}"
        )

    ass_path = work_dir / "subtitles.ass"
    ass_path.write_text("\n".join(ass_lines) + "\n", encoding="utf-8")
    return ass_path


def _escape_subtitle_filter_path(path):
    """Escape a path for ffmpeg subtitle/ass video filter arguments."""
    text = str(path).replace("\\", "/")
    for raw, escaped in (
        ("\\", "\\\\"),
        (":", "\\:"),
        ("'", "\\'"),
        (",", "\\,"),
        ("[", "\\["),
        ("]", "\\]"),
    ):
        text = text.replace(raw, escaped)
    return text


def _subtitle_burn_filter(subtitle_path):
    """Build the ffmpeg video filter used for hard-sub rendering."""
    return f"subtitles=filename='{_escape_subtitle_filter_path(subtitle_path)}'"


def _output_downscale_filter(max_h):
    """Lanczos downscale that forces BOTH output dimensions even (libx264/yuv420p need it).

    -2 keeps the aspect ratio with an even width; 2*trunc(min(ih,H)/2) caps the height at H
    yet forces it even, so an odd OUTPUT_MAX_HEIGHT (e.g. 721) cannot produce an odd height
    that makes libx264 abort with an empty output file. 'min(ih,H)' only ever shrinks.
    """
    return f"scale=-2:'2*trunc(min(ih,{max_h})/2)':flags=lanczos"


def final_loudnorm_filter():
    """Final-mix loudness normalization filter from CONFIG, or None when disabled.

    Ducking branches set only relative balance; this single stage owns the
    absolute output loudness so the recap is not left too quiet.
    """
    if not CONFIG.get("final_loudnorm", True):
        return None
    return (
        f"loudnorm=I={CONFIG.get('target_lufs', -14.0)}"
        f":TP={CONFIG.get('target_true_peak', -1.0)}"
        f":LRA={CONFIG.get('target_lra', 11.0)}"
    )


def _apply_narration_speed(tts_segments, work_dir):
    """Globally speed up narration audio via atempo (CONFIG['narration_speed']).

    MiMo TTS reads a touch slowly for short-form recaps; a 1.1-1.2x bump makes it
    snappier without the chipmunk effect. Rewrites each segment's audio_path/duration
    to the sped copy so the rest of assembly is unchanged. No-op at speed 1.0.
    """
    speed = float(CONFIG.get("narration_speed", 1.0) or 1.0)
    if abs(speed - 1.0) <= 1e-3:
        return
    factor = max(0.5, min(2.0, speed))
    done = 0
    for seg in tts_segments:
        src = seg.get("audio_path")
        if not src or not os.path.exists(src):
            continue
        out = str(Path(work_dir) / f"_spd_{seg.get('index', 0)}.wav")
        res = run_cmd(["ffmpeg", "-y", "-i", src, "-filter:a", f"atempo={factor:.3f}",
                       "-ar", "44100", "-ac", "1", "-acodec", "pcm_s16le", out])
        if res.returncode == 0 and os.path.exists(out):
            seg["audio_path"] = out
            seg["audio_duration"] = get_video_duration(out)
            done += 1
    log(f"解说整体提速: atempo={factor:.2f} ({done} 段)")


def _source_subtitle_mask_filter():
    """ffmpeg drawbox that masks burned-in source subtitles along the bottom, or None.

    Many source videos (e.g. 庆余年) ship hardcoded subtitles; without this the recap
    shows the original subs AND our narration subs stacked. Covers the bottom band with
    an opaque box; our subtitles render on top of it.
    """
    if not (CONFIG.get("burn_subtitles", False) and CONFIG.get("mask_source_subtitles", False)):
        return None
    ratio = max(0.0, min(0.5, float(CONFIG.get("source_subtitle_mask_ratio", 0.14) or 0.0)))
    # When we burn our OWN subtitle the band must sit behind it, but our subtitles are split into
    # short ONE-LINE chunks (see _subtitle_entries), so the band only needs to cover a single line
    # plus its bottom margin — never two. Sizing for one line keeps the black bar small so it does
    # not compress the picture (a 2-line band ate ~23% of the height). Take the larger of the raw
    # ratio and this single-line need.
    if CONFIG.get("burn_subtitles", False):
        style = _subtitle_style_config()
        play_res_y = max(1.0, float(style["play_res_y"]))
        line_h = float(style["font_size"]) * 1.25
        sub_band = (float(style["margin_v"]) + line_h + 10.0) / play_res_y
        ratio = min(0.5, max(ratio, sub_band))
    if ratio <= 0:
        return None
    return f"drawbox=x=0:y=ih-ih*{ratio:.3f}:w=iw:h=ih*{ratio:.3f}:color=black@1.0:t=fill"


def _seg_place_window(seg):
    """Return a segment's actual placed (start, end) on the output timeline."""
    s = seg.get("actual_place_start", seg.get("start", 0))
    e = seg.get("actual_place_end", seg.get("end", 0))
    return s, e


def _amix_tail(narr_vol, bgm_chain=""):
    """Mix the prepared original track [orig] (+ optional BGM bed) with the boosted
    narration [narr] into [aout]. bgm_chain, when given, defines [bgm] from input [2:a]."""
    narr = f"[1:a]volume={narr_vol},aresample=48000[narr];"
    if bgm_chain:
        return bgm_chain + narr + "[orig][bgm][narr]amix=inputs=3:duration=first:dropout_transition=0:normalize=0[aout]"
    return narr + "[orig][narr]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"


def _duck_ramp(s, e, fade):
    """A 0→1 trapezoid over [s,e] with `fade`-second ramps at each edge (no clicks)."""
    if float(fade or 0) <= 0:
        return f"between(t,{s:.2f},{e:.2f})"
    return f"min(1,max(0,min(t-{s:.2f},{e:.2f}-t)/{fade:.2f}))"


def _placement_windows(tts_segments, level_for):
    """Collect [(start, end, level)] placement windows for the placed beats, using
    `level_for(seg)` to pick each beat's duck level. Skips non-dicts and empty spans."""
    windows = []
    for seg in tts_segments:
        if not isinstance(seg, dict):
            continue
        s, e = _seg_place_window(seg)
        if e - s <= 0:
            continue
        windows.append((s, e, level_for(seg)))
    return windows


def _coalesce_duck_windows(windows, bridge):
    """Merge placement windows whose gap is smaller than `bridge` into one held span,
    so the duck stays low across short inter-beat gaps (a continuous bed) instead of the
    original swelling back to idle between sentences. Each merged span keeps the
    most-ducked (lowest) level of its members. Genuine gaps >= bridge survive as separate
    dips, so the original still breathes there. `windows` is [(start, end, level)]."""
    rel = sorted(([float(s), float(e), float(g)] for s, e, g in windows if e > s),
                 key=lambda w: w[0])
    if not rel:
        return []
    merged = [rel[0][:]]
    for s, e, g in rel[1:]:
        if s - merged[-1][1] < bridge:
            merged[-1][1] = max(merged[-1][1], e)
            merged[-1][2] = min(merged[-1][2], g)
        else:
            merged.append([s, e, g])
    return merged


def _duck_envelope(tts_segments, idle, speech_vol, quiet_vol, fade, bridge=None):
    """Per-beat ducking automation for the ORIGINAL track.

    Ramps the original down under each placed narration window — to speech_vol where the
    beat overlaps source dialogue, quiet_vol where it sits in a quiet window — and HOLDS
    that duck across inter-beat gaps shorter than `bridge` so the source dialogue does not
    pop back up between sentences. Only gaps >= bridge swell back to `idle` (lead-in/out
    and deliberate long pauses). Returns a volume= expression, or None when no beat carries
    placement info (caller falls back to a constant)."""
    if bridge is None:
        bridge = 2 * float(fade or 0)
    windows = _placement_windows(
        tts_segments, lambda seg: speech_vol if seg.get("overlaps_speech", True) else quiet_vol)
    merged = _coalesce_duck_windows(windows, bridge)
    if not merged:
        return None
    terms = [f"+({level - idle:.3f})*{_duck_ramp(s, e, fade)}" for s, e, level in merged]
    return f"max(0,min(1,{idle}{''.join(terms)}))"


def _bgm_envelope(tts_segments, base, duck, fade, bridge=None):
    """Per-beat ducking automation for the BGM track: hold the bed at `base`, dip to
    `duck` under each narration window (held across gaps < `bridge`) so the voice stays
    clear. None if no beats."""
    if bridge is None:
        bridge = 2 * float(fade or 0)
    windows = _placement_windows(tts_segments, lambda seg: duck)
    merged = _coalesce_duck_windows(windows, bridge)
    if not merged:
        return None
    terms = [f"+({lvl - base:.3f})*{_duck_ramp(s, e, fade)}" for s, e, lvl in merged]
    return f"max(0,min(1,{base}{''.join(terms)}))"


def _build_audio_filter_complex(
    tts_segments,
    has_bgm=False,
    *,
    original_audio_label="0:a",
    bgm_audio_label=None,
):
    """Compose the audio tracks into [aout], like a cut-software timeline.

    Tracks:
      - original (input [0:a], the video's own audio): ducked under each narration
        window by a per-beat volume envelope, but held up at `idle_orig_volume` in
        the gaps so the recap never drops to dead air between sentences.
      - bgm (input [2:a], optional): a looped music bed, gently ducked under narration.
      - narration (input [1:a]): the TTS, boosted and laid on top.
    CONFIG["ducking_mode"] (default "fixed") selects the original-track strategy:
    fixed = the gap-fill envelope above; sidechaincompress = auto-duck keyed off the
    narration; none = no ducking. Placement comes from actual_place_start/end.
    """
    ducking_mode = CONFIG.get("ducking_mode", "fixed")
    narr_vol = CONFIG.get("ducking_narr_weight", 1.5)
    fade = CONFIG.get("duck_fade_seconds", 0.3)
    original_in = f"[{original_audio_label}]"
    bgm_in = f"[{bgm_audio_label or '2:a'}]"

    # BGM bed (input [2:a]): ducked under each narration window when present.
    bgm_chain = ""
    if has_bgm:
        base = CONFIG.get("bgm_volume", 0.18)
        bgm_expr = _bgm_envelope(tts_segments, base, CONFIG.get("bgm_ducking_volume", 0.10), fade,
                                 bridge=CONFIG.get("duck_bridge_seconds", 1.5))
        if bgm_expr:
            bgm_chain = f"{bgm_in}volume='{bgm_expr}':eval=frame,aresample=48000[bgm];"
        else:
            bgm_chain = f"{bgm_in}volume={base},aresample=48000[bgm];"

    if ducking_mode == "sidechaincompress":
        # The narration keys the compressor; split it so it can also be mixed in.
        head = (
            f"{original_in}aresample=48000[o0];"
            "[1:a]aresample=48000,asplit=2[sckey][scnarr];"
            f"[o0][sckey]sidechaincompress="
            f"threshold={CONFIG['ducking_threshold']}:ratio={CONFIG['ducking_ratio']}"
            f":attack={CONFIG['ducking_attack']}:release={CONFIG['ducking_release']}"
            f":knee=2.5:makeup={CONFIG['ducking_makeup']}:level_sc={CONFIG['ducking_level_sc']}[orig];"
        )
        narr = f"[scnarr]volume={narr_vol}[narr];"
        if bgm_chain:
            return head + bgm_chain + narr + "[orig][bgm][narr]amix=inputs=3:duration=first:dropout_transition=0:normalize=0[aout]"
        return head + narr + "[orig][narr]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"

    if ducking_mode == "none":
        return f"{original_in}aresample=48000[orig];" + _amix_tail(narr_vol, bgm_chain)

    # fixed (default): gap-fill ducking envelope on the original track.
    idle = CONFIG.get("idle_orig_volume", 1.0)
    speech_vol = CONFIG.get("speech_ducking_volume", 0.2)
    quiet_vol = CONFIG.get("zone_ducking_volume", 0.12)
    bridge = CONFIG.get("duck_bridge_seconds", 1.5)
    expr = _duck_envelope(tts_segments, idle, speech_vol, quiet_vol, fade, bridge=bridge)
    if expr:
        n_overlap = sum(1 for s in tts_segments if isinstance(s, dict) and s.get("overlaps_speech", True))
        n_quiet = sum(1 for s in tts_segments if isinstance(s, dict) and not s.get("overlaps_speech", True))
        log(f"gap-fill ducking: 间隙原声={idle}, 对白段={speech_vol}({n_overlap}), 安静段={quiet_vol}({n_quiet}), 桥接间隙<{bridge}s")
        orig = f"{original_in}volume='{expr}':eval=frame,aresample=48000[orig];"
    else:
        # No placement info at all: hold the original at a constant level.
        orig = f"{original_in}volume={CONFIG.get('ducking_orig_volume', 0.3)},aresample=48000[orig];"
    return orig + _amix_tail(narr_vol, bgm_chain)


def assemble_video(input_video, tts_segments, work_dir, output_path):
    """组装最终视频"""
    if not tts_segments:
        raise RuntimeError("tts_meta.json 没有有效解说音频，已中止以避免生成无解说视频")

    video_duration = get_video_duration(input_video)

    # 解说整体提速（可选）后，将所有 TTS 片段按时间位置合成到与视频等长的音轨上
    _apply_narration_speed(tts_segments, work_dir)
    narration_wav = work_dir / "narration.wav"
    _build_timed_narration(tts_segments, narration_wav, video_duration, work_dir)

    # 始终生成 SRT 字幕文件（原声留白处补烧原声字幕，传入成片时长以计算留白区间）
    srt_path = _generate_srt(tts_segments, work_dir, video_duration)
    log(f"字幕文件: {srt_path}")
    ass_path = None
    if CONFIG.get("burn_subtitles", False):
        ass_path = _generate_ass(tts_segments, work_dir, video_duration)
        log(f"压制字幕文件: {ass_path}")

    # 可选 BGM：作为一条独立音轨（input [2:a]）混入，旁白处自动压低
    bgm_path = CONFIG.get("bgm_path", "")
    has_bgm = bool(bgm_path) and os.path.exists(bgm_path)
    if bgm_path and not has_bgm:
        log(f"  ⚠️ BGM 文件不存在，跳过: {bgm_path}")
    elif has_bgm:
        log(f"BGM 铺底: {bgm_path} (音量 {CONFIG.get('bgm_volume', 0.18)}，旁白时 {CONFIG.get('bgm_ducking_volume', 0.10)})")

    # 多轨时间线模型（timeline.json）：canonical 渲染仍是 ffmpeg，此模型供检视/可选导出
    _emit_timeline(input_video, tts_segments, work_dir, video_duration, has_bgm)

    # 混合原始音频 + 解说音频（+ 可选 BGM）
    source_has_audio = _has_audio_stream(input_video)
    if source_has_audio:
        original_audio_input = []
        original_audio_label = "0:a"
        bgm_audio_label = "2:a"
    else:
        log("源视频无音轨，使用静音原声音轨进行混音")
        original_audio_input = [
            "-f", "lavfi", "-t", str(video_duration),
            "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        ]
        original_audio_label = "2:a"
        bgm_audio_label = "3:a"
    filter_complex = _build_audio_filter_complex(
        tts_segments,
        has_bgm,
        original_audio_label=original_audio_label,
        bgm_audio_label=bgm_audio_label,
    )

    # 对于超长 volume 表达式（多段解说），使用 -filter_complex_script 避免命令行溢出
    # 末端整体响度归一：ducking 只管相对平衡，这一步统一成片绝对响度
    aout_label = "[aout]"
    final_ln = final_loudnorm_filter()
    if final_ln:
        filter_complex += f";[aout]{final_ln}[aoutln]"
        aout_label = "[aoutln]"
        log(f"成片响度归一: {final_ln}")

    # BGM is input [2:a]; -stream_loop -1 loops it to cover the whole timeline (amix
    # duration=first + -t trim it back to the video length).
    bgm_input = ["-stream_loop", "-1", "-i", str(bgm_path)] if has_bgm else []

    filter_complex_bytes = filter_complex.encode('utf-8')
    if len(filter_complex_bytes) > 8000:
        fc_script = Path(work_dir) / ".filter_complex.txt"
        fc_script.write_text(filter_complex, encoding="utf-8")
        log(f"使用 filter_complex_script (表达式长度 {len(filter_complex_bytes)} bytes)")
        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_video),
            "-i", str(narration_wav),
            *original_audio_input,
            *bgm_input,
            "-filter_complex_script", str(fc_script),
            "-map", "0:v", "-map", aout_label,
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_video),
            "-i", str(narration_wav),
            *original_audio_input,
            *bgm_input,
            "-filter_complex", filter_complex,
            "-map", "0:v", "-map", aout_label,
        ]

    # Video filter chain: mask source subtitles first (drawbox), then burn our subtitles
    # on top. Either one forces a re-encode; with neither, the video stream is copied.
    crf = str(CONFIG.get("output_crf", 18))  # env_int already clamps to >=0; keep 0 (lossless) intact
    preset = str(CONFIG.get("output_preset", "veryfast") or "veryfast")
    max_h = int(CONFIG.get("output_max_height", 0) or 0)
    vf_chain = []
    mask_filter = _source_subtitle_mask_filter()
    if mask_filter:
        vf_chain.append(mask_filter)
    if CONFIG.get("burn_subtitles", False):
        vf_chain.append(_subtitle_burn_filter(ass_path))
    # Downscale LAST so the mask + burned subtitles render at native resolution and are then
    # scaled down with the frame (crisp). The helper forces both dimensions even so an odd
    # OUTPUT_MAX_HEIGHT can't crash libx264; 'min(ih,H)' only ever shrinks the source.
    if max_h > 0:
        vf_chain.append(_output_downscale_filter(max_h))
    # yuv420p: 10-bit/4:2:2 sources re-encoded as-is play on desktop but fail on WeChat/
    # mobile/Safari; force 8-bit 4:2:0 so every recap is universally decodable. yuv420p also
    # needs EVEN width AND height, so normalize odd dims (4:2:2/4:4:4 permit them) before the
    # encode — otherwise libx264 aborts to a 0-byte file. The downscale helper already evens out.
    even = "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    if vf_chain:
        if max_h <= 0:  # no downscale in the chain to force even dims
            vf_chain.append(even)
        cmd += ["-vf", ",".join(vf_chain), "-c:v", "libx264", "-preset", preset, "-crf", crf,
                "-pix_fmt", "yuv420p"]
        notes = ((["遮挡原字幕"] if mask_filter else [])
                 + (["压制解说字幕"] if CONFIG.get("burn_subtitles", False) else [])
                 + ([f"缩放≤{max_h}p"] if max_h > 0 else []))
        log(f"视频重编码: {' + '.join(notes)} (crf={crf}, preset={preset})")
    elif CONFIG.get("force_video_reencode", False):
        cmd += ["-vf", even, "-c:v", "libx264", "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p"]
    else:
        cmd += ["-c:v", "copy"]

    # +faststart relocates the moov atom to the front so web/social players can start
    # before the full file downloads; valid (and beneficial) on the copy path too.
    cmd += ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
            "-t", str(video_duration), str(output_path)]
    try:
        result = run_cmd(cmd)
        if result.returncode != 0:
            raise RuntimeError(f"视频组装失败: {result.stderr}")
    finally:
        # 清理临时 filter_complex 脚本（无论 ffmpeg 是否成功）
        if len(filter_complex_bytes) > 8000:
            fc_script.unlink(missing_ok=True)

    log(f"最终视频: {output_path} ({output_path.stat().st_size / 1024 / 1024:.1f}MB)")
    return output_path


def _adjust_tts_speed(audio_path, target_duration, work_dir, tts_rate_offset=0.0):
    """如果 TTS 音频超过目标时长，用 ffmpeg atempo 温和加速"""
    audio_path = Path(audio_path)
    current_dur = get_video_duration(audio_path)
    if current_dur <= target_duration or current_dur == 0:
        return str(audio_path), current_dur

    ratio = current_dur / target_duration

    # 累积上限：TTS rate × atempo ≤ 1.2x
    effective_max = 1.2 / (1.0 + tts_rate_offset)
    effective_max = max(1.0, effective_max)

    if ratio > effective_max:
        # 实际截断到目标时长，加 fade-out 防止爆音
        truncated_path = audio_path.with_name(f"{audio_path.stem}_cut{audio_path.suffix}")
        fade_out = min(0.15, target_duration * 0.1)
        cmd = ["ffmpeg", "-y", "-i", str(audio_path),
               "-t", f"{target_duration:.3f}",
               "-af", f"afade=t=out:st={max(0, target_duration - fade_out):.3f}:d={fade_out:.3f}",
               "-ar", "44100", "-ac", "1", str(truncated_path)]
        result = run_cmd(cmd)
        if result.returncode == 0:
            new_dur = get_video_duration(truncated_path)
            log(f"  TTS 截断: {current_dur:.1f}s → {new_dur:.1f}s (无法加速到 x{ratio:.2f})")
            return str(truncated_path), new_dur
        log(f"  警告: TTS 截断失败，保留原音频 ({current_dur:.1f}s)")
        return str(audio_path), current_dur

    # 温和加速
    tempo = min(ratio, effective_max)
    adjusted_path = audio_path.with_name(f"{audio_path.stem}_adj{audio_path.suffix}")
    cmd = ["ffmpeg", "-y", "-i", str(audio_path),
           "-filter:a", f"atempo={tempo:.3f}",
           "-ar", "44100", "-ac", "1", str(adjusted_path)]
    result = run_cmd(cmd)
    if result.returncode == 0:
        new_dur = get_video_duration(adjusted_path)
        log(f"  TTS 温和加速: {current_dur:.1f}s → {new_dur:.1f}s (x{tempo:.2f})")
        return str(adjusted_path), new_dur
    return str(audio_path), current_dur


def _build_timed_narration(tts_segments, output_wav, video_duration, work_dir):
    """将 TTS 片段按时间轴放置到一条与视频等长的音轨上"""
    sample_rate = 44100
    total_samples = int(video_duration * sample_rate)
    buffer = bytearray(total_samples * 2)
    last_written_end = 0  # 追踪已写入位置，防止重叠
    prev_pause_samples = 0  # 前一段的 pause_after_ms，控制段间间隔
    skipped_count = 0  # 因 WAV 缺失/损坏/重采样失败而被跳过的段数
    placed_count = 0  # 真正写入音频的段数；防止"成功"生成全静音旁白
    prev_authored_end = None  # 上一段作者标注的结束时间，用于判断"段落"边界
    run_gap = float(CONFIG.get("narration_run_gap_seconds", 1.6))   # 作者留白 > 此值 = 新段落
    tighten = bool(CONFIG.get("narration_tighten", True))
    tight_pause_samples = int(max(0.0, float(CONFIG.get("narration_tight_pause_seconds", 0.35))) * sample_rate)
    # 漂移上限：收紧时一句最多比作者标注的时间提前 max_pull 秒，避免整段解说被全部压到前面、与画面脱节
    max_pull_samples = int(max(0.0, float(CONFIG.get("narration_max_pull_seconds", 2.5))) * sample_rate)

    for seg in tts_segments:
        wav_path = seg["audio_path"]
        seg_pause_ms = seg.get("pause_after_ms", CONFIG.get("breath_ms", 250))
        # 段落收紧：同一段落内（与上一句作者留白 <= run_gap）把这一句紧贴上一句的实际收尾播放，
        # 句间间隔固定为 tight_pause，不受 slot 内居中延迟 / TTS 时长波动影响。段落之间（作者特意留
        # 的大留白，让精彩原声透出）才放回原声。这样句间间隔稳定、不会出现"一句解说一段空白"。
        cur_authored_start = float(seg.get("start", 0.0))
        is_run_start = (placed_count == 0 or prev_authored_end is None
                        or cur_authored_start - prev_authored_end > run_gap)
        prev_authored_end = float(seg.get("end", cur_authored_start))

        if not os.path.exists(wav_path):
            seg["actual_place_start"] = seg["start"]
            seg["actual_place_end"] = seg["start"]
            prev_pause_samples = int(seg_pause_ms * sample_rate / 1000)
            skipped_count += 1
            continue

        # WAV 格式验证 + 采样率检查（合并为一次 wave.open）
        original_wav_path = wav_path
        _do_resample = False
        try:
            with wave.open(wav_path, 'rb') as wf_check:
                wf_channels = wf_check.getnchannels()
                wf_sampwidth = wf_check.getsampwidth()
                if wf_sampwidth != 2 or wf_channels != 1:
                    log(f"  跳过非标准 WAV: {wav_path} (channels={wf_channels}, sampwidth={wf_sampwidth}), 需要 mono 16-bit")
                    seg["actual_place_start"] = seg["start"]
                    seg["actual_place_end"] = seg["start"]
                    prev_pause_samples = int(seg_pause_ms * sample_rate / 1000)
                    skipped_count += 1
                    continue
                if wf_check.getframerate() != sample_rate:
                    _do_resample = True
        except Exception as e:
            log(f"  WAV 读取失败: {wav_path}: {e}")
            seg["actual_place_start"] = seg["start"]
            seg["actual_place_end"] = seg["start"]
            prev_pause_samples = int(seg_pause_ms * sample_rate / 1000)
            skipped_count += 1
            continue

        tts_rate_offset = seg.get("tts_rate_offset", 0.0)
        tts_dur = seg.get("audio_duration", 0)

        configured_delay = max(0.0, float(CONFIG.get("narration_delay_seconds", 1.5) or 0.0))
        tail_pad = max(0.0, float(CONFIG.get("narration_tail_pad_seconds", 0.1) or 0.0))
        slot_duration = max(0.0, float(seg["end"]) - float(seg["start"]))
        max_delay = max(0.0, slot_duration - float(tts_dur or 0.0) - tail_pad)
        narration_delay = min(configured_delay, max_delay)
        start_sample = int((seg["start"] + narration_delay) * sample_rate)
        end_boundary = int(min(seg["end"], video_duration) * sample_rate)

        # 段间间隔：使用前一段的 pause_after_ms（来自 narration.json）
        min_start_with_pause = last_written_end + prev_pause_samples
        if tighten and not is_run_start:
            # 段落内：紧贴上一句的实际收尾播放，句间间隔固定为 tight_pause（不被 slot 内居中延迟撑大），
            # 但不早于"作者标注起始 - max_pull"，防止整段被压到前面与画面脱节。
            drift_floor = int(cur_authored_start * sample_rate) - max_pull_samples
            actual_start = max(last_written_end + tight_pause_samples, drift_floor)
        else:
            # 段落起点（或关闭收紧）：尊重作者标注的起始 + 入场延迟，让画面/原声先立住
            actual_start = max(start_sample, min_start_with_pause)
        actual_start = min(actual_start, end_boundary)  # 不超出 slot 边界

        # 根据实际可用空间决定是否加速
        available_samples = end_boundary - actual_start
        available_duration = max(available_samples / sample_rate, 0)
        if tts_dur > available_duration > 0:
            wav_path, _actual_dur = _adjust_tts_speed(wav_path, available_duration, work_dir, tts_rate_offset)
        else:
            pass  # tts_dur <= available_duration, no speed adjust needed

        # _adjust_tts_speed 输出固定 44100Hz mono 16bit，若文件被替换则无需 resample
        if wav_path != original_wav_path:
            _do_resample = False
        if _do_resample:
            tmp_path = str(Path(work_dir) / f"_rs_{seg.get('index', 0)}.wav")
            rs_result = run_cmd(["ffmpeg", "-y", "-i", wav_path,
                                 "-ar", str(sample_rate), "-ac", "1",
                                 "-acodec", "pcm_s16le", tmp_path])
            if rs_result.returncode != 0 or not os.path.exists(tmp_path):
                log(f"  重采样失败，跳过本段: {wav_path}: {rs_result.stderr}")
                seg["actual_place_start"] = seg["start"]
                seg["actual_place_end"] = seg["start"]
                prev_pause_samples = int(seg_pause_ms * sample_rate / 1000)
                skipped_count += 1
                continue
            wav_path = tmp_path

        with wave.open(wav_path, "rb") as wf:
            wf_data = bytearray(wf.readframes(wf.getnframes()))

        # 按场景边界裁剪
        audio_samples = len(wf_data) // 2
        available = end_boundary - actual_start
        write_samples = min(audio_samples, max(available, 0))

        if write_samples <= 0:
            log(f"  跳过: {seg['start']:.1f}s-{seg['end']:.1f}s (无空间)")
            seg["actual_place_start"] = seg["start"]
            seg["actual_place_end"] = seg["start"]
            prev_pause_samples = int(seg_pause_ms * sample_rate / 1000)
            continue

        # 裁剪到写入长度
        wf_data = wf_data[:write_samples * 2]

        # 重叠检测：跳过与前段重叠的部分（在 fade 之前，避免截断后丢失 fade-in）
        if actual_start < last_written_end:
            overlap_ms = (last_written_end - actual_start) * 1000 / sample_rate
            if last_written_end >= actual_start + write_samples:
                log(f"  跳过重叠段: {actual_start/sample_rate:.1f}s "
                    f"(与前段重叠 {overlap_ms:.0f}ms)")
                seg["actual_place_start"] = seg["start"]
                seg["actual_place_end"] = seg["start"]
                prev_pause_samples = int(seg_pause_ms * sample_rate / 1000)
                continue
            log(f"  重叠 {overlap_ms:.0f}ms，截断前部")
            skip_samples = last_written_end - actual_start
            wf_data = wf_data[skip_samples * 2:]
            write_samples -= skip_samples
            actual_start = last_written_end

        # fade-in / fade-out（在 overlap 裁剪之后应用，确保正确的音频包络）
        fade_len = min(int(CONFIG.get("fade_ms", 300) * sample_rate / 1000), write_samples // 4)
        for i in range(fade_len):
            gain = i / fade_len
            s = i * 2
            sample = int.from_bytes(wf_data[s:s+2], 'little', signed=True)
            sample = int(sample * gain)
            wf_data[s:s+2] = sample.to_bytes(2, 'little', signed=True)
        for i in range(fade_len):
            gain = 1.0 - i / fade_len
            s = (write_samples - 1 - i) * 2
            if s < 0:
                break
            sample = int.from_bytes(wf_data[s:s+2], 'little', signed=True)
            sample = int(sample * gain)
            wf_data[s:s+2] = sample.to_bytes(2, 'little', signed=True)

        buffer[actual_start * 2: actual_start * 2 + write_samples * 2] = wf_data
        seg["actual_place_start"] = actual_start / sample_rate
        seg["actual_place_end"] = (actual_start + write_samples) / sample_rate
        last_written_end = actual_start + write_samples
        prev_pause_samples = int(seg_pause_ms * sample_rate / 1000)
        placed_count += 1

    with wave.open(str(output_wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(buffer))

    if tts_segments and placed_count == 0:
        output_wav.unlink(missing_ok=True)
        raise RuntimeError(
            f"全部 {len(tts_segments)} 段解说均被跳过或未能写入"
            f"（WAV 缺失/损坏/重采样失败/无可用时间；跳过 {skipped_count} 段），"
            "已中止以避免生成无解说视频"
        )

    log(f"解说音轨: {video_duration:.1f}s, {len(tts_segments)} 段")


def _ffmpeg_filters():
    """ffmpeg's compiled-in filter names. Independent copy of
    skills/video-recap/scripts/doctor.py:_ffmpeg_filters (skills share no code) — keep the
    parse in sync with that copy."""
    import shutil
    import subprocess
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return set()
    try:
        result = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                                text=True, capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return set()
    if result.returncode != 0:
        return set()
    filters = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] and parts[0][0] in ".TSCAPN|":
            filters.add(parts[1])
    return filters


def _preflight_burn_subtitles():
    """Fail before the (re-encoding) render when burn-in is on but ffmpeg lacks the libass
    `subtitles` filter. _subtitle_burn_filter burns even the .ass through `subtitles=`, so
    that is the required capability. Defense-in-depth: the orchestrator (recap.py) preflights
    this earlier, but assemble.py can be run standalone. Only fires when ffmpeg EXISTS but
    can't burn — an absent ffmpeg fails the render regardless and would also break the mocked,
    ffmpeg-less test environment."""
    import shutil
    if not CONFIG.get("burn_subtitles", False):
        return
    if shutil.which("ffmpeg") is None:
        return
    if "subtitles" not in _ffmpeg_filters():
        raise SystemExit(
            "字幕烧录已开启，但当前 ffmpeg 不支持 subtitles/libass 滤镜，渲染会在最后一步失败。\n"
            "  解决：安装带 libass 的 ffmpeg，或加 --no-burn-subtitles 关闭烧录（仍输出 .srt 外挂字幕）。")


def main():
    import argparse
    import json
    import shutil
    from pathlib import Path
    ap = argparse.ArgumentParser(
        description="video-assemble: mux narration audio over the video, duck the original, render subtitles.")
    ap.add_argument("video", help="source video (edited_source.mp4 in cut mode, else the original)")
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--tts-meta", default=None, help="tts_meta.json (default: <work-dir>/tts_meta.json)")
    ap.add_argument("--recap-stem", default=None, help="final recap filename stem (default: video stem)")
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--burn-subtitles", action=argparse.BooleanOptionalAction, default=None,
                    help="burn narration subtitles into the video (default on; --no-burn-subtitles to disable)")
    ap.add_argument("--source-video", default=None,
                    help="original source video (cut mode) so timeline.json / 剪映 export reference the real clips")
    ap.add_argument("--export-jianying", action="store_true",
                    help="also export an OPTIONAL 剪映/JianYing draft from timeline.json after rendering")
    ap.add_argument("--jianying-out", default=None, help="parent dir for the 剪映 draft (default: work-dir)")
    ap.add_argument("--jianying-bundle-media", action="store_true",
                    help="copy media into the 剪映 draft folder (default on; portable/self-contained)")
    ap.add_argument("--jianying-no-bundle-media", action="store_true",
                    help="do NOT copy media into the draft — reference in place (only if 剪映 can read those paths; macOS 剪映 usually cannot)")
    args = ap.parse_args()
    work_dir = Path(args.work_dir)
    if args.burn_subtitles is not None:
        CONFIG["burn_subtitles"] = args.burn_subtitles
    if args.source_video:
        CONFIG["source_video"] = args.source_video
        CONFIG["source_video_explicit"] = True
    else:
        # SOURCE_VIDEO is an ambient env var in lib.CONFIG. Do not let a stale
        # shell value silently bind full-mode/direct timeline.json or JianYing
        # exports to an unrelated original; cut mode must pass --source-video.
        CONFIG["source_video"] = ""
        CONFIG["source_video_explicit"] = False
    if args.export_jianying:
        CONFIG["export_jianying"] = True
    if args.jianying_bundle_media:
        CONFIG["jianying_bundle_media"] = True
    if args.jianying_no_bundle_media:
        CONFIG["jianying_bundle_media"] = False
    _preflight_burn_subtitles()  # fail before the render if burn-in is on but ffmpeg lacks libass
    tts_meta = Path(args.tts_meta) if args.tts_meta else work_dir / "tts_meta.json"
    tts_segments = json.loads(tts_meta.read_text(encoding="utf-8"))["segments"]
    output_path = work_dir / "output.mp4"
    assemble_video(args.video, tts_segments, work_dir, output_path)
    stem = args.recap_stem or Path(args.video).stem
    base = Path(args.output_dir) if args.output_dir else work_dir.parent
    base.mkdir(parents=True, exist_ok=True)
    final_output = _resolve_final_output(base, stem)
    shutil.copy2(str(output_path), str(final_output))
    manifest = _assembly_manifest_payload(
        args.video, tts_segments, work_dir, output_path,
        tts_meta_path=tts_meta, final_output=final_output,
    )
    _write_assembly_manifest(work_dir, manifest)
    log(f"组装完成: {final_output}")

    # OPTIONAL, decoupled: export a 剪映 draft from the timeline (lazy import; never
    # required by the core render path).
    if CONFIG.get("export_jianying"):
        _maybe_export_jianying(work_dir, args.jianying_out, stem)

    print(json.dumps({"status": "assembled", "output": str(final_output), "work_dir": str(work_dir)},
                     ensure_ascii=False))


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


if __name__ == "__main__":
    main()
