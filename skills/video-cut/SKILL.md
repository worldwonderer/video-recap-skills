---
name: video-cut
description: >
 Cut a long video down to selected source ranges (montage / clip assembly) and remap
 timestamped narration onto the shortened timeline. Use when an agent has chosen which
 original-video segments to keep (a clip plan) and needs a single concatenated source
 video plus narration mapped to the new (output) time. Part of the video-recap bundle:
 consumes clip_plan.json + the source video, produces edited_source.mp4 +
 narration_mapped.json. 触发词: 视频剪辑, 剪辑式解说, video cut, clip plan, 拼剪.
---

## What this does

Takes an agent-authored **clip plan** (which source ranges to keep, in order) and:
1. Validates + enriches it into `clip_plan_validated.json` (clip ids, source/output times, total duration, overlap checks).
2. Concatenates those source ranges into a single `edited_source.mp4`.
3. Maps narration written in **original-video time** onto the cut **output** timeline → `narration_mapped.json`.

It is stateless: given the same inputs it reproduces the same outputs. Caching is just
"reuse `edited_source.mp4` if it is newer than `clip_plan.json`".

## Input contract

`work_dir/clip_plan.json` — a JSON array, or `{"clips": [...]}`. Each clip:

```json
{"start": 12.0, "end": 28.5, "reason": "inciting incident"}
```

`start`/`end` are **original-video seconds** (aliases `source_start`/`source_end` or `in`/`out` accepted).
Optionally `target_duration` at the object top level (e.g. `"10m"`).

`work_dir/narration.json` (optional) — segments with original-video `start`/`end` + `narration` text.
Each segment may carry `source_clip_id` to disambiguate when overlapping clips are allowed.

## Run

```bash
python3 scripts/cut.py <video> --work-dir <work_dir> \
  [--target-duration 10m] [--clip-padding 0] [--allow-overlap]
```

## Output contract

- `clip_plan_validated.json` — normalized clips with `clip_id`, `source_start/end`, `output_start/end`, `duration`.
- `edited_source.mp4` — the concatenated source video (the new shortened timeline).
- `narration_mapped.json` — narration with `start`/`end` rewritten to output time and `source_clip_id` set.

Downstream (voiceover + assemble) treat `edited_source.mp4` as the video and `narration_mapped.json` as the narration.

## Notes

- Timestamps in `clip_plan.json` and `narration.json` are always **original-video time**; this tool does the source→output remap.
- Overlapping/duplicate source ranges raise an error unless `--allow-overlap` is set; with overlap, narration should set `source_clip_id`.
- Segments that fall outside every kept clip are dropped (logged).
