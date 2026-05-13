---
name: video-recap
description: >
 Generate Chinese voiceover / narration / recap videos from any input video.
 Use when the user provides a video file (.mp4 / .mov / .mkv / .webm) and asks
 to add narration, generate voiceover, dub, summarize, or produce a recap.
 Supports: 短剧 / 电视剧 / 电影 / 纪录片 / 科普视频.
 The narration script is written by the agent (you), not by an LLM API call.
 Pipeline: scene detection → VLM analysis → ASR → narration (you write it) →
 TTS → assembly.
 触发词: 视频解说, 视频旁白, 生成解说, 视频recap, video recap, voiceover,
 narration, auto-dub, recap.
---

## References（按需读取）

| 何时读 | 文档 |
|---|---|
| **写 narration.json 之前必读** | `references/agent-mode-workflow.md` |
| 撰写解说词时（风格 / 反幻觉 / 字数公式） | `references/prompt-templates.md` |
| 读写中间 JSON | `references/data-schema.md` |
| 改 CLI 参数或环境变量 | `references/parameters.md` |
| 中断恢复 / 局部重跑 | `references/pipeline-resume.md` |
| 调 ducking / zone / volume | `references/internal-config.md` |

## 安装与依赖

```bash
brew install ffmpeg && pip3 install edge-tts
export OPENAI_API_KEY=***
# 可选：OPENAI_API_URL / OPENAI_MODEL
```

推荐安装方式：

```bash
git clone <repo> /tmp/video-recap-repo
ln -s /tmp/video-recap-repo/skills/video-recap ~/.claude/skills/video-recap
```

## 使用流程

> 解说词必须由 Agent 亲自撰写，**禁止**调用 LLM API 自动生成。
> 命令必须带 `--agent-mode`。

### 1. 跑前置 pipeline（在 Step 5 前自动暂停）

```bash
python3 scripts/video_recap.py <video> --agent-mode --tts edge-tts --context "背景"
```

### 2. 撰写解说词

读 vlm_analysis / asr_result / silence_periods，写 `work_dir/narration.json`。
字段格式与铁律见 `agent-mode-workflow.md`。

### 3. （可选）背景调研

调用 browser-cdp skill，写 `work_dir/background_research.json`。

### 4. 继续 TTS + 组装

```bash
touch work_dir/.step_script.done
rm -rf work_dir/tts_segments/ work_dir/.step_tts.done \
  work_dir/.step_assemble.done work_dir/tts_meta.json
python3 scripts/video_recap.py <video> --resume work_dir
```

⚠️ 改完 narration.json 必须删 tts_segments/，否则复用旧音频。

## 输出

- `recap_<video>.mp4` — 最终视频
- `subtitles.srt` — 字幕
- `work_dir/` — 所有中间 JSON
