---
name: video-recap
description: >
 Generate Chinese voiceover / narration / recap videos from an input video.
 Use when the user provides a video file (.mp4 / .mov / .mkv / .webm) and asks
 to add narration, generate voiceover, dub, summarize, or produce a recap.
 Supports: 短剧 / 电视剧 / 电影 / 纪录片 / 科普视频.
 Pipeline: scene detection → VLM analysis → ASR → agent writes narration.json
 (optional cut mode: agent writes clip_plan.json) → TTS → assembly.
 触发词: 视频解说, 视频旁白, 生成解说, 视频recap, video recap, voiceover,
 narration, auto-dub, recap.
---

## References（按需读取）

| 何时读 | 文档 |
|---|---|
| **写 narration.json 之前必读** | `references/agent-mode-workflow.md` |
| 撰写解说词时（风格 / 反幻觉 / 字数公式） | `references/agent-mode-workflow.md` |
| 读写中间 JSON | `references/data-schema.md` |
| 改 CLI 参数或环境变量 | `references/parameters.md` |
| 中断恢复 / 局部重跑 | `references/pipeline-resume.md` |
| 调 ducking / zone / volume | `references/internal-config.md` |

## 安装与依赖

```bash
brew install ffmpeg && pip3 install edge-tts
export OPENAI_API_KEY=***
export OPENAI_MODEL=doubao-seed-2-0-lite-260428
# 可选：OPENAI_API_URL
```

MiMo 可选（勿把 key 写入仓库）：`OPENAI_*` 负责帧级 VLM，`MIMO_API_KEY`（+`MIMO_MODEL`）同时负责 MiMo 分片视频理解和 TTS；需要拆网关时再设 `MIMO_VIDEO_*` / `MIMO_TTS_*`。`tp-*` Token Plan key 默认走 cn 集群，可用 `MIMO_TOKEN_PLAN_CLUSTER=sgp|ams` 或 `MIMO_API_URL` 覆盖。

推荐安装：`git clone <repo> /tmp/video-recap-repo && ln -s /tmp/video-recap-repo/skills/video-recap ~/.claude/skills/video-recap`

## 使用流程

### 1. 运行前置分析（自动暂停）

```bash
python3 scripts/video_recap.py <video> --context "背景"
```

开启 MiMo 分片视频理解 + 指定 TTS 音色：`--mimo-video-overview --mimo-tts-voice 冰糖`（有 `MIMO_API_KEY` 时 TTS 默认选 MiMo）。

剪辑式解说（长视频剪短）加：

```bash
python3 scripts/video_recap.py <video> --edit-mode cut --target-duration 10m
```

### 2. 撰写解说词

读取 `work_dir/agent_narration_brief.md` 以及 vlm_analysis / asr_result / silence_periods，写 `work_dir/narration.json`。cut 模式还要按 `agent-mode-workflow.md` 先写 `clip_plan.json`，完成 `clip_plan_review.md` 片段审阅后再写解说。时间戳都使用原视频时间。
字段格式、cut 模式顺序与写作规则见 `agent-mode-workflow.md`。

### 3. （可选）背景调研

使用任意可用搜索/浏览方式调研，写 `work_dir/background_research.json`；没有工具就跳过。

### 4. 继续 TTS + 组装

```bash
python3 scripts/video_recap.py <video> --resume work_dir
```

需要把解说字幕压制进视频时：

```bash
python3 scripts/video_recap.py <video> --resume work_dir --burn-subtitles
```

⚠️ 改完 narration.json 后如需重配音，按 `references/pipeline-resume.md` 清理 TTS/组装缓存再续跑。cut 模式下 CLI 会自动检测 `clip_plan.json` / `narration.json` 是否比剪辑产物更新并重建映射。

## 自检

```bash
python3 scripts/video_recap.py --doctor
```

## 输出

- `recap_<video>.mp4` — 最终视频
- `subtitles.srt` — 字幕
- `subtitles.ass` — `--burn-subtitles` 时用于压制的字幕
- `work_dir/agent_narration_brief.md` — 解说词写作 brief
- `work_dir/asr_writing_chunks.json` — 按语义句界切分的 ASR 写稿块
- `work_dir/timeline_fusion.json` — VLM/ASR/静音窗口对齐后的时间轴视图
- `work_dir/narration.json` — Agent 写的解说词
- `work_dir/clip_plan.json` — cut 模式下 Agent 选择的原片片段
- `work_dir/mimo_video_overview.json` — 可选 MiMo 场景分片视频理解结果
- `work_dir/edited_source.mp4` — cut 模式下拼出的短视频源
- `work_dir/narration_mapped.json` — cut 模式下映射到短视频时间轴的解说词
- `work_dir/workflow_state.json` — 步骤状态、耗时、参数指纹与视频内容指纹
- `work_dir/` — 所有中间 JSON
