# Changelog

All notable changes to this project are documented here. This project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.4.0] - 2026-07-13

汇总 `v0.3.3` 之后的多源剪辑、QC、字幕与配音改进，并扩展可携带剪映草稿能力。

### 新增

- **贴合原片字幕带。** `tools/measure_subtitle.py` 用 stdlib + ffmpeg 抽帧、检测并输出红框预览；`--subtitle-y-top/--subtitle-y-bot` 把新字幕基线与字号适配到测得坐标，并显式启用该区域遮罩。
- **半透明、解说时段遮罩。** 显式启用原字幕遮罩后，默认改为 `SUBTITLE_MASK_OPACITY=0.6`、`SOURCE_SUBTITLE_MASK_TIMING=narration`，原声留白不再常驻黑条；仍可设为 `1` / `all` 恢复全黑全时段效果。
- **参考音色解说。** recap / voiceover 新增 `--voice-ref`，通过 `mimo-v2.5-tts-voiceclone` 给普通解说克隆音色；新生成时参考音频惰性转码一次、最长取 30 秒，内容与转码版本指纹参与 TTS 缓存校验。
- **正式的建议性 MiMo QC。** `--mimo-qc pre-assemble|post-render|both` 在单源/多源流水线的组装前、成片后各最多发起一次 MiMo 请求，把语义/审美观察聚合进 `mimo_qc.json`；内容缓存、`--mimo-qc-refresh`、最多 6 张/768px 临时抽帧、密钥/base64 不落盘均有回归覆盖。缺 key、401/429、超时、畸形响应和本地异常全部 fail-open，只提示 Agent/用户，永远不生成 blocker。
- **可携带剪映草稿。** video/audio/image 默认打包到 `Resources/local`；timeline v2 新增恒定变速、倒放、transform、富文本/逐字样式、转场/mask/LUT、绿幕复合和离线资源轨道。

### 修复

- 亮色画面不再把整片背景误并为字幕候选；测量结果按源隔离，失败时保留上一轮成功产物。
- 校订版原声字幕使用逐窗口全不透明遮罩，避免与原片硬字幕重影；测量坐标拒绝不兼容的非底部 ASS 对齐。
- voice reference 使用同一不可变快照完成转码和缓存标识，进程内重复调用不再继承上一次音色；CLI 与环境变量统一提前校验。
- 长视频的遮罩滤镜超过安全命令长度时改用 ffmpeg filter script，避免 Windows `CreateProcess` 上限；measured band 统一为 `[top, bot)` 并成为视觉 QC 的真实安全区。
- 测量产物提交失败会回滚整组旧产物；recap 通过显式 assemble 参数传递字幕坐标，不再污染进程环境。
- 删除测试专用/生产不可达的 assemble、review、audio automation、QC rule-loader 兼容层；MiMo/其他 non-deterministic finding 不再有 allow-list 升级 blocker 的逃生口。每个 skill 的 `lib.py` / `brief.py` / `narration.py` 复制仍刻意保留，确保单独 clone/安装即可运行。

### 验证

- 全套 `python3 scripts/test.py` 通过（801 tests）；`ruff`、`compileall`、修改模块 `mypy` clean；其中 assemble 275 tests 覆盖剪映草稿协议、timeline 迁移和便携资源写入。
- 真实 ffmpeg 合成字幕样片验证：测量工具识别 `y=[613,637)`；遮罩像素在留白帧为 `128`、解说帧为 `51`。
- 剪映专业版 `10.8.7-beta1` 实测：视频/解说/BGM/字幕/图片轨在线，预览、保存、关闭与重开正常。


## [0.3.3] - 2026-06-28

多源视频剪辑解说 + 文件系统素材库复用为主线，并合入跨 harness 支持、成片兼容性与竖屏字幕修复、解说评审硬闸等改进。

### 新增

