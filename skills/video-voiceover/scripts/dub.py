"""English→Chinese video DUBBING (original voice cloned) — v1 pipeline.

This is the producer for the upcoming `edit_mode="dub"`. It is DIFFERENT from recap/解说:
it REPLACES the original speech with a faithful Chinese translation spoken in the
ORIGINAL speaker's cloned timbre (Xiaomi MiMo `mimo-v2.5-tts-voiceclone`), instead of
overlaying commentary on ducked original audio.

Pipeline: extract audio → ASR (windowed, source timing) → translate EN→ZH (timing-aware)
→ pull one reference clip → clone-TTS per segment → time-fit to the SOURCE utterance
window → place on a silent canvas at the source start → mux (full-replace) → loudnorm.

ALIGNMENT (addresses the recap "voice finishes before the video" drift): every line is
ANCHORED to its source-utterance start, and we ONLY speed it up when it would overrun the
room before the next line. No global speed-up is applied (recap's NARRATION_SPEED=1.3 is the
thing that makes the voice end early). A line shorter than its slot simply leaves the
speaker's natural pause, so segments never drift out of sync with the picture.

v1 scope: single speaker, full-replace audio (no background-music separation). The clone
call here will be promoted into voiceover.py's engine dispatch + recap.py edit_mode=dub once
validated; ASR is intentionally reimplemented minimally to keep v1 runnable standalone.
"""
import argparse
import base64
import difflib
import json
import re
import shutil
import wave
from pathlib import Path

from lib import (
    CONFIG,
    api_call,
    get_video_duration,
    log,
    mimo_asr_api_call,
    mimo_tts_api_call,
    run_cmd,
)

CLONE_MODEL = "mimo-v2.5-tts-voiceclone"
CLONE_SR = 24000  # mimo voiceclone returns 24kHz mono PCM16 wav
ASR_MIME = "audio/wav"
ATEMPO_CAP = 2.0  # max compression before we trim instead (atempo>2 also sounds rushed)
CHARS_PER_SEC = 5.0  # rough zh speaking rate, used to size translations to the slot
QC_THRESHOLD = 0.6  # MiMo-ASR round-trip char-similarity below this → re-synthesize the segment
QC_RETRIES = 2  # extra clone attempts when a segment fails QC (clone TTS is nondeterministic)


# ── audio helpers ────────────────────────────────────────────────────

def _ffmpeg_extract_wav(video, out_wav, sr=16000):
    run_cmd(["ffmpeg", "-y", "-i", str(video), "-vn", "-ar", str(sr), "-ac", "1", str(out_wav)])


def _cut_wav(src_wav, out_wav, start, dur, sr=16000):
    run_cmd(["ffmpeg", "-y", "-i", str(src_wav), "-ss", str(start), "-t", str(dur),
             "-ar", str(sr), "-ac", "1", str(out_wav)])


def _atempo_chain(factor):
    """ffmpeg atempo only accepts 0.5–2.0 per stage; chain for larger factors."""
    factor = max(0.5, factor)
    stages = []
    remaining = factor
    while remaining > 2.0:
        stages.append("atempo=2.0")
        remaining /= 2.0
    stages.append(f"atempo={remaining:.4f}")
    return ",".join(stages)


def _wav_frames(path):
    with wave.open(str(path), "rb") as w:
        return w.getframerate(), w.getnchannels(), w.readframes(w.getnframes())


# ── stage 1: ASR (windowed, source timing) ───────────────────────────

def _asr_windows(audio_wav, segs_dir, duration, window):
    """Transcribe fixed-time windows → [{start, end, text}] (English, with punctuation)."""
    windows = []
    start = 0.0
    idx = 0
    while start < duration:
        end = min(start + window, duration)
        seg_wav = segs_dir / f"asr_{idx:03d}.wav"
        _cut_wav(audio_wav, seg_wav, start, end - start)
        text = _run_asr(seg_wav)
        if text:
            windows.append({"start": round(start, 2), "end": round(end, 2), "text": text})
        log(f"  ASR {start:.0f}-{end:.0f}s: {len(text)} chars")
        start = end
        idx += 1
    return windows


