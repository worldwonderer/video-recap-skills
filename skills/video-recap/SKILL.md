---
name: video-recap
description: >
 Generate a Chinese-narration recap video from an input video, end to end. Use when the user
 gives a video file (.mp4 / .mov / .mkv / .webm) and asks to add narration, generate voiceover,
 dub, summarize, or produce a recap (短剧 / 电视剧 / 电影 / 纪录片 / 科普). Orchestrates the
 video-* skill bundle: understanding → (agent writes narration) → cut → voiceover → assemble.
 触发词: 视频解说, 视频旁白, 生成解说, 视频recap, video recap, voiceover, narration, auto-dub, recap.
---

## What this is

A thin orchestrator over five independent, self-contained skills (each in `skills/`, sharing only
JSON/MP4 artifacts in a `work_dir` — no shared code):

```
video-understanding ─▶ (agent writes narration.json per video-script) ─▶ [video-cut] ─▶ video-voiceover ─▶ video-assemble
```

It is resume-safe: rerun the same command after writing `narration.json` to continue.
Phase B validates `recap_run_manifest.json` so an old `work_dir` from another source video or
different run settings is rejected instead of silently reusing stale narration. Understanding
artifacts are reused only when their provenance matches. For per-stage detail, read each skill's own SKILL.md.

## Install / env

```bash
# ffmpeg: brew install ffmpeg | apt install ffmpeg | choco install ffmpeg
export MIMO_API_KEY=***          # ONE key drives ASR + VLM + TTS (all MiMo)
```

The whole pipeline runs on ffmpeg + a single MiMo key: ASR (`mimo-v2.5-asr`), VLM (`mimo-v2.5`),
TTS (`mimo-v2.5-tts`). `tp-*` Token Plan keys default to the cn cluster (`MIMO_TOKEN_PLAN_CLUSTER`).
Optional MiMo scene-chunk video understanding: `--mimo-video-overview`.

Overridable defaults (zero-config otherwise): see `references/config-playbook.md`.

## Use

### 0. Research first (recommended)

If you can identify the source (show, film, topic), research it **before** analyzing and write
`work_dir/background_research.json` (see `video-understanding/references/research-guide.md`).
video-understanding folds it into the VLM context, so scene analysis can name characters and read
scenes with plot knowledge instead of labelling everyone "黑衣男子". Skip it when you can't research.

### 1. Analyze → pause for narration

```bash
python3 scripts/recap.py <video> --work-dir <work_dir> --context "背景"
```

Runs video-understanding (using `background_research.json` if you wrote it), writes
`agent_narration_brief.md`, and pauses. Then **write `work_dir/narration.json`** following the
**video-script** skill (read the brief first).
Cut mode (`--edit-mode cut --target-duration 10m`) also requires `clip_plan.json`.

### 2. Continue → produce the recap

Rerun the **same command** (narration.json now exists):

```bash
python3 scripts/recap.py <video> --work-dir <work_dir>          # [--edit-mode cut] [--no-burn-subtitles]
```

This validates the narration, (cut: builds `edited_source.mp4`), synthesizes the voiceover, and
assembles `recap_<name>.mp4`.

### Self-check

```bash
python3 scripts/recap.py --doctor
```

## Output

- `recap_<video>.mp4` — final video · `subtitles.srt` / `.ass` — subtitles
- `work_dir/` — all intermediate artifacts (the inter-skill contract; see `references/data-schema.md`)

## Options (passed through to the stage skills)
`--context`, `--scene-threshold`, `--style`, `--edit-mode {full,cut}`, `--target-duration`,
`--skip-asr`, `--mimo-video-overview`, `--consolidate`, `--consolidate-asr`, `--mimo-tts-voice`,
`--no-burn-subtitles` (burn is on by default), `--output-dir`.

## What this skill does NOT do
- Does NOT write narration.json / clip_plan.json — the agent authors those (see the video-script skill).
- Does NOT hard-block on the narration review (advisory; validate.py is the hard gate).
- Is NOT an unattended scheduler — it is human-in-the-loop and posts to no channel.
- Shares NO code between stage skills — they communicate only through work_dir artifacts.
