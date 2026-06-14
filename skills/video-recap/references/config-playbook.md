# Config playbook (override-only)

The bundle runs **zero-config** with sensible defaults. To change behavior, set the
environment variables below (or pass the noted CLI flags) — they **override** the defaults.
Nothing here is required; this is documentation only. No tool reads a config file, and the
bundle ships no root `CLAUDE.md` (so it never collides with your project/global instructions).

| Concern | Env var / flag | Default | Notes |
|---|---|---|---|
| MiMo API key | `MIMO_API_KEY` | — | **required**; one key drives ASR + VLM + TTS. `tp-*` Token-Plan keys auto-route to the cluster base URL |
| Token-Plan cluster | `MIMO_TOKEN_PLAN_CLUSTER` | `cn` | `cn` / `sgp` / `ams` (only for `tp-*` keys) |
| VLM / chat model | `MIMO_MODEL` | `mimo-v2.5` | frame VLM + reviewer + consolidate |
| ASR model | `MIMO_ASR_MODEL` | `mimo-v2.5-asr` | speech-to-text |
| ASR language | `MIMO_ASR_LANGUAGE` | `auto` | `auto` / `zh` / `en` |
| ASR window | `ASR_SEGMENT_SECONDS` | `30` | smaller → finer dialogue timestamps (stays under MiMo's 10MB base64 cap) |
| TTS model | `MIMO_TTS_MODEL` | `mimo-v2.5-tts` | the only TTS engine |
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