def _sentence_segments(windows):
    """Re-segment windowed ASR into COMPLETE sentences with interpolated timing, so the clone
    TTS never receives a mid-sentence fragment (the cause of unstable/garbled audio). Char
    position → source time is interpolated linearly within each window. A trailing sentence
    with no terminal punctuation is the source cut-off and is dropped."""
    full = ""
    spans = []  # (char_start, char_end, t_start, t_end)
    for w in windows:
        t = (w.get("text") or "").strip()
        if not t:
            continue
        cs = len(full)
        full += t
        spans.append((cs, len(full), w["start"], w["end"]))
        full += " "
    if not spans:
        return []

    def time_at(pos):
        pos = max(0, min(pos, len(full)))
        for cs, ce, ts, te in spans:
            if pos < ce:
                if pos <= cs:
                    return ts
                return ts + (pos - cs) / max(1, ce - cs) * (te - ts)
        return spans[-1][3]

    segments = []
    for m in re.finditer(r"[^.!?]*[.!?]+|\S[^.!?]*$", full):
        sent = m.group(0).strip()
        if not sent:
            continue
        complete = bool(re.search(r"[.!?]$", sent))
        segments.append({"start": round(time_at(m.start()), 2),
                         "end": round(time_at(m.end()), 2),
                         "en": sent, "complete": complete})
    while segments and not segments[-1]["complete"]:
        dropped = segments.pop()
        log(f"  [dub] dropped cut-off tail: «{dropped['en'][:40]}»")
    return segments


