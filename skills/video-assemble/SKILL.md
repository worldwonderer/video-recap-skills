---
name: video-assemble
user-invocable: false
description: >
 合成视频解说最终成片：把旁白音频铺到源视频上，按旁白窗口压低原声，生成 SRT / ASS 字幕并可烧录，
 最后做响度标准化。作为最终合成阶段使用。输入源视频、tts_meta.json 与旁白位置；
 输出 recap 成片和字幕。触发词：视频合成、混音、字幕、压字幕、assemble video、mux、ducking、subtitles、成片。
---

## 1. 定位

本技能负责最终合成：

1. 把各段旁白音频放到视频时间线上。
2. 在旁白窗口内压低原声，支持 fixed / sidechain / zone 模式。
3. 根据旁白位置生成 `subtitles.srt`；默认同时生成并烧录 `subtitles.ass`，`--no-burn-subtitles` 可关闭。
4. 可选把最终响度标准化到目标 LUFS。

## 2. 声音收尾契约

合成阶段只实现创作决定，不凭空制造决定。Agent 在写旁白位置前，已在 `visual_audio_board.json` 为每个 beat 指定 `audio_owner`：

- `original_dialogue`
- `action_sound`
- `ambience` / `music`
- `silence`
- `narration`

因此，旁白间隙是主动选择，不是必须填满的空白。不要为了“更满”而加入通用 BGM、压住必须听见的台词或消除有意义的沉默。

当前渲染器不解析 `visual_audio_board.json`；Agent 通过旁白时间、`overlaps_speech`、原声留白与现有混音参数落实这些决定。

## 3. 输入契约

- `<video>`：源视频；cut 模式下为 `edited_source.mp4`。
- `work_dir/tts_meta.json`：配音阶段写出的 `{segments: [...]}`。每段包含 `audio_path`、时间、`pause_after_ms`、`overlaps_speech` 和用于混音/字幕的位置。

下面的 `scripts/...` 均相对于本技能目录。若执行器从仓库根目录启动，请给脚本路径加上本技能的绝对目录。脚本不从其他技能目录读取文件；外部输入仅限命令显式传入的视频、参数与 `work_dir` 产物。

## 4. 运行命令

```bash
python3 scripts/assemble.py <video> --work-dir <work_dir> \
  [--recap-stem <name>] [--output-dir <dir>] [--no-burn-subtitles] \
  [--subtitle-y-top <inclusive-y> --subtitle-y-bot <exclusive-y>] \
  [--source-video <orig.mp4>] [--export-jianying [--jianying-out <dir>]]
```

## 5. 输出契约

- `recap_<stem>.mp4`：稳定的最终输出别名；每次运行覆盖更新。
- `work_dir/output.mp4`：工作目录内成片。
- `subtitles.srt`：旁白字幕；烧录时另有 `subtitles.ass`。
- `timeline.json`：后端无关的多轨模型，包含视频、原声、旁白、BGM、字幕和 ducking 自动化。
- `_placed_*.wav`：实际写入主混音的完整逐段旁白 PCM；时间线与剪映只引用这些文件。
- `assembly_manifest.json`：输入来源、cut 来源指纹、渲染设置与最终输出路径。
- `assembly_qc.json`：旁白完整性、原声句末交接、时间线素材时长与交付质量的发布门禁。
- 剪映草稿目录：仅 `--export-jianying` 时生成，包含 `draft_content.json`、`draft_info.json` 与 `draft_meta_info.json`。

## 6. 合成规则

- 音频按轨道混合：原声、可选 BGM 与旁白各自独立。
- 旁白不做任何容差裁尾；温和加速后仍放不下即 `no_safe_fit`。每段 `_placed_*.wav`
  必须与序列化后的时间线区间等长或更短，否则 `timeline_audio_mismatch` 阻断。
- 原声在旁白结束后保持压低到下一可靠句末的 `pause_start`，只在实测停顿内渐强，
  于 `source_restore_at` 回满；无后续锚点时保持压低到时间线末端，而不是放出半句。
- `--export-jianying` / `EXPORT_JIANYING=1` 可把 `timeline.json` 导出为可编辑草稿。cut 模式应传 `--source-video <orig>`，让草稿引用真实原片区间。
- 剪映导出默认把视频、音频与图片复制到 `Resources/local/{video,audio,image}`，保持草稿可搬迁；`--jianying-no-bundle-media` 只适合原路径始终可访问的情况。
- 重叠覆盖物会拆到编号轨道；非空目标目录不会覆盖，而会创建编号兄弟目录。
- 常速、倒放、变换、富文本、转场、蒙版、LUT、绿幕复合草稿及显式特效轨道通过 timeline v2 扩展表达。需要素材包的功能只接受调用方合法提供的离线资源。
- 剪映草稿引用未烧录的源视频，因此原片硬字幕仍会保留，必要时在剪映内另行遮罩。
- 字幕外观可用 `SUBTITLE_FONT_SIZE`、`SUBTITLE_MARGIN_V`、`SUBTITLE_MAX_CHARS` 等控制。
- `SUBTITLE_Y_TOP/BOT` 把 ASS 基线放到测得的原片字幕区域，坐标为半开 `[top, bot)`；显式遮罩策略下默认 `SUBTITLE_MASK_OPACITY=0.6`，`SOURCE_SUBTITLE_MASK_TIMING=narration`。
- 原声在旁白间隙回到 `IDLE_ORIG_VOLUME`，旁白下压到 `SPEECH_DUCKING_VOLUME`；`DUCK_FADE_SECONDS` 控制过渡。还可配置 `DUCKING_MODE`、`ZONE_DUCKING_VOLUME`、`FINAL_LOUDNORM` 与 `TARGET_LUFS`。
- 可通过 `BGM_PATH` 指定 BGM；它会循环到成片长度，并按 `BGM_VOLUME` / `BGM_DUCKING_VOLUME` 混音。不要在没有创作依据时设置通用 BGM。
- 烧录字幕需要带 `subtitles` / libass 的 ffmpeg；合成阶段会预检并在缺失时明确失败。
- 原声留白中的对白字幕优先读取 Agent 校对的 `original_subtitles.json`；否则保守映射 ASR。只有遮罩覆盖留白或用户字幕明确要求替换时才烧录原声对白，并用 `「」` 与旁白区分。

## 7. 能力边界

- 不生成旁白文字，也不合成 TTS。
- 不重新转写视频，不擅自改变 Agent 的时间决定。
- 字幕烧录默认开启；关闭时不会重编码绘制字幕区域。
