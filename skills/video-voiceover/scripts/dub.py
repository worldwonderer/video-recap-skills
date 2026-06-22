"""English→Chinese dubbing — the render engine behind `recap.py --edit-mode dub`.

Invoked by the orchestrator (NOT run by hand). It replaces the original English speech with a
faithful Chinese translation spoken in the ORIGINAL speaker's cloned voice
(`mimo-v2.5-tts-voiceclone`, same MIMO_API_KEY, pure stdlib + ffmpeg, no GPU). This differs
from recap/解说, which overlays Chinese commentary on ducked original audio.

Division of labour (the same as recap): CODE does only the mechanical parts; the AGENT does all
the judgment. So there are NO text heuristics here (no sentence-splitting, hook-dedup, or junk
filters) — those are exactly the things an LLM does better, and trying to do them in code is
brittle and case-by-case. Two stages around an agent-authored pause:

  --stage prepare : extract audio → English ASR (timed windows) → pull one reference clip →
                    write dub_transcript.json + dub_brief.md, then stop.
  --stage render  : read the agent's dub_script.json ([{"start", "zh"}] on the source timeline)
                    → clone the original voice per line → time-fit each to its slot (anchored at
                    its start; only compress when it would overrun the next line — never a global
                    speed-up, so the voice never finishes ahead of the picture) → full-replace
                    the audio track → mux → dub_<name>.mp4.
"""
import argparse
import base64
import json
import re
import wave
from pathlib import Path

