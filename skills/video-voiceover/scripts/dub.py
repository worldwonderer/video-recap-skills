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
import unicodedata
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
DUB_FAST_SPEECH_CPS = 7.0
DUB_TRIM_RISK_CPS = 7.0

DUB_SCHEMA_VERSION = 1
# Tolerate ~1-frame rounding in agent-estimated timings so the gate blocks only GENUINE
# errors, not rounding noise (the old render clamped such cases via min(slot_end, nxt)).
DUB_TIMING_EPS = 0.05
DUB_LINT_SCHEMA = {
    "schema_version": DUB_SCHEMA_VERSION,
    "artifact": "dub_lint.json",
    "fields": {
        "verdict": "PASS|FAIL",
        "issues": "[{severity, code, line, message, start, end}]",
        "summary": "{lines, errors, warnings, max_chars_per_second, trim_risk_lines}",
    },
}
DUB_REVIEW_SCHEMA = {
    "schema_version": DUB_SCHEMA_VERSION,
    "artifact": "dub_review.json",
    "fields": {
        "verdict": "PASS|REVISE|FAIL",
        "checks": "{faithful_to_source, spoken_chinese, speaker_tone, timing_fit, platform_fit}",
        "highest_return_edits": "[{line, start, issue, suggestion}]",
        "notes": "human/agent review notes; script-level output is deterministic pre-review guidance",
    },
}
DUB_ARTIFACT_SCHEMAS = {
    "dub_transcript.json": {
        "schema_version": DUB_SCHEMA_VERSION,
        "shape": {"video": "path", "duration": "seconds", "windows": [{"start": 0.0, "end": 6.0, "text": "English ASR"}]},
    },
    "dub_script.json": {
        "schema_version": DUB_SCHEMA_VERSION,
        "shape": [{"start": 0.0, "end": 2.4, "zh": "中文台词"}],
    },
    "dub_manifest.json": {
        "schema_version": DUB_SCHEMA_VERSION,
        "shape": {"video": "path", "duration": "seconds", "lines": [{"start": 0.0, "end": 2.4, "zh": "中文台词", "fitted_wav": "path", "fitted_dur": 1.8, "room": 2.4}]},
    },
    "dub_lint.json": DUB_LINT_SCHEMA,
    "dub_review.json": DUB_REVIEW_SCHEMA,
}


def _chars_per_second(text, room):
    room = max(float(room or 0.0), 0.001)
    effective = 0
    for ch in str(text or ""):
        if ch.isspace():
            continue
        cat = unicodedata.category(ch)
        if cat.startswith("P") or cat.startswith("S"):
            continue
        effective += 1
    return effective / room


def _issue(severity, code, line, message, start=None, end=None):
    item = {"severity": severity, "code": code, "line": line, "message": message}
    if start is not None:
        item["start"] = round(float(start), 3)
    if end is not None:
        item["end"] = round(float(end), 3)
    return item


def _normalize_dub_script(script):
    if not isinstance(script, list):
        raise ValueError("dub_script.json must be a list")
    lines = []
    for i, item in enumerate(script):
        if not isinstance(item, dict):
            lines.append({"_input_index": i, "start": None, "end": None, "zh": ""})
            continue
        start = item.get("start")
        end = item.get("end")
        try:
            start = float(start)
        except (TypeError, ValueError):
            start = None
        try:
            end = float(end) if end is not None else None
        except (TypeError, ValueError):
            end = None
        lines.append({"_input_index": i, "start": start, "end": end,
                      "zh": str(item.get("zh", "")).strip()})
    return sorted(lines, key=lambda x: (float("inf") if x["start"] is None else x["start"], x["_input_index"]))


