---
name: video-assemble
description: >
 Assemble a final recap video: mux narration audio over the source video, duck the original
 audio under the narration, render subtitles (SRT/ASS, optionally burned in), and loudness-
 normalize. Use as the last stage of the video-recap bundle. Consumes the source video +
 tts_meta.json (+ narration placement); produces recap_<name>.mp4 + subtitles.srt/.ass.
 触发词: 视频合成, 混音, 字幕, 压字幕, assemble video, mux, ducking, subtitles, 成片.
---

## What this does

1. Mixes the narration audio segments onto the source video at their placed times.
2. **Ducks** the original audio under narration (fixed / sidechain / zone modes).
3. Renders **subtitles** from the narration placement → `subtitles.srt` (+ `subtitles.ass`,
   burned in with `--burn-subtitles`).
4. Optional final **loudness normalization** to a target LUFS.

## Input contract

- `<video>` — the source video (the original, or `edited_source.mp4` in cut mode).
- `work_dir/tts_meta.json` — `{segments: [...]}` from **video-voiceover** (each segment carries
  `audio_path`, timing, `pause_after_ms`, and `overlaps_speech`/placement used for ducking + subtitles).

## Run

```bash
python3 scripts/assemble.py <video> --work-dir <work_dir> \
  [--recap-stem <name>] [--output-dir <dir>] [--burn-subtitles]
```

## Output contract

- `recap_<stem>.mp4` — the final recap video (written to `--output-dir` or `work_dir`'s parent).
- `work_dir/output.mp4` — the in-place render.
- `subtitles.srt` — narration subtitles; `subtitles.ass` when `--burn-subtitles` is used.

## Notes
- Subtitle look: `SUBTITLE_FONT_SIZE`, `SUBTITLE_MARGIN_V`, `SUBTITLE_MAX_CHARS`, etc.
- Ducking / loudness: `DUCKING_MODE`, `ZONE_DUCKING_VOLUME`, `FINAL_LOUDNORM`, `TARGET_LUFS`.
- Burning subtitles requires an ffmpeg with `subtitles`/libass support.

## What this skill does NOT do
- Does NOT generate narration or synthesize TTS.
- Does NOT re-transcribe or alter timing decisions — it consumes placement from tts_meta.json.
- Burning subtitles is opt-in (`--burn-subtitles`); it does not re-encode unless asked.