- **多视频剪辑解说（cut 模式）。** 一次传入多个源视频，按 `source_id` 选取片段，剪成一个成片；项目级 `multi_source_manifest.json` 作为 recap / cut / assemble 的来源契约，`clip_plan.json` 每个片段带 `source_id`，重叠检测按源隔离。多视频 MVP 仅开放 `--edit-mode cut`。
- **文件系统素材库复用。** `--material-library-dir` 搭配 `--save-materials` / `--use-materials`，把每个源视频的分析产物沉淀为 grep 友好的 `material.json` / `material.md` / 追加式 `materials_index.jsonl`，不复制原始媒体；按源指纹 + 设置指纹门控恢复，复用前清理旧 work dir 的残留产物。无 DB / embedding / 语义检索，纯文件系统 + `grep`。
- **多源 provenance 透出。** `video-assemble` / `recap_inspect` 在时间线与剪映草稿中保留 `source_id` / `source_path`；个别源缺失时按片段降级并显式标记，保留其余在场源的来源，而非丢弃整条时间线。
- **`video-understanding --brief-only`。** 从已恢复 / 缓存的分析产物重建 OUTPUT 时间轴 brief，不重跑抽帧 / ASR / VLM / 外部 API。
- **跨 harness 支持 + Claude Code marketplace。** Codex 与 OpenClaw 直接读取 `.claude-plugin` 包，无需每 harness 文件；marketplace 命名为 `video-recap`。(#50)
- **解说评审 scorecard + dub-lint 硬闸 + partial-TTS 可见性。** (#49)

### 改进

- **单视频 full / cut / dub 行为保持兼容。** 多视频仅在 cut 模式开放；单源剪辑滤镜图保持不变（字节级一致）。

### 修复

- **异源 concat 几何归一化。** 多源片段先归一到统一画布（scale / pad / setsar / fps / yuv420p）再 concat，分辨率 / SAR / 帧率不同的源视频不再让 ffmpeg 报错；不同分辨率的多视频可正常合成一个成片。
- **多源音轨按源处理。** 个别无声源不再导致整段成片静音；每个片段都有音频（原声或合成静音）。
- **密钥脱敏更精确。** 只脱敏凭证形态（`tp-` / `sk-` / `gh*_` / `AKIA` / JWT 与 `KEY=VALUE`）与凭证命名的 JSON key，不再误伤 transcript / summary 里的 `secret` / `token` 等普通词，也不再把多个 key 合并丢值。
- **出片强制 `yuv420p` + faststart。** 微信 / 手机可播、边下边播。(#51)
- **字幕样式按探测画布缩放。** 修复竖屏 (9:16) 字幕被拉伸。(#53)

### 验证

- 全套 `python3 scripts/test.py` 全部 skill groups passed（551 tests），`ruff` / `compileall` clean。
- 新增真实 ffmpeg 多分辨率 + 混合音频渲染测试（验证异源 concat 与音轨归一化）；密钥脱敏保留正常词 / 不合并 key / 凭证形态测试；assemble 按片段降级（保留在场源 provenance）测试。

## [0.3.2] - 2026-06-22

让剪映草稿导出跟上新版工程结构，方便在剪映专业版里继续精修。

### 新增

- **新版剪映 schema-driven 草稿导出。** 剪映导出从单文件 JSON 拼装拆成 schema / model / builder / track / writer 分层，草稿基线升级到 `version: 360000`、`new_version: 111.0.0`、`app_version: 5.9.5-beta1`，并补齐包含 `common_mask` 在内的新版 `materials` skeleton。
- **素材类型注册表与能力清单。** 明确区分已支持的 `video` / `audio` / `text` / `subtitle` / `speed`，以及预留但暂不写出的 image/sticker/effect/mask 等类别；未知或暂不支持类别会输出 note 并跳过，避免生成畸形草稿。

### 改进

- **剪映导出仍保持可选、懒加载、stdlib-only。** `export_jianying.py` 现在只是薄 facade，核心 ffmpeg 渲染路径不会导入任何 `jianying_*` 模块；`timeline.json` 仍是后端无关的 canonical input，ffmpeg 仍是最终成片判定标准。
- **草稿写入更安全。** 写入器继续保留非空目录避让、媒体打包、路径重写、临时目录原子替换；并新增 `draft_name` 校验，拒绝空名、绝对路径、`..`、以及路径分隔符，防止错误名称逃逸草稿父目录。
- **BGM 循环与音量自动化覆盖更完整。** 循环 BGM 会拆成多段铺满时间线，并把窗口内 `KFTypeVolume` 音量关键帧放到对应片段。

### 验证

- `ruff` / `py_compile` / `mypy --ignore-missing-imports` 覆盖剪映导出模块；相关 assemble/timeline 测试 84 passed，全项目 `scripts/test.py` 全部 skill groups passed。
- 本机剪映专业版 `10.8.7` 实测：生成并打开 schema E2E 草稿；又把历史 `longvacation_2min_work/timeline.json` 转成 `recap_tmp_convert_longvacation_2min_20260623_003900`，剪映已登记并可打开。

## [0.3.0] - 2026-06-20

长视频更稳、跨语言更干净、剪辑更顺眼，并新增解说导航与成片压缩工具。

### 新增

- **VLM 场景分析可断点续传 + 限流自愈。** 长视频（数百场景）过去偶发 HTTP 429 会让整轮画面理解失败、再跑得从头重来。现在每个场景分析完即落盘（`vlm_scene_cache.json`，原子写），失败只重试缺失/失败的场景；遇到限流(429)的场景自动降到 ¼ 并发重试一次，持久性错误（空响应／解析失败）不重试。默认 8 并发不再拖垮长视频。
- **跨语言解说降噪 `FOREIGN_SOURCE_AUDIO`。** 当原片语言与解说不同（如日剧配中文解说）时，解说下方被压低的原声本就听不懂、还会被当成「怪音」。该开关把解说下的原声压到近静音（0.05），而原声留白块仍保持满音量；显式 `SPEECH_DUCKING_VOLUME`／`ZONE_DUCKING_VOLUME` 仍可覆盖。
- **剪辑边界吸附原片切镜头 `SCENE_CUT_SNAP`。** 片段边界若落在原片硬切点附近，会先闪一下相邻镜头再切，形成可见闪烁。新增一道吸附（在自然停顿吸附之后）：用 ffmpeg 在窄窗口里探测原片硬切并把边界移上去（每片段约 2 次轻量探测，复用现有缓存）；已对齐或附近无切点的边界不动，会把片段压到 ~0.5s 以下的吸附跳过。
- **成片压缩参数 `OUTPUT_CRF` / `OUTPUT_PRESET` / `OUTPUT_MAX_HEIGHT`。** 最终混流过去硬编码 `-crf 18 -preset veryfast` 且从不缩放，成片体积偏大。现可调 CRF／preset／高度上限（缩放放在最后，遮挡与字幕先在原分辨率渲染再随帧缩小，更清晰）；默认仍是 18／veryfast／不缩放。demo：长假 2 分钟成片由 119MB 降到 16.9MB。
- **解说导航工具（咨询性，不影响成片）。** 新增只读的 `inspect`（`state` 看流程进度／源视频指纹／下一处暂停；`clip-map` 在成片↔原片时间轴间精确换算，回答「成片 30–60s = 原片哪段」）与视频故事板（源时间轴 + 剪辑成片时间轴的缩略图总览，写作时扫一张图就能定位转场／反转，复用已抽帧不重抽）。任一缺失或异常都只降级提示、绝不阻断流程。

### 变更

- **手动评审自动按成片时间轴。** `review.py` 的 `--timeline` 默认改为 `auto`：检测到已验证的剪辑成片（`clip_plan_validated.json` + `edited_source.mp4`）就按成片时间轴评审，否则按原片。消除了 cut 模式下手动评审把原片时间当成成片、误报一堆「幻觉」的问题（demo 上 4 个假阳性 → 1 个真问题）。编排器显式传入的 `cut_output` 仍优先。
- **ASR 默认分段 30→15s。** 时间戳最坏误差大致减半，并重新启用静音／ASR 交叉校验；代价是每个视频约 2× 顺序 ASR 调用（`ASR_SEGMENT_SECONDS` 可调）。
- **自带字幕在不遮挡时也显示。** 原声留白字幕过去被绑在「遮挡原字幕(mask)」开关上，干净／外语片源（无烧录字幕、mask 关）会连同自带 `user_subtitles.*` 一起被丢掉。现解耦：有自带字幕文件即视为明确意图、在留白处照常显示（干净片源无重影风险）。与 `FOREIGN_SOURCE_AUDIO` 搭配适配「外语剧 + 自带中文字幕」。

### 修复

- **cut 模式 pass2 简报崩溃。** 被拆分的场景拿到字符串 id（如 `"5.0"`）、未拆分的仍是 int，`sorted(scene_ids)` 因 int／str 混排崩溃。改为类型安全排序键（int 在前、拆分串在后），并对字节孪生的 `brief.py`／`narration.py` 同步修改（md5 保持一致）。
- **原声留白字幕滞后。** 粗粒度 ASR（按时钟分箱、按字符位置估时）会让某句晚显示约 6–8s。现「精确来源」与 ASR 兜底都改为按整句、从留白起点顺序排布，多句不再重叠或散到字符比例尾槽。
- **成片压缩两处健壮性（发布前评审发现）。** 奇数 `OUTPUT_MAX_HEIGHT`（如 721）过去会产生奇数高度、被 libx264／yuv420p 拒绝 → 空成片 + 笼统报错；现强制宽高都为偶数。`OUTPUT_CRF=0`（无损，合法值）过去被当假值改成 18；现原样保留。
- **inspect 测试接入 CI。** 新增的 inspect 测试组此前只写进 `scripts/test.sh`，而 CI 实际跑的是 `scripts/test.py`，导致这 22 个测试从未在 CI 运行；现已补进运行器。

### 其他

- demo 换成《悠长假日》第一集 2 分钟 cut 模式解说，集中展示本轮能力（无闪烁边界、跨语言降噪、自带中文字幕留白、CRF24/720p 压缩）；并更新 README 中的 demo 链接。

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
