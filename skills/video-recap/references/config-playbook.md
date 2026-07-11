# Config playbook (override-only)

The bundle runs **zero-config** with sensible defaults. To change behavior, set the
environment variables below (or pass the noted CLI flags) — they **override** the defaults.
Nothing here is required; this is documentation only. No tool reads a config file, and the
bundle ships no root `CLAUDE.md` (so it never collides with your project/global instructions).
Defaults below are bundle-level defaults unless a note scopes them to a specific stage.

| Concern | Env var / flag | Default | Notes |
|---|---|---|---|
| MiMo API key | `MIMO_API_KEY` | — | **required**; one key drives ASR + VLM + TTS. `tp-*` Token-Plan keys auto-route to the cluster base URL |
| Token-Plan cluster | `MIMO_TOKEN_PLAN_CLUSTER` | `cn` | `cn` / `sgp` / `ams` (only for `tp-*` keys) |
| VLM / chat model | `MIMO_MODEL` | `mimo-v2.5` | frame VLM + reviewer + consolidate |
| ASR model | `MIMO_ASR_MODEL` | `mimo-v2.5-asr` | speech-to-text |
| ASR language | `MIMO_ASR_LANGUAGE` | `auto` | `auto` / `zh` / `en` |
| ASR window | `ASR_SEGMENT_SECONDS` | `15` | smaller → finer dialogue timestamps (stays under MiMo's 10MB base64 cap) |
| TTS model | `MIMO_TTS_MODEL` | `mimo-v2.5-tts` | the only TTS engine |
| MiMo voice | `MIMO_TTS_VOICE` / `--mimo-tts-voice` | `冰糖` | |
| Cloned narration voice | `VOICE_REF` / `--voice-ref` | off | full/cut only; lazily normalize once, then use `mimo-v2.5-tts-voiceclone`; mutually exclusive with `--mimo-tts-voice`; requires authorization and sends the reference to MiMo |
| Narration block coverage | `NARRATION_COVERAGE_TARGET` / `NARRATION_BLOCK_SECONDS` | `0.7` / `9.0` | current block-recap density controls; old `TARGET_SEGMENTS_PER_MINUTE` applies only to legacy single-pass cut mapping reports |
| Narration speed | `NARRATION_SPEED` | `1.15` | global atempo on the voiceover; set `1.0` for long-form/documentary |
| Mask source subs | `MASK_SOURCE_SUBTITLES` / `SOURCE_SUBTITLE_MASK_POLICY` / `SOURCE_SUBTITLE_MASK_RATIO` | off / `off` / `0.14` | masking requires an explicit policy (`opt_in`, `safe`, or `forced`) and burned recap subtitles. Passing measured `--subtitle-y-top/--subtitle-y-bot` opts in automatically. With `--no-burn-subtitles`, the MP4 stays unmasked |
| Measured subtitle band | `SUBTITLE_Y_TOP` / `SUBTITLE_Y_BOT`; `--subtitle-y-top/--subtitle-y-bot` | off | source-frame pixel coordinates; use `tools/measure_subtitle.py` to generate previews and suggestions |
| Subtitle mask look | `SUBTITLE_MASK_OPACITY` / `SOURCE_SUBTITLE_MASK_TIMING` / `SUBTITLE_MASK_PADDING` | `0.6` / `narration` / `4` | enhanced default is a translucent band only while narration plays; use opacity `1` + timing `all` for the legacy solid/full-time look |
| Original ducking | `IDLE_ORIG_VOLUME` / `SPEECH_DUCKING_VOLUME` | `1.0` / `0.2` | the original returns to full-volume `IDLE` in deliberate gaps/original blocks, and ducks to `SPEECH` under narration. Inter-beat gaps shorter than `DUCK_BRIDGE_SECONDS` stay ducked so a single narration block does not swell between sentences. `DUCKING_ORIG_VOLUME` (`0.3`) is only the fallback when beats carry no placement info |
| Foreign source audio | `FOREIGN_SOURCE_AUDIO` | off | set when the original audio is in a language the narration is **not** (e.g. a Japanese drama recapped in Chinese). The under-narration original (`SPEECH_DUCKING_VOLUME` / `ZONE_DUCKING_VOLUME`) drops from `0.2`/`0.12` to `0.05` so the foreign speech doesn't bleed under the narration as 怪音; original-audio gap blocks still play full-volume (`IDLE_ORIG_VOLUME`). Explicit `SPEECH_DUCKING_VOLUME`/`ZONE_DUCKING_VOLUME` still override. Pairs with bring-your-own `user_subtitles.*` for the foreign dialogue |
| Duck fade | `DUCK_FADE_SECONDS` | `0.3` | ramp time for each duck transition, so full-volume original blocks and ducked narration blocks switch without clicks |
| Duck bridge | `DUCK_BRIDGE_SECONDS` | `1.5` | inter-beat gaps shorter than this stay ducked inside one narration block; gaps >= this are treated as intentional original-audio blocks and return to `IDLE_ORIG_VOLUME` |
| Background music | `BGM_PATH` / `BGM_VOLUME` / `BGM_DUCKING_VOLUME` | off / `0.18` / `0.10` | optional looped music bed mixed as its own track; point `BGM_PATH` at any audio file. It ducks to `BGM_DUCKING_VOLUME` under narration |
| Final loudness | `FINAL_LOUDNORM` / `TARGET_LUFS` | `true` / `-14` | end-of-pipeline normalize |
| Output compression | `OUTPUT_CRF` / `OUTPUT_PRESET` / `OUTPUT_MAX_HEIGHT` | `18` / `veryfast` / `0` | x264 re-encode controls, applied whenever the final mux re-encodes (burning subtitles / masking / scaling / `FORCE_VIDEO_REENCODE`). Higher `OUTPUT_CRF` = smaller file/lower quality (18≈visually lossless, 23–26 much smaller); `slow`/`slower` preset shrinks more at the same CRF; `OUTPUT_MAX_HEIGHT>0` downscales the final height (keeps aspect, even width), e.g. `720` to halve 1080p pixels. Subtitles/mask render at native res then downscale, so they stay crisp |
| Style | `--style` | `纪录片` | freeform verbatim guidance passed into the agent brief. The agent synthesizes voice/pacing from this exact text plus evidence; it is not a fixed option list, preset, switch, or finite style taxonomy |
| Edit mode | `EDIT_MODE` / `--edit-mode` | `full` | `full` or `cut` |
| Cut target | `TARGET_DURATION` / `--target-duration` | — | e.g. `10m` (cut mode) |
| Scene threshold | `--scene-threshold` | `0.1` | scene-cut sensitivity |
| Shot-change-aware cut | `SCENE_CUT_SNAP` / `SCENE_CUT_SNAP_MARGIN` / `SCENE_CUT_DETECT_THRESHOLD` | on / `0.5` / `0.4` | cut mode: nudge each clip boundary off the original footage's hard cuts so the edit point doesn't flash a sliver of the adjacent shot (闪烁). source_start moves forward onto / source_end back onto any shot-change within the margin; boundaries already on a cut, or that would shrink a clip below ~0.5s, are left as-is. Set `SCENE_CUT_SNAP=0` to disable |
| VLM workers | `VLM_WORKERS` | `8` | lower to 1 if a proxy/WAF rate-limits |
| Subtitle size | `SUBTITLE_FONT_SIZE` / `SUBTITLE_MARGIN_V` | `42` / `48` | look & placement |
| 整理 / index | `--no-consolidate` / `--consolidate-asr` | on | build the understanding index (and optionally clean ASR); use `--no-consolidate` to skip |
| Advisory / strict narration review | `REVIEW_NARRATION` / `--review-narration` / `--no-review-narration`; strict: `REQUIRE_NARRATION_REVIEW` / `--require-narration-review` | advisory on, strict off | runs `video-script/review.py` after validation and before TTS. Default advisory mode is fail-open; strict mode blocks TTS on review failure, parse error, or error-severity findings. In cut mode the reviewer uses `clip_plan_validated.json` to remap VLM/ASR grounding onto the output timeline |
| 剪映 export (optional) | `--export-jianying` / `EXPORT_JIANYING` | off | after rendering, also write a 剪映/JianYing draft from `timeline.json`. Decoupled — the core render never needs it |
| 剪映 draft dir | `JIANYING_DRAFT_DIR` | work_dir | parent folder for the exported draft (point it at 剪映's drafts root to open in-app) |
| 剪映 bundle media | `JIANYING_BUNDLE_MEDIA` / `--jianying-no-bundle-media` | **on** | copies media into the draft folder so it is self-contained. **Required on macOS** — 剪映 is sandboxed and cannot read external paths, so an unbundled draft opens with all media offline. Use `--jianying-no-bundle-media` only if 剪映 can reach the original paths |
| Source video | `--source-video` | — | original video (cut mode) so `timeline.json` / 剪映 export reference the real source clips instead of the concatenated `edited_source.mp4`; direct `video-assemble` runs intentionally ignore ambient `SOURCE_VIDEO` unless `--source-video` is passed |

`video-assemble` always writes `timeline.json` — a backend-neutral multi-track model
(video / original-audio / narration / BGM / subtitle, with ducking automation). The
canonical renderer is ffmpeg; the 剪映 exporter is an optional consumer of the same file. Subtitle text in `timeline.json` is display-ready and follows the same terminal-punctuation policy as SRT/ASS.

See each stage skill's SKILL.md for the full per-stage option list.
