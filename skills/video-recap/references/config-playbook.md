# Config playbook (override-only)

The bundle runs **zero-config** with sensible defaults. To change behavior, set the
environment variables below (or pass the noted CLI flags) — they **override** the defaults.
Nothing here is required; this is documentation only. No tool reads a config file, and the
bundle ships no root `CLAUDE.md` (so it never collides with your project/global instructions).

| Concern | Env var / flag | Default | Notes |
|---|---|---|---|
| Chat/VLM model | `OPENAI_MODEL` | `doubao-seed-2-0-lite-260428` | frame VLM + reviewer + consolidate |
| Chat API key | `OPENAI_API_KEY` (or `MIMO_API_KEY`) | — | required for VLM/review/consolidate only |
| TTS engine | `TTS_ENGINE` | `auto` | `auto` prefers MiMo when `MIMO_API_KEY` set, else edge-tts |
| edge voice | `--voice` | `zh-CN-YunxiNeural` | per run |
| MiMo voice | `MIMO_TTS_VOICE` / `--mimo-tts-voice` | `冰糖` | |
| Narration density | `TARGET_SEGMENTS_PER_MINUTE` | `9.6` | min `MIN_SEGMENTS_PER_MINUTE=6.24` |
| Final loudness | `FINAL_LOUDNORM` / `TARGET_LUFS` | `true` / `-14` | end-of-pipeline normalize |
| Style | `--style` | `纪录片` | |
| Edit mode | `EDIT_MODE` / `--edit-mode` | `full` | `full` or `cut` |
| Cut target | `TARGET_DURATION` / `--target-duration` | — | e.g. `10m` (cut mode) |
| Scene threshold | `--scene-threshold` | `0.1` | scene-cut sensitivity |
| VLM workers | `VLM_WORKERS` | `8` | lower to 1 if a proxy/WAF rate-limits |
| Subtitle size | `SUBTITLE_FONT_SIZE` / `SUBTITLE_MARGIN_V` | `42` / `48` | look & placement |
| 整理 / index | `--consolidate` / `--consolidate-asr` | off | build the understanding index (and optionally clean ASR) |

See each stage skill's SKILL.md for the full per-stage option list.