from lib import (
    CONFIG,
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


# ── ffmpeg / wav helpers ─────────────────────────────────────────────

def _ffmpeg_extract_wav(video, out_wav, sr=16000):
    run_cmd(["ffmpeg", "-y", "-i", str(video), "-vn", "-ar", str(sr), "-ac", "1", str(out_wav)])


def _cut_wav(src_wav, out_wav, start, dur, sr=16000):
    run_cmd(["ffmpeg", "-y", "-i", str(src_wav), "-ss", str(start), "-t", str(dur),
             "-ar", str(sr), "-ac", "1", str(out_wav)])


def _atempo_chain(factor):
    """ffmpeg atempo only accepts 0.5–2.0 per stage; chain for larger factors."""
    factor = max(0.5, factor)
    stages, remaining = [], factor
    while remaining > 2.0:
        stages.append("atempo=2.0")
        remaining /= 2.0
    stages.append(f"atempo={remaining:.4f}")
    return ",".join(stages)


def _wav_frames(path):
    with wave.open(str(path), "rb") as w:
        return w.getframerate(), w.getnchannels(), w.readframes(w.getnframes())


# ── ASR (timed windows) ──────────────────────────────────────────────

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
    return re.sub(r"<[^>]{1,20}>", "", text).strip()  # strip ASR markers like "<chinese>"


def _asr_windows(audio_wav, segs_dir, duration, window):
    """Transcribe fixed-time windows → [{start, end, text}]. Coarse timing anchors for the agent;
    the agent does the sentence/segment judgment, not this code."""
    windows = []
    start, idx = 0.0, 0
    while start < duration:
        end = min(start + window, duration)
        seg_wav = segs_dir / f"asr_{idx:03d}.wav"
        _cut_wav(audio_wav, seg_wav, start, end - start)
        text = _run_asr(seg_wav)
        if text:
            windows.append({"start": round(start, 2), "end": round(end, 2), "text": text})
        log(f"  ASR {start:.0f}-{end:.0f}s: {len(text)} chars")
        start, idx = end, idx + 1
    return windows


# ── clone TTS + time-fit + mix ───────────────────────────────────────

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


def _time_fit(raw_wav, fitted_wav, room_seconds):
    """Anchor-at-start fit: only compress when the dub would overrun `room_seconds` (the gap until
    the next line). Never globally speed up; a short dub keeps the natural pause. Returns the
    fitted duration."""
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
    run_cmd(["ffmpeg", "-y", "-i", str(raw_wav),
             "-filter:a", f"{_atempo_chain(ATEMPO_CAP)},atrim=0:{room_seconds:.3f},"
                          f"afade=t=out:st={max(0, room_seconds - 0.15):.3f}:d=0.15",
             "-ar", str(CLONE_SR), "-ac", "1", str(fitted_wav)])
    return get_video_duration(fitted_wav)


def _build_dub_track(lines, duration, out_wav):
    canvas = bytearray(int(duration * CLONE_SR) * 2)  # 16-bit mono silence
    for ln in lines:
        fw = ln.get("fitted_wav")
        if not fw or not Path(fw).exists():
            continue
        sr, ch, frames = _wav_frames(fw)
        if sr != CLONE_SR or ch != 1:
            continue
        off = int(ln["start"] * CLONE_SR) * 2
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


# ── stages ───────────────────────────────────────────────────────────

def _ref_window(duration, ref_start, ref_dur):
    start = max(0.0, min(ref_start, max(0.0, duration - 2.0)))
    return start, max(2.0, min(ref_dur, duration - start))


def stage_prepare(video, work, asr_window, ref_start, ref_dur):
    work.mkdir(parents=True, exist_ok=True)
    segs_dir = work / "dub_asr"
    segs_dir.mkdir(exist_ok=True)
    duration = get_video_duration(video)
    log(f"[dub:prepare] video {duration:.1f}s")

    audio_wav = work / "dub_source.wav"
    _ffmpeg_extract_wav(video, audio_wav)
    windows = _asr_windows(audio_wav, segs_dir, duration, asr_window)
    if not windows:
        raise SystemExit("[dub] no speech transcribed")
    log(f"[dub:prepare] {len(windows)} ASR windows")

    rs, rd = _ref_window(duration, ref_start, ref_dur)
    _cut_wav(audio_wav, work / "dub_reference.wav", rs, rd)

    (work / "dub_transcript.json").write_text(
        json.dumps({"video": str(video), "duration": duration, "windows": windows},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    (work / "dub_brief.md").write_text(_brief_md(windows, duration), encoding="utf-8")
    print(json.dumps({"status": "dub_prepared", "windows": len(windows),
                      "brief": str(work / "dub_brief.md")}, ensure_ascii=False))


def _brief_md(windows, duration):
    lines = [
        "# 配音翻译任务（dub）",
        "",
        f"视频时长 {duration:.1f}s。下面是英文原声的分窗转写（时间戳偏粗，仅作锚点）。",
        "把它**忠实翻译并配音**，写入 `dub_script.json`——核心是**和原视频节奏一致**，不是做解说、"
        "也不是做精简版。",
        "",
        "要点：",
        "1. 逐句忠实翻译，原声说什么就配什么；**不要删内容、不要合并、不要自作主张去掉开场 hook**；"
        "原声重复，配音也跟着重复。",
        "2. 每句放在它在原声里被说出的时间，`start`/`end` 取那句话的起止——这样配音才跟着原声节奏走"
        "（该停顿处自然留白）。",
        "3. 译文忠实精简，能在自己的 `start`→`end` 区间内用正常语速说完（约 5 字/秒）。",
        "4. 只有完全被截断、没法说完整的残句，才取其中说得完整的部分。",
        "",
        "输出 `dub_script.json` = `[{\"start\": 起秒, \"end\": 止秒, \"zh\": \"中文台词\"}, ...]`，按 start "
        "升序，[start,end] 是该句在原声里的时间区间，相邻句不要重叠。",
        "",
        "## 英文原声转写（按时间窗）",
        "",
        "| 起–止 | 英文 |",
        "|---|---|",
    ]
    for w in windows:
        lines.append(f"| {w['start']}–{w['end']}s | {w['text'].replace('|', '/')} |")
    return "\n".join(lines) + "\n"


def stage_render(video, work, ref_start, ref_dur):
    transcript = json.loads((work / "dub_transcript.json").read_text(encoding="utf-8"))
    duration = transcript["duration"]
    script = json.loads((work / "dub_script.json").read_text(encoding="utf-8"))
    lines = sorted(({"start": float(d["start"]),
                     "end": float(d["end"]) if d.get("end") is not None else None,
                     "zh": str(d.get("zh", "")).strip()}
                    for d in script if str(d.get("zh", "")).strip()),
                   key=lambda x: x["start"])
    if not lines:
        raise SystemExit("[dub] dub_script.json has no lines")

    ref_wav = work / "dub_reference.wav"
    if not ref_wav.exists():
        rs, rd = _ref_window(duration, ref_start, ref_dur)
        _cut_wav(work / "dub_source.wav", ref_wav, rs, rd)
    ref_b64 = base64.b64encode(ref_wav.read_bytes()).decode("ascii")

    tts_dir = work / "dub_tts"
    tts_dir.mkdir(exist_ok=True)
    log("[dub:render] clone-TTS + time-fit per line…")
    for i, ln in enumerate(lines):
        nxt = lines[i + 1]["start"] if i + 1 < len(lines) else duration
        slot_end = ln["end"] if ln["end"] is not None else nxt
        # fit to the original utterance's span (rhythm-faithful), but never overrun the next line
        room = max(0.4, min(slot_end, nxt) - ln["start"])
        raw = tts_dir / f"line_{i:03d}_raw.wav"
        fitted = tts_dir / f"line_{i:03d}.wav"
        _clone_tts(ln["zh"], ref_b64, raw)
        ln["fitted_wav"] = str(fitted)
        ln["fitted_dur"] = round(_time_fit(raw, fitted, room), 2)
        ln["room"] = round(room, 2)
        log(f"  line {i}: {ln['start']:.1f}s fit={ln['fitted_dur']}s/room {ln['room']}s «{ln['zh'][:18]}»")

    dub_wav = work / "dub_track.wav"
    _build_dub_track(lines, duration, dub_wav)
    out_video = work / f"dub_{Path(video).stem}.mp4"
    _mux(video, dub_wav, out_video)
    (work / "dub_manifest.json").write_text(
        json.dumps({"video": str(video), "duration": duration, "lines": lines},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"[dub:render] done → {out_video}")
    print(json.dumps({"status": "dubbed", "output": str(out_video), "lines": len(lines)},
                     ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["prepare", "render"], required=True)
    ap.add_argument("--video", required=True)
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--asr-window", type=float, default=6.0)
    ap.add_argument("--ref-start", type=float, default=2.0)
    ap.add_argument("--ref-dur", type=float, default=10.0)
    args = ap.parse_args()
    video, work = Path(args.video), Path(args.work_dir)
    if args.stage == "prepare":
        stage_prepare(video, work, args.asr_window, args.ref_start, args.ref_dur)
    else:
        stage_render(video, work, args.ref_start, args.ref_dur)


if __name__ == "__main__":
    main()
