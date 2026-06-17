# Changelog

All notable changes to this project are documented here. This project adheres to
[Semantic Versioning](https://semver.org/).

## [0.2.1] - 2026-06-17

A delivery-quality release: narration now plays in blocks with the original audio breathing
between them at full volume, and the burned-in subtitle band no longer compresses the picture.

### Changed

- **Narration is delivered in BLOCKS, ~7:3.** Each beat is a few sentences written as one
  continuous thought and synthesized as a single fluent TTS utterance — fixing the choppy,
  sentence-by-sentence delivery. Between blocks the recap leaves deliberate original-audio
  blocks (~30% of the timeline) where the original scene plays at FULL volume.
- **Original-audio blocks play at full volume.** `idle_orig_volume` now defaults to `1.0` and
  `duck_bridge_seconds` to `1.5` (was `12`), so the original is ducked only under a narration
  block and swells back to full in the gaps, instead of sitting under one permanent low bed.
  This reverses the 0.2.0 "continuous bed" default. Tune with `IDLE_ORIG_VOLUME` /
  `DUCK_BRIDGE_SECONDS`.
- **Burned-in subtitles are split into short one-line chunks** timed karaoke-style across each
  block, and the source-subtitle masking band is sized for ONE line (~14% of height) instead of
  two (~23%) — the black band no longer compresses the picture.
- **The brief and lint steer block authoring.** The agent is told to write blocks and leave
  ~30% original-audio gaps; the per-sentence density lint is replaced by a block-coverage lint
  (`no_original_blocks` / `under_narrated` / `no_original_breaks` / `fragmented_beats`), and the
  block count is derived from coverage instead of beats-per-minute.

### Fixed

- **Blocks are no longer truncated by the speed-up.** `voiceover` sized a segment's text against
  the raw TTS duration, ignoring the `narration_speed` (1.3×) atempo that assemble applies before
  placement — so a correctly-budgeted block was clipped into a fragment. The truncation budget now
  accounts for `narration_speed`.

## [0.2.0] - 2026-06-16

A quality-focused release that re-architects cut mode and the narration mix so the
recap feels like a recap, not captions over a clip.

### Changed

- **Cut mode is now cut-first / narrate-second (two pauses).** The orchestrator renders
  `edited_source.mp4` from `clip_plan.json` first, then asks the agent to write
  `narration.json` against that real output timeline. Narration and picture stay in sync
  by construction — the old source→output remap that could silently drop or clamp beats is
  gone. Full mode is unchanged (single pause).
- **Continuous original-audio bed.** The original is ducked into one continuous low bed
  under the narration instead of swelling back up between sentences. Inter-beat gaps shorter
  than `duck_bridge_seconds` (default 12s, just above the max narration gap) stay ducked;
  only the lead-in and lead-out return to full volume. Tune with `DUCK_BRIDGE_SECONDS`.
- **Narration density is a guide, not a quota.** The brief frames beats/min as a target to
  aim for, explicitly telling the agent never to pad with filler or pixel-description to hit
  a number — fewer "cold", caption-like recaps.
- **`--consolidate` story index is on by default**, with a backward-compatible manifest shim
  so existing `work_dir`s still resume. Use `--no-consolidate` to opt out.
- **Research directive only fires when the substrate is thin/empty** (not on every titled
  run), and the orchestrator surfaces a research hint in the pause banner.

### Added

- **Cut-desync floor:** narration is linted against the normalized clip plan, with a blocking
  preflight that fails before TTS on heavy drop / too-sparse / long-gap output; `--allow-sparse-cut`
  ships an intentional montage anyway.
- **Phase ledger (`recap_phase.json`)** for deterministic cut-mode resume; a stale narration
  from a changed `clip_plan` can no longer resume into TTS.
- **`duck_bridge_seconds`** config knob (env `DUCK_BRIDGE_SECONDS`).

### Fixed

- **Long-video understanding rides out MiMo cluster rate limits.** A full episode fans out
  into ~90 ASR + ~185 VLM calls; the MiMo endpoints now retry up to 10× with a 60s backoff
  cap (plus a 10s floor when the server sends no `Retry-After`), and an optional
  `ASR_THROTTLE_SECONDS` spaces sequential ASR — so a transient 429 no longer aborts the run.
- **Resume cannot reuse stale artifacts.** Cached-artifact reuse now proves it matches the
  current source bytes / settings and rejects stale provenance, so a changed input or config
  can no longer silently resume on an out-of-date intermediate.

## [0.1.0]

- Initial release: turn any video into a Chinese-narration recap on `ffmpeg` + one Xiaomi
  MiMo API key. Five independent skills (understanding, script, cut, voiceover, assemble)
  plus a thin orchestrator; optional 剪映 draft export.