def _run_asr(wav_path, lang="en"):
    raw = Path(wav_path).read_bytes()
    if not raw:
        return ""
    b64 = base64.b64encode(raw).decode("ascii")
    payload = {
        "model": CONFIG.get("mimo_asr_model", "mimo-v2.5-asr"),
        "messages": [{"role": "user", "content": [
            {"type": "input_audio", "input_audio": {"data": f"data:{ASR_MIME};base64,{b64}"}},
        ]}],
        "asr_options": {"language": lang},
    }
    resp = mimo_asr_api_call(payload)
    try:
        text = str(resp["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        return ""
    # MiMo ASR may emit language markers like "<chinese>"/"<english>"; strip them.
    return re.sub(r"<[^>]{1,20}>", "", text).strip()


# ── stage 2: translate EN→ZH (timing-aware) ──────────────────────────

def _translate(segments):
    """One batched chat call → faithful but concise zh per segment, sized to its slot."""
    items = [{"i": i, "seconds": round(s["end"] - s["start"], 1),
              "max_chars": max(4, int((s["end"] - s["start"]) * CHARS_PER_SEC)), "en": s["en"]}
             for i, s in enumerate(segments)]
    prompt = (
        "你是专业的影视译制翻译。把每条英文台词翻译成自然、口语化、可直接配音的简体中文。\n"
        "规则：\n"
        "1. 每条必须是完整、自洽的句子。若英文在结尾被截断成残句（例如 '...in terms on how to'），"
        "只翻译已说完整的部分，丢弃结尾不完整的残句，绝不要输出像「至于如何」这样的悬空短语"
        "（残句会让配音模型发音紊乱）。\n"
        "2. 译文要能在 `seconds` 秒内用正常语速说完，长度不超过 `max_chars` 个汉字；"
        "宁可意译精简也不要超长，去掉口头禅和冗余，保留原意与语气。\n"
        "3. 若某条没有任何完整内容可翻译，zh 返回空字符串。\n"
        "只返回 JSON 数组，每项 {\"i\": 序号, \"zh\": \"译文\"}，不要解释。\n\n"
        + json.dumps(items, ensure_ascii=False)
    )
    resp = api_call({"model": CONFIG.get("vlm_model", "mimo-v2.5"),
                     "messages": [{"role": "user", "content": prompt}]})
    content = resp["choices"][0]["message"]["content"]
    data = _parse_json_array(content)
    by_i = {int(d["i"]): str(d.get("zh", "")).strip() for d in data if "i" in d}
    for i, s in enumerate(segments):
        s["zh"] = by_i.get(i, "")
    return segments


def _parse_json_array(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
    m = re.search(r"\[.*\]", text, re.DOTALL)
    return json.loads(m.group(0)) if m else []


def _char_similarity(a, b):
    """Char-level similarity of intended vs MiMo-ASR-heard text (punctuation/space ignored)."""
    def norm(x):
        return re.sub(r"[\s，。！？、,.!?]", "", x or "")
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


# ── stage 3: clone-TTS ───────────────────────────────────────────────

def _clone_tts(text, ref_b64, out_wav):
    payload = {
        "model": CLONE_MODEL,
        "messages": [
            {"role": "user", "content": "自然、清晰，保持原说话人的音色与节奏，语气平稳。"},
            {"role": "assistant", "content": text},
        ],
        "audio": {"format": "wav", "voice": f"data:audio/wav;base64,{ref_b64}"},
    }
    resp = mimo_tts_api_call(payload)
    data = resp["choices"][0]["message"]["audio"]["data"]
    Path(out_wav).write_bytes(base64.b64decode(data))


# ── stage 4: time-fit to source window (the alignment fix) ────────────

def _time_fit(raw_wav, fitted_wav, room_seconds):
    """Anchor-at-source-start fit: only compress when the dub would overrun `room_seconds`
    (the gap until the next line). Never globally speed up; a short dub keeps the natural
    pause. Returns the fitted duration."""
    dur = get_video_duration(raw_wav)
    if dur <= 0:
        return 0.0
    if dur <= room_seconds + 0.05:
        run_cmd(["ffmpeg", "-y", "-i", str(raw_wav), "-ar", str(CLONE_SR), "-ac", "1", str(fitted_wav)])
        return dur
    factor = dur / room_seconds
    if factor <= ATEMPO_CAP:
        run_cmd(["ffmpeg", "-y", "-i", str(raw_wav), "-filter:a", _atempo_chain(factor),
                 "-ar", str(CLONE_SR), "-ac", "1", str(fitted_wav)])
        return get_video_duration(fitted_wav)
    # too long even at the cap: compress to cap, then hard-trim to the room with a fade
    run_cmd(["ffmpeg", "-y", "-i", str(raw_wav),
             "-filter:a", f"{_atempo_chain(ATEMPO_CAP)},atrim=0:{room_seconds:.3f},afade=t=out:st={max(0, room_seconds-0.15):.3f}:d=0.15",
             "-ar", str(CLONE_SR), "-ac", "1", str(fitted_wav)])
    return get_video_duration(fitted_wav)


# ── stage 5: build canvas + mux ──────────────────────────────────────

def _build_dub_track(segments, duration, out_wav):
    total = int(duration * CLONE_SR)
    canvas = bytearray(total * 2)  # 16-bit mono silence
    for s in segments:
        fw = s.get("fitted_wav")
        if not fw or not Path(fw).exists():
            continue
        sr, ch, frames = _wav_frames(fw)
        if sr != CLONE_SR or ch != 1:
            continue
        off = int(s["start"] * CLONE_SR) * 2
        end = min(off + len(frames), len(canvas))
        canvas[off:end] = frames[: end - off]
    with wave.open(str(out_wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(CLONE_SR)
        w.writeframes(bytes(canvas))


def _mux(video, dub_wav, out_video):
    run_cmd(["ffmpeg", "-y", "-i", str(video), "-i", str(dub_wav),
             "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
             "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "192k",
             "-shortest", str(out_video)])


# ── orchestration ────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--asr-window", type=float, default=6.0, help="ASR window seconds (smaller = finer alignment)")
    ap.add_argument("--ref-start", type=float, default=2.0)
    ap.add_argument("--ref-dur", type=float, default=10.0)
    args = ap.parse_args()

    video = Path(args.video)
    work = Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    segs_dir = work / "dub_asr"
    segs_dir.mkdir(exist_ok=True)
    tts_dir = work / "dub_tts"
    tts_dir.mkdir(exist_ok=True)

    duration = get_video_duration(video)
    log(f"[dub] video {duration:.1f}s")

    audio_wav = work / "dub_source.wav"
    _ffmpeg_extract_wav(video, audio_wav)

    log("[dub] ASR (English, windowed)…")
    windows = _asr_windows(audio_wav, segs_dir, duration, args.asr_window)
    segments = _sentence_segments(windows)
    if not segments:
        raise SystemExit("[dub] no speech transcribed")
    log(f"[dub] {len(windows)} windows → {len(segments)} sentences")

    log(f"[dub] translate {len(segments)} segments EN→ZH…")
    _translate(segments)

    ref_wav = work / "dub_reference.wav"
    _cut_wav(audio_wav, ref_wav, args.ref_start, args.ref_dur)
    ref_b64 = base64.b64encode(ref_wav.read_bytes()).decode("ascii")

    log("[dub] clone-TTS + time-fit + MiMo-ASR QC per segment…")
    qc_report = []
    for i, s in enumerate(segments):
        zh = s.get("zh")
        if not zh:
            continue
        nxt = segments[i + 1]["start"] if i + 1 < len(segments) else duration
        room = max(0.4, nxt - s["start"])  # never overrun the next line
        fitted = tts_dir / f"seg_{i:03d}.wav"
        best_sim = -1.0
        for attempt in range(QC_RETRIES + 1):
            raw = tts_dir / f"seg_{i:03d}_raw{attempt}.wav"
            cand = tts_dir / f"seg_{i:03d}_cand{attempt}.wav"
            _clone_tts(zh, ref_b64, raw)
            cand_dur = _time_fit(raw, cand, room)
            heard = _run_asr(cand, lang="zh")  # round-trip QC: re-transcribe the synthesized zh
            sim = _char_similarity(zh, heard)
            if sim > best_sim:  # keep the best take across attempts (clone is nondeterministic)
                best_sim = sim
                shutil.copyfile(cand, fitted)
                s.update(fitted_wav=str(fitted), fitted_dur=round(cand_dur, 2),
                         room=round(room, 2), heard=heard, qc_sim=round(sim, 2))
            if sim >= QC_THRESHOLD:
                break
        flag = "" if best_sim >= QC_THRESHOLD else "  ⚠ LOW"
        log(f"  seg {i}: {s['start']:.1f}s qc={best_sim:.2f} fit={s['fitted_dur']}s "
            f"«{zh[:16]}» 听到«{s.get('heard', '')[:16]}»{flag}")
        qc_report.append({"i": i, "start": s["start"], "intended": zh,
                          "heard": s.get("heard", ""), "qc_sim": s.get("qc_sim")})

    dub_wav = work / "dub_track.wav"
    _build_dub_track(segments, duration, dub_wav)

    out_video = work / f"dub_{video.stem}.mp4"
    _mux(video, dub_wav, out_video)

    (work / "dub_manifest.json").write_text(
        json.dumps({"video": str(video), "duration": duration, "segments": segments},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    (work / "dub_qc.json").write_text(
        json.dumps(qc_report, ensure_ascii=False, indent=2), encoding="utf-8")

    low = [r for r in qc_report if (r.get("qc_sim") or 0) < QC_THRESHOLD]
    if low:
        log(f"[dub] ⚠ QC: {len(low)}/{len(qc_report)} segment(s) below {QC_THRESHOLD}: idx {[r['i'] for r in low]}")
    else:
        log(f"[dub] ✓ QC passed: all {len(qc_report)} segments ≥ {QC_THRESHOLD}")
    log(f"[dub] done → {out_video}")
    print(json.dumps({"status": "dubbed", "output": str(out_video),
                      "segments": len(qc_report), "qc_low": [r["i"] for r in low]},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
