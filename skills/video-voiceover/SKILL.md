---
name: video-voiceover
user-invocable: false
description: >
 Synthesize Chinese narration audio (TTS voiceover) from a timestamped narration.json.
 Use to turn a written narration script into per-segment speech audio, with MiMo TTS
 (mimo-v2.5-tts), dynamic speed fitting, and loudness handling. Part of the video-recap bundle:
 consumes narration.json (or an explicit narration_mapped.json), produces tts_segments + tts_meta.json.
 触发词: 配音, 语音合成, TTS, 解说配音, voiceover, text to speech, 旁白配音.
---

## What this does

Reads a timestamped narration script and synthesizes one audio clip per segment, fitting speech
to each segment's time slot (dynamic rate), then records placement metadata. The only engine is
MiMo TTS (`mimo-v2.5-tts`).

## Requirements

```bash
export MIMO_API_KEY=***         # MiMo TTS (or a TTS-specific MIMO_TTS_API_KEY)
```

## Input contract

`work_dir/narration.json` (or an explicit `work_dir/narration_mapped.json` in cut mode) — segments with
`start` / `end` / `narration` (+ optional `pause_after_ms`, `overlaps_speech`). Times are the
**output-timeline** seconds the audio will be placed at.

## Run

```bash
python3 scripts/voiceover.py --work-dir <work_dir> --narration <narration.json> [--mimo-voice 冰糖]
```

For direct one-off use, omitting `--narration` reads `work_dir/narration.json`.
Pass `--narration work_dir/narration_mapped.json` explicitly for cut-mode output;
the video-recap orchestrator always passes the intended file.

## Output contract

- `tts_segments/*.wav` — one synthesized clip per narration segment.
- `tts_meta.json` — `{segments: [...], engine, narration}` where each segment carries its
  `audio_path`, timing, `pause_after_ms`, and placement fields consumed by **video-assemble**.

## Notes
- Re-runs safely reuse only matching per-segment audio; edited narration or TTS settings regenerate the affected WAVs.
- `TTS_WORKERS`, `TTS_TIMEOUT`, `TTS_RETRIES`, `ALLOW_PARTIAL_TTS` tune throughput/robustness.

## What this skill does NOT do
- Does NOT write or edit narration text.
- Does NOT mux, duck, or render subtitles — that is video-assemble.
- Does NOT analyze the video or choose timestamps — it voices the segments it is given.
