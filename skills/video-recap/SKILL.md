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

It is **stateless**: rerun the same command after writing `narration.json` to continue.
Understanding artifacts are reused when fresh. For per-stage detail, read each skill's own SKILL.md.

## Install / env

```bash
brew install ffmpeg && pip3 install edge-tts
export OPENAI_API_KEY=***                       # frame VLM (or MIMO_API_KEY for MiMo)
export OPENAI_MODEL=doubao-seed-2-0-lite-260428
```

MiMo (optional): `MIMO_API_KEY` enables MiMo scene-chunk video understanding (`--mimo-video-overview`)
and is preferred for TTS. `tp-*` Token Plan keys default to the cn cluster (`MIMO_TOKEN_PLAN_CLUSTER`).

Overridable defaults (zero-config otherwise): see `references/config-playbook.md`.

## Use

### 1. Analyze → pause for narration

```bash
python3 scripts/recap.py <video> --work-dir <work_dir> --context "背景"
```

Runs video-understanding, writes `agent_narration_brief.md`, and pauses. Then **write
`work_dir/narration.json`** following the **video-script** skill (read the brief first).
Cut mode (`--edit-mode cut --target-duration 10m`) also requires `clip_plan.json`.

### 2. Continue → produce the recap

Rerun the **same command** (narration.json now exists):

```bash
python3 scripts/recap.py <video> --work-dir <work_dir>          # [--edit-mode cut] [--burn-subtitles]
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
`--skip-asr`, `--mimo-video-overview`, `--voice`, `--mimo-tts-voice`, `--tts-engine`,
`--burn-subtitles`, `--output-dir`.

## What this skill does NOT do
- Does NOT write narration.json / clip_plan.json — the agent authors those (see the video-script skill).
- Does NOT hard-block on the narration review (advisory; validate.py is the hard gate).
- Is NOT an unattended scheduler — it is human-in-the-loop and posts to no channel.
- Shares NO code between stage skills — they communicate only through work_dir artifacts.