def lint_dub_script(script, duration, work_dir=None):
    """Deterministic hard gate for dub_script.json before expensive voiceclone render."""
    duration = float(duration)
    issues = []
    if not isinstance(script, list):
        # A malformed (non-list) script is a hard FAIL, not a crash: keep the "fail fast,
        # see dub_lint.json" promise so the agent gets a clean message instead of a traceback.
        bad = _issue("error", "script_not_a_list", None, "dub_script.json must be a JSON array of {start,end,zh}")
        report = {
            "schema_version": DUB_SCHEMA_VERSION,
            "verdict": "FAIL",
            "blocking": True,
            "errors": [bad],
            "issues": [bad],
            "summary": {"lines": 0, "errors": 1, "warnings": 0, "max_chars_per_second": 0.0, "trim_risk_lines": []},
        }
        if work_dir is not None:
            _write_json(Path(work_dir) / "dub_lint.json", report)
        return report
    lines = _normalize_dub_script(script)
    max_cps = 0.0
    trim_risk = []
    for i, ln in enumerate(lines):
        start, end, zh = ln["start"], ln["end"], ln["zh"]
        line_no = ln["_input_index"]
        nxt = lines[i + 1]["start"] if i + 1 < len(lines) else duration
        if start is None:
            issues.append(_issue("error", "missing_start", line_no, "line start must be a number"))
            continue
        out_of_range = start < -DUB_TIMING_EPS or start > duration + DUB_TIMING_EPS
        if end is not None and (end <= start or end > duration + DUB_TIMING_EPS):
            out_of_range = True
        timing_valid = not out_of_range
        if out_of_range:
            issues.append(_issue("error", "time_out_of_range", line_no, "line timing must be within video duration and end must be > start", start, end))
        if not zh:
            issues.append(_issue("error", "empty_translation", line_no, "zh must not be empty", start, end))
        if (i + 1 < len(lines) and end is not None and lines[i + 1]["start"] is not None
                and end > lines[i + 1]["start"] + DUB_TIMING_EPS):
            issues.append(_issue("error", "overlap", line_no, "line end overlaps the next line start", start, end))
        slot_end = end if end is not None else nxt
        if timing_valid and start is not None and slot_end is not None:
            room = max(0.0, min(slot_end, nxt if nxt is not None else slot_end) - start)
            cps = _chars_per_second(zh, room)
            max_cps = max(max_cps, cps)
            if room < 0.4:
                issues.append(_issue("error", "room_too_short", line_no, "line has less than 0.4s room for speech", start, end))
            elif zh and cps >= DUB_FAST_SPEECH_CPS:
                issues.append(_issue("warning", "fast_speech", line_no, f"translation is dense ({cps:.1f} chars/s)", start, end))
            if zh and cps >= DUB_TRIM_RISK_CPS:
                trim_risk.append(line_no)
                issues.append(_issue("warning", "trim_risk", line_no, "likely to be compressed hard or trimmed by render", start, end))
    error_items = [x for x in issues if x["severity"] == "error"]
    priority = {"empty_translation": 0, "overlap": 1, "time_out_of_range": 2}
    error_items = sorted(error_items, key=lambda x: (priority.get(x["code"], 99), x.get("line", 0)))
    warnings = sum(1 for x in issues if x["severity"] == "warning")
    report = {
        "schema_version": DUB_SCHEMA_VERSION,
        "verdict": "FAIL" if error_items else "PASS",
        "blocking": bool(error_items),
        "errors": error_items,
        "issues": issues,
        "summary": {
            "lines": len(lines),
            "errors": len(error_items),
            "warnings": warnings,
            "max_chars_per_second": round(max_cps, 2),
            "trim_risk_lines": trim_risk,
        },
    }
    if work_dir is not None:
        _write_json(Path(work_dir) / "dub_lint.json", report)
    return report


def build_dub_review(script, transcript, lint=None):
    """Script-level review scaffold: deterministic timing/naturalness signals for agent review."""
    duration = float(transcript.get("duration", 0.0))
    lint = lint or lint_dub_script(script, duration)
    try:
        lines = _normalize_dub_script(script)
    except ValueError:
        lines = []  # non-list script already FAILs lint; review is just a scaffold here
    edits = []
    for issue in lint.get("issues", []):
        if issue["severity"] in {"error", "warning"}:
            edits.append({
                "line": issue.get("line"),
                "start": issue.get("start"),
                "issue": f"{issue['code']}: {issue['message']}",
                "suggestion": "缩短译文、调整 start/end，或回到 transcript 核对原句边界。",
            })
    verdict = "FAIL" if lint.get("verdict") == "FAIL" else ("REVISE" if edits else "PASS")
    return {
        "schema_version": DUB_SCHEMA_VERSION,
        "verdict": verdict,
        "checks": {
            "faithful_to_source": "needs_agent_review",
            "spoken_chinese": "REVISE" if any(i.get("code") == "fast_speech" for i in lint.get("issues", [])) else "PASS",
            "speaker_tone": "needs_agent_review",
            "timing_fit": "FAIL" if lint.get("verdict") == "FAIL" else ("REVISE" if edits else "PASS"),
            "platform_fit": "needs_agent_review",
        },
        "highest_return_edits": edits[:8],
        "notes": "Deterministic script-level review; an agent/human should fill semantic fidelity and tone judgments against dub_transcript.json.",
        "coverage": {"transcript_windows": len(transcript.get("windows", [])), "script_lines": len(lines)},
    }


