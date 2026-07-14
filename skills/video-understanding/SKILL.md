---
name: video-understanding
user-invocable: false
description: >
 把视频分析为结构化理解索引：场景检测、ASR 转写、逐场景 VLM 观察、静音窗口、融合时间线和写作 brief。
 用于理解、索引或总结视频，也作为后续创作前的分析阶段。输入视频文件；输出 scenes.json、
 asr_result.json、vlm_analysis.json、silence_periods.json、timeline_fusion.json、agent_narration_brief.md。
 触发词：视频理解、视频分析、视频索引、video understanding、analyze video、看懂视频。
---

## 1. 定位

本技能把源视频转成 Agent 与下游阶段可读取的理解索引。它的创作角色是**素材观察员 / 场记**，不是导演：

- 先观察，再解释；事实与推断分开。
- 除了“发生了什么”，还要让下游看见知识、权力、目标、关系或情绪在哪一刻变化。
- 标出由谁的 POV 承载变化、哪个反应或表演不可替代，以及哪里存在完整台词/动作的自然剪辑边界。
- 证据不足时保留不确定性，不制造戏剧结论。

## 2. 处理阶段

1. **场景检测**：写 `scenes.json`，包含切点、时长和废片段过滤结果。
2. **抽帧**：为视觉分析提取代表帧。
3. **ASR**：通过 `mimo-v2.5-asr` 写时间戳对白 `asr_result.json`。
4. **静音检测**：写 `silence_periods.json`，标注安静窗口与 `has_speech`。
5. **VLM 观察**：写 `vlm_analysis.json`，包含场景描述、深层分析和 `frame_facts`。
6. **时间线融合与创作 brief**：写 `timeline_fusion.json`、`asr_writing_chunks.json` 和 `agent_narration_brief.md`。

各阶段只有在输出产物与 provenance sidecar 同时匹配当前视频及影响结果的设置时才会复用；`--force` 强制重算。

## 3. 环境要求

```bash
# ffmpeg: brew install ffmpeg | apt install ffmpeg | choco install ffmpeg
export MIMO_API_KEY=***
```

ASR 使用 `mimo-v2.5-asr`；VLM 使用 `mimo-v2.5`。`--skip-asr` 可跳过对白转写，但完整理解仍需要 `MIMO_API_KEY` 运行 VLM。`--mimo-video-overview` 可开启按场景块的视频概览。

若 `work_dir/background_research.json` 存在，本技能会把剧情梗概和角色名折入 VLM 上下文；`--context` 可补充一条简短提示。

下面的 `scripts/...` 均相对于本技能目录。若执行器从仓库根目录启动，请给脚本路径加上本技能的绝对目录。脚本不从其他技能目录读取文件；外部输入仅限命令显式传入的视频、参数与 `work_dir` 产物。

## 4. 运行命令

```bash
python3 scripts/understand.py <video> --work-dir <work_dir> \
  [--context "节目名/角色名"] [--scene-threshold 0.1] [--skip-asr] [--mimo-video-overview] [--force]
```

## 5. 输出契约

| 文件 | 内容 |
|------|------|
| `scenes.json` | 场景切点、起止时间与时长 |
| `asr_result.json` | `[{start, end, text}]` 时间戳对白 |
| `vlm_analysis.json` | 逐场景描述、深层分析与 `frame_facts` |
| `silence_periods.json` | `[{start, end, duration, has_speech}]` 安静窗口 |
| `timeline_fusion.json` | VLM、ASR 与静音信息的统一时间线 |
| `asr_writing_chunks.json` | 按句界和场景切分的 ASR 写作块 |
| `agent_narration_brief.md` | Agent 首先阅读的创作简报 |

后续写作阶段根据创作简报与索引制定方案并写 `narration.json`。

## 6. 参考资料

- 背景调研：`references/research-guide.md`，产出 `background_research.json`。
- JSON 结构：`references/data-schema.md`。

## 7. 能力边界

- 不写解说词，也不做解说评分；只负责生成理解索引与创作简报。
- 不剪辑、不配音、不合成视频。
- 不编造信号无法支持的剧情；当 ASR / VLM 过薄时输出素材警告。
- 不发布、不调度，只向 `work_dir` 写产物并停止。
