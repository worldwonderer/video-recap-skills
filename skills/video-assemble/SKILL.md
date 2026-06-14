---
name: video-assemble
user-invocable: false
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
  [--source-video <orig.mp4>] [--export-jianying [--jianying-out <dir>]]
```

## Output contract

- `recap_<stem>.mp4` — the final recap video (written to `--output-dir` or `work_dir`'s parent).
- `work_dir/output.mp4` — the in-place render.
- `subtitles.srt` — narration subtitles; `subtitles.ass` when `--burn-subtitles` is used.
- `timeline.json` — backend-neutral multi-track model (video / original-audio / narration / BGM / subtitle tracks with ducking automation). Always written.
- 剪映 draft folder (`recap_<stem>/draft_content.json` + `draft_info.json` + `draft_meta_info.json`) — only with `--export-jianying`.

## Notes
- Audio is mixed as tracks (like a cut-software timeline): the original audio, an optional BGM bed, and the narration.
- Optional 剪映/JianYing export: `--export-jianying` (or `EXPORT_JIANYING=1`) turns `timeline.json` into an editable 剪映 draft — original clips, separate audio tracks, and volume keyframes for the ducking. Fully decoupled and lazy-imported: the ffmpeg render never depends on it, and 剪映 need not be installed. In cut mode pass `--source-video <orig>` so the draft references the real clips. Point `--jianying-out` at 剪映's drafts root to open it in-app; add `--jianying-bundle-media` to copy media into the draft folder (portable/self-contained). Note: the draft references the un-burned original, so the source's hardcoded subtitles are visible there (mask them in 剪映 if needed).
- Subtitle look: `SUBTITLE_FONT_SIZE`, `SUBTITLE_MARGIN_V`, `SUBTITLE_MAX_CHARS`, etc.
- Ducking / loudness: the original swells to `IDLE_ORIG_VOLUME` in the gaps and ducks to `SPEECH_DUCKING_VOLUME` under narration (`DUCK_FADE_SECONDS` smooths the transition); also `DUCKING_MODE`, `ZONE_DUCKING_VOLUME`, `FINAL_LOUDNORM`, `TARGET_LUFS`.
- BGM (optional): set `BGM_PATH` to any audio file; it loops to length and ducks under narration (`BGM_VOLUME` / `BGM_DUCKING_VOLUME`).
- Burning subtitles requires an ffmpeg with `subtitles`/libass support.

## What this skill does NOT do
- Does NOT generate narration or synthesize TTS.
- Does NOT re-transcribe or alter timing decisions — it consumes placement from tts_meta.json.
- Burning subtitles is opt-in (`--burn-subtitles`); it does not re-encode unless asked.
