# Changelog

All notable changes to this project are documented here. This project adheres to
[Semantic Versioning](https://semver.org/).

## [0.2.3] - 2026-06-19

一轮成片质量打磨：原声留白字幕更准（可自带字幕）、画面理解更密、解说去掉破折号、评审更稳。

### 新增

- **自带原声字幕（更准）。** 解说留白处的原声字幕，除了 Agent 校对、ASR 兜底之外，现在可以直接放一份准确的字幕文件作为**首选来源**：`work_dir/user_subtitles.json`（`[{start,end,text}]`，默认按成片时间轴；或写成 `{"timeline":"source","lines":[...]}` 用原片时间轴，按剪辑计划自动映射到成片）或 `user_subtitles.srt` / `.ass`（默认按原片时间轴映射）。优先级：自带字幕 › Agent 校对的 `original_subtitles.json` › ASR 兜底。
- **逐帧采样随场景时长伸缩。** VLM 每个场景的取帧数过去硬上限 6 帧，长场景（合并后可达上百秒）只能 1 帧／约 20 秒，`frame_facts` 严重稀疏。现按场景时长伸缩（约每 `VLM_SECONDS_PER_FRAME`=4 秒一帧，下限 3、上限 `VLM_MAX_FRAMES`=16），长场景的画面理解不再被饿死；VLM `max_tokens` 800→1500（`VLM_MAX_TOKENS`）。
- **MiMo 视频概览可作主理解来源。** 开启视频概览（`--mimo-video-overview` / `MIMO_VIDEO_OVERVIEW=1`）时，它会成为每个场景的**主要描述**（带动态、读得懂剧情），逐帧 `frame_facts` 仍保留作锚点与兜底；因为不动 `frame_facts`，substrate 评级不会因此回退。概览仍是可选项（默认关闭）。

### 变更

- **解说不再用破折号。** 破折号烧进字幕里很突兀：写作规则禁止在解说与 `original_subtitles.json` 里用破折号（——／—），渲染时再做一道归一化（替换为逗号）兜底；只改字幕显示，不动 TTS 朗读文本。
- **解说评审更确定、只对硬伤拦。** 评委固定 `temperature=0`+种子，复跑结论一致；只有 `hallucination`／`incomplete`（事实类）能在严格模式拦截，文笔类意见（钩子弱、念画面、套话等）一律降为提示；评审规则承认 `background_research` 与画面、对白并列为有效依据，不再把有据可查的设定误判成幻觉。
- **覆盖率指标按写作预算同速率计。** 解说覆盖率过去用 4.55 字／秒打分、却用 3.87 字／秒给 Agent 配额，比自己的预算还严约 18%，容易误报「讲得太少」。现统一用 3.87（含 `speech_safety_margin`）；并把几个覆盖率阈值提升为真正的 CONFIG 项。
- **ASR 人名按背景资料纠错。** 转写后用 `background_research.json` 里的人名修正单字同音错误（如 叶青眉→叶轻眉），严格限定「恰好一字之差、且窗口本身不是已知人名」，避免误改。
- **视频概览部分被审核拦截时降级。** 概览分片若部分被内容审核拦截，不再整体中止理解，而是用可用分片降级产出、未覆盖场景回退到逐帧描述；概览取帧帧率 `mimo_video_fps` 2→3。

### 修复

- **原声留白字幕与原声对不上。** 字幕时间过去依赖粗粒度 ASR（按块时间戳、中点估时），偶尔和原声对不上。现在「精确来源」（自带字幕／Agent 校对稿）按句**区间裁剪**精确落到所覆盖的留白：跨解说块的句子按时间比例切成各段、不再整句重复出现；过密的行截断显示而非直接丢成空白。

## [0.2.2] - 2026-06-18

让分块解说的成片更连贯、更好看：给原声留白补上**校对过**的字幕、解说与原声自然衔接、剪辑不再切断台词；并把会到最后才炸的失败提前暴露。

### 新增

- **原声留白也烧字幕了。** 解说块之间留给原声的留白，过去字幕是空的（解说字幕只写解说，原片自带字幕又被遮挡）。现在这些留白会烧上**原声台词字幕**，并用 `「」` 与解说区分开。优先采用 Agent 校对过的 `original_subtitles.json`（OUTPUT 时间轴 `[{start,end,text}]`：订正 ASR 错字与人名、只保留留白里真正出声的台词）；没有该文件时退回保守的 ASR 兜底——按句归到它所在的那一段留白、跳过太密读不完的行（`SUBTITLE_ORIGINAL_IN_GAPS`，默认开；cut 模式按剪辑计划把 ASR 从源时间映射到成片时间）。
- **剪辑不再切断一句台词（cut 模式）。** `video-cut` 会把每个片段的结尾向后吸附到最近的自然停顿（依据 `silence_periods.json`，上限 `CLIP_SNAP_MAX_EXTEND`，默认 2 秒；`SNAP_CLIP_LINE_END` 可开关），让原声把话说完；选片 brief 也提示 Agent 在完整句尾收口。
- **字幕烧录预检（快速失败）。** 烧字幕需要带 libass（`subtitles` 滤镜）的 ffmpeg。编排器在整条流程开跑前就检查，缺失即报错并给出处置（装一个带 libass 的 ffmpeg，或加 `--no-burn-subtitles`），不再跑完理解 / VLM / ASR / TTS、到最后渲染才失败；`video-assemble` 单独运行时同样有此预检。
- **成片时直接给出解说评审入口。** 存在 `narration_review.md` 时，编排器收尾会打印它的结论与路径，把内容风险（钩子弱 / 没主线 / 节奏）摆到眼前——仍是建议性，硬门禁只有 `validate.py`。

### 变更

- **解说块与原声自然衔接。** brief、写作规则和评审一起教会 Agent：原声留白前的那一块要把原声**引出来**，留白后的那一块要**接住**原声刚呈现的内容，让解说和它包裹的原声读成一个连贯的 beat，而不是各说各的（评审新增 `disjoint_handoff` 类别）。

### 修复

- **原声字幕过度渲染 / 与解说混在一起。** 早先的实现会把一整段（多句）ASR 文本塞进一小段留白、还在多段留白里重复出现，渲染出根本没说出口的台词。现在按句归属到单段留白、跳过过密的行、并用 `「」` 与解说分隔；最佳效果由 Agent 校对的 `original_subtitles.json` 提供。
- **文档：字幕烧录默认开启。** 两份 README 与 SKILL.md 原先把烧字幕写成需要 `--burn-subtitles` 才开，实际是默认开（用 `--no-burn-subtitles` 关闭）；已更正措辞。

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
