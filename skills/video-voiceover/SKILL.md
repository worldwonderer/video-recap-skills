---
name: video-voiceover
description: >
 Synthesize Chinese narration audio (TTS voiceover) from a timestamped narration.json.
 Use to turn a written narration script into per-segment speech audio, with edge-tts or
 MiMo TTS, dynamic speed fitting, and loudness handling. Part of the video-recap bundle:
 consumes narration.json (or narration_mapped.json), produces tts_segments + tts_meta.json.
 触发词: 配音, 语音合成, TTS, 解说配音, voiceover, text to speech, 旁白配音.
---

## What this does

Reads a timestamped narration script and synthesizes one audio clip per segment, fitting speech
to each segment's time slot (dynamic rate), then records placement metadata. Engine selection:
`auto` prefers MiMo TTS when `MIMO_API_KEY` is set, else `edge-tts`.

## Requirements

```bash
pip3 install edge-tts          # edge-tts engine
# MiMo TTS (optional, preferred when available):
export MIMO_API_KEY=***         # or MIMO_TTS_API_KEY
```

## Input contract

`work_dir/narration.json` (or `work_dir/narration_mapped.json` in cut mode) — segments with
`start` / `end` / `narration` (+ optional `pause_after_ms`, `overlaps_speech`). Times are the
**output-timeline** seconds the audio will be placed at.

## Run

```bash
python3 scripts/voiceover.py --work-dir <work_dir> \
  [--engine auto|edge-tts|mimo-tts] [--voice zh-CN-YunxiNeural] [--mimo-voice 冰糖]
```

(Defaults to `narration_mapped.json` if present, else `narration.json`.)

## Output contract

- `tts_segments/*.wav` — one synthesized clip per narration segment.
- `tts_meta.json` — `{segments: [...], engine, narration}` where each segment carries its
  `audio_path`, timing, `pause_after_ms`, and placement fields consumed by **video-assemble**.

## Notes
- To re-voice after editing narration, delete `tts_segments/` + `tts_meta.json` and rerun.
- `TTS_WORKERS`, `TTS_TIMEOUT`, `TTS_RETRIES`, `ALLOW_PARTIAL_TTS` tune throughput/robustness.

## What this skill does NOT do
- Does NOT write or edit narration text.
- Does NOT mux, duck, or render subtitles — that is video-assemble.
- Does NOT analyze the video or choose timestamps — it voices the segments it is given.