def _write_json(path, payload):
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


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
    # Mechanical hard gate BEFORE any voiceclone spend: empty/overlapping/out-of-range lines
    # cannot produce a publishable dub, so fail fast instead of paying for a broken render.
    lint = lint_dub_script(script, duration)
    _write_json(work / "dub_lint.json", lint)
    review = build_dub_review(script, transcript, lint)
    _write_json(work / "dub_review.json", review)
    if lint["verdict"] != "PASS":
        raise SystemExit(f"[dub] dub_script.json failed lint; see {work / 'dub_lint.json'}")
    lines = [{"start": ln["start"], "end": ln["end"], "zh": ln["zh"]}
             for ln in _normalize_dub_script(script) if ln["zh"]]
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
        json.dumps({"schema_version": DUB_SCHEMA_VERSION, "video": str(video), "duration": duration, "lines": lines},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"[dub:render] done → {out_video}")
    print(json.dumps({"status": "dubbed", "output": str(out_video), "lines": len(lines)},
                     ensure_ascii=False))


def stage_lint(work):
    transcript = json.loads((work / "dub_transcript.json").read_text(encoding="utf-8"))
    script = json.loads((work / "dub_script.json").read_text(encoding="utf-8"))
    lint = lint_dub_script(script, transcript["duration"])
    path = _write_json(work / "dub_lint.json", lint)
    print(json.dumps({"status": "dub_linted", "verdict": lint["verdict"], "issues": len(lint["issues"]), "dub_lint": str(path)}, ensure_ascii=False))
    return lint


def stage_review(work):
    transcript = json.loads((work / "dub_transcript.json").read_text(encoding="utf-8"))
    script = json.loads((work / "dub_script.json").read_text(encoding="utf-8"))
    lint = lint_dub_script(script, transcript["duration"])
    _write_json(work / "dub_lint.json", lint)
    review = build_dub_review(script, transcript, lint)
    path = _write_json(work / "dub_review.json", review)
    print(json.dumps({"status": "dub_reviewed", "verdict": review["verdict"], "edits": len(review["highest_return_edits"]), "dub_review": str(path)}, ensure_ascii=False))
    return review


def print_schemas():
    print(json.dumps(DUB_ARTIFACT_SCHEMAS, ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser(
        description=(
            "English→Chinese dub workflow. Artifacts: dub_transcript.json, "
            "agent-authored dub_script.json, dub_lint.json, dub_review.json, dub_manifest.json. "
            "Uses MiMo ASR plus mimo-v2.5-tts-voiceclone; ordinary narration TTS config is separate."
        )
    )
    ap.add_argument("--stage", choices=["prepare", "lint", "review", "render"], required=False,
                    help="prepare ASR brief; lint/review dub_script.json; render writes lint/review before voiceclone")
    ap.add_argument("--video", required=False, help="source video (required for prepare/render)")
    ap.add_argument("--work-dir", required=False, help="work directory containing dub artifacts")
    ap.add_argument("--asr-window", type=float, default=6.0)
    ap.add_argument("--ref-start", type=float, default=2.0)
    ap.add_argument("--ref-dur", type=float, default=10.0)
    ap.add_argument("--print-schema", action="store_true",
                    help="print JSON schemas for dub_transcript/script/lint/review/manifest and exit")
    args = ap.parse_args()
    if args.print_schema:
        print_schemas()
        return
    if not args.stage:
        ap.error("--stage is required unless --print-schema is used")
    if not args.work_dir:
        ap.error("--work-dir is required")
    work = Path(args.work_dir)
    if args.stage in {"prepare", "render"} and not args.video:
        ap.error("--video is required for prepare/render")
    video = Path(args.video) if args.video else None
    if args.stage == "prepare":
        stage_prepare(video, work, args.asr_window, args.ref_start, args.ref_dur)
    elif args.stage == "lint":
        stage_lint(work)
    elif args.stage == "review":
        stage_review(work)
    else:
        stage_render(video, work, args.ref_start, args.ref_dur)


if __name__ == "__main__":
    main()
