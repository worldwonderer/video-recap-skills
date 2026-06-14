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
| Narration speed | `NARRATION_SPEED` | `1.2` | global atempo on the voiceover; default leans snappy for short-form, set `1.0` for long-form/documentary |
| Mask source subs | `MASK_SOURCE_SUBTITLES` / `SOURCE_SUBTITLE_MASK_RATIO` | on / `0.14` | covers burned-in source subtitles (bottom band) so only the recap's subtitles show; set `MASK_SOURCE_SUBTITLES=false` for sources without hardcoded subs |
| Original ducking | `IDLE_ORIG_VOLUME` / `SPEECH_DUCKING_VOLUME` | `0.85` / `0.2` | the original swells to `IDLE` in the gaps between sentences (so the recap never goes dead-air / choppy) and ducks to `SPEECH` under narration. `DUCKING_ORIG_VOLUME` (`0.3`) is only the fallback when beats carry no placement info |
| Duck fade | `DUCK_FADE_SECONDS` | `0.25` | ramp time for each duck transition, so the original fades down/up without clicks |
| Background music | `BGM_PATH` / `BGM_VOLUME` / `BGM_DUCKING_VOLUME` | off / `0.18` / `0.10` | optional looped music bed mixed as its own track; point `BGM_PATH` at any audio file. It ducks to `BGM_DUCKING_VOLUME` under narration |
| Final loudness | `FINAL_LOUDNORM` / `TARGET_LUFS` | `true` / `-14` | end-of-pipeline normalize |
| Style | `--style` | `纪录片` | |
| Edit mode | `EDIT_MODE` / `--edit-mode` | `full` | `full` or `cut` |
| Cut target | `TARGET_DURATION` / `--target-duration` | — | e.g. `10m` (cut mode) |
| Scene threshold | `--scene-threshold` | `0.1` | scene-cut sensitivity |
| VLM workers | `VLM_WORKERS` | `8` | lower to 1 if a proxy/WAF rate-limits |
| Subtitle size | `SUBTITLE_FONT_SIZE` / `SUBTITLE_MARGIN_V` | `42` / `48` | look & placement |
| 整理 / index | `--consolidate` / `--consolidate-asr` | off | build the understanding index (and optionally clean ASR) |
| 剪映 export (optional) | `--export-jianying` / `EXPORT_JIANYING` | off | after rendering, also write a 剪映/JianYing draft from `timeline.json`. Decoupled — the core render never needs it |
| 剪映 draft dir | `JIANYING_DRAFT_DIR` | work_dir | parent folder for the exported draft (point it at 剪映's drafts root to open in-app) |
| 剪映 bundle media | `--jianying-bundle-media` / `JIANYING_BUNDLE_MEDIA` | off | copy referenced media into the draft folder so it is self-contained / portable to another machine |
| Source video | `--source-video` / `SOURCE_VIDEO` | — | original video (cut mode) so `timeline.json` / 剪映 export reference the real source clips instead of the concatenated `edited_source.mp4` |

`video-assemble` always writes `timeline.json` — a backend-neutral multi-track model
(video / original-audio / narration / BGM / subtitle, with ducking automation). The
canonical renderer is ffmpeg; the 剪映 exporter is an optional consumer of the same file.

See each stage skill's SKILL.md for the full per-stage option list.
